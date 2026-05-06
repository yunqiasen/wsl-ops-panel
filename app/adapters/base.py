from pydantic import BaseModel, ConfigDict, Field


class ActionPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')

    commands: list[list[str]]
    requires_sudo: bool = False
    working_dir: str | None = None
    preview_paths: list[str] = Field(default_factory=list)
    preview_objects: list[str] = Field(default_factory=list)
