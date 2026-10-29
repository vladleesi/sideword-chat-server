# Security policy

Security fixes target the latest `main` code; Sideword is pre-1.0.

## Report a vulnerability

Use GitHub **Security → Report a vulnerability**. Include affected versions,
reproduction and impact. Keep findings out of public issues/PRs. If private reporting
is unavailable, request a private contact channel without disclosing details.
Initial acknowledgement is expected within seven days; resolution depends on severity.

## Security limits

The browser client is unaudited. V1 has no recipient forward secrecy or post-compromise
recovery; first-use pins cannot authenticate initial keys or malicious served code.
Read the [security review](docs/SECURITY_REVIEW.md) and [protocol](docs/PROTOCOL.md).
