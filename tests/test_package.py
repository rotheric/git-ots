"""Package metadata and version contract."""

from importlib.metadata import version

import pytest

from git_ots.cli import main


def test_package_imports():
    import git_ots  # noqa: F401


def test_exposes_version():
    import git_ots

    assert hasattr(git_ots, "__version__")
    assert isinstance(git_ots.__version__, str)
    assert git_ots.__version__ == "0.0.1"
    assert version("git-ots") == git_ots.__version__


def test_cli_reports_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out == "0.0.1\n"
