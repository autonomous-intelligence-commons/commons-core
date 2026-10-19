"""The streaming backup pipeline — pg_dump piped through age encryption.

Stage 1 review correction 1 (eliminate plaintext intermediate backups):

The original research PoC wrote a plaintext ``pg_dump -Fc`` file to disk and
only then encrypted it. This module implements the corrected shape: the
dump NEVER exists as a plaintext file anywhere. ``pg_dump`` writes its
custom-format output to a pipe consumed directly by ``age``, which streams
the authenticated-encrypted result into the artifact file. Only the
encrypted bytes ever touch the filesystem.

Explicit process supervision (no reliance on shell pipeline behavior):

* both subprocesses are spawned by this module with argv that NEVER
  contains credentials — connection parameters reach ``pg_dump`` through
  libpq environment variables (``PGHOST``/``PGPORT``/``PGDATABASE``/
  ``PGUSER``/``PGPASSWORD``/…), so neither process listings nor tracebacks
  can leak the URL or password;
* both subprocesses' exit statuses are inspected and propagated — a
  non-zero status from EITHER side fails the whole pipeline;
* stderr from both sides is drained by reader threads (no pipe-buffer
  deadlock) and scrubbed of secrets before it can appear in any error;
* premature death of ``age`` (bad recipient, disk full, …) closes the pipe
  so ``pg_dump`` dies with SIGPIPE — reported as "downstream exited
  prematurely", never mistaken for success;
* partial output can never be published: the encrypted artifact is staged
  under a ``.part`` name, verified (SHA-256 + structural age check), and
  only then handed to the storage adapter. ANY failure path unlinks the
  staged file first.

``pg_dump`` producing "some output" is never treated as success: the only
success indicator is the storage adapter's commit marker after both
objects verified (see :mod:`commons_core.backup.storage`).

R10: connection parameters for the dump subprocess come from the single
canonical connection contract
(:meth:`CanonicalConnection.subprocess_env`) — the inherited environment
is stripped of every ``PG*`` variable first, so no inherited ``PGHOST``/
``PGSERVICE``/``PGHOSTADDR`` can redirect the dump away from the URL the
caller named.

R12: every subprocess wait is bounded by a total deadline, both sides'
stderr capture is size-bounded, processes run in their own process group
(POSIX) so a deadline kill reaps the whole tree, and the staged ``.part``
artifact is removed — with an explicitly reported outcome — on EVERY
failure path, publication failures included.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import create_engine, text

from .age_format import AgeFormatError, verify_age_file
from .connection import (
    CanonicalConnection,
    ConnectionContractError,
    scrubbed_pg_environ,
)
from .destination import describe_url, scrub_secrets
from .manifest import (
    BackupManifest,
    ManifestBuildInputs,
    build_manifest,
    new_backup_id,
    sha256_file,
    utc_now,
)
from .storage import StorageAdapter
from .tools import (
    STDERR_MAX_LINES,
    Toolbelt,
    ToolError,
    locate_toolbelt,
    postgres_major,
    start_new_session_flag,
    subprocess_deadline_seconds,
    terminate_process_tree,
)


class BackupError(RuntimeError):
    """The backup pipeline failed; nothing was published."""


class PreflightError(BackupError):
    """Configuration/validation failed before anything ran (fail-closed)."""


class DumpFailed(BackupError):
    """pg_dump failed (connection, permission, timeout, partial output)."""


class EncryptionFailed(BackupError):
    """age failed, or died before consuming the whole dump."""


class ArtifactVerificationFailed(BackupError):
    """The encrypted artifact failed local verification before publication."""


class PublicationFailed(BackupError):
    """The storage adapter could not publish/verify both objects + marker.

    Storage-layer exceptions (including immutable-id conflicts) are wrapped
    here so every pipeline failure surfaces as a BackupError subtype —
    callers never need to catch two hierarchies. The original message is
    preserved verbatim (it is already credential-free by construction).
    """


class DeadlineExceeded(BackupError):
    """A subprocess did not exit within its total deadline (R12).

    The process tree was terminated (graceful SIGTERM first, then SIGKILL);
    staged output was removed. Bounded failure instead of a hung pipeline.
    """


@dataclass(frozen=True)
class BackupOutcome:
    """What the caller gets on success (never on failure — failure raises)."""

    manifest: BackupManifest
    artifact_sha256: str

    def to_json(self) -> str:
        return self.manifest.to_json()


def _reader_thread(stream, sink) -> threading.Thread:
    """Drain a subprocess stderr pipe into a bounded sink (R12).

    ``sink`` must be a ``deque(maxlen=STDERR_MAX_LINES)``: the newest lines
    are kept, the oldest are dropped when a tool floods its stderr — the
    pipeline's memory stays bounded no matter what the tool prints.
    """

    def run() -> None:
        try:
            for line in iter(stream.readline, b""):
                sink.append(line.decode("utf-8", "replace"))
        finally:
            stream.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _new_stderr_sink() -> deque:
    return deque(maxlen=STDERR_MAX_LINES)


def pg_env_from_url(database_url: str) -> dict[str, str]:
    """Translate a PostgreSQL URL into libpq environment variables.

    Secrets ride in the environment (visible only to the owner-privileged
    process and its children), never in argv. ``PGSSLMODE`` defaults to
    ``require`` for TCP connections to remote hosts and is left off for
    loopback (the CI/disposable databases use trust/scram on loopback —
    forcing TLS there would break them; the rule is documented in
    docs/BACKUP.md).

    R10: the URL is parsed by the single canonical connection contract —
    query parameters that could redirect the effective connection
    (``host``, ``hostaddr``, ``port``, ``service``, …) or unknown parameters
    are REJECTED here instead of being promoted to ``PG*`` variables.
    """
    try:
        canonical = CanonicalConnection.from_url(database_url)
    except ConnectionContractError as exc:
        parts = urlsplit(database_url or "")
        if parts.scheme not in ("postgresql", "postgresql+psycopg"):
            raise PreflightError(
                f"backup database URL must use the postgresql scheme, got {parts.scheme!r}"
            ) from exc
        raise PreflightError(f"backup database URL rejected: {exc}") from exc
    return canonical.libpq_env()


def _source_metadata(database_url: str) -> tuple[dict, str | None, int]:
    """Query server version, current database, and the alembic head revision.

    Consistency semantics (manifest correction 2): these values are read
    BEFORE pg_dump starts, from an ordinary (non-snapshot-pinned) session.
    They are recorded as informational context — not as proof that the dump
    contains exactly this schema version. See ``manifest.py``.

    R10: the engine is built from the canonical URL and the inherited
    ``PG*`` environment is scrubbed for the duration of the read, so the
    metadata query provably hits the connection the caller named.
    """
    try:
        canonical = CanonicalConnection.from_url(database_url)
    except ConnectionContractError as exc:
        raise PreflightError(f"backup database URL rejected: {exc}") from exc
    try:
        with scrubbed_pg_environ():
            engine = create_engine(canonical.sqlalchemy_url(), pool_pre_ping=True)
            try:
                with engine.connect() as conn:
                    server_version = conn.execute(text("SELECT version()")).scalar_one()
                    database_name = conn.execute(text("SELECT current_database()")).scalar_one()
                    try:
                        alembic_revision: str | None = conn.execute(
                            text("SELECT version_num FROM alembic_version")
                        ).scalar_one_or_none()
                    except Exception:  # noqa: BLE001 — unmigrated database: honest "unknown"
                        alembic_revision = None
                    major = postgres_major(str(server_version))
                    return (
                        {
                            "server_version": str(server_version),
                            "database_name": str(database_name),
                        },
                        alembic_revision,
                        major,
                    )
            finally:
                engine.dispose()
    except PreflightError:
        raise
    except Exception as exc:  # noqa: BLE001 — any connection failure is a fail-closed preflight error
        raise PreflightError(
            f"cannot read source metadata from {describe_url(database_url)} "
            f"({type(exc).__name__}) — is the server reachable and the URL correct?"
        ) from exc


def _describe_sigpipe(stage_stderr, extra_secrets: Sequence[str] = ()) -> str:
    """Scrub a subprocess's captured stderr for error messages.

    ``extra_secrets`` carries the run's libpq credentials (PGPASSWORD,
    PGUSER): PostgreSQL's own failure text can legitimately echo the
    USERNAME (e.g. ``FATAL: password authentication failed for user …``),
    and usernames are exactly as barred from operational output as
    passwords are — both are redacted here. R12: the sink is a bounded
    deque, so the newest lines are materialized before scrubbing.
    """
    joined = "".join(list(stage_stderr)[-8:]).strip()
    if not joined:
        return "(no stderr captured)"
    known = tuple(secret for secret in extra_secrets if secret)
    return scrub_secrets(joined, extra_secrets=known)


def _start_new_session() -> bool:
    """Own process group per subprocess, where the platform supports it."""
    return start_new_session_flag()


def _wait_with_deadline(
    proc: subprocess.Popen,
    *,
    label: str,
    deadline_seconds: float,
) -> int:
    """``wait()`` with a TOTAL deadline; on expiry kill the tree and fail (R12)."""
    try:
        return proc.wait(timeout=deadline_seconds)
    except subprocess.TimeoutExpired:
        note = terminate_process_tree(proc, label=label)
        detail = f" {note}" if note else ""
        raise DeadlineExceeded(
            f"{label} did not exit within its {deadline_seconds:.0f}s deadline — "
            f"process tree terminated; partial output discarded{detail}"
        ) from None


def _stream_dump_to_age(
    *,
    belt: Toolbelt,
    subprocess_env: dict[str, str],
    recipients: Sequence[str],
    staged_artifact: Path,
) -> None:
    """Run ``pg_dump --format=custom | age -r RECIPIENT…`` under supervision.

    The dump is encrypted to every recipient in *recipients* (multiple
    ``-r`` flags — age's native multi-recipient support), so any one of
    their identities can decrypt the artifact later.

    Raises :class:`DumpFailed` / :class:`EncryptionFailed` on any subprocess
    problem, and guarantees that ``staged_artifact`` is removed on failure
    (partial encrypted output must never survive).

    R10: ``subprocess_env`` is the COMPLETE child environment — inherited
    variables minus every ``PG*`` definition, plus the canonical connection
    values. Nothing inherited can redirect the dump.

    R12: every wait is deadline-bounded; both processes run in their own
    process group (POSIX) so deadline kills reap the whole tree; stderr
    capture is bounded.
    """
    dump_argv = [belt.pg_dump, "--format=custom"]
    age_argv = [belt.age]
    for one_recipient in recipients:
        age_argv.extend(["-r", one_recipient])
    age_argv.extend(["-o", str(staged_artifact)])

    dump_proc = age_proc = None
    dump_err = _new_stderr_sink()
    age_err = _new_stderr_sink()
    threads: list[threading.Thread] = []
    deadline = subprocess_deadline_seconds()
    try:
        dump_proc = subprocess.Popen(
            dump_argv,
            env=subprocess_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=_start_new_session(),
        )
        age_proc = subprocess.Popen(
            age_argv,
            stdin=dump_proc.stdout,  # type: ignore[arg-type]
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=_start_new_session(),
        )
        # Parent hands its copy of the pipe to age; closing our end lets
        # pg_dump receive SIGPIPE if age dies (fail-fast broken pipe).
        dump_proc.stdout.close()  # type: ignore[union-attr]

        threads.append(_reader_thread(dump_proc.stderr, dump_err))
        threads.append(_reader_thread(age_proc.stderr, age_err))

        dump_rc = _wait_with_deadline(dump_proc, label="pg_dump", deadline_seconds=deadline)
        age_rc = _wait_with_deadline(age_proc, label="age", deadline_seconds=deadline)
        for thread in threads:
            thread.join(timeout=5)

        # The run's libpq credentials ride to pg_dump via the environment;
        # the same values are handed to the scrubber so that neither the
        # password nor the USERNAME can survive in failure detail text.
        known_credentials = (subprocess_env.get("PGPASSWORD", ""), subprocess_env.get("PGUSER", ""))

        if dump_rc != 0:
            detail = _describe_sigpipe(dump_err, known_credentials)
            raise DumpFailed(
                f"pg_dump failed with status {dump_rc} ("
                f"{'terminated by SIGPIPE — downstream age process exited prematurely' if dump_rc == -signal.SIGPIPE else 'see scrubbed stderr'}"
                f"): {detail}"
            )
        if age_rc != 0:
            detail = _describe_sigpipe(age_err, known_credentials)
            raise EncryptionFailed(
                f"age failed with status {age_rc} after consuming the dump: {detail}"
            )
    except BackupError:
        _unlink(staged_artifact)
        raise
    except Exception as exc:  # noqa: BLE001 — wrap any supervision surprise
        _unlink(staged_artifact)
        raise BackupError(f"pipeline supervision failure: {exc}") from exc
    finally:
        notes: list[str] = []
        for proc, label in ((dump_proc, "pg_dump"), (age_proc, "age")):
            if proc is not None and proc.poll() is None:
                note = terminate_process_tree(proc, label=label)
                if note:
                    notes.append(note)
        if notes:  # pragma: no cover — pathological reap failure
            print(
                "[backup] WARNING: incomplete process cleanup — " + "; ".join(notes),
                file=sys.stderr,
            )

    # Reaching here means both subprocesses exited 0 — the artifact exists,
    # but "some output was produced" is NOT success (correction 1): local
    # verification happens next, and any failure there unlinks the file.


def _unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _remove_staged_observed(path: Path) -> tuple[bool, str | None]:
    """Remove the staged artifact, RETURNING the outcome instead of
    discarding it (R12).

    Returns ``(True, None)`` when the file is gone, ``(False, note)`` when
    the removal failed — the caller reports incomplete cleanup explicitly.
    """
    try:
        if path.exists():
            path.unlink()
        return True, None
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _publish_with_wrapped_errors(
    storage: StorageAdapter,
    *,
    backup_id: str,
    artifact_path,
    manifest_bytes: bytes,
    artifact_sha256: str,
) -> dict:
    """Call the storage adapter, surfacing every failure as BackupError."""
    from .storage import StorageError

    try:
        return storage.publish(
            backup_id,
            artifact_path=artifact_path,
            manifest_bytes=manifest_bytes,
            artifact_sha256=artifact_sha256,
        )
    except StorageError as exc:
        raise PublicationFailed(str(exc)) from exc


def run_backup(
    *,
    database_url: str,
    recipient: str,
    storage: StorageAdapter,
    work_dir: str | os.PathLike,
    toolbelt: Toolbelt | None = None,
    backup_id: str | None = None,
    extra_recipients: Sequence[str] = (),
) -> BackupOutcome:
    """Execute one full backup: dump → encrypt → verify → publish → confirm.

    On ANY failure the staged encrypted artifact is removed and nothing is
    published (no manifest, no commit marker). On success the manifest —
    whose ``state`` is only honest because publication verified both
    objects — is returned.

    R10: the dump subprocess receives the canonical, fully-controlled child
    environment built from the SAME parsed URL (inherited ``PG*``
    variables stripped; connection-redirecting query parameters rejected
    up front by :func:`pg_env_from_url`'s canonical parser).

    R12: the staged ``.part`` artifact is managed by ONE try/except —
    publication failures clean it up too (this closes the T10 gap where a
    ``PublicationFailed`` left the partial ciphertext on disk), and a
    cleanup failure is REPORTED, never silently discarded: on the failure
    path it is appended to the raised error, on the success path it is an
    explicit warning (the committed backup itself remains valid — the
    published copy was verified by the adapter).
    """
    if not database_url or not database_url.strip():
        raise PreflightError("no database URL supplied (fail-closed — refusing to run)")
    if not recipient or not recipient.strip():
        raise PreflightError("no age recipient supplied (fail-closed — refusing to run)")
    recipient = recipient.strip()
    if not recipient.startswith("age1"):
        raise PreflightError(
            "age recipient must be a public age1… key (private identities are never "
            "accepted by the backup path)"
        )

    recipients: list[str] = [recipient]
    for extra in extra_recipients:
        extra = extra.strip()
        if not extra.startswith("age1"):
            raise PreflightError(
                "extra age recipients must be public age1… keys (private "
                "identities are never accepted by the backup path)"
            )
        if extra not in recipients:
            recipients.append(extra)

    # Fail-closed URL contract check BEFORE any staging directory exists (R10).
    canonical = CanonicalConnection.from_url(database_url)

    belt = toolbelt or locate_toolbelt()
    backup_id = backup_id or new_backup_id()
    started_at = utc_now()
    work_path = Path(work_dir)
    work_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    staged = work_path / f"{backup_id}.age.part"

    print(
        f"[backup] backup_id={backup_id} source={describe_url(database_url)} started={started_at}"
    )

    child_env = canonical.subprocess_env(app_name="commons-core-backup")

    # Tool compatibility is validated BEFORE we touch the database.
    source_meta, alembic_revision, server_major = _source_metadata(database_url)
    try:
        belt.ensure_compatible(server_major)
    except ToolError as exc:
        raise PreflightError(str(exc)) from exc

    try:
        print("[backup] streaming pg_dump -> age (no plaintext intermediate file)")
        _stream_dump_to_age(
            belt=belt,
            subprocess_env=child_env,
            recipients=recipients,
            staged_artifact=staged,
        )

        # Local verification layer 1 (structural) + the authoritative hash.
        artifact_sha256 = sha256_file(staged)
        artifact_size = staged.stat().st_size
        if artifact_size == 0:
            raise ArtifactVerificationFailed("encrypted artifact is empty after a 0-exit pipeline")
        try:
            verify_age_file(staged)
        except AgeFormatError as exc:
            raise ArtifactVerificationFailed(
                f"encrypted artifact failed structural verification: {exc}"
            ) from exc

        manifest = build_manifest(
            ManifestBuildInputs(
                backup_id=backup_id,
                started_at_utc=started_at,
                created_at_utc=utc_now(),
                server_version=source_meta["server_version"],
                database_name=source_meta["database_name"],
                alembic_revision=alembic_revision,
                tool_versions=belt.version_info(),
                recipient=recipients[0],
                extra_recipients=tuple(recipients[1:]),
                artifact_sha256=artifact_sha256,
                artifact_size=artifact_size,
                artifact_filename=f"{backup_id}.age",
            )
        )

        marker = _publish_with_wrapped_errors(
            storage,
            backup_id=backup_id,
            artifact_path=staged,
            manifest_bytes=manifest.to_bytes(),
            artifact_sha256=artifact_sha256,
        )
        if marker.get("artifact_sha256") != artifact_sha256:
            raise BackupError("storage commit marker does not match the published artifact hash")

        # Success: remove the staged working copy (the published object is
        # the only durable copy; it was verified by the adapter).
        removed, removal_note = _remove_staged_observed(staged)
        if not removed:
            # The backup IS committed and verified — report success, but
            # never hide the duplicate working copy left behind (R12).
            print(
                f"[backup] WARNING: staged working copy {staged} could not be "
                f"removed after publication ({removal_note}) — remove it manually",
                file=sys.stderr,
            )
        print(
            f"[backup] published backup_id={backup_id} "
            f"sha256={artifact_sha256[:12]}… size={artifact_size} state={manifest.state}"
        )
        return BackupOutcome(manifest=manifest, artifact_sha256=artifact_sha256)
    except BaseException as primary_exc:
        # R12: EVERY failure path removes the staged encrypted artifact, and
        # a cleanup failure is reported explicitly — never silently
        # discarded, and never masking the primary failure.
        removed, removal_note = _remove_staged_observed(staged)
        if not removed:
            raise BackupError(
                f"{type(primary_exc).__name__}: {primary_exc} | "
                f"ADDITIONALLY, cleanup of the staged artifact FAILED "
                f"({removal_note}) — INCOMPLETE CLEANUP: the partial encrypted "
                f"artifact remains at {staged} and must be removed manually"
            ) from primary_exc
        raise
