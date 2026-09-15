"""Tests for the CLI commands."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from git_ots.cli import main
from git_ots.gitconfig import MAPPING


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

    Maps `Config`-field-shaped kwargs (`max_age="1h"`, `fetch_before_run=False`,
    ...) onto their `ots.*` camelCase key (FS-0015 behaviour 5) and writes each
    with `git config --local`, converting Python bools to Git's own boolean
    spelling. This is the git-config replacement for the TOML fixtures this
    file used to write directly to a `git-ots.toml`.
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


def test_run_dry_run_prints_selected_commits_and_triggers(
    capsys, tmp_path: Path
) -> None:
    """Dry-run resolves repository state, prints selected commits/triggers, and makes no mutations."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    now = commit_date + timedelta(hours=2)

    _set_config(repo, max_age="1h", fetch_before_run=False)

    before_head = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    before_tags = _git(["tag", "-l"], cwd=repo).strip()

    exit_code = main(
        argv=["run", "--dry-run"],
        now=now,
        cwd=repo,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert commit_id in captured.out
    assert commit_id[:12] in captured.out
    assert "max_age" in captured.out

    # No side effects occurred: no proof directory, no tags, no new commits.
    assert not (repo / ".opentimestamps").exists()
    assert _git(["tag", "-l"], cwd=repo).strip() == before_tags
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == before_head


def test_status_command_reports_repository_state_read_only(
    capsys, tmp_path: Path
) -> None:
    """The status command prints source, baseline, pending, and trigger state without mutating the repository."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    now = commit_date + timedelta(hours=2)

    _set_config(
        repo,
        max_age="1h",
        fixed_time="00:00",
        timezone="UTC",
        fetch_before_run=False,
    )

    before_head = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    before_tags = _git(["tag", "-l"], cwd=repo).strip()

    exit_code = main(
        argv=["status"],
        now=now,
        cwd=repo,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "source ref:" in captured.out
    assert "source commit:" in captured.out
    assert commit_id[:12] in captured.out
    assert "last timestamped source:" in captured.out
    assert "pending meaningful: 1" in captured.out
    assert "oldest pending age:" in captured.out
    assert "max-age trigger: due" in captured.out
    assert "fixed-time trigger: due" in captured.out

    # Status is read-only: no commits, tags, or proof files were created.
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == before_head
    assert _git(["tag", "-l"], cwd=repo).strip() == before_tags
    assert not (repo / ".opentimestamps").exists()


def _set_base_config(repo: Path) -> None:
    _set_config(repo, max_age="1h", fetch_before_run=False, commit=False)


def test_run_default_output_is_concise(capsys, tmp_path: Path) -> None:
    """Without -v/--verbose, run output stays concise and no diagnostics appear."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    now = commit_date + timedelta(hours=2)

    _set_base_config(repo)

    exit_code = main(
        argv=["run", "--dry-run"],
        now=now,
        cwd=repo,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0].startswith("Selected:")
    assert lines[1].startswith("Triggers:")


@pytest.mark.parametrize("verbose_flag", ["-v", "--verbose"])
def test_run_verbose_adds_diagnostics(
    verbose_flag: str, capsys, tmp_path: Path
) -> None:
    """-v/--verbose emits inspection and operation diagnostics to stderr."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    now = commit_date + timedelta(hours=2)

    _set_base_config(repo)

    exit_code = main(
        argv=["run", "--dry-run", verbose_flag],
        now=now,
        cwd=repo,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Inspecting repository state" in captured.err
    assert "Policy decision" in captured.err
    assert "max_age" in captured.err
    # stdout remains concise.
    assert captured.out.startswith("Selected:")


def test_status_verbose_adds_inspection_diagnostics(capsys, tmp_path: Path) -> None:
    """-v/--verbose with status emits inspection diagnostics without mutating state."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    now = commit_date + timedelta(hours=2)

    _set_base_config(repo)

    before_head = _git(["rev-parse", "HEAD"], cwd=repo).strip()

    exit_code = main(
        argv=["status", "-v"],
        now=now,
        cwd=repo,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Inspecting repository state" in captured.err
    assert "source ref:" in captured.out
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == before_head


def test_verbose_does_not_write_log_files(tmp_path: Path) -> None:
    """Verbose diagnostics are emitted to stderr; no persistent log files are created."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    now = commit_date + timedelta(hours=2)

    _set_base_config(repo)

    log_files_before = set(tmp_path.rglob("*.log"))
    main(
        argv=["run", "--dry-run", "-v"],
        now=now,
        cwd=repo,
    )
    log_files_after = set(tmp_path.rglob("*.log"))
    assert log_files_after == log_files_before


def test_local_scope_config_is_effective_from_a_nested_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """`ots.*` set in local scope (`.git/config`) is effective for a subcommand
    invoked from a nested working directory, not only from the repository
    root -- `git config` walks up to find `.git` on its own, replacing the
    old TOML two-step (invocation directory, then repository root) discovery
    this test used to exercise."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    now = commit_date + timedelta(hours=2)

    _set_config(repo, max_age="1h", fetch_before_run=False, commit=False)
    nested = repo / "subdir"
    nested.mkdir()

    before_head = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    before_tags = _git(["tag", "-l"], cwd=repo).strip()

    from_root = main(argv=["status"], now=now, cwd=repo)
    root_output = capsys.readouterr()
    assert from_root == 0
    assert f"source commit: {commit_id[:12]}" in root_output.out
    assert "pending meaningful: 1" in root_output.out

    from_nested = main(argv=["status"], now=now, cwd=nested)
    nested_output = capsys.readouterr()
    assert from_nested == 0
    assert f"source commit: {commit_id[:12]}" in nested_output.out
    assert "pending meaningful: 1" in nested_output.out
    assert "source ref:" in nested_output.out

    # status must remain read-only: no lock, no tags, no commits.
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == before_head
    assert _git(["tag", "-l"], cwd=repo).strip() == before_tags
    assert not (repo / ".opentimestamps").exists()

    # run --dry-run from the nested directory sees the same source and triggers.
    dry_run_exit = main(argv=["run", "--dry-run"], now=now, cwd=nested)
    dry_run_output = capsys.readouterr()
    assert dry_run_exit == 0
    assert commit_id in dry_run_output.out
    assert "max_age" in dry_run_output.out


# test_explicit_relative_config_resolved_against_invocation_directory and
# test_invocation_directory_config_takes_precedence_over_root were removed
# here (FS-0015 S5): both exercised `--config` path-resolution and
# invocation-directory-vs-repository-root TOML precedence, neither of which
# exists any more -- there is no `--config` flag (AC-CLIFLAG-1) and no
# per-directory file to prioritise. The general concept each was a facet of
# -- narrower scope beating wider scope -- is Git's own and is covered by
# test_gitconfig.py's AC-MERGE-1/AC-SCOPE-1 (`--worktree` overriding
# `--global`) and AC-TRIG-2 worked-table tests; the "explicit override wins"
# facet specifically is AC-OVERRIDE-1's `git -c` test below.


def test_default_source_ref_head_works_without_upstream(
    capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """With the default HEAD source_ref, status and run --dry-run succeed without an upstream."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    now = commit_date + timedelta(hours=2)

    _set_config(repo, max_age="1h", fetch_before_run=False)

    before_head = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    before_tags = _git(["tag", "-l"], cwd=repo).strip()

    exit_code = main(argv=["status"], now=now, cwd=repo)
    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"source commit: {commit_id[:12]}" in captured.out
    assert "pending meaningful: 1" in captured.out

    exit_code = main(argv=["run", "--dry-run"], now=now, cwd=repo)
    captured = capsys.readouterr()
    assert exit_code == 0
    assert commit_id in captured.out
    assert "max_age" in captured.out

    # No mutations occurred.
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == before_head
    assert _git(["tag", "-l"], cwd=repo).strip() == before_tags
    assert not (repo / ".opentimestamps").exists()


def test_outside_git_repository_exits_3(
    capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """Invoking the tool outside any Git repository reports a repository-state error."""
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    exit_code = main(argv=["status"], cwd=not_a_repo)
    captured = capsys.readouterr()
    assert exit_code == 3
    assert "Repository state error" in captured.err
    assert "Unexpected error" not in captured.err


def test_run_dry_run_fetches_before_run_when_configured(capsys, tmp_path: Path) -> None:
    """With fetch_before_run=true, run fetches the owning remote before resolving source.

    A stale clone with source_ref=@{upstream} sees a newly pushed remote commit
    after fetching, while local HEAD, index, and worktree remain untouched.
    """
    remote = tmp_path / "remote.git"
    remote.mkdir(parents=True)
    _git(["init", "-q", "--bare", "-b", "main", "."], cwd=remote)

    seed = tmp_path / "seed"
    _init_repo(seed)
    a_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _commit(seed, paths=["README"], message="seed", date=a_date)
    _git(["remote", "add", "origin", str(remote)], cwd=seed)
    _git(["push", "-q", "-u", "origin", "main"], cwd=seed)

    clone = tmp_path / "clone"
    _git(["clone", "-q", str(remote), str(clone)], cwd=tmp_path)
    _git(["config", "user.email", "test@example.com"], cwd=clone)
    _git(["config", "user.name", "Test"], cwd=clone)

    # Push a second commit from the seed repository without updating the clone.
    b_date = a_date + timedelta(hours=1)
    _commit(seed, paths=["NEW"], message="second", date=b_date)
    _git(["push", "-q", "origin", "main"], cwd=seed)
    b_id = _git(["rev-parse", "HEAD"], cwd=seed).strip()

    _set_config(
        clone,
        max_age="1h",
        source_ref="@{upstream}",
        fetch_before_run=True,
        commit=False,
    )

    before_head = _git(["rev-parse", "HEAD"], cwd=clone).strip()
    before_tracking = _git(["rev-parse", "refs/remotes/origin/main"], cwd=clone).strip()
    before_status = _git(["status", "--porcelain"], cwd=clone)
    before_index = _git(["ls-files", "--stage"], cwd=clone)

    exit_code = main(
        argv=["run", "--dry-run"],
        now=b_date + timedelta(hours=2),
        cwd=clone,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert b_id[:12] in captured.out
    assert "max_age" in captured.out

    # Local HEAD and worktree state did not change; the remote-tracking ref advanced.
    assert _git(["rev-parse", "HEAD"], cwd=clone).strip() == before_head
    assert _git(["status", "--porcelain"], cwd=clone) == before_status
    assert _git(["ls-files", "--stage"], cwd=clone) == before_index
    assert _git(["rev-parse", "refs/remotes/origin/main"], cwd=clone).strip() == b_id
    assert before_tracking != b_id
    assert not (clone / ".opentimestamps").exists()


def test_no_config_anywhere_falls_back_to_the_defaults(
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """An empty `ots.*` namespace (no key set in any scope) is not an error.

    Invoked from a subdirectory, so `git config`'s own upward discovery is
    exercised, not merely the repository root. Configuration is optional;
    the built-in defaults apply and the command proceeds.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A")
    nested = repo / "nested"
    nested.mkdir()

    for command in (["status"], ["run", "--dry-run"]):
        exit_code = main(argv=command, cwd=nested)
        captured = capsys.readouterr()
        assert exit_code == 0, command
        assert "Configuration error:" not in captured.err, command
        assert "Unexpected error" not in captured.err, command


@pytest.mark.parametrize(
    ("bad_config_path", "create"),
    [
        ("git-ots.toml", lambda p: p.mkdir()),
        ("git-ots.toml", lambda p: p.write_text("")),
        ("git-ots.toml", lambda p: p.write_text("not valid toml =")),
    ],
    ids=["directory", "empty-file", "malformed"],
)
def test_orphan_config_file_is_never_opened_regardless_of_shape(
    bad_config_path: str,
    create: callable,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """AC-ORPHAN-1: a `git-ots.toml` at the worktree root is not read at all,
    for any shape that would have been a loader error before S5 -- a
    directory, or a file whose *contents* are malformed TOML. Since the file
    is never opened, `status`/`run --dry-run` succeed exactly as they would
    with no file present, and nothing about the file appears on stdout or
    stderr; this supersedes the pre-S5 `test_invalid_config_file_exits_2`,
    which asserted the opposite (a loader-shaped exit 2) under the TOML path
    this story removed.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A")
    target = repo / bad_config_path
    create(target)

    for command in (["status"], ["run", "--dry-run"]):
        exit_code = main(argv=command, cwd=repo)
        captured = capsys.readouterr()
        assert exit_code == 0, command
        assert "not valid TOML" not in captured.out + captured.err, command
        assert "Configuration error" not in captured.err, command


def _set_subhour_config(repo: Path) -> None:
    _set_config(repo, max_age="30m", fetch_before_run=False)


def test_status_warns_on_sub_hour_max_age(
    capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """A sub-hour max_age emits a configuration warning during status without changing the exit code."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n")
    _set_subhour_config(repo)

    with pytest.warns(UserWarning, match="below one hour"):
        exit_code = main(argv=["status"], cwd=repo)

    assert exit_code == 0


def test_run_warns_on_sub_hour_max_age(
    capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """A sub-hour max_age emits a configuration warning during run without changing the exit code."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    now = commit_date + timedelta(hours=2)

    _set_subhour_config(repo)

    with pytest.warns(UserWarning, match="below one hour"):
        exit_code = main(
            argv=["run", "--dry-run"],
            now=now,
            cwd=repo,
        )

    assert exit_code == 0


def test_run_reports_blocking_repository_state_before_announcing_work(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A blocked run must not first announce a timestamp it cannot create.

    The committable-state precondition lives at the orchestration mutation
    boundary, so a run during a rebase printed "Selected: ..." and "Triggers:
    ..." and only then refused. The refusal now comes first; the mutation
    boundary keeps its own check for callers that bypass the CLI.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(
        repo,
        paths=["a.txt"],
        message="A\n",
        date=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _set_config(repo, max_age="1m", fetch_before_run=False)

    # Simulate an interrupted rebase.
    common = subprocess.run(
        ["git", "rev-parse", "--git-path", "rebase-merge/"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (repo / common).mkdir(parents=True)

    exit_code = main(argv=["run"], cwd=repo)

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "rebase-merge/" in captured.err
    assert "Selected:" not in captured.out, (
        "the run announced work it then refused to do"
    )
    assert "Triggers:" not in captured.out


def test_run_reports_a_missing_opentimestamps_client_as_exit_four(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A missing client is a submission failure, not an unexpected error."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(
        repo,
        paths=["a.txt"],
        message="A\n",
        date=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _set_config(
        repo,
        max_age="1h",
        fetch_before_run=False,
        command="git-ots-no-such-client",
    )

    exit_code = main(argv=["run"], cwd=repo, now=datetime(2026, 1, 2, tzinfo=UTC))

    captured = capsys.readouterr()
    # Exit 4 is the documented "OpenTimestamps submission failure" code. It used
    # to escape as `Unexpected error: [Errno 2] ...` and exit 1.
    assert exit_code == 4
    assert "git-ots-no-such-client" in captured.err
    assert "opentimestamps-client" in captured.err, "must name the remedy"
    assert "Unexpected error" not in captured.err
    # A dry run needs no client and must still report.
    assert (
        main(argv=["run", "--dry-run"], cwd=repo, now=datetime(2026, 1, 2, tzinfo=UTC))
        == 0
    )
    assert "Selected:" in capsys.readouterr().out


# --- S4: end-to-end wiring (AC-UX-3) -----------------------------------------


def _spy_on_configured_git_runner(monkeypatch: pytest.MonkeyPatch):
    """Patch ``git_ots.cli.make_process_runner`` to record its ``timeout=``
    and wrap the real runner it delegates to, so both the exact value cli.py
    built the runner with and whether that runner was ever actually used can
    be asserted.

    Only the text-mode factory is patched; ``make_process_runner_bytes`` is
    left real (unused by build_snapshot/validate_proofs/upgrade_proofs's git
    calls). ``_default_process_runner`` -- the built-in-default singleton the
    pre-config ``_locate_repository`` calls use -- was built at git.py import
    time through the *unpatched* factory, long before this fixture runs, so
    it cannot leak into ``captured_timeouts``.
    """
    from git_ots.git import make_process_runner as real_make_process_runner

    captured_timeouts: list[float | None] = []
    call_count = {"n": 0}

    def _spy(*, timeout):
        captured_timeouts.append(timeout)
        inner = real_make_process_runner(timeout=timeout)

        def _wrapped(argv, *, cwd, stdin=None):
            call_count["n"] += 1
            return inner(argv, cwd=cwd, stdin=stdin)

        return _wrapped

    monkeypatch.setattr("git_ots.cli.make_process_runner", _spy)
    return captured_timeouts, call_count


def test_run_supplies_configured_git_timeout_to_build_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-UX-3: `run` builds a git_timeout-derived runner and passes it to
    build_snapshot -- not the built-in default (60s).

    An implementation that hardcodes the built-in default and ignores
    ``limits.git_timeout`` would capture ``60.0`` here instead of ``5.0``.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n", date=datetime(2026, 1, 1, tzinfo=UTC))
    _set_config(repo, max_age="1h", fetch_before_run=False, git_timeout="5s")

    captured_timeouts, call_count = _spy_on_configured_git_runner(monkeypatch)

    exit_code = main(
        argv=["run", "--dry-run"], cwd=repo, now=datetime(2026, 1, 2, tzinfo=UTC)
    )

    assert exit_code == 0
    assert captured_timeouts == [5.0]
    assert call_count["n"] > 0, "the configured runner was built but never used"


def test_validate_supplies_configured_git_timeout_to_validate_proofs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-UX-3: `validate` passes its configured runner end to end."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n", date=datetime(2026, 1, 1, tzinfo=UTC))
    _set_config(repo, max_age="1h", fetch_before_run=False, git_timeout="7s")

    captured_timeouts, call_count = _spy_on_configured_git_runner(monkeypatch)

    exit_code = main(argv=["validate"], cwd=repo, now=datetime(2026, 1, 2, tzinfo=UTC))

    assert exit_code == 0
    assert captured_timeouts == [7.0]
    assert call_count["n"] > 0, "the configured runner was built but never used"


def test_upgrade_supplies_configured_git_timeout_to_upgrade_proofs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-UX-3: `upgrade` builds a git_timeout-derived runner and passes it to
    upgrade_proofs -- not the built-in default (60s)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n", date=datetime(2026, 1, 1, tzinfo=UTC))
    _set_config(repo, max_age="1h", fetch_before_run=False, git_timeout="9s")

    captured_timeouts, call_count = _spy_on_configured_git_runner(monkeypatch)

    exit_code = main(
        argv=["upgrade", "--dry-run"], cwd=repo, now=datetime(2026, 1, 2, tzinfo=UTC)
    )

    assert exit_code == 0
    assert captured_timeouts == [9.0]
    assert call_count["n"] > 0, "the configured runner was built but never used"


def test_layer_read_is_bounded_by_the_built_in_default_not_the_value_it_discovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pre-config/post-config timeout split (cli.py `_configured_process_runners`)
    must still hold once the config layer read itself is a `git config`
    subprocess call: the read that discovers `ots.gitTimeout` cannot be bounded
    by the value it is in the middle of discovering (FS-0015 behaviour 4).

    `ots.gitTimeout` is set to the smallest expressible duration (1s), far
    below the 60s built-in default. If the layer read were self-referentially
    bounded by its own not-yet-discovered ceiling, either the read itself
    would be built with `timeout=1.0` (visible as an extra entry in
    `captured_timeouts`, distinct from the 60s built-in) or it would
    spuriously time out reading a namespace of five cheap subprocess calls.
    Instead only one runner is ever built through
    `git_ots.cli.make_process_runner` -- the post-config one, at the real 1s
    value -- and the read that discovered it completes successfully
    beforehand, under the built-in default.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n", date=datetime(2026, 1, 1, tzinfo=UTC))
    _set_config(repo, max_age="1h", fetch_before_run=False, git_timeout="1s")

    captured_timeouts, _call_count = _spy_on_configured_git_runner(monkeypatch)

    exit_code = main(argv=["status"], cwd=repo, now=datetime(2026, 1, 2, tzinfo=UTC))

    assert exit_code == 0
    # Exactly one runner built through the patched factory: the post-config
    # one. The layer read itself never goes through `make_process_runner` at
    # all -- it falls through to git.py's built-in-default singleton, exactly
    # as `_locate_repository`'s two pre-config `rev-parse` calls do.
    assert captured_timeouts == [1.0]


# ---------------------------------------------------------------------------
# S5: CLI wiring and --config removal (AC-OVERRIDE-1, AC-CLEAN-1, AC-BARE-1,
# AC-ORPHAN-1, AC-CLIFLAG-1).
# ---------------------------------------------------------------------------


def test_git_c_override_flows_through_gits_own_machinery(tmp_path: Path) -> None:
    """AC-OVERRIDE-1: `git -c ots.maxAge=1h ots status` reflects the override.

    A real subprocess through Git's own subcommand dispatch (`git ots ...`
    resolves to the installed `git-ots` console script on PATH), not a
    monkeypatch of the reader simulating what `-c` would produce -- `-c`'s
    env-var machinery (`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/
    `GIT_CONFIG_VALUE_0`) is exercised for real.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    _set_config(repo, fetch_before_run=False)

    venv_bin = str(Path(sys.executable).parent)
    env = {**os.environ, "PATH": venv_bin + os.pathsep + os.environ.get("PATH", "")}

    completed = subprocess.run(
        ["git", "-c", "ots.maxAge=1h", "ots", "status"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "max-age trigger: due" in completed.stdout


def test_effective_configuration_with_ots_keys_set_leaves_the_worktree_clean(
    tmp_path: Path,
) -> None:
    """AC-CLEAN-1: the FS-0014 regression guard, extended to a repository with
    `ots.*` keys actually set (any scope). `git status --porcelain
    --untracked-files=all` is byte-identical before and after every
    subcommand runs -- not merely for an unconfigured repository, which is
    the case FS-0014's own guard already covers."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    now = commit_date + timedelta(hours=2)

    _set_config(repo, max_age="1h", fetch_before_run=False, tag_prefix="clean/")

    before = _git(["status", "--porcelain", "--untracked-files=all"], cwd=repo)
    assert before == ""

    for command in (
        ["status"],
        ["run", "--dry-run"],
        ["validate"],
        ["verify"],
        ["upgrade", "--dry-run"],
    ):
        exit_code = main(argv=command, cwd=repo, now=now)
        assert exit_code == 0, command

    after = _git(["status", "--porcelain", "--untracked-files=all"], cwd=repo)
    assert after == before


def test_validate_succeeds_against_a_bare_clone_reading_global_scope(
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """AC-BARE-1: `git-ots validate` against a bare clone, with an `ots.*` key
    set in global scope, succeeds -- the layer read (and repository location
    ahead of it) does not require a worktree. The only layering behaviour
    (FS-0015 behaviour 4) with no criterion coverage before this story.

    A bare repository has no filesystem worktree at all, so there is no
    directory `validate_proofs` could scan for a `.opentimestamps` proof
    directory even if it wanted to -- asserting the "No stored proofs
    found." branch (rather than only the exit code) confirms `validate` ran
    all the way through its normal read path to completion, not that it
    silently short-circuited somewhere before reaching it.
    """
    seed = tmp_path / "seed"
    _init_repo(seed)
    _commit(seed, paths=["a.txt"], message="A\n")

    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(["init", "-q", "--bare", "-b", "main", "."], cwd=bare)
    _git(["remote", "add", "origin", str(bare)], cwd=seed)
    _git(["push", "-q", "origin", "main"], cwd=seed)

    _git(["config", "--global", "ots.maxAge", "1h"], cwd=bare)

    exit_code = main(argv=["validate"], cwd=bare)

    assert exit_code == 0
    assert "No stored proofs found; nothing was validated." in capsys.readouterr().out


def test_run_against_a_bare_clone_fails_cleanly_and_creates_nothing(
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """Regression guard for the write-path side of AC-BARE-1's fallback.

    `locate_repository` making `worktree_root` fall back to the Git
    directory for a bare repository (so `validate` keeps working, see
    `test_validate_succeeds_against_a_bare_clone_reading_global_scope` above)
    must not also let write commands treat that Git directory as a place to
    write. Before the `assert_worktree_present` guard, `git-ots run` against
    a bare clone with `ots.everyCommit=true` created `.opentimestamps/` and
    `git-ots.lock` directly inside the bare repository's Git directory.

    The exit-code assertion alone is not load-bearing: `run` also fails
    without the guard, just later and for an unrelated reason (no `ots`
    client / no commit to select), so a passing exit code here would not by
    itself prove the regression is fixed. The directory-untouched assertion
    is what actually pins the fix.
    """
    seed = tmp_path / "seed"
    _init_repo(seed)
    _commit(seed, paths=["a.txt"], message="A\n")

    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(["init", "-q", "--bare", "-b", "main", "."], cwd=bare)
    _git(["remote", "add", "origin", str(bare)], cwd=seed)
    _git(["push", "-q", "origin", "main"], cwd=seed)

    _git(["config", "ots.everyCommit", "true"], cwd=bare)

    before = sorted(p.relative_to(bare) for p in bare.rglob("*"))

    exit_code = main(argv=["run"], cwd=bare)

    assert exit_code == 3
    assert "working tree" in capsys.readouterr().err

    after = sorted(p.relative_to(bare) for p in bare.rglob("*"))
    assert after == before
    assert not (bare / "git-ots.lock").exists()
    assert not (bare / ".opentimestamps").exists()


def test_upgrade_against_a_bare_clone_fails_cleanly_and_creates_nothing(
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """`upgrade` is the other write path that shares `run`'s exposure: it
    also acquires the repository lock unconditionally (even under
    `--dry-run`), so it must be refused the same way."""
    seed = tmp_path / "seed"
    _init_repo(seed)
    _commit(seed, paths=["a.txt"], message="A\n")

    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(["init", "-q", "--bare", "-b", "main", "."], cwd=bare)
    _git(["remote", "add", "origin", str(bare)], cwd=seed)
    _git(["push", "-q", "origin", "main"], cwd=seed)

    before = sorted(p.relative_to(bare) for p in bare.rglob("*"))

    exit_code = main(argv=["upgrade"], cwd=bare)

    assert exit_code == 3
    assert "working tree" in capsys.readouterr().err

    after = sorted(p.relative_to(bare) for p in bare.rglob("*"))
    assert after == before
    assert not (bare / "git-ots.lock").exists()


def test_orphan_toml_with_real_ots_keys_set_never_surfaces_in_output(
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """AC-ORPHAN-1: a `git-ots.toml` at the worktree root, holding valid TOML
    and `ots.*`-shaped keys, is not read -- with real `ots.*` git-config keys
    also set, nothing about the file's presence or contents appears on
    stdout or stderr. The orphan file's `command` value is a marker string
    that would appear in `status`'s "not found in PATH" warning if it were
    ever read; asserting its absence is a positive proof of non-reading, not
    merely an absence of a generic "Configuration error" string."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    _set_config(repo, max_age="1h", fetch_before_run=False)

    marker = "orphan-toml-command-marker-must-never-surface"
    (repo / "git-ots.toml").write_text(f'[opentimestamps]\ncommand = "{marker}"\n')

    exit_code = main(argv=["status"], cwd=repo, now=commit_date + timedelta(hours=2))
    captured = capsys.readouterr()

    assert exit_code == 0
    assert marker not in captured.out
    assert marker not in captured.err


@pytest.mark.parametrize(
    "command", ["run", "status", "validate", "verify", "upgrade", "config"]
)
def test_config_flag_is_not_accepted_by_any_subcommand(
    command: str, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """AC-CLIFLAG-1: `--config` is gone. No subcommand's argument parser
    accepts it; argparse's own usage-error exit code (2) is what a caller
    still passing it now sees."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    with pytest.raises(SystemExit) as excinfo:
        main(argv=[command, "--config", "anything"], cwd=repo)

    assert excinfo.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# S6: `git-ots config` -- behaviour 10, AC-REPORT-1.
# ---------------------------------------------------------------------------


def test_config_command_reports_effective_configuration_with_per_key_origin(
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """AC-REPORT-1's core case, end to end through the CLI: a key set in
    local scope only, alongside a key never set anywhere. Both appear, with
    the configured key's real value and origin and the unset key's default
    value and `default` origin -- discriminating against a stub that prints
    only what was configured, or only the built-in defaults."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _set_config(repo, tag_prefix="stamped/")

    exit_code = main(argv=["config"], cwd=repo)
    lines = capsys.readouterr().out.splitlines()

    assert exit_code == 0
    rows = {line.split()[0]: line.rstrip() for line in lines}
    assert set(rows) == {row.key for row in MAPPING}

    assert "stamped/" in rows["ots.tagprefix"]
    assert rows["ots.tagprefix"].endswith("git config local")

    # Never configured: the field's own built-in default value, with
    # origin `default` -- not the local scope, and not some other
    # placeholder.
    assert "inherit" in rows["ots.signing"]
    assert rows["ots.signing"].endswith("default")


def test_config_command_reports_narrower_scope_for_key_set_in_two_scopes(
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """AC-REPORT-1: `ots.tagPrefix` set in both global and local scope
    reports the narrower, winning `local` scope -- the wider `global`
    value must not appear anywhere in that key's row."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--global", "ots.tagPrefix", "wide/"], cwd=repo)
    _git(["config", "--local", "ots.tagPrefix", "narrow/"], cwd=repo)

    main(argv=["config"], cwd=repo)
    lines = capsys.readouterr().out.splitlines()
    row = next(line.rstrip() for line in lines if line.startswith("ots.tagprefix"))

    assert "narrow/" in row
    assert row.endswith("git config local")
    assert "wide/" not in row
    assert "git config global" not in row


def test_config_command_reports_default_for_a_trigger_the_ladder_discards(
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """The story's central design question, at the CLI/report layer: a
    wider-scope `ots.maxAge` that behaviour 8's precedence ladder discards
    in favour of a narrower-scope `ots.fixedTime` reports `default`, not
    the `global` scope it lost in -- the effective configuration does not
    carry that `max_age` value at all
    (gitconfig.py's `test_describe_effective_configuration_reports_default_
    for_a_trigger_the_ladder_discards` covers this at the unit layer)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--global", "ots.maxAge", "12h"], cwd=repo)
    _set_config(repo, fixed_time="03:00", timezone="UTC")

    main(argv=["config"], cwd=repo)
    lines = [line.rstrip() for line in capsys.readouterr().out.splitlines()]
    max_age_row = next(line for line in lines if line.startswith("ots.maxage"))
    fixed_time_row = next(line for line in lines if line.startswith("ots.fixedtime"))

    assert max_age_row.endswith("default")
    assert "12h" not in max_age_row
    assert "<unset>" in max_age_row
    assert fixed_time_row.endswith("git config local")


def test_config_command_output_vocabulary_is_exactly_the_six_permitted_origins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC-REPORT-1: the full origin vocabulary is exactly `{git config
    system, git config global, git config local, git config worktree, git
    config command, default}` -- no other origin kind. Exercised end to end
    through five real Git scopes plus the default fallback (not merely
    faked, except where a real fixture cannot reach the scope at all: see
    the note on `system` below), and asserted as exact set equality against
    the six-item vocabulary, not mere membership.

    `git config --system` is not writable under the suite's default
    isolation (`GIT_CONFIG_SYSTEM=/dev/null`, conftest.py's
    `_isolated_git_config`); this test narrows that isolation deliberately
    for itself, pointing `GIT_CONFIG_SYSTEM` at a real, writable, per-test
    file and turning `GIT_CONFIG_NOSYSTEM` off, so system scope is
    exercised for real rather than left uncovered.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "extensions.worktreeConfig", "true"], cwd=repo)

    system_config = tmp_path / "system-gitconfig"
    system_config.touch()
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "0")

    _git(["config", "--system", "ots.sourceRef", "refs/heads/system"], cwd=repo)
    _git(["config", "--global", "ots.command", "global-ots"], cwd=repo)
    _git(["config", "--local", "ots.signing", "required"], cwd=repo)
    _git(["config", "--worktree", "ots.initialHistory", "all"], cwd=repo)
    # ots.tagPrefix is left unset anywhere -> supplies "default".

    venv_bin = str(Path(sys.executable).parent)
    env = {
        **os.environ,
        "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
    }
    completed = subprocess.run(
        ["git", "-c", "ots.fetchBeforeRun=false", "ots", "config"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    vocabulary = {
        "git config system",
        "git config global",
        "git config local",
        "git config worktree",
        "git config command",
        "default",
    }
    seen: set[str] = set()
    for line in completed.stdout.splitlines():
        stripped = line.rstrip()
        matches = [origin for origin in vocabulary if stripped.endswith(origin)]
        assert len(matches) == 1, f"line has an unrecognised origin: {line!r}"
        seen.add(matches[0])

    assert seen == vocabulary

    lines = {line.split()[0]: line.rstrip() for line in completed.stdout.splitlines()}
    assert lines["ots.sourceref"].endswith("git config system")
    assert lines["ots.command"].endswith("git config global")
    assert lines["ots.signing"].endswith("git config local")
    assert lines["ots.initialhistory"].endswith("git config worktree")
    assert lines["ots.fetchbeforerun"].endswith("git config command")
    assert lines["ots.tagprefix"].endswith("default")


def test_config_command_is_read_only(tmp_path: Path) -> None:
    """VQ-S6-002: `git-ots config` takes no arguments and writes nothing --
    the FS-0014 regression guard used throughout this epic, applied here.
    `git status --porcelain --untracked-files=all` is unaffected, and the
    `ots.*` namespace itself is unchanged."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _set_config(repo, tag_prefix="stamped/", max_age="6h")

    before_status = _git(["status", "--porcelain", "--untracked-files=all"], cwd=repo)
    before_config = _git(
        ["config", "-z", "--show-scope", "--get-regexp", r"^ots\."], cwd=repo
    )

    exit_code = main(argv=["config"], cwd=repo)

    assert exit_code == 0
    assert (
        _git(["status", "--porcelain", "--untracked-files=all"], cwd=repo)
        == before_status
    )
    assert (
        _git(["config", "-z", "--show-scope", "--get-regexp", r"^ots\."], cwd=repo)
        == before_config
    )


def test_config_command_succeeds_against_a_bare_clone_reading_global_scope(
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """Behaviour 4: reading the layer does not require a worktree, so
    `git-ots config` works the same in a bare repository as `verify` does
    (mirrors `test_verify_succeeds_against_a_bare_clone_reading_global_scope`)."""
    seed = tmp_path / "seed"
    _init_repo(seed)
    _commit(seed, paths=["a.txt"], message="A\n")

    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(["init", "-q", "--bare", "-b", "main", "."], cwd=bare)
    _git(["remote", "add", "origin", str(bare)], cwd=seed)
    _git(["push", "-q", "origin", "main"], cwd=seed)

    _git(["config", "--global", "ots.tagPrefix", "bare/"], cwd=bare)

    exit_code = main(argv=["config"], cwd=bare)
    lines = capsys.readouterr().out.splitlines()
    row = next(line.rstrip() for line in lines if line.startswith("ots.tagprefix"))

    assert exit_code == 0
    assert "bare/" in row
    assert row.endswith("git config global")
