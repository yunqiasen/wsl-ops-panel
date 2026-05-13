# WSL Ops Panel UI Performance Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild WSL Ops Panel into a clean light SaaS operations UI and remove avoidable page/button latency.

**Architecture:** Keep the existing FastAPI + Jinja architecture. Move page rendering to lighter templates, keep terminal PTY behavior intact, and make expensive version lookups lazy through the existing `/api/assets/{object_id}/versions` endpoint. Use one local JS entry that activates behavior by page markers instead of initializing terminal code globally.

**Tech Stack:** FastAPI, Jinja2 templates, vanilla CSS, vanilla JavaScript, xterm.js vendored static files, pytest.

---

## File map

- `app/templates/base.html` — global shell, local assets only, sidebar/topbar/content layout.
- `app/templates/login.html` — light SaaS login page.
- `app/templates/overview.html` — summary dashboard and category cards.
- `app/templates/category.html` — compact asset cards by category.
- `app/templates/asset_detail.html` — split detail/action layout and lazy version panel.
- `app/templates/tasks.html` — task cards/list with unified status style.
- `app/templates/logs.html` — log cards and compact code blocks.
- `app/templates/settings.html` — registry state and reload action.
- `app/templates/terminals.html` — real PTY workbench, same visual language as the app.
- `app/static/app.css` — full light SaaS visual system and terminal styles.
- `app/static/app.js` — local action handling, lazy version loading, terminal-only xterm bootstrap.
- `app/api/overview.py` — stop synchronous detail version lookup on first page render.
- `app/api/assets.py` — keep `/versions` endpoint as lazy source; optionally expose cached snapshot first.
- `tests/test_overview_routes.py` — adjust UI assertions to new class names and lazy version behavior.
- `tests/test_package_asset_routes.py` — keep API version behavior covered.
- `tests/test_debug_terminal.py`, `tests/test_system_terminal.py` — run to protect PTY behavior.

---

### Task 1: Lock behavior with tests for local-only assets and lazy detail versions

**Files:**
- Modify: `tests/test_overview_routes.py`

- [ ] **Step 1: Add assertions that the protected pages do not load remote CDN scripts**

Add these assertions inside `test_protected_pages_render_nav_and_actions` after `overview = client.get('/')`:

```python
    assert 'https://unpkg.com' not in overview.text
    assert 'data-page="overview"' in overview.text
```

Add these assertions after `detail = client.get('/assets/cpa')`:

```python
    assert 'https://unpkg.com' not in detail.text
    assert 'data-version-panel' in detail.text
    assert 'data-version-endpoint="/api/assets/cpa/versions"' in detail.text
```

- [ ] **Step 2: Replace old class assertions with new layout assertions**

Replace:

```python
    assert 'asset-grid' in category.text
    assert 'asset-card-title' in category.text
    assert 'asset-stat-grid' in category.text
    assert 'asset-path-row' in category.text
```

with:

```python
    assert 'asset-board' in category.text
    assert 'asset-card__title' in category.text
    assert 'metric-strip' in category.text
    assert 'path-chip' in category.text
```

- [ ] **Step 3: Run the focused route test and confirm it fails before implementation**

Run:

```bash
./.venv/bin/pytest -q tests/test_overview_routes.py::test_protected_pages_render_nav_and_actions
```

Expected before implementation: failure because new classes and lazy version markup are not rendered yet.

---

### Task 2: Rebuild the global shell and page templates

**Files:**
- Modify: `app/templates/base.html`
- Modify: `app/templates/login.html`
- Modify: `app/templates/overview.html`
- Modify: `app/templates/category.html`
- Modify: `app/templates/asset_detail.html`
- Modify: `app/templates/tasks.html`
- Modify: `app/templates/logs.html`
- Modify: `app/templates/settings.html`
- Modify: `app/templates/terminals.html`

- [ ] **Step 1: Replace `base.html` with a local-only app shell**

Use this structure:

```html
<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{{ title }}</title>
    <link rel="stylesheet" href="/static/app.css" />
    {% block head_extra %}{% endblock %}
    {% block script_extra %}{% endblock %}
    <script defer src="/static/app.js"></script>
  </head>
  <body data-page="{{ active_page or 'page' }}">
    <div class="app-shell">
      <aside class="sidebar" aria-label="主导航">
        <a class="brand" href="/">
          <span class="brand-mark">W</span>
          <span>
            <strong>WSL Ops</strong>
            <small>维护更新管理面板</small>
          </span>
        </a>
        <nav class="nav-stack">
          <section class="nav-group">
            <div class="nav-label">资产分类</div>
            <a class="nav-item{% if active_page == 'overview' %} is-active{% endif %}" href="/">总览</a>
            {% for category in nav_categories %}
            <a class="nav-item{% if active_page == category.id %} is-active{% endif %}" href="{{ request.url_for('category_page', category_id=category.id) }}">
              {{ category.label }}
            </a>
            {% endfor %}
          </section>
          <section class="nav-group">
            <div class="nav-label">控制中心</div>
            {% for page in fixed_pages %}
            <a class="nav-item{% if active_page == page.id %} is-active{% endif %}" href="{{ request.url_for(page.route_name) }}">
              {{ page.label }}
            </a>
            {% endfor %}
          </section>
        </nav>
      </aside>
      <main class="main-shell">
        {% block content %}{% endblock %}
      </main>
    </div>
  </body>
</html>
```

- [ ] **Step 2: Replace `overview.html` with dashboard cards**

Use summary cards, category cards, and preview list. Keep these strings present: `总览`, category labels, `任务中心`.

- [ ] **Step 3: Replace `category.html` with compact `asset-board` cards**

Each card must include:

```html
<ul class="asset-board">
  <li class="asset-card">
    <a class="asset-card__title" href="{{ request.url_for('asset_detail', object_id=asset.object_id) }}">{{ asset.name }}</a>
    <div class="metric-strip">...</div>
    <div class="path-chip">...</div>
  </li>
</ul>
```

Keep category-specific metadata, but show only the most useful fields per card.

- [ ] **Step 4: Replace `asset_detail.html` with split detail/action layout**

Do not call synchronous version lists in the template. Add lazy panel:

```html
<section class="panel version-panel" data-version-panel data-version-endpoint="/api/assets/{{ asset.object_id }}/versions">
  <div class="section-heading">
    <div>
      <h3>版本</h3>
      <p>按需加载，避免拖慢首屏。</p>
    </div>
    <button class="ghost-button" type="button" data-version-refresh>刷新版本</button>
  </div>
  <div class="version-placeholder" data-version-content>等待加载版本信息…</div>
</section>
```

Action forms still post to the same endpoints, and deploy-version form starts disabled until versions are loaded.

- [ ] **Step 5: Update remaining templates with the same class language**

Use `.page-hero`, `.panel`, `.section-heading`, `.table-list`, `.log-box`, `.terminal-workbench`. Do not introduce remote assets.

- [ ] **Step 6: Run template-focused tests**

Run:

```bash
./.venv/bin/pytest -q tests/test_overview_routes.py
```

Expected after implementation: route tests pass after assertions are updated.

---

### Task 3: Rewrite CSS as a light SaaS visual system

**Files:**
- Modify: `app/static/app.css`

- [ ] **Step 1: Replace global tokens**

Define tokens for light background, text, borders, panels, accent colors, and terminal colors:

```css
:root {
  color-scheme: light;
  --bg: #f6f8fb;
  --surface: #ffffff;
  --surface-soft: #f9fbff;
  --line: #d8e0ea;
  --line-strong: #c5d0de;
  --ink: #111827;
  --muted: #667085;
  --faint: #98a2b3;
  --accent: #2563eb;
  --accent-2: #0891b2;
  --success: #0f9f6e;
  --warning: #b7791f;
  --danger: #dc2626;
  --radius-lg: 22px;
  --radius-md: 14px;
  --radius-sm: 10px;
  --shadow-soft: 0 14px 40px rgba(15, 23, 42, 0.06);
  font-family: "Aptos", "Avenir Next", "PingFang SC", "Microsoft YaHei", sans-serif;
}
```

- [ ] **Step 2: Add layout and navigation styles**

Implement `.app-shell`, `.sidebar`, `.brand`, `.nav-stack`, `.nav-item`, `.main-shell`, `.page-hero`.

- [ ] **Step 3: Add card, button, form, status, list styles**

Implement `.panel`, `.dashboard-grid`, `.asset-board`, `.asset-card`, `.metric-strip`, `.path-chip`, `.status-pill`, `.button-row`, `.primary-button`, `.ghost-button`, `.danger-button`, `.inline-form`, `.flash`.

- [ ] **Step 4: Add terminal styles without changing PTY behavior**

Keep `.terminal-workbench`, `.terminal-main`, `.terminal-toolbar`, `.terminal-viewports`, `.terminal-pane`, `.terminal-screen`, `.terminal-sidebar`, `.terminal-tab`. Make it match the light app shell while keeping terminal screen dark.

- [ ] **Step 5: Add responsive rules**

Collapse sidebar on small screens, make cards single-column, preserve terminal usability.

---

### Task 4: Optimize JavaScript activation and lazy version loading

**Files:**
- Modify: `app/static/app.js`

- [ ] **Step 1: Keep HTMX-compatible local action fallback**

Keep `emulateHtmxRequest` and `installHtmxFallback`, but add button pending feedback:

```javascript
function setPending(element, pending) {
  if (!element) return;
  element.toggleAttribute('disabled', pending);
  if (pending) {
    element.dataset.originalText = element.textContent;
    element.textContent = '处理中…';
  } else if (element.dataset.originalText) {
    element.textContent = element.dataset.originalText;
    delete element.dataset.originalText;
  }
}
```

- [ ] **Step 2: Add lazy version panel loader**

Implement:

```javascript
async function loadVersionPanel(panel) {
  const endpoint = panel.dataset.versionEndpoint;
  const content = panel.querySelector('[data-version-content]');
  const select = document.querySelector('[data-version-select]');
  const deployButton = document.querySelector('[data-deploy-version-button]');
  if (!endpoint || !content) return;

  content.textContent = '正在加载版本信息…';
  const response = await fetch(endpoint, { headers: { Accept: 'application/json' } });
  if (redirectIfUnauthorized(response) || !response.ok) {
    content.textContent = '版本信息加载失败';
    return;
  }
  const payload = await response.json();
  renderVersionPayload(content, payload);
  if (select) {
    select.innerHTML = '';
    payload.versions.forEach((version) => {
      const option = document.createElement('option');
      option.value = version;
      option.textContent = version;
      select.append(option);
    });
  }
  if (deployButton) {
    deployButton.disabled = !payload.versions.length;
  }
}
```

- [ ] **Step 3: Make terminal bootstrap conditional**

Only run terminal code when `[data-terminal-workbench]` exists. Avoid touching xterm globals on other pages.

- [ ] **Step 4: Initialize page behaviors**

On `DOMContentLoaded`, run:

```javascript
installHtmxFallback();
document.querySelectorAll('[data-version-panel]').forEach(loadVersionPanel);
initTerminalWorkbench();
```

---

### Task 5: Stop detail pages from synchronously querying versions

**Files:**
- Modify: `app/api/overview.py`
- Modify: `app/templates/asset_detail.html`
- Optionally modify: `app/api/assets.py`

- [ ] **Step 1: Remove synchronous version calls from `asset_detail_page`**

Change:

```python
    available_versions = get_page_asset_versions(request, object_id, asset)
    version_info = get_page_asset_version_info(request, object_id, asset)
```

to no synchronous lookup. Pass only:

```python
        asset=asset,
```

- [ ] **Step 2: Remove unused imports**

Remove `get_page_asset_version_info` and `get_page_asset_versions` imports from `app/api/overview.py` if no longer used.

- [ ] **Step 3: Keep `/api/assets/{object_id}/versions` as the source of truth**

Do not remove the endpoint. Existing API tests must still pass.

- [ ] **Step 4: Run focused tests**

Run:

```bash
./.venv/bin/pytest -q tests/test_overview_routes.py tests/test_package_asset_routes.py
```

Expected: all pass after tests reflect lazy UI.

---

### Task 6: Full verification and service smoke check

**Files:**
- Verify only.

- [ ] **Step 1: Run lint**

```bash
./.venv/bin/ruff check app tests
```

Expected: no lint errors.

- [ ] **Step 2: Run full test suite**

```bash
./.venv/bin/pytest -q
```

Expected: all tests pass.

- [ ] **Step 3: Restart and verify service**

```bash
systemctl --user restart wsl-ops-panel.service || sudo systemctl restart wsl-ops-panel.service
curl -sS http://127.0.0.1:8328/healthz
curl -sS http://100.126.43.55:8328/healthz
```

Expected:

```json
{"status":"ok"}
```

- [ ] **Step 4: Verify no remote frontend dependency remains**

```bash
rg -n "https://|unpkg|cdn" app/templates app/static
```

Expected: no match for external frontend runtime dependencies.

---

## Self-review

- Spec coverage: visual redesign, local-only loading, terminal preservation, lazy version panel, button feedback, and tests are covered.
- Placeholder scan: no placeholder implementation steps remain.
- Type consistency: all endpoints and data fields already exist in `AssetVersionsResponse`; JS consumes the current JSON shape.

