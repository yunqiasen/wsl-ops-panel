from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator


class CategoryDefinition(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str
    label: str
    order: int = Field(default=100)
    description: str = ''
    enabled: bool = True


class DockerComposeConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    project_dir: StrictStr
    compose_file: StrictStr
    primary_container: StrictStr | None = None
    compose_service: StrictStr | None = None


class SystemdUnitConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    unit_name: StrictStr
    working_dir: StrictStr


_OBJECT_CONFIG_MODELS: dict[str, type[BaseModel]] = {
    'docker_compose': DockerComposeConfig,
    'systemd_unit': SystemdUnitConfig,
}


class ObjectDefinition(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str
    category: str
    type: str
    name: str
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_known_type_config(self) -> 'ObjectDefinition':
        config_model = _OBJECT_CONFIG_MODELS.get(self.type)
        if config_model is None:
            return self

        validated_config = config_model.model_validate(self.config)
        self.config = validated_config.model_dump(exclude_none=True)
        return self


class RegistrySnapshot(BaseModel):
    categories: list[CategoryDefinition]
    objects: list[ObjectDefinition]
