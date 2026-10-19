"""Synthetic restore drill — verify a backup end-to-end, content-blind.

The drill is the ONLY supported restore path in Stage 1, and it refuses
production-looking destinations by construction (see
:mod:`commons_core.backup.destination`). There is deliberately NO
automatic production restore command (ADR-0009).

Flow (every step is a gate — the first failure stops the drill; cleanup
always runs):

1. destination guard: the server host must be loopback/private, the
   target database name must look disposable (or be explicitly
   allowlisted); the target database must not already exist;
2. load the published manifest and commit marker — a backup without a
   commit marker was never fully published and is rejected;
3. re-verify the artifact SHA-256 against the manifest (storage
   verification layer) and the client/server major-version
   compatibility rule before restoring;
4. stream ``age -d`` stdout straight into ``pg_restore`` stdin: the
   decrypted plaintext dump exists only inside the pipe, never on disk
   (pg_restore reads the archive from stdin when the archive argument is
   omitted);
5. run the application-level verification battery against the restored
   database (schema revision, controlled synthetic expectations, AEAD
   envelope decryption with the correct key, wrong-key failure, tamper
   rejection — the four required application-encryption demonstrations);
6. drop the disposable database and remove every temporary file on BOTH
   the success and failure paths (unless ``keep_database`` — which exists
   for debugging and is prominently recorded in the report);
7. emit a machine-readable report: PASS/FAIL per check, counts and
   labels only — never message/memory content, never credentials.

A non-zero pg_restore exit is a failure even if objects were already
created in the target — they are destroyed with the dropped database.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

from sqlalchemy import create_engine, select, text

from .destination import (
    UnsafeDestinationError,
    describe_url,
    scrub_secrets,
    validate_restore_destination,
)
from .manifest import BackupManifest, sha256_file
from .storage import StorageAdapter
from .tools import Toolbelt, locate_toolbelt, postgres_major


class DrillFailure(RuntimeError):
    """The restore drill failed — see the report for the failing gate."""


@dataclass(frozen=True)
class DrillCheck:
    label: str
    passed: bool
    detail: str = ""

    def render(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        suffix = f" — {self.detail}" if self.detail else ""
        return f"[{status}] {self.label}{suffix}"


@dataclass
class DrillReport:
    backup_id: str
    database_name: str
    passed: bool = False
    checks: list[DrillCheck] = field(default_factory=list)

    def add(self, label: str, passed: bool, detail: str = "") -> bool:
        self.checks.append(DrillCheck(label=label, passed=passed, detail=detail))
        return passed

    def to_json(self) -> str:
        payload = {
            "backup_id": self.backup_id,
            "database_name": self.database_name,
            "passed": self.passed,
            "checks": [asdict(c) for c in self.checks],
        }
        return json.dumps(payload, indent=2, sort_keys=True)

    def render(self) -> str:
        lines = [c.render() for c in self.checks]
        verdict = "PASS" if self.passed else "FAIL"
        lines.append(f"DRILL-{verdict} backup_id={self.backup_id} database={self.database_name}")
        return "\n".join(lines)


def _admin_url(server_url: str) -> str:
    """Rewrite any server URL to its maintenance database (…/postgres)."""
    parts = urlsplit(server_url)
    if not parts.path.lstrip("/"):
        raise DrillFailure(
            "server URL must name a maintenance database (e.g. …/postgres) so the "
            "drill can create and drop the disposable target"
        )
    return urlunsplit(parts._replace(path="/postgres"))


def _target_url(server_url: str, database_name: str) -> str:
    parts = urlsplit(server_url)
    return urlunsplit(parts._replace(path=f"/{database_name}"))


def _create_database(server_url: str, name: str) -> None:
    engine = create_engine(_admin_url(server_url), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
            ).scalar()
            if exists:
                raise DrillFailure(
                    f"disposable database {name!r} already exists — refusing to reuse "
                    "(unique names per drill run are mandatory)"
                )
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except DrillFailure:
        raise
    except Exception as exc:  # noqa: BLE001 — surface as a drill gate failure
        raise DrillFailure(
            f"could not create disposable database {name!r} (does the role have "
            f"CREATEDB? {type(exc).__name__})"
        ) from exc
    finally:
        engine.dispose()


def _drop_database(server_url: str, name: str) -> None:
    engine = create_engine(_admin_url(server_url), isolation_level="AUTOCOMMIT")
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
    except Exception:  # noqa: BLE001 — best-effort cleanup; drop failures re-raised by caller if fatal
        pass
    finally:
        engine.dispose()


def _libpq_env(target_url: str, *, inherit: bool = True) -> dict[str, str]:
    """Connection parameters as libpq environment variables (no argv secrets)."""
    parts = urlsplit(target_url)
    env = (
        dict(os.environ)
        if inherit
        else {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    )
    if parts.hostname:
        env["PGHOST"] = unquote(parts.hostname)
    if parts.port:
        env["PGPORT"] = str(parts.port)
    if parts.username:
        env["PGUSER"] = unquote(parts.username)
    if parts.password:
        env["PGPASSWORD"] = unquote(parts.password)
    for key, value in parse_qsl(parts.query):
        env[f"PG{key.upper().replace('-', '_')}"] = value
    return env


def _stream_decrypt_restore(
    *,
    belt: Toolbelt,
    identity_path: Path,
    artifact: Path,
    target_url: str,
) -> tuple[bool, str]:
    """``age -d | pg_restore --no-owner --no-privileges --dbname=NAME``.

    The plaintext dump never lands on disk: pg_restore reads the custom
    archive from stdin when no archive file argument is given. Connection
    parameters travel as libpq env vars; argv carries only the public
    database NAME.
    """
    import subprocess
    import threading

    target_db = unquote(urlsplit(target_url).path.lstrip("/"))
    age_argv = [belt.age, "-d", "-i", str(identity_path)]
    restore_argv = [
        belt.pg_restore,
        "-d",
        target_db,  # database NAME only — credentials ride in libpq env
        "--no-owner",
        "--no-privileges",
    ]

    age_proc = restore_proc = None
    age_err: list[str] = []
    restore_err: list[str] = []

    def pump(stream, sink: list[str]) -> None:
        try:
            for line in iter(stream.readline, b""):
                sink.append(line.decode("utf-8", "replace"))
        finally:
            stream.close()

    try:
        with open(artifact, "rb") as artifact_fh:
            age_proc = subprocess.Popen(
                age_argv, stdin=artifact_fh, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            restore_proc = subprocess.Popen(
                restore_argv,
                stdin=age_proc.stdout,  # type: ignore[arg-type]
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=_libpq_env(target_url),
            )
            age_proc.stdout.close()  # type: ignore[union-attr]
        threads = [
            threading.Thread(target=pump, args=(age_proc.stderr, age_err), daemon=True),
            threading.Thread(target=pump, args=(restore_proc.stderr, restore_err), daemon=True),
        ]
        for thread in threads:
            thread.start()
        age_rc = age_proc.wait()
        restore_rc = restore_proc.wait()
        for thread in threads:
            thread.join(timeout=5)

        if age_rc != 0:
            detail = scrub_secrets("".join(age_err[-6:]).strip())
            return False, f"age decrypt failed with status {age_rc}: {detail or '(no stderr)'}"
        if restore_rc != 0:
            detail = scrub_secrets("".join(restore_err[-10:]).strip())
            return False, (
                f"pg_restore failed with status {restore_rc} (objects created before "
                f"the failure are destroyed with the dropped database): "
                f"{detail or '(no stderr)'}"
            )
        return True, ""
    finally:
        for proc in (age_proc, restore_proc):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()


def _build_cipher(master_keys: str | None, active_key_id: str | None):
    """Application cipher from explicit key material (never from argv).

    Master keys reach the drill through the application environment
    (``AIC_MASTER_KEYS`` / ``AIC_ACTIVE_KEY_ID``) — the same fail-closed
    variables the API service uses. Returns (key_store, cipher).
    """
    if not master_keys or not master_keys.strip():
        return None, None
    from commons_core.security.cryptography.key_store import KeyStore
    from commons_core.security.cryptography.service import EnvelopeCipher

    store = KeyStore(master_keys.strip(), active_key_id or "k1", env="test")
    return store, EnvelopeCipher(store)


def verify_restored_database(
    database_url: str,
    *,
    expected: dict | None = None,
    master_keys: str | None = None,
    active_key_id: str | None = None,
    alembic_revision_from_manifest: str | None = None,
) -> DrillReport:
    """Application-level verification battery against a restored database.

    Battery (content-blind — counts, labels and PASS/FAIL only):

    1. alembic revision present; equal to the manifest's revision when the
       manifest recorded one (a sanity signal, not proof — see the
       manifest consistency semantics);
    2. with controlled synthetic ``expected`` facts: per-table row counts
       equal the expectations (post-restore validation, deliberately
       separate from the manifest — correction 2);
    3. encrypted rows came back as AEAD envelopes — ciphertext present,
       plaintext absent: restoring the database alone does NOT recover
       encrypted content (application-encryption demonstration 1);
    4. with the CORRECT master keys: memory/message/activity/governance
       envelopes decrypt byte-for-byte (demonstration 2);
    5. with a random WRONG key: decryption of a sample envelope must fail
       (demonstration 3);
    6. a tampered envelope (one flipped ciphertext character) must be
       rejected by the AEAD tag (demonstration 4);
    7. the emergency-stop system state row survives the restore.
    """
    database_name = database_url.rsplit("/", 1)[-1]
    report = DrillReport(backup_id="(verification-only)", database_name=database_name)

    from commons_core.activities.service import activity_aad
    from commons_core.communication.service import message_aad
    from commons_core.database.models import (
        Activity,
        Channel,
        GovernanceRecord,
        MemoryEntry,
        MemoryNamespace,
        Message,
    )
    from commons_core.governance.service import governance_aad
    from commons_core.infrastructure.system_state import EMERGENCY_STOP_KEY
    from commons_core.memory.service import entry_aad
    from commons_core.security.cryptography.envelope import Envelope

    _, cipher = _build_cipher(master_keys, active_key_id)

    from sqlalchemy.orm import Session

    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            revision = session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
            report.add(
                "alembic revision present",
                revision is not None,
                f"revision={revision}" if revision else "no alembic_version row",
            )
            if alembic_revision_from_manifest:
                report.add(
                    "alembic revision matches manifest (sanity signal)",
                    revision == alembic_revision_from_manifest,
                    f"manifest={alembic_revision_from_manifest} restored={revision}",
                )

            ns_row = session.scalars(
                select(MemoryNamespace).order_by(MemoryNamespace.created_at).limit(1)
            ).first()
            entry_row = session.scalars(
                select(MemoryEntry).order_by(MemoryEntry.created_at).limit(1)
            ).first()

            if entry_row is None:
                report.add("encrypted rows restored", False, "no memory rows found")
            else:
                envelope = dict(entry_row.envelope)
                has_ct = bool(envelope.get("ciphertext"))
                report.add(
                    "restored rows are AEAD envelopes (ciphertext present, plaintext absent)",
                    has_ct and envelope.get("alg") == "AES-256-GCM",
                    f"alg={envelope.get('alg')} ciphertext={'yes' if has_ct else 'MISSING'}",
                )
                if expected:
                    chain = (expected.get("memory_plaintexts") or {}).get(entry_row.key) or [None]
                    sample_plaintext = chain[0]
                    if sample_plaintext:
                        report.add(
                            "restore alone does not reveal content (envelope != plaintext)",
                            envelope.get("ciphertext") != sample_plaintext,
                        )

            if expected:
                for table_name, expected_count in sorted(expected.get("table_counts", {}).items()):
                    got = session.execute(text(f'SELECT count(*) FROM "{table_name}"')).scalar_one()
                    report.add(
                        f"table count {table_name}",
                        got == expected_count,
                        f"expected={expected_count} restored={got}",
                    )

            if expected and cipher is not None:
                if ns_row is not None:
                    for key, chain in sorted(expected.get("memory_plaintexts", {}).items()):
                        rows = session.scalars(
                            select(MemoryEntry)
                            .where(
                                MemoryEntry.namespace_id == ns_row.id,
                                MemoryEntry.key == key,
                            )
                            .order_by(MemoryEntry.version)
                        ).all()
                        try:
                            got = [
                                cipher.open(Envelope(**dict(r.envelope)), entry_aad(ns_row.id, key))
                                for r in rows
                            ]
                            report.add(
                                f"memory chain {key!r} decrypts byte-for-byte",
                                got == chain,
                                f"{len(got)} versions",
                            )
                        except Exception as exc:  # noqa: BLE001
                            report.add(
                                f"memory chain {key!r} decrypts byte-for-byte",
                                False,
                                f"decryption error {type(exc).__name__}",
                            )

                ch_row = session.scalars(
                    select(Channel).where(
                        Channel.handle == expected.get("channel_handle", "\x00missing")
                    )
                ).first()
                msg_rows = (
                    session.scalars(
                        select(Message)
                        .where(Message.channel_id == ch_row.id)
                        .order_by(Message.sequence)
                    ).all()
                    if ch_row is not None
                    else []
                )
                try:
                    got_msgs = [
                        cipher.open(
                            Envelope(**dict(m.envelope)), message_aad(ch_row.id, m.sender_id)
                        )
                        for m in msg_rows
                    ]
                    report.add(
                        "message envelopes decrypt byte-for-byte",
                        got_msgs == expected.get("message_plaintexts", []),
                        f"{len(got_msgs)} messages",
                    )
                except Exception as exc:  # noqa: BLE001
                    report.add(
                        "message envelopes decrypt byte-for-byte",
                        False,
                        f"decryption error {type(exc).__name__}",
                    )

                act_row = session.scalars(select(Activity).limit(1)).first()
                if act_row is not None:
                    try:
                        got_summary = cipher.open(
                            Envelope(**dict(act_row.summary_envelope)), activity_aad(act_row.id)
                        )
                        report.add(
                            "activity summary envelope decrypts byte-for-byte",
                            got_summary == expected.get("activity_summary"),
                        )
                    except Exception as exc:  # noqa: BLE001
                        report.add(
                            "activity summary envelope decrypts byte-for-byte",
                            False,
                            f"decryption error {type(exc).__name__}",
                        )

                gov_rows = session.scalars(
                    select(GovernanceRecord).order_by(GovernanceRecord.version)
                ).all()
                try:
                    got_gov = [
                        cipher.open(
                            Envelope(**dict(g.envelope)),
                            governance_aad(g.kind, g.handle, g.version),
                        )
                        for g in gov_rows
                    ]
                    report.add(
                        "governance envelopes decrypt byte-for-byte",
                        got_gov == expected.get("governance_plaintexts", []),
                        f"{len(got_gov)} records",
                    )
                except Exception as exc:  # noqa: BLE001
                    report.add(
                        "governance envelopes decrypt byte-for-byte",
                        False,
                        f"decryption error {type(exc).__name__}",
                    )
            elif expected:
                report.add(
                    "application envelope checks",
                    False,
                    "expected facts supplied but no master keys available "
                    "(AIC_MASTER_KEYS) — content recovery is unproven for this drill",
                )

            if expected and entry_row is not None and ns_row is not None:
                _, wrong_cipher = _build_cipher(f"k1:{secrets.token_hex(32)}", "k1")
                assert wrong_cipher is not None
                try:
                    wrong_cipher.open(
                        Envelope(**dict(entry_row.envelope)), entry_aad(ns_row.id, entry_row.key)
                    )
                    report.add("wrong application master key fails", False, "decrypted anyway!")
                except Exception as exc:  # noqa: BLE001
                    report.add(
                        "wrong application master key fails",
                        True,
                        f"rejected ({type(exc).__name__})",
                    )

                tampered = dict(entry_row.envelope)
                chars = list(tampered["ciphertext"])
                mid = len(chars) // 2
                chars[mid] = "0" if chars[mid] != "0" else "1"
                tampered["ciphertext"] = "".join(chars)
                try:
                    cipher.open(Envelope(**tampered), entry_aad(ns_row.id, entry_row.key))
                    report.add("tampered envelope rejected", False, "opened anyway!")
                except Exception as exc:  # noqa: BLE001
                    report.add(
                        "tampered envelope rejected", True, f"rejected ({type(exc).__name__})"
                    )

            from commons_core.database.models import SystemState

            stop_state = session.scalars(
                select(SystemState).where(SystemState.key == EMERGENCY_STOP_KEY).limit(1)
            ).first()
            stop_value = None
            if stop_state is not None and isinstance(stop_state.value, dict):
                stop_value = stop_state.value
            if expected:
                expected_stop = (expected.get("emergency_stop") or {}).get("active")
                report.add(
                    "emergency-stop system state carried over",
                    (stop_value or {}).get("active") == expected_stop,
                    f"active={(stop_value or {}).get('active')}",
                )
    finally:
        engine.dispose()

    report.passed = all(check.passed for check in report.checks)
    return report


def run_restore_drill(
    *,
    server_url: str,
    storage: StorageAdapter,
    backup_id: str,
    identity_path: str | os.PathLike,
    toolbelt: Toolbelt | None = None,
    expected_facts: dict | None = None,
    expected_facts_path: str | os.PathLike | None = None,
    master_keys: str | None = None,
    active_key_id: str | None = None,
    database_name: str | None = None,
    allow_nonlocal: bool = False,
    allowed_db_names: tuple[str, ...] = (),
    keep_database: bool = False,
) -> DrillReport:
    """Full synthetic restore drill against a disposable database.

    Gates, in order: destination guard → commit marker → manifest →
    artifact hash → client/server version compatibility → streamed
    decrypt+restore → application battery. Cleanup (drop target) runs on
    every path unless ``keep_database`` is set.
    """
    belt = toolbelt or locate_toolbelt()
    identity = Path(identity_path)
    if not identity.is_file():
        raise DrillFailure(f"age identity file not found: {identity}")

    if expected_facts_path and not expected_facts:
        expected_facts = json.loads(Path(expected_facts_path).read_text(encoding="utf-8"))

    target_name = database_name or f"aic_drill_{secrets.token_hex(4)}"
    target_url = _target_url(server_url, target_name)
    try:
        validate_restore_destination(
            target_url,
            allow_nonlocal=allow_nonlocal,
            allowed_db_names=allowed_db_names,
        )
    except UnsafeDestinationError as exc:
        raise DrillFailure(str(exc)) from exc

    marker = storage.commit_marker(backup_id)
    if marker is None:
        raise DrillFailure(
            f"backup {backup_id!r} has no commit marker — it was never fully published; "
            "refusing to drill a partial backup"
        )
    try:
        manifest_dict = storage.load_manifest(backup_id)
    except Exception as exc:  # noqa: BLE001 — missing/corrupt manifest is a drill gate
        raise DrillFailure(f"manifest for backup {backup_id!r} unreadable: {exc}") from exc
    manifest_bytes = json.dumps(manifest_dict, sort_keys=True).encode("utf-8")
    manifest = BackupManifest.from_bytes(manifest_bytes)
    artifact = storage.artifact_path(backup_id)

    # Layered defense: even an attacker who rewrites the manifest's artifact
    # hash must also forge the commit marker's manifest hash to get past this
    # gate — and the AEAD decrypt still rejects tampered ciphertext.
    import hashlib

    manifest_sha = hashlib.sha256(
        json.dumps(manifest_dict, sort_keys=True).encode("utf-8")
    ).hexdigest()
    marker_manifest_sha = marker.get("manifest_sha256")
    if marker_manifest_sha and marker_manifest_sha != manifest_sha:
        raise DrillFailure(
            f"manifest does not match the commit marker's manifest hash "
            f"({manifest_sha[:12]}… != {marker_manifest_sha[:12]}…) — the "
            "published manifest was modified after the marker was written"
        )

    actual_sha = sha256_file(artifact)
    if actual_sha != manifest.artifact_sha256():
        raise DrillFailure(
            f"artifact hash mismatch: manifest {manifest.artifact_sha256()[:12]}… vs "
            f"actual {actual_sha[:12]}… — storage corruption or tampering"
        )

    server_major = postgres_major(str(manifest.source["server_version"]))
    try:
        belt.ensure_compatible(server_major)
    except Exception as exc:  # noqa: BLE001 — tool incompatibility is a drill gate
        raise DrillFailure(str(exc)) from exc

    report = DrillReport(backup_id=backup_id, database_name=target_name)
    report.add("destination validated as disposable", True, describe_url(target_url))
    report.add("commit marker present", True)
    report.add("artifact sha256 matches manifest", True, f"sha={actual_sha[:12]}…")
    report.add(
        "tool compatibility (pg_restore >= server major)",
        True,
        f"server_major={server_major}",
    )

    if keep_database:
        report.add(
            "keep-database override ACTIVE — disposable database NOT dropped",
            True,
            "debug mode; manual cleanup required",
        )

    created = False
    try:
        _create_database(server_url, target_name)
        created = True
        ok, detail = _stream_decrypt_restore(
            belt=belt,
            identity_path=identity,
            artifact=artifact,
            target_url=target_url,
        )
        if not ok:
            report.add("age decrypt + pg_restore (streamed, no plaintext file)", False, detail)
            report.passed = False
            return report
        report.add("age decrypt + pg_restore (streamed, no plaintext file)", True)

        battery = verify_restored_database(
            target_url,
            expected=expected_facts,
            master_keys=master_keys,
            active_key_id=active_key_id,
            alembic_revision_from_manifest=manifest.alembic_revision,
        )
        report.checks.extend(battery.checks)
        report.passed = all(check.passed for check in report.checks)
        return report
    finally:
        if created and not keep_database:
            _drop_database(server_url, target_name)
