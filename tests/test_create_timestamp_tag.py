"""Tests for creating immutable annotated source tags.

Spec section 12 requires every successful submission to produce an annotated
Git tag pointing directly at the source commit. The tag name embeds the UTC
submission timestamp and a short SHA; the annotation carries schema-1
metadata. The tag must target the frozen source commit, not the current
``HEAD``.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from git_ots.git import InvalidRepositoryStateError, create_timestamp_tag


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


def test_create_timestamp_tag_targets_source_commit_not_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(repo, paths=["a.txt"], message="A\n")
    b_id = _commit(repo, paths=["b.txt"], message="B\n")

    tag_name = create_timestamp_tag(
        cwd=repo,
        source_commit_id=a_id,
        tag_prefix="ots/",
        submitted_at=datetime(2026, 8, 8, 22, 0, 3, tzinfo=UTC),
        proof=f".opentimestamps/{a_id}.ots",
        triggers=frozenset({"fixed_time"}),
    )

    # The current branch HEAD is B, but the tag must point at the frozen A.
    assert _git(["rev-parse", f"{tag_name}^{{commit}}"], cwd=repo).strip() == a_id
    assert b_id != a_id


def test_create_timestamp_tag_annotation_fields_are_correct(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(repo, paths=["a.txt"], message="A\n")

    tag_name = create_timestamp_tag(
        cwd=repo,
        source_commit_id=a_id,
        tag_prefix="ots/",
        submitted_at=datetime(2026, 8, 8, 22, 0, 3, tzinfo=UTC),
        proof=f".opentimestamps/{a_id}.ots",
        triggers=frozenset({"max_age", "fixed_time"}),
    )

    contents = _git(["tag", "-l", "--format=%(contents)", tag_name], cwd=repo)
    assert "git-ots schema: 2" in contents
    assert "producer: git-ots 0.0.1" in contents
    assert f"source: {a_id}" in contents
    assert "submitted-at: 2026-08-08T22:00:03Z" in contents
    assert f"proof: .opentimestamps/{a_id}.ots" in contents
    assert "triggers: fixed_time max_age" in contents


def test_create_timestamp_tag_name_uses_utc_timestamp_and_short_sha(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(repo, paths=["a.txt"], message="A\n")

    tag_name = create_timestamp_tag(
        cwd=repo,
        source_commit_id=a_id,
        tag_prefix="ots/",
        submitted_at=datetime(2026, 8, 8, 22, 0, 3, tzinfo=UTC),
        proof=f".opentimestamps/{a_id}.ots",
        triggers=frozenset({"fixed_time"}),
    )

    assert tag_name == f"ots/20260808T220003Z/{a_id[:12]}"


def test_create_timestamp_tag_is_idempotent_for_identical_tag(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(repo, paths=["a.txt"], message="A\n")

    args = {
        "cwd": repo,
        "source_commit_id": a_id,
        "tag_prefix": "ots/",
        "submitted_at": datetime(2026, 8, 8, 22, 0, 3, tzinfo=UTC),
        "proof": f".opentimestamps/{a_id}.ots",
        "triggers": frozenset({"fixed_time"}),
    }

    tag_name1 = create_timestamp_tag(**args)
    tag_name2 = create_timestamp_tag(**args)

    assert tag_name1 == tag_name2
    assert _git(["tag", "--list"], cwd=repo).splitlines() == [tag_name1]


def test_create_timestamp_tag_rejects_different_annotation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(repo, paths=["a.txt"], message="A\n")

    tag_name = f"ots/20260808T220003Z/{a_id[:12]}"
    _git(
        ["tag", "-a", "-m", "git-ots schema: 1\nsource: X", tag_name, a_id],
        cwd=repo,
    )

    with pytest.raises(InvalidRepositoryStateError) as exc_info:
        create_timestamp_tag(
            cwd=repo,
            source_commit_id=a_id,
            tag_prefix="ots/",
            submitted_at=datetime(2026, 8, 8, 22, 0, 3, tzinfo=UTC),
            proof=f".opentimestamps/{a_id}.ots",
            triggers=frozenset({"fixed_time"}),
        )

    assert tag_name in str(exc_info.value)
    assert "collision" in str(exc_info.value).lower()
    contents = _git(["tag", "-l", "--format=%(contents)", tag_name], cwd=repo)
    assert "source: X" in contents


def test_create_timestamp_tag_rejects_different_target(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(repo, paths=["a.txt"], message="A\n")
    b_id = _commit(repo, paths=["b.txt"], message="B\n")

    tag_name = f"ots/20260808T220003Z/{a_id[:12]}"
    _git(
        [
            "tag",
            "-a",
            "-m",
            "external annotation",
            tag_name,
            b_id,
        ],
        cwd=repo,
    )

    with pytest.raises(InvalidRepositoryStateError) as exc_info:
        create_timestamp_tag(
            cwd=repo,
            source_commit_id=a_id,
            tag_prefix="ots/",
            submitted_at=datetime(2026, 8, 8, 22, 0, 3, tzinfo=UTC),
            proof=f".opentimestamps/{a_id}.ots",
            triggers=frozenset({"fixed_time"}),
        )

    assert tag_name in str(exc_info.value)
    assert _git(["rev-parse", f"{tag_name}^{{commit}}"], cwd=repo).strip() == b_id
