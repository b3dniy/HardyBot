# app/models.py
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, String, Integer, DateTime, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship, foreign

from app.db import Base
from app.enums import Role, Status, Priority


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default=Role.USER.value)
    is_authenticated: Mapped[bool] = mapped_column(Boolean, default=False)
    on_vacation: Mapped[bool] = mapped_column(Boolean, default=False)

    # НОВОЕ: профиль пользователя
    sip_ext: Mapped[Optional[str]] = mapped_column(String(3), nullable=True, index=True)  # доб. "505"
    profile_completed: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # задачи, назначенные этому админу (связываем по users.tg_id <-> tasks.assignee_tg_id)
    tasks_assigned: Mapped[list["Task"]] = relationship(
        back_populates="assignee",
        primaryjoin=lambda: User.tg_id == foreign(Task.assignee_tg_id),
        foreign_keys=lambda: Task.assignee_tg_id,
    )

    # задачи, созданные этим пользователем (автор)
    tasks_created: Mapped[list["Task"]] = relationship(
        back_populates="author",
        primaryjoin=lambda: User.tg_id == foreign(Task.author_tg_id),
        foreign_keys=lambda: Task.author_tg_id,
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    author_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)                 # кто создал заявку
    assignee_tg_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True, nullable=True)  # кому назначено
    category: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default=Status.NEW.value)
    priority: Mapped[str] = mapped_column(String(10), default=Priority.MEDIUM.value)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False)
    user_visible: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    expected_complexity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    final_complexity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # НОВОЕ: "снимок" автора на момент создания заявки (чтобы заявки не менялись при редактировании профиля)
    author_full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    author_sip: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)

    # связи (симметрично к User.*), помечаем «дочерние» колонки как foreign()
    assignee: Mapped[Optional["User"]] = relationship(
        back_populates="tasks_assigned",
        primaryjoin=lambda: foreign(Task.assignee_tg_id) == User.tg_id,
        foreign_keys=lambda: Task.assignee_tg_id,
    )
    author: Mapped[Optional["User"]] = relationship(
        back_populates="tasks_created",
        primaryjoin=lambda: foreign(Task.author_tg_id) == User.tg_id,
        foreign_keys=lambda: Task.author_tg_id,
    )

    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
    )


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    file_id: Mapped[str] = mapped_column(String(256))
    file_type: Mapped[str] = mapped_column(String(32))  # photo|video|voice|document
    caption: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    media_group_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    task: Mapped["Task"] = relationship(back_populates="attachments")
