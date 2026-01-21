from aiogram.fsm.state import StatesGroup, State


class AuthState(StatesGroup):
    # ожидание ввода пароля
    waiting_passphrase = State()


class Registration(StatesGroup):
    # пошаговая регистрация профиля
    ask_full_name = State()
    ask_sip = State()
    confirm = State()


class TicketState(StatesGroup):
    choosing_category = State()
    collecting = State()
    admin_add_description = State()
    admin_add_confirm = State()
    boss_new_pick_admin = State()
    boss_new_pick_priority = State()
    boss_new_collect = State()


# состояние для режима "отправляю отчёт пользователю"
class AdminReport(StatesGroup):
    collecting = State()

class AdminTelegraphReport(StatesGroup):
    picking_day = State()
    picking_week = State()
    picking_month = State()