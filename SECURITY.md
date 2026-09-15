# Security policy

## Reporting a vulnerability

Use **Report a vulnerability** on the repository's Security tab so the report
is handled privately. If private vulnerability reporting is unavailable, email
`markus@rotheric.com`. Do not disclose the issue publicly before a fix and
coordinated release are available.

You should receive an initial acknowledgement within seven days. Include the
affected git-ots version, platform, reproduction steps, impact, and any relevant
debug output. Debug tracebacks may contain local paths, branch names, and
filenames; review them before sharing.

## Threat model

An OpenTimestamps proof binds the canonical representation of a Git commit ID
to an attestation incorporated into Bitcoin. It does not prove authorship,
truthfulness of repository content, or exclusive possession of the source at
the claimed time. See `docs/what-a-proof-proves.md` for the complete evidence
model.

An attacker who can rewrite repository history can add, remove, or replace Git
objects and git-ots metadata. They cannot fabricate a valid Bitcoin attestation
for arbitrary content without breaking the underlying cryptography or Bitcoin
history. Fetch proofs and refs through a trusted channel when repository write
access is in question.

`git-ots validate` performs offline structural, binding, lineage, manifest, and
tag-consistency checks. It does not consult Bitcoin. `git-ots verify` delegates
Bitcoin-attestation verification to the OpenTimestamps client and a reachable
Bitcoin node. A successful local validation is not a substitute for chain
verification.

git-ots executes locally configured `git` and `ots` programs. Repository clones
cannot provide executable configuration; nevertheless, operators remain
responsible for the executables and Git configuration installed on their
machine.

Only versions listed in current release notes receive security fixes. This
alpha project does not promise maintenance for superseded `0.x` releases.
