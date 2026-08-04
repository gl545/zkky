# Security Policy

## Supported version

Security fixes are applied to the latest revision on the `main` branch.

## Reporting a vulnerability

Use the repository host's private security-advisory feature. Include:

- affected revision and environment;
- minimal offline reproduction;
- expected and observed behavior;
- impact and suggested remediation;
- confirmation that the report contains no live credentials or payment data.

Do not open a public issue containing access tokens, cookies, proxy credentials,
email addresses, billing details, card data, PaymentMethod identifiers,
client secrets, telemetry, attestation values, HAR captures, or unredacted logs.

## Security properties

- The HTTP service binds to `127.0.0.1` by default.
- Runtime account and proxy input is held in memory or browser localStorage.
- The local configuration and fingerprint cache are excluded from Git.
- Mutating payment requests are not automatically replayed after an ambiguous
  transport failure.
- A successful HTTP response is not treated as proof of subscription activation;
  final account state is checked separately.

Changing any of these properties requires tests and a clear explanation in the
pull request.
