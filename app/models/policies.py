from pydantic import BaseModel, ConfigDict, Field


class PackageRule(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str
    managed_by: str
    protected: bool = False
    allowed_actions: list[str] = Field(default_factory=list)
    full_delete_paths: list[str] = Field(default_factory=list)
    blocked_reason: str | None = None


class PackageRuleSet(BaseModel):
    model_config = ConfigDict(extra='forbid')

    packages: list[PackageRule] = Field(default_factory=list)
