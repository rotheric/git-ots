"""Tests for `git-ots repair` -- reconstructing tags from stored manifests.

`run` completes the bookkeeping for the stamps it is currently concerned with.
`repair` is the route back for a proof that fell outside that window, which is
the state an operator is left in when an interrupted run orphaned a proof some
time ago. The invariants that matter are all about *not* inventing anything:
the annotation is reconstructed from the manifest, the proof must bind to the
source the manifest names, an existing tag is never moved, and a proof whose
source was rewritten off the lineage is reported rather than tagged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from git_ots.git import create_timestamp_tag, enumerate_timestamp_tags
from git_ots.orchestration import build_snapshot
from git_ots.orchestration import run as run_orchestration
from git_ots.repair import RepairAction, repair_missing_tags
from git_ots.timestamp import build_manifest, build_payload
from tests.test_integration import _commit, _fresh_config, _git, _init_repo
from tests.test_timestamp import _make_fake_detached_proof

_T0 = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
_SUBMITTED_AT = datetime(2026, 8, 20, 7, 42, 24, tzinfo=UTC)


def _write_orphan_proof(
    repo: Path,
    commit_id: str,
    *,
    submitted_at: datetime = _SUBMITTED_AT,
    triggers: tuple[str, ...] = ("max_age",),
    manifest_overrides: dict | None = None,
) -> None:
    """Write a valid, untagged proof/manifest pair straight into the tree."""
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir(parents=True, exist_ok=True)
    (proof_dir / f"{commit_id}.ots").write_bytes(
        _make_fake_detached_proof(payload=build_payload("sha1", commit_id))
    )
    manifest = build_manifest(
        object_format="sha1",
        commit_id=commit_id,
        proof_name=f"{commit_id}.ots",
        source_ref="refs/heads/main",
        submitted_at=submitted_at,
        triggers=triggers,
    )
    manifest.update(manifest_overrides or {})
    (proof_dir / f"{commit_id}.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def _repo_with_orphan(tmp_path: Path) -> tuple[Path, object, str]:
    repo = tmp_path / "repo"
    _init_repo(repo)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=_T0)
    _write_orphan_proof(repo, source_id)
    return repo, _fresh_config(), source_id


def test_repair_recreates_the_missing_tag_from_the_manifest(tmp_path: Path) -> None:
    repo, config, source_id = _repo_with_orphan(tmp_path)

    results = repair_missing_tags(cwd=repo, config=config)

    assert [r.action for r in results] == [RepairAction.TAGGED]
    tag = next(iter(enumerate_timestamp_tags(cwd=repo, tag_prefix="ots/")))
    assert tag.commit_id == source_id
    annotation = _git(["cat-file", "tag", tag.name], cwd=repo)
    assert f"source: {source_id}" in annotation
    assert "submitted-at: 2026-08-20T07:42:24Z" in annotation
    assert "triggers: max_age" in annotation
    assert f"proof: .opentimestamps/{source_id}.ots" in annotation


def test_repair_uses_the_manifest_time_not_the_current_time(tmp_path: Path) -> None:
    """The tag name and annotation both encode the recorded submission.

    Reconstructing from "now" would assert a submission that never happened at
    that moment, which is the one thing a repair path must never do.
    """
    repo, config, source_id = _repo_with_orphan(tmp_path)

    repair_missing_tags(cwd=repo, config=config)

    tag = next(iter(enumerate_timestamp_tags(cwd=repo, tag_prefix="ots/")))
    assert tag.name == f"ots/20260820T074224Z/{source_id[:12]}"


def test_repair_is_idempotent_and_never_moves_an_existing_tag(
    tmp_path: Path,
) -> None:
    repo, config, _source_id = _repo_with_orphan(tmp_path)
    repair_missing_tags(cwd=repo, config=config)
    before = _git(
        ["for-each-ref", "refs/tags", "--format=%(refname) %(objectname)"], cwd=repo
    )

    results = repair_missing_tags(cwd=repo, config=config)

    assert [r.action for r in results] == [RepairAction.ALREADY_TAGGED]
    after = _git(
        ["for-each-ref", "refs/tags", "--format=%(refname) %(objectname)"], cwd=repo
    )
    assert after == before


def test_repair_leaves_a_differently_named_existing_tag_alone(
    tmp_path: Path,
) -> None:
    """A tag already naming the source wins, whatever its own timestamp says.

    Spec section 12 makes tags immutable to git-ots. Repair recognises the
    source, not the name it would itself have chosen.
    """
    repo, config, source_id = _repo_with_orphan(tmp_path)
    create_timestamp_tag(
        cwd=repo,
        source_commit_id=source_id,
        tag_prefix="ots/",
        submitted_at=_SUBMITTED_AT + timedelta(days=1),
        proof=f".opentimestamps/{source_id}.ots",
        triggers=frozenset({"fixed_time"}),
    )

    results = repair_missing_tags(cwd=repo, config=config)

    assert [r.action for r in results] == [RepairAction.ALREADY_TAGGED]
    assert len(list(enumerate_timestamp_tags(cwd=repo, tag_prefix="ots/"))) == 1


def test_repair_dry_run_creates_nothing(tmp_path: Path) -> None:
    repo, config, _source_id = _repo_with_orphan(tmp_path)

    results = repair_missing_tags(cwd=repo, config=config, dry_run=True)

    assert [r.action for r in results] == [RepairAction.WOULD_TAG]
    assert list(enumerate_timestamp_tags(cwd=repo, tag_prefix="ots/")) == []


def test_repair_refuses_a_proof_that_does_not_bind_to_its_source(
    tmp_path: Path,
) -> None:
    """A manifest that lies about its source fails cryptographically.

    The manifest is checked, not trusted (ADR 0001 D5): the proof's embedded
    digest must derive from the canonical payload for this exact commit.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=_T0)
    other_id = "0" * 39 + "1"
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir(parents=True, exist_ok=True)
    # A structurally valid proof, but bound to a different commit entirely.
    (proof_dir / f"{source_id}.ots").write_bytes(
        _make_fake_detached_proof(payload=build_payload("sha1", other_id))
    )
    (proof_dir / f"{source_id}.json").write_text(
        json.dumps(
            build_manifest(
                object_format="sha1",
                commit_id=source_id,
                proof_name=f"{source_id}.ots",
                source_ref="refs/heads/main",
                submitted_at=_SUBMITTED_AT,
                triggers=["max_age"],
            )
        )
        + "\n",
        encoding="utf-8",
    )

    results = repair_missing_tags(cwd=repo, config=_fresh_config())

    assert [r.action for r in results] == [RepairAction.UNREPAIRABLE]
    assert list(enumerate_timestamp_tags(cwd=repo, tag_prefix="ots/")) == []


def test_repair_reports_a_missing_manifest_without_inventing_one(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    source_id = _commit(repo, paths=["a.txt"], message="A\n", date=_T0)
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir(parents=True, exist_ok=True)
    (proof_dir / f"{source_id}.ots").write_bytes(
        _make_fake_detached_proof(payload=build_payload("sha1", source_id))
    )

    results = repair_missing_tags(cwd=repo, config=_fresh_config())

    assert [r.action for r in results] == [RepairAction.UNREPAIRABLE]
    assert "manifest" in results[0].detail


def test_repair_skips_a_source_rewritten_off_the_lineage(tmp_path: Path) -> None:
    """Minting a tag on an abandoned lineage would move policy ground.

    A tag outside the lineage changes what the baseline search sees and can
    trip the `every_commit` rewritten-history guard. `validate` already names
    these proofs `orphaned`; deciding what to do about one is not a repair.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n", date=_T0)
    abandoned_id = _commit(repo, paths=["b.txt"], message="B\n", date=_T0)
    _write_orphan_proof(repo, abandoned_id)
    _git(["reset", "-q", "--hard", "HEAD~1"], cwd=repo)

    results = repair_missing_tags(cwd=repo, config=_fresh_config())

    assert [r.action for r in results] == [RepairAction.SKIPPED_OFF_LINEAGE]
    assert list(enumerate_timestamp_tags(cwd=repo, tag_prefix="ots/")) == []


def test_repair_skips_a_source_the_repository_no_longer_has(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n", date=_T0)
    unknown_id = "0" * 39 + "1"
    _write_orphan_proof(repo, unknown_id)

    results = repair_missing_tags(cwd=repo, config=_fresh_config())

    assert [r.action for r in results] == [RepairAction.SKIPPED_UNRESOLVED]


def test_repair_reports_every_proof_and_repairs_what_it_can(
    tmp_path: Path,
) -> None:
    """One unrepairable proof must not stop the others being repaired."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    first = _commit(repo, paths=["a.txt"], message="A\n", date=_T0)
    second = _commit(repo, paths=["b.txt"], message="B\n", date=_T0)
    _write_orphan_proof(repo, first)
    _write_orphan_proof(repo, second)
    # Corrupt only the second manifest.
    (repo / ".opentimestamps" / f"{second}.json").write_text(
        "{ not json", encoding="utf-8"
    )

    results = {
        r.source_commit_id: r.action
        for r in repair_missing_tags(cwd=repo, config=_fresh_config())
    }

    assert results[first] is RepairAction.TAGGED
    assert results[second] is RepairAction.UNREPAIRABLE


def test_repair_finds_nothing_to_do_after_a_healthy_run(tmp_path: Path) -> None:
    """A repository git-ots stamped itself is already fully reconciled."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n", date=_T0)
    config = _fresh_config()
    now = _T0 + timedelta(days=2)

    def submit(commit_id: str, payload: bytes) -> bytes:
        return _make_fake_detached_proof(payload=build_payload("sha1", commit_id))

    run_orchestration(
        snapshot=build_snapshot(cwd=repo, config=config, now=now),
        now=now,
        submit=submit,
    )

    results = repair_missing_tags(cwd=repo, config=config)

    assert results
    assert all(r.action is RepairAction.ALREADY_TAGGED for r in results)


def test_repair_returns_empty_when_no_proof_directory_exists(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n", date=_T0)

    assert repair_missing_tags(cwd=repo, config=_fresh_config()) == []
