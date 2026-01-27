# app/handlers/boss.py
from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from typing import Dict, List, Optional, Sequence, Tuple

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from aiogram.types import Message as TgMessage
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.enums import Priority, Role, Status
from app.keyboards import admin_accept_kb
from app.models import Attachment, Task, User
from app.services.assignment import InMemoryNotifications
from app.states import TicketState

router = Router(name="boss")

# один «якорь» на чат босса (редактируем его вместо спама) + учёт медиа, чтобы чистить
BOSS_ANCHOR: Dict[int, int] = {}         # boss_id -> message_id
BOSS_MEDIA: Dict[int, List[int]] = {}    # boss_id -> [media_message_id,...]
BOSS_CTX: Dict[int, Dict] = {}           # boss_id -> навигационный контекст (статистика)

PAGE = 9  # 3x3 плитки


def is_boss(user_id: int) -> bool:
    try:
        return int(user_id) == int(settings.BOSS)
    except Exception:
        return False


# ----------------- helpers ----------------- #

# Безопасная «константа» для ASSIGNED (как в admin.py), чтобы не падать, если статуса нет
try:
    STATUS_ASSIGNED_VALUE: str = Status.ASSIGNED.value  # type: ignore[attr-defined]
except Exception:
    STATUS_ASSIGNED_VALUE = Status.IN_PROGRESS.value

# Набор «актуальных» статусов (как в admin.py), учитываем доступные поля
OPEN_STATUSES = {
    Status.NEW.value,
    STATUS_ASSIGNED_VALUE,
    Status.IN_PROGRESS.value,
    (getattr(Status, "WAITING", Status.IN_PROGRESS).value if hasattr(Status, "WAITING") else Status.IN_PROGRESS.value),
    (getattr(Status, "REOPENED", Status.IN_PROGRESS).value if hasattr(Status, "REOPENED") else Status.IN_PROGRESS.value),
}


def _priority_square(priority_value: str) -> str:
    p = (priority_value or "").strip().upper()
    if p == Priority.HIGH.value:
        return "🟥"
    if p == Priority.MEDIUM.value:
        return "🟨"
    if p == Priority.LOW.value:
        return "🟩"

    # совместимость со старыми/временными значениями
    pl = (priority_value or "").strip().lower()
    if pl == "high":
        return "🟥"
    if pl == "medium":
        return "🟨"
    if pl == "low":
        return "🟩"

    return "🟨"


def _boss_admin_caption(priority_value: str, description: str) -> str:
    sq = _priority_square(priority_value)
    body = escape((description or "").strip()) or "—"
    txt = f"{sq} <b>От Босса</b> {sq}\n<blockquote>{body}</blockquote>"
    return txt[:1024]


async def _get_user(session: AsyncSession, tg_id: int) -> Optional[User]:
    res = await session.execute(select(User).where(User.tg_id == tg_id))
    return res.scalars().first()


async def _count_closed(session: AsyncSession, admin_tg_id: int) -> int:
    q = await session.execute(
        select(func.count()).select_from(Task).where(
            Task.assignee_tg_id == admin_tg_id,
            Task.status == Status.CLOSED.value,
        )
    )
    return int(q.scalar_one())


async def _count_current(session: AsyncSession, admin_tg_id: int) -> int:
    q = await session.execute(
        select(func.count()).select_from(Task).where(
            Task.assignee_tg_id == admin_tg_id,
            Task.status.in_(list(OPEN_STATUSES)),
        )
    )
    return int(q.scalar_one())


def _emojibar(n: int) -> str:
    return "🟢" * min(n, 8) + (f"×{n}" if n > 8 else "")


async def _show_anchor(bot: Bot, chat_id: int, text: str, kb, anchor_id: Optional[int]) -> int:
    """Редактируем якорь, если можно. Иначе присылаем новый и пробуем удалить старый."""
    if anchor_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=anchor_id,
                text=text,
                reply_markup=kb,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return anchor_id
        except TelegramBadRequest:
            pass
        except Exception:
            pass

    msg = await bot.send_message(
        chat_id,
        text,
        reply_markup=kb,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    if anchor_id and anchor_id != msg.message_id:
        try:
            await bot.delete_message(chat_id, anchor_id)
        except Exception:
            pass
    return msg.message_id


async def _clean_media(bot: Bot, boss_id: int) -> None:
    mids = BOSS_MEDIA.pop(boss_id, [])
    for mid in mids:
        try:
            await bot.delete_message(boss_id, mid)
        except Exception:
            pass


async def _send_boss_task_to_admin(bot: Bot, admin_id: int, task: Task, attachments: Sequence[Attachment]) -> None:
    """
    Отправка задачи выбранному админу:
    - Заголовок: 🟥/🟨/🟩 От Босса 🟥/🟨/🟩
    - Без "NEW", без "№..."
    - Обязательно кнопка "Принять"
    - Вложения: если 1 media — с caption+кнопкой; если >=2 — media_group + отдельное сообщение с кнопкой.
      Voice — отдельными сообщениями.
    """
    caption = _boss_admin_caption(task.priority or Priority.MEDIUM.value, task.description or "")

    media_items: List[Tuple[str, str, Optional[str]]] = []
    voice_items: List[Tuple[str, str, Optional[str]]] = []
    for a in attachments:
        if a.file_type in ("photo", "video", "document"):
            media_items.append((a.file_type, a.file_id, a.caption))
        elif a.file_type == "voice":
            voice_items.append((a.file_type, a.file_id, a.caption))

    if len(media_items) >= 2:
        medias = []
        for t, fid, _cap in media_items[:10]:
            if t == "photo":
                medias.append(InputMediaPhoto(media=fid))
            elif t == "video":
                medias.append(InputMediaVideo(media=fid))
            else:
                medias.append(InputMediaDocument(media=fid))

        try:
            msgs = await bot.send_media_group(chat_id=admin_id, media=medias)
            for m in msgs:
                InMemoryNotifications.remember_admin(task.id, admin_id, admin_id, m.message_id)
        except Exception:
            pass

        try:
            msg = await bot.send_message(
                admin_id,
                caption,
                reply_markup=admin_accept_kb(task.id),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            InMemoryNotifications.remember_admin(task.id, admin_id, admin_id, msg.message_id)
        except Exception:
            pass

    elif len(media_items) == 1:
        t, fid, _cap = media_items[0]
        try:
            if t == "photo":
                msg = await bot.send_photo(
                    admin_id,
                    fid,
                    caption=caption,
                    reply_markup=admin_accept_kb(task.id),
                    parse_mode="HTML",
                )
            elif t == "video":
                msg = await bot.send_video(
                    admin_id,
                    fid,
                    caption=caption,
                    reply_markup=admin_accept_kb(task.id),
                    parse_mode="HTML",
                )
            else:
                msg = await bot.send_document(
                    admin_id,
                    fid,
                    caption=caption,
                    reply_markup=admin_accept_kb(task.id),
                    parse_mode="HTML",
                )
            InMemoryNotifications.remember_admin(task.id, admin_id, admin_id, msg.message_id)
        except Exception:
            try:
                msg = await bot.send_message(
                    admin_id,
                    caption,
                    reply_markup=admin_accept_kb(task.id),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                InMemoryNotifications.remember_admin(task.id, admin_id, admin_id, msg.message_id)
            except Exception:
                pass

    else:
        try:
            msg = await bot.send_message(
                admin_id,
                caption,
                reply_markup=admin_accept_kb(task.id),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            InMemoryNotifications.remember_admin(task.id, admin_id, admin_id, msg.message_id)
        except Exception:
            pass

    for _t, fid, cap in voice_items[:10]:
        try:
            m = await bot.send_voice(admin_id, fid, caption=cap)
            InMemoryNotifications.remember_admin(task.id, admin_id, admin_id, m.message_id)
        except Exception:
            pass


# ----------------- локальные клавиатуры ----------------- #

def kb_boss_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🆕 Задача", callback_data="b:new")
    kb.button(text="📊 Статистика", callback_data="b:stats")
    kb.button(text="📤 Отправленные", callback_data="b:sent")
    kb.button(text="☀️ Отпуска", callback_data="b:vac")
    kb.adjust(2, 2)
    return kb.as_markup()


def kb_pick_admin() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👨‍💻 Артур", callback_data=f"b:new:pick_admin:{settings.ADMIN_1}")
    kb.button(text="🧑‍💻 Андрей К.", callback_data=f"b:new:pick_admin:{settings.ADMIN_2}")
    kb.button(text="🔙 Назад", callback_data="b:menu")
    kb.adjust(2, 1)
    return kb.as_markup()


def kb_pick_priority() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🟥 Высокий", callback_data="b:new:prio:high")
    kb.button(text="🟨 Средний", callback_data="b:new:prio:medium")
    kb.button(text="🟩 Низкий", callback_data="b:new:prio:low")
    kb.button(text="🔙 Назад", callback_data="b:menu")
    kb.button(text="✖️ Отмена", callback_data="b:new:cancel")
    kb.adjust(3, 2)
    return kb.as_markup()


def kb_collect() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data="b:new:done")
    kb.button(text="✖️ Отмена", callback_data="b:new:cancel")
    kb.button(text="🔙 Назад", callback_data="b:new:back:prio")
    kb.adjust(2, 1)
    return kb.as_markup()


def kb_vacation(artur_on: bool, andrey_on: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"👨‍💻 Артур — {'☀️ отпуск' if artur_on else '🟢 работает'}",
        callback_data=f"b:toggle_vac:{settings.ADMIN_1}",
    )
    kb.button(
        text=f"🧑‍💻 Андрей К. — {'☀️ отпуск' if andrey_on else '🟢 работает'}",
        callback_data=f"b:toggle_vac:{settings.ADMIN_2}",
    )
    kb.button(text="🔙 Назад", callback_data="b:menu")
    kb.adjust(1, 1, 1)
    return kb.as_markup()


def kb_stats_root(artur_cur: int, andrey_cur: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🧑‍💻 Андрей К.  {_emojibar(andrey_cur)}", callback_data=f"b:stats:emp:{settings.ADMIN_2}")
    kb.button(text=f"👨‍💻 Артур  {_emojibar(artur_cur)}", callback_data=f"b:stats:emp:{settings.ADMIN_1}")
    kb.button(text="🔙 Назад", callback_data="b:menu")
    kb.adjust(1, 1, 1)
    return kb.as_markup()


def kb_stats_emp_filters(emp_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🟢 Актуальные", callback_data=f"b:stats:list:{emp_id}:cur:1")
    kb.button(text="✅ Завершённые", callback_data=f"b:stats:list:{emp_id}:done:1")
    kb.button(text="🔙 Назад", callback_data="b:stats:back")
    kb.adjust(2, 1)
    return kb.as_markup()


def kb_grid(
    items: List[Tuple[int, str]],
    page: int,
    pages: int,
    back_cb: str,
    base_open_prefix: str,
    base_page_prefix: str,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for tid, label in items:
        kb.button(text=label, callback_data=f"{base_open_prefix}:{tid}")
    if len(items) < PAGE:
        for _ in range(PAGE - len(items)):
            kb.button(text="‎", callback_data="b:nop")
    prev_p = max(page - 1, 1)
    next_p = min(page + 1, pages)
    kb.button(text="◀️", callback_data=f"{base_page_prefix}:{prev_p}")
    kb.button(text=f"Page {page}/{pages}", callback_data="b:nop")
    kb.button(text="▶️", callback_data=f"{base_page_prefix}:{next_p}")
    kb.button(text="🔙 Назад", callback_data=back_cb)
    kb.adjust(3, 3, 3, 3, 1)
    return kb.as_markup()


# ----------------- Драфт (FSM) ----------------- #

@dataclass
class BossDraft:
    assignee: Optional[int] = None
    priority: str = Priority.MEDIUM.value
    description: str = ""
    attachments: List[Tuple[str, str, Optional[str], Optional[str]]] = field(default_factory=list)


async def _get_draft(state: FSMContext) -> BossDraft:
    data = await state.get_data()
    d = data.get("boss_draft")
    if isinstance(d, BossDraft):
        return d
    draft = BossDraft()
    await state.update_data(boss_draft=draft)
    return draft


# ====================== Главный экран ====================== #

@router.message(Command("boss"))
async def cmd_boss(message: Message, session: AsyncSession, bot: Bot, state: FSMContext):
    if not message.from_user or not is_boss(message.from_user.id):
        return await message.answer("Доступно только начальнику.")
    await state.clear()
    await _clean_media(bot, message.from_user.id)

    _ = await _get_user(session, settings.ADMIN_1)
    _ = await _get_user(session, settings.ADMIN_2)

    text = "🧭 <b>Панель начальника</b>\nВыберите действие ниже."
    anchor = BOSS_ANCHOR.get(message.from_user.id)
    msg_id = await _show_anchor(bot, message.chat.id, text, kb_boss_menu(), anchor)
    BOSS_ANCHOR[message.from_user.id] = msg_id


# ====================== Отпуска ====================== #

@router.callback_query(F.data == "b:vac")
async def b_vac(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    if not is_boss(cb.from_user.id):
        return await cb.answer("Нет прав.", show_alert=True)

    artur = await _get_user(session, settings.ADMIN_1)
    andrey = await _get_user(session, settings.ADMIN_2)
    text = "☀️ <b>Отпуска</b>\nПереключайте статусы кнопками ниже."
    new_id = await _show_anchor(
        bot,
        cb.from_user.id,
        text,
        kb_vacation(artur.on_vacation if artur else False, andrey.on_vacation if andrey else False),
        BOSS_ANCHOR.get(cb.from_user.id),
    )
    BOSS_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


@router.callback_query(F.data.startswith("b:toggle_vac:"))
async def b_toggle_vac(cb: CallbackQuery, session: AsyncSession):
    if not is_boss(cb.from_user.id):
        return await cb.answer("Нет прав.", show_alert=True)
    admin_id = int((cb.data or "").split(":")[-1])
    user = await _get_user(session, admin_id)
    if not user:
        user = User(tg_id=admin_id, full_name="Админ", role=Role.ADMIN.value, is_authenticated=True, on_vacation=False)
        session.add(user)
        await session.flush()
    user.on_vacation = not user.on_vacation
    await session.commit()
    await cb.answer("Обновлено ✅")

    artur = await _get_user(session, settings.ADMIN_1)
    andrey = await _get_user(session, settings.ADMIN_2)
    if isinstance(cb.message, TgMessage):
        try:
            await cb.message.edit_reply_markup(
                reply_markup=kb_vacation(
                    artur.on_vacation if artur else False,
                    andrey.on_vacation if andrey else False,
                )
            )
        except TelegramBadRequest:
            pass


@router.callback_query(F.data == "b:menu")
async def b_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not is_boss(cb.from_user.id):
        return await cb.answer("Нет прав.", show_alert=True)
    await state.clear()
    await _clean_media(bot, cb.from_user.id)
    text = "🧭 <b>Панель начальника</b>\nВыберите действие ниже."
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb_boss_menu(), BOSS_ANCHOR.get(cb.from_user.id))
    BOSS_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


# ====================== Статистика ====================== #

@router.callback_query(F.data == "b:stats")
async def b_stats_root(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    if not is_boss(cb.from_user.id):
        return await cb.answer("Нет прав.", show_alert=True)

    andrey_cur = await _count_current(session, settings.ADMIN_2)
    artur_cur = await _count_current(session, settings.ADMIN_1)

    text = "📊 <b>Статистика</b>\nВыберите сотрудника."
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb_stats_root(artur_cur, andrey_cur), BOSS_ANCHOR.get(cb.from_user.id))
    BOSS_ANCHOR[cb.from_user.id] = new_id
    BOSS_CTX[cb.from_user.id] = {"screen": "stats_root"}
    await cb.answer()


@router.callback_query(F.data == "b:stats:back")
async def b_stats_back(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    return await b_stats_root(cb, session, bot)


@router.callback_query(F.data.startswith("b:stats:emp:"))
async def b_stats_emp(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    if not is_boss(cb.from_user.id):
        return await cb.answer("Нет прав.", show_alert=True)

    emp_id = int((cb.data or "").split(":")[-1])
    text = "👤 <b>Сотрудник</b>\nВыберите тип задач."
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb_stats_emp_filters(emp_id), BOSS_ANCHOR.get(cb.from_user.id))
    BOSS_ANCHOR[cb.from_user.id] = new_id
    BOSS_CTX[cb.from_user.id] = {"screen": "emp", "emp_id": emp_id}
    await cb.answer()


async def _fetch_tasks(session: AsyncSession, emp_id: int, mode: str, page: int):
    where = [Task.assignee_tg_id == emp_id]
    if mode == "cur":
        where.append(Task.status.in_(list(OPEN_STATUSES)))
        order = Task.created_at.desc()
    else:
        where.append(Task.status == Status.CLOSED.value)
        order = Task.created_at.desc()

    total_q = await session.execute(select(func.count()).select_from(Task).where(*where))
    total = int(total_q.scalar_one())
    pages = max((total + PAGE - 1) // PAGE, 1)
    page = min(max(page, 1), pages)
    offset = (page - 1) * PAGE

    q = await session.execute(select(Task).where(*where).order_by(order).offset(offset).limit(PAGE))
    tasks = q.scalars().all()
    return tasks, page, pages, total


def _name_for_emp(emp_id: int) -> str:
    return "Андрей К." if emp_id == settings.ADMIN_2 else "Артур"


@router.callback_query(F.data.startswith("b:stats:list:"))
async def b_stats_list(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    if not is_boss(cb.from_user.id):
        return await cb.answer("Нет прав.", show_alert=True)

    parts = (cb.data or "").split(":")
    emp_s, mode, page_s = parts[3], parts[4], parts[5]
    emp_id = int(emp_s)
    page = int(page_s)

    tasks, page, pages, total = await _fetch_tasks(session, emp_id, mode, page)

    items: List[Tuple[int, str]] = [(t.id, f"№{t.id} — {t.category or '—'}") for t in tasks]
    base_open = "b:stats:open"
    base_page = f"b:stats:list:{emp_id}:{mode}"

    kb = kb_grid(items, page, pages, back_cb=f"b:stats:emp:{emp_id}", base_open_prefix=base_open, base_page_prefix=base_page)

    title = "🟢 Актуальные" if mode == "cur" else "✅ Завершённые"
    text = f"👤 <b>{_name_for_emp(emp_id)}</b> — {title}\nВсего: {total}"
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb, BOSS_ANCHOR.get(cb.from_user.id))
    BOSS_ANCHOR[cb.from_user.id] = new_id

    BOSS_CTX[cb.from_user.id] = {"screen": "list", "emp_id": emp_id, "mode": mode, "page": page}
    await _clean_media(bot, cb.from_user.id)
    await cb.answer()


@router.callback_query(F.data.startswith("b:stats:open:"))
async def b_stats_open(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    if not is_boss(cb.from_user.id):
        return await cb.answer("Нет прав.", show_alert=True)

    task_id = int((cb.data or "").split(":")[-1])
    q = await session.execute(select(Task).where(Task.id == task_id))
    task = q.scalars().first()
    if not task:
        return await cb.answer("Заявка не найдена.", show_alert=True)

    rating = task.final_complexity if task.final_complexity is not None else "—"
    text = (
        f"🧾 <b>Заявка №{task.id}</b>\n"
        f"📂 Категория: {escape(task.category or '—')}\n"
        f"🔖 Статус: {escape(task.status or '—')}\n"
        f"⭐️ Оценка: {escape(str(rating))}\n\n"
        f"📝 <b>Описание</b>:\n{escape(task.description or '—')}"
    )

    ctx = BOSS_CTX.get(cb.from_user.id, {})
    if ctx.get("screen") == "list":
        emp_id = ctx.get("emp_id")
        mode = ctx.get("mode")
        page = ctx.get("page", 1)
        back_cb = f"b:stats:list:{emp_id}:{mode}:{page}"
    else:
        back_cb = "b:stats"

    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 Назад", callback_data=back_cb)
    markup = kb.as_markup()

    new_id = await _show_anchor(bot, cb.from_user.id, text, markup, BOSS_ANCHOR.get(cb.from_user.id))
    BOSS_ANCHOR[cb.from_user.id] = new_id

    await _clean_media(bot, cb.from_user.id)
    ares = await session.execute(select(Attachment).where(Attachment.task_id == task.id))
    atts = ares.scalars().all()
    sent_ids: List[int] = []
    media_items = [(a.file_type, a.file_id, a.caption) for a in atts if a.file_type in ("photo", "video", "document")]

    try:
        if len(media_items) >= 2:
            medias = []
            for t, fid, cap in media_items[:10]:
                if t == "photo":
                    medias.append(InputMediaPhoto(media=fid, caption=cap if not medias else None))
                elif t == "video":
                    medias.append(InputMediaVideo(media=fid, caption=cap if not medias else None))
                elif t == "document":
                    medias.append(InputMediaDocument(media=fid, caption=cap if not medias else None))
            msgs = await bot.send_media_group(chat_id=cb.from_user.id, media=medias)
            sent_ids.extend(m.message_id for m in msgs)
        elif len(media_items) == 1:
            t, fid, cap = media_items[0]
            if t == "photo":
                m = await bot.send_photo(cb.from_user.id, fid, caption=cap)
            elif t == "video":
                m = await bot.send_video(cb.from_user.id, fid, caption=cap)
            else:
                m = await bot.send_document(cb.from_user.id, fid, caption=cap)
            sent_ids.append(m.message_id)
    except Exception:
        pass

    try:
        for a in atts:
            if a.file_type == "voice":
                m = await bot.send_voice(cb.from_user.id, a.file_id, caption=a.caption)
                sent_ids.append(m.message_id)
    except Exception:
        pass

    if sent_ids:
        BOSS_MEDIA[cb.from_user.id] = sent_ids

    await cb.answer()


# ====================== Отправленные (задачи босса) ====================== #

def _kb_sent_list(items: List[Tuple[int, str]], page: int, pages: int) -> InlineKeyboardMarkup:
    return kb_grid(items, page, pages, back_cb="b:menu", base_open_prefix="b:sent:open", base_page_prefix="b:sent:p")


@router.callback_query(F.data == "b:sent")
async def b_sent(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    if not is_boss(cb.from_user.id):
        return await cb.answer("Нет прав.", show_alert=True)

    total_q = await session.execute(select(func.count()).select_from(Task).where(Task.author_tg_id == cb.from_user.id))
    total = int(total_q.scalar_one())
    pages = max((total + PAGE - 1) // PAGE, 1)

    q = await session.execute(
        select(Task).where(Task.author_tg_id == cb.from_user.id).order_by(Task.created_at.desc()).offset(0).limit(PAGE)
    )
    tasks = q.scalars().all()
    items = [(t.id, f"№{t.id} — {t.status}") for t in tasks]

    kb = _kb_sent_list(items, 1, pages)
    text = "📤 <b>Отправленные</b>\nВаши задачи."
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb, BOSS_ANCHOR.get(cb.from_user.id))
    BOSS_ANCHOR[cb.from_user.id] = new_id
    await _clean_media(bot, cb.from_user.id)
    await cb.answer()


@router.callback_query(F.data.startswith("b:sent:p:"))
async def b_sent_page(cb: CallbackQuery, session: AsyncSession):
    if not is_boss(cb.from_user.id):
        return await cb.answer("Нет прав.", show_alert=True)
    page = int((cb.data or "").split(":")[-1])

    total_q = await session.execute(select(func.count()).select_from(Task).where(Task.author_tg_id == cb.from_user.id))
    total = int(total_q.scalar_one())
    pages = max((total + PAGE - 1) // PAGE, 1)
    page = min(max(page, 1), pages)
    offset = (page - 1) * PAGE

    q = await session.execute(
        select(Task).where(Task.author_tg_id == cb.from_user.id).order_by(Task.created_at.desc()).offset(offset).limit(PAGE)
    )
    tasks = q.scalars().all()
    items = [(t.id, f"№{t.id} — {t.status}") for t in tasks]

    if isinstance(cb.message, TgMessage):
        try:
            await cb.message.edit_reply_markup(reply_markup=_kb_sent_list(items, page, pages))
        except TelegramBadRequest:
            pass
    await cb.answer()


@router.callback_query(F.data.startswith("b:sent:open:"))
async def b_sent_open(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    if not is_boss(cb.from_user.id):
        return await cb.answer("Нет прав.", show_alert=True)

    task_id = int((cb.data or "").split(":")[-1])
    q = await session.execute(select(Task).where(Task.id == task_id))
    task = q.scalars().first()
    if not task:
        return await cb.answer("Заявка не найдена.", show_alert=True)

    text = (
        f"🧾 <b>Заявка №{task.id}</b>\n"
        f"📂 Категория: {escape(task.category or '—')}\n"
        f"👤 Исполнитель: {escape(str(task.assignee_tg_id) if task.assignee_tg_id else '—')}\n"
        f"🔖 Статус: {escape(task.status or '—')}\n"
        f"📝 <b>Описание</b>:\n{escape(task.description or '—')}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 Назад", callback_data="b:sent")
    markup = kb.as_markup()

    new_id = await _show_anchor(bot, cb.from_user.id, text, markup, BOSS_ANCHOR.get(cb.from_user.id))
    BOSS_ANCHOR[cb.from_user.id] = new_id

    await _clean_media(bot, cb.from_user.id)
    ares = await session.execute(select(Attachment).where(Attachment.task_id == task.id))
    atts = ares.scalars().all()
    mids: List[int] = []
    try:
        for a in atts:
            m = None
            if a.file_type == "photo":
                m = await bot.send_photo(cb.from_user.id, a.file_id, caption=a.caption)
            elif a.file_type == "video":
                m = await bot.send_video(cb.from_user.id, a.file_id, caption=a.caption)
            elif a.file_type == "document":
                m = await bot.send_document(cb.from_user.id, a.file_id, caption=a.caption)
            elif a.file_type == "voice":
                m = await bot.send_voice(cb.from_user.id, a.file_id, caption=a.caption)
            if m:
                mids.append(m.message_id)
    except Exception:
        pass
    if mids:
        BOSS_MEDIA[cb.from_user.id] = mids
    await cb.answer()


# ====================== Назначить задачу (мастер) ====================== #

@router.callback_query(F.data == "b:new")
async def b_new(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_boss(cb.from_user.id):
        return await cb.answer("Нет прав.", show_alert=True)
    await state.set_state(TicketState.boss_new_pick_admin)
    await state.update_data(boss_draft=BossDraft())
    text = "👥 Кому назначить задачу?"
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb_pick_admin(), BOSS_ANCHOR.get(cb.from_user.id))
    BOSS_ANCHOR[cb.from_user.id] = new_id
    await _clean_media(bot, cb.from_user.id)
    await cb.answer()


@router.callback_query(F.data == "b:new:cancel")
async def b_new_cancel(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_boss(cb.from_user.id):
        return await cb.answer("Нет прав.", show_alert=True)
    await state.clear()
    await _clean_media(bot, cb.from_user.id)
    text = "🧭 <b>Панель начальника</b>\nВыберите действие ниже."
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb_boss_menu(), BOSS_ANCHOR.get(cb.from_user.id))
    BOSS_ANCHOR[cb.from_user.id] = new_id
    await cb.answer("Отменено")


@router.callback_query(F.data == "b:new:back:admin")
async def b_back_admin(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_boss(cb.from_user.id):
        return await cb.answer("Нет прав.", show_alert=True)
    await state.set_state(TicketState.boss_new_pick_admin)
    text = "👥 Кому назначить задачу?"
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb_pick_admin(), BOSS_ANCHOR.get(cb.from_user.id))
    BOSS_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


@router.callback_query(F.data == "b:new:back:prio")
async def b_back_prio(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_boss(cb.from_user.id):
        return await cb.answer("Нет прав.", show_alert=True)
    await state.set_state(TicketState.boss_new_pick_priority)
    text = "⚡️ Выбери приоритет:"
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb_pick_priority(), BOSS_ANCHOR.get(cb.from_user.id))
    BOSS_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


@router.callback_query(TicketState.boss_new_pick_admin, F.data.startswith("b:new:pick_admin:"))
async def b_pick_admin(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_boss(cb.from_user.id):
        return await cb.answer("Нет прав.", show_alert=True)
    admin_id = int((cb.data or "").split(":")[-1])
    d = await _get_draft(state)
    d.assignee = admin_id
    await state.update_data(boss_draft=d)

    await state.set_state(TicketState.boss_new_pick_priority)
    text = "⚡️ Выбери приоритет:"
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb_pick_priority(), BOSS_ANCHOR.get(cb.from_user.id))
    BOSS_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


@router.callback_query(TicketState.boss_new_pick_priority, F.data.startswith("b:new:prio:"))
async def b_pick_prio(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_boss(cb.from_user.id):
        return await cb.answer("Нет прав.", show_alert=True)

    raw = (cb.data or "").split(":")[-1].lower().strip()

    d = await _get_draft(state)
    if raw == "high":
        d.priority = Priority.HIGH.value
    elif raw == "low":
        d.priority = Priority.LOW.value
    else:
        d.priority = Priority.MEDIUM.value
    await state.update_data(boss_draft=d)

    await state.set_state(TicketState.boss_new_collect)
    text = (
        "📝 Опиши задачу текстом и/или приложи медиа (фото/видео/голос/док).\n"
        "Когда закончишь — жми <b>«✅ Готово»</b>.\n"
        "Можно отменить — <b>«✖️ Отмена»</b>."
    )
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb_collect(), BOSS_ANCHOR.get(cb.from_user.id))
    BOSS_ANCHOR[cb.from_user.id] = new_id
    await _clean_media(bot, cb.from_user.id)
    await cb.answer()


@router.message(TicketState.boss_new_collect)
async def b_collect(message: Message, state: FSMContext):
    if not message.from_user or not is_boss(message.from_user.id):
        return
    d = await _get_draft(state)
    updated = False
    if message.text:
        d.description += (("\n" if d.description else "") + message.text)
        updated = True
    elif message.photo:
        d.attachments.append(("photo", message.photo[-1].file_id, message.caption, message.media_group_id))
        updated = True
    elif message.video:
        d.attachments.append(("video", message.video.file_id, message.caption, message.media_group_id))
        updated = True
    elif message.voice:
        d.attachments.append(("voice", message.voice.file_id, message.caption, message.media_group_id))
        updated = True
    elif message.document:
        d.attachments.append(("document", message.document.file_id, message.caption, message.media_group_id))
        updated = True
    if updated:
        await state.update_data(boss_draft=d)


@router.callback_query(TicketState.boss_new_collect, F.data == "b:new:done")
async def b_done(cb: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext):
    if not is_boss(cb.from_user.id):
        return await cb.answer("Нет прав.", show_alert=True)

    d = await _get_draft(state)
    if not d.assignee:
        return await cb.answer("Не выбран исполнитель.", show_alert=True)

    # Не назначаем заранее: чтобы "Принять" работало штатно в admin.py
    task = Task(
        author_tg_id=cb.from_user.id,
        assignee_tg_id=None,
        category="— от начальника —",
        description=d.description.strip(),
        status=Status.NEW.value,
        priority=d.priority,
        is_internal=True,
        user_visible=False,
    )
    session.add(task)
    await session.flush()

    for t, fid, cap, mg in d.attachments:
        session.add(Attachment(task_id=task.id, file_id=fid, file_type=t, caption=cap, media_group_id=mg))
    await session.commit()

    try:
        ares = await session.execute(select(Attachment).where(Attachment.task_id == task.id))
        atts = ares.scalars().all()
        await _send_boss_task_to_admin(bot, d.assignee, task, atts)
    except Exception:
        pass

    await state.clear()
    await _clean_media(bot, cb.from_user.id)

    text = "🧭 <b>Панель начальника</b>\n✅ Задача создана и отправлена."
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb_boss_menu(), BOSS_ANCHOR.get(cb.from_user.id))
    BOSS_ANCHOR[cb.from_user.id] = new_id
    await cb.answer("Создано ✅")


@router.callback_query(TicketState.boss_new_collect, F.data == "b:new:cancel")
async def b_cancel_collect(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not is_boss(cb.from_user.id):
        return await cb.answer("Нет прав.", show_alert=True)
    await state.clear()
    await _clean_media(bot, cb.from_user.id)
    text = "🧭 <b>Панель начальника</b>\nВыберите действие ниже."
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb_boss_menu(), BOSS_ANCHOR.get(cb.from_user.id))
    BOSS_ANCHOR[cb.from_user.id] = new_id
    await cb.answer("Отменено")


# ----------------- заглушки ----------------- #
@router.callback_query(F.data == "b:nop")
async def b_nop(cb: CallbackQuery):
    await cb.answer()
