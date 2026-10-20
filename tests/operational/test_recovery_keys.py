"""Recovery-key custody tests (ADR-0010, Backup & Recovery Stage 2).

Two groups, per the Stage 2 spec:

* Application master-key continuity (spec §7): the ten mandated K1/K2
  scenarios executed against the REAL key store and envelope cipher —
  active-key selection, key-ring expansion without re-encryption,
  retirement breakage, package-mediated restoration, wrong-key and
  tamper rejection.
* Custody package mechanics (spec §§6, 9, 11): create/verify/unlock/
  export/duplicate roundtrips, two-copy redundancy and loss, trust-anchor
  checks, and the fault-injection battery (truncation, tampering, swapped
  sections, inconsistent claims, unsafe destinations, interrupted
  creation, read-only storage, overwrite refusal…).

Every secret in this file is runtime-generated synthetic material; no
real production key may ever be supplied here (spec §6). Output captured
from tools is scanned to prove secrets never leak to stdout/stderr.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from commons_core.backup.recovery_keys import (
    AgeIdentity,
    CustodyToolError,
    PackageExistsError,
    PackageFormatError,
    PackageVerificationError,
    UnsafeOutputError,
    create_recovery_package,
    duplicate_recovery_package,
    export_age_identity,
    export_application_env,
    generate_age_identity,
    read_age_identity_file,
    unlock_recovery_package,
    verify_recovery_package,
)
from commons_core.security.cryptography import (
    Envelope,
    EnvelopeCipher,
    EnvelopeFormatError,
    KeyStore,
    TamperDetectedError,
    UnknownKeyError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

AGE_BIN = os.environ.get("AIC_BACKUP_AGE_BIN", "").strip() or shutil.which("age") or ""
KEYGEN_BIN = (
    os.environ.get("AIC_BACKUP_AGE_KEYGEN_BIN", "").strip() or shutil.which("age-keygen") or ""
)
AGE_AVAILABLE = bool(AGE_BIN and KEYGEN_BIN)
requires_age = pytest.mark.skipif(
    not AGE_AVAILABLE, reason="age / age-keygen binaries not available"
)

_SYNC_DIRS = ("dropbox", "onedrive", "icloud", "google drive")


def _two_key_spec() -> tuple[str, str, str]:
    """(full spec, k1-only spec, k2-only spec) — all runtime-synthetic."""
    k1 = secrets.token_hex(32)
    k2 = secrets.token_hex(32)
    return f"k1:{k1},k2:{k2}", f"k1:{k1}", f"k2:{k2}"


def _make_custody(tmp_path: Path) -> tuple[AgeIdentity, Path]:
    """A synthetic custody identity + its (moved-out) identity file."""
    identity = generate_age_identity(
        label="custody-test", work_dir=tmp_path / "work", age_keygen=KEYGEN_BIN
    )
    ident_file = tmp_path / "custody.txt"
    ident_file.write_text(
        f"# public key: {identity.recipient}\n{identity.identity}\n", encoding="utf-8"
    )
    return identity, ident_file


# ---------------------------------------------------------------------------
# Application master-key continuity (spec §7 — the ten mandated scenarios)
# ---------------------------------------------------------------------------


def test_s1_seal_uses_active_kid():
    spec, _, _ = _two_key_spec()
    cipher = EnvelopeCipher(KeyStore(spec, "k1", env="test"))
    envelope = cipher.seal("payload-one", "memory:ns1:key-a")
    assert envelope.kid == "k1"


def test_s2_keyring_supports_historical_keys():
    spec, _, _ = _two_key_spec()
    store = KeyStore(spec, "k1", env="test")
    assert set(store.list_key_ids()) == {"k1", "k2"}
    assert store.get("k2") is not None


def test_s3_active_key_selection_switches():
    spec, _, _ = _two_key_spec()
    store = KeyStore(spec, "k2", env="test")
    assert store.active_kid == "k2"


def test_s4_new_content_seals_under_new_active_key():
    spec, _, _ = _two_key_spec()
    cipher = EnvelopeCipher(KeyStore(spec, "k2", env="test"))
    assert cipher.seal("new era", "memory:ns1:key-b").kid == "k2"


def test_s5_k1_content_remains_decryptable_while_retained():
    spec, _, _ = _two_key_spec()
    old_cipher = EnvelopeCipher(KeyStore(spec, "k1", env="test"))
    envelope = old_cipher.seal("k1-era-secret", "memory:ns1:key-a")
    rotated_cipher = EnvelopeCipher(KeyStore(spec, "k2", env="test"))
    assert rotated_cipher.open(envelope, "memory:ns1:key-a") == "k1-era-secret"


def test_s6_removing_k1_breaks_k1_envelopes():
    spec, _, k2_only = _two_key_spec()
    old_cipher = EnvelopeCipher(KeyStore(spec, "k1", env="test"))
    envelope = old_cipher.seal("k1-era-secret", "memory:ns1:key-a")
    retired_cipher = EnvelopeCipher(KeyStore(k2_only, "k2", env="test"))
    with pytest.raises(UnknownKeyError):
        retired_cipher.open(envelope, "memory:ns1:key-a")


@requires_age
def test_s7_k1_restorable_from_recovery_package(tmp_path):
    spec, _, _ = _two_key_spec()
    custody, ident_file = _make_custody(tmp_path)
    create_recovery_package(
        tmp_path / "pkg",
        age_identities=[
            generate_age_identity(
                label="operational", work_dir=tmp_path / "work", age_keygen=KEYGEN_BIN
            )
        ],
        custody_identities=[custody],
        generate_custody_identity=False,
        master_keys=spec,
        active_key_id="k2",
    )
    moved = tmp_path / "custody-moved.txt"
    moved.write_text(ident_file.read_text(), encoding="utf-8")
    payload, _ = unlock_recovery_package(tmp_path / "pkg", moved)
    env_file = export_application_env(payload, tmp_path / "app.env")
    restored_spec = dict(
        line.split("=", 1)
        for line in env_file.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    rebuilt = KeyStore(restored_spec["AIC_MASTER_KEYS"], "k1", env="test")
    assert set(rebuilt.list_key_ids()) == {"k1", "k2"}


@requires_age
def test_s8_historical_content_decrypts_again_after_restore(tmp_path):
    spec, _, _ = _two_key_spec()
    old_cipher = EnvelopeCipher(KeyStore(spec, "k1", env="test"))
    envelope = old_cipher.seal("archaeology", "memory:ns9:old-key")
    payload_blob = json.dumps(
        {
            "payload_version": 1,
            "package_kind": "aic-recovery-package",
            "custody_generation": "gen-test",
            "backup_domain": {"age_identities": []},
            "application_domain": {"master_keys": spec, "active_key_id": "k2"},
        },
        indent=2,
        sort_keys=True,
    )
    exported = tmp_path / "restored.env"
    payload = json.loads(payload_blob)
    export_application_env(payload, exported)
    spec_back = dict(
        line.split("=", 1)
        for line in exported.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )["AIC_MASTER_KEYS"]
    cipher = EnvelopeCipher(KeyStore(spec_back, "k2", env="test"))
    assert cipher.open(envelope, "memory:ns9:old-key") == "archaeology"


def test_s9_wrong_key_material_rejected():
    spec, _, _ = _two_key_spec()
    cipher = EnvelopeCipher(KeyStore(spec, "k1", env="test"))
    envelope = cipher.seal("right key", "memory:ns1:key-a")
    wrong = f"k1:{secrets.token_hex(32)}"
    wrong_cipher = EnvelopeCipher(KeyStore(wrong, "k1", env="test"))
    with pytest.raises(TamperDetectedError):
        wrong_cipher.open(envelope, "memory:ns1:key-a")


def test_s10_corrupted_envelopes_rejected():
    spec, _, _ = _two_key_spec()
    cipher = EnvelopeCipher(KeyStore(spec, "k1", env="test"))
    envelope = cipher.seal("integrity matters", "memory:ns1:key-a")

    flipped = envelope.model_copy(
        update={
            "ciphertext": ("0" if envelope.ciphertext[0] != "0" else "1") + envelope.ciphertext[1:]
        }
    )
    with pytest.raises(TamperDetectedError):
        cipher.open(flipped, "memory:ns1:key-a")

    bad_hex = envelope.model_copy(update={"ciphertext": "zz" + envelope.ciphertext[2:]})
    with pytest.raises(EnvelopeFormatError):
        cipher.open(bad_hex, "memory:ns1:key-a")

    unknown_kid = envelope.model_copy(update={"kid": "k-gone"})
    with pytest.raises(UnknownKeyError):
        cipher.open(unknown_kid, "memory:ns1:key-a")

    with pytest.raises(TamperDetectedError):
        cipher.open(envelope, "memory:ns1:OTHER-key")


def test_s11_active_key_change_never_reencrypts_existing_ciphertext():
    spec, _, _ = _two_key_spec()
    k1_cipher = EnvelopeCipher(KeyStore(spec, "k1", env="test"))
    sealed = k1_cipher.seal("frozen in time", "memory:ns1:key-a")
    snapshot = sealed.model_dump()
    k2_cipher = EnvelopeCipher(KeyStore(spec, "k2", env="test"))
    _ = k2_cipher.seal("fresh seal", "memory:ns1:key-b")
    assert sealed.model_dump() == snapshot  # untouched object
    reopened = Envelope.model_validate(snapshot)
    assert k2_cipher.open(reopened, "memory:ns1:key-a") == "frozen in time"
    assert reopened.kid == "k1"  # historical kid is preserved, not rewritten


# ---------------------------------------------------------------------------
# Custody package mechanics (spec §§6, 9, 11)
# ---------------------------------------------------------------------------


def _create_simple_package(
    tmp_path: Path, *, master_keys: str | None = None, active: str | None = None, name: str = "pkg"
) -> Path:
    custody, _ = _make_custody(tmp_path)
    ident_file = tmp_path / "custody.txt"
    create_recovery_package(
        tmp_path / name,
        age_identities=[
            generate_age_identity(
                label="operational", work_dir=tmp_path / "work", age_keygen=KEYGEN_BIN
            )
        ],
        custody_identities=[custody],
        generate_custody_identity=False,
        master_keys=master_keys,
        active_key_id=active,
    )
    return ident_file


@requires_age
def test_package_create_verify_full_roundtrip(tmp_path, capsys):
    spec, _, _ = _two_key_spec()
    custody, ident_file = _make_custody(tmp_path)
    created = create_recovery_package(
        tmp_path / "pkg",
        age_identities=[
            generate_age_identity(
                label="operational", work_dir=tmp_path / "work", age_keygen=KEYGEN_BIN
            )
        ],
        custody_identities=[custody],
        generate_custody_identity=False,
        master_keys=spec,
        active_key_id="k2",
        app_commit="deadbeefcafe",
    )
    assert created.metadata_path.is_file() and created.secrets_path.is_file()
    metadata = json.loads(created.metadata_path.read_text(encoding="utf-8"))
    assert metadata["application_domain"]["key_ids"] == ["k1", "k2"]
    assert metadata["application_domain"]["active_key_id"] == "k2"
    assert metadata["secret_section"]["payload_sha256"]
    report = verify_recovery_package(
        tmp_path / "pkg",
        expected_sha256=created.custody_manifest_sha256,
        identity_file=ident_file,
        check_decrypt=True,
    )
    assert report.passed, report.render()
    labels = [c.label for c in report.checks]
    assert "trust-anchor-sha256" in labels
    captured = capsys.readouterr()
    assert custody.identity not in captured.out + captured.err


@requires_age
def test_generated_custody_identity_must_leave_the_package_dir(tmp_path):
    spec, _, _ = _two_key_spec()
    created = create_recovery_package(
        tmp_path / "pkg",
        generate_operational_identity=True,
        master_keys=spec,
        active_key_id="k2",
    )
    assert created.custody_identity_path is not None
    before = verify_recovery_package(tmp_path / "pkg")
    assert not before.passed  # hygiene check fails while identity sits inside
    moved = tmp_path / "elsewhere" / "custody.txt"
    moved.parent.mkdir(parents=True)
    moved.write_text(created.custody_identity_path.read_text(encoding="utf-8"))
    created.custody_identity_path.unlink()
    after = verify_recovery_package(tmp_path / "pkg", identity_file=moved, check_decrypt=True)
    assert after.passed, after.render()


@requires_age
def test_trust_anchor_mismatch_is_detected(tmp_path):
    spec, _, _ = _two_key_spec()
    custody, ident_file = _make_custody(tmp_path)
    create_recovery_package(
        tmp_path / "pkg",
        age_identities=[
            generate_age_identity(
                label="operational", work_dir=tmp_path / "work", age_keygen=KEYGEN_BIN
            )
        ],
        custody_identities=[custody],
        generate_custody_identity=False,
        master_keys=spec,
        active_key_id="k2",
    )
    report = verify_recovery_package(tmp_path / "pkg", expected_sha256="f" * 64)
    assert not report.passed
    assert any(c.label == "trust-anchor-sha256" and not c.passed for c in report.checks)


@requires_age
def test_tampered_secret_section_rejected(tmp_path):
    _create_simple_package(tmp_path)
    secrets_file = next((tmp_path / "pkg").glob("*.recovery-secrets.age"))
    blob = bytearray(secrets_file.read_bytes())
    blob[len(blob) // 2] ^= 0x01
    secrets_file.write_bytes(bytes(blob))
    report = verify_recovery_package(tmp_path / "pkg")
    assert not report.passed
    assert any("secrets-sha256" == c.label and not c.passed for c in report.checks)


@requires_age
def test_truncated_secret_section_rejected(tmp_path):
    ident_file = _create_simple_package(tmp_path)
    secrets_file = next((tmp_path / "pkg").glob("*.recovery-secrets.age"))
    blob = secrets_file.read_bytes()
    secrets_file.write_bytes(blob[: len(blob) // 2])
    report = verify_recovery_package(tmp_path / "pkg")
    assert not report.passed
    with pytest.raises(PackageVerificationError):
        unlock_recovery_package(tmp_path / "pkg", ident_file)


@requires_age
def test_missing_metadata_and_secrets_reported_explicitly(tmp_path):
    _create_simple_package(tmp_path)
    (next((tmp_path / "pkg").glob("*.recovery-metadata.json"))).unlink()
    report = verify_recovery_package(tmp_path / "pkg")
    assert not report.passed
    assert any("metadata-parse" == c.label and not c.passed for c in report.checks)
    # and the reverse: secrets missing
    _create_simple_package(tmp_path, name="pkg2")
    (next((tmp_path / "pkg2").glob("*.recovery-secrets.age"))).unlink()
    report2 = verify_recovery_package(tmp_path / "pkg2")
    assert any("secrets-present" == c.label and not c.passed for c in report2.checks)


@requires_age
def test_unsupported_package_version_rejected(tmp_path):
    _create_simple_package(tmp_path)
    meta_file = next((tmp_path / "pkg").glob("*.recovery-metadata.json"))
    metadata = json.loads(meta_file.read_text(encoding="utf-8"))
    metadata["package_version"] = 999
    meta_file.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    report = verify_recovery_package(tmp_path / "pkg")
    assert not report.passed
    assert any("metadata-parse" == c.label and not c.passed for c in report.checks)


@requires_age
def test_modified_metadata_breaks_recorded_anchor_and_claims(tmp_path):
    spec, _, _ = _two_key_spec()
    custody, ident_file = _make_custody(tmp_path)
    created = create_recovery_package(
        tmp_path / "pkg",
        age_identities=[
            generate_age_identity(
                label="operational", work_dir=tmp_path / "work", age_keygen=KEYGEN_BIN
            )
        ],
        custody_identities=[custody],
        generate_custody_identity=False,
        master_keys=spec,
        active_key_id="k2",
    )
    meta_file = created.metadata_path
    metadata = json.loads(meta_file.read_text(encoding="utf-8"))
    metadata["application_domain"]["key_ids"] = ["k1", "k2", "k3"]
    meta_file.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    stale = verify_recovery_package(
        tmp_path / "pkg", expected_sha256=created.custody_manifest_sha256
    )
    assert any("trust-anchor" in c.label and not c.passed for c in stale.checks)
    fresh = verify_recovery_package(tmp_path / "pkg", identity_file=ident_file, check_decrypt=True)
    assert not fresh.passed
    assert any("claims-consistent" == c.label and not c.passed for c in fresh.checks)


@requires_age
def test_swapped_secret_section_detected_via_payload_binding(tmp_path):
    spec, _, _ = _two_key_spec()
    custody, ident_file = _make_custody(tmp_path)
    first = create_recovery_package(
        tmp_path / "one",
        age_identities=[
            generate_age_identity(
                label="operational", work_dir=tmp_path / "work", age_keygen=KEYGEN_BIN
            )
        ],
        custody_identities=[custody],
        generate_custody_identity=False,
        master_keys=spec,
        active_key_id="k2",
    )
    second = create_recovery_package(
        tmp_path / "two",
        age_identities=[
            generate_age_identity(
                label="operational", work_dir=tmp_path / "work", age_keygen=KEYGEN_BIN
            )
        ],
        custody_identities=[custody],
        generate_custody_identity=False,
        master_keys=f"k9:{secrets.token_hex(32)}",
        active_key_id="k9",
    )
    # swap: second's secrets file inside first's directory
    (tmp_path / "one" / first.secrets_path.name).write_bytes(second.secrets_path.read_bytes())
    with pytest.raises(PackageVerificationError):
        unlock_recovery_package(tmp_path / "one", ident_file)
    report = verify_recovery_package(tmp_path / "one", identity_file=ident_file, check_decrypt=True)
    assert not report.passed


@requires_age
def test_duplicate_copies_and_drift_detection(tmp_path):
    _create_simple_package(tmp_path)
    dup = duplicate_recovery_package(tmp_path / "pkg", tmp_path / "second")
    same = verify_recovery_package(tmp_path / "pkg", compare_dir=dup)
    assert same.passed
    drifted = next(dup.glob("*.recovery-secrets.age"))
    blob = bytearray(drifted.read_bytes())
    blob[-1] ^= 0x01
    drifted.write_bytes(bytes(blob))
    differs = verify_recovery_package(tmp_path / "pkg", compare_dir=dup)
    assert any("copy-identical" == c.label and not c.passed for c in differs.checks)


@requires_age
def test_loss_of_either_copy_is_recoverable(tmp_path):
    spec, _, _ = _two_key_spec()
    ident_file = _create_simple_package(tmp_path, master_keys=spec, active="k2")
    dup = duplicate_recovery_package(tmp_path / "pkg", tmp_path / "second")
    shutil.rmtree(tmp_path / "pkg")
    report = verify_recovery_package(dup, identity_file=ident_file, check_decrypt=True)
    assert report.passed, report.render()
    payload, _ = unlock_recovery_package(dup, ident_file)
    assert payload["application_domain"]["master_keys"] == spec


@requires_age
def test_both_copies_corrupted_fails_safely(tmp_path):
    _create_simple_package(tmp_path)
    dup = duplicate_recovery_package(tmp_path / "pkg", tmp_path / "second")
    for package in (tmp_path / "pkg", dup):
        victim = next(package.glob("*.recovery-secrets.age"))
        blob = bytearray(victim.read_bytes())
        blob[64] ^= 0xFF
        victim.write_bytes(bytes(blob))
    for package in (tmp_path / "pkg", dup):
        report = verify_recovery_package(package)
        assert not report.passed


@requires_age
def test_no_silent_overwrite_ever(tmp_path):
    _create_simple_package(tmp_path)
    with pytest.raises(PackageExistsError):
        create_recovery_package(
            tmp_path / "pkg",
            generate_operational_identity=True,
            generate_custody_identity=True,
        )
    with pytest.raises((PackageExistsError, UnsafeOutputError)):
        duplicate_recovery_package(tmp_path / "pkg", tmp_path / "pkg")


def test_unsafe_output_directories_refused(tmp_path):
    git_dir = tmp_path / "repo"
    (git_dir / ".git").mkdir(parents=True)
    with pytest.raises(UnsafeOutputError):
        create_recovery_package(git_dir / "pkg", generate_operational_identity=False)
    for sync_name in _SYNC_DIRS:
        with pytest.raises(UnsafeOutputError):
            create_recovery_package(
                tmp_path / sync_name / "pkg", generate_operational_identity=False
            )
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "somefile.txt").write_text("x", encoding="utf-8")
    with pytest.raises(PackageExistsError):
        create_recovery_package(occupied, generate_operational_identity=False)


@requires_age
def test_read_only_storage_fails_explicitly(tmp_path):
    target = tmp_path / "readonly"
    target.mkdir()
    os.chmod(target, stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises((CustodyToolError, OSError)):
            create_recovery_package(
                target / "pkg",
                generate_operational_identity=True,
                generate_custody_identity=True,
            )
        assert list(target.iterdir()) == []  # nothing partial left behind
    finally:
        os.chmod(target, stat.S_IRWXU)


@requires_age
def test_interrupted_creation_is_detectably_incomplete(tmp_path, monkeypatch):
    spec, _, _ = _two_key_spec()
    real_replace = os.replace

    def failing_replace(src, dst, *args, **kwargs):
        raise OSError("simulated interruption during promotion")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises((CustodyToolError, OSError)):
        create_recovery_package(
            tmp_path / "pkg",
            generate_operational_identity=True,
            master_keys=spec,
            active_key_id="k1",
        )
    monkeypatch.setattr(os, "replace", real_replace)
    leftovers = [p.name for p in (tmp_path / "pkg").iterdir()]
    assert not any(name.endswith(".recovery-metadata.json") for name in leftovers)
    assert not any(name.endswith(".part") for name in leftovers)
    report = verify_recovery_package(tmp_path / "pkg")
    assert not report.passed  # incomplete package must never verify


@requires_age
def test_verification_is_stateless(tmp_path):
    _create_simple_package(tmp_path)
    before = sorted(p.name for p in (tmp_path / "pkg").iterdir())
    verify_recovery_package(tmp_path / "pkg")
    verify_recovery_package(tmp_path / "pkg", compare_dir=tmp_path)  # even a bogus compare
    after = sorted(p.name for p in (tmp_path / "pkg").iterdir())
    assert before == after


@requires_age
def test_wrong_and_corrupted_custody_identities_rejected(tmp_path):
    spec, _, _ = _two_key_spec()
    _create_simple_package(tmp_path, master_keys=spec, active="k2")
    other = generate_age_identity(
        label="stranger", work_dir=tmp_path / "work", age_keygen=KEYGEN_BIN
    )
    stranger_file = tmp_path / "stranger.txt"
    stranger_file.write_text(
        f"# public key: {other.recipient}\n{other.identity}\n", encoding="utf-8"
    )
    with pytest.raises(PackageVerificationError):
        unlock_recovery_package(tmp_path / "pkg", stranger_file)
    corrupted = tmp_path / "corrupted.txt"
    corrupted.write_text("# public key: age1corrupted\nNOT-A-REAL-KEY\n", encoding="utf-8")
    with pytest.raises(PackageFormatError):
        read_age_identity_file(corrupted)


@requires_age
def test_rotation_readiness_against_backup_manifests(tmp_path):
    custody, ident_file = _make_custody(tmp_path)
    operational = generate_age_identity(
        label="operational", work_dir=tmp_path / "work", age_keygen=KEYGEN_BIN
    )
    create_recovery_package(
        tmp_path / "pkg",
        age_identities=[operational],
        custody_identities=[custody],
        generate_custody_identity=False,
    )
    covered = tmp_path / "covered.json"
    covered.write_text(
        json.dumps({"artifact": {"recipient": operational.recipient}}), encoding="utf-8"
    )
    ok = verify_recovery_package(tmp_path / "pkg", rotation_manifest_path=covered)
    assert any("rotation-ready" == c.label and c.passed for c in ok.checks)
    uncovered = tmp_path / "uncovered.json"
    uncovered.write_text(
        json.dumps({"artifact": {"recipient": "age1unrelated000"}}), encoding="utf-8"
    )
    bad = verify_recovery_package(tmp_path / "pkg", rotation_manifest_path=uncovered)
    assert any("rotation-ready" == c.label and not c.passed for c in bad.checks)
    multi = tmp_path / "multi.json"
    multi.write_text(
        json.dumps(
            {
                "artifact": {
                    "recipient": "age1primary0000",
                    "recipients": ["age1primary0000", operational.recipient],
                }
            }
        ),
        encoding="utf-8",
    )
    both = verify_recovery_package(tmp_path / "pkg", rotation_manifest_path=multi)
    assert any("rotation-ready" == c.label and c.passed for c in both.checks)


@requires_age
def test_exports_refuse_overwrite_and_bad_labels(tmp_path):
    spec, _, _ = _two_key_spec()
    _create_simple_package(tmp_path, master_keys=spec, active="k2")
    generate_age_identity(label="spare", work_dir=tmp_path / "work", age_keygen=KEYGEN_BIN)
    # find the real packaged identity label from metadata
    meta = json.loads(
        next((tmp_path / "pkg").glob("*.recovery-metadata.json")).read_text(encoding="utf-8")
    )
    label = meta["backup_domain"]["age_identities"][0]["label"]
    custody_file = tmp_path / "custody.txt"
    payload, _ = unlock_recovery_package(tmp_path / "pkg", custody_file)
    out = export_age_identity(payload, label, tmp_path / "exported.txt")
    assert out.is_file()
    with pytest.raises(PackageExistsError):
        export_age_identity(payload, label, tmp_path / "exported.txt")
    with pytest.raises(PackageFormatError):
        export_age_identity(payload, "no-such-label", tmp_path / "x.txt")


@requires_age
def test_backup_domain_only_package(tmp_path):
    ident_file = _create_simple_package(tmp_path)  # no master keys
    payload, _ = unlock_recovery_package(tmp_path / "pkg", ident_file)
    assert payload["application_domain"] is None
    with pytest.raises(PackageFormatError):
        export_application_env(payload, tmp_path / "app.env")


@requires_age
def test_created_identity_roundtrips_through_export(tmp_path):
    spec, _, _ = _two_key_spec()
    custody, ident_file = _make_custody(tmp_path)
    operational = generate_age_identity(
        label="operational", work_dir=tmp_path / "work", age_keygen=KEYGEN_BIN
    )
    create_recovery_package(
        tmp_path / "pkg",
        age_identities=[operational],
        custody_identities=[custody],
        generate_custody_identity=False,
        master_keys=spec,
        active_key_id="k1",
    )
    payload, _ = unlock_recovery_package(tmp_path / "pkg", ident_file)
    exported = export_age_identity(payload, "operational", tmp_path / "op-identity.txt")
    # the exported identity file must be re-readable and structurally identical
    reread = read_age_identity_file(exported)
    assert reread.recipient == operational.recipient


# ---------------------------------------------------------------------------
# CLI behavior (exit codes, no secrets on stdout)
# ---------------------------------------------------------------------------


def _run_cli(script: str, *argv: str, env_extra: dict | None = None):
    env = {**os.environ, "AIC_BACKUP_AGE_BIN": AGE_BIN, "AIC_BACKUP_AGE_KEYGEN_BIN": KEYGEN_BIN}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script), *argv],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )


@requires_age
def test_cli_init_and_verify_happy_path(tmp_path):
    k1 = secrets.token_hex(32)
    k2 = secrets.token_hex(32)
    primary = tmp_path / "primary"
    result = _run_cli(
        "recovery_key_init.py",
        "--out-dir",
        str(primary),
        "--generate-age-identity",
        "--with-application-keys",
        "--app-commit",
        "integration-test",
        "--json",
        env_extra={"AIC_MASTER_KEYS": f"k1:{k1},k2:{k2}", "AIC_ACTIVE_KEY_ID": "k2"},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout[result.stdout.index("{") :])
    assert payload["generation"]
    anchor = payload["custody_manifest_sha256"]
    identity_path = Path(payload["custody_identity_path"])
    assert identity_path.is_file()
    moved = tmp_path / "custody-moved.txt"
    moved.write_text(identity_path.read_text(encoding="utf-8"))
    identity_path.unlink()

    verified = _run_cli(
        "recovery_key_verify.py",
        "--package-dir",
        str(primary),
        "--expected-sha256",
        anchor,
        "--identity-file",
        str(moved),
        "--check-decrypt",
        "--report-json",
        str(tmp_path / "report.json"),
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["passed"] is True

    exported = _run_cli(
        "recovery_key_verify.py",
        "--package-dir",
        str(primary),
        "--identity-file",
        str(moved),
        "--export-identity",
        f"operational:{tmp_path / 'op.txt'}",
        "--export-env-file",
        str(tmp_path / "app.env"),
    )
    assert exported.returncode == 0, exported.stdout + exported.stderr
    assert (tmp_path / "app.env").is_file()
    leaked = result.stdout + result.stderr + verified.stdout + verified.stderr + exported.stdout
    assert "AGE-SECRET-KEY-1" not in leaked
    assert k1 not in leaked and k2 not in leaked


@requires_age
def test_cli_duplicate_and_compare(tmp_path):
    k1 = secrets.token_hex(32)
    primary = tmp_path / "primary"
    init = _run_cli(
        "recovery_key_init.py",
        "--out-dir",
        str(primary),
        "--generate-age-identity",
        "--with-application-keys",
        env_extra={"AIC_MASTER_KEYS": f"k1:{k1}", "AIC_ACTIVE_KEY_ID": "k1"},
    )
    assert init.returncode == 0, init.stderr
    # the generated custody identity must leave the package dir before the
    # hygiene check can pass (spec §11) — move it to "offline storage" first
    identity_path = next(primary.glob("age-identity-custody-*.txt"))
    moved = tmp_path / "custody-moved.txt"
    moved.write_text(identity_path.read_text(encoding="utf-8"))
    identity_path.unlink()
    dup = _run_cli(
        "recovery_key_init.py",
        "--duplicate-from",
        str(primary),
        "--out-dir",
        str(tmp_path / "second"),
    )
    assert dup.returncode == 0, dup.stderr
    compare = _run_cli(
        "recovery_key_verify.py",
        "--package-dir",
        str(primary),
        "--compare-dir",
        str(tmp_path / "second"),
    )
    assert compare.returncode == 0, compare.stdout


@requires_age
def test_cli_usage_errors_exit_2(tmp_path):
    missing_mode = _run_cli(
        "recovery_key_init.py",
        "--out-dir",
        str(tmp_path / "a"),
        "--generate-age-identity",
    )
    assert missing_mode.returncode == 2
    keys_without_env = _run_cli(
        "recovery_key_init.py",
        "--out-dir",
        str(tmp_path / "b"),
        "--generate-age-identity",
        "--with-application-keys",
    )
    assert keys_without_env.returncode == 2
    decrypt_without_identity = _run_cli(
        "recovery_key_verify.py",
        "--package-dir",
        str(tmp_path / "b"),
        "--check-decrypt",
    )
    assert decrypt_without_identity.returncode == 2


@requires_age
def test_cli_tampered_package_exits_1(tmp_path):
    k1 = secrets.token_hex(32)
    primary = tmp_path / "primary"
    init = _run_cli(
        "recovery_key_init.py",
        "--out-dir",
        str(primary),
        "--generate-age-identity",
        "--with-application-keys",
        env_extra={"AIC_MASTER_KEYS": f"k1:{k1}", "AIC_ACTIVE_KEY_ID": "k1"},
    )
    assert init.returncode == 0, init.stderr
    victim = next(primary.glob("*.recovery-secrets.age"))
    blob = bytearray(victim.read_bytes())
    blob[10] ^= 0x01
    victim.write_bytes(bytes(blob))
    verify = _run_cli("recovery_key_verify.py", "--package-dir", str(primary))
    assert verify.returncode == 1
    assert "AGE-SECRET-KEY-1" not in verify.stdout + verify.stderr


@requires_age
def test_cli_overwrite_refused_exit_2(tmp_path):
    k1 = secrets.token_hex(32)
    primary = tmp_path / "primary"
    init = _run_cli(
        "recovery_key_init.py",
        "--out-dir",
        str(primary),
        "--generate-age-identity",
        "--with-application-keys",
        env_extra={"AIC_MASTER_KEYS": f"k1:{k1}", "AIC_ACTIVE_KEY_ID": "k1"},
    )
    assert init.returncode == 0
    again = _run_cli(
        "recovery_key_init.py",
        "--out-dir",
        str(primary),
        "--generate-age-identity",
        "--with-application-keys",
        env_extra={"AIC_MASTER_KEYS": f"k1:{k1}", "AIC_ACTIVE_KEY_ID": "k1"},
    )
    assert again.returncode == 2
    assert "AGE-SECRET-KEY-1" not in again.stdout + again.stderr
