from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CategoryDefinition(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str
    label: str
    order: int = Field(default=100)
    description: str = ''
    enabled: bool = True


class ObjectDefinition(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str
    category: str
    type: str
    name: str
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class RegistrySnapshot(BaseModel):
    categories: list[CategoryDefinition]
    objects: list[ObjectDefinition]
