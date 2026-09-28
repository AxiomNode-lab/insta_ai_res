import hmac
from fastapi import APIRouter, Depends, Header, HTTPException
from app.core.redis_utils import get_redis_client, get_silence_metrics
from app.core.config import settings
import time

router = APIRouter()

async def require_ops_key(x_ops_key: str | None = Header(default=None)) -> None:
    configured = settings.OPS_API_KEY
    if not configured or not x_ops_key or not hmac.compare_digest(x_ops_key, configured):
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/status")
async def get_ops_status():
    """
    Operational Status Data (Not UI).
    Returns metrics for Owner Status Page.
    """
    redis = await get_redis_client()
    
    # 1. System Status
    kill_switch = await redis.exists("global_kill_switch")
    status = "OPERATIONAL"
    if kill_switch:
        status = "PAUSED (GLOBAL KILL SWITCH)"
        
    # 2. Workers Alive (Approx)
    # We don't have a direct registry yet, but we can check worker keys if we implemented heartbeat
    # For now, let's return "N/A" or check a heartbeat key if we added one
    # Assuming we added "worker_heartbeat:{id}" in previous turns? 
    # Let's count keys
    worker_keys = await redis.keys("worker:*")
    workers_alive = len(worker_keys)
    
    # 3. Queue Health
    # Sum of all queues
    queue_keys = await redis.keys("queue:*")
    total_queue = 0
    for k in queue_keys:
        total_queue += await redis.llen(k)
        
    # 4. Metrics (Silence Detection)
    metrics = await get_silence_metrics(window_minutes=5)
    
    # 5. Webhook Rate (Approx via incoming metric)
    webhook_rate_per_min = metrics["incoming"] / 5 if metrics["incoming"] else 0
    
    # 6. Reply Rate
    reply_rate_per_min = metrics["outgoing"] / 5 if metrics["outgoing"] else 0
    
    return {
        "system_status": status,
        "workers_alive": workers_alive,
        "total_queue_backlog": total_queue,
        "incoming_rate_5m": webhook_rate_per_min,
        "outgoing_rate_5m": reply_rate_per_min,
        "last_updated": time.time()
    }
