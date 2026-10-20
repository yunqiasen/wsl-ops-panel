"""The serialized CF operation is data, never an executable shell fragment."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.models.assets import AssetSnapshot


class TargetChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    revision: str


class CFOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    owner: str
    action: Literal["cf_create", "cf_refresh", "cf_disable"]
    asset: AssetSnapshot
    target: TargetChoice | None = None


class CFError(ValueError):
    def __init__(self, stage: str, message: str, *, handle: dict | None = None):
        super().__init__(message)
        self.stage = stage
        self.handle = handle

    def as_dict(self) -> dict[str, str]:
        return {"stage": self.stage, "message": str(self)}
