# app/bot.py
import asyncio
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from sqlalchemy import text

from app.config import settings
from app.db import engine, Base, SessionLocal
from app.error_handlers import register_error_handlers
from app.handlers import admin as admin_handlers
from app.handlers import boss as boss_handlers
from app.handlers import common as common_handlers
from app.handlers import user as user_handlers
from app.logging_setup import setup_logging
from app.middlewares.antispam import AntiSpamMiddleware
from app.middlewares.auth import RequireAuthMiddleware, RequireProfileMiddleware
from app.middlewares.db_session import DBSessionMiddleware
from app.middlewares.logging import LoggingMiddleware
from app.utils.uptime import UptimePrinter, format_dt, format_uptime

# Инициализация логирования должна происходить как можно раньше
setup_logging(log_dir="logs", log_file="bot.log", level="INFO", max_mb=50, backup_count=10)


def print_bot_started(staff_ids: set[int]) -> None:
    print(
        """
██████╗  ██████╗ ████████╗
██╔══██╗██╔═══██╗╚══██╔══╝
██████╔╝██║   ██║   ██║
██╔══██╗██║   ██║   ██║
██████╔╝╚██████╔╝   ██║
╚═════╝  ╚═════╝    ╚═╝

███████╗████████╗ █████╗ ██████╗ ████████╗███████╗██████╗
██╔════╝╚══██╔══╝██╔══██╗██╔══██╗╚══██╔══╝██╔════╝██╔══██╗
███████╗   ██║   ███████║██████╔╝   ██║   █████╗  ██║  ██║
╚════██║   ██║   ██╔══██║██╔══██╗   ██║   ██╔══╝  ██║  ██║
███████║   ██║   ██║  ██║██║  ██║   ██║   ███████╗██████╔╝
╚══════╝   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝   ╚══════╝╚═════╝
"""
    )
    print(f"[DBG] staff_ids: {staff_ids}")
    print("-" * 50)


def print_bot_stopped() -> None:
    print(
        """
██████╗  ██████╗ ████████╗
██╔══██╗██╔═══██╗╚══██╔══╝
██████╔╝██║   ██║   ██║
██╔══██╗██║   ██║   ██║
██████╔╝╚██████╔╝   ██║
╚═════╝  ╚═════╝    ╚═╝

███████╗████████╗ ██████╗ ██████╗ ██████╗ ███████╗██████╗
██╔════╝╚══██╔══╝██╔═══██╗██╔══██╗██╔══██╗██╔════╝██╔══██╗
███████╗   ██║   ██║   ██║██████╔╝██████╔╝█████╗  ██║  ██║
╚════██║   ██║   ██║   ██║██╔═══╝ ██╔═══╝ ██╔══╝  ██║  ██║
███████║   ██║   ╚██████╔╝██║     ██║     ███████╗██████╔╝
╚══════╝   ╚═╝    ╚═════╝ ╚═╝     ╚═╝     ╚══════╝╚═════╝
"""
    )


async def _apply_simple_migrations():
    """
    Простейшие "миграции" колонок, если нет Alembic.
    Безопасны: обёрнуты в try/except для совместимости с уже созданными колонками.
    """
    async with engine.begin() as conn:
        # users: sip_ext, profile_completed
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN sip_ext VARCHAR(3)"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN profile_completed BOOLEAN DEFAULT 0"))
        except Exception:
            pass
        try:
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_sip_ext ON users (sip_ext)"))
        except Exception:
            pass

        # tasks: author_full_name, author_sip
        try:
            await conn.execute(text("ALTER TABLE tasks ADD COLUMN author_full_name VARCHAR(255)"))
        except Exception:
            pass
        try:
            await conn.execute(text("ALTER TABLE tasks ADD COLUMN author_sip VARCHAR(3)"))
        except Exception:
            pass


async def on_startup(bot: Bot):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await _apply_simple_migrations()

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Начать"),
            BotCommand(command="help", description="Справка"),
        ]
    )
    logging.getLogger(__name__).info("Startup complete. ENV=%s", settings.ENV)


def setup_middlewares(dp: Dispatcher):
    # Логирование должно стоять максимально рано, чтобы видеть всё, что прилетает
    dp.message.middleware(LoggingMiddleware())
    dp.callback_query.middleware(LoggingMiddleware())

    dp.message.middleware(AntiSpamMiddleware())
    dp.callback_query.middleware(AntiSpamMiddleware())

    dbmw = DBSessionMiddleware(SessionLocal)
    dp.message.middleware(dbmw)
    dp.callback_query.middleware(dbmw)

    authmw = RequireAuthMiddleware(SessionLocal)
    dp.message.middleware(authmw)
    dp.callback_query.middleware(authmw)

    profmw = RequireProfileMiddleware()
    dp.message.middleware(profmw)
    dp.callback_query.middleware(profmw)


def setup_routers(dp: Dispatcher):
    dp.include_router(common_handlers.router)
    dp.include_router(user_handlers.router)
    dp.include_router(admin_handlers.router)
    dp.include_router(boss_handlers.router)


async def main() -> None:
    print_bot_started(settings.staff_ids)

    started_at = datetime.now().astimezone()
    uptime = UptimePrinter(started_at=started_at)
    uptime.start()

    dp = Dispatcher(storage=MemoryStorage())
    setup_middlewares(dp)
    setup_routers(dp)
    register_error_handlers(dp)

    bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))

    try:
        await on_startup(bot)

        # При Ctrl+C polling часто завершается через CancelledError — это штатно.
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        except asyncio.CancelledError:
            pass

    finally:
        await uptime.stop()
        await bot.session.close()

        print_bot_stopped()

        finished_at = datetime.now().astimezone()
        worked_sec = int((finished_at - started_at).total_seconds())

        print(f"Started at : {format_dt(started_at)}")
        print(f"Stopped at : {format_dt(finished_at)}")
        print(f"Worked     : {format_uptime(worked_sec)}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # подавляем traceback при Ctrl+C (логика остановки уже в finally внутри main)
        pass
