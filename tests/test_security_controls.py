import logging
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app.core.config import Settings, settings
from app.core.observability import RedactingFilter
from app.main import app


client = TestClient(app)


def production_settings(**overrides):
    values = settings.model_dump()
    values.update(
        {
            "ENV": "production",
            "SECRET_KEY": "s" * 48,
            "META_APP_SECRET": "m" * 32,
            "META_VERIFY_TOKEN": "v" * 32,
            "OPS_API_TOKEN": "o" * 48,
            "TELEGRAM_BOT_TOKEN": "t" * 48,
            "INSTAGRAM_ACCESS_TOKEN": "i" * 48,
            "META_APP_SECRET_PREVIOUS": None,
            "OPS_API_TOKEN_PREVIOUS": None,
            "SECRET_KEY_PREVIOUS": None,
        }
    )
    values.update(overrides)
    return Settings(**values)


def test_ops_requires_authentication():
    response = client.get("/ops/status")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_ops_accepts_configured_bearer_token():
    class FakeRedis:
        async def exists(self, _key):
            return 0

        async def scan_iter(self, match, count):
            if match == "worker:*":
                yield "worker:pool_0"
            elif match == "queue:*":
                yield "queue:1"

        async def llen(self, _key):
            return 2

    with (
        patch("app.routers.ops.get_redis_client", AsyncMock(return_value=FakeRedis())),
        patch(
            "app.routers.ops.get_silence_metrics",
            AsyncMock(return_value={"incoming": 10, "outgoing": 5}),
        ),
        patch(
            "app.routers.ops.get_webhook_queue_metrics",
            AsyncMock(
                return_value={"queued": 0, "processing": 0, "retrying": 0, "dead_letter": 0}
            ),
        ),
    ):
        response = client.get(
            "/ops/status", headers={"Authorization": "Bearer ops-test-token"}
        )

    assert response.status_code == 200
    assert response.json()["total_queue_backlog"] == 2


def test_redacting_filter_removes_common_secret_forms():
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="authorization=Bearer abc123 access_token=secret-value",
        args=(),
        exc_info=None,
    )
    assert RedactingFilter().filter(record)
    rendered = record.getMessage()
    assert "abc123" not in rendered
    assert "secret-value" not in rendered
    assert "[REDACTED]" in rendered


@pytest.mark.parametrize(
    "field,value",
    [
        ("OPS_API_TOKEN", "replace-with-32-plus-random-characters"),
        ("SECRET_KEY", "replace-with-32-plus-random-characters"),
        ("META_APP_SECRET_PREVIOUS", "x"),
        ("OPS_API_TOKEN_PREVIOUS", "x"),
    ],
)
def test_production_rejects_placeholder_or_weak_rotation_secrets(field, value):
    with pytest.raises(ValidationError):
        production_settings(**{field: value})


def test_external_request_id_is_not_copied_to_logs_or_response():
    supplied = "customer-123-access-token-secret"
    response = client.get("/health/live", headers={"X-Request-ID": supplied})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != supplied


def test_liveness_is_dependency_independent():
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_meta_graph_api_version_is_explicit_and_validated():
    configured = production_settings(META_GRAPH_API_VERSION="v26.0")
    assert configured.META_GRAPH_API_VERSION == "v26.0"

    with pytest.raises(ValidationError):
        production_settings(META_GRAPH_API_VERSION="latest")
