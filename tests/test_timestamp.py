import hashlib
import os
import random
import shutil
import subprocess
import sys
import time
from dataclasses import FrozenInstanceError
from datetime import timedelta
from pathlib import Path

import pytest

from git_ots.timestamp import (
    OpenTimestampsCli,
    PayloadValidationError,
    ProofParseError,
    SubmissionError,
    SubmissionTimeoutError,
    SubprocessResult,
    TimestampProof,
    TimestampRequest,
    TimestampResult,
    VerificationAttempt,
    _default_runner,
    _kill_process_group,
    build_payload,
    classify_detached_proof,
    validate_detached_proof,
)


def _encode_varuint(n: int) -> bytes:
    """Encode an OpenTimestamps varuint (little-endian base 128).

    This is *not* Bitcoin's compact-size encoding, which the two agree with
    only below 0xFD. OpenTimestamps uses base-128 continuation bytes for both
    varuints and varbytes lengths; compact size appears only inside the
    serialized Bitcoin transactions a proof embeds.
    """
    if n < 0:
        raise ValueError("varuint must be non-negative")
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _make_fake_detached_proof(
    payload: bytes | None = None,
    *,
    file_hash_op_tag: int = 0x08,
    digest: bytes | None = None,
) -> bytes:
    """Build a structurally valid but cryptographically fake OTS proof.

    Fresh ``ots stamp`` output is a pending OpenTimestamps detached proof file.
    This helper produces the same shape: magic header, major version 1, a known
    hash operation tag, a digest of the matching length, and one pending
    attestation. It is used in place of a real calendar response so tests can
    exercise the adapter contract without network access.

    When ``payload`` is provided the embedded file digest is computed from it
    using the hash algorithm implied by ``file_hash_op_tag`` (``0x02`` for
    SHA-1, ``0x08`` for SHA-256), matching the real offline binding check. Pass
    ``digest`` explicitly only to simulate a mismatch or a structural-only
    proof.
    """
    header = b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"
    version = b"\x01"
    if file_hash_op_tag == 0x02:
        digest_length = 20
        hash_name = "sha1"
    elif file_hash_op_tag == 0x08:
        digest_length = 32
        hash_name = "sha256"
    else:
        raise ValueError(f"unsupported hash op tag: {file_hash_op_tag:#x}")
    if digest is None:
        if payload is None:
            digest = bytes(digest_length)
        else:
            digest = hashlib.new(hash_name, payload).digest()
    elif len(digest) != digest_length:
        raise ValueError("digest length does not match hash op")
    url = b"https://example.com"
    pending_magic = b"\x83\xdf\xe3\x0d\x2e\xf9\x0c\x8e"
    # An attestation is TAG || varbytes(payload); a pending attestation's
    # payload is itself varbytes(uri), so the URI carries its own length.
    uri_field = _encode_varuint(len(url)) + url
    attestation = pending_magic + _encode_varuint(len(uri_field)) + uri_field
    timestamp = b"\x00" + attestation
    return header + version + bytes([file_hash_op_tag]) + digest + timestamp


def _make_fake_ots_script(tmp_path, proof_bytes: bytes, exit_code: int = 0) -> str:
    """Create a disposable executable that models ``ots stamp <file>``.

    The script writes ``proof_bytes`` to ``<input-file>.ots`` and exits with
    ``exit_code``. Other subcommands exit non-zero so misuse is obvious.
    """
    script = tmp_path / "ots"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"if sys.argv[1] != 'stamp': sys.exit(2)\n"
        f"with open(sys.argv[2] + '.ots', 'wb') as f:\n"
        f"    f.write({proof_bytes!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script)


def test_build_payload_sha1_exact_bytes():
    commit_id = "0123456789abcdef0123456789abcdef01234567"
    assert build_payload("sha1", commit_id) == (
        b"git:sha1:0123456789abcdef0123456789abcdef01234567\n"
    )


def test_build_payload_sha256_exact_bytes():
    commit_id = "a" * 64
    expected = ("git:sha256:" + "a" * 64 + "\n").encode("ascii")
    assert build_payload("sha256", commit_id) == expected


def test_build_payload_returns_bytes_with_single_trailing_lf():
    commit_id = "0123456789abcdef0123456789abcdef01234567"
    payload = build_payload("sha1", commit_id)
    assert isinstance(payload, bytes)
    assert payload.endswith(b"\n")
    assert payload.count(b"\n") == 1


@pytest.mark.parametrize(
    "object_format", ["", "SHA1", "sha-1", "md5", "sha512", " sha1", "sha1 "]
)
def test_build_payload_rejects_unsupported_object_format(object_format: str):
    with pytest.raises(PayloadValidationError):
        build_payload(object_format, "0123456789abcdef0123456789abcdef01234567")


@pytest.mark.parametrize(
    "object_format,commit_id",
    [
        ("sha1", "a" * 39),
        ("sha1", "a" * 41),
        ("sha1", ""),
        ("sha256", "a" * 63),
        ("sha256", "a" * 65),
    ],
)
def test_build_payload_rejects_wrong_length_commit_id(
    object_format: str, commit_id: str
):
    with pytest.raises(PayloadValidationError):
        build_payload(object_format, commit_id)


@pytest.mark.parametrize(
    "object_format,commit_id",
    [
        ("sha1", "A" * 40),
        ("sha1", "0123456789ABCDEF0123456789abcdef01234567"),
        ("sha1", "g" * 40),
        ("sha1", "z" * 40),
        ("sha256", "A" * 64),
        ("sha256", "g" * 64),
    ],
)
def test_build_payload_rejects_non_lowercase_hex(object_format: str, commit_id: str):
    with pytest.raises(PayloadValidationError):
        build_payload(object_format, commit_id)


@pytest.mark.parametrize(
    "object_format,commit_id",
    [
        ("sha1", "0123456789abcdef0123456789abcdef01234 67"),
        ("sha1", "0123456789abcdef0123456789abcdef0123456\n"),
        ("sha1", "\t0123456789abcdef0123456789abcdef0123456"),
        ("sha1", "0123456789abcdef0123456789abcdef01234567 "),
        ("sha256", "a" * 63 + " "),
    ],
)
def test_build_payload_rejects_embedded_whitespace(object_format: str, commit_id: str):
    with pytest.raises(PayloadValidationError):
        build_payload(object_format, commit_id)


def test_timestamp_request_carries_frozen_payload():
    commit_id = "0123456789abcdef0123456789abcdef01234567"
    payload = build_payload("sha1", commit_id)
    request = TimestampRequest(
        object_format="sha1", commit_id=commit_id, payload=payload
    )
    assert request.payload == payload
    with pytest.raises(FrozenInstanceError):
        request.payload = b"other"


class _FakeClient:
    def __init__(self, proof_bytes: bytes) -> None:
        self._proof_bytes = proof_bytes
        self.seen_requests: list[TimestampRequest] = []

    def submit(self, request: TimestampRequest) -> TimestampResult:
        self.seen_requests.append(request)
        return TimestampResult(proof=TimestampProof(data=self._proof_bytes))


def test_fake_client_returns_proof_bytes_for_request():
    commit_id = "0123456789abcdef0123456789abcdef01234567"
    payload = build_payload("sha1", commit_id)
    request = TimestampRequest(
        object_format="sha1", commit_id=commit_id, payload=payload
    )
    proof_bytes = b"\x00fake-detached-proof\x01"
    client = _FakeClient(proof_bytes)

    result = client.submit(request)

    assert client.seen_requests == [request]
    assert client.seen_requests[0].payload == payload
    assert isinstance(result, TimestampResult)
    assert result.proof.data == proof_bytes


class _RecordingFileRunner:
    """Capture argv and simulate writing the ``.ots`` file next to the input."""

    def __init__(self, proof_bytes: bytes, exit_code: int = 0) -> None:
        self._proof_bytes = proof_bytes
        self._exit_code = exit_code
        self.calls: list[dict] = []

    def __call__(
        self,
        argv: list[str],
        *,
        stdin: bytes,
    ) -> SubprocessResult:
        self.calls.append({"argv": list(argv), "stdin": bytes(stdin)})
        if self._exit_code == 0:
            proof_path = Path(argv[2]).parent / (Path(argv[2]).name + ".ots")
            proof_path.write_bytes(self._proof_bytes)
        return SubprocessResult(
            exit_code=self._exit_code, stdout=b"", stderr=b"ots: simulated"
        )


def test_cli_adapter_passes_payload_as_temp_file_and_argv_is_list():
    """The payload is a temporary file argument; ``argv`` is always a list."""
    commit_id = "0123456789abcdef0123456789abcdef01234567"
    payload = build_payload("sha1", commit_id)
    request = TimestampRequest(
        object_format="sha1", commit_id=commit_id, payload=payload
    )
    proof_bytes = _make_fake_detached_proof(payload=payload)
    runner = _RecordingFileRunner(proof_bytes)
    client = OpenTimestampsCli(command="ots", runner=runner)

    result = client.submit(request)

    assert len(runner.calls) == 1
    argv = runner.calls[0]["argv"]
    assert isinstance(argv, list)
    assert all(isinstance(arg, str) for arg in argv)
    assert argv[0] == "ots"
    assert argv[1] == "stamp"
    assert Path(argv[2]).name == "payload"
    assert runner.calls[0]["stdin"] == b""
    assert result.proof.data == proof_bytes


def test_cli_adapter_uses_configured_command_as_argv_zero(tmp_path):
    commit_id = "0123456789abcdef0123456789abcdef01234567"
    payload = build_payload("sha1", commit_id)
    request = TimestampRequest(
        object_format="sha1", commit_id=commit_id, payload=payload
    )
    proof_bytes = _make_fake_detached_proof(payload=payload)
    command = _make_fake_ots_script(tmp_path, proof_bytes)
    client = OpenTimestampsCli(command=command)

    result = client.submit(request)

    assert result.proof.data == proof_bytes


def test_cli_adapter_invokes_ots_stamp_with_file_argument_and_reads_ots_file(
    tmp_path,
):
    """The supported ``ots`` interface stamps a file and writes ``<file>.ots``.

    The adapter must invoke ``ots stamp <payload-file>``, then read the
    resulting ``<payload-file>.ots`` and return its bytes, rather than passing
    the payload on stdin and reading stdout.
    """
    commit_id = "0123456789abcdef0123456789abcdef01234567"
    payload = build_payload("sha1", commit_id)
    request = TimestampRequest(
        object_format="sha1", commit_id=commit_id, payload=payload
    )
    proof_bytes = _make_fake_detached_proof(payload=payload)
    command = _make_fake_ots_script(tmp_path, proof_bytes)

    client = OpenTimestampsCli(command=command)
    result = client.submit(request)

    assert isinstance(result, TimestampResult)
    assert result.proof.data == proof_bytes


@pytest.mark.parametrize(
    "script_body,expected_substring",
    [
        ("sys.exit(3)", "3"),
        ("sys.exit(0)", "no proof file"),
    ],
)
def test_cli_adapter_failure_modes(tmp_path, script_body, expected_substring):
    """Non-zero exits and missing output files are mapped to submission errors."""
    commit_id = "0123456789abcdef0123456789abcdef01234567"
    payload = build_payload("sha1", commit_id)
    request = TimestampRequest(
        object_format="sha1", commit_id=commit_id, payload=payload
    )
    script = tmp_path / "ots"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if sys.argv[1] != 'stamp': sys.exit(2)\n"
        f"{script_body}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    client = OpenTimestampsCli(command=str(script))

    with pytest.raises(SubmissionError) as excinfo:
        client.submit(request)

    assert expected_substring in str(excinfo.value)


def test_cli_adapter_rejects_empty_proof_file(tmp_path):
    """A zero exit that produces an empty ``.ots`` file must fail."""
    commit_id = "0123456789abcdef0123456789abcdef01234567"
    payload = build_payload("sha1", commit_id)
    request = TimestampRequest(
        object_format="sha1", commit_id=commit_id, payload=payload
    )
    command = _make_fake_ots_script(tmp_path, b"")
    client = OpenTimestampsCli(command=command)

    with pytest.raises(SubmissionError) as excinfo:
        client.submit(request)

    assert "empty" in str(excinfo.value).lower()


def test_cli_adapter_rejects_structurally_invalid_proof_file(tmp_path):
    """A zero exit with a non-OTS file at the expected location must fail."""
    commit_id = "0123456789abcdef0123456789abcdef01234567"
    payload = build_payload("sha1", commit_id)
    request = TimestampRequest(
        object_format="sha1", commit_id=commit_id, payload=payload
    )
    command = _make_fake_ots_script(tmp_path, b"not a real ots proof")
    client = OpenTimestampsCli(command=command)

    with pytest.raises(SubmissionError):
        client.submit(request)


def test_validate_detached_proof_accepts_minimal_pending_proof():
    proof = _make_fake_detached_proof()
    validate_detached_proof(proof)  # should not raise


@pytest.mark.parametrize(
    "proof",
    [
        b"",
        b"not an ots proof",
        # Wrong major version.
        (
            b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"
            b"\x02\x08" + b"\x00" * 32 + b"\x00"
        ),
        # Truncated after digest.
        (
            b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"
            b"\x01\x08" + b"\x00" * 32
        ),
    ],
)
def test_validate_detached_proof_rejects_malformed_proofs(proof: bytes):
    with pytest.raises(ValueError):
        validate_detached_proof(proof)


def test_validate_detached_proof_accepts_matching_payload_binding():
    commit_id = "0123456789abcdef0123456789abcdef01234567"
    payload = build_payload("sha1", commit_id)
    proof = _make_fake_detached_proof(payload=payload)
    validate_detached_proof(proof, payload)  # should not raise


def test_validate_detached_proof_rejects_mismatched_payload_binding():
    """A structurally valid proof for a different payload must not pass."""
    commit_id = "0123456789abcdef0123456789abcdef01234567"
    payload = build_payload("sha1", commit_id)
    other_payload = build_payload("sha1", "abcdef0123456789abcdef0123456789abcdef01")
    proof = _make_fake_detached_proof(payload=other_payload)
    with pytest.raises(ValueError, match="payload"):
        validate_detached_proof(proof, payload)


def test_cli_adapter_rejects_proof_for_different_payload(tmp_path):
    """A zero exit with a structurally valid but unbound proof file must fail."""
    commit_id = "0123456789abcdef0123456789abcdef01234567"
    payload = build_payload("sha1", commit_id)
    other_payload = build_payload("sha1", "abcdef0123456789abcdef0123456789abcdef01")
    proof_bytes = _make_fake_detached_proof(payload=other_payload)
    command = _make_fake_ots_script(tmp_path, proof_bytes)
    client = OpenTimestampsCli(command=command)
    request = TimestampRequest(
        object_format="sha1", commit_id=commit_id, payload=payload
    )

    with pytest.raises(SubmissionError, match="payload"):
        client.submit(request)


def test_cli_adapter_verifies_temp_payload_and_proof_files() -> None:
    commit_id = "0123456789abcdef0123456789abcdef01234567"
    payload = build_payload("sha1", commit_id)
    proof = _make_fake_detached_proof(payload=payload)
    seen: list[list[str]] = []

    def runner(argv: list[str], *, stdin: bytes) -> SubprocessResult:
        seen.append(argv)
        assert stdin == b""
        assert argv[:3] == ["ots", "verify", "-f"]
        assert Path(argv[3]).read_bytes() == payload
        assert Path(argv[4]).read_bytes() == proof
        return SubprocessResult(
            exit_code=0,
            stdout=b"Success! Bitcoin block 800000 attests data\n",
            stderr=b"",
        )

    result = OpenTimestampsCli(command="ots", runner=runner).verify(proof, payload)

    assert result == VerificationAttempt(
        verified=True, detail="Success! Bitcoin block 800000 attests data"
    )
    assert len(seen) == 1


def test_cli_adapter_returns_nonzero_verification_as_per_proof_failure() -> None:
    commit_id = "0123456789abcdef0123456789abcdef01234567"
    payload = build_payload("sha1", commit_id)
    proof = _make_fake_detached_proof(payload=payload)

    def runner(argv: list[str], *, stdin: bytes) -> SubprocessResult:
        return SubprocessResult(
            exit_code=1,
            stdout=b"",
            stderr=b"Could not connect to Bitcoin node\n",
        )

    result = OpenTimestampsCli(command="ots", runner=runner).verify(proof, payload)

    assert result == VerificationAttempt(
        verified=False, detail="Could not connect to Bitcoin node"
    )


@pytest.mark.skipif(
    shutil.which("ots") is None,
    reason="real ``ots`` executable is not available",
)
def test_real_ots_produces_structurally_valid_proof(tmp_path):
    """Opt-in smoke test against the installed OpenTimestamps client.

    This test contacts public calendar servers, so it is skipped unless a real
    ``ots`` command is on ``PATH``. It exercises the actual adapter path: a
    temporary payload file is stamped, a ``.ots`` file appears next to it, and
    the returned proof bytes pass structural validation.
    """
    commit_id = "0123456789abcdef0123456789abcdef01234567"
    payload = build_payload("sha1", commit_id)
    request = TimestampRequest(
        object_format="sha1", commit_id=commit_id, payload=payload
    )
    client = OpenTimestampsCli(command="ots")

    result = client.submit(request)

    assert result.proof.data
    validate_detached_proof(result.proof.data)


def test_submit_reports_a_missing_client_as_a_submission_failure() -> None:
    """A missing `ots` binary is a submission failure, not an unexpected error.

    subprocess raises FileNotFoundError when the command does not exist, which
    escaped as `Unexpected error: [Errno 2] No such file or directory: 'ots'`
    and exit 1, contradicting the documented exit code 4 and telling the
    operator nothing about the remedy.
    """
    client = OpenTimestampsCli(command="git-ots-no-such-client")
    request = TimestampRequest(
        object_format="sha1",
        commit_id="0" * 40,
        payload=build_payload("sha1", "0" * 40),
    )

    with pytest.raises(SubmissionError) as excinfo:
        client.submit(request)

    message = str(excinfo.value)
    assert "git-ots-no-such-client" in message
    assert "not found" in message


class _SleepingRunner:
    """A ``SubprocessRunner`` double that blocks past any configured ceiling.

    Stands in for an ``ots`` calendar that accepts a connection and never
    responds -- there is no way to provoke that against a real binary
    deterministically and quickly. It genuinely sleeps rather than
    pre-emptively raising, so it only lets a test pass if
    ``OpenTimestampsCli`` itself detects and acts on the elapsed time.
    """

    def __init__(self, sleep_seconds: float) -> None:
        self._sleep_seconds = sleep_seconds
        self.calls = 0

    def __call__(self, argv: list[str], *, stdin: bytes) -> SubprocessResult:
        self.calls += 1
        time.sleep(self._sleep_seconds)
        return SubprocessResult(exit_code=0, stdout=b"", stderr=b"")


def _submit_request() -> TimestampRequest:
    commit_id = "0123456789abcdef0123456789abcdef01234567"
    payload = build_payload("sha1", commit_id)
    return TimestampRequest(object_format="sha1", commit_id=commit_id, payload=payload)


def test_submit_raises_submission_timeout_error_when_runner_blocks_past_limit() -> None:
    """AC-ERR-1: a runner that hangs past ``limits.ots_timeout`` is a SubmissionError."""
    runner = _SleepingRunner(sleep_seconds=2.0)
    client = OpenTimestampsCli(
        command="ots", runner=runner, limit=timedelta(milliseconds=50)
    )

    started = time.monotonic()
    with pytest.raises(SubmissionError) as excinfo:
        client.submit(_submit_request())
    elapsed = time.monotonic() - started

    assert isinstance(excinfo.value, SubmissionTimeoutError)
    # submit() must give up at the configured limit, not wait for the
    # (still-sleeping) runner to eventually return.
    assert elapsed < 1.0
    assert runner.calls == 1


def test_upgrade_raises_submission_timeout_error_when_runner_blocks_past_limit() -> (
    None
):
    """AC-ERR-5 (adapter half): the same ceiling applies to ``upgrade``."""
    runner = _SleepingRunner(sleep_seconds=2.0)
    client = OpenTimestampsCli(
        command="ots", runner=runner, limit=timedelta(milliseconds=50)
    )

    started = time.monotonic()
    with pytest.raises(SubmissionError) as excinfo:
        client.upgrade(_make_fake_detached_proof(), payload=b"git:sha1:" + b"0" * 40)
    elapsed = time.monotonic() - started

    assert isinstance(excinfo.value, SubmissionTimeoutError)
    assert elapsed < 1.0
    assert runner.calls == 1


def test_submission_timeout_error_names_command_limit_and_no_proof() -> None:
    """AC-ERR-3: the message names the command and limit, not a fake exit code."""
    runner = _SleepingRunner(sleep_seconds=2.0)
    client = OpenTimestampsCli(
        command="my-ots-command", runner=runner, limit=timedelta(milliseconds=50)
    )

    with pytest.raises(SubmissionTimeoutError) as excinfo:
        client.submit(_submit_request())

    message = str(excinfo.value)
    assert "my-ots-command" in message
    assert "0.05" in message  # the configured limit, in seconds
    assert "no proof" in message.lower()
    assert "exited with code" not in message.lower()


def test_submission_timeout_error_still_carries_base_class_attributes() -> None:
    """Existing ``except SubmissionError`` callers reading .exit_code/.stderr keep working."""
    runner = _SleepingRunner(sleep_seconds=2.0)
    client = OpenTimestampsCli(
        command="ots", runner=runner, limit=timedelta(milliseconds=50)
    )

    with pytest.raises(SubmissionTimeoutError) as excinfo:
        client.submit(_submit_request())

    assert isinstance(excinfo.value.exit_code, int)
    assert isinstance(excinfo.value.stderr, bytes)


def test_submit_with_no_limit_never_times_out_for_a_runner_that_returns() -> None:
    """No configured limit is a direct passthrough -- unaffected by the ceiling wrapper."""
    proof_bytes = _make_fake_detached_proof(payload=_submit_request().payload)
    runner = _RecordingFileRunner(proof_bytes)
    client = OpenTimestampsCli(command="ots", runner=runner, limit=None)

    result = client.submit(_submit_request())

    assert result.proof.data == proof_bytes
    assert len(runner.calls) == 1


def test_submit_reports_a_missing_client_as_a_submission_failure_with_a_limit_set() -> (
    None
):
    """A missing client must stay a plain SubmissionError, not a false timeout.

    With a limit configured, ``_invoke`` runs the runner call on a
    background thread; a fast-failing OSError from the production runner
    (the command not existing) must still surface promptly as the ordinary
    missing-client error, not get masked as a SubmissionTimeoutError by the
    ceiling wrapper.
    """
    client = OpenTimestampsCli(
        command="git-ots-no-such-client", limit=timedelta(seconds=5)
    )

    with pytest.raises(SubmissionError) as excinfo:
        client.submit(_submit_request())

    assert not isinstance(excinfo.value, SubmissionTimeoutError)
    assert "not found" in str(excinfo.value)


def test_default_runner_spawns_with_start_new_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """VQ-S2-002/006: the production runner must start a new session/process group.

    Without ``start_new_session=True``, a later ``os.killpg`` on expiry would
    signal the caller's own process group (the pytest harness itself) rather
    than the child's -- see the pairing this asserts.
    """
    captured_kwargs: dict = {}
    request = _submit_request()
    script = _make_fake_ots_script(
        tmp_path, _make_fake_detached_proof(payload=request.payload)
    )

    import subprocess as _subprocess

    real_popen = _subprocess.Popen

    class _RecordingPopen(real_popen):
        def __init__(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("git_ots.timestamp.subprocess.Popen", _RecordingPopen)
    client = OpenTimestampsCli(command=script)

    client.submit(request)

    assert captured_kwargs.get("start_new_session") is True


def test_terminate_active_child_kills_a_live_child_process_group(
    tmp_path: Path,
) -> None:
    """The interrupt-cleanup seam actually kills a running child's process group.

    Not itself an AC of this story (that's AC-PROC-3, owned by S4's
    KeyboardInterrupt wiring), but ``cli.py`` will call this method to react
    to Ctrl-C without importing ``subprocess``/``os`` itself, so it must work
    on its own before S4 consumes it.
    """
    marker = tmp_path / "child_pid"
    script = tmp_path / "ots"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys, time\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(600)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    client = OpenTimestampsCli(command=str(script))

    outcome: list[SubmissionError] = []

    def _run_submit() -> None:
        try:
            client.submit(_submit_request())
        except SubmissionError as exc:
            outcome.append(exc)

    import threading as _threading

    worker = _threading.Thread(target=_run_submit, daemon=True)
    worker.start()
    deadline = time.monotonic() + 5.0
    while not marker.exists():
        if time.monotonic() > deadline:
            raise AssertionError("child never started")
        time.sleep(0.05)
    child_pid = int(marker.read_text())

    client.terminate_active_child()
    worker.join(timeout=5.0)

    # There is no configured ceiling on this path (limit=None): the kill
    # above is purely the external terminate_active_child() call, not a
    # timeout, so submit() sees an ordinary killed-child exit code and
    # raises the base SubmissionError -- not SubmissionTimeoutError, which
    # only fires on the ceiling path. Pinning this shape (rather than
    # swallowing whatever submit() raises with a bare `except BaseException`)
    # means an unexpected failure inside submit() would fail this assertion
    # instead of passing silently as long as the child happens to die.
    assert len(outcome) == 1, "submit() did not raise SubmissionError as expected"
    assert not isinstance(outcome[0], SubmissionTimeoutError)
    assert "exited with code -9" in str(outcome[0])

    def _child_alive() -> bool:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return False
        return True

    deadline = time.monotonic() + 5.0
    while _child_alive():
        if time.monotonic() > deadline:
            raise AssertionError("child was not terminated within 5 seconds")
        time.sleep(0.05)


def test_default_runner_raises_submission_timeout_error_on_its_own_deadline(
    tmp_path: Path,
) -> None:
    """Regression: the inner layer's own timeout must raise, not return a result.

    ``_default_runner`` and ``OpenTimestampsCli._invoke`` race the same
    deadline; whichever one notices expiry first must be the one to signal
    it. ``_invoke``'s outer deadline usually wins in practice (its own
    thread-scheduling overhead is smaller than the production runner's
    process-spawn-then-wait path), which is why the earlier defect --
    ``_default_runner`` catching ``TimeoutExpired``, killing the child, and
    then *returning* a ``SubprocessResult`` with ``exit_code=-SIGKILL``
    instead of raising -- passed unnoticed by tests that only exercise the
    combined ``OpenTimestampsCli.submit`` path: ``submit()`` would see that
    non-zero exit code and raise a plain ``SubmissionError`` reporting
    "opentimestamps exited with code -9", the exact false claim AC-ERR-3
    forbids. Calling ``_default_runner`` directly here bypasses the outer
    layer entirely, so this asserts the inner layer's own behaviour with no
    race involved -- it is not exercising a scenario that depends on
    winning a scheduling race, unlike the multi-trial integration test in
    tests/test_integration.py, whose natural inner-wins rate is small and
    ceiling-dependent enough that it cannot be relied on alone to catch a
    regression here.
    """
    fake_ots = tmp_path / "fake_ots"
    fake_ots.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(600)\n",
        encoding="utf-8",
    )
    fake_ots.chmod(0o755)

    with pytest.raises(SubmissionTimeoutError) as excinfo:
        _default_runner([str(fake_ots)], stdin=b"", limit=timedelta(milliseconds=100))

    assert str(fake_ots) in str(excinfo.value)


def test_kill_process_group_kills_survivors_after_the_leader_is_reaped(
    tmp_path: Path,
) -> None:
    """Regression: killing by a remembered pgid must work even once the
    group leader is already gone -- the nastiest shape of the orphaned-
    grandchild scenario AC-PROC-1 protects against, and exactly the case
    ``os.getpgid(pid)`` loses: once the leader is reaped,
    ``os.getpgid(leader_pid)`` simply has no process left to resolve and
    raises ``ProcessLookupError`` (not a pid-reuse race -- pids stay
    reserved until the parent calls ``wait``, so that risk is not real
    here), even though the process *group* itself (identified by the
    now-unresolvable pgid) can still have live members.

    Unlike the other AC-PROC-1 tests, this does not depend on winning any
    timing race: the leader here exits and is reaped deterministically
    fast, well before ``_kill_process_group`` is ever called, so this
    exercises the leader-already-gone case on every run rather than only
    when a race happens to land that way.
    """
    marker = tmp_path / "grandchild_pid"
    leader_script = tmp_path / "leader"
    leader_script.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, subprocess, sys\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "grandchild = subprocess.Popen(\n"
        f"    [{sys.executable!r}, '-c', 'import time; time.sleep(600)']\n"
        ")\n"
        "marker.write_text(str(grandchild.pid))\n"
        # Exit immediately -- the grandchild stays alive in the same
        # process group, since it never called its own start_new_session.
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    leader_script.chmod(0o755)

    process = subprocess.Popen(
        [str(leader_script)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Remembered at spawn, exactly as _default_runner does -- pgid == pid
    # because of start_new_session=True.
    pgid = process.pid
    process.wait(timeout=5.0)  # the leader exits fast and is fully reaped here

    deadline = time.monotonic() + 5.0
    while not marker.exists():
        if time.monotonic() > deadline:
            raise AssertionError("grandchild marker never appeared")
        time.sleep(0.05)
    grandchild_pid = int(marker.read_text())

    def _grandchild_alive() -> bool:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            return False
        return True

    assert _grandchild_alive(), "test setup: grandchild should still be running"

    _kill_process_group(pgid)

    deadline = time.monotonic() + 5.0
    while _grandchild_alive():
        if time.monotonic() > deadline:
            raise AssertionError(
                "grandchild survived the group kill -- killing by the "
                "remembered pgid must not depend on the leader still "
                "being resolvable via os.getpgid()"
            )
        time.sleep(0.05)


def test_default_runner_timeout_survives_a_failing_reap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: an OSError from the post-kill reap must not displace the
    timeout it was reporting. Simulates the reap itself failing by making
    ``Popen.wait()`` raise unconditionally; ``SubmissionTimeoutError`` must
    still be what propagates. If the reap's ``OSError`` were allowed to
    leak instead, it would land in ``submit()``'s unrelated
    ``except OSError`` handler and misreport a timeout as "client not
    found" -- the same family of false diagnosis AC-ERR-3 exists to
    prevent, just via a different code path.
    """
    fake_ots = tmp_path / "fake_ots"
    fake_ots.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(600)\n",
        encoding="utf-8",
    )
    fake_ots.chmod(0o755)

    def _failing_wait(self, *args, **kwargs):
        raise OSError("simulated reap failure")

    monkeypatch.setattr("git_ots.timestamp.subprocess.Popen.wait", _failing_wait)

    with pytest.raises(SubmissionTimeoutError):
        _default_runner([str(fake_ots)], stdin=b"", limit=timedelta(milliseconds=100))


# ---------------------------------------------------------------------------
# Detached-proof parser robustness
#
# ``classify_detached_proof`` is the one place where git-ots reads bytes it
# did not write: proof files come back from the OpenTimestamps client and are
# re-read from the repository on every ``verify`` and ``upgrade``. A mutation
# run over timestamp.py showed the parser's bounds checks and its recursion
# limit to be undiscriminated -- every ``>`` could become ``>=``, the
# attestation slice could be taken from the wrong end, and the depth counter
# could count *down*, with the suite still green. The tests below assert the
# two things a parser of untrusted input owes its caller, without naming any
# offset or branch inside it:
#
#   1. Whatever the bytes are, the answer is a classification or the parser's
#      own error -- never an IndexError, a RecursionError, or a hang.
#   2. The recursion limit is a real limit, at a stated depth.
#
# No AC covers proof-parser robustness; filed as a spec-gap finding.
# ---------------------------------------------------------------------------

_CLASSIFICATIONS = {"valid", "pending-attestation"}


def _classification_or_parse_error(data: bytes) -> str:
    """Return the classification, or the ProofParseError message.

    Any *other* exception escapes, which is the whole point: the contract is
    that a caller handling ProofParseError has handled every malformed input.
    """
    try:
        result = classify_detached_proof(data)
    except ProofParseError as exc:
        return f"rejected: {exc}"
    assert result in _CLASSIFICATIONS, f"unexpected classification {result!r}"
    return result


def _nested_proof(operations: int) -> bytes:
    """A structurally valid proof whose timestamp tree is ``operations`` deep.

    Each level is one unary hash operation whose child timestamp follows, so
    the tree recurses once per operation before reaching the single pending
    attestation at the bottom.
    """
    header = b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"
    url = b"https://example.com"
    uri_field = _encode_varuint(len(url)) + url
    attestation = (
        b"\x00"
        + b"\x83\xdf\xe3\x0d\x2e\xf9\x0c\x8e"
        + _encode_varuint(len(uri_field))
        + uri_field
    )
    return header + b"\x01" + b"\x08" + bytes(32) + b"\x08" * operations + attestation


def test_every_truncation_of_a_proof_is_rejected_rather_than_crashing():
    """A proof file that stops early -- an interrupted write, a truncated
    download, a corrupted checkout -- must be reported as an unreadable proof.
    Every prefix is tried because the interesting failures are exactly at the
    boundaries between the header, the digest, the tree and each attestation.
    """
    proof = _make_fake_detached_proof(payload=b"payload\n")
    for length in range(len(proof)):
        outcome = _classification_or_parse_error(proof[:length])
        assert outcome.startswith("rejected: "), (
            f"truncation to {length} bytes was accepted as {outcome!r}"
        )


def test_no_byte_sequence_makes_the_parser_fail_in_an_unexpected_way():
    """The parser reads bytes produced by another program and stored in a
    repository other people can write to. Whatever it is handed, the caller
    must only ever have to handle ProofParseError -- an IndexError or a
    RecursionError escaping to the CLI is an "Unexpected error" traceback in
    front of an operator who cannot act on it.

    The corpus is deterministic: every single-byte corruption of a real proof
    (where the structural boundaries are), plus pseudo-random and pathological
    byte strings.
    """
    proof = _make_fake_detached_proof(payload=b"payload\n")
    header_length = len(
        b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"
    )

    corpus = [b"", proof]
    for index in range(header_length, len(proof)):
        for replacement in (0x00, 0x01, 0x08, 0x7F, 0x80, 0xFF):
            corpus.append(proof[:index] + bytes([replacement]) + proof[index + 1 :])
    # Truncated and over-long trees, and a run of fork markers with nothing
    # behind them.
    corpus.append(proof + b"\xff")
    corpus.append(proof[:header_length] + b"\x01\x08" + bytes(32) + b"\xff" * 64)
    rng = random.Random(20260821)
    for _ in range(200):
        size = rng.randrange(0, 96)
        corpus.append(
            proof[:header_length] + bytes(rng.randrange(256) for _ in range(size))
        )

    for data in corpus:
        _classification_or_parse_error(data)


def test_a_deeply_nested_timestamp_tree_is_refused_at_a_stated_depth():
    """A proof can nest its timestamp tree arbitrarily; a hand-crafted one can
    nest it far enough to exhaust the interpreter stack. The parser must turn
    that into a normal rejection with a reason, and it must do so only past
    the documented limit -- a limit that also refuses ordinary proofs would be
    a denial of service against legitimate ones.
    """
    accepted = _classification_or_parse_error(_nested_proof(256))
    assert accepted == "pending-attestation"

    refused = _classification_or_parse_error(_nested_proof(257))
    assert refused == "rejected: timestamp recursion limit exceeded"

    deep = _classification_or_parse_error(_nested_proof(5000))
    assert deep == "rejected: timestamp recursion limit exceeded"
