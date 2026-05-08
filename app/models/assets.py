from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DockerContainerSnapshot(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str
    name: str
    image: str
    image_tag: str | None = None
    status: str
    state: str | None = None
    ports: str | None = None
    compose_project: str | None = None
    compose_service: str | None = None
    compose_working_dir: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class AssetSnapshot(BaseModel):
    model_config = ConfigDict(extra='forbid')

    object_id: str
    category: str
    name: str
    status: str
    current_version: str | None = None
    latest_version: str | None = None
    supports_actions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    containers: list[DockerContainerSnapshot] = Field(default_factory=list)
    primary_container_name: str | None = None
    actionable: bool = False
    blocked_reason: str | None = None
    managed_by: str | None = None
    policy_source: str | None = None


class RuntimeVersionInfo(BaseModel):
    model_config = ConfigDict(extra='forbid')

    image: str | None = None
    image_tag: str | None = None
    oci_version: str | None = None
    oci_revision: str | None = None
    ports: str | None = None


class PackageVersionInfo(BaseModel):
    model_config = ConfigDict(extra='forbid')

    current_version: str | None = None
    latest_version: str | None = None
    versions: list[str] = Field(default_factory=list)
    source_status: str = 'ok'
    error: str | None = None
    lifecycle_strategy: str | None = None
    version_source: str | None = None
    runtime: RuntimeVersionInfo | None = None
    managed_services: list[str] = Field(default_factory=list)
    ignored_services: list[str] = Field(default_factory=list)
