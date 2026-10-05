# Agent MCP Compact Rows and Client-Specific Uninstall Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make MCP rows compact and add explicit per-device, per-client uninstall with backup, atomic writes, format validation, and readback verification.

**Architecture:** Extend the existing canonical MCP adapters with verified removal and generate the same removal behavior for queued local/SSH tasks. Reuse the target matrix in install/uninstall modes and aggregate truthful observations by device before rendering compact status summaries.

**Tech Stack:** FastAPI, Pydantic, SQLite, Jinja2, vanilla JavaScript/CSS, pytest, Ruff, Playwright acceptance.

---

## File map

- Modify `app/services/agent_mcp_adapters.py`: validate and read back client-specific removal.
- Modify `app/services/agent_mcp.py`: generate verified uninstall shell for queued targets.
- Modify `app/services/state_store.py`: remove only selected desired assignments.
- Modify `app/api/agent.py`: add `/mcp/uninstall`, target checks, queue grouping.
- Modify `app/services/agent_workbench.py`: expose observations grouped by device for compact rendering.
- Modify `app/templates/agent_category.html`: compact rows, uninstall button, dual-mode matrix.
- Modify `app/static/app.js`: install/uninstall mode, preview and result messages.
- Modify `app/static/app.css`: compact row geometry and responsive layout.
- Modify `tests/test_agent_workbench.py` and `tests/test_state_store.py`: behavior and render contracts.
- Modify `README.md` and `docs/operations.md`: distinguish uninstall from local-library deletion.

### Task 1: Verified client-specific removal

- [x] Add failing adapter tests for Codex, Claude, Gemini, OpenCode, OpenClaw, and Hermes proving that explicit IDs are removed while unrelated MCPs and top-level settings remain.
- [x] Run `.venv/bin/pytest -q tests/test_agent_workbench.py -k 'remove_mcp'` and confirm failures describe missing verification/behavior.
- [x] Update `remove_mcp_from_home()` to validate rendered TOML/JSON/YAML, atomically write, and fail readback when a requested ID remains.
- [x] Add `build_mcp_remove_shell(server_ids, apps, windows=False)` with the same client paths/table keys, backup, atomic replacement, parse validation, and readback checks used by adapters.
- [x] Run the targeted adapter/shell tests and confirm green.

### Task 2: Assignment-safe uninstall API

- [x] Add failing state-store test for removing selected MCP assignments without replacing unrelated assignments.
- [x] Add failing API tests for local uninstall queueing, offline/Windows skipping, unobserved target skipping, and per-client task grouping.
- [x] Implement `remove_mcp_assignments(node_id, client_id, mcp_ids)` in `PanelStateStore`.
- [x] Implement `POST /api/agent/mcp/uninstall` using `AgentMcpMatrixRequest`; only observed installed targets queue, each node receives one task, and assignments are removed only for queued targets.
- [x] Record queued uninstall operations without raw specs or credentials.
- [x] Run `.venv/bin/pytest -q tests/test_state_store.py tests/test_agent_workbench.py -k 'remove_mcp_assignment or mcp_uninstall'` and confirm green.

### Task 3: Compact truthful rows

- [x] Add failing render tests requiring `data-agent-mcp-uninstall-one`, grouped device summaries, a separate local-library delete menu, and no repeated full status badge per client.
- [x] Add grouped observation data in `build_agent_workbench_context()` or render grouping directly from `observations_by_target` without changing truth sources.
- [x] Rewrite MCP row markup into main identity, grouped status, actions, optional metadata, and folded JSON.
- [x] Add scoped CSS so desktop rows use a compact single-line grid, badges remain one line, actions do not move, and 390px falls back to three tidy rows.
- [x] Run targeted render tests and confirm green.

### Task 4: Install/uninstall matrix modes

- [x] Add failing UI contract tests for dialog mode labels, uninstall buttons, and per-cell installed MCP metadata.
- [x] Add matrix cell `data-installed-mcp-ids` from real observations.
- [x] Refactor `openMcpMatrix(serverIds, mode)`; uninstall mode disables cells unless every selected MCP is installed there.
- [x] Route preview text and confirm action to `/apply` or `/uninstall`, with `确认安装` / `确认卸载` and target-specific skipped reasons.
- [x] Run targeted UI/API tests and `node --check app/static/app.js`.

### Task 5: Docs and verification

- [x] Run Ruff, compileall, JavaScript syntax, frontend tests, `git diff --check`, targeted pytest, then full pytest.
- [x] Update README and operations docs after tests pass.
- [x] Restart `wsl-ops-panel.service`; verify local and Tailscale `/healthz`.
- [x] Browser-test 1600px, 1280px, and 390px: compact rows, install preview, uninstall preview, no overflow, no console/network errors.
- [x] Mark this plan complete without committing or overwriting unrelated workspace changes.
