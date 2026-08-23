# Threat Model

## Overview

IG Reply Desk is a multi-tenant webhook and message-processing service. It receives Internet-originated Meta webhook requests, looks up tenant state in PostgreSQL, stores delivery/coordination state in Redis, sends replies through Meta Graph API, and exposes operator workflows through Telegram and an authenticated HTTP operations route.

The security-critical assets are Meta, Telegram, Redis, PostgreSQL, and operator credentials; encrypted per-account access tokens; customer identifiers and message bodies; tenant configuration; webhook and outbound queue integrity; idempotency state; audit records; and platform-wide pause/lock controls.

## Threat Model, Trust Boundaries, and Assumptions

- Internet → reverse proxy → FastAPI is untrusted. Request bodies, headers, source addresses, JSON structure, Meta challenge parameters, message text, comment text, IDs, and event timing are attacker-influenced.
- Meta authenticity is established only by `X-Hub-Signature-256` verification over the exact bounded body. Possession of a valid Meta app secret is trusted within its rotation window.
- FastAPI → Redis is a privileged boundary. Redis holds raw webhook jobs, message jobs, leases, retries, DLQs, limits, and global safety flags. Redis must require authentication, persistence, `noeviction`, and network isolation.
- FastAPI/workers → PostgreSQL is a privileged boundary. PostgreSQL holds tenants, encrypted account tokens, messages, rules, operators, and audits. The application database role and backup path are trusted and must be least-privilege.
- Workers → Meta Graph API is an external-effect boundary. Local acknowledgement follows a positive Meta response, but a crash between the external effect and local acknowledgement leaves an at-least-once duplicate risk.
- Telegram → admin handlers is an authenticated but not universally privileged boundary. Database-backed OWNER/MANAGER users are tenant-scoped. Only IDs configured in `ADMIN_IDS` are platform administrators allowed to invoke global controls or cross-account diagnostics.
- Operators with `/ops/*` access are trusted to view aggregate platform health, not raw message bodies or credentials. Bearer tokens are high-value secrets.
- Application logs and monitoring backends are lower-trust operational stores. They may contain request IDs, opaque identity hashes, counts, status codes, and exception classes only.
- Repository developers and CI are trusted to review code and synthetic fixtures. Tests must not use production payloads or credentials.

The model assumes TLS termination and edge request limits at a trusted reverse proxy; correct Meta secret provisioning; a private authenticated data network; controlled host/container administration; and a single embedded scheduler/Telegram role. Adding multiple app replicas without separating singleton roles is outside the qualified deployment design.

## Attack Surface, Mitigations, and Attacker Stories

### Public HTTP ingress

Relevant attacks include oversized/chunked bodies, signature forgery, replay, malformed JSON, request floods, forged heartbeat state, detailed error probing, and queue exhaustion. Controls are streamed body limiting, a pre-authentication ingress work limiter, current/previous HMAC validation before business processing, a separate post-authentication limiter, bounded local fallback, atomic delivery deduplication, a hard webhook backlog ceiling, generic error responses, and privacy-safe request logs.

An invalid signature must never update trusted heartbeat or queue state. If Redis cannot durably accept a valid delivery, the service returns `503` so Meta can retry. Identical accepted bodies are suppressed for the configured deduplication window.

The application backlog ceiling is sized below the reference Redis memory budget. Reaching either capacity fails closed with a retryable `503`; persistent-volume quotas and alerts remain deployment responsibilities.

### Queue and dependency failures

Relevant failures include worker death after claim, PostgreSQL/Redis outages, poison jobs, permanent Meta errors, retry storms, stale locks, and DLQ disclosure. Webhook and outbound queues use ready, leased-processing, delayed-retry, and terminal-dead states. Claims expire, retries are bounded with capped exponential backoff, and terminal jobs require explicit operator replay. Redis AOF limits acknowledged-delivery loss, but does not make Redis a substitute for tested backups.

At-least-once external effects remain a residual risk: a worker can crash after Meta accepts a send but before Redis acknowledgement. A future Meta-supported idempotency/reconciliation identifier should be persisted when available.

### Tenant and operator controls

Relevant attacks include tenant OWNER users invoking global actions, caller-supplied cross-account IDs, unscoped Redis conversation keys, forged callback data, leaked ops tokens, and expensive operational scans. Controls include explicit platform-admin checks, account ownership verification before state reads/writes, account-namespaced conversation keys, router-level ops authentication, constant-time token comparison, bounded Redis SCAN, and dual-token rotation windows.

### Secrets, privacy, and rendering

Relevant attacks include placeholder credentials, secrets in exceptions, response-body logging, PII copied to logs, HTML injection into Telegram alerts, long-lived audit copies, and unsafe rotation. Controls include production startup validation, JSON logging with redaction, opaque identifier hashes, error-code-only upstream logging, HTML escaping, and documented current/previous secret rotation. Stored customer data and ActivityEvent retention still require deployment-specific legal policy and periodic verification.

### Container and adjacent services

Relevant attacks include application code execution escalating through root, writable source persistence, build tools in runtime, adjacent containers mutating Redis, known database passwords, and resource exhaustion. The production reference uses a multi-stage non-root image, read-only filesystem, tmpfs, dropped capabilities, no-new-privileges, resource limits, authenticated persistent Redis, required PostgreSQL password, and an internal data network.

## Severity Calibration (Critical, High, Medium, Low)

- Critical: remotely forgeable webhooks using a committed/known production app secret leading to cross-tenant outbound messages; unauthenticated arbitrary code execution in the public service; exposure of usable Meta/Telegram/database credentials with immediate broad compromise.
- High: tenant-to-tenant control or message disclosure; valid webhook acknowledgement without durable acceptance causing broad permanent loss; adjacent workload ability to inject tenant queue jobs; systemic loss of outbound messages after destructive claims.
- Medium: unauthenticated operations telemetry, bounded denial of service, retry amplification, Telegram markup injection, stable customer identifiers in logs, weak container isolation, or an ownerless conversation lock causing duplicates/out-of-order sends.
- Low: coarse internal topology leakage without credentials, limited diagnostic metadata exposure, or defense-in-depth gaps requiring prior privileged access and producing low impact.

Self-only behavior, platform-admin actions that do not cross an additional boundary, local development placeholders under `ENV=development`, and test-only synthetic credentials are not reportable production vulnerabilities. Claims about exactly-once delivery, multi-replica singleton safety, or hardware/cloud compatibility are explicitly out of scope unless separately tested.
