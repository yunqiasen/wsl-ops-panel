# WSL Ops Panel Docker Bulk Discovery Capabilities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add category-level multi-select operations, runtime Docker discovery, a Project category, and first-pass CF/autostart/notify capability visibility/actions.

**Architecture:** Keep the existing FastAPI + Jinja + vanilla JS stack. Extend `AssetSnapshot.metadata` instead of introducing a new database migration in this pass. Use one new bulk API router that decomposes bulk actions into the existing serial task queue. Keep destructive operations queued and visible in the existing system terminal logs.

**Tech Stack:** FastAPI, Pydantic, Jinja2, vanilla JavaScript, vanilla CSS, Docker CLI, systemd CLI, pytest.

---

## File map

- `app/models/assets.py` — keep current model; capability/source data lives in `metadata`.
- `app/scanners/docker_scanner.py` — include stopped containers and keep labels/ports/runtime fields parsed.
- `app/scanners/project_scanner.py` — new shallow scanner for `/home/div/1_Project_dir` non-Docker projects.
- `app/services/assets.py` — merge registered Docker assets with runtime-discovered Docker assets; add Project category.
- `app/adapters/docker_adapter.py` — add start/stop/autostart/cf/notify action planning.
- `app/api/assets.py` — expose reusable action enqueue helper and support runtime-discovered Docker assets.
- `app/api/bulk_actions.py` — new bulk API that enqueues one task per selected asset.
- `app/main.py` — register bulk router and project scanner dependency.
- `config/categories/project.yaml` — new Project category.
- `app/templates/category.html` — add selection circles, inline version select, bulk toolbar.
- `app/templates/asset_detail.html` — remove mutation controls; show capabilities/repo/link info.
- `app/static/app.js` — selection state, lazy card version loading, bulk request submission.
- `app/static/app.css` — polished multi-select/toolbar/capability styling.
- `tests/test_docker_scanner.py` — Docker runtime discovery tests.
- `tests/test_project_scanner.py` — Project scanner tests.
- `tests/test_bulk_actions.py` — bulk queue tests.
- `tests/test_overview_routes.py` — UI contract tests for category/detail pages.

---

### Task 1: Tests for Docker runtime discovery

**Files:**
- Modify: `tests/test_docker_scanner.py`

- [ ] Add a test that registered Docker assets and runtime-discovered Docker assets are both returned.
- [ ] Assert discovered assets include `metadata.discovery_source == "runtime_discovered"`, `git_remote_url`, `image_repository`, and partial supported actions.
- [ ] Run `./.venv/bin/pytest -q tests/test_docker_scanner.py::test_build_docker_asset_snapshots_adds_runtime_discovered_projects -q` and verify it fails before implementation.

### Task 2: Implement Docker runtime discovery

**Files:**
- Modify: `app/scanners/docker_scanner.py`
- Modify: `app/services/assets.py`
- Modify: `app/api/assets.py`

- [ ] Change Docker scan to use `docker ps -a --format '{{json .}}'`.
- [ ] Add unregistered compose groups to Docker asset snapshots.
- [ ] Infer compose file, primary service, image repository, Git remote, branch, HEAD sha, and capabilities into metadata.
- [ ] Allow `_get_asset()` and `_build_adapter()` to operate on runtime-discovered Docker assets.
- [ ] Run focused Docker scanner tests until green.

### Task 3: Tests and implementation for Project category

**Files:**
- Create: `tests/test_project_scanner.py`
- Create: `app/scanners/project_scanner.py`
- Modify: `app/services/assets.py`
- Modify: `app/main.py`
- Create: `config/categories/project.yaml`

- [ ] Write tests for detecting Git/Node/Python projects and skipping Docker compose directories.
- [ ] Implement shallow scanner rooted at `/home/div/1_Project_dir`.
- [ ] Add Project category to AssetService and app dependency injection.
- [ ] Run `./.venv/bin/pytest -q tests/test_project_scanner.py` until green.

### Task 4: Tests and implementation for bulk action API

**Files:**
- Create: `tests/test_bulk_actions.py`
- Create: `app/api/bulk_actions.py`
- Modify: `app/api/assets.py`
- Modify: `app/main.py`
- Modify: `app/adapters/docker_adapter.py`

- [ ] Write tests for bulk update and bulk deploy-version queueing two independent tasks.
- [ ] Expose `enqueue_asset_action()` from `app/api/assets.py`.
- [ ] Add bulk router routes for update/deploy/delete/full-delete/start/stop/autostart/cf/notify.
- [ ] Extend Docker adapter action planning for start/stop/autostart/cf/notify.
- [ ] Run `./.venv/bin/pytest -q tests/test_bulk_actions.py` until green.

### Task 5: Tests and implementation for category multi-select UI

**Files:**
- Modify: `tests/test_overview_routes.py`
- Modify: `app/templates/category.html`
- Modify: `app/templates/asset_detail.html`
- Modify: `app/static/app.js`
- Modify: `app/static/app.css`

- [ ] Assert category page has `data-bulk-toolbar`, card selection buttons, inline version selector containers, and bulk action buttons.
- [ ] Assert detail page no longer renders mutation action panel but does render capability/repo sections.
- [ ] Implement polished multi-select toolbar and card controls.
- [ ] Implement JS selection, version loading, and bulk submission.
- [ ] Run `./.venv/bin/pytest -q tests/test_overview_routes.py` until green.

### Task 6: Full verification and service restart

**Files:**
- No planned code changes.

- [ ] Run `./.venv/bin/ruff check app tests`.
- [ ] Run `./.venv/bin/pytest -q`.
- [ ] Restart service with `sudo systemctl restart wsl-ops-panel.service`.
- [ ] Verify `curl -s http://127.0.0.1:8328/healthz` returns `{"status":"ok"}`.
- [ ] Verify Tailscale health endpoint if available.

---

## Self-review

Spec coverage:

- Bulk category operations: Task 4 and Task 5.
- Detail read-only conversion: Task 5.
- Docker automatic discovery: Task 1 and Task 2.
- Project category: Task 3.
- CF/autostart/notify first-pass capability actions: Task 2 and Task 4.
- Serial queue behavior: Task 4 reuses existing queue.

Scope boundary:

- This plan does not migrate `startup-notify.sh` into a fully data-driven template engine yet.
- This plan adds first-pass visibility/actions for existing scripts/units, then leaves full template editor as the next follow-up.
