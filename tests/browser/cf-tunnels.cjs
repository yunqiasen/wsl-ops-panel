const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.BROWSER_EXECUTABLE || undefined});
  try {
    const base = process.env.CF_BROWSER_URL;
    const context = await browser.newContext({viewport: {width: 1320, height: 1000}, permissions: ['clipboard-read', 'clipboard-write']});
    await context.addCookies([{name: process.env.CF_COOKIE_NAME, value: process.env.CF_COOKIE_TOKEN, url: base}]);
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    // Version discovery is unrelated to CF; prevent registry network calls.
    await page.route('**/versions', route => route.fulfill({status: 200, contentType: 'application/json', body: '{"versions":[],"source_status":"unsupported"}'}));
    await page.goto(`${base}/categories/docker`);
    const card = page.locator('[data-asset-id="web"]');
    assert.match(await card.locator('[data-cf-state]').textContent(), /已关闭/);
    await card.locator('[data-asset-select]').click();
    await page.getByRole('button', {name: '开启 CF', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('[data-cf-state]')?.textContent === '在线');
    const link = card.locator('[data-cf-url]');
    assert.equal(await link.getAttribute('href'), 'https://business-fixture.trycloudflare.com/');
    await card.getByRole('button', {name: '复制网址'}).click();
    await page.waitForFunction(() => document.querySelector('[data-cf-error]')?.textContent === '网址已复制');
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), 'https://business-fixture.trycloudflare.com/');
    await page.getByRole('button', {name: '关闭 CF', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('[data-cf-state]')?.textContent === '已关闭');
    assert.equal(await link.isVisible(), false);
    await page.getByRole('button', {name: '开启 CF', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('[data-cf-state]')?.textContent === '在线');
    assert.match(await page.locator('[data-bulk-result]').textContent(), /已入队/);
    assert.match(await page.locator('[data-cf-notice]').textContent(), /SSE/);
    const desktop = process.env.CF_BROWSER_OUTPUT;
    if (desktop) {
      fs.mkdirSync(desktop, {recursive: true});
      await page.screenshot({path: path.join(desktop, 'cf-desktop.png'), fullPage: true});
    }
    await page.setViewportSize({width: 390, height: 844});
    if (desktop) await page.screenshot({path: path.join(desktop, 'cf-mobile.png'), fullPage: true});
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({onOffOn: true, clipboard: true, pageErrors: errors}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
