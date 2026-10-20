"""Creation-side behaviour of scripts/dev_checkpoint.py (spec §6/§7/§15).

Positive: clean-tree bundling, manifest completeness, worklog snapshot,
self-verification, dry-run, zero-patch baseline checkpoints.

Negative matrix (each must refuse EXPLICITLY, never silently proceed):
existing uncommitted changes, unexpected untracked files, attempted
overwrite, unsafe output destination, non-repository, missing git,
secret-bearing content without acknowledgement.
"""

from __future__ import annotations

import json

from tests.dev_checkpoint.ckpt_synth import (
    CREATE_TOOL,
    FIXED_CLOCK,
    commit_file,
    git,
    make_bundle,
    read_members,
    run_tool,
    synth_repo,  # noqa: F401 — importing registers the "synth" fixture
)

# ---------------------------------------------------------------------------
# Positive
# ---------------------------------------------------------------------------


class TestCreatePositive:
    def test_bundle_structure_and_manifest(self, synth) -> None:
        proc, bundle = make_bundle(
            synth,
            worklog=synth.worklog,
            extra_args=[
                "--milestone-done",
                "implementation",
                "--milestone-pending",
                "integration tests",
                "--test",
                "python -m pytest -q",
                "--test-result",
                "488 passed",
                "--env-dep",
                "PostgreSQL 16 @127.0.0.1:5432",
                "--next-action",
                "run the PG16 suite",
            ],
        )
        assert "BUNDLE_SHA256=" in proc.stdout
        assert "LOCAL_ONLY" in proc.stdout
        members = read_members(bundle)
        assert set(members) == {
            "MANIFEST.json",
            "CHECKSUMS.sha256",
            "RECOVER.md",
            "WORKLOG.md",
            "patches/0001-feat-synthetic-change.patch",
        }
        manifest = json.loads(members["MANIFEST.json"])
        assert manifest["schema_version"] == 1
        repo = manifest["repository"]
        assert repo["baseline_commit"] == synth.baseline
        assert repo["head_commit"] == synth.head
        assert repo["head_tree"] == synth.head_tree
        assert repo["branch"] == "feature/test"
        assert repo["commit_count"] == 1
        assert manifest["patches"][0]["commit"] == synth.head
        assert manifest["worklog"]["dirty"] is False
        assert manifest["worklog"]["sha256"]
        assert manifest["milestones"] == {
            "completed": ["implementation"],
            "pending": ["integration tests"],
        }
        assert manifest["tests"] == [{"command": "python -m pytest -q", "result": "488 passed"}]
        assert manifest["environment"] == ["PostgreSQL 16 @127.0.0.1:5432"]
        assert manifest["next_action"] == "run the PG16 suite"
        assert manifest["delivery"]["status"] == "LOCAL_ONLY"
        assert manifest["secret_scan"]["findings"] == 0
        # RECOVER.md embeds the exact gates and hashes.
        recover = members["RECOVER.md"].decode("utf-8")
        assert synth.baseline in recover
        assert synth.head in recover
        assert synth.head_tree in recover
        assert "HEAD^{tree}" in recover
        assert "git am" in recover
        # Worklog snapshot content is verbatim.
        assert members["WORKLOG.md"] == synth.worklog.read_bytes()

    def test_deterministic_name_and_no_overwrite(self, synth) -> None:
        _, first = make_bundle(synth, worklog=synth.worklog)
        assert first is not None
        assert first.name == f"checkpoint-synth-20261020T000000Z-{synth.head[:8]}.zip"
        original = first.read_bytes()
        # Same clock + same head ⇒ same target name ⇒ refusal, file untouched.
        proc, again = make_bundle(synth, worklog=synth.worklog, expect_success=False)
        assert proc.returncode == 2, proc.stderr
        assert "refusing to overwrite" in (proc.stderr + proc.stdout)
        assert again is None  # a refusal prints no BUNDLE= line
        assert first.read_bytes() == original

    def test_new_checkpoint_gets_new_name(self, synth) -> None:
        _, first = make_bundle(synth)
        # One more commit ⇒ different head ⇒ different filename even with
        # the same fixed clock.
        commit_file(synth.repo, "more.txt", "more\n", "feat: second change")
        proc, second = make_bundle(synth)
        assert second.name != first.name
        assert proc.returncode == 0

    def test_dry_run_writes_nothing(self, synth) -> None:
        proc = run_tool(
            CREATE_TOOL,
            "--repo",
            str(synth.repo),
            "--output-dir",
            str(synth.out),
            "--task-slug",
            "synth",
            "--dry-run",
            env={"AIC_CHECKPOINT_CLOCK": FIXED_CLOCK},
        )
        assert proc.returncode == 0
        assert "DRY-RUN" in proc.stdout
        assert not list(synth.out.glob("checkpoint-*.zip"))
        assert not list(synth.out.glob("*.part"))

    def test_zero_patch_baseline_checkpoint(self, synth) -> None:
        # A checkpoint at the baseline (HEAD == baseline, no commits) is a
        # valid, explicitly-recorded state — e.g. "audit completed, nothing
        # implemented yet".
        git(synth.repo, "checkout", "-q", "main")
        proc = run_tool(
            CREATE_TOOL,
            "--repo",
            str(synth.repo),
            "--output-dir",
            str(synth.out),
            "--task-slug",
            "baseline-state",
            "--baseline",
            synth.baseline,
            env={"AIC_CHECKPOINT_CLOCK": FIXED_CLOCK},
        )
        assert proc.returncode == 0, proc.stderr
        bundle = next(synth.out.glob("checkpoint-baseline-state-*.zip"))
        members = read_members(bundle)
        manifest = json.loads(members["MANIFEST.json"])
        assert manifest["repository"]["commit_count"] == 0
        assert manifest["patches"] == []
        assert "no patches to apply" in members["RECOVER.md"].decode("utf-8")

    def test_stale_part_file_is_garbage_collected(self, synth) -> None:
        stale = synth.out / f"checkpoint-synth-20261020T000000Z-{synth.head[:8]}.zip.part"
        stale.write_bytes(b"garbage from an interrupted run")
        _, bundle = make_bundle(synth)
        assert bundle.is_file()
        assert not stale.exists()

    def test_allow_dirty_worklog_tolerates_only_the_worklog(self, synth) -> None:
        # The worklog lives INSIDE the repo and is dirty — the single
        # explicit exception (protocol §6).
        repo_worklog = synth.repo / "WORKLOG.md"
        repo_worklog.write_text("# in-repo worklog\n", encoding="utf-8")
        proc = run_tool(
            CREATE_TOOL,
            "--repo",
            str(synth.repo),
            "--output-dir",
            str(synth.out),
            "--task-slug",
            "synth",
            "--worklog",
            str(repo_worklog),
            "--allow-dirty-worklog",
            env={"AIC_CHECKPOINT_CLOCK": FIXED_CLOCK},
        )
        assert proc.returncode == 0, proc.stderr
        bundle = synth.out / f"checkpoint-synth-20261020T000000Z-{synth.head[:8]}.zip"
        manifest = json.loads(read_members(bundle)["MANIFEST.json"])
        assert manifest["worklog"]["dirty"] is True
        # Any OTHER dirt still refuses, even with the flag.
        (synth.repo / "other.txt").write_text("unrelated dirt\n", encoding="utf-8")
        proc = run_tool(
            CREATE_TOOL,
            "--repo",
            str(synth.repo),
            "--output-dir",
            str(synth.out),
            "--task-slug",
            "synth2",
            "--worklog",
            str(repo_worklog),
            "--allow-dirty-worklog",
            env={"AIC_CHECKPOINT_CLOCK": FIXED_CLOCK},
        )
        assert proc.returncode == 2
        assert "other dirty entries remain refused" in (proc.stderr + proc.stdout)


# ---------------------------------------------------------------------------
# Negative matrix (spec §15 — creation side)
# ---------------------------------------------------------------------------


class TestCreateNegative:
    def test_refuses_uncommitted_changes(self, synth) -> None:
        (synth.repo / "README.md").write_text("modified\n", encoding="utf-8")
        proc = run_tool(
            CREATE_TOOL,
            "--repo",
            str(synth.repo),
            "--output-dir",
            str(synth.out),
        )
        assert proc.returncode == 2
        assert "work tree is not clean" in proc.stderr

    def test_refuses_untracked_files(self, synth) -> None:
        (synth.repo / "untracked.txt").write_text("surprise\n", encoding="utf-8")
        proc = run_tool(
            CREATE_TOOL,
            "--repo",
            str(synth.repo),
            "--output-dir",
            str(synth.out),
        )
        assert proc.returncode == 2
        assert "untracked.txt" in proc.stderr

    def test_refuses_output_inside_repository(self, synth) -> None:
        proc = run_tool(
            CREATE_TOOL,
            "--repo",
            str(synth.repo),
            "--output-dir",
            str(synth.repo / "checkpoints"),
        )
        assert proc.returncode == 2
        assert "INSIDE the repository work tree" in proc.stderr

    def test_refuses_non_repository(self, tmp_path) -> None:
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        (plain / "file.txt").write_text("no git here\n", encoding="utf-8")
        proc = run_tool(
            CREATE_TOOL,
            "--repo",
            str(plain),
            "--output-dir",
            str(tmp_path / "out"),
        )
        assert proc.returncode == 2
        assert "not a Git repository" in proc.stderr

    def test_refuses_missing_git(self, synth) -> None:
        proc = run_tool(
            CREATE_TOOL,
            "--repo",
            str(synth.repo),
            "--output-dir",
            str(synth.out),
            env={"PATH": "/nonexistent"},
        )
        assert proc.returncode == 2
        assert "git executable not found" in proc.stderr
        assert not list(synth.out.glob("checkpoint-*.zip"))

    def test_refuses_baseline_not_ancestor(self, synth) -> None:
        # Build an unrelated commit, then ask for a baseline that is not an
        # ancestor of HEAD.
        git(synth.repo, "checkout", "-q", "main")
        unrelated = commit_file(
            synth.repo, "other-branch.txt", "x\n", "chore: unrelated branch commit"
        )
        git(synth.repo, "checkout", "-q", "feature/test")
        proc = run_tool(
            CREATE_TOOL,
            "--repo",
            str(synth.repo),
            "--output-dir",
            str(synth.out),
            "--baseline",
            unrelated,
        )
        assert proc.returncode == 2
        assert "not an ancestor of HEAD" in proc.stderr

    def test_secret_scan_refuses_age_key_and_acknowledges(self, synth) -> None:
        commit_file(
            synth.repo,
            "leak.txt",
            "AGE-SECRET-KEY-1SYNTHETICINVALIDNOTAREALKEY0000000000abcd\n",
            "chore: synthetic key material",
        )
        base_args = [
            "--repo",
            str(synth.repo),
            "--output-dir",
            str(synth.out),
            "--task-slug",
            "leaky",
        ]
        proc = run_tool(CREATE_TOOL, *base_args)
        assert proc.returncode == 3
        assert "age-private-key" in proc.stderr
        assert "refusing to bundle" in proc.stderr
        assert not list(synth.out.glob("checkpoint-leaky-*.zip"))
        # Explicit acknowledgement proceeds AND is recorded in the manifest.
        proc = run_tool(CREATE_TOOL, *base_args, "--acknowledge-scan-findings")
        assert proc.returncode == 0, proc.stderr
        bundle = next(synth.out.glob("checkpoint-leaky-*.zip"))
        manifest = json.loads(read_members(bundle)["MANIFEST.json"])
        assert manifest["secret_scan"]["acknowledged"] is True
        assert manifest["secret_scan"]["findings"] >= 1

    def test_secret_scan_catches_pem_header(self, synth) -> None:
        commit_file(
            synth.repo,
            "pem.txt",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIBogIBAAJBAK\n-----END RSA PRIVATE KEY-----\n",
            "chore: synthetic pem",
        )
        proc = run_tool(
            CREATE_TOOL,
            "--repo",
            str(synth.repo),
            "--output-dir",
            str(synth.out),
            "--task-slug",
            "pem",
        )
        assert proc.returncode == 3
        assert "pem-private-key" in proc.stderr

    def test_secret_scan_catches_credential_assignment(self, synth) -> None:
        commit_file(
            synth.repo,
            "settings.txt",
            "api_key = abcdefghijklmnopqrstuvwxyz123456\n",
            "chore: synthetic credential",
        )
        proc = run_tool(
            CREATE_TOOL,
            "--repo",
            str(synth.repo),
            "--output-dir",
            str(synth.out),
            "--task-slug",
            "creddy",
        )
        assert proc.returncode == 3
        assert "credential-assignment" in proc.stderr

    def test_secret_scan_whitelists_bare_hex_constants(self, synth) -> None:
        # Bare hex constants of SHA-256 / git object ids appear legitimately
        # in documentation and must not trip the scan.
        commit_file(
            synth.repo,
            "hashes.txt",
            "sha256 constant: "
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n"
            "git object: 0123456789abcdef0123456789abcdef01234567\n",
            "docs: legitimate hash constants",
        )
        proc = run_tool(
            CREATE_TOOL,
            "--repo",
            str(synth.repo),
            "--output-dir",
            str(synth.out),
            "--task-slug",
            "hashy",
        )
        assert proc.returncode == 0, proc.stderr

    def test_secret_scan_tolerates_slash_joined_identifier_lists(self, synth) -> None:
        # REAL false positives observed while dogfooding the tool on the
        # actual task worklog: slash-joined identifier lists are long
        # [A-Za-z0-9+/] runs but never mix digits with both cases.
        commit_file(
            synth.repo,
            "identifiers.txt",
            "exports: DrillCleanupError/PublishedSetCorrupted/"
            "DeadlineExceeded/CanonicalConnection/BackupError\n"
            "params: host/hostaddr/port/service/user/dbname/options/passfile\n"
            "dirs: unit/integration/operational/privacy/recovery/security\n",
            "docs: identifier lists are not key material",
        )
        proc = run_tool(
            CREATE_TOOL,
            "--repo",
            str(synth.repo),
            "--output-dir",
            str(synth.out),
            "--task-slug",
            "identy",
        )
        assert proc.returncode == 0, proc.stderr

    def test_secret_scan_catches_key_shaped_base64(self, synth) -> None:
        # Mixed-case + digits base64 in a >40-char literal IS key-shaped.
        commit_file(
            synth.repo,
            "keymat.txt",
            "the key is c3VwZXJzZWNyZXQxMjM0NTY3ODkwQUJDREVGR0hJSktMTU5PUFF\n",
            "chore: synthetic key material",
        )
        proc = run_tool(
            CREATE_TOOL,
            "--repo",
            str(synth.repo),
            "--output-dir",
            str(synth.out),
            "--task-slug",
            "keymat",
        )
        assert proc.returncode == 3
        assert "high-entropy-literal" in proc.stderr
