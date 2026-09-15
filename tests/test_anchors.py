"""Tests for offline Bitcoin anchor extraction.

The golden fixture is a real upgraded proof produced by this repository, and
every value asserted against it was cross-checked against the reference
implementation's own `ots info` output (block heights, transaction ids, and
block merkle roots all match byte for byte). That makes it the authority for
this module: the synthetic cases below cover structure the fixture cannot.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from git_ots.anchors import (
    AnchorExtractionError,
    _deserialize_transaction,
    _extract_op_return,
    describe_anchors,
)
from git_ots.timestamp import build_payload, validate_detached_proof
from tests.test_timestamp import _encode_varuint, _make_fake_detached_proof

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "upgraded-two-anchors.ots"
FIXTURE_SOURCE_COMMIT = "23afa0b0345f1d85b9ce346617b5677a4372b98d"

ALICE = "https://alice.btc.calendar.opentimestamps.org"
BOB = "https://bob.btc.calendar.opentimestamps.org"
CATALLAXY = "https://btc.calendar.catallaxy.com"
FINNEY = "https://finney.calendar.eternitywall.com"


@pytest.fixture
def upgraded_proof() -> bytes:
    return FIXTURE.read_bytes()


def test_fixture_still_binds_to_its_source_commit(upgraded_proof: bytes) -> None:
    """Guards the fixture itself: an unbound proof would invalidate the rest."""
    validate_detached_proof(
        upgraded_proof, build_payload("sha1", FIXTURE_SOURCE_COMMIT)
    )


def test_extracts_both_bitcoin_anchors(upgraded_proof: bytes) -> None:
    anchors = describe_anchors(upgraded_proof).anchors

    assert [a.block_height for a in anchors] == [963292, 963295]
    assert [a.block_merkle_root for a in anchors] == [
        "2737ab35d56e1091b353359b173b429d1bc0feba1ddd29128a2ea6ec976af9ef",
        "0550de4104fce758bcce1307f9b793d1feea55e7b7cb5f5a98b80eb56374fd1f",
    ]


def test_extracts_transaction_ids_in_bitcoin_display_order(
    upgraded_proof: bytes,
) -> None:
    anchors = describe_anchors(upgraded_proof).anchors

    assert [a.transaction_id for a in anchors] == [
        "32f1a1c3c32b7a050940263b9d1f2bd911954067a7fcdc7690c3607c6557a15c",
        "634e78b9546a905c001a5b5324f1c268c276f273e61418a3202726f63eb50674",
    ]


def test_extracts_op_return_commitments(upgraded_proof: bytes) -> None:
    anchors = describe_anchors(upgraded_proof).anchors

    op_returns = [a.op_return_data for a in anchors]
    assert op_returns == [
        "0562fe8d215231fa67e3f1e842437a013ea66718a3e8a03c2d7e523ee2d6e295",
        "d42e0cbcca5c35ae7ff14ee88af29cb041d3710a33d28fb76e512148fd90dad9",
    ]
    # Each commitment is the 32-byte value the calendar pushed on chain.
    assert all(len(bytes.fromhex(data)) == 32 for data in op_returns)


def test_attributes_each_anchor_to_the_calendar_that_published_it(
    upgraded_proof: bytes,
) -> None:
    """An upgrade appends below the pending attestation rather than replacing it.

    That is what makes the publishing calendar recoverable at all, and it only
    works because every attestation at a node is serialized before that node's
    operations.
    """
    anchors = describe_anchors(upgraded_proof).anchors

    assert [a.calendars for a in anchors] == [(ALICE,), (BOB,)]


def test_reports_calendars_that_have_not_anchored_as_pending(
    upgraded_proof: bytes,
) -> None:
    described = describe_anchors(upgraded_proof)

    assert described.pending_calendars == (CATALLAXY, FINNEY)
    # A calendar that already anchored must not also be reported as outstanding.
    anchored = {c for anchor in described.anchors for c in anchor.calendars}
    assert anchored.isdisjoint(described.pending_calendars)


def test_calendar_sets_report_realized_redundancy(upgraded_proof: bytes) -> None:
    described = describe_anchors(upgraded_proof)

    assert described.anchoring_calendars == (ALICE, BOB)
    assert described.promised_calendars == (ALICE, BOB, CATALLAXY, FINNEY)
    # Two of the four calendars this proof was submitted to actually anchored
    # it. The gap is permanent, not progress: the client stopped upgrading at
    # the first attestation.
    assert len(described.anchoring_calendars) == 2
    assert len(described.promised_calendars) == 4


def test_fresh_proof_promises_without_anchoring() -> None:
    payload = build_payload("sha1", "a" * 40)
    described = describe_anchors(_make_fake_detached_proof(payload=payload))

    assert described.anchoring_calendars == ()
    assert described.promised_calendars == ("https://example.com",)


def test_fresh_proof_has_no_anchors_and_lists_its_calendar() -> None:
    payload = build_payload("sha1", "a" * 40)
    described = describe_anchors(_make_fake_detached_proof(payload=payload))

    assert described.anchors == ()
    assert described.pending_calendars == ("https://example.com",)


def test_rejects_bytes_that_are_not_a_detached_proof() -> None:
    with pytest.raises(AnchorExtractionError, match="not an OpenTimestamps"):
        describe_anchors(b"definitely not a proof")


def test_rejects_a_truncated_proof(upgraded_proof: bytes) -> None:
    with pytest.raises(AnchorExtractionError):
        describe_anchors(upgraded_proof[:-40])


# --- transaction parsing ----------------------------------------------------


def _build_transaction(output_scripts: list[bytes]) -> bytes:
    """Serialize a minimal legacy transaction with one input."""
    parts = [
        (1).to_bytes(4, "little"),
        b"\x01",
        bytes(32),
        (0).to_bytes(4, "little"),
        b"\x00",
        b"\xff\xff\xff\xff",
        bytes([len(output_scripts)]),
    ]
    for script in output_scripts:
        parts.append((0).to_bytes(8, "little"))
        parts.append(bytes([len(script)]))
        parts.append(script)
    parts.append((0).to_bytes(4, "little"))
    return b"".join(parts)


def test_transaction_parser_requires_the_whole_buffer_to_be_consumed() -> None:
    transaction = _build_transaction([b"\x6a\x20" + bytes(32)])

    assert _deserialize_transaction(transaction) is not None
    # A prefix that happens to parse is not a transaction; trailing bytes must
    # disqualify it, otherwise arbitrary merkle-path messages could be
    # mislabelled as the calendar transaction.
    assert _deserialize_transaction(transaction + b"\x00") is None


def test_transaction_parser_rejects_the_segwit_serialization() -> None:
    legacy = _build_transaction([b"\x6a\x20" + bytes(32)])
    segwit = legacy[:4] + b"\x00\x01" + legacy[5:]

    assert _deserialize_transaction(segwit) is None


def test_transaction_parser_rejects_a_merkle_path_digest() -> None:
    assert _deserialize_transaction(hashlib.sha256(b"sibling").digest()) is None


def test_op_return_extraction_handles_a_direct_push() -> None:
    commitment = bytes(range(32))
    transaction = _deserialize_transaction(
        _build_transaction([b"\x76\xa9", b"\x6a\x20" + commitment])
    )

    assert transaction is not None
    assert _extract_op_return(transaction) == commitment


def test_op_return_extraction_handles_pushdata1() -> None:
    commitment = bytes(range(32))
    script = b"\x6a\x4c" + bytes([len(commitment)]) + commitment
    transaction = _deserialize_transaction(_build_transaction([script]))

    assert transaction is not None
    assert _extract_op_return(transaction) == commitment


def test_transaction_without_op_return_yields_no_commitment() -> None:
    transaction = _deserialize_transaction(_build_transaction([b"\x76\xa9\x14"]))

    assert transaction is not None
    assert _extract_op_return(transaction) is None


def test_anchor_without_a_transaction_still_reports_the_block() -> None:
    """A proof may attest a block without the path passing through a full tx."""
    payload = build_payload("sha1", "a" * 40)
    header = b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"
    digest = hashlib.sha256(payload).digest()
    bitcoin_tag = bytes.fromhex("0588960d73d71901")
    height = _encode_varuint(700_000)
    attestation = bitcoin_tag + _encode_varuint(len(height)) + height
    proof = header + b"\x01" + b"\x08" + digest + b"\x00" + attestation

    anchors = describe_anchors(proof).anchors

    assert len(anchors) == 1
    assert anchors[0].block_height == 700_000
    assert anchors[0].transaction_id is None
    assert anchors[0].op_return_data is None
