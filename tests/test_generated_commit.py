"""Tests for classifying generated proof commits by trailer (ADR D3).

A commit is non-meaningful (an ``git-ots``-generated proof commit) when its
commit message carries the canonical ``OpenTimestamps-Generated: true``
trailer. Proof-directory path inspection is no longer part of the
classification, but the tool warns when a trailer-bearing commit also changes
paths outside the configured proof directory so the case is visible instead of
silent.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from git_ots.git import (
    InvalidRepositoryStateError,
    is_generated_proof_commit,
)

PROOF_DIR = ".opentimestamps"
GENERATED_MESSAGE = "Store OpenTimestamps proof\n\nOpenTimestamps-Generated: true\n"


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
    (path / "README").write_text("seed\n")
    _git(["add", "README"], cwd=path)
    _git(["commit", "-q", "-m", "seed"], cwd=path)


def _commit(repo: Path, *, paths: list[str], message: str) -> str:
    """Create a commit that adds/modifies the given paths with ``message``."""
    for rel in paths:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{rel}\n")
    _git(["add", *paths], cwd=repo)
    _git(["commit", "-q", "-m", message], cwd=repo)
    return _git(["rev-parse", "HEAD"], cwd=repo).strip()


def test_trailer_and_proof_only_changes_is_generated(tmp_path: Path) -> None:
    # Canonical case: a commit with the trailer that touches only paths
    # inside the proof directory is generated (non-meaningful).
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_id = _commit(
        repo,
        paths=[f"{PROOF_DIR}/abc.ots", f"{PROOF_DIR}/abc.json"],
        message=GENERATED_MESSAGE,
    )

    assert (
        is_generated_proof_commit(cwd=repo, commit=commit_id, proof_directory=PROOF_DIR)
        is True
    )


def test_trailer_with_outside_paths_is_generated_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A trailer-bearing commit that also modifies non-proof files is still
    # classified as generated under trailer-only recognition, but the tool
    # warns about the outside paths (ADR D3).
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_id = _commit(
        repo,
        paths=[f"{PROOF_DIR}/abc.ots", "src/code.py"],
        message=GENERATED_MESSAGE,
    )

    with caplog.at_level(logging.WARNING, logger="git_ots"):
        result = is_generated_proof_commit(
            cwd=repo, commit=commit_id, proof_directory=PROOF_DIR
        )

    assert result is True
    assert any("changes paths outside" in rec.message for rec in caplog.records)


def test_trailer_with_only_non_proof_paths_is_generated_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Trailer present and no proof paths at all: still generated, with a
    # warning that every changed path is outside the proof directory.
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_id = _commit(
        repo,
        paths=["src/code.py"],
        message=GENERATED_MESSAGE,
    )

    with caplog.at_level(logging.WARNING, logger="git_ots"):
        result = is_generated_proof_commit(
            cwd=repo, commit=commit_id, proof_directory=PROOF_DIR
        )

    assert result is True
    assert any("changes paths outside" in rec.message for rec in caplog.records)


def test_proof_only_changes_without_trailer_are_meaningful(
    tmp_path: Path,
) -> None:
    # An ordinary user commit that edits proof files without the generated
    # trailer is meaningful.
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_id = _commit(
        repo,
        paths=[f"{PROOF_DIR}/abc.ots"],
        message="Manually update proof\n",
    )

    assert (
        is_generated_proof_commit(cwd=repo, commit=commit_id, proof_directory=PROOF_DIR)
        is False
    )


def test_no_trailer_and_non_proof_changes_is_meaningful(tmp_path: Path) -> None:
    # Plain user commit: no trailer means meaningful.
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_id = _commit(
        repo,
        paths=["src/code.py"],
        message="Implement a feature\n",
    )

    assert (
        is_generated_proof_commit(cwd=repo, commit=commit_id, proof_directory=PROOF_DIR)
        is False
    )


def test_proof_subdirectory_paths_are_inside_proof_dir(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Paths nested further inside the proof directory are still proof-only;
    # no warning is emitted.
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_id = _commit(
        repo,
        paths=[f"{PROOF_DIR}/sub/dir/abc.ots"],
        message=GENERATED_MESSAGE,
    )

    with caplog.at_level(logging.WARNING, logger="git_ots"):
        result = is_generated_proof_commit(
            cwd=repo, commit=commit_id, proof_directory=PROOF_DIR
        )

    assert result is True
    assert not any("changes paths outside" in rec.message for rec in caplog.records)


def test_sibling_directory_with_similar_prefix_is_generated_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # ``.opentimestamps-evil/x`` is outside ``.opentimestamps``; the trailer
    # still makes the commit generated, but a warning is emitted.
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_id = _commit(
        repo,
        paths=[f"{PROOF_DIR}-evil/abc.ots"],
        message=GENERATED_MESSAGE,
    )

    with caplog.at_level(logging.WARNING, logger="git_ots"):
        result = is_generated_proof_commit(
            cwd=repo, commit=commit_id, proof_directory=PROOF_DIR
        )

    assert result is True
    assert any("changes paths outside" in rec.message for rec in caplog.records)


def test_unknown_commit_raises_invalid_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    try:
        is_generated_proof_commit(
            cwd=repo,
            commit="refs/heads/does-not-exist",
            proof_directory=PROOF_DIR,
        )
    except InvalidRepositoryStateError:
        return
    raise AssertionError("expected InvalidRepositoryStateError")
