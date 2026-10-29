import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const app = fs.readFileSync(new URL("../token_tracker/static/app.js", import.meta.url), "utf8");
const page = fs.readFileSync(new URL("../token_tracker/static/runway.html", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../token_tracker/static/runway_refined.css", import.meta.url), "utf8");
assert.match(page, /window\.__TOKEN_TRACKER_REFINED__=true/);
assert.match(page, /window\.__TOKEN_TRACKER_COMPACT__=true/);
assert.match(page, /runwayParams\.get\("theme"\)==="classic"\)\{window\.__TOKEN_TRACKER_REFINED__=false/);
assert.match(page, /<header class="titlebar compact-toolbar pywebview-drag-region">/,
  "compact controls are present in the startup markup without a hidden attribute");
assert.match(page, /<div id="compact-summary" class="compact-summary pywebview-drag-region"/,
  "the compact summary remains part of the persistent toolbar");

const now = Date.parse("2026-10-19T12:00:00Z"), nodes = new Map();
let testNow = now;
const widgetListeners = new Map(), layoutListeners = new Map(), documentListeners = new Map(), windowListeners = new Map(), widgetClasses = new Set(), bodyClasses = new Set(), intervalCalls = [];
let documentRef, timerId = 0;
const timers = new Map();
const node = selector => {
  if (!nodes.has(selector)) nodes.set(selector, { selector, innerHTML: "", textContent: "", value: "", dataset: {}, attributes: {}, style: { setProperty() {} },
    hidden: false, inert: false, scrollHeight: 0,
    setAttribute(name, value) { this.attributes[name] = String(value); }, getAttribute(name) { return this.attributes[name] ?? null; },
    removeAttribute(name) { delete this.attributes[name]; }, querySelectorAll() { return []; },
    classList: { add(name) { this.owner.add(name); }, remove(name) { this.owner.delete(name); }, toggle(name, enabled) { enabled ? this.owner.add(name) : this.owner.delete(name); }, owner: selector === ".widget" ? widgetClasses : bodyClasses },
    addEventListener(type, handler, options) { if (selector === ".widget") widgetListeners.set(type, handler); if (selector === ".layout") { const listeners = layoutListeners.get(type) || []; listeners.push({ handler, options }); layoutListeners.set(type, listeners); } }, removeEventListener() {},
    contains(target) { return selector === ".widget" && Boolean(target?.dataset?.insideWidget); },
    closest(query) { return query === ".compact-toolbar" && this.dataset.insideWidget && this.tagName === "BUTTON" ? {} : null; },
    focus() { if (selector === "#chat-search" && node(".chats").inert) return; documentRef.activeElement = this; },
    blur() { if (documentRef.activeElement === this) documentRef.activeElement = null; } });
  return nodes.get(selector);
};
class TestDate extends Date { constructor(...args) { super(...(args.length ? args : [testNow])); } static now() { return testNow; } }
documentRef = { querySelector: node, querySelectorAll: () => [], hidden: false, activeElement: null,
  body: { dataset: {}, classList: { toggle(name, enabled) { enabled ? bodyClasses.add(name) : bodyClasses.delete(name); } } },
  addEventListener(type, handler) { const listeners = documentListeners.get(type) || []; listeners.push(handler); documentListeners.set(type, listeners); } };
const context = { window: { __TOKEN_TRACKER_TEST__: true, __TOKEN_TRACKER_RUNWAY__: true, __TOKEN_TRACKER_REFINED__: true, __TOKEN_TRACKER_COMPACT__: true,
    addEventListener(type, handler) { const listeners = windowListeners.get(type) || []; listeners.push(handler); windowListeners.set(type, listeners); } },
  document: documentRef, Intl, Date: TestDate,
  Math, Number, JSON, AbortController, AbortSignal,
  setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
  clearTimeout(id) { timers.delete(id); }, setInterval(callback, delay) { intervalCalls.push(delay); return intervalCalls.length; } };
vm.createContext(context);
vm.runInContext(app, context);
const T = context.window.__tokenTrackerTest, stamp = minutes => new Date(now - minutes * 60000).toISOString();
assert.equal(T.runway, true);
assert.equal(T.compact, true);

const parent = { id: "exact-thread-id", title: "Parent title", model: "gpt-6.1-sol", contextModel: "gpt-6.1-sol",
  contextObservedAt: stamp(1), status: "needs-input", pendingQuestion: "Continue this exact task?", pendingQuestionId: "q-17",
  pendingOptions: ["Continue", "Wait"], stateSince: stamp(2), updatedAt: stamp(1), usageUpdatedAt: stamp(1),
  contextTokens: 160000, contextWindow: 258400, contextPercent: 62, workspace: "D:\\work\\sample-app",
  lastRequest: { input: 1000, cached: 800 }, latestUpdate: "must not appear", objective: "must not appear either" };
const child = { id: "child-id", parentId: parent.id, title: "agent-check-live-updates", agentName: "agent-check-live-updates", model: "gpt-6.1-sol",
  contextModel: "gpt-6.1-sol", contextObservedAt: stamp(1), status: "running", stateSince: stamp(3), updatedAt: stamp(1),
  usageUpdatedAt: stamp(1), contextTokens: 10000, contextWindow: 258400, contextPercent: 4, workspace: "D:\\work\\sample-app",
  lastRequest: { input: 200, cached: 100 }, latestMessageAt: stamp(0), latestMessageDirection: "agent-to-parent" };
const provider = { name: "Codex", connected: true, chats: [parent, child], fiveHour: { percent: 90, resetAt: now / 1000 + 600 },
  weekly: { percent: 75, resetAt: now / 1000 + 86400 }, limitsUpdatedAt: stamp(1) };
T.render({ generatedAt: stamp(0), enabledProviders: ["Codex"], providers: [provider] });
const parentHTML = T.rowHTML(T.state.forest.rows.find(chat => chat.id === parent.id));
const childHTML = T.rowHTML(T.state.forest.rows.find(chat => chat.id === child.id));
for (const slot of ["title", "timing", "status", "activity-column", "path-column", "tokens"]) assert.ok(parentHTML.includes(`data-runway-slot="${slot}"`), `parent has ${slot} slot`);
assert.match(parentHTML, /title="160000 \/ 258400"/);
assert.match(parentHTML, /Cache 80%/);
assert.match(parentHTML, /runway-refined-elapsed">2:00/);
assert.match(parentHTML, /runway-state-word runway-waiting-link[^>]*>Waiting/);
assert.match(parentHTML, /data-runway-slot="status"[^>]*data-own-waiting="true" data-child-waiting="false"/);
assert.match(parentHTML, /data-runway-slot="tree-anchor"/);
assert.match(parentHTML, /runway-state-glyph/);
assert.doesNotMatch(parentHTML, /runway-context-label">Context/, "the context label remains omitted");
assert.match(parentHTML, /D:\\work\\sample-app/);
assert.doesNotMatch(parentHTML, /must not appear/);
assert.match(parentHTML, /data-action="runway-question" data-key="Codex:exact-thread-id"/);
assert.match(childHTML, /data-runway-slot="message-lane"/);
assert.match(childHTML, /data-runway-slot="status"[^>]*data-own-waiting="false" data-child-waiting="false"/);
const childAgentHTML = T.runwayTelemetryHTML(child, true);
assert.match(childAgentHTML, /title="Agent check live updates"/);
assert.match(childAgentHTML, /data-runway-slot="label"[^>]*>Agent check live updates<\/span>/);
assert.doesNotMatch(childAgentHTML, /\/root\//);
assert.equal((childAgentHTML.match(/class="runway-message-indicator"/g) || []).length, 1);
assert.ok(childAgentHTML.indexOf('data-runway-slot="label"') < childAgentHTML.indexOf('class="runway-message-indicator"'));
const nearExpiryMessage = { ...child, latestMessageAt: new Date(now - 9999).toISOString() };
assert.equal(T.runwayMessageAge(nearExpiryMessage), 9999);
assert.match(T.runwayTelemetryHTML(nearExpiryMessage, true), /class="runway-message-indicator"/);
testNow = now + 1;
assert.equal(T.runwayMessageAge(nearExpiryMessage), null);
assert.doesNotMatch(T.runwayTelemetryHTML(nearExpiryMessage, true), /class="runway-message-indicator"/);
testNow = now;
assert.match(T.runwayQuestionPopover(T.state.forest.rows.find(chat => chat.id === parent.id)), /data-thread="exact-thread-id"/);

// The status column already names the state; the activity column never repeats it.
const compacting = { ...T.state.forest.rows.find(chat => chat.id === child.id), status: "compacting", compactionStartedAt: stamp(0), stateSince: stamp(0), latestMessageAt: null };
const compactingHTML = T.rowHTML(compacting);
assert.equal((compactingHTML.match(/>Compacting</g) || []).length, 1, "Compacting appears once");
assert.doesNotMatch(parentHTML, /Waiting for input/, "Waiting is not repeated as activity");

const noCache = T.rowHTML({ ...T.state.forest.rows.find(chat => chat.id === child.id), lastRequest: { input: 0, cached: 0 } });
assert.doesNotMatch(noCache, /Cache \d+%/);
const unavailable = T.rowHTML({ ...T.state.forest.rows.find(chat => chat.id === child.id), telemetryUnavailable: true });
assert.doesNotMatch(unavailable, /Cache \d+%/);

T.renderResets([provider]);
assert.match(node("#resets").innerHTML, /id="r0-fiveHour-left"/);
assert.doesNotMatch(node("#resets").innerHTML, /id="r0-weekly-left"/);
assert.match(node("#weekly-resets").innerHTML, /id="r0-weekly-left"/);
assert.match(node("#weekly-resets").innerHTML, /id="r0-weekly-bar"/);
assert.match(css, /runway-refined-row \.runway-refined-path>\*\{grid-column:1;min-width:0;max-width:100%/);
assert.match(css, /\.chat>\.row\{position:sticky;top:0;z-index:3;[^}]*background:transparent;/, "a resting parent lets the trunk line show through");
assert.match(css, /\.chat>\.row\.runway-root-stuck\{background:#0e1017;box-shadow:[^}]+\}/, "a stuck parent covers children scrolling under it and casts a shadow");
assert.match(css, /\.chat:has\(>\.runway-tree-connectors\)>\.row\.runway-root-stuck/);
assert.doesNotMatch(css, /#chat-list>\.chat:only-child/);

const fireTimer = id => { const timer = timers.get(id); assert.ok(timer, `scheduled timer ${id} exists`); timers.delete(id); timer.callback(); };
assert.equal(T.notch.mode, "inspect", "the first active lifecycle change gets a transient peek");
assert.equal(timers.get(T.notch.autoTimer)?.delay, 3000, "the activation peek lasts three seconds");
fireTimer(T.notch.autoTimer);
assert.equal(T.notch.mode, "controls");
assert.equal(node("#compact-counts").innerHTML, node("#notch-counts").innerHTML, "rest mode retains the active and waiting counts");

const directionLabels = { "parent-to-agent": "from parent", "agent-to-parent": "to parent", "agent-to-agent": "between agents" };
for (const [direction, label] of Object.entries(directionLabels)) {
  testNow += 2000;
  const latestMessageAt = new Date(testNow - 500).toISOString();
  const nextChild = { ...child, latestMessageAt, latestMessageDirection: direction };
  const nextProvider = { ...provider, chats: [parent, nextChild] };
  T.render({ generatedAt: new Date(testNow).toISOString(), enabledProviders: ["Codex"], providers: [nextProvider] });
  assert.equal(timers.get(T.notch.autoTimer)?.delay, 3000, `${direction} arrival gets a three-second peek`);
  const badgeHTML = T.runwayTelemetryHTML(nextChild, true);
  assert.equal((badgeHTML.match(/class="runway-message-indicator"/g) || []).length, 1);
  assert.ok(badgeHTML.includes(`Recent message · ${label} · visible for 10 seconds`));
  fireTimer(T.notch.autoTimer);
}
testNow += 2000;
const unsupportedMessage = { ...child, latestMessageAt: new Date(testNow - 500).toISOString(), latestMessageDirection: "assistant-analysis" };
T.render({ generatedAt: new Date(testNow).toISOString(), enabledProviders: ["Codex"], providers: [{ ...provider, chats: [parent, unsupportedMessage] }] });
assert.equal(T.notch.autoTimer, null, "unproven message directions do not trigger the peek");
assert.equal(T.runwayMessageAge(unsupportedMessage), null, "unsupported directions do not produce a badge");

const bridgeCalls = [];
context.window.pywebview = { api: { set_notch(...args) { bridgeCalls.push(args); return Promise.resolve(true); } } };
const flushBridge = async () => { await Promise.resolve(); await Promise.resolve(); };
const selectMode = async (active, { hover = false, focus = false, search = false } = {}) => {
  T.notch.active = active; T.notch.hover = hover; T.notch.qualifiedHover = hover; T.notch.autoExpanded = false; T.notch.manual = false;
  T.notch.keyboardInspecting = false; T.notch.inspecting = hover; T.state.searchOpen = search; T.state.popover = null;
  documentRef.activeElement = focus ? node("#widget-keep-open") : null;
  if (focus) node("#widget-keep-open").dataset.insideWidget = true;
  T.setNotch(false, true); await flushBridge();
};
await selectMode(0);
assert.equal(T.notch.mode, "controls"); assert.equal(T.notchHeight(), 110);
assert.ok(bodyClasses.has("compact-controls"));
assert.equal(node("#widget-keep-open").hidden, false, "toolbar controls stay visible while the chat list is collapsed");
assert.equal(node(".chats").inert, true, "collapsed chats remain inert until inspection");
await selectMode(1);
assert.equal(T.notch.mode, "controls"); assert.equal(T.notchHeight(), 110, "active work without inspection retains compact controls");
await selectMode(0, { hover: true });
assert.equal(T.notch.mode, "controls"); assert.equal(T.notchHeight(), 110);
node("#chat-list").scrollHeight = 900;
await selectMode(1, { search: true });
assert.equal(T.notch.mode, "inspect"); assert.equal(T.notchHeight(), 420, "inspection content is capped at 420px");
assert.deepEqual(bridgeCalls.map(args => args[3]), ["controls", "inspect"]);
assert.ok(bridgeCalls.every(args => args.length === 4), "compact native bridge receives its fourth mode argument");
assert.deepEqual(bridgeCalls.map(args => args[1]), [110, 420]);

const deferredBridge = [];
context.window.pywebview.api.set_notch = (...args) => new Promise((resolve, reject) => deferredBridge.push({ args, resolve, reject }));
await selectMode(0);
await selectMode(1, { search: true });
await selectMode(0);
await selectMode(1, { search: true });
assert.equal(deferredBridge.length, 1, "only one compact bridge request is in flight");
assert.equal(deferredBridge[0].args[3], "controls");
deferredBridge[0].resolve(true); await flushBridge();
assert.equal(deferredBridge.length, 2, "intermediate pending modes collapse to the latest mode");
assert.equal(deferredBridge[1].args[3], "inspect");
await selectMode(0);
deferredBridge[1].reject(new Error("late bridge failure")); await flushBridge();
assert.equal(deferredBridge.length, 3);
assert.equal(deferredBridge[2].args[3], "controls", "a rejected older request does not erase the newer pending mode");
deferredBridge[2].resolve(true); await flushBridge();
assert.ok(T.notch.sent?.startsWith("controls:false:110:"));

let keepOpen = { keepOpen: false, keepOpenRevision: 4 }, keepWrites = [];
context.window.pywebview.api.get_keep_open = async () => ({ ...keepOpen });
context.window.pywebview.api.set_keep_open = async (value, revision) => {
  keepWrites.push([value, revision]);
  if (revision !== keepOpen.keepOpenRevision) return { ...keepOpen };
  keepOpen = { keepOpen: value, keepOpenRevision: revision + 1 };
  return { ...keepOpen };
};
await T.syncKeepOpen();
const lockRevision = T.widgetLock.revision;
assert.equal(await T.setKeepOpen(true), true);
assert.deepEqual(keepWrites, [[true, 4]]);
assert.equal(T.keepOpenState.revision, 5);
assert.equal(T.widgetLock.revision, lockRevision, "keep-open has an independent revision from position lock");
assert.equal(T.applyKeepOpen({ keepOpen: false, keepOpenRevision: 4 }), false, "an old keep-open response cannot regress the setting");

node("#chat-search").dataset.insideWidget = true;
node("#chats-head").hidden = false;
T.notch.active = 0; T.notch.hover = false; T.notch.autoExpanded = false; T.state.searchOpen = false; T.state.query = "preserved query";
T.keepOpenState.value = false; T.state.popover = null; T.setNotch(false, true);
assert.equal(node(".chats").inert, true);
T.handleKey({ key: "/", target: { tagName: "BODY" }, preventDefault() {} });
assert.equal(T.state.searchOpen, true);
assert.equal(documentRef.activeElement, node("#chat-search"), "search receives focus after the compact view reveals it");
assert.equal(T.notch.mode, "inspect");
assert.equal(node(".chats").inert, false);

const headerHover = { closest: selector => selector === ".titlebar" ? node(".titlebar") : null };
const dispatchDocument = (type, event) => { for (const handler of documentListeners.get(type) || []) { handler(event); if (event.immediateStopped) break; } };
const dispatchWindow = (type, event) => { for (const handler of windowListeners.get(type) || []) { handler(event); if (event.immediateStopped) break; } };
T.setupNotch();
assert.ok(widgetListeners.has("focusin"), "compact inspection also opens on keyboard focus");
T.notch.active = 0; T.notch.hover = false; T.notch.qualifiedHover = false; T.notch.autoExpanded = false;
T.notch.manual = false; T.notch.inspecting = false; T.state.searchOpen = false; T.keepOpenState.value = false;
documentRef.activeElement = null; T.notch.globalPointerX = 95; T.notch.globalPointerY = 20; T.setNotch(false, true);
widgetListeners.get("pointerenter")({ target: headerHover, clientX: 102, clientY: 20 });
const idleHoverDwell = T.notch.inspectTimer;
assert.equal(timers.get(idleHoverDwell)?.delay, 60, "compact hover uses the approved 60ms dwell");
assert.equal(T.notch.mode, "controls");
assert.equal(T.notch.inspecting, false, "hover has not qualified before the dwell completes");
fireTimer(idleHoverDwell); assert.equal(T.notch.mode, "controls", "idle hover reveals the compact controls row");
assert.equal(T.notch.inspecting, true, "hover qualifies at the 60ms boundary");
widgetListeners.get("pointerleave")();
const idleHoverExit = T.notch.exitTimer; assert.equal(timers.get(idleHoverExit)?.delay, 110);
fireTimer(idleHoverExit); assert.equal(T.notch.mode, "controls");

const moveCompactPointer = (screenX, screenY, clientX = screenX, clientY = screenY) => {
  const event = { target: headerHover, screenX, screenY, clientX, clientY };
  dispatchDocument("pointermove", event); widgetListeners.get("pointermove")(event);
};
T.notch.active = 0; T.notch.hover = false; T.notch.hoverSuppressed = false; T.notch.qualifiedHover = false;
T.notch.inspecting = false; T.notch.manual = false; T.notch.keyboardInspecting = false;
documentRef.activeElement = null; T.notch.globalPointerX = 100; T.notch.globalPointerY = 20; T.setNotch(false, true);
widgetListeners.get("pointerenter")({ target: headerHover, screenX: 100, screenY: 20, clientX: 10, clientY: 10 });
moveCompactPointer(102, 20, 12, 10); assert.equal(T.notch.inspectTimer, null, "first 2px step does not qualify hover");
moveCompactPointer(104, 20, 14, 10);
const accumulatedIdleDwell = T.notch.inspectTimer;
assert.equal(timers.get(accumulatedIdleDwell)?.delay, 60, "two 2px steps accumulate to deliberate idle hover");
fireTimer(accumulatedIdleDwell); assert.equal(T.notch.mode, "controls");
widgetListeners.get("pointerleave")(); if (T.notch.exitTimer) fireTimer(T.notch.exitTimer);

T.notch.active = 1; T.notch.activeKeys = new Set(["Codex:slow-hover"]); T.notch.hover = false;
T.notch.hoverSuppressed = false; T.notch.qualifiedHover = false; T.notch.inspecting = false; T.notch.manual = false;
T.notch.keyboardInspecting = false; T.notch.globalPointerX = 200; T.notch.globalPointerY = 20;
documentRef.activeElement = null; T.setNotch(false, true);
widgetListeners.get("pointerenter")({ target: headerHover, screenX: 200, screenY: 20, clientX: 20, clientY: 10 });
moveCompactPointer(202, 20, 22, 10); assert.equal(T.notch.inspectTimer, null, "first active 2px step does not qualify hover");
moveCompactPointer(204, 20, 24, 10);
const accumulatedActiveDwell = T.notch.inspectTimer;
assert.equal(timers.get(accumulatedActiveDwell)?.delay, 60, "two 2px steps accumulate to deliberate active hover");
assert.equal(T.notch.mode, "controls", "active hover remains collapsed before the dwell completes");
fireTimer(accumulatedActiveDwell); assert.equal(T.notch.mode, "inspect");
widgetListeners.get("pointerleave")(); if (T.notch.exitTimer) fireTimer(T.notch.exitTimer);

T.notch.active = 1; T.notch.hover = false; T.notch.hoverSuppressed = false; T.notch.qualifiedHover = false;
T.notch.inspecting = false; T.notch.manual = false; T.notch.keyboardInspecting = false;
T.notch.globalPointerX = 300; T.notch.globalPointerY = 20; documentRef.activeElement = null; T.setNotch(false, true);
widgetListeners.get("pointerenter")({ target: headerHover, screenX: 300, screenY: 20, clientX: 30, clientY: 10 });
moveCompactPointer(300, 20, 100, 10);
assert.equal(T.notch.inspectTimer, null, "client-coordinate jumps with stationary screen coordinates do not qualify compact hover");
widgetListeners.get("pointerleave")(); if (T.notch.exitTimer) fireTimer(T.notch.exitTimer);

T.notch.active = 1; T.notch.hover = false; T.notch.qualifiedHover = false; T.widgetLock.locked = true;
T.notch.manual = false; T.notch.inspecting = false; T.keepOpenState.value = false;
documentRef.activeElement = null; T.setNotch(false, true);
widgetListeners.get("pointerenter")({ target: headerHover, clientX: 150, clientY: 20 });
const pointerLockButton = node("#widget-lock"); pointerLockButton.dataset.insideWidget = true; pointerLockButton.tagName = "BUTTON";
dispatchDocument("pointerdown", { target: pointerLockButton, pointerType: "mouse" });
documentRef.activeElement = pointerLockButton; widgetListeners.get("focusin")({ target: pointerLockButton });
assert.equal(T.notch.mode, "controls", "pointer focus on the position lock does not hold inspection open");
widgetListeners.get("pointerleave")();
const pointerLockExit = T.notch.exitTimer; if (pointerLockExit) fireTimer(pointerLockExit);
assert.equal(documentRef.activeElement, pointerLockButton, "the pointer-focused toolbar control remains visible and focused when hover ends");
assert.equal(T.notch.mode, "controls"); assert.equal(T.notch.expanded, false, "persistent toolbar focus does not expand the chat tree");
T.widgetLock.locked = false;

T.notch.active = 1; T.state.searchOpen = false; T.notch.autoExpanded = false; T.notch.hover = false;
documentRef.activeElement = null; T.setNotch(false, true);
const keepButton = node("#widget-keep-open"); keepButton.dataset.insideWidget = true;
dispatchDocument("keydown", { key: "Tab", target: node("#chat-search") });
documentRef.activeElement = node("#chat-search");
widgetListeners.get("focusin")({ target: node("#chat-search") });
assert.equal(T.notch.mode, "inspect");
documentRef.activeElement = keepButton;
widgetListeners.get("focusout")({ target: node("#chat-search"), relatedTarget: keepButton });
assert.equal(T.notch.mode, "inspect", "tabbing between widget controls does not close inspection");

const toolbarButton = node("#widget-lock"); toolbarButton.tagName = "BUTTON"; toolbarButton.dataset.insideWidget = true;
T.keepOpenState.value = false; T.notch.hover = false; T.notch.autoExpanded = false; T.notch.inspecting = false; T.notch.manual = false;
T.notch.keyboardInspecting = false; T.notch.qualifiedHover = false; T.notch.active = 1; T.state.searchOpen = false;
documentRef.activeElement = null; T.setNotch(false, true);
dispatchDocument("pointerdown", { target: toolbarButton, pointerType: "mouse" });
documentRef.activeElement = toolbarButton;
widgetListeners.get("focusin")({ target: toolbarButton });
assert.equal(T.notch.mode, "controls", "pointer-only toolbar focus does not pin the expanded view");
assert.equal(T.notch.expanded, false, "pointer toolbar focus does not expand the tree");
dispatchDocument("keydown", { key: "Tab", target: toolbarButton });
documentRef.activeElement = toolbarButton;
widgetListeners.get("focusin")({ target: toolbarButton });
assert.equal(T.notch.mode, "inspect", "keyboard focus inside the widget holds inspection");

T.notch.qualifiedHover = false; T.notch.hover = false; T.notch.autoExpanded = false; T.notch.inspecting = false;
T.notch.keyboardInspecting = false; T.notch.manual = false; documentRef.activeElement = null; T.setNotch(false, true);
T.notch.globalPointerX = 100; T.notch.globalPointerY = 20; T.notch.activeKeys = new Set(); T.notch.workStates.clear();
T.syncNotch(1, [{ key: "Codex:stationary-pointer", active: true, runStartedAt: new Date(testNow).toISOString() }]);
const stationaryPeek = T.notch.autoTimer;
assert.equal(timers.get(stationaryPeek)?.delay, 3000);
widgetListeners.get("pointerenter")({ target: headerHover, clientX: 100, clientY: 20 });
assert.equal(T.notch.inspectTimer, null, "a stationary pointer under an automatic reveal is not deliberate hover");
fireTimer(stationaryPeek);
assert.equal(T.notch.mode, "controls");

widgetListeners.get("pointerleave")();
const pointerExit = T.notch.exitTimer; if (pointerExit) fireTimer(pointerExit);
T.notch.hover = false; T.notch.hoverSuppressed = false; T.notch.qualifiedHover = false;
T.notch.globalPointerX = 95; T.notch.globalPointerY = 20; T.notch.activeKeys = new Set(); T.notch.workStates.clear();
T.syncNotch(1, [{ key: "Codex:deliberate-entry", active: true, runStartedAt: new Date(testNow).toISOString() }]);
const deliberatePeek = T.notch.autoTimer;
widgetListeners.get("pointerenter")({ target: headerHover, clientX: 102, clientY: 20 });
const dwell = T.notch.inspectTimer;
assert.equal(timers.get(dwell)?.delay, 60, "qualified hover waits 60ms, including at the toolbar");
fireTimer(dwell); assert.equal(T.notch.mode, "inspect");
fireTimer(deliberatePeek); assert.equal(T.notch.mode, "inspect", "qualified hover outlives the three-second peek");
widgetListeners.get("pointerleave")();
const exitGrace = T.notch.exitTimer;
assert.equal(T.notch.mode, "inspect", "inspection remains open before the 110ms exit grace completes");
assert.equal(timers.get(exitGrace)?.delay, 110);
fireTimer(exitGrace); assert.equal(T.notch.mode, "controls");

T.notch.active = 1; T.notch.activeKeys = new Set(["Codex:escape-hover"]); T.notch.qualifiedHover = true;
T.notch.hover = true; T.state.searchOpen = true; T.setNotch(false, true);
const escapeEvent = () => ({ key: "Escape", target: { tagName: "BODY" }, immediateStopped: false,
  preventDefault() {}, stopImmediatePropagation() { this.immediateStopped = true; } });
const firstEscape = escapeEvent(); T.handleKey(firstEscape);
assert.equal(firstEscape.immediateStopped, true); assert.equal(T.state.searchOpen, false);
assert.equal(T.notch.mode, "inspect", "Escape closes search before cancelling qualified hover");
const secondEscape = escapeEvent(); dispatchDocument("keydown", secondEscape); T.handleKey(secondEscape);
assert.equal(T.notch.mode, "controls", "the next Escape releases transient inspection");
widgetListeners.get("pointermove")({ target: headerHover, clientX: 102, clientY: 20 });
assert.equal(T.notch.mode, "controls", "the current hover remains suppressed after Escape");
widgetListeners.get("pointerleave")();

T.keepOpenState.value = true; T.notch.hover = false; T.notch.qualifiedHover = false; T.notch.active = 0;
T.setNotch(false, true);
assert.equal(T.notch.mode, "controls", "keep-open does not open an empty panel when no chats are active");
T.notch.active = 1; T.setNotch(false, true); T.handleKey(escapeEvent());
assert.equal(T.keepOpenState.value, true); assert.equal(T.notch.mode, "inspect", "Escape cannot clear persistent keep-open");
T.notch.active = 0;

T.setupWindowDrag();
const dragBegins = [], dragEnds = []; let nextGeneration = 40, acceptCompactDrag = true;
context.window.pywebview.api.begin_drag = async mode => {
  dragBegins.push(mode);
  return acceptCompactDrag ? { accepted: true, generation: nextGeneration++ } : { accepted: false };
};
context.window.pywebview.api.end_drag = async (generation, commit) => { dragEnds.push([generation, commit]); return true; };
const dragTarget = (interactive = false) => ({ closest(selector) {
  if (selector === ".compact-toolbar.pywebview-drag-region,.compact-summary.pywebview-drag-region") return {};
  if (selector.includes("button,input,textarea")) return interactive ? {} : null;
  return null;
} });
const settleDrag = () => new Promise(resolve => setImmediate(resolve));
const startDrag = target => dispatchDocument("mousedown", { target, button: 0 });
const endDrag = () => dispatchDocument("mouseup", { type: "mouseup", button: 0 });
T.widgetLock.known = true; T.widgetLock.locked = true;
for (const [mode, region] of [["controls", "compact-toolbar"], ["inspect", "compact-summary"]]) {
  T.notch.mode = mode; T.notch.expanded = mode === "inspect"; documentRef.body.dataset.notchMode = mode;
  const expectedGeneration = nextGeneration;
  startDrag(dragTarget(false)); await settleDrag(); endDrag(); await settleDrag();
  assert.equal(dragBegins.at(-1), "compact-v1");
  assert.deepEqual(dragEnds.at(-1), [expectedGeneration, true], `${region} commits with its accepted generation on left mouseup while position-locked`);
  assert.equal(T.widgetLock.locked, true, "compact dragging does not silently change lock state");
}
for (const [eventType, dispatch] of [
  ["pointercancel", () => dispatchDocument("pointercancel", { type: "pointercancel" })],
  ["blur", () => { for (const handler of windowListeners.get("blur") || []) handler({ type: "blur" }); }]
]) {
  T.notch.mode = "controls"; T.notch.expanded = false; documentRef.body.dataset.notchMode = "controls";
  const expectedGeneration = nextGeneration;
  const endsBefore = dragEnds.length;
  startDrag(dragTarget(false)); await settleDrag();
  assert.equal(dragEnds.length, endsBefore, `${eventType} must not end before a matching cancellation`);
  dispatch(); await settleDrag();
  assert.deepEqual(dragEnds.at(-1), [expectedGeneration, false], `${eventType} rolls back the matching accepted generation`);
}
const beginCount = dragBegins.length, endCount = dragEnds.length;
startDrag(dragTarget(true)); endDrag();
// The second gesture uses a non-left button and must also be ignored.
dispatchDocument("mousedown", { target: dragTarget(false), button: 2 }); endDrag(); await settleDrag();
assert.equal(dragBegins.length, beginCount, "buttons and non-left clicks are excluded from compact drag");
acceptCompactDrag = false; startDrag(dragTarget(false)); await settleDrag(); endDrag(); await settleDrag();
assert.equal(dragEnds.length, endCount, "a native drag without an accepted generation never ends");

const safePopover = { dataset: { action: "watcher" } };
T.state.popover = safePopover; T.state.searchOpen = true; T.state.query = "preserved query"; node("#chat-search").value = "preserved query";
const emptySnapshot = { generatedAt: new Date(testNow).toISOString(), enabledProviders: ["Codex"], providers: [{ ...provider, chats: [] }] };
T.render(emptySnapshot);
assert.equal(T.state.searchOpen, true); assert.equal(T.state.query, "preserved query");
assert.equal(node("#chat-search").value, "preserved query"); assert.equal(node("#chats-head").hidden, false);
assert.equal(T.state.popover, safePopover, "a safe popover survives an empty work roster");
testNow += 10000;
T.render({ generatedAt: new Date(testNow - 60000).toISOString(), enabledProviders: ["Codex"], providers: [provider] });
assert.equal(T.state.searchOpen, true); assert.equal(T.state.query, "preserved query");
assert.equal(node("#chats-head").hidden, false); assert.equal(T.state.popover, safePopover);
assert.equal(node("#chat-list").innerHTML, '<div class="empty">No matches</div>', "stale evidence cannot leave old chat rows on screen; a searched empty result stays quiet");

// A compact refresh keeps its icon while pending, then clears busy state on either outcome.
node("#popover").hidden = true; T.state.popover = null; T.state.searchOpen = false; T.state.query = "";
T.state.snapshot = null; T.state.loading = false; T.state.refreshing = false; T.state.refreshPending = false;
T.state.refreshPendingForce = false; T.state.live = null; T.state.watcher = null; T.notch.active = 0;
const syncAge = node("#sync-age"), pendingStats = [];
const freshSnapshot = { generatedAt: new Date(testNow).toISOString(), enabledProviders: ["Codex"], providers: [{ ...provider, chats: [] }] };
context.fetch = async url => {
  if (url.startsWith("/api/stats")) return await new Promise(resolve => pendingStats.push(resolve));
  if (url === "/api/limit-reset") return { ok: true, json: async () => ({ providers: {} }) };
  throw new Error(`unexpected refresh request: ${url}`);
};
const clickRefresh = async () => {
  T.handleClick({ target: { closest: selector => selector === "[data-action]" ? { dataset: { action: "refresh" } } : null } });
  await Promise.resolve();
  assert.equal(pendingStats.length, 1, "refresh waits on its stats request");
  assert.match(syncAge.innerHTML, /<svg class="ic"/);
  assert.match(syncAge.className, /\bis-loading\b/);
  assert.equal(syncAge.getAttribute("aria-busy"), "true");
};
await clickRefresh();
pendingStats.shift()({ ok: true, json: async () => freshSnapshot });
await new Promise(resolve => setImmediate(resolve));
assert.doesNotMatch(syncAge.className, /\bis-loading\b/);
assert.equal(syncAge.getAttribute("aria-busy"), "false");
assert.match(syncAge.innerHTML, /<svg class="ic"/);
const beforeClockTick = JSON.stringify(T.state.snapshot), beforeClockIntervals = intervalCalls.length;
const expectedLocalTime = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", hourCycle: "h12" }).format(new Date(testNow));
T.tick();
assert.equal(node("#compact-clock").textContent, expectedLocalTime);
assert.equal(node("#toolbar-clock").textContent, expectedLocalTime);
assert.equal(JSON.stringify(T.state.snapshot), beforeClockTick, "clock refresh does not invent or rewrite provider data");
assert.equal(intervalCalls.length, beforeClockIntervals, "the local clock reuses tick instead of adding an interval");
await clickRefresh();
pendingStats.shift()({ ok: false, status: 503, json: async () => ({}) });
await new Promise(resolve => setImmediate(resolve));
assert.doesNotMatch(syncAge.className, /\bis-loading\b/);
assert.equal(syncAge.getAttribute("aria-busy"), "false");
assert.match(syncAge.innerHTML, /<svg class="ic"/);

// Tall-root classification is driven by positive, visible layout geometry and settles through the existing RAF pass.
const forestForGeometry = T.buildForest([{ ...provider, chats: [parent, child] }]);
T.state.forest = forestForGeometry;
let measuredRowHeight = 80, rootTop = 0, rowTop = 0, childOffset = 0, visibleAgentRows;
const rowClasses = new Set(), makeRect = (left, top, width, height) => ({ left, top, width, height, right: left + width, bottom: top + height });
const titleNode = { getBoundingClientRect: () => makeRect(20, 5, 150, 19) };
const parentAnchor = { getBoundingClientRect: () => makeRect(4, rowTop + 10, 4, 4) };
const parentRow = {
  classList: { contains: name => rowClasses.has(name), toggle(name, enabled) { enabled ? rowClasses.add(name) : rowClasses.delete(name); } },
  getBoundingClientRect: () => makeRect(0, rowTop, 420, measuredRowHeight),
  querySelector(selector) { return selector === ".r-title" ? titleNode : selector === ".runway-refined-tree-anchor" ? parentAnchor : null; },
};
const childAnchor = { getBoundingClientRect: () => makeRect(12, 50 + childOffset, 4, 4) };
const glanceNode = { getBoundingClientRect: () => makeRect(8, 42 + childOffset, 400, 24), querySelector: selector => selector === ".runway-refined-tree-anchor" ? childAnchor : null };
const childName = { getBoundingClientRect: () => makeRect(30, 43 + childOffset, 160, 19) };
const agentEl = {
  dataset: { sub: forestForGeometry.rows.find(item => item.parentId === parent.id).key },
  _leaving: false, getClientRects: () => [makeRect(0, 40 + childOffset, 420, 30)], getBoundingClientRect: () => makeRect(0, 40 + childOffset, 420, 30),
  querySelector(selector) { return selector === ".runway-agent-glance" ? glanceNode : selector === ".runway-agent-name" ? childName : null; },
};
visibleAgentRows = [agentEl];
const rootElement = {
  dataset: { key: forestForGeometry.roots[0].key }, children: [parentRow],
  classList: { contains: name => name === "chat" },
  getBoundingClientRect: () => makeRect(0, rootTop, 420, 360),
  querySelector: selector => selector === ".row" ? parentRow : null,
  querySelectorAll: selector => selector === ".ag.runway-agent[data-sub]" ? visibleAgentRows : [],
  appendChild(el) { this.children.push(el); el.parentNode = this; return el; },
};
const chatList = node("#chat-list"); chatList.children = [rootElement];
const layoutNode = node(".layout"); let layoutClientHeight = 300, layoutHeightReads = 0;
Object.defineProperty(layoutNode, "clientHeight", { configurable: true, get() { layoutHeightReads++; return layoutClientHeight; }, set(value) { layoutClientHeight = value; } });
documentRef.createElement = () => ({});
documentRef.createElementNS = () => ({
  dataset: {}, children: [], attributes: {}, replaceCount: 0, classList: { contains(name) { return this.name === name; }, name: "runway-tree-connectors" },
  setAttribute(name, value) { this.attributes[name] = String(value); if (name === "class") this.classList.name = value; },
  appendChild(el) { this.children.push(el); el.parentNode = this; return el; },
  replaceChildren(...children) { this.replaceCount++; this.children = children; },
  remove() { if (this.parentNode) { this.parentNode.children = this.parentNode.children.filter(child => child !== this); this.parentNode = null; } },
});
let frameId = 0; const queuedFrames = new Map();
context.requestAnimationFrame = callback => { const id = ++frameId; queuedFrames.set(id, callback); return id; };
context.cancelAnimationFrame = id => queuedFrames.delete(id);
const runGeometryFrame = () => { const [id, callback] = queuedFrames.entries().next().value || []; assert.ok(callback, "expected a scheduled geometry frame"); queuedFrames.delete(id); callback(); };
const inspectGeometry = () => { T.notch.active = 1; T.notch.inspecting = true; T.notch.manual = false; T.keepOpenState.value = false; T.state.searchOpen = false; T.state.popover = null; T.setNotch(false); runGeometryFrame(); };
measuredRowHeight = 80; inspectGeometry();
assert.equal(rowClasses.has("runway-tall-root"), false, "ordinary-height roots keep sticky alignment");
assert.equal(queuedFrames.size, 0, "unchanged geometry does not schedule an extra frame");
measuredRowHeight = 120; inspectGeometry();
assert.equal(rowClasses.has("runway-tall-root"), true, "a root taller than one third of visible layout receives the non-sticky lane class");
assert.equal(queuedFrames.size, 1, "a class change schedules one settling geometry frame");
runGeometryFrame(); assert.equal(queuedFrames.size, 0, "settling does not create a frame loop");
measuredRowHeight = 80; inspectGeometry();
assert.equal(rowClasses.has("runway-tall-root"), false, "resizing the root below threshold restores normal alignment");
assert.equal(queuedFrames.size, 1); runGeometryFrame();
layoutNode.clientHeight = 0; measuredRowHeight = 200; inspectGeometry();
assert.equal(rowClasses.has("runway-tall-root"), false, "hidden or zero-height layout geometry cannot poison the sticky class");
assert.equal(queuedFrames.size, 0);
const scrollListeners = layoutListeners.get("scroll") || [];
assert.equal(scrollListeners.length, 1, "compact inspection installs one layout scroll listener");
assert.equal(scrollListeners[0].options?.passive, true, "layout scroll tracking is passive");
const onLayoutScroll = scrollListeners[0].handler;
layoutClientHeight = 300; measuredRowHeight = 80; rootTop = 0; rowTop = 0; childOffset = 0; visibleAgentRows = [agentEl]; inspectGeometry();
assert.equal(rowClasses.has("runway-root-stuck"), false, "a root at its own flow top uses the transparent rest surface");
const svg = rootElement.children.find(el => el.classList?.contains("runway-tree-connectors"));
assert.ok(svg, "one active child creates the root SVG");
const restSvgSignature = svg.dataset.geometry;
const notchBeforeScroll = { mode: T.notch.mode, expanded: T.notch.expanded, frame: T.notch.frame };
rootTop = -40; rowTop = 0; childOffset = -40;
layoutHeightReads = 0;
onLayoutScroll(); onLayoutScroll();
assert.equal(queuedFrames.size, 1, "multiple scroll events coalesce into one geometry frame");
assert.equal(layoutHeightReads, 0, "scroll-only geometry does not read or recalculate notch size");
assert.equal(T.notch.frame, notchBeforeScroll.frame, "scroll-only geometry does not borrow the notch frame handle");
runGeometryFrame();
assert.equal(rowClasses.has("runway-root-stuck"), true, `a row displaced from its root is marked stuck (mode=${T.notch.mode}, rootTop=${rootTop}, rowTop=${rowTop})`);
assert.deepEqual({ mode: T.notch.mode, expanded: T.notch.expanded, frame: T.notch.frame }, notchBeforeScroll, "scroll synchronization does not mutate notch state");
assert.equal(layoutHeightReads, 0); assert.equal(queuedFrames.size, 0);
const unchangedSvgSignature = svg.dataset.geometry, unchangedSvgReplacements = svg.replaceCount;
onLayoutScroll(); runGeometryFrame();
assert.equal(rowClasses.has("runway-root-stuck"), true, "an unchanged SVG signature does not clear stuck paint state");
assert.equal(svg.dataset.geometry, unchangedSvgSignature); assert.equal(svg.replaceCount, unchangedSvgReplacements, "unchanged geometry leaves SVG children untouched");
assert.equal(svg.dataset.geometry, restSvgSignature, "scrolling never redraws the tree");
inspectGeometry(); queuedFrames.clear();
assert.equal(svg.dataset.geometry, restSvgSignature, "a stuck parent is measured where it sits unstuck, so the tree keeps its geometry");
visibleAgentRows = []; inspectGeometry(); queuedFrames.clear();
assert.equal(rowClasses.has("runway-root-stuck"), true, "stuck state is updated before the no-path cleanup");
assert.equal(rootElement.children.some(el => el.classList?.contains("runway-tree-connectors")), false, "the empty tree removes its stale SVG");
rowTop = rootTop; onLayoutScroll(); runGeometryFrame();
assert.equal(rowClasses.has("runway-root-stuck"), false, "pathless roots clear stuck paint state when they return to flow");
rootTop = -320; rowTop = -40; onLayoutScroll(); runGeometryFrame();
assert.equal(rowClasses.has("runway-root-stuck"), true, "bottom-constrained sticky displacement remains detected");
rootTop = 0; rowTop = -3; onLayoutScroll(); runGeometryFrame();
assert.equal(rowClasses.has("runway-root-stuck"), false, "negative above-flow displacement does not paint the opaque surface");
rowClasses.add("runway-tall-root"); rowTop = rootTop; onLayoutScroll(); runGeometryFrame();
assert.equal(rowClasses.has("runway-root-stuck"), false, "nonsticky tall roots clear the stuck surface"); rowClasses.delete("runway-tall-root");
delete context.requestAnimationFrame; delete context.cancelAnimationFrame;

// Time, status, activity, workspace, and tokens have shared columns at every depth.
assert.match(css, /\[data-notch-mode="inspect"\]:not\(\.phone\) \.runway-refined-row\{\s*grid-template-columns:minmax\(0,2fr\) 58px 64px minmax\(0,1fr\) 84px 80px/);
assert.match(css, /\.ag\.runway-agent \.runway-refined-row\{padding-left:30px/);
assert.match(css, /\.runway-refined-timing\{grid-column:2/);assert.match(css,/\.runway-refined-status\{grid-column:3/);
assert.match(css,/\.runway-refined-activity\{grid-column:4/);assert.match(css,/\.runway-refined-path\{grid-column:5/);assert.match(css,/\.runway-refined-tokens\{grid-column:6/);

// A Claude chat waiting on a permission prompt has no question text but still explains itself.
{
  const claudeWaiting = { id: "s-perm", key: "Claude:s-perm", provider: "Claude", title: "Perm", status: "needs-input", executionStatus: "waiting", stateSince: stamp(1), updatedAt: stamp(0) };
  assert.match(T.runwayQuestionPopover(claudeWaiting), /Waiting for your approval in Claude/);
  const asked = { ...claudeWaiting, pendingQuestion: "Where should it run?", pendingOptions: ["Hosted", "Local"], pendingQuestionAt: stamp(0) };
  assert.match(T.runwayQuestionPopover(asked), /Where should it run\?/);
  assert.doesNotMatch(T.rowHTML(asked), /in Codex/, "Claude rows never point at Codex");
}
// Each information column has its own centred track instead of sharing or shifting with content.
assert.match(css, /grid-template-columns:minmax\(0,2fr\) 58px 64px minmax\(0,1fr\) 84px 80px/);
// Timing (58px) holds the longest elapsed text ("12h 59m", "12d 23h"); 44px clipped "1h 06m".
assert.doesNotMatch(css, /grid-template-columns:minmax\(0,2fr\) (44px 70px|60px 70px)/);

// The yellow waiting count is the way to the waiting chat.
T.renderRunwayCounts({ roots: 2, total: 3, waiting: 1 });
assert.match(node("#notch-counts").innerHTML, /data-count-slot="waiting"[^>]*data-action="jump-waiting"[^>]*role="button"/);
T.renderRunwayCounts({ roots: 2, total: 3, waiting: 0 });
assert.doesNotMatch(node("#notch-counts").innerHTML, /jump-waiting/, "no jump target when nothing waits");
