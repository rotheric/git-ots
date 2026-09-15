import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = REPO_ROOT / "Makefile"
REQUIRED_TARGETS = [
    "help",
    "sync",
    "lint",
    "format",
    "format-check",
    "test",
    "test-integration",
    "check",
    "build",
    "install",
    "install-dev",
    "uninstall",
    "where",
    "clean",
    "prereqs",
]


@pytest.fixture
def makefile_text():
    assert MAKEFILE.is_file(), "Makefile must exist at the repository root"
    return MAKEFILE.read_text()


@pytest.mark.parametrize("target", REQUIRED_TARGETS)
def test_makefile_declares_required_targets(target, makefile_text):
    pattern = re.compile(rf"^{re.escape(target)}\s*:", re.MULTILINE)
    assert pattern.search(makefile_text), f"Makefile is missing target: {target}"


def test_makefile_phony_includes_all_required_targets(makefile_text):
    match = re.search(r"^\.PHONY:\s*(.*)$", makefile_text, re.MULTILINE)
    assert match, "Makefile must declare a .PHONY line"
    phony_targets = {t.strip() for t in match.group(1).split()}
    missing = [t for t in REQUIRED_TARGETS if t not in phony_targets]
    assert not missing, f".PHONY is missing targets: {missing}"


def test_makefile_install_uses_uv_tool_install(makefile_text):
    install_body = _target_body(makefile_text, "install")
    # Allow either a literal `uv` invocation or the $(UV) variable defined above.
    assert re.search(r"(\buv\b|\$\(UV\))\s+tool\s+install\b", install_body), (
        "install recipe must invoke 'uv tool install'"
    )


def test_makefile_install_dev_is_editable(makefile_text):
    install_dev_body = _target_body(makefile_text, "install-dev")
    assert "--editable" in install_dev_body, "install-dev recipe must pass --editable"


def test_makefile_has_no_disallowed_targets(makefile_text):
    disallowed = {"git push", "git commit", "ots submit", "ots upgrade"}
    lowered = makefile_text.lower()
    found = [d for d in disallowed if d in lowered]
    assert not found, f"Makefile contains disallowed operation references: {found}"


def _target_body(text, target):
    match = re.search(
        rf"^{re.escape(target)}\s*:.*?(?=\n\S|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"Could not extract body for target: {target}"
    return match.group(0)


def _run_prereqs(
    tmp_path: Path, *, ots_command: str | None
) -> subprocess.CompletedProcess:
    """Run `make prereqs` for real, against the actual Makefile, and return
    the completed process.

    AC-PREREQS-1 requires `make prereqs` to resolve the OpenTimestamps client
    via `git config --get ots.command` and to never reference `git-ots.toml`
    in its output. A text-only grep of the Makefile's recipe cannot catch a
    quoting or `$$`-escaping mistake in that recipe (VQ-S7-001) -- only
    actually running the target does, so this invokes `make` as a real
    subprocess and asserts on its observed stdout.

    The working directory is a fresh, non-repository `tmp_path` subdirectory
    -- not this checkout's own root -- so `git config --get` can only ever
    see the isolated global/system scope `conftest._isolated_git_config`
    already pinned into the inherited environment (never this checkout's own
    local `.git/config`, and never the real developer's `~/.gitconfig`). The
    `ots.command` value under test is layered on top via the
    `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0` command-scope
    override -- the same mechanism `conftest.py` documents as the narrowest
    Git config scope of all -- rather than writing a real config file.
    """
    workdir = tmp_path / "prereqs-cwd"
    workdir.mkdir()

    env = dict(os.environ)
    env.pop("GIT_CONFIG_COUNT", None)
    env.pop("GIT_CONFIG_KEY_0", None)
    env.pop("GIT_CONFIG_VALUE_0", None)
    if ots_command is not None:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "ots.command"
        env["GIT_CONFIG_VALUE_0"] = ots_command

    return subprocess.run(
        ["make", "-C", str(workdir), "-f", str(MAKEFILE), "prereqs"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_prereqs_reports_configured_ots_command(tmp_path: Path) -> None:
    """AC-PREREQS-1: with `ots.command` set, `make prereqs` reports that
    resolved command -- not a hardcoded default and not a file's existence."""
    result = _run_prereqs(tmp_path, ots_command="totally-distinct-ots-binary-xyz")
    assert "totally-distinct-ots-binary-xyz" in result.stdout, (
        f"expected the configured ots.command in output, got:\n{result.stdout}"
    )
    assert "git config" in result.stdout, (
        "output should attribute the value to git config"
    )
    assert "git-ots.toml" not in result.stdout, (
        f"output must not reference the removed git-ots.toml, got:\n{result.stdout}"
    )


def test_prereqs_falls_back_to_ots_when_unconfigured(tmp_path: Path) -> None:
    """AC-PREREQS-1: with no `ots.command` set anywhere, `make prereqs` falls
    back to the literal `ots` and still never references `git-ots.toml`."""
    result = _run_prereqs(tmp_path, ots_command=None)
    assert "none set -- built-in defaults apply" in result.stdout, (
        f"expected the unconfigured fallback message, got:\n{result.stdout}"
    )
    assert "  ots " in result.stdout, (
        f"expected the fallback client name 'ots' in output, got:\n{result.stdout}"
    )
    assert "git-ots.toml" not in result.stdout, (
        f"output must not reference the removed git-ots.toml, got:\n{result.stdout}"
    )
