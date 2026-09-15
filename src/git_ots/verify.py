"""Offline validation and chain-backed verification of stored proofs.

``validate_proofs`` implements the offline ``git-ots validate`` command.
``verify_proofs`` builds on that result and delegates Bitcoin verification to
the OpenTimestamps client. Neither path mutates repository artifacts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from .config import Config
from .git import (
    GitCommandError,
    GitRunner,
    enumerate_timestamp_tags,
    locate_repository,
    resolve_source_ref,
)
from .timestamp import (
    ProofParseError,
    VerificationAttempt,
    build_payload,
    classify_detached_proof,
    validate_detached_proof,
)


class ProofState(str, Enum):
    """Offline validation result for a single stored proof."""

    VALID = "valid"
    PENDING_ATTESTATION = "pending-attestation"
    ORPHANED = "orphaned"
    INVALID = "invalid"
    UNKNOWN_SOURCE = "unknown-source"


@dataclass(frozen=True, slots=True)
class ProofResult:
    """Offline validation result for one proof/manifest pair.

    ``has_timestamp_tag`` is deliberately a field rather than another
    :class:`ProofState` member. The states are mutually exclusive claims
    about the proof itself -- it binds or it does not, its source is on the
    lineage or it is not -- whereas whether the repository carries the
    matching ``ots/*`` tag is an orthogonal fact about bookkeeping. A proof
    can be orphaned *and* untagged, and folding the two into one enumeration
    would force a report to lose one of them.

    A missing tag is reported but is **not** an error and does not change any
    exit code, because it is not by itself evidence of damage: git-ots never
    pushes tags, so a fresh clone legitimately has proof commits and no tags
    at all. What it does mean is that the tag -- the only place
    ``submitted_at``, ``source_ref`` and the trigger set are recorded in a
    signed, human-readable object -- is missing, which `git-ots repair` can
    reconstruct from the manifest.
    """

    proof_path: str
    source_commit_id: str
    state: ProofState
    reason: str
    has_bitcoin_attestation: bool = False
    has_timestamp_tag: bool = True
    object_format: str = ""


class ChainState(str, Enum):
    """Bitcoin verification outcome for one stored proof."""

    VERIFIED = "verified"
    PENDING_ATTESTATION = "pending-attestation"
    VERIFICATION_FAILED = "verification-failed"
    NOT_CHECKED = "not-checked"


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Combined repository validation and Bitcoin verification outcome."""

    proof_path: str
    source_commit_id: str
    validation_state: ProofState
    state: ChainState
    reason: str


class _VerificationClient(Protocol):
    def verify(self, proof_bytes: bytes, payload: bytes) -> VerificationAttempt: ...


class _ProcessRunner(Protocol):
    def __call__(
        self, argv: list[str], *, cwd: Path, stdin: bytes | None = None
    ) -> tuple[int, str, str]: ...


def _source_commit_status(
    *,
    cwd: Path,
    source_commit_id: str,
    frozen_source_id: str,
    runner: GitRunner,
) -> ProofState:
    """Return ORPHANED, UNKNOWN_SOURCE, or VALID based on source ancestry.

    Resolvability is tested first, and separately, because
    ``merge-base --is-ancestor`` *errors* on an object the repository no longer
    has rather than answering false. Folding the two checks together would
    report a garbage-collected attested commit as ``orphaned`` -- claiming the
    proof attests to a commit that merely left the lineage, when in truth the
    commit is gone and nothing can be said about it. They are distinct states
    and must stay distinct.
    """
    try:
        runner.run(["rev-parse", "--verify", f"{source_commit_id}^{{commit}}"])
    except GitCommandError:
        return ProofState.UNKNOWN_SOURCE

    try:
        runner.run(
            [
                "merge-base",
                "--is-ancestor",
                source_commit_id,
                frozen_source_id,
            ]
        )
    except GitCommandError:
        return ProofState.ORPHANED

    return ProofState.VALID


def _validate_single_proof(
    *,
    cwd: Path,
    proof_dir: Path,
    proof_name: str,
    frozen_source_id: str,
    tagged_sources: frozenset[str],
    runner: GitRunner,
) -> ProofResult:
    """Validate one ``.ots`` proof file with its matching manifest."""
    proof_path = proof_dir / proof_name
    manifest_path = proof_dir / (proof_name[:-4] + ".json")
    proof_rel = str(proof_path.relative_to(cwd))

    if not manifest_path.is_file():
        return ProofResult(
            proof_path=proof_rel,
            source_commit_id="",
            state=ProofState.INVALID,
            reason=f"manifest missing: {manifest_path.name}",
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return ProofResult(
            proof_path=proof_rel,
            source_commit_id="",
            state=ProofState.INVALID,
            reason=f"manifest unreadable or malformed: {exc}",
        )

    if not isinstance(manifest, dict):
        return ProofResult(
            proof_path=proof_rel,
            source_commit_id="",
            state=ProofState.INVALID,
            reason="manifest is not a JSON object",
        )

    source_commit_id = manifest.get("source_commit")
    if not isinstance(source_commit_id, str):
        return ProofResult(
            proof_path=proof_rel,
            source_commit_id="",
            state=ProofState.INVALID,
            reason="manifest missing source_commit",
        )

    object_format = manifest.get("git_object_format")
    if not isinstance(object_format, str):
        return ProofResult(
            proof_path=proof_rel,
            source_commit_id=source_commit_id,
            state=ProofState.INVALID,
            reason="manifest missing git_object_format",
        )

    try:
        payload = build_payload(object_format, source_commit_id)
    except Exception as exc:  # noqa: BLE001
        return ProofResult(
            proof_path=proof_rel,
            source_commit_id=source_commit_id,
            state=ProofState.INVALID,
            reason=f"cannot build canonical payload: {exc}",
        )

    try:
        proof_bytes = proof_path.read_bytes()
        validate_detached_proof(proof_bytes, payload)
        attestation_state = classify_detached_proof(proof_bytes)
    except (OSError, ValueError, ProofParseError) as exc:
        return ProofResult(
            proof_path=proof_rel,
            source_commit_id=source_commit_id,
            state=ProofState.INVALID,
            reason=f"proof does not bind to source commit: {exc}",
        )

    has_bitcoin_attestation = attestation_state == "valid"
    ancestry_state = _source_commit_status(
        cwd=cwd,
        source_commit_id=source_commit_id,
        frozen_source_id=frozen_source_id,
        runner=runner,
    )
    if ancestry_state in (ProofState.ORPHANED, ProofState.UNKNOWN_SOURCE):
        reason = {
            ProofState.ORPHANED: "attested commit is not an ancestor of the source ref",
            ProofState.UNKNOWN_SOURCE: "attested commit does not resolve in this repository",
        }[ancestry_state]
        return ProofResult(
            proof_path=proof_rel,
            source_commit_id=source_commit_id,
            state=ancestry_state,
            reason=reason,
            has_bitcoin_attestation=has_bitcoin_attestation,
            has_timestamp_tag=source_commit_id in tagged_sources,
            object_format=object_format,
        )

    if has_bitcoin_attestation:
        return ProofResult(
            proof_path=proof_rel,
            source_commit_id=source_commit_id,
            state=ProofState.VALID,
            reason="Bitcoin block header attestation present but not chain-verified",
            has_bitcoin_attestation=True,
            has_timestamp_tag=source_commit_id in tagged_sources,
            object_format=object_format,
        )

    return ProofResult(
        proof_path=proof_rel,
        source_commit_id=source_commit_id,
        state=ProofState.PENDING_ATTESTATION,
        reason="proof contains only non-Bitcoin attestations",
        has_timestamp_tag=source_commit_id in tagged_sources,
        object_format=object_format,
    )


def validate_proofs(
    *,
    cwd: Path,
    config: Config,
    proof_directories: tuple[Path, ...] | None = None,
    process_runner: _ProcessRunner | None = None,
) -> list[ProofResult]:
    """Validate every stored proof in the configured proof directory, offline.

    Resolves the repository, source ref, and object format, then scans the
    proof directory for ``<commit-id>.ots`` files. Each proof is checked
    against its manifest's ``source_commit`` for ancestry and binding, and
    against the repository's ``ots/*`` tags for the presence of the matching
    timestamp tag (``ProofResult.has_timestamp_tag``).

    That last check exists because proof state and tag state could previously
    diverge silently and permanently: an interrupted run could leave a fully
    anchored proof with no tag, and every diagnostic the tool offered reported
    the repository as healthy. It is reported, never treated as an error --
    see :class:`ProofResult`.

    This function performs no writes, locks, network access, or OpenTimestamps
    CLI invocations.
    """
    layout = locate_repository(cwd=cwd, process_runner=process_runner)
    source = resolve_source_ref(
        cwd=layout.worktree_root,
        ref=config.git.source_ref,
        process_runner=process_runner,
    )
    runner = GitRunner(cwd=layout.worktree_root, process_runner=process_runner)

    if proof_directories is None:
        conventional = layout.worktree_root / ".opentimestamps"
        if conventional.is_dir() and any(conventional.glob("*.ots")):
            proof_directories = (Path(".opentimestamps"),)
        else:
            tracked = runner.run(["ls-files", "-z", "--", "*.ots"]).stdout
            proof_directories = tuple(
                sorted({Path(path).parent for path in tracked.split("\0") if path})
            )

    # Enumerated once for the whole scan rather than per proof: the tag list
    # is a single `git for-each-ref`, and asking per proof would make the
    # subprocess cost grow with the number of stamps ever made -- the same
    # trap ADR 0001 D6's "validation must be bounded" refinement records.
    tagged_sources = frozenset(
        tag.commit_id
        for tag in enumerate_timestamp_tags(
            cwd=layout.worktree_root,
            tag_prefix=config.git.tag_prefix,
            process_runner=process_runner,
        )
    )

    results: list[ProofResult] = []
    for relative_dir in proof_directories:
        proof_dir = layout.worktree_root / relative_dir
        if not proof_dir.is_dir():
            continue
        for proof_path in sorted(proof_dir.iterdir()):
            if not proof_path.is_file() or proof_path.suffix != ".ots":
                continue
            results.append(
                _validate_single_proof(
                    cwd=layout.worktree_root,
                    proof_dir=proof_dir,
                    proof_name=proof_path.name,
                    frozen_source_id=source.commit_id,
                    tagged_sources=tagged_sources,
                    runner=runner,
                )
            )

    return results


def verify_proofs(
    *,
    cwd: Path,
    config: Config,
    client: _VerificationClient,
    proof_directories: tuple[Path, ...] | None = None,
    process_runner: _ProcessRunner | None = None,
) -> list[VerificationResult]:
    """Verify every Bitcoin-attested proof through an OpenTimestamps client.

    Local validation always runs first. Malformed proofs are never handed to
    the external client, while orphaned and unknown source commits can still
    have their Bitcoin anchors verified because the canonical payload is fully
    determined by the manifest's commit identifier.
    """
    validations = validate_proofs(
        cwd=cwd,
        config=config,
        proof_directories=proof_directories,
        process_runner=process_runner,
    )
    if not validations:
        return []

    layout = locate_repository(cwd=cwd, process_runner=process_runner)
    results: list[VerificationResult] = []
    for validation in validations:
        if validation.state is ProofState.INVALID:
            results.append(
                VerificationResult(
                    proof_path=validation.proof_path,
                    source_commit_id=validation.source_commit_id,
                    validation_state=validation.state,
                    state=ChainState.NOT_CHECKED,
                    reason=validation.reason,
                )
            )
            continue
        if not validation.has_bitcoin_attestation:
            results.append(
                VerificationResult(
                    proof_path=validation.proof_path,
                    source_commit_id=validation.source_commit_id,
                    validation_state=validation.state,
                    state=ChainState.PENDING_ATTESTATION,
                    reason="proof has no Bitcoin attestation to verify",
                )
            )
            continue

        try:
            proof_bytes = (layout.worktree_root / validation.proof_path).read_bytes()
            payload = build_payload(
                validation.object_format, validation.source_commit_id
            )
        except (OSError, ValueError) as exc:
            results.append(
                VerificationResult(
                    proof_path=validation.proof_path,
                    source_commit_id=validation.source_commit_id,
                    validation_state=validation.state,
                    state=ChainState.NOT_CHECKED,
                    reason=f"proof changed after validation: {exc}",
                )
            )
            continue

        attempt = client.verify(proof_bytes, payload)
        state = (
            ChainState.VERIFIED if attempt.verified else ChainState.VERIFICATION_FAILED
        )
        reason = attempt.detail or (
            "Bitcoin attestation confirmed by the configured node"
            if attempt.verified
            else "OpenTimestamps verification failed"
        )
        results.append(
            VerificationResult(
                proof_path=validation.proof_path,
                source_commit_id=validation.source_commit_id,
                validation_state=validation.state,
                state=state,
                reason=reason,
            )
        )

    return results
