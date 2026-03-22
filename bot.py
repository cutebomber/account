"""
Telegram Account Backup Bot
Main entry point — runs the aiogram bot and Telethon session manager together.
"""

import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN, OWNER_ID
from handlers import router
from session_manager import SessionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Inject shared session manager into bot data so handlers can access it
    session_manager = SessionManager(bot=bot, owner_id=OWNER_ID)
    dp["session_manager"] = session_manager

    # Resume all previously saved sessions
    logger.info("Resuming saved Telethon sessions...")
    await session_manager.resume_all()

    logger.info("Bot is running...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
