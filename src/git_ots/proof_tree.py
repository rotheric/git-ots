"""Parse, merge, and re-serialize OpenTimestamps detached proofs.

The rest of this package only ever *reads* proof bytes: ``timestamp.py`` skips
through them structurally and ``anchors.py`` executes them to report what they
say. Collecting an attestation a calendar published after the proof was
considered complete needs more than that -- the new attestation has to be
spliced into the tree and the whole proof written back out.

The parse keeps every item in its original order and every operation as its
original bytes, so re-serializing an unmodified proof reproduces it byte for
byte. That property is the safety net for the whole module: a merge is only
ever "the original tree, plus items that were not there before", and anything
that would rewrite existing bytes shows up immediately as a round-trip
mismatch.

Nothing here contacts the network. Callers supply the calendar response.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .timestamp import ProofParseError, _read_varuint

_HEADER_MAGIC = b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"
_DIGEST_LENGTHS = {0x02: 20, 0x08: 32}
_UNARY_OP_TAGS = frozenset({0x02, 0x03, 0x08, 0x67, 0xF2, 0xF3})
_BINARY_OP_TAGS = frozenset({0xF0, 0xF1})

# python-opentimestamps refuses to read a calendar response larger than this,
# and a merged proof has no business growing without bound either.
MAX_RESPONSE_BYTES = 10_000


def _encode_varuint(value: int) -> bytes:
    """Encode an OpenTimestamps varuint (little-endian base 128)."""
    if value < 0:
        raise ValueError("varuint must be non-negative")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


@dataclass(frozen=True, slots=True)
class Attestation:
    """One attestation item: an 8-byte tag and its payload."""

    tag: bytes
    payload: bytes

    def serialize(self) -> bytes:
        return b"\x00" + self.tag + _encode_varuint(len(self.payload)) + self.payload


@dataclass(slots=True)
class Branch:
    """One operation item and the timestamp that continues below it.

    ``operation`` holds the operation's original serialized bytes. Keeping them
    verbatim makes both re-serialization exact and branch identity a plain
    bytes comparison, which is what a merge needs.
    """

    operation: bytes
    child: Node

    def serialize(self) -> bytes:
        return self.operation + self.child.serialize()


@dataclass(slots=True)
class Node:
    """A timestamp node: a list of items in their original order."""

    items: list[Attestation | Branch] = field(default_factory=list)

    def serialize(self) -> bytes:
        if not self.items:
            raise ProofParseError("an empty timestamp cannot be serialized")
        out = bytearray()
        for item in self.items[:-1]:
            out += b"\xff"
            out += item.serialize()
        out += self.items[-1].serialize()
        return bytes(out)

    def attestations(self) -> list[Attestation]:
        return [item for item in self.items if isinstance(item, Attestation)]

    def branches(self) -> list[Branch]:
        return [item for item in self.items if isinstance(item, Branch)]


@dataclass(frozen=True, slots=True)
class DetachedProof:
    """A parsed detached proof: its verbatim header and its timestamp tree."""

    header: bytes
    digest: bytes
    root: Node

    def serialize(self) -> bytes:
        return self.header + self.root.serialize()


def _read_operation(data: bytes, offset: int) -> tuple[bytes, int]:
    """Return one operation's verbatim bytes and the offset after it."""
    if offset >= len(data):
        raise ProofParseError("truncated operation")
    start = offset
    tag = data[offset]
    offset += 1
    if tag in _UNARY_OP_TAGS:
        return data[start:offset], offset
    if tag in _BINARY_OP_TAGS:
        length, offset = _read_varuint(data, offset)
        offset += length
        if offset > len(data):
            raise ProofParseError("truncated operation argument")
        return data[start:offset], offset
    raise ProofParseError(f"unknown operation tag: {tag:#x}")


def _read_attestation(data: bytes, offset: int) -> tuple[Attestation, int]:
    if offset + 8 > len(data):
        raise ProofParseError("truncated attestation tag")
    tag = data[offset : offset + 8]
    offset += 8
    length, offset = _read_varuint(data, offset)
    end = offset + length
    if end > len(data):
        raise ProofParseError("truncated attestation payload")
    return Attestation(tag=tag, payload=data[offset:end]), end


def parse_timestamp(
    data: bytes, offset: int, *, depth: int = 0, max_depth: int = 256
) -> tuple[Node, int]:
    """Parse one timestamp node, returning it and the offset after it."""
    if depth > max_depth:
        raise ProofParseError("timestamp recursion limit exceeded")

    items: list[Attestation | Branch] = []
    while offset < len(data) and data[offset] == 0xFF:
        offset += 1
        item, offset = _parse_item(data, offset, depth=depth, max_depth=max_depth)
        items.append(item)

    if offset >= len(data):
        raise ProofParseError("truncated timestamp")
    item, offset = _parse_item(data, offset, depth=depth, max_depth=max_depth)
    items.append(item)
    return Node(items=items), offset


def _parse_item(
    data: bytes, offset: int, *, depth: int, max_depth: int
) -> tuple[Attestation | Branch, int]:
    if data[offset] == 0x00:
        return _read_attestation(data, offset + 1)
    operation, offset = _read_operation(data, offset)
    child, offset = parse_timestamp(data, offset, depth=depth + 1, max_depth=max_depth)
    return Branch(operation=operation, child=child), offset


def parse_proof(data: bytes) -> DetachedProof:
    """Parse a detached proof file into a tree.

    ``serialize`` on the result reproduces ``data`` exactly.
    """
    if not data.startswith(_HEADER_MAGIC):
        raise ProofParseError("not an OpenTimestamps detached proof")
    offset = len(_HEADER_MAGIC)

    version, offset = _read_varuint(data, offset)
    if version != 1:
        raise ProofParseError("unsupported OpenTimestamps proof version")

    if offset >= len(data):
        raise ProofParseError("truncated OpenTimestamps proof")
    digest_length = _DIGEST_LENGTHS.get(data[offset])
    if digest_length is None:
        raise ProofParseError("unsupported hash operation in proof")
    offset += 1

    digest = data[offset : offset + digest_length]
    if len(digest) != digest_length:
        raise ProofParseError("truncated OpenTimestamps proof digest")
    offset += digest_length
    header = data[:offset]

    root, offset = parse_timestamp(data, offset)
    if offset != len(data):
        raise ProofParseError("trailing bytes after timestamp tree")
    return DetachedProof(header=header, digest=digest, root=root)


def merge_node(target: Node, source: Node) -> int:
    """Merge ``source``'s items into ``target``, returning how many were added.

    Only additions happen. An attestation already present is left alone, and a
    branch whose operation matches an existing one recurses instead of
    duplicating, so merging a response the proof already contains is a no-op
    and merging twice is idempotent.
    """
    added = 0
    existing_attestations = {(item.tag, item.payload) for item in target.attestations()}
    branches_by_operation = {item.operation: item for item in target.branches()}

    for item in source.items:
        if isinstance(item, Attestation):
            if (item.tag, item.payload) in existing_attestations:
                continue
            existing_attestations.add((item.tag, item.payload))
            # Attestations precede operations at a node, which is the order
            # the reference implementation writes and the order anchor
            # attribution depends on.
            insertion = len(target.attestations())
            target.items.insert(insertion, item)
            added += 1
            continue

        existing_branch = branches_by_operation.get(item.operation)
        if existing_branch is None:
            target.items.append(item)
            branches_by_operation[item.operation] = item
            added += 1
            continue
        added += merge_node(existing_branch.child, item.child)

    return added


def all_attestations(node: Node) -> list[tuple[bytes, bytes]]:
    """Return every attestation in the subtree, as ``(tag, payload)`` pairs.

    Used to assert that a merge only ever added: whatever a proof attested to
    before, it must still attest to afterwards.
    """
    found: list[tuple[bytes, bytes]] = []
    for item in node.items:
        if isinstance(item, Attestation):
            found.append((item.tag, item.payload))
        else:
            found.extend(all_attestations(item.child))
    return found


def parse_calendar_response(data: bytes) -> Node:
    """Parse a calendar's ``/timestamp/<commitment>`` response body.

    The response is a bare timestamp for the requested commitment -- the same
    grammar as a node's contents, without a detached-file header.
    """
    if not data:
        raise ProofParseError("empty calendar response")
    if len(data) > MAX_RESPONSE_BYTES:
        raise ProofParseError("calendar response exceeded size limit")
    node, offset = parse_timestamp(data, 0)
    if offset != len(data):
        raise ProofParseError("trailing bytes after calendar timestamp")
    return node
