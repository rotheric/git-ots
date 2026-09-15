"""Tests for signing the objects `git-ots` creates.

`[git] signing` covers the timestamp tag and the generated
proof and upgrade commits -- the objects this tool authors. It deliberately
says nothing about the commits being timestamped: whether those carry
signatures is a property of how the repository is worked in.

The signature-stripping tests matter more than the signing tests. Git stores a
tag signature *inside the tag body*, so it lands in the middle of the spec-12
annotation that `validate_timestamp_tag` parses, and armored signatures may
carry `Version:`/`Comment:` headers that parse as annotation keys. Without
stripping, a signed tag is rejected as malformed, stops counting as a
timestamp baseline, and its source commit is stamped again on the next run.
That failure predates this setting -- `tag.gpgSign = true` in ambient Git
configuration already signs the tags this tool creates -- which is why
stripping is unconditional.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from git_ots.cli import main
from git_ots.git import (
    GitCommandError,
    InvalidRepositoryStateError,
    create_generated_proof_commit,
    create_timestamp_tag,
    enumerate_timestamp_tags,
    strip_signature_block,
    validate_timestamp_tag,
)

# A signer that succeeds, emitting armor headers with colons in them. The
# headers are the point: a signature body is base64 and contains no colon, so a
# fake signer without them would pass a parser that only happens to work by
# accident. Real GnuPG emits `Version:` whenever `--emit-version` is set.
_FAKE_SIGNER = """#!/bin/sh
for a in "$@"; do
  case "$a" in --status-fd=*) fd="${a#--status-fd=}" ;; esac
done
cat > /dev/null
echo "$@" >> "$SIGNER_LOG"
printf -- '-----BEGIN PGP SIGNATURE-----\\n'
printf -- 'Version: GnuPG v2\\n'
printf -- 'Comment: emitted by a test signer\\n'
printf -- '\\n'
printf -- 'ZmFrZXNpZ25hdHVyZWJ5dGVz\\n'
printf -- '-----END PGP SIGNATURE-----\\n'
[ -n "$fd" ] && echo "[GNUPG:] SIG_CREATED " >&"$fd"
exit 0
"""

_FAILING_SIGNER = """#!/bin/sh
cat > /dev/null
echo "$@" >> "$SIGNER_LOG"
echo "signing key unavailable" >&2
exit 1
"""


def _git(args: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return completed.stdout


def _install_signer(repo: Path, script: str, *, log: Path) -> None:
    name = "failing" if script is _FAILING_SIGNER else "working"
    program = repo.parent / f"signer-{name}.sh"
    program.write_text(script)
    program.chmod(0o755)
    _git(["config", "gpg.program", str(program)], cwd=repo)
    _git(["config", "user.signingkey", "TESTKEY"], cwd=repo)
    log.write_text("")


def _init_repo(
    path: Path, *, signer: str | None = None, log: Path | None = None
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main", "."], cwd=path)
    _git(["config", "user.email", "test@example.com"], cwd=path)
    _git(["config", "user.name", "Test"], cwd=path)
    if signer is not None:
        assert log is not None
        _install_signer(path, signer, log=log)


def _commit(repo: Path, *, paths: list[str], message: str) -> str:
    for rel in paths:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{rel}\n")
    _git(["add", *paths], cwd=repo)
    _git(["commit", "-q", "--no-gpg-sign", "-m", message], cwd=repo)
    return _git(["rev-parse", "HEAD"], cwd=repo).strip()


@pytest.fixture
def signer_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Path the fake signer appends its argv to, so invocation is observable."""
    log = tmp_path / "signer.log"
    monkeypatch.setenv("SIGNER_LOG", str(log))
    return log


def _stage_proof(repo: Path, source_id: str) -> list[str]:
    proof_dir = repo / ".opentimestamps"
    proof_dir.mkdir(exist_ok=True)
    (proof_dir / f"{source_id}.ots").write_bytes(b"proof")
    (proof_dir / f"{source_id}.json").write_text("{}")
    paths = [f".opentimestamps/{source_id}.ots", f".opentimestamps/{source_id}.json"]
    _git(["add", *paths], cwd=repo)
    return paths


# --- the stripper itself ----------------------------------------------------


@pytest.mark.parametrize(
    "armor",
    [
        "-----BEGIN PGP SIGNATURE-----",
        "-----BEGIN SSH SIGNATURE-----",
        "-----BEGIN SIGNED MESSAGE-----",
    ],
)
def test_strip_signature_block_removes_every_armor_flavour(armor: str) -> None:
    """gpg.format selects between these three; all must strip.

    Only the PGP spelling is reachable through the fake signer used elsewhere
    in this file, so the other two are asserted directly rather than left to an
    assumption about what `gpg.format = ssh` produces.
    """
    body = f"git-ots schema: 1\nsource: abc\n{armor}\nVersion: x\n\nZm9v\n"
    # The newline terminating the last annotation line is kept: the rendered
    # annotation this is compared against in create_timestamp_tag ends in one.
    assert strip_signature_block(body) == "git-ots schema: 1\nsource: abc\n"


def test_strip_signature_block_leaves_an_unsigned_annotation_alone() -> None:
    body = "git-ots schema: 1\nsource: abc\ntriggers: max_age\n"
    assert strip_signature_block(body) == body


# --- the regression the stripper exists for ---------------------------------


def test_a_signed_tag_still_validates_as_a_timestamp_baseline(
    tmp_path: Path, signer_log: Path
) -> None:
    """The failure that would otherwise re-stamp every commit forever.

    A signed tag's armor headers parse as `key: value` pairs, and
    validate_timestamp_tag rejects any annotation carrying keys outside the
    schema-12 set. A rejected tag is not a baseline, so the source commit reads
    as pending on the next run -- and on every run after that, because the new
    tag is signed too.
    """
    repo = tmp_path / "repo"
    _init_repo(repo, signer=_FAKE_SIGNER, log=signer_log)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")

    tag_name = create_timestamp_tag(
        cwd=repo,
        source_commit_id=source_id,
        tag_prefix="ots/",
        submitted_at=datetime(2026, 8, 8, 22, 0, 3, tzinfo=UTC),
        proof=f".opentimestamps/{source_id}.ots",
        triggers={"max_age"},
        sign=True,
    )

    raw = _git(["cat-file", "tag", tag_name], cwd=repo)
    assert "-----BEGIN PGP SIGNATURE-----" in raw, "test setup: tag was not signed"
    assert "Version: GnuPG v2" in raw, "test setup: signer emitted no armor headers"

    tags = enumerate_timestamp_tags(cwd=repo, tag_prefix="ots/")
    assert [t.name for t in tags] == [tag_name]
    validated = validate_timestamp_tag(tags[0])
    assert validated.source == source_id
    assert validated.triggers == frozenset({"max_age"})


def test_a_tag_signed_by_ambient_config_alone_still_validates(
    tmp_path: Path, signer_log: Path
) -> None:
    """Stripping cannot be conditional on the signing mode.

    `tag.gpgSign = true` in a user's Git configuration signs the tags this tool
    creates whether or not the tool asked for it, so annotations in the wild
    carry signatures no git-ots setting explains. This is the pre-existing bug;
    note sign=False below.
    """
    repo = tmp_path / "repo"
    _init_repo(repo, signer=_FAKE_SIGNER, log=signer_log)
    _git(["config", "tag.gpgSign", "true"], cwd=repo)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")

    tag_name = create_timestamp_tag(
        cwd=repo,
        source_commit_id=source_id,
        tag_prefix="ots/",
        submitted_at=datetime(2026, 8, 8, 22, 0, 3, tzinfo=UTC),
        proof=f".opentimestamps/{source_id}.ots",
        triggers={"max_age"},
        sign=False,
    )

    assert "-----BEGIN PGP SIGNATURE-----" in _git(
        ["cat-file", "tag", tag_name], cwd=repo
    )
    tags = enumerate_timestamp_tags(cwd=repo, tag_prefix="ots/")
    assert validate_timestamp_tag(tags[0]).source == source_id


def test_recreating_an_identical_signed_tag_is_idempotent_not_a_collision(
    tmp_path: Path, signer_log: Path
) -> None:
    """create_timestamp_tag compares a stored annotation to a rendered one.

    The stored side carries a signature and the rendered side never does, so
    without stripping every re-entry into the crash-recovery path -- tag
    already written, proof not yet committed -- would report a false collision.
    """
    repo = tmp_path / "repo"
    _init_repo(repo, signer=_FAKE_SIGNER, log=signer_log)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")
    args = {
        "cwd": repo,
        "source_commit_id": source_id,
        "tag_prefix": "ots/",
        "submitted_at": datetime(2026, 8, 8, 22, 0, 3, tzinfo=UTC),
        "proof": f".opentimestamps/{source_id}.ots",
        "triggers": {"max_age"},
        "sign": True,
    }

    first = create_timestamp_tag(**args)
    second = create_timestamp_tag(**args)
    assert first == second


def test_a_genuine_collision_on_a_signed_tag_is_still_detected(
    tmp_path: Path, signer_log: Path
) -> None:
    """Stripping must not blind the collision check to a real mismatch."""
    repo = tmp_path / "repo"
    _init_repo(repo, signer=_FAKE_SIGNER, log=signer_log)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")
    submitted_at = datetime(2026, 8, 8, 22, 0, 3, tzinfo=UTC)
    create_timestamp_tag(
        cwd=repo,
        source_commit_id=source_id,
        tag_prefix="ots/",
        submitted_at=submitted_at,
        proof=f".opentimestamps/{source_id}.ots",
        triggers={"max_age"},
        sign=True,
    )

    with pytest.raises(InvalidRepositoryStateError, match="collision"):
        create_timestamp_tag(
            cwd=repo,
            source_commit_id=source_id,
            tag_prefix="ots/",
            submitted_at=submitted_at,
            proof=f".opentimestamps/{source_id}.ots",
            triggers={"fixed_time"},  # different annotation, same tag name
            sign=True,
        )


# --- the setting doing what it says -----------------------------------------


def test_sign_produces_a_signed_proof_commit(tmp_path: Path, signer_log: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, signer=_FAKE_SIGNER, log=signer_log)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")
    paths = _stage_proof(repo, source_id)

    commit_id = create_generated_proof_commit(
        cwd=repo,
        source_commit_ids=[source_id],
        proof_directory=".opentimestamps",
        paths=paths,
        sign=True,
    )

    assert "gpgsig" in _git(["cat-file", "commit", commit_id], cwd=repo)
    # The signature is a header, so the trailer classification is undisturbed.
    body = _git(["log", "-1", "--format=%B", commit_id], cwd=repo)
    assert "OpenTimestamps-Generated: true" in body
    assert "PGP SIGNATURE" not in body


def test_the_default_signs_nothing_and_never_invokes_a_signer(
    tmp_path: Path, signer_log: Path
) -> None:
    """Off must mean the signing program is not run at all.

    Asserting the object is unsigned would not catch a call that ran and was
    ignored; on a host where the signer blocks on a passphrase prompt, that
    distinction is the difference between a working cron job and a hung one.
    """
    repo = tmp_path / "repo"
    _init_repo(repo, signer=_FAKE_SIGNER, log=signer_log)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")
    paths = _stage_proof(repo, source_id)

    tag_name = create_timestamp_tag(
        cwd=repo,
        source_commit_id=source_id,
        tag_prefix="ots/",
        submitted_at=datetime(2026, 8, 8, 22, 0, 3, tzinfo=UTC),
        proof=f".opentimestamps/{source_id}.ots",
        triggers={"max_age"},
    )
    commit_id = create_generated_proof_commit(
        cwd=repo,
        source_commit_ids=[source_id],
        proof_directory=".opentimestamps",
        paths=paths,
    )

    assert "PGP SIGNATURE" not in _git(["cat-file", "tag", tag_name], cwd=repo)
    assert "gpgsig" not in _git(["cat-file", "commit", commit_id], cwd=repo)
    assert signer_log.read_text() == "", "signing program was invoked with signing off"


def test_a_failing_signer_fails_the_tag_rather_than_leaving_it_unsigned(
    tmp_path: Path, signer_log: Path
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, signer=_FAILING_SIGNER, log=signer_log)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")

    # `GitCommandError`, not `InvalidRepositoryStateError`: a signing failure
    # keeps Git's own failure classification so the CLI reports exit 6, per
    # FS-0002 criterion 4. It is not a claim about the repository's state --
    # the repository is fine, the signer is not. The proof-commit sibling test
    # below has always expected `GitCommandError` for the identical scenario;
    # the tag path disagreeing meant one failing signer produced two different
    # exit codes depending on which object it struck.
    with pytest.raises(GitCommandError) as excinfo:
        create_timestamp_tag(
            cwd=repo,
            source_commit_id=source_id,
            tag_prefix="ots/",
            submitted_at=datetime(2026, 8, 8, 22, 0, 3, tzinfo=UTC),
            proof=f".opentimestamps/{source_id}.ots",
            triggers={"max_age"},
            sign=True,
        )

    # The signer's own stderr survives, so the operator is told *why* signing
    # failed rather than being handed a generic tag-creation message
    # (FS-0002 criterion 4: "the underlying git/gpg stderr preserved").
    assert "signing key unavailable" in excinfo.value.stderr
    assert enumerate_timestamp_tags(cwd=repo, tag_prefix="ots/") == ()


def test_a_failing_signer_exits_6_through_the_cli(
    tmp_path: Path,
    signer_log: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """FS-0002 acceptance criterion 6, at the CLI contract rather than at the
    exception type: a run whose signer exits non-zero exits **6**, with the
    signer's stderr in the reported failure.

    The sibling tests above assert which exception `create_timestamp_tag`
    raises. That is necessary but not sufficient -- it was possible for those
    to pass while the CLI still reported exit 3, which is exactly the defect
    this test now pins. The exception raised into `main` here is a *real* one,
    produced by really running `git tag -s` against a really failing signer,
    not a hand-constructed stand-in.
    """
    repo = tmp_path / "repo"
    _init_repo(repo, signer=_FAILING_SIGNER, log=signer_log)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")

    try:
        create_timestamp_tag(
            cwd=repo,
            source_commit_id=source_id,
            tag_prefix="ots/",
            submitted_at=datetime(2026, 8, 8, 22, 0, 3, tzinfo=UTC),
            proof=f".opentimestamps/{source_id}.ots",
            triggers={"max_age"},
            sign=True,
        )
    except GitCommandError as exc:
        real_signing_failure = exc
    else:  # pragma: no cover - the signer is rigged to fail
        pytest.fail("the failing signer did not fail the tag")

    def _raise(**_kwargs: object) -> None:
        raise real_signing_failure

    monkeypatch.setattr("git_ots.cli.run_orchestration", _raise)

    returned = main(
        argv=["run"],
        now=datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC),
        cwd=repo,
    )

    assert returned == 6, (
        "a signing failure is a Git failure (exit 6), not repository state (exit 3)"
    )
    assert "signing key unavailable" in capsys.readouterr().err


def test_a_failing_signer_fails_the_proof_commit_rather_than_leaving_it_unsigned(
    tmp_path: Path, signer_log: Path
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, signer=_FAILING_SIGNER, log=signer_log)
    source_id = _commit(repo, paths=["a.txt"], message="A\n")
    head_before = _git(["rev-parse", "HEAD"], cwd=repo).strip()
    paths = _stage_proof(repo, source_id)

    with pytest.raises(GitCommandError):
        create_generated_proof_commit(
            cwd=repo,
            source_commit_ids=[source_id],
            proof_directory=".opentimestamps",
            paths=paths,
            sign=True,
        )

    assert _git(["rev-parse", "HEAD"], cwd=repo).strip() == head_before
