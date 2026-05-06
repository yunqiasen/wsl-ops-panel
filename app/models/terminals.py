from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

DebugTerminalStatus = Literal['running', 'closed']


class DebugTerminalSession(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str
    shell: str
    cwd: str
    status: DebugTerminalStatus
    created_at: datetime
    closed_at: datetime | None = None
    exit_code: int | None = None
    pid: int | None = None
    output_log_path: str
    input_log_path: str
    metadata_path: str


class DebugTerminalCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    shell: str | None = None
    cwd: str | None = None
