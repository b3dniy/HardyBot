from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Dict

# attachment: (file_type, file_id, caption, media_group_id)
AttachmentTuple = Tuple[str, str, Optional[str], Optional[str]]

@dataclass
class DraftData:
    category: Optional[str] = None
    description: str = ""
    attachments: List[AttachmentTuple] = field(default_factory=list)

    # id исходного сообщения с инструкцией (которое редактируем и меняем клавиатуру)
    root_message_id: Optional[int] = None
    # id последнего «Принято…» (временного) сообщения — удаляем при новом контенте/Done/Cancel
    hint_message_id: Optional[int] = None

# user_id -> DraftData
DRAFTS: Dict[int, DraftData] = {}

# ------------------------------------------------------------
# Учёт сообщений бота для "чистки чата" (удаляем только свои)
# ------------------------------------------------------------

# user_id -> list[message_id]
_BOT_MESSAGES: Dict[int, List[int]] = {}

def register_bot_message(user_id: int, message_id: int) -> None:
    """
    Регистрируем message_id отправленного ботом сообщения, чтобы потом удалить.
    """
    if not message_id:
        return
    _BOT_MESSAGES.setdefault(user_id, []).append(message_id)

def drain_bot_messages(user_id: int) -> List[int]:
    """
    Забираем и очищаем список бот-сообщений пользователя.
    """
    return _BOT_MESSAGES.pop(user_id, [])
