import os
import re

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(), override=True)
except Exception:
    pass


def _int(name: str, default: int = 0) -> int:
    val = os.getenv(name, "")
    try:
        return int(str(val).strip())
    except Exception:
        return default


def _clean_token(s: str) -> str:
    if not s:
        return ""
    s = str(s).strip().replace('"', "").replace("'", "")
    # чистим невидимые символы, которые иногда попадают из буфера обмена
    s = s.replace("\u200e", "").replace("\u200f", "").replace("\xa0", " ")
    return s.strip()


class Settings:
    # окружение
    ENV: str = os.getenv("ENV", "dev")  # dev/stage/prod

    # базовые настройки
    BOT_TOKEN: str = _clean_token(os.getenv("BOT_TOKEN", ""))
    DB_URL: str = os.getenv("DB_URL", "sqlite+aiosqlite:///./bot.db")

    # ⚠️ для обратной совместимости оставляем, но в коде предпочтительно используем PASS_PHRASE_HASH
    PASS_PHRASE: str | None = os.getenv("PASS_PHRASE") or None
    # ✅ новый параметр — bcrypt-хэш пароля
    PASS_PHRASE_HASH: str = os.getenv("PASS_PHRASE_HASH", "").strip()

    # Telegraph
    TELEGRAPH_TOKEN: str = os.getenv("TELEGRAPH_TOKEN", "").strip()
    TELEGRAPH_AUTHOR_NAME: str = os.getenv("TELEGRAPH_AUTHOR_NAME", "HardyBot").strip()
    TELEGRAPH_AUTHOR_URL: str = os.getenv("TELEGRAPH_AUTHOR_URL", "").strip()

    # анти-брутфорс
    AUTH_MAX_FAILS: int = _int("AUTH_MAX_FAILS", 5)
    AUTH_BAN_MINUTES: int = _int("AUTH_BAN_MINUTES", 15)

    # персонал
    ADMIN_1: int = _int("ADMIN_1")
    ADMIN_2: int = _int("ADMIN_2")
    BOSS: int = _int("BOSS")

    @property
    def admin_ids(self) -> tuple[int, ...]:
        """Два ID админов как кортеж (строго без босса)."""
        out: list[int] = []
        for v in (self.ADMIN_2, self.ADMIN_1):
            if v:
                try:
                    out.append(int(v))
                except Exception:
                    pass
        return tuple(out)

    @property
    def boss_id(self) -> int | None:
        return int(self.BOSS) if self.BOSS else None

    @property
    def staff_ids(self) -> set[int]:
        """Весь персонал: оба админа + босс."""
        result: set[int] = set()
        for v in (self.ADMIN_1, self.ADMIN_2, self.BOSS):
            if v:
                try:
                    result.add(int(v))
                except Exception:
                    pass
        return result


settings = Settings()

# необязательный самотест токена (просто предупреждение в консоль)
_token_ok = bool(re.match(r"^\d{5,}:[A-Za-z0-9_-]{30,}$", settings.BOT_TOKEN))
if not _token_ok:
    print("[WARN] BOT_TOKEN looks invalid.")
