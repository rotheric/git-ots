"""Tests for the ``git-ots upgrade`` subcommand.

Upgrading is the one operation that both contacts the network and rewrites
stored proofs, so these tests pin three things: the adapter never lets the
client touch the repository directly, a proof is only replaced when the new
bytes still bind to the same source commit, and the resulting commit carries
the generated trailer so proof maintenance never triggers a new timestamp.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from git_ots.cli import main
from git_ots.config import Config
from git_ots.git import (
    GitCommandError,
    InvalidRepositoryStateError,
    filter_pending_meaningful_commits,
)
from git_ots.timestamp import (
    OpenTimestampsCli,
    SubprocessResult,
    UpgradeAttempt,
    UpgradeError,
    build_payload,
)
from git_ots.upgrade import UpgradeState, upgrade_proofs
from tests.test_anchors import CATALLAXY, FIXTURE_SOURCE_COMMIT
from tests.test_servers import _repo_with_fixture_proof
from tests.test_timestamp import _make_fake_detached_proof
from tests.test_verify import (
    _commit,
    _fresh_config,
    _git,
    _init_repo,
    _make_bitcoin_attested_proof,
    _write_config,
    _write_proof_and_manifest,
)


def _no_calendars(url: str, commitment: bytes) -> tuple[bytes | None, str]:
    """A fetcher that reaches no calendar, so tests never touch the network."""
    return None, "not contacted"


class _RecordingFetcher:
    """Fetcher returning a queued body per calendar URL, recording requests."""

    def __init__(self, bodies: dict[str, bytes]) -> None:
        self._bodies = bodies
        self.calls: list[tuple[str, bytes]] = []

    def __call__(self, url: str, commitment: bytes) -> tuple[bytes | None, str]:
        self.calls.append((url, commitment))
        body = self._bodies.get(url)
        if body is None:
            return None, "not yet confirmed"
        return body, "ok"


class _FakeUpgradeClient:
    """Upgrade client that returns queued outcomes and records its inputs."""

    def __init__(self, outcomes: list[UpgradeAttempt]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[bytes] = []

    def upgrade(self, proof_bytes: bytes, payload: bytes) -> UpgradeAttempt:
        self.calls.append(proof_bytes)
        return self._outcomes.pop(0)


def _rewriting_runner(
    new_bytes: bytes | None, *, exit_code: int = 0, stderr: bytes = b""
):
    """Return a runner that replaces the target file the way ``ots`` would."""

    def runner(argv: list[str], *, stdin: bytes) -> SubprocessResult:
        if new_bytes is not None:
            target = Path(argv[2])
            target.with_suffix(".ots.bak").write_bytes(target.read_bytes())
            target.write_bytes(new_bytes)
        return SubprocessResult(exit_code=exit_code, stdout=b"", stderr=stderr)

    return runner


# --- adapter contract -------------------------------------------------------


def test_upgrade_returns_none_when_client_changes_nothing() -> None:
    payload = build_payload("sha1", "a" * 40)
    proof = _make_fake_detached_proof(payload=payload)
    client = OpenTimestampsCli(
        command="ots",
        runner=_rewriting_runner(
            None, exit_code=1, stderr=b"Failed! Timestamp not complete\n"
        ),
    )

    attempt = client.upgrade(proof, payload)

    assert attempt.upgraded is None
    # A non-zero exit is the ordinary pending case, so it must not raise -- but
    # the diagnostic has to survive, otherwise a network outage is
    # indistinguishable from a calendar that simply is not ready.
    assert attempt.detail == "Failed! Timestamp not complete"


def test_upgrade_returns_new_bytes_when_client_completes_the_proof() -> None:
    payload = build_payload("sha1", "a" * 40)
    proof = _make_fake_detached_proof(payload=payload)
    completed = _make_bitcoin_attested_proof(payload)
    client = OpenTimestampsCli(command="ots", runner=_rewriting_runner(completed))

    attempt = client.upgrade(proof, payload)

    assert attempt.upgraded == completed


def test_upgrade_operates_on_a_copy_outside_the_repository(tmp_path: Path) -> None:
    payload = build_payload("sha1", "a" * 40)
    proof = _make_fake_detached_proof(payload=payload)
    proof_dir = tmp_path / ".opentimestamps"
    proof_dir.mkdir()
    stored = proof_dir / f"{'a' * 40}.ots"
    stored.write_bytes(proof)

    seen: list[str] = []

    def runner(argv: list[str], *, stdin: bytes) -> SubprocessResult:
        seen.append(argv[2])
        return SubprocessResult(exit_code=1, stdout=b"", stderr=b"")

    OpenTimestampsCli(command="ots", runner=runner).upgrade(proof, payload)

    assert seen and Path(seen[0]).parent != proof_dir
    # `ots upgrade` leaves a .bak beside its target; nothing may appear next to
    # the stored proof.
    assert sorted(p.name for p in proof_dir.iterdir()) == [f"{'a' * 40}.ots"]


def test_upgrade_rejects_bytes_that_no_longer_bind_to_the_source() -> None:
    payload = build_payload("sha1", "a" * 40)
    proof = _make_fake_detached_proof(payload=payload)
    other = _make_bitcoin_attested_proof(build_payload("sha1", "b" * 40))
    client = OpenTimestampsCli(command="ots", runner=_rewriting_runner(other))

    with pytest.raises(UpgradeError, match="no longer binds"):
        client.upgrade(proof, payload)


def test_upgrade_reports_a_missing_client_as_a_submission_failure() -> None:
    payload = build_payload("sha1", "a" * 40)
    proof = _make_fake_detached_proof(payload=payload)

    def runner(argv: list[str], *, stdin: bytes) -> SubprocessResult:
        raise OSError(2, "No such file or directory")

    client = OpenTimestampsCli(command="nope", runner=runner)

    with pytest.raises(UpgradeError) as excinfo:
        client.upgrade(proof, payload)
    assert excinfo.value.exit_code == 127


# --- upgrade pass -----------------------------------------------------------


def _repo_with_pending_proof(tmp_path: Path) -> tuple[Path, str, bytes]:
    repo = tmp_path / "repo"
    _init_repo(repo)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")
    payload = build_payload("sha1", source_id)
    pending = _make_fake_detached_proof(payload=payload)
    _write_proof_and_manifest(repo, source_id, pending)
    _git(["add", ".opentimestamps"], cwd=repo)
    subprocess.run(
        [
            "git",
            "commit",
            "-q",
            "-m",
            (
                f"Store OpenTimestamps proof for {source_id[:12]}\n\n"
                f"OpenTimestamps-Generated: true\n"
                f"OpenTimestamps-Source: {source_id}\n"
            ),
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    _write_config(repo, _fresh_config())
    return repo, source_id, payload


def test_pending_proof_is_upgraded_persisted_and_committed(tmp_path: Path) -> None:
    repo, source_id, payload = _repo_with_pending_proof(tmp_path)
    completed = _make_bitcoin_attested_proof(payload)
    client = _FakeUpgradeClient([UpgradeAttempt(upgraded=completed, detail="")])

    report = upgrade_proofs(
        cwd=repo, config=_fresh_config(), client=client, fetcher=_no_calendars
    )

    assert [r.state for r in report.results] == [UpgradeState.UPGRADED]
    assert (repo / ".opentimestamps" / f"{source_id}.ots").read_bytes() == completed
    assert report.commit_id is not None
    message = _git(["log", "-1", "--format=%B"], cwd=repo)
    assert message.startswith(f"Upgrade OpenTimestamps proof for {source_id[:12]}")
    assert "OpenTimestamps-Generated: true" in message
    assert f"OpenTimestamps-Upgraded: {source_id}" in message
    # An upgrade refreshes a proof an earlier commit already claimed; reusing
    # the source trailer would leave two commits claiming one source.
    assert "OpenTimestamps-Source:" not in message


def test_next_upgrade_commits_proof_left_dirty_by_failed_upgrade_commit(
    tmp_path: Path,
) -> None:
    """A persisted upgrade remains resumable after its Git commit is vetoed."""
    repo, source_id, payload = _repo_with_pending_proof(tmp_path)
    completed = _make_bitcoin_attested_proof(payload)
    proof_path = repo / ".opentimestamps" / f"{source_id}.ots"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    with pytest.raises(GitCommandError):
        upgrade_proofs(
            cwd=repo,
            config=_fresh_config(),
            client=_FakeUpgradeClient([UpgradeAttempt(upgraded=completed, detail="")]),
            fetcher=_no_calendars,
        )

    assert proof_path.read_bytes() == completed
    assert _git(
        ["status", "--short", "--", str(proof_path.relative_to(repo))], cwd=repo
    )

    hook.unlink()
    retry_client = _FakeUpgradeClient([])
    report = upgrade_proofs(
        cwd=repo,
        config=_fresh_config(),
        client=retry_client,
        fetcher=_no_calendars,
    )

    assert report.commit_id is not None
    assert report.results[0].state is UpgradeState.UPGRADED
    assert "recovered an additive proof upgrade" in report.results[0].reason
    assert retry_client.calls == []
    assert (
        _git(["status", "--short", "--", str(proof_path.relative_to(repo))], cwd=repo)
        == ""
    )
    message = _git(["log", "-1", "--format=%B"], cwd=repo)
    assert f"OpenTimestamps-Upgraded: {source_id}" in message


def test_dirty_proof_that_drops_a_committed_anchor_is_not_adopted(
    tmp_path: Path,
) -> None:
    """Source binding alone cannot authorize committing replacement bytes."""
    repo, source_id, payload = _repo_with_pending_proof(tmp_path)
    first = _make_bitcoin_attested_proof(payload)
    proof_path = repo / ".opentimestamps" / f"{source_id}.ots"
    proof_path.write_bytes(first)
    _git(["add", str(proof_path.relative_to(repo))], cwd=repo)
    subprocess.run(
        ["git", "commit", "-q", "-m", "Record first anchor"],
        cwd=repo,
        check=True,
    )

    # Changing the encoded block height yields another bound and structurally
    # valid proof, but it removes the already-committed Bitcoin attestation.
    replacement = first.replace(b"\x80\xea\x30", b"\x81\xea\x30")
    proof_path.write_bytes(replacement)

    report = upgrade_proofs(
        cwd=repo,
        config=_fresh_config(),
        client=_FakeUpgradeClient([]),
        fetcher=_no_calendars,
    )

    assert report.commit_id is None
    assert report.results[0].state is UpgradeState.SKIPPED
    assert "not an additive upgrade" in report.results[0].reason
    assert proof_path.read_bytes() == replacement


def test_upgrade_commit_is_not_a_pending_meaningful_commit(tmp_path: Path) -> None:
    """The loop guard: proof maintenance must not itself need timestamping."""
    repo, source_id, payload = _repo_with_pending_proof(tmp_path)
    client = _FakeUpgradeClient(
        [UpgradeAttempt(upgraded=_make_bitcoin_attested_proof(payload), detail="")]
    )
    report = upgrade_proofs(
        cwd=repo, config=_fresh_config(), client=client, fetcher=_no_calendars
    )
    assert report.commit_id is not None

    pending = filter_pending_meaningful_commits(
        cwd=repo,
        ref="HEAD",
        baseline=source_id,
        proof_directory=".opentimestamps",
    )

    assert pending == ()


def test_already_complete_proof_is_left_alone_and_never_sent_to_the_client(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")
    completed = _make_bitcoin_attested_proof(build_payload("sha1", source_id))
    _write_proof_and_manifest(repo, source_id, completed)
    client = _FakeUpgradeClient([])

    report = upgrade_proofs(
        cwd=repo, config=_fresh_config(), client=client, fetcher=_no_calendars
    )

    assert [r.state for r in report.results] == [UpgradeState.ALREADY_COMPLETE]
    assert client.calls == []
    assert (repo / ".opentimestamps" / f"{source_id}.ots").read_bytes() == completed


def test_still_pending_proof_is_reported_with_the_client_diagnostic(
    tmp_path: Path,
) -> None:
    repo, source_id, _payload = _repo_with_pending_proof(tmp_path)
    before = (repo / ".opentimestamps" / f"{source_id}.ots").read_bytes()
    client = _FakeUpgradeClient(
        [UpgradeAttempt(upgraded=None, detail="waiting for 6 confirmations")]
    )

    report = upgrade_proofs(
        cwd=repo, config=_fresh_config(), client=client, fetcher=_no_calendars
    )

    assert [r.state for r in report.results] == [UpgradeState.STILL_PENDING]
    assert "waiting for 6 confirmations" in report.results[0].reason
    assert (repo / ".opentimestamps" / f"{source_id}.ots").read_bytes() == before
    assert report.commit_id is None


def test_dry_run_touches_nothing_and_never_calls_the_client(tmp_path: Path) -> None:
    repo, source_id, _payload = _repo_with_pending_proof(tmp_path)
    head_before = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    before = (repo / ".opentimestamps" / f"{source_id}.ots").read_bytes()
    client = _FakeUpgradeClient([])

    report = upgrade_proofs(
        cwd=repo,
        config=_fresh_config(),
        dry_run=True,
        client=client,
        fetcher=_no_calendars,
    )

    assert [r.state for r in report.results] == [UpgradeState.WOULD_UPGRADE]
    assert client.calls == []
    assert report.commit_id is None
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == head_before
    assert (repo / ".opentimestamps" / f"{source_id}.ots").read_bytes() == before


def test_malformed_manifest_is_skipped_and_the_proof_is_untouched(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")
    pending = _make_fake_detached_proof(payload=build_payload("sha1", source_id))
    _write_proof_and_manifest(repo, source_id, pending)
    manifest_path = repo / ".opentimestamps" / f"{source_id}.json"
    manifest_path.write_text("{not json", encoding="utf-8")
    client = _FakeUpgradeClient([])

    report = upgrade_proofs(
        cwd=repo, config=_fresh_config(), client=client, fetcher=_no_calendars
    )

    assert [r.state for r in report.results] == [UpgradeState.SKIPPED]
    assert client.calls == []
    assert (repo / ".opentimestamps" / f"{source_id}.ots").read_bytes() == pending
    assert manifest_path.read_text(encoding="utf-8") == "{not json"


def test_proof_directory_absent_reports_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n")

    report = upgrade_proofs(cwd=repo, config=_fresh_config(), fetcher=_no_calendars)

    assert report.results == ()
    assert report.commit_id is None


# --- CLI --------------------------------------------------------------------


def test_cli_upgrade_dry_run_prints_states_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, source_id, _payload = _repo_with_pending_proof(tmp_path)

    exit_code = main(["upgrade", "--dry-run"], cwd=repo)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert f".opentimestamps/{source_id}.ots: would-upgrade" in captured.out
    assert "1 still pending" in captured.out


def test_cli_upgrade_reports_skipped_proofs_as_exit_five(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")
    _write_proof_and_manifest(
        repo,
        source_id,
        _make_fake_detached_proof(payload=build_payload("sha1", source_id)),
    )
    (repo / ".opentimestamps" / f"{source_id}.json").unlink()
    _write_config(repo, _fresh_config())

    exit_code = main(["upgrade", "--dry-run"], cwd=repo)

    captured = capsys.readouterr()
    assert exit_code == 5
    assert "skipped" in captured.out


def test_cli_upgrade_on_empty_proof_directory_reports_no_proofs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n")
    _write_config(repo, _fresh_config())

    exit_code = main(["upgrade"], cwd=repo)

    assert exit_code == 0
    assert "No stored proofs found." in capsys.readouterr().out


def test_status_and_upgrade_name_the_same_outstanding_calendars(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The two commands must agree about what is still outstanding.

    An anchored proof keeps the pending attestations of calendars that never
    delivered, and upgrade now asks each of them, so status calling them
    awaited is a promise the tool actually keeps. This pins the two views
    together: whatever status lists as awaited is exactly what upgrade goes
    and asks for.
    """
    # The fixture binds to its own source commit, so it must be stored under
    # that id or it would be rejected as unbound before either command has an
    # opinion about it.
    repo = _repo_with_fixture_proof(tmp_path)

    main(["status"], cwd=repo)
    status_out = capsys.readouterr().out
    fetcher = _RecordingFetcher({})
    upgrade_proofs(
        cwd=repo,
        config=_fresh_config(),
        client=_FakeUpgradeClient([]),
        fetcher=fetcher,
    )

    awaited = {
        line.split("awaiting ", 1)[1].strip()
        for line in status_out.splitlines()
        if "awaiting " in line
    }
    assert awaited
    assert awaited == {url for url, _commitment in fetcher.calls}


def test_already_complete_proof_reports_realized_redundancy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ratio is the point of reporting an already-complete proof at all.

    Without it "already-complete" hides that half the calendars this proof was
    submitted to never anchored it, which is exactly what a reader wants to
    know about a timestamp's resilience.
    """
    repo = _repo_with_fixture_proof(tmp_path)

    report = upgrade_proofs(
        cwd=repo,
        config=_fresh_config(),
        client=_FakeUpgradeClient([]),
        fetcher=_no_calendars,
    )

    assert report.results[0].anchored_calendars == 2
    assert report.results[0].promised_calendars == 4

    assert report.results[0].state is UpgradeState.ALREADY_COMPLETE


def test_pending_proof_reports_zero_of_its_promised_calendars(
    tmp_path: Path,
) -> None:
    repo, _source_id, _payload = _repo_with_pending_proof(tmp_path)

    report = upgrade_proofs(
        cwd=repo,
        config=_fresh_config(),
        client=_FakeUpgradeClient([UpgradeAttempt(upgraded=None, detail="")]),
        fetcher=_no_calendars,
    )

    assert report.results[0].anchored_calendars == 0
    assert report.results[0].promised_calendars == 1


# --- collecting what calendars published after the client stopped asking ----


def _calendar_response_for(proof_bytes: bytes, url: str, attestation) -> bytes:
    """Build the response a calendar would give for its own commitment."""
    from git_ots.anchors import collectible_calendars
    from git_ots.proof_tree import Node, parse_proof

    # Guard the premise: a response only means anything if the proof actually
    # still has an uncollected promise from this calendar.
    assert any(c.url == url for c in collectible_calendars(parse_proof(proof_bytes))), (
        f"{url} is not outstanding in this proof"
    )
    return Node(items=[attestation]).serialize()


def test_collects_an_attestation_the_client_never_asked_for(
    tmp_path: Path,
) -> None:
    """The point of the whole collection pass.

    The OpenTimestamps client stops upgrading at a proof's first Bitcoin
    attestation, so a calendar that confirmed later is never asked again. Its
    attestation exists and is served on request; leaving it out makes the
    stored proof claim less than actually happened.
    """
    from git_ots.anchors import describe_anchors
    from git_ots.proof_tree import Attestation

    repo = _repo_with_fixture_proof(tmp_path)
    proof_path = repo / ".opentimestamps" / f"{FIXTURE_SOURCE_COMMIT}.ots"
    before = proof_path.read_bytes()
    assert len(describe_anchors(before).anchors) == 2

    late = Attestation(
        tag=bytes.fromhex("0588960d73d71901"), payload=bytes([2, 0xDE, 0x07])
    )
    fetcher = _RecordingFetcher(
        {CATALLAXY: _calendar_response_for(before, CATALLAXY, late)}
    )

    report = upgrade_proofs(
        cwd=repo,
        config=_fresh_config(),
        client=_FakeUpgradeClient([]),
        fetcher=fetcher,
    )

    after = proof_path.read_bytes()
    assert report.results[0].state is UpgradeState.UPGRADED
    assert CATALLAXY in report.results[0].reason
    assert len(describe_anchors(after).anchors) == 3
    assert report.results[0].anchored_calendars == 3


def test_collection_asks_every_calendar_with_its_own_commitment(
    tmp_path: Path,
) -> None:
    """Each calendar files under the commitment it was given, not a shared one."""
    from git_ots.anchors import collectible_calendars
    from git_ots.proof_tree import parse_proof

    repo = _repo_with_fixture_proof(tmp_path)
    proof_bytes = (
        repo / ".opentimestamps" / f"{FIXTURE_SOURCE_COMMIT}.ots"
    ).read_bytes()
    expected = {
        c.url: c.commitment for c in collectible_calendars(parse_proof(proof_bytes))
    }
    fetcher = _RecordingFetcher({})

    upgrade_proofs(
        cwd=repo,
        config=_fresh_config(),
        client=_FakeUpgradeClient([]),
        fetcher=fetcher,
    )

    assert dict(fetcher.calls) == expected
    assert len(set(expected.values())) == len(expected)


def test_collection_is_idempotent(tmp_path: Path) -> None:
    from git_ots.proof_tree import Attestation

    repo = _repo_with_fixture_proof(tmp_path)
    proof_path = repo / ".opentimestamps" / f"{FIXTURE_SOURCE_COMMIT}.ots"
    late = Attestation(
        tag=bytes.fromhex("0588960d73d71901"), payload=bytes([2, 0xDE, 0x07])
    )
    response = _calendar_response_for(proof_path.read_bytes(), CATALLAXY, late)

    def run():
        return upgrade_proofs(
            cwd=repo,
            config=_fresh_config(),
            client=_FakeUpgradeClient([]),
            fetcher=_RecordingFetcher({CATALLAXY: response}),
        )

    run()
    first = proof_path.read_bytes()
    second_report = run()

    assert proof_path.read_bytes() == first
    assert second_report.results[0].state is UpgradeState.ALREADY_COMPLETE
    assert second_report.commit_id is None


def test_collection_leaves_the_proof_alone_when_nothing_is_available(
    tmp_path: Path,
) -> None:
    repo = _repo_with_fixture_proof(tmp_path)
    proof_path = repo / ".opentimestamps" / f"{FIXTURE_SOURCE_COMMIT}.ots"
    before = proof_path.read_bytes()

    report = upgrade_proofs(
        cwd=repo,
        config=_fresh_config(),
        client=_FakeUpgradeClient([]),
        fetcher=_RecordingFetcher({}),
    )

    assert proof_path.read_bytes() == before
    assert report.results[0].state is UpgradeState.ALREADY_COMPLETE
    assert report.commit_id is None


def test_an_unusable_calendar_response_never_reaches_the_proof(
    tmp_path: Path,
) -> None:
    repo = _repo_with_fixture_proof(tmp_path)
    proof_path = repo / ".opentimestamps" / f"{FIXTURE_SOURCE_COMMIT}.ots"
    before = proof_path.read_bytes()

    report = upgrade_proofs(
        cwd=repo,
        config=_fresh_config(),
        client=_FakeUpgradeClient([]),
        fetcher=_RecordingFetcher({CATALLAXY: b"garbage, not a timestamp"}),
    )

    assert proof_path.read_bytes() == before
    assert report.results[0].state is UpgradeState.ALREADY_COMPLETE


def test_dry_run_names_the_calendars_it_would_ask(tmp_path: Path) -> None:
    repo = _repo_with_fixture_proof(tmp_path)
    fetcher = _RecordingFetcher({})

    report = upgrade_proofs(
        cwd=repo,
        config=_fresh_config(),
        dry_run=True,
        client=_FakeUpgradeClient([]),
        fetcher=fetcher,
    )

    assert report.results[0].state is UpgradeState.WOULD_UPGRADE
    assert CATALLAXY in report.results[0].reason
    assert fetcher.calls == []


def test_upgraded_proof_still_validates_against_its_source(tmp_path: Path) -> None:
    """An upgrade must not break the binding the validate command relies on."""
    repo, source_id, payload = _repo_with_pending_proof(tmp_path)
    client = _FakeUpgradeClient(
        [UpgradeAttempt(upgraded=_make_bitcoin_attested_proof(payload), detail="")]
    )
    upgrade_proofs(
        cwd=repo, config=_fresh_config(), client=client, fetcher=_no_calendars
    )

    exit_code = main(["validate"], cwd=repo)

    assert exit_code == 0
    manifest = json.loads(
        (repo / ".opentimestamps" / f"{source_id}.json").read_text(encoding="utf-8")
    )
    # The manifest records the submission, not the upgrade, so it must not have
    # been rewritten.
    assert manifest["submitted_at"] == "2026-08-09T00:00:03Z"


# --- report row contract ----------------------------------------------------
#
# A mutation run over upgrade.py showed every field of ``UpgradeResult``
# except ``state`` to be unasserted: proof_path, source_commit_id, reason and
# the two calendar counters could all be replaced by ``None`` with the suite
# still green. `git-ots upgrade` prints one line per stored proof and that
# line is the whole output of the command, so a row that names no proof and
# gives no reason is a command that reported nothing.
#
# No AC covers the upgrade report's row contract; filed as a spec-gap finding.


def _assert_row_is_self_describing(result, *, proof_name: str) -> None:
    """Every row must say which proof it is about and why it ended up here."""
    assert result.proof_path == f".opentimestamps/{proof_name}"
    assert isinstance(result.state, UpgradeState)
    assert result.reason, f"{result.state} row carries no reason"
    assert result.reason != "None"
    assert isinstance(result.anchored_calendars, int)
    assert isinstance(result.promised_calendars, int)
    assert 0 <= result.anchored_calendars <= result.promised_calendars


def test_upgraded_row_names_its_proof_and_source_commit(tmp_path: Path) -> None:
    repo, source_id, payload = _repo_with_pending_proof(tmp_path)
    client = _FakeUpgradeClient(
        [UpgradeAttempt(upgraded=_make_bitcoin_attested_proof(payload), detail="")]
    )

    report = upgrade_proofs(
        cwd=repo, config=_fresh_config(), client=client, fetcher=_no_calendars
    )

    (result,) = report.results
    _assert_row_is_self_describing(result, proof_name=f"{source_id}.ots")
    assert result.state is UpgradeState.UPGRADED
    assert result.source_commit_id == source_id


def test_still_pending_row_names_its_proof_and_source_commit(tmp_path: Path) -> None:
    repo, source_id, _payload = _repo_with_pending_proof(tmp_path)
    client = _FakeUpgradeClient(
        [UpgradeAttempt(upgraded=None, detail="waiting for 6 confirmations")]
    )

    report = upgrade_proofs(
        cwd=repo, config=_fresh_config(), client=client, fetcher=_no_calendars
    )

    (result,) = report.results
    _assert_row_is_self_describing(result, proof_name=f"{source_id}.ots")
    assert result.state is UpgradeState.STILL_PENDING
    assert result.source_commit_id == source_id


def test_already_complete_row_names_its_proof_and_source_commit(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")
    completed = _make_bitcoin_attested_proof(build_payload("sha1", source_id))
    _write_proof_and_manifest(repo, source_id, completed)

    report = upgrade_proofs(
        cwd=repo,
        config=_fresh_config(),
        client=_FakeUpgradeClient([]),
        fetcher=_no_calendars,
    )

    (result,) = report.results
    _assert_row_is_self_describing(result, proof_name=f"{source_id}.ots")
    assert result.state is UpgradeState.ALREADY_COMPLETE
    assert result.source_commit_id == source_id


def test_dry_run_row_names_its_proof_and_source_commit(tmp_path: Path) -> None:
    repo, source_id, _payload = _repo_with_pending_proof(tmp_path)

    report = upgrade_proofs(
        cwd=repo,
        config=_fresh_config(),
        dry_run=True,
        client=_FakeUpgradeClient([]),
        fetcher=_no_calendars,
    )

    (result,) = report.results
    _assert_row_is_self_describing(result, proof_name=f"{source_id}.ots")
    assert result.state is UpgradeState.WOULD_UPGRADE
    assert result.source_commit_id == source_id


def test_skipped_row_names_the_proof_it_could_not_read(tmp_path: Path) -> None:
    """A proof whose manifest is unreadable is the one row that cannot name a
    source commit -- which makes naming the *file* the only way an operator
    can find what to fix."""
    repo, source_id, _payload = _repo_with_pending_proof(tmp_path)
    manifest_path = repo / ".opentimestamps" / f"{source_id}.json"
    manifest_path.write_text("{not json", encoding="utf-8")

    report = upgrade_proofs(
        cwd=repo,
        config=_fresh_config(),
        client=_FakeUpgradeClient([]),
        fetcher=_no_calendars,
    )

    (result,) = report.results
    _assert_row_is_self_describing(result, proof_name=f"{source_id}.ots")
    assert result.state is UpgradeState.SKIPPED
    assert "manifest" in result.reason


def test_an_already_complete_proof_does_not_end_the_pass(tmp_path: Path) -> None:
    """Proofs are scanned in directory order and a repository accumulates
    complete ones over time. If the first complete proof stopped the pass,
    every pending proof behind it would silently never be upgraded -- the
    failure mode is invisible because the command still exits zero.
    """
    repo, pending_id, pending_payload = _repo_with_pending_proof(tmp_path)
    other_id = _commit(repo, paths=["b.txt"], message="B\n")
    _write_proof_and_manifest(
        repo, other_id, _make_bitcoin_attested_proof(build_payload("sha1", other_id))
    )
    client = _FakeUpgradeClient(
        [
            UpgradeAttempt(
                upgraded=_make_bitcoin_attested_proof(pending_payload), detail=""
            )
        ]
    )

    report = upgrade_proofs(
        cwd=repo, config=_fresh_config(), client=client, fetcher=_no_calendars
    )

    by_commit = {r.source_commit_id: r.state for r in report.results}
    assert by_commit == {
        pending_id: UpgradeState.UPGRADED,
        other_id: UpgradeState.ALREADY_COMPLETE,
    }


# --- pre-flight repository checks -------------------------------------------
#
# Every test above runs with ``require_clean_worktree = false``, which is what
# the mutation run surfaced: the guard, its polarity, and the condition that
# decides whether it applies at all were entirely unexercised for ``upgrade``.


def _requiring_clean_worktree(*, commit: bool = True) -> Config:
    base = _fresh_config()
    return replace(
        base,
        git=replace(base.git, require_clean_worktree=True),
        proof=replace(base.proof, commit=commit),
    )


def test_upgrade_refuses_to_start_on_a_dirty_worktree_when_configured_to(
    tmp_path: Path,
) -> None:
    """The upgrade pass ends in a commit of its own, so it must not start on
    top of an operator's half-finished work. The refusal has to name the
    setting that produced it, or the operator has no way to opt out."""
    repo, _source_id, _payload = _repo_with_pending_proof(tmp_path)
    (repo / "a.txt").write_text("uncommitted edit\n")

    with pytest.raises(InvalidRepositoryStateError) as excinfo:
        upgrade_proofs(
            cwd=repo,
            config=_requiring_clean_worktree(),
            client=_FakeUpgradeClient([]),
            fetcher=_no_calendars,
        )

    message = str(excinfo.value)
    assert "uncommitted changes" in message
    assert "require_clean_worktree" in message


def test_upgrade_proceeds_on_a_clean_worktree_when_the_check_is_on(
    tmp_path: Path,
) -> None:
    """The other half of the guard: with the check enabled and nothing
    outstanding, the pass must run normally. A check that refuses a clean
    worktree would make the setting unusable."""
    repo, source_id, payload = _repo_with_pending_proof(tmp_path)
    # The fixture leaves an untracked git-ots.toml behind, which is itself a
    # dirty worktree -- commit it so "clean" means clean.
    _git(["add", "-A"], cwd=repo)
    subprocess.run(
        ["git", "commit", "-q", "-m", "Add configuration\n"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    client = _FakeUpgradeClient(
        [UpgradeAttempt(upgraded=_make_bitcoin_attested_proof(payload), detail="")]
    )

    report = upgrade_proofs(
        cwd=repo,
        config=_requiring_clean_worktree(),
        client=client,
        fetcher=_no_calendars,
    )

    assert [r.state for r in report.results] == [UpgradeState.UPGRADED]
    assert report.commit_id is not None
    assert (repo / ".opentimestamps" / f"{source_id}.ots").read_bytes() != b""


def test_upgrade_that_will_not_commit_does_not_demand_a_clean_worktree(
    tmp_path: Path,
) -> None:
    """``[proof] commit = false`` means the operator commits proofs themselves,
    so the pass leaves the worktree alone and has no reason to insist it be
    clean -- refusing here would make the two settings mutually exclusive."""
    repo, source_id, payload = _repo_with_pending_proof(tmp_path)
    (repo / "a.txt").write_text("uncommitted edit\n")
    head_before = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    completed = _make_bitcoin_attested_proof(payload)
    client = _FakeUpgradeClient([UpgradeAttempt(upgraded=completed, detail="")])

    report = upgrade_proofs(
        cwd=repo,
        config=_requiring_clean_worktree(commit=False),
        client=client,
        fetcher=_no_calendars,
    )

    assert [r.state for r in report.results] == [UpgradeState.UPGRADED]
    assert report.commit_id is None
    assert (repo / ".opentimestamps" / f"{source_id}.ots").read_bytes() == completed
    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == head_before


def test_dry_run_does_not_demand_a_clean_worktree(tmp_path: Path) -> None:
    """A dry run writes nothing, so it must stay usable as a "what would
    happen?" question an operator can ask in the middle of other work."""
    repo, _source_id, _payload = _repo_with_pending_proof(tmp_path)
    (repo / "a.txt").write_text("uncommitted edit\n")

    report = upgrade_proofs(
        cwd=repo,
        config=_requiring_clean_worktree(),
        dry_run=True,
        client=_FakeUpgradeClient([]),
        fetcher=_no_calendars,
    )

    assert [r.state for r in report.results] == [UpgradeState.WOULD_UPGRADE]
    assert report.commit_id is None
