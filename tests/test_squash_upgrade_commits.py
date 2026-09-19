"""Tests for ``ots.squashUpgradeCommits`` -- folding repeated upgrades.

Amending is the one thing this tool does that rewrites an object somebody may
already have, so the tests here pin the guards at least as hard as the
behaviour: what shape of ``HEAD`` is eligible, what a squash preserves that a
fresh commit would have preserved anyway, and that the default stays off. See
specs/spec.md section 28.6.2.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from git_ots.cli import main
from git_ots.config import Config, ProofConfig
from git_ots.git import (
    GitCommandError,
    InvalidRepositoryStateError,
    amend_upgrade_commit,
    create_upgrade_commit,
    find_squashable_upgrade_commit,
    is_published,
    stage_paths,
)
from git_ots.timestamp import UpgradeAttempt, build_payload
from git_ots.upgrade import UpgradeState, upgrade_proofs
from tests.test_signing import (
    _FAILING_SIGNER,
    _FAKE_SIGNER,
    _install_signer,
)
from tests.test_timestamp import _make_fake_detached_proof
from tests.test_verify import (
    _commit,
    _fresh_config,
    _git,
    _init_repo,
    _make_bitcoin_attested_proof,
    _write_proof_and_manifest,
)

PROOF_DIRECTORY = ".opentimestamps"


def _no_calendars(url: str, commitment: bytes) -> tuple[bytes | None, str]:
    """A fetcher that reaches no calendar, so tests never touch the network."""
    return None, "not contacted"


class _ByProofClient:
    """Upgrade client keyed on the proof bytes it is handed.

    The per-candidate ordering of an upgrade pass is an implementation detail
    (the proof directory is walked in sorted order), so a queue of outcomes
    would couple these tests to it. Keying on the input says which proof is
    meant to complete without asserting when it is reached.
    """

    def __init__(self, completions: dict[bytes, bytes]) -> None:
        self._completions = completions
        self.calls: list[bytes] = []

    def upgrade(self, proof_bytes: bytes, payload: bytes) -> UpgradeAttempt:
        self.calls.append(proof_bytes)
        completed = self._completions.get(proof_bytes)
        if completed is None:
            return UpgradeAttempt(
                upgraded=None, detail="Failed! Timestamp not complete"
            )
        return UpgradeAttempt(upgraded=completed, detail="")


def _squashing_config(**proof_overrides: object) -> Config:
    base = _fresh_config()
    return replace(
        base,
        proof=replace(base.proof, squash_upgrade_commits=True, **proof_overrides),
    )


def _commit_message(repo: Path, rev: str = "HEAD") -> str:
    return _git(["log", "-1", "--format=%B", rev], cwd=repo)


def _commit_count(repo: Path) -> int:
    return int(_git(["rev-list", "--count", "HEAD"], cwd=repo).strip())


def _repo_with_pending_proofs(
    tmp_path: Path,
    *,
    count: int = 2,
    proof_directory: str = PROOF_DIRECTORY,
    signer: str | None = None,
    signer_log: Path | None = None,
) -> tuple[Path, tuple[str, ...]]:
    """A repository with ``count`` committed, still-pending proofs.

    At least two are needed for anything about squashing: a squash needs two
    upgrade passes that each have something to commit, and a proof that is
    already anchored is never upgraded a second time.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    if signer is not None:
        assert signer_log is not None
        _install_signer(repo, signer, log=signer_log)

    sources = tuple(
        _commit(repo, paths=[f"{chr(ord('a') + index)}.txt"], message=f"C{index}\n")
        for index in range(count)
    )

    for source_id in sources:
        payload = build_payload("sha1", source_id)
        _write_proof_and_manifest(
            repo, source_id, _make_fake_detached_proof(payload=payload)
        )
    if proof_directory != PROOF_DIRECTORY:
        # A plain rename, not `git mv`: nothing is tracked yet at this point.
        destination = repo / proof_directory
        destination.parent.mkdir(parents=True, exist_ok=True)
        (repo / PROOF_DIRECTORY).rename(destination)

    _git(["add", proof_directory], cwd=repo)
    trailers = "".join(f"OpenTimestamps-Source: {source}\n" for source in sources)
    subprocess.run(
        [
            "git",
            "commit",
            "-q",
            "--no-gpg-sign",
            "-m",
            f"Store OpenTimestamps proofs\n\nOpenTimestamps-Generated: true\n{trailers}",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo, sources


def _anchor_on_disk(repo: Path, source_id: str) -> str:
    """Replace a stored proof with an anchored one and return its path.

    Lets the amend tests drive `create_upgrade_commit`/`amend_upgrade_commit`
    directly: a commit restricted to a pathspec fails when that path has not
    actually changed.
    """
    payload = build_payload("sha1", source_id)
    relative = f"{PROOF_DIRECTORY}/{source_id}.ots"
    (repo / relative).write_bytes(_make_bitcoin_attested_proof(payload))
    return relative


def _upgrade_one(repo: Path, source_id: str, *, config: Config, fetcher=_no_calendars):
    """Run an upgrade pass in which exactly ``source_id``'s proof completes."""
    payload = build_payload("sha1", source_id)
    pending = (repo / config.proof.directory / f"{source_id}.ots").read_bytes()
    client = _ByProofClient({pending: _make_bitcoin_attested_proof(payload)})
    return upgrade_proofs(cwd=repo, config=config, client=client, fetcher=fetcher)


# --- squash-target eligibility ---------------------------------------------


def _repo_with_one_upgrade_commit(tmp_path: Path) -> tuple[Path, str, str]:
    repo, (first, second) = _repo_with_pending_proofs(tmp_path)
    report = _upgrade_one(repo, first, config=_fresh_config())
    assert report.commit_id is not None
    return repo, first, second


def test_head_upgrade_commit_is_a_squash_target(tmp_path: Path) -> None:
    repo, first, _second = _repo_with_one_upgrade_commit(tmp_path)

    target = find_squashable_upgrade_commit(cwd=repo, proof_directory=PROOF_DIRECTORY)

    assert target is not None
    assert target.source_commit_ids == (first,)
    assert target.commit_id == _git(["rev-parse", "HEAD"], cwd=repo).strip()


def test_head_proof_commit_is_not_a_squash_target(tmp_path: Path) -> None:
    """A proof commit dates a submission and is what recovery reads back."""
    repo, _sources = _repo_with_pending_proofs(tmp_path)

    assert "OpenTimestamps-Source:" in _commit_message(repo)
    assert (
        find_squashable_upgrade_commit(cwd=repo, proof_directory=PROOF_DIRECTORY)
        is None
    )


def test_head_ordinary_commit_is_not_a_squash_target(tmp_path: Path) -> None:
    repo, _first, _second = _repo_with_one_upgrade_commit(tmp_path)
    _commit(repo, paths=["c.txt"], message="C\n")

    assert (
        find_squashable_upgrade_commit(cwd=repo, proof_directory=PROOF_DIRECTORY)
        is None
    )


def test_generated_commit_without_upgraded_trailer_is_not_a_squash_target(
    tmp_path: Path,
) -> None:
    """The generated trailer alone does not say which kind of commit this is."""
    repo, _sources = _repo_with_pending_proofs(tmp_path)
    (repo / "note.txt").write_text("note\n")
    _git(["add", "note.txt"], cwd=repo)
    subprocess.run(
        [
            "git",
            "commit",
            "-q",
            "-m",
            "Bookkeeping\n\nOpenTimestamps-Generated: true\n",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    assert (
        find_squashable_upgrade_commit(cwd=repo, proof_directory=PROOF_DIRECTORY)
        is None
    )


def test_merge_commit_is_not_a_squash_target(tmp_path: Path) -> None:
    """Amending a merge would reshape a commit with more than one parent."""
    repo, first, _second = _repo_with_one_upgrade_commit(tmp_path)
    upgrade_commit = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    _git(["checkout", "-q", "-b", "side", "HEAD~1"], cwd=repo)
    _commit(repo, paths=["side.txt"], message="side\n")
    _git(["checkout", "-q", "main"], cwd=repo)
    subprocess.run(
        [
            "git",
            "merge",
            "-q",
            "--no-ff",
            "side",
            "-m",
            (
                f"Merge side\n\nOpenTimestamps-Generated: true\n"
                f"OpenTimestamps-Upgraded: {first}\n"
            ),
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() != upgrade_commit

    assert (
        find_squashable_upgrade_commit(cwd=repo, proof_directory=PROOF_DIRECTORY)
        is None
    )


def test_published_upgrade_commit_is_not_a_squash_target(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Amending a pushed commit leaves a branch that cannot fast-forward."""
    repo, _first, _second = _repo_with_one_upgrade_commit(tmp_path)
    remote = tmp_path / "remote.git"
    _git(["init", "-q", "--bare", str(remote)], cwd=tmp_path)
    _git(["remote", "add", "origin", str(remote)], cwd=repo)
    _git(["push", "-q", "-u", "origin", "main"], cwd=repo)

    with caplog.at_level(logging.WARNING, logger="git_ots.git"):
        target = find_squashable_upgrade_commit(
            cwd=repo, proof_directory=PROOF_DIRECTORY
        )

    assert target is None
    # The one declined case an operator asked for and did not get.
    assert "remote-tracking ref" in caplog.text


def test_is_published_tracks_remote_reachability(tmp_path: Path) -> None:
    repo, _first, _second = _repo_with_one_upgrade_commit(tmp_path)
    remote = tmp_path / "remote.git"
    _git(["init", "-q", "--bare", str(remote)], cwd=tmp_path)
    _git(["remote", "add", "origin", str(remote)], cwd=repo)

    # A remote that exists but has never been pushed to publishes nothing.
    assert is_published(cwd=repo, commit="HEAD") is False

    _git(["push", "-q", "-u", "origin", "main"], cwd=repo)

    assert is_published(cwd=repo, commit="HEAD") is True

    _commit(repo, paths=["c.txt"], message="C\n")

    assert is_published(cwd=repo, commit="HEAD") is False
    assert is_published(cwd=repo, commit="HEAD~1") is True


# --- the amend itself -------------------------------------------------------


def test_amend_names_the_union_of_old_and_new_sources(tmp_path: Path) -> None:
    repo, (first, second) = _repo_with_pending_proofs(tmp_path)
    paths = [_anchor_on_disk(repo, first)]
    stage_paths(cwd=repo, paths=paths)
    create_upgrade_commit(
        cwd=repo,
        source_commit_ids=[first],
        proof_directory=PROOF_DIRECTORY,
        paths=paths,
    )
    parent_before = _git(["rev-parse", "HEAD^"], cwd=repo).strip()
    author_date_before = _git(["log", "-1", "--format=%aI"], cwd=repo).strip()

    new_paths = [_anchor_on_disk(repo, second)]
    stage_paths(cwd=repo, paths=new_paths)
    amended = amend_upgrade_commit(
        cwd=repo,
        expected_head=_git(["rev-parse", "HEAD"], cwd=repo).strip(),
        previous_source_commit_ids=[first],
        source_commit_ids=[second],
        proof_directory=PROOF_DIRECTORY,
        paths=new_paths,
    )

    message = _commit_message(repo)
    assert message.startswith("Upgrade 2 OpenTimestamps proofs")
    assert f"OpenTimestamps-Upgraded: {first}" in message
    assert f"OpenTimestamps-Upgraded: {second}" in message
    assert "OpenTimestamps-Generated: true" in message
    assert _git(["rev-parse", f"{amended}^"], cwd=repo).strip() == parent_before
    assert _git(["log", "-1", "--format=%aI"], cwd=repo).strip() == author_date_before


def test_amend_deduplicates_a_source_upgraded_again(tmp_path: Path) -> None:
    """Two passes may both touch one proof; the record says so once."""
    repo, (first, _second) = _repo_with_pending_proofs(tmp_path)
    paths = [_anchor_on_disk(repo, first)]
    stage_paths(cwd=repo, paths=paths)
    create_upgrade_commit(
        cwd=repo,
        source_commit_ids=[first],
        proof_directory=PROOF_DIRECTORY,
        paths=paths,
    )

    amend_upgrade_commit(
        cwd=repo,
        expected_head=_git(["rev-parse", "HEAD"], cwd=repo).strip(),
        previous_source_commit_ids=[first],
        source_commit_ids=[first],
        proof_directory=PROOF_DIRECTORY,
        paths=paths,
    )

    message = _commit_message(repo)
    assert message.count(f"OpenTimestamps-Upgraded: {first}") == 1
    assert message.startswith("Upgrade OpenTimestamps proof for ")


def test_amend_does_not_sweep_in_unrelated_staged_or_dirty_changes(
    tmp_path: Path,
) -> None:
    """The amend must be no less selective than the commit it replaces."""
    repo, (first, second) = _repo_with_pending_proofs(tmp_path)
    paths = [_anchor_on_disk(repo, first)]
    stage_paths(cwd=repo, paths=paths)
    create_upgrade_commit(
        cwd=repo,
        source_commit_ids=[first],
        proof_directory=PROOF_DIRECTORY,
        paths=paths,
    )

    (repo / "staged.txt").write_text("staged\n")
    _git(["add", "staged.txt"], cwd=repo)
    (repo / "a.txt").write_text("edited\n")

    new_paths = [_anchor_on_disk(repo, second)]
    stage_paths(cwd=repo, paths=new_paths)
    amend_upgrade_commit(
        cwd=repo,
        expected_head=_git(["rev-parse", "HEAD"], cwd=repo).strip(),
        previous_source_commit_ids=[first],
        source_commit_ids=[second],
        proof_directory=PROOF_DIRECTORY,
        paths=new_paths,
    )

    committed = _git(["ls-tree", "-r", "--name-only", "HEAD"], cwd=repo).split()
    assert "staged.txt" not in committed
    assert _git(["show", "HEAD:a.txt"], cwd=repo) == "a.txt\n"
    # Both proofs the two passes touched are in the amended tree.
    assert f"{PROOF_DIRECTORY}/{first}.ots" in committed
    assert f"{PROOF_DIRECTORY}/{second}.ots" in committed


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"source_commit_ids": []}, id="no-new-sources"),
        pytest.param({"source_commit_ids": [""]}, id="empty-new-source"),
        pytest.param({"previous_source_commit_ids": [""]}, id="empty-previous-source"),
        pytest.param({"paths": []}, id="no-paths"),
        pytest.param({"paths": [""]}, id="empty-path"),
        pytest.param({"proof_directory": ""}, id="no-proof-directory"),
        pytest.param({"expected_head": ""}, id="no-expected-head"),
    ],
)
def test_amend_rejects_degenerate_arguments(tmp_path: Path, kwargs: dict) -> None:
    repo, (first, second) = _repo_with_pending_proofs(tmp_path)
    arguments = {
        "cwd": repo,
        "expected_head": _git(["rev-parse", "HEAD"], cwd=repo).strip(),
        "previous_source_commit_ids": [first],
        "source_commit_ids": [second],
        "proof_directory": PROOF_DIRECTORY,
        "paths": [f"{PROOF_DIRECTORY}/{second}.ots"],
        **kwargs,
    }

    with pytest.raises(ValueError):
        amend_upgrade_commit(**arguments)


# --- the upgrade pass -------------------------------------------------------


def test_second_upgrade_stacks_a_commit_by_default(tmp_path: Path) -> None:
    """The default is off, so the behaviour every existing repository sees
    must be the one it saw before this setting existed."""
    repo, (first, second) = _repo_with_pending_proofs(tmp_path)
    _upgrade_one(repo, first, config=_fresh_config())
    before = _commit_count(repo)

    report = _upgrade_one(repo, second, config=_fresh_config())

    assert report.squashed is False
    assert _commit_count(repo) == before + 1
    assert f"OpenTimestamps-Upgraded: {first}" not in _commit_message(repo)


def test_second_upgrade_is_folded_into_the_first_when_enabled(tmp_path: Path) -> None:
    repo, (first, second) = _repo_with_pending_proofs(tmp_path)
    config = _squashing_config()
    first_report = _upgrade_one(repo, first, config=config)
    assert first_report.squashed is False
    after_first = _commit_count(repo)
    first_commit = first_report.commit_id

    report = _upgrade_one(repo, second, config=config)

    assert report.squashed is True
    assert _commit_count(repo) == after_first
    assert report.commit_id is not None
    # Amending rewrites the object, so the reported ID is necessarily new.
    assert report.commit_id != first_commit
    message = _commit_message(repo)
    assert f"OpenTimestamps-Upgraded: {first}" in message
    assert f"OpenTimestamps-Upgraded: {second}" in message
    assert {r.source_commit_id: r.state for r in report.results} == {
        first: UpgradeState.ALREADY_COMPLETE,
        second: UpgradeState.UPGRADED,
    }


def test_squashing_keeps_the_proof_the_earlier_upgrade_committed(
    tmp_path: Path,
) -> None:
    """The amended tree is HEAD's, not its parent's: a fold may not undo an
    upgrade it is folding into."""
    repo, (first, second) = _repo_with_pending_proofs(tmp_path)
    config = _squashing_config()
    _upgrade_one(repo, first, config=config)
    anchored_first = (repo / PROOF_DIRECTORY / f"{first}.ots").read_bytes()

    _upgrade_one(repo, second, config=config)

    committed = subprocess.run(
        ["git", "show", f"HEAD:{PROOF_DIRECTORY}/{first}.ots"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    assert committed == anchored_first
    assert _git(["status", "--porcelain"], cwd=repo) == ""


def test_first_upgrade_after_a_proof_commit_is_never_folded(tmp_path: Path) -> None:
    repo, (first, _second) = _repo_with_pending_proofs(tmp_path)
    before = _commit_count(repo)

    report = _upgrade_one(repo, first, config=_squashing_config())

    assert report.squashed is False
    assert _commit_count(repo) == before + 1
    assert "OpenTimestamps-Source:" in _commit_message(repo, "HEAD~1")


def test_published_upgrade_commit_gets_a_new_commit_instead(tmp_path: Path) -> None:
    repo, (first, second) = _repo_with_pending_proofs(tmp_path)
    config = _squashing_config()
    _upgrade_one(repo, first, config=config)
    remote = tmp_path / "remote.git"
    _git(["init", "-q", "--bare", str(remote)], cwd=tmp_path)
    _git(["remote", "add", "origin", str(remote)], cwd=repo)
    _git(["push", "-q", "-u", "origin", "main"], cwd=repo)
    published = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    before = _commit_count(repo)

    report = _upgrade_one(repo, second, config=config)

    assert report.squashed is False
    assert _commit_count(repo) == before + 1
    assert _git(["rev-parse", "HEAD^"], cwd=repo).strip() == published


def test_squashing_is_inert_when_proofs_are_not_committed(tmp_path: Path) -> None:
    repo, (first, second) = _repo_with_pending_proofs(tmp_path)
    config = _squashing_config(commit=False)
    before = _commit_count(repo)

    _upgrade_one(repo, first, config=config)
    report = _upgrade_one(repo, second, config=config)

    assert report.commit_id is None
    assert report.squashed is False
    assert _commit_count(repo) == before


def test_cli_reports_a_fold_distinctly_from_a_new_commit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, (first, second) = _repo_with_pending_proofs(tmp_path)
    _git(["config", "ots.squashUpgradeCommits", "true"], cwd=repo)
    _git(["config", "ots.fetchBeforeRun", "false"], cwd=repo)

    def _client_for(repo_path: Path, source_id: str):
        payload = build_payload("sha1", source_id)
        pending = (repo_path / PROOF_DIRECTORY / f"{source_id}.ots").read_bytes()
        return _ByProofClient({pending: _make_bitcoin_attested_proof(payload)})

    import git_ots.upgrade as upgrade_module

    for source_id, expected in (
        (first, "Committed upgraded proofs as "),
        (second, "Folded upgraded proofs into the previous upgrade commit, now "),
    ):
        client = _client_for(repo, source_id)
        monkeypatch.setattr(
            upgrade_module, "OpenTimestampsCli", lambda *a, _c=client, **k: _c
        )
        monkeypatch.setattr(upgrade_module, "fetch_calendar_timestamp", _no_calendars)
        assert main(["upgrade"], cwd=str(repo)) == 0
        assert expected in capsys.readouterr().out


def test_configuration_key_enables_squashing_through_git_config(
    tmp_path: Path,
) -> None:
    """The setting reaches `Config` by the ordinary namespace route."""
    from git_ots.gitconfig import assemble_config

    repo, _sources = _repo_with_pending_proofs(tmp_path)

    assert assemble_config(cwd=repo).proof.squash_upgrade_commits is False

    _git(["config", "ots.squashUpgradeCommits", "true"], cwd=repo)

    assert assemble_config(cwd=repo).proof.squash_upgrade_commits is True


def test_default_proof_config_does_not_squash() -> None:
    assert ProofConfig().squash_upgrade_commits is False


def test_three_consecutive_upgrades_fold_into_one_commit(tmp_path: Path) -> None:
    """The point of the setting is the third and fourth run, not the second:
    a squashed commit must itself remain a squash target."""
    repo, sources = _repo_with_pending_proofs(tmp_path, count=3)
    config = _squashing_config()
    _upgrade_one(repo, sources[0], config=config)
    after_first = _commit_count(repo)

    for source_id in sources[1:]:
        report = _upgrade_one(repo, source_id, config=config)
        assert report.squashed is True

    assert _commit_count(repo) == after_first
    message = _commit_message(repo)
    assert message.startswith("Upgrade 3 OpenTimestamps proofs")
    for source_id in sources:
        assert f"OpenTimestamps-Upgraded: {source_id}" in message
    # Every proof the three passes anchored is in the one commit's tree.
    committed = _git(["ls-tree", "-r", "--name-only", "HEAD"], cwd=repo).split()
    for source_id in sources:
        assert f"{PROOF_DIRECTORY}/{source_id}.ots" in committed
    assert _git(["status", "--porcelain"], cwd=repo) == ""


def test_squashing_follows_a_moved_proof_directory(tmp_path: Path) -> None:
    repo, (first, second) = _repo_with_pending_proofs(
        tmp_path, proof_directory="audit/anchors"
    )
    base = _fresh_config()
    config = replace(
        base,
        proof=replace(
            base.proof, directory="audit/anchors", squash_upgrade_commits=True
        ),
    )
    _upgrade_one(repo, first, config=config)
    after_first = _commit_count(repo)

    report = _upgrade_one(repo, second, config=config)

    assert report.squashed is True
    assert _commit_count(repo) == after_first
    committed = _git(["ls-tree", "-r", "--name-only", "HEAD"], cwd=repo).split()
    assert f"audit/anchors/{first}.ots" in committed
    assert f"audit/anchors/{second}.ots" in committed


def test_amend_is_signed_when_signing_is_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "signer.log"
    monkeypatch.setenv("SIGNER_LOG", str(log))
    repo, (first, second) = _repo_with_pending_proofs(
        tmp_path, signer=_FAKE_SIGNER, signer_log=log
    )
    base = _squashing_config()
    config = replace(base, git=replace(base.git, signing="required"))
    _upgrade_one(repo, first, config=config)

    report = _upgrade_one(repo, second, config=config)

    assert report.squashed is True
    assert "-----BEGIN PGP SIGNATURE-----" in _git(
        ["cat-file", "commit", "HEAD"], cwd=repo
    )


def test_failing_signer_aborts_the_amend_without_destroying_the_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An amend that Git refuses must leave HEAD where it was.

    This is the failure mode that would make squashing unsafe: a rewrite that
    half-happened would take the earlier upgrade's record with it. The
    upgraded proof stays on disk and uncommitted, exactly as it does when an
    ordinary upgrade commit is vetoed, so the next pass recovers it.
    """
    log = tmp_path / "signer.log"
    monkeypatch.setenv("SIGNER_LOG", str(log))
    repo, (first, second) = _repo_with_pending_proofs(tmp_path)
    config = _squashing_config()
    _upgrade_one(repo, first, config=config)
    head_before = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    message_before = _commit_message(repo)

    _install_signer(repo, _FAILING_SIGNER, log=log)
    signing = replace(config, git=replace(config.git, signing="required"))
    with pytest.raises(GitCommandError):
        _upgrade_one(repo, second, config=signing)

    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == head_before
    assert _commit_message(repo) == message_before
    assert f"{PROOF_DIRECTORY}/{second}.ots" in _git(
        ["status", "--porcelain"], cwd=repo
    )


def test_amend_rejected_by_a_hook_leaves_the_predecessor_intact(
    tmp_path: Path,
) -> None:
    repo, (first, second) = _repo_with_pending_proofs(tmp_path)
    config = _squashing_config()
    _upgrade_one(repo, first, config=config)
    head_before = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    with pytest.raises(GitCommandError):
        _upgrade_one(repo, second, config=config)

    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == head_before

    hook.unlink()
    report = _upgrade_one(repo, second, config=config)

    assert report.squashed is True
    assert f"OpenTimestamps-Upgraded: {first}" in _commit_message(repo)
    assert f"OpenTimestamps-Upgraded: {second}" in _commit_message(repo)


def _squashed_in_operator_commit(repo: Path, upgraded_source: str) -> str:
    """Commit what `git rebase -i` leaves when an upgrade is squashed into work.

    Git concatenates the two messages, so the result carries this tool's
    trailers *and* the operator's subject and body, and changes real files.
    Classification by trailer alone (ADR D3) reads it as generated metadata.
    """
    (repo / "src.py").write_text("real work\n")
    (repo / PROOF_DIRECTORY / f"{upgraded_source}.ots").write_bytes(
        _make_bitcoin_attested_proof(build_payload("sha1", upgraded_source))
    )
    _git(["add", "src.py", PROOF_DIRECTORY], cwd=repo)
    subprocess.run(
        ["git", "commit", "-q", "-F", "-"],
        cwd=repo,
        check=True,
        capture_output=True,
        input=(
            "Fix the widget frobnicator\n"
            "\n"
            "A long explanation nobody else has a copy of.\n"
            "\n"
            "OpenTimestamps-Generated: true\n"
            f"OpenTimestamps-Upgraded: {upgraded_source}\n"
        ).encode(),
    )
    return _git(["rev-parse", "HEAD"], cwd=repo).strip()


def test_operator_commit_carrying_upgrade_trailers_is_not_a_squash_target(
    tmp_path: Path,
) -> None:
    """The trailers are not proof of authorship; the whole message is.

    Amending this commit would keep its tree but replace its message with
    this tool's, destroying the only copy of what the operator wrote.
    """
    repo, (first, _second) = _repo_with_pending_proofs(tmp_path)
    _squashed_in_operator_commit(repo, first)

    assert (
        find_squashable_upgrade_commit(cwd=repo, proof_directory=PROOF_DIRECTORY)
        is None
    )


def test_an_upgrade_does_not_rewrite_an_operator_commit_message(
    tmp_path: Path,
) -> None:
    repo, (first, second) = _repo_with_pending_proofs(tmp_path)
    _squashed_in_operator_commit(repo, first)
    message_before = _commit_message(repo)
    before = _commit_count(repo)

    report = _upgrade_one(repo, second, config=_squashing_config())

    assert report.squashed is False
    assert _commit_count(repo) == before + 1
    assert _commit_message(repo, "HEAD~1") == message_before
    assert "Fix the widget frobnicator" in _commit_message(repo, "HEAD~1")


def test_upgrade_commit_touching_a_path_outside_the_proof_directory_is_declined(
    tmp_path: Path,
) -> None:
    """§13's outside-the-directory diagnostic declines here rather than warns:
    the commit is about to be rewritten, not merely classified."""
    repo, (first, _second) = _repo_with_pending_proofs(tmp_path)
    paths = [_anchor_on_disk(repo, first)]
    stage_paths(cwd=repo, paths=paths)
    create_upgrade_commit(
        cwd=repo,
        source_commit_ids=[first],
        proof_directory=PROOF_DIRECTORY,
        paths=paths,
    )
    approved = find_squashable_upgrade_commit(cwd=repo, proof_directory=PROOF_DIRECTORY)
    assert approved is not None and approved.source_commit_ids == (first,)

    # Same message, one extra file folded in.
    (repo / "smuggled.txt").write_text("smuggled\n")
    _git(["add", "smuggled.txt"], cwd=repo)
    subprocess.run(
        ["git", "commit", "-q", "--amend", "--no-edit"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    assert (
        find_squashable_upgrade_commit(cwd=repo, proof_directory=PROOF_DIRECTORY)
        is None
    )


def test_dry_run_never_amends(tmp_path: Path) -> None:
    repo, (first, _second) = _repo_with_pending_proofs(tmp_path)
    config = _squashing_config()
    _upgrade_one(repo, first, config=config)
    head_before = _git(["rev-parse", "HEAD"], cwd=repo).strip()

    report = upgrade_proofs(
        cwd=repo, config=config, dry_run=True, fetcher=_no_calendars
    )

    assert report.commit_id is None
    assert report.squashed is False
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == head_before
    assert _git(["status", "--porcelain"], cwd=repo) == ""


def test_dry_run_reports_the_fold_it_would_make(tmp_path: Path) -> None:
    """The one moment an operator can see the decision before it rewrites."""
    repo, (first, _second) = _repo_with_pending_proofs(tmp_path)
    config = _squashing_config()
    _upgrade_one(repo, first, config=config)
    head = _git(["rev-parse", "HEAD"], cwd=repo).strip()

    report = upgrade_proofs(
        cwd=repo, config=config, dry_run=True, fetcher=_no_calendars
    )

    assert report.would_squash_into == head
    assert report.commit_id is None
    assert report.squashed is False


def test_dry_run_reports_no_fold_when_head_is_not_a_target(tmp_path: Path) -> None:
    repo, _sources = _repo_with_pending_proofs(tmp_path)  # HEAD is a proof commit

    report = upgrade_proofs(
        cwd=repo, config=_squashing_config(), dry_run=True, fetcher=_no_calendars
    )

    assert any(r.state is UpgradeState.WOULD_UPGRADE for r in report.results)
    assert report.would_squash_into is None


def test_dry_run_does_not_look_for_a_target_when_squashing_is_off(
    tmp_path: Path,
) -> None:
    repo, (first, _second) = _repo_with_pending_proofs(tmp_path)
    _upgrade_one(repo, first, config=_fresh_config())

    report = upgrade_proofs(
        cwd=repo, config=_fresh_config(), dry_run=True, fetcher=_no_calendars
    )

    assert report.would_squash_into is None


def test_cli_dry_run_names_the_fold_decision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, (first, _second) = _repo_with_pending_proofs(tmp_path)
    _git(["config", "ots.squashUpgradeCommits", "true"], cwd=repo)
    _git(["config", "ots.fetchBeforeRun", "false"], cwd=repo)
    import git_ots.upgrade as upgrade_module

    monkeypatch.setattr(upgrade_module, "fetch_calendar_timestamp", _no_calendars)

    assert main(["upgrade", "--dry-run"], cwd=str(repo)) == 0
    assert "the commit at HEAD is not a squash target" in capsys.readouterr().out

    _upgrade_one(repo, first, config=_squashing_config())
    head = _git(["rev-parse", "HEAD"], cwd=repo).strip()

    assert main(["upgrade", "--dry-run"], cwd=str(repo)) == 0
    assert (
        f"Would fold upgraded proofs into the upgrade commit at HEAD, {head[:12]}."
        in (capsys.readouterr().out)
    )


def test_message_mismatch_is_explained_below_warning_level(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A commit-msg hook that reshapes messages would otherwise leave the
    setting silently inert forever; --verbose has to be able to say why."""
    repo, (first, _second) = _repo_with_pending_proofs(tmp_path)
    paths = [_anchor_on_disk(repo, first)]
    stage_paths(cwd=repo, paths=paths)
    create_upgrade_commit(
        cwd=repo,
        source_commit_ids=[first],
        proof_directory=PROOF_DIRECTORY,
        paths=paths,
    )
    subprocess.run(
        [
            "git",
            "commit",
            "-q",
            "--amend",
            "--no-gpg-sign",
            "--no-edit",
            "--trailer",
            "Change-Id: I0123",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    with caplog.at_level(logging.INFO, logger="git_ots"):
        target = find_squashable_upgrade_commit(
            cwd=repo, proof_directory=PROOF_DIRECTORY
        )

    assert target is None
    records = [r for r in caplog.records if "commit-msg hook" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO


def test_require_clean_worktree_is_checked_before_a_squash(tmp_path: Path) -> None:
    """The gate runs before anything is written, squash or not."""
    repo, (first, second) = _repo_with_pending_proofs(tmp_path)
    base = _squashing_config()
    _upgrade_one(repo, first, config=base)
    head_before = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    (repo / "a.txt").write_text("unrelated edit\n")
    strict = replace(base, git=replace(base.git, require_clean_worktree=True))

    with pytest.raises(InvalidRepositoryStateError):
        _upgrade_one(repo, second, config=strict)

    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == head_before


# --- concurrency and signature safety ---------------------------------------


def test_amend_refuses_when_head_moved_after_approval(tmp_path: Path) -> None:
    """`git commit --amend` names no commit; it rewrites whatever HEAD is now.

    The repository lock excludes other `git-ots` invocations, not an ordinary
    `git commit` in another terminal. Approving one object and rewriting
    another is the data-loss shape this guard exists to prevent.
    """
    repo, (first, second) = _repo_with_pending_proofs(tmp_path)
    paths = [_anchor_on_disk(repo, first)]
    stage_paths(cwd=repo, paths=paths)
    create_upgrade_commit(
        cwd=repo,
        source_commit_ids=[first],
        proof_directory=PROOF_DIRECTORY,
        paths=paths,
    )
    approved = find_squashable_upgrade_commit(cwd=repo, proof_directory=PROOF_DIRECTORY)
    assert approved is not None

    # Somebody else commits between the approval and the rewrite.
    interloper = _commit(repo, paths=["urgent.txt"], message="Urgent fix\n")

    with pytest.raises(InvalidRepositoryStateError, match="HEAD moved"):
        amend_upgrade_commit(
            cwd=repo,
            expected_head=approved.commit_id,
            previous_source_commit_ids=approved.source_commit_ids,
            source_commit_ids=[second],
            proof_directory=PROOF_DIRECTORY,
            paths=[_anchor_on_disk(repo, second)],
        )

    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == interloper
    assert _commit_message(repo).startswith("Urgent fix")


def test_amend_rechecks_head_after_index_lock_contention(tmp_path: Path) -> None:
    repo, (first, second) = _repo_with_pending_proofs(tmp_path)
    _upgrade_one(repo, first, config=_squashing_config())
    approved = find_squashable_upgrade_commit(cwd=repo, proof_directory=PROOF_DIRECTORY)
    assert approved is not None
    path = _anchor_on_disk(repo, second)
    interloper = None
    attempts = 0

    def contended_runner(argv, *, cwd, stdin=None):
        nonlocal interloper, attempts
        if "--amend" in argv:
            attempts += 1
            if attempts == 1:
                # Another writer wins the index and advances HEAD before
                # our failed attempt is retried.
                interloper = _commit(repo, paths=["urgent.txt"], message="Urgent fix\n")
                return (
                    128,
                    "",
                    (
                        "fatal: Unable to create '.git/index.lock': File exists.\n"
                        "Another git process seems to be running in this repository."
                    ),
                )
        result = subprocess.run(
            argv, cwd=cwd, input=stdin, capture_output=True, check=False
        )
        return result.returncode, result.stdout.decode(), result.stderr.decode()

    with pytest.raises(InvalidRepositoryStateError, match="HEAD moved"):
        amend_upgrade_commit(
            cwd=repo,
            expected_head=approved.commit_id,
            previous_source_commit_ids=approved.source_commit_ids,
            source_commit_ids=[second],
            proof_directory=PROOF_DIRECTORY,
            paths=[path],
            process_runner=contended_runner,
            index_lock_retry_window=5,
        )

    assert attempts == 1
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == interloper
    assert _commit_message(repo).startswith("Urgent fix")
    assert _git(["diff", "--name-only"], cwd=repo).strip() == path


def test_squash_target_is_pinned_to_an_object_id_not_the_head_name(
    tmp_path: Path,
) -> None:
    repo, (first, _second) = _repo_with_pending_proofs(tmp_path)
    _upgrade_one(repo, first, config=_squashing_config())

    target = find_squashable_upgrade_commit(cwd=repo, proof_directory=PROOF_DIRECTORY)

    assert target is not None
    assert target.commit_id == _git(["rev-parse", "HEAD"], cwd=repo).strip()
    assert len(target.commit_id) == 40


def test_signed_upgrade_commit_is_not_squashed_into_when_the_fold_is_unsigned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Amending replaces the object, so a signature is recreated or lost.

    With neither `[git] signing = required` nor ambient `commit.gpgsign`, the
    fold would quietly hand back an unsigned commit where a signed one stood.
    """
    log = tmp_path / "signer.log"
    monkeypatch.setenv("SIGNER_LOG", str(log))
    repo, (first, second) = _repo_with_pending_proofs(
        tmp_path, signer=_FAKE_SIGNER, signer_log=log
    )
    signing = _squashing_config()
    signing = replace(signing, git=replace(signing.git, signing="required"))
    _upgrade_one(repo, first, config=signing)
    assert "-----BEGIN PGP SIGNATURE-----" in _git(
        ["cat-file", "commit", "HEAD"], cwd=repo
    )
    before = _commit_count(repo)

    # Signing turned back off: this tool passes no flag and nothing ambient
    # is set, so an amend would drop the signature.
    with caplog.at_level(logging.WARNING, logger="git_ots.git"):
        report = _upgrade_one(repo, second, config=_squashing_config())

    assert report.squashed is False
    assert _commit_count(repo) == before + 1
    assert "signed" in caplog.text
    assert "-----BEGIN PGP SIGNATURE-----" in _git(
        ["cat-file", "commit", "HEAD~1"], cwd=repo
    )


def test_signed_upgrade_commit_is_squashed_when_ambient_signing_recreates_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`inherit` passes no flag, but ambient `commit.gpgsign` still signs, so
    the signature is recreated and there is nothing to protect."""
    log = tmp_path / "signer.log"
    monkeypatch.setenv("SIGNER_LOG", str(log))
    repo, (first, second) = _repo_with_pending_proofs(
        tmp_path, signer=_FAKE_SIGNER, signer_log=log
    )
    _git(["config", "commit.gpgsign", "true"], cwd=repo)
    config = _squashing_config()
    _upgrade_one(repo, first, config=config)
    after_first = _commit_count(repo)

    report = _upgrade_one(repo, second, config=config)

    assert report.squashed is True
    assert _commit_count(repo) == after_first
    assert "-----BEGIN PGP SIGNATURE-----" in _git(
        ["cat-file", "commit", "HEAD"], cwd=repo
    )


def test_unsigned_upgrade_commit_is_unaffected_by_the_signature_guard(
    tmp_path: Path,
) -> None:
    repo, (first, second) = _repo_with_pending_proofs(tmp_path)
    config = _squashing_config()
    _upgrade_one(repo, first, config=config)
    after_first = _commit_count(repo)

    report = _upgrade_one(repo, second, config=config)

    assert report.squashed is True
    assert _commit_count(repo) == after_first
