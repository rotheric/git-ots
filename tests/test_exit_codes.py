"""Tests that CLI failures map to the documented exit codes."""

from __future__ import annotations

import fcntl
import logging
import multiprocessing as mp
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from git_ots.cli import main
from git_ots.config import ConfigError
from git_ots.git import (
    GitCommandError,
    GitCommandTimeoutError,
    InvalidRepositoryStateError,
    RepositoryLock,
)
from git_ots.timestamp import (
    PersistenceError,
    RecoveryValidationError,
    SubmissionError,
    SubmissionTimeoutError,
    TimestampProof,
    TimestampResult,
)
from tests.test_timestamp import _make_fake_detached_proof


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


def _set_config(repo: Path, **settings: object) -> None:
    """Set `ots.*` local-scope git config keys from friendly kwargs.

    Maps `Config`-field-shaped kwargs onto their `ots.*` camelCase key
    (FS-0015 behaviour 5) and writes each with `git config --local`,
    converting Python bools to Git's own boolean spelling. Mirrors
    test_cli.py's helper of the same name.
    """
    key_map = {
        "every_commit": "ots.everyCommit",
        "max_age": "ots.maxAge",
        "fixed_time": "ots.fixedTime",
        "timezone": "ots.timezone",
        "initial_history": "ots.initialHistory",
        "source_ref": "ots.sourceRef",
        "fetch_before_run": "ots.fetchBeforeRun",
        "tag_prefix": "ots.tagPrefix",
        "require_clean_worktree": "ots.requireCleanWorktree",
        "signing": "ots.signing",
        "commit": "ots.proofCommit",
        "directory": "ots.proofDirectory",
        "command": "ots.command",
        "ots_timeout": "ots.otsTimeout",
        "git_timeout": "ots.gitTimeout",
    }
    for field, value in settings.items():
        key = key_map[field]
        text = "true" if value is True else "false" if value is False else str(value)
        _git(["config", "--local", key, text], cwd=repo)


def _due_commit_repo(tmp_path: Path) -> tuple[Path, str, datetime]:
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    now = commit_date + timedelta(hours=2)
    _set_config(repo, max_age="1h", fetch_before_run=False)
    return repo, commit_id, now


def test_unexpected_failure_names_debug_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    repo, _commit_id, now = _due_commit_repo(tmp_path)

    def explode(**_kwargs: object) -> None:
        raise RuntimeError("diagnostic marker")

    monkeypatch.setattr("git_ots.cli.run_orchestration", explode)
    assert main(argv=["run"], now=now, cwd=repo) == 1
    stderr = capsys.readouterr().err
    assert "RuntimeError" in stderr
    assert "--debug" in stderr
    assert "Traceback" not in stderr


@pytest.mark.parametrize("debug_source", ["flag", "environment"])
def test_debug_prints_unexpected_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    debug_source: str,
) -> None:
    repo, _commit_id, now = _due_commit_repo(tmp_path)

    def diagnostic_frame(**_kwargs: object) -> None:
        raise RuntimeError("diagnostic marker")

    monkeypatch.setattr("git_ots.cli.run_orchestration", diagnostic_frame)
    argv = ["--debug", "run"] if debug_source == "flag" else ["run"]
    if debug_source == "environment":
        monkeypatch.setenv("GIT_OTS_DEBUG", "1")
    assert main(argv=argv, now=now, cwd=repo) == 1
    stderr = capsys.readouterr().err
    assert "Traceback" in stderr
    assert "diagnostic_frame" in stderr
    assert "diagnostic marker" in stderr


@pytest.mark.parametrize(
    ("exception", "exit_code", "description"),
    [
        pytest.param(
            ConfigError("no policy enabled"),
            2,
            "invalid configuration",
            id="config",
        ),
        pytest.param(
            InvalidRepositoryStateError("unknown ref"),
            3,
            "invalid Git repository state",
            id="git-state",
        ),
        pytest.param(
            SubmissionError(1, b"ots failed"),
            4,
            "OpenTimestamps submission failure",
            id="submission",
        ),
        pytest.param(
            SubmissionTimeoutError(command="ots", limit=timedelta(seconds=120)),
            4,
            "OpenTimestamps submission timeout (AC-ERR-1)",
            id="submission-timeout",
        ),
        pytest.param(
            PersistenceError("cannot write proof"),
            5,
            "proof persistence failure",
            id="persistence",
        ),
        pytest.param(
            RecoveryValidationError("corrupt recovery artifacts"),
            5,
            "recovery validation failure",
            id="recovery",
        ),
        pytest.param(
            GitCommandError(argv=["git", "tag"], exit_code=1, stderr="tag failed"),
            6,
            "Git tag/commit failure",
            id="git-mutation",
        ),
        pytest.param(
            GitCommandTimeoutError(argv=["git", "fetch"], timeout=60.0),
            6,
            "Git command timeout (AC-ERR-2)",
            id="git-timeout",
        ),
    ],
)
def test_failure_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    exception: Exception,
    exit_code: int,
    description: str,
) -> None:
    """Simulated downstream failures are mapped to the spec exit codes."""
    repo, _commit_id, now = _due_commit_repo(tmp_path)
    monkeypatch.setattr(
        "git_ots.cli.run_orchestration", lambda **_kw: (_ for _ in ()).throw(exception)
    )

    returned = main(
        argv=["run"],
        now=now,
        cwd=repo,
    )

    assert returned == exit_code, f"expected exit code {exit_code} for {description}"
    captured = capsys.readouterr()
    assert captured.err


def test_upgrade_submission_timeout_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """AC-ERR-5 (exit-code half): a SubmissionTimeoutError from the upgrade path is exit 4.

    This is an adapter/exit-code-mapping check only -- it raises the exception
    straight into ``main``'s exception ladder via a monkeypatched
    ``upgrade_proofs``, following this file's established parametrized-table
    precedent. It does not assert that ``limits.ots_timeout`` is actually
    wired into the upgrade path's runner construction; that end-to-end wiring
    assertion is AC-UX-4, owned by S4.
    """
    repo, _commit_id, _now = _due_commit_repo(tmp_path)
    monkeypatch.setattr(
        "git_ots.cli.upgrade_proofs",
        lambda **_kw: (_ for _ in ()).throw(
            SubmissionTimeoutError(command="ots", limit=timedelta(seconds=120))
        ),
    )

    returned = main(argv=["upgrade"], cwd=repo)

    assert returned == 4
    assert capsys.readouterr().err


def test_invalid_configuration_real_path(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """An unknown `ots.*` key maps to exit code 2.

    `every_commit = false` alone no longer produces an invalid configuration
    under the git-config trigger ladder (FS-0015 behaviour 9: a false trigger
    enables nothing and is never mistaken for "no policy enabled" -- that
    error is unreachable and was removed with the TOML loader). An unknown
    key is the git-config-path equivalent invalid-configuration case this
    end-to-end exit-code test exercises instead.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n")
    _set_config(repo, fetch_before_run=False)
    _git(["config", "--local", "ots.bogus", "1"], cwd=repo)

    returned = main(
        argv=["run"],
        cwd=repo,
    )

    assert returned == 2
    assert "ots.bogus" in capsys.readouterr().err.lower()


def test_invalid_git_state_real_path(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """An unresolvable source ref maps to exit code 3."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n")
    _set_config(repo, max_age="1h", source_ref="does-not-exist", fetch_before_run=False)

    returned = main(
        argv=["run"],
        cwd=repo,
    )

    assert returned == 3
    assert "ref" in capsys.readouterr().err.lower()


def _hold_lock(common_git_dir: Path, started: mp.Event, release: mp.Event) -> None:
    """Acquire the repository lock and wait until ``release`` is set."""
    lock = RepositoryLock(common_git_dir=common_git_dir)
    lock.acquire()
    started.set()
    release.wait()
    lock.release()


def test_repository_locked_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """When another process holds the advisory lock, the CLI exits 7."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n")
    _set_config(repo, max_age="1h", fetch_before_run=False)

    started = mp.Event()
    release = mp.Event()
    holder = mp.Process(
        target=_hold_lock,
        args=(repo / ".git", started, release),
    )
    holder.start()
    try:
        assert started.wait(timeout=5.0), "lock holder did not start in time"
        returned = main(
            argv=["run"],
            cwd=repo,
        )
    finally:
        release.set()
        holder.join(timeout=5.0)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5.0)

    assert returned == 7
    assert "lock" in capsys.readouterr().err.lower()


def test_no_work_exits_zero(tmp_path: Path) -> None:
    """When no timestamp is required, the CLI exits 0."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    now = commit_date + timedelta(minutes=30)
    _set_config(repo, max_age="1h", fetch_before_run=False)

    returned = main(
        argv=["run"],
        now=now,
        cwd=repo,
    )

    assert returned == 0


def test_dry_run_work_exits_zero(tmp_path: Path) -> None:
    """A dry run that would timestamp something exits 0."""
    repo, _commit_id, now = _due_commit_repo(tmp_path)

    returned = main(
        argv=["run", "--dry-run"],
        now=now,
        cwd=repo,
    )

    assert returned == 0


# --- S4: end-to-end wiring and interruption ---------------------------------


@pytest.mark.parametrize("subcommand", ["commit", "add"])
def test_git_command_timeout_error_names_index_lock_hazard_for_commit_and_add(
    subcommand: str,
) -> None:
    """AC-ERR-6: a timed-out `git commit`/`git add` must diagnose the hazard.

    Both subcommands take the index lock before running hooks and only
    release it once the whole command finishes; SIGKILL gives git no chance
    to do that, so a stale `.git/index.lock` can remain and block every later
    `git add`/`git commit` in the repository. An operator reading this
    message (e.g. in cron mail) must be able to act on it without first
    diagnosing the hazard themselves -- the remedy (removing the file) must
    be named directly.
    """
    exc = GitCommandTimeoutError(argv=["git", subcommand, "--", "x"], timeout=1.0)
    message = str(exc)
    assert "index.lock" in message
    assert "remove" in message.lower()


def test_git_command_timeout_error_omits_index_lock_hazard_for_non_index_commands() -> (
    None
):
    """AC-ERR-6: a timed-out read-only command must not mention index.lock.

    `git rev-parse` (and similarly `tag`, `cat-file`, etc.) never takes the
    index lock, so a diagnostic naming that hazard would be actively
    misleading for these commands.
    """
    exc = GitCommandTimeoutError(argv=["git", "rev-parse", "HEAD"], timeout=1.0)
    assert "index.lock" not in str(exc)


@pytest.mark.parametrize("command", ["run", "status", "validate", "verify", "upgrade"])
def test_keyboard_interrupt_returns_nonzero_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    command: str,
) -> None:
    """AC-UX-2: a KeyboardInterrupt during any subcommand is a clean non-zero exit.

    ``assemble_config`` is a step every subcommand's try block reaches, so
    patching it to raise ``KeyboardInterrupt`` exercises each of the five
    per-subcommand handlers added in ``main`` uniformly -- what is under test
    here is that the exception never propagates past ``main`` (no traceback
    on stderr) and is converted to a non-zero return, not the interrupt
    cleanup itself (that in-flight behaviour is AC-PROC-3, driven through a
    real forking script in tests/test_integration.py).
    """
    repo, _commit_id, now = _due_commit_repo(tmp_path)

    def _raise(*_args: object, **_kwargs: object):
        raise KeyboardInterrupt

    monkeypatch.setattr("git_ots.cli.assemble_config", _raise)

    argv = [command]

    returned = main(argv=argv, now=now, cwd=repo)

    assert returned != 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "KeyboardInterrupt" not in captured.err


def test_timed_out_run_leaves_no_proof_manifest_or_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-STATE-1: after a run that fails per AC-ERR-1, nothing new is left behind.

    Not merely "no exception-specific artifact": the proof directory's whole
    file listing and the repository's whole tag list are snapshotted before
    the timed-out run and compared unchanged afterwards.
    """
    repo, _commit_id, now = _due_commit_repo(tmp_path)
    proof_dir = repo / ".opentimestamps"
    before_proof_files = (
        sorted(p.name for p in proof_dir.iterdir()) if proof_dir.is_dir() else []
    )
    before_tags = _git(["tag", "-l"], cwd=repo).strip()

    monkeypatch.setattr(
        "git_ots.cli.run_orchestration",
        lambda **_kw: (_ for _ in ()).throw(
            SubmissionTimeoutError(command="ots", limit=timedelta(seconds=1))
        ),
    )

    returned = main(argv=["run"], now=now, cwd=repo)

    assert returned == 4
    after_proof_files = (
        sorted(p.name for p in proof_dir.iterdir()) if proof_dir.is_dir() else []
    )
    assert after_proof_files == before_proof_files
    assert _git(["tag", "-l"], cwd=repo).strip() == before_tags


def _probe_lock_from_separate_process(lock_path: str, result: mp.Queue) -> None:
    """Attempt a genuinely separate-process ``flock`` and report the outcome.

    Deliberately does not construct a ``RepositoryLock`` -- that class's
    ``acquire()`` short-circuits on the in-process ``_LOCK_STATE`` refcount
    (git.py:250-256) before ever calling ``fcntl.flock``, which would report
    success vacuously even if the lock file descriptor were never released at
    the OS level. This function is spawned in its own process specifically so
    that refcount does not exist to short-circuit against.
    """
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        result.put(False)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        result.put(True)
    finally:
        os.close(fd)


def test_timed_out_run_releases_repository_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-STATE-2: a separate process can flock the lock file after a timed-out run.

    The timeout unwinds both nested ``with RepositoryLock(...)`` blocks
    (cli.py's outer acquisition and orchestration.py's inner one); no special
    release handling exists in production code because the reentrant refcount
    does this naturally (architecture.md Implementation constraint 3).
    """
    repo, _commit_id, now = _due_commit_repo(tmp_path)
    monkeypatch.setattr(
        "git_ots.cli.run_orchestration",
        lambda **_kw: (_ for _ in ()).throw(
            SubmissionTimeoutError(command="ots", limit=timedelta(seconds=1))
        ),
    )

    returned = main(argv=["run"], now=now, cwd=repo)
    assert returned == 4

    lock_path = str(repo / ".git" / "git-ots.lock")
    result_queue: mp.Queue = mp.Queue()
    prober = mp.Process(
        target=_probe_lock_from_separate_process, args=(lock_path, result_queue)
    )
    prober.start()
    prober.join(timeout=5.0)
    assert not prober.is_alive(), "separate-process lock probe did not finish in time"
    assert result_queue.get(timeout=1.0) is True, (
        "a separate process could not acquire the lock -- it was not released"
    )


def test_timed_out_run_then_successful_run_leaves_exactly_one_proof_and_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-STATE-3: a timed-out run followed by a successful one is idempotent.

    Not "at least one" and not a count taken only after the second run: the
    proof directory and tag list are inspected after both runs to confirm
    nothing leaked from the first, failed attempt.
    """
    repo, commit_id, now = _due_commit_repo(tmp_path)

    state = {"fail": True}

    class _ScriptedOpenTimestampsCli:
        def __init__(self, command: str, *, runner=None, limit=None) -> None:
            del command, runner, limit

        def submit(self, request):
            if state["fail"]:
                raise SubmissionTimeoutError(command="ots", limit=timedelta(seconds=1))
            proof_bytes = _make_fake_detached_proof(payload=request.payload)
            return TimestampResult(proof=TimestampProof(data=proof_bytes))

    monkeypatch.setattr(
        "git_ots.orchestration.OpenTimestampsCli", _ScriptedOpenTimestampsCli
    )

    failed = main(argv=["run"], now=now, cwd=repo)
    assert failed == 4

    state["fail"] = False
    succeeded = main(argv=["run"], now=now, cwd=repo)
    assert succeeded == 0

    proof_dir = repo / ".opentimestamps"
    ots_files = sorted(p for p in proof_dir.iterdir() if p.suffix == ".ots")
    assert [p.stem for p in ots_files] == [commit_id]

    tags = [
        line for line in _git(["tag", "-l", "ots/*"], cwd=repo).splitlines() if line
    ]
    assert len(tags) == 1


def test_timed_out_run_leaves_status_pending_classification_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """AC-STATE-4: a timeout leaves nothing for crash recovery to act on.

    ``status`` reports the subject commit's pending classification
    identically before and after the timed-out run -- not merely that
    ``status`` exits 0.
    """
    repo, _commit_id, now = _due_commit_repo(tmp_path)

    before = main(argv=["status"], now=now, cwd=repo)
    assert before == 0
    before_output = capsys.readouterr().out

    monkeypatch.setattr(
        "git_ots.cli.run_orchestration",
        lambda **_kw: (_ for _ in ()).throw(
            SubmissionTimeoutError(command="ots", limit=timedelta(seconds=1))
        ),
    )
    failed = main(argv=["run"], now=now, cwd=repo)
    assert failed == 4
    capsys.readouterr()

    after = main(argv=["status"], now=now, cwd=repo)
    assert after == 0
    after_output = capsys.readouterr().out

    assert after_output == before_output


# ---------------------------------------------------------------------------
# CLI surface contract
#
# A mutation run over cli.py showed the argument parser and the two
# argparse-owned exit paths to be almost entirely unasserted: the `-v` short
# form could be renamed, and both `main([])` and the interrupt path could
# return a different number, with the suite still green. The tests below pin
# the parts of that surface an operator or a scheduler actually depends on.
# (The original finding also named the now-removed `--config` flag; see
# test_cli.py's test_config_flag_is_not_accepted_by_any_subcommand for its
# FS-0015 S5 replacement.)
#
# Note that neither 130 nor the "no subcommand" 2 appears in the documented
# exit-code table (spec.md 29 / README); they are undocumented runtime
# behaviour. Filed as spec-gap findings.
# ---------------------------------------------------------------------------

_SUBCOMMANDS = ["run", "status", "verify", "upgrade"]


def test_no_subcommand_prints_usage_and_exits_two(
    capsys: pytest.CaptureFixture,
) -> None:
    """Invoked with no subcommand at all, git-ots must show the operator how
    to invoke it and exit non-zero -- a scheduler that mis-spells the command
    must not see a success code."""
    returned = main(argv=[])
    assert returned == 2
    captured = capsys.readouterr()
    assert "usage: git-ots" in captured.out


@pytest.mark.parametrize("command", _SUBCOMMANDS)
def test_keyboard_interrupt_uses_the_shell_interrupt_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    command: str,
) -> None:
    """Ctrl-C must exit 130 -- the shell convention for "killed by SIGINT" --
    and say so, for every subcommand alike. A scheduler distinguishing an
    operator abort from a real failure has only the exit code to go on, so
    "non-zero" (which the sibling AC-UX-2 test asserts) is not enough.

    130 is not in the documented exit-code table; see the spec-gap finding.
    """
    repo, _commit_id, now = _due_commit_repo(tmp_path)

    def _raise(*_args: object, **_kwargs: object):
        raise KeyboardInterrupt

    monkeypatch.setattr("git_ots.cli.assemble_config", _raise)

    argv = [command]

    returned = main(argv=argv, now=now, cwd=repo)

    assert returned == 130
    assert "Interrupted" in capsys.readouterr().err


@pytest.mark.parametrize("command", _SUBCOMMANDS)
@pytest.mark.parametrize("flag", ["-v", "--verbose"])
def test_verbose_is_reachable_by_both_spellings_and_raises_the_log_level(
    tmp_path: Path, command: str, flag: str
) -> None:
    """`-v` is the spelling documented in the help output and the one an
    operator types; it must stay a single letter, must stay a switch (no
    value), and must actually turn diagnostics on. Without the flag the same
    invocation must stay quiet -- a verbosity that is always on is the same
    bug as one that never turns on."""
    repo, _commit_id, now = _due_commit_repo(tmp_path)
    logger = logging.getLogger("git_ots")

    main(argv=[command], now=now, cwd=repo)
    assert logger.level == logging.WARNING

    main(argv=[command, flag], now=now, cwd=repo)
    assert logger.level == logging.INFO


# test_explicit_config_path_wins_over_the_ambient_file and
# test_a_config_flag_naming_a_missing_file_exits_two were removed here
# (FS-0015 S5): both were exercises of the `--config` flag itself -- an
# explicit path overriding an ambient file's discovery, and a mis-typed path
# being an error rather than a silent fallback -- neither of which has a
# meaning once the flag is gone (AC-CLIFLAG-1). test_cli.py's
# test_config_flag_is_not_accepted_by_any_subcommand covers the flag's
# removal; test_cli.py's test_orphan_toml_with_real_ots_keys_set_never_surfaces_in_output
# covers the "ambient file is inert" half of the first test's intent.
