from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time, timedelta
from zoneinfo import ZoneInfo

#: The built-in timestamping policy. A repository with no configuration at all
#: is timestamped daily; the value is the specification, not a starter-template
#: suggestion, so it can be revised for every user at once rather than only for
#: repositories initialised after the change.
DEFAULT_MAX_AGE = timedelta(hours=24)

#: Shared text for the sub-hour ``max_age`` operator warning. A module-level
#: constant rather than inlined into `gitconfig.assemble_config` -- its sole
#: emitter since S5 removed the TOML loader's own copy -- so a second call
#: site (e.g. a future `git-ots config` diagnostic) can raise the identical
#: wording without duplicating it.
SUBHOUR_MAX_AGE_WARNING = (
    "`max_age` below one hour cannot be honored more precisely than the "
    "Bitcoin block interval and requires a correspondingly frequent scheduler."
)

#: The default repository-relative directory proofs are written to and read
#: from. It is the value of ``ProofConfig.directory`` when ``ots.proofDirectory``
#: is unset, which is every repository that has not deliberately moved its
#: proofs -- so this stays the one spelling a reader of an unconfigured
#: repository has to know, and the one place to change it.
#:
#: FS-0015 behaviour 15 briefly made this the *only* possible value, removing
#: ``ProofConfig.directory`` in favour of a bare constant. That is reversed:
#: the directory is configurable again through ``ots.proofDirectory``, and the
#: constant is once more a default rather than a fixed law. The discovery
#: hazard behaviour 15 named is real and unchanged -- a consumer who clones a
#: repository whose proofs were moved has to be told where to look, because
#: nothing in the repository can tell them -- so moving the directory remains
#: a deliberate act with a cost, not a routine preference.
PROOF_DIRECTORY = ".opentimestamps"

_DURATION_PATTERN = re.compile(r"^(\d+)([smhd])$")
_DURATION_UNITS = {
    "s": lambda n: timedelta(seconds=n),
    "m": lambda n: timedelta(minutes=n),
    "h": lambda n: timedelta(hours=n),
    "d": lambda n: timedelta(days=n),
}

_FIXED_TIME_PATTERN = re.compile(r"^(\d{2}):(\d{2})$")
_TIMEZONE_PATTERN = re.compile(
    r"^(?:UTC|[A-Za-z][A-Za-z0-9._+-]*(?:/[A-Za-z0-9._+-]+)+)$"
)


class ConfigError(ValueError):
    """Raised when configuration is invalid."""


def parse_duration(text: str) -> timedelta:
    match = _DURATION_PATTERN.match(text)
    if match is None:
        raise ConfigError(f"unsupported duration: {text!r}")
    value, unit = match.groups()
    number = int(value)
    if number <= 0:
        raise ConfigError(f"duration must be positive: {text!r}")
    try:
        return _DURATION_UNITS[unit](number)
    except OverflowError as exc:
        raise ConfigError(f"duration out of range: {text!r}") from exc


def parse_timezone(text: str) -> ZoneInfo:
    # Host zoneinfo databases may expose non-portable aliases such as `utc`
    # or `UTC+1`. Accept the one universal identifier exactly, or the
    # slash-qualified Area/Location form, before asking the host database.
    if not _TIMEZONE_PATTERN.fullmatch(text):
        raise ConfigError(
            f"timezone must be 'UTC' or a slash-qualified IANA identifier: {text!r}"
        )
    try:
        return ZoneInfo(text)
    except Exception as exc:
        raise ConfigError(f"unknown IANA timezone identifier: {text!r}") from exc


def parse_fixed_time(text: str) -> time:
    match = _FIXED_TIME_PATTERN.match(text)
    if match is None:
        raise ConfigError(f"invalid fixed time: {text!r}")
    hour_s, minute_s = match.groups()
    hour = int(hour_s)
    minute = int(minute_s)
    if hour > 23:
        raise ConfigError(f"fixed time hour out of range: {text!r}")
    if minute > 59:
        raise ConfigError(f"fixed time minute out of range: {text!r}")
    return time(hour, minute)


@dataclass(frozen=True)
class PolicyConfig:
    every_commit: bool = False
    max_age: timedelta | None = None
    fixed_time: time | None = None
    timezone: ZoneInfo | None = None
    initial_history: str = "latest"


#: The policy of a repository that configures none: see DEFAULT_MAX_AGE.
DEFAULT_POLICY = PolicyConfig(max_age=DEFAULT_MAX_AGE)


@dataclass(frozen=True)
class GitConfig:
    """Repository-facing execution surface.

    ``require_clean_worktree`` defaults to ``False``. It once defaulted to
    ``True`` because a broad ``git add`` could have swept unrelated edits into
    a generated proof commit; ADR D1 replaced that with an explicit pathspec,
    so the exclusion is now structural and the gate protects nothing it did
    not already protect. What the gate still does is refuse the run, which on
    the scheduled path -- the path this tool exists for -- turns an ordinary
    working day into a silently skipped timestamp. Operators who want a run to
    stop while edits are outstanding opt in explicitly.

    ``signing`` covers only the objects this tool creates -- the timestamp tag
    and the generated proof and upgrade commits. It never asks anything of the
    commits being timestamped: whether those are signed is a property of how
    the repository is worked in, which is not this tool's business. Note that a
    signature over a timestamp tag is not itself timestamped, so unlike a
    signature on a source commit it gets no protection from the anchor.

    Its default is spelled ``inherit`` rather than ``off`` because that is what
    it does: no signing flag is passed, and ambient ``commit.gpgsign`` or
    ``tag.gpgSign`` still signs the objects. ``off`` is reserved for the mode
    that actively suppresses that -- see :data:`_SIGNING_MODES`.
    """

    source_ref: str = "HEAD"
    fetch_before_run: bool = True
    tag_prefix: str = "ots/"
    require_clean_worktree: bool = False
    signing: str = "inherit"

    @property
    def signs_generated_objects(self) -> bool:
        """Whether ``git-ots`` asks Git to sign the objects it creates.

        ``inherit`` returns ``False`` because the tool passes no signing flag,
        not because the object ends up unsigned: ambient ``commit.gpgsign`` or
        ``tag.gpgSign`` still applies and still signs it.
        """
        return self.signing == "required"


@dataclass(frozen=True)
class ProofConfig:
    """Where proof artifacts live, and whether they are committed.

    ``directory`` is repository-relative and never ends in ``/``: every
    consumer joins it with an f-string (``f"{directory}/{commit_id}.ots"``)
    or a ``Path``, so a trailing separator would produce a doubled one in
    the pathspecs handed to Git. :func:`validate_proof_directory` enforces
    that, along with the containment rules that keep a configured value from
    naming anything outside the worktree.
    """

    directory: str = PROOF_DIRECTORY
    commit: bool = True


@dataclass(frozen=True)
class OpenTimestampsConfig:
    command: str = "ots"


@dataclass(frozen=True)
class LimitsConfig:
    """Wall-clock ceilings for production subprocess invocations.

    ``None`` means unbounded and is producible only by an operator supplying
    the literal string ``"0"`` for ``ots.otsTimeout``/``ots.gitTimeout`` --
    see ``gitconfig._parse_timeout``.
    """

    ots_timeout: timedelta | None = timedelta(seconds=120)
    git_timeout: timedelta | None = timedelta(seconds=60)


@dataclass(frozen=True)
class Config:
    """The effective configuration.

    Every field has a default, so ``Config()`` is the configuration of a
    repository that has never been configured. ``git config``'s ``ots.*``
    namespace (see ``gitconfig.assemble_config``) overrides parts of it; an
    unconfigured namespace is not an error.
    """

    policy: PolicyConfig = DEFAULT_POLICY
    git: GitConfig = GitConfig()
    proof: ProofConfig = ProofConfig()
    opentimestamps: OpenTimestampsConfig = OpenTimestampsConfig()
    limits: LimitsConfig = LimitsConfig()


def parse_initial_history(text: str) -> str:
    if text not in ("latest", "all"):
        raise ConfigError(
            f"invalid initial_history: {text!r} (expected 'latest' or 'all')"
        )
    return text


#: Accepted values for ``[git] signing``. ``"off"`` is deliberately absent:
#: FS-0002 specifies it as actively suppressing ambient signing configuration
#: (``--no-gpg-sign``), which is not implemented. Naming today's default
#: ``inherit`` rather than ``off`` keeps that name free for the behaviour it
#: describes, so adding it later is additive instead of a silent change of
#: meaning.
#:
#: ``"required"`` rather than ``"on"`` for the same reason -- it names the part
#: that matters. Asking Git to sign is not the property an operator wants; a
#: signer that fails aborting the operation, rather than yielding an unsigned
#: object, is.
_SIGNING_MODES = ("inherit", "required")


def parse_signing(text: str) -> str:
    if text == "off":
        raise ConfigError(
            "invalid signing: 'off' is not implemented -- it would suppress "
            "ambient Git signing configuration; 'inherit' leaves that "
            "configuration alone"
        )
    if text not in _SIGNING_MODES:
        raise ConfigError(
            f"invalid signing: {text!r} (expected 'inherit' or 'required')"
        )
    return text


#: One path segment of a proof directory. Deliberately narrower than what a
#: filesystem accepts: the value ends up in a Git pathspec, in a timestamp
#: tag's ``proof:`` annotation, and in `git ls-tree` output that `git-ots`
#: parses back, so anything Git would quote or a shell would resplit is
#: rejected rather than escaped at every one of those call sites. A leading
#: ``.`` is allowed -- the default ``.opentimestamps`` starts with one.
_PROOF_DIRECTORY_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_proof_directory(text: str) -> str:
    """Return the normalized repository-relative proof directory, or raise.

    Purely lexical: never touches the filesystem, so it cannot depend on
    which repository happens to be current. Rejects absolute paths, ``..``
    traversal, ``.`` segments, the repository root, empty strings,
    trailing/duplicate separators, ``~`` and backslashes.

    The containment rules are the point. A proof directory names where this
    tool writes files and what pathspec it hands to `git add`; a value that
    escaped the worktree would put both outside the repository the proof is
    supposed to travel with. ``git.py``'s private ``_validate_proof_directory``
    re-checks the two properties its own f-strings depend on (non-empty, no
    trailing ``/``) for callers that construct a ``Config`` directly rather
    than through ``git config``; this is the operator-facing gate.
    """
    if not text:
        raise ConfigError("proof directory must not be empty")
    if "\\" in text:
        raise ConfigError(f"proof directory must use '/' separators: {text!r}")
    if text.startswith("/"):
        raise ConfigError(f"proof directory must be repository-relative: {text!r}")
    if text.endswith("/"):
        raise ConfigError(f"proof directory must not end with '/': {text!r}")
    segments = text.split("/")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise ConfigError(
                f"proof directory must not contain {segment!r} segments: {text!r}"
            )
        if segment.startswith("~"):
            raise ConfigError(f"proof directory must not use '~': {text!r}")
        if not _PROOF_DIRECTORY_SEGMENT_PATTERN.match(segment):
            raise ConfigError(
                f"proof directory segment has disallowed characters: {text!r}"
            )
    return text


_TAG_PREFIX_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_TAG_PREFIX_FORBIDDEN_SUBSTRINGS = ("..", "@{")
_TAG_PREFIX_FORBIDDEN_CHARS = set(" ~^:?*[\\")


def validate_tag_prefix(text: str) -> str:
    """Lexically validate a tag prefix; never invokes ``git``.

    Conservative: aligned with ``git check-ref-format`` rules but entirely
    offline. The prefix must end in ``/`` and each segment must be a safe
    ref component (no ``..``, no ``.lock`` suffix, no control or ref-special
    characters, no leading ``.`` or trailing ``.lock``).
    """
    if not text:
        raise ConfigError("tag prefix must not be empty")
    if not text.endswith("/"):
        raise ConfigError(f"tag prefix must end with '/': {text!r}")
    if text.startswith("/"):
        raise ConfigError(f"tag prefix must not start with '/': {text!r}")
    segments = text[:-1].split("/")
    for segment in segments:
        if not segment:
            raise ConfigError(f"tag prefix must not contain empty segments: {text!r}")
        if ".." in segment:
            raise ConfigError(f"tag prefix segment must not contain '..': {text!r}")
        if "@{" in segment:
            raise ConfigError(f"tag prefix segment must not contain '@{{': {text!r}")
        if segment.endswith(".lock"):
            raise ConfigError(f"tag prefix segment must not end with '.lock': {text!r}")
        for ch in segment:
            if ch in _TAG_PREFIX_FORBIDDEN_CHARS or ord(ch) < 0x20 or ord(ch) == 0x7F:
                raise ConfigError(
                    f"tag prefix segment contains a forbidden character: {text!r}"
                )
        if not _TAG_PREFIX_SEGMENT_PATTERN.match(segment):
            raise ConfigError(f"tag prefix segment has disallowed form: {text!r}")
    return text
