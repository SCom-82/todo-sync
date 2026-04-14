import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


# --- Task Lists ---

class TaskListCreate(BaseModel):
    display_name: str = Field(..., max_length=500)


class TaskListUpdate(BaseModel):
    display_name: str = Field(..., max_length=500)


class TaskListResponse(BaseModel):
    id: uuid.UUID
    ms_id: str | None = None
    display_name: str
    is_owner: bool
    is_shared: bool
    wellknown_list_name: str | None = None
    sync_status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Checklist ---

class ChecklistItem(BaseModel):
    """Один пункт чек-листа (подзадача) Microsoft To Do."""
    id: str | None = None          # ms_id из Graph; None для новых, созданных через API
    displayName: str = Field(..., max_length=1000)
    isChecked: bool = False


class ChecklistItemCreate(BaseModel):
    """Тело запроса для POST /tasks/{task_id}/checklist."""
    displayName: str = Field(..., max_length=1000)
    isChecked: bool = False


class ChecklistItemUpdate(BaseModel):
    """Тело запроса для PATCH /tasks/{task_id}/checklist/{item_id}."""
    displayName: str | None = Field(None, max_length=1000)
    isChecked: bool | None = None


class ChecklistItemResponse(BaseModel):
    """Ответ для одного пункта чек-листа."""
    id: str
    displayName: str
    isChecked: bool


# --- F1.4: Recurrence ---

class RecurrencePattern(BaseModel):
    """patternedRecurrence.pattern по схеме MS Graph."""
    type: Literal[
        "daily", "weekly",
        "absoluteMonthly", "relativeMonthly",
        "absoluteYearly", "relativeYearly",
    ]
    interval: int = Field(1, ge=1)
    # weekly
    daysOfWeek: list[str] | None = None
    firstDayOfWeek: str | None = None
    # relativeMonthly / relativeYearly
    index: str | None = None
    # absoluteMonthly / absoluteYearly
    dayOfMonth: int | None = None
    # absoluteYearly / relativeYearly
    month: int | None = None

    @model_validator(mode="after")
    def check_pattern_fields(self) -> "RecurrencePattern":
        if self.type == "weekly" and not self.daysOfWeek:
            raise ValueError("daysOfWeek is required for weekly recurrence")
        if self.type in ("absoluteMonthly", "absoluteYearly") and self.dayOfMonth is None:
            raise ValueError(f"dayOfMonth is required for {self.type} recurrence")
        return self


class RecurrenceRange(BaseModel):
    """patternedRecurrence.range по схеме MS Graph."""
    type: Literal["endDate", "noEnd", "numbered"]
    startDate: str  # YYYY-MM-DD
    endDate: str | None = None
    numberOfOccurrences: int | None = None

    @model_validator(mode="after")
    def check_range_fields(self) -> "RecurrenceRange":
        if self.type == "endDate" and not self.endDate:
            raise ValueError("endDate is required for endDate range type")
        if self.type == "numbered" and self.numberOfOccurrences is None:
            raise ValueError("numberOfOccurrences is required for numbered range type")
        return self


class PatternedRecurrence(BaseModel):
    """Объект повторения задачи (patternedRecurrence)."""
    pattern: RecurrencePattern
    range: RecurrenceRange


# --- Tasks ---

class TaskCreate(BaseModel):
    # F1.1: принимаем list_id ИЛИ list_name ИЛИ list_ms_id — ровно один
    list_id: uuid.UUID | None = None
    list_name: str | None = None
    list_ms_id: str | None = None

    title: str = Field(..., max_length=1000)
    body: str | None = None
    # F1.3: body content type
    body_content_type: Literal["text", "html"] = "text"
    importance: str = "normal"
    due_date: date | None = None
    # F1.2: полный datetime + timezone
    due_datetime: datetime | None = None
    due_timezone: str | None = None
    start_datetime: datetime | None = None
    start_timezone: str | None = None
    reminder_datetime: datetime | None = None
    is_reminder_on: bool = False
    categories: list[str] = Field(default_factory=list)
    checklist_items: list[ChecklistItem] = Field(default_factory=list)
    # F1.4: recurrence
    recurrence: PatternedRecurrence | None = None

    @model_validator(mode="after")
    def check_list_identifier(self) -> "TaskCreate":
        provided = sum([
            self.list_id is not None,
            self.list_name is not None,
            self.list_ms_id is not None,
        ])
        if provided != 1:
            raise ValueError(
                "Exactly one of list_id, list_name, or list_ms_id must be provided"
            )
        return self


class TaskUpdate(BaseModel):
    title: str | None = Field(None, max_length=1000)
    body: str | None = None
    # F1.3
    body_content_type: Literal["text", "html"] | None = None
    importance: str | None = None
    status: str | None = None
    due_date: date | None = None
    # F1.2
    due_datetime: datetime | None = None
    due_timezone: str | None = None
    start_datetime: datetime | None = None
    start_timezone: str | None = None
    reminder_datetime: datetime | None = None
    is_reminder_on: bool | None = None
    categories: list[str] | None = None
    checklist_items: list[ChecklistItem] | None = None
    # F1.4
    recurrence: PatternedRecurrence | None = None


class TaskResponse(BaseModel):
    id: uuid.UUID
    ms_id: str | None = None
    list_id: uuid.UUID
    title: str
    body: str | None = None
    # F1.3
    body_content_type: str = "text"
    importance: str
    status: str
    due_date: date | None = None
    # F1.2
    due_datetime: datetime | None = None
    due_timezone: str | None = None
    start_datetime: datetime | None = None
    start_timezone: str | None = None
    reminder_datetime: datetime | None = None
    is_reminder_on: bool
    completed_datetime: datetime | None = None
    recurrence: dict | None = None
    categories: list = Field(default_factory=list)
    checklist_items: list = Field(default_factory=list)
    sync_status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Stats ---

class StatsResponse(BaseModel):
    total: int
    not_started: int
    in_progress: int
    completed: int
    overdue: int
    due_today: int
    due_this_week: int
    by_list: list[dict]


# --- Auth ---

class DeviceCodeResponse(BaseModel):
    user_code: str
    verification_uri: str
    expires_in: int
    message: str


class AuthStatusResponse(BaseModel):
    authenticated: bool


# --- Sync ---

class SyncStatusResponse(BaseModel):
    last_sync_at: datetime | None
    last_sync_status: str | None
    resources: list[dict]


class SyncLogEntry(BaseModel):
    id: int
    sync_type: str
    resource_type: str
    items_pulled: int
    items_pushed: int
    items_deleted: int
    errors: int
    duration_ms: int | None
    created_at: datetime

    model_config = {"from_attributes": True}
