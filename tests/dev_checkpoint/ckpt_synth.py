"""Shared synthetic-repository helpers for the checkpoint-tool test suite.

Everything here is synthetic, offline, and disposable (spec §9/§15): the
fixtures build throwaway Git repositories under pytest's ``tmp_path`` and
exercise the tools exactly the way an operator would — as subprocesses.
No network, no real repository, no credentials.

This is a plain module (NOT conftest.py) deliberately: the repo's existing
suite imports the root ``conftest`` by its bare module name, and a second
conftest.py in a subdirectory would collide with it under pytest's default
import mode.  Importing the ``synth`` fixture from this module registers it
for the importing test module — the established pattern
(``tests/operational/backup_synth.py``).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
CREATE_TOOL = SCRIPTS / "dev_checkpoint.py"
VERIFY_TOOL = SCRIPTS / "dev_checkpoint_verify.py"
RESUME_TOOL = SCRIPTS / "dev_resume_check.py"

#: Deterministic bundle-name clock (test hook documented in dev_checkpoint.py).
FIXED_CLOCK = "2026-10-20T00:00:00Z"


def run_tool(
    script: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one of the checkpoint tools as a subprocess and never raise."""
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        env=merged,
        check=False,
    )


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def commit_file(repo: Path, name: str, content: str, message: str) -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture(name="synth")
def synth_repo(tmp_path: Path) -> SimpleNamespace:
    """A disposable synthetic repository with a surviving bare origin.

    Layout: ``repo`` (working tree, branch ``feature/test`` with one commit
    on top of ``main``) and ``origin.git`` (bare clone — the stand-in for
    the public GitHub remote that survives sandbox destruction).
    """
    root = tmp_path / "synth"
    root.mkdir()
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Synthetic Tester")
    git(repo, "config", "user.email", "synthetic@example.invalid")
    baseline = commit_file(repo, "README.md", "baseline\n", "chore: baseline commit")
    bare = root / "origin.git"
    git(repo, "clone", "-q", "--bare", ".", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "fetch", "-q", "origin")
    git(repo, "checkout", "-qb", "feature/test")
    head = commit_file(repo, "feature.txt", "feature work\n", "feat: synthetic change")
    worklog = root / "worklog.md"
    worklog.write_text(
        "# synthetic worklog\n\n- task: checkpoint tools test\n- state: synthetic\n",
        encoding="utf-8",
    )
    out = root / "checkpoints"
    out.mkdir()
    return SimpleNamespace(
        root=root,
        repo=repo,
        bare=bare,
        baseline=baseline,
        head=head,
        head_tree=git(repo, "rev-parse", "HEAD^{tree}").stdout.strip(),
        worklog=worklog,
        out=out,
    )


def make_bundle(
    synth_obj: SimpleNamespace,
    *,
    clock: str | None = FIXED_CLOCK,
    extra_args: list[str] | None = None,
    worklog: Path | None = None,
    expect_success: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path | None]:
    """Create a checkpoint bundle from the synthetic repo.

    ``expect_success=False`` is for negative cases (refusal must leave no
    new bundle; the returned path is then the newest EXISTING bundle, if
    any).
    """
    args = [
        "--repo",
        str(synth_obj.repo),
        "--output-dir",
        str(synth_obj.out),
        "--task-slug",
        "synth",
    ]
    if worklog is not None:
        args += ["--worklog", str(worklog)]
    if extra_args:
        args += extra_args
    env = {"AIC_CHECKPOINT_CLOCK": clock} if clock else None
    proc = run_tool(CREATE_TOOL, *args, env=env)
    if expect_success:
        assert proc.returncode == 0, f"creation failed:\n{proc.stdout}\n{proc.stderr}"
    # The tool prints the exact bundle path — name ordering is NOT a
    # reliable recency signal (same-clock bundles differ only by head sha).
    bundle: Path | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("BUNDLE="):
            bundle = Path(line.split("=", 1)[1])
            break
    if expect_success:
        assert bundle is not None, f"no BUNDLE= line in output:\n{proc.stdout}"
    return proc, bundle


# ---------------------------------------------------------------------------
# Bundle tampering helpers (for the §15 negative matrix)
# ---------------------------------------------------------------------------


def read_members(bundle: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(bundle) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def write_members(bundle: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def regen_checksums(members: dict[str, bytes]) -> None:
    """Regenerate CHECKSUMS.sha256 so it consistently covers the members.

    Used by tampering tests to get PAST the checksum layer and probe the
    deeper manifest validations (a real attacker rewriting everything
    consistently is the documented outer-sha256 boundary, §6).
    """
    lines = [f"{sha256_bytes(members['MANIFEST.json'])}  MANIFEST.json"]
    for name in sorted(members):
        if name in {"MANIFEST.json", "CHECKSUMS.sha256"}:
            continue
        lines.append(f"{sha256_bytes(members[name])}  {name}")
    members["CHECKSUMS.sha256"] = ("\n".join(lines) + "\n").encode("utf-8")


def tamper_manifest(bundle: Path, edit) -> None:
    """Edit MANIFEST.json (dict in / dict out) and regenerate CHECKSUMS."""
    members = read_members(bundle)
    manifest = json.loads(members["MANIFEST.json"])
    edit(manifest)
    members["MANIFEST.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    regen_checksums(members)
    write_members(bundle, members)
