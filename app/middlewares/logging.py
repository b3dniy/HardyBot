import logging
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, Update


log_chat = logging.getLogger("CHAT")
log_cb = logging.getLogger("CALLBACK")


def _safe_text(s: str | None, limit: int = 1500) -> str:
    if not s:
        return ""
    s = s.replace("\n", "\\n")
    if len(s) > limit:
        return s[:limit] + "...(truncated)"
    return s


class LoggingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Update, data):
        # Message
        if isinstance(event, Message):
            user_id = event.from_user.id if event.from_user else None
            chat_id = event.chat.id if event.chat else None

            text = event.text or event.caption or ""
            kind = "text" if event.text else ("caption" if event.caption else "nontext")

            # Медиа/файлы — логируем тип, без бинарщины
            media = []
            if event.photo:
                media.append(f"photo[{len(event.photo)}]")
            if event.video:
                media.append("video")
            if event.document:
                media.append(f"document:{event.document.file_name}")
            if event.voice:
                media.append("voice")
            if event.audio:
                media.append("audio")
            if event.sticker:
                media.append("sticker")
            if event.location:
                media.append("location")

            media_part = (" media=" + ",".join(media)) if media else ""
            log_chat.info(
                "IN  | chat_id=%s user_id=%s kind=%s%s text=%s",
                chat_id,
                user_id,
                kind,
                media_part,
                _safe_text(text),
            )

        # CallbackQuery
        elif isinstance(event, CallbackQuery):
            user_id = event.from_user.id if event.from_user else None
            chat_id = event.message.chat.id if event.message and event.message.chat else None
            log_cb.info(
                "IN  | chat_id=%s user_id=%s data=%s",
                chat_id,
                user_id,
                _safe_text(event.data, limit=500),
            )

        try:
            return await handler(event, data)
        except Exception:
            logging.getLogger("ERR").exception("ERR | handler exception (event=%s)", type(event).__name__)
            raise
