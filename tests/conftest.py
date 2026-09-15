"""Shared test fixtures.

The network guard below is the important one. Several commands now contact
calendars by default, so a test that forgets to inject a fetcher would quietly
make real HTTP requests -- slow, flaky, and dependent on someone else's
servers. Blocking the transport makes that a loud failure instead.

The git-config isolation fixture is the other important one. The upcoming
epic makes git-ots read `ots.*` configuration via `git config`, from every
Git scope including global (`~/.gitconfig`) and system (`/etc/gitconfig`).
Without isolation, a temp repo built the way tests build one still reads the
*developer's real* `~/.gitconfig` -- so a value like `ots.maxAge` sitting in
someone's laptop config would silently change what a test observes, and the
same test would behave differently in CI (no such file) than on that
developer's machine, in the very component that decides when a repository
gets timestamped. Pinning every git-config scope to an empty, per-test
location makes the suite's git config identical everywhere it runs.
"""

from __future__ import annotations

import os
import urllib.request
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that tries to open a URL.

    Tests that need calendar behavior inject a fake fetcher or opener; nothing
    in the suite should reach the real transport.
    """

    def _blocked(*args, **kwargs):
        target = args[0] if args else "<unknown>"
        url = getattr(target, "full_url", target)
        raise AssertionError(
            f"test attempted a network request to {url!r}; inject a fake "
            "fetcher or opener instead"
        )

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)


@pytest.fixture(autouse=True)
def _isolated_git_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Cut every test off from the invoking machine's real git configuration.

    Git reads configuration from several scopes -- system, global, local,
    worktree, and command -- and merges them. Nothing in this suite should
    ever see the system or global scope: a real `~/.gitconfig` carrying,
    say, `ots.maxAge` would make tests pass or fail differently depending on
    whose machine (or which CI runner) ran them, in exactly the component
    that decides when a repository gets timestamped. Each test gets its own
    fresh HOME, its own fresh XDG config directory, and its own fresh, empty
    global-config file so no test's git-config state can leak into
    another's.

    All artifacts this fixture creates live under one reserved subdirectory
    of `tmp_path` (rather than directly in `tmp_path`), so a test that later
    does `git init` at `tmp_path` itself never sees them as stray untracked
    files.

    Note on version coverage: `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM`
    only exist from git 2.32 onward. On older git (down to this project's
    2.26 minimum) those two env vars are silently ignored, and isolation on
    those versions is carried entirely by `HOME` (global scope),
    `XDG_CONFIG_HOME` (global scope, `$XDG_CONFIG_HOME/git/config`), and
    `GIT_CONFIG_NOSYSTEM` (system scope).
    """
    root = tmp_path / "_git_isolation"
    root.mkdir(exist_ok=True)

    home = root / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    # XDG_CONFIG_HOME is a global-scope source in its own right: git reads
    # `$XDG_CONFIG_HOME/git/config` there. Pinning it to an empty, isolated
    # directory closes that leak on every supported git version, including
    # 2.26-2.31 where GIT_CONFIG_GLOBAL does not yet exist to override it.
    xdg_config_home = root / "xdg"
    xdg_config_home.mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config_home))

    # Load-bearing: leaving GIT_CONFIG_GLOBAL unset would let git fall
    # through to the real default `~/.gitconfig` path. It must point at a
    # real, empty, existing file instead. (git >= 2.32 only; see the
    # version-coverage note above.)
    global_config = root / "gitconfig-global"
    global_config.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    # GIT_CONFIG_NOSYSTEM suppresses the system scope. Git parses it as a
    # boolean via git_env_bool, not by mere presence: "1"/"yes" suppress the
    # scope, but "0"/"false"/"" would leave it fully readable. "1" is used
    # here because it is truthy, not because any non-empty value works.
    # GIT_CONFIG_SYSTEM=/dev/null is defense-in-depth alongside it for git
    # >= 2.32, which introduced GIT_CONFIG_SYSTEM long after
    # GIT_CONFIG_NOSYSTEM already existed.
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")

    # Git also merges in a "command" scope built from the environment
    # itself -- GIT_CONFIG_PARAMETERS (as used by `git -c`) and the
    # GIT_CONFIG_COUNT / GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n family. This
    # is the narrowest scope of all and beats every file-based scope above
    # it, so it must be scrubbed from the ambient environment or a stray
    # value there would still leak into every subprocess this suite spawns.
    monkeypatch.delenv("GIT_CONFIG_PARAMETERS", raising=False)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    for key in list(os.environ):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            monkeypatch.delenv(key, raising=False)

    # GIT_CONFIG (the legacy, deprecated single-file override, distinct
    # from GIT_CONFIG_GLOBAL) is still honored by `git config` on every
    # supported version. Nobody reaches for it deliberately -- that's
    # exactly why it must be scrubbed rather than trusted to stay unset.
    monkeypatch.delenv("GIT_CONFIG", raising=False)

    # Repository-discovery variables are inherited from the ambient
    # environment by default. GIT_DIR in particular overrides `cwd=`
    # entirely, so an unscrubbed value here (e.g. from a git hook or `git
    # rebase -x` invoking the test runner) would silently point a test's
    # git subprocess at the invoking repository instead of its own temp
    # repo. GIT_TEMPLATE_DIR is scrubbed alongside them: `git init` copies
    # a `config` file out of it straight into the new repository's LOCAL
    # scope, so a developer with it exported would get an ambient key
    # pre-planted into every test repo `_init_repo` builds.
    # GIT_ALTERNATE_OBJECT_DIRECTORIES and GIT_NAMESPACE are scrubbed here
    # for symmetry with the rest of this discovery-variable group; neither
    # can inject into any `git config` scope this suite isolates -- they
    # affect object lookup and ref namespacing, not configuration -- so
    # this is tidiness rather than a config-isolation fix.
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_COMMON_DIR",
        "GIT_TEMPLATE_DIR",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
    ):
        monkeypatch.delenv(key, raising=False)

    # GIT_CEILING_DIRECTORIES is *set*, not merely scrubbed: an unset value
    # lets git's repository discovery walk upward from `cwd=` past the
    # per-test sandbox to whatever real repository happens to sit above
    # TMPDIR (routine in container and project-local-TMPDIR setups),
    # reading that ancestor's LOCAL scope -- the one scope tests most rely
    # on being empty by default. Confining discovery to `tmp_path` closes
    # that regardless of where TMPDIR itself lives.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    # Git identity and trace variables are inherited from the ambient
    # environment by default. GIT_AUTHOR_*/GIT_COMMITTER_* would silently
    # override the `user.name`/`user.email` that `_init_repo` sets in each
    # test repo's LOCAL scope, making commit identity depend on the
    # invoking developer's shell rather than the test; GIT_TRACE* would
    # leak arbitrary diagnostic output onto subprocess stderr.
    for key in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_TRACE",
        "GIT_TRACE2",
        "GIT_TRACE2_EVENT",
        "GIT_TRACE2_PERF",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def isolated_home(_isolated_git_config: None, tmp_path: Path) -> Path:
    """Return the `_isolated_git_config` fixture's substitute HOME, guarded.

    Non-autouse companion for tests that need to plant a file (e.g. a
    stray `.gitconfig`) directly at the conventional global-config
    location for the *current test's* isolated HOME. Asserts the ambient
    `HOME` really is this directory before handing it back, so a test can
    never write into a developer's real home directory if the autouse
    fixture is absent, disabled, or its internal layout changes -- one
    guard, shared by every canary that needs it, rather than a copy each
    test could independently forget or drift out of sync with.

    Depends explicitly on `_isolated_git_config` rather than relying on
    pytest's same-scope autouse-before-non-autouse ordering: that ordering
    is real but implicit, and this fixture's guard assertion only holds
    once the isolation fixture has actually run.
    """
    home = tmp_path / "_git_isolation" / "home"
    assert Path(os.environ["HOME"]) == home, (
        "HOME does not match the isolation fixture's expected directory; "
        "refusing to write a .gitconfig, since it may be the real one"
    )
    return home


@pytest.fixture
def isolated_non_repo_cwd(_isolated_git_config: None, tmp_path: Path) -> Path:
    """Return a dedicated non-repo `cwd=` for git subprocesses under test.

    Lives under the `_git_isolation/` subdirectory reserved by
    `_isolated_git_config`, not directly under `tmp_path`, so a test that
    later does `git init` at `tmp_path` itself never sees this as a stray
    untracked directory. Must NOT itself be (or sit inside) a git
    repository, or a `git config` lookup run with `cwd=` here would also
    pick up that repository's own LOCAL scope.

    Depends explicitly on `_isolated_git_config` rather than relying on
    pytest's same-scope autouse-before-non-autouse ordering: that ordering
    is real but implicit, and the `_git_isolation/` subdirectory this
    fixture reuses is created by the isolation fixture first.
    """
    cwd = tmp_path / "_git_isolation" / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    return cwd
