"""P0 backup & recovery security hardening tests — R10 / R11 / R12.

Independent remediation of the three P0 findings from the 2026-10-19
comprehensive risk review (evidence probes T09/T10/T11/T12):

* **R10 — restore destination validation vs the effective libpq
  connection.** The guard and the connection must be the SAME object:
  query-string destination overrides (``?host=``, ``?hostaddr=``,
  ``?service=``…) are rejected everywhere, inherited ``PG*`` environment
  variables never reach a subprocess or a SQLAlchemy connection, and
  hostless (Unix-socket) URLs are refused.
* **R11 — truthful backup publication.** A commit marker alone never means
  success: the idempotent fast path re-verifies the whole committed set
  (artifact + manifest hashes against the marker), missing or corrupted
  objects raise :class:`PublishedSetCorrupted`, staging names are unique
  with exclusive-create promotion, and a failed ``latest`` pointer update
  is a warning — never a demotion of a verified backup.
* **R12 — explicit, bounded, verifiable failure cleanup.** Publication
  failures remove the staged ``.part`` artifact with an OBSERVED outcome,
  cleanup failures are reported (never swallowed — T11), the drill report
  carries a dedicated cleanup verdict, subprocess waits are deadline-
  bounded with process-group termination, and stderr capture is bounded.

Everything here is synthetic and local: temporary directories, fake
tools, runtime-generated objects. No production systems, no real keys.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from commons_core.backup import pipeline, restore, tools
from commons_core.backup.connection import (
    CanonicalConnection,
    ConnectionContractError,
    scrubbed_pg_environ,
)
from commons_core.backup.destination import (
    UnsafeDestinationError,
    parse_target,
    validate_restore_destination,
)
from commons_core.backup.manifest import (
    ManifestBuildInputs,
    build_manifest,
    new_backup_id,
)
from commons_core.backup.pipeline import (
    BackupError,
    DeadlineExceeded,
    PreflightError,
    PublicationFailed,
    pg_env_from_url,
    run_backup,
)
from commons_core.backup.restore import (
    DrillCleanupError,
    DrillFailure,
    DrillReport,
    run_restore_drill,
)
from commons_core.backup.storage import (
    BackupConflictError,
    LocalFilesystemStorage,
    PublishedSetCorrupted,
    StorageError,
    sha256_bytes,
)
from commons_core.backup.tools import STDERR_MAX_LINES, terminate_process_tree

# ---------------------------------------------------------------------------
# Shared helpers (synthetic only)
# ---------------------------------------------------------------------------
GOOD_URL = "postgresql://commons:pw@127.0.0.1:5432/aic_drill_probe01"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _artifact_file(root: Path, payload: bytes, name: str = "artifact.age") -> Path:
    path = root / name
    path.write_bytes(payload)
    return path


def _publish_into(root: Path, backup_id: str, payload: bytes, storage=None):
    """Publish a well-formed synthetic backup (mirrors the Stage 1 helper)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    storage = storage or LocalFilesystemStorage(root)
    artifact = _artifact_file(root, payload, name=f"{backup_id}.age")
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
            artifact_sha256=_sha(payload),
            artifact_size=len(payload),
            artifact_filename=f"{backup_id}.age",
        )
    )
    marker = storage.publish(
        backup_id,
        artifact_path=artifact,
        manifest_bytes=manifest.to_bytes(),
        artifact_sha256=_sha(payload),
    )
    return storage, manifest, marker


def _write_fake_tool(directory: Path, name: str, script: str) -> str:
    path = directory / name
    path.write_text(script, encoding="utf-8")
    os.chmod(path, 0o755)
    return str(path)


def _fake_belt(
    directory: Path, *, pg_dump_script: str | None = None, age_script: str | None = None
):
    """A Toolbelt made entirely of fake tools (no real binaries needed)."""
    pg_dump = _write_fake_tool(
        directory,
        "fake-pg-dump",
        pg_dump_script
        or (
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if '--version' in sys.argv:\n"
            "    print('pg_dump (PostgreSQL) 18.6 (fake)')\n"
            "    sys.exit(0)\n"
            "sys.stdout.buffer.write(b'PGDMP' + b'\\x00' * 4096)\n"
        ),
    )
    age = _write_fake_tool(
        directory,
        "fake-age",
        age_script
        or (
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if '--version' in sys.argv:\n"
            "    print('v1.3.2 (fake)')\n"
            "    sys.exit(0)\n"
            "out = sys.argv[sys.argv.index('-o') + 1]\n"
            "data = sys.stdin.buffer.read()  # consume the whole dump\n"
            "with open(out, 'wb') as fh:\n"
            "    fh.write(b'age-encryption-fake-header: synthetic\\n' + data[:16])\n"
        ),
    )
    return tools.Toolbelt(age=age, pg_dump=pg_dump, pg_restore=pg_dump)


# ---------------------------------------------------------------------------
# R10 — canonical connection contract: rejected (adversarial) configurations
# ---------------------------------------------------------------------------
class TestR10RejectedConfigurations:
    """R10 negative tests 1–9: every redirect/override path is fail-closed."""

    def test_query_host_override_is_rejected_by_guard_and_libpq_env(self):
        """T12 regression: the authority says 127.0.0.1, the query says evil."""
        url = GOOD_URL + "?host=other.example.invalid"
        with pytest.raises(UnsafeDestinationError):
            validate_restore_destination(url)
        with pytest.raises(PreflightError):
            pg_env_from_url(url)

    def test_query_hostaddr_override_is_rejected(self):
        url = GOOD_URL + "?hostaddr=10.9.8.7"
        with pytest.raises(UnsafeDestinationError):
            validate_restore_destination(url)
        with pytest.raises(PreflightError):
            pg_env_from_url(url)

    def test_query_port_override_is_rejected(self):
        url = GOOD_URL + "?port=6543"
        with pytest.raises(UnsafeDestinationError):
            validate_restore_destination(url)
        with pytest.raises(PreflightError):
            pg_env_from_url(url)

    def test_service_file_configuration_is_rejected(self):
        for query in ("?service=evil", "?servicefile=/tmp/evil.conf"):
            with pytest.raises(UnsafeDestinationError):
                validate_restore_destination(GOOD_URL + query)
            with pytest.raises(PreflightError):
                pg_env_from_url(GOOD_URL + query)

    def test_user_dbname_options_passfile_overrides_are_rejected(self):
        for query in ("?user=other", "?dbname=commons", "?options=-c%20x=1", "?passfile=/tmp/x"):
            with pytest.raises(UnsafeDestinationError):
                validate_restore_destination(GOOD_URL + query)
            with pytest.raises(PreflightError):
                pg_env_from_url(GOOD_URL + query)

    def test_duplicate_query_parameter_is_rejected(self):
        url = GOOD_URL + "?sslmode=require&sslmode=disable"
        with pytest.raises(UnsafeDestinationError):
            validate_restore_destination(url)
        with pytest.raises(PreflightError):
            pg_env_from_url(url)

    def test_unknown_query_parameter_is_rejected(self):
        url = GOOD_URL + "?future_libpq_param=1"
        with pytest.raises(UnsafeDestinationError):
            validate_restore_destination(url)
        with pytest.raises(PreflightError):
            pg_env_from_url(url)

    def test_unix_socket_url_without_explicit_host_is_rejected(self):
        """Hostless URL would fall back to the socket directory (R10 case 7)."""
        for url in (
            "postgresql://commons@/aic_drill_probe01",
            "postgresql:///aic_drill_probe01",
        ):
            with pytest.raises(UnsafeDestinationError, match="host"):
                validate_restore_destination(url)
            with pytest.raises(ConnectionContractError, match="host"):
                CanonicalConnection.from_url(url)

    def test_malformed_urls_are_rejected(self):
        for url in (
            GOOD_URL.replace("5432", "not-a-port"),  # invalid port
            "postgresql://commons:pw@127.0.0.1:70000/aic_drill_probe01",  # port out of range
            "postgresql://commons:pw@127.0.0.1:5432/",  # no database name
            "sqlite+pysqlite:///x.db",  # wrong scheme
            "",  # empty
            "postgresql://commons:pw@127.0.0.1:5432/aic\x00drill",  # NUL in path
        ):
            with pytest.raises((UnsafeDestinationError, ConnectionContractError)):
                validate_restore_destination(url)

    def test_backup_pipeline_rejects_redirecting_url_before_staging(self, tmp_path):
        """run_backup fails closed BEFORE anything is staged or touched."""
        url = "postgresql://commons:pw@127.0.0.1:5432/aic_test_x?host=evil.example.invalid"
        with pytest.raises(ConnectionContractError):
            run_backup(
                database_url=url,
                recipient="age1example",
                storage=LocalFilesystemStorage(tmp_path / "storage"),
                work_dir=tmp_path / "work",
            )
        assert not (tmp_path / "work").exists()


# ---------------------------------------------------------------------------
# R10 — accepted configurations + environment control + resolution equality
# ---------------------------------------------------------------------------
class TestR10AcceptedConfigurations:
    def test_safe_sslmode_is_accepted_by_guard_and_env(self):
        url = GOOD_URL + "?sslmode=require"
        target = validate_restore_destination(url)
        assert (target.host, target.port, target.database_name) == (
            "127.0.0.1",
            5432,
            "aic_drill_probe01",
        )
        env = pg_env_from_url(url)
        assert env["PGHOST"] == "127.0.0.1"
        assert env["PGSSLMODE"] == "require"

    def test_percent_encoded_credentials_round_trip(self):
        url = "postgresql://com%6Dons:pw%40x@127.0.0.1:5433/aic_test_x?sslmode=require"
        env = pg_env_from_url(url)
        assert env["PGUSER"] == "commons"
        assert env["PGPASSWORD"] == "pw@x"  # unquoted exactly once
        canonical = CanonicalConnection.from_url(url)
        reparsed = CanonicalConnection.from_url(canonical.sqlalchemy_url())
        assert (reparsed.host, reparsed.port, reparsed.database_name) == (
            canonical.host,
            canonical.port,
            canonical.database_name,
        )
        assert reparsed.password == canonical.password == "pw@x"

    def test_guard_parse_equals_canonical_parse_equals_libpq_env(self):
        """R10 negative test 10: Python and subprocess resolution agree."""
        for url in (
            GOOD_URL,
            GOOD_URL + "?sslmode=require",
            "postgresql+psycopg://commons:pw%40x@localhost:5433/commons_test_abc?connect_timeout=5",
        ):
            canonical = CanonicalConnection.from_url(url)
            target = parse_target(url)
            env = pg_env_from_url(url)
            assert target.host == canonical.host == env["PGHOST"]
            assert str(target.port) == str(canonical.port) == env["PGPORT"]
            assert target.database_name == canonical.database_name == env["PGDATABASE"]


class TestR10EnvironmentControl:
    def test_subprocess_env_strips_inherited_pg_variables(self, monkeypatch):
        """R10 cases 5–6: conflicting inherited PGHOST/PGPORT never leak."""
        monkeypatch.setenv("PGHOST", "evil.example.invalid")
        monkeypatch.setenv("PGPORT", "6543")
        monkeypatch.setenv("PGSERVICE", "evil-service")
        monkeypatch.setenv("PGSERVICEFILE", "/tmp/evil-services.conf")
        monkeypatch.setenv("PGPASSFILE", "/tmp/evil-passfile")
        env = CanonicalConnection.from_url(GOOD_URL).subprocess_env(app_name="commons-core-backup")
        # Canonical destination parameters REPLACE the inherited ones.
        assert env["PGHOST"] == "127.0.0.1"
        assert env["PGPORT"] == "5432"
        assert env["PGDATABASE"] == "aic_drill_probe01"
        assert env["PGAPPNAME"] == "commons-core-backup"
        # Everything else PG* that libpq honors is stripped outright.
        for key in ("PGSERVICE", "PGSERVICEFILE", "PGPASSFILE"):
            assert key not in env

    def test_scrubbed_pg_environ_removes_and_restores(self, monkeypatch):
        monkeypatch.setenv("PGHOST", "evil.example.invalid")
        monkeypatch.setenv("PGAPPNAME", "keepme-out")
        with scrubbed_pg_environ():
            assert "PGHOST" not in os.environ
            assert "PGAPPNAME" not in os.environ
        assert os.environ["PGHOST"] == "evil.example.invalid"  # restored

    def test_drill_rejects_redirecting_server_url_before_any_connection(self, tmp_path):
        identity = tmp_path / "identity.txt"
        identity.write_text("AGE-SECRET-KEY-1SYNTHETIC\n")
        url = "postgresql://commons:pw@127.0.0.1:5432/postgres?host=evil.example.invalid"
        with pytest.raises(DrillFailure, match="connection contract"):
            run_restore_drill(
                server_url=url,
                storage=LocalFilesystemStorage(tmp_path / "storage"),
                backup_id="probe",
                identity_path=identity,
            )

    def test_drill_rejects_hostless_server_url(self, tmp_path):
        identity = tmp_path / "identity.txt"
        identity.write_text("AGE-SECRET-KEY-1SYNTHETIC\n")
        with pytest.raises(DrillFailure, match="host"):
            run_restore_drill(
                server_url="postgresql://commons:pw@/postgres",
                storage=LocalFilesystemStorage(tmp_path / "storage"),
                backup_id="probe",
                identity_path=identity,
            )


# ---------------------------------------------------------------------------
# R11 — truthful publication: the committed set, not the marker, is success
# ---------------------------------------------------------------------------
class TestR11CommittedSetRevalidation:
    def test_valid_repeated_publication_of_intact_set_is_idempotent(self, tmp_path):
        backup_id = new_backup_id()
        _publish_into(tmp_path, backup_id, b"identical")
        _, _, marker2 = _publish_into(tmp_path, backup_id, b"identical")
        assert marker2["state"] == "verified-published"

    def test_t09_regression_marker_present_artifact_missing_is_explicit_failure(self, tmp_path):
        """The exact evidence scenario: artifact deleted, marker replayed."""
        backup_id = new_backup_id()
        storage, manifest, marker = _publish_into(tmp_path, backup_id, b"ciphertext")
        (tmp_path / "data" / f"{backup_id}.age").unlink()
        with pytest.raises(PublishedSetCorrupted) as exc_info:
            _publish_into(tmp_path, backup_id, b"ciphertext")
        assert "MISSING" in str(exc_info.value) or "INCOMPLETE" in str(exc_info.value)
        assert not (tmp_path / "data" / f"{backup_id}.age").exists()  # nothing fabricated

    def test_marker_present_manifest_missing_is_explicit_failure(self, tmp_path):
        backup_id = new_backup_id()
        _publish_into(tmp_path, backup_id, b"ciphertext")
        (tmp_path / "manifest" / f"{backup_id}.json").unlink()
        with pytest.raises(PublishedSetCorrupted):
            _publish_into(tmp_path, backup_id, b"ciphertext")

    def test_marker_present_artifact_hash_mismatch_is_explicit_failure(self, tmp_path):
        """Truncated / tampered artifact after commit (R11 truncation case)."""
        backup_id = new_backup_id()
        _publish_into(tmp_path, backup_id, b"ciphertext-long-enough")
        artifact = tmp_path / "data" / f"{backup_id}.age"
        blob = artifact.read_bytes()
        artifact.write_bytes(blob[: len(blob) // 2])  # truncation
        with pytest.raises(PublishedSetCorrupted, match="CORRUPT"):
            _publish_into(tmp_path, backup_id, b"ciphertext-long-enough")

    def test_marker_present_manifest_edited_after_commit_is_explicit_failure(self, tmp_path):
        backup_id = new_backup_id()
        storage, manifest, marker = _publish_into(tmp_path, backup_id, b"ciphertext")
        manifest_path = tmp_path / "manifest" / f"{backup_id}.json"
        edited = json.loads(manifest_path.read_text())
        edited["artifact_size"] = 424242  # hand edit, still valid JSON
        manifest_path.write_text(json.dumps(edited))
        with pytest.raises(PublishedSetCorrupted):
            _publish_into(tmp_path, backup_id, b"ciphertext")

    def test_marker_with_wrong_state_is_rejected(self, tmp_path):
        backup_id = new_backup_id()
        _publish_into(tmp_path, backup_id, b"ciphertext")
        marker_path = tmp_path / "status" / f"{backup_id}.json"
        marker = json.loads(marker_path.read_text())
        marker["state"] = "hand-edited"
        marker_path.write_text(json.dumps(marker))
        with pytest.raises(PublishedSetCorrupted):
            _publish_into(tmp_path, backup_id, b"ciphertext")

    def test_marker_naming_another_backup_id_is_rejected(self, tmp_path):
        backup_id = new_backup_id()
        _publish_into(tmp_path, backup_id, b"ciphertext")
        marker_path = tmp_path / "status" / f"{backup_id}.json"
        marker = json.loads(marker_path.read_text())
        marker["backup_id"] = "someone-else"
        marker_path.write_text(json.dumps(marker))
        with pytest.raises(PublishedSetCorrupted):
            _publish_into(tmp_path, backup_id, b"ciphertext")

    def test_unreadable_marker_json_is_fail_closed(self, tmp_path):
        backup_id = new_backup_id()
        _publish_into(tmp_path, backup_id, b"ciphertext")
        (tmp_path / "status" / f"{backup_id}.json").write_text("{not json")
        with pytest.raises(StorageError, match="unreadable"):
            _publish_into(tmp_path, backup_id, b"ciphertext")

    def test_conflicting_republication_preserves_the_valid_committed_set(self, tmp_path):
        backup_id = new_backup_id()
        storage, manifest, marker = _publish_into(tmp_path, backup_id, b"first-content")
        artifact_sha = _sha((tmp_path / "data" / f"{backup_id}.age").read_bytes())
        with pytest.raises(BackupConflictError):
            _publish_into(tmp_path, backup_id, b"different-content")
        # The valid earlier backup is untouched and still verifiable.
        storage2 = LocalFilesystemStorage(tmp_path)
        assert storage2.commit_marker(backup_id)["artifact_sha256"] == artifact_sha
        _, _, replay = _publish_into(tmp_path, backup_id, b"first-content")
        assert replay["state"] == "verified-published"

    def test_extra_unexpected_artifacts_do_not_affect_validation(self, tmp_path):
        backup_id = new_backup_id()
        _publish_into(tmp_path, backup_id, b"ciphertext")
        (tmp_path / "data" / "stray-file.age").write_bytes(b"unrelated")
        _, _, marker2 = _publish_into(tmp_path, backup_id, b"ciphertext")
        assert marker2["state"] == "verified-published"

    def test_replay_with_same_artifact_but_new_manifest_returns_committed_marker(self, tmp_path):
        """The COMMITTED set is authoritative: timestamps in a retry's fresh
        manifest do not rewrite history for an already-committed backup id."""
        backup_id = new_backup_id()
        storage, manifest, marker = _publish_into(tmp_path, backup_id, b"ciphertext")
        artifact = _artifact_file(tmp_path, b"ciphertext", name="retry-source.age")
        fresh_manifest = build_manifest(
            ManifestBuildInputs(
                backup_id=backup_id,
                started_at_utc="2026-10-20T09:00:00+00:00",
                created_at_utc="2026-10-20T09:00:01+00:00",
                server_version="PostgreSQL 18.6",
                database_name="aic_test_x",
                alembic_revision=None,
                tool_versions={"age": "v1.3.2", "pg_dump": "pg_dump (PostgreSQL) 18.6"},
                recipient="age1example",
                artifact_sha256=_sha(b"ciphertext"),
                artifact_size=9,
                artifact_filename=f"{backup_id}.age",
            )
        )
        replay = storage.publish(
            backup_id,
            artifact_path=artifact,
            manifest_bytes=fresh_manifest.to_bytes(),
            artifact_sha256=_sha(b"ciphertext"),
        )
        assert replay == marker  # the committed marker, not the fresh manifest
        on_disk = json.loads((tmp_path / "manifest" / f"{backup_id}.json").read_text())
        assert on_disk["created_at_utc"] == "2026-10-19T00:00:01+00:00"  # untouched


class TestR11PublicationProtocol:
    def test_staging_names_are_unique_and_no_part_files_survive(self, tmp_path):
        backup_id = new_backup_id()
        _publish_into(tmp_path, backup_id, b"ciphertext")
        assert list(tmp_path.rglob("*.part")) == []

    def test_interrupted_marker_creation_is_completed_by_safe_retry(self, tmp_path):
        """A crash between manifest and marker: retry with SAME id completes."""
        backup_id = new_backup_id()
        storage = LocalFilesystemStorage(tmp_path)
        storage._ensure_dirs()
        payload = b"interrupted-then-completed"
        artifact = _artifact_file(tmp_path, payload)
        # Crash simulation: artifact + manifest promoted, marker NOT written.
        storage._publish_large(storage._data_path(backup_id), artifact, _sha(payload))
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
                artifact_sha256=_sha(payload),
                artifact_size=len(payload),
                artifact_filename=f"{backup_id}.age",
            )
        )
        from commons_core.backup.storage import _write_part

        _write_part(
            storage._manifest_path(backup_id),
            manifest.to_bytes(),
            expected_sha256=sha256_bytes(manifest.to_bytes()),
        )
        assert storage.commit_marker(backup_id) is None  # interrupted: no marker
        # Safe retry with the SAME id and bytes completes the publication.
        _, _, marker = _publish_into(tmp_path, backup_id, payload)
        assert marker["state"] == "verified-published"
        assert list(tmp_path.rglob("*.part")) == []

    def test_failure_then_safe_retry_completes_publication(self, tmp_path):
        """A transient storage outage fails publish; a retry with the same id
        and same bytes completes cleanly (idempotent recovery, R11)."""
        backup_id = new_backup_id()

        class FlakyStorage(LocalFilesystemStorage):
            def __init__(self, root, fail_times=1):
                super().__init__(root)
                self.fail_times = fail_times

            def _publish_large(self, final, source, expected_sha):
                if self.fail_times > 0:
                    self.fail_times -= 1
                    raise StorageError("simulated transient outage", transient=True)
                return super()._publish_large(final, source, expected_sha)

        flaky = FlakyStorage(tmp_path, fail_times=3)  # > retry_attempts → hard failure
        flaky.retry_attempts = 3
        payload = b"retry-me"
        artifact = _artifact_file(tmp_path, payload, name="retry-source.age")
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
                artifact_sha256=_sha(payload),
                artifact_size=len(payload),
                artifact_filename=f"{backup_id}.age",
            )
        )
        with pytest.raises(StorageError, match="artifact publication failed"):
            flaky.publish(
                backup_id,
                artifact_path=artifact,
                manifest_bytes=manifest.to_bytes(),
                artifact_sha256=_sha(payload),
            )
        assert flaky.commit_marker(backup_id) is None  # nothing blessed
        # Safe retry with the SAME id and bytes completes.
        _, _, marker = _publish_into(tmp_path, backup_id, payload)
        assert marker["state"] == "verified-published"

    def test_concurrent_same_content_publications_both_succeed(self, tmp_path):
        """Two threads publish the SAME backup id/content simultaneously:
        unique staging + exclusive promotion keep the object set consistent
        and both publishers observe a verified commit (R11 race window)."""
        backup_id = new_backup_id()
        payload = b"concurrent-same-content"
        results: dict[str, object] = {}

        def worker(name: str) -> None:
            try:
                _, _, marker = _publish_into(tmp_path, backup_id, payload)
                results[name] = marker["state"]
            except Exception as exc:  # noqa: BLE001 — record any failure
                results[name] = f"{type(exc).__name__}: {exc}"

        threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert all(state == "verified-published" for state in results.values()), results
        assert list(tmp_path.rglob("*.part")) == []
        # The committed set is fully verifiable after the race.
        _, _, replay = _publish_into(tmp_path, backup_id, payload)
        assert replay["state"] == "verified-published"

    def test_concurrent_different_content_publication_keeps_set_intact(self, tmp_path):
        backup_id = new_backup_id()
        outcomes: list[str] = []

        def worker(payload: bytes) -> None:
            try:
                _publish_into(tmp_path, backup_id, payload)
                outcomes.append("published")
            except (BackupConflictError, PublishedSetCorrupted, StorageError) as exc:
                outcomes.append(type(exc).__name__)

        threads = [
            threading.Thread(target=worker, args=(f"content-{i}".encode(),)) for i in range(3)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert "published" in outcomes
        losers = [o for o in outcomes if o != "published"]
        # Whatever lost the race must be an explicit conflict/corruption error
        # (with distinct content at most one publisher can win).
        assert all(o in ("BackupConflictError", "PublishedSetCorrupted") for o in losers)
        # The surviving committed set is self-consistent.
        storage = LocalFilesystemStorage(tmp_path)
        marker = storage.commit_marker(backup_id)
        assert marker is not None
        artifact_sha = _sha((tmp_path / "data" / f"{backup_id}.age").read_bytes())
        assert marker["artifact_sha256"] == artifact_sha

    def test_failed_latest_pointer_does_not_demote_a_committed_backup(self, tmp_path, capsys):
        backup_id = new_backup_id()

        def broken_latest(latest, marker):
            raise StorageError("simulated latest.json outage", transient=True)

        with patch("commons_core.backup.storage._write_latest", side_effect=broken_latest):
            storage, manifest, marker = _publish_into(tmp_path, backup_id, b"ciphertext")
        assert marker["state"] == "verified-published"  # committed set is success
        out = capsys.readouterr()
        assert "WARNING" in (out.err + out.out)
        # The backup remains fully verifiable despite the stale index.
        _, _, replay = _publish_into(tmp_path, backup_id, b"ciphertext")
        assert replay["state"] == "verified-published"


# ---------------------------------------------------------------------------
# R12 — backup-side cleanup: observable, bounded, never silent
# ---------------------------------------------------------------------------
class TestR12BackupCleanup:
    def _synthetic_run_backup(self, tmp_path, *, storage, backup_id="probe-fail"):
        """run_backup with the streaming stage stubbed to produce staged output."""
        belt = SimpleNamespace(
            ensure_compatible=lambda x: None,
            version_info=lambda: {"age": "synthetic", "pg_dump": "synthetic"},
        )

        def synth_stream(**kwargs):
            kwargs["staged_artifact"].write_bytes(b"synthetic encrypted placeholder")

        with (
            patch.object(
                pipeline,
                "_source_metadata",
                return_value=(
                    {"server_version": "PostgreSQL 18.6", "database_name": "synthetic"},
                    "0001",
                    18,
                ),
            ),
            patch.object(pipeline, "_stream_dump_to_age", side_effect=synth_stream),
            patch.object(pipeline, "verify_age_file", return_value=None),
        ):
            return run_backup(
                database_url="postgresql://commons:pw@127.0.0.1:5432/aic_test_synth",
                recipient="age1synthetic",
                storage=storage,
                work_dir=tmp_path / "work",
                toolbelt=belt,
                backup_id=backup_id,
            )

    def test_t10_regression_publication_failure_removes_staged_artifact(self, tmp_path):
        failing = SimpleNamespace(
            publish=lambda *a, **k: (_ for _ in ()).throw(
                StorageError("injected publication failure")
            )
        )
        with pytest.raises(PublicationFailed):
            self._synthetic_run_backup(tmp_path, storage=failing)
        staged = tmp_path / "work" / "probe-fail.age.part"
        assert not staged.exists()  # the T10 residue is gone

    def test_cleanup_failure_on_failure_path_is_reported_not_swallowed(self, tmp_path):
        failing = SimpleNamespace(
            publish=lambda *a, **k: (_ for _ in ()).throw(
                StorageError("injected publication failure")
            )
        )
        with patch.object(
            pipeline,
            "_remove_staged_observed",
            return_value=(False, "PermissionError: simulated unlink refusal"),
        ):
            with pytest.raises(BackupError) as exc_info:
                self._synthetic_run_backup(tmp_path, storage=failing)
        message = str(exc_info.value)
        assert "injected publication failure" in message  # primary failure visible
        assert "INCOMPLETE CLEANUP" in message  # cleanup failure visible too
        assert "PermissionError" in message

    def test_cleanup_failure_on_success_path_is_an_explicit_warning(self, tmp_path, capsys):
        storage = LocalFilesystemStorage(tmp_path / "storage")
        with patch.object(
            pipeline,
            "_remove_staged_observed",
            return_value=(False, "PermissionError: simulated unlink refusal"),
        ):
            outcome = self._synthetic_run_backup(tmp_path, storage=storage, backup_id="probe-ok")
        assert outcome.manifest.backup_id == "probe-ok"  # committed + verified
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "could not be removed" in captured.err

    def test_pg_dump_failure_removes_staged_artifact_and_publishes_nothing(self, tmp_path):
        """Fault injection 1: pg_dump exits non-zero mid-run (R12)."""
        belt = _fake_belt(
            tmp_path,
            pg_dump_script=(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "if '--version' in sys.argv:\n"
                "    print('pg_dump (PostgreSQL) 18.6 (fake)')\n"
                "    sys.exit(0)\n"
                "sys.stdout.buffer.write(b'PGDMP' + b'\\x00' * 64)\n"
                "sys.stdout.buffer.flush()\n"
                "sys.stderr.write('fake pg_dump: mid-dump failure\\n')\n"
                "sys.exit(2)\n"
            ),
        )
        storage = LocalFilesystemStorage(tmp_path / "storage")
        with pytest.raises(BackupError):
            run_backup(
                database_url="postgresql://commons:pw@127.0.0.1:5432/missing_synth_db",
                recipient="age1example",
                storage=storage,
                work_dir=tmp_path / "work",
                toolbelt=belt,
            )
        assert list((tmp_path / "work").rglob("*.part")) == []
        assert not storage.list_backup_ids()

    def test_hanging_subprocess_hits_the_deadline_and_is_terminated(self, tmp_path, monkeypatch):
        """Fault injections 3+5: a tool that ignores time and SIGTERM is
        killed by the deadline, bounded, and reported (R12)."""
        monkeypatch.setenv("AIC_BACKUP_SUBPROCESS_DEADLINE", "1")
        belt = _fake_belt(
            tmp_path,
            pg_dump_script=(
                "#!/usr/bin/env python3\n"
                "import signal, sys, time\n"
                "if '--version' in sys.argv:\n"
                "    print('pg_dump (PostgreSQL) 18.6 (fake)')\n"
                "    sys.exit(0)\n"
                "signal.signal(signal.SIGTERM, lambda *a: None)  # refuse grace\n"
                "sys.stdout.buffer.write(b'PGDMP' + b'\\x00' * 16)\n"
                "sys.stdout.buffer.flush()\n"
                "time.sleep(30)\n"
            ),
        )
        storage = LocalFilesystemStorage(tmp_path / "storage")
        started = time.monotonic()
        with patch.object(
            pipeline,
            "_source_metadata",
            return_value=(
                {"server_version": "PostgreSQL 18.6", "database_name": "synthetic"},
                "0001",
                18,
            ),
        ):
            with pytest.raises(DeadlineExceeded) as exc_info:
                run_backup(
                    database_url="postgresql://commons:pw@127.0.0.1:5432/missing_synth_db",
                    recipient="age1example",
                    storage=storage,
                    work_dir=tmp_path / "work",
                    toolbelt=belt,
                )
        elapsed = time.monotonic() - started
        assert elapsed < 30, "the deadline must bound the failure, not the tool"
        assert "deadline" in str(exc_info.value)
        assert list((tmp_path / "work").rglob("*.part")) == []  # staged output discarded
        assert not storage.list_backup_ids()

    def test_stderr_capture_is_bounded(self, tmp_path):
        """A flooding tool cannot exhaust memory through the reader threads."""
        assert STDERR_MAX_LINES == 256
        sink = pipeline._new_stderr_sink()

        class FakeStream:
            def __init__(self, lines=5000):
                self._lines = (b"flood line\n" for _ in range(lines))

            def readline(self):
                try:
                    return next(self._lines)
                except StopIteration:
                    return b""

            def close(self):
                pass

        thread = pipeline._reader_thread(FakeStream(), sink)
        thread.join(timeout=10)
        assert len(sink) == STDERR_MAX_LINES  # bounded, newest lines kept


class TestR12TerminationHelpers:
    def test_terminate_process_tree_escalates_and_reports_unreaped(self):
        """Fault injection 6: even a SIGKILL that fails to reap is reported."""
        from subprocess import TimeoutExpired

        class StubbornProc:
            pid = 0x7FFFFFFF  # no such process group → killpg raises, falls back

            def poll(self):
                return None  # never exits from the supervisor's point of view

            def terminate(self):
                pass  # refuses

            def kill(self):
                pass  # even SIGKILL "fails"

            def wait(self, timeout=None):
                raise TimeoutExpired(cmd="stubborn", timeout=timeout)

        note = terminate_process_tree(StubbornProc(), label="stubborn-tool")
        assert note is not None
        assert "could not be reaped" in note

    def test_terminate_process_tree_reaps_gracefully(self):
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, no user input
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert terminate_process_tree(proc, label="sleeper") is None
        assert proc.poll() is not None  # confirmed dead

    def test_sigterm_refusing_process_is_killed_by_escalation(self):
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, no user input
            [
                sys.executable,
                "-c",
                "import signal, time\nsignal.signal(signal.SIGTERM, lambda *a: None)\ntime.sleep(30)",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        started = time.monotonic()
        assert terminate_process_tree(proc, label="sigterm-trapper") is None
        assert proc.poll() is not None
        assert time.monotonic() - started < 20  # 5s grace + 10s reap bound


# ---------------------------------------------------------------------------
# R12 — restore-side cleanup: observed, reported, never swallowed
# ---------------------------------------------------------------------------
class TestR12RestoreCleanup:
    def _drill_scaffolding(self, tmp_path):
        """A published synthetic backup + drill inputs (no server touched)."""
        storage = LocalFilesystemStorage(tmp_path / "storage")
        backup_id = new_backup_id()
        _, manifest, _ = _publish_into(tmp_path / "storage", backup_id, b"drill-ciphertext")
        identity = tmp_path / "identity.txt"
        identity.write_text("AGE-SECRET-KEY-1SYNTHETIC\n")
        belt = SimpleNamespace(ensure_compatible=lambda x: None)
        return storage, backup_id, identity, belt

    def test_t11_regression_drop_failure_raises_instead_of_returning_none(self):
        class FakeEngine:
            def connect(self):
                raise RuntimeError("synthetic database unavailable")

            def dispose(self):
                pass

        with patch.object(restore, "create_engine", return_value=FakeEngine()):
            with pytest.raises(DrillCleanupError, match="may remain"):
                restore._drop_database(
                    "postgresql://commons:pw@127.0.0.1:5432/postgres", "aic_drill_synthetic"
                )

    def test_drop_reports_database_that_survives_the_drop_statement(self):
        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, stmt, params=None):
                return SimpleNamespace(scalar=lambda: True)  # still exists

        class FakeEngine:
            def connect(self):
                return FakeConn()

            def dispose(self):
                pass

        with patch.object(restore, "create_engine", return_value=FakeEngine()):
            with pytest.raises(DrillCleanupError, match="STILL EXISTS"):
                restore._drop_database(
                    "postgresql://commons:pw@127.0.0.1:5432/postgres", "aic_drill_synthetic"
                )

    def test_drill_reports_failed_cleanup_and_never_claims_full_success(self, tmp_path):
        """D14 acceptance: a drill with unhandled residue reports FAIL."""
        storage, backup_id, identity, belt = self._drill_scaffolding(tmp_path)
        passing = DrillReport(backup_id=backup_id, database_name="aic_drill_probe02")
        passing.add("synthetic battery check", True)
        passing.passed = True
        with (
            patch.object(restore, "_create_database"),
            patch.object(
                restore,
                "_stream_decrypt_restore",
                return_value=(True, ""),
            ),
            patch.object(restore, "verify_restored_database", return_value=passing),
            patch.object(
                restore,
                "_drop_database",
                side_effect=DrillCleanupError(
                    "cleanup of disposable database 'x' failed (RuntimeError) — "
                    "it may remain on the server"
                ),
            ),
        ):
            report = run_restore_drill(
                server_url="postgresql://commons:pw@127.0.0.1:5432/postgres",
                storage=storage,
                backup_id=backup_id,
                identity_path=identity,
                toolbelt=belt,
                database_name="aic_drill_probe02",
            )
        assert report.cleanup_state == "failed"
        assert report.cleanup_detail
        assert report.passed is False  # residue ⇒ never a complete PASS
        assert any(not c.passed for c in report.checks if "cleanup" in c.label.lower())
        payload = json.loads(report.to_json())
        assert payload["cleanup_state"] == "failed"

    def test_drill_success_reports_verified_cleanup(self, tmp_path):
        storage, backup_id, identity, belt = self._drill_scaffolding(tmp_path)
        passing = DrillReport(backup_id=backup_id, database_name="aic_drill_probe03")
        passing.add("synthetic battery check", True)
        passing.passed = True
        with (
            patch.object(restore, "_create_database"),
            patch.object(
                restore,
                "_stream_decrypt_restore",
                return_value=(True, ""),
            ),
            patch.object(restore, "verify_restored_database", return_value=passing),
            patch.object(restore, "_drop_database") as drop,
        ):
            report = run_restore_drill(
                server_url="postgresql://commons:pw@127.0.0.1:5432/postgres",
                storage=storage,
                backup_id=backup_id,
                identity_path=identity,
                toolbelt=belt,
                database_name="aic_drill_probe03",
            )
        drop.assert_called_once()
        assert report.cleanup_state == "dropped"
        assert report.passed is True
        assert any(c.passed for c in report.checks if "cleanup verified" in c.label)

    def test_primary_restore_failure_plus_cleanup_failure_shows_both(self, tmp_path):
        """R12 fault injection 11: neither failure masks the other.

        The primary failure here is an UNEXPECTED exception propagating out
        of the verification battery; the cleanup failure is chained onto it
        so both remain visible instead of one masking the other.
        """
        storage, backup_id, identity, belt = self._drill_scaffolding(tmp_path)
        with (
            patch.object(restore, "_create_database"),
            patch.object(
                restore,
                "_stream_decrypt_restore",
                return_value=(True, ""),
            ),
            patch.object(
                restore,
                "verify_restored_database",
                side_effect=RuntimeError("synthetic battery crash"),
            ),
            patch.object(
                restore,
                "_drop_database",
                side_effect=DrillCleanupError(
                    "cleanup of disposable database 'x' failed (RuntimeError) — "
                    "it may remain on the server"
                ),
            ),
        ):
            with pytest.raises(DrillFailure) as exc_info:
                run_restore_drill(
                    server_url="postgresql://commons:pw@127.0.0.1:5432/postgres",
                    storage=storage,
                    backup_id=backup_id,
                    identity_path=identity,
                    toolbelt=belt,
                    database_name="aic_drill_probe04",
                )
        message = str(exc_info.value)
        assert "synthetic battery crash" in message  # primary preserved
        assert "ADDITIONALLY" in message  # secondary surfaced
        assert "cleanup" in message

    def test_reported_restore_failure_plus_cleanup_failure_both_in_report(self, tmp_path):
        """The handled-failure variant: the stream fails (reported, not
        raised) AND cleanup fails — the returned report must carry BOTH
        verdicts and never claim success (R12)."""
        storage, backup_id, identity, belt = self._drill_scaffolding(tmp_path)
        with (
            patch.object(restore, "_create_database"),
            patch.object(
                restore,
                "_stream_decrypt_restore",
                return_value=(False, "synthetic restore failure"),
            ),
            patch.object(
                restore,
                "_drop_database",
                side_effect=DrillCleanupError(
                    "cleanup of disposable database 'x' failed (RuntimeError) — "
                    "it may remain on the server"
                ),
            ),
        ):
            report = run_restore_drill(
                server_url="postgresql://commons:pw@127.0.0.1:5432/postgres",
                storage=storage,
                backup_id=backup_id,
                identity_path=identity,
                toolbelt=belt,
                database_name="aic_drill_probe04b",
            )
        assert report.passed is False
        assert report.cleanup_state == "failed"
        assert any(not c.passed for c in report.checks if "pg_restore" in c.label)
        assert any(not c.passed for c in report.checks if "cleanup" in c.label.lower())

    def test_interrupted_restore_with_successful_cleanup_reports_fail_not_crash(self, tmp_path):
        """R12 fault injection 10: interrupted restore + successful cleanup."""
        storage, backup_id, identity, belt = self._drill_scaffolding(tmp_path)
        with (
            patch.object(restore, "_create_database"),
            patch.object(
                restore,
                "_stream_decrypt_restore",
                return_value=(False, "pg_restore failed with status 1 (synthetic)"),
            ),
            patch.object(restore, "_drop_database") as drop,
        ):
            report = run_restore_drill(
                server_url="postgresql://commons:pw@127.0.0.1:5432/postgres",
                storage=storage,
                backup_id=backup_id,
                identity_path=identity,
                toolbelt=belt,
                database_name="aic_drill_probe05",
            )
        drop.assert_called_once()
        assert report.cleanup_state == "dropped"
        assert report.passed is False

    def test_keep_database_override_skips_drop_and_is_recorded(self, tmp_path):
        storage, backup_id, identity, belt = self._drill_scaffolding(tmp_path)
        passing = DrillReport(backup_id=backup_id, database_name="aic_drill_probe06")
        passing.add("synthetic battery check", True)
        passing.passed = True
        with (
            patch.object(restore, "_create_database"),
            patch.object(
                restore,
                "_stream_decrypt_restore",
                return_value=(True, ""),
            ),
            patch.object(restore, "verify_restored_database", return_value=passing),
            patch.object(restore, "_drop_database") as drop,
        ):
            report = run_restore_drill(
                server_url="postgresql://commons:pw@127.0.0.1:5432/postgres",
                storage=storage,
                backup_id=backup_id,
                identity_path=identity,
                toolbelt=belt,
                database_name="aic_drill_probe06",
                keep_database=True,
            )
        drop.assert_not_called()
        assert report.cleanup_state == "kept-for-debug"

    def test_create_database_failure_leaves_nothing_to_clean(self, tmp_path):
        storage, backup_id, identity, belt = self._drill_scaffolding(tmp_path)
        with (
            patch.object(
                restore,
                "_create_database",
                side_effect=DrillFailure("could not create disposable database (synthetic)"),
            ),
            patch.object(restore, "_drop_database") as drop,
        ):
            with pytest.raises(DrillFailure, match="disposable database"):
                run_restore_drill(
                    server_url="postgresql://commons:pw@127.0.0.1:5432/postgres",
                    storage=storage,
                    backup_id=backup_id,
                    identity_path=identity,
                    toolbelt=belt,
                    database_name="aic_drill_probe07",
                )
        drop.assert_not_called()  # nothing was created; no blind DROP issued
