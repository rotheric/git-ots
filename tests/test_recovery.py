"""Tests for read-only recovery artifact inspection.

A matching proof/manifest pair is accepted; malformed
JSON, missing proofs, mismatched commits, mismatched object formats, and
zero-byte proofs are all rejected. This implements the read-only half of
spec section 22.1 ("Proof exists, tag absent").
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from git_ots.timestamp import (
    RecoveryValidationError,
    build_manifest,
    build_payload,
    inspect_recovery_artifacts,
)
from tests.test_timestamp import _make_fake_detached_proof

_COMMIT_ID = "0123456789abcdef0123456789abcdef01234567"
_SUBMITTED_AT = datetime(2026, 8, 9, 0, 0, 3, tzinfo=UTC)
_PROOF_BYTES = _make_fake_detached_proof(payload=build_payload("sha1", _COMMIT_ID))
_DELETE_KEY = object()


def _write_pair(
    proof_dir: Path,
    *,
    commit_id: str = _COMMIT_ID,
    object_format: str = "sha1",
    proof_bytes: bytes = _PROOF_BYTES,
    manifest_overrides: dict | None = None,
    raw_manifest: bytes | None = None,
) -> tuple[Path, Path]:
    """Persist a proof and manifest pair directly for the test.

    Returns ``(proof_path, manifest_path)``. The manifest is built with the
    real :func:`build_manifest` so the accepted case always matches the
    documented schema, then selectively overridden (or keys removed with
    ``_DELETE_KEY``) when a test wants a corrupt or mismatched manifest.
    """
    proof_dir.mkdir(parents=True, exist_ok=True)
    proof_path = proof_dir / f"{commit_id}.ots"
    manifest_path = proof_dir / f"{commit_id}.json"
    proof_path.write_bytes(proof_bytes)
    if raw_manifest is not None:
        manifest_path.write_bytes(raw_manifest)
    else:
        build_kwargs = {
            "object_format": object_format,
            "commit_id": commit_id,
            "proof_name": f"{commit_id}.ots",
            "source_ref": "origin/main",
            "submitted_at": _SUBMITTED_AT,
            "triggers": ["fixed_time"],
        }
        build_arg_names = set(build_kwargs)
        overrides = manifest_overrides or {}

        def _is_valid_build_value(key: str, value: object) -> bool:
            if value is _DELETE_KEY:
                return False
            if key == "submitted_at":
                return isinstance(value, datetime)
            if key == "triggers":
                return isinstance(value, (list, tuple))
            return isinstance(value, str)

        for key, value in overrides.items():
            if key in build_arg_names and _is_valid_build_value(key, value):
                build_kwargs[key] = value
        manifest = build_manifest(**build_kwargs)
        for key, value in overrides.items():
            if key in build_arg_names:
                if value is _DELETE_KEY:
                    manifest.pop(key, None)
                elif not _is_valid_build_value(key, value):
                    manifest[key] = value
            else:
                if value is _DELETE_KEY:
                    manifest.pop(key, None)
                else:
                    manifest[key] = value
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
    return proof_path, manifest_path


def test_inspect_recovery_artifacts_accepts_matching_pair(tmp_path: Path):
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir)

    artifacts = inspect_recovery_artifacts(
        repository_root=tmp_path,
        proof_directory=Path(".opentimestamps"),
        object_format="sha1",
        commit_id=_COMMIT_ID,
    )

    assert artifacts.proof_path == proof_dir / f"{_COMMIT_ID}.ots"
    assert artifacts.manifest_path == proof_dir / f"{_COMMIT_ID}.json"
    assert artifacts.proof_bytes == _PROOF_BYTES
    assert artifacts.manifest["source_commit"] == _COMMIT_ID
    assert artifacts.manifest["git_object_format"] == "sha1"
    assert artifacts.manifest["proof"] == f"{_COMMIT_ID}.ots"


def test_inspect_recovery_artifacts_is_read_only(tmp_path: Path):
    """Inspection must not modify, create, or delete files on disk."""
    proof_dir = tmp_path / ".opentimestamps"
    proof_path, manifest_path = _write_pair(proof_dir)
    before_proof = proof_path.read_bytes()
    before_manifest = manifest_path.read_bytes()
    before_listing = sorted(p.name for p in proof_dir.iterdir())

    inspect_recovery_artifacts(
        repository_root=tmp_path,
        proof_directory=Path(".opentimestamps"),
        object_format="sha1",
        commit_id=_COMMIT_ID,
    )

    assert proof_path.read_bytes() == before_proof
    assert manifest_path.read_bytes() == before_manifest
    assert sorted(p.name for p in proof_dir.iterdir()) == before_listing


def test_inspect_recovery_artifacts_rejects_missing_proof(tmp_path: Path):
    proof_dir = tmp_path / ".opentimestamps"
    proof_dir.mkdir()
    manifest = build_manifest(
        object_format="sha1",
        commit_id=_COMMIT_ID,
        proof_name=f"{_COMMIT_ID}.ots",
        source_ref="origin/main",
        submitted_at=_SUBMITTED_AT,
        triggers=["fixed_time"],
    )
    (proof_dir / f"{_COMMIT_ID}.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_missing_manifest(tmp_path: Path):
    proof_dir = tmp_path / ".opentimestamps"
    proof_dir.mkdir()
    (proof_dir / f"{_COMMIT_ID}.ots").write_bytes(_PROOF_BYTES)

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_zero_byte_proof(tmp_path: Path):
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir, proof_bytes=b"")

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_malformed_manifest_json(tmp_path: Path):
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir, raw_manifest=b"{ this is not json")

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_wrong_source_commit(tmp_path: Path):
    """A manifest describing a different commit must not match this recovery."""
    proof_dir = tmp_path / ".opentimestamps"
    other_id = "f" * 40
    _write_pair(proof_dir, manifest_overrides={"commit_id": other_id})

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_wrong_object_format(tmp_path: Path):
    """A manifest declaring ``sha256`` must not match a sha1 recovery."""
    proof_dir = tmp_path / ".opentimestamps"
    proof_dir.mkdir()
    (proof_dir / f"{_COMMIT_ID}.ots").write_bytes(_PROOF_BYTES)
    # Write a manifest that internally claims sha256 (overriding after
    # build_manifest so the commit-id length check doesn't fire during
    # setup). The point is that the on-disk manifest is inconsistent with
    # the caller's object_format.
    manifest = build_manifest(
        object_format="sha1",
        commit_id=_COMMIT_ID,
        proof_name=f"{_COMMIT_ID}.ots",
        source_ref="origin/main",
        submitted_at=_SUBMITTED_AT,
        triggers=["fixed_time"],
    )
    manifest["git_object_format"] = "sha256"
    (proof_dir / f"{_COMMIT_ID}.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_caller_mismatched_object_format(
    tmp_path: Path,
):
    """The caller-supplied object_format must match the manifest's."""
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir)

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha256",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_non_object_manifest(tmp_path: Path):
    """A manifest that is not a JSON object must be rejected, even if valid JSON."""
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir, raw_manifest=b"[1, 2, 3]\n")

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_missing_schema(tmp_path: Path):
    """A manifest without a schema field is not a valid recovery artifact."""
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir, manifest_overrides={"schema": _DELETE_KEY})

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_invalid_schema(tmp_path: Path):
    """Only schema version 1 is accepted for recovery."""
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir, manifest_overrides={"schema": 2})

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_missing_payload_format(tmp_path: Path):
    """A manifest without the payload format cannot describe a canonical submission."""
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir, manifest_overrides={"payload_format": _DELETE_KEY})

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_invalid_payload_format(tmp_path: Path):
    """Only the documented payload format is accepted for recovery."""
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir, manifest_overrides={"payload_format": "other"})

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_missing_submitted_at(tmp_path: Path):
    """A manifest without a submission timestamp cannot preserve original evidence."""
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir, manifest_overrides={"submitted_at": _DELETE_KEY})

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_malformed_submitted_at(tmp_path: Path):
    """A malformed submitted_at cannot be used to recreate the timestamp tag."""
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir, manifest_overrides={"submitted_at": "not-a-timestamp"})

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_non_utc_submitted_at(tmp_path: Path):
    """submitted_at must be UTC RFC 3339 so tag names are deterministic."""
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(
        proof_dir, manifest_overrides={"submitted_at": "2026-08-09T00:00:03+02:00"}
    )

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_missing_proof_filename(tmp_path: Path):
    """A manifest must name the proof file to link metadata to cryptographic evidence."""
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir, manifest_overrides={"proof": _DELETE_KEY})

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_wrong_proof_filename(tmp_path: Path):
    """The manifest proof filename must identify the selected commit."""
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir, manifest_overrides={"proof": "wrong.ots"})

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_missing_source_ref(tmp_path: Path):
    """A manifest must record the concrete source ref that was frozen."""
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir, manifest_overrides={"source_ref": None})

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_empty_source_ref(tmp_path: Path):
    """An empty source_ref is not a valid concrete ref."""
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir, manifest_overrides={"source_ref": ""})

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_missing_triggers(tmp_path: Path):
    """A manifest must record why the submission happened."""
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir, manifest_overrides={"triggers": _DELETE_KEY})

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_empty_triggers(tmp_path: Path):
    """An empty trigger list cannot recreate the timestamp tag annotation."""
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir, manifest_overrides={"triggers": []})

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_unknown_trigger(tmp_path: Path):
    """Only known policy triggers are accepted in a recovery manifest."""
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir, manifest_overrides={"triggers": ["unknown"]})

    with pytest.raises(RecoveryValidationError):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_exposes_manifest_datetime_and_triggers(
    tmp_path: Path,
):
    """A valid recovery artifact exposes the original submitted_at and triggers."""
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir)

    artifacts = inspect_recovery_artifacts(
        repository_root=tmp_path,
        proof_directory=Path(".opentimestamps"),
        object_format="sha1",
        commit_id=_COMMIT_ID,
    )

    assert artifacts.submitted_at == _SUBMITTED_AT
    assert artifacts.triggers == frozenset({"fixed_time"})


def test_inspect_recovery_artifacts_rejects_mismatched_payload_binding(
    tmp_path: Path,
):
    """A structurally valid proof for a different source payload is rejected."""
    proof_dir = tmp_path / ".opentimestamps"
    other_commit = "abcdef0123456789abcdef0123456789abcdef01"
    other_payload = build_payload("sha1", other_commit)
    proof_bytes = _make_fake_detached_proof(payload=other_payload)
    _write_pair(proof_dir, proof_bytes=proof_bytes)

    with pytest.raises(RecoveryValidationError, match="payload"):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )


def test_inspect_recovery_artifacts_rejects_malformed_proof_bytes(
    tmp_path: Path,
):
    """Non-OTS proof bytes cannot be recovered as cryptographic evidence."""
    proof_dir = tmp_path / ".opentimestamps"
    _write_pair(proof_dir, proof_bytes=b"not an ots proof")

    with pytest.raises(RecoveryValidationError, match="OpenTimestamps"):
        inspect_recovery_artifacts(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
        )
