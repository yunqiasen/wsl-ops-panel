const test = require('node:test');
const assert = require('node:assert/strict');
const { initAssetSearch, matchesAssetFilter, nextRuntimeFilter } = require('../../app/static/app.js');

function fakeElement({ dataset = {}, textContent = '' } = {}) {
  const listeners = new Map();
  const classes = new Set();
  const attributes = new Map();
  return {
    dataset,
    textContent,
    value: '',
    hidden: false,
    classList: {
      contains(name) {
        return classes.has(name);
      },
      toggle(name, force) {
        if (force) classes.add(name);
        else classes.delete(name);
      },
    },
    addEventListener(type, listener) {
      const group = listeners.get(type) || [];
      group.push(listener);
      listeners.set(type, group);
    },
    dispatch(type) {
      (listeners.get(type) || []).forEach((listener) => listener({ target: this }));
    },
    getAttribute(name) {
      return attributes.get(name) ?? null;
    },
    setAttribute(name, value) {
      attributes.set(name, String(value));
    },
  };
}

function installFakeDocument({ cards = [] } = {}) {
  const input = fakeElement();
  const count = fakeElement();
  const running = fakeElement({ dataset: { runtimeFilter: 'running' }, textContent: '运行中' });
  const stopped = fakeElement({ dataset: { runtimeFilter: 'stopped' }, textContent: '已关闭' });
  running.setAttribute('aria-pressed', 'false');
  stopped.setAttribute('aria-pressed', 'false');
  const buttons = [running, stopped];
  global.window = globalThis;
  global.document = {
    querySelector(selector) {
      if (selector === '[data-asset-search]') return input;
      if (selector === '[data-asset-search-count]') return count;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === '[data-asset-card]') return cards;
      if (selector === '[data-runtime-filter]') return buttons;
      return [];
    },
  };
  return { buttons, count, input };
}

test.afterEach(() => {
  delete global.document;
  delete global.window;
});

test('文字和状态筛选使用 AND 组合', () => {
  assert.equal(matchesAssetFilter('CPA API', 'running', 'cpa', 'running'), true);
  assert.equal(matchesAssetFilter('CPA API', 'running', 'cpa', 'stopped'), false);
  assert.equal(matchesAssetFilter('CPA API', 'unknown', '', 'running'), false);
  assert.equal(matchesAssetFilter('New API', 'stopped', 'new', ''), true);
});

test('再次点击激活按钮恢复全部', () => {
  assert.equal(nextRuntimeFilter('', 'running'), 'running');
  assert.equal(nextRuntimeFilter('running', 'running'), '');
  assert.equal(nextRuntimeFilter('running', 'stopped'), 'stopped');
});

test('零资产页面的筛选按钮仍可切换并显示零匹配', () => {
  const { buttons, count } = installFakeDocument();

  initAssetSearch();
  buttons[0].dispatch('click');

  assert.equal(buttons[0].getAttribute('aria-pressed'), 'true');
  assert.equal(buttons[0].classList.contains('is-active'), true);
  assert.equal(count.textContent, '0 个匹配');

  buttons[0].dispatch('click');
  assert.equal(buttons[0].getAttribute('aria-pressed'), 'false');
  assert.equal(buttons[0].classList.contains('is-active'), false);
});

test('按钮点击会同步卡片显隐和最终匹配数量', () => {
  const cards = [
    fakeElement({ dataset: { assetSearchText: 'CPA', runtimeState: 'running' }, textContent: 'CLIProxyAPI' }),
    fakeElement({ dataset: { assetSearchText: 'New API', runtimeState: 'stopped' }, textContent: 'New API' }),
    fakeElement({ dataset: { assetSearchText: 'Folder', runtimeState: 'unknown' }, textContent: 'Folder' }),
  ];
  const { buttons, count } = installFakeDocument({ cards });

  initAssetSearch();
  buttons[1].dispatch('click');

  assert.deepEqual(cards.map((card) => card.hidden), [true, false, true]);
  assert.equal(count.textContent, '1 个匹配');
  assert.equal(buttons[1].getAttribute('aria-pressed'), 'true');
});
