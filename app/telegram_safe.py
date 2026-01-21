# app/telegram_safe.py
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Callable, Awaitable, Iterable, List

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram import Bot

logger = logging.getLogger(__name__)


async def _backoff_sleep(attempt: int) -> None:
    # 0.4, 0.8, 1.6, 3.2... + jitter
    base = min(0.4 * (2 ** (attempt - 1)), 5.0)
    await asyncio.sleep(base + random.uniform(0, 0.25))


async def call_with_retry(
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    max_attempts: int = 5,
) -> Any:
    """
    Универсальный ретрай вызова Telegram API.
    coro_factory — лямбда, которая создаёт корутину вызова метода.
    """
    attempt = 1
    while True:
        try:
            return await coro_factory()
        except TelegramRetryAfter as e:
            retry = float(getattr(e, "retry_after", 1.0) or 1.0)
            logger.warning("RetryAfter: sleep %.2fs (attempt %s/%s)", retry, attempt, max_attempts)
            await asyncio.sleep(retry + 0.2)
        except TelegramForbiddenError as e:
            logger.info("Forbidden: %s — skip further attempts", e)
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
                logger.warning("BadRequest(noisy): %s — not retrying", e)
                return None
            logger.warning("BadRequest: %s — attempt %s/%s", e, attempt, max_attempts)
        except TelegramNetworkError as e:
            logger.warning("Network error: %s — attempt %s/%s", e, attempt, max_attempts)
        except Exception as e:
            logger.error("Unexpected error: %r — attempt %s/%s", e, attempt, max_attempts)
        attempt += 1
        if attempt > max_attempts:
            logger.error("Giving up after %s attempts", max_attempts)
            return None
        await _backoff_sleep(attempt)


# Удобные обёртки

async def safe_send_message(bot: Bot, chat_id: int, text: str, **kwargs) -> Any:
    return await call_with_retry(lambda: bot.send_message(chat_id, text, **kwargs))


async def safe_edit_text(bot: Bot, chat_id: int, message_id: int, text: str, **kwargs) -> Any:
    # В aiogram v3 chat_id/message_id после text являются keyword-only.
    return await call_with_retry(
        lambda: bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, **kwargs)
    )


async def safe_delete_message(bot: Bot, chat_id: int, message_id: int) -> Any:
    return await call_with_retry(lambda: bot.delete_message(chat_id, message_id))


async def safe_bulk_delete(bot: Bot, chat_id: int, message_ids: Iterable[int], *, chunk_size: int = 20) -> None:
    """
    Последовательно удаляем список сообщений бота.
    Telegram не даёт массовый delete в приватном чате — удаляем по одному,
    игнорируя «шумные» ошибки и устаревшие сообщения.
    """
    if not message_ids:
        return
    # Удаляем от самых свежих — так вероятность успеха выше
    ids: List[int] = list(message_ids)
    ids.sort(reverse=True)
    for i in ids:
        try:
            await bot.delete_message(chat_id, i)
        except Exception:
            # Глотаем, чтобы не мешать UX
            continue
