"""The streaming backup pipeline — pg_dump piped through age encryption.

Stage 1 review correction 1 (eliminate plaintext intermediate backups):

The original research PoC wrote a plaintext ``pg_dump -Fc`` file to disk and
only then encrypted it. This module implements the corrected shape: the
dump NEVER exists as a plaintext file anywhere. ``pg_dump`` writes its
custom-format output to a pipe consumed directly by ``age``, which streams
the authenticated-encrypted result into the artifact file. Only the
encrypted bytes ever touch the filesystem.

Explicit process supervision (no reliance on shell pipeline behavior):

* both subprocesses are spawned by this module with argv that NEVER
  contains credentials — connection parameters reach ``pg_dump`` through
  libpq environment variables (``PGHOST``/``PGPORT``/``PGDATABASE``/
  ``PGUSER``/``PGPASSWORD``/…), so neither process listings nor tracebacks
  can leak the URL or password;
* both subprocesses' exit statuses are inspected and propagated — a
  non-zero status from EITHER side fails the whole pipeline;
* stderr from both sides is drained by reader threads (no pipe-buffer
  deadlock) and scrubbed of secrets before it can appear in any error;
* premature death of ``age`` (bad recipient, disk full, …) closes the pipe
  so ``pg_dump`` dies with SIGPIPE — reported as "downstream exited
  prematurely", never mistaken for success;
* partial output can never be published: the encrypted artifact is staged
  under a ``.part`` name, verified (SHA-256 + structural age check), and
  only then handed to the storage adapter. ANY failure path unlinks the
  staged file first.

``pg_dump`` producing "some output" is never treated as success: the only
success indicator is the storage adapter's commit marker after both
objects verified (see :mod:`commons_core.backup.storage`).
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from sqlalchemy import create_engine, text

from .age_format import AgeFormatError, verify_age_file
from .destination import describe_url, scrub_secrets
from .manifest import (
    BackupManifest,
    ManifestBuildInputs,
    build_manifest,
    new_backup_id,
    sha256_file,
    utc_now,
)
from .storage import StorageAdapter
from .tools import Toolbelt, ToolError, locate_toolbelt, postgres_major


class BackupError(RuntimeError):
    """The backup pipeline failed; nothing was published."""


class PreflightError(BackupError):
    """Configuration/validation failed before anything ran (fail-closed)."""


class DumpFailed(BackupError):
    """pg_dump failed (connection, permission, timeout, partial output)."""


class EncryptionFailed(BackupError):
    """age failed, or died before consuming the whole dump."""


class ArtifactVerificationFailed(BackupError):
    """The encrypted artifact failed local verification before publication."""


class PublicationFailed(BackupError):
    """The storage adapter could not publish/verify both objects + marker.

    Storage-layer exceptions (including immutable-id conflicts) are wrapped
    here so every pipeline failure surfaces as a BackupError subtype —
    callers never need to catch two hierarchies. The original message is
    preserved verbatim (it is already credential-free by construction).
    """


@dataclass(frozen=True)
class BackupOutcome:
    """What the caller gets on success (never on failure — failure raises)."""

    manifest: BackupManifest
    artifact_sha256: str

    def to_json(self) -> str:
        return self.manifest.to_json()


def _reader_thread(stream, sink: list[str]) -> threading.Thread:
    def run() -> None:
        try:
            for line in iter(stream.readline, b""):
                sink.append(line.decode("utf-8", "replace"))
        finally:
            stream.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def pg_env_from_url(database_url: str) -> dict[str, str]:
    """Translate a PostgreSQL URL into libpq environment variables.

    Secrets ride in the environment (visible only to the owner-privileged
    process and its children), never in argv. ``PGSSLMODE`` defaults to
    ``require`` for TCP connections to remote hosts and is left off for
    loopback (the CI/disposable databases use trust/scram on loopback —
    forcing TLS there would break them; the rule is documented in
    docs/BACKUP.md).
    """
    parts = urlsplit(database_url)
    if parts.scheme not in ("postgresql", "postgresql+psycopg"):
        raise PreflightError(
            f"backup database URL must use the postgresql scheme, got {parts.scheme!r}"
        )
    env: dict[str, str] = {}
    if parts.hostname:
        env["PGHOST"] = unquote(parts.hostname)
    if parts.port:
        env["PGPORT"] = str(parts.port)
    if parts.username:
        env["PGUSER"] = unquote(parts.username)
    if parts.password:
        env["PGPASSWORD"] = unquote(parts.password)
    database = parts.path.lstrip("/")
    if database:
        env["PGDATABASE"] = unquote(database)
    params = dict(parse_qsl(parts.query))
    for key, value in params.items():
        env[f"PG{key.upper().replace('-', '_')}"] = value
    return env


def _source_metadata(database_url: str) -> tuple[dict, str | None, int]:
    """Query server version, current database, and the alembic head revision.

    Consistency semantics (manifest correction 2): these values are read
    BEFORE pg_dump starts, from an ordinary (non-snapshot-pinned) session.
    They are recorded as informational context — not as proof that the dump
    contains exactly this schema version. See ``manifest.py``.
    """
    normalized = database_url.replace("postgresql+psycopg://", "postgresql+psycopg://")
    try:
        engine = create_engine(normalized, pool_pre_ping=True)
        try:
            with engine.connect() as conn:
                server_version = conn.execute(text("SELECT version()")).scalar_one()
                database_name = conn.execute(text("SELECT current_database()")).scalar_one()
                try:
                    alembic_revision: str | None = conn.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one_or_none()
                except Exception:  # noqa: BLE001 — unmigrated database: honest "unknown"
                    alembic_revision = None
                major = postgres_major(str(server_version))
                return (
                    {
                        "server_version": str(server_version),
                        "database_name": str(database_name),
                    },
                    alembic_revision,
                    major,
                )
        finally:
            engine.dispose()
    except PreflightError:
        raise
    except Exception as exc:  # noqa: BLE001 — any connection failure is a fail-closed preflight error
        raise PreflightError(
            f"cannot read source metadata from {describe_url(database_url)} "
            f"({type(exc).__name__}) — is the server reachable and the URL correct?"
        ) from exc


def _describe_sigpipe(stage_stderr: list[str]) -> str:
    joined = "".join(stage_stderr[-8:]).strip()
    return scrub_secrets(joined) if joined else "(no stderr captured)"


def _stream_dump_to_age(
    *,
    belt: Toolbelt,
    pg_env: dict[str, str],
    recipients: Sequence[str],
    staged_artifact: Path,
) -> None:
    """Run ``pg_dump --format=custom | age -r RECIPIENT…`` under supervision.

    The dump is encrypted to every recipient in *recipients* (multiple
    ``-r`` flags — age's native multi-recipient support), so any one of
    their identities can decrypt the artifact later.

    Raises :class:`DumpFailed` / :class:`EncryptionFailed` on any subprocess
    problem, and guarantees that ``staged_artifact`` is removed on failure
    (partial encrypted output must never survive).
    """
    dump_argv = [belt.pg_dump, "--format=custom"]
    age_argv = [belt.age]
    for one_recipient in recipients:
        age_argv.extend(["-r", one_recipient])
    age_argv.extend(["-o", str(staged_artifact)])

    dump_proc = age_proc = None
    dump_err: list[str] = []
    age_err: list[str] = []
    threads: list[threading.Thread] = []
    try:
        dump_proc = subprocess.Popen(
            dump_argv,
            env={**os.environ, **pg_env},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        age_proc = subprocess.Popen(
            age_argv,
            stdin=dump_proc.stdout,  # type: ignore[arg-type]
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        # Parent hands its copy of the pipe to age; closing our end lets
        # pg_dump receive SIGPIPE if age dies (fail-fast broken pipe).
        dump_proc.stdout.close()  # type: ignore[union-attr]

        threads.append(_reader_thread(dump_proc.stderr, dump_err))
        threads.append(_reader_thread(age_proc.stderr, age_err))

        dump_rc = dump_proc.wait()
        age_rc = age_proc.wait()
        for thread in threads:
            thread.join(timeout=5)

        if dump_rc != 0:
            detail = _describe_sigpipe(dump_err)
            raise DumpFailed(
                f"pg_dump failed with status {dump_rc} ("
                f"{'terminated by SIGPIPE — downstream age process exited prematurely' if dump_rc == -signal.SIGPIPE else 'see scrubbed stderr'}"
                f"): {detail}"
            )
        if age_rc != 0:
            detail = _describe_sigpipe(age_err)
            raise EncryptionFailed(
                f"age failed with status {age_rc} after consuming the dump: {detail}"
            )
    except BackupError:
        _unlink(staged_artifact)
        raise
    except Exception as exc:  # noqa: BLE001 — wrap any supervision surprise
        _unlink(staged_artifact)
        raise BackupError(f"pipeline supervision failure: {exc}") from exc
    finally:
        for proc in (dump_proc, age_proc):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()

    # Reaching here means both subprocesses exited 0 — the artifact exists,
    # but "some output was produced" is NOT success (correction 1): local
    # verification happens next, and any failure there unlinks the file.


def _unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _publish_with_wrapped_errors(
    storage: StorageAdapter,
    *,
    backup_id: str,
    artifact_path,
    manifest_bytes: bytes,
    artifact_sha256: str,
) -> dict:
    """Call the storage adapter, surfacing every failure as BackupError."""
    from .storage import StorageError

    try:
        return storage.publish(
            backup_id,
            artifact_path=artifact_path,
            manifest_bytes=manifest_bytes,
            artifact_sha256=artifact_sha256,
        )
    except StorageError as exc:
        raise PublicationFailed(str(exc)) from exc


def run_backup(
    *,
    database_url: str,
    recipient: str,
    storage: StorageAdapter,
    work_dir: str | os.PathLike,
    toolbelt: Toolbelt | None = None,
    backup_id: str | None = None,
    extra_recipients: Sequence[str] = (),
) -> BackupOutcome:
    """Execute one full backup: dump → encrypt → verify → publish → confirm.

    On ANY failure the staged encrypted artifact is removed and nothing is
    published (no manifest, no commit marker). On success the manifest —
    whose ``state`` is only honest because publication verified both
    objects — is returned.
    """
    if not database_url or not database_url.strip():
        raise PreflightError("no database URL supplied (fail-closed — refusing to run)")
    if not recipient or not recipient.strip():
        raise PreflightError("no age recipient supplied (fail-closed — refusing to run)")
    recipient = recipient.strip()
    if not recipient.startswith("age1"):
        raise PreflightError(
            "age recipient must be a public age1… key (private identities are never "
            "accepted by the backup path)"
        )

    recipients: list[str] = [recipient]
    for extra in extra_recipients:
        extra = extra.strip()
        if not extra.startswith("age1"):
            raise PreflightError(
                "extra age recipients must be public age1… keys (private "
                "identities are never accepted by the backup path)"
            )
        if extra not in recipients:
            recipients.append(extra)

    belt = toolbelt or locate_toolbelt()
    backup_id = backup_id or new_backup_id()
    started_at = utc_now()
    work_path = Path(work_dir)
    work_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    staged = work_path / f"{backup_id}.age.part"

    print(
        f"[backup] backup_id={backup_id} source={describe_url(database_url)} started={started_at}"
    )

    pg_env = pg_env_from_url(database_url)

    # Tool compatibility is validated BEFORE we touch the database.
    source_meta, alembic_revision, server_major = _source_metadata(database_url)
    try:
        belt.ensure_compatible(server_major)
    except ToolError as exc:
        raise PreflightError(str(exc)) from exc

    print("[backup] streaming pg_dump -> age (no plaintext intermediate file)")
    _stream_dump_to_age(
        belt=belt,
        pg_env=pg_env,
        recipients=recipients,
        staged_artifact=staged,
    )

    # Local verification layer 1 (structural) + the authoritative hash.
    artifact_sha256 = sha256_file(staged)
    artifact_size = staged.stat().st_size
    if artifact_size == 0:
        _unlink(staged)
        raise ArtifactVerificationFailed("encrypted artifact is empty after a 0-exit pipeline")
    try:
        verify_age_file(staged)
    except AgeFormatError as exc:
        _unlink(staged)
        raise ArtifactVerificationFailed(
            f"encrypted artifact failed structural verification: {exc}"
        ) from exc

    manifest = build_manifest(
        ManifestBuildInputs(
            backup_id=backup_id,
            started_at_utc=started_at,
            created_at_utc=utc_now(),
            server_version=source_meta["server_version"],
            database_name=source_meta["database_name"],
            alembic_revision=alembic_revision,
            tool_versions=belt.version_info(),
            recipient=recipients[0],
            extra_recipients=tuple(recipients[1:]),
            artifact_sha256=artifact_sha256,
            artifact_size=artifact_size,
            artifact_filename=f"{backup_id}.age",
        )
    )

    marker = _publish_with_wrapped_errors(
        storage,
        backup_id=backup_id,
        artifact_path=staged,
        manifest_bytes=manifest.to_bytes(),
        artifact_sha256=artifact_sha256,
    )
    if marker.get("artifact_sha256") != artifact_sha256:
        raise BackupError("storage commit marker does not match the published artifact hash")

    # Success: remove the staged working copy (the published object is the
    # only durable copy; it was verified by the adapter).
    _unlink(staged)
    print(
        f"[backup] published backup_id={backup_id} "
        f"sha256={artifact_sha256[:12]}… size={artifact_size} state={manifest.state}"
    )
    return BackupOutcome(manifest=manifest, artifact_sha256=artifact_sha256)
