# git-ots

[![Verify](https://github.com/rotheric/git-ots/actions/workflows/verify.yml/badge.svg?branch=master)](https://github.com/rotheric/git-ots/actions/workflows/verify.yml)
![Python 3.12–3.14](https://img.shields.io/badge/python-3.12%E2%80%933.14-blue)

`git-ots` anchors meaningful Git repository states using [OpenTimestamps](https://opentimestamps.org/).

It periodically inspects a Git repository, identifies source commits that represent real changes, and submits the commit identifier to OpenTimestamps. The resulting detached proof and a small JSON manifest are stored in the repository, committed automatically, and the source commit is tagged with an immutable annotated Git tag.

The CLI is invocation-driven: it does not run a daemon itself. Run it from `cron`, a `systemd` timer, a CI pipeline, or a post-push hook.

## What is timestamped

The exact subject submitted to OpenTimestamps is the full Git commit identifier, encoded as a canonical payload:

```text
git:<object-format>:<full-commit-id>\n
```

Examples:

```text
git:sha1:0123456789abcdef0123456789abcdef01234567
```

or, for SHA-256 repositories:

```text
git:sha256:<64-character-hash>
```

The payload is a single line terminated by exactly one LF byte. This representation prevents ambiguity between SHA-1 and SHA-256 repositories, and between textual and binary commit IDs.

## Prerequisites

| Requirement | Needed for | Notes |
|---|---|---|
| [`uv`](https://docs.astral.sh/uv/) | everything | Drives installation, the virtualenv, tests, and builds. |
| Python 3.12, 3.13, or 3.14 | everything | Supported on Linux and macOS; `uv` provisions a matching interpreter automatically. |
| `git`, on `PATH` | every command | `git-ots` shells out to it rather than using a Git library. |
| An `ots` executable | `run`, `upgrade`, and `verify` | The OpenTimestamps client. See [OpenTimestamps client](#opentimestamps-client) — `make install` installs it for you. |
| A Bitcoin node | `git-ots verify` | The client fetches the attested block header from it. See [Verifying stored proofs](#verifying-stored-proofs). |

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

Because the executable is named `git-ots` and lives on `PATH`, Git dispatches it
as a subcommand: `git ots status` and `git-ots status` are the same command.
This document uses the `git-ots` spelling throughout, since that is the form
that works in a cron entry or a hook script without relying on `git`'s lookup.

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
uv run git-ots status
uv run git-ots run --dry-run
uv run git-ots run
```

The `git-ots` console script is also directly available inside an activated
project virtual environment.

Report the installed version with `git-ots --version`.

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

## Getting started

There is no initialization step. `git-ots` runs on an unconfigured repository:

```bash
git-ots status          # what would happen, and why
git-ots run --dry-run   # the same, as the run would decide it
git-ots run             # timestamp
git-ots run --push      # timestamp, then push the current branch and timestamp tag
git-ots validate        # check the stored proofs, offline
git-ots repair          # recreate tags an interrupted run failed to write
```

The built-in policy is `ots.maxAge` set to `24h`: the newest meaningful commit is
timestamped once it is a day old. Every other setting has a default too; they
are tabulated under [Configuration](#configuration). Configuration exists to
change those defaults, not to enable the tool.

Nothing about a proof depends on your configuration -- it decides *when* and
*whether* a run stamps, never what a proof means. Someone who clones the
repository can therefore run `git-ots validate` without configuring anything,
provided the proofs are in the default directory. `ots.proofDirectory` moves
them, and moving them has a cost worth stating plainly: configuration lives in
`.git/config`, which a clone never receives, so nothing in the repository can
tell a consumer where the proofs went. They have to be told out of band. Leave
the default unless you have a reason not to.

## Configuration

Configuration is optional and lives entirely in Git's own configuration
system, under the `ots.*` namespace. There is no configuration file and no
`--config` flag: `git-ots` runs on an unconfigured repository using the
built-in defaults tabulated below.

A key is set the same way any other Git setting is,
across any of Git's normal scopes:

```bash
git config ots.maxAge 12h                  # this repository (local scope)
git config --global ots.signing required   # every repository on this machine
git -c ots.everyCommit=true git-ots run     # this invocation only (command scope)
```

Git resolves these exactly the way it resolves any other setting: system,
then global, then local, then worktree, then the `-c`/`GIT_CONFIG_*` command
scope, each narrower scope overriding the wider ones for the same key. Run
`git-ots config` to see the effective value `git-ots` will use for every key
and which scope it came from.

Only the keys you want to change need setting; anything unset keeps its
default. Note that naming a different policy trigger replaces the default
policy rather than adding to it -- setting `ots.fixedTime` (and
`ots.timezone`) without also setting `ots.maxAge` leaves no maximum-age
trigger.

A complete example, setting every key to a value:

```bash
git config ots.everyCommit false
git config ots.maxAge 24h
git config ots.fixedTime 00:00
git config ots.timezone Europe/Berlin
git config ots.initialHistory latest
git config ots.sourceRef HEAD
git config ots.fetchBeforeRun true
git config ots.tagPrefix ots/
git config ots.requireCleanWorktree false
git config ots.signing inherit
git config ots.proofCommit true
git config ots.proofDirectory .opentimestamps
git config ots.command ots
git config ots.otsTimeout 120s
git config ots.gitTimeout 60s
```

Every setting is optional. These are the defaults, and they are what an
unconfigured repository uses:

| Git config key | Default | Meaning |
|---|---|---|
| `ots.everyCommit` | `false` | Timestamp each meaningful commit rather than the newest |
| `ots.maxAge` | `24h` | Timestamp the newest meaningful commit once it is this old |
| `ots.fixedTime` | unset | Timestamp at a wall-clock time of day |
| `ots.timezone` | unset | The zone `ots.fixedTime` is read in; required with it |
| `ots.initialHistory` | `latest` | What the first `every_commit` run covers |
| `ots.sourceRef` | `HEAD` | The ref whose tip is timestamped |
| `ots.fetchBeforeRun` | `true` | Fetch before deciding |
| `ots.tagPrefix` | `ots/` | Namespace for dedup tags |
| `ots.requireCleanWorktree` | `false` | Refuse to run with uncommitted changes |
| `ots.signing` | `inherit` | `required` signs the tags and commits `git-ots` creates |
| `ots.proofCommit` | `true` | Commit generated proofs |
| `ots.proofDirectory` | `.opentimestamps` | Where proofs and manifests are stored |
| `ots.command` | `ots` | The client executable to invoke |
| `ots.otsTimeout` | `120s` | Ceiling on an OpenTimestamps subprocess |
| `ots.gitTimeout` | `60s` | Ceiling on a Git subprocess |

`ots.maxAge` is the default policy, so at least one policy is always enabled
unless it is turned off explicitly. Setting `ots.everyCommit` or
`ots.fixedTime` without also setting `ots.maxAge` replaces the default
policy rather than adding to it; setting `ots.everyCommit` to `false` alone
leaves nothing enabled and is rejected.

### `ots.initialHistory`

`ots.initialHistory` controls the first `every_commit` run when no timestamp
tag exists. The default, `latest`, timestamps only the latest meaningful
commit; set it to `all` to timestamp the complete existing meaningful
history in oldest-to-newest order. It does not change the behavior of
aggregate policies.

### `ots.proofDirectory`

```bash
git config ots.proofDirectory audit/anchors
```

Where `.ots` proofs and their `.json` manifests are written, read, committed,
and named in timestamp-tag annotations. Repository-relative, no trailing `/`,
and it must stay inside the worktree -- an absolute path, a `..` segment, a
`~`, a backslash, or a segment outside `[A-Za-z0-9._-]` is rejected with exit
2. The value ends up in Git pathspecs and in `git ls-tree` output this tool
parses back, so the accepted alphabet is narrower than the filesystem's.

Every command honors it: `run` writes there; `status`, `validate`, `verify`, and
`upgrade` scan it; and the generated proof commit stages exactly those paths.

**Moving it costs something.** Configuration lives in `.git/config`, which is
never cloned, so a consumer of your repository has no way to discover that the
proofs are not in `.opentimestamps/`. They see a repository whose proofs appear
to be missing. Nothing in the repository can tell them otherwise, so you have
to.

**Changing it on a repository that already has proofs needs one extra step.**
Move the existing files in the same change:

```bash
git config ots.proofDirectory audit/anchors
mkdir -p audit && git mv .opentimestamps audit/anchors
git commit -m "Move proofs to audit/anchors"
```

Skipping the `git mv` leaves the old proofs behind where nothing looks for
them: `validate`, `verify`, `upgrade`, and `status` scan only the configured directory, so
those proofs go invisible immediately. `run` keeps working for as long as the
local timestamp tags survive -- they record the source commit, not a path --
but tags are never pushed, so anyone who clones has only the generated proof
commits to go on. For them the artifacts are missing from where the
configuration says they live, and `run` refuses with exit 3 (`inconsistent
repository state`) rather than silently timestamping the same commit twice.
Older generated proof commits also start warning that they touch paths outside
the configured directory, which is the same condition reported earlier.

### `ots.otsTimeout` and `ots.gitTimeout`

```bash
git config ots.otsTimeout 120s
git config ots.gitTimeout 60s
```

`ots.otsTimeout` bounds a single OpenTimestamps submission, upgrade, or verification;
`ots.gitTimeout` bounds a single `git` invocation that `git-ots` issues to
inspect refs, fetch, tag, or commit. Exceeding either ceiling kills the
child's whole process group and raises a typed timeout error (exit 4 for
`ots.otsTimeout`, exit 6 for `ots.gitTimeout`; see [Exit codes](#exit-codes)).
Setting either value too low
turns a slow-but-healthy calendar into a recurring failure, so raise the
limit rather than leaving a marginal connection permanently failing.

Either key MAY be set to the literal string `"0"` to mean unbounded — no
ceiling is applied. `"0"` is recognized only for these two keys; it is not a
valid spelling for `ots.maxAge` or any other duration-valued key, and the
near-miss `"0s"` is rejected everywhere, including here.

`ots.gitTimeout` does not reach every `git` invocation `git-ots` makes.
Reading a file out of a commit tree — `git show <commit>:<path>` and
`git cat-file --batch`, used when comparing proof-manifest history — is
always bounded at the built-in 60-second default regardless of this
setting, because those reads carry no hook execution and no network I/O. If
a timeout message names `cat-file`, raising `ots.gitTimeout` will not
change its ceiling.

Bounding these subprocesses also changed how they run: both `git` and `ots`
children now start detached from the controlling terminal, with their own
standard input closed. Passphrase and credential prompts — for example an SSH
key requiring a passphrase during `git fetch`, which runs whenever
`ots.fetchBeforeRun` is enabled (the default) — no longer reach the operator;
the child sees end-of-file instead of a prompt and fails immediately rather
than hanging. Configure `ssh-agent` (or the equivalent HTTPS credential
helper) so these operations succeed non-interactively, and use
`SSH_ASKPASS` or `GIT_ASKPASS` if a passphrase must still be supplied
programmatically.

A `git` timeout that interrupts an index-mutating command (`git commit`,
`git add`) can leave a stale `.git/index.lock` behind, exactly as killing
`git` by any other means would. `git-ots` deliberately never deletes this file
— its own advisory lock does not exclude a concurrent `git` invocation run
directly by the operator, which could legitimately hold it — so every
subsequent `git-ots` run that needs to stage or commit (a `run` or `upgrade`
with `ots.proofCommit` enabled) fails until a human removes it. Read-only
commands (`status`, `validate`, `run --dry-run`) are unaffected. Once you have
confirmed no other `git` process is running against the repository:

```bash
rm .git/index.lock
```

## Policies and scheduling

`git-ots` evaluates three independent timestamping policies:

### `every_commit`

When enabled, every pending meaningful commit receives its own OpenTimestamps proof and tag.

```bash
git config ots.everyCommit true
```

For example, if commits B, C, and D are pending, the tool submits B, then C, then D in repository order. This mode is a good fit for CI pipelines that run on every push.

### `max_age`

Ensures the oldest pending meaningful commit does not remain unstamped beyond a configured duration:

```bash
git config ots.maxAge 24h
```

The rule is due when the oldest pending commit reaches the configured age. The timestamp target is the **latest** pending commit, because timestamping that state cryptographically covers all earlier history. Supported durations include `30m`, `12h`, `24h`, `2d`, and `7d`.

### `fixed_time`

Anchors the latest pending state once per configured local wall-clock time:

```bash
git config ots.fixedTime 00:00
git config ots.timezone Europe/Berlin
```

The timezone is an IANA identifier such as `Europe/Berlin` or `America/New_York`. The fixed-time occurrence becomes eligible at the configured local time and remains eligible until the next occurrence. This graceful window protects against a slightly delayed scheduler.

### Combining policies

Enabled policies are evaluated independently and their triggers combine with a logical OR:

```text
should_timestamp =
    every_commit
    OR (pending_changes AND max_age_due)
    OR (pending_changes AND fixed_time_due)
```

When multiple aggregate triggers fire at once, they produce exactly one submission for the latest pending commit, and the manifest records all applicable trigger reasons. `every_commit` has stronger target semantics: if enabled, all pending meaningful commits are submitted individually.

### Invocation-driven behavior and scheduler latency

`git-ots` is invocation-driven: it does not run a daemon itself. It is designed to be called from an external scheduler such as `cron`, a `systemd` timer, or a CI pipeline.

The maximum-age guarantee is therefore:

```text
configured max_age + scheduler invocation latency
```

For example, a 24-hour `max_age` combined with an hourly scheduler gives an effective worst-case detection delay of approximately `24h + 1h`. For fixed-time policies, run the scheduler more frequently than the precision required around the configured wall-clock time.

`git-ots` is designed to be invoked once per hour or slower. A `max_age` below one hour cannot be honored more precisely than the Bitcoin block interval, and requires a scheduler that runs at least as often as the configured duration.

### Scheduler examples

Run from `cron` every hour:

```cron
0 * * * * cd /path/to/repo && git-ots run
```

A `systemd` timer unit (`git-ots.timer`):

```ini
[Unit]
Description=Run git-ots every hour

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

With the matching service (`git-ots.service`):

```ini
[Unit]
Description=git-ots timestamp run

[Service]
Type=oneshot
WorkingDirectory=/path/to/repo
ExecStart=git-ots run
```

Or run from a CI pipeline after pushes:

```yaml
steps:
  - name: Timestamp meaningful commits
    run: git-ots run
```

## Source ref and upstream behavior

The default `ots.sourceRef` is `HEAD`, so the eligible state is the current branch
tip. This makes stamping before publication a first-class use case: a Git commit
hash is identical whether the commit is local or on a forge, so the remote is
not a cryptographic trust boundary.

```bash
git config ots.sourceRef HEAD
```

Users who prefer to timestamp only pushed state may configure the upstream ref
(`@{upstream}`) so that only commits reachable from a configured upstream are
timestamped:

```bash
git config ots.sourceRef "@{upstream}"
```

That upstream-only mode is a churn-and-cost preference, not a safety property:
it avoids leaving permanent timestamp tags on amended, squashed, or rebased
local commits, and it avoids paying for submissions whose hashes may never be
referenced elsewhere. Unpushed local commits are ignored when `@{upstream}` is
configured.

When `ots.fetchBeforeRun` is enabled (the default), `run` fetches the relevant remote before
resolving the ref, but the local `HEAD`, index, and worktree remain unchanged.
`status` is strictly read-only: it does not fetch, lock the repository, submit
a timestamp, or modify files.

## Proof layout

For a source commit with full SHA-1 `0123456789abcdef0123456789abcdef01234567`, the default proof layout is:

```text
.opentimestamps/0123456789abcdef0123456789abcdef01234567.ots
.opentimestamps/0123456789abcdef0123456789abcdef01234567.json
```

The `.ots` file is the OpenTimestamps detached proof. The `.json` file is a small operational manifest recording the source commit, submission time, trigger reasons, and relative proof path.

`.opentimestamps/` is the default, not the only possibility: [`ots.proofDirectory`](#otsproofdirectory) moves both files elsewhere in the worktree. The file names and the manifest's contents are unaffected -- only the directory changes.

## Timestamp tags

Every successful submission creates an immutable annotated Git tag on the source commit. The tag name format is:

```text
ots/<UTC-timestamp>/<short-sha>
```

Example:

```text
ots/20260808T220003Z/0123456789ab
```

The short SHA component prevents collisions when several commits are timestamped during the same second. The tag annotation records the source commit, submission time, proof path, and trigger reasons.

The tag is the only place `submitted_at`, the source ref and the trigger set
appear in a signed, human-readable Git object — the proof itself says nothing
about any of them. If a tag is missing because a run was interrupted,
[`git-ots repair`](#reconciling-a-proof-that-no-run-will-reach) rebuilds it from
the proof's manifest rather than leaving you to hand-craft the tag object.

Tags are never pushed. A clone therefore carries the generated proof commits
and no tags, which is expected rather than damage.

## First-run baseline

When no timestamp tag exists, all reachable meaningful history is considered
pending. Aggregate policies (`max_age` and `fixed_time`) therefore timestamp the
latest meaningful commit as soon as they become due. With `every_commit` enabled,
`initial_history` controls how the first run behaves: the default `"latest"`
timestamps only the newest pending commit, while `"all"` timestamps the complete
existing meaningful history in oldest-to-newest order. `initial_history` affects
only `every_commit`; it does not change the behavior of aggregate policies.

## Non-goals

`git-ots` only ever writes inside the local repository. It never pushes commits
or tags, and `git-ots run` never runs `ots upgrade`: the scheduled path submits
proofs and nothing else, so it never depends on a calendar being reachable and
never rewrites a proof it already stored. It never removes proofs or tags
automatically. Pushing to remotes is left to the operator, and completing
pending proofs is a separate, explicitly invoked
[`git-ots upgrade`](#completing-proofs-with-git-ots-upgrade).

`git-ots` also stores no chain data. Proofs record the attested block *height*;
they never cache block headers, block hashes, or anything else fetched from
Bitcoin. A stored header cannot reduce the trust a verifier must extend — it is
only meaningful in a chain with work behind it — and it would invite reading a
cached claim as a confirmed fact. See
The proof deliberately does not store Bitcoin block headers; verification
retrieves the relevant header independently from the configured node.

## Idempotency and generated commits

`git-ots` is designed to be safe to run repeatedly. After a successful run, the next invocation finds the timestamp tag, the proof, and the generated proof commit, and concludes that no further action is required. Generated proof commits are filtered out from the meaningful history because they carry the `OpenTimestamps-Generated: true` trailer and change only files inside the configured proof directory. This keeps proof bookkeeping from ever creating new timestamp obligations.

A normal user commit that edits a `.ots` file without the generated trailer is still meaningful, because the repository content itself changed.

## Clean-worktree safety

A generated proof commit cannot include unrelated user edits. `git-ots` stages
only the specific `.ots` and `.json` paths it produced and commits them by
explicit pathspec; it never runs a broad `git add .`. Uncommitted work
elsewhere in the tree is therefore excluded structurally, not by precondition.

Because of that, `ots.requireCleanWorktree` defaults to `false`. It is an
operator preference rather than a safety property: set it to `true` and, when
`ots.proofCommit` is enabled, any uncommitted change -- an untracked file included
-- fails the run before submission.

Turning it on is not free on the scheduled path. Cron fires whether or not you
happen to have edits open, and a refused run is a timestamp not taken, visible
only in the job's exit status. It suits a repository no human edits in place;
leave it off for a checkout you work in.

## Signing what `git-ots` creates

Generated tags and commits use Git's configured `user.name` and `user.email`.
If Git cannot resolve that identity, a real run stops before submitting
anything and explains how to configure it; git-ots never invents an identity.

`git-ots` authors two kinds of Git object on your behalf: the annotated
timestamp tag, and the generated proof and upgrade commits. `ots.signing`
controls whether it signs them.

```bash
git config ots.signing required
```

`"required"` passes `-s` to `git tag` and `-S` to `git commit`, taking the key
from your ordinary Git signing configuration (`user.signingkey`, `gpg.format`).

The name is the guarantee. Asking Git to sign is not what you want; what you
want is that an object never exists unsigned. So a signer that fails aborts the
operation rather than producing an unsigned object, and a scheduled run on a
host with no available key fails loudly instead of quietly downgrading.

**It never touches your own commits.** Whether the commits being timestamped
are signed is a property of how you work in the repository, and `git-ots`
neither requires nor produces signatures there. The tool is limited to
timestamp generation.

### What a signature here does and does not add

Be clear about the size of the gain, because it is smaller than it looks.

* It authenticates **who ran the tool** — a question the Bitcoin attestation
  cannot answer. It adds nothing whatsoever to what that attestation proves.
* It is **not** what stops a forged timestamp. A fabricated tag or proof does
  not survive `ots verify` regardless, because forging an attestation means
  forging Bitcoin. What the signature buys is cheap local triage — full
  verification needs a Bitcoin node, a signature check needs neither — and
  defence against a plausible-looking tag planted by someone with push access.
* What it authenticates most meaningfully is the **manifest**, whose
  `submitted_at` and trigger fields no attestation covers.
* A tag signature is **not itself timestamped.** A tag object is never the
  subject of a payload, so unlike a signature on a source commit — which sits
  inside the commit object the payload names, and is therefore anchored by the
  very act of timestamping it — a tag signature keeps the ordinary problem that
  key revocation is retroactive.

A generated proof commit fares slightly better: once a later run stamps a
source commit that descends from it, the proof commit is anchored transitively
through the parent chain. The most recent one never is, and none of them are if
proofs live on a branch outside the stamped history.

### Why the default is `inherit`, not `off`

```bash
git config ots.signing inherit   # the default
```

`inherit` is named for what it does. `git-ots` passes no signing flag, and your
ambient Git configuration applies unchanged — so if you have
`commit.gpgsign = true` or `tag.gpgSign = true` globally, generated objects are
signed whether or not you asked `git-ots` for it. On a host with no TTY that is
a route to a scheduled run that fails, or hangs waiting for a passphrase.

Calling that state `off` would have been a lie, and an expensive one: `off` is
the name for a mode that actively *suppresses* ambient configuration, which
`git-ots` does not implement. Reserving the name means that mode can be added
later without changing what any existing configuration means. Writing
`signing = "off"` today is rejected with an error
saying so, rather than silently accepted as the default.

Annotations are parsed with any signature block stripped, so a tag signed
either way still reads correctly as a timestamp baseline.

## Concurrency and locking

Only one `git-ots` process may modify a repository at a time. The tool acquires an OS-level advisory lock in the Git common directory (for example `.git/git-ots.lock` or the equivalent for linked worktrees). A second concurrent invocation exits with code 7 instead of blocking or interfering.

The locking contract applies to ordinary local filesystems, including linked
Git worktrees. Repositories on network filesystems such as NFS or SMB are
outside the supported deployment model because their advisory-lock semantics
cannot be guaranteed.

## Crash recovery

Submitting to the calendars is irreversible and happens before several fallible
local steps — writing the proof, staging it, committing it, tagging the source.
A failure in any of those leaves a commitment the calendars hold and the
repository does not yet record. `git-ots run` therefore **finishes an
interrupted transaction before it starts a new one**, and says so on stdout:

```console
$ git-ots run
source ref: refs/heads/master
...
completed timestamp tag for 2335c6e5828d from its stored proof
```

The states it recognises:

* **Proof exists, tag absent** — the manifest and proof are inspected; if they match the intended source commit, the run continues by creating the tag instead of resubmitting.
* **Tag exists, proof files uncommitted** — the run commits the existing proof files without a new submission or tag.
* **Proof artifacts committed, tag absent** — the tag is created from the manifest, whatever committed the artifacts. A proof swept into history by an ordinary commit is recovered exactly like one committed by `git-ots` itself: what is checked is that the proof binds cryptographically to the commit its manifest names, not which commit stored it.
* **Submitted but nowhere recorded ("in flight")** — the proof is on disk, valid, and no tag names it. The run commits and tags it, and **does not submit anything new that run**. See below.
* **Proof commit exists, tag absent, manifest unusable** — if the manifest does not provide enough evidence for safe recovery, the command fails with an actionable error rather than guessing.

Recovery never invents a submission time. `submitted_at`, the source ref and
the trigger set always come from the stored manifest, so a tag recovered weeks
later still records when the commitment was actually made.

Partial temporary files are never treated as completed proofs.

### Why a blocked run defers instead of pressing on

Every run re-resolves the source ref and re-evaluates its triggers against the
current `HEAD`. If a run cannot commit — a repository-wide `pre-commit` hook
rejects the generated commit, `.git/index.lock` is held, the machine is
interrupted — then by the time the obstruction clears, `HEAD` has usually moved.
A run that simply pressed on would stamp a *different* commit and abandon the
one it already paid for, once per blocked attempt.

So a run that finds an in-flight submission completes it and defers this run's
own trigger by one invocation:

```console
deferred timestamping 8f21a4c0b3de to finish an earlier run's unrecorded submission first
```

Nothing is lost: the next run sees a real baseline and stamps normally. The
practical effect is that a hook veto or a lock collision costs a delay rather
than an abandoned commitment, however long it lasts.

`git-ots` waits out `.git/index.lock` rather than aborting on it — an unattended
job and a person working in the same repository contend for the index routinely.
The wait is bounded by `ots.gitTimeout` and applies *only* to lock contention: a
rejected hook or a failing signer still fails immediately, because a real
problem should surface fast rather than after a delay.

### Reconciling a proof that no run will reach

`run` reconciles the baseline, the pending commits, and anything in flight. A
proof orphaned by an interruption further back is outside that window, and
`git-ots validate` is what surfaces it:

```console
$ git-ots validate
opentimestamps/2335c6e5828d….ots: valid (2335c6e5828d) Bitcoin block header attestation present but not chain-verified; untagged
1 proof(s) have no timestamp tag. Run `git-ots repair` to recreate the tags from their manifests.
```

`git-ots repair` creates the missing tags, reconstructing each annotation from
that proof's own manifest:

```bash
git-ots repair --dry-run   # report what would be tagged
git-ots repair
```

It never submits, never moves an existing tag, and never manufactures a
submission time. Proofs whose source has been rewritten off the current lineage
are reported and left alone — tagging an abandoned lineage would change what the
baseline search sees — as are proofs whose manifest cannot be validated, since
the annotation genuinely cannot be reconstructed without one (exit 5).

An untagged proof is **not** an error and does not change `validate`'s exit
code. `git-ots` never pushes tags, so a fresh clone legitimately has the
generated proof commits and no tags at all.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success; timestamp created or no action required |
| 1 | General operational failure |
| 2 | Invalid configuration |
| 3 | Invalid Git repository state |
| 4 | OpenTimestamps submission, upgrade, or verification failure |
| 5 | Proof persistence failure |
| 6 | Git tag/commit failure |
| 7 | Repository locked by another process |
| 100 | `run --exit-code` only: nothing was due and nothing was repaired |
| 130 | Interrupted by SIGINT (Ctrl-C) |

`git-ots run` exits 0 whether it created a timestamp, completed an earlier run's
bookkeeping, or correctly did nothing — and that stays the default, because
making the most common outcome non-zero would turn every healthy scheduled run
into a reported failure. Pass `--exit-code` when a wrapper needs the
distinction:

```bash
git-ots run --exit-code
case $? in
  0)   echo "timestamped or repaired" ;;
  100) echo "nothing due" ;;
  *)   echo "failed with $?" >&2 ;;
esac
```

100 sits outside the range above so it can never be confused with a failure.
`--dry-run` always exits 0: it reports rather than acts, so it is never "idle"
in the sense the flag asks about.

Unexpected internal failures stay concise by default and name their exception
type. Re-run as `git-ots --debug <command>` for verbose logging and a complete
traceback, or set `GIT_OTS_DEBUG=1` for an existing unattended invocation.
Tracebacks are emitted verbatim to local stderr and can contain filesystem
paths, branch names, and filenames; review them before posting publicly.

An `ots.gitTimeout` expiry does not always surface as exit 6. Several internal Git
checks (for example resolving the tag ref or asserting a clean, committable
worktree) catch `GitCommandError` and relabel it as an invalid-repository-state
failure, which maps to exit 3 instead. The timeout message text is unaffected
either way, but a scheduler alerting only on exit 6 will silently miss those
cases; alert on 3 as well if you want complete coverage of `git` timeouts.

## Verifying stored proofs

Each proof is a standard OpenTimestamps detached proof. `git-ots` stamps a temporary file containing the exact canonical payload (`git:<object-format>:<full-commit-id>\n`), so verification requires the same payload content as the original file:

```bash
commit_sha=<commit-sha>
printf 'git:sha1:%s\n' "$commit_sha" > "/tmp/$commit_sha.subject"
ots verify -f "/tmp/$commit_sha.subject" ".opentimestamps/$commit_sha.ots"
```

For SHA-256 repositories, replace `git:sha1:` with `git:sha256:`. The `ots verify` subcommand compares the proof against the supplied original file (`-f`) or a hex digest (`-d`). Verification is independent of `git-ots` and does not modify the repository.

`ots verify` needs a reachable Bitcoin node: it reads the attested block height
out of the proof and fetches that block's header over RPC, from
`~/.bitcoin/bitcoin.conf` or from `--bitcoin-node`. The OpenTimestamps client
has no block-explorer fallback, and `--no-bitcoin` makes it exit 1 rather than
check anything. No calendar is contacted.

For what a proof establishes on its own, why the block header is not stored in
it, and what the anchor does and does not say about time, see
[What a proof proves](docs/what-a-proof-proves.md).

## Validating and verifying proofs

`git-ots validate` performs all repository-local checks without contacting the
network:

```bash
git-ots validate
```

Proof discovery does not depend on local configuration. Supply
`--proof-dir <directory>` explicitly, or git-ots checks `.opentimestamps/` and
then falls back to directories containing tracked `*.ots` files. If none are
found, it reports that nothing was validated rather than claiming that proofs
were successfully checked. `verify` uses the same discovery order.

It reports one of five states for each proof:

* **valid** — the proof is structurally valid, cryptographically bound to the
  attested source commit, and that commit is an ancestor of the current source ref.
* **orphaned** — the proof is valid and bound, but the attested commit is no
  longer an ancestor of the current source ref (for example, after a history rewrite).
* **pending-attestation** — the proof is structurally valid and bound, but it
  contains only calendar attestations and has not yet been anchored to a Bitcoin block.
* **invalid** — the proof is malformed, unbound, or otherwise fails validation.
* **unknown-source** — the attested commit cannot be resolved at all, for example
  because it has been garbage-collected. This is reported separately from
  **orphaned**: an orphaned proof attests to a commit the repository still has
  but which has left the current lineage, whereas here the commit itself is gone.

Alongside the state, each proof is reported as `untagged` when no `ots/*` tag
names its attested commit:

```console
opentimestamps/2335c6e5828d….ots: valid (2335c6e5828d) Bitcoin block header attestation present but not chain-verified; untagged
1 proof(s) have no timestamp tag. Run `git-ots repair` to recreate the tags from their manifests.
```

This is reported separately from the five states rather than as a sixth,
because it is an orthogonal fact: a proof can be both `orphaned` and untagged,
and one label cannot carry both. It does **not** change the exit code —
`git-ots` never pushes tags, so a fresh clone has none, and failing `validate`
there would be wrong. See
[Reconciling a proof that no run will reach](#reconciling-a-proof-that-no-run-will-reach).

The command creates no tag, commit, or file write, and performs no network
access. A `valid` result means locally valid: the Bitcoin attestation is present
but has not been checked against the chain.

`git-ots verify` performs that final check:

```bash
git-ots verify
```

It first runs the same local validation, reconstructs the canonical payload,
and delegates each Bitcoin-attested proof to `ots verify`. It reports
`verified` only when the configured OpenTimestamps client successfully checks
the attestation against a block obtained from a Bitcoin node. A pending proof
is reported as `pending-attestation`; an unreachable or misconfigured node is
`verification-failed`, not an invalid proof. The command is read-only, but it
requires the `ots` executable and a reachable Bitcoin node.

## Where a proof is anchored

`git-ots status` reports, for every stored proof, where it landed on chain:

```text
proofs: 1 anchored, 0 pending
  23afa0b0345f anchored
    block 963292 via https://alice.btc.calendar.opentimestamps.org
      tx 32f1a1c3c32b7a050940263b9d1f2bd911954067a7fcdc7690c3607c6557a15c
      op_return 0562fe8d215231fa67e3f1e842437a013ea66718a3e8a03c2d7e523ee2d6e295
      merkle root 2737ab35d56e1091b353359b173b429d1bc0feba1ddd29128a2ea6ec976af9ef
    block 963295 via https://bob.btc.calendar.opentimestamps.org
      tx 634e78b9546a905c001a5b5324f1c268c276f273e61418a3202726f63eb50674
      op_return d42e0cbcca5c35ae7ff14ee88af29cb041d3710a33d28fb76e512148fd90dad9
      merkle root 0550de4104fce758bcce1307f9b793d1feea55e7b7cb5f5a98b80eb56374fd1f
    awaiting https://btc.calendar.catallaxy.com
    awaiting https://finney.calendar.eternitywall.com
```

A proof is normally anchored more than once: it is submitted to several
independent calendars, and each anchors it in its own transaction. `awaiting`
lines name calendars that have not delivered an attestation into this file yet.
`git-ots upgrade` asks each of them on every run — see
[below](#why-git-ots-asks-calendars-directly) for why that is not what the
OpenTimestamps client does.

Both `status` and `upgrade` report the ratio:

```text
  23afa0b0345f anchored (2 of 4 calendars anchored)
```

Read it as realized redundancy: how many of the calendars this proof was
submitted to have actually anchored it. It is not the count of Bitcoin
confirmations, which a stored proof does not record — a proof holds a block
height, and depth would have to be computed against the current chain tip.

Every value is computed from the proof file alone, by executing the operations
it stores. Only the block height is recorded outright, in the attestation; the
rest are intermediate values along the path: the merkle root is the message
reaching the attestation, the transaction id is the double-SHA-256 of whichever
message is itself a serialized Bitcoin transaction, and the `OP_RETURN`
commitment is read out of that transaction's outputs.

The calendar that published each anchor is recoverable because an upgrade
appends the path to Bitcoin *below* the pending attestation instead of
replacing it — the promise stays in the file next to its fulfilment.

This is a report of what the proof claims, not a confirmation of it. Nothing
here contacts Bitcoin; checking the anchor against a real block is what
`git-ots verify` delegates to `ots verify`.

## Checking calendar availability

`git-ots` does not configure calendars — the OpenTimestamps client owns that —
so the servers this repository depends on are the ones its proofs name. They
can be probed on demand:

```bash
git-ots status --check-servers
```

```text
calendars: 3/4 reachable
  https://alice.btc.calendar.opentimestamps.org ok (HTTP 200)
  https://bob.btc.calendar.opentimestamps.org ok (HTTP 200)
  https://btc.calendar.catallaxy.com unavailable (Connection refused, 1 pending)
  https://finney.calendar.eternitywall.com ok (HTTP 200, 1 pending)
```

`N pending` counts proofs that have no Bitcoin attestation *at all* and named
this calendar. A calendar named only by proofs that are already anchored counts
zero, whether it anchored them or not — nothing is waiting on it, so an outage
there blocks nothing.

An unavailable calendar with proofs waiting on it also produces a warning on
stderr, because it means those proofs cannot be completed by `git-ots upgrade`
until it returns. It does not change the exit code — an outage is information
about the world, not a failure of the command.

Any HTTP response counts as reachable; a calendar root commonly answers 404
while serving the digest endpoints an upgrade needs perfectly well. Only a
transport-level failure counts as unavailable.

The probe is opt-in and off by default: plain `git-ots status` performs
no network access at all, because it is the command a scheduler runs and
must not depend on connectivity.

## Completing proofs with `git-ots upgrade`

A freshly submitted proof contains only calendar promises — `validate` reports it
as `pending-attestation`. The Bitcoin block header attestation that makes a
proof independently verifiable offline becomes available once the calendar's
transaction has confirmed (in practice roughly an hour or two after
submission), and it has to be fetched and written into the `.ots` file. Nothing
does that automatically:

```bash
git-ots upgrade
```

The two-phase construction is not an implementation detail that could be
optimized away: the block that anchors a commit does not exist when that commit
is submitted. [What a proof proves](docs/what-a-proof-proves.md) works through
why.

The command scans the proof directory, asks the OpenTimestamps client to
complete every proof that still lacks a Bitcoin attestation, and reports one
state per proof:

* **upgraded** — new attestations were fetched and the `.ots` file was rewritten.
* **still-pending** — no calendar had anything new yet. The client's own
  diagnostic is included, so a calendar that is merely waiting for
  confirmations reads differently from a network failure.
* **already-complete** — the proof is anchored and no calendar had anything
  further to add. The reported ratio shows how many of its calendars anchored
  it.
* **skipped** — the proof or its manifest is malformed or unbound. It is left
  untouched and the command exits 5.

`--dry-run` reports what would be attempted, naming the calendars that would be
asked, without contacting any of them or writing anything.

### Why `git-ots` asks calendars directly

`ots upgrade` alone is not enough to keep a proof honest. The OpenTimestamps
client treats a timestamp as complete at its *first* Bitcoin attestation —
`is_timestamp_complete` in `otsclient` returns true as soon as one exists, and
`upgrade_timestamp` loops only `while not is_timestamp_complete`. Once any
calendar anchors a proof, the others are never asked again, and running
`ots upgrade` reports "Success! Timestamp complete" without contacting them.

Those calendars may well have anchored it anyway. In this repository's own
proof, two calendars landed and the client stopped; querying a third directly
returned a Bitcoin attestation it had published eighteen blocks later. The
attestation existed, was served on request, and would never have reached the
file.

So after running the client, `git-ots upgrade` asks every calendar that has not
yet delivered — `GET <calendar>/timestamp/<commitment>`, using the commitment
that calendar was actually given, recovered by executing the proof to its
pending attestation — and merges what comes back. The result is a proof that
reflects what happened rather than what the client happened to stop at.

Merging is additive and guarded. A proof is parsed into a tree that
re-serializes byte for byte, so a merge can only append items that were not
there before; re-collecting an answer already present is a no-op, and running
upgrade twice changes nothing. Before rewritten bytes may replace a stored
proof they must still commit to the same source commit and must still contain
every attestation the proof had before. An unparseable or unusable calendar
response is discarded without touching the file.

Upgrades happen on a copy in a temporary directory and are written back through
the same atomic-write path as a new proof, so the client never rewrites a file
inside the repository directly and never leaves its `.ots.bak` behind. An
upgraded proof is only stored if it still binds to the same source commit.

When `ots.proofCommit` is enabled, the rewritten `.ots` files are committed as a
single generated commit:

```text
Upgrade OpenTimestamps proof for 0123456789ab

OpenTimestamps-Generated: true
OpenTimestamps-Upgraded: 0123456789abcdef0123456789abcdef01234567
```

The `OpenTimestamps-Generated: true` trailer is what keeps proof maintenance
from creating new timestamp obligations. Upgraded sources are named with
`OpenTimestamps-Upgraded` rather than `OpenTimestamps-Source`, because the
source trailer is a claim to have stamped a commit and an earlier proof commit
already made that claim.

Manifests are not rewritten: `submitted_at` records the submission, not the
upgrade.

Because `upgrade` mutates proof files, `ots.requireCleanWorktree` is
honored the same way it is for `run` -- off unless you set it -- and when set
the check happens before any proof is rewritten.

## Integration tests

The unit test suite is fully offline and deterministic. A separately marked integration target exercises the real OpenTimestamps CLI against live calendars:

```bash
make test-integration     # or: uv run pytest -m integration
```

The integration target installs a pinned `opentimestamps-client` (currently `0.7.2`), stamps the canonical Git payload, and checks the resulting proof with `ots verify -f`. Because it requires network access, it is not part of the default `uv run pytest` run and is executed in a dedicated CI job.

## License

`git-ots` is released under the [MIT License](./LICENSE).

The license covers the tool itself, not the proofs it produces: an `.ots` file
and its manifest are data about your repository, and nothing in this project
claims any rights over them.
