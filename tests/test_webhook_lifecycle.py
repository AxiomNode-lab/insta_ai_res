import hashlib
import hmac
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import settings
from app.main import app
from app.services.webhook_queue import WebhookQueueCapacityError, WebhookQueueResult


client = TestClient(app)
PAYLOAD = b'{"entry":[{"id":"page-test-id","messaging":[]}],"object":"instagram"}'


def signature(secret: str, body: bytes = PAYLOAD) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_rejects_invalid_signature_before_queue_or_heartbeat():
    enqueue = AsyncMock()
    redis = AsyncMock()
    ingress_limit = AsyncMock()
    with (
        patch("app.api.webhook.webhook_ingress_rate_limit", ingress_limit),
        patch("app.api.webhook.enqueue_verified_webhook", enqueue),
        patch("app.api.webhook.get_redis_client", AsyncMock(return_value=redis)),
    ):
        response = client.post(
            "/instagram/webhook",
            content=PAYLOAD,
            headers={"X-Hub-Signature-256": "sha256=invalid"},
        )

    assert response.status_code == 403
    ingress_limit.assert_awaited_once()
    enqueue.assert_not_awaited()
    redis.set.assert_not_awaited()


def test_accepts_current_secret_and_durably_queues_before_heartbeat():
    calls = []

    async def enqueue(*_args):
        calls.append("enqueue")
        return WebhookQueueResult(accepted=True, duplicate=False)

    redis = AsyncMock()

    async def heartbeat(*_args, **_kwargs):
        calls.append("heartbeat")

    redis.set.side_effect = heartbeat
    with (
        patch("app.api.webhook.webhook_ingress_rate_limit", AsyncMock()),
        patch("app.api.webhook.enqueue_verified_webhook", enqueue),
        patch("app.api.webhook.get_redis_client", AsyncMock(return_value=redis)),
        patch("app.api.webhook.webhook_rate_limit", AsyncMock()),
    ):
        response = client.post(
            "/instagram/webhook",
            content=PAYLOAD,
            headers={"X-Hub-Signature-256": signature(settings.META_APP_SECRET)},
        )

    assert response.status_code == 200
    assert response.headers["X-Webhook-Duplicate"] == "0"
    assert calls == ["enqueue", "heartbeat"]


def test_accepts_previous_secret_during_rotation_window():
    redis = AsyncMock()
    with (
        patch("app.api.webhook.webhook_ingress_rate_limit", AsyncMock()),
        patch(
            "app.api.webhook.enqueue_verified_webhook",
            AsyncMock(return_value=WebhookQueueResult(accepted=False, duplicate=True)),
        ),
        patch("app.api.webhook.get_redis_client", AsyncMock(return_value=redis)),
        patch("app.api.webhook.webhook_rate_limit", AsyncMock()),
    ):
        response = client.post(
            "/instagram/webhook",
            content=PAYLOAD,
            headers={"X-Hub-Signature-256": signature(settings.META_APP_SECRET_PREVIOUS)},
        )

    assert response.status_code == 200
    assert response.headers["X-Webhook-Duplicate"] == "1"


def test_returns_retryable_response_when_durable_inbox_is_down():
    with (
        patch("app.api.webhook.webhook_ingress_rate_limit", AsyncMock()),
        patch(
            "app.api.webhook.enqueue_verified_webhook",
            AsyncMock(side_effect=RedisConnectionError("private connection details")),
        ),
        patch("app.api.webhook.webhook_rate_limit", AsyncMock()),
    ):
        response = client.post(
            "/instagram/webhook",
            content=PAYLOAD,
            headers={"X-Hub-Signature-256": signature(settings.META_APP_SECRET)},
        )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert "private connection details" not in response.text


def test_returns_retryable_response_when_webhook_inbox_is_full():
    with (
        patch("app.api.webhook.webhook_ingress_rate_limit", AsyncMock()),
        patch(
            "app.api.webhook.enqueue_verified_webhook",
            AsyncMock(side_effect=WebhookQueueCapacityError("webhook inbox capacity reached")),
        ),
        patch("app.api.webhook.webhook_rate_limit", AsyncMock()),
    ):
        response = client.post(
            "/instagram/webhook",
            content=PAYLOAD,
            headers={"X-Hub-Signature-256": signature(settings.META_APP_SECRET)},
        )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert "capacity" not in response.text


def test_rejects_oversized_body_before_route_processing():
    body = b"x" * (settings.MAX_WEBHOOK_BODY_BYTES + 1)
    with patch("app.api.webhook.webhook_ingress_rate_limit", AsyncMock()):
        response = client.post(
            "/instagram/webhook",
            content=body,
            headers={"X-Hub-Signature-256": signature(settings.META_APP_SECRET, body)},
        )
    assert response.status_code == 413


def test_meta_verification_lifecycle():
    with patch("app.api.webhook.public_rate_limit", AsyncMock()):
        ok = client.get(
            "/instagram/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": settings.META_VERIFY_TOKEN,
                "hub.challenge": "challenge-value",
            },
        )
        denied = client.get(
            "/instagram/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong",
                "hub.challenge": "challenge-value",
            },
        )

    assert ok.status_code == 200
    assert ok.text == "challenge-value"
    assert denied.status_code == 403
