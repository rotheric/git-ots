"""Regression tests for the orphaned-proof defect.

`run` performs an irreversible external action -- submitting to the
OpenTimestamps calendars -- before several fallible local ones. When the proof
commit failed, the reported symptom was a *permanently* untagged, fully
anchored proof: the baseline search read the manifest out of the source tree
and reported the source as timestamped, while the tag-completion path asked a
different question ("does a generated proof commit *claim* this source?"),
found nothing, and skipped it silently. Nothing was due, so no future run
could ever create the tag, and both `status` and `validate` reported the
repository as healthy.

The tests below pin the whole reported sequence: the veto, the retries that
must not re-submit, the sweep-in that makes the state permanent, and the
completion that now repairs it -- with the *original* submission time, never
the recovery time.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from git_ots.git import (
    GitCommandError,
    enumerate_timestamp_tags,
    make_process_runner,
)
from git_ots.orchestration import build_snapshot
from git_ots.orchestration import run as run_orchestration
from git_ots.timestamp import build_payload
from git_ots.verify import validate_proofs
from tests.test_integration import _commit, _fresh_config, _git, _init_repo
from tests.test_timestamp import _make_fake_detached_proof

_T0 = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)


class _Calendar:
    """A fake calendar that records every submission it is asked for."""

    def __init__(self) -> None:
        self.submissions: list[str] = []

    def __call__(self, commit_id: str, payload: bytes) -> bytes:
        self.submissions.append(commit_id)
        return _make_fake_detached_proof(payload=build_payload("sha1", commit_id))


def _veto_commits(repo: Path) -> Path:
    """Install a `pre-commit` hook that rejects every commit, as reported.

    The reported hook enforced a whole-tree invariant and objected to files
    that had nothing to do with the proof commit. From git-ots's point of view
    that is simply an environmental veto it cannot satisfy, so a hook that
    always fails reproduces it exactly.
    """
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    return hook


def _tag_names(repo: Path) -> str:
    return _git(["tag", "-l"], cwd=repo)


def _stamp(repo: Path, config, *, now: datetime, submit) -> object:
    return run_orchestration(
        snapshot=build_snapshot(cwd=repo, config=config, now=now),
        now=now,
        submit=submit,
    )


@pytest.fixture
def orphaned(tmp_path: Path):
    """Build the reported repository state: a healthy stamp, then an orphan.

    Returns ``(repo, config, calendar, orphan_id, now, hook)`` where
    ``orphan_id`` has been submitted and its proof written and staged, but the
    proof commit was vetoed, so no tag exists and nothing reached history.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n", date=_T0)
    config = _fresh_config()
    calendar = _Calendar()

    # A healthy first stamp, so the repository carries a generated proof
    # commit -- the ADR D6 idempotence marker the reported repository had.
    first_now = _T0 + timedelta(days=2)
    _stamp(repo, config, now=first_now, submit=calendar)

    later = first_now + timedelta(hours=1)
    orphan_id = _commit(repo, paths=["b.txt"], message="B\n", date=later)
    now = later + timedelta(days=2)

    hook = _veto_commits(repo)
    with pytest.raises(GitCommandError):
        _stamp(repo, config, now=now, submit=calendar)

    assert calendar.submissions[-1] == orphan_id
    assert orphan_id[:12] not in _tag_names(repo)
    assert (repo / ".opentimestamps" / f"{orphan_id}.ots").is_file()
    return repo, config, calendar, orphan_id, now, hook


def test_veto_leaves_a_submitted_proof_with_no_tag(orphaned) -> None:
    """The reported starting state: submitted, stored, uncommitted, untagged."""
    repo, _config, calendar, orphan_id, _now, _hook = orphaned

    assert orphan_id in calendar.submissions
    assert orphan_id[:12] not in _tag_names(repo)
    # `git add` succeeded and `git commit` was vetoed, so the artifacts sit in
    # the index but are absent from history -- exactly the split the report
    # describes, and the reason the next human commit sweeps them in.
    assert f"{orphan_id}.ots" in _git(["ls-files", ".opentimestamps"], cwd=repo)
    committed = _git(["ls-tree", "-r", "--name-only", "HEAD"], cwd=repo)
    assert f"{orphan_id}.ots" not in committed


def test_retrying_a_vetoed_run_never_resubmits(orphaned) -> None:
    """29 retries in the report produced one submission, and must keep doing so.

    Re-submitting would mint a second commitment for one commit on every
    scheduler tick. The proof already on disk is reused instead, which is what
    the unchanged `submitted_at` in the report implied but nothing pinned.
    """
    repo, config, calendar, orphan_id, now, _hook = orphaned
    before = list(calendar.submissions)

    for _ in range(3):
        with pytest.raises(GitCommandError):
            _stamp(repo, config, now=now, submit=calendar)

    assert calendar.submissions == before
    assert calendar.submissions.count(orphan_id) == 1


def test_run_tags_the_orphan_once_its_artifacts_reach_history(orphaned) -> None:
    """The reported permanent failure, now repaired on the next run.

    An unrelated human commit sweeps the stray proof files into history. That
    is the moment the old code declared the timestamp complete and stopped
    doing anything at all; here it is the moment the missing tag becomes
    creatable, because the artifacts are now durable in history -- which is
    the precondition ADR 0001 D4 actually attaches to tagging.
    """
    repo, config, calendar, orphan_id, now, hook = orphaned
    hook.unlink()
    _git(["add", "-A"], cwd=repo)
    subprocess.run(
        ["git", "commit", "-q", "-m", "sweep in stray files"], cwd=repo, check=True
    )
    submissions_before = list(calendar.submissions)

    outcome = _stamp(repo, config, now=now + timedelta(hours=1), submit=calendar)

    assert outcome.tagged == (orphan_id,)
    assert outcome.timestamped == ()
    assert calendar.submissions == submissions_before, "must not re-submit"
    tags = [
        tag
        for tag in enumerate_timestamp_tags(cwd=repo, tag_prefix="ots/")
        if tag.commit_id == orphan_id
    ]
    assert len(tags) == 1


def test_recovered_tag_carries_the_original_submission_time(orphaned) -> None:
    """`submitted_at` comes from the manifest, never from the recovery clock.

    Using recovery time would forge the attestation time -- the tag would
    assert a submission that never happened at that moment. ADR 0001 D5 makes
    this the decisive constraint on any recovery path.
    """
    repo, config, calendar, orphan_id, now, hook = orphaned
    hook.unlink()
    _git(["add", "-A"], cwd=repo)
    subprocess.run(["git", "commit", "-q", "-m", "sweep"], cwd=repo, check=True)

    # The run that repairs the tag is given a clock an hour past the
    # submission. If recovery time were substituted for the manifest's
    # `submitted_at`, both the annotation and the tag name would say so.
    recovery_now = now + timedelta(hours=1)
    _stamp(repo, config, now=recovery_now, submit=calendar)

    tag = next(
        tag
        for tag in enumerate_timestamp_tags(cwd=repo, tag_prefix="ots/")
        if tag.commit_id == orphan_id
    )
    annotation = _git(["cat-file", "tag", tag.name], cwd=repo)
    original = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert f"submitted-at: {original}" in annotation
    assert recovery_now.strftime("%Y-%m-%dT%H:%M:%SZ") not in annotation
    # The tag name encodes the same instant, so it sorts by submission.
    assert tag.name.startswith(f"ots/{now.strftime('%Y%m%dT%H%M%SZ')}/")


def test_completion_does_not_need_a_claiming_generated_proof_commit(
    orphaned,
) -> None:
    """The precise defect: completion keyed on a claim, not on evidence.

    After the sweep-in, no generated proof commit claims the orphan -- the one
    that would have was vetoed. The repository nonetheless holds a manifest
    that validates and a proof that binds cryptographically to this exact
    source, which is strictly stronger evidence than a trailer claim.
    """
    repo, config, calendar, orphan_id, now, hook = orphaned
    hook.unlink()
    _git(["add", "-A"], cwd=repo)
    subprocess.run(["git", "commit", "-q", "-m", "sweep"], cwd=repo, check=True)

    claiming = _git(
        ["log", "--all", "--format=%B", f"--grep=OpenTimestamps-Source: {orphan_id}"],
        cwd=repo,
    )
    assert orphan_id not in claiming, "no proof commit claims the orphan"

    _stamp(repo, config, now=now + timedelta(hours=1), submit=calendar)

    assert orphan_id[:12] in _tag_names(repo)


def test_validate_reports_the_orphan_instead_of_reporting_health(
    orphaned,
) -> None:
    """`validate` said "valid" while the tag was missing; now it says both.

    The silence was half the defect: every built-in diagnostic reported
    success. The missing tag is reported, but does not become an error --
    git-ots never pushes tags, so a fresh clone legitimately has none.
    """
    repo, config, _calendar, orphan_id, _now, hook = orphaned
    hook.unlink()
    _git(["add", "-A"], cwd=repo)
    subprocess.run(["git", "commit", "-q", "-m", "sweep"], cwd=repo, check=True)

    results = {r.source_commit_id: r for r in validate_proofs(cwd=repo, config=config)}

    orphan = results[orphan_id]
    assert orphan.has_timestamp_tag is False
    # The proof itself is fine, and that must still be reported as such.
    assert orphan.state.value in ("valid", "pending-attestation")
    healthy = next(r for cid, r in results.items() if cid != orphan_id)
    assert healthy.has_timestamp_tag is True


# ---------------------------------------------------------------------------
# The two failure modes the reporter identified after filing: a blocked run
# races the repository instead of converging on it, and the state it leaves
# behind is staged-but-uncommitted, which git-ots's own pathspec-scoped proof
# commit then walks straight past.
# ---------------------------------------------------------------------------


def _selective_veto(repo: Path) -> Path:
    """A hook that rejects commits while a marker exists, and permits them after.

    The reported hook enforced a whole-tree invariant: it rejected git-ots's
    proof commit over unrelated files while the operator's own commits, made
    against a different tree state, went through. A marker file reproduces
    that asymmetry without depending on what is in the tree.
    """
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        '#!/bin/sh\n[ -e "$(git rev-parse --git-dir)/veto" ] && exit 1\nexit 0\n'
    )
    hook.chmod(0o755)
    marker = repo / ".git" / "veto"
    marker.write_text("")
    return marker


def _human_commit(repo: Path, name: str, message: str, date: datetime) -> str:
    """Commit one named file, leaving anything else staged exactly as it was.

    The pathspec is the point: it is what the operator's own commit looked
    like in the report, and what leaves git-ots's staged proof files behind.
    """
    (repo / name).write_text(f"{name}\n")
    _git(["add", "--", name], cwd=repo)
    subprocess.run(
        ["git", "commit", "-q", "-m", message, "--", name],
        cwd=repo,
        check=True,
        env={
            **os.environ,
            "GIT_COMMITTER_DATE": date.strftime("%Y-%m-%d %H:%M:%S %z"),
        },
    )
    return _git(["rev-parse", "HEAD"], cwd=repo).strip()


@pytest.fixture
def blocked_with_moving_head(tmp_path: Path):
    """A stranded submission, then HEAD advancing underneath the blocked job.

    Returns ``(repo, config, calendar, stranded_id, marker, now)``.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, paths=["a.txt"], message="A\n", date=_T0)
    config = _fresh_config()
    calendar = _Calendar()
    marker = _selective_veto(repo)

    now = _T0 + timedelta(days=2)
    with pytest.raises(GitCommandError):
        _stamp(repo, config, now=now, submit=calendar)
    stranded_id = calendar.submissions[-1]

    # An active writer advances HEAD while the job is blocked. Their commit
    # names its own path, so git-ots's staged proof files stay staged.
    marker.unlink()
    _human_commit(repo, "c.txt", "C", date=now + timedelta(hours=1))
    marker.write_text("")
    return repo, config, calendar, stranded_id, marker, now + timedelta(days=2)


def test_a_blocked_run_does_not_strand_a_second_submission(
    blocked_with_moving_head,
) -> None:
    """Each blocked run used to buy a fresh commitment and abandon the last.

    The trigger is re-evaluated against the current HEAD every run, so a job
    blocked long enough eventually succeeds on a *different* commit -- silently
    abandoning the one it already paid for. Completing the unrecorded
    submission first makes the run converge instead of race.
    """
    repo, config, calendar, stranded_id, _marker, now = blocked_with_moving_head
    before = list(calendar.submissions)

    with pytest.raises(GitCommandError):
        _stamp(repo, config, now=now, submit=calendar)

    assert calendar.submissions == before, "a blocked run must not buy more"
    assert calendar.submissions.count(stranded_id) == 1


def test_the_recovered_run_completes_the_stranded_submission(
    blocked_with_moving_head,
) -> None:
    """When the blockage clears, the paid-for commitment is what gets recorded."""
    repo, config, calendar, stranded_id, marker, now = blocked_with_moving_head
    marker.unlink()
    before = list(calendar.submissions)

    outcome = _stamp(repo, config, now=now, submit=calendar)

    assert calendar.submissions == before, "must not re-submit"
    assert outcome.tagged == (stranded_id,)
    assert outcome.timestamped == ()
    assert outcome.committed == (stranded_id,)
    assert stranded_id[:12] in _tag_names(repo)
    committed = _git(["ls-tree", "-r", "--name-only", "HEAD"], cwd=repo)
    assert f"{stranded_id}.ots" in committed


def test_a_generated_proof_commit_never_leaves_earlier_proofs_staged(
    blocked_with_moving_head,
) -> None:
    """The trap: a pathspec-scoped proof commit walking past staged siblings.

    git-ots commits its artifacts by explicit pathspec (ADR 0001 D1), so a
    later run's *successful* proof commit named only its own paths and left the
    earlier, stranded proof's files sitting in the index -- tracked by nothing,
    reachable from nothing, and invisible to every diagnostic. That is how the
    reported proof became permanent.
    """
    repo, config, calendar, stranded_id, marker, now = blocked_with_moving_head
    marker.unlink()

    _stamp(repo, config, now=now, submit=calendar)

    staged = _git(["diff", "--cached", "--name-only"], cwd=repo).split()
    assert staged == [], f"artifacts left staged: {staged}"
    assert stranded_id[:12] in _tag_names(repo)


def test_a_deferred_trigger_is_reported_rather_than_silently_dropped(
    blocked_with_moving_head,
) -> None:
    """Converging is not the same as doing nothing, and must not look like it."""
    repo, config, calendar, stranded_id, marker, now = blocked_with_moving_head
    marker.unlink()
    snapshot = build_snapshot(cwd=repo, config=config, now=now)
    due = [pending.commit_id for pending in snapshot.decision.commits]
    assert due and due != [stranded_id], "the trigger has moved on to new work"

    outcome = run_orchestration(snapshot=snapshot, now=now, submit=calendar)

    assert outcome.deferred == tuple(due)
    assert outcome.changed is True


def test_the_deferred_commit_is_stamped_on_the_following_run(
    blocked_with_moving_head,
) -> None:
    """Deferral delays the new work by one run; it never loses it."""
    repo, config, calendar, _stranded_id, marker, now = blocked_with_moving_head
    marker.unlink()
    _stamp(repo, config, now=now, submit=calendar)

    later = now + timedelta(days=2)
    outcome = _stamp(repo, config, now=later, submit=calendar)

    assert outcome.timestamped, "the deferred work is picked up next run"
    assert outcome.deferred == ()
    for source_commit_id in outcome.timestamped:
        assert source_commit_id[:12] in _tag_names(repo)


def test_in_flight_scanning_cost_does_not_grow_with_stored_proofs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan must cost the same at 1 stamp and at 1000.

    ADR 0001 D6's refinement records this project being measured into
    hundreds of subprocesses per invocation by a check that asked its
    question once per stored proof. Answering "is this committed?" per proof
    would reintroduce exactly that.

    The tags are deleted before measuring, which is not a contrivance: it is
    what a clone looks like, since git-ots never pushes tags. With tags
    present every proof is dismissed by a set lookup and the expensive check
    is never reached -- so a measurement taken with them would pass whatever
    the implementation does, which is the trap D6's own note warns regression
    tests here to avoid.

    `run` builds its own process runner from the configured `ots.gitTimeout`
    rather than accepting one, so the counter is installed by patching the
    factory; passing a runner to `build_snapshot` would only ever count the
    snapshot's calls.

    Only the tree reads are counted, not every subprocess. The empty-decision
    completion path separately enumerates generated proof commits, and that
    enumeration already grows with the stamp count -- a pre-existing cost this
    test deliberately does not claim to constrain, since folding it in would
    make the assertion untrippable by the thing it exists to catch.
    """

    def _subprocess_count_for(stamps: int) -> int:
        repo = tmp_path / f"repo{stamps}"
        _init_repo(repo)
        _commit(repo, paths=["a.txt"], message="A\n", date=_T0)
        config = _fresh_config()
        calendar = _Calendar()
        now = _T0 + timedelta(days=2)
        for index in range(stamps):
            _commit(
                repo,
                paths=[f"f{index}.txt"],
                message=f"F{index}\n",
                date=_T0 + timedelta(days=index + 1),
            )
            now = _T0 + timedelta(days=index + 4)
            _stamp(repo, config, now=now, submit=calendar)

        for tag in enumerate_timestamp_tags(cwd=repo, tag_prefix="ots/"):
            _git(["tag", "-d", tag.name], cwd=repo)

        calls: list[list[str]] = []
        real = make_process_runner(timeout=60.0)

        def counting(argv, *, cwd, stdin=None):
            calls.append(list(argv))
            return real(argv, cwd=cwd, stdin=stdin)

        monkeypatch.setattr(
            "git_ots.orchestration.make_process_runner", lambda **_: counting
        )
        try:
            idle = now + timedelta(minutes=1)
            run_orchestration(
                snapshot=build_snapshot(cwd=repo, config=config, now=idle),
                now=idle,
                submit=calendar,
            )
        finally:
            monkeypatch.undo()
        return sum(1 for argv in calls if argv[1] in ("ls-tree", "show"))

    few = _subprocess_count_for(2)
    many = _subprocess_count_for(6)

    assert many == few, (
        f"tree reads grew with the number of stored proofs: {few} -> {many}"
    )
