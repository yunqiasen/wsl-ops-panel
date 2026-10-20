const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.BROWSER_EXECUTABLE || undefined});
  try {
    const base = process.env.CF_BROWSER_URL;
    const scenario = process.env.CF_BROWSER_SCENARIO;
    const expectedUrl = process.env.CF_BROWSER_EXPECTED_URL || 'https://business-fixture.trycloudflare.com/';
    const context = await browser.newContext({viewport: {width: 1320, height: 1000}, permissions: ['clipboard-read', 'clipboard-write']});
    await context.addCookies([{name: process.env.CF_COOKIE_NAME, value: process.env.CF_COOKIE_TOKEN, url: base}]);
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    // Version discovery is unrelated to CF; prevent registry network calls.
    await page.route('**/versions', route => route.fulfill({status: 200, contentType: 'application/json', body: '{"versions":[],"source_status":"unsupported"}'}));
    await page.goto(`${base}/categories/docker`);
    const observer = await context.newPage();
    observer.setDefaultTimeout(5000);
    observer.on('pageerror', error => errors.push(error.message));
    await observer.route('**/versions', route => route.fulfill({status: 200, contentType: 'application/json', body: '{"versions":[],"source_status":"unsupported"}'}));
    await observer.goto(`${base}/categories/docker`);
    await page.bringToFront();
    const card = page.locator('[data-asset-id="web"]');
    if (scenario === 'hint') {
      const management = card.locator('[data-cf-management]');
      assert.match(await management.textContent(), /外部线索.*缺少可核验 systemd 管理对象/);
      await page.waitForResponse(response => response.url().endsWith('/api/assets/web/cf'));
      assert.match(await management.textContent(), /外部线索.*缺少可核验 systemd 管理对象/);
      await card.locator('[data-asset-select]').click();
      assert.equal(await page.locator('[data-bulk-action="cf-create"]:enabled').count(), 0);
      assert.equal(await card.locator('[data-cf-url]').isVisible(), false);
      assert.deepEqual(errors, []);
      console.log(JSON.stringify({scenario, externalHint: true, polling: true, pageErrors: errors}));
      return;
    }
    assert.match(await card.locator('[data-cf-state]').textContent(), scenario === 'legacy' ? /待核验/ : /已关闭/);
    if (scenario === 'legacy') assert.match(await card.locator('[data-cf-management]').textContent(), /外部管理/);
    await card.locator('[data-asset-select]').click();
    await page.getByRole('button', {name: '开启 CF', exact: true}).click();
    if (process.env.CF_BROWSER_MULTIPLE === '1') {
      const dialog = page.getByRole('dialog', {name: '选择项目入口'});
      await dialog.waitFor({state: 'visible'});
      assert.match(await dialog.textContent(), /整个 HTTP 服务/);
      await page.keyboard.press('Escape');
      await dialog.waitFor({state: 'hidden'});
      assert.match(await card.locator('[data-cf-state]').textContent(), /已关闭/);
      await page.waitForFunction(() => document.activeElement?.dataset.bulkAction === 'cf-create');
      await page.getByRole('button', {name: '开启 CF', exact: true}).click();
      await dialog.waitFor({state: 'visible'});
      await page.setViewportSize({width: 390, height: 844});
      const box = await dialog.boundingBox();
      assert.ok(box.width <= 390 && box.x >= 0);
      const choices = dialog.getByRole('radio');
      await choices.nth(1).focus();
      await page.keyboard.press('Space');
      for (let index = 0; index < 6; index++) {
        await page.keyboard.press('Tab');
        assert.equal(await page.evaluate(() => !!document.activeElement.closest('dialog')), true);
      }
      await dialog.getByRole('button', {name: '确认入口'}).click();
      await page.setViewportSize({width: 1320, height: 1000});
    }
    await page.waitForFunction(() => document.querySelector('[data-cf-state]')?.textContent === '在线');
    await observer.bringToFront();
    await observer.waitForFunction(() => document.querySelector('[data-cf-state]')?.textContent === '在线', null, {timeout: 5000});
    await page.bringToFront();
    const link = card.locator('[data-cf-url]');
    assert.equal(await link.getAttribute('href'), expectedUrl);
    await card.getByRole('button', {name: '复制网址'}).click();
    await page.waitForFunction(() => document.querySelector('[data-cf-error]')?.textContent === '网址已复制');
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), expectedUrl);
    const publicVisits = [];
    await context.route(`${new URL(expectedUrl).origin}/**`, route => {
      publicVisits.push(route.request().url());
      return route.fulfill({status: 200, contentType: 'text/html', body: '<title>CF browser fixture</title>Business fixture'});
    });
    await page.route('**/api/assets/web/cf', route => route.fulfill({status: 503, contentType: 'application/json', body: '{"detail":"open verification fixture unavailable"}'}));
    const failedPopupPromise = page.waitForEvent('popup');
    await link.click();
    const failedPopup = await failedPopupPromise;
    await page.waitForFunction(() => document.querySelector('[data-cf-state]')?.textContent === '异常');
    if (!failedPopup.isClosed()) await failedPopup.waitForEvent('close');
    assert.equal(failedPopup.isClosed(), true);
    assert.deepEqual(publicVisits, []);
    await page.unroute('**/api/assets/web/cf');
    await page.waitForFunction(() => document.querySelector('[data-cf-state]')?.textContent === '在线');
    const popupPromise = page.waitForEvent('popup');
    await link.click();
    const popup = await popupPromise;
    await popup.waitForURL(expectedUrl);
    assert.equal(await popup.title(), 'CF browser fixture');
    assert.equal(await popup.evaluate(() => window.opener), null);
    await popup.close();
    await page.getByRole('button', {name: '关闭 CF', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('[data-cf-state]')?.textContent === '已关闭');
    assert.equal(await link.isVisible(), false);
    await page.getByRole('button', {name: '开启 CF', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('[data-cf-state]')?.textContent === '在线');
    assert.match(await page.locator('[data-bulk-result]').textContent(), /已入队/);
    assert.match(await page.locator('[data-cf-notice]').textContent(), /SSE/);
    await page.route('**/api/assets/web/cf', route => route.fulfill({status: 503, contentType: 'application/json', body: '{"detail":"status fixture unavailable"}'}));
    await card.getByRole('button', {name: '复制网址'}).click();
    await page.waitForFunction(() => document.querySelector('[data-cf-state]')?.textContent === '异常');
    assert.equal(await link.isVisible(), false);
    assert.equal(await card.getByRole('button', {name: '复制网址'}).isDisabled(), true);
    await page.unroute('**/api/assets/web/cf');
    await page.waitForFunction(() => document.querySelector('[data-cf-state]')?.textContent === '在线');
    const desktop = process.env.CF_BROWSER_OUTPUT;
    if (desktop) {
      fs.mkdirSync(desktop, {recursive: true});
      await page.screenshot({path: path.join(desktop, 'cf-desktop.png'), fullPage: true});
    }
    await page.setViewportSize({width: 390, height: 844});
    if (desktop) await page.screenshot({path: path.join(desktop, 'cf-mobile.png'), fullPage: true});
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({scenario, onOffOn: true, clipboard: true, crossPage: true, verifiedOpen: true, pageErrors: errors}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
