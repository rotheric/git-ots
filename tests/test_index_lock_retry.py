"""Tests for waiting out `.git/index.lock` contention.

An unattended timestamping job and a human working in the same repository
contend for the index as a matter of course. Before this, the first collision
aborted the run -- and it aborts it *after* the OpenTimestamps submission has
already happened, which is the most expensive moment to give up.

The discipline these tests pin is narrowness: only lock contention is retried.
A rejected hook, a failing signer, or a bad pathspec must still fail on the
first attempt, because retrying those turns a fast, honest failure into a slow
one and buries the operator's real problem under a delay.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from git_ots.git import (
    GitCommandError,
    GitCommandTimeoutError,
    GitRunner,
    is_index_lock_contention,
    run_with_index_lock_retry,
    stage_paths,
)

_LOCK_STDERR = (
    "fatal: Unable to create '/repo/.git/index.lock': File exists.\n"
    "\n"
    "Another git process seems to be running in this repository, or the lock "
    "file may be stale"
)
_REF_LOCK_STDERR = "fatal: cannot lock ref 'refs/heads/main': File exists."
_HOOK_STDERR = "pre-commit ERROR: untracked immutable file, not part of this commit"


class _ScriptedRunner:
    """A process runner that replays a scripted sequence of results."""

    def __init__(self, results: list[tuple[int, str]]) -> None:
        self._results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, cwd, stdin=None):
        self.calls.append(list(argv))
        exit_code, stderr = self._results.pop(0)
        return exit_code, "", stderr


class _Clock:
    """A monotonic clock that only advances when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def _runner(results: list[tuple[int, str]]) -> tuple[GitRunner, _ScriptedRunner]:
    process_runner = _ScriptedRunner(results)
    return GitRunner(cwd=Path("/repo"), process_runner=process_runner), process_runner


def test_recognises_an_index_lock_collision() -> None:
    error = GitCommandError(argv=["git", "add"], exit_code=128, stderr=_LOCK_STDERR)
    assert is_index_lock_contention(error) is True


def test_recognises_a_ref_lock_collision() -> None:
    error = GitCommandError(argv=["git", "tag"], exit_code=128, stderr=_REF_LOCK_STDERR)
    assert is_index_lock_contention(error) is True


def test_does_not_mistake_a_rejected_hook_for_lock_contention() -> None:
    error = GitCommandError(argv=["git", "commit"], exit_code=1, stderr=_HOOK_STDERR)
    assert is_index_lock_contention(error) is False


def test_does_not_mistake_a_timeout_for_lock_contention() -> None:
    """A killed command is a ceiling problem, not a contention problem.

    `GitCommandTimeoutError` subclasses `GitCommandError`, and its own message
    for `add`/`commit` names the index lock -- because SIGKILL denies git the
    chance to clean `.git/index.lock` up. So its stderr matches the contention
    markers exactly. Without the explicit check the retry budget would be spent
    re-running a command that will be killed again, on a lock the previous
    attempt created and nothing will release.
    """
    error = GitCommandTimeoutError(argv=["git", "add"], timeout=1.0)

    assert "index.lock" in str(error).lower()
    assert is_index_lock_contention(error) is False


def test_retries_until_the_lock_clears() -> None:
    runner, process_runner = _runner(
        [
            (128, _LOCK_STDERR),
            (128, _LOCK_STDERR),
            (0, ""),
        ]
    )
    clock = _Clock()

    result = run_with_index_lock_retry(
        runner,
        ["add", "--", "a.txt"],
        retry_window=60.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result.exit_code == 0
    assert len(process_runner.calls) == 3
    # Backoff doubles rather than hammering the lock.
    assert clock.slept == [0.1, 0.2]


def test_gives_up_when_the_retry_window_is_exhausted() -> None:
    """The wait is bounded: a stuck lock must not become a stuck job."""
    runner, process_runner = _runner([(128, _LOCK_STDERR)] * 50)
    clock = _Clock()

    with pytest.raises(GitCommandError):
        run_with_index_lock_retry(
            runner,
            ["add", "--", "a.txt"],
            retry_window=1.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert clock.monotonic() <= 1.0
    assert len(process_runner.calls) < 50


def test_a_rejected_hook_fails_on_the_first_attempt() -> None:
    runner, process_runner = _runner([(1, _HOOK_STDERR)] * 5)
    clock = _Clock()

    with pytest.raises(GitCommandError):
        run_with_index_lock_retry(
            runner,
            ["commit", "-q", "-m", "x"],
            retry_window=60.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert len(process_runner.calls) == 1
    assert clock.slept == []


def test_retrying_is_off_unless_a_window_is_given() -> None:
    """Default behaviour is unchanged, so no existing caller starts waiting."""
    runner, process_runner = _runner([(128, _LOCK_STDERR)] * 5)

    with pytest.raises(GitCommandError):
        run_with_index_lock_retry(runner, ["add", "--", "a.txt"], retry_window=None)

    assert len(process_runner.calls) == 1


def test_stage_paths_waits_out_a_transient_lock(tmp_path: Path) -> None:
    """The wiring is real: `git add` itself survives a collision."""
    process_runner = _ScriptedRunner([(128, _LOCK_STDERR), (0, "")])

    stage_paths(
        cwd=tmp_path,
        paths=[".opentimestamps/a.ots"],
        process_runner=process_runner,
        index_lock_retry_window=60.0,
    )

    assert len(process_runner.calls) == 2
    assert process_runner.calls[0] == ["git", "add", "--", ".opentimestamps/a.ots"]
