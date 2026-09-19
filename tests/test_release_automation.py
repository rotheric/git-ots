"""Exercise release selection against real Git history."""

import os
import runpy
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
select = runpy.run_path(str(ROOT / ".github/scripts/release_target.py"))["select"]


@pytest.fixture
def history(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()

    git("init", "-b", "master")
    git("config", "user.name", "Release test")
    git("config", "user.email", "release@example.invalid")
    version_file = repo / "src/git_ots/__init__.py"
    version_file.parent.mkdir(parents=True)

    def commit(version):
        version_file.write_text(f'__version__ = "{version}"\n')
        git("add", ".")
        git("commit", "--allow-empty", "-m", version)
        sha = git("rev-parse", "HEAD")
        git("update-ref", "refs/remotes/origin/master", sha)
        return sha

    return git, commit


def event(before, after):
    return {"ref": "refs/heads/master", "before": before, "after": after}


def test_version_bump_uses_event_commit_even_when_master_has_moved(history):
    _, commit = history
    old = commit("0.0.1")
    target = commit("0.0.2")
    commit("0.0.3")
    assert select(event(old, target), "push") == (target, "v0.0.2")


def test_proof_only_commit_does_not_release_again(history):
    git, commit = history
    old = commit("0.0.2")
    Path(".opentimestamps").mkdir()
    Path(".opentimestamps/proof.ots").write_bytes(b"proof")
    new = commit("0.0.2")
    assert git("diff", "--name-only", old, new) == ".opentimestamps/proof.ots"
    assert select(event(old, new), "push") is None


def test_retries_accept_same_tag_but_reject_moved_tag(history):
    git, commit = history
    old = commit("0.0.1")
    target = commit("0.0.2")
    git("tag", "-a", "v0.0.2", target, "-m", "Release")
    assert select(event(old, target), "push") == (target, "v0.0.2")
    git("tag", "-f", "v0.0.2", old)
    with pytest.raises(ValueError, match="another commit"):
        select(event(old, target), "push")


@pytest.mark.parametrize("version", ["0.0.1", "0.0.0"])
def test_version_decrease_is_rejected(history, version):
    _, commit = history
    old = commit("0.0.2")
    target = commit(version)
    with pytest.raises(ValueError, match="must increase"):
        select(event(old, target), "push")


def test_manual_recovery_uses_existing_tag(history):
    git, commit = history
    target = commit("0.0.2")
    git("tag", "v0.0.2")
    commit("0.0.3")
    assert select({"inputs": {"release_tag": "v0.0.2"}}, "workflow_dispatch") == (
        target,
        "v0.0.2",
    )


def test_manual_recovery_rejects_mismatched_version(history):
    git, commit = history
    commit("0.0.2")
    git("tag", "v0.0.3")
    with pytest.raises(ValueError, match="disagree"):
        select({"inputs": {"release_tag": "v0.0.3"}}, "workflow_dispatch")


def test_release_must_belong_to_master(history):
    git, commit = history
    base = commit("0.0.1")
    other = commit("0.0.2")
    git("update-ref", "refs/remotes/origin/master", base)
    with pytest.raises(subprocess.CalledProcessError):
        select(event(base, other), "push")


def test_tag_push_cannot_select_a_release(history):
    _, commit = history
    target = commit("0.0.2")
    tag_event = event(target, target)
    tag_event["ref"] = "refs/tags/v0.0.2"
    assert select(tag_event, "push") is None


def test_workflow_guards_against_cascades_and_publishes_after_timestamp():
    text = (ROOT / ".github/workflows/release.yml").read_text()
    triggers = text.split("permissions:", 1)[0]
    assert "branches: [master]" in triggers
    assert 'paths: ["src/git_ots/__init__.py"]' in triggers
    assert "tags:" not in triggers
    assert "group: release\n" in text
    assert "needs: [select, timestamp]" in text
    assert "git push --atomic --follow-tags" in text
    assert "--force" not in text


@pytest.mark.parametrize("remote_moves", [False, True])
def test_timestamp_push_is_atomic_and_retryable(history, tmp_path, remote_moves):
    git, commit = history
    target = commit("0.0.2")
    remote = tmp_path / "origin.git"
    git("init", "--bare", str(remote))
    git("remote", "add", "origin", str(remote))
    git("push", "origin", "master")
    if remote_moves:
        newer = commit("0.0.2")
        git("push", "origin", "master")
        git("reset", "--hard", target)
    else:
        newer = target

    # Only the calendar submission is substituted. Run the workflow's actual
    # Git commands against a bare remote, including its atomic push and retry.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/bash\nset -eu\n"
        'if test -f ".opentimestamps/$RELEASE_TARGET.ots"; then exit 0; fi\n'
        "mkdir -p .opentimestamps\n"
        'echo proof > ".opentimestamps/$RELEASE_TARGET.ots"\n'
        'echo manifest > ".opentimestamps/$RELEASE_TARGET.json"\n'
        "git add .opentimestamps\n"
        "git commit -m 'Store proof'\n"
        'git tag -a ots/test "$RELEASE_TARGET" -m Timestamp\n'
    )
    uv.chmod(0o755)
    text = (ROOT / ".github/workflows/release.yml").read_text()
    block = text.split("      - name: Store timestamp and create version tag\n")[1]
    block = block.split("        run: |\n")[1].split("\n  build:")[0]
    script = "\n".join(line[10:] for line in block.splitlines())
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RELEASE_TARGET": target,
        "RELEASE_TAG": "v0.0.2",
    }
    result = subprocess.run(["bash", "-eu", "-c", script], env=env, check=False)
    if remote_moves:
        assert result.returncode != 0
        assert git("--git-dir", str(remote), "rev-parse", "master") == newer
        assert git("--git-dir", str(remote), "tag", "--list") == ""
    else:
        assert result.returncode == 0
        proof_commit = git("--git-dir", str(remote), "rev-parse", "master")
        subprocess.run(["bash", "-eu", "-c", script], env=env, check=True)
        assert git("--git-dir", str(remote), "rev-parse", "master") == proof_commit
        assert git("--git-dir", str(remote), "rev-parse", "v0.0.2^{commit}") == target
        assert git("--git-dir", str(remote), "tag", "--list") == "ots/test\nv0.0.2"
