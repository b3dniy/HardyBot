from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Dict

AttachmentTuple = Tuple[str, str, Optional[str], Optional[str]]

@dataclass
class DraftData:
    category: Optional[str] = None
    description: str = ""
    attachments: List[AttachmentTuple] = field(default_factory=list)

    root_message_id: Optional[int] = None
    hint_message_id: Optional[int] = None

# user_id -> DraftData
DRAFTS: Dict[int, DraftData] = {}

# ------------------------------------------------------------
# Учёт сообщений бота для "чистки чата" (удаляем только свои)
# ------------------------------------------------------------

_BOT_MESSAGES: Dict[int, List[int]] = {}

def register_bot_message(user_id: int, message_id: int) -> None:
    """
    Регистрируем message_id отправленного ботом сообщения, чтобы потом удалить.
    """
    if not message_id:
        return
    lst = _BOT_MESSAGES.setdefault(user_id, [])
    if message_id not in lst:
        lst.append(message_id)

def drain_bot_messages(user_id: int) -> List[int]:
    """
    Забираем и очищаем список бот-сообщений пользователя.
    """
    ids = _BOT_MESSAGES.pop(user_id, [])
    return list(dict.fromkeys(ids))
