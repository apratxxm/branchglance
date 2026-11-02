const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync(__dirname + '/fixture.js', 'utf8');

async function capture(live, prefetched) {
  const calls = [];
  let load;
  const tracker = {state: {}, notch: {}, applyLive: () => calls.push('apply'),
    render: () => calls.push('render'), setupNotch: () => {}, setNotch: () => calls.push('notch')};
  class XHR { open(method, url, async) { assert.equal(async, false); assert.equal(url, '/capture-ready'); } send() { calls.push('ready'); this.status = 200; } }
  const window = {__captureLive: prefetched, __tokenTrackerTest: tracker, addEventListener: (_, fn) => { load = fn; }};
  const context = {window, location: {search: '?capture=tree&live=' + live}, URLSearchParams, Date, XMLHttpRequest: XHR,
    document: {querySelector: () => ({style: {setProperty() {}}})}, fetch: async () => { throw new Error('unexpected fetch'); }};
  vm.runInNewContext(code, context);
  return {calls, result: load()};
}
(async () => {
  const stamp = new Date(Date.now() - 1000).toISOString();
  const valid = {stats: {generatedAt: stamp, providers: []}, live: {generatedAt: stamp, chats: []}};
  const ok = await capture('1', valid);
  // No await or network request can precede application, render, and acknowledgement.
  assert.deepEqual(ok.calls, ['apply', 'render', 'notch', 'ready']);
  await ok.result;
  const invalid = await capture('1', {stats: valid.stats, live: {...valid.live, generatedAt: 'invalid'}});
  await assert.rejects(invalid.result, /Invalid live capture snapshot/);
  assert.deepEqual(invalid.calls, []);
  const missing = await capture('1', null);
  await assert.rejects(missing.result, /Missing prefetched live snapshot/);
  assert.deepEqual(missing.calls, []);
  console.log('capture readiness tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
