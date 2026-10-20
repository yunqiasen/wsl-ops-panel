const test = require('node:test');
const assert = require('node:assert/strict');
const {currentUrl} = require('../../app/static/cf-tunnels.js');

test('copyable CF URL is restricted to the current online Quick Tunnel', () => {
  assert.equal(currentUrl({state:'online', current_url:'https://business-fixture.trycloudflare.com/ui'}), 'https://business-fixture.trycloudflare.com/ui');
  for (const state of ['off', 'starting', 'stopping', 'error']) {
    assert.equal(currentUrl({state, current_url:'https://business-fixture.trycloudflare.com'}), '');
  }
  for (const current_url of ['http://business-fixture.trycloudflare.com', 'https://trycloudflare.com.evil.test', 'https://user:pass@business-fixture.trycloudflare.com', 'javascript:alert(1)', 'https://business-fixture.trycloudflare.com:444']) {
    assert.equal(currentUrl({state:'online', current_url}), '');
  }
});
