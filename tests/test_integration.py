"""Integration tests for the read-only repository snapshot and CLI orchestration.

These tests exercise the composition of config, Git, and policy modules without
relying on real OpenTimestamps network access. Snapshot tests verify that
inspecting a repository is read-only: no tags, commits, files, or fetches are
performed.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import signal
import subprocess
import sys
import time as time_module
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from git_ots.cli import main
from git_ots.config import (
    PROOF_DIRECTORY,
    Config,
    GitConfig,
    LimitsConfig,
    OpenTimestampsConfig,
    PolicyConfig,
    ProofConfig,
)
from git_ots.git import (
    CommitInfo,
    GitCommandTimeoutError,
    InvalidRepositoryStateError,
    LineageBaselineResult,
    ResolvedRef,
    create_generated_proof_commit,
    create_timestamp_tag,
    make_process_runner,
    stage_paths,
)
from git_ots.orchestration import RepositorySnapshot, build_snapshot
from git_ots.orchestration import run as run_orchestration
from git_ots.policy import Decision
from git_ots.timestamp import (
    OpenTimestampsCli,
    PersistenceError,
    RecoveryValidationError,
    SubmissionTimeoutError,
    TimestampRequest,
    build_manifest,
    build_payload,
    persist_manifest,
    persist_proof,
    validate_detached_proof,
)
from tests.test_timestamp import _make_fake_detached_proof


def _assert_proof_binds_to_commit(proof_bytes: bytes, commit_id: str) -> None:
    """Assert that fake proof bytes are a valid detached proof for ``commit_id``."""
    payload = build_payload("sha1", commit_id)
    validate_detached_proof(proof_bytes, payload)


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


def _set_config(repo: Path, **settings: object) -> None:
    """Set `ots.*` local-scope git config keys from friendly kwargs.

    Maps `Config`-field-shaped kwargs onto their `ots.*` camelCase key
    (FS-0015 behaviour 5) and writes each with `git config --local`,
    converting Python bools to Git's own boolean spelling. Mirrors
    test_cli.py's/test_exit_codes.py's helper of the same name.
    """
    key_map = {
        "every_commit": "ots.everyCommit",
        "max_age": "ots.maxAge",
        "fixed_time": "ots.fixedTime",
        "timezone": "ots.timezone",
        "initial_history": "ots.initialHistory",
        "source_ref": "ots.sourceRef",
        "fetch_before_run": "ots.fetchBeforeRun",
        "tag_prefix": "ots.tagPrefix",
        "require_clean_worktree": "ots.requireCleanWorktree",
        "signing": "ots.signing",
        "commit": "ots.proofCommit",
        "directory": "ots.proofDirectory",
        "command": "ots.command",
        "ots_timeout": "ots.otsTimeout",
        "git_timeout": "ots.gitTimeout",
    }
    for field, value in settings.items():
        key = key_map[field]
        text = "true" if value is True else "false" if value is False else str(value)
        _git(["config", "--local", key, text], cwd=repo)


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
    _completed = subprocess.run(
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
            max_age=timedelta(hours=24),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )


def test_build_snapshot_returns_read_only_repository_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
    a_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    before_head = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    before_tags = _git(["tag", "-l"], cwd=repo).strip()

    config = _fresh_config()
    snapshot = build_snapshot(
        cwd=repo, config=config, now=commit_date + timedelta(hours=1)
    )

    assert isinstance(snapshot, RepositorySnapshot)
    assert snapshot.config is config
    assert snapshot.layout.worktree_root == repo.resolve()
    assert snapshot.source == ResolvedRef(commit_id=a_id, display_ref="refs/heads/main")
    assert snapshot.object_format == "sha1"
    assert isinstance(snapshot.baseline, LineageBaselineResult)
    assert snapshot.baseline.baseline is None
    assert snapshot.baseline.has_abandoned_tags is False
    assert len(snapshot.pending) == 1
    assert snapshot.pending[0] == CommitInfo(
        commit_id=a_id,
        committer_time=commit_date,
    )
    assert snapshot.decision == Decision()

    # Verify read-only behavior: no mutations occurred.
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == before_head
    assert _git(["tag", "-l"], cwd=repo).strip() == before_tags
    assert not (repo / ".opentimestamps").exists()


def test_run_no_action_invokes_no_callbacks(tmp_path: Path) -> None:
    """When the policy decision is empty, run returns without side effects.

    All timestamp/tag/commit collaborator callbacks must remain uncalled, and
    the repository must be left untouched.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = _fresh_config()
    now = commit_date + timedelta(minutes=1)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    calls: list[object] = []

    def submit(commit: object) -> None:
        calls.append(("submit", commit))

    def persist(commit: object, result: object) -> None:
        calls.append(("persist", commit, result))

    def tag(commit: object) -> None:
        calls.append(("tag", commit))

    def commit_callback() -> None:
        calls.append("commit")

    run_orchestration(
        snapshot=snapshot,
        submit=submit,
        persist=persist,
        tag=tag,
        commit=commit_callback,
    )

    assert calls == []
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == commit_id
    assert _git(["tag", "-l"], cwd=repo).strip() == ""
    assert not (repo / ".opentimestamps").exists()


def test_run_submits_and_persists_one_selected_source(tmp_path: Path) -> None:
    """When a policy selects a commit, run submits, persists, commits, and tags.

    The operation order must be: frozen source commit id, payload construction,
    submission, atomic proof persistence, manifest persistence, generated proof
    commit, annotated source tag. The commit step precedes the tag so a tag is
    never created unless the proof artifacts are already committed.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )
    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    calls: list[object] = []

    def submit(commit: str, payload: bytes) -> bytes:
        calls.append(("submit", commit, payload))
        return _make_fake_detached_proof(payload=payload)

    def persist(commit: str, proof: bytes) -> None:
        calls.append(("persist", commit, proof))

    def tag(commit: str) -> None:
        calls.append(("tag", commit))

    def commit_callback(source_commit_ids: list[str]) -> None:
        calls.append(("commit", source_commit_ids))

    run_orchestration(
        snapshot=snapshot,
        now=now,
        submit=submit,
        persist=persist,
        tag=tag,
        commit=commit_callback,
    )

    expected_payload = build_payload(snapshot.object_format, commit_id)
    assert len(calls) == 4
    assert calls[0] == ("submit", commit_id, expected_payload)
    assert calls[1][0] == "persist"
    assert calls[1][1] == commit_id
    _assert_proof_binds_to_commit(calls[1][2], commit_id)
    assert calls[2] == ("commit", [commit_id])
    assert calls[3] == ("tag", commit_id)


def test_run_creates_tag_after_proof_and_manifest_exist(tmp_path: Path) -> None:
    """After successful persistence and commit, an annotated tag is created.

    The tag callback receives the same frozen source commit id used for the
    payload, submission, persistence, and commit steps. The generated proof commit
    precedes the tag so the tag is never created against uncommitted artifacts.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )
    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    calls: list[object] = []

    def submit(commit: str, payload: bytes) -> bytes:
        calls.append(("submit", commit, payload))
        return _make_fake_detached_proof(payload=payload)

    def persist(commit: str, proof: bytes) -> None:
        calls.append(("persist", commit, proof))

    def tag(commit: str) -> None:
        calls.append(("tag", commit))

    def commit_callback(source_commit_ids: list[str]) -> None:
        calls.append(("commit", source_commit_ids))

    run_orchestration(
        snapshot=snapshot,
        now=now,
        submit=submit,
        persist=persist,
        tag=tag,
        commit=commit_callback,
    )

    assert ("tag", commit_id) in calls
    tag_index = calls.index(("tag", commit_id))
    assert tag_index == 3
    assert calls[0] == (
        "submit",
        commit_id,
        build_payload(snapshot.object_format, commit_id),
    )
    assert calls[1][0] == "persist"
    assert calls[1][1] == commit_id
    _assert_proof_binds_to_commit(calls[1][2], commit_id)
    assert calls[2] == ("commit", [commit_id])


def test_run_commits_proof_artifacts_last_and_tag_points_at_source(
    tmp_path: Path,
) -> None:
    """After tagging the source, the generated commit stores only proof artifacts.

    The normal single-source flow must: submit the source, atomically persist the
    proof and manifest, create an annotated source tag, stage exactly those two
    files, and create the generated proof commit. The timestamp tag must continue
    to point at the original source commit, not at the generated proof commit.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )
    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    def submit(commit: str, payload: bytes) -> bytes:
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(
        snapshot=snapshot,
        now=now,
        submit=submit,
    )

    proof_path = f".opentimestamps/{source_id}.ots"
    manifest_path = f".opentimestamps/{source_id}.json"
    _assert_proof_binds_to_commit((repo / proof_path).read_bytes(), source_id)
    assert (repo / manifest_path).exists()

    tag_names = _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n")
    assert len(tag_names) == 1
    tag_name = tag_names[0]
    assert _git(["rev-parse", f"{tag_name}^{{commit}}"], cwd=repo).strip() == source_id

    head_id = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    assert head_id != source_id
    assert _git(["rev-parse", f"{head_id}~1"], cwd=repo).strip() == source_id

    changed = _git(
        [
            "diff-tree",
            "--root",
            "-r",
            "--no-commit-id",
            "--no-renames",
            "--name-only",
            "-z",
            head_id,
        ],
        cwd=repo,
    ).split("\x00")
    changed = [p for p in changed if p]
    assert sorted(changed) == sorted([proof_path, manifest_path])

    message = _git(["log", "-1", "--format=%B", head_id], cwd=repo)
    assert "OpenTimestamps-Generated: true\n" in message
    assert f"OpenTimestamps-Source: {source_id}\n" in message


def test_run_reorders_transaction_commit_before_tag(tmp_path: Path) -> None:
    """The transaction order is submit → persist → commit → tag (ADR D4).

    The injected ``commit`` callable is actually invoked, not treated as a
    suppression flag. A simulated commit-step failure prevents tag creation; a
    simulated tag-step failure leaves the proof commit and committed artifacts in
    place. A rerun after a tag failure completes by creating the missing tag and
    without a second commit.
    """
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    now = commit_date + timedelta(hours=2)
    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )

    # 1. Operation order for a single selected source.
    repo = tmp_path / "repo"
    _init_repo(repo)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    calls: list[object] = []
    persisted_proofs: dict[str, bytes] = {}

    def submit(commit: str, payload: bytes) -> bytes:
        calls.append(("submit", commit, payload))
        return _make_fake_detached_proof(payload=payload)

    def persist(commit: str, proof: bytes) -> None:
        calls.append(("persist", commit))
        persisted_proofs[commit] = proof

    def commit_callback(source_commit_ids: list[str]) -> None:
        calls.append(("commit", source_commit_ids))

    def tag(commit: str) -> None:
        calls.append(("tag", commit))

    run_orchestration(
        snapshot=snapshot,
        now=now,
        submit=submit,
        persist=persist,
        commit=commit_callback,
        tag=tag,
    )

    expected_payload = build_payload(snapshot.object_format, source_id)
    assert len(calls) == 4
    assert calls[0] == ("submit", source_id, expected_payload)
    assert calls[1] == ("persist", source_id)
    _assert_proof_binds_to_commit(persisted_proofs[source_id], source_id)
    assert calls[2] == ("commit", [source_id])
    assert calls[3] == ("tag", source_id)

    # 2. Simulated commit-step failure leaves no tag.
    repo_commit_fail = tmp_path / "repo_commit_fail"
    _init_repo(repo_commit_fail)
    _commit(repo_commit_fail, paths=["a.txt"], message="A\n", date=commit_date)
    snapshot_cf = build_snapshot(cwd=repo_commit_fail, config=config, now=now)

    def commit_fail(source_commit_ids: list[str]) -> None:
        raise RuntimeError("simulated commit failure")

    with pytest.raises(RuntimeError, match="simulated commit failure"):
        run_orchestration(
            snapshot=snapshot_cf,
            now=now,
            submit=submit,
            persist=persist,
            commit=commit_fail,
            tag=tag,
        )

    assert _git(["tag", "-l"], cwd=repo_commit_fail).strip() == ""

    # 3. Simulated tag-step failure leaves the proof commit and artifacts.
    repo_tag_fail = tmp_path / "repo_tag_fail"
    _init_repo(repo_tag_fail)
    source_id_tf = _commit(
        repo_tag_fail, paths=["a.txt"], message="A\n", date=commit_date
    )
    snapshot_tf = build_snapshot(cwd=repo_tag_fail, config=config, now=now)

    def tag_fail(commit: str) -> None:
        raise RuntimeError("simulated tag failure")

    def submit_for_tag_fail(commit: str, payload: bytes) -> bytes:
        return _make_fake_detached_proof(payload=payload)

    with pytest.raises(RuntimeError, match="simulated tag failure"):
        run_orchestration(
            snapshot=snapshot_tf,
            now=now,
            submit=submit_for_tag_fail,
            tag=tag_fail,
        )

    head_id = _git(["rev-parse", "HEAD"], cwd=repo_tag_fail).strip()
    assert head_id != source_id_tf
    assert (
        _git(["rev-parse", f"{head_id}~1"], cwd=repo_tag_fail).strip() == source_id_tf
    )

    changed = _git(
        [
            "diff-tree",
            "--root",
            "-r",
            "--no-commit-id",
            "--no-renames",
            "--name-only",
            "-z",
            head_id,
        ],
        cwd=repo_tag_fail,
    ).split("\x00")
    changed = sorted(p for p in changed if p)
    assert changed == sorted(
        [
            f".opentimestamps/{source_id_tf}.ots",
            f".opentimestamps/{source_id_tf}.json",
        ]
    )
    assert _git(["tag", "-l"], cwd=repo_tag_fail).strip() == ""

    # 4. Rerun after tag failure creates the missing tag and avoids a second commit.
    submissions2: list[tuple[str, bytes]] = []

    def submit2(commit: str, payload: bytes) -> bytes:
        submissions2.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(
        snapshot=build_snapshot(cwd=repo_tag_fail, config=config, now=now),
        now=now,
        submit=submit2,
    )

    tag_names = [
        n
        for n in _git(["tag", "-l", "ots/*"], cwd=repo_tag_fail).strip().split("\n")
        if n
    ]
    assert len(tag_names) == 1
    assert (
        _git(["rev-parse", f"{tag_names[0]}^{{commit}}"], cwd=repo_tag_fail).strip()
        == source_id_tf
    )
    assert _git(["rev-parse", "HEAD"], cwd=repo_tag_fail).strip() == head_id
    assert submissions2 == []


def test_run_does_not_tag_when_persistence_fails(tmp_path: Path) -> None:
    """Persistence failure prevents tag creation so partial state remains safe."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )
    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    calls: list[object] = []

    def submit(commit: str, payload: bytes) -> bytes:
        calls.append(("submit", commit, payload))
        return _make_fake_detached_proof(payload=payload)

    def persist(commit: str, proof: bytes) -> None:
        calls.append(("persist", commit, proof))
        raise PersistenceError("simulated persistence failure")

    def tag(commit: str) -> None:
        calls.append(("tag", commit))

    with pytest.raises(PersistenceError):
        run_orchestration(
            snapshot=snapshot,
            now=now,
            submit=submit,
            persist=persist,
            tag=tag,
        )

    expected_payload = build_payload(snapshot.object_format, commit_id)
    assert len(calls) == 2
    assert calls[0] == ("submit", commit_id, expected_payload)
    assert calls[1][0] == "persist"
    assert calls[1][1] == commit_id
    _assert_proof_binds_to_commit(calls[1][2], commit_id)
    assert ("tag", commit_id) not in calls


def test_run_processes_every_commit_batch_in_repository_order(tmp_path: Path) -> None:
    """With every_commit enabled, B/C/D are timestamped in repository order.

    Each selected source commit receives its own submission, source tag, and
    proof/manifest pair. All artifacts are committed together in a single
    generated proof commit carrying every source trailer.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)

    base = datetime(2026, 8, 18, 8, 0, 0, tzinfo=UTC)
    b_id = _commit(repo, paths=["b.txt"], message="B\n", date=base)
    c_id = _commit(repo, paths=["c.txt"], message="C\n", date=base + timedelta(hours=1))
    d_id = _commit(repo, paths=["d.txt"], message="D\n", date=base + timedelta(hours=2))

    config = Config(
        policy=PolicyConfig(
            every_commit=True,
            max_age=None,
            fixed_time=None,
            timezone=None,
            initial_history="all",
        ),
        git=GitConfig(
            source_ref="HEAD",
            fetch_before_run=False,
            tag_prefix="ots/",
            require_clean_worktree=False,
        ),
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )
    now = base + timedelta(hours=3)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(
        snapshot=snapshot,
        now=now,
        submit=submit,
    )

    # Three submissions in repository order B, C, D.
    expected_payloads = [
        build_payload(snapshot.object_format, commit_id)
        for commit_id in [b_id, c_id, d_id]
    ]
    assert submissions == [
        (b_id, expected_payloads[0]),
        (c_id, expected_payloads[1]),
        (d_id, expected_payloads[2]),
    ]

    # Three source tags, each pointing at its respective source commit.
    tag_names = _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n")
    tag_names = [n for n in tag_names if n]
    assert len(tag_names) == 3
    tagged_sources = {
        _git(["rev-parse", f"{name}^{{commit}}"], cwd=repo).strip()
        for name in tag_names
    }
    assert tagged_sources == {b_id, c_id, d_id}
    for name in tag_names:
        annotation = _git(["tag", "-l", "-n10", name], cwd=repo)
        assert "triggers: every_commit" in annotation

    # All proof and manifest files exist for each source.
    for source_id in [b_id, c_id, d_id]:
        _assert_proof_binds_to_commit(
            (repo / f".opentimestamps/{source_id}.ots").read_bytes(), source_id
        )
        assert (repo / f".opentimestamps/{source_id}.json").exists()

    # A single generated proof commit contains all artifacts and source trailers.
    head_id = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    assert _git(["rev-parse", f"{head_id}~1"], cwd=repo).strip() == d_id

    changed = _git(
        [
            "diff-tree",
            "--root",
            "-r",
            "--no-commit-id",
            "--no-renames",
            "--name-only",
            "-z",
            head_id,
        ],
        cwd=repo,
    ).split("\x00")
    changed = sorted(p for p in changed if p)
    expected_paths = sorted(
        f".opentimestamps/{source_id}.{ext}"
        for source_id in [b_id, c_id, d_id]
        for ext in ("ots", "json")
    )
    assert changed == expected_paths

    message = _git(["log", "-1", "--format=%B", head_id], cwd=repo)
    assert "OpenTimestamps-Generated: true\n" in message
    assert "Store OpenTimestamps proofs\n" in message
    for source_id in [b_id, c_id, d_id]:
        assert f"OpenTimestamps-Source: {source_id}\n" in message


def test_run_avoids_duplicate_work_for_overlapping_triggers_and_reruns(
    tmp_path: Path,
) -> None:
    """Overlapping max-age and fixed-time triggers produce one submission; rerun is a no-op.

    The first run must create exactly one timestamp submission, one source tag, and
    one generated proof commit. A second run immediately afterwards must observe
    the new baseline and pending state, perform zero submissions, zero tags, and
    zero commits, and exit cleanly.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)

    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
            fixed_time=time(0, 0),
            timezone=ZoneInfo("UTC"),
            initial_history="latest",
        ),
        git=GitConfig(
            source_ref="HEAD",
            fetch_before_run=False,
            tag_prefix="ots/",
            require_clean_worktree=False,
        ),
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )
    now = commit_date + timedelta(hours=2)

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    assert snapshot.decision.commits
    assert {"max_age", "fixed_time"} <= snapshot.decision.triggers

    run_orchestration(
        snapshot=snapshot,
        now=now,
        submit=submit,
    )

    # Exactly one submission despite both triggers being due.
    assert len(submissions) == 1
    assert submissions[0][0] == source_id

    tag_names = _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n")
    tag_names = [n for n in tag_names if n]
    assert len(tag_names) == 1
    assert (
        _git(["rev-parse", f"{tag_names[0]}^{{commit}}"], cwd=repo).strip() == source_id
    )

    head_before_rerun = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    assert (repo / f".opentimestamps/{source_id}.ots").exists()
    assert (repo / f".opentimestamps/{source_id}.json").exists()

    # Second run: rebuild snapshot from the updated repository state.
    submissions.clear()
    snapshot2 = build_snapshot(cwd=repo, config=config, now=now)
    assert not snapshot2.decision.commits
    assert snapshot2.decision.triggers == frozenset()

    run_orchestration(
        snapshot=snapshot2,
        now=now,
        submit=submit,
    )

    assert submissions == []
    assert _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") == tag_names
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == head_before_rerun


def test_run_recovers_when_proof_exists_but_tag_is_absent(tmp_path: Path) -> None:
    """A valid proof/manifest pair without a tag continues the transaction.

    The implementation must inspect the on-disk artifacts, recognize that the
    source was already submitted, skip the timestamp submission, create the
    annotated source tag, and commit the existing artifacts if configured.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )

    # Pre-create valid recovery artifacts so the prior run crashed after
    # persistence but before tagging.
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    proof_path = proof_dir / f"{source_id}.ots"
    manifest_path = proof_dir / f"{source_id}.json"
    payload = build_payload("sha1", source_id)
    proof_bytes = _make_fake_detached_proof(payload=payload)
    proof_path.write_bytes(proof_bytes)
    manifest = build_manifest(
        object_format="sha1",
        commit_id=source_id,
        proof_name=f"{source_id}.ots",
        source_ref="refs/heads/main",
        submitted_at=commit_date + timedelta(minutes=30),
        triggers=["max_age"],
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(
        snapshot=snapshot,
        now=now,
        submit=submit,
    )

    # No resubmission occurred; recovery continued the prior transaction.
    assert submissions == []

    # One annotated tag pointing at the source commit.
    tag_names = _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n")
    tag_names = [n for n in tag_names if n]
    assert len(tag_names) == 1
    assert (
        _git(["rev-parse", f"{tag_names[0]}^{{commit}}"], cwd=repo).strip() == source_id
    )

    # The existing artifacts were committed in a generated proof commit.
    head_id = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    assert head_id != source_id
    assert _git(["rev-parse", f"{head_id}~1"], cwd=repo).strip() == source_id
    changed = _git(
        [
            "diff-tree",
            "--root",
            "-r",
            "--no-commit-id",
            "--no-renames",
            "--name-only",
            "-z",
            head_id,
        ],
        cwd=repo,
    ).split("\x00")
    changed = sorted(p for p in changed if p)
    assert changed == sorted(
        [f".opentimestamps/{source_id}.ots", f".opentimestamps/{source_id}.json"]
    )

    # Original recovered proof bytes are preserved.
    assert (repo / f".opentimestamps/{source_id}.ots").read_bytes() == proof_bytes


def test_run_recovery_uses_original_manifest_for_tag(tmp_path: Path) -> None:
    """A recovered proof/manifest pair drives the timestamp tag metadata.

    The recreated tag must use the manifest's original submitted_at and
    triggers, not the current invocation time or the current policy decision.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )

    original_submitted = commit_date + timedelta(minutes=30)
    original_triggers = ["fixed_time"]
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    proof_path = proof_dir / f"{source_id}.ots"
    manifest_path = proof_dir / f"{source_id}.json"
    recovered_proof = _make_fake_detached_proof(
        payload=build_payload("sha1", source_id)
    )
    proof_path.write_bytes(recovered_proof)
    manifest = build_manifest(
        object_format="sha1",
        commit_id=source_id,
        proof_name=f"{source_id}.ots",
        source_ref="refs/heads/main",
        submitted_at=original_submitted,
        triggers=original_triggers,
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # Choose a run time and policy that would produce different tag metadata
    # if the implementation used the invocation state instead of the manifest.
    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(
        snapshot=snapshot,
        now=now,
        submit=submit,
    )

    assert submissions == []

    tag_names = [
        n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n
    ]
    assert len(tag_names) == 1
    tag_name = tag_names[0]
    assert _git(["rev-parse", f"{tag_name}^{{commit}}"], cwd=repo).strip() == source_id

    # Tag name embeds the original submission time, not the current run time.
    expected_utc = original_submitted.astimezone(UTC)
    expected_name = f"ots/{expected_utc.strftime('%Y%m%dT%H%M%SZ')}/{source_id[:12]}"
    assert tag_name == expected_name

    annotation = _git(["cat-file", "tag", tag_name], cwd=repo)
    assert (
        f"submitted-at: {expected_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}\n" in annotation
    )
    assert "triggers: fixed_time\n" in annotation
    assert "max_age" not in annotation


def test_run_recovers_tag_from_proof_commit_even_when_parent_is_not_branch_tip(
    tmp_path: Path,
) -> None:
    """A valid manifest in a committed proof commit drives tag recovery.

    The proof commit's parent is the stamped source, but a later meaningful
    commit exists on top of the proof commit. The run must create the missing
    tag from the manifest's source_commit, submitted_at, and triggers without
    resubmitting.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=24),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )

    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    proof_path = proof_dir / f"{source_id}.ots"
    manifest_path = proof_dir / f"{source_id}.json"
    proof_bytes = _make_fake_detached_proof(payload=build_payload("sha1", source_id))
    proof_path.write_bytes(proof_bytes)
    original_submitted = commit_date + timedelta(minutes=30)
    original_triggers = ["max_age"]
    manifest = build_manifest(
        object_format="sha1",
        commit_id=source_id,
        proof_name=f"{source_id}.ots",
        source_ref="refs/heads/main",
        submitted_at=original_submitted,
        triggers=original_triggers,
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    stage_paths(
        cwd=repo,
        paths=[
            f".opentimestamps/{source_id}.ots",
            f".opentimestamps/{source_id}.json",
        ],
    )
    create_generated_proof_commit(
        cwd=repo,
        source_commit_ids=[source_id],
        proof_directory=".opentimestamps",
        paths=[
            f".opentimestamps/{source_id}.ots",
            f".opentimestamps/{source_id}.json",
        ],
    )

    # A later meaningful commit sits on top of the generated proof commit, so
    # the stamped source is no longer the branch tip.
    later_id = _commit(
        repo,
        paths=["b.txt"],
        message="B\n",
        date=commit_date + timedelta(minutes=45),
    )

    now = commit_date + timedelta(minutes=50)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    assert not snapshot.decision.commits

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(
        snapshot=snapshot,
        now=now,
        submit=submit,
    )

    assert submissions == []

    tag_names = [
        n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n
    ]
    assert len(tag_names) == 1
    tag_name = tag_names[0]
    assert _git(["rev-parse", f"{tag_name}^{{commit}}"], cwd=repo).strip() == source_id

    expected_utc = original_submitted.astimezone(UTC)
    expected_name = f"ots/{expected_utc.strftime('%Y%m%dT%H%M%SZ')}/{source_id[:12]}"
    assert tag_name == expected_name

    annotation = _git(["cat-file", "tag", tag_name], cwd=repo)
    assert (
        f"submitted-at: {expected_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}\n" in annotation
    )
    assert "triggers: max_age\n" in annotation

    # HEAD must not move; the recovery path only creates the missing tag.
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == later_id


def test_run_diagnoses_corrupt_manifest_by_testing_candidates(tmp_path: Path) -> None:
    """A corrupt manifest in a committed proof commit is diagnosed, not ignored.

    The command tests the proof commit's parent and each pending commit as
    candidates using payload-binding validation, reports the identified
    source, creates no tag, and records zero submissions. The unresolved
    state maps to exit code 5.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=24),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )

    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    proof_path = proof_dir / f"{source_id}.ots"
    manifest_path = proof_dir / f"{source_id}.json"
    proof_bytes = _make_fake_detached_proof(payload=build_payload("sha1", source_id))
    proof_path.write_bytes(proof_bytes)
    manifest_path.write_text("{ this is not valid json\n", encoding="utf-8")

    stage_paths(
        cwd=repo,
        paths=[
            f".opentimestamps/{source_id}.ots",
            f".opentimestamps/{source_id}.json",
        ],
    )
    create_generated_proof_commit(
        cwd=repo,
        source_commit_ids=[source_id],
        proof_directory=".opentimestamps",
        paths=[
            f".opentimestamps/{source_id}.ots",
            f".opentimestamps/{source_id}.json",
        ],
    )

    later_id = _commit(
        repo,
        paths=["b.txt"],
        message="B\n",
        date=commit_date + timedelta(minutes=45),
    )

    now = commit_date + timedelta(minutes=50)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    assert not snapshot.decision.commits

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    with pytest.raises(PersistenceError, match=source_id[:12]):
        run_orchestration(
            snapshot=snapshot,
            now=now,
            submit=submit,
        )

    assert submissions == []
    assert _git(["tag", "-l", "ots/*"], cwd=repo).strip() == ""
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == later_id


def test_run_recovery_rejects_inconsistent_existing_tag(tmp_path: Path) -> None:
    """An existing tag that conflicts with the recovered manifest is rejected.

    Recovery must not resubmit the source when a tag already exists with the
    same name but inconsistent annotation.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )

    original_submitted = commit_date + timedelta(minutes=30)
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    proof_path = proof_dir / f"{source_id}.ots"
    manifest_path = proof_dir / f"{source_id}.json"
    proof_path.write_bytes(
        _make_fake_detached_proof(payload=build_payload("sha1", source_id))
    )
    manifest = build_manifest(
        object_format="sha1",
        commit_id=source_id,
        proof_name=f"{source_id}.ots",
        source_ref="refs/heads/main",
        submitted_at=original_submitted,
        triggers=["max_age"],
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # Create a tag with the same name as the recovered manifest would produce,
    # but with an annotation that does not match the manifest.
    expected_utc = original_submitted.astimezone(UTC)
    tag_name = f"ots/{expected_utc.strftime('%Y%m%dT%H%M%SZ')}/{source_id[:12]}"
    _git(
        ["tag", "-a", "-m", "inconsistent annotation", tag_name, source_id],
        cwd=repo,
    )

    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    with pytest.raises(InvalidRepositoryStateError):
        run_orchestration(
            snapshot=snapshot,
            now=now,
            submit=submit,
        )

    assert submissions == []

    # The existing tag is untouched and still points at the source.
    assert _git(["rev-parse", f"{tag_name}^{{commit}}"], cwd=repo).strip() == source_id


def test_run_recovers_when_tag_exists_but_proof_commit_is_absent(
    tmp_path: Path,
) -> None:
    """A valid source tag with uncommitted proof/manifest artifacts finishes the commit.

    The implementation must recognize that the source is already timestamped,
    skip submission and any new tag creation, and create exactly one generated
    proof commit containing the existing artifacts.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )

    # Pre-create recovery artifacts and the source tag, simulating a crash
    # after tag creation but before the generated proof commit.
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    proof_path = proof_dir / f"{source_id}.ots"
    manifest_path = proof_dir / f"{source_id}.json"
    proof_bytes = _make_fake_detached_proof(payload=build_payload("sha1", source_id))
    proof_path.write_bytes(proof_bytes)
    manifest = build_manifest(
        object_format="sha1",
        commit_id=source_id,
        proof_name=f"{source_id}.ots",
        source_ref="refs/heads/main",
        submitted_at=commit_date + timedelta(minutes=30),
        triggers=["max_age"],
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # Create the annotated source tag as if the prior run completed tagging.
    create_timestamp_tag(
        cwd=repo,
        source_commit_id=source_id,
        tag_prefix="ots/",
        submitted_at=commit_date + timedelta(minutes=30),
        proof=f".opentimestamps/{source_id}.ots",
        triggers=frozenset({"max_age"}),
    )

    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    submissions: list[tuple[str, bytes]] = []
    tag_calls: list[str] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    def tag(commit: str) -> None:
        tag_calls.append(commit)

    run_orchestration(
        snapshot=snapshot,
        now=now,
        submit=submit,
        tag=tag,
    )

    # No resubmission or new tag creation occurred.
    assert submissions == []
    assert tag_calls == []

    # Exactly one annotated tag exists pointing at the source.
    tag_names = _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n")
    tag_names = [n for n in tag_names if n]
    assert len(tag_names) == 1
    assert (
        _git(["rev-parse", f"{tag_names[0]}^{{commit}}"], cwd=repo).strip() == source_id
    )

    # The existing artifacts were committed in a generated proof commit.
    head_id = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    assert head_id != source_id
    assert _git(["rev-parse", f"{head_id}~1"], cwd=repo).strip() == source_id
    changed = _git(
        [
            "diff-tree",
            "--root",
            "-r",
            "--no-commit-id",
            "--no-renames",
            "--name-only",
            "-z",
            head_id,
        ],
        cwd=repo,
    ).split("\x00")
    changed = sorted(p for p in changed if p)
    assert changed == sorted(
        [f".opentimestamps/{source_id}.ots", f".opentimestamps/{source_id}.json"]
    )

    message = _git(["log", "-1", "--format=%B", head_id], cwd=repo)
    assert "OpenTimestamps-Generated: true\n" in message
    assert f"OpenTimestamps-Source: {source_id}\n" in message

    # Original recovered proof bytes are preserved.
    assert (repo / f".opentimestamps/{source_id}.ots").read_bytes() == proof_bytes


def test_run_rejects_inconsistent_proof_commit_without_tag(tmp_path: Path) -> None:
    """A generated proof commit without valid recovery evidence is inconsistent.

    If a generated proof commit exists for a source but the proof/manifest pair
    is not valid recovery evidence (for example, a malformed manifest), the run
    must fail with an actionable error rather than silently resubmitting the
    source commit.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )

    # Create a generated proof commit with an invalid manifest and no tag.
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    proof_path = proof_dir / f"{source_id}.ots"
    manifest_path = proof_dir / f"{source_id}.json"
    proof_path.write_bytes(
        _make_fake_detached_proof(payload=build_payload("sha1", source_id))
    )
    manifest_path.write_text("{ this is not valid json\n", encoding="utf-8")
    stage_paths(
        cwd=repo,
        paths=[
            f".opentimestamps/{source_id}.ots",
            f".opentimestamps/{source_id}.json",
        ],
    )
    create_generated_proof_commit(
        cwd=repo,
        source_commit_ids=[source_id],
        proof_directory=".opentimestamps",
        paths=[
            f".opentimestamps/{source_id}.ots",
            f".opentimestamps/{source_id}.json",
        ],
    )

    # Ensure no timestamp tag exists.
    assert _git(["tag", "-l"], cwd=repo).strip() == ""

    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    with pytest.raises(RecoveryValidationError):
        run_orchestration(
            snapshot=snapshot,
            now=now,
            submit=submit,
        )

    # No resubmission occurred; the inconsistent state was rejected.
    assert submissions == []


@pytest.mark.parametrize(
    "artifact_state",
    [
        pytest.param("proof_only", id="proof-only"),
        pytest.param("manifest_only", id="manifest-only"),
        pytest.param("invalid_manifest", id="invalid-manifest"),
        pytest.param("mismatched_proof", id="mismatched-proof"),
        pytest.param("invalid_proof", id="invalid-proof"),
    ],
)
def test_run_fails_closed_on_incomplete_or_corrupt_recovery_artifacts(
    tmp_path: Path,
    artifact_state: str,
) -> None:
    """A pending source with partial or invalid recovery artifacts must not resubmit.

    Even when no generated proof commit claims the source, any on-disk evidence
    that looks like a prior submission must be complete and valid. Incomplete or
    corrupt state is treated as a persistence/recovery failure so the operator can
    inspect and resolve it; the tool must not overwrite, resubmit, or create a tag.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )

    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    proof_path = proof_dir / f"{source_id}.ots"
    manifest_path = proof_dir / f"{source_id}.json"

    valid_payload = build_payload("sha1", source_id)
    valid_proof = _make_fake_detached_proof(payload=valid_payload)
    valid_manifest = build_manifest(
        object_format="sha1",
        commit_id=source_id,
        proof_name=f"{source_id}.ots",
        source_ref="refs/heads/main",
        submitted_at=commit_date + timedelta(minutes=30),
        triggers=["max_age"],
    )

    if artifact_state == "proof_only":
        proof_path.write_bytes(valid_proof)
    elif artifact_state == "manifest_only":
        manifest_path.write_text(
            json.dumps(valid_manifest, indent=2) + "\n", encoding="utf-8"
        )
    elif artifact_state == "invalid_manifest":
        proof_path.write_bytes(valid_proof)
        manifest_path.write_text("{ this is not valid json\n", encoding="utf-8")
    elif artifact_state == "mismatched_proof":
        other_id = "abcdef0123456789abcdef0123456789abcdef01"
        other_payload = build_payload("sha1", other_id)
        proof_path.write_bytes(_make_fake_detached_proof(payload=other_payload))
        manifest_path.write_text(
            json.dumps(valid_manifest, indent=2) + "\n", encoding="utf-8"
        )
    elif artifact_state == "invalid_proof":
        proof_path.write_bytes(b"not an ots proof")
        manifest_path.write_text(
            json.dumps(valid_manifest, indent=2) + "\n", encoding="utf-8"
        )
    else:
        raise ValueError(f"unknown artifact state: {artifact_state!r}")

    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    with pytest.raises(RecoveryValidationError):
        run_orchestration(
            snapshot=snapshot,
            now=now,
            submit=submit,
        )

    assert submissions == []
    assert _git(["tag", "-l"], cwd=repo).strip() == ""
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == source_id
    assert not any(p.name.endswith(".tmp") for p in proof_dir.iterdir())


def test_concurrent_run_allows_only_one_submission(tmp_path: Path) -> None:
    """Two concurrent invocations share a repository-level advisory lock.

    While one process holds the lock inside a blocking fake OpenTimestamps
    submission, a second process must fail to acquire the lock and exit 7. At
    most one submission and one set of modifications may occur.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    work = tmp_path / "work"

    work.mkdir()
    called_marker = work / "called"
    release_marker = work / "release"
    fake_ots = work / "fake_ots"
    fake_ots.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys, time\n"
        f"called = pathlib.Path({str(called_marker)!r})\n"
        f"release = pathlib.Path({str(release_marker)!r})\n"
        "called.write_text('')\n"
        "for _ in range(200):\n"
        "    if release.exists():\n"
        "        break\n"
        "    time.sleep(0.05)\n"
        # The real ``ots stamp <file>`` writes ``<file>.ots``; emit a
        # structurally valid (but fake) OpenTimestamps detached proof so the
        # adapter accepts it.
        "input_path = pathlib.Path(sys.argv[2])\n"
        "payload = input_path.read_bytes()\n"
        "digest = __import__('hashlib').sha256(payload).digest()\n"
        "proof = (\n"
        "    b'\\x00OpenTimestamps\\x00\\x00Proof\\x00\\xbf\\x89\\xe2\\xe8\\x84\\xe8\\x92\\x94'\n"
        "    + b'\\x01\\x08' + digest\n"
        "    + b'\\x00\\x83\\xdf\\xe3\\x0d\\x2e\\xf9\\x0c\\x8e\\x0bexample.com'\n"
        ")\n"
        "(input_path.parent / (input_path.name + '.ots')).write_bytes(proof)\n"
        "sys.exit(0)\n"
    )
    fake_ots.chmod(0o755)

    _set_config(
        repo,
        max_age="1h",
        fetch_before_run=False,
        commit=True,
        command=str(fake_ots),
    )

    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root / "src")

    first = subprocess.Popen(
        [sys.executable, "-m", "git_ots", "run"],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def _wait_for(predicate, timeout: float = 5.0) -> None:
        deadline = time_module.monotonic() + timeout
        while not predicate():
            if time_module.monotonic() > deadline:
                raise AssertionError(
                    "timeout waiting for first process to start submission"
                )
            time_module.sleep(0.05)

    try:
        _wait_for(called_marker.exists)

        second = subprocess.run(
            [sys.executable, "-m", "git_ots", "run"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert second.returncode == 7, second.stderr
        assert "lock" in second.stderr.lower()

        release_marker.write_text("")
        _stdout, stderr = first.communicate(timeout=10)
        assert first.returncode == 0, stderr.decode("utf-8", errors="replace")
    finally:
        if first.poll() is None:
            release_marker.write_text("")
            first.terminate()
            first.wait(timeout=5)

    tag_names = [
        n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n
    ]
    assert len(tag_names) == 1
    assert (
        _git(["rev-parse", f"{tag_names[0]}^{{commit}}"], cwd=repo).strip() == source_id
    )

    head_id = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    assert head_id != source_id
    assert _git(["rev-parse", f"{head_id}~1"], cwd=repo).strip() == source_id


def test_submit_kills_background_child_of_a_forking_ots_script(tmp_path: Path) -> None:
    """AC-PROC-1: killing the parent on timeout must not orphan its child.

    The configured ``ots`` command is a real executable that spawns its own
    long-lived background child (standing in for ``ots`` spawning its own
    network children) and then hangs -- standing in for a calendar that
    accepts a connection and never responds. When
    ``OpenTimestampsCli.submit`` gives up at the configured ceiling, the
    whole process group -- not just the direct ``ots`` process -- must be
    killed, or the background child is left running indefinitely.

    An implementation that kills only the direct child fails this test: the
    background child would still answer the liveness probe below throughout
    the whole 5-second poll window, since nothing reparented onto init ever
    dies on its own within that window.
    """
    child_pid_marker = tmp_path / "child_pid"
    fake_ots = tmp_path / "fake_ots"
    fake_ots.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, subprocess, sys, time\n"
        f"marker = pathlib.Path({str(child_pid_marker)!r})\n"
        "grandchild = subprocess.Popen(\n"
        f"    [{sys.executable!r}, '-c', 'import time; time.sleep(600)']\n"
        ")\n"
        "marker.write_text(str(grandchild.pid))\n"
        "time.sleep(600)\n",
        encoding="utf-8",
    )
    fake_ots.chmod(0o755)

    commit_id = "0" * 40
    payload = build_payload("sha1", commit_id)
    request = TimestampRequest(
        object_format="sha1", commit_id=commit_id, payload=payload
    )
    client = OpenTimestampsCli(command=str(fake_ots), limit=timedelta(seconds=1.0))

    with pytest.raises(SubmissionTimeoutError):
        client.submit(request)

    def _wait_for(predicate, timeout: float = 5.0) -> None:
        deadline = time_module.monotonic() + timeout
        while not predicate():
            if time_module.monotonic() > deadline:
                raise AssertionError("condition not met within the timeout")
            time_module.sleep(0.05)

    # The fake script writes the marker well before the 1s ceiling fires,
    # but wait for it defensively rather than assuming it beat the deadline.
    _wait_for(child_pid_marker.exists, timeout=5.0)
    grandchild_pid = int(child_pid_marker.read_text())

    def _grandchild_not_running() -> bool:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            return True
        return False

    _wait_for(_grandchild_not_running, timeout=5.0)


def test_submit_kills_background_child_even_when_caller_exits_immediately(
    tmp_path: Path,
) -> None:
    """AC-PROC-1, production shape: the kill must not depend on a thread surviving.

    The ceiling is enforced by two racing timers armed with the same limit:
    ``OpenTimestampsCli._invoke``'s outer ``worker.join()``, and the
    production runner's own inner ``Popen.communicate(timeout=)``. Only the
    inner layer's background thread can perform the real kill on its own; if
    the outer layer's deadline fires first, ``_invoke`` must kill the child
    itself, synchronously, before raising -- because if it just raises and
    abandons the (still-running) worker thread, that thread only completes
    the kill if it is ever scheduled again. A caller that catches the
    timeout and exits the process immediately (exactly what the real CLI
    does) gives an abandoned daemon thread no such chance.

    ``test_submit_kills_background_child_of_a_forking_ots_script`` above
    cannot see this: it keeps the pytest process alive while polling for up
    to 5 seconds, which is more than enough time for an abandoned thread to
    get scheduled and finish the kill regardless of which timer fired first.
    Only a driver that exits immediately after the timeout -- not "shortly
    after", but with no further scheduling opportunity at all -- can tell
    "killed synchronously by the caller" apart from "killed eventually by an
    abandoned thread, if the process happens to stay alive long enough".

    Because which of the two timers fires first is a sub-millisecond
    scheduling race (confirmed by direct measurement: as close as 0.4ms
    apart, either side capable of winning), a single trial is not reliable
    proof either way. The driver is run several times; every single
    grandchild must be gone, since any survivor demonstrates the kill was
    left to a thread that was never scheduled again.

    The same race also has a second failure mode on the *other* side: if the
    inner layer's own deadline fires first, it must raise
    SubmissionTimeoutError itself rather than returning a SubprocessResult
    describing the killed child as an ordinary exit (which would surface as
    a plain SubmissionError -- "opentimestamps exited with code -9", the
    exact false claim AC-ERR-3 forbids). The driver checks the raised
    exception's *type*, not just that submit() eventually raised something,
    so this test fails-by-construction against either half of the race
    being mishandled -- see the distinct exit codes below.
    """
    # Distinct driver exit codes so a failure names *which* invariant broke,
    # rather than the trial just asserting `returncode == 0`:
    #   0 = SubmissionTimeoutError raised, as required.
    #   3 = submit() returned normally against a script that only hangs.
    #   4 = a plain SubmissionError (not the Timeout subclass) was raised --
    #       the signature of the inner runner returning a killed-but-"exited"
    #       result instead of raising, e.g. "opentimestamps exited with -9".
    NO_EXCEPTION_RAISED = 3
    WRONG_EXCEPTION_TYPE = 4

    driver_script = tmp_path / "driver.py"
    driver_script.write_text(
        "import os, sys\n"
        "from datetime import timedelta\n"
        "from git_ots.timestamp import (\n"
        "    OpenTimestampsCli,\n"
        "    SubmissionError,\n"
        "    SubmissionTimeoutError,\n"
        "    TimestampRequest,\n"
        "    build_payload,\n"
        ")\n"
        "\n"
        "fake_ots, marker_path, limit_seconds = sys.argv[1], sys.argv[2], sys.argv[3]\n"
        "commit_id = '0' * 40\n"
        "payload = build_payload('sha1', commit_id)\n"
        "request = TimestampRequest(\n"
        "    object_format='sha1', commit_id=commit_id, payload=payload\n"
        ")\n"
        "client = OpenTimestampsCli(\n"
        "    command=fake_ots, limit=timedelta(seconds=float(limit_seconds))\n"
        ")\n"
        "try:\n"
        "    client.submit(request)\n"
        "except SubmissionTimeoutError:\n"
        "    pass\n"
        f"except SubmissionError:\n"
        f"    os._exit({WRONG_EXCEPTION_TYPE})\n"
        "else:\n"
        f"    os._exit({NO_EXCEPTION_RAISED})\n"
        "# Simulate the real CLI: catch the timeout and unwind straight to\n"
        "# process exit, with no further chance for any background thread\n"
        "# to be scheduled -- os._exit skips even normal interpreter\n"
        "# shutdown, so this is the harshest realistic case.\n"
        "os._exit(0)\n",
        encoding="utf-8",
    )

    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root / "src")

    def _wait_for(predicate, timeout: float = 5.0) -> None:
        deadline = time_module.monotonic() + timeout
        while not predicate():
            if time_module.monotonic() > deadline:
                raise AssertionError("condition not met within the timeout")
            time_module.sleep(0.05)

    def _not_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    trials = 5
    for trial in range(trials):
        trial_dir = tmp_path / f"trial-{trial}"
        trial_dir.mkdir()
        child_pid_marker = trial_dir / "child_pid"
        fake_ots = trial_dir / "fake_ots"
        fake_ots.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, subprocess, sys, time\n"
            f"marker = pathlib.Path({str(child_pid_marker)!r})\n"
            "grandchild = subprocess.Popen(\n"
            f"    [{sys.executable!r}, '-c', 'import time; time.sleep(600)']\n"
            ")\n"
            "marker.write_text(str(grandchild.pid))\n"
            "time.sleep(600)\n",
            encoding="utf-8",
        )
        fake_ots.chmod(0o755)

        completed = subprocess.run(
            [
                sys.executable,
                str(driver_script),
                str(fake_ots),
                str(child_pid_marker),
                "1.0",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.returncode == NO_EXCEPTION_RAISED:
            raise AssertionError(
                f"trial {trial}: submit() returned normally against a script "
                f"that only hangs -- expected SubmissionTimeoutError"
            )
        if completed.returncode == WRONG_EXCEPTION_TYPE:
            raise AssertionError(
                f"trial {trial}: submit() raised a plain SubmissionError "
                f"instead of SubmissionTimeoutError -- the inner runner "
                f"returned a killed result (e.g. exit code -SIGKILL) instead "
                f"of raising on its own timeout"
            )
        assert completed.returncode == 0, (
            f"trial {trial}: driver exited {completed.returncode} unexpectedly; "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        )

        _wait_for(child_pid_marker.exists, timeout=5.0)
        grandchild_pid = int(child_pid_marker.read_text())
        _wait_for(lambda pid=grandchild_pid: _not_running(pid), timeout=5.0)


# --- S4: end-to-end wiring and interruption ---------------------------------


@pytest.mark.parametrize(
    "ots_timeout",
    [None, "0"],
    ids=["bounded-built-in-default", "unbounded-ots-timeout-zero"],
)
def test_sigint_during_run_kills_background_child_of_a_forking_ots_script(
    tmp_path: Path,
    ots_timeout: str | None,
) -> None:
    """AC-PROC-3: SIGINT during a run kills the forking ots script's background child.

    Reuses the ``forking script`` fixture shape from
    ``test_submit_kills_background_child_of_a_forking_ots_script`` (S2's
    AC-PROC-1 vehicle, per stories.json's note that S4 does not need a second
    one) but interrupts with a real SIGINT delivered to a real ``git-ots run``
    process instead of a configured timeout. ``start_new_session=True`` moves
    the ``ots`` child (and its own grandchild) out of the terminal's
    foreground process group, so a ``KeyboardInterrupt`` handler that returns
    a non-zero exit without terminating the child's process group would leave
    the grandchild running for the whole 5-second poll window below.

    Parametrized over the built-in default (no ``[limits]``, so
    ``_invoke`` bounds ``submit()`` on a background worker thread) and
    ``ots_timeout = "0"`` (unbounded, so ``_default_runner`` runs inline on
    the main thread instead). The two configurations are not equivalent: with
    ``limit is None``, a ``KeyboardInterrupt`` unwinds through
    ``_default_runner`` itself, so its own ``finally: on_exit()`` clears
    ``OpenTimestampsCli``'s child-pgid registration *before* any outer
    ``except KeyboardInterrupt`` runs, making a caller's
    ``terminate_active_child()`` a silent no-op unless ``_default_runner``
    also kills the process group itself on any exception (not just its own
    timeout) -- this is what pins that fix in place.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    child_pid_marker = tmp_path / "child_pid"
    fake_ots = tmp_path / "fake_ots"
    fake_ots.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, subprocess, sys, time\n"
        f"marker = pathlib.Path({str(child_pid_marker)!r})\n"
        "grandchild = subprocess.Popen(\n"
        f"    [{sys.executable!r}, '-c', 'import time; time.sleep(600)']\n"
        ")\n"
        "marker.write_text(str(grandchild.pid))\n"
        "time.sleep(600)\n",
        encoding="utf-8",
    )
    fake_ots.chmod(0o755)

    config_kwargs: dict[str, object] = {
        "max_age": "1h",
        "fetch_before_run": False,
        "commit": True,
        "command": str(fake_ots),
    }
    if ots_timeout is not None:
        config_kwargs["ots_timeout"] = ots_timeout
    _set_config(repo, **config_kwargs)

    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root / "src")

    proc = subprocess.Popen(
        [sys.executable, "-m", "git_ots", "run"],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def _wait_for(predicate, timeout: float = 5.0) -> None:
        deadline = time_module.monotonic() + timeout
        while not predicate():
            if time_module.monotonic() > deadline:
                raise AssertionError("condition not met within the timeout")
            time_module.sleep(0.05)

    try:
        _wait_for(child_pid_marker.exists, timeout=5.0)
        grandchild_pid = int(child_pid_marker.read_text())

        proc.send_signal(signal.SIGINT)
        _stdout, stderr = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)

    stderr_text = stderr.decode("utf-8", errors="replace")
    assert proc.returncode != 0
    assert "Traceback" not in stderr_text

    def _grandchild_not_running() -> bool:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            return True
        return False

    _wait_for(_grandchild_not_running, timeout=5.0)


def test_sigint_during_status_kills_background_child_of_a_forking_git_script(
    tmp_path: Path,
) -> None:
    """Regression (post-epic HIGH finding, found by independent review after
    commit 7c889ed): SIGINT must kill a hung git child's process group too,
    not only the ots adapter's.

    Pre-fix, ``git.py:_run_bounded_git_subprocess`` had no interrupt guard
    (unlike ``timestamp.py:_default_runner``'s ``except BaseException:
    _kill_process_group(pgid); raise``), so ``start_new_session=True`` --
    needed for the *timeout* path's own group-kill -- had the side effect of
    moving every bounded git child out of the terminal's foreground process
    group with nothing left to catch Ctrl-C for it: a real regression from
    the pre-epic behaviour, where an unbounded ``subprocess.run`` child at
    least sat in the foreground group and died with the parent.

    The ``git`` command name is not configurable the way ``ots``'s is (see
    the Test vehicles precedent for AC-ERR-2/4), so the fake binary here is
    planted by shadowing ``git`` on ``PATH`` rather than via config. This
    drives the built-in default runner directly (``_default_process_runner``,
    AC-PROC-4's unconfigured window) through ``cli.py:_locate_repository``'s
    first ``git rev-parse`` call, which runs *before* any config file is
    read -- so no ``[policy]``/``[proof]``/``[opentimestamps]`` table, and no
    real Git repository, is needed to reach it; the fake ``git`` ignores its
    argv entirely and just forks a background child, then hangs.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    child_pid_marker = tmp_path / "child_pid"
    fake_git_bin = tmp_path / "fake_git_bin"
    fake_git_bin.mkdir()
    fake_git = fake_git_bin / "git"
    fake_git.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, subprocess, sys, time\n"
        f"marker = pathlib.Path({str(child_pid_marker)!r})\n"
        "grandchild = subprocess.Popen(\n"
        f"    [{sys.executable!r}, '-c', 'import time; time.sleep(600)']\n"
        ")\n"
        "marker.write_text(str(grandchild.pid))\n"
        "time.sleep(600)\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)

    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root / "src")
    # Shadow the real `git` binary: the fake one must be found first.
    env["PATH"] = f"{fake_git_bin}{os.pathsep}{env.get('PATH', '')}"

    proc = subprocess.Popen(
        [sys.executable, "-m", "git_ots", "status"],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def _wait_for(predicate, timeout: float = 5.0) -> None:
        deadline = time_module.monotonic() + timeout
        while not predicate():
            if time_module.monotonic() > deadline:
                raise AssertionError("condition not met within the timeout")
            time_module.sleep(0.05)

    try:
        _wait_for(child_pid_marker.exists, timeout=5.0)
        grandchild_pid = int(child_pid_marker.read_text())

        proc.send_signal(signal.SIGINT)
        _stdout, stderr = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)

    stderr_text = stderr.decode("utf-8", errors="replace")
    assert proc.returncode != 0
    assert "Traceback" not in stderr_text

    def _grandchild_not_running() -> bool:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            return True
        return False

    _wait_for(_grandchild_not_running, timeout=5.0)


def test_run_with_unbounded_ots_timeout_threads_none_into_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-UX-1: `ots_timeout = "0"` reaches OpenTimestampsCli as `limit=None`.

    A wiring assertion first: an implementation that coalesces ``None`` into
    some substituted ceiling (e.g. ``config.limits.ots_timeout or DEFAULT``)
    would capture something other than ``None`` here; waiting past a
    wrongly-substituted ceiling (the documented default is 120s) is not
    practical in a test. A behavioural assertion then drives a real
    slow-but-finite fake ``ots`` through the unpatched client and confirms
    the run succeeds without raising, ruling out the opposite failure mode --
    ``"0"`` being mistaken for an *immediate* timeout instead of an unbounded
    one.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    now = commit_date + timedelta(hours=2)

    captured_limits: list[timedelta | None] = []

    class _SpyOpenTimestampsCli(OpenTimestampsCli):
        def __init__(self, command, *, runner=None, limit=None):
            captured_limits.append(limit)
            super().__init__(command, runner=runner, limit=limit)

    monkeypatch.setattr(
        "git_ots.orchestration.OpenTimestampsCli", _SpyOpenTimestampsCli
    )

    fake_ots = tmp_path / "fake_ots"
    fake_ots.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys, time\n"
        "time.sleep(0.3)\n"
        "input_path = pathlib.Path(sys.argv[2])\n"
        "payload = input_path.read_bytes()\n"
        "digest = __import__('hashlib').sha256(payload).digest()\n"
        "proof = (\n"
        "    b'\\x00OpenTimestamps\\x00\\x00Proof\\x00\\xbf\\x89\\xe2\\xe8\\x84\\xe8\\x92\\x94'\n"
        "    + b'\\x01\\x08' + digest\n"
        "    + b'\\x00\\x83\\xdf\\xe3\\x0d\\x2e\\xf9\\x0c\\x8e\\x0bexample.com'\n"
        ")\n"
        "(input_path.parent / (input_path.name + '.ots')).write_bytes(proof)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    fake_ots.chmod(0o755)

    _set_config(
        repo,
        max_age="1h",
        fetch_before_run=False,
        commit=True,
        command=str(fake_ots),
        ots_timeout="0",
    )

    started = time_module.monotonic()
    exit_code = main(argv=["run"], now=now, cwd=repo)
    elapsed = time_module.monotonic() - started

    assert exit_code == 0
    assert captured_limits == [None]
    assert elapsed >= 0.25, "returned before the fake ots script's own sleep completed"
    assert (repo / ".opentimestamps" / f"{commit_id}.ots").exists()


def test_run_supplies_configured_ots_timeout_to_the_submit_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured, positive `ots_timeout` must reach the OpenTimestampsCli
    built at orchestration.py:194 for the `run` path.

    AC-UX-1's own test (above) only proves `limit is None` reaches the
    constructor when `ots_timeout = "0"` -- which is exactly what an
    *unwired* constructor also yields, since `None` is
    `OpenTimestampsCli.__init__`'s own parameter default. A dropped
    `limit=snapshot.config.limits.ots_timeout` would pass that test for the
    wrong reason. This test configures a positive value distinguishable from
    both `None` and the built-in default (120s) -- mirroring the treatment
    AC-UX-4 already gives the `upgrade` path -- so it fails if the wiring at
    orchestration.py:194 is dropped entirely. No real `ots` subprocess is
    needed: the spy's own `submit()` raises immediately once the
    constructor argument is captured.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    now = commit_date + timedelta(hours=2)

    captured_limits: list[timedelta | None] = []

    class _SpyOpenTimestampsCli(OpenTimestampsCli):
        def __init__(self, command, *, runner=None, limit=None):
            captured_limits.append(limit)
            super().__init__(command, runner=runner, limit=limit)

        def submit(self, request):
            raise SubmissionTimeoutError(
                command="stop-here-wiring-already-captured", limit=timedelta(seconds=45)
            )

    monkeypatch.setattr(
        "git_ots.orchestration.OpenTimestampsCli", _SpyOpenTimestampsCli
    )

    _set_config(repo, max_age="1h", fetch_before_run=False, ots_timeout="45s")

    exit_code = main(argv=["run"], now=now, cwd=repo)

    assert exit_code == 4
    assert captured_limits == [timedelta(seconds=45)]


def test_upgrade_proofs_enforces_configured_ots_timeout_not_the_built_in_default(
    tmp_path: Path,
) -> None:
    """AC-UX-4: `limits.ots_timeout` reaches the OpenTimestampsCli constructed
    at upgrade.py:444, demonstrated by an actual timing boundary rather than
    merely an exit code.

    The configured ceiling (1s, the smallest expressible value per Design
    Decision 11) is distinguishable from the built-in default (120s): the
    fake ``ots`` only hangs, and the timeout is asserted to fire well under
    120s. An implementation that wires the limit into the `run` path but not
    `upgrade` would instead hang -- not for 120s, but forever, since a
    dropped `limit=` argument defaults to `OpenTimestampsCli.__init__`'s own
    `None` (unbounded).

    Driven through a real subprocess with an outer `subprocess.run(timeout=)`
    (mirroring S2's AC-PROC-1 driver-script pattern) rather than calling
    `upgrade_proofs` in-process: an in-process call has no way to bound its
    own wait against a genuinely unbounded regression, so a dropped `limit=`
    would hang the whole test *suite*, not just fail this test -- observed
    directly (blocked past two minutes) before this was fixed.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n")

    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    payload = build_payload("sha1", commit_id)
    proof_bytes = _make_fake_detached_proof(payload=payload)
    (proof_dir / f"{commit_id}.ots").write_bytes(proof_bytes)
    manifest = {
        "schema": 1,
        "source_commit": commit_id,
        "git_object_format": "sha1",
        "payload_format": "git-commit-id-v1",
        "submitted_at": "2026-08-09T00:00:03Z",
        "proof": f"{commit_id}.ots",
        "source_ref": "refs/heads/main",
        "triggers": ["max_age"],
    }
    (proof_dir / f"{commit_id}.json").write_text(
        json.dumps(manifest) + "\n", encoding="utf-8"
    )

    fake_ots = tmp_path / "fake_ots"
    fake_ots.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(600)\n", encoding="utf-8"
    )
    fake_ots.chmod(0o755)

    driver_script = tmp_path / "driver.py"
    driver_script.write_text(
        "import sys\n"
        "from datetime import timedelta\n"
        "from git_ots.config import (\n"
        "    Config, GitConfig, LimitsConfig, OpenTimestampsConfig, PolicyConfig,\n"
        "    ProofConfig,\n"
        ")\n"
        "from git_ots.timestamp import SubmissionTimeoutError\n"
        "from git_ots.upgrade import upgrade_proofs\n"
        "\n"
        "repo_path, ots_command = sys.argv[1], sys.argv[2]\n"
        "config = Config(\n"
        "    policy=PolicyConfig(\n"
        "        every_commit=False, max_age=timedelta(hours=24), fixed_time=None,\n"
        "        timezone=None, initial_history='latest',\n"
        "    ),\n"
        "    git=GitConfig(\n"
        "        source_ref='HEAD', fetch_before_run=False, tag_prefix='ots/',\n"
        "        require_clean_worktree=False,\n"
        "    ),\n"
        "    proof=ProofConfig(commit=False),\n"
        "    opentimestamps=OpenTimestampsConfig(command=ots_command),\n"
        "    limits=LimitsConfig(\n"
        "        ots_timeout=timedelta(seconds=1), git_timeout=timedelta(seconds=60),\n"
        "    ),\n"
        ")\n"
        "try:\n"
        "    upgrade_proofs(cwd=repo_path, config=config)\n"
        "except SubmissionTimeoutError:\n"
        "    sys.exit(0)\n"
        "else:\n"
        "    sys.exit(3)\n",
        encoding="utf-8",
    )

    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root / "src")

    started = time_module.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, str(driver_script), str(repo), str(fake_ots)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            "upgrade_proofs did not raise SubmissionTimeoutError within 10s -- "
            "the configured 1s ots_timeout did not reach the client (an "
            "unwired limit= defaults to None, unbounded, so this hangs "
            "rather than merely running past the 120s built-in default)"
        ) from exc
    elapsed = time_module.monotonic() - started

    assert completed.returncode == 0, (
        f"driver exited {completed.returncode} unexpectedly (3 == "
        f"upgrade_proofs returned normally instead of raising "
        f"SubmissionTimeoutError); stdout={completed.stdout!r} "
        f"stderr={completed.stderr!r}"
    )
    assert elapsed < 10.0, "did not fire near the configured 1s boundary"


def _run_config(
    *,
    git_timeout: timedelta | None,
    ots_timeout: timedelta | None = timedelta(seconds=120),
) -> Config:
    """A minimal, proof-committing Config with `limits` set explicitly.

    Passing `limits=` explicitly (rather than relying on the field default)
    matters here specifically: `Config.limits`'s default equals the
    documented defaults, so an omitted kwarg would silently supply 120s/60s
    and any test intending a distinguishable or unbounded value would pass
    for the wrong reason.
    """
    return Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(command="ots"),
        limits=LimitsConfig(ots_timeout=ots_timeout, git_timeout=git_timeout),
    )


def test_run_supplies_configured_git_timeout_to_create_generated_proof_commit(
    tmp_path: Path,
) -> None:
    """AC-UX-5: `limits.git_timeout` must reach the six `run()` git call sites
    that previously always fell back to the 60s built-in default -- most
    importantly `create_generated_proof_commit`'s `git commit`, which runs
    without `--no-verify` and so executes the repository's own
    pre-commit/commit-msg hooks (arbitrary operator code, not merely a fast
    local operation).

    A configured ceiling (1s, the smallest expressible value) distinguishable
    from the built-in default (60s) is paired with a `pre-commit` hook that
    only sleeps, so the timeout is asserted to fire well under 60s. An
    implementation that builds `run()`'s process_runner but forgets to pass
    it into `create_generated_proof_commit` specifically would instead hang
    for the whole 60s built-in default.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    now = commit_date + timedelta(hours=2)

    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    pre_commit_hook = hooks_dir / "pre-commit"
    pre_commit_hook.write_text("#!/bin/sh\nsleep 600\n", encoding="utf-8")
    pre_commit_hook.chmod(0o755)

    config = _run_config(git_timeout=timedelta(seconds=1))
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    def _submit(commit_id: str, payload: bytes) -> bytes:
        return _make_fake_detached_proof(payload=payload)

    started = time_module.monotonic()
    with pytest.raises(GitCommandTimeoutError):
        run_orchestration(snapshot=snapshot, now=now, submit=_submit)
    elapsed = time_module.monotonic() - started

    assert elapsed < 30.0, "did not fire near the configured 1s boundary"
    assert (repo / ".opentimestamps" / f"{commit_id}.ots").exists()


def test_timeout_during_tag_creation_leaves_a_section_22_recoverable_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A timeout during the mutation path's tag step -- not just submit --
    must still land in a state spec.md section 22 crash recovery reconciles.

    AC-STATE-1/2/3 only assert state cleanliness for a timeout during
    *submit*; the deliberately-chosen vehicle here is `create_timestamp_tag`
    rather than `create_generated_proof_commit`: `git tag -a` runs no hooks,
    unlike a killed `git commit`, which is now a documented wedge (AC-ERR-6)
    rather than a recoverable state and deliberately not the vehicle for this
    test. `proof.commit = False` keeps the generated-proof-commit step out of
    the picture entirely, isolating the tag-timeout case.

    Note what this test does NOT show: `git tag -a` does take its own lock
    (`.git/refs/tags/<name>.lock`), and the double below raises before any
    real `git tag` process is even spawned, so nothing here demonstrates that
    a genuinely killed `git tag` leaves no lock behind -- that would need a
    real subprocess kill, the way AC-ERR-6's fixture uses one for `commit`.
    The reason recovery is genuinely safe here is different and narrower:
    timestamp tag names are unique per run (`<tag_prefix><UTC-timestamp>/
    <short-sha>`, generated fresh each time `create_timestamp_tag` is
    called), so even a real stale ref lock from a killed attempt could not
    block the *next* run's differently-named tag the way a stale
    `.git/index.lock` blocks every later `git add`/`git commit` regardless of
    what they touch. Getting this reasoning right matters beyond this one
    test -- this epic has already been bitten twice by an inherited-but-false
    constraint copied from test to test.

    Real git hooks cannot slow down `git tag` (it has no hook of its own),
    so the timeout is simulated with an injected process_runner double that
    sleeps past the deadline and raises `GitCommandTimeoutError` itself --
    the same vehicle shape S3's tests use for `GitRunner`, which has no
    `_invoke`-style wrapper of its own (architecture.md's ceiling contract).
    This double also makes the test an (accidental but worth preserving)
    AC-UX-5 guard: it patches `git_ots.orchestration.make_process_runner`,
    the exact factory call `run()` must make from `snapshot.config.limits
    .git_timeout` -- if `run()` ever stopped building its own runner there,
    this spy would never fire, `create_timestamp_tag` would use the
    unpatched built-in default, and the `pytest.raises(...)` below would fail
    rather than silently passing. Do not retarget this patch to a lower-level
    seam without preserving that property.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    now = commit_date + timedelta(hours=2)

    should_delay_tag = {"value": True}

    def _spy(*, timeout):
        real_runner = make_process_runner(timeout=timeout)

        def _wrapped(argv, *, cwd, stdin=None):
            if should_delay_tag["value"] and len(argv) > 1 and argv[1] == "tag":
                time_module.sleep(max(timeout or 0.0, 0.0) + 0.2)
                raise GitCommandTimeoutError(argv=argv, timeout=timeout or 0.0)
            return real_runner(argv, cwd=cwd, stdin=stdin)

        return _wrapped

    monkeypatch.setattr("git_ots.orchestration.make_process_runner", _spy)

    config = _run_config(git_timeout=timedelta(seconds=1))
    config = Config(
        policy=config.policy,
        git=config.git,
        proof=ProofConfig(commit=False),
        opentimestamps=config.opentimestamps,
        limits=config.limits,
    )

    first_submissions: list[str] = []

    def _submit_first(commit_id: str, payload: bytes) -> bytes:
        first_submissions.append(commit_id)
        return _make_fake_detached_proof(payload=payload)

    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    # create_timestamp_tag catches GitCommandError (which GitCommandTimeoutError
    # subclasses) around its own `git tag -a` call and translates it into
    # InvalidRepositoryStateError -- pre-existing behaviour, unrelated to this
    # story's wiring; the underlying cause is still asserted via __cause__.
    with pytest.raises(InvalidRepositoryStateError) as excinfo:
        run_orchestration(snapshot=snapshot, now=now, submit=_submit_first)
    assert isinstance(excinfo.value.__cause__, GitCommandTimeoutError)

    assert first_submissions == [commit_id]

    proof_dir = repo / ".opentimestamps"
    assert (proof_dir / f"{commit_id}.ots").exists()
    assert (proof_dir / f"{commit_id}.json").exists()
    assert _git(["tag", "-l"], cwd=repo).strip() == ""

    # Recovery step: driven exactly as a scheduler's next tick would, with
    # the same configured runner (now behaving normally for `tag`).
    should_delay_tag["value"] = False

    second_submissions: list[str] = []

    def _submit_second(commit_id: str, payload: bytes) -> bytes:
        second_submissions.append(commit_id)
        return _make_fake_detached_proof(payload=payload)

    snapshot2 = build_snapshot(cwd=repo, config=config, now=now)
    run_orchestration(snapshot=snapshot2, now=now, submit=_submit_second)

    assert second_submissions == [], (
        "recovery must reuse the already-persisted proof, not resubmit"
    )
    tags = [t for t in _git(["tag", "-l", "ots/*"], cwd=repo).strip().splitlines() if t]
    assert len(tags) == 1
    assert _git(["rev-parse", f"{tags[0]}^{{commit}}"], cwd=repo).strip() == commit_id


def test_run_upstream_resolution_ignores_unpushed_local_commit(tmp_path: Path) -> None:
    """With source_ref @{upstream}, unpushed local commits are not timestamped.

    The upstream ref resolves to the remote-tracking state, so a local-only
    commit B remains ineligible and no submission occurs.
    """
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(["init", "-q", "--bare", str(remote)], cwd=tmp_path)

    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    a_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    _git(["remote", "add", "origin", str(remote)], cwd=repo)
    _git(["push", "-u", "origin", "main"], cwd=repo)

    # Establish A as the timestamped baseline so only new local work is pending.
    create_timestamp_tag(
        cwd=repo,
        source_commit_id=a_id,
        tag_prefix="ots/",
        submitted_at=commit_date + timedelta(minutes=1),
        proof=f".opentimestamps/{a_id}.ots",
        triggers=frozenset({"max_age"}),
    )

    # Local commit B is not pushed.
    b_id = _commit(
        repo,
        paths=["b.txt"],
        message="B\n",
        date=commit_date + timedelta(hours=1),
    )

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
            fixed_time=None,
            timezone=None,
            initial_history="latest",
        ),
        git=GitConfig(
            source_ref="@{upstream}",
            fetch_before_run=False,
            tag_prefix="ots/",
            require_clean_worktree=False,
        ),
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )
    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    # Source should be A, the upstream state, not the local HEAD B.
    assert snapshot.source.commit_id == a_id
    assert snapshot.pending == ()
    assert not snapshot.decision.commits

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(snapshot=snapshot, now=now, submit=submit)

    assert submissions == []
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == b_id


def test_run_dirty_worktree_fails_before_submission_when_proof_commit_enabled(
    tmp_path: Path,
) -> None:
    """A dirty worktree blocks the run before any OpenTimestamps submission.

    require_clean_worktree is opt-in -- it defaults to false -- and this test
    sets it, because what is under test is that the gate, once asked for, is
    enforced inside the run orchestration before side effects.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _ = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    # Dirty the worktree with an unrelated tracked change.
    (repo / "a.txt").write_text("modified\n")

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
            fixed_time=None,
            timezone=None,
            initial_history="latest",
        ),
        git=GitConfig(
            source_ref="HEAD",
            fetch_before_run=False,
            tag_prefix="ots/",
            require_clean_worktree=True,
        ),
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )
    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    assert snapshot.decision.commits

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    with pytest.raises(InvalidRepositoryStateError):
        run_orchestration(snapshot=snapshot, now=now, submit=submit)

    assert submissions == []
    assert _git(["tag", "-l", "ots/*"], cwd=repo).strip() == ""


def test_run_does_not_create_proof_commit_when_proof_commit_disabled(
    tmp_path: Path,
) -> None:
    """With proof.commit disabled, timestamping creates a tag but no generated commit."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=False),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )
    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    def submit(commit: str, payload: bytes) -> bytes:
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(snapshot=snapshot, now=now, submit=submit)

    # Tag exists and points at source; HEAD did not advance.
    tag_names = [
        n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n
    ]
    assert len(tag_names) == 1
    assert (
        _git(["rev-parse", f"{tag_names[0]}^{{commit}}"], cwd=repo).strip() == source_id
    )
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == source_id

    # Proof files were still persisted on disk.
    _assert_proof_binds_to_commit(
        (repo / f".opentimestamps/{source_id}.ots").read_bytes(), source_id
    )
    assert (repo / f".opentimestamps/{source_id}.json").exists()


def test_open_timestamps_failure_is_atomic(tmp_path: Path) -> None:
    """A failing OpenTimestamps submission leaves no proof, tag, or commit.

    The fake command exits non-zero and writes no proof. The CLI must map this
    to exit code 4 and must not create any final artifacts, timestamp tags, or
    generated proof commits.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    work = tmp_path / "work"
    work.mkdir()
    fake_ots = work / "fake_ots"
    fake_ots.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stderr.write('simulated network failure\n')\n"
        "sys.exit(1)\n"
    )
    fake_ots.chmod(0o755)

    _set_config(
        repo,
        max_age="1h",
        fetch_before_run=False,
        commit=True,
        command=str(fake_ots),
    )

    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root / "src")

    result = subprocess.run(
        [sys.executable, "-m", "git_ots", "run"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 4, result.stderr
    assert "simulated network failure" in result.stderr
    assert _git(["tag", "-l", "ots/*"], cwd=repo).strip() == ""
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == source_id
    assert not (repo / ".opentimestamps" / f"{source_id}.ots").exists()
    assert not (repo / ".opentimestamps" / f"{source_id}.json").exists()


def test_persistence_failure_leaves_recoverable_state(tmp_path: Path) -> None:
    """A persistence failure after writing proof/manifest leaves recoverable state.

    The simulated persist callback writes the real proof and manifest files and
    then raises. The orchestration must not create a tag or generated commit, but
    the recovery artifacts must remain so a subsequent run can finish the
    transaction (spec section 22.1).
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )
    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    def persist(commit: str, proof: bytes) -> None:
        # Write the real recovery artifacts, then simulate a post-write failure.
        repository_root = snapshot.layout.worktree_root
        proof_directory = Path(PROOF_DIRECTORY)
        object_format = snapshot.object_format
        persist_proof(
            repository_root=repository_root,
            proof_directory=proof_directory,
            object_format=object_format,
            commit_id=commit,
            proof_bytes=proof,
        )
        proof_name = f"{commit}.ots"
        manifest = build_manifest(
            object_format=object_format,
            commit_id=commit,
            proof_name=proof_name,
            source_ref=snapshot.source.display_ref,
            submitted_at=now,
            triggers=snapshot.decision.triggers,
        )
        persist_manifest(
            repository_root=repository_root,
            proof_directory=proof_directory,
            manifest=manifest,
        )
        raise PersistenceError("simulated persistence failure")

    with pytest.raises(PersistenceError):
        run_orchestration(
            snapshot=snapshot,
            now=now,
            submit=submit,
            persist=persist,
        )

    # Submission occurred, but tagging/committing did not.
    assert len(submissions) == 1
    assert submissions[0][0] == source_id
    assert _git(["tag", "-l", "ots/*"], cwd=repo).strip() == ""
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == source_id

    # Recoverable state exists: proof + manifest, no tag.
    _assert_proof_binds_to_commit(
        (repo / ".opentimestamps" / f"{source_id}.ots").read_bytes(), source_id
    )
    assert (repo / ".opentimestamps" / f"{source_id}.json").exists()

    # A second run recovers without resubmission by creating the tag and commit.
    submissions.clear()
    snapshot2 = build_snapshot(cwd=repo, config=config, now=now)
    run_orchestration(
        snapshot=snapshot2,
        now=now,
        submit=submit,
    )
    assert submissions == []

    tag_names = [
        n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n
    ]
    assert len(tag_names) == 1
    assert (
        _git(["rev-parse", f"{tag_names[0]}^{{commit}}"], cwd=repo).strip() == source_id
    )

    head_id = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    assert head_id != source_id
    assert _git(["rev-parse", f"{head_id}~1"], cwd=repo).strip() == source_id


def test_run_source_ref_race_uses_frozen_source(tmp_path: Path) -> None:
    """Advancing the configured ref after snapshot creation does not alter the target.

    The source ref is frozen during snapshot creation. Mutation steps must not
    re-resolve it, so the payload and timestamp tag still identify the
    original source commit even if HEAD advances during submission.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    a_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )
    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        # Advance HEAD before returning, simulating a concurrent push or local
        # commit that moves the configured ref after the snapshot was captured.
        _commit(repo, paths=["b.txt"], message="B\n", date=now)
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(
        snapshot=snapshot,
        now=now,
        submit=submit,
    )

    # The submission used the frozen source, not the advanced ref.
    assert len(submissions) == 1
    assert submissions[0][0] == a_id
    assert submissions[0][1] == build_payload(snapshot.object_format, a_id)

    # The timestamp tag points at the originally frozen source.
    tag_names = [
        n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n
    ]
    assert len(tag_names) == 1
    assert _git(["rev-parse", f"{tag_names[0]}^{{commit}}"], cwd=repo).strip() == a_id

    # HEAD advanced to B during submission, then the generated proof commit
    # advanced it further. The original source remains reachable as the
    # grandparent of the new HEAD.
    head_id = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    assert _git(["rev-parse", f"{head_id}~2"], cwd=repo).strip() == a_id


def _run_orchestration_in_process(
    repo: Path,
    config: Config,
    now: datetime,
    submit_blocks: bool,
    result_queue: Any,
    lock_held: mp.Event | None,
    release: mp.Event | None,
) -> None:
    """Run the public mutation entry point in a spawned process.

    Reports outcome via ``result_queue`` so the parent can distinguish a
    successful run from a lock failure without relying on exit codes alone.
    """
    try:
        snapshot = build_snapshot(cwd=repo, config=config, now=now)

        def submit(commit: str, payload: bytes) -> bytes:
            if submit_blocks and lock_held is not None:
                lock_held.set()
            if release is not None:
                release.wait()
            return _make_fake_detached_proof(payload=payload)

        run_orchestration(snapshot=snapshot, now=now, submit=submit)
        result_queue.put(("ok", None))
    except Exception as exc:  # noqa: BLE001
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def test_run_orchestration_direct_call_respects_advisory_lock(tmp_path: Path) -> None:
    """Direct calls to the public mutation entry point cannot bypass the lock.

    When one process holds the advisory lock inside ``run()``, a second process
    calling ``run()`` directly must fail with ``RepositoryLockedError`` rather
    than performing mutations. The first process must eventually complete the
    normal timestamping flow after the lock is released.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    now = commit_date + timedelta(hours=2)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(command="ots"),
    )

    def _wait_for(predicate, timeout: float = 5.0) -> None:
        deadline = time_module.monotonic() + timeout
        while not predicate():
            if time_module.monotonic() > deadline:
                raise AssertionError(
                    "timeout waiting for first process to acquire lock"
                )
            time_module.sleep(0.05)

    ctx = mp.get_context("spawn")
    lock_held = ctx.Event()
    release = ctx.Event()

    first_queue: mp.Queue = ctx.Queue()
    first = ctx.Process(
        target=_run_orchestration_in_process,
        args=(repo, config, now, True, first_queue, lock_held, release),
    )
    first.start()

    second_queue: mp.Queue = ctx.Queue()
    try:
        _wait_for(lock_held.is_set)

        second = ctx.Process(
            target=_run_orchestration_in_process,
            args=(repo, config, now, False, second_queue, None, None),
        )
        second.start()
        second.join(timeout=5)
        assert not second.is_alive(), "second process should have stopped at lock"
        assert second.exitcode == 0, "second process catches exception and reports it"

        status, detail = second_queue.get(timeout=1)
        assert status == "error", f"unexpected second process outcome: {detail}"
        assert "RepositoryLockedError" in detail, detail
    finally:
        release.set()
        first.join(timeout=5)
        if first.is_alive():
            first.terminate()
            first.join(timeout=5)

    assert first.exitcode == 0
    status, detail = first_queue.get(timeout=1)
    assert status == "ok", f"first process failed: {detail}"

    # The first process completed the full mutation; the second created nothing.
    tag_names = [
        n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n
    ]
    assert len(tag_names) == 1
    assert (
        _git(["rev-parse", f"{tag_names[0]}^{{commit}}"], cwd=repo).strip() == source_id
    )


@pytest.mark.parametrize(
    "operation_marker",
    [
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "rebase-merge/",
        "rebase-apply/",
    ],
)
def test_run_committable_state_rejects_in_progress_git_operation(
    tmp_path: Path,
    operation_marker: str,
) -> None:
    """A run fails before any submission when a Git operation is in progress.

    The precondition is checked at the orchestration mutation boundary, before
    any OpenTimestamps submission, tag, or commit. It uses per-worktree state so
    linked worktrees are handled correctly.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _ = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    if operation_marker == "MERGE_HEAD":
        _git(["checkout", "-b", "feature"], cwd=repo)
        _commit(repo, paths=["feature.txt"], message="Feature\n")
        _git(["checkout", "main"], cwd=repo)
        result = subprocess.run(
            ["git", "merge", "--no-commit", "--no-ff", "feature"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    else:
        # CHERRY_PICK_HEAD, rebase-merge/, and rebase-apply/ are detected by
        # the existence of per-worktree state files/directories. Create them at
        # the path Git reports so the precondition sees them.
        marker_path = _git(
            ["rev-parse", "--git-path", operation_marker], cwd=repo
        ).strip()
        resolved = (repo / marker_path).resolve()
        if operation_marker == "CHERRY_PICK_HEAD":
            resolved.write_text("dummy\n")
        else:
            resolved.mkdir(parents=True, exist_ok=True)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )
    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    with pytest.raises(InvalidRepositoryStateError):
        run_orchestration(snapshot=snapshot, now=now, submit=submit)

    assert submissions == []
    assert _git(["tag", "-l", "ots/*"], cwd=repo).strip() == ""


def test_run_committable_state_rejects_detached_head_when_proof_commit_enabled(
    tmp_path: Path,
) -> None:
    """A detached HEAD blocks mutation when proof.commit is true.

    Without a branch to attach to, the generated proof commit would be orphaned
    as soon as HEAD moves.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _ = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    _git(["checkout", "--detach"], cwd=repo)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )
    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    with pytest.raises(InvalidRepositoryStateError):
        run_orchestration(snapshot=snapshot, now=now, submit=submit)

    assert submissions == []
    assert _git(["tag", "-l", "ots/*"], cwd=repo).strip() == ""


def test_run_committable_state_allows_detached_head_when_proof_commit_disabled(
    tmp_path: Path,
) -> None:
    """A detached HEAD is harmless when proof.commit is false.

    CI checkouts are typically detached, and no proof commit is created, so the
    tag attaches directly to the source commit.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    _git(["checkout", "--detach"], cwd=repo)

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=False),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )
    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    def submit(commit: str, payload: bytes) -> bytes:
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(snapshot=snapshot, now=now, submit=submit)

    tag_names = [
        n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n
    ]
    assert len(tag_names) == 1
    assert (
        _git(["rev-parse", f"{tag_names[0]}^{{commit}}"], cwd=repo).strip() == source_id
    )
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == source_id


def test_baseline_derived_from_proof_commit_adr_d6(tmp_path: Path) -> None:
    """Baseline derivation uses proof commits with full ADR D6 validation.

    The idempotence marker moves from refs to commit content, so the validation
    rules (ancestry, binding, precedence, tree reads, determinism, batches) must
    all be enforced.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)

    base_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    a_id = _commit(repo, paths=["a.txt"], message="A\n", date=base_date)
    b_id = _commit(
        repo,
        paths=["b.txt"],
        message="B\n",
        date=base_date + timedelta(hours=1),
    )

    # Create a generated proof commit for A, but do not create the tag. This
    # simulates the post-D4 crash state "proof commit present, tag absent".
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    proof_path = proof_dir / f"{a_id}.ots"
    manifest_path = proof_dir / f"{a_id}.json"
    a_proof = _make_fake_detached_proof(payload=build_payload("sha1", a_id))
    proof_path.write_bytes(a_proof)
    a_manifest = build_manifest(
        object_format="sha1",
        commit_id=a_id,
        proof_name=f"{a_id}.ots",
        source_ref="refs/heads/main",
        submitted_at=base_date + timedelta(minutes=30),
        triggers=["max_age"],
    )
    manifest_path.write_text(json.dumps(a_manifest, indent=2) + "\n", encoding="utf-8")
    stage_paths(
        cwd=repo,
        paths=[
            f".opentimestamps/{a_id}.ots",
            f".opentimestamps/{a_id}.json",
        ],
    )
    create_generated_proof_commit(
        cwd=repo,
        source_commit_ids=[a_id],
        proof_directory=".opentimestamps",
        paths=[
            f".opentimestamps/{a_id}.ots",
            f".opentimestamps/{a_id}.json",
        ],
    )

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=24),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )

    # (a) The proof commit is the baseline, no submission is made, and the
    # missing tag is created from the manifest.
    #
    # B is pending: the manifest attests to A alone, and B is an ordinary user
    # commit made after it. This assertion previously expected an empty pending
    # set, which held only because the pending range started at the *proof
    # commit* -- a descendant of B -- so an unstamped commit sitting between the
    # stamped source and its proof commit was silently swallowed and could
    # never be timestamped. Item 114 starts the range at the stamped source
    # instead. The max-age policy is not due at +2h, so the decision is still
    # empty and the recovery path is exercised exactly as before.
    now = base_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    assert snapshot.baseline.baseline is not None
    assert snapshot.baseline.baseline.source_commit_id == a_id
    assert snapshot.baseline.baseline.proof_commit_id is not None
    assert [c.commit_id for c in snapshot.pending] == [b_id]
    assert not snapshot.decision.commits

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(snapshot=snapshot, now=now, submit=submit)
    assert submissions == []

    tag_names = [
        n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n
    ]
    assert len(tag_names) == 1
    assert _git(["rev-parse", f"{tag_names[0]}^{{commit}}"], cwd=repo).strip() == a_id

    expected_utc = (base_date + timedelta(minutes=30)).astimezone(UTC)
    expected_name = f"ots/{expected_utc.strftime('%Y%m%dT%H%M%SZ')}/{a_id[:12]}"
    assert tag_names[0] == expected_name

    # (b) A new meaningful commit on top of the proof commit is pending.
    c_id = _commit(
        repo,
        paths=["c.txt"],
        message="C\n",
        date=base_date + timedelta(hours=3),
    )
    # C is committed at +3h and max_age is 24h, so the policy only becomes due
    # once C itself reaches that age; evaluating at +4h would leave the decision
    # legitimately empty and assert nothing about the baseline.
    now_c = base_date + timedelta(hours=3) + timedelta(hours=24)
    snapshot_c = build_snapshot(cwd=repo, config=config, now=now_c)
    # B and C are both unstamped -- only A carries a proof -- so both are
    # pending, oldest first. The aggregate policy still targets the latest.
    assert [p.commit_id for p in snapshot_c.pending] == [b_id, c_id]
    assert snapshot_c.decision.commits
    assert snapshot_c.decision.commits[0].commit_id == c_id

    run_orchestration(
        snapshot=snapshot_c,
        now=now_c,
        submit=submit,
    )
    assert any(commit_id == c_id for commit_id, _ in submissions)
    assert (
        len(
            [n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n]
        )
        == 2
    )

    # (c) Repeated runs over an unchanged repository create no additional commits.
    head_before = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    submissions.clear()
    snapshot_repeat = build_snapshot(
        cwd=repo, config=config, now=now_c + timedelta(hours=1)
    )
    assert not snapshot_repeat.decision.commits
    run_orchestration(
        snapshot=snapshot_repeat, now=now_c + timedelta(hours=1), submit=submit
    )
    assert submissions == []
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == head_before


def test_baseline_rejects_rebased_and_cherry_picked_proof_commits_adr_d6(
    tmp_path: Path,
) -> None:
    """Proof commits transported by history operations are not trusted blindly.

    A rebased proof commit whose source is no longer an ancestor must be rejected
    and the run must fall through to tag search without crashing. A cherry-picked
    proof commit from an ahead branch must not import its newer baseline.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)

    base_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    a_id = _commit(repo, paths=["a.txt"], message="A\n", date=base_date)
    b_id = _commit(
        repo,
        paths=["b.txt"],
        message="B\n",
        date=base_date + timedelta(hours=1),
    )
    _commit(
        repo,
        paths=["c.txt"],
        message="C\n",
        date=base_date + timedelta(hours=2),
    )

    # Create a proof commit for B on the original lineage.
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    proof_path = proof_dir / f"{b_id}.ots"
    manifest_path = proof_dir / f"{b_id}.json"
    proof_path.write_bytes(
        _make_fake_detached_proof(payload=build_payload("sha1", b_id))
    )
    manifest = build_manifest(
        object_format="sha1",
        commit_id=b_id,
        proof_name=f"{b_id}.ots",
        source_ref="refs/heads/main",
        submitted_at=base_date + timedelta(minutes=90),
        triggers=["max_age"],
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    stage_paths(
        cwd=repo,
        paths=[
            f".opentimestamps/{b_id}.ots",
            f".opentimestamps/{b_id}.json",
        ],
    )
    create_generated_proof_commit(
        cwd=repo,
        source_commit_ids=[b_id],
        proof_directory=".opentimestamps",
        paths=[
            f".opentimestamps/{b_id}.ots",
            f".opentimestamps/{b_id}.json",
        ],
    )

    # Rebase the branch so A/B/C are rewritten; the proof commit's manifest
    # source (B) is no longer an ancestor of the new tip.
    #
    # The rewrite must be genuine: `git rebase -i --root` with no edits is a
    # no-op that preserves every hash, which would leave B an ancestor and make
    # this test assert nothing. Amend the root instead, so every descendant --
    # including the proof commit, which the rebase carries along with its
    # manifest intact -- is replayed onto a new lineage.
    # Build the new lineage explicitly rather than with `git rebase`. Two
    # reasons: `git rebase -i --root` with no edits is a no-op that preserves
    # every hash (leaving B an ancestor, so the test would assert nothing), and
    # a real rebase stamps every replayed commit with one wall-clock committer
    # date -- both far outside this test's fabricated timeline and identical to
    # each other, which makes "latest pending" ambiguous. Branching at A and
    # cherry-picking the proof commit models the same hazard exactly: a proof
    # commit transported onto a lineage its manifest says nothing about.
    proof_commit_id = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    _git(["checkout", "-q", "-b", "rewritten", a_id], cwd=repo)
    _commit(
        repo,
        paths=["b2.txt"],
        message="B rewritten\n",
        date=base_date + timedelta(hours=1),
    )
    rewritten_c = _commit(
        repo,
        paths=["c2.txt"],
        message="C rewritten\n",
        date=base_date + timedelta(hours=2),
    )
    subprocess.run(
        ["git", "cherry-pick", proof_commit_id],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_COMMITTER_DATE": (base_date + timedelta(hours=3)).strftime(
                "%Y-%m-%d %H:%M:%S %z"
            ),
        },
    )
    new_head = _git(["rev-parse", "HEAD"], cwd=repo).strip()

    # Guard the premise: the transported proof commit's manifest source (B) is
    # not an ancestor of the new lineage.
    assert (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", b_id, new_head],
            cwd=repo,
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )

    now = base_date + timedelta(hours=5)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    # The rebased proof commit must not be used as baseline: its source is not
    # an ancestor of the frozen source. There are no tags either, so baseline is
    # None and the full rewritten history is pending.
    assert snapshot.baseline.baseline is None
    assert snapshot.baseline.has_abandoned_tags is False
    pending_ids = {p.commit_id for p in snapshot.pending}

    # `new_head` is the *replayed proof commit* -- the rebase carried it along as
    # the tip. It still carries the generated trailer, so it is correctly
    # excluded from pending; the newest pending commit is the rewritten C
    # beneath it. Timestamping targets that, not the bookkeeping commit.
    assert _git(["rev-parse", f"{new_head}^"], cwd=repo).strip() == rewritten_c
    assert new_head not in pending_ids
    assert rewritten_c in pending_ids
    assert len(pending_ids) == 3
    assert snapshot.decision.commits
    assert snapshot.decision.commits[0].commit_id == rewritten_c

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(snapshot=snapshot, now=now, submit=submit)
    assert len(submissions) == 1
    assert submissions[0][0] == rewritten_c


def test_baseline_tag_wins_over_older_proof_commit_adr_d6(tmp_path: Path) -> None:
    """The newest baseline by submitted_at wins, whether it comes from a tag or a proof commit."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    base_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    a_id = _commit(repo, paths=["a.txt"], message="A\n", date=base_date)
    b_id = _commit(
        repo,
        paths=["b.txt"],
        message="B\n",
        date=base_date + timedelta(hours=1),
    )

    # Older proof commit for A.
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    proof_path = proof_dir / f"{a_id}.ots"
    manifest_path = proof_dir / f"{a_id}.json"
    proof_path.write_bytes(
        _make_fake_detached_proof(payload=build_payload("sha1", a_id))
    )
    manifest = build_manifest(
        object_format="sha1",
        commit_id=a_id,
        proof_name=f"{a_id}.ots",
        source_ref="refs/heads/main",
        submitted_at=base_date + timedelta(minutes=30),
        triggers=["max_age"],
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    stage_paths(
        cwd=repo,
        paths=[
            f".opentimestamps/{a_id}.ots",
            f".opentimestamps/{a_id}.json",
        ],
    )
    create_generated_proof_commit(
        cwd=repo,
        source_commit_ids=[a_id],
        proof_directory=".opentimestamps",
        paths=[
            f".opentimestamps/{a_id}.ots",
            f".opentimestamps/{a_id}.json",
        ],
    )

    # Newer tag-only stamp for B (proof.commit = false).
    create_timestamp_tag(
        cwd=repo,
        source_commit_id=b_id,
        tag_prefix="ots/",
        submitted_at=base_date + timedelta(hours=2),
        proof=f".opentimestamps/{b_id}.ots",
        triggers=frozenset({"max_age"}),
    )

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )

    now = base_date + timedelta(hours=3)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    # The newer tag baseline wins; B is the baseline and there are no pending commits.
    assert snapshot.baseline.baseline is not None
    assert snapshot.baseline.baseline.source_commit_id == b_id
    assert snapshot.baseline.baseline.tag_name.startswith("ots/")
    assert snapshot.pending == ()
    assert not snapshot.decision.commits

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(snapshot=snapshot, now=now, submit=submit)
    assert submissions == []
    assert (
        len(
            [n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n]
        )
        == 1
    )


def test_baseline_falls_through_when_proof_commit_manifest_is_unusable_adr_d6(
    tmp_path: Path,
) -> None:
    """A generated proof commit with an unusable manifest yields no baseline.

    If the manifest inside the proof commit tree is missing or the proof directory
    has been reconfigured so the manifest path no longer matches, baseline
    derivation must fall through rather than fail.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)

    base_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    a_id = _commit(repo, paths=["a.txt"], message="A\n", date=base_date)
    b_id = _commit(
        repo,
        paths=["b.txt"],
        message="B\n",
        date=base_date + timedelta(hours=1),
    )

    # Create a proof commit for A that contains only the proof file, no manifest.
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    proof_path = proof_dir / f"{a_id}.ots"
    proof_path.write_bytes(
        _make_fake_detached_proof(payload=build_payload("sha1", a_id))
    )
    stage_paths(
        cwd=repo,
        paths=[f".opentimestamps/{a_id}.ots"],
    )
    create_generated_proof_commit(
        cwd=repo,
        source_commit_ids=[a_id],
        proof_directory=".opentimestamps",
        paths=[f".opentimestamps/{a_id}.ots"],
    )

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )

    now = base_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    # No usable baseline from the manifest-less proof commit.
    assert snapshot.baseline.baseline is None
    assert snapshot.baseline.has_abandoned_tags is False
    assert len(snapshot.pending) == 2
    assert snapshot.decision.commits[0].commit_id == b_id

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(snapshot=snapshot, now=now, submit=submit)
    assert len(submissions) == 1
    assert submissions[0][0] == b_id


def test_run_unrelated_changes_not_captured_with_clean_worktree_disabled(
    tmp_path: Path,
) -> None:
    """With require_clean_worktree=false, unrelated changes are not captured.

    Item 101 made the explicit-pathspec guarantee structural, so this mode can
    complete without requiring a spotless worktree.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt", "b.txt"], message="A\n", date=commit_date)

    # Staged modification of tracked file.
    (repo / "a.txt").write_text("staged modified\n")
    _git(["add", "a.txt"], cwd=repo)

    # Modified tracked file (unstaged).
    (repo / "b.txt").write_text("modified\n")

    # Untracked file.
    (repo / "untracked.txt").write_text("untracked\n")

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
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
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )
    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    def submit(commit: str, payload: bytes) -> bytes:
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(snapshot=snapshot, now=now, submit=submit)

    # A source tag was created and HEAD advanced.
    tag_names = [
        n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n
    ]
    assert len(tag_names) == 1
    head_id = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    assert (
        _git(["rev-parse", f"{tag_names[0]}^{{commit}}"], cwd=repo).strip() == source_id
    )
    assert head_id != source_id

    # The generated proof commit contains only the two artifact paths.
    changed = _git(
        [
            "diff-tree",
            "--root",
            "-r",
            "--no-commit-id",
            "--no-renames",
            "--name-only",
            "-z",
            head_id,
        ],
        cwd=repo,
    ).split("\x00")
    changed = sorted(p for p in changed if p)
    assert changed == sorted(
        [f".opentimestamps/{source_id}.ots", f".opentimestamps/{source_id}.json"]
    )

    # Unrelated worktree state is preserved.
    status = _git(["status", "--porcelain"], cwd=repo)
    assert "M  a.txt" in status
    assert " b.txt" in status
    assert "?? untracked.txt" in status


def test_run_with_a_dirty_worktree_is_permitted_by_the_default_config(
    tmp_path: Path,
) -> None:
    """The default must not refuse a run over ordinary work in progress.

    This is the reason require_clean_worktree defaults to false rather than
    true. The gate never provided the exclusion it was introduced for -- the
    test above shows the explicit pathspec provides that whether the gate is
    set or not -- so all a defaulted-on gate could do was refuse. On the
    scheduled path that refusal is invisible: cron fires on a workday, the
    worktree has edits in it, and the timestamp is silently not taken.

    GitConfig is constructed without naming require_clean_worktree on purpose.
    Spelling it out would make this pass against either default and assert
    nothing.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    (repo / "a.txt").write_text("work in progress\n")
    (repo / "untracked.txt").write_text("also in progress\n")

    config = Config(
        policy=PolicyConfig(
            every_commit=False,
            max_age=timedelta(hours=1),
            fixed_time=None,
            timezone=None,
            initial_history="latest",
        ),
        git=GitConfig(fetch_before_run=False),
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )
    assert config.git.require_clean_worktree is False
    now = commit_date + timedelta(hours=2)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)

    submissions: list[str] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append(commit)
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(snapshot=snapshot, now=now, submit=submit)

    assert submissions == [source_id]
    tag_names = [
        n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n
    ]
    assert len(tag_names) == 1

    # The work in progress is still work in progress.
    status = _git(["status", "--porcelain"], cwd=repo)
    assert " M a.txt" in status
    assert "?? untracked.txt" in status


def test_build_snapshot_subprocess_count_is_independent_of_history_size(
    tmp_path: Path,
) -> None:
    """Baseline derivation and pending filtering must not walk history per commit.

    Item 106 initially classified every reachable commit with its own git
    subprocesses, making ``status`` and ``run`` cost O(history). Pin the fix:
    with no generated proof commits, the subprocess count of a snapshot is
    identical for a short and a much longer history.
    """

    def _snapshot_subprocess_count(commit_count: int, repo: Path) -> int:
        _init_repo(repo)
        base_date = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
        for i in range(commit_count):
            _commit(
                repo,
                paths=[f"f{i}.txt"],
                message=f"C{i}\n",
                date=base_date + timedelta(minutes=i),
            )
        calls = 0

        def counting_runner(argv, *, cwd, stdin=None):
            nonlocal calls
            calls += 1
            completed = subprocess.run(
                argv,
                cwd=cwd,
                input=stdin.decode("utf-8") if stdin is not None else None,
                capture_output=True,
                text=True,
                shell=False,
                check=False,
            )
            return completed.returncode, completed.stdout, completed.stderr

        snapshot = build_snapshot(
            cwd=repo,
            config=_fresh_config(),
            now=base_date + timedelta(hours=48),
            process_runner=counting_runner,
        )
        assert len(snapshot.pending) == commit_count
        return calls

    short = _snapshot_subprocess_count(3, tmp_path / "short")
    long = _snapshot_subprocess_count(40, tmp_path / "long")
    assert short == long


def test_batch_tags_use_each_commits_own_recovery_metadata(tmp_path: Path) -> None:
    """A mixed recovered/fresh batch must not cross-attribute recovery metadata.

    ``run()`` processes every selected commit, then tags them in a second loop.
    Recovery artifacts were held in one shared variable, so the tag loop saw
    whatever the *last* processed commit left behind: a recovered commit
    followed by a fresh one was tagged with ``submitted_at = now`` rather than
    its manifest's real submission time -- the attestation-time forgery plan
    ADR D5 exists to prevent.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)

    b_id = _commit(repo, paths=["b.txt"], message="B\n", date=base_date)

    # B already has a valid proof and manifest, uncommitted: no proof commit and
    # no tag exist, so B is still pending and will be recovered rather than
    # resubmitted. Its manifest records a submission well before this run.
    real_submitted_at = base_date + timedelta(minutes=30)
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    (proof_dir / f"{b_id}.ots").write_bytes(
        _make_fake_detached_proof(payload=build_payload("sha1", b_id))
    )
    manifest = build_manifest(
        object_format="sha1",
        commit_id=b_id,
        proof_name=f"{b_id}.ots",
        source_ref="refs/heads/main",
        submitted_at=real_submitted_at,
        triggers=["every_commit"],
    )
    (proof_dir / f"{b_id}.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    # A fresh commit after it, so the batch is [B recovered, C fresh] and C is
    # processed last.
    c_id = _commit(
        repo, paths=["c.txt"], message="C\n", date=base_date + timedelta(hours=1)
    )

    config = Config(
        policy=PolicyConfig(
            every_commit=True,
            max_age=None,
            fixed_time=None,
            timezone=None,
            initial_history="all",
        ),
        git=GitConfig(
            source_ref="HEAD",
            fetch_before_run=False,
            tag_prefix="ots/",
            require_clean_worktree=False,
        ),
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(command="ots"),
    )

    submissions: list[str] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append(commit)
        return _make_fake_detached_proof(payload=payload)

    now = base_date + timedelta(hours=6)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    assert [c.commit_id for c in snapshot.decision.commits] == [b_id, c_id]

    run_orchestration(snapshot=snapshot, now=now, submit=submit)

    # B was recovered, not resubmitted; C was submitted fresh.
    assert submissions == [c_id]

    tags = {
        _git(["rev-parse", f"{name}^{{commit}}"], cwd=repo).strip(): name
        for name in _git(["tag", "-l", "ots/*"], cwd=repo).split()
        if name
    }
    b_tag, c_tag = tags[b_id], tags[c_id]

    expected_b = real_submitted_at.strftime("%Y%m%dT%H%M%SZ")
    assert expected_b in b_tag, (
        f"B's tag {b_tag!r} must embed its manifest's submission time "
        f"{expected_b}, not the invocation time"
    )
    b_annotation = _git(["tag", "-l", "--format=%(contents)", b_tag], cwd=repo)
    assert (
        f"submitted-at: {real_submitted_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        in b_annotation
    )
    assert f"{b_id}.ots" in b_annotation, "B's tag must name B's own proof"

    assert now.strftime("%Y%m%dT%H%M%SZ") in c_tag
    c_annotation = _git(["tag", "-l", "--format=%(contents)", c_tag], cwd=repo)
    assert f"{c_id}.ots" in c_annotation


def test_batch_tags_are_correct_when_the_recovered_commit_is_last(
    tmp_path: Path,
) -> None:
    """Mirror of the mixed-batch case with the recovery at the end.

    With the shared variable, the fresh commit processed first was tagged using
    the *recovered* commit's submission time and proof path. Because the tag
    name embeds that time, this also collided with the genuine tag.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)

    # B is fresh and processed first; C already has artifacts and is recovered.
    b_id = _commit(repo, paths=["b.txt"], message="B\n", date=base_date)
    c_id = _commit(
        repo, paths=["c.txt"], message="C\n", date=base_date + timedelta(hours=1)
    )

    real_submitted_at = base_date + timedelta(minutes=45)
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    (proof_dir / f"{c_id}.ots").write_bytes(
        _make_fake_detached_proof(payload=build_payload("sha1", c_id))
    )
    manifest = build_manifest(
        object_format="sha1",
        commit_id=c_id,
        proof_name=f"{c_id}.ots",
        source_ref="refs/heads/main",
        submitted_at=real_submitted_at,
        triggers=["every_commit"],
    )
    (proof_dir / f"{c_id}.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    config = Config(
        policy=PolicyConfig(
            every_commit=True,
            max_age=None,
            fixed_time=None,
            timezone=None,
            initial_history="all",
        ),
        git=GitConfig(
            source_ref="HEAD",
            fetch_before_run=False,
            tag_prefix="ots/",
            require_clean_worktree=False,
        ),
        proof=ProofConfig(commit=True),
        opentimestamps=OpenTimestampsConfig(command="ots"),
    )

    submissions: list[str] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append(commit)
        return _make_fake_detached_proof(payload=payload)

    now = base_date + timedelta(hours=6)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    assert [c.commit_id for c in snapshot.decision.commits] == [b_id, c_id]

    run_orchestration(snapshot=snapshot, now=now, submit=submit)
    assert submissions == [b_id]

    tags = {
        _git(["rev-parse", f"{name}^{{commit}}"], cwd=repo).strip(): name
        for name in _git(["tag", "-l", "ots/*"], cwd=repo).split()
        if name
    }
    assert now.strftime("%Y%m%dT%H%M%SZ") in tags[b_id], (
        "the fresh commit must be tagged with the invocation time, not the "
        "recovered commit's submission time"
    )
    assert f"{b_id}.ots" in _git(
        ["tag", "-l", "--format=%(contents)", tags[b_id]], cwd=repo
    )
    assert real_submitted_at.strftime("%Y%m%dT%H%M%SZ") in tags[c_id]


# ---------------------------------------------------------------------------
# `ots.proofDirectory`: the whole flow through the real CLI, not just the
# `run` half. A configured directory that only `run` honoured would produce
# proofs no later command could find, which is worse than not honouring it at
# all -- so `status`, `verify` and `upgrade` are driven over the same
# repository in the same test.
# ---------------------------------------------------------------------------

_CONFIGURED_PROOF_DIR = "audit/anchors"


def _write_fake_pending_ots_client(path: Path) -> None:
    """Write a fake `ots` that stamps with one un-anchored calendar.

    Un-anchored on purpose: it makes the proof an `upgrade` candidate and a
    `pending-attestation` for `verify`, so one run sets up every command the
    test then drives.
    """
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import hashlib, pathlib, sys\n"
        "input_path = pathlib.Path(sys.argv[2])\n"
        "digest = hashlib.sha256(input_path.read_bytes()).digest()\n"
        "proof = (\n"
        "    b'\\x00OpenTimestamps\\x00\\x00Proof\\x00\\xbf\\x89\\xe2\\xe8\\x84\\xe8\\x92\\x94'\n"
        "    + b'\\x01\\x08' + digest\n"
        "    + b'\\x00\\x83\\xdf\\xe3\\x0d\\x2e\\xf9\\x0c\\x8e\\x0bexample.com'\n"
        ")\n"
        "(input_path.parent / (input_path.name + '.ots')).write_bytes(proof)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_configured_proof_directory_is_honoured_by_every_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ots.proofDirectory` redirects proof I/O for run, status, verify and upgrade.

    A nested, non-dot-prefixed directory is used deliberately: it shares no
    prefix with the default, so a call site still reading `.opentimestamps`
    cannot pass by accident, and its two path segments would break any
    consumer that assumed a single component.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    now = commit_date + timedelta(hours=2)

    fake_ots = tmp_path / "fake_ots"
    _write_fake_pending_ots_client(fake_ots)

    _set_config(
        repo,
        max_age="1h",
        fetch_before_run=False,
        commit=True,
        command=str(fake_ots),
        directory=_CONFIGURED_PROOF_DIR,
    )

    assert main(argv=["run"], now=now, cwd=repo) == 0

    proof_dir = repo / _CONFIGURED_PROOF_DIR
    assert (proof_dir / f"{commit_id}.ots").is_file()
    assert (proof_dir / f"{commit_id}.json").is_file()
    assert not (repo / ".opentimestamps").exists(), (
        "the default directory must not be written alongside the configured one"
    )

    # The generated proof commit has to name the configured paths, or the
    # D6 idempotence marker points at files that do not exist.
    proof_commit_paths = _git(
        ["show", "--name-only", "--format=", "HEAD"], cwd=repo
    ).split()
    assert sorted(proof_commit_paths) == [
        f"{_CONFIGURED_PROOF_DIR}/{commit_id}.json",
        f"{_CONFIGURED_PROOF_DIR}/{commit_id}.ots",
    ]

    # The timestamp tag's `proof:` annotation is how a reader finds the proof
    # from the tag alone, so it must carry the configured directory too.
    tag_name = _git(["tag", "-l", "ots/*"], cwd=repo).split()[0]
    annotation = _git(["tag", "-l", "--format=%(contents)", tag_name], cwd=repo)
    assert f"proof: {_CONFIGURED_PROOF_DIR}/{commit_id}.ots" in annotation

    capsys.readouterr()
    assert main(argv=["validate"], cwd=repo, now=now) == 0
    validate_out = capsys.readouterr().out
    assert f"{_CONFIGURED_PROOF_DIR}/{commit_id}.ots" in validate_out
    assert "pending-attestation" in validate_out

    assert main(argv=["status"], cwd=repo, now=now) == 0
    status_out = capsys.readouterr().out
    assert "proofs: none" not in status_out, (
        "status scanned the default directory instead of the configured one"
    )

    assert main(argv=["upgrade", "--dry-run"], cwd=repo, now=now) == 0
    upgrade_out = capsys.readouterr().out
    assert f"{_CONFIGURED_PROOF_DIR}/{commit_id}.ots" in upgrade_out

    # A second run finds the baseline it just wrote and does nothing further.
    assert main(argv=["run"], now=now, cwd=repo) == 0
    assert len(_git(["tag", "-l", "ots/*"], cwd=repo).split()) == 1, (
        "the configured directory must be rediscovered, not re-stamped"
    )


def test_invalid_proof_directory_fails_with_the_configuration_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An escaping `ots.proofDirectory` is refused before anything is written.

    The failure has to be the ordinary invalid-configuration exit, not a
    filesystem error raised half-way through a run -- a value that reached
    the write path would already have created a directory outside the
    repository by the time it failed.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    _set_config(repo, max_age="1h", fetch_before_run=False, directory="../escape")

    exit_code = main(argv=["run"], now=commit_date + timedelta(hours=2), cwd=repo)

    assert exit_code == 2, "invalid configuration must exit 2"
    assert "proof directory" in capsys.readouterr().err
    assert not (tmp_path / "escape").exists()


def test_moving_the_proofs_alongside_the_config_preserves_continuity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The migration the README documents actually works.

    Changing `ots.proofDirectory` does not move the proofs already written.
    The documented remedy is to `git mv` them in the same change; this drives
    exactly that sequence and then removes the local tags -- which is what any
    consumer of the repository sees, since `git-ots` never pushes tags -- so
    the surviving baseline is the generated proof commit alone. It has to be
    rediscovered under the new directory rather than resubmitted.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    fake_ots = tmp_path / "fake_ots"
    _write_fake_pending_ots_client(fake_ots)
    _set_config(
        repo,
        max_age="1h",
        fetch_before_run=False,
        commit=True,
        command=str(fake_ots),
    )

    assert main(argv=["run"], now=commit_date + timedelta(hours=2), cwd=repo) == 0
    assert (repo / ".opentimestamps" / f"{commit_id}.ots").is_file()

    _set_config(repo, directory=_CONFIGURED_PROOF_DIR)
    (repo / _CONFIGURED_PROOF_DIR).parent.mkdir(parents=True, exist_ok=True)
    _git(["mv", ".opentimestamps", _CONFIGURED_PROOF_DIR], cwd=repo)
    _git(["commit", "-q", "-m", "Move proofs\n"], cwd=repo)

    for tag_name in _git(["tag", "-l", "ots/*"], cwd=repo).split():
        _git(["tag", "-d", tag_name], cwd=repo)

    capsys.readouterr()
    exit_code = main(argv=["run"], now=commit_date + timedelta(hours=6), cwd=repo)

    assert exit_code == 0, capsys.readouterr().err
    # Rediscovered, not resubmitted: the tag is recreated from the moved
    # manifest and the proof bytes are the ones the first run wrote.
    tags = _git(["tag", "-l", "ots/*"], cwd=repo).split()
    assert len(tags) == 1
    annotation = _git(["tag", "-l", "--format=%(contents)", tags[0]], cwd=repo)
    assert f"proof: {_CONFIGURED_PROOF_DIR}/{commit_id}.ots" in annotation
    assert not (repo / ".opentimestamps").exists()


def test_changing_the_directory_without_moving_the_proofs_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Skipping the `git mv` is reported, not silently re-timestamped.

    This is the counterpart to the migration test above and pins the sharp
    edge the README warns about: with the local tags gone -- the state every
    consumer of the repository is in -- a generated proof commit still claims
    the source, but its artifacts are not under the configured directory. Spec
    section 22.3 requires that be reported rather than resolved by stamping the
    same commit a second time, which would produce two proofs for one commit.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    fake_ots = tmp_path / "fake_ots"
    _write_fake_pending_ots_client(fake_ots)
    _set_config(
        repo,
        max_age="1h",
        fetch_before_run=False,
        commit=True,
        command=str(fake_ots),
    )

    assert main(argv=["run"], now=commit_date + timedelta(hours=2), cwd=repo) == 0

    _set_config(repo, directory=_CONFIGURED_PROOF_DIR)
    for tag_name in _git(["tag", "-l", "ots/*"], cwd=repo).split():
        _git(["tag", "-d", tag_name], cwd=repo)

    capsys.readouterr()
    exit_code = main(argv=["run"], now=commit_date + timedelta(hours=6), cwd=repo)
    captured = capsys.readouterr()

    assert exit_code == 3, "an inconsistent repository state must exit 3"
    assert "inconsistent repository state" in captured.err
    assert not _git(["tag", "-l", "ots/*"], cwd=repo).split(), (
        "the refusal must not have created a second timestamp for one commit"
    )
    # The original proof is untouched where it was left.
    assert (repo / ".opentimestamps" / f"{commit_id}.ots").is_file()
