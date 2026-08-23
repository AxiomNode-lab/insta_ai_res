import asyncio
import json
import logging
import time
from dataclasses import dataclass

from redis.exceptions import RedisError

from app.core.config import settings
from app.core.redis_utils import get_redis_client


logger = logging.getLogger(__name__)

QUEUE_KEY = "webhook:queue"
PROCESSING_KEY = "webhook:processing"
RETRY_KEY = "webhook:retry"
DEAD_LETTER_KEY = "webhook:dead-letter"

_ENQUEUE_SCRIPT = """
local dedupe = KEYS[1]
local queue = KEYS[2]
if redis.call('EXISTS', dedupe) == 1 then
  return 0
end
local depth = redis.call('LLEN', queue) + redis.call('ZCARD', KEYS[3]) + redis.call('ZCARD', KEYS[4])
if depth >= tonumber(ARGV[3]) then
  return -1
end
redis.call('SET', dedupe, 'queued', 'EX', ARGV[1])
redis.call('LPUSH', queue, ARGV[2])
return 1
"""

_CLAIM_SCRIPT = """
local raw = redis.call('RPOP', KEYS[1])
if not raw then
  return false
end
redis.call('ZADD', KEYS[2], ARGV[1], raw)
return raw
"""

_MOVE_DUE_SCRIPT = """
local members = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
for _, raw in ipairs(members) do
  if redis.call('ZREM', KEYS[1], raw) == 1 then
    redis.call('LPUSH', KEYS[2], raw)
  end
end
return #members
"""


@dataclass(frozen=True)
class WebhookQueueResult:
    accepted: bool
    duplicate: bool


class WebhookQueueCapacityError(RedisError):
    pass


def _dedupe_key(delivery_id: str) -> str:
    return f"webhook:dedupe:{delivery_id}"


async def enqueue_verified_webhook(delivery_id: str, payload: dict) -> WebhookQueueResult:
    client = await get_redis_client()
    raw = json.dumps(
        {
            "delivery_id": delivery_id,
            "payload": payload,
            "attempt": 0,
            "queued_at": time.time(),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    inserted = await client.eval(
        _ENQUEUE_SCRIPT,
        4,
        _dedupe_key(delivery_id),
        QUEUE_KEY,
        PROCESSING_KEY,
        RETRY_KEY,
        settings.WEBHOOK_DEDUP_TTL_SECONDS,
        raw,
        settings.WEBHOOK_QUEUE_MAX_DEPTH,
    )
    if int(inserted) < 0:
        raise WebhookQueueCapacityError("webhook inbox capacity reached")
    return WebhookQueueResult(accepted=bool(inserted), duplicate=not bool(inserted))


async def _move_due(source: str, now: float, batch_size: int = 100) -> int:
    client = await get_redis_client()
    return int(await client.eval(_MOVE_DUE_SCRIPT, 2, source, QUEUE_KEY, now, batch_size))


async def recover_expired_webhooks() -> int:
    now = time.time()
    recovered = await _move_due(PROCESSING_KEY, now)
    recovered += await _move_due(RETRY_KEY, now)
    return recovered


async def claim_webhook() -> str | None:
    client = await get_redis_client()
    lease_until = time.time() + settings.WEBHOOK_PROCESSING_LEASE_SECONDS
    return await client.eval(_CLAIM_SCRIPT, 2, QUEUE_KEY, PROCESSING_KEY, lease_until)


async def _mark_complete(raw: str, delivery_id: str) -> None:
    client = await get_redis_client()
    pipe = client.pipeline(transaction=True)
    pipe.zrem(PROCESSING_KEY, raw)
    pipe.set(
        _dedupe_key(delivery_id),
        "complete",
        ex=settings.WEBHOOK_DEDUP_TTL_SECONDS,
    )
    await pipe.execute()


async def _mark_failed(
    raw: str,
    job: dict,
    error_type: str,
    *,
    count_attempt: bool = True,
    retry_delay: int | None = None,
) -> None:
    client = await get_redis_client()
    attempt = int(job.get("attempt", 0)) + (1 if count_attempt else 0)
    job["attempt"] = attempt
    job["last_error_type"] = error_type
    job["last_failed_at"] = time.time()
    updated = json.dumps(job, separators=(",", ":"), sort_keys=True)

    pipe = client.pipeline(transaction=True)
    pipe.zrem(PROCESSING_KEY, raw)
    if attempt >= settings.WEBHOOK_MAX_ATTEMPTS:
        pipe.lpush(DEAD_LETTER_KEY, updated)
        pipe.ltrim(DEAD_LETTER_KEY, 0, 999)
        pipe.set(
            _dedupe_key(job["delivery_id"]),
            "dead-letter",
            ex=settings.WEBHOOK_DEDUP_TTL_SECONDS,
        )
    else:
        delay = retry_delay or min(
            settings.WEBHOOK_RETRY_BASE_SECONDS * (2 ** max(attempt - 1, 0)), 300
        )
        pipe.zadd(RETRY_KEY, {updated: time.time() + delay})
        pipe.set(
            _dedupe_key(job["delivery_id"]),
            f"retry:{attempt}",
            ex=settings.WEBHOOK_DEDUP_TTL_SECONDS,
        )
    await pipe.execute()


async def process_one_webhook() -> bool:
    raw = await claim_webhook()
    if not raw:
        return False

    try:
        job = json.loads(raw)
        delivery_id = str(job["delivery_id"])
        payload = job["payload"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        client = await get_redis_client()
        pipe = client.pipeline(transaction=True)
        pipe.zrem(PROCESSING_KEY, raw)
        pipe.lpush(
            DEAD_LETTER_KEY,
            json.dumps(
                {"last_error_type": type(exc).__name__, "dead_lettered_at": time.time()},
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        pipe.ltrim(DEAD_LETTER_KEY, 0, 999)
        await pipe.execute()
        logger.error("Discarded malformed webhook job error_type=%s", type(exc).__name__)
        return True

    try:
        from app.services.instagram_service import process_webhook_payload

        await process_webhook_payload(payload)
    except Exception as exc:
        event_busy = type(exc).__name__ == "EventAlreadyProcessing"
        await _mark_failed(
            raw,
            job,
            type(exc).__name__,
            count_attempt=not event_busy,
            retry_delay=settings.WEBHOOK_PROCESSING_LEASE_SECONDS if event_busy else None,
        )
        logger.warning(
            "Webhook processing scheduled for retry delivery=%s error_type=%s attempt=%s",
            delivery_id[:12],
            type(exc).__name__,
            int(job.get("attempt", 0)),
        )
    else:
        await _mark_complete(raw, delivery_id)
    return True


async def webhook_worker(stop_event: asyncio.Event) -> None:
    logger.info("Webhook inbox worker started")
    while not stop_event.is_set():
        try:
            recovered = await recover_expired_webhooks()
            if recovered:
                logger.info("Recovered webhook jobs count=%s", recovered)
            processed = await process_one_webhook()
            if not processed:
                await asyncio.sleep(0.5)
        except RedisError as exc:
            logger.warning("Webhook queue unavailable error_type=%s", type(exc).__name__)
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Webhook worker failure error_type=%s", type(exc).__name__)
            await asyncio.sleep(1)


async def get_webhook_queue_metrics() -> dict[str, int]:
    client = await get_redis_client()
    pipe = client.pipeline()
    pipe.llen(QUEUE_KEY)
    pipe.zcard(PROCESSING_KEY)
    pipe.zcard(RETRY_KEY)
    pipe.llen(DEAD_LETTER_KEY)
    queued, processing, retrying, dead = await pipe.execute()
    return {
        "queued": int(queued),
        "processing": int(processing),
        "retrying": int(retrying),
        "dead_letter": int(dead),
    }
