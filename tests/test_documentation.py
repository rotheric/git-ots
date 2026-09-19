"""Documentation checks for files, links, and technical identifiers."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

README = Path(__file__).resolve().parents[1] / "README.md"
SPEC = Path(__file__).resolve().parents[1] / "specs" / "spec.md"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DETAIL_DOCUMENTS = (
    "docs/installation.md",
    "docs/configuration.md",
    "docs/github-actions.md",
    "docs/proof-lifecycle.md",
    "docs/operations.md",
    "docs/what-a-proof-proves.md",
)


@pytest.mark.parametrize(
    "relative_path",
    [
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
def readme_only_text() -> str:
    if not README.exists():
        pytest.fail(f"README file not found: {README}")
    return README.read_text(encoding="utf-8")


@pytest.fixture
def readme_text(readme_only_text: str) -> str:
    """Complete operator documentation, rooted at the concise README.

    Historical test names retain ``readme`` because the material originally
    lived in one file. Assertions now cover the linked detail documents too.
    """
    details = "\n".join(
        (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        for relative_path in DETAIL_DOCUMENTS
    )
    return f"{readme_only_text}\n{details}"


@pytest.fixture
def spec_text() -> str:
    if not SPEC.exists():
        pytest.fail(f"spec file not found: {SPEC}")
    return SPEC.read_text(encoding="utf-8")


@pytest.mark.parametrize("relative_path", DETAIL_DOCUMENTS)
def test_detail_documents_are_present_and_nonempty(relative_path: str) -> None:
    text = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
    assert text.startswith("# ")
    assert len(text.splitlines()) >= 20


def test_local_markdown_links_resolve() -> None:
    documents = [README, *(PROJECT_ROOT / path for path in DETAIL_DOCUMENTS)]
    for document in documents:
        text = document.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
            path_text = target.split("#", 1)[0]
            if not path_text or "://" in path_text or path_text.startswith("mailto:"):
                continue
            target_path = (document.parent / path_text).resolve()
            assert target_path.exists(), f"broken link in {document}: {target}"


def test_readme_documents_installation_and_run_commands(readme_text: str) -> None:
    text = readme_text.lower()
    assert "uv" in text, "README should mention uv installation"
    assert "uv sync" in text or "uv run" in text, "README should include uv commands"
    assert "git ots run" in text, "README should document the run command"
    assert "git ots status" in text or "--dry-run" in text, (
        "README should document status or dry-run"
    )


def test_documentation_prefers_git_subcommand_spelling(
    readme_text: str, spec_text: str
) -> None:
    documentation = f"{readme_text}\n{spec_text}"
    assert "git ots run" in documentation
    direct_invocation = re.compile(
        r"\bgit-ots (?:--debug|--version|run|status|upgrade|validate|verify|repair|config)\b"
    )
    assert not direct_invocation.search(documentation)


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


def test_documentation_names_every_mapped_config_key(readme_text: str) -> None:
    """Check configuration coverage without prescribing prose or table layout."""
    from git_ots.gitconfig import MAPPING

    documented_keys = set(re.findall(r"\bots\.[a-z]+\b", readme_text.lower()))
    missing = [row.key for row in MAPPING if row.key not in documented_keys]
    assert not missing, f"Documentation is missing configuration keys: {missing}"


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


def test_readme_documents_limits_table(readme_text: str) -> None:
    """AC-DOC-2 (FS-0015 update): the two timeout keys are documented, now
    under their `ots.*` git config spelling rather than a `[limits]` TOML
    table."""
    assert "ots.otsTimeout" in readme_text, "README should name ots.otsTimeout"
    assert "ots.gitTimeout" in readme_text, "README should name ots.gitTimeout"


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
