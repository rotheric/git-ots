# What a proof proves

The `.ots` files under `.opentimestamps/` are the entire evidence `git-ots`
produces. This document explains what one of them establishes on its own, what
it needs from outside the repository, and what it does not claim. It is
background for anyone auditing a proof; the commands themselves are documented
in the [README](../README.md).

## The short version

A stored proof, once upgraded, plus **one Bitcoin block header** is sufficient
to establish that a given commit id existed before that block was mined.

* The OpenTimestamps calendar servers are not involved in that check, and are
  not trusted by it.
* The transaction carrying the commitment does not need to be fetched from
  anywhere — it is embedded verbatim in the proof.
* The only external input is Bitcoin itself.

## Anatomy of a stored proof

A proof is a program: a chain of `append`, `prepend`, and `sha256` operations
starting from the hash of the canonical payload and ending at an attestation.
Executing it is pure arithmetic. `ots info` prints it:

```console
$ ots info .opentimestamps/23afa0b0345f1d85b9ce346617b5677a4372b98d.ots
File sha256 hash: 8dc93a72ceaf5041a557d87b9382c37fa486c292d24145e5bc0a3ef1d08315b1
Timestamp:
append f33f391ea96229b0ffa425d9d1c25847
sha256
 -> append 0c92a9ff3450960b1af6a5b4d6f1e236
    sha256
    ...
    verify PendingAttestation('https://alice.btc.calendar.opentimestamps.org')
    ...
    prepend 010000000185b666...0000000000000000226a20
    append fdb20e00
    # Transaction id 9c9563dc598fcf726daf5e71d90c898e84eee992aea81e10367eb3b938fe6acc
    sha256
    sha256
    ...
    verify BitcoinBlockHeaderAttestation(963295)
    # Bitcoin block merkle root 0550de4104fce758bcce1307f9b793d1feea55e7b7cb5f5a98b80eb56374fd1f
```

The examples throughout this document are taken from this repository's own
proof for source commit `23afa0b0345f`.

Structurally the path is a stack of commitment layers:

```text
  git:sha1:23afa0b0…\n              reconstructed by the verifier
        │ append nonce, sha256
        ▼
  ┌─ calendar aggregation ─┐        append/prepend + sha256 operations
  └────────────────────────┘        batching many submissions together
        ▼
  merkle root  ──────────────────►  the value pushed by OP_RETURN
        │                              6a 20 <32 bytes>
        ▼
  raw transaction 0100000001…fdb20e00
        │ sha256, sha256
        ▼
  txid 9c9563dc598fcf72…
        │
  ┌─ block merkle tree ────┐        double-sha256 sibling steps
  └────────────────────────┘
        ▼
  merkle root 0550de41…    ──────►  a field in the block 963295 header
                                    ↑ the one value fetched from outside
```

Each layer is worth understanding separately, because the joints between them
are not all the same kind of claim.

### The blinding nonce

The first two operations are always `append` of 16 bytes followed by `sha256`.
Those bytes are random, generated locally before submission
(`OpAppend(os.urandom(16))` in `otsclient/cmds.py`). The calendar therefore
never sees the hash of the payload being timestamped — only a blinded value.

The servers that anchored this repository do not know what they anchored. The
nonce also keeps proofs for adjacent files from leaking each other's digests
once the proofs are separated.

### The calendar aggregation

The next twenty or so operations are the calendar's aggregation tree, which
batches many independent submissions into one commitment so that a single
Bitcoin transaction can timestamp all of them. This region is not a strict
binary merkle tree: OpenTimestamps uses `append`/`prepend` over
arbitrary-length operands, of which a binary merkle path is one special case.

Its output is a 32-byte root.

### The OP_RETURN commitment

The root is then wrapped in a Bitcoin transaction, expressed as one `prepend`
of everything before it and one `append` of everything after:

```text
prepend 0100000001 85b666…a35c 00000000 00 feffffff
        02
        0c53020000000000 16 001463966a1e7cf39e4efac7cf177efedb9e8a95323f
        0000000000000000 22 6a20
append  fdb20e00
```

(Field boundaries are spaced out here for readability; the proof stores each
side as one unbroken byte string.)

Reading the tail of the prefix: `22` is a 34-byte output script, `6a` is
`OP_RETURN`, and `20` pushes the following 32 bytes. The value flowing through
this step *is* the OP_RETURN payload — that is a structural fact about the
serialization, not an assertion the proof asks to be believed.

The rest decodes as an ordinary wallet transaction: version 1, one input, a
152,332-satoshi P2WPKH change output, a zero-value OP_RETURN output, and
`nLockTime` 963,325 — standard anti-fee-sniping, one block below the height
this branch attests to.

### The transaction serialization

The two `sha256` operations after the transaction bytes produce its txid. Note
that this joint is *not* a merkle join: it says "this 32-byte value sits at
this offset inside a message whose hash is a leaf of the block's tree", which
is a different claim from "these two hashes combine into a parent".

That asymmetry matters for soundness. A Bitcoin merkle path is unambiguous only
because internal nodes are exactly 64 bytes — two concatenated hashes — so a
64-byte transaction could otherwise be replayed as an internal node, or an
internal node presented as a transaction. The transaction here is 125 bytes of
well-formed transaction, so the layers cannot be confused. A path that joined
the two trees root-to-root, with no serialization step in between, would not
have that property.

### The block merkle branch

The remaining double-`sha256` steps are the ordinary merkle branch from the txid
up to the block's merkle root. This part *is* a binary merkle path.

### The attestation

`BitcoinBlockHeaderAttestation(963295)` terminates the chain. Its serialized
payload is a single varint: the block height, and nothing else
(`_serialize_payload` in `opentimestamps/core/notary.py`). Verification is one
comparison:

```python
if digest != block_header.hashMerkleRoot:
    raise VerificationError("Digest does not match merkleroot")
return block_header.nTime
```

The attestation is not asserting "here is the header, trust me". It states a
testable claim — *replay these operations and you will obtain the merkle root
of block 963295* — and leaves the verifier to supply the block. A calendar that
lied about the height would only produce a proof that fails against the wrong
block.

## Why the block header is not in the file

The block header cannot be known when a commit is stamped, and does not need to
be, because the causality runs the other way: the commit id goes *into* the
block. The header is downstream of the data, not an input to it.

The usual analogy is a newspaper classified ad. You cannot staple tomorrow's
paper to a sealed envelope; you publish a hash of the envelope in tomorrow's
paper, and anyone can look that issue up later. The paper is needed at reading
time, not at sealing time.

This is why a proof is built in two phases, and why `git-ots` has a separate
`upgrade` subcommand:

1. **Submission.** The calendars return only
   `PendingAttestation('https://…')` — a promise. There is no Bitcoin path and
   no height, because the block does not exist yet. `git ots validate` reports
   the proof as `pending-attestation`.
2. **Upgrade**, typically an hour or two later. The calendars have since
   batched the commitment into a transaction that has confirmed, so they can
   supply the missing middle: the aggregation steps, the raw transaction, the
   block merkle branch, and the height.

Even after upgrading, the header itself is still not in the file. Only the
height is recorded — a lookup key, not evidence.

## What verification needs

`ots verify` obtains the header from a **Bitcoin node**, not from a calendar.
In `opentimestamps-client` 0.7.2 the entire network interaction of
`verify_all_attestations` (`otsclient/cmds.py`) is:

```python
proxy = args.setup_bitcoin()
block_count  = proxy.getblockcount()
blockhash    = proxy.getblockhash(attestation.height)
block_header = proxy.getblockheader(blockhash)
attested_time = attestation.verify_against_blockheader(msg, block_header)
```

`setup_bitcoin` returns a `bitcoin.rpc.Proxy`, configured from
`~/.bitcoin/bitcoin.conf` or from `--bitcoin-node`. There is no block-explorer
fallback anywhere in the client — searching it for `blockstream`,
`blockchain.info`, or `esplora` returns nothing. Without a reachable node,
verification fails; with `--no-bitcoin` it exits 1 reporting
`Bitcoin disabled, could not check attestations`.

**Verifying a proof therefore requires access to a Bitcoin node.** The
OpenTimestamps *website* verifier uses block explorers instead, which is a
weaker trust model: it substitutes a named third party for the chain.

The equivalent check by hand is to read the claimed root out of the proof and
compare it against the chain:

```bash
ots info .opentimestamps/<commit>.ots | grep 'block merkle root'
bitcoin-cli getblockheader "$(bitcoin-cli getblockhash 963295)" | grep merkleroot
```

### What each command actually checks

| Command | Contacts | Establishes |
|---|---|---|
| `git ots validate` | nothing | the proof is well-formed and bound to the commit |
| `git ots status` | nothing (calendars with `--check-servers`) | what the proof *claims* — height, txid, commitment |
| `git ots upgrade` | calendars | fetches the Bitcoin path missing from a pending proof |
| `git ots verify` | a Bitcoin node, through `ots verify` | that the anchor is real |

Only the last row consults Bitcoin. The first two report structure and claims;
neither confirms that block 963295 says what the proof says it says.

## Why `git-ots` does not cache headers either

Since `git-ots` already writes proof artifacts into the repository, an obvious
question is why it does not also store the block header, so that a proof
carries its own anchor and needs no node. It does not, and cannot usefully.

**A header is not self-authenticating.** Either the verifier has a validated
chain — in which case they can fetch the header themselves and a stored copy is
redundant — or they do not, in which case a stored copy is worthless, because
an adversary who fabricates a proof fabricates its header for free alongside
it. Storing the header relocates the fetch without moving the trust boundary.
It would defend only against accidental corruption of the file.

**One header is not evidence.** What makes a header meaningful is its position
in a chain with cumulative proof-of-work behind it. An isolated 80-byte header
carries the work of its own block and nothing more; the 963,000-odd blocks
beneath it, which are the actual claim, do not travel with it.

**The version that would work is the wrong size.** Verifying genuinely offline
needs the header chain from genesis to the anchor — roughly 77 MB today at 80
bytes each, growing about 4.2 MB a year — plus headers above the anchor to show
burial. That is four orders of magnitude larger than the proof it accompanies,
committed into the repository being timestamped, duplicating one of the most
replicated datasets in existence. It would still not be trustless unless the
verifier checked the work, at which point they have reimplemented a node.

**There is nowhere appropriate to put it.** `BitcoinBlockHeaderAttestation`
serializes a varuint height and nothing else, so the format has no slot for it.
A custom attestation tag would survive other clients as an `UnknownAttestation`
rather than breaking them, but it would fork the format for no gain and would
falsify the property this tool advertises — that verification does not depend
on `git-ots`. That leaves the manifest, which is deliberately operational.

The last point is the decisive one. A header a verifier accepts *without*
checking it against the chain is worse than no header at all: it manufactures
confidence in exactly the place none is warranted. The full reasoning, and the
alternatives weighed, are recorded in
This is why proofs deliberately omit block headers and verification retrieves
the relevant header independently from the configured Bitcoin node.

## What the calendars can and cannot do

Calendars are contacted at submission and at upgrade, and are untrusted at
both.

A malicious or compromised calendar can refuse to answer, can return a
malformed response, or can hand back a merkle path that is simply wrong. What
it cannot do is produce a wrong path that *verifies*: that would require either
finding a SHA-256 collision or out-mining the network to place a forged root in
a real block. The worst outcome is a proof that fails verification or never
completes — never a proof that verifies and is false.

This is also why `git ots upgrade` merging responses from several calendars is
safe. Merging is additive and every merged proof must still bind to the same
source commit; a bogus contribution can only add a branch that fails to verify,
not weaken the branches already present.

The four independent Bitcoin attestations in this repository's proof (heights
963292, 963295, 963310 and 963326) are redundancy against a single calendar's
path being malformed or lost. Any one of them is sufficient on its own.

## What a proof does not prove

* **Only an upper bound on time.** The proof establishes that the commit id
  existed *before* block 963295 — not when it was created, and not that it was
  created recently before it. `verify_against_blockheader` returns the header's
  `nTime`, and block timestamps are themselves loose: a miner has a
  median-time-past floor and roughly a two-hour future ceiling. Read an anchor
  as "before approximately this time", never as an exact instant.
* **Nothing about authorship.** Anyone who knows a commit id can timestamp it.
  A proof says the id existed, not who made it or who stamped it. Signing the
  tag and proof commit (`[git] signing = "required"`) answers the second half
  of that — who stamped it — and nothing at all of the first. The first is
  answered only by a signature on the source commit itself, which `git-ots`
  does not create and does not require; note that such a signature lives inside
  the commit object the payload names, so timestamping the commit id anchors it
  for free.
* **Nothing about the subject on its own.** The `.ots` is a detached proof; it
  begins from a hash and does not contain the payload. The verifier must
  independently reconstruct `git:<object-format>:<full-commit-id>\n` and confirm
  the commit id is the one they care about. That binding is what the manifest
  records and what `git ots validate` checks.
* **No more than the hash function allows.** The payload commits to the commit
  id, so for a SHA-1 repository the strength of "this specific commit" is
  bounded by SHA-1's chosen-prefix collision resistance, not by the timestamp.
  The timestamp itself rests on SHA-256.
* **Nothing about repository history.** A proof anchors one commit id. That the
  commit is still reachable from the source ref is a separate, local check —
  the difference `git ots validate` reports as `valid` versus `orphaned`.
