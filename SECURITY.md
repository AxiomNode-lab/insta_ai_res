# Security Policy

## Supported branch

Security fixes target the default branch and the latest tagged release. Do not submit production credentials, customer message bodies, webhook payloads, or Redis/PostgreSQL exports in an issue.

## Reporting

Report suspected vulnerabilities privately to the repository owner through GitHub's private vulnerability reporting when enabled. Include affected commit, route/component, attacker prerequisites, sanitized reproduction steps, and impact. Use synthetic identifiers and secrets.

## Security invariants

- No webhook-derived side effect occurs before Meta HMAC verification.
- A `200` webhook response means Redis durably accepted the delivery or already holds its deduplication state.
- Queue items remain leased/recoverable until acknowledged or terminally dead-lettered.
- Tenant administrators can access only their account; platform-wide controls require `ADMIN_IDS` membership.
- `/ops/*` fails closed without a valid operator token.
- Logs never contain tokens, raw webhook/message bodies, stable customer identifiers, or upstream response bodies.
- Production rejects documented placeholder/short secrets and uses authenticated isolated data services.

The service provides at-least-once processing. Exactly-once external Meta sends are not claimed.
