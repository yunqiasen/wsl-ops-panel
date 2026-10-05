(() => {
  function getUnmanagedMcpIds(serverIds, managedMcpIds) {
    return (serverIds || []).filter((serverId) => serverId && !managedMcpIds.has(serverId));
  }

  function getMcpMatrixCellState({ mode, mcpIds, installedMcpIds, unavailable, defaultStatus }) {
    if (unavailable) {
      return { disabled: true, status: defaultStatus || '设备不可用' };
    }
    if (mode !== 'uninstall') {
      return { disabled: false, status: defaultStatus || '可预览' };
    }
    const installed = new Set(installedMcpIds || []);
    const selected = (mcpIds || []).filter(Boolean);
    if (selected.length && selected.every((mcpId) => installed.has(mcpId))) {
      return { disabled: false, status: `已安装 ${selected.length} 个` };
    }
    return { disabled: true, status: '并非全部已装' };
  }

  function getVisibleAgentTargetSummary(target, selectedNodes, selectedApps) {
    if (!selectedNodes.has(target.nodeId)) {
      return null;
    }
    const names = (target.clientIds || []).reduce((visible, clientId, index) => {
      if (selectedApps.has(clientId)) {
        visible.push((target.clientNames || [])[index] || clientId);
      }
      return visible;
    }, []);
    return names.length ? { count: names.length, names } : null;
  }

  function redirectIfUnauthorized(response) {
    const redirectTo = response.headers.get('HX-Redirect') || response.headers.get('X-Login-Redirect');
    if (response.status === 401 && redirectTo) {
      window.location.href = redirectTo;
      return true;
    }
    return false;
  }

  function setPending(element, pending) {
    if (!element) {
      return;
    }
    element.toggleAttribute('disabled', pending);
    element.classList.toggle('is-busy', pending);
    element.setAttribute('aria-busy', pending ? 'true' : 'false');
    if (pending) {
      if (!element.dataset.busyMinWidth && element.offsetWidth) {
        element.dataset.busyMinWidth = `${Math.ceil(element.offsetWidth)}px`;
        element.style.minWidth = element.dataset.busyMinWidth;
      }
    } else {
      element.removeAttribute('aria-busy');
      if (element.dataset.busyMinWidth) {
        delete element.dataset.busyMinWidth;
        element.style.minWidth = '';
      }
    }
  }

  function setGroupPending(elements, pending, except = null) {
    elements.forEach((item) => {
      if (item !== except) {
        item.toggleAttribute('disabled', pending);
      }
      item.classList.toggle('is-group-busy', pending);
    });
  }

  function setFlash(target, message, { success = false } = {}) {
    if (!target) {
      return;
    }
    let item = target.querySelector('.flash');
    if (!item) {
      item = document.createElement('div');
      item.className = 'flash';
      target.replaceChildren(item);
    }
    item.className = success ? 'flash flash-success' : 'flash';
    item.textContent = message;
  }

  function debounce(fn, wait = 140) {
    let timer = 0;
    return (...args) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => fn(...args), wait);
    };
  }

  function findAssetCard(assetId) {
    return Array.from(document.querySelectorAll('[data-asset-card]')).find((card) => card.dataset.assetId === assetId) || null;
  }

  function findCardVersionSelect(assetId) {
    return (
      Array.from(document.querySelectorAll('[data-card-version-select]')).find((select) => select.dataset.cardVersionSelect === assetId) || null
    );
  }

  async function postJson(endpoint, payload) {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(payload),
    });
    if (redirectIfUnauthorized(response)) {
      return null;
    }
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body.detail || '任务提交失败');
    }
    return body;
  }

  async function emulateHtmxRequest(element, event) {
    const endpoint = element.getAttribute('hx-post');
    if (!endpoint) {
      return;
    }
    event.preventDefault();

    const submitter = event.submitter || element;
    const target = document.querySelector(element.getAttribute('hx-target'));
    const headers = { 'HX-Request': 'true' };
    const options = { method: 'POST', headers };

    if (element.tagName === 'FORM') {
      options.body = new FormData(element);
    }

    setPending(submitter, true);
    try {
      const response = await fetch(endpoint, options);
      if (redirectIfUnauthorized(response)) {
        return;
      }
      const html = await response.text();
      if (target) {
        target.innerHTML = html;
      }
    } catch (error) {
      if (target) {
        target.innerHTML = `<div class="flash">请求失败：${error.message}</div>`;
      }
    } finally {
      setPending(submitter, false);
    }
  }

  function installHtmxFallback() {
    if (window.htmx) {
      return;
    }

    document.addEventListener('click', (event) => {
      const target = event.target.closest('[hx-post]');
      if (!target || target.tagName === 'FORM') {
        return;
      }
      emulateHtmxRequest(target, event);
    });

    document.addEventListener('submit', (event) => {
      const form = event.target.closest('form[hx-post]');
      if (!form) {
        return;
      }
      emulateHtmxRequest(form, event);
    });
  }

  function valueOrUnknown(value) {
    return value || 'unknown';
  }

  function versionStatusText(payload) {
    const status = payload.source_status || 'unknown';
    if (status === 'error') {
      return `版本源失败：${payload.error || '请看任务日志'}`;
    }
    if (status === 'local') {
      return payload.error || '本地构建/本地项目：无远端版本列表';
    }
    if (status === 'auth_required') {
      return payload.error || '版本仓库需要认证，已保留当前版本';
    }
    if (status === 'not_found') {
      return payload.error || '公共仓库未找到该包/镜像';
    }
    if (status === 'unsupported') {
      return `暂不支持该镜像仓库：${payload.error || ''}`.trim();
    }
    return status;
  }

  function renderVersionPayload(content, payload) {
    const runtime = payload.runtime || {};
    const versions = Array.isArray(payload.versions) ? payload.versions : [];
    const firstVersions = versions.slice(0, 8).join(', ') || versionStatusText(payload);
    content.className = 'version-grid';
    content.innerHTML = `
      <div><small>当前可部署版本</small><strong>${valueOrUnknown(payload.current_version)}</strong></div>
      <div><small>最新可部署版本</small><strong>${valueOrUnknown(payload.latest_version)}</strong></div>
      <div><small>版本源状态</small><strong>${valueOrUnknown(payload.source_status)}</strong></div>
      <div><small>版本来源</small><strong>${valueOrUnknown(payload.version_source)}</strong></div>
      <div><small>生命周期策略</small><strong>${valueOrUnknown(payload.lifecycle_strategy)}</strong></div>
      <div><small>运行镜像 Tag</small><strong>${valueOrUnknown(runtime.image_tag)}</strong></div>
      <div><small>OCI Version</small><strong>${valueOrUnknown(runtime.oci_version)}</strong></div>
      <div><small>OCI Revision</small><strong>${valueOrUnknown(runtime.oci_revision)}</strong></div>
      <div class="version-span"><small>可部署版本</small><strong>${firstVersions}</strong></div>
      ${payload.error ? `<div class="version-span"><small>错误</small><strong>${payload.error}</strong></div>` : ''}
    `;
  }

  async function loadVersionPanel(panel) {
    const endpoint = panel.dataset.versionEndpoint;
    const content = panel.querySelector('[data-version-content]');
    const select = document.querySelector('[data-version-select]');
    const deployButton = document.querySelector('[data-deploy-version-button]');
    if (!endpoint || !content) {
      return;
    }

    content.className = 'version-placeholder';
    content.textContent = '正在加载版本信息…';
    try {
      const response = await fetch(endpoint, { headers: { Accept: 'application/json' } });
      if (redirectIfUnauthorized(response) || !response.ok) {
        content.textContent = '版本信息加载失败';
        return;
      }
      const payload = await response.json();
      renderVersionPayload(content, payload);
      const versions = Array.isArray(payload.versions) ? payload.versions : [];
      if (select) {
        fillVersionSelect(select, payload);
      }
      if (deployButton) {
        deployButton.disabled = !versions.length || payload.source_status === 'error' || payload.source_status === 'unsupported';
      }
    } catch (error) {
      content.textContent = `版本信息加载失败：${error.message}`;
    }
  }

  function initVersionPanels() {
    document.querySelectorAll('[data-version-panel]').forEach((panel) => {
      loadVersionPanel(panel);
      const refresh = panel.querySelector('[data-version-refresh]');
      if (refresh) {
        refresh.addEventListener('click', () => loadVersionPanel(panel));
      }
    });
  }


  function normalizeVersionText(value) {
    return String(value || '').trim().replace(/^v/i, '');
  }

  function versionLooksDifferent(current, latest) {
    const normalizedCurrent = normalizeVersionText(current);
    const normalizedLatest = normalizeVersionText(latest);
    return Boolean(normalizedLatest && normalizedCurrent && normalizedCurrent !== normalizedLatest && normalizedCurrent !== 'latest');
  }

  function renderVersionBadge(badge, payload) {
    const label = badge.querySelector('span');
    const value = badge.querySelector('strong');
    const current = payload.current_version || badge.dataset.currentVersion || '';
    const latest = payload.latest_version || '';
    badge.classList.remove('version-alert--checking', 'version-alert--update', 'version-alert--ok', 'version-alert--error', 'version-alert--muted');
    if (payload.source_status === 'error' || payload.source_status === 'unsupported') {
      badge.classList.add('version-alert--error');
      if (label) label.textContent = '版本源失败';
      if (value) value.textContent = payload.error || payload.source_status;
      return;
    }
    if (payload.source_status === 'auth_required' || payload.source_status === 'not_found') {
      badge.classList.add('version-alert--muted');
      if (label) label.textContent = payload.source_status === 'auth_required' ? '需认证' : '未发布';
      if (value) value.textContent = versionStatusText(payload);
      return;
    }
    if (!latest) {
      badge.classList.add('version-alert--muted');
      if (label) label.textContent = '版本检查';
      if (value) value.textContent = versionStatusText(payload);
      return;
    }
    if (versionLooksDifferent(current, latest)) {
      badge.classList.add('version-alert--update');
      if (label) label.textContent = '可更新';
      if (value) value.textContent = `${current || 'unknown'} → ${latest}`;
      return;
    }
    badge.classList.add('version-alert--ok');
    if (label) label.textContent = '已是最新';
    if (value) value.textContent = latest;
  }

  function initVersionBadges() {
    const badges = Array.from(document.querySelectorAll('[data-version-badge]'));
    if (!badges.length) {
      return;
    }
    const loadBadge = async (badge) => {
      if (!badge.dataset.versionEndpoint || badge.dataset.loaded === 'true') {
        return;
      }
      badge.dataset.loaded = 'true';
      try {
        const response = await fetch(badge.dataset.versionEndpoint, { headers: { Accept: 'application/json' } });
        if (redirectIfUnauthorized(response) || !response.ok) {
          throw new Error('版本接口失败');
        }
        renderVersionBadge(badge, await response.json());
      } catch (error) {
        badge.classList.remove('version-alert--checking');
        badge.classList.add('version-alert--error');
        const label = badge.querySelector('span');
        const value = badge.querySelector('strong');
        if (label) label.textContent = '版本检查失败';
        if (value) value.textContent = error.message;
      }
    };
    if ('IntersectionObserver' in window) {
      const observer = new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            observer.unobserve(entry.target);
            loadBadge(entry.target);
          }
        });
      }, { rootMargin: '260px' });
      badges.forEach((badge) => observer.observe(badge));
      return;
    }
    badges.slice(0, 30).forEach(loadBadge);
  }


  function fillVersionSelect(select, payload) {
    const versions = Array.isArray(payload.versions) ? payload.versions : [];
    select.innerHTML = '';
    if (!versions.length || payload.source_status === 'error' || payload.source_status === 'unsupported') {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = versionStatusText(payload);
      option.disabled = true;
      select.append(option);
      select.disabled = true;
      return;
    }
    versions.forEach((version) => {
      const option = document.createElement('option');
      option.value = version;
      option.textContent = version;
      select.append(option);
    });
    select.disabled = false;
  }

  async function populateVersionSelect(select) {
    if (!select || select.dataset.loaded === 'true') {
      return;
    }
    const endpoint = select.dataset.versionEndpoint;
    if (!endpoint) {
      return;
    }
    select.disabled = true;
    select.innerHTML = '<option value="">版本加载中…</option>';
    try {
      const response = await fetch(endpoint, { headers: { Accept: 'application/json' } });
      if (redirectIfUnauthorized(response) || !response.ok) {
        select.innerHTML = '<option value="">版本加载失败</option>';
        return;
      }
      const payload = await response.json();
      fillVersionSelect(select, payload);
      select.dataset.loaded = 'true';
    } catch (error) {
      select.innerHTML = `<option value="">${error.message}</option>`;
    }
  }

  function nextRuntimeFilter(current, requested) {
    return current === requested ? '' : requested;
  }

  function matchesAssetFilter(haystack, runtimeState, query, runtimeFilter) {
    const normalizedQuery = String(query || '').trim().toLowerCase();
    const matchesQuery = !normalizedQuery || String(haystack || '').toLowerCase().includes(normalizedQuery);
    const matchesRuntime = !runtimeFilter || runtimeState === runtimeFilter;
    return matchesQuery && matchesRuntime;
  }

  function initAssetSearch() {
    const input = document.querySelector('[data-asset-search]');
    const count = document.querySelector('[data-asset-search-count]');
    const cards = Array.from(document.querySelectorAll('[data-asset-card]'));
    const runtimeButtons = Array.from(document.querySelectorAll('[data-runtime-filter]'));
    if (!input) {
      return;
    }
    let activeRuntimeFilter = '';

    function applyFilter() {
      let visible = 0;
      cards.forEach((card) => {
        const haystack = `${card.dataset.assetSearchText || ''} ${card.textContent || ''}`;
        const matched = matchesAssetFilter(
          haystack,
          card.dataset.runtimeState || 'unknown',
          input.value,
          activeRuntimeFilter,
        );
        card.hidden = !matched;
        if (matched) {
          visible += 1;
        }
      });
      runtimeButtons.forEach((button) => {
        const active = button.dataset.runtimeFilter === activeRuntimeFilter;
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-pressed', String(active));
      });
      if (count) {
        count.textContent = `${visible} 个匹配`;
      }
    }

    runtimeButtons.forEach((button) => {
      button.addEventListener('click', () => {
        activeRuntimeFilter = nextRuntimeFilter(activeRuntimeFilter, button.dataset.runtimeFilter || '');
        applyFilter();
      });
    });
    input.addEventListener('input', debounce(applyFilter, 120));
    applyFilter();
  }

  function initBulkSelection() {
    const board = document.querySelector('[data-asset-board]');
    const toolbar = document.querySelector('[data-bulk-toolbar]');
    if (!board || !toolbar) {
      return;
    }
    const selected = new Set();
    const count = toolbar.querySelector('[data-selected-count]');
    const result = toolbar.querySelector('[data-bulk-result]');
    const categoryId = toolbar.dataset.categoryId || '';
    const remoteTargetPanel = toolbar.querySelector('[data-remote-target-panel]');

    function selectedRemoteTargets() {
      if (!remoteTargetPanel) {
        return ['__local__'];
      }
      return Array.from(remoteTargetPanel.querySelectorAll('[data-remote-target]:checked:not(:disabled)')).map((item) => item.value);
    }

    function assetNamesForIds(assetIds) {
      return assetIds.map((assetId) => findAssetCard(assetId)?.dataset.assetName || '').filter(Boolean);
    }

    function packageActionForBulk(action) {
      if (action === 'update-latest' || action === 'deploy-version') {
        return 'install_or_update';
      }
      if (action === 'delete') {
        return 'delete';
      }
      return null;
    }

    async function postJson(endpoint, payload) {
      const response = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(payload),
      });
      if (redirectIfUnauthorized(response)) {
        return null;
      }
      const body = await response.json();
      if (!response.ok) {
        throw new Error(body.detail || '任务提交失败');
      }
      return body;
    }

    function assetIdsForSelection() {
      return Array.from(selected);
    }

    function actionSlugToName(slug) {
      return {
        'update-latest': 'update_latest',
        'deploy-version': 'deploy_version',
        'full-delete': 'full_delete',
        'autostart-enable': 'autostart_enable',
        'autostart-disable': 'autostart_disable',
        'cf-create': 'cf_create',
        'cf-refresh': 'cf_refresh',
        'cf-disable': 'cf_disable',
        'notify-send': 'notify_send',
        start: 'start',
        stop: 'stop',
        restart: 'restart',
        delete: 'delete',
      }[slug] || slug;
    }

    function syncUI() {
      document.querySelectorAll('[data-asset-card]').forEach((card) => {
        const assetId = card.dataset.assetId;
        const isSelected = selected.has(assetId);
        card.classList.toggle('is-selected', isSelected);
        const picker = card.querySelector('[data-card-version-panel]');
        if (picker) {
          picker.hidden = false;
          picker.classList.toggle('is-passive', !isSelected);
          const select = picker.querySelector('[data-card-version-select]');
          if (select && !isSelected && select.dataset.loaded !== 'true') {
            select.disabled = true;
            select.innerHTML = '<option value="">选中项目后加载版本</option>';
          }
          if (isSelected) {
            populateVersionSelect(select);
          }
        }
      });
      if (count) {
        count.textContent = String(selected.size);
      }
      const hint = toolbar.querySelector('[data-bulk-hint]');
      if (hint) {
        hint.textContent = selected.size
          ? `已选择 ${selected.size} 个项目。可直接点动作按钮，不支持的项目会自动跳过并提示。`
          : '点卡片或右上角圆圈选择项目。动作会拆成任务并进入串行队列。';
      }
      const selectedActions = assetIdsForSelection().map((assetId) => {
        const card = findAssetCard(assetId);
        return new Set((card?.dataset.supportedActions || '').split(',').filter(Boolean));
      });
      toolbar.querySelectorAll('[data-bulk-action]').forEach((actionButton) => {
        const action = actionSlugToName(actionButton.dataset.bulkAction);
        const hasSelection = selectedActions.length > 0;
        const supportedCount = selectedActions.filter((actions) => actions.has(action)).length;
        const label = actionButton.dataset.actionLabel || actionButton.textContent.replace(/\s*\(.*\)$/, '');
        actionButton.disabled = hasSelection && supportedCount === 0;
        actionButton.classList.toggle('needs-selection', !hasSelection);
        actionButton.classList.toggle('is-unsupported-selection', hasSelection && supportedCount === 0);
        actionButton.classList.toggle('is-partial-selection', hasSelection && supportedCount > 0 && supportedCount < selectedActions.length);
        actionButton.textContent = label;
        if (!hasSelection) {
          actionButton.title = '先选择至少一个项目';
        } else if (supportedCount === 0) {
          actionButton.title = '当前已选项目都不支持这个动作';
        } else if (supportedCount < selectedActions.length) {
          actionButton.title = `支持 ${supportedCount} 个，其他会自动跳过`;
        } else {
          actionButton.title = '会对已选项目入队';
        }
      });
    }

    toolbar.addEventListener('click', (event) => {
      if (event.target.closest('[data-select-all-assets]')) {
        event.preventDefault();
        document.querySelectorAll('[data-asset-card]').forEach((card) => {
          if (card.dataset.assetId) {
            selected.add(card.dataset.assetId);
          }
        });
        syncUI();
      }
      if (event.target.closest('[data-clear-selection]')) {
        event.preventDefault();
        selected.clear();
        syncUI();
      }
    });

    function shouldIgnoreCardToggle(target) {
      return Boolean(
        target.closest(
          'a, button, input, select, textarea, label, dialog, [data-card-version-panel], [data-notification-dialog]'
        )
      );
    }

    function toggleAssetSelection(assetId) {
      if (!assetId) {
        return;
      }
      if (selected.has(assetId)) {
        selected.delete(assetId);
      } else {
        selected.add(assetId);
      }
      syncUI();
    }

    board.addEventListener('click', (event) => {
      const button = event.target.closest('[data-asset-select]');
      const card = event.target.closest('[data-asset-card]');
      if (!button && (!card || shouldIgnoreCardToggle(event.target))) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      toggleAssetSelection(button?.dataset.assetSelect || card?.dataset.assetId);
    });

    toolbar.addEventListener('click', async (event) => {
      const agentScanButton = event.target.closest('[data-agent-remote-scan]');
      if (agentScanButton) {
        event.preventDefault();
        const assetIds = Array.from(selected);
        if (!assetIds.length) {
          setFlash(result, '先选择至少一个 Agent 客户端。');
          return;
        }
        const nodeIds = selectedRemoteTargets();
        if (!nodeIds.length) {
          setFlash(result, '至少选择一个执行设备。');
          return;
        }
        const clients = assetIds.map((assetId) => assetId.split('__').pop()).filter(Boolean);
        setPending(agentScanButton, true);
        try {
          const payload = await postJson('/api/remote/actions/agent-bulk', { agent_clients: clients, node_ids: nodeIds });
          if (!payload) return;
          setFlash(result, `Agent 扫描已入队 ${payload.queued_count} 个任务`, { success: true });
        } catch (error) {
          setFlash(result, `请求失败：${error.message}`);
        } finally {
          setPending(agentScanButton, false);
        }
        return;
      }

      const button = event.target.closest('[data-bulk-action]');
      if (!button) {
        return;
      }
      event.preventDefault();
      const action = button.dataset.bulkAction;
      const assetIds = Array.from(selected);
      if (!assetIds.length) {
        setFlash(result, '先选择至少一个项目。点项目卡片、右上角圆圈，或者点“全选”。');
        return;
      }
      const selectedActions = assetIds.map((assetId) => {
        const card = findAssetCard(assetId);
        return new Set((card?.dataset.supportedActions || '').split(',').filter(Boolean));
      });
      const actionName = actionSlugToName(action);
      const unsupported = assetIds.filter((_assetId, index) => !selectedActions[index].has(actionName));
      const supportedAssetIds = assetIds.filter((assetId) => !unsupported.includes(assetId));
      if (!supportedAssetIds.length) {
        setFlash(result, `已选项目都不支持“${button.dataset.actionLabel || action}”：${unsupported.join(', ')}`);
        return;
      }
      const versionMap = {};
      if (action === 'deploy-version') {
        for (const assetId of supportedAssetIds) {
          const select = findCardVersionSelect(assetId);
          if (!select || !select.value) {
            setFlash(result, `${assetId} 未选择版本`);
            return;
          }
          versionMap[assetId] = select.value;
        }
      }

      const actionButtons = Array.from(toolbar.querySelectorAll('[data-bulk-action]'));
      setPending(button, true);
      setGroupPending(actionButtons, true, button);
      setFlash(result, `正在提交“${button.dataset.actionLabel || action}”任务…`, { success: true });
      try {
        const targets = selectedRemoteTargets();
        const localSelected = targets.includes('__local__');
        const remoteNodeIds = targets.filter((target) => target !== '__local__');
        const packageAction = packageActionForBulk(action);
        const canRemotePackage = ['node', 'python'].includes(categoryId) && packageAction && remoteNodeIds.length > 0;
        const messages = [];
        const backendSkipped = [];
        let totalQueued = 0;

        if (!remoteTargetPanel || localSelected) {
          const payload = await postJson(`/api/bulk/actions/${action}`, { asset_ids: supportedAssetIds, version_map: versionMap });
          if (!payload) return;
          messages.push(`本机 ${payload.queued_count} 个`);
          totalQueued += payload.queued_count || 0;
          backendSkipped.push(...(payload.skipped || []));
        }

        if (canRemotePackage) {
          const versionMapByPackage = {};
          supportedAssetIds.forEach((assetId) => {
            const packageName = findAssetCard(assetId)?.dataset.assetName;
            if (packageName && versionMap[assetId]) {
              versionMapByPackage[packageName] = versionMap[assetId];
            }
          });
          const payload = await postJson('/api/remote/actions/package-bulk', {
            tool_type: categoryId,
            package_action: packageAction,
            package_names: assetNamesForIds(supportedAssetIds),
            version_map: versionMapByPackage,
            node_ids: remoteNodeIds,
          });
          if (!payload) return;
          messages.push(`远程 ${payload.queued_count} 个`);
          totalQueued += payload.queued_count || 0;
          backendSkipped.push(...(payload.skipped || []));
        }

        if (remoteTargetPanel && !localSelected && !canRemotePackage) {
          throw new Error('当前动作不支持远程设备，请勾选“当前 WSL”或换成更新/部署/删除。');
        }

        const skipped = unsupported.length ? `，已跳过不支持的 ${unsupported.length} 个：${unsupported.join(', ')}` : '';
        const backendReasons = backendSkipped.map((item) => `${item.asset_id}: ${item.reason}`).join('；');
        const detail = backendReasons ? `；跳过：${backendReasons}` : '';
        setFlash(result, `${totalQueued ? '已入队（执行结果见任务页）' : '未入队'}：${messages.join(' + ')}${skipped}${detail}`, { success: totalQueued > 0 && !backendSkipped.length });
      } catch (error) {
        setFlash(result, `请求失败：${error.message}`);
      } finally {
        setPending(button, false);
        setGroupPending(actionButtons, false, button);
        syncUI();
      }
    });
    syncUI();
  }



  async function requestJson(endpoint, options = {}) {
    const response = await fetch(endpoint, {
      ...options,
      headers: { Accept: 'application/json', ...(options.headers || {}) },
    });
    if (redirectIfUnauthorized(response)) {
      return null;
    }
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || '请求失败');
    }
    return payload;
  }

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function shellWords(value) {
    const matches = String(value || '').match(/"(?:\\.|[^"])*"|'(?:\\.|[^'])*'|\S+/g) || [];
    return matches.map((item) => item.replace(/^['"]|['"]$/g, '').trim()).filter(Boolean);
  }

  function splitPackageToken(value, toolType = '') {
    if (!value) {
      return '';
    }
    if (toolType === 'node') {
      if (value.startsWith('@')) {
        const scopeEnd = value.indexOf('/', 1);
        const versionAt = value.indexOf('@', scopeEnd > 0 ? scopeEnd + 1 : 1);
        return versionAt > 0 ? value.slice(0, versionAt) : value;
      }
      const versionAt = value.lastIndexOf('@');
      return versionAt > 0 ? value.slice(0, versionAt) : value;
    }
    const match = value.match(/^(.+?)(==|>=|<=|~=|!=|>|<).+$/);
    return (match ? match[1] : value).replace(/^#egg=/, '').trim();
  }

  function splitPackageNames(value, toolType = '') {
    const tokens = shellWords(value)
      .flatMap((item) => item.split(/[，,]+/))
      .map((item) => item.trim())
      .filter(Boolean);
    const commandWords = new Set(['npm', 'pnpm', 'yarn', 'node', 'npx', 'pip', 'pip3', 'python', 'python3', '-m', 'install', 'i', 'add', 'global', '-g', '--global', '-u', '--upgrade', 'upgrade', 'uv', 'pipx', 'brew', 'winget']);
    const valueOptions = new Set(['--registry', '--proxy', '--https-proxy', '--prefix', '--cache', '-i', '--index-url', '--extra-index-url', '--trusted-host', '--find-links', '-f']);
    const packages = [];
    for (let index = 0; index < tokens.length; index += 1) {
      const token = tokens[index];
      if (valueOptions.has(token)) {
        index += 1;
        continue;
      }
      if (commandWords.has(token.toLowerCase()) || token.startsWith('-') || ['&&', ';', '|'].includes(token)) {
        continue;
      }
      if (toolType === 'node' && /^(https?:|git\+|file:)/.test(token)) {
        packages.push(token);
        continue;
      }
      if (/^(https?:|git\+|file:)/.test(token) && !token.includes('#egg=')) {
        continue;
      }
      const packageName = splitPackageToken(token, toolType);
      if (packageName) {
        packages.push(packageName);
      }
    }
    return Array.from(new Set(packages));
  }

  function initPackageInstallPanel() {
    const panel = document.querySelector('[data-package-install-panel]');
    if (!panel) {
      return;
    }
    const toolbar = document.querySelector('[data-bulk-toolbar]');
    const result = panel.querySelector('[data-bulk-result]') || document.querySelector('[data-bulk-result]');
    const toolType = panel.dataset.toolType;
    const suggestionSelect = panel.querySelector('[data-package-suggestion-select]');
    const managerSelect = panel.querySelector('[data-package-manager-select]');
    const preview = panel.querySelector('[data-package-suggestion-preview]');
    const commandInput = panel.querySelector('[data-install-command-input]');
    const versionSelect = panel.querySelector('[data-package-version-select]');
    const installSelected = panel.querySelector('[data-package-install-selected]');
    const deleteSelected = panel.querySelector('[data-package-delete-selected]');
    const installCustom = panel.querySelector('[data-package-install-custom]');
    const refresh = panel.querySelector('[data-package-status-refresh]');
    const versionCache = new Map();
    let activePackageName = '';
    let versionRequestSeq = 0;
    let commandInputTimer = null;

    function defaultManager() {
      return toolType === 'node' ? 'npm' : 'pip';
    }

    function selectedManager() {
      return managerSelect?.value || defaultManager();
    }

    function selectedVersion() {
      return versionSelect && !versionSelect.disabled ? versionSelect.value : '';
    }

    function packageTarget(packageName, version = '') {
      if (!version) {
        return packageName;
      }
      return toolType === 'python' ? `${packageName}==${version}` : `${packageName}@${version}`;
    }

    function commandForPackages(manager, packageNames, version = '') {
      const names = packageNames.filter(Boolean);
      if (!names.length) {
        return '';
      }
      const targets = names.map((name) => packageTarget(name, version));
      const plainNames = names.join(' ');
      const targetList = targets.join(' ');
      const commands = {
        npm: `npm install -g ${targetList}`,
        pnpm: `pnpm add -g ${targetList}`,
        yarn: `yarn global add ${targetList}`,
        bun: `bun add -g ${targetList}`,
        pip: `pip install ${targetList}`,
        uv: `uv pip install ${targetList}`,
        pipx: `pipx install ${targetList}`,
        brew: `brew install ${plainNames}`,
        winget: `winget install ${plainNames}`,
      };
      return commands[manager] || `${manager} install ${targetList}`;
    }

    function commandFor(manager, packageName, version = '') {
      return commandForPackages(manager, [packageName], version);
    }

    function commandForPackageVersionMap(manager, packageNames, versionMap = {}) {
      const names = packageNames.filter(Boolean);
      if (!names.length) {
        return '';
      }
      const targets = names.map((name) => packageTarget(name, versionMap[name] || ''));
      const plainNames = names.join(' ');
      const targetList = targets.join(' ');
      const commands = {
        npm: `npm install -g ${targetList}`,
        pnpm: `pnpm add -g ${targetList}`,
        yarn: `yarn global add ${targetList}`,
        bun: `bun add -g ${targetList}`,
        pip: `pip install ${targetList}`,
        uv: `uv pip install ${targetList}`,
        pipx: `pipx install ${targetList}`,
        brew: `brew install ${plainNames}`,
        winget: `winget install ${plainNames}`,
      };
      return commands[manager] || `${manager} install ${targetList}`;
    }

    function selectedSuggestion() {
      return suggestionSelect?.selectedOptions?.[0] || null;
    }

    function packageDescription(packageName) {
      const option = Array.from(suggestionSelect?.options || []).find((item) => item.value === packageName);
      if (option?.dataset.description) {
        return option.dataset.description;
      }
      const card = Array.from(panel.querySelectorAll('[data-package-card]')).find((item) => item.dataset.packageName === packageName);
      return card?.querySelector('p')?.textContent?.trim() || '暂无简介';
    }

    function setPreview(packageName, fallback = '选择工具后这里显示简介。') {
      if (!preview) {
        return;
      }
      preview.textContent = packageName ? `${packageName}：${packageDescription(packageName)}` : fallback;
    }

    function resetVersionSelect(message = '选择工具后自动检测最新版') {
      if (!versionSelect) {
        return;
      }
      versionSelect.innerHTML = '';
      const option = document.createElement('option');
      option.value = '';
      option.textContent = message;
      versionSelect.append(option);
      versionSelect.disabled = true;
    }

    function fillVersionSelect(packageName, payload) {
      if (!versionSelect || packageName !== activePackageName) {
        return;
      }
      versionSelect.innerHTML = '';
      const latestOption = document.createElement('option');
      latestOption.value = '';
      if (payload.source_status === 'error') {
        latestOption.textContent = payload.error ? `版本检测失败，默认 latest：${payload.error}` : '版本检测失败，默认 latest';
      } else {
        latestOption.textContent = payload.latest_version ? `latest（${payload.latest_version}）` : 'latest';
      }
      versionSelect.append(latestOption);
      const seen = new Set([payload.latest_version, '']);
      (payload.versions || []).forEach((version) => {
        if (!version || seen.has(version)) {
          return;
        }
        seen.add(version);
        const option = document.createElement('option');
        option.value = version;
        option.textContent = version;
        versionSelect.append(option);
      });
      versionSelect.disabled = false;
    }

    function fillPackageVersionOptions(select, payload, { latestLabel = 'latest' } = {}) {
      select.innerHTML = '';
      const latestOption = document.createElement('option');
      latestOption.value = '';
      if (payload.source_status === 'error') {
        latestOption.textContent = payload.error ? `版本检测失败，默认 latest：${payload.error}` : '版本检测失败，默认 latest';
      } else {
        latestOption.textContent = payload.latest_version ? `${latestLabel}（${payload.latest_version}）` : latestLabel;
      }
      select.append(latestOption);
      const seen = new Set([payload.latest_version, '']);
      (payload.versions || []).forEach((version) => {
        if (!version || seen.has(version)) {
          return;
        }
        seen.add(version);
        const option = document.createElement('option');
        option.value = version;
        option.textContent = version;
        select.append(option);
      });
      select.disabled = false;
    }

    async function fetchPackageVersions(packageName) {
      if (versionCache.has(packageName)) {
        return versionCache.get(packageName);
      }
      try {
        const payload = await requestJson(`/api/packages/versions?tool_type=${encodeURIComponent(toolType)}&package_name=${encodeURIComponent(packageName)}`);
        versionCache.set(packageName, payload);
        return payload;
      } catch (error) {
        const payload = { source_status: 'error', error: error.message, versions: [], latest_version: null };
        versionCache.set(packageName, payload);
        return payload;
      }
    }

    async function loadCardVersionSelect(packageName, select) {
      if (!packageName || !select) {
        return;
      }
      select.innerHTML = '<option value="">版本检测中…</option>';
      select.disabled = true;
      const payload = await fetchPackageVersions(packageName);
      fillPackageVersionOptions(select, payload);
    }

    async function loadPackageVersions(packageName) {
      activePackageName = packageName || '';
      if (!activePackageName) {
        resetVersionSelect();
        setPreview('');
        return;
      }
      setPreview(activePackageName);
      resetVersionSelect('版本检测中…');
      const requestSeq = ++versionRequestSeq;
      const payload = await fetchPackageVersions(activePackageName);
      if (requestSeq === versionRequestSeq && versionSelect) {
        fillPackageVersionOptions(versionSelect, payload);
      }
    }


    function refreshCommandFromControls() {
      const packageName = activePackageName || selectedSuggestion()?.value || '';
      if (packageName && commandInput) {
        commandInput.value = commandFor(selectedManager(), packageName, selectedVersion());
      }
      setPreview(packageName);
    }

    function packageNamesFromCommandInput() {
      return splitPackageNames(commandInput?.value?.trim() || '', toolType);
    }

    function commandLooksLikeInstallCommand(command) {
      const first = shellWords(command)[0]?.toLowerCase() || '';
      return ['npm', 'pnpm', 'yarn', 'bun', 'pip', 'pip3', 'python', 'python3', 'uv', 'pipx', 'brew', 'winget'].includes(first);
    }

    function targets() {
      const targetPanel = panel.querySelector('[data-remote-target-panel]') || toolbar?.querySelector('[data-remote-target-panel]');
      if (!targetPanel) {
        return ['__local__'];
      }
      return Array.from(targetPanel.querySelectorAll('[data-remote-target]:checked:not(:disabled)')).map((item) => item.value);
    }

    function checkedPackages() {
      return Array.from(panel.querySelectorAll('[data-package-checkbox]:checked')).map((item) => item.value);
    }

    function targetDescriptors() {
      const targetPanel = panel.querySelector('[data-remote-target-panel]') || toolbar?.querySelector('[data-remote-target-panel]');
      if (!targetPanel) {
        return [{ id: '__local__', name: '当前 WSL', available: true }];
      }
      return Array.from(targetPanel.querySelectorAll('[data-remote-target]')).map((input) => ({
        id: input.value,
        name: input.closest('label')?.textContent?.replace(/\s+/g, ' ').trim() || input.value,
        available: !input.disabled,
      }));
    }

    function ensurePackageCard(packageName) {
      if (!packageName || Array.from(panel.querySelectorAll('[data-package-card]')).some((card) => card.dataset.packageName === packageName)) {
        return;
      }
      const grid = panel.querySelector('.package-catalog-grid');
      if (!grid) {
        return;
      }
      const article = document.createElement('article');
      article.className = 'package-catalog-card package-catalog-card--custom';
      article.dataset.packageCard = '';
      article.dataset.packageName = packageName;
      const statuses = targetDescriptors().map((target) => {
        const status = target.available ? 'unknown' : 'unavailable';
        const text = target.available ? '未检查' : '不可用';
        return `<span class="package-device-chip status-${status}" data-package-status-device="${escapeHtml(target.id)}">${escapeHtml(target.name)}：${text}</span>`;
      }).join('');
      article.innerHTML = `
        <div class="package-catalog-select">
          <label class="package-card-check"><input type="checkbox" data-package-checkbox value="${escapeHtml(packageName)}" checked /> <span class="visually-hidden">选择 ${escapeHtml(packageName)}</span></label>
          <strong class="package-card-title">${escapeHtml(packageName)}</strong>
        </div>
        <p>自定义输入添加。支持直接填包名，也支持粘贴安装命令后自动提取包名。</p>
        <div class="tag-row"><span class="tag tag-muted">自定义</span></div>
        <div class="package-device-status">${statuses}</div>
      `;
      grid.prepend(article);
    }

    function allCatalogPackages() {
      return Array.from(panel.querySelectorAll('[data-package-card]')).map((card) => card.dataset.packageName).filter(Boolean);
    }

    function selectedOrAllCatalogPackages() {
      const selected = checkedPackages();
      return selected.length ? selected : allCatalogPackages();
    }

    function cardVersionSelectFor(packageName) {
      return Array.from(panel.querySelectorAll('[data-package-card-version-select]')).find((select) => select.dataset.packageName === packageName) || null;
    }

    function versionMapFor(packageNames, action) {
      if (action !== 'install_or_update') {
        return {};
      }
      const map = {};
      packageNames.forEach((name) => {
        const select = cardVersionSelectFor(name);
        if (select && !select.disabled && select.value) {
          map[name] = select.value;
        }
      });
      if (packageNames.length === 1 && !map[packageNames[0]]) {
        const version = selectedVersion();
        if (version) {
          map[packageNames[0]] = version;
        }
      }
      return map;
    }


    async function runPackageAction(packageNames, button, options = {}) {
      const action = options.action || 'install_or_update';
      let installCommand = options.installCommand || '';
      if (!packageNames.length) {
        setFlash(result, '先选择包，或者输入自定义包名 / 安装命令。');
        return;
      }
      packageNames.forEach(ensurePackageCard);
      const nodeIds = targets();
      if (!nodeIds.length) {
        setFlash(result, '至少选择一个可用设备。红色设备不可用，需要先测速连通。');
        return;
      }
      const packageVersionMap = versionMapFor(packageNames, action);
      if (action === 'install_or_update' && !installCommand) {
        installCommand = commandForPackageVersionMap(selectedManager(), packageNames, packageVersionMap);
      }
      setPending(button, true);
      try {
        const payload = await requestJson('/api/packages/install', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            tool_type: toolType,
            package_action: action,
            package_names: packageNames,
            install_command: installCommand,
            version_map: packageVersionMap,
            node_ids: nodeIds,
          }),
        });
        if (!payload) return;
        const skipped = payload.skipped?.length ? `，跳过 ${payload.skipped.length} 个` : '';
        const label = action === 'delete' ? '删除' : '安装/更新';
        setFlash(result, `${label}任务已入队 ${payload.queued_count} 个${skipped}`, { success: true });
        window.setTimeout(() => refreshStatus(packageNames), 1800);
      } catch (error) {
        const label = action === 'delete' ? '删除' : '安装';
        setFlash(result, `${label}失败：${error.message}`);
      } finally {
        setPending(button, false);
      }
    }

    function updateStatusChip(packageName, nodeId, status, message) {
      const card = Array.from(panel.querySelectorAll('[data-package-card]')).find((item) => item.dataset.packageName === packageName);
      const chip = card?.querySelector(`[data-package-status-device="${CSS.escape(nodeId)}"]`);
      if (!chip) {
        return;
      }
      const label = chip.textContent.split('：')[0];
      const text = { installed: '已安装', missing: '未安装', unavailable: '不可用', error: '检查失败', unknown: '未知' }[status] || status;
      chip.className = `package-device-chip status-${status}`;
      chip.textContent = `${label}：${text}`;
      chip.title = message || text;
    }

    async function refreshStatus(packageNames = selectedOrAllCatalogPackages()) {
      if (!packageNames.length) {
        return;
      }
      const nodeIds = targets();
      if (!nodeIds.length) {
        setFlash(result, '至少选择一个可用设备。');
        return;
      }
      setPending(refresh, true);
      try {
        const payload = await requestJson('/api/packages/status', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ tool_type: toolType, package_names: packageNames, node_ids: nodeIds }),
        });
        if (!payload) return;
        payload.items.forEach((item) => updateStatusChip(item.package_name, item.node_id, item.status, item.message));
        setFlash(result, `已刷新 ${payload.items.length} 条安装状态`, { success: true });
      } catch (error) {
        setFlash(result, `状态检查失败：${error.message}`);
      } finally {
        setPending(refresh, false);
      }
    }

    suggestionSelect?.addEventListener('change', async () => {
      const option = suggestionSelect.selectedOptions[0];
      const packageName = option?.value || '';
      if (!packageName) {
        activePackageName = '';
        resetVersionSelect();
        refreshCommandFromControls();
        return;
      }
      if (managerSelect && option.dataset.manager) {
        managerSelect.value = option.dataset.manager;
      }
      await loadPackageVersions(packageName);
      refreshCommandFromControls();
    });
    managerSelect?.addEventListener('change', refreshCommandFromControls);
    versionSelect?.addEventListener('change', refreshCommandFromControls);
    commandInput?.addEventListener('input', () => {
      window.clearTimeout(commandInputTimer);
      commandInputTimer = window.setTimeout(() => {
        const names = packageNamesFromCommandInput();
        if (names.length === 1 && names[0] !== activePackageName) {
          loadPackageVersions(names[0]);
        } else if (!names.length) {
          activePackageName = '';
          resetVersionSelect();
          setPreview('');
        }
      }, 250);
    });
    panel.addEventListener('click', (event) => {
      const target = event.target;
      if (target.closest('a, button, input, select, textarea, label')) {
        return;
      }
      const card = target.closest('[data-package-card]');
      const detailUrl = card?.dataset.packageDetailUrl;
      if (detailUrl) {
        window.location.href = detailUrl;
      }
    });

    panel.addEventListener('change', async (event) => {
      const target = event.target;
      if (!(target instanceof HTMLInputElement) || !target.matches('[data-package-checkbox]')) {
        return;
      }
      const selected = checkedPackages();
      if (target.checked) {
        const select = target.closest('[data-package-card]')?.querySelector('[data-package-card-version-select]');
        loadCardVersionSelect(target.value, select);
      }
      if (selected.length === 1) {
        await loadPackageVersions(selected[0]);
        refreshCommandFromControls();
      } else if (selected.length > 1) {
        activePackageName = '';
        resetVersionSelect('多选时默认安装最新版');
        setPreview('', `已选择 ${selected.length} 个工具。多选更新默认安装最新版。`);
      }
    });

    panel.addEventListener('change', (event) => {
      const target = event.target;
      if (!(target instanceof HTMLSelectElement) || !target.matches('[data-package-card-version-select]')) {
        return;
      }
      const selected = checkedPackages();
      if (selected.length === 1 && selected[0] === target.dataset.packageName) {
        if (versionSelect && !versionSelect.disabled) {
          versionSelect.value = target.value;
        }
        refreshCommandFromControls();
      }
    });

    installSelected?.addEventListener('click', () => runPackageAction(checkedPackages(), installSelected));
    deleteSelected?.addEventListener('click', () => runPackageAction(checkedPackages(), deleteSelected, { action: 'delete' }));
    installCustom?.addEventListener('click', () => {
      const command = commandInput?.value?.trim() || '';
      const packageNames = packageNamesFromCommandInput();
      const installCommand = commandLooksLikeInstallCommand(command)
        ? command
        : commandForPackages(selectedManager(), packageNames, packageNames.length === 1 ? selectedVersion() : '');
      runPackageAction(packageNames, installCustom, { installCommand });
    });
    refresh?.addEventListener('click', () => refreshStatus());
  }

  function initConfigSyncPanel() {
    const panel = document.querySelector('[data-config-sync-panel]');
    if (!panel) {
      return;
    }
    const scanForm = panel.querySelector('[data-config-scan-form]');
    const applyForm = panel.querySelector('[data-config-apply-form]');
    const moduleInputs = Array.from(panel.querySelectorAll('[name="sync_kind"]'));
    const nodeInputs = Array.from(panel.querySelectorAll('[name="node_ids"]'));
    const count = panel.querySelector('[data-config-selected-count]');
    const scanSubmit = scanForm?.querySelector('button[type="submit"]');
    const applySubmit = panel.querySelector('button[form="config-apply-form"]');
    const applyNodes = applyForm?.querySelector('[data-config-apply-nodes]');
    const applyModule = panel.querySelector('[data-config-apply-module]');
    const applyContent = panel.querySelector('[data-config-apply-content]');
    const compatHint = panel.querySelector('[data-config-compat-hint]');

    function selectedModules() {
      return moduleInputs.filter((input) => input.checked).map((input) => input.value);
    }

    function selectedNodeInputs() {
      return nodeInputs.filter((input) => input.checked && !input.disabled);
    }

    function selectedNodes() {
      return selectedNodeInputs().map((input) => input.value);
    }

    function compatibleOsList() {
      const option = applyModule?.selectedOptions?.[0];
      return (option?.dataset.compatible || '')
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean);
    }

    function nodeOs(input) {
      return input.dataset.nodeOs || 'unknown';
    }

    function isNodeCompatible(input, compatible) {
      const os = nodeOs(input);
      if (!compatible.length) {
        return true;
      }
      if (os === 'unknown') {
        return !compatible.includes('windows');
      }
      return compatible.includes(os);
    }

    function refreshApplyNodes() {
      if (!applyNodes) {
        return [];
      }
      applyNodes.textContent = '';
      const compatible = compatibleOsList();
      const targets = selectedNodeInputs();
      targets.forEach((input) => {
        const hidden = document.createElement('input');
        hidden.type = 'hidden';
        hidden.name = 'node_ids';
        hidden.value = input.value;
        applyNodes.appendChild(hidden);
      });
      return targets.filter((input) => isNodeCompatible(input, compatible));
    }

    function syncApplyUi() {
      const targets = refreshApplyNodes();
      const allTargets = selectedNodeInputs();
      const skipped = Math.max(0, allTargets.length - targets.length);
      const content = applyContent?.value?.trim() || '';
      if (compatHint) {
        const compatible = compatibleOsList().join(' / ') || '自动判断';
        compatHint.textContent = allTargets.length
          ? `兼容 ${compatible}；将执行 ${targets.length} 台，自动跳过 ${skipped} 台。`
          : '先选择至少一个在线设备。';
      }
      if (applySubmit) {
        applySubmit.disabled = !allTargets.length || !content;
        applySubmit.title = !allTargets.length ? '先选择至少一个设备' : !content ? '先填写要追加的内容' : '备份后追加到兼容设备';
      }
    }

    function syncConfigUi() {
      const modules = selectedModules();
      const nodes = selectedNodes();
      if (count) {
        count.textContent = String(modules.length);
      }
      panel.querySelectorAll('[data-config-module-card]').forEach((card) => {
        const input = card.querySelector('[name="sync_kind"]');
        card.classList.toggle('is-selected', Boolean(input?.checked));
      });
      if (scanSubmit) {
        scanSubmit.disabled = !modules.length || !nodes.length;
        scanSubmit.title = !modules.length ? '先选择至少一个配置模块' : !nodes.length ? '先选择至少一个设备' : '扫描选中配置';
      }
      syncApplyUi();
    }

    function applyPreset(button) {
      const preset = button.dataset.configPreset;
      if (preset === 'clear') {
        moduleInputs.forEach((input) => { input.checked = false; });
        syncConfigUi();
        return;
      }
      if (preset === 'all') {
        moduleInputs.forEach((input) => { input.checked = true; });
        syncConfigUi();
        return;
      }
      const modules = new Set((button.dataset.configModules || '').split(',').map((item) => item.trim()).filter(Boolean));
      moduleInputs.forEach((input) => { input.checked = modules.has(input.value); });
      syncConfigUi();
    }

    panel.addEventListener('click', (event) => {
      const button = event.target.closest('[data-config-preset]');
      if (!button) {
        return;
      }
      event.preventDefault();
      applyPreset(button);
    });
    panel.addEventListener('change', syncConfigUi);
    panel.addEventListener('input', (event) => {
      if (event.target === applyContent) {
        syncApplyUi();
      }
    });
    scanForm?.addEventListener('submit', (event) => {
      syncConfigUi();
      if (!selectedModules().length || !selectedNodes().length) {
        event.preventDefault();
      }
    });
    applyForm?.addEventListener('submit', (event) => {
      syncApplyUi();
      if (!selectedNodes().length || !(applyContent?.value || '').trim()) {
        event.preventDefault();
      }
    });
    syncConfigUi();
  }


  function nextAgentClient(currentClient, requestedClient) {
    return requestedClient || currentClient || null;
  }

  // MCP 本地库只保存“定义”，安装状态来自真实客户端扫描结果。
  // 这样保存一个条目不会偷偷把它标记成已安装。
  function buildMcpSavePayload({ serverId, name, spec, description, homepage, docs, tags } = {}) {
    const payload = {
      server_id: String(serverId || '').trim(),
      name: String(name || serverId || '').trim(),
      spec: spec && typeof spec === 'object' ? spec : {},
      apps: {},
    };
    if (description !== undefined && description !== null) payload.description = String(description);
    if (homepage) payload.homepage = String(homepage);
    if (docs) payload.docs = String(docs);
    if (Array.isArray(tags) && tags.length) payload.tags = tags.map(String);
    return payload;
  }

  function moveAgentResourceId(resourceIds, resourceId, direction) {
    const ids = Array.isArray(resourceIds) ? resourceIds.map(String) : [];
    const id = String(resourceId || '');
    const index = ids.indexOf(id);
    const offset = direction === 'up' ? -1 : direction === 'down' ? 1 : 0;
    const nextIndex = index + offset;
    if (!offset || index < 0 || nextIndex < 0 || nextIndex >= ids.length) return ids;
    [ids[index], ids[nextIndex]] = [ids[nextIndex], ids[index]];
    return ids;
  }

  function buildAgentResourceOrderPayload(resourceType, resourceIds, clientId = '') {
    const payload = {
      resource_type: String(resourceType || '').trim(),
      resource_ids: (Array.isArray(resourceIds) ? resourceIds : [])
        .map((resourceId) => String(resourceId || '').trim())
        .filter(Boolean),
    };
    if (payload.resource_type === 'provider') payload.client_id = String(clientId || '').trim();
    return payload;
  }

  // Provider 路由字段统一由一个纯函数组装，尤其保留 false 值和完整端点开关。
  function buildAgentProviderRouting({
    baseUrl,
    apiFormat = 'openai_chat',
    authMode = 'bearer',
    model = '',
    modelMap = {},
    headers = {},
    apiKey = '',
    fullUrl = false,
    useOutboundProxy = true,
  } = {}) {
    const routing = {
      base_url: String(baseUrl || '').trim(),
      api_format: apiFormat || 'openai_chat',
      auth_mode: authMode || 'bearer',
      model: String(model || '').trim() || null,
      model_map: modelMap && typeof modelMap === 'object' ? modelMap : {},
      headers: headers && typeof headers === 'object' ? headers : {},
      full_url: Boolean(fullUrl),
      use_outbound_proxy: Boolean(useOutboundProxy),
    };
    if (apiKey !== undefined && apiKey !== null && String(apiKey).trim()) {
      routing.api_key = String(apiKey).trim();
    }
    return routing;
  }

  function buildAgentProviderPayload({
    appId,
    providerId,
    name,
    description,
    websiteUrl,
    form = {},
    meta = {},
  } = {}) {
    return {
      app_id: String(appId || '').trim(),
      provider_id: String(providerId || '').trim(),
      name: String(name || providerId || '').trim(),
      notes: description === undefined || description === null ? null : String(description).trim(),
      website_url: websiteUrl === undefined || websiteUrl === null ? null : String(websiteUrl).trim(),
      form: form && typeof form === 'object' ? form : {},
      meta: meta && typeof meta === 'object' ? meta : {},
    };
  }

  function getAgentProviderPrimaryAction({
    mode = 'exclusive',
    isCurrent = false,
    liveState = 'saved',
    takeover = false,
    readOnly = false,
  } = {}) {
    if (readOnly) return { action: 'activate', label: '只读', disabled: true };
    if (liveState === 'unknown') return { action: 'activate', label: '状态未知', disabled: true };
    if (mode === 'additive') {
      if (liveState === 'added') return { action: 'remove-live', label: '移除', disabled: false };
      return { action: 'activate', label: '添加', disabled: false };
    }
    if (isCurrent) return { action: 'activate', label: takeover ? '当前路由' : '当前', disabled: true };
    return { action: 'activate', label: takeover ? '切换路由' : '启用', disabled: false };
  }

  function getAgentRouterPolicy(clientButton) {
    const dataset = clientButton?.dataset || {};
    const asBoolean = (value, fallback = false) => {
      if (value === undefined || value === null || value === '') return fallback;
      return value === true || String(value).toLowerCase() === 'true';
    };
    const asInteger = (value, fallback) => {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? Math.trunc(parsed) : fallback;
    };
    return {
      configured: asBoolean(dataset.agentRouterConfigured),
      autoFailover: asBoolean(dataset.agentRouterAutoFailover),
      maxRetries: asInteger(dataset.agentRouterMaxRetries, 0),
      failureThreshold: asInteger(dataset.agentRouterFailureThreshold, 3),
      cooldownSeconds: asInteger(dataset.agentRouterCooldownSeconds, 60),
    };
  }

  function buildAgentRouterPolicyPayload({
    autoFailover = false,
    maxRetries = 0,
    failureThreshold = 3,
    cooldownSeconds = 60,
  } = {}) {
    const asInteger = (value, fallback) => {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : fallback;
    };
    return {
      auto_failover: Boolean(autoFailover),
      max_retries: asInteger(maxRetries, 0),
      failure_threshold: Math.max(1, asInteger(failureThreshold, 3)),
      cooldown_seconds: asInteger(cooldownSeconds, 60),
    };
  }

  function applyAgentRouterProfileDataset(clientButton, providerId, profile = {}) {
    if (!clientButton) return;
    clientButton.dataset.agentRouterProvider = String(providerId || '');
    clientButton.dataset.agentRouterConfigured = 'true';
    clientButton.dataset.agentRouterAutoFailover = String(Boolean(profile.auto_failover));
    clientButton.dataset.agentRouterMaxRetries = String(profile.max_retries ?? 0);
    clientButton.dataset.agentRouterFailureThreshold = String(profile.failure_threshold ?? 3);
    clientButton.dataset.agentRouterCooldownSeconds = String(profile.cooldown_seconds ?? 60);
  }

  function getAgentProviderOwnershipState(clientButton) {
    const takeover = String(clientButton?.dataset?.agentRouterTakeover || '').toLowerCase() === 'true';
    return {
      takeover,
      disabled: takeover,
      title: takeover ? 'Router 接管中，先关闭当前客户端接管' : '',
    };
  }

  function applyAgentRouterTakeoverDataset(clientButton, enabled) {
    if (!clientButton) return;
    clientButton.dataset.agentRouterTakeover = String(Boolean(enabled));
  }

  function getAgentClientStatus(clientButton) {
    const dataset = clientButton?.dataset || {};
    const count = (value) => {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : 0;
    };
    return {
      featureCount: count(dataset.agentFeatureCount),
      providerCount: count(dataset.agentProviderCount),
      skillCount: count(dataset.agentSkillCount),
      mcpPath: dataset.agentMcpPath || '无 MCP 配置',
    };
  }

  function getAgentResourceClientState(resource = {}, clientId = '', nodeId = '__local__') {
    const assignments = Array.isArray(resource.assignments) ? resource.assignments : [];
    const observations = Array.isArray(resource.observations)
      ? resource.observations
      : Object.entries(resource.observations || {}).map(([key, value]) => ({ client_id: key, ...(value || {}) }));
    const variants = Array.isArray(resource.variants) ? resource.variants : [];
    const assignment = assignments.find((item) => (
      String(item?.node_id || '__local__') === nodeId
      && String(item?.client_id || '') === clientId
      && item?.desired_enabled !== false
    ));
    const observation = observations.find((item) => (
      String(item?.node_id || '__local__') === nodeId
      && String(item?.client_id || '') === clientId
    ));
    const assigned = Boolean(assignment);
    const present = Boolean(observation?.present);
    const rawStatus = String(observation?.status || '').trim().toLowerCase();
    let status = 'uninstalled';
    if (rawStatus === 'error' || observation?.error) status = 'error';
    else if (['drift', 'drifted', 'outdated', 'hash_mismatch'].includes(rawStatus)) status = 'drifted';
    else if (present && ['installed', 'present', 'ok', 'verified'].includes(rawStatus || 'installed')) status = 'installed';
    else if (assigned || ['missing', 'absent'].includes(rawStatus)) status = 'missing';
    else if (present) status = 'installed';

    const statusMeta = {
      installed: { label: '已安装', tone: 'good' },
      drifted: { label: '漂移', tone: 'warn' },
      missing: { label: '缺失', tone: 'danger' },
      error: { label: '错误', tone: 'danger' },
      uninstalled: { label: '未安装', tone: 'muted' },
    }[status];
    const variant = variants.find((item) => (
      String(item?.client_id || '') === clientId && String(item?.platform || '') === 'linux'
    )) || variants.find((item) => (
      String(item?.client_id || '') === clientId && String(item?.platform || '') === 'any'
    )) || variants.find((item) => String(item?.client_id || '') === clientId);
    const variantClientId = variant ? clientId : 'base';
    const variantPlatform = variant ? String(variant.platform || 'any') : 'any';
    return {
      assigned,
      present,
      status,
      statusLabel: statusMeta.label,
      tone: statusMeta.tone,
      variantClientId,
      variantPlatform,
      variantLabel: variant ? `${variantClientId} / ${variantPlatform}` : '基础定义',
    };
  }

  function describeAgentReconcileResult(payload = {}) {
    if (payload?.changed === false || (!Array.isArray(payload?.operations) || payload.operations.length === 0)) {
      const warnings = Array.isArray(payload?.warnings) ? payload.warnings.filter(Boolean) : [];
      if (warnings.length) return { message: warnings[0], success: false };
      return { message: '已与数据库一致', success: true };
    }
    const operations = Array.isArray(payload.operations) ? payload.operations : [];
    const operationCount = operations.length;
    const failures = Array.isArray(payload.failures) ? payload.failures.length : 0;
    if (failures || payload.verified === false) {
      return { message: `资源操作有 ${failures || operationCount || 1} 项未通过回读`, success: false };
    }
    const actionCounts = operations.reduce((counts, operation) => {
      const action = ['install', 'update', 'uninstall'].includes(operation?.action) ? operation.action : 'install';
      counts[action] += 1;
      return counts;
    }, { install: 0, update: 0, uninstall: 0 });
    const actionEntries = [
      ['install', '安装'],
      ['update', '按数据库更新'],
      ['uninstall', '卸载'],
    ].filter(([action]) => actionCounts[action] > 0);
    if (actionEntries.length === 1) {
      const [action, label] = actionEntries[0];
      return { message: `已${label} ${actionCounts[action]} 项`, success: true };
    }
    const details = actionEntries.map(([action, label]) => `${label} ${actionCounts[action]}`).join('，');
    return { message: `已完成 ${operationCount} 项：${details}`, success: true };
  }

  function getAgentDiscoveryForClient(discovery = {}, resourceType = '', clientId = '') {
    const rows = Array.isArray(discovery?.[resourceType]) ? discovery[resourceType] : [];
    return rows.filter((item) => {
      if (resourceType !== 'mcp') return String(item?.client_id || '') === clientId;
      if (item?.observations?.[clientId]?.present) return true;
      return Boolean(item?.observations_by_target?.__local__?.[clientId]?.present);
    });
  }

  function getAgentInstalledSkillCount(library = {}, clientId = '') {
    const installedIds = new Set();
    (Array.isArray(library?.skills) ? library.skills : []).forEach((resource) => {
      if (!getAgentResourceClientState(resource, clientId).present) return;
      const id = String(resource?.id || '').trim();
      if (id) installedIds.add(id);
    });
    getAgentDiscoveryForClient(library?.discovery || {}, 'skills', clientId).forEach((resource) => {
      const id = String(resource?.id || resource?.name || '').trim();
      if (id) installedIds.add(id);
    });
    return installedIds.size;
  }

  function buildAgentProfilePayload({
    name = '',
    description = '',
    clientId = '',
    existingItems = [],
    selections = {},
    routerConfig = {},
  } = {}) {
    const items = (Array.isArray(existingItems) ? existingItems : [])
      .filter((item) => String(item?.client_id || '') !== clientId)
      .map((item, index) => ({
        client_id: String(item.client_id || ''),
        resource_type: String(item.resource_type || ''),
        resource_id: String(item.resource_id || ''),
        config: item.config && typeof item.config === 'object' ? { ...item.config } : {},
        sort_index: Number.isFinite(Number(item.sort_index)) ? Number(item.sort_index) : index,
      }));
    const orderedTypes = ['provider', 'mcp', 'skill', 'prompt', 'router'];
    orderedTypes.forEach((resourceType) => {
      const selected = Array.isArray(selections[resourceType]) ? selections[resourceType] : [];
      selected.filter(Boolean).forEach((resourceId) => {
        items.push({
          client_id: String(clientId || ''),
          resource_type: resourceType,
          resource_id: String(resourceId),
          config: resourceType === 'router' ? { ...(routerConfig || {}) } : {},
          sort_index: items.length,
        });
      });
    });
    return {
      name: String(name || '').trim(),
      description: String(description || '').trim() || null,
      items,
    };
  }

  function initAgentWorkbench() {
    const panel = document.querySelector('[data-agent-workbench]');
    if (!panel) return;

    const result = panel.querySelector('[data-agent-result]');
    const buttons = Array.from(panel.querySelectorAll('[data-agent-app]'));
    const clientById = (id) => buttons.find((button) => button.value === id);
    const activeClient = () => panel.dataset.agentActiveClient || buttons.find((button) => button.getAttribute('aria-pressed') === 'true')?.value || '';
    const clientName = (id) => clientById(id)?.dataset.agentAppName || id || '当前客户端';
    const flash = (message, options = {}) => setFlash(result, message, options);
    const reloadSoon = (delay = 520) => window.setTimeout(() => window.location.reload(), delay);
    const jsonValue = (selector, fallback = {}) => {
      try {
        const raw = panel.querySelector(selector)?.value?.trim() || '';
        return raw ? JSON.parse(raw) : fallback;
      } catch (error) {
        throw new Error(`${selector} JSON 无效：${error.message}`);
      }
    };
    const requestJson = async (url, options = {}) => {
      const response = await fetch(url, {
        headers: { Accept: 'application/json', 'Content-Type': 'application/json', ...(options.headers || {}) },
        ...options,
      });
      if (redirectIfUnauthorized(response)) return null;
      let body = {};
      try { body = await response.json(); } catch { body = {}; }
      if (!response.ok) {
        const detail = typeof body.detail === 'string' ? body.detail : body.detail?.message;
        throw new Error(detail || body.error || '请求失败');
      }
      return body;
    };
    const putJson = (url, payload) => requestJson(url, { method: 'PUT', body: JSON.stringify(payload) });
    const post = (url, payload) => postJson(url, payload);
    const localPayload = (extra = {}) => ({ client_id: activeClient(), ...extra });
    const selectedButtons = (selector) => Array.from(panel.querySelectorAll(`${selector}[aria-pressed="true"]`)).map((button) => button.value).filter(Boolean);
    const setButtonState = (button, active) => {
      button.setAttribute('aria-pressed', String(Boolean(active)));
      button.classList.toggle('is-active', Boolean(active));
      button.classList.toggle('is-on', Boolean(active));
    };
    const providerOwnershipControls = [
      panel.querySelector('[data-agent-provider-import]'),
    ].filter(Boolean);
    const syncProviderPrimaryActions = () => {
      const clientId = activeClient();
      const takeover = getAgentProviderOwnershipState(clientById(clientId)).takeover;
      panel.querySelectorAll(`[data-agent-provider-card][data-provider-app="${CSS.escape(clientId)}"]`).forEach((card) => {
        const button = card.querySelector('[data-agent-provider-activate]');
        if (!button) return;
        const isCurrent = card.classList.contains('is-current') || card.dataset.providerLiveState === 'current';
        const readOnly = card.dataset.providerReadOnly === 'true';
        const primary = getAgentProviderPrimaryAction({
          mode: card.dataset.providerMode,
          isCurrent,
          liveState: card.dataset.providerLiveState,
          takeover,
          readOnly,
        });
        button.textContent = primary.label;
        button.disabled = primary.disabled;
        if (readOnly) button.title = card.dataset.providerReadOnlyReason || '原生只读 Provider 由客户端维护';
        else if (card.dataset.providerLiveState === 'unknown') button.title = '现场配置读取失败';
        else if (isCurrent) button.title = takeover ? '当前 Router 上游已启用' : '当前 Provider 已启用';
        else button.removeAttribute('title');
      });
    };
    const syncProviderOwnershipControls = () => {
      const state = getAgentProviderOwnershipState(clientById(activeClient()));
      providerOwnershipControls.forEach((control) => {
        control.disabled = state.disabled;
        control.dataset.agentTakeoverDisabled = String(state.disabled);
        if (state.title) control.setAttribute('title', state.title);
        else control.removeAttribute('title');
      });
      syncProviderPrimaryActions();
    };

    const routerPolicyControls = {
      autoFailover: panel.querySelector('[data-agent-router-policy-auto-failover]'),
      maxRetries: panel.querySelector('[data-agent-router-policy-max-retries]'),
      failureThreshold: panel.querySelector('[data-agent-router-policy-failure-threshold]'),
      cooldownSeconds: panel.querySelector('[data-agent-router-policy-cooldown-seconds]'),
      save: panel.querySelector('[data-agent-router-policy-save]'),
      state: panel.querySelector('[data-agent-router-policy-state]'),
    };
    const syncRouterPolicyControls = () => {
      const policy = getAgentRouterPolicy(clientById(activeClient()));
      if (routerPolicyControls.autoFailover) routerPolicyControls.autoFailover.checked = policy.autoFailover;
      if (routerPolicyControls.maxRetries) routerPolicyControls.maxRetries.value = String(policy.maxRetries);
      if (routerPolicyControls.failureThreshold) routerPolicyControls.failureThreshold.value = String(policy.failureThreshold);
      if (routerPolicyControls.cooldownSeconds) routerPolicyControls.cooldownSeconds.value = String(policy.cooldownSeconds);
      if (routerPolicyControls.state) {
        routerPolicyControls.state.textContent = policy.configured ? '已绑定 Provider' : '先在 Providers 中点“当前”';
        routerPolicyControls.state.classList.toggle('is-good', policy.configured);
      }
      [routerPolicyControls.autoFailover, routerPolicyControls.maxRetries, routerPolicyControls.failureThreshold, routerPolicyControls.cooldownSeconds]
        .filter(Boolean)
        .forEach((control) => { control.disabled = !policy.configured; });
      if (routerPolicyControls.save) routerPolicyControls.save.disabled = !policy.configured;
    };

    const failoverQueueControls = {
      root: panel.querySelector('[data-agent-failover-queue]'),
      list: panel.querySelector('[data-agent-failover-queue-list]'),
      addSelect: panel.querySelector('[data-agent-failover-queue-add]'),
      addButton: panel.querySelector('[data-agent-failover-queue-add][type="button"]'),
      save: panel.querySelector('[data-agent-failover-queue-save]'),
      state: panel.querySelector('[data-agent-failover-queue-state]'),
    };
    let failoverQueueState = { providerIds: [], available: [], names: {} };
    const renderFailoverQueue = () => {
      const controls = failoverQueueControls;
      if (!controls.list) return;
      const names = failoverQueueState.names || {};
      controls.list.innerHTML = failoverQueueState.providerIds.length
        ? failoverQueueState.providerIds.map((providerId, index) => `
            <span class="agent-queue-chip">
              <b>${index + 1}</b><span>${escapeHtml(names[providerId] || providerId)}</span>
              <button type="button" class="agent-queue-chip-action" data-agent-queue-action="up" data-agent-queue-index="${index}" ${index === 0 ? 'disabled' : ''} aria-label="上移">↑</button>
              <button type="button" class="agent-queue-chip-action" data-agent-queue-action="down" data-agent-queue-index="${index}" ${index === failoverQueueState.providerIds.length - 1 ? 'disabled' : ''} aria-label="下移">↓</button>
              <button type="button" class="agent-queue-chip-action" data-agent-queue-action="remove" data-agent-queue-index="${index}" aria-label="移除">×</button>
            </span>`).join('')
        : '<span class="agent-muted">尚未指定队列。开启策略时会把当前 Provider 自动放入 P1。</span>';
      if (controls.addSelect) {
        const available = (failoverQueueState.available || []).filter((item) => !failoverQueueState.providerIds.includes(item.provider_id));
        controls.addSelect.innerHTML = '<option value="">加入 Provider…</option>' + available.map((item) => `<option value="${escapeHtml(item.provider_id)}">${escapeHtml(item.name || item.provider_id)}</option>`).join('');
        controls.addSelect.disabled = !getAgentRouterPolicy(clientById(activeClient())).configured || !available.length;
      }
      if (controls.addButton) controls.addButton.disabled = controls.addSelect?.disabled || !controls.addSelect?.value;
      if (controls.save) controls.save.disabled = !getAgentRouterPolicy(clientById(activeClient())).configured;
      if (controls.state) {
        controls.state.textContent = failoverQueueState.providerIds.length ? `${failoverQueueState.providerIds.length} 个 Provider` : '队列为空';
        controls.state.classList.toggle('is-good', Boolean(failoverQueueState.providerIds.length));
        controls.state.classList.toggle('is-muted', !failoverQueueState.providerIds.length);
      }
    };
    const loadFailoverQueue = async (clientId) => {
      if (!failoverQueueControls.root || !clientId) return;
      try {
        const body = await requestJson(`/api/agent/router/apps/${encodeURIComponent(clientId)}/failover-queue`, { method: 'GET' });
        const names = {};
        (body?.items || []).forEach((item) => { names[item.provider_id] = item.name; });
        (body?.available || []).forEach((item) => { names[item.provider_id] = item.name; });
        failoverQueueState = { providerIds: Array.isArray(body?.provider_ids) ? body.provider_ids : [], available: Array.isArray(body?.available) ? body.available : [], names };
        renderFailoverQueue();
      } catch (error) {
        failoverQueueState = { providerIds: [], available: [], names: {} };
        if (failoverQueueControls.state) { failoverQueueControls.state.textContent = '加载失败'; failoverQueueControls.state.classList.remove('is-good'); }
      }
    };

    const tabButtons = Array.from(panel.querySelectorAll('[data-agent-tab-control]'));
    const tabPanels = Array.from(panel.querySelectorAll('[data-agent-tab]'));
    const visibleTabButtons = () => tabButtons.filter((button) => !button.hidden);
    const activeCapabilities = () => new Set((clientById(activeClient())?.dataset.agentCapabilities || '').split(',').filter(Boolean));
    const activateTab = (name) => {
      const candidate = tabButtons.find((button) => button.dataset.agentTabControl === name && !button.hidden)
        || visibleTabButtons()[0];
      if (!candidate) return;
      const selected = candidate.dataset.agentTabControl;
      tabButtons.forEach((button) => {
        const active = button === candidate;
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-selected', String(active));
      });
      tabPanels.forEach((section) => {
        const active = section.dataset.agentTab === selected;
        section.hidden = !active;
        section.classList.toggle('is-active', active);
      });
      if (history.replaceState) history.replaceState(null, '', `#agent-${selected}`);
    };
    const syncTabs = () => {
      const capabilities = activeCapabilities();
      tabButtons.forEach((button) => {
        button.hidden = Boolean(button.dataset.agentFeature && !capabilities.has(button.dataset.agentFeature));
      });
      const current = tabButtons.find((button) => button.classList.contains('is-active'));
      if (!current || current.hidden) activateTab('providers');
    };

    const emptyLibrary = () => ({ mcp: [], skills: [], prompts: [], providers: [], profiles: [], router: {}, discovery: { mcp: [], skills: [], prompts: [] } });
    let libraryState = emptyLibrary();
    try {
      const bootstrap = panel.querySelector('[data-agent-library-bootstrap]')?.textContent || '{}';
      libraryState = { ...emptyLibrary(), ...JSON.parse(bootstrap) };
      libraryState.discovery = { ...emptyLibrary().discovery, ...(libraryState.discovery || {}) };
    } catch {
      libraryState = emptyLibrary();
    }
    const libraryKey = (resourceType) => ({ mcp: 'mcp', skill: 'skills', prompt: 'prompts' }[resourceType] || resourceType);
    const resourceTypeForKey = (key) => ({ mcp: 'mcp', skills: 'skill', prompts: 'prompt' }[key] || key);
    const resourceById = (resourceType, resourceId) => (
      (libraryState[libraryKey(resourceType)] || []).find((item) => String(item.id) === String(resourceId)) || null
    );
    const orderListFor = (resourceType) => panel.querySelector(`[data-agent-order-list="${resourceType}"]`);
    const orderCardsFor = (resourceType, clientId = activeClient()) => {
      const list = orderListFor(resourceType);
      if (!list) return [];
      return Array.from(list.querySelectorAll('[data-agent-order-card]')).filter((card) => (
        card.dataset.resourceType === resourceType
        && (resourceType !== 'provider' || card.dataset.providerApp === clientId)
      ));
    };
    const orderIdsFor = (resourceType, clientId = activeClient()) => (
      orderCardsFor(resourceType, clientId).map((card) => card.dataset.resourceId).filter(Boolean)
    );
    const syncOrderControls = (resourceType, clientId = activeClient()) => {
      const cards = orderCardsFor(resourceType, clientId);
      cards.forEach((card, index) => {
        const up = card.querySelector('[data-agent-resource-move="up"]');
        const down = card.querySelector('[data-agent-resource-move="down"]');
        if (up) up.disabled = index === 0;
        if (down) down.disabled = index === cards.length - 1;
      });
    };
    const applyResourceOrderToDom = (resourceType, resourceIds, clientId = activeClient()) => {
      const list = orderListFor(resourceType);
      if (!list) return;
      const cards = new Map(
        orderCardsFor(resourceType, clientId).map((card) => [card.dataset.resourceId, card]),
      );
      resourceIds.forEach((resourceId) => {
        const card = cards.get(String(resourceId));
        if (card) list.append(card);
      });
      syncOrderControls(resourceType, clientId);
    };
    const applyResourceOrderToState = (resourceType, resourceIds, clientId = activeClient()) => {
      const ids = resourceIds.map(String);
      if (resourceType === 'provider') {
        const selected = (libraryState.providers || []).filter((item) => String(item.app_id) === clientId);
        const byId = new Map(selected.map((item) => [String(item.id), item]));
        const other = (libraryState.providers || []).filter((item) => String(item.app_id) !== clientId);
        libraryState.providers = [...other, ...ids.map((id) => byId.get(id)).filter(Boolean)];
        return;
      }
      const key = libraryKey(resourceType);
      const byId = new Map((libraryState[key] || []).map((item) => [String(item.id), item]));
      libraryState[key] = ids.map((id) => byId.get(id)).filter(Boolean);
    };
    const syncProviderOrderFromLibrary = () => {
      const list = orderListFor('provider');
      if (!list) return;
      (libraryState.providers || []).forEach((provider) => {
        const card = Array.from(list.querySelectorAll('[data-agent-provider-card]')).find((item) => (
          item.dataset.providerApp === String(provider.app_id)
          && item.dataset.resourceId === String(provider.id)
        ));
        if (card) list.append(card);
      });
    };
    const stateToneClass = (tone) => ({ good: 'is-good', warn: 'is-warn', danger: 'is-danger', muted: 'is-muted' }[tone] || 'is-muted');
    const resourceSource = (resourceType, resource) => {
      if (resourceType === 'skill') return resource.source_kind || resource.source || 'database';
      if (resourceType === 'prompt') return resource.variants?.length ? 'database + variants' : 'database';
      return resource.source || 'manual';
    };
    const resourceMark = (resourceType) => ({ mcp: 'M', skill: 'S', prompt: 'P' }[resourceType] || 'R');
    const renderOrderControls = (resourceType, resource, index, total) => {
      const name = escapeHtml(resource.name || resource.id || resourceType);
      const typeLabel = ({ provider: 'Provider', mcp: 'MCP', skill: 'Skill', prompt: 'Prompt', profile: 'Profile' }[resourceType] || '资源');
      return `<div class="agent-order-controls" aria-label="${typeLabel} 排序">
        <span class="agent-drag-handle" data-agent-drag-handle tabindex="0" role="img" aria-label="拖动 ${name} 排序" title="拖动排序">⠿</span>
        <button type="button" data-agent-resource-move="up" aria-label="上移 ${name}"${index <= 0 ? ' disabled' : ''}>↑</button>
        <button type="button" data-agent-resource-move="down" aria-label="下移 ${name}"${index >= total - 1 ? ' disabled' : ''}>↓</button>
      </div>`;
    };
    const renderResourceCard = (resourceType, resource, index, total) => {
      const state = getAgentResourceClientState(resource, activeClient());
      const id = String(resource.id || '');
      const name = String(resource.name || id);
      const encodedId = escapeHtml(id);
      const source = escapeHtml(resourceSource(resourceType, resource));
      const legacyCard = resourceType === 'mcp'
        ? ` data-agent-mcp-card data-agent-mcp-card-id="${encodedId}" data-agent-mcp-managed="true"`
        : '';
      const legacyEdit = resourceType === 'mcp' ? ` data-agent-mcp-edit="${encodedId}"` : '';
      const legacyInstall = resourceType === 'mcp' ? ` data-agent-mcp-install-one="${encodedId}"` : '';
      const legacyUninstall = resourceType === 'mcp' ? ` data-agent-mcp-uninstall-one="${encodedId}"` : '';
      const legacyDelete = resourceType === 'mcp'
        ? ` data-agent-mcp-delete="${encodedId}"`
        : resourceType === 'prompt' ? ` data-agent-prompt-delete="${encodedId}"` : '';
      const uninstallDisabled = !state.assigned && !state.present ? ' disabled' : '';
      const syncDisabled = !state.assigned ? ' disabled' : '';
      const description = resource.description
        ? `<small class="agent-resource-description" title="${escapeHtml(resource.description)}">${escapeHtml(resource.description)}</small>`
        : '';
      return `
        <article class="agent-resource-card" data-agent-resource-card data-agent-order-card data-resource-type="${resourceType}" data-resource-id="${encodedId}" draggable="true"${legacyCard}>
          ${renderOrderControls(resourceType, resource, index, total)}
          <div class="agent-resource-identity">
            <span class="agent-resource-mark is-${resourceType}">${resourceMark(resourceType)}</span>
            <div><strong title="${escapeHtml(name)}">${escapeHtml(name)}</strong><small title="${encodedId} · ${source}">${encodedId} · ${source}</small>${description}</div>
          </div>
          <div class="agent-resource-state" aria-label="当前客户端资源状态">
            <span class="agent-state-pill is-library">库中</span>
            <span class="agent-state-pill ${state.assigned ? 'is-assigned' : 'is-muted'}">${state.assigned ? '已分配' : '未分配'}</span>
            <span class="agent-state-pill ${stateToneClass(state.tone)}">回读：${state.statusLabel}</span>
            <span class="agent-state-pill is-variant" title="客户端变体">${escapeHtml(state.variantLabel)}</span>
          </div>
          <div class="agent-resource-actions">
            <button class="button button--small button--quiet" type="button" data-agent-resource-edit data-resource-type="${resourceType}" data-resource-id="${encodedId}"${legacyEdit}>编辑</button>
            <button class="button button--small button--primary" type="button" data-agent-resource-install data-resource-type="${resourceType}" data-resource-id="${encodedId}"${legacyInstall}>安装/更新</button>
            <button class="button button--small button--quiet" type="button" data-agent-resource-uninstall data-resource-type="${resourceType}" data-resource-id="${encodedId}"${legacyUninstall}${uninstallDisabled}>卸载</button>
            <button class="button button--small button--quiet" type="button" data-agent-resource-sync data-resource-type="${resourceType}" data-resource-id="${encodedId}"${syncDisabled}>按库同步</button>
            <button class="button button--small button--danger" type="button" data-agent-resource-delete data-resource-type="${resourceType}" data-resource-id="${encodedId}"${legacyDelete}>从库删除</button>
          </div>
          ${resourceType === 'mcp' ? `<span hidden data-agent-mcp-json="${encodedId}"></span>` : ''}
        </article>`;
    };
    const renderResourceLists = () => {
      ['mcp', 'skills', 'prompts'].forEach((key) => {
        const resources = Array.isArray(libraryState[key]) ? libraryState[key] : [];
        const resourceType = resourceTypeForKey(key);
        const list = panel.querySelector(`[data-agent-resource-list="${key}"]`);
        const count = panel.querySelector(`[data-agent-library-count="${key}"]`);
        if (count) count.textContent = String(resources.length);
        if (!list) return;
        list.innerHTML = resources.length
          ? resources.map((resource, index) => renderResourceCard(resourceType, resource, index, resources.length)).join('')
          : `<div class="agent-empty-card">数据库中还没有 ${key === 'mcp' ? 'MCP' : key === 'skills' ? 'Skill' : 'Prompt'} 资源。</div>`;
      });
    };
    const renderDiscoveryLists = () => {
      ['mcp', 'skills', 'prompts'].forEach((key) => {
        const rows = getAgentDiscoveryForClient(libraryState.discovery, key, activeClient());
        const list = panel.querySelector(`[data-agent-discovery-list="${key}"]`);
        const count = panel.querySelector(`[data-agent-discovery-count="${key}"]`);
        if (count) count.textContent = String(rows.length);
        if (!list) return;
        const rendered = rows.map((item) => {
          const resourceType = resourceTypeForKey(key);
          const id = escapeHtml(item.id || item.name || '');
          const name = escapeHtml(item.name || item.id || '未命名');
          const source = escapeHtml(item.path || item.source || 'observed');
          return `<article class="agent-discovery-row" data-agent-discovery-item data-resource-type="${resourceType}" data-resource-id="${id}"><div><strong>${name}</strong><small title="${source}">${id} · ${source}</small></div><button class="button button--small button--primary" type="button" data-agent-discovery-import data-resource-type="${resourceType}" data-resource-id="${id}">导入到库</button></article>`;
        });
        if (key === 'prompts' && !rendered.length) {
          rendered.push('<button class="agent-discovery-row agent-discovery-row--action" type="button" data-agent-discovery-import data-resource-type="prompt"><span><strong>导入当前 Prompt 文件</strong><small>使用右侧 ID 与名称；留空则自动命名</small></span><span class="button button--small button--primary">导入到库</span></button>');
        }
        list.innerHTML = rendered.length ? rendered.join('') : '<div class="agent-empty-inline">当前客户端没有未纳管现场项。</div>';
      });
    };

    const profileFields = {
      id: panel.querySelector('[data-agent-profile-id]'),
      name: panel.querySelector('[data-agent-profile-name]'),
      description: panel.querySelector('[data-agent-profile-description]'),
      picker: panel.querySelector('[data-agent-profile-resource-picker]'),
    };
    let profileDraft = { profileId: '', existingItems: [], selections: { provider: [], mcp: [], skill: [], prompt: [], router: [] } };
    const emptyProfileSelections = () => ({ provider: [], mcp: [], skill: [], prompt: [], router: [] });
    const profileSelectionsForClient = (items, clientId) => {
      const selections = emptyProfileSelections();
      (Array.isArray(items) ? items : []).forEach((item) => {
        if (String(item.client_id || '') !== clientId || !selections[item.resource_type]) return;
        selections[item.resource_type].push(String(item.resource_id || ''));
      });
      return selections;
    };
    const routerProfileConfig = () => {
      const clientId = activeClient();
      const existing = profileDraft.existingItems.find((item) => item.client_id === clientId && item.resource_type === 'router');
      if (existing?.config && Object.keys(existing.config).length) return { ...existing.config };
      const router = libraryState.router || {};
      const config = {};
      const providerId = router.provider_ids?.[clientId];
      if (providerId) config.provider_id = providerId;
      config.takeover = Boolean(router.takeover?.[clientId]);
      const queue = router.failover_queues?.[clientId];
      if (Array.isArray(queue) && queue.length) config.failover_queue = [...queue];
      const policy = router.providers?.[clientId];
      if (policy) {
        config.policy = {
          auto_failover: Boolean(policy.auto_failover),
          max_retries: Number(policy.max_retries || 0),
          failure_threshold: Number(policy.failure_threshold || 3),
          cooldown_seconds: Number(policy.cooldown_seconds || 60),
        };
      }
      return config;
    };
    const profileOptionGroups = () => {
      const clientId = activeClient();
      const capabilities = activeCapabilities();
      return [
        { type: 'provider', label: 'Provider', singular: true, items: (libraryState.providers || []).filter((item) => item.app_id === clientId) },
        { type: 'mcp', label: 'MCP', singular: false, items: capabilities.has('mcp') ? (libraryState.mcp || []) : [] },
        { type: 'skill', label: 'Skills', singular: false, items: capabilities.has('skills') ? (libraryState.skills || []) : [] },
        { type: 'prompt', label: 'Prompt', singular: true, items: capabilities.has('prompts') ? (libraryState.prompts || []) : [] },
        { type: 'router', label: 'Router', singular: true, items: capabilities.has('route') ? [{ id: 'default', name: '当前路由策略' }] : [] },
      ];
    };
    const renderProfilePicker = () => {
      if (!profileFields.picker) return;
      if (profileDraft.clientId !== activeClient()) {
        profileDraft.clientId = activeClient();
        profileDraft.selections = profileSelectionsForClient(profileDraft.existingItems, activeClient());
      }
      const groups = profileOptionGroups();
      profileFields.picker.innerHTML = groups.map((group) => {
        const selected = new Set(profileDraft.selections[group.type] || []);
        const items = group.items.map((item) => {
          const id = String(item.id || '');
          const active = selected.has(id);
          return `<button type="button" class="agent-profile-resource ${active ? 'is-active' : ''}" data-agent-profile-resource data-resource-type="${group.type}" data-resource-id="${escapeHtml(id)}" data-resource-singular="${String(group.singular)}" aria-pressed="${String(active)}"><span>${escapeHtml(item.name || id)}</span><small>${escapeHtml(id)}</small></button>`;
        }).join('');
        return `<section class="agent-profile-resource-group"><header><strong>${group.label}</strong><span>${group.items.length}</span></header><div>${items || '<span class="agent-muted">当前客户端没有可选资源</span>'}</div></section>`;
      }).join('');
      const glyph = panel.querySelector('[data-agent-profile-client-glyph]');
      if (glyph) { glyph.textContent = clientName(activeClient()).slice(0, 1); glyph.className = `agent-client-glyph agent-client-glyph--${activeClient()}`; }
      const name = panel.querySelector('[data-agent-profile-client-name]');
      if (name) name.textContent = clientName(activeClient());
    };
    const renderProfiles = () => {
      const profiles = Array.isArray(libraryState.profiles) ? libraryState.profiles : [];
      const list = panel.querySelector('[data-agent-profile-list]');
      const count = panel.querySelector('[data-agent-profile-count]');
      if (count) count.textContent = String(profiles.length);
      if (list) {
        list.innerHTML = profiles.length ? profiles.map((profile, index) => {
          const currentCount = (profile.items || []).filter((item) => item.client_id === activeClient()).length;
          return `<article class="agent-profile-card" data-agent-profile-card="${escapeHtml(profile.id)}" data-agent-order-card data-resource-type="profile" data-resource-id="${escapeHtml(profile.id)}" draggable="true">${renderOrderControls('profile', profile, index, profiles.length)}<div class="agent-profile-copy"><strong>${escapeHtml(profile.name || profile.id)}</strong><small>${escapeHtml(profile.id)} · 当前客户端 ${currentCount} 项 / 全部 ${(profile.items || []).length} 项</small>${profile.description ? `<p>${escapeHtml(profile.description)}</p>` : ''}</div><div class="agent-resource-actions"><button class="button button--small button--quiet" type="button" data-agent-profile-edit="${escapeHtml(profile.id)}">编辑</button><button class="button button--small button--primary" type="button" data-agent-profile-apply="${escapeHtml(profile.id)}">应用到当前客户端</button><button class="button button--small button--danger" type="button" data-agent-profile-delete="${escapeHtml(profile.id)}">删除</button></div></article>`;
        }).join('') : '<div class="agent-empty-card">还没有 Profile。右侧选择当前客户端资源并保存。</div>';
      }
      renderProfilePicker();
    };
    const renderAgentLibraryView = () => {
      renderResourceLists();
      renderDiscoveryLists();
      renderProfiles();
      syncProviderOrderFromLibrary();
      ['provider', 'mcp', 'skill', 'prompt', 'profile'].forEach((resourceType) => syncOrderControls(resourceType));
      buttons.forEach((button) => {
        const clientId = button.value;
        button.dataset.agentSkillCount = String(getAgentInstalledSkillCount(libraryState, clientId));
        button.dataset.agentProviderCount = String((libraryState.providers || []).filter((provider) => provider.app_id === clientId).length);
      });
      const status = getAgentClientStatus(clientById(activeClient()));
      const skillCount = panel.querySelector('[data-agent-active-skill-count]');
      if (skillCount) skillCount.textContent = String(status.skillCount);
      const providerCount = panel.querySelector('[data-agent-active-provider-count]');
      if (providerCount) providerCount.textContent = String(status.providerCount);
    };
    const loadAgentLibrary = async ({ quiet = false } = {}) => {
      try {
        const body = await requestJson('/api/agent/library', { method: 'GET' });
        if (!body) return null;
        libraryState = { ...emptyLibrary(), ...body, discovery: { ...emptyLibrary().discovery, ...(body.discovery || {}) } };
        renderAgentLibraryView();
        return body;
      } catch (error) {
        if (!quiet) flash(`资源库刷新失败：${error.message}`);
        return null;
      }
    };
    const persistResourceOrder = async (resourceType, resourceIds, trigger = null) => {
      const list = orderListFor(resourceType);
      if (trigger) setPending(trigger, true);
      list?.classList.add('is-saving-order');
      try {
        await putJson(
          '/api/agent/resources/order',
          buildAgentResourceOrderPayload(resourceType, resourceIds, activeClient()),
        );
        flash('列表顺序已保存', { success: true });
        return true;
      } catch (error) {
        await loadAgentLibrary({ quiet: true });
        flash(`排序保存失败，已恢复数据库顺序：${error.message}`);
        return false;
      } finally {
        list?.classList.remove('is-saving-order');
        if (trigger) setPending(trigger, false);
      }
    };
    const syncClientView = () => {
      const appId = activeClient();
      panel.dataset.agentActiveClient = appId;
      buttons.forEach((button) => setButtonState(button, button.value === appId));
      panel.querySelectorAll('[data-agent-active-name]').forEach((node) => { node.textContent = clientName(appId); });
      const activeGlyph = panel.querySelector('[data-agent-active-glyph]');
      if (activeGlyph) {
        activeGlyph.textContent = clientName(appId).slice(0, 1);
        activeGlyph.className = `agent-client-glyph agent-client-glyph--${appId}`;
      }
      const activeSource = panel.querySelector('[data-agent-active-source]');
      if (activeSource) activeSource.textContent = clientById(appId)?.dataset.agentDetectionSource || 'config';
      const status = getAgentClientStatus(clientById(appId));
      const featureCount = panel.querySelector('[data-agent-active-feature-count]');
      if (featureCount) featureCount.textContent = String(status.featureCount);
      const providerCount = panel.querySelector('[data-agent-active-provider-count]');
      if (providerCount) providerCount.textContent = String(status.providerCount);
      const skillCount = panel.querySelector('[data-agent-active-skill-count]');
      if (skillCount) skillCount.textContent = String(status.skillCount);
      const mcpPath = panel.querySelector('[data-agent-active-mcp-path]');
      if (mcpPath) mcpPath.textContent = status.mcpPath;
      const routeTarget = panel.querySelector('[data-agent-router-current-provider]');
      if (routeTarget) routeTarget.textContent = clientById(appId)?.dataset.agentRouterProvider || '未选择';
      syncProviderOwnershipControls();
      panel.querySelectorAll('[data-provider-app]').forEach((card) => { card.hidden = card.dataset.providerApp !== appId; });
      panel.querySelectorAll('[data-agent-provider-fields]').forEach((fields) => { fields.hidden = fields.dataset.agentProviderFields !== appId; });
      panel.querySelectorAll('[data-agent-skill-client]').forEach((card) => { card.hidden = card.dataset.agentSkillClient !== appId; });
      syncTabs();
      renderAgentLibraryView();
      syncRouterPolicyControls();
      void loadFailoverQueue(appId);
      renderFailoverQueue();
      const appInput = panel.querySelector('[data-agent-provider-app]');
      if (appInput) appInput.value = appId;
    };

    buttons.forEach((button) => {
      button.addEventListener('click', () => {
        const next = nextAgentClient(activeClient(), button.value);
        if (!next) return;
        panel.dataset.agentActiveClient = next;
        syncClientView();
      });
    });
    panel.querySelectorAll('[data-agent-tab-control]').forEach((button) => {
      button.addEventListener('click', () => activateTab(button.dataset.agentTabControl));
    });

    const hashTab = (window.location.hash || '').replace('#agent-', '');
    syncClientView();
    activateTab(hashTab || 'providers');

    // Provider editor: native client fields are the database source document.
    const providerEditor = panel.querySelector('[data-agent-provider-editor]');
    const providerId = panel.querySelector('[data-agent-provider-id]');
    const providerName = panel.querySelector('[data-agent-provider-name]');
    const providerDescription = panel.querySelector('[data-agent-provider-description]');
    const providerWebsite = panel.querySelector('[data-agent-provider-website]');
    const providerBase = panel.querySelector('[data-agent-provider-base-url]');
    const providerFormat = panel.querySelector('[data-agent-provider-format]');
    const providerAuth = panel.querySelector('[data-agent-provider-auth]');
    const providerModel = panel.querySelector('[data-agent-provider-model]');
    const providerKey = panel.querySelector('[data-agent-provider-key]');
    const providerMap = panel.querySelector('[data-agent-provider-model-map]');
    const providerHeaders = panel.querySelector('[data-agent-provider-headers]');
    const providerSettings = panel.querySelector('[data-agent-provider-settings]');
    const providerFullUrl = panel.querySelector('[data-agent-provider-full-url]');
    const providerOutboundProxy = panel.querySelector('[data-agent-provider-outbound-proxy]');
    const providerSave = panel.querySelector('[data-agent-provider-save]');
    const providerReadonly = panel.querySelector('[data-agent-provider-readonly]');
    const providerDefaults = {
      claude: { format: 'anthropic', auth: 'x-api-key' },
      codex: { format: 'openai_responses', auth: 'bearer' },
      gemini: { format: 'gemini', auth: 'query' },
      grokbuild: { format: 'openai_responses', auth: 'bearer' },
      opencode: { format: 'openai_chat', auth: 'bearer' },
      openclaw: { format: 'openai_chat', auth: 'bearer' },
      hermes: { format: 'openai_chat', auth: 'bearer' },
    };
    const providerKeyParts = (value) => {
      const parts = String(value || '').split('::');
      return { appId: parts.shift() || '', providerId: parts.join('::') };
    };
    const providerFieldsFor = (appId) => panel.querySelector(`[data-agent-provider-fields="${CSS.escape(String(appId || ''))}"]`);
    const providerField = (appId, selector) => providerFieldsFor(appId)?.querySelector(selector) || null;
    const setProviderEditorEditable = (editable, reason = '') => {
      if (!providerEditor) return;
      providerEditor.dataset.agentProviderEditable = String(Boolean(editable));
      providerEditor.querySelectorAll('input, select, textarea').forEach((control) => {
        if (control === providerSettings) {
          control.readOnly = true;
          return;
        }
        if (control.matches('select, input[type="checkbox"]')) control.disabled = !editable;
        else control.readOnly = !editable;
      });
      if (providerSave) providerSave.disabled = !editable;
      if (providerReadonly) {
        providerReadonly.hidden = Boolean(editable);
        providerReadonly.textContent = editable ? '' : (reason || '此 Provider 由客户端维护，仅供查看和复制。');
      }
    };
    const clearProvider = () => {
      if (!providerEditor) return;
      setProviderEditorEditable(true);
      providerEditor.dataset.agentProviderApp = activeClient();
      providerEditor.querySelectorAll('input:not([type="checkbox"]), textarea').forEach((input) => { input.value = ''; });
      providerEditor.querySelectorAll('[data-agent-provider-fields]').forEach((fields) => { fields.hidden = fields.dataset.agentProviderFields !== activeClient(); });
      const defaults = providerDefaults[activeClient()] || providerDefaults.opencode;
      if (providerId) providerId.readOnly = false;
      if (providerFormat) providerFormat.value = defaults.format;
      if (providerAuth) providerAuth.value = defaults.auth;
      if (providerFullUrl) providerFullUrl.checked = false;
      if (providerOutboundProxy) providerOutboundProxy.checked = true;
      if (providerSettings) providerSettings.readOnly = true;
    };
    const providerJson = (input, fallback, label) => {
      const raw = input?.value?.trim();
      if (!raw) return fallback;
      try { return JSON.parse(raw); }
      catch (error) { throw new Error(`${label} JSON 无效：${error.message}`); }
    };
    const fillProvider = (provider) => {
      if (!provider) return;
      const appId = provider.app_id || activeClient();
      panel.dataset.agentActiveClient = appId;
      syncClientView();
      clearProvider();
      const form = provider.form || {};
      const meta = provider.meta || {};
      const summary = provider.summary || {};
      if (providerEditor) providerEditor.dataset.agentProviderApp = appId;
      if (providerId) { providerId.value = provider.id || ''; providerId.readOnly = true; }
      if (providerName) providerName.value = provider.name || '';
      if (providerDescription) providerDescription.value = provider.notes || '';
      if (providerWebsite) providerWebsite.value = provider.website_url || '';
      if (providerBase) providerBase.value = form.base_url || summary.base_url || '';
      if (providerFormat) providerFormat.value = form.api_format || summary.api_format || providerDefaults[appId]?.format || 'openai_chat';
      if (providerAuth) providerAuth.value = form.auth_mode || summary.auth_mode || providerDefaults[appId]?.auth || 'bearer';
      if (providerModel) providerModel.value = form.model || summary.model || '';
      if (providerKey) providerKey.value = form.api_key || '';
      if (providerMap) providerMap.value = JSON.stringify(meta.model_map || form.model_map || {}, null, 2);
      if (providerHeaders) providerHeaders.value = JSON.stringify(form.headers || {}, null, 2);
      if (providerSettings) providerSettings.value = JSON.stringify(provider.settings_config || {}, null, 2);
      if (providerFullUrl) providerFullUrl.checked = Boolean(meta.full_url ?? form.full_url);
      if (providerOutboundProxy) providerOutboundProxy.checked = (meta.use_outbound_proxy ?? form.use_outbound_proxy) !== false;
      const scoped = providerFieldsFor(appId);
      const assign = (selector, value) => { const field = scoped?.querySelector(selector); if (field) field.value = value ?? ''; };
      assign('[data-agent-provider-haiku-model]', form.haiku_model);
      assign('[data-agent-provider-sonnet-model]', form.sonnet_model);
      assign('[data-agent-provider-opus-model]', form.opus_model);
      assign('[data-agent-provider-key-name]', form.provider_key);
      assign('[data-agent-provider-profile]', form.profile);
      assign('[data-agent-provider-env-key]', form.env_key);
      assign('[data-agent-provider-context-window]', form.context_window);
      assign('[data-agent-provider-npm]', form.npm);
      const models = scoped?.querySelector('[data-agent-provider-models]');
      if (models) models.value = JSON.stringify(form.models ?? (appId === 'openclaw' ? [] : {}), null, 2);
      setProviderEditorEditable(summary.editable !== false, summary.read_only_reason || '此 Provider 由客户端维护，仅供查看和复制。');
      if (providerId) providerId.readOnly = true;
    };
    const loadProvider = async (key) => {
      const { appId, providerId: id } = providerKeyParts(key);
      if (!appId || !id) return null;
      return requestJson(`/api/agent/providers/${encodeURIComponent(appId)}/${encodeURIComponent(id)}`, { method: 'GET' });
    };
    const providerPayload = () => {
      const appId = activeClient();
      const id = providerId?.value?.trim() || '';
      if (!appId || !id) throw new Error('先填写 Provider ID');
      const form = {
        base_url: providerBase?.value?.trim() || '',
        api_key: providerKey?.value || '',
        model: providerModel?.value?.trim() || '',
        api_format: providerFormat?.value || providerDefaults[appId]?.format || 'openai_chat',
        auth_mode: providerAuth?.value || providerDefaults[appId]?.auth || 'bearer',
        headers: providerJson(providerHeaders, {}, 'Headers'),
      };
      const scoped = providerFieldsFor(appId);
      const value = (selector) => scoped?.querySelector(selector)?.value?.trim() || '';
      if (appId === 'claude') {
        form.haiku_model = value('[data-agent-provider-haiku-model]');
        form.sonnet_model = value('[data-agent-provider-sonnet-model]');
        form.opus_model = value('[data-agent-provider-opus-model]');
      } else if (appId === 'codex') {
        form.provider_key = value('[data-agent-provider-key-name]') || id;
      } else if (appId === 'grokbuild') {
        form.profile = value('[data-agent-provider-profile]') || id;
        form.env_key = value('[data-agent-provider-env-key]');
        form.context_window = value('[data-agent-provider-context-window]');
      } else if (appId === 'opencode') {
        form.npm = value('[data-agent-provider-npm]');
        form.models = providerJson(providerField(appId, '[data-agent-provider-models]'), {}, '模型字典');
      } else if (appId === 'openclaw') {
        form.models = providerJson(providerField(appId, '[data-agent-provider-models]'), [], '模型列表');
      } else if (appId === 'hermes') {
        form.env_key = value('[data-agent-provider-env-key]');
        form.models = providerJson(providerField(appId, '[data-agent-provider-models]'), {}, '模型字典');
      }
      return buildAgentProviderPayload({
        appId,
        providerId: id,
        name: providerName?.value?.trim() || id,
        description: providerDescription?.value ?? '',
        websiteUrl: providerWebsite?.value?.trim() || '',
        form,
        meta: {
          model_map: providerJson(providerMap, {}, '模型映射'),
          full_url: Boolean(providerFullUrl?.checked),
          use_outbound_proxy: providerOutboundProxy?.checked !== false,
        },
      });
    };
    if (providerEditor && !providerEditor.dataset.agentProviderApp) providerEditor.dataset.agentProviderApp = activeClient();
    buttons.forEach((button) => button.addEventListener('click', () => {
      if (providerEditor?.dataset.agentProviderApp !== button.value) clearProvider();
    }));
    panel.querySelector('[data-agent-provider-new]')?.addEventListener('click', () => { clearProvider(); providerId?.focus(); });
    panel.querySelector('[data-agent-provider-clear]')?.addEventListener('click', clearProvider);
    panel.querySelector('[data-agent-provider-import]')?.addEventListener('click', async (event) => {
      const trigger = event.currentTarget;
      try {
        setPending(trigger, true);
        const body = await post('/api/agent/providers/import-local', { apps: [activeClient()] });
        flash(`已从客户端配置导入 ${body?.imported_count || 0} 个 Provider`, { success: true });
        reloadSoon();
      } catch (error) { flash(`导入失败：${error.message}`); }
      finally { setPending(trigger, false); }
    });
    panel.querySelectorAll('[data-agent-provider-edit]').forEach((button) => button.addEventListener('click', async (event) => {
      const trigger = event.currentTarget;
      try {
        setPending(trigger, true);
        fillProvider(await loadProvider(trigger.dataset.agentProviderEdit));
        flash('Provider 详情已载入', { success: true });
      } catch (error) { flash(`读取失败：${error.message}`); }
      finally { setPending(trigger, false); }
    }));
    providerSave?.addEventListener('click', async (event) => {
      const trigger = event.currentTarget;
      try {
        setPending(trigger, true);
        await post('/api/agent/providers', providerPayload());
        flash('Provider 已保存到数据库', { success: true });
        reloadSoon();
      } catch (error) { flash(`保存失败：${error.message}`); }
      finally { setPending(trigger, false); }
    });
    panel.querySelectorAll('[data-agent-provider-activate]').forEach((button) => button.addEventListener('click', async (event) => {
      const trigger = event.currentTarget;
      const { appId, providerId: id } = providerKeyParts(trigger.dataset.agentProviderActivate);
      try {
        setPending(trigger, true);
        const body = await post(`/api/agent/providers/${encodeURIComponent(appId)}/${encodeURIComponent(id)}/activate`, { write_secrets: true });
        const message = body?.mode === 'router' ? 'Router 上游已热切换' : body?.mode === 'additive' ? 'Provider 添加任务已入队' : 'Provider 启用任务已入队';
        flash(message, { success: true });
        reloadSoon();
      } catch (error) { flash(`操作失败：${error.message}`); }
      finally { setPending(trigger, false); }
    }));
    panel.querySelectorAll('[data-agent-provider-remove-live]').forEach((button) => button.addEventListener('click', async (event) => {
      const trigger = event.currentTarget;
      const { appId, providerId: id } = providerKeyParts(trigger.dataset.agentProviderRemoveLive);
      try {
        setPending(trigger, true);
        await requestJson(`/api/agent/providers/${encodeURIComponent(appId)}/${encodeURIComponent(id)}/remove-live`, { method: 'POST' });
        flash('Provider 移除任务已入队，数据库记录保留', { success: true });
        reloadSoon();
      } catch (error) { flash(`移除失败：${error.message}`); }
      finally { setPending(trigger, false); }
    }));
    panel.querySelectorAll('[data-agent-provider-duplicate]').forEach((button) => button.addEventListener('click', async (event) => {
      const trigger = event.currentTarget;
      const { appId, providerId: id } = providerKeyParts(trigger.dataset.agentProviderDuplicate);
      try {
        setPending(trigger, true);
        const copy = await post(`/api/agent/providers/${encodeURIComponent(appId)}/${encodeURIComponent(id)}/duplicate`, {});
        flash(`已复制为 ${copy?.name || copy?.id || '新 Provider'}`, { success: true });
        reloadSoon();
      } catch (error) { flash(`复制失败：${error.message}`); }
      finally { setPending(trigger, false); }
    }));
    panel.querySelectorAll('[data-agent-provider-test]').forEach((button) => button.addEventListener('click', async (event) => {
      const trigger = event.currentTarget;
      const { appId, providerId: id } = providerKeyParts(trigger.dataset.agentProviderTest);
      try {
        setPending(trigger, true);
        const body = await requestJson(`/api/agent/providers/${encodeURIComponent(appId)}/${encodeURIComponent(id)}/test`, { method: 'POST' });
        flash(body.ok ? `端点可达：HTTP ${body.status_code} · ${body.latency_ms}ms` : '端点连接失败', { success: Boolean(body.ok) });
      } catch (error) { flash(`检测失败：${error.message}`); }
      finally { setPending(trigger, false); }
    }));
    panel.querySelectorAll('[data-agent-provider-delete]').forEach((button) => button.addEventListener('click', async (event) => {
      const trigger = event.currentTarget;
      if (!window.confirm(`从数据库删除 Provider：${trigger.dataset.agentProviderDelete}？`)) return;
      const { appId, providerId: id } = providerKeyParts(trigger.dataset.agentProviderDelete);
      try {
        setPending(trigger, true);
        await requestJson(`/api/agent/providers/${encodeURIComponent(appId)}/${encodeURIComponent(id)}`, { method: 'DELETE' });
        flash('Provider 已从数据库删除', { success: true });
        reloadSoon();
      } catch (error) { flash(`删除失败：${error.message}`); }
      finally { setPending(trigger, false); }
    }));
    panel.querySelectorAll('[data-agent-provider-select]').forEach((button) => button.addEventListener('click', () => {
      panel.querySelectorAll('[data-agent-provider-select]').forEach((item) => setButtonState(item, item === button));
      const card = button.closest('[data-agent-provider-card]');
      if (card) panel.querySelector(`[data-agent-provider-edit="${CSS.escape(`${card.dataset.providerApp}::${card.dataset.providerId}`)}"]`)?.click();
    }));

    failoverQueueControls.list?.addEventListener('click', (event) => {
      const action = event.target.closest('[data-agent-queue-action]');
      if (!action) return;
      const index = Number(action.dataset.agentQueueIndex);
      if (!Number.isInteger(index) || index < 0 || index >= failoverQueueState.providerIds.length) return;
      const ids = [...failoverQueueState.providerIds];
      if (action.dataset.agentQueueAction === 'up' && index > 0) [ids[index - 1], ids[index]] = [ids[index], ids[index - 1]];
      if (action.dataset.agentQueueAction === 'down' && index < ids.length - 1) [ids[index], ids[index + 1]] = [ids[index + 1], ids[index]];
      if (action.dataset.agentQueueAction === 'remove') ids.splice(index, 1);
      failoverQueueState.providerIds = ids;
      renderFailoverQueue();
    });
    failoverQueueControls.addSelect?.addEventListener('change', () => renderFailoverQueue());
    failoverQueueControls.addButton?.addEventListener('click', () => {
      const providerId = failoverQueueControls.addSelect?.value;
      if (!providerId || failoverQueueState.providerIds.includes(providerId)) return;
      failoverQueueState.providerIds.push(providerId);
      renderFailoverQueue();
    });
    failoverQueueControls.save?.addEventListener('click', async (event) => {
      const trigger = event.currentTarget;
      const clientId = activeClient();
      const policy = getAgentRouterPolicy(clientById(clientId));
      if (!policy.configured) { flash('先在 Providers 中选择“当前” Provider'); return; }
      try {
        setPending(trigger, true);
        const body = await putJson(`/api/agent/router/apps/${encodeURIComponent(clientId)}/failover-queue`, { provider_ids: failoverQueueState.providerIds });
        failoverQueueState.providerIds = Array.isArray(body?.provider_ids) ? body.provider_ids : failoverQueueState.providerIds;
        renderFailoverQueue();
        flash('故障转移队列已保存，顺序已生效', { success: true });
      } catch (error) { flash(`队列保存失败：${error.message}`); } finally { setPending(trigger, false); }
    });

    // Router control plane.
    const routerHealth = panel.querySelector('[data-agent-router-health]');
    const renderRouterStatus = (status) => {
      const health = status?.healthy ? '运行中' : status?.service === 'port_conflict' ? '端口冲突' : '已停止';
      if (routerHealth) { routerHealth.textContent = health; routerHealth.classList.toggle('is-good', Boolean(status?.healthy)); }
      const state = panel.querySelector('[data-agent-router-state]'); if (state) state.textContent = health;
      const config = status?.config || {};
      const address = panel.querySelector('[data-agent-router-address-input]'); if (address && config.listen_address) address.value = config.listen_address;
      const port = panel.querySelector('[data-agent-router-port]'); if (port && config.listen_port) port.value = config.listen_port;
    };
    const refreshRouter = async () => {
      try { renderRouterStatus(await requestJson('/api/agent/router/status', { method: 'GET' })); }
      catch (error) { if (routerHealth) routerHealth.textContent = '检查失败'; }
    };
    panel.querySelector('[data-agent-router-refresh]')?.addEventListener('click', refreshRouter);
    panel.querySelectorAll('[data-agent-router-action]').forEach((button) => button.addEventListener('click', async (event) => {
      const trigger = event.currentTarget;
      const action = trigger.dataset.agentRouterAction;
      try { setPending(trigger, true); const body = await requestJson(`/api/agent/router/${action}`, { method: action === 'stop' ? 'POST' : 'POST', body: JSON.stringify({ restore_clients: action === 'stop' }) }); flash(`Router ${action} 请求已提交`, { success: true }); renderRouterStatus(body); refreshRouter(); }
      catch (error) { flash(`Router 操作失败：${error.message}`); } finally { setPending(trigger, false); }
    }));
    panel.querySelector('[data-agent-router-save]')?.addEventListener('click', async (event) => {
      const trigger = event.currentTarget;
      const address = panel.querySelector('[data-agent-router-address-input]')?.value?.trim();
      const port = Number(panel.querySelector('[data-agent-router-port]')?.value || 7888);
      const proxy = panel.querySelector('[data-agent-router-proxy]')?.value?.trim() || null;
      try {
        setPending(trigger, true);
        await putJson('/api/agent/router/config', { listen_address: address, listen_port: port, show_home_switch: Boolean(panel.querySelector('[data-agent-router-home-switch]')?.checked), outbound_proxy: proxy });
        const policy = getAgentRouterPolicy(clientById(activeClient()));
        if (policy.configured) {
          const values = await putJson(`/api/agent/router/apps/${encodeURIComponent(activeClient())}/policy`, buildAgentRouterPolicyPayload({
            autoFailover: routerPolicyControls.autoFailover?.checked,
            maxRetries: routerPolicyControls.maxRetries?.value,
            failureThreshold: routerPolicyControls.failureThreshold?.value,
            cooldownSeconds: routerPolicyControls.cooldownSeconds?.value,
          }));
          const button = clientById(activeClient());
          applyAgentRouterProfileDataset(button, button?.dataset.agentRouterProvider, values?.provider || {});
          syncRouterPolicyControls();
        }
        flash(policy.configured ? 'Router 运行配置与当前客户端路由策略已保存' : 'Router 运行配置已保存；先选择当前 Provider 才能启用故障转移策略', { success: true });
      }
      catch (error) { flash(`保存失败：${error.message}`); } finally { setPending(trigger, false); }
    });
    routerPolicyControls.save?.addEventListener('click', async (event) => {
      const trigger = event.currentTarget;
      const clientId = activeClient();
      const policy = getAgentRouterPolicy(clientById(clientId));
      if (!policy.configured) { flash('先在 Providers 中选择“当前” Provider'); return; }
      try {
        setPending(trigger, true);
        const body = await putJson(`/api/agent/router/apps/${encodeURIComponent(clientId)}/policy`, buildAgentRouterPolicyPayload({
          autoFailover: routerPolicyControls.autoFailover?.checked,
          maxRetries: routerPolicyControls.maxRetries?.value,
          failureThreshold: routerPolicyControls.failureThreshold?.value,
          cooldownSeconds: routerPolicyControls.cooldownSeconds?.value,
        }));
        const button = clientById(clientId);
        applyAgentRouterProfileDataset(button, button?.dataset.agentRouterProvider, body?.provider || {});
        syncRouterPolicyControls();
        flash('当前客户端的故障转移策略已保存', { success: true });
      } catch (error) { flash(`策略保存失败：${error.message}`); } finally { setPending(trigger, false); }
    });
    panel.querySelectorAll('[data-agent-route-takeover]').forEach((button) => button.addEventListener('click', async (event) => {
      const target = event.currentTarget;
      const clientId = target.dataset.agentRouteTakeover;
      const enabled = target.getAttribute('aria-pressed') !== 'true';
      try {
        setPending(target, true);
        const body = await putJson(`/api/agent/router/apps/${encodeURIComponent(clientId)}/takeover`, { enabled });
        const actual = body?.takeover?.[clientId] ?? enabled;
        setButtonState(target, actual);
        applyAgentRouterTakeoverDataset(clientById(clientId), actual);
        syncProviderOwnershipControls();
        const label = target.querySelector('[data-agent-takeover-label]');
        if (label) label.textContent = actual ? '已接管' : '未接管';
        flash(actual ? '客户端已接管本地 Router' : '客户端已恢复原配置', { success: true });
      }
      catch (error) { flash(`接管操作失败：${error.message}`); } finally { setPending(target, false); }
    }));
    refreshRouter();

    const resourceIdField = (resourceType) => panel.querySelector(`[data-agent-${resourceType}-id]`);
    const clearResourceEditor = (resourceType) => {
      const selectors = {
        mcp: ['[data-agent-mcp-id]', '[data-agent-mcp-name]', '[data-agent-mcp-description]', '[data-agent-mcp-spec]'],
        skill: ['[data-agent-skill-id]', '[data-agent-skill-name]', '[data-agent-skill-description]', '[data-agent-skill-source]', '[data-agent-skill-version]'],
        prompt: ['[data-agent-prompt-id]', '[data-agent-prompt-name]', '[data-agent-prompt-description]', '[data-agent-prompt-content]'],
      }[resourceType] || [];
      selectors.forEach((selector) => {
        const field = panel.querySelector(selector);
        if (field) field.value = '';
      });
      const id = resourceIdField(resourceType);
      if (id) { id.readOnly = false; id.focus(); }
      panel.querySelector(`[data-agent-resource-editor="${resourceType}"]`)?.scrollIntoView({ block: 'nearest' });
    };
    const fillResourceEditor = (resourceType, resourceId) => {
      const resource = resourceById(resourceType, resourceId);
      if (!resource) { flash('数据库资源不存在'); return; }
      if (resourceType === 'mcp') {
        const id = panel.querySelector('[data-agent-mcp-id]');
        const name = panel.querySelector('[data-agent-mcp-name]');
        const description = panel.querySelector('[data-agent-mcp-description]');
        const spec = panel.querySelector('[data-agent-mcp-spec]');
        if (id) id.value = resource.id || '';
        if (id) id.readOnly = true;
        if (name) name.value = resource.name || resource.id || '';
        if (description) description.value = resource.description || '';
        if (spec) spec.value = JSON.stringify(resource.spec || {}, null, 2);
      } else if (resourceType === 'skill') {
        const id = panel.querySelector('[data-agent-skill-id]');
        const name = panel.querySelector('[data-agent-skill-name]');
        const source = panel.querySelector('[data-agent-skill-source]');
        const version = panel.querySelector('[data-agent-skill-version]');
        const description = panel.querySelector('[data-agent-skill-description]');
        if (id) id.value = resource.id || '';
        if (id) id.readOnly = true;
        if (name) name.value = resource.name || resource.id || '';
        if (source) source.value = resource.source || '';
        if (version) version.value = resource.version || '';
        if (description) description.value = resource.description || '';
      } else if (resourceType === 'prompt') {
        const id = panel.querySelector('[data-agent-prompt-id]');
        const name = panel.querySelector('[data-agent-prompt-name]');
        const description = panel.querySelector('[data-agent-prompt-description]');
        const content = panel.querySelector('[data-agent-prompt-content]');
        if (id) id.value = resource.id || '';
        if (id) id.readOnly = true;
        if (name) name.value = resource.name || resource.id || '';
        if (description) description.value = resource.description || '';
        if (content) content.value = resource.content || '';
      }
      panel.querySelector(`[data-agent-resource-editor="${resourceType}"]`)?.scrollIntoView({ block: 'nearest' });
    };
    const runResourceAction = async (button, action, resourceType, resourceId) => {
      if (!activeClient()) { flash('当前没有可操作客户端'); return; }
      try {
        setPending(button, true);
        const body = await requestJson('/api/agent/resources/reconcile', {
          method: 'POST',
          body: JSON.stringify({
            action,
            resource_type: resourceType,
            resource_ids: [resourceId],
            client_id: activeClient(),
            node_id: '__local__',
          }),
        });
        const summary = describeAgentReconcileResult(body || {});
        flash(summary.message, { success: summary.success });
        await loadAgentLibrary({ quiet: true });
      } catch (error) {
        flash(`资源操作失败：${error.message}`);
      } finally {
        setPending(button, false);
      }
    };
    const deleteLibraryResource = async (button, resourceType, resourceId) => {
      if (!window.confirm(`从数据库删除 ${resourceId}？已分配到本地客户端的副本会先卸载。`)) return;
      try {
        setPending(button, true);
        await requestJson(`/api/agent/resources/${encodeURIComponent(resourceType)}/${encodeURIComponent(resourceId)}`, { method: 'DELETE' });
        flash('资源已卸载并从数据库删除', { success: true });
        await loadAgentLibrary({ quiet: true });
      } catch (error) {
        flash(`从库删除失败：${error.message}`);
      } finally {
        setPending(button, false);
      }
    };
    const importDiscoveryItem = async (button, resourceType, resourceId) => {
      try {
        setPending(button, true);
        if (resourceType === 'mcp') {
          const body = await requestJson('/api/agent/mcp/import-current', {
            method: 'POST',
            body: JSON.stringify({ ...localPayload(), mcp_ids: resourceId ? [resourceId] : [] }),
          });
          flash(`已从当前客户端导入 ${body?.imported_count || 0} 个 MCP`, { success: true });
        } else if (resourceType === 'skill') {
          const item = getAgentDiscoveryForClient(libraryState.discovery, 'skills', activeClient()).find((row) => String(row.id) === String(resourceId));
          if (!item) throw new Error('发现项已变化，请刷新库存');
          await requestJson('/api/agent/skills/import-current', {
            method: 'POST',
            body: JSON.stringify({ client_id: activeClient(), skill_name: item.name || item.id, skill_id: item.id, name: item.name || item.id }),
          });
          flash(`Skill ${item.name || item.id} 已导入数据库`, { success: true });
        } else if (resourceType === 'prompt') {
          const id = panel.querySelector('[data-agent-prompt-id]')?.value?.trim() || resourceId || null;
          const name = panel.querySelector('[data-agent-prompt-name]')?.value?.trim() || null;
          const saved = await requestJson('/api/agent/prompts/import-current', {
            method: 'POST',
            body: JSON.stringify({ client_id: activeClient(), prompt_id: id, name }),
          });
          const content = panel.querySelector('[data-agent-prompt-content]');
          if (content) content.value = saved?.content || '';
          flash('当前 Prompt 已导入数据库并保存客户端变体', { success: true });
        }
        await loadAgentLibrary({ quiet: true });
      } catch (error) {
        flash(`导入失败：${error.message}`);
      } finally {
        setPending(button, false);
      }
    };
    const clearProfileEditor = () => {
      profileDraft = { profileId: '', clientId: activeClient(), existingItems: [], selections: emptyProfileSelections() };
      if (profileFields.id) { profileFields.id.value = ''; profileFields.id.readOnly = false; }
      if (profileFields.name) profileFields.name.value = '';
      if (profileFields.description) profileFields.description.value = '';
      renderProfilePicker();
    };
    const editProfile = (profileId) => {
      const profile = (libraryState.profiles || []).find((item) => String(item.id) === String(profileId));
      if (!profile) { flash('Profile 不存在'); return; }
      profileDraft = {
        profileId: profile.id,
        clientId: activeClient(),
        existingItems: (profile.items || []).map((item) => ({ ...item, config: { ...(item.config || {}) } })),
        selections: profileSelectionsForClient(profile.items || [], activeClient()),
      };
      if (profileFields.id) { profileFields.id.value = profile.id || ''; profileFields.id.readOnly = true; }
      if (profileFields.name) profileFields.name.value = profile.name || profile.id || '';
      if (profileFields.description) profileFields.description.value = profile.description || '';
      renderProfilePicker();
    };

    panel.addEventListener('click', async (event) => {
      const target = event.target.closest('button');
      if (!target || !panel.contains(target)) return;
      const resourceType = target.dataset.resourceType;
      const resourceId = target.dataset.resourceId;
      if (target.matches('[data-agent-resource-new]')) {
        const newType = target.dataset.agentResourceNew;
        if (newType === 'provider') clearProvider();
        else if (newType === 'profile') clearProfileEditor();
        else clearResourceEditor(newType);
        return;
      }
      if (target.matches('[data-agent-resource-move]')) {
        const card = target.closest('[data-agent-order-card]');
        const moveType = card?.dataset.resourceType;
        const moveId = card?.dataset.resourceId;
        if (!moveType || !moveId) return;
        const currentIds = orderIdsFor(moveType);
        const nextIds = moveAgentResourceId(currentIds, moveId, target.dataset.agentResourceMove);
        if (nextIds.join('\0') === currentIds.join('\0')) return;
        applyResourceOrderToState(moveType, nextIds);
        applyResourceOrderToDom(moveType, nextIds);
        await persistResourceOrder(moveType, nextIds, target);
        return;
      }
      if (target.matches('[data-agent-resource-edit]')) {
        fillResourceEditor(resourceType, resourceId);
        return;
      }
      if (target.matches('[data-agent-resource-install]')) {
        await runResourceAction(target, 'install', resourceType, resourceId);
        return;
      }
      if (target.matches('[data-agent-resource-uninstall]')) {
        await runResourceAction(target, 'uninstall', resourceType, resourceId);
        return;
      }
      if (target.matches('[data-agent-resource-sync]')) {
        await runResourceAction(target, 'sync', resourceType, resourceId);
        return;
      }
      if (target.matches('[data-agent-resource-delete]')) {
        await deleteLibraryResource(target, resourceType, resourceId);
        return;
      }
      if (target.matches('[data-agent-discovery-import]')) {
        await importDiscoveryItem(target, resourceType, resourceId);
        return;
      }
      if (target.matches('[data-agent-discovery-refresh="mcp"]')) {
        try {
          setPending(target, true);
          const body = await requestJson('/api/agent/mcp/scan', {
            method: 'POST',
            body: JSON.stringify({ node_ids: ['__local__'], apps: [activeClient()] }),
          });
          const count = body?.targets?.reduce((total, item) => total + (item.mcp_ids?.length || 0), 0) || 0;
          await loadAgentLibrary({ quiet: true });
          flash(`已回读 ${clientName(activeClient())}：${count} 个 MCP`, { success: true });
        } catch (error) {
          flash(`扫描失败：${error.message}`);
        } finally {
          setPending(target, false);
        }
        return;
      }
      if (target.matches('[data-agent-profile-resource]')) {
        const type = target.dataset.resourceType;
        const id = target.dataset.resourceId;
        const current = new Set(profileDraft.selections[type] || []);
        if (target.getAttribute('aria-pressed') === 'true') current.delete(id);
        else {
          if (target.dataset.resourceSingular === 'true') current.clear();
          current.add(id);
        }
        profileDraft.selections[type] = [...current];
        renderProfilePicker();
        return;
      }
      if (target.matches('[data-agent-profile-new]')) {
        clearProfileEditor();
        return;
      }
      if (target.matches('[data-agent-profile-edit]')) {
        editProfile(target.dataset.agentProfileEdit);
        return;
      }
      if (target.matches('[data-agent-profile-apply]')) {
        try {
          setPending(target, true);
          const body = await requestJson(`/api/agent/profiles/${encodeURIComponent(target.dataset.agentProfileApply)}/apply`, {
            method: 'POST',
            body: JSON.stringify({ node_id: '__local__', client_id: activeClient() }),
          });
          const summary = describeAgentReconcileResult(body || {});
          flash(summary.message, { success: summary.success });
          await loadAgentLibrary({ quiet: true });
        } catch (error) {
          flash(`Profile 应用失败：${error.message}`);
        } finally {
          setPending(target, false);
        }
        return;
      }
      if (target.matches('[data-agent-profile-delete]')) {
        const profileId = target.dataset.agentProfileDelete;
        if (!window.confirm(`删除 Profile ${profileId}？资源库和客户端配置保持不变。`)) return;
        try {
          setPending(target, true);
          await requestJson(`/api/agent/profiles/${encodeURIComponent(profileId)}`, { method: 'DELETE' });
          if (profileDraft.profileId === profileId) clearProfileEditor();
          await loadAgentLibrary({ quiet: true });
          flash('Profile 已删除', { success: true });
        } catch (error) {
          flash(`Profile 删除失败：${error.message}`);
        } finally {
          setPending(target, false);
        }
      }
    });

    let armedDragCard = null;
    let draggingCard = null;
    panel.addEventListener('pointerdown', (event) => {
      const handle = event.target.closest('[data-agent-drag-handle]');
      armedDragCard = handle?.closest('[data-agent-order-card]') || null;
    });
    panel.addEventListener('pointerup', () => { armedDragCard = null; });
    panel.addEventListener('dragstart', (event) => {
      const card = event.target.closest('[data-agent-order-card]');
      if (!card || armedDragCard !== card) {
        event.preventDefault();
        return;
      }
      draggingCard = card;
      card.classList.add('is-dragging');
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', card.dataset.resourceId || '');
      }
    });
    panel.addEventListener('dragover', (event) => {
      if (!draggingCard) return;
      const targetCard = event.target.closest('[data-agent-order-card]');
      if (!targetCard || targetCard === draggingCard) return;
      if (targetCard.dataset.resourceType !== draggingCard.dataset.resourceType) return;
      if (draggingCard.dataset.resourceType === 'provider' && targetCard.dataset.providerApp !== draggingCard.dataset.providerApp) return;
      event.preventDefault();
      panel.querySelectorAll('[data-agent-order-card].is-drop-before, [data-agent-order-card].is-drop-after').forEach((card) => card.classList.remove('is-drop-before', 'is-drop-after'));
      const after = event.clientY >= targetCard.getBoundingClientRect().top + targetCard.getBoundingClientRect().height / 2;
      targetCard.classList.add(after ? 'is-drop-after' : 'is-drop-before');
      if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
    });
    panel.addEventListener('drop', async (event) => {
      if (!draggingCard) return;
      const targetCard = event.target.closest('[data-agent-order-card]');
      if (!targetCard || targetCard === draggingCard) return;
      const resourceType = draggingCard.dataset.resourceType;
      if (targetCard.dataset.resourceType !== resourceType) return;
      if (resourceType === 'provider' && targetCard.dataset.providerApp !== draggingCard.dataset.providerApp) return;
      event.preventDefault();
      const currentIds = orderIdsFor(resourceType);
      const draggedId = draggingCard.dataset.resourceId;
      const targetId = targetCard.dataset.resourceId;
      const nextIds = currentIds.filter((resourceId) => resourceId !== draggedId);
      const targetIndex = nextIds.indexOf(targetId);
      const after = event.clientY >= targetCard.getBoundingClientRect().top + targetCard.getBoundingClientRect().height / 2;
      nextIds.splice(targetIndex + (after ? 1 : 0), 0, draggedId);
      applyResourceOrderToState(resourceType, nextIds);
      applyResourceOrderToDom(resourceType, nextIds);
      await persistResourceOrder(resourceType, nextIds);
    });
    panel.addEventListener('dragend', () => {
      panel.querySelectorAll('[data-agent-order-card].is-dragging, [data-agent-order-card].is-drop-before, [data-agent-order-card].is-drop-after').forEach((card) => card.classList.remove('is-dragging', 'is-drop-before', 'is-drop-after'));
      draggingCard = null;
      armedDragCard = null;
    });

    panel.querySelector('[data-agent-mcp-format]')?.addEventListener('click', () => {
      const field = panel.querySelector('[data-agent-mcp-spec]');
      try { field.value = JSON.stringify(JSON.parse(field.value || '{}'), null, 2); flash('JSON 已格式化', { success: true }); }
      catch (error) { flash(`JSON 无效：${error.message}`); }
    });
    panel.querySelector('[data-agent-mcp-save]')?.addEventListener('click', async (event) => {
      const trigger = event.currentTarget;
      const id = panel.querySelector('[data-agent-mcp-id]')?.value?.trim();
      const name = panel.querySelector('[data-agent-mcp-name]')?.value?.trim() || id;
      let spec;
      try { spec = JSON.parse(panel.querySelector('[data-agent-mcp-spec]')?.value || '{}'); }
      catch (error) { flash(`Spec JSON 无效：${error.message}`); return; }
      if (!id || !Object.keys(spec).length) { flash('先填写 MCP ID 和 Spec'); return; }
      try {
        setPending(trigger, true);
        await requestJson('/api/agent/mcp/servers', {
          method: 'POST',
          body: JSON.stringify(buildMcpSavePayload({
            serverId: id,
            name,
            description: panel.querySelector('[data-agent-mcp-description]')?.value?.trim() || '',
            spec,
          })),
        });
        const idField = resourceIdField('mcp');
        if (idField) idField.readOnly = true;
        await loadAgentLibrary({ quiet: true });
        flash('MCP 已保存到数据库；客户端配置未改动', { success: true });
      } catch (error) { flash(`保存失败：${error.message}`); }
      finally { setPending(trigger, false); }
    });
    panel.querySelector('[data-agent-skill-import-source]')?.addEventListener('click', async (event) => {
      const trigger = event.currentTarget;
      const id = panel.querySelector('[data-agent-skill-id]')?.value?.trim();
      const name = panel.querySelector('[data-agent-skill-name]')?.value?.trim() || id;
      const source = panel.querySelector('[data-agent-skill-source]')?.value?.trim();
      if (!id || !source) { flash('先填写 Skill ID 和来源'); return; }
      try {
        setPending(trigger, true);
        await requestJson('/api/agent/skills/import-source', {
          method: 'POST',
          body: JSON.stringify({
            skill_id: id,
            name,
            source,
            version: panel.querySelector('[data-agent-skill-version]')?.value?.trim() || null,
            description: panel.querySelector('[data-agent-skill-description]')?.value?.trim() || null,
          }),
        });
        const idField = resourceIdField('skill');
        if (idField) idField.readOnly = true;
        await loadAgentLibrary({ quiet: true });
        flash('Skill 已导入统一 SSOT；客户端配置未改动', { success: true });
      } catch (error) { flash(`Skill 入库失败：${error.message}`); }
      finally { setPending(trigger, false); }
    });
    panel.querySelector('[data-agent-prompt-save]')?.addEventListener('click', async (event) => {
      const trigger = event.currentTarget;
      const id = panel.querySelector('[data-agent-prompt-id]')?.value?.trim();
      const name = panel.querySelector('[data-agent-prompt-name]')?.value?.trim() || id;
      const description = panel.querySelector('[data-agent-prompt-description]')?.value?.trim() || null;
      const content = panel.querySelector('[data-agent-prompt-content]')?.value || '';
      if (!id || !content.trim()) { flash('先填写 Prompt ID 和内容'); return; }
      try {
        setPending(trigger, true);
        await requestJson('/api/agent/prompts', { method: 'POST', body: JSON.stringify({ prompt_id: id, name, description, content }) });
        const idField = resourceIdField('prompt');
        if (idField) idField.readOnly = true;
        await loadAgentLibrary({ quiet: true });
        flash('Prompt 已保存到数据库；客户端文件未改动', { success: true });
      } catch (error) { flash(`保存失败：${error.message}`); }
      finally { setPending(trigger, false); }
    });
    panel.querySelector('[data-agent-profile-save]')?.addEventListener('click', async (event) => {
      const trigger = event.currentTarget;
      const profileId = profileFields.id?.value?.trim();
      const name = profileFields.name?.value?.trim();
      if (!profileId || !name) { flash('先填写 Profile ID 和名称'); return; }
      const payload = buildAgentProfilePayload({
        name,
        description: profileFields.description?.value || '',
        clientId: activeClient(),
        existingItems: profileDraft.existingItems,
        selections: profileDraft.selections,
        routerConfig: routerProfileConfig(),
      });
      try {
        setPending(trigger, true);
        const saved = await putJson(`/api/agent/profiles/${encodeURIComponent(profileId)}`, payload);
        profileDraft = {
          profileId: saved.id,
          clientId: activeClient(),
          existingItems: saved.items || [],
          selections: profileSelectionsForClient(saved.items || [], activeClient()),
        };
        if (profileFields.id) profileFields.id.readOnly = true;
        await loadAgentLibrary({ quiet: true });
        flash('Profile 已保存到数据库', { success: true });
      } catch (error) { flash(`Profile 保存失败：${error.message}`); }
      finally { setPending(trigger, false); }
    });
    clearProfileEditor();
    renderAgentLibraryView();
    void loadAgentLibrary({ quiet: true });
  }

  function initNotificationPanel() {
    document.querySelectorAll('[data-notification-panel], [data-notification-dialog]').forEach((panel) => {
      wireNotificationPanel(panel);
    });
  }

  function wireNotificationPanel(panel) {
    const endpoint = panel.dataset.notificationEndpoint;
    const enabled = panel.querySelector('[data-notification-enabled]');
    const title = panel.querySelector('[data-notification-title]');
    const template = panel.querySelector('[data-notification-template]');
    const preview = panel.querySelector('[data-notification-preview]');
    const result = panel.querySelector('[data-notification-result]');
    const save = panel.querySelector('[data-notification-save]');
    const send = panel.querySelector('[data-notification-send]');
    const load = panel.querySelector('[data-notification-load]');
    const detailLink = panel.querySelector('[data-notification-detail-link]');
    if (!enabled || !title || !template || !preview || !save || !send) {
      return;
    }

    function setEndpoint(nextEndpoint, assetId) {
      panel.dataset.notificationEndpoint = nextEndpoint || '';
      panel.dataset.assetId = assetId || '';
      if (detailLink && assetId) {
        detailLink.href = `/assets/${encodeURIComponent(assetId)}#notification`;
      }
    }

    function currentEndpoint() {
      return panel.dataset.notificationEndpoint || endpoint;
    }

    function render(payload) {
      if (!payload) {
        return;
      }
      enabled.checked = Boolean(payload.config.enabled);
      title.value = payload.config.title || '';
      template.value = payload.config.template || '';
      preview.textContent = payload.preview.content || '';
    }

    async function loadConfig() {
      const activeEndpoint = currentEndpoint();
      if (!activeEndpoint) {
        return;
      }
      try {
        const payload = await requestJson(activeEndpoint);
        render(payload);
      } catch (error) {
        preview.textContent = `通知配置加载失败：${error.message}`;
      }
    }

    async function saveConfig() {
      const activeEndpoint = currentEndpoint();
      if (!activeEndpoint) {
        return;
      }
      setPending(save, true);
      try {
        const payload = await requestJson(activeEndpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: enabled.checked, title: title.value, template: template.value }),
        });
        render(payload);
        setFlash(result, '通知内容已保存', { success: true });
      } catch (error) {
        showAgentFlash(`保存失败：${error.message}`);
      } finally {
        setPending(save, false);
      }
    }

    async function sendNotification() {
      const activeEndpoint = currentEndpoint();
      if (!activeEndpoint) {
        return;
      }
      setPending(send, true);
      try {
        const payload = await requestJson(`${activeEndpoint}/send`, { method: 'POST' });
        setFlash(result, `通知任务已入队：${payload.task.id}`, { success: true });
      } catch (error) {
        setFlash(result, `发送失败：${error.message}`);
      } finally {
        setPending(send, false);
      }
    }

    save?.addEventListener('click', saveConfig);
    send?.addEventListener('click', sendNotification);
    load?.addEventListener('click', loadConfig);
    panel.notificationController = { loadConfig, setEndpoint };
    if (endpoint) {
      loadConfig();
    }
  }

  function initNotificationDialog() {
    const dialog = document.querySelector('[data-notification-dialog]');
    if (!dialog) {
      return;
    }
    const close = dialog.querySelector('[data-notification-dialog-close]');
    const title = dialog.querySelector('[data-dialog-title]');
    close?.addEventListener('click', () => dialog.close());
    document.addEventListener('click', (event) => {
      const trigger = event.target.closest('[data-notification-edit]');
      if (!trigger) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      if (title) {
        title.textContent = `${trigger.dataset.notificationName || trigger.dataset.notificationEdit} · 微信通知模板`;
      }
      dialog.notificationController?.setEndpoint(trigger.dataset.notificationEndpoint, trigger.dataset.notificationEdit);
      dialog.notificationController?.loadConfig();
      if (typeof dialog.showModal === 'function') {
        dialog.showModal();
      } else {
        dialog.setAttribute('open', '');
      }
    });
  }

  function terminalName(session) {
    if (session.id === 'system') {
      return '系统日志';
    }
    if (session.title) {
      return session.title;
    }
    const shell = session.shell.split('/').pop() || session.shell;
    return `${shell} ${session.id.slice(0, 6)}`;
  }

  function initTerminalWorkbench() {
    const terminalWorkbench = document.querySelector('[data-terminal-workbench]');
    if (!terminalWorkbench) {
      return;
    }

    const terminalTabs = document.getElementById('debug-terminals');
    const terminalViewports = document.getElementById('terminal-viewports');
    const createButton = document.getElementById('create-debug-terminal');
    const uploadInput = document.querySelector('[data-terminal-upload]');
    const uploadStatus = document.querySelector('[data-terminal-upload-status]');
    const terminals = new Map();

    function createTerminalTab(session) {
      const tab = document.createElement('button');
      tab.className = 'terminal-tab';
      tab.setAttribute('aria-label', terminalName(session));
      tab.type = 'button';
      tab.dataset.terminalTab = '';
      tab.dataset.terminalId = session.id;

      const icon = document.createElement('span');
      icon.className = 'terminal-tab-icon';
      icon.textContent = session.id === 'system' ? '●' : '▸';

      const title = document.createElement('span');
      title.className = 'terminal-tab-title';
      title.textContent = terminalName(session);
      if (session.id !== 'system') {
        title.title = '双击改名';
        title.dataset.terminalRename = '';
      }

      tab.append(icon, title);
      if (session.id === 'system') {
        const badge = document.createElement('span');
        badge.className = 'terminal-tab-badge';
        badge.textContent = '只读';
        tab.append(badge);
      } else {
        const close = document.createElement('span');
        close.className = 'terminal-tab-close';
        close.dataset.terminalClose = '';
        close.setAttribute('role', 'button');
        close.setAttribute('aria-label', `关闭 ${terminalName(session)}`);
        close.textContent = '×';
        tab.append(close);
      }
      return tab;
    }

    function createTerminalPane(session) {
      const pane = document.createElement('section');
      pane.className = 'terminal-pane';
      pane.dataset.terminalPane = '';
      pane.dataset.terminalId = session.id;

      const header = document.createElement('div');
      header.className = 'terminal-pane-header';
      header.innerHTML = `<span data-terminal-pane-title>${terminalName(session)}</span><small>${session.cwd || ''}</small>`;

      const screen = document.createElement('div');
      screen.className = session.id === 'system' ? 'terminal-screen terminal-screen--system' : 'terminal-screen';
      screen.dataset.terminalScreen = '';
      if (session.id === 'system') {
        screen.dataset.systemTerminalScreen = '';
      }
      screen.setAttribute('aria-label', `${terminalName(session)} terminal`);
      pane.append(header, screen);
      return { pane, screen };
    }

    function sendTerminalResize(socket, terminal) {
      if (!socket || socket.readyState !== WebSocket.OPEN || !terminal) {
        return;
      }
      socket.send(JSON.stringify({ type: 'resize', cols: terminal.cols, rows: terminal.rows }));
    }

    function terminalTheme() {
      return {
        background: '#07101f',
        foreground: '#e6edf7',
        cursor: '#7dd3fc',
        selectionBackground: '#2d5f94',
        black: '#09111f',
        red: '#ff6b7a',
        green: '#63e6be',
        yellow: '#ffd166',
        blue: '#74c0fc',
        magenta: '#c084fc',
        cyan: '#67e8f9',
        white: '#edf2f7',
        brightBlack: '#64748b',
        brightRed: '#ffa8b5',
        brightGreen: '#8ce99a',
        brightYellow: '#ffe066',
        brightBlue: '#a5d8ff',
        brightMagenta: '#d0bfff',
        brightCyan: '#99f6e4',
        brightWhite: '#ffffff',
      };
    }

    function createTerminalWriter(terminal) {
      let pending = '';
      let frame = 0;
      const flush = () => {
        frame = 0;
        const chunk = pending;
        pending = '';
        if (chunk) {
          terminal.write(chunk);
        }
      };
      return {
        write(chunk) {
          if (!chunk) {
            return;
          }
          pending += chunk;
          if (!frame) {
            frame = requestAnimationFrame(flush);
          }
        },
        writeln(line) {
          this.write(`${line}\r\n`);
        },
      };
    }

    function stripAnsi(value) {
      return value.replace(/\x1B\[[0-?]*[ -/]*[@-~]/g, '').replace(/\x1B\][^\x07]*(\x07|\x1B\\)/g, '');
    }

    function classifyLogLine(line) {
      const clean = stripAnsi(line).trim();
      const lower = clean.toLowerCase();
      if (!clean) {
        return null;
      }
      if (/\b(error|failed|failure|exception|traceback|denied|invalid|panic|fatal|conflict)\b|失败|报错|异常|拒绝|错误/.test(lower)) {
        return { level: 'error', label: '错误' };
      }
      if (/\b(warn|warning|retry|waiting|timeout|skip|skipped)\b|警告|等待|重试|跳过/.test(lower)) {
        return { level: 'warning', label: '提醒' };
      }
      if (/^\s*(│\s*)?\$\s+/.test(clean) || /^\+\s+/.test(clean)) {
        return { level: 'command', label: '命令' };
      }
      if (/\b(success|succeeded|complete|completed|healthy|active|started|accepted|200 ok|204|202 accepted)\b|成功|完成|已入队|已启动|正常/.test(lower)) {
        return { level: 'success', label: '成功' };
      }
      if (/\b(info:|http\/1\.1|get |post |put |delete )\b/.test(lower)) {
        return { level: 'network', label: '请求' };
      }
      return { level: 'info', label: '信息' };
    }

    function createSystemLogRenderer(root) {
      if (!root) {
        return { write() {} };
      }
      const feed = root.querySelector('[data-system-log-feed]');
      const search = root.querySelector('[data-system-log-search]');
      const filterButtons = Array.from(root.querySelectorAll('[data-log-filter]'));
      const viewButtons = Array.from(root.querySelectorAll('[data-log-view]'));
      const viewPanels = Array.from(root.querySelectorAll('[data-log-view-panel]'));
      const autoscrollButton = root.querySelector('[data-log-autoscroll]');
      const statNodes = new Map(Array.from(root.querySelectorAll('[data-system-log-stat]')).map((item) => [item.dataset.systemLogStat, item]));
      if (!feed) {
        return { write() {} };
      }

      const state = {
        buffer: '',
        filter: 'all',
        query: '',
        autoscroll: true,
        entries: [],
        pendingEntries: [],
        scheduled: 0,
        currentTask: null,
        counts: { total: 0, error: 0, warning: 0, success: 0, command: 0, network: 0, info: 0 },
      };

      function timestamp() {
        return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date());
      }

      function cleanLine(line) {
        return stripAnsi(line).replace(/\s+$/g, '').trim();
      }

      function valueAfter(line, marker) {
        return line.slice(marker.length).trim();
      }

      function matches(entry) {
        const filterMatched = state.filter === 'all' || entry.level === state.filter || entry.tags?.has(state.filter);
        const queryMatched = !state.query || entry.searchText.includes(state.query);
        return filterMatched && queryMatched;
      }

      function applyFilters() {
        state.entries.forEach((entry) => {
          entry.node.hidden = !matches(entry);
        });
      }

      function updateStats() {
        Object.entries(state.counts).forEach(([key, value]) => {
          const node = statNodes.get(key);
          if (node) {
            node.textContent = String(value);
          }
        });
      }

      function setView(mode) {
        root.dataset.logViewMode = mode;
        viewPanels.forEach((panel) => {
          const active = panel.dataset.logViewPanel === mode;
          panel.hidden = !active;
          panel.classList.toggle('is-active', active);
        });
        viewButtons.forEach((button) => button.classList.toggle('is-active', button.dataset.logView === mode));
        if (mode === 'raw') {
          scheduleTerminalRefit(terminals.get('system'));
        }
      }

      function createSimpleEntry(line) {
        const meta = classifyLogLine(line);
        if (!meta) {
          return null;
        }
        const message = cleanLine(line);
        const node = document.createElement('article');
        node.className = `system-log-entry system-log-entry--${meta.level}`;

        const badge = document.createElement('span');
        badge.className = 'system-log-badge';
        badge.textContent = meta.label;

        const time = document.createElement('time');
        time.textContent = timestamp();

        const humanMessage = humanizeLogText(message);
        if (!humanMessage) {
          return null;
        }
        const body = document.createElement('pre');
        body.textContent = humanMessage;

        node.append(badge, time, body);
        return {
          level: meta.level,
          tags: new Set([meta.level]),
          node,
          searchText: message.toLowerCase(),
        };
      }

      function humanizeLogText(text) {
        const cleaned = text
          .replace(/^│\s*/g, '')
          .replace(/^├─\s*/g, '')
          .replace(/^└─\s*/g, '')
          .replace(/\s+->\s+/g, '  →  ')
          .trim();
        const lower = cleaned.toLowerCase();
        if (lower.includes('command not found: npm')) return '没有找到 npm：远程 shell 没加载 Node/nvm 环境';
        if (lower.includes('command not found: node')) return '没有找到 node：远程 shell 没加载 Node/nvm 环境';
        if (lower.includes('could not resolve hostname')) return 'SSH 目标地址解析失败：节点地址或测试配置不对';
        if (lower.includes('identity file') && lower.includes('not accessible')) return 'SSH Key 文件不存在或不可读';
        if (lower.includes('operation not permitted') && lower.includes('codex.exe')) return 'Windows 正在占用旧 codex.exe，npm 清理残留目录失败';
        if (lower.startsWith('npm timing ')) return '';
        if (cleaned.startsWith('changed ') && lower.includes('packages in')) {
          return cleaned.replace('changed', '已安装/更新').replace('packages in', '个包，用时').replace('package in', '个包，用时');
        }
        if (lower.startsWith('worker failed: command')) return '执行命令失败：原始命令已保存到任务详情';
        return cleaned;
      }

      function createTaskEntry(title) {
        const node = document.createElement('article');
        node.className = 'system-log-task system-log-task--running';
        node.innerHTML = `
          <div class="system-log-task-head">
            <span class="system-log-task-status">进行中</span>
            <div class="system-log-task-title"><strong></strong><small></small></div>
            <time>${timestamp()}</time>
          </div>
          <div class="system-log-task-fields"></div>
          <div class="system-log-task-body"></div>
        `;
        node.querySelector('.system-log-task-title strong').textContent = title || '任务开始';
        node.querySelector('.system-log-task-title small').textContent = '正在整理执行流程…';
        return {
          level: 'info',
          tags: new Set(['info']),
          node,
          fields: node.querySelector('.system-log-task-fields'),
          body: node.querySelector('.system-log-task-body'),
          title: node.querySelector('.system-log-task-title strong'),
          subtitle: node.querySelector('.system-log-task-title small'),
          status: node.querySelector('.system-log-task-status'),
          searchText: title.toLowerCase(),
        };
      }

      function updateEntrySearch(entry, text) {
        entry.searchText = `${entry.searchText} ${text}`.toLowerCase();
      }

      function markEntry(entry, level, label) {
        entry.level = level;
        entry.tags.add(level);
        entry.node.classList.remove('system-log-task--running', 'system-log-task--success', 'system-log-task--error', 'system-log-task--warning');
        entry.node.classList.add(`system-log-task--${level}`);
        if (entry.status) {
          entry.status.textContent = label;
        }
        entry.node.hidden = !matches(entry);
      }

      function addField(entry, label, value, kind = '') {
        if (!value) return;
        const item = document.createElement('span');
        item.className = `system-log-field ${kind ? `system-log-field--${kind}` : ''}`;
        item.innerHTML = `<small></small><strong></strong>`;
        item.querySelector('small').textContent = label;
        item.querySelector('strong').textContent = value;
        entry.fields.append(item);
        updateEntrySearch(entry, `${label} ${value}`);
      }

      function addFlow(entry, value) {
        if (!value) return;
        const item = document.createElement('div');
        item.className = 'system-log-flow';
        const label = document.createElement('small');
        label.textContent = '流程';
        const chain = document.createElement('div');
        value.split(/\s*->\s*|\s*→\s*/).filter(Boolean).forEach((part, index) => {
          if (index > 0) {
            const arrow = document.createElement('i');
            arrow.textContent = '→';
            chain.append(arrow);
          }
          const chip = document.createElement('span');
          chip.textContent = part.trim();
          chain.append(chip);
        });
        item.append(label, chain);
        entry.fields.append(item);
        updateEntrySearch(entry, value);
      }

      function addTaskLine(entry, level, label, text) {
        const row = document.createElement('div');
        row.className = `system-log-task-line system-log-task-line--${level}`;
        row.innerHTML = `<span></span><pre></pre>`;
        row.querySelector('span').textContent = label;
        row.querySelector('pre').textContent = text;
        entry.body.append(row);
        entry.tags.add(level);
        updateEntrySearch(entry, `${label} ${text}`);
        entry.node.hidden = !matches(entry);
      }

      function appendEntry(entry) {
        if (!entry) return;
        state.pendingEntries.push(entry);
        if (!state.scheduled) {
          state.scheduled = requestAnimationFrame(flushEntries);
        }
      }

      function trimEntries() {
        while (state.entries.length > 160) {
          const removed = state.entries.shift();
          removed?.node.remove();
        }
      }

      function flushEntries() {
        state.scheduled = 0;
        if (!state.pendingEntries.length) {
          return;
        }
        const empty = feed.querySelector('.system-log-empty');
        empty?.remove();
        const fragment = document.createDocumentFragment();
        for (const entry of state.pendingEntries.splice(0)) {
          state.entries.push(entry);
          state.counts.total += 1;
          state.counts[entry.level] = (state.counts[entry.level] || 0) + 1;
          entry.node.hidden = !matches(entry);
          fragment.append(entry.node);
        }
        feed.append(fragment);
        trimEntries();
        updateStats();
        if (state.autoscroll) {
          feed.scrollTop = feed.scrollHeight;
        }
      }

      function bumpLevelCount(level) {
        if (level !== 'info') {
          state.counts[level] = (state.counts[level] || 0) + 1;
          updateStats();
        }
      }

      function handleTaskLine(line) {
        const clean = cleanLine(line);
        if (!clean) return true;
        if (clean.startsWith('┌─ 任务开始：')) {
          const title = valueAfter(clean, '┌─ 任务开始：');
          const entry = createTaskEntry(title);
          state.currentTask = entry;
          appendEntry(entry);
          return true;
        }
        const task = state.currentTask;
        if (!task) return false;

        if (clean.startsWith('│  项目：')) {
          const value = valueAfter(clean, '│  项目：');
          task.subtitle.textContent = value;
          addField(task, '项目', value, 'primary');
          return true;
        }
        if (clean.startsWith('│  版本：')) {
          addField(task, '版本', valueAfter(clean, '│  版本：'));
          return true;
        }
        if (clean.startsWith('│  设备：')) {
          addField(task, '设备', valueAfter(clean, '│  设备：'), 'primary');
          return true;
        }
        if (clean.startsWith('│  类型：')) {
          addField(task, '类型', valueAfter(clean, '│  类型：'));
          return true;
        }
        if (clean.startsWith('│  对象：')) {
          addField(task, '对象', valueAfter(clean, '│  对象：'), 'primary');
          return true;
        }
        if (clean.startsWith('│  流程：')) {
          addFlow(task, valueAfter(clean, '│  流程：'));
          return true;
        }
        if (clean.startsWith('│  任务ID：')) {
          addField(task, '任务ID', valueAfter(clean, '│  任务ID：'), 'muted');
          return true;
        }
        if (clean.startsWith('├─ 步骤') || clean.startsWith('├─ 收尾')) {
          const text = clean.replace(/^├─\s*/, '');
          addTaskLine(task, 'command', '步骤', text);
          bumpLevelCount('command');
          return true;
        }
        if (clean.startsWith('│  命令：')) {
          const text = humanizeLogText(valueAfter(clean, '│  命令：'));
          if (text) addTaskLine(task, 'command', '命令', text);
          return true;
        }
        if (clean.startsWith('│  操作：')) {
          addTaskLine(task, 'command', '操作', valueAfter(clean, '│  操作：'));
          return true;
        }
        if (clean.startsWith('│  输出：')) {
          const text = humanizeLogText(valueAfter(clean, '│  输出：'));
          if (text) addTaskLine(task, 'info', '输出', text);
          return true;
        }
        if (clean.startsWith('│  报错：')) {
          const text = humanizeLogText(valueAfter(clean, '│  报错：'));
          if (text) addTaskLine(task, 'error', '报错', text);
          markEntry(task, 'error', '异常');
          bumpLevelCount('error');
          return true;
        }
        if (clean.startsWith('│  诊断：')) {
          addTaskLine(task, 'error', '诊断', valueAfter(clean, '│  诊断：'));
          markEntry(task, 'error', '异常');
          bumpLevelCount('error');
          return true;
        }
        if (clean.startsWith('│  建议：')) {
          addTaskLine(task, 'warning', '建议', valueAfter(clean, '│  建议：'));
          task.tags.add('warning');
          bumpLevelCount('warning');
          return true;
        }
        if (clean.startsWith('│  提醒：')) {
          addTaskLine(task, 'warning', '提醒', valueAfter(clean, '│  提醒：'));
          task.tags.add('warning');
          bumpLevelCount('warning');
          return true;
        }
        if (clean.startsWith('│  重试：')) {
          addTaskLine(task, 'warning', '重试', valueAfter(clean, '│  重试：'));
          task.tags.add('warning');
          bumpLevelCount('warning');
          return true;
        }
        if (clean.startsWith('│  完成：')) {
          addTaskLine(task, 'success', '完成', valueAfter(clean, '│  完成：'));
          task.tags.add('success');
          return true;
        }
        if (clean.startsWith('│  结果：失败') || clean.startsWith('└─ 任务失败') || clean.startsWith('└─ 收尾失败') || clean.startsWith('└─ 执行器异常') || clean.startsWith('└─ 结论：❌')) {
          addTaskLine(task, 'error', '失败', humanizeLogText(clean));
          markEntry(task, 'error', '失败');
          bumpLevelCount('error');
          state.currentTask = null;
          return true;
        }
        if (clean.startsWith('└─ 任务完成：') || clean.startsWith('└─ 结论：✅')) {
          addTaskLine(task, 'success', '完成', humanizeLogText(clean).replace(/^结论：✅\s*/, ''));
          markEntry(task, 'success', '成功');
          bumpLevelCount('success');
          state.currentTask = null;
          return true;
        }
        if (clean.startsWith('│  ')) {
          const text = humanizeLogText(clean);
          if (text) addTaskLine(task, 'info', '信息', text);
          return true;
        }
        return false;
      }

      function enqueue(line) {
        if (handleTaskLine(line)) {
          if (state.autoscroll) feed.scrollTop = feed.scrollHeight;
          return;
        }
        const entry = createSimpleEntry(line);
        appendEntry(entry);
      }

      filterButtons.forEach((button) => {
        button.addEventListener('click', () => {
          state.filter = button.dataset.logFilter || 'all';
          filterButtons.forEach((item) => item.classList.toggle('is-active', item === button));
          applyFilters();
        });
      });

      viewButtons.forEach((button) => {
        button.addEventListener('click', () => setView(button.dataset.logView || 'events'));
      });

      search?.addEventListener('input', debounce(() => {
        state.query = search.value.trim().toLowerCase();
        applyFilters();
      }, 120));

      autoscrollButton?.addEventListener('click', () => {
        state.autoscroll = !state.autoscroll;
        autoscrollButton.dataset.logAutoscroll = state.autoscroll ? 'true' : 'false';
        autoscrollButton.textContent = `自动滚动：${state.autoscroll ? '开' : '关'}`;
        if (state.autoscroll) {
          feed.scrollTop = feed.scrollHeight;
        }
      });

      return {
        write(chunk) {
          if (!chunk) {
            return;
          }
          state.buffer += chunk.replace(/\r/g, '\n');
          const lines = state.buffer.split('\n');
          state.buffer = lines.pop() || '';
          lines.forEach(enqueue);
        },
      };
    }

    function createXterm(screen, { readOnly = false } = {}) {
      if (!window.Terminal) {
        screen.textContent = 'xterm.js 未加载，终端无法启动。';
        return { terminal: null, fitAddon: null };
      }
      const terminal = new window.Terminal({
        cursorBlink: !readOnly,
        cursorStyle: 'block',
        disableStdin: readOnly,
        fontFamily: '"Noto Sans Mono CJK SC", "WenQuanYi Zen Hei Mono", "Sarasa Mono SC", "Ubuntu Mono", "JetBrains Mono", "Cascadia Code", "SFMono-Regular", Consolas, monospace',
        fontSize: 14,
        fontWeight: 400,
        fontWeightBold: 700,
        lineHeight: 1.18,
        letterSpacing: 0,
        scrollback: 12000,
        convertEol: readOnly,
        allowTransparency: true,
        allowProposedApi: true,
        rescaleOverlappingGlyphs: true,
        minimumContrastRatio: 1,
        smoothScrollDuration: 0,
        theme: terminalTheme(),
      });
      try {
        if (window.Unicode11Addon?.Unicode11Addon) {
          terminal.loadAddon(new window.Unicode11Addon.Unicode11Addon());
        }
        if (terminal.unicode && terminal.unicode.versions.includes('11')) {
          terminal.unicode.activeVersion = '11';
        }
      } catch (_error) {
        // xterm unicode provider may be unavailable in bundled builds.
      }
      const fitAddon = window.FitAddon ? new window.FitAddon.FitAddon() : null;
      if (fitAddon) {
        terminal.loadAddon(fitAddon);
      }
      terminal.open(screen);
      if (fitAddon) {
        fitAddon.fit();
      }
      return { terminal, fitAddon };
    }

    function connectSystemTerminal(screen) {
      const { terminal, fitAddon } = createXterm(screen, { readOnly: true });
      if (!terminal) {
        return { eventSource: null, terminal: null, fitAddon: null, socket: null };
      }
      const writer = createTerminalWriter(terminal);
      const visualLog = createSystemLogRenderer(screen.closest('[data-system-log-workspace]'));
      writer.writeln('\x1b[38;5;110m系统日志已连接。任务输出会显示在这里。\x1b[0m');
      visualLog.write('系统日志已连接。任务输出会显示在这里。\n');
      const eventSource = new EventSource('/api/terminals/system/stream');
      eventSource.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          const chunk = payload.chunk || '';
          writer.write(chunk);
          visualLog.write(chunk);
        } catch (_error) {
          writer.write(event.data);
          visualLog.write(`${event.data}\n`);
        }
      };
      eventSource.onerror = () => {
        writer.writeln('\x1b[38;5;203m[系统日志连接等待重试]\x1b[0m');
        visualLog.write('[系统日志连接等待重试]\n');
      };
      return { eventSource, terminal, fitAddon, socket: null };
    }

    function connectDebugTerminal(session, screen) {
      const { terminal, fitAddon } = createXterm(screen);
      if (!terminal) {
        return { socket: null, terminal: null, fitAddon: null };
      }

      const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
      const writer = createTerminalWriter(terminal);
      const socket = new WebSocket(`${protocol}://${window.location.host}/api/terminals/debug/${session.id}/ws`);
      socket.onmessage = (event) => writer.write(event.data);
      socket.onopen = () => {
        terminal.focus();
        scheduleTerminalRefit(terminals.get(session.id), { focus: true });
      };
      socket.onclose = () => {
        screen.dataset.closed = 'true';
        writer.write('\r\n\x1b[38;5;244m[连接已关闭]\x1b[0m\r\n');
      };
      terminal.onData((chunk) => socket.readyState === WebSocket.OPEN && socket.send(chunk));
      terminal.onResize(() => sendTerminalResize(socket, terminal));

      screen.addEventListener('click', () => terminal.focus());
      return { socket, terminal, fitAddon, eventSource: null };
    }

    function refitTerminal(entry, { focus = false } = {}) {
      if (!entry?.terminal) {
        return;
      }
      if (!entry.pane.classList.contains('is-active')) {
        return;
      }
      if (entry.fitAddon) {
        try {
          entry.fitAddon.fit();
        } catch (_error) {
          // xterm may not have measurable dimensions while the pane is being shown.
        }
      }
      sendTerminalResize(entry.socket, entry.terminal);
      if (focus && entry.session.id !== 'system') {
        entry.terminal.focus();
      }
    }

    function scheduleTerminalRefit(entry, options = {}) {
      if (!entry) {
        return;
      }
      const run = () => refitTerminal(entry, options);
      requestAnimationFrame(() => {
        run();
        window.setTimeout(run, 80);
      });
    }

    function activeWritableTerminal() {
      const active = Array.from(terminals.values()).find((item) => item.pane.classList.contains('is-active'));
      if (active && active.session.id !== 'system' && active.socket?.readyState === WebSocket.OPEN) {
        return active;
      }
      return Array.from(terminals.values()).find((item) => item.session.id !== 'system' && item.socket?.readyState === WebSocket.OPEN) || null;
    }

    async function uploadTerminalFile(file) {
      if (!file) {
        return;
      }
      if (uploadStatus) {
        uploadStatus.hidden = false;
        uploadStatus.textContent = `正在上传 ${file.name}…`;
      }
      const body = new FormData();
      body.append('file', file);
      try {
        const response = await fetch('/api/terminals/uploads', {
          method: 'POST',
          headers: { Accept: 'application/json' },
          body,
        });
        if (redirectIfUnauthorized(response)) {
          return;
        }
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.detail || '上传失败');
        }
        const insertText = payload.path;
        const target = activeWritableTerminal();
        if (target) {
          target.socket.send(insertText);
          target.terminal.focus();
        }
        if (navigator.clipboard?.writeText) {
          try {
            await navigator.clipboard.writeText(insertText);
          } catch (_error) {
            // Clipboard permission may be unavailable on plain HTTP.
          }
        }
        if (uploadStatus) {
          uploadStatus.textContent = target
            ? `已上传并插入当前终端：${insertText}`
            : `已上传：${insertText}。新建终端后可复制这个路径给 Codex。`;
        }
      } catch (error) {
        if (uploadStatus) {
          uploadStatus.textContent = `上传失败：${error.message}`;
        }
      } finally {
        if (uploadInput) {
          uploadInput.value = '';
        }
      }
    }

    function renderTerminal(session) {
      if (!terminalTabs || !terminalViewports || terminals.has(session.id)) {
        return;
      }

      const existingSystemTab = terminalTabs.querySelector('[data-terminal-id="system"]');
      const existingSystemPane = terminalViewports.querySelector('[data-terminal-id="system"]');
      const tab = session.id === 'system' && existingSystemTab ? existingSystemTab : createTerminalTab(session);
      const panePayload =
        session.id === 'system' && existingSystemPane
          ? { pane: existingSystemPane, screen: existingSystemPane.querySelector('[data-terminal-screen]') }
          : createTerminalPane(session);

      if (!tab.isConnected) {
        terminalTabs.append(tab);
      }
      if (!panePayload.pane.isConnected) {
        terminalViewports.append(panePayload.pane);
      }

      const connection = session.id === 'system' ? connectSystemTerminal(panePayload.screen) : connectDebugTerminal(session, panePayload.screen);
      terminals.set(session.id, {
        session,
        tab,
        pane: panePayload.pane,
        screen: panePayload.screen,
        ...connection,
      });
      scheduleTerminalRefit(terminals.get(session.id));
    }

    function activateTerminal(sessionId) {
      terminals.forEach((entry, id) => {
        const active = id === sessionId;
        entry.tab.classList.toggle('is-active', active);
        entry.pane.classList.toggle('is-active', active);
        if (active) {
          scheduleTerminalRefit(entry, { focus: id !== 'system' });
        }
      });
    }

    async function loadDebugSessions() {
      const response = await fetch('/api/terminals/debug');
      if (redirectIfUnauthorized(response) || !response.ok) {
        return;
      }
      const sessions = await response.json();
      sessions.forEach(renderTerminal);
      activateTerminal('system');
    }

    async function createDebugSession() {
      const response = await fetch('/api/terminals/debug', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      if (redirectIfUnauthorized(response) || !response.ok) {
        return;
      }
      const session = await response.json();
      renderTerminal(session);
      activateTerminal(session.id);
    }

    async function renameTerminal(sessionId) {
      if (sessionId === 'system') {
        return;
      }
      const terminal = terminals.get(sessionId);
      if (!terminal) {
        return;
      }
      const currentName = terminalName(terminal.session);
      const nextName = window.prompt('终端名称', currentName);
      if (!nextName || nextName.trim() === currentName) {
        return;
      }
      const response = await fetch(`/api/terminals/debug/${sessionId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ title: nextName.trim() }),
      });
      if (redirectIfUnauthorized(response) || !response.ok) {
        return;
      }
      const updated = await response.json();
      terminal.session = updated;
      const title = terminal.tab.querySelector('[data-terminal-rename]');
      if (title) {
        title.textContent = terminalName(updated);
      }
      const paneTitle = terminal.pane.querySelector('[data-terminal-pane-title]');
      if (paneTitle) {
        paneTitle.textContent = terminalName(updated);
      }
    }

    async function closeTerminal(sessionId) {
      if (sessionId === 'system') {
        return;
      }
      const terminal = terminals.get(sessionId);
      if (!terminal) {
        return;
      }
      await fetch(`/api/terminals/debug/${sessionId}`, { method: 'DELETE' });
      if (terminal.socket) {
        terminal.socket.close();
      }
      if (terminal.eventSource) {
        terminal.eventSource.close();
      }
      if (terminal.terminal) {
        terminal.terminal.dispose();
      }
      terminal.tab.remove();
      terminal.pane.remove();
      terminals.delete(sessionId);
      activateTerminal('system');
    }

    const refitActiveTerminal = () => {
      const active = Array.from(terminals.values()).find((entry) => entry.pane.classList.contains('is-active'));
      scheduleTerminalRefit(active, { focus: active?.session.id !== 'system' });
    };
    window.addEventListener('resize', refitActiveTerminal);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        refitActiveTerminal();
      }
    });
    if (window.ResizeObserver && terminalViewports) {
      const resizeObserver = new ResizeObserver(refitActiveTerminal);
      resizeObserver.observe(terminalViewports);
    }

    if (createButton) {
      createButton.addEventListener('click', async () => {
        setPending(createButton, true);
        try {
          await createDebugSession();
        } finally {
          setPending(createButton, false);
        }
      });
    }
    uploadInput?.addEventListener('change', () => {
      uploadTerminalFile(uploadInput.files?.[0]);
    });
    if (terminalTabs) {
      terminalTabs.addEventListener('dblclick', (event) => {
        const rename = event.target.closest('[data-terminal-rename]');
        if (!rename) {
          return;
        }
        const tab = rename.closest('[data-terminal-tab]');
        if (tab) {
          renameTerminal(tab.dataset.terminalId);
        }
      });
      terminalTabs.addEventListener('click', (event) => {
        const close = event.target.closest('[data-terminal-close]');
        if (close) {
          const tab = close.closest('[data-terminal-tab]');
          if (tab) {
            closeTerminal(tab.dataset.terminalId);
          }
          return;
        }
        const tab = event.target.closest('[data-terminal-tab]');
        if (tab) {
          activateTerminal(tab.dataset.terminalId);
        }
      });
    }

    loadDebugSessions();
  }


  function initHostPortActions() {
    const result = document.querySelector('[data-bulk-result]');
    document.addEventListener('click', async (event) => {
      const button = event.target.closest('[data-host-port-action]');
      if (!button) {
        return;
      }
      event.preventDefault();
      const assetId = button.dataset.hostAssetId;
      const action = button.dataset.hostPortAction;
      if (!assetId || !action) {
        return;
      }
      setPending(button, true);
      try {
        const response = await fetch(`/api/assets/${encodeURIComponent(assetId)}/actions/${action === 'start' ? 'start' : 'stop'}`, {
          method: 'POST',
          headers: { Accept: 'application/json' },
        });
        if (redirectIfUnauthorized(response)) {
          return;
        }
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.detail || '端口任务提交失败');
        }
        setFlash(result, `端口任务已入队：${payload.task.object_id} · ${payload.task.action}`, { success: true });
      } catch (error) {
        setFlash(result, `端口操作失败：${error.message}`);
      } finally {
        setPending(button, false);
      }
    });
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { getMcpMatrixCellState, getUnmanagedMcpIds, getVisibleAgentTargetSummary, getAgentClientStatus, getAgentResourceClientState, describeAgentReconcileResult, getAgentDiscoveryForClient, getAgentInstalledSkillCount, buildAgentProfilePayload, nextAgentClient, buildMcpSavePayload, moveAgentResourceId, buildAgentResourceOrderPayload, buildAgentProviderRouting, buildAgentProviderPayload, getAgentProviderPrimaryAction, getAgentRouterPolicy, buildAgentRouterPolicyPayload, applyAgentRouterProfileDataset, getAgentProviderOwnershipState, applyAgentRouterTakeoverDataset, initAssetSearch, matchesAssetFilter, nextRuntimeFilter };
  }

  if (typeof document !== 'undefined') {
    document.addEventListener('DOMContentLoaded', () => {
      installHtmxFallback();
      initVersionPanels();
      initVersionBadges();
      initAssetSearch();
      initBulkSelection();
      initPackageInstallPanel();
      initConfigSyncPanel();
      initAgentWorkbench();
      initNotificationPanel();
      initNotificationDialog();
      initHostPortActions();
      initTerminalWorkbench();
    });
  }
})();
