# Security policy

## Supported versions

Sideword Server is pre-1.0. Security fixes are applied only to the latest code on
the `main` branch until stable releases are published.

## Reporting a vulnerability

Please do not disclose suspected vulnerabilities in public issues, discussions,
or pull requests.

Use GitHub's **Report a vulnerability** option in the repository's Security tab
to submit a private security advisory. Include reproduction steps, affected
endpoints or versions, expected impact, and any suggested mitigation.

If private reporting is unavailable, open a minimal issue asking a maintainer
for a private contact channel without including vulnerability details.

You should receive an initial acknowledgement within seven days. Timelines for
validation, remediation, and coordinated disclosure depend on severity and
complexity.

## Scope notes

- The bundled browser client is a testing aid, not an audited end-user client.
- Client applications are responsible for generating keys and encrypting message
  payloads correctly.
- The project has not undergone an independent security audit.
- The test-client v1 protocol has no recipient forward secrecy or post-compromise
  recovery. Local first-use key pinning cannot authenticate an initial key or
  protect against malicious code delivered by the server.
- See the [security review](docs/SECURITY_REVIEW.md) and
  [v1 protocol contract](docs/PROTOCOL.md) for boundaries and remaining risks.
