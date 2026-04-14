import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TaskList(Base):
    __tablename__ = "task_lists"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ms_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    display_name: Mapped[str] = mapped_column(String(500), nullable=False)
    is_owner: Mapped[bool] = mapped_column(Boolean, default=True)
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False)
    wellknown_list_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ms_last_modified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_status: Mapped[str] = mapped_column(String(20), default="synced")

    tasks: Mapped[list["Task"]] = relationship("Task", back_populates="task_list", lazy="selectin")

    __table_args__ = (
        Index("ix_task_lists_ms_id", "ms_id"),
        Index("ix_task_lists_sync_status", "sync_status"),
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ms_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    list_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("task_lists.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_content_type: Mapped[str] = mapped_column(String(10), default="text")
    importance: Mapped[str] = mapped_column(String(10), default="normal")
    status: Mapped[str] = mapped_column(String(20), default="notStarted")
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_timezone: Mapped[str] = mapped_column(String(50), default="UTC")
    # F1.2: full datetime with timezone support
    due_datetime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    start_datetime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    start_timezone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reminder_datetime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_reminder_on: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_datetime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_timezone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    recurrence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    categories: Mapped[list] = mapped_column(JSONB, default=list)
    checklist_items: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]", default=list)
    ms_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ms_last_modified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_status: Mapped[str] = mapped_column(String(20), default="synced")

    task_list: Mapped["TaskList"] = relationship("TaskList", back_populates="tasks")

    __table_args__ = (
        Index("ix_tasks_ms_id", "ms_id"),
        Index("ix_tasks_list_id", "list_id"),
        Index("ix_tasks_status", "status"),
        Index("ix_tasks_due_date", "due_date"),
        Index("ix_tasks_sync_status", "sync_status"),
        Index("ix_tasks_reminder", "is_reminder_on", "reminder_datetime"),
    )


class SyncState(Base):
    __tablename__ = "sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_type: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    delta_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_status: Mapped[str] = mapped_column(String(20), default="success")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuthToken(Base):
    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_cache: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SyncLog(Base):
    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sync_type: Mapped[str] = mapped_column(String(20), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    items_pulled: Mapped[int] = mapped_column(Integer, default=0)
    items_pushed: Mapped[int] = mapped_column(Integer, default=0)
    items_deleted: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
