from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any


class AgentProviderSecretStore:
    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)
        self.path = self.data_root / "provider-secrets.json"
        self._lock = RLock()

    def put(
        self, client_id: str, provider_id: str, secrets: dict[str, Any]
    ) -> str:
        ref = f"provider-secret:{client_id}:{provider_id}"
        with self._lock:
            payload = self._read()
            payload[ref] = secrets
            self._write(payload)
        return ref

    def resolve(self, ref: str | None) -> dict[str, Any]:
        if not ref:
            return {}
        with self._lock:
            value = self._read().get(ref)
        return dict(value) if isinstance(value, dict) else {}

    def delete(self, ref: str | None) -> bool:
        if not ref:
            return False
        with self._lock:
            payload = self._read()
            existed = ref in payload
            payload.pop(ref, None)
            if existed:
                self._write(payload)
        return existed

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, delete=False
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.chmod(0o600)
        os.replace(temporary, self.path)
        self.path.chmod(0o600)
