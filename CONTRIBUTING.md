# Contributing

Thank you for helping improve git-ots.

## Workflow

1. Open an issue for behavioral changes or security-sensitive design work.
2. Add or update tests before changing behavior.
3. Keep `specs/spec.md` synchronized with normative behavior. It is the single
   specification; do not add separate feature specifications.
4. Record a rationale in the relevant spec section when a choice affects proof
   meaning, compatibility, security boundaries, or persistent formats.
5. Run `make check` before proposing a change. Run `make test-integration` when
   changing interaction with the OpenTimestamps client or calendars.

Documentation-consistency tests intentionally bind user-facing claims in the
README and specification to implemented behavior. Update documentation and its
corresponding assertions together.

Pull requests should be focused, explain the motivation and compatibility
impact, and include automated tests where possible. For requirements that
cannot be automated, add or update the manual verification protocol in
`specs/spec.md`.

Do not report vulnerabilities in public issues; follow `SECURITY.md`.
