from aiogram import Bot
from aiogram.types import Message

async def safe_send(bot: Bot, chat_id: int, text: str, reply_markup=None):
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception:
        # Игнорируем ошибки доставки (например, админ не нажал Start)
        pass
