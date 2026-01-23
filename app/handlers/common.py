# app/handlers/common.py

from __future__ import annotations

import logging
import re

import bcrypt
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.types import Message as TgMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.handlers.admin import _show_admin_panel, is_admin
from app.keyboards import profile_menu_kb, reg_confirm_kb, user_main_menu
from app.middlewares.auth import RequireAuthMiddleware
from app.models import User
from app.states import AuthState, Registration
from app.telegram_safe import safe_bulk_delete
from app.utils.media import drain_bot_messages, register_bot_message

router = Router(name="common")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# UI helpers (re-send UX for auth/registration)
#   Требование: при ошибках и шагах отправлять НОВОЕ сообщение и удалять старое,
#   чтобы экран всегда был внизу.
# ---------------------------------------------------------------------


async def _clear_bot_ui(bot: Bot, chat_id: int, uid: int) -> None:
    """
    Удаляет предыдущие сообщения бота, зарегистрированные через register_bot_message().
    Используется для экранов логина/регистрации, чтобы не плодить флуд.
    """
    mids = drain_bot_messages(uid)
    if mids:
        await safe_bulk_delete(bot, chat_id, mids)


async def _ui_set(state: FSMContext, msg_id: int | None) -> None:
    await state.update_data(ui_msg_id=msg_id)


async def _ui_get(state: FSMContext) -> int | None:
    data = await state.get_data()
    mid = data.get("ui_msg_id")
    try:
        return int(mid) if mid else None
    except Exception:
        return None


async def _ui_delete_only(*, bot: Bot, chat_id: int, state: FSMContext) -> None:
    """
    Удаляет текущее ui-сообщение (если было) и очищает ui_msg_id в state.
    Не трогает drain_bot_messages — это общий список, его чистит _clear_bot_ui().
    """
    mid = await _ui_get(state)
    if not mid:
        return
    try:
        await safe_bulk_delete(bot, chat_id, [mid])
    finally:
        await _ui_set(state, None)


async def _ui_send_replace(
    *,
    bot: Bot,
    state: FSMContext,
    chat_id: int,
    text: str,
    reply_markup=None,
) -> int:
    """
    Гарантирует: новый экран = НОВОЕ сообщение внизу.
    Делает:
      1) удаляет предыдущий ui_msg_id (если есть)
      2) отправляет новое сообщение
      3) сохраняет новый ui_msg_id
      4) регистрирует в register_bot_message (для общей чистки при /start/выходе)
    """
    old = await _ui_get(state)
    if old:
        await safe_bulk_delete(bot, chat_id, [old])

    sent = await bot.send_message(chat_id, text, reply_markup=reply_markup)
    await _ui_set(state, sent.message_id)
    register_bot_message(chat_id, sent.message_id)
    return sent.message_id


# ---------------------------------------------------------------------
# Validators / DB helpers
# ---------------------------------------------------------------------


def _check_pass_with_fallback(plain: str) -> bool:
    hashed = (settings.PASS_PHRASE_HASH or "").strip()
    if hashed:
        try:
            return bool(bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8")))
        except Exception:
            log.exception("bcrypt check failed")
            return False

    legacy = (settings.PASS_PHRASE or "").strip()
    if not legacy:
        return False
    return plain == legacy


def _valid_name(s: str) -> bool:
    s = s.strip()
    if len(s) < 3 or len(s) > 100:
        return False
    parts = [p for p in re.split(r"\s+", s) if p]
    return len(parts) >= 2


def _valid_sip(s: str) -> bool:
    return bool(re.fullmatch(r"\d{3}", s.strip()))


async def _get_or_create_user(session: AsyncSession, tg_id: int, full_name: str) -> User:
    res = await session.execute(select(User).where(User.tg_id == tg_id))
    user = res.scalars().first()
    if user:
        if full_name and user.full_name != full_name:
            user.full_name = full_name
            await session.commit()
        return user

    user = User(tg_id=tg_id, full_name=full_name or "", is_authenticated=False)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


# ---------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    uid = message.from_user.id if message.from_user else None
    if uid is not None and is_admin(uid):
        await message.answer("Админ-команды:\n• /admin — лоховская панель\n• /help — помощь\n• /boss — крутая панель")
        return

    await message.answer(
        "🧰 HardyBot — как пользоваться\n\n"
        "1️⃣ Регистрация\n"
        "• При первом запуске бот попросит твоё ФИО и добавочный (SIP).\n"
        "• Если ошибся — открой 👤 Профиль → ✏️ Изменить ФИО / ☎️ Изменить SIP.\n\n"
        "2️⃣ Главное меню\n"
        "• ✉️ Отправить задачу — создать новую заявку в IT.\n"
        "• 📚 История задач — посмотреть все свои заявки и их статусы.\n"
        "• 👤 Профиль — проверить/исправить ФИО и SIP.\n\n"
        "3️⃣ Новая заявка\n"
        "• 🗂️ Выбери категорию:\n (Интернет, Принтер, Компьютер, 1С, ЭЦП, Удалёнка, Пропуск, Дверь, Другое).\n"
        "• 📝 Кратко опиши проблему: что не работает, что уже пробовал.\n"
        "• 📎 Прикрепи при необходимости файлы: фото/скриншоты, видео, документы, голосовые.\n"
        "• ✅ Подтверди отправку — заявка уйдёт напрямую специалистам.\n\n"
        "4️⃣ Полезно знать\n"
        "• 💡 Чем точнее описание и больше контекста (скрины, номера ошибок), тем быстрее решение.\n"
        "• 🚨 Срочно? В начале описания напиши «[СРОЧНО]» и укажи причину.\n\n"
        "⌨️ Команды\n"
        "• /start — перезапустить диалог с ботом\n"
        "• /help — эта подсказка\n\n"
        "🧹 Рекомендация\n"
        "• Чтобы не мешались старые сообщения, очисти переписку с ботом и заново нажми /start.\n"
    )


# ---------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------


@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot, session: AsyncSession, state: FSMContext) -> None:
    if not message.from_user:
        await message.answer("Пожалуйста, напишите мне из личного чата.")
        return

    uid = message.from_user.id
    full_name = message.from_user.full_name or ""

    # Персонал — сразу в админку
    if is_admin(uid):
        await state.clear()
        await _show_admin_panel(bot, uid)
        return

    user = await _get_or_create_user(session, uid, full_name)

    # Если уже авторизован и профиль ок — просто меню
    if user.is_authenticated and user.profile_completed and user.sip_ext and len(user.sip_ext) == 3:
        await state.clear()
        await _clear_bot_ui(bot, message.chat.id, uid)
        sent = await message.answer("Готово ✅", reply_markup=user_main_menu())
        register_bot_message(uid, sent.message_id)
        return

    # Если авторизован, но профиля нет — регистрация
    if user.is_authenticated:
        await _start_registration(message, state, bot)
        return

    # Иначе — вход по паролю (экран внизу)
    await state.set_state(AuthState.waiting_passphrase)
    await state.update_data(reg_name=None, reg_sip=None)
    await _clear_bot_ui(bot, message.chat.id, uid)
    await _ui_set(state, None)

    await _ui_send_replace(
        bot=bot,
        state=state,
        chat_id=message.chat.id,
        text=(
            "🔐 <b>Вход в Hardy Helpdesk</b>\n\n"
            "Введите пароль для входа.\n"
            "Если ошиблись — просто отправьте правильный ещё раз.\n"
        ),
    )


# ---------------------------------------------------------------------
# Auth step: passphrase
# ---------------------------------------------------------------------


@router.message(AuthState.waiting_passphrase, F.text)
async def auth_check(message: Message, session: AsyncSession, state: FSMContext, bot: Bot) -> None:
    if not message.from_user:
        await message.answer("Пожалуйста, отправьте команду из личного чата.")
        await state.clear()
        return

    uid = message.from_user.id
    text = (message.text or "").strip()

    if not _check_pass_with_fallback(text):
        RequireAuthMiddleware.register_fail(uid)
        await _ui_send_replace(
            bot=bot,
            state=state,
            chat_id=message.chat.id,
            text=(
                "❌ <b>Пароль неверный.</b>\n"
                "Попробуйте ещё раз.\n\n"
                "🔐 <b>Вход в Hardy Helpdesk</b>\n"
                "Введите пароль для входа."
            ),
        )
        return

    RequireAuthMiddleware.clear(uid)

    res = await session.execute(select(User).where(User.tg_id == uid))
    user = res.scalars().first()
    if user:
        user.is_authenticated = True
        await session.commit()

    if user and not (user.profile_completed and user.sip_ext and len(user.sip_ext) == 3):
        await _start_registration(message, state, bot)
        return

    await state.clear()
    await _clear_bot_ui(bot, message.chat.id, uid)
    await _ui_set(state, None)
    sent = await message.answer("Доступ разрешён ✅", reply_markup=user_main_menu())
    register_bot_message(uid, sent.message_id)


# ---------------------------------------------------------------------
# Registration (re-send UX)
# ---------------------------------------------------------------------


async def _start_registration(message: Message, state: FSMContext, bot: Bot) -> None:
    uid = message.from_user.id if message.from_user else message.chat.id

    await state.clear()
    await state.set_state(Registration.ask_full_name)
    await state.update_data(reg_name=None, reg_sip=None)

    await _clear_bot_ui(bot, message.chat.id, uid)
    await _ui_set(state, None)

    await _ui_send_replace(
        bot=bot,
        state=state,
        chat_id=message.chat.id,
        text=(
            "👋 Добро пожаловать!\n\n"
            "Для работы укажи, пожалуйста, свои данные.\n\n"
            "👤 <b>Шаг 1 — ФИО</b>\n\n"
            "Напиши фамилию и имя \n(при желании — отчество).\n"
            "Пример: Иванов Иван\n\n"
            "❗ Требования: минимум 2 слова. ❗"
        ),
    )


@router.message(Registration.ask_full_name, F.text)
async def reg_full_name(message: Message, state: FSMContext, bot: Bot) -> None:
    name = (message.text or "").strip()

    if not _valid_name(name):
        # ВАЖНО: переотправляем экран, чтобы был внизу
        await _ui_send_replace(
            bot=bot,
            state=state,
            chat_id=message.chat.id,
            text=(
                "🤔 <b>Похоже на опечатку.</b> Нужно минимум 2 слова.\n"
                "Пример: <i>Петров Пётр</i>\n\n"
                "👤 <b>Шаг 1 — ФИО</b>\n"
                "Напиши фамилию и имя (при желании — отчество)."
            ),
        )
        return

    await state.update_data(reg_name=name)
    await state.set_state(Registration.ask_sip)

    await _ui_send_replace(
        bot=bot,
        state=state,
        chat_id=message.chat.id,
        text=(
            "☎️ <b>Шаг 2 — SIP-добавочный</b>\n"
            "❗ Введи <b>ровно 3 цифры</b>. Пример: 505 ❗"
        ),
    )


@router.message(Registration.ask_sip, F.text)
async def reg_sip(message: Message, state: FSMContext, bot: Bot) -> None:
    sip = (message.text or "").strip()

    if not _valid_sip(sip):
        await _ui_send_replace(
            bot=bot,
            state=state,
            chat_id=message.chat.id,
            text=(
                "⚠️ <b>SIP должен состоять ровно из 3 цифр</b>, например 505.\n\n"
                "☎️ <b>Шаг 2 — SIP-добавочный</b>\n"
                "❗ Введи <b>ровно 3 цифры</b>. Пример: 505 ❗"
            ),
        )
        return

    await state.update_data(reg_sip=sip)
    await state.set_state(Registration.confirm)

    data = await state.get_data()
    name = data.get("reg_name") or "—"

    await _ui_send_replace(
        bot=bot,
        state=state,
        chat_id=message.chat.id,
        text=(
            "🧾 <b>Шаг 3 — Подтверждение</b>\n\n"
            f"👤 ФИО: <b>{name}</b>\n"
            f"☎️ SIP: <b>{sip}</b>\n\n"
            "Если всё верно — нажми «✅Подтвердить». Если нужно поправить — выбери, что изменить."
        ),
        reply_markup=reg_confirm_kb(),
    )


@router.callback_query(Registration.confirm, F.data == "reg:edit_name")
async def reg_edit_name(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.set_state(Registration.ask_full_name)

    await _ui_send_replace(
        bot=bot,
        state=state,
        chat_id=cb.from_user.id,
        text=(
            "✏️ <b>Изменение ФИО</b>\n\n"
            "Введи ФИО ещё раз.\n"
            "Пример: Иванов Иван"
        ),
    )
    await cb.answer()


@router.callback_query(Registration.confirm, F.data == "reg:edit_sip")
async def reg_edit_sip(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.set_state(Registration.ask_sip)

    await _ui_send_replace(
        bot=bot,
        state=state,
        chat_id=cb.from_user.id,
        text=(
            "✏️ <b>Изменение SIP</b>\n\n"
            "Введи SIP (3 цифры).\n"
            "Пример: 505"
        ),
    )
    await cb.answer()


@router.callback_query(Registration.confirm, F.data == "reg:cancel")
async def reg_cancel(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    uid = cb.from_user.id
    await state.clear()

    await _clear_bot_ui(bot, uid, uid)
    await _ui_set(state, None)

    sent = await bot.send_message(uid, "Регистрация отменена. Вернуться можно командой /start.")
    register_bot_message(uid, sent.message_id)
    await cb.answer()


@router.callback_query(Registration.confirm, F.data == "reg:confirm")
async def reg_confirm(cb: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot) -> None:
    uid = cb.from_user.id

    data = await state.get_data()
    name = (data.get("reg_name") or "").strip()
    sip = (data.get("reg_sip") or "").strip()

    if not (_valid_name(name) and _valid_sip(sip)):
        await _ui_send_replace(
            bot=bot,
            state=state,
            chat_id=uid,
            text="⚠️ Данные некорректны. Начни заново: /start",
        )
        await cb.answer("Данные некорректны", show_alert=True)
        await state.clear()
        return

    res = await session.execute(select(User).where(User.tg_id == uid))
    u = res.scalars().first()
    if not u:
        u = User(tg_id=uid, full_name=name, is_authenticated=True)
        session.add(u)
        await session.flush()

    u.full_name = name
    u.sip_ext = sip
    u.profile_completed = True
    await session.commit()

    await state.clear()

    # Удаляем экраны регистрации (включая последний ui_msg_id)
    await _clear_bot_ui(bot, uid, uid)
    await _ui_set(state, None)

    await cb.answer("Сохранено ✅")

    sent = await bot.send_message(
        uid,
        "🎉 Профиль сохранён! Добро пожаловать 👋\n"
        "Выберите действие в меню ниже. Нажмите /help, чтобы узнать как пользоваться ботом.",
        reply_markup=user_main_menu(),
    )
    register_bot_message(uid, sent.message_id)


# ---------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------


@router.callback_query(F.data == "u:profile")
async def profile_open(cb: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    res = await session.execute(select(User).where(User.tg_id == cb.from_user.id))
    u = res.scalars().first()

    name = (u.full_name if u and u.full_name else "—")
    sip = (u.sip_ext if u and u.sip_ext else "—")

    text = f"👤 <b>Профиль</b>\nФИО: <b>{name}</b>\nSIP: <b>{sip}</b>"

    msg = cb.message
    if isinstance(msg, TgMessage):
        try:
            await msg.edit_text(text, reply_markup=profile_menu_kb())
        except TelegramBadRequest:
            await msg.answer(text, reply_markup=profile_menu_kb())
    else:
        await bot.send_message(cb.from_user.id, text, reply_markup=profile_menu_kb())
    await cb.answer()


@router.callback_query(F.data == "u:menu")
async def back_to_menu(cb: CallbackQuery, bot: Bot) -> None:
    msg = cb.message
    if isinstance(msg, TgMessage):
        try:
            await msg.edit_text("📱Главное меню:", reply_markup=user_main_menu())
        except TelegramBadRequest:
            await msg.answer("📱Главное меню:", reply_markup=user_main_menu())
    else:
        await bot.send_message(cb.from_user.id, "📱Главное меню:", reply_markup=user_main_menu())
    await cb.answer()


@router.callback_query(F.data == "u:profile:edit_name")
async def profile_edit_name(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.set_state(Registration.ask_full_name)
    msg = cb.message
    if isinstance(msg, TgMessage):
        await msg.answer("👤 Введи новое ФИО:")
    else:
        await bot.send_message(cb.from_user.id, "👤 Введи новое ФИО:")
    await cb.answer()


@router.callback_query(F.data == "u:profile:edit_sip")
async def profile_edit_sip(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.set_state(Registration.ask_sip)
    msg = cb.message
    if isinstance(msg, TgMessage):
        await msg.answer("☎️ Введи новый SIP (3 цифры):")
    else:
        await bot.send_message(cb.from_user.id, "☎️ Введи новый SIP (3 цифры):")
    await cb.answer()
