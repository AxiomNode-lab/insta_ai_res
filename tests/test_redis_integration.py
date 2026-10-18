import asyncio
import os

import pytest

from app.core.config import settings
from app.core.redis_utils import (
    QueueBackpressure,
    close_redis_pool,
    check_account_limits,
    enqueue_after_hours_notice,
    enqueue_message,
    enqueue_private_reply,
    stable_fingerprint,
    get_redis_client,
)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_REDIS_INTEGRATION") != "1",
    reason="requires an isolated Redis test database",
)


@pytest.mark.asyncio
async def test_after_hours_notice_is_atomic_and_capacity_bounded(monkeypatch):
    account_id = 991_337
    recipient_id = "synthetic-user"
    keys = [
        f"after_hours_notice:{account_id}:{recipient_id}",
        f"queue:{account_id}",
        f"queue:processing:{account_id}",
        f"queue:retry:{account_id}",
    ]
    client = await get_redis_client()
    monkeypatch.setattr(settings, "OUTBOUND_QUEUE_MAX_DEPTH", 1)

    try:
        await client.delete(*keys)
        await client.srem("active_accounts_set", str(account_id))

        assert await enqueue_after_hours_notice(recipient_id, "closed", account_id)
        assert not await enqueue_after_hours_notice(recipient_id, "closed", account_id)
        assert await client.llen(f"queue:{account_id}") == 1

        other_recipient = "synthetic-user-2"
        with pytest.raises(QueueBackpressure):
            await enqueue_after_hours_notice(other_recipient, "closed", account_id)
        assert not await client.exists(
            f"after_hours_notice:{account_id}:{other_recipient}"
        )
    finally:
        await client.delete(
            *keys,
            f"after_hours_notice:{account_id}:synthetic-user-2",
        )
        await client.srem("active_accounts_set", str(account_id))
        await client.aclose()
        await close_redis_pool()



@pytest.mark.asyncio
async def test_outbound_queue_capacity_is_atomic_under_concurrent_writers(monkeypatch):
    account_id = 991_338
    queue_key = f"queue:{account_id}"
    processing_key = f"queue:processing:{account_id}"
    retry_key = f"queue:retry:{account_id}"
    client = await get_redis_client()
    monkeypatch.setattr(settings, "OUTBOUND_QUEUE_MAX_DEPTH", 1)

    try:
        await client.delete(queue_key, processing_key, retry_key)
        await client.srem("active_accounts_set", str(account_id))

        async def enqueue(index: int) -> bool:
            try:
                await enqueue_message(
                    recipient_id=f"synthetic-recipient-{index}",
                    text="Synthetic queue-capacity test",
                    account_id=account_id,
                )
                return True
            except QueueBackpressure:
                return False

        accepted = await asyncio.gather(*(enqueue(index) for index in range(12)))

        assert sum(accepted) == 1
        assert await client.llen(queue_key) == 1
        assert await client.sismember("active_accounts_set", str(account_id))
    finally:
        await client.delete(queue_key, processing_key, retry_key)
        await client.srem("active_accounts_set", str(account_id))
        await client.aclose()
        await close_redis_pool()



@pytest.mark.asyncio
async def test_private_reply_is_idempotent_and_outbound_queue_is_fifo(monkeypatch):
    account_id = 991_340
    comment_id = "synthetic-comment-991340"
    queue_key = f"queue:{account_id}"
    processing_key = f"queue:processing:{account_id}"
    retry_key = f"queue:retry:{account_id}"
    dedupe_key = f"outbound:dedupe:{stable_fingerprint(f'private-reply:{account_id}:{comment_id}')}"
    client = await get_redis_client()
    monkeypatch.setattr(settings, "OUTBOUND_QUEUE_MAX_DEPTH", 10)

    try:
        await client.delete(queue_key, processing_key, retry_key, dedupe_key)
        await client.srem("active_accounts_set", str(account_id))

        assert await enqueue_private_reply(comment_id, "Synthetic private reply", account_id)
        assert not await enqueue_private_reply(comment_id, "Duplicate synthetic reply", account_id)
        await enqueue_message("fifo-first", "first", account_id)
        await enqueue_message("fifo-second", "second", account_id)

        assert await client.llen(queue_key) == 3
        import json

        delivered_order = [
            json.loads(await client.rpop(queue_key))["recipient_id"]
            for _ in range(3)
        ]
        assert delivered_order == [comment_id, "fifo-first", "fifo-second"]
    finally:
        await client.delete(queue_key, processing_key, retry_key, dedupe_key)
        await client.srem("active_accounts_set", str(account_id))
        await client.aclose()
        await close_redis_pool()


@pytest.mark.asyncio
async def test_account_hourly_and_daily_limits_are_atomic(monkeypatch):
    account_id = 991_339
    keys = [
        f"safe_mode:acc:{account_id}",
        f"quarantine:acc:{account_id}",
        f"limit:acc:{account_id}:hour",
        f"limit:acc:{account_id}:day",
    ]
    client = await get_redis_client()

    try:
        await client.delete(*keys)

        async def try_accept() -> bool:
            return await check_account_limits(
                account_id, max_hour=1, max_day=1, hour_period=3600, day_period=86400
            )

        accepted = await asyncio.gather(*(try_accept() for _ in range(12)))

        assert sum(accepted) == 1
        assert int(await client.get(f"limit:acc:{account_id}:hour")) == 1
        assert int(await client.get(f"limit:acc:{account_id}:day")) == 1

        await client.set(f"safe_mode:acc:{account_id}", "1", ex=60)
        assert not await check_account_limits(
            account_id, max_hour=2, max_day=2
        )
    finally:
        await client.delete(*keys)
        await client.aclose()
        await close_redis_pool()
