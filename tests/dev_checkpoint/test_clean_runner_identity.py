"""Regression coverage for the clean CI-runner Git-identity scenario.

GitHub Actions runners carry NO global Git identity, and a fresh clone never
inherits the fixture repository's local ``user.*`` configuration.  Three CI
jobs (checkpoint tools, functional suite, PostgreSQL 16 integration) failed
identically with::

    Committer identity unknown
    fatal: empty ident name (...) not allowed

because synthetic recovery tests invoked ``git am`` inside fresh clones
while silently relying on the developer machine's global Git identity.

Two complementary guarantees now hold:

* the ``git()`` helper in ``ckpt_synth`` runs every test-side Git call in a
  hermetic environment — the host's global/system Git configuration files
  are disabled and inherited identity variables are stripped, so the suite
  behaves identically on a clean CI runner and on a developer machine
  (spec §9/§15: synthetic, offline, disposable; a real user's global Git
  settings are never read or modified);
* every disposable repository that creates commits (the fixture and each
  recovery clone) configures its OWN deterministic synthetic identity, so
  no commit operation ever depends on ambient configuration.
"""

from __future__ import annotations

import os
import subprocess
import zipfile

from tests.dev_checkpoint.ckpt_synth import (
    commit_file,
    git,
    git_env,
    make_bundle,
    synth_repo,  # noqa: F401 — importing registers the "synth" fixture
)

#: Deterministic synthetic identity of the recovery clones (the pattern the
#: end-to-end drill already used): distinct from the fixture repository's
#: "Synthetic Tester" identity, which guarantees ``git am`` reproduces the
#: checkpoint content with NEW commit SHAs.
RESUMED_AGENT = "Resumed Agent <resumed@example.invalid>"


class TestCleanRunnerIdentity:
    def test_clean_runner_without_identity_fails(self, synth) -> None:
        """Root-cause proof: with every identity source removed, Git refuses.

        A fresh clone with NO identity configured anywhere — the exact
        recovery scenario that broke CI — is probed through a raw subprocess
        (deliberately NOT the hardened ``git()`` helper) with no global
        config, no system config, no identity environment variables and
        auto-detection disabled.  Should the recovery clones ever lose
        their own identity, this is what CI would report again.
        """
        fresh = synth.root / "fresh-no-identity"
        git(synth.repo, "clone", "-q", str(synth.bare), str(fresh))
        stripped = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_") and key != "EMAIL"
        }
        stripped["GIT_CONFIG_GLOBAL"] = os.devnull
        stripped["GIT_CONFIG_SYSTEM"] = os.devnull
        probe = subprocess.run(
            [
                "git",
                "-C",
                str(fresh),
                "-c",
                "user.useConfigOnly=true",
                "var",
                "GIT_COMMITTER_IDENT",
            ],
            capture_output=True,
            text=True,
            env=stripped,
            check=False,
        )
        assert probe.returncode != 0
        assert "Committer identity unknown" in probe.stderr

    def test_git_am_in_fresh_clone_without_global_identity(self, synth) -> None:
        """The fix: a recovery clone carries its own deterministic identity.

        The fresh clone is created with the hardened helper (host config
        disabled) and configures ONLY its local synthetic identity — exactly
        like a clean CI-runner checkout following RECOVER.md.  Synthetic
        ``git am`` and ``git commit`` both succeed with deterministic
        author/committer identities.
        """
        _, bundle = make_bundle(synth)
        fresh = synth.root / "fresh-clean-runner"
        git(synth.repo, "clone", "-q", str(synth.bare), str(fresh))
        git(fresh, "config", "user.name", "Resumed Agent")
        git(fresh, "config", "user.email", "resumed@example.invalid")
        git(fresh, "checkout", "-q", synth.baseline)
        git(fresh, "switch", "-qc", "feature/test")
        extracted = synth.root / "ckpt-clean-runner"
        with zipfile.ZipFile(bundle) as archive:
            archive.extractall(extracted)
        applied = git(fresh, "am", str(extracted / "patches" / "0001-feat-synthetic-change.patch"))
        assert applied.returncode == 0, applied.stderr
        # Author comes from the patch (the fixture's Synthetic Tester);
        # committer is the recovery clone's own deterministic identity.
        log = git(fresh, "log", "-1", "--format=%an <%ae> | %cn <%ce>")
        assert (
            log.stdout.strip() == f"Synthetic Tester <synthetic@example.invalid> | {RESUMED_AGENT}"
        )
        # git commit through the same helper is equally self-sufficient.
        final = commit_file(fresh, "clean-runner.txt", "ok\n", "feat: no global identity needed")
        assert len(final) == 40

    def test_helper_ignores_host_identity_environment(self, synth, monkeypatch) -> None:
        """A polluted host identity environment cannot leak into the suite.

        Some developer machines and CI setups export ``GIT_COMMITTER_*`` or
        ``EMAIL``; the helper must strip them so every synthetic commit
        keeps its deterministic disposable-repository identity.
        """
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Real Person")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "real@corp.example.com")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "Real Person")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "real@corp.example.com")
        monkeypatch.setenv("EMAIL", "real@corp.example.com")
        probe = git(synth.repo, "var", "GIT_COMMITTER_IDENT")
        assert "Real Person" not in probe.stdout
        assert "real@corp.example.com" not in probe.stdout
        # The disposable repository's OWN identity wins.
        assert probe.stdout.startswith("Synthetic Tester <synthetic@example.invalid>")
        # Structural: the hermetic environment is guaranteed on every call.
        scoped = git_env()
        assert scoped["GIT_CONFIG_GLOBAL"] == os.devnull
        assert scoped["GIT_CONFIG_SYSTEM"] == os.devnull
        for leaked in (
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
            "EMAIL",
        ):
            assert leaked not in scoped
