"""Item 02: prove python -m git_ots delegates to cli.main and pyproject.toml declares the console script."""

import subprocess
import sys
from pathlib import Path


def test_python_m_git_ots_runs_without_error(tmp_path):
    """python -m git_ots should invoke the package (exits cleanly or errors via click, not ImportError)."""
    result = subprocess.run(
        [sys.executable, "-m", "git_ots"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    # We expect click to print usage/error (exit=1 or 2) but NOT ModuleNotFoundError
    assert "No module named" not in result.stderr


def test_console_script_metadata():
    """pyproject.toml must declare an [project.scripts] entry for git-ots."""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    import tomllib

    with open(pyproject, "rb") as f:
        cfg = tomllib.load(f)

    scripts = cfg.get("project", {}).get("scripts", {})
    assert "git-ots" in scripts, "pyproject.toml must define [project.scripts.git-ots]"
    assert scripts["git-ots"] == "git_ots.cli:main"
