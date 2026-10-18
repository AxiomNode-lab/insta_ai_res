from unittest.mock import AsyncMock

import pytest

from app.core import redis_utils
from app.services import instagram_service


class FakeMetaResponse:
    status_code = 200

    def __init__(self, invalid_json: bool):
        self.invalid_json = invalid_json

    def raise_for_status(self):
        return None

    def json(self):
        if self.invalid_json:
            raise ValueError("synthetic non-JSON response")
        return {"message_id": "synthetic-message-id"}


class FakeMetaClient:
    def __init__(self, invalid_json: bool, calls: list[dict]):
        self.response = FakeMetaResponse(invalid_json)
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, *, params, json):
        self.calls.append({"url": url, "params": params, "payload": json})
        return self.response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metrics_fail", "invalid_json"),
    [(True, False), (False, True)],
)
async def test_successful_meta_send_is_not_failed_by_telemetry_or_response_parsing(
    monkeypatch, metrics_fail, invalid_json
):
    calls = []
    metrics = (
        AsyncMock(side_effect=RuntimeError("synthetic Redis telemetry outage"))
        if metrics_fail
        else AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        instagram_service.httpx,
        "AsyncClient",
        lambda: FakeMetaClient(invalid_json, calls),
    )
    monkeypatch.setattr(
        instagram_service,
        "is_within_24h_window",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        instagram_service,
        "generate_appsecret_proof",
        lambda _token: "synthetic-proof",
    )
    monkeypatch.setattr(redis_utils, "track_outgoing_message", metrics)

    result = await instagram_service.send_instagram_message_api(
        account_id=42,
        recipient_id="synthetic-recipient",
        text="A synthetic test reply",
        access_token="synthetic-access-token",
    )

    assert len(calls) == 1
    assert calls[0]["payload"]["recipient"] == {"id": "synthetic-recipient"}
    assert result == ({} if invalid_json else {"message_id": "synthetic-message-id"})
    metrics.assert_awaited_once_with(1)
