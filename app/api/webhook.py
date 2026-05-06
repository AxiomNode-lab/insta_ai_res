from fastapi import APIRouter, Request, HTTPException, Response, Depends, BackgroundTasks
from app.core.config import settings
from app.core.security import verify_meta_signature
from app.services.instagram_service import process_webhook_payload
from app.core.redis_utils import get_redis_client
import logging
import time

router = APIRouter()
logger = logging.getLogger(__name__)

@router.get("/webhook")
async def verify_webhook(request: Request):
    """
    Verification endpoint for Instagram Webhook (Hub Challenge).
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    
    if mode and token:
        if mode == "subscribe" and token == settings.META_VERIFY_TOKEN:
            logger.info("Webhook verified successfully.")
            return Response(content=challenge, media_type="text/plain")
        else:
            logger.warning("Webhook verification failed. Token mismatch.")
            raise HTTPException(status_code=403, detail="Verification failed")
            
    raise HTTPException(status_code=400, detail="Missing parameters")

@router.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receives webhook events from Instagram.
    """
    # 1. Update Heartbeat (Redis)
    try:
        redis = await get_redis_client()
        await redis.set("last_webhook_ts", str(time.time()))
    except Exception as e:
        logger.error(f"Failed to update heartbeat: {e}")

    # 2. Get Raw Body and Signature
    body_bytes = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    
    # 3. Verify Signature
    if not verify_meta_signature(body_bytes, signature):
        logger.error("Invalid signature.")
        raise HTTPException(status_code=403, detail="Invalid signature")
        
    # 4. Process Event (Background Task to return 200 OK quickly)
    try:
        payload = await request.json()
        background_tasks.add_task(process_webhook_payload, payload)
        return Response(content="EVENT_RECEIVED", status_code=200)
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        # Still return 200 to Meta to avoid retries on bad payload
        return Response(content="EVENT_RECEIVED", status_code=200)
