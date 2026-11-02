import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { test } from "node:test";

const app = fs.readFileSync(new URL("../token_tracker/static/app.js", import.meta.url), "utf8");
const baseNow = Date.parse("2026-10-21T10:00:00.000Z");
assert.match(app,/renderLoading\(\);if\(RUNWAY\)\{void loadProviderPolicy\(\);void loadQuota\(\);\}refresh\(\)/,"desktop startup loads policy and quota beside the unchanged full-stat refresh");

function harness(fetchImpl = async () => ({ ok: true, json: async () => ({ providers: {} }) })) {
  let now = baseNow, fetcher = fetchImpl;
  const nodes = new Map();
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, { selector, innerHTML: "", textContent: "", value: "", hidden: false, title: "", dataset: {}, attributes: {},
      style: { setProperty() {} }, classList: { add() {}, remove() {}, toggle() {} }, setAttribute(name, value) { this.attributes[name] = String(value); },
      getAttribute(name) { return this.attributes[name] ?? null; }, removeAttribute(name) { delete this.attributes[name]; }, querySelectorAll() { return []; },
      querySelector() { return null; }, addEventListener() {}, removeEventListener() {}, contains() { return false; }, focus() {}, blur() {} });
    return nodes.get(selector);
  };
  class TestDate extends Date { constructor(...args) { super(...(args.length ? args : [now])); } static now() { return now; } }
  const document = { hidden: false, activeElement: null, querySelector: node, querySelectorAll: () => [], addEventListener() {},
    body: { dataset: {}, classList: { toggle() {} } } };
  const context = { window: { __TOKEN_TRACKER_TEST__: true, __TOKEN_TRACKER_RUNWAY__: true, __TOKEN_TRACKER_REFINED__: true }, document,
    localStorage: { getItem(key) { return key === "tokenTrackerRunwayHiddenSeedV1" ? "1" : null; }, setItem() {} },
    fetch: (...args) => fetcher(...args), Intl, Date: TestDate, Math, Number, JSON, AbortController, AbortSignal,
    setTimeout() { return 1; }, clearTimeout() {}, setInterval() { return 1; } };
  vm.createContext(context); vm.runInContext(app, context);
  return { T: context.window.__tokenTrackerTest, nodes, setNow(value) { now = value; }, setFetch(value) { fetcher = value; } };
}

const iso = value => new Date(value).toISOString();
const emptySnapshot = (limitsUpdatedAt = null, cache = undefined) => {
  const snapshot = { generatedAt: iso(baseNow - 2000), enabledProviders: ["Codex"], providers: [{ name: "Codex", connected: true, chats: [],
    limitsUpdatedAt, fiveHour: { percent: 20, resetAt: 1_800_000_000 }, weekly: { percent: 30, resetAt: 1_900_000_000 } }] };
  if (cache !== undefined) snapshot.snapshotCache = cache;
  return snapshot;
};
const watcher = (checkedAt, five = 80, weekly = 70, resetAt = 1_900_000_100) => ({ providers: { Codex: { lastStatus: {
  five_hour_percent: five, weekly_percent: weekly, reset_at: resetAt, weekly_reset_at: resetAt + 100, ...(checkedAt === undefined ? {} : { checkedAt })
} } } });
const cacheMeta = (overrides = {}) => ({ generation: 4, builtAt: iso(baseNow - 5000), ageSeconds: 5, building: false, stale: false,
  lastFailureAt: null, lastFailureCategory: null, ...overrides });
const quotaPayload = (sample=null,meta={}) => ({ providers: { Codex: sample ? { lastStatus: sample } : {} }, quotaCache: {
  ready:Boolean(sample),fresh:Boolean(sample),ageSeconds:sample?0.25:null,refreshing:!sample,errorCategory:null,...meta
} });

test("watcher quotas never replace newer dated stats or watcher observations", () => {
  const { T } = harness();
  const statsAt = iso(baseNow - 3000), snapshot = emptySnapshot(statsAt); T.state.snapshot = snapshot;
  assert.equal(T.applyWatcherLimits(watcher(iso(baseNow - 4000), 90)), false);
  assert.equal(snapshot.providers[0].fiveHour.percent, 20);
  assert.equal(T.applyWatcherLimits(watcher(statsAt, 55, 66, null)), false, "equal checkedAt keeps the current canonical stats sample");
  assert.equal(snapshot.providers[0].fiveHour.percent, 20);
  const newerAt=iso(baseNow-2000);
  assert.equal(T.applyWatcherLimits(watcher(newerAt,55,66,null)),true,"strictly newer watcher data is accepted");
  assert.equal(snapshot.providers[0].fiveHour.percent, 55);
  assert.equal(snapshot.providers[0].fiveHour.resetAt, 1_800_000_000, "missing reset preserves the known deadline");
  assert.equal(snapshot.providers[0].weekly.percent, 66);
  assert.equal(snapshot.providers[0].limitsUpdatedAt, newerAt);

  const undated = emptySnapshot(null); T.state.snapshot = undated;
  assert.equal(T.applyWatcherLimits(watcher(iso(baseNow - 1000), 61), 3), true);
  assert.equal(T.applyWatcherLimits(watcher(iso(baseNow - 2000), 92), 4), false, "older watcher observation cannot regress an accepted sample");
  assert.equal(undated.providers[0].fiveHour.percent, 61);
});

test("full snapshots reconcile only quota fields against the freshest accepted evidence", () => {
  const { T } = harness(),t10=iso(baseNow-10000),t20=iso(baseNow-8000),t30=iso(baseNow-6000);
  const initial=emptySnapshot(t10);initial.providers[0].contextPercent=63;initial.providers[0].chats=[{id:"unchanged-chat"}];
  T.state.snapshot=initial;
  const latest=watcher(t20,40,50);T.state.watcher=latest;
  assert.equal(T.applyWatcherLimits(latest,1),true);
  const retainedChats=initial.providers[0].chats;
  latest.providers.Codex.lastStatus.five_hour_percent=99;latest.providers.Codex.lastStatus.weekly_percent=98;

  const oldStats=emptySnapshot(t10,cacheMeta({builtAt:t10}));oldStats.providers[0].contextPercent=71;oldStats.providers[0].chats=retainedChats;
  const oldGeneratedAt=oldStats.generatedAt,oldCache=oldStats.snapshotCache;
  T.render(oldStats);
  assert.equal(oldStats.providers[0].fiveHour.percent,40,"a delayed stats response cannot replace newer accepted watcher values");
  assert.equal(oldStats.providers[0].weekly.percent,50);
  assert.equal(oldStats.providers[0].limitsUpdatedAt,t20);
  assert.equal(oldStats.providers[0].contextPercent,71,"reconciliation does not copy non-quota provider data");
  assert.equal(oldStats.providers[0].chats,retainedChats,"reconciliation leaves chats untouched");
  assert.equal(oldStats.generatedAt,oldGeneratedAt,"reconciliation leaves snapshot generation metadata untouched");
  assert.equal(oldStats.snapshotCache,oldCache,"reconciliation leaves cache metadata untouched");
  assert.equal(T.applyWatcherLimits(watcher(iso(baseNow-9000),88),2),false,"an old watcher sample cannot regress retained quota data");

  const equalStats=emptySnapshot(t20);equalStats.providers[0].fiveHour={percent:77,resetAt:2_000_000_000};
  T.render(equalStats);
  assert.equal(equalStats.providers[0].fiveHour.percent,40,"equal timestamps keep the current canonical value");
  const newerStats=emptySnapshot(t30);newerStats.providers[0].fiveHour={percent:30,resetAt:2_000_000_001};
  T.render(newerStats);
  assert.equal(newerStats.providers[0].fiveHour.percent,30,"strictly newer stats beat retained watcher data");
  const stillOlder=emptySnapshot(t10);T.render(stillOlder);
  assert.equal(stillOlder.providers[0].fiveHour.percent,30,"an older later stats response cannot regress the newest canonical value");

  const explicitNull=emptySnapshot(iso(baseNow-5000));explicitNull.providers[0].fiveHour=null;explicitNull.providers[0].weekly=null;
  T.render(explicitNull);
  assert.equal(explicitNull.providers[0].fiveHour,null,"explicit null quota facts remain null when newer");
  assert.equal(explicitNull.providers[0].weekly,null);
  const missingStamp=emptySnapshot(null);missingStamp.providers[0].fiveHour={percent:88};
  T.render(missingStamp);
  assert.equal(missingStamp.providers[0].fiveHour,null,"undated stats cannot outrank valid dated nulls");
  const invalidFuture=emptySnapshot(iso(baseNow+1000));invalidFuture.providers[0].fiveHour={percent:99};
  T.render(invalidFuture);
  assert.equal(invalidFuture.providers[0].fiveHour,null,"future timestamps cannot outrank valid dated quota evidence");
});

test("quota windows reconcile independently and quota bootstrap updates an existing canonical snapshot", async () => {
  const t10=iso(baseNow-10000),t15=iso(baseNow-5000),t20=iso(baseNow-1000);
  const makeSample=(checkedAt,fields)=>({observed_at:Date.parse(checkedAt)/1000,checkedAt,...fields});
  let payload;
  const {T}=harness(async url=>url==="/api/quota"?{ok:true,json:async()=>payload}:Promise.reject(new Error("unexpected request")));

  const first=emptySnapshot(t10);T.render(first);const readyAt=T.state.lastOkAt,refreshedAt=T.state.refreshedAt,painted=T.state.runwaySnapshotPainted;
  payload=quotaPayload(makeSample(t20,{five_hour_percent:40}));await T.loadQuota();
  assert.equal(T.state.snapshot,first,"quota bootstrap updates the current canonical object rather than replacing it");
  assert.equal(first.providers[0].fiveHour.percent,40);
  assert.equal(first.providers[0].weekly.percent,30);
  assert.equal(first.providers[0]._quotaWindowObservedAt.weekly,t10,"a five-hour update cannot relabel the weekly observation");
  assert.equal(T.state.lastOkAt,readyAt);assert.equal(T.state.refreshedAt,refreshedAt);assert.equal(T.state.runwaySnapshotPainted,painted);

  const laterStats=emptySnapshot(t15);laterStats.providers[0].fiveHour={percent:55,resetAt:1_800_000_000};laterStats.providers[0].weekly={percent:65,resetAt:1_900_000_000};
  T.render(laterStats);
  assert.equal(laterStats.providers[0].fiveHour.percent,40,"newer quota survives an older five-hour stats window");
  assert.equal(laterStats.providers[0].weekly.percent,65,"independently newer weekly stats are accepted");
  assert.equal(laterStats.providers[0]._quotaWindowObservedAt.fiveHour??laterStats.providers[0].limitsUpdatedAt,t20);
  assert.equal(laterStats.providers[0]._quotaWindowObservedAt.weekly,t15);

  let reversePayload=quotaPayload(makeSample(t20,{weekly_percent:75,weekly_reset_at:1_900_000_700}));
  const reverseEnv=harness(async url=>url==="/api/quota"?{ok:true,json:async()=>reversePayload}:Promise.reject(new Error("unexpected request"))),R=reverseEnv.T;
  const reverse=emptySnapshot(t10);R.render(reverse);await R.loadQuota();
  const reverseStats=emptySnapshot(t15);reverseStats.providers[0].fiveHour={percent:52,resetAt:1_800_000_700};reverseStats.providers[0].weekly={percent:62,resetAt:1_900_000_800};
  R.render(reverseStats);
  assert.equal(reverseStats.providers[0].fiveHour.percent,52,"weekly-only evidence does not block newer five-hour stats");
  assert.equal(reverseStats.providers[0].weekly.percent,75,"newer weekly quota survives later older stats");
  assert.equal(reverseStats.providers[0]._quotaWindowObservedAt.weekly??reverseStats.providers[0].limitsUpdatedAt,t20);
  assert.equal(reverseStats.providers[0]._quotaWindowObservedAt.fiveHour,t15);

  let invalidationPayload=quotaPayload(makeSample(t20,{five_hour_percent:null}));
  const invalidationEnv=harness(async url=>url==="/api/quota"?{ok:true,json:async()=>invalidationPayload}:Promise.reject(new Error("unexpected request"))),I=invalidationEnv.T;
  const invalidation=emptySnapshot(t10);I.render(invalidation);await I.loadQuota();
  const invalidatedStats=emptySnapshot(t15);I.render(invalidatedStats);
  assert.equal(invalidatedStats.providers[0].fiveHour,null,"explicit dated null invalidates only its own window");
  assert.equal(invalidatedStats.providers[0].weekly.percent,30);
  assert.equal(invalidatedStats.providers[0]._quotaWindowObservedAt.weekly,t15);
});

test("missing timestamps use sequence only without any dated source; malformed and future timestamps are rejected", () => {
  const { T } = harness();
  const known = emptySnapshot(iso(baseNow - 5000)); T.state.snapshot = known;
  assert.equal(T.applyWatcherLimits(watcher(undefined, 99), 2), false);
  assert.equal(T.applyWatcherLimits(watcher("not-an-iso-time", 99), 3), false);
  assert.equal(T.applyWatcherLimits(watcher(iso(baseNow + 1000), 99), 4), false);
  assert.equal(known.providers[0].fiveHour.percent, 20);

  const unknown = emptySnapshot(null); T.state.snapshot = unknown;
  assert.equal(T.applyWatcherLimits(watcher(undefined, 44), 2), true);
  assert.equal(unknown.providers[0].fiveHour.percent, 44);
  assert.equal(unknown.providers[0].limitsUpdatedAt, null, "undated sequence fallback invents no observation time");
  assert.equal(T.applyWatcherLimits(watcher(undefined, 77), 1), false, "older fallback sequence cannot regress quota values");
});

test("snapshot cache metadata gates the full-data refresh clock without affecting fresh live evidence", () => {
  const { T, nodes } = harness();
  T.state.refreshedAt = baseNow - 60_000;
  const stale = cacheMeta({ ageSeconds: 3600, stale: true, building: true, lastFailureAt: iso(baseNow - 1000), lastFailureCategory: "timeout" });
  assert.equal(T.acceptSnapshotCache({ snapshotCache: stale }, baseNow).advanced, false);
  assert.equal(T.state.refreshedAt, baseNow - 60_000);
  T.state.refreshing = false; T.tick();
  assert.match(nodes.get("#sync-age").title, /Snapshot stale/);
  assert.match(nodes.get("#sync-age").title, /Snapshot building/);
  assert.match(nodes.get("#sync-age").title, /timeout/);

  assert.equal(T.acceptSnapshotCache({ snapshotCache: cacheMeta({ builtAt: iso(baseNow - 1000) }) }, baseNow).advanced, true);
  assert.equal(T.state.refreshedAt, baseNow - 1000);
  assert.equal(T.acceptSnapshotCache(emptySnapshot(), baseNow).kind, "legacy");
  assert.equal(T.state.refreshedAt, baseNow, "metadata-less older backends use the successful response time");
  T.state.refreshedAt = baseNow - 60_000;
  assert.equal(T.acceptSnapshotCache({ snapshotCache: cacheMeta({ builtAt: iso(baseNow + 1000) }) }, baseNow).kind, "invalid");
  assert.equal(T.state.refreshedAt, baseNow - 60_000);
  T.state.liveSampleAccepted = true; T.state.liveAcceptedGeneratedAt = baseNow;
  assert.equal(T.runwayCurrentEvidenceFresh(baseNow), true, "old full-data time does not suppress current live work");
});

test("refresh and live polling coalesce quota demands into one in-flight watcher loader", async () => {
  const gates = [], watcherBodies = [watcher(iso(baseNow - 1000), 41), watcher(iso(baseNow - 900), 42)];
  let watcherCalls = 0, watcherActive = 0, maxWatcherActive = 0, statsCalls = 0;
  const { T } = harness(async url => {
    if (String(url) === "/api/live") return { ok: true, json: async () => ({ generatedAt: iso(baseNow), chats: [] }) };
    if (String(url).startsWith("/api/stats")) { statsCalls++; return { ok: true, json: async () => emptySnapshot(null, cacheMeta({ builtAt: iso(baseNow - 2000) })) }; }
    if (String(url) === "/api/limit-reset") {
      const index = watcherCalls++, gate = gates[index]; watcherActive++; maxWatcherActive = Math.max(maxWatcherActive, watcherActive);
      try { const body = await gate.promise; return { ok: true, json: async () => body }; }
      finally { watcherActive--; }
    }
    throw new Error("unexpected fetch: " + url);
  });
  T.state.snapshot = emptySnapshot(null); gates.push(deferred(), deferred());
  T.tick(); assert.equal(watcherCalls, 0, "a simple tick never polls the watcher");
  const live = T.pollLive();
  await until(() => watcherCalls === 1);
  const full = T.refresh();
  await until(() => statsCalls === 1);
  await Promise.resolve();
  assert.equal(watcherCalls, 1, "a concurrent refresh queues demand instead of opening a duplicate request");
  assert.equal(maxWatcherActive, 1);
  gates[0].resolve(watcherBodies[0]);
  await until(() => watcherCalls === 2);
  assert.equal(maxWatcherActive, 1, "the latest queued demand starts only after the prior request settles");
  gates[1].resolve(watcherBodies[1]);
  await Promise.all([live, full, T.watcherLoader.promise]);
  assert.equal(watcherCalls, 2, "multiple in-flight demands coalesce into one latest follow-up");
  assert.equal(T.state.watcher.providers.Codex.lastStatus.five_hour_percent, 42);
  assert.equal(T.state.snapshot.providers[0].fiveHour.percent, 42);
});

test("watcher fetch failure retains last-known state and a later success clears its separate error", async () => {
  let fail = true;
  const retained = { providers: { Codex: { lastStatus: { five_hour_percent: 70 } } } };
  const { T } = harness(async () => fail ? Promise.reject(new Error("offline")) : { ok: true, json: async () => watcher(iso(baseNow), 50) });
  T.state.snapshot = emptySnapshot(null); T.state.watcher = retained;
  assert.equal(await T.loadWatcher(), false);
  assert.equal(T.state.watcher, retained);
  assert.equal(T.state.watcherFetchError.kind, "unavailable");
  assert.equal(typeof T.state.watcherFetchError.at, "number");
  fail = false;
  assert.equal(await T.loadWatcher(), true);
  assert.equal(T.state.watcherFetchError, null);
});

test("fast quota boot paints from bootstrap while full stats remain pending, then merges before roster readiness", async () => {
  const quotaGate=deferred(),statsGate=deferred(),calls=[];
  const {T,nodes}=harness(async(url,options={})=>{
    calls.push([String(url),options.method||"GET"]);
    if(url==="/api/quota")return quotaGate.promise;
    if(String(url).startsWith("/api/stats"))return statsGate.promise;
    if(url==="/api/limit-reset")return {ok:true,json:async()=>({providers:{}})};
    throw new Error("unexpected request: "+url);
  });
  T.renderLoading();
  const quotaPromise=T.loadQuota(),samePromise=T.loadQuota();
  const statsPromise=T.refresh();
  const gatedLive=T.pollLive();
  await until(()=>calls.some(([url])=>url==="/api/quota")&&calls.some(([url])=>url.startsWith("/api/stats")));
  assert.equal(calls.filter(([url])=>url==="/api/quota").length,1,"overlapping startup/2s quota demands share one request");
  assert.equal(quotaPromise,samePromise);
  assert.equal(T.state.snapshot,null);
  assert.equal(T.state.runwaySnapshotPainted,false);
  assert.equal(nodes.get("#chat-list").innerHTML,"");
  const t20=iso(baseNow-2000),sample={observed_at:(baseNow-2000)/1000,checkedAt:t20,five_hour_percent:40,weekly_percent:55,reset_at:1_800_000_200,weekly_reset_at:1_900_000_200};
  quotaGate.resolve({ok:true,json:async()=>quotaPayload(sample)});
  assert.equal(await quotaPromise,true);
  await gatedLive;
  assert.equal(T.state.snapshot,null,"quota data never fabricates a ready snapshot or roster");
  assert.equal(T.state.runwaySnapshotPainted,false);
  assert.equal(nodes.get("#r0-fiveHour-left").innerHTML,'<span class="rr-value"><b>40%</b><i>used</i></span>');
  assert.equal(nodes.get("#r0-weekly-left").innerHTML,'<span class="rr-value"><b>55%</b><i>used</i></span>');
  assert.equal(T.state.lastOkAt,null,"quota success is not a full-stat success signal");
  assert.equal(T.state.watcher,null,"quota data does not populate greeter/action status");
  assert.ok(calls.every(([,method])=>method==="GET"),"startup quota polling sends no action or message writes");

  const oldStats=emptySnapshot(iso(baseNow-5000));oldStats.providers[0].fiveHour={percent:80,resetAt:1_700_000_000};oldStats.providers[0].weekly={percent:90,resetAt:1_700_000_100};
  statsGate.resolve({ok:true,json:async()=>oldStats});
  await statsPromise;
  assert.equal(T.state.runwaySnapshotPainted,true,"only the full snapshot marks the roster ready");
  assert.equal(T.state.snapshot.providers[0].fiveHour.percent,40,"early quota survives an older delayed stats response");
  assert.equal(T.state.snapshot.providers[0].weekly.percent,55);
  assert.ok(T.state.lastOkAt!==null,"full stats success owns the full-data success clock");
});

test("cold quota refresh reports readiness separately and the next existing poll reveals measured values", async () => {
  let quotaCalls=0,active=0,maxActive=0,liveCalls=0;
  const sample={observed_at:(baseNow-1000)/1000,checkedAt:iso(baseNow-1000),five_hour_percent:25,weekly_percent:70,reset_at:1_800_000_300,weekly_reset_at:1_900_000_300};
  const {T,nodes}=harness(async(url)=>{
    if(url==="/api/quota"){
      quotaCalls++;active++;maxActive=Math.max(maxActive,active);
      try{return {ok:true,json:async()=>quotaCalls===1?quotaPayload(null,{refreshing:true}):quotaPayload(sample)};}
      finally{active--;}
    }
    if(url==="/api/live"){liveCalls++;return {ok:true,json:async()=>({generatedAt:iso(baseNow),chats:[]})};}
    throw new Error("unexpected request: "+url);
  });
  T.renderLoading();T.state.loading=true;
  await T.pollLive();
  await T.quotaLoader.promise;
  assert.equal(quotaCalls,1,"quota is invoked before the no-snapshot/loading live guard");
  assert.equal(liveCalls,0,"the quota poll does not bypass the existing full-stats gate");
  assert.equal(T.state.snapshot,null);assert.equal(T.state.runwaySnapshotPainted,false);
  assert.equal(T.quotaBootstrap.metadata.ready,false);assert.equal(T.quotaBootstrap.metadata.refreshing,true);
  T.state.loading=false;
  await T.pollLive();
  await until(()=>T.quotaBootstrap.metadata?.ready===true);
  assert.equal(quotaCalls,2,"the existing live cadence retries a cold cache without adding a timer");
  assert.equal(maxActive,1,"quota transport never overlaps");
  assert.equal(T.quotaBootstrap.provider.fiveHour.percent,25);
  assert.equal(T.quotaBootstrap.provider.weekly.percent,70);
  assert.equal(T.state.snapshot,null);assert.equal(T.state.runwaySnapshotPainted,false);
  assert.match(nodes.get("#r0-fiveHour-left").innerHTML,/25%/);
  assert.equal(T.state.watcher,null);
});

test("quota stale metadata and one-window partials preserve known facts; invalid samples cannot replace them", async () => {
  let payload=null,now=baseNow;
  const sample={observed_at:(baseNow-2000)/1000,checkedAt:iso(baseNow-2000),five_hour_percent:40,weekly_percent:60,reset_at:1_800_000_400,weekly_reset_at:1_900_000_400};
  const {T,setNow}=harness(async url=>url==="/api/quota"?{ok:true,json:async()=>payload}:Promise.reject(new Error("unexpected request")));
  setNow(now);T.renderLoading();payload=quotaPayload(sample,{fresh:false,ageSeconds:8,refreshing:true,errorCategory:"timeout"});
  await T.loadQuota();
  assert.equal(T.quotaBootstrap.metadata.fresh,false,"cache age is separate from the immutable sample time");
  assert.equal(T.quotaBootstrap.provider.fiveHour.percent,40);
  const newer={observed_at:(baseNow-1000)/1000,checkedAt:iso(baseNow-1000),weekly_percent:72,weekly_reset_at:1_900_000_500};
  payload=quotaPayload(newer);now=baseNow+5000;setNow(now);T.quotaLoader.retryAfter=0;
  await T.loadQuota();
  assert.equal(T.quotaBootstrap.provider.fiveHour.percent,40,"a missing window does not clear its retained value");
  assert.equal(T.quotaBootstrap.provider.fiveHour.resetAt,1_800_000_400,"a missing window does not clear its reset");
  assert.equal(T.quotaBootstrap.provider.weekly.percent,72);
  const priorDate=T.quotaBootstrap.provider.limitsUpdatedAt,priorPercent=T.quotaBootstrap.provider.weekly.percent;
  payload=quotaPayload(sample);T.quotaLoader.retryAfter=0;await T.loadQuota();
  assert.equal(T.quotaBootstrap.provider.limitsUpdatedAt,priorDate,"an older valid response cannot regress the accepted sample");
  assert.equal(T.quotaBootstrap.provider.weekly.percent,priorPercent);
  const invalidResponses=[current=>({...quotaPayload({...sample,checkedAt:undefined}),providers:{Codex:{lastStatus:{...sample,checkedAt:undefined}}}}),
    current=>quotaPayload({...sample,checkedAt:iso(current+1000),observed_at:(current+1000)/1000}),
    current=>quotaPayload({...sample,checkedAt:"not-a-timestamp"})];
  for(const makeInvalid of invalidResponses){
    now+=5000;setNow(now);payload=makeInvalid(now);T.quotaLoader.retryAfter=0;await T.loadQuota();
    assert.equal(T.quotaBootstrap.provider.limitsUpdatedAt,priorDate);
    assert.equal(T.quotaBootstrap.provider.weekly.percent,priorPercent);
  }
  assert.equal(T.state.snapshot,null,"quota-only refreshes never synthesize full stats");
});

test("an old server quota route degrades once to full-stats fallback", async () => {
  let quotaCalls=0;
  const {T}=harness(async url=>{if(url==="/api/quota"){quotaCalls++;return {ok:false,status:404};}throw new Error("unexpected request");});
  assert.equal(await T.loadQuota(),false);
  assert.equal(T.quotaLoader.unsupported,true);
  assert.equal(await T.loadQuota(),false);
  assert.equal(quotaCalls,1,"404 does not create a 2s retry storm");
  const stats=emptySnapshot(iso(baseNow-1000));
  T.render(stats);
  assert.equal(T.state.snapshot.providers[0].fiveHour.percent,20,"the existing full-stats quota path remains available");
});

function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; }
async function until(condition) { for (let i = 0; i < 100; i++) { if (condition()) return; await new Promise(resolve => setImmediate(resolve)); } throw new Error("condition timed out"); }
