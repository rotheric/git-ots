"""Tests that prove the autouse git-config isolation fixture actually isolates.

The upcoming epic makes git-ots read `ots.*` configuration via `git config`
from every scope, including global (`~/.gitconfig`) and system
(`/etc/gitconfig`). `conftest.py`'s `_isolated_git_config` fixture exists to
cut every test off from the invoking machine's real git configuration, so
that a developer's `~/.gitconfig` (which might carry, say, `ots.maxAge`) can
never leak into a test result. These tests exist to prove that fixture
genuinely works and genuinely matters, not merely that a handful of env vars
are set:

- Test A proves AC-STRUCT-1: a real `git config` subprocess, run under the
  fixture, cannot see a planted `ots.*` key sitting at the conventional
  global-config location -- with a negative control proving the planted key
  really would leak without the fixture's overrides.
- Test B proves AC-STRUCT-2: a previously environment-replacing call site
  (`tests/test_baseline_proof_commit.py`'s `_commit()` helper) now hands the
  fixture's isolation variables through to the real subprocess it spawns,
  rather than silently dropping them the way a bare `{"GIT_COMMITTER_DATE":
  ...}` dict literal used to. It also has a behavioural leg: the exact
  captured `env` dict, used to run a real `git config` lookup against a
  planted `ots.*` key, must genuinely fail to resolve it -- not merely
  contain the isolation variable names by name, which a leak through
  Git's environment-injected "command" scope (`GIT_CONFIG_PARAMETERS`,
  `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`) could satisfy
  while the key still resolves.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.test_baseline_proof_commit import _commit, _init_repo


def test_a_real_git_subprocess_cannot_see_a_planted_ots_key_at_the_conventional_global_config_location(
    isolated_home: Path,
    isolated_non_repo_cwd: Path,
) -> None:
    """AC-STRUCT-1: the isolation fixture hides a stray real ~/.gitconfig.

    Plant a `.gitconfig` carrying `ots.maxAge` at the conventional global
    config location for the fixture's substitute HOME -- modeling a
    developer's real `~/.gitconfig` carrying their own `ots.maxAge`. A real
    `git config` subprocess run under the current, fixture-isolated
    environment must not see it, because GIT_CONFIG_GLOBAL points elsewhere.

    A negative control -- the same planted file, but with the fixture's
    overrides stripped out of the child environment -- proves the planted
    key is a genuine trap that leaks in the absence of isolation, and that
    the fixture's env vars are what stands between the suite and that leak.

    Both subprocess calls run with `cwd=` pointed at a dedicated non-repo
    temp directory, not this test's own working directory -- otherwise they
    would also pick up *this repository's* local `.git/config` scope, which
    would make the isolation assertion below break the moment anyone sets
    an `ots.*` key in this repo (exactly the capability this epic ships).

    The `isolated_home` fixture carries the "is this really the throwaway
    HOME" guard -- see its docstring in `conftest.py`.
    """
    stray_global_config = isolated_home / ".gitconfig"
    stray_global_config.write_text("[ots]\n    maxAge = 999\n")

    isolated = subprocess.run(
        ["git", "config", "-z", "--get-regexp", "^ots\\."],
        cwd=isolated_non_repo_cwd,
        env=os.environ.copy(),
        capture_output=True,
        check=False,
    )
    assert isolated.returncode == 1
    assert isolated.stdout == b""

    unisolated_env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"}
    }
    leaked = subprocess.run(
        ["git", "config", "-z", "--get-regexp", "^ots\\."],
        cwd=isolated_non_repo_cwd,
        env=unisolated_env,
        capture_output=True,
        check=False,
    )
    assert leaked.returncode == 0
    assert b"ots.maxage" in leaked.stdout.lower()


def test_b_the_fixed_commit_helper_hands_the_isolation_variables_through_to_the_real_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_home: Path,
    isolated_non_repo_cwd: Path,
) -> None:
    """AC-STRUCT-2: a previously environment-replacing call site now extends.

    `tests/test_baseline_proof_commit.py::_commit()` used to build its `env`
    kwarg as a bare `{"GIT_COMMITTER_DATE": ...}` dict literal, which
    REPLACES rather than extends the child environment -- silently dropping
    whatever isolation variables the autouse fixture set (and PATH, etc).
    Wrap the real `subprocess.run` to capture the exact `env` dict handed to
    the OS-level commit call `_commit(..., date=...)` makes, and assert it
    still carries GIT_CONFIG_GLOBAL and HOME through, then prove -- via a
    real planted `ots.*` key and a real `git config` lookup run with that
    exact captured env -- that the isolation those variables provide
    genuinely holds and not merely that the variable names are present.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("hello\n")

    real_run = subprocess.run
    captured_env: dict[str, list[dict[str, str] | None]] = {"calls": []}

    def _spy(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        if argv and "commit" in argv and "-m" in argv:
            captured_env["calls"].append(kwargs.get("env"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _spy)

    _commit(
        repo,
        paths=["README.md"],
        message="Initial commit",
        date=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert len(captured_env["calls"]) == 1
    env = captured_env["calls"][0]
    assert env is not None
    assert env.get("GIT_CONFIG_GLOBAL") == os.environ["GIT_CONFIG_GLOBAL"]
    assert env.get("HOME") == os.environ["HOME"]

    # Behavioural leg: plumbing alone is not proof. Plant an `ots.*` key at
    # the conventional global-config location for this exact captured env's
    # HOME, then run a real `git config` lookup with that exact env and
    # confirm the key genuinely does not resolve.
    #
    # Write through `isolated_home`, not `Path(env["HOME"])`, so the write
    # is guarded structurally by that fixture's assertion rather than
    # incidentally by whatever happens to raise first if the isolation
    # fixture is ever absent or its layout drifts (see the fixture's
    # docstring in conftest.py). The assertion above already establishes
    # env["HOME"] == os.environ["HOME"], which `isolated_home` in turn
    # guarantees equals this exact directory.
    assert env["HOME"] == str(isolated_home)
    stray_global_config = isolated_home / ".gitconfig"
    stray_global_config.write_text("[ots]\n    maxAge = 999\n")

    lookup = subprocess.run(
        ["git", "config", "-z", "--get-regexp", "^ots\\."],
        cwd=isolated_non_repo_cwd,
        env=env,
        capture_output=True,
        check=False,
    )
    assert lookup.returncode == 1
    assert lookup.stdout == b""
