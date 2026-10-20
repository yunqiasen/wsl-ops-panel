"""On-demand CF access, independent from the local application's lifecycle."""

from pathlib import Path

import httpx

from app.models.assets import AssetSnapshot
from app.services.cf_tunnels.origins import prepare_targets


class CFTunnelService:
    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)
        self.http_client = httpx.Client(trust_env=False)

    def status(self, asset: AssetSnapshot) -> dict[str, object]:
        return {
            "object_id": asset.object_id,
            "capable": asset.category == "docker" and bool(asset.containers),
            "management": "none",
            "desired": "off",
            "state": "off",
            "current_url": None,
            "last_verified_at": None,
            "last_task_id": None,
            "error": None,
        }

    def prepare(self, asset: AssetSnapshot) -> dict[str, object]:
        return prepare_targets(asset, self.http_client)
