from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager
import asyncio
import logging
import time
from sqlalchemy import text
from aiogram import Bot

from app.core.config import settings
from app.core.database import engine, AsyncSessionLocal
from app.core.redis_utils import get_redis_client, init_redis_pool, close_redis_pool, ACTIVE_ACCOUNTS_KEY
from app.models.base import Base
# Import all models to ensure they are registered with Base.metadata
from app.models import all_models 
from app.models.all_models import Account, AccountStatus
from sqlalchemy import select
from app.api import webhook
from app.routers import ops, legal # New Legal Router
from app.bot.main import start_telegram_bot, stop_telegram_bot
# from app.services.instagram_service import process_outgoing_queue # Deprecated in Phase 4
from app.services.worker_pool_manager import worker_pool # New Worker Pool Manager
from app.services.background_tasks import scheduler

# Setup Logging
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Heartbeat Monitor ---
last_webhook_time = time.time()

async def notify_super_admin(text: str):
    if not settings.ADMIN_IDS:
        return
    try:
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        for admin_id in settings.ADMIN_IDS:
             await bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
        await bot.session.close()
    except Exception as e:
        logger.error(f"Failed to alert super admin: {e}")

async def heartbeat_monitor_redis():
    logger.info("Heartbeat Monitor (Redis) Started.")
    redis = await get_redis_client()
    
    while True:
        await asyncio.sleep(60)
        
        if await redis.exists("global_safe_mode"):
            continue
            
        last_ts = await redis.get("last_webhook_ts")
        if last_ts:
             if time.time() - float(last_ts) > 600:
                 logger.warning("🚨 Heartbeat Alert (Redis): No Webhook received for 10 minutes!")
                 await notify_super_admin("🚨 <b>CRITICAL:</b> No Webhook received for 10 minutes!")
        else:
             # Initial state or expired
             pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting up...")
    
    # Initialize Global Redis Pool
    await init_redis_pool()

    # Create Tables (For dev/first run convenience)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Active Accounts Recovery
    logger.info("Recovering Active Accounts to Redis...")
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Account.id).where(Account.status == AccountStatus.ACTIVE))
        active_ids = result.scalars().all()
        if active_ids:
             redis = await get_redis_client()
             pipe = redis.pipeline()
             for aid in active_ids:
                 pipe.sadd(ACTIVE_ACCOUNTS_KEY, str(aid))
             await pipe.execute()
    
    # Start Telegram Bot in background
    bot_task = asyncio.create_task(start_telegram_bot())
    
    # Start Worker Manager (Isolated Processes)
    # worker_task = asyncio.create_task(process_outgoing_queue()) # Replaced
    worker_pool_task = asyncio.create_task(worker_pool.start())
    
    # Start Scheduler (Backup & Token Refresh & Data Cleanup)
    scheduler_task = asyncio.create_task(scheduler())
    
    # Start Heartbeat Monitor
    heartbeat_task = asyncio.create_task(heartbeat_monitor_redis())
    
    yield
    
    # Shutdown
    logger.info("Shutting down...")
    bot_task.cancel()
    # worker_task.cancel()
    worker_pool.stop()
    worker_pool_task.cancel()
    scheduler_task.cancel()
    heartbeat_task.cancel()
    await stop_telegram_bot()
    await close_redis_pool() # Close Pool
    await engine.dispose()

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan
)

app.include_router(webhook.router, prefix="/instagram", tags=["webhook"])
app.include_router(ops.router, prefix="/ops", tags=["operations"])
app.include_router(legal.router, tags=["legal"])

@app.get("/")
async def root():
    return {"message": "Instagram Auto Reply System is Running"}

@app.get("/health")
async def health_check():
    """
    Production Health Check Endpoint.
    Checks: DB, Redis, Meta Config, Global Safe Mode.
    """
    health_status = {
        "db": "unknown",
        "redis": "unknown",
        "meta_config": "unknown",
        "system_status": "running",
        "status": "unhealthy"
    }
    
    # 1. Check DB
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        health_status["db"] = "healthy"
    except Exception as e:
        health_status["db"] = f"unhealthy: {str(e)}"

    # 2. Check Redis
    try:
        redis = await get_redis_client()
        await redis.ping()
        health_status["redis"] = "healthy"
        
        # Check Global Safe Mode
        if await redis.exists("global_safe_mode"):
            health_status["system_status"] = "SAFE_MODE (PAUSED)"
            
    except Exception as e:
        health_status["redis"] = f"unhealthy: {str(e)}"
        
    # 3. Check Meta Config (Global)
    if settings.INSTAGRAM_ACCESS_TOKEN and settings.META_APP_SECRET:
         health_status["meta_config"] = "healthy"
    else:
         health_status["meta_config"] = "missing_config"
         
    # Final Status
    if (health_status["db"] == "healthy" and 
        health_status["redis"] == "healthy" and 
        health_status["meta_config"] == "healthy"):
        health_status["status"] = "healthy"
        return health_status
    else:
        from fastapi import Response
        import json
        return Response(content=json.dumps(health_status), media_type="application/json", status_code=503)
