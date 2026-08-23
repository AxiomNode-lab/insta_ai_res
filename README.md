# IG Reply Desk

FastAPI service for receiving signed Meta/Instagram webhooks and scheduling tenant-scoped replies through recoverable Redis queues.

## Security and reliability guarantees

- `X-Hub-Signature-256` is verified over the bounded raw body before heartbeat, JSON parsing, or durable queue acceptance.
- Current and previous Meta secrets are accepted during a bounded rotation window.
- A valid delivery returns `200` only after atomic Redis inbox + deduplication acceptance. Redis failure returns retryable `503`.
- Webhook and outbound workers use processing leases, capped exponential backoff, crash recovery, and terminal dead letters.
- `/ops/*` requires an operator bearer token. Tenant Telegram admins are kept within their account; platform-global actions require an ID in `ADMIN_IDS`.
- Logs use JSON, opaque user identifiers, request correlation IDs, and credential redaction. Webhook/message bodies are not logged.
- `/health/live` tests the process only; `/health/ready` checks PostgreSQL and Redis without returning connection details.

See [the threat model](docs/THREAT_MODEL.md), [integration-test report](docs/INTEGRATION_TEST_REPORT.md), and [operations runbook](RUNBOOK.md).

## Request lifecycle

1. A pre-authentication ingress limiter bounds forged-request work, and the ASGI body limiter rejects bodies over `MAX_WEBHOOK_BODY_BYTES` while streaming.
2. The route verifies the Meta HMAC, then applies a separate valid-delivery rate limit.
3. A SHA-256 delivery identity and payload are atomically inserted into the capacity-bounded Redis inbox if unseen.
4. The inbox worker claims the delivery with a lease and processes tenant events.
5. Event-level claims are marked complete only after processing; failures release the claim and schedule the delivery for retry.
6. Outbound jobs are leased until Meta delivery succeeds. Terminal failures remain in `dead_letter:{account_id}` for explicit operator handling.

Redis must use AOF persistence and `noeviction`; the production Compose reference configures both. This design is at-least-once. A crash after Meta accepts an outbound send but before local acknowledgement can still produce a duplicate; consumers and operators should reconcile with Meta delivery identifiers where available.

## Local development

Requires Python 3.12, PostgreSQL, and authenticated Redis.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
pytest -q
uvicorn app.main:app --reload
```

Use `ENV=development` locally. `ENV=production` rejects short or documented placeholder credentials. Schema auto-creation is disabled by default; use `DB_AUTO_CREATE_SCHEMA=true` only for an empty local development database, never as the production migration strategy.

## Production container

Generate independent random values for PostgreSQL, Redis, application encryption, Meta verification, and ops authentication. Use a URL-safe random Redis password because the reference injects it into `REDIS_URL`. Then:

```bash
docker compose -f docker-compose.prod.yml config
docker compose -f docker-compose.prod.yml build --pull
docker compose -f docker-compose.prod.yml up -d
```

The reference binds the app to loopback. Terminate TLS at a trusted reverse proxy, preserve the source address, apply a matching edge body/rate limit, and expose only the application port. Do not add untrusted workloads to the internal `data` network.

## Endpoints

| Endpoint | Access | Purpose |
|---|---|---|
| `GET /health/live` | probe | Process liveness only |
| `GET /health/ready` | probe | Coarse PostgreSQL/Redis readiness |
| `GET /instagram/webhook` | public, limited | Meta subscription challenge |
| `POST /instagram/webhook` | signed, limited | Durable webhook acceptance |
| `GET /ops/status` | operator bearer token | Queue/worker status |
| `GET /terms`, `GET /privacy` | public, limited | Legal pages |

Authenticate operations with `Authorization: Bearer $OPS_API_TOKEN`. `X-Ops-Token` remains supported for systems that cannot set bearer authorization.

## Tests

```bash
python -m compileall -q app tests
pytest -q
```

Webhook lifecycle tests use synthetic local payloads and mocked Meta/storage boundaries. They do not contact Meta or use production credentials.

## Responsible use

Operate only accounts you are authorized to manage and follow Meta platform policies. This project does not provide bulk messaging, authentication bypass, or policy circumvention.
