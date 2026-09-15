"""Upgrade stored OpenTimestamps proofs to Bitcoin block attestations.

Implements the ``git-ots upgrade`` subcommand. A freshly submitted proof
carries only calendar promises; the Bitcoin block header attestation becomes
available once the calendar's transaction has confirmed, and must be fetched
and written into the proof file to make it independently verifiable offline.

This is the one command that both contacts the network and rewrites existing
proof files, so it is never part of a scheduled ``run``: an operator invokes
it explicitly. ``run`` remains submission-only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from .anchors import collectible_calendars, describe_anchors
from .config import Config
from .git import (
    InvalidRepositoryStateError,
    RepositoryLock,
    assert_committable_state,
    assert_worktree_present,
    create_upgrade_commit,
    detect_object_format,
    is_worktree_clean,
    locate_repository,
    read_tree_bytes,
    stage_paths,
)
from .proof_tree import (
    Attestation,
    Branch,
    all_attestations,
    merge_node,
    parse_calendar_response,
    parse_proof,
)
from .servers import fetch_calendar_timestamp
from .timestamp import (
    OpenTimestampsCli,
    PersistenceError,
    ProofParseError,
    UpgradeAttempt,
    build_payload,
    classify_detached_proof,
    persist_upgraded_proof,
    validate_detached_proof,
)


class UpgradeState(str, Enum):
    """Per-proof outcome of an upgrade pass."""

    UPGRADED = "upgraded"
    STILL_PENDING = "still-pending"
    ALREADY_COMPLETE = "already-complete"
    WOULD_UPGRADE = "would-upgrade"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class UpgradeResult:
    """Outcome for one stored proof.

    ``anchored_calendars`` of ``promised_calendars`` reports how many of the
    calendars a proof was submitted to actually published an anchor. It is not
    a progress bar: the OpenTimestamps client stops upgrading at the first
    Bitcoin attestation, so a complete proof routinely sits below its
    denominator forever. The ratio measures realized redundancy, not work
    outstanding.
    """

    proof_path: str
    source_commit_id: str
    state: UpgradeState
    reason: str
    anchored_calendars: int = 0
    promised_calendars: int = 0


@dataclass(frozen=True, slots=True)
class UpgradeReport:
    """Outcome of a whole upgrade pass, plus the commit it produced if any."""

    results: tuple[UpgradeResult, ...]
    commit_id: str | None


class _UpgradeClient(Protocol):
    def upgrade(self, proof_bytes: bytes, payload: bytes) -> UpgradeAttempt: ...


class _CalendarFetcher(Protocol):
    def __call__(self, url: str, commitment: bytes) -> tuple[bytes | None, str]: ...


class _ProcessRunner(Protocol):
    def __call__(
        self, argv: list[str], *, cwd: Path, stdin: bytes | None = None
    ) -> tuple[int, str, str]: ...


class _ProcessRunnerBytes(Protocol):
    def __call__(
        self, argv: list[str], *, cwd: Path, stdin: bytes | None = None
    ) -> tuple[int, bytes, bytes]: ...


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A stored proof that parses, binds to its source, and lacks Bitcoin."""

    proof_path: Path
    proof_rel: str
    source_commit_id: str
    payload: bytes
    proof_bytes: bytes
    promised_calendars: int
    anchored: bool


def _is_additive_upgrade(*, committed: bytes, worktree: bytes, payload: bytes) -> bool:
    """Return whether ``worktree`` preserves every completed attestation.

    Binding both versions to the source is necessary but not sufficient: two
    different valid proofs can bind to the same payload. Calendar promises are
    deliberately excluded from the preservation check because the reference
    client replaces a promise with the timestamp subtree returned by that
    calendar. Every non-pending attestation, including every Bitcoin anchor,
    must remain at the same operation path.
    """
    try:
        validate_detached_proof(committed, payload)
        validate_detached_proof(worktree, payload)
        committed_proof = parse_proof(committed)
        worktree_proof = parse_proof(worktree)
    except (ValueError, ProofParseError):
        return False
    if committed_proof.header != worktree_proof.header:
        return False

    pending_tag = bytes.fromhex("83dfe30d2ef90c8e")

    def completed(node, path: tuple[bytes, ...] = ()) -> set[tuple]:
        found: set[tuple] = set()
        for item in node.items:
            if isinstance(item, Attestation):
                if item.tag != pending_tag:
                    found.add((path, item.tag, item.payload))
            elif isinstance(item, Branch):
                found.update(completed(item.child, (*path, item.operation)))
        return found

    return completed(committed_proof.root) <= completed(worktree_proof.root)


def _classify_stored_proof(
    *,
    cwd: Path,
    proof_path: Path,
    object_format: str,
) -> tuple[_Candidate | None, UpgradeResult | None]:
    """Return an upgrade candidate, or a terminal result for this proof.

    Ancestry is deliberately not considered. A proof whose source commit is no
    longer on the source ref's lineage -- ``orphaned`` in ``validate`` terms --
    is still a real timestamp of a real commit, and completing it costs one
    calendar request. Selection here is purely "does this proof still lack a
    Bitcoin attestation".
    """
    proof_rel = str(proof_path.relative_to(cwd))
    manifest_path = proof_path.with_suffix(".json")

    if not manifest_path.is_file():
        return None, UpgradeResult(
            proof_path=proof_rel,
            source_commit_id="",
            state=UpgradeState.SKIPPED,
            reason=f"manifest missing: {manifest_path.name}",
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, UpgradeResult(
            proof_path=proof_rel,
            source_commit_id="",
            state=UpgradeState.SKIPPED,
            reason=f"manifest unreadable or malformed: {exc}",
        )

    source_commit_id = (
        manifest.get("source_commit") if isinstance(manifest, dict) else None
    )
    if not isinstance(source_commit_id, str):
        return None, UpgradeResult(
            proof_path=proof_rel,
            source_commit_id="",
            state=UpgradeState.SKIPPED,
            reason="manifest missing source_commit",
        )

    try:
        payload = build_payload(object_format, source_commit_id)
        proof_bytes = proof_path.read_bytes()
        validate_detached_proof(proof_bytes, payload)
        state = classify_detached_proof(proof_bytes)
    except (OSError, ValueError, ProofParseError) as exc:
        return None, UpgradeResult(
            proof_path=proof_rel,
            source_commit_id=source_commit_id,
            state=UpgradeState.SKIPPED,
            reason=f"proof does not bind to source commit: {exc}",
        )

    described = describe_anchors(proof_bytes)

    # An already-anchored proof is still a candidate. The calendars it never
    # collected may have published attestations of their own, and omitting
    # them makes the stored proof claim less than actually happened.
    return (
        _Candidate(
            proof_path=proof_path,
            proof_rel=proof_rel,
            source_commit_id=source_commit_id,
            payload=payload,
            proof_bytes=proof_bytes,
            promised_calendars=len(described.promised_calendars),
            anchored=state == "valid",
        ),
        None,
    )


def _collect_from_calendars(
    *,
    proof_bytes: bytes,
    payload: bytes,
    fetcher: _CalendarFetcher,
) -> tuple[bytes | None, tuple[str, ...], tuple[str, ...]]:
    """Ask every unfulfilled calendar for the attestation it filed.

    Returns the rewritten proof (or ``None`` when nothing was added), the
    calendars that contributed, and diagnostics for those that could not.

    The OpenTimestamps client stops upgrading at a proof's first Bitcoin
    attestation, so a calendar that confirmed later is never asked again and
    its attestation never reaches the file. This asks directly, which is the
    only way the stored proof can reflect what actually happened.
    """
    proof = parse_proof(proof_bytes)
    targets = collectible_calendars(proof)
    if not targets:
        return None, (), ()

    before = sorted(all_attestations(proof.root))
    collected: list[str] = []
    notes: list[str] = []
    added_total = 0

    for target in targets:
        body, detail = fetcher(target.url, target.commitment)
        if body is None:
            notes.append(f"{target.url}: {detail}")
            continue
        try:
            response = parse_calendar_response(body)
        except ProofParseError as exc:
            notes.append(f"{target.url}: unusable response ({exc})")
            continue
        added = merge_node(target.node, response)
        if added:
            collected.append(target.url)
            added_total += added

    if not added_total:
        return None, (), tuple(notes)

    merged = proof.serialize()

    # Two invariants before these bytes may replace a stored proof: it still
    # commits to the same source commit, and it attests to everything it
    # attested to before. A merge may only add.
    validate_detached_proof(merged, payload)
    after = sorted(all_attestations(parse_proof(merged).root))
    missing = [item for item in before if item not in after]
    if missing:
        raise PersistenceError(
            "refusing to store a merged proof that dropped "
            f"{len(missing)} existing attestation(s)"
        )

    return merged, tuple(collected), tuple(notes)


def _process_candidate(
    *,
    candidate: _Candidate,
    client: _UpgradeClient,
    fetcher: _CalendarFetcher,
    repository_root: Path,
    proof_directory: str,
    object_format: str,
) -> UpgradeResult:
    """Complete one proof: upgrade it if pending, then collect what is owed."""
    current = candidate.proof_bytes
    reasons: list[str] = []
    pending_detail = ""

    if not candidate.anchored:
        try:
            attempt = client.upgrade(current, candidate.payload)
        except KeyboardInterrupt:
            # In flight only while upgrade() is still on the stack -- see
            # orchestration.py:_default_submit's identical comment. ``client``
            # is a ``_UpgradeClient`` Protocol, so a test double need not carry
            # this cleanup primitive; only the production OpenTimestampsCli
            # does, and that is the one with a live child to terminate.
            terminate = getattr(client, "terminate_active_child", None)
            if terminate is not None:
                terminate()
            raise
        if attempt.upgraded is None:
            pending_detail = attempt.detail
        else:
            current = attempt.upgraded
            # A calendar can hand back attestations that still fall short of a
            # Bitcoin block header -- one calendar completing while another
            # lags. Those bytes are worth keeping, but the proof is not yet
            # independently verifiable, and saying so is the honest report.
            if classify_detached_proof(current) == "valid":
                reasons.append("Bitcoin block header attestation added")
            else:
                reasons.append(
                    "attestations added, but still no Bitcoin block attestation"
                )

    merged, collected, notes = _collect_from_calendars(
        proof_bytes=current, payload=candidate.payload, fetcher=fetcher
    )
    if merged is not None:
        current = merged
        names = ", ".join(collected)
        reasons.append(f"collected attestations from {names}")

    if current != candidate.proof_bytes:
        persist_upgraded_proof(
            repository_root=repository_root,
            proof_directory=Path(proof_directory),
            object_format=object_format,
            commit_id=candidate.source_commit_id,
            proof_bytes=current,
        )

    described = describe_anchors(current)
    counts = {
        "anchored_calendars": len(described.anchoring_calendars),
        "promised_calendars": len(described.promised_calendars),
    }

    if current != candidate.proof_bytes:
        return UpgradeResult(
            proof_path=candidate.proof_rel,
            source_commit_id=candidate.source_commit_id,
            state=UpgradeState.UPGRADED,
            reason="; ".join(reasons),
            **counts,
        )

    if described.anchors:
        reason = "Bitcoin block header attestation already present"
        if notes:
            reason = f"{reason}; nothing further available ({'; '.join(notes)})"
        return UpgradeResult(
            proof_path=candidate.proof_rel,
            source_commit_id=candidate.source_commit_id,
            state=UpgradeState.ALREADY_COMPLETE,
            reason=reason,
            **counts,
        )

    reason = "no new attestations available yet"
    if pending_detail:
        reason = f"{reason} ({pending_detail})"
    return UpgradeResult(
        proof_path=candidate.proof_rel,
        source_commit_id=candidate.source_commit_id,
        state=UpgradeState.STILL_PENDING,
        reason=reason,
        **counts,
    )


def upgrade_proofs(
    *,
    cwd: Path,
    config: Config,
    dry_run: bool = False,
    client: _UpgradeClient | None = None,
    fetcher: _CalendarFetcher | None = None,
    process_runner: _ProcessRunner | None = None,
    process_runner_bytes: _ProcessRunnerBytes | None = None,
) -> UpgradeReport:
    """Upgrade every stored proof that still lacks a Bitcoin attestation.

    Scans the configured proof directory, asks the OpenTimestamps client to
    complete each pending proof, and atomically rewrites the ones that gained
    attestations. When ``[proof] commit`` is enabled the changed ``.ots``
    files are committed as a single generated commit.

    ``dry_run`` reports what would be attempted and performs no network access
    and no writes. Malformed or unbound proofs are reported and left untouched
    -- this command never repairs, deletes, or resubmits anything.
    """
    layout = locate_repository(cwd=cwd, process_runner=process_runner)
    # A bare repository has no working tree: worktree_root falls back to the
    # Git directory (see locate_repository's docstring), so an unguarded
    # upgrade would rewrite proof files, create the lock file, and commit
    # generated changes straight into `.git`. Refuse before any of that,
    # including before the lock acquired below -- this command is a write
    # path even under dry_run, since the lock file is created unconditionally.
    assert_worktree_present(layout, operation="upgrade")
    object_format = detect_object_format(
        cwd=layout.worktree_root, process_runner=process_runner
    )
    repository_root = layout.worktree_root

    proof_dir = repository_root / config.proof.directory
    if not proof_dir.is_dir():
        return UpgradeReport(results=(), commit_id=None)

    with RepositoryLock(common_git_dir=layout.common_git_dir):
        # Both checks run before anything is rewritten. An upgrade dirties the
        # proof files themselves, so deferring the cleanliness check until
        # commit time would refuse the run only after mutating the worktree.
        if not dry_run and config.proof.commit:
            assert_committable_state(
                cwd=repository_root,
                require_symbolic_head=True,
                process_runner=process_runner,
            )
            if config.git.require_clean_worktree and not is_worktree_clean(
                cwd=repository_root, process_runner=process_runner
            ):
                raise InvalidRepositoryStateError(
                    "worktree has uncommitted changes; commit or stash them, or "
                    "set [git] require_clean_worktree = false -- generated "
                    "commits name their paths explicitly, so unrelated changes "
                    "are never included"
                )

        results: list[UpgradeResult] = []
        candidates: list[_Candidate] = []
        recovered_sources: set[str] = set()
        for proof_path in sorted(proof_dir.iterdir()):
            if not proof_path.is_file() or proof_path.suffix != ".ots":
                continue
            candidate, result = _classify_stored_proof(
                cwd=repository_root,
                proof_path=proof_path,
                object_format=object_format.value,
            )
            if result is not None:
                results.append(result)
            if candidate is not None:
                committed = read_tree_bytes(
                    cwd=repository_root,
                    commit="HEAD",
                    path=candidate.proof_rel,
                    process_runner_bytes=process_runner_bytes,
                )
                if (
                    config.proof.commit
                    and committed is not None
                    and committed != candidate.proof_bytes
                ):
                    if not _is_additive_upgrade(
                        committed=committed,
                        worktree=candidate.proof_bytes,
                        payload=candidate.payload,
                    ):
                        described = describe_anchors(candidate.proof_bytes)
                        results.append(
                            UpgradeResult(
                                proof_path=candidate.proof_rel,
                                source_commit_id=candidate.source_commit_id,
                                state=UpgradeState.SKIPPED,
                                reason=(
                                    "worktree proof differs from HEAD but is not an "
                                    "additive upgrade; left untouched"
                                ),
                                anchored_calendars=len(described.anchoring_calendars),
                                promised_calendars=candidate.promised_calendars,
                            )
                        )
                        continue
                    recovered_sources.add(candidate.source_commit_id)
                candidates.append(candidate)

        if dry_run:
            for candidate in candidates:
                outstanding = collectible_calendars(parse_proof(candidate.proof_bytes))
                described = describe_anchors(candidate.proof_bytes)
                if not candidate.anchored:
                    reason = "pending attestation; calendars would be contacted"
                elif outstanding:
                    names = ", ".join(sorted(c.url for c in outstanding))
                    reason = f"anchored; would ask {names} for what they filed"
                else:
                    results.append(
                        UpgradeResult(
                            proof_path=candidate.proof_rel,
                            source_commit_id=candidate.source_commit_id,
                            state=UpgradeState.ALREADY_COMPLETE,
                            reason="anchored by every calendar that was asked",
                            anchored_calendars=len(described.anchoring_calendars),
                            promised_calendars=len(described.promised_calendars),
                        )
                    )
                    continue
                results.append(
                    UpgradeResult(
                        proof_path=candidate.proof_rel,
                        source_commit_id=candidate.source_commit_id,
                        state=UpgradeState.WOULD_UPGRADE,
                        reason=reason,
                        anchored_calendars=len(described.anchoring_calendars),
                        promised_calendars=len(described.promised_calendars),
                    )
                )
            return UpgradeReport(results=tuple(_ordered(results)), commit_id=None)

        _client = (
            client
            if client is not None
            else OpenTimestampsCli(
                config.opentimestamps.command, limit=config.limits.ots_timeout
            )
        )

        _fetcher = fetcher if fetcher is not None else fetch_calendar_timestamp

        upgraded_sources: list[str] = []
        for candidate in candidates:
            result = _process_candidate(
                candidate=candidate,
                client=_client,
                fetcher=_fetcher,
                repository_root=repository_root,
                proof_directory=config.proof.directory,
                object_format=object_format.value,
            )
            results.append(result)
            if candidate.source_commit_id in recovered_sources:
                if result.state is not UpgradeState.UPGRADED:
                    result = UpgradeResult(
                        proof_path=result.proof_path,
                        source_commit_id=result.source_commit_id,
                        state=UpgradeState.UPGRADED,
                        reason=(
                            "recovered an additive proof upgrade left "
                            "uncommitted by an earlier invocation; " + result.reason
                        ),
                        anchored_calendars=result.anchored_calendars,
                        promised_calendars=result.promised_calendars,
                    )
                    results[-1] = result
                upgraded_sources.append(candidate.source_commit_id)
            elif result.state is UpgradeState.UPGRADED:
                upgraded_sources.append(candidate.source_commit_id)

        commit_id = None
        if upgraded_sources and config.proof.commit:
            paths = [
                f"{config.proof.directory}/{source_id}.ots"
                for source_id in upgraded_sources
            ]
            stage_paths(cwd=repository_root, paths=paths, process_runner=process_runner)
            commit_id = create_upgrade_commit(
                cwd=repository_root,
                source_commit_ids=upgraded_sources,
                proof_directory=config.proof.directory,
                paths=paths,
                sign=config.git.signs_generated_objects,
                process_runner=process_runner,
            )

        return UpgradeReport(results=tuple(_ordered(results)), commit_id=commit_id)


def _ordered(results: list[UpgradeResult]) -> list[UpgradeResult]:
    """Sort results by proof path so output is stable across runs."""
    return sorted(results, key=lambda result: result.proof_path)
