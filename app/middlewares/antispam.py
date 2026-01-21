from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Deque, Dict, Tuple
from collections import deque

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject
from aiogram.exceptions import TelegramBadRequest


@dataclass(frozen=True)
class Limits:
    msg_max: int = 6
    msg_window: float = 8.0
    msg_cooldown: float = 1.0

    cmd_max: int = 4
    cmd_window: float = 10.0
    cmd_cooldown: float = 0.8

    cb_max: int = 10
    cb_window: float = 8.0
    cb_cooldown: float = 0.6

    cb_same_ttl: float = 2.0


class MemoryBucket:
    def __init__(self):
        self._events: Dict[Tuple[int, str], Deque[float]] = {}
        self._seen_callbacks: Dict[Tuple[int, str], float] = {}
        self._last_cb_data: Dict[Tuple[int, str], float] = {}

    def push_and_check(self, key: Tuple[int, str], now: float, max_n: int, window: float) -> int:
        dq = self._events.setdefault(key, deque())
        cutoff = now - window
        while dq and dq[0] < cutoff:
            dq.popleft()
        dq.append(now)
        return len(dq)

    def seen_callback(self, user_id: int, cb_id: str, now: float, ttl: float) -> bool:
        k = (user_id, cb_id)
        ts = self._seen_callbacks.get(k)
        if ts and now - ts < ttl:
            return True
        self._seen_callbacks[k] = now
        return False

    def same_cb_too_soon(self, user_id: int, data: str, now: float, ttl: float) -> bool:
        k = (user_id, data)
        ts = self._last_cb_data.get(k)
        if ts and now - ts < ttl:
            return True
        self._last_cb_data[k] = now
        return False


class AntiSpamMiddleware(BaseMiddleware):
    def __init__(self, limits: Limits | None = None, bucket: MemoryBucket | None = None):
        super().__init__()
        self.limits = limits or Limits()
        self.bucket = bucket or MemoryBucket()
        self._last_warn: Dict[Tuple[int, str], float] = {}

    async def __call__(self, handler, event: TelegramObject, data: Dict):
        now = time.monotonic()

        if isinstance(event, Message):
            user_id = event.from_user.id if event.from_user else 0
            text = event.text or ""
            kind = "cmd" if text.startswith("/") else "msg"
            max_n = self.limits.cmd_max if kind == "cmd" else self.limits.msg_max
            window = self.limits.cmd_window if kind == "cmd" else self.limits.msg_window
            cooldown = self.limits.cmd_cooldown if kind == "cmd" else self.limits.msg_cooldown

            n = self.bucket.push_and_check((user_id, kind), now, max_n, window)
            if n > max_n:
                await self._warn_user(event, user_id, kind, now, window)
                return

            if n > 1:
                prev_key = (user_id, f"{kind}:last_ts")
                last_ts = self._last_warn.get(prev_key, 0.0)
                if now - last_ts < cooldown:
                    return
                self._last_warn[prev_key] = now

            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            user_id = event.from_user.id if event.from_user else 0
            cb_id = event.id
            data_str = event.data or ""

            if self.bucket.seen_callback(user_id, cb_id, now, ttl=10.0):
                return

            if data_str and self.bucket.same_cb_too_soon(user_id, data_str, now, self.limits.cb_same_ttl):
                try:
                    await event.answer("⏳ Подожди чуть-чуть…", show_alert=False)
                except TelegramBadRequest:
                    pass
                return

            n = self.bucket.push_and_check((user_id, "cb"), now, self.limits.cb_max, self.limits.cb_window)
            if n > self.limits.cb_max:
                try:
                    await event.answer("Слишком часто жмёшь. Попробуй позже.", show_alert=False)
                except TelegramBadRequest:
                    pass
                return

            prev_key = (user_id, "cb:last_ts")
            last_ts = self._last_warn.get(prev_key, 0.0)
            if now - last_ts < self.limits.cb_cooldown:
                return
            self._last_warn[prev_key] = now

            return await handler(event, data)

        return await handler(event, data)

    async def _warn_user(self, msg: Message, user_id: int, kind: str, now: float, window: float):
        key = (user_id, f"warn:{kind}")
        last = self._last_warn.get(key, 0.0)
        if now - last >= window / 2:
            try:
                await msg.answer("Слишком много запросов. Попробуй чуть позже.")
            except Exception:
                pass
            self._last_warn[key] = now
