const test = require('node:test');
const assert = require('node:assert/strict');
const { createAgentClientState } = require('../../app/static/app.js');
test('回包绑定客户端和选择代次，A-B-A 也淘汰旧 A 回包', () => {
  const state = createAgentClientState('codex');
  const a = state.begin('queue');
  state.select('claude');
  assert.equal(state.accepts(a), false);
  state.select('codex');
  assert.equal(state.accepts(a), false);
  assert.equal(state.accepts(state.begin('queue')), true);
});
test('同客户端请求乱序只接收最新，资源种类互不干扰', () => {
  const state = createAgentClientState('codex');
  const a = state.begin('provider');
  const queue = state.begin('queue');
  const b = state.begin('provider');
  assert.equal(state.accepts(a), false);
  assert.equal(state.accepts(queue), true);
  assert.equal(state.accepts(b), true);
  state.select('codex');
  assert.equal(state.accepts(b), true);
});
