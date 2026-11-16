/* Optional DOM integration checks: npm install --prefix /tmp/hpc-ui jsdom
 * NODE_PATH=/tmp/hpc-ui/node_modules node tests/frontend_smoke.cjs URL FIXTURE FOLDER
 * Run once with URL=http://127.0.0.1:PORT and once with URL=standalone.
 * Tests the actual shipped event handlers; Chromium/native date popups are separate.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {File} = require('node:buffer');
const {JSDOM, VirtualConsole} = require('jsdom');
const [url, fixture, folder] = process.argv.slice(2);
const standalone = url === 'standalone';
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function wait(test, message) {
  for (let i = 0; i < 500; i++) { if (test()) return; await delay(20); }
  throw Error(message);
}
(async () => {
  const errors = [], calls = [];
  const html = standalone ? fs.readFileSync('HPC_Usage_Monitor.html', 'utf8') : await (await fetch(url)).text();
  const console = new VirtualConsole();
  console.on('jsdomError', e => errors.push(String(e)));
  let savedBlob;
  const dom = new JSDOM(html, {
    url: standalone ? 'http://offline.example/' : url,
    runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: console,
    beforeParse(w) {
      w.fetch = (path, options) => {
        if (options?.headers?.['Content-Type'] === 'application/json') calls.push(JSON.parse(options.body));
        return fetch(new URL(path, url), options);
      };
      w.FormData = FormData; w.Blob = Blob; w.TextDecoder = TextDecoder;
      w.URL.createObjectURL = () => { throw Error('DOM checks exercise the supported worker fallback'); };
      w.URL.revokeObjectURL = () => {};
      w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
      w.HTMLDialogElement.prototype.close = function () { this.open = false; };
    }
  });
  try {
    const w = dom.window, d = w.document, $ = s => d.querySelector(s);
    await wait(() => $('#view button'), 'Initial render failed');
    if (!standalone) await wait(() => $('#busy').hidden, 'Initial metadata request failed');
    // Upload the same JSON through the actual HTML fallback / Python multipart API.
    const input = $('#file-input');
    Object.defineProperty(input, 'files', {value: [new File([fs.readFileSync(fixture)], 'two-days.json')], configurable: true});
    input.dispatchEvent(new w.Event('change', {bubbles: true}));
    await wait(() => !$('#busy').hidden, 'Import never started');
    await wait(() => $('#busy').hidden && $('#dataset-badge').textContent.includes('개 기록'), 'Import failed');
    assert.equal($('#from').type, 'date');
    $('#from').value = '2026-11-11'; $('#to').value = '2026-11-12'; $('#apply-range').click();
    await wait(() => $('#busy').hidden && $('#range-caption').textContent.includes('종료일 포함'), 'Date query failed');
    $('[data-tab="groups"]').click();
    assert.equal(d.querySelectorAll('#view [data-chart]').length, 1, 'Inactive charts should not be constructed');
    const analysisCalls = calls.filter(c => c.op === 'analyze').length;
    $('#pie-max').value = '80'; $('#pie-form button').click();
    assert.equal($('[data-actual]').getAttribute('data-actual'), '80');
    assert.equal($('[data-fill]').getAttribute('data-fill'), '1');
    assert.ok($('#group-pies').textContent.includes('80%'));
    $('#pie-max').value = '70'; $('#pie-form button').click();
    assert.equal($('[data-fill]').getAttribute('data-fill'), '1');
    assert.equal($('[data-actual]').getAttribute('data-actual'), '80');
    assert.equal(calls.filter(c => c.op === 'analyze').length, analysisCalls, 'Scale changes must not repeat accounting');
    $('[data-tab="nodes"]').click();
    if (d.querySelectorAll('#node-pies [data-actual]').length === 24) {
      $('[data-pie="node-pies"][data-delta="1"]').click();
      assert.equal(d.querySelectorAll('#node-pies [data-actual]').length, 1);
      assert.ok($('#node-pies').textContent.includes('g4-24'));
    }
    $('[data-tab="groups"]').click();
    $('#range-mode').value = 'times'; $('#range-mode').dispatchEvent(new w.Event('change', {bubbles: true}));
    assert.equal($('#to').value, '2026-11-13T00:00');
    $('#range-mode').value = 'dates'; $('#range-mode').dispatchEvent(new w.Event('change', {bubbles: true}));
    assert.equal($('#to').value, '2026-11-12');
    if (!standalone) {
      const query = calls.filter(c => c.op === 'analyze').at(-1);
      assert.equal(query.options.to - query.options.from, 2 * 86400000);
      $('#import-button').click();
      await wait(() => $('#server-file-list').getAttribute('aria-busy') === 'false', 'Initial directory failed');
      $('#server-path').value = folder;
      $('#server-path-form').dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true}));
      await wait(() => $('#server-path').value === folder && $('#server-file-list [data-server-file]'), 'Directory navigation failed');
      $('#server-path').focus();
      $('#server-path').dispatchEvent(new w.KeyboardEvent('keydown', {key: 'a', ctrlKey: true, bubbles: true, cancelable: true}));
      assert.equal($('#server-path').selectionStart, 0);
      assert.equal($('#server-path').selectionEnd, folder.length);
      $('#server-file-list').focus();
      $('#server-file-list').dispatchEvent(new w.KeyboardEvent('keydown', {key: 'a', ctrlKey: true, bubbles: true, cancelable: true}));
      assert.equal(d.querySelectorAll('[data-server-file]:checked').length, d.querySelectorAll('[data-server-file]').length);
      assert.ok(d.querySelectorAll('[data-server-folder]').length > 0);
      $('#server-files').close();
    }
    // Export the actual application result and confirm presentation does not alter data.
    w.URL.createObjectURL = blob => { savedBlob = blob; return 'blob:export'; };
    d.addEventListener('click', e => { if (e.target.closest('a[download]')) e.preventDefault(); });
    $('#export-csv').click();
    await wait(() => savedBlob, 'CSV export failed');
    assert.match(await savedBlob.text(), /80/);
    assert.deepEqual(errors, [], 'Frontend script errors');
    process.stdout.write((standalone ? 'HTML' : 'Python Web') + ': dates, scale, labels, chart rendering, keyboard and CSV passed\n');
  } finally { dom.window.close(); }
})().catch(e => { process.stderr.write(e.stack + '\n'); process.exitCode = 1; });
