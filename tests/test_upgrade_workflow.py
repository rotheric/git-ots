"""Run the upgrade workflow shell against a real local Git remote."""

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/upgrade-proofs.yml"


def test_upgrade_workflow_has_no_push_trigger_and_uses_separate_concurrency():
    text = WORKFLOW.read_text()
    triggers = text.split("permissions:")[0]
    assert "schedule:" in triggers and "workflow_dispatch:" in triggers
    assert "push:" not in triggers and "pull_request:" not in triggers
    assert 'cron: "43 * * * *"' in triggers
    assert "group: upgrade-proofs\n" in text
    assert "cancel-in-progress: false" in text
    assert "ref: master" in text and "fetch-depth: 0" in text
    assert "secrets.RELEASE_PUSH_TOKEN" in text
    assert "timeout-minutes: 20" in text
    uses = re.findall(r"uses:\s+([^\s#]+)", text)
    assert uses and all(re.search(r"@[0-9a-f]{40}$", use) for use in uses)


@pytest.mark.parametrize("mode", ["unchanged", "updated", "failed", "race"])
def test_upgrade_push_behavior(tmp_path, mode):
    repo = tmp_path / "work"
    remote = tmp_path / "origin.git"

    def git(*args, cwd=repo):
        return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()

    repo.mkdir()
    git("init", "-b", "master")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    proof = repo / ".opentimestamps/test.ots"
    proof.parent.mkdir()
    proof.write_text("pending")
    git("add", ".")
    git("commit", "-m", "Initial proof")
    git("init", "--bare", str(remote))
    git("remote", "add", "origin", str(remote))
    git("push", "-u", "origin", "master")
    before = git("rev-parse", "HEAD")
    remote_before = before
    if mode == "race":
        other = tmp_path / "other"
        git("clone", "-b", "master", str(remote), str(other))
        git("config", "user.name", "Other", cwd=other)
        git("config", "user.email", "other@example.invalid", cwd=other)
        git("commit", "--allow-empty", "-m", "Concurrent change", cwd=other)
        git("push", "origin", "master", cwd=other)
        remote_before = git("rev-parse", "HEAD", cwd=other)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/bash\nset -eu\n"
        'test "$*" = "run --frozen --with opentimestamps-client==0.7.2 git ots upgrade"\n'
        'test "$(git config ots.proofCommit)" = true\n'
        'test "$(git config ots.squashUpgradeCommits)" = false\n'
        'if test "$UPGRADE_MODE" = unchanged; then exit 0; fi\n'
        "echo upgraded > .opentimestamps/test.ots\n"
        "git add .opentimestamps\ngit commit -m 'Upgrade proof'\n"
        'if test "$UPGRADE_MODE" = failed; then exit 5; fi\n'
    )
    uv.chmod(0o755)
    if mode == "unchanged":
        # Even contacting the remote must be skipped when no proof changed.
        git("remote", "set-url", "origin", str(tmp_path / "missing.git"))
    # Record successful updates independently of the local commit state.
    hook = remote / "hooks/pre-receive"
    hook.write_text('#!/bin/sh\ntouch "$(git rev-parse --absolute-git-dir)/pushed"\n')
    hook.chmod(0o755)
    block = WORKFLOW.read_text().split("        run: |\n")[1]
    script = "\n".join(line[10:] for line in block.splitlines())
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "UPGRADE_MODE": mode,
        },
        check=False,
    )
    remote_after = git("--git-dir", str(remote), "rev-parse", "master")
    assert (result.returncode == 0) == (mode in {"unchanged", "updated"})
    if mode == "updated":
        assert remote_after == git("rev-parse", "HEAD") != before
        assert git("diff", "--name-only", before, remote_after) == (
            ".opentimestamps/test.ots"
        )
        assert (remote / "pushed").exists()
    else:
        assert remote_after == remote_before
        assert not (remote / "pushed").exists()
    assert git("--git-dir", str(remote), "tag", "--list") == ""
