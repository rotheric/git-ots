"""Tests for creating generated proof commits.

When ``[proof] commit = true`` the tool commits the generated proof and
manifest files as a clearly machine-identifiable commit. The commit message
must contain the canonical ``OpenTimestamps-Generated: true`` trailer and
one ``OpenTimestamps-Source`` trailer per source commit (spec section 13).
The operation must commit only the paths that were already staged by the
caller; it must never pick up unrelated worktree changes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_ots.git import (
    GitCommandError,
    create_generated_proof_commit,
    stage_paths,
)


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


def _commit(repo: Path, *, paths: list[str], message: str) -> str:
    for rel in paths:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{rel}\n")
    _git(["add", *paths], cwd=repo)
    _git(["commit", "-q", "-m", message], cwd=repo)
    return _git(["rev-parse", "HEAD"], cwd=repo).strip()


def _changed_paths(repo: Path, commit_id: str) -> tuple[str, ...]:
    output = _git(
        [
            "diff-tree",
            "--root",
            "-r",
            "--no-commit-id",
            "--no-renames",
            "--name-only",
            "-z",
            commit_id,
        ],
        cwd=repo,
    )
    return tuple(sorted(p for p in output.split("\x00") if p))


def _stage_proof(repo: Path, commit_id: str) -> tuple[str, str]:
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir(exist_ok=True)
    ots = f".opentimestamps/{commit_id}.ots"
    json_path = f".opentimestamps/{commit_id}.json"
    (repo / ots).write_text("proof")
    (repo / json_path).write_text("manifest")
    stage_paths(cwd=repo, paths=[ots, json_path])
    return ots, json_path


def test_create_generated_proof_commit_single_source(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["README"], message="seed\n")
    a_id = _commit(repo, paths=["a.txt"], message="A\n")

    ots, json_path = _stage_proof(repo, a_id)

    # Unrelated worktree change that must not be swept into the generated commit.
    (repo / "unrelated.txt").write_text("dirty\n")

    commit_id = create_generated_proof_commit(
        cwd=repo,
        source_commit_ids=[a_id],
        proof_directory=".opentimestamps",
        paths=[ots, json_path],
    )

    message = _git(["log", "-1", "--format=%B", commit_id], cwd=repo)
    assert "OpenTimestamps-Generated: true\n" in message
    assert f"OpenTimestamps-Source: {a_id}\n" in message

    subject = message.split("\n", 1)[0]
    assert subject.startswith("Store OpenTimestamps proof for ")
    assert a_id[:12] in subject

    # Only the explicitly staged proof files are committed.
    changed = _changed_paths(repo, commit_id)
    assert sorted(changed) == sorted([ots, json_path])

    # The unrelated file remains a worktree modification.
    status = _git(["status", "--porcelain"], cwd=repo)
    assert "unrelated.txt" in status

    # HEAD moved forward to the generated commit, leaving seed/a behind it.
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == commit_id
    assert _git(["rev-parse", f"{commit_id}~1"], cwd=repo).strip() == a_id


def test_create_generated_proof_commit_batch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["README"], message="seed\n")
    a_id = _commit(repo, paths=["a.txt"], message="A\n")
    b_id = _commit(repo, paths=["b.txt"], message="B\n")
    c_id = _commit(repo, paths=["c.txt"], message="C\n")

    ots_a, json_a = _stage_proof(repo, a_id)
    ots_b, json_b = _stage_proof(repo, b_id)
    ots_c, json_c = _stage_proof(repo, c_id)

    commit_id = create_generated_proof_commit(
        cwd=repo,
        source_commit_ids=[a_id, b_id, c_id],
        proof_directory=".opentimestamps",
        paths=[ots_a, json_a, ots_b, json_b, ots_c, json_c],
    )

    message = _git(["log", "-1", "--format=%B", commit_id], cwd=repo)
    assert message.startswith("Store OpenTimestamps proofs\n")
    assert "OpenTimestamps-Generated: true\n" in message
    assert f"OpenTimestamps-Source: {a_id}\n" in message
    assert f"OpenTimestamps-Source: {b_id}\n" in message
    assert f"OpenTimestamps-Source: {c_id}\n" in message

    changed = _changed_paths(repo, commit_id)
    assert sorted(changed) == sorted([ots_a, json_a, ots_b, json_b, ots_c, json_c])


def test_create_generated_proof_commit_requires_source_ids(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    with pytest.raises(ValueError):
        create_generated_proof_commit(
            cwd=repo,
            source_commit_ids=[],
            proof_directory=".opentimestamps",
            paths=[".opentimestamps/aaa.ots"],
        )


def test_create_generated_proof_commit_requires_non_empty_proof_directory(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    with pytest.raises(ValueError):
        create_generated_proof_commit(
            cwd=repo,
            source_commit_ids=["a" * 40],
            proof_directory="",
            paths=[".opentimestamps/aaa.ots"],
        )


def test_create_generated_proof_commit_propagates_git_error(tmp_path: Path) -> None:
    # If nothing is staged, ``git commit`` fails and the error is surfaced.
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["README"], message="seed\n")

    with pytest.raises(GitCommandError):
        create_generated_proof_commit(
            cwd=repo,
            source_commit_ids=["a" * 40],
            proof_directory=".opentimestamps",
            paths=[".opentimestamps/a" * 40 + ".ots"],
        )


def test_create_generated_proof_commit_uses_explicit_pathspec(
    tmp_path: Path,
) -> None:
    """Generated proof commits commit only staged artifact paths."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["README", "tracked.txt"], message="seed\n")

    # Staged unrelated modification to tracked file -> "M " in porcelain.
    (repo / "tracked.txt").write_text("staged modified\n")
    _git(["add", "tracked.txt"], cwd=repo)

    # Newly-added-then-further-modified -> "AM" in porcelain.
    (repo / "will_add.txt").write_text("initial added content\n")
    _git(["add", "will_add.txt"], cwd=repo)
    (repo / "will_add.txt").write_text("further worktree edit\n")

    # Untracked file -> "??" in porcelain.
    (repo / "untracked.txt").write_text("untracked\n")

    a_path = repo / "a.txt"
    a_path.parent.mkdir(parents=True, exist_ok=True)
    a_path.write_text("a\n")
    _git(["add", "a.txt"], cwd=repo)
    source_id = _git(["commit", "-q", "-m", "A", "--", "a.txt"], cwd=repo).strip()
    source_id = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    ots, json_path = _stage_proof(repo, source_id)

    before_status = _git(["status", "--porcelain"], cwd=repo)
    assert "M  tracked.txt" in before_status
    assert "AM will_add.txt" in before_status
    assert "?? untracked.txt" in before_status

    commit_id = create_generated_proof_commit(
        cwd=repo,
        source_commit_ids=[source_id],
        proof_directory=".opentimestamps",
        paths=[ots, json_path],
    )

    # Only the two artifact paths are part of the commit tree.
    changed = _changed_paths(repo, commit_id)
    assert sorted(changed) == sorted([ots, json_path])

    # Unrelated worktree/index state is preserved exactly.
    after_status = _git(["status", "--porcelain"], cwd=repo)
    assert "M  tracked.txt" in after_status
    assert "AM will_add.txt" in after_status
    assert "?? untracked.txt" in after_status
