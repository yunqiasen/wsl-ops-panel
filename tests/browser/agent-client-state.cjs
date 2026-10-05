const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const dir = process.env.ARCHITECTURE_REVIEW_OUTPUT;
if (!dir) throw Error('fixture directory is required');
const repo = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(dir, 'fixture.html'), 'utf8');
const pause = ms => new Promise(r => setTimeout(r, ms));
async function until(fn) { for(let i=0;i<100;i++){if(fn())return; await pause(30);} throw Error('pending request timeout'); }
(async () => {
 const browser = await chromium.launch({headless: true, executablePath:process.env.BROWSER_EXECUTABLE || undefined});
 try {
 const page = await browser.newPage({viewport:{width:1440,height:1000}});
 const errors = []; const outbound=[];
 page.on('pageerror', e => errors.push(e.message));
 let holdQueue=false, pendingQueue=null, holdProvider=false, pendingProvider=null, holdSave=false, pendingSave=null, holdConfig=false, pendingConfig=null;
 function queueData(client) { return { provider_ids:[`${client}-provider`], items:[{provider_id:`${client}-provider`,name:`${client.toUpperCase()}-P`}], available:[]}; }
 await page.route('**/*', async route => {
   const url = new URL(route.request().url());
   const json = body => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
   if(url.hostname !== 'fixture.test') return route.fulfill({status:200,body:'',contentType:'text/javascript'});
   if(url.pathname === '/') return route.fulfill({status:200,contentType:'text/html',body:html});
   if(url.pathname.startsWith('/static/')) {
     const file = path.resolve(repo,'app',url.pathname.slice(1));
     if(file.startsWith(repo+'/app/static/') && fs.existsSync(file)) return route.fulfill({status:200,body:fs.readFileSync(file),contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'image/png'});
   }
   if(url.pathname === '/api/agent/router/config' && route.request().method()==='PUT' && holdConfig){pendingConfig=route;return;}
   if(url.pathname.endsWith('/policy')){outbound.push({path:url.pathname,body:route.request().postData()}); return json({provider:{max_retries:3}});}
   if(url.pathname.includes('/failover-queue')) {
     const client=url.pathname.split('/')[5];
     if(client==='codex'&&holdSave&&route.request().method()==='PUT'){outbound.push({path:url.pathname,body:route.request().postData()});pendingSave=route;return;}
     if(client==='codex'&&holdQueue&&route.request().method()==='GET'){pendingQueue=route; return;}
     if(route.request().method()!=='GET')outbound.push({path:url.pathname,body:route.request().postData()});
     return json(queueData(client));
   }
   if(url.pathname === '/api/agent/providers/codex/codex-provider') {
      if(holdProvider){pendingProvider=route;return;}
      return json({app_id:'codex',id:'codex-provider',name:'Codex fixture',form:{model:'fixture'},summary:{editable:true},meta:{},settings_config:{}});
   }
   if(url.pathname === '/api/agent/providers/codex/codex-second') return json({app_id:'codex',id:'codex-second',name:'Second detail',form:{model:'second'},summary:{editable:true},meta:{},settings_config:{}});
   if(url.pathname === '/api/agent/library') return json({mcp:[],skills:[],prompts:[],providers:[],profiles:[],router:{},discovery:{mcp:[],skills:[],prompts:[]}});
   return json({});
 });
 await page.goto('http://fixture.test/',{waitUntil:'domcontentloaded'});
 await page.locator('[data-agent-app][value="codex"]').click();
 await page.waitForFunction(()=>document.querySelector('[data-agent-failover-queue-list]').textContent.includes('CODEX-P'));
 holdQueue=true;
 await page.locator('[data-agent-app][value="codex"]').click();
 await until(()=>pendingQueue);
 await page.locator('[data-agent-app][value="claude"]').click();
 await page.waitForFunction(()=>document.querySelector('[data-agent-failover-queue-list]').textContent.includes('CLAUDE-P'));
 const queueBefore=await page.locator('[data-agent-failover-queue-list]').textContent();
 holdQueue=false;
 const queueResponse = page.waitForResponse(r=>r.url().includes('/codex/failover-queue'));
 await pendingQueue.fulfill({status:200,contentType:'application/json',body:JSON.stringify(queueData('codex'))});
 await (await queueResponse).finished();
 await page.evaluate(() => new Promise(requestAnimationFrame));
 const first=await page.evaluate(()=>({active:document.querySelector('[data-agent-workbench]').dataset.agentActiveClient,queue:document.querySelector('[data-agent-failover-queue-list]').textContent.trim()}));
 first.before=queueBefore.trim(); first.defect_reproduced=first.active==='claude'&&first.queue.includes('CODEX-P');
 // Same real event path: a slow Provider read overrides a newer client selection.
 await page.locator('[data-agent-app][value="codex"]').click();
 await page.locator('[data-agent-tab-control="providers"]').click();
 holdProvider=true;
 await page.locator('[data-agent-provider-edit="codex::codex-provider"]').click();
 await until(()=>pendingProvider);
 await page.locator('[data-agent-app][value="claude"]').click();
 const selectedBefore=await page.locator('[data-agent-workbench]').getAttribute('data-agent-active-client');
 holdProvider=false;
 await pendingProvider.fulfill({status:200,contentType:'application/json',body:JSON.stringify({app_id:'codex',id:'codex-provider',name:'Codex fixture',form:{model:'fixture'},summary:{editable:true},meta:{},settings_config:{}})});
 await page.locator('[data-agent-provider-edit="codex::codex-provider"]').waitFor({state:'attached'});
 await page.waitForFunction(()=>!document.querySelector('[data-agent-provider-edit="codex::codex-provider"]').disabled);
 const second={before:selectedBefore,after:await page.locator('[data-agent-workbench]').getAttribute('data-agent-active-client')};
 second.defect_reproduced=second.before==='claude'&&second.after==='codex';

 const select = async id => page.locator(`[data-agent-app][value="${id}"]`).click();
 const name = page.locator('[data-agent-provider-name]');
 const edit = page.locator('[data-agent-provider-edit="codex::codex-provider"]');
 const releaseProvider = async () => {
   holdProvider=false;
   await pendingProvider.fulfill({status:200,contentType:'application/json',body:JSON.stringify({app_id:'codex',id:'codex-provider',name:'Stale detail',form:{model:'stale'},summary:{editable:true},meta:{},settings_config:{}})});
   await page.waitForFunction(()=>!document.querySelector('[data-agent-provider-edit="codex::codex-provider"]').disabled);
   pendingProvider=null;
 };
 // Drafts are client-owned, including the A -> B -> A case with a pending read.
 await select('codex');
 await name.fill('Codex draft');
 holdProvider=true; await edit.click(); await until(()=>pendingProvider);
 await select('claude'); await name.fill('Claude draft');
 await select('codex'); await releaseProvider();
 assert.equal(await name.inputValue(),'Codex draft');
 await select('claude'); assert.equal(await name.inputValue(),'Claude draft');
 await select('codex');
 // An edit made while a detail request is pending must survive its response.
 holdProvider=true; await edit.click(); await until(()=>pendingProvider);
 await name.fill('Typed after request'); await releaseProvider();
 assert.equal(await name.inputValue(),'Typed after request');
 // A later detail request wins even when the earlier record returns last.
 holdProvider=true; await edit.click(); await until(()=>pendingProvider);
 await page.locator('[data-agent-provider-edit="codex::codex-second"]').click();
 await page.waitForFunction(()=>document.querySelector('[data-agent-provider-name]').value==='Second detail');
 await releaseProvider(); assert.equal(await name.inputValue(),'Second detail');
 // A late save response from Codex must not replace Claude's visible queue.
 await page.locator('[data-agent-tab-control="route"]').click();
 await page.waitForFunction(()=>!document.querySelector('[data-agent-failover-queue-save]').disabled);
 holdSave=true;
 await page.locator('[data-agent-failover-queue-save]').click(); await until(()=>pendingSave);
 await select('claude');
 await page.waitForFunction(()=>document.querySelector('[data-agent-failover-queue-list]').textContent.includes('CLAUDE-P'));
 const saveResponse=page.waitForResponse(r=>r.request().method()==='PUT'&&r.url().includes('/codex/failover-queue'));
 await pendingSave.fulfill({status:200,contentType:'application/json',body:JSON.stringify(queueData('codex'))});
 await (await saveResponse).finished(); await page.evaluate(()=>new Promise(requestAnimationFrame));
 assert.match(await page.locator('[data-agent-failover-queue-list]').textContent(), /CLAUDE-P/);
 await select('codex');
 holdConfig=true;
 await page.locator('[data-agent-router-save]').click(); await until(()=>pendingConfig);
 await select('claude');
 await pendingConfig.fulfill({status:200,contentType:'application/json',body:'{}'});
 await page.waitForFunction(()=>!document.querySelector('[data-agent-router-save]').disabled);
 assert.equal(outbound.filter(x=>x.path.endsWith('/policy')).length,0, 'late global-save response must not write a policy to another client');
 const extraChecks={draftRoundTrip:true,abaRead:true,inputWinsPendingRead:true,latestDetailWins:true,lateQueueSave:true,lateRouterSave:true};
 await page.screenshot({path:path.join(dir,'browser-fixture.png')});
 const result={fixture:'real rendered template and current project app.js; all network intercepted; no live writes',queueRace:first,providerSelectionRace:second,extraChecks,pageErrors:errors,outbound};
 fs.writeFileSync(path.join(dir,'browser-probes.json'),JSON.stringify(result,null,2));
 console.log(JSON.stringify(result,null,2));
 if(errors.length||first.defect_reproduced||second.defect_reproduced)process.exitCode=1;
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
