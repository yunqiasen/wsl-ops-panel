from pathlib import Path

import pytest

from app.services.agent_provider_profiles import (
    ProviderProfileError,
    normalize_provider_profile,
    public_runtime_provider,
)
from app.services.agent_provider_secrets import AgentProviderSecretStore
from app.services.agent_providers import AgentProviderStore


def test_custom_provider_accepts_any_http_base_url() -> None:
    profile = normalize_provider_profile(
        "codex",
        {
            "base_url": "https://relay.example/v1",
            "api_format": "openai_responses",
            "api_key": "secret",
            "model": "gpt-x",
        },
    )

    assert profile["base_url"] == "https://relay.example/v1"
    assert profile["api_format"] == "openai_responses"
    assert profile["auth_mode"] == "bearer"
    assert profile["api_key"] == "secret"
    assert profile["model"] == "gpt-x"


def test_profile_extracts_claude_legacy_environment() -> None:
    profile = normalize_provider_profile(
        "claude",
        {
            "type": "claude",
            "config": {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://anthropic-relay.example",
                    "ANTHROPIC_AUTH_TOKEN": "token-value",
                    "ANTHROPIC_MODEL": "claude-relay",
                }
            },
        },
    )

    assert profile["base_url"] == "https://anthropic-relay.example"
    assert profile["api_format"] == "anthropic"
    assert profile["auth_mode"] == "bearer"
    assert profile["api_key"] == "token-value"
    assert profile["model"] == "claude-relay"


def test_profile_rejects_non_http_endpoint() -> None:
    with pytest.raises(ProviderProfileError, match="http"):
        normalize_provider_profile(
            "codex", {"base_url": "file:///tmp/socket", "api_format": "openai_responses"}
        )


def test_public_profile_redacts_headers_and_api_key() -> None:
    public = public_runtime_provider(
        {
            "base_url": "https://relay.example/v1",
            "api_key": "secret",
            "headers": {"X-Key": "secret", "X-Tenant": "team-a"},
        }
    )

    assert public["base_url"] == "https://relay.example/v1"
    assert public["api_key"] == "••••••••"
    assert public["headers"] == {"X-Key": "••••••••", "X-Tenant": "••••••••"}


def test_secret_store_returns_reference_not_raw_value(tmp_path: Path) -> None:
    store = AgentProviderSecretStore(tmp_path)

    ref = store.put("codex", "relay", {"api_key": "secret"})

    assert ref == "provider-secret:codex:relay"
    assert store.resolve(ref)["api_key"] == "secret"
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert "secret" in store.path.read_text(encoding="utf-8")


def test_provider_store_persists_secret_reference_and_resolves_runtime_profile(
    tmp_path: Path,
) -> None:
    store = AgentProviderStore(tmp_path)

    saved = store.upsert_provider(
        app_id="codex",
        provider_id="relay",
        name="Relay",
        settings={
            "type": "codex",
            "routing": {
                "base_url": "https://relay.example/v1",
                "api_format": "openai_responses",
                "api_key": "secret",
                "headers": {"X-Tenant": "team-a"},
            },
        },
    )

    persisted_routing = saved["settings_config"]["routing"]
    assert "api_key" not in persisted_routing
    assert "headers" not in persisted_routing
    assert persisted_routing["secret_ref"] == "provider-secret:codex:relay"
    runtime = store.runtime_profile("codex", "relay")
    assert runtime is not None
    assert runtime["api_key"] == "secret"
    assert runtime["headers"] == {"X-Tenant": "team-a"}


def test_provider_api_accepts_routing_and_probes_without_returning_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    from fastapi.testclient import TestClient

    from app.core.security import COOKIE_NAME, issue_session_token
    from tests.app_factory import create_app

    (tmp_path / "categories").mkdir()
    (tmp_path / "categories" / "agent.yaml").write_text(
        "id: agent\nlabel: Agent\norder: 60\nenabled: true\n", encoding="utf-8"
    )
    (tmp_path / "objects").mkdir()
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "node-packages.yaml").write_text(
        "packages: []\n", encoding="utf-8"
    )
    (tmp_path / "rules" / "python-packages.yaml").write_text(
        "packages: []\n", encoding="utf-8"
    )
    captured: dict[str, object] = {}

    class FakeHttpClient:
        def __init__(self, **kwargs: object) -> None:
            captured["options"] = kwargs

        def __enter__(self) -> "FakeHttpClient":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(
            self,
            url: str,
            *,
            headers: dict[str, str],
            params: dict[str, str],
        ) -> httpx.Response:
            captured.update(url=url, headers=headers, params=params)
            request = httpx.Request("GET", url, headers=headers, params=params)
            return httpx.Response(
                200,
                request=request,
                json={"data": [{"id": "gpt-x"}]},
            )

    monkeypatch.setattr("app.api.agent.httpx.Client", FakeHttpClient)
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    saved = client.post(
        "/api/agent/providers",
        json={
            "app_id": "codex",
            "provider_id": "relay",
            "name": "Relay",
            "settings_config": {"type": "codex"},
            "routing": {
                "base_url": "https://relay.example/v1",
                "api_format": "openai_responses",
                "api_key": "secret",
                "model": "gpt-x",
            },
        },
    )
    probed = client.post("/api/agent/providers/codex/relay/test")

    assert saved.status_code == 200
    routing = saved.json()["settings_config"]["routing"]
    assert "api_key" not in routing
    assert routing["secret_ref"] == "••••••••"
    assert probed.status_code == 200
    assert probed.json()["ok"] is True
    assert probed.json()["status_code"] == 200
    assert isinstance(probed.json()["latency_ms"], int)
    assert "secret" not in probed.text
    assert captured["url"] == "https://relay.example/v1"
    assert captured["headers"] == {"Authorization": "Bearer secret"}


def test_profile_extracts_grokbuild_and_hermes_native_settings() -> None:
    grok = normalize_provider_profile(
        "grokbuild",
        {
            "type": "grokbuild",
            "config": (
                '[models]\ndefault = "relay"\n'
                '[model.relay]\nmodel = "grok-model"\n'
                'base_url = "https://grok.example/v1"\nname = "Relay"\n'
                'env_key = "GROK_KEY"\napi_backend = "responses"\n'
                'context_window = 500000\n'
            ),
        },
    )
    hermes = normalize_provider_profile(
        "hermes",
        {
            "type": "hermes",
            "native_provider": {
                "name": "relay",
                "base_url": "https://hermes.example/v1",
                "api_mode": "codex_responses",
                "api_key": "fixture",
                "model": "gpt-x",
            },
        },
    )

    assert grok["base_url"] == "https://grok.example/v1"
    assert grok["api_format"] == "openai_responses"
    assert grok["model"] == "grok-model"
    assert hermes["base_url"] == "https://hermes.example/v1"
    assert hermes["api_format"] == "openai_responses"
    assert hermes["api_key"] == "fixture"
    assert hermes["model"] == "gpt-x"


def test_public_provider_redacts_secrets_inside_native_config_snapshots() -> None:
    from app.services.agent_providers import public_provider

    safe = public_provider(
        {
            "id": "hermes",
            "settings_config": {
                "type": "hermes",
                "config": "custom_providers:\n  - name: relay\n    api_key: SECRET_IN_YAML\n",
            },
        },
        include_settings=True,
    )

    assert "SECRET_IN_YAML" not in safe["settings_config"]["config"]
    assert "••••••••" in safe["settings_config"]["config"]


def test_grokbuild_profile_resolves_env_key_without_persisting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROK_FIXTURE_KEY", "env-secret")
    profile = normalize_provider_profile(
        "grokbuild",
        {
            "type": "grokbuild",
            "native_provider": {"env_key": "GROK_FIXTURE_KEY"},
            "routing": {
                "base_url": "https://grok.example/v1",
                "api_format": "openai_responses",
                "model": "grok-model",
            },
        },
    )

    assert profile["api_key"] == "env-secret"


def test_provider_for_apply_only_hydrates_secret_store_when_requested(tmp_path: Path) -> None:
    store = AgentProviderStore(tmp_path)
    store.upsert_provider(
        app_id="codex",
        provider_id="relay",
        name="Relay",
        settings={
            "routing": {
                "base_url": "https://relay.example/v1",
                "api_format": "openai_responses",
                "api_key": "stored-secret",
            }
        },
    )

    without = store.provider_for_apply("codex", "relay", include_secrets=False)
    with_secret = store.provider_for_apply("codex", "relay", include_secrets=True)

    assert "api_key" not in without["settings_config"]["routing"]
    assert with_secret["settings_config"]["routing"]["api_key"] == "stored-secret"


def test_provider_connectivity_probe_checks_base_url_and_accepts_http_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按 CC Switch：连通性只证明网关可达，不把 401/404 当作网络断开。"""
    import httpx
    from fastapi.testclient import TestClient

    from app.core.security import COOKIE_NAME, issue_session_token
    from tests.app_factory import create_app

    (tmp_path / "categories").mkdir()
    (tmp_path / "categories" / "agent.yaml").write_text(
        "id: agent\nlabel: Agent\norder: 60\nenabled: true\n", encoding="utf-8"
    )
    (tmp_path / "objects").mkdir()
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "node-packages.yaml").write_text(
        "packages: []\n", encoding="utf-8"
    )
    (tmp_path / "rules" / "python-packages.yaml").write_text(
        "packages: []\n", encoding="utf-8"
    )
    captured: dict[str, object] = {}

    class FakeHttpClient:
        def __init__(self, **kwargs: object) -> None:
            captured["options"] = kwargs

        def __enter__(self) -> "FakeHttpClient":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(
            self,
            url: str,
            *,
            headers: dict[str, str],
            params: dict[str, str],
        ) -> httpx.Response:
            captured.update(url=url, headers=headers, params=params)
            request = httpx.Request("GET", url, headers=headers, params=params)
            return httpx.Response(401, request=request, text="gateway alive")

    monkeypatch.setattr("app.api.agent.httpx.Client", FakeHttpClient)
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    saved = client.post(
        "/api/agent/providers",
        json={
            "app_id": "codex",
            "provider_id": "relay",
            "name": "Relay",
            "settings_config": {"type": "codex"},
            "routing": {
                "base_url": "https://relay.example/v1",
                "api_format": "openai_responses",
                "api_key": "secret",
            },
        },
    )
    assert saved.status_code == 200, saved.text

    probed = client.post("/api/agent/providers/codex/relay/test")

    assert probed.status_code == 200, probed.text
    assert probed.json()["ok"] is True
    assert probed.json()["status_code"] == 401
    assert captured["url"] == "https://relay.example/v1"
    assert captured["headers"] == {"Authorization": "Bearer secret"}


def test_normalize_provider_profile_reads_native_claude_settings() -> None:
    profile = normalize_provider_profile(
        "claude",
        {
            "env": {
                "ANTHROPIC_BASE_URL": "https://claude-native.example/v1",
                "ANTHROPIC_AUTH_TOKEN": "native-key",
                "ANTHROPIC_MODEL": "claude-native",
            },
            "hooks": {"keep": True},
        },
    )

    assert profile["base_url"] == "https://claude-native.example/v1"
    assert profile["api_key"] == "native-key"
    assert profile["model"] == "claude-native"
    assert profile["api_format"] == "anthropic"


def test_provider_store_runtime_profile_uses_native_settings_and_meta(tmp_path: Path) -> None:
    store = AgentProviderStore(tmp_path / "data" / "agent")
    store.upsert_provider(
        app_id="openclaw",
        provider_id="relay",
        name="Relay",
        settings={
            "baseUrl": "https://claw-native.example/v1",
            "apiKey": "claw-key",
            "api": "openai-responses",
            "models": [{"id": "gpt-5.6"}],
        },
        meta={
            "model_map": {"gpt-5.6": "upstream-5.6"},
            "use_outbound_proxy": False,
        },
    )

    profile = store.runtime_profile("openclaw", "relay")

    assert profile is not None
    assert profile["base_url"] == "https://claw-native.example/v1"
    assert profile["api_format"] == "openai_responses"
    assert profile["model"] == "gpt-5.6"
    assert profile["model_map"] == {"gpt-5.6": "upstream-5.6"}
    assert profile["use_outbound_proxy"] is False
