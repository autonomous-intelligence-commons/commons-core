"""Backup & recovery pipeline tests — Stage 1 (ADR-0009, synthetic data only).

Test organization (mirrors the assignment's required-coverage sections):

* **Unit** (no PostgreSQL, no age): manifest semantics, destination guard,
  storage publication protocol (atomic marker, conflict, idempotent
  completion, fault injection), age structural validation, secret
  scrubbing, and Git hygiene (no committed keys/artifacts).
* **Integration** (real age + real PostgreSQL): the genuine end-to-end
  synthetic backup → publish → drill restore with the application-level
  four demonstrations, plus the seventeen failure-injection scenarios the
  independent review demanded (dump/encrypt failures, broken pipes,
  truncation, corruption, wrong identities, manifest attacks, partial
  publication, conflicts, insufficient backup-role privileges, unsafe
  destinations, wrong application keys).

Every test runs against disposable databases with runtime-generated
synthetic keys (correction 6): no fixture key, identity, or dump is ever
committed, and temporary artifacts are scoped to pytest tmp directories.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from commons_core.backup.age_format import AgeFormatError, verify_age_file
from commons_core.backup.destination import (
    UnsafeDestinationError,
    describe_url,
    scrub_secrets,
    validate_restore_destination,
)
from commons_core.backup.manifest import (
    BackupManifest,
    ManifestBuildInputs,
    build_manifest,
    new_backup_id,
)
from commons_core.backup.pipeline import (
    BackupError,
    DumpFailed,
    EncryptionFailed,
    PreflightError,
    pg_env_from_url,
    run_backup,
)
from commons_core.backup.restore import DrillFailure, run_restore_drill
from commons_core.backup.storage import (
    BackupConflictError,
    LocalFilesystemStorage,
    MissingBackupObject,
)
from commons_core.backup.tools import Toolbelt
from tests.operational.backup_synth import (
    populate_synthetic_source,
    random_master_key,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Environment plumbing — integration tests need: a disposable PostgreSQL
# server (admin URL), matching client tools, and the age binaries.
# ---------------------------------------------------------------------------
ADMIN_URL = os.environ.get("AIC_BACKUP_TEST_ADMIN_URL", "").strip()
APP_ROLE = os.environ.get("AIC_BACKUP_TEST_APP_ROLE", "commons")
APP_PASSWORD = os.environ.get("AIC_BACKUP_TEST_APP_PASSWORD", "commons-ci")
AGE_BIN = os.environ.get("AIC_BACKUP_AGE_BIN", "").strip() or shutil.which("age") or ""
AGE_KEYGEN_BIN = (
    os.environ.get("AIC_BACKUP_AGE_KEYGEN_BIN", "").strip() or shutil.which("age-keygen") or ""
)
PG_TOOLS_DIR = os.environ.get("AIC_BACKUP_PG_TOOLS_DIR", "").strip()
PG_DUMP_BIN = (
    os.environ.get("AIC_BACKUP_PG_DUMP_BIN", "").strip()
    or (os.path.join(PG_TOOLS_DIR, "pg_dump") if PG_TOOLS_DIR else "")
    or shutil.which("pg_dump")
    or ""
)
PG_RESTORE_BIN = (
    os.environ.get("AIC_BACKUP_PG_RESTORE_BIN", "").strip()
    or (os.path.join(PG_TOOLS_DIR, "pg_restore") if PG_TOOLS_DIR else "")
    or shutil.which("pg_restore")
    or ""
)

HAS_INTEGRATION = bool(ADMIN_URL and AGE_BIN and AGE_KEYGEN_BIN and PG_DUMP_BIN and PG_RESTORE_BIN)

integration = pytest.mark.skipif(
    not HAS_INTEGRATION,
    reason=(
        "backup integration tests need a disposable PostgreSQL server "
        "(AIC_BACKUP_TEST_ADMIN_URL) plus age/age-keygen/pg_dump/pg_restore "
        "binaries (AIC_BACKUP_* envs or PATH)"
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_toolbelt() -> Toolbelt:
    return Toolbelt(age=AGE_BIN, pg_dump=PG_DUMP_BIN, pg_restore=PG_RESTORE_BIN)


def app_url_for(db_name: str, scheme: str | None = None) -> str:
    """App-role URL for a database (carries the synthetic credentials).

    ``scheme`` forces a URL spelling (``postgresql`` or
    ``postgresql+psycopg``); by default the admin URL's own scheme is
    kept — both spellings must behave identically everywhere.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(ADMIN_URL)
    netloc = f"{APP_ROLE}:{APP_PASSWORD}@{parts.hostname}" + (
        f":{parts.port}" if parts.port else ""
    )
    return urlunsplit(
        parts._replace(
            scheme=scheme or parts.scheme,
            netloc=netloc,
            path=f"/{db_name}",
        )
    )


def admin_create_db(name: str) -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(ADMIN_URL.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}" OWNER "{APP_ROLE}"'))
    finally:
        engine.dispose()


def admin_drop_db(name: str) -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(ADMIN_URL.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    finally:
        engine.dispose()


def alembic_upgrade(database_url: str) -> None:
    env = dict(os.environ)
    env["AIC_DATABASE_URL"] = database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        timeout=180,
    )


def make_identity(directory: Path) -> tuple[Path, str]:
    """Runtime-generated SYNTHETIC age identity (correction 6)."""
    identity = directory / f"identity-{secrets.token_hex(4)}.txt"
    subprocess.run(
        [AGE_KEYGEN_BIN, "-o", str(identity)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    os.chmod(identity, 0o600)
    recipient = ""
    for line in identity.read_text(encoding="utf-8").splitlines():
        if "public key:" in line:
            recipient = line.split("public key:", 1)[1].strip()
    assert recipient.startswith("age1"), "age-keygen did not emit a recipient"
    return identity, recipient


def build_synthetic_source(
    prefix: str = "aic_test", scheme: str | None = None
) -> tuple[str, str, dict]:
    """Create a migrated, populated synthetic database; return (url, keys, facts)."""
    db_name = f"{prefix}_{secrets.token_hex(4)}"
    admin_create_db(db_name)
    url = app_url_for(db_name, scheme=scheme)
    master_keys = random_master_key()
    try:
        alembic_upgrade(url)
        facts = populate_synthetic_source(url, master_keys)
    except Exception:
        admin_drop_db(db_name)
        raise
    return url, master_keys, facts


def write_fake_tool(directory: Path, name: str, script: str) -> str:
    path = directory / name
    path.write_text(script, encoding="utf-8")
    os.chmod(path, 0o755)
    return str(path)


FAKE_PG_DUMP_PARTIAL = f"""#!/usr/bin/env python3
import sys
if "--version" in sys.argv:
    print("pg_dump (PostgreSQL) {os.environ.get("AIC_TEST_SERVER_MAJOR", "18")}.6 (fake)")
    sys.exit(0)
# Emit a plausible-looking partial custom-format header, then fail AFTER
# producing output — the review's "nonzero exit after partial output".
sys.stdout.buffer.write(b"PGDMP" + b"\\x00" * 4096)
sys.stdout.buffer.flush()
sys.stderr.write("fake pg_dump: simulated mid-dump failure\\n")
sys.exit(1)
"""

FAKE_AGE_EARLY_EXIT = """#!/usr/bin/env python3
import sys
if "--version" in sys.argv:
    print("v1.3.2 (fake)")
    sys.exit(0)
sys.stderr.write("fake age: refusing to encrypt (simulated crash)\\n")
sys.exit(3)
"""


# ---------------------------------------------------------------------------
# Unit tests — manifest semantics (correction 2)
# ---------------------------------------------------------------------------
def test_backup_id_is_unique_and_well_formed():
    first = new_backup_id()
    second = new_backup_id()
    assert first.startswith("aic-") and second.startswith("aic-")
    assert first != second
    assert len(first.split("-")[-1]) == 8


def test_manifest_omits_row_counts_entirely():
    """Correction 2: no count queries live in the authoritative manifest."""
    manifest = build_manifest(
        ManifestBuildInputs(
            backup_id=new_backup_id(),
            started_at_utc="2026-10-19T00:00:00+00:00",
            created_at_utc="2026-10-19T00:00:05+00:00",
            server_version="PostgreSQL 18.6",
            database_name="aic_test_x",
            alembic_revision="0001",
            tool_versions={"age": "v1.3.2", "pg_dump": "pg_dump (PostgreSQL) 18.6"},
            recipient="age1example",
            artifact_sha256="a" * 64,
            artifact_size=123,
            artifact_filename="x.age",
        )
    )
    blob = json.loads(manifest.to_json())
    assert "table_counts" not in blob
    assert "counts" not in json.dumps(blob["source"])
    # Round-trip preserves every field.
    restored = BackupManifest.from_bytes(manifest.to_bytes())
    assert restored.backup_id == manifest.backup_id
    assert restored.artifact_sha256() == manifest.artifact_sha256()
    # Pre-dump fields carry informational semantics, documented by presence:
    assert blob["alembic_revision"] == "0001"
    assert blob["state"] == "verified-published"


def test_run_backup_refuses_private_identity_as_recipient(tmp_path):
    """The pipeline must never accept an age IDENTITY (private) as recipient."""
    storage = LocalFilesystemStorage(tmp_path / "storage")
    with pytest.raises(PreflightError, match="age1"):
        run_backup(
            database_url="postgresql://commons:pw@127.0.0.1:5432/aic_test_x",
            recipient="AGE-SECRET-KEY-1QSDFSDF",
            storage=storage,
            work_dir=tmp_path / "work",
        )
    with pytest.raises(PreflightError, match="no database URL|age recipient"):
        run_backup(
            database_url="",
            recipient="",
            storage=storage,
            work_dir=tmp_path / "work",
        )


@pytest.mark.parametrize(
    ("scheme", "expected"),
    [
        ("postgresql", "postgresql://db.internal.example.com:5433/prod"),
        (
            "postgresql+psycopg",
            "postgresql+psycopg://db.internal.example.com:5433/prod",
        ),
    ],
    ids=["plain-scheme", "psycopg-scheme"],
)
def test_describe_url_drops_identity_material(scheme, expected):
    """Both accepted scheme spellings: the username, password, and query
    string are dropped entirely; scheme/host/port/database survive.

    Stage 2 review correction — database usernames are barred from
    operational output just like passwords, and the bare scheme prefix
    of the credential-free description is explicitly NOT a leak.
    """
    url = f"{scheme}://svc_backup:hunter2@db.internal.example.com:5433/prod?sslmode=require"
    described = describe_url(url)
    assert described == expected
    assert "hunter2" not in described
    assert "svc_backup" not in described
    assert "sslmode" not in described
    assert "@" not in described


@pytest.mark.parametrize(
    "scheme", ["postgresql", "postgresql+psycopg"], ids=["plain-scheme", "psycopg-scheme"]
)
def test_describe_url_handles_userinfo_free_urls(scheme):
    """URLs without any userinfo describe to themselves (nothing to drop)."""
    assert (
        describe_url(f"{scheme}://127.0.0.1:5433/aic_drill_ab12cd34")
        == f"{scheme}://127.0.0.1:5433/aic_drill_ab12cd34"
    )


@pytest.mark.parametrize(
    "scheme", ["postgresql", "postgresql+psycopg"], ids=["plain-scheme", "psycopg-scheme"]
)
def test_scrub_secrets_redacts_urls_and_hex64(scheme):
    text = (
        f"connect to {scheme}://u:p@h/db failed with key "
        "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
    )
    scrubbed = scrub_secrets(text)
    assert "p@h" not in scrubbed
    assert "***:***@" in scrubbed
    assert "abcdef0123" not in scrubbed
    scrubbed2 = scrub_secrets("leak the password", extra_secrets=("the password",))
    assert "the password" not in scrubbed2 and "***REDACTED***" in scrubbed2


def test_scrub_secrets_redacts_known_username_in_tool_stderr():
    """PostgreSQL echoes the USERNAME inside its own failure text (e.g.
    ``FATAL: password authentication failed for user …``); the failure
    paths hand the run's libpq credentials to the scrubber so neither the
    username nor the password can survive (Stage 2 review correction)."""
    stderr = 'FATAL: password authentication failed for user "svc_backup"'
    scrubbed = scrub_secrets(stderr, extra_secrets=("sup3r-s3cret", "svc_backup"))
    assert "svc_backup" not in scrubbed
    assert "sup3r-s3cret" not in scrubbed
    assert scrubbed == 'FATAL: password authentication failed for user "***REDACTED***"'


def test_pg_env_from_url_keeps_credentials_out_of_argv():
    env = pg_env_from_url("postgresql://commons:pw%40x@127.0.0.1:5433/aic_test_x?sslmode=require")
    assert env == {
        "PGHOST": "127.0.0.1",
        "PGPORT": "5433",
        "PGUSER": "commons",
        "PGPASSWORD": "pw@x",
        "PGDATABASE": "aic_test_x",
        "PGSSLMODE": "require",
    }


def test_pg_env_rejects_non_postgres_scheme():
    with pytest.raises(PreflightError):
        pg_env_from_url("sqlite+pysqlite:///x.db")


# ---------------------------------------------------------------------------
# Unit tests — destination guard (restore safety)
# ---------------------------------------------------------------------------
def test_destination_accepts_loopback_disposable_name():
    target = validate_restore_destination(
        "postgresql://commons:pw@127.0.0.1:5432/aic_drill_ab12cd34"
    )
    assert target.database_name == "aic_drill_ab12cd34"


def test_destination_rejects_railway_host_without_override():
    with pytest.raises(UnsafeDestinationError, match="Railway"):
        validate_restore_destination("postgresql://u:p@monorail.railway.internal:5432/aic_drill_x")


def test_destination_rejects_public_ip():
    with pytest.raises(UnsafeDestinationError, match="not loopback/private"):
        validate_restore_destination("postgresql://u:p@8.8.8.8:5432/aic_drill_x")


def test_destination_public_ip_needs_explicit_override():
    validate_restore_destination(
        "postgresql://u:p@203.0.113.9:5432/aic_drill_x", allow_nonlocal=True
    )


def test_destination_rejects_production_looking_db_name():
    with pytest.raises(UnsafeDestinationError, match="disposable"):
        validate_restore_destination("postgresql://u:p@127.0.0.1:5432/commons")


def test_destination_explicit_allowlist_beats_name_pattern():
    target = validate_restore_destination(
        "postgresql://u:p@127.0.0.1:5432/reviewed-target",
        allowed_db_names=("reviewed-target",),
    )
    assert target.database_name == "reviewed-target"


def test_destination_rejects_wrong_scheme():
    with pytest.raises(UnsafeDestinationError, match="scheme"):
        validate_restore_destination("mysql://u:p@127.0.0.1/aic_drill_x")


# ---------------------------------------------------------------------------
# Unit tests — storage publication protocol (correction 3)
# ---------------------------------------------------------------------------
def _artifact_file(directory: Path, payload: bytes) -> Path:
    path = directory / "local.age"
    path.write_bytes(payload)

    return path


def _publish_into(root: Path, backup_id: str, payload: bytes, storage=None):
    import hashlib

    storage = storage or LocalFilesystemStorage(root)
    artifact = _artifact_file(root, payload)
    manifest = build_manifest(
        ManifestBuildInputs(
            backup_id=backup_id,
            started_at_utc="2026-10-19T00:00:00+00:00",
            created_at_utc="2026-10-19T00:00:01+00:00",
            server_version="PostgreSQL 18.6",
            database_name="aic_test_x",
            alembic_revision=None,
            tool_versions={"age": "v1.3.2", "pg_dump": "pg_dump (PostgreSQL) 18.6"},
            recipient="age1example",
            artifact_sha256=hashlib.sha256(payload).hexdigest(),
            artifact_size=len(payload),
            artifact_filename=f"{backup_id}.age",
        )
    )
    marker = storage.publish(
        backup_id,
        artifact_path=artifact,
        manifest_bytes=manifest.to_bytes(),
        artifact_sha256=hashlib.sha256(payload).hexdigest(),
    )
    return storage, manifest, marker


def test_storage_publish_writes_marker_last(tmp_path):
    backup_id = new_backup_id()
    storage, manifest, marker = _publish_into(tmp_path, backup_id, b"ciphertext-bytes")
    assert marker["state"] == "verified-published"
    assert storage.has_commit_marker(backup_id)
    assert (tmp_path / "data" / f"{backup_id}.age").is_file()
    assert (tmp_path / "manifest" / f"{backup_id}.json").is_file()
    latest = json.loads((tmp_path / "status" / "latest.json").read_text())
    assert latest["backup_id"] == backup_id


def test_storage_uses_restrictive_permissions(tmp_path):
    backup_id = new_backup_id()
    _publish_into(tmp_path, backup_id, b"ciphertext-bytes")
    for sub in ("data", "manifest", "status"):
        mode = stat.S_IMODE((tmp_path / sub).stat().st_mode)
        assert mode == 0o700, f"{sub} dir mode {oct(mode)}"
    for f in (
        tmp_path / "data" / f"{backup_id}.age",
        tmp_path / "manifest" / f"{backup_id}.json",
        tmp_path / "status" / f"{backup_id}.json",
    ):
        mode = stat.S_IMODE(f.stat().st_mode)
        assert mode == 0o600, f"{f.name} file mode {oct(mode)}"


def test_storage_conflicting_republication_is_refused(tmp_path):
    backup_id = new_backup_id()
    _publish_into(tmp_path, backup_id, b"first-content")
    with pytest.raises(BackupConflictError):
        _publish_into(tmp_path, backup_id, b"different-content")


def test_storage_idempotent_republication_of_same_bytes(tmp_path):
    backup_id = new_backup_id()
    _publish_into(tmp_path, backup_id, b"identical")
    _, _, marker2 = _publish_into(tmp_path, backup_id, b"identical")
    assert marker2["backup_id"] == backup_id


def test_storage_idempotent_completion_after_partial_publication(tmp_path):
    """Artifact published, manifest/marker interrupted → same-id retry completes."""
    backup_id = new_backup_id()
    # Simulate the interruption precisely: publish the artifact only, by
    # calling the internal step directly.
    import hashlib

    storage = LocalFilesystemStorage(tmp_path)
    storage._ensure_dirs()
    payload = b"partial-then-completed"
    artifact = _artifact_file(tmp_path, payload)
    sha = hashlib.sha256(payload).hexdigest()
    storage._publish_large(storage._data_path(backup_id), artifact, sha)
    assert storage.commit_marker(backup_id) is None  # partial: no marker

    # Full retry with the SAME id and SAME bytes must complete cleanly.
    storage2, manifest, marker = _publish_into(tmp_path, backup_id, payload)
    assert storage2.has_commit_marker(backup_id)
    assert marker["artifact_sha256"] == sha


def test_storage_missing_manifest_raises(tmp_path):
    backup_id = new_backup_id()
    storage = LocalFilesystemStorage(tmp_path)
    with pytest.raises(MissingBackupObject):
        storage.load_manifest(backup_id)


def test_storage_no_temporary_part_files_survive(tmp_path):
    backup_id = new_backup_id()
    _publish_into(tmp_path, backup_id, b"ciphertext")
    parts = list(tmp_path.rglob("*.part"))
    assert parts == []


class FaultyArtifactStorage(LocalFilesystemStorage):
    """Fault injection: every artifact staging write raises (simulated upload failure)."""

    def _publish_large(self, final, source, expected_sha):
        from commons_core.backup.storage import StorageError

        raise StorageError("simulated storage outage", transient=True)


def test_storage_upload_failure_publishes_nothing(tmp_path):
    backup_id = new_backup_id()
    storage = FaultyArtifactStorage(tmp_path, retry_attempts=2)
    with pytest.raises(Exception, match="artifact publication failed"):
        _publish_into(tmp_path, backup_id, b"ciphertext", storage=storage)
    assert not storage.has_commit_marker(backup_id)
    assert not (tmp_path / "data" / f"{backup_id}.age").exists()
    assert list(tmp_path.rglob("*.part")) == []


# ---------------------------------------------------------------------------
# Unit tests — age structural validation (local verification layer 1)
# ---------------------------------------------------------------------------
def _age_like_file(tmp_path, name: str, payload: bytes = b"\x00" * 64) -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def test_age_format_accepts_well_formed_header(tmp_path):
    header = (
        b"age-encryption.org/v1\n"
        b"-> X25519 AjJhZm9yZA\n"
        b"abcdefghijklmnopqrstuvwxyz123456\n"
        b"--- MACLINEBASE64DATA==\n" + b"\x01" * 64
    )
    path = _age_like_file(tmp_path, "well-formed.age", header)
    verify_age_file(path)  # no exception


def test_age_format_rejects_empty_file(tmp_path):
    with pytest.raises(AgeFormatError, match="empty"):
        verify_age_file(_age_like_file(tmp_path, "empty.age", b""))


def test_age_format_rejects_bad_magic(tmp_path):
    with pytest.raises(AgeFormatError, match="magic"):
        verify_age_file(_age_like_file(tmp_path, "bad.age", b"NOT-AGE\n" + b"\x00" * 64))


def test_age_format_rejects_missing_stanza(tmp_path):
    with pytest.raises(AgeFormatError, match="X25519"):
        verify_age_file(
            _age_like_file(
                tmp_path, "nostanza.age", b"age-encryption.org/v1\n--- MAC==\n" + b"\x00" * 64
            )
        )


def test_age_format_rejects_truncated_after_header(tmp_path):
    with pytest.raises(AgeFormatError, match="payload"):
        verify_age_file(
            _age_like_file(
                tmp_path,
                "trunc.age",
                b"age-encryption.org/v1\n-> X25519 AAAA\nBBBB\n--- MAC==\n",
            )
        )


# ---------------------------------------------------------------------------
# Unit tests — D21-02 boundary regression (Stage 2.2 remediation)
#
# Pre-remediation, verify_age_file read MAX_HEADER_BYTES + 1 bytes and
# rejected the artifact whenever the READ came back longer than the window —
# before parsing a single line. Every real-world backup (a multi-megabyte
# encrypted payload behind a header of a few hundred bytes) therefore failed
# closed at structural verification. The remediation parses the header inside
# the bounded window and only rejects when the header (magic line through
# the ``--- <mac>`` separator line) cannot END inside that window; the total
# artifact size is irrelevant to layer-1 structural validation.
# ---------------------------------------------------------------------------
_D21_WINDOW = 1 << 20  # mirrors age_format.MAX_HEADER_BYTES (1 MiB)


def _sized_age_artifact(tmp_path, name: str, total_size: int) -> Path:
    """Well-formed synthetic age artifact of exactly total_size bytes:
    the standard header plus enough opaque payload to reach the target size."""
    header = (
        b"age-encryption.org/v1\n"
        b"-> X25519 AjJhZm9yZA\n"
        b"abcdefghijklmnopqrstuvwxyz123456\n"
        b"--- MACLINEBASE64DATA==\n"
    )
    payload_len = total_size - len(header)
    assert payload_len > 0, "artifact size must exceed the header length"
    path = tmp_path / name
    path.write_bytes(header + b"\x01" * payload_len)
    assert path.stat().st_size == total_size
    return path


def test_d21_02_01_small_valid_artifact_still_accepted(tmp_path):
    """D21-02-01: existing valid small artifacts remain accepted."""
    verify_age_file(_sized_age_artifact(tmp_path, "small.age", 256))


def test_d21_02_02_artifact_exactly_one_mib_accepted(tmp_path):
    """D21-02-02: a valid artifact of exactly 1,048,576 bytes is accepted."""
    verify_age_file(_sized_age_artifact(tmp_path, "exact1mib.age", _D21_WINDOW))


def test_d21_02_03_artifact_one_byte_over_one_mib_accepted(tmp_path):
    """D21-02-03 (the reported defect boundary): a valid artifact of
    1,048,577 bytes must be accepted post-remediation."""
    verify_age_file(_sized_age_artifact(tmp_path, "over1mib.age", _D21_WINDOW + 1))


def test_d21_02_04_multimegabyte_artifact_accepted(tmp_path):
    """D21-02-04: a valid multi-megabyte encrypted artifact is accepted."""
    verify_age_file(_sized_age_artifact(tmp_path, "multi.age", 4 * _D21_WINDOW))


def test_d21_02_05_large_artifact_with_invalid_magic_rejected(tmp_path):
    """D21-02-05 (magic): artifact size must not weaken the magic check."""
    path = _age_like_file(tmp_path, "bigbadmagic.age", b"NOT-AGE\n" + b"\x00" * (2 * _D21_WINDOW))
    with pytest.raises(AgeFormatError, match="magic"):
        verify_age_file(path)


def test_d21_02_05_large_artifact_with_invalid_stanza_rejected(tmp_path):
    """D21-02-05 (stanza): a >1 MiB artifact with an unparseable recipient
    stanza inside the window is still rejected."""
    path = _age_like_file(
        tmp_path,
        "bigbadstanza.age",
        b"age-encryption.org/v1\n-> NOTAKNOWNSTANZA x\n" + b"\x00" * (2 * _D21_WINDOW),
    )
    with pytest.raises(AgeFormatError):
        verify_age_file(path)


def test_d21_02_05_large_artifact_with_invalid_separator_rejected(tmp_path):
    """D21-02-05 (separator): a >1 MiB artifact whose separator MAC line is
    not base64 is still rejected."""
    path = _age_like_file(
        tmp_path,
        "bigbadmac.age",
        b"age-encryption.org/v1\n-> X25519 AjJhZm9yZA\nabcdef\n"
        b"--- MAC WITH SPACES AND SYMBOLS!\n" + b"\x01" * (2 * _D21_WINDOW),
    )
    with pytest.raises(AgeFormatError, match="base64"):
        verify_age_file(path)


def test_d21_02_06_header_without_separator_inside_window_rejected(tmp_path):
    """D21-02-06: a valid-looking header whose stanza body line alone fills
    the whole header window — no separator anywhere inside it — is rejected
    with a bounded scan: at most the window is read and scanned even though
    the artifact is far larger than the window."""
    import time

    header = b"age-encryption.org/v1\n-> X25519 AjJhZm9yZA\n"
    # One single base64 body line far longer than the window (no '\n'), so
    # the bounded read always lands mid-line with a deterministic,
    # well-formed stanza body prefix and never a half-cut stanza line.
    pad = b"B" * (_D21_WINDOW + 4096)
    path = _age_like_file(tmp_path, "nosep.age", header + pad + b"\x01" * (63 * _D21_WINDOW))
    t0 = time.monotonic()
    with pytest.raises(AgeFormatError, match="separator"):
        verify_age_file(path)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"bounded-window scan took {elapsed:.2f}s on a 64 MiB artifact"


def test_d21_02_07_valid_header_alone_is_not_a_decryptability_claim(tmp_path):
    """D21-02-07: structural verification stays distinct from cryptographic
    integrity verification. A valid header with NO payload bytes is
    rejected; a structurally valid header alone can never be represented as
    proof that the backup is decryptable or restorable (layer 3 owns that)."""
    bare_header = (
        b"age-encryption.org/v1\n"
        b"-> X25519 AjJhZm9yZA\n"
        b"abcdefghijklmnopqrstuvwxyz123456\n"
        b"--- MACLINEBASE64DATA==\n"
    )
    with pytest.raises(AgeFormatError, match="payload"):
        verify_age_file(_age_like_file(tmp_path, "bareheader.age", bare_header))
    # Layer separation is also asserted the other way: a truncated tail after
    # a short payload keeps failing the structural payload check (pre-existing
    # behavior preserved by the remediation).
    with pytest.raises(AgeFormatError, match="payload"):
        verify_age_file(
            _age_like_file(
                tmp_path,
                "trunc2.age",
                b"age-encryption.org/v1\n-> X25519 AAAA\nBBBB\n--- MAC==\n",
            )
        )


# ---------------------------------------------------------------------------
# Unit tests — Git hygiene (correction 6)
# ---------------------------------------------------------------------------
def test_gitignore_covers_backup_artifacts():
    """Generated artifacts must never be committable, fixtures stay allowed."""
    import subprocess as sp

    for name in (
        "x.age",
        "x.age.part",
        "backup.pgdump",
        "backup.dump",
        "backup.backup",
        "age-identity.txt",
    ):
        proc = sp.run(
            ["git", "check-ignore", f"verification/{name}"],
            capture_output=True,
            cwd=REPO_ROOT,
            check=False,
        )
        assert proc.returncode == 0, f"{name} is NOT git-ignored — artifact hygiene gap"


def test_no_private_keys_in_tracked_files():
    """Scan every tracked file for real age identities / PEM private keys.

    The patterns are deliberately STRICT — they match actual key material
    (a bech32 body (data part may contain 0-9), a complete PEM header), not documentation that merely
    describes the format. Documentation legitimately contains strings
    like ``AGE-SECRET-KEY-1…``; a committed key never would.
    """
    import re

    age_identity_re = re.compile(r"AGE-SECRET-KEY-1[A-Z0-9]{10,}")  # bech32 data part (0-9 allowed)
    pem_private_re = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
    proc = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    tracked = [line for line in proc.stdout.splitlines() if line.strip()]
    assert tracked, "no tracked files found — running outside a Git checkout?"
    offenders = []
    for name in tracked:
        path = REPO_ROOT / name
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            blob = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if age_identity_re.search(blob) or pem_private_re.search(blob):
            offenders.append(name)
    assert not offenders, f"private key material tracked in Git: {offenders}"


# ---------------------------------------------------------------------------
# Integration tests — the genuine end-to-end pipeline (real tools, no mocks)
# ---------------------------------------------------------------------------
@integration
class TestEndToEndBackupAndDrill:
    def test_full_backup_publish_drill_roundtrip(self, tmp_path):
        source_url, master_keys, facts = build_synthetic_source()
        storage_root = tmp_path / "storage"
        storage = LocalFilesystemStorage(storage_root)
        identity, recipient = make_identity(tmp_path)
        try:
            outcome = run_backup(
                database_url=source_url,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
            )
            backup_id = outcome.manifest.backup_id
            assert storage.has_commit_marker(backup_id)
            assert outcome.manifest.alembic_revision == facts["alembic_version"]

            report = run_restore_drill(
                server_url=ADMIN_URL,
                storage=storage,
                backup_id=backup_id,
                identity_path=identity,
                toolbelt=make_toolbelt(),
                expected_facts=facts,
                master_keys=master_keys,
            )
            assert report.passed, report.render()
            assert len(report.checks) >= 10
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])

    def test_no_plaintext_dump_files_anywhere(self, tmp_path):
        """Operational safeguard: the plaintext dump never lands on disk."""
        source_url, master_keys, facts = build_synthetic_source()
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        work_dir = tmp_path / "work"
        try:
            run_backup(
                database_url=source_url,
                recipient=recipient,
                storage=storage,
                work_dir=work_dir,
            )
            # Nothing dump-shaped may exist in storage or work dirs.
            for pattern in ("*.pgdump", "*.dump", "*.backup", "*.sql"):
                assert list(tmp_path.rglob(pattern)) == [], f"plaintext artifact {pattern} found"
            # The staged .part file was removed after publication.
            assert list(work_dir.rglob("*.part")) == []
            # Every data file in storage is .age only.
            data_files = list((tmp_path / "storage" / "data").iterdir())
            assert data_files and all(f.suffix == ".age" for f in data_files)
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])

    @staticmethod
    def _assert_operational_output_is_credential_free(combined: str, source_url: str) -> None:
        """The real operational-output invariant, scheme-agnostic.

        What must NEVER appear: the password, the database username, the
        verbatim credentialed URL, or any URL-userinfo shape. What is
        explicitly NOT a leak: the bare ``postgresql://`` or
        ``postgresql+psycopg://`` prefix of the credential-free description
        the pipeline prints (the OLD assertion here — ``"postgresql://"
        not in combined`` — treated the harmless scheme substring itself
        as a leak, which made the test pass or fail depending only on
        which spelling the environment happened to use; Stage 2 review
        correction).
        """
        assert APP_PASSWORD not in combined, "password in operational output"
        assert APP_ROLE not in combined, "database username in operational output"
        assert source_url not in combined, "verbatim credentialed URL echoed"
        assert not re.search(r"://[^/@\s]+@", combined), "URL userinfo in operational output"
        # Sanity: the credential-free description IS what got printed —
        # operational context is preserved, only identity material is gone.
        assert describe_url(source_url) in combined

    def test_cli_output_has_no_credentials(self, tmp_path, capsys):
        """Operational safeguard: stdout/stderr never contain secret material
        (under the environment's own scheme spelling)."""

        source_url, master_keys, facts = build_synthetic_source()
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        try:
            run_backup(
                database_url=source_url,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
            )
            captured = capsys.readouterr()
            self._assert_operational_output_is_credential_free(
                captured.out + captured.err, source_url
            )
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])

    def test_cli_output_has_no_credentials_other_scheme(self, tmp_path, capsys):
        """Same invariant under the OTHER accepted scheme spelling.

        Together with the sibling test this guarantees BOTH
        ``postgresql://`` and ``postgresql+psycopg://`` are exercised in
        every environment, regardless of which spelling the admin URL
        uses (CI uses +psycopg; a plain-scheme local run is the case that
        originally produced the false positive).
        """
        from urllib.parse import urlsplit

        other = "postgresql" if urlsplit(ADMIN_URL).scheme != "postgresql" else "postgresql+psycopg"
        source_url, master_keys, facts = build_synthetic_source(scheme=other)
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        try:
            run_backup(
                database_url=source_url,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
            )
            captured = capsys.readouterr()
            self._assert_operational_output_is_credential_free(
                captured.out + captured.err, source_url
            )
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])


# ---------------------------------------------------------------------------
# Integration tests — the four application-encryption demonstrations
# ---------------------------------------------------------------------------
@integration
class TestApplicationEncryptionDemonstrations:
    def test_restore_alone_does_not_recover_content_without_keys(self, tmp_path):
        """Demonstration 1: DB restore + NO master keys ⇒ content stays encrypted.

        The drill must FAIL loudly (content recovery unproven) rather than
        silently pass without application keys.
        """
        source_url, master_keys, facts = build_synthetic_source()
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        try:
            outcome = run_backup(
                database_url=source_url,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
            )
            report = run_restore_drill(
                server_url=ADMIN_URL,
                storage=storage,
                backup_id=outcome.manifest.backup_id,
                identity_path=identity,
                toolbelt=make_toolbelt(),
                expected_facts=facts,
                master_keys=None,  # deliberately WITHOUT application keys
            )
            assert not report.passed
            assert any("no master keys" in c.detail for c in report.checks if not c.passed), (
                report.render()
            )
            assert any("ciphertext" in c.label for c in report.checks), (
                "the envelope-presence check must still run"
            )
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])

    def test_wrong_application_master_key_fails(self, tmp_path):
        """Demonstration 3: a wrong application master key cannot decrypt."""
        source_url, master_keys, facts = build_synthetic_source()
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        wrong_keys = random_master_key()
        try:
            outcome = run_backup(
                database_url=source_url,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
            )
            report = run_restore_drill(
                server_url=ADMIN_URL,
                storage=storage,
                backup_id=outcome.manifest.backup_id,
                identity_path=identity,
                toolbelt=make_toolbelt(),
                expected_facts=facts,
                master_keys=wrong_keys,  # WRONG application key
            )
            assert not report.passed
            assert any("decrypts byte-for-byte" in c.label and not c.passed for c in report.checks)
            assert any(
                "wrong application master key fails" in c.label and c.passed for c in report.checks
            )
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])


# ---------------------------------------------------------------------------
# Integration tests — failure injection (corrections 1 & 3)
# ---------------------------------------------------------------------------
@integration
class TestPipelineFailureInjection:
    def test_source_connection_failure_is_preflight_refusal(self, tmp_path):
        """Unreachable/missing database → fail-closed preflight error, nothing staged."""
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        bad_url = app_url_for("aic_test_missing_db")
        with pytest.raises(PreflightError, match="cannot read source metadata"):
            run_backup(
                database_url=bad_url,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
            )
        assert list((tmp_path / "work").rglob("*.part")) == []
        assert not storage.list_backup_ids()

    def test_pg_dump_connection_failure_exit_is_not_success(self, tmp_path):
        """A pg_dump that fails on connect (exit != 0, no output) publishes nothing."""
        fake = write_fake_tool(
            tmp_path,
            "fake-pg-dump-connfail",
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if '--version' in sys.argv:\n"
            "    print('pg_dump (PostgreSQL) 18.6 (fake)')\n"
            "    sys.exit(0)\n"
            "sys.stderr.write('fake pg_dump: connection refused\\n')\n"
            "sys.exit(2)\n",
        )
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        source_url, _, _ = build_synthetic_source()
        belt = Toolbelt(age=AGE_BIN, pg_dump=fake, pg_restore=PG_RESTORE_BIN)
        try:
            with pytest.raises(DumpFailed):
                run_backup(
                    database_url=source_url,
                    recipient=recipient,
                    storage=storage,
                    work_dir=tmp_path / "work",
                    toolbelt=belt,
                )
            assert list((tmp_path / "work").rglob("*.part")) == []
            assert not storage.list_backup_ids()
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])

    def test_dump_failure_detail_redacts_libpq_credentials(self, tmp_path):
        """A failing pg_dump whose stderr echoes PGUSER/PGPASSWORD must
        surface only scrubbed text — the USERNAME is exactly as barred
        from operational output as the password (Stage 2 review
        correction, requirement 3)."""
        fake = write_fake_tool(
            tmp_path,
            "fake-pg-dump-echo-creds",
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "if '--version' in sys.argv:\n"
            "    print('pg_dump (PostgreSQL) 18.6 (fake)')\n"
            "    sys.exit(0)\n"
            "user = os.environ.get('PGUSER', '?')\n"
            "pw = os.environ.get('PGPASSWORD', '?')\n"
            "sys.stderr.write(f'connect user={user} pw={pw} failed\\n')\n"
            "sys.exit(2)\n",
        )
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        source_url, _, _ = build_synthetic_source()
        belt = Toolbelt(age=AGE_BIN, pg_dump=fake, pg_restore=PG_RESTORE_BIN)
        try:
            with pytest.raises(DumpFailed) as exc_info:
                run_backup(
                    database_url=source_url,
                    recipient=recipient,
                    storage=storage,
                    work_dir=tmp_path / "work",
                    toolbelt=belt,
                )
            message = str(exc_info.value)
            assert APP_PASSWORD not in message, "password in failure detail"
            assert APP_ROLE not in message, "username in failure detail"
            assert "***REDACTED***" in message
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])

    def test_pg_dump_partial_output_then_nonzero_exit(self, tmp_path):
        """pg_dump producing output and THEN failing must not publish."""
        fake = write_fake_tool(tmp_path, "fake-pg-dump", FAKE_PG_DUMP_PARTIAL)
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        source_url, _, _ = build_synthetic_source()
        belt = Toolbelt(age=AGE_BIN, pg_dump=fake, pg_restore=PG_RESTORE_BIN)
        try:
            with pytest.raises(BackupError) as exc_info:
                run_backup(
                    database_url=source_url,
                    recipient=recipient,
                    storage=storage,
                    work_dir=tmp_path / "work",
                    toolbelt=belt,
                )
            assert isinstance(exc_info.value, DumpFailed)
            # The staged encrypted artifact was removed; nothing published.
            assert list((tmp_path / "work").rglob("*.part")) == []
            assert not (tmp_path / "storage" / "data").exists() or not any(
                (tmp_path / "storage" / "data").iterdir()
            )
            assert not storage.list_backup_ids()
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])

    def test_age_failure_removes_artifact_and_publishes_nothing(self, tmp_path):
        """An invalid recipient makes age itself fail → nothing survives."""
        storage = LocalFilesystemStorage(tmp_path / "storage")
        source_url, _, _ = build_synthetic_source()
        try:
            with pytest.raises(BackupError) as exc_info:
                run_backup(
                    database_url=source_url,
                    recipient="age1not-a-valid-recipient",
                    storage=storage,
                    work_dir=tmp_path / "work",
                )
            assert isinstance(exc_info.value, (DumpFailed, EncryptionFailed))
            assert list((tmp_path / "work").rglob("*.part")) == []
            assert not storage.list_backup_ids()
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])

    def test_age_premature_exit_is_reported_as_broken_pipe(self, tmp_path):
        """age dying early closes the pipe: pg_dump gets SIGPIPE; fail loud."""
        fake_age = write_fake_tool(tmp_path, "fake-age", FAKE_AGE_EARLY_EXIT)
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        source_url, _, _ = build_synthetic_source()
        belt = Toolbelt(age=fake_age, pg_dump=PG_DUMP_BIN, pg_restore=PG_RESTORE_BIN)
        try:
            with pytest.raises(BackupError) as exc_info:
                run_backup(
                    database_url=source_url,
                    recipient=recipient,
                    storage=storage,
                    work_dir=tmp_path / "work",
                    toolbelt=belt,
                )
            assert isinstance(exc_info.value, (DumpFailed, EncryptionFailed))
            message = str(exc_info.value)
            assert "SIGPIPE" in message or "prematurely" in message or "status" in message
            assert list((tmp_path / "work").rglob("*.part")) == []
            assert not storage.list_backup_ids()
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])

    def test_truncated_artifact_is_rejected_by_drill(self, tmp_path):
        source_url, master_keys, facts = build_synthetic_source()
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        try:
            outcome = run_backup(
                database_url=source_url,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
            )
            backup_id = outcome.manifest.backup_id
            artifact = storage.artifact_path(backup_id)
            blob = artifact.read_bytes()
            artifact.write_bytes(blob[: len(blob) // 2])
            with pytest.raises(DrillFailure, match="hash mismatch|sha"):
                run_restore_drill(
                    server_url=ADMIN_URL,
                    storage=storage,
                    backup_id=backup_id,
                    identity_path=identity,
                    toolbelt=make_toolbelt(),
                )
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])

    def test_corrupted_ciphertext_rejected_even_if_hashes_rewritten(self, tmp_path):
        """Layered defense: forging the manifest hash still fails at AEAD."""
        import hashlib

        source_url, master_keys, facts = build_synthetic_source()
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        try:
            outcome = run_backup(
                database_url=source_url,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
            )
            backup_id = outcome.manifest.backup_id
            artifact = storage.artifact_path(backup_id)
            blob = bytearray(artifact.read_bytes())
            blob[len(blob) // 2] ^= 0x01
            artifact.write_bytes(bytes(blob))
            new_sha = hashlib.sha256(bytes(blob)).hexdigest()
            manifest_path = tmp_path / "storage" / "manifest" / f"{backup_id}.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["artifact"]["sha256"] = new_sha
            manifest["artifact"]["size_bytes"] = len(blob)
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
            # …and the commit marker to match the forged manifest (full
            # hash-layer bypass), demonstrating that the AEAD decrypt layer
            # is the final, independent integrity gate.
            marker_path = tmp_path / "storage" / "status" / f"{backup_id}.json"
            marker = json.loads(marker_path.read_text())
            marker["artifact_sha256"] = new_sha
            marker["manifest_sha256"] = hashlib.sha256(
                json.dumps(manifest, sort_keys=True).encode("utf-8")
            ).hexdigest()
            marker_path.write_text(json.dumps(marker, sort_keys=True))
            report = run_restore_drill(
                server_url=ADMIN_URL,
                storage=storage,
                backup_id=backup_id,
                identity_path=identity,
                toolbelt=make_toolbelt(),
            )
            assert not report.passed
            assert any("decrypt" in c.label and not c.passed for c in report.checks), (
                report.render()
            )
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])

    def test_wrong_identity_cannot_decrypt(self, tmp_path):
        source_url, master_keys, facts = build_synthetic_source()
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        other_identity, _ = make_identity(tmp_path)  # a DIFFERENT identity
        try:
            outcome = run_backup(
                database_url=source_url,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
            )
            report = run_restore_drill(
                server_url=ADMIN_URL,
                storage=storage,
                backup_id=outcome.manifest.backup_id,
                identity_path=other_identity,
                toolbelt=make_toolbelt(),
            )
            assert not report.passed
            assert any("age decrypt" in c.label and not c.passed for c in report.checks)
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])

    def test_missing_manifest_refused_by_drill(self, tmp_path):
        source_url, master_keys, facts = build_synthetic_source()
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        try:
            outcome = run_backup(
                database_url=source_url,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
            )
            backup_id = outcome.manifest.backup_id
            (tmp_path / "storage" / "manifest" / f"{backup_id}.json").unlink()
            with pytest.raises(DrillFailure):
                run_restore_drill(
                    server_url=ADMIN_URL,
                    storage=storage,
                    backup_id=backup_id,
                    identity_path=identity,
                    toolbelt=make_toolbelt(),
                )
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])

    def test_corrupted_manifest_refused_by_drill(self, tmp_path):
        source_url, _, _ = build_synthetic_source()
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        try:
            outcome = run_backup(
                database_url=source_url,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
            )
            backup_id = outcome.manifest.backup_id
            path = tmp_path / "storage" / "manifest" / f"{backup_id}.json"
            path.write_text("{not json at all", encoding="utf-8")
            with pytest.raises(DrillFailure):
                run_restore_drill(
                    server_url=ADMIN_URL,
                    storage=storage,
                    backup_id=backup_id,
                    identity_path=identity,
                    toolbelt=make_toolbelt(),
                )
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])

    def test_manifest_artifact_hash_mismatch_refused(self, tmp_path):
        source_url, _, _ = build_synthetic_source()
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        try:
            outcome = run_backup(
                database_url=source_url,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
            )
            backup_id = outcome.manifest.backup_id
            path = tmp_path / "storage" / "manifest" / f"{backup_id}.json"
            manifest = json.loads(path.read_text())
            manifest["artifact"]["sha256"] = "b" * 64
            path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
            with pytest.raises(DrillFailure):
                run_restore_drill(
                    server_url=ADMIN_URL,
                    storage=storage,
                    backup_id=backup_id,
                    identity_path=identity,
                    toolbelt=make_toolbelt(),
                )
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])

    def test_backup_without_commit_marker_refused_by_drill(self, tmp_path):
        """A partially published backup (marker missing) must never drill."""
        source_url, _, _ = build_synthetic_source()
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        try:
            outcome = run_backup(
                database_url=source_url,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
            )
            backup_id = outcome.manifest.backup_id
            (tmp_path / "storage" / "status" / f"{backup_id}.json").unlink()
            with pytest.raises(DrillFailure, match="commit marker"):
                run_restore_drill(
                    server_url=ADMIN_URL,
                    storage=storage,
                    backup_id=backup_id,
                    identity_path=identity,
                    toolbelt=make_toolbelt(),
                )
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])

    def test_conflicting_backup_id_reuse_is_refused(self, tmp_path):
        """Same backup id + different content ⇒ conflict, never overwrite."""
        source_a, _, _ = build_synthetic_source()
        source_b, _, _ = build_synthetic_source()
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        backup_id = new_backup_id()
        try:
            run_backup(
                database_url=source_a,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
                backup_id=backup_id,
            )
            with pytest.raises(BackupError, match="conflict|different content"):
                run_backup(
                    database_url=source_b,
                    recipient=recipient,
                    storage=storage,
                    work_dir=tmp_path / "work",
                    backup_id=backup_id,
                )
        finally:
            admin_drop_db(source_a.rsplit("/", 1)[-1])
            admin_drop_db(source_b.rsplit("/", 1)[-1])

    def test_idempotent_retry_with_same_id_and_content(self, tmp_path):
        """Retry semantics under one backup id, stated precisely.

        pg_dump output is NOT byte-deterministic across runs (timestamps,
        internal ordering), so re-running the FULL pipeline with the same
        id produces different artifact bytes — the storage layer must
        refuse that as an immutable-id conflict (never overwrite the
        already-committed backup). True idempotent completion applies to
        the SAME staged bytes (a partially published backup retried with
        identical content), which the storage-layer unit test
        ``test_storage_idempotent_completion_after_partial_publication``
        proves end-to-end. Here we assert the pipeline-facing half: the
        committed backup survives a conflicting retry untouched.
        """
        source_url, _, _ = build_synthetic_source()
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        backup_id = new_backup_id()
        try:
            first = run_backup(
                database_url=source_url,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
                backup_id=backup_id,
            )
            committed_sha = first.manifest.artifact_sha256()
            with pytest.raises(BackupError, match="different content"):
                run_backup(
                    database_url=source_url,
                    recipient=recipient,
                    storage=storage,
                    work_dir=tmp_path / "work",
                    backup_id=backup_id,
                )
            # The original committed backup is intact and untouched.
            assert storage.has_commit_marker(backup_id)
            import hashlib

            assert (
                hashlib.sha256(storage.artifact_path(backup_id).read_bytes()).hexdigest()
                == committed_sha
            )
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])


# ---------------------------------------------------------------------------
# Integration tests — restore destination safety
# ---------------------------------------------------------------------------
@integration
class TestRestoreDestinationSafety:
    def test_drill_refuses_railway_style_server(self, tmp_path):
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, _ = make_identity(tmp_path)
        with pytest.raises(DrillFailure, match="Railway|railway"):
            run_restore_drill(
                server_url="postgresql+psycopg://commons:pw@containers.railway.internal:5432/postgres",
                storage=storage,
                backup_id="aic-20260101T000000Z-00000000",
                identity_path=identity,
                toolbelt=make_toolbelt(),
            )

    def test_drill_refuses_public_server_without_override(self, tmp_path):
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, _ = make_identity(tmp_path)
        with pytest.raises(DrillFailure, match="loopback|private"):
            run_restore_drill(
                server_url="postgresql+psycopg://commons:pw@8.8.8.8:5432/postgres",
                storage=storage,
                backup_id="aic-20260101T000000Z-00000000",
                identity_path=identity,
                toolbelt=make_toolbelt(),
            )

    def test_drill_refuses_non_disposable_database_name(self, tmp_path):
        source_url, _, _ = build_synthetic_source()
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        try:
            outcome = run_backup(
                database_url=source_url,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
            )
            with pytest.raises(DrillFailure, match="disposable"):
                run_restore_drill(
                    server_url=ADMIN_URL,
                    storage=storage,
                    backup_id=outcome.manifest.backup_id,
                    identity_path=identity,
                    toolbelt=make_toolbelt(),
                    database_name="commons",  # production-looking name
                )
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])

    def test_drill_accepts_explicitly_allowlisted_database_name(self, tmp_path):
        source_url, master_keys, facts = build_synthetic_source()
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        try:
            outcome = run_backup(
                database_url=source_url,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
            )
            report = run_restore_drill(
                server_url=ADMIN_URL,
                storage=storage,
                backup_id=outcome.manifest.backup_id,
                identity_path=identity,
                toolbelt=make_toolbelt(),
                expected_facts=facts,
                master_keys=master_keys,
                database_name="reviewed-drill-target",
                allowed_db_names=("reviewed-drill-target",),
            )
            assert report.passed, report.render()
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])


# ---------------------------------------------------------------------------
# Integration tests — backup role & least privilege (assignment §9)
# ---------------------------------------------------------------------------
@integration
class TestBackupRolePrivileges:
    def _create_least_privilege_role(self, source_db: str) -> tuple[str, str]:
        """The designed minimum backup role: CONNECT + USAGE + SELECT only.

        Mirrors the least-privilege design in docs/BACKUP.md §9: no CREATE,
        no write grants, no superuser, and default privileges so future
        migrations' tables stay SELECTable for the backup role.
        """
        from sqlalchemy import create_engine, text

        role = f"aic_backup_reader_{secrets.token_hex(3)}"
        password = f"pw_{secrets.token_hex(8)}"
        engine = create_engine(
            ADMIN_URL.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT"
        )
        try:
            with engine.connect() as conn:
                conn.execute(text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'"))
                conn.execute(text(f'GRANT CONNECT ON DATABASE "{source_db}" TO "{role}"'))
        finally:
            engine.dispose()
        engine = create_engine(app_url_for(source_db), isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as conn:
                conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
                conn.execute(text(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{role}"'))
                conn.execute(text(f'GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO "{role}"'))
                conn.execute(
                    text(
                        f'ALTER DEFAULT PRIVILEGES FOR ROLE "{APP_ROLE}" IN SCHEMA public '
                        f'GRANT SELECT ON TABLES TO "{role}"'
                    )
                )
        finally:
            engine.dispose()
        return role, password

    def test_least_privilege_backup_role_succeeds(self, tmp_path):
        """The minimum-privilege role (SELECT-only) can produce a full backup."""
        source_url, master_keys, facts = build_synthetic_source()
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        db = source_url.rsplit("/", 1)[-1]
        role, password = self._create_least_privilege_role(db)
        role_url = app_url_for(db).replace(f"{APP_ROLE}:{APP_PASSWORD}", f"{role}:{password}")
        try:
            outcome = run_backup(
                database_url=role_url,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
            )
            report = run_restore_drill(
                server_url=ADMIN_URL,
                storage=storage,
                backup_id=outcome.manifest.backup_id,
                identity_path=identity,
                toolbelt=make_toolbelt(),
                expected_facts=facts,
                master_keys=master_keys,
            )
            assert report.passed, report.render()
        finally:
            admin_drop_db(db)
            from sqlalchemy import create_engine, text

            engine = create_engine(
                ADMIN_URL.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT"
            )
            try:
                with engine.connect() as conn:
                    conn.execute(text(f'DROP OWNED BY "{role}"'))
                    conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
            finally:
                engine.dispose()

    def test_insufficient_role_privileges_fail_closed(self, tmp_path):
        """A role missing table SELECT grants cannot dump → nothing published."""
        from sqlalchemy import create_engine, text

        source_url, _, _ = build_synthetic_source()
        db = source_url.rsplit("/", 1)[-1]
        role = f"aic_noaccess_{secrets.token_hex(3)}"
        password = f"pw_{secrets.token_hex(8)}"
        storage = LocalFilesystemStorage(tmp_path / "storage")
        identity, recipient = make_identity(tmp_path)
        engine = create_engine(
            ADMIN_URL.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT"
        )
        try:
            with engine.connect() as conn:
                conn.execute(text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'"))
                conn.execute(text(f'GRANT CONNECT ON DATABASE "{db}" TO "{role}"'))
            # Schema access is granted INSIDE the target database (the
            # realistic least-privilege shape) — deliberately NO table grants.
            db_engine = create_engine(
                ADMIN_URL.rsplit("/", 1)[0] + f"/{db}", isolation_level="AUTOCOMMIT"
            )
            try:
                with db_engine.connect() as conn:
                    conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
            finally:
                db_engine.dispose()
            role_url = app_url_for(db).replace(f"{APP_ROLE}:{APP_PASSWORD}", f"{role}:{password}")
            with pytest.raises(BackupError):
                run_backup(
                    database_url=role_url,
                    recipient=recipient,
                    storage=storage,
                    work_dir=tmp_path / "work",
                )
            assert not storage.list_backup_ids()
            assert list((tmp_path / "work").rglob("*.part")) == []
        finally:
            admin_drop_db(db)
            engine2 = create_engine(
                ADMIN_URL.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT"
            )
            try:
                with engine2.connect() as conn:
                    conn.execute(text(f'DROP OWNED BY "{role}"'))
                    conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
            finally:
                engine2.dispose()


# ---------------------------------------------------------------------------
# Integration — D21-02-08: the real >1 MiB pipeline (Stage 2.2 remediation)
#
# Pre-remediation, a synthetic full pipeline whose compressed custom-format
# backup encrypted to more than 1 MiB failed with ArtifactVerificationFailed
# at the structural verification stage (fail-closed, nothing published).
# Post-remediation the same pipeline must complete, publish consistently, and
# stay fully recoverable. The dataset deliberately uses high-entropy content:
# every entry is an AES-256-GCM ciphertext envelope produced by the real
# EnvelopeCipher, so the custom-format dump cannot compress below the
# 1 MiB window (no low-entropy filler).
# ---------------------------------------------------------------------------
def _inject_incompressible_entries(database_url: str, master_keys: str) -> int:
    """Append ~4 MiB of high-entropy (encrypted-envelope) memory entries.

    Returns the number of entries injected so the caller can fold them into
    the expected facts (the restore drill verifies they all round-trip).
    """
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from commons_core.database.models import Agent, MemoryNamespace
    from commons_core.memory.service import write_entry as memory_write_entry
    from tests.operational.backup_synth import MAX_BYTES, build_cipher

    cipher = build_cipher(master_keys, "k1")
    engine = create_engine(database_url)
    injected = 0
    try:
        with Session(engine) as session:
            admin = session.execute(
                select(Agent).where(Agent.handle == "backup-synth-admin")
            ).scalar_one()
            ns = session.execute(
                select(MemoryNamespace).where(MemoryNamespace.handle == "backup-synth-namespace")
            ).scalar_one()
            for i in range(64):
                memory_write_entry(
                    session,
                    admin,
                    ns,
                    key=f"d2102-bulk-{i:03d}",
                    payload=secrets.token_hex(MAX_BYTES // 2),
                    content_type="text/plain",
                    cipher=cipher,
                    max_bytes=MAX_BYTES,
                )
                injected += 1
            session.commit()
    finally:
        engine.dispose()
    return injected


class TestD2102FullPipelineMultiMegabyte:
    @integration
    def test_backup_of_multimegabyte_dataset_roundtrips(self, tmp_path):
        source_url, master_keys, facts = build_synthetic_source(prefix="aic_d2102")
        storage_root = tmp_path / "storage"
        storage = LocalFilesystemStorage(storage_root)
        identity, recipient = make_identity(tmp_path)
        try:
            injected = _inject_incompressible_entries(source_url, master_keys)
            # The injected bulk entries are part of the expected facts: the
            # drill must find every one of them restored (68 rows, not 4).
            facts["table_counts"]["memory_entries"] = (
                facts["table_counts"].get("memory_entries", 0) + injected
            )
            outcome = run_backup(
                database_url=source_url,
                recipient=recipient,
                storage=storage,
                work_dir=tmp_path / "work",
            )
            backup_id = outcome.manifest.backup_id

            # Publication state is consistent (no fail-closed rejection).
            assert storage.has_commit_marker(backup_id)

            # The stored artifact exceeds the 1 MiB header window.
            stored = storage_root / "data" / f"{backup_id}.age"
            assert stored.is_file()
            assert stored.stat().st_size > (1 << 20), (
                f"artifact is only {stored.stat().st_size} bytes; "
                "the dataset did not exercise the >1 MiB boundary"
            )

            # Stored artifact hash matches the manifest.
            from commons_core.backup.manifest import sha256_file

            assert sha256_file(stored) == outcome.artifact_sha256
            assert sha256_file(stored) == outcome.manifest.artifact["sha256"]

            # Storage protections: no stray temporaries, .age-only data dir.
            assert list((tmp_path / "work").rglob("*.part")) == []
            data_files = list((storage_root / "data").iterdir())
            assert data_files and all(f.suffix == ".age" for f in data_files)

            # Full cryptographic + restore verification with the synthetic
            # recovery identity (layer 3) — decryptability IS proven here,
            # never by the structural layer alone.
            report = run_restore_drill(
                server_url=ADMIN_URL,
                storage=storage,
                backup_id=backup_id,
                identity_path=identity,
                toolbelt=make_toolbelt(),
                expected_facts=facts,
                master_keys=master_keys,
            )
            assert report.passed, report.render()
            assert len(report.checks) >= 10
        finally:
            admin_drop_db(source_url.rsplit("/", 1)[-1])
