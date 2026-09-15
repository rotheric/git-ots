from datetime import UTC, datetime, timedelta

import pytest

from git_ots.config import PolicyConfig
from git_ots.git import InvalidRepositoryStateError
from git_ots.policy import (
    Decision,
    PendingCommit,
    PolicyState,
    evaluate_policy,
    most_recent_fixed_time_occurrence,
)


def _utc(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def test_pending_commit_stores_full_id_and_committer_timestamp():
    commit = PendingCommit(
        commit_id="0123456789abcdef0123456789abcdef01234567",
        committer_time=_utc(2026, 8, 14, 12, 30),
    )
    assert commit.commit_id == "0123456789abcdef0123456789abcdef01234567"
    assert commit.committer_time == _utc(2026, 8, 14, 12, 30)


def test_pending_commit_compares_chronologically_by_committer_time():
    older = PendingCommit(
        commit_id="a" * 40,
        committer_time=_utc(2026, 8, 14, 1, 0),
    )
    newer = PendingCommit(
        commit_id="b" * 40,
        committer_time=_utc(2026, 8, 14, 2, 0),
    )
    assert older < newer
    assert newer > older
    assert sorted([newer, older]) == [older, newer]


def test_pending_commit_is_immutable():
    commit = PendingCommit(
        commit_id="a" * 40,
        committer_time=_utc(2026, 8, 14, 1, 0),
    )
    with pytest.raises(AttributeError):
        commit.commit_id = "b" * 40  # type: ignore[misc]


def test_pending_commit_rejects_naive_committer_time():
    with pytest.raises(ValueError, match="committer_time"):
        PendingCommit(
            commit_id="a" * 40,
            committer_time=datetime(2026, 8, 14, 1, 0),  # noqa: DTZ001
        )


def test_pending_commit_naive_error_message_identifies_timezone_awareness():
    # The error must be actionable: it names the offending field and states
    # that a timezone-aware datetime is required.
    with pytest.raises(ValueError) as excinfo:
        PendingCommit(
            commit_id="a" * 40,
            committer_time=datetime(2026, 8, 14, 1, 0),  # noqa: DTZ001
        )
    message = str(excinfo.value)
    assert "committer_time" in message
    assert "timezone-aware" in message


def test_policy_state_defaults_to_no_pending_and_no_triggers():
    state = PolicyState(now=_utc(2026, 8, 15, 12, 0))
    assert state.pending_commits == ()
    assert state.last_fixed_time_occurrence is None


def test_policy_state_is_immutable():
    state = PolicyState(now=_utc(2026, 8, 15, 12, 0))
    with pytest.raises(AttributeError):
        state.now = _utc(2026, 8, 16)  # type: ignore[misc]


def test_policy_state_rejects_naive_now():
    with pytest.raises(ValueError) as excinfo:
        PolicyState(now=datetime(2026, 8, 15, 12, 0))  # noqa: DTZ001
    message = str(excinfo.value)
    assert "now" in message
    assert "timezone-aware" in message


def test_policy_state_rejects_naive_last_fixed_time_occurrence():
    with pytest.raises(ValueError) as excinfo:
        PolicyState(
            now=_utc(2026, 8, 15, 12, 0),
            last_fixed_time_occurrence=datetime(2026, 8, 14, 0, 0),  # noqa: DTZ001
        )
    message = str(excinfo.value)
    assert "last_fixed_time_occurrence" in message
    assert "timezone-aware" in message


def test_most_recent_fixed_time_occurrence_rejects_naive_now():
    # The occurrence helper is on the boundary of policy calculations: a
    # naive ``now`` would silently produce ambiguous wall-clock occurrences.
    from datetime import time
    from zoneinfo import ZoneInfo

    with pytest.raises(ValueError) as excinfo:
        most_recent_fixed_time_occurrence(
            datetime(2026, 8, 15, 12, 0),  # noqa: DTZ001
            time(0, 0),
            ZoneInfo("UTC"),
        )
    message = str(excinfo.value)
    assert "now" in message
    assert "timezone-aware" in message


def test_decision_defaults_to_no_commits_and_empty_triggers():
    decision = Decision()
    assert decision.commits == ()
    assert decision.triggers == frozenset()


def test_decision_stores_commits_in_order_and_immutable_triggers():
    commit_b = PendingCommit(commit_id="b" * 40, committer_time=_utc(2026, 8, 14, 1, 0))
    commit_c = PendingCommit(commit_id="c" * 40, committer_time=_utc(2026, 8, 14, 2, 0))
    decision = Decision(
        commits=(commit_b, commit_c),
        triggers=frozenset({"every_commit"}),
    )
    assert decision.commits == (commit_b, commit_c)
    assert decision.triggers == frozenset({"every_commit"})
    with pytest.raises(AttributeError):
        decision.triggers.add("max_age")  # type: ignore[attr-defined]


def test_decision_is_immutable():
    decision = Decision()
    with pytest.raises(AttributeError):
        decision.commits = ()  # type: ignore[misc]


def test_evaluate_policy_returns_no_decision_when_nothing_pending_every_commit():
    state = PolicyState(now=_utc(2026, 8, 15, 12, 0))
    config = PolicyConfig(every_commit=True)
    decision = evaluate_policy(state, config)
    assert decision.commits == ()
    assert decision.triggers == frozenset()


def test_evaluate_policy_returns_no_decision_when_nothing_pending_max_age():
    state = PolicyState(now=_utc(2026, 8, 15, 12, 0))
    config = PolicyConfig(max_age=timedelta(hours=24))
    decision = evaluate_policy(state, config)
    assert decision.commits == ()
    assert decision.triggers == frozenset()


def test_evaluate_policy_returns_no_decision_when_nothing_pending_fixed_time():
    from datetime import time
    from zoneinfo import ZoneInfo

    state = PolicyState(now=_utc(2026, 8, 15, 12, 0))
    config = PolicyConfig(fixed_time=time(0, 0), timezone=ZoneInfo("UTC"))
    decision = evaluate_policy(state, config)
    assert decision.commits == ()
    assert decision.triggers == frozenset()


def test_evaluate_policy_every_commit_selects_all_pending_in_order():
    commit_b = PendingCommit(commit_id="b" * 40, committer_time=_utc(2026, 8, 14, 1, 0))
    commit_c = PendingCommit(commit_id="c" * 40, committer_time=_utc(2026, 8, 14, 2, 0))
    commit_d = PendingCommit(commit_id="d" * 40, committer_time=_utc(2026, 8, 14, 3, 0))
    state = PolicyState(
        now=_utc(2026, 8, 15, 12, 0),
        pending_commits=(commit_b, commit_c, commit_d),
    )
    config = PolicyConfig(every_commit=True)
    decision = evaluate_policy(state, config)
    assert decision.commits == (commit_b, commit_c, commit_d)
    assert decision.triggers == frozenset({"every_commit"})


def test_evaluate_policy_max_age_not_due_returns_no_decision():
    # Oldest pending commit is 23h59m old; max_age is 24h — not yet due.
    now = _utc(2026, 8, 15, 12, 0)
    oldest = PendingCommit(
        commit_id="b" * 40,
        committer_time=now - timedelta(hours=23, minutes=59),
    )
    state = PolicyState(now=now, pending_commits=(oldest,))
    config = PolicyConfig(max_age=timedelta(hours=24))
    decision = evaluate_policy(state, config)
    assert decision.commits == ()
    assert decision.triggers == frozenset()


def test_evaluate_policy_max_age_boundary_selects_latest_with_max_age_trigger():
    # Oldest pending commit is exactly 24h old; max_age is 24h — boundary is due.
    now = _utc(2026, 8, 15, 12, 0)
    oldest = PendingCommit(
        commit_id="b" * 40,
        committer_time=now - timedelta(hours=24),
    )
    latest = PendingCommit(
        commit_id="c" * 40,
        committer_time=now - timedelta(hours=1),
    )
    state = PolicyState(now=now, pending_commits=(oldest, latest))
    config = PolicyConfig(max_age=timedelta(hours=24))
    decision = evaluate_policy(state, config)
    assert decision.commits == (latest,)
    assert decision.triggers == frozenset({"max_age"})


def test_evaluate_policy_max_age_uses_oldest_for_age_and_latest_for_target():
    # B is old enough to trigger max_age; C and D are recent.
    # Age testing must use B (oldest), target selection must use D (latest).
    now = _utc(2026, 8, 15, 12, 0)
    commit_b = PendingCommit(
        commit_id="b" * 40,
        committer_time=now - timedelta(hours=25),
    )
    commit_c = PendingCommit(
        commit_id="c" * 40,
        committer_time=now - timedelta(hours=2),
    )
    commit_d = PendingCommit(
        commit_id="d" * 40,
        committer_time=now - timedelta(hours=1),
    )
    state = PolicyState(
        now=now,
        pending_commits=(commit_b, commit_c, commit_d),
    )
    config = PolicyConfig(max_age=timedelta(hours=24))
    decision = evaluate_policy(state, config)
    assert decision.commits == (commit_d,)
    assert decision.triggers == frozenset({"max_age"})


def test_evaluate_policy_max_age_tie_on_committer_time_selects_latest_in_repo_order():
    # ``git rebase`` stamps every replayed commit with a single wall-clock
    # committer date, so equal committer timestamps are routine. Under a tie
    # the stamp must land on the commit closest to the source tip (the last
    # in repository order), or it does not cover the later pending state.
    # PendingCommit equality ignores commit_id, so assert on the id itself.
    now = _utc(2026, 8, 15, 12, 0)
    shared_time = now - timedelta(hours=25)
    commits = tuple(
        PendingCommit(commit_id=c * 40, committer_time=shared_time) for c in "bcd"
    )
    state = PolicyState(now=now, pending_commits=commits)
    config = PolicyConfig(max_age=timedelta(hours=24))
    decision = evaluate_policy(state, config)
    assert [c.commit_id for c in decision.commits] == ["d" * 40]
    assert decision.triggers == frozenset({"max_age"})


def test_evaluate_policy_every_commit_initial_latest_tie_selects_latest_in_repo_order():
    # Same tie-break requirement for the initial-history "latest" selection.
    now = _utc(2026, 8, 15, 12, 0)
    shared_time = now - timedelta(hours=1)
    commits = tuple(
        PendingCommit(commit_id=c * 40, committer_time=shared_time) for c in "bcd"
    )
    state = PolicyState(
        now=now,
        pending_commits=commits,
        has_baseline=False,
    )
    config = PolicyConfig(every_commit=True)
    decision = evaluate_policy(state, config)
    assert [c.commit_id for c in decision.commits] == ["d" * 40]
    assert decision.triggers == frozenset({"every_commit"})


def test_most_recent_fixed_time_occurrence_before_wall_time_uses_previous_day():
    from datetime import time
    from zoneinfo import ZoneInfo

    # 23:30 UTC, wall time is 23:59 — today's occurrence hasn't happened yet,
    # so the most recent occurrence is yesterday at 23:59 UTC.
    now = _utc(2026, 8, 15, 23, 30)
    occurrence = most_recent_fixed_time_occurrence(now, time(23, 59), ZoneInfo("UTC"))
    assert occurrence == _utc(2026, 8, 14, 23, 59)


def test_most_recent_fixed_time_occurrence_after_wall_time_uses_today():
    from datetime import time
    from zoneinfo import ZoneInfo

    # 00:30 UTC, wall time is 00:00 — today's occurrence has already passed.
    now = _utc(2026, 8, 15, 0, 30)
    occurrence = most_recent_fixed_time_occurrence(now, time(0, 0), ZoneInfo("UTC"))
    assert occurrence == _utc(2026, 8, 15, 0, 0)


def test_most_recent_fixed_time_occurrence_at_exact_wall_time_is_today():
    from datetime import time
    from zoneinfo import ZoneInfo

    # Exactly at the wall time — the occurrence is at-or-before now.
    now = _utc(2026, 8, 15, 0, 0)
    occurrence = most_recent_fixed_time_occurrence(now, time(0, 0), ZoneInfo("UTC"))
    assert occurrence == _utc(2026, 8, 15, 0, 0)


def test_most_recent_fixed_time_occurrence_returns_aware_datetime_in_zone():
    from datetime import time
    from zoneinfo import ZoneInfo

    now = _utc(2026, 8, 15, 12, 0)
    occurrence = most_recent_fixed_time_occurrence(now, time(0, 0), ZoneInfo("UTC"))
    assert occurrence.tzinfo is not None
    assert occurrence.utcoffset() is not None


def test_evaluate_policy_fixed_time_scheduler_delay_selects_latest():
    # A 00:00 occurrence evaluated at 00:07 (scheduler delay), with the
    # occurrence unsatisfied and pending changes present, SHALL select the
    # latest commit with the fixed_time trigger.
    from datetime import time
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("UTC")
    now = _utc(2026, 8, 15, 0, 7)
    commit_b = PendingCommit(
        commit_id="b" * 40,
        committer_time=_utc(2026, 8, 14, 10, 0),
    )
    commit_c = PendingCommit(
        commit_id="c" * 40,
        committer_time=_utc(2026, 8, 14, 22, 0),
    )
    state = PolicyState(
        now=now,
        pending_commits=(commit_b, commit_c),
        last_fixed_time_occurrence=None,
    )
    config = PolicyConfig(fixed_time=time(0, 0), timezone=tz)
    decision = evaluate_policy(state, config)
    assert decision.commits == (commit_c,)
    assert decision.triggers == frozenset({"fixed_time"})


def test_evaluate_policy_fixed_time_satisfied_occurrence_returns_no_decision():
    # Most recent 00:00 occurrence equals the recorded satisfied occurrence:
    # nothing more to do for this occurrence.
    from datetime import time
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("UTC")
    now = _utc(2026, 8, 15, 0, 7)
    occurrence = most_recent_fixed_time_occurrence(now, time(0, 0), tz)
    commit_b = PendingCommit(
        commit_id="b" * 40,
        committer_time=_utc(2026, 8, 14, 10, 0),
    )
    state = PolicyState(
        now=now,
        pending_commits=(commit_b,),
        last_fixed_time_occurrence=occurrence,
    )
    config = PolicyConfig(fixed_time=time(0, 0), timezone=tz)
    decision = evaluate_policy(state, config)
    assert decision.commits == ()
    assert decision.triggers == frozenset()


def test_evaluate_policy_fixed_time_after_next_occurrence_selects_latest():
    # After the next 00:00 occurrence has passed, the fixed-time rule is due
    # again even though an earlier occurrence was already satisfied.
    from datetime import time
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("UTC")
    # Yesterday's 00:00 occurrence was already satisfied.
    yesterday_occurrence = _utc(2026, 8, 14, 0, 0)
    now = _utc(2026, 8, 15, 0, 30)
    commit_b = PendingCommit(
        commit_id="b" * 40,
        committer_time=_utc(2026, 8, 14, 12, 0),
    )
    state = PolicyState(
        now=now,
        pending_commits=(commit_b,),
        last_fixed_time_occurrence=yesterday_occurrence,
    )
    config = PolicyConfig(fixed_time=time(0, 0), timezone=tz)
    decision = evaluate_policy(state, config)
    assert decision.commits == (commit_b,)
    assert decision.triggers == frozenset({"fixed_time"})


def test_evaluate_policy_returns_no_decision_when_nothing_pending_combined():
    from datetime import time
    from zoneinfo import ZoneInfo

    state = PolicyState(now=_utc(2026, 8, 15, 12, 0))
    config = PolicyConfig(
        every_commit=True,
        max_age=timedelta(hours=24),
        fixed_time=time(0, 0),
        timezone=ZoneInfo("UTC"),
    )
    decision = evaluate_policy(state, config)
    assert decision.commits == ()
    assert decision.triggers == frozenset()


def test_most_recent_fixed_time_occurrence_spring_forward_returns_valid_aware():
    # Europe/Berlin springs forward on 2026-03-29: 02:00 -> 03:00 CEST begins.
    # A 02:30 wall time on that date does not exist locally; the occurrence
    # helper must still return a valid aware datetime in the configured zone
    # (not a phantom local time that fails a UTC round trip).
    from datetime import time
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Berlin")
    # 01:30 UTC on 2026-03-29 — just after the gap begins locally (03:30 CEST).
    now = datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    occurrence = most_recent_fixed_time_occurrence(now, time(2, 30), tz)
    assert occurrence.tzinfo is not None
    assert occurrence.utcoffset() is not None
    # The occurrence must round-trip through UTC unchanged: an aware datetime
    # that does not survive a UTC round trip represents a phantom local time.
    as_utc = occurrence.astimezone(UTC)
    assert as_utc.astimezone(tz) == occurrence
    # It must also be at or before ``now`` in absolute time.
    assert occurrence <= now


def test_most_recent_fixed_time_occurrence_fall_back_returns_single_valid_occurrence():
    # Europe/Berlin falls back on 2026-10-25: 03:00 CEST -> 02:00 CET.
    # A 02:30 wall time on that date is ambiguous (occurs twice). The helper
    # must return exactly one valid aware occurrence per local day, so the
    # fixed-time rule fires once per day even across the fall-back transition.
    from datetime import time
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Berlin")
    # 2026-10-25 12:00 UTC — well past both ambiguous 02:30 instants.
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    occurrence = most_recent_fixed_time_occurrence(now, time(2, 30), tz)
    assert occurrence.tzinfo is not None
    assert occurrence.utcoffset() is not None
    as_utc = occurrence.astimezone(UTC)
    assert as_utc.astimezone(tz) == occurrence
    assert occurrence <= now
    # Local calendar date must be the fall-back day itself.
    assert occurrence.astimezone(tz).date().isoformat() == "2026-10-25"


def test_evaluate_policy_combines_max_age_and_fixed_time_into_one_submission():
    # When both max_age and fixed_time become due together, the policy SHALL
    # produce one submission for the latest pending commit whose trigger set
    # records both reasons.
    from datetime import time
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("UTC")
    now = _utc(2026, 8, 15, 0, 30)
    commit_b = PendingCommit(
        commit_id="b" * 40,
        committer_time=now - timedelta(hours=25),
    )
    commit_c = PendingCommit(
        commit_id="c" * 40,
        committer_time=now - timedelta(hours=2),
    )
    commit_d = PendingCommit(
        commit_id="d" * 40,
        committer_time=now - timedelta(hours=1),
    )
    state = PolicyState(
        now=now,
        pending_commits=(commit_b, commit_c, commit_d),
        last_fixed_time_occurrence=None,
    )
    config = PolicyConfig(
        max_age=timedelta(hours=24),
        fixed_time=time(0, 0),
        timezone=tz,
    )
    decision = evaluate_policy(state, config)
    assert decision.commits == (commit_d,)
    assert decision.triggers == frozenset({"max_age", "fixed_time"})


def test_evaluate_policy_every_commit_combined_with_aggregates_selects_all_pending():
    # every_commit has stronger target-selection semantics: when enabled
    # alongside aggregate policies, every pending meaningful commit SHALL be
    # selected individually with `every_commit` semantics. The aggregate
    # policies become operationally redundant for those commits.
    from datetime import time
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("UTC")
    now = _utc(2026, 8, 15, 0, 30)
    commit_b = PendingCommit(
        commit_id="b" * 40,
        committer_time=now - timedelta(hours=25),
    )
    commit_c = PendingCommit(
        commit_id="c" * 40,
        committer_time=now - timedelta(hours=2),
    )
    commit_d = PendingCommit(
        commit_id="d" * 40,
        committer_time=now - timedelta(hours=1),
    )
    state = PolicyState(
        now=now,
        pending_commits=(commit_b, commit_c, commit_d),
        last_fixed_time_occurrence=None,
    )
    config = PolicyConfig(
        every_commit=True,
        max_age=timedelta(hours=24),
        fixed_time=time(0, 0),
        timezone=tz,
    )
    decision = evaluate_policy(state, config)
    # Each of B, C, D is selected exactly once, in repository order.
    assert decision.commits == (commit_b, commit_c, commit_d)
    assert len(decision.commits) == 3
    assert len({c.commit_id for c in decision.commits}) == 3
    # every_commit subsumes the aggregate triggers: the decision records only
    # the every_commit semantics, not max_age/fixed_time, even though both
    # would have fired had every_commit been disabled.
    assert decision.triggers == frozenset({"every_commit"})


def test_policy_state_has_baseline_established_default_true():
    state = PolicyState(now=_utc(2026, 8, 15, 12, 0))
    assert state.has_baseline is True


def test_evaluate_policy_max_age_without_baseline_selects_latest():
    # No baseline exists yet (initial invocation on a long existing history).
    # Aggregate policy: the latest pending commit SHALL be selected when due.
    now = _utc(2026, 8, 15, 12, 0)
    commits = tuple(
        PendingCommit(
            commit_id=("%x" % (i + 1)) * 40,
            committer_time=now - timedelta(hours=25 - i),
        )
        for i in range(3)
    )
    state = PolicyState(
        now=now,
        pending_commits=commits,
        has_baseline=False,
    )
    config = PolicyConfig(max_age=timedelta(hours=24))
    decision = evaluate_policy(state, config)
    assert decision.commits == (commits[-1],)
    assert decision.triggers == frozenset({"max_age"})


def test_evaluate_policy_every_commit_without_baseline_defaults_to_latest_only():
    # With no baseline and `initial_history = "latest"` (the default),
    # `every_commit` SHALL select only the latest pending commit to establish
    # the initial baseline; it must not blindly replay the entire history.
    now = _utc(2026, 8, 15, 12, 0)
    commit_b = PendingCommit(
        commit_id="b" * 40,
        committer_time=now - timedelta(hours=25),
    )
    commit_c = PendingCommit(
        commit_id="c" * 40,
        committer_time=now - timedelta(hours=2),
    )
    commit_d = PendingCommit(
        commit_id="d" * 40,
        committer_time=now - timedelta(hours=1),
    )
    state = PolicyState(
        now=now,
        pending_commits=(commit_b, commit_c, commit_d),
        has_baseline=False,
    )
    config = PolicyConfig(every_commit=True)
    decision = evaluate_policy(state, config)
    assert decision.commits == (commit_d,)
    assert decision.triggers == frozenset({"every_commit"})


def test_evaluate_policy_every_commit_without_baseline_initial_history_all_selects_all():
    # With no baseline and `initial_history = "all"` (explicit opt-in),
    # `every_commit` SHALL select every meaningful pending commit so the
    # entire initial history is timestamped.
    now = _utc(2026, 8, 15, 12, 0)
    commit_b = PendingCommit(
        commit_id="b" * 40,
        committer_time=now - timedelta(hours=25),
    )
    commit_c = PendingCommit(
        commit_id="c" * 40,
        committer_time=now - timedelta(hours=2),
    )
    commit_d = PendingCommit(
        commit_id="d" * 40,
        committer_time=now - timedelta(hours=1),
    )
    state = PolicyState(
        now=now,
        pending_commits=(commit_b, commit_c, commit_d),
        has_baseline=False,
    )
    config = PolicyConfig(every_commit=True, initial_history="all")
    decision = evaluate_policy(state, config)
    assert decision.commits == (commit_b, commit_c, commit_d)
    assert decision.triggers == frozenset({"every_commit"})


def test_evaluate_policy_fixed_time_fall_back_does_not_fire_twice_in_one_day():
    # Across the fall-back day in Europe/Berlin, after the 02:30 occurrence is
    # recorded as satisfied, a later invocation the same local day must not
    # fire again — one occurrence per local date, never duplicated.
    from datetime import time
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    satisfied = most_recent_fixed_time_occurrence(now, time(2, 30), tz)
    commit_b = PendingCommit(
        commit_id="b" * 40,
        committer_time=datetime(2026, 10, 24, 12, 0, tzinfo=UTC),
    )
    state = PolicyState(
        now=now,
        pending_commits=(commit_b,),
        last_fixed_time_occurrence=satisfied,
    )
    config = PolicyConfig(fixed_time=time(2, 30), timezone=tz)
    decision = evaluate_policy(state, config)
    assert decision.commits == ()
    assert decision.triggers == frozenset()


def test_evaluate_policy_every_commit_refuses_rewritten_history():
    # Spec section 26: when timestamp tags exist but none are ancestors of the
    # current source (abandoned lineage), every_commit must fail rather than
    # implicitly timestamp the entire rewritten history.
    now = _utc(2026, 8, 15, 12, 0)
    commit_b = PendingCommit(
        commit_id="b" * 40,
        committer_time=now - timedelta(hours=2),
    )
    commit_c = PendingCommit(
        commit_id="c" * 40,
        committer_time=now - timedelta(hours=1),
    )
    state = PolicyState(
        now=now,
        pending_commits=(commit_b, commit_c),
        has_abandoned_tags=True,
    )
    config = PolicyConfig(every_commit=True)
    with pytest.raises(InvalidRepositoryStateError, match="rewritten"):
        evaluate_policy(state, config)
