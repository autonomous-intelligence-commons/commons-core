"""AIC development checkpoint verifier — integrity and recovery gates.

Usage::

    python scripts/dev_checkpoint_verify.py <bundle.zip> [--json]
    python scripts/dev_checkpoint_verify.py <bundle.zip> --check-baseline-in <repo>
    python scripts/dev_checkpoint_verify.py <bundle.zip> --verify-applied <repo>
    python scripts/dev_checkpoint_verify.py <bundle.zip> \\
        [--expect-baseline <sha>] [--expect-head <sha>] [--expect-branch <name>]

Verification stages (all must pass; each failure is explicit — an incomplete
or tampered checkpoint is never silently accepted):

1. path exists and carries the ``.zip`` extension;
2. the first bytes are the ZIP magic ``PK\\x03\\x04`` — an HTML error page
   served with a 200 status is a real failure mode and must be detected;
3. the archive opens and every member passes its CRC check;
4. ``MANIFEST.json`` parses and conforms to schema version 1 (required
   fields, correct types, full 40-hex commit ids);
5. the member set EXACTLY equals the manifest file map plus
   ``MANIFEST.json`` and ``CHECKSUMS.sha256`` — no missing and no
   unexpected members;
6. every member's recomputed SHA-256 and byte size match the manifest;
7. ``CHECKSUMS.sha256`` (``sha256sum -c``-compatible) covers every member
   and every digest matches a fresh recomputation;
8. patches are strictly ordered ``patches/0001-*.patch`` … with consecutive
   indices matching the manifest and the recorded commit count.

Gates (mechanical enforcement of the protocol's baseline rule):

* ``--check-baseline-in REPO``: the repository's HEAD MUST equal the
  manifest baseline — run this BEFORE ``git am``; a wrong base refuses.
* ``--verify-applied REPO``: HEAD MUST equal the manifest head commit AND
  ``HEAD^{tree}`` MUST equal the recorded head tree — run this AFTER
  ``git am`` to prove the reconstruction is byte-identical.

Integrity limits (honest): internal consistency cannot detect a bundle that
was consistently rewritten everywhere INCLUDING the manifest — the outer
boundary is the bundle-level SHA-256 recorded in the worklog register at
creation time.  Always compare BOTH (docs/DEVELOPMENT_CHECKPOINTS.md §6).

Exit codes: 0 verification passed; 1 verification failed; 2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

MANIFEST_SCHEMA_VERSION = 1
_ZIP_MAGIC = b"PK\x03\x04"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class _Failure(Exception):
    """A single failed check with a human-readable detail."""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise _Failure(message)


def _load_manifest(archive: zipfile.ZipFile) -> dict[str, object]:
    names = archive.namelist()
    _check("MANIFEST.json" in names, "MANIFEST.json missing from bundle")
    try:
        manifest = json.loads(archive.read("MANIFEST.json").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _Failure(f"MANIFEST.json is not valid JSON: {exc}") from exc
    _check(isinstance(manifest, dict), "MANIFEST.json is not a JSON object")
    _check(
        manifest.get("schema_version") == MANIFEST_SCHEMA_VERSION,
        f"unsupported manifest schema_version: "
        f"{manifest.get('schema_version')!r} (expected {MANIFEST_SCHEMA_VERSION})",
    )
    for key in (
        "created_utc",
        "task_slug",
        "repository",
        "patches",
        "worklog",
        "milestones",
        "delivery",
        "files",
        "secret_scan",
    ):
        _check(key in manifest, f"MANIFEST.json missing required key: {key}")
    repo = manifest.get("repository")
    _check(isinstance(repo, dict), "manifest.repository is not an object")
    assert isinstance(repo, dict)
    for key in (
        "origin_url",
        "name",
        "branch",
        "baseline_commit",
        "head_commit",
        "head_tree",
        "commit_count",
    ):
        _check(key in repo, f"manifest.repository missing key: {key}")
    _check(
        isinstance(repo.get("commit_count"), int) and repo["commit_count"] >= 0,
        "manifest.repository.commit_count must be a non-negative integer",
    )
    for key in ("baseline_commit", "head_commit", "head_tree"):
        _check(
            isinstance(repo.get(key), str) and _HEX40.match(str(repo[key])),
            f"manifest.repository.{key} is not a full 40-hex commit id",
        )
    patches = manifest.get("patches")
    _check(isinstance(patches, list), "manifest.patches is not a list")
    assert isinstance(patches, list)
    for entry in patches:
        _check(isinstance(entry, dict), "manifest.patches contains a non-object entry")
        assert isinstance(entry, dict)
        for key in ("index", "commit", "subject", "filename", "sha256", "bytes"):
            _check(key in entry, f"manifest.patches entry missing key: {key}")
        _check(
            _HEX40.match(str(entry.get("commit", ""))) is not None,
            f"patch {entry.get('index')!r}: commit is not a full 40-hex id",
        )
        _check(
            _HEX64.match(str(entry.get("sha256", ""))) is not None,
            f"patch {entry.get('index')!r}: sha256 is malformed",
        )
    files = manifest.get("files")
    _check(isinstance(files, dict) and files, "manifest.files must be a non-empty map")
    assert isinstance(files, dict)
    for name, meta in files.items():
        _check(
            isinstance(meta, dict) and _HEX64.match(str(meta.get("sha256", ""))) is not None,
            f"manifest.files[{name}]: sha256 is malformed",
        )
        _check(
            isinstance(meta.get("bytes"), int) and meta["bytes"] >= 0,
            f"manifest.files[{name}]: bytes must be a non-negative integer",
        )
    worklog = manifest.get("worklog")
    _check(
        worklog is None or isinstance(worklog, dict),
        "manifest.worklog must be null or an object",
    )
    delivery = manifest.get("delivery")
    _check(
        isinstance(delivery, dict) and isinstance(delivery.get("status"), str),
        "manifest.delivery.status must be recorded",
    )
    return manifest


def _verify_structure(bundle: Path) -> dict[str, object]:
    """Full structural verification. Returns the manifest or raises _Failure."""
    _check(bundle.is_file(), f"bundle not found: {bundle}")
    _check(bundle.suffix == ".zip", f"unexpected bundle extension: {bundle.suffix!r}")
    with open(bundle, "rb") as handle:
        magic = handle.read(4)
    _check(
        magic == _ZIP_MAGIC,
        f"not a ZIP archive (first bytes {magic!r}) — this is the "
        "HTML-error-page-instead-of-bytes failure mode; the artifact is invalid",
    )
    try:
        archive = zipfile.ZipFile(bundle)
    except zipfile.BadZipFile as exc:
        raise _Failure(f"corrupted archive: {exc}") from exc
    with archive:
        bad = archive.testzip()
        _check(bad is None, f"CRC check failed for member: {bad}")
        manifest = _load_manifest(archive)

        files = manifest["files"]
        assert isinstance(files, dict)
        names = set(archive.namelist())
        expected = set(files) | {"MANIFEST.json", "CHECKSUMS.sha256"}
        missing = sorted(expected - names)
        extra = sorted(names - expected)
        _check(
            not missing,
            f"bundle members missing vs manifest: {missing}",
        )
        _check(
            not extra,
            f"unexpected bundle members not in manifest: {extra}",
        )
        for name, meta in files.items():
            data = archive.read(name)
            digest = hashlib.sha256(data).hexdigest()
            _check(
                digest == meta["sha256"],  # type: ignore[index]
                f"SHA-256 mismatch for {name}: manifest says "
                f"{str(meta['sha256'])[:16]}…, actual {digest[:16]}…",
            )
            _check(
                len(data) == meta["bytes"],  # type: ignore[index]
                f"byte-size mismatch for {name}: manifest {meta['bytes']}, actual {len(data)}",
            )

        _check("CHECKSUMS.sha256" in names, "CHECKSUMS.sha256 missing from bundle")
        checksums: dict[str, str] = {}
        for line in archive.read("CHECKSUMS.sha256").decode("utf-8").splitlines():
            if "  " in line:
                digest, name = line.split("  ", 1)
                checksums[name] = digest
        expected_checksum_names = set(files) | {"MANIFEST.json"}
        _check(
            set(checksums) == expected_checksum_names,
            "CHECKSUMS.sha256 covers "
            f"{sorted(set(checksums) ^ expected_checksum_names)} "
            "instead of exactly the manifest files plus MANIFEST.json",
        )
        for name, digest in checksums.items():
            actual = hashlib.sha256(archive.read(name)).hexdigest()
            _check(
                actual == digest,
                f"CHECKSUMS.sha256 mismatch for {name}: "
                f"recorded {digest[:16]}…, actual {actual[:16]}…",
            )

        patches = manifest["patches"]
        assert isinstance(patches, list)
        repo = manifest["repository"]
        assert isinstance(repo, dict)
        for position, entry in enumerate(patches, start=1):
            assert isinstance(entry, dict)
            index = entry.get("index")
            filename = str(entry.get("filename", ""))
            _check(
                index == position,
                f"patch order broken: position {position} has index {index!r}",
            )
            _check(
                filename.startswith(f"patches/{position:04d}-") and filename.endswith(".patch"),
                f"patch {position}: unexpected filename {filename!r} "
                f"(expected patches/{position:04d}-*.patch)",
            )
            _check(
                filename in files,
                f"patch {position}: {filename} is declared but absent from the manifest file map",
            )
        _check(
            len(patches) == repo["commit_count"],
            f"patch count {len(patches)} != manifest.repository.commit_count "
            f"{repo['commit_count']}",
        )
        worklog = manifest.get("worklog")
        if worklog is not None:
            assert isinstance(worklog, dict)
            _check(
                str(worklog.get("filename")) in files,
                f"manifest declares worklog {worklog.get('filename')!r} but it is "
                "absent from the bundle",
            )
    return manifest


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise _Failure(
            f"git {' '.join(args)} in {repo} failed: {(proc.stderr or proc.stdout).strip()[:300]}"
        )
    return proc.stdout.strip()


def _check_baseline_in(manifest: dict[str, object], repo_path: str) -> None:
    repo = manifest["repository"]
    assert isinstance(repo, dict)
    baseline = str(repo["baseline_commit"])
    repo_dir = Path(repo_path)
    if not repo_dir.is_dir():
        raise _Failure(f"--check-baseline-in: directory not found: {repo_dir}")
    head = _git(repo_dir, "rev-parse", "HEAD")
    _check(
        head == baseline,
        f"BASELINE GATE FAILED: repository HEAD is {head[:12]}, manifest baseline "
        f"is {baseline[:12]} — do NOT apply patches to this base; investigate the "
        "divergence first (docs/DEVELOPMENT_CHECKPOINTS.md §11).",
    )


def _verify_applied(manifest: dict[str, object], repo_path: str) -> None:
    """Post-``git am`` gate.

    Content equality is proven by the TREE hash (docs protocol §8/§11):
    re-applied patches legitimately produce NEW commit SHAs — ``git am``
    re-commits with the applying identity and a fresh committer timestamp —
    so commit-SHA equality is a bonus note, never the gate.  The mandatory
    checks are: identical head tree, identical patch count, identical
    ordered subjects.
    """
    repo_meta = manifest["repository"]
    assert isinstance(repo_meta, dict)
    head_commit = str(repo_meta["head_commit"])
    head_tree = str(repo_meta["head_tree"])
    baseline = str(repo_meta["baseline_commit"])
    commit_count = int(repo_meta["commit_count"])
    patches = manifest["patches"]
    assert isinstance(patches, list)
    expected_subjects = [str(entry.get("subject")) for entry in patches]
    repo_dir = Path(repo_path)
    if not repo_dir.is_dir():
        raise _Failure(f"--verify-applied: directory not found: {repo_dir}")
    tree = _git(repo_dir, "rev-parse", "HEAD^{tree}")
    _check(
        tree == head_tree,
        f"APPLIED GATE FAILED: HEAD tree is {tree}, manifest head tree is "
        f"{head_tree} — content differs from the checkpointed state",
    )
    actual_count = int(_git(repo_dir, "rev-list", "--count", f"{baseline}..HEAD"))
    _check(
        actual_count == commit_count,
        f"APPLIED GATE FAILED: {actual_count} commit(s) applied on the baseline, "
        f"manifest records {commit_count} — patches missing or duplicated",
    )
    actual_subjects = _git(
        repo_dir, "log", "--reverse", "--format=%s", f"{baseline}..HEAD"
    ).splitlines()
    _check(
        actual_subjects == expected_subjects,
        f"APPLIED GATE FAILED: applied subjects {actual_subjects!r} do not match "
        f"the manifest subjects {expected_subjects!r}",
    )
    head = _git(repo_dir, "rev-parse", "HEAD")
    if head != head_commit:
        # Expected after git am (committer identity/timestamp differ); the
        # tree-hash equality above is the content proof.  Surfaced as info.
        print(
            f"NOTE: applied HEAD {head[:12]} differs from the manifest head "
            f"{head_commit[:12]} — expected when patches are re-applied via "
            "git am (committer identity/timestamp differ); content equality is "
            "proven by the tree hash above."
        )
    else:
        print(
            f"NOTE: applied HEAD equals the manifest head exactly ({head[:12]}) — "
            "recovered via commit-object transfer, not just patch replay."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dev_checkpoint_verify.py",
        description="Verify a development checkpoint bundle and enforce recovery gates.",
    )
    parser.add_argument("bundle", help="checkpoint bundle (.zip) to verify")
    parser.add_argument(
        "--check-baseline-in",
        metavar="REPO",
        help="gate: repository HEAD must equal the manifest baseline (run BEFORE git am)",
    )
    parser.add_argument(
        "--verify-applied",
        metavar="REPO",
        help="gate: repository HEAD and tree must equal the manifest head (run AFTER git am)",
    )
    parser.add_argument(
        "--expect-baseline",
        metavar="SHA",
        help="additionally require the manifest baseline to equal this commit",
    )
    parser.add_argument(
        "--expect-head",
        metavar="SHA",
        help="additionally require the manifest head to equal this commit",
    )
    parser.add_argument(
        "--expect-branch",
        metavar="NAME",
        help="additionally require the manifest branch to equal this name",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    bundle = Path(args.bundle)
    manifest: dict[str, object] | None = None
    failure: str | None = None
    try:
        manifest = _verify_structure(bundle)
        if args.expect_baseline:
            repo = manifest["repository"]
            assert isinstance(repo, dict)
            _check(
                str(repo["baseline_commit"]) == args.expect_baseline,
                f"--expect-baseline mismatch: {repo['baseline_commit']} != {args.expect_baseline}",
            )
        if args.expect_head:
            repo = manifest["repository"]
            assert isinstance(repo, dict)
            _check(
                str(repo["head_commit"]) == args.expect_head,
                f"--expect-head mismatch: {repo['head_commit']} != {args.expect_head}",
            )
        if args.expect_branch:
            repo = manifest["repository"]
            assert isinstance(repo, dict)
            _check(
                str(repo["branch"]) == args.expect_branch,
                f"--expect-branch mismatch: {repo['branch']!r} != {args.expect_branch!r}",
            )
        if args.check_baseline_in:
            assert manifest is not None
            _check_baseline_in(manifest, args.check_baseline_in)
        if args.verify_applied:
            assert manifest is not None
            _verify_applied(manifest, args.verify_applied)
    except _Failure as exc:
        failure = str(exc)
    except OSError as exc:
        failure = f"cannot read bundle: {exc}"

    ok = failure is None
    if args.json:
        payload: dict[str, object] = {
            "ok": ok,
            "bundle": str(bundle),
            "error": failure,
            "manifest": manifest,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if ok:
            repo = manifest["repository"] if manifest else {}  # type: ignore[assignment]
            assert isinstance(repo, dict)
            print(f"PASS  structure — {bundle.name}")
            print(f"PASS  repository — {repo.get('name')} branch {repo.get('branch')!r}")
            print(
                f"PASS  range — {str(repo.get('baseline_commit'))[:12]}.."
                f"{str(repo.get('head_commit'))[:12]} "
                f"({repo.get('commit_count')} patch(es))"
            )
            print(
                "PASS  integrity — member set, per-file SHA-256, CHECKSUMS.sha256, "
                "patch order all verified"
            )
            if args.check_baseline_in:
                print(f"PASS  baseline gate — {args.check_baseline_in}")
            if args.verify_applied:
                print(f"PASS  applied gate — {args.verify_applied}")
            print("RESULT: OK")
        else:
            print(f"FAIL  {failure}", file=sys.stderr)
            print("RESULT: FAILED", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
