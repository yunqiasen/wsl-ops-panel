/* CF interaction owns no business lifecycle operations. */
(() => {
  'use strict';
  const labels = {off: '已关闭', starting: '开启中', online: '在线', stopping: '关闭中', error: '异常'};
  const states = new Map();
  const refreshSequences = new Map();

  function currentUrl(status) {
    if (status.state !== 'online' || !status.current_url) return '';
    try {
      const url = new URL(status.current_url);
      return url.protocol === 'https:' && /^[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com$/.test(url.hostname)
        && !url.username && !url.password && !url.port ? url.href : '';
    } catch { return ''; }
  }

  async function request(endpoint, options = {}) {
    const response = await fetch(endpoint, {signal: AbortSignal.timeout(20000), ...options, headers: {Accept: 'application/json', ...options.headers}});
    if (response.status === 401) {
      window.location.assign('/login');
      throw new Error('登录已过期');
    }
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : body.detail?.message || 'CF 请求失败');
    return body;
  }

  function render(element, status) {
    states.set(element.dataset.cfObject, status);
    element.dataset.cfStatus = status.state;
    element.querySelector('[data-cf-state]').textContent = labels[status.state] || '待核验';
    const managementLabel = {external: '外部管理', external_hint: '外部线索'}[status.management] || '';
    const management = element.querySelector('[data-cf-management]');
    management.hidden = !managementLabel;
    management.textContent = managementLabel + (managementLabel && status.reason ? `：${status.reason}` : '');
    element.querySelector('[data-cf-login]').hidden = !(status.state === 'online' && status.requires_login);
    const url = currentUrl(status);
    const link = element.querySelector('[data-cf-url]');
    link.textContent = url;
    link.hidden = !url;
    if (url) link.href = url; else link.removeAttribute('href');
    element.querySelector('[data-cf-copy]').disabled = !url;
    element.querySelector('[data-cf-error]').textContent = status.error ? `${status.error.stage}：${status.error.message}` : '';
    element.querySelector('[data-cf-verified]').textContent = status.last_verified_at ? `核验：${new Date(status.last_verified_at).toLocaleString()}` : '';
    const history = element.querySelector('[data-cf-history]');
    history.hidden = !status.history_url || status.history_url === url;
    history.querySelector('code').textContent = status.history_url || '';
    const task = element.querySelector('[data-cf-task]');
    task.hidden = !status.last_task_id;
    if (status.last_task_id) task.href = `/tasks#task-${encodeURIComponent(status.last_task_id)}`;
  }

  async function refresh(id) {
    const elements = [...document.querySelectorAll('[data-cf-object]')].filter(element => element.dataset.cfObject === id);
    if (!elements.length) return null;
    const sequence = (refreshSequences.get(id) || 0) + 1;
    refreshSequences.set(id, sequence);
    try {
      const status = await request(`/api/assets/${encodeURIComponent(id)}/cf`);
      if (sequence !== refreshSequences.get(id)) return null;
      elements.forEach(element => render(element, status));
      return status;
    } catch (error) {
      if (sequence !== refreshSequences.get(id)) return null;
      const status = {...states.get(id), state: 'error', current_url: null, error: {stage: 'status', message: error.message}};
      elements.forEach(element => render(element, status));
      return null;
    }
  }

  const sourceLabels = {published_port: '宿主发布端口', container_network: '容器网络', host_network: 'host 网络', project_listener: '项目监听'};

  function chooseTarget(id, prepared) {
    return new Promise(resolve => {
      const dialog = document.createElement('dialog');
      dialog.className = 'cf-entry-dialog';
      dialog.setAttribute('aria-labelledby', 'cf-entry-heading');
      dialog.innerHTML = '<h2 id="cf-entry-heading">选择项目入口</h2><p data-cf-project></p><fieldset><legend>当前可达入口</legend></fieldset><p class="cf-entry-notice">开放的是整个 HTTP 服务，不只下面的页面。临时网址重建后会变化；Quick Tunnel 不支持 SSE，有 200 个进行中请求上限及可用性限制。</p><div class="cf-entry-actions"><button type="button" class="ghost-button" data-cf-cancel>取消此项目</button><button type="button" class="primary-button" data-cf-confirm disabled>确认入口</button></div>';
      dialog.querySelector('[data-cf-project]').textContent = id;
      const confirm = dialog.querySelector('[data-cf-confirm]');
      const fieldset = dialog.querySelector('fieldset');
      let selected = null;
      for (const target of prepared.targets) {
        const label = document.createElement('label');
        label.className = `cf-entry-option${target.reachable ? '' : ' cf-entry-option--unavailable'}`;
        const input = document.createElement('input');
        input.type = 'radio'; input.name = 'cf-entry'; input.disabled = !target.reachable;
        input.setAttribute('aria-label', `${target.name} ${target.origin}${target.path || '/'}`);
        input.addEventListener('change', () => {
          selected = {id: target.id, revision: target.revision}; confirm.disabled = false;
        });
        const text = document.createElement('span');
        for (const [tag, value] of [
          ['strong', target.name], ['code', `${target.origin}${target.path || '/'}`],
          ['small', `${sourceLabels[target.source] || target.source} · ${target.reason || (target.reachable ? '可达' : '待核验')}`],
        ]) {
          const line = document.createElement(tag); line.textContent = value; text.append(line);
        }
        label.append(input, text); fieldset.append(label);
      }
      const finish = choice => { dialog.close(); dialog.remove(); resolve(choice); };
      confirm.addEventListener('click', () => { if (selected) finish(selected); });
      dialog.querySelector('[data-cf-cancel]').addEventListener('click', () => finish(null));
      dialog.addEventListener('cancel', event => { event.preventDefault(); finish(null); });
      dialog.addEventListener('keydown', event => {
        if (event.key !== 'Tab') return;
        const focusable = [...dialog.querySelectorAll('input:not(:disabled),button:not(:disabled)')];
        const first = focusable[0], last = focusable.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      });
      document.body.append(dialog); dialog.showModal();
      dialog.querySelector('input:not(:disabled),button').focus();
    });
  }

  async function submit(action, assetIds, result) {
    result.textContent = '正在核对 CF 入口…';
    const ready = [], skipped = [], targetMap = {};
    const trigger = [...document.querySelectorAll('[data-bulk-action]')].find(button => button.dataset.bulkAction === action);
    try {
      for (const id of assetIds) {
        try {
          if (action !== 'cf-disable') {
            const prepared = await request(`/api/assets/${encodeURIComponent(id)}/cf/targets`);
            const targets = prepared.targets.filter(target => target.reachable);
            if (!targets.length) throw new Error(prepared.targets.map(target => target.reason).filter(Boolean).join('；') || '没有可达的 HTTP 入口');
            const saved = targets.find(target => target.id === prepared.selected_id);
            const automatic = !prepared.requires_selection && (saved || (targets.length === 1 ? targets[0] : null));
            const choice = automatic ? {id: automatic.id, revision: automatic.revision} : await chooseTarget(id, prepared);
            if (!choice) { skipped.push(`${id}：已取消，保持原状`); continue; }
            targetMap[id] = choice;
          }
          ready.push(id);
        } catch (error) { skipped.push(`${id}：${error.message}`); }
      }
      let payload = {queued_count: 0, tasks: [], skipped: []};
      if (ready.length) payload = await request(`/api/bulk/actions/${action}`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({asset_ids: ready, cf_target_map: targetMap}),
      });
      skipped.push(...payload.skipped.map(item => `${item.asset_id}：${item.reason}`));
      result.textContent = `${payload.queued_count ? `已入队 ${payload.queued_count} 个，实际结果见卡片与任务中心。` : '未入队。'}${skipped.join('；')}`;
      await Promise.all(ready.map(refresh));
      return payload;
    } finally {
      // The toolbar enables its buttons when this promise settles.
      window.setTimeout(() => { if (trigger?.isConnected) trigger.focus(); }, 0);
    }
  }

  function init() {
    const elements = [...document.querySelectorAll('[data-cf-object]')];
    for (const element of elements) {
      states.set(element.dataset.cfObject, {state: element.dataset.cfStatus});
      const openVerified = async event => {
        if (event.button !== 0 && event.button !== 1) return;
        event.preventDefault();
        // Reserve the tab during the user gesture; navigate only after a fresh
        // server observation, never to the href left behind in a sleeping tab.
        const destination = window.open('about:blank', '_blank');
        if (!destination) {
          element.querySelector('[data-cf-error]').textContent = '浏览器拦截了新窗口，请允许后重试';
          return;
        }
        destination.opener = null;
        const policy = destination.document.createElement('meta');
        policy.name = 'referrer'; policy.content = 'no-referrer';
        destination.document.head.append(policy);
        const status = await refresh(element.dataset.cfObject);
        const url = status && currentUrl(status);
        if (!url) { destination.close(); return; }
        if (!destination.closed) destination.location.replace(url);
      };
      element.querySelector('[data-cf-url]').addEventListener('click', openVerified);
      element.querySelector('[data-cf-url]').addEventListener('auxclick', openVerified);
      element.querySelector('[data-cf-copy]').addEventListener('click', async () => {
        const status = await refresh(element.dataset.cfObject);
        const url = status && currentUrl(status);
        if (!url) return;
        try {
          if (navigator.clipboard) await navigator.clipboard.writeText(url);
          else {
            const text = document.createElement('textarea');
            text.value = url;
            text.style.position = 'fixed';
            text.style.opacity = '0';
            document.body.append(text); text.select();
            const copied = document.execCommand('copy'); text.remove();
            if (!copied) throw new Error('请选中网址复制');
            element.querySelector('[data-cf-copy]').focus();
          }
          element.querySelector('[data-cf-error]').textContent = '网址已复制';
        } catch (error) { element.querySelector('[data-cf-error]').textContent = error.message; }
      });
    }
    async function poll() {
      if (!document.hidden) {
        const pending = [...states.keys()];
        await Promise.all(Array.from({length: Math.min(4, pending.length)}, async () => {
          while (pending.length) await refresh(pending.shift());
        }));
      }
      if (elements.length) window.setTimeout(poll, 2000);
    }
    if (elements.length) window.setTimeout(poll, 2000);
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {currentUrl};
  if (typeof window !== 'undefined') window.WslCFTunnels = {submit, refresh};
  if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', init);
})();
