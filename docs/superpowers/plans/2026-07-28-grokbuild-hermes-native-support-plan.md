# Grok Build and Hermes Native Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add truthful, native Grok Build and Hermes Provider/MCP/Skill/Prompt support, plus verified Grok Build route takeover.

**Architecture:** Extend the existing client registry and filesystem adapters. Grok Build uses TOML section-aware conversion and Hermes uses YAML merge-on-write; both retain client-owned fields, back up before writes, atomically replace files, and verify by parsing and reading back.

**Tech Stack:** Python 3.13, FastAPI, PyYAML, `tomllib`, SQLite, pytest, Node test runner.

---

## File map

- `app/services/agent_clients.py`: enable Grok Build write capabilities.
- `app/services/agent_mcp_adapters.py`: local Grok TOML and Hermes merge adapters.
- `app/services/agent_mcp.py`: queued/import/remote script support for Grok and Hermes merge behavior.
- `app/services/agent_providers.py`: Grok provider import/write and Hermes native provider import/write.
- `app/services/agent_provider_profiles.py`: Grok and Hermes legacy profile extraction.
- `app/services/agent_route_takeover.py`: Grok route takeover and field-level restore.
- `tests/test_agent_clients.py`: capability and truthful detection tests.
- `tests/test_agent_workbench.py`: MCP, Provider, local API, Skill, Prompt integration tests.
- `tests/test_agent_provider_profiles.py`: normalized Grok/Hermes profiles.
- `tests/test_agent_route_takeover.py`: Grok takeover/restore tests.
- `docs/operations.md`, `agent-audit-20260713-130739.md`: operations and verified outcome.

### Task 1: Capability declaration

- [x] Add a failing test asserting Grok Build write support is `providers`, `route`, `mcp`, `skills`, `prompts` and Hermes exposes `providers`, `mcp`, `skills`, `prompts`.
- [x] Run `.venv/bin/pytest tests/test_agent_clients.py -q` and confirm the Grok assertion fails.
- [x] Update `app/services/agent_clients.py` with the declared capabilities.
- [x] Re-run the focused test and confirm it passes.

### Task 2: Native MCP adapters

- [x] Add failing tests for Grok scan/apply/remove, `type` stripping, header mapping, unrelated TOML preservation, and Hermes extra-field preservation across apply/remove.
- [x] Run the focused tests and confirm missing Grok support plus Hermes field loss.
- [x] Add `grokbuild` to `CLIENT_PATHS`; implement Grok canonical conversion and TOML rendering.
- [x] Change Hermes apply to merge each selected core spec into its existing native entry while retaining extra fields.
- [x] Re-run focused tests.

### Task 3: Import and queued MCP scripts

- [x] Add failing tests for Grok local import, apply shell, remove shell, and scan shell output.
- [x] Run focused tests and confirm Grok is omitted.
- [x] Add Grok to supported app sets and every import/apply/remove/scan branch.
- [x] Make the generated Hermes apply script preserve extra per-server fields.
- [x] Re-run focused tests.

### Task 4: Native Provider support

- [x] Add failing tests that import a Grok `[models]` profile, preserve official-mode TOML, import Hermes `custom_providers`, and update one Hermes provider without replacing siblings or unknown fields.
- [x] Run focused tests and confirm Grok is absent and Hermes imports only one whole-file snapshot.
- [x] Implement Grok and Hermes readers with one imported record per native provider where applicable.
- [x] Extend the apply script with Grok section-aware TOML updates and Hermes provider-list/model-default updates.
- [x] Extend legacy profile normalization for Grok snapshots and nested Hermes records.
- [x] Re-run focused tests.

### Task 5: Grok route takeover

- [x] Add failing tests for Grok custom-model takeover, exact restore, external-edit field restore, and official-mode rejection before mutation.
- [x] Run the focused tests and confirm Grok is reported unsupported.
- [x] Implement Grok path, TOML validation, selected-profile field updates, and field ownership restore.
- [x] Re-run focused tests.

### Task 6: Skill, Prompt, API and UI fixture integration

- [x] Add temporary-Home tests proving detected Grok/Hermes appear and their declared Skill/Prompt operations write only client-specific paths.
- [x] Add local MCP API tests for both clients.
- [x] Run focused backend and frontend tests.
- [x] Make only capability-filtering adjustments shown necessary by failing tests.

### Task 7: Verification and docs

- [x] Run `.venv/bin/pytest -q`.
- [x] Run `.venv/bin/ruff check app tests`, `.venv/bin/python -m compileall -q app tests`, `node --check app/static/app.js`, and all `tests/frontend/*.test.cjs`.
- [x] Update `docs/operations.md` and `agent-audit-20260713-130739.md` with current evidence.
- [x] Restart the panel and Router processes through their installed service path or current launch path, then probe ports 8328 and 7888.
- [x] Use the browser against `http://127.0.0.1:8328/categories/agent` to verify real-client visibility, tab switching, console errors, failed requests, and responsive layout.
- [x] Run `git diff --check` and record the final test totals.
