# Security Policy

Metron is read-only portfolio analytics. It connects to brokerage accounts through
read-only data connections (broker exports / SnapTrade) — it never places trades, moves
funds, or holds custody. It does, however, read users' real holdings and transaction
history, so we take disclosures seriously.

## Supported versions

Metron is a continuously deployed web application — `main` is the only supported version
(the live site tracks it). There are no tagged releases to back-port fixes to; security
fixes land on `main` and deploy on merge.

## Reporting a vulnerability

Email **[security@nousergon.ai](mailto:security@nousergon.ai)** for code-level
vulnerabilities, credential or secret leaks in commit history, dependency CVEs, or any
issue that could expose a user's brokerage data, tokens, or cross-tenant boundary.

Please **don't** open a public GitHub issue for security-sensitive disclosures — email
the inbox above so we can triage privately. Include enough detail to reproduce (affected
endpoint/page, steps, and impact).

Best-effort acknowledgement within 7 days. We ask for reasonable time to ship and deploy
a fix before public disclosure.

## Scope notes

- **No trade execution / no custody.** Metron reads positions and activity; it cannot
  move money. The highest-severity class is unauthorized access to another user's data
  (a tenant-isolation break) or leakage of broker tokens/secrets.
- **AGPL-3.0-or-later.** The source is public; please report secrets or proprietary
  values that have leaked into the public tree as a vulnerability rather than a bug.
