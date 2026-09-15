"""Offline extraction of Bitcoin anchor detail from a stored proof.

``timestamp.py`` walks a proof structurally: it skips operations to find the
attestation tags, which is all the ``valid``/``pending-attestation`` split
needs. Reporting *where* a commit is anchored needs the operations actually
executed, because the interesting values are intermediate messages rather than
stored fields:

* the **block height** is the only value stored outright, in the Bitcoin
  attestation payload;
* the **block merkle root** is the message reaching that attestation;
* the **transaction id** is the double-SHA-256 of whichever message along the
  path is itself a serialized Bitcoin transaction;
* the **OP_RETURN payload** is the commitment pushed by that transaction.

Everything here is computed from the stored bytes alone. Nothing in this
module contacts the network, reads a block, or validates the anchor against
Bitcoin -- it reports what the proof claims, which is exactly the offline
subset ``status`` can honestly show.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .proof_tree import Attestation, DetachedProof, Node
from .timestamp import (
    _BITCOIN_BLOCK_HEADER_ATTESTATION_TAG,
    _PENDING_ATTESTATION_TAG,
    ProofParseError,
    _read_varuint,
)

# Operations that transform the message. ``0x67`` (keccak256) has no hashlib
# equivalent; a branch using it cannot be executed and is abandoned rather than
# reported wrongly. Bitcoin calendars do not produce it.
_OP_SHA1 = 0x02
_OP_RIPEMD160 = 0x03
_OP_SHA256 = 0x08
_OP_KECCAK256 = 0x67
_OP_REVERSE = 0xF2
_OP_HEXLIFY = 0xF3
_OP_APPEND = 0xF0
_OP_PREPEND = 0xF1

# A message longer than this is not worth test-deserializing as a transaction
# on every node, and no calendar transaction approaches it.
_MAX_TRANSACTION_BYTES = 1_000_000


class AnchorExtractionError(ProofParseError):
    """Raised when a proof cannot be executed far enough to describe it."""


@dataclass(frozen=True, slots=True)
class BitcoinAnchor:
    """One Bitcoin block header attestation, with the path that produced it."""

    block_height: int
    block_merkle_root: str
    transaction_id: str | None
    op_return_data: str | None
    calendars: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProofAnchors:
    """Everything a stored proof says about where and by whom it is anchored.

    ``pending_calendars`` lists calendars that promised this timestamp but
    whose branch carries no Bitcoin attestation. That is a statement about the
    proof's structure, not a prediction: once *any* branch reaches Bitcoin the
    OpenTimestamps client considers the timestamp complete and stops
    upgrading, so on an anchored proof these promises are simply never
    collected. Callers presenting this to a user must not describe them as
    awaited.
    """

    anchors: tuple[BitcoinAnchor, ...]
    pending_calendars: tuple[str, ...]

    @property
    def anchoring_calendars(self) -> tuple[str, ...]:
        """Distinct calendars that published an anchor for this proof."""
        return tuple(
            sorted(
                {calendar for anchor in self.anchors for calendar in anchor.calendars}
            )
        )

    @property
    def promised_calendars(self) -> tuple[str, ...]:
        """Every distinct calendar this proof was submitted to.

        Anchoring and still-pending calendars together, which is the
        denominator for "how much of the redundancy this proof was submitted
        for actually materialized".
        """
        return tuple(sorted({*self.anchoring_calendars, *self.pending_calendars}))


def _double_sha256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def _to_display_hex(data: bytes) -> str:
    """Return byte-reversed hex, the form Bitcoin displays hashes in."""
    return data[::-1].hex()


def _read_compact_size(data: bytes, offset: int) -> tuple[int, int]:
    """Read a Bitcoin compact-size integer, returning value and next offset."""
    if offset >= len(data):
        raise ValueError("truncated compact size")
    first = data[offset]
    offset += 1
    if first < 0xFD:
        return first, offset
    width = {0xFD: 2, 0xFE: 4, 0xFF: 8}[first]
    if offset + width > len(data):
        raise ValueError("truncated compact size payload")
    return int.from_bytes(data[offset : offset + width], "little"), offset + width


@dataclass(frozen=True, slots=True)
class _Transaction:
    output_scripts: tuple[bytes, ...]


def _deserialize_transaction(data: bytes) -> _Transaction | None:
    """Return the parsed transaction, or ``None`` if ``data`` is not one.

    Only the legacy (non-witness) serialization is accepted, which is the
    correct and only relevant form here: the transaction id is defined over
    exactly those bytes, so that is what a proof commits to. The whole buffer
    must be consumed -- a prefix that happens to parse is not a transaction.

    This mirrors what python-opentimestamps does to label a transaction id
    while printing a proof: attempt a deserialization and treat success as
    proof that the message is a transaction.
    """
    if not (10 <= len(data) <= _MAX_TRANSACTION_BYTES):
        return None
    try:
        offset = 4  # version
        input_count, offset = _read_compact_size(data, offset)
        # Zero inputs marks the segwit extended serialization, which is never
        # what a transaction id is computed over.
        if input_count == 0:
            return None
        for _ in range(input_count):
            offset += 36  # previous outpoint
            script_length, offset = _read_compact_size(data, offset)
            offset += script_length + 4  # script plus sequence
            if offset > len(data):
                return None

        output_count, offset = _read_compact_size(data, offset)
        scripts: list[bytes] = []
        for _ in range(output_count):
            offset += 8  # value
            script_length, offset = _read_compact_size(data, offset)
            if offset + script_length > len(data):
                return None
            scripts.append(data[offset : offset + script_length])
            offset += script_length

        offset += 4  # locktime
    except (ValueError, KeyError):
        return None

    if offset != len(data):
        return None
    return _Transaction(output_scripts=tuple(scripts))


def _extract_op_return(transaction: _Transaction) -> bytes | None:
    """Return the data pushed by the first OP_RETURN output, if any."""
    for script in transaction.output_scripts:
        if not script or script[0] != 0x6A:
            continue
        if len(script) < 2:
            continue
        push_op = script[1]
        if 1 <= push_op <= 75:
            data = script[2 : 2 + push_op]
        elif push_op == 0x4C and len(script) >= 3:  # OP_PUSHDATA1
            data = script[3 : 3 + script[2]]
        else:
            continue
        if data:
            return data
    return None


@dataclass(frozen=True, slots=True)
class _PathState:
    """What executing the proof so far has produced on the current branch."""

    message: bytes
    calendars: tuple[str, ...]
    transaction_id: str | None
    op_return_data: str | None


def _apply_operation(
    data: bytes, offset: int, message: bytes
) -> tuple[bytes | None, int]:
    """Execute one operation against ``message``.

    Returns the new message and the offset after the operation. The message is
    ``None`` when the operation is structurally valid but cannot be executed,
    which abandons the branch instead of misreporting it.
    """
    if offset >= len(data):
        raise AnchorExtractionError("truncated operation")
    tag = data[offset]
    offset += 1

    if tag in (_OP_APPEND, _OP_PREPEND):
        length, offset = _read_varuint(data, offset)
        end = offset + length
        if end > len(data):
            raise AnchorExtractionError("truncated operation argument")
        argument = data[offset:end]
        offset = end
        if tag == _OP_APPEND:
            return message + argument, offset
        return argument + message, offset

    if tag == _OP_SHA256:
        return hashlib.sha256(message).digest(), offset
    if tag == _OP_SHA1:
        return hashlib.sha1(message).digest(), offset
    if tag == _OP_RIPEMD160:
        try:
            return hashlib.new("ripemd160", message).digest(), offset
        except ValueError:
            # Absent from OpenSSL 3 builds by default. Not reachable from a
            # Bitcoin calendar path, so abandoning the branch is enough.
            return None, offset
    if tag == _OP_REVERSE:
        return message[::-1], offset
    if tag == _OP_HEXLIFY:
        return message.hex().encode("ascii"), offset
    if tag == _OP_KECCAK256:
        return None, offset

    raise AnchorExtractionError(f"unknown operation tag: {tag:#x}")


def _read_attestation(data: bytes, offset: int) -> tuple[bytes, bytes, int]:
    """Read an attestation, returning its tag, payload, and the next offset."""
    if offset + 8 > len(data):
        raise AnchorExtractionError("truncated attestation tag")
    tag = data[offset : offset + 8]
    offset += 8
    length, offset = _read_varuint(data, offset)
    end = offset + length
    if end > len(data):
        raise AnchorExtractionError("truncated attestation payload")
    return tag, data[offset:end], end


def _decode_calendar_uri(payload: bytes) -> str | None:
    """Decode a pending attestation's calendar URI."""
    try:
        length, offset = _read_varuint(payload, 0)
    except ProofParseError:
        return None
    uri = payload[offset : offset + length]
    if len(uri) != length:
        return None
    try:
        return uri.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _walk(
    data: bytes,
    offset: int,
    state: _PathState,
    anchors: list[BitcoinAnchor],
    pending: list[str],
    *,
    depth: int = 0,
    max_depth: int = 256,
) -> int:
    """Execute one timestamp node, recording anchors found in its subtree."""
    if depth > max_depth:
        raise AnchorExtractionError("timestamp recursion limit exceeded")

    # A message that is itself a serialized transaction identifies the calendar
    # transaction. Everything below this node in the tree is the merkle path
    # from that transaction to the block, so the identity is inherited.
    if state.transaction_id is None:
        transaction = _deserialize_transaction(state.message)
        if transaction is not None:
            op_return = _extract_op_return(transaction)
            state = _PathState(
                message=state.message,
                calendars=state.calendars,
                transaction_id=_to_display_hex(_double_sha256(state.message)),
                op_return_data=op_return.hex() if op_return else None,
            )

    # Every attestation at a node is serialized before any of its operations,
    # so a calendar promise read here is in scope for the branches that follow
    # -- which is precisely how the calendar that completed a branch is
    # identified, since an upgrade appends the path to Bitcoin below the
    # pending attestation rather than replacing it.
    while offset < len(data) and data[offset] == 0xFF:
        offset += 1
        offset, state = _walk_branch(
            data, offset, state, anchors, pending, depth=depth, max_depth=max_depth
        )

    if offset >= len(data):
        raise AnchorExtractionError("truncated timestamp")
    offset, _ = _walk_branch(
        data, offset, state, anchors, pending, depth=depth, max_depth=max_depth
    )
    return offset


def _walk_branch(
    data: bytes,
    offset: int,
    state: _PathState,
    anchors: list[BitcoinAnchor],
    pending: list[str],
    *,
    depth: int,
    max_depth: int,
) -> tuple[int, _PathState]:
    """Execute one attestation or one operation-plus-child-timestamp.

    Returns the next offset and the node state, which a pending attestation
    extends with its calendar for the node's remaining branches.
    """
    if data[offset] == 0x00:
        tag, payload, offset = _read_attestation(data, offset + 1)
        if tag == _BITCOIN_BLOCK_HEADER_ATTESTATION_TAG:
            height, _ = _read_varuint(payload, 0)
            anchors.append(
                BitcoinAnchor(
                    block_height=height,
                    block_merkle_root=_to_display_hex(state.message),
                    transaction_id=state.transaction_id,
                    op_return_data=state.op_return_data,
                    calendars=state.calendars,
                )
            )
        elif tag == _PENDING_ATTESTATION_TAG:
            uri = _decode_calendar_uri(payload)
            if uri is not None and uri not in state.calendars:
                pending.append(uri)
                state = _PathState(
                    message=state.message,
                    calendars=(*state.calendars, uri),
                    transaction_id=state.transaction_id,
                    op_return_data=state.op_return_data,
                )
        return offset, state

    message, offset = _apply_operation(data, offset, state.message)
    if message is None:
        return (
            _skip_subtree(data, offset, depth=depth + 1, max_depth=max_depth),
            state,
        )
    child = _PathState(
        message=message,
        calendars=state.calendars,
        transaction_id=state.transaction_id,
        op_return_data=state.op_return_data,
    )
    return (
        _walk(
            data, offset, child, anchors, pending, depth=depth + 1, max_depth=max_depth
        ),
        state,
    )


def _skip_subtree(data: bytes, offset: int, *, depth: int, max_depth: int) -> int:
    """Advance past a subtree that could not be executed."""
    anchors: list[BitcoinAnchor] = []
    pending: list[str] = []
    unusable = _PathState(
        message=b"", calendars=(), transaction_id=None, op_return_data=None
    )
    return _walk(
        data, offset, unusable, anchors, pending, depth=depth, max_depth=max_depth
    )


def describe_anchors(proof_bytes: bytes) -> ProofAnchors:
    """Return the Bitcoin anchors and outstanding calendars a proof records.

    ``proof_bytes`` must already have passed
    :func:`git_ots.timestamp.validate_detached_proof`, which checks the header
    and the binding to the source commit; this function starts from the
    embedded file digest and executes the tree from there.

    Calendars that appear on a branch reaching a Bitcoin attestation are
    reported on that anchor; calendars whose branch has no anchor yet are
    reported as pending.
    """
    header_magic = (
        b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"
    )
    if not proof_bytes.startswith(header_magic):
        raise AnchorExtractionError("not an OpenTimestamps detached proof")

    offset = len(header_magic)
    if len(proof_bytes) <= offset or proof_bytes[offset] != 1:
        raise AnchorExtractionError("unsupported OpenTimestamps proof version")
    offset += 1

    if offset >= len(proof_bytes):
        raise AnchorExtractionError("truncated OpenTimestamps proof")
    digest_length = {0x02: 20, 0x08: 32}.get(proof_bytes[offset])
    if digest_length is None:
        raise AnchorExtractionError("unsupported hash operation in proof")
    offset += 1

    digest = proof_bytes[offset : offset + digest_length]
    if len(digest) != digest_length:
        raise AnchorExtractionError("truncated OpenTimestamps proof digest")
    offset += digest_length

    anchors: list[BitcoinAnchor] = []
    pending: list[str] = []
    _walk(
        proof_bytes,
        offset,
        _PathState(
            message=digest, calendars=(), transaction_id=None, op_return_data=None
        ),
        anchors,
        pending,
    )

    anchored = {calendar for anchor in anchors for calendar in anchor.calendars}
    outstanding = tuple(
        sorted({calendar for calendar in pending if calendar not in anchored})
    )
    return ProofAnchors(
        anchors=tuple(sorted(anchors, key=lambda a: a.block_height)),
        pending_calendars=outstanding,
    )


@dataclass(frozen=True, slots=True)
class CollectibleCalendar:
    """A calendar promise that has not been fulfilled inside the proof.

    ``node`` is the timestamp node carrying the pending attestation, and
    ``commitment`` is the message that reached it -- precisely the value the
    calendar was given and the key it files its attestation under. Merging a
    calendar's response into ``node`` is what makes the proof reflect what the
    calendar actually published.
    """

    url: str
    commitment: bytes
    node: Node


def _subtree_has_bitcoin_attestation(node: Node) -> bool:
    for item in node.items:
        if isinstance(item, Attestation):
            if item.tag == _BITCOIN_BLOCK_HEADER_ATTESTATION_TAG:
                return True
        elif _subtree_has_bitcoin_attestation(item.child):
            return True
    return False


def collectible_calendars(proof: DetachedProof) -> tuple[CollectibleCalendar, ...]:
    """Return calendar promises in ``proof`` that no attestation has fulfilled.

    A pending attestation whose subtree already reaches Bitcoin has been
    fulfilled and is skipped. Everything else is a calendar that was asked for
    a timestamp and whose answer, if any, is not in this file -- which is
    exactly what the OpenTimestamps client stops collecting once any branch
    reaches Bitcoin.
    """
    found: list[CollectibleCalendar] = []

    def walk(node: Node, message: bytes | None, depth: int) -> None:
        if depth > 256:
            raise AnchorExtractionError("timestamp recursion limit exceeded")
        for item in node.items:
            if isinstance(item, Attestation):
                if item.tag != _PENDING_ATTESTATION_TAG or message is None:
                    continue
                if _subtree_has_bitcoin_attestation(node):
                    continue
                url = _decode_calendar_uri(item.payload)
                if url is not None:
                    found.append(
                        CollectibleCalendar(url=url, commitment=message, node=node)
                    )
                continue
            if message is None:
                continue
            child_message, consumed = _apply_operation(item.operation, 0, message)
            if consumed != len(item.operation):
                raise AnchorExtractionError("operation bytes not fully consumed")
            walk(item.child, child_message, depth + 1)

    walk(proof.root, proof.digest, 0)
    return tuple(found)
