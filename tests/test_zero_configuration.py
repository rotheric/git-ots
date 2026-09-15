"""Tests for running an unconfigured repository (FS-0014).

These replace tests/test_init.py. `git-ots init` was removed because writing a
starter file into the worktree root put the tool into a state its own
clean-worktree precondition refuses to run against: init created an untracked
git-ots.toml, `is_worktree_clean` counts untracked files, and `run` then
reported "worktree has uncommitted changes" on a repository whose only change
was the file init had just written. Both escapes were bad -- committing the
file makes execution surface clonable, and disabling require_clean_worktree
turns off a real safety property to work around a bootstrap artifact.

The fix was to stop writing anything: the defaults are the configuration.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from git_ots.cli import main
from git_ots.config import DEFAULT_MAX_AGE


def _git(args: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main", "."], cwd=path)
    _git(["config", "user.email", "test@example.com"], cwd=path)
    _git(["config", "user.name", "Test"], cwd=path)


def _commit(
    repo: Path, *, paths: list[str], message: str, date: datetime | None = None
) -> str:
    for rel in paths:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{rel}\n")
    _git(["add", *paths], cwd=repo)
    env = None
    if date is not None:
        env = {
            **os.environ,
            "GIT_COMMITTER_DATE": date.strftime("%Y-%m-%d %H:%M:%S %z"),
        }
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return _git(["rev-parse", "HEAD"], cwd=repo).strip()


def test_there_is_no_subcommand_that_writes_configuration(
    capsys: pytest.CaptureFixture,
) -> None:
    """A guard against the bootstrap deadlock coming back by accident.

    Any subcommand that creates a file in the worktree reintroduces it, so the
    property under test is the absence of the command, not the absence of the
    old implementation.
    """
    with pytest.raises(SystemExit):
        main(argv=["init"])
    assert "invalid choice: 'init'" in capsys.readouterr().err


def test_status_runs_on_a_repository_with_no_configuration(
    capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """The first command an operator types must work before anything is set up."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A")

    assert main(argv=["status"], cwd=repo) == 0

    out = capsys.readouterr().out
    assert "source ref:" in out
    assert "pending meaningful: 1" in out
    assert not (repo / "git-ots.toml").exists()


def test_an_unconfigured_repository_uses_the_default_max_age_policy(
    capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """The built-in policy is `max_age = "24h"`, and it decides real runs.

    Driven through `run --dry-run` rather than load_config so that the default
    is asserted where it takes effect: a default that never reaches the policy
    evaluator would still satisfy a configuration-level assertion.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    committed_at = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    _commit(repo, paths=["a.txt"], message="A", date=committed_at)

    # One minute short of the default: nothing is due.
    not_yet = committed_at + DEFAULT_MAX_AGE - timedelta(minutes=1)
    assert main(argv=["run", "--dry-run"], cwd=repo, now=not_yet) == 0
    assert "No timestamp required" in capsys.readouterr().out

    # One minute past it: the max_age trigger fires.
    due = committed_at + DEFAULT_MAX_AGE + timedelta(minutes=1)
    assert main(argv=["run", "--dry-run"], cwd=repo, now=due) == 0
    out = capsys.readouterr().out
    assert "Selected:" in out
    assert "max_age" in out


def test_a_first_run_is_not_blocked_by_its_own_bootstrap(
    tmp_path: Path,
) -> None:
    """The regression this change exists for.

    `init` wrote git-ots.toml into the worktree root, `is_worktree_clean`
    counts untracked files, and require_clean_worktree defaults to true -- so
    the documented first-run sequence produced "worktree has uncommitted
    changes" on a repository the operator had just left clean. Asserting on the
    porcelain status rather than on the absence of one filename keeps the guard
    honest if the bootstrap ever writes something under a different name.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    committed_at = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    _commit(repo, paths=["a.txt"], message="A", date=committed_at)
    before = _git(["status", "--porcelain", "--untracked-files=all"], cwd=repo)
    assert before == ""

    due = committed_at + DEFAULT_MAX_AGE + timedelta(minutes=1)
    assert main(argv=["status"], cwd=repo, now=due) == 0
    assert main(argv=["run", "--dry-run"], cwd=repo, now=due) == 0

    after = _git(["status", "--porcelain", "--untracked-files=all"], cwd=repo)
    assert after == before, "inspecting an unconfigured repository must not dirty it"


def test_status_warns_when_the_opentimestamps_client_is_missing(
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preflight that `init` used to perform, moved to where it repeats.

    A client present when a repository was set up can be uninstalled or absent
    on the machine the scheduler runs on, so the check belongs on the command
    an operator reaches for -- not on a one-shot bootstrap.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A")

    monkeypatch.setattr("git_ots.cli.shutil.which", lambda cmd: None)

    assert main(argv=["status"], cwd=repo) == 0

    err = capsys.readouterr().err
    assert "ots" in err
    assert "run" in err
    assert "install" in err.lower()


def test_status_is_silent_about_a_client_that_is_present(
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The warning must discriminate. A check that fires unconditionally reads
    as noise and stops being read at all."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A")

    monkeypatch.setattr("git_ots.cli.shutil.which", lambda cmd: "/usr/bin/ots")

    assert main(argv=["status"], cwd=repo) == 0
    assert "not found in PATH" not in capsys.readouterr().err
