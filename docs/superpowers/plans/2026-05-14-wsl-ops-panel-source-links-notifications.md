# WSL Ops Panel Source Links and Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix SearXNG display, add source links, add select-all controls, and replace the broken WeChat notification action with editable per-project notifications.

**Architecture:** Keep registry and scanner behavior intact, enrich metadata through a focused `source_links` service, and add a separate `notifications` service/API. Bulk notify remains queued through `ActionPlan`, but the command calls the panel notification CLI instead of restarting `startup-notify.service`.

**Tech Stack:** FastAPI, Pydantic, Jinja2, vanilla JS, pytest, ruff, YAML config.

---

## Files

- Create `app/services/source_links.py`: pure helpers for GitHub/Docker/npm/PyPI/homepage links.
- Create `app/services/notifications.py`: config load/save, template render, PushPlus send, CLI entrypoint.
- Create `app/api/notifications.py`: authenticated JSON endpoints for detail page save/preview/send.
- Create `config/objects/docker-searxng.yaml`: registered Docker object.
- Create `config/notifications/projects.yaml`: notification defaults and PushPlus token placeholder.
- Modify `app/main.py`: include notification router and state service.
- Modify `app/services/assets.py`: attach source links to Docker assets.
- Modify `app/scanners/docker_scanner.py`: parse OCI labels from docker output.
- Modify `app/scanners/project_scanner.py`: attach source links from git and package.json.
- Modify `app/scanners/node_scanner.py`: attach npm source link.
- Modify `app/scanners/python_scanner.py`: attach PyPI source link.
- Modify `app/adapters/docker_adapter.py`: `notify_send` calls notification CLI.
- Modify `app/api/assets.py`: pass config root and asset JSON into Docker adapter.
- Modify `app/templates/category.html`: source summary, select-all buttons.
- Modify `app/templates/asset_detail.html`: source link rendering and notification editor.
- Modify `app/static/app.js`: select all / clear selection and notification editor behavior.
- Modify `app/static/app.css`: compact source links and notification form styles.
- Add tests in `tests/test_source_links.py`, `tests/test_notifications.py`; extend route/scanner/adapter tests.

## Tasks

### Task 1: Source link service and scanner metadata

- [ ] Write failing tests for Docker OCI source labels, Docker repository URL generation, npm URL, PyPI URL, and Project package.json repository/homepage.
- [ ] Run targeted tests and confirm they fail because `source_links` is missing.
- [ ] Implement `app/services/source_links.py` and wire Node/Python/Project/Docker metadata.
- [ ] Run targeted tests and confirm they pass.

### Task 2: SearXNG registered object

- [ ] Add route/scanner test proving Docker category shows `SearXNG` and hides `docker__searxng-mcp` when the runtime working dir is registered.
- [ ] Run targeted test and confirm it fails before object exists.
- [ ] Add `config/objects/docker-searxng.yaml`.
- [ ] Run targeted test and confirm it passes.

### Task 3: Category select all and clear selection

- [ ] Add route contract test for `data-select-all-assets` and `data-clear-selection`.
- [ ] Run targeted test and confirm it fails.
- [ ] Update `category.html` and `app.js`.
- [ ] Run targeted test and confirm it passes.

### Task 4: Notification service and API

- [ ] Add tests for default render, save config, send command plan, and API save/send contract.
- [ ] Run targeted tests and confirm they fail.
- [ ] Implement notification service, API router, and app wiring.
- [ ] Run targeted tests and confirm they pass.

### Task 5: UI integration

- [ ] Add route tests for source links and notification editor on detail page.
- [ ] Run targeted tests and confirm failures.
- [ ] Update templates, JS, CSS.
- [ ] Run targeted tests and confirm they pass.

### Task 6: Verification and deploy

- [ ] Run `./.venv/bin/ruff check app tests`.
- [ ] Run `./.venv/bin/pytest -q`.
- [ ] Restart `wsl-ops-panel.service`.
- [ ] Check `http://127.0.0.1:8328/healthz` and `http://100.126.43.55:8328/healthz`.
- [ ] Commit and push `phase1-complete`.
