# Installation and development

## Prerequisites

| Requirement | Needed for | Notes |
|---|---|---|
| [`uv`](https://docs.astral.sh/uv/) | everything | Drives installation, the virtualenv, tests, and builds. |
| Python 3.12, 3.13, or 3.14 | everything | Supported on Linux and macOS; `uv` provisions a matching interpreter automatically. |
| `git`, on `PATH` | every command | `git-ots` shells out to it rather than using a Git library. |
| An `ots` executable | `run`, `upgrade`, and `verify` | The OpenTimestamps client. See [OpenTimestamps client](#opentimestamps-client) — `make install` installs it for you. |
| A Bitcoin node | `git ots verify` | The client fetches the attested block header from it. See [Verifying stored proofs](proof-lifecycle.md#verifying-stored-proofs). |

CI pins both `uv` and its setup action to reviewed releases for reproducibility
and supply-chain integrity. Local development may use a newer `uv`; dependency
resolution remains fixed by `uv.lock`, and CI uses `--frozen` so an out-of-date
lockfile fails rather than being rewritten.

`git-ots` itself has **no third-party runtime dependencies** — `dependencies` in
`pyproject.toml` is empty and the package uses only the standard library. The
development extras (`pytest`, `ruff`) are installed by `uv sync` and are not
needed to run the tool.

To check a machine that is meant to run real timestamps:

```bash
make prereqs
```

It reports `uv`, `git`, `git-ots`, and the effective OpenTimestamps client
configuration (`ots.command`, resolved from Git config), and exits 1 if any
of them is missing.

## Installation

### As a user-wide executable

For normal use — including cron jobs, `systemd` timers, and post-push hooks —
install `git-ots` as a standalone executable on `PATH`:

```bash
make install
```

This runs `uv tool install --force .`, which places a `git-ots` executable in
`~/.local/bin` with its own isolated environment. If the command is not found
afterwards, add that directory to `PATH` with `uv tool update-shell`. Verify
with `make where`, and remove it again with `make uninstall`.

The installed executable is a snapshot of the working tree at install time.
Re-run `make install` after changing the source, or use `make install-dev`
(editable install) while developing.

For a released version without PyPI, install the tagged GitHub source directly:

```bash
pipx install "git+https://github.com/rotheric/git-ots.git@v0.0.1"
```

When the package is available on PyPI, `pipx install git-ots` installs the same
release.

The installed executable is named `git-ots`. Because it lives on `PATH`, Git
automatically dispatches `git ots ...` to it; the documentation therefore uses
the more natural Git subcommand spelling.

### When a reinstall appears not to take effect

There are two causes, and `make install` now detects both.

**A cached build.** Repeated source installs within one release keep the same
version, so `uv` may reuse a previously built wheel and install stale code — the giveaway is
a `uv tool install` that prints no `Building git-ots` line. `make install`
passes `--reinstall` to prevent this, and afterwards compares the installed
package against `src/git_ots`, reporting any file that differs:

```console
WARNING: the installed copy does NOT match src/git_ots.
uv served a cached build instead of rebuilding. Differences:
  Files src/git_ots/cli.py and /home/you/.local/share/uv/tools/... differ

Retry with:  uv cache clean git-ots && make install
```

**A shadowing executable.** `make install` replaces only the `uv` tool copy. If
another `git-ots` sits earlier on `PATH` it keeps winning. The usual culprit is
this project's own `.venv/bin/git-ots`, which `uv sync` creates and which an
activated virtualenv — or any `uv run` — places first.

`make where` lists every copy on `PATH`:

```console
$ make where
/path/to/repo/.venv/bin/git-ots   <- wins
/home/you/.local/bin/git-ots   (shadowed)
```

Deactivate the virtualenv to use the installed copy. If the path was cached by
your shell within the same session, run `hash -r`.

### From a project checkout

To work on the project itself, create the local virtual environment instead:

```bash
uv sync
```

Then run the command through `uv`, with no installation step:

```bash
uv run git ots status
uv run git ots run --dry-run
uv run git ots run
```

The `git-ots` console script is also directly available inside an activated
project virtual environment.

Report the installed version with `git ots --version`.

### OpenTimestamps client

The `run` command shells out to an `ots` executable. `make install` installs one
for you when it is missing, pinned to the version the integration job tests
against; override with `make install OTS_CLIENT=opentimestamps-client==X.Y.Z`.
To install it separately:

```bash
make install-client
```

`status` and `run --dry-run` never invoke it. If you keep the client elsewhere,
point `ots.command` at it instead. `make prereqs` reports whether
everything a real run needs is present.

`status` probes the configured client for the `stamp`, `upgrade`, and `verify`
capabilities and reports its version. Version 0.7.2 is the tested baseline; a
different version that exposes the required interface is reported as
compatible but untested rather than rejected.

## Make targets

`make` with no arguments, or `make help`, lists every target with its purpose.
The full set:

**Development**

| Target | Purpose |
|--------|---------|
| `make sync` | Create/refresh the project virtualenv (`.venv`) with dev dependencies |
| `make lint` | Run `ruff` checks over `src` and `tests` |
| `make format` | Apply `ruff` formatting |
| `make test` | Run the offline unit test suite |
| `make test-integration` | Run the tests that need network access and the real `ots` CLI |
| `make check` | The full local quality gate: `lint` then `test` |
| `make clean` | Remove build artifacts, tool caches, and `__pycache__` directories |

**Packaging and installation**

| Target | Purpose |
|--------|---------|
| `make install` | Install `git-ots` **and** the OpenTimestamps client it needs |
| `make install-dev` | Install user-wide in editable mode, tracking this working tree |
| `make install-client` | Install the OpenTimestamps client, unless one is already present |
| `make uninstall` | Remove the user-wide installation |
| `make verify-install` | Check the installed copy matches `src/git_ots` and is not shadowed |
| `make check-shadowing` | Warn if another `git-ots` earlier on `PATH` hides the installed one |
| `make where` | Show every `git-ots` on `PATH` and flag shadowing |
| `make build` | Build the wheel and sdist into `dist/` |

`verify-install` and `check-shadowing` run automatically at the end of
`make install` and `make install-dev`; they are exposed separately so a
suspected stale or shadowed install can be re-checked without reinstalling.
Both are read-only. See
[When a reinstall appears not to take effect](#when-a-reinstall-appears-not-to-take-effect)
for what they are guarding against.

**Running against this repository**

| Target | Purpose |
|--------|---------|
| `make prereqs` | Check everything a real run needs (uv, git, client, config) |
| `make run` | Check prerequisites, then timestamp this repository |
| `make dry-run` | Show what a run would do; needs no OpenTimestamps client |

`make run` and `make dry-run` act on *this* checkout, using its own Git
configuration (`ots.*` keys set with `git config`). They are a convenience
for operating the repository, not part of the build.

**Variables**

Override on the command line, for example
`make install OTS_CLIENT=opentimestamps-client==0.7.1`.

| Variable | Default | Meaning |
|----------|---------|---------|
| `UV` | `uv` | The `uv` executable to drive everything with |
| `OTS` | `ots` | The OpenTimestamps client `install-client` and `prereqs` look for |
| `OTS_CLIENT` | `opentimestamps-client==0.7.2` | The pinned client `make install` installs when none is present |

`OTS_CLIENT` is pinned to the version the integration job tests against. Note
that `make prereqs` prefers `ots.command` from Git config when it is set,
since that is what an actual run will invoke.
