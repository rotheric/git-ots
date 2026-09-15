"""Tests for deriving the timestamp baseline from generated proof commits.

ADR D6 moves the idempotence marker from refs to commit content. These tests
exercise the validation rules that make that trade safe: ancestry checks,
payload binding, tree reads, and precedence by submission time.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from git_ots.config import (
    PROOF_DIRECTORY,
    Config,
    GitConfig,
    OpenTimestampsConfig,
    PolicyConfig,
    ProofConfig,
)
from git_ots.git import (
    create_generated_proof_commit,
    create_timestamp_tag,
    stage_paths,
)
from git_ots.orchestration import build_snapshot
from git_ots.orchestration import run as run_orchestration
from git_ots.timestamp import (
    build_manifest,
    build_payload,
)
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
        proof=ProofConfig(
            commit=True,
        ),
        opentimestamps=OpenTimestampsConfig(
            command="ots",
        ),
    )


def _submit_fake(commit: str, payload: bytes) -> bytes:
    return _make_fake_detached_proof(payload=payload)


def test_proof_commit_without_tag_creates_missing_tag_and_reports_zero_pending(
    tmp_path: Path,
) -> None:
    """A proof commit whose tag was never written is recognized as the baseline."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = _fresh_config()
    now = commit_date + timedelta(hours=25)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    run_orchestration(snapshot=snapshot, now=now, submit=_submit_fake)

    head_after_first = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    tag_names = [
        n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n
    ]
    assert len(tag_names) == 1

    # Simulate the "proof commit present, tag absent" crash state.
    _git(["tag", "-d", tag_names[0]], cwd=repo)

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    snapshot2 = build_snapshot(cwd=repo, config=config, now=now)
    assert not snapshot2.decision.commits
    assert snapshot2.baseline.baseline is not None
    assert snapshot2.baseline.baseline.source_commit_id == source_id

    run_orchestration(snapshot=snapshot2, now=now, submit=submit)

    assert submissions == []
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == head_after_first
    tag_names = [
        n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n
    ]
    assert len(tag_names) == 1
    assert (
        _git(["rev-parse", f"{tag_names[0]}^{{commit}}"], cwd=repo).strip() == source_id
    )


def test_new_user_commit_on_top_of_proof_commit_is_pending(
    tmp_path: Path,
) -> None:
    """After a proof-commit baseline, new meaningful commits are pending."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = _fresh_config()
    now = commit_date + timedelta(hours=25)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    run_orchestration(snapshot=snapshot, now=now, submit=_submit_fake)

    later_id = _commit(
        repo,
        paths=["b.txt"],
        message="B\n",
        date=commit_date + timedelta(hours=26),
    )

    snapshot2 = build_snapshot(cwd=repo, config=config, now=now + timedelta(hours=26))
    assert snapshot2.baseline.baseline is not None
    assert snapshot2.baseline.baseline.source_commit_id == source_id
    assert len(snapshot2.pending) == 1
    assert snapshot2.pending[0].commit_id == later_id


def test_repeated_runs_over_unchanged_repository_create_no_additional_commits(
    tmp_path: Path,
) -> None:
    """Multiple runs over an already-stamped state are idempotent."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = _fresh_config()
    now = commit_date + timedelta(hours=25)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    run_orchestration(snapshot=snapshot, now=now, submit=_submit_fake)

    head_id = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    tag_count = len(
        [n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n]
    )

    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    for _ in range(3):
        snapshot = build_snapshot(cwd=repo, config=config, now=now)
        run_orchestration(snapshot=snapshot, now=now, submit=submit)

    assert submissions == []
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == head_id
    assert (
        len(
            [n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n]
        )
        == tag_count
    )


def test_rebased_proof_commit_with_non_ancestor_source_falls_through(
    tmp_path: Path,
) -> None:
    """A rebased proof commit whose source is not an ancestor is rejected."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    a_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    b_id = _commit(
        repo, paths=["b.txt"], message="B\n", date=commit_date + timedelta(hours=1)
    )

    config = _fresh_config()
    now = commit_date + timedelta(hours=25)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    run_orchestration(snapshot=snapshot, now=now, submit=_submit_fake)

    proof_commit_id = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    assert proof_commit_id != b_id

    # Start a new lineage from A and carry the proof commit onto it.
    _git(["checkout", "-b", "newlineage", a_id], cwd=repo)
    _git(["cherry-pick", "--no-commit", proof_commit_id], cwd=repo)
    _git(["commit", "-m", "Rebased proof commit\n"], cwd=repo)

    c_id = _commit(
        repo,
        paths=["c.txt"],
        message="C\n",
        date=commit_date + timedelta(hours=2),
    )

    # Source is now C. The rebased proof commit claims B, which is not an ancestor
    # of C. The run must not crash and must not use B as the baseline.
    snapshot2 = build_snapshot(cwd=repo, config=config, now=now + timedelta(hours=3))
    assert (
        snapshot2.baseline.baseline is None
        or snapshot2.baseline.baseline.source_commit_id != b_id
    )
    assert len(snapshot2.pending) >= 1
    pending_ids = {p.commit_id for p in snapshot2.pending}
    assert c_id in pending_ids


def test_cherry_picked_proof_commit_does_not_import_newer_baseline(
    tmp_path: Path,
) -> None:
    """A cherry-picked proof commit from an ahead branch is not trusted as a baseline."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    b_id = _commit(
        repo, paths=["b.txt"], message="B\n", date=commit_date + timedelta(hours=1)
    )

    config = _fresh_config()
    now = commit_date + timedelta(hours=25)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    run_orchestration(snapshot=snapshot, now=now, submit=_submit_fake)

    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() != b_id

    # Side branch from B with two additional user commits and a timestamp.
    _git(["checkout", "-b", "side", b_id], cwd=repo)
    c_id = _commit(
        repo,
        paths=["c.txt"],
        message="C\n",
        date=commit_date + timedelta(hours=2),
    )
    _commit(
        repo,
        paths=["d.txt"],
        message="D\n",
        date=commit_date + timedelta(hours=3),
    )
    snapshot_side = build_snapshot(
        cwd=repo, config=config, now=now + timedelta(hours=4)
    )
    run_orchestration(
        snapshot=snapshot_side, now=now + timedelta(hours=4), submit=_submit_fake
    )

    # Identify the proof commit on the side branch.
    side_log = _git(["log", "--format=%H", "side"], cwd=repo).strip().split("\n")
    side_proof_commit: str | None = None
    for commit_id in side_log:
        body = _git(["log", "-1", "--format=%B", commit_id], cwd=repo)
        if "OpenTimestamps-Generated: true\n" in body:
            side_proof_commit = commit_id
            break
    assert side_proof_commit is not None

    # Back on main: cherry-pick the user commit C and the side proof commit.
    _git(["checkout", "main"], cwd=repo)
    _git(["cherry-pick", "--no-commit", c_id], cwd=repo)
    _git(["commit", "-m", "Cherry-pick C\n"], cwd=repo)
    cherry_c_id = _git(["rev-parse", "HEAD"], cwd=repo).strip()

    _git(["cherry-pick", "--no-commit", side_proof_commit], cwd=repo)
    _git(["commit", "-m", "Cherry-pick proof commit\n"], cwd=repo)

    e_id = _commit(
        repo,
        paths=["e.txt"],
        message="E\n",
        date=commit_date + timedelta(hours=5),
    )

    # The baseline must remain B (from main's own tag), not D from the imported
    # proof commit. The imported proof commit's source D is not an ancestor of
    # the frozen source E, so it must be rejected. C' and E should be pending.
    snapshot_main = build_snapshot(
        cwd=repo, config=config, now=now + timedelta(hours=6)
    )
    assert snapshot_main.baseline.baseline is not None
    assert snapshot_main.baseline.baseline.source_commit_id == b_id
    pending_ids = {p.commit_id for p in snapshot_main.pending}
    assert cherry_c_id in pending_ids
    assert e_id in pending_ids


def test_tag_only_newer_stamp_wins_over_older_proof_commit(
    tmp_path: Path,
) -> None:
    """A newer tag-only baseline beats an older proof-commit baseline."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    a_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = _fresh_config()
    now = commit_date + timedelta(hours=25)
    # First run with proof commits enabled timestamps A.
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    run_orchestration(snapshot=snapshot, now=now, submit=_submit_fake)
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() != a_id

    # Add B on top of the generated proof commit.
    b_id = _commit(
        repo, paths=["b.txt"], message="B\n", date=commit_date + timedelta(hours=1)
    )

    # Second run with proof.commit disabled timestamps B (tag only).
    config_no_commit = Config(
        policy=config.policy,
        git=config.git,
        proof=ProofConfig(commit=False),
        opentimestamps=config.opentimestamps,
    )
    now2 = commit_date + timedelta(hours=26)
    snapshot2 = build_snapshot(cwd=repo, config=config_no_commit, now=now2)
    submissions: list[tuple[str, bytes]] = []

    def submit(commit: str, payload: bytes) -> bytes:
        submissions.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(snapshot=snapshot2, now=now2, submit=submit)
    assert submissions == [(b_id, build_payload("sha1", b_id))]

    # Switch back to proof.commit enabled. The baseline should be B (tag), not A
    # (older proof commit). No new submission, tag, or proof commit.
    now3 = commit_date + timedelta(hours=27)
    snapshot3 = build_snapshot(cwd=repo, config=config, now=now3)
    assert snapshot3.baseline.baseline is not None
    assert snapshot3.baseline.baseline.source_commit_id == b_id
    assert not snapshot3.decision.commits

    submissions2: list[tuple[str, bytes]] = []

    def submit2(commit: str, payload: bytes) -> bytes:
        submissions2.append((commit, payload))
        return _make_fake_detached_proof(payload=payload)

    run_orchestration(snapshot=snapshot3, now=now3, submit=submit2)
    assert submissions2 == []
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == b_id
    assert (
        len(
            [n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n]
        )
        == 2
    )


def test_proof_commit_with_invalid_tree_manifest_falls_through(
    tmp_path: Path,
) -> None:
    """A proof commit whose manifest cannot be validated yields no baseline."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    a_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    b_id = _commit(
        repo, paths=["b.txt"], message="B\n", date=commit_date + timedelta(hours=1)
    )

    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    proof_path = proof_dir / f"{b_id}.ots"
    manifest_path = proof_dir / f"{b_id}.json"
    proof_path.write_bytes(
        _make_fake_detached_proof(payload=build_payload("sha1", b_id))
    )
    manifest_path.write_text("{ this is not valid json\n", encoding="utf-8")
    stage_paths(
        cwd=repo,
        paths=[f".opentimestamps/{b_id}.ots", f".opentimestamps/{b_id}.json"],
    )
    create_generated_proof_commit(
        cwd=repo,
        source_commit_ids=[b_id],
        proof_directory=".opentimestamps",
        paths=[f".opentimestamps/{b_id}.ots", f".opentimestamps/{b_id}.json"],
    )

    config = _fresh_config()
    now = commit_date + timedelta(hours=25)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    assert snapshot.baseline.baseline is None
    pending_ids = {p.commit_id for p in snapshot.pending}
    assert a_id in pending_ids
    assert b_id in pending_ids


def test_unconfigured_proof_directory_is_the_documented_default(
    tmp_path: Path,
) -> None:
    """A ``Config`` that names no proof directory stores proofs in the default.

    ``ProofConfig.directory`` is configurable again (it was briefly fixed by
    FS-0015 behaviour 15), so this pins the *default* rather than the absence
    of a choice: an unconfigured repository must still put its proofs where a
    consumer who clones it will look, which is :data:`PROOF_DIRECTORY`.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = Config(
        policy=PolicyConfig(every_commit=True),
        git=GitConfig(fetch_before_run=False),
    )
    assert config.proof == ProofConfig(commit=True)
    assert config.proof.directory == PROOF_DIRECTORY

    now = commit_date + timedelta(hours=1)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    run_orchestration(snapshot=snapshot, now=now, submit=_submit_fake)

    assert (repo / PROOF_DIRECTORY / f"{source_id}.ots").exists()
    assert (repo / PROOF_DIRECTORY / f"{source_id}.json").exists()


def test_configured_proof_directory_redirects_proof_io(tmp_path: Path) -> None:
    """A configured ``proof.directory`` moves both artifacts and nothing else.

    The default directory must not merely be *also* written -- a consumer of
    a moved repository would then never notice the move, and the next run
    would see two baselines -- so its absence is asserted alongside the
    presence of the configured one.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    base = _fresh_config()
    config = Config(
        policy=PolicyConfig(every_commit=True),
        git=base.git,
        proof=ProofConfig(directory="proofs/nested", commit=True),
        opentimestamps=base.opentimestamps,
    )

    now = commit_date + timedelta(hours=1)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    run_orchestration(snapshot=snapshot, now=now, submit=_submit_fake)

    assert (repo / "proofs" / "nested" / f"{source_id}.ots").exists()
    assert (repo / "proofs" / "nested" / f"{source_id}.json").exists()
    assert not (repo / PROOF_DIRECTORY).exists()

    # The proof commit and the tag both have to name the configured path, or
    # the next run cannot rediscover the baseline it just created.
    snapshot2 = build_snapshot(cwd=repo, config=config, now=now)
    assert snapshot2.baseline.baseline is not None
    assert snapshot2.baseline.baseline.source_commit_id == source_id
    assert snapshot2.baseline.baseline.proof_commit_id is not None


def test_reconfigured_proof_directory_falls_through_for_old_proof_commit(
    tmp_path: Path,
) -> None:
    """When ``proof.directory`` changes, an old proof commit gives no baseline.

    Baseline derivation reads the manifest at the *configured* path, so a
    proof commit written under the previous directory no longer matches. The
    requirement is that this falls through quietly -- an operator who moves
    the directory gets a fresh timestamp, not a crash on the leftovers.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    config = _fresh_config()
    now = commit_date + timedelta(hours=25)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    run_orchestration(snapshot=snapshot, now=now, submit=_submit_fake)

    # Remove the tag so the only baseline candidate is the old proof commit.
    tag_names = [
        n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n
    ]
    assert len(tag_names) == 1
    _git(["tag", "-d", tag_names[0]], cwd=repo)

    config_new = Config(
        policy=config.policy,
        git=config.git,
        proof=ProofConfig(directory="proofs", commit=True),
        opentimestamps=config.opentimestamps,
    )
    snapshot2 = build_snapshot(cwd=repo, config=config_new, now=now)
    assert snapshot2.baseline.baseline is None


def test_non_ascii_proof_directory_still_yields_proof_commit_baseline(
    tmp_path: Path,
) -> None:
    """Baseline derivation must survive a proof directory git would C-quote.

    ``git ls-tree`` C-style-quotes paths containing non-ASCII bytes unless
    ``-z`` is used; a quoted path does not round-trip into
    ``git show <commit>:<path>``, which would silently disable D6 for such a
    configuration. ``validate_proof_directory`` rejects non-ASCII, so no
    operator reaches this through ``ots.proofDirectory`` -- but ``Config`` is
    constructed directly by every embedder and by this suite, so the quoting
    hazard is reachable and stays covered.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)

    base = _fresh_config()
    config = Config(
        policy=base.policy,
        git=base.git,
        proof=ProofConfig(directory=".öts-proofs", commit=True),
        opentimestamps=base.opentimestamps,
    )
    now = commit_date + timedelta(hours=25)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    run_orchestration(snapshot=snapshot, now=now, submit=_submit_fake)

    # Remove the tag so the only baseline candidate is the proof commit.
    tag_names = [
        n for n in _git(["tag", "-l", "ots/*"], cwd=repo).strip().split("\n") if n
    ]
    assert len(tag_names) == 1
    _git(["tag", "-d", tag_names[0]], cwd=repo)

    snapshot2 = build_snapshot(cwd=repo, config=config, now=now)
    assert snapshot2.baseline.baseline is not None
    assert snapshot2.baseline.baseline.source_commit_id == source_id
    assert snapshot2.baseline.baseline.proof_commit_id is not None


def _baseline_for_tag_prefix(
    repo: Path,
    *,
    tag_prefix: str,
    now: datetime,
) -> str:
    """Return the selected baseline's source commit for a given tag prefix."""
    base = _fresh_config()
    config = Config(
        policy=base.policy,
        git=GitConfig(
            source_ref="HEAD",
            fetch_before_run=False,
            tag_prefix=tag_prefix,
            require_clean_worktree=False,
        ),
        proof=base.proof,
        opentimestamps=base.opentimestamps,
    )
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    assert snapshot.baseline.baseline is not None
    return snapshot.baseline.baseline.source_commit_id


def test_submitted_at_tie_between_tag_and_proof_commit_is_decided_by_kind(
    tmp_path: Path,
) -> None:
    """A tag/proof-commit tie must not be decided by the operator's tag_prefix.

    Both candidate sorts order by ``(submitted_at, tag_name)``. Proof-commit
    candidates carry a synthesized ``proof-commit:<sha>`` name rather than a real
    tag, so a tie was resolved by string comparison against whatever
    ``tag_prefix`` the operator configured -- an arbitrary winner that flips
    between prefixes sorting before and after ``"proof-commit:"``.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    a_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    b_id = _commit(
        repo,
        paths=["b.txt"],
        message="B\n",
        date=commit_date + timedelta(hours=1),
    )

    # One instant shared by both candidates, which name different sources.
    tie = commit_date + timedelta(hours=2)

    # Proof-commit candidate: manifest claims A.
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()
    (proof_dir / f"{a_id}.ots").write_bytes(
        _make_fake_detached_proof(payload=build_payload("sha1", a_id))
    )
    manifest = build_manifest(
        object_format="sha1",
        commit_id=a_id,
        proof_name=f"{a_id}.ots",
        source_ref="refs/heads/main",
        submitted_at=tie,
        triggers=["max_age"],
    )
    (proof_dir / f"{a_id}.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    artifact_paths = [f".opentimestamps/{a_id}.ots", f".opentimestamps/{a_id}.json"]
    stage_paths(cwd=repo, paths=artifact_paths)
    create_generated_proof_commit(
        cwd=repo,
        source_commit_ids=[a_id],
        proof_directory=".opentimestamps",
        paths=artifact_paths,
    )

    now = tie + timedelta(hours=1)

    # Tag candidate on B at the same instant, created under each prefix in turn.
    # "a-ots/" sorts before "proof-commit:", "z-ots/" sorts after it.
    selected: dict[str, str] = {}
    for prefix in ("a-ots/", "z-ots/"):
        tag_name = create_timestamp_tag(
            cwd=repo,
            source_commit_id=b_id,
            tag_prefix=prefix,
            submitted_at=tie,
            proof=f"{b_id}.ots",
            triggers={"max_age"},
        )
        selected[prefix] = _baseline_for_tag_prefix(repo, tag_prefix=prefix, now=now)
        _git(["tag", "-d", tag_name], cwd=repo)

    # The winner must not depend on how tag_prefix happens to sort.
    assert selected["a-ots/"] == selected["z-ots/"]

    # The proof commit is the authoritative D6 marker and is validated for
    # ancestry and payload binding before becoming a candidate, so it wins.
    assert selected["a-ots/"] == a_id


def test_proof_commit_baseline_subprocess_count_scales_linearly_with_stamps(
    tmp_path: Path,
) -> None:
    """Baseline derivation must not be quadratic in the number of stamps.

    Manifests are read from a single tree -- the frozen source's -- and batched
    into a constant number of subprocesses, rather than rescanning every
    reachable proof commit's tree and validating every manifest in each.

    Do not "optimize" this by scanning only the newest generated proof commit's
    tree on the theory that artifacts accumulate: that holds on a linear history
    and is false across a merge, where each branch's proof commit carries only
    its own manifests. See
    test_baseline_sees_manifests_from_both_sides_of_a_merge.
    """

    def _stamp_repository(repo: Path, stamp_count: int) -> None:
        _init_repo(repo)
        commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
        config = _fresh_config()
        for i in range(stamp_count):
            _commit(
                repo,
                paths=[f"c{i}.txt"],
                message=f"C{i}\n",
                date=commit_date + timedelta(hours=i),
            )
            now = commit_date + timedelta(hours=i + 25)
            snapshot = build_snapshot(cwd=repo, config=config, now=now)
            run_orchestration(snapshot=snapshot, now=now, submit=_submit_fake)

    def _snapshot_subprocess_count(repo: Path) -> int:
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

        config = _fresh_config()
        now = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC) + timedelta(hours=100)
        snapshot = build_snapshot(
            cwd=repo,
            config=config,
            now=now,
            process_runner=counting_runner,
        )
        assert snapshot.baseline.baseline is not None
        return calls

    small_repo = tmp_path / "small"
    large_repo = tmp_path / "large"
    _stamp_repository(small_repo, 4)
    _stamp_repository(large_repo, 8)

    small_calls = _snapshot_subprocess_count(small_repo)
    large_calls = _snapshot_subprocess_count(large_repo)

    # The unoptimized implementation grows quadratically (4 stamps -> ~49
    # subprocesses, 8 -> ~135); the fix must keep the ratio near 1.
    assert large_calls <= small_calls * 2


def test_proof_commit_baseline_prefers_newer_submitted_at_over_nearer_commit(
    tmp_path: Path,
) -> None:
    """D6 rule 3: the newest submitted_at wins, not the nearest proof commit.

    The newest generated proof commit's tree contains every older manifest, so
    a deeper proof commit with a later ``submitted_at`` must still be selected
    as the baseline.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_date = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
    a_id = _commit(repo, paths=["a.txt"], message="A\n", date=commit_date)
    b_id = _commit(
        repo,
        paths=["b.txt"],
        message="B\n",
        date=commit_date + timedelta(hours=1),
    )

    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir()

    late = commit_date + timedelta(hours=10)
    early = commit_date + timedelta(hours=2)

    # Create the deeper proof commit for A with a late submitted_at.
    (proof_dir / f"{a_id}.ots").write_bytes(
        _make_fake_detached_proof(payload=build_payload("sha1", a_id))
    )
    manifest_a = build_manifest(
        object_format="sha1",
        commit_id=a_id,
        proof_name=f"{a_id}.ots",
        source_ref="refs/heads/main",
        submitted_at=late,
        triggers=["max_age"],
    )
    (proof_dir / f"{a_id}.json").write_text(
        json.dumps(manifest_a, indent=2) + "\n", encoding="utf-8"
    )
    stage_paths(
        cwd=repo,
        paths=[f".opentimestamps/{a_id}.ots", f".opentimestamps/{a_id}.json"],
    )
    p1_id = create_generated_proof_commit(
        cwd=repo,
        source_commit_ids=[a_id],
        proof_directory=".opentimestamps",
        paths=[f".opentimestamps/{a_id}.ots", f".opentimestamps/{a_id}.json"],
    )

    # Create the nearer proof commit for B with an early submitted_at.
    (proof_dir / f"{b_id}.ots").write_bytes(
        _make_fake_detached_proof(payload=build_payload("sha1", b_id))
    )
    manifest_b = build_manifest(
        object_format="sha1",
        commit_id=b_id,
        proof_name=f"{b_id}.ots",
        source_ref="refs/heads/main",
        submitted_at=early,
        triggers=["max_age"],
    )
    (proof_dir / f"{b_id}.json").write_text(
        json.dumps(manifest_b, indent=2) + "\n", encoding="utf-8"
    )
    stage_paths(
        cwd=repo,
        paths=[f".opentimestamps/{b_id}.ots", f".opentimestamps/{b_id}.json"],
    )
    p2_id = create_generated_proof_commit(
        cwd=repo,
        source_commit_ids=[b_id],
        proof_directory=".opentimestamps",
        paths=[f".opentimestamps/{b_id}.ots", f".opentimestamps/{b_id}.json"],
    )

    assert p1_id != p2_id

    config = _fresh_config()
    now = commit_date + timedelta(hours=25)
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    assert snapshot.baseline.baseline is not None
    # P_1's manifest has the later submitted_at even though P_1 is deeper. The
    # manifest is carried forward by P_2, so proof_commit_id is the newest
    # proof commit; the selected source is still A.
    assert snapshot.baseline.baseline.source_commit_id == a_id
    assert snapshot.baseline.baseline.proof_commit_id == p2_id


def _stamp(repo: Path, config: Config, now: datetime) -> None:
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    run_orchestration(snapshot=snapshot, now=now, submit=_submit_fake)


def _every_commit_config() -> Config:
    base = _fresh_config()
    return Config(
        policy=PolicyConfig(
            every_commit=True,
            max_age=None,
            fixed_time=None,
            timezone=None,
            initial_history="latest",
        ),
        git=base.git,
        proof=base.proof,
        opentimestamps=base.opentimestamps,
    )


def test_baseline_sees_manifests_from_both_sides_of_a_merge(tmp_path: Path) -> None:
    """Merged branches each carry only their own manifests.

    Item 112 scans one proof commit's tree, relying on artifacts accumulating.
    Across a merge that is false: the union of manifests exists only in the
    merge commit, which is not itself a generated proof commit. The tags are
    deleted before asserting because the tag search would otherwise find the
    newer stamp and mask the gap -- and a fresh clone has no tags, since
    ``git-ots`` never pushes them, which is the case ADR D6 exists to serve.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    config = _every_commit_config()
    base = datetime(2026, 1, 1, tzinfo=UTC)

    _commit(repo, paths=["a.txt"], message="A\n", date=base)
    _stamp(repo, config, base + timedelta(hours=1))

    _git(["checkout", "-q", "-b", "feature"], cwd=repo)
    b_id = _commit(repo, paths=["b.txt"], message="B\n", date=base + timedelta(hours=2))
    _stamp(repo, config, base + timedelta(hours=5))  # newest submitted_at

    _git(["checkout", "-q", "main"], cwd=repo)
    c_id = _commit(repo, paths=["c.txt"], message="C\n", date=base + timedelta(hours=3))
    _stamp(repo, config, base + timedelta(hours=4))  # older submitted_at

    _git(["merge", "--no-ff", "-q", "-m", "merge feature", "feature"], cwd=repo)

    # A clone receives the proof commits but not the tags.
    for tag in [t for t in _git(["tag", "-l", "ots/*"], cwd=repo).split() if t]:
        _git(["tag", "-d", tag], cwd=repo)

    snapshot = build_snapshot(cwd=repo, config=config, now=base + timedelta(hours=6))

    assert snapshot.baseline.baseline is not None
    assert snapshot.baseline.baseline.source_commit_id == b_id, (
        "the newest stamp lives on the merged-in branch; scanning only one "
        "proof commit's tree misses it and selects the older stamp instead"
    )
    assert snapshot.baseline.baseline.submitted_at == base + timedelta(hours=5)

    # B already carries a valid proof, so it must not be re-stamped. C and the
    # merge commit are not ancestors of B and legitimately remain pending.
    pending_ids = {p.commit_id for p in snapshot.pending}
    assert b_id not in pending_ids
    assert c_id in pending_ids


def test_merged_repository_baseline_stays_within_the_subprocess_bound(
    tmp_path: Path,
) -> None:
    """Reading a wider manifest set must not reintroduce per-proof-commit scanning."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    config = _every_commit_config()
    base = datetime(2026, 1, 1, tzinfo=UTC)

    _commit(repo, paths=["a.txt"], message="A\n", date=base)
    _stamp(repo, config, base + timedelta(hours=1))
    _git(["checkout", "-q", "-b", "feature"], cwd=repo)
    for i in range(4):
        _commit(
            repo,
            paths=[f"b{i}.txt"],
            message=f"B{i}\n",
            date=base + timedelta(hours=2, minutes=i),
        )
        _stamp(repo, config, base + timedelta(hours=3, minutes=i))
    _git(["checkout", "-q", "main"], cwd=repo)
    for i in range(4):
        _commit(
            repo,
            paths=[f"c{i}.txt"],
            message=f"C{i}\n",
            date=base + timedelta(hours=4, minutes=i),
        )
        _stamp(repo, config, base + timedelta(hours=5, minutes=i))
    _git(["merge", "--no-ff", "-q", "-m", "merge feature", "feature"], cwd=repo)

    calls: list[str] = []
    real_run = subprocess.run

    def counting_run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and args and args[0] == "git":
            calls.append(args[1] if len(args) > 1 else "?")
        return real_run(args, *a, **kw)

    subprocess.run = counting_run
    try:
        build_snapshot(cwd=repo, config=config, now=base + timedelta(hours=9))
    finally:
        subprocess.run = real_run

    # 9 stamps across two branches. A per-proof-commit rescan would issue
    # several subprocesses per proof commit per manifest; a single tree read
    # plus one batch stays far below that.
    assert calls.count("ls-tree") <= 2
    assert calls.count("cat-file") <= 2
    assert len(calls) < 60, f"{len(calls)} subprocesses: {sorted(set(calls))}"


def test_every_commit_batch_baseline_is_the_newest_claimed_source(
    tmp_path: Path,
) -> None:
    """A batch shares one submitted_at; the baseline must still be its newest source.

    ADR D6 rule 6: for a manifest set claiming several sources, the baseline is
    the newest claimed source. An ``every_commit`` batch is written by a single
    run, so every manifest carries an identical ``submitted_at`` and sorting by
    time alone leaves the winner to ``ls-tree`` order -- which is commit-SHA
    order, i.e. arbitrary. Picking wrongly leaves already-stamped commits
    pending, and they are then re-decided on an unchanged repository.

    The repository is rebuilt until the newest batch member is *not* the
    SHA-smallest, so the adverse ordering is exercised every run rather than
    one time in three.
    """
    config = _every_commit_config()
    base = datetime(2026, 1, 1, tzinfo=UTC)

    for attempt in range(12):
        repo = tmp_path / f"repo{attempt}"
        _init_repo(repo)
        _commit(repo, paths=["a.txt"], message="A\n", date=base)
        _stamp(repo, config, base + timedelta(hours=1))

        batch = [
            _commit(
                repo,
                paths=[f"{name}.txt"],
                message=f"{name}-{attempt}\n",
                date=base + timedelta(hours=2 + offset),
            )
            for offset, name in enumerate(("b", "c", "d"))
        ]
        newest = batch[-1]
        if newest == min(batch):
            continue  # lucky ordering would pass even unfixed; rebuild
        _stamp(repo, config, base + timedelta(hours=5))
        break
    else:  # pragma: no cover - 12 consecutive lucky orderings is implausible
        raise AssertionError("could not construct an adverse SHA ordering")

    snapshot = build_snapshot(cwd=repo, config=config, now=base + timedelta(hours=6))

    assert snapshot.baseline.baseline is not None
    assert snapshot.baseline.baseline.source_commit_id == newest, (
        "the batch baseline must be its newest claimed source, not whichever "
        "manifest git happened to list first"
    )
    assert snapshot.pending == (), (
        "every batch member carries a proof; none may be re-decided"
    )
    assert not snapshot.decision.commits
