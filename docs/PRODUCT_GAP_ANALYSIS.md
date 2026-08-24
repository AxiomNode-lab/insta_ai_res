# Product gap analysis

## What this project does today

IG Reply Desk is an inbound Instagram support automation service. Meta sends signed webhook deliveries to FastAPI; the service durably queues them in Redis, resolves the tenant account, stores conversations in PostgreSQL, matches exact/keyword/fallback rules, and queues policy-checked replies. Telegram is the operator interface for rules, account text, human takeover, and live-chat notifications.

Despite the repository name, the current reply engine is deterministic. It normalizes Arabic and uses configured rules and safety heuristics; it does not call an LLM, retrieval system, or generative-AI provider.

## Comparison with Meta-native capabilities and established tools

| Capability | Before this branch | This branch | Remaining work |
|---|---:|---:|---|
| Direct-message keyword and exact rules | Yes | Yes | Visual flow builder and rule versioning |
| Comment-to-private-reply | Incorrectly addressed as a normal user DM | Meta `comment_id` private reply | End-to-end sandbox test with a real Meta test account |
| Ice Breaker / Persistent Menu postbacks | Ignored when no `message.text` existed | Postback and quick-reply payload ingestion | Admin publishing UI for menu/ice-breaker configuration |
| Business hours | No | Per-account IANA timezone, weekday/overnight schedule, cooldown notice | Holiday exceptions and multiple daily intervals |
| Human handoff | Telegram notification and human mode | Same | Shared web inbox, assignment, internal notes, SLA views |
| Onboarding | Database/operator provisioned | Same | Instagram Login OAuth and self-service account connection |
| Analytics | Basic daily stats and activity events | Adds after-hours status/event | Funnel, attribution, agent, and automation conversion reporting |
| AI | Rule engine only | Same | Optional grounded AI with tenant knowledge, confidence threshold, and human fallback |
| Compliance callbacks | Privacy/terms pages and retention cleanup | Same | Meta user-data deletion callback and auditable deletion job |
| Delivery UX | Text messages | Text plus postback inputs | Button/generic templates, sender actions, and supported attachments |
| Meta API lifecycle | Hard-coded expired Graph API `v19.0` | Explicit configurable `v26.0` default | Quarterly compatibility review and sandbox upgrade gate |
| Engineering delivery | Local integration tests | CI, 35 local tests, isolated Redis atomicity test, production-image build | PostgreSQL failover/process-kill tests and Meta sandbox smoke test |

## Why these additions were selected

Meta documents Private Replies as a single message whose recipient contains the comment ID. Addressing the commenter as an ordinary DM both misrepresents the platform contract and incorrectly opens the service's local 24-hour window. This branch uses the comment ID and keeps ordinary DM window enforcement separate.

Meta's Ice Breakers, Persistent Menu, and Quick Replies all create structured selections/postbacks. Mapping their title or payload into the existing reply engine unlocks those native entry points without adding an unsafe broadcast system.

Business-hours handling is a common support-inbox primitive. It prevents automation from pretending that a human team is available, stores the inbound message, alerts the team, and limits the customer notice to one per six-hour window.

Meta's version schedule shows Graph API `v19.0` expired on 2026-05-21, while `v26.0` is the current release. The version is now explicit and validated through `META_GRAPH_API_VERSION`, so upgrades are deliberate and visible instead of hidden in a service constant.

## Recommended roadmap

1. **P0 — Meta sandbox verification:** connect a dedicated test Instagram professional account and verify comment private reply, postback, message echo, retry, and duplicate delivery behavior.
2. **P0 — Data deletion callback:** implement signed-request parsing, account/user lookup, asynchronous deletion, status URL, audit record, and deletion SLA monitoring.
3. **P1 — OAuth onboarding:** Instagram Login, least-privilege scopes, token expiry monitoring, reauthorization, and account disconnect.
4. **P1 — Shared team inbox:** assignment, collaborators, internal notes, collision detection, status/SLA, and tenant-scoped search.
5. **P1 — Migrations and supply chain:** real Alembic revisions/rollback tests, image scanning, SBOM, and signed release images.
6. **P2 — Grounded AI:** tenant-controlled knowledge sources, citations in agent view, confidence threshold, prompt-injection defenses, PII controls, evaluation set, and mandatory human fallback for sensitive intents.
7. **P2 — Native message configuration:** publish Ice Breakers/Persistent Menu and support button/generic templates through an admin surface.

Broadcasts and unsolicited outbound automation are intentionally not recommended. The product should remain user-initiated and policy-constrained.

## Primary references

- Meta, [Instagram Messaging API](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/messaging-api)
- Meta, [Private Replies](https://developers.facebook.com/documentation/instagram-platform/private-replies)
- Meta, [Ice Breakers](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/messaging-api/ice-breakers)
- Meta, [Persistent Menu](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/messaging-api/persistent-menu)
- Meta, [Quick Replies](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/messaging-api/quick-replies)
- Meta, [Data deletion callback](https://developers.facebook.com/documentation/development/create-an-app/app-dashboard/data-deletion-callback)
- Meta, [Graph API versions](https://developers.facebook.com/docs/graph-api/changelog/versions/)
- ManyChat, [Key Instagram automation features](https://manychat.com/blog/key-instagram-automation-features/)
- respond.io, [Collaborating with your team in Inbox](https://respond.io/help/inbox/collaborating-with-your-team-in-inbox)
