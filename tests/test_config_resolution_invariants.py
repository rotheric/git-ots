"""S9: configuration-resolution whole-flow guarantees (G1-G4, AC-INV-1..4).

Verifies the four cross-cutting, order-sensitive-composition guarantees from
architecture.md's "Order-Sensitive Composition" section against the real,
already-implemented `gitconfig.py` / `config.py` / `policy.py` producers
assembled together -- no test doubles for any of the three. See
specs/spec.md
S9-config-resolution-whole-flow-guarantees/verification.json.

Every case below builds a real repository (and, where a case needs the
`worktree` or `system` scope, real `git config --worktree`/`--system`
state) and reads it back through `gitconfig.assemble_config` -- exactly the
function `cli.py`'s `run`/`status` subcommands call once `Config` is needed
-- then, for AC-INV-1/2/3 (each of which the epic's `Spans modules:` line
in acceptance-criteria.md names as spanning `policy` too), routes the
resolved `PolicyConfig` through the real `policy.evaluate_policy` so the
scope-tier ladder's effect is observed in an actual `Decision`, not only in
the intermediate `Config` object.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from git_ots.config import Config
from git_ots.git import make_process_runner
from git_ots.gitconfig import BOOLEAN_KEYS, MAPPING, assemble_config
from git_ots.policy import Decision, PendingCommit, PolicyState, evaluate_policy

NOW = datetime(2026, 1, 1, tzinfo=UTC)

TIERS: tuple[str, ...] = ("system", "global", "local", "worktree")
_TIER_INDEX = {tier: index for index, tier in enumerate(TIERS)}
_SCOPE_FLAG = {
    "system": "--system",
    "global": "--global",
    "local": "--local",
    "worktree": "--worktree",
}


def _git(args: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return completed.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main", "."], cwd=path)
    _git(["config", "user.email", "test@example.com"], cwd=path)
    _git(["config", "user.name", "Test"], cwd=path)
    (path / "README").write_text("seed\n")
    _git(["add", "README"], cwd=path)
    _git(["commit", "-q", "-m", "seed"], cwd=path)


def _set_scope(repo: Path, scope: str, key: str, value: str) -> None:
    _git(["config", _SCOPE_FLAG[scope], key, value], cwd=repo)


def _prepare_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tiers_used: set[str],
) -> Path:
    """Build a repo ready to receive `git config` writes at every tier in
    `tiers_used`. `local` and `global` need no extra setup -- the suite's
    autouse `_isolated_git_config` fixture (conftest.py) already gives every
    test a real, writable, isolated global-scope file. `worktree` needs
    `extensions.worktreeConfig` turned on first; `system` needs
    `GIT_CONFIG_SYSTEM` narrowed from conftest's `/dev/null` to a real,
    writable, per-test file with `GIT_CONFIG_NOSYSTEM` turned off, mirroring
    test_cli.py's `test_config_command_output_vocabulary_...` pattern.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    if "worktree" in tiers_used:
        _git(["config", "extensions.worktreeConfig", "true"], cwd=repo)
    if "system" in tiers_used:
        system_config = tmp_path / "system-gitconfig"
        system_config.touch()
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_config))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "0")
    return repo


def _evaluate(policy_config, *, now: datetime) -> Decision:
    """Route a resolved `PolicyConfig` through the real `policy.evaluate_policy`.

    One pending commit, ten years old, with no prior fixed-time occurrence
    recorded: old enough that any `max_age` this file configures (all well
    under ten years) is satisfied, and `last_fixed_time_occurrence=None`
    unconditionally satisfies `fixed_time`'s "newer than the last recorded
    occurrence" check. So whichever of the resolved `PolicyConfig`'s trigger
    fields is actually set becomes observable in the returned `Decision`,
    without this helper re-deciding anything `evaluate_policy` itself
    decides (`every_commit` still shadows `max_age`/`fixed_time` in
    `evaluate_policy`'s own precedence when more than one field is set --
    irrelevant here since every case below resolves at most one).
    """
    state = PolicyState(
        now=now,
        pending_commits=(
            PendingCommit(
                commit_id="0" * 40, committer_time=now - timedelta(days=3650)
            ),
        ),
        last_fixed_time_occurrence=None,
        has_baseline=True,
        has_abandoned_tags=False,
    )
    return evaluate_policy(state, policy_config)


# ---------------------------------------------------------------------------
# AC-INV-1 -- G1, trigger-set exclusivity.
# ---------------------------------------------------------------------------

# Representative, always-valid values per trigger kind. The "narrow" value is
# chosen to differ from that field's built-in default (`max_age`'s default is
# 24h, so 2h; `fixed_time`/`timezone` default to `None`, so any value would
# do, but a fixed pair is used for a precise equality assertion rather than a
# mere not-None check -- VQ-S9-007). The "wide" value differs from the narrow
# one too, so a passing test proves the narrow value actually survived rather
# than merely not having changed.
_KIND_VALUES = {
    "every_commit": {"narrow": None, "wide": None},  # boolean; value is fixed "true"
    "max_age": {"narrow": "2h", "wide": "9h"},
    "fixed_time": {
        "narrow": ("03:30", "UTC"),
        "wide": ("21:15", "America/New_York"),
    },
}


def _configure_kind(repo: Path, scope: str, kind: str, *, narrow: bool) -> None:
    key = "narrow" if narrow else "wide"
    if kind == "every_commit":
        _set_scope(repo, scope, "ots.everyCommit", "true")
    elif kind == "max_age":
        _set_scope(repo, scope, "ots.maxAge", _KIND_VALUES["max_age"][key])
    else:  # fixed_time
        fixed_time, timezone = _KIND_VALUES["fixed_time"][key]
        _set_scope(repo, scope, "ots.fixedTime", fixed_time)
        _set_scope(repo, scope, "ots.timezone", timezone)


def _assert_resolved_exactly(config: Config, kind: str) -> None:
    """Assert the resolved `PolicyConfig` carries exactly `kind`'s value and
    no other trigger field -- the field-level form of AC-INV-1's "resolved
    trigger set equals exactly the enabling trigger(s) at the narrowest
    naming scope, and no wider-scope trigger survives"."""
    if kind == "every_commit":
        assert config.policy.every_commit is True
        assert config.policy.max_age is None
        assert config.policy.fixed_time is None
        assert config.policy.timezone is None
    elif kind == "max_age":
        assert config.policy.every_commit is False
        assert config.policy.max_age == timedelta(hours=2)
        assert config.policy.fixed_time is None
        assert config.policy.timezone is None
    else:  # fixed_time
        assert config.policy.every_commit is False
        assert config.policy.max_age is None
        assert config.policy.fixed_time == dt_time(3, 30)
        assert config.policy.timezone == ZoneInfo("UTC")


_KINDS: tuple[str, ...] = ("every_commit", "max_age", "fixed_time")
_WIDER_KINDS: tuple[str | None, ...] = (None, *_KINDS)
_TIER_PAIRS = [
    (wider, narrower)
    for wider in TIERS
    for narrower in TIERS
    if _TIER_INDEX[wider] < _TIER_INDEX[narrower]
]

INV1_CASES = [
    pytest.param(
        wider_tier,
        narrower_tier,
        wider_kind,
        narrower_kind,
        id=f"{wider_kind}@{wider_tier}-vs-{narrower_kind}@{narrower_tier}",
    )
    for wider_tier, narrower_tier in _TIER_PAIRS
    for wider_kind in _WIDER_KINDS
    for narrower_kind in _KINDS
]


@pytest.mark.parametrize(
    "wider_tier,narrower_tier,wider_kind,narrower_kind", INV1_CASES
)
def test_ac_inv_1_trigger_set_exclusivity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wider_tier: str,
    narrower_tier: str,
    wider_kind: str | None,
    narrower_kind: str,
) -> None:
    """AC-INV-1 / G1: the resolved trigger set equals exactly the enabling
    trigger(s) at the narrowest scope naming any enabling trigger, whatever
    a wider scope names (including no trigger at all, and including a
    *different* enabling trigger than the narrower scope's) -- generated
    across all 6 narrower-than-wider scope-tier pairs x 4 wider-scope
    options (none, or one of the 3 trigger kinds) x 3 narrower-scope trigger
    kinds = 72 cases.
    """
    tiers_used = {narrower_tier}
    if wider_kind is not None:
        tiers_used.add(wider_tier)
    repo = _prepare_repo(tmp_path, monkeypatch, tiers_used)

    if wider_kind is not None:
        _configure_kind(repo, wider_tier, wider_kind, narrow=False)
    _configure_kind(repo, narrower_tier, narrower_kind, narrow=True)

    config = assemble_config(cwd=repo)

    _assert_resolved_exactly(config, narrower_kind)

    decision = _evaluate(config.policy, now=NOW)
    assert decision.triggers == frozenset({narrower_kind})


def test_ac_inv_1_ac_trig_2_load_bearing_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact AC-TRIG-2 row, named verbatim by AC-INV-1 as a mandatory
    minimum case: `ots.maxAge=12h` at global scope, `ots.fixedTime=03:00`
    plus a qualifying `ots.timezone` at local scope. The resolved trigger
    set MUST be `fixed_time` only, with `max_age` equal to `None` --
    discarded, not merged, and not left at its non-`None` global value.
    Already one of the 72 generated `test_ac_inv_1_trigger_set_exclusivity`
    cases (`max_age@global-vs-fixed_time@local`); named again here,
    standalone, so the exact acceptance-criteria values are visible without
    cross-referencing the generator.
    """
    repo = _prepare_repo(tmp_path, monkeypatch, {"global", "local"})
    _set_scope(repo, "global", "ots.maxAge", "12h")
    _set_scope(repo, "local", "ots.fixedTime", "03:00")
    _set_scope(repo, "local", "ots.timezone", "UTC")

    config = assemble_config(cwd=repo)

    assert config.policy.max_age is None
    assert config.policy.fixed_time == dt_time(3, 0)
    assert config.policy.timezone == ZoneInfo("UTC")
    assert config.policy.every_commit is False

    decision = _evaluate(config.policy, now=NOW)
    assert decision.triggers == frozenset({"fixed_time"})


def test_ac_inv_1_no_scope_names_any_trigger_falls_back_to_default_max_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline row of behaviour 8's worked table: no scope names any
    enabling trigger anywhere -> the resolved trigger set is the built-in
    default (`max_age=DEFAULT_MAX_AGE`), not an empty set -- the default is
    the ladder's bottom rung, per architecture.md. A legitimate no-override
    case (VQ-S9-007): distinguished here from the override-must-survive
    cases above rather than asserted with the same non-default check.
    """
    repo = _prepare_repo(tmp_path, monkeypatch, set())

    config = assemble_config(cwd=repo)

    assert config.policy.every_commit is False
    assert config.policy.max_age == timedelta(hours=24)
    assert config.policy.fixed_time is None
    assert config.policy.timezone is None

    decision = _evaluate(config.policy, now=NOW)
    assert decision.triggers == frozenset({"max_age"})


# AC-INV-1's resolved set is "the enabling trigger(s)" -- plural -- "named at
# the narrowest scope tier that names any enabling trigger", and the AC is
# framed over "any assignment". `INV1_CASES` above names exactly one kind per
# tier, so it never generates the assignment where the *narrowest* tier names
# two or more enabling triggers at once. That case is not a curiosity: it is
# the branch where behaviour 8's table has same-tier triggers *coexisting*
# (both survive) rather than one displacing the other, so it is precisely
# where a ladder implementation that over-applies "narrowest wins" per key
# would wrongly drop one of them. S4 covers the behaviour with hand-written
# examples (`test_gitconfig.py`'s `test_trigger_ladder_row_both_triggers_set_
# in_one_scope` and `test_trigger_ladder_same_scope_boolean_every_commit_and_
# max_age_coexist`); what the whole-flow property suite was missing is the
# *generated* form -- every scope-tier pair, every trigger combination, with a
# wider tier's trigger simultaneously required to be discarded.
_NARROW_COMBOS: tuple[frozenset[str], ...] = (
    frozenset({"every_commit", "max_age"}),
    frozenset({"every_commit", "fixed_time"}),
    frozenset({"max_age", "fixed_time"}),
    frozenset({"every_commit", "max_age", "fixed_time"}),
)


def _assert_resolved_exactly_set(config: Config, kinds: frozenset[str]) -> None:
    """The set-valued form of `_assert_resolved_exactly`: the resolved
    `PolicyConfig` carries every trigger in `kinds` at its narrow-position
    value, and no trigger outside `kinds` at all."""
    assert config.policy.every_commit is ("every_commit" in kinds)
    if "max_age" in kinds:
        assert config.policy.max_age == timedelta(hours=2)
    else:
        assert config.policy.max_age is None
    if "fixed_time" in kinds:
        assert config.policy.fixed_time == dt_time(3, 30)
        assert config.policy.timezone == ZoneInfo("UTC")
    else:
        assert config.policy.fixed_time is None
        assert config.policy.timezone is None


INV1_PLURAL_CASES = [
    pytest.param(
        wider_tier,
        narrower_tier,
        wider_kind,
        narrow_kinds,
        id=f"{wider_kind}@{wider_tier}-vs-{'+'.join(sorted(narrow_kinds))}@{narrower_tier}",
    )
    for wider_tier, narrower_tier in _TIER_PAIRS
    for wider_kind in _KINDS
    for narrow_kinds in _NARROW_COMBOS
]


@pytest.mark.parametrize(
    "wider_tier,narrower_tier,wider_kind,narrow_kinds", INV1_PLURAL_CASES
)
def test_ac_inv_1_plural_trigger_set_at_narrowest_tier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wider_tier: str,
    narrower_tier: str,
    wider_kind: str,
    narrow_kinds: frozenset[str],
) -> None:
    """AC-INV-1 / G1, plural form: when the narrowest tier naming any enabling
    trigger names *several*, the resolved set is exactly those -- all of them
    surviving together -- and the wider tier's trigger is still discarded.
    Generated across all 6 scope-tier pairs x 3 wider-scope trigger kinds x 4
    narrow-scope trigger combinations (3 pairs + the full triple) = 72 cases.

    Note the generator deliberately includes cases where `wider_kind` is also
    a member of `narrow_kinds` (e.g. `max_age@global` vs
    `max_age+fixed_time@local`). Those are the sharpest cases: the wide and
    narrow positions carry *different values* for that kind (`_KIND_VALUES`),
    so the assertion proves the narrow tier's value won rather than merely
    that some value is present.
    """
    repo = _prepare_repo(tmp_path, monkeypatch, {wider_tier, narrower_tier})

    _configure_kind(repo, wider_tier, wider_kind, narrow=False)
    for kind in sorted(narrow_kinds):
        _configure_kind(repo, narrower_tier, kind, narrow=True)

    config = assemble_config(cwd=repo)

    _assert_resolved_exactly_set(config, narrow_kinds)

    # `evaluate_policy` short-circuits on `every_commit` and reports it alone;
    # otherwise `max_age` and `fixed_time` both fire under `_evaluate`'s state.
    expected = (
        frozenset({"every_commit"})
        if "every_commit" in narrow_kinds
        else frozenset(narrow_kinds)
    )
    decision = _evaluate(config.policy, now=NOW)
    assert decision.triggers == expected


# ---------------------------------------------------------------------------
# AC-INV-2 -- G2, enabling-value monotonicity.
# ---------------------------------------------------------------------------

_FALSE_SPELLINGS = ("false", "no", "off", "0")

# The baseline enabling trigger is varied over two kinds, and the second one
# is load-bearing rather than decorative. When the baseline is `ots.maxAge`,
# the false value added by the test is a *different key* from the baseline
# trigger, so Git's per-key resolution keeps both records visible and the
# ladder sees the baseline no matter how the boolean read is implemented.
# When the baseline is `ots.everyCommit=true`, the added false is the *same
# key*, so Git's own `--get` collapses the pair to just the narrower false --
# and an implementation that reads only that winning value silently loses the
# wider `true`. That is precisely the suppression AC-INV-2 forbids, and it is
# invisible to a `max_age` baseline. An independent review found exactly this
# defect in `gitconfig._read_boolean` after the first version of this file
# tested only the `max_age` baseline; the fix reads `--type=bool --get-all`
# and resolves the ladder from the narrowest *true* scope.
_BASELINE_KINDS: tuple[str, ...] = ("max_age", "every_commit")

INV2_CASES = [
    pytest.param(
        target_tier,
        spelling,
        baseline_kind,
        id=f"{target_tier}={spelling}-over-{baseline_kind}",
    )
    for target_tier in TIERS
    for spelling in _FALSE_SPELLINGS
    for baseline_kind in _BASELINE_KINDS
]


@pytest.mark.parametrize("target_tier,spelling,baseline_kind", INV2_CASES)
def test_ac_inv_2_enabling_value_monotonicity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_tier: str,
    spelling: str,
    baseline_kind: str,
) -> None:
    """AC-INV-2 / G2: adding `ots.everyCommit=<false spelling>` at a scope
    tier that does not already carry an enabling trigger changes nothing --
    not the resolved trigger set, not any other resolved key. Generated
    across all 4 scope tiers x all 4 Git-boolean false spellings = 16 cases.

    The pre-existing, non-default baseline configuration (an `ots.maxAge`
    trigger plus two non-trigger keys) always sits at a scope tier different
    from `target_tier`: `local` for every target except `local` itself, for
    which the baseline moves to `worktree`. For `target_tier == "global"`
    this puts the baseline at `local` -- narrower than global -- which is
    VQ-S9-005's specific security-relevant framing: a false value at the
    *widest* commonly-configured scope must not silently degrade a
    narrower, already-resolved policy. For `target_tier == "worktree"` the
    baseline sits at the *wider* `local` scope, which is the more
    surprising direction: even a false value at the narrowest possible
    scope tier of all must not out-rank a real enabling trigger at a wider
    one (behaviour 9 -- a false trigger "neither enables nor claims its
    scope's tier", regardless of how narrow that scope is).
    """
    baseline_tier = "worktree" if target_tier == "local" else "local"
    repo = _prepare_repo(tmp_path, monkeypatch, {target_tier, baseline_tier})

    if baseline_kind == "max_age":
        _set_scope(repo, baseline_tier, "ots.maxAge", "6h")
    else:
        _set_scope(repo, baseline_tier, "ots.everyCommit", "true")
    _set_scope(repo, baseline_tier, "ots.tagPrefix", "custom/")
    _set_scope(repo, baseline_tier, "ots.signing", "required")

    config_before = assemble_config(cwd=repo)
    assert config_before != Config()  # non-default baseline (VQ-S9-007)
    if baseline_kind == "max_age":
        assert config_before.policy.every_commit is False
        assert config_before.policy.max_age == timedelta(hours=6)
    else:
        assert config_before.policy.every_commit is True
        assert config_before.policy.max_age is None

    _set_scope(repo, target_tier, "ots.everyCommit", spelling)

    config_after = assemble_config(cwd=repo)

    assert config_after == config_before
    assert _evaluate(config_after.policy, now=NOW) == _evaluate(
        config_before.policy, now=NOW
    )


def test_ac_inv_2_narrower_false_does_not_suppress_wider_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact shape an independent review reproduced against the first
    version of this file: `ots.everyCommit=true` in `global`,
    `ots.everyCommit=false` in `local`. Git's own `--get` collapses that pair
    to the narrower `false`, so a loader reading only the winning value sees
    no enabling trigger anywhere and silently falls back to the default
    24-hour `max_age` policy -- the operator's `global` opt-in to
    every-commit timestamping quietly discarded by an inert `false`.

    AC-INV-2 forbids exactly this: a false trigger "MUST NOT suppress an
    enabling trigger named at a wider scope". Named standalone (rather than
    left implicit among the generated cases) because it is a fixed
    production defect, and a regression here is a silent policy downgrade
    rather than a loud failure.
    """
    repo = _prepare_repo(tmp_path, monkeypatch, {"global", "local"})
    _set_scope(repo, "global", "ots.everyCommit", "true")
    _set_scope(repo, "local", "ots.everyCommit", "false")

    config = assemble_config(cwd=repo)

    assert config.policy.every_commit is True
    assert config.policy.max_age is None

    decision = _evaluate(config.policy, now=NOW)
    assert decision.triggers == frozenset({"every_commit"})


# ---------------------------------------------------------------------------
# AC-INV-3 -- G3, non-trigger keys independent of the trigger ladder.
# ---------------------------------------------------------------------------

# The exact 8 non-trigger keys AC-INV-3 (and VQ-S9-010) name verbatim.
# `ots.fetchBeforeRun` and `ots.requireCleanWorktree` are also non-trigger
# `MAPPING` rows but are not in the AC's own enumeration, so they are left
# out of this generator to match what an examiner will check against the AC
# text precisely.
_AC_INV_3_KEYS = (
    "ots.tagprefix",
    "ots.command",
    "ots.sourceref",
    "ots.signing",
    "ots.gittimeout",
    "ots.otstimeout",
    "ots.initialhistory",
    "ots.proofcommit",
)
_NON_TRIGGER_ROWS = [row for row in MAPPING if row.key in _AC_INV_3_KEYS]
assert {row.key for row in _NON_TRIGGER_ROWS} == set(_AC_INV_3_KEYS)

# (wide, narrow) values per key -- `narrow` always differs from that field's
# built-in default, so a passing assertion proves an override was actually
# observed rather than tolerating the default (VQ-S9-007).
_VALUES_BY_KEY = {
    "ots.tagprefix": ("a/", "b/"),
    "ots.command": ("ots-wide", "ots-narrow"),
    "ots.sourceref": ("refs/heads/wide", "refs/heads/narrow"),
    "ots.signing": ("inherit", "required"),
    "ots.gittimeout": ("45s", "30s"),
    "ots.otstimeout": ("90s", "15s"),
    "ots.initialhistory": ("latest", "all"),
    "ots.proofcommit": ("true", "false"),
}


def _expected_value(row, raw: str):
    """Compute the expected resolved `Config` field value for one `MAPPING`
    row's raw `git config` string, reusing the row's own real validator
    (`row.parse`, imported unmodified from `config.py`) rather than
    reimplementing validation here -- boolean-typed keys resolve through
    Git's own `--type=bool` read instead (see `gitconfig.BOOLEAN_KEYS`),
    which `read_namespace` already turns into a Python `bool` before
    `row.parse` ever runs, so this mirrors that order.
    """
    if row.key in BOOLEAN_KEYS:
        return raw == "true"
    if row.parse is not None:
        return row.parse(raw)
    return raw


_TIER_PAIRS_INV3 = (("system", "global"), ("global", "local"), ("local", "worktree"))

INV3_CASES = [
    pytest.param(
        row,
        wider_tier,
        narrower_tier,
        trigger_scope,
        id=f"{row.key}-{wider_tier}->{narrower_tier}-trig@{trigger_scope}",
    )
    for row in _NON_TRIGGER_ROWS
    for wider_tier, narrower_tier in _TIER_PAIRS_INV3
    for trigger_scope in (t for t in TIERS if t not in (wider_tier, narrower_tier))
]


@pytest.mark.parametrize("row,wider_tier,narrower_tier,trigger_scope", INV3_CASES)
def test_ac_inv_3_non_trigger_key_independent_of_trigger_ladder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    row,
    wider_tier: str,
    narrower_tier: str,
    trigger_scope: str,
) -> None:
    """AC-INV-3 / G3: a non-trigger key set at two scope tiers resolves by
    ordinary per-key Git precedence (narrowest wins) regardless of which,
    genuinely different, third scope tier wins the policy trigger ladder.
    Generated across the 8 AC-INV-3 non-trigger keys x 3 curated
    (wider, narrower) tier pairs x the 2 remaining tiers each pair leaves
    for the trigger = 48 cases; every case's trigger scope is a tier
    distinct from both of the non-trigger key's two scopes, matching the
    AC's "a third, differing scope tier" wording literally.
    """
    wide_value, narrow_value = _VALUES_BY_KEY[row.key]
    tiers_used = {wider_tier, narrower_tier, trigger_scope}
    repo = _prepare_repo(tmp_path, monkeypatch, tiers_used)

    _set_scope(repo, wider_tier, row.key, wide_value)
    _set_scope(repo, narrower_tier, row.key, narrow_value)
    _set_scope(repo, trigger_scope, "ots.everyCommit", "true")

    config = assemble_config(cwd=repo)

    resolved = getattr(getattr(config, row.section), row.field)
    assert resolved == _expected_value(row, narrow_value)
    assert config.policy.every_commit is True

    decision = _evaluate(config.policy, now=NOW)
    assert decision.triggers == frozenset({"every_commit"})


# ---------------------------------------------------------------------------
# AC-INV-4 -- G4, read boundedness is a schema property.
# ---------------------------------------------------------------------------

# Valid values for every one of the `ots.*` keys, all set in `local`
# scope. `ots.fixedTime`/`ots.timezone` must always move as a pair -- either
# both present or neither -- or `_resolve_triggers`'s own cross-field
# validation raises (AC-INV-4 is about invocation count, not about
# exercising that validation error).
_ALL_KEYS = {
    "ots.everycommit": "true",
    "ots.maxage": "6h",
    "ots.fixedtime": "03:30",
    "ots.timezone": "UTC",
    "ots.initialhistory": "all",
    "ots.sourceref": "refs/heads/x",
    "ots.fetchbeforerun": "true",
    "ots.tagprefix": "custom/",
    "ots.requirecleanworktree": "true",
    "ots.signing": "required",
    "ots.proofcommit": "false",
    "ots.proofdirectory": "proofs",
    "ots.squashupgradecommits": "true",
    "ots.command": "custom-ots",
    "ots.otstimeout": "45s",
    "ots.gittimeout": "20s",
}
assert set(_ALL_KEYS) == {row.key for row in MAPPING}

_FIXED_PAIR = frozenset({"ots.fixedtime", "ots.timezone"})


def _build_subsets() -> list[frozenset[str]]:
    """0-through-N is a 2**N lattice; exhaustive enumeration is neither
    feasible nor what VQ-S9-011 asks for ("ideally intermediate subsets").
    This builds a structurally-generated sample that covers every boundary
    (empty, full) and every individual key (as its own singleton, or paired
    with its required companion) plus a few genuinely multi-key subsets,
    rather than one hand-picked sequence.
    """
    all_keys = list(_ALL_KEYS)
    subsets: list[frozenset[str]] = [frozenset(), frozenset(all_keys)]
    seen_pair = False
    for key in all_keys:
        if key in _FIXED_PAIR:
            if seen_pair:
                continue
            seen_pair = True
            subsets.append(_FIXED_PAIR)
        else:
            subsets.append(frozenset({key}))
    half = len(all_keys) // 2

    def _with_pair(keys: list[str]) -> frozenset[str]:
        selected = set(keys)
        if selected & _FIXED_PAIR:
            selected |= _FIXED_PAIR
        return frozenset(selected)

    subsets.append(_with_pair(all_keys[:half]))
    subsets.append(_with_pair(all_keys[half:]))
    subsets.append(_with_pair(all_keys[::2]))
    return list(dict.fromkeys(subsets))


INV4_SUBSETS = _build_subsets()


@pytest.mark.parametrize(
    "subset",
    INV4_SUBSETS,
    ids=[f"{len(s)}keys-{i}" for i, s in enumerate(INV4_SUBSETS)],
)
def test_ac_inv_4_read_boundedness_invariant_under_keys_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subset: frozenset[str]
) -> None:
    """AC-INV-4 / G4: the number of subprocess invocations
    `gitconfig.assemble_config` performs is exactly `1 + B` (B = the
    schema's boolean keys), invariant with how many `ots.*` keys are
    actually set -- asserted identically for the empty subset, singleton
    subsets of every key, and several multi-key subsets up to and
    including the whole schema.

    The counting wrapper below is a real `ProcessRunner`: it delegates every
    call to the genuine `make_process_runner`, which shells out to the real
    `git` binary exactly as production does, and only additionally records
    each call. This is deliberately not a scripted/canned double -- FR-A7
    forbids substituting `gitconfig.read_namespace`'s behaviour here, and
    this wrapper never does; it only observes real behaviour. Exercises the
    real CLI-wired loader (`assemble_config`, the exact function `cli.py`'s
    `run`/`status` subcommands call), not `read_namespace` in isolation.
    """
    repo = _prepare_repo(tmp_path, monkeypatch, set())
    for key in subset:
        _set_scope(repo, "local", key, _ALL_KEYS[key])

    calls: list[list[str]] = []
    real_runner = make_process_runner(timeout=None)

    def _counting_runner(argv, *, cwd, stdin=None):
        calls.append(list(argv))
        return real_runner(argv, cwd=cwd, stdin=stdin)

    assemble_config(cwd=repo, process_runner=_counting_runner)

    assert len(calls) == 1 + len(BOOLEAN_KEYS)
