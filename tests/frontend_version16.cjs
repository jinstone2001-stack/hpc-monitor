/* Real Chromium checks, including Worker execution and downloaded PNG bytes.
 * NODE_PATH=<Playwright packages> HPC_PYTHON=<python> HPC_BROWSER=<optional chrome>
 * node tests/frontend_version16.cjs
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');

const output = path.resolve('qa-output');
fs.mkdirSync(output, {recursive: true});

async function downloaded(page, button) {
  const pending = page.waitForEvent('download');
  await button.click();
  const download = await pending;
  return fs.readFileSync(await download.path());
}

async function exercise(browser, url, label) {
  const context = await browser.newContext({viewport: {width: 1450, height: 1100}, acceptDownloads: true});
  const page = await context.newPage(), errors = [], calls = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('request', request => {
    if (request.url().endsWith('/api/command')) calls.push(request.postDataJSON());
  });
  try {
    await page.goto(url);
    await page.locator('#demo-button').click();
    await page.locator('#daily-count').waitFor();
    await page.locator('[data-tab="users"]').click();
    await page.locator('[data-rename-user="kim"]').click();
    await page.locator('#rename-name').fill('<img src=x> 김연구');
    await page.locator('#rename-custom-color').check();
    await page.locator('#rename-color').fill('#123456');
    await page.locator('#rename-form button[type="submit"], #rename-form .primary').click();
    assert.equal(await page.locator('[data-rename-user="kim"]').textContent(), '<img src=x> 김연구');
    assert.equal(await page.locator('#view img').count(), 0, 'Names must be escaped');
    assert.equal(await page.locator('#filter-users input[value="kim"]').count(), 1, 'Filters retain IDs');

    const count = calls.filter(c => c.op === 'analyze').length;
    await page.locator('#appearance-button').click();
    await page.locator('#user-names').fill('kim=연구자\nlee=연구자');
    await page.locator('#user-colors').fill('kim=#123456');
    const style = {chartWidth: 800, chartHeight: 500, pieColumns: 2, piesPerPage: 3,
      pieSize: 160, pieGap: 30, pieHole: 40, fontSize: 14, titleSize: 18, valueSize: 26, exportScale: 2};
    for (const [key, value] of Object.entries(style)) await page.locator(`[data-style="${key}"]`).fill(String(value));
    await page.locator('#appearance-form .primary').click();
    assert.equal(calls.filter(c => c.op === 'analyze').length, count, 'Style changes must reuse accounting');
    assert.equal(await page.locator('[data-rename-user]').filter({hasText: '연구자'}).count(), 2);

    await page.locator('.export-menu summary').click();
    const csv = await downloaded(page, page.locator('#export-csv'));
    assert.match(csv.toString('utf8'), /kim/);
    assert.match(csv.toString('utf8'), /연구자/);
    if (label === 'HTML') {
      await page.locator('#appearance-button').click();
      for (const [key, value] of Object.entries({chartWidth: 4000, chartHeight: 4000, exportScale: 4}))
        await page.locator(`[data-style="${key}"]`).fill(String(value));
      await page.locator('#appearance-form .primary').click();
      const large = await downloaded(page, page.locator('[data-png="user-rank"]'));
      assert.ok(large.readUInt32BE(16) * large.readUInt32BE(20) <= 32000000);
      assert.ok(Math.max(large.readUInt32BE(16), large.readUInt32BE(20)) <= 16384);
      await page.locator('#appearance-button').click();
      for (const [key, value] of Object.entries(style)) await page.locator(`[data-style="${key}"]`).fill(String(value));
      await page.locator('#appearance-form .primary').click();
    }

    await page.locator('[data-tab="nodes"]').click();
    const circles = page.locator('#node-pies [data-actual]');
    assert.equal(await circles.count(), 3);
    assert.equal(await circles.first().getAttribute('r'), '56');
    assert.equal(await circles.first().getAttribute('stroke-width'), '48');
    const centers = await circles.evaluateAll(items => items.map(el => [el.getAttribute('cx'), el.getAttribute('cy')]));
    assert.equal(centers[0][1], centers[1][1]);
    assert.notEqual(centers[1][1], centers[2][1]);
    const png = await downloaded(page, page.locator('[data-png="node-pies"]'));
    assert.equal(png.readUInt32BE(16), 1600);
    assert.equal(png.readUInt32BE(20), 1160);
    fs.writeFileSync(path.join(output, label + '-pies.png'), png);
    await page.locator('[data-pie="node-pies"][data-delta="1"]').click();
    assert.equal(await page.locator('#node-pies [data-actual]').count(), 3);
    await page.screenshot({path: path.join(output, label + '-screen.png'), fullPage: true});

    await page.locator('.export-menu summary').click();
    const sessionBytes = await downloaded(page, page.locator('#export-session'));
    const session = JSON.parse(sessionBytes.toString('utf8'));
    assert.equal(session.settings.userNames.kim, '연구자');
    assert.equal(session.settings.chartStyle.pieColumns, 2);
    assert.ok(session.tables.user_cpu.some(r => r.user === 'kim'));
    await page.locator('#file-input').setInputFiles({name: 'roundtrip.json', mimeType: 'application/json', buffer: sessionBytes});
    await page.locator('#busy').waitFor({state: 'hidden'});
    await page.locator('#appearance-button').click();
    assert.equal(await page.locator('[data-style="pieColumns"]').inputValue(), '2');
    await page.locator('#appearance-dialog [data-close]').first().click();

    await page.locator('#settings-button').click();
    await page.locator('#setting-import').selectOption('replace');
    await page.locator('#settings-form .primary').click();
    await page.locator('#busy').waitFor({state: 'hidden'});
    const gpu = 'timestamp\tnode\tgpu_index\tgpu_util\tmem_total_mb\tmem_used_mb\tpower_w\ttemp_c\n'
      + '2026-11-10T00:00:00\tg1\t0\t80\t1000\t250\t100\t55\n'
      + '2026-11-10T00:30:00\tg1\t0\t90\t1000\t300\t110\t56\n';
    await page.locator('#file-input').setInputFiles({name: 'gpu.tsv', mimeType: 'text/tab-separated-values', buffer: Buffer.from(gpu)});
    await page.locator('#gpu-latest').waitFor();
    for (const id of ['gpu-heat', 'gpu-trend', 'gpu-memory', 'gpu-power', 'gpu-temp']) {
      await page.locator(`[data-chart-select="${id}"]`).click();
      assert.ok(await page.locator(`svg#${id}`).count(), id + ' must render');
    }
    const process = 'timestamp\tnode\tgpu_index\tuser\tpid\tmem_used_mb\n2026-11-10T00:00:00\tg1\t0\tkim\t123\t512\n';
    await page.locator('#file-input').setInputFiles({name: 'gpu_user.tsv', mimeType: 'text/tab-separated-values', buffer: Buffer.from(process)});
    await page.locator('svg#gpu-user-memory').waitFor();
    assert.match(await page.locator('#view').textContent(), /512/);
    assert.equal(await page.locator('[data-rename-user="kim"]').textContent(), '연구자');
    await page.reload();
    await page.locator('#appearance-button').click();
    assert.match(await page.locator('#user-names').inputValue(), /kim=연구자/);
    assert.equal(await page.locator('[data-style="pieColumns"]').inputValue(), '2');
    assert.deepEqual(errors, []);
    console.log(label + ': real Worker/API, GPU metrics, names/colors, settings/JSON, PNG size and CSV passed');
  } catch (error) {
    if (errors.length) error.message += '\nBrowser errors: ' + errors.join('\n');
    throw error;
  } finally { await context.close(); }
}

(async () => {
  const server = spawn(process.env.HPC_PYTHON || 'python3', ['-u', 'HPC_Monitor_Web.py', '--port', '0', '--no-browser'], {windowsHide: true, stdio: ['ignore', 'pipe', 'pipe']});
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      let output = '';
      const timer = setTimeout(() => reject(Error('Local Python server did not start: ' + output)), 15000);
      server.stdout.on('data', bytes => { output += bytes; const match = output.match(/http:\/\/127\.0\.0\.1:\d+\//); if (match) {clearTimeout(timer); resolve(match[0]);} });
      server.on('error', reject);
      server.on('exit', code => {if (code) reject(Error('Local Python server exited: ' + code));});
    });
    browser = await chromium.launch({headless: true, ...(process.env.HPC_BROWSER ? {executablePath: process.env.HPC_BROWSER} : {})});
    await exercise(browser, pathToFileURL(path.resolve('HPC_Usage_Monitor.html')).href, 'HTML');
    await exercise(browser, url, 'Python-Web');
  } finally { if (browser) await browser.close(); server.kill(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
