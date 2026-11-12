import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import {forestProviders, reportTree} from "./fixtures/debug_report_forest.mjs";

const dashboardSource = fs.readFileSync(new URL("../token_tracker/static/dashboard.js", import.meta.url), "utf8");
const appSource = fs.readFileSync(new URL("../token_tracker/static/app.js", import.meta.url), "utf8");
const REPORT = "# Branchglance debug report\n\nBody";
const HOSTILE = "# Branchglance debug report\n<img src=x onerror=alert(1)>";

function bootDashboard({fetchText = REPORT, fetchOk = true, platform = "Win32", version = "1.2.3"} = {}) {
  const calls = {fetch: [], clipboard: [], open: []};
  const elements = new Map();
  const context = {
    window: {__TOKEN_TRACKER_DASHBOARD_TEST__: true, addEventListener() {},
      open(...args) { calls.open.push(args); }},
    document: {querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {},
      execCommand() { return false; }},
    navigator: {platform, clipboard: {writeText: async text => { calls.clipboard.push(text); }}},
    localStorage: {getItem: () => null, setItem() {}},
    location: {origin: "http://127.0.0.1:8765"},
    Intl, Date, Math, Number, JSON, Map, Set, Array, Promise, URL,
    fetch: async (url, options) => {
      calls.fetch.push({url: String(url), options});
      return {ok: fetchOk, status: fetchOk ? 200 : 503, text: async () => fetchText, json: async () => ({})};
    },
  };
  vm.createContext(context);
  vm.runInContext(dashboardSource, context);
  const dashboard = context.window.__tokenTrackerDashboardTest;
  dashboard.state.identity = {version};
  return {dashboard, calls, elements};
}

// Settings UI: preview, copy and the issue URL.
{
  const {dashboard, calls} = bootDashboard({platform: "Win32", version: "1.2.3"});
  const html = dashboard.settingsHTML();
  assert.match(html, /data-dashboard-action="debug-preview"/);
  assert.match(html, /data-dashboard-action="debug-copy"/);
  assert.match(html, /data-dashboard-action="debug-issue"/);
  assert.match(html, /<textarea class="dash-debug-text" id="dash-debug-report" readonly/);
  assert.match(html, /Open GitHub issue/);

  const issue = new URL(dashboard.debugIssueURL());
  assert.equal(issue.origin + issue.pathname, "https://github.com/apratxxm/branchglance/issues/new");
  assert.deepEqual([...issue.searchParams.keys()], ["template", "version", "os"]);
  assert.equal(issue.searchParams.get("template"), "bug_report.yml");
  assert.equal(issue.searchParams.get("version"), "1.2.3");
  assert.equal(issue.searchParams.get("os"), "Windows");

  const hostile = bootDashboard({version: "1.2.3&os=x&body=secret"}).dashboard;
  const hostileURL = new URL(hostile.debugIssueURL());
  assert.deepEqual([...hostileURL.searchParams.keys()], ["template", "os"], "unvalidated version is dropped");
  assert.equal(hostileURL.searchParams.get("os"), "Windows");
  assert.equal(new URL(bootDashboard({platform: "MacIntel"}).dashboard.debugIssueURL()).searchParams.get("os"), "macOS");
  assert.equal(new URL(bootDashboard({platform: "Plan9"}).dashboard.debugIssueURL()).searchParams.get("os"), "unknown");

  // Copy before preview is refused; nothing is sent to the clipboard.
  await dashboard.copyDebugReport();
  assert.equal(calls.clipboard.length, 0);
  assert.equal(dashboard.state.debugStatus, "Preview the report first.");
}

{
  const {dashboard, calls} = bootDashboard({fetchText: HOSTILE});
  await dashboard.previewDebugReport();
  assert.equal(calls.fetch.length, 1);
  assert.equal(calls.fetch[0].url, "/api/debug-report");
  assert.equal(calls.fetch[0].options.cache, "no-store");
  assert.equal(dashboard.state.debugReport, HOSTILE);
  const rendered = dashboard.settingsHTML();
  assert.match(rendered, /&lt;img src=x onerror=alert\(1\)&gt;/, "preview text is escaped, never HTML");
  assert.doesNotMatch(rendered, /<img src=x/);

  await dashboard.copyDebugReport();
  assert.deepEqual(calls.clipboard, [HOSTILE], "copy puts the exact report text on the clipboard");
  assert.equal(dashboard.state.debugStatus, "Copied.");

  dashboard.openDebugIssue();
  assert.equal(calls.open.length, 1);
  const [url, target, features] = calls.open[0];
  assert.equal(url, dashboard.debugIssueURL());
  assert.equal(target, "_blank");
  assert.equal(features, "noopener,noreferrer");
  assert.equal(new URL(url).searchParams.has("body"), false, "the report is never placed in the URL");
}

{
  const {dashboard, calls} = bootDashboard({fetchOk: false});
  await dashboard.previewDebugReport();
  assert.equal(dashboard.state.debugReport, undefined);
  assert.equal(dashboard.state.debugError, true);
  assert.equal(dashboard.state.debugStatus, "Debug report is unavailable.");
  assert.equal(calls.clipboard.length, 0);
}

// Report-derived tree rendered through app.js buildForest, using the synthetic fixture.
function bootApp() {
  const timers = [];
  const element = () => ({innerHTML: "", textContent: "", value: "", hidden: false, dataset: {}, style: {},
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {},
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    getBoundingClientRect() { return {left: 0, top: 0, width: 0, height: 0}; }, getClientRects() { return []; }});
  const context = {
    window: {__TOKEN_TRACKER_TEST__: true, __TOKEN_TRACKER_RUNWAY__: true, __TOKEN_TRACKER_REFINED__: true, __TOKEN_TRACKER_COMPACT__: true, addEventListener() {}},
    document: {body: {dataset: {}, classList: {add() {}, remove() {}, toggle() {}}},
      querySelector: () => element(), querySelectorAll: () => [], documentElement: {style: {setProperty() {}}},
      hidden: false, activeElement: null, addEventListener() {}},
    localStorage: {getItem: () => null, setItem() {}},
    fetch: async () => ({ok: true, json: async () => ({})}),
    Intl, Date, Math, Number, JSON, AbortController, AbortSignal, URLSearchParams,
    setTimeout(callback) { timers.push(callback); return timers.length; }, clearTimeout() {}, setInterval() { return 0; },
  };
  context.window.addEventListener = () => {};
  vm.createContext(context);
  vm.runInContext(appSource, context);
  return context.window.__tokenTrackerTest;
}

{
  const fixedNow = Date.parse("2026-11-12T12:00:00Z");
  const rows = [
    {alias: "r1", provider: "Claude", kind: "chat", depth: 0, workspaceKind: "unknown", model: "Claude Opus 4.7",
      status: "running", runStartedAt: -312, updatedAt: "<1m", dropped: ["title", "cwd"]},
    {alias: "r2", provider: "Claude", kind: "subagent", depth: 1, parent: "r1", workspaceKind: "unknown",
      model: "Claude Haiku 5.5", status: "quiet", linkSource: "process", activityKind: "reading",
      hasPendingQuestion: true, runStartedAt: -120, dropped: []},
    {alias: "r3", provider: "Codex", kind: "exec", depth: 0, workspaceKind: "scratch", model: "GPT-6.1 Sol",
      status: "idle", stateDetail: "completed", executionStatus: "idle", executionSource: "transcript",
      stoppedAt: -40, dropped: []},
    {alias: "r4", provider: "Codex", kind: "subagent", depth: 1, parent: "r3", workspaceKind: "unknown",
      model: "custom/unknown", status: "running", firstSeenAt: -60, dropped: ["prompt"]},
    {alias: "r5", provider: "Claude", kind: "chat", depth: 0, workspaceKind: "unknown", model: "Claude Opus 4.7",
      status: "running", executionStatus: "running", executionSource: "hook", executionObservedAt: -10,
      runStartedAt: -200, dropped: []},
  ];
  const report = ["# Branchglance debug report", "", "## Agent tree", "", "```json",
    JSON.stringify(rows, null, 2), "```", "", "## Recent problems", "", "```json", "[]", "```", ""].join("\n");
  assert.deepEqual(reportTree(report), rows);

  const T = bootApp();
  const {roots, rows: forestRows} = T.buildForest(forestProviders(rows, {now: fixedNow}));
  assert.equal(forestRows.length, 5);
  const byId = new Map(forestRows.map(row => [row.id, row]));
  assert.equal(byId.get("r2").pendingQuestion, "Synthetic question");
  assert.equal(byId.get("r2").activity?.kind, "reading");
  assert.equal(byId.get("r1").title, "Synthetic r1");
  assert.equal(byId.get("r1").runStartedAt, new Date(fixedNow - 312000).toISOString());
  // Arrays built inside the vm realm are spread into this realm before strict comparison.
  const rootIds = [...roots.map(root => root.id)].sort();
  assert.deepEqual(rootIds, ["r1", "r3", "r5"]);
  const r1 = roots.find(root => root.id === "r1");
  assert.deepEqual([...r1.children.map(child => child.id)], ["r2"]);
  assert.equal(r1.children[0].depth, 1);
  const r3 = roots.find(root => root.id === "r3");
  assert.deepEqual([...r3.children.map(child => child.id)], ["r4"]);
}

// Runway-mode predicates on report-derived rows. Freshness uses the real clock because app.js reads Date.now().
{
  const now = Date.now();
  const rows = [
    {alias: "q1", provider: "Claude", kind: "chat", depth: 0, workspaceKind: "unknown", model: "Claude Opus 4.7",
      status: "quiet", hasPendingQuestion: true, pendingQuestionExpired: true, dropped: []},
    {alias: "q2", provider: "Claude", kind: "chat", depth: 0, workspaceKind: "unknown", model: "Claude Opus 4.7",
      status: "quiet", hasPendingQuestion: true, pendingQuestionExpired: false, dropped: []},
    {alias: "u1", provider: "Codex", kind: "chat", depth: 0, workspaceKind: "scratch", model: "GPT-6.1 Sol",
      status: "needs input", hasPendingQuestion: true, telemetryUnavailable: true, activityUnavailable: true,
      metricsUnavailable: true, dropped: []},
    {alias: "x1", provider: "Codex", kind: "exec", depth: 0, workspaceKind: "scratch", model: "GPT-6.1 Sol",
      status: "idle", stateDetail: "stopped", retainedRun: true, stoppedAt: -120, dropped: []},
    {alias: "x2", provider: "Codex", kind: "exec", depth: 0, workspaceKind: "scratch", model: "GPT-6.1 Sol",
      status: "idle", stateDetail: "stopped", retainedRun: true, stoppedAt: -1000, dropped: []},
    {alias: "h1", provider: "Claude", kind: "chat", depth: 0, workspaceKind: "unknown", model: "Claude Opus 4.7",
      status: "running", executionStatus: "running", executionSource: "hook", executionObservedAt: -10, dropped: []},
    {alias: "t1", provider: "Codex", kind: "chat", depth: 0, workspaceKind: "scratch", model: "GPT-6.1 Sol",
      status: "running", executionStatus: "running", executionSource: "transcript", executionObservedAt: -200,
      dropped: []},
  ];
  const report = ["## Agent tree", "", "```json", JSON.stringify(rows, null, 2), "```", ""].join("\n");
  assert.deepEqual(reportTree(report), rows);

  const T = bootApp();
  assert.equal(T.runway, true);
  const {rows: forestRows} = T.buildForest(forestProviders(rows, {now}));
  const byId = new Map(forestRows.map(row => [row.id, row]));

  // Expired question: no longer asking and not waiting; the unexpired control still waits.
  assert.equal(byId.get("q1").pendingQuestionExpired, true);
  assert.equal(T.isAsking(byId.get("q1")), false);
  assert.equal(T.runwayWaiting(byId.get("q1")), false);
  assert.equal(T.isAsking(byId.get("q2")), true);
  assert.equal(T.runwayWaiting(byId.get("q2")), true);

  // Unavailable telemetry: the question is still pending, but the runway does not report it as waiting.
  assert.equal(byId.get("u1").telemetryUnavailable, true);
  assert.equal(byId.get("u1").activityUnavailable, true);
  assert.equal(byId.get("u1").metricsUnavailable, true);
  assert.equal(T.isAsking(byId.get("u1")), true);
  assert.equal(T.runwayWaiting(byId.get("u1")), false);

  // Retained run: visible for 15 minutes after it stopped, then not.
  assert.equal(byId.get("x1").retainedRun, true);
  assert.equal(T.runwayVisibleActive(byId.get("x1")), true);
  assert.equal(T.runwayVisibleActive(byId.get("x2")), false);

  // Execution evidence: a hook row with fresh observation is execution; a stale transcript row is not.
  assert.equal(T.runwayExecutionView(byId.get("h1"))?.source, "hook");
  assert.equal(T.runwayExecutionView(byId.get("t1")), null);
}
