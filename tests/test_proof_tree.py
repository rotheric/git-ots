"""Tests for parsing, merging, and re-serializing proofs.

Byte-exact round-tripping is the safety property this module rests on. A merge
is defined as "the original tree, plus items that were not there before", so
anything that would silently rewrite existing bytes has to show up as a
round-trip mismatch rather than as a corrupted proof in someone's repository.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from git_ots.proof_tree import (
    Attestation,
    ProofParseError,
    all_attestations,
    merge_node,
    parse_calendar_response,
    parse_proof,
    parse_timestamp,
)
from git_ots.timestamp import build_payload, validate_detached_proof
from tests.test_anchors import FIXTURE, FIXTURE_SOURCE_COMMIT
from tests.test_timestamp import _make_fake_detached_proof

BITCOIN_TAG = bytes.fromhex("0588960d73d71901")


@pytest.fixture
def upgraded_proof() -> bytes:
    return FIXTURE.read_bytes()


def test_round_trip_is_byte_exact(upgraded_proof: bytes) -> None:
    assert parse_proof(upgraded_proof).serialize() == upgraded_proof


def test_round_trip_is_byte_exact_for_a_fresh_proof() -> None:
    proof = _make_fake_detached_proof(payload=build_payload("sha1", "a" * 40))

    assert parse_proof(proof).serialize() == proof


def test_rejects_trailing_bytes(upgraded_proof: bytes) -> None:
    with pytest.raises(ProofParseError, match="trailing bytes"):
        parse_proof(upgraded_proof + b"\x00")


def test_rejects_a_foreign_header() -> None:
    with pytest.raises(ProofParseError, match="not an OpenTimestamps"):
        parse_proof(b"nope")


def test_merging_a_proof_into_itself_changes_nothing(
    upgraded_proof: bytes,
) -> None:
    """Idempotence: re-collecting an answer the proof already holds is a no-op."""
    target = parse_proof(upgraded_proof)
    source = parse_proof(upgraded_proof)

    added = merge_node(target.root, source.root)

    assert added == 0
    assert target.serialize() == upgraded_proof


def test_merge_adds_an_attestation_and_keeps_the_proof_valid(
    upgraded_proof: bytes,
) -> None:
    target = parse_proof(upgraded_proof)
    before = all_attestations(target.root)
    new_attestation = Attestation(tag=BITCOIN_TAG, payload=b"\x99\x99")

    added = merge_node(target.root, type(target.root)(items=[new_attestation]))
    merged = target.serialize()

    assert added == 1
    # The merged proof still commits to the same source commit, and still
    # attests to everything it attested to before.
    validate_detached_proof(merged, build_payload("sha1", FIXTURE_SOURCE_COMMIT))
    after = all_attestations(parse_proof(merged).root)
    assert all(item in after for item in before)
    assert (BITCOIN_TAG, b"\x99\x99") in after


def test_merge_recurses_into_a_branch_it_already_has(
    upgraded_proof: bytes,
) -> None:
    """A shared operation must be descended into, not duplicated."""
    target = parse_proof(upgraded_proof)
    source = parse_proof(upgraded_proof)
    branch_count_before = len(target.root.branches())

    # Give the source a new attestation deep under an operation both share.
    source.root.branches()[0].child.items.append(
        Attestation(tag=BITCOIN_TAG, payload=b"\x77")
    )
    added = merge_node(target.root, source.root)

    assert added == 1
    assert len(target.root.branches()) == branch_count_before
    assert (BITCOIN_TAG, b"\x77") in all_attestations(target.root)


def test_merged_proof_reparses(upgraded_proof: bytes) -> None:
    target = parse_proof(upgraded_proof)
    merge_node(
        target.root,
        type(target.root)(items=[Attestation(tag=BITCOIN_TAG, payload=b"\x01")]),
    )
    merged = target.serialize()

    assert parse_proof(merged).serialize() == merged


def test_calendar_response_must_be_a_bare_timestamp() -> None:
    node, _offset = parse_timestamp(b"\x00" + BITCOIN_TAG + b"\x01\x05", 0)
    body = node.serialize()

    assert parse_calendar_response(body).serialize() == body


def test_calendar_response_rejects_trailing_bytes() -> None:
    with pytest.raises(ProofParseError, match="trailing bytes"):
        parse_calendar_response(b"\x00" + BITCOIN_TAG + b"\x01\x05" + b"\xaa")


def test_calendar_response_rejects_an_empty_body() -> None:
    with pytest.raises(ProofParseError, match="empty"):
        parse_calendar_response(b"")


def test_calendar_response_rejects_an_oversized_body() -> None:
    with pytest.raises(ProofParseError, match="size limit"):
        parse_calendar_response(b"\x00" * 10_001)


def test_every_operation_in_the_fixture_survives_reserialization(
    upgraded_proof: bytes,
) -> None:
    """Operations are kept as their original bytes, so re-encoding cannot drift."""
    original = parse_proof(upgraded_proof)
    reparsed = parse_proof(original.serialize())

    def operations(node) -> list[bytes]:
        found = []
        for branch in node.branches():
            found.append(branch.operation)
            found.extend(operations(branch.child))
        return found

    assert operations(original.root) == operations(reparsed.root)
    assert operations(original.root)


def test_fixture_path_is_shared_with_the_anchor_tests() -> None:
    assert (
        FIXTURE
        == Path(__file__).resolve().parent / "fixtures" / "upgraded-two-anchors.ots"
    )
