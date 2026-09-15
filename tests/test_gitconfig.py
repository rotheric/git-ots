"""Tests for the raw `ots.*` Git config namespace reader and its `Config`
assembler.

Covers the four verified `git config` output behaviours the reader must
surface without reimplementing (lower-cased names, valueless booleans,
multivar last-entry-wins, `--get-regexp` exit 1 meaning empty), the
distinction between exit code 1 and every other non-zero exit, and the
`1 + B` subprocess invocation bound. See
specs/spec.md configuration contract
and specs/spec.md
S2-git-config-reader/verification.json.

Also covers `assemble_config`'s mapping, validation and trigger-ladder
assembly (behaviours 5-9). See
specs/spec.md
S4-config-mapping-validation-assembly/verification.json.
"""

from __future__ import annotations

import subprocess
import warnings
from dataclasses import fields
from datetime import timedelta
from pathlib import Path

import pytest

from git_ots.config import (
    DEFAULT_MAX_AGE,
    Config,
    ConfigError,
    GitConfig,
    LimitsConfig,
    OpenTimestampsConfig,
    PolicyConfig,
    ProofConfig,
    validate_proof_directory,
)
from git_ots.git import GitCommandError
from git_ots.gitconfig import (
    BOOLEAN_KEYS,
    MAPPING,
    RawConfigValue,
    assemble_config,
    describe_effective_configuration,
    read_namespace,
)


def _git(args: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
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


def _append_valueless_key(repo: Path, key: str) -> None:
    """Append a bare, valueless key under a fresh `[ots]` block.

    `git config` itself refuses to write a valueless entry (every write
    subcommand requires a value argument), so the only way to produce one
    is to append it to the config file directly -- exactly how an operator
    would hand-edit `.git/config` to spell "this trigger is on" with no
    `= true`.
    """

    config_path = repo / ".git" / "config"
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[ots]\n\t{key}\n")


def _scripted_runner(
    script: dict[tuple[str, ...], tuple[int, str, str]],
    recorded: list[dict],
):
    """A `ProcessRunner` double that answers from a fixed script and logs every call.

    Mirrors the injection seam `locate_repository` (git.py:625) and
    `detect_object_format` (git.py:692) already consume -- no real `git`
    binary is invoked, and every invocation the reader makes is recorded
    for the caller to assert against.
    """

    def _runner(argv, *, cwd, stdin=None):
        recorded.append({"argv": list(argv), "cwd": cwd, "stdin": stdin})
        key = tuple(argv[1:])  # drop the leading "git"
        return script[key]

    return _runner


def _regexp_stdout(entries: list[tuple[str, str, str]]) -> str:
    """Build `-z --show-scope --get-regexp` style stdout for non-boolean entries.

    `entries` is a list of `(scope, key, value)`. Matches the empirically
    verified format: `scope\\0key\\nvalue`, each record terminated by a
    trailing NUL, including after the very last one.
    """

    parts: list[str] = []
    for scope, key, value in entries:
        parts.append(scope)
        parts.append(f"{key}\n{value}")
    return "\0".join(parts) + "\0" if parts else ""


def _bool_stdout(scope: str, value: str) -> str:
    return f"{scope}\0{value}\0"


# ---------------------------------------------------------------------------
# The four verified `git config` output behaviours (VQ-S2-004), each its own
# distinctly named test.
# ---------------------------------------------------------------------------


def test_variable_names_are_read_back_lowercased(tmp_path: Path) -> None:
    """(a) Git reports `ots.tagprefix`, not `ots.tagPrefix` -- the reader
    matches on whatever canonical form Git itself hands back, not a
    re-camelCased guess."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.tagPrefix", "stamped/"], cwd=repo)

    records = read_namespace(cwd=repo)

    assert "ots.tagprefix" in records
    assert "ots.tagPrefix" not in records
    assert records["ots.tagprefix"] == RawConfigValue(value="stamped/", scope="local")


def test_valueless_key_under_ots_does_not_break_the_regexp_parse(
    tmp_path: Path,
) -> None:
    """(b) A bare, valueless key emits no `\\n<value>` at all under `-z`
    (unlike every other entry). A non-boolean valueless key must parse to
    a `None` value rather than crashing or misreading the next record."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _append_valueless_key(repo, "tagPrefixBare")

    records = read_namespace(cwd=repo)

    assert records["ots.tagprefixbare"] == RawConfigValue(value=None, scope="local")


def test_multivar_key_resolves_last_entry_matching_real_get(tmp_path: Path) -> None:
    """(c) `--get-regexp` lists every value of a multivar in file order;
    a scalar `--get` of the same key returns only the last. Both paths
    are exercised against real Git, not assumed equivalent: the raw
    listing is checked to genuinely carry both entries, and the reader's
    resolution is checked against Git's own `--get` for the same key."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "--add", "ots.tagPrefix", "first/"], cwd=repo)
    _git(["config", "--local", "--add", "ots.tagPrefix", "second/"], cwd=repo)

    raw_listing = _git(
        ["config", "-z", "--show-scope", "--get-regexp", r"^ots\."], cwd=repo
    )
    assert raw_listing.count("ots.tagprefix\nfirst/") == 1
    assert raw_listing.count("ots.tagprefix\nsecond/") == 1

    real_get = _git(["config", "--get", "ots.tagPrefix"], cwd=repo).strip()
    assert real_get == "second/"

    records = read_namespace(cwd=repo)
    assert records["ots.tagprefix"].value == real_get


def test_get_regexp_exit_code_one_means_empty_namespace(tmp_path: Path) -> None:
    """(d) An unconfigured repository makes `--get-regexp` exit 1. That is
    read as an empty record set, not an exception and not a retried call."""

    repo = tmp_path / "repo"
    _init_repo(repo)

    records = read_namespace(cwd=repo)

    assert records == {}


# ---------------------------------------------------------------------------
# Exit code 1 vs every other non-zero exit (VQ-S2-005).
# ---------------------------------------------------------------------------


def test_get_regexp_non_one_exit_code_propagates_as_git_command_error() -> None:
    """A real Git failure -- corrupt config, permission error, unsupported
    flag -- must not be folded into "namespace empty" alongside exit 1."""

    recorded: list[dict] = []
    script = {
        ("config", "-z", "--show-scope", "--get-regexp", r"^ots\."): (
            129,
            "",
            "fatal: bad config line 3 in file .git/config\n",
        ),
    }
    runner = _scripted_runner(script, recorded)

    with pytest.raises(GitCommandError) as excinfo:
        read_namespace(cwd=Path("/repo"), process_runner=runner)

    assert excinfo.value.exit_code == 129
    assert "bad config line" in excinfo.value.stderr


def test_boolean_type_get_non_one_exit_code_propagates_as_git_command_error() -> None:
    """An unparseable boolean spelling (`maybe`) makes Git's own
    `--type=bool --get` fail with a non-1 exit. That failure must
    propagate; the reader performs no second, tool-implemented check of
    the raw string (AC-BOOL-2)."""

    recorded: list[dict] = []
    script: dict[tuple[str, ...], tuple[int, str, str]] = {
        ("config", "-z", "--show-scope", "--get-regexp", r"^ots\."): (1, "", ""),
        (
            "config",
            "-z",
            "--show-scope",
            "--type=bool",
            "--get-all",
            "ots.everycommit",
        ): (
            128,
            "",
            "fatal: bad boolean config value 'maybe' for 'ots.everycommit'\n",
        ),
        (
            "config",
            "-z",
            "--show-scope",
            "--type=bool",
            "--get-all",
            "ots.fetchbeforerun",
        ): (1, "", ""),
        (
            "config",
            "-z",
            "--show-scope",
            "--type=bool",
            "--get-all",
            "ots.requirecleanworktree",
        ): (1, "", ""),
        (
            "config",
            "-z",
            "--show-scope",
            "--type=bool",
            "--get-all",
            "ots.proofcommit",
        ): (1, "", ""),
    }
    runner = _scripted_runner(script, recorded)

    with pytest.raises(GitCommandError) as excinfo:
        read_namespace(cwd=Path("/repo"), process_runner=runner)

    assert excinfo.value.exit_code == 128
    assert "bad boolean config value" in excinfo.value.stderr


# ---------------------------------------------------------------------------
# AC-BOOL-1: Git's own boolean vocabulary, not a reimplemented table.
# ---------------------------------------------------------------------------


def test_boolean_yes_and_off_resolve_via_gits_own_vocabulary(tmp_path: Path) -> None:
    repo_yes = tmp_path / "repo_yes"
    _init_repo(repo_yes)
    _git(["config", "--local", "ots.everyCommit", "yes"], cwd=repo_yes)
    assert read_namespace(cwd=repo_yes)["ots.everycommit"].value is True

    repo_off = tmp_path / "repo_off"
    _init_repo(repo_off)
    _git(["config", "--local", "ots.everyCommit", "off"], cwd=repo_off)
    assert read_namespace(cwd=repo_off)["ots.everycommit"].value is False


def test_valueless_boolean_key_resolves_to_true(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _append_valueless_key(repo, "everyCommit")

    records = read_namespace(cwd=repo)

    # `enabling_scope` is "local" too: a valueless key under `[ots]` is Git's
    # own spelling of true, so this record does enable the trigger and does
    # claim local's tier (unlike an explicit `false`, which claims nothing).
    assert records["ots.everycommit"] == RawConfigValue(
        value=True, scope="local", enabling_scope="local"
    )


def test_boolean_resolution_ignores_the_raw_string_and_trusts_the_typed_read() -> None:
    """The reader must not hold its own `{'yes': True, 'off': False, ...}`
    table. Proven by making the raw `--get-regexp` value for a boolean key
    something no such table would recognize (`banana`), while the
    dedicated `--type=bool --get` call -- exactly as Git itself would
    resolve a spelling like `on` or `1` -- reports `true`. If the reader
    inspected the raw string itself, this would not resolve to `True`."""

    recorded: list[dict] = []
    script: dict[tuple[str, ...], tuple[int, str, str]] = {
        ("config", "-z", "--show-scope", "--get-regexp", r"^ots\."): (
            0,
            _regexp_stdout([("local", "ots.everycommit", "banana")]),
            "",
        ),
        (
            "config",
            "-z",
            "--show-scope",
            "--type=bool",
            "--get-all",
            "ots.everycommit",
        ): (0, _bool_stdout("local", "true"), ""),
        (
            "config",
            "-z",
            "--show-scope",
            "--type=bool",
            "--get-all",
            "ots.fetchbeforerun",
        ): (1, "", ""),
        (
            "config",
            "-z",
            "--show-scope",
            "--type=bool",
            "--get-all",
            "ots.requirecleanworktree",
        ): (1, "", ""),
        (
            "config",
            "-z",
            "--show-scope",
            "--type=bool",
            "--get-all",
            "ots.proofcommit",
        ): (1, "", ""),
    }
    runner = _scripted_runner(script, recorded)

    records = read_namespace(cwd=Path("/repo"), process_runner=runner)

    assert records["ots.everycommit"].value is True


# ---------------------------------------------------------------------------
# AC-PERF-1: the 1 + B invocation bound, invariant under how many keys are
# set (VQ-S2-001, VQ-S2-009).
# ---------------------------------------------------------------------------

_ALL_BOOLEAN_ARGV: list[tuple[str, ...]] = [
    ("config", "-z", "--show-scope", "--type=bool", "--get-all", key)
    for key in sorted(BOOLEAN_KEYS)
]

_NON_BOOLEAN_SAMPLE: list[tuple[str, str, str]] = [
    ("local", "ots.maxage", "12h"),
    ("local", "ots.fixedtime", "03:00"),
    ("local", "ots.timezone", "UTC"),
    ("local", "ots.initialhistory", "since-configured"),
    ("local", "ots.sourceref", "HEAD"),
    ("local", "ots.tagprefix", "ots/"),
    ("local", "ots.signing", "inherit"),
    ("local", "ots.command", "ots"),
    ("local", "ots.proofdirectory", "proofs"),
    ("local", "ots.otstimeout", "30s"),
    ("local", "ots.gittimeout", "60s"),
]


def _script_for(*, keys_set: bool) -> dict[tuple[str, ...], tuple[int, str, str]]:
    regexp_argv = ("config", "-z", "--show-scope", "--get-regexp", r"^ots\.")
    if keys_set:
        script: dict[tuple[str, ...], tuple[int, str, str]] = {
            regexp_argv: (0, _regexp_stdout(_NON_BOOLEAN_SAMPLE), "")
        }
        for argv in _ALL_BOOLEAN_ARGV:
            script[argv] = (0, _bool_stdout("local", "true"), "")
        return script

    script = {regexp_argv: (1, "", "")}
    for argv in _ALL_BOOLEAN_ARGV:
        script[argv] = (1, "", "")
    return script


def test_invocation_count_is_invariant_between_zero_and_all_fifteen_keys_set() -> None:
    """The ceiling is `1 + B` (B = 4 boolean-typed keys), so at most 5.
    A repository with zero `ots.*` keys set must cost exactly the same
    number of invocations as one with all fifteen set -- the count comes
    from the injected `ProcessRunner`'s own call log, not a hardcoded
    expected-count literal."""

    recorded_zero: list[dict] = []
    read_namespace(
        cwd=Path("/repo-zero"),
        process_runner=_scripted_runner(_script_for(keys_set=False), recorded_zero),
    )

    recorded_all: list[dict] = []
    result_all = read_namespace(
        cwd=Path("/repo-all"),
        process_runner=_scripted_runner(_script_for(keys_set=True), recorded_all),
    )

    ceiling = 1 + len(BOOLEAN_KEYS)
    assert len(recorded_zero) == ceiling
    assert len(recorded_all) == ceiling
    assert len(recorded_zero) == len(recorded_all)
    # Sanity: the "all fifteen" fixture actually resolved all fifteen.
    assert len(result_all) == len(_NON_BOOLEAN_SAMPLE) + len(BOOLEAN_KEYS)


def test_invocation_count_does_not_short_circuit_to_an_empty_result() -> None:
    """A reader that silently skipped invoking `git config` at all would
    also return an empty dict for the zero-keys fixture -- this asserts
    against the call log directly, not just the returned shape, so that
    kind of short-circuit is caught."""

    recorded: list[dict] = []
    result = read_namespace(
        cwd=Path("/repo-zero"),
        process_runner=_scripted_runner(_script_for(keys_set=False), recorded),
    )

    assert result == {}
    assert len(recorded) == 1 + len(BOOLEAN_KEYS)
    assert recorded[0]["argv"] == [
        "git",
        "config",
        "-z",
        "--show-scope",
        "--get-regexp",
        r"^ots\.",
    ]


# ---------------------------------------------------------------------------
# Injection seam (VQ-S2-002, VQ-S2-003): consumed exactly as
# `locate_repository` / `detect_object_format` already consume it.
# ---------------------------------------------------------------------------


def test_process_runner_injection_seam_receives_fixed_cwd_and_argument_array() -> None:
    recorded: list[dict] = []
    fixed_cwd = Path("/some/repo")
    runner = _scripted_runner(_script_for(keys_set=False), recorded)

    read_namespace(cwd=fixed_cwd, process_runner=runner)

    assert all(call["cwd"] == fixed_cwd for call in recorded)
    assert all(isinstance(call["argv"], list) for call in recorded)
    assert all(call["argv"][0] == "git" for call in recorded)


# ---------------------------------------------------------------------------
# S4: mapping, validation and Config assembly (behaviours 5-9).
# ---------------------------------------------------------------------------


def test_mapping_table_is_bidirectionally_complete_against_config_fields() -> None:
    """AC-MAP-1: every mapping row has a `Config` field, and every `Config`
    field has a mapping row -- checked both ways as a single set-equality,
    not by walking the mapping table and checking each entry against
    `Config` (which would miss an orphaned `Config` field with no key)."""

    section_dataclasses = {
        "policy": PolicyConfig,
        "git": GitConfig,
        "proof": ProofConfig,
        "opentimestamps": OpenTimestampsConfig,
        "limits": LimitsConfig,
    }
    assert {f.name for f in fields(Config)} == set(section_dataclasses)

    mapped_pairs = {(row.section, row.field) for row in MAPPING}
    config_pairs = {
        (section, f.name)
        for section, dataclass_type in section_dataclasses.items()
        for f in fields(dataclass_type)
    }

    assert mapped_pairs == config_pairs
    assert len(MAPPING) == 15


def test_local_and_global_scope_values_both_survive_assembly(tmp_path: Path) -> None:
    """AC-MERGE-1: `ots.requireCleanWorktree=true` (local) and
    `ots.maxAge=12h` (global) both survive assembly. Neither is a default
    (`require_clean_worktree` defaults False, `max_age` defaults 24h), and
    the *global*-scope `maxAge` value specifically must survive -- a stub
    that resolves only the narrowest scope's keys and drops any
    wider-scope key it does not itself override would fail this."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--global", "ots.maxAge", "12h"], cwd=repo)
    _git(["config", "--local", "ots.requireCleanWorktree", "true"], cwd=repo)

    config = assemble_config(cwd=repo)

    assert config.git.require_clean_worktree is True
    assert config.policy.max_age == timedelta(hours=12)


def test_invalid_tag_prefix_reuses_validate_tag_prefix_message_verbatim(
    tmp_path: Path,
) -> None:
    """AC-VALID-1: `ots.tagPrefix="!!bad"` fails with `validate_tag_prefix`'s
    own message, pinned as a literal string -- reused unmodified from
    config.py (config.py:224), not reworded."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.tagPrefix", "!!bad"], cwd=repo)

    with pytest.raises(ConfigError) as excinfo:
        assemble_config(cwd=repo)

    assert str(excinfo.value) == "tag prefix must end with '/': '!!bad'"


def test_unknown_key_is_rejected_naming_the_key_and_the_namespace(
    tmp_path: Path,
) -> None:
    """AC-VALID-2: `ots.nonsenseKey=1` is rejected, naming both the
    canonical lower-case key (`ots.nonsensekey`, matching `git config
    --list`) and the `ots` namespace -- not the operator's original
    mixed-case spelling, and not silently dropped as if unset."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.nonsenseKey", "1"], cwd=repo)

    with pytest.raises(ConfigError) as excinfo:
        assemble_config(cwd=repo)

    message = str(excinfo.value)
    assert "ots.nonsensekey" in message
    assert "ots.nonsenseKey" not in message
    assert "ots" in message


def test_proof_directory_is_a_mapped_key_not_an_unknown_one(
    tmp_path: Path,
) -> None:
    """`ots.proofDirectory` reaches `Config.proof.directory`.

    This reverses FS-0015 behaviour 15, under which the key had no mapping
    row and was rejected by the same unknown-key path `ots.nonsenseKey`
    takes. The value below is deliberately not the default, so a mapping
    that silently dropped the key -- leaving `ProofConfig`'s own default in
    place -- fails here rather than passing by coincidence.
    """

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.proofDirectory", "proofs/nested"], cwd=repo)

    config = assemble_config(cwd=repo)

    assert config.proof.directory == "proofs/nested"
    # The sibling key in the same section is untouched by the new row.
    assert config.proof.commit is True


def test_unset_proof_directory_keeps_the_documented_default(
    tmp_path: Path,
) -> None:
    """An unconfigured repository still stores proofs in `.opentimestamps`.

    The point of the default is that a consumer who clones a repository
    nobody reconfigured can find the proofs without being told where to
    look, so it is pinned against the literal spelling rather than against
    `ProofConfig()`'s own field -- which would agree with itself however
    the default were changed.
    """

    repo = tmp_path / "repo"
    _init_repo(repo)

    assert assemble_config(cwd=repo).proof.directory == ".opentimestamps"


@pytest.mark.parametrize(
    "value",
    [
        "/absolute/proofs",
        "../escape",
        "proofs/../escape",
        "proofs/",
        "~/proofs",
    ],
)
def test_proof_directory_rejection_reuses_the_validator_message(
    tmp_path: Path, value: str
) -> None:
    """AC-VALID-1's guarantee applied to the new key: an invalid
    `ots.proofDirectory` fails with `validate_proof_directory`'s own
    diagnosis, not a reworded one. Each value below escapes the worktree or
    breaks the no-trailing-separator shape every consumer's f-string
    assumes."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.proofDirectory", value], cwd=repo)

    with pytest.raises(ConfigError) as excinfo:
        assemble_config(cwd=repo)

    with pytest.raises(ConfigError) as direct:
        validate_proof_directory(value)

    assert str(excinfo.value) == str(direct.value)
    assert "proof directory" in str(excinfo.value)


def test_every_commit_false_enables_nothing_default_max_age_wins(
    tmp_path: Path,
) -> None:
    """AC-TRIG-1: `ots.everyCommit=off` with no other trigger key set
    anywhere resolves cleanly to the default `max_age` trigger -- a false
    trigger enables nothing and is never mistaken for "no policy enabled"
    (behaviour 9's failure mode is unreachable)."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.everyCommit", "off"], cwd=repo)
    _git(["config", "--local", "ots.tagPrefix", "custom/"], cwd=repo)

    config = assemble_config(cwd=repo)

    assert config.policy.every_commit is False
    assert config.policy.max_age == DEFAULT_MAX_AGE
    assert config.policy.fixed_time is None
    # every_commit=False, DEFAULT_MAX_AGE and fixed_time=None are exactly
    # what a loader that never read the `ots.*` namespace would also
    # produce (the dataclass default and the no-enabling-triggers
    # fallback branch coincide). tag_prefix pins the namespace read: it
    # is a non-trigger key with a non-default value that only lands on
    # `Config` if `ots.tagPrefix` was actually read from this scope.
    assert config.git.tag_prefix == "custom/"


# ---------------------------------------------------------------------------
# S5 gap-closing additions. `load_config`'s deletion (config.py's own copy of
# the fixed_time/timezone cross-field check, and the TOML-shaped tests that
# exercised it) left these behaviours without a git-config-path test; added
# here since they exercise `gitconfig.py` directly, alongside the rest of
# this module's `assemble_config` coverage.
# ---------------------------------------------------------------------------


def test_fixed_time_without_timezone_is_rejected(tmp_path: Path) -> None:
    """The cross-field check migrated from `config.py`'s (now-deleted)
    `load_config` to `_resolve_triggers`, which is its sole implementation
    as of S5. `ots.fixedTime` alone, with no `ots.timezone` in the same
    winning scope, is rejected."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.fixedTime", "03:00"], cwd=repo)

    with pytest.raises(ConfigError, match="ots.fixedTime requires"):
        assemble_config(cwd=repo)


def test_timezone_without_fixed_time_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.everyCommit", "true"], cwd=repo)
    _git(["config", "--local", "ots.timezone", "Europe/Berlin"], cwd=repo)

    with pytest.raises(ConfigError, match="ots.timezone is only meaningful"):
        assemble_config(cwd=repo)


def test_all_four_boolean_keys_round_trip_through_assemble_config(
    tmp_path: Path,
) -> None:
    """Every boolean in the `ots.*` schema must arrive at the assembled
    `Config` exactly as written, via real `git config` -- not merely via
    `read_namespace`'s lower-level records (already covered above) or
    `_resolve_triggers` (which only ever exercises `ots.everyCommit`
    among the four). `ots.fetchBeforeRun` and `ots.proofCommit` in
    particular reach `Config.git.fetch_before_run` /
    `Config.proof.commit` through the generic per-key path in
    `assemble_config`, which no other test in this module exercises for
    either key."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.everyCommit", "true"], cwd=repo)
    _git(["config", "--local", "ots.fetchBeforeRun", "false"], cwd=repo)
    _git(["config", "--local", "ots.requireCleanWorktree", "true"], cwd=repo)
    _git(["config", "--local", "ots.proofCommit", "false"], cwd=repo)

    config = assemble_config(cwd=repo)

    assert config.policy.every_commit is True
    assert config.git.fetch_before_run is False
    assert config.git.require_clean_worktree is True
    assert config.proof.commit is False


def test_non_default_timeout_values_parse_through_assemble_config(
    tmp_path: Path,
) -> None:
    """`ots.otsTimeout`/`ots.gitTimeout` parse non-"0" durations through
    `_parse_timeout` -> `parse_duration`. AC-ESCAPE-1's own coverage only
    exercises the "0"-unbounded spelling; a positive duration is a
    distinct code path (the non-"0" branch) with no other coverage."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.otsTimeout", "5s"], cwd=repo)
    _git(["config", "--local", "ots.gitTimeout", "3m"], cwd=repo)

    config = assemble_config(cwd=repo)

    assert config.limits.ots_timeout == timedelta(seconds=5)
    assert config.limits.git_timeout == timedelta(minutes=3)


def test_a_git_only_key_leaves_policy_at_the_default(tmp_path: Path) -> None:
    """Setting only a non-trigger `[git]`-section key must not disturb the
    policy ladder: with no trigger key set anywhere, the assembled
    `PolicyConfig` is exactly `DEFAULT_POLICY`, matching an unconfigured
    repository."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.requireCleanWorktree", "true"], cwd=repo)

    config = assemble_config(cwd=repo)

    assert config.git.require_clean_worktree is True
    assert config.policy == PolicyConfig(max_age=DEFAULT_MAX_AGE)


def test_worktree_scope_diverges_from_sibling_worktree(tmp_path: Path) -> None:
    """AC-SCOPE-1: `git config --worktree ots.requireCleanWorktree=true`
    set in one linked worktree produces an assembled `Config` in that
    worktree differing from a sibling worktree's, and neither worktree's
    `Config` reflects the other's `--worktree`-scoped value. Both
    worktrees are asserted independently, not just the modified one."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "extensions.worktreeConfig", "true"], cwd=repo)

    worktree_a = tmp_path / "wt-a"
    worktree_b = tmp_path / "wt-b"
    _git(["worktree", "add", str(worktree_a), "-b", "wt-a-branch"], cwd=repo)
    _git(["worktree", "add", str(worktree_b), "-b", "wt-b-branch"], cwd=repo)

    _git(
        ["config", "--worktree", "ots.requireCleanWorktree", "true"],
        cwd=worktree_a,
    )
    _git(["config", "--worktree", "ots.tagPrefix", "wt-a/"], cwd=worktree_a)
    _git(["config", "--worktree", "ots.tagPrefix", "wt-b/"], cwd=worktree_b)

    config_a = assemble_config(cwd=worktree_a)
    config_b = assemble_config(cwd=worktree_b)

    assert config_a.git.require_clean_worktree is True
    assert config_b.git.require_clean_worktree is False
    # `require_clean_worktree is False` on worktree B is both the
    # dataclass default and what a loader that never read the
    # `--worktree` scope would also produce, so on its own it does not
    # prove B's own worktree config was read. Giving each worktree a
    # distinct, non-default `tagPrefix` and asserting both pins that
    # each `Config` reflects its *own* `--worktree` scope, not a
    # never-read default and not the other worktree's value.
    assert config_a.git.tag_prefix == "wt-a/"
    assert config_b.git.tag_prefix == "wt-b/"


# ---------------------------------------------------------------------------
# AC-TRIG-2: behaviour 8's worked table, one test per row.
# ---------------------------------------------------------------------------


def test_trigger_ladder_row_no_scopes_set_resolves_default(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    config = assemble_config(cwd=repo)

    assert config.policy.max_age == DEFAULT_MAX_AGE
    assert config.policy.fixed_time is None
    assert config.policy.every_commit is False


def test_trigger_ladder_row_global_only_max_age(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--global", "ots.maxAge", "12h"], cwd=repo)

    config = assemble_config(cwd=repo)

    assert config.policy.max_age == timedelta(hours=12)
    assert config.policy.fixed_time is None


def test_trigger_ladder_row_load_bearing_narrower_fixed_time_discards_wider_max_age(
    tmp_path: Path,
) -> None:
    """The load-bearing row: global `ots.maxAge=12h` plus local
    `ots.fixedTime=03:00` (+ `ots.timezone`) resolves to `fixed_time` only,
    with `max_age is None` -- not both merged, and not `fixed_time` with
    `max_age` left at its non-`None` global value."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--global", "ots.maxAge", "12h"], cwd=repo)
    _git(["config", "--local", "ots.fixedTime", "03:00"], cwd=repo)
    _git(["config", "--local", "ots.timezone", "Europe/Berlin"], cwd=repo)

    config = assemble_config(cwd=repo)

    assert config.policy.fixed_time is not None
    assert config.policy.fixed_time.hour == 3
    assert config.policy.fixed_time.minute == 0
    assert config.policy.timezone is not None
    assert config.policy.timezone.key == "Europe/Berlin"
    assert config.policy.max_age is None


def test_trigger_ladder_row_same_key_narrower_scope_overrides_wider(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--global", "ots.maxAge", "12h"], cwd=repo)
    _git(["config", "--local", "ots.maxAge", "6h"], cwd=repo)

    config = assemble_config(cwd=repo)

    assert config.policy.max_age == timedelta(hours=6)
    assert config.policy.fixed_time is None


def test_trigger_ladder_row_both_triggers_set_in_one_scope(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.fixedTime", "03:00"], cwd=repo)
    _git(["config", "--local", "ots.timezone", "Europe/Berlin"], cwd=repo)
    _git(["config", "--local", "ots.maxAge", "6h"], cwd=repo)

    config = assemble_config(cwd=repo)

    assert config.policy.max_age == timedelta(hours=6)
    assert config.policy.fixed_time is not None
    assert config.policy.fixed_time.hour == 3
    assert config.policy.timezone is not None
    assert config.policy.timezone.key == "Europe/Berlin"


# `every_commit` is a `bool` whose `PolicyConfig` default is `False`, and every
# worked row above that exercises a *discarded wider-scope* trigger discards
# either `max_age` (a duration, default `DEFAULT_MAX_AGE`, so a wrongly-kept
# value and a correctly-discarded one are trivially distinguishable) or an
# `everyCommit=off` value, which behaviour 9 already treats as inert and so
# never reaches the ladder's discard branch either way. None of them puts a
# *true* `ots.everyCommit` through the discard path. That gap matters because
# the failure mode is asymmetric: a regression that wrongly *keeps* a
# discarded wider-scope `everyCommit=true` produces `True`, which an assertion
# would catch -- but a regression that wrongly *drops* a winning narrower-scope
# `everyCommit=true` produces `False`, indistinguishable from the field's own
# default unless a test asserts `True` explicitly. The three tests below close
# that gap: discard, the converse (survive), and same-tier coexistence.
def test_trigger_ladder_wider_scope_boolean_every_commit_is_discarded(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--global", "ots.everyCommit", "true"], cwd=repo)
    _git(["config", "--local", "ots.maxAge", "6h"], cwd=repo)

    config = assemble_config(cwd=repo)

    assert config.policy.every_commit is False
    assert config.policy.max_age == timedelta(hours=6)


def test_trigger_ladder_narrower_scope_boolean_every_commit_wins(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--global", "ots.maxAge", "12h"], cwd=repo)
    _git(["config", "--local", "ots.everyCommit", "true"], cwd=repo)

    config = assemble_config(cwd=repo)

    assert config.policy.every_commit is True
    assert config.policy.max_age is None


def test_trigger_ladder_same_scope_boolean_every_commit_and_max_age_coexist(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.everyCommit", "true"], cwd=repo)
    _git(["config", "--local", "ots.maxAge", "6h"], cwd=repo)

    config = assemble_config(cwd=repo)

    assert config.policy.every_commit is True
    assert config.policy.max_age == timedelta(hours=6)


# ---------------------------------------------------------------------------
# AC-ESCAPE-1: the "0"-unbounded escape hatch, confined to the two timeouts.
# ---------------------------------------------------------------------------


def test_zero_git_timeout_and_ots_timeout_mean_unbounded(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.gitTimeout", "0"], cwd=repo)
    _git(["config", "--local", "ots.otsTimeout", "0"], cwd=repo)

    config = assemble_config(cwd=repo)

    assert config.limits.git_timeout is None
    assert config.limits.ots_timeout is None


def test_zero_max_age_is_rejected_the_escape_hatch_does_not_leak(
    tmp_path: Path,
) -> None:
    """The `"0"`-means-unbounded spelling is confined to
    `ots.gitTimeout`/`ots.otsTimeout`; `ots.maxAge=0` goes through the same
    `parse_duration` every other duration-valued key uses, and
    `parse_duration` rejects a non-positive duration outright."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.maxAge", "0"], cwd=repo)

    with pytest.raises(ConfigError):
        assemble_config(cwd=repo)


def test_bogus_git_timeout_diagnostic_mentions_the_zero_affordance(
    tmp_path: Path,
) -> None:
    """An unparsable `ots.gitTimeout` must still surface `parse_duration`'s
    own diagnosis (single-parser discipline: `_parse_timeout` never
    reimplements or reword it), but since `"0"` is a real, documented
    escape hatch confined to this key (AC-ESCAPE-1), the diagnostic also
    tells the operator that `"0"` means unbounded -- otherwise a typo'd
    timeout gives no hint the affordance exists."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.gitTimeout", "bogus"], cwd=repo)

    with pytest.raises(ConfigError) as excinfo:
        assemble_config(cwd=repo)

    message = str(excinfo.value)
    assert "unsupported duration: 'bogus'" in message
    assert "0" in message


def test_bogus_ots_timeout_diagnostic_mentions_the_zero_affordance(
    tmp_path: Path,
) -> None:
    """Same as `ots.gitTimeout` above, for `ots.otsTimeout` -- both keys
    share `_parse_timeout` as their parser, so both must get the
    augmented diagnostic."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.otsTimeout", "bogus"], cwd=repo)

    with pytest.raises(ConfigError) as excinfo:
        assemble_config(cwd=repo)

    message = str(excinfo.value)
    assert "unsupported duration: 'bogus'" in message
    assert "0" in message


def test_bogus_max_age_diagnostic_does_not_mention_the_zero_affordance(
    tmp_path: Path,
) -> None:
    """Pins the confinement (AC-ESCAPE-1): `ots.maxAge` calls
    `parse_duration` directly, not `_parse_timeout`, so its diagnostic
    must stay exactly `parse_duration`'s own message with no `"0"`
    mention -- `ots.maxAge=0` is itself rejected, so advertising the
    affordance here would be actively wrong. Without this test, a future
    change routing `ots.maxAge` through `_parse_timeout`'s augmentation
    could leak the hint onto every duration key undetected."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.maxAge", "bogus"], cwd=repo)

    with pytest.raises(ConfigError) as excinfo:
        assemble_config(cwd=repo)

    message = str(excinfo.value)
    assert message == "unsupported duration: 'bogus'"


# ---------------------------------------------------------------------------
# The sub-hour `max_age` operator warning, carried forward from
# `config.py`'s `load_config` (see `SUBHOUR_MAX_AGE_WARNING`'s docstring)
# so the behaviour survives S5's deletion of `load_config`. No acceptance
# criterion names it; it is a lead-review remediation, not a story AC.
# ---------------------------------------------------------------------------


def test_sub_hour_max_age_warns(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.maxAge", "30m"], cwd=repo)

    with pytest.warns(UserWarning, match="below one hour"):
        config = assemble_config(cwd=repo)

    assert config.policy.max_age == timedelta(minutes=30)


def test_above_hour_max_age_does_not_warn(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.maxAge", "2h"], cwd=repo)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        config = assemble_config(cwd=repo)

    assert config.policy.max_age == timedelta(hours=2)


def test_max_age_discarded_by_narrower_fixed_time_does_not_warn(
    tmp_path: Path,
) -> None:
    """A wider-scope sub-hour `ots.maxAge` that the trigger ladder discards
    in favour of a narrower-scope `ots.fixedTime` must not warn -- that
    `max_age` value never reaches the assembled `Config` (behaviour 8's
    discard, not a raw `ots.maxAge` read)."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--global", "ots.maxAge", "30m"], cwd=repo)
    _git(["config", "--local", "ots.fixedTime", "03:00"], cwd=repo)
    _git(["config", "--local", "ots.timezone", "Europe/Berlin"], cwd=repo)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        config = assemble_config(cwd=repo)

    assert config.policy.max_age is None
    assert config.policy.fixed_time is not None


# ---------------------------------------------------------------------------
# S6: `describe_effective_configuration` -- behaviour 10, AC-REPORT-1.
# Backs `git-ots config` (cli.py's `_config_command`); see
# specs/spec.md
# S6-config-subcommand/verification.json.
# ---------------------------------------------------------------------------

#: The full origin vocabulary AC-REPORT-1 permits. Used as an explicit
#: membership guard below, not merely to spot-check individual strings --
#: a report that quietly invented a seventh origin kind (a `flag` origin,
#: or a raw file path) would still pass a test that only checks for the
#: presence of a few expected strings.
_ALLOWED_ORIGINS = frozenset(
    {
        "git config system",
        "git config global",
        "git config local",
        "git config worktree",
        "git config command",
        "default",
    }
)


def test_describe_effective_configuration_origin_vocabulary_matches_exactly() -> None:
    """AC-REPORT-1's full vocabulary, exercised with one fake record per
    scope (including `command`, unreachable through a real single-repo
    fixture without a `-c` subprocess -- see
    test_config_command_output_vocabulary_is_exactly_the_six_permitted_origins
    in test_cli.py for that path exercised for real) plus keys left
    entirely unset. Asserted as exact set equality against
    `_ALLOWED_ORIGINS`, not mere membership, so a reader that drops one
    origin kind or emits an extra one is caught either way -- this is the
    "distinct assertion" VQ-S6-004/005 ask for, not incidental coverage
    from the other origin-specific tests below."""

    recorded: list[dict] = []
    entries = [
        ("system", "ots.tagprefix", "sys/"),
        ("global", "ots.sourceref", "refs/heads/global"),
        ("local", "ots.signing", "required"),
        ("worktree", "ots.initialhistory", "all"),
        ("command", "ots.command", "cscope-ots"),
    ]
    script = {
        ("config", "-z", "--show-scope", "--get-regexp", r"^ots\."): (
            0,
            _regexp_stdout(entries),
            "",
        ),
    }
    for key in sorted(BOOLEAN_KEYS):
        script[("config", "-z", "--show-scope", "--type=bool", "--get-all", key)] = (
            1,
            "",
            "",
        )
    runner = _scripted_runner(script, recorded)

    config, origins = describe_effective_configuration(
        cwd=Path("/repo"), process_runner=runner
    )

    assert origins["ots.tagprefix"] == "git config system"
    assert origins["ots.sourceref"] == "git config global"
    assert origins["ots.signing"] == "git config local"
    assert origins["ots.initialhistory"] == "git config worktree"
    assert origins["ots.command"] == "git config command"
    # Every trigger key and every boolean key is unset in this fixture ->
    # "default", supplying the sixth vocabulary member.
    assert origins["ots.everycommit"] == "default"
    assert origins["ots.maxage"] == "default"

    assert set(origins) == {row.key for row in MAPPING}
    assert set(origins.values()) == _ALLOWED_ORIGINS

    assert config.git.tag_prefix == "sys/"
    assert config.opentimestamps.command == "cscope-ots"


def test_describe_effective_configuration_does_not_issue_a_second_namespace_read() -> (
    None
):
    """VQ-S6-001: origin comes from the same raw records `read_namespace`
    already produced -- calling `describe_effective_configuration` costs no
    more subprocess invocations than `assemble_config` costs for the same
    fixture, and both stay at the `1 + B` ceiling. A subcommand that
    re-derived origin via a fresh `--show-scope` call of its own would
    double this."""

    ceiling = 1 + len(BOOLEAN_KEYS)

    # The invocation count is fixed by `read_namespace` before either
    # function's own validation runs, so it is captured even from
    # `_NON_BOOLEAN_SAMPLE`'s deliberately-unvalidated `ots.initialHistory`
    # sample value (`assemble_config`/`describe_effective_configuration`
    # reject it, which is irrelevant to what is being measured here).
    recorded_assemble: list[dict] = []
    try:
        assemble_config(
            cwd=Path("/repo"),
            process_runner=_scripted_runner(
                _script_for(keys_set=True), recorded_assemble
            ),
        )
    except ConfigError:
        pass

    recorded_describe: list[dict] = []
    try:
        describe_effective_configuration(
            cwd=Path("/repo"),
            process_runner=_scripted_runner(
                _script_for(keys_set=True), recorded_describe
            ),
        )
    except ConfigError:
        pass

    assert len(recorded_assemble) == ceiling
    assert len(recorded_describe) == ceiling
    assert len(recorded_describe) == len(recorded_assemble)


def test_describe_effective_configuration_reports_narrower_winning_scope_for_two_scope_key(
    tmp_path: Path,
) -> None:
    """AC-REPORT-1's core case: a non-trigger key set in both local and
    global scope reports its origin as the narrower, winning `local` scope
    -- never `global`, even though `global` is also present in the raw
    records."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--global", "ots.tagPrefix", "wide/"], cwd=repo)
    _git(["config", "--local", "ots.tagPrefix", "narrow/"], cwd=repo)

    config, origins = describe_effective_configuration(cwd=repo)

    assert config.git.tag_prefix == "narrow/"
    assert origins["ots.tagprefix"] == "git config local"
    assert origins["ots.tagprefix"] != "git config global"
    assert set(origins.values()) <= _ALLOWED_ORIGINS


def test_describe_effective_configuration_reports_default_for_a_key_set_in_no_scope(
    tmp_path: Path,
) -> None:
    """AC-REPORT-1's other half: a key never set in any scope reports
    `default`. Asserted across the whole mapping, not one key, so a reader
    that defaults to some other placeholder for an absent record is
    caught."""

    repo = tmp_path / "repo"
    _init_repo(repo)

    _config, origins = describe_effective_configuration(cwd=repo)

    assert set(origins.values()) == {"default"}
    assert set(origins) == {row.key for row in MAPPING}


def test_describe_effective_configuration_reports_default_for_a_trigger_the_ladder_discards(
    tmp_path: Path,
) -> None:
    """The story's central design question. `ots.maxAge` set in global
    scope loses behaviour 8's precedence ladder to `ots.fixedTime` (+
    `ots.timezone`) set in local scope -- the wider-scope `max_age` value
    never reaches the assembled `Config` at all
    (`test_max_age_discarded_by_narrower_fixed_time_does_not_warn` already
    covers that half). Its origin must therefore read `default`, not
    `git config global`: crediting the discarded scope would claim the
    effective configuration carries a value it does not. The winning
    `ots.fixedTime`/`ots.timezone` report their real (local) scope."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--global", "ots.maxAge", "12h"], cwd=repo)
    _git(["config", "--local", "ots.fixedTime", "03:00"], cwd=repo)
    _git(["config", "--local", "ots.timezone", "UTC"], cwd=repo)

    config, origins = describe_effective_configuration(cwd=repo)

    assert config.policy.max_age is None
    assert config.policy.fixed_time is not None
    assert origins["ots.maxage"] == "default"
    assert origins["ots.fixedtime"] == "git config local"
    assert origins["ots.timezone"] == "git config local"


def test_describe_effective_configuration_never_credits_a_non_enabling_false_trigger_with_its_scope(
    tmp_path: Path,
) -> None:
    """Behaviour 9: `ots.everyCommit=false` "asserts the default state" and
    claims nothing, even when it sits at the ladder's own winning scope
    (here, alongside a genuinely enabling `ots.maxAge` in the same, local,
    scope). Its origin reports `default`, not `git config local` --
    reporting the scope would credit the record with claiming the tier,
    which behaviour 9 explicitly says it does not do."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.everyCommit", "false"], cwd=repo)
    _git(["config", "--local", "ots.maxAge", "6h"], cwd=repo)

    config, origins = describe_effective_configuration(cwd=repo)

    assert config.policy.every_commit is False
    assert config.policy.max_age == timedelta(hours=6)
    assert origins["ots.everycommit"] == "default"
    assert origins["ots.maxage"] == "git config local"


def test_describe_effective_configuration_reports_worktree_scope(
    tmp_path: Path,
) -> None:
    """A key set with `git config --worktree` reports `git config
    worktree`, distinct from every other scope."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "extensions.worktreeConfig", "true"], cwd=repo)

    worktree = tmp_path / "wt"
    _git(["worktree", "add", str(worktree), "-b", "wt-branch"], cwd=repo)
    _git(["config", "--worktree", "ots.tagPrefix", "wt/"], cwd=worktree)

    config, origins = describe_effective_configuration(cwd=worktree)

    assert config.git.tag_prefix == "wt/"
    assert origins["ots.tagprefix"] == "git config worktree"
