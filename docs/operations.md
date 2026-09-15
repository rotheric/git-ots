# Operations and recovery

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
repository does not yet record. `git ots run` therefore **finishes an
interrupted transaction before it starts a new one**, and says so on stdout:

```console
$ git ots run
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
`git ots validate` is what surfaces it:

```console
$ git ots validate
opentimestamps/2335c6e5828d….ots: valid (2335c6e5828d) Bitcoin block header attestation present but not chain-verified; untagged
1 proof(s) have no timestamp tag. Run `git ots repair` to recreate the tags from their manifests.
```

`git ots repair` creates the missing tags, reconstructing each annotation from
that proof's own manifest:

```bash
git ots repair --dry-run   # report what would be tagged
git ots repair
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

`git ots run` exits 0 whether it created a timestamp, completed an earlier run's
bookkeeping, or correctly did nothing — and that stays the default, because
making the most common outcome non-zero would turn every healthy scheduled run
into a reported failure. Pass `--exit-code` when a wrapper needs the
distinction:

```bash
git ots run --exit-code
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
type. Re-run as `git ots --debug <command>` for verbose logging and a complete
traceback, or set `GIT_OTS_DEBUG=1` for an existing unattended invocation.
Tracebacks are emitted verbatim to local stderr and can contain filesystem
paths, branch names, and filenames; review them before posting publicly.

An `ots.gitTimeout` expiry does not always surface as exit 6. Several internal Git
checks (for example resolving the tag ref or asserting a clean, committable
worktree) catch `GitCommandError` and relabel it as an invalid-repository-state
failure, which maps to exit 3 instead. The timeout message text is unaffected
either way, but a scheduler alerting only on exit 6 will silently miss those
cases; alert on 3 as well if you want complete coverage of `git` timeouts.

## Integration tests

The unit test suite is fully offline and deterministic. A separately marked integration target exercises the real OpenTimestamps CLI against live calendars:

```bash
make test-integration     # or: uv run pytest -m integration
```

The integration target installs a pinned `opentimestamps-client` (currently `0.7.2`), stamps the canonical Git payload, and checks the resulting proof with `ots verify -f`. Because it requires network access, it is not part of the default `uv run pytest` run and is executed in a dedicated CI job.
