"""Tests for ``config.py``'s dataclasses and standalone validators.

FS-0015 (git config is the configuration format) removed the TOML loader
(``load_config``) from ``config.py`` in story S5: the loader, its
section-shaped diagnostics, and its own copy of the cross-field
``fixed_time``/``timezone`` check are gone, and the "at least one policy
must be enabled" error is unreachable and gone with them (behaviour 9 --
under the git-config trigger ladder the built-in defaults are always the
bottom rung, so an empty trigger set never arises). What remains here is
coverage for the pieces of ``config.py`` that survive the loader's removal:
the dataclasses' own field defaults, and the standalone parsers/validators
(``parse_signing``, ``parse_initial_history``, ``validate_tag_prefix``,
``validate_proof_directory``) that ``gitconfig.assemble_config`` now calls
directly and that had no
loader-independent coverage of their own (unlike ``parse_duration``,
``parse_fixed_time`` and ``parse_timezone``, which already have dedicated
test_duration.py/test_fixed_time.py/test_timezone.py files).

Coverage that depended on ``load_config`` and had no TOML-specific shape
(mapping, unknown-key rejection, the trigger ladder, the "0"-unbounded
escape hatch, the sub-hour warning) migrated to
tests/test_gitconfig.py's `assemble_config` suite during S4/S5 -- see that
file's module docstring. Coverage that was inherently TOML-shaped (section
tables, TOML type errors, file discovery, the now-unreachable "no policy
enabled" error) has no git-config equivalent and was deleted rather than
migrated; see S5's `result.json` for the full accounting of what was
dropped and why.
"""

from __future__ import annotations

import subprocess
import warnings
from datetime import timedelta
from pathlib import Path

import pytest

from git_ots.config import (
    DEFAULT_MAX_AGE,
    PROOF_DIRECTORY,
    Config,
    ConfigError,
    GitConfig,
    LimitsConfig,
    OpenTimestampsConfig,
    PolicyConfig,
    ProofConfig,
    parse_initial_history,
    parse_signing,
    validate_proof_directory,
    validate_tag_prefix,
)
from git_ots.gitconfig import assemble_config

_SUBHOUR_WARNING = "below one hour"


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


# ---------------------------------------------------------------------------
# Dataclass field defaults -- unaffected by the loader's removal.
# ---------------------------------------------------------------------------


def test_policy_defaults():
    """PolicyConfig's own field defaults carry no trigger.

    The built-in policy is DEFAULT_POLICY, a distinct object -- not this
    dataclass's field defaults. Putting the 24h default here instead would
    mean `PolicyConfig(fixed_time=...)` silently also had a max-age trigger,
    turning every direct construction in the codebase into a two-policy
    configuration.
    """
    policy = PolicyConfig()
    assert policy.every_commit is False
    assert policy.max_age is None
    assert policy.fixed_time is None
    assert policy.timezone is None


def test_git_defaults():
    git = GitConfig()
    assert git.source_ref == "HEAD"
    assert git.fetch_before_run is True
    assert git.tag_prefix == "ots/"
    assert git.require_clean_worktree is False
    assert git.signing == "inherit"
    assert git.signs_generated_objects is False


def test_proof_defaults():
    proof = ProofConfig()
    assert proof.directory == PROOF_DIRECTORY == ".opentimestamps"
    assert proof.commit is True


def test_opentimestamps_defaults():
    ots = OpenTimestampsConfig()
    assert ots.command == "ots"


def test_limits_defaults():
    limits = LimitsConfig()
    assert limits.ots_timeout == timedelta(seconds=120)
    assert limits.git_timeout == timedelta(seconds=60)


def test_default_config_is_the_documented_built_in_policy():
    """`Config()` is what an unconfigured repository runs on.

    It is asserted as a whole rather than field by field so that adding a
    setting without giving it a default -- which would make Config() raise and
    every unconfigured repository fail -- cannot pass.
    """
    config = Config()
    assert config.policy.max_age == DEFAULT_MAX_AGE == timedelta(hours=24)
    assert config.policy.every_commit is False
    assert config.policy.fixed_time is None
    assert config.git == GitConfig()
    assert config.proof == ProofConfig()
    assert config.opentimestamps == OpenTimestampsConfig()
    assert config.limits == LimitsConfig()


# ---------------------------------------------------------------------------
# parse_signing -- direct calls. No dedicated test_signing.py exists (that
# name is already taken by git.py's GPG-signing behaviour tests), and no
# other file exercises this validator directly, so these are the sole
# coverage for its accept/reject surface.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["inherit", "required"])
def test_signing_allowed_values(value):
    assert parse_signing(value) == value


@pytest.mark.parametrize("value", ["", "true", "on", "REQUIRED", "sign", "yes"])
def test_signing_rejects_unknown_values(value):
    with pytest.raises(ConfigError, match="invalid signing"):
        parse_signing(value)


def test_signing_rejects_off_by_name_rather_than_as_a_synonym():
    """`off` is the one wrong value an operator is likely to reach for.

    FS-0002 published the name for a mode that suppresses ambient signing
    configuration, and that mode does not exist. Accepting it as a synonym for
    the default would silently promise suppression that never happens, so the
    error has to say which of the two things it is not doing.
    """
    with pytest.raises(ConfigError) as excinfo:
        parse_signing("off")
    message = str(excinfo.value)
    assert "not implemented" in message
    assert "inherit" in message
    assert "suppress" in message


# ---------------------------------------------------------------------------
# parse_initial_history -- direct calls; no other dedicated coverage.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["latest", "all"])
def test_initial_history_allowed_values(value):
    assert parse_initial_history(value) == value


@pytest.mark.parametrize("value", ["", "recent", "LATEST", "none"])
def test_initial_history_rejects_unknown_values(value):
    with pytest.raises(ConfigError):
        parse_initial_history(value)


# ---------------------------------------------------------------------------
# validate_tag_prefix -- direct calls. test_gitconfig.py's AC-VALID-1 pins
# exactly one bad prefix ("!!bad") through assemble_config's message-reuse
# guarantee; the full accept/reject surface below has no equivalent
# elsewhere.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prefix",
    [
        "ots/",
        "timestamps/",
        "v1/ots/",
        "release-1/",
        "ots.2026/",
        "a/",
    ],
)
def test_tag_prefix_accepts_safe_prefixes(prefix):
    assert validate_tag_prefix(prefix) == prefix


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "ots",
        "/ots/",
        "ots//",
        "ots/../",
        "ots../",
        "ots.lock/",
        "ots /",
        "o ts/",
        "ots~/",
        "ots^/",
        "ots:/",
        "ots?/",
        "ots*/",
        "ots[/",
        "ots\\/",
        "ots@{/}",
        ".ots/",
    ],
)
def test_tag_prefix_rejects_ref_unsafe_prefixes(prefix):
    with pytest.raises(ConfigError):
        validate_tag_prefix(prefix)


# ---------------------------------------------------------------------------
# validate_proof_directory -- direct calls. test_gitconfig.py pins the
# message-reuse guarantee for a handful of values reached through
# `ots.proofDirectory`; the full accept/reject surface lives here.
#
# The rules exist because the value is not just a path: it becomes a Git
# pathspec, the `proof:` annotation of a timestamp tag, and a path parsed back
# out of `git ls-tree` output. So the validator is narrower than the
# filesystem, and every rejection below names a distinct reason rather than a
# generic "invalid".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "directory",
    [
        ".opentimestamps",
        "proofs",
        "proofs/nested",
        "a/b/c",
        "ots-proofs",
        "ots_proofs",
        "proofs.v2",
    ],
)
def test_proof_directory_accepts_relative_paths(directory):
    assert validate_proof_directory(directory) == directory


@pytest.mark.parametrize(
    ("directory", "reason"),
    [
        ("", "proof directory must not be empty"),
        (".", "proof directory must not contain '.' segments"),
        ("./", "proof directory must not end with '/'"),
        ("/", "proof directory must be repository-relative"),
        ("/etc", "proof directory must be repository-relative"),
        ("/tmp/proofs", "proof directory must be repository-relative"),
        ("..", "proof directory must not contain '..' segments"),
        ("../proofs", "proof directory must not contain '..' segments"),
        ("proofs/../other", "proof directory must not contain '..' segments"),
        ("proofs/../../escape", "proof directory must not contain '..' segments"),
        ("./proofs", "proof directory must not contain '.' segments"),
        ("proofs/./nested", "proof directory must not contain '.' segments"),
        ("proofs/", "proof directory must not end with '/'"),
        ("proofs//nested", "proof directory must not contain '' segments"),
        ("~/.proofs", "proof directory must not use '~'"),
        ("proofs\\backslash", "proof directory must use '/' separators"),
        ("proofs/ /spaces", "proof directory segment has disallowed characters"),
        ("pro ofs", "proof directory segment has disallowed characters"),
        (".öts-proofs", "proof directory segment has disallowed characters"),
    ],
)
def test_proof_directory_rejects_invalid_paths(directory, reason):
    """Each case names what the operator must change, not merely that the
    value is wrong -- the reason string is asserted, so two rejections that
    happen to share a code path cannot silently collapse into one
    diagnosis."""
    with pytest.raises(ConfigError) as excinfo:
        validate_proof_directory(directory)
    assert reason in str(excinfo.value)


# ---------------------------------------------------------------------------
# The sub-hour `max_age` operator warning, migrated through the git-config
# path rather than deleted (lead-review remediation carried forward from S4:
# this behaviour was found at risk of silently vanishing in S5's loader
# deletion, so these two assertions are kept alive here rather than trusted
# to test_gitconfig.py's own, narrower coverage of the same warning).
# ---------------------------------------------------------------------------


def test_assemble_config_warns_on_sub_hour_max_age(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.maxAge", "30m"], cwd=repo)

    with pytest.warns(UserWarning, match=_SUBHOUR_WARNING):
        config = assemble_config(cwd=repo)
    assert config.policy.max_age == timedelta(minutes=30)


def test_assemble_config_warns_on_sub_hour_max_age_in_seconds(tmp_path: Path) -> None:
    """AC-CFG-9: the widened grammar's seconds unit gets the identical
    sub-hour treatment as "30m" -- it must parse, not newly reject."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.maxAge", "45s"], cwd=repo)

    with pytest.warns(UserWarning, match=_SUBHOUR_WARNING):
        config = assemble_config(cwd=repo)
    assert config.policy.max_age == timedelta(seconds=45)


@pytest.mark.parametrize("duration", ["1h", "24h", "2d", "7d"])
def test_load_config_no_warning_for_hour_or_longer_max_age(
    tmp_path: Path, duration: str
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["config", "--local", "ots.maxAge", duration], cwd=repo)

    with warnings.catch_warnings(record=True) as warning_list:
        warnings.simplefilter("always")
        config = assemble_config(cwd=repo)
    assert config.policy.max_age >= timedelta(hours=1)
    subhour_warnings = [w for w in warning_list if _SUBHOUR_WARNING in str(w.message)]
    assert not subhour_warnings
