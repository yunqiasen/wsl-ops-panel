# Agent Workbench CC Switch Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Rebuild the local WSL Agent workbench around a truthful single-client UI, arbitrary Provider profiles, an independent local Agent Router, and client-specific MCP/Skill/Prompt operations.

**Architecture:** FastAPI remains the control plane. A separate `app.agent_router.main` process reads a permission-restricted runtime configuration and proxies namespaced client requests to arbitrary upstreams. Client adapters own detection, config writing, route takeover, restore, MCP, Skill, and Prompt paths; the Web UI only exposes capabilities reported by the selected detected client.

**Tech Stack:** Python 3.13, FastAPI, httpx, Pydantic 2, SQLite, Jinja2, vanilla JavaScript/CSS, pytest, Node test runner, systemd.

---

## Working-tree constraint

The authoritative workspace already contains a large uncommitted Agent implementation. Work in the current `phase1-complete` branch, never run reset/checkout/clean, never stage with `git add -A`, and commit only the exact files named by each task. Preserve unrelated modifications.

## File map

- `app/services/agent_clients.py`: eight-client registry, capability declaration, truthful detection.
- `app/services/agent_provider_profiles.py`: normalized arbitrary-upstream profile and legacy settings extraction.
- `app/services/agent_provider_secrets.py`: permission-restricted Provider credential references.
- `app/services/agent_router_config.py`: atomic `router.json` runtime configuration and public redaction.
- `app/services/agent_route_takeover.py`: per-client route endpoint, backup, takeover, readback, restore.
- `app/services/agent_router_control.py`: process health and systemd lifecycle control.
- `app/agent_router/transforms.py`: protocol request/response transformations.
- `app/agent_router/app.py`: independent data-plane FastAPI app and streaming forwarder.
- `app/agent_router/main.py`: uvicorn entrypoint using environment configuration.
- `app/api/agent_router.py`: authenticated control-plane API.
- `app/api/agent.py`: Provider/MCP/Skill/Prompt local-only API adjustments.
- `app/services/agent_skills.py`: copy/symlink/ZIP/Git installation.
- `app/services/agent_prompts.py`: import/apply/restore with atomic writes.
- `app/services/agent_workbench.py`: selected-client context and router status.
- `app/templates/agent_category.html`: compact single-client UI and route panel.
- `app/static/app.js`: exclusive client selection and local actions.
- `app/static/app.css`: compact CC Switch-like layout.
- `scripts/install_agent_router_service.sh`: systemd unit installer.
- `config/objects/systemd-agent-router.yaml`: operations-panel service registration.
- `tests/test_agent_clients.py`: registry and capability tests.
- `tests/test_agent_provider_profiles.py`: arbitrary Provider normalization and redaction.
- `tests/test_agent_router.py`: config, transforms, forwarding, controller, and control API.
- `tests/test_agent_route_takeover.py`: client takeover/restore tests.
- `tests/test_agent_skills_prompts.py`: Skill and Prompt filesystem tests.
- `tests/test_agent_workbench.py`: page/API regression tests.
- `tests/frontend/agent-workbench.test.cjs`: exclusive client state and capability filtering.
- `README.md`, `docs/operations.md`: delivered behavior and operations.

### Task 1: Eight-client capability registry and single-client context

**Files:**
- Modify: `app/services/agent_clients.py`
- Modify: `app/services/agent_workbench.py`
- Create: `tests/test_agent_clients.py`

- [x] **Step 1: Write failing registry tests**

```python
def test_registry_contains_all_eight_clients_and_declares_route_capability():
    by_id = {item.id: item for item in AGENT_CLIENTS}
    assert set(by_id) == {"claude", "claude-desktop", "codex", "gemini", "grokbuild", "opencode", "openclaw", "hermes"}
    assert "route" in by_id["codex"].features
    assert "route" not in by_id["hermes"].features


def test_payload_returns_detected_clients_separately(tmp_path):
    (tmp_path / ".codex").mkdir()
    payload = agent_clients_payload(tmp_path)
    assert [row["id"] for row in payload if row["detected"]] == ["codex"]
```

- [x] **Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_agent_clients.py -q`
Expected: fail because Claude Desktop/Grok Build and route capabilities are absent.

- [x] **Step 3: Extend the definition and context**

Add `binary_names`, `features`, `write_support`, `route_path`, and `detection_paths`. Detection is true only when a configured path exists or `shutil.which()` finds a declared binary. Return `agent_detected_clients`, `agent_supported_clients`, and `agent_active_client` where active defaults to the first detected client.

- [x] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest tests/test_agent_clients.py tests/test_agent_workbench.py -q`
Expected: all tests pass.

- [x] **Step 5: Commit exact files**

```bash
git add app/services/agent_clients.py app/services/agent_workbench.py tests/test_agent_clients.py
git commit -m "feat: add truthful agent client capability registry"
```

### Task 2: Normalized arbitrary Provider profiles

**Files:**
- Create: `app/services/agent_provider_profiles.py`
- Create: `app/services/agent_provider_secrets.py`
- Modify: `app/services/agent_providers.py`
- Modify: `app/api/agent.py`
- Create: `tests/test_agent_provider_profiles.py`

- [x] **Step 1: Write failing normalization tests**

```python
def test_custom_provider_accepts_any_base_url():
    profile = normalize_provider_profile("codex", {"base_url": "https://relay.example/v1", "api_format": "openai_responses", "api_key": "secret", "model": "gpt-x"})
    assert profile["base_url"] == "https://relay.example/v1"
    assert profile["api_format"] == "openai_responses"


def test_public_profile_redacts_headers_and_api_key():
    public = public_runtime_provider({"api_key": "secret", "headers": {"X-Key": "secret"}})
    assert public == {"api_key": "••••••••", "headers": {"X-Key": "••••••••"}}


def test_secret_store_returns_reference_not_raw_value(tmp_path):
    store = AgentProviderSecretStore(tmp_path)
    ref = store.put("codex", "relay", {"api_key": "secret"})
    assert ref.startswith("provider-secret:")
    assert store.resolve(ref)["api_key"] == "secret"
    assert store.path.stat().st_mode & 0o777 == 0o600
```

- [x] **Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_agent_provider_profiles.py -q`
Expected: import failure for the new module.

- [x] **Step 3: Implement profile schema**

Implement `ProviderRuntimeProfile` with `base_url`, `api_format`, `api_key`, `auth_mode`, `model`, `model_map`, `headers`, `full_url`, and `use_outbound_proxy`. Add legacy extraction from Codex TOML summary, Claude env, Gemini env, OpenCode options, OpenClaw camelCase, and Hermes snake_case. Save normalized non-secret values under `settings_config.routing`; move `api_key` and secret headers into `AgentProviderSecretStore`, store only `secret_ref`, and preserve client-native configuration for round-trip import.

- [x] **Step 4: Extend Provider API**

Accept optional `routing` in `AgentProviderUpsertRequest`; merge it into `settings_config`. Add `POST /api/agent/providers/{app_id}/{provider_id}/test` using `httpx` with `/v1/models` when the profile is OpenAI compatible, returning status/latency without returning credentials.

- [x] **Step 5: Verify GREEN**

Run: `.venv/bin/pytest tests/test_agent_provider_profiles.py tests/test_agent_workbench.py -q`
Expected: all tests pass.

- [x] **Step 6: Commit exact files**

```bash
git add app/services/agent_provider_profiles.py app/services/agent_provider_secrets.py app/services/agent_providers.py app/api/agent.py tests/test_agent_provider_profiles.py
git commit -m "feat: support arbitrary agent provider profiles"
```

### Task 3: Router runtime configuration store

**Files:**
- Create: `app/services/agent_router_config.py`
- Create: `tests/test_agent_router.py`

- [x] **Step 1: Write failing atomic-store tests**

```python
def test_router_store_is_atomic_private_and_redacted(tmp_path):
    store = AgentRouterConfigStore(tmp_path)
    saved = store.update_global(listen_port=7888, outbound_proxy="socks5://127.0.0.1:1080")
    store.set_provider("codex", {"base_url": "https://relay.example/v1", "api_key": "secret", "api_format": "openai_responses"})
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.public_snapshot()["providers"]["codex"]["api_key"] == "••••••••"
    assert saved["listen_port"] == 7888
```

- [x] **Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_agent_router.py::test_router_store_is_atomic_private_and_redacted -q`
Expected: import failure for `AgentRouterConfigStore`.

- [x] **Step 3: Implement store**

Use `data/agent/router.json` with schema version 1, default `127.0.0.1:7888`, `show_home_switch`, `outbound_proxy`, `takeover`, `providers`, and `updated_at`. Write through a temporary file, `fsync`, `os.replace`, and `chmod(0600)`. Expose raw `snapshot()` only to trusted service code and `public_snapshot()` to APIs/templates.

- [x] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest tests/test_agent_router.py -q`
Expected: store tests pass.

- [x] **Step 5: Commit exact files**

```bash
git add app/services/agent_router_config.py tests/test_agent_router.py
git commit -m "feat: add private agent router configuration store"
```

### Task 4: Protocol transform core

**Files:**
- Create: `app/agent_router/__init__.py`
- Create: `app/agent_router/transforms.py`
- Modify: `tests/test_agent_router.py`

- [x] **Step 1: Write failing transform tests**

```python
def test_anthropic_request_converts_to_openai_chat():
    result = transform_request("anthropic", "openai_chat", {"model": "claude", "system": "rules", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 64})
    assert result.body["messages"][:2] == [{"role": "system", "content": "rules"}, {"role": "user", "content": "hello"}]
    assert result.endpoint == "/v1/chat/completions"


def test_openai_chat_response_converts_to_anthropic():
    body = transform_response("openai_chat", "anthropic", {"id": "x", "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}})
    assert body["content"] == [{"type": "text", "text": "hi"}]
    assert body["usage"] == {"input_tokens": 2, "output_tokens": 1}
```

- [x] **Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_agent_router.py -k transform -q`
Expected: import failure for transforms.

- [x] **Step 3: Implement minimal complete transforms**

Implement same-format pass-through plus Anthropic ↔ OpenAI Chat, Anthropic ↔ OpenAI Responses, and OpenAI Responses ↔ OpenAI Chat for text, system messages, tools, tool calls/results, model, token limits, temperature, stop reason, and usage. Unsupported pairs raise `UnsupportedProtocolTransform` with both formats in the message.

- [x] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest tests/test_agent_router.py -k transform -q`
Expected: all transform tests pass.

- [x] **Step 5: Commit exact files**

```bash
git add app/agent_router/__init__.py app/agent_router/transforms.py tests/test_agent_router.py
git commit -m "feat: add agent router protocol transforms"
```

### Task 5: Independent Agent Router data plane

**Files:**
- Create: `app/agent_router/app.py`
- Create: `app/agent_router/main.py`
- Modify: `tests/test_agent_router.py`

- [x] **Step 1: Write failing proxy tests**

```python
def test_cross_protocol_stream_is_transformed_to_anthropic_sse():
    events = transform_sse("openai_chat", "anthropic", [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n', b"data: [DONE]\n\n"])
    rendered = b"".join(events)
    assert b"content_block_delta" in rendered
    assert b'"text":"hi"' in rendered


def test_router_forwards_codex_to_arbitrary_upstream(tmp_path, monkeypatch):
    store = AgentRouterConfigStore(tmp_path)
    store.set_provider("codex", {"base_url": "https://relay.example/v1", "api_format": "openai_responses", "api_key": "secret", "model": "relay-model"})
    app = create_agent_router_app(store=store, transport=mock_transport)
    response = TestClient(app).post("/codex/v1/responses", json={"model": "client-model", "input": "hello"})
    assert response.status_code == 200
    assert captured.url == "https://relay.example/v1/responses"
    assert captured.headers["authorization"] == "Bearer secret"
    assert captured.json()["model"] == "relay-model"
```

- [x] **Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_agent_router.py -k forwards -q`
Expected: import failure for `create_agent_router_app`.

- [x] **Step 3: Implement data plane**

Expose `/health`, `/status`, and namespaced catch-all routes for `claude`, `codex`, `gemini`, `grokbuild`, `opencode`, and `openclaw`. Select the current per-client Provider, apply model mapping, build the upstream URL, set auth/header rules, honor optional HTTP/SOCKS proxy, stream same-format responses, and transform SSE incrementally for supported cross-protocol pairs. Maintain in-memory active/total/success/failure counters without logging bodies or secrets.

- [x] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest tests/test_agent_router.py -q`
Expected: all router tests pass.

- [x] **Step 5: Commit exact files**

```bash
git add app/agent_router/app.py app/agent_router/main.py tests/test_agent_router.py
git commit -m "feat: add independent local agent router"
```

### Task 6: Client route takeover and restore

**Files:**
- Create: `app/services/agent_route_takeover.py`
- Create: `tests/test_agent_route_takeover.py`

- [x] **Step 1: Write failing takeover tests**

```python
def test_codex_takeover_preserves_config_and_restores(tmp_path):
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('model = "keep"\nmodel_provider = "original"\n[model_providers.original]\nbase_url = "https://relay.example/v1"\n')
    manager = AgentRouteTakeover(tmp_path, tmp_path / "state")
    manager.enable("codex", "http://127.0.0.1:7888/codex/v1")
    assert 'model = "keep"' in config.read_text()
    assert "127.0.0.1:7888" in config.read_text()
    manager.disable("codex")
    assert config.read_text().startswith('model = "keep"')
    assert "https://relay.example/v1" in config.read_text()
```

- [x] **Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_agent_route_takeover.py -q`
Expected: import failure for the takeover manager.

- [x] **Step 3: Implement client adapters**

Support Codex TOML, Claude `.claude.json`, Gemini `.env`, OpenCode JSON, and OpenClaw JSON. Store a private snapshot and post-takeover hash per client. On disable, restore the full file when unchanged; when externally changed, restore only fields owned by routing. Validate each written file and read back the local route address.

- [x] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest tests/test_agent_route_takeover.py -q`
Expected: all takeover and external-edit tests pass.

- [x] **Step 5: Commit exact files**

```bash
git add app/services/agent_route_takeover.py tests/test_agent_route_takeover.py
git commit -m "feat: add verified client route takeover"
```

### Task 7: Router lifecycle and authenticated control API

**Files:**
- Create: `app/services/agent_router_control.py`
- Create: `app/api/agent_router.py`
- Modify: `app/main.py`
- Modify: `app/services/agent_workbench.py`
- Modify: `tests/test_agent_router.py`

- [x] **Step 1: Write failing controller/API tests**

```python
def test_router_control_api_updates_provider_and_takeover(auth_client, tmp_path, monkeypatch):
    response = auth_client.put("/api/agent/router/config", json={"listen_address": "127.0.0.1", "listen_port": 7888, "show_home_switch": True, "outbound_proxy": None})
    assert response.status_code == 200
    takeover = auth_client.put("/api/agent/router/apps/codex/takeover", json={"enabled": True})
    assert takeover.status_code == 200
    assert takeover.json()["takeover"]["codex"] is True
```

- [x] **Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_agent_router.py -k control_api -q`
Expected: 404 for the new API.

- [x] **Step 3: Implement controller and API**

Controller probes `/health`, reports port conflicts, and runs exact `sudo -n systemctl start|stop|restart wsl-agent-router.service` commands through an injected runner. API exposes status, global config, lifecycle, current Provider, and per-client takeover. Stopping with active takeovers returns 409 unless `restore_clients=true`; restore all before stopping.

- [x] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest tests/test_agent_router.py tests/test_app_smoke.py -q`
Expected: all tests pass.

- [x] **Step 5: Commit exact files**

```bash
git add app/services/agent_router_control.py app/api/agent_router.py app/main.py app/services/agent_workbench.py tests/test_agent_router.py
git commit -m "feat: expose agent router control plane"
```

### Task 8: Compact single-client UI with Route tab

**Files:**
- Modify: `app/templates/agent_category.html`
- Modify: `app/static/app.js`
- Modify: `app/static/app.css`
- Modify: `tests/test_agent_workbench.py`
- Create: `tests/frontend/agent-workbench.test.cjs`

- [x] **Step 1: Write failing page and JS tests**

```python
def test_agent_page_is_local_single_client_and_has_route_panel(tmp_path, monkeypatch):
    response = authenticated_agent_page(tmp_path, monkeypatch)
    assert 'data-agent-tab-control="route"' in response.text
    assert "执行设备" not in response.text
    assert "data-agent-mcp-matrix" not in response.text
    assert "未检测" not in extract_client_switcher(response.text)
```

```javascript
test('客户端选择始终保持一个激活项', () => {
  assert.equal(nextAgentClient('codex', 'codex'), 'codex');
  assert.equal(nextAgentClient('codex', 'claude'), 'claude');
});
```

- [x] **Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_agent_workbench.py -k single_client -q && node --test tests/frontend/agent-workbench.test.cjs`
Expected: page assertion fails and JS export is missing.

- [x] **Step 3: Replace information architecture**

Remove device bar and matrix dialog from the Agent page. Render only detected clients as equal-width exclusive buttons. Add Provider, Route, MCP, Skills, Prompt tabs and hide tabs not listed in the active client capabilities. Route panel includes service status, total switch, homepage switch, per-detected-client takeover buttons, service address, outbound proxy, and current target.

- [x] **Step 4: Implement local actions**

Every Provider/MCP/Skill/Prompt action sends exactly one `client_id` and implicit `__local__`. Remove the generic “同步选中 MCP” listener. Keep explicit install/update, uninstall, edit, and local-library delete semantics.

- [x] **Step 5: Verify GREEN**

Run: `.venv/bin/pytest tests/test_agent_workbench.py -q && node --test tests/frontend/*.test.cjs`
Expected: all Python and frontend tests pass.

- [x] **Step 6: Commit exact files**

```bash
git add app/templates/agent_category.html app/static/app.js app/static/app.css tests/test_agent_workbench.py tests/frontend/agent-workbench.test.cjs
git commit -m "feat: rebuild agent workbench as single-client UI"
```

### Task 9: Local MCP direct operation closure

**Files:**
- Modify: `app/api/agent.py`
- Modify: `app/services/agent_mcp_adapters.py`
- Modify: `app/services/agent_workbench.py`
- Modify: `tests/test_agent_workbench.py`

- [x] **Step 1: Write failing local-operation tests**

```python
def test_local_mcp_install_and_uninstall_execute_selected_client(auth_client, monkeypatch):
    install = auth_client.post("/api/agent/mcp/local/install", json={"client_id": "codex", "mcp_ids": ["context7"]})
    assert install.status_code == 200
    assert install.json()["verified"] is True
    uninstall = auth_client.post("/api/agent/mcp/local/uninstall", json={"client_id": "codex", "mcp_ids": ["context7"]})
    assert uninstall.status_code == 200
    assert uninstall.json()["verified"] is True
```

- [x] **Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_agent_workbench.py -k local_mcp -q`
Expected: 404 for local endpoints.

- [x] **Step 3: Implement synchronous local endpoints**

Use `apply_mcp_to_home` and `remove_mcp_from_home` directly for the current WSL, then rescan and update observations. Return added/updated/removed IDs and `verified`; never infer removal from unchecked items. Preserve legacy queue endpoints for compatibility but remove them from the new UI.

- [x] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest tests/test_agent_workbench.py -q`
Expected: all tests pass.

- [x] **Step 5: Commit exact files**

```bash
git add app/api/agent.py app/services/agent_mcp_adapters.py app/services/agent_workbench.py tests/test_agent_workbench.py
git commit -m "feat: close local single-client MCP workflow"
```

### Task 10: Skill library sources and sync modes

**Files:**
- Modify: `app/services/agent_skills.py`
- Modify: `app/api/agent.py`
- Create: `tests/test_agent_skills_prompts.py`

- [x] **Step 1: Write failing Skill tests**

```python
def test_skill_install_supports_zip_copy_and_symlink(tmp_path):
    archive = build_skill_zip(tmp_path)
    install_skill_to_home(tmp_path / "home", "codex", "demo", str(archive), mode="copy")
    assert (tmp_path / "home/.codex/skills/demo/SKILL.md").exists()
    source = tmp_path / "source"
    create_skill(source)
    install_skill_to_home(tmp_path / "home", "claude", "linked", str(source), mode="symlink")
    assert (tmp_path / "home/.claude/skills/linked").is_symlink()
```

- [x] **Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_agent_skills_prompts.py -k skill -q`
Expected: import failure for direct Skill functions.

- [x] **Step 3: Implement direct local Skill library**

Add safe ZIP extraction that rejects absolute paths and `..`, Git clone/update, local directory copy, and symlink mode. Require a `SKILL.md` entrypoint, write `.wsl-ops-skill.json` source metadata, move removed installs to the backup directory, and return readback status.

- [x] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest tests/test_agent_skills_prompts.py -k skill -q`
Expected: all Skill tests pass.

- [x] **Step 5: Commit exact files**

```bash
git add app/services/agent_skills.py app/api/agent.py tests/test_agent_skills_prompts.py
git commit -m "feat: add verified local skill installation modes"
```

### Task 11: Prompt import, apply, and restore

**Files:**
- Modify: `app/services/agent_prompts.py`
- Modify: `app/api/agent.py`
- Modify: `tests/test_agent_skills_prompts.py`

- [x] **Step 1: Write failing Prompt tests**

```python
def test_prompt_import_apply_and_restore(tmp_path):
    target = tmp_path / ".codex/AGENTS.md"
    target.parent.mkdir(parents=True)
    target.write_text("original\n")
    manager = AgentPromptFileManager(tmp_path, tmp_path / "state")
    assert manager.import_current("codex")["content"] == "original\n"
    manager.apply("codex", "replacement\n")
    assert target.read_text() == "replacement\n"
    manager.restore("codex")
    assert target.read_text() == "original\n"
```

- [x] **Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_agent_skills_prompts.py -k prompt -q`
Expected: import failure for `AgentPromptFileManager`.

- [x] **Step 3: Implement file manager and API**

Resolve the adapter-declared prompt target, import current content, create private backup metadata, atomically write Markdown, verify hash, and restore. Add `/prompts/import-current`, `/prompts/local/apply`, and `/prompts/local/restore` endpoints for one client.

- [x] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest tests/test_agent_skills_prompts.py tests/test_agent_workbench.py -q`
Expected: all tests pass.

- [x] **Step 5: Commit exact files**

```bash
git add app/services/agent_prompts.py app/api/agent.py tests/test_agent_skills_prompts.py
git commit -m "feat: add prompt import apply and restore"
```

### Task 12: Service installation, documentation, and end-to-end verification

**Files:**
- Create: `scripts/install_agent_router_service.sh`
- Create: `config/objects/systemd-agent-router.yaml`
- Modify: `scripts/check_sudo_rules.sh`
- Modify: `README.md`
- Modify: `docs/operations.md`

- [x] **Step 1: Add Router systemd installer**

Create `wsl-agent-router.service` with project working directory, `.venv/bin/python -m app.agent_router.main`, `AGENT_ROUTER_CONFIG=<project>/data/agent/router.json`, restart-on-failure, and localhost defaults. Register the unit in the systemd object catalog and sudo check.

- [x] **Step 2: Run focused verification**

Run:

```bash
.venv/bin/pytest tests/test_agent_clients.py tests/test_agent_provider_profiles.py tests/test_agent_router.py tests/test_agent_route_takeover.py tests/test_agent_skills_prompts.py tests/test_agent_workbench.py -q
node --test tests/frontend/*.test.cjs
.venv/bin/ruff check app tests
```

Expected: zero failures and zero lint errors.

- [x] **Step 3: Update documentation**

Document single-client navigation, arbitrary upstreams, direct vs local route, outbound proxy distinction, per-client takeover/restore, explicit MCP operations, Skill modes, Prompt restore, service commands, ports, and credential redaction.

- [x] **Step 4: Run full project verification**

Run:

```bash
.venv/bin/pytest -q
node --test tests/frontend/*.test.cjs
.venv/bin/ruff check .
git diff --check
```

Expected: zero failures, zero lint errors, no whitespace errors.

- [x] **Step 5: Install and live-verify**

Run:

```bash
bash scripts/install_agent_router_service.sh
sudo systemctl restart wsl-ops-panel.service
curl -fsS http://127.0.0.1:7888/health
curl -fsS http://127.0.0.1:8328/healthz
```

Then use the browser to verify the Agent page, route switch, per-client selection, Provider editor, MCP install/uninstall, Skill and Prompt panels, console errors, and failed requests.

- [x] **Step 6: Commit exact files**

```bash
git add scripts/install_agent_router_service.sh config/objects/systemd-agent-router.yaml scripts/check_sudo_rules.sh README.md docs/operations.md
git commit -m "docs: ship local agent router operations"
```

## 实施验收（2026-07-26）

- 聚焦 Agent 测试：`72 passed`。
- 全量测试：`345 passed`。
- 前端 Node 测试：`9 passed`。
- Ruff、Python 编译、JavaScript 语法、Shell 语法和 `git diff --check` 全部通过。
- 运行态：`wsl-ops-panel.service` 与 `wsl-agent-router.service` 均为 `active`；`8328/healthz` 与 `7888/health` 返回正常。
- 浏览器验收：桌面 `1600px`、移动端 `390px` 均无横向溢出；4 个真实检测客户端按钮等宽且单选；5 个页签可切换；无设备栏、无 MCP 矩阵、无控制台错误和失败请求。
