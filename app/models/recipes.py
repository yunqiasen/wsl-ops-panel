from pydantic import BaseModel, ConfigDict, Field


class DockerRecipeHealthcheck(BaseModel):
    model_config = ConfigDict(extra='forbid')

    url: str
    expect_status: int = 200


class DockerRecipe(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str
    lifecycle_strategy: str
    version_source: str
    repo_dir: str
    compose_file: str
    compose_service: str
    primary_container: str
    override_file: str
    git_remote: str = 'origin'
    tag_pattern: str = r'^v\d+\.\d+\.\d+$'
    managed_services: list[str] = Field(default_factory=list)
    ignored_services: list[str] = Field(default_factory=list)
    local_image_repository: str | None = None
    local_image_tag_template: str | None = None
    healthcheck: DockerRecipeHealthcheck | None = None
    full_delete_paths: list[str] = Field(default_factory=list)
