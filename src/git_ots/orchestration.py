"""Read-only orchestration service that composes config, Git, and policy inputs.

The :class:`RepositorySnapshot` captures everything needed to decide whether a
timestamp is required without performing any side effects. It is the
foundation for ``status``, ``run --dry-run``, and the normal run path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config
from .git import (
    DEFAULT_INDEX_LOCK_RETRY_WINDOW,
    CommitInfo,
    InvalidRepositoryStateError,
    LineageBaselineResult,
    RepositoryLayout,
    RepositoryLock,
    ResolvedRef,
    assert_committable_state,
    assert_worktree_present,
    create_generated_proof_commit,
    create_timestamp_tag,
    detect_object_format,
    enumerate_generated_proof_commit_sources,
    enumerate_timestamp_tags,
    filter_pending_meaningful_commits,
    find_generated_proof_commits_claiming_source,
    find_newest_relevant_baseline,
    get_commit_parents,
    has_generated_proof_commits_in_history,
    is_ancestor_commit,
    is_worktree_clean,
    list_tree_proof_paths,
    locate_repository,
    make_process_runner,
    order_commits_newest_first,
    paths_are_committed,
    read_committed_proof_evidence,
    resolve_source_ref,
    stage_paths,
)
from .policy import Decision, PendingCommit, PolicyState, evaluate_policy
from .timestamp import (
    OpenTimestampsCli,
    PersistenceError,
    ProofEvidence,
    RecoveryValidationError,
    TimestampRequest,
    build_manifest,
    build_payload,
    inspect_recovery_artifacts,
    inspect_recovery_artifacts_if_present,
    persist_manifest,
    persist_proof,
    validate_detached_proof,
)


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """Read-only repository state captured at the start of an invocation.

    ``source`` and ``object_format`` are frozen once at snapshot creation so
    later mutation steps cannot accidentally re-resolve the configured ref.
    ``pending`` contains the meaningful commits between the baseline and the
    frozen source. ``policy_state`` and ``decision`` provide the schedule
    evidence needed by status and dry-run output.
    """

    config: Config
    layout: RepositoryLayout
    source: ResolvedRef
    object_format: str
    baseline: LineageBaselineResult
    pending: tuple[CommitInfo, ...]
    policy_state: PolicyState
    decision: Decision


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What a completed :func:`run` actually did.

    ``run`` returns 0 from the CLI whether it stamped something or correctly
    did nothing, which is right for the default contract but leaves a
    scheduler unable to tell the two apart. This record carries the
    distinction so the CLI can surface it on request without the caller
    parsing prose.

    ``timestamped`` names sources newly submitted this run. ``tagged`` and
    ``committed`` name sources whose *bookkeeping* was completed from
    evidence that already existed -- a proof commit or tag finished after an
    earlier run was interrupted. A run that repaired something is not idle,
    so ``changed`` covers all three.

    ``deferred`` names sources a trigger selected but which this run did not
    submit, because it spent itself finishing an earlier run's unrecorded
    submission instead. This is not a failure and not idleness: it is the run
    converging on work already paid for rather than racing ahead of it.
    """

    timestamped: tuple[str, ...] = ()
    tagged: tuple[str, ...] = ()
    committed: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        """True when this run altered the repository in any way."""
        return bool(self.timestamped or self.tagged or self.committed)


def build_snapshot(
    *,
    cwd: Path,
    config: Config,
    now: datetime,
    process_runner=None,
) -> RepositorySnapshot:
    """Build a read-only snapshot of the repository for the given configuration.

    This function never fetches, writes files, creates tags, or commits. It
    locates the repository, resolves the source ref once, detects the object
    format, finds the newest relevant timestamp baseline, enumerates pending
    meaningful commits, and evaluates the configured policies.
    """
    layout = locate_repository(cwd=cwd, process_runner=process_runner)
    object_format = detect_object_format(
        cwd=layout.worktree_root, process_runner=process_runner
    )
    source = resolve_source_ref(
        cwd=layout.worktree_root,
        ref=config.git.source_ref,
        process_runner=process_runner,
    )
    baseline = find_newest_relevant_baseline(
        cwd=layout.worktree_root,
        source_commit_id=source.commit_id,
        tag_prefix=config.git.tag_prefix,
        proof_directory=config.proof.directory,
        object_format=object_format.value,
        process_runner=process_runner,
    )
    pending = filter_pending_meaningful_commits(
        cwd=layout.worktree_root,
        ref=source.commit_id,
        # Exclude from the stamped source, not from the proof commit that
        # recorded it. The two sit on the same lineage only in a linear
        # history; across a merge the marker commit can be on the opposite
        # branch from the selected source, which would then leave
        # the stamped commit itself inside the pending range. Generated proof
        # commits are dropped by the filter regardless, so excluding from the
        # source loses nothing and correctly keeps commits merged in after the
        # stamp pending -- the proof attests to that source alone.
        baseline=(
            baseline.baseline.source_commit_id
            if baseline.baseline is not None
            else None
        ),
        proof_directory=config.proof.directory,
        process_runner=process_runner,
    )
    last_fixed = None
    if baseline.baseline is not None and "fixed_time" in baseline.baseline.triggers:
        last_fixed = baseline.baseline.submitted_at
    policy_state = PolicyState(
        now=now,
        pending_commits=tuple(
            PendingCommit(
                commit_id=c.commit_id,
                committer_time=c.committer_time,
            )
            for c in pending
        ),
        last_fixed_time_occurrence=last_fixed,
        has_baseline=baseline.baseline is not None,
        has_abandoned_tags=baseline.has_abandoned_tags,
    )
    decision = evaluate_policy(policy_state, config.policy)
    return RepositorySnapshot(
        config=config,
        layout=layout,
        source=source,
        object_format=object_format.value,
        baseline=baseline,
        pending=pending,
        policy_state=policy_state,
        decision=decision,
    )


def run(
    *,
    snapshot: RepositorySnapshot,
    now: datetime | None = None,
    submit: Callable[..., Any] | None = None,
    persist: Callable[..., Any] | None = None,
    tag: Callable[..., Any] | None = None,
    commit: Callable[..., Any] | None = None,
) -> RunOutcome:
    """Execute the timestamping run for a prepared snapshot.

    When the policy decision selects commits, each selected commit is
    submitted in repository order. The default collaborators perform the
    real OpenTimestamps call, atomic proof persistence, annotated source tag
    creation, and generated proof commit creation.

    When the decision is empty, the run completes any bookkeeping an earlier
    interrupted run left unfinished rather than doing nothing: committing
    proof artifacts that were never committed (spec section 22.2), and
    creating a timestamp tag for any source whose proof artifacts are already
    durable in history but which carries no tag (spec section 22.3).

    Returns a :class:`RunOutcome` describing what was done, so a caller can
    distinguish "timestamped", "repaired", and "nothing was due" -- all three
    of which are success.
    """
    if now is None:
        now = datetime.now(UTC)

    # AC-UX-5: built here, from snapshot.config, the same way _default_submit
    # (below) already derives the `ots` ceiling from snapshot.config.limits
    # .ots_timeout -- this makes `run` symmetric with itself rather than
    # introducing a new pattern. None (an operator's git_timeout = "0") is
    # threaded straight through as unbounded, never coalesced. Passed into
    # every git call this function makes -- all of them previously received
    # no process_runner and so silently fell back to the 60s built-in default
    # regardless of configuration. Most importantly create_generated_proof_
    # commit's `git commit`, which runs without --no-verify and so executes
    # the repository's pre-commit/commit-msg hooks: arbitrary operator code
    # that a 60s ceiling can cut off with no way to raise it. The other ten
    # call sites are read-only inspections with no hook execution of their
    # own, so the hook-hazard argument doesn't apply to them individually --
    # they are wired anyway because the inconsistency of wiring only some of
    # run()'s calls, in a single function where process_runner is already a
    # local variable, reads as an oversight rather than a boundary (unlike
    # the module-edge boundary that keeps GitRunner.run_bytes's binary path
    # on the built-in default -- see architecture.json).
    git_timeout = snapshot.config.limits.git_timeout
    process_runner = make_process_runner(
        timeout=git_timeout.total_seconds() if git_timeout is not None else None
    )

    # How long the index-mutating steps will wait out a *concurrent* Git
    # process holding `.git/index.lock`, as opposed to how long any single Git
    # invocation may run (which is what `ots.gitTimeout` bounds). A scheduled
    # run against a repository someone is also working in meets this routinely,
    # and before this the first collision aborted the run -- after the
    # OpenTimestamps submission had already happened, which is precisely the
    # window in which giving up is most expensive. Reusing `ots.gitTimeout` as
    # the budget keeps the operator's single "how patient is this tool with
    # git" dial meaningful instead of introducing a second one; an unbounded
    # setting falls back to a finite window, because waiting forever for
    # another process would turn a scheduled job into a stuck one.
    index_lock_retry_window = (
        git_timeout.total_seconds()
        if git_timeout is not None
        else DEFAULT_INDEX_LOCK_RETRY_WINDOW
    )

    def _try_recover(commit_id: str) -> ProofEvidence | None:
        """Return tag evidence from the *worktree*, or None if it is unusable."""
        try:
            artifacts = inspect_recovery_artifacts(
                repository_root=snapshot.layout.worktree_root,
                proof_directory=Path(snapshot.config.proof.directory),
                object_format=snapshot.object_format,
                commit_id=commit_id,
            )
        except (PersistenceError, RecoveryValidationError):
            return None
        return artifacts.as_evidence(commit_id)

    def _committed_evidence(commit_id: str) -> ProofEvidence | None:
        """Return tag evidence read from the frozen source's tree, or None.

        Evidence found here is by construction already committed, which is
        the precondition ADR 0001 D4 attaches to creating a tag.
        """
        return read_committed_proof_evidence(
            cwd=snapshot.layout.worktree_root,
            source_commit_id=commit_id,
            artifact_commit=snapshot.source.commit_id,
            proof_directory=snapshot.config.proof.directory,
            object_format=snapshot.object_format,
            process_runner=process_runner,
        )

    def _default_submit(commit_id: str, payload: bytes) -> bytes:
        client = OpenTimestampsCli(
            command=snapshot.config.opentimestamps.command,
            limit=snapshot.config.limits.ots_timeout,
        )
        request = TimestampRequest(
            object_format=snapshot.object_format,
            commit_id=commit_id,
            payload=payload,
        )
        try:
            result = client.submit(request)
        except KeyboardInterrupt:
            # This must happen while submit() is still on the stack: once it
            # has returned or raised on its own, _default_runner's finally has
            # already cleared the child registration and terminate_active_child()
            # becomes a silent no-op (architecture.md's cross-story
            # constraint on the interrupt-cleanup seam). cli.py cannot do this
            # itself -- it has no reference to this client.
            client.terminate_active_child()
            raise
        return result.proof.data

    def _default_persist(commit_id: str, proof: bytes) -> None:
        repository_root = snapshot.layout.worktree_root
        proof_directory = Path(snapshot.config.proof.directory)
        object_format = snapshot.object_format
        persist_proof(
            repository_root=repository_root,
            proof_directory=proof_directory,
            object_format=object_format,
            commit_id=commit_id,
            proof_bytes=proof,
        )
        proof_name = f"{commit_id}.ots"
        manifest = build_manifest(
            object_format=object_format,
            commit_id=commit_id,
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

    # Keyed by commit id, not a single shared value: commits are processed in
    # one loop and tagged in a later one, so a shared variable would leave every
    # commit except the last tagged with another commit's recovery metadata --
    # including its submission time, which would forge the attestation
    # (ADR D5). Reachable whenever one decision mixes recovered and fresh
    # commits: an `every_commit` batch interrupted partway, or a clone that
    # received proof artifacts but not tags, since git-ots never pushes tags.
    _recovered_by_commit: dict[str, ProofEvidence] = {}

    # Sources whose bookkeeping this run finished from evidence that already
    # existed, rather than by submitting anything new.
    completed_tags: list[str] = []

    def _default_tag(commit_id: str) -> None:
        proof_directory = snapshot.config.proof.directory
        submitted_at = now
        triggers = snapshot.decision.triggers
        proof_name = f"{commit_id}.ots"
        recovered = _recovered_by_commit.get(commit_id)
        if recovered is not None:
            submitted_at = recovered.submitted_at
            triggers = recovered.triggers
            proof_name = recovered.proof_name
        create_timestamp_tag(
            cwd=snapshot.layout.worktree_root,
            source_commit_id=commit_id,
            tag_prefix=snapshot.config.git.tag_prefix,
            submitted_at=submitted_at,
            proof=f"{proof_directory}/{proof_name}",
            triggers=triggers,
            sign=snapshot.config.git.signs_generated_objects,
            process_runner=process_runner,
        )

    def _default_commit(source_commit_ids: list[str]) -> None:
        repository_root = snapshot.layout.worktree_root
        proof_directory = snapshot.config.proof.directory
        paths: list[str] = []
        for commit_id in source_commit_ids:
            paths.append(f"{proof_directory}/{commit_id}.ots")
            paths.append(f"{proof_directory}/{commit_id}.json")
        stage_paths(
            cwd=repository_root,
            paths=paths,
            process_runner=process_runner,
            index_lock_retry_window=index_lock_retry_window,
        )
        create_generated_proof_commit(
            cwd=repository_root,
            source_commit_ids=source_commit_ids,
            proof_directory=proof_directory,
            paths=paths,
            sign=snapshot.config.git.signs_generated_objects,
            process_runner=process_runner,
            index_lock_retry_window=index_lock_retry_window,
        )

    def _in_flight_sources(exclude: frozenset[str]) -> list[str]:
        """Return sources this repository paid for but never recorded.

        An in-flight source is one whose proof and manifest are on disk, valid,
        and cryptographically bound to it, but which history does not yet
        carry. That is a submission already made to the calendars -- money and
        an irreversible external commitment -- that no artifact in the
        repository records.

        The state is reached whenever the proof commit fails: the artifacts are
        written and staged, and `git commit` is refused. What made it
        *permanent* is that the next run does not re-enter the same
        transaction. It re-resolves the source ref and re-evaluates the
        trigger against whatever HEAD is now, so in a repository with an active
        writer the run that finally gets past the blockage is stamping a
        different commit -- and because generated proof commits name their
        paths explicitly (ADR 0001 D1), that successful commit walks straight
        past the earlier proof's staged files and leaves them staged. One
        stranded submission per blocked run, each invisible.

        Ancestry is required: a proof for a commit that has been rewritten off
        the lineage is not evidence about this history, and completing it would
        put a tag on an abandoned lineage. `validate` reports those as
        `orphaned` and `git-ots repair` is where the operator decides.

        A source that already carries a timestamp tag is **not** in flight,
        however its artifacts are stored. The tag is the record; once it
        exists the submission is accounted for in the ref namespace and
        nothing has been abandoned. Whether the artifacts are additionally
        committed is a separate question the operator answers with
        `ots.proofCommit`, and a source stamped under `proofCommit = false`
        must not be swept into a proof commit by a later run that has it
        enabled (ADR 0001 D4/D6). "Tag present, artifacts uncommitted" is spec
        section 22.2 and has its own completion path.

        Sources already in ``exclude`` -- the ones this run's own decision
        selected -- are left out, because the main loop already resumes them
        through `inspect_recovery_artifacts_if_present`.
        """
        proof_directory = snapshot.config.proof.directory
        proof_dir = snapshot.layout.worktree_root / Path(proof_directory)
        if not proof_dir.is_dir():
            return []
        # Two subprocesses for the whole scan, whatever the number of stored
        # proofs. Asking per proof whether it is tagged or committed would make
        # the cost of every run grow with the number of stamps ever made -- the
        # bound ADR 0001 D6's refinement exists to protect. Everything after
        # these two is either a set lookup or a filesystem read, and the one
        # remaining subprocess (the ancestry test) runs only for a genuine
        # candidate, of which a healthy repository has none.
        tagged_sources = {
            existing.commit_id
            for existing in enumerate_timestamp_tags(
                cwd=snapshot.layout.worktree_root,
                tag_prefix=snapshot.config.git.tag_prefix,
                process_runner=process_runner,
            )
        }
        committed_paths = list_tree_proof_paths(
            cwd=snapshot.layout.worktree_root,
            commit=snapshot.source.commit_id,
            proof_directory=proof_directory,
            process_runner=process_runner,
        )
        found: list[str] = []
        for proof_path in sorted(proof_dir.glob("*.ots")):
            source_commit_id = proof_path.stem
            if source_commit_id in exclude or source_commit_id in tagged_sources:
                continue
            # Already durable in history: not in flight. Tag completion for
            # this case is the empty-decision path's job (section 22.3).
            if f"{proof_directory}/{source_commit_id}.ots" in committed_paths:
                continue
            evidence = _try_recover(source_commit_id)
            if evidence is None:
                continue
            if not is_ancestor_commit(
                cwd=snapshot.layout.worktree_root,
                source_commit_id=source_commit_id,
                frozen_source_id=snapshot.source.commit_id,
                process_runner=process_runner,
            ):
                continue
            _recovered_by_commit[source_commit_id] = evidence
            found.append(source_commit_id)
        # Oldest first, so a batch proof commit lists its sources in the order
        # history made them rather than in object-id order.
        return list(
            reversed(
                order_commits_newest_first(
                    cwd=snapshot.layout.worktree_root,
                    source_commit_id=snapshot.source.commit_id,
                    commit_ids=found,
                    process_runner=process_runner,
                )
            )
        )

    # A bare repository has no working tree, so snapshot.layout.worktree_root
    # is the Git directory itself (locate_repository's fallback). This is
    # this module's own mutation boundary -- cli.py's `run` command checks
    # this too, but that must not be the only guard, since this function is
    # the public orchestration entry point direct callers can reach without
    # going through the CLI at all (see the lock comment below).
    assert_worktree_present(snapshot.layout, operation="run")

    # Acquire the repository-level advisory lock before any mutation. This
    # protects the public orchestration mutation entry point so direct calls
    # from separate processes cannot bypass the lock (spec section 24).
    with RepositoryLock(common_git_dir=snapshot.layout.common_git_dir):
        # Verify the repository is in a state where committing is meaningful
        # before any side effect. With proof commits enabled this also requires
        # a symbolic HEAD so the generated proof commit is not orphaned.
        assert_committable_state(
            cwd=snapshot.layout.worktree_root,
            require_symbolic_head=snapshot.config.proof.commit,
            process_runner=process_runner,
        )

        # Enforce worktree cleanliness before any mutation when proof commits are
        # enabled. Item 101 made generated proof commits use an explicit pathspec,
        # so unrelated worktree changes are no longer swept in; the
        # require_clean_worktree key remains an operator preference.
        if (
            snapshot.config.proof.commit
            and snapshot.config.git.require_clean_worktree
            and not is_worktree_clean(
                cwd=snapshot.layout.worktree_root, process_runner=process_runner
            )
        ):
            raise InvalidRepositoryStateError(
                "worktree has uncommitted changes; commit or stash them, or set "
                "[git] require_clean_worktree = false -- generated commits name "
                "their paths explicitly, so unrelated changes are never included"
            )

        _submit = submit if submit is not None else _default_submit
        _persist = persist if persist is not None else _default_persist
        _tag = tag if tag is not None else _default_tag
        _commit = commit if commit is not None else _default_commit

        # Finish an earlier run's unrecorded submission before acting on this
        # run's trigger. Doing it in this order is the whole point: a run that
        # submits first races the blockage instead of converging on it, and
        # every lap of that race abandons another paid-for commitment.
        # Injected persistence/commit collaborators mean the caller is
        # simulating the transaction rather than performing it, so on-disk
        # artifacts are not this run's to adopt.
        in_flight: list[str] = []
        if persist is None and commit is None:
            in_flight = _in_flight_sources(
                exclude=frozenset(
                    pending.commit_id for pending in snapshot.decision.commits
                )
            )

        if not snapshot.decision.commits and not in_flight:
            # Completion recovery: a valid timestamp tag exists but its proof
            # artifacts were never committed as a generated proof commit.
            if (
                snapshot.config.proof.commit
                and snapshot.baseline.baseline is not None
                and persist is None
                and commit is None
            ):
                baseline_id = snapshot.baseline.baseline.source_commit_id
                recovered = _try_recover(baseline_id)
                if recovered is not None:
                    proof_directory = snapshot.config.proof.directory
                    paths = [
                        f"{proof_directory}/{baseline_id}.ots",
                        f"{proof_directory}/{baseline_id}.json",
                    ]
                    # Once the repository contains a generated proof commit, the
                    # D6 idempotence marker is authoritative. A tag-only
                    # baseline is treated as complete and any leftover worktree
                    # artifacts are not auto-committed (ADR D4/D6).
                    if not paths_are_committed(
                        cwd=snapshot.layout.worktree_root,
                        paths=paths,
                        process_runner=process_runner,
                    ) and not has_generated_proof_commits_in_history(
                        cwd=snapshot.layout.worktree_root,
                        source_commit_id=snapshot.source.commit_id,
                        process_runner=process_runner,
                    ):
                        _commit([baseline_id])
                        return RunOutcome(committed=(baseline_id,))

            # Completion path for "proof commit present, tag absent" (ADR D4).
            # Because baseline derivation uses generated proof commits (ADR
            # D6), this state produces an empty decision, so the tag must be created
            # from the manifest without resubmitting or committing again.
            if snapshot.config.proof.commit:
                existing_tags = enumerate_timestamp_tags(
                    cwd=snapshot.layout.worktree_root,
                    tag_prefix=snapshot.config.git.tag_prefix,
                    process_runner=process_runner,
                )
                tagged_sources = {t.commit_id for t in existing_tags}

                # Sources are checked against at most two search ranges (full
                # history for the baseline source, baseline..source for pending
                # sources). Enumerate each range once and answer per-source
                # claims from the cached mapping so the subprocess cost does
                # not scale with the number of pending commits.
                claims_cache: dict[str | None, dict[str, tuple[str, ...]]] = {}

                def _claiming_proof_commits(
                    source_commit_id: str, search_baseline: str | None
                ) -> tuple[str, ...]:
                    if search_baseline not in claims_cache:
                        mapping: dict[str, list[str]] = {}
                        for (
                            proof_commit_id,
                            sources,
                        ) in enumerate_generated_proof_commit_sources(
                            cwd=snapshot.layout.worktree_root,
                            ref=snapshot.source.commit_id,
                            baseline=search_baseline,
                            proof_directory=snapshot.config.proof.directory,
                            process_runner=process_runner,
                        ):
                            for claimed in sources:
                                mapping.setdefault(claimed, []).append(proof_commit_id)
                        claims_cache[search_baseline] = {
                            claimed: tuple(commits)
                            for claimed, commits in mapping.items()
                        }
                    return claims_cache[search_baseline].get(source_commit_id, ())

                sources_to_check: list[str] = []
                if snapshot.baseline.baseline is not None:
                    # Unconditionally, including a tag-derived baseline: the
                    # `tagged_sources` test below already excludes anything
                    # that has a tag, so the previous `proof_commit_id is not
                    # None` guard was a proxy for that test rather than an
                    # additional condition -- and a misleading one, because it
                    # implied the completion path was about proof commits when
                    # it is about the absence of a tag.
                    sources_to_check.append(snapshot.baseline.baseline.source_commit_id)
                sources_to_check.extend(p.commit_id for p in snapshot.pending)
                seen_sources: set[str] = set()
                for source_commit_id in sources_to_check:
                    if source_commit_id in seen_sources:
                        continue
                    seen_sources.add(source_commit_id)
                    if source_commit_id in tagged_sources:
                        continue
                    # For the baseline source itself the proof commit is at or
                    # before the baseline, so search the full history reachable
                    # from the frozen source. Pending sources are searched in
                    # the usual baseline..source window.
                    baseline_for_search = (
                        None
                        if (
                            snapshot.baseline.baseline is not None
                            and snapshot.baseline.baseline.source_commit_id
                            == source_commit_id
                        )
                        else (
                            snapshot.baseline.baseline.source_commit_id
                            if snapshot.baseline.baseline is not None
                            else None
                        )
                    )
                    claiming = _claiming_proof_commits(
                        source_commit_id, baseline_for_search
                    )
                    # Completion is gated on validated *evidence*, not on a
                    # proof commit's claim to have stamped this source. The
                    # two are not the same question, and keying on the claim
                    # made a fully anchored proof permanently untagged: if the
                    # proof commit is vetoed (a repository-wide `pre-commit`
                    # hook rejects it over unrelated files) and the artifacts
                    # later reach history by some other route -- an ordinary
                    # human commit sweeping them in -- then the baseline search
                    # reads the manifest from the source tree and reports the
                    # source as timestamped, while this loop found no claiming
                    # proof commit and skipped it. Baseline and completion
                    # disagreed, nothing was due, and no future run could ever
                    # create the tag.
                    #
                    # Committed evidence is the stronger test in any case: a
                    # trailer claim is only a claim (ADR D3 accepts forgery as
                    # out of scope), whereas this manifest has been checked
                    # against the schema and its proof shown to bind to the
                    # canonical payload for this exact source. Reading it from
                    # the frozen source's tree also establishes the artifacts
                    # are committed, which is the precondition ADR D4 attaches
                    # to creating a tag -- D4 requires the artifacts to be
                    # durable in history, and is indifferent to which commit
                    # made them so.
                    evidence = _committed_evidence(source_commit_id)
                    if evidence is None and claiming:
                        # Fall back to the worktree when a proof commit does
                        # claim the source but its artifacts are not readable
                        # from the source tree -- a reconfigured proof
                        # directory, or a later commit that deleted them.
                        evidence = _try_recover(source_commit_id)
                    if evidence is not None:
                        _recovered_by_commit[source_commit_id] = evidence
                        _tag(source_commit_id)
                        completed_tags.append(source_commit_id)
                        continue
                    if not claiming:
                        continue

                    # Diagnosis path (ADR D5). A generated proof commit claims
                    # this source but the manifest is missing or corrupt. Test the
                    # proof commit's parent(s) and each pending commit as
                    # candidates using payload binding; report the identified
                    # source and fail closed (exit 5) so a scheduler does not
                    # turn this into a silent perpetual no-op.
                    proof_dir = snapshot.layout.worktree_root / Path(
                        snapshot.config.proof.directory
                    )
                    proof_path = proof_dir / f"{source_commit_id}.ots"
                    proof_bytes: bytes | None = None
                    if proof_path.is_file():
                        read_bytes = proof_path.read_bytes()
                        if read_bytes:
                            proof_bytes = read_bytes

                    matched: str | None = None
                    if proof_bytes is not None:
                        candidate_ids: list[str] = []
                        for proof_commit_id in claiming:
                            candidate_ids.extend(
                                get_commit_parents(
                                    cwd=snapshot.layout.worktree_root,
                                    commit=proof_commit_id,
                                    process_runner=process_runner,
                                )
                            )
                        candidate_ids.extend(
                            pending.commit_id for pending in snapshot.pending
                        )
                        tested: set[str] = set()
                        for candidate_id in candidate_ids:
                            if candidate_id in tested:
                                continue
                            tested.add(candidate_id)
                            try:
                                payload = build_payload(
                                    snapshot.object_format, candidate_id
                                )
                                validate_detached_proof(proof_bytes, payload)
                                matched = candidate_id
                                break
                            except ValueError:
                                continue

                    if matched is not None:
                        raise PersistenceError(
                            f"inconsistent recovery state: proof commit(s) "
                            f"{[c[:12] for c in claiming]} claim source "
                            f"{source_commit_id[:12]} but the manifest is missing "
                            f"or corrupt; payload binding identifies the source as "
                            f"{matched[:12]} (resolve manually before rerunning)"
                        )
                    raise PersistenceError(
                        f"inconsistent recovery state: proof commit(s) "
                        f"{[c[:12] for c in claiming]} claim source "
                        f"{source_commit_id[:12]} but the manifest is missing or "
                        f"corrupt (resolve manually before rerunning)"
                    )
            return RunOutcome(tagged=tuple(completed_tags))

        # Ensure the proof directory exists before any persistence step. This
        # closes a transaction-preparation gap: injected persist callbacks (used
        # in tests) and the default callback both assume the workspace is ready.
        proof_dir = snapshot.layout.worktree_root / Path(
            snapshot.config.proof.directory
        )
        proof_dir.mkdir(parents=True, exist_ok=True)

        processed_commit_ids: list[str] = list(in_flight)
        deferred_commit_ids: tuple[str, ...] = ()
        if in_flight:
            # Do not also submit what this run's trigger selected. The trigger
            # is a statement about how long the repository has gone unstamped,
            # and an unrecorded submission is exactly the evidence that it has
            # not -- the evidence is simply not durable yet. Submitting anyway
            # would mint a fresh commitment on every blocked tick while the
            # earlier ones went on being abandoned. Once this run makes the
            # in-flight work durable, the next run derives a real baseline from
            # it and evaluates the trigger against the truth.
            deferred_commit_ids = tuple(
                pending.commit_id for pending in snapshot.decision.commits
            )
        selected = () if in_flight else snapshot.decision.commits
        for pending in selected:
            commit_id = pending.commit_id
            recovered = inspect_recovery_artifacts_if_present(
                repository_root=snapshot.layout.worktree_root,
                proof_directory=Path(snapshot.config.proof.directory),
                object_format=snapshot.object_format,
                commit_id=commit_id,
            )
            if recovered is not None:
                _recovered_by_commit[commit_id] = recovered.as_evidence(commit_id)
                proof = recovered.proof_bytes
            else:
                # A generated proof commit claiming this source without valid
                # recovery artifacts is an inconsistent state (spec section 22.3).
                claiming = find_generated_proof_commits_claiming_source(
                    cwd=snapshot.layout.worktree_root,
                    source_commit_id=commit_id,
                    ref=snapshot.source.commit_id,
                    baseline=(
                        snapshot.baseline.baseline.source_commit_id
                        if snapshot.baseline.baseline is not None
                        else None
                    ),
                    proof_directory=snapshot.config.proof.directory,
                    process_runner=process_runner,
                )
                if claiming:
                    raise InvalidRepositoryStateError(
                        f"inconsistent repository state: generated proof commit(s) "
                        f"{claiming!r} claim source {commit_id!r} but recovery "
                        f"artifacts are missing or invalid"
                    )
                payload = build_payload(snapshot.object_format, commit_id)
                proof = _submit(commit_id, payload)
                _persist(commit_id, proof)
            processed_commit_ids.append(commit_id)

        committed_commit_ids: tuple[str, ...] = ()
        if processed_commit_ids and snapshot.config.proof.commit:
            proof_directory = snapshot.config.proof.directory
            artifact_paths = [
                f"{proof_directory}/{commit_id}.{ext}"
                for commit_id in processed_commit_ids
                for ext in ("ots", "json")
            ]
            if not paths_are_committed(
                cwd=snapshot.layout.worktree_root,
                paths=artifact_paths,
                process_runner=process_runner,
            ):
                _commit(processed_commit_ids)
                committed_commit_ids = tuple(processed_commit_ids)

        for commit_id in processed_commit_ids:
            _tag(commit_id)

        return RunOutcome(
            timestamped=tuple(
                cid for cid in processed_commit_ids if cid not in in_flight
            ),
            tagged=tuple(processed_commit_ids),
            committed=committed_commit_ids,
            deferred=deferred_commit_ids,
        )
