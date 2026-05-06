import asyncio
import logging
from aiogram import Bot, Dispatcher
from app.core.config import settings
from app.bot.handlers import router

logger = logging.getLogger(__name__)

bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
dp.include_router(router)

async def start_telegram_bot():
    """
    Starts the Telegram bot in polling mode.
    """
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Starting Telegram Bot polling...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Error starting Telegram Bot: {e}")

async def stop_telegram_bot():
    """
    Stops the Telegram bot.
    """
    logger.info("Stopping Telegram Bot...")
    await bot.session.close()
