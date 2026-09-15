"""Tests for the safe Git command runner and repository discovery.

The runner boundary must guarantee that Git is always invoked as an
argument array in a fixed working directory with captured text output,
and that shell mode can never be requested. Repository discovery must
locate the repository root and the absolute common Git directory,
including from inside a linked worktree. See specs/spec.md sections 24,
33, and 41.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from git_ots.git import (
    _DEFAULT_GIT_TIMEOUT_SECONDS,
    CommitInfo,
    FetchResult,
    GitCommandError,
    GitCommandTimeoutError,
    GitRunner,
    GitSuccess,
    InvalidRepositoryStateError,
    ObjectFormat,
    RepositoryLayout,
    RepositoryLock,
    RepositoryLockedError,
    ResolvedRef,
    _default_process_runner,
    _default_process_runner_bytes,
    _kill_process_group,
    _run_bounded_git_subprocess,
    assert_committable_state,
    assert_worktree_present,
    detect_object_format,
    enumerate_commits,
    fetch_remote,
    has_generated_trailer,
    is_worktree_clean,
    locate_repository,
    make_process_runner,
    make_process_runner_bytes,
    push_current_branch,
    resolve_source_ref,
    stage_paths,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _recording_runner(
    recorded: list[dict], *, exit_code: int = 0, stdout: str = "", stderr: str = ""
):
    def _runner(argv, *, cwd, stdin=None):
        recorded.append(
            {
                "argv": argv,
                "cwd": cwd,
                "stdin": stdin,
            }
        )
        return exit_code, stdout, stderr

    return _runner


def test_push_current_branch_uses_follow_tags(tmp_path: Path) -> None:
    recorded: list[dict] = []

    push_current_branch(
        cwd=tmp_path,
        process_runner=_recording_runner(recorded),
    )

    assert recorded == [
        {"argv": ["git", "push", "--follow-tags"], "cwd": tmp_path, "stdin": None}
    ]


def test_runner_invokes_with_argument_array_and_fixed_cwd(tmp_path: Path) -> None:
    recorded: list[dict] = []
    runner = GitRunner(
        cwd=tmp_path,
        process_runner=_recording_runner(recorded, stdout="ok\n"),
    )

    result = runner.run(["rev-parse", "HEAD"])

    assert isinstance(result, GitSuccess)
    assert result.stdout == "ok\n"
    assert result.stderr == ""
    assert result.exit_code == 0
    assert recorded == [
        {
            "argv": ["git", "rev-parse", "HEAD"],
            "cwd": tmp_path,
            "stdin": None,
        }
    ]


def _recording_popen_class(captured: dict) -> type[subprocess.Popen]:
    """Build a real ``Popen`` subclass that records the kwargs it receives
    into ``captured``, then behaves exactly like the real thing.

    Extends the precedent this module previously used for
    ``subprocess.run`` (kwarg-capturing via a stub) to ``subprocess.Popen``,
    following ``tests/test_timestamp.py:test_default_runner_spawns_with_start_new_session``:
    the production runners now use ``Popen`` directly rather than
    ``subprocess.run``, so they can retain the child's pgid for
    ``os.killpg`` on expiry -- see ``_run_bounded_git_subprocess``'s
    docstring. ``communicate`` is overridden too, so the same ``captured``
    dict also gets the ``timeout=`` kwarg, which ``Popen``'s constructor
    never sees.
    """

    class _RecordingPopen(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

        def communicate(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return super().communicate(*args, **kwargs)

    return _RecordingPopen


def test_runner_uses_no_shell_and_captures_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The default process runner must spawn with shell=False, text-mode
    # capture, and (AC-PROC-4) a non-None timeout plus start_new_session.
    # Verified by inspecting what a real Popen subclass receives.
    captured: dict = {}
    monkeypatch.setattr(
        "git_ots.git.subprocess.Popen", _recording_popen_class(captured)
    )

    runner = GitRunner(cwd=tmp_path)
    result = runner.run(["--version"])

    assert isinstance(result, GitSuccess)
    assert captured["shell"] is False
    assert captured["text"] is True
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "surrogateescape"
    assert captured["cwd"] == tmp_path
    # AC-PROC-4: the unconfigured default MUST pass a non-None ceiling and
    # start_new_session=True to the process it spawns.
    assert captured["start_new_session"] is True
    assert captured["timeout"] == pytest.approx(60.0)


def test_default_process_runner_bytes_spawns_bounded_and_in_a_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-PROC-4: the bytes variant must not be left unbounded.

    An implementation that bounds only the text runner (or only a
    separately-constructed *configured* runner) while leaving
    ``_default_process_runner_bytes`` itself unbounded must fail this --
    this is a distinct call to a distinct function, not an assertion "by
    symmetry" with the text runner's test above.
    """
    captured: dict = {}
    monkeypatch.setattr(
        "git_ots.git.subprocess.Popen", _recording_popen_class(captured)
    )

    exit_code, _stdout, _stderr = _default_process_runner_bytes(
        ["git", "--version"], cwd=tmp_path
    )

    assert exit_code == 0
    assert captured["shell"] is False
    assert "text" not in captured or captured["text"] is not True
    assert captured["start_new_session"] is True
    assert captured["timeout"] == pytest.approx(60.0)


def test_default_process_runner_invoked_directly_with_no_config_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-PROC-4, driven exactly as cli.py:_locate_repository's pre-config
    calls drive it: the default runner invoked directly, with no
    ``GitRunner``, no configured runner, and no ``Config`` in scope at all.
    """
    captured: dict = {}
    monkeypatch.setattr(
        "git_ots.git.subprocess.Popen", _recording_popen_class(captured)
    )

    exit_code, _stdout, _stderr = _default_process_runner(
        ["git", "--version"], cwd=tmp_path
    )

    assert exit_code == 0
    assert captured["start_new_session"] is True
    assert captured["timeout"] == pytest.approx(60.0)


def test_builtin_git_timeout_matches_limits_config_default() -> None:
    """The built-in default is duplicated because git.py cannot import
    config.py (config.py sits above git.py in architecture.md's leaf-to-root
    dependency order, so the reverse edge is forbidden). Nothing at module
    level keeps ``_DEFAULT_GIT_TIMEOUT_SECONDS`` and
    ``LimitsConfig.git_timeout``'s default in sync -- this test is the only
    thing that does. If it fails, the two values were changed independently;
    bring them back in agreement (in whichever module actually needs the
    new default), do not "fix" this test to match just one of them.
    """
    from git_ots.config import LimitsConfig

    assert _DEFAULT_GIT_TIMEOUT_SECONDS == LimitsConfig().git_timeout.total_seconds()


def test_make_process_runner_builds_a_runner_bounded_at_the_given_timeout(
    tmp_path: Path,
) -> None:
    """A runner built by make_process_runner is bounded at the timeout it
    was constructed with, not the built-in default -- the seam S4 needs to
    supply a *configured* ceiling (a value distinguishable from
    ``_DEFAULT_GIT_TIMEOUT_SECONDS``) actually has an effect.
    """
    runner = make_process_runner(timeout=0.1)
    argv = [sys.executable, "-c", "import time; time.sleep(600)"]

    started = time.monotonic()
    with pytest.raises(GitCommandTimeoutError) as excinfo:
        runner(argv, cwd=tmp_path, stdin=None)
    elapsed = time.monotonic() - started

    # Bounded at the configured 0.1s, not the built-in default (60s) and
    # not the sleeping child's 600s.
    assert elapsed < 5.0
    assert excinfo.value.timeout == pytest.approx(0.1)


def test_make_process_runner_bytes_builds_a_runner_bounded_at_the_given_timeout(
    tmp_path: Path,
) -> None:
    """The bytes variant of the factory is independently bounded too."""
    runner = make_process_runner_bytes(timeout=0.1)
    argv = [sys.executable, "-c", "import time; time.sleep(600)"]

    started = time.monotonic()
    with pytest.raises(GitCommandTimeoutError) as excinfo:
        runner(argv, cwd=tmp_path, stdin=None)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert excinfo.value.timeout == pytest.approx(0.1)


def test_make_process_runner_handles_bytes_stdin_correctly(tmp_path: Path) -> None:
    """A runner built by make_process_runner must accept real ``bytes``
    stdin -- the ``ProcessRunner`` contract ``GitRunner.run`` calls
    through. This drives an arbitrary command directly (not through
    ``GitRunner``); the actual production call sites -- ``git.py:827`` /
    ``:1165``'s ``interpret-trailers --parse`` calls, which feed
    ``body.encode("utf-8")`` as stdin -- are covered separately below by
    ``test_make_process_runner_reaches_has_generated_trailer_production_shape``,
    since a test aimed at an unrelated command would never reach that code
    and would pass against a decode-skipping defect there.

    A bare ``functools.partial(_run_bounded_git_subprocess, timeout=t,
    text=True)`` fails this with ``AttributeError: 'bytes' object has no
    attribute 'encode'`` because it skips the stdin decode step -- that was
    the concrete bug the reviewer found and this factory exists to close.
    """
    runner = make_process_runner(timeout=5.0)
    payload = b"hello world \xf0\x9f\x9a\x80"  # includes a non-ASCII rocket emoji

    exit_code, stdout, stderr = runner(
        ["git", "hash-object", "--stdin"], cwd=tmp_path, stdin=payload
    )

    assert exit_code == 0
    assert stdout.strip()
    assert stderr == ""


def test_make_process_runner_bytes_handles_bytes_stdin_correctly(
    tmp_path: Path,
) -> None:
    """The bytes variant needs no decode step, but must still round-trip
    real bytes stdin correctly through the factory."""
    runner = make_process_runner_bytes(timeout=5.0)
    payload = b"hello world \xf0\x9f\x9a\x80"

    exit_code, stdout, stderr = runner(
        ["git", "hash-object", "--stdin"], cwd=tmp_path, stdin=payload
    )

    assert exit_code == 0
    assert isinstance(stdout, bytes)
    assert stdout.strip()
    assert stderr == b""


def test_make_process_runner_with_timeout_none_is_unbounded(tmp_path: Path) -> None:
    """timeout=None is the operator's unbounded escape hatch (git_timeout =
    "0") -- a runner built with it must not be interrupted by a command
    that briefly outlasts the built-in default's magnitude, unlike a
    runner built with a short explicit timeout.
    """
    runner = make_process_runner(timeout=None)

    exit_code, stdout, _stderr = runner(["git", "--version"], cwd=tmp_path, stdin=None)

    assert exit_code == 0
    assert "git version" in stdout


def test_functools_partial_of_the_private_helper_breaks_on_bytes_stdin(
    tmp_path: Path,
) -> None:
    """Documents, by construction, the exact trap make_process_runner exists
    to close: building a runner with a bare
    ``functools.partial(_run_bounded_git_subprocess, ..., text=True)``
    instead of going through the factory skips the stdin decode step and
    fails on real bytes stdin -- the shape every real ``ProcessRunner``
    caller (``GitRunner.run``) actually uses.
    """
    import functools

    broken_runner = functools.partial(
        _run_bounded_git_subprocess, timeout=5.0, text=True
    )

    with pytest.raises(AttributeError):
        broken_runner(
            ["git", "hash-object", "--stdin"],
            cwd=tmp_path,
            stdin=b"this is bytes, not str",
        )


def test_runner_returns_typed_error_on_nonzero_exit(tmp_path: Path) -> None:
    runner = GitRunner(
        cwd=tmp_path,
        process_runner=_recording_runner(
            [], exit_code=128, stderr="fatal: not a git repository\n"
        ),
    )

    with pytest.raises(GitCommandError) as excinfo:
        runner.run(["rev-parse", "HEAD"])

    err = excinfo.value
    assert err.exit_code == 128
    assert "not a git repository" in err.stderr
    assert err.argv == ["git", "rev-parse", "HEAD"]
    assert "rev-parse" in str(err)


def test_runner_never_accepts_shell_strings(tmp_path: Path) -> None:
    runner = GitRunner(
        cwd=tmp_path,
        process_runner=_recording_runner([]),
    )

    with pytest.raises(TypeError):
        runner.run("rev-parse HEAD")  # type: ignore[arg-type]


def test_default_process_runner_decodes_tree_text_as_utf8_under_c_locale(
    tmp_path: Path,
) -> None:
    """Text read from a commit tree must be byte-identical regardless of locale.

    ``subprocess.run(text=True)`` picks its decoder from the parent process
    locale when ``encoding`` is omitted. Under ``LC_ALL=C`` that silently
    corrupts UTF-8 bytes into mojibake. The runner must therefore pass an
    explicit ``encoding="utf-8"``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "--quiet"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test User"], cwd=repo)

    manifest = {
        "schema": 1,
        "source_ref": "refs/heads/souérce",
        "source_commit": "a" * 40,
        "submitted_at": "2024-01-01T00:00:00+00:00",
        "proof_name": "proof.ots",
        "object_format": "sha1",
        "payload_format": "git:sha1",
        "triggers": [],
    }
    manifest_path = repo / "ots" / "manifest.json"
    manifest_path.parent.mkdir()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    _git(["add", "ots/manifest.json"], cwd=repo)
    _git(["commit", "-m", "add manifest", "--quiet"], cwd=repo)

    commit = _git(["rev-parse", "HEAD"], cwd=repo).strip()

    script = tmp_path / "read_manifest.py"
    script.write_text(
        f"""\
import json
import sys
from pathlib import Path

sys.path.insert(0, {str(PROJECT_ROOT / "src")!r})

from git_ots.git import _read_tree_text

text = _read_tree_text(
    cwd=Path({str(repo)!r}),
    commit={commit!r},
    path="ots/manifest.json",
)
parsed = json.loads(text)
assert parsed["source_ref"] == "refs/heads/souérce"
""",
        encoding="utf-8",
    )
    # A child interpreter is required, not merely convenient. `subprocess.run`
    # selects its decoder via `locale.getpreferredencoding(False)` in the
    # *parent* at call time, so mutating `os.environ["LC_ALL"]` inside this test
    # would not reliably change it and the assertion would pass vacuously. Do
    # not "simplify" this into an in-process environment patch.
    env = {**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0"}
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        env=env,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Repository discovery.
# ---------------------------------------------------------------------------


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
    (path / "README").write_text("seed\n")
    _git(["add", "README"], cwd=path)
    _git(["commit", "-q", "-m", "seed"], cwd=path)


def test_locate_repository_in_main_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    layout = locate_repository(cwd=repo)

    assert isinstance(layout, RepositoryLayout)
    assert layout.worktree_root == repo.resolve()
    assert layout.common_git_dir == (repo / ".git").resolve()
    assert layout.common_git_dir.is_absolute()


def test_locate_repository_from_subdirectory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)

    layout = locate_repository(cwd=nested)

    assert layout.worktree_root == repo.resolve()
    assert layout.common_git_dir == (repo / ".git").resolve()


def test_locate_repository_in_linked_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    worktree = tmp_path / "wt"
    _git(["worktree", "add", "-q", str(worktree)], cwd=repo)

    layout = locate_repository(cwd=worktree)

    assert layout.worktree_root == worktree.resolve()
    # The common Git dir of a linked worktree is the main repository's .git.
    assert layout.common_git_dir == (repo / ".git").resolve()
    assert layout.common_git_dir.is_absolute()


def test_locate_repository_rejects_non_repository(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    with pytest.raises(GitCommandError):
        locate_repository(cwd=not_a_repo)


def test_locate_repository_against_a_bare_repository(tmp_path: Path) -> None:
    """FS-0015 AC-BARE-1: a bare repository has no worktree, so
    ``--show-toplevel`` fails there; ``locate_repository`` falls back to
    ``--absolute-git-dir`` for ``worktree_root`` rather than raising, so
    callers that only need `cwd`-scoped Git plumbing (the `ots.*` config
    layer read, ref resolution) keep working.

    ``is_bare`` records that fallback explicitly so write paths (``run``,
    ``upgrade``) can refuse to operate on the Git directory that
    ``worktree_root`` now points at, instead of inferring bareness by
    comparing paths downstream -- see ``assert_worktree_present``.
    """
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(["init", "-q", "--bare", "-b", "main", "."], cwd=bare)

    layout = locate_repository(cwd=bare)

    assert layout.worktree_root == bare.resolve()
    assert layout.common_git_dir == bare.resolve()
    assert layout.is_bare is True


def test_locate_repository_against_a_normal_repository_is_not_bare(
    tmp_path: Path,
) -> None:
    """The counterpart to the bare-repository case above: a normal
    repository (with a checked-out worktree) must not be flagged bare."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main", "."], cwd=repo)

    layout = locate_repository(cwd=repo)

    assert layout.is_bare is False


def test_assert_worktree_present_rejects_a_bare_layout(tmp_path: Path) -> None:
    """The write-path guard raises on a bare layout, naming the refused
    operation in the message, and leaves a non-bare layout untouched."""
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(["init", "-q", "--bare", "-b", "main", "."], cwd=bare)
    layout = locate_repository(cwd=bare)

    with pytest.raises(InvalidRepositoryStateError) as excinfo:
        assert_worktree_present(layout, operation="run")

    assert "run" in str(excinfo.value)
    assert "bare" in str(excinfo.value)


def test_assert_worktree_present_allows_a_normal_layout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main", "."], cwd=repo)
    layout = locate_repository(cwd=repo)

    assert_worktree_present(layout, operation="run")  # does not raise


def test_locate_repository_rejects_non_repository_even_after_the_bare_fallback(
    tmp_path: Path,
) -> None:
    """The bare-repository fallback must not swallow the "not a repository at
    all" case: when neither ``--show-toplevel`` nor ``--absolute-git-dir``
    succeeds, the original ``--show-toplevel`` error propagates."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    with pytest.raises(GitCommandError) as excinfo:
        locate_repository(cwd=not_a_repo)

    assert "show-toplevel" in " ".join(excinfo.value.argv)


# ---------------------------------------------------------------------------
# Source ref resolution.
# ---------------------------------------------------------------------------


def test_resolve_source_ref_returns_full_id_and_display_ref(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    resolved = resolve_source_ref(cwd=repo, ref="HEAD")

    assert isinstance(resolved, ResolvedRef)
    # Full object ID: 40 hex characters for SHA-1.
    assert len(resolved.commit_id) == 40
    assert all(c in "0123456789abcdef" for c in resolved.commit_id)
    # The display ref is the concrete, fully-qualified ref (not "HEAD").
    assert resolved.display_ref == "refs/heads/main"
    # The resolved commit ID matches what Git reports for HEAD directly.
    head_id = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    assert resolved.commit_id == head_id


def test_resolve_source_ref_unknown_ref_raises_invalid_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    with pytest.raises(InvalidRepositoryStateError):
        resolve_source_ref(cwd=repo, ref="refs/heads/does-not-exist")


# ---------------------------------------------------------------------------
# Object format detection.
# ---------------------------------------------------------------------------


def test_detect_object_format_sha1(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    fmt = detect_object_format(cwd=repo)

    assert isinstance(fmt, ObjectFormat)
    assert fmt == ObjectFormat.SHA1
    assert fmt.value == "sha1"


def test_detect_object_format_sha256_when_supported(tmp_path: Path) -> None:
    repo = tmp_path / "repo-sha256"
    repo.mkdir(parents=True)
    try:
        _git(
            ["init", "-q", "-b", "main", "--object-format=sha256", "."],
            cwd=repo,
        )
    except subprocess.CalledProcessError:
        pytest.skip("installed Git does not support --object-format=sha256")

    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test"], cwd=repo)
    (repo / "README").write_text("seed\n")
    _git(["add", "README"], cwd=repo)
    _git(["commit", "-q", "-m", "seed"], cwd=repo)

    fmt = detect_object_format(cwd=repo)

    assert fmt == ObjectFormat.SHA256
    assert fmt.value == "sha256"
    # A SHA-256 commit ID is 64 hex characters.
    head_id = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    assert len(head_id) == 64


# ---------------------------------------------------------------------------
# Fetch without integrating changes.
# ---------------------------------------------------------------------------


def _init_remote_pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create a bare remote and a clone with a checked-out main branch.

    Returns ``(remote, seed_repo, clone)``. The remote contains the initial
    commit only; ``seed_repo`` is the working repository used to push new
    commits to the remote; ``clone`` is the consumer repository under test.
    """

    remote = tmp_path / "remote.git"
    remote.mkdir(parents=True)
    _git(["init", "-q", "--bare", "-b", "main", "."], cwd=remote)

    seed = tmp_path / "seed"
    seed.mkdir(parents=True)
    _git(["init", "-q", "-b", "main", "."], cwd=seed)
    _git(["config", "user.email", "test@example.com"], cwd=seed)
    _git(["config", "user.name", "Test"], cwd=seed)
    (seed / "README").write_text("seed\n")
    _git(["add", "README"], cwd=seed)
    _git(["commit", "-q", "-m", "seed"], cwd=seed)
    _git(["remote", "add", "origin", str(remote)], cwd=seed)
    _git(["push", "-q", "-u", "origin", "main"], cwd=seed)

    clone = tmp_path / "clone"
    _git(["clone", "-q", str(remote), str(clone)], cwd=tmp_path)
    _git(["config", "user.email", "test@example.com"], cwd=clone)
    _git(["config", "user.name", "Test"], cwd=clone)

    return remote, seed, clone


def test_fetch_remote_advances_tracking_ref_without_touching_worktree(
    tmp_path: Path,
) -> None:
    _remote, seed, clone = _init_remote_pair(tmp_path)

    # Capture clone state before the remote advances.
    head_before = _git(["rev-parse", "HEAD"], cwd=clone).strip()
    tracking_before = _git(["rev-parse", "refs/remotes/origin/main"], cwd=clone).strip()
    assert head_before == tracking_before
    status_before = _git(["status", "--porcelain"], cwd=clone)
    index_before = _git(["ls-files", "--stage"], cwd=clone)

    # Advance the remote with a new commit pushed from the seed repository.
    (seed / "NEW").write_text("new content\n")
    _git(["add", "NEW"], cwd=seed)
    _git(["commit", "-q", "-m", "second"], cwd=seed)
    _git(["push", "-q", "origin", "main"], cwd=seed)
    new_remote_id = _git(["rev-parse", "main"], cwd=seed).strip()
    assert new_remote_id != head_before

    result = fetch_remote(cwd=clone, remote="origin")

    assert isinstance(result, FetchResult)
    assert result.remote == "origin"

    # Remote-tracking ref advanced; HEAD and worktree state did not.
    tracking_after = _git(["rev-parse", "refs/remotes/origin/main"], cwd=clone).strip()
    assert tracking_after == new_remote_id
    assert _git(["rev-parse", "HEAD"], cwd=clone).strip() == head_before
    # The newly pushed file exists in the remote but is not in our worktree.
    assert not (clone / "NEW").exists()
    assert _git(["status", "--porcelain"], cwd=clone) == status_before
    assert _git(["ls-files", "--stage"], cwd=clone) == index_before


def test_fetch_remote_uses_argument_array_without_shell(tmp_path: Path) -> None:
    recorded: list[dict] = []
    fake = _recording_runner(recorded, stdout="", stderr="")

    result = fetch_remote(cwd=tmp_path, remote="origin", process_runner=fake)

    assert isinstance(result, FetchResult)
    assert recorded == [
        {
            "argv": ["git", "fetch", "--prune", "origin"],
            "cwd": tmp_path,
            "stdin": None,
        }
    ]


def test_fetch_remote_failure_raises_git_command_error(tmp_path: Path) -> None:
    fake = _recording_runner(
        [], exit_code=128, stderr="fatal: could not read from remote\n"
    )

    with pytest.raises(GitCommandError) as excinfo:
        fetch_remote(cwd=tmp_path, remote="origin", process_runner=fake)

    assert "could not read from remote" in excinfo.value.stderr


# ---------------------------------------------------------------------------
# Upstream-only eligibility.
# ---------------------------------------------------------------------------


def test_upstream_resolution_ignores_unpushed_local_commits(tmp_path: Path) -> None:
    # origin/main = A (pushed); HEAD = B (unpushed). Resolving @{upstream}
    # must yield A, never silently fall back to HEAD. See spec sections 2.4
    # and 16, and acceptance case 42.13.
    _remote, _seed, clone = _init_remote_pair(tmp_path)

    a_id = _git(["rev-parse", "HEAD"], cwd=clone).strip()

    # Create an unpushed local commit B on top of A.
    (clone / "B.txt").write_text("unpushed\n")
    _git(["add", "B.txt"], cwd=clone)
    _git(["commit", "-q", "-m", "B"], cwd=clone)
    b_id = _git(["rev-parse", "HEAD"], cwd=clone).strip()
    assert b_id != a_id

    resolved = resolve_source_ref(cwd=clone, ref="@{upstream}")

    assert isinstance(resolved, ResolvedRef)
    assert resolved.commit_id == a_id
    assert resolved.commit_id != b_id
    # The concrete remote-tracking ref is exposed, not "HEAD".
    assert "origin" in resolved.display_ref
    assert "HEAD" not in resolved.display_ref


def test_resolve_source_ref_does_not_silently_fall_back_to_head(
    tmp_path: Path,
) -> None:
    # If @{upstream} is unconfigured, resolution must fail rather than
    # silently substituting HEAD. See spec section 15 and the "no silent
    # fallback" rule for upstream-only source refs.
    repo = tmp_path / "repo"
    _init_repo(repo)

    with pytest.raises(InvalidRepositoryStateError):
        resolve_source_ref(cwd=repo, ref="@{upstream}")


# ---------------------------------------------------------------------------
# Commit enumeration.
# ---------------------------------------------------------------------------


def _commit_with_committer_time(
    repo: Path, *, name: str, message: str, committer_iso: str
) -> str:
    """Create a commit with a controlled committer timestamp."""
    (repo / name).write_text(f"{message}\n")
    _git(["add", name], cwd=repo)
    env = dict(**__import__("os").environ)
    env["GIT_COMMITTER_DATE"] = committer_iso
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return _git(["rev-parse", "HEAD"], cwd=repo).strip()


def test_enumerate_commits_oldest_to_newest_with_aware_committer_times(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(["init", "-q", "-b", "main", "."], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test"], cwd=repo)

    a_id = _commit_with_committer_time(
        repo,
        name="A",
        message="A",
        committer_iso="2026-01-01T10:00:00+00:00",
    )
    b_id = _commit_with_committer_time(
        repo,
        name="B",
        message="B",
        committer_iso="2026-01-01T11:00:00+00:00",
    )
    c_id = _commit_with_committer_time(
        repo,
        name="C",
        message="C",
        committer_iso="2026-01-01T12:00:00+00:00",
    )

    commits = enumerate_commits(cwd=repo, ref="HEAD")

    assert [c.commit_id for c in commits] == [a_id, b_id, c_id]
    assert all(isinstance(c, CommitInfo) for c in commits)
    # Full object IDs (40 hex chars for SHA-1).
    assert all(len(c.commit_id) == 40 for c in commits)
    # Committer timestamps are timezone-aware.
    assert all(
        c.committer_time.tzinfo is not None and c.committer_time.utcoffset() is not None
        for c in commits
    )
    # Oldest-to-newest ordering matches committer times.
    assert commits[0].committer_time < commits[1].committer_time
    assert commits[1].committer_time < commits[2].committer_time


def test_enumerate_commits_uses_committer_not_author_time(tmp_path: Path) -> None:
    # The maximum-age rule depends on committer timestamps (spec 6.2). If
    # enumeration accidentally used author dates, the order would be wrong
    # when they disagree.
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(["init", "-q", "-b", "main", "."], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test"], cwd=repo)

    # Author date deliberately out of order relative to committer date.
    import os

    (repo / "A").write_text("A\n")
    _git(["add", "A"], cwd=repo)
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = "2026-01-05T00:00:00+00:00"
    env["GIT_COMMITTER_DATE"] = "2026-01-01T10:00:00+00:00"
    subprocess.run(
        ["git", "commit", "-q", "-m", "A"],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    a_id = _git(["rev-parse", "HEAD"], cwd=repo).strip()

    (repo / "B").write_text("B\n")
    _git(["add", "B"], cwd=repo)
    env["GIT_AUTHOR_DATE"] = "2026-01-01T00:00:00+00:00"  # earlier author
    env["GIT_COMMITTER_DATE"] = "2026-01-01T11:00:00+00:00"
    subprocess.run(
        ["git", "commit", "-q", "-m", "B"],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    b_id = _git(["rev-parse", "HEAD"], cwd=repo).strip()

    commits = enumerate_commits(cwd=repo, ref="HEAD")

    # Order is by committer time (A then B), not author time (B then A).
    assert [c.commit_id for c in commits] == [a_id, b_id]


def test_enumerate_commits_unknown_ref_raises_invalid_state(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    with pytest.raises(InvalidRepositoryStateError):
        enumerate_commits(cwd=repo, ref="refs/heads/does-not-exist")


# ---------------------------------------------------------------------------
# Generated-trailer parsing.
# ---------------------------------------------------------------------------


def _commit_with_message(repo: Path, *, name: str, message: str) -> str:
    (repo / name).write_text(f"{name}\n")
    _git(["add", name], cwd=repo)
    _git(["commit", "-q", "-m", message], cwd=repo)
    return _git(["rev-parse", "HEAD"], cwd=repo).strip()


def test_generated_trailer_exact_match_detected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_id = _commit_with_message(
        repo,
        name="a",
        message="Store OpenTimestamps proof\n\nOpenTimestamps-Generated: true\n",
    )

    assert has_generated_trailer(cwd=repo, commit=commit_id) is True


def test_make_process_runner_reaches_has_generated_trailer_production_shape(
    tmp_path: Path,
) -> None:
    """Regression (team-lead correction, remediation round 2): a
    factory-built runner must correctly handle real bytes stdin at the
    actual production call site the team lead identified --
    ``has_generated_trailer``'s ``git interpret-trailers --parse`` call
    (``git.py:827``), which feeds ``body.encode("utf-8")`` as stdin. This
    is the generated-commit *detection* path -- load-bearing for recovery
    and idempotency (spec.md sections 20, 22) -- not tag or commit
    creation as an earlier version of this brief mistakenly named. A test
    aimed at an unrelated command would never reach this code and would
    pass against a decode-skipping defect here.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_id = _commit_with_message(
        repo,
        name="a",
        message="Store OpenTimestamps proof\n\nOpenTimestamps-Generated: true\n",
    )

    result = has_generated_trailer(
        cwd=repo,
        commit=commit_id,
        process_runner=make_process_runner(timeout=5.0),
    )

    assert result is True


def test_make_process_runner_bytes_reaches_cat_file_batch_production_shape(
    tmp_path: Path,
) -> None:
    """Regression (team-lead correction, remediation round 2): the bytes
    variant must correctly round-trip real bytes stdin at a production
    bytes-path call site -- ``git.py:1864``'s ``git cat-file --batch``
    invocation (used by ``_batch_read_tree_texts``). That module-level
    function does not itself expose a ``process_runner_bytes`` injection
    point (only ``GitRunner.__init__`` does), so this drives the identical
    argv and stdin shape directly through ``GitRunner.run_bytes`` instead.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_id = _commit_with_message(repo, name="a", message="A\n")

    runner = GitRunner(
        cwd=repo, process_runner_bytes=make_process_runner_bytes(timeout=5.0)
    )
    stdin = f"{commit_id}:a".encode()

    result = runner.run_bytes(["cat-file", "--batch"], stdin=stdin)

    assert result.exit_code == 0
    assert b"a\n" in result.stdout


def test_generated_trailer_not_detected_in_plain_body_text(tmp_path: Path) -> None:
    # The exact phrase appearing as the subject or as a paragraph in the body
    # (not separated as a trailer) must not be treated as the trailer.
    repo = tmp_path / "repo"
    _init_repo(repo)

    subject_only = _commit_with_message(
        repo,
        name="a",
        message="OpenTimestamps-Generated: true",
    )
    assert has_generated_trailer(cwd=repo, commit=subject_only) is False

    body_text = _commit_with_message(
        repo,
        name="b",
        message=(
            "Some subject\n\n"
            "This body mentions OpenTimestamps-Generated: true in prose.\n"
        ),
    )
    assert has_generated_trailer(cwd=repo, commit=body_text) is False


def test_generated_trailer_false_value_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_id = _commit_with_message(
        repo,
        name="a",
        message="x\n\nOpenTimestamps-Generated: false\n",
    )

    assert has_generated_trailer(cwd=repo, commit=commit_id) is False


def test_generated_trailer_other_values_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    # ``git interpret-trailers`` normalizes whitespace around the value, so
    # a leading or trailing space would be collapsed to the canonical form;
    # we exercise distinct, non-``true`` values that must not be accepted.
    for i, value in enumerate(["yes", "1", "TRUE", "True", "on", "enable"]):
        commit_id = _commit_with_message(
            repo,
            name=f"f{i}",
            message=f"x\n\nOpenTimestamps-Generated: {value}\n",
        )
        assert has_generated_trailer(cwd=repo, commit=commit_id) is False, (
            f"value {value!r} must not be treated as the canonical trailer"
        )


def test_generated_trailer_misspelled_key_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    for i, key in enumerate(
        [
            "Opentimestamps-Generated",
            "OpenTimestamps-generated",
            "Open-Timestamps-Generated",
            "OpenTimestamps-Generatd",
            "OpenTimestamps-Generatedd",
        ]
    ):
        commit_id = _commit_with_message(
            repo,
            name=f"f{i}",
            message=f"x\n\n{key}: true\n",
        )
        assert has_generated_trailer(cwd=repo, commit=commit_id) is False, (
            f"misspelled key {key!r} must not be accepted"
        )


def test_generated_trailer_other_trailers_alone_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_id = _commit_with_message(
        repo,
        name="a",
        message=(
            "x\n\n"
            "OpenTimestamps-Source: 0123456789abcdef0123456789abcdef01234567\n"
            "Signed-off-by: T <t@example.com>\n"
        ),
    )

    assert has_generated_trailer(cwd=repo, commit=commit_id) is False


def test_generated_trailer_detected_among_other_trailers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_id = _commit_with_message(
        repo,
        name="a",
        message=(
            "x\n\n"
            "OpenTimestamps-Source: 0123456789abcdef0123456789abcdef01234567\n"
            "OpenTimestamps-Generated: true\n"
            "Signed-off-by: T <t@example.com>\n"
        ),
    )

    assert has_generated_trailer(cwd=repo, commit=commit_id) is True


def test_generated_trailer_unknown_commit_raises_invalid_state(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    with pytest.raises(InvalidRepositoryStateError):
        has_generated_trailer(cwd=repo, commit="refs/heads/does-not-exist")


# ---------------------------------------------------------------------------
# Worktree cleanliness.
# ---------------------------------------------------------------------------


def test_worktree_clean_on_fresh_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    assert is_worktree_clean(cwd=repo) is True


def test_worktree_dirty_with_tracked_edit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README").write_text("modified\n")

    assert is_worktree_clean(cwd=repo) is False


def test_worktree_dirty_with_staged_edit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README").write_text("staged\n")
    _git(["add", "README"], cwd=repo)

    assert is_worktree_clean(cwd=repo) is False


def test_worktree_dirty_with_untracked_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "new-file.txt").write_text("untracked\n")

    assert is_worktree_clean(cwd=repo) is False


def test_is_worktree_clean_uses_porcelain_argument_array(tmp_path: Path) -> None:
    recorded: list[dict] = []
    fake = _recording_runner(recorded, stdout="", stderr="")

    result = is_worktree_clean(cwd=tmp_path, process_runner=fake)

    assert result is True
    assert recorded == [
        {
            "argv": ["git", "status", "--porcelain", "--untracked-files=all"],
            "cwd": tmp_path,
            "stdin": None,
        }
    ]


# ---------------------------------------------------------------------------
# Explicit staging.
# ---------------------------------------------------------------------------


def test_stage_paths_uses_argument_array_and_explicit_pathspecs(
    tmp_path: Path,
) -> None:
    recorded: list[dict] = []
    fake = _recording_runner(recorded)

    stage_paths(
        cwd=tmp_path,
        paths=["a.ots", "b.json"],
        process_runner=fake,
    )

    assert recorded == [
        {
            "argv": ["git", "add", "--", "a.ots", "b.json"],
            "cwd": tmp_path,
            "stdin": None,
        }
    ]


def test_stage_paths_only_adds_named_paths_to_index(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    # Unrelated dirty tracked file and untracked file must not be staged.
    (repo / "README").write_text("modified\n")
    (repo / "unrelated.txt").write_text("untracked\n")

    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    (proof_dir / "abc.ots").write_text("proof\n")
    (proof_dir / "abc.json").write_text("manifest\n")

    stage_paths(
        cwd=repo,
        paths=[".opentimestamps/abc.ots", ".opentimestamps/abc.json"],
    )

    staged = _git(["diff", "--cached", "--name-only"], cwd=repo).splitlines()
    assert staged == [".opentimestamps/abc.json", ".opentimestamps/abc.ots"]


# ---------------------------------------------------------------------------
# Repository-level advisory lock.
# ---------------------------------------------------------------------------


def _hold_lock(
    common_git_dir: Path,
    started: multiprocessing.Event,
    release: multiprocessing.Event,
) -> None:
    """Acquire the repository lock and wait until ``release`` is set."""

    lock = RepositoryLock(common_git_dir=common_git_dir)
    lock.acquire()
    started.set()
    release.wait()
    lock.release()


def test_repository_lock_reports_locked_when_held_by_another_process(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    layout = locate_repository(cwd=repo)

    started = multiprocessing.Event()
    release = multiprocessing.Event()
    holder = multiprocessing.Process(
        target=_hold_lock,
        args=(layout.common_git_dir, started, release),
    )
    holder.start()
    try:
        assert started.wait(timeout=5.0), "lock holder did not start in time"

        contender = RepositoryLock(common_git_dir=layout.common_git_dir)
        with pytest.raises(RepositoryLockedError):
            contender.acquire()

        release.set()
        holder.join(timeout=5.0)
        assert not holder.is_alive(), "lock holder did not release in time"

        # After the holder releases, acquisition succeeds.
        contender.acquire()
        assert contender._fd is not None
        contender.release()
    finally:
        if holder.is_alive():
            release.set()
            holder.terminate()
            holder.join(timeout=5.0)

    # The lock file lives inside the common Git directory and no temp file
    # is left behind.
    assert (layout.common_git_dir / "git-ots.lock").exists()
    assert not any(
        p.name.startswith("git-ots.lock.tmp") for p in layout.common_git_dir.iterdir()
    )


def test_committable_state_flags_an_empty_operation_directory_as_stale(
    tmp_path: Path,
) -> None:
    """An empty rebase directory is a leftover, and the error should say so.

    Git reports "You are currently rebasing" from the directory's existence
    alone, so an empty `.git/rebase-merge/` left behind by a crashed tool -- or
    created by something that only mkdir'd it -- blocks work with no rebase to
    continue or abort. Matching Git's judgement is right, but the message must
    point at the real remedy instead of sending the reader hunting for a rebase
    that never happened.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    marker = subprocess.run(
        ["git", "rev-parse", "--git-path", "rebase-merge/"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (repo / marker).mkdir(parents=True)

    with pytest.raises(InvalidRepositoryStateError) as excinfo:
        assert_committable_state(cwd=repo, require_symbolic_head=True)

    message = str(excinfo.value)
    assert "rebase-merge/" in message
    assert "empty" in message
    assert "rmdir" in message


def test_committable_state_rejects_missing_identity_with_setup_guidance(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo-without-identity"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=repo, check=True)

    with pytest.raises(InvalidRepositoryStateError) as excinfo:
        assert_committable_state(cwd=repo, require_symbolic_head=True)

    message = str(excinfo.value)
    assert "identity" in message
    assert "git config user.name" in message
    assert "git config user.email" in message


# --- S3: bounding the git subprocesses (AC-ERR-2, AC-ERR-4, AC-PROC-4) -------


class _SleepingProcessRunner:
    """A ``ProcessRunner``/``ProcessRunnerBytes`` double that blocks past a
    configured ceiling and then raises, standing in for a real bounded
    runner without spawning an actual subprocess.

    Unlike ``timestamp.py``'s ``_SleepingRunner`` (a naive ``time.sleep()``
    with no timeout-awareness of its own, bounded from *outside* by
    ``OpenTimestampsCli._invoke``), this double must raise the timeout
    error itself: ``GitRunner`` has no generic wrapper analogous to
    ``_invoke`` that bounds an arbitrary injected runner -- per
    architecture.md Design Decision 9, the ceiling is injected at the
    runner seam itself (production impls "carry the built-in default"),
    not layered around it by ``GitRunner``. A process_runner double with no
    timeout-awareness of its own would simply hang forever here. This
    double therefore models what a real bounded runner (the production
    default, or S4's future configured runner) does against a command that
    blocks past its ceiling: block, then raise ``GitCommandTimeoutError``.
    """

    def __init__(self, *, sleep_seconds: float, timeout: float) -> None:
        self._sleep_seconds = sleep_seconds
        self._timeout = timeout
        self.calls = 0

    def __call__(self, argv, *, cwd, stdin=None):
        self.calls += 1
        time.sleep(self._sleep_seconds)
        raise GitCommandTimeoutError(argv=argv, timeout=self._timeout)


def test_run_raises_git_command_timeout_error_when_process_runner_blocks_past_limit(
    tmp_path: Path,
) -> None:
    """AC-ERR-2: GitRunner.run, backed by a sleeping runner that blocks past
    the configured limit, raises an exception that is a GitCommandError.

    Confirms ``GitRunner.run`` propagates whatever the injected runner
    raises rather than catching and reinterpreting it -- the exit-code-6
    half of AC-ERR-2 is covered separately in tests/test_exit_codes.py by
    driving ``main(["run"])`` end to end.
    """
    runner_double = _SleepingProcessRunner(sleep_seconds=0.05, timeout=0.05)
    runner = GitRunner(cwd=tmp_path, process_runner=runner_double)

    with pytest.raises(GitCommandError) as excinfo:
        runner.run(["status"])

    assert isinstance(excinfo.value, GitCommandTimeoutError)
    assert runner_double.calls == 1


def test_run_bytes_raises_git_command_timeout_error_when_process_runner_blocks_past_limit(
    tmp_path: Path,
) -> None:
    """AC-ERR-4: run_bytes raises the same exception type as AC-ERR-2 under
    the same condition -- a distinct test targeting run_bytes specifically,
    not an assertion "by symmetry" with the text-path test above.
    """
    runner_double = _SleepingProcessRunner(sleep_seconds=0.05, timeout=0.05)
    runner = GitRunner(cwd=tmp_path, process_runner_bytes=runner_double)

    with pytest.raises(GitCommandError) as excinfo:
        runner.run_bytes(["cat-file", "-p", "HEAD"])

    assert isinstance(excinfo.value, GitCommandTimeoutError)
    assert runner_double.calls == 1


def test_bounded_git_subprocess_raises_timeout_error_on_its_own_deadline(
    tmp_path: Path,
) -> None:
    """Regression: expiry must raise GitCommandTimeoutError, not return a
    result that describes a killed process as an ordinary exit (mirrors
    tests/test_timestamp.py:test_default_runner_raises_submission_timeout_error_on_its_own_deadline,
    S2's worst defect in a narrower form). Also asserts the *calling*
    (pytest) process's own process group is untouched by the kill --
    start_new_session=True and killpg must target only the child's own
    group, never the harness's (VQ-S3-006); if this were wrong the whole
    test run would not survive to report a result.
    """
    own_pgrp_before = os.getpgrp()
    argv = [sys.executable, "-c", "import time; time.sleep(600)"]

    with pytest.raises(GitCommandTimeoutError) as excinfo:
        _run_bounded_git_subprocess(
            argv, cwd=tmp_path, stdin=None, timeout=0.1, text=True
        )

    assert sys.executable in str(excinfo.value)
    assert os.getpgrp() == own_pgrp_before


def test_bounded_git_subprocess_timeout_survives_a_failing_reap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: an OSError from the post-kill reap must not displace the
    timeout it was reporting (mirrors
    tests/test_timestamp.py:test_default_runner_timeout_survives_a_failing_reap).
    Simulates the reap itself failing by making ``Popen.wait()`` raise
    unconditionally; ``GitCommandTimeoutError`` must still be what
    propagates.
    """

    def _failing_wait(self, *args, **kwargs):
        raise OSError("simulated reap failure")

    monkeypatch.setattr("git_ots.git.subprocess.Popen.wait", _failing_wait)
    argv = [sys.executable, "-c", "import time; time.sleep(600)"]

    with pytest.raises(GitCommandTimeoutError):
        _run_bounded_git_subprocess(
            argv, cwd=tmp_path, stdin=None, timeout=0.1, text=True
        )


def test_kill_process_group_kills_survivors_after_the_leader_is_reaped(
    tmp_path: Path,
) -> None:
    """Regression: killing by a remembered pgid must work even once the
    group leader is already gone -- mirrors
    tests/test_timestamp.py:test_kill_process_group_kills_survivors_after_the_leader_is_reaped.
    Once the leader is reaped, ``os.getpgid(leader_pid)`` has no process
    left to resolve and raises ``ProcessLookupError``, even though the
    process *group* itself (identified by the now-unresolvable pgid) can
    still have live members -- exactly why ``_kill_process_group`` kills by
    the remembered pgid rather than re-deriving it at kill time.
    """
    marker = tmp_path / "grandchild_pid"
    leader_script = tmp_path / "leader"
    leader_script.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, subprocess, sys\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "grandchild = subprocess.Popen(\n"
        f"    [{sys.executable!r}, '-c', 'import time; time.sleep(600)']\n"
        ")\n"
        "marker.write_text(str(grandchild.pid))\n"
        # Exit immediately -- the grandchild stays alive in the same
        # process group, since it never called its own start_new_session.
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    leader_script.chmod(0o755)

    process = subprocess.Popen(
        [str(leader_script)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Remembered at spawn, exactly as _run_bounded_git_subprocess does --
    # pgid == pid because of start_new_session=True.
    pgid = process.pid
    process.wait(timeout=5.0)  # the leader exits fast and is fully reaped here

    deadline = time.monotonic() + 5.0
    while not marker.exists():
        if time.monotonic() > deadline:
            raise AssertionError("grandchild marker never appeared")
        time.sleep(0.05)
    grandchild_pid = int(marker.read_text())

    def _grandchild_alive() -> bool:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            return False
        return True

    assert _grandchild_alive(), "test setup: grandchild should still be running"

    _kill_process_group(pgid)

    deadline = time.monotonic() + 5.0
    while _grandchild_alive():
        if time.monotonic() > deadline:
            raise AssertionError(
                "grandchild survived the group kill -- killing by the "
                "remembered pgid must not depend on the leader still "
                "being resolvable via os.getpgid()"
            )
        time.sleep(0.05)
