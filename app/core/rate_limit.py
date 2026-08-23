import asyncio
import hashlib
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.redis_utils import get_redis_client


_fallback_lock = asyncio.Lock()
_fallback_windows: dict[str, deque[float]] = defaultdict(deque)
_MAX_FALLBACK_SOURCES = 10_000


def _source_key(request: Request) -> str:
    host = request.client.host if request.client else "unknown"
    return hashlib.sha256(host.encode("utf-8")).hexdigest()[:24]


async def _local_fallback(key: str, limit: int, period: int) -> bool:
    now = time.monotonic()
    cutoff = now - period
    async with _fallback_lock:
        if key not in _fallback_windows and len(_fallback_windows) >= _MAX_FALLBACK_SOURCES:
            _fallback_windows.pop(next(iter(_fallback_windows)))
        window = _fallback_windows[key]
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        return True


async def enforce_rate_limit(request: Request, *, bucket: str, limit: int, period: int = 60) -> None:
    source = _source_key(request)
    window = int(time.time() // period)
    redis_key = f"rate:public:{bucket}:{source}:{window}"
    fallback_key = f"rate:public:{bucket}:{source}"
    try:
        client = await get_redis_client()
        pipe = client.pipeline()
        pipe.incr(redis_key)
        pipe.expire(redis_key, period + 5)
        current, _ = await pipe.execute()
        allowed = int(current) <= limit
    except RedisError:
        allowed = await _local_fallback(fallback_key, limit, period)

    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(period)},
        )


async def public_rate_limit(request: Request) -> None:
    await enforce_rate_limit(
        request,
        bucket="general",
        limit=settings.PUBLIC_RATE_LIMIT_PER_MINUTE,
    )


async def webhook_rate_limit(request: Request) -> None:
    await enforce_rate_limit(
        request,
        bucket="meta-webhook",
        limit=settings.WEBHOOK_RATE_LIMIT_PER_MINUTE,
    )


async def webhook_ingress_rate_limit(request: Request) -> None:
    """Bound forged-request work before body buffering and HMAC verification."""
    await enforce_rate_limit(
        request,
        bucket="meta-webhook-ingress",
        limit=settings.WEBHOOK_INGRESS_RATE_LIMIT_PER_MINUTE,
    )
