"""Raw reader and ``Config`` assembler for the ``ots.*`` Git config namespace.

Reads every ``ots.*`` key Git config knows about -- across system, global,
local, worktree, and ``-c``/``GIT_CONFIG_*`` scope -- and returns raw
``key -> (value, scope)`` records. See specs/spec.md section 28.7 and
the configuration contract in specs/spec.md.

``read_namespace`` surfaces exactly what Git itself reports, including
Git's own boolean parsing, so it never re-implements a boolean vocabulary
Git already owns. ``assemble_config`` builds on it: the flat key-mapping
table (behaviour 5), unknown-key rejection (behaviour 6), the reused
``config.py`` validators (behaviour 7), the ``"0"``-unbounded escape hatch
confined to the two timeout keys, and the policy-trigger precedence
ladder resolved by scope tier with enabling-value semantics (behaviours
8-9).
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .config import (
    DEFAULT_MAX_AGE,
    SUBHOUR_MAX_AGE_WARNING,
    Config,
    ConfigError,
    GitConfig,
    LimitsConfig,
    OpenTimestampsConfig,
    PolicyConfig,
    ProofConfig,
    parse_duration,
    parse_fixed_time,
    parse_initial_history,
    parse_signing,
    parse_timezone,
    validate_proof_directory,
    validate_tag_prefix,
)
from .git import GitCommandError, GitRunner, ProcessRunner

# The pattern passed to `--get-regexp`. Anchored so it matches only the
# `ots.` namespace, not a key that merely contains "ots" elsewhere.
_NAMESPACE_PATTERN = r"^ots\."

# The boolean-typed keys in the ots.* schema, in their canonical lower-case
# form (Git lower-cases variable names; see `read_namespace`).
# This is B in the "1 + B" invocation bound: each is read with its own
# `--type=bool --get`, unconditionally, so the invocation count never
# depends on which keys an operator happened to set.
BOOLEAN_KEYS: frozenset[str] = frozenset(
    {
        "ots.everycommit",
        "ots.fetchbeforerun",
        "ots.requirecleanworktree",
        "ots.proofcommit",
        "ots.squashupgradecommits",
    }
)


@dataclass(frozen=True, slots=True)
class RawConfigValue:
    """One resolved ``ots.*`` record, exactly as Git config reported it.

    ``value`` is a Python ``bool`` for the keys in :data:`BOOLEAN_KEYS`,
    resolved by Git's own ``--type=bool --get`` -- never by a lookup table
    this module implements. For every other key it is the raw string Git
    reported, already collapsed to last-entry-wins for a multivar (matching
    what ``git config --get`` would return). ``scope`` is one of Git's own
    scope names (``system``, ``global``, ``local``, ``worktree``,
    ``command``) as reported by ``--show-scope``.
    """

    value: str | bool
    scope: str
    #: For a *boolean trigger* key only (currently just ``ots.everyCommit``),
    #: the narrowest scope at which this key is set ``true`` -- or ``None``
    #: when no scope sets it true. ``scope`` above is Git's single winning
    #: record, which is the right answer for every ordinary key but hides a
    #: wider enabling value behind a narrower inert one: behaviour 9 makes a
    #: false trigger inert, so ``ots.everyCommit=false`` in ``local`` must
    #: neither claim ``local``'s tier nor suppress a ``true`` in ``global``
    #: (AC-INV-2). The ladder therefore needs the narrowest *true* scope,
    #: which ``git config --get`` alone cannot express. ``None`` for every
    #: non-boolean-trigger key, which resolves by ordinary Git precedence.
    enabling_scope: str | None = None


def read_namespace(
    *,
    cwd: Path,
    process_runner: ProcessRunner | None = None,
) -> dict[str, RawConfigValue]:
    """Read the ``ots.*`` Git config namespace.

    Costs exactly ``1 + B`` subprocess invocations, where ``B`` is the
    number of boolean-typed keys in the schema (five): one
    ``git config -z --show-scope --get-regexp '^ots\\.'`` read of the
    whole namespace resolves every non-boolean key, and one
    ``git config -z --show-scope --type=bool --get`` read per schema
    boolean key resolves that key's presence, value and scope together
    -- run unconditionally for all of them, so the count never depends on
    how many keys, boolean or not, an operator has set.
    """

    runner = GitRunner(cwd=cwd, process_runner=process_runner)

    records: dict[str, RawConfigValue] = {}
    for key, (value, scope) in _read_raw_entries(runner).items():
        if key in BOOLEAN_KEYS:
            continue  # resolved independently below, via Git's typed read
        records[key] = RawConfigValue(value=value, scope=scope)

    for key in BOOLEAN_KEYS:
        resolved = _read_boolean(runner, key)
        if resolved is not None:
            records[key] = resolved

    return records


def _read_raw_entries(runner: GitRunner) -> dict[str, tuple[str | None, str]]:
    """Run the single namespace-wide ``--get-regexp`` invocation.

    Returns ``key -> (value, scope)``, keyed by Git's canonical lower-case
    key. When a key appears more than once (a multivar, or the same key
    set in more than one scope), the *last* occurrence in Git's own output
    order wins -- the same entry ``git config --get`` would return for
    that key. ``value`` is ``None`` for a valueless entry (a bare key
    under ``[ots]`` with no ``=``); Git's ``-z`` output marks this by
    emitting no ``\\n<value>`` at all for that record, which is why a
    parser assuming every record splits on a newline mis-reads exactly
    this case. (Boolean-typed keys resolve their valueless case through
    :func:`_read_boolean` instead; entries for them here are discarded by
    the caller.)

    ``--get-regexp`` exiting with code 1 means "no key in this namespace
    is set anywhere" -- the common case for an unconfigured repository --
    and is read as an empty record set, not an error. Every other
    non-zero exit is a real Git failure (a corrupt config file, a
    permission error, an unsupported flag on too-old Git) and propagates
    as :class:`~git_ots.git.GitCommandError` unchanged.
    """

    try:
        stdout = runner.run(
            ["config", "-z", "--show-scope", "--get-regexp", _NAMESPACE_PATTERN]
        ).stdout
    except GitCommandError as exc:
        if exc.exit_code == 1:
            return {}
        raise

    parts = stdout.split("\0")
    if parts and parts[-1] == "":
        parts = parts[:-1]

    entries: dict[str, tuple[str | None, str]] = {}
    for i in range(0, len(parts), 2):
        scope = parts[i]
        key_and_value = parts[i + 1]
        if "\n" in key_and_value:
            key, value = key_and_value.split("\n", 1)
        else:
            key, value = key_and_value, None
        entries[key] = (value, scope)

    return entries


def _read_boolean(runner: GitRunner, key: str) -> RawConfigValue | None:
    """Resolve one schema boolean key via Git's own typed read.

    Returns ``None`` when ``key`` is not set in any scope (``--get`` exits
    1 for a missing key, exactly as it does for an unmatched
    ``--get-regexp``). Any other non-zero exit -- for example Git
    rejecting an unparseable boolean spelling such as ``maybe`` --
    propagates as :class:`~git_ots.git.GitCommandError`: the rejection is
    Git's own, not a second boolean-value check this module performs.
    """

    try:
        stdout = runner.run(
            ["config", "-z", "--show-scope", "--type=bool", "--get-all", key]
        ).stdout
    except GitCommandError as exc:
        if exc.exit_code == 1:
            return None
        raise

    # `--get-all` emits `scope\0value\0` per record, widest scope first, and
    # costs the same single invocation `--get` did -- so the `1 + B` bound
    # (AC-PERF-1, AC-INV-4) is unchanged. `--get` would return only the last
    # record, which is the winning value but discards whether some *wider*
    # scope set this key true; see `enabling_scope` on `RawConfigValue`.
    fields = stdout.split("\0")[:-1]
    scoped = [
        (fields[index + 1] == "true", fields[index])
        for index in range(0, len(fields), 2)
    ]
    winning_value, winning_scope = scoped[-1]
    enabling_scope = next((scope for value, scope in reversed(scoped) if value), None)
    return RawConfigValue(
        value=winning_value, scope=winning_scope, enabling_scope=enabling_scope
    )


# ---------------------------------------------------------------------------
# Mapping, validation and Config assembly (behaviours 5-9).
# ---------------------------------------------------------------------------

# Git's own scope precedence, narrowest (highest-precedence) last. "command"
# covers `-c` and `GIT_CONFIG_*` (behaviour 10) and outranks every file-based
# scope. Used only to compare which of several *already per-key-resolved*
# records (see MappingRow docstring) sits in the narrowest scope -- never to
# re-resolve precedence within one key, which `read_namespace` already did.
_SCOPE_TIER: dict[str, int] = {
    "system": 0,
    "global": 1,
    "local": 2,
    "worktree": 3,
    "command": 4,
}


@dataclass(frozen=True, slots=True)
class MappingRow:
    """One row of behaviour 5's ``ots.*`` -> ``Config`` field mapping table.

    ``parse`` is the validator to apply to the raw string Git reported --
    reused unmodified from ``config.py``, never reimplemented here (per
    architecture.md Boundary Rule 3) -- or ``None`` for a value
    `read_namespace` already produced in its final form (a Python ``bool``
    for the schema's boolean keys, a plain string for a str field).

    ``is_trigger`` marks the four keys behaviour 8's scope-tier ladder
    governs (``ots.everyCommit``, ``ots.maxAge``, ``ots.fixedTime``,
    ``ots.timezone``); :func:`assemble_config` resolves those separately,
    via :func:`_resolve_triggers`, rather than through the generic
    per-key loop every other row goes through.
    """

    key: str
    section: str
    field: str
    parse: Callable[[str], object] | None
    is_trigger: bool = False


def _parse_timeout(text: str) -> timedelta | None:
    """Resolve one ``ots.gitTimeout``/``ots.otsTimeout`` value.

    The ``"0"``-means-unbounded escape hatch (behaviour 7) is confined to
    these two keys by construction: it is only ever reached through the two
    :data:`MAPPING` rows that name this function as their parser.  Any other
    string is handed to :func:`~git_ots.config.parse_duration`, so
    :func:`~git_ots.config.parse_duration` stays the sole authority on what
    a valid duration is -- this function never branches on value shape
    itself. Its :class:`~git_ots.config.ConfigError` is caught only to
    append a mention of the ``"0"`` affordance, never to reword the
    diagnosis; the escape hatch still cannot leak into ``ots.maxAge`` or
    any other duration-valued key, which calls ``parse_duration`` directly
    and so never gains this mention (AC-ESCAPE-1).
    """

    if text == "0":
        return None
    try:
        return parse_duration(text)
    except ConfigError as exc:
        raise ConfigError(f'{exc} (or "0" for unbounded)') from exc


#: Behaviour 5's key-mapping table -- sixteen rows, one per ``Config``
#: field across ``PolicyConfig`` (5), ``GitConfig`` (5), ``ProofConfig``
#: (3), ``OpenTimestampsConfig`` (1) and ``LimitsConfig`` (2).
#:
#: ``ots.proofDirectory`` has a row again. FS-0015 behaviour 15 had no
#: row for it and :func:`assemble_config` rejected it as an unknown key; that
#: is reversed -- the proof directory is configurable again, and the
#: bidirectional field/key correspondence this table asserts holds with it
#: mapped rather than with the field removed.
MAPPING: tuple[MappingRow, ...] = (
    MappingRow("ots.everycommit", "policy", "every_commit", None, is_trigger=True),
    MappingRow("ots.maxage", "policy", "max_age", parse_duration, is_trigger=True),
    MappingRow(
        "ots.fixedtime", "policy", "fixed_time", parse_fixed_time, is_trigger=True
    ),
    MappingRow("ots.timezone", "policy", "timezone", parse_timezone, is_trigger=True),
    MappingRow(
        "ots.initialhistory", "policy", "initial_history", parse_initial_history
    ),
    MappingRow("ots.sourceref", "git", "source_ref", None),
    MappingRow("ots.fetchbeforerun", "git", "fetch_before_run", None),
    MappingRow("ots.tagprefix", "git", "tag_prefix", validate_tag_prefix),
    MappingRow("ots.requirecleanworktree", "git", "require_clean_worktree", None),
    MappingRow("ots.signing", "git", "signing", parse_signing),
    MappingRow("ots.proofcommit", "proof", "commit", None),
    MappingRow("ots.proofdirectory", "proof", "directory", validate_proof_directory),
    MappingRow("ots.squashupgradecommits", "proof", "squash_upgrade_commits", None),
    MappingRow("ots.command", "opentimestamps", "command", None),
    MappingRow("ots.otstimeout", "limits", "ots_timeout", _parse_timeout),
    MappingRow("ots.gittimeout", "limits", "git_timeout", _parse_timeout),
)

_MAPPING_BY_KEY: dict[str, MappingRow] = {row.key: row for row in MAPPING}

# Sanity: MAPPING must name every boolean key the raw reader resolves, and
# no others -- both modules describe the same sixteen-key schema.
assert {
    row.key for row in MAPPING if row.parse is None and row.key in BOOLEAN_KEYS
} == BOOLEAN_KEYS


#: The display form of a mapped key's origin, per behaviour 10 / AC-REPORT-1:
#: either the two-word ``"git config <scope>"`` naming the winning scope, or
#: the literal string ``"default"``. No other origin kind is ever produced --
#: see :func:`describe_effective_configuration`.
_DEFAULT_ORIGIN = "default"


def _scope_origin(scope: str) -> str:
    return f"git config {scope}"


def _resolve_triggers(
    records: dict[str, RawConfigValue],
) -> tuple[dict[str, object], dict[str, str]]:
    """Resolve behaviour 8's precedence ladder over the policy trigger set.

    ``records`` already carries each trigger key's *single* winning
    ``(value, scope)`` -- `read_namespace`'s own last-entry-wins collapse
    already applied Git's cross-scope precedence per key, exactly as
    ``git config --get`` would. What remains is the whole-*set* decision
    behaviour 8 adds on top: the narrowest scope naming any *enabling*
    trigger (behaviour 9 -- a false ``ots.everyCommit`` never counts) wins
    entirely, and trigger keys resolved at any wider scope are discarded
    even when the winning scope never named them (architecture.md G1).

    Every present trigger value is validated regardless of whether its own
    scope ultimately wins the ladder, so an invalid value anywhere -- not
    only at the winning scope -- still fails loudly (behaviour 7,
    AC-ESCAPE-1's ``ots.maxAge=0`` rejection).

    Returns the four resolved ``PolicyConfig`` field values alongside each
    trigger key's report origin (S6, AC-REPORT-1), keyed by the same
    canonical ``ots.*`` spelling :data:`MAPPING` uses. A trigger key that
    was set but discarded by the ladder reports ``"default"``, the same as
    a key never set anywhere: its value does not survive into the effective
    ``PolicyConfig``, so crediting the scope that lost would attribute a
    value the report is not actually showing (this is the "most interesting
    design question" the story instructions describe -- see
    :func:`describe_effective_configuration`'s docstring for the fuller
    reasoning). Likewise ``ots.everyCommit=false`` reports ``"default"``
    even at the winning scope: behaviour 9 already treats a false trigger as
    "assert[ing] the default state", and the origin report follows that
    same reading rather than inventing a second one.
    """

    every_commit_rec = records.get("ots.everycommit")
    max_age_rec = records.get("ots.maxage")
    fixed_time_rec = records.get("ots.fixedtime")
    timezone_rec = records.get("ots.timezone")

    max_age_value = None
    if max_age_rec is not None:
        if max_age_rec.value is None:
            raise ConfigError("ots.maxAge requires a value")
        max_age_value = parse_duration(max_age_rec.value)

    fixed_time_value = None
    if fixed_time_rec is not None:
        if fixed_time_rec.value is None:
            raise ConfigError("ots.fixedTime requires a value")
        fixed_time_value = parse_fixed_time(fixed_time_rec.value)

    timezone_value = None
    if timezone_rec is not None:
        if timezone_rec.value is None:
            raise ConfigError("ots.timezone requires a value")
        timezone_value = parse_timezone(timezone_rec.value)

    # A false `ots.everyCommit` is inert (behaviour 9): it neither claims its
    # own tier nor suppresses a `true` at a wider scope, so the ladder asks
    # for the narrowest scope that sets the key *true* -- not for Git's single
    # winning record, which a narrower false would otherwise hide (AC-INV-2).
    every_commit_enabling_scope = (
        every_commit_rec.enabling_scope if every_commit_rec is not None else None
    )

    enabling_tiers = []
    if every_commit_enabling_scope is not None:
        enabling_tiers.append(_SCOPE_TIER[every_commit_enabling_scope])
    if max_age_rec is not None:
        enabling_tiers.append(_SCOPE_TIER[max_age_rec.scope])
    if fixed_time_rec is not None:
        enabling_tiers.append(_SCOPE_TIER[fixed_time_rec.scope])

    if not enabling_tiers:
        # No scope names an enabling trigger anywhere: the built-in default
        # is the bottom rung of the ladder, so this is not a failure
        # (behaviour 9's "at least one policy must be enabled" error is
        # unreachable here). Every trigger key's origin is "default",
        # including one that is merely present-but-non-enabling (e.g. a
        # lone `ots.everyCommit=false`) -- it claims nothing, per behaviour
        # 9, so it is not credited with any scope.
        return (
            {
                "every_commit": False,
                "max_age": DEFAULT_MAX_AGE,
                "fixed_time": None,
                "timezone": None,
            },
            {
                "ots.everycommit": _DEFAULT_ORIGIN,
                "ots.maxage": _DEFAULT_ORIGIN,
                "ots.fixedtime": _DEFAULT_ORIGIN,
                "ots.timezone": _DEFAULT_ORIGIN,
            },
        )

    winning_tier = max(enabling_tiers)

    def _at_winning_tier(rec: RawConfigValue | None) -> bool:
        return rec is not None and _SCOPE_TIER[rec.scope] == winning_tier

    every_commit = (
        every_commit_enabling_scope is not None
        and _SCOPE_TIER[every_commit_enabling_scope] == winning_tier
    )
    max_age = max_age_value if _at_winning_tier(max_age_rec) else None
    fixed_time = fixed_time_value if _at_winning_tier(fixed_time_rec) else None
    timezone = timezone_value if _at_winning_tier(timezone_rec) else None

    if fixed_time is not None and timezone is None:
        raise ConfigError(
            "ots.fixedTime requires a configured ots.timezone in the same scope"
        )
    if timezone is not None and fixed_time is None:
        raise ConfigError(
            "ots.timezone is only meaningful with ots.fixedTime in the same scope"
        )

    # Each origin reflects whether *this* key's own record is the one that
    # actually determined the resolved value above -- not merely whether the
    # record is present, and not merely whether it sits at the winning tier
    # (an every_commit=false record can sit at the winning tier via a
    # sibling trigger in the same scope and still contribute nothing).
    origins = {
        "ots.everycommit": (
            _scope_origin(every_commit_enabling_scope)
            if every_commit and every_commit_enabling_scope is not None
            else _DEFAULT_ORIGIN
        ),
        "ots.maxage": (
            _scope_origin(max_age_rec.scope) if max_age is not None else _DEFAULT_ORIGIN
        ),
        "ots.fixedtime": (
            _scope_origin(fixed_time_rec.scope)
            if fixed_time is not None
            else _DEFAULT_ORIGIN
        ),
        "ots.timezone": (
            _scope_origin(timezone_rec.scope)
            if timezone is not None
            else _DEFAULT_ORIGIN
        ),
    }

    return (
        {
            "every_commit": every_commit,
            "max_age": max_age,
            "fixed_time": fixed_time,
            "timezone": timezone,
        },
        origins,
    )


def _assemble(
    records: dict[str, RawConfigValue],
) -> tuple[Config, dict[str, str]]:
    """Validate ``records`` and assemble both the ``Config`` and its origins.

    Shared implementation behind :func:`assemble_config` (which discards the
    origin map -- every pre-S6 caller only ever wanted the ``Config``) and
    :func:`describe_effective_configuration` (S6, which wants both). Keeping
    one function here means the origin report can never drift from the value
    assembly it describes: they are computed in the same pass, from the same
    ``records``, so a `git-ots config` origin is never derived by re-deciding
    anything `assemble_config` already decided.

    Unknown keys (any key `read_namespace` reports that :data:`MAPPING`
    does not name) are rejected with
    :class:`~git_ots.config.ConfigError`, naming the canonical lower-case
    key and the ``ots`` namespace. Every present
    value is validated with its mapped parser; a validator's
    :class:`~git_ots.config.ConfigError` propagates unchanged (behaviour
    7 -- no rewording). The four policy-trigger keys resolve through
    :func:`_resolve_triggers` instead of the generic per-key path.

    The returned origin map has exactly :data:`MAPPING`'s sixteen keys,
    each valued ``"git config <scope>"`` or ``"default"`` -- the full
    vocabulary AC-REPORT-1 permits and no other.
    """

    for key in records:
        if key not in _MAPPING_BY_KEY:
            raise ConfigError(
                f"unknown configuration key {key!r} in the 'ots' git config namespace"
            )

    section_kwargs: dict[str, dict[str, object]] = {
        "policy": {},
        "git": {},
        "proof": {},
        "opentimestamps": {},
        "limits": {},
    }
    origins: dict[str, str] = {}

    for row in MAPPING:
        if row.is_trigger:
            continue
        record = records.get(row.key)
        if record is None:
            origins[row.key] = _DEFAULT_ORIGIN
            continue
        if record.value is None:
            raise ConfigError(f"{row.key} requires a value")
        value = row.parse(record.value) if row.parse is not None else record.value
        section_kwargs[row.section][row.field] = value
        origins[row.key] = _scope_origin(record.scope)

    trigger_values, trigger_origins = _resolve_triggers(records)
    section_kwargs["policy"].update(trigger_values)
    origins.update(trigger_origins)

    config = Config(
        policy=PolicyConfig(**section_kwargs["policy"]),
        git=GitConfig(**section_kwargs["git"]),
        proof=ProofConfig(**section_kwargs["proof"]),
        opentimestamps=OpenTimestampsConfig(**section_kwargs["opentimestamps"]),
        limits=LimitsConfig(**section_kwargs["limits"]),
    )

    # Same operator warning `config.py`'s `load_config` emits (behaviour
    # carried forward across S4/S5 -- see `SUBHOUR_MAX_AGE_WARNING`'s
    # docstring). Checked against the *assembled* `max_age` -- the value
    # that survived `_resolve_triggers`' scope-tier ladder -- never a raw
    # `ots.maxAge` read, so a `max_age` a narrower-scope trigger discarded
    # does not warn.
    if config.policy.max_age is not None and config.policy.max_age < timedelta(hours=1):
        warnings.warn(SUBHOUR_MAX_AGE_WARNING, UserWarning, stacklevel=1)

    return config, origins


def assemble_config(
    *,
    cwd: Path,
    process_runner: ProcessRunner | None = None,
) -> Config:
    """Read the ``ots.*`` namespace and assemble a validated ``Config``.

    See :func:`_assemble` for what "assemble" means here; this wrapper reads
    the namespace once and keeps only the ``Config`` half of its result. Used
    by every command except ``git-ots config`` (S6), which needs the origin
    map too and calls :func:`describe_effective_configuration` instead --
    reading the namespace itself only once either way.
    """

    records = read_namespace(cwd=cwd, process_runner=process_runner)
    config, _origins = _assemble(records)
    return config


def describe_effective_configuration(
    *,
    cwd: Path,
    process_runner: ProcessRunner | None = None,
) -> tuple[Config, dict[str, str]]:
    """Read the ``ots.*`` namespace and report it with per-key origins.

    Behaviour 10 / AC-REPORT-1: backs ``git-ots config`` (S6). Reuses the
    single ``read_namespace`` call :func:`assemble_config` also makes --
    the ``--show-scope`` data behaviour 3's one namespace invocation already
    captured -- rather than issuing a second `git config` read to discover
    origin, which would silently double this call's invocation count against
    the ``1 + B`` bound AC-PERF-1/AC-INV-4 pin for every other command.

    The origin map has one entry per :data:`MAPPING` row (sixteen keys,
    canonical lower-case ``ots.*`` spelling), each either
    ``"git config <scope>"`` -- Git's own ``system``/``global``/``local``/
    ``worktree``, or ``command`` for ``-c``/``GIT_CONFIG_*`` -- or the
    literal string ``"default"``. No other origin kind is producible.

    A policy-trigger key (``ots.everyCommit``, ``ots.maxAge``,
    ``ots.fixedTime``, ``ots.timezone``) set in a scope behaviour 8's
    ladder discards reports ``"default"``, not the discarded scope. The
    ladder resolves the trigger *set* by scope tier, not per key (behaviour
    8), so a wider-scope ``ots.maxAge`` beaten by a narrower-scope
    ``ots.fixedTime`` never reaches ``Config.policy.max_age`` at all --
    that field is ``None`` in the effective configuration, identically to
    a repository where ``ots.maxAge`` was never set anywhere. Reporting the
    discarded scope would claim the effective configuration carries a value
    it does not: the report describes what *is* effective, not everywhere an
    operator has written a key. This is also why a bare ``ots.everyCommit=
    false`` never reports its own scope even when nothing else contests the
    ladder -- behaviour 9 already reads a false trigger as "assert[ing] the
    default state", so the origin report treats it exactly as it treats an
    absent key.
    """

    records = read_namespace(cwd=cwd, process_runner=process_runner)
    return _assemble(records)
