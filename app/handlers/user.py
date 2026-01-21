# app/handlers/user.py
from __future__ import annotations

from typing import Union, List, Tuple, Optional, Dict, Sequence

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    CallbackQuery,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InlineKeyboardMarkup,
)
# noqa
from aiogram.types import Message as TgMessage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.enums import Status, Priority
from app.keyboards import (
    user_main_menu,
    categories_kb,
    done_cancel_kb,
    cancel_only_kb,
    admin_accept_kb,
    USER_CATEGORIES,
    STATUS_EMOJI,
)
from app.models import Task, Attachment, User
from app.services.assignment import assign_by_category, InMemoryNotifications
from app.states import TicketState
from app.utils.media import DRAFTS, DraftData, register_bot_message, drain_bot_messages
from app.telegram_safe import safe_bulk_delete

router = Router(name="user")

# ===================== Вспомогательные =====================

def _slug_to_title(slug: str) -> str:
    for title, _emoji, s in USER_CATEGORIES:
        if s == slug:
            return title
    return slug

def _slug_to_category_and_emoji(slug: str) -> tuple[str, str]:
    for title, emoji, s in USER_CATEGORIES:
        if s == slug:
            return title, emoji
    return slug, ""

def _status_label(status_value: str) -> str:
    return STATUS_EMOJI.get(status_value, status_value)

# ——— трекинг отправленных пользователю медиа/сообщений при просмотре заявки
USER_VIEWER: Dict[int, List[int]] = {}

async def _clear_user_viewer(bot: Bot, user_id: int) -> None:
    ids = USER_VIEWER.pop(user_id, None)
    if not ids:
        return
    for mid in ids:
        try:
            await bot.delete_message(user_id, mid)
        except Exception:
            pass

# ===================== Главное меню =====================

@router.message(Command("menu"))
async def cmd_menu(message: Message, bot: Bot):
    # при переходе в меню чистим возможные «хвосты» альбомов
    try:
        uid = message.from_user.id if message.from_user else message.chat.id
        await _clear_user_viewer(bot, uid)
    except Exception:
        pass
    sent = await message.answer("📱Главное меню:", reply_markup=user_main_menu())
    if message.from_user:
        register_bot_message(message.from_user.id, sent.message_id)

@router.callback_query(F.data == "u:menu")
async def cb_menu(cb: CallbackQuery, bot: Bot):
    # при переходе в меню чистим возможные «хвосты» альбомов
    try:
        await _clear_user_viewer(bot, cb.from_user.id)
    except Exception:
        pass

    msg = cb.message
    if isinstance(msg, TgMessage):
        try:
            await msg.edit_text("📱Главное меню:", reply_markup=user_main_menu())
            register_bot_message(cb.from_user.id, msg.message_id)
        except Exception:
            sent = await msg.answer("📱Главное меню:", reply_markup=user_main_menu())
            register_bot_message(cb.from_user.id, sent.message_id)
            try:
                await msg.delete()
            except Exception:
                pass
    else:
        sent = await bot.send_message(cb.from_user.id, "📱Главное меню:", reply_markup=user_main_menu())
        register_bot_message(cb.from_user.id, sent.message_id)

    await cb.answer()

# ===================== Новая заявка =====================

@router.callback_query(F.data == "u:new")
async def cb_new(cb: CallbackQuery, state: FSMContext, bot: Bot):
    msg = cb.message
    if isinstance(msg, TgMessage):
        try:
            await msg.edit_text("🆕 Выбери категорию проблемы:", reply_markup=categories_kb())
            register_bot_message(cb.from_user.id, msg.message_id)
        except Exception:
            sent = await msg.answer("🆕 Выбери категорию проблемы:", reply_markup=categories_kb())
            register_bot_message(cb.from_user.id, sent.message_id)
    else:
        # нет доступного сообщения для редактирования — шлём новое
        sent = await bot.send_message(cb.from_user.id, "🆕 Выбери категорию проблемы:", reply_markup=categories_kb())
        register_bot_message(cb.from_user.id, sent.message_id)

    await state.set_state(TicketState.choosing_category)
    await cb.answer()

@router.callback_query(F.data == "u:back")
async def cb_back_to_main(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()

    msg = cb.message
    if isinstance(msg, TgMessage):
        try:
            await msg.edit_text("📱Главное меню:", reply_markup=user_main_menu())
            register_bot_message(cb.from_user.id, msg.message_id)
        except Exception:
            sent = await msg.answer("📱Главное меню:", reply_markup=user_main_menu())
            register_bot_message(cb.from_user.id, sent.message_id)
    else:
        sent = await bot.send_message(cb.from_user.id, "📱Главное меню:", reply_markup=user_main_menu())
        register_bot_message(cb.from_user.id, sent.message_id)

    await cb.answer()

# ===================== Выбор категории =====================

@router.callback_query(F.data.startswith("u:cat:"))
async def cb_pick_category(cb: CallbackQuery, state: FSMContext, bot: Bot):
    slug = (cb.data or "").split(":", 2)[2] if cb.data else ""
    category, emoji = _slug_to_category_and_emoji(slug)

    root_id: Optional[int] = cb.message.message_id if isinstance(cb.message, TgMessage) else None
    draft = DraftData(category=category, root_message_id=root_id)
    DRAFTS[cb.from_user.id] = draft

    text = (
        f"{emoji} <b>Категория:</b> <b>{category}</b>\n"
        f"Опиши проблему текстом и/или приложи медиа (фото/видео/голос/док).\n"
        f"Когда закончишь — нажми «Готово»."
    )
    msg = cb.message
    if isinstance(msg, TgMessage):
        try:
            await msg.edit_text(text, reply_markup=cancel_only_kb(), parse_mode="HTML")
            register_bot_message(cb.from_user.id, msg.message_id)
        except Exception:
            sent = await msg.answer(text, reply_markup=cancel_only_kb(), parse_mode="HTML")
            register_bot_message(cb.from_user.id, sent.message_id)
    else:
        sent = await bot.send_message(cb.from_user.id, text, reply_markup=cancel_only_kb(), parse_mode="HTML")
        register_bot_message(cb.from_user.id, sent.message_id)

    await state.set_state(TicketState.collecting)
    await cb.answer()

# ===================== Отмена/Назад в сборе =====================

@router.callback_query(F.data == "cancel_collect")
async def cb_cancel_collect(cb: CallbackQuery, state: FSMContext, bot: Bot):
    draft = DRAFTS.pop(cb.from_user.id, None)

    # Удалим последние «Принято…»
    if draft and draft.hint_message_id:
        try:
            await bot.delete_message(cb.from_user.id, draft.hint_message_id)
        except Exception:
            pass

    await state.clear()

    # Возврат в главное меню и чистка бот-сообщений этой сессии
    mids = drain_bot_messages(cb.from_user.id)
    chat_id = cb.message.chat.id if isinstance(cb.message, TgMessage) else cb.from_user.id
    await safe_bulk_delete(bot, chat_id, mids)

    if isinstance(cb.message, TgMessage):
        sent = await cb.message.answer("Отменено. Возврат в меню.", reply_markup=user_main_menu())
    else:
        sent = await bot.send_message(cb.from_user.id, "Отменено. Возврат в меню.", reply_markup=user_main_menu())
    register_bot_message(cb.from_user.id, sent.message_id)
    await cb.answer("Отменено")

# ===================== Сбор контента =====================

@router.message(TicketState.collecting, F.content_type.in_({"text", "photo", "video", "voice", "document"}))
async def collecting(message: Message, bot: Bot):
    uid = message.from_user.id if message.from_user else message.chat.id
    draft = DRAFTS.setdefault(uid, DraftData())

    if draft.hint_message_id:
        try:
            await bot.delete_message(message.chat.id, draft.hint_message_id)
        except Exception:
            pass
        draft.hint_message_id = None

    was_empty = (not draft.description.strip()) and (len(draft.attachments) == 0)

    if message.text:
        if draft.description:
            draft.description += "\n"
        draft.description += message.text
    elif message.photo:
        draft.attachments.append(("photo", message.photo[-1].file_id, message.caption, message.media_group_id))
    elif message.video:
        draft.attachments.append(("video", message.video.file_id, message.caption, message.media_group_id))
    elif message.voice:
        draft.attachments.append(("voice", message.voice.file_id, message.caption, message.media_group_id))
    elif message.document:
        draft.attachments.append(("document", message.document.file_id, message.caption, message.media_group_id))

    if was_empty and draft.root_message_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=draft.root_message_id,
                reply_markup=done_cancel_kb(),
            )
        except Exception:
            pass

    try:
        sent = await message.answer("Принято. Можешь добавить ещё или нажимай «Готово».")
        draft.hint_message_id = sent.message_id
        register_bot_message(uid, sent.message_id)
    except Exception:
        pass

# ===================== Финализация заявки =====================

def _build_admin_caption(task: Task, author_username: str | None = None) -> str:
    """
    Красивый HTML-текст карточки, которую получают админы.
    С эмодзи категории и аккуратной врезкой описания.
    """
    # эмодзи категории
    emoji = ""
    for title, emj, _ in USER_CATEGORIES:
        if title == (task.category or ""):
            emoji = emj
            break

    author_line = "Автор: "
    if task.author_full_name or task.author_sip:
        author_line += f"<b>{task.author_full_name or '—'}</b>"
        if task.author_sip:
            author_line += f" · доб. <b>{task.author_sip}</b>"
        if task.author_tg_id:
            handle = f"@{author_username}" if author_username else f"tg:<code>{task.author_tg_id}</code>"
            author_line += f" ({handle})"
    else:
        author_line += f"{task.author_tg_id or '—'}"

    body = (task.description or "—").strip()
    body = f"<blockquote>{body}</blockquote>"

    return (
        f"🆕 <b>Новая заявка №{task.id}</b>\n"
        f"👤 {author_line}\n"
        f"🏷️ Категория: {emoji} <b>{task.category or '—'}</b>\n"
        f"📝 Сообщение:\n{body}"
    )[:1024]

async def _send_admin_card_with_media(
    bot: Bot,
    admin_id: int,
    task: Task,
    caption: str,
    media_items: List[Tuple[str, str, str | None]],
) -> None:
    """
    Уведомление админа о новой заявке.

    • 1 медиа  → одно медиа + caption + кнопка.
    • ≥2 медиа → альбом без подписей, затем отдельное текстовое сообщение
                 с кнопкой «Взять». Все message_id сохраняем в InMemoryNotifications.
    """
    try:
        if len(media_items) == 1:
            mtype, fid, _ = media_items[0]
            if mtype == "photo":
                msg = await bot.send_photo(admin_id, fid, caption=caption, reply_markup=admin_accept_kb(task.id), parse_mode="HTML")
            elif mtype == "video":
                msg = await bot.send_video(admin_id, fid, caption=caption, reply_markup=admin_accept_kb(task.id), parse_mode="HTML")
            elif mtype == "document":
                msg = await bot.send_document(admin_id, fid, caption=caption, reply_markup=admin_accept_kb(task.id), parse_mode="HTML")
            else:
                msg = await bot.send_message(admin_id, caption, reply_markup=admin_accept_kb(task.id), parse_mode="HTML")
            InMemoryNotifications.remember_admin(task.id, admin_id, admin_id, msg.message_id)
            return

        # ≥ 2 медиа — сначала альбом (без caption)
        medias = []
        for mtype, fid, _ in media_items[:10]:
            if mtype == "photo":
                medias.append(InputMediaPhoto(media=fid))
            elif mtype == "video":
                medias.append(InputMediaVideo(media=fid))
            elif mtype == "document":
                medias.append(InputMediaDocument(media=fid))
        if medias:
            msgs = await bot.send_media_group(chat_id=admin_id, media=medias)
            for m in msgs:
                InMemoryNotifications.remember_admin(task.id, admin_id, admin_id, m.message_id)

        # если вложений > 10 — шлём остаток батчами по 10 (без кнопок/подписей)
        rest = media_items[10:]
        while rest:
            batch = rest[:10]
            rest = rest[10:]
            more = []
            for mtype, fid, _ in batch:
                if mtype == "photo":
                    more.append(InputMediaPhoto(media=fid))
                elif mtype == "video":
                    more.append(InputMediaVideo(media=fid))
                elif mtype == "document":
                    more.append(InputMediaDocument(media=fid))
            if more:
                more_msgs = await bot.send_media_group(chat_id=admin_id, media=more)
                for m in more_msgs:
                    InMemoryNotifications.remember_admin(task.id, admin_id, admin_id, m.message_id)

        # затем отдельная текстовая карточка с кнопкой «Взять»
        card = await bot.send_message(admin_id, caption, reply_markup=admin_accept_kb(task.id), parse_mode="HTML")
        InMemoryNotifications.remember_admin(task.id, admin_id, admin_id, card.message_id)

    except Exception:
        # запасной вариант — хотя бы текстовая карточка с кнопкой
        try:
            fallback = await bot.send_message(admin_id, caption, reply_markup=admin_accept_kb(task.id), parse_mode="HTML")
            InMemoryNotifications.remember_admin(task.id, admin_id, admin_id, fallback.message_id)
        except Exception:
            pass


async def _send_admin_voices(
    bot: Bot, admin_id: int, task_id: int, voices: List[Tuple[str, str, str | None]]
):
    for _t, fid, cap in voices:
        try:
            m = await bot.send_voice(admin_id, fid, caption=cap)
            InMemoryNotifications.remember_admin(task_id, admin_id, admin_id, m.message_id)
        except Exception:
            continue

async def _finalize_ticket(
    source: Union[CallbackQuery, Message],
    session: AsyncSession,
    bot: Bot,
    state: FSMContext,
):
    uid = source.from_user.id if source.from_user else (source.chat.id if isinstance(source, TgMessage) else None)
    if uid is None:
        # без uid некого уведомлять
        if isinstance(source, CallbackQuery):
            await source.answer("Не удалось определить пользователя.", show_alert=True)
        return

    draft = DRAFTS.pop(uid, None)

    if not draft or not draft.category:
        if isinstance(source, CallbackQuery):
            await source.answer("Нет данных для заявки.", show_alert=True)
        else:
            await source.answer("Нет данных для заявки.")
        return

    if draft.hint_message_id:
        try:
            await bot.delete_message(uid, draft.hint_message_id)
        except Exception:
            pass

    # ensure user
    u = (await session.execute(select(User).where(User.tg_id == uid))).scalars().first()
    author_full_name = (u.full_name if u and u.full_name else (source.from_user.full_name if source.from_user else "Пользователь"))
    author_sip = (u.sip_ext if u and u.sip_ext else None)

    # username только из Telegram-профиля (БД не трогаем)
    author_username: Optional[str] = source.from_user.username if (source.from_user and source.from_user.username) else None

    if not u:
        u = User(tg_id=uid, full_name=author_full_name)
        session.add(u)
        await session.flush()
    else:
        # при каждом обращении подтягиваем текущее имя из Telegram (не ломая введённое ФИО, если пусто)
        if source.from_user and source.from_user.full_name and not u.full_name:
            u.full_name = source.from_user.full_name

    task = Task(
        author_tg_id=uid,
        author_full_name=author_full_name,
        author_sip=author_sip,
        category=draft.category,
        description=draft.description.strip(),
        status=Status.NEW.value,
        priority=Priority.MEDIUM.value,
        is_internal=False,
        user_visible=True,
    )
    session.add(task)
    await session.flush()

    for file_type, file_id, caption, media_gid in draft.attachments:
        session.add(
            Attachment(
                task_id=task.id,
                file_id=file_id,
                file_type=file_type,
                caption=caption,
                media_group_id=media_gid,
            )
        )

    notify_ids, assignee = await assign_by_category(session, draft.category)
    task.assignee_tg_id = assignee
    await session.commit()

    # Подтверждение пользователю
    text_ok = (
        f"✅ Заявка №{task.id} создана.\n"
        f"Категория: {task.category}.\n"
        f"👤 {task.author_full_name or '—'}  ☎️ доб. {task.author_sip or '—'}\n"
        f"Ожидай ответ от IT."
    )
    msg = source.message if isinstance(source, CallbackQuery) else source
    if isinstance(msg, TgMessage):
        try:
            await msg.edit_text(text_ok)
            register_bot_message(uid, msg.message_id)
        except Exception:
            sent = await msg.answer(text_ok)
            register_bot_message(uid, sent.message_id)
    else:
        sent = await bot.send_message(uid, text_ok)
        register_bot_message(uid, sent.message_id)

    # ===== Уведомление админам =====
    media_items: List[Tuple[str, str, str | None]] = []
    voice_items: List[Tuple[str, str, str | None]] = []
    for (t, fid, cap, _gid) in draft.attachments:
        if t in ("photo", "video", "document"):
            media_items.append((t, fid, cap))
        elif t == "voice":
            voice_items.append((t, fid, cap))

    caption = _build_admin_caption(task, author_username=author_username)

    for admin_id in notify_ids:
        try:
            if media_items:
                await _send_admin_card_with_media(bot, admin_id, task, caption, media_items)
            else:
                card = await bot.send_message(admin_id, caption, reply_markup=admin_accept_kb(task.id), parse_mode="HTML")
                InMemoryNotifications.remember_admin(task.id, admin_id, admin_id, card.message_id)
        except Exception:
            try:
                fallback = await bot.send_message(admin_id, caption, parse_mode="HTML")
                InMemoryNotifications.remember_admin(task.id, admin_id, admin_id, fallback.message_id)
            except Exception:
                pass
        if voice_items:
            await _send_admin_voices(bot, admin_id, task.id, voice_items)

    await state.clear()

    # Чистим чат пользователя (бот-сообщения этой сессии) и показываем главное меню
    mids = drain_bot_messages(uid)
    chat_id = msg.chat.id if isinstance(msg, TgMessage) else uid
    await safe_bulk_delete(bot, chat_id, mids)

    sent_menu = await bot.send_message(uid, "Готово ✅ Возврат в главное меню.", reply_markup=user_main_menu())
    register_bot_message(uid, sent_menu.message_id)

# Завершение
@router.callback_query(F.data == "done_collect")
async def cb_done_collect(cb: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext):
    await _finalize_ticket(cb, session, bot, state)
    await cb.answer("Отправлено ✅")

@router.message(TicketState.collecting, F.text == "/done")
async def done_text(message: Message, session: AsyncSession, bot: Bot, state: FSMContext):
    await _finalize_ticket(message, session, bot, state)

# ===================== История моих заявок: сетка 2×3, «низ» закреплён =====================

PAGE_SIZE = 6  # 2x3

def _placeholder_btn_text() -> str:
    return " "  # узкий пробел — «пустая» кнопка

def _history_kb(tasks: Sequence[Task], page: int, pages: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    # карточки (ровно 6)
    present = 0
    for t in tasks:
        status = _status_label(t.status)
        kb.button(text=f"{t.category}  {status}", callback_data=f"u:view:{t.id}:{page}")
        present += 1
    for _ in range(PAGE_SIZE - present):
        kb.button(text=_placeholder_btn_text(), callback_data="u:noop")

    # низ
    pages = max(pages, 1)
    left_page = max(page - 1, 1)
    right_page = min(page + 1, pages)
    kb.button(text="◀️", callback_data=f"u:history:p:{left_page}")
    kb.button(text="▶️", callback_data=f"u:history:p:{right_page}")
    kb.button(text=f"Page {page}/{pages}", callback_data="u:noop")
    kb.button(text="⬅️ Назад", callback_data="u:menu")

    kb.adjust(2, 2, 2, 2, 1, 1)
    return kb.as_markup()

@router.callback_query(F.data.startswith("u:history:p:"))
async def cb_history(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    try:
        page = int((cb.data or "").split(":")[-1])
    except Exception:
        page = 1

    # на входе/листании истории удаляем ранее показанные медиа карточки
    await _clear_user_viewer(bot, cb.from_user.id)

    # Считаем всего заявок
    total_q = await session.execute(
        select(func.count()).select_from(
            select(Task.id).where(Task.author_tg_id == cb.from_user.id).subquery()
        )
    )
    total = int(total_q.scalar() or 0)
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = max(1, min(page, pages))

    # Получаем задания текущей страницы
    offset = (page - 1) * PAGE_SIZE
    res = await session.execute(
        select(Task)
        .where(Task.author_tg_id == cb.from_user.id)
        .order_by(Task.created_at.desc())
        .offset(offset)
        .limit(PAGE_SIZE)
    )
    tasks = res.scalars().all()

    header = "📚 История заявок"
    markup = _history_kb(tasks, page, pages)

    msg = cb.message
    if isinstance(msg, TgMessage) and msg.content_type == "text":
        try:
            if (msg.text or "") == header:
                await msg.edit_reply_markup(reply_markup=markup)
            else:
                await msg.edit_text(header, reply_markup=markup)
            register_bot_message(cb.from_user.id, msg.message_id)
        except TelegramBadRequest:
            # Фолбэк: шлём новое и удаляем старое
            sent = await msg.answer(header, reply_markup=markup)
            register_bot_message(cb.from_user.id, sent.message_id)
            try:
                await msg.delete()
            except Exception:
                pass
    else:
        # медиа/недоступное — шлём новое и удаляем текущее, если это Message
        sent = await bot.send_message(cb.from_user.id, header, reply_markup=markup)
        register_bot_message(cb.from_user.id, sent.message_id)
        if isinstance(msg, TgMessage):
            try:
                await msg.delete()
            except Exception:
                pass

    await cb.answer()

@router.message(Command("my"))
async def cmd_my(message: Message, session: AsyncSession, bot: Bot):
    sent = await message.answer("Загружаю историю…")
    if message.from_user:
        register_bot_message(message.from_user.id, sent.message_id)

    # мини-обёртка для вызова cb_history
    class _CB:
        def __init__(self, data: str, from_user, message: TgMessage):
            self.data = data
            self.from_user = from_user
            self.message = message

    fake_cb = _CB("u:history:p:1", message.from_user, sent)
    await cb_history(fake_cb, session, bot)  # типы соблюдены

@router.callback_query(F.data.startswith("u:view:"))
async def cb_user_view(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    parts = (cb.data or "").split(":")
    try:
        task_id = int(parts[2])
    except Exception:
        return await cb.answer("Некорректный id", show_alert=True)

    page = 1
    if len(parts) >= 4:
        try:
            page = int(parts[3])
        except Exception:
            page = 1

    res = await session.execute(select(Task).where(Task.id == task_id, Task.author_tg_id == cb.from_user.id))
    t = res.scalars().first()
    if not t:
        return await cb.answer("Заявка не найдена.", show_alert=True)

    # очистим любые старые медиа карточки пользователя
    await _clear_user_viewer(bot, cb.from_user.id)

    # соберём вложения
    ares = await session.execute(select(Attachment).where(Attachment.task_id == t.id))
    attachments = ares.scalars().all()

    media_items = [(a.file_type, a.file_id) for a in attachments if a.file_type in ("photo", "video", "document")]
    voices = [a for a in attachments if a.file_type == "voice"]

    # клавиатура «назад/меню»
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=f"u:history:p:{page}")
    kb.button(text="🏠 Меню", callback_data="u:menu")
    kb.adjust(2)

    # текст карточки
    caption = (
        f"🧾 <b>Заявка №{t.id}</b>\n"
        f"👤 {t.author_full_name or '—'}  ☎️ доб. {t.author_sip or '—'}\n"
        f"📂 Категория: {t.category}\n"
        f"🔖 Статус: {_status_label(t.status)}\n\n"
        f"📝 <b>Описание:</b>\n{t.description or '—'}"
    )

    sent_ids: List[int] = []

    if len(media_items) == 1:
        # одно медиа — одиночное сообщение с caption и кнопками
        typ, fid = media_items[0]
        try:
            if typ == "photo":
                m = await bot.send_photo(cb.from_user.id, fid, caption=caption, parse_mode="HTML", reply_markup=kb.as_markup())
            elif typ == "video":
                m = await bot.send_video(cb.from_user.id, fid, caption=caption, parse_mode="HTML", reply_markup=kb.as_markup())
            elif typ == "document":
                m = await bot.send_document(cb.from_user.id, fid, caption=caption, parse_mode="HTML", reply_markup=kb.as_markup())
            else:
                m = await bot.send_message(cb.from_user.id, caption, parse_mode="HTML", reply_markup=kb.as_markup())
            sent_ids.append(m.message_id)
        except Exception:
            m = await bot.send_message(cb.from_user.id, caption, parse_mode="HTML", reply_markup=kb.as_markup())
            sent_ids.append(m.message_id)
            try:
                if typ == "photo":
                    pm = await bot.send_photo(cb.from_user.id, fid)
                elif typ == "video":
                    pm = await bot.send_video(cb.from_user.id, fid)
                elif typ == "document":
                    pm = await bot.send_document(cb.from_user.id, fid)
                else:
                    pm = None
                if pm:
                    sent_ids.append(pm.message_id)
            except Exception:
                pass

        if isinstance(cb.message, TgMessage):
            try:
                await cb.message.delete()
            except Exception:
                pass

    elif len(media_items) >= 2:
        # 1) альбом БЕЗ подписей
        chunk = media_items[:10]
        medias = []
        for typ, fid in chunk:
            if typ == "photo":
                medias.append(InputMediaPhoto(media=fid))
            elif typ == "video":
                medias.append(InputMediaVideo(media=fid))
            elif typ == "document":
                medias.append(InputMediaDocument(media=fid))
        msgs = await bot.send_media_group(chat_id=cb.from_user.id, media=medias)
        # ⬇️ регистрируем ТОЛЬКО id медиа-сообщений (не текст ниже)
        sent_ids.extend(m.message_id for m in msgs)

        # 2) отдельное текстовое сообщение под альбомом с кнопками (НЕ кладём его в USER_VIEWER)
        await bot.send_message(cb.from_user.id, caption, parse_mode="HTML", reply_markup=kb.as_markup())

        # удаляем исходное сообщение (список/история) если оно доступно
        if isinstance(cb.message, TgMessage):
            try:
                await cb.message.delete()
            except Exception:
                pass

        # остаток >10 — альбомами без кнопок
        rest = media_items[10:]
        while rest:
            batch = rest[:10]
            rest = rest[10:]
            more = []
            for typ, fid in batch:
                if typ == "photo":
                    more.append(InputMediaPhoto(media=fid))
                elif typ == "video":
                    more.append(InputMediaVideo(media=fid))
                elif typ == "document":
                    more.append(InputMediaDocument(media=fid))
            try:
                more_msgs = await bot.send_media_group(chat_id=cb.from_user.id, media=more)
                sent_ids.extend(m.message_id for m in more_msgs)
            except Exception:
                break

    else:
        # нет медиа — обычный текст в том же сообщении
        if isinstance(cb.message, TgMessage):
            try:
                await cb.message.edit_text(caption, reply_markup=kb.as_markup(), parse_mode="HTML")
                # это сообщение не заносим в USER_VIEWER
            except TelegramBadRequest:
                _ = await bot.send_message(cb.from_user.id, caption, parse_mode="HTML", reply_markup=kb.as_markup())
                try:
                    await cb.message.delete()
                except Exception:
                    pass
        else:
            _ = await bot.send_message(cb.from_user.id, caption, parse_mode="HTML", reply_markup=kb.as_markup())

    # голосовые — отдельными сообщениями
    if voices:
        try:
            for a in voices:
                vm = await bot.send_voice(cb.from_user.id, a.file_id)
                sent_ids.append(vm.message_id)
        except Exception:
            pass

    if sent_ids:
        USER_VIEWER[cb.from_user.id] = sent_ids

    await cb.answer()

@router.callback_query(F.data == "u:noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()
