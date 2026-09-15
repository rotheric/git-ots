"""Opt-in reachability checks for the calendars a repository's proofs name.

``git-ots`` does not configure calendars -- the OpenTimestamps client owns
that -- so the servers this repository actually depends on are the ones named
in its stored proofs. A calendar that has gone away means the pending proofs
promising to complete through it never will, which is invisible from the
proof files alone.

This is the only place in the tool that makes an outbound request outside a
submission or an upgrade, and it runs solely behind ``status --check-servers``.
``status`` is otherwise read-only and offline by contract, because it is the
command a scheduler runs.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .anchors import AnchorExtractionError, describe_anchors
from .config import Config
from .proof_tree import MAX_RESPONSE_BYTES

_DEFAULT_TIMEOUT_SECONDS = 5.0
_USER_AGENT = "git-ots/1.0 (+calendar reachability check)"


@dataclass(frozen=True, slots=True)
class ServerStatus:
    """Reachability of one calendar named in this repository's proofs."""

    url: str
    reachable: bool
    detail: str
    pending_proofs: int


def collect_calendar_urls(*, repository_root: Path, config: Config) -> dict[str, int]:
    """Return calendar URLs named by stored proofs, mapped to pending counts.

    A calendar counts as pending for every proof that named it and has not
    received its attestation, whether or not some other calendar already
    anchored that proof: ``git-ots upgrade`` asks each of them on every run, so
    an outage really does hold up work. Calendars that have delivered appear
    with a count of zero.
    """
    proof_dir = repository_root / config.proof.directory
    if not proof_dir.is_dir():
        return {}

    counts: dict[str, int] = {}
    for proof_path in sorted(proof_dir.iterdir()):
        if not proof_path.is_file() or proof_path.suffix != ".ots":
            continue
        try:
            described = describe_anchors(proof_path.read_bytes())
        except (OSError, AnchorExtractionError):
            # Unreadable proofs are reported by `validate`; a reachability check
            # is not the place to raise on them.
            continue
        for anchor in described.anchors:
            for url in anchor.calendars:
                counts.setdefault(url, 0)
        for url in described.pending_calendars:
            counts[url] = counts.get(url, 0) + 1
    return counts


def _probe(url: str, *, timeout: float, opener) -> tuple[bool, str]:
    """Return whether ``url`` answers, plus a short diagnostic."""
    scheme = urlsplit(url).scheme
    if scheme not in ("http", "https"):
        # Calendar URLs come out of proof files, so they are attacker-supplied
        # in the limit. Only ever speak HTTP to them.
        return False, f"unsupported URL scheme {scheme!r}"

    # The scheme is checked above, so this never opens a file:// or other
    # local-resource URL out of a proof.
    request = urllib.request.Request(
        url, method="GET", headers={"User-Agent": _USER_AGENT}
    )
    try:
        with opener(request, timeout=timeout) as response:
            return True, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        # The server answered, which is what reachability means here. A
        # calendar root commonly replies 404 or 405 while being perfectly able
        # to serve the digest endpoints an upgrade uses.
        return True, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, str(exc.reason)
    except (TimeoutError, OSError) as exc:
        return False, str(exc)


def check_calendars(
    *,
    repository_root: Path,
    config: Config,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    opener=None,
) -> tuple[ServerStatus, ...]:
    """Probe every calendar this repository's proofs name.

    Returns one result per distinct calendar, ordered by URL. Failures are
    reported rather than raised: an unreachable calendar is information about
    the world, not a malfunction of this command.
    """
    _opener = opener if opener is not None else urllib.request.urlopen
    counts = collect_calendar_urls(repository_root=repository_root, config=config)
    results: list[ServerStatus] = []
    for url in sorted(counts):
        reachable, detail = _probe(url, timeout=timeout, opener=_opener)
        results.append(
            ServerStatus(
                url=url,
                reachable=reachable,
                detail=detail,
                pending_proofs=counts[url],
            )
        )
    return tuple(results)


def fetch_calendar_timestamp(
    url: str,
    commitment: bytes,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    opener=None,
) -> tuple[bytes | None, str]:
    """Ask a calendar for the timestamp it filed under ``commitment``.

    Returns the response body and a short diagnostic. The body is ``None``
    when the calendar has nothing to give -- a 404, which is how a calendar
    reports "still pending", or any failure. Nothing here raises: a calendar
    that is down or has not confirmed yet is an ordinary outcome of asking.

    This speaks the calendar protocol directly rather than through the
    OpenTimestamps client, because the client deliberately stops asking once a
    proof has any Bitcoin attestation.
    """
    scheme = urlsplit(url).scheme
    if scheme not in ("http", "https"):
        return None, f"unsupported URL scheme {scheme!r}"

    endpoint = f"{url.rstrip('/')}/timestamp/{commitment.hex()}"
    request = urllib.request.Request(
        endpoint,
        method="GET",
        headers={
            "Accept": "application/vnd.opentimestamps.v1",
            "User-Agent": _USER_AGENT,
        },
    )
    _opener = opener if opener is not None else urllib.request.urlopen
    try:
        with _opener(request, timeout=timeout) as response:
            if response.status != 200:
                return None, f"HTTP {response.status}"
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, "not yet confirmed"
        return None, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, str(exc.reason)
    except (TimeoutError, OSError) as exc:
        return None, str(exc)

    if not body:
        return None, "empty response"
    if len(body) > MAX_RESPONSE_BYTES:
        return None, "response exceeded size limit"
    return body, "ok"
