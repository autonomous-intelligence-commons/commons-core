"""AIC development checkpoint creator — portable, verifiable state export.

Usage (from any Git work tree; the tree must be CLEAN)::

    python scripts/dev_checkpoint.py \\
        --output-dir /home/z/checkpoints \\
        --worklog /home/z/my-project/worklog.md \\
        --task-slug my-task \\
        --milestone-done "implementation" --milestone-pending "integration" \\
        --test "python -m pytest -q" --test-result "488 passed" \\
        --next-action "run the PostgreSQL 16 suite" \\
        [--baseline <sha>] [--base-branch origin/main] [--dry-run]

Behaviour (docs/DEVELOPMENT_CHECKPOINTS.md):

* the work tree must be clean — untracked/uncommitted changes are NEVER
  silently bundled.  The one explicit exception is ``--allow-dirty-worklog``
  which tolerates dirt for EXACTLY the ``--worklog`` path (recorded in the
  manifest as ``worklog.dirty``).  To export unfinished code, make an
  explicit WIP commit instead.
* the baseline is ``--baseline`` or the merge-base of HEAD with
  ``--base-branch`` (default ``origin/main``, falling back to ``main``);
  it must be an ancestor of HEAD.
* patches are produced with ``git format-patch baseline..HEAD`` into the
  bundle in order (``patches/0001-*.patch`` …).
* the bundle contains MANIFEST.json, the ordered patches, the worklog
  snapshot (as ``WORKLOG.md``), a generated RECOVER.md, and a
  ``sha256sum -c``-compatible CHECKSUMS.sha256 covering every other file.
* patch text and the worklog snapshot are scanned for credential-shaped
  content (age private keys, PEM keys, password/secret/token assignments,
  high-entropy base64 literals).  Findings refuse the run unless
  ``--acknowledge-scan-findings`` is passed; the acknowledgement is
  recorded in the manifest.  A scan can never guarantee absence of secrets.
* output safety: the destination must be OUTSIDE the repository work tree;
  an existing bundle filename is never overwritten; the zip is written to
  ``<name>.part`` and atomically renamed only after re-reading it and
  re-verifying every recorded hash (an interrupted run can never leave a
  bundle that looks valid).
* ``--dry-run`` performs every check and prints the plan without writing.

Exit codes: 0 success; 1 failure (git/packaging/self-verification);
2 configuration or preflight error; 3 secret-scan findings (see report).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from zipfile import ZIP_DEFLATED, ZipFile

TOOL_NAME = "dev_checkpoint.py"
TOOL_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
BUNDLE_SUFFIX = ".zip"
CHUNK = 1 << 20

# ---------------------------------------------------------------------------
# Secret hygiene (docs/DEVELOPMENT_CHECKPOINTS.md §9).  Practical, honest
# limits: regex + entropy heuristics can never guarantee absence of secrets.
# ---------------------------------------------------------------------------
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("age-private-key", re.compile(r"AGE-SECRET-KEY-1[A-Za-z0-9+/]{10,}")),
    (
        "pem-private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"),
    ),
    (
        "credential-assignment",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|"
            r"master[_-]?key|token|auth[_-]?token|private[_-]?key)\b"
            r"[\s]*[:=][\s]*[^\s\"'`]{8,}"
        ),
    ),
)
# Known benign constants that must not trip the entropy heuristic: bare hex
# constants of Git (40) / SHA-256 (64) length appear legitimately in patches
# and documentation.
_HEX_WHITELIST = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
# Long base64-ish literals (mixed case + digits) — the shape of age
# identities and API keys, distinct from whitelisted hex constants.
_ENTROPY_CANDIDATE = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")


def scan_text_for_secrets(text: str) -> list[dict[str, str]]:
    """Return findings (pattern, line, redacted excerpt) for credential-shaped text."""
    findings: list[dict[str, str]] = []
    seen: set[tuple[str, int]] = set()

    def record(label: str, line_no: int, excerpt: str) -> None:
        key = (label, line_no)
        if key in seen:
            return
        seen.add(key)
        findings.append(
            {
                "pattern": label,
                "line": str(line_no),
                "excerpt": excerpt[:12] + "…" if len(excerpt) > 12 else excerpt,
            }
        )

    for label, pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            record(label, text.count("\n", 0, match.start()) + 1, match.group(0))
    for match in _ENTROPY_CANDIDATE.finditer(text):
        token = match.group(0)
        if _HEX_WHITELIST.match(token):
            continue
        if token.isdigit() or token.isalpha():
            continue
        record("high-entropy-literal", text.count("\n", 0, match.start()) + 1, token)
    findings.sort(key=lambda f: (int(f["line"]), f["pattern"]))
    return findings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fail(message: str, code: int = 1) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        _fail("git executable not found on PATH — install git >= 2.30 first.", 2)
    if check and proc.returncode != 0:
        _fail(
            f"git {' '.join(args)} failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}",
            1,
        )
    return proc.stdout


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    """Bundle creation timestamp.

    ``AIC_CHECKPOINT_CLOCK`` (ISO-ish UTC string) overrides the clock — a
    determinism hook for tests; it makes bundle filenames predictable so
    no-overwrite behaviour can be tested without racing the wall clock.
    """
    override = os.environ.get("AIC_CHECKPOINT_CLOCK", "").strip()
    if override:
        return override
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9-]+", "-", value).strip("-").lower()
    return slug or "task"


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def _resolve_repo(path: str) -> Path:
    repo = Path(path).resolve()
    if not repo.exists():
        _fail(f"repository path does not exist: {repo}", 2)
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        _fail("git executable not found on PATH — install git >= 2.30 first.", 2)
    if proc.returncode != 0:
        _fail(f"not a Git repository (or git missing): {repo}", 2)
    return Path(proc.stdout.strip()).resolve()


def _porcelain(repo: Path) -> list[str]:
    return [line for line in _git(repo, "status", "--porcelain").splitlines() if line]


def _check_clean_tree(repo: Path, allow_dirty_worklog: bool, worklog: Path | None) -> None:
    entries = _porcelain(repo)
    if not entries:
        return
    if not (allow_dirty_worklog and worklog is not None):
        preview = "\n  ".join(entries[:10])
        extra = "" if len(entries) <= 10 else f"\n  … and {len(entries) - 10} more"
        _fail(
            "work tree is not clean — refusing to bundle uncommitted or untracked "
            "changes (commit them, or make an explicit WIP commit; see "
            "docs/DEVELOPMENT_CHECKPOINTS.md §6). Dirty entries:\n  "
            f"{preview}{extra}",
            2,
        )
    try:
        worklog_rel = worklog.resolve().relative_to(repo)  # type: ignore[union-attr]
    except ValueError:
        _fail(
            "--allow-dirty-worklog requires the worklog to live INSIDE the "
            "repository (external worklogs cannot be dirty repo entries).",
            2,
        )
    dirty_others = [e for e in entries if e[3:].strip('"') != worklog_rel.as_posix()]
    if dirty_others:
        preview = "\n  ".join(dirty_others[:10])
        _fail(
            "--allow-dirty-worklog tolerates dirt ONLY for the --worklog file; "
            "other dirty entries remain refused:\n  " + preview,
            2,
        )


def _resolve_baseline(repo: Path, explicit: str | None, base_branch: str) -> tuple[str, str]:
    if explicit:
        baseline = _git(repo, "rev-parse", "--verify", f"{explicit}^{{commit}}").strip()
        source = "explicit --baseline"
    else:
        baseline = ""
        source = ""
        for branch in dict.fromkeys([base_branch, "origin/main", "main"]):
            probe = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--verify", branch],
                capture_output=True,
                text=True,
                check=False,
            )
            if probe.returncode == 0:
                baseline = probe.stdout.strip()
                source = f"merge-base with {branch}"
                break
        if not baseline:
            _fail(
                "could not determine a baseline: pass --baseline <commit> explicitly.",
                2,
            )
        merged = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "HEAD", baseline],
            capture_output=True,
            text=True,
            check=False,
        )
        if merged.returncode != 0 or not merged.stdout.strip():
            _fail(f"could not compute merge-base of HEAD with {source}.", 2)
        baseline = merged.stdout.strip()
    ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", baseline, "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if ancestor.returncode != 0:
        _fail(
            f"baseline {baseline[:12]} is not an ancestor of HEAD — refusing "
            "(patches would not reproduce this work tree).",
            2,
        )
    return baseline, source


def _check_output_dir(repo: Path, output_dir: Path) -> Path:
    resolved = output_dir.resolve()
    if resolved == repo or resolved.is_relative_to(repo):
        _fail(
            f"unsafe output destination: {resolved} is INSIDE the repository work "
            f"tree {repo} — bundles must live outside (never committed, never "
            "self-referential).",
            2,
        )
    if resolved.exists() and not resolved.is_dir():
        _fail(f"output path exists and is not a directory: {resolved}", 2)
    return resolved


# ---------------------------------------------------------------------------
# RECOVER.md template
# ---------------------------------------------------------------------------

_RECOVER_TEMPLATE = """# Recovery instructions — {bundle_name}

Generated by `scripts/dev_checkpoint.py` at {created_utc}.
Repository: {origin_url}
Branch:     `{branch}`
Baseline:   `{baseline}`  — {baseline_subject}
Head:       `{head}`  — {head_subject}
Head tree:  `{head_tree}`  ({commit_count} patch(es) to apply)

## Prerequisites

- git >= 2.30 is the only mandatory tool.  Python 3.12 is needed ONLY for
  the optional verification scripts; manual hash verification below needs
  just `sha256sum` (included in Git Bash on Windows).

```bash
sha256sum {bundle_name}          # compare with the value recorded by the creator
unzip -o {bundle_name} -d ckpt   # or: tar -xf, or PowerShell: Expand-Archive
cd ckpt && sha256sum -c CHECKSUMS.sha256   # verifies EVERY bundled file
```

## Reconstruction (Linux/macOS sandbox or Windows Git Bash)

```bash
git clone {origin_url} repo
cd repo
git checkout {baseline}
# GATE 1 (mandatory): the checkout MUST equal the manifest baseline:
git rev-parse HEAD        # expected: {baseline}
git switch -c {branch}
{apply_line}
# GATE 2 (mandatory): reconstructed CONTENT must match the checkpoint.
# git am legitimately produces NEW commit SHAs (committer identity and
# timestamp differ) — content equality is proven by the TREE hash:
git rev-parse HEAD^{{tree}}            # expected: {head_tree}
git log --format=%s --reverse {baseline}..HEAD   # same subjects, same order
cp ../ckpt/WORKLOG.md <your-worklog-location>   # if bundled
```

Mechanical gates (when Python 3.12 is available, from this branch):

```bash
python scripts/dev_checkpoint_verify.py {bundle_name} --check-baseline-in repo/   # BEFORE git am
python scripts/dev_checkpoint_verify.py {bundle_name} --verify-applied repo/      # AFTER git am
```

If `git am` reports a patch is already applied, STOP — do not force it.  The
state may already be recovered; run `scripts/dev_resume_check.py` (from this
branch) to compare before retrying anything.

## After reconstruction

1. Rebuild the environment per docs/DEVELOPMENT_CHECKPOINTS.md §11
   (Python 3.12 venv + `pip install -e ".[dev,postgres]"`; PostgreSQL 16 /
   pinned 18.6 + age v1.3.2 ONLY if this task's tests need them).
2. Re-run the recorded test commands (see MANIFEST.json `tests`).
3. Resume from the worklog section "Next exact action required to resume"
   (hint recorded in the manifest: `{next_action}`).
4. Never re-apply already-applied commits; never overwrite surviving work.

## Honest limits

This bundle carries committed state only — anything uncommitted was never
checkpointed.  Internal hash consistency is the INNER integrity boundary:
a consistently rewritten (tampered) bundle would re-verify internally but
NOT against the bundle-level SHA-256 recorded in the worklog register.
Always compare BOTH before trusting a recovered bundle.
"""


# ---------------------------------------------------------------------------
# Bundle assembly
# ---------------------------------------------------------------------------


def _build_bundle(
    repo: Path,
    staging: Path,
    args: argparse.Namespace,
    *,
    baseline: str,
    baseline_subject: str,
    head: str,
    head_subject: str,
    head_tree: str,
    branch: str,
    origin_url: str,
    repo_name: str,
    commits: list[str],
    subjects: list[str],
    worklog_source: Path | None,
    worklog_dirty: bool,
    created_utc: str,
    bundle_name: str,
    scan_findings: list[dict[str, str]],
) -> dict[str, object]:
    patch_dir = staging / "patches"
    patch_dir.mkdir(parents=True, exist_ok=True)
    patch_metas: list[dict[str, object]] = []
    files: dict[str, dict[str, object]] = {}

    if commits:
        _git(repo, "format-patch", f"{baseline}..{head}", "-o", str(patch_dir))
        patch_files = sorted(patch_dir.iterdir())
        if len(patch_files) != len(commits):
            _fail(
                f"git format-patch produced {len(patch_files)} files for "
                f"{len(commits)} commits — refusing.",
                1,
            )
        for index, (commit, subject, path) in enumerate(
            zip(commits, subjects, patch_files, strict=True), start=1
        ):
            meta: dict[str, object] = {
                "index": index,
                "commit": commit,
                "subject": subject,
                "filename": f"patches/{path.name}",
                "sha256": _sha256_file(path),
                "bytes": path.stat().st_size,
            }
            patch_metas.append(meta)
            files[str(meta["filename"])] = {"sha256": meta["sha256"], "bytes": meta["bytes"]}

    worklog_entry: dict[str, object] | None = None
    if worklog_source is not None:
        shutil.copyfile(worklog_source, staging / "WORKLOG.md")
        worklog_entry = {
            "filename": "WORKLOG.md",
            "sha256": _sha256_file(staging / "WORKLOG.md"),
            "bytes": (staging / "WORKLOG.md").stat().st_size,
            "dirty": worklog_dirty,
        }
        files["WORKLOG.md"] = {
            "sha256": worklog_entry["sha256"],
            "bytes": worklog_entry["bytes"],
        }

    next_patch = f"{len(patch_metas):04d}"
    apply_line = (
        "git am ../ckpt/patches/*.patch          # ordered 0001.." + next_patch
        if patch_metas
        else "# (baseline checkpoint: HEAD equals the baseline; no patches to apply)"
    )
    recover_text = _RECOVER_TEMPLATE.format(
        bundle_name=bundle_name,
        created_utc=created_utc,
        origin_url=origin_url or "<record the origin with: git remote add origin <URL>>",
        branch=branch,
        baseline=baseline,
        baseline_subject=baseline_subject,
        head=head,
        head_subject=head_subject,
        head_tree=head_tree,
        commit_count=len(patch_metas),
        apply_line=apply_line,
        next_action=args.next_action or "(see the recovered worklog)",
    )
    recover_path = staging / "RECOVER.md"
    recover_path.write_text(recover_text, encoding="utf-8")
    files["RECOVER.md"] = {
        "sha256": _sha256_file(recover_path),
        "bytes": recover_path.stat().st_size,
    }

    manifest: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "created_by": {"tool": TOOL_NAME, "tool_version": TOOL_VERSION},
        "created_utc": created_utc,
        "task_slug": args.task_slug,
        "repository": {
            "origin_url": origin_url,
            "name": repo_name,
            "branch": branch,
            "baseline_commit": baseline,
            "baseline_subject": baseline_subject,
            "head_commit": head,
            "head_subject": head_subject,
            "head_tree": head_tree,
            "commit_count": len(patch_metas),
        },
        "patches": patch_metas,
        "worklog": worklog_entry,
        "milestones": {
            "completed": list(args.milestone_done),
            "pending": list(args.milestone_pending),
        },
        "tests": [
            {"command": cmd, "result": res}
            for cmd, res in zip(args.test, args.test_result, strict=False)
        ],
        "known_limitations": list(args.limitation),
        "environment": list(args.env_dep),
        "next_action": args.next_action,
        "secret_scan": {
            "findings": len(scan_findings),
            "acknowledged": bool(args.acknowledge_scan_findings),
            "details": scan_findings if args.acknowledge_scan_findings else [],
        },
        "delivery": {
            "status": "LOCAL_ONLY",
            "note": "export before relying on this state (protocol §7)",
        },
        "files": files,
    }
    manifest_path = staging / "MANIFEST.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    checksum_lines = [f"{_sha256_file(manifest_path)}  MANIFEST.json"]
    for name in sorted(files):
        checksum_lines.append(f"{files[name]['sha256']}  {name}")
    (staging / "CHECKSUMS.sha256").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    return manifest


def _self_verify(staging: Path, manifest: dict[str, object]) -> None:
    files = manifest["files"]
    assert isinstance(files, dict)
    for name, meta in files.items():
        path = staging / str(name)
        if not path.is_file():
            _fail(f"self-verification: staged file missing: {name}", 1)
        if _sha256_file(path) != str(meta["sha256"]):
            _fail(f"self-verification: hash mismatch for {name}", 1)
    checksums: dict[str, str] = {}
    for line in (staging / "CHECKSUMS.sha256").read_text(encoding="utf-8").splitlines():
        if "  " in line:
            digest, name = line.split("  ", 1)
            checksums[name] = digest
    expected = {"MANIFEST.json": _sha256_file(staging / "MANIFEST.json")} | {
        str(name): str(meta["sha256"]) for name, meta in files.items()
    }
    if checksums != expected:
        _fail("self-verification: CHECKSUMS.sha256 does not match staged content", 1)


def _zip_bundle(staging: Path, target: Path) -> None:
    part = target.with_name(target.name + ".part")
    if part.exists():
        part.unlink()  # garbage from an interrupted run; never a valid bundle
    with ZipFile(part, "w", compression=ZIP_DEFLATED) as archive:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(staging).as_posix())
    with ZipFile(part) as archive:
        if archive.testzip() is not None:
            _fail("self-verification: written zip failed its CRC check", 1)
    if target.exists():
        _fail(
            f"refusing to overwrite existing bundle: {target} — checkpoints are "
            "immutable; create a new versioned checkpoint instead.",
            2,
        )
    os.replace(part, target)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Create a portable, verifiable development checkpoint bundle.",
    )
    parser.add_argument("--repo", default=".", help="repository path (default: cwd)")
    parser.add_argument(
        "--output-dir",
        required=True,
        help="bundle destination directory (must be OUTSIDE the repository work "
        "tree; created if missing)",
    )
    parser.add_argument("--worklog", help="worklog file to snapshot into the bundle")
    parser.add_argument(
        "--allow-dirty-worklog",
        action="store_true",
        help="tolerate an uncommitted work tree for EXACTLY the --worklog file "
        "(recorded as worklog.dirty=true; all other dirt still refuses)",
    )
    parser.add_argument(
        "--task-slug",
        help="short task identifier for the bundle filename (default: branch slug)",
    )
    parser.add_argument(
        "--baseline",
        help="explicit baseline commit (default: merge-base of HEAD with --base-branch)",
    )
    parser.add_argument(
        "--base-branch",
        default="origin/main",
        help="branch used to auto-detect the baseline (default: origin/main)",
    )
    parser.add_argument(
        "--milestone-done",
        action="append",
        default=[],
        metavar="TEXT",
        help="completed milestone (repeatable)",
    )
    parser.add_argument(
        "--milestone-pending",
        action="append",
        default=[],
        metavar="TEXT",
        help="pending milestone (repeatable)",
    )
    parser.add_argument(
        "--test",
        action="append",
        default=[],
        metavar="CMD",
        help="recorded test command (repeatable)",
    )
    parser.add_argument(
        "--test-result",
        action="append",
        default=[],
        metavar="TEXT",
        help="result matching the Nth --test entry",
    )
    parser.add_argument(
        "--env-dep",
        action="append",
        default=[],
        metavar="TEXT",
        help="environment dependency to verify on resume (repeatable)",
    )
    parser.add_argument(
        "--limitation",
        action="append",
        default=[],
        metavar="TEXT",
        help="known limitation to record (repeatable)",
    )
    parser.add_argument("--next-action", help="next exact action required to resume")
    parser.add_argument(
        "--acknowledge-scan-findings",
        action="store_true",
        help="explicitly acknowledge secret-scan findings and proceed anyway "
        "(the acknowledgement is recorded in the manifest)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run every check and print the plan WITHOUT writing anything",
    )
    args = parser.parse_args(argv)

    repo = _resolve_repo(args.repo)
    worklog_source = Path(args.worklog).resolve() if args.worklog else None
    if args.allow_dirty_worklog and worklog_source is None:
        _fail("--allow-dirty-worklog requires --worklog.", 2)
    if worklog_source is not None and not worklog_source.is_file():
        _fail(f"worklog file not found: {worklog_source}", 2)
    if len(args.test_result) > len(args.test):
        _fail("more --test-result entries than --test entries.", 2)

    _check_clean_tree(repo, args.allow_dirty_worklog, worklog_source)
    baseline, baseline_source = _resolve_baseline(repo, args.baseline, args.base_branch)
    head = _git(repo, "rev-parse", "HEAD").strip()
    head_tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    head_subject = _git(repo, "log", "-1", "--format=%s", head).strip()
    baseline_subject = _git(repo, "log", "-1", "--format=%s", baseline).strip()
    origin_url = _git(repo, "config", "--get", "remote.origin.url").strip()

    commit_count = int(_git(repo, "rev-list", "--count", f"{baseline}..{head}").strip())
    commits = (
        _git(repo, "rev-list", "--reverse", f"{baseline}..{head}").splitlines()
        if commit_count
        else []
    )
    subjects = (
        _git(repo, "log", "--reverse", "--format=%s", f"{baseline}..{head}").splitlines()
        if commit_count
        else []
    )

    worklog_dirty = False
    if worklog_source is not None:
        try:
            worklog_rel = worklog_source.relative_to(repo).as_posix()
        except ValueError:
            worklog_rel = None
        if worklog_rel:
            worklog_dirty = any(entry[3:].strip('"') == worklog_rel for entry in _porcelain(repo))

    created_utc = _utc_now()
    slug = _slugify(args.task_slug or branch)
    args.task_slug = slug

    # ---- secret scan over the content that would be bundled ----------------
    findings: list[dict[str, str]] = []
    if commits:
        patch_text = _git(repo, "log", "-p", f"{baseline}..{head}")
        for finding in scan_text_for_secrets(patch_text):
            findings.append({**finding, "source": "patches"})
    if worklog_source is not None:
        for finding in scan_text_for_secrets(worklog_source.read_text(encoding="utf-8")):
            findings.append({**finding, "source": "worklog"})
    if findings and not args.acknowledge_scan_findings:
        print("SECRET-SCAN FINDINGS (refusing to bundle):", file=sys.stderr)
        for finding in findings:
            print(
                f"  [{finding['source']}] {finding['pattern']} near line "
                f"{finding['line']}: {finding['excerpt']}",
                file=sys.stderr,
            )
        print(
            "Review these findings. If they are false positives (e.g. synthetic "
            "test material), re-run with --acknowledge-scan-findings; the "
            "acknowledgement is recorded in the manifest. A scan can never "
            "guarantee absence of all secrets.",
            file=sys.stderr,
        )
        raise SystemExit(3)

    # ---- plan --------------------------------------------------------------
    output_dir = _check_output_dir(repo, Path(args.output_dir))
    bundle_name = (
        f"checkpoint-{slug}-{created_utc.replace(':', '').replace('-', '')}"
        f"-{head[:8]}{BUNDLE_SUFFIX}"
    )
    target = output_dir / bundle_name

    plan = {
        "repository": str(repo),
        "branch": branch,
        "baseline": baseline,
        "baseline_source": baseline_source,
        "head": head,
        "head_tree": head_tree,
        "commits": commit_count,
        "worklog": str(worklog_source) if worklog_source else None,
        "worklog_dirty": worklog_dirty,
        "secret_scan_findings": len(findings),
        "secret_scan_acknowledged": bool(args.acknowledge_scan_findings),
        "bundle": str(target),
        "milestones_completed": list(args.milestone_done),
        "milestones_pending": list(args.milestone_pending),
        "next_action": args.next_action,
    }
    print(json.dumps(plan, indent=2))

    if args.dry_run:
        print("DRY-RUN: no bundle written.")
        return 0

    if target.exists():
        _fail(
            f"refusing to overwrite existing bundle: {target} — checkpoints are "
            "immutable; create a new versioned checkpoint instead.",
            2,
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix="dev-checkpoint-", dir=output_dir))
    try:
        manifest = _build_bundle(
            repo,
            staging,
            args,
            baseline=baseline,
            baseline_subject=baseline_subject,
            head=head,
            head_subject=head_subject,
            head_tree=head_tree,
            branch=branch,
            origin_url=origin_url,
            repo_name=repo.name,
            commits=commits,
            subjects=subjects,
            worklog_source=worklog_source,
            worklog_dirty=worklog_dirty,
            created_utc=created_utc,
            bundle_name=bundle_name,
            scan_findings=findings,
        )
        _self_verify(staging, manifest)
        _zip_bundle(staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    bundle_sha = _sha256_file(target)
    print(f"BUNDLE={target}")
    print(f"BUNDLE_SHA256={bundle_sha}")
    print(
        "Delivery status: LOCAL_ONLY — export through an authorized external "
        "mechanism and record the transition in the worklog register "
        "(docs/DEVELOPMENT_CHECKPOINTS.md §7)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
