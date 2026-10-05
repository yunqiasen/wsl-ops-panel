"""Concrete review cases beyond the initial ABCD examples."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.agent_router.transforms import transform_sse
from app.services.agent_provider_adapters import read_provider_records
from app.services.agent_provider_projection import (
    ProviderProjection,
    provider_matches,
    provider_projection,
)
from app.services.agent_router_config import AgentRouterConfigStore
from app.services.agent_router_control import AgentRouterController
from tests.test_provider_projection_lifecycle import fixture
from tests.test_router_stream_semantics import decoded, event


@pytest.mark.parametrize("client_id", ["codex", "grokbuild", "gemini"])
def test_imported_provider_is_current_with_independent_mcp(
    tmp_path, monkeypatch, client_id
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    roots = {"codex": ".codex", "grokbuild": ".grok", "gemini": ".gemini"}
    root = tmp_path / roots[client_id]
    root.mkdir()
    if client_id == "gemini":
        (root / "settings.json").write_text(
            json.dumps(
                {"theme": "light", "mcpServers": {"fixture": {"command": "fixture"}}}
            )
        )
        (root / ".env").write_text("GEMINI_MODEL=fixture\n")
    else:
        (root / "config.toml").write_text(
            'model = "fixture"\n[mcp_servers.fixture]\ncommand = "fixture"\n'
        )
    record = read_provider_records(tmp_path, client_id)[0]
    assert provider_matches(
        tmp_path,
        {
            "id": record["provider_id"],
            "app_id": client_id,
            "settings_config": record["settings"],
            "meta": record["meta"],
        },
    )


def test_gemini_distinct_calls_without_optional_ids_are_not_concatenated():
    def call(name, args, finish=False):
        candidate = {
            "content": {"parts": [{"functionCall": {"name": name, "args": args}}]}
        }
        if finish:
            candidate["finishReason"] = "STOP"
        return event("message", {"candidates": [candidate]})

    values = decoded(
        b"".join(
            transform_sse(
                "gemini",
                "openai_chat",
                [call("first", {"x": 1}), call("second", {"y": 2}, True)],
            )
        )
    )
    calls = [
        t
        for v in values
        for c in v.get("choices", [])
        for t in c.get("delta", {}).get("tool_calls", [])
    ]
    assert len({t["index"] for t in calls}) == 2
    assert {t.get("function", {}).get("name") for t in calls} == {"first", "second"}
    assert not any(v.get("error") for v in values)


def test_takeover_waits_for_provider_write_before_snapshotting(tmp_path, monkeypatch):
    app, client, store, tasks, path = fixture(tmp_path, monkeypatch)
    projection = ProviderProjection(
        data_root=str(store.data_root),
        home=str(path.parents[1]),
        client_id="codex",
        provider_id="new",
        provider=store.get_provider("codex", "new"),
    )
    controller = AgentRouterController(
        AgentRouterConfigStore(store.data_root),
        home=path.parents[1],
        health_probe=lambda: {"status": "ok"},
    )
    attempted = Event()
    original = controller.takeover.enable
    entered = Event()

    def observe(*args):
        entered.set()
        return original(*args)

    monkeypatch.setattr(controller.takeover, "enable", observe)

    def takeover():
        attempted.set()
        return controller.enable_takeover("codex")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with provider_projection(projection):
            future = pool.submit(takeover)
            assert attempted.wait(2)
            assert not entered.wait(0.15), (
                "takeover entered while Provider owns native files"
            )
            path.write_text('model = "new"\n')
        assert future.result(timeout=3)["result"]["verified"]
    assert "127.0.0.1" in path.read_text()
    controller.disable_takeover("codex")
    assert path.read_text() == 'model = "new"\n'


def test_corrupt_hermes_is_unknown_not_a_workbench_crash(tmp_path, monkeypatch):
    from app.services.agent_workbench import _provider_public_rows

    app, client, store, tasks, path = fixture(tmp_path, monkeypatch)
    root = path.parents[1] / ".hermes"
    root.mkdir()
    (root / "config.yaml").write_text("providers: [broken")
    store.upsert_provider(
        app_id="hermes",
        provider_id="fixture",
        name="fixture",
        settings={"config": "model: fixture\n"},
        is_current=True,
    )
    rows = _provider_public_rows(store, store.list_providers("hermes"), path.parents[1])
    assert rows["hermes"][0]["live_state"] == "unknown"
    assert client.get("/categories/agent").status_code == 200
