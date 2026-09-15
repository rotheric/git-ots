import os
from pathlib import Path

import pytest

from git_ots.timestamp import (
    PersistenceError,
    build_payload,
    persist_proof,
)
from tests.test_timestamp import _make_fake_detached_proof

_COMMIT_ID = "0123456789abcdef0123456789abcdef01234567"


def _valid_proof() -> bytes:
    """Return a fake but valid detached proof bound to the test commit."""
    return _make_fake_detached_proof(payload=build_payload("sha1", _COMMIT_ID))


def test_persist_proof_writes_exact_bytes_under_full_sha(tmp_path: Path):
    proof_dir = tmp_path / ".opentimestamps"
    proof_dir.mkdir()
    proof_bytes = _valid_proof()

    final_path = persist_proof(
        repository_root=tmp_path,
        proof_directory=Path(".opentimestamps"),
        object_format="sha1",
        commit_id=_COMMIT_ID,
        proof_bytes=proof_bytes,
    )

    assert final_path == proof_dir / f"{_COMMIT_ID}.ots"
    assert final_path.exists()
    assert final_path.read_bytes() == proof_bytes


def test_persist_proof_leaves_no_temporary_files(tmp_path: Path):
    proof_dir = tmp_path / ".opentimestamps"
    proof_dir.mkdir()
    proof_bytes = _valid_proof()

    persist_proof(
        repository_root=tmp_path,
        proof_directory=Path(".opentimestamps"),
        object_format="sha1",
        commit_id=_COMMIT_ID,
        proof_bytes=proof_bytes,
    )

    names = [entry.name for entry in proof_dir.iterdir()]
    assert names == [f"{_COMMIT_ID}.ots"]


def test_persist_proof_uses_same_directory_for_temp_and_final(
    tmp_path: Path, monkeypatch
):
    """The temp file must live in the proof directory so ``os.replace`` is atomic.

    Cross-device renames silently fall back to copy+delete on some platforms,
    which is not atomic. Spy on ``os.replace`` to confirm the source path is
    inside the proof directory.
    """
    proof_dir = tmp_path / ".opentimestamps"
    proof_dir.mkdir()
    replaced: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def spy_replace(src, dst):
        replaced.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)

    persist_proof(
        repository_root=tmp_path,
        proof_directory=Path(".opentimestamps"),
        object_format="sha1",
        commit_id=_COMMIT_ID,
        proof_bytes=_valid_proof(),
    )

    assert len(replaced) == 1
    src, dst = replaced[0]
    assert src.parent == proof_dir
    assert dst.parent == proof_dir


def test_persist_proof_rejects_empty_bytes(tmp_path: Path):
    (tmp_path / ".opentimestamps").mkdir()
    with pytest.raises(PersistenceError):
        persist_proof(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
            proof_bytes=b"",
        )


def test_persist_proof_refuses_to_overwrite_different_bytes(tmp_path: Path):
    """An existing proof with different bytes must not be overwritten."""
    proof_dir = tmp_path / ".opentimestamps"
    proof_dir.mkdir()
    final_path = proof_dir / f"{_COMMIT_ID}.ots"
    original = _valid_proof()
    final_path.write_bytes(original)
    other_commit = "abcdef0123456789abcdef0123456789abcdef01"
    other_payload = build_payload("sha1", other_commit)
    different_proof = _make_fake_detached_proof(payload=other_payload)

    with pytest.raises(PersistenceError):
        persist_proof(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
            proof_bytes=different_proof,
        )

    assert final_path.read_bytes() == original


def test_persist_proof_is_idempotent_for_identical_bytes(tmp_path: Path):
    """Writing identical bytes to an existing proof is a no-op success."""
    proof_dir = tmp_path / ".opentimestamps"
    proof_dir.mkdir()
    proof_bytes = _valid_proof()
    final_path = proof_dir / f"{_COMMIT_ID}.ots"
    final_path.write_bytes(proof_bytes)

    result = persist_proof(
        repository_root=tmp_path,
        proof_directory=Path(".opentimestamps"),
        object_format="sha1",
        commit_id=_COMMIT_ID,
        proof_bytes=proof_bytes,
    )

    assert result == final_path
    assert final_path.read_bytes() == proof_bytes


def test_persist_proof_rejects_missing_proof_directory(tmp_path: Path):
    """The proof directory must already exist; this layer does not create it."""
    with pytest.raises(PersistenceError):
        persist_proof(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
            proof_bytes=_valid_proof(),
        )


def test_persist_proof_rejects_mismatched_payload_binding(tmp_path: Path):
    """A structurally valid proof for a different source payload is rejected."""
    proof_dir = tmp_path / ".opentimestamps"
    proof_dir.mkdir()
    other_commit = "abcdef0123456789abcdef0123456789abcdef01"
    other_payload = build_payload("sha1", other_commit)
    proof_bytes = _make_fake_detached_proof(payload=other_payload)

    with pytest.raises(PersistenceError, match="payload"):
        persist_proof(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
            proof_bytes=proof_bytes,
        )


def test_persist_proof_rejects_malformed_proof_bytes(tmp_path: Path):
    """Non-OTS proof bytes cannot be persisted as cryptographic evidence."""
    proof_dir = tmp_path / ".opentimestamps"
    proof_dir.mkdir()

    with pytest.raises(PersistenceError, match="OpenTimestamps"):
        persist_proof(
            repository_root=tmp_path,
            proof_directory=Path(".opentimestamps"),
            object_format="sha1",
            commit_id=_COMMIT_ID,
            proof_bytes=b"not an ots proof",
        )
