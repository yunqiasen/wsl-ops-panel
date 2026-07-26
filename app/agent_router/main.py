from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from app.agent_router.app import create_agent_router_app
from app.services.agent_router_config import AgentRouterConfigStore


def main() -> None:
    config_path = Path(os.environ.get("AGENT_ROUTER_CONFIG", "data/agent/router.json"))
    store = AgentRouterConfigStore(config_path.parent)
    config = store.snapshot()
    app = create_agent_router_app(store=store)
    uvicorn.run(
        app,
        host=str(config.get("listen_address", "127.0.0.1")),
        port=int(config.get("listen_port", 7888)),
        log_level=os.environ.get("AGENT_ROUTER_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
