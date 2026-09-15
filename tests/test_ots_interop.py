"""Integration test exercising the real OpenTimestamps CLI interoperability path.

This test is marked ``integration`` because it requires network access to
OpenTimestamps calendars. It installs a pinned ``opentimestamps-client`` in an
isolated temporary virtual environment, stamps the canonical Git payload,
and verifies the resulting detached proof with ``ots verify -f``. The default
unit test suite remains offline and deterministic.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from git_ots.timestamp import build_payload

pytestmark = pytest.mark.integration

_PINNED_OTS_VERSION = "opentimestamps-client==0.7.2"
_SAMPLE_COMMIT_ID = "0123456789abcdef0123456789abcdef01234567"


def _find_system_ots() -> str | None:
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(path_dir) / "ots"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


@pytest.fixture(scope="module")
def ots_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Return the path to a pinned ``ots`` executable.

    A pre-existing ``ots`` on ``PATH`` is used when available so CI jobs that
    pre-install the client are fast. Otherwise the pinned package is installed
    into an isolated temporary virtual environment.
    """
    existing = _find_system_ots()
    if existing is not None:
        return existing

    venv_path = tmp_path_factory.mktemp("ots-venv")
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_path)],
        check=True,
        capture_output=True,
    )
    pip = venv_path / "bin" / "pip"
    subprocess.run(
        [str(pip), "install", _PINNED_OTS_VERSION],
        check=True,
        capture_output=True,
    )
    ots = venv_path / "bin" / "ots"
    if not ots.is_file():
        pytest.fail("ots executable not found after installing pinned client")
    return str(ots)


def test_real_ots_stamps_and_verifies_canonical_payload(
    tmp_path: Path,
    ots_path: str,
) -> None:
    """A real ``ots stamp`` of the canonical payload can be verified with ``ots verify -f``."""
    payload = build_payload("sha1", _SAMPLE_COMMIT_ID)
    payload_file = tmp_path / "subject"
    payload_file.write_bytes(payload)

    stamp_result = subprocess.run(
        [ots_path, "stamp", str(payload_file)],
        capture_output=True,
        check=False,
    )
    assert stamp_result.returncode == 0, (
        f"ots stamp failed: {stamp_result.stderr.decode('utf-8', errors='replace')}"
    )

    proof_file = payload_file.with_suffix(payload_file.suffix + ".ots")
    assert proof_file.is_file(), "ots stamp did not produce a proof file"
    proof_bytes = proof_file.read_bytes()
    assert proof_bytes, "ots stamp produced an empty proof file"
    assert proof_bytes.startswith(b"\x00OpenTimestamps"), (
        "proof file does not start with the OpenTimestamps magic header"
    )

    verify_result = subprocess.run(
        [ots_path, "verify", "-f", str(payload_file), str(proof_file)],
        capture_output=True,
        check=False,
    )
    stderr = verify_result.stderr.decode("utf-8", errors="replace")

    if verify_result.returncode == 0:
        return

    # Fresh calendar attestations are not yet confirmed in the Bitcoin
    # blockchain. ``ots verify`` reports this condition rather than rejecting
    # the proof, which is sufficient to demonstrate CLI interoperability.
    assert "Pending confirmation" in stderr, f"ots verify failed unexpectedly: {stderr}"


def test_pinned_client_exposes_required_capabilities(ots_path: str) -> None:
    version = subprocess.run(
        [ots_path, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    help_text = subprocess.run(
        [ots_path, "--help"], capture_output=True, text=True, check=True
    ).stdout
    assert version == "v0.7.2"
    assert all(command in help_text for command in ("stamp", "upgrade", "verify"))
