"""Tests for strict timestamp-tag annotation validation.

Spec section 12 fixes the annotation schema: a ``git-ots schema: 1``
header plus ``source``, ``submitted-at``, ``proof``, and ``triggers``
fields. A strict parser must accept exactly that shape and reject
malformed input: unknown schema versions, missing or extra keys,
non-hex ``source`` values, naive or malformed ``submitted-at``
timestamps, ``proof`` paths that do not reference the source commit,
and empty or unknown ``triggers`` tokens.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from git_ots.git import (
    TimestampTag,
    TimestampTagAnnotationError,
    ValidatedAnnotation,
    validate_timestamp_tag,
)

COMMIT_ID = "0123456789abcdef0123456789abcdef01234567"
OTHER_COMMIT_ID = "ffffffffffffffffffffffffffffffffffffffff"


def _tag(annotation_text: str, *, commit_id: str = COMMIT_ID) -> TimestampTag:
    """Build a ``TimestampTag`` from a raw annotation body.

    The body is split into ``key: value`` pairs the same way the
    permissive enumerator does; this helper exists so the strict
    validator can be exercised without going through Git.
    """

    pairs: list[tuple[str, str]] = []
    for line in annotation_text.splitlines():
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not key:
            continue
        pairs.append((key, value.strip()))
    return TimestampTag(
        name=f"ots/20260808T220003Z/{commit_id[:12]}",
        commit_id=commit_id,
        annotation=tuple(pairs),
    )


VALID_ANNOTATION = (
    "git-ots schema: 1\n"
    f"source: {COMMIT_ID}\n"
    "submitted-at: 2026-08-08T22:00:03Z\n"
    f"proof: .opentimestamps/{COMMIT_ID}.ots\n"
    "triggers: fixed_time\n"
)


def test_valid_annotation_accepted() -> None:
    validated = validate_timestamp_tag(_tag(VALID_ANNOTATION))

    assert isinstance(validated, ValidatedAnnotation)
    assert validated.schema == 1
    assert validated.source == COMMIT_ID
    assert validated.submitted_at == datetime(2026, 8, 8, 22, 0, 3, tzinfo=UTC)
    assert validated.proof == f".opentimestamps/{COMMIT_ID}.ots"
    assert validated.triggers == frozenset({"fixed_time"})
    assert validated.producer is None


def test_schema_two_records_producer_version() -> None:
    text = VALID_ANNOTATION.replace(
        "git-ots schema: 1", "git-ots schema: 2\nproducer: git-ots 0.0.1"
    )
    validated = validate_timestamp_tag(_tag(text))
    assert validated.producer == "git-ots 0.0.1"


def test_multiple_triggers_accepted() -> None:
    text = (
        "git-ots schema: 1\n"
        f"source: {COMMIT_ID}\n"
        "submitted-at: 2026-08-08T22:00:03Z\n"
        f"proof: .opentimestamps/{COMMIT_ID}.ots\n"
        "triggers: max_age fixed_time\n"
    )
    validated = validate_timestamp_tag(_tag(text))
    assert validated.triggers == frozenset({"max_age", "fixed_time"})


def test_every_commit_trigger_accepted() -> None:
    text = (
        "git-ots schema: 1\n"
        f"source: {COMMIT_ID}\n"
        "submitted-at: 2026-08-08T22:00:03Z\n"
        f"proof: .opentimestamps/{COMMIT_ID}.ots\n"
        "triggers: every_commit\n"
    )
    validated = validate_timestamp_tag(_tag(text))
    assert validated.triggers == frozenset({"every_commit"})


def test_missing_required_key_rejected() -> None:
    for missing in (
        "git-ots schema",
        "source",
        "submitted-at",
        "proof",
        "triggers",
    ):
        lines = [
            line
            for line in VALID_ANNOTATION.splitlines()
            if not line.startswith(f"{missing}:")
        ]
        text = "\n".join(lines) + "\n"
        with pytest.raises(TimestampTagAnnotationError):
            validate_timestamp_tag(_tag(text))


def test_unknown_schema_rejected() -> None:
    text = VALID_ANNOTATION.replace("git-ots schema: 1", "git-ots schema: 2")
    with pytest.raises(TimestampTagAnnotationError):
        validate_timestamp_tag(_tag(text))


def test_non_integer_schema_rejected() -> None:
    text = VALID_ANNOTATION.replace("git-ots schema: 1", "git-ots schema: one")
    with pytest.raises(TimestampTagAnnotationError):
        validate_timestamp_tag(_tag(text))


def test_source_must_match_tag_target() -> None:
    text = VALID_ANNOTATION.replace(
        f"source: {COMMIT_ID}", f"source: {OTHER_COMMIT_ID}"
    )
    with pytest.raises(TimestampTagAnnotationError):
        validate_timestamp_tag(_tag(text))


def test_source_must_be_full_hex_id() -> None:
    text = VALID_ANNOTATION.replace(f"source: {COMMIT_ID}", "source: not-a-commit-id")
    with pytest.raises(TimestampTagAnnotationError):
        validate_timestamp_tag(_tag(text))


def test_naive_submitted_at_rejected() -> None:
    text = VALID_ANNOTATION.replace(
        "submitted-at: 2026-08-08T22:00:03Z",
        "submitted-at: 2026-08-08T22:00:03",
    )
    with pytest.raises(TimestampTagAnnotationError):
        validate_timestamp_tag(_tag(text))


def test_malformed_submitted_at_rejected() -> None:
    text = VALID_ANNOTATION.replace(
        "submitted-at: 2026-08-08T22:00:03Z",
        "submitted-at: not a timestamp",
    )
    with pytest.raises(TimestampTagAnnotationError):
        validate_timestamp_tag(_tag(text))


def test_proof_must_reference_source_commit() -> None:
    text = VALID_ANNOTATION.replace(
        f"proof: .opentimestamps/{COMMIT_ID}.ots",
        f"proof: .opentimestamps/{OTHER_COMMIT_ID}.ots",
    )
    with pytest.raises(TimestampTagAnnotationError):
        validate_timestamp_tag(_tag(text))


def test_proof_must_use_ots_suffix() -> None:
    text = VALID_ANNOTATION.replace(
        f"proof: .opentimestamps/{COMMIT_ID}.ots",
        f"proof: .opentimestamps/{COMMIT_ID}.txt",
    )
    with pytest.raises(TimestampTagAnnotationError):
        validate_timestamp_tag(_tag(text))


def test_empty_triggers_rejected() -> None:
    text = VALID_ANNOTATION.replace("triggers: fixed_time", "triggers:")
    with pytest.raises(TimestampTagAnnotationError):
        validate_timestamp_tag(_tag(text))


def test_unknown_trigger_rejected() -> None:
    text = VALID_ANNOTATION.replace("triggers: fixed_time", "triggers: arbitrary_token")
    with pytest.raises(TimestampTagAnnotationError):
        validate_timestamp_tag(_tag(text))


def test_unexpected_extra_keys_rejected() -> None:
    text = VALID_ANNOTATION + "comment: hi\n"
    with pytest.raises(TimestampTagAnnotationError):
        validate_timestamp_tag(_tag(text))


def test_duplicate_keys_rejected() -> None:
    text = VALID_ANNOTATION + "source: " + COMMIT_ID + "\n"
    with pytest.raises(TimestampTagAnnotationError):
        validate_timestamp_tag(_tag(text))
