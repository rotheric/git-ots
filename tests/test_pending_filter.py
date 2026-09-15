"""Tests for filtering pending history to meaningful commits.

A pending range is the set of commits after the baseline (exclusive) up to
the frozen source (inclusive). Only meaningful commits are returned;
generated proof commits — those with the canonical trailer AND only
proof-directory changes — are filtered out (spec sections 14 and 17).

This function composes history enumeration and generated-commit
classification; it must not embed policy rules (max-age, fixed-time,
every-commit selection are policy concerns).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from git_ots.git import (
    InvalidRepositoryStateError,
    filter_pending_meaningful_commits,
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


def _commit(repo: Path, *, paths: list[str], message: str, when: str) -> str:
    for rel in paths:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{rel}\n")
    _git(["add", *paths], cwd=repo)
    _git(
        ["commit", "-q", "-m", message],
        cwd=repo,
    )
    # Force committer time so ordering is deterministic.
    _ = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--amend",
            "--no-edit",
            "-q",
            "--date",
            when,
        ],
        cwd=repo,
        # Inherits the full environment so conftest's `_isolated_git_config`
        # governs which git-config scopes this subprocess can see. This call
        # used to pin `PATH` and nothing else, which left HOME unset and so
        # isolated the commit from the developer's `~/.gitconfig` -- but by
        # accident, as a side effect of replacing the environment rather than
        # extending it. That accident is now a deliberate guarantee held in one
        # place, and reproducing it here would instead drop the isolation
        # variables the fixture sets. Identity comes from the `-c` flags above,
        # not from configuration, so nothing here depends on the narrower PATH.
        env={
            **os.environ,
            "GIT_COMMITTER_DATE": when,
            "GIT_AUTHOR_DATE": when,
        },
        check=True,
    )
    return _git(["rev-parse", "HEAD"], cwd=repo).strip()


def test_generated_proofs_filtered_out_between_baseline_and_head(
    tmp_path: Path,
) -> None:
    # Spec example from the plan: history A (baseline) → generated P →
    # user B → generated Q → HEAD. Only B is pending-meaningful.
    repo = tmp_path / "repo"
    _init_repo(repo)

    a_id = _commit(
        repo, paths=["a.txt"], message="A\n", when="2026-01-01T00:00:00+00:00"
    )
    _commit(
        repo,
        paths=[f"{PROOF_DIR}/p.ots"],
        message=GENERATED_MESSAGE,
        when="2026-01-01T01:00:00+00:00",
    )
    b_id = _commit(
        repo, paths=["b.txt"], message="B\n", when="2026-01-01T02:00:00+00:00"
    )
    _commit(
        repo,
        paths=[f"{PROOF_DIR}/q.ots"],
        message=GENERATED_MESSAGE,
        when="2026-01-01T03:00:00+00:00",
    )

    pending = filter_pending_meaningful_commits(
        cwd=repo,
        ref="HEAD",
        baseline=a_id,
        proof_directory=PROOF_DIR,
    )

    assert [c.commit_id for c in pending] == [b_id]
    assert pending[0].committer_time.tzinfo is not None


def test_empty_pending_range_yields_no_commits(tmp_path: Path) -> None:
    # When the source ref equals the baseline, no commits are pending.
    repo = tmp_path / "repo"
    _init_repo(repo)

    a_id = _commit(
        repo, paths=["a.txt"], message="A\n", when="2026-01-01T00:00:00+00:00"
    )

    pending = filter_pending_meaningful_commits(
        cwd=repo,
        ref=a_id,
        baseline=a_id,
        proof_directory=PROOF_DIR,
    )

    assert pending == ()


def test_no_baseline_returns_all_meaningful_history(tmp_path: Path) -> None:
    # Without a baseline, the entire reachable history is enumerated and
    # filtered — generated proof commits still drop out.
    repo = tmp_path / "repo"
    _init_repo(repo)

    a_id = _commit(
        repo, paths=["a.txt"], message="A\n", when="2026-01-01T00:00:00+00:00"
    )
    _commit(
        repo,
        paths=[f"{PROOF_DIR}/p.ots"],
        message=GENERATED_MESSAGE,
        when="2026-01-01T01:00:00+00:00",
    )
    b_id = _commit(
        repo, paths=["b.txt"], message="B\n", when="2026-01-01T02:00:00+00:00"
    )

    pending = filter_pending_meaningful_commits(
        cwd=repo,
        ref="HEAD",
        baseline=None,
        proof_directory=PROOF_DIR,
    )

    assert [c.commit_id for c in pending] == [a_id, b_id]


def test_proof_only_user_commit_without_trailer_is_meaningful(
    tmp_path: Path,
) -> None:
    # A user commit that touches only proof files but lacks the canonical
    # trailer is meaningful and must remain pending (spec section 14).
    repo = tmp_path / "repo"
    _init_repo(repo)

    a_id = _commit(
        repo, paths=["a.txt"], message="A\n", when="2026-01-01T00:00:00+00:00"
    )
    user_proof_id = _commit(
        repo,
        paths=[f"{PROOF_DIR}/manual.ots"],
        message="Manually adjust proof\n",
        when="2026-01-01T01:00:00+00:00",
    )

    pending = filter_pending_meaningful_commits(
        cwd=repo,
        ref="HEAD",
        baseline=a_id,
        proof_directory=PROOF_DIR,
    )

    assert [c.commit_id for c in pending] == [user_proof_id]


def test_unknown_ref_raises_invalid_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(
        repo, paths=["a.txt"], message="A\n", when="2026-01-01T00:00:00+00:00"
    )

    try:
        filter_pending_meaningful_commits(
            cwd=repo,
            ref="refs/heads/does-not-exist",
            baseline=a_id,
            proof_directory=PROOF_DIR,
        )
    except InvalidRepositoryStateError:
        return
    raise AssertionError("expected InvalidRepositoryStateError")


def test_near_miss_trailers_remain_meaningful_through_single_pass_filter(
    tmp_path: Path,
) -> None:
    # Trailer detection now happens in a single history pass; the near-miss
    # semantics pinned on has_generated_trailer must hold there
    # too: a "false" value, a miscased or misspelled key, and the trailer
    # text appearing as body prose are all meaningful commits.
    repo = tmp_path / "repo"
    _init_repo(repo)
    a_id = _commit(
        repo, paths=["a.txt"], message="A\n", when="2026-01-01T00:00:00+00:00"
    )
    near_misses = [
        "x\n\nOpenTimestamps-Generated: false\n",
        "x\n\nOpentimestamps-Generated: true\n",
        "x\n\nOpenTimestamps-generated: true\n",
        "x\n\nOpenTimestamps-Generatd: true\n",
        "Some subject\n\nBody mentions OpenTimestamps-Generated: true in prose.\n",
        "OpenTimestamps-Generated: true",
    ]
    expected: list[str] = []
    for i, message in enumerate(near_misses):
        expected.append(
            _commit(
                repo,
                paths=[f"n{i}.txt"],
                message=message,
                when=f"2026-01-01T0{i + 1}:00:00+00:00",
            )
        )
    # One genuine generated commit among them is still filtered out.
    _commit(
        repo,
        paths=[f"{PROOF_DIR}/p.ots"],
        message=GENERATED_MESSAGE,
        when="2026-01-01T08:00:00+00:00",
    )

    pending = filter_pending_meaningful_commits(
        cwd=repo,
        ref="HEAD",
        baseline=a_id,
        proof_directory=PROOF_DIR,
    )

    assert [c.commit_id for c in pending] == expected
