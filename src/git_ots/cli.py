from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import traceback
from collections import Counter
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

from . import __version__
from .anchors import AnchorExtractionError, describe_anchors
from .config import ConfigError, validate_proof_directory
from .git import (
    GitCommandError,
    InvalidRepositoryStateError,
    ProcessRunner,
    ProcessRunnerBytes,
    RepositoryLayout,
    RepositoryLock,
    RepositoryLockedError,
    assert_committable_state,
    assert_worktree_present,
    locate_repository,
    make_process_runner,
    make_process_runner_bytes,
    maybe_fetch_before_run,
    push_current_branch,
)
from .gitconfig import MAPPING, assemble_config, describe_effective_configuration
from .orchestration import build_snapshot
from .orchestration import run as run_orchestration
from .repair import RepairAction, repair_missing_tags
from .servers import check_calendars
from .timestamp import (
    OpenTimestampsCli,
    PersistenceError,
    RecoveryValidationError,
    SubmissionError,
)
from .upgrade import UpgradeState, upgrade_proofs
from .verify import ChainState, ProofState, validate_proofs, verify_proofs

_logger = logging.getLogger("git_ots")

# Opt-in exit code for `run --exit-code` when the run was correctly idle.
#
# Deliberately outside the documented 0-7 failure range rather than extending
# it. FS-0008 records the project's position that the exit-code table is small
# on purpose and that structured output, not more codes, is where per-run
# detail belongs; and making "nothing was due" non-zero by default would break
# every existing cron and launchd wrapper at once -- for the *common* case,
# turning healthy runs into reported failures. So the distinction is available
# to a caller that asks for it and invisible to one that does not, and the
# value it uses can never be confused with a failure the table already
# defines.
IDLE_EXIT_CODE = 100


def _configure_logging(verbose: bool) -> None:
    """Configure diagnostics to stderr only; never write persistent log files."""
    logger = logging.getLogger("git_ots")
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO if verbose else logging.WARNING)


def _debug_from_environment() -> bool:
    value = os.environ.get("GIT_OTS_DEBUG", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _unexpected_error(exc: Exception, *, debug: bool) -> int:
    if debug:
        traceback.print_exc()
    else:
        print(
            f"Unexpected error ({type(exc).__name__}): {exc} "
            "(re-run with --debug for a traceback)",
            file=sys.stderr,
        )
    return 1


def _interrupted(*, debug: bool) -> int:
    if debug:
        traceback.print_exc()
    else:
        print("Interrupted; terminated the in-progress operation.", file=sys.stderr)
    return 130


def _locate_repository(cwd: Path) -> RepositoryLayout:
    """Locate the repository containing ``cwd``.

    A Git failure is surfaced as an :class:`InvalidRepositoryStateError`
    so the CLI maps it to the documented repository-state exit code.
    """
    try:
        return locate_repository(cwd=cwd)
    except GitCommandError as exc:
        detail = exc.stderr.strip() or str(exc)
        raise InvalidRepositoryStateError(
            f"cannot locate Git repository at {cwd}: {detail}"
        ) from exc


def _configured_process_runners(config) -> tuple[ProcessRunner, ProcessRunnerBytes]:
    """Build this invocation's configured git process runners.

    Built once per subcommand, immediately after ``assemble_config`` returns,
    so it is supplied explicitly into every call that accepts a
    ``process_runner`` -- ``build_snapshot``, ``validate_proofs``,
    ``upgrade_proofs``, ``maybe_fetch_before_run``, and
    ``assert_committable_state``. Before this point, ``_locate_repository``'s
    two pre-config ``git rev-parse`` calls and ``assemble_config``'s own
    ``ots.*`` layer read run against the built-in default instead -- that
    ceiling lives in ``_default_process_runner``/``_default_process_runner_bytes``
    themselves, not here (Design Decision 10; FS-0015 behaviour 4 joins the
    layer read on this same near side of the split).

    ``config.limits.git_timeout is None`` is an operator's ``git_timeout =
    "0"``; it is threaded straight through as an unbounded ceiling rather than
    being coalesced into any default.
    """
    git_timeout = config.limits.git_timeout
    seconds = git_timeout.total_seconds() if git_timeout is not None else None
    return (
        make_process_runner(timeout=seconds),
        make_process_runner_bytes(timeout=seconds),
    )


def _format_age(td: timedelta) -> str:
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return "".join(parts) if parts else "0m"


def _check_opentimestamps_command(command: str) -> str | None:
    """Return a warning when the configured OpenTimestamps command is unavailable.

    Reported by ``status`` rather than at configuration time: the client is an
    external dependency that can be uninstalled, upgraded off ``PATH``, or
    absent on the machine the scheduler actually runs on, so a check that fires
    once at setup is a check that fires at the moment it matters least.
    """
    if shutil.which(command) is None:
        return (
            f"Warning: OpenTimestamps command {command!r} not found in PATH; "
            "`git-ots run` will fail until the client is installed."
        )
    return None


def _print_decision(snapshot) -> None:
    if not snapshot.decision.commits:
        if snapshot.pending:
            oldest = min(snapshot.pending, key=lambda c: c.committer_time)
            age = snapshot.policy_state.now - oldest.committer_time
            print(
                f"No timestamp required: {len(snapshot.pending)} pending commits, "
                f"oldest age {_format_age(age)}."
            )
        else:
            print("No timestamp required.")
        return

    for commit in snapshot.decision.commits:
        short = commit.commit_id[:12]
        print(f"Selected: {commit.commit_id} ({short})")
    print(f"Triggers: {' '.join(sorted(snapshot.decision.triggers))}")


def _print_status(snapshot) -> None:
    source = snapshot.source
    baseline = snapshot.baseline.baseline
    pending = snapshot.pending

    print(f"source ref: {source.display_ref}")
    print(f"source commit: {source.commit_id[:12]}")
    if baseline is None:
        print("last timestamped source: none")
    else:
        print(f"last timestamped source: {baseline.source_commit_id[:12]}")
    print(f"pending meaningful: {len(pending)}")
    if pending:
        oldest = min(pending, key=lambda c: c.committer_time)
        age = snapshot.policy_state.now - oldest.committer_time
        print(f"oldest pending age: {_format_age(age)}")
    else:
        print("oldest pending age: N/A")

    if "max_age" in snapshot.decision.triggers:
        print("max-age trigger: due")
    else:
        print("max-age trigger: not due")

    if "fixed_time" in snapshot.decision.triggers:
        print("fixed-time trigger: due")
    else:
        print("fixed-time trigger: not due")


def _print_proof_anchors(*, repository_root: Path, proof_directory: str) -> None:
    """Print where each stored proof is anchored, from the proof bytes alone.

    Every value shown is computed offline by executing the proof's own
    operations: the block height is stored in the attestation, and the
    transaction id, its OP_RETURN commitment, and the block merkle root are
    intermediate messages along the path. Nothing here contacts Bitcoin, so
    this reports what the proof claims rather than confirming it on-chain.
    """
    proof_dir = repository_root / proof_directory
    if not proof_dir.is_dir():
        print("proofs: none")
        return

    proof_paths = sorted(
        path for path in proof_dir.iterdir() if path.is_file() and path.suffix == ".ots"
    )
    if not proof_paths:
        print("proofs: none")
        return

    described = []
    anchored = pending = unreadable = 0
    for proof_path in proof_paths:
        source_id = proof_path.stem
        try:
            anchors = describe_anchors(proof_path.read_bytes())
        except (OSError, AnchorExtractionError) as exc:
            described.append((source_id, None, str(exc)))
            unreadable += 1
            continue
        described.append((source_id, anchors, ""))
        if anchors.anchors:
            anchored += 1
        else:
            pending += 1

    summary = f"proofs: {anchored} anchored, {pending} pending"
    if unreadable:
        summary = f"{summary}, {unreadable} unreadable"
    print(summary)

    for source_id, anchors, error in described:
        if anchors is None:
            print(f"  {source_id[:12]} unreadable: {error}")
            continue
        state = "anchored" if anchors.anchors else "pending"
        promised = len(anchors.promised_calendars)
        detail = ""
        if promised:
            detail = (
                f" ({len(anchors.anchoring_calendars)} of {promised} "
                f"calendars anchored)"
            )
        print(f"  {source_id[:12]} {state}{detail}")
        for anchor in anchors.anchors:
            via = anchor.calendars[0] if anchor.calendars else "unknown calendar"
            print(f"    block {anchor.block_height} via {via}")
            if anchor.transaction_id is not None:
                print(f"      tx {anchor.transaction_id}")
            if anchor.op_return_data is not None:
                print(f"      op_return {anchor.op_return_data}")
            print(f"      merkle root {anchor.block_merkle_root}")
        for calendar in anchors.pending_calendars:
            # True regardless of whether the proof is already anchored:
            # `git-ots upgrade` asks every calendar that has not yet delivered,
            # rather than stopping at the first Bitcoin attestation the way the
            # OpenTimestamps client does.
            print(f"    awaiting {calendar}")


def _print_server_checks(*, repository_root: Path, config) -> None:
    """Probe the calendars this repository's proofs name and report each one.

    Unreachable calendars are additionally warned about on stderr, because a
    calendar that has gone away means the proofs promising to complete through
    it never will -- which the proof files themselves cannot show.
    """
    results = check_calendars(repository_root=repository_root, config=config)
    if not results:
        print("calendars: none named by stored proofs")
        return

    reachable = sum(1 for result in results if result.reachable)
    print(f"calendars: {reachable}/{len(results)} reachable")
    for result in results:
        state = "ok" if result.reachable else "unavailable"
        waiting = f", {result.pending_proofs} pending" if result.pending_proofs else ""
        print(f"  {result.url} {state} ({result.detail}{waiting})")

    sys.stdout.flush()
    for result in results:
        if result.reachable:
            continue
        if result.pending_proofs:
            _logger.warning(
                "Warning: calendar %s is unavailable (%s); %d proof(s) are "
                "waiting on it and cannot be completed by `git-ots upgrade`.",
                result.url,
                result.detail,
                result.pending_proofs,
            )
        else:
            _logger.warning(
                "Warning: calendar %s is unavailable (%s); no proof is "
                "currently waiting on it.",
                result.url,
                result.detail,
            )


def _format_config_duration(td: timedelta) -> str:
    """Render a duration the way ``ots.maxAge``/``ots.gitTimeout``/
    ``ots.otsTimeout`` accept it: the largest of ``d``/``h``/``m``/``s``
    that divides it evenly. ``config.parse_duration``'s grammar accepts
    exactly one unit per value, and any value it accepts has an exact
    representation in one of these four, so this always renders an
    equivalent (if not necessarily identically-spelled) duration -- never a
    fraction and never a value ``ots.gitTimeout=0``'s escape hatch would
    also accept, since a real timeout is never zero.
    """
    total_seconds = int(td.total_seconds())
    for unit_seconds, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        if total_seconds and total_seconds % unit_seconds == 0:
            return f"{total_seconds // unit_seconds}{suffix}"
    return f"{total_seconds}s"


def _format_config_value(section: str, value: object) -> str:
    """Render one ``Config`` field for the ``git-ots config`` report.

    Two ``None``-valued cases must not be spelled the same way. A
    ``limits.*`` field's ``None`` is always an operator's explicit
    ``ots.gitTimeout=0``/``ots.otsTimeout=0`` (both fields default to a
    concrete duration, never to ``None``), so it prints as the unbounded
    escape hatch itself. A ``policy.*`` trigger field's ``None`` means the
    effective configuration does not carry that trigger at all -- unset
    anywhere, or set somewhere behaviour 8's ladder discarded (see
    ``gitconfig.describe_effective_configuration``'s docstring for why
    those two cases are indistinguishable by design) -- so it prints as
    ``<unset>``, distinct from ``false``/``0``, which are themselves valid,
    present ``Config`` values.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "0 (unbounded)" if section == "limits" else "<unset>"
    if isinstance(value, timedelta):
        return _format_config_duration(value)
    if isinstance(value, dt_time):
        return value.strftime("%H:%M")
    return str(value)  # plain strings, and ZoneInfo (str() is its IANA key)


def _print_config_report(config, origins: dict[str, str]) -> None:
    """Print the effective configuration, one row per ``ots.*`` key.

    Every one of :data:`MAPPING`'s fifteen rows is shown, in its
    declaration order, whether or not an operator set it -- an unconfigured
    key's row (e.g. ``ots.signing  inherit  default``) is as much part of
    "the effective configuration" as a configured one, and showing every
    row is what lets two reports be diffed to see exactly which keys
    differ, not only which keys happen to be set. Keys print in Git's
    canonical lower-case form -- the same rule behaviour 3 states for
    diagnostics -- so what this prints matches what ``git config --list``
    shows for the same repository.
    """
    rows = [
        (
            row.key,
            _format_config_value(
                row.section, getattr(getattr(config, row.section), row.field)
            ),
            origins[row.key],
        )
        for row in MAPPING
    ]
    key_width = max(len(key) for key, _, _ in rows)
    value_width = max(len(value) for _, value, _ in rows)
    for key, value, origin in rows:
        print(f"{key.ljust(key_width)}  {value.ljust(value_width)}  {origin}")


def _config_command(*, cwd: Path, verbose: bool) -> int:
    _configure_logging(verbose)
    layout = _locate_repository(cwd)
    config, origins = describe_effective_configuration(cwd=layout.worktree_root)
    _print_config_report(config, origins)
    return 0


def _run_command(
    *,
    cwd: Path,
    now: datetime,
    dry_run: bool,
    verbose: bool,
    exit_code: bool = False,
    push: bool = False,
) -> int:
    _configure_logging(verbose)
    _logger.info("Inspecting repository state")
    layout = _locate_repository(cwd)
    # A bare repository has no working tree at all: locate_repository falls
    # back to the Git directory as worktree_root so read-only commands keep
    # working (AC-BARE-1), but `run` writes proof files, a lock file, and
    # generated commits/tags. Refuse before any of that -- including before
    # the lock file below -- rather than after failing for an unrelated
    # reason (see the finding this guards against: an unguarded `run` wrote
    # `.opentimestamps/` and `git-ots.lock` straight into a bare repo's Git
    # directory).
    assert_worktree_present(layout, operation="run")
    config = assemble_config(cwd=layout.worktree_root)
    process_runner, _process_runner_bytes = _configured_process_runners(config)
    with RepositoryLock(common_git_dir=layout.common_git_dir):
        maybe_fetch_before_run(
            cwd=layout.worktree_root,
            source_ref=config.git.source_ref,
            fetch_before_run=config.git.fetch_before_run,
            process_runner=process_runner,
        )
        snapshot = build_snapshot(
            cwd=layout.worktree_root,
            config=config,
            now=now,
            process_runner=process_runner,
        )
        if not dry_run:
            # Fail before announcing work this run cannot carry out. The
            # orchestration mutation boundary checks this too and remains the
            # authority for callers that bypass the CLI; repeating it here is
            # cheap, read-only, and keeps the refusal ahead of the decision
            # output. Dry runs skip it deliberately: they mutate nothing and
            # should still report what would happen.
            assert_committable_state(
                cwd=layout.worktree_root,
                require_symbolic_head=config.proof.commit,
                process_runner=process_runner,
            )
        triggers = ", ".join(sorted(snapshot.decision.triggers)) or "no action"
        _logger.info("Policy decision: %s", triggers)
        _print_decision(snapshot)
        outcome = None
        if not dry_run:
            if snapshot.decision.commits:
                _logger.info(
                    "Timestamping %d commit(s)", len(snapshot.decision.commits)
                )
            outcome = run_orchestration(snapshot=snapshot)
            for source_commit_id in outcome.tagged:
                if source_commit_id in outcome.timestamped:
                    continue
                # Completing an interrupted run is not routine and is not
                # visible in the decision output above, which correctly says
                # nothing was due. Report it on stdout so a scheduler's log
                # records that the repository was repaired rather than idle.
                print(
                    f"completed timestamp tag for {source_commit_id[:12]} "
                    "from its stored proof"
                )
            for source_commit_id in outcome.committed:
                if source_commit_id in outcome.timestamped:
                    continue
                print(f"committed stored proof artifacts for {source_commit_id[:12]}")
            if outcome.deferred:
                # The decision output above already announced this work as
                # due. Saying why it did not happen keeps the two consistent,
                # and keeps a one-run delay from reading as a silent drop.
                deferred = ", ".join(cid[:12] for cid in outcome.deferred)
                print(
                    f"deferred timestamping {deferred} to finish an earlier "
                    "run's unrecorded submission first"
                )
            if push and outcome.changed:
                push_current_branch(
                    cwd=layout.worktree_root,
                    process_runner=process_runner,
                )
            _logger.info("Operations complete")
    # A dry run reports rather than acts, so it is never "idle" in the sense
    # --exit-code asks about; it exits 0 whatever it found.
    if exit_code and not dry_run and outcome is not None and not outcome.changed:
        return IDLE_EXIT_CODE
    return 0


def _status_command(
    *,
    cwd: Path,
    now: datetime,
    verbose: bool,
    check_servers: bool = False,
) -> int:
    _configure_logging(verbose)
    _logger.info("Inspecting repository state")
    layout = _locate_repository(cwd)
    config = assemble_config(cwd=layout.worktree_root)
    process_runner, _process_runner_bytes = _configured_process_runners(config)
    snapshot = build_snapshot(
        cwd=layout.worktree_root,
        config=config,
        now=now,
        process_runner=process_runner,
    )
    _print_status(snapshot)
    _print_proof_anchors(
        repository_root=layout.worktree_root,
        proof_directory=config.proof.directory,
    )
    # Reachability is opt-in: `status` is what a scheduler runs, and it must
    # not become dependent on network conditions by default.
    if check_servers:
        _print_server_checks(repository_root=layout.worktree_root, config=config)
    # A missing client is not a status failure -- the repository state reported
    # above is accurate either way -- but it is the reason the next `run` will
    # fail, so it belongs on the command an operator reaches for first. stdout
    # is block-buffered when redirected, as under cron, so flush to keep the
    # two streams in the order the reader expects.
    ots_warning = _check_opentimestamps_command(config.opentimestamps.command)
    if ots_warning is not None:
        sys.stdout.flush()
        _logger.warning(ots_warning)
    else:
        client = OpenTimestampsCli(
            config.opentimestamps.command, limit=config.limits.ots_timeout
        )
        try:
            client_version, tested = client.probe()
        except SubmissionError as exc:
            _logger.warning("Warning: OpenTimestamps client probe failed: %s", exc)
        else:
            qualifier = "tested" if tested else "compatible, untested version"
            print(f"OpenTimestamps client: {client_version} ({qualifier})")
    return 0


def _validate_command(*, cwd: Path, verbose: bool, proof_dir: str | None) -> int:
    _configure_logging(verbose)
    layout = _locate_repository(cwd)
    config = assemble_config(cwd=layout.worktree_root)
    process_runner, _process_runner_bytes = _configured_process_runners(config)
    results = validate_proofs(
        cwd=layout.worktree_root,
        config=config,
        proof_directories=(Path(validate_proof_directory(proof_dir)),)
        if proof_dir
        else None,
        process_runner=process_runner,
    )

    if not results:
        print("No stored proofs found; nothing was validated.")
        return 0

    for result in results:
        short_source = (
            result.source_commit_id[:12] if result.source_commit_id else "unknown"
        )
        untagged = "" if result.has_timestamp_tag else "; untagged"
        print(
            f"{result.proof_path}: {result.state.value} "
            f"({short_source}) {result.reason}{untagged}"
        )

    # Reported, never fatal. A proof and its tag can diverge without anything
    # being wrong -- git-ots does not push tags, so a fresh clone has none --
    # but before this the divergence was not reported at all, which is how a
    # fully anchored proof stayed permanently untagged while every diagnostic
    # said the repository was healthy.
    untagged_count = sum(1 for result in results if not result.has_timestamp_tag)
    if untagged_count:
        print(
            f"{untagged_count} proof(s) have no timestamp tag. "
            "Run `git-ots repair` to recreate the tags from their manifests."
        )

    if any(
        result.state in (ProofState.INVALID, ProofState.UNKNOWN_SOURCE)
        for result in results
    ):
        return 5
    return 0


def _repair_command(*, cwd: Path, dry_run: bool, verbose: bool) -> int:
    _configure_logging(verbose)
    layout = _locate_repository(cwd)
    config = assemble_config(cwd=layout.worktree_root)
    process_runner, _process_runner_bytes = _configured_process_runners(config)
    results = repair_missing_tags(
        cwd=layout.worktree_root,
        config=config,
        dry_run=dry_run,
        process_runner=process_runner,
    )

    if not results:
        print("No stored proofs found.")
        return 0

    actionable = [
        result for result in results if result.action is not RepairAction.ALREADY_TAGGED
    ]
    if not actionable:
        print(f"All {len(results)} stored proof(s) already have a timestamp tag.")
        return 0

    for result in actionable:
        short_source = result.source_commit_id[:12]
        name = f" {result.tag_name}" if result.tag_name else ""
        print(
            f"{result.proof_path}: {result.action.value}{name} "
            f"({short_source}) {result.detail}"
        )

    # An unrepairable proof is the one case that must not be reported as
    # success: its manifest cannot be validated, so the four facts a tag
    # asserts are unavailable and no amount of rerunning will produce them.
    if any(result.action is RepairAction.UNREPAIRABLE for result in actionable):
        return 5
    return 0


def _verify_command(*, cwd: Path, verbose: bool, proof_dir: str | None) -> int:
    _configure_logging(verbose)
    layout = _locate_repository(cwd)
    config = assemble_config(cwd=layout.worktree_root)
    process_runner, _process_runner_bytes = _configured_process_runners(config)
    client = OpenTimestampsCli(
        config.opentimestamps.command, limit=config.limits.ots_timeout
    )
    results = verify_proofs(
        cwd=layout.worktree_root,
        config=config,
        client=client,
        proof_directories=(Path(validate_proof_directory(proof_dir)),)
        if proof_dir
        else None,
        process_runner=process_runner,
    )

    if not results:
        print("No stored proofs found; nothing was verified.")
        return 0

    for result in results:
        short_source = (
            result.source_commit_id[:12] if result.source_commit_id else "unknown"
        )
        context = (
            ""
            if result.validation_state is ProofState.VALID
            else f"; repository state: {result.validation_state.value}"
        )
        print(
            f"{result.proof_path}: {result.state.value} "
            f"({short_source}) {result.reason}{context}"
        )

    if any(result.validation_state is ProofState.INVALID for result in results):
        return 5
    if any(result.validation_state is ProofState.UNKNOWN_SOURCE for result in results):
        return 5
    if any(result.state is not ChainState.VERIFIED for result in results):
        return 4
    return 0


def _upgrade_command(*, cwd: Path, dry_run: bool, verbose: bool) -> int:
    _configure_logging(verbose)
    layout = _locate_repository(cwd)
    config = assemble_config(cwd=layout.worktree_root)
    process_runner, process_runner_bytes = _configured_process_runners(config)
    report = upgrade_proofs(
        cwd=layout.worktree_root,
        config=config,
        dry_run=dry_run,
        process_runner=process_runner,
        process_runner_bytes=process_runner_bytes,
    )

    if not report.results:
        print("No stored proofs found.")
        return 0

    for result in report.results:
        short_source = (
            result.source_commit_id[:12] if result.source_commit_id else "unknown"
        )
        ratio = ""
        if result.promised_calendars:
            ratio = (
                f"; {result.anchored_calendars} of "
                f"{result.promised_calendars} calendars anchored"
            )
        print(
            f"{result.proof_path}: {result.state.value} "
            f"({short_source}) {result.reason}{ratio}"
        )

    counts = Counter(result.state for result in report.results)
    upgraded = counts[UpgradeState.UPGRADED]
    pending = counts[UpgradeState.STILL_PENDING] + counts[UpgradeState.WOULD_UPGRADE]
    complete = counts[UpgradeState.ALREADY_COMPLETE]
    print(f"{upgraded} upgraded, {pending} still pending, {complete} already complete.")
    if report.commit_id is not None:
        print(f"Committed upgraded proofs as {report.commit_id[:12]}.")
    elif upgraded:
        print("Upgraded proofs are uncommitted in the worktree.")

    if counts[UpgradeState.SKIPPED]:
        return 5
    return 0


_SUMMARY = "timestamp Git commits on the Bitcoin blockchain with OpenTimestamps"

_COMMAND_SUMMARIES = (
    ("run", "timestamp the source commit when a policy trigger is due"),
    ("status", "show repository state, due triggers, and proof anchors"),
    ("validate", "check stored proofs against the repository, offline"),
    ("repair", "recreate timestamp tags missing for stored proofs"),
    ("verify", "verify stored proofs against Bitcoin through a node"),
    ("upgrade", "complete pending proofs from the calendars"),
    ("config", "report the effective configuration and each key's origin"),
)


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Format help the way Git's own commands do.

    Prose blocks are printed verbatim -- they are wrapped where they are
    written, so a paragraph break survives -- and the option column is widened
    so every option's description starts in the same column, the two-column
    shape ``git <command> -h`` produces.
    """

    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=32)


def _command_list() -> str:
    """Render the subcommand table shown by ``git-ots -h``, Git-style."""
    width = max(len(name) for name, _ in _COMMAND_SUMMARIES)
    return "\n".join(
        f"   {name.ljust(width)}   {summary}" for name, summary in _COMMAND_SUMMARIES
    )


def main(
    argv: list[str] | None = None,
    *,
    now: datetime | None = None,
    cwd: str | Path | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="git-ots",
        usage="git-ots <command> [<options>]",
        formatter_class=_HelpFormatter,
        add_help=False,
        description=(
            f"git-ots - {_SUMMARY}.\n"
            "\n"
            "It decides when a timestamp is due, submits the source commit to the\n"
            "OpenTimestamps calendars, stores the proof in the repository, and tags\n"
            "the commit it timestamped.\n"
            "\n"
            "These are the git-ots commands:\n"
            "\n"
            f"{_command_list()}"
        ),
        epilog=(
            "Run 'git-ots <command> -h' for that command's options.\n"
            "\n"
            "Configuration is optional: git-ots reads the 'ots.*' namespace from\n"
            "'git config' (any scope) and falls back to built-in defaults when a\n"
            "key is unset. Use 'git -c ots.<key>=<value>' to override one key for a\n"
            "single invocation, or GIT_CONFIG_GLOBAL to point at a whole\n"
            "configuration file. Installed on PATH, git-ots is also reachable as\n"
            "'git ots <command>'.\n"
            "\n"
            "Exit codes: 0 success, 1 failure, 2 usage or configuration, 3\n"
            "repository state, 4 OpenTimestamps operation, 5 proof persistence,\n"
            "6 git, 7 repository locked, 130 interrupted."
        ),
    )
    parser.add_argument(
        "-h",
        "--help",
        action="help",
        default=argparse.SUPPRESS,
        help="show this help and exit",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="show full tracebacks and verbose diagnostics",
    )
    parser.add_argument("--version", action="version", version=__version__)
    # The command table above is written by hand, so the generated one -- which
    # nests the choices under a metavar line no Git command prints -- is
    # suppressed rather than shown twice.
    subparsers = parser.add_subparsers(
        dest="command", metavar="<command>", help=argparse.SUPPRESS
    )

    def _add_common_arguments(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "-v",
            "--verbose",
            action="store_true",
            default=False,
            help="report progress to stderr",
        )
        # Added last, and hence listed last, because Git puts the boilerplate
        # after the options a reader came for.
        command_parser.add_argument(
            "-h",
            "--help",
            action="help",
            default=argparse.SUPPRESS,
            help="show this help and exit",
        )

    run_parser = subparsers.add_parser(
        "run",
        usage="git-ots run [--dry-run] [--exit-code] [--push] [-v]",
        formatter_class=_HelpFormatter,
        add_help=False,
        description=(
            "Timestamp the source commit when a policy trigger is due.\n"
            "\n"
            "Evaluates the configured policies against the repository. When a\n"
            "trigger is due, submits the selected commit to the calendars, writes\n"
            "the proof into the proof directory, tags the timestamped commit and\n"
            "commits the proof. It also finishes any bookkeeping an interrupted\n"
            "earlier run left behind. When nothing is due it says so and exits 0;\n"
            f"--exit-code makes that case exit {IDLE_EXIT_CODE} instead, so a scheduler can\n"
            "tell 'timestamped' from 'nothing to do' without parsing output.\n"
            "With --push, a successful change is followed by a push of the current\n"
            "branch and its reachable annotated timestamp tags.\n"
            "\n"
            "Needs the OpenTimestamps client on PATH and network access, and\n"
            "refuses to run on a worktree it cannot commit to."
        ),
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="report the decision only; contact nothing, write nothing",
    )
    run_parser.add_argument(
        "--exit-code",
        action="store_true",
        default=False,
        help=f"exit {IDLE_EXIT_CODE} instead of 0 when nothing was due",
    )
    run_parser.add_argument(
        "--push",
        action="store_true",
        default=False,
        help="push the current branch after a successful timestamp change",
    )
    _add_common_arguments(run_parser)

    status_parser = subparsers.add_parser(
        "status",
        usage="git-ots status [--check-servers] [-v]",
        formatter_class=_HelpFormatter,
        add_help=False,
        description=(
            "Show what the next run would do, and what the stored proofs say.\n"
            "\n"
            "Reports the source ref and commit, the last timestamped source commit,\n"
            "how many meaningful commits are pending and the age of the oldest,\n"
            "which triggers are due, and where each stored proof is anchored --\n"
            "block height, transaction, OP_RETURN commitment and merkle root, all\n"
            "read out of the proof itself rather than from a chain.\n"
            "\n"
            "Read-only, and offline unless --check-servers is given."
        ),
    )
    status_parser.add_argument(
        "--check-servers",
        action="store_true",
        default=False,
        help="probe the calendars named by stored proofs for reachability",
    )
    _add_common_arguments(status_parser)

    validate_parser = subparsers.add_parser(
        "validate",
        usage="git-ots validate [-v]",
        formatter_class=_HelpFormatter,
        add_help=False,
        description=(
            "Validate every stored proof against this repository.\n"
            "\n"
            "Reports one state per proof -- valid, orphaned, pending-attestation,\n"
            "invalid or unknown-source -- from the proof's structure and its\n"
            "binding to the commit its manifest names. A Bitcoin attestation is\n"
            "reported as present but is not checked against the chain. A proof\n"
            "with no matching 'ots/*' tag is flagged 'untagged'; 'git-ots repair'\n"
            "recreates the tag from the proof's manifest.\n"
            "\n"
            "Read-only and offline. Exits 5 if a proof is invalid or its source\n"
            "commit cannot be resolved; an untagged proof is not an error."
        ),
    )
    validate_parser.add_argument("--proof-dir", help="directory containing proofs")
    _add_common_arguments(validate_parser)

    repair_parser = subparsers.add_parser(
        "repair",
        usage="git-ots repair [--dry-run] [-v]",
        formatter_class=_HelpFormatter,
        add_help=False,
        description=(
            "Recreate timestamp tags that stored proofs are missing.\n"
            "\n"
            "An interrupted run can leave a proof submitted and stored but never\n"
            "tagged -- and the tag is the only place submitted-at, the source ref\n"
            "and the trigger set are recorded. This reconstructs the annotation\n"
            "from the proof's own manifest, after checking that the proof really\n"
            "does derive from the commit the manifest names.\n"
            "\n"
            "Never re-submits, never moves an existing tag, and never invents a\n"
            "submission time. Proofs whose source has been rewritten out of the\n"
            "lineage are reported and left alone. Exits 5 if a proof's manifest\n"
            "is missing or corrupt, since its tag cannot be reconstructed."
        ),
    )
    repair_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="report what would be tagged; create nothing",
    )
    _add_common_arguments(repair_parser)

    verify_parser = subparsers.add_parser(
        "verify",
        usage="git-ots verify [-v]",
        formatter_class=_HelpFormatter,
        add_help=False,
        description=(
            "Verify every stored proof against Bitcoin.\n"
            "\n"
            "Validates each proof locally, reconstructs its canonical payload,\n"
            "then delegates its Bitcoin attestation check to 'ots verify'. A\n"
            "reachable, configured Bitcoin node is required. Pending proofs are\n"
            "reported without invoking the client.\n"
            "\n"
            "Read-only but not offline. Exits 4 if any Bitcoin check cannot be\n"
            "completed, or 5 if a local proof artifact is invalid."
        ),
    )
    verify_parser.add_argument("--proof-dir", help="directory containing proofs")
    _add_common_arguments(verify_parser)

    upgrade_parser = subparsers.add_parser(
        "upgrade",
        usage="git-ots upgrade [--dry-run] [-v]",
        formatter_class=_HelpFormatter,
        add_help=False,
        description=(
            "Complete stored proofs that carry no Bitcoin attestation yet.\n"
            "\n"
            "Asks every calendar that has not yet delivered -- including the ones\n"
            "the OpenTimestamps client stops asking once another calendar has\n"
            "anchored the proof -- and commits what came back. 'git-ots run' never\n"
            "upgrades.\n"
            "\n"
            "Needs the OpenTimestamps client on PATH and network access. Exits 5 if\n"
            "a proof had to be skipped."
        ),
    )
    upgrade_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="name the calendars that would be asked; change nothing",
    )
    _add_common_arguments(upgrade_parser)

    config_parser = subparsers.add_parser(
        "config",
        usage="git-ots config [-v]",
        formatter_class=_HelpFormatter,
        add_help=False,
        description=(
            "Report the effective configuration and where each value came from.\n"
            "\n"
            "Prints every key in the 'ots.*' namespace, its effective value, and\n"
            "its origin -- 'git config <scope>' naming the winning Git scope\n"
            "(system, global, local, worktree, or command for '-c'/\n"
            "'GIT_CONFIG_*'), or 'default' when no scope sets it. A policy\n"
            "trigger set in a scope the precedence ladder discards also reports\n"
            "'default': its value is not part of the effective configuration,\n"
            "so crediting the discarded scope would misreport what is actually\n"
            "in effect.\n"
            "\n"
            "Read-only and takes no arguments; 'git config ots.<key> <value>' is\n"
            "how a key is set."
        ),
    )
    _add_common_arguments(config_parser)

    args = parser.parse_args(argv)
    debug = args.debug or _debug_from_environment()

    if args.command is None:
        # The whole help, not the usage line alone: an operator who typed the
        # bare command wants the list of commands, which is what `git` itself
        # prints here. The exit code stays non-zero so a scheduler that lost
        # its subcommand still fails.
        parser.print_help()
        return 2

    args.verbose = args.verbose or debug

    if now is None:
        now = datetime.now(UTC)
    if cwd is None:
        cwd = Path.cwd()
    else:
        cwd = Path(cwd)

    if args.command == "run":
        try:
            return _run_command(
                cwd=cwd,
                now=now,
                dry_run=args.dry_run,
                verbose=args.verbose,
                exit_code=args.exit_code,
                push=args.push,
            )
        except KeyboardInterrupt:
            # start_new_session=True (S2/S3) moves children out of the
            # terminal's foreground process group, so Ctrl-C no longer
            # reaches them on its own; both adapters' own bounded runners
            # terminate their in-flight child's process group before this
            # is ever reached -- the ots adapter's submit()/upgrade() call
            # (orchestration.py:_default_submit) via OpenTimestampsCli's
            # background-thread wrapper and terminate_active_child(), and
            # every bounded git invocation via
            # git.py:_run_bounded_git_subprocess's own
            # ``except BaseException: _kill_process_group(pgid); raise``,
            # which runs inline since GitRunner has no equivalent
            # background-thread wrapper of its own.
            return _interrupted(debug=debug)
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        except RepositoryLockedError as exc:
            print(f"Repository locked: {exc}", file=sys.stderr)
            return 7
        except InvalidRepositoryStateError as exc:
            print(f"Repository state error: {exc}", file=sys.stderr)
            return 3
        except SubmissionError as exc:
            print(f"OpenTimestamps submission failed: {exc}", file=sys.stderr)
            return 4
        except (PersistenceError, RecoveryValidationError) as exc:
            print(f"Proof persistence failed: {exc}", file=sys.stderr)
            return 5
        except GitCommandError as exc:
            print(f"Git command failed: {exc}", file=sys.stderr)
            return 6
        except Exception as exc:  # noqa: BLE001
            return _unexpected_error(exc, debug=debug)

    if args.command == "status":
        try:
            return _status_command(
                cwd=cwd,
                now=now,
                verbose=args.verbose,
                check_servers=args.check_servers,
            )
        except KeyboardInterrupt:
            return _interrupted(debug=debug)
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        except InvalidRepositoryStateError as exc:
            print(f"Repository state error: {exc}", file=sys.stderr)
            return 3
        except GitCommandError as exc:
            print(f"Git command failed: {exc}", file=sys.stderr)
            return 6
        except Exception as exc:  # noqa: BLE001
            return _unexpected_error(exc, debug=debug)

    if args.command == "validate":
        try:
            return _validate_command(
                cwd=cwd,
                verbose=args.verbose,
                proof_dir=args.proof_dir,
            )
        except KeyboardInterrupt:
            return _interrupted(debug=debug)
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        except InvalidRepositoryStateError as exc:
            print(f"Repository state error: {exc}", file=sys.stderr)
            return 3
        except GitCommandError as exc:
            print(f"Git command failed: {exc}", file=sys.stderr)
            return 6
        except Exception as exc:  # noqa: BLE001
            return _unexpected_error(exc, debug=debug)

    if args.command == "repair":
        try:
            return _repair_command(
                cwd=cwd,
                dry_run=args.dry_run,
                verbose=args.verbose,
            )
        except KeyboardInterrupt:
            return _interrupted(debug=debug)
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        except RepositoryLockedError as exc:
            print(f"Repository locked: {exc}", file=sys.stderr)
            return 7
        except InvalidRepositoryStateError as exc:
            print(f"Repository state error: {exc}", file=sys.stderr)
            return 3
        except (PersistenceError, RecoveryValidationError) as exc:
            print(f"Proof persistence failed: {exc}", file=sys.stderr)
            return 5
        except GitCommandError as exc:
            print(f"Git command failed: {exc}", file=sys.stderr)
            return 6
        except Exception as exc:  # noqa: BLE001
            return _unexpected_error(exc, debug=debug)

    if args.command == "verify":
        try:
            return _verify_command(
                cwd=cwd,
                verbose=args.verbose,
                proof_dir=args.proof_dir,
            )
        except KeyboardInterrupt:
            return _interrupted(debug=debug)
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        except InvalidRepositoryStateError as exc:
            print(f"Repository state error: {exc}", file=sys.stderr)
            return 3
        except SubmissionError as exc:
            print(f"OpenTimestamps verification failed: {exc}", file=sys.stderr)
            return 4
        except GitCommandError as exc:
            print(f"Git command failed: {exc}", file=sys.stderr)
            return 6
        except Exception as exc:  # noqa: BLE001
            return _unexpected_error(exc, debug=debug)

    if args.command == "config":
        try:
            return _config_command(
                cwd=cwd,
                verbose=args.verbose,
            )
        except KeyboardInterrupt:
            return _interrupted(debug=debug)
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        except InvalidRepositoryStateError as exc:
            print(f"Repository state error: {exc}", file=sys.stderr)
            return 3
        except GitCommandError as exc:
            print(f"Git command failed: {exc}", file=sys.stderr)
            return 6
        except Exception as exc:  # noqa: BLE001
            return _unexpected_error(exc, debug=debug)

    if args.command == "upgrade":
        try:
            return _upgrade_command(
                cwd=cwd,
                dry_run=args.dry_run,
                verbose=args.verbose,
            )
        except KeyboardInterrupt:
            return _interrupted(debug=debug)
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        except RepositoryLockedError as exc:
            print(f"Repository locked: {exc}", file=sys.stderr)
            return 7
        except InvalidRepositoryStateError as exc:
            print(f"Repository state error: {exc}", file=sys.stderr)
            return 3
        except SubmissionError as exc:
            print(f"OpenTimestamps upgrade failed: {exc}", file=sys.stderr)
            return 4
        except PersistenceError as exc:
            print(f"Proof persistence failed: {exc}", file=sys.stderr)
            return 5
        except GitCommandError as exc:
            print(f"Git command failed: {exc}", file=sys.stderr)
            return 6
        except Exception as exc:  # noqa: BLE001
            return _unexpected_error(exc, debug=debug)

    return 1


if __name__ == "__main__":
    sys.exit(main())
