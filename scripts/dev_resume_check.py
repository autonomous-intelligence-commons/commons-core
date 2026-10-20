"""AIC resume audit — compare a surviving sandbox with exported checkpoints.

Usage::

    python scripts/dev_resume_check.py \\
        --repo /path/to/repo \\
        --worklog /path/to/worklog.md \\
        --checkpoint /path/to/bundle.zip [--checkpoint ...] \\
        [--json]

Implements the standardized resume checklist
(docs/DEVELOPMENT_CHECKPOINTS.md §12):

1.  inspect the sandbox: repository present?  branch?  clean or dirty tree?
2.  identify surviving worklogs;
3.  identify surviving commits (HEAD, tree, origin);
4.  locate and VERIFY each checkpoint bundle (delegates every integrity
    check to scripts/dev_checkpoint_verify.py);
5.  compare the checkpoint state with the surviving workspace:
    - checkpoint head present in repo and equal to HEAD → fully recovered;
    - checkpoint head is an ancestor → repo is AHEAD (unexported local
      work — export a new checkpoint);
    - checkpoint head missing and repo HEAD == baseline → patches still
      to apply (the normal mid-resume state);
    - checkpoint head missing and repo HEAD != baseline → baseline
      mismatch — investigate before applying anything;
    - checkpoint bundle file absent/expired → re-download and re-verify,
      or (if the repo already contains the checkpoint head) treat it as
      an audit-only loss;
6.  surface recorded environment dependencies and the recorded
    "next exact action";
7.  emit an ORDERED list of next actions.

Honest limits: this tool verifies structure and commits — NOT semantic test
results.  Recorded test results must be re-run once the environment is
rebuilt.  It never modifies anything: read-only audit.

Exit codes: 0 coherent resumable state (the printed next actions are the
resume path); 1 blocking inconsistency (not a repository, corrupt
checkpoint, baseline mismatch, or an unavailable checkpoint whose state is
missing from the repo); 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

VERIFY_TOOL = Path(__file__).resolve().parent / "dev_checkpoint_verify.py"


class _Entry:
    def __init__(self, name: str, status: str, detail: str) -> None:
        self.name = name
        self.status = status  # ok | warn | fail | info
        self.detail = detail

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[:300])
    return proc.stdout


def _verify_bundle(bundle: Path) -> tuple[bool, dict[str, object] | None, str]:
    """Delegate to dev_checkpoint_verify.py --json. Returns (ok, manifest, error)."""
    proc = subprocess.run(
        [sys.executable, str(VERIFY_TOOL), str(bundle), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False, None, f"verifier produced no parseable output (exit {proc.returncode})"
    ok = bool(payload.get("ok"))
    manifest = payload.get("manifest")
    error = str(payload.get("error") or "")
    return ok, manifest if isinstance(manifest, dict) else None, error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dev_resume_check.py",
        description="Audit a surviving sandbox against exported checkpoints and "
        "emit the ordered resume plan.",
    )
    parser.add_argument("--repo", default=".", help="repository path (default: cwd)")
    parser.add_argument("--worklog", help="recorded worklog path to look for")
    parser.add_argument(
        "--checkpoint",
        action="append",
        default=[],
        metavar="BUNDLE",
        help="checkpoint bundle to verify and compare (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    entries: list[_Entry] = []
    actions: list[str] = []
    repo_info: dict[str, object] = {}
    blocking = False

    repo = Path(args.repo).resolve()
    # ---- 1. sandbox / repository -------------------------------------------
    try:
        toplevel = Path(
            subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        ).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError):
        entries.append(
            _Entry(
                "repository",
                "fail",
                f"not a usable Git repository (or git missing): {repo}",
            )
        )
        blocking = True
        _finish(entries, actions, repo_info, blocking, args.json)
        return 1
    head = _git(toplevel, "rev-parse", "HEAD").strip()
    head_tree = _git(toplevel, "rev-parse", "HEAD^{tree}").strip()
    branch = _git(toplevel, "rev-parse", "--abbrev-ref", "HEAD").strip()
    origin = _git(toplevel, "config", "--get", "remote.origin.url", check=False).strip()
    porcelain = [line for line in _git(toplevel, "status", "--porcelain").splitlines() if line]
    repo_info = {
        "toplevel": str(toplevel),
        "branch": branch,
        "head": head,
        "head_tree": head_tree,
        "origin": origin,
        "dirty": bool(porcelain),
    }
    entries.append(
        _Entry(
            "repository",
            "ok",
            f"{toplevel} — branch {branch!r}, HEAD {head[:12]}",
        )
    )
    if porcelain:
        entries.append(
            _Entry(
                "work-tree",
                "warn",
                f"{len(porcelain)} uncommitted/untracked entries (e.g. "
                f"{porcelain[0][3:].strip(chr(34))!r}); commit or stash them "
                "before creating further checkpoints",
            )
        )
        actions.append(
            "Commit (or explicitly WIP-commit) the dirty entries so the tree is checkpointable."
        )
    else:
        entries.append(_Entry("work-tree", "ok", "clean"))

    # ---- 2. worklog ----------------------------------------------------------
    worklog_info: dict[str, object] | None = None
    if args.worklog:
        worklog = Path(args.worklog)
        if worklog.is_file():
            worklog_info = {"path": str(worklog), "bytes": worklog.stat().st_size}
            entries.append(_Entry("worklog", "ok", f"{worklog} ({worklog.stat().st_size} B)"))
        else:
            entries.append(
                _Entry(
                    "worklog",
                    "warn",
                    f"recorded worklog ABSENT: {worklog} — recover it from the "
                    "latest verified checkpoint (bundled as WORKLOG.md)",
                )
            )
            actions.append(
                "Recover the worklog: extract WORKLOG.md from the latest verified "
                "checkpoint bundle."
            )

    # ---- 3./4./5. checkpoints -------------------------------------------------
    checkpoint_reports: list[dict[str, object]] = []
    newest_manifest: dict[str, object] | None = None
    absent_bundles: list[_Entry] = []
    for bundle_path in args.checkpoint:
        bundle = Path(bundle_path)
        report: dict[str, object] = {"path": str(bundle)}
        if not bundle.is_file():
            # Temporary-artifact-expiration scenario: the recorded bundle is gone.
            entry = _Entry(
                f"checkpoint:{bundle.name}",
                "fail",
                "bundle file ABSENT (expired/unavailable) — re-download via the "
                "recorded channel and compare the bundle SHA-256 against the "
                "worklog register before trusting it",
            )
            entries.append(entry)
            absent_bundles.append(entry)
            report["status"] = "absent"
            checkpoint_reports.append(report)
            continue
        ok, manifest, error = _verify_bundle(bundle)
        if not ok or manifest is None:
            entries.append(
                _Entry(
                    f"checkpoint:{bundle.name}",
                    "fail",
                    f"verification FAILED: {error}",
                )
            )
            report["status"] = "corrupt"
            blocking = True
            checkpoint_reports.append(report)
            continue
        report["status"] = "verified"
        report["manifest"] = {
            "branch": ((manifest.get("repository") or {}).get("branch")),
            "baseline": ((manifest.get("repository") or {}).get("baseline_commit")),
            "head": ((manifest.get("repository") or {}).get("head_commit")),
        }
        entries.append(_Entry(f"checkpoint:{bundle.name}", "ok", "integrity verified"))
        checkpoint_reports.append(report)
        if newest_manifest is None:
            newest_manifest = manifest

    # ---- delta analysis against the newest verified checkpoint ---------------
    recovery_state = "unknown"
    if newest_manifest is not None:
        repo_meta = newest_manifest.get("repository")
        assert isinstance(repo_meta, dict)
        ckpt_tree = str(repo_meta.get("head_tree"))
        ckpt_baseline = str(repo_meta.get("baseline_commit"))
        ckpt_branch = str(repo_meta.get("branch"))
        commit_count = int(repo_meta.get("commit_count") or 0)
        # Content equality is proven by the TREE hash: git am re-applies produce
        # new commit SHAs (committer identity/timestamp), so the tree — not the
        # commit SHA — is the recovery identity (protocol §8).
        if head_tree == ckpt_tree:
            recovery_state = "recovered"
            entries.append(
                _Entry(
                    "delta",
                    "ok",
                    "repository HEAD tree EQUALS the checkpoint head tree — state "
                    "fully recovered (content-identical); do NOT re-apply patches",
                )
            )
            actions.append(
                "State recovered. Rebuild the environment if needed, re-run the "
                "recorded tests, then resume outstanding milestones from the "
                "worklog's 'next exact action'."
            )
        elif head == ckpt_baseline:
            recovery_state = "pending-apply"
            entries.append(
                _Entry(
                    "delta",
                    "warn",
                    f"checkpoint NOT yet applied: repo sits at the baseline; "
                    f"{commit_count} ordered patch(es) to apply (RECOVER.md)",
                )
            )
            actions.append(
                "Apply the checkpoint: verify the bundle, run "
                "dev_checkpoint_verify.py <bundle> --check-baseline-in <repo>, "
                "then `git am` the ordered patches, then --verify-applied <repo>."
            )
        else:
            # Locate the checkpoint CONTENT inside the branch history by tree
            # hash — works both for push/fetch recovery (original SHAs exist)
            # and for git am recovery (SHAs differ, trees match).
            walk = _git(
                toplevel,
                "log",
                "--reverse",
                "--format=%H %T",
                f"{ckpt_baseline}..{head}",
            ).splitlines()
            pairs = [line.split() for line in walk if line]
            match_index = None
            for idx, pair in enumerate(pairs):
                if len(pair) == 2 and pair[1] == ckpt_tree:
                    match_index = idx
            if match_index is not None:
                ahead = len(pairs) - 1 - match_index
                recovery_state = "ahead"
                entries.append(
                    _Entry(
                        "delta",
                        "warn",
                        f"repository CONTAINS the checkpoint content and is AHEAD "
                        f"by {ahead} commit(s) — local work not yet exported; "
                        "export a new checkpoint",
                    )
                )
                actions.append(
                    f"Export a fresh checkpoint: {ahead} commit(s) exist only in "
                    "this sandbox (scripts/dev_checkpoint.py), then continue."
                )
            else:
                recovery_state = "diverged"
                entries.append(
                    _Entry(
                        "delta",
                        "fail",
                        f"DIVERGENCE: repo HEAD {head[:12]} (tree {head_tree[:12]}) "
                        f"is neither the checkpoint tree {ckpt_tree[:12]}, nor its "
                        f"baseline {ckpt_baseline[:12]}, nor a state containing the "
                        "checkpoint content — investigate before applying or "
                        "overwriting anything",
                    )
                )
                blocking = True
        env_deps = newest_manifest.get("environment")
        if isinstance(env_deps, list) and env_deps:
            entries.append(
                _Entry(
                    "environment",
                    "info",
                    "recorded dependencies to verify on resume: "
                    + "; ".join(str(dep) for dep in env_deps),
                )
            )
        next_action = newest_manifest.get("next_action")
        if next_action:
            entries.append(_Entry("next-action", "info", f"recorded: {next_action}"))
        if ckpt_branch != branch and recovery_state == "pending-apply":
            entries.append(
                _Entry(
                    "branch",
                    "info",
                    f"checkpoint branch {ckpt_branch!r} differs from current "
                    f"{branch!r} — recreate it with "
                    f"`git switch -c {ckpt_branch}` before `git am`",
                )
            )

    # Missing-bundle severity refinement: an absent bundle is a hard blocker
    # ONLY while the recovered state is unproven.  When another verified
    # checkpoint proves the repo already contains the checkpointed head,
    # the loss is downgraded to an audit warning.
    if absent_bundles:
        if recovery_state == "recovered":
            for entry in absent_bundles:
                entry.status = "warn"
                entry.detail += " [downgraded: repository already contains the "
                "verified checkpoint head — treat as audit-only loss]"
        else:
            blocking = True
            missing = ", ".join(entry.name for entry in absent_bundles)
            actions.insert(
                0,
                f"Re-download the missing checkpoint bundle(s) ({missing}), verify "
                "each SHA-256 against the worklog register, then re-run this audit.",
            )

    if newest_manifest is None and not args.checkpoint:
        entries.append(
            _Entry(
                "checkpoints",
                "info",
                "no checkpoint bundles supplied — pass --checkpoint to compare "
                "against exported state",
            )
        )

    _finish(
        entries,
        actions,
        repo_info
        | {
            "recovery_state": recovery_state,
            "worklog": worklog_info,
            "checkpoints": checkpoint_reports,
        },
        blocking,
        args.json,
    )
    return 1 if blocking else 0


def _finish(
    entries: list[_Entry],
    actions: list[str],
    payload_extra: dict[str, object],
    blocking: bool,
    as_json: bool,
) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "ok": not blocking,
                    "checks": [entry.as_dict() for entry in entries],
                    "next_actions": actions,
                    **payload_extra,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    print("== AIC resume audit ==")
    for entry in entries:
        marker = {
            "ok": "PASS ",
            "warn": "WARN ",
            "fail": "FAIL ",
            "info": "INFO ",
        }[entry.status]
        print(f"{marker} {entry.name}: {entry.detail}")
    if actions:
        print("\n== Next actions (in order) ==")
        for number, action in enumerate(actions, start=1):
            print(f"  {number}. {action}")
    print(f"\nRESULT: {'BLOCKED' if blocking else 'RESUMABLE'}")


if __name__ == "__main__":
    raise SystemExit(main())
