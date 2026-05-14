(() => {
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
    if (pending) {
      element.dataset.originalText = element.textContent;
      element.textContent = '处理中…';
    } else if (element.dataset.originalText) {
      element.textContent = element.dataset.originalText;
      delete element.dataset.originalText;
    }
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

  function renderVersionPayload(content, payload) {
    const runtime = payload.runtime || {};
    const versions = Array.isArray(payload.versions) ? payload.versions : [];
    const firstVersions = versions.slice(0, 8).join(', ') || '暂无可部署版本';
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
        select.innerHTML = '';
        if (!versions.length) {
          const option = document.createElement('option');
          option.value = '';
          option.textContent = '暂无可部署版本';
          select.append(option);
        }
        versions.forEach((version) => {
          const option = document.createElement('option');
          option.value = version;
          option.textContent = version;
          select.append(option);
        });
      }
      if (deployButton) {
        deployButton.disabled = !versions.length;
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


  async function populateVersionSelect(select) {
    if (!select || select.dataset.loaded === 'true') {
      return;
    }
    const endpoint = select.dataset.versionEndpoint;
    if (!endpoint) {
      return;
    }
    select.innerHTML = '<option value="">版本加载中…</option>';
    try {
      const response = await fetch(endpoint, { headers: { Accept: 'application/json' } });
      if (redirectIfUnauthorized(response) || !response.ok) {
        select.innerHTML = '<option value="">版本加载失败</option>';
        return;
      }
      const payload = await response.json();
      const versions = Array.isArray(payload.versions) ? payload.versions : [];
      select.innerHTML = '';
      if (!versions.length) {
        const fallback = payload.current_version || 'latest';
        const option = document.createElement('option');
        option.value = fallback;
        option.textContent = fallback;
        select.append(option);
      } else {
        versions.forEach((version) => {
          const option = document.createElement('option');
          option.value = version;
          option.textContent = version;
          select.append(option);
        });
      }
      select.dataset.loaded = 'true';
    } catch (error) {
      select.innerHTML = `<option value="">${error.message}</option>`;
    }
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
      }[slug] || slug;
    }

    function syncUI() {
      document.querySelectorAll('[data-asset-card]').forEach((card) => {
        const assetId = card.dataset.assetId;
        const isSelected = selected.has(assetId);
        card.classList.toggle('is-selected', isSelected);
        const picker = card.querySelector('[data-card-version-panel]');
        if (picker) {
          picker.hidden = !isSelected;
          if (isSelected) {
            const select = picker.querySelector('[data-card-version-select]');
            populateVersionSelect(select);
          }
        }
      });
      toolbar.hidden = selected.size === 0;
      if (count) {
        count.textContent = String(selected.size);
      }
      const selectedActions = assetIdsForSelection().map((assetId) => {
        const card = document.querySelector(`[data-asset-id="${CSS.escape(assetId)}"]`);
        return new Set((card?.dataset.supportedActions || '').split(',').filter(Boolean));
      });
      toolbar.querySelectorAll('[data-bulk-action]').forEach((actionButton) => {
        const action = actionSlugToName(actionButton.dataset.bulkAction);
        const allowed = selectedActions.length === 0 || selectedActions.every((actions) => actions.has(action));
        actionButton.hidden = !allowed;
      });
    }

    board.addEventListener('click', (event) => {
      const button = event.target.closest('[data-asset-select]');
      if (!button) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      const assetId = button.dataset.assetSelect;
      if (selected.has(assetId)) {
        selected.delete(assetId);
      } else {
        selected.add(assetId);
      }
      syncUI();
    });

    toolbar.addEventListener('click', async (event) => {
      const button = event.target.closest('[data-bulk-action]');
      if (!button) {
        return;
      }
      const action = button.dataset.bulkAction;
      const assetIds = Array.from(selected);
      const versionMap = {};
      if (action === 'deploy-version') {
        for (const assetId of assetIds) {
          const select = document.querySelector(`[data-card-version-select="${CSS.escape(assetId)}"]`);
          if (!select || !select.value) {
            if (result) {
              result.innerHTML = `<div class="flash">${assetId} 未选择版本</div>`;
            }
            return;
          }
          versionMap[assetId] = select.value;
        }
      }

      setPending(button, true);
      try {
        const response = await fetch(`/api/bulk/actions/${action}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ asset_ids: assetIds, version_map: versionMap }),
        });
        if (redirectIfUnauthorized(response)) {
          return;
        }
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.detail || '批量任务提交失败');
        }
        if (result) {
          result.innerHTML = `<div class="flash flash-success">已入队 ${payload.queued_count} 个任务：${payload.action}</div>`;
        }
      } catch (error) {
        if (result) {
          result.innerHTML = `<div class="flash">请求失败：${error.message}</div>`;
        }
      } finally {
        setPending(button, false);
      }
    });
  }

  function terminalName(session) {
    if (session.id === 'system') {
      return '系统 bash';
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
    const terminals = new Map();

    function createTerminalTab(session) {
      const tab = document.createElement('button');
      tab.className = 'terminal-tab';
      tab.type = 'button';
      tab.dataset.terminalTab = '';
      tab.dataset.terminalId = session.id;

      const icon = document.createElement('span');
      icon.className = 'terminal-tab-icon';
      icon.textContent = '▸';

      const title = document.createElement('span');
      title.className = 'terminal-tab-title';
      title.textContent = terminalName(session);

      tab.append(icon, title);
      if (session.id !== 'system') {
        const close = document.createElement('span');
        close.className = 'terminal-tab-close';
        close.dataset.terminalClose = '';
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

      const screen = document.createElement('div');
      screen.className = 'terminal-screen';
      screen.dataset.terminalScreen = '';
      screen.setAttribute('aria-label', `${terminalName(session)} terminal`);
      pane.append(screen);
      return { pane, screen };
    }

    function sendTerminalResize(socket, terminal) {
      if (!socket || socket.readyState !== WebSocket.OPEN || !terminal) {
        return;
      }
      socket.send(JSON.stringify({ type: 'resize', cols: terminal.cols, rows: terminal.rows }));
    }

    function connectTerminal(session, screen) {
      if (!window.Terminal) {
        screen.textContent = 'xterm.js 未加载，终端无法启动。';
        return { socket: null, terminal: null, fitAddon: null };
      }

      const terminal = new window.Terminal({
        cursorBlink: true,
        cursorStyle: 'block',
        fontFamily: '"Cascadia Code", "JetBrains Mono", "SFMono-Regular", Consolas, monospace',
        fontSize: 14,
        lineHeight: 1.2,
        scrollback: 5000,
        convertEol: false,
        allowTransparency: true,
        theme: {
          background: '#0a1020',
          foreground: '#dbe7ff',
          cursor: '#7dd3fc',
          selectionBackground: '#284b8a',
          black: '#0b1020',
          red: '#fb7185',
          green: '#34d399',
          yellow: '#facc15',
          blue: '#60a5fa',
          magenta: '#c084fc',
          cyan: '#22d3ee',
          white: '#e5e7eb',
          brightBlack: '#64748b',
          brightRed: '#fda4af',
          brightGreen: '#86efac',
          brightYellow: '#fde047',
          brightBlue: '#93c5fd',
          brightMagenta: '#d8b4fe',
          brightCyan: '#67e8f9',
          brightWhite: '#ffffff',
        },
      });
      const fitAddon = window.FitAddon ? new window.FitAddon.FitAddon() : null;
      if (fitAddon) {
        terminal.loadAddon(fitAddon);
      }
      terminal.open(screen);
      if (fitAddon) {
        fitAddon.fit();
      }

      const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
      const socket = new WebSocket(`${protocol}://${window.location.host}/api/terminals/debug/${session.id}/ws`);
      socket.onmessage = (event) => terminal.write(event.data);
      socket.onopen = () => {
        terminal.focus();
        sendTerminalResize(socket, terminal);
      };
      socket.onclose = () => {
        screen.dataset.closed = 'true';
        terminal.write('\r\n\x1b[38;5;244m[连接已关闭]\x1b[0m\r\n');
      };
      terminal.onData((chunk) => socket.readyState === WebSocket.OPEN && socket.send(chunk));
      terminal.onResize(() => sendTerminalResize(socket, terminal));

      screen.addEventListener('click', () => terminal.focus());
      const resizeHandler = () => {
        if (!fitAddon) {
          return;
        }
        fitAddon.fit();
        sendTerminalResize(socket, terminal);
      };
      window.addEventListener('resize', resizeHandler);
      return { socket, terminal, fitAddon, resizeHandler };
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

      const connection = connectTerminal(session, panePayload.screen);
      terminals.set(session.id, {
        session,
        tab,
        pane: panePayload.pane,
        screen: panePayload.screen,
        ...connection,
      });
    }

    function activateTerminal(sessionId) {
      terminals.forEach((terminal, id) => {
        const active = id === sessionId;
        terminal.tab.classList.toggle('is-active', active);
        terminal.pane.classList.toggle('is-active', active);
        if (active) {
          if (terminal.fitAddon) {
            terminal.fitAddon.fit();
          }
          if (terminal.terminal) {
            terminal.terminal.focus();
          }
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
      if (terminal.terminal) {
        terminal.terminal.dispose();
      }
      if (terminal.resizeHandler) {
        window.removeEventListener('resize', terminal.resizeHandler);
      }
      terminal.tab.remove();
      terminal.pane.remove();
      terminals.delete(sessionId);
      activateTerminal('system');
    }

    if (createButton) {
      createButton.addEventListener('click', createDebugSession);
    }
    if (terminalTabs) {
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

  document.addEventListener('DOMContentLoaded', () => {
    installHtmxFallback();
    initVersionPanels();
    initBulkSelection();
    initTerminalWorkbench();
  });
})();
