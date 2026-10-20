"""Verification-side behaviour of scripts/dev_checkpoint_verify.py.

Positive: valid bundle passes (human + --json), gates accept the correct
states (--check-baseline-in at the baseline, --verify-applied after git am).

Negative matrix (spec §15 — every corruption fails EXPLICITLY, exit 1,
never a silent pass): corrupted archive, truncated (interrupted-creation)
archive, missing patch, modified patch, invalid manifest (malformed JSON,
missing key, wrong schema version, non-consecutive patch order, malformed
commit ids), SHA-256 mismatch, missing worklog file, extra member,
CHECKSUMS coverage gap, wrong extension, HTML-instead-of-bytes, bundle not
found, incorrect baseline gate, premature applied gate, duplicate patch
application, --expect-* mismatches.
"""

from __future__ import annotations

import json
import zipfile

from tests.dev_checkpoint.ckpt_synth import (
    VERIFY_TOOL,
    commit_file,
    git,
    make_bundle,
    read_members,
    regen_checksums,
    run_tool,
    synth_repo,  # noqa: F401 — importing registers the "synth" fixture
    tamper_manifest,
    write_members,
)

# ---------------------------------------------------------------------------
# Positive
# ---------------------------------------------------------------------------


class TestVerifyPositive:
    def test_valid_bundle_passes(self, synth) -> None:
        _, bundle = make_bundle(synth, worklog=synth.worklog)
        proc = run_tool(VERIFY_TOOL, str(bundle))
        assert proc.returncode == 0, proc.stderr
        assert "RESULT: OK" in proc.stdout
        assert "integrity" in proc.stdout

    def test_json_mode_contains_manifest(self, synth) -> None:
        _, bundle = make_bundle(synth, worklog=synth.worklog)
        proc = run_tool(VERIFY_TOOL, str(bundle), "--json")
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert payload["ok"] is True
        assert payload["manifest"]["repository"]["head_commit"] == synth.head

    def test_check_baseline_gate_accepts_baseline(self, synth) -> None:
        _, bundle = make_bundle(synth)
        fresh = synth.root / "fresh"
        git(synth.repo, "clone", "-q", str(synth.bare), str(fresh))
        git(fresh, "checkout", "-q", synth.baseline)
        proc = run_tool(VERIFY_TOOL, str(bundle), "--check-baseline-in", str(fresh))
        assert proc.returncode == 0, proc.stderr
        assert "baseline gate" in proc.stdout

    def test_verify_applied_gate_accepts_git_am_result(self, synth) -> None:
        _, bundle = make_bundle(synth)
        fresh = synth.root / "fresh"
        git(synth.repo, "clone", "-q", str(synth.bare), str(fresh))
        # Recovery clones carry their OWN synthetic identity — a fresh
        # clone inherits no user.* config and clean CI runners have no
        # global one, so git am would fail with "Committer identity
        # unknown" (the GitHub CI regression).  A distinct identity from
        # the fixture repo also guarantees the am-produced commits get NEW
        # SHAs, as the gate's NOTE path expects.
        git(fresh, "config", "user.name", "Resumed Agent")
        git(fresh, "config", "user.email", "resumed@example.invalid")
        git(fresh, "checkout", "-q", synth.baseline)
        git(fresh, "switch", "-qc", "feature/test")
        extracted = synth.root / "ckpt"
        with zipfile.ZipFile(bundle) as archive:
            archive.extractall(extracted)
        git_am = git(fresh, "am", str(extracted / "patches" / "0001-feat-synthetic-change.patch"))
        assert git_am.returncode == 0
        proc = run_tool(VERIFY_TOOL, str(bundle), "--verify-applied", str(fresh))
        assert proc.returncode == 0, proc.stderr
        # git am produced a NEW commit sha — the tool must say so as a NOTE,
        # not treat it as a failure.
        assert "differs from the manifest head" in proc.stdout
        assert "content equality is proven by the tree hash" in proc.stdout

    def test_expect_flags_pass_and_fail(self, synth) -> None:
        _, bundle = make_bundle(synth)
        ok = run_tool(
            VERIFY_TOOL,
            str(bundle),
            "--expect-baseline",
            synth.baseline,
            "--expect-head",
            synth.head,
            "--expect-branch",
            "feature/test",
        )
        assert ok.returncode == 0
        bad = run_tool(VERIFY_TOOL, str(bundle), "--expect-branch", "nope")
        assert bad.returncode == 1
        assert "--expect-branch mismatch" in bad.stderr


# ---------------------------------------------------------------------------
# Negative matrix (spec §15 — verification side)
# ---------------------------------------------------------------------------


class TestVerifyNegative:
    def test_bundle_not_found(self, synth) -> None:
        proc = run_tool(VERIFY_TOOL, str(synth.out / "missing.zip"))
        assert proc.returncode == 1
        assert "bundle not found" in proc.stderr

    def test_wrong_extension_refused(self, synth) -> None:
        _, bundle = make_bundle(synth)
        renamed = bundle.with_suffix(".tar")
        renamed.write_bytes(bundle.read_bytes())
        proc = run_tool(VERIFY_TOOL, str(renamed))
        assert proc.returncode == 1
        assert "unexpected bundle extension" in proc.stderr

    def test_html_instead_of_bytes_detected(self, synth, tmp_path) -> None:
        # The REAL failure mode observed with public file hosts: a 200
        # status serving an HTML interstitial instead of the artifact.
        fake = tmp_path / "fake-checkpoint.zip"
        fake.write_text("<html><body>download page</body></html>", encoding="utf-8")
        proc = run_tool(VERIFY_TOOL, str(fake))
        assert proc.returncode == 1
        assert "HTML-error-page-instead-of-bytes" in proc.stderr

    def test_corrupted_archive_detected(self, synth) -> None:
        _, bundle = make_bundle(synth)
        data = bytearray(bundle.read_bytes())
        # Corrupt the END-OF-CENTRAL-DIRECTORY signature — the one structure
        # zipfile unconditionally requires.  (Flipping arbitrary middle
        # bytes can land in fields zipfile never validates.)
        data[-22] ^= 0xFF
        bundle.write_bytes(bytes(data))
        proc = run_tool(VERIFY_TOOL, str(bundle))
        assert proc.returncode == 1
        assert "corrupted archive" in proc.stderr

    def test_truncated_archive_detected(self, synth) -> None:
        # An interrupted bundle creation leaves a truncated file — it must
        # NEVER be accepted as a checkpoint.
        _, bundle = make_bundle(synth)
        data = bundle.read_bytes()
        bundle.write_bytes(data[: len(data) // 2])
        proc = run_tool(VERIFY_TOOL, str(bundle))
        assert proc.returncode == 1
        assert any(
            marker in proc.stderr
            for marker in ("corrupted archive", "CRC check failed", "not a ZIP")
        )

    def test_part_file_is_not_a_bundle(self, synth) -> None:
        _, bundle = make_bundle(synth)
        part = bundle.with_name(bundle.name + ".part")
        part.write_bytes(bundle.read_bytes()[:1000])
        proc = run_tool(VERIFY_TOOL, str(part))
        assert proc.returncode == 1

    def test_missing_patch_detected(self, synth) -> None:
        _, bundle = make_bundle(synth)
        members = read_members(bundle)
        del members["patches/0001-feat-synthetic-change.patch"]
        regen_checksums(members)
        write_members(bundle, members)
        proc = run_tool(VERIFY_TOOL, str(bundle))
        assert proc.returncode == 1
        assert "bundle members missing" in proc.stderr

    def test_modified_patch_detected(self, synth) -> None:
        _, bundle = make_bundle(synth)
        members = read_members(bundle)
        members["patches/0001-feat-synthetic-change.patch"] += b"\n# tampered\n"
        regen_checksums(members)
        write_members(bundle, members)
        # A fully consistent rewrite passes checksums but the PER-FILE hash
        # vs manifest must fail: manifest.files was not updated.
        proc = run_tool(VERIFY_TOOL, str(bundle))
        assert proc.returncode == 1
        assert "SHA-256 mismatch for patches/0001" in proc.stderr

    def test_sha_mismatch_detected(self, synth) -> None:
        _, bundle = make_bundle(synth)
        members = read_members(bundle)
        # Swap two members' content without touching the manifest.
        members["patches/0001-feat-synthetic-change.patch"], members["RECOVER.md"] = (
            members["RECOVER.md"],
            members["patches/0001-feat-synthetic-change.patch"],
        )
        write_members(bundle, members)
        proc = run_tool(VERIFY_TOOL, str(bundle))
        assert proc.returncode == 1
        assert "SHA-256 mismatch" in proc.stderr

    def test_manifest_malformed_json(self, synth) -> None:
        _, bundle = make_bundle(synth)
        members = read_members(bundle)
        members["MANIFEST.json"] = members["MANIFEST.json"][:20]
        regen_checksums(members)
        write_members(bundle, members)
        proc = run_tool(VERIFY_TOOL, str(bundle))
        assert proc.returncode == 1
        assert "not valid JSON" in proc.stderr

    def test_manifest_missing_required_key(self, synth) -> None:
        _, bundle = make_bundle(synth)

        def edit(manifest: dict) -> None:
            del manifest["repository"]

        tamper_manifest(bundle, edit)
        proc = run_tool(VERIFY_TOOL, str(bundle))
        assert proc.returncode == 1
        assert "missing required key: repository" in proc.stderr

    def test_manifest_wrong_schema_version(self, synth) -> None:
        _, bundle = make_bundle(synth)

        def edit(manifest: dict) -> None:
            manifest["schema_version"] = 99

        tamper_manifest(bundle, edit)
        proc = run_tool(VERIFY_TOOL, str(bundle))
        assert proc.returncode == 1
        assert "unsupported manifest schema_version" in proc.stderr

    def test_manifest_malformed_commit_id(self, synth) -> None:
        _, bundle = make_bundle(synth)

        def edit(manifest: dict) -> None:
            manifest["repository"]["head_commit"] = "deadbeef"

        tamper_manifest(bundle, edit)
        proc = run_tool(VERIFY_TOOL, str(bundle))
        assert proc.returncode == 1
        assert "not a full 40-hex commit id" in proc.stderr

    def test_manifest_patch_order_broken(self, synth) -> None:
        # Two commits ⇒ indices must be 1,2. Rewrite index 2 as 3 —
        # the order check must fail even with consistent checksums.
        commit_file(synth.repo, "second.txt", "second\n", "feat: second change")
        _, bundle = make_bundle(synth)
        names = [n for n in read_members(bundle) if n.startswith("patches/")]
        assert len(names) == 2

        def edit(manifest: dict) -> None:
            manifest["patches"][1]["index"] = 3

        tamper_manifest(bundle, edit)
        proc = run_tool(VERIFY_TOOL, str(bundle))
        assert proc.returncode == 1
        assert "patch order broken" in proc.stderr

    def test_manifest_declares_missing_worklog(self, synth) -> None:
        _, bundle = make_bundle(synth, worklog=synth.worklog)
        members = read_members(bundle)
        del members["WORKLOG.md"]
        regen_checksums(members)
        write_members(bundle, members)
        proc = run_tool(VERIFY_TOOL, str(bundle))
        assert proc.returncode == 1
        assert "members missing" in proc.stderr

    def test_unexpected_extra_member_detected(self, synth) -> None:
        _, bundle = make_bundle(synth)
        members = read_members(bundle)
        members["evil/unexpected.txt"] = b"not accounted for in the manifest"
        regen_checksums(members)
        write_members(bundle, members)
        proc = run_tool(VERIFY_TOOL, str(bundle))
        assert proc.returncode == 1
        assert "unexpected bundle members" in proc.stderr

    def test_checksums_coverage_gap_detected(self, synth) -> None:
        _, bundle = make_bundle(synth)
        members = read_members(bundle)
        lines = members["CHECKSUMS.sha256"].decode("utf-8").splitlines()
        kept = [line for line in lines if not line.endswith("RECOVER.md")]
        members["CHECKSUMS.sha256"] = ("\n".join(kept) + "\n").encode("utf-8")
        write_members(bundle, members)
        proc = run_tool(VERIFY_TOOL, str(bundle))
        assert proc.returncode == 1
        assert "CHECKSUMS.sha256 covers" in proc.stderr

    def test_baseline_gate_rejects_wrong_head(self, synth) -> None:
        _, bundle = make_bundle(synth)
        # A clone that drifted BEYOND the baseline (new local commit): the
        # gate must refuse — applying the patches to this base would be wrong.
        fresh = synth.root / "fresh"
        git(synth.repo, "clone", "-q", str(synth.bare), str(fresh))
        git(fresh, "config", "user.name", "Synthetic Tester")
        git(fresh, "config", "user.email", "synthetic@example.invalid")
        commit_file(fresh, "drift.txt", "drift\n", "chore: drifted beyond the baseline")
        proc = run_tool(VERIFY_TOOL, str(bundle), "--check-baseline-in", str(fresh))
        assert proc.returncode == 1
        assert "BASELINE GATE FAILED" in proc.stderr

    def test_applied_gate_rejects_premature_state(self, synth) -> None:
        _, bundle = make_bundle(synth)
        fresh = synth.root / "fresh"
        git(synth.repo, "clone", "-q", str(synth.bare), str(fresh))
        git(fresh, "checkout", "-q", synth.baseline)
        # Patches NOT applied yet: tree differs from the checkpoint tree.
        proc = run_tool(VERIFY_TOOL, str(bundle), "--verify-applied", str(fresh))
        assert proc.returncode == 1
        assert "APPLIED GATE FAILED" in proc.stderr

    def test_applied_gate_rejects_duplicate_application(self, synth) -> None:
        # git am itself must refuse a second application, and the gate must
        # still hold at exactly the checkpointed content.
        _, bundle = make_bundle(synth)
        fresh = synth.root / "fresh"
        git(synth.repo, "clone", "-q", str(synth.bare), str(fresh))
        # Own synthetic identity for the recovery clone (clean-runner
        # regression: no global Git identity exists on CI runners).
        git(fresh, "config", "user.name", "Resumed Agent")
        git(fresh, "config", "user.email", "resumed@example.invalid")
        git(fresh, "checkout", "-q", synth.baseline)
        git(fresh, "switch", "-qc", "feature/test")
        extracted = synth.root / "ckpt"
        with zipfile.ZipFile(bundle) as archive:
            archive.extractall(extracted)
        patch = str(extracted / "patches" / "0001-feat-synthetic-change.patch")
        first = git(fresh, "am", patch)
        assert first.returncode == 0
        # git am itself must REFUSE the duplicate application (conflict on
        # the already-present file); the stopped session is then aborted.
        second = git(fresh, "am", patch, check=False)
        assert second.returncode != 0
        git(fresh, "am", "--abort", check=False)
        # …and the gate proves the state is exactly the checkpoint content.
        gate = run_tool(VERIFY_TOOL, str(bundle), "--verify-applied", str(fresh))
        assert gate.returncode == 0, gate.stderr

    def test_gate_on_non_repository(self, synth) -> None:
        _, bundle = make_bundle(synth)
        plain = synth.root / "plain"
        plain.mkdir()
        proc = run_tool(VERIFY_TOOL, str(bundle), "--check-baseline-in", str(plain))
        assert proc.returncode == 1
        assert "failed" in proc.stderr
