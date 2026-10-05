"""App fixtures with an explicit auth configuration for custom registry roots."""
from pathlib import Path
import shutil

from app.main import create_app as _create_app


def create_app(**kwargs):
    root = Path(kwargs.get('config_root', 'config'))
    panel = root / 'panel.yaml'
    if not panel.exists():
        root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path('config/panel.yaml'), panel)
    return _create_app(**kwargs)
