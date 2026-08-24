# Roadmap

The roadmap favors reliable inbound support over growth-at-any-cost automation.

## Production gate

- Complete a real Meta test-account matrix for private replies, postbacks, duplicate delivery, permissions, and API-version upgrades.
- Implement the Meta user-data deletion callback with an auditable asynchronous deletion lifecycle.
- Add real PostgreSQL migration revisions and upgrade/rollback tests.
- Exercise Redis AOF recovery, PostgreSQL outage recovery, and worker process-kill recovery in staging.

## Product foundation

- Instagram Login OAuth onboarding, disconnect, reauthorization, and token-expiry UX.
- Shared team inbox with assignment, internal notes, collision detection, and SLA state.
- Admin publishing for Ice Breakers, Persistent Menu, Quick Replies, and supported templates.
- Holiday exceptions and multiple intervals for business hours.
- Tenant-scoped analytics for automation resolution, handoff, response time, and conversion.

## Optional grounded AI

- Tenant-controlled knowledge sources and retrieval.
- Confidence thresholds, citations in the operator view, and mandatory human fallback.
- Prompt-injection, PII, retention, and evaluation controls before production enablement.

## Explicitly out of scope

- Unsolicited bulk messaging or broadcast spam.
- Messaging-window or permission bypasses.
- Scraping private data or storing raw customer payloads in logs.
