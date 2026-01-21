from __future__ import annotations

from typing import Callable, Dict, Any, Awaitable

from aiogram import BaseMiddleware


class DBSessionMiddleware(BaseMiddleware):
    """
    Оборачивает апдейты в контекст асинхронной сессии БД.
    В data прокидывает ключ 'session'.
    """

    def __init__(self, session_factory: Callable):
        super().__init__()
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        async with self.session_factory() as session:
            data["session"] = session
            return await handler(event, data)
