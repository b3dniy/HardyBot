# app\handlers\common.py

from __future__ import annotations
import re
import logging

import bcrypt
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.types import Message as TgMessage
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.models import User
from app.keyboards import user_main_menu, profile_menu_kb, reg_confirm_kb
from app.handlers.admin import is_admin, _show_admin_panel
from app.telegram_safe import safe_bulk_delete
from app.utils.media import register_bot_message, drain_bot_messages
from app.states import AuthState, Registration  # общие состояния
from app.middlewares.auth import RequireAuthMiddleware  # для учёта фейлов/сброса

router = Router(name="common")
log = logging.getLogger(__name__)


# -------- /help --------
@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    uid = message.from_user.id if message.from_user else None
    if uid is not None and is_admin(uid):
        await message.answer("Админ-команды:\n• /admin — лоховская панель\n• /help — помощь\n• /boss — крутая панель")
    else:
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


# -------- /start --------
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

    # Уже авторизован?
    if user.is_authenticated:
        # Профиль заполнен?
        if user.profile_completed and user.sip_ext and len(user.sip_ext) == 3:
            await state.clear()
            mids = drain_bot_messages(uid)
            await safe_bulk_delete(bot, message.chat.id, mids)
            sent = await message.answer("Готово ✅", reply_markup=user_main_menu())
            register_bot_message(uid, sent.message_id)
            return
        # Профиль не заполнен — шаг 2 (ФИО)
        await _start_registration(message, state, user)
        return

    # Шаг 0: вход по паролю
    await state.set_state(AuthState.waiting_passphrase)
    text = (
        "🔐 <b>Вход в Hardy Helpdesk</b>\n\n"
        "Введите пароль для входа.\n"
        "Если ошиблись — просто отправьте правильный ещё раз.\n\n"
    )
    sent = await message.answer(text)
    register_bot_message(uid, sent.message_id)


def _check_pass_with_fallback(plain: str) -> bool:
    """
    Основная проверка: bcrypt по PASS_PHRASE_HASH.
    Фолбэк (временная совместимость): сравнение со старым PASS_PHRASE, если хэш не задан.
    """
    hashed = (settings.PASS_PHRASE_HASH or "").strip()
    if hashed:
        try:
            ok = bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
            return bool(ok)
        except Exception:
            log.exception("bcrypt check failed")
            return False
    # fallback на старую схему — лучше выключить в проде
    legacy = (settings.PASS_PHRASE or "").strip()
    if not legacy:
        return False
    return plain == legacy


# -------- Шаг 1. Проверка пароля --------
@router.message(AuthState.waiting_passphrase, F.text)
async def auth_check(message: Message, session: AsyncSession, state: FSMContext, bot: Bot) -> None:
    if not message.from_user:
        await message.answer("Пожалуйста, отправьте команду из личного чата.")
        await state.clear()
        return

    uid = message.from_user.id
    text = (message.text or "").strip()

    if not _check_pass_with_fallback(text):
        # регистрируем неудачу и отвечаем без деталей
        RequireAuthMiddleware.register_fail(uid)
        sent = await message.answer(
            "❌ Пароль не верный.\n"
            "Попробуйте ещё раз."
        )
        register_bot_message(uid, sent.message_id)
        return

    # успех — обнуляем счётчик брутфорса
    RequireAuthMiddleware.clear(uid)

    # отметим пользователя авторизованным
    res = await session.execute(select(User).where(User.tg_id == uid))
    user = res.scalars().first()
    if user:
        user.is_authenticated = True
        await session.commit()

    # если профиль не заполнен — сразу к шагу ФИО
    if user and not (user.profile_completed and user.sip_ext and len(user.sip_ext) == 3):
        await _start_registration(message, state, user)
        return

    # иначе — в меню
    await state.clear()
    mids = drain_bot_messages(uid)
    await safe_bulk_delete(bot, message.chat.id, mids)
    sent = await message.answer("Доступ разрешён ✅", reply_markup=user_main_menu())
    register_bot_message(uid, sent.message_id)


# ================= Регистрация профиля (Шаг 1–3) =================

async def _start_registration(message: Message, state: FSMContext, user: User) -> None:
    await state.clear()
    await state.set_state(Registration.ask_full_name)
    await state.update_data(reg_name=None, reg_sip=None)
    hint = await message.answer(
        "👋 Добро пожаловать!\n\n"
        "Для работы укажи, пожалуйста, свои данные.\n\n"
        "👤 <b>Шаг 1 — ФИО</b>\n\n"
        "Напиши фамилию и имя \n(при желании — отчество).\n"
        "Пример: Иванов Иван\n\n"
        "❗ Требования: минимум 2 слова. ❗"
    )
    # безопасно определяем uid (Pylance не уверен в from_user)
    uid = message.from_user.id if message.from_user else message.chat.id
    register_bot_message(uid, hint.message_id)


def _valid_name(s: str) -> bool:
    s = s.strip()
    if len(s) < 3 or len(s) > 100:
        return False
    parts = [p for p in re.split(r"\s+", s) if p]
    return len(parts) >= 2


def _valid_sip(s: str) -> bool:
    return bool(re.fullmatch(r"\d{3}", s.strip()))


@router.message(Registration.ask_full_name, F.text)
async def reg_full_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not _valid_name(name):
        await message.answer("🤔 Выглядит как опечатка. Нужно минимум 2 слова, например: Петров Пётр")
        return
    await state.update_data(reg_name=name)
    await state.set_state(Registration.ask_sip)
    await message.answer(
        "☎️ <b>Шаг 2 — SIP-добавочный</b>\n"
        "❗ Введи <b>ровно 3 цифры</b>. Пример: 505 ❗"
    )


@router.message(Registration.ask_sip, F.text)
async def reg_sip(message: Message, state: FSMContext):
    sip = (message.text or "").strip()
    if not _valid_sip(sip):
        await message.answer("⚠️ SIP должен состоять <b>ровно из 3 цифр</b>, например 505.")
        return
    await state.update_data(reg_sip=sip)
    await state.set_state(Registration.confirm)
    data = await state.get_data()
    name = data.get("reg_name") or "—"
    await message.answer(
        "🧾 <b>Шаг 3 — Подтверждение</b>\n\n"
        f"👤 ФИО: <b>{name}</b>\n"
        f"☎️ SIP: <b>{sip}</b>\n\n"
        "Если всё верно — нажми «✅Подтвердить». Если нужно поправить — выбери, что изменить.",
        reply_markup=reg_confirm_kb()
    )


@router.callback_query(Registration.confirm, F.data == "reg:edit_name")
async def reg_edit_name(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(Registration.ask_full_name)
    msg = cb.message
    if isinstance(msg, TgMessage):
        await msg.answer("✏️ Ок, введи ФИО ещё раз.")
    else:
        await bot.send_message(cb.from_user.id, "✏️ Ок, введи ФИО ещё раз.")
    await cb.answer()


@router.callback_query(Registration.confirm, F.data == "reg:edit_sip")
async def reg_edit_sip(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(Registration.ask_sip)
    msg = cb.message
    if isinstance(msg, TgMessage):
        await msg.answer("✏️ Ок, введи SIP (3 цифры).")
    else:
        await bot.send_message(cb.from_user.id, "✏️ Ок, введи SIP (3 цифры).")
    await cb.answer()


@router.callback_query(Registration.confirm, F.data == "reg:cancel")
async def reg_cancel(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    msg = cb.message
    if isinstance(msg, TgMessage):
        await msg.answer("Регистрация отменена. Вернуться можно командой /start.")
    else:
        await bot.send_message(cb.from_user.id, "Регистрация отменена. Вернуться можно командой /start.")
    await cb.answer()


@router.callback_query(Registration.confirm, F.data == "reg:confirm")
async def reg_confirm(cb: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot):
    data = await state.get_data()
    name = (data.get("reg_name") or "").strip()
    sip = (data.get("reg_sip") or "").strip()
    if not (_valid_name(name) and _valid_sip(sip)):
        await cb.answer("Данные некорректны, начни заново /start", show_alert=True)
        await state.clear()
        return

    res = await session.execute(select(User).where(User.tg_id == cb.from_user.id))
    u = res.scalars().first()
    if not u:
        u = User(tg_id=cb.from_user.id, full_name=name, is_authenticated=True)
        session.add(u)
        await session.flush()

    u.full_name = name
    u.sip_ext = sip
    u.profile_completed = True
    await session.commit()

    await state.clear()
    await cb.answer("Сохранено ✅")

    sent = await bot.send_message(
        cb.from_user.id,
        "🎉 Профиль сохранён! Добро пожаловать 👋\nВыберите действие в меню ниже. Нажмите /help, чтобы узнать как пользоваться ботом.",
        reply_markup=user_main_menu(),
    )
    register_bot_message(cb.from_user.id, sent.message_id)


# -------- Профиль --------

@router.callback_query(F.data == "u:profile")
async def profile_open(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    res = await session.execute(select(User).where(User.tg_id == cb.from_user.id))
    u = res.scalars().first()
    name = (u.full_name if u and u.full_name else "—")
    sip = (u.sip_ext if u and u.sip_ext else "—")
    text = (
        f"👤 <b>Профиль</b>\n"
        f"ФИО: <b>{name}</b>\n"
        f"SIP: <b>{sip}</b>"
    )
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
async def back_to_menu(cb: CallbackQuery, bot: Bot):
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
async def profile_edit_name(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(Registration.ask_full_name)
    msg = cb.message
    if isinstance(msg, TgMessage):
        await msg.answer("👤 Введи новое ФИО:")
    else:
        await bot.send_message(cb.from_user.id, "👤 Введи новое ФИО:")
    await cb.answer()


@router.callback_query(F.data == "u:profile:edit_sip")
async def profile_edit_sip(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(Registration.ask_sip)
    msg = cb.message
    if isinstance(msg, TgMessage):
        await msg.answer("☎️ Введи новый SIP (3 цифры):")
    else:
        await bot.send_message(cb.from_user.id, "☎️ Введи новый SIP (3 цифры):")
    await cb.answer()


# -------- Вспомогательное --------

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
