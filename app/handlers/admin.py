# app/handlers/app_handlers_admin.py
from __future__ import annotations

from typing import Optional, Dict, List, Tuple, Sequence
import asyncio
import time as _time
from datetime import datetime, timedelta, timezone, date
from html import escape

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.types import Message as TgMessage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_, update

from app.config import settings
from app.enums import Status
from app.keyboards import (
    admin_menu,
    rating_kb,
    report_finish_kb,
)
from app.models import Task, User, Attachment
from app.states import AdminReport
from app.services.telegraph_report import TelegraphClient, TelegraphConfig

# общий реестр message_id карточек, чтобы удалять у обоих админов
from app.services.assignment import InMemoryNotifications, cleanup_admin_cards

router = Router(name="admin")

# ===== Time helpers =====
# В БД даты/время хранятся как naive UTC (datetime.utcnow()).
# Для отображения в отчётах/календаре переводим в локальную TZ машины, где запущен бот.

def _utc_naive_to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()

def _local_date_range_to_utc(start_d: date, end_d: date) -> tuple[datetime, datetime]:
    # Local calendar [start_d, end_d) -> UTC naive datetimes for DB фильтра.
    start_local = datetime(start_d.year, start_d.month, start_d.day)
    end_local = datetime(end_d.year, end_d.month, end_d.day)

    # time.mktime() интерпретирует naive datetime как local time и корректно учитывает DST.
    start_ts = _time.mktime(start_local.timetuple())
    end_ts = _time.mktime(end_local.timetuple())

    return datetime.utcfromtimestamp(start_ts), datetime.utcfromtimestamp(end_ts)

def _local_date_to_utc_range(d: date) -> tuple[datetime, datetime]:
    return _local_date_range_to_utc(d, d + timedelta(days=1))


# ===== Константы =====
PAGE_SIZE = 9  # статистика 3x3

# Категории для мастера «Добавить себе…» (эмодзи + порядок; «Другое» — последним)
ADMIN_CATEGORIES: List[Tuple[str, str]] = [
    ("Интернет", "🌐"), ("Принтер", "🖨"), ("Компьютер", "💻"),
    ("Ноутбук", "💼"), ("Монитор", "🖥"), ("Почта", "✉️"),
    ("Телефония", "📞"), ("Wi-Fi", "📶"), ("VPN", "🛡️"),
    ("Сеть/Сервер", "🖧"), ("Доступы/Права", "🔑"), ("Аккаунт/Пароль", "🔐"),
    ("ПО", "🧩"), ("1C", "📑"), ("ОС/Windows", "🪟"),
    ("ВКС/Конференции", "🎥"), ("Вирус/Безопасность", "🛡️"), ("Сайт/CRM", "🕸"),
    ("Мобильная связь", "📱"), ("ЭЦП", "🔏"), ("Удаленка", "🏠"),
    ("Пропуск", "🎟"), ("Доступ в дверь", "🚪"),
    ("Другое", "➕"),
]

# Для быстрых подпесей: категория -> эмодзи
CATEGORY_EMOJI: Dict[str, str] = {name: emoji for name, emoji in ADMIN_CATEGORIES}

# Список для политик назначения (без эмодзи)
CATEGORIES: List[str] = [name for name, _ in ADMIN_CATEGORIES]

ARTUR_ID: Optional[int] = settings.ADMIN_1
ANDREY_K_ID: Optional[int] = settings.ADMIN_2
BOSS_ANDREY_T_ID: Optional[int] = settings.BOSS

ONLY_ARTUR = {"Компьютер", "Удаленка", "1C", "1С"}
ONLY_ANDREY = {"Пропуск", "Доступ в дверь"}
BOTH = {"Интернет", "Мобильная связь", "Принтер", "ЭЦП", "Другое"}

MONTH_NAMES_RU = {
    1: "Январь",
    2: "Февраль",
    3: "Март",
    4: "Апрель",
    5: "Май",
    6: "Июнь",
    7: "Июль",
    8: "Август",
    9: "Сентябрь",
    10: "Октябрь",
    11: "Ноябрь",
    12: "Декабрь",
}


def _policy_for(category: str) -> str:
    if category in ONLY_ARTUR:
        return "ARTUR"
    if category in ONLY_ANDREY:
        return "ANDREY"
    if category in BOTH:
        return "BOTH"
    return "BOTH"


# Безопасная «константа» для статуса ASSIGNED (чтобы не ругался анализатор)
try:
    STATUS_ASSIGNED_VALUE: str = Status.ASSIGNED.value  # type: ignore[attr-defined]
except Exception:
    STATUS_ASSIGNED_VALUE = Status.IN_PROGRESS.value

# ===== Технические контейнеры =====
VIEWER: Dict[int, List[int]] = {}
ADMIN_LAST_NOTIFY: Dict[int, int] = {}
ADMIN_TRASH: Dict[int, List[int]] = {}

# ЕДИНЫЙ якорь для экрана администратора (и панель, и списки рисуем в одном сообщении)
ADMIN_ANCHOR: Dict[int, int] = {}

# Telegraph alert в шапке (у тебя уже есть _with_alert — используем его)
ADMIN_TGRAPH_ALERT: Dict[int, str] = {}  # admin_id -> alert_text


# ===== Вспомогательные =====
def is_admin(user_id: int) -> bool:
    try:
        return int(user_id) in settings.staff_ids
    except Exception:
        return False


def _get_telegraph_client() -> Optional[TelegraphClient]:
    token = settings.TELEGRAPH_TOKEN
    if not token:
        return None
    cfg = TelegraphConfig(
        access_token=token,
        author_name=settings.TELEGRAPH_AUTHOR_NAME or "HardyBot",
        author_url=settings.TELEGRAPH_AUTHOR_URL or None,
    )
    return TelegraphClient(cfg)


def _pick_colleague(me_id: int) -> Tuple[int, str]:
    uid = int(me_id)
    if uid == ANDREY_K_ID and ARTUR_ID:
        return ARTUR_ID, "Артуру"
    if uid == ARTUR_ID and ANDREY_K_ID:
        return ANDREY_K_ID, "Андрею"
    if ANDREY_K_ID:
        return ANDREY_K_ID, "Андрею"
    # запасной кейс — если нет ANDREY_K_ID
    return (ARTUR_ID or uid), "Артуру"


def _other_admin_id(admin_id: int) -> Optional[int]:
    if admin_id == ARTUR_ID and ANDREY_K_ID:
        return ANDREY_K_ID
    if admin_id == ANDREY_K_ID and ARTUR_ID:
        return ARTUR_ID
    return None


def _admin_name(admin_id: int) -> str:
    if admin_id == ARTUR_ID:
        return "Артур"
    if admin_id == ANDREY_K_ID:
        return "Андрей"
    return "Администратор"


def _short(s: Optional[str], n: int = 22) -> str:
    if not s:
        return "—"
    return s if len(s) <= n else s[: n - 1] + "…"


def _shift_month(year: int, month: int, delta: int) -> Tuple[int, int]:
    """Сдвиг месяца на delta с учётом переходов года."""
    month += delta
    while month < 1:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return year, month


def _month_title(year: int, month: int) -> str:
    return f"{MONTH_NAMES_RU.get(month, str(month))} {year}"


def _with_alert(base_text: str, alert: Optional[str]) -> str:
    if not alert:
        return base_text
    return f"{base_text}\n\n{alert}"


def _tgraph_set_alert(admin_id: int, text: Optional[str]) -> None:
    if not text:
        ADMIN_TGRAPH_ALERT.pop(admin_id, None)
    else:
        ADMIN_TGRAPH_ALERT[admin_id] = text


def _tgraph_get_alert(admin_id: int) -> Optional[str]:
    return ADMIN_TGRAPH_ALERT.get(admin_id)


def _tgraph_clear_alert(admin_id: int) -> None:
    ADMIN_TGRAPH_ALERT.pop(admin_id, None)


# ---------- очистки ----------
async def _clear_viewer(bot: Bot, admin_id: int) -> None:
    ids = VIEWER.pop(admin_id, None)
    if not ids:
        return
    for mid in ids:
        try:
            await bot.delete_message(admin_id, mid)
        except Exception:
            pass


async def _clear_last_notify(bot: Bot, admin_id: int) -> None:
    mid = ADMIN_LAST_NOTIFY.pop(admin_id, None)
    if mid:
        try:
            await bot.delete_message(admin_id, mid)
        except Exception:
            pass


async def _clear_trash(bot: Bot, admin_id: int) -> None:
    trash = ADMIN_TRASH.pop(admin_id, [])
    for mid in trash:
        try:
            await bot.delete_message(admin_id, mid)
        except Exception:
            pass


def _trash_add(admin_id: int, mid: int) -> None:
    ADMIN_TRASH.setdefault(admin_id, []).append(mid)


# удалить карточку(и) «Новая заявка…» у указанного админа, если мы их трекали
async def _remove_task_card_if_any(bot: Bot, task_id: int, admin_id: int) -> None:
    infos = InMemoryNotifications.get_admin_msgs(task_id, admin_id)
    if not infos:
        return
    for chat_id, message_id in infos:
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            pass
    InMemoryNotifications.forget_admin(task_id, admin_id)


# ---- пользовательское уведомление о закрытии: удаление/автоудаление ----
async def _delete_user_notice_if_any(bot: Bot, task_id: int) -> None:
    info = InMemoryNotifications.get_user_msg(task_id)
    if not info:
        return
    chat_id, message_id = info
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass
    InMemoryNotifications.forget_user(task_id)


async def _auto_del_user_notice(bot: Bot, task_id: int, delay_sec: int = 300) -> None:
    try:
        await asyncio.sleep(delay_sec)
        await _delete_user_notice_if_any(bot, task_id)
    except Exception:
        pass


# ---------- универсальный рендерер «якоря» ----------
async def _show_anchor(
    bot: Bot,
    chat_id: int,
    text: str,
    kb: InlineKeyboardMarkup,
    anchor_id: Optional[int],
) -> int:
    """
    Пытаемся ОБНОВИТЬ существующий якорь.
    Если не получилось (нет сообщения / ошибка), пробуем удалить и шлём новый.
    """
    if anchor_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=anchor_id, text=text, reply_markup=kb)
            return anchor_id
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                try:
                    await bot.edit_message_reply_markup(
                        chat_id=chat_id,
                        message_id=anchor_id,
                        reply_markup=kb,
                    )
                    return anchor_id
                except Exception:
                    pass
            try:
                await bot.delete_message(chat_id, anchor_id)
            except Exception:
                pass

    msg = await bot.send_message(chat_id, text, reply_markup=kb)
    return msg.message_id


# ---------- показ единой панели ----------
async def _show_admin_panel(bot: Bot, admin_id: int) -> None:
    """Всегда держим ОДНО сообщение-«якорь» на чат администратора."""
    await _clear_viewer(bot, admin_id)
    await _clear_last_notify(bot, admin_id)
    await _clear_trash(bot, admin_id)

    new_id = await _show_anchor(bot, admin_id, "🧰 Панель администратора:", admin_menu(), ADMIN_ANCHOR.get(admin_id))
    ADMIN_ANCHOR[admin_id] = new_id


# ======= Уведомления (читаемые тексты) =======
def _fmt_minimal_new_task(task: Task) -> str:
    return f"№{task.id} — {task.category}\nСтатус: {task.status}"


def _fmt_user_accepted(task: Task, assignee_name: str) -> str:
    return (
        f"✅ Ваша заявка №{task.id} принята.\n"
        f"Ей занимается: <b>{assignee_name}</b>.\n"
        f"Категория: <b>{task.category}</b>\n"
        f"Мы свяжемся с вами при необходимости."
    )


def _fmt_user_assigned_immediately(task: Task, assignee_name: str) -> str:
    return (
        f"✅ Ваша заявка №{task.id} назначена специалисту: <b>{assignee_name}</b>.\n"
        f"Категория: <b>{task.category}</b>"
    )


async def send_minimal_new_task_notify(bot: Bot, admin_id: int, task: Task) -> None:
    text = _fmt_minimal_new_task(task)
    m = await bot.send_message(admin_id, text)
    ADMIN_LAST_NOTIFY[admin_id] = m.message_id


# ====== Подсчёт открытых задач для балансировки ======
OPEN_STATUSES = {
    Status.NEW.value,
    STATUS_ASSIGNED_VALUE,
    Status.IN_PROGRESS.value,
    (getattr(Status, "WAITING", Status.IN_PROGRESS).value if hasattr(Status, "WAITING") else Status.IN_PROGRESS.value),
    (getattr(Status, "REOPENED", Status.IN_PROGRESS).value if hasattr(Status, "REOPENED") else Status.IN_PROGRESS.value),
}


async def _count_open_tasks(session: AsyncSession, assignee_tg_id: int) -> int:
    q = select(func.count()).select_from(Task).where(
        Task.assignee_tg_id == assignee_tg_id,
        Task.status.in_(list(OPEN_STATUSES)),
    )
    res = await session.execute(q)
    return int(res.scalar_one())


# ===================== БАЗОВОЕ МЕНЮ АДМИНА =====================
@router.message(Command("admin"))
async def cmd_admin(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or not is_admin(user.id):
        await message.answer("Доступно только администраторам.")
        return
    await _show_admin_panel(bot, user.id)


# ===================== МОИ ЗАДАЧИ =====================
def _my_tasks_kb(tasks: Sequence[Task], me: int) -> InlineKeyboardMarkup:
    """
    ДВЕ колонки на строку:
      [ 📄 {id} · {cat} ]   [ ▶️ Начать / ✅ Готово ]
    """
    kb = InlineKeyboardBuilder()

    for t in tasks:
        left = InlineKeyboardButton(
            text=f"📄 {t.id} · {_short(t.category)}",
            callback_data=f"a:view:{t.id}",
        )

        # справа — действие
        if t.status == Status.NEW.value and t.assignee_tg_id is None:
            right = InlineKeyboardButton(text="▶️ Начать", callback_data=f"a:accept:{t.id}")
        else:
            # всё, что уже на мне и не закрыто — можно завершать
            right = InlineKeyboardButton(text="✅ Готово", callback_data=f"a:done:{t.id}")

        kb.row(left, right)

    # низ: назад в панель
    kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="a:back_admin"))
    return kb.as_markup()


async def _render_my_tasks(session: AsyncSession, me: int) -> Tuple[str, InlineKeyboardMarkup]:
    res = await session.execute(
        select(Task).where(
            and_(
                Task.status != Status.CLOSED.value,
                or_(
                    Task.assignee_tg_id == me,
                    and_(Task.assignee_tg_id.is_(None), Task.status == Status.NEW.value),
                ),
            )
        ).order_by(Task.created_at.desc()),
    )
    tasks = list(res.scalars().all())

    if not tasks:
        text = "Пока нет задач."
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="a:back_admin"))
        return text, kb.as_markup()

    return "📂 Мои задачи:", _my_tasks_kb(tasks, me)


async def _show_my_tasks(bot: Bot, session: AsyncSession, admin_id: int) -> None:
    text, markup = await _render_my_tasks(session, admin_id)
    new_id = await _show_anchor(bot, admin_id, text, markup, ADMIN_ANCHOR.get(admin_id))
    ADMIN_ANCHOR[admin_id] = new_id


@router.callback_query(F.data == "a:back_admin")
async def cb_back_admin(cb: CallbackQuery, bot: Bot) -> None:
    await _show_admin_panel(bot, cb.from_user.id)
    await cb.answer()


@router.callback_query(F.data == "a:list")
async def cb_list(cb: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав.", show_alert=True)
        return
    await _clear_last_notify(bot, cb.from_user.id)
    await _show_my_tasks(bot, session, cb.from_user.id)
    await cb.answer()


# ===================== ПРИНЯТЬ / ЗАКРЫТЬ / ОТЧЁТ / ОЦЕНКА =====================
@router.callback_query(F.data.startswith("a:accept:"))
async def cb_accept(cb: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    """
    «Кто успел — того и задача». Атомарно назначаем.
    После успешного принятия карточка удаляется у ОБОИХ админов.
    Никаких дополнительных окон/кнопок «Готово!» не показываем.
    """
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав.", show_alert=True)
        return

    try:
        task_id = int((cb.data or "").split(":")[2])
    except Exception:
        await cb.answer("Некорректный callback.", show_alert=True)
        return

    q = (
        update(Task)
        .where(Task.id == task_id, Task.status == Status.NEW.value, Task.assignee_tg_id.is_(None))
        .values(status=STATUS_ASSIGNED_VALUE, assignee_tg_id=cb.from_user.id)
    )
    res = await session.execute(q)
    if getattr(res, "rowcount", 0) and res.rowcount > 0:
        await session.commit()

        # удалить карточки у обоих админов (все сообщения по задаче)
        await _remove_task_card_if_any(bot, task_id, cb.from_user.id)
        other_id = _other_admin_id(cb.from_user.id)
        if other_id:
            await _remove_task_card_if_any(bot, task_id, other_id)

        # удалить текущее сообщение (если оно не было в реестре)
        if isinstance(cb.message, TgMessage):
            try:
                await cb.message.delete()
            except Exception:
                pass

        # подчистить мини-уведомления и обновить список задач
        await _clear_last_notify(bot, cb.from_user.id)
        if other_id:
            await _clear_last_notify(bot, other_id)
        await _show_my_tasks(bot, session, cb.from_user.id)

        await cb.answer("Заявка закреплена за вами ✅")
        return

    # ---- не получилось — уже забрали
    if isinstance(cb.message, TgMessage):
        try:
            await cb.message.delete()
        except Exception:
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

    t_res = await session.execute(select(Task).where(Task.id == task_id))
    task = t_res.scalars().first()
    if task and task.assignee_tg_id:
        await cb.answer(f"Уже забрано: {_admin_name(task.assignee_tg_id)}", show_alert=False)
    else:
        await cb.answer("Не удалось взять заявку. Обновите список.", show_alert=False)

    # синхронизируем якорь со списком
    await _show_my_tasks(bot, session, cb.from_user.id)


@router.callback_query(F.data.startswith("a:done:"))
async def cb_done(cb: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext) -> None:
    """Переводим задачу в CLOSED и просим отправить отчёт."""
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав.", show_alert=True)
        return

    try:
        task_id = int((cb.data or "").split(":")[2])
    except Exception:
        await cb.answer("Некорректный callback.", show_alert=True)
        return

    res = await session.execute(select(Task).where(Task.id == task_id))
    task = res.scalars().first()
    if not task:
        await cb.answer("Задача не найдена.", show_alert=True)
        return

    task.assignee_tg_id = cb.from_user.id
    task.status = Status.CLOSED.value
    if getattr(task, 'closed_at', None) is None:
        task.closed_at = datetime.utcnow()
    await session.commit()

    # подчистим любые карточки/медиа по этому task_id у всех админов
    try:
        await cleanup_admin_cards(bot, task.id)
    except Exception:
        pass

    # отправим пользователю уведомление и ЗАПОМНИМ, чтобы удалить позже
    if task.user_visible and task.author_tg_id:
        try:
            m = await bot.send_message(task.author_tg_id, f"✅ Заявка №{task.id}: выполнено")
            InMemoryNotifications.remember_user(task.id, task.author_tg_id, m.message_id)
            # автоудаление через 5 минут (можно отключить/изменить)
            asyncio.create_task(_auto_del_user_notice(bot, task.id, delay_sec=300))
        except Exception:
            pass

    await state.set_state(AdminReport.collecting)
    await state.update_data(report_task_id=task.id, report_user_id=task.author_tg_id)

    if isinstance(cb.message, TgMessage):
        m = await cb.message.answer(
            f"Задача №{task.id} закрыта.\n"
            f"Отправь пользователю текст/фото/видео/голос как отчёт. "
            f"Когда закончишь — нажми кнопку ниже.",
            reply_markup=report_finish_kb(task.id),
        )
    else:
        m = await bot.send_message(
            cb.from_user.id,
            f"Задача №{task.id} закрыта.\n"
            f"Отправь пользователю текст/фото/видео/голос как отчёт. "
            f"Когда закончишь — нажми кнопку ниже.",
            reply_markup=report_finish_kb(task.id),
        )
    _trash_add(cb.from_user.id, m.message_id)
    await cb.answer("Закрыто")


@router.message(AdminReport.collecting, F.content_type.in_({"text", "photo", "video", "voice", "document"}))
async def report_forward(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    user_id = data.get("report_user_id")
    if not user_id:
        return
    try:
        if message.text:
            await bot.send_message(user_id, f"Сообщение от администратора:\n{message.text}")
        elif message.photo:
            await bot.send_photo(user_id, message.photo[-1].file_id, caption=message.caption)
        elif message.video:
            await bot.send_video(user_id, message.video.file_id, caption=message.caption)
        elif message.voice:
            await bot.send_voice(user_id, message.voice.file_id, caption=message.caption)
        elif message.document:
            await bot.send_document(user_id, message.document.file_id, caption=message.caption)
        await message.answer("Отправлено пользователю ✅")
    except Exception:
        await message.answer("Не удалось отправить пользователю (возможно, он не писал боту).")


@router.callback_query(F.data.startswith("a:report_finish:"))
async def cb_report_finish(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await cb.answer("Готово")
    # удалим пользовательское уведомление о закрытии, если ещё висит
    try:
        task_id = int((cb.data or "").split(":")[-1])
        await _delete_user_notice_if_any(bot, task_id)
    except Exception:
        pass

    # безопасная правка: cb.message может быть недоступен
    if isinstance(cb.message, TgMessage):
        try:
            await cb.message.edit_text("Оцени сложность задачи по 10-балльной шкале:")
            await cb.message.edit_reply_markup(reply_markup=rating_kb(int((cb.data or "").split(":")[-1])))
        except TelegramBadRequest:
            await cb.message.answer(
                "Оцени сложность задачи по 10-балльной шкале:",
                reply_markup=rating_kb(int((cb.data or "").split(":")[-1])),
            )
        _trash_add(cb.from_user.id, cb.message.message_id)


@router.callback_query(F.data.startswith("a:rate:"))
async def cb_rate(cb: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    try:
        _, _, task_id_s, score_s = (cb.data or "").split(":")
        task_id = int(task_id_s)
        score = int(score_s)
    except Exception:
        await cb.answer("Некорректный callback.", show_alert=True)
        return

    if score < 1 or score > 10:
        await cb.answer("1–10 только", show_alert=True)
        return

    res = await session.execute(select(Task).where(Task.id == task_id))
    task = res.scalars().first()
    if not task:
        await cb.answer("Не нашёл задачу.", show_alert=True)
        return

    task.final_complexity = score
    await session.commit()
    await cb.answer("Спасибо!")

    # на всякий случай ещё раз попробуем удалить пользовательское уведомление
    await _delete_user_notice_if_any(bot, task.id)

    await _clear_viewer(bot, cb.from_user.id)
    await _clear_trash(bot, cb.from_user.id)
    await _show_admin_panel(bot, cb.from_user.id)


# ===================== ПРОСМОТР ЗАДАЧИ (из списка «Мои задачи») =====================
@router.callback_query(F.data.startswith("a:view:"))
async def view_task(cb: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав.", show_alert=True)
        return

    try:
        task_id = int((cb.data or "").split(":")[-1])
    except Exception:
        await cb.answer("Некорректный callback.", show_alert=True)
        return

    res = await session.execute(select(Task).where(Task.id == task_id))
    task = res.scalars().first()
    if not task:
        await cb.answer("Задача не найдена.", show_alert=True)
        return

    # очистить ранее показанные карточки этой сессии просмотра
    await _clear_viewer(bot, cb.from_user.id)
    sent_ids: List[int] = []

    # автор (для обратной совместимости берём из users, если в Task нет снимка)
    author_name = "-"
    if task.author_tg_id:
        ures = await session.execute(select(User).where(User.tg_id == task.author_tg_id))
        u = ures.scalars().first()
        if u:
            author_name = u.full_name

    rating = task.final_complexity if task.final_complexity is not None else "—"
    caption = (
        f"🧾 <b>Заявка №{task.id}</b>\n"
        f"👤 <b>Автор:</b> {escape(task.author_full_name or author_name or '—')} "
        f"{('· доб. ' + escape(task.author_sip)) if task.author_sip else ''} "
        f"({task.author_tg_id})\n"
        f"📌 <b>Категория:</b> {escape(task.category or '—')}\n"
        f"📍 <b>Статус:</b> {escape(task.status or '—')}\n"
        f"⭐ <b>Оценка (1–10):</b> {rating}\n\n"
        f"📝 <b>Описание:</b>\n{escape(task.description or '—')}"
    )

    # вложения
    ares = await session.execute(select(Attachment).where(Attachment.task_id == task.id))
    attachments = ares.scalars().all()
    media_items = [
        (a.file_type, a.file_id, a.caption)
        for a in attachments
        if a.file_type in ("photo", "video", "document")
    ]
    voices = [a for a in attachments if a.file_type == "voice"]

    try:
        if len(media_items) == 1:
            typ, fid, _ = media_items[0]
            if typ == "photo":
                m = await bot.send_photo(cb.from_user.id, fid, caption=caption, parse_mode="HTML")
            elif typ == "video":
                m = await bot.send_video(cb.from_user.id, fid, caption=caption, parse_mode="HTML")
            elif typ == "document":
                m = await bot.send_document(cb.from_user.id, fid, caption=caption, parse_mode="HTML")
            else:
                if isinstance(cb.message, TgMessage):
                    m = await cb.message.answer(caption, parse_mode="HTML")
                else:
                    m = await bot.send_message(cb.from_user.id, caption, parse_mode="HTML")
            sent_ids.append(m.message_id)

        elif len(media_items) >= 2:
            # альбом без подписей
            medias = []
            for t, fid, _ in media_items[:10]:
                if t == "photo":
                    medias.append(InputMediaPhoto(media=fid))
                elif t == "video":
                    medias.append(InputMediaVideo(media=fid))
                elif t == "document":
                    medias.append(InputMediaDocument(media=fid))
            msgs = await bot.send_media_group(chat_id=cb.from_user.id, media=medias)
            sent_ids.extend(m.message_id for m in msgs)

            # остаток >10 — следующими альбомами
            rest = media_items[10:]
            while rest:
                batch, rest = rest[:10], rest[10:]
                more = []
                for t, fid, _ in batch:
                    if t == "photo":
                        more.append(InputMediaPhoto(media=fid))
                    elif t == "video":
                        more.append(InputMediaVideo(media=fid))
                    elif t == "document":
                        more.append(InputMediaDocument(media=fid))
                more_msgs = await bot.send_media_group(chat_id=cb.from_user.id, media=more)
                sent_ids.extend(m.message_id for m in more_msgs)

            # текст — отдельным сообщением НИЖЕ альбома
            if isinstance(cb.message, TgMessage):
                txt = await cb.message.answer(caption, parse_mode="HTML")
            else:
                txt = await bot.send_message(cb.from_user.id, caption, parse_mode="HTML")
            sent_ids.append(txt.message_id)

        else:
            # медиа нет — просто текст
            if isinstance(cb.message, TgMessage):
                txt = await cb.message.answer(caption, parse_mode="HTML")
            else:
                txt = await bot.send_message(cb.from_user.id, caption, parse_mode="HTML")
            sent_ids.append(txt.message_id)
    except Exception:
        # как минимум покажем текст
        if isinstance(cb.message, TgMessage):
            txt = await cb.message.answer(caption, parse_mode="HTML")
        else:
            txt = await bot.send_message(cb.from_user.id, caption, parse_mode="HTML")
        sent_ids.append(txt.message_id)

    # голосовые — по одному
    for a in voices:
        try:
            vm = await bot.send_voice(cb.from_user.id, a.file_id, caption=a.caption)
            sent_ids.append(vm.message_id)
        except Exception:
            pass

    VIEWER[cb.from_user.id] = sent_ids
    await cb.answer("Открыто")


# ===================== СТАТИСТИКА (пагинация + просмотр заявки) =====================
def _stats_kb(items: List[Tuple[int, str]], page: int, pages: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for task_id, label in items:
        kb.button(text=label, callback_data=f"a:stats:open:{task_id}")
    if len(items) < PAGE_SIZE:
        for _ in range(PAGE_SIZE - len(items)):
            kb.button(text="‎", callback_data="a:stats:nop")
    prev_page = max(page - 1, 1)
    next_page = min(page + 1, pages)
    kb.button(text="◀️", callback_data=f"a:stats:p:{prev_page}")
    kb.button(text=f"Page. {page}/{pages}", callback_data="a:stats:nop")
    kb.button(text="▶️", callback_data=f"a:stats:p:{next_page}")
    kb.button(text="🔙 Назад", callback_data="a:stats:back")
    kb.adjust(3, 3, 3, 3, 1)
    return kb.as_markup()


def _stats_label_for_task(t: Task) -> str:
    emoji = CATEGORY_EMOJI.get(t.category or "", "•")
    score = t.final_complexity if t.final_complexity is not None else "—"
    return f"{emoji} №{t.id} · {score}"


async def _fetch_stats_page(session: AsyncSession, page: int, me: int) -> Tuple[List[Task], int, int]:
    # считаем только мои закрытые
    total_q = await session.execute(
        select(func.count()).select_from(Task).where(
            Task.status == Status.CLOSED.value,
            Task.assignee_tg_id == me,
        ),
    )
    total = int(total_q.scalar_one())
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(max(page, 1), pages)
    offset = (page - 1) * PAGE_SIZE
    q = await session.execute(
        select(Task)
        .where(Task.status == Status.CLOSED.value, Task.assignee_tg_id == me)
        .order_by(Task.created_at.desc())
        .offset(offset)
        .limit(PAGE_SIZE),
    )
    tasks = list(q.scalars().all())
    return tasks, pages, total


async def _render_stats_markup(session: AsyncSession, page: int, me: int) -> InlineKeyboardMarkup:
    tasks, pages, _ = await _fetch_stats_page(session, page, me)
    items: List[Tuple[int, str]] = [(t.id, _stats_label_for_task(t)) for t in tasks]
    return _stats_kb(items, page, pages)


@router.callback_query(F.data == "a:stats")
async def stats_root(cb: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав.", show_alert=True)
        return
    kb = await _render_stats_markup(session, page=1, me=cb.from_user.id)
    if isinstance(cb.message, TgMessage):
        try:
            await cb.message.edit_text("Закрытые заявки (твои):", reply_markup=kb)
        except TelegramBadRequest as e:
            text = str(e).lower()
            if "message is not modified" in text:
                pass
            else:
                try:
                    await cb.message.edit_reply_markup(reply_markup=kb)
                except TelegramBadRequest as e2:
                    if "message is not modified" in str(e2).lower():
                        pass
                    else:
                        try:
                            await cb.message.delete()
                        except Exception:
                            pass
                        await cb.message.answer("Закрытые заявки (твои):", reply_markup=kb)
    else:
        # нет доступного сообщения — просто шлём новое
        await bot.send_message(cb.from_user.id, "Закрытые заявки (твои):", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("a:stats:p:"))
async def stats_page(cb: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав.", show_alert=True)
        return
    try:
        page = int((cb.data or "").split(":")[-1])
    except Exception:
        page = 1
    kb = await _render_stats_markup(session, page=page, me=cb.from_user.id)
    if isinstance(cb.message, TgMessage):
        try:
            await cb.message.edit_reply_markup(reply_markup=kb)
        except TelegramBadRequest as e:
            text = str(e).lower()
            if "message is not modified" in text:
                await cb.answer()
                return
            try:
                await cb.message.edit_text("Закрытые заявки (твои):", reply_markup=kb)
            except TelegramBadRequest as e2:
                if "message is not modified" in str(e2).lower():
                    pass
                else:
                    try:
                        await cb.message.delete()
                    except Exception:
                        pass
                    await cb.message.answer("Закрытые заявки (твои):", reply_markup=kb)
    else:
        await bot.send_message(cb.from_user.id, "Закрытые заявки (твои):", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "a:stats:nop")
async def stats_nop(cb: CallbackQuery) -> None:
    await cb.answer("Листайте ◀️ / ▶️")


@router.callback_query(F.data == "a:stats:back")
async def stats_back(cb: CallbackQuery, bot: Bot) -> None:
    await _clear_viewer(bot, cb.from_user.id)
    await _show_admin_panel(bot, cb.from_user.id)
    await cb.answer()


@router.callback_query(F.data.startswith("a:stats:open:"))
async def stats_open(cb: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    # Используем тот же просмотр, что и из "Мои задачи"
    await view_task(cb, session, bot)


# ===================== СОЗДАТЬ ЗАДАЧУ (админ) =====================
class AdminCreate(StatesGroup):
    pick_category = State()
    collecting = State()
    pick_assignee = State()


def _categories_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    # категории — строго по 3 в ряд
    for name, emoji in ADMIN_CATEGORIES:
        kb.button(text=f"{emoji} {name}", callback_data=f"a:add:cat:{name}")
    kb.adjust(3)
    # отдельной строкой — «Назад»
    kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="a:back_admin"))
    return kb.as_markup()


def _create_collect_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Готово ✅", callback_data="a:add:done")
    kb.button(text="Отмена ❌", callback_data="a:add:cancel")
    kb.adjust(2)
    return kb.as_markup()


def _assignee_kb(my_id: int) -> InlineKeyboardMarkup:
    _colleague_id, label = _pick_colleague(my_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="Себе", callback_data=f"a:add:who:{my_id}")
    kb.button(text=label, callback_data="a:add:who:colleague")
    kb.button(text="🔙 Назад", callback_data="a:back_admin")
    kb.adjust(2, 1)
    return kb.as_markup()


async def _wizard_trash_clear(state: FSMContext, bot: Bot, admin_id: int) -> None:
    data = await state.get_data()
    hint_id = data.get("hint_id")
    if hint_id:
        try:
            await bot.delete_message(admin_id, hint_id)
        except Exception:
            pass
    await state.update_data(hint_id=None)


@router.callback_query(F.data == "a:add")
async def add_task_start(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Запуск мастера — показываем выбор категории В ЯКОРЕ (без новых сообщений)."""
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав.", show_alert=True)
        return
    await state.clear()
    await _clear_trash(bot, cb.from_user.id)  # подчистим старые подсказки, если были
    await state.set_state(AdminCreate.pick_category)

    new_id = await _show_anchor(
        bot,
        cb.from_user.id,
        "Выбери категорию:",
        _categories_kb(),
        ADMIN_ANCHOR.get(cb.from_user.id),
    )
    ADMIN_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


@router.callback_query(F.data.startswith("a:add:cat:"))
async def add_pick_category(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Выбрали категорию — остаёмся в якоре и переходим к сбору описания/медиа."""
    cat = (cb.data or "").split(":", 3)[-1]
    await state.update_data(cat=cat, texts=[], atts=[], hint_id=None)
    await state.set_state(AdminCreate.collecting)
    text = (
        f"Категория: <b>{escape(cat)}</b>\n"
        f"Добавь описание и/или медиа (фото/видео/док/голос).\n"
        f"Когда закончишь — нажми «✅Готово»."
    )
    new_id = await _show_anchor(
        bot,
        cb.from_user.id,
        text,
        _create_collect_kb(),
        ADMIN_ANCHOR.get(cb.from_user.id),
    )
    ADMIN_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


@router.message(AdminCreate.collecting, F.content_type.in_({"text", "photo", "video", "voice", "document"}))
async def add_collect(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    texts: List[str] = data.get("texts", []) or []
    atts: List[Dict[str, Optional[str]]] = data.get("atts", []) or []
    last_hint = data.get("hint_id")

    if last_hint:
        try:
            await bot.delete_message(message.chat.id, last_hint)
        except Exception:
            pass

    if message.text:
        texts.append(message.text)
    elif message.photo:
        atts.append({"type": "photo", "id": message.photo[-1].file_id, "cap": message.caption})
    elif message.video:
        atts.append({"type": "video", "id": message.video.file_id, "cap": message.caption})
    elif message.voice:
        atts.append({"type": "voice", "id": message.voice.file_id, "cap": message.caption})
    elif message.document:
        atts.append({"type": "document", "id": message.document.file_id, "cap": message.caption})

    await state.update_data(texts=texts, atts=atts)
    hint = await message.answer("Добавлено ✅")
    await state.update_data(hint_id=hint.message_id)


@router.callback_query(F.data == "a:add:done")
async def add_done(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Завершили сбор — в том же якоре спрашиваем исполнителя."""
    await state.set_state(AdminCreate.pick_assignee)
    await _wizard_trash_clear(state, bot, cb.from_user.id)
    new_id = await _show_anchor(
        bot,
        cb.from_user.id,
        "Кому назначить задачу?",
        _assignee_kb(cb.from_user.id),
        ADMIN_ANCHOR.get(cb.from_user.id),
    )
    ADMIN_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


@router.callback_query(F.data == "a:add:cancel")
async def add_cancel(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await _wizard_trash_clear(state, bot, cb.from_user.id)
    await _clear_trash(bot, cb.from_user.id)
    await state.clear()
    await cb.answer("Отменено")
    await _show_admin_panel(bot, cb.from_user.id)


@router.callback_query(F.data.startswith("a:add:who:"))
async def add_pick_assignee(cb: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    cat = data.get("cat")
    texts: List[str] = data.get("texts", []) or []
    atts: List[Dict[str, Optional[str]]] = data.get("atts", []) or []

    who = (cb.data or "").split(":")[-1]
    if who == "colleague":
        assignee_id, _ = _pick_colleague(cb.from_user.id)
    else:
        try:
            assignee_id = int(who)
        except Exception:
            assignee_id = cb.from_user.id

    task = Task(
        category=cat,
        description="\n".join(texts) if texts else None,
        status=Status.NEW.value,
        author_tg_id=cb.from_user.id,
        user_visible=False,
        assignee_tg_id=assignee_id,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    for a in atts:
        session.add(
            Attachment(
                task_id=task.id,
                file_type=a["type"],
                file_id=a["id"],
                caption=a.get("cap"),
            )
        )
    await session.commit()

    if assignee_id != cb.from_user.id:
        try:
            await send_minimal_new_task_notify(bot, assignee_id, task)
        except Exception:
            pass

    await _wizard_trash_clear(state, bot, cb.from_user.id)
    await _clear_trash(bot, cb.from_user.id)
    await state.clear()
    await cb.answer("Создано")

    if assignee_id == cb.from_user.id:
        await _show_my_tasks(bot, session, cb.from_user.id)
    else:
        await _show_admin_panel(bot, cb.from_user.id)


# ==================== Telegraph: отчёты с календарём и картинками ====================
async def _build_admin_tgraph_for_period(
    viewer_id: int,
    session: AsyncSession,
    bot: Bot,
    start: datetime,
    end: datetime,
) -> Optional[str]:
    """
    Собирает задачи администратора viewer_id за период [start, end) и создаёт
    страницу в Telegraph (с картинками из вложений).

    Возвращает строку-«алерт» для вывода в шапке (в якоре), либо None.
    НИЧЕГО не отправляет отдельными сообщениями в чат.
    """
    client = _get_telegraph_client()
    if client is None:
        return "⚠️ Telegraph не настроен (нужен TELEGRAPH_TOKEN)."

    me = viewer_id
    q = await session.execute(
        select(Task).where(
            Task.status == Status.CLOSED.value,
            Task.assignee_tg_id == me,
            Task.created_at >= start,
            Task.created_at < end,
        ).order_by(Task.id),
    )
    tasks = q.scalars().all()

    if not tasks:
        return "⚠️ За выбранный период закрытых задач нет."

    start_local = _utc_naive_to_local(start)
    end_local = _utc_naive_to_local(end)

    days_span = (end_local.date() - start_local.date()).days
    if days_span == 1:
        title_suffix = start_local.strftime("%d.%m.%Y")
        human = f"за {title_suffix}"
    else:
        start_s = start_local.strftime("%d.%m.%Y")
        end_s = (end_local.date() - timedelta(days=1)).strftime("%d.%m.%Y")
        human = f"за период {start_s}–{end_s}"
        title_suffix = f"{start_s}–{end_s}"

    title = f"Отчёт по задачам админа {me} {title_suffix}"

    try:
        url = await client.create_tasks_page(title, tasks, bot=bot, session=session)
    except Exception as e:
        return f"⚠️ Не удалось создать отчёт в Telegraph: {e}"

    return f"✅ Готово. Ваш отчёт {human}:\n{url}"


def _tgraph_root_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Сегодня", callback_data="a:tgraph:today")
    kb.button(text="День", callback_data="a:tgraph:day")
    kb.button(text="Неделя", callback_data="a:tgraph:week")
    kb.button(text="Месяц", callback_data="a:tgraph:month")
    kb.button(text="⬅️ Назад", callback_data="a:back_admin")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def _tgraph_root_text(admin_id: int, alert: Optional[str] = None) -> str:
    # внешний alert (из builder) имеет приоритет, иначе берём сохранённый
    use_alert = alert if alert is not None else _tgraph_get_alert(admin_id)
    base = "📄 <b>Telegraph-отчёт</b>\nВыберите период:"
    return _with_alert(base, use_alert)


def _tgraph_day_kb(year: int, month: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    prev_y, prev_m = _shift_month(year, month, -1)
    next_y, next_m = _shift_month(year, month, 1)

    # центральная кнопка — просто актуальная дата, без действия
    today = datetime.now()
    center_label = today.strftime("%d.%m")

    kb.row(
        InlineKeyboardButton(
            text="◀️",
            callback_data=f"a:tgraph:day:month:{prev_y:04d}-{prev_m:02d}",
        ),
        InlineKeyboardButton(text=center_label, callback_data="a:tgraph:nop"),
        InlineKeyboardButton(
            text="▶️",
            callback_data=f"a:tgraph:day:month:{next_y:04d}-{next_m:02d}",
        ),
    )

    # заголовки дней недели
    weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    kb.row(*[InlineKeyboardButton(text=w, callback_data="a:tgraph:nop") for w in weekdays])

    first = datetime(year, month, 1)
    if month == 12:
        next_first = datetime(year + 1, 1, 1)
    else:
        next_first = datetime(year, month + 1, 1)
    num_days = (next_first - first).days

    start_weekday = first.weekday()  # Пн=0..Вс=6

    cells: List[InlineKeyboardButton] = []
    for _ in range(start_weekday):
        cells.append(InlineKeyboardButton(text=" ", callback_data="a:tgraph:nop"))

    for day in range(1, num_days + 1):
        date_str = f"{year:04d}-{month:02d}-{day:02d}"
        cells.append(
            InlineKeyboardButton(
                text=str(day),
                callback_data=f"a:tgraph:day:pick:{date_str}",
            )
        )

    while len(cells) % 7 != 0:
        cells.append(InlineKeyboardButton(text=" ", callback_data="a:tgraph:nop"))

    for i in range(0, len(cells), 7):
        kb.row(*cells[i: i + 7])

    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a:tgraph"))
    return kb.as_markup()


def _tgraph_day_text(year: int, month: int, admin_id: int, alert: Optional[str] = None) -> str:
    use_alert = alert if alert is not None else _tgraph_get_alert(admin_id)
    base = f"📅 Telegraph-отчёт (день)\n{_month_title(year, month)}\nВыберите дату."
    return _with_alert(base, use_alert)


def _tgraph_week_kb(year: int, month: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    prev_y, prev_m = _shift_month(year, month, -1)
    next_y, next_m = _shift_month(year, month, 1)

    # центральная кнопка — актуальная неделя (пн–вс) без действия
    today = datetime.now()
    base_dt = datetime(year=today.year, month=today.month, day=today.day)
    monday = base_dt - timedelta(days=base_dt.weekday())
    sunday = monday + timedelta(days=6)
    center_label = f"{monday.strftime('%d.%m')}-{sunday.strftime('%d.%m')}"

    kb.row(
        InlineKeyboardButton(
            text="◀️",
            callback_data=f"a:tgraph:week:month:{prev_y:04d}-{prev_m:02d}",
        ),
        InlineKeyboardButton(text=center_label, callback_data="a:tgraph:nop"),
        InlineKeyboardButton(
            text="▶️",
            callback_data=f"a:tgraph:week:month:{next_y:04d}-{next_m:02d}",
        ),
    )

    first = datetime(year, month, 1)
    if month == 12:
        last_day = datetime(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = datetime(year, month + 1, 1) - timedelta(days=1)

    # понедельник недели, в которую попадает первый день месяца
    monday = first - timedelta(days=first.weekday())
    limit = last_day + timedelta(days=6)

    while monday <= limit:
        sunday = monday + timedelta(days=6)
        label = f"{monday.strftime('%d.%m')}-{sunday.strftime('%d.%m')} (пн–вс)"
        cb_data = f"a:tgraph:week:pick:{monday.strftime('%Y-%m-%d')}"
        kb.row(InlineKeyboardButton(text=label, callback_data=cb_data))
        monday += timedelta(days=7)

    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a:tgraph"))
    return kb.as_markup()


def _tgraph_week_text(year: int, month: int, admin_id: int, alert: Optional[str] = None) -> str:
    use_alert = alert if alert is not None else _tgraph_get_alert(admin_id)
    base = f"📅 Telegraph-отчёт (неделя)\n{_month_title(year, month)}\nВыберите неделю (пн–вс)."
    return _with_alert(base, use_alert)


def _tgraph_month_kb(year: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    prev_y = year - 1
    next_y = year + 1

    today = datetime.now()
    current_month_label = f"{MONTH_NAMES_RU.get(today.month, str(today.month))} {today.year}"

    kb.row(
        InlineKeyboardButton(
            text=f"◀️ {prev_y}",
            callback_data=f"a:tgraph:month:year:{prev_y}",
        ),
        InlineKeyboardButton(
            text=current_month_label,
            callback_data="a:tgraph:nop",
        ),
        InlineKeyboardButton(
            text=f"{next_y} ▶️",
            callback_data=f"a:tgraph:month:year:{next_y}",
        ),
    )

    # 12 месяцев сеткой 3x4
    month_buttons: List[InlineKeyboardButton] = []
    for m in range(1, 13):
        label = MONTH_NAMES_RU.get(m, str(m))
        cb_data = f"a:tgraph:month:pick:{year:04d}-{m:02d}"
        month_buttons.append(InlineKeyboardButton(text=label, callback_data=cb_data))

    for i in range(0, len(month_buttons), 3):
        kb.row(*month_buttons[i: i + 3])

    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="a:tgraph"))
    return kb.as_markup()


def _tgraph_month_text(year: int, admin_id: int, alert: Optional[str] = None) -> str:
    use_alert = alert if alert is not None else _tgraph_get_alert(admin_id)
    base = f"📅 Telegraph-отчёт (месяц)\nГод: {year}\nВыберите месяц."
    return _with_alert(base, use_alert)


@router.callback_query(F.data == "a:tgraph")
async def admin_tgraph_root(cb: CallbackQuery, bot: Bot) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав.", show_alert=True)
        return

    client = _get_telegraph_client()
    if client is None:
        await cb.answer("Telegraph не настроен (нужен TELEGRAPH_TOKEN).", show_alert=True)
        return

    _tgraph_clear_alert(cb.from_user.id)

    text = _tgraph_root_text(cb.from_user.id, alert=None)
    anchor = ADMIN_ANCHOR.get(cb.from_user.id)
    new_id = await _show_anchor(bot, cb.from_user.id, text, _tgraph_root_kb(), anchor)
    ADMIN_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


@router.callback_query(F.data == "a:tgraph:today")
async def admin_tgraph_today(cb: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав.", show_alert=True)
        return

    _tgraph_clear_alert(cb.from_user.id)

    today_local = datetime.now().date()
    start, end = _local_date_to_utc_range(today_local)

    alert = await _build_admin_tgraph_for_period(
        viewer_id=cb.from_user.id,
        session=session,
        bot=bot,
        start=start,
        end=end,
    )
    _tgraph_set_alert(cb.from_user.id, alert)

    # остаёмся на главном экране telegraph-отчётов и показываем алерт в шапке
    text = _tgraph_root_text(cb.from_user.id)
    anchor = ADMIN_ANCHOR.get(cb.from_user.id)
    new_id = await _show_anchor(bot, cb.from_user.id, text, _tgraph_root_kb(), anchor)
    ADMIN_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


@router.callback_query(F.data == "a:tgraph:day")
async def admin_tgraph_day_root(cb: CallbackQuery, bot: Bot) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав.", show_alert=True)
        return

    _tgraph_clear_alert(cb.from_user.id)

    today = datetime.now()
    year, month = today.year, today.month
    text = _tgraph_day_text(year, month, cb.from_user.id, alert=None)
    kb = _tgraph_day_kb(year, month)
    anchor = ADMIN_ANCHOR.get(cb.from_user.id)
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb, anchor)
    ADMIN_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


@router.callback_query(F.data.startswith("a:tgraph:day:month:"))
async def admin_tgraph_day_month(cb: CallbackQuery, bot: Bot) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав.", show_alert=True)
        return

    _tgraph_clear_alert(cb.from_user.id)

    try:
        _, _, _, _, ym = (cb.data or "").split(":", 4)
        year_s, month_s = ym.split("-")
        year, month = int(year_s), int(month_s)
    except Exception:
        await cb.answer("Некорректный период.", show_alert=True)
        return

    text = _tgraph_day_text(year, month, cb.from_user.id, alert=None)
    kb = _tgraph_day_kb(year, month)
    anchor = ADMIN_ANCHOR.get(cb.from_user.id)
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb, anchor)
    ADMIN_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


@router.callback_query(F.data.startswith("a:tgraph:day:pick:"))
async def admin_tgraph_day_pick(cb: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав.", show_alert=True)
        return

    _tgraph_clear_alert(cb.from_user.id)

    try:
        date_s = (cb.data or "").split(":", 4)[-1]
        dt = datetime.strptime(date_s, "%Y-%m-%d")
    except Exception:
        await cb.answer("Некорректная дата.", show_alert=True)
        return

    start, end = _local_date_to_utc_range(dt.date())

    alert = await _build_admin_tgraph_for_period(
        viewer_id=cb.from_user.id,
        session=session,
        bot=bot,
        start=start,
        end=end,
    )
    _tgraph_set_alert(cb.from_user.id, alert)

    # остаёмся в календаре (месяц выбранной даты) и показываем алерт в шапке
    year, month = dt.year, dt.month
    text = _tgraph_day_text(year, month, cb.from_user.id)
    kb = _tgraph_day_kb(year, month)
    anchor = ADMIN_ANCHOR.get(cb.from_user.id)
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb, anchor)
    ADMIN_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


@router.callback_query(F.data == "a:tgraph:week")
async def admin_tgraph_week_root(cb: CallbackQuery, bot: Bot) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав.", show_alert=True)
        return

    _tgraph_clear_alert(cb.from_user.id)

    today = datetime.now()
    year, month = today.year, today.month
    text = _tgraph_week_text(year, month, cb.from_user.id, alert=None)
    kb = _tgraph_week_kb(year, month)
    anchor = ADMIN_ANCHOR.get(cb.from_user.id)
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb, anchor)
    ADMIN_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


@router.callback_query(F.data.startswith("a:tgraph:week:month:"))
async def admin_tgraph_week_month(cb: CallbackQuery, bot: Bot) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав.", show_alert=True)
        return

    _tgraph_clear_alert(cb.from_user.id)

    try:
        _, _, _, _, ym = (cb.data or "").split(":", 4)
        year_s, month_s = ym.split("-")
        year, month = int(year_s), int(month_s)
    except Exception:
        await cb.answer("Некорректный период.", show_alert=True)
        return

    text = _tgraph_week_text(year, month, cb.from_user.id, alert=None)
    kb = _tgraph_week_kb(year, month)
    anchor = ADMIN_ANCHOR.get(cb.from_user.id)
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb, anchor)
    ADMIN_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


@router.callback_query(F.data.startswith("a:tgraph:week:pick:"))
async def admin_tgraph_week_pick(cb: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав.", show_alert=True)
        return

    _tgraph_clear_alert(cb.from_user.id)

    try:
        date_s = (cb.data or "").split(":", 4)[-1]
        monday = datetime.strptime(date_s, "%Y-%m-%d")
    except Exception:
        await cb.answer("Некорректная дата.", show_alert=True)
        return

    start, end = _local_date_range_to_utc(monday.date(), (monday + timedelta(days=7)).date())

    alert = await _build_admin_tgraph_for_period(
        viewer_id=cb.from_user.id,
        session=session,
        bot=bot,
        start=start,
        end=end,
    )
    _tgraph_set_alert(cb.from_user.id, alert)

    # перерисуем недельный экран (месяц от monday) и покажем алерт в шапке
    year, month = monday.year, monday.month
    text = _tgraph_week_text(year, month, cb.from_user.id)
    kb = _tgraph_week_kb(year, month)
    anchor = ADMIN_ANCHOR.get(cb.from_user.id)
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb, anchor)
    ADMIN_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


@router.callback_query(F.data == "a:tgraph:month")
async def admin_tgraph_month_root(cb: CallbackQuery, bot: Bot) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав.", show_alert=True)
        return

    _tgraph_clear_alert(cb.from_user.id)

    year = datetime.now().year
    text = _tgraph_month_text(year, cb.from_user.id, alert=None)
    kb = _tgraph_month_kb(year)
    anchor = ADMIN_ANCHOR.get(cb.from_user.id)
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb, anchor)
    ADMIN_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


@router.callback_query(F.data.startswith("a:tgraph:month:year:"))
async def admin_tgraph_month_year(cb: CallbackQuery, bot: Bot) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав.", show_alert=True)
        return

    _tgraph_clear_alert(cb.from_user.id)

    try:
        year = int((cb.data or "").split(":")[-1])
    except Exception:
        await cb.answer("Некорректный год.", show_alert=True)
        return

    text = _tgraph_month_text(year, cb.from_user.id, alert=None)
    kb = _tgraph_month_kb(year)
    anchor = ADMIN_ANCHOR.get(cb.from_user.id)
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb, anchor)
    ADMIN_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


@router.callback_query(F.data.startswith("a:tgraph:month:pick:"))
async def admin_tgraph_month_pick(cb: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет прав.", show_alert=True)
        return

    _tgraph_clear_alert(cb.from_user.id)

    try:
        ym = (cb.data or "").split(":", 4)[-1]
        year_s, month_s = ym.split("-")
        year, month = int(year_s), int(month_s)
    except Exception:
        await cb.answer("Некорректный месяц.", show_alert=True)
        return

    start_d = date(year, month, 1)
    if month == 12:
        end_d = date(year + 1, 1, 1)
    else:
        end_d = date(year, month + 1, 1)

    start, end = _local_date_range_to_utc(start_d, end_d)

    alert = await _build_admin_tgraph_for_period(
        viewer_id=cb.from_user.id,
        session=session,
        bot=bot,
        start=start,
        end=end,
    )
    _tgraph_set_alert(cb.from_user.id, alert)

    # остаёмся на выборе месяцев того же года и показываем алерт
    text = _tgraph_month_text(year, cb.from_user.id)
    kb = _tgraph_month_kb(year)
    anchor = ADMIN_ANCHOR.get(cb.from_user.id)
    new_id = await _show_anchor(bot, cb.from_user.id, text, kb, anchor)
    ADMIN_ANCHOR[cb.from_user.id] = new_id
    await cb.answer()


@router.callback_query(F.data == "a:tgraph:nop")
async def admin_tgraph_nop(cb: CallbackQuery) -> None:
    await cb.answer()


# ===================== Прочее =====================
@router.callback_query(F.data == "a:nop")
async def noop(cb: CallbackQuery) -> None:
    await cb.answer()
