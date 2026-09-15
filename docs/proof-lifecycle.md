# Proof lifecycle and verification

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

## Proof layout

For a source commit with full SHA-1 `0123456789abcdef0123456789abcdef01234567`, the default proof layout is:

```text
.opentimestamps/0123456789abcdef0123456789abcdef01234567.ots
.opentimestamps/0123456789abcdef0123456789abcdef01234567.json
```

The `.ots` file is the OpenTimestamps detached proof. The `.json` file is a small operational manifest recording the source commit, submission time, trigger reasons, and relative proof path.

`.opentimestamps/` is the default, not the only possibility:
[`ots.proofDirectory`](configuration.md#otsproofdirectory) moves both files
elsewhere in the worktree. The file names and the manifest's contents are
unaffected -- only the directory changes.

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
[`git ots repair`](operations.md#reconciling-a-proof-that-no-run-will-reach) rebuilds it from
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
or tags, and `git ots run` never runs `ots upgrade`: the scheduled path submits
proofs and nothing else, so it never depends on a calendar being reachable and
never rewrites a proof it already stored. It never removes proofs or tags
automatically. Pushing to remotes is left to the operator, and completing
pending proofs is a separate, explicitly invoked
[`git ots upgrade`](#completing-proofs-with-git-ots-upgrade).

`git-ots` also stores no chain data. Proofs record the attested block *height*;
they never cache block headers, block hashes, or anything else fetched from
Bitcoin. A stored header cannot reduce the trust a verifier must extend — it is
only meaningful in a chain with work behind it — and it would invite reading a
cached claim as a confirmed fact. See
The proof deliberately does not store Bitcoin block headers; verification
retrieves the relevant header independently from the configured node.

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
[What a proof proves](what-a-proof-proves.md).

## Validating and verifying proofs

`git ots validate` performs all repository-local checks without contacting the
network:

```bash
git ots validate
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
1 proof(s) have no timestamp tag. Run `git ots repair` to recreate the tags from their manifests.
```

This is reported separately from the five states rather than as a sixth,
because it is an orthogonal fact: a proof can be both `orphaned` and untagged,
and one label cannot carry both. It does **not** change the exit code —
`git-ots` never pushes tags, so a fresh clone has none, and failing `validate`
there would be wrong. See
[Reconciling a proof that no run will reach](operations.md#reconciling-a-proof-that-no-run-will-reach).

The command creates no tag, commit, or file write, and performs no network
access. A `valid` result means locally valid: the Bitcoin attestation is present
but has not been checked against the chain.

`git ots verify` performs that final check:

```bash
git ots verify
```

It first runs the same local validation, reconstructs the canonical payload,
and delegates each Bitcoin-attested proof to `ots verify`. It reports
`verified` only when the configured OpenTimestamps client successfully checks
the attestation against a block obtained from a Bitcoin node. A pending proof
is reported as `pending-attestation`; an unreachable or misconfigured node is
`verification-failed`, not an invalid proof. The command is read-only, but it
requires the `ots` executable and a reachable Bitcoin node.

## Where a proof is anchored

`git ots status` reports, for every stored proof, where it landed on chain:

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
`git ots upgrade` asks each of them on every run — see
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
`git ots verify` delegates to `ots verify`.

## Checking calendar availability

`git-ots` does not configure calendars — the OpenTimestamps client owns that —
so the servers this repository depends on are the ones its proofs name. They
can be probed on demand:

```bash
git ots status --check-servers
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
stderr, because it means those proofs cannot be completed by `git ots upgrade`
until it returns. It does not change the exit code — an outage is information
about the world, not a failure of the command.

Any HTTP response counts as reachable; a calendar root commonly answers 404
while serving the digest endpoints an upgrade needs perfectly well. Only a
transport-level failure counts as unavailable.

The probe is opt-in and off by default: plain `git ots status` performs
no network access at all, because it is the command a scheduler runs and
must not depend on connectivity.

## Completing proofs with `git ots upgrade`

A freshly submitted proof contains only calendar promises — `validate` reports it
as `pending-attestation`. The Bitcoin block header attestation that makes a
proof independently verifiable offline becomes available once the calendar's
transaction has confirmed (in practice roughly an hour or two after
submission), and it has to be fetched and written into the `.ots` file. Nothing
does that automatically:

```bash
git ots upgrade
```

The two-phase construction is not an implementation detail that could be
optimized away: the block that anchors a commit does not exist when that commit
is submitted. [What a proof proves](what-a-proof-proves.md) works through
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

So after running the client, `git ots upgrade` asks every calendar that has not
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
