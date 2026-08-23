from fastapi import Depends, FastAPI, Response, status
from contextlib import asynccontextmanager
import asyncio
import logging
import time
from sqlalchemy import text
from aiogram import Bot

from app.core.config import settings
from app.core.auth import require_ops_access
from app.core.database import engine, AsyncSessionLocal
from app.core.http import MaxBodySizeMiddleware
from app.core.observability import configure_logging, request_context_middleware
from app.core.rate_limit import public_rate_limit
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
from app.services.webhook_queue import webhook_worker

# Setup privacy-safe structured logging.
configure_logging()
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
    except Exception as exc:
        logger.error("Failed to alert super admin error_type=%s", type(exc).__name__)

async def heartbeat_monitor_redis():
    logger.info("Heartbeat Monitor (Redis) Started.")
    while True:
        await asyncio.sleep(60)
        try:
            redis = await get_redis_client()
            if await redis.exists("global_safe_mode"):
                continue
            last_ts = await redis.get("last_webhook_ts")
            if last_ts and time.time() - float(last_ts) > 600:
                logger.warning("No authenticated webhook received for 10 minutes")
                await notify_super_admin("🚨 <b>CRITICAL:</b> No Webhook received for 10 minutes!")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Heartbeat dependency unavailable error_type=%s", type(exc).__name__)


async def _supervise(name: str, coroutine_factory, stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            await coroutine_factory()
            if not stop_event.is_set():
                logger.warning("Background service exited service=%s", name)
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Background service failed service=%s error_type=%s", name, type(exc).__name__)
            await asyncio.sleep(2)


async def _recover_dependencies(stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            if settings.DB_AUTO_CREATE_SCHEMA:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Account.id).where(Account.status == AccountStatus.ACTIVE)
                )
                active_ids = result.scalars().all()
            redis = await get_redis_client()
            if active_ids:
                await redis.sadd(ACTIVE_ACCOUNTS_KEY, *[str(value) for value in active_ids])
            logger.info("Dependency recovery completed active_accounts=%s", len(active_ids))
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Dependencies not ready error_type=%s", type(exc).__name__)
            await asyncio.sleep(5)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting up...")
    
    stop_event = asyncio.Event()
    await init_redis_pool()
    tasks = [
        asyncio.create_task(_recover_dependencies(stop_event), name="dependency-recovery"),
        asyncio.create_task(_supervise("telegram", start_telegram_bot, stop_event), name="telegram"),
        asyncio.create_task(_supervise("worker-pool", worker_pool.start, stop_event), name="worker-pool"),
        asyncio.create_task(_supervise("scheduler", scheduler, stop_event), name="scheduler"),
        asyncio.create_task(_supervise("heartbeat", heartbeat_monitor_redis, stop_event), name="heartbeat"),
        asyncio.create_task(webhook_worker(stop_event), name="webhook-inbox"),
    ]
    
    yield
    
    # Shutdown
    logger.info("Shutting down...")
    stop_event.set()
    worker_pool.stop()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await stop_telegram_bot()
    await close_redis_pool() # Close Pool
    await engine.dispose()

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan
)
app.add_middleware(MaxBodySizeMiddleware, max_body_bytes=settings.MAX_WEBHOOK_BODY_BYTES)
app.middleware("http")(request_context_middleware)

app.include_router(webhook.router, prefix="/instagram", tags=["webhook"])
app.include_router(
    ops.router,
    prefix="/ops",
    tags=["operations"],
    dependencies=[Depends(require_ops_access)],
)
app.include_router(legal.router, tags=["legal"], dependencies=[Depends(public_rate_limit)])

@app.get("/", dependencies=[Depends(public_rate_limit)])
async def root():
    return {"message": "Instagram Auto Reply System is Running"}

@app.get("/health/live", include_in_schema=False)
async def liveness():
    return {"status": "alive"}


async def _readiness_payload() -> tuple[dict, int]:
    components = {"postgres": "unhealthy", "redis": "unhealthy", "configuration": "healthy"}

    async def check_postgres():
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))

    async def check_redis():
        redis = await get_redis_client()
        await redis.ping()

    try:
        await asyncio.wait_for(check_postgres(), timeout=2)
        components["postgres"] = "healthy"
    except Exception as exc:
        logger.warning("Readiness database check failed error_type=%s", type(exc).__name__)

    try:
        await asyncio.wait_for(check_redis(), timeout=2)
        components["redis"] = "healthy"
    except Exception as exc:
        logger.warning("Readiness Redis check failed error_type=%s", type(exc).__name__)

    ready = all(value == "healthy" for value in components.values())
    return {"status": "ready" if ready else "not_ready", "components": components}, (
        status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
    )


@app.get("/health/ready", include_in_schema=False)
@app.get("/health", include_in_schema=False)
async def readiness(response: Response):
    payload, status_code = await _readiness_payload()
    response.status_code = status_code
    return payload
