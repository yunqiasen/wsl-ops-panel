const test = require('node:test');
const assert = require('node:assert/strict');
const { nextAgentClient } = require('../../app/static/app.js');

test('客户端选择始终保持一个激活项', () => {
  assert.equal(nextAgentClient('codex', 'codex'), 'codex');
  assert.equal(nextAgentClient('codex', 'claude'), 'claude');
  assert.equal(nextAgentClient('', ''), null);
});
