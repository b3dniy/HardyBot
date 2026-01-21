# app/error_handlers.py
from __future__ import annotations

import asyncio
import logging
import random
from typing import Tuple

from aiogram import Dispatcher
from aiogram.filters.exception import ExceptionTypeFilter
from aiogram.types import Update, CallbackQuery, Message
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramAPIError,
)

logger = logging.getLogger(__name__)


# ========= Вспомогательные =========

def _extract_ctx(update: Update) -> Tuple[int | None, int | None, str]:
    """
    Для логов достаём: user_id, chat_id, тип апдейта.
    """
    if update.message:
        m: Message = update.message
        return (m.from_user.id if m.from_user else None), m.chat.id, "message"
    if update.callback_query:
        cb: CallbackQuery = update.callback_query
        chat_id = cb.message.chat.id if cb.message else None
        return (cb.from_user.id if cb.from_user else None), chat_id, "callback_query"
    if update.inline_query:
        return update.inline_query.from_user.id, None, "inline_query"
    return None, None, "update"


async def _safe_answer_cb(update: Update, text: str | None = None, show_alert: bool = False) -> None:
    """
    Аккуратно закрыть «часики» у callback_query.
    """
    if not update or not update.callback_query:
        return
    try:
        await update.callback_query.answer(text or "", show_alert=show_alert)
    except Exception:
        pass


def _is_noise_bad_request(exc: TelegramBadRequest) -> bool:
    """
    Фильтруем «мусорные» 400-ки, которые не требуют реакции.
    """
    t = str(exc).lower()
    noisy = (
        "message is not modified",
        "message to edit not found",
        "message to delete not found",
        "chat not found",
        "bot was blocked by the user",
        "user is deactivated",
        "not enough rights",
        "message is too old",
        "replied message not found",
        "message can't be deleted",
    )
    return any(x in t for x in noisy)


async def _sleep_with_jitter(seconds: float) -> None:
    await asyncio.sleep(max(0.0, seconds) + random.uniform(0, 0.25))


# ========= Регистрация =========

def register_error_handlers(dp: Dispatcher) -> None:
    """
    Единая точка регистрации обработчиков ошибок.
    Порядок имеет значение: от частных к общему.
    """

    # 429 Flood/RetryAfter — подождать и замолчать
    @dp.errors(ExceptionTypeFilter(TelegramRetryAfter))
    async def _retry_after_handler(event, exc: TelegramRetryAfter):
        user_id, chat_id, kind = _extract_ctx(event.update)
        retry = float(getattr(exc, "retry_after", 1.0) or 1.0)
        logger.warning("429 RetryAfter %.2fs on %s (user=%s chat=%s): %s", retry, kind, user_id, chat_id, exc)
        await _safe_answer_cb(event.update)  # закрыть «часики»
        await _sleep_with_jitter(retry)
        return True  # гасим

    # 403 Forbidden — пользователь удалил чат/заблокировал бота
    @dp.errors(ExceptionTypeFilter(TelegramForbiddenError))
    async def _forbidden_handler(event, exc: TelegramForbiddenError):
        user_id, chat_id, kind = _extract_ctx(event.update)
        logger.info("403 Forbidden on %s (user=%s chat=%s): %s", kind, user_id, chat_id, exc)
        await _safe_answer_cb(event.update)
        return True

    # 400 BadRequest — часто «шум»
    @dp.errors(ExceptionTypeFilter(TelegramBadRequest))
    async def _bad_request_handler(event, exc: TelegramBadRequest):
        user_id, chat_id, kind = _extract_ctx(event.update)
        level = logging.WARNING if _is_noise_bad_request(exc) else logging.ERROR
        logger.log(level, "400 BadRequest on %s (user=%s chat=%s): %s", kind, user_id, chat_id, exc)
        await _safe_answer_cb(event.update)
        return True

    # Сетевые ошибки — небольшой бэк-офф
    @dp.errors(ExceptionTypeFilter(TelegramNetworkError))
    async def _network_handler(event, exc: TelegramNetworkError):
        user_id, chat_id, kind = _extract_ctx(event.update)
        logger.warning("Network error on %s (user=%s chat=%s): %s", kind, user_id, chat_id, exc)
        await _safe_answer_cb(event.update)
        await _sleep_with_jitter(1.0)
        return True

    # Прочие API-ошибки Telegram
    @dp.errors(ExceptionTypeFilter(TelegramAPIError))
    async def _api_error_handler(event, exc: TelegramAPIError):
        user_id, chat_id, kind = _extract_ctx(event.update)
        logger.error("TelegramAPIError on %s (user=%s chat=%s): %s", kind, user_id, chat_id, exc)
        await _safe_answer_cb(event.update)
        return True

    # Отмена корутин (обычно при остановке бота) — обрабатываем без фильтра,
    # т.к. CancelledError наследуется от BaseException, а не от Exception.
    @dp.errors()
    async def _cancelled_handler(event, exc):  # тип оставляем общий
        if isinstance(exc, asyncio.CancelledError):
            user_id, chat_id, kind = _extract_ctx(event.update)
            logger.debug("CancelledError on %s (user=%s chat=%s)", kind, user_id, chat_id)
            await _safe_answer_cb(event.update)
            return True  # погасили — дальше не идём

    # Фоллбэк: любой другой эксепшен
    @dp.errors()
    async def _fallback_handler(event, exc: Exception):
        user_id, chat_id, kind = _extract_ctx(event.update)
        logger.exception("Unhandled error on %s (user=%s chat=%s): %r", kind, user_id, chat_id, exc)
        # Если это кнопка — сообщим коротко, чтобы юзер понимал, что делать
        await _safe_answer_cb(event.update, "Произошла ошибка. Попробуйте ещё раз.", show_alert=False)
        return True
