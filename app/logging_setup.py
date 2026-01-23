import logging
import os
import sys
from logging.handlers import RotatingFileHandler


def setup_logging(
    log_dir: str = "logs",
    log_file: str = "bot.log",
    level: str = "INFO",
    max_mb: int = 50,
    backup_count: int = 10,
    console: bool = False,  # <-- по умолчанию НЕ пишем логи в консоль
) -> str:
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, log_file)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Файл + ротация
    fh = RotatingFileHandler(
        log_path,
        maxBytes=max_mb * 1024 * 1024,
        backupCount=backup_count,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    fh.setLevel(root.level)

    root.handlers.clear()
    root.addHandler(fh)

    # Если когда-нибудь надо вернуть логи в консоль
    if console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        sh.setLevel(root.level)
        root.addHandler(sh)

    # Необработанные исключения - всё равно в файл
    def _excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        logging.getLogger("ERR").error("Uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _excepthook

    logging.getLogger("BOOT").info("Logging initialized -> %s", os.path.abspath(log_path))
    return log_path
