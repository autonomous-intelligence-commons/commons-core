"""Backup publication storage — atomic, verified, conflict-safe.

Stage 1 review correction 3 (atomic publication and verified completion):

An encrypted file plus a manifest only become *a backup* when both objects
are durably present and verified. The publication protocol implemented by
:class:`LocalFilesystemStorage` (and required of every future adapter, e.g.
the Stage 3 S3-compatible backend) is:

1. write each object under a temporary ``*.part`` name with mode ``0600``;
2. read the temporary object back and verify SHA-256 against the local
   artifact (remote-upload verification in miniature);
3. refuse to overwrite: if the final object name already exists, the
   adapter compares hashes — equal hashes make the publish idempotent
   (retry-safe), different hashes are a hard conflict (never silently
   rewrite history);
4. promote with :func:`os.replace` — atomic on POSIX within one
   filesystem (do NOT assume an object-storage rename is atomic; a future
   S3 adapter must copy-then-verify-then-delete the staging key instead);
5. only after BOTH objects are in place and verified, write the commit
   marker ``status/<backup_id>.json``. The marker is the single success
   indicator; its absence always means "this backup was never fully
   published", no matter which intermediate files exist.

Retries wrap each fallible step (configurable attempts with linear
backoff) so transient storage failures don't strand a half-published
backup. Partial publications are recoverable by re-running with the SAME
backup id (idempotent completion) but never by overwriting with different
content.

Least privilege (correction 4): this adapter creates directories with
mode ``0700`` and files with ``0600``. It grants itself no delete on final
objects — deletion is a retention-administrator concern implemented in a
FUTURE, separately reviewed stage.
"""

from __future__ import annotations

import json
import os
import shutil
import time
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
    tmp = path.with_name(path.name + ".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        _verify_readback(tmp, expected_sha256)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    _verify_readback(path, expected_sha256)


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

        marker = self.commit_marker(backup_id)
        if marker is not None:
            # Already fully published: verify it is the SAME backup.
            if marker.get("artifact_sha256") == artifact_sha256:
                return marker  # idempotent replay of a completed publish
            raise BackupConflictError(
                f"backup id {backup_id!r} is already committed with different content "
                f"({marker.get('artifact_sha256', '?')[:12]}… != {artifact_sha256[:12]}…) — "
                "backup ids are immutable; refusing to overwrite"
            )

        # 1-2-3: artifact — stage under a .part name, verify, promote.
        self._with_retries(
            "artifact publication",
            lambda: self._publish_large(final_artifact, artifact_path, artifact_sha256),
        )

        # 4: manifest — small object, same write-verify-promote protocol.
        # The manifest hash recorded in the marker is computed over the
        # CANONICAL JSON form (json.dumps(..., sort_keys=True), no indent)
        # so it is format-independent and can be re-derived by the restore
        # drill from the parsed manifest alone.
        manifest_sha = sha256_bytes(
            json.dumps(json.loads(manifest_bytes), sort_keys=True).encode("utf-8")
        )
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
        self._with_retries(
            "latest pointer",
            lambda: _write_latest(self._latest_path(), marker),
        )
        return marker

    def _publish_large(self, final: Path, source: str | os.PathLike, expected_sha256: str) -> None:
        """Stage the artifact, verify the staging copy, promote atomically."""
        if final.exists():
            existing = sha256_file(final)
            if existing == expected_sha256:
                return  # same bytes already published (idempotent step)
            raise BackupConflictError(
                f"artifact object {final.name!r} already exists with different content "
                f"({existing[:12]}… != {expected_sha256[:12]}…) — refusing to overwrite"
            )
        tmp = final.with_name(final.name + ".part")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
                shutil.copyfileobj(src, out, length=1 << 20)
            os.chmod(tmp, 0o600)
            _verify_readback(tmp, expected_sha256)
            os.replace(tmp, final)
        finally:
            if tmp.exists():
                tmp.unlink()
        _verify_readback(final, expected_sha256)

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
    marker_sha = sha256_bytes(json.dumps(marker, sort_keys=True).encode("utf-8"))
    # latest.json is a convenience pointer: same integrity protocol, but it
    # MAY be overwritten (it is not a backup object, it points at the newest
    # committed one).
    latest_tmp = latest.with_name("latest.json.part")
    fd = os.open(latest_tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(json.dumps(marker, sort_keys=True).encode("utf-8"))
    _verify_readback(latest_tmp, marker_sha)
    os.replace(latest_tmp, latest)
