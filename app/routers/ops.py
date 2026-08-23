from fastapi import APIRouter, HTTPException, status
from redis.exceptions import RedisError
from app.core.redis_utils import get_redis_client, get_silence_metrics
from app.core.config import settings
from app.services.webhook_queue import get_webhook_queue_metrics
import time

router = APIRouter()

@router.get("/status")
async def get_ops_status():
    """
    Operational Status Data (Not UI).
    Returns metrics for Owner Status Page.
    """
    try:
        redis = await get_redis_client()
    
    # 1. System Status
        kill_switch = await redis.exists("global_kill_switch")
        system_status = "OPERATIONAL"
        if kill_switch:
            system_status = "PAUSED"
        
    # 2. Workers Alive (Approx)
    # We don't have a direct registry yet, but we can check worker keys if we implemented heartbeat
    # For now, let's return "N/A" or check a heartbeat key if we added one
    # Assuming we added "worker_heartbeat:{id}" in previous turns? 
    # Let's count keys
        workers_alive = 0
        async for _ in redis.scan_iter(match="worker:*", count=100):
            workers_alive += 1
    
    # 3. Queue Health
    # Sum of all queues
        total_queue = 0
        async for key in redis.scan_iter(match="queue:*", count=100):
            if key.count(":") == 1:
                total_queue += await redis.llen(key)
        
    # 4. Metrics (Silence Detection)
        metrics = await get_silence_metrics(window_minutes=5)
        webhook_queue = await get_webhook_queue_metrics()
    except RedisError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Operations datastore unavailable",
        ) from exc
    
    # 5. Webhook Rate (Approx via incoming metric)
    webhook_rate_per_min = metrics["incoming"] / 5 if metrics["incoming"] else 0
    
    # 6. Reply Rate
    reply_rate_per_min = metrics["outgoing"] / 5 if metrics["outgoing"] else 0
    
    return {
        "system_status": system_status,
        "workers_alive": workers_alive,
        "total_queue_backlog": total_queue,
        "incoming_rate_5m": webhook_rate_per_min,
        "outgoing_rate_5m": reply_rate_per_min,
        "webhook_queue": webhook_queue,
        "last_updated": time.time()
    }
