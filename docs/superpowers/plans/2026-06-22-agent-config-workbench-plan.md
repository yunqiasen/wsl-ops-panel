# Agent Config Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first usable Agent configuration workbench for MCP, Prompts, and Skills.

**Architecture:** Add focused Agent service modules and a dedicated Agent category template. All risky writes are converted into queued shell/Python tasks with backups.

**Tech Stack:** FastAPI, Jinja2 templates, existing task queue, pytest, vanilla JS.

---

### Task 1: Agent service models

**Files:**
- Create: `app/services/agent_clients.py`
- Create: `app/services/agent_mcp.py`
- Create: `app/services/agent_prompts.py`
- Create: `app/services/agent_skills.py`
- Test: `tests/test_agent_workbench.py`

- [ ] Write failing tests for client definitions, MCP import, prompt command, and skill scan.
- [ ] Implement minimal services.
- [ ] Run `pytest tests/test_agent_workbench.py -q`.

### Task 2: Agent API

**Files:**
- Create: `app/api/agent.py`
- Modify: `app/main.py`
- Test: `tests/test_agent_workbench.py`

- [ ] Write failing tests for import-local and queue endpoints.
- [ ] Implement API endpoints.
- [ ] Run targeted tests.

### Task 3: Agent UI

**Files:**
- Create: `app/templates/agent_category.html`
- Modify: `app/api/overview.py`
- Modify: `app/static/app.js`
- Modify: `app/static/app.css`
- Test: `tests/test_agent_workbench.py`

- [ ] Write failing page render test.
- [ ] Render Agent workbench instead of generic cards.
- [ ] Add small JS for checkbox JSON payloads.
- [ ] Run page tests.

### Task 4: Verification

- [ ] Run `ruff check` on changed Python files.
- [ ] Run `node --check app/static/app.js`.
- [ ] Run `pytest -q`.
- [ ] Restart `wsl-ops-panel.service`.
- [ ] Verify `/categories/agent` contains Agent Workbench, MCP, Prompts, Skills.
