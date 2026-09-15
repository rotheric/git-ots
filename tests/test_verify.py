"""Tests for offline validation and chain-backed verification commands.

Covers ADR D8: validate reports per-proof status without mutation or network
access, while verify delegates the final Bitcoin check.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from git_ots.cli import main
from git_ots.config import (
    PROOF_DIRECTORY,
    Config,
    GitConfig,
    OpenTimestampsConfig,
    PolicyConfig,
    ProofConfig,
)
from git_ots.timestamp import VerificationAttempt, build_payload
from tests.test_timestamp import _encode_varuint, _make_fake_detached_proof


def _git(args: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main", "."], cwd=path)
    _git(["config", "user.email", "test@example.com"], cwd=path)
    _git(["config", "user.name", "Test"], cwd=path)


def _commit(
    repo: Path, *, paths: list[str], message: str, date: datetime | None = None
) -> str:
    for rel in paths:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{rel}\n")
    _git(["add", *paths], cwd=repo)
    env = None
    if date is not None:
        env = {
            **os.environ,
            "GIT_COMMITTER_DATE": date.strftime("%Y-%m-%d %H:%M:%S %z"),
        }
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return _git(["rev-parse", "HEAD"], cwd=repo).strip()


def _fresh_config() -> Config:
    return Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=None,
            fixed_time=None,
            timezone=None,
            initial_history="latest",
        ),
        git=GitConfig(
            source_ref="HEAD",
            fetch_before_run=False,
            tag_prefix="ots/",
            require_clean_worktree=False,
        ),
        proof=ProofConfig(
            commit=True,
        ),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )


def _write_config(repo: Path, config: Config) -> None:
    config_path = repo / "git-ots.toml"
    lines = [
        "[policy]\n",
        'max_age = "24h"\n',
        "[git]\n",
        f'source_ref = "{config.git.source_ref}"\n',
        "fetch_before_run = false\n",
        f'tag_prefix = "{config.git.tag_prefix}"\n',
        "require_clean_worktree = false\n",
        "[proof]\n",
        f'directory = "{PROOF_DIRECTORY}"\n',
        "commit = true\n",
        "[opentimestamps]\n",
        f'command = "{config.opentimestamps.command}"\n',
    ]
    config_path.write_text("".join(lines), encoding="utf-8")


def _make_bitcoin_attested_proof(payload: bytes) -> bytes:
    """Return a structurally valid fake proof with a Bitcoin attestation."""
    header = b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"
    version = b"\x01"
    file_hash_op_tag = 0x08
    digest = __import__("hashlib").new("sha256", payload).digest()
    # BitcoinBlockHeaderAttestation.TAG from python-opentimestamps.
    bitcoin_tag = bytes.fromhex("0588960d73d71901")
    # Attestations are serialized as TAG || varbytes(payload). The Bitcoin
    # attestation payload is itself a varuint block height.
    inner_payload = _encode_varuint(800_000)
    attestation = bitcoin_tag + _encode_varuint(len(inner_payload)) + inner_payload
    timestamp = b"\x00" + attestation
    return header + version + bytes([file_hash_op_tag]) + digest + timestamp


def _write_proof_and_manifest(
    repo: Path,
    commit_id: str,
    proof_bytes: bytes,
    source_ref: str = "refs/heads/main",
) -> tuple[Path, Path]:
    """Write a proof/manifest pair for ``commit_id`` into the proof directory."""
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir(parents=True, exist_ok=True)
    proof_path = proof_dir / f"{commit_id}.ots"
    manifest_path = proof_dir / f"{commit_id}.json"
    manifest = {
        "schema": 1,
        "source_commit": commit_id,
        "git_object_format": "sha1",
        "payload_format": "git-commit-id-v1",
        "submitted_at": "2026-08-09T00:00:03Z",
        "proof": f"{commit_id}.ots",
        "source_ref": source_ref,
        "triggers": ["max_age"],
    }
    proof_path.write_bytes(proof_bytes)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return proof_path, manifest_path


def _repo_state_hashes(repo: Path) -> tuple[str, str, list[str]]:
    """Return (HEAD, tag list, proof-dir listing) for mutation checks."""
    head = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    tags = _git(["tag", "-l"], cwd=repo).strip()
    proof_dir = repo / ".opentimestamps"
    listing = sorted(p.name for p in proof_dir.iterdir()) if proof_dir.exists() else []
    return head, tags, listing


def test_validate_discovers_tracked_proofs_outside_conventional_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")
    proof, manifest = _write_proof_and_manifest(
        repo, source_id, _make_bitcoin_attested_proof(build_payload("sha1", source_id))
    )
    alternate = repo / "historical-proofs"
    alternate.mkdir()
    proof.rename(alternate / proof.name)
    manifest.rename(alternate / manifest.name)
    (repo / ".opentimestamps").rmdir()
    _git(["add", "historical-proofs"], cwd=repo)
    _git(["commit", "-q", "-m", "Store proofs"], cwd=repo)

    assert main(["validate"], cwd=repo) == 0
    assert "historical-proofs/" in capsys.readouterr().out


def test_explicit_proof_directory_takes_precedence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n")
    assert main(["validate", "--proof-dir", "chosen"], cwd=repo) == 0
    assert "nothing was validated" in capsys.readouterr().out


def test_explicit_proof_directory_cannot_escape_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n")
    assert main(["validate", "--proof-dir", "../outside"], cwd=repo) == 2


def test_validate_valid_proof_reports_valid_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(repo, paths=["a.txt"], message="A\n")
    _commit(repo, paths=["b.txt"], message="B\n")
    payload = build_payload("sha1", a_id)
    proof_bytes = _make_bitcoin_attested_proof(payload)
    _write_proof_and_manifest(repo, a_id, proof_bytes)
    _write_config(repo, _fresh_config())

    before = _repo_state_hashes(repo)
    exit_code = main(["validate"], cwd=str(repo))
    after = _repo_state_hashes(repo)

    assert exit_code == 0
    assert "valid" in capsys.readouterr().out
    assert after == before


def test_validate_pending_attestation_reports_pending_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(repo, paths=["a.txt"], message="A\n")
    _commit(repo, paths=["b.txt"], message="B\n")
    payload = build_payload("sha1", a_id)
    proof_bytes = _make_fake_detached_proof(payload)
    _write_proof_and_manifest(repo, a_id, proof_bytes)
    _write_config(repo, _fresh_config())

    before = _repo_state_hashes(repo)
    exit_code = main(["validate"], cwd=str(repo))
    after = _repo_state_hashes(repo)

    assert exit_code == 0
    assert "pending-attestation" in capsys.readouterr().out
    assert after == before


def test_validate_orphaned_proof_reports_orphaned_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(repo, paths=["a.txt"], message="A\n")
    payload = build_payload("sha1", a_id)
    proof_bytes = _make_bitcoin_attested_proof(payload)
    _write_proof_and_manifest(repo, a_id, proof_bytes)

    # Create an unrelated orphan commit and make it the current source.
    orphan_date = datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC)
    orphan_id = subprocess.run(
        ["git", "commit-tree", "HEAD^{tree}", "-m", "Orphan"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": orphan_date.isoformat(),
            "GIT_COMMITTER_DATE": orphan_date.isoformat(),
        },
    ).stdout.strip()
    _git(["checkout", "-q", orphan_id], cwd=repo)
    _write_config(repo, _fresh_config())

    before = _repo_state_hashes(repo)
    exit_code = main(["validate"], cwd=str(repo))
    after = _repo_state_hashes(repo)

    assert exit_code == 0
    assert "orphaned" in capsys.readouterr().out
    assert after == before


def test_validate_invalid_proof_reports_invalid_and_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(repo, paths=["a.txt"], message="A\n")
    _commit(repo, paths=["b.txt"], message="B\n")
    # Structurally valid proof, but bound to a different commit.
    other_id = "abcdef0123456789abcdef0123456789abcdef01"
    payload = build_payload("sha1", other_id)
    proof_bytes = _make_fake_detached_proof(payload)
    _write_proof_and_manifest(repo, a_id, proof_bytes)
    _write_config(repo, _fresh_config())

    before = _repo_state_hashes(repo)
    exit_code = main(["validate"], cwd=str(repo))
    after = _repo_state_hashes(repo)

    assert exit_code != 0
    assert "invalid" in capsys.readouterr().out
    assert after == before


def test_validate_unknown_source_reports_distinct_state_and_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n")
    _commit(repo, paths=["b.txt"], message="B\n")
    fake_id = "deadbeef" * 5  # 40 hex chars, not a real commit.
    payload = build_payload("sha1", fake_id)
    proof_bytes = _make_bitcoin_attested_proof(payload)
    _write_proof_and_manifest(repo, fake_id, proof_bytes)
    _write_config(repo, _fresh_config())

    before = _repo_state_hashes(repo)
    exit_code = main(["validate"], cwd=str(repo))
    after = _repo_state_hashes(repo)

    assert exit_code != 0
    output = capsys.readouterr().out
    assert "unknown-source" in output
    assert after == before


def test_verify_delegates_bitcoin_check_and_reports_verified(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")
    payload = build_payload("sha1", source_id)
    proof_bytes = _make_bitcoin_attested_proof(payload)
    _write_proof_and_manifest(repo, source_id, proof_bytes)
    _write_config(repo, _fresh_config())

    calls: list[tuple[bytes, bytes]] = []

    class FakeClient:
        def __init__(self, command: str, *, limit=None) -> None:
            assert command == "ots"

        def verify(self, proof: bytes, subject: bytes) -> VerificationAttempt:
            calls.append((proof, subject))
            return VerificationAttempt(
                verified=True, detail="Success! Bitcoin block 800000 attests data"
            )

    monkeypatch.setattr("git_ots.cli.OpenTimestampsCli", FakeClient)

    before = _repo_state_hashes(repo)
    exit_code = main(["verify"], cwd=repo)

    assert exit_code == 0
    assert calls == [(proof_bytes, payload)]
    assert "verified" in capsys.readouterr().out
    assert _repo_state_hashes(repo) == before


def test_verify_reports_client_rejection_and_exits_four(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")
    payload = build_payload("sha1", source_id)
    _write_proof_and_manifest(repo, source_id, _make_bitcoin_attested_proof(payload))
    _write_config(repo, _fresh_config())

    class FakeClient:
        def __init__(self, command: str, *, limit=None) -> None:
            pass

        def verify(self, proof: bytes, subject: bytes) -> VerificationAttempt:
            return VerificationAttempt(
                verified=False, detail="Could not connect to Bitcoin node"
            )

    monkeypatch.setattr("git_ots.cli.OpenTimestampsCli", FakeClient)

    assert main(["verify"], cwd=repo) == 4
    output = capsys.readouterr().out
    assert "verification-failed" in output
    assert "Could not connect" in output


def test_verify_pending_proof_does_not_invoke_client(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")
    payload = build_payload("sha1", source_id)
    _write_proof_and_manifest(repo, source_id, _make_fake_detached_proof(payload))
    _write_config(repo, _fresh_config())

    class FakeClient:
        def __init__(self, command: str, *, limit=None) -> None:
            pass

        def verify(self, proof: bytes, subject: bytes) -> VerificationAttempt:
            raise AssertionError("pending proof must not reach ots verify")

    monkeypatch.setattr("git_ots.cli.OpenTimestampsCli", FakeClient)

    assert main(["verify"], cwd=repo) == 4
    assert "pending-attestation" in capsys.readouterr().out


def test_verify_invalid_proof_is_not_checked_against_bitcoin(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")
    other_payload = build_payload("sha1", "abcdef0123456789abcdef0123456789abcdef01")
    _write_proof_and_manifest(
        repo, source_id, _make_bitcoin_attested_proof(other_payload)
    )
    _write_config(repo, _fresh_config())

    class FakeClient:
        def __init__(self, command: str, *, limit=None) -> None:
            pass

        def verify(self, proof: bytes, subject: bytes) -> VerificationAttempt:
            raise AssertionError("invalid proof must not reach ots verify")

    monkeypatch.setattr("git_ots.cli.OpenTimestampsCli", FakeClient)

    assert main(["verify"], cwd=repo) == 5
    output = capsys.readouterr().out
    assert "not-checked" in output
    assert "does not bind" in output
