import redis.asyncio as redis
from app.core.config import settings
import json
import time
import uuid

# --- Scalability: Global Connection Pool ---
# Global Redis Pool Singleton
_redis_pool = None


class QueueBackpressure(RuntimeError):
    pass

async def init_redis_pool():
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = redis.ConnectionPool.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            max_connections=100,
            socket_connect_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
            health_check_interval=30,
        )

async def close_redis_pool():
    global _redis_pool
    if _redis_pool:
        await _redis_pool.disconnect()
        _redis_pool = None

async def get_redis_client():
    if _redis_pool is None:
        await init_redis_pool()
    return redis.Redis(connection_pool=_redis_pool)

# --- Global Trust Protection Layer (Behavior Monitor) ---
async def is_event_processed(event_id: str, expiration: int = 86400) -> bool:
    client = await get_redis_client()
    key = f"event_processed:{event_id}"
    success = await client.set(key, "1", ex=expiration, nx=True)
    return not success


async def claim_event(event_id: str) -> tuple[str, str | None]:
    """Return (claimed|complete|busy, token)."""
    client = await get_redis_client()
    key = f"event_processed:{event_id}"
    token = uuid.uuid4().hex
    claimed = await client.set(
        key,
        f"processing:{token}",
        ex=settings.WEBHOOK_PROCESSING_LEASE_SECONDS,
        nx=True,
    )
    if claimed:
        return "claimed", token
    value = await client.get(key)
    return ("complete", None) if value == "complete" else ("busy", None)


async def complete_event(event_id: str, token: str) -> bool:
    client = await get_redis_client()
    script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      redis.call('SET', KEYS[1], 'complete', 'EX', ARGV[2])
      return 1
    end
    return 0
    """
    return bool(
        await client.eval(
            script,
            1,
            f"event_processed:{event_id}",
            f"processing:{token}",
            settings.WEBHOOK_DEDUP_TTL_SECONDS,
        )
    )


async def release_event(event_id: str, token: str) -> bool:
    client = await get_redis_client()
    script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    end
    return 0
    """
    return bool(
        await client.eval(
            script,
            1,
            f"event_processed:{event_id}",
            f"processing:{token}",
        )
    )

# --- Global Behaviour Monitor ---
async def record_global_behavior(response_time: float, is_human: bool = False, is_ignored: bool = False):
    """
    Records global stats for 1-minute window.
    """
    client = await get_redis_client()
    now_min = int(time.time() / 60)
    key = f"global_monitor:{now_min}"
    
    pipe = client.pipeline()
    pipe.incr(f"{key}:total")
    if response_time < 2.0:
        pipe.incr(f"{key}:fast_replies")
    if is_human:
        pipe.incr(f"{key}:human_escalation")
    if is_ignored:
        pipe.incr(f"{key}:ignored")
        
    pipe.expire(key, 300) # Keep for 5 mins
    pipe.expire(f"{key}:total", 300)
    pipe.expire(f"{key}:fast_replies", 300)
    pipe.expire(f"{key}:human_escalation", 300)
    pipe.expire(f"{key}:ignored", 300)
    await pipe.execute()

async def record_global_reply_pattern(account_id: int, text: str):
    """
    Tracks reply patterns across all accounts to detect bot networks.
    """
    client = await get_redis_client()
    text_hash = hash(text.strip().lower()[:100])
    now_min = int(time.time() / 60)
    
    # Track accounts using this reply in this minute
    key = f"global_pattern:{now_min}:{text_hash}"
    await client.sadd(key, str(account_id))
    await client.expire(key, 300)

async def check_app_reputation_risk() -> str:
    """
    Calculates App Risk Score based on cross-account patterns.
    Returns: NORMAL | HIGH_RISK
    """
    client = await get_redis_client()
    now_min = int(time.time() / 60)
    
    # 1. Check for shared replies (Bot Network)
    # Scan patterns for current minute
    pattern_keys = await client.keys(f"global_pattern:{now_min}:*")
    for key in pattern_keys:
        count = await client.scard(key)
        if count > 10: # More than 10 accounts sending same reply
            await client.setex("app_risk_mode", 600, "HIGH_RISK")
            return "HIGH_RISK"

    # 2. Check global fast replies ratio (from existing monitor)
    key = f"global_monitor:{now_min}"
    total = await client.get(f"{key}:total")
    fast = await client.get(f"{key}:fast_replies")
    
    if total and int(total) > 100:
        fast_ratio = int(fast or 0) / int(total)
        if fast_ratio > 0.90: # 90% fast replies globally
             await client.setex("app_risk_mode", 600, "HIGH_RISK")
             return "HIGH_RISK"
             
    return "NORMAL"

async def is_app_risk_high() -> bool:
    client = await get_redis_client()
    return await client.exists("app_risk_mode")


async def check_global_risk_score() -> str:
    """
    Calculates Risk Score based on last 5 minutes.
    Returns: NORMAL | HIGH_RISK
    """
    client = await get_redis_client()
    now_min = int(time.time() / 60)
    
    # Check last minute
    key = f"global_monitor:{now_min}"
    total = await client.get(f"{key}:total")
    fast = await client.get(f"{key}:fast_replies")
    
    if total and int(total) > 50: # If traffic is significant
        fast_ratio = int(fast or 0) / int(total)
        if fast_ratio > 0.85: # > 85% replies < 2s
            # Trigger Protection
            await client.setex("global_protection_mode", 300, "1") # 5 mins
            return "HIGH_RISK"
            
    return "NORMAL"

async def is_global_protection_active() -> bool:
    client = await get_redis_client()
    return await client.exists("global_protection_mode")

# --- Rate Limiting (User Level) ---
async def is_rate_limited(account_id: int, user_id: str, limit: int = 5, period: int = 60) -> bool:
    client = await get_redis_client()
    key = f"rate_limit:{account_id}:{user_id}"
    current = await client.incr(key)
    if current == 1:
        await client.expire(key, period)
    return current > limit

# --- Backpressure & Adaptive Throttling ---
async def get_account_load_status(account_id: int) -> str:
    """
    Determines account load status: NORMAL, HIGH, CRITICAL
    Based on incoming vs processing rate.
    """
    client = await get_redis_client()
    incoming_key = f"metrics:incoming:{account_id}"
    processed_key = f"metrics:processed:{account_id}"
    
    # Get rates (approx over last few seconds if we used sliding window, 
    # but for simplicity assume these keys are incremented and expired every second by caller or worker)
    # Actually simpler: Check Queue Size
    queue_key = f"queue:{account_id}"
    pipe = client.pipeline()
    pipe.llen(queue_key)
    pipe.zcard(f"queue:processing:{account_id}")
    pipe.zcard(f"queue:retry:{account_id}")
    ready, processing, retrying = await pipe.execute()
    queue_size = int(ready) + int(processing) + int(retrying)
    
    if queue_size >= settings.OUTBOUND_QUEUE_MAX_DEPTH:
        return "CRITICAL"
    elif queue_size > 100:
        return "HIGH"
    return "NORMAL"

async def check_throttling(account_id: int) -> bool:
    """
    Returns True if we should throttle (delay/reject), False if ok.
    """
    status = await get_account_load_status(account_id)
    if status == "CRITICAL":
        return True
    return False

# --- Meta API Circuit Breaker ---
async def check_circuit_breaker(account_id: int) -> bool:
    """
    Returns True if Circuit is OPEN (Stop sending), False if CLOSED (OK).
    """
    client = await get_redis_client()
    key = f"circuit:{account_id}"
    state = await client.get(key)
    return state == "OPEN"

async def record_meta_failure(account_id: int):
    """
    Records a failure. If > 10 in 60s, Open Circuit.
    """
    client = await get_redis_client()
    fail_key = f"circuit:fails:{account_id}"
    
    # Increment failure count
    count = await client.incr(fail_key)
    if count == 1:
        await client.expire(fail_key, 60)
        
    if count >= 10:
        # Open Circuit
        circuit_key = f"circuit:{account_id}"
        await client.setex(circuit_key, 60, "OPEN") # Open for 60s
        # Log outage event (can be done in caller)

async def record_meta_success(account_id: int):
    """
    Reset failure count on success (Half-Open logic simplified: success closes/resets).
    """
    client = await get_redis_client()
    # If we were OPEN (and key expired so we tried), we are now effectively closed.
    # Just clear failure count to be safe.
    fail_key = f"circuit:fails:{account_id}"
    await client.delete(fail_key)

# --- Dead Letter Queue Limits ---
async def move_to_dead_letter(account_id: int, payload: dict, error_msg: str):
    """
    Moves failed message to DLQ with limit of 1000.
    """
    client = await get_redis_client()
    dlq_key = f"dead_letter:{account_id}"
    payload = dict(payload)
    payload["error_type"] = error_msg
    payload["failed_at"] = time.time()
    
    # Push new
    await client.rpush(dlq_key, json.dumps(payload))
    
    # Trim to 1000
    # ltrim(start, stop) -> keep range
    # we want to keep last 1000. 
    # List is: [oldest, ..., newest] (rpush)
    # So we want indices: -1000 to -1
    await client.ltrim(dlq_key, -1000, -1)
    await client.expire(dlq_key, 2_592_000)

# --- Metrics Collection ---
async def record_metric(metric_name: str, account_id: int, value: float = 1):
    """
    Records operational metrics.
    metric_name: processing_time_ms, queue_size, retry_count, failed_requests
    """
    client = await get_redis_client()
    # Simple counter for counts, or Average for timing?
    # For simplicity:
    # metrics:{account_id}:{metric_name} -> increment or set
    key = f"metrics:{account_id}:{metric_name}"
    
    if metric_name == "processing_time_ms":
        # Store average? Moving average?
        # Simpler: Store last value for snapshot
        await client.set(key, value)
    else:
        await client.incrby(key, int(value))

async def get_metrics(account_id: int) -> dict:
    client = await get_redis_client()
    keys = [
        f"metrics:{account_id}:processing_time_ms",
        f"metrics:{account_id}:retry_count",
        f"metrics:{account_id}:failed_requests",
        f"queue:{account_id}"
    ]
    # Also circuit state
    circuit_key = f"circuit:{account_id}"
    
    values = await client.mget(keys + [circuit_key])
    
    queue_len = await client.llen(f"queue:{account_id}")
    
    return {
        "processing_time_ms": values[0],
        "retry_count": values[1],
        "failed_requests": values[2],
        "queue_size": queue_len,
        "circuit_state": values[4] or "CLOSED"
    }

# --- Account Limits & Safe Mode & Quarantine ---
async def check_account_limits(account_id: int) -> bool:
    if await is_account_safe_mode(account_id) or await is_account_quarantined(account_id):
        return False

    client = await get_redis_client()
    hour_key = f"limit:acc:{account_id}:hour"
    day_key = f"limit:acc:{account_id}:day"
    
    hour_count = await client.get(hour_key)
    day_count = await client.get(day_key)
    
    max_hour = 40
    max_day = 400
    
    if hour_count and int(hour_count) >= max_hour:
        return False
    if day_count and int(day_count) >= max_day:
        return False
        
    pipe = client.pipeline()
    pipe.incr(hour_key)
    pipe.expire(hour_key, 3600)
    pipe.incr(day_key)
    pipe.expire(day_key, 86400)
    await pipe.execute()
    
    return True

async def is_account_safe_mode(account_id: int) -> bool:
    client = await get_redis_client()
    key = f"safe_mode:acc:{account_id}"
    return await client.exists(key)

async def set_account_safe_mode(account_id: int, active: bool):
    client = await get_redis_client()
    key = f"safe_mode:acc:{account_id}"
    if active:
        await client.setex(key, 86400, "1") 
    else:
        await client.delete(key)

async def is_account_quarantined(account_id: int) -> bool:
    client = await get_redis_client()
    key = f"quarantine:acc:{account_id}"
    return await client.exists(key)

async def set_account_quarantine(account_id: int, active: bool):
    client = await get_redis_client()
    key = f"quarantine:acc:{account_id}"
    if active:
        await client.set(key, "1") 
    else:
        await client.delete(key)

# --- Human Takeover ---
async def set_human_takeover(account_id: int, user_id: str, active: bool):
    client = await get_redis_client()
    key = f"human_mode:{account_id}:{user_id}"
    if active:
        await client.set(key, "1")
    else:
        await client.delete(key)

async def is_human_takeover_active(account_id: int, user_id: str) -> bool:
    client = await get_redis_client()
    key = f"human_mode:{account_id}:{user_id}"
    return await client.exists(key)

# --- 24h Window Management ---
async def update_last_interaction(account_id: int, user_id: str, timestamp: int = None):
    client = await get_redis_client()
    key = f"last_interaction:{account_id}:{user_id}"
    ts = timestamp if timestamp else int(time.time())
    await client.set(key, ts)

async def is_within_24h_window(account_id: int, user_id: str, current_timestamp: int = None) -> bool:
    client = await get_redis_client()
    key = f"last_interaction:{account_id}:{user_id}"
    last_time = await client.get(key)
    
    if not last_time:
        return False
        
    now = current_timestamp if current_timestamp else int(time.time())
    return (now - int(last_time)) < 86400

# --- Multi-Tenant Queue & Idempotency ---
ACTIVE_ACCOUNTS_KEY = "active_accounts_set"

async def acquire_queue_lock(lock_key: str, expiration: int = 30) -> bool:
    client = await get_redis_client()
    success = await client.set(lock_key, "1", ex=expiration, nx=True)
    return bool(success)

async def enqueue_message(
    recipient_id: str,
    text: str,
    account_id: int,
    delay: float = 0,
    recipient_type: str = "user",
):
    if recipient_type not in {"user", "comment"}:
        raise ValueError("Unsupported Meta recipient type")
    client = await get_redis_client()
    
    # Backpressure check
    if await check_throttling(account_id):
        raise QueueBackpressure(f"Outbound queue capacity reached for account {account_id}")

    queue_key = f"queue:{account_id}"
    payload = json.dumps({
        "job_id": uuid.uuid4().hex,
        "recipient_id": recipient_id, 
        "recipient_type": recipient_type,
        "text": text, 
        "account_id": account_id,
        "delay": delay,
        "attempt": 0,
        "queued_at": time.time(),
    }, separators=(",", ":"), sort_keys=True)
    
    pipe = client.pipeline()
    pipe.rpush(queue_key, payload)
    pipe.sadd(ACTIVE_ACCOUNTS_KEY, str(account_id))
    await pipe.execute()


async def enqueue_private_reply(comment_id: str, text: str, account_id: int, delay: float = 0):
    """Queue Meta's one-time private reply addressed to the comment, not the user."""
    await enqueue_message(
        recipient_id=comment_id,
        text=text,
        account_id=account_id,
        delay=delay,
        recipient_type="comment",
    )


_ENQUEUE_AFTER_HOURS_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return 0
end
local depth = redis.call('LLEN', KEYS[2])
  + redis.call('ZCARD', KEYS[3])
  + redis.call('ZCARD', KEYS[4])
if depth >= tonumber(ARGV[2]) then
  return -1
end
redis.call('SET', KEYS[1], '1', 'EX', ARGV[1])
redis.call('RPUSH', KEYS[2], ARGV[3])
redis.call('SADD', KEYS[5], ARGV[4])
return 1
"""


async def enqueue_after_hours_notice(
    recipient_id: str,
    text: str,
    account_id: int,
    delay: float = 0,
    expiration: int = 21600,
) -> bool:
    """Atomically claim the cooldown and durably enqueue the customer notice."""
    payload = json.dumps(
        {
            "job_id": uuid.uuid4().hex,
            "recipient_id": recipient_id,
            "recipient_type": "user",
            "text": text,
            "account_id": account_id,
            "delay": delay,
            "attempt": 0,
            "queued_at": time.time(),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    client = await get_redis_client()
    result = int(
        await client.eval(
            _ENQUEUE_AFTER_HOURS_SCRIPT,
            5,
            f"after_hours_notice:{account_id}:{recipient_id}",
            f"queue:{account_id}",
            f"queue:processing:{account_id}",
            f"queue:retry:{account_id}",
            ACTIVE_ACCOUNTS_KEY,
            expiration,
            settings.OUTBOUND_QUEUE_MAX_DEPTH,
            payload,
            account_id,
        )
    )
    if result == -1:
        raise QueueBackpressure(f"Outbound queue capacity reached for account {account_id}")
    return result == 1


_CLAIM_OUTBOUND_SCRIPT = """
local raw = redis.call('RPOP', KEYS[1])
if not raw then
  return false
end
redis.call('ZADD', KEYS[2], ARGV[1], raw)
return raw
"""

_MOVE_OUTBOUND_DUE_SCRIPT = """
local members = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
for _, raw in ipairs(members) do
  if redis.call('ZREM', KEYS[1], raw) == 1 then
    redis.call('LPUSH', KEYS[2], raw)
  end
end
return #members
"""


async def recover_outbound_jobs(account_id: int, batch_size: int = 100) -> int:
    client = await get_redis_client()
    now = time.time()
    queue_key = f"queue:{account_id}"
    recovered = 0
    for source in (f"queue:processing:{account_id}", f"queue:retry:{account_id}"):
        recovered += int(
            await client.eval(
                _MOVE_OUTBOUND_DUE_SCRIPT,
                2,
                source,
                queue_key,
                now,
                batch_size,
            )
        )
    return recovered


async def claim_outbound_message(account_id: int, lease_seconds: int = 300) -> str | None:
    await recover_outbound_jobs(account_id)
    client = await get_redis_client()
    return await client.eval(
        _CLAIM_OUTBOUND_SCRIPT,
        2,
        f"queue:{account_id}",
        f"queue:processing:{account_id}",
        time.time() + lease_seconds,
    )


async def acknowledge_outbound_message(account_id: int, raw: str) -> None:
    client = await get_redis_client()
    await client.zrem(f"queue:processing:{account_id}", raw)


async def retry_outbound_message(
    account_id: int,
    raw: str,
    payload: dict,
    error_type: str,
    max_attempts: int = 6,
) -> bool:
    """Schedule a bounded retry. Return True when moved to the terminal DLQ."""
    client = await get_redis_client()
    updated = dict(payload)
    attempt = int(updated.get("attempt", 0)) + 1
    updated["attempt"] = attempt
    updated["last_error_type"] = error_type
    updated["last_failed_at"] = time.time()
    encoded = json.dumps(updated, separators=(",", ":"), sort_keys=True)

    pipe = client.pipeline(transaction=True)
    pipe.zrem(f"queue:processing:{account_id}", raw)
    if attempt >= max_attempts:
        dlq_key = f"dead_letter:{account_id}"
        pipe.rpush(dlq_key, encoded)
        pipe.ltrim(dlq_key, -1000, -1)
        pipe.expire(dlq_key, 2_592_000)
        terminal = True
    else:
        delay = min(5 * (2 ** (attempt - 1)), 300)
        pipe.zadd(f"queue:retry:{account_id}", {encoded: time.time() + delay})
        terminal = False
    await pipe.execute()
    return terminal

async def get_next_message_from_queue() -> dict | None:
    client = await get_redis_client()
    accounts = await client.smembers(ACTIVE_ACCOUNTS_KEY)
    if not accounts:
        return None
    
    for acc_id in accounts:
        queue_key = f"queue:{acc_id}"
        data = await client.lpop(queue_key)
        
        if data:
            return json.loads(data)
        else:
            await client.srem(ACTIVE_ACCOUNTS_KEY, acc_id)
            
    return None

async def set_worker_status(account_id: int, status: str):
    client = await get_redis_client()
    key = f"worker:{account_id}"
    await client.setex(key, 60, status) 

async def check_daily_conversation_limit(account_id: int, max_limit: int = 120) -> bool:
    client = await get_redis_client()
    today = time.strftime("%Y-%m-%d")
    key = f"daily_conv_limit:{account_id}:{today}"
    # current = await client.incr(key) # This increments on every call!
    # We should only read here or increment?
    # The requirement was "if exceeds".
    # But where do we increment?
    # Previous implementation was simple check-and-increment.
    # But now we pass max_limit dynamically.
    # So we should get value, compare, then increment?
    # Or just increment and compare result.
    # "check_daily_conversation_limit" sounds like a check.
    # But usually rate limiters increment.
    # Let's keep incrementing but respect the dynamic limit.
    current = await client.incr(key)
    if current == 1:
        await client.expire(key, 86400)
    return current <= max_limit

async def check_reply_repetition(account_id: int, text: str, max_limit: int = 50) -> bool:
    client = await get_redis_client()
    text_hash = hash(text)
    key = f"reply_repetition:{account_id}:{text_hash}"
    current = await client.incr(key)
    if current == 1:
        await client.expire(key, 3600) # 1 hour
    return current <= max_limit

# --- Reliability: Conversation Lock ---
async def acquire_conversation_lock(account_id: int, conversation_id: str, expiration: int = 600) -> str | None:
    client = await get_redis_client()
    key = f"lock:conversation:{account_id}:{conversation_id}"
    token = uuid.uuid4().hex
    acquired = await client.set(key, token, ex=expiration, nx=True)
    return token if acquired else None

async def release_conversation_lock(account_id: int, conversation_id: str, token: str):
    client = await get_redis_client()
    key = f"lock:conversation:{account_id}:{conversation_id}"
    script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    end
    return 0
    """
    await client.eval(script, 1, key, token)

async def record_unknown_intent(account_id: int, text: str) -> bool:
    """
    Increments count for unknown message text.
    Returns True if count reaches 5 (threshold to suggest).
    """
    client = await get_redis_client()
    # Normalize text (basic)
    norm_text = text.strip().lower()[:50] # Limit length
    text_hash = hash(norm_text)
    
    key = f"unknown_intent:{account_id}:{text_hash}"
    count = await client.incr(key)
    
    if count == 1:
        await client.expire(key, 3600) # 1 hour
        # Store text map to retrieve later
        # Use SETEX which sets value and expiry
        await client.setex(f"unknown_text:{account_id}:{text_hash}", 3600, text)
        
    return count == 5

async def record_admin_reply(account_id: int, user_id: str):
    client = await get_redis_client()
    key = f"last_admin_reply:{account_id}:{user_id}"
    await client.set(key, int(time.time()))

# --- Conversation Quality Engine ---

async def track_conversation_depth(account_id: int, conversation_id: int, direction: str):
    """
    Increments user or bot message counts for depth analysis.
    direction: "incoming" (user) or "outgoing" (bot)
    """
    client = await get_redis_client()
    key = f"conv_depth:{conversation_id}"
    
    pipe = client.pipeline()
    if direction == "incoming":
        pipe.hincrby(key, "user_msgs", 1)
    else:
        pipe.hincrby(key, "bot_msgs", 1)
        
    pipe.expire(key, 86400) # Keep for 24h
    await pipe.execute()

async def get_conversation_depth(conversation_id: int) -> dict:
    client = await get_redis_client()
    key = f"conv_depth:{conversation_id}"
    data = await client.hgetall(key)
    return {
        "user_msgs": int(data.get("user_msgs", 0)),
        "bot_msgs": int(data.get("bot_msgs", 0))
    }

async def check_follow_up_cooldown(account_id: int, user_id: str, text_hash: str) -> bool:
    """
    Returns True if user sent same message < 30s ago (Follow-Up Cooldown).
    """
    client = await get_redis_client()
    key = f"follow_up_cooldown:{account_id}:{user_id}:{text_hash}"
    # setnx: set if not exists
    # If exists, return True (Cooldown active)
    # If not, set it and expire in 30s
    if await client.set(key, "1", ex=30, nx=True):
        return False # No cooldown, proceed
    return True # Cooldown active

async def check_conversation_diversity(account_id: int, structure_hash: str) -> bool:
    """
    Checks if a conversation structure is repeating too often.
    structure_hash: hash of (last_msg -> reply -> end) or similar.
    Returns True if diversity violation (too frequent).
    """
    client = await get_redis_client()
    key = f"conv_diversity:{account_id}:{structure_hash}"
    count = await client.incr(key)
    if count == 1:
        await client.expire(key, 3600)
        
    return count > 20 # Arbitrary threshold for "too frequent"

async def set_account_behavior_state(account_id: int, state: str):
    """
    state: HEALTHY | DRY_CONVERSATIONS | BOT_LIKE_PATTERN
    """
    client = await get_redis_client()
    key = f"behavior_state:{account_id}"
    await client.set(key, state)

async def get_account_behavior_state(account_id: int) -> str:
    client = await get_redis_client()
    key = f"behavior_state:{account_id}"
    return await client.get(key) or "HEALTHY"

async def set_platform_behavior_risk(active: bool):
    client = await get_redis_client()
    key = "platform_behavior_risk"
    if active:
        await client.set(key, "1")
    else:
        await client.delete(key)

async def is_platform_behavior_risk() -> bool:
    client = await get_redis_client()
    return await client.exists("platform_behavior_risk")

async def is_trust_recovery_mode(account_id: int) -> bool:
    """
    Checks if Trust Recovery Mode is active (BOT_LIKE_PATTERN > 30 mins).
    """
    client = await get_redis_client()
    # We can check if behavior state has been BOT_LIKE_PATTERN for long.
    # We need a timestamp for when it started.
    start_time = await client.get(f"bot_like_start:{account_id}")
    if start_time and (int(time.time()) - int(start_time)) > 1800: # 30 mins
        return True
    return False

async def record_bot_like_start(account_id: int, is_bot_like: bool):
    client = await get_redis_client()
    key = f"bot_like_start:{account_id}"
    if is_bot_like:
        await client.set(key, int(time.time()), nx=True) # Only set if not exists
    else:
        await client.delete(key)

# --- Operational Layer (Phase 1) ---

async def is_global_kill_switch_active() -> bool:
    """
    Checks if Global Kill Switch is active.
    Returns: True if PAUSED or SAFE_MODE.
    """
    client = await get_redis_client()
    return await client.exists("global_kill_switch")

async def set_global_kill_switch(active: bool):
    client = await get_redis_client()
    if active:
        await client.set("global_kill_switch", "1")
    else:
        await client.delete("global_kill_switch")

async def track_incoming_message(count: int = 1):
    """
    Tracks incoming messages for Silence Detection.
    """
    client = await get_redis_client()
    now_min = int(time.time() / 60)
    # Track for current minute and previous minute (2-min window)
    key = f"ops:incoming:{now_min}"
    pipe = client.pipeline()
    pipe.incrby(key, count)
    pipe.expire(key, 300) # Keep for 5 mins
    await pipe.execute()

async def track_outgoing_message(count: int = 1):
    """
    Tracks outgoing messages for Silence Detection.
    """
    client = await get_redis_client()
    now_min = int(time.time() / 60)
    key = f"ops:outgoing:{now_min}"
    pipe = client.pipeline()
    pipe.incrby(key, count)
    pipe.expire(key, 300)
    await pipe.execute()

async def get_silence_metrics(window_minutes: int = 2) -> dict:
    """
    Returns total incoming and outgoing for the last X minutes.
    """
    client = await get_redis_client()
    now_min = int(time.time() / 60)
    
    total_in = 0
    total_out = 0
    
    # Check last X minutes (excluding current incomplete minute? No, include current)
    # Let's check [now, now-1]
    keys_in = [f"ops:incoming:{now_min - i}" for i in range(window_minutes)]
    keys_out = [f"ops:outgoing:{now_min - i}" for i in range(window_minutes)]
    
    vals_in = await client.mget(keys_in)
    vals_out = await client.mget(keys_out)
    
    for v in vals_in:
        if v: total_in += int(v)
    for v in vals_out:
        if v: total_out += int(v)
        
    return {"incoming": total_in, "outgoing": total_out}

async def is_account_locked(account_id: int) -> bool:
    """
    Emergency Account Isolation.
    """
    client = await get_redis_client()
    return await client.exists(f"account_lockdown:{account_id}")

async def set_account_lockdown(account_id: int, active: bool):
    client = await get_redis_client()
    key = f"account_lockdown:{account_id}"
    if active:
        await client.set(key, "1")
    else:
        await client.delete(key)
