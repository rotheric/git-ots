from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .git import InvalidRepositoryStateError

if TYPE_CHECKING:
    from .config import PolicyConfig


def _require_aware(name: str, value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value


def _require_aware_optional(name: str, value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _require_aware(name, value)


@dataclass(frozen=True, order=True)
class PendingCommit:
    """A meaningful source commit newer than the latest timestamped baseline.

    Ordered by committer timestamp so chronological comparisons and sorting
    match repository history semantics.
    """

    commit_id: str = field(compare=False)
    committer_time: datetime

    def __post_init__(self) -> None:
        _require_aware("committer_time", self.committer_time)


@dataclass(frozen=True)
class PolicyState:
    """Pure-policy input captured at the start of an invocation."""

    now: datetime
    pending_commits: tuple[PendingCommit, ...] = ()
    last_fixed_time_occurrence: datetime | None = None
    has_baseline: bool = True
    has_abandoned_tags: bool = False

    def __post_init__(self) -> None:
        _require_aware("now", self.now)
        _require_aware_optional(
            "last_fixed_time_occurrence", self.last_fixed_time_occurrence
        )


@dataclass(frozen=True)
class Decision:
    """Pure-policy output describing which commits to timestamp and why."""

    commits: tuple[PendingCommit, ...] = ()
    triggers: frozenset[str] = frozenset()


def most_recent_fixed_time_occurrence(
    now: datetime, wall_time: time, tz: ZoneInfo
) -> datetime:
    """Return the most recent scheduled occurrence at or before ``now``.

    The occurrence is computed in the configured timezone: take ``now`` in
    that zone, combine today's local date with ``wall_time``, and step back
    one local day if that candidate is still in the future.

    Daylight-saving transitions are normalized so each local date yields
    exactly one valid aware occurrence: a non-existent local time (spring
    forward) is snapped forward to the first valid instant by round-tripping
    through UTC, and an ambiguous local time (fall back) resolves to the
    earlier of the two instants (``fold=0``).
    """
    _require_aware("now", now)
    local_now = now.astimezone(tz)
    candidate = _local_occurrence(local_now.date(), wall_time, tz)
    if candidate > local_now:
        candidate = _local_occurrence(
            local_now.date() - timedelta(days=1), wall_time, tz
        )
    return candidate


def _local_occurrence(local_date, wall_time: time, tz: ZoneInfo) -> datetime:
    candidate = datetime.combine(local_date, wall_time, tzinfo=tz)
    round_tripped = candidate.astimezone(ZoneInfo("UTC")).astimezone(tz)
    if round_tripped.replace(fold=0) != candidate.replace(fold=0):
        # Non-existent local time (spring-forward gap): use the first valid
        # instant after the gap.
        return round_tripped
    return candidate


def _latest_pending(pending: tuple[PendingCommit, ...]) -> PendingCommit:
    """Return the newest pending commit, breaking timestamp ties by position.

    ``pending`` is in oldest-to-newest repository order (the
    :func:`git_ots.git.filter_pending_meaningful_commits` contract). Plain
    ``max`` would return the *first* maximal element, selecting the earliest
    commit in repository order among those sharing the newest committer
    timestamp — and equal committer timestamps are routine, because ``git
    rebase`` stamps every replayed commit with a single wall-clock committer
    date. Breaking ties by position selects the commit closest to the source
    tip, so the stamp covers the latest pending state.
    """
    latest_index = max(
        range(len(pending)),
        key=lambda i: (pending[i].committer_time, i),
    )
    return pending[latest_index]


def evaluate_policy(state: PolicyState, config: PolicyConfig) -> Decision:
    """Evaluate configured policies against the pending commits.

    When nothing is pending, no policy can fire and the decision is empty.
    """
    if config.every_commit and state.has_abandoned_tags:
        raise InvalidRepositoryStateError(
            "refusing to replay rewritten history: every_commit is enabled but "
            "no timestamp tag is an ancestor of the current source; existing "
            "tags are on an abandoned lineage"
        )
    if not state.pending_commits:
        return Decision()
    if config.every_commit:
        if not state.has_baseline:
            if config.initial_history == "latest":
                latest = _latest_pending(state.pending_commits)
                return Decision(
                    commits=(latest,),
                    triggers=frozenset({"every_commit"}),
                )
            # initial_history == "all": opt-in to timestamping every
            # meaningful pending commit so the full initial history is
            # covered, not just the latest one.
            return Decision(
                commits=state.pending_commits,
                triggers=frozenset({"every_commit"}),
            )
        return Decision(
            commits=state.pending_commits,
            triggers=frozenset({"every_commit"}),
        )
    triggers: set[str] = set()
    if config.max_age is not None:
        oldest = min(state.pending_commits)
        if state.now >= oldest.committer_time + config.max_age:
            triggers.add("max_age")
    if config.fixed_time is not None:
        occurrence = most_recent_fixed_time_occurrence(
            state.now, config.fixed_time, config.timezone
        )
        if (
            state.last_fixed_time_occurrence is None
            or occurrence > state.last_fixed_time_occurrence
        ):
            triggers.add("fixed_time")
    if not triggers:
        return Decision()
    latest = _latest_pending(state.pending_commits)
    return Decision(commits=(latest,), triggers=frozenset(triggers))
