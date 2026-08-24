# Operations Runbook - IG Reply Desk

## Meta-native feature configuration

### Comment private replies

Use Telegram's **رد خاص للتعليقات** menu to map a comment keyword to one private reply. The worker addresses the request to Meta's `comment_id`; it does not treat the public comment as permission to open an ordinary 24-hour DM window.

Before enabling in production, subscribe the Meta app to comment events and run the manual sandbox sequence in `docs/META_NATIVE_TEST_REPORT.md`. A duplicate webhook for the same comment must not generate another reply. Do not replay a comment dead-letter blindly if operators already observed a successful Meta delivery.

### Postbacks

Subscribe the Meta app to the webhook fields required for messages and messaging postbacks. Quick Reply payloads and Ice Breaker/Persistent Menu postbacks are mapped into the same account-scoped exact/keyword rule engine. Keep configured payloads stable and non-sensitive; payload values may appear in stored conversation content and rule diagnostics.

### Business hours

From Telegram choose **ساعات العمل**. Configure an IANA timezone and a schedule such as:

```text
Europe/Istanbul 0,1,2,3,4 09:00 18:00
```

Days use `0=Monday` through `6=Sunday`. A close time earlier than the open time is treated as an overnight shift. Outside the schedule, the inbound message is stored, the team is notified, and the customer receives at most one notice every six hours. Disabling the setting restores continuous automation.

During incident response, business hours are not a global kill switch. Use the existing account lockdown or global kill switch when outbound delivery must stop.

### Meta Graph API upgrades

The deployment pins `META_GRAPH_API_VERSION` (currently `v26.0`). At least quarterly, compare it with Meta's official version schedule. Before changing it:

1. run all local tests;
2. deploy the candidate version to a Meta test app/account;
3. verify message send, comment private reply, postback, permission check, token refresh, and webhook lifecycle;
4. roll out gradually and watch Meta error codes, retries, and dead letters;
5. keep the previous deploy image available for rollback, but never roll back to a sunset API version.

This runbook is written for real daily use.
Follow it in order, escalate early, and avoid risky manual fixes during incidents.

## Morning checks (about 5 minutes)

1. Call `/ops/status` with `Authorization: Bearer $OPS_API_TOKEN` and verify:
- `system_status = OPERATIONAL`
- `workers_alive > 0`
- `total_queue_backlog < 50`

2. Review Telegram admin alerts:
- Treat `SILENCE DETECTED` and `CRITICAL` as immediate action items.

3. Run `/setup_check` in the bot:
- Confirm tokens and baseline integration health.

## Live monitoring

### Alert meaning

- `SILENCE DETECTED`: workers may be stalled or not consuming jobs.
- `QUEUE BACKLOG`: incoming workload is exceeding processing capacity.
- `HEARTBEAT ALERT`: webhook delivery or connectivity problem.

### First actions

- Queue above 1000:
    check `/health_report`; if a single account is causing load, run `/lock_account {id}`.
- Incoming events > 0 but outgoing replies = 0:
    restart worker services and re-check `/ops/status`.

## Incident response playbook

### A) Full outage (Meta issue or internal failure)

1. Pause processing:
- `/pause_all`

2. Investigate quickly:
- review app logs
- check Meta platform status

3. Resume only when stable:
- `/resume_all`

### B) Noisy account (loop, spam, bad rules)

1. Identify the account from alerts.
2. Isolate it:
- `/lock_account {id}`

3. Inspect failed/blocked items:
- `/deadletters {id}`

4. Recover after root cause is fixed:
- `/unlock_account {id}`

### C) No outgoing replies

1. Confirm incoming rate in `/ops/status`.
2. If incoming exists and outgoing is zero:
- restart workers immediately.

## Security response checklist

Run this checklist if you suspect token leak, abuse, or unauthorized access.

1. Pause processing: `/pause_all`.
2. Rotate Meta and Telegram tokens.
3. Revoke old credentials and active sessions.
4. Audit logs for unusual sources, spikes, or command misuse.
5. Resume only after verification checks pass.

Never paste a secret, webhook payload, sender ID, message body, Redis job, or database URL into tickets or chat. Record request IDs, opaque job IDs, error classes, timestamps, and aggregate counts only.

## Deployment procedure

1. Build an immutable image from the reviewed commit. Do not mount the source tree into the production container.
2. Supply secrets from the deployment secret store; `ENV=production` must be enabled.
3. Apply reviewed database migrations before starting the new application. Keep `DB_AUTO_CREATE_SCHEMA=false`.
4. Validate `docker compose -f docker-compose.prod.yml config`; confirm Redis AOF, `noeviction`, authentication, internal data network, read-only app filesystem, dropped capabilities, and resource limits remain present.
5. Start one application instance. The Telegram bot and schedulers are currently embedded; multiple replicas require separating singleton roles first.
6. Wait for `/health/ready` to return `200`, then send the synthetic signed webhook smoke test from `tests/test_webhook_lifecycle.py`.
7. Confirm `/ops/status` is `401` without a token and `200` with the active operator token.
8. Monitor queue depth, retries, dead letters, dependency errors, and Meta response codes for at least 15 minutes.

Rollback the image if readiness or security checks fail. Do not roll back secrets that have already been revoked. Keep new and previous application secrets configured while rolling application versions across a rotation window.

## PostgreSQL outage

- Liveness should remain `200`; readiness must return `503` with only coarse component status.
- Signed deliveries already accepted in Redis remain leased/retryable. Processing failures move to exponential backoff and then the webhook DLQ after `WEBHOOK_MAX_ATTEMPTS`.
- Restore PostgreSQL, verify `SELECT 1`, then watch `webhook:retry`, `webhook:processing`, and queue depth fall.
- Do not delete deduplication or processing keys. Expired leases are reclaimed automatically.

## Redis outage or storage pressure

- New signed deliveries return `503` plus `Retry-After`, causing Meta to retry instead of losing events.
- Liveness remains available and readiness becomes `503`.
- Restore the same persistent Redis volume. Verify AOF health, authentication, free memory, and `maxmemory-policy noeviction` before resuming traffic.
- Keep `WEBHOOK_QUEUE_MAX_DEPTH` below the capacity represented by Redis `REDIS_MAXMEMORY`; a full inbox intentionally returns `503` so Meta retries instead of losing work.
- Apply a host/provider quota and free-space alert to the `redis_data` AOF volume; Compose cannot declare a portable volume-size quota.
- If Redis data is unrecoverable, pause outbound processing and reconcile deliveries with Meta before clearing state; do not claim exactly-once recovery.

## Queue retry and dead letters

- Webhook and outbound jobs use leases. A worker crash leaves a leased item that is reclaimed after `WEBHOOK_PROCESSING_LEASE_SECONDS`.
- Retries use capped exponential backoff. Terminal webhook jobs are stored in `webhook:dead-letter`; terminal outbound jobs in `dead_letter:{account_id}`.
- Dead letters are not replayed automatically. Diagnose the error class, confirm the underlying fault is corrected, obtain operator approval, and replay a bounded set by immutable job ID.
- Never export full DLQ payloads into logs or support tickets because they contain message data.

## Secret rotation

### Meta app secret

1. Set the old value as `META_APP_SECRET_PREVIOUS` and the new value as `META_APP_SECRET`.
2. Deploy and verify signatures made with both values.
3. Update Meta to the new secret and observe successful deliveries.
4. After the maximum delivery/retry window, clear `META_APP_SECRET_PREVIOUS` and redeploy.

### Operations token

1. Set the old value as `OPS_API_TOKEN_PREVIOUS` and the new value as `OPS_API_TOKEN`.
2. Deploy, update every authorized client, and verify the new bearer token.
3. Remove `OPS_API_TOKEN_PREVIOUS` promptly and redeploy.

### Account-token encryption key

1. Set the old key as `SECRET_KEY_PREVIOUS` and a new random key as `SECRET_KEY`.
2. Back up PostgreSQL, deploy, and run `python -m scripts.rotate_encrypted_tokens` once.
3. Verify a sample of accounts can send and refresh tokens without logging token values.
4. Clear `SECRET_KEY_PREVIOUS` only after every stored token has been re-encrypted and the rollback window closes.

### Meta, Telegram, Redis, and PostgreSQL credentials

Use the provider/database native create-new → deploy → verify → revoke-old sequence. PostgreSQL and Redis URL changes make readiness fail until both the service and app are updated. Never retain the example values in production.

## End-of-day checks

1. Run `/stats` and review reply quality (human vs auto ratio).
2. Inspect a sample of dead letters with `/deadletters {id}`.
3. Confirm `global_kill_switch` is not left enabled.

## Weekly maintenance

1. Confirm retention cleanup jobs completed.
2. Verify token rotation and expiration windows.
3. Review processing latency trend and scale workers when needed.

## Feature flow: comment keyword to private DM

### Setup

1. In Telegram, open `💬 رد خاص للتعليقات`.
2. Add keyword and DM text.
3. Optionally update account copy in `📝 تخصيص نصوص الحساب`.

### Event requirements

- Webhook payload includes `entry[].changes[]`.
- `field` equals `comments`.
- Supported media types: `VIDEO`, `REELS`, `IGTV`.

### Local payload example

```json
{
    "object": "instagram",
    "entry": [
        {
            "id": "YOUR_INSTAGRAM_PAGE_ID",
            "time": 1710000000,
            "changes": [
                {
                    "field": "comments",
                    "value": {
                        "id": "17890000000000001",
                        "text": "Can I get the price?",
                        "from": {
                            "id": "17840000000000001",
                            "username": "test_user"
                        },
                        "media": {
                            "id": "17910000000000001",
                            "media_product_type": "VIDEO"
                        }
                    }
                }
            ]
        }
    ]
}
```

### Expected result

1. Comment matches an active keyword rule.
2. DM job is queued in `queue:{account_id}`.
3. Worker sends DM through Graph API.
4. Activity log records `COMMENT_DM_TRIGGERED`.

### Negative checks

1. Media type is `IMAGE` -> no DM is sent.
2. No keyword match -> no DM is sent.
3. Duplicate comment ID -> event is ignored by deduplication.
