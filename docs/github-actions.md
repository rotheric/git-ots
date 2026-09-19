# GitHub Actions examples

This repository uses `git-ots` in two workflows: one submits release commits
for timestamping, and one later upgrades the stored proofs. They are working
examples of using the tool in CI, with different triggers and the same proof
store on `master`.

| Workflow | Trigger | Command | Result |
|---|---|---|---|
| [Release](../.github/workflows/release.yml) | Version bump pushed to `master`; manual recovery | `git ots run` | Missing timestamps, proof commits, timestamp tags, then a version tag and publication |
| [Upgrade timestamp proofs](../.github/workflows/upgrade-proofs.yml) | Hourly at minute 43 UTC; manual run | `git ots upgrade` | Updated existing proofs, committed and pushed only when something changed |

## Credentials and checkout

Both workflows check out `master` with full history (`fetch-depth: 0`) when
writing proofs. This provides the source commits and timestamp tags used for
proof binding and deduplication, and gives Git a branch on which to commit.
They configure a bot name and email for generated commits.

For this repository, create a fine-grained PAT with these settings:

| Setting | Value |
|---|---|
| Token name | `git-ots-release-push` |
| Resource owner | `rotheric` |
| Repository access | Only select repositories: `git-ots` |
| Contents | Read and write |
| Workflows | Read and write |
| Metadata | Read-only, automatically included |
| Expiration | Choose a renewal date and replace the secret before it expires |
| Other permissions | Leave unset |

Save its value under **Settings → Secrets and variables → Actions → New
repository secret**, named exactly `RELEASE_PUSH_TOKEN`. Both workflows reuse
this secret; no second upgrade token is needed. Its owner must be permitted to
push directly under the branch rules. This repository's `Protect master`
ruleset permits repository administrators to bypass the pull-request and
status-check requirements for generated proof commits.

**Workflows write permission is necessary even for a timestamp-tag push** when
the referenced history contains changes to `.github/workflows/`. A classic PAT
instead needs the `workflow` scope alongside its repository access. A token
that only has Contents write access can fail with “refusing to allow a
Personal Access Token to create or update workflow”. Add the workflow
permission and rerun the failed job; replace the Actions secret if the token
value changed. The token does not need Secrets permission at runtime; that
permission is only needed to administer the secret through the API.

The built-in `GITHUB_TOKEN` remains read-only for the upgrade workflow; checkout
uses the PAT for Git pushes. In a repository whose branch rules permit it, you
can instead use the built-in token with `contents: write` and omit the custom
checkout token. Unlike PAT pushes, pushes using `GITHUB_TOKEN` generally do not
trigger other workflows; see GitHub's
[workflow trigger documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

## Example 1: timestamp a release before publishing

The release workflow watches `src/git_ots/__init__.py` on `master`. Its
[selection helper](../.github/scripts/release_target.py) compares `__version__`
before and after the push, accepts a strictly increasing `MAJOR.MINOR.PATCH`,
and freezes the pushed commit ID as the release target. An edit without a
version change does not release anything. Make only one version bump per push.

It tests the source, checks out current `master`, and performs the equivalent
of these commands (with `RELEASE_TARGET` set to the selected commit):

```bash
git config ots.sourceRef "$RELEASE_TARGET"
git config ots.everyCommit true
git config ots.fetchBeforeRun false
uv run --frozen --with opentimestamps-client==0.7.2 git ots run
```

`everyCommit` makes submission immediate and can also timestamp older pending
meaningful commits since the last baseline. Setting `sourceRef` prevents a
later push from changing the release target. `git ots run` stores the `.ots`
proof and JSON manifest, creates a proof commit, and tags the timestamped
source. It does not upgrade previous proofs.

The workflow confirms that the target's proof and manifest are committed,
creates the annotated version tag on the original source commit, and pushes
the proof commits and tags atomically. Build and publication jobs depend on
that successful push. Initial calendar receipts are sufficient for this
release gate; it does not wait for Bitcoin confirmation.

For normal releases, update the version and changelog, then push or merge to
`master`. Do not push a version tag separately. For recovery of an existing
tag, use **Actions → Release → Run workflow**, select `master`, and enter the
tag (for example `v0.0.2`) as `release_tag`. See the
[release procedure](../RELEASING.md) for publication setup and retry details.

## Example 2: upgrade all stored proofs on a schedule

The upgrade workflow runs hourly and can also be started through **Actions →
Upgrade timestamp proofs → Run workflow**. Merge the workflow onto the default
branch to enable its schedule. It checks out fresh `master`, installs the
pinned tool environment with `uv`, and runs:

```bash
git config ots.proofCommit true
git config ots.squashUpgradeCommits false
before="$(git rev-parse HEAD)"
uv run --frozen --with opentimestamps-client==0.7.2 git ots upgrade
if test "$(git rev-parse HEAD)" != "$before"; then
  git push origin HEAD:refs/heads/master
fi
```

The command scans the whole `.opentimestamps` store, including proofs from
previous releases. It requests missing calendar attestations and commits
proofs that gain evidence. It continues checking outstanding calendar promises
even if another calendar already supplied a Bitcoin attestation. It neither
timestamps new source commits nor creates new release tags.

Pending proofs may remain unchanged until calendars have more evidence. With
no updates, including an empty or already complete proof store, HEAD stays the
same and the workflow does not push. Squashing is explicitly off because each
upgrade commit is published immediately. No force push or history rewrite is
needed. The workflow has a 20-minute timeout.

Upgrading gathers evidence; it does not independently verify the Bitcoin
anchor against your node. Use `git ots verify` with Bitcoin Core for that
separate check, as described in the
[proof lifecycle guide](proof-lifecycle.md).

GitHub schedules are best-effort and run on the default branch. Runs can be
delayed, and public-repository schedules can be disabled after 60 days without
repository activity. Minute 43 avoids the start-of-hour load peak. See
[GitHub's schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Why the workflows do not cascade

The upgrade workflow has only schedule and manual triggers, never a push
trigger. The release workflow accepts only pushes to `master` touching the
version file and then checks the version value. Generated proof commits touch
the proof store, so they cannot start another release. Neither workflow starts
from timestamp-tag or version-tag pushes. PAT pushes may start the ordinary
verification workflow, which does not write back to the repository.

Each workflow serializes its own runs with `cancel-in-progress: false`. They
use different concurrency groups so scheduled upgrades cannot replace a
pending release. They can still overlap with each other or with a user push.
Both use ordinary fast-forward pushes: if `master` advances, the stale push
fails instead of overwriting it. Rerun a failed release job; for an upgrade,
rerun the workflow or wait for the next hourly run, which checks out current
`master` and asks the calendars again. No automatic rebase or force push is
performed. If the upgrade command fails or skips an invalid proof, the shell
stops before pushing; inspect its log and the
[operations guide](operations.md).

## Adapting these examples to another repository

Copy the linked workflow files and, for automatic version detection, the
release selection helper. Change the branch name, version-file path and parser,
repository-specific publication jobs, and credential setup to match your
project. Preserve the distinction between source commits and proof commits,
and keep upgrade pushes out of the release trigger.

These workflows use `uv run --frozen` because this repository contains the
`git-ots` source and its `uv.lock`. In a different project, install a chosen
released version of `git-ots` and `opentimestamps-client==0.7.2` as tools, then
invoke `git ots run` or `git ots upgrade` in that project's checkout. Keep
full history, the Git author identity, explicit policy configuration, and
the guarded push steps. Follow your branch rules when choosing between a
direct bot push and a pull request for proof updates.
