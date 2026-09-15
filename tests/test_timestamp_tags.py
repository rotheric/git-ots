"""Tests for enumerating timestamp tags by prefix.

Spec section 17 requires inspecting tags matching the configured tag
prefix. Only annotated tags under the prefix qualify as timestamp tags;
lightweight tags (which carry no annotation) and tags outside the prefix
must be excluded. Returned values expose the tag name, target commit id,
and the parsed annotation fields so downstream code can pick the newest
relevant baseline.

Strict annotation validation (schema, source, submitted, proof, triggers
shape) belongs to validate_timestamp_tag and is not covered here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from git_ots.git import enumerate_timestamp_tags


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


ANNOTATION = (
    "git-ots schema: 1\n"
    "source: {source}\n"
    "submitted-at: 2026-08-08T22:00:03Z\n"
    "proof: .opentimestamps/{source}.ots\n"
    "triggers: fixed_time\n"
)


def test_only_matching_annotated_tags_returned(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(repo, paths=["a.txt"], message="A\n")
    b_id = _commit(repo, paths=["b.txt"], message="B\n")

    # An annotated tag under the configured prefix → timestamp tag.
    _git(
        [
            "tag",
            "-a",
            "-m",
            ANNOTATION.format(source=a_id),
            f"ots/20260808T220003Z/{a_id[:12]}",
            a_id,
        ],
        cwd=repo,
    )
    # A lightweight tag under the prefix — no annotation, excluded.
    _git(["tag", f"ots/lightweight/{b_id[:12]}", b_id], cwd=repo)
    # An annotated tag outside the prefix — unrelated, excluded.
    _git(
        ["tag", "-a", "-m", "release notes\n", "v1.0.0", b_id],
        cwd=repo,
    )
    # A lightweight tag outside the prefix — excluded.
    _git(["tag", "scratch", a_id], cwd=repo)

    tags = enumerate_timestamp_tags(cwd=repo, tag_prefix="ots/")

    assert [t.name for t in tags] == [f"ots/20260808T220003Z/{a_id[:12]}"]
    assert tags[0].commit_id == a_id


def test_annotation_fields_parsed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(repo, paths=["a.txt"], message="A\n")

    _git(
        [
            "tag",
            "-a",
            "-m",
            ANNOTATION.format(source=a_id),
            f"ots/20260808T220003Z/{a_id[:12]}",
            a_id,
        ],
        cwd=repo,
    )

    tags = enumerate_timestamp_tags(cwd=repo, tag_prefix="ots/")

    assert len(tags) == 1
    tag = tags[0]
    assert tag.annotation_get("git-ots schema") == "1"
    assert tag.annotation_get("source") == a_id
    assert tag.annotation_get("submitted-at") == "2026-08-08T22:00:03Z"
    assert tag.annotation_get("proof") == f".opentimestamps/{a_id}.ots"
    assert tag.annotation_get("triggers") == "fixed_time"


def test_empty_repository_returns_no_tags(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n")

    tags = enumerate_timestamp_tags(cwd=repo, tag_prefix="ots/")

    assert tags == ()


def test_prefix_without_slash_is_matched_literally(tmp_path: Path) -> None:
    # The prefix is matched literally; the test pins the behavior so a
    # caller-supplied prefix like "ots-" does not silently broaden.
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(repo, paths=["a.txt"], message="A\n")

    _git(
        [
            "tag",
            "-a",
            "-m",
            ANNOTATION.format(source=a_id),
            f"ots-2026/{a_id[:12]}",
            a_id,
        ],
        cwd=repo,
    )

    tags = enumerate_timestamp_tags(cwd=repo, tag_prefix="ots-")
    assert [t.name for t in tags] == [f"ots-2026/{a_id[:12]}"]
