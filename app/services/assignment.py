# app/services/assignment.py
from __future__ import annotations
from html import escape
import logging
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, List

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.enums import Status
from app.models import Task, User
from app.keyboards import (
    admin_task_actions_kb,
    admin_task_claimed_kb,
    admin_back_kb,
)

logger = logging.getLogger(__name__)

# ----------------------------
# Безопасные алиасы статусов (если каких-то значений нет в enum)
# ----------------------------
ASSIGNED_STATUS = getattr(Status, "ASSIGNED", Status.IN_PROGRESS)
WAITING_STATUS = getattr(Status, "WAITING", Status.IN_PROGRESS)
REOPENED_STATUS = getattr(Status, "REOPENED", Status.IN_PROGRESS)

OPEN_STATUSES = {
    Status.NEW.value,
    ASSIGNED_STATUS.value,
    Status.IN_PROGRESS.value,
    WAITING_STATUS.value,
    REOPENED_STATUS.value,
}

# ----------------------------
# Администраторы и имена
# ----------------------------
ARTUR_ID = settings.ADMIN_1
ANDREY_ID = settings.ADMIN_2

ADMIN_NAMES: Dict[int, str] = {
    ARTUR_ID: "Артур",
    ANDREY_ID: "Андрей",
}


def _admin_name(uid: Optional[int]) -> str:
    try:
        return ADMIN_NAMES.get(int(uid or 0), "Администратор")
    except Exception:
        return "Администратор"


# ------------------------------------------------------------
# Routing/assignment helpers
#
# Текущая политика:
#   • Все заявки, направляемые в IT, должны приходить ОБОИМ админам.
#   • Заявка назначается тому, кто первым нажал «▶️ Начать / Взять».
#
# Ранее здесь была логика распределения по категориям/балансировка.
# Она отключена по требованию: никакого «разделения» между ADMIN_1/ADMIN_2.
# ------------------------------------------------------------


# ----------------------------
# Категорийная политика (ОТКЛЮЧЕНА)
# ----------------------------
def _policy_for(_category: str) -> str:
    """Всегда возвращаем BOTH: уведомляем обоих админов."""
    return "BOTH"


# ----------------------------
# Память message_id (поддержка МНОЖЕСТВА сообщений на админа)
# ----------------------------
class InMemoryNotifications:
    # task_id -> (admin_id -> List[(chat_id, message_id)])
    _admin_msgs: Dict[int, Dict[int, List[Tuple[int, int]]]] = {}
    # task_id -> (user_chat_id, message_id)
    _user_msg: Dict[int, Tuple[int, int]] = {}

    # ---- admin ----
    @classmethod
    def remember_admin(cls, task_id: int, admin_id: int, chat_id: int, message_id: int):
        cls._admin_msgs.setdefault(task_id, {}).setdefault(admin_id, []).append((chat_id, message_id))

    @classmethod
    def get_admin_msg(cls, task_id: int, admin_id: int) -> Optional[Tuple[int, int]]:
        """Сохранена для обратной совместимости: возвращает ПОСЛЕДНИЙ message_id (если нужен один)."""
        lst = cls._admin_msgs.get(task_id, {}).get(admin_id)
        return lst[-1] if lst else None

    @classmethod
    def get_admin_msgs(cls, task_id: int, admin_id: int) -> List[Tuple[int, int]]:
        """Новый метод: вернуть список всех (chat_id, message_id) для админа по задаче."""
        return list(cls._admin_msgs.get(task_id, {}).get(admin_id, []))

    @classmethod
    def forget_admin(cls, task_id: int, admin_id: int):
        d = cls._admin_msgs.get(task_id)
        if not d:
            return
        d.pop(admin_id, None)
        if not d:
            cls._admin_msgs.pop(task_id, None)

    @classmethod
    def forget_admin_all(cls, task_id: int):
        cls._admin_msgs.pop(task_id, None)

    # ---- user ----
    @classmethod
    def remember_user(cls, task_id: int, chat_id: int, message_id: int):
        cls._user_msg[task_id] = (chat_id, message_id)

    @classmethod
    def get_user_msg(cls, task_id: int) -> Optional[Tuple[int, int]]:
        return cls._user_msg.get(task_id)

    @classmethod
    def forget_user(cls, task_id: int):
        cls._user_msg.pop(task_id, None)


# ----------------------------
# Тексты
# ----------------------------
CATEGORY_EMOJI: dict[str, str] = {
    "Интернет": "🌐",
    "Принтер": "🖨",
    "Компьютер": "💻",
    "1C": "🧾",
    "ЭЦП": "🔏",
    "Удаленка": "🏠",
    "Пропуск": "🎫",
    "Доступ в дверь": "🚪",
    "Другое": "➕",
}


def _cat_label(name: Optional[str]) -> str:
    if not name:
        return "<b>—</b>"
    emoji = CATEGORY_EMOJI.get(name, "")
    emoji = (emoji + " ") if emoji else ""
    return f"{emoji}<b>{escape(name)}</b>"


def _author_label(full_name: Optional[str], sip: Optional[str], tg_id: Optional[int]) -> str:
    fio = escape(full_name) if full_name else "Без ФИО"
    ext = escape(str(sip)) if sip else "—"
    tail = f" · tg:<code>{tg_id}</code>" if tg_id else ""
    return f"<b>{fio}</b> · доб. <b>{ext}</b>{tail}"


def _blockquote(text: Optional[str]) -> str:
    if not text:
        return "—"
    body = escape(text).strip()
    return f"<blockquote>{body}</blockquote>"


def fmt_task_card_for_admin(
    task: Task,
    author_full_name: str | None = None,
    author_sip: str | None = None,
) -> str:
    return (
        f"🆕 <b>Новая заявка №{task.id}</b>\n"
        f"👤 Автор: {_author_label(author_full_name, author_sip, task.author_tg_id)}\n"
        f"🏷️ Категория: {_cat_label(task.category)}\n"
        f"📝 Сообщение:\n{_blockquote(task.description)}"
    )


def fmt_task_claimed_for_admin(task: Task, assignee_name: str) -> str:
    return (
        f"✅ <b>Заявка №{task.id}</b>\n"
        f"🏷️ Категория: {_cat_label(task.category)}\n"
        f"👨‍🔧 Исполнитель: <b>{escape(assignee_name)}</b>\n"
        f"Статус: <b>назначена</b>."
    )


def fmt_taken_notice_for_other_admin(task_id: int, assignee_name: str) -> str:
    return (
        f"ℹ️ Заявку №{task_id} забрал <b>{escape(assignee_name)}</b>.\n"
        f"Карточка скрыта."
    )


def fmt_user_accepted(task: Task, assignee_name: str) -> str:
    return (
        f"✅ Ваша заявка №{task.id} принята.\n"
        f"Ей занимается: <b>{escape(assignee_name)}</b>.\n"
        f"🏷️ Категория: {_cat_label(task.category)}\n"
        f"Мы свяжемся с вами при необходимости."
    )


def fmt_user_assigned_immediately(task: Task, assignee_name: str) -> str:
    return (
        f"✅ Ваша заявка №{task.id} назначена специалисту: <b>{escape(assignee_name)}</b>.\n"
        f"🏷️ Категория: {_cat_label(task.category)}"
    )


# ----------------------------
# Подсчёт открытых задач
# ----------------------------
async def count_open_tasks(session: AsyncSession, assignee_tg_id: int) -> int:
    q = select(func.count()).select_from(Task).where(
        Task.assignee_tg_id == assignee_tg_id,
        Task.status.in_(list(OPEN_STATUSES)),
    )
    res = await session.execute(q)
    return int(res.scalar_one())


# ----------------------------
# Отправка карточек (одно сообщение с кнопкой «Принять»)
# ----------------------------
async def _send_admin_card(bot: Bot, session: AsyncSession, admin_id: int, task: Task):
    author_name: Optional[str] = None
    if task.author_tg_id:
        u = (await session.execute(select(User).where(User.tg_id == task.author_tg_id))).scalars().first()
        if u:
            author_name = u.full_name

    text = fmt_task_card_for_admin(task, author_name)
    kb = admin_task_actions_kb(task.id)
    msg = await bot.send_message(admin_id, text, reply_markup=kb)
    InMemoryNotifications.remember_admin(task.id, admin_id, admin_id, msg.message_id)
    return msg


async def _delete_admin_cards_if_any(bot: Bot, task_id: int, admin_id: int):
    infos = InMemoryNotifications.get_admin_msgs(task_id, admin_id)
    if not infos:
        return
    for chat_id, message_id in infos:
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            pass
    InMemoryNotifications.forget_admin(task_id, admin_id)


async def _edit_or_delete_other_admin(bot: Bot, task_id: int, other_admin_id: Optional[int], assignee_name: str):
    if not other_admin_id:
        return
    infos = InMemoryNotifications.get_admin_msgs(task_id, other_admin_id)
    if not infos:
        return
    first = True
    for chat_id, message_id in infos:
        try:
            if first:
                await bot.edit_message_text(
                    fmt_taken_notice_for_other_admin(task_id, assignee_name),
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=admin_back_kb(),
                )
                first = False
            else:
                await bot.delete_message(chat_id, message_id)
        except TelegramBadRequest:
            try:
                await bot.delete_message(chat_id, message_id)
            except Exception:
                pass
    InMemoryNotifications.forget_admin(task_id, other_admin_id)


async def _notify_user_accepted(bot: Bot, task: Task, assignee_name: str):
    uinfo = InMemoryNotifications.get_user_msg(task.id)
    if uinfo:
        chat_id, message_id = uinfo
        try:
            await bot.edit_message_text(
                fmt_user_accepted(task, assignee_name),
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=None,
                parse_mode="HTML",
            )
            return
        except TelegramBadRequest:
            try:
                await bot.delete_message(chat_id, message_id)
            except Exception:
                pass
            InMemoryNotifications.forget_user(task.id)
    try:
        await bot.send_message(task.author_tg_id, fmt_user_accepted(task, assignee_name), parse_mode="HTML")
    except Exception:
        pass


# ----------------------------
# Публичные API (новое)
# ----------------------------
@dataclass
class NewTaskDispatchResult:
    sent_to: Tuple[bool, bool]  # (to_artur, to_andrey)
    assigned_immediately_to: Optional[int]


async def dispatch_new_task(bot: Bot, session: AsyncSession, task: Task) -> NewTaskDispatchResult:
    """
    Вызов после сохранения task: отправить карточку «Принять» ОБОИМ админам.
    Назначение происходит только после нажатия админом кнопки «Принять/Взять».
    """
    _ = _policy_for(task.category or "")

    sent_artur = False
    sent_andrey = False

    if ARTUR_ID:
        try:
            await _send_admin_card(bot, session, ARTUR_ID, task)
            sent_artur = True
        except Exception:
            pass

    if ANDREY_ID and ANDREY_ID != ARTUR_ID:
        try:
            await _send_admin_card(bot, session, ANDREY_ID, task)
            sent_andrey = True
        except Exception:
            pass

    return NewTaskDispatchResult((sent_artur, sent_andrey), assigned_immediately_to=None)


async def admin_try_claim_task(bot: Bot, session: AsyncSession, task_id: int, admin_tg_id: int) -> Tuple[bool, Optional[str]]:
    q = (
        update(Task)
        .where(Task.id == task_id, Task.status == Status.NEW.value, Task.assignee_tg_id.is_(None))
        .values(status=ASSIGNED_STATUS.value, assignee_tg_id=admin_tg_id)
    )
    res = await session.execute(q)
    if res.rowcount and res.rowcount > 0:
        await session.commit()

        for admin_id in (ARTUR_ID, ANDREY_ID):
            if admin_id:
                await _delete_admin_cards_if_any(bot, task_id, admin_id)

        t_res = await session.execute(select(Task).where(Task.id == task_id))
        task = t_res.scalars().first()
        assignee_name = _admin_name(admin_tg_id)
        if task is not None:
            await _notify_user_accepted(bot, task, assignee_name)
        return True, assignee_name

    t_res = await session.execute(select(Task).where(Task.id == task_id))
    task = t_res.scalars().first()
    if not task or not task.assignee_tg_id:
        return False, None
    winner = _admin_name(task.assignee_tg_id)
    return False, winner


async def admin_hide_task_card(bot: Bot, task_id: int, admin_tg_id: int):
    await _delete_admin_cards_if_any(bot, task_id, admin_tg_id)


async def cleanup_admin_cards(bot: Bot, task_id: int):
    for admin_id in (ARTUR_ID, ANDREY_ID):
        if admin_id:
            await _delete_admin_cards_if_any(bot, task_id, admin_id)


# ----------------------------
# Backward-compat API (старое имя и сигнатура)
# ----------------------------
async def assign_by_category(session: AsyncSession, category: str) -> Tuple[Tuple[int, ...], Optional[int]]:
    """
    Старый контракт, который использует user.py:
    Возвращает (notify_ids, assignee_id_or_None). НИЧЕГО не отправляет.

    Новая политика: уведомлять ОБОИХ админов без балансировки/разделения.
    Назначение (assignee) всегда None до нажатия «Принять».
    """
    _ = _policy_for(category or "")

    ids: List[int] = []
    for v in (ARTUR_ID, ANDREY_ID):
        if v and v not in ids:
            ids.append(int(v))
    return tuple(ids), None
