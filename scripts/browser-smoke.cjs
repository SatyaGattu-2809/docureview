/* Real-browser integration against disposable API/worker processes and SQLite data. */
const {chromium} = require('playwright');
const {spawn} = require('node:child_process');
const {randomBytes} = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'docureview-browser-'));
const reviewerKey = randomBytes(24).toString('hex');
const env = {...process.env, DOCUREVIEW_API_KEY:randomBytes(24).toString('hex'),
  DOCUREVIEW_REVIEWER_KEY:reviewerKey, DOCUREVIEW_DATABASE:path.join(directory,'test.db'),
  DOCUREVIEW_PROVIDER:'demo', DOCUREVIEW_AUTO_ACCEPT:'false'};
delete env.DOCUREVIEW_IDENTITIES_FILE;
const python = process.env.PYTHON || 'python';
const port = process.env.TEST_PORT || '8765';
let api, worker, browser;
async function main() {
  api = spawn(python,['-m','uvicorn','docureview.api:create_app','--factory','--host','127.0.0.1','--port',port],{env,cwd:root,stdio:'ignore'});
  const base = `http://127.0.0.1:${port}`;
  let ready = false;
  for (let i=0;i<60;i++) {
    try { if ((await fetch(`${base}/healthz`)).ok) {ready=true;break;} } catch {}
    await new Promise(resolve=>setTimeout(resolve,250));
  }
  assert(ready,'API did not start');
  worker = spawn(python,['-m','docureview.worker'],{env,cwd:root,stdio:'ignore'});
  browser = await chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1440,height:1100}});
  const errors = [];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(`${base}/review`);
  await page.getByLabel('Your API key').fill(reviewerKey);
  await page.getByRole('button',{name:'Connect',exact:true}).click();
  await page.locator('#identity').filter({hasText:'local-reviewer'}).waitFor();
  await page.locator('#file').setInputFiles(path.join(root,'examples/invoice.txt'));
  await page.getByRole('button',{name:'Upload & process'}).click();
  await page.locator('#field-total').waitFor({timeout:30000});
  assert.equal(await page.locator('#field-total').inputValue(),'1080.00');
  await page.getByRole('button',{name:'Page 1: 1080.00',exact:true}).click();
  assert.equal(await page.locator('mark').textContent(),'1080.00');
  await page.getByRole('button',{name:'Open original',exact:true}).click();
  await page.getByRole('link',{name:'Download original'}).waitFor();
  await page.locator('#field-vendor').fill('Acme Analytics LLC (verified)');
  const approval = page.waitForResponse(r=>r.url().endsWith('/review') && r.request().method()==='POST');
  await page.getByRole('button',{name:'Approve invoice',exact:true}).click();
  assert.equal((await approval).status(),200);
  await page.locator('.metadata').filter({hasText:'approved'}).waitFor();
  assert.equal(await page.locator('#field-vendor').inputValue(),'Acme Analytics LLC (verified)');
  if(process.env.SCREENSHOT) await page.screenshot({path:process.env.SCREENSHOT,fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth <= window.innerWidth),'Mobile horizontal overflow');
  page.on('dialog',dialog=>dialog.accept());
  await page.getByRole('button',{name:'Delete document and original'}).click();
  await page.locator('#message').filter({hasText:'Document deleted.'}).waitFor();
  await page.getByRole('button',{name:'Disconnect',exact:true}).click();
  assert.equal(await page.locator('#key').inputValue(),'');
  assert.deepEqual(errors,[]);
  console.log('Browser smoke passed: login, durable upload, source highlight, original, correction, approval, mobile layout, deletion, logout.');
}
main().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(browser) await browser.close();
  const stop = child => new Promise(resolve=>{
    if(!child || child.exitCode!==null){resolve();return;}
    child.once('exit',resolve);child.kill('SIGTERM');
  });
  await Promise.all([stop(worker),stop(api)]);
  fs.rmSync(directory,{recursive:true,force:true});
});
