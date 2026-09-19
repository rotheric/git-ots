# Releasing git-ots

Releases follow Semantic Versioning. During the `0.x` series, every breaking
change must be called out explicitly in `CHANGELOG.md`.

1. Update `src/git_ots/__init__.py` and move the pending changelog entries from
   `Unreleased` into a dated version section.
2. Run `make check`, `make build`, and inspect both distributions.
3. Merge or push the version bump to `master`. Use a strictly increasing
   `MAJOR.MINOR.PATCH` version. The workflow compares versions at the beginning
   and end of the push, so make only one release bump per push.
4. The release workflow tests the pushed source commit, timestamps it, pushes
   the generated proof commit and `ots/...` tags, and creates the annotated
   `v<version>` tag on the source commit. Do not create the version tag manually.
5. The workflow verifies that the tag matches the package version and
   that `master` stores a proof for the tagged commit. It then builds once,
   publishes checksums and provenance attestations, creates the GitHub Release,
   and publishes the identical wheel and source distribution to PyPI.

## One-time setup

Create the repository Actions secret `RELEASE_PUSH_TOKEN` with a fine-grained
PAT restricted to `rotheric/git-ots`, with Contents read/write access. Its owner
must be allowed to bypass the `Protect master` ruleset: the generated proof
commit is pushed directly. The current ruleset already permits repository
administrators to bypass it. The built-in Actions token is used for other jobs.

On PyPI, configure the trusted publisher for `git-ots` with owner `rotheric`,
repository `git-ots`, workflow `release.yml`, and environment `pypi`. No
long-lived PyPI token is used. The GitHub environment alone does not register
the publisher with PyPI.

## Cascade prevention and recovery

The workflow listens only to pushes on `master` that change the version file,
and then checks that the version actually increased. It does not listen to tag
pushes. A PAT push can trigger workflows, but generated proof commits do not
touch the version file; they can trigger normal verification, never another
release. All publishing jobs belong to the same release workflow.

Release runs share one concurrency group and never cancel an active release.
GitHub can replace a pending run when another arrives, so wait for a release to
finish before pushing another version bump.

Proofs, timestamp tags, and the version tag are pushed atomically, without
force. If `master` changes during timestamping, the push fails safely; rerun the
failed job to timestamp against the updated branch. Existing tags must point
at the same source commit. Retries preserve existing GitHub Releases and skip
already uploaded PyPI distributions.

For a failed release whose version tag already exists (including `v0.0.2`),
open **Actions → Release → Run workflow**, select `master`, and enter that tag
in `release_tag`. This runs the new workflow against the tagged source commit
and stores its proof on current `master`. Rerunning an old workflow run uses
its old workflow definition, so use this manual entry point after migrating.
Initial calendar proofs satisfy the release check; Bitcoin confirmation and
proof upgrading happen later.
