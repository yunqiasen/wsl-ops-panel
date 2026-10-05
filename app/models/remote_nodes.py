from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class RemoteNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    host: str
    port: int = 22
    username: str
    auth_type: str = "password"
    password: str | None = None
    key_path: str | None = None
    os_hint: str = "auto"
    tags: list[str] = Field(default_factory=list)
    status: str = "unknown"
    last_checked_at: str | None = None
    last_error: str | None = None

    @field_validator("auth_type")
    @classmethod
    def validate_auth_type(cls, value: str) -> str:
        if value not in {"password", "key"}:
            raise ValueError("auth_type must be password or key")
        return value


class RemoteNodeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    host: str
    port: int = 22
    username: str
    auth_type: str = "password"
    password: str | None = None
    key_path: str | None = None
    os_hint: str = "auto"
    tags: list[str] = Field(default_factory=list)

    @field_validator("name", "host", "username", mode="before")
    @classmethod
    def strip_required_text(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if not _HOST_RE.fullmatch(value):
            raise ValueError("host contains unsupported characters")
        return value

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        if not _USERNAME_RE.fullmatch(value):
            raise ValueError("username contains unsupported characters")
        return value

    @field_validator("port")
    @classmethod
    def validate_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("port must be between 1 and 65535")
        return value

    @model_validator(mode="after")
    def validate_credentials(self) -> "RemoteNodeCreate":
        if self.auth_type == "password" and not (self.password or "").strip():
            raise ValueError("password is required for password auth")
        if self.auth_type == "key" and not (self.key_path or "").strip():
            raise ValueError("key_path is required for key auth")
        return self


class RemoteNodePublic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    host: str
    port: int
    username: str
    auth_type: str
    os_hint: str
    tags: list[str]
    status: str
    last_checked_at: str | None = None
    last_error: str | None = None
    has_password: bool = False
    has_key: bool = False

    @classmethod
    def from_node(cls, node: RemoteNode) -> "RemoteNodePublic":
        return cls(
            id=node.id,
            name=node.name,
            host=node.host,
            port=node.port,
            username=node.username,
            auth_type=node.auth_type,
            os_hint=node.os_hint,
            tags=list(node.tags),
            status=node.status,
            last_checked_at=node.last_checked_at,
            last_error=node.last_error,
            has_password=bool(node.password),
            has_key=bool(node.key_path),
        )
