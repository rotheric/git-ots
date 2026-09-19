"""Safe Git command runner boundary.

The :class:`GitRunner` guarantees that Git is always invoked as an
argument array (never a shell string), in a fixed working directory,
with captured text output, and with ``shell=False``. See specs/spec.md
sections 33 and 41.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Self

from .timestamp import (
    PayloadValidationError,
    ProofEvidence,
    RecoveryValidationError,
    _validate_recovery_manifest,
    build_payload,
    validate_detached_proof,
)

_logger = logging.getLogger("git_ots")

# ``RepositoryLock`` may be acquired reentrantly within a single process (for
# example, the CLI holds the lock around inspection and the orchestration
# entry point re-acquires it before mutations). The module-level state tracks
# per-process ownership and a shared file descriptor so the OS lock is held
# until the outermost release.
_LOCK_STATE: dict[Path, tuple[int, int]] = {}
_LOCK_STATE_GUARD = threading.Lock()


class GitCommandError(RuntimeError):
    """Raised when a Git invocation exits non-zero.

    Carries the exit code, the full argument vector, and captured stderr
    so callers can surface actionable diagnostics.
    """

    def __init__(self, *, argv: list[str], exit_code: int, stderr: str) -> None:
        self.argv = list(argv)
        self.exit_code = exit_code
        self.stderr = stderr
        detail = stderr.strip()
        message = f"git exited with code {exit_code}: {' '.join(self.argv)}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


# AC-ERR-6: the git subcommands that take the index lock before running
# hooks, and therefore can leave a stale `.git/index.lock` (and, past the
# hook-invocation point, a `next-index-<pid>.lock`) behind when killed. `git
# tag`/`rev-parse`/`cat-file`/etc. never take this lock, so a timeout on those
# must not carry a diagnostic that names a hazard they cannot cause.
_INDEX_LOCK_SUBCOMMANDS = frozenset({"add", "commit"})


class GitCommandTimeoutError(GitCommandError):
    """Raised when a Git invocation does not finish within its ceiling.

    This cannot be a trivial subclass that stops at inheriting
    ``GitCommandError.__init__``: that method hardcodes ``"git exited with
    code {exit_code}: {argv}"``, which is false for a child that was
    killed rather than exited (mirrors ``timestamp.py``'s
    ``SubmissionTimeoutError`` next to ``SubmissionError`` for the same
    reason). ``__init__`` is overridden to synthesize its own message
    naming the command and the configured ceiling, while still populating
    ``argv``/``exit_code``/``stderr`` so existing ``except GitCommandError``
    callers keep working unchanged -- notably
    ``cli.py:_locate_repository``, which reads ``exc.stderr.strip()``
    expecting a ``str`` (not ``bytes``, unlike ``timestamp.py``'s
    ``SubmissionError.stderr``). The whole process group is killed before
    this is raised -- see :func:`_kill_process_group`.

    AC-ERR-6: when ``argv`` names one of :data:`_INDEX_LOCK_SUBCOMMANDS`
    (``add``/``commit``), the message additionally diagnoses a real hazard
    -- git creates ``.git/index.lock`` (and, once past the pre-commit/
    commit-msg hook invocation point, a ``next-index-<pid>.lock``) before
    running hooks and only removes it once the whole command finishes;
    ``SIGKILL`` gives git no chance to do that cleanup, so the lock can be
    left behind and then blocks *every later* ``git add``/``git commit`` in
    this repository -- an unattended schedule's every subsequent run, not
    just this one. The tool deliberately never deletes files under ``.git``
    itself (ruling: this project's advisory lock does not exclude a
    concurrently running operator ``git`` command, which could legitimately
    hold the same lock, so an automatic removal here could corrupt that
    other command's in-flight work) -- so the message names the remedy for
    an operator to apply by hand instead of silently doing it for them. A
    ``rev-parse``/``tag``/etc. timeout never takes this lock and must not
    carry this diagnostic.
    """

    def __init__(self, *, argv: list[str], timeout: float) -> None:
        self.argv = list(argv)
        self.timeout = timeout
        # There is no real exit code -- the child was killed -- but a
        # killed process's conventional Python ``returncode`` is the
        # negated signal number, so this stays a meaningful (and
        # still-``int``) value for any caller reading the base class's
        # attribute. Mirrors timestamp.py:SubmissionTimeoutError.
        self.exit_code = -signal.SIGKILL
        self.stderr = ""
        message = (
            f"{' '.join(self.argv)!r} did not finish within the configured "
            f"limit of {timeout:g}s and was killed"
        )
        subcommand = argv[1] if len(argv) > 1 else None
        if subcommand in _INDEX_LOCK_SUBCOMMANDS:
            message += (
                "; this may have left a stale .git/index.lock behind, which "
                "will block every later `git add`/`git commit` in this "
                "repository -- once you have confirmed no other git process "
                "is running here, remove .git/index.lock (and any "
                "next-index-*.lock next to it) by hand to recover"
            )
        RuntimeError.__init__(self, message)


@dataclass(frozen=True, slots=True)
class GitSuccess:
    """Successful Git invocation result with captured text output."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class GitSuccessBytes:
    """Successful Git invocation result with captured binary output."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: bytes
    stderr: bytes


# A process runner receives the full argv (starting with "git"), the fixed
# working directory, and optional stdin bytes. It returns (exit_code,
# stdout, stderr) as text. There is deliberately no ``shell`` parameter.
ProcessRunner = Callable[..., tuple[int, str, str]]

# Bytes variant for reading binary objects (for example detached OpenTimestamps
# proofs stored in a commit tree).
ProcessRunnerBytes = Callable[..., tuple[int, bytes, bytes]]


# Wall-clock ceiling the production runners enforce when no configuration has
# been loaded yet. This bounds the two pre-config ``git rev-parse`` calls
# ``cli.py:_locate_repository`` issues before any ``[limits]`` table can
# possibly exist (AC-PROC-4; architecture.md's "Cross-story load-bearing
# constraints" and Design Decision 10).
#
# Duplicated from ``LimitsConfig.git_timeout``'s default in config.py
# (``timedelta(seconds=60)``) rather than imported: architecture.md's
# leaf-to-root dependency order places ``git`` *before* ``config``, so
# ``config`` may import from ``git`` but not the reverse -- a ``git.py``
# import of ``config.py`` would be exactly the kind of new cross-module edge
# Boundary Rule 1 forbids, for the sake of a single float. See
# architecture.json for the full reasoning. This literal and config.py's
# default must be changed together if the built-in default value ever
# changes.
_DEFAULT_GIT_TIMEOUT_SECONDS: float = 60.0


def _kill_process_group(pgid: int) -> None:
    """Terminate the whole process group identified by ``pgid``.

    Duplicated from ``timestamp.py``'s helper of the same name rather than
    imported or shared through a new module: Boundary Rule 1 confines
    ``subprocess``/process-lifetime code to the two adapter modules and
    forbids new cross-module imports between them beyond the existing
    ``git`` -> ``timestamp`` edge (payload validation) that already flows in
    the leaf-to-root direction. Adding the reverse edge just to share four
    lines of cleanup would be worse than the duplication. See
    architecture.json for the full reasoning.

    ``pgid`` must be the pgid of a process that was started with
    ``start_new_session=True``, which makes it the leader of its own new
    process group -- so its pgid equals its pid at the moment it is spawned.
    Callers pass that *remembered* value directly; this function does not
    call ``os.getpgid(pid)`` to re-derive it at kill time, because that
    lookup depends on the leader still being alive: once it has been reaped,
    ``os.getpgid(leader_pid)`` raises ``ProcessLookupError`` even though the
    group itself can still have live members (any children the reaped
    leader spawned before exiting). Killing by the remembered pgid instead
    of re-deriving it sidesteps that lookup entirely. Silently ignored if
    the whole group is already gone. A repeated best-effort kill can also
    report ``EPERM`` on macOS after a concurrent cleanup path has already
    killed the child; that race is treated as completed cleanup so it cannot
    replace the command's real timeout exception.
    """
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _run_bounded_git_subprocess(
    argv: list[str],
    *,
    cwd: Path,
    stdin: bytes | str | None,
    timeout: float | None,
    text: bool,
) -> tuple[int, str, str] | tuple[int, bytes, bytes]:
    """Spawn ``argv`` and enforce ``timeout`` as a wall-clock ceiling.

    Shared by every runner :func:`make_process_runner` and
    :func:`make_process_runner_bytes` build (including the built-in
    defaults, :data:`_default_process_runner` and
    :data:`_default_process_runner_bytes`), which differ only in whether
    output is captured as text or bytes. The child is started in its own
    session (``start_new_session=True``) so that, on expiry, its whole
    process group can be killed rather than only the direct child -- see
    :func:`_kill_process_group`. On expiry this function kills the group,
    reaps the child, and *raises* :class:`GitCommandTimeoutError` -- it does
    not return a result describing a killed process as an ordinary exit,
    which would let ``GitRunner.run``/``run_bytes`` see a non-zero
    ``exit_code`` and raise a plain ``GitCommandError`` reporting "git
    exited with code -9", the false claim ``GitCommandTimeoutError`` exists
    to avoid (mirrors ``timestamp.py:_default_runner``'s docstring).

    ``timeout=None`` is the unbounded case (an operator's ``git_timeout =
    "0"``): ``Popen.communicate(timeout=None)`` never raises
    ``TimeoutExpired``, so this function simply never gives up. This
    function is not itself a public seam -- callers outside this module
    build runners through :func:`make_process_runner` /
    :func:`make_process_runner_bytes`, which additionally handle the
    ``ProcessRunner``/``ProcessRunnerBytes`` stdin contract (see those
    functions' docstrings for why that step cannot be skipped).

    The outer ``except BaseException: _kill_process_group(pgid); raise``
    below is the *only* mechanism that kills a git child's process group on
    interrupt -- not a backstop, the sole one. Unlike ``timestamp.py``'s
    ``OpenTimestampsCli``, ``GitRunner`` has no background-thread wrapper
    and no child-pgid registry a caller could consult after the fact
    (Design Decision 9: the ceiling lives entirely in the runner callable,
    not in a separate adapter object with its own bookkeeping), so *every*
    call to this function -- bounded or unbounded -- runs inline on the
    caller's own thread. A ``KeyboardInterrupt`` therefore always unwinds
    directly through ``process.communicate()`` right here, in the one frame
    that has ``pgid`` in scope; there is nowhere else in the call chain that
    could clean this up. Without this guard, ``start_new_session=True``
    (needed for the timeout path's own group-kill, above) has already moved
    the child out of the terminal's foreground process group, so Ctrl-C no
    longer reaches it at all -- making interrupt behaviour *worse* than the
    pre-epic unbounded ``subprocess.run``, which at least sat in the
    foreground group and died with the parent. Do not delete this as
    apparently redundant with the timeout path's kill above: that one only
    fires on ``TimeoutExpired``; this one is the only kill on the interrupt
    path. Mirrors ``timestamp.py:_default_runner``'s identical guard.
    """
    popen_kwargs: dict[str, object] = {
        "cwd": cwd,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
        "start_new_session": True,
    }
    if text:
        popen_kwargs.update(text=True, encoding="utf-8", errors="surrogateescape")
    process = subprocess.Popen(argv, **popen_kwargs)
    # start_new_session=True makes this process the leader of its own new
    # process group, so its pgid equals its pid at this exact moment --
    # remember it now rather than re-deriving it via os.getpgid(pid) later,
    # once the leader may already have been reaped. See _kill_process_group.
    pgid = process.pid
    try:
        try:
            stdout, stderr = process.communicate(input=stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(pgid)
            # `timeout` is never None here: communicate() only raises
            # TimeoutExpired when it was given a numeric `timeout`, which
            # only happens when `timeout is not None` (mirrors
            # timestamp.py:_default_runner's identical comment for `limit`).
            timeout_error = GitCommandTimeoutError(argv=argv, timeout=timeout)  # type: ignore[arg-type]
            try:
                # Reap with wait(), not another communicate(): the child is
                # already dead (or dying) from the kill above, and nothing
                # downstream reads its output on this path. wait() only
                # waits for the direct child's own exit status, so it
                # cannot be blocked by a grandchild that escaped the
                # process group and still holds the pipe fds open --
                # unlike communicate(), which would then block forever
                # waiting for EOF that never comes. Any OSError here is
                # swallowed rather than allowed to displace the
                # already-built timeout error below (mirrors
                # timestamp.py:_default_runner).
                process.wait()
            except OSError:
                pass
            raise timeout_error from None
        return process.returncode, stdout, stderr
    except BaseException:
        # The only kill on the interrupt path -- see this function's
        # docstring for why nothing else in the call chain can do this.
        _kill_process_group(pgid)
        raise


def make_process_runner(*, timeout: float | None) -> ProcessRunner:
    """Build a text-mode :data:`ProcessRunner` bounded by ``timeout``.

    This is the public seam for constructing a runner that carries a
    *configured* ceiling -- e.g. S4's ``cli.py`` wiring, once ``Config`` is
    available, builds ``make_process_runner(timeout=config.limits
    .git_timeout.total_seconds() if config.limits.git_timeout else None)``
    and passes the result into ``GitRunner(process_runner=...)``.
    ``timeout=None`` is the operator's unbounded escape hatch
    (``git_timeout = "0"``); see :func:`_run_bounded_git_subprocess`.

    :data:`_default_process_runner` (the built-in default -- AC-PROC-4) is
    exactly ``make_process_runner(timeout=_DEFAULT_GIT_TIMEOUT_SECONDS)``:
    the built-in default is not a separate code path, it is this factory
    called with one particular value.

    Building a runner with ``functools.partial(_run_bounded_git_subprocess,
    ...)`` instead of through this factory is a trap, not just a stylistic
    difference: that helper's ``stdin`` parameter expects already-decoded
    text in text mode, but :data:`ProcessRunner`'s contract -- the one
    ``GitRunner.run`` actually calls through -- always passes ``bytes`` (or
    ``None``); :func:`has_generated_trailer` and
    :func:`_read_message_and_trailers`, both ``git interpret-trailers
    --parse``, pass real ``body.encode("utf-8")`` payloads. A bare
    ``partial`` skips the decode step this factory performs and fails on
    any non-empty stdin (``AttributeError: 'bytes' object has no attribute
    'encode'`` from ``communicate()``'s own text-mode encoding, since
    ``Popen(text=True)`` expects a ``str`` and instead receives the raw
    ``bytes`` -- reachable in production through the generated-commit
    detection path, load-bearing for recovery and idempotency, spec.md
    sections 20 and 22).
    """

    def _runner(
        argv: list[str], *, cwd: Path, stdin: bytes | None = None
    ) -> tuple[int, str, str]:
        # The encoding is fixed explicitly so the tool behaves consistently
        # regardless of the ambient locale (``LC_ALL=C`` would otherwise
        # select ASCII and corrupt or fail on non-ASCII bytes).
        # ``surrogateescape`` preserves arbitrary byte sequences from Git in
        # paths and author names, matching Git's own tolerance; callers
        # that require strict UTF-8 (such as manifest reads) must validate
        # the returned text themselves. This decode is the ``ProcessRunner``
        # contract's ``bytes`` stdin meeting ``_run_bounded_git_subprocess``'s
        # text-mode ``Popen``, which needs a ``str`` -- see this function's
        # docstring for why skipping it (e.g. via a bare
        # ``functools.partial``) breaks on real stdin.
        exit_code, stdout, stderr = _run_bounded_git_subprocess(
            argv,
            cwd=cwd,
            stdin=stdin.decode("utf-8") if stdin is not None else None,
            timeout=timeout,
            text=True,
        )
        return exit_code, stdout, stderr

    return _runner


def make_process_runner_bytes(*, timeout: float | None) -> ProcessRunnerBytes:
    """Build a bytes-mode :data:`ProcessRunnerBytes` bounded by ``timeout``.

    See :func:`make_process_runner`'s docstring for the seam this serves
    and why a bare ``functools.partial(_run_bounded_git_subprocess, ...)``
    is not an equivalent substitute: this variant needs no stdin decode
    (the ``ProcessRunnerBytes`` contract and ``_run_bounded_git_subprocess``
    both already deal in raw ``bytes``), but is still built through a
    factory rather than a partial so the two runner kinds stay
    symmetric and so a future change to either contract has exactly one
    place to change.

    :data:`_default_process_runner_bytes` is exactly
    ``make_process_runner_bytes(timeout=_DEFAULT_GIT_TIMEOUT_SECONDS)``.
    """

    def _runner(
        argv: list[str], *, cwd: Path, stdin: bytes | None = None
    ) -> tuple[int, bytes, bytes]:
        exit_code, stdout, stderr = _run_bounded_git_subprocess(
            argv,
            cwd=cwd,
            stdin=stdin,
            timeout=timeout,
            text=False,
        )
        return exit_code, stdout, stderr

    return _runner


# The built-in default ceiling (AC-PROC-4): the runner
# ``cli.py:_locate_repository``'s pre-config ``git rev-parse`` calls reach
# through ``GitRunner`` before any ``Config`` exists. Each is exactly its
# factory called with one particular value -- not a separate
# implementation -- so a configured runner built the same way (S4) is
# guaranteed to behave identically apart from the ceiling itself.
_default_process_runner: ProcessRunner = make_process_runner(
    timeout=_DEFAULT_GIT_TIMEOUT_SECONDS
)
_default_process_runner_bytes: ProcessRunnerBytes = make_process_runner_bytes(
    timeout=_DEFAULT_GIT_TIMEOUT_SECONDS
)


class GitRunner:
    """Runs Git with an argument array in a fixed working directory.

    The runner is injectable so tests can supply a fake; the production
    defaults (:data:`_default_process_runner` /
    :data:`_default_process_runner_bytes`) use ``subprocess.Popen`` with
    ``shell=False``, ``start_new_session=True``, and a wall-clock ceiling
    (the built-in default, or a configured one built via
    :func:`make_process_runner` / :func:`make_process_runner_bytes` --
    ``GitRunner`` itself takes no ceiling parameter; the runner callable
    carries it). A command that runs past its ceiling raises
    :class:`GitCommandTimeoutError` and has its whole process group killed
    rather than only the direct child. The command is always prefixed with
    ``git``; callers cannot invoke arbitrary programs or use shell strings.
    """

    def __init__(
        self,
        *,
        cwd: Path,
        process_runner: ProcessRunner | None = None,
        process_runner_bytes: ProcessRunnerBytes | None = None,
    ) -> None:
        self._cwd = Path(cwd)
        self._process_runner = (
            process_runner if process_runner is not None else _default_process_runner
        )
        self._process_runner_bytes = (
            process_runner_bytes
            if process_runner_bytes is not None
            else _default_process_runner_bytes
        )

    @property
    def cwd(self) -> Path:
        return self._cwd

    def run(
        self,
        args: list[str] | tuple[str, ...],
        *,
        stdin: bytes | None = None,
    ) -> GitSuccess:
        if isinstance(args, (str, bytes)):
            raise TypeError("git arguments must be an array, not a shell string")
        argv = ["git", *args]
        exit_code, stdout, stderr = self._process_runner(
            argv, cwd=self._cwd, stdin=stdin
        )
        if exit_code != 0:
            raise GitCommandError(argv=argv, exit_code=exit_code, stderr=stderr)
        return GitSuccess(
            argv=tuple(argv), exit_code=exit_code, stdout=stdout, stderr=stderr
        )

    def run_bytes(
        self,
        args: list[str] | tuple[str, ...],
        *,
        stdin: bytes | None = None,
    ) -> GitSuccessBytes:
        """Run Git and return captured binary output.

        Used for reading binary objects from a commit tree (for example
        ``.ots`` proof files) without corrupting them with text decoding.
        """
        if isinstance(args, (str, bytes)):
            raise TypeError("git arguments must be an array, not a shell string")
        argv = ["git", *args]
        exit_code, stdout, stderr = self._process_runner_bytes(
            argv, cwd=self._cwd, stdin=stdin
        )
        if exit_code != 0:
            raise GitCommandError(
                argv=argv,
                exit_code=exit_code,
                stderr=stderr.decode("utf-8", errors="replace"),
            )
        return GitSuccessBytes(
            argv=tuple(argv), exit_code=exit_code, stdout=stdout, stderr=stderr
        )


class InvalidRepositoryStateError(RuntimeError):
    """Raised when the repository is in an invalid state for the operation.

    Examples: a configured source ref cannot be resolved, the repository
    is empty when a commit is required, or refs are otherwise inconsistent.
    Maps to exit code 3 in spec section 29.
    """


class RepositoryLockedError(RuntimeError):
    """Raised when the repository-level advisory lock is already held.

    Only one ``git-ots`` process may modify a repository at a time (spec
    section 24). Maps to exit code 7 in spec section 29.
    """


class RepositoryLock:
    """An OS-released advisory lock stored in the common Git directory.

    Uses ``fcntl.flock`` so the lock is automatically released when the
    holding process exits. The lock file lives in the common Git directory
    rather than the worktree so linked worktrees share the same lock.
    """

    def __init__(
        self, *, common_git_dir: Path, lock_filename: str = "git-ots.lock"
    ) -> None:
        self._path = Path(common_git_dir) / lock_filename
        self._fd: int | None = None

    def acquire(self) -> None:
        """Acquire the lock, raising ``RepositoryLockedError`` if busy."""

        if self._fd is not None:
            return
        with _LOCK_STATE_GUARD:
            state = _LOCK_STATE.get(self._path)
            if state is not None:
                count, fd = state
                self._fd = fd
                _LOCK_STATE[self._path] = (count + 1, fd)
                return

        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            os.close(fd)
            raise RepositoryLockedError(
                f"repository lock is already held: {self._path}"
            ) from exc
        with _LOCK_STATE_GUARD:
            _LOCK_STATE[self._path] = (1, fd)
            self._fd = fd

    def release(self) -> None:
        """Release the lock if currently held by this instance."""

        fd = self._fd
        if fd is None:
            return
        self._fd = None
        with _LOCK_STATE_GUARD:
            state = _LOCK_STATE.get(self._path)
            if state is None:
                return
            count, shared_fd = state
            if shared_fd != fd:
                return
            if count > 1:
                _LOCK_STATE[self._path] = (count - 1, fd)
                return
            del _LOCK_STATE[self._path]
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.release()


@dataclass(frozen=True, slots=True)
class ResolvedRef:
    """A one-time, frozen resolution of a source ref.

    ``commit_id`` is the full object ID (40 hex chars for SHA-1, 64 for
    SHA-256). ``display_ref`` is the concrete, fully-qualified ref the
    input resolved to (for example ``refs/heads/main``), not a symbolic
    name like ``HEAD``. See spec sections 15 and 21.
    """

    commit_id: str
    display_ref: str


@dataclass(frozen=True, slots=True)
class RepositoryLayout:
    """Resolved repository locations for a working directory.

    ``worktree_root`` is the top of the working tree. ``common_git_dir``
    is the repository's shared ``.git`` directory. For a linked worktree
    the two differ; for a normal repository the common Git dir is the
    ``.git`` directory directly under the worktree root. Both are
    absolute paths.

    ``is_bare`` records whether ``locate_repository`` had to fall back to
    ``--absolute-git-dir`` because the repository has no working tree at
    all -- see its docstring. It is set once, at the point where bareness
    is actually known, so downstream callers never have to infer it by
    comparing ``worktree_root`` to ``common_git_dir`` (which linked
    worktrees make an unreliable signal anyway).
    """

    worktree_root: Path
    common_git_dir: Path
    is_bare: bool = False


def locate_repository(
    *,
    cwd: Path,
    process_runner: ProcessRunner | None = None,
) -> RepositoryLayout:
    """Locate the repository containing ``cwd`` without mutating it.

    Runs ``git rev-parse --show-toplevel`` and ``--git-common-dir`` and
    returns absolute paths. ``--git-common-dir`` may print a path relative
    to ``cwd`` (for example ``.git`` inside a regular repository); it is
    resolved against ``cwd`` so the result is always absolute and correct
    for both regular repositories and linked worktrees.

    A bare repository (``git init --bare``/``git clone --bare``) has no
    worktree at all, so ``--show-toplevel`` fails there with "this
    operation must be run in a work tree". That failure is caught and
    ``--absolute-git-dir`` -- the repository's own directory -- is used as
    ``worktree_root`` instead, so callers that need only ``cwd``-scoped Git
    plumbing (reading `ots.*` config, resolving refs) keep working, while
    anything that scans ``worktree_root`` on the filesystem correctly finds
    no checked-out files (see FS-0015 behaviour 4 / AC-BARE-1: `git-ots
    verify` succeeds against a bare clone precisely because there is
    nothing on disk to find, not because bare repositories are given a
    real worktree). If ``--absolute-git-dir`` *also* fails, this is not a
    repository at all and the original ``--show-toplevel`` error -- the
    more informative of the two -- is what propagates.
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    base = Path(cwd).resolve()

    is_bare = False
    try:
        top = runner.run(["rev-parse", "--show-toplevel"]).stdout.strip()
    except GitCommandError as exc:
        try:
            top = runner.run(["rev-parse", "--absolute-git-dir"]).stdout.strip()
        except GitCommandError:
            raise exc from None
        is_bare = True
    if not top:
        raise GitCommandError(
            argv=["git", "rev-parse", "--show-toplevel"],
            exit_code=0,
            stderr="empty worktree root",
        )
    worktree_root = Path(top).resolve()

    common_raw = runner.run(["rev-parse", "--git-common-dir"]).stdout.strip()
    if not common_raw:
        raise GitCommandError(
            argv=["git", "rev-parse", "--git-common-dir"],
            exit_code=0,
            stderr="empty common git dir",
        )
    common_path = Path(common_raw)
    if not common_path.is_absolute():
        common_path = base / common_path
    common_git_dir = common_path.resolve()

    return RepositoryLayout(
        worktree_root=worktree_root,
        common_git_dir=common_git_dir,
        is_bare=is_bare,
    )


def assert_worktree_present(layout: RepositoryLayout, *, operation: str) -> None:
    """Raise if ``layout`` has no working tree.

    ``locate_repository`` deliberately keeps working against a bare
    repository so read-only commands (``validate`` and ``verify``) can still inspect it --
    AC-BARE-1. Anything that *writes* must not: with no working tree,
    ``worktree_root`` is the repository's own Git directory, so proof
    files, the advisory lock file, generated commits, and tags would all
    land inside ``.git`` instead of being refused. Call this before any
    filesystem mutation on a write path -- including before the advisory
    lock file is created -- never on a read-only path.
    """
    if layout.is_bare:
        raise InvalidRepositoryStateError(
            f"git-ots {operation} requires a working tree; this repository is bare"
        )


class ObjectFormat(Enum):
    """Git object hash format.

    Mirrors the values Git reports via ``git rev-parse --show-object-format``.
    The string value is the canonical lowercase name used in the
    ``git:<object-format>:<full-commit-id>`` payload (spec section 9).
    """

    SHA1 = "sha1"
    SHA256 = "sha256"


def detect_object_format(
    *,
    cwd: Path,
    process_runner: ProcessRunner | None = None,
) -> ObjectFormat:
    """Return the repository's object hash format.

    Runs ``git rev-parse --show-object-format`` in ``cwd``. Raises
    :class:`InvalidRepositoryStateError` when Git reports an unrecognized
    value.
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    raw = runner.run(["rev-parse", "--show-object-format"]).stdout.strip()
    try:
        return ObjectFormat(raw)
    except ValueError as exc:
        raise InvalidRepositoryStateError(
            f"unrecognized git object format {raw!r}"
        ) from exc


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of a non-integrating ``git fetch``.

    The fetch updates remote-tracking refs only; it never merges, rebases,
    checks out, or otherwise modifies the worktree (spec section 15).
    """

    remote: str


def fetch_remote(
    *,
    cwd: Path,
    remote: str,
    process_runner: ProcessRunner | None = None,
) -> FetchResult:
    """Fetch ``remote`` without integrating changes.

    Runs ``git fetch --prune <remote>`` in ``cwd``. This updates
    remote-tracking refs and prunes those that no longer exist upstream
    but leaves ``HEAD``, the index, and the worktree untouched. Raises
    :class:`GitCommandError` when the fetch fails.
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    runner.run(["fetch", "--prune", remote])
    return FetchResult(remote=remote)


def push_current_branch(
    *,
    cwd: Path,
    process_runner: ProcessRunner | None = None,
) -> None:
    """Push the current branch and its reachable annotated timestamp tags.

    Git's configured upstream and ``push.default`` determine the destination,
    exactly as for an ordinary ``git push``. ``--follow-tags`` additionally
    publishes the annotated timestamp tag created by a successful run without
    sweeping unrelated, unreachable tags into the remote.
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    runner.run(["push", "--follow-tags"])


def discover_remote_for_ref(
    *,
    cwd: Path,
    ref: str,
    process_runner: ProcessRunner | None = None,
) -> str | None:
    """Return the remote name owning ``ref``, or ``None`` when it has no remote.

    Resolves the symbolic full name of ``ref`` (for example
    ``refs/remotes/origin/main`` for ``@{upstream}``) and extracts the
    remote from ``refs/remotes/<remote>/<branch>``. Local branches, tags,
    and arbitrary commit IDs have no remote and return ``None``.
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    try:
        full = runner.run(["rev-parse", "--symbolic-full-name", ref]).stdout.strip()
    except GitCommandError:
        return None

    prefix = "refs/remotes/"
    if full.startswith(prefix):
        remainder = full[len(prefix) :]
        remote = remainder.split("/", 1)[0]
        if remote:
            return remote
    return None


def maybe_fetch_before_run(
    *,
    cwd: Path,
    source_ref: str,
    fetch_before_run: bool,
    process_runner: ProcessRunner | None = None,
) -> FetchResult | None:
    """Fetch the owning remote for ``source_ref`` when configured to do so.

    Returns ``None`` when fetching is disabled or the configured source ref
    has no remote. This keeps the ``status`` command read-only; only the
    ``run`` path invokes this function.
    """

    if not fetch_before_run:
        return None
    remote = discover_remote_for_ref(
        cwd=cwd, ref=source_ref, process_runner=process_runner
    )
    if remote is None:
        return None
    return fetch_remote(cwd=cwd, remote=remote, process_runner=process_runner)


def resolve_source_ref(
    *,
    cwd: Path,
    ref: str,
    process_runner: ProcessRunner | None = None,
) -> ResolvedRef:
    """Resolve ``ref`` once to a frozen commit ID and concrete display ref.

    Runs a single ``git rev-parse`` invocation that both verifies the ref
    and dereferences it to a commit, then discovers the concrete
    fully-qualified ref for display. Raises
    :class:`InvalidRepositoryStateError` when the ref cannot be resolved.
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)

    try:
        commit_id = runner.run(
            ["rev-parse", "--verify", f"{ref}^{{commit}}"]
        ).stdout.strip()
    except GitCommandError as exc:
        raise InvalidRepositoryStateError(
            f"cannot resolve source ref {ref!r}: {exc.stderr.strip() or exc}"
        ) from exc
    if not commit_id:
        raise InvalidRepositoryStateError(
            f"cannot resolve source ref {ref!r}: empty commit id"
        )

    # Resolve the concrete, fully-qualified display ref. For symbolic refs
    # like HEAD this yields the underlying branch ref; for upstream or
    # remote-tracking expressions like "@{upstream}", symbolic-ref does not
    # apply, so fall back to ``rev-parse --symbolic-full-name`` which maps
    # them to their concrete ref (for example ``refs/remotes/origin/main``).
    # Only as a last resort do we echo the original input.
    display_ref = ""
    try:
        display_ref = runner.run(["symbolic-ref", "-q", ref]).stdout.strip()
    except GitCommandError:
        display_ref = ""
    if not display_ref:
        try:
            candidate = runner.run(
                ["rev-parse", "--symbolic-full-name", ref]
            ).stdout.strip()
        except GitCommandError:
            candidate = ""
        # Accept the candidate only if it is a fully-qualified ref; otherwise
        # fall back to the original input.
        if candidate.startswith("refs/"):
            display_ref = candidate
    if not display_ref:
        display_ref = ref

    return ResolvedRef(commit_id=commit_id, display_ref=display_ref)


@dataclass(frozen=True, slots=True)
class CommitInfo:
    """A single commit reachable from a source ref.

    ``commit_id`` is the full object ID. ``committer_time`` is the
    timezone-aware committer timestamp (never the author timestamp);
    spec section 6.2 makes the committer timestamp the basis for
    maximum-age evaluation.
    """

    commit_id: str
    committer_time: datetime


GENERATED_TRAILER_KEY = "OpenTimestamps-Generated"
GENERATED_TRAILER_VALUE = "true"
SOURCE_TRAILER_KEY = "OpenTimestamps-Source"
UPGRADED_TRAILER_KEY = "OpenTimestamps-Upgraded"


def has_generated_trailer(
    *,
    cwd: Path,
    commit: str,
    process_runner: ProcessRunner | None = None,
) -> bool:
    """Return True when ``commit`` carries the canonical generated trailer.

    Uses ``git interpret-trailers --parse`` so only actual trailer lines are
    considered; prose in the subject or body that merely looks like a
    trailer is ignored. The trailer must match the canonical key
    ``OpenTimestamps-Generated`` and the exact lowercase value ``true``
    (spec section 13).
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)

    try:
        runner.run(["rev-parse", "--verify", f"{commit}^{{commit}}"])
    except GitCommandError as exc:
        raise InvalidRepositoryStateError(
            f"cannot read trailers for unknown commit {commit!r}: "
            f"{exc.stderr.strip() or exc}"
        ) from exc

    body = runner.run(["log", "-1", "--format=%B", commit]).stdout
    parsed = runner.run(
        ["interpret-trailers", "--parse"],
        stdin=body.encode("utf-8"),
    ).stdout

    prefix = f"{GENERATED_TRAILER_KEY}: "
    for line in parsed.splitlines():
        if line.startswith(prefix) and line[len(prefix) :] == GENERATED_TRAILER_VALUE:
            return True
    return False


def _list_changed_paths(
    *,
    cwd: Path,
    commit: str,
    process_runner: ProcessRunner | None = None,
) -> tuple[str, ...]:
    """Return the normalized repository-relative paths changed by ``commit``.

    Private helper used only for the trailer-only generated-commit warning
    diagnostic (ADR D3). Uses ``git diff-tree`` with ``--name-only``, ``-r``,
    and ``-z`` so paths containing whitespace, quotes, backslashes, or
    non-ASCII bytes round-trip unambiguously. ``--root`` makes root commits
    diff against the empty tree so their additions are included.
    ``--no-commit-id`` suppresses the commit-ID header line. ``--no-renames``
    disables rename detection. Raises :class:`InvalidRepositoryStateError`
    when the commit cannot be resolved.
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)

    try:
        runner.run(["rev-parse", "--verify", f"{commit}^{{commit}}"])
    except GitCommandError as exc:
        raise InvalidRepositoryStateError(
            f"cannot list changed paths for unknown commit {commit!r}: "
            f"{exc.stderr.strip() or exc}"
        ) from exc

    output = runner.run(
        [
            "diff-tree",
            "--root",
            "-r",
            "--no-commit-id",
            "--no-renames",
            "--name-only",
            "-z",
            commit,
        ]
    ).stdout

    paths: list[str] = []
    for entry in output.split("\x00"):
        if not entry:
            continue
        paths.append(entry)
    # ``-z`` output is unordered when multiple entries are produced from a
    # rename/copy pair; sort for determinism so callers see a stable,
    # comparable value.
    paths.sort()
    return tuple(paths)


def is_worktree_clean(
    *,
    cwd: Path,
    process_runner: ProcessRunner | None = None,
) -> bool:
    """Return True when the worktree has no uncommitted changes.

    Uses ``git status --porcelain`` to detect tracked modifications, staged
    edits, and untracked files. The porcelain format keeps output stable and
    machine-readable, and any non-empty output means the worktree is dirty.

    This is a read-only inspection; callers enforce ``require_clean_worktree``
    before any side-effecting submission or commit operations (spec section 25).
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    output = runner.run(
        [
            "status",
            "--porcelain",
            "--untracked-files=all",
        ]
    ).stdout
    return output.strip() == ""


def assert_committable_state(
    *,
    cwd: Path,
    require_symbolic_head: bool,
    process_runner: ProcessRunner | None = None,
) -> None:
    """Raise when the repository is not in a state where committing is meaningful.

    Resolves in-progress operation markers via ``git rev-parse --git-path`` so
    linked worktrees are handled correctly (ADR D2). Files like ``MERGE_HEAD``
    and ``CHERRY_PICK_HEAD`` and directories like ``rebase-merge/`` and
    ``rebase-apply/`` indicate that a commit would be ambiguous or dangerous.

    When ``require_symbolic_head`` is true, HEAD must be a symbolic ref. A
    detached HEAD with proof commits enabled would produce an orphaned proof
    commit as soon as the checkout moves. With ``proof.commit = false`` the
    tag attaches directly to the source commit and a detached HEAD is harmless.

    Raises :class:`InvalidRepositoryStateError` when any precondition fails.
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)

    # Annotated timestamp tags always need a tagger identity, and generated
    # proof commits need an author/committer identity as well. Resolve it before
    # submission so an unattended run cannot persist a proof only to fail while
    # creating the Git objects that record it.
    try:
        # Git otherwise invents a fallback from the operating-system account
        # and hostname on some platforms. `user.useConfigOnly` still accepts
        # normal Git configuration and explicit identity environment variables,
        # but makes the promised "do not invent an identity" rule portable.
        runner.run(["-c", "user.useConfigOnly=true", "var", "GIT_COMMITTER_IDENT"])
    except GitCommandError as exc:
        detail = exc.stderr.strip()
        message = (
            "Git author identity is not configured; set user.name and "
            "user.email (for example, `git config user.name NAME` and "
            "`git config user.email ADDRESS`) before running git-ots"
        )
        if detail:
            message += f": {detail}"
        raise InvalidRepositoryStateError(message) from None

    in_progress: list[str] = []
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD"):
        path = runner.run(["rev-parse", "--git-path", name]).stdout.strip()
        if (Path(cwd) / path).exists():
            in_progress.append(name)

    stale: list[str] = []
    for name in ("rebase-merge", "rebase-apply"):
        path = runner.run(["rev-parse", "--git-path", f"{name}/"]).stdout.strip()
        resolved = Path(cwd) / path
        if resolved.exists() and resolved.is_dir():
            in_progress.append(f"{name}/")
            # Git reports "You are currently rebasing" from the directory's
            # existence alone, so an empty one blocks work with no rebase to
            # continue or abort. Say so, rather than sending the reader looking
            # for an operation that never started.
            if not any(resolved.iterdir()):
                stale.append(str(resolved))

    if in_progress:
        message = (
            f"cannot run while a Git operation is in progress: {', '.join(in_progress)}"
        )
        if stale:
            message += (
                f" -- but {'it is' if len(stale) == 1 else 'they are'} empty and"
                " therefore a stale leftover, not a real operation. If no rebase"
                " is running, remove with: rmdir " + " ".join(stale)
            )
        raise InvalidRepositoryStateError(message)

    if require_symbolic_head:
        try:
            runner.run(["symbolic-ref", "HEAD"])
        except GitCommandError:
            raise InvalidRepositoryStateError(
                "cannot create proof commit on detached HEAD; checkout a branch first"
            ) from None


# Git's own wording for the two ways an index-lock collision surfaces. Both
# come from `lock_file.c`; the first is what a concurrent `git add`/`git
# commit` produces, the second what a concurrent ref update produces. Matched
# case-insensitively against stderr because the exit code alone (128) does not
# distinguish contention from any other fatal error, and retrying a genuine
# fatal error -- a rejected hook, a bad pathspec -- would turn a fast, honest
# failure into a slow one.
_INDEX_LOCK_MARKERS: tuple[str, ...] = (
    "index.lock",
    "unable to create ",
    "cannot lock ref",
)
_INDEX_LOCK_CONFIRMATION = "file exists"

# Backoff between retries: start at a tenth of a second, double, cap at two
# seconds. An interactive `git commit` holds the index for well under a
# second; a rebase or a large `git add` can hold it for several. Capping the
# interval keeps a long wait responsive rather than sleeping past the moment
# the lock clears.
_INDEX_LOCK_INITIAL_BACKOFF = 0.1
_INDEX_LOCK_MAX_BACKOFF = 2.0

# Used when the caller wants retries but has no ceiling to derive one from --
# an operator's `ots.gitTimeout = 0` (unbounded). Waiting forever for another
# process to release the index would convert a scheduled job into a stuck one,
# so the retry window is bounded even when the command itself is not.
DEFAULT_INDEX_LOCK_RETRY_WINDOW = 60.0


def is_index_lock_contention(error: GitCommandError) -> bool:
    """Return True when ``error`` is another process holding the Git index.

    An unattended timestamping job and a human working in the same repository
    contend for ``.git/index.lock`` as a matter of course, and Git reports
    that contention as a plain fatal error (exit 128). Recognising it lets the
    index-mutating steps wait the other process out instead of aborting a run
    whose OpenTimestamps submission has already happened.
    """
    if isinstance(error, GitCommandTimeoutError):
        return False
    stderr = error.stderr.lower()
    if _INDEX_LOCK_CONFIRMATION not in stderr and "another git process" not in stderr:
        return False
    return any(marker in stderr for marker in _INDEX_LOCK_MARKERS)


def run_with_index_lock_retry(
    runner: GitRunner,
    argv: Sequence[str],
    *,
    retry_window: float | None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    before_attempt: Callable[[], None] | None = None,
) -> GitSuccess:
    """Run an index-mutating Git command, waiting out index-lock contention.

    ``retry_window`` is the total wall-clock budget for retries, in seconds;
    ``None`` disables retrying entirely, which is the default everywhere so
    that adding this changes no existing caller's behaviour. Only
    :func:`is_index_lock_contention` failures are retried -- every other
    ``GitCommandError`` propagates on the first attempt, unchanged, so a
    rejected pre-commit hook still fails immediately and loudly.

    ``before_attempt`` revalidates any mutable preconditions before each
    invocation, including retries after another writer releases the lock.

    The window is deliberately spent on *retries* rather than folded into the
    per-command ceiling: ``ots.gitTimeout`` bounds how long one Git invocation
    may run, which is a different question from how long the tool is willing
    to wait for another process to let go of the index.
    """
    if retry_window is None or retry_window <= 0:
        if before_attempt is not None:
            before_attempt()
        return runner.run(list(argv))

    deadline = monotonic() + retry_window
    backoff = _INDEX_LOCK_INITIAL_BACKOFF
    attempt = 0
    while True:
        attempt += 1
        if before_attempt is not None:
            before_attempt()
        try:
            return runner.run(list(argv))
        except GitCommandError as exc:
            if not is_index_lock_contention(exc):
                raise
            remaining = deadline - monotonic()
            if remaining <= 0:
                _logger.warning(
                    "Gave up waiting for the Git index lock after %.0fs and "
                    "%d attempt(s): %s",
                    retry_window,
                    attempt,
                    exc.stderr.strip() or exc,
                )
                raise
            _logger.info(
                "Git index is locked by another process; retrying in %.1fs "
                "(%.0fs of the retry budget left)",
                min(backoff, remaining),
                remaining,
            )
            sleep(min(backoff, remaining))
            backoff = min(backoff * 2, _INDEX_LOCK_MAX_BACKOFF)


def stage_paths(
    *,
    cwd: Path,
    paths: list[str] | tuple[str, ...],
    process_runner: ProcessRunner | None = None,
    index_lock_retry_window: float | None = None,
) -> GitSuccess:
    """Stage only the explicitly named paths.

    Uses ``git add -- <path>...`` so each path is treated as a literal
    pathspec, never as an option. This guarantees that only the generated
    proof and manifest files supplied by the caller enter the index (spec
    section 25). The runner still receives the full argument array and
    fixed working directory.

    ``index_lock_retry_window`` bounds how long to wait out another process
    holding ``.git/index.lock``; ``None`` (the default) fails on the first
    collision, as before.
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    return run_with_index_lock_retry(
        runner, ["add", "--", *paths], retry_window=index_lock_retry_window
    )


def paths_are_committed(
    *,
    cwd: Path,
    paths: Sequence[str],
    process_runner: ProcessRunner | None = None,
) -> bool:
    """Return True when every path exists in the current ``HEAD`` tree.

    Used during crash recovery to determine whether proof/manifest artifacts
    have already been committed by a generated proof commit, avoiding
    duplicate generated commits.
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    for path in paths:
        output = runner.run(
            [
                "ls-tree",
                "-r",
                "HEAD",
                "--",
                path,
            ]
        ).stdout.strip()
        if not output:
            return False
    return True


def _validate_proof_directory(proof_directory: str) -> None:
    if not proof_directory:
        raise ValueError("proof_directory must not be empty")
    if proof_directory.endswith("/"):
        raise ValueError("proof_directory must not end with '/'")


def _warn_when_generated_commit_changes_outside_paths(
    *,
    cwd: Path,
    commit: str,
    proof_directory: str,
    process_runner: ProcessRunner | None = None,
) -> None:
    """Emit the ADR D3 diagnostic for a trailer-bearing commit.

    Path inspection is no longer part of the classification; it survives only
    as a warning when a commit carrying the generated trailer also changes
    paths outside the configured proof directory, so that a forged or squashed
    trailer is visible rather than silently ignored. The proof directory is
    matched on path-segment boundaries, so a sibling like
    ``.opentimestamps-evil/x`` is not treated as inside ``.opentimestamps``.
    """

    paths = _list_changed_paths(cwd=cwd, commit=commit, process_runner=process_runner)
    prefix = f"{proof_directory}/"
    outside = [p for p in paths if not p.startswith(prefix)]
    if outside:
        _logger.warning(
            "Commit %s carries the OpenTimestamps-Generated trailer but "
            "changes paths outside the configured proof directory %r: %s. "
            "It is being treated as generated metadata.",
            commit[:12],
            proof_directory,
            ", ".join(sorted(outside)),
        )


def is_generated_proof_commit(
    *,
    cwd: Path,
    commit: str,
    proof_directory: str,
    process_runner: ProcessRunner | None = None,
) -> bool:
    """Return True when ``commit`` is an ``git-ots``-generated proof commit.

    A commit is recognized as generated metadata by the canonical
    ``OpenTimestamps-Generated: true`` trailer alone (ADR D3), with a warning
    diagnostic when a trailer-bearing commit also changes paths outside the
    configured proof directory.
    """

    _validate_proof_directory(proof_directory)

    # ``has_generated_trailer`` raises InvalidRepositoryStateError for an
    # unknown commit, which is the contract callers rely on.
    if not has_generated_trailer(cwd=cwd, commit=commit, process_runner=process_runner):
        return False

    _warn_when_generated_commit_changes_outside_paths(
        cwd=cwd,
        commit=commit,
        proof_directory=proof_directory,
        process_runner=process_runner,
    )
    return True


def _log_generated_trailer_commit_ids(
    *,
    cwd: Path,
    range_spec: str,
    process_runner: ProcessRunner | None = None,
) -> tuple[str, ...]:
    """Return commit IDs in ``range_spec`` carrying the canonical generated trailer.

    Uses a single ``git log`` invocation with the ``%(trailers)`` format so the
    cost is one subprocess regardless of history size. ``only=true`` restricts
    output to actual trailer lines (prose that merely looks like a trailer is
    excluded, matching ``git interpret-trailers --parse``) and ``unfold=true``
    joins folded continuation lines exactly as ``--parse`` does, so a folded
    value never equals the canonical ``true``. The key is matched in Python
    against the exact canonical trailer line because ``%(trailers:key=...)``
    matches keys case-insensitively, which would wrongly accept miscased keys
    that ``has_generated_trailer`` rejects.

    Commit IDs are returned newest-to-oldest (``git log`` order).
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    output = runner.run(
        [
            "log",
            "-z",
            "--format=%H%x1f%(trailers:only=true,unfold=true)",
            range_spec,
        ]
    ).stdout

    canonical = f"{GENERATED_TRAILER_KEY}: {GENERATED_TRAILER_VALUE}"
    found: list[str] = []
    for record in output.split("\x00"):
        if not record:
            continue
        commit_id, _, trailers = record.partition("\x1f")
        if not commit_id:
            continue
        if any(line == canonical for line in trailers.splitlines()):
            found.append(commit_id)
    return tuple(found)


def _read_message_and_trailers(
    *,
    cwd: Path,
    commit: str,
    process_runner: ProcessRunner | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Return ``commit``'s raw message body and its parsed trailer lines.

    One read of both, because a caller that needs the trailers usually needs
    the message they came out of: reading them separately costs a second
    ``log`` and a second ``interpret-trailers``, and lets the two answers be
    about different commits if the ref moves between them.

    ``--parse`` means only actual trailer lines are returned, so body text
    that merely looks like a trailer is not among them. Raises
    :class:`InvalidRepositoryStateError` when ``commit`` cannot be resolved.
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)

    try:
        runner.run(["rev-parse", "--verify", f"{commit}^{{commit}}"])
    except GitCommandError as exc:
        raise InvalidRepositoryStateError(
            f"cannot read the message of unknown commit {commit!r}: "
            f"{exc.stderr.strip() or exc}"
        ) from exc

    body = runner.run(["log", "-1", "--format=%B", commit]).stdout
    parsed = runner.run(
        ["interpret-trailers", "--parse"],
        stdin=body.encode("utf-8"),
    ).stdout
    return body, tuple(parsed.splitlines())


def _trailer_values(trailer_lines: Sequence[str], key: str) -> tuple[str, ...]:
    """Pick one key's values out of parsed trailer lines, in message order.

    The key is matched case-sensitively against its canonical spelling,
    matching :func:`has_generated_trailer` -- Git's own ``%(trailers:key=...)``
    matches case-insensitively, which would accept miscased keys this tool
    rejects everywhere else.
    """

    prefix = f"{key}: "
    return tuple(
        line[len(prefix) :].strip() for line in trailer_lines if line.startswith(prefix)
    )


def _extract_trailer_values(
    *,
    cwd: Path,
    commit: str,
    key: str,
    process_runner: ProcessRunner | None = None,
) -> tuple[str, ...]:
    """Return the values of ``commit``'s ``key`` trailers, in message order."""

    _body, trailer_lines = _read_message_and_trailers(
        cwd=cwd, commit=commit, process_runner=process_runner
    )
    return _trailer_values(trailer_lines, key)


def _extract_generated_commit_sources(
    *,
    cwd: Path,
    commit: str,
    process_runner: ProcessRunner | None = None,
) -> tuple[str, ...]:
    """Return source commit IDs from a commit's ``OpenTimestamps-Source`` trailers."""

    return _extract_trailer_values(
        cwd=cwd,
        commit=commit,
        key=SOURCE_TRAILER_KEY,
        process_runner=process_runner,
    )


def get_commit_parents(
    *,
    cwd: Path,
    commit: str,
    process_runner: ProcessRunner | None = None,
) -> tuple[str, ...]:
    """Return the full commit IDs of ``commit``'s parents in repository order.

    Uses ``git rev-list --parents -n 1`` so merge commits return multiple
    parents and root commits return an empty tuple. Raises
    :class:`InvalidRepositoryStateError` when ``commit`` cannot be resolved.
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)

    try:
        runner.run(["rev-parse", "--verify", f"{commit}^{{commit}}"])
    except GitCommandError as exc:
        raise InvalidRepositoryStateError(
            f"cannot read parents for unknown commit {commit!r}: "
            f"{exc.stderr.strip() or exc}"
        ) from exc

    output = runner.run(["rev-list", "--parents", "-n", "1", commit]).stdout.strip()
    if not output:
        return ()
    parts = output.split()
    # First token is the commit itself; remaining tokens are its parents.
    return tuple(parts[1:])


def enumerate_generated_proof_commit_sources(
    *,
    cwd: Path,
    ref: str,
    baseline: str | None,
    proof_directory: str,
    process_runner: ProcessRunner | None = None,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return ``(commit_id, claimed_sources)`` for generated proof commits in range.

    Searches the half-open range ``baseline..ref`` (or ``ref`` when
    ``baseline`` is ``None``) with a single history pass; only commits that
    carry the canonical generated trailer incur further per-commit
    inspection, so the subprocess cost is bounded by the number of generated
    proof commits, not by the range size. Results are in oldest-to-newest
    repository order. The ADR D3 warning for trailer-bearing commits that
    change paths outside the proof directory is emitted here, exactly as the
    per-commit classification would.
    """

    _validate_proof_directory(proof_directory)

    runner = GitRunner(cwd=cwd, process_runner=process_runner)

    try:
        runner.run(["rev-parse", "--verify", f"{ref}^{{commit}}"])
    except GitCommandError as exc:
        raise InvalidRepositoryStateError(
            f"cannot search generated proof commits for unknown ref {ref!r}: "
            f"{exc.stderr.strip() or exc}"
        ) from exc

    if baseline is not None:
        try:
            runner.run(["rev-parse", "--verify", f"{baseline}^{{commit}}"])
        except GitCommandError as exc:
            raise InvalidRepositoryStateError(
                f"cannot search generated proof commits for unknown baseline "
                f"{baseline!r}: {exc.stderr.strip() or exc}"
            ) from exc
        range_spec = f"{baseline}..{ref}"
    else:
        range_spec = ref

    generated_ids = _log_generated_trailer_commit_ids(
        cwd=cwd, range_spec=range_spec, process_runner=process_runner
    )

    results: list[tuple[str, tuple[str, ...]]] = []
    for commit_id in reversed(generated_ids):
        _warn_when_generated_commit_changes_outside_paths(
            cwd=cwd,
            commit=commit_id,
            proof_directory=proof_directory,
            process_runner=process_runner,
        )
        sources = _extract_generated_commit_sources(
            cwd=cwd,
            commit=commit_id,
            process_runner=process_runner,
        )
        results.append((commit_id, sources))
    return tuple(results)


def find_generated_proof_commits_claiming_source(
    *,
    cwd: Path,
    source_commit_id: str,
    ref: str,
    baseline: str | None,
    proof_directory: str,
    process_runner: ProcessRunner | None = None,
) -> tuple[str, ...]:
    """Return commit IDs of generated proof commits that claim ``source_commit_id``.

    Searches the half-open range ``baseline..ref`` (or ``ref`` when
    ``baseline`` is ``None``) for generated proof commits that carry an
    ``OpenTimestamps-Source`` trailer naming ``source_commit_id``.
    """

    claims = enumerate_generated_proof_commit_sources(
        cwd=cwd,
        ref=ref,
        baseline=baseline,
        proof_directory=proof_directory,
        process_runner=process_runner,
    )
    return tuple(
        commit_id for commit_id, sources in claims if source_commit_id in sources
    )


def enumerate_commits(
    *,
    cwd: Path,
    ref: str,
    process_runner: ProcessRunner | None = None,
) -> tuple[CommitInfo, ...]:
    """Enumerate commits reachable from ``ref`` in repository order.

    Returns commits oldest-to-newest using the committer timestamp as
    recorded by Git. Author dates are deliberately not consulted. Raises
    :class:`InvalidRepositoryStateError` when the ref cannot be resolved.
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)

    # Verify the ref once so unknown refs surface as a typed error rather
    # than as an empty history.
    try:
        runner.run(["rev-parse", "--verify", f"{ref}^{{commit}}"])
    except GitCommandError as exc:
        raise InvalidRepositoryStateError(
            f"cannot enumerate commits for unknown ref {ref!r}: "
            f"{exc.stderr.strip() or exc}"
        ) from exc

    # Use NUL-separated %H and %ct so commit IDs containing no whitespace
    # parse cleanly. --reverse gives oldest-to-newest repository order.
    output = runner.run(
        [
            "log",
            "--reverse",
            "--format=%H%x00%ct",
            ref,
        ]
    ).stdout

    commits: list[CommitInfo] = []
    for line in output.splitlines():
        if not line:
            continue
        commit_id, _, ts_text = line.partition("\x00")
        if not commit_id or not ts_text:
            raise InvalidRepositoryStateError(f"unexpected git log line: {line!r}")
        try:
            epoch = int(ts_text)
        except ValueError as exc:
            raise InvalidRepositoryStateError(
                f"unexpected committer timestamp {ts_text!r} for {commit_id}"
            ) from exc
        commits.append(
            CommitInfo(
                commit_id=commit_id,
                committer_time=datetime.fromtimestamp(epoch, tz=UTC),
            )
        )
    return tuple(commits)


class TimestampTagAnnotationError(ValueError):
    """Raised when a timestamp-tag annotation fails strict validation.

    Spec section 12 fixes the annotation schema; any deviation — wrong
    schema version, missing or extra keys, malformed identifiers or
    timestamps, mismatched source ids — makes the tag unusable as a
    timestamp baseline.
    """


@dataclass(frozen=True, slots=True)
class ValidatedAnnotation:
    """Strictly validated timestamp-tag annotation.

    ``source`` is the full commit ID the tag claims to timestamp and
    must equal the tag's target commit. ``submitted_at`` is the
    timezone-aware UTC timestamp from the annotation. ``triggers`` is
    the non-empty set of policy triggers that fired.
    """

    schema: int
    source: str
    submitted_at: datetime
    proof: str
    triggers: frozenset[str]
    producer: str | None = None


# Triggers the policy layer (policy.py) can record. An annotation naming
# anything outside this set is treated as malformed.
KNOWN_TRIGGERS: frozenset[str] = frozenset({"every_commit", "max_age", "fixed_time"})

_HEX_COMMIT_ID_LENGTHS = (40, 64)


@dataclass(frozen=True, slots=True)
class TimestampTag:
    """A timestamp tag under the configured prefix.

    ``name`` is the full tag name (for example
    ``ots/20260808T220003Z/0123456789ab``). ``commit_id`` is the full
    object ID of the commit the tag points at — the *target* of the
    annotated tag object, not the tag object itself. ``annotation`` is
    the parsed tag message as a mapping of ``key: value`` lines, with
    leading/trailing whitespace stripped from values. Strict schema
    validation is a separate concern, handled by
    ``validate_timestamp_tag``; this mapping preserves whatever keys the
    tagger wrote so the strict parser can reject malformed entries
    without re-reading the repository.
    """

    name: str
    commit_id: str
    annotation: tuple[tuple[str, str], ...]

    def annotation_get(self, key: str) -> str | None:
        for k, v in self.annotation:
            if k == key:
                return v
        return None


def enumerate_timestamp_tags(
    *,
    cwd: Path,
    tag_prefix: str,
    process_runner: ProcessRunner | None = None,
) -> tuple[TimestampTag, ...]:
    """Return annotated tags under ``tag_prefix`` as timestamp tags.

    Runs ``git for-each-ref refs/tags/<prefix>*`` and keeps only entries
    whose object type is ``tag`` (annotated tags). Lightweight tags —
    which point directly at commits and carry no annotation — are
    excluded because they cannot carry the schema/source/submitted
    metadata spec section 12 requires. Tags outside the prefix are
    never inspected.

    The tag target is resolved with ``%(*objectname)`` so the returned
    ``commit_id`` is the tagged commit, not the tag object. Fields are
    separated with ``%00`` (literal NUL) so an annotation containing
    newlines round-trips unambiguously. The annotation body is parsed
    into ``key: value`` pairs line-by-line; strict shape validation
    (schema, submitted-at, proof, triggers) belongs to
    ``validate_timestamp_tag``.

    Results are sorted by tag name for deterministic iteration.
    """

    if not tag_prefix:
        raise ValueError("tag_prefix must not be empty")

    runner = GitRunner(cwd=cwd, process_runner=process_runner)

    # %(objecttype) is "tag" for annotated tags and "commit" for
    # lightweight tags; %(*objectname) dereferences annotated tags to
    # their target commit (empty for lightweight tags). The literal \x00
    # separators keep fields unambiguous even if a tag message contains
    # newlines or whitespace.
    # ``git for-each-ref`` treats a pattern without glob characters as a
    # literal or as a prefix when it ends in ``/``. A glob like ``*``
    # would not cross ``/``, which is exactly what we don't want here:
    # the prefix is the user's namespace (``ots/``) and may contain
    # nested path components (``ots/20260808T220003Z/0123456789ab``).
    # Filter in Python instead so any non-empty prefix is matched
    # literally, with or without a trailing slash.
    output = runner.run(
        [
            "for-each-ref",
            "--format=%(refname:short)%00%(objecttype)%00%(*objectname)%00%(contents)%00",
            "refs/tags/",
        ]
    ).stdout

    tags: list[TimestampTag] = []
    for record in output.split("\x00\n"):
        record = record.rstrip("\x00")
        if not record:
            continue
        # The record itself is four NUL-separated fields; the annotation
        # body may contain arbitrary newlines, which is why we split the
        # stream on NUL-newline above and split fields on NUL here.
        parts = record.split("\x00", 3)
        if len(parts) != 4:
            continue
        name, object_type, target_id, contents = parts
        if not name.startswith(tag_prefix):
            continue
        if object_type != "tag":
            continue
        if not target_id:
            continue
        annotation = _parse_tag_annotation(strip_signature_block(contents))
        tags.append(
            TimestampTag(
                name=name,
                commit_id=target_id,
                annotation=annotation,
            )
        )
    tags.sort(key=lambda t: t.name)
    return tuple(tags)


def validate_timestamp_tag(tag: TimestampTag) -> ValidatedAnnotation:
    """Validate a timestamp-tag annotation against the spec-12 schema.

    The annotation must contain exactly the keys ``git-ots schema``,
    ``source``, ``submitted-at``, ``proof``, and ``triggers`` — no
    duplicates, no extras. The schema must be the integer ``1``. The
    ``source`` must be a full lowercase hex commit ID equal to the
    tag's target. ``submitted-at`` must be a timezone-aware RFC 3339
    timestamp. ``proof`` must end in ``.ots`` and embed the source
    commit ID. ``triggers`` must be a non-empty set of known policy
    trigger tokens separated by whitespace or commas.

    Raises :class:`TimestampTagAnnotationError` on any deviation.
    """

    def fail(reason: str) -> TimestampTagAnnotationError:
        return TimestampTagAnnotationError(
            f"invalid annotation on tag {tag.name!r}: {reason}"
        )

    required_keys = (
        "git-ots schema",
        "source",
        "submitted-at",
        "proof",
        "triggers",
    )

    seen: dict[str, str] = {}
    for key, value in tag.annotation:
        if key in seen:
            raise fail(f"duplicate key {key!r}")
        seen[key] = value

    extras = [k for k in seen if k not in (*required_keys, "producer")]
    if extras:
        raise fail(f"unexpected key(s): {', '.join(sorted(extras))}")
    missing = [k for k in required_keys if k not in seen]
    if missing:
        raise fail(f"missing key(s): {', '.join(missing)}")

    schema_text = seen["git-ots schema"]
    try:
        schema = int(schema_text, 10)
    except ValueError:
        raise fail(f"git-ots schema is not an integer: {schema_text!r}")
    if schema not in (1, 2):
        raise fail(f"unsupported git-ots schema {schema}")
    producer = seen.get("producer")
    if schema == 2 and not producer:
        raise fail("schema 2 requires a non-empty producer")
    if schema == 1 and producer is not None:
        raise fail("schema 1 does not define producer")

    source = seen["source"]
    if len(source) not in _HEX_COMMIT_ID_LENGTHS or any(
        c not in "0123456789abcdef" for c in source
    ):
        raise fail(f"source is not a full lowercase hex commit id: {source!r}")
    if source != tag.commit_id:
        raise fail(f"source {source!r} does not match tag target {tag.commit_id!r}")

    submitted_text = seen["submitted-at"]
    parse_text = (
        submitted_text[:-1] + "+00:00"
        if submitted_text.endswith("Z")
        else submitted_text
    )
    try:
        submitted_at = datetime.fromisoformat(parse_text)
    except ValueError:
        raise fail(f"submitted-at is not a valid timestamp: {submitted_text!r}")
    if submitted_at.tzinfo is None or submitted_at.utcoffset() is None:
        raise fail(f"submitted-at must be timezone-aware: {submitted_text!r}")

    proof = seen["proof"]
    if not proof.endswith(".ots"):
        raise fail(f"proof must end with '.ots': {proof!r}")
    if source not in proof:
        raise fail(f"proof {proof!r} does not reference source commit {source!r}")

    triggers_text = seen["triggers"].replace(",", " ")
    trigger_tokens = {tok for tok in triggers_text.split() if tok}
    if not trigger_tokens:
        raise fail("triggers must not be empty")
    unknown = trigger_tokens - KNOWN_TRIGGERS
    if unknown:
        raise fail(f"unknown trigger(s): {', '.join(sorted(unknown))}")

    return ValidatedAnnotation(
        schema=schema,
        source=source,
        submitted_at=submitted_at,
        proof=proof,
        triggers=frozenset(trigger_tokens),
        producer=producer,
    )


#: Opening line of an ASCII-armored signature block appended to a tag body.
#: Matched loosely on purpose: GPG emits ``-----BEGIN PGP SIGNATURE-----``,
#: ``gpg.format = ssh`` emits ``-----BEGIN SSH SIGNATURE-----``, and x509
#: signers emit ``-----BEGIN SIGNED MESSAGE-----``. The spec-12 annotation is a
#: fixed set of ``key: value`` lines, so no legitimate annotation line can take
#: this shape and a permissive match cannot swallow real content.
_SIGNATURE_ARMOR_RE = re.compile(r"^-----BEGIN [A-Z0-9][A-Z0-9 ]*-----$")


def strip_signature_block(body: str) -> str:
    """Return ``body`` with any trailing ASCII-armored signature removed.

    Git stores a tag signature *inside the tag body*, after the annotation
    ``git tag -m`` was given, so every reader of an annotation sees it. Two
    things break if it is not removed. Armored signatures may carry headers
    (``Version:``, ``Comment:``) which parse as ``key: value`` pairs, and
    :func:`validate_timestamp_tag` rejects any annotation with keys outside
    the schema-12 set -- so a signed tag stops counting as a timestamp
    baseline and its source commit is stamped again. Separately,
    :func:`create_timestamp_tag` compares a stored annotation against a freshly
    rendered one to decide whether an existing tag is the same tag; a signature
    on one side and not the other makes every comparison a false collision.

    This is applied unconditionally, not only when ``git-ots`` did the signing:
    ``tag.gpgSign = true`` in ambient Git configuration already signs the tags
    this tool creates, so annotations in the wild can carry a signature that no
    ``git-ots`` setting asked for.

    Commit signatures need no equivalent: ``gpgsig`` is a commit *header*, so
    it never appears in ``%B`` or in the trailers used to classify generated
    proof commits.
    """

    # Slice at the armor line's offset rather than rejoining the lines before
    # it: the newline terminating the last annotation line belongs to the
    # annotation, and create_timestamp_tag compares the result against a
    # rendered annotation that ends in one.
    offset = 0
    for line in body.split("\n"):
        if _SIGNATURE_ARMOR_RE.match(line):
            return body[:offset]
        offset += len(line) + 1
    return body


def _parse_tag_annotation(contents: str) -> tuple[tuple[str, str], ...]:
    """Parse a tag annotation body into ``key: value`` pairs.

    Lines without a ``key: value`` shape are skipped so a stray header
    or trailing newline does not produce phantom entries. Values have
    leading and trailing whitespace stripped. Parsing here is
    deliberately permissive — strict schema validation (rejecting
    missing keys, malformed timestamps, mismatched source ids) is
    performed by ``validate_timestamp_tag``.
    """

    pairs: list[tuple[str, str]] = []
    for line in contents.splitlines():
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not key:
            continue
        pairs.append((key, value.strip()))
    return tuple(pairs)


class BaselineKind(Enum):
    """Which marker a baseline candidate was derived from.

    Kind is recorded explicitly rather than inferred from ``tag_name`` so the
    candidate ordering never depends on how a configured tag prefix happens to
    sort against a synthesized name.
    """

    TAG = "tag"
    PROOF_COMMIT = "proof-commit"


# Ordering rank used to break `submitted_at` ties between candidate kinds. The
# proof commit wins: ADR D6 makes it the authoritative idempotence marker, and
# it is validated for ancestry and payload binding before it becomes a
# candidate, whereas a tag annotation is accepted on its own word.
_BASELINE_KIND_RANK: dict[BaselineKind, int] = {
    BaselineKind.TAG: 0,
    BaselineKind.PROOF_COMMIT: 1,
}


def _baseline_sort_key(baseline: TimestampBaseline) -> tuple[datetime, int, str, str]:
    """Return a total ordering key; the greatest candidate is the newest baseline."""
    return (
        baseline.submitted_at,
        _BASELINE_KIND_RANK[baseline.kind],
        baseline.source_commit_id,
        baseline.tag_name,
    )


@dataclass(frozen=True, slots=True)
class TimestampBaseline:
    """The newest relevant timestamped source commit for a source ref.

    ``source_commit_id`` is the full object ID of the tagged source commit,
    which is an ancestor of the frozen source ref. ``submitted_at`` comes
    from the validated tag annotation and is used to pick the newest among
    multiple ancestor tags. ``tag_name`` is the full annotated tag name so
    diagnostics can refer to the exact tag. ``proof_commit_id`` is set when
    the baseline was derived from a generated proof commit rather than a tag
    (ADR D6); it records which commit carried the manifest used as evidence.
    ``kind`` states which marker produced the candidate, and is what breaks a
    ``submitted_at`` tie between the two sources -- never ``tag_name``, which
    for proof commits is synthesized rather than a real ref.
    """

    source_commit_id: str
    submitted_at: datetime
    triggers: frozenset[str]
    tag_name: str
    proof_commit_id: str | None = None
    kind: BaselineKind = BaselineKind.TAG


@dataclass(frozen=True, slots=True)
class LineageBaselineResult:
    """Result of searching for a relevant timestamp baseline.

    ``baseline`` is the newest validated timestamp tag that is an ancestor of
    the frozen source commit, or ``None`` when no such tag exists.
    ``has_abandoned_tags`` is ``True`` when at least one validated timestamp
    tag exists but none are ancestors of the source commit. This distinction
    matters for rewritten history (spec section 26): aggregate policies
    may start a new lineage, while ``every_commit`` must refuse to replay an
    entire rewritten history.
    """

    baseline: TimestampBaseline | None
    has_abandoned_tags: bool


def _read_tree_text(
    *,
    cwd: Path,
    commit: str,
    path: str,
    process_runner: ProcessRunner | None = None,
) -> str | None:
    """Return the text content of ``path`` in ``commit``'s tree, or ``None``.

    Uses ``git show <commit>:<path>``. Missing objects, objects that are not
    valid strict UTF-8, or non-text objects return ``None`` without raising so
    callers can fall through to other evidence.
    """
    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    try:
        text = runner.run(["show", f"{commit}:{path}"]).stdout
    except GitCommandError:
        return None
    try:
        text.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return None
    return text


def read_tree_bytes(
    *,
    cwd: Path,
    commit: str,
    path: str,
    process_runner: ProcessRunner | None = None,
    process_runner_bytes: ProcessRunnerBytes | None = None,
) -> bytes | None:
    """Return the raw bytes of ``path`` in ``commit``'s tree, or ``None``.

    Used for binary objects such as detached OpenTimestamps proof files.
    Missing objects return ``None`` without raising. This is public because
    mutation recovery must compare a binary worktree artifact with its last
    committed value before deciding that it is safe to adopt.
    """
    runner = GitRunner(
        cwd=cwd,
        process_runner=process_runner,
        process_runner_bytes=process_runner_bytes,
    )
    try:
        return runner.run_bytes(["show", f"{commit}:{path}"]).stdout
    except GitCommandError:
        return None


def list_tree_proof_paths(
    *,
    cwd: Path,
    commit: str,
    proof_directory: str,
    process_runner: ProcessRunner | None = None,
) -> frozenset[str]:
    """Return every repository-relative path under the proof directory in a tree.

    One ``git ls-tree`` for the whole directory. Callers that need to know
    whether a particular artifact is committed must use this rather than
    reading each artifact individually: per-artifact reads make the subprocess
    cost grow with the number of stamps ever made, which is the trap ADR 0001
    D6's "validation must be bounded" refinement records after a repository
    stamped hourly was measured into the hundreds of subprocesses per
    invocation.

    ``-z`` makes ``git ls-tree`` emit literal NUL-separated paths; without it,
    paths containing non-ASCII or special characters are C-style quoted and
    would not round-trip into ``git show <commit>:<path>``.
    """
    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    try:
        output = runner.run(
            [
                "ls-tree",
                "-r",
                "-z",
                "--name-only",
                commit,
                "--",
                f"{proof_directory}/",
            ]
        ).stdout
    except GitCommandError:
        return frozenset()
    prefix = f"{proof_directory}/"
    return frozenset(
        entry for entry in output.split("\x00") if entry.startswith(prefix)
    )


def _list_tree_json_manifests(
    *,
    cwd: Path,
    commit: str,
    proof_directory: str,
    process_runner: ProcessRunner | None = None,
) -> tuple[str, ...]:
    """Return repository-relative ``.json`` manifest paths under the proof directory.

    A generated proof commit may carry one manifest per source (for example an
    ``every_commit`` batch), so the validation loop inspects each ``.json``
    entry in turn.
    """
    return tuple(
        sorted(
            path
            for path in list_tree_proof_paths(
                cwd=cwd,
                commit=commit,
                proof_directory=proof_directory,
                process_runner=process_runner,
            )
            if path.endswith(".json")
        )
    )


def is_ancestor_commit(
    *,
    cwd: Path,
    source_commit_id: str,
    frozen_source_id: str,
    process_runner: ProcessRunner | None = None,
) -> bool:
    """Return True when ``source_commit_id`` resolves and is an ancestor of ``frozen_source_id``."""
    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    try:
        runner.run(
            [
                "merge-base",
                "--is-ancestor",
                source_commit_id,
                frozen_source_id,
            ]
        )
    except GitCommandError:
        return False
    return True


def has_generated_proof_commits_in_history(
    *,
    cwd: Path,
    source_commit_id: str,
    process_runner: ProcessRunner | None = None,
) -> bool:
    """Return True when the source history contains any generated proof commit.

    This is used by the legacy "tag exists, artifacts uncommitted" recovery
    path to decide whether the repository has already adopted the D6
    idempotence marker (proof commits). When proof commits exist, a tag-only
    baseline is treated as complete and leftover worktree artifacts are not
    auto-committed.
    """
    return bool(
        _find_generated_proof_commit_ancestors(
            cwd=cwd,
            source_commit_id=source_commit_id,
            process_runner=process_runner,
        )
    )


def _find_generated_proof_commit_ancestors(
    *,
    cwd: Path,
    source_commit_id: str,
    process_runner: ProcessRunner | None = None,
) -> tuple[str, ...]:
    """Return all generated proof commits reachable from ``source_commit_id``.

    Results are ordered newest-to-oldest so callers can stop after the first
    valid one if desired. A commit is recognized by the canonical generated
    trailer alone (ADR D3); path checks are intentionally avoided here. The
    entire reachable history is inspected with a single ``git log``
    invocation, so the subprocess cost is constant in history size.
    """
    try:
        return _log_generated_trailer_commit_ids(
            cwd=cwd,
            range_spec=source_commit_id,
            process_runner=process_runner,
        )
    except GitCommandError:
        return ()


def _batch_read_tree_texts(
    *,
    cwd: Path,
    object_specs: Sequence[str],
    process_runner: ProcessRunner | None = None,
) -> dict[str, str]:
    """Read multiple ``<commit>:<path>`` objects in one ``git cat-file --batch`` pass.

    Returns a mapping from each successfully-read object spec to its UTF-8
    decoded text content. Missing objects and objects that are not valid UTF-8
    are omitted from the result.
    """
    if not object_specs:
        return {}

    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    stdin = "\n".join(object_specs).encode("utf-8")
    try:
        result = runner.run_bytes(["cat-file", "--batch"], stdin=stdin)
    except GitCommandError:
        return {}

    output = result.stdout
    contents: dict[str, str] = {}
    offset = 0
    for spec in object_specs:
        newline = output.find(b"\n", offset)
        if newline == -1:
            break
        header = output[offset:newline].decode("ascii", errors="replace")
        offset = newline + 1
        if header.endswith(" missing"):
            continue
        parts = header.split()
        if len(parts) != 3:
            break
        try:
            size = int(parts[2])
        except ValueError:
            break
        content_end = offset + size
        if content_end > len(output):
            break
        try:
            contents[spec] = output[offset:content_end].decode("utf-8")
        except UnicodeDecodeError:
            pass
        offset = content_end
        # ``git cat-file --batch`` emits a trailing newline after each blob
        # content that is not counted in the size header; skip it so the next
        # header starts at ``offset``.
        if offset < len(output) and output[offset] == ord("\n"):
            offset += 1
    return contents


def _validate_proof_commit_manifest_content(
    *,
    cwd: Path,
    proof_commit_id: str,
    manifest: dict,
    proof_directory: str,
    object_format: str,
    frozen_source_id: str,
    artifact_commit: str | None = None,
    process_runner: ProcessRunner | None = None,
) -> TimestampBaseline | None:
    """Validate an already-read manifest dict and return a baseline candidate.

    The manifest's ``source_commit`` must resolve and be an ancestor of the
    frozen source. The referenced proof must bind cryptographically to the
    canonical payload for that source. If any step fails, ``None`` is returned
    so the search falls through safely.

    ``artifact_commit`` is the tree the referenced proof is read from, and
    defaults to ``proof_commit_id``. They differ when manifests are collected
    from the frozen source's tree: the artifacts are read from the
    source, while ``proof_commit_id`` still records the generated proof commit
    that serves as the ADR D6 marker.
    """
    artifact_commit = artifact_commit or proof_commit_id
    source_commit_id = manifest.get("source_commit")
    if not isinstance(source_commit_id, str):
        return None

    if not is_ancestor_commit(
        cwd=cwd,
        source_commit_id=source_commit_id,
        frozen_source_id=frozen_source_id,
        process_runner=process_runner,
    ):
        return None

    try:
        payload = build_payload(object_format, source_commit_id)
    except PayloadValidationError:
        return None

    proof_name = manifest.get("proof")
    if not isinstance(proof_name, str) or not proof_name.endswith(".ots"):
        return None
    proof_path = f"{proof_directory}/{proof_name}"
    proof_bytes = read_tree_bytes(
        cwd=cwd,
        commit=artifact_commit,
        path=proof_path,
        process_runner=process_runner,
    )
    if proof_bytes is None:
        return None
    try:
        validate_detached_proof(proof_bytes, payload)
    except ValueError:
        return None

    try:
        submitted_at, triggers = _validate_recovery_manifest(
            manifest=manifest,
            commit_id=source_commit_id,
            object_format=object_format,
        )
    except (RecoveryValidationError, PayloadValidationError, ValueError):
        return None

    return TimestampBaseline(
        source_commit_id=source_commit_id,
        submitted_at=submitted_at,
        triggers=triggers,
        tag_name=f"proof-commit:{proof_commit_id}",
        proof_commit_id=proof_commit_id,
        kind=BaselineKind.PROOF_COMMIT,
    )


def read_committed_proof_evidence(
    *,
    cwd: Path,
    source_commit_id: str,
    artifact_commit: str,
    proof_directory: str,
    object_format: str,
    process_runner: ProcessRunner | None = None,
) -> ProofEvidence | None:
    """Return validated tag evidence for ``source_commit_id`` from a commit tree.

    Reads ``<proof_directory>/<source_commit_id>.json`` and the proof it names
    out of ``artifact_commit``'s tree, then applies the same two checks the
    baseline search applies: the manifest must satisfy the schema-1 contract,
    and the referenced proof must bind cryptographically to the canonical
    payload for ``source_commit_id``. Returns ``None`` -- never raises -- when
    any of that fails, so callers fall through to other evidence.

    Reading from a tree rather than the worktree is deliberate and does two
    jobs at once. It honours ADR 0001 D6 rule 4 (historical evidence comes
    from history, not from files a later commit may have edited), and it
    *is* the committed-ness test: if the artifacts are in the tree they are
    committed, which is the precondition ADR 0001 D4 attaches to creating a
    tag. That precondition is about the artifacts being durable in history,
    not about which kind of commit put them there -- a point the first
    implementation of the completion path got wrong by requiring a generated
    proof commit to *claim* the source, so a proof swept into history by an
    ordinary human commit was recorded as timestamped by the baseline search
    and simultaneously invisible to tag completion, permanently.

    Ancestry is not checked here. Callers reach this function for sources
    they have already placed on the lineage (the baseline, or a pending
    commit), and folding the check in would make the function answer two
    questions with one ``None``.
    """
    manifest_path = f"{proof_directory}/{source_commit_id}.json"
    manifest_text = _read_tree_text(
        cwd=cwd,
        commit=artifact_commit,
        path=manifest_path,
        process_runner=process_runner,
    )
    if manifest_text is None:
        return None
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(manifest, dict):
        return None
    if manifest.get("source_commit") != source_commit_id:
        return None

    proof_name = manifest.get("proof")
    if not isinstance(proof_name, str) or not proof_name.endswith(".ots"):
        return None
    proof_bytes = read_tree_bytes(
        cwd=cwd,
        commit=artifact_commit,
        path=f"{proof_directory}/{proof_name}",
        process_runner=process_runner,
    )
    if not proof_bytes:
        return None

    try:
        payload = build_payload(object_format, source_commit_id)
        validate_detached_proof(proof_bytes, payload)
        submitted_at, triggers = _validate_recovery_manifest(
            manifest=manifest,
            commit_id=source_commit_id,
            object_format=object_format,
        )
    except (PayloadValidationError, RecoveryValidationError, ValueError):
        return None

    return ProofEvidence(
        source_commit_id=source_commit_id,
        submitted_at=submitted_at,
        triggers=triggers,
        proof_name=proof_name,
    )


def order_commits_newest_first(
    *,
    cwd: Path,
    source_commit_id: str,
    commit_ids: Sequence[str],
    process_runner: ProcessRunner | None = None,
) -> tuple[str, ...]:
    """Order ``commit_ids`` newest-first by position in the source's history.

    Used only to break a ``submitted_at`` tie, so it costs one subprocess and
    only when a tie exists. ``git rev-list`` emits commits newest-first, so the
    earliest position is the newest commit. Anything not found in the walk
    sorts last; the caller has already established ancestry for candidates it
    accepts, so that case only arises for input this function does not select.
    """
    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    try:
        output = runner.run(["rev-list", source_commit_id]).stdout
    except GitCommandError:
        return tuple(commit_ids)
    position = {
        line.strip(): index
        for index, line in enumerate(output.splitlines())
        if line.strip()
    }
    return tuple(sorted(commit_ids, key=lambda cid: position.get(cid, len(position))))


def _find_proof_commit_baselines(
    *,
    cwd: Path,
    source_commit_id: str,
    proof_directory: str,
    object_format: str,
    process_runner: ProcessRunner | None = None,
) -> tuple[TimestampBaseline, ...]:
    """Return valid baselines derived from generated proof commits (ADR D6).

    Manifests are collected from the **frozen source commit's tree**, not from
    any single proof commit's tree. The source is the tip, so its tree holds
    exactly the artifacts that survive to the current state: the union across
    merged branches, minus anything deliberately deleted. Do not narrow this to
    the newest proof commit's tree on the theory that artifacts accumulate: that
    holds on a linear history and is false across a merge, because each branch's
    proof commit carries only its own manifests and the union exists solely in
    the merge commit, which is not itself a generated proof commit.

    The presence of a reachable generated proof commit is still required -- it
    is the ADR D6 idempotence marker -- and the newest one is recorded as
    ``proof_commit_id``. Note that it names the marker, not necessarily the
    commit whose tree carried the selected manifest: since manifests are read
    from the source tree, the two can differ across a merge.

    Manifests are batch-read in a single ``git cat-file --batch`` pass, sorted
    by ``submitted_at`` newest first, and then ancestry and payload-binding
    validation is performed only until the first candidate passes, so the cost
    does not grow with the number of stamps ever made.
    """
    proof_commits = _find_generated_proof_commit_ancestors(
        cwd=cwd,
        source_commit_id=source_commit_id,
        process_runner=process_runner,
    )
    if not proof_commits:
        return ()

    newest_proof_commit = proof_commits[0]

    manifest_paths = _list_tree_json_manifests(
        cwd=cwd,
        commit=source_commit_id,
        proof_directory=proof_directory,
        process_runner=process_runner,
    )
    if not manifest_paths:
        return ()

    specs = [f"{source_commit_id}:{path}" for path in manifest_paths]
    manifest_texts = _batch_read_tree_texts(
        cwd=cwd,
        object_specs=specs,
        process_runner=process_runner,
    )

    candidate_infos: list[tuple[datetime, str, dict]] = []
    for spec, manifest_text in manifest_texts.items():
        try:
            manifest = json.loads(manifest_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(manifest, dict):
            continue
        submitted_text = manifest.get("submitted_at")
        if not isinstance(submitted_text, str) or not submitted_text.endswith("Z"):
            continue
        try:
            submitted_at = datetime.fromisoformat(submitted_text)
        except ValueError:
            continue
        if submitted_at.tzinfo is not UTC:
            continue
        manifest_path = spec.split(":", 1)[1]
        candidate_infos.append((submitted_at, manifest_path, manifest))

    candidate_infos.sort(key=lambda item: item[0], reverse=True)

    # Walk groups of equal ``submitted_at``. An ``every_commit`` batch is
    # written by one run, so all of its manifests share a submission time and
    # sorting by time alone would leave the winner to ``ls-tree`` order -- commit
    # SHA order, which is arbitrary. ADR D6 rule 6 requires the newest claimed
    # source, so ties are resolved by position in the source's history. The
    # extra walk costs one subprocess and runs only when a group is tied.
    index = 0
    while index < len(candidate_infos):
        tied_at = candidate_infos[index][0]
        group: dict[str, dict] = {}
        while index < len(candidate_infos) and candidate_infos[index][0] == tied_at:
            manifest = candidate_infos[index][2]
            claimed = manifest.get("source_commit")
            if isinstance(claimed, str):
                group[claimed] = manifest
            index += 1
        if not group:
            continue

        ordered = (
            tuple(group)
            if len(group) == 1
            else order_commits_newest_first(
                cwd=cwd,
                source_commit_id=source_commit_id,
                commit_ids=tuple(group),
                process_runner=process_runner,
            )
        )
        for claimed in ordered:
            candidate = _validate_proof_commit_manifest_content(
                cwd=cwd,
                proof_commit_id=newest_proof_commit,
                manifest=group[claimed],
                proof_directory=proof_directory,
                object_format=object_format,
                frozen_source_id=source_commit_id,
                artifact_commit=source_commit_id,
                process_runner=process_runner,
            )
            if candidate is not None:
                return (candidate,)

    return ()


def _find_tag_baseline(
    *,
    cwd: Path,
    source_commit_id: str,
    tag_prefix: str,
    process_runner: ProcessRunner | None = None,
) -> tuple[TimestampBaseline | None, bool]:
    """Return the newest validated tag baseline and whether any tags are abandoned.

    This is the original tag-only baseline search, factored out so the combined
    baseline search can also consider proof commits.
    """
    runner = GitRunner(cwd=cwd, process_runner=process_runner)

    tags = enumerate_timestamp_tags(
        cwd=cwd, tag_prefix=tag_prefix, process_runner=process_runner
    )

    valid_tags: list[tuple[ValidatedAnnotation, TimestampTag]] = []
    candidates: list[TimestampBaseline] = []
    for tag in tags:
        try:
            annotation = validate_timestamp_tag(tag)
        except TimestampTagAnnotationError:
            continue

        valid_tags.append((annotation, tag))

        try:
            runner.run(
                [
                    "merge-base",
                    "--is-ancestor",
                    annotation.source,
                    source_commit_id,
                ]
            )
        except GitCommandError:
            continue

        candidates.append(
            TimestampBaseline(
                source_commit_id=annotation.source,
                submitted_at=annotation.submitted_at,
                triggers=annotation.triggers,
                tag_name=tag.name,
                kind=BaselineKind.TAG,
            )
        )

    if candidates:
        candidates.sort(key=_baseline_sort_key)
        return candidates[-1], False

    if valid_tags:
        return None, True

    return None, False


def find_newest_relevant_baseline(
    *,
    cwd: Path,
    source_commit_id: str,
    tag_prefix: str,
    proof_directory: str | None = None,
    object_format: str | None = None,
    process_runner: ProcessRunner | None = None,
) -> LineageBaselineResult:
    """Return the newest timestamped source commit that is an ancestor of ``source_commit_id``.

    Inspects annotated tags under ``tag_prefix`` and generated proof commits in
    the source lineage. Tag baselines and proof-commit baselines compete by
    ``submitted_at``; the newest valid one wins (ADR D6 rule 3). Tags outside
    the lineage are ignored; no tags or commits are created, moved, or deleted.
    """
    tag_baseline, has_abandoned_tags = _find_tag_baseline(
        cwd=cwd,
        source_commit_id=source_commit_id,
        tag_prefix=tag_prefix,
        process_runner=process_runner,
    )

    candidates: list[TimestampBaseline] = []
    if tag_baseline is not None:
        candidates.append(tag_baseline)

    if proof_directory is not None and object_format is not None:
        candidates.extend(
            _find_proof_commit_baselines(
                cwd=cwd,
                source_commit_id=source_commit_id,
                proof_directory=proof_directory,
                object_format=object_format,
                process_runner=process_runner,
            )
        )

    if candidates:
        candidates.sort(key=_baseline_sort_key)
        return LineageBaselineResult(
            baseline=candidates[-1],
            has_abandoned_tags=False,
        )

    return LineageBaselineResult(
        baseline=None,
        has_abandoned_tags=has_abandoned_tags,
    )


def filter_pending_meaningful_commits(
    *,
    cwd: Path,
    ref: str,
    baseline: str | None,
    proof_directory: str,
    process_runner: ProcessRunner | None = None,
) -> tuple[CommitInfo, ...]:
    """Return pending meaningful commits between ``baseline`` and ``ref``.

    The pending range is ``baseline..ref`` (baseline exclusive, ref
    inclusive). When ``baseline`` is ``None``, the entire reachable history
    of ``ref`` is enumerated.     Each enumerated commit is classified with
    :func:`is_generated_proof_commit`; generated proof commits drop out,
    everything else is returned in oldest-to-newest repository order. This
    composition deliberately does not encode policy rules — selection among
    the pending meaningful commits is the policy layer's concern (spec
    sections 14 and 17). Classification is by generated trailer only (ADR
    D3); a warning is emitted when a trailer-bearing commit also changes
    paths outside the configured proof directory. Trailer detection uses a
    single history pass so the subprocess cost is bounded by the number of
    generated proof commits in the range, not the range size.
    """

    _validate_proof_directory(proof_directory)

    runner = GitRunner(cwd=cwd, process_runner=process_runner)

    # Verify the source ref so unknown refs surface as a typed error.
    # ``rev-parse --verify`` rejects range expressions, so verify each
    # endpoint individually before delegating the range to ``git log``.
    try:
        runner.run(["rev-parse", "--verify", f"{ref}^{{commit}}"])
    except GitCommandError as exc:
        raise InvalidRepositoryStateError(
            f"cannot enumerate commits for unknown ref {ref!r}: "
            f"{exc.stderr.strip() or exc}"
        ) from exc

    if baseline is not None:
        try:
            runner.run(["rev-parse", "--verify", f"{baseline}^{{commit}}"])
        except GitCommandError as exc:
            raise InvalidRepositoryStateError(
                f"cannot enumerate commits for unknown baseline {baseline!r}: "
                f"{exc.stderr.strip() or exc}"
            ) from exc
        range_spec = f"{baseline}..{ref}"
    else:
        range_spec = ref

    output = runner.run(
        [
            "log",
            "--reverse",
            "--format=%H%x00%ct",
            range_spec,
        ]
    ).stdout

    enumerated: list[CommitInfo] = []
    for line in output.splitlines():
        if not line:
            continue
        commit_id, _, ts_text = line.partition("\x00")
        if not commit_id or not ts_text:
            raise InvalidRepositoryStateError(f"unexpected git log line: {line!r}")
        try:
            epoch = int(ts_text)
        except ValueError as exc:
            raise InvalidRepositoryStateError(
                f"unexpected committer timestamp {ts_text!r} for {commit_id}"
            ) from exc
        enumerated.append(
            CommitInfo(
                commit_id=commit_id,
                committer_time=datetime.fromtimestamp(epoch, tz=UTC),
            )
        )

    generated_ids = frozenset(
        _log_generated_trailer_commit_ids(
            cwd=cwd, range_spec=range_spec, process_runner=process_runner
        )
    )

    meaningful: list[CommitInfo] = []
    for commit in enumerated:
        if commit.commit_id in generated_ids:
            _warn_when_generated_commit_changes_outside_paths(
                cwd=cwd,
                commit=commit.commit_id,
                proof_directory=proof_directory,
                process_runner=process_runner,
            )
            continue
        meaningful.append(commit)
    return tuple(meaningful)


def create_timestamp_tag(
    *,
    cwd: Path,
    source_commit_id: str,
    tag_prefix: str,
    submitted_at: datetime,
    proof: str,
    triggers: frozenset[str] | set[str],
    producer_version: str | None = None,
    sign: bool = False,
    process_runner: ProcessRunner | None = None,
) -> str:
    """Create an immutable annotated timestamp tag pointing at ``source_commit_id``.

    The tag name is ``<tag_prefix><UTC-timestamp>/<short-sha>`` (spec
    section 12). The annotation carries the schema-1 fields and a sorted
    trigger list. The tag is created with ``git tag -a`` and points directly
    at the frozen source commit, not at ``HEAD``. Raises
    :class:`InvalidRepositoryStateError` when the Git invocation fails.

    With ``sign`` the tag is created with ``-s`` as well, and a signer that
    fails takes the whole call with it rather than yielding an unsigned tag.
    ``-s`` implies an annotated tag but does not replace ``-a``: both are
    passed so the object type does not depend on the setting.
    """

    if submitted_at.tzinfo is None or submitted_at.utcoffset() is None:
        raise ValueError("submitted_at must be a timezone-aware datetime")

    utc_submitted = submitted_at.astimezone(UTC)
    short_sha = source_commit_id[:12]
    tag_name = f"{tag_prefix}{utc_submitted.strftime('%Y%m%dT%H%M%SZ')}/{short_sha}"
    triggers_text = " ".join(sorted(triggers))
    if producer_version is None:
        from . import __version__

        producer_version = __version__
    annotation = (
        "git-ots schema: 2\n"
        f"producer: git-ots {producer_version}\n"
        f"source: {source_commit_id}\n"
        f"submitted-at: {utc_submitted.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"proof: {proof}\n"
        f"triggers: {triggers_text}\n"
    )

    runner = GitRunner(cwd=cwd, process_runner=process_runner)

    # Spec section 12: timestamp tags are immutable. Detect collisions before
    # attempting creation so an identical tag is idempotent and any mismatch
    # produces a clear error without moving or forcing the existing tag.
    tag_exists = False
    try:
        runner.run(["rev-parse", "--verify", f"refs/tags/{tag_name}"])
        tag_exists = True
    except GitCommandError:
        tag_exists = False

    if tag_exists:
        existing_target = runner.run(
            ["rev-parse", f"{tag_name}^{{commit}}"]
        ).stdout.strip()
        try:
            raw = runner.run(["cat-file", "tag", tag_name]).stdout
            _, body = raw.split("\n\n", 1)
            existing_annotation = strip_signature_block(body)
        except (GitCommandError, ValueError) as exc:
            raise InvalidRepositoryStateError(
                f"timestamp tag {tag_name!r} already exists but is not a "
                f"valid annotated tag (collision)"
            ) from exc

        if existing_target == source_commit_id and existing_annotation == annotation:
            return tag_name

        raise InvalidRepositoryStateError(
            f"timestamp tag {tag_name!r} already exists with a different "
            f"target or annotation (collision)"
        )

    tag_argv = ["tag", "-a"]
    if sign:
        tag_argv.append("-s")
    tag_argv += ["-m", annotation, tag_name, source_commit_id]

    try:
        runner.run(tag_argv)
    except GitCommandError as exc:
        if sign:
            # A signing failure keeps Git's own failure classification, so the
            # CLI reports it as a Git command failure (exit 6) with the
            # signer's stderr intact -- FS-0002 criterion 4 and its acceptance
            # criterion 6. Reclassifying it as repository state would report
            # exit 3 and describe a working signer setup as a broken
            # repository. `create_generated_proof_commit` and
            # `create_upgrade_commit` already let `GitCommandError` propagate
            # for exactly this reason; wrapping here meant one failing signer
            # produced two different exit codes depending on whether it struck
            # the tag or the proof commit.
            raise
        raise InvalidRepositoryStateError(
            f"cannot create timestamp tag {tag_name!r}: {exc.stderr.strip() or exc}"
        ) from exc

    return tag_name


def create_generated_proof_commit(
    *,
    cwd: Path,
    source_commit_ids: Sequence[str],
    proof_directory: str,
    paths: Sequence[str],
    sign: bool = False,
    process_runner: ProcessRunner | None = None,
    index_lock_retry_window: float | None = None,
) -> str:
    """Create an ``git-ots``-generated proof commit from staged proof files.

    The commit message carries the canonical ``OpenTimestamps-Generated: true``
    trailer and one ``OpenTimestamps-Source`` trailer per source commit (spec
    section 13). For a single source the subject names the short SHA; for a
    batch the subject is ``Store OpenTimestamps proofs``. Only the named
    ``paths`` are committed, so both staged and unstaged unrelated worktree
    changes are never swept in. With ``sign`` the commit is made with ``-S``;
    a signer that fails aborts the commit rather than producing an unsigned
    one.

    Returns the full object ID of the newly created commit.
    """

    if not proof_directory:
        raise ValueError("proof_directory must not be empty")
    if not source_commit_ids:
        raise ValueError("source_commit_ids must not be empty")
    if any(not sid for sid in source_commit_ids):
        raise ValueError("source_commit_ids must not contain empty values")
    if not paths:
        raise ValueError("paths must not be empty")
    if any(not p for p in paths):
        raise ValueError("paths must not contain empty values")

    if len(source_commit_ids) == 1:
        short_sha = source_commit_ids[0][:12]
        subject = f"Store OpenTimestamps proof for {short_sha}"
    else:
        subject = "Store OpenTimestamps proofs"

    body_lines = [f"{GENERATED_TRAILER_KEY}: {GENERATED_TRAILER_VALUE}"]
    for source_id in source_commit_ids:
        body_lines.append(f"{SOURCE_TRAILER_KEY}: {source_id}")
    message = subject + "\n\n" + "\n".join(body_lines) + "\n"

    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    commit_argv = ["commit", "-q"]
    if sign:
        commit_argv.append("-S")
    commit_argv += ["-m", message, "--", *paths]
    # Only index-lock contention is retried; a rejected hook or a failing
    # signer still aborts on the first attempt (see
    # `run_with_index_lock_retry`).
    run_with_index_lock_retry(runner, commit_argv, retry_window=index_lock_retry_window)

    return runner.run(["rev-parse", "HEAD"]).stdout.strip()


def create_upgrade_commit(
    *,
    cwd: Path,
    source_commit_ids: Sequence[str],
    proof_directory: str,
    paths: Sequence[str],
    sign: bool = False,
    process_runner: ProcessRunner | None = None,
    index_lock_retry_window: float | None = None,
) -> str:
    """Create an ``git-ots``-generated commit recording upgraded proof files.

    Carries the same ``OpenTimestamps-Generated: true`` trailer as a proof
    commit, because classification is by that trailer alone (ADR D3): without
    it, refreshing a proof would itself look like a meaningful change and
    trigger a fresh timestamp on the next run, which would then need its own
    upgrade -- a loop.

    Upgraded sources are named with ``OpenTimestamps-Upgraded`` rather than
    ``OpenTimestamps-Source``. The source trailer is a claim to have stamped a
    commit, and an earlier proof commit already made that claim; reusing it
    would leave two commits claiming one source, which the recovery
    diagnostics in :func:`git_ots.orchestration.run` report as an inconsistent
    state.

    Only the named ``paths`` are committed, so unrelated worktree changes are
    never swept in. ``sign`` behaves as it does for a generated proof commit.
    Returns the full object ID of the new commit.
    """

    if not proof_directory:
        raise ValueError("proof_directory must not be empty")
    if not source_commit_ids:
        raise ValueError("source_commit_ids must not be empty")
    if any(not sid for sid in source_commit_ids):
        raise ValueError("source_commit_ids must not contain empty values")
    if not paths:
        raise ValueError("paths must not be empty")
    if any(not p for p in paths):
        raise ValueError("paths must not contain empty values")

    message = _upgrade_commit_message(source_commit_ids)

    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    commit_argv = ["commit", "-q"]
    if sign:
        commit_argv.append("-S")
    commit_argv += ["-m", message, "--", *paths]
    # Only index-lock contention is retried; a rejected hook or a failing
    # signer still aborts on the first attempt (see
    # `run_with_index_lock_retry`).
    run_with_index_lock_retry(runner, commit_argv, retry_window=index_lock_retry_window)

    return runner.run(["rev-parse", "HEAD"]).stdout.strip()


@dataclass(frozen=True, slots=True)
class SquashTarget:
    """An upgrade commit that may absorb another, pinned by object ID.

    ``commit_id`` is the resolved object ID, never the symbolic ``HEAD`` the
    caller asked about. Every eligibility check is made against that ID, and
    :func:`amend_upgrade_commit` re-reads ``HEAD`` and refuses unless it is
    still the same object -- so a branch that moves between the decision and
    the rewrite aborts the rewrite instead of redirecting it onto whatever
    arrived in the meantime.
    """

    commit_id: str
    source_commit_ids: tuple[str, ...]


def find_squashable_upgrade_commit(
    *,
    cwd: Path,
    proof_directory: str,
    commit: str = "HEAD",
    sign: bool = False,
    process_runner: ProcessRunner | None = None,
) -> SquashTarget | None:
    """Return the commit a new upgrade may be folded into, or ``None``.

    ``None`` means "make an ordinary commit instead", and is the answer
    whenever ``commit`` does not have exactly the shape
    :func:`create_upgrade_commit` writes. The caller only consults this when
    ``[proof] squash_upgrade_commits`` is enabled; the decision to fold is
    the operator's, but whether folding is *safe here* is not, so every
    condition below is checked rather than assumed:

    * at least one ``OpenTimestamps-Upgraded`` trailer is present;
    * the message is byte-for-byte what :func:`_upgrade_commit_message` would
      write for exactly those sources. This is the load-bearing check, and it
      is deliberately stricter than classification by trailer (ADR D3). A
      trailer-bearing commit is not necessarily one this tool wrote as it now
      stands: ``git rebase -i`` squashing an upgrade commit into a real one
      concatenates the two messages, so the result carries the generated and
      upgraded trailers *and* the operator's own subject and body. Amending
      that would preserve its tree but replace its message with this tool's,
      destroying the only copy of what the operator wrote. Requiring the
      whole message also subsumes the trailers this tool's own message
      always has and never has -- ``OpenTimestamps-Generated: true`` present,
      ``OpenTimestamps-Source`` absent -- so a proof commit, which dates a
      submission and is the recovery artifact §22.3 reads back, is never a
      target either;
    * the commit changes nothing outside ``proof_directory``. §13 asks for a
      warning when a trailer-bearing commit reaches outside it; here the
      commit is about to be rewritten rather than merely classified, so the
      same condition declines instead of warning;
    * the commit has exactly one parent, so amending cannot reshape a merge;
    * amending will not silently drop a signature the commit already carries
      -- see :func:`_amend_would_drop_a_signature`;
    * the commit is not reachable from any remote-tracking ref. Amending a
      published commit produces a branch that no longer fast-forwards, which
      turns a scheduled upgrade into a failing push. This is the one
      declined case an operator asked for and did not get, so it is logged.

    What the message check is *not* is a proof of authorship. It establishes
    shape, not provenance: a commit somebody hand-wrote to be byte-identical,
    touching only the proof directory, with one parent and unpushed, is
    indistinguishable from one this tool wrote and is treated as one. Git
    records no durable "this tool made this object" marker that a rewrite
    could rely on, so shape is the strongest available test; every condition
    above exists to keep the set of commits with that shape to ones where
    amending loses nothing.

    Reachability is judged against this repository's own ``refs/remotes/*``,
    which is knowledge of what *this clone* has seen pushed, not proof that a
    commit is unpublished -- see :func:`is_published`. A local tag or a second
    local branch pointing at ``commit`` is not a decline: amending moves only
    the current branch, and those refs keep the old object, so nothing is lost
    -- but the two do then diverge.
    """

    _validate_proof_directory(proof_directory)

    # Resolved once, and every check below is made against this ID rather
    # than against the symbolic name: a caller passing "HEAD" must not have
    # one condition answered about one commit and another about its
    # successor. `amend_upgrade_commit` is handed the same ID and refuses if
    # HEAD has left it.
    try:
        commit_id = (
            GitRunner(cwd=cwd, process_runner=process_runner)
            .run(["rev-parse", "--verify", f"{commit}^{{commit}}"])
            .stdout.strip()
        )
    except GitCommandError as exc:
        raise InvalidRepositoryStateError(
            f"cannot resolve {commit!r} while looking for a squash target: "
            f"{exc.stderr.strip() or exc}"
        ) from exc

    message, trailer_lines = _read_message_and_trailers(
        cwd=cwd, commit=commit_id, process_runner=process_runner
    )
    upgraded = _trailer_values(trailer_lines, UPGRADED_TRAILER_KEY)
    if not upgraded:
        return None
    # Trailing newlines are the one difference that is not a difference:
    # `--format=%B` emits the body plus a newline of its own, and Git's own
    # `-m` cleanup already collapsed any trailing blank lines before the
    # commit was written, so neither side can carry a meaningful one.
    if message.rstrip("\n") != _upgrade_commit_message(upgraded).rstrip("\n"):
        # Below warning level: most mismatches are simply commits that are
        # not this tool's, and an operator did not ask to hear about those.
        # The case worth surfacing under --verbose is the repository whose
        # commit-msg hook or commit.cleanup setting reshapes every message
        # as it is written, so that no upgrade commit is ever a target and
        # the setting silently never does anything.
        _logger.info(
            "Not squashing into commit %s: it carries OpenTimestamps-Upgraded "
            "trailers, but its message is not the one this tool writes for "
            "them. A commit-msg hook or commit.cleanup setting that alters "
            "messages makes every upgrade commit ineligible.",
            commit_id[:12],
        )
        return None

    changed = _list_changed_paths(
        cwd=cwd, commit=commit_id, process_runner=process_runner
    )
    prefix = f"{proof_directory}/"
    if any(not path.startswith(prefix) for path in changed):
        return None

    if (
        len(
            get_commit_parents(cwd=cwd, commit=commit_id, process_runner=process_runner)
        )
        != 1
    ):
        return None

    if _amend_would_drop_a_signature(
        cwd=cwd, commit=commit_id, sign=sign, process_runner=process_runner
    ):
        _logger.warning(
            "Not squashing into upgrade commit %s: it is signed, and this "
            "configuration would replace it with an unsigned commit. "
            "The upgrade goes into a new commit instead.",
            commit_id[:12],
        )
        return None

    if is_published(cwd=cwd, commit=commit_id, process_runner=process_runner):
        _logger.warning(
            "Not squashing into upgrade commit %s: it is already reachable "
            "from a remote-tracking ref, and amending it would leave this "
            "branch unable to fast-forward. The upgrade goes into a new "
            "commit instead.",
            commit_id[:12],
        )
        return None

    return SquashTarget(commit_id=commit_id, source_commit_ids=upgraded)


def _commit_is_signed(
    *,
    cwd: Path,
    commit: str,
    process_runner: ProcessRunner | None = None,
) -> bool:
    """Return whether ``commit`` carries a signature header.

    Reads the object's headers directly rather than asking for ``%G?``:
    ``%G?`` reports the *verification* result, which needs a working GnuPG and
    the signer's public key, and answers "no good signature" for a signature
    this machine merely cannot check. The question here is whether a
    signature exists to be destroyed, which is a property of the object.
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    raw = runner.run(["cat-file", "commit", commit]).stdout
    headers = raw.split("\n\n", 1)[0]
    return any(
        line.startswith(("gpgsig ", "gpgsig-sha256 ")) for line in headers.splitlines()
    )


def _amend_would_drop_a_signature(
    *,
    cwd: Path,
    commit: str,
    sign: bool = False,
    process_runner: ProcessRunner | None = None,
) -> bool:
    """Return whether amending ``commit`` would leave it unsigned.

    A squash replaces the commit object, so its signature is not carried over
    -- it is recreated, or it is gone. ``sign`` is this tool's own
    ``[git] signing = required``; ambient ``commit.gpgsign`` signs the amended
    commit just as well, and is consulted because ``inherit`` deliberately
    passes no flag of its own (see :class:`~git_ots.config.GitConfig`). Only
    when neither applies does the rewrite actually lose something, and only
    then is it worth declining a fold the operator asked for.
    """

    if not _commit_is_signed(cwd=cwd, commit=commit, process_runner=process_runner):
        return False
    if sign:
        return False

    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    try:
        ambient = runner.run(
            ["config", "--type=bool", "--get", "commit.gpgsign"]
        ).stdout.strip()
    except GitCommandError as exc:
        if exc.exit_code == 1:  # unset in every scope
            return True
        raise
    return ambient != "true"


def is_published(
    *,
    cwd: Path,
    commit: str,
    process_runner: ProcessRunner | None = None,
) -> bool:
    """Return True when ``commit`` is reachable from any remote-tracking ref.

    ``git rev-list --max-count=1 <commit> --not --remotes`` lists the newest
    commit reachable from ``commit`` but from no ``refs/remotes/*`` ref; empty
    output therefore means ``commit`` itself is already in one of them.

    A True answer is reliable; a False one is not proof of anything. This
    reads cached remote-tracking refs, so it reports what *this clone has
    seen*, and a commit can be published without that showing here: pushed
    from another clone, pushed to a URL that leaves no tracking ref, sitting
    behind a pruned or deleted tracking ref, or simply pushed since the last
    fetch. No purely local query can do better -- non-publication is not a
    fact a repository holds -- so callers must treat False as "not known to be
    published" and size the consequences accordingly, never as a guarantee
    that rewriting is invisible to others.
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)
    output = runner.run(
        ["rev-list", "--max-count=1", commit, "--not", "--remotes"]
    ).stdout.strip()
    return output == ""


def amend_upgrade_commit(
    *,
    cwd: Path,
    expected_head: str,
    previous_source_commit_ids: Sequence[str],
    source_commit_ids: Sequence[str],
    proof_directory: str,
    paths: Sequence[str],
    sign: bool = False,
    process_runner: ProcessRunner | None = None,
    index_lock_retry_window: float | None = None,
) -> str:
    """Fold newly upgraded proofs into the upgrade commit already at ``HEAD``.

    ``expected_head`` is the object ID
    :func:`find_squashable_upgrade_commit` approved. ``HEAD`` is re-read
    immediately before the rewrite and must still be that object, or the call
    raises :class:`InvalidRepositoryStateError` without touching anything.
    This matters because ``git commit --amend`` names no commit: it rewrites
    whatever ``HEAD`` points at *now*. Every eligibility check was made
    against one specific object, and :class:`RepositoryLock` excludes only
    other ``git-ots`` invocations -- an ordinary ``git commit`` in another
    terminal can still advance the branch in between. Without this check the
    rewrite would silently land on that new, entirely unvetted commit and
    replace its message.

    The re-read narrows the window rather than closing it: nothing Git offers
    makes "amend, but only if HEAD is still X" a single atomic operation
    while also running hooks and honoring signing configuration the way an
    ordinary commit does. HEAD is checked again before every lock retry,
    but a concurrent writer can still move it between the check and Git
    reading HEAD. Callers must avoid concurrent repository mutations.

    The replacement message names the union of
    ``previous_source_commit_ids`` -- what the commit being amended already
    claimed, as read by :func:`find_squashable_upgrade_commit` -- and
    ``source_commit_ids``, in that order and deduplicated. The union is the
    point of taking both: a squashed commit that named only the new sources
    would silently drop the record that the others were ever refreshed.

    ``git commit --amend -- <paths>`` rebuilds the commit from ``HEAD``'s own
    tree with only the named paths updated, so proofs an earlier upgrade
    committed survive untouched and unrelated worktree or index changes are no
    more included than :func:`create_upgrade_commit` includes them. The author
    date carries over from the commit being amended and the committer date
    becomes now, so the squashed commit spans from the first upgrade it
    absorbed to the most recent.

    Returns the full object ID of the amended commit, which is necessarily a
    new one: amending rewrites the object.
    """

    if not proof_directory:
        raise ValueError("proof_directory must not be empty")
    if not expected_head:
        raise ValueError("expected_head must not be empty")
    if not source_commit_ids:
        raise ValueError("source_commit_ids must not be empty")
    if any(not sid for sid in source_commit_ids):
        raise ValueError("source_commit_ids must not contain empty values")
    if any(not sid for sid in previous_source_commit_ids):
        raise ValueError("previous_source_commit_ids must not contain empty values")
    if not paths:
        raise ValueError("paths must not be empty")
    if any(not p for p in paths):
        raise ValueError("paths must not contain empty values")

    merged = list(dict.fromkeys([*previous_source_commit_ids, *source_commit_ids]))
    message = _upgrade_commit_message(merged)

    runner = GitRunner(cwd=cwd, process_runner=process_runner)

    def check_head() -> None:
        current_head = runner.run(
            ["rev-parse", "--verify", "HEAD^{commit}"]
        ).stdout.strip()
        if current_head != expected_head:
            raise InvalidRepositoryStateError(
                f"refusing to amend: HEAD moved from {expected_head[:12]} to "
                f"{current_head[:12]} after the squash target was approved; the "
                f"upgraded proofs are written and staged, and the next run records "
                f"them -- unless ots.requireCleanWorktree is set, in which case "
                f"commit or stash them first"
            )

    commit_argv = ["commit", "-q", "--amend"]
    if sign:
        commit_argv.append("-S")
    commit_argv += ["-m", message, "--", *paths]
    run_with_index_lock_retry(
        runner,
        commit_argv,
        retry_window=index_lock_retry_window,
        before_attempt=check_head,
    )

    return runner.run(["rev-parse", "HEAD"]).stdout.strip()


def _upgrade_commit_message(source_commit_ids: Sequence[str]) -> str:
    """Build the message shared by a created and an amended upgrade commit.

    One function rather than two so a squashed commit is textually
    indistinguishable from the single commit the same upgrades would have
    produced had they arrived together -- and so the next squash reads its
    predecessor's trailers back with the same parser that wrote them.
    """

    if len(source_commit_ids) == 1:
        subject = f"Upgrade OpenTimestamps proof for {source_commit_ids[0][:12]}"
    else:
        subject = f"Upgrade {len(source_commit_ids)} OpenTimestamps proofs"

    body_lines = [f"{GENERATED_TRAILER_KEY}: {GENERATED_TRAILER_VALUE}"]
    for source_id in source_commit_ids:
        body_lines.append(f"{UPGRADED_TRAILER_KEY}: {source_id}")
    return subject + "\n\n" + "\n".join(body_lines) + "\n"
