from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

TaskStatus = Literal['queued', 'running', 'succeeded', 'failed', 'interrupted']


class TaskRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str
    object_id: str
    action: str
    requested_version: str | None = None
    status: TaskStatus
    stdout_log_path: str
    stderr_log_path: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
