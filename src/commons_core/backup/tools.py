"""External tool discovery for the Stage 1 backup pipeline.

The backup pipeline drives two families of external binaries:

* ``age`` / ``age-keygen`` (pinned v1.3.2 in CI; the version actually used is
  recorded in every backup manifest) — authenticated public-key encryption.
* ``pg_dump`` / ``pg_restore`` — PostgreSQL logical backup tools. The
  PostgreSQL project only supports a *client that is the same major version
  or newer* than the server, so the toolbelt refuses to run a dump/restore
  when the local client major is older than the server major (fail-closed
  instead of producing a subtly incompatible artifact).

Resolution order (each may be overridden by the environment):

1. ``AIC_BACKUP_AGE_BIN`` / ``AIC_BACKUP_AGE_KEYGEN_BIN`` — explicit path.
2. ``AIC_BACKUP_PG_TOOLS_DIR`` — directory containing pg_dump/pg_restore.
3. ``PATH`` lookup via :func:`shutil.which`.

No tool is ever invoked with credential-bearing arguments (see
:mod:`commons_core.backup.pipeline`), so a process listing cannot leak a
database URL or password.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass

AGE_VERSION_PIN = "v1.3.2"
"""Version pinned and checksum-verified in CI (see .github/workflows/ci.yml).

Recorded here for documentation; the toolbelt intentionally does NOT refuse
other local age versions so the module stays testable outside CI — every
run records the actual version in the manifest for auditability.
"""


class ToolError(RuntimeError):
    """A required external tool is missing or unusable (fail-closed)."""


#: Upper bound on captured stderr lines per subprocess (R12: a chatty
#: tool must not be able to exhaust memory through the reader threads).
STDERR_MAX_LINES = 256


def subprocess_deadline_seconds() -> float:
    """Total wall-clock seconds one backup/restore subprocess may run (R12).

    Default 900s; overridable through ``AIC_BACKUP_SUBPROCESS_DEADLINE``
    for fault-injection tests. No subprocess wait in the backup/restore
    paths is unbounded anymore.
    """
    try:
        return float(os.environ.get("AIC_BACKUP_SUBPROCESS_DEADLINE", "900"))
    except ValueError:
        return 900.0


def start_new_session_flag() -> bool:
    """Run each subprocess in its own process group, where supported (R12).

    Process groups let a deadline kill reap the whole tree (pg_dump may
    spawn helpers); Windows has no process groups, so ``killpg`` is skipped
    there — a documented platform limitation, not a silent guarantee.
    """
    return sys.platform != "win32"


def terminate_process_tree(proc: subprocess.Popen, *, label: str) -> str | None:
    """Bounded, observable termination of one subprocess (R12).

    Graceful SIGTERM to the whole process group first (where supported),
    a short grace period, then SIGKILL. Returns a diagnostic note when the
    process could not be reaped even after SIGKILL — never an unbounded
    wait, never a silent discard of the outcome.
    """
    if proc.poll() is not None:
        return None
    if start_new_session_flag() and hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            proc.terminate()
    else:
        proc.terminate()
    try:
        proc.wait(timeout=5)
        return None
    except subprocess.TimeoutExpired:
        pass
    if start_new_session_flag() and hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
    else:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        return (
            f"{label} could not be reaped within 10s of SIGKILL — "
            "process state is unconfirmed; inspect the host"
        )
    return None


def _resolve(env_var: str, name: str, tools_dir: str | None = None) -> str:
    explicit = os.environ.get(env_var, "").strip()
    if explicit:
        if not os.path.isfile(explicit) or not os.access(explicit, os.X_OK):
            raise ToolError(f"{env_var} points to {explicit!r}, which is not an executable file")
        return explicit
    if tools_dir:
        candidate = os.path.join(tools_dir, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which(name)
    if found:
        return found
    raise ToolError(
        f"required tool {name!r} not found — set {env_var} to its path or add it to PATH"
    )


def _run_version(binary: str) -> str:
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no user input
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError as exc:
        raise ToolError(f"could not execute {binary!r}: {exc}") from exc
    if proc.returncode != 0:
        raise ToolError(f"{binary!r} --version exited with status {proc.returncode}")
    return (proc.stdout or proc.stderr).strip()


_PG_MAJOR_RE = re.compile(r"\(PostgreSQL\)\s+(\d+)\.")


def postgres_major(version_output: str) -> int:
    """Extract the major version from a PostgreSQL tool/server version string.

    >>> postgres_major("pg_dump (PostgreSQL) 18.6 (Ubuntu 18.6-1)")
    18
    """
    match = _PG_MAJOR_RE.search(version_output)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d+)\.", version_output)
    if match:
        return int(match.group(1))
    raise ToolError(f"cannot parse PostgreSQL version from {version_output!r}")


def age_major_version(binary: str) -> str:
    """Return the age tool version string (e.g. ``v1.3.2``)."""
    return _run_version(binary)


@dataclass(frozen=True)
class Toolbelt:
    """Resolved locations and versions of every external tool we invoke."""

    age: str
    pg_dump: str
    pg_restore: str
    age_keygen: str | None = None

    def version_info(self) -> dict[str, str]:
        """Tool versions recorded verbatim in backup manifests (non-secret)."""
        return {
            "age": _run_version(self.age),
            "pg_dump": _run_version(self.pg_dump),
        }

    def ensure_compatible(self, server_major: int) -> None:
        """Fail closed when the pg client major is older than the server.

        PostgreSQL supports dumps/restores made by a client whose major
        version is equal to or newer than the server. An older client may
        produce or accept subtly wrong output, so we refuse up front rather
        than publish an artifact we cannot trust.
        """
        client_major = postgres_major(_run_version(self.pg_dump))
        if client_major < server_major:
            raise ToolError(
                f"pg_dump {client_major}.x is older than PostgreSQL server "
                f"{server_major}.x — logical backup requires a client of the "
                "same major version or newer (fail-closed)"
            )
        restore_major = postgres_major(_run_version(self.pg_restore))
        if restore_major < server_major:
            raise ToolError(
                f"pg_restore {restore_major}.x is older than PostgreSQL server "
                f"{server_major}.x — restore requires a client of the same "
                "major version or newer (fail-closed)"
            )


def locate_toolbelt(*, require_keygen: bool = False) -> Toolbelt:
    """Resolve all external tools; raise :class:`ToolError` if any is missing.

    Never a silent partial success: the backup and restore paths must fail
    safely when dependencies are unavailable (Stage 1 requirement).
    """
    tools_dir = os.environ.get("AIC_BACKUP_PG_TOOLS_DIR", "").strip() or None
    belt = Toolbelt(
        age=_resolve("AIC_BACKUP_AGE_BIN", "age"),
        pg_dump=_resolve("AIC_BACKUP_PG_DUMP_BIN", "pg_dump", tools_dir),
        pg_restore=_resolve("AIC_BACKUP_PG_RESTORE_BIN", "pg_restore", tools_dir),
        age_keygen=None,
    )
    if require_keygen:
        belt = Toolbelt(
            age=belt.age,
            pg_dump=belt.pg_dump,
            pg_restore=belt.pg_restore,
            age_keygen=_resolve("AIC_BACKUP_AGE_KEYGEN_BIN", "age-keygen"),
        )
    return belt
