# git-ots

[![Verify](https://github.com/rotheric/git-ots/actions/workflows/verify.yml/badge.svg?branch=master)](https://github.com/rotheric/git-ots/actions/workflows/verify.yml)
![Python 3.12–3.14](https://img.shields.io/badge/python-3.12%E2%80%933.14-blue)

A Git commit identifies an exact repository state, but commit metadata can be rewritten. A newly
presented history can look internally consistent, even claiming an incorrect creation time.

`git-ots` records evidence that selected Git commits existed **before a Bitcoin block was mined**.
It uses [OpenTimestamps](https://opentimestamps.org/) to timestamp a canonical representation of a
commit identifier, then stores and commits the resulting proof.

This anchors a particular version of the commit history to the Bitcoin blockchain. It proves that
the referenced commit and its ancestors existed before the anchoring block was mined. Rewriting
those commits changes their identifiers, so the existing proof no longer applies to the rewritten
history. It remains valid for the original commits, but does not establish whether their recorded
dates were truthful.

## How does this work?

OpenTimestamps calendar servers aggregate cryptographic commitments into Merkle trees and anchor
the resulting roots in Bitcoin transactions. A proof records the hash operations linking the
timestamped data to the Bitcoin anchor. The submitted commitment is derived from the canonical
commit identifier; the commit itself is not submitted.

Once upgraded, the proof contains the cryptographic path from the commit identifier to a Bitcoin
block's Merkle root. It can then be verified without the calendar, using the corresponding block
header from a Bitcoin node. `git-ots` stores the proof in a generated commit.

## Usage

### Create a proof

From inside the repository, inspect what would happen:

```bash
git ots status
git ots run --dry-run
```

To timestamp every meaningful commit, configure the policy once, then request timestamps for
eligible commits:

```bash
git config ots.everyCommit true
git ots run
```

For each selected commit, a successful run creates `.opentimestamps/<commit>.ots` and its JSON
manifest, commits both files, and creates an annotated `ots/...` tag on the source commit. That is
the commit being anchored, not the commit containing the proof.

### Update a pending proof

An initial proof normally contains calendar attestations but no Bitcoin anchor, because the
transaction has not yet been constructed, broadcast, or mined. Once the calendar transaction has
been included in a Bitcoin block, complete the proof with:

```bash
git ots upgrade
```

If new Bitcoin attestations become available, the updated proofs are stored in
a new generated commit. Running the command again is safe.

Attestations arrive over days, so upgrading on a schedule leaves a run of such
commits. To keep them to one, let each upgrade amend the previous one instead:

```bash
git config ots.squashUpgradeCommits true
```

This rewrites history and is therefore off by default. It folds only what
your repository still believes is local, so a schedule that pushes after every
upgrade will not fold anything — and the collapsed commits take the record of
*when* each refresh happened with them. See
[Configuration and scheduling](docs/configuration.md#otssquashupgradecommits)
for what is kept, what is lost, and when folding is declined.

### Validate and verify

Check repository structure and proof-to-commit binding offline:

```bash
git ots validate
```

Once a proof is anchored, cryptographically verify it against Bitcoin. Verification relies on the
underlying OpenTimestamps client’s normal node discovery.

```bash
git ots verify
```

Verification requires access to a Bitcoin Core node; a pruned node is sufficient. The verifier
checks the proof against the block header supplied by that node.

Validation answers whether the repository has the expected evidence and metadata. Verification
additionally establishes the Bitcoin time anchor.

## Install

The supported environments are Python 3.12–3.14 on Linux and macOS. You also need Git,
[`uv`](https://docs.astral.sh/uv/), and the OpenTimestamps `ots` client.

Install the tagged release as an isolated command with `pipx`:

```bash
pipx install "git+https://github.com/rotheric/git-ots.git@v0.0.1"
```

Install the timestamp client separately:

```bash
uv tool install opentimestamps-client==0.7.2
```

Confirm both commands are available:

```bash
git ots --version
ots --version
```

See [Installation and development](docs/installation.md) for checkout-based installation, make
targets, troubleshooting, and contributor setup.

## Detailed documentation

- [Installation and development](docs/installation.md) — prerequisites, installation variants, make
  targets, and troubleshooting.
- [Configuration and scheduling](docs/configuration.md) — every `ots.*` key, policy behavior, source
  selection, cron, systemd, and CI scheduling.
- [Proof lifecycle and verification](docs/proof-lifecycle.md) — timestamp payloads, proof storage,
  tags, upgrading, validation, and Bitcoin verification.
- [Operations and recovery](docs/operations.md) — safety, signing, concurrency, recovery, exit
  codes, and integration tests.
- [What a proof proves](docs/what-a-proof-proves.md) — security interpretation and trust boundaries.
- [Normative specification](specs/spec.md) — the complete behavioral contract.
- [Changelog](CHANGELOG.md) and [release procedure](RELEASING.md).

## License

`git-ots` is released under the [MIT License](LICENSE).
