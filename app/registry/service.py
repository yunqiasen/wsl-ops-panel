from pathlib import Path

from app.models.registry import RegistrySnapshot
from app.registry.loader import load_registry


class RegistryService:
    def __init__(self, config_root: Path) -> None:
        self._config_root = config_root
        self._snapshot = load_registry(config_root)

    @property
    def snapshot(self) -> RegistrySnapshot:
        return self._snapshot.model_copy(deep=True)

    def reload(self) -> RegistrySnapshot:
        self._snapshot = load_registry(self._config_root)
        return self._snapshot.model_copy(deep=True)
