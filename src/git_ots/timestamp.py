"""Canonical timestamp payload construction and timestamp-client boundary.

Implements the ``git:<object-format>:<full-commit-id>\n`` canonical form
described in specs/spec.md section 9, and the small protocol/result model
described in specs/spec.md section 10.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

_HEX_DIGITS = frozenset("0123456789abcdef")
_FORMAT_LENGTHS: dict[str, int] = {"sha1": 40, "sha256": 64}

# OpenTimestamps file hash operation tags mapped to the hashlib algorithm name
# and expected digest length. ``ots stamp`` uses SHA-256 (tag 0x08) by default.
_HASH_OP_ALGORITHMS: dict[int, tuple[str, int]] = {
    0x02: ("sha1", 20),
    0x08: ("sha256", 32),
}

# Triggers the policy layer can record. A recovery manifest naming anything
# outside this set is treated as malformed.
_KNOWN_TRIGGERS: frozenset[str] = frozenset({"every_commit", "max_age", "fixed_time"})

_PAYLOAD_FORMAT_V1 = "git-commit-id-v1"
_MANIFEST_SCHEMA_V1 = 1

# OpenTimestamps attestation tags. The Bitcoin block header attestation is the
# only one that makes a proof fully verifiable without contacting a calendar.
# PendingAttestation is a calendar promise; other blockchain attestations are
# recognized but treated as not-yet-Bitcoin-verified for this tool's purposes.
_BITCOIN_BLOCK_HEADER_ATTESTATION_TAG = bytes.fromhex("0588960d73d71901")
_PENDING_ATTESTATION_TAG = bytes.fromhex("83dfe30d2ef90c8e")

# Unary cryptographic/encoding operations. These consume no argument bytes.
_UNARY_OP_TAGS: frozenset[int] = frozenset({0x02, 0x03, 0x08, 0x67, 0xF2, 0xF3})

# Binary operations. These are followed by a varbytes argument.
_BINARY_OP_TAGS: frozenset[int] = frozenset({0xF0, 0xF1})


class PayloadValidationError(ValueError):
    """Raised when a payload input is not a valid canonical commit reference."""


class SubmissionError(RuntimeError):
    """Raised when the OpenTimestamps CLI exits non-zero.

    Carries the process exit code and captured stderr so callers can surface
    an actionable diagnostic without trusting process output as a proof.
    """

    def __init__(self, exit_code: int, stderr: bytes) -> None:
        self.exit_code = exit_code
        self.stderr = stderr
        detail = stderr.decode("utf-8", errors="replace").strip()
        message = f"opentimestamps exited with code {exit_code}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class SubmissionTimeoutError(SubmissionError):
    """Raised when the OpenTimestamps CLI does not finish within its ceiling.

    Unlike :class:`UpgradeError`, this cannot simply narrow the parent's
    meaning: ``SubmissionError.__init__`` hardcodes a message asserting the
    process *exited* with a code, which is false here -- the child was
    killed, not exited. ``__init__`` is overridden to synthesize a message
    naming the invoked command and the configured limit and stating that no
    proof was written, while still populating ``exit_code``/``stderr`` so
    code that catches the base ``SubmissionError`` keeps working. The child
    is killed (its whole process group, not just the direct process) before
    this is raised.
    """

    def __init__(self, *, command: str, limit: timedelta) -> None:
        self.command = command
        self.limit = limit
        # There is no real exit code -- the child was killed -- but a killed
        # process's conventional Python ``returncode`` is the negated signal
        # number, so this stays a meaningful (and still-``int``) value for
        # any caller reading the base class's attribute.
        self.exit_code = -signal.SIGKILL
        self.stderr = b""
        seconds = limit.total_seconds()
        message = (
            f"{command!r} did not finish within the configured limit of "
            f"{seconds:g}s and was killed; no proof was written"
        )
        RuntimeError.__init__(self, message)


class UpgradeError(SubmissionError):
    """Raised when upgrading an existing proof fails in a way an operator must fix.

    A proof for which no calendar has anything new yet is deliberately *not*
    an error: the client reports "not complete" and leaves the file untouched,
    which the adapter reports as "not upgraded". This exception is reserved
    for a missing client, an unreadable result, or an upgraded proof that no
    longer binds to its source commit.
    """


class VerificationError(SubmissionError):
    """Raised when the OpenTimestamps verifier cannot be invoked."""


class PersistenceError(RuntimeError):
    """Raised when a proof cannot be persisted atomically or safely."""


class RecoveryValidationError(RuntimeError):
    """Raised when on-disk recovery artifacts do not form a valid pair.

    Used by the read-only inspection that backs spec section 22.1 ("proof
    exists, tag absent"). Any mismatch — missing files, malformed JSON,
    wrong commit, wrong object format, zero-byte proof — must be treated
    as a failure rather than silently resubmitting or guessing.
    """


def build_manifest(
    *,
    object_format: str,
    commit_id: str,
    proof_name: str,
    source_ref: str,
    submitted_at: datetime,
    triggers,
) -> dict:
    """Build the deterministic schema-1 manifest value for a proof.

    Returns a plain dict with stable key insertion order matching the
    documented schema in specs/spec.md section 11.1. ``submitted_at`` must
    be timezone-aware and is normalized to UTC and serialized as RFC 3339
    with a ``Z`` suffix. ``triggers`` are sorted lexicographically so the
    serialized form is independent of the caller's iteration order.

    The same payload validator used by :func:`build_payload` constrains
    ``object_format`` and ``commit_id`` so a malformed identifier can never
    leak into a manifest.
    """
    build_payload(object_format, commit_id)
    if (
        submitted_at.tzinfo is None
        or submitted_at.tzinfo.utcoffset(submitted_at) is None
    ):
        raise ValueError("submitted_at must be a timezone-aware datetime")
    submitted_utc = submitted_at.astimezone(UTC)
    submitted_str = submitted_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema": _MANIFEST_SCHEMA_V1,
        "source_commit": commit_id,
        "git_object_format": object_format,
        "payload_format": _PAYLOAD_FORMAT_V1,
        "submitted_at": submitted_str,
        "proof": proof_name,
        "source_ref": source_ref,
        "triggers": sorted(triggers),
    }


def validate_detached_proof(data: bytes, payload: bytes | None = None) -> None:
    """Validate that ``data`` is a structurally sound OpenTimestamps proof.

    Checks the magic header, major version, supported hash operation tag,
    matching digest length, and a non-empty timestamp beginning with a valid
    token. This is intentionally not a full cryptographic verification: it
    rejects obvious garbage without trusting a zero exit code, but does not
    require the Bitcoin blockchain or calendar servers.

    When ``payload`` is provided, the embedded file digest is also checked
    against the hash of ``payload`` using the algorithm indicated by the file
    hash operation tag. This establishes offline that the proof actually
    derives from the exact canonical payload that was submitted.
    """
    header_magic = (
        b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"
    )
    # A timestamp may start with an attestation (0x00), a fork marker (0xff),
    # or a known operation tag.
    valid_timestamp_starts = frozenset(
        {0x00, 0xFF, 0x02, 0x03, 0x08, 0x67, 0xF0, 0xF1, 0xF2, 0xF3}
    )

    if not data.startswith(header_magic):
        raise ValueError("not an OpenTimestamps detached proof")

    version_pos = len(header_magic)
    if len(data) <= version_pos:
        raise ValueError("truncated OpenTimestamps proof")
    if data[version_pos] != 1:
        raise ValueError("unsupported OpenTimestamps proof version")

    op_pos = version_pos + 1
    if len(data) <= op_pos:
        raise ValueError("truncated OpenTimestamps proof")
    op_tag = data[op_pos]
    hash_info = _HASH_OP_ALGORITHMS.get(op_tag)
    if hash_info is None:
        raise ValueError(f"unsupported hash operation in proof: {op_tag:#x}")
    hash_name, digest_length = hash_info

    digest_end = op_pos + 1 + digest_length
    timestamp_pos = digest_end
    if len(data) <= timestamp_pos:
        raise ValueError("truncated OpenTimestamps proof digest or timestamp")
    if data[timestamp_pos] not in valid_timestamp_starts:
        raise ValueError("invalid OpenTimestamps timestamp structure")

    if payload is not None:
        embedded_digest = data[op_pos + 1 : digest_end]
        expected_digest = hashlib.new(hash_name, payload).digest()
        if embedded_digest != expected_digest:
            raise ValueError(
                "OpenTimestamps proof digest does not match the canonical payload"
            )


class ProofParseError(ValueError):
    """Raised when a recursive OpenTimestamps proof parse fails."""


def _read_varuint(data: bytes, offset: int) -> tuple[int, int]:
    """Read a little-endian base-128 varuint from ``data`` at ``offset``.

    Returns the decoded value and the offset immediately after it.
    """
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ProofParseError("truncated varuint")
        byte = data[offset]
        value |= (byte & 0x7F) << shift
        offset += 1
        if not (byte & 0x80):
            break
        shift += 7
    return value, offset


def _skip_attestation(data: bytes, offset: int) -> int:
    """Skip an OpenTimestamps attestation and return the next offset.

    An attestation is an 8-byte tag followed by a varbytes payload.
    """
    if offset + 8 > len(data):
        raise ProofParseError("truncated attestation tag")
    offset += 8
    payload_length, offset = _read_varuint(data, offset)
    offset += payload_length
    if offset > len(data):
        raise ProofParseError("truncated attestation payload")
    return offset


def _skip_operation(data: bytes, offset: int) -> int:
    """Skip a known OpenTimestamps operation and return the next offset.

    Unary operations consist of a single tag byte. Binary operations are
    followed by a varbytes argument. Unknown tags raise ``ProofParseError``.
    """
    if offset >= len(data):
        raise ProofParseError("truncated operation")
    tag = data[offset]
    offset += 1
    if tag in _UNARY_OP_TAGS:
        return offset
    if tag in _BINARY_OP_TAGS:
        arg_length, offset = _read_varuint(data, offset)
        offset += arg_length
        if offset > len(data):
            raise ProofParseError("truncated operation argument")
        return offset
    raise ProofParseError(f"unknown operation tag: {tag:#x}")


def _parse_timestamp_tree(
    data: bytes,
    offset: int,
    *,
    _depth: int = 0,
    _max_depth: int = 256,
) -> tuple[set[bytes], int]:
    """Recursively parse an OpenTimestamps timestamp tree.

    Returns the set of 8-byte attestation tags found and the offset after the
    tree. ``0xff`` is a fork marker; ``0x00`` introduces an attestation; any
    other byte begins an operation whose child timestamp follows.
    """
    if _depth > _max_depth:
        raise ProofParseError("timestamp recursion limit exceeded")

    seen: set[bytes] = set()
    while offset < len(data) and data[offset] == 0xFF:
        offset += 1
        if offset >= len(data):
            raise ProofParseError("truncated fork marker")
        tag = data[offset]
        offset += 1
        if tag == 0x00:
            attestation_end = offset + 8
            if attestation_end > len(data):
                raise ProofParseError("truncated attestation tag")
            seen.add(data[offset:attestation_end])
            offset = _skip_attestation(data, offset)
        else:
            offset = _skip_operation(data, offset - 1)
            child_tags, offset = _parse_timestamp_tree(
                data, offset, _depth=_depth + 1, _max_depth=_max_depth
            )
            seen.update(child_tags)

    if offset >= len(data):
        raise ProofParseError("truncated timestamp")
    tag = data[offset]
    offset += 1
    if tag == 0x00:
        attestation_end = offset + 8
        if attestation_end > len(data):
            raise ProofParseError("truncated attestation tag")
        seen.add(data[offset:attestation_end])
        offset = _skip_attestation(data, offset)
    else:
        offset = _skip_operation(data, offset - 1)
        child_tags, offset = _parse_timestamp_tree(
            data, offset, _depth=_depth + 1, _max_depth=_max_depth
        )
        seen.update(child_tags)

    return seen, offset


def classify_detached_proof(data: bytes) -> str:
    """Classify a structurally valid OpenTimestamps proof by attestation type.

    Performs a full recursive parse of the timestamp tree and inspects the
    attestation tags. Returns ``"valid"`` when at least one Bitcoin block
    header attestation is present. Returns ``"pending-attestation"`` when the
    proof is structurally sound but contains only non-Bitcoin attestations
    (typically remote-calendar pending attestations).

    This is intentionally not a cryptographic verification: it does not contact
    the Bitcoin network or verify block headers. It distinguishes the two
    offline-stable states the ``validate`` command reports.

    Raises ``ProofParseError`` when the proof is malformed or contains no
    attestations.
    """
    header_magic = (
        b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"
    )
    if not data.startswith(header_magic):
        raise ProofParseError("not an OpenTimestamps detached proof")

    version_pos = len(header_magic)
    if len(data) <= version_pos:
        raise ProofParseError("truncated OpenTimestamps proof")
    if data[version_pos] != 1:
        raise ProofParseError("unsupported OpenTimestamps proof version")

    op_pos = version_pos + 1
    if op_pos >= len(data):
        raise ProofParseError("truncated OpenTimestamps proof")
    op_tag = data[op_pos]
    hash_info = _HASH_OP_ALGORITHMS.get(op_tag)
    if hash_info is None:
        raise ProofParseError(f"unsupported hash operation in proof: {op_tag:#x}")
    _hash_name, digest_length = hash_info

    timestamp_pos = op_pos + 1 + digest_length
    if timestamp_pos >= len(data):
        raise ProofParseError("truncated OpenTimestamps proof digest or timestamp")

    tags, final_offset = _parse_timestamp_tree(data, timestamp_pos)
    if final_offset != len(data):
        raise ProofParseError("trailing bytes after timestamp tree")
    if not tags:
        raise ProofParseError("timestamp tree contains no attestations")

    if _BITCOIN_BLOCK_HEADER_ATTESTATION_TAG in tags:
        return "valid"
    return "pending-attestation"


def build_payload(object_format: str, commit_id: str) -> bytes:
    """Return the LF-terminated canonical payload bytes.

    Raises :class:`PayloadValidationError` if ``object_format`` is not a
    supported Git object format or ``commit_id`` is not a full-length
    lowercase hexadecimal identifier of the matching length.
    """
    if object_format not in _FORMAT_LENGTHS:
        raise PayloadValidationError(f"unsupported object format: {object_format!r}")
    expected_length = _FORMAT_LENGTHS[object_format]
    if len(commit_id) != expected_length:
        raise PayloadValidationError(
            f"commit id must be {expected_length} characters for "
            f"{object_format}, got {len(commit_id)}"
        )
    if any(ch not in _HEX_DIGITS for ch in commit_id):
        raise PayloadValidationError(
            f"commit id must be lowercase hexadecimal: {commit_id!r}"
        )
    return f"git:{object_format}:{commit_id}\n".encode("ascii")


@dataclass(frozen=True, slots=True)
class TimestampRequest:
    """A single OpenTimestamps submission request.

    ``payload`` MUST be the canonical bytes produced by :func:`build_payload`.
    """

    object_format: str
    commit_id: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class TimestampProof:
    """Detached OpenTimestamps proof bytes returned by a submission."""

    data: bytes


@dataclass(frozen=True, slots=True)
class TimestampResult:
    """Outcome of a successful timestamp submission."""

    proof: TimestampProof


@dataclass(frozen=True, slots=True)
class UpgradeAttempt:
    """Outcome of one upgrade invocation against a single stored proof.

    ``upgraded`` is ``None`` when the client had nothing new to add. ``detail``
    carries the client's own diagnostic so a network failure -- which also
    leaves the proof untouched -- can be told apart from a calendar that is
    simply not ready yet.
    """

    upgraded: bytes | None
    detail: str


@dataclass(frozen=True, slots=True)
class VerificationAttempt:
    """Outcome of checking one proof against Bitcoin through ``ots verify``."""

    verified: bool
    detail: str


class TimestampClient(Protocol):
    """Boundary between policy/git code and the OpenTimestamps adapter."""

    def submit(self, request: TimestampRequest) -> TimestampResult:
        """Submit ``request`` and return the detached proof."""
        ...


@dataclass(frozen=True, slots=True)
class SubprocessResult:
    """Captured outcome of an OpenTimestamps CLI invocation."""

    exit_code: int
    stdout: bytes
    stderr: bytes


# A runner receives the full argument array (never a shell string) plus the
# payload bytes on stdin. There is deliberately no ``shell`` parameter; the
# subprocess is always invoked with an argument array so shell mode cannot
# be requested. See specs/spec.md section 35.
SubprocessRunner = Callable[..., SubprocessResult]


def _kill_process_group(pgid: int) -> None:
    """Terminate the whole process group identified by ``pgid``.

    ``git.py`` carries its own copy of this exact function, duplicated
    rather than imported (Boundary Rule 2 confines process-lifetime code to
    each adapter module independently -- see that module's docstring and
    architecture.json for the full reasoning). If this function's kill
    strategy ever changes, check whether ``git.py``'s twin needs the same
    change; nothing enforces the two staying in lockstep besides this note.

    ``pgid`` must be the pgid of a process that was started with
    ``start_new_session=True``, which makes it the leader of its own new
    process group -- so its pgid equals its pid at the moment it is spawned.
    Callers pass that *remembered* value directly; this function does not
    call ``os.getpgid(pid)`` to re-derive it at kill time. That lookup
    depends on the leader still being alive: once it has been reaped (e.g.
    by ``communicate()``/``wait()`` completing), ``os.getpgid(leader_pid)``
    raises ``ProcessLookupError`` -- not because the pid was reused (pids
    are reserved until the parent calls ``wait``, and even a zombie still
    answers ``getpgid``; a same-pid collision is not a realistic risk here)
    but simply because there is no such process left to ask. The group
    itself, identified by the pgid, can still have live members -- any
    children the reaped leader spawned before exiting -- and the lookup
    failing means this function would silently kill nothing, leaving
    exactly the surviving grandchildren AC-PROC-1 exists to reap. Killing
    by the remembered pgid instead of the leader's pid sidesteps that
    lookup entirely, so it still reaches those survivors even when the
    leader is already gone. Killing the group rather than just the leader
    reaches any children the process spawned -- a plain ``process.kill()``
    reaps only the direct child and would leave those orphaned and running.
    Silently ignored if the whole group is already gone. A repeated
    best-effort kill can also report ``EPERM`` on macOS after a concurrent
    cleanup path has already killed the child; that race is treated as
    completed cleanup so it cannot replace the operation's real exception.
    """
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _default_runner(
    argv: list[str],
    *,
    stdin: bytes,
    limit: timedelta | None = None,
    on_spawn: Callable[[int], None] | None = None,
    on_exit: Callable[[], None] | None = None,
) -> SubprocessResult:
    """Spawn ``argv`` and enforce ``limit`` as a wall-clock ceiling.

    The child is started in its own session (``start_new_session=True``) so
    that, on expiry, its whole process group can be killed rather than only
    the direct child -- see :func:`_kill_process_group`. On expiry this
    function kills the group, reaps the child, and *raises*
    :class:`SubmissionTimeoutError` -- it does not return a
    ``SubprocessResult`` describing a killed process as an ordinary exit.
    Returning one here was a real defect: ``submit``/``upgrade`` treat any
    non-zero ``exit_code`` as a plain ``SubmissionError``, so a killed child
    (``exit_code`` conventionally ``-SIGKILL``) would have produced the
    message "opentimestamps exited with code -9" -- exactly the false claim
    AC-ERR-3 forbids. Raising here instead means this function owns both the
    real process *and* the real kill *and* the real timeout signal: whichever
    of this function's own deadline or :meth:`OpenTimestampsCli._invoke`'s
    outer one fires first, the caller ends up with the same
    ``SubmissionTimeoutError`` either way, so that race is benign now rather
    than being able to leak the base class's message through one of its two
    sides.

    ``on_spawn``/``on_exit`` let :class:`OpenTimestampsCli` track the
    currently-live child's pgid so both an interactive interrupt and
    ``_invoke``'s own timeout path can also reach it directly (see
    ``OpenTimestampsCli.terminate_active_child``, called synchronously by
    ``_invoke`` on its own expiry -- a backstop for an injected runner that
    never returns, not this function's primary kill path); production
    callers always pass both, tests calling this function directly may omit
    them.

    The outer ``except BaseException: _kill_process_group(pgid); raise`` is
    load-bearing on its own, not merely a backstop for the ``on_spawn``/
    ``on_exit`` registration above. When ``limit is None`` (an operator's
    ``ots_timeout = "0"``), :meth:`OpenTimestampsCli._invoke` is a direct
    passthrough -- this function then runs inline on the caller's own thread,
    so a ``KeyboardInterrupt`` unwinds *through* ``process.communicate()``
    itself rather than being confined to a background worker thread the way
    it is in the bounded case. That unwind reaches this function's own
    ``finally: on_exit()`` before it reaches any caller's ``except
    KeyboardInterrupt`` -- clearing ``OpenTimestampsCli``'s child-pgid
    registration while the child is still alive, which makes any caller's
    later ``terminate_active_child()`` call a silent no-op and orphans the
    child. Killing the process group here, in the one frame that always has
    ``pgid`` in scope regardless of which thread is unwinding, closes that
    window without depending on any registration surviving the unwind.
    Redundant-but-harmless on the timeout path above (already killed) and
    when ``on_spawn``/``on_exit``'s own cleanup wins the race; the only case
    where it is the sole kill is this one.
    """
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    # start_new_session=True makes this process the leader of its own new
    # process group, so its pgid equals its pid at this exact moment --
    # remember it now rather than re-deriving it via os.getpgid(pid) later,
    # once the leader may already have been reaped. See _kill_process_group.
    pgid = process.pid
    try:
        if on_spawn is not None:
            on_spawn(pgid)
        seconds = limit.total_seconds() if limit is not None else None
        try:
            stdout, stderr = process.communicate(input=stdin, timeout=seconds)
        except subprocess.TimeoutExpired:
            _kill_process_group(pgid)
            # `limit` is never None here: communicate() only raises
            # TimeoutExpired when it was given a numeric `timeout`, which
            # only happens when `limit is not None`.
            timeout_error = SubmissionTimeoutError(command=argv[0], limit=limit)
            try:
                # Reap with wait(), not another communicate(): the child is
                # already dead (or dying) from the kill above, and nothing
                # downstream reads its output on this path, so there is no
                # reason to drain its stdout/stderr pipes to EOF. That
                # distinction matters -- if a grandchild escaped the
                # process group via its own start_new_session and still
                # holds the write end of those pipes open, communicate()
                # would block indefinitely waiting for EOF that never
                # comes, even though the direct child is already gone.
                # wait() only waits for the direct child's own exit status
                # and cannot be blocked by that. Any OSError here is
                # swallowed rather than allowed to propagate and displace
                # the timeout below -- an already-built exception is raised
                # unconditionally so a failure during best-effort cleanup
                # can never masquerade as something else (e.g. a missing
                # client, if it propagated as OSError into submit()'s
                # unrelated `except OSError` handler).
                process.wait()
            except OSError:
                pass
            raise timeout_error from None
        return SubprocessResult(
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    except BaseException:
        _kill_process_group(pgid)
        raise
    finally:
        if on_exit is not None:
            on_exit()


class OpenTimestampsCli:
    """Adapter that invokes the OpenTimestamps CLI as a subprocess.

    The runner is injectable so tests can supply a fake; the production
    default is :func:`_default_runner`, which spawns with an argument array
    (``shell=False``) and enforces ``limit`` as a wall-clock ceiling.

    **Invariant this class guarantees:** ``submit``/``upgrade``/``verify``
    never block longer than ``limit``, *whatever runner is injected*. The ``runner``
    parameter is a permanently public, permanently injectable seam (module
    boundary rule 3), not an implementation detail of the production path --
    so the ceiling is this adapter's responsibility, not any one runner
    implementation's. That is why the ceiling is enforced in two places,
    both raising the same :class:`SubmissionTimeoutError` on expiry so it
    never matters which one "wins":

    - :func:`_default_runner` owns the real child process. On its own
      deadline it kills the process group and raises, rather than returning
      a ``SubprocessResult`` describing a killed child as an ordinary exit
      (that used to happen, and leaked the base class's "exited with code"
      message for a child that never exited -- see its docstring). For the
      production runner specifically, this is what actually terminates the
      real OS process; nothing else in this class can.
    - :meth:`_invoke` is the layer that makes the invariant above true for
      *any* runner, not just the production one. No production call site
      passes ``runner=`` today (``orchestration.py`` and ``upgrade.py``
      always construct with the default), which is true but is not a
      reason to remove this layer: the seam is public API by design, and an
      arbitrary injected callable -- the ``sleeping runner`` test double is
      one example, a future caller supplying their own is another -- has no
      timeout awareness of its own and nothing else in this class could
      bound it. ``submit``/``upgrade`` route their runner call through
      ``_invoke``, which runs it on a background thread and gives up once
      ``limit`` elapses regardless of what kind of runner is plugged in;
      the cost is one thread per bounded invocation. On its own expiry it
      also calls :meth:`terminate_active_child` itself, synchronously,
      rather than trusting the (possibly abandoned) worker thread to finish
      the kill on its own -- see :meth:`_invoke`'s docstring for why that
      matters even though ``_default_runner`` normally gets there first.
    """

    def __init__(
        self,
        command: str,
        *,
        runner: SubprocessRunner | None = None,
        limit: timedelta | None = None,
    ) -> None:
        if not command:
            raise PayloadValidationError("opentimestamps command must not be empty")
        self._command = command
        self._limit = limit
        # Deliberately not lock-protected -- see terminate_active_child()'s
        # docstring for why a lock here would be actively dangerous rather
        # than merely unnecessary.
        self._active_child_pgid: int | None = None
        if runner is not None:
            self._runner = runner
        else:
            self._runner = functools.partial(
                _default_runner,
                limit=limit,
                on_spawn=self._set_active_child,
                on_exit=self._clear_active_child,
            )

    def _set_active_child(self, pgid: int) -> None:
        self._active_child_pgid = pgid

    def _clear_active_child(self) -> None:
        self._active_child_pgid = None

    def terminate_active_child(self) -> None:
        """Kill the process group of whichever child is currently in flight.

        A no-op if no child is currently running -- including when the
        production runner was never used (an injected test runner), and
        when ``submit``/``upgrade`` have already returned or raised: the
        registration this method reads is cleared by ``on_exit`` in
        :func:`_default_runner`'s ``finally`` block the moment the call
        completes, by any path. A caller that invokes this from *outside*
        the in-flight call -- e.g. from cleanup code reached only after
        ``submit``/``upgrade`` has already unwound -- gets a silent no-op:
        no exception, no killed process, and no signal that anything was
        skipped. To actually reach a live child, this must be called while
        the ``submit``/``upgrade`` call is still on the stack, e.g. from the
        ``except`` block wrapping it, not from an outer handler that runs
        after it has already returned.

        Exposed so callers such as ``cli.py`` can react to an interactive
        interrupt without importing ``subprocess``/``os`` themselves --
        ``subprocess`` stays confined to this module and ``git.py``
        (spec.md section 32).

        ``self._active_child_pgid`` is deliberately a plain attribute, not
        lock-protected: a single ``int | None`` assignment/read is already
        atomic against other Python threads in CPython, so this method is
        safe to call from either an ``except KeyboardInterrupt:`` block or
        a ``signal.signal(signal.SIGINT, handler)`` callback. A lock here
        previously made only the first of those two safe -- the second
        could deadlock, which matters because ``_default_runner`` runs
        inline on the main thread whenever no ``limit`` is configured
        (including the documented ``ots_timeout = "0"`` escape hatch), so
        the unsafe case was reachable, not hypothetical.
        """
        pgid = self._active_child_pgid
        if pgid is not None:
            _kill_process_group(pgid)

    def _invoke(self, argv: list[str], *, stdin: bytes) -> SubprocessResult:
        """Call ``self._runner``, giving up once ``self._limit`` elapses.

        With no configured limit this is a direct passthrough -- existing
        callers that never set ``limit`` see no behavioural change. With a
        limit, the runner call is made on a background daemon thread; if it
        has not finished by the deadline, this thread (the caller's) kills
        whatever child the production runner has registered as active via
        :meth:`terminate_active_child`, *then* raises
        :class:`SubmissionTimeoutError`.

        The kill must happen here, synchronously, rather than being left to
        the abandoned worker thread's own matching deadline inside
        :func:`_default_runner`: both deadlines are armed with the same
        ``self._limit`` and start within microseconds of each other, so
        which one fires first is a scheduling coin flip. If the worker's own
        kill loses that race, this thread has already raised and the caller
        is typically already unwinding to process exit -- and an abandoned
        daemon thread does not get scheduled again after the interpreter
        starts shutting down, so the kill would silently never happen,
        orphaning the child's process group. Calling
        ``terminate_active_child`` here removes that race: whichever side
        fires the kill, it happens before this method returns control to the
        caller. (This is a no-op for an injected test runner that never
        registered a child, and merely redundant -- not incorrect -- on the
        production path if the worker's own kill already won.)
        """
        if self._limit is None:
            return self._runner(argv, stdin=stdin)

        outcome: list[SubprocessResult] = []
        failure: list[BaseException] = []

        def _target() -> None:
            try:
                outcome.append(self._runner(argv, stdin=stdin))
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                failure.append(exc)

        worker = threading.Thread(target=_target, daemon=True)
        worker.start()
        worker.join(self._limit.total_seconds())
        if worker.is_alive():
            self.terminate_active_child()
            raise SubmissionTimeoutError(command=self._command, limit=self._limit)
        if failure:
            raise failure[0]
        return outcome[0]

    def probe(self) -> tuple[str, bool]:
        """Return the client version and whether its required CLI is present."""
        try:
            version_result = self._invoke([self._command, "--version"], stdin=b"")
            help_result = self._invoke([self._command, "--help"], stdin=b"")
        except OSError as exc:
            raise SubmissionError(127, str(exc).encode()) from exc
        if version_result.exit_code != 0:
            raise SubmissionError(version_result.exit_code, version_result.stderr)
        if help_result.exit_code != 0:
            raise SubmissionError(help_result.exit_code, help_result.stderr)
        version = version_result.stdout.decode(errors="replace").strip()
        help_text = (help_result.stdout + help_result.stderr).decode(errors="replace")
        required = ("stamp", "upgrade", "verify")
        compatible = all(command in help_text for command in required)
        if not compatible:
            missing = ", ".join(
                command for command in required if command not in help_text
            )
            raise SubmissionError(
                1, f"client lacks required command(s): {missing}".encode()
            )
        return version, version == "v0.7.2"

    def submit(self, request: TimestampRequest) -> TimestampResult:
        # The supported ``ots`` CLI interface stamps a file and writes the
        # detached proof to ``<file>.ots`` in the same directory. We avoid
        # stdin/stdout assumptions by writing the payload to a temporary file,
        # invoking ``ots stamp <path>``, and reading the resulting ``.ots`` file.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / "payload"
            tmp_path.write_bytes(request.payload)
            argv = [self._command, "stamp", str(tmp_path)]
            try:
                result = self._invoke(argv, stdin=b"")
            except OSError as exc:
                # The command could not be executed at all -- typically absent
                # from PATH. That is a submission failure with a documented exit
                # code, not an unexpected internal error.
                raise SubmissionError(
                    exit_code=127,
                    stderr=(
                        f"OpenTimestamps client {self._command!r} not found or "
                        f"not executable ({exc.strerror}). Install a client, for "
                        f"example: uv tool install opentimestamps-client"
                    ).encode(),
                ) from exc
            if result.exit_code != 0:
                raise SubmissionError(exit_code=result.exit_code, stderr=result.stderr)
            proof_path = tmp_path.parent / (tmp_path.name + ".ots")
            if not proof_path.exists():
                raise SubmissionError(
                    exit_code=result.exit_code,
                    stderr=result.stderr or b"opentimestamps produced no proof file",
                )
            proof_bytes = proof_path.read_bytes()
            if not proof_bytes:
                raise SubmissionError(
                    exit_code=result.exit_code,
                    stderr=result.stderr
                    or b"opentimestamps produced an empty proof file",
                )
            try:
                validate_detached_proof(proof_bytes, request.payload)
            except ValueError as exc:
                raise SubmissionError(
                    exit_code=result.exit_code,
                    stderr=f"opentimestamps produced an invalid proof file: {exc}".encode(),
                ) from None
            return TimestampResult(proof=TimestampProof(data=proof_bytes))

    def upgrade(self, proof_bytes: bytes, payload: bytes) -> UpgradeAttempt:
        """Attempt to complete ``proof_bytes`` from the calendars it names.

        Upgrading happens on a copy in a temporary directory. ``ots upgrade``
        rewrites its target in place and leaves a ``.bak`` file beside it;
        doing that inside the repository would litter the proof directory and
        bypass the atomic-write boundary every other proof mutation uses.

        The client's exit code is deliberately not treated as the outcome: it
        reports failure both for "no calendar can complete this yet" -- the
        ordinary pending case -- and for real errors. The resulting file
        content is the reliable signal, so the exit status is captured into
        ``detail`` for the operator instead of raising.
        """
        if not proof_bytes:
            raise UpgradeError(exit_code=1, stderr=b"cannot upgrade an empty proof")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / "proof.ots"
            tmp_path.write_bytes(proof_bytes)
            argv = [self._command, "upgrade", str(tmp_path)]
            try:
                result = self._invoke(argv, stdin=b"")
            except OSError as exc:
                raise UpgradeError(
                    exit_code=127,
                    stderr=(
                        f"OpenTimestamps client {self._command!r} not found or "
                        f"not executable ({exc.strerror}). Install a client, for "
                        f"example: uv tool install opentimestamps-client"
                    ).encode(),
                ) from exc
            try:
                upgraded = tmp_path.read_bytes()
            except OSError as exc:
                raise UpgradeError(
                    exit_code=result.exit_code,
                    stderr=f"cannot read back upgraded proof: {exc}".encode(),
                ) from exc

            detail = _last_diagnostic_line(result.stderr) or _last_diagnostic_line(
                result.stdout
            )
            if upgraded == proof_bytes:
                return UpgradeAttempt(upgraded=None, detail=detail)

            # An upgrade only ever appends attestations to an existing tree, so
            # the result must still commit to the same payload. Anything else
            # means the client rewrote the proof against a different input and
            # must never reach the repository.
            try:
                validate_detached_proof(upgraded, payload)
            except ValueError as exc:
                raise UpgradeError(
                    exit_code=result.exit_code,
                    stderr=(
                        f"upgraded proof no longer binds to its source commit: {exc}"
                    ).encode(),
                ) from None
            return UpgradeAttempt(upgraded=upgraded, detail=detail)

    def verify(self, proof_bytes: bytes, payload: bytes) -> VerificationAttempt:
        """Verify ``proof_bytes`` against Bitcoin through the configured client.

        Both inputs are written to a temporary directory because the supported
        OpenTimestamps interface is ``ots verify -f <payload> <proof>``. The
        repository's proof is never passed as a writable target and is never
        modified. A non-zero client exit is a per-proof negative outcome whose
        diagnostic is returned to the caller; inability to execute the client
        at all is an operational :class:`VerificationError`.
        """
        if not proof_bytes:
            raise VerificationError(exit_code=1, stderr=b"cannot verify an empty proof")
        try:
            validate_detached_proof(proof_bytes, payload)
        except ValueError as exc:
            raise VerificationError(
                exit_code=1,
                stderr=f"cannot verify a proof not bound to its payload: {exc}".encode(),
            ) from None

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            payload_path = tmp_root / "payload"
            proof_path = tmp_root / "proof.ots"
            payload_path.write_bytes(payload)
            proof_path.write_bytes(proof_bytes)
            argv = [
                self._command,
                "verify",
                "-f",
                str(payload_path),
                str(proof_path),
            ]
            try:
                result = self._invoke(argv, stdin=b"")
            except OSError as exc:
                raise VerificationError(
                    exit_code=127,
                    stderr=(
                        f"OpenTimestamps client {self._command!r} not found or "
                        f"not executable ({exc.strerror}). Install a client, for "
                        f"example: uv tool install opentimestamps-client"
                    ).encode(),
                ) from exc

        detail = _last_diagnostic_line(result.stderr) or _last_diagnostic_line(
            result.stdout
        )
        return VerificationAttempt(verified=result.exit_code == 0, detail=detail)


def _last_diagnostic_line(stream: bytes) -> str:
    """Return the last non-empty line of captured client output, or ``""``."""
    text = stream.decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def persist_proof(
    *,
    repository_root: Path,
    proof_directory: Path,
    object_format: str,
    commit_id: str,
    proof_bytes: bytes,
) -> Path:
    """Atomically persist ``proof_bytes`` as ``<proof_directory>/<commit_id>.ots``.

    Writes to a temporary file in the same directory, flushes and fsyncs,
    then ``os.replace``s into the final path so partial ``.ots`` files are
    never observable at the final location (spec section 23).

    ``proof_directory`` is interpreted relative to ``repository_root`` and
    must already exist. ``object_format``/``commit_id`` are validated with
    the same rules as :func:`build_payload` so a malformed identifier can
    never become a filename.
    """
    # Reuse the canonical payload validator to constrain the filename and to
    # ensure the proof is bound to the exact source commit being persisted.
    payload = build_payload(object_format, commit_id)
    if not proof_bytes:
        raise PersistenceError("proof bytes must be non-empty")
    try:
        validate_detached_proof(proof_bytes, payload)
    except ValueError as exc:
        raise PersistenceError(
            f"invalid OpenTimestamps proof for {commit_id}: {exc}"
        ) from None
    proof_dir = _resolve_proof_dir(repository_root, proof_directory)
    final_path = proof_dir / f"{commit_id}.ots"
    return _atomic_write(
        final_path=final_path,
        content=proof_bytes,
        tmp_prefix=f".{commit_id}.",
    )


def persist_upgraded_proof(
    *,
    repository_root: Path,
    proof_directory: Path,
    object_format: str,
    commit_id: str,
    proof_bytes: bytes,
) -> Path:
    """Atomically replace an existing ``<commit_id>.ots`` with upgraded bytes.

    Shares :func:`persist_proof`'s validation and atomic-write boundary but
    overwrites deliberately, which is the one case where replacing a stored
    proof is legitimate: an upgrade adds attestations to a proof that already
    binds to the same source commit. The binding is revalidated here rather
    than trusted from the adapter, and the file must already exist -- this
    function never creates a proof for an unstamped commit.
    """
    payload = build_payload(object_format, commit_id)
    if not proof_bytes:
        raise PersistenceError("upgraded proof bytes must be non-empty")
    try:
        validate_detached_proof(proof_bytes, payload)
    except ValueError as exc:
        raise PersistenceError(
            f"invalid upgraded OpenTimestamps proof for {commit_id}: {exc}"
        ) from None
    proof_dir = _resolve_proof_dir(repository_root, proof_directory)
    final_path = proof_dir / f"{commit_id}.ots"
    if not final_path.is_file():
        raise PersistenceError(
            f"cannot upgrade a proof that does not exist: {final_path}"
        )
    return _atomic_write(
        final_path=final_path,
        content=proof_bytes,
        tmp_prefix=f".{commit_id}.",
        force=True,
    )


def persist_manifest(
    *,
    repository_root: Path,
    proof_directory: Path,
    manifest: dict,
) -> Path:
    """Atomically persist a schema-1 manifest as ``<proof_directory>/<commit_id>.json``.

    The JSON form is deterministic: two-space indentation and a trailing
    newline, so rerunning with the same manifest produces byte-identical
    output. Reuses the same atomic-write boundary as :func:`persist_proof`
    (temp file, fsync, ``os.replace``) so a partial manifest is never
    observable at the final path.

    An existing manifest is overwritten only when the new content is
    byte-identical; otherwise a :class:`PersistenceError` is raised and the
    existing file is preserved.
    """
    commit_id = manifest.get("source_commit")
    object_format = manifest.get("git_object_format")
    if not isinstance(commit_id, str) or not isinstance(object_format, str):
        raise PersistenceError(
            "manifest must include source_commit and git_object_format strings"
        )
    # Constrain the filename with the same validator used for proofs.
    build_payload(object_format, commit_id)

    proof_dir = _resolve_proof_dir(repository_root, proof_directory)
    final_path = proof_dir / f"{commit_id}.json"
    content = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    return _atomic_write(
        final_path=final_path,
        content=content,
        tmp_prefix=f".{commit_id}.",
    )


@dataclass(frozen=True, slots=True)
class ProofEvidence:
    """The four facts a timestamp tag asserts, from validated proof evidence.

    A tag annotation carries exactly ``source``, ``submitted-at``, ``proof``
    and ``triggers`` (spec section 12). This record is what a completion or
    repair path needs, and deliberately nothing more: it is produced only
    after the manifest schema has been validated and the referenced proof has
    been shown to bind cryptographically to the canonical payload for
    ``source_commit_id`` (ADR 0001 D5 -- the manifest is checked, not
    trusted).

    It is decoupled from where the evidence was read. ``RecoveryArtifacts``
    (worktree) and :func:`git_ots.git.read_committed_proof_evidence` (a
    commit tree) both reduce to this, so the tag-completion path can treat
    the two sources identically instead of branching on provenance.
    """

    source_commit_id: str
    submitted_at: datetime
    triggers: frozenset[str]
    proof_name: str


@dataclass(frozen=True, slots=True)
class RecoveryArtifacts:
    """Validated proof/manifest pair read from disk.

    Returned by :func:`inspect_recovery_artifacts` only when the pair is
    internally consistent and matches the caller's expected commit and
    object format. The dataclass is frozen so downstream code can rely on
    the values not changing between inspection and any subsequent action.

    ``submitted_at`` and ``triggers`` are parsed from the manifest so that
    crash-recovery can recreate the original timestamp tag exactly as it
    would have been created during the submission that produced the proof.
    """

    proof_path: Path
    manifest_path: Path
    proof_bytes: bytes
    manifest: dict
    submitted_at: datetime
    triggers: frozenset[str]

    def as_evidence(self, commit_id: str) -> ProofEvidence:
        """Narrow these artifacts to the four facts a timestamp tag needs."""
        return ProofEvidence(
            source_commit_id=commit_id,
            submitted_at=self.submitted_at,
            triggers=self.triggers,
            proof_name=self.manifest.get("proof", f"{commit_id}.ots"),
        )


def _validate_recovery_manifest(
    *,
    manifest: dict,
    commit_id: str,
    object_format: str,
) -> tuple[datetime, frozenset[str]]:
    """Validate the schema-1 manifest fields required for safe recovery.

    Returns the parsed UTC ``submitted_at`` and the trigger set. Any
    deviation from the documented manifest schema raises
    :class:`RecoveryValidationError`.
    """

    def _fail(reason: str) -> RecoveryValidationError:
        return RecoveryValidationError(f"invalid recovery manifest: {reason}")

    schema = manifest.get("schema")
    if schema != _MANIFEST_SCHEMA_V1:
        raise _fail(f"schema must be {_MANIFEST_SCHEMA_V1}, got {schema!r}")

    payload_format = manifest.get("payload_format")
    if payload_format != _PAYLOAD_FORMAT_V1:
        raise _fail(
            f"payload_format must be {_PAYLOAD_FORMAT_V1!r}, got {payload_format!r}"
        )

    submitted_text = manifest.get("submitted_at")
    if not isinstance(submitted_text, str) or not submitted_text.endswith("Z"):
        raise _fail(
            f"submitted_at must be a UTC RFC 3339 string ending in 'Z', got {submitted_text!r}"
        )
    try:
        submitted_at = datetime.fromisoformat(submitted_text)
    except ValueError as exc:
        raise _fail(
            f"submitted_at is not a valid RFC 3339 timestamp: {submitted_text!r}"
        ) from exc
    if submitted_at.tzinfo is not UTC:
        raise _fail(f"submitted_at must be UTC, got {submitted_text!r}")

    proof_name = manifest.get("proof")
    expected_proof = f"{commit_id}.ots"
    if proof_name != expected_proof:
        raise _fail(f"proof must be {expected_proof!r}, got {proof_name!r}")

    source_ref = manifest.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref:
        raise _fail(f"source_ref must be a non-empty string, got {source_ref!r}")

    triggers_value = manifest.get("triggers")
    if not isinstance(triggers_value, (list, tuple)) or not triggers_value:
        raise _fail(f"triggers must be a non-empty list, got {triggers_value!r}")
    if not all(isinstance(t, str) for t in triggers_value):
        raise _fail(f"triggers must be strings, got {triggers_value!r}")
    triggers = frozenset(str(t) for t in triggers_value)
    unknown = triggers - _KNOWN_TRIGGERS
    if unknown:
        raise _fail(f"unknown trigger(s): {', '.join(sorted(unknown))}")

    return submitted_at, triggers


def inspect_recovery_artifacts_if_present(
    *,
    repository_root: Path,
    proof_directory: Path,
    object_format: str,
    commit_id: str,
) -> RecoveryArtifacts | None:
    """Validate recovery artifacts only if at least one file exists.

    Returns a validated :class:`RecoveryArtifacts` when a complete, valid pair
    exists. Returns ``None`` when neither the proof nor the manifest file
    exists, indicating a normal new submission. Raises
    :class:`RecoveryValidationError` when any artifact exists but is
    incomplete or invalid, so callers fail closed rather than overwriting or
    resubmitting.
    """
    proof_dir = repository_root / proof_directory
    proof_path = proof_dir / f"{commit_id}.ots"
    manifest_path = proof_dir / f"{commit_id}.json"

    if not proof_path.is_file() and not manifest_path.is_file():
        return None

    return inspect_recovery_artifacts(
        repository_root=repository_root,
        proof_directory=proof_directory,
        object_format=object_format,
        commit_id=commit_id,
    )


def inspect_recovery_artifacts(
    *,
    repository_root: Path,
    proof_directory: Path,
    object_format: str,
    commit_id: str,
) -> RecoveryArtifacts:
    """Read and validate a proof/manifest pair for ``commit_id``.

    Implements the read-only half of spec section 22.1. The function never
    writes, creates, or deletes files. Any inconsistency — missing proof,
    missing manifest, malformed JSON, non-object manifest, mismatched
    source commit, mismatched object format, invalid schema, payload format,
    submitted_at, proof filename, source_ref, or trigger list — raises
    :class:`RecoveryValidationError` so callers cannot accidentally treat
    partial or corrupt state as a successful prior submission.
    """
    # Validate identifiers so a malformed id never becomes a path component.
    # The commit_id is checked against its own length/shape (not the
    # caller-supplied object_format's length) so that an object-format
    # mismatch can be reported as a recovery error rather than a payload
    # error. Both the commit_id and object_format are then cross-checked
    # against the manifest below.
    if object_format not in _FORMAT_LENGTHS:
        raise RecoveryValidationError(f"unsupported object format: {object_format!r}")
    if not commit_id or any(ch not in _HEX_DIGITS for ch in commit_id):
        raise RecoveryValidationError(
            f"commit id must be lowercase hexadecimal: {commit_id!r}"
        )
    proof_dir = _resolve_proof_dir(repository_root, proof_directory)
    proof_path = proof_dir / f"{commit_id}.ots"
    manifest_path = proof_dir / f"{commit_id}.json"

    if not proof_path.is_file():
        raise RecoveryValidationError(f"proof file missing: {proof_path}")
    if not manifest_path.is_file():
        raise RecoveryValidationError(f"manifest file missing: {manifest_path}")

    proof_bytes = proof_path.read_bytes()
    if not proof_bytes:
        raise RecoveryValidationError(f"proof file is empty: {proof_path}")

    # Rebuild the canonical payload for this commit/object-format pair and
    # verify the detached proof is bound to it. A structurally valid proof for
    # a different source is not safe recovery evidence.
    try:
        payload = build_payload(object_format, commit_id)
    except PayloadValidationError as exc:
        raise RecoveryValidationError(
            f"cannot build canonical payload for {commit_id!r}: {exc}"
        ) from exc
    try:
        validate_detached_proof(proof_bytes, payload)
    except ValueError as exc:
        raise RecoveryValidationError(
            f"invalid OpenTimestamps proof for {commit_id}: {exc}"
        ) from exc

    try:
        raw_manifest = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RecoveryValidationError(
            f"manifest is not readable UTF-8: {manifest_path}"
        ) from exc
    try:
        manifest = json.loads(raw_manifest)
    except json.JSONDecodeError as exc:
        raise RecoveryValidationError(
            f"manifest is not valid JSON: {manifest_path}"
        ) from exc
    if not isinstance(manifest, dict):
        raise RecoveryValidationError(
            f"manifest must be a JSON object: {manifest_path}"
        )

    if manifest.get("source_commit") != commit_id:
        raise RecoveryValidationError(
            f"manifest source_commit does not match {commit_id!r}: "
            f"{manifest.get('source_commit')!r}"
        )
    if manifest.get("git_object_format") != object_format:
        raise RecoveryValidationError(
            f"manifest git_object_format does not match {object_format!r}: "
            f"{manifest.get('git_object_format')!r}"
        )

    submitted_at, triggers = _validate_recovery_manifest(
        manifest=manifest,
        commit_id=commit_id,
        object_format=object_format,
    )

    return RecoveryArtifacts(
        proof_path=proof_path,
        manifest_path=manifest_path,
        proof_bytes=proof_bytes,
        manifest=manifest,
        submitted_at=submitted_at,
        triggers=triggers,
    )


def _resolve_proof_dir(repository_root: Path, proof_directory: Path) -> Path:
    if proof_directory.is_absolute() or ".." in proof_directory.parts:
        raise PersistenceError(
            f"proof directory must be a repository-relative path: {proof_directory!r}"
        )
    proof_dir = repository_root / proof_directory
    if not proof_dir.is_dir():
        raise PersistenceError(f"proof directory does not exist: {proof_dir}")
    return proof_dir


def _atomic_write(
    *,
    final_path: Path,
    content: bytes,
    tmp_prefix: str,
    force: bool = False,
) -> Path:
    if final_path.exists():
        existing = final_path.read_bytes()
        if existing == content:
            return final_path
        if not force:
            raise PersistenceError(
                f"file already exists with different content: {final_path}"
            )

    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=final_path.parent,
        prefix=tmp_prefix,
        suffix=".tmp",
        delete=False,
    ) as tmp_file:
        tmp_path = Path(tmp_file.name)
        try:
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
            os.replace(tmp_path, final_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
    return final_path
