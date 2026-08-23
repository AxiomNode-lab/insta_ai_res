import hashlib
import hmac
import json
import logging
import time

from fastapi import APIRouter, Request, HTTPException, Response, status
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.rate_limit import (
    public_rate_limit,
    webhook_ingress_rate_limit,
    webhook_rate_limit,
)
from app.core.security import verify_meta_signature
from app.core.redis_utils import get_redis_client
from app.services.webhook_queue import enqueue_verified_webhook

router = APIRouter()
logger = logging.getLogger(__name__)

@router.get("/webhook")
async def verify_webhook(request: Request):
    """
    Verification endpoint for Instagram Webhook (Hub Challenge).
    """
    await public_rate_limit(request)
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    
    if mode and token:
        token_valid = hmac.compare_digest(token, settings.META_VERIFY_TOKEN)
        if mode == "subscribe" and token_valid:
            logger.info("Webhook verified successfully.")
            return Response(content=challenge, media_type="text/plain")
        else:
            logger.warning("Webhook verification failed. Token mismatch.")
            raise HTTPException(status_code=403, detail="Verification failed")
            
    raise HTTPException(status_code=400, detail="Missing parameters")

@router.post("/webhook")
async def handle_webhook(request: Request):
    """
    Receives webhook events from Instagram.
    """
    # This intentionally precedes authentication: it bounds the aggregate work
    # required to buffer and authenticate forged requests.
    await webhook_ingress_rate_limit(request)

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.MAX_WEBHOOK_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Webhook body too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc

    # Signature verification is deliberately the first operation that uses body data.
    body_bytes = await request.body()
    if len(body_bytes) > settings.MAX_WEBHOOK_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Webhook body too large")
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_meta_signature(body_bytes, signature):
        logger.warning("Rejected webhook with invalid signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    await webhook_rate_limit(request)

    try:
        payload = json.loads(body_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    if not isinstance(payload, dict) or payload.get("object") != "instagram":
        raise HTTPException(status_code=400, detail="Unsupported webhook payload")

    delivery_id = hashlib.sha256(body_bytes).hexdigest()
    try:
        result = await enqueue_verified_webhook(delivery_id, payload)
        redis = await get_redis_client()
        await redis.set("last_webhook_ts", str(time.time()), ex=86_400)
    except RedisError as exc:
        logger.warning("Webhook inbox unavailable error_type=%s", type(exc).__name__)
        # A retryable response prevents an acknowledged-but-lost Meta delivery.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook inbox unavailable",
            headers={"Retry-After": "5"},
        ) from exc

    response = Response(content="EVENT_RECEIVED", status_code=200)
    response.headers["X-Webhook-Duplicate"] = "1" if result.duplicate else "0"
    return response
