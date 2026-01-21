import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from sqlalchemy import text

from app.middlewares.antispam import AntiSpamMiddleware

from app.config import settings
from app.db import engine, Base, SessionLocal
from app.handlers import common as common_handlers
from app.handlers import user as user_handlers
from app.handlers import admin as admin_handlers
from app.handlers import boss as boss_handlers
from app.middlewares.auth import RequireAuthMiddleware, RequireProfileMiddleware
from app.middlewares.db_session import DBSessionMiddleware
from app.error_handlers import register_error_handlers

print("[DBG] staff_ids:", settings.staff_ids)


async def _apply_simple_migrations():
    """
    Простейшие "миграции" колонок, если нет Alembic.
    Безопасны: обёрнуты в try/except для совместимости с уже созданными колонками.
    """
    async with engine.begin() as conn:
        # users: sip_ext, profile_completed
        try:
            await conn.execute(text('ALTER TABLE users ADD COLUMN sip_ext VARCHAR(3)'))
        except Exception:
            pass
        try:
            await conn.execute(text('ALTER TABLE users ADD COLUMN profile_completed BOOLEAN DEFAULT 0'))
        except Exception:
            pass
        try:
            await conn.execute(text('CREATE INDEX IF NOT EXISTS ix_users_sip_ext ON users (sip_ext)'))
        except Exception:
            pass

        # tasks: author_full_name, author_sip
        try:
            await conn.execute(text('ALTER TABLE tasks ADD COLUMN author_full_name VARCHAR(255)'))
        except Exception:
            pass
        try:
            await conn.execute(text('ALTER TABLE tasks ADD COLUMN author_sip VARCHAR(3)'))
        except Exception:
            pass


async def on_startup(bot: Bot):
    # создаём таблицы (для прототипа; в проде — alembic миграции)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # применим "ленивые" миграции
    await _apply_simple_migrations()

    # команды бота
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Начать"),
            BotCommand(command="help", description="Справка"),
        ]
    )
    logging.getLogger(__name__).info("Startup complete. ENV=%s", settings.ENV)


def setup_middlewares(dp: Dispatcher):
    dp.message.middleware(AntiSpamMiddleware())       # для сообщений и команд
    dp.callback_query.middleware(AntiSpamMiddleware())  # для кнопок

    # сначала прокидываем сессию БД в хендлеры
    dbmw = DBSessionMiddleware(SessionLocal)
    dp.message.middleware(dbmw)
    dp.callback_query.middleware(dbmw)

    # затем проверка аутентификации
    authmw = RequireAuthMiddleware(SessionLocal)
    dp.message.middleware(authmw)
    dp.callback_query.middleware(authmw)

    # НОВОЕ: требуем заполненный профиль (ФИО + SIP) для всех действий, кроме регистрации/базовых
    profmw = RequireProfileMiddleware()
    dp.message.middleware(profmw)
    dp.callback_query.middleware(profmw)


def setup_routers(dp: Dispatcher):
    dp.include_router(common_handlers.router)
    dp.include_router(user_handlers.router)
    dp.include_router(admin_handlers.router)
    dp.include_router(boss_handlers.router)


async def main():
    dp = Dispatcher(storage=MemoryStorage())
    setup_middlewares(dp)
    setup_routers(dp)
    register_error_handlers(dp)

    bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))

    try:
        await on_startup(bot)
        # ограничим типы апдейтов теми, что реально используются
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
