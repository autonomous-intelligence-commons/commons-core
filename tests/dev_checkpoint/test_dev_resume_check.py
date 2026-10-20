"""Behaviour of scripts/dev_resume_check.py (spec §11/§12/§15).

Positive: coherent states audit cleanly (recovered / ahead / pending-apply /
no-checkpoints) with ordered next actions; JSON mode is machine-readable.

Negative matrix: non-repository (blocking), corrupt checkpoint (blocking),
missing/expired checkpoint artifact (blocking while the state is unproven,
downgraded to a warning once another verified checkpoint proves recovery),
diverged repository (blocking), dirty work tree (warn), absent worklog
(warn + recovery action).
"""

from __future__ import annotations

import json
import zipfile

from tests.dev_checkpoint.ckpt_synth import (
    RESUME_TOOL,
    commit_file,
    git,
    make_bundle,
    run_tool,
    synth_repo,  # noqa: F401 — importing registers the "synth" fixture
)


def audit(*args: str):
    return run_tool(RESUME_TOOL, *args)


class TestResumePositive:
    def test_clean_repo_without_checkpoints_is_resumable(self, synth) -> None:
        proc = audit("--repo", str(synth.repo))
        assert proc.returncode == 0, proc.stderr
        assert "RESULT: RESUMABLE" in proc.stdout
        assert "no checkpoint bundles supplied" in proc.stdout

    def test_recovered_state_detected_by_tree(self, synth) -> None:
        # Recovered via git am: commit SHAs differ, the tree matches — the
        # audit must recognize content-identical recovery.
        _, bundle = make_bundle(synth, worklog=synth.worklog)
        fresh = synth.root / "fresh"
        git(synth.repo, "clone", "-q", str(synth.bare), str(fresh))
        git(fresh, "checkout", "-q", synth.baseline)
        git(fresh, "switch", "-qc", "feature/test")
        extracted = synth.root / "ckpt"
        with zipfile.ZipFile(bundle) as archive:
            archive.extractall(extracted)
        git(fresh, "am", str(extracted / "patches" / "0001-feat-synthetic-change.patch"))
        proc = audit(
            "--repo",
            str(fresh),
            "--worklog",
            str(synth.root / "recovered-worklog.md"),
            "--checkpoint",
            str(bundle),
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "fully recovered" in proc.stdout
        assert "do NOT re-apply patches" in proc.stdout
        # The recorded worklog is absent on the new sandbox → recovery hint.
        assert "recorded worklog ABSENT" in proc.stdout

    def test_ahead_state_prompts_new_export(self, synth) -> None:
        _, bundle = make_bundle(synth)
        commit_file(synth.repo, "more.txt", "more\n", "feat: post-checkpoint work")
        proc = audit("--repo", str(synth.repo), "--checkpoint", str(bundle))
        assert proc.returncode == 0
        assert "AHEAD" in proc.stdout
        assert "Export a fresh checkpoint" in proc.stdout

    def test_pending_apply_state_gives_ordered_actions(self, synth) -> None:
        _, bundle = make_bundle(synth)
        fresh = synth.root / "fresh"
        git(synth.repo, "clone", "-q", str(synth.bare), str(fresh))
        git(fresh, "checkout", "-q", synth.baseline)
        proc = audit("--repo", str(fresh), "--checkpoint", str(bundle))
        assert proc.returncode == 0
        assert "checkpoint NOT yet applied" in proc.stdout
        assert "--check-baseline-in" in proc.stdout
        assert "recreate it with `git switch -c feature/test`" in proc.stdout

    def test_json_mode(self, synth) -> None:
        _, bundle = make_bundle(synth)
        proc = audit("--repo", str(synth.repo), "--checkpoint", str(bundle), "--json")
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert payload["ok"] is True
        assert payload["recovery_state"] == "recovered"
        assert payload["checkpoints"][0]["status"] == "verified"
        assert any(check["name"] == "delta" for check in payload["checks"])


class TestResumeNegative:
    def test_non_repository_is_blocking(self, tmp_path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        proc = audit("--repo", str(plain))
        assert proc.returncode == 1
        assert "not a usable Git repository" in proc.stdout

    def test_corrupt_checkpoint_is_blocking(self, synth) -> None:
        _, bundle = make_bundle(synth)
        data = bytearray(bundle.read_bytes())
        data[len(data) // 2] ^= 0xFF
        bundle.write_bytes(bytes(data))
        proc = audit("--repo", str(synth.repo), "--checkpoint", str(bundle))
        assert proc.returncode == 1
        assert "RESULT: BLOCKED" in proc.stdout
        assert "verification FAILED" in proc.stdout

    def test_expired_artifact_blocks_unproven_state(self, synth) -> None:
        # The recorded bundle is GONE (temporary host expired, download lost)
        # and the repo sits at the baseline — recovery is impossible without
        # re-fetching the artifact, so the audit must block.
        _, bundle = make_bundle(synth)
        fresh = synth.root / "fresh"
        git(synth.repo, "clone", "-q", str(synth.bare), str(fresh))
        git(fresh, "checkout", "-q", synth.baseline)
        proc = audit("--repo", str(fresh), "--checkpoint", str(synth.root / "expired.zip"))
        assert proc.returncode == 1
        assert "ABSENT (expired/unavailable)" in proc.stdout
        assert "Re-download the missing checkpoint bundle" in proc.stdout

    def test_expired_artifact_downgrades_when_state_proven(self, synth) -> None:
        # A verified checkpoint proves the repo contains the checkpointed
        # content; a second, missing bundle is then an audit-only loss.
        _, first = make_bundle(synth)
        second = first.with_name(first.name.replace("synth", "synth-b"))
        second.write_bytes(first.read_bytes())
        proc = audit(
            "--repo",
            str(synth.repo),
            "--checkpoint",
            str(first),
            "--checkpoint",
            str(synth.root / "expired.zip"),
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "downgraded" in proc.stdout

    def test_diverged_repository_is_blocking(self, synth) -> None:
        _, bundle = make_bundle(synth)
        # A repo whose HEAD contains neither the checkpoint content nor its
        # baseline — applying patches here would be wrong.
        strange = synth.root / "strange"
        git(synth.repo, "clone", "-q", str(synth.bare), str(strange))
        git(strange, "config", "user.name", "Synthetic Tester")
        git(strange, "config", "user.email", "synthetic@example.invalid")
        git(strange, "checkout", "-q", synth.baseline)
        commit_file(strange, "unrelated.txt", "different work\n", "feat: different direction")
        proc = audit("--repo", str(strange), "--checkpoint", str(bundle))
        assert proc.returncode == 1
        assert "DIVERGENCE" in proc.stdout

    def test_dirty_tree_is_warned_not_hidden(self, synth) -> None:
        (synth.repo / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
        proc = audit("--repo", str(synth.repo))
        assert proc.returncode == 0
        assert "1 uncommitted/untracked entries" in proc.stdout
        assert "uncommitted.txt" in proc.stdout
