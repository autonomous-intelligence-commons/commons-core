"""The authoritative backup manifest — non-sensitive metadata only.

Stage 1 review correction 2 (manifest consistency):

The original research PoC stored per-table row counts in the manifest as if
they proved snapshot consistency. They did not: any count query executed
before or after ``pg_dump`` reads a *different* MVCC snapshot than the dump
itself, so the numbers can legally disagree with the restored database
under concurrent writes. Independent count queries are therefore NOT
snapshot-consistent proof and must not be presented as such.

This implementation resolves the correction by *omitting* row counts from
the authoritative manifest entirely (the review's sanctioned alternative to
implementing PostgreSQL snapshot-sharing) and validating restored data
against **controlled synthetic expectations** produced by the test fixture
that populated the source database (see
``tests/operational/backup_synth.py``). Expected-fact validation is a
separate concern from backup metadata and never travels with the backup.

Every field below documents its exact consistency semantics. Fields
recorded *before* the dump (server version, alembic revision) are honest
*informational* context — the authoritative statement of what the artifact
is lives in the post-encryption ``artifact`` block, which is fully
deterministic given the artifact bytes.

Privacy: manifests must never carry Agent memory, message content, or any
secret (no URLs, no passwords, no key material beyond the *public* age
recipient, which is safe to store by design).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import secrets
from dataclasses import asdict, dataclass

MANIFEST_SCHEMA_VERSION = 1
DUMP_FORMAT = "pg_dump custom (-Fc)"


def utc_now() -> str:
    """ISO-8601 UTC timestamp with an explicit offset."""
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def new_backup_id() -> str:
    """A unique, immutable, lexicographically sortable backup identifier.

      Format ``aic-<UTCstamp>-<8hex>``: sorting by id approximates creation
      order and the random suffix makes collisions between concurrent runs
      (or retries within the same second) impossible in practice. Ids are
    never reused with different content — the storage layer enforces this
      (see :mod:`commons_core.backup.storage`).
    """
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"aic-{stamp}-{secrets.token_hex(4)}"


def sha256_file(path) -> str:
    """Streaming SHA-256 of *path* (never loads the file into memory)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class BackupManifest:
    """One backup's metadata — see the module docstring for field semantics.

    Consistency semantics, field by field:

    * ``schema_version`` — manifest format version (int). Lets future
      readers migrate old manifests; not a statement about the database.
    * ``backup_id`` — unique run identifier (see :func:`new_backup_id`).
    * ``started_at_utc`` / ``created_at_utc`` — pipeline start and the
      moment the encrypted artifact finished verification. Wall-clock
      context only; NOT the scheduled time and NOT a snapshot marker.
    * ``source.server_version`` — ``SELECT version()`` executed *before*
      pg_dump started: informational, may differ from the exact server
      binary if the server upgrades mid-run (astronomically unlikely at
      Phase 1 scale, but the claim is scoped honestly).
    * ``source.database_name`` — ``current_database()`` from the same
      pre-dump query. Non-sensitive (synthetic/throwaway or, later, the
      production database's plain name).
    * ``source.format`` — constant describing the logical dump format.
    * ``alembic_revision`` — schema migration head observed *before* the
      dump. If migrations run concurrently the dumped schema may be newer;
      restore drills compare the *restored* revision against this field
      only as a sanity signal, never as proof.
    * ``tools`` — exact ``--version`` output of the binaries actually run.
    * ``artifact`` — THE authoritative block, computed after encryption
      finished: ``sha256``/``size_bytes`` are deterministic properties of
      the published bytes. ``encryption`` names the envelope scheme.
      ``recipient`` is the age *public* recipient string (public by
      design; stored so key-rotation audits can tell which key generation
      can decrypt which backup).
    * ``retention_class`` — bookkeeping tag for the (future) lifecycle
      stage; Stage 1 always writes ``daily`` and never deletes anything.
    * ``state`` — ``"verified-published"`` only after the storage adapter
      confirmed both objects and wrote its commit marker (correction 3).
    """

    schema_version: int
    backup_id: str
    started_at_utc: str
    created_at_utc: str
    source: dict
    tools: dict
    alembic_revision: str | None
    artifact: dict
    retention_class: str = "daily"
    state: str = "verified-published"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    def to_bytes(self) -> bytes:
        return self.to_json().encode("utf-8")

    @staticmethod
    def from_bytes(blob: bytes) -> BackupManifest:
        parsed = json.loads(blob.decode("utf-8"))
        return BackupManifest(
            schema_version=parsed["schema_version"],
            backup_id=parsed["backup_id"],
            started_at_utc=parsed["started_at_utc"],
            created_at_utc=parsed["created_at_utc"],
            source=parsed["source"],
            tools=parsed["tools"],
            alembic_revision=parsed.get("alembic_revision"),
            artifact=parsed["artifact"],
            retention_class=parsed.get("retention_class", "daily"),
            state=parsed.get("state", "unknown"),
        )

    def artifact_sha256(self) -> str:
        return self.artifact["sha256"]


@dataclass(frozen=True)
class ManifestBuildInputs:
    """Everything the pipeline measures/collects before building a manifest."""

    backup_id: str
    started_at_utc: str
    created_at_utc: str
    server_version: str
    database_name: str
    alembic_revision: str | None
    tool_versions: dict
    recipient: str
    artifact_sha256: str
    artifact_size: int
    artifact_filename: str


def build_manifest(inputs: ManifestBuildInputs) -> BackupManifest:
    """Assemble the authoritative manifest from measured facts."""
    return BackupManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        backup_id=inputs.backup_id,
        started_at_utc=inputs.started_at_utc,
        created_at_utc=inputs.created_at_utc,
        source={
            "database_name": inputs.database_name,
            "server_version": inputs.server_version,
            "format": DUMP_FORMAT,
        },
        tools=dict(inputs.tool_versions),
        alembic_revision=inputs.alembic_revision,
        artifact={
            "filename": inputs.artifact_filename,
            "sha256": inputs.artifact_sha256,
            "size_bytes": inputs.artifact_size,
            "encryption": "age-v1 X25519+ChaCha20-Poly1305 (AEAD)",
            "recipient": inputs.recipient,
        },
        retention_class="daily",
        state="verified-published",
    )
