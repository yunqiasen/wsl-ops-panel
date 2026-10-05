# Agent MCP Target Matrix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Replace misleading MCP client markers with verified device/client inventory and provide safe per-device, per-client MCP installation through a target matrix.

**Architecture:** Keep `agent_mcp_servers` as the logical library, add variants/assignments/observations/operations to `state.db`, and isolate client-format differences behind adapters. Scans read real target configuration; apply plans incrementally merge selected MCP entries, back up files, validate output, and verify by reading the written configuration.

**Tech Stack:** FastAPI, Pydantic, SQLite, Jinja2, vanilla JavaScript/CSS, SSH task queue, pytest, Ruff.

---

## File map

- Create `app/services/agent_mcp_adapters.py`: client-specific read/normalize/merge/render behavior.
- Modify `app/services/state_store.py`: variants, assignments, observations, operations schema and CRUD.
- Modify `app/services/agent_mcp.py`: inventory scanning, preview models, incremental verified apply scripts.
- Modify `app/services/agent_workbench.py`: truthful local inventory for page rendering.
- Modify `app/api/agent.py`: scan, inventory, preview, apply, uninstall endpoints.
- Modify `app/templates/agent_category.html`: equal toggle buttons, truthful status rows, install matrix dialog.
- Modify `app/static/app.js`: `aria-pressed` selection, scan/import, preview, matrix submit, result rendering.
- Modify `app/static/app.css`: equal control sizing, matrix, status badges, responsive layout.
- Modify `tests/test_state_store.py`: persistence and migration behavior.
- Modify `tests/test_agent_workbench.py`: adapters, APIs, truthful rendering, incremental writes.
- Modify `README.md` and `docs/operations.md`: MCP workflow and operational guarantees.

### Task 1: Persist MCP variants, assignments, observations, and operations

**Files:**
- Modify: `app/services/state_store.py`
- Test: `tests/test_state_store.py`

- [x] **Step 1: Write failing schema/CRUD tests**

Add tests that create one logical MCP, save a Codex/Linux variant, assign it to `__local__/codex`, save an installed observation, and assert round-trip values. Add a migration assertion proving legacy `agent_mcp_targets` rows do not create installed observations.

- [x] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_state_store.py -k 'mcp_variant or mcp_assignment or mcp_observation or legacy_mcp_targets'
```

Expected: failures because the new store methods/tables do not exist.

- [x] **Step 3: Add schema and focused CRUD methods**

Add tables with unique keys:

```sql
UNIQUE(mcp_id, client_id, platform)
UNIQUE(node_id, client_id, mcp_id)
UNIQUE(node_id, client_id, mcp_id)
```

Add methods:

```python
upsert_mcp_variant(...)
list_mcp_variants(...)
replace_mcp_assignments(...)
list_mcp_assignments(...)
replace_mcp_observations(...)
list_mcp_observations(...)
record_mcp_operation(...)
list_mcp_operations(...)
```

Public observation specs remain redacted before leaving the service layer.

- [x] **Step 4: Run tests and verify GREEN**

Run the targeted command from Step 2; expected all selected tests pass.

### Task 2: Add client-specific MCP adapters

**Files:**
- Create: `app/services/agent_mcp_adapters.py`
- Modify: `app/services/agent_mcp.py`
- Test: `tests/test_agent_workbench.py`

- [x] **Step 1: Write failing adapter tests**

Cover:

- Codex TOML reads and incrementally preserves an existing MCP.
- Claude/Gemini JSON preserves unrelated top-level fields and unselected MCPs.
- OpenCode converts between `local/remote` and canonical specs.
- OpenClaw preserves the discovered MCP table key.
- Hermes preserves unrelated YAML fields.
- Every adapter returns the same canonical shape for `stdio` and remote HTTP MCPs.

- [x] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest -q tests/test_agent_workbench.py -k 'adapter or incremental_mcp'
```

Expected: import or assertion failures because adapters do not exist.

- [x] **Step 3: Implement adapter registry**

Expose:

```python
get_mcp_adapter(client_id: str) -> McpClientAdapter | None
scan_mcp_home(home: Path, client_id: str) -> dict[str, dict]
merge_mcp_home(home: Path, client_id: str, selected: dict[str, dict]) -> ApplyResult
remove_mcp_home(home: Path, client_id: str, server_ids: set[str]) -> ApplyResult
```

Use atomic writes and validate generated TOML/JSON/YAML before replacement. Do not log raw specs.

- [x] **Step 4: Run tests and verify GREEN**

Run the targeted adapter tests; expected all pass.

### Task 3: Build truthful inventory and target preview

**Files:**
- Modify: `app/services/agent_mcp.py`
- Modify: `app/services/agent_workbench.py`
- Test: `tests/test_agent_workbench.py`

- [x] **Step 1: Write failing inventory tests**

Create temporary homes with different MCP sets per client. Assert:

```python
inventory['codex']['context7']['status'] == 'installed'
inventory['openclaw']['context7']['status'] == 'not_assigned'
```

Assert legacy `apps=True` never produces `installed` without a real observation. Add preview coverage where WSL/Codex receives `context7` and Mac/Claude receives `deepwiki` in one request.

- [x] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest -q tests/test_agent_workbench.py -k 'truthful_inventory or target_preview'
```

- [x] **Step 3: Implement inventory and preview services**

Add service models/functions:

```python
scan_local_mcp_inventory(store, home, apps)
build_mcp_inventory(store, node_ids, apps)
preview_mcp_assignments(store, assignments)
```

Status calculation uses observations and desired assignments, never legacy apps markers.

- [x] **Step 4: Run tests and verify GREEN**

Run the targeted inventory tests; expected all pass.

### Task 4: Replace destructive sync with incremental verified apply

**Files:**
- Modify: `app/services/agent_mcp.py`
- Modify: `app/api/agent.py`
- Test: `tests/test_agent_workbench.py`

- [x] **Step 1: Write failing apply tests**

Assert:

- applying `new` preserves existing `old`;
- selected assignments can differ by node and client;
- Windows returns `unsupported` before queueing;
- offline nodes appear in `skipped`;
- generated apply command performs backup, atomic write, parse validation, and readback verification;
- no raw credential value appears in task object ID, public response, or readable command summary.

- [x] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest -q tests/test_agent_workbench.py -k 'mcp_apply or preserves_existing or unsupported_windows'
```

- [x] **Step 3: Add APIs and queued apply plan**

Add:

```http
POST /api/agent/mcp/scan
GET  /api/agent/mcp/inventory
POST /api/agent/mcp/preview
POST /api/agent/mcp/apply
POST /api/agent/mcp/uninstall
```

Keep `/mcp/import-local` temporarily as a compatibility alias for local scan/import. Deprecate `/mcp/sync` by routing it through incremental apply semantics until the UI no longer uses it.

For apply, group assignments by node, then create one queued task per node. Within each node, invoke the appropriate adapter payload per client. The remote script verifies the written MCP IDs and exits non-zero on mismatch.

- [x] **Step 4: Run tests and verify GREEN**

Run targeted API/apply tests; expected all pass.

### Task 5: Replace checkbox controls and false status dots

**Files:**
- Modify: `app/templates/agent_category.html`
- Modify: `app/static/app.js`
- Modify: `app/static/app.css`
- Test: `tests/test_agent_workbench.py`

- [x] **Step 1: Write failing render tests**

Assert the page:

```python
assert 'data-agent-scope-device' in html
assert 'data-agent-scope-client' in html
assert 'aria-pressed=' in html
assert 'data-agent-mcp-install' in html
assert 'data-agent-mcp-matrix' in html
assert 'agent-app-dot' not in html
assert 'data-agent-app value=' not in html
```

Also assert local real observations render status text and scan timestamps.

- [x] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest -q tests/test_agent_workbench.py -k 'renders_scope_buttons or renders_truthful_mcp_status'
```

- [x] **Step 3: Implement semantic equal-size toggle buttons**

Use `<button type="button" aria-pressed="true|false">` for device and client filters. CSS requirements:

```css
.agent-scope-button {
  inline-size: 168px;
  min-block-size: 48px;
}
```

At mobile widths use two equal columns. Disabled/offline buttons include visible text, not opacity alone.

- [x] **Step 4: Replace MCP dots with verified status**

Each row renders target scope, status text, last scan time, and actions `安装到…`, `编辑`, `从目标卸载…`, `从本地库删除`.

- [x] **Step 5: Run tests and verify GREEN**

Run the targeted render tests; expected all pass.

### Task 6: Implement install matrix and preview interaction

**Files:**
- Modify: `app/templates/agent_category.html`
- Modify: `app/static/app.js`
- Modify: `app/static/app.css`
- Test: `tests/test_agent_workbench.py`

- [x] **Step 1: Write failing API/UI contract tests**

Assert matrix cells identify `node_id` and `client_id`, unsupported/offline cells are disabled, and the preview payload supports different MCP sets per target.

- [x] **Step 2: Run tests and verify RED**

```bash
.venv/bin/pytest -q tests/test_agent_workbench.py -k 'install_matrix or preview_payload'
```

- [x] **Step 3: Implement matrix dialog**

The dialog flow is:

```text
select MCPs -> choose target cells -> preview -> confirm apply -> show per-target results
```

Matrix cells use buttons with `aria-pressed`. At 390px, render device cards with client buttons instead of horizontal table scrolling.

- [x] **Step 4: Render skipped and failed targets**

Do not reduce responses to `queued_count`. Show each target result and link queued task IDs to Task Center.

- [x] **Step 5: Run tests and verify GREEN**

Run targeted matrix tests; expected all pass.

### Task 7: Migrate current UI state safely

**Files:**
- Modify: `app/services/state_store.py`
- Modify: `app/services/agent_workbench.py`
- Test: `tests/test_state_store.py`
- Test: `tests/test_agent_workbench.py`

- [x] **Step 1: Write failing migration test**

Load legacy MCP servers and targets. Assert definitions remain, no installed status is inferred, and a subsequent local scan creates observations matching actual config.

- [x] **Step 2: Run test and verify RED**

```bash
.venv/bin/pytest -q tests/test_state_store.py tests/test_agent_workbench.py -k 'legacy_mcp_migration'
```

- [x] **Step 3: Implement idempotent migration**

Do not delete existing MCP definitions, credentials, metadata, or legacy targets. Mark imported variants with `source=migration`; actual status remains `unknown` until scan.

- [x] **Step 4: Run test and verify GREEN**

Run the targeted migration test; expected all pass.

### Task 8: Documentation and full verification

**Files:**
- Modify: `README.md`
- Modify: `docs/operations.md`
- Verify: all changed files

- [x] **Step 1: Run static and syntax checks**

```bash
.venv/bin/ruff check app tests
.venv/bin/python -m compileall -q app tests
node --check app/static/app.js
git diff --check
```

Expected: all exit `0`.

- [x] **Step 2: Run targeted and full tests**

```bash
.venv/bin/pytest -q tests/test_agent_workbench.py tests/test_state_store.py tests/test_remote_nodes.py
.venv/bin/pytest -q
```

Expected: zero failures.

- [x] **Step 3: Update docs after tests pass**

Document scan/import, target matrix, incremental apply, backup, readback verification, Windows unsupported state, and live truth sources.

- [x] **Step 4: Restart and verify service**

```bash
sudo systemctl restart wsl-ops-panel.service
systemctl is-active wsl-ops-panel.service
curl -fsS http://127.0.0.1:8328/healthz
```

Expected: `active` and `{"status":"ok"}`.

- [x] **Step 5: Browser acceptance**

Verify `/categories/agent` at 1600px, 1280px, and 390px:

- equal-size device/client buttons;
- no visible checkbox;
- truthful local MCP names/status;
- install matrix target selection;
- no horizontal overflow;
- no console errors, failed requests, or `>=400` responses during normal flow.

## Completion record

- 2026-07-13：方案 B 已落地，移动端和桌面端范围按钮统一等宽。
- 静态检查、定向测试、完整测试、服务健康检查和 1600px / 1280px / 390px 浏览器验收均通过。
