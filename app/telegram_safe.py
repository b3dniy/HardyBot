# app/telegram_safe.py
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Awaitable, Callable, Iterable, List, Optional

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

logger = logging.getLogger(__name__)
chat_log = logging.getLogger("CHAT")
err_log = logging.getLogger("ERR")


def _safe_text(s: str | None, limit: int = 1500) -> str:
    if not s:
        return ""
    s = s.replace("\n", "\\n")
    if len(s) > limit:
        return s[:limit] + "...(truncated)"
    return s


def _summarize_kwargs(kwargs: dict, max_len: int = 800) -> str:
    """
    Не пытаемся сериализовать сложные типы (клавиатуры и т.п.) — только безопасное repr,
    и ограничиваем длину, чтобы не раздувать лог.
    """
    if not kwargs:
        return ""
    try:
        s = repr(kwargs)
    except Exception:
        s = "<kwargs:unrepr>"
    if len(s) > max_len:
        s = s[:max_len] + "...(truncated)"
    return s


async def _backoff_sleep(attempt: int) -> None:
    # 0.4, 0.8, 1.6, 3.2... + jitter
    base = min(0.4 * (2 ** (attempt - 1)), 5.0)
    await asyncio.sleep(base + random.uniform(0, 0.25))


async def call_with_retry(
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    max_attempts: int = 5,
    context: str = "",
) -> Any:
    """
    Универсальный ретрай вызова Telegram API.
    coro_factory — лямбда, которая создаёт корутину вызова метода.
    context — строка для логов (что именно пытались сделать).
    """
    attempt = 1
    while True:
        try:
            return await coro_factory()

        except TelegramRetryAfter as e:
            retry = float(getattr(e, "retry_after", 1.0) or 1.0)
            logger.warning(
                "RetryAfter: sleep %.2fs (attempt %s/%s) ctx=%s",
                retry,
                attempt,
                max_attempts,
                context,
            )
            await asyncio.sleep(retry + 0.2)

        except TelegramForbiddenError as e:
            # Пользователь/чат запретил бота — дальнейшие попытки бессмысленны
            logger.info("Forbidden: %s — skip further attempts ctx=%s", e, context)
            return None

        except TelegramBadRequest as e:
            text = str(e).lower()
            noisy = (
                "message is not modified",
                "message to edit not found",
                "message to delete not found",
                "chat not found",
                "replied message not found",
                "message can't be deleted",
            )
            if any(x in text for x in noisy):
                logger.warning("BadRequest(noisy): %s — not retrying ctx=%s", e, context)
                return None
            logger.warning("BadRequest: %s — attempt %s/%s ctx=%s", e, attempt, max_attempts, context)

        except TelegramNetworkError as e:
            logger.warning("Network error: %s — attempt %s/%s ctx=%s", e, attempt, max_attempts, context)

        except Exception as e:
            # Не теряем стек в файле логов
            err_log.exception("ERR | Unexpected error in Telegram call ctx=%s err=%r", context, e)

        attempt += 1
        if attempt > max_attempts:
            logger.error("Giving up after %s attempts ctx=%s", max_attempts, context)
            return None

        await _backoff_sleep(attempt)


# Удобные обёртки + OUT/ERR логирование


async def safe_send_message(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    user_id: Optional[int] = None,
    **kwargs,
) -> Any:
    ctx = f"send_message chat_id={chat_id} user_id={user_id} text={_safe_text(text)} kwargs={_summarize_kwargs(kwargs)}"
    res = await call_with_retry(lambda: bot.send_message(chat_id, text, **kwargs), context=ctx)
    if res is not None:
        chat_log.info(
            "OUT | chat_id=%s user_id=%s text=%s",
            chat_id,
            user_id,
            _safe_text(text),
        )
    return res


async def safe_edit_text(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    user_id: Optional[int] = None,
    **kwargs,
) -> Any:
    # В aiogram v3 chat_id/message_id после text являются keyword-only.
    ctx = (
        f"edit_message_text chat_id={chat_id} message_id={message_id} user_id={user_id} "
        f"text={_safe_text(text)} kwargs={_summarize_kwargs(kwargs)}"
    )
    res = await call_with_retry(
        lambda: bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, **kwargs),
        context=ctx,
    )
    if res is not None:
        chat_log.info(
            "OUT | chat_id=%s user_id=%s edit_message_id=%s text=%s",
            chat_id,
            user_id,
            message_id,
            _safe_text(text),
        )
    return res


async def safe_delete_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    *,
    user_id: Optional[int] = None,
) -> Any:
    ctx = f"delete_message chat_id={chat_id} message_id={message_id} user_id={user_id}"
    res = await call_with_retry(lambda: bot.delete_message(chat_id, message_id), context=ctx)
    if res is not None:
        chat_log.info(
            "OUT | chat_id=%s user_id=%s delete_message_id=%s",
            chat_id,
            user_id,
            message_id,
        )
    return res


async def safe_bulk_delete(
    bot: Bot,
    chat_id: int,
    message_ids: Iterable[int],
    *,
    chunk_size: int = 20,
    user_id: Optional[int] = None,
) -> None:
    """
    Последовательно удаляем список сообщений бота.
    Telegram не даёт массовый delete в приватном чате — удаляем по одному,
    игнорируя «шумные» ошибки и устаревшие сообщения.

    chunk_size сохранён для совместимости (реального bulk-API нет), но можно использовать
    его как “yield control”, чтобы не держать loop на больших списках.
    """
    if not message_ids:
        return

    ids: List[int] = list(message_ids)
    ids.sort(reverse=True)

    processed = 0
    for mid in ids:
        # Для UX и стабильности — используем safe_delete_message (там уже “шумные” ошибки отфильтруются)
        try:
            await safe_delete_message(bot, chat_id, mid, user_id=user_id)
        except Exception:
            # Никогда не валим UX из-за delete
            err_log.exception("ERR | bulk_delete failed chat_id=%s message_id=%s user_id=%s", chat_id, mid, user_id)

        processed += 1
        if chunk_size > 0 and processed % chunk_size == 0:
            await asyncio.sleep(0)
