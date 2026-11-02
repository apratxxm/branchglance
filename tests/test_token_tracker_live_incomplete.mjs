import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { test } from "node:test";

const app = fs.readFileSync(new URL("../token_tracker/static/app.js", import.meta.url), "utf8");
const now = Date.parse("2026-11-02T10:00:00.000Z");
const ago = minutes => new Date(now - minutes * 60000).toISOString();

function boot() {
  const nodes = new Map();
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, { selector, innerHTML: "", textContent: "", value: "", hidden: false, title: "", dataset: {}, attributes: {},
      style: { setProperty() {} }, classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
      querySelectorAll() { return []; }, querySelector() { return null; }, addEventListener() {}, removeEventListener() {}, contains() { return false; }, focus() {}, blur() {} });
    return nodes.get(selector);
  };
  class TestDate extends Date { constructor(...args) { super(...(args.length ? args : [now])); } static now() { return now; } }
  const document = { hidden: false, activeElement: null, querySelector: node, querySelectorAll: () => [], addEventListener() {},
    body: { dataset: {}, classList: { add() {}, remove() {}, toggle() {} } }, documentElement: { style: { setProperty() {} } } };
  const context = { window: { __TOKEN_TRACKER_TEST__: true, __TOKEN_TRACKER_RUNWAY__: true, __TOKEN_TRACKER_REFINED__: true, __TOKEN_TRACKER_COMPACT__: true, addEventListener() {} },
    document, localStorage: { getItem: () => null, setItem() {} }, fetch: async () => ({ ok: true, json: async () => ({}) }),
    Intl, Date: TestDate, Math, Number, JSON, AbortController, AbortSignal, URLSearchParams,
    setTimeout() { return 1; }, clearTimeout() {}, setInterval() { return 0; } };
  vm.createContext(context);
  vm.runInContext(app, context);
  return context.window.__tokenTrackerTest;
}

const policy = { schemaVersion: 1, generation: "incomplete-fixture", providers: [
  { name: "Codex", collect: true, show: true, send: false, capabilities: {} },
  { name: "Claude", collect: true, show: true, send: false, capabilities: {} }] };

// Claude:a and Claude:b are old; Codex:c is a running child that the snapshot links to Claude:a.
const snapshot = () => ({
  generatedAt: ago(2),
  providerPolicies: policy,
  enabledProviders: ["Codex", "Claude"],
  providers: [
    { name: "Claude", connected: true, chats: [
      { id: "a", key: "Claude:a", status: "running", title: "alpha" },
      { id: "b", key: "Claude:b", status: "running", title: "beta" },
    ] },
    { name: "Codex", connected: true, chats: [
      { id: "c", key: "Codex:c", parentKey: "Claude:a", status: "running", title: "child" },
    ] },
  ],
  graph: { revision: 1, edges: { "Claude:a": null, "Claude:b": null, "Codex:c": "Claude:a" } },
});

const claudeRows = s => s.providers.find(provider => provider.name === "Claude").chats;

test("a skipped row is a placeholder: it never ends or flashes a row on screen", () => {
  const T = boot();
  const s = snapshot();
  const payload = {
    generatedAt: ago(0), complete: false, pendingChats: 1,
    chats: [
      { id: "a", key: "Claude:a", provider: "Claude", status: "idle", statusSource: "transcript", activityAt: ago(0) },
      { id: "b", key: "Claude:b", provider: "Claude", status: "idle", statusSource: "transcript", activityAt: ago(0), detailPending: true },
    ],
    graph: { revision: 1, edges: { "Claude:a": null, "Claude:b": null, "Codex:c": "Claude:a" } },
  };
  T.applyLive(s, payload);
  const b = claudeRows(s).find(row => row.id === "b");
  assert.equal(b.status, "running", "the detail-pending row keeps the status on screen");
  assert.equal(b.title, "beta", "the detail-pending row keeps its title");
  assert.equal(claudeRows(s).find(row => row.id === "a").status, "idle", "a fully read row still updates");
});

test("a detail-pending row that is new to the screen is inserted without a status", () => {
  const T = boot();
  const s = snapshot();
  T.applyLive(s, {
    generatedAt: ago(0), complete: false, pendingChats: 1,
    chats: [{ id: "z", key: "Claude:z", provider: "Claude", status: "idle", statusSource: "transcript",
      title: "zeta", parentId: "a", parentProvider: "Claude", detailPending: true }],
    graph: { revision: 1, edges: {} },
  });
  const z = claudeRows(s).find(row => row.id === "z");
  assert.ok(z, "the roster is complete, so the row is inserted");
  assert.equal(z.title, "zeta");
  assert.equal(z.parentId, "a");
  assert.equal(z.status, undefined, "no status is inserted from a placeholder");
});

test("an incomplete payload's graph applies, and a processed parent correction wins", () => {
  const T = boot();
  const s = snapshot();
  const result = T.applyLive(s, {
    generatedAt: ago(0), complete: false, pendingChats: 1,
    chats: [
      { id: "c", key: "Codex:c", provider: "Codex", parentId: "b", parentProvider: "Claude", parentKey: "Claude:b",
        status: "running", statusSource: "transcript", activityAt: ago(0) },
      { id: "a", key: "Claude:a", provider: "Claude", status: "running", statusSource: "transcript", activityAt: ago(0) },
      { id: "b", key: "Claude:b", provider: "Claude", status: "running", statusSource: "transcript", detailPending: true },
    ],
    graph: { revision: 2, edges: { "Claude:a": null, "Claude:b": null, "Codex:c": "Claude:b" } },
  });
  assert.equal(s.graph.revision, 2, "the full-roster graph applies");
  assert.equal(s.graph.edges["Codex:c"], "Claude:b", "the processed correction replaces the stale edge");
  assert.equal(result.changed, true);
});

test("a complete live payload still replaces the graph", () => {
  const T = boot();
  const s = snapshot();
  T.applyLive(s, {
    generatedAt: ago(0), complete: true, pendingChats: 0,
    chats: [{ id: "a", key: "Claude:a", provider: "Claude", status: "running", activityAt: ago(0) }],
    graph: { revision: 2, edges: { "Claude:a": null } },
  });
  assert.equal(s.graph.revision, 2);
  assert.equal(s.providers[0].chats.length, 2, "rows are still never removed by absence");
});
