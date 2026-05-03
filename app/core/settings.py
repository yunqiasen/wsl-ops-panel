from pathlib import Path

import yaml
from pydantic import BaseModel


class PanelAuthConfig(BaseModel):
    username: str
    password: str


class PanelConfig(BaseModel):
    title_cn: str
    title_en: str
    host: str
    port: int
    auth: PanelAuthConfig


def load_panel_config() -> PanelConfig:
    raw = yaml.safe_load(Path('config/panel.yaml').read_text())
    return PanelConfig.model_validate(raw)
