# Integration Test Report

Date: 2026-08-24

Scope: production-security hardening branch

Environment: local Python 3.12 virtual environment; synthetic payloads; mocked Redis/Meta boundaries; no production credentials or live Meta calls.

## Automated results

| Control | Test evidence | Result |
|---|---|---|
| Invalid Meta HMAC rejected before queue/heartbeat | `test_rejects_invalid_signature_before_queue_or_heartbeat` | PASS |
| Forged webhook ingress limiter runs before HMAC work | invalid-signature lifecycle assertion | PASS |
| Current secret accepted and queue precedes heartbeat | `test_accepts_current_secret_and_durably_queues_before_heartbeat` | PASS |
| Previous Meta secret accepted during rotation | `test_accepts_previous_secret_during_rotation_window` | PASS |
| Duplicate delivery returns success without duplicate acceptance | previous-secret lifecycle fixture | PASS |
| Redis inbox outage returns generic retryable `503` | `test_returns_retryable_response_when_durable_inbox_is_down` | PASS |
| Full webhook inbox returns generic retryable `503` | `test_returns_retryable_response_when_webhook_inbox_is_full` | PASS |
| Oversized body rejected by ASGI limiter | `test_rejects_oversized_body_before_route_processing` | PASS |
| Meta subscription challenge success/failure | `test_meta_verification_lifecycle` | PASS |
| `/ops/*` rejects missing credentials | `test_ops_requires_authentication` | PASS |
| `/ops/*` accepts the configured bearer token | `test_ops_accepts_configured_bearer_token` | PASS |
| Log filter removes bearer/access-token values | `test_redacting_filter_removes_common_secret_forms` | PASS |
| Production rejects public placeholders and weak previous secrets | `test_production_rejects_placeholder_or_weak_rotation_secrets` | PASS (4 cases) |
| Caller-supplied request IDs are replaced at ingress | `test_external_request_id_is_not_copied_to_logs_or_response` | PASS |
| Liveness is dependency-independent | `test_liveness_is_dependency_independent` | PASS |

Command: `python -m pytest -q`

Result: **16 passed**, one third-party Starlette/httpx deprecation warning.

Syntax/import command: `python -m compileall -q app tests scripts`

Result: **PASS**.

Docker/Compose execution was not available in the audit environment (`docker: command not found`). The Dockerfile and Compose policy were statically reviewed, but image build, health checks, non-root runtime, read-only filesystem, Redis AOF recovery, and resource limits remain staging gates.

## Failure-mode qualification

- Redis unavailable during ingress: verified synthetic valid request receives `503` and no internal connection detail.
- PostgreSQL unavailable after durable ingress: source-qualified worker leaves the job in leased/retry state; live Postgres interruption remains a manual deployment test.
- Worker death after claim: source-qualified lease expiry/recovery exists for webhook and outbound jobs; process-kill test against real Redis remains manual.
- Retry exhaustion: bounded attempt counters and terminal DLQ paths are source-qualified; time-accelerated real Redis test remains manual.
- Secret rotation: current/previous Meta verification is automated. Stored account-token re-encryption requires staging database validation with `scripts/rotate_encrypted_tokens.py`.

## Not claimed

This report does not prove live Meta compatibility, exactly-once outbound delivery, multi-replica singleton scheduler safety, production Redis AOF recovery, production PostgreSQL failover, reverse-proxy configuration, or cloud/provider compatibility. Complete the manual runbook checks in staging before production rollout.
