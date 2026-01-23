from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, TextIO


def format_uptime(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, sec = divmod(rem, 60)
    return f"{days:02d} {hours:02d}:{minutes:02d}:{sec:02d}"


def format_dt(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class UptimePrinter:
    """
    Печатает "живую" строку в консоль:
      Started: <time> | Uptime: dd hh:mm:ss
    Обновление раз в секунду, пока не остановят.
    """
    started_at: datetime
    prefix: str = ""
    stream: TextIO = sys.stdout

    _task: Optional[asyncio.Task] = None
    _last_line_len: int = 0
    _stopping: bool = False

    def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="uptime_printer")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is None:
            # на всякий случай переведём курсор на новую строку
            self.stream.write("\n")
            self.stream.flush()
            return

        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self.stream.write("\n")
            self.stream.flush()

    async def _run(self) -> None:
        while not self._stopping:
            now = datetime.now().astimezone()
            uptime_sec = int((now - self.started_at.astimezone()).total_seconds())

            line = (
                f"{self.prefix}"
                f"Started: {format_dt(self.started_at)} | "
                f"Uptime: {format_uptime(uptime_sec)}"
            )

            # затираем хвост, если новая строка короче предыдущей
            pad = ""
            if len(line) < self._last_line_len:
                pad = " " * (self._last_line_len - len(line))
            self._last_line_len = len(line)

            self.stream.write("\r" + line + pad)
            self.stream.flush()
            await asyncio.sleep(1)
