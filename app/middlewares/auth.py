# app/middlewares/auth.py

from __future__ import annotations

from typing import Callable, Awaitable, Dict, Any, Optional, Tuple
import time

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.models import User as UserModel


class _AuthBrute:
    """
    Простой in-memory учёт брутфорса: user_id -> (fails, unlock_ts)
    """
    FAILS: Dict[int, Tuple[int, float]] = {}

    @classmethod
    def status(cls, uid: int) -> Tuple[int, float]:
        return cls.FAILS.get(uid, (0, 0.0))

    @classmethod
    def register_fail(cls, uid: int) -> None:
        fails, _ = cls.FAILS.get(uid, (0, 0.0))
        fails += 1
        if fails >= max(1, settings.AUTH_MAX_FAILS):
            cls.FAILS[uid] = (0, time.time() + max(1, settings.AUTH_BAN_MINUTES) * 60)
        else:
            cls.FAILS[uid] = (fails, 0.0)

    @classmethod
    def clear(cls, uid: int) -> None:
        cls.FAILS.pop(uid, None)


class AuthMiddleware(BaseMiddleware):
    """
    Персонал (оба админа + босс) — без пароля.
    Остальным разрешаем /start, /help, /admin, /boss; всё остальное — только после авторизации.
    Также блокируем ввод пароля при активном бане после нескольких неудач.
    """

    def __init__(self, session_factory: Optional[Callable[[], AsyncSession]] = None) -> None:
        super().__init__()
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: Dict[str, Any],
    ) -> Any:
        tg_user = getattr(event, "from_user", None)
        if not tg_user:
            return await handler(event, data)

        uid = int(tg_user.id)
        session: Optional[AsyncSession] = data.get("session")
        fsm: Optional[FSMContext] = data.get("state")

        # текущее состояние FSM
        state_name: Optional[str] = None
        if fsm:
            try:
                state_name = await fsm.get_state()
            except Exception:
                state_name = None

        # 0) Блокировка при бане (даже на шаге ввода пароля)
        fails, unlock = _AuthBrute.status(uid)
        if unlock and time.time() < unlock:
            # Стараемся не спамить; но тут допускаемо одно короткое сообщение
            txt = "Доступ временно ограничен. Попробуй позже."
            if isinstance(event, Message):
                await event.answer(txt)
            else:
                await event.answer(txt, show_alert=False)
            return

        # 1) Персонал проходит без пароля (+ помечаем авторизованным в БД)
        if uid in settings.staff_ids:
            if session:
                res = await session.execute(select(UserModel).where(UserModel.tg_id == uid))
                dbu = res.scalars().first()
                if not dbu:
                    session.add(
                        UserModel(
                            tg_id=uid,
                            full_name=tg_user.full_name or "",
                            is_authenticated=True,
                        )
                    )
                    await session.commit()
                elif not dbu.is_authenticated:
                    dbu.is_authenticated = True
                    await session.commit()
            return await handler(event, data)

        # 2) Если пользователь вводит пароль — не мешаем, пропускаем в хендлер шага AuthState.*
        if isinstance(event, Message) and state_name and state_name.startswith("AuthState"):
            return await handler(event, data)

        # 3) Разрешаем базовые команды
        if isinstance(event, Message):
            txt = (event.text or "").strip().lower()
            if txt.startswith(("/start", "/help", "/admin", "/boss")):
                return await handler(event, data)

        # 4) Уже авторизован?
        if session:
            res = await session.execute(select(UserModel).where(UserModel.tg_id == uid))
            dbu = res.scalars().first()
            if dbu and dbu.is_authenticated:
                return await handler(event, data)

        # 5) Требуем пройти аутентификацию
        msg = (
            "🔒 Нужно войти.\n"
            "Отправь /start и введи пароль."
        )
        if isinstance(event, Message):
            await event.answer(msg)
        else:
            # тихий ответ на колбэк без алерта
            await event.answer("Нужно войти: /start → пароль", show_alert=False)
        return


# Совместимость со старым импортом и доступ к счетчику
class RequireAuthMiddleware(AuthMiddleware):
    @staticmethod
    def register_fail(uid: int) -> None:
        _AuthBrute.register_fail(uid)

    @staticmethod
    def clear(uid: int) -> None:
        _AuthBrute.clear(uid)


class RequireProfileMiddleware(BaseMiddleware):
    """
    Блокирует любые действия, если профиль пользователя не заполнен (full_name + sip_ext).
    Пропускает:
      • /start, /help
      • шаги FSM Registration.* (регистрация профиля)
      • шаги FSM AuthState.* (ввод пароля)
      • персонал (staff)
      • админ/босс панели (/admin, /boss)
    Также есть анти-спам: одно предупреждение раз в warn_window секунд.
    """

    def __init__(self, warn_window: float = 20.0) -> None:
        super().__init__()
        self.warn_window = warn_window
        # user_id -> last_warn_ts
        self._last_warn: Dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: Dict[str, Any],
    ) -> Any:
        tg_user = getattr(event, "from_user", None)
        if not tg_user:
            return await handler(event, data)

        uid = int(tg_user.id)
        # персонал — без проверки профиля
        if uid in settings.staff_ids:
            return await handler(event, data)

        session: Optional[AsyncSession] = data.get("session")
        state: Optional[FSMContext] = data.get("state")

        # Разрешаем базовые команды
        if isinstance(event, Message):
            low = (event.text or "").strip().lower()
            if low.startswith(("/start", "/help")):
                return await handler(event, data)

        # Разрешаем шаги FSM регистрации и аутентификации
        if state:
            try:
                st = await state.get_state()
            except Exception:
                st = None
            if st and (st.startswith("Registration") or st.startswith("AuthState")):
                return await handler(event, data)

        # Разрешаем админ/босс панели
        if isinstance(event, Message):
            low = (event.text or "").strip().lower()
            if low.startswith(("/admin", "/boss")):
                return await handler(event, data)

        # Проверяем профиль
        if not session:
            return await handler(event, data)

        res = await session.execute(select(UserModel).where(UserModel.tg_id == uid))
        u = res.scalars().first()
        if u and u.is_authenticated and u.profile_completed and (u.sip_ext and len(u.sip_ext) == 3):
            return await handler(event, data)

        # Требуем заполнить профиль (анти-спам на повторные подсказки)
        now = time.monotonic()
        last = self._last_warn.get(uid, 0.0)
        if now - last >= self.warn_window:
            text = (
                "📝 Нужно заполнить профиль, чтобы продолжить.\n"
                "Отправь /start и следуй подсказкам: сначала ФИО, затем SIP (3 цифры)."
            )
            if isinstance(event, Message):
                await event.answer(text)
            else:
                await event.answer("Заполни профиль: /start → ФИО → SIP (3 цифры)", show_alert=False)
            self._last_warn[uid] = now

        return
