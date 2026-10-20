"""On-demand CF access, independent from the local application's lifecycle."""

from pathlib import Path

from app.models.assets import AssetSnapshot


class CFTunnelService:
    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)

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
