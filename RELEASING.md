# Releasing git-ots

Releases follow Semantic Versioning. During the `0.x` series, every breaking
change must be called out explicitly in `CHANGELOG.md`.

1. Update `src/git_ots/__init__.py` and move the pending changelog entries from
   `Unreleased` into a dated version section.
2. Run `make check`, `make build`, and inspect both distributions.
3. Commit the release change normally, run `git ots` so that commit has a stored
   proof and `ots/...` tag, and push both the source and generated proof commit.
4. Create and push an annotated version tag such as `v0.0.1` on the release
   commit. Do not use the `ots/...` namespace for version tags.
5. The release workflow verifies that the tag matches the package version and
   that `master` stores a proof for the tagged commit. It then builds once,
   publishes checksums and provenance attestations, creates the GitHub Release,
   and publishes the identical wheel and source distribution to PyPI.

Before the first PyPI release, configure the `pypi` GitHub environment as a
Trusted Publisher for the `release.yml` workflow. No long-lived PyPI token is
used.
