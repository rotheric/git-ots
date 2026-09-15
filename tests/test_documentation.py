"""Documentation tests ensuring README covers required topics."""

from __future__ import annotations

from pathlib import Path

import pytest

README = Path(__file__).resolve().parents[1] / "README.md"
SPEC = Path(__file__).resolve().parents[1] / "specs" / "spec.md"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "relative_path",
    [
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CODE_OF_CONDUCT.md",
        ".github/ISSUE_TEMPLATE/bug_report.md",
        ".github/ISSUE_TEMPLATE/feature_request.md",
        ".github/pull_request_template.md",
    ],
)
def test_public_project_files_are_present_and_nonempty(relative_path: str) -> None:
    path = PROJECT_ROOT / relative_path
    assert path.is_file(), relative_path
    assert path.read_text(encoding="utf-8").strip(), relative_path


@pytest.fixture
def readme_text() -> str:
    if not README.exists():
        pytest.fail(f"README file not found: {README}")
    return README.read_text(encoding="utf-8")


@pytest.fixture
def spec_text() -> str:
    if not SPEC.exists():
        pytest.fail(f"spec file not found: {SPEC}")
    return SPEC.read_text(encoding="utf-8")


def test_readme_documents_installation_and_run_commands(readme_text: str) -> None:
    text = readme_text.lower()
    assert "uv" in text, "README should mention uv installation"
    assert "uv sync" in text or "uv run" in text, "README should include uv commands"
    assert "git-ots run" in text, "README should document the run command"
    assert "git-ots status" in text or "--dry-run" in text, (
        "README should document status or dry-run"
    )


def test_spec_is_the_only_specification_document() -> None:
    files = sorted(
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "specs").rglob("*")
        if path.is_file()
    )
    assert files == ["specs/spec.md"]


def test_spec_has_no_development_record_references(spec_text: str) -> None:
    assert "specs/features" not in spec_text
    assert "./features/" not in spec_text
    assert "./adr/" not in spec_text
    assert "epic-fs" not in spec_text


def test_readme_documents_canonical_payload_format(readme_text: str) -> None:
    assert (
        "git:<object-format>:<full-commit-id>" in readme_text
        or "git:sha1:" in readme_text
    ), "README should document the canonical payload format"


def test_readme_documents_complete_configuration(readme_text: str) -> None:
    """FS-0015: configuration lives entirely in the `ots.*` git config
    namespace now -- there is no TOML file or `[section]` grouping to
    document. Checks one representative key per former TOML table."""
    assert "ots.maxAge" in readme_text, (
        "README should document policy keys (e.g. ots.maxAge)"
    )
    assert "ots.sourceRef" in readme_text, (
        "README should document git-execution keys (e.g. ots.sourceRef)"
    )
    assert "ots.proofCommit" in readme_text, (
        "README should document the proof-commit key"
    )
    assert "ots.proofDirectory" in readme_text, (
        "README should document the proof-directory key"
    )
    assert "ots.command" in readme_text, (
        "README should document the OpenTimestamps client key"
    )


def test_readme_defaults_table_names_every_mapped_config_key(readme_text: str) -> None:
    """The defaults table is the operator-facing schema, so it must list every
    `ots.*` key the mapping table recognises -- checked against `MAPPING`
    itself rather than a hand-maintained list, so a key added to the code
    without a table row fails here instead of being discovered by an operator.

    Compared case-insensitively: `MAPPING` carries Git's canonical lower-case
    spelling and the README uses the camelCase form for readability.
    """
    from git_ots.gitconfig import MAPPING

    lowered = readme_text.lower()
    missing = [row.key for row in MAPPING if f"| `{row.key}` |" not in lowered]
    assert not missing, f"README defaults table is missing rows for {missing}"


def test_readme_documents_source_ref_and_upstream_behavior(readme_text: str) -> None:
    assert "@{upstream}" in readme_text, (
        "README should document the default upstream source ref"
    )
    assert "unpushed" in readme_text.lower() or "upstream" in readme_text.lower(), (
        "README should explain upstream-only behavior"
    )


def test_readme_documents_proof_layout(readme_text: str) -> None:
    assert ".opentimestamps" in readme_text, (
        "README should document the proof directory"
    )
    assert ".ots" in readme_text, "README should mention the proof file extension"
    assert ".json" in readme_text, "README should mention the manifest extension"


def test_readme_documents_tag_format(readme_text: str) -> None:
    assert "ots/" in readme_text, "README should document the tag prefix"
    assert "ots/<utc-timestamp>/<short-sha>" in readme_text or "ots/" in readme_text, (
        "README should document the timestamp tag format"
    )


def test_readme_documents_make_install_and_target_table(readme_text: str) -> None:
    assert "make install" in readme_text, (
        "README should document the executable installation"
    )
    assert "| target" in readme_text.lower(), (
        "README should document the make target table"
    )
    assert "make uninstall" in readme_text.lower(), (
        "README should mention make uninstall"
    )
    assert "make check" in readme_text.lower(), "README should mention make check"


def test_readme_documents_that_configuration_is_optional(readme_text: str) -> None:
    """FS-0014: there is no initialization step, and the README must not send
    an operator looking for one. The default policy is documented text, not a
    starter file -- so it can only be learned here."""
    lower = readme_text.lower()
    assert "git-ots init" not in lower, (
        "README should not document an init subcommand; it no longer exists"
    )
    assert "there is no initialization step" in lower, (
        "README should state that no initialization step is needed"
    )
    assert "configuration is optional" in lower, (
        "README should state that configuration is optional"
    )
    assert "`ots.maxAge` set to `24h`" in readme_text, (
        "README should name the built-in default policy"
    )


def test_readme_documents_configuration_discovery(readme_text: str) -> None:
    """FS-0015: there is no configuration file to discover any more, and no
    `--config` flag -- configuration is read from the `ots.*` git config
    namespace across Git's normal scopes."""
    assert "git-ots.toml" not in readme_text, (
        "README should not reference the removed git-ots.toml file"
    )
    assert "status --config" not in readme_text and "run --config" not in readme_text, (
        "README should not document invoking the removed --config flag"
    )
    assert "ots.*" in readme_text, (
        "README should document the ots.* git config namespace"
    )
    lower = readme_text.lower()
    for scope in ("system", "global", "local", "worktree"):
        assert scope in lower, f"README should document the {scope!r} git config scope"


def test_readme_documents_upstream_requirement(readme_text: str) -> None:
    lower = readme_text.lower()
    assert "@{upstream}" in readme_text, "README should document the default source ref"
    assert "upstream branch" in lower or "configured upstream" in lower, (
        "README should explain the upstream requirement"
    )


def test_readme_documents_first_run_baseline_behavior(readme_text: str) -> None:
    lower = readme_text.lower()
    assert "no timestamp tag" in lower, (
        "README should document first-run baseline behavior"
    )
    assert "initial_history" in lower, "README should document initial_history"
    assert "every_commit" in lower, (
        "README should note initial_history only affects every_commit"
    )


def test_readme_documents_non_goals(readme_text: str) -> None:
    lower = readme_text.lower()
    assert "never pushes" in lower, "README should state that git-ots never pushes"
    assert "never runs" in lower and "ots upgrade" in lower, (
        "README should state that git-ots never runs ots upgrade"
    )


def test_readme_documents_policies_and_scheduling(readme_text: str) -> None:
    lower = readme_text.lower()
    assert "every_commit" in readme_text, (
        "README should document the every_commit policy"
    )
    assert "max_age" in readme_text, "README should document the max_age policy"
    assert "fixed_time" in readme_text, "README should document the fixed_time policy"
    assert "scheduler latency" in lower or "max_age +" in lower, (
        "README should document the max_age + scheduler latency guarantee"
    )
    assert "invocation-driven" in lower or "invocation" in lower, (
        "README should document invocation-driven behavior"
    )
    assert "cron" in lower, "README should include a cron scheduler example"
    assert "systemd" in lower, "README should include a systemd timer example"
    assert "ci" in lower or "pipeline" in lower, (
        "README should include a CI or pipeline scheduler example"
    )
    assert "aggregation" in lower or "combine" in lower or "logical or" in lower, (
        "README should document OR/aggregation semantics"
    )


def test_readme_documents_idempotency_failures_recovery_and_verification(
    readme_text: str,
) -> None:
    lower = readme_text.lower()
    assert "idempot" in lower or "idempotent" in lower, (
        "README should document idempotency of repeated runs"
    )
    assert "clean-worktree" in lower or "requirecleanworktree" in lower, (
        "README should document clean-worktree safety"
    )
    assert "lock" in lower, "README should document repository locking"
    assert "crash" in lower or "recover" in lower or "interruption" in lower, (
        "README should document crash recovery behavior"
    )
    assert "exit code" in lower, "README should document exit codes"
    assert "verify" in lower or "ots verify" in lower, (
        "README should document real proof verification"
    )


def test_readme_documents_head_default_and_reframed_upstream_rationale(
    readme_text: str,
) -> None:
    lower = readme_text.lower()
    assert "`ots.sourceref` is `head`" in lower, (
        "README should document the HEAD default for ots.sourceRef"
    )
    assert "churn-and-cost" in lower or "churn and cost" in lower, (
        "README should explain upstream-only mode as a churn-and-cost preference"
    )


def test_readme_documents_recommended_scheduler_cadence(readme_text: str) -> None:
    lower = readme_text.lower()
    assert "once per hour" in lower or "hourly" in lower, (
        "README should recommend hourly-or-slower invocation"
    )
    assert "bitcoin block interval" in lower or "block interval" in lower, (
        "README should document the sub-hour policy warning"
    )


def test_readme_distinguishes_validation_from_chain_verification(
    readme_text: str,
) -> None:
    lower = readme_text.lower()
    assert "git-ots validate" in lower, "README should document offline validation"
    assert "git-ots verify" in lower, "README should document the verify subcommand"
    assert "valid" in lower, "README should document the valid validation result"
    assert "orphaned" in lower, "README should document the orphaned validation result"
    assert "pending-attestation" in lower
    assert "verification-failed" in lower
    assert "bitcoin node" in lower


def test_readme_documents_anchor_reporting(readme_text: str) -> None:
    lower = readme_text.lower()
    assert "op_return" in lower, "README should document the OP_RETURN commitment"
    assert "merkle root" in lower, "README should document the block merkle root"
    assert "block 963292" in lower or "block " in lower, (
        "README should show the reported block height"
    )
    assert "awaiting" in lower, "README should document outstanding calendars"


def test_readme_documents_calendar_reachability_check(readme_text: str) -> None:
    lower = readme_text.lower()
    assert "--check-servers" in lower, "README should document the reachability flag"
    assert "unavailable" in lower, "README should document the unavailable state"
    assert "no network access" in lower or "performs no network" in lower, (
        "README should state that status is offline by default"
    )


def test_readme_documents_upgrade_subcommand_and_states(readme_text: str) -> None:
    lower = readme_text.lower()
    assert "git-ots upgrade" in lower, "README should document the upgrade subcommand"
    assert "still-pending" in lower, (
        "README should document the still-pending upgrade result"
    )
    assert "already-complete" in lower, (
        "README should document the already-complete upgrade result"
    )
    assert "opentimestamps-upgraded" in lower, (
        "README should document the upgrade commit trailer"
    )
    assert "run` never runs `ots upgrade" in readme_text, (
        "README should scope the non-goal to the scheduled run path"
    )


def test_readme_documents_no_automatic_removal_non_goal(readme_text: str) -> None:
    lower = readme_text.lower()
    assert "never removes" in lower and "proofs or tags" in lower, (
        "README should state that git-ots never removes proofs or tags automatically"
    )


def test_spec_documents_the_two_timeout_keys(spec_text: str) -> None:
    """AC-DOC-1, inverted by FS-0015 (AC-STRUCT-3): spec.md names both timeout
    keys and the "0" unbounded value under their `ots.*` git config spelling.

    The `[limits]` TOML table this originally asserted no longer exists in any
    form the tool reads, so asserting its presence would pin spec.md to a
    format the tool cannot be configured with -- the same inversion already
    applied to the README's equivalent assertion.
    """
    assert "ots.otsTimeout" in spec_text, "spec.md should name ots.otsTimeout"
    assert "ots.gitTimeout" in spec_text, "spec.md should name ots.gitTimeout"
    assert "unbounded" in spec_text.lower(), (
        'spec.md should document the "0" unbounded escape hatch'
    )
    assert "[limits]" not in spec_text, (
        "spec.md should no longer document a [limits] TOML table"
    )


def test_spec_section_6_2_documents_seconds_duration_unit(spec_text: str) -> None:
    """AC-DOC-1: section 6.2's supported-duration list gains the seconds unit."""
    start = spec_text.index("## 6.2")
    end = spec_text.index("## 6.3")
    section_6_2 = spec_text[start:end]
    assert "45s" in section_6_2, (
        "spec.md section 6.2 should list a seconds-valued duration example"
    )


def test_readme_documents_limits_table(readme_text: str) -> None:
    """AC-DOC-2 (FS-0015 update): the two timeout keys are documented, now
    under their `ots.*` git config spelling rather than a `[limits]` TOML
    table."""
    assert "ots.otsTimeout" in readme_text, "README should name ots.otsTimeout"
    assert "ots.gitTimeout" in readme_text, "README should name ots.gitTimeout"


def test_readme_documents_limits_unbounded_zero_spelling(readme_text: str) -> None:
    """AC-DOC-2: the exact "0" spelling for unbounded."""
    assert '"0"' in readme_text, (
        'README should show the exact "0" spelling for unbounded'
    )
    assert "unbounded" in readme_text.lower(), (
        'README should state that "0" means unbounded'
    )


def test_readme_documents_limits_risk_statement(readme_text: str) -> None:
    """AC-DOC-2: a limit set too low turns a slow-but-healthy calendar into a
    recurring failure -- keyed on this distinctive phrase, not a common word."""
    assert (
        "turns a slow-but-healthy calendar into a recurring failure" in readme_text
    ), "README should state the risk of setting a limit too low"


def test_example_config_template_is_gone() -> None:
    """FS-0015: configuration lives only in Git config now, so the tracked
    TOML template is gone. A test explicitly fails if it is reintroduced,
    rather than merely omitting the old presence assertions (VQ-S7-004).

    This does not touch `git-ots.toml` itself: that file is untracked,
    gitignored, personal per-checkout state (possibly still present, inert,
    on some machine) -- not repository content this test suite owns an
    opinion about.
    """
    project_root = Path(__file__).resolve().parents[1]
    assert not (project_root / "git-ots.toml.example").exists(), (
        "git-ots.toml.example should have been deleted -- configuration is "
        "documented in README.md's Configuration section instead"
    )


def test_readme_documents_detached_terminal_and_credential_mitigations(
    readme_text: str,
) -> None:
    """AC-DOC-5: git/ots children run detached from the terminal, prompts no
    longer reach the operator, and the mitigations are named."""
    lower = readme_text.lower()
    assert "detached from the controlling terminal" in lower, (
        "README should state that git/ots children run detached from the terminal"
    )
    assert "no longer reach the operator" in lower, (
        "README should state that passphrase/credential prompts no longer reach the operator"
    )
    assert "ssh-agent" in readme_text, "README should name ssh-agent as a mitigation"
    assert "SSH_ASKPASS" in readme_text, "README should name SSH_ASKPASS"
    assert "GIT_ASKPASS" in readme_text, "README should name GIT_ASKPASS"


def test_readme_documents_git_timeout_scope_exception(readme_text: str) -> None:
    """AC-DOC-6: git_timeout does not bound reading binary objects out of
    commit trees; git show <commit>:<path> and git cat-file --batch stay
    bounded at the built-in 60-second default."""
    assert "git show <commit>:<path>" in readme_text, (
        "README should name git show <commit>:<path>"
    )
    assert "git cat-file --batch" in readme_text, (
        "README should name git cat-file --batch"
    )
    assert "built-in 60-second default" in readme_text, (
        "README should state that these reads stay bounded at the built-in 60-second default"
    )


def test_readme_documents_stale_index_lock_hazard(readme_text: str) -> None:
    """AC-DOC-7: a git timeout during an index-mutating command can leave a
    stale .git/index.lock, and the remedy to remove it. The safety caveat
    (confirm no other git process is running first) is asserted separately
    from the rm command itself: the rm command needs no protection, but the
    caveat is what the user personally ruled on, and a future tightening
    that dropped the caveat while keeping the rm would otherwise stay green."""
    assert ".git/index.lock" in readme_text, "README should name .git/index.lock"
    assert "rm .git/index.lock" in readme_text, (
        "README should give the command to remove the stale lock"
    )
    assert "confirmed no other `git` process is running" in readme_text, (
        "README should state the safety caveat: confirm no other git process "
        "is running before removing the lock"
    )


def test_readme_documents_git_timeout_exit_3_vs_6(readme_text: str) -> None:
    """AC-DOC-8: a git timeout can surface as exit 3 rather than exit 6 inside
    operations that report repository state."""
    lower = readme_text.lower()
    assert "does not always surface as exit 6" in lower, (
        "README should state that a git_timeout expiry does not always surface as exit 6"
    )
    assert "exit 3 instead" in lower, (
        "README should state that the relabelled failure maps to exit 3 instead"
    )


def test_readme_and_spec_document_exit_code_130(
    readme_text: str, spec_text: str
) -> None:
    """This epic's KeyboardInterrupt handling introduced exit code 130
    (SIGINT) but the published exit-code tables did not document it -- an
    operator alerting only on the documented codes would misclassify a clean
    Ctrl-C abort as an unexpected failure. The README check is keyed on the
    table-row spelling "| 130 |" rather than a bare "130" substring, because
    a hex merkle-root example elsewhere in the file coincidentally contains
    the digits "130"."""
    assert "| 130 |" in readme_text, "README's exit-code table should list 130"
    assert "sigint" in readme_text.lower(), "README should name SIGINT for exit 130"
    assert "130" in spec_text, "spec.md should document exit code 130"
    assert "sigint" in spec_text.lower(), "spec.md should name SIGINT for exit code 130"


def test_readme_bounds_what_a_generated_signature_adds(readme_text: str) -> None:
    """The setting is easy to over-read as making a timestamp harder to forge.

    It does not: a fabricated attestation fails verification with or without a
    signature. The README has to say what the signature is actually for, and
    that it never reaches the commits being timestamped -- otherwise the
    obvious reading is that git-ots has an opinion about how you sign your own
    work.
    """
    assert "git config ots.signing required" in readme_text, (
        "README should document the signing setting"
    )
    assert "git config ots.signing inherit" in readme_text, (
        "README should document that the default inherits ambient Git signing "
        "configuration rather than suppressing it"
    )
    lower = readme_text.lower()
    assert "who ran the tool" in lower, (
        "README should state that a generated signature authenticates who ran "
        "the tool, not what the attestation proves"
    )
    assert "never touches your own commits" in lower, (
        "README should state that the setting does not reach source commits"
    )
    assert "not itself timestamped" in lower, (
        "README should state that a tag signature is not anchored by the proof"
    )


REQUIRED_FILES = [
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "uv.lock",
    "src/git_ots/__init__.py",
    "src/git_ots/__main__.py",
    "src/git_ots/cli.py",
    "src/git_ots/config.py",
    "src/git_ots/git.py",
    "src/git_ots/policy.py",
    "src/git_ots/timestamp.py",
    "src/git_ots/orchestration.py",
    "src/git_ots/verify.py",
    "src/git_ots/upgrade.py",
    "src/git_ots/anchors.py",
    "src/git_ots/servers.py",
    "src/git_ots/proof_tree.py",
    "tests/test_config.py",
    "tests/test_policy.py",
    "tests/test_git.py",
    "tests/test_integration.py",
]


def test_required_files_exist() -> None:
    project_root = Path(__file__).resolve().parents[1]
    for relative_path in REQUIRED_FILES:
        full_path = project_root / relative_path
        assert full_path.exists(), f"Required file missing: {relative_path}"

    license_path = project_root / "LICENSE"
    assert license_path.stat().st_size > 0, "LICENSE must not be empty"


# ---------------------------------------------------------------------------
# FS-0015 S8: the normative spec and the request records describe git config,
# not a TOML file. AC-STRUCT-3 and AC-STRUCT-4 are deliberately phrased so a
# grep-shaped check discriminates a pre-fix checkout from a post-fix one.
# ---------------------------------------------------------------------------

#: The eleven sections AC-STRUCT-3 sweeps, by their heading text. Each once
#: carried a per-key ``toml`` fence illustrating an ``ots.*``-mapped setting.
_SWEPT_SECTIONS = (
    "## 2.1",
    "## 2.4",
    "## 6.1",
    "## 6.2",
    "## 6.3",
    "# 13.",
    "# 15.",
    "# 16.",
    "# 18.",
    "# 19.",
    "# 25.",
)


def _section_slice(spec_text: str, heading: str) -> str:
    """The text from ``heading`` up to the next heading at any level."""
    start = spec_text.index(heading)
    rest = spec_text[start + len(heading) :]
    offsets = [rest.index(m) for m in ("\n## ", "\n# ") if m in rest]
    return rest[: min(offsets)] if offsets else rest


@pytest.mark.parametrize("heading", _SWEPT_SECTIONS)
def test_spec_per_key_sections_carry_no_toml_fence(
    spec_text: str, heading: str
) -> None:
    """AC-STRUCT-3: no swept section illustrates an `ots.*` key with TOML."""
    assert "```toml" not in _section_slice(spec_text, heading), (
        f"spec.md {heading} still illustrates configuration with a TOML fence; "
        "FS-0015 removed the TOML format entirely"
    )


def test_spec_packaging_section_keeps_its_toml_fence(spec_text: str) -> None:
    """AC-STRUCT-3 explicitly exempts section 33: its TOML fence is the
    unrelated `pyproject.toml` manifest, not tool configuration.

    Asserted so the sweep is provably surgical rather than a blanket deletion
    of every fence in the document -- a check that only looked for the absence
    of TOML would pass just as well if section 33 had been damaged.
    """
    assert "```toml" in _section_slice(spec_text, "# 33."), (
        "section 33's pyproject.toml fence should be untouched by the sweep"
    )


def test_spec_layout_and_run_sections_drop_the_configuration_file(
    spec_text: str,
) -> None:
    """AC-STRUCT-3: section 4's layout trees and section 28.2's invocation
    example name neither the file nor the flag."""
    for heading in ("# 4.", "## 28.2"):
        section = _section_slice(spec_text, heading)
        assert "git-ots.toml" not in section, f"{heading} still lists git-ots.toml"
        assert "--config" not in section, f"{heading} still invokes --config"


def test_spec_configuration_section_documents_the_git_config_namespace(
    spec_text: str,
) -> None:
    """AC-STRUCT-3: section 5 is a rewrite carrying no TOML-format prose."""
    section = _section_slice(spec_text, "# 5.")
    assert "```toml" not in section
    assert "TOML" not in section, "section 5 should no longer mention TOML at all"
    assert "ots.maxAge" in section, "section 5 should document the ots.* namespace"


# ---------------------------------------------------------------------------
# ADR 0005 -- a submission is not complete until the repository records it.
# The defect these guard against was half a silence: `status` and `validate`
# both reported a repository as healthy while an anchored proof carried no tag
# and no invocation could produce one. Documentation that does not say what the
# tool now does about that leaves an operator with the same blind spot.
# ---------------------------------------------------------------------------


def test_spec_gates_tag_completion_on_evidence_not_on_a_trailer_claim(
    spec_text: str,
) -> None:
    """The precise defect, stated normatively so it cannot be reintroduced.

    Keying completion on a claiming generated proof commit is the tempting
    implementation -- it is what the first one did -- and it produces a
    permanently untagged proof whenever the artifacts reach history by any
    other route.
    """
    section = _section_slice(spec_text, "## 22.3")
    assert "not in terms of a" in section and "OpenTimestamps-Source" in section, (
        "section 22.3 must forbid keying completion on the trailer claim"
    )
    assert "binds to the canonical payload" in section, (
        "section 22.3 must state the binding check as the actual condition"
    )
    assert "MUST NOT substitute the recovery time" in section, (
        "section 22.3 must forbid manufacturing submitted_at"
    )


def test_spec_documents_in_flight_completion_before_new_submission(
    spec_text: str,
) -> None:
    """Completing before submitting is what stops a blocked run racing HEAD."""
    section = _section_slice(spec_text, "## 22.4")
    assert "SHALL NOT submit anything in the same invocation" in section, (
        "section 22.4 must forbid submitting alongside in-flight completion"
    )
    assert "SHALL NOT be treated as in flight" in section, (
        "section 22.4 must exclude a tagged source from in-flight completion"
    )
    assert "staged and unreferenced" in section, (
        "section 22.4 must require in-flight paths in the commit pathspec"
    )


def test_spec_documents_the_repair_command(spec_text: str) -> None:
    section = _section_slice(spec_text, "## 28.5.1")
    assert "git-ots repair" in section
    assert "SHALL NOT re-submit" in section or "SHALL NOT submit" in section
    assert "SHALL NOT move or overwrite an existing" in section


def test_spec_keeps_run_at_zero_by_default_and_makes_100_opt_in(
    spec_text: str,
) -> None:
    """The distinction is available on request and never imposed.

    Making "nothing was due" non-zero by default would turn the most common
    outcome of every scheduled run into a reported failure.
    """
    section = _section_slice(spec_text, "# 29.")
    assert "100" in section and "--exit-code" in section
    assert "SHALL remain the default" in section


def test_readme_documents_repair_and_untagged_reporting(readme_text: str) -> None:
    assert "git-ots repair" in readme_text, "README should document the repair command"
    assert "untagged" in readme_text, (
        "README should document validate's untagged reporting"
    )
    lowered = readme_text.lower()
    assert "never pushes tags" in lowered or "tags are never pushed" in lowered, (
        "README must explain why an untagged proof is not an error"
    )


def test_readme_explains_why_a_blocked_run_defers(readme_text: str) -> None:
    """A one-run delay reads as a bug unless the reason is written down."""
    assert "defer" in readme_text.lower(), "README should document deferral"
    assert "abandon" in readme_text.lower(), (
        "README should say what deferral protects against: an abandoned commitment"
    )


def test_readme_exit_code_table_lists_the_opt_in_idle_code(readme_text: str) -> None:
    assert "| 100 |" in readme_text, "README exit-code table should list 100"
    assert "--exit-code" in readme_text
