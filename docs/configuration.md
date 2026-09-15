# Configuration and scheduling

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
git -c ots.everyCommit=true ots run     # this invocation only (command scope)
```

Git resolves these exactly the way it resolves any other setting: system,
then global, then local, then worktree, then the `-c`/`GIT_CONFIG_*` command
scope, each narrower scope overriding the wider ones for the same key. Run
`git ots config` to see the effective value `git-ots` will use for every key
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
`ots.otsTimeout`, exit 6 for `ots.gitTimeout`; see
[Exit codes](operations.md#exit-codes)).
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

The timezone is exact `UTC` or a slash-qualified IANA identifier such as
`Europe/Berlin` or `America/New_York`. Host-specific aliases are rejected so a
configuration behaves consistently across machines. The fixed-time occurrence
becomes eligible at the configured local time and remains eligible until the
next occurrence. This graceful window protects against a slightly delayed
scheduler.

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
0 * * * * cd /path/to/repo && git ots run
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
ExecStart=git ots run
```

Or run from a CI pipeline after pushes:

```yaml
steps:
  - name: Timestamp meaningful commits
    run: git ots run
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
