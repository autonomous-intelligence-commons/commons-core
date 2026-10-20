"""Offline recovery-key custody packages (ADR-0010, Backup & Recovery Stage 2).

Stage 1 (ADR-0009) proved that encrypted PostgreSQL backups can be produced,
published, and restored into disposable environments. Stage 2 answers the
question that pipeline deliberately left open: *where do the recovery keys
live so that AIC can survive the total loss of its original infrastructure
without leaking private secrets into source control, CI, logs, or any cloud
service?*

Two key domains — never mixed (Stage 2 spec §2)
==============================================

``backup`` domain
    age *identities* (``AGE-SECRET-KEY-1…``, private) and their public
    *recipients* (``age1…``). The backup writer only ever receives public
    recipients; identities exist solely inside encrypted recovery packages
    and in the owner's offline custody material.

``application`` domain
    The ``AIC_MASTER_KEYS`` key ring (``"kid:<64 hex>, …"``) plus
    ``AIC_ACTIVE_KEY_ID``. The backup workload never receives these keys;
    they are packaged here so a restored database remains decryptable after
    the original environment (including its configuration) is lost.

The recovery package
====================

A *recovery package* is a pair of files in an owner-chosen offline
directory:

``<generation>.recovery-metadata.json``
    Public, non-secret metadata: package format version, creation time,
    custody generation identifier, the *public* recipients of every packaged
    age identity and custody identity, application key *ids* (never key
    material), compatibility requirements, and integrity hashes binding the
    metadata to the encrypted secret section.

``<generation>.recovery-secrets.age``
    The secret section: a JSON payload (age identities + application key
    ring) encrypted with age to one or more *custody recipients* — dedicated
    public keys whose private identities are the root of the whole custody
    design. Custody identities are generated offline by
    :func:`create_recovery_package` (or supplied by the owner), written to a
    restrictive-permission file the owner immediately moves to independent
    offline storage, and never committed anywhere. The custody layer is
    intentionally symmetric with the backup layer (age X25519 AEAD), so a
    single well-understood tool protects both.

Custody root and trust anchor (honest threat model)
===================================================

* Encryption protects *confidentiality* of the secret section. It does NOT
  establish *authenticity*: an attacker who can replace both the package and
  its checksum defeats any hash alone. The metadata therefore records a
  ``custody_manifest_sha256`` — SHA-256 over the canonical metadata with the
  ``trust_anchor`` member removed — which the owner writes down in at least
  two places *independent of the package copies*;
  :func:`verify_recovery_package` checks it when ``expected_sha256`` is
  supplied and reports loudly when it is not.
* Integrity of the secret section is cryptographically enforced by age's
  AEAD plus two content hashes in the metadata (file-level ``sha256`` and
  plaintext-level ``payload_sha256``), so a secrets file swapped in from
  another package is detected even when the same custody identity can
  decrypt it.
* Availability comes from *redundancy*: :func:`duplicate_recovery_package`
  creates a second byte-identical copy for independent offline storage;
  losing either copy must not impair recovery. The custody identity itself
  needs its own independent copies (runbook: docs/RECOVERY_KEYS.md).

Safety rules implemented here (Stage 2 spec §6)
===============================================

* This module never generates or handles real production keys; tests use
  runtime-generated synthetic identities and keys only.
* Refuses to write into Git repositories/work trees, well-known cloud-sync
  folders, or onto existing packages — no silent overwrites, ever.
* Atomic publication: staged ``.part`` files are verified (SHA-256
  readback) then promoted with ``os.replace``; the metadata is written LAST
  so an interrupted creation is detectably incomplete.
* Private key material never reaches argv, logs, error messages, or
  tracebacks; identity files are created with 0600 permissions on POSIX.
* Windows caveat (honest): ``os.chmod`` cannot express POSIX permissions on
  NTFS. Creation prints an explicit warning on Windows and the runbook
  documents manual ``icacls`` hardening; the tool never pretends otherwise.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from commons_core.backup.destination import scrub_secrets
from commons_core.backup.manifest import utc_now
from commons_core.security.cryptography.errors import KeyConfigurationError
from commons_core.security.cryptography.key_store import parse_master_keys

#: Recovery-package metadata format understood by this module.
PACKAGE_FORMAT_VERSION = 1

#: Secret-section payload format understood by this module.
SECRETS_PAYLOAD_VERSION = 1

#: Package kind marker written into every metadata document.
PACKAGE_KIND = "aic-recovery-package"

#: File suffixes of the two package members (metadata is written LAST).
METADATA_SUFFIX = ".recovery-metadata.json"
SECRETS_SUFFIX = ".recovery-secrets.age"

#: Restrictive POSIX permissions for package material (owner-only).
_DIR_MODE = 0o700
_FILE_MODE = 0o600

#: Structural age material patterns (validated, never echoed). age key
#: material is Bech32, not Base32: the data part may contain 0/8/9 (and
#: never the digit 1); kept as permissive as the recipient pattern below.
_AGE_IDENTITY_RE = re.compile(r"^AGE-SECRET-KEY-1[A-Z0-9]{58}$")
_AGE_RECIPIENT_RE = re.compile(r"^age1[0-9a-z]{58}$")
_KEYGEN_RECIPIENT_LINE_RE = re.compile(r"^# public key:\s*(age1[0-9a-z]{58})\s*$", re.M)

#: Directory names that indicate a cloud-synced folder (matched
#: case-insensitively against every path component). Writing recovery
#: packages there would silently upload every secret to a third party.
_SYNC_DIR_NAMES = frozenset(
    {
        "dropbox",
        "onedrive",
        "googledrive",
        "google drive",
        "icloud",
        "iclouddrive",
        "icloud drive",
        "box sync",
        "megasync",
    }
)


class RecoveryKeyError(RuntimeError):
    """Base error for recovery-key custody operations (never carries secrets)."""


class UnsafeOutputError(RecoveryKeyError):
    """The requested output location is unsafe (repo, sync folder, non-empty dir)."""


class PackageExistsError(RecoveryKeyError):
    """Refusing to overwrite existing recovery material (no silent replacements)."""


class PackageFormatError(RecoveryKeyError):
    """The recovery package is malformed, unsupported, or internally inconsistent."""


class PackageVerificationError(RecoveryKeyError):
    """A verification check failed (integrity, claims, or decryption)."""


class CustodyToolError(RecoveryKeyError):
    """age/age-keygen could not be located or failed (operational detail only)."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgeIdentity:
    """One age identity pair.

    ``identity`` is PRIVATE material: it is never printed, logged, or
    included in error messages. ``recipient`` is the matching public key and
    is safe to expose in manifests and reports.
    """

    label: str
    identity: str
    recipient: str

    def public_dict(self) -> dict:
        """Non-secret projection safe for manifests and reports."""
        return {"label": self.label, "recipient": self.recipient}


@dataclass(frozen=True)
class PackageCreated:
    """Result of a successful package creation — carries no secret material."""

    package_dir: Path
    generation: str
    metadata_path: Path
    secrets_path: Path
    custody_manifest_sha256: str
    secrets_sha256: str
    custody_recipients: tuple[str, ...]
    packaged_recipients: tuple[str, ...]
    application_key_ids: tuple[str, ...]
    active_key_id: str | None
    custody_identity_path: Path | None


@dataclass(frozen=True)
class VerificationCheck:
    """One PASS/FAIL check in a verification report (no secret content)."""

    label: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class VerificationReport:
    """Machine-friendly verification result (content-blind by construction)."""

    package_dir: Path
    generation: str
    passed: bool
    checks: tuple[VerificationCheck, ...] = ()

    def render(self) -> str:
        lines = [f"[recovery-verify] package={self.package_dir} generation={self.generation}"]
        for check in self.checks:
            mark = "PASS" if check.passed else "FAIL"
            line = f"  {mark} {check.label}"
            if check.detail:
                line += f" — {check.detail}"
            lines.append(line)
        verdict = "RECOVERY-VERIFY-PASS" if self.passed else "RECOVERY-VERIFY-FAIL"
        lines.append(f"{verdict} generation={self.generation}")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(
            {
                "package_dir": str(self.package_dir),
                "generation": self.generation,
                "passed": self.passed,
                "checks": [
                    {"label": c.label, "passed": c.passed, "detail": c.detail} for c in self.checks
                ],
            },
            indent=2,
            sort_keys=True,
        )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def new_generation_id() -> str:
    """Fresh custody-generation identifier (UTC-stamped, collision-averse)."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"gen-{stamp}-{secrets.token_hex(4)}"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _resolve_age_tool(env_var: str, default_name: str) -> str:
    """Resolve one age tool binary (env override → PATH), fail-closed."""
    explicit = os.environ.get(env_var, "").strip()
    candidate = Path(explicit) if explicit else Path(shutil.which(default_name) or "")
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise CustodyToolError(
            f"{default_name} binary not found or not executable — set {env_var} "
            "to an explicit path (fail-closed)"
        )
    return str(candidate)


def locate_age_tools(*, require_keygen: bool = True) -> tuple[str, str | None]:
    """Resolve ``(age, age-keygen)`` using the Stage 1 environment variables.

    Reuses ``AIC_BACKUP_AGE_BIN`` / ``AIC_BACKUP_AGE_KEYGEN_BIN`` so operators
    configure one set of tool paths for backup and custody work alike.
    """
    age = _resolve_age_tool("AIC_BACKUP_AGE_BIN", "age")
    keygen = (
        _resolve_age_tool("AIC_BACKUP_AGE_KEYGEN_BIN", "age-keygen") if require_keygen else None
    )
    return age, keygen


def validate_recipient(recipient: str) -> str:
    """Validate a public age recipient (never accepts private identities)."""
    recipient = recipient.strip()
    if not _AGE_RECIPIENT_RE.fullmatch(recipient):
        raise PackageFormatError(
            "not a valid public age recipient (expected age1…); private "
            "identities are never accepted as recipients"
        )
    return recipient


def _identity_fingerprint(identity: str) -> str:
    """Stable, non-reversible identifier for an identity (sha256 of its text)."""
    return _sha256_bytes(identity.encode("utf-8"))


# ---------------------------------------------------------------------------
# Output-location safety
# ---------------------------------------------------------------------------


def ensure_safe_output_dir(out_dir: str | os.PathLike) -> Path:
    """Refuse unsafe destinations before anything is written.

    Rejects (with explicit reasons): directories inside a Git work tree,
    paths crossing well-known cloud-sync folders, the home directory itself,
    filesystem roots, non-empty directories, and directories that already
    hold recovery material (no silent overwrites).
    """
    target = Path(out_dir).expanduser().resolve()

    if target.exists():
        if not target.is_dir():
            raise UnsafeOutputError("output path exists and is not a directory")
        if any(target.iterdir()):
            raise PackageExistsError(
                "target directory is not empty — choose a dedicated fresh "
                "directory for recovery material (no silent overwrites)"
            )
    if target == Path.home().resolve():
        raise UnsafeOutputError("refusing to use the home directory itself")
    if target.parent == target:
        raise UnsafeOutputError("refusing to write to a filesystem root")

    for ancestor in (target, *target.parents):
        if (ancestor / ".git").exists():
            raise UnsafeOutputError(
                f"refusing to write inside a Git repository ({ancestor}) — "
                "recovery secrets must never be version-controlled"
            )

    lowered_parts = {part.lower() for part in target.parts}
    for sync_name in _SYNC_DIR_NAMES:
        if sync_name in lowered_parts:
            raise UnsafeOutputError(
                f"path runs through a cloud-sync folder ({sync_name!r}) — "
                "recovery secrets must not be auto-uploaded to a third party"
            )
    return target


def _atomic_write(path: Path, payload: bytes, *, mode: int = _FILE_MODE) -> None:
    """Write bytes atomically: 0600 staging file → SHA readback → os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    fd, staged_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".part", dir=str(path.parent)
    )
    staged = Path(staged_name)
    try:
        os.write(fd, payload)
        os.close(fd)
        os.chmod(staged, mode)
        with staged.open("rb") as handle:
            readback = handle.read()
        if readback != payload:
            raise OSError("readback mismatch while staging package member")
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Identity generation and parsing
# ---------------------------------------------------------------------------


def generate_age_identity(*, label: str, work_dir: Path, age_keygen: str) -> AgeIdentity:
    """Generate a fresh age identity via age-keygen (synthetic or offline-real).

    The identity file is created with 0600 permissions, read once, and
    deleted immediately — the material lives on only in memory (and later
    inside the encrypted secret section).
    """
    work_dir.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    fd, name = tempfile.mkstemp(prefix=".age-keygen-", suffix=".txt", dir=str(work_dir))
    os.close(fd)
    key_file = Path(name)
    try:
        key_file.unlink()  # age-keygen refuses existing outputs (O_EXCL); name reserved
        proc = subprocess.run(
            [age_keygen, "-o", str(key_file)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            raise CustodyToolError(
                f"age-keygen failed with status {proc.returncode} "
                f"({scrub_secrets(proc.stderr.strip() or 'no detail')})"
            )
        os.chmod(key_file, _FILE_MODE)
        identity, recipient = _parse_age_keygen_output(key_file, proc.stdout)
        return AgeIdentity(label=label, identity=identity, recipient=recipient)
    finally:
        key_file.unlink(missing_ok=True)


def _parse_age_keygen_output(key_file: Path, stdout: str) -> tuple[str, str]:
    """Parse age-keygen's private file + ``Public key:`` stdout line."""
    blob = key_file.read_text(encoding="utf-8")
    identity_lines = [
        line.strip() for line in blob.splitlines() if _AGE_IDENTITY_RE.fullmatch(line.strip())
    ]
    if len(identity_lines) != 1:
        raise CustodyToolError(
            "age-keygen output did not contain exactly one identity line (content suppressed)"
        )
    identity = identity_lines[0]
    match = _KEYGEN_RECIPIENT_LINE_RE.search(stdout) or _KEYGEN_RECIPIENT_LINE_RE.search(blob)
    if match is None:
        raise CustodyToolError(
            "age-keygen did not report the public recipient (content suppressed)"
        )
    return identity, match.group(1)


def read_age_identity_file(path: str | os.PathLike, *, label: str = "") -> AgeIdentity:
    """Read an existing age identity file (age-keygen layout, private + public).

    The file must contain the ``# public key: age1…`` comment so the metadata
    can record the matching recipient without deriving it. Errors never echo
    file content.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise PackageFormatError(f"age identity file not found: {file_path}")
    if os.name == "posix":
        try:
            mode = file_path.stat().st_mode & 0o777
            if mode & 0o077:
                sys.stderr.write(
                    f"[recovery] WARNING: identity file {file_path} is group/other-"
                    "readable (permissions too open)\n"
                )
        except OSError:
            pass
    blob = file_path.read_text(encoding="utf-8")
    identity_lines = [
        line.strip() for line in blob.splitlines() if _AGE_IDENTITY_RE.fullmatch(line.strip())
    ]
    match = _KEYGEN_RECIPIENT_LINE_RE.search(blob)
    if len(identity_lines) != 1 or match is None:
        raise PackageFormatError(
            f"identity file {file_path} is not a valid age-keygen file (needs "
            "the identity line and the '# public key:' comment; content suppressed)"
        )
    return AgeIdentity(
        label=label or file_path.stem, identity=identity_lines[0], recipient=match.group(1)
    )


def _require_age_bin(age_bin: str | None) -> str:
    if age_bin:
        return age_bin
    age, _ = locate_age_tools(require_keygen=False)
    return age


# ---------------------------------------------------------------------------
# Package creation
# ---------------------------------------------------------------------------


def custody_manifest_sha256(metadata: dict) -> str:
    """SHA-256 over the canonical metadata with the ``trust_anchor`` removed.

    The owner records this value in at least two places independent of the
    package copies; ``verify_recovery_package(expected_sha256=…)`` compares
    against it. A hash recorded *next to* a copy does not defend against
    replacement of that copy — the runbook says so explicitly.
    """
    canonical = {key: value for key, value in metadata.items() if key != "trust_anchor"}
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(blob)


def create_recovery_package(
    out_dir: str | os.PathLike,
    *,
    age_identities: Sequence[AgeIdentity] | None = None,
    generate_operational_identity: bool = False,
    operational_identity_label: str = "operational",
    custody_identities: Sequence[AgeIdentity] | None = None,
    custody_recipients_only: Sequence[str] | None = None,
    generate_custody_identity: bool = True,
    master_keys: str | None = None,
    active_key_id: str | None = None,
    app_commit: str | None = None,
    alembic_revision: str | None = None,
    age_bin: str | None = None,
    age_keygen_bin: str | None = None,
    generation: str | None = None,
) -> PackageCreated:
    """Create a recovery package (metadata + encrypted secret section).

    Either supply ``age_identities`` (existing pairs, e.g. read from
    age-keygen files) or pass ``generate_operational_identity=True`` to
    create a fresh one. The secret section is encrypted to the custody
    layer: supplied ``custody_identities``, extra public
    ``custody_recipients_only``, and/or a freshly generated custody identity
    (default). The generated custody identity is written to a 0600 file
    inside ``out_dir`` that the owner must immediately move to independent
    offline storage — the verifier fails loudly while it sits there.

    ``master_keys`` uses the exact ``AIC_MASTER_KEYS`` format and is
    validated through the application's own parser (no reimplementation);
    ``active_key_id`` must be one of its key ids.
    """
    target = ensure_safe_output_dir(out_dir)
    age = _require_age_bin(age_bin)
    keygen = age_keygen_bin or locate_age_tools(require_keygen=True)[1]
    if keygen is None:
        raise CustodyToolError("age-keygen is required to generate identities")

    generation = generation or new_generation_id()
    staging = target / ".staging"
    try:
        staging.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    except OSError as exc:
        raise CustodyToolError(f"cannot write recovery material: {exc}") from exc

    identities: list[AgeIdentity] = list(age_identities or [])
    if generate_operational_identity:
        identities.append(
            generate_age_identity(
                label=operational_identity_label, work_dir=staging, age_keygen=keygen
            )
        )
    if not identities:
        raise PackageFormatError(
            "no age identities to package — supply existing identities or pass "
            "generate_operational_identity=True"
        )
    seen_labels: set[str] = set()
    seen_recipients: set[str] = set()
    for entry in identities:
        validate_recipient(entry.recipient)
        if entry.label in seen_labels:
            raise PackageFormatError(f"duplicate identity label {entry.label!r}")
        if entry.recipient in seen_recipients:
            raise PackageFormatError("the same age recipient was packaged twice")
        seen_labels.add(entry.label)
        seen_recipients.add(entry.recipient)

    custody: list[AgeIdentity] = list(custody_identities or [])
    generated_custody: AgeIdentity | None = None
    if generate_custody_identity:
        generated_custody = generate_age_identity(
            label="custody-primary", work_dir=staging, age_keygen=keygen
        )
        custody.append(generated_custody)
    extra_recipients = [validate_recipient(r) for r in (custody_recipients_only or [])]
    custody_recipients: list[str] = []
    for entry in custody:
        if entry.recipient not in custody_recipients:
            custody_recipients.append(entry.recipient)
    for recipient in extra_recipients:
        if recipient not in custody_recipients:
            custody_recipients.append(recipient)
    if not custody_recipients:
        raise PackageFormatError(
            "no custody recipients — the secret section must be encrypted to "
            "at least one offline custody identity"
        )

    application_key_ids: list[str] = []
    if master_keys is not None:
        try:
            key_ring = parse_master_keys(master_keys)
        except KeyConfigurationError as exc:
            raise PackageFormatError(f"invalid application master keys: {exc}") from exc
        application_key_ids = sorted(key_ring)
        if not active_key_id:
            raise PackageFormatError("active_key_id is required when application keys are packaged")
        if active_key_id not in key_ring:
            raise PackageFormatError(
                f"active key id {active_key_id!r} is not part of the supplied key ring"
            )
    elif active_key_id is not None:
        raise PackageFormatError(
            "active_key_id supplied without master keys — package the key ring too"
        )

    payload: dict = {
        "payload_version": SECRETS_PAYLOAD_VERSION,
        "package_kind": PACKAGE_KIND,
        "custody_generation": generation,
        "created_at_utc": utc_now(),
        "backup_domain": {
            "age_identities": [
                {
                    "label": entry.label,
                    "identity": entry.identity,
                    "recipient": entry.recipient,
                }
                for entry in identities
            ],
        },
        "application_domain": (
            {"master_keys": master_keys, "active_key_id": active_key_id}
            if master_keys is not None
            else None
        ),
    }
    payload_bytes = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    payload_sha256 = _sha256_bytes(payload_bytes)

    secrets_path = target / f"{generation}{SECRETS_SUFFIX}"
    argv = [age]
    for recipient in custody_recipients:
        argv.extend(["-r", recipient])
    argv.extend(["-o", str(secrets_path) + ".part"])
    proc = subprocess.run(argv, input=payload_bytes, capture_output=True, timeout=600, check=False)
    staged_secrets = Path(str(secrets_path) + ".part")
    try:
        if proc.returncode != 0:
            raise CustodyToolError(
                f"age failed with status {proc.returncode} while sealing the "
                f"secret section ({scrub_secrets(proc.stderr.decode('utf-8', 'replace').strip())})"
            )
        if not staged_secrets.is_file():
            raise CustodyToolError("age produced no secret section file")
        os.chmod(staged_secrets, _FILE_MODE)
        with staged_secrets.open("rb") as handle:
            if handle.read() != staged_secrets.read_bytes():
                raise CustodyToolError("staged secret section readback mismatch")
        secrets_blob = staged_secrets.read_bytes()
        secrets_sha256 = _sha256_bytes(secrets_blob)
        os.replace(staged_secrets, secrets_path)
    except OSError as exc:
        raise CustodyToolError(f"cannot write recovery material: {exc}") from exc
    finally:
        staged_secrets.unlink(missing_ok=True)

    metadata: dict = {
        "package_version": PACKAGE_FORMAT_VERSION,
        "package_kind": PACKAGE_KIND,
        "custody_generation": generation,
        "created_at_utc": utc_now(),
        "custody": {
            "recipients": custody_recipients,
            "identity_fingerprints": {
                entry.recipient: _identity_fingerprint(entry.identity) for entry in custody
            },
        },
        "backup_domain": {
            "age_identities": [entry.public_dict() for entry in identities],
        },
        "application_domain": (
            {"key_ids": application_key_ids, "active_key_id": active_key_id}
            if master_keys is not None
            else None
        ),
        "compatibility": {
            "app_commit": app_commit,
            "alembic_revision": alembic_revision,
            "requires": {
                "age": "v1.3.2 (CI pin); any age >= v1.0.0 can decrypt",
                "pg_client_major": ">= source server major (18 for Railway PG 18.6)",
                "python": ">= 3.12",
            },
        },
        "secret_section": {
            "filename": secrets_path.name,
            "encryption": "age-v1 X25519+ChaCha20-Poly1305 (AEAD), custody recipients",
            "sha256": secrets_sha256,
            "size_bytes": len(secrets_blob),
            "payload_sha256": payload_sha256,
        },
    }
    metadata["trust_anchor"] = {
        "kind": "independently-recorded-sha256",
        "custody_manifest_sha256": custody_manifest_sha256(metadata),
        "note": (
            "Record this SHA-256 in at least two places independent of both "
            "package copies; a hash stored next to a copy does not defend "
            "against replacement of that copy."
        ),
    }
    metadata_blob = json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8")
    metadata_path = target / f"{generation}{METADATA_SUFFIX}"

    custody_identity_path: Path | None = None
    if generated_custody is not None:
        custody_identity_path = target / f"age-identity-custody-{generation}.txt"
        identity_blob = (
            f"# created: {utc_now()}\n"
            f"# public key: {generated_custody.recipient}\n"
            f"{generated_custody.identity}\n"
        ).encode()
        try:
            _atomic_write(custody_identity_path, identity_blob)
        except OSError as exc:
            raise CustodyToolError(f"cannot write recovery material: {exc}") from exc

    try:
        _atomic_write(metadata_path, metadata_blob)  # metadata LAST
    except OSError as exc:
        raise CustodyToolError(f"cannot write recovery material: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    if os.name == "nt":
        sys.stderr.write(
            "[recovery] WARNING: Windows does not honor POSIX 0600 permissions; "
            "restrict the package directory manually (see docs/RECOVERY_KEYS.md)\n"
        )

    return PackageCreated(
        package_dir=target,
        generation=generation,
        metadata_path=metadata_path,
        secrets_path=secrets_path,
        custody_manifest_sha256=metadata["trust_anchor"]["custody_manifest_sha256"],
        secrets_sha256=secrets_sha256,
        custody_recipients=tuple(custody_recipients),
        packaged_recipients=tuple(entry.recipient for entry in identities),
        application_key_ids=tuple(application_key_ids),
        active_key_id=active_key_id,
        custody_identity_path=custody_identity_path,
    )


# ---------------------------------------------------------------------------
# Verification, unlock, exports, duplication
# ---------------------------------------------------------------------------


def _load_metadata(package_dir: Path) -> tuple[dict, Path]:
    matches = sorted(package_dir.glob(f"*{METADATA_SUFFIX}"))
    if len(matches) != 1:
        raise PackageFormatError(
            f"expected exactly one {METADATA_SUFFIX} file in {package_dir} "
            f"(found {len(matches)}) — incomplete or foreign directory"
        )
    try:
        metadata = json.loads(matches[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageFormatError(f"metadata unreadable or not JSON: {exc}") from exc
    if not isinstance(metadata, dict):
        raise PackageFormatError("metadata is not a JSON object")
    if metadata.get("package_kind") != PACKAGE_KIND:
        raise PackageFormatError("metadata package_kind mismatch — not a recovery package")
    found = metadata.get("package_version")
    if found != PACKAGE_FORMAT_VERSION:
        raise PackageFormatError(
            f"unsupported recovery package format version {found!r} "
            f"(this tool understands {PACKAGE_FORMAT_VERSION})"
        )
    return metadata, matches[0]


def verify_recovery_package(
    package_dir: str | os.PathLike,
    *,
    expected_sha256: str | None = None,
    compare_dir: str | os.PathLike | None = None,
    identity_file: str | os.PathLike | None = None,
    check_decrypt: bool = False,
    rotation_manifest_path: str | os.PathLike | None = None,
    age_bin: str | None = None,
) -> VerificationReport:
    """Verify a recovery package without ever printing secret material.

    Checks (each reported PASS/FAIL): metadata present/parsable/kind/version;
    secret section present with matching SHA-256 and size; independently
    recorded trust-anchor hash (when supplied); custody identity hygiene
    (no identity files sitting in the package directory); byte-identity with
    a second copy (when ``compare_dir`` given); decryption + payload claims
    (when ``identity_file`` given); rotation readiness against a backup
    manifest (when ``rotation_manifest_path`` given).
    """
    pkg = Path(package_dir)
    checks: list[VerificationCheck] = []

    def add(label: str, passed: bool, detail: str = "") -> bool:
        checks.append(VerificationCheck(label=label, passed=passed, detail=detail))
        return passed

    generation = ""
    try:
        metadata, metadata_path = _load_metadata(pkg)
        generation = str(metadata.get("custody_generation") or "")
        add("metadata-parse", True)
    except PackageFormatError as exc:
        add("metadata-parse", False, str(exc))
        return VerificationReport(pkg, generation, False, tuple(checks))

    section = metadata.get("secret_section") or {}
    secrets_name = str(section.get("filename") or "")
    secrets_path = pkg / secrets_name
    if not secrets_name or not secrets_path.is_file():
        add("secrets-present", False, f"{secrets_name or '<unnamed>'} missing")
    else:
        add("secrets-present", True)
        blob = secrets_path.read_bytes()
        sha_ok = _sha256_bytes(blob) == str(section.get("sha256") or "")
        add(
            "secrets-sha256",
            sha_ok,
            "" if sha_ok else "SHA-256 mismatch — file corrupted or replaced",
        )
        size_ok = len(blob) == int(section.get("size_bytes") or -1)
        add(
            "secrets-size",
            size_ok,
            "" if size_ok else "size mismatch — file truncated or replaced",
        )

    recomputed_anchor = custody_manifest_sha256(metadata)
    if expected_sha256 is not None:
        anchor_ok = expected_sha256.strip().lower() == recomputed_anchor
        add(
            "trust-anchor-sha256",
            anchor_ok,
            ""
            if anchor_ok
            else "does not match the independently recorded value — package may have been replaced",
        )
    else:
        add(
            "trust-anchor-note",
            True,
            "authenticity NOT verified (no expected hash supplied) — record "
            f"custody_manifest_sha256={recomputed_anchor} and re-verify",
        )

    stray_identities = list(pkg.glob("age-identity*.txt"))
    add(
        "no-identity-in-package-dir",
        not stray_identities,
        ""
        if not stray_identities
        else "custody identity file found next to the package — MOVE it to independent offline storage",
    )

    if compare_dir is not None:
        other = Path(compare_dir)
        same = True
        detail = "byte-identical"
        for meta_file in pkg.glob(f"*{METADATA_SUFFIX}"):
            twin = other / meta_file.name
            if not twin.is_file() or twin.read_bytes() != meta_file.read_bytes():
                same = False
                detail = f"{meta_file.name} differs or is missing in the other copy"
        for secrets_file in pkg.glob(f"*{SECRETS_SUFFIX}"):
            twin = other / secrets_file.name
            if not twin.is_file() or twin.read_bytes() != secrets_file.read_bytes():
                same = False
                detail = f"{secrets_file.name} differs or is missing in the other copy"
        add("copy-identical", same, detail)

    if check_decrypt or identity_file is not None:
        if identity_file is None:
            add(
                "secrets-decrypt",
                False,
                "--check-decrypt requires a custody identity file",
            )
        else:
            try:
                payload, _ = unlock_recovery_package(
                    pkg, identity_file, age_bin=age_bin, _metadata=metadata
                )
                add("secrets-decrypt", True)
                add("payload-parse", True)
                add(
                    "payload-version",
                    payload.get("payload_version") == SECRETS_PAYLOAD_VERSION,
                )
                blob = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
                binding_ok = _sha256_bytes(blob) == str(section.get("payload_sha256") or "")
                add(
                    "payload-sha256",
                    binding_ok,
                    ""
                    if binding_ok
                    else "decrypted payload does not match the metadata's binding "
                    "(possible section swap)",
                )
                add(
                    "generation-consistent",
                    payload.get("custody_generation") == generation,
                    ""
                    if payload.get("custody_generation") == generation
                    else "payload belongs to a different generation",
                )
                claims_ok, claims_detail = _claims_consistent(metadata, payload)
                add("claims-consistent", claims_ok, claims_detail)
            except (PackageVerificationError, PackageFormatError, CustodyToolError) as exc:
                add("secrets-decrypt", False, str(exc))

    if rotation_manifest_path is not None:
        try:
            backup_manifest = json.loads(Path(rotation_manifest_path).read_text("utf-8"))
            artifact = backup_manifest.get("artifact") or {}
            backup_recipients = set(artifact.get("recipients") or [artifact.get("recipient")])
            backup_recipients.discard(None)
            package_recipients = {
                entry.get("recipient")
                for entry in (metadata.get("backup_domain") or {}).get("age_identities") or []
            }
            overlap = backup_recipients & package_recipients
            add(
                "rotation-ready",
                bool(overlap),
                "backup is encrypted for recipients NOT in this package — restore would fail"
                if not overlap
                else f"covered recipients: {sorted(overlap)}",
            )
        except (OSError, json.JSONDecodeError) as exc:
            add("rotation-ready", False, f"backup manifest unreadable: {exc}")

    try:
        locate_age_tools(require_keygen=False)
        add("dependency-age", True, "age binary resolvable")
    except CustodyToolError as exc:
        add("dependency-age", False, str(exc))

    passed = all(check.passed for check in checks)
    return VerificationReport(pkg, generation, passed, tuple(checks))


def _claims_consistent(metadata: dict, payload: dict) -> tuple[bool, str]:
    """Cross-check the metadata's public claims against the decrypted payload."""
    meta_backup = metadata.get("backup_domain") or {}
    meta_ids = {entry.get("recipient") for entry in meta_backup.get("age_identities") or []}
    payload_backup = payload.get("backup_domain") or {}
    payload_ids = {entry.get("recipient") for entry in payload_backup.get("age_identities") or []}
    if meta_ids != payload_ids:
        return False, "packaged identity recipients differ from metadata claims"
    meta_app = metadata.get("application_domain")
    payload_app = payload.get("application_domain")
    if (meta_app is None) != (payload_app is None):
        return False, "application domain presence differs between metadata and payload"
    if meta_app is not None and payload_app is not None:
        ring = parse_master_keys(str(payload_app.get("master_keys") or ""))
        if sorted(ring) != list(meta_app.get("key_ids") or []):
            return False, "application key ids differ from metadata claims"
        if payload_app.get("active_key_id") != meta_app.get("active_key_id"):
            return False, "active key id differs from metadata claim"
    return True, "metadata claims match the decrypted payload"


def unlock_recovery_package(
    package_dir: str | os.PathLike,
    identity_file: str | os.PathLike,
    *,
    age_bin: str | None = None,
    _metadata: dict | None = None,
) -> tuple[dict, bytes]:
    """Decrypt and parse the secret section with a custody identity.

    Returns ``(payload_dict, payload_bytes)``. Raises
    :class:`PackageVerificationError` with an explicit, secret-free reason
    on any failure. ``payload_bytes`` is the exact decrypted byte stream
    (needed for the payload SHA-256 binding).
    """
    pkg = Path(package_dir)
    metadata = _metadata if _metadata is not None else _load_metadata(pkg)[0]
    section = metadata.get("secret_section") or {}
    secrets_path = pkg / str(section.get("filename") or "")
    if not secrets_path.is_file():
        raise PackageVerificationError("secret section file is missing")
    blob = secrets_path.read_bytes()
    if _sha256_bytes(blob) != str(section.get("sha256") or ""):
        raise PackageVerificationError(
            "secret section SHA-256 mismatch — corrupted, truncated, or replaced"
        )
    age = _require_age_bin(age_bin)
    proc = subprocess.run(
        [age, "-d", "-i", str(Path(identity_file)), str(secrets_path)],
        capture_output=True,
        timeout=600,
        check=False,
    )
    if proc.returncode != 0:
        raise PackageVerificationError(
            "decryption failed — wrong custody identity, corrupted secret "
            f"section, or unsupported age format ({scrub_secrets(proc.stderr.decode('utf-8', 'replace').strip())})"
        )
    payload_bytes = proc.stdout
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageVerificationError(f"decrypted payload is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("package_kind") != PACKAGE_KIND:
        raise PackageVerificationError("decrypted payload is not a recovery secret section")
    expected_binding = str(section.get("payload_sha256") or "")
    canonical = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    if _sha256_bytes(canonical) != expected_binding:
        raise PackageVerificationError(
            "payload SHA-256 does not match the metadata binding — secrets "
            "section may have been swapped between packages"
        )
    return payload, canonical


def export_age_identity(payload: dict, label: str, out_path: str | os.PathLike) -> Path:
    """Export one packaged age identity to a 0600 file (refuses overwrite)."""
    entries = (payload.get("backup_domain") or {}).get("age_identities") or []
    match = next((entry for entry in entries if entry.get("label") == label), None)
    if match is None:
        raise PackageFormatError(
            f"no packaged age identity with label {label!r} "
            f"(available: {[e.get('label') for e in entries]})"
        )
    target = Path(out_path)
    if target.exists():
        raise PackageExistsError(f"refusing to overwrite {target}")
    identity = str(match.get("identity") or "")
    if not _AGE_IDENTITY_RE.fullmatch(identity):
        raise PackageFormatError("packaged identity is malformed (content suppressed)")
    blob = (f"# public key: {match.get('recipient')}\n{identity}\n").encode()
    try:
        _atomic_write(target, blob)
    except OSError as exc:
        raise CustodyToolError(f"cannot write recovery material: {exc}") from exc
    return target


def export_application_env(payload: dict, out_path: str | os.PathLike) -> Path:
    """Export AIC_MASTER_KEYS / AIC_ACTIVE_KEY_ID to a 0600 env file."""
    app = payload.get("application_domain")
    if not app:
        raise PackageFormatError("this package carries no application keys (backup-domain-only)")
    master_keys = str(app.get("master_keys") or "")
    parse_master_keys(master_keys)  # fail-closed validation before writing
    target = Path(out_path)
    if target.exists():
        raise PackageExistsError(f"refusing to overwrite {target}")
    blob = (
        f"AIC_MASTER_KEYS={master_keys}\nAIC_ACTIVE_KEY_ID={app.get('active_key_id')}\n"
    ).encode()
    try:
        _atomic_write(target, blob)
    except OSError as exc:
        raise CustodyToolError(f"cannot write recovery material: {exc}") from exc
    return target


def duplicate_recovery_package(source_dir: str | os.PathLike, out_dir: str | os.PathLike) -> Path:
    """Create a second byte-identical copy of a package (redundancy).

    Verifies the source metadata first, refuses unsafe destinations, writes
    both members atomically with SHA readback, and re-verifies the copy.
    """
    source = Path(source_dir)
    _load_metadata(source)  # fail-closed: refuse to duplicate junk
    target = ensure_safe_output_dir(out_dir)
    for member in sorted(source.iterdir()):
        if member.name.startswith(".") or member.name.endswith(".part"):
            continue
        if member.name.endswith(METADATA_SUFFIX) or member.name.endswith(SECRETS_SUFFIX):
            try:
                _atomic_write(target / member.name, member.read_bytes())
            except OSError as exc:
                raise CustodyToolError(f"cannot write recovery material: {exc}") from exc
    _load_metadata(target)
    return target
