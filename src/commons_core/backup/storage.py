"""Backup publication storage — atomic, verified, conflict-safe.

Stage 1 review correction 3 (atomic publication and verified completion):

An encrypted file plus a manifest only become *a backup* when both objects
are durably present and verified. The publication protocol implemented by
:class:`LocalFilesystemStorage` (and required of every future adapter, e.g.
the Stage 3 S3-compatible backend) is:

1. write each object under a UNIQUE temporary ``*.part`` name (exclusive
   create, mode ``0600``) so concurrent publications of the same backup id
   can never collide on the staging path (R11);
2. read the temporary object back and verify SHA-256 against the local
   artifact (remote-upload verification in miniature) and fsync its
   contents before promotion (R11 durability);
3. refuse to overwrite: promotion uses an exclusive atomic create
   (``os.link`` → fails if the final name exists); if the final object
   name already exists, the adapter compares hashes — equal hashes make
   the publish idempotent (retry-safe), different hashes are a hard
   conflict (never silently rewrite history);
4. only after BOTH objects are in place and verified, write the commit
   marker ``status/<backup_id>.json`` — and fsync the containing
   directories so the marker cannot be lost while the objects survive a
   crash (R11). The marker is the single success indicator; its absence
   always means "this backup was never fully published", no matter which
   intermediate files exist;
5. NEVER report success from the marker alone (R11/T09): the idempotent
   fast path RE-VERIFIES the whole committed set — the artifact must exist
   with the marker's hash, the manifest must exist with the marker's
   manifest hash, and the marker must be structurally sane. A committed
   backup whose objects are missing or corrupted is an EXPLICIT failure
   ("published set corrupted"), never a silent success.

Retries wrap each fallible step (configurable attempts with linear
backoff) so transient storage failures don't strand a half-published
backup. Partial publications are recoverable by re-running with the SAME
backup id (idempotent completion) but never by overwriting with different
content.

``latest.json`` is a rebuildable convenience index, not a backup object:
a failure to update it is reported as a warning and can never turn an
already-committed backup into a failure (R11 — success is defined by the
complete committed set alone).

Least privilege (correction 4): this adapter creates directories with
mode ``0700`` and files with ``0600``. It grants itself no delete on final
objects — deletion is a retention-administrator concern implemented in a
FUTURE, separately reviewed stage.

Platform limitations (documented honestly, R11/R12): directory fsync is
unsupported on some platforms/filesystems (e.g. Windows) — there the code
proceeds without it and the operator must not assume crash-atomicity of
the marker ordering on those platforms; the exclusive-create promotion
falls back to ``os.replace`` where ``os.link`` is unusable, which preserves
single-writer semantics but not last-writer prevention between concurrent
processes on those platforms.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Protocol

from .manifest import sha256_file


class StorageError(RuntimeError):
    """A storage adapter failed (retryable only if `transient`)."""

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class BackupConflictError(StorageError):
    """A different backup already exists under this backup id.

    Not transient and never retried silently: backup ids are immutable.
    """


class MissingBackupObject(StorageError):
    """A requested backup id has no published manifest/artifact/marker."""


class PublishedSetCorrupted(StorageError):
    """A committed backup's object set is missing or corrupted (R11).

    The commit marker exists, but the artifact/manifest it commits are
    absent or fail hash verification. This is an EXPLICIT failure: the
    adapter must never report success for a backup whose objects cannot
    be verified, no matter what the marker says. The backup id remains
    immutable — recovery means publishing under a NEW id (or restoring the
    objects from a trusted copy and re-verifying), never re-blessing this
    one through the fast path.
    """


class StorageAdapter(Protocol):
    """What every storage backend must provide (documented for Stage 3 S3)."""

    def publish(
        self,
        backup_id: str,
        *,
        artifact_path: str | os.PathLike,
        manifest_bytes: bytes,
        artifact_sha256: str,
    ) -> dict:
        """Publish artifact + manifest, then the commit marker. Idempotent."""
        ...

    def load_manifest(self, backup_id: str) -> dict:
        """Return the published manifest (raises MissingBackupObject)."""
        ...

    def artifact_path(self, backup_id: str) -> Path:
        """Return a local readable path to the encrypted artifact."""
        ...

    def commit_marker(self, backup_id: str) -> dict | None:
        """Return the commit marker if this backup is fully published."""
        ...


def _verify_readback(path: Path, expected_sha256: str) -> None:
    if not path.is_file():
        raise StorageError(f"staging object {path.name!r} missing after write")
    got = sha256_file(path)
    if got != expected_sha256:
        raise StorageError(
            f"staging object {path.name!r} readback hash mismatch "
            f"({got[:12]}… != {expected_sha256[:12]}…) — storage layer corrupting writes"
        )


def _canonical_json_sha256(payload: bytes) -> str:
    """Hash of the CANONICAL JSON form — format-independent (see marker)."""
    import hashlib

    return hashlib.sha256(
        json.dumps(json.loads(payload), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _fsync_dir(directory: Path) -> None:
    """Best-effort durability of a directory entry (R11).

    Unsupported on some platforms/filesystems (e.g. Windows) — proceed
    without it there; the limitation is documented in the module docstring.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return  # platform without directory fds — documented limitation
    try:
        os.fsync(fd)
    except OSError:
        pass  # filesystem without dir fsync — documented limitation
    finally:
        os.close(fd)


def _unique_staging_path(final: Path) -> Path:
    """A unique, unpredictable staging name in the target directory (R11).

    Fixed ``<final>.part`` names let two concurrent publications of the
    same backup id fight over one staging file (T09 race window); a unique
    suffix keeps concurrent writers independent until the atomic promote.
    """
    return final.with_name(f"{final.name}.{uuid.uuid4().hex}.part")


def _promote_exclusive(tmp: Path, final: Path) -> None:
    """Atomically promote ``tmp`` to ``final`` WITHOUT overwriting (R11).

    ``os.link`` is an exclusive create on POSIX: it fails with EEXIST if
    the final name already exists, so two concurrent publishers cannot
    both win. Where hard links are unavailable the fallback is
    ``os.replace`` — atomic, but last-writer-wins between concurrent
    processes on those platforms (documented limitation).
    """
    try:
        os.link(tmp, final)
        return
    except FileExistsError:
        raise  # genuine conflict — caller compares hashes and decides
    except OSError:
        # EPERM/EXDEV/ENOSYS or platform without hard links — documented
        # fallback: atomic replace without the exclusive-create guarantee.
        os.replace(tmp, final)


def _write_part(path: Path, payload: bytes, *, expected_sha256: str) -> None:
    """Write-verify-promote one small object (manifest, marker)."""
    if path.exists():
        # Idempotent completion of a partially published backup: the same
        # bytes already promoted → done; different bytes → hard conflict.
        existing = sha256_bytes(path.read_bytes())
        if existing == expected_sha256:
            return
        raise BackupConflictError(
            f"object {path.name!r} already published with different content "
            f"({existing[:12]}… != {expected_sha256[:12]}…) — refusing to overwrite"
        )
    tmp = _unique_staging_path(path)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        _verify_readback(tmp, expected_sha256)
        try:
            _promote_exclusive(tmp, path)
        except FileExistsError:
            # Lost the race to another writer of the SAME object: the
            # winner's bytes must match ours (same expected hash) or it is
            # a hard conflict.
            existing = sha256_file(path)
            if existing != expected_sha256:
                raise BackupConflictError(
                    f"object {path.name!r} already published with different content "
                    f"({existing[:12]}… != {expected_sha256[:12]}…) — refusing to overwrite"
                ) from None
    finally:
        if tmp.exists():
            tmp.unlink()
    _verify_readback(path, expected_sha256)
    _fsync_dir(path.parent)


class LocalFilesystemStorage:
    """Stage 1 adapter: a directory tree standing in for the future bucket.

    Layout mirrors the planned bucket structure so Stage 3 only swaps the
    adapter, not the protocol::

        <root>/data/<backup_id>.age          encrypted artifact (0600)
        <root>/manifest/<backup_id>.json     authoritative manifest (0600)
        <root>/status/<backup_id>.json       commit marker (written LAST)
        <root>/status/latest.json            convenience pointer (marker-first)
    """

    def __init__(self, root: str | os.PathLike, *, retry_attempts: int = 3):
        self.root = Path(root)
        self.retry_attempts = max(1, retry_attempts)

    # -- layout helpers ----------------------------------------------------
    def _data_path(self, backup_id: str) -> Path:
        return self.root / "data" / f"{backup_id}.age"

    def _manifest_path(self, backup_id: str) -> Path:
        return self.root / "manifest" / f"{backup_id}.json"

    def _marker_path(self, backup_id: str) -> Path:
        return self.root / "status" / f"{backup_id}.json"

    def _latest_path(self) -> Path:
        return self.root / "status" / "latest.json"

    # -- internal retry wrapper --------------------------------------------
    def _with_retries(self, label: str, operation):
        last: Exception | None = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return operation()
            except BackupConflictError:
                raise
            except StorageError as exc:
                if not exc.transient and "refusing to overwrite" in str(exc):
                    raise
                last = exc
                if attempt < self.retry_attempts:
                    time.sleep(0.25 * attempt)
        raise StorageError(f"{label} failed after {self.retry_attempts} attempts: {last}")

    def _ensure_dirs(self) -> None:
        for sub in ("data", "manifest", "status"):
            directory = self.root / sub
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.exists():
                os.chmod(directory, 0o700)

    # -- publish ------------------------------------------------------------
    def _validate_marker_structure(self, backup_id: str, marker: dict) -> None:
        """Structural sanity of a commit marker (R11).

        A marker that is not a well-formed "verified-published" commit for
        this backup id is corruption — the storage layer must never treat
        a hand-edited or half-written marker as a success statement.
        """
        if not isinstance(marker, dict):
            raise PublishedSetCorrupted(
                f"commit marker for backup {backup_id!r} is not a JSON object — "
                "the published set cannot be verified from it"
            )
        if marker.get("backup_id") != backup_id:
            raise PublishedSetCorrupted(
                f"commit marker for backup {backup_id!r} names a different "
                f"backup id ({marker.get('backup_id')!r}) — marker/object mismatch"
            )
        if marker.get("state") != "verified-published":
            raise PublishedSetCorrupted(
                f"commit marker for backup {backup_id!r} has state "
                f"{marker.get('state')!r} — only a 'verified-published' marker "
                "commits a backup"
            )
        if not isinstance(marker.get("artifact_sha256"), str) or not isinstance(
            marker.get("manifest_sha256"), str
        ):
            raise PublishedSetCorrupted(
                f"commit marker for backup {backup_id!r} lacks the artifact/"
                "manifest hash fields — the published set cannot be verified"
            )

    def _revalidate_committed_set(self, backup_id: str, marker: dict) -> None:
        """Re-verify the FULL committed object set against the marker (R11).

        The T09 defect: the idempotent fast path trusted the marker alone,
        so a backup whose artifact had been deleted was still reported as
        "verified-published". Now the marker is only an INDEX — success is
        re-derived from the objects on every replay:

        * the artifact must exist and hash to ``marker['artifact_sha256']``;
        * the manifest must exist and hash (canonical JSON) to
          ``marker['manifest_sha256']``;
        * the marker itself must be structurally sane.

        Any violation raises :class:`PublishedSetCorrupted` — an explicit,
        actionable failure. The id stays immutable; the operator publishes
        under a new id or restores the objects from a trusted copy.
        """
        self._validate_marker_structure(backup_id, marker)

        artifact = self._data_path(backup_id)
        if not artifact.is_file():
            raise PublishedSetCorrupted(
                f"committed backup {backup_id!r} is INCOMPLETE: the encrypted "
                f"artifact {artifact.name!r} is MISSING while its commit marker "
                "exists — the marker alone is not success; restore the object "
                "from a trusted copy or publish under a new backup id"
            )
        actual_artifact_sha = sha256_file(artifact)
        if actual_artifact_sha != marker["artifact_sha256"]:
            raise PublishedSetCorrupted(
                f"committed backup {backup_id!r} is CORRUPT: artifact hash "
                f"{actual_artifact_sha[:12]}… does not match the committed "
                f"{marker['artifact_sha256'][:12]}… — truncated or tampered "
                "artifact; restore from a trusted copy or publish under a new id"
            )

        manifest_path = self._manifest_path(backup_id)
        if not manifest_path.is_file():
            raise PublishedSetCorrupted(
                f"committed backup {backup_id!r} is INCOMPLETE: the manifest "
                f"{manifest_path.name!r} is MISSING while its commit marker "
                "exists — the marker alone is not success; restore the object "
                "from a trusted copy or publish under a new backup id"
            )
        try:
            manifest_sha = _canonical_json_sha256(manifest_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise PublishedSetCorrupted(
                f"committed backup {backup_id!r} has an UNREADABLE manifest "
                f"({type(exc).__name__}) — the published set cannot be verified"
            ) from exc
        if manifest_sha != marker["manifest_sha256"]:
            raise PublishedSetCorrupted(
                f"committed backup {backup_id!r} is CORRUPT: manifest hash "
                f"{manifest_sha[:12]}… does not match the commit marker's "
                f"{marker['manifest_sha256'][:12]}… — the manifest changed "
                "after commit; restore from a trusted copy or publish under a new id"
            )

    def publish(
        self,
        backup_id: str,
        *,
        artifact_path: str | os.PathLike,
        manifest_bytes: bytes,
        artifact_sha256: str,
    ) -> dict:
        self._ensure_dirs()

        final_artifact = self._data_path(backup_id)
        manifest_path = self._manifest_path(backup_id)

        try:
            marker = self.commit_marker(backup_id)
        except (OSError, ValueError) as exc:
            raise StorageError(
                f"commit marker for backup {backup_id!r} exists but is unreadable "
                f"({type(exc).__name__}) — fail-closed; the marker file must be "
                "inspected and repaired manually before any publication"
            ) from exc
        if marker is not None:
            # Already fully published — R11: the marker alone is NOT success.
            # Re-verify the complete committed object set first; a missing or
            # corrupted set is an explicit PublishedSetCorrupted failure.
            self._revalidate_committed_set(backup_id, marker)
            if marker.get("artifact_sha256") == artifact_sha256:
                # Idempotent replay of a VERIFIED completed publish.
                self._refresh_latest_safely(marker)
                return marker
            raise BackupConflictError(
                f"backup id {backup_id!r} is already committed with different content "
                f"({marker.get('artifact_sha256', '?')[:12]}… != {artifact_sha256[:12]}…) — "
                "backup ids are immutable; refusing to overwrite"
            )

        # 1-2-3: artifact — stage under a UNIQUE .part name, verify, promote
        # exclusively (never overwrite a different existing object).
        self._with_retries(
            "artifact publication",
            lambda: self._publish_large(final_artifact, artifact_path, artifact_sha256),
        )

        # 4: manifest — small object, same write-verify-promote protocol.
        # The manifest hash recorded in the marker is computed over the
        # CANONICAL JSON form (json.dumps(..., sort_keys=True), no indent)
        # so it is format-independent and can be re-derived by the restore
        # drill from the parsed manifest alone.
        manifest_sha = _canonical_json_sha256(manifest_bytes)
        self._with_retries(
            "manifest publication",
            lambda: _write_part(
                manifest_path, manifest_bytes, expected_sha256=sha256_bytes(manifest_bytes)
            ),
        )

        # 5: commit marker — the ONLY success signal, written last.
        marker = {
            "backup_id": backup_id,
            "artifact_sha256": artifact_sha256,
            "manifest_sha256": manifest_sha,
            "state": "verified-published",
        }
        marker_sha = sha256_bytes(json.dumps(marker, sort_keys=True).encode("utf-8"))
        self._with_retries(
            "commit marker publication",
            lambda: _write_part(
                self._marker_path(backup_id),
                json.dumps(marker, sort_keys=True).encode("utf-8"),
                expected_sha256=marker_sha,
            ),
        )
        # latest.json is a rebuildable convenience index (R11): a failure
        # here is reported as a WARNING and never demotes the committed set.
        self._refresh_latest_safely(marker)
        return marker

    def _refresh_latest_safely(self, marker: dict) -> None:
        """Best-effort latest-pointer update (R11).

        The committed set (artifact + manifest + marker) alone defines
        success; a stale or failed latest.json must not turn a verified
        backup into a reported failure. The warning is explicit so
        operators notice the stale index.
        """
        try:
            self._with_retries("latest pointer", lambda: _write_latest(self._latest_path(), marker))
        except StorageError as exc:
            print(
                f"[storage] WARNING: latest pointer could not be updated ({exc}); "
                "the backup itself is committed and verified — the index is stale, "
                "rebuild it by republishing the same backup id",
                file=sys.stderr,
            )

    def _publish_large(self, final: Path, source: str | os.PathLike, expected_sha256: str) -> None:
        """Stage the artifact under a UNIQUE name, verify, promote exclusively."""
        if final.exists():
            existing = sha256_file(final)
            if existing == expected_sha256:
                return  # same bytes already published (idempotent step)
            raise BackupConflictError(
                f"artifact object {final.name!r} already exists with different content "
                f"({existing[:12]}… != {expected_sha256[:12]}…) — refusing to overwrite"
            )
        tmp = _unique_staging_path(final)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
                shutil.copyfileobj(src, out, length=1 << 20)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(tmp, 0o600)
            _verify_readback(tmp, expected_sha256)
            try:
                _promote_exclusive(tmp, final)
            except FileExistsError:
                # Concurrent writer won the final name: same bytes → both
                # publications agree (idempotent); different bytes → conflict.
                existing = sha256_file(final)
                if existing != expected_sha256:
                    raise BackupConflictError(
                        f"artifact object {final.name!r} already exists with different "
                        f"content ({existing[:12]}… != {expected_sha256[:12]}…) — "
                        "refusing to overwrite"
                    ) from None
        finally:
            if tmp.exists():
                tmp.unlink()
        _verify_readback(final, expected_sha256)
        _fsync_dir(final.parent)

    # -- reads ---------------------------------------------------------------
    def load_manifest(self, backup_id: str) -> dict:
        path = self._manifest_path(backup_id)
        if not path.is_file():
            raise MissingBackupObject(f"no published manifest for backup {backup_id!r}")
        return json.loads(path.read_text(encoding="utf-8"))

    def artifact_path(self, backup_id: str) -> Path:
        path = self._data_path(backup_id)
        if not path.is_file():
            raise MissingBackupObject(f"no published artifact for backup {backup_id!r}")
        return path

    def commit_marker(self, backup_id: str) -> dict | None:
        path = self._marker_path(backup_id)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def has_commit_marker(self, backup_id: str) -> bool:
        return self.commit_marker(backup_id) is not None

    def list_backup_ids(self) -> list[str]:
        status_dir = self.root / "status"
        if not status_dir.is_dir():
            return []
        ids = [
            p.stem
            for p in status_dir.glob("*.json")
            if p.name != "latest.json"
            and json.loads(p.read_text(encoding="utf-8")).get("state") == "verified-published"
        ]
        return sorted(ids)


def sha256_bytes(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def _write_latest(latest: Path, marker: dict) -> None:
    """Update the convenience pointer atomically (R11).

    Unique staging name + exclusive create: concurrent publishers cannot
    corrupt each other's staging files, and a concurrent update can only
    replace the pointer atomically. This is a REBUILDABLE INDEX, not a
    backup object — it may legitimately be overwritten.
    """
    marker_sha = sha256_bytes(json.dumps(marker, sort_keys=True).encode("utf-8"))
    latest_tmp = _unique_staging_path(latest)
    fd = os.open(latest_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(json.dumps(marker, sort_keys=True).encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        _verify_readback(latest_tmp, marker_sha)
        os.replace(latest_tmp, latest)
    finally:
        if latest_tmp.exists():
            latest_tmp.unlink()
    _fsync_dir(latest.parent)
