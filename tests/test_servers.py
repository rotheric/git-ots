"""Tests for calendar reachability checks and the extended status output."""

from __future__ import annotations

import urllib.error
from contextlib import contextmanager
from pathlib import Path

import pytest

from git_ots.cli import main
from git_ots.servers import ServerStatus, check_calendars, collect_calendar_urls
from git_ots.timestamp import build_payload
from tests.test_anchors import ALICE, BOB, CATALLAXY, FINNEY, FIXTURE
from tests.test_timestamp import _make_fake_detached_proof
from tests.test_verify import (
    _commit,
    _fresh_config,
    _init_repo,
    _write_config,
    _write_proof_and_manifest,
)

FIXTURE_SOURCE_COMMIT = "23afa0b0345f1d85b9ce346617b5677a4372b98d"


def _opener(outcomes: dict[str, object]):
    """Build a urlopen stand-in driven by a per-URL outcome table."""

    @contextmanager
    def _response(status: int):
        class _Response:
            def __init__(self) -> None:
                self.status = status

        yield _Response()

    def opener(request, timeout: float):
        outcome = outcomes[request.full_url]
        if isinstance(outcome, Exception):
            raise outcome
        return _response(outcome)

    return opener


def _repo_with_fixture_proof(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n")
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir(parents=True, exist_ok=True)
    _write_proof_and_manifest(repo, FIXTURE_SOURCE_COMMIT, FIXTURE.read_bytes())
    _write_config(repo, _fresh_config())
    return repo


def test_undelivered_calendars_count_as_pending_even_when_anchored(
    tmp_path: Path,
) -> None:
    """Being anchored does not settle the calendars that have not answered.

    `git-ots upgrade` asks each of them on every run rather than stopping at
    the first Bitcoin attestation, so an outage at one genuinely holds up
    work and has to be counted.
    """
    repo = _repo_with_fixture_proof(tmp_path)

    counts = collect_calendar_urls(repository_root=repo, config=_fresh_config())

    assert counts == {ALICE: 0, BOB: 0, CATALLAXY: 1, FINNEY: 1}


def test_unanchored_proof_counts_its_calendars_as_pending(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")
    _write_proof_and_manifest(
        repo,
        source_id,
        _make_fake_detached_proof(payload=build_payload("sha1", source_id)),
    )

    counts = collect_calendar_urls(repository_root=repo, config=_fresh_config())

    assert counts == {"https://example.com": 1}


def test_collect_returns_nothing_without_a_proof_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n")

    assert collect_calendar_urls(repository_root=repo, config=_fresh_config()) == {}


def test_collect_skips_unreadable_proofs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")
    _write_proof_and_manifest(repo, source_id, b"not a proof at all")

    assert collect_calendar_urls(repository_root=repo, config=_fresh_config()) == {}


def test_reachable_calendar_is_reported_ok(tmp_path: Path) -> None:
    repo = _repo_with_fixture_proof(tmp_path)
    outcomes = {ALICE: 200, BOB: 200, CATALLAXY: 200, FINNEY: 200}

    results = check_calendars(
        repository_root=repo, config=_fresh_config(), opener=_opener(outcomes)
    )

    assert all(result.reachable for result in results)
    assert [result.url for result in results] == [ALICE, BOB, CATALLAXY, FINNEY]


def test_http_error_still_counts_as_reachable(tmp_path: Path) -> None:
    """A calendar root answering 404 is up; only the root path is uninteresting."""
    repo = _repo_with_fixture_proof(tmp_path)
    outcomes = {
        ALICE: urllib.error.HTTPError(ALICE, 404, "Not Found", {}, None),
        BOB: 200,
        CATALLAXY: 200,
        FINNEY: 200,
    }

    results = check_calendars(
        repository_root=repo, config=_fresh_config(), opener=_opener(outcomes)
    )

    alice = next(r for r in results if r.url == ALICE)
    assert alice.reachable
    assert "404" in alice.detail


def test_unreachable_calendar_is_reported_with_its_reason(tmp_path: Path) -> None:
    repo = _repo_with_fixture_proof(tmp_path)
    outcomes = {
        ALICE: 200,
        BOB: 200,
        CATALLAXY: urllib.error.URLError("Name or service not known"),
        FINNEY: 200,
    }

    results = check_calendars(
        repository_root=repo, config=_fresh_config(), opener=_opener(outcomes)
    )

    catallaxy = next(r for r in results if r.url == CATALLAXY)
    assert not catallaxy.reachable
    assert "Name or service not known" in catallaxy.detail
    # The fixture proof never received catallaxy's attestation, so this
    # outage does block collecting it.
    assert catallaxy.pending_proofs == 1


def test_non_http_calendar_url_is_never_opened(tmp_path: Path) -> None:
    """Calendar URLs come out of proof files, so the scheme must be constrained."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")
    payload = build_payload("sha1", source_id)
    proof = _make_fake_detached_proof(payload=payload)
    # Length-matched so the surrounding varbytes framing stays valid; a
    # shorter URL would simply fail to decode and prove nothing.
    proof = proof.replace(b"https://example.com", b"file:///etc/passwdx")
    _write_proof_and_manifest(repo, source_id, proof)

    def exploding_opener(request, timeout: float):
        raise AssertionError(f"must not open {request.full_url}")

    results = check_calendars(
        repository_root=repo, config=_fresh_config(), opener=exploding_opener
    )

    assert len(results) == 1
    assert not results[0].reachable
    assert "scheme" in results[0].detail


# --- status output ----------------------------------------------------------


def test_status_reports_block_transaction_and_op_return(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo_with_fixture_proof(tmp_path)

    exit_code = main(["status"], cwd=repo)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "proofs: 1 anchored, 0 pending" in out
    assert f"block 963292 via {ALICE}" in out
    assert "tx 32f1a1c3c32b7a050940263b9d1f2bd911954067a7fcdc7690c3607c6557a15c" in out
    assert (
        "op_return 0562fe8d215231fa67e3f1e842437a013ea66718a3e8a03c2d7e523ee2d6e295"
        in out
    )
    assert f"awaiting {CATALLAXY}" in out


def test_status_reports_a_pending_proof_without_anchors(
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
    _write_config(repo, _fresh_config())

    main(["status"], cwd=repo)

    out = capsys.readouterr().out
    assert "proofs: 0 anchored, 1 pending" in out
    assert "awaiting https://example.com" in out


def test_status_makes_no_network_request_without_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduled path must stay offline; that is why the probe is opt-in."""
    repo = _repo_with_fixture_proof(tmp_path)

    def explode(**kwargs):
        raise AssertionError("status must not contact calendars by default")

    monkeypatch.setattr("git_ots.cli.check_calendars", explode)

    assert main(["status"], cwd=repo) == 0


def test_status_check_servers_warns_about_an_unavailable_calendar(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_fixture_proof(tmp_path)

    monkeypatch.setattr(
        "git_ots.cli.check_calendars",
        lambda **kwargs: (
            ServerStatus(
                url=ALICE, reachable=True, detail="HTTP 200", pending_proofs=0
            ),
            ServerStatus(
                url=CATALLAXY,
                reachable=False,
                detail="Connection refused",
                pending_proofs=1,
            ),
        ),
    )

    exit_code = main(["status", "--check-servers"], cwd=repo)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "calendars: 1/2 reachable" in captured.out
    assert f"{CATALLAXY} unavailable (Connection refused, 1 pending)" in captured.out
    assert "is unavailable" in captured.err
    assert "cannot be completed by `git-ots upgrade`" in captured.err
