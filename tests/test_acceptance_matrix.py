"""Compact end-to-end acceptance matrix for the policy/orchestration contract.

These tests exercise the full orchestration path against temporary Git
repositories with fake OpenTimestamps submissions. They are deliberately
focused on the spec acceptance cases that are not already covered by the
larger integration tests above.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from git_ots.config import (
    Config,
    GitConfig,
    OpenTimestampsConfig,
    PolicyConfig,
    ProofConfig,
)
from git_ots.git import create_timestamp_tag
from git_ots.orchestration import build_snapshot
from git_ots.orchestration import run as run_orchestration
from git_ots.timestamp import build_payload
from tests.test_timestamp import _make_fake_detached_proof


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
    repo: Path,
    *,
    paths: list[str],
    message: str,
    date: datetime | None = None,
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
    _completed = subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return _git(["rev-parse", "HEAD"], cwd=repo).strip()


def _config(**policy_overrides: object) -> Config:
    fields = {
        "every_commit": False,
        "max_age": timedelta(hours=24),
        "fixed_time": None,
        "timezone": None,
        "initial_history": "latest",
    }
    fields.update(policy_overrides)
    policy = PolicyConfig(**fields)
    return Config(
        policy=policy,
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


def _run(repo: Path, config: Config, now: datetime) -> list[tuple[str, bytes]]:
    """Run orchestration and return the list of (commit_id, payload) submissions."""
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(snapshot=snapshot, now=now, submit=submit)
    return submissions


# 42.1 No changes


def test_no_changes_after_baseline_produces_no_submission(tmp_path: Path) -> None:
    """A timestamped baseline with no new meaningful commits yields no action."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    a_id = _commit(repo, paths=["a.txt"], message="A\n", date=base)

    create_timestamp_tag(
        cwd=repo,
        source_commit_id=a_id,
        tag_prefix="ots/",
        submitted_at=base + timedelta(minutes=1),
        proof=f".opentimestamps/{a_id}.ots",
        triggers=frozenset({"max_age"}),
    )

    submissions = _run(repo, _config(), base + timedelta(hours=2))
    assert submissions == []


# 42.2 Proof-only change


def test_proof_only_change_produces_no_submission(tmp_path: Path) -> None:
    """A generated proof commit alone does not trigger another timestamp."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    a_id = _commit(repo, paths=["a.txt"], message="A\n", date=base)

    # Simulate a completed initial run: tag on A and generated proof commit P.
    create_timestamp_tag(
        cwd=repo,
        source_commit_id=a_id,
        tag_prefix="ots/",
        submitted_at=base + timedelta(minutes=1),
        proof=f".opentimestamps/{a_id}.ots",
        triggers=frozenset({"max_age"}),
    )

    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    (proof_dir / f"{a_id}.ots").write_bytes(b"fake-proof")
    (proof_dir / f"{a_id}.json").write_text('{"schema": 1}\n')
    _git(["add", "."], cwd=repo)
    _git(
        [
            "commit",
            "-q",
            "-m",
            f"Store OpenTimestamps proof\n\nOpenTimestamps-Generated: true\nOpenTimestamps-Source: {a_id}\n",
        ],
        cwd=repo,
    )

    submissions = _run(
        repo, _config(max_age=timedelta(hours=1)), base + timedelta(hours=2)
    )
    assert submissions == []


# 42.3 Ordinary proof-directory edit


def test_ordinary_proof_directory_edit_is_meaningful(tmp_path: Path) -> None:
    """A user commit inside the proof directory without the generated trailer is timestamped."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    a_id = _commit(repo, paths=["a.txt"], message="A\n", date=base)

    create_timestamp_tag(
        cwd=repo,
        source_commit_id=a_id,
        tag_prefix="ots/",
        submitted_at=base + timedelta(minutes=1),
        proof=f".opentimestamps/{a_id}.ots",
        triggers=frozenset({"max_age"}),
    )

    # User edits a proof file without the generated trailer.
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    (proof_dir / "manual.ots").write_bytes(b"user-proof")
    user_id = _commit(
        repo,
        paths=[".opentimestamps/manual.ots"],
        message="Update manual proof\n",
        date=base + timedelta(hours=2),
    )

    submissions = _run(
        repo, _config(max_age=timedelta(hours=1)), base + timedelta(hours=3)
    )
    assert len(submissions) == 1
    assert submissions[0][0] == user_id


# 42.4 Maximum age not reached / 42.5 Maximum age reached boundary


@pytest.mark.parametrize(
    ("oldest_age_minutes", "expected_submissions"),
    [
        (23 * 60 + 59, 0),  # 23h59m: not due
        (24 * 60, 1),  # exactly 24h: due
    ],
)
def test_max_age_boundary(
    tmp_path: Path,
    oldest_age_minutes: int,
    expected_submissions: int,
) -> None:
    """At 23h59m no submission; at exactly 24h the pending commit is timestamped."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n", date=base)

    submissions = _run(
        repo,
        _config(max_age=timedelta(hours=24)),
        base + timedelta(minutes=oldest_age_minutes),
    )
    assert len(submissions) == expected_submissions
    if expected_submissions:
        assert submissions[0][0] == commit_id


# 42.6 Multiple pending commits aggregate to one timestamp


def test_multiple_pending_commits_aggregates_to_latest(tmp_path: Path) -> None:
    """With B/C/D pending and an aggregate policy due, only D is timestamped."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _commit(repo, paths=["a.txt"], message="A\n", date=base)
    _commit(repo, paths=["b.txt"], message="B\n", date=base + timedelta(hours=1))
    _commit(repo, paths=["c.txt"], message="C\n", date=base + timedelta(hours=2))
    d_id = _commit(repo, paths=["d.txt"], message="D\n", date=base + timedelta(hours=3))

    submissions = _run(
        repo, _config(max_age=timedelta(hours=1)), base + timedelta(hours=5)
    )
    assert len(submissions) == 1
    assert submissions[0][0] == d_id
    assert submissions[0][1] == build_payload("sha1", d_id)


# 42.7 Every commit already covered by test_integration.py; include a compact variant.


def test_every_commit_timestamps_each_pending_commit_in_order(tmp_path: Path) -> None:
    """With every_commit enabled, B/C/D each receive a timestamp in repository order."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    b_id = _commit(repo, paths=["b.txt"], message="B\n", date=base)
    c_id = _commit(repo, paths=["c.txt"], message="C\n", date=base + timedelta(hours=1))
    d_id = _commit(repo, paths=["d.txt"], message="D\n", date=base + timedelta(hours=2))

    config = _config(
        every_commit=True,
        max_age=None,
        fixed_time=None,
        timezone=None,
        initial_history="all",
    )
    submissions = _run(repo, config, base + timedelta(hours=3))
    submitted_ids = [s[0] for s in submissions]
    assert submitted_ids == [b_id, c_id, d_id]


# 42.8 Fixed time with no changes / 42.9 Fixed time with changes / 42.10 Delayed scheduler


def test_fixed_time_no_changes_no_submission(tmp_path: Path) -> None:
    """A fixed-time occurrence with no meaningful pending commits produces no submission."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    a_id = _commit(repo, paths=["a.txt"], message="A\n", date=base)

    create_timestamp_tag(
        cwd=repo,
        source_commit_id=a_id,
        tag_prefix="ots/",
        submitted_at=base + timedelta(minutes=1),
        proof=f".opentimestamps/{a_id}.ots",
        triggers=frozenset({"fixed_time"}),
    )

    # 00:07 after a 00:00 occurrence with no new meaningful work.
    now = datetime(2026, 8, 19, 0, 7, 0, tzinfo=ZoneInfo("UTC"))
    submissions = _run(
        repo,
        _config(
            max_age=None,
            fixed_time=time(0, 0),
            timezone=ZoneInfo("UTC"),
        ),
        now,
    )
    assert submissions == []


def test_fixed_time_with_changes_and_scheduler_delay(tmp_path: Path) -> None:
    """A 00:07 invocation for a 00:00 occurrence selects the latest pending commit."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    a_id = _commit(repo, paths=["a.txt"], message="A\n", date=base)

    create_timestamp_tag(
        cwd=repo,
        source_commit_id=a_id,
        tag_prefix="ots/",
        submitted_at=base + timedelta(minutes=1),
        proof=f".opentimestamps/{a_id}.ots",
        triggers=frozenset({"fixed_time"}),
    )

    b_id = _commit(
        repo,
        paths=["b.txt"],
        message="B\n",
        date=datetime(2026, 8, 19, 0, 5, 0, tzinfo=ZoneInfo("UTC")),
    )

    now = datetime(2026, 8, 19, 0, 7, 0, tzinfo=ZoneInfo("UTC"))
    submissions = _run(
        repo,
        _config(
            max_age=None,
            fixed_time=time(0, 0),
            timezone=ZoneInfo("UTC"),
        ),
        now,
    )
    assert len(submissions) == 1
    assert submissions[0][0] == b_id
