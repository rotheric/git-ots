import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from git_ots.timestamp import build_manifest, persist_manifest

_COMMIT_ID = "0123456789abcdef0123456789abcdef01234567"
_SUBMITTED_AT = datetime(2026, 8, 9, 0, 0, 3, tzinfo=UTC)


def _manifest_dict(**overrides):
    kwargs = {
        "object_format": "sha1",
        "commit_id": _COMMIT_ID,
        "proof_name": f"{_COMMIT_ID}.ots",
        "source_ref": "origin/main",
        "submitted_at": _SUBMITTED_AT,
        "triggers": ["fixed_time"],
    }
    kwargs.update(overrides)
    return build_manifest(**kwargs)


def test_build_manifest_includes_every_schema1_field():
    manifest = _manifest_dict()
    assert manifest["schema"] == 1
    assert manifest["source_commit"] == _COMMIT_ID
    assert manifest["git_object_format"] == "sha1"
    assert manifest["payload_format"] == "git-commit-id-v1"
    assert manifest["submitted_at"] == "2026-08-09T00:00:03Z"
    assert manifest["proof"] == f"{_COMMIT_ID}.ots"
    assert manifest["source_ref"] == "origin/main"
    assert manifest["triggers"] == ["fixed_time"]


def test_build_manifest_proof_is_relative_filename_only():
    """The manifest records the proof name, not a path or absolute location."""
    manifest = _manifest_dict(proof_name=f"{_COMMIT_ID}.ots")
    assert manifest["proof"] == f"{_COMMIT_ID}.ots"
    assert "/" not in manifest["proof"]
    assert not manifest["proof"].startswith(".")


def test_build_manifest_submitted_at_is_utc_rfc3339():
    """The output uses RFC 3339 with the ``Z`` suffix, not an offset."""
    manifest = _manifest_dict()
    assert manifest["submitted_at"] == "2026-08-09T00:00:03Z"


def test_build_manifest_converts_non_utc_to_utc():
    """A non-UTC aware datetime is normalized to UTC before serialization."""
    from zoneinfo import ZoneInfo

    berlin = datetime(2026, 8, 9, 2, 0, 3, tzinfo=ZoneInfo("Europe/Berlin"))
    manifest = _manifest_dict(submitted_at=berlin)
    assert manifest["submitted_at"] == "2026-08-09T00:00:03Z"


def test_build_manifest_triggers_are_sorted():
    """Trigger order must be deterministic regardless of input order."""
    manifest = _manifest_dict(triggers=["fixed_time", "max_age", "every_commit"])
    assert manifest["triggers"] == ["every_commit", "fixed_time", "max_age"]


def test_build_manifest_triggers_sorted_independent_of_input_order():
    a = _manifest_dict(triggers=["max_age", "fixed_time"])
    b = _manifest_dict(triggers=["fixed_time", "max_age"])
    assert a["triggers"] == b["triggers"] == ["fixed_time", "max_age"]


def test_build_manifest_rejects_naive_submitted_at():
    naive = datetime(2026, 8, 9, 0, 0, 3)  # noqa: DTZ001
    with pytest.raises(ValueError):
        _manifest_dict(submitted_at=naive)


def test_build_manifest_is_deterministic_for_same_inputs():
    a = _manifest_dict()
    b = _manifest_dict()
    assert a == b


def test_build_manifest_supports_sha256():
    commit_id = "a" * 64
    manifest = _manifest_dict(
        object_format="sha256",
        commit_id=commit_id,
        proof_name=f"{commit_id}.ots",
    )
    assert manifest["git_object_format"] == "sha256"
    assert manifest["source_commit"] == commit_id
    assert manifest["proof"] == f"{commit_id}.ots"


def test_persist_manifest_writes_parseable_json_under_full_sha(tmp_path: Path):
    proof_dir = tmp_path / ".opentimestamps"
    proof_dir.mkdir()
    manifest = _manifest_dict()

    final_path = persist_manifest(
        repository_root=tmp_path,
        proof_directory=Path(".opentimestamps"),
        manifest=manifest,
    )

    assert final_path == proof_dir / f"{_COMMIT_ID}.json"
    assert final_path.exists()
    parsed = json.loads(final_path.read_text(encoding="utf-8"))
    assert parsed == manifest


def test_persist_manifest_uses_stable_formatting_and_trailing_newline(tmp_path: Path):
    """The on-disk form is deterministic: pretty-printed and LF-terminated."""
    proof_dir = tmp_path / ".opentimestamps"
    proof_dir.mkdir()
    manifest = _manifest_dict()

    final_path = persist_manifest(
        repository_root=tmp_path,
        proof_directory=Path(".opentimestamps"),
        manifest=manifest,
    )

    raw = final_path.read_bytes()
    assert raw.endswith(b"\n")
    assert b"\r" not in raw
    # Two-space indentation is stable and diff-friendly.
    assert b'  "schema": 1' in raw
    # Re-running with the same manifest produces identical bytes.
    again = persist_manifest(
        repository_root=tmp_path,
        proof_directory=Path(".opentimestamps"),
        manifest=manifest,
    )
    assert again == final_path
    assert final_path.read_bytes() == raw


def test_persist_manifest_leaves_no_temporary_files(tmp_path: Path):
    proof_dir = tmp_path / ".opentimestamps"
    proof_dir.mkdir()

    persist_manifest(
        repository_root=tmp_path,
        proof_directory=Path(".opentimestamps"),
        manifest=_manifest_dict(),
    )

    names = [entry.name for entry in proof_dir.iterdir()]
    assert names == [f"{_COMMIT_ID}.json"]


def test_persist_manifest_refuses_to_overwrite_different_manifest(tmp_path: Path):
    """An existing manifest with different content must not be overwritten."""
    from git_ots.timestamp import PersistenceError

    proof_dir = tmp_path / ".opentimestamps"
    proof_dir.mkdir()
    original = _manifest_dict()

    final_path = persist_manifest(
        repository_root=tmp_path,
        proof_directory=Path(".opentimestamps"),
        manifest=original,
    )
    original_bytes = final_path.read_bytes()

    different = _manifest_dict(triggers=["max_age"])
    with pytest.raises(PersistenceError):
        persist_manifest(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            manifest=different,
        )

    assert final_path.read_bytes() == original_bytes
