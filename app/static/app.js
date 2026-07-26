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

        if (!remoteTargetPanel || localSelected) {
          const payload = await postJson(`/api/bulk/actions/${action}`, { asset_ids: supportedAssetIds, version_map: versionMap });
          if (!payload) return;
          messages.push(`本机 ${payload.queued_count} 个`);
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
        }

        if (remoteTargetPanel && !localSelected && !canRemotePackage) {
          throw new Error('当前动作不支持远程设备，请勾选“当前 WSL”或换成更新/部署/删除。');
        }

        const skipped = unsupported.length ? `，已跳过不支持的 ${unsupported.length} 个：${unsupported.join(', ')}` : '';
        setFlash(result, `已入队：${messages.join(' + ')}${skipped}`, { success: true });
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

    const updateMcpRows = () => {
      const appId = activeClient();
      panel.querySelectorAll('[data-agent-mcp-card]').forEach((row) => {
        let server = null;
        const script = row.querySelector('[data-agent-mcp-json]');
        try { server = JSON.parse(script?.textContent || '{}'); } catch { server = {}; }
        const observation = server?.observations?.[appId] || {};
        const installed = Boolean(observation.present && observation.status === 'installed');
        row.hidden = false;
        row.dataset.agentMcpObserved = String(installed);
        const status = row.querySelector('[data-agent-mcp-status]');
        if (status) {
          status.textContent = installed ? '已安装' : '未安装 / 未扫描';
          status.classList.toggle('is-good', installed);
          status.classList.toggle('is-muted', !installed);
        }
        const uninstall = row.querySelector('[data-agent-mcp-uninstall-one]');
        if (uninstall) uninstall.disabled = !installed;
      });
    };
    const syncClientView = () => {
      const appId = activeClient();
      panel.dataset.agentActiveClient = appId;
      buttons.forEach((button) => setButtonState(button, button.value === appId));
      panel.querySelectorAll('[data-agent-active-name]').forEach((node) => { node.textContent = clientName(appId); });
      const routeTarget = panel.querySelector('[data-agent-router-current-provider]');
      if (routeTarget) routeTarget.textContent = clientById(appId)?.dataset.agentRouterProvider || '未选择';
      panel.querySelectorAll('[data-provider-app]').forEach((card) => { card.hidden = card.dataset.providerApp !== appId; });
      panel.querySelectorAll('[data-agent-skill-client]').forEach((card) => { card.hidden = card.dataset.agentSkillClient !== appId; });
      syncTabs();
      updateMcpRows();
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

    // Provider editor: the generic routing profile is shared by all clients;
    // native settings remain an opaque round-trip field.
    const providerId = panel.querySelector('[data-agent-provider-id]');
    const providerName = panel.querySelector('[data-agent-provider-name]');
    const providerBase = panel.querySelector('[data-agent-provider-base-url]');
    const providerFormat = panel.querySelector('[data-agent-provider-format]');
    const providerAuth = panel.querySelector('[data-agent-provider-auth]');
    const providerModel = panel.querySelector('[data-agent-provider-model]');
    const providerKey = panel.querySelector('[data-agent-provider-key]');
    const providerMap = panel.querySelector('[data-agent-provider-model-map]');
    const providerHeaders = panel.querySelector('[data-agent-provider-headers]');
    const providerSettings = panel.querySelector('[data-agent-provider-settings]');
    const providerCurrent = panel.querySelector('[data-agent-provider-current-check]');
    const clearProvider = () => {
      [providerId, providerName, providerBase, providerModel, providerKey, providerMap, providerHeaders, providerSettings].forEach((input) => { if (input) input.value = ''; });
      if (providerFormat) providerFormat.value = 'openai_chat';
      if (providerAuth) providerAuth.value = 'bearer';
      if (providerCurrent) providerCurrent.checked = false;
    };
    const providerKeyParts = (value) => {
      const parts = String(value || '').split('::');
      return { appId: parts.shift() || '', providerId: parts.join('::') };
    };
    const fillProvider = (provider) => {
      if (!provider) return;
      panel.dataset.agentActiveClient = provider.app_id || activeClient();
      syncClientView();
      const settings = provider.settings_config || {};
      const routing = settings.routing || {};
      if (providerId) providerId.value = provider.id || '';
      if (providerName) providerName.value = provider.name || '';
      if (providerBase) providerBase.value = routing.base_url || '';
      if (providerFormat) providerFormat.value = routing.api_format || 'openai_chat';
      if (providerAuth) providerAuth.value = routing.auth_mode || 'bearer';
      if (providerModel) providerModel.value = routing.model || '';
      if (providerKey) providerKey.value = routing.api_key || '';
      if (providerMap) providerMap.value = JSON.stringify(routing.model_map || {}, null, 2);
      if (providerHeaders) providerHeaders.value = JSON.stringify(routing.headers || {}, null, 2);
      if (providerSettings) providerSettings.value = JSON.stringify(settings, null, 2);
      if (providerCurrent) providerCurrent.checked = Boolean(provider.is_current);
    };
    const loadProvider = async (key) => {
      const { appId, providerId: id } = providerKeyParts(key);
      if (!appId || !id) return null;
      return requestJson(`/api/agent/providers/${encodeURIComponent(appId)}/${encodeURIComponent(id)}`, { method: 'GET' });
    };
    const providerPayload = () => {
      const appId = activeClient();
      const id = providerId?.value?.trim() || '';
      if (!appId || !id || !providerBase?.value?.trim()) throw new Error('先填写 Provider ID 和 Base URL');
      const settings = (() => {
        try { return JSON.parse(providerSettings?.value?.trim() || '{}'); } catch { return { type: appId }; }
      })();
      const routing = {
        base_url: providerBase.value.trim(),
        api_format: providerFormat?.value || 'openai_chat',
        auth_mode: providerAuth?.value || 'bearer',
        model: providerModel?.value?.trim() || null,
        model_map: jsonValue('[data-agent-provider-model-map]', {}),
        headers: jsonValue('[data-agent-provider-headers]', {}),
      };
      if (providerKey?.value?.trim()) routing.api_key = providerKey.value.trim();
      return { app_id: appId, provider_id: id, name: providerName?.value?.trim() || id, settings_config: settings, routing, is_current: Boolean(providerCurrent?.checked) };
    };
    panel.querySelector('[data-agent-provider-new]')?.addEventListener('click', () => { clearProvider(); providerId?.focus(); });
    panel.querySelector('[data-agent-provider-clear]')?.addEventListener('click', clearProvider);
    panel.querySelector('[data-agent-provider-import]')?.addEventListener('click', async (event) => {
      const button = event.currentTarget; setPending(button, true);
      try { const body = await post('/api/agent/providers/import-local', { apps: [activeClient()] }); flash(`已导入 ${body?.imported_count || 0} 个 Provider`, { success: true }); reloadSoon(); }
      catch (error) { flash(`导入失败：${error.message}`); } finally { setPending(button, false); }
    });
    panel.querySelectorAll('[data-agent-provider-edit]').forEach((button) => button.addEventListener('click', async (event) => {
      setPending(event.currentTarget, true);
      try { fillProvider(await loadProvider(event.currentTarget.dataset.agentProviderEdit)); flash('Provider 已载入', { success: true }); }
      catch (error) { flash(`读取失败：${error.message}`); } finally { setPending(event.currentTarget, false); }
    }));
    panel.querySelector('[data-agent-provider-save]')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      try {
        setPending(button, true);
        await post('/api/agent/providers', providerPayload());
        flash('Provider 已保存；凭证只进入私有 Secret Store', { success: true }); reloadSoon();
      } catch (error) { flash(`保存失败：${error.message}`); } finally { setPending(button, false); }
    });
    panel.querySelector('[data-agent-provider-apply]')?.addEventListener('click', async (event) => {
      const selected = panel.querySelector('[data-agent-provider-select][aria-pressed="true"]')?.value;
      const key = selected || `${activeClient()}::${providerId?.value || ''}`;
      const { appId, providerId: id } = providerKeyParts(key);
      if (!appId || !id) { flash('先选择一个 Provider'); return; }
      try { setPending(event.currentTarget, true); const body = await post('/api/agent/providers/apply', { app_id: appId, provider_id: id, node_ids: ['__local__'], write_secrets: false }); flash(`直连配置任务已入队：${body.queued_count || 0}`, { success: true }); reloadSoon(); }
      catch (error) { flash(`应用失败：${error.message}`); } finally { setPending(event.currentTarget, false); }
    });
    panel.querySelectorAll('[data-agent-provider-test]').forEach((button) => button.addEventListener('click', async (event) => {
      const { appId, providerId: id } = providerKeyParts(event.currentTarget.dataset.agentProviderTest);
      try { setPending(event.currentTarget, true); const body = await requestJson(`/api/agent/providers/${encodeURIComponent(appId)}/${encodeURIComponent(id)}/test`, { method: 'POST' }); flash(body.ok ? `连通：HTTP ${body.status_code} · ${body.latency_ms}ms` : '端点未通过连通性测试', { success: Boolean(body.ok) }); }
      catch (error) { flash(`测试失败：${error.message}`); } finally { setPending(event.currentTarget, false); }
    }));
    panel.querySelectorAll('[data-agent-provider-current]').forEach((button) => button.addEventListener('click', async (event) => {
      const { appId, providerId: id } = providerKeyParts(event.currentTarget.dataset.agentProviderCurrent);
      try {
        setPending(event.currentTarget, true);
        await putJson(`/api/agent/router/apps/${encodeURIComponent(appId)}/provider`, { provider_id: id });
        const clientButton = clientById(appId);
        if (clientButton) clientButton.dataset.agentRouterProvider = id;
        syncClientView();
        flash('已设为当前 Provider，并写入本地 Router 私有配置', { success: true });
        reloadSoon();
      } catch (error) { flash(`设置失败：${error.message}`); } finally { setPending(event.currentTarget, false); }
    }));
    panel.querySelectorAll('[data-agent-provider-delete]').forEach((button) => button.addEventListener('click', async (event) => {
      if (!window.confirm(`删除本地 Provider：${event.currentTarget.dataset.agentProviderDelete}？`)) return;
      const { appId, providerId: id } = providerKeyParts(event.currentTarget.dataset.agentProviderDelete);
      try { setPending(event.currentTarget, true); await requestJson(`/api/agent/providers/${encodeURIComponent(appId)}/${encodeURIComponent(id)}`, { method: 'DELETE' }); flash('Provider 定义已删除', { success: true }); reloadSoon(); }
      catch (error) { flash(`删除失败：${error.message}`); } finally { setPending(event.currentTarget, false); }
    }));
    panel.querySelectorAll('[data-agent-provider-select]').forEach((button) => button.addEventListener('click', () => {
      panel.querySelectorAll('[data-agent-provider-select]').forEach((item) => setButtonState(item, item === button));
      const card = button.closest('[data-agent-provider-card]');
      if (card) panel.querySelector(`[data-agent-provider-edit="${CSS.escape(`${card.dataset.providerApp}::${card.dataset.providerId}`)}"]`)?.click();
    }));

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
      const action = event.currentTarget.dataset.agentRouterAction;
      try { setPending(event.currentTarget, true); const body = await requestJson(`/api/agent/router/${action}`, { method: action === 'stop' ? 'POST' : 'POST', body: JSON.stringify({ restore_clients: action === 'stop' }) }); flash(`Router ${action} 请求已提交`, { success: true }); renderRouterStatus(body); refreshRouter(); }
      catch (error) { flash(`Router 操作失败：${error.message}`); } finally { setPending(event.currentTarget, false); }
    }));
    panel.querySelector('[data-agent-router-save]')?.addEventListener('click', async (event) => {
      const address = panel.querySelector('[data-agent-router-address-input]')?.value?.trim();
      const port = Number(panel.querySelector('[data-agent-router-port]')?.value || 7888);
      const proxy = panel.querySelector('[data-agent-router-proxy]')?.value?.trim() || null;
      try { setPending(event.currentTarget, true); await putJson('/api/agent/router/config', { listen_address: address, listen_port: port, show_home_switch: Boolean(panel.querySelector('[data-agent-router-home-switch]')?.checked), outbound_proxy: proxy }); flash('Router 运行配置已保存', { success: true }); }
      catch (error) { flash(`保存失败：${error.message}`); } finally { setPending(event.currentTarget, false); }
    });
    panel.querySelectorAll('[data-agent-route-takeover]').forEach((button) => button.addEventListener('click', async (event) => {
      const target = event.currentTarget; const enabled = target.getAttribute('aria-pressed') !== 'true';
      try { setPending(target, true); await putJson(`/api/agent/router/apps/${encodeURIComponent(target.dataset.agentRouteTakeover)}/takeover`, { enabled }); setButtonState(target, enabled); const label = target.querySelector('[data-agent-takeover-label]'); if (label) label.textContent = enabled ? '已接管' : '未接管'; flash(enabled ? '客户端已接管本地 Router' : '客户端已恢复原配置', { success: true }); }
      catch (error) { flash(`接管操作失败：${error.message}`); } finally { setPending(target, false); }
    }));
    refreshRouter();

    // MCP: selection is a button state, never a device/client matrix.
    const mcpSelection = () => selectedButtons('[data-agent-mcp-server]');
    panel.querySelectorAll('[data-agent-mcp-server]').forEach((button) => button.addEventListener('click', () => setButtonState(button, button.getAttribute('aria-pressed') !== 'true')));
    const mcpAction = async (endpoint, ids, button) => {
      if (!ids.length) { flash('先选择 MCP'); return; }
      try { setPending(button, true); const body = await post(endpoint, localPayload({ mcp_ids: ids })); flash(`MCP 操作完成：${(body.added || body.removed || ids).length || ids.length} 个`, { success: Boolean(body.verified ?? true) }); reloadSoon(); }
      catch (error) { flash(`MCP 操作失败：${error.message}`); } finally { setPending(button, false); }
    };
    panel.querySelector('[data-agent-mcp-install]')?.addEventListener('click', (event) => mcpAction('/api/agent/mcp/local/install', mcpSelection(), event.currentTarget));
    panel.querySelector('[data-agent-mcp-uninstall]')?.addEventListener('click', (event) => mcpAction('/api/agent/mcp/local/uninstall', mcpSelection(), event.currentTarget));
    panel.querySelectorAll('[data-agent-mcp-install-one]').forEach((button) => button.addEventListener('click', (event) => mcpAction('/api/agent/mcp/local/install', [event.currentTarget.dataset.agentMcpInstallOne], event.currentTarget)));
    panel.querySelectorAll('[data-agent-mcp-uninstall-one]').forEach((button) => button.addEventListener('click', (event) => mcpAction('/api/agent/mcp/local/uninstall', [event.currentTarget.dataset.agentMcpUninstallOne], event.currentTarget)));
    panel.querySelector('[data-agent-mcp-import]')?.addEventListener('click', async (event) => {
      try { setPending(event.currentTarget, true); const body = await post('/api/agent/mcp/scan', { node_ids: ['__local__'], apps: [activeClient()] }); const count = body.targets?.reduce((sum, item) => sum + (item.mcp_ids?.length || 0), 0) || 0; flash(`已扫描 ${clientName(activeClient())}：发现 ${count} 个 MCP`, { success: true }); reloadSoon(700); }
      catch (error) { flash(`扫描失败：${error.message}`); } finally { setPending(event.currentTarget, false); }
    });
    panel.querySelector('[data-agent-mcp-format]')?.addEventListener('click', () => { const field = panel.querySelector('[data-agent-mcp-spec]'); try { field.value = JSON.stringify(JSON.parse(field.value || '{}'), null, 2); flash('JSON 已格式化', { success: true }); } catch (error) { flash(`JSON 无效：${error.message}`); } });
    panel.querySelectorAll('[data-agent-mcp-edit]').forEach((button) => button.addEventListener('click', () => {
      const script = panel.querySelector(`[data-agent-mcp-json="${CSS.escape(button.dataset.agentMcpEdit)}"]`); let server; try { server = JSON.parse(script?.textContent || '{}'); } catch { server = {}; }
      panel.querySelector('[data-agent-mcp-id]').value = server.id || ''; panel.querySelector('[data-agent-mcp-name]').value = server.name || server.id || ''; panel.querySelector('[data-agent-mcp-spec]').value = JSON.stringify(server.spec || {}, null, 2);
    }));
    panel.querySelector('[data-agent-mcp-save]')?.addEventListener('click', async (event) => {
      const id = panel.querySelector('[data-agent-mcp-id]')?.value?.trim(); const name = panel.querySelector('[data-agent-mcp-name]')?.value?.trim() || id; let spec;
      try { spec = JSON.parse(panel.querySelector('[data-agent-mcp-spec]')?.value || '{}'); } catch (error) { flash(`Spec JSON 无效：${error.message}`); return; }
      if (!id || !Object.keys(spec).length) { flash('先填写 MCP ID 和 Spec'); return; }
      try { setPending(event.currentTarget, true); await post('/api/agent/mcp/servers', { server_id: id, name, spec, apps: { [activeClient()]: true } }); flash('MCP 已保存到本地库', { success: true }); reloadSoon(); }
      catch (error) { flash(`保存失败：${error.message}`); } finally { setPending(event.currentTarget, false); }
    });
    panel.querySelectorAll('[data-agent-mcp-delete]').forEach((button) => button.addEventListener('click', async (event) => {
      if (!window.confirm(`只删除本地库定义：${event.currentTarget.dataset.agentMcpDelete}？`)) return;
      try { setPending(event.currentTarget, true); await requestJson(`/api/agent/mcp/servers/${encodeURIComponent(event.currentTarget.dataset.agentMcpDelete)}`, { method: 'DELETE' }); flash('本地库定义已删除，客户端配置未改动', { success: true }); reloadSoon(); }
      catch (error) { flash(`删除失败：${error.message}`); } finally { setPending(event.currentTarget, false); }
    }));

    // Skills and prompts always carry exactly one local client target.
    const skillName = panel.querySelector('[data-agent-skill-name]');
    const skillSource = panel.querySelector('[data-agent-skill-source]');
    const skillMode = panel.querySelector('[data-agent-skill-mode]');
    const runSkill = async (endpoint, button, needsSource) => {
      if (!skillName?.value?.trim() || (needsSource && !skillSource?.value?.trim())) { flash('先填写 Skill 名称和来源'); return; }
      const payload = localPayload({
        skill_name: skillName.value.trim(),
        ...(needsSource ? { source: skillSource.value.trim(), mode: skillMode?.value || 'copy' } : {}),
      });
      try {
        setPending(button, true);
        const body = await post(endpoint, payload);
        const changed = [...(body.added || []), ...(body.updated || []), ...(body.removed || [])];
        flash(`Skill 操作完成：${changed.join('、') || skillName.value.trim()}`, { success: Boolean(body.verified) });
        reloadSoon();
      } catch (error) { flash(`Skill 操作失败：${error.message}`); } finally { setPending(button, false); }
    };
    panel.querySelectorAll('[data-agent-skill-install]').forEach((button) => button.addEventListener('click', (event) => runSkill('/api/agent/skills/local/install', event.currentTarget, true)));
    panel.querySelectorAll('[data-agent-skill-update]').forEach((button) => button.addEventListener('click', (event) => runSkill('/api/agent/skills/local/update', event.currentTarget, false)));
    panel.querySelectorAll('[data-agent-skill-delete]').forEach((button) => button.addEventListener('click', (event) => {
      if (!skillName?.value?.trim() || !window.confirm(`卸载 Skill 并移入备份：${skillName.value.trim()}？`)) return;
      runSkill('/api/agent/skills/local/uninstall', event.currentTarget, false);
    }));
    panel.querySelectorAll('[data-agent-skill-pick]').forEach((button) => button.addEventListener('click', (event) => { skillName.value = event.currentTarget.dataset.agentSkillPick || ''; panel.dataset.agentActiveClient = event.currentTarget.dataset.agentSkillAppPick || activeClient(); syncClientView(); }));

    panel.querySelector('[data-agent-prompt-save]')?.addEventListener('click', async (event) => {
      const id = panel.querySelector('[data-agent-prompt-id]')?.value?.trim(); const name = panel.querySelector('[data-agent-prompt-name]')?.value?.trim() || id; const content = panel.querySelector('[data-agent-prompt-content]')?.value || '';
      if (!id || !content.trim()) { flash('先填写 Prompt ID 和内容'); return; }
      try { setPending(event.currentTarget, true); await post('/api/agent/prompts', { prompt_id: id, name, content }); flash('Prompt 模板已保存', { success: true }); reloadSoon(); }
      catch (error) { flash(`保存失败：${error.message}`); } finally { setPending(event.currentTarget, false); }
    });
    panel.querySelector('[data-agent-prompt-import-current]')?.addEventListener('click', async (event) => {
      try {
        setPending(event.currentTarget, true);
        const body = await post('/api/agent/prompts/import-current', localPayload());
        const content = panel.querySelector('[data-agent-prompt-content]');
        const id = panel.querySelector('[data-agent-prompt-id]');
        const name = panel.querySelector('[data-agent-prompt-name]');
        if (content) content.value = body.content || '';
        if (id && !id.value) id.value = `${activeClient()}-current`;
        if (name && !name.value) name.value = `${clientName(activeClient())} 当前内容`;
        flash(body.exists ? '已读取当前 Prompt 文件' : '当前客户端还没有 Prompt 文件', { success: true });
      } catch (error) { flash(`导入失败：${error.message}`); } finally { setPending(event.currentTarget, false); }
    });
    panel.querySelectorAll('[data-agent-prompt-apply]').forEach((button) => button.addEventListener('click', async (event) => {
      try {
        setPending(event.currentTarget, true);
        const body = await post('/api/agent/prompts/local/apply', localPayload({ prompt_id: event.currentTarget.dataset.agentPromptApply }));
        flash('Prompt 已写入并通过回读校验', { success: Boolean(body.verified) });
        reloadSoon();
      } catch (error) { flash(`应用失败：${error.message}`); } finally { setPending(event.currentTarget, false); }
    }));
    panel.querySelector('[data-agent-prompt-restore]')?.addEventListener('click', async (event) => {
      if (!window.confirm(`恢复 ${clientName(activeClient())} 接管前的 Prompt 内容？`)) return;
      try {
        setPending(event.currentTarget, true);
        const body = await post('/api/agent/prompts/local/restore', localPayload());
        flash('Prompt 原文件已恢复', { success: Boolean(body.verified) });
        reloadSoon();
      } catch (error) { flash(`恢复失败：${error.message}`); } finally { setPending(event.currentTarget, false); }
    });
    panel.querySelectorAll('[data-agent-prompt-delete]').forEach((button) => button.addEventListener('click', async (event) => {
      if (!window.confirm(`删除 Prompt 模板：${event.currentTarget.dataset.agentPromptDelete}？`)) return;
      try { setPending(event.currentTarget, true); await requestJson(`/api/agent/prompts/${encodeURIComponent(event.currentTarget.dataset.agentPromptDelete)}`, { method: 'DELETE' }); flash('Prompt 模板已删除', { success: true }); reloadSoon(); }
      catch (error) { flash(`删除失败：${error.message}`); } finally { setPending(event.currentTarget, false); }
    }));
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
    module.exports = { getMcpMatrixCellState, getUnmanagedMcpIds, getVisibleAgentTargetSummary, nextAgentClient, initAssetSearch, matchesAssetFilter, nextRuntimeFilter };
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
