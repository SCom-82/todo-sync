import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field


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


# --- Tasks ---

class ChecklistItem(BaseModel):
    """Один пункт чек-листа (подзадача) Microsoft To Do."""
    id: str | None = None          # ms_id из Graph; None для новых, созданных через API
    displayName: str = Field(..., max_length=1000)
    isChecked: bool = False


class TaskCreate(BaseModel):
    list_id: uuid.UUID
    title: str = Field(..., max_length=1000)
    body: str | None = None
    importance: str = "normal"
    due_date: date | None = None
    reminder_datetime: datetime | None = None
    is_reminder_on: bool = False
    categories: list[str] = Field(default_factory=list)
    checklist_items: list[ChecklistItem] = Field(default_factory=list)


class TaskUpdate(BaseModel):
    title: str | None = Field(None, max_length=1000)
    body: str | None = None
    importance: str | None = None
    status: str | None = None
    due_date: date | None = None
    reminder_datetime: datetime | None = None
    is_reminder_on: bool | None = None
    categories: list[str] | None = None
    checklist_items: list[ChecklistItem] | None = None


class TaskResponse(BaseModel):
    id: uuid.UUID
    ms_id: str | None = None
    list_id: uuid.UUID
    title: str
    body: str | None = None
    importance: str
    status: str
    due_date: date | None = None
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
