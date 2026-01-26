# app/services/telegraph_report.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, List, Dict, Any
from collections import defaultdict
from datetime import datetime, date, timezone
import json
from io import BytesIO
import logging
import mimetypes
import os

import aiohttp
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from html import escape

from app.models import Task, Attachment


logger = logging.getLogger(__name__)

TELEGRAPH_API_URL = "https://api.telegra.ph"
TELEGRAPH_UPLOAD_URL = "https://telegra.ph/upload"


@dataclass
class TelegraphConfig:
    access_token: str
    author_name: str = "HardyBot"
    author_url: Optional[str] = None


class TelegraphClient:
    def __init__(self, config: TelegraphConfig) -> None:
        self.config = config

    # ============ низкоуровневые запросы ============

    async def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Telegraph API иногда отвечает не строго application/json.
        Поэтому читаем text и пробуем json.loads.
        """
        url = f"{TELEGRAPH_API_URL}/{method}"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=params) as resp:
                raw = await resp.text()

        try:
            data = json.loads(raw)
        except Exception:
            raise RuntimeError(f"Telegraph error: cannot decode JSON. raw={raw[:200]!r}")

        if not data.get("ok"):
            raise RuntimeError(f"Telegraph error: {data.get('error')}")
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Telegraph error: invalid result payload: {result!r}")
        return result

    async def _upload_telegram_file(self, bot: Bot, file_id: str) -> Optional[str]:
        """
        Скачиваем файл из Telegram и загружаем в Telegraph.
        Возвращаем полный URL (https://telegra.ph/…).
        """
        try:
            # 1) тянем файл из Telegram
            tg_file = await bot.get_file(file_id)

            buf = BytesIO()

            # aiogram 3.x: чаще всего bot.download(file, destination=...)
            try:
                await bot.download(tg_file, destination=buf)
            except TypeError:
                file_path = getattr(tg_file, "file_path", None)
                if not file_path:
                    raise RuntimeError("Telegram returned empty file_path for this file_id")
                await bot.download_file(file_path, destination=buf)

            content = buf.getvalue()

            # filename (если есть file_path — возьмём расширение)
            file_path = getattr(tg_file, "file_path", None) or ""
            filename = os.path.basename(file_path) or "file"
            ext = os.path.splitext(filename)[-1].lower()
            content_type = mimetypes.types_map.get(ext, "application/octet-stream")

            # 2) шлём в Telegraph
            form = aiohttp.FormData()
            form.add_field(
                "file",
                content,
                filename=filename,
                content_type=content_type,
            )

            async with aiohttp.ClientSession() as session:
                async with session.post(TELEGRAPH_UPLOAD_URL, data=form) as resp:
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        logger.error("Telegraph upload: JSON decode error, response text=%r", text)
                        return None

            # формат ответа Telegraph: [{"src": "/file/xxxx.jpg"}] либо {"error": "..."}
            if isinstance(data, list) and data and isinstance(data[0], dict) and "src" in data[0]:
                src = data[0]["src"]
                if isinstance(src, str):
                    if src.startswith("http"):
                        return src
                    return "https://telegra.ph" + src

            logger.error("Telegraph upload: unexpected response %r", data)
            return None

        except Exception as e:
            logger.exception("Telegraph upload failed for file_id %s: %s", file_id, e)
            return None

    # ============ публичный метод создания страницы ============

    async def create_tasks_page(
        self,
        title: str,
        tasks: Sequence[Task],
        *,
        bot: Bot,
        session: AsyncSession,
    ) -> str:
        """
        Создаёт страницу Telegraph со списком задач для админа.
        """

        content: List[Dict[str, Any]] = []

        # для итогов
        durations_sec: List[float] = []
        complexities: List[int] = []

        # кеш username по tg_id, чтобы не долбить Telegram лишний раз
        username_cache: Dict[int, Optional[str]] = {}

        # эмодзи по категориям (дополняй по необходимости)
        CATEGORY_EMOJI: Dict[str, str] = {
            "Интернет": "🌐",
            "Мобильная связь": "📶",
            "1С": "🧾",
            "1C": "🧾",
            "Удаленка": "🏠",
            "Удалёнка": "🏠",
            "Принтер": "🖨",
            "Компьютер": "💻",
            "Пропуск": "🎫",
            "Доступ в дверь": "🚪",
            "ЭЦП": "🔏",
            "Другое": "➕",
            "Доступы/Права": "🔑",
            "Wi-Fi": "📶",
            "Вирус/Безопасность": "🦠",
            "Монитор": "🖥",
        }

        def _to_local(dt: datetime) -> datetime:
            """Convert naive-UTC datetime from DB into local timezone-aware datetime."""
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone()


        # ===== группировка по дате создания =====
        grouped: Dict[Optional[date], List[Task]] = defaultdict(list)
        for t in tasks:
            created = getattr(t, "created_at", None)
            if isinstance(created, datetime):
                d: Optional[date] = _to_local(created).date()
            elif isinstance(created, date):
                d = created
            else:
                d = None
            grouped[d].append(t)

        def _sort_key(item: tuple[Optional[date], List[Task]]) -> tuple[int, date]:
            d, _ = item
            if d is None:
                return (1, date(9999, 12, 31))
            return (0, d)

        for group_date, day_tasks in sorted(grouped.items(), key=_sort_key):
            # заголовок дня
            if group_date is not None:
                day_title = group_date.strftime("📅 %d.%m.%Y")
            else:
                day_title = "📅 Без даты создания"
            content.append({"tag": "h2", "children": [escape(day_title)]})

            # заявки за день
            for task in day_tasks:
                created_at = getattr(task, "created_at", None)
                closed_at = getattr(task, "closed_at", None) or getattr(task, "updated_at", None)

                created_local: Optional[datetime] = _to_local(created_at) if isinstance(created_at, datetime) else None
                closed_local: Optional[datetime] = _to_local(closed_at) if isinstance(closed_at, datetime) else None

                created_str = created_local.strftime("%d.%m.%Y %H:%M") if created_local else "—"
                closed_str = closed_local.strftime("%d.%m.%Y %H:%M") if closed_local else "—"

                # длительность
                duration_str = "—"
                if created_local and closed_local:
                    delta = closed_local - created_local
                    sec = max(delta.total_seconds(), 0)
                    durations_sec.append(sec)
                    minutes = int(sec // 60)
                    hours = minutes // 60
                    minutes = minutes % 60
                    if hours:
                        duration_str = f"{hours} ч {minutes} мин"
                    else:
                        duration_str = f"{minutes} мин"

                # сложность
                complexity_val = getattr(task, "final_complexity", None)
                if complexity_val is not None:
                    try:
                        c_int = int(complexity_val)
                    except Exception:
                        c_int = None
                    if c_int is not None:
                        complexities.append(c_int)
                        complexity_str = f"{c_int}/10"
                    else:
                        complexity_str = "—"
                else:
                    complexity_str = "—"

                # автор: snapshot ФИО + username через @
                author_name = getattr(task, "author_full_name", None) or "—"
                author_tg_id = getattr(task, "author_tg_id", None)
                author_username: Optional[str] = None
                if isinstance(author_tg_id, int):
                    if author_tg_id in username_cache:
                        author_username = username_cache[author_tg_id]
                    else:
                        try:
                            chat = await bot.get_chat(author_tg_id)
                            author_username = getattr(chat, "username", None)
                        except Exception:
                            author_username = None
                        username_cache[author_tg_id] = author_username

                username_part = f" (@{author_username})" if author_username else ""

                category = getattr(task, "category", None) or "Без категории"
                status = getattr(task, "status", None) or "—"

                # заголовок заявки
                cat_emoji = CATEGORY_EMOJI.get(category, "")
                if cat_emoji:
                    header_text = f"🧾 Заявка №{task.id} — {category} {cat_emoji}"
                else:
                    header_text = f"🧾 Заявка №{task.id} — {category}"

                content.append({"tag": "h3", "children": [escape(header_text)]})

                # основные поля
                details_items: List[str] = []
                details_items.append(f"👤 Автор: {author_name}{username_part}")

                # статус не показываем, если CLOSED
                if status and str(status).upper() != "CLOSED":
                    details_items.append(f"🏷 Статус: {status}")

                details_items.append(f"🕒 Создано: {created_str}")
                details_items.append(f"✅ Завершено: {closed_str}")
                details_items.append(f"⏱ Время выполнения: {duration_str}")
                details_items.append(f"⭐ Сложность: {complexity_str}")

                content.append(
                    {
                        "tag": "ul",
                        "children": [{"tag": "li", "children": [escape(item)]} for item in details_items],
                    }
                )

                # описание
                if getattr(task, "description", None):
                    content.append(
                        {
                            "tag": "p",
                            "children": [escape(f"📝 Описание:\n{task.description}")],
                        }
                    )

                # вложения
                ares = await session.execute(select(Attachment).where(Attachment.task_id == task.id))
                attachments: List[Attachment] = list(ares.scalars().all())

                for att in attachments:
                    url = await self._upload_telegram_file(bot, att.file_id)

                    if not url:
                        # хотя бы показать, что вложение было, и не молча проглатывать
                        content.append(
                            {
                                "tag": "p",
                                "children": [escape(f"⚠️ Не удалось загрузить вложение ({att.file_type}).")],
                            }
                        )
                        continue

                    if att.file_type == "photo":
                        node: Dict[str, Any] = {
                            "tag": "figure",
                            "children": [{"tag": "img", "attrs": {"src": url}}],
                        }
                        if att.caption:
                            node["children"].append(
                                {"tag": "figcaption", "children": [escape(att.caption)]}
                            )
                        content.append(node)

                    elif att.file_type == "video":
                        link_text = att.caption or "🎬 Видео"
                        content.append(
                            {
                                "tag": "p",
                                "children": [
                                    {
                                        "tag": "a",
                                        "attrs": {"href": url},
                                        "children": [escape(link_text)],
                                    }
                                ],
                            }
                        )

                    elif att.file_type == "voice":
                        link_text = att.caption or "🎙 Голосовое сообщение"
                        content.append(
                            {
                                "tag": "p",
                                "children": [
                                    {
                                        "tag": "a",
                                        "attrs": {"href": url},
                                        "children": [escape(link_text)],
                                    }
                                ],
                            }
                        )

                    elif att.file_type == "document":
                        link_text = att.caption or "📎 Документ"
                        content.append(
                            {
                                "tag": "p",
                                "children": [
                                    {
                                        "tag": "a",
                                        "attrs": {"href": url},
                                        "children": [escape(link_text)],
                                    }
                                ],
                            }
                        )

                # разделитель между заявками
                content.append({"tag": "hr"})

        # ===== Итоги периода =====
        total_tasks = len(tasks)
        if total_tasks:
            content.append({"tag": "h3", "children": [escape("📊 Итоги периода")]})

            summary_lines: List[str] = [f"📌 Всего закрытых заявок: {total_tasks}"]

            if durations_sec:
                avg_sec = sum(durations_sec) / len(durations_sec)
                avg_minutes = int(avg_sec // 60)
                avg_hours = avg_minutes // 60
                avg_minutes = avg_minutes % 60
                if avg_hours:
                    avg_duration_str = f"{avg_hours} ч {avg_minutes} мин"
                else:
                    avg_duration_str = f"{avg_minutes} мин"
                summary_lines.append(f"⏱ Среднее время выполнения: {avg_duration_str}")

            if complexities:
                avg_complexity = sum(complexities) / len(complexities)
                summary_lines.append(f"⭐ Средняя сложность задач: {avg_complexity:.1f}/10")

            content.append(
                {
                    "tag": "ul",
                    "children": [{"tag": "li", "children": [escape(line)]} for line in summary_lines],
                }
            )

        # ===== создание страницы =====
        params: Dict[str, Any] = {
            "access_token": self.config.access_token,
            "title": title,
            "author_name": self.config.author_name,
            "content": json.dumps(content, ensure_ascii=False),
            "return_content": "false",
        }
        if self.config.author_url:
            params["author_url"] = self.config.author_url

        result = await self._request("createPage", params)
        url = result.get("url")
        if not url:
            path = result.get("path", "")
            url = f"https://telegra.ph/{path}"
        return str(url)
