"""Every navigation module renders from a private app without live scanners."""

from pathlib import Path
import shutil

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from app.main import create_app
from app.tasks.store import InMemoryTaskStore
from tests.test_runtime_reliability import config_at


def test_all_eleven_modules_render_with_accurate_control_center_scope(tmp_path):
    config = config_at(tmp_path / "config")
    shutil.copytree(
        Path("config/categories"), config / "categories", dirs_exist_ok=True
    )
    store = InMemoryTaskStore()
    app = create_app(
        config_root=config,
        task_store=store,
        **{
            key: lambda: []
            for key in (
                "docker_scanner",
                "systemd_scanner",
                "node_scanner",
                "python_scanner",
                "host_process_scanner",
                "system_infra_scanner",
                "project_scanner",
            )
        },
    )
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token(config_root=config))
    paths = [
        "/categories/" + name
        for name in (
            "docker",
            "project",
            "node",
            "python",
            "agent",
            "remote",
            "host",
            "system",
        )
    ]
    paths += ["/tasks", "/terminals", "/settings"]
    pages = {}
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "/login" not in str(response.url), path
        pages[path] = response.text
    assert "不重启面板或加载代码" in pages["/settings"]
    assert "手动终端命令不计入任务队列" in pages["/tasks"]
    assert "草稿不会自动应用" in pages["/categories/remote"]
    assert '<option value="apt_sources_d"' not in pages["/categories/remote"]
    assert '<option value="powershell_profile"' not in pages["/categories/remote"]
    assert '<option value="ssh_config"' in pages["/categories/remote"]
    assert store.list_all() == []
