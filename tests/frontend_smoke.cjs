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
      const anchorClick = w.HTMLAnchorElement.prototype.click;
      w.HTMLAnchorElement.prototype.click = function () {
        // jsdom has no download manager. Verify the generated Blob below.
        if (!this.hasAttribute('download')) return anchorClick.call(this);
      };
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
    // Multi-date selection is transactional and survives moving between months.
    $('#open-date-calendar').click();
    $('[data-date="2026-11-11"]').click();
    assert.equal($('[data-date="2026-11-11"]').getAttribute('aria-pressed'), 'true');
    $('[data-date="2026-11-11"]').click();
    assert.equal($('[data-date="2026-11-11"]').getAttribute('aria-pressed'), 'false');
    $('[data-date="2026-11-11"]').click();
    $('#calendar-next').click();
    assert.ok($('#date-selected-list').textContent.includes('2026-11-11'));
    $('#date-dialog [data-close="date-dialog"]').click();
    assert.equal($('#date-mode').value, 'all', 'Cancel must not commit draft dates');
    $('#open-date-calendar').click();
    assert.equal($('[data-date="2026-11-11"]').getAttribute('aria-pressed'), 'false');
    $('[data-date="2026-11-11"]').click();
    $('#apply-date-selection').click();
    await wait(() => $('#busy').hidden && $('#range-caption').textContent.includes('집계 24시간'), 'Date exclusion failed');
    assert.equal($('#date-mode').value, 'exclude');
    $('#date-mode').value = 'include'; $('#date-mode').dispatchEvent(new w.Event('change'));
    $('#apply-range').click();
    await wait(() => $('#busy').hidden && $('#range-caption').textContent.includes('클릭한 날짜만 포함'), 'Date inclusion failed');
    w.URL.createObjectURL = blob => { savedBlob = blob; return 'blob:export'; };
    savedBlob = null; $('#export-session').click();
    await wait(() => savedBlob, 'Session export failed');
    const sessionText = await savedBlob.text(), session = JSON.parse(sessionText);
    assert.equal(session.settings.dateMode, 'include');
    assert.deepEqual(session.settings.selectedDates, ['2026-11-11']);
    Object.defineProperty(input, 'files', {value: [new File([sessionText], 'selected-dates.json')], configurable: true});
    input.dispatchEvent(new w.Event('change', {bubbles: true}));
    await wait(() => !$('#busy').hidden, 'Session restore never started');
    await wait(() => $('#busy').hidden && $('#range-caption').textContent.includes('집계 24시간'), 'Session selection restore failed');
    assert.equal($('#date-mode').value, 'include');
    $('#to').value = '2026-11-12';
    $('#open-date-calendar').click(); $('#calendar-clear').click(); $('#apply-date-selection').click();
    await wait(() => $('#busy').hidden && $('#notice').textContent.includes('집계할 날짜가 없습니다'), 'Empty selection failed');
    // Weekend helper covers the entire query, independent of displayed month.
    $('#to').value = '2026-11-16'; $('#open-date-calendar').click(); $('#calendar-weekends').click();
    assert.equal(d.querySelectorAll('[data-remove-date]').length, 2);
    assert.ok($('#date-selected-list').textContent.includes('2026-11-14'));
    assert.ok($('#date-selected-list').textContent.includes('2026-11-15'));
    $('#date-dialog [data-close="date-dialog"]').click();
    $('#reset-filters').click();
    await wait(() => $('#busy').hidden && $('#notice').textContent.indexOf('집계할 날짜가 없습니다') < 0, 'Reset must restore all dates');
    assert.equal($('#date-mode').value, 'all');
    // Restore the original two-day period before the existing scale checks.
    $('#from').value = '2026-11-11'; $('#to').value = '2026-11-12'; $('#apply-range').click();
    await wait(() => $('#busy').hidden && $('#range-caption').textContent.includes('종료일 포함'), 'Date reset query failed');
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
    savedBlob = null;
    $('#export-csv').click();
    await wait(() => savedBlob, 'CSV export failed');
    assert.match(await savedBlob.text(), /80/);
    assert.deepEqual(errors, [], 'Frontend script errors');
    process.stdout.write((standalone ? 'HTML' : 'Python Web') + ': multi-date inclusion/exclusion, weekends, cancel/reset, empty selection, scale, keyboard and CSV passed\n');
  } finally { dom.window.close(); }
})().catch(e => { process.stderr.write(e.stack + '\n'); process.exitCode = 1; });
