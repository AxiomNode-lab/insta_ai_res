import os

import pytest

from app.core.config import settings
from app.core.redis_utils import (
    QueueBackpressure,
    close_redis_pool,
    enqueue_after_hours_notice,
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
