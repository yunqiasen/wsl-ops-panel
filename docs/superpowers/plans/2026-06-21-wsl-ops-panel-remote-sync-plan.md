# WSL Ops Panel Remote Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first usable slice of category cleanup, Node/Python package descriptions, and SSH node management.

**Architecture:** Keep systemd as an internal capability source, not a visible category. Add a remote category backed by YAML node storage and a small SSH execution service. Fetch package metadata only for detail pages and cache it in memory.

**Tech Stack:** FastAPI, Jinja2, Pydantic, YAML, system ssh, pytest.

---

### Task 1: Category cleanup

**Files:**
- Modify: `config/categories/systemd.yaml`
- Create: `config/categories/remote.yaml`
- Modify: `app/services/assets.py`
- Modify: `app/services/capabilities.py`
- Test: `tests/test_readonly_scanners.py`

- [x] Hide `systemd` from navigation by setting `enabled: false`.
- [x] Add `remote` category.
- [x] Keep internal systemd scanner support intact.
- [x] Add remote static assets for node center and config sync center.

### Task 2: Node/Python package metadata

**Files:**
- Create: `app/services/package_metadata.py`
- Modify: `app/services/assets.py`
- Modify: `app/templates/asset_detail.html`
- Test: `tests/test_package_metadata.py`
- Test: `tests/test_package_asset_routes.py`

- [x] Add metadata service for npm registry and PyPI JSON.
- [x] Cache metadata in memory.
- [x] Enrich Node/Python detail assets only.
- [x] Show intro, package page, homepage, repository and keywords.

### Task 3: SSH node center

**Files:**
- Create: `app/models/remote_nodes.py`
- Create: `app/services/remote_nodes.py`
- Create: `app/api/remote_nodes.py`
- Modify: `app/main.py`
- Create: `app/templates/remote_nodes.html`
- Create: `app/templates/remote_config_sync.html`
- Modify: `app/static/app.css`
- Test: `tests/test_remote_nodes.py`

- [x] Store nodes in `data/remote_nodes.yaml`.
- [x] Add HTML pages for node center and config sync center.
- [x] Add JSON API for list/create/delete/test.
- [x] Add form routes for browser usage.
- [x] Test SSH command construction through a fake runner.

### Task 4: Verification

**Files:**
- All changed files.

- [ ] Run focused tests.
- [ ] Run full pytest if focused tests pass.
- [ ] Restart `wsl-ops-panel.service` if tests pass.
- [ ] Check local HTTP route.
