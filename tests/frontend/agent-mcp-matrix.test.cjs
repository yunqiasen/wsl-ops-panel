const test = require('node:test');
const assert = require('node:assert/strict');
const {
  getMcpMatrixCellState,
  getVisibleAgentTargetSummary,
} = require('../../app/static/app.js');

test('卸载模式仅允许全部所选 MCP 都已安装的目标', () => {
  assert.deepEqual(
    getMcpMatrixCellState({
      mode: 'uninstall',
      mcpIds: ['context7', 'deepwiki'],
      installedMcpIds: ['context7', 'deepwiki', 'other'],
      unavailable: false,
      defaultStatus: '已检测',
    }),
    { disabled: false, status: '已安装 2 个' },
  );
  assert.deepEqual(
    getMcpMatrixCellState({
      mode: 'uninstall',
      mcpIds: ['context7', 'deepwiki'],
      installedMcpIds: ['context7'],
      unavailable: false,
      defaultStatus: '已检测',
    }),
    { disabled: true, status: '并非全部已装' },
  );
});

test('安装模式保留设备可用性和默认状态', () => {
  assert.deepEqual(
    getMcpMatrixCellState({
      mode: 'install',
      mcpIds: ['context7'],
      installedMcpIds: [],
      unavailable: false,
      defaultStatus: '可预览',
    }),
    { disabled: false, status: '可预览' },
  );
  assert.deepEqual(
    getMcpMatrixCellState({
      mode: 'install',
      mcpIds: ['context7'],
      installedMcpIds: [],
      unavailable: true,
      defaultStatus: '设备离线',
    }),
    { disabled: true, status: '设备离线' },
  );
});

test('聚合状态按设备和客户端筛选后重新计数', () => {
  assert.deepEqual(
    getVisibleAgentTargetSummary(
      {
        nodeId: '__local__',
        clientIds: ['codex', 'claude', 'gemini'],
        clientNames: ['Codex', 'Claude Code', 'Gemini CLI'],
      },
      new Set(['__local__']),
      new Set(['codex', 'gemini']),
    ),
    { count: 2, names: ['Codex', 'Gemini CLI'] },
  );
  assert.equal(
    getVisibleAgentTargetSummary(
      { nodeId: 'remote-a', clientIds: ['codex'], clientNames: ['Codex'] },
      new Set(['__local__']),
      new Set(['codex']),
    ),
    null,
  );
});

test('安装模式拒绝尚未保存到本地库的观测项', () => {
  const { getUnmanagedMcpIds } = require('../../app/static/app.js');
  assert.deepEqual(
    getUnmanagedMcpIds(['managed', 'observed-only'], new Set(['managed'])),
    ['observed-only'],
  );
  assert.deepEqual(getUnmanagedMcpIds(['managed'], new Set(['managed'])), []);
});
