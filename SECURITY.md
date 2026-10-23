# Security policy

## Supported versions

Sideword is pre-1.0. Security fixes target the latest `main` code only.

## Report a vulnerability

Use **Security → Report a vulnerability** for a private GitHub advisory. Include
reproduction steps, affected endpoints/versions, impact and suggested mitigations.
Do not publish details in issues, discussions or pull requests. If private reporting
is unavailable, ask for a private contact channel without including the findings.

Initial acknowledgement is expected within seven days; remediation and coordinated
disclosure depend on severity and complexity.

## Security limits

The bundled browser client is an unaudited testing aid. Clients must encrypt and
verify peers correctly; the server relays opaque ciphertext. The v1 protocol has
no recipient forward secrecy or post-compromise recovery. First-use pinning cannot
authenticate an initial key or protect against malicious origin code.

See the [security review](docs/SECURITY_REVIEW.md) for controls, open findings and
verification limits, and the [protocol](docs/PROTOCOL.md) for the wire contract.
