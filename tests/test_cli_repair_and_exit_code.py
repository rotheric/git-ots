"""CLI surface for tag reconciliation: `repair`, `validate`, `run --exit-code`.

These pin the operator-facing half of the orphaned-proof fix -- the part that
turns "every diagnostic reports success while the tag is missing" into
something a person, and a scheduler, can act on.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from git_ots.cli import IDLE_EXIT_CODE, main
from git_ots.git import enumerate_timestamp_tags
from git_ots.orchestration import RunOutcome
from git_ots.timestamp import build_manifest, build_payload
from tests.test_cli import _commit, _git, _init_repo, _set_config
from tests.test_timestamp import _make_fake_detached_proof

_T0 = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)
_SUBMITTED_AT = datetime(2026, 8, 18, 10, 30, 0, tzinfo=UTC)


def _write_untagged_proof(repo: Path, commit_id: str) -> None:
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir(parents=True, exist_ok=True)
    (proof_dir / f"{commit_id}.ots").write_bytes(
        _make_fake_detached_proof(payload=build_payload("sha1", commit_id))
    )
    (proof_dir / f"{commit_id}.json").write_text(
        json.dumps(
            build_manifest(
                object_format="sha1",
                commit_id=commit_id,
                proof_name=f"{commit_id}.ots",
                source_ref="refs/heads/main",
                submitted_at=_SUBMITTED_AT,
                triggers=["max_age"],
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _repo_with_untagged_proof(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n", date=_T0)
    _write_untagged_proof(repo, commit_id)
    return repo, commit_id


def test_validate_names_the_untagged_proof_and_still_exits_zero(
    capsys, tmp_path: Path
) -> None:
    """The silence is gone; the exit contract is not.

    A missing tag is real information, but it is not evidence of damage -- a
    fresh clone has no tags at all, because git-ots never pushes them. Making
    it non-zero would fail `validate` on every clone.
    """
    repo, commit_id = _repo_with_untagged_proof(tmp_path)

    exit_code = main(argv=["validate"], cwd=repo, now=_T0 + timedelta(hours=2))

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "untagged" in captured.out
    assert commit_id[:12] in captured.out
    assert "git-ots repair" in captured.out


def test_validate_says_nothing_about_tags_when_none_are_missing(
    capsys, tmp_path: Path
) -> None:
    repo, _commit_id = _repo_with_untagged_proof(tmp_path)
    main(argv=["repair"], cwd=repo, now=_T0 + timedelta(hours=2))
    capsys.readouterr()

    main(argv=["validate"], cwd=repo, now=_T0 + timedelta(hours=2))

    captured = capsys.readouterr()
    assert "untagged" not in captured.out


def test_repair_creates_the_tag_and_reports_it(capsys, tmp_path: Path) -> None:
    repo, commit_id = _repo_with_untagged_proof(tmp_path)

    exit_code = main(argv=["repair"], cwd=repo, now=_T0 + timedelta(hours=2))

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "tagged" in captured.out
    assert commit_id[:12] in captured.out
    tags = list(enumerate_timestamp_tags(cwd=repo, tag_prefix="ots/"))
    assert [tag.commit_id for tag in tags] == [commit_id]


def test_repair_dry_run_reports_without_creating(capsys, tmp_path: Path) -> None:
    repo, _commit_id = _repo_with_untagged_proof(tmp_path)

    exit_code = main(
        argv=["repair", "--dry-run"], cwd=repo, now=_T0 + timedelta(hours=2)
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "would-tag" in captured.out
    assert list(enumerate_timestamp_tags(cwd=repo, tag_prefix="ots/")) == []


def test_repair_is_quiet_when_there_is_nothing_to_reconcile(
    capsys, tmp_path: Path
) -> None:
    repo, _commit_id = _repo_with_untagged_proof(tmp_path)
    main(argv=["repair"], cwd=repo, now=_T0 + timedelta(hours=2))
    capsys.readouterr()

    exit_code = main(argv=["repair"], cwd=repo, now=_T0 + timedelta(hours=2))

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "already have a timestamp tag" in captured.out


def test_repair_exits_5_when_a_manifest_cannot_be_validated(
    capsys, tmp_path: Path
) -> None:
    """A tag that cannot be reconstructed is not a success to report as one."""
    repo, commit_id = _repo_with_untagged_proof(tmp_path)
    (repo / ".opentimestamps" / f"{commit_id}.json").write_text(
        "{ not json", encoding="utf-8"
    )

    exit_code = main(argv=["repair"], cwd=repo, now=_T0 + timedelta(hours=2))

    captured = capsys.readouterr()
    assert exit_code == 5
    assert "unrepairable" in captured.out


def test_repair_reports_no_proofs_when_the_directory_is_absent(
    capsys, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n", date=_T0)

    exit_code = main(argv=["repair"], cwd=repo, now=_T0 + timedelta(hours=2))

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "No stored proofs found." in captured.out


def test_run_exit_code_flag_reports_an_idle_run(capsys, tmp_path: Path) -> None:
    """Opt-in, so no existing cron wrapper changes meaning underneath it."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n", date=_T0)
    _set_config(repo, max_age="24h", fetch_before_run=False)
    now = _T0 + timedelta(hours=1)  # nothing is due yet

    assert main(argv=["run"], cwd=repo, now=now) == 0
    capsys.readouterr()

    assert main(argv=["run", "--exit-code"], cwd=repo, now=now) == IDLE_EXIT_CODE


def test_run_exit_code_flag_leaves_a_dry_run_at_zero(tmp_path: Path) -> None:
    """A dry run reports rather than acts, so it is never 'idle' in this sense."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n", date=_T0)
    _set_config(repo, max_age="24h", fetch_before_run=False)

    exit_code = main(
        argv=["run", "--dry-run", "--exit-code"],
        cwd=repo,
        now=_T0 + timedelta(hours=1),
    )

    assert exit_code == 0


def test_run_exit_code_flag_reports_a_repairing_run_as_not_idle(
    capsys, tmp_path: Path
) -> None:
    """Completing an interrupted run is work, and must not report as idle."""
    repo, commit_id = _repo_with_untagged_proof(tmp_path)
    _set_config(repo, max_age="24h", fetch_before_run=False)
    _git(["add", "--", ".opentimestamps"], cwd=repo)
    _git(["commit", "-q", "-m", "sweep in stray files"], cwd=repo)
    now = _T0 + timedelta(hours=1)

    exit_code = main(argv=["run", "--exit-code"], cwd=repo, now=now)

    captured = capsys.readouterr()
    assert exit_code == 0, captured.out
    assert "completed timestamp tag" in captured.out
    assert commit_id[:12] in captured.out


def test_run_pushes_only_after_a_successful_change(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    commit_id = _commit(repo, paths=["a.txt"], message="A\n", date=_T0)
    _set_config(repo, max_age="24h", fetch_before_run=False)
    calls: list[Path] = []

    monkeypatch.setattr(
        "git_ots.cli.run_orchestration",
        lambda *, snapshot: RunOutcome(tagged=(commit_id,)),
    )
    monkeypatch.setattr(
        "git_ots.cli.push_current_branch",
        lambda *, cwd, process_runner: calls.append(cwd),
    )

    assert main(["run", "--push"], cwd=repo, now=_T0 + timedelta(hours=1)) == 0
    assert calls == [repo]


def test_run_push_flag_does_not_push_an_idle_or_dry_run(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n", date=_T0)
    _set_config(repo, max_age="24h", fetch_before_run=False)
    calls: list[Path] = []
    monkeypatch.setattr(
        "git_ots.cli.push_current_branch",
        lambda *, cwd, process_runner: calls.append(cwd),
    )

    now = _T0 + timedelta(hours=1)
    assert main(["run", "--push"], cwd=repo, now=now) == 0
    assert main(["run", "--dry-run", "--push"], cwd=repo, now=now) == 0
    assert calls == []
