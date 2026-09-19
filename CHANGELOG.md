# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.0.2] - 2026-09-19

### Added

- `ots.squashUpgradeCommits` (default `false`): when set, `git ots upgrade`
  amends the previous upgrade commit instead of stacking a new one on top.
  Declined, with an ordinary commit made instead, unless that commit's message
  is exactly the one this tool writes, it touches only the proof directory, it
  has one parent, it would not lose a signature, and it is not reachable from
  any remote-tracking ref. Folding discards the per-refresh chronology; the
  proofs and their Bitcoin anchors are unaffected. `git ots upgrade --dry-run`
  reports whether the fold would happen, and `--verbose` explains a decline
  caused by a message the tool does not recognize as its own.

## [0.0.1] - 2026-09-15

### Added

- Initial alpha release of policy-driven Git commit timestamping with
  OpenTimestamps.
- Offline proof validation, Bitcoin-backed verification, proof upgrades, and
  interrupted-run repair.
- Git-config-based operation, generated proof commits, and immutable timestamp
  tags.

[Unreleased]: https://github.com/rotheric/git-ots/compare/v0.0.2...HEAD
[0.0.2]: https://github.com/rotheric/git-ots/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/rotheric/git-ots/releases/tag/v0.0.1
