from pydantic import BaseModel, ConfigDict, Field

from app.services.agent_provider_projection import ProviderProjection


class ActionPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')

    provider_projection: ProviderProjection | None = None
    commands: list[list[str]]
    success_commands: list[list[str]] = Field(default_factory=list)
    retry_policy: dict[str, object] | None = None
    command_timeout_seconds: float | None = None
    requires_sudo: bool = False
    working_dir: str | None = None
    preview_paths: list[str] = Field(default_factory=list)
    preview_objects: list[str] = Field(default_factory=list)
