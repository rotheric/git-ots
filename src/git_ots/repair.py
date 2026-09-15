"""Reconcile stored proofs with the timestamp tags that should accompany them.

``git-ots run`` completes the bookkeeping of the stamps it is currently
concerned with -- the baseline and the pending sources. That is enough to keep
a scheduled repository converging, but it deliberately does not sweep the whole
proof directory, so a proof that fell out of that window during an interrupted
run has no route back.

This module is that route. It creates the missing annotated tag for any stored
proof whose source is on the current lineage, reconstructing the annotation
from the proof's own sidecar manifest -- which is where ``submitted_at``,
``source_ref`` and the trigger set live, and the only place they live. Nothing
here re-submits, moves an existing tag, or writes a proof file.

The safety property that makes this sound is the same one ADR 0001 D5 relies
on: the manifest is *checked*, not trusted. Before a tag is created, the
detached proof must be shown to derive from the exact canonical payload
``git:<fmt>:<source>\\n``, which a manifest lying about its source cannot fake.
The recovery time is never substituted for ``submitted_at``, because doing so
would forge the attestation time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .config import Config
from .git import (
    GitCommandError,
    GitRunner,
    RepositoryLock,
    assert_worktree_present,
    create_timestamp_tag,
    detect_object_format,
    enumerate_timestamp_tags,
    locate_repository,
    resolve_source_ref,
)
from .timestamp import (
    PersistenceError,
    RecoveryValidationError,
    inspect_recovery_artifacts,
)


class RepairAction(str, Enum):
    """What repair did, or declined to do, for one stored proof."""

    TAGGED = "tagged"
    WOULD_TAG = "would-tag"
    ALREADY_TAGGED = "already-tagged"
    SKIPPED_UNRESOLVED = "skipped-unresolved-source"
    SKIPPED_OFF_LINEAGE = "skipped-off-lineage"
    UNREPAIRABLE = "unrepairable"


@dataclass(frozen=True, slots=True)
class RepairResult:
    """Outcome of considering one stored proof for tag repair."""

    proof_path: str
    source_commit_id: str
    action: RepairAction
    detail: str
    tag_name: str | None = None


def _source_is_on_lineage(
    *, runner: GitRunner, source_commit_id: str, frozen_source_id: str
) -> RepairAction | None:
    """Return the skip reason for ``source_commit_id``, or ``None`` to proceed."""
    try:
        runner.run(["rev-parse", "--verify", f"{source_commit_id}^{{commit}}"])
    except GitCommandError:
        return RepairAction.SKIPPED_UNRESOLVED
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
        return RepairAction.SKIPPED_OFF_LINEAGE
    return None


def repair_missing_tags(
    *,
    cwd: Path,
    config: Config,
    dry_run: bool = False,
    process_runner=None,
) -> list[RepairResult]:
    """Create the missing timestamp tag for every repairable stored proof.

    Proofs whose source is already tagged are left untouched (tags are
    immutable to git-ots -- spec section 12). Proofs whose source does not
    resolve, or is not an ancestor of the frozen source ref, are reported and
    skipped rather than tagged: minting an ``ots/*`` tag on an abandoned
    lineage would change what the baseline search and the ``every_commit``
    rewritten-history guard see, and a repair command must not move policy
    ground under the operator. ``git-ots validate`` already names those
    proofs (``orphaned`` / ``unknown-source``); resolving them is a decision,
    not a repair.

    With ``dry_run`` nothing is created and every action is reported as
    ``would-tag``. The repository lock is still taken, so a dry run cannot
    race a concurrent ``run`` into reporting state that is already stale.
    """
    layout = locate_repository(cwd=cwd, process_runner=process_runner)
    assert_worktree_present(layout, operation="repair")
    object_format = detect_object_format(
        cwd=layout.worktree_root, process_runner=process_runner
    )
    proof_dir = layout.worktree_root / config.proof.directory
    if not proof_dir.is_dir():
        return []

    results: list[RepairResult] = []
    with RepositoryLock(common_git_dir=layout.common_git_dir):
        source = resolve_source_ref(
            cwd=layout.worktree_root,
            ref=config.git.source_ref,
            process_runner=process_runner,
        )
        runner = GitRunner(cwd=layout.worktree_root, process_runner=process_runner)
        tagged_sources = {
            tag.commit_id
            for tag in enumerate_timestamp_tags(
                cwd=layout.worktree_root,
                tag_prefix=config.git.tag_prefix,
                process_runner=process_runner,
            )
        }

        for proof_path in sorted(proof_dir.iterdir()):
            if not proof_path.is_file() or proof_path.suffix != ".ots":
                continue
            source_commit_id = proof_path.stem
            proof_rel = str(proof_path.relative_to(layout.worktree_root))

            if source_commit_id in tagged_sources:
                results.append(
                    RepairResult(
                        proof_path=proof_rel,
                        source_commit_id=source_commit_id,
                        action=RepairAction.ALREADY_TAGGED,
                        detail="a timestamp tag already names this source",
                    )
                )
                continue

            skip = _source_is_on_lineage(
                runner=runner,
                source_commit_id=source_commit_id,
                frozen_source_id=source.commit_id,
            )
            if skip is not None:
                detail = {
                    RepairAction.SKIPPED_UNRESOLVED: (
                        "attested commit does not resolve in this repository"
                    ),
                    RepairAction.SKIPPED_OFF_LINEAGE: (
                        "attested commit is not an ancestor of the source ref"
                    ),
                }[skip]
                results.append(
                    RepairResult(
                        proof_path=proof_rel,
                        source_commit_id=source_commit_id,
                        action=skip,
                        detail=detail,
                    )
                )
                continue

            try:
                artifacts = inspect_recovery_artifacts(
                    repository_root=layout.worktree_root,
                    proof_directory=Path(config.proof.directory),
                    object_format=object_format.value,
                    commit_id=source_commit_id,
                )
            except (PersistenceError, RecoveryValidationError) as exc:
                # Fail-visible, not fail-closed: one unrepairable proof must
                # not stop the others from being repaired, and the annotation
                # simply cannot be reconstructed without a manifest that
                # survives validation -- `submitted_at` has no other source
                # and must never be manufactured (ADR 0001 D5).
                results.append(
                    RepairResult(
                        proof_path=proof_rel,
                        source_commit_id=source_commit_id,
                        action=RepairAction.UNREPAIRABLE,
                        detail=str(exc),
                    )
                )
                continue

            evidence = artifacts.as_evidence(source_commit_id)
            if dry_run:
                results.append(
                    RepairResult(
                        proof_path=proof_rel,
                        source_commit_id=source_commit_id,
                        action=RepairAction.WOULD_TAG,
                        detail=(
                            f"submitted at {evidence.submitted_at:%Y-%m-%dT%H:%M:%SZ}, "
                            f"triggers: {' '.join(sorted(evidence.triggers))}"
                        ),
                    )
                )
                continue

            tag_name = create_timestamp_tag(
                cwd=layout.worktree_root,
                source_commit_id=source_commit_id,
                tag_prefix=config.git.tag_prefix,
                submitted_at=evidence.submitted_at,
                proof=f"{config.proof.directory}/{evidence.proof_name}",
                triggers=evidence.triggers,
                sign=config.git.signs_generated_objects,
                process_runner=process_runner,
            )
            results.append(
                RepairResult(
                    proof_path=proof_rel,
                    source_commit_id=source_commit_id,
                    action=RepairAction.TAGGED,
                    detail="tag reconstructed from the stored manifest",
                    tag_name=tag_name,
                )
            )

    return results
