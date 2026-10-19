"""Synthetic disaster-recovery exercises (ADR-0010, Backup & Recovery Stage 2).

Spec §10's sixteen-step sequence, executed against a REAL PostgreSQL 18.6
server and the REAL Stage 1 backup/restore pipeline — with synthetic
runtime-generated keys only. The exercise must prove more than table
restoration: the recovery package (two offline copies) plus the moved-out
custody identity are the ONLY recovery inputs after the original database
is destroyed, and they must restore access to application-encrypted
content sealed under BOTH key generations (K1-era and K2-era records).

Also here (spec §8): backup-recipient rotation with retained archives —
single-recipient backups, dual-recipient transition, removal of a
recipient from future backups, and the honest statement that rotation
never revokes old archives (they stay decryptable by their own identities
for as long as retention keeps them).

Observability (spec §10): every phase prints a content-blind line with
timings; failures print explicit categories; nothing ever prints
messages, memories, keys, or identities.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from commons_core.backup.pipeline import run_backup
from commons_core.backup.recovery_keys import (
    create_recovery_package,
    duplicate_recovery_package,
    export_age_identity,
    export_application_env,
    read_age_identity_file,
    unlock_recovery_package,
    verify_recovery_package,
)
from commons_core.backup.restore import DrillFailure, run_restore_drill
from commons_core.backup.storage import LocalFilesystemStorage
from commons_core.database.models import Agent, MemoryEntry, MemoryNamespace
from commons_core.memory.service import write_entry as memory_write_entry
from commons_core.security.cryptography import EnvelopeCipher, KeyStore, UnknownKeyError
from tests.operational.backup_synth import MAX_BYTES, build_cipher, populate_synthetic_source
from tests.operational.test_backup_pipeline import (
    admin_create_db,
    admin_drop_db,
    alembic_upgrade,
    app_url_for,
    build_synthetic_source,
    make_identity,
    make_toolbelt,
)

# ---------------------------------------------------------------------------
# Environment plumbing — mirrors the Stage 1 pattern exactly (names and
# HAS_* rule identical to tests/operational/test_backup_pipeline.py).
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

HAS_DR_INTEGRATION = bool(
    ADMIN_URL and AGE_BIN and AGE_KEYGEN_BIN and PG_DUMP_BIN and PG_RESTORE_BIN
)

integration = pytest.mark.skipif(
    not HAS_DR_INTEGRATION,
    reason=(
        "disaster-recovery integration tests need a disposable PostgreSQL server "
        "(AIC_BACKUP_TEST_ADMIN_URL) plus age/age-keygen/pg_dump/pg_restore "
        "binaries (AIC_BACKUP_* envs or PATH)"
    ),
)


class _PhaseLog:
    """Content-blind observability: phase names + timings, never content."""

    def __init__(self) -> None:
        self._t0 = time.perf_counter()
        self.lines: list[str] = []

    def mark(self, label: str, note: str = "") -> None:
        elapsed = time.perf_counter() - self._t0
        line = f"[drill] {label} t+{elapsed:06.2f}s"
        if note:
            line += f" — {note}"
        self.lines.append(line)
        print(line)

    def summary(self, passed: bool, extra: dict | None = None) -> None:
        total = time.perf_counter() - self._t0
        verdict = "DR-PASS" if passed else "DR-FAIL"
        facts = " ".join(f"{k}={v}" for k, v in (extra or {}).items())
        print(f"[drill] {verdict} total={total:.2f}s {facts}")


# ---------------------------------------------------------------------------
# Local helpers (Stage 1 patterns, adapted)
# ---------------------------------------------------------------------------
def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse an exported AIC_*.env file into a plain dict."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _kid_entries(spec: str) -> list[str]:
    """The non-empty ``kid:<hex>`` entries of an AIC_MASTER_KEYS spec."""
    return [entry.strip() for entry in spec.split(",") if entry.strip()]


def _entry_kid(entry: str) -> str:
    return entry.split(":", 1)[0]


def _add_k2_era_record(database_url: str, master_full: str, facts: dict) -> None:
    """Write ONE extra memory entry sealed under k2 into the EXISTING namespace.

    Mirrors populate_synthetic_source's own service call (memory_write_entry
    with an explicit cipher), so the K2-era envelope is produced by the
    production code path. The author is the namespace's creator (the
    populate fixture's admin agent), so the service's ownership check
    passes without any extra permission plumbing.
    """
    cipher = build_cipher(master_full, "k2")
    payload = "RECOVERY-DR K2-era note v1 (sealed under the rotated active key)"
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            namespace = session.scalars(
                select(MemoryNamespace).where(MemoryNamespace.handle == facts["namespace_handle"])
            ).one()
            admin = session.scalars(select(Agent).where(Agent.handle == "backup-synth-admin")).one()
            memory_write_entry(
                session,
                admin,
                namespace,
                key="recovery-dr-k2-era",
                payload=payload,
                content_type="text/plain",
                cipher=cipher,
                max_bytes=MAX_BYTES,
            )
            session.commit()
    finally:
        engine.dispose()
    facts["memory_plaintexts"]["recovery-dr-k2-era"] = [payload]
    facts["table_counts"][MemoryEntry.__tablename__] += 1


def _leftover_databases() -> list[str]:
    """Database names matching the drill/source patterns (must end up empty)."""
    engine = create_engine(ADMIN_URL)
    try:
        with engine.connect() as conn:
            return list(
                conn.execute(
                    text(
                        "SELECT datname FROM pg_database "
                        "WHERE datname LIKE 'aic_dr%' OR datname LIKE 'aic_drill%'"
                    )
                ).scalars()
            )
    finally:
        engine.dispose()


def _failed_labels(report) -> list[str]:
    return [check.label for check in report.checks if not check.passed]


# ---------------------------------------------------------------------------
# Spec §10 — the sixteen-step disaster-recovery exercise
# ---------------------------------------------------------------------------
@integration
def test_full_disaster_recovery_two_copy_custody(tmp_path):
    """Total infrastructure loss survived with ONLY the offline custody set.

    After step 8 (the source database is dropped) the test never touches the
    runtime key ring or the original URL again: recovery inputs are exactly
    the two package copies, the moved-out custody identity, the backup
    vault, and the admin server URL.
    """
    log = _PhaseLog()
    db_name = f"aic_dr_{secrets.token_hex(4)}"
    try:
        # -- steps 1-4: synthetic source with a TWO-KEY ring; K1-era content
        # from populate_synthetic_source (sealed under the then-active k1),
        # plus ONE K2-era record added through the same service call path.
        admin_create_db(db_name)
        source_url = app_url_for(db_name)
        master_full = f"k1:{secrets.token_hex(32)},k2:{secrets.token_hex(32)}"
        log.mark("source-create", note=f"db={db_name} two-key ring")
        try:
            alembic_upgrade(source_url)
            facts = populate_synthetic_source(source_url, master_full, active_key_id="k1")
        except Exception:
            admin_drop_db(db_name)
            raise
        _add_k2_era_record(source_url, master_full, facts)
        log.mark(
            "source-populated",
            note=(
                f"tables={len(facts['table_counts'])} "
                f"rows={sum(facts['table_counts'].values())} "
                f"memory-keys={len(facts['memory_plaintexts'])} (K1-era + K2-era)"
            ),
        )

        # -- step 5: the owner makes the recovery package BEFORE the disaster;
        # the generated custody identity is moved OUT to offline storage.
        primary_dir = tmp_path / "offline" / "primary"
        created = create_recovery_package(
            primary_dir,
            generate_operational_identity=True,
            master_keys=master_full,
            active_key_id="k2",
            app_commit="stage2-dr-test",
            alembic_revision=facts["alembic_version"],
        )
        assert created.application_key_ids == ("k1", "k2")
        assert created.active_key_id == "k2"
        recipient = created.packaged_recipients[0]
        assert created.custody_identity_path is not None
        custody_file = tmp_path / "offline-storage" / "custody.txt"
        custody_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(created.custody_identity_path), str(custody_file))
        assert not created.custody_identity_path.exists()
        log.mark("recovery-package-created", note=f"generation={created.generation}")

        # -- step 6: the Stage 1 backup pipeline, encrypted to the packaged
        # operational recipient only.
        storage = LocalFilesystemStorage(tmp_path / "vault")
        outcome = run_backup(
            database_url=source_url,
            recipient=recipient,
            storage=storage,
            work_dir=tmp_path / "work",
            toolbelt=make_toolbelt(),
        )
        backup_id = outcome.manifest.backup_id
        assert outcome.manifest.alembic_revision == facts["alembic_version"]
        manifest_file = tmp_path / "backup-manifest.json"
        manifest_file.write_text(outcome.manifest.to_json(), encoding="utf-8")
        log.mark("backup-published", note=f"backup_id={backup_id}")

        # -- step 7: the second, byte-identical offline copy.
        secondary_dir = tmp_path / "offline" / "secondary"
        duplicate_recovery_package(primary_dir, secondary_dir)
        log.mark("second-copy-made", note="byte-identical redundancy copy")

        # -- steps 8-9: TOTAL LOSS. From here on the test must NOT use the
        # runtime key ring or the source URL — recovery relies only on the
        # package copies, the custody identity, the vault, and the admin URL.
        admin_drop_db(db_name)
        master_full = ""
        source_url = ""
        log.mark("environment-lost", note="original database destroyed")

        # -- step 10: verify the primary copy end-to-end: manifest integrity,
        # trust anchor, custody hygiene, decrypt, and rotation readiness.
        report = verify_recovery_package(
            primary_dir,
            expected_sha256=created.custody_manifest_sha256,
            identity_file=custody_file,
            check_decrypt=True,
            rotation_manifest_path=manifest_file,
        )
        assert report.passed, report.render()
        labels = {check.label: check.passed for check in report.checks}
        assert labels["trust-anchor-sha256"]
        assert labels["secrets-decrypt"]
        assert labels["no-identity-in-package-dir"]
        assert labels["rotation-ready"]
        log.mark("primary-package-verified", note=f"checks={len(report.checks)}")

        # -- step 11: the two copies are byte-identical.
        compare = verify_recovery_package(primary_dir, compare_dir=secondary_dir)
        assert compare.passed, compare.render()
        log.mark("copies-compared", note="copy-identical")

        # -- step 12: recover BOTH key generations from the package alone.
        payload, _ = unlock_recovery_package(primary_dir, custody_file)
        recovered_identity = export_age_identity(
            payload, "operational", tmp_path / "recovered-identity.txt"
        )
        env_file = export_application_env(payload, tmp_path / "recovered.env")
        env_values = _parse_env_file(env_file)
        master_spec_recovered = env_values["AIC_MASTER_KEYS"]
        active_recovered = env_values["AIC_ACTIVE_KEY_ID"]
        assert active_recovered == "k2"
        store = KeyStore(master_spec_recovered, active_recovered, env="test")
        assert sorted(store.list_key_ids()) == ["k1", "k2"]
        log.mark(
            "keys-recovered",
            note=f"active={active_recovered} key-ids={len(store.list_key_ids())}",
        )

        # -- step 13: restore + verify against a disposable database with the
        # RECOVERED material (full ring opens K1-era AND K2-era content).
        drill = run_restore_drill(
            server_url=ADMIN_URL,
            storage=storage,
            backup_id=backup_id,
            identity_path=recovered_identity,
            toolbelt=make_toolbelt(),
            expected_facts=facts,
            master_keys=master_spec_recovered,
            active_key_id=active_recovered,
        )
        assert drill.passed is True, drill.render()
        print(drill.render())
        log.mark(
            "restore-drill-passed",
            note=(
                f"checks={len(drill.checks)} "
                f"tables={len(facts['table_counts'])} "
                f"rows={sum(facts['table_counts'].values())}"
            ),
        )

        # -- step 14a: a K2-ONLY key ring must fail the drill explicitly on the
        # K1-era memory chain (no silent partial recovery), and the partial
        # ring provably cannot open a K1-sealed envelope.
        k2_only_spec = ",".join(
            entry for entry in _kid_entries(master_spec_recovered) if _entry_kid(entry) == "k2"
        )
        assert _entry_kid(k2_only_spec) == "k2" and "," not in k2_only_spec
        drill_partial = run_restore_drill(
            server_url=ADMIN_URL,
            storage=storage,
            backup_id=backup_id,
            identity_path=recovered_identity,
            toolbelt=make_toolbelt(),
            expected_facts=facts,
            master_keys=k2_only_spec,
            active_key_id="k2",
        )
        assert drill_partial.passed is False
        partial_failed = _failed_labels(drill_partial)
        assert any("memory chain" in label for label in partial_failed), partial_failed
        print(drill_partial.render())
        full_cipher = EnvelopeCipher(KeyStore(master_spec_recovered, "k1", env="test"))
        k1_envelope = full_cipher.seal("dr-k1-era-local-probe", "dr-test:k1-envelope")
        partial_cipher = EnvelopeCipher(KeyStore(k2_only_spec, "k2", env="test"))
        with pytest.raises(UnknownKeyError):
            partial_cipher.open(k1_envelope, "dr-test:k1-envelope")
        log.mark(
            "expected-failure partial-keyring",
            note=f"category=keyring-incomplete failed-checks={len(partial_failed)}",
        )

        # -- step 14b: the WRONG age identity fails loudly. A missing identity
        # file raises DrillFailure; an unrelated (existing) identity produces
        # an explicit failed decrypt-gate check — never a silent pass.
        with pytest.raises(DrillFailure):
            run_restore_drill(
                server_url=ADMIN_URL,
                storage=storage,
                backup_id=backup_id,
                identity_path=tmp_path / "nonexistent-identity.txt",
                toolbelt=make_toolbelt(),
            )
        wrong_dir = tmp_path / "wrong-identity"
        wrong_dir.mkdir(parents=True, exist_ok=True)
        wrong_identity, _wrong_recipient = make_identity(wrong_dir)
        drill_wrong = run_restore_drill(
            server_url=ADMIN_URL,
            storage=storage,
            backup_id=backup_id,
            identity_path=wrong_identity,
            toolbelt=make_toolbelt(),
        )
        assert drill_wrong.passed is False
        wrong_failed = _failed_labels(drill_wrong)
        assert any("age decrypt" in label for label in wrong_failed), wrong_failed
        print(drill_wrong.render())
        log.mark(
            "expected-failure wrong-identity",
            note=f"category=decrypt-gate failed-checks={len(wrong_failed)}",
        )

        # -- step 15: tamper with the SECONDARY copy — verification must fail
        # there while the pristine PRIMARY copy still fully verifies.
        secondary_secrets = secondary_dir / created.secrets_path.name
        blob = secondary_secrets.read_bytes()
        mid = len(blob) // 2
        secondary_secrets.write_bytes(blob[:mid] + bytes([blob[mid] ^ 0x01]) + blob[mid + 1 :])
        tampered_report = verify_recovery_package(secondary_dir)
        assert tampered_report.passed is False
        assert any(
            check.label == "secrets-sha256" and not check.passed for check in tampered_report.checks
        )
        primary_report = verify_recovery_package(
            primary_dir,
            expected_sha256=created.custody_manifest_sha256,
            identity_file=custody_file,
            check_decrypt=True,
        )
        assert primary_report.passed is True, primary_report.render()
        log.mark(
            "tamper-detected redundancy-intact",
            note="secondary corrupted; primary still verifies + decrypts",
        )

        # -- step 16: no drill/source databases remain; temporary material is
        # removed.
        leftovers = _leftover_databases()
        assert leftovers == [], leftovers
        shutil.rmtree(tmp_path, ignore_errors=True)
        log.summary(
            passed=True,
            extra={
                "generation": created.generation,
                "tables": len(facts["table_counts"]),
                "rows": sum(facts["table_counts"].values()),
            },
        )
    except Exception:
        log.summary(passed=False, extra={"category": "unexpected"})
        raise
    finally:
        # Best-effort hygiene: the source db is already gone after step 8 on
        # the success path; this covers failures before the total-loss step.
        admin_drop_db(db_name)


# ---------------------------------------------------------------------------
# Spec §8 — backup-recipient rotation with retained archives
# ---------------------------------------------------------------------------
@integration
def test_backup_recipient_rotation_with_retained_archives(tmp_path):
    """Rotation changes FUTURE backups only; old archives stay decryptable.

    recipientA era → dual-recipient transition (A+B) → recipientB removed
    from future backups → recipientB's retained archive still drills green
    with its own identity. A package holding identityA only is
    rotation-ready for the A-era manifest and explicitly NOT ready for the
    B-era manifest.
    """
    log = _PhaseLog()
    source_url, master_keys, facts = build_synthetic_source(prefix="aic_rot")
    try:
        log.mark(
            "source-built",
            note=f"tables={len(facts['table_counts'])} rows={sum(facts['table_counts'].values())}",
        )
        ids_dir = tmp_path / "ids"
        ids_dir.mkdir(parents=True, exist_ok=True)
        identity_a, recipient_a = make_identity(ids_dir)
        identity_b, recipient_b = make_identity(ids_dir)
        storage = LocalFilesystemStorage(tmp_path / "vault")
        work_dir = tmp_path / "work"

        def publish(recipient: str, extra: list[str] | None = None, name: str = ""):
            outcome = run_backup(
                database_url=source_url,
                recipient=recipient,
                storage=storage,
                work_dir=work_dir,
                toolbelt=make_toolbelt(),
                extra_recipients=extra or [],
            )
            manifest_file = tmp_path / f"manifest-{name}.json"
            manifest_file.write_text(outcome.manifest.to_json(), encoding="utf-8")
            return outcome, manifest_file

        def drill(identity: Path, backup_id: str):
            return run_restore_drill(
                server_url=ADMIN_URL,
                storage=storage,
                backup_id=backup_id,
                identity_path=identity,
                toolbelt=make_toolbelt(),
                expected_facts=facts,
                master_keys=master_keys,
            )

        # Single-recipient era: backup1 → A only, backup2 → B only.
        out1, _m1 = publish(recipient_a, name="1")
        out2, m2 = publish(recipient_b, name="2")
        log.mark(
            "single-recipient-era",
            note=f"backup1={out1.manifest.backup_id} backup2={out2.manifest.backup_id}",
        )
        assert drill(identity_a, out1.manifest.backup_id).passed is True
        wrong_for_backup1 = drill(identity_b, out1.manifest.backup_id)
        assert wrong_for_backup1.passed is False
        assert any("age decrypt" in label for label in _failed_labels(wrong_for_backup1))
        log.mark("backup1-scoped", note="A passes; B fails explicitly")

        # Dual-recipient transition: backup3 → A + B.
        out3, _m3 = publish(recipient_a, extra=[recipient_b], name="3")
        recipients3 = json.loads(out3.manifest.to_json())["artifact"]["recipients"]
        assert recipients3 == [recipient_a, recipient_b]
        assert drill(identity_a, out3.manifest.backup_id).passed is True
        assert drill(identity_b, out3.manifest.backup_id).passed is True
        log.mark("dual-recipient-transition", note="backup3 decryptable by BOTH identities")

        # Removal from future backups: backup4 → A only again.
        out4, m4 = publish(recipient_a, name="4")
        recipients4 = json.loads(out4.manifest.to_json())["artifact"]["recipients"]
        assert recipients4 == [recipient_a]
        removed = drill(identity_b, out4.manifest.backup_id)
        assert removed.passed is False
        assert any("age decrypt" in label for label in _failed_labels(removed))
        assert drill(identity_a, out4.manifest.backup_id).passed is True
        log.mark("recipient-removed-from-future", note="backup4 scoped to A only")

        # Retained archives: rotation NEVER revokes old archives — backup2
        # still restores with identityB (and still refuses identityA).
        assert drill(identity_b, out2.manifest.backup_id).passed is True
        retained_wrong = drill(identity_a, out2.manifest.backup_id)
        assert retained_wrong.passed is False
        log.mark(
            "retained-archive-still-decryptable",
            note="backup2 (B-era) unaffected by later rotation",
        )

        # Package rotation readiness: a package holding identityA only.
        package_dir = tmp_path / "rot-package"
        ident_a = read_age_identity_file(identity_a, label="rot-operational-a")
        created = create_recovery_package(
            package_dir,
            age_identities=[ident_a],
            generate_custody_identity=True,
        )
        assert created.custody_identity_path is not None
        moved_custody = tmp_path / "rot-offline" / "custody.txt"
        moved_custody.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(created.custody_identity_path), str(moved_custody))
        stale = verify_recovery_package(package_dir, rotation_manifest_path=m2)
        assert stale.passed is False
        assert any(check.label == "rotation-ready" and not check.passed for check in stale.checks)
        ready = verify_recovery_package(package_dir, rotation_manifest_path=m4)
        assert ready.passed is True, ready.render()
        log.mark(
            "package-rotation-readiness",
            note="B-era manifest NOT covered; A-era manifest covered",
        )

        log.summary(passed=True, extra={"backups": 4, "package": created.generation})
    except Exception:
        log.summary(passed=False, extra={"category": "unexpected"})
        raise
    finally:
        admin_drop_db(source_url.rsplit("/", 1)[-1])


# NOTE (task 3-d): a focused `test_partial_keyring_drill_failure_is_explicit`
# is intentionally NOT written — step 14a of the sixteen-step exercise above
# already covers exactly that scenario (K2-only drill fails explicitly with a
# memory-chain failure on K1-era content, plus the unit-level UnknownKeyError
# probe). A second drill run would duplicate ~15s of pg_dump/age/restore work
# without adding any assertion.
