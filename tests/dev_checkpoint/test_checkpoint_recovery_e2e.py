"""End-to-end synthetic reset-and-resume cycle (spec §9, offline variant).

This is the automated, network-free version of the mandatory recovery
drill: a disposable repository with a surviving bare origin is
checkpointed, the working tree is DESTROYED, the state is reconstructed in
a fresh clone from the surviving origin, the ordered patches are applied
through the mechanical gates, and work resumes WITHOUT duplicating commits.

The genuine EXTERNAL variant of this drill (real upload, independent
re-download, SHA-256 comparison) is performed and recorded as delivery
evidence per docs/DEVELOPMENT_CHECKPOINTS.md §8 — CI deliberately never
touches file-hosting services (protocol §14).
"""

from __future__ import annotations

import json
import shutil
import zipfile

from tests.dev_checkpoint.ckpt_synth import (
    RESUME_TOOL,
    VERIFY_TOOL,
    commit_file,
    git,
    make_bundle,
    read_members,
    run_tool,
    sha256_bytes,
    synth_repo,  # noqa: F401 — importing registers the "synth" fixture
)


class TestLocalResetAndResumeCycle:
    def test_full_cycle(self, synth) -> None:
        # ---- Phase A: prepare a synthetic checkpoint -----------------------
        commit_file(synth.repo, "second.txt", "second feature\n", "feat: second synthetic change")
        _, bundle = make_bundle(
            synth,
            worklog=synth.worklog,
            extra_args=[
                "--milestone-done",
                "implementation",
                "--milestone-pending",
                "resume drill",
                "--next-action",
                "add the final synthetic file",
                "--env-dep",
                "git >= 2.30",
            ],
        )
        assert bundle is not None
        members = read_members(bundle)
        patch_names = [name for name in members if name.startswith("patches/")]
        assert patch_names == [
            "patches/0001-feat-synthetic-change.patch",
            "patches/0002-feat-second-synthetic-change.patch",
        ]
        original_bundle_sha = sha256_bytes(bundle.read_bytes())
        manifest = json.loads(members["MANIFEST.json"])

        # ---- Phase B (offline stand-in): external copy + retrieval ---------
        # The genuine drill uploads to an authorized external host; this
        # offline stand-in proves the RETRIEVAL side: a byte-identical copy
        # taken OUT of the original working directory re-verifies and its
        # SHA-256 matches the recorded value.
        external_dir = synth.root.parent / "external-storage"
        external_dir.mkdir(exist_ok=True)
        external_copy = external_dir / bundle.name
        external_copy.write_bytes(bundle.read_bytes())
        assert sha256_bytes(external_copy.read_bytes()) == original_bundle_sha
        retrieved = run_tool(VERIFY_TOOL, str(external_copy))
        assert retrieved.returncode == 0, retrieved.stderr

        # ---- Phase C: simulate TOTAL loss of the working directory ---------
        shutil.rmtree(synth.repo)
        assert not synth.repo.exists()  # only the disposable repo is gone
        assert synth.bare.exists()  # the "public origin" survives, like GitHub

        fresh = synth.root / "resumed-repo"
        git(synth.root, "clone", "-q", str(synth.bare), str(fresh))
        git(fresh, "config", "user.name", "Resumed Agent")
        git(fresh, "config", "user.email", "resumed@example.invalid")
        baseline = manifest["repository"]["baseline_commit"]
        git(fresh, "checkout", "-q", baseline)
        assert git(fresh, "rev-parse", "HEAD").stdout.strip() == baseline

        # GATE 1 — baseline validation BEFORE applying patches.
        gate1 = run_tool(VERIFY_TOOL, str(external_copy), "--check-baseline-in", str(fresh))
        assert gate1.returncode == 0, gate1.stderr

        extracted = synth.root / "retrieved"
        with zipfile.ZipFile(external_copy) as archive:
            archive.extractall(extracted)
        git(fresh, "switch", "-qc", "feature/test")
        patches = sorted((extracted / "patches").iterdir())
        assert [patch.name[:4] for patch in patches] == ["0001", "0002"]
        for patch in patches:
            applied = git(fresh, "am", str(patch))
            assert applied.returncode == 0, applied.stderr

        # GATE 2 — reconstructed content must equal the checkpoint content.
        gate2 = run_tool(VERIFY_TOOL, str(external_copy), "--verify-applied", str(fresh))
        assert gate2.returncode == 0, gate2.stderr
        assert (
            git(fresh, "rev-parse", "HEAD^{tree}").stdout.strip()
            == manifest["repository"]["head_tree"]
        )

        # Worklog recovered verbatim from the retrieved checkpoint.
        recovered_worklog = synth.root / "recovered-worklog.md"
        recovered_worklog.write_bytes(members["WORKLOG.md"])
        assert recovered_worklog.read_text(encoding="utf-8").startswith("# synthetic")

        # ---- Resume the remaining synthetic task WITHOUT duplication ------
        resume_audit = run_tool(
            RESUME_TOOL,
            "--repo",
            str(fresh),
            "--worklog",
            str(recovered_worklog),
            "--checkpoint",
            str(external_copy),
        )
        assert resume_audit.returncode == 0, resume_audit.stdout + resume_audit.stderr
        assert "fully recovered" in resume_audit.stdout
        assert "add the final synthetic file" in resume_audit.stdout

        final = commit_file(
            fresh, "final.txt", "resumed work\n", "feat: resume without duplication"
        )
        assert git(fresh, "rev-list", "--count", f"{baseline}..{final}").stdout.strip() == "3"

        # The resumed state is now AHEAD of the exported checkpoint — the
        # audit must say so and ask for a fresh export.
        post_resume = run_tool(
            RESUME_TOOL, "--repo", str(fresh), "--checkpoint", str(external_copy)
        )
        assert post_resume.returncode == 0
        assert "AHEAD" in post_resume.stdout

        # Duplicate application is refused by git am; after aborting the
        # stopped am session the audit still recognizes the recovered state.
        duplicate = git(fresh, "am", str(patches[0]), check=False)
        assert duplicate.returncode != 0
        git(fresh, "am", "--abort", check=False)
        still_recovered = run_tool(
            RESUME_TOOL, "--repo", str(fresh), "--checkpoint", str(external_copy)
        )
        assert still_recovered.returncode == 0
        assert ("fully recovered" in still_recovered.stdout) or ("AHEAD" in still_recovered.stdout)
