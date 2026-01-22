# app/keyboards.py

from __future__ import annotations

from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup

# ---------- Пользовательское главное меню и категории ----------

USER_CATEGORIES: list[tuple[str, str, str]] = [
    ("Интернет", "🌐", "internet"),
    ("Мобильная связь", "📶", "mobile"),
    ("1С", "🧾", "1c"),
    ("Удалёнка", "🏠", "remote"),
    ("Принтер", "🖨", "printer"),
    ("Компьютер", "💻", "computer"),
    ("Пропуск", "🎫", "pass"),
    ("Доступ в дверь", "🚪", "door"),
    ("ЭЦП", "🔏", "ecp"),
    ("Другое", "➕", "other"),
]

STATUS_EMOJI = {
    "NEW": "📨 Отправлен",
    "ACCEPTED": "🛠️ В работе",
    "IN_PROGRESS": "🛠️ В работе",
    "CLOSED": "✅ Завершён",
}


def user_main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🆕 Новая заявка", callback_data="u:new")
    kb.button(text="📚 История", callback_data="u:history:p:1")
    kb.button(text="👤 Профиль", callback_data="u:profile")
    kb.adjust(1, 1, 1)
    return kb.as_markup()


def categories_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for title, emoji, slug in USER_CATEGORIES:
        kb.button(text=f"{emoji} {title}", callback_data=f"u:cat:{slug}")
    kb.button(text="⬅️ Назад", callback_data="u:back")
    kb.adjust(2, 2, 2, 2, 2, 1)
    return kb.as_markup()


def done_cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Готово ✅", callback_data="done_collect")
    kb.button(text="Отмена ❌", callback_data="cancel_collect")
    kb.adjust(2)
    return kb.as_markup()


def cancel_only_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Отмена ❌", callback_data="cancel_collect")
    kb.adjust(1)
    return kb.as_markup()


# ---------- Профиль / Регистрация ----------

def profile_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить ФИО", callback_data="u:profile:edit_name")
    kb.button(text="✏️ Изменить SIP", callback_data="u:profile:edit_sip")
    kb.button(text="🏠 Меню", callback_data="u:menu")
    kb.adjust(2, 1)
    return kb.as_markup()


def reg_confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    # 1-я строка: Изменить SIP | Изменить ФИО
    kb.button(text="✏️ Изменить SIP", callback_data="reg:edit_sip")
    kb.button(text="✏️ Изменить ФИО", callback_data="reg:edit_name")
    # 2-я строка: Отменить | Подтвердить
    kb.button(text="❌ Отменить", callback_data="reg:cancel")
    kb.button(text="✅ Подтвердить", callback_data="reg:confirm")
    kb.adjust(2, 2)
    return kb.as_markup()



# ---------- Админ ----------

def admin_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Мои задачи", callback_data="a:list")
    kb.button(text="Добавить себе...", callback_data="a:add")
    kb.button(text="Статистика", callback_data="a:stats")
    kb.button(text="📄 Отчёт (Telegraph)", callback_data="a:tgraph")
    kb.adjust(1, 1, 1, 1)
    return kb.as_markup()


def admin_accept_kb(task_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Принять", callback_data=f"a:accept:{task_id}")
    kb.adjust(1)
    return kb.as_markup()


def admin_done_kb(task_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Готово!", callback_data=f"a:done:{task_id}")
    kb.adjust(1)
    return kb.as_markup()


def rating_kb(task_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i in range(1, 11):
        kb.button(text=str(i), callback_data=f"a:rate:{task_id}:{i}")
    kb.adjust(5, 5)
    return kb.as_markup()


def report_finish_kb(task_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Завершить отчёт", callback_data=f"a:report_finish:{task_id}")
    kb.adjust(1)
    return kb.as_markup()


def admin_task_actions_kb(task_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Взять в работу", callback_data=f"a:task:claim:{task_id}")
    kb.button(text="🙈 Скрыть", callback_data=f"a:task:hide:{task_id}")
    kb.button(text="⬅️ Назад", callback_data="a:back")
    kb.adjust(1, 1, 1)
    return kb.as_markup()


def admin_task_claimed_kb(task_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="a:back")
    kb.adjust(1)
    return kb.as_markup()


def admin_back_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="a:back")
    kb.adjust(1)
    return kb.as_markup()


# ---------- Босс (совместимость) ----------

def boss_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🆕 Назначить задачу", callback_data="b:new")
    kb.button(text="📊 Статистика", callback_data="b:stats")
    kb.button(text="☀️ Отпуски", callback_data="b:vac")
    kb.adjust(2, 1)
    return kb.as_markup()


def pick_admin_kb(artur_id: int, andrey_k_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Артур Б.", callback_data=f"b:pick_admin:{artur_id}")
    kb.button(text="Андрей К.", callback_data=f"b:pick_admin:{andrey_k_id}")
    kb.adjust(2)
    return kb.as_markup()


def pick_priority_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Высокий", callback_data="b:prio:HIGH")
    kb.button(text="Средний", callback_data="b:prio:MEDIUM")
    kb.button(text="Низкий", callback_data="b:prio:LOW")
    kb.adjust(3)
    return kb.as_markup()


def vacation_kb(
    artur_on_vac: bool,
    andrey_on_vac: bool,
    artur_id: int,
    andrey_id: int,
    with_back: bool = True,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"👨‍💻 Артур — {'☀️ отпуск' if artur_on_vac else '🟢 работает'}",
        callback_data=f"b:toggle_vac:{artur_id}",
    )
    kb.button(
        text=f"🧑‍💻 Андрей К. — {'☀️ отпуск' if andrey_on_vac else '🟢 работает'}",
        callback_data=f"b:toggle_vac:{andrey_id}",
    )
    if with_back:
        kb.button(text="🔙 Назад", callback_data="b:back")
        kb.adjust(1, 1, 1)
    else:
        kb.adjust(1, 1)
    return kb.as_markup()


__all__ = [
    # user
    "user_main_menu",
    "USER_CATEGORIES",
    "STATUS_EMOJI",
    "categories_kb",
    "done_cancel_kb",
    "cancel_only_kb",
    # profile
    "profile_menu_kb",
    "reg_confirm_kb",
    # admin
    "admin_menu",
    "admin_accept_kb",
    "admin_done_kb",
    "rating_kb",
    "report_finish_kb",
    "admin_task_actions_kb",
    "admin_task_claimed_kb",
    "admin_back_kb",
    # boss
    "boss_menu",
    "pick_admin_kb",
    "pick_priority_kb",
    "vacation_kb",
]
