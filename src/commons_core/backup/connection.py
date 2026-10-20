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
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

__all__ = [
    "ConnectionContractError",
    "InProcessEnvironmentError",
    "CanonicalConnection",
    "canonical_from_url",
]


class ConnectionContractError(RuntimeError):
    """A URL violates the canonical connection contract (fail-closed).

    The message is credential-free by construction: it names parameters
    and hosts, never passwords.
    """


class InProcessEnvironmentError(ConnectionContractError):
    """The inherited environment would influence an in-process connection.

    Raised by :meth:`CanonicalConnection.audit_in_process_environment` when
    a ``PG*`` variable is set that the canonical URL cannot override (the
    parameter is rejected from URLs outright, or the URL simply does not
    pin the slot). Fail-closed: the operation refuses to run rather than
    silently honouring — or silently ignoring — ambient libpq
    configuration. No process-global state is mutated (spec §8 residual
    review: the former ``scrubbed_pg_environ`` context manager temporarily
    mutated ``os.environ``; explicit parameters plus this audit replace it
    with thread-safe per-connection construction).
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

    # -- in-process connections (residual review §8) ------------------------
    def audit_in_process_environment(self) -> None:
        """Fail closed if inherited ``PG*`` variables would fill any slot
        the canonical URL does not pin.

        ``libpq`` resolves every connection parameter with conninfo >
        environment > service file > defaults. The URL pins host, port,
        database, user (when present), password (when present) and the
        allowlisted query parameters, so those slots can never be steered
        by the environment. The variables audited here are the ones that
        would STILL take effect for slots the URL leaves unset — and that
        matter for destination, credentials, identity or trust anchors:

        * ``PGHOSTADDR`` / ``PGSERVICE`` / ``PGSERVICEFILE`` /
          ``PGTARGETSESSIONATTRS`` / ``PGREQUIRESSL`` /
          ``PGSSLMINPROTOCOLVERSION`` / ``PGSSLMAXPROTOCOLVERSION`` /
          ``PGCHANNELBINDING``: parameters the canonical contract rejects
          from URLs outright. A service file or ``PGHOSTADDR`` can redirect
          the effective TCP destination even when the URL names a host, so
          presence is a hard error rather than something to silently
          ignore (ignoring would require mutating ``os.environ`` — see the
          residual review).
        * ``PGPASSWORD`` / ``PGPASSFILE``: only consulted when the URL
          carries no password — refused in exactly that case.
        * ``PGUSER``: only consulted when the URL carries no user.
        * ``PGSSLCERT`` / ``PGSSLKEY`` / ``PGSSLPASSWORD`` /
          ``PGSSLROOTCERT`` / ``PGSSLCRL`` / ``PGSSLCRLDIR``: TLS material
          and trust anchors — refused when the URL does not pin the
          matching parameter itself.

        Behavioural, non-security parameters (``PGAPPNAME``,
        ``PGCONNECT_TIMEOUT``, ``PGOPTIONS``, ``PGCLIENTENCODING``,
        ``PGDATESTYLE``, ``PGTZ``, keepalives…) may still be inherited;
        they cannot change the destination, the credentials or the
        transport security mode, and are documented as such.

        This check NEVER mutates ``os.environ`` — it is therefore safe
        under concurrent invocation from multiple threads (the former
        process-global scrub was not).
        """
        forbidden: set[str] = set(_UNPINNABLE_PG_ENV)
        if self.password is None:
            forbidden |= {"PGPASSWORD", "PGPASSFILE"}
        if self.user is None:
            forbidden.add("PGUSER")
        for env_name, slot in _TLS_TRUST_ENV.items():
            if slot not in self.params:
                forbidden.add(env_name)
        offending = sorted(key for key in os.environ if key in forbidden)
        if offending:
            raise InProcessEnvironmentError(
                "inherited environment variables can influence connection "
                "parameters the database URL does not pin: "
                + ", ".join(offending)
                + " — unset them (or put the values in the database URL) and "
                "retry; in-process connections refuse to run on ambient "
                "libpq configuration (R10 / residual review §8)"
            )

    def psycopg_connect_kwargs(self) -> dict[str, str]:
        """Explicit connect arguments for in-process SQLAlchemy/psycopg use.

        Everything the canonical URL pins already reaches libpq through
        the SQLAlchemy URL (authority + allowlisted query). The one
        security-relevant slot the URL may leave unset is pinned here to
        libpq's documented default, so an inherited ``PGSSLMODE`` can never
        downgrade the transport for an in-process connection:

        * ``sslmode`` — pinned to ``prefer`` when the URL does not set it.

        Returns an empty mapping when the URL already pins everything
        (values are never duplicated between the URL query and
        ``connect_args``).
        """
        kwargs: dict[str, str] = {}
        if "sslmode" not in self.params:
            kwargs["sslmode"] = "prefer"
        return kwargs


def canonical_from_url(url: str) -> CanonicalConnection:
    """Parse a URL into the single canonical connection (fail-closed)."""
    return CanonicalConnection.from_url(url)


#: libpq environment variables whose corresponding connection parameter can
#: NEVER be pinned by the canonical URL (the contract rejects it outright),
#: and whose inherited value could redirect the effective connection or its
#: security posture. Presence at an in-process connection is fail-closed.
_UNPINNABLE_PG_ENV: frozenset[str] = frozenset(
    {
        "PGHOSTADDR",  # redirects TCP while `host` only names/verifies
        "PGSERVICE",  # a service file can fill any unset slot (incl. hostaddr)
        "PGSERVICEFILE",
        "PGTARGETSESSIONATTRS",  # multi-host connection selection
        "PGREQUIRESSL",  # deprecated transport toggle
        "PGSSLMINPROTOCOLVERSION",
        "PGSSLMAXPROTOCOLVERSION",
        "PGCHANNELBINDING",
    }
)

#: TLS material / trust-anchor environment variables → the allowlisted URL
#: query parameter that pins the same slot. Refused only when the URL does
#: not pin the parameter itself (a pinned slot always wins over the
#: environment, so the variable is then provably inert).
_TLS_TRUST_ENV: Mapping[str, str] = {
    "PGSSLCERT": "sslcert",
    "PGSSLKEY": "sslkey",
    "PGSSLPASSWORD": "sslpassword",
    "PGSSLROOTCERT": "sslrootcert",
    "PGSSLCRL": "sslcrl",
    "PGSSLCRLDIR": "sslcrldir",
}
