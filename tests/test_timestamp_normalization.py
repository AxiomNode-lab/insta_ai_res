import time
from unittest.mock import AsyncMock

import pytest

from app.core import redis_utils


def test_normalizes_seconds_and_milliseconds_to_epoch_seconds():
    now = int(time.time())

    assert redis_utils.normalize_timestamp_seconds(now - 30, now=now) == now - 30
    assert redis_utils.normalize_timestamp_seconds(
        (now - 30) * 1000, now=now
    ) == now - 30
    assert redis_utils.normalize_timestamp_seconds(
        str((now - 30) * 1000), now=now
    ) == now - 30


@pytest.mark.parametrize(
    "timestamp",
    [None, True, False, "", "not-a-time", 0, -1, float("inf"), float("nan")],
)
def test_rejects_invalid_timestamps(timestamp):
    assert redis_utils.normalize_timestamp_seconds(timestamp) is None


def test_rejects_implausibly_future_timestamp():
    now = int(time.time())
    assert redis_utils.normalize_timestamp_seconds(now + 301, now=now) is None
    assert redis_utils.normalize_timestamp_seconds(
        (now + 301) * 1000, now=now
    ) is None


@pytest.mark.asyncio
async def test_window_check_normalizes_legacy_millisecond_value(monkeypatch):
    now = int(time.time())
    client = AsyncMock()
    client.get = AsyncMock(return_value=str((now - 23 * 60 * 60) * 1000))
    monkeypatch.setattr(
        redis_utils, "get_redis_client", AsyncMock(return_value=client)
    )

    assert await redis_utils.is_within_24h_window(
        42, "synthetic-user", current_timestamp=now * 1000
    )


@pytest.mark.asyncio
async def test_window_check_rejects_expired_and_future_interactions(monkeypatch):
    now = int(time.time())
    client = AsyncMock()
    monkeypatch.setattr(
        redis_utils, "get_redis_client", AsyncMock(return_value=client)
    )

    client.get.return_value = str(now - 86_401)
    assert not await redis_utils.is_within_24h_window(42, "synthetic-user", now)

    client.get.return_value = str(now + 3_600)
    assert not await redis_utils.is_within_24h_window(42, "synthetic-user", now)

    client.get.return_value = None
    assert not await redis_utils.is_within_24h_window(42, "synthetic-user", now)


@pytest.mark.asyncio
async def test_update_last_interaction_stores_seconds_not_milliseconds(monkeypatch):
    now = int(time.time())
    client = AsyncMock()
    monkeypatch.setattr(
        redis_utils, "get_redis_client", AsyncMock(return_value=client)
    )

    await redis_utils.update_last_interaction(
        42, "synthetic-user", timestamp=(now - 15) * 1000
    )

    client.set.assert_awaited_once_with(
        "last_interaction:42:synthetic-user", now - 15
    )


@pytest.mark.asyncio
async def test_invalid_future_timestamp_does_not_replace_last_interaction(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(
        redis_utils, "get_redis_client", AsyncMock(return_value=client)
    )

    await redis_utils.update_last_interaction(
        42, "synthetic-user", timestamp=int(time.time()) + 3_600
    )

    client.set.assert_not_awaited()
