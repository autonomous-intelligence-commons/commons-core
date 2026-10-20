"""Fail-closed guards for restore destinations (Stage 1 restore safety).

Restores are the destructive direction of the backup system, so the guard
here is deliberately paranoid:

* The restore tools refuse production-looking destinations. Stage 1 runs
  only against disposable databases, and no automatic production restore
  command exists (by decision — see ADR-0009).
* Hosts must resolve to loopback or RFC1918 private space by default;
  anything else requires an explicit ``allow_nonlocal`` escape hatch that
  is itself flagged in the drill report.
* Database *names* must either match a conservative disposable-database
  pattern or be explicitly allowlisted by the operator
  (``allowed_db_names``). This is the "explicit test-environment
  allowlisting" the review asked for; the railway-ish host check below is
  only an additional heuristic.
* URLs are never echoed: error messages use :func:`describe_url`, which
  drops the username, password, and query string entirely — only the
  scheme, host, port, and database name remain (operational context
  without any identity material).

R10 hardening — the guard and the effective connection are now the SAME
thing: ``validate_restore_destination`` parses the URL through the
single canonical connection contract
(:mod:`commons_core.backup.connection`) that every execution path
(SQLAlchemy, ``pg_dump``, ``pg_restore``) also uses. Query parameters that
could redirect the connection (``host``, ``hostaddr``, ``port``,
``service``, ``user``, ``dbname``, ``options``, ``passfile``, …) are
rejected before any policy check, inherited ``PG*`` environment variables
are scrubbed by the drill before any connection is made, and hostless
URLs (Unix-socket fallback) are refused outright. The guard can no longer
say "127.0.0.1 is safe" while the tools connect somewhere else.

Heuristic limitations (documented honestly, per the review):

* A hostname such as ``db.internal.example`` is neither a literal IP nor a
  Railway pattern — it is rejected by default for lack of proof it is
  private, but ``allow_nonlocal`` can override for legitimately isolated
  networks. Operators must review that override explicitly.
* The production database could in principle sit on a private IP too, so
  the allowlist of disposable *names* is the real gate — never rely on the
  host check alone.
* No syntactic host/port check constitutes a network boundary: real
  isolation requires network controls (see docs/BACKUP.md).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .connection import CanonicalConnection, ConnectionContractError

PRODUCTION_HOST_HINTS = re.compile(r"railway|rlwy", re.IGNORECASE)
DEFAULT_DISPOSABLE_DB_PATTERN = re.compile(
    r"^(aic_(test|backup|drill|restore|synthetic)[a-z0-9_]*|commons_(test|backup)[a-z0-9_]*)$"
)


class UnsafeDestinationError(RuntimeError):
    """The restore destination failed the fail-closed safety guard."""


@dataclass(frozen=True)
class DestinationTarget:
    """A validated restore destination (password stripped; keep ``user`` out
    of rendered output — see :func:`describe_url`)."""

    host: str
    port: int
    database_name: str
    user: str | None
    is_nonlocal: bool

    def describe(self) -> str:
        """Credential-free rendering: never includes the username."""
        return f"{self.host}:{self.port}/{self.database_name}"


def describe_url(url: str) -> str:
    """A credential-free description of a database URL for logs/errors.

    The username, password, and query string are dropped entirely — never
    masked with a reversible scheme, simply not included. What remains is
    operational context only (scheme, host, port, database name): no
    identity material of any kind reaches operational output, under either
    accepted scheme spelling (``postgresql`` or ``postgresql+psycopg`` —
    the bare scheme prefix of the description is NOT a credential leak).
    """
    parts = urlsplit(url)
    host = parts.hostname or "unknown-host"
    port = f":{parts.port}" if parts.port else ""
    db = parts.path.lstrip("/") or "?"
    return f"{parts.scheme or 'postgresql'}://{host}{port}/{db}"


def _host_is_local(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def parse_target(url: str) -> DestinationTarget:
    """Parse a URL through the canonical contract (fail-closed).

    R10: this now goes through :class:`CanonicalConnection`, so a URL that
    only *looks* safe (query-string host overrides, unsupported parameters,
    Unix-socket fallback, duplicate parameters) is rejected here — the same
    parser the execution paths use, never a divergent one.
    """
    try:
        canonical = CanonicalConnection.from_url(url)
    except ConnectionContractError as exc:
        raise UnsafeDestinationError(str(exc)) from exc
    host = canonical.host
    return DestinationTarget(
        host=host,
        port=canonical.port,
        database_name=canonical.database_name,
        user=canonical.user,
        is_nonlocal=not _host_is_local(host),
    )


def validate_restore_destination(
    url: str,
    *,
    allow_nonlocal: bool = False,
    allowed_db_names: tuple[str, ...] = (),
    db_pattern: re.Pattern | None = None,
) -> DestinationTarget:
    """Validate a restore destination or raise :class:`UnsafeDestinationError`.

    Rules (all must pass — fail-closed):

    1. the URL must satisfy the canonical connection contract
       (:mod:`commons_core.backup.connection`): explicit host (no Unix-socket
       fallback), no destination-redirecting or unsupported query
       parameters, no duplicate parameters — the exact contract every
       execution path uses (R10);
    2. scheme must be ``postgresql`` (optionally with the ``+psycopg``
       driver suffix used by SQLAlchemy URLs);
    3. the host must not look like a Railway endpoint (hard refusal that
       no flag overrides — the live production database lives there);
    4. the host must be loopback/private, unless ``allow_nonlocal`` was
       explicitly passed (recorded in the drill report);
    5. the database name must match the disposable-database pattern or be
       present in ``allowed_db_names``.
    """
    parts = urlsplit(url)
    scheme = parts.scheme or ""
    if scheme not in ("postgresql", "postgresql+psycopg"):
        raise UnsafeDestinationError(
            f"restore target scheme must be postgresql, got {scheme!r} "
            f"({describe_url(url)} — fail-closed)"
        )
    if PRODUCTION_HOST_HINTS.search(parts.hostname or ""):
        raise UnsafeDestinationError(
            "restore destination host looks like a Railway production endpoint "
            f"({(parts.hostname or '')!r}) — Stage 1 never restores toward "
            "production; this check is not overridable"
        )
    target = parse_target(url)
    if target.is_nonlocal and not allow_nonlocal:
        raise UnsafeDestinationError(
            f"restore destination host {target.host!r} is not loopback/private — "
            "pass an explicitly reviewed allow_nonlocal=True (CLI: "
            "--allow-nonlocal-destination) to restore toward an isolated "
            "private network host"
        )
    pattern = db_pattern if db_pattern is not None else DEFAULT_DISPOSABLE_DB_PATTERN
    if not target.database_name:
        raise UnsafeDestinationError("restore destination URL has no database name component")
    if target.database_name in allowed_db_names:
        return target
    if not pattern.match(target.database_name):
        raise UnsafeDestinationError(
            f"destination database {target.database_name!r} does not look like a "
            "disposable test database — Stage 1 restores only into names matching "
            f"{pattern.pattern!r}; use an explicit allowlist entry "
            "(CLI: --allow-db NAME) after separate review"
        )
    return target


def scrub_secrets(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Remove credential-shaped substrings from tool output before logging.

    ``extra_secrets`` lets callers additionally redact any secret they know
    about (e.g. the PGPASSWORD value passed to subprocesses). The 64-hex
    pattern also masks application master keys that could leak into output.
    """
    scrubbed = re.sub(r"://[^:/@\s]+:[^@\s]+@", "://***:***@", text)
    scrubbed = re.sub(r"\b[0-9a-f]{64}\b", "***HEX64-REDACTED***", scrubbed)
    for secret in extra_secrets:
        if secret:
            scrubbed = scrubbed.replace(secret, "***REDACTED***")
    return scrubbed
