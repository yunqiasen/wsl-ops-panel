/* CF interaction owns no business lifecycle operations. */
(() => {
  'use strict';
  const labels = {off: '已关闭', starting: '开启中', online: '在线', stopping: '关闭中', error: '异常'};
  const states = new Map();

  function currentUrl(status) {
    if (status.state !== 'online' || !status.current_url) return '';
    try {
      const url = new URL(status.current_url);
      return url.protocol === 'https:' && /^[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com$/.test(url.hostname)
        && !url.username && !url.password && !url.port ? url.href : '';
    } catch { return ''; }
  }

  async function request(endpoint, options = {}) {
    const response = await fetch(endpoint, {...options, headers: {Accept: 'application/json', ...options.headers}});
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
    const url = currentUrl(status);
    const link = element.querySelector('[data-cf-url]');
    link.textContent = url;
    link.hidden = !url;
    if (url) link.href = url; else link.removeAttribute('href');
    element.querySelector('[data-cf-copy]').disabled = !url;
    element.querySelector('[data-cf-error]').textContent = status.error ? `${status.error.stage}：${status.error.message}` : '';
    element.querySelector('[data-cf-verified]').textContent = status.last_verified_at ? `核验：${new Date(status.last_verified_at).toLocaleString()}` : '';
    const task = element.querySelector('[data-cf-task]');
    task.hidden = !status.last_task_id;
    if (status.last_task_id) task.href = `/tasks#task-${encodeURIComponent(status.last_task_id)}`;
  }

  async function refresh(id) {
    const elements = [...document.querySelectorAll('[data-cf-object]')].filter(element => element.dataset.cfObject === id);
    if (!elements.length) return null;
    try {
      const status = await request(`/api/assets/${encodeURIComponent(id)}/cf`);
      elements.forEach(element => render(element, status));
      return status;
    } catch (error) {
      const status = {...states.get(id), state: 'error', current_url: null, error: {stage: 'status', message: error.message}};
      elements.forEach(element => render(element, status));
      return null;
    }
  }

  async function submit(action, assetIds, result) {
    result.textContent = '正在核对 CF 入口…';
    const ready = [];
    const skipped = [];
    for (const id of assetIds) {
      try {
        if (action !== 'cf-disable') {
          const prepared = await request(`/api/assets/${encodeURIComponent(id)}/cf/targets`);
          const targets = prepared.targets.filter(target => target.reachable);
          if (targets.length !== 1) throw new Error(targets.length ? '有多个入口，请选择后开启' : '没有可达的 HTTP 入口');
        }
        ready.push(id);
      } catch (error) { skipped.push(`${id}：${error.message}`); }
    }
    let payload = {queued_count: 0, tasks: [], skipped: []};
    if (ready.length) payload = await request(`/api/bulk/actions/${action}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({asset_ids: ready}),
    });
    skipped.push(...payload.skipped.map(item => `${item.asset_id}：${item.reason}`));
    result.textContent = `${payload.queued_count ? `已入队 ${payload.queued_count} 个，实际结果见卡片与任务中心。` : '未入队。'}${skipped.join('；')}`;
    await Promise.all(ready.map(refresh));
    return payload;
  }

  function init() {
    const elements = [...document.querySelectorAll('[data-cf-object]')];
    for (const element of elements) {
      states.set(element.dataset.cfObject, {state: element.dataset.cfStatus});
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
      if (!document.hidden) await Promise.all([...states].filter(([, status]) => status.state !== 'off').map(([id]) => refresh(id)));
      if (elements.length) window.setTimeout(poll, 2000);
    }
    if (elements.length) window.setTimeout(poll, 2000);
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {currentUrl};
  if (typeof window !== 'undefined') window.WslCFTunnels = {submit, refresh};
  if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', init);
})();
