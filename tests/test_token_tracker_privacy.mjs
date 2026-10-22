import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { test } from "node:test";

const appPath = new URL("../token_tracker/static/app.js", import.meta.url);
const app = fs.readFileSync(appPath, "utf8");
const hiddenLoader = app.slice(app.indexOf("function loadHiddenChats() {"), app.indexOf("function saveHiddenChats()"));

function harness(initial = {}) {
  const storage = new Map(Object.entries(initial)), writes = [], reads = [];
  const document = { hidden: false, activeElement: null, querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {} };
  const context = { window: { __TOKEN_TRACKER_TEST__: true, __TOKEN_TRACKER_RUNWAY__: true }, document,
    localStorage: { getItem(key) { reads.push(key); return storage.has(key) ? storage.get(key) : null; }, setItem(key, value) { writes.push([key, String(value)]); storage.set(key, String(value)); } },
    Intl, Date, Math, Number, JSON, AbortController, AbortSignal, setTimeout() { return 1; }, clearTimeout() {}, setInterval() { return 1; } };
  vm.createContext(context); vm.runInContext(app, context);
  return { state: context.window.__tokenTrackerTest.state, storage, writes, reads };
}

test("a fresh install does not seed personal chats or create a seed marker", () => {
  const { state, writes, reads } = harness();
  assert.equal(state.hiddenChats.size, 0);
  assert.deepEqual(writes, []);
  assert.equal(reads.includes("tokenTrackerRunwayHiddenSeedV1"), false);
  assert.doesNotMatch(hiddenLoader, /HiddenSeed|const ids\s*=|ids\.forEach/);
});

test("persisted hidden-chat and pin preferences remain unchanged, including the old marker", () => {
  const hidden = ["Codex:chat-one", "Codex:chat-two", "Claude:other-chat"];
  const pins = ["Codex:pinned-one", "Codex:pinned-two"];
  const initial = { tokenTrackerRunwayHiddenChats: JSON.stringify(hidden), tokenTrackerPinsV2: JSON.stringify(pins), tokenTrackerRunwayHiddenSeedV1: "1" };
  const { state, storage, writes } = harness(initial);
  assert.deepEqual([...state.hiddenChats], hidden.slice(0, 2));
  assert.deepEqual([...state.pins], pins);
  assert.equal(storage.get("tokenTrackerRunwayHiddenSeedV1"), "1");
  assert.deepEqual(writes, []);
});

test("malformed saved preference data keeps the existing empty fail-safe without rewriting storage", () => {
  const initial = { tokenTrackerRunwayHiddenChats: "{bad", tokenTrackerPinsV2: "{bad", tokenTrackerPins: "{also bad" };
  const { state, storage, writes } = harness(initial);
  assert.equal(state.hiddenChats.size, 0);
  assert.equal(state.pins.size, 0);
  assert.equal(storage.get("tokenTrackerRunwayHiddenChats"), "{bad");
  assert.equal(storage.get("tokenTrackerPinsV2"), "{bad");
  assert.deepEqual(writes, []);
});

test("public settings copy states the explicit auto-actions prerequisite", () => {
  const settingsPath = new URL("../token_tracker/static/reset_actions.js", import.meta.url);
  if (!fs.existsSync(settingsPath)) return;
  const settings = fs.readFileSync(settingsPath, "utf8");
  assert.match(settings, /Choice saved\. It runs at the next reset only while auto actions are enabled\./);
  assert.doesNotMatch(settings, /This action is armed for the next genuine reset/);
});
