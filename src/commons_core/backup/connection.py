"""The single canonical PostgreSQL connection contract (R10 remediation).

Why this module exists
-----------------------

The independent risk review (R10 / D13 / T12) demonstrated that the restore
destination *guard* and the *effective* libpq connection could disagree:

* ``?host=other.example.invalid`` in the URL query became ``PGHOST`` — the
  guard validated the authority (``127.0.0.1``) while every PostgreSQL
  subprocess connected somewhere else;
* inherited ``PG*`` environment variables (``PGHOST``, ``PGPORT``,
  ``PGSERVICE``, ``PGSERVICEFILE``, ``PGHOSTADDR``, …) could redirect
  connections that the guard never saw;
* SQLAlchemy and libpq resolved connection parameters through different
  code paths, so "validated" and "used" were never provably identical.

The contract here is fail-closed: ONE parser produces ONE immutable
:class:`CanonicalConnection` for a URL, and that object is the ONLY source
for every downstream representation (libpq subprocess environment,
SQLAlchemy URL). A URL is accepted only if its query string contains
exclusively allowlisted, non-destination parameters — anything that could
redirect the connection (``host``, ``hostaddr``, ``port``, ``service``,
``servicefile``, ``user``, ``dbname``, ``options``, ``passfile``) is a hard
error, not a silent override.

Unix sockets are NOT supported by the guard: a URL without an explicit host
would make libpq fall back to the socket directory, which the destination
guard cannot validate — so hostless URLs are rejected fail-closed.

Security invariant enforced here
--------------------------------

**The connection whose destination is validated as safe is byte-for-byte
the same effective connection used by every backup or restore operation.**

No syntactic host check establishes a network boundary by itself: real
isolation still requires network controls (documented in docs/BACKUP.md and
the threat model). What this module guarantees is *internal consistency*:
validation, SQLAlchemy, ``pg_dump`` and ``pg_restore`` cannot be steered to
different destinations through supported configuration paths.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

__all__ = [
    "ConnectionContractError",
    "CanonicalConnection",
    "canonical_from_url",
    "scrubbed_pg_environ",
]


class ConnectionContractError(RuntimeError):
    """A URL violates the canonical connection contract (fail-closed).

    The message is credential-free by construction: it names parameters
    and hosts, never passwords.
    """


#: libpq connection parameters that can REDIRECT the effective connection.
#: They must never ride in the URL query string — if the caller wants a
#: different destination, the URL authority itself must say so (and then
#: the destination guard judges it).
DESTINATION_PARAMS: frozenset[str] = frozenset(
    {
        "host",
        "hostaddr",
        "port",
        "user",
        "username",
        "dbname",
        "database",
        "service",
        "servicefile",
        "options",
        "passfile",
        "target_session_attrs",
        "require_auth",
        "channel_binding",
        "sslnegotiation",
    }
)

#: Non-destination libpq parameters we recognize and deliberately allow.
#: Anything not listed here is rejected as unsupported (fail-closed) — an
#: allowlist, not a blocklist, so future libpq parameters cannot silently
#: widen the attack surface.
SAFE_QUERY_PARAMS: frozenset[str] = frozenset(
    {
        "sslmode",
        "sslcert",
        "sslkey",
        "sslrootcert",
        "sslpassword",
        "sslsni",
        "sslcrl",
        "sslcrldir",
        "connect_timeout",
        "application_name",
        "keepalives",
        "keepalives_idle",
        "keepalives_interval",
        "keepalives_count",
        "client_encoding",
    }
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _reject_control_chars(where: str, value: str) -> None:
    if _CONTROL_CHARS.search(value):
        raise ConnectionContractError(f"{where} contains control characters — fail-closed")


@dataclass(frozen=True)
class CanonicalConnection:
    """One validated, immutable effective connection.

    Instances are built only through :meth:`from_url` (via
    :func:`canonical_from_url`), which enforces the full contract. Every
    representation below is derived from the SAME fields, so validation and
    execution cannot diverge (the R10 fix).
    """

    host: str
    port: int
    database_name: str
    user: str | None
    password: str | None
    params: Mapping[str, str]

    # -- construction -------------------------------------------------------
    @classmethod
    def from_url(cls, url: str) -> CanonicalConnection:
        if not url or not url.strip():
            raise ConnectionContractError("empty database URL (fail-closed)")
        parts = urlsplit(url.strip())
        scheme = parts.scheme or ""
        if scheme not in ("postgresql", "postgresql+psycopg"):
            raise ConnectionContractError(f"database URL scheme must be postgresql, got {scheme!r}")

        # Host: REQUIRED. A missing host means a Unix-socket connection,
        # which the destination guard cannot validate — rejected here so
        # every caller (guard, SQLAlchemy, subprocess) sees the same rule.
        host = parts.hostname or ""
        if not host:
            raise ConnectionContractError(
                "database URL has no host component — Unix-socket connections "
                "are not supported by the backup/restore guard (fail-closed); "
                "provide an explicit host in the URL authority"
            )
        _reject_control_chars("host", host)

        try:
            port = parts.port if parts.port else 5432
        except ValueError as exc:
            raise ConnectionContractError(f"database URL has an invalid port: {exc}") from exc
        if not 1 <= port <= 65535:
            raise ConnectionContractError(f"database URL port {port} is out of range (fail-closed)")

        database = unquote(parts.path.lstrip("/"))
        if not database:
            raise ConnectionContractError(
                "database URL has no database name component (fail-closed)"
            )
        _reject_control_chars("database name", database)

        user = unquote(parts.username) if parts.username else None
        password = unquote(parts.password) if parts.password else None
        if user:
            _reject_control_chars("user", user)
        if password:
            _reject_control_chars("password", password)

        # Query string: allowlist only. Duplicates are ambiguous (libpq and
        # SQLAlchemy may pick different winners) → rejected.
        params: dict[str, str] = {}
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            normalized = key.lower()
            if normalized in DESTINATION_PARAMS:
                raise ConnectionContractError(
                    f"connection URL query parameter {key!r} can redirect the "
                    "effective connection — destination parameters are not "
                    "allowed in the query string; put the destination in the "
                    "URL authority so the destination guard judges it (R10)"
                )
            if normalized in params:
                raise ConnectionContractError(
                    f"connection URL has duplicate query parameter {key!r} — "
                    "ambiguous destination semantics are rejected (fail-closed)"
                )
            if normalized not in SAFE_QUERY_PARAMS:
                raise ConnectionContractError(
                    f"unsupported connection URL query parameter {key!r} — "
                    "only the reviewed allowlist is accepted (fail-closed)"
                )
            _reject_control_chars(f"query parameter {key!r}", value)
            params[normalized] = value

        return cls(
            host=host,
            port=port,
            database_name=database,
            user=user,
            password=password,
            params=params,
        )

    def with_database(self, database_name: str) -> CanonicalConnection:
        """The same connection pointed at a different database (admin URL)."""
        return CanonicalConnection(
            host=self.host,
            port=self.port,
            database_name=database_name,
            user=self.user,
            password=self.password,
            params=dict(self.params),
        )

    # -- derived representations (same fields → same destination) ----------
    def describe(self) -> str:
        """Credential-free description for logs and error messages."""
        user = f"{self.user}@" if self.user else ""
        extras = ""
        if self.params:
            extras = "?" + "&".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"postgresql://{user}{self.host}:{self.port}/{self.database_name}{extras}"

    def libpq_env(self, *, app_name: str | None = None) -> dict[str, str]:
        """Canonical connection parameters as libpq environment variables.

        Credentials ride in the environment (visible only to the owning
        process and its children), never in argv — unchanged Stage 1 rule.
        """
        env: dict[str, str] = {
            "PGHOST": self.host,
            "PGPORT": str(self.port),
            "PGDATABASE": self.database_name,
        }
        if self.user:
            env["PGUSER"] = self.user
        if self.password:
            env["PGPASSWORD"] = self.password
        for key, value in self.params.items():
            env[f"PG{key.upper().replace('-', '_')}"] = value
        if app_name:
            env.setdefault("PGAPPNAME", app_name)
        return env

    def subprocess_env(self, *, app_name: str | None = None) -> dict[str, str]:
        """A COMPLETE child environment: inherited env minus every ``PG*``
        variable, then the canonical values.

        This is the R10 environment guarantee: inherited ``PGHOST``,
        ``PGSERVICE``, ``PGSERVICEFILE``, ``PGHOSTADDR``, ``PGPASSFILE``…
        can never override the validated destination for a subprocess,
        because they never reach it.
        """
        base = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
        base.update(self.libpq_env(app_name=app_name))
        return base

    def sqlalchemy_url(self) -> str:
        """Rebuild the SQLAlchemy URL from canonical fields ONLY.

        The authority carries host/port/user/password/database; the query
        string (if any) contains ONLY the allowlisted non-destination
        parameters. SQLAlchemy therefore resolves exactly the connection
        the guard validated.
        """
        netloc = ""
        if self.user is not None:
            netloc = quote(self.user, safe="")
            if self.password is not None:
                netloc += f":{quote(self.password, safe='')}"
            netloc += "@"
        netloc += f"{quote(self.host, safe='')}:{self.port}"
        path = f"/{quote(self.database_name, safe='')}"
        query = "&".join(f"{k}={quote(v, safe='')}" for k, v in sorted(self.params.items()))
        return urlunsplit(("postgresql+psycopg", netloc, path, query, ""))


def canonical_from_url(url: str) -> CanonicalConnection:
    """Parse a URL into the single canonical connection (fail-closed)."""
    return CanonicalConnection.from_url(url)


@contextmanager
def scrubbed_pg_environ() -> Iterator[None]:
    """Temporarily remove every inherited ``PG*`` variable from the process.

    SQLAlchemy/psycopg connections read libpq environment variables
    (``PGSERVICE`` and friends can inject ``hostaddr`` even when the DSN
    names a host, redirecting the effective connection). Drills wrap every
    engine interaction in this scrub so inherited variables cannot override
    the validated destination on the Python side either.

    Limitation (documented, honest): this is process-global mutation; the
    drill is single-threaded by design. Hostile *local* users able to
    control the parent environment remain an out-of-scope trust boundary —
    the fix removes silent *inheritance*, not local compromise.
    """
    removed: dict[str, str] = {}
    try:
        for key in list(os.environ):
            if key.startswith("PG"):
                removed[key] = os.environ.pop(key)
        yield
    finally:
        for key, value in removed.items():
            os.environ[key] = value
