"""Tests for selecting the newest relevant timestamp baseline.

Spec section 17 requires identifying the newest timestamped source commit that
is an ancestor of the frozen source ref. Tags pointing at commits outside the
source lineage must be ignored, and among the remaining ancestors the most
recent submission (by annotation timestamp) is selected. Tags are never moved,
deleted, or rewritten by this lookup.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from git_ots.git import find_newest_relevant_baseline


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


def _annotation(*, source: str, submitted_at: str) -> str:
    return (
        "git-ots schema: 1\n"
        f"source: {source}\n"
        f"submitted-at: {submitted_at}\n"
        f"proof: .opentimestamps/{source}.ots\n"
        "triggers: fixed_time\n"
    )


def _tag_name(*, submitted_at: str, short: str) -> str:
    # "2026-08-08T22:00:03Z" -> "20260808T220003Z"
    utc = submitted_at.replace("-", "").replace(":", "").rstrip("Z")
    return f"ots/{utc}/{short}"


def test_find_newest_relevant_baseline_selects_newest_ancestor(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(repo, paths=["a.txt"], message="A\n")
    b_id = _commit(repo, paths=["b.txt"], message="B\n")

    # Create a sibling branch off A with its own tagged commit X.
    _git(["checkout", "-q", "-b", "side", a_id], cwd=repo)
    x_id = _commit(repo, paths=["x.txt"], message="X\n")
    _git(["checkout", "-q", "main"], cwd=repo)

    # Tag A, X, and B with distinct submission timestamps. When the source
    # is B, X is off the lineage and must be ignored; B is the newest
    # ancestor and must be selected over A.
    for cid, ts in [
        (a_id, "2026-08-08T20:00:00Z"),
        (x_id, "2026-08-08T23:00:00Z"),  # newest timestamp, but off lineage
        (b_id, "2026-08-08T22:00:00Z"),  # newest on the main lineage
    ]:
        _git(
            [
                "tag",
                "-a",
                "-m",
                _annotation(source=cid, submitted_at=ts),
                _tag_name(submitted_at=ts, short=cid[:12]),
                cid,
            ],
            cwd=repo,
        )

    result = find_newest_relevant_baseline(
        cwd=repo, source_commit_id=b_id, tag_prefix="ots/"
    )

    assert result.baseline is not None
    assert result.baseline.source_commit_id == b_id
    assert result.baseline.tag_name == _tag_name(
        submitted_at="2026-08-08T22:00:00Z", short=b_id[:12]
    )
    assert result.has_abandoned_tags is False


def test_find_newest_relevant_baseline_returns_none_without_ancestors(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(repo, paths=["a.txt"], message="A\n")
    b_id = _commit(repo, paths=["b.txt"], message="B\n")

    # Tag only the side branch, which is not an ancestor of the source B.
    _git(["checkout", "-q", "-b", "side", a_id], cwd=repo)
    x_id = _commit(repo, paths=["x.txt"], message="X\n")
    _git(["checkout", "-q", "main"], cwd=repo)

    _git(
        [
            "tag",
            "-a",
            "-m",
            _annotation(source=x_id, submitted_at="2026-08-08T22:00:00Z"),
            _tag_name(submitted_at="2026-08-08T22:00:00Z", short=x_id[:12]),
            x_id,
        ],
        cwd=repo,
    )

    result = find_newest_relevant_baseline(
        cwd=repo, source_commit_id=b_id, tag_prefix="ots/"
    )

    assert result.baseline is None
    assert result.has_abandoned_tags is True


def test_find_newest_relevant_baseline_reports_no_abandoned_tags_for_fresh_repo(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(repo, paths=["a.txt"], message="A\n")

    result = find_newest_relevant_baseline(
        cwd=repo, source_commit_id=a_id, tag_prefix="ots/"
    )

    assert result.baseline is None
    assert result.has_abandoned_tags is False


def test_find_newest_relevant_baseline_handles_rewritten_history(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(repo, paths=["a.txt"], message="A\n")
    b_id = _commit(repo, paths=["b.txt"], message="B\n")

    # Timestamp the original main state B.
    original_tag = _tag_name(submitted_at="2026-08-08T22:00:00Z", short=b_id[:12])
    _git(
        [
            "tag",
            "-a",
            "-m",
            _annotation(source=b_id, submitted_at="2026-08-08T22:00:00Z"),
            original_tag,
            b_id,
        ],
        cwd=repo,
    )

    # Rewrite history: create a new commit C whose parent is A, abandoning B.
    _git(["checkout", "-q", "-b", "rewritten", a_id], cwd=repo)
    c_id = _commit(repo, paths=["c.txt"], message="C\n")

    result = find_newest_relevant_baseline(
        cwd=repo, source_commit_id=c_id, tag_prefix="ots/"
    )

    # No relevant baseline on the new lineage.
    assert result.baseline is None
    # Explicit lineage outcome data must report the abandoned tags.
    assert result.has_abandoned_tags is True
    # The old timestamp tag remains untouched.
    assert original_tag in _git(["tag", "-l"], cwd=repo)
    assert _git(["rev-parse", original_tag + "^{commit}"], cwd=repo).strip() == b_id
