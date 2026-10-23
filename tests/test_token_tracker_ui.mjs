import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const app=fs.readFileSync(new URL("../token_tracker/static/app.js",import.meta.url),"utf8");
const html=fs.readFileSync(new URL("../token_tracker/static/index.html",import.meta.url),"utf8");
const css=fs.readFileSync(new URL("../token_tracker/static/styles.css",import.meta.url),"utf8");

// ---------- static shell ----------
assert.match(html,/href="fonts\/localfonts\.css"/); // shipped local fonts avoid an external font request
assert.doesNotMatch(html,/fonts\.googleapis\.com|fonts\.gstatic\.com/);
assert.match(html,/class="titlebar pywebview-drag-region"/);
assert.match(html,/id="window-fit" aria-label="Fit widget to screen"/);
assert.match(app,/\$\("#window-fit"\)\?\.addEventListener\("click",\(\)=>window\.pywebview\?\.api\?\.fit\?\.\(\)\)/);
assert.match(html,/id="strip"[\s\S]*id="resets"[\s\S]*id="analytics"[\s\S]*id="chat-list"[\s\S]*id="totals"/);
assert.match(html,/<section id="analytics"[^>]*\bhidden\b/);
assert.match(html,/id="window-seg"[\s\S]*data-days="1"[\s\S]*data-days="2"[\s\S]*data-days="5"[\s\S]*data-days="7"/);
assert.equal((html.match(/class="resize-edge/g)||[]).length,3);
assert.doesNotMatch(html,/<select|⌕|⌄|↳|⌖/);
for(const token of ["--bg:#0A0A0A","--surface:#111111","--line:#242424","--text-1:#EDEDED","--text-2:#A1A1A1","--text-3:#878787","--live:#3FCF8E","--attn:#F5A524","--err:#FF6166","--stat-tok:#6FA8F0","--stat-tool:#B69CF6","--stat-file:#5CC8C0"])
  assert.ok(css.includes(token),token);
assert.match(css,/prefers-reduced-motion:reduce/);
assert.match(css,/@media \(min-width:680px\)/);
assert.match(css,/\.chat-list\{[^}]*overflow-y:auto/);
assert.match(css,/\.strip\{[^}]*max-height:40vh[^}]*overflow-y:auto/); // long attention prompts cannot consume the entire compact widget
assert.match(css,/\.q-opt\{[^}]*min-width:0[^}]*white-space:normal[^}]*overflow-wrap:anywhere/); // long answer choices wrap inside narrow widgets
assert.match(css,/\.si-ask \.si-qt\{[^}]*white-space:normal[^}]*overflow-wrap:anywhere/);
assert.match(css,/\.analytics details/);
assert.match(css,/\.an-advice/);
assert.doesNotMatch(css,/linear-gradient|radial-gradient|text-shadow/);
// No declared font size under 11px anywhere.
for(const m of css.matchAll(/font(?:-size)?:[^;}]*?(\d+(?:\.\d+)?)px/g)) assert.ok(Number(m[1])>=11,"font size "+m[0]);

// ---------- VM with DOM stubs ----------
const nodes=new Map();
function node(selector){
  if(!nodes.has(selector))nodes.set(selector,{innerHTML:"",textContent:"",value:"",hidden:false,title:"",className:"",dataset:{},attributes:{},style:{setProperty(k,v){this[k]=v;}},
    contains(){return false},querySelectorAll(){return []},setAttribute(name,value){this.attributes[name]=String(value);},getAttribute(name){return this.attributes[name]??null;},removeAttribute(name){delete this.attributes[name];},classList:{toggle(){},add(){},remove(){}}});
  return nodes.get(selector);
}
const saved=new Map();
const fixedNow=new Date(2026,8,23,2,30,0).getTime();   // Wed 23 Sep 2026, 2:30 am local
class TestDate extends Date { constructor(...args){super(...(args.length?args:[fixedNow]));} static now(){return fixedNow;} }
const context={window:{__TOKEN_TRACKER_TEST__:true},document:{querySelector:node,querySelectorAll(){return []}},localStorage:{getItem(key){return saved.get(key)??null},setItem(key,value){saved.set(key,String(value))}},Intl,Date:TestDate,Math,Number,JSON,setTimeout:fn=>fn()};
context.window.window=context.window;
vm.createContext(context);
vm.runInContext(app,context);
const T=context.window.__tokenTrackerTest;
const ago=m=>new Date(fixedNow-m*60000).toISOString();
const warmModel={warmSeconds:1800,coldSeconds:5400,samples:148,learned:true};
function snap(chats,extra={}){return {generatedAt:new Date(fixedNow).toISOString(),enabledProviders:["Codex"],providers:[{name:"Codex",connected:true,cacheWarmth:warmModel,activity:[],chats,...extra}]};}
function reset(){T.state.open.clear();T.state.pins.clear();T.state.dismissed.clear();T.state.query="";T.state.windowDays=1;T.state.watcher=null;T.state.failSince=null;T.state.lastOkAt=fixedNow;T.state.agentLive.clear();T.state.finishedAt.clear();}

// A second VM runs the actual desktop Runway branch against a clock we can advance.
let runwayNow=fixedNow,runwayTimerId=0;
class RunwayDate extends Date { constructor(...args){super(...(args.length?args:[runwayNow]));} static now(){return runwayNow;} }
const runwayNodes=new Map();
function runwayNode(selector){
  if(!runwayNodes.has(selector))runwayNodes.set(selector,{innerHTML:"",textContent:"",value:"",hidden:false,title:"",className:"",dataset:{},style:{setProperty(k,v){this[k]=v;}},
    attributes:{},contains(){return false},querySelectorAll(){return []},setAttribute(name,value){this.attributes[name]=String(value);},getAttribute(name){return this.attributes[name]??null;},removeAttribute(name){delete this.attributes[name];},classList:{toggle(){},add(){},remove(){},contains(){return false}}});
  return runwayNodes.get(selector);
}
const runwaySaved=new Map();
const runwayContext={window:{__TOKEN_TRACKER_TEST__:true,__TOKEN_TRACKER_RUNWAY__:true},document:{hidden:false,activeElement:null,querySelector:runwayNode,querySelectorAll(){return [];},body:{classList:{toggle(){}}}},
  localStorage:{getItem(key){return runwaySaved.get(key)??null;},setItem(key,value){runwaySaved.set(key,String(value));}},Intl,Date:RunwayDate,Math,Number,JSON,
  setTimeout(){return ++runwayTimerId;},clearTimeout(){},AbortSignal:{timeout(ms){return {ms};}}};
runwayContext.window.window=runwayContext.window;
vm.createContext(runwayContext);
vm.runInContext(app,runwayContext);
const R=runwayContext.window.__tokenTrackerTest;
const runwayIso=epoch=>new Date(epoch).toISOString();
function runwayChats(at,options={}){
  const messageAt=options.messageAt??fixedNow-1000,started=options.started??fixedNow-60000;
  return [
    {id:"run-root",title:"Current parent run",model:"gpt-5.6-sol",status:"running",statusSource:"transcript",statusObservedAt:runwayIso(at),updatedAt:runwayIso(at),activityAt:runwayIso(at),
      executionStatus:"running",executionSource:"transcript",executionObservedAt:runwayIso(at),executionSince:runwayIso(started),executionActivity:{kind:"reading",files:["D:\\work\\sample-app\\live_telemetry.py"],observedAt:runwayIso(at)},
      workspace:"D:\\work\\sample-app",activity:{kind:"reading",detail:"Read live telemetry"},latestRequest:"Verify the current run",latestRequestAt:runwayIso(at),latestUpdate:"Current parent checkpoint",latestUpdateAt:runwayIso(at),
      runStartedAt:runwayIso(started),activeRunStartedAt:runwayIso(started),stateSince:runwayIso(started),contextModel:"gpt-5.6-sol",contextTokens:80000,contextWindow:200000,contextPercent:40,contextObservedAt:runwayIso(at),usageUpdatedAt:runwayIso(at),toolCalls:4,filesEdited:2},
    {id:"run-worker",parentId:"run-root",agentName:"/root/check_live",title:"Check live telemetry",model:"gpt-5.6-luna",status:options.childStatus??"needs-input",statusSource:"transcript",statusObservedAt:runwayIso(at),updatedAt:runwayIso(at),activityAt:runwayIso(at),
      pendingQuestion:options.pendingQuestion===false?null:"May I inspect the updated cache?",pendingOptions:["Yes","No"],pendingQuestionId:"question-1",questionId:"question-1",pendingQuestionAt:runwayIso(at),
      runStartedAt:runwayIso(started),activeRunStartedAt:runwayIso(started),stateSince:runwayIso(started),latestMessageAt:runwayIso(messageAt),latestMessageDirection:"agent-to-parent",contextModel:"gpt-5.6-luna",contextTokens:24000,contextWindow:128000,contextPercent:19,contextObservedAt:runwayIso(at),usageUpdatedAt:runwayIso(at),toolCalls:3,filesEdited:1}
  ];
}
function runwaySnapshot(at,chats=runwayChats(at)){return {generatedAt:runwayIso(at),structureStamp:"stable",enabledProviders:["Codex"],providers:[{name:"Codex",connected:true,activity:[],chats}]};}
function runwayLive(at,chats=runwayChats(at)){return {generatedAt:runwayIso(at),structureStamp:"stable",chats:chats.map(chat=>({...chat,key:"Codex:"+chat.id,provider:"Codex"}))};}
async function acceptRunwayLive(sample){
  runwayContext.fetch=async url=>url==="/api/live"?{ok:true,json:async()=>sample}:{ok:true,json:async()=>({providers:[]})};
  await R.pollLive();
}

// A restored stats snapshot cannot establish a quota observation.
{
  const restored=runwaySnapshot(runwayNow);
  restored.snapshotCache={generation:1,builtAt:runwayIso(runwayNow),ageSeconds:0,building:true,stale:true,lastFailureAt:null,lastFailureCategory:null};
  restored.providers[0].fiveHour={percent:null,resetAt:runwayNow/1000+3600};
  restored.providers[0].weekly={percent:null,resetAt:runwayNow/1000+86400};
  restored.providers[0].limitsUpdatedAt=null;
  R.render(restored);
  const cachedProvider=R.state.snapshot.providers[0];
  assert.equal(R.resetView(cachedProvider,"fiveHour").measured,false);
  assert.equal(R.resetView(cachedProvider,"weekly").measured,false);
  assert.equal(R.resetView(cachedProvider,"fiveHour").lastKnown,false,"restored snapshots with null quota are not treated as last known values");
  assert.equal(R.resetView(cachedProvider,"weekly").lastKnown,false,"restored snapshots with null quota are not treated as last known values");
  assert.equal(runwayNode("#r0-fiveHour-meter").getAttribute("aria-valuenow"),null);
  assert.equal(runwayNode("#r0-weekly-meter").getAttribute("aria-valuenow"),null);
  assert.equal(runwayNode("#r0-fiveHour-meter").getAttribute("aria-hidden"),"true");

  const checkedAt=runwayIso(runwayNow);
  assert.equal(R.applyQuotaSamples({Codex:{lastStatus:{checkedAt,five_hour_percent:23,weekly_percent:41}}},1,R.limitProvidersFor(R.state.snapshot)),true);
  R.renderResets(R.limitProvidersFor(R.state.snapshot));
  assert.equal(runwayNode("#r0-fiveHour-meter").getAttribute("aria-valuenow"),"23");
  assert.equal(runwayNode("#r0-weekly-meter").getAttribute("aria-valuenow"),"41");
}

// ---------- current-evidence expiry, monotonic recovery, and active reveal ----------
{
  const initial=runwaySnapshot(runwayNow);
  R.render(initial);
  assert.equal(R.runway,true);
  assert.equal(R.runwayCurrentEvidenceFresh(),true);
  assert.deepEqual(JSON.parse(JSON.stringify(R.runwayCounts(R.state.forest))),{roots:1,total:2,waiting:1});
  const coldExpiredAt=runwayNow+R.RUNWAY_EVIDENCE_MAX_AGE_MS+1;
  runwayNow=coldExpiredAt;
  assert.equal(R.runwayCurrentEvidenceFresh(),false,"cold full stats anchor expires after eight seconds");
  assert.deepEqual(JSON.parse(JSON.stringify(R.runwayCounts(R.state.forest))),{roots:0,total:0,waiting:0});

  // A later full stats response can establish a new anchor before any live sample is accepted.
  R.render(runwaySnapshot(runwayNow));
  assert.equal(R.runwayCurrentEvidenceFresh(),true);
  const acceptedAt=runwayNow+1000;
  runwayNow=acceptedAt;
  await acceptRunwayLive(runwayLive(acceptedAt));
  assert.equal(R.state.liveAcceptedGeneratedAt,acceptedAt);
  assert.equal(R.runwayCurrentEvidenceFresh(),true);

  // Once live is accepted, a later stats refresh cannot extend its independent expiry.
  runwayNow=acceptedAt+R.RUNWAY_EVIDENCE_MAX_AGE_MS+1;
  R.render(runwaySnapshot(runwayNow));
  assert.equal(R.runwayCurrentEvidenceFresh(),false,"stats refresh cannot renew an accepted live timestamp");
  assert.deepEqual(JSON.parse(JSON.stringify(R.runwayCounts(R.state.forest))),{roots:0,total:0,waiting:0});
  assert.equal(R.notch.active,0);
  assert.equal(R.notch.autoTimer,null);
  assert.equal(R.notch.autoExpanded,false);
  const staleRoot=R.state.forest.roots[0],staleChild=R.state.forest.rows.find(chat=>chat.id==="run-worker");
  assert.equal(R.runwayVisibleActive(staleRoot),false);
  assert.equal(R.runwayQuestionPopover(staleChild),"");

  const savedAnchor=R.state.liveAcceptedGeneratedAt;
  await acceptRunwayLive(runwayLive(runwayNow+1)); // Future timestamp is rejected.
  assert.equal(R.state.liveAcceptedGeneratedAt,savedAnchor);
  await acceptRunwayLive(runwayLive(savedAnchor-1)); // Regressing timestamp is ignored.
  assert.equal(R.state.liveAcceptedGeneratedAt,savedAnchor);
  await acceptRunwayLive(runwayLive(savedAnchor)); // Equality is accepted without extending freshness.
  assert.equal(R.state.liveAcceptedGeneratedAt,savedAnchor);
  assert.equal(R.runwayCurrentEvidenceFresh(),false);

  // A newer valid sample restores current root and child telemetry. A recent message on
  // the already-known run is baselined, so it does not create a message-only reveal.
  runwayNow+=1;
  const recoveryChats=runwayChats(runwayNow,{messageAt:runwayNow-500});
  await acceptRunwayLive(runwayLive(runwayNow,recoveryChats));
  assert.equal(R.runwayCurrentEvidenceFresh(),true);
  assert.equal(R.state.liveRecoveryPending,false);
  assert.deepEqual(JSON.parse(JSON.stringify(R.runwayCounts(R.state.forest))),{roots:1,total:2,waiting:1});
  const root=R.state.forest.roots[0],child=R.state.forest.rows.find(chat=>chat.id==="run-worker");
  const rootMarkup=R.rowHTML(root),childMarkup=R.rowHTML(child,true);
  assert.match(rootMarkup,/runway-status-dot[\s\S]*aria-label="Running"/);
  // Rows show fixed activity labels from executionActivity; free-text detail is never rendered.
  assert.match(rootMarkup,/data-runway-slot="activity"[^>]*>Reading live_telemetry\.py</);
  assert.doesNotMatch(rootMarkup,/Read live telemetry/);
  assert.match(rootMarkup,/class="runway-model-timing"/);
  assert.notEqual(R.runwayStateElapsed(root),"—");
  assert.match(childMarkup,/runway-telemetry-state t-attn[^>]*aria-label="Waiting"/);
  assert.match(childMarkup,/data-action="runway-question" data-key="Codex:run-worker"/);
  assert.notEqual(R.runwayQuestionPopover(child),"");
  assert.equal(R.notch.active,1);
  assert.equal(R.notch.autoTimer,null,"message-only recovery must not auto-reveal the existing run");
  assert.equal(R.notch.autoExpanded,false);

  // A genuinely new active identity after recovery still reveals the live tree.
  runwayNow+=100;
  const newWorker={...runwayChats(runwayNow)[1],id:"new-worker",parentId:"run-root",agentName:"/root/new_worker",title:"New worker",status:"running",pendingQuestion:null,activeRunStartedAt:runwayIso(runwayNow),runStartedAt:runwayIso(runwayNow),activityAt:runwayIso(runwayNow),updatedAt:runwayIso(runwayNow)};
  R.state.structuralRefreshAt=runwayNow; // Keep the test on the UI reveal path, without starting stats refresh.
  await acceptRunwayLive(runwayLive(runwayNow,[...runwayChats(runwayNow),newWorker]));
  assert.ok(R.state.pendingOutside.has("Codex:new-worker"));
  assert.ok(R.state.snapshot.providers[0].chats.some(chat=>chat.id==="new-worker"&&chat.parentId==="run-root"),"live-only worker is upserted with its parent link");
  assert.deepEqual(JSON.parse(JSON.stringify(R.runwayCounts(R.state.forest))),{roots:1,total:3,waiting:1});
  assert.ok(R.notch.autoTimer!==null||R.notch.autoExpanded,"new active identities still reveal the tree");
}

// ---------- formatting ----------
assert.equal(T.fmt(null),"—");
assert.match(T.fmt(1_020_000),/1\.02M/);
assert.equal(T.normalizeStatus("active"),"running");
assert.equal(T.normalizeStatus("needs input"),"needs-input");
assert.equal(T.normalizeStatus("error"),"failed");
assert.equal(T.normalizeStatus("inactive"),"idle");
assert.equal(T.statusRank("needs-input"),0);
assert.equal(T.contextTone(74),"");
assert.equal(T.contextTone(75),"attn");
assert.equal(T.contextTone(90),"err");
assert.equal(T.countdown(3*3600+2*60+5),"3h 02m");
assert.equal(T.countdown(42*60+10),"42m");
assert.equal(T.countdown(9*60+41),"9:41");
assert.equal(T.countdown(5*86400+19*3600),"5d 19h");
assert.equal(T.countdown(2*86400+6*3600),"2d 06h");
assert.equal(T.runSpan(247),"4:07");
assert.equal(T.runSpan(12*60+30),"12m");
assert.equal(T.runSpan(3600+4*60),"1h 04m");
assert.equal(T.ageShort(fixedNow-20000),"now");
assert.equal(T.ageShort(fixedNow-3*60000),"3m");
assert.equal(T.ageShort(fixedNow-2*86400000),"2d");

// ---------- power-user analytics: local provenance and unavailable states ----------
{
  const summary=snap([
    {id:"big",title:"Long context chat",contextPercent:88,model:"gpt-5.6-sol"},
    {id:"small",title:"Other",contextPercent:43,model:"gpt-5.6-luna"}
  ],{
    tracked31Days:12_300_000,source:"Codex local session telemetry",
    activityNote:"Local session counters may omit early baselines.",
    cache:{percent:42,read:420_000,cacheSavings:3.25},
    modelUsage:[{model:"gpt-5.6-sol",tokens:8_000_000,observedRequests:31,subagentTokens:2_000_000,subagentRequests:7}],
    modelUsageNote:"Local token_count deltas over the last 31 days. Observed requests count nonzero counter updates, not guaranteed one-per-turn; subagent totals use session parent metadata."
  });
  let html=T.analyticsHTML(summary.providers[0]&&summary);
  assert.match(html,/Tracked tokens[\s\S]*12\.3M/);
  assert.match(html,/Usage now · 2 sampled[\s\S]*88%/);
  assert.match(html,/Long context chat is at 88%/);
  assert.match(html,/8M tok[\s\S]*31 requests observed · 25% subagent tokens · 23% subagent requests/);
  assert.match(html,/not guaranteed one-per-turn/);
  assert.match(html,/Source: Codex local session telemetry/);
  const cacheAdvice=T.analyticsHTML(snap([],{cache:{percent:42,read:420_000},cacheWarmth:{learned:false}}));
  assert.match(cacheAdvice,/Only 42% of recorded input was cache reads/);
  T.state.analyticsOpen=false;
  T.state.snapshot=summary;
  T.handleClick({target:{closest:()=>({dataset:{action:"analytics"}})}});
  assert.equal(T.state.analyticsOpen,true);assert.match(node("#analytics").innerHTML,/aria-expanded="true"[\s\S]*id="analytics-panel" class="an-panel"/);
  T.state.snapshot=null;
  const missing=T.analyticsHTML({enabledProviders:["Codex"],providers:[{name:"Codex",connected:true,chats:[]}]});
  assert.match(missing,/Model and subagent totals unavailable/);assert.match(missing,/unavailable/);
  const offline=T.analyticsHTML({enabledProviders:["Codex"],providers:[{name:"Codex",connected:false,chats:[]}]});
  assert.match(offline,/offline/);assert.match(offline,/Model and subagent totals unavailable/);
  T.state.analyticsOpen=false;
}

// ---------- reset block: day + date + time strings ----------
{
  const at=(d,h,m)=>new Date(2026,8,23+d,h,m).getTime()/1000;
  const today=T.resetParts({resetAt:at(0,5,12)});
  assert.equal(today.day,"Today");assert.equal(today.time,"5:12 am");assert.equal(today.countdown,"2h 42m");
  assert.equal(T.resetParts({resetAt:at(1,1,0)}).day,"Tomorrow");
  assert.equal(T.resetParts({resetAt:at(5,22,6)}).day,"Mon 28 Sep");
  // Weekly always carries the date, even tomorrow.
  const weekly=T.resetParts({resetAt:at(1,9,10)},null,"weekly");
  assert.equal(weekly.day,"Thu 24 Sep");assert.equal(weekly.time,"9:10 am");
  assert.equal(T.resetParts({resetAt:at(0,2,20)}).countdown,"due");
  assert.equal(T.resetParts("reset unavailable").time,"—:—");
  assert.equal(T.resetAt({reset:"resets 1h 00m"},new Date(fixedNow-60000).toISOString()),fixedNow+3540000);
  const low=T.resetView({fiveHour:{percent:94,resetAt:at(0,3,8)},limitsUpdatedAt:new Date(fixedNow).toISOString()},"fiveHour");
  assert.equal(low.usedText,"94% used");assert.equal(low.usedPercent,94);assert.equal(low.tone,"err");assert.equal(low.countdown,"38m");assert.equal(low.stale,"");
  const weeklyLow=T.resetView({weekly:{percent:88,resetAt:at(2,8,30)},limitsUpdatedAt:ago(120)},"weekly");
  assert.equal(weeklyLow.tone,"err");assert.equal(weeklyLow.usedText,"88% used");assert.equal(weeklyLow.stale,"");   // sample age is stated once, on the 5-hour row
  assert.equal(T.resetView({weekly:{percent:71},limitsUpdatedAt:new Date(fixedNow).toISOString()},"weekly").tone,"err");
  assert.equal(T.resetView({weekly:{percent:70},limitsUpdatedAt:new Date(fixedNow).toISOString()},"weekly").tone,"err");
  assert.equal(T.resetView({weekly:{percent:69},limitsUpdatedAt:new Date(fixedNow).toISOString()},"weekly").tone,"attn");
  assert.equal(T.resetView({weekly:{percent:50},limitsUpdatedAt:new Date(fixedNow).toISOString()},"weekly").tone,"attn");
  assert.equal(T.resetView({weekly:{percent:49},limitsUpdatedAt:new Date(fixedNow).toISOString()},"weekly").tone,"");
  assert.equal(T.resetView({fiveHour:{percent:12},limitsUpdatedAt:ago(120)},"fiveHour").stale,"sampled 2h ago");
  assert.equal(T.resetView({fiveHour:{percent:40},limitsUpdatedAt:ago(0)},"fiveHour").tone,"");
  T.renderResets([{name:"Codex",fiveHour:{percent:94,resetAt:at(0,3,8)},weekly:{percent:12,resetAt:at(5,22,6)},limitsUpdatedAt:new Date(fixedNow).toISOString()}]);
  assert.equal(node("#r0-fiveHour-time").textContent,"3:08 am");
  assert.equal(node("#r0-fiveHour-left").className,"rr-left t-err");
  assert.equal(node("#r0-weekly-day").textContent,"Mon 28 Sep");
  assert.equal(node("#r0-weekly-cd").textContent,"5d 19h");
  assert.equal(node("#r0-fiveHour-bar").style.width,"94%");
  assert.equal(node("#r0-weekly-bar").style.width,"12%");
  assert.equal(node("#r0-fiveHour-meter").getAttribute("aria-valuenow"),"94");
  assert.equal(node("#r0-fiveHour-meter").getAttribute("aria-valuetext"),"94% used");
  assert.match(node("#resets").innerHTML,/5-hour limit used/);
}

// ---------- forest, parent roll-up, agent tray ----------
reset();
const tree=snap([
  {id:"p",title:"Port reset watcher",model:"gpt-5.6-sol",status:"running",contextPercent:92,contextTokens:238000,contextWindow:258400,tokens:8.4e6,turns:18,toolCalls:88,filesEdited:11,runStartedAt:ago(12),updatedAt:ago(0),usageUpdatedAt:ago(0),workspace:"A:\\work"},
  {id:"a1",parentId:"p",agentName:"/root/windows_service",title:"windows_service",model:"gpt-5.6-terra",status:"needs-input",pendingQuestion:"Allow `sc.exe create`?",tokens:1.3e6,turns:9,toolCalls:34,filesEdited:5,runStartedAt:ago(12),updatedAt:ago(2)},
  {id:"a2",parentId:"p",agentName:"/root/schema_audit",title:"schema_audit",model:"gpt-5.6-luna",status:"running",tokens:910e3,turns:6,toolCalls:21,filesEdited:3,runStartedAt:new Date(fixedNow-247000).toISOString(),updatedAt:ago(0)},
  {id:"a3",parentId:"p",agentName:"/root/test_port",title:"test_port",model:"gpt-5.6-luna",status:"idle",stateDetail:"completed",tokens:240e3,turns:3,toolCalls:9,filesEdited:2,runStartedAt:ago(24),runEndedAt:ago(6),updatedAt:ago(6)},
  {id:"a4",parentId:"p",title:"Subagent",model:"Codex",status:"failed",stateDetail:"errored",tokens:null,turns:null,updatedAt:ago(4)},
]);
T.render(tree);
{
  const root=T.state.forest.roots[0];
  assert.equal(root.descendants.length,4);
  const roll=T.rollup(root);
  assert.equal(roll.key,"needs");assert.equal(roll.via,true);
  const row=T.rowHTML(root);
  assert.match(row,/r-state t-attn">Needs you<svg/);
  assert.match(row,/r-subs t-attn" title="1 running · 1 needs you · 1 failed · 1 done">/);
  assert.match(row,/ctx t-err[\s\S]*92%/);
  assert.match(row,/r-warm w-run[^>]*>[\s\S]*12m</);   // running chat: run timer in the warmth column
  // Auto-review child degrades gracefully without backend fields.
  const review=T.state.forest.rows.find(c=>c.id==="a4");
  assert.equal(T.agentLabel(review),"Auto-review");assert.equal(T.modelLabel(review),"review");
  assert.equal(T.agentLabel({agentRole:"auto-review",model:"gpt-5.6-luna"}),"Auto-review");
  assert.equal(T.agentLabel({agentName:"/root/test_port"}),"Test port");   // Codex-app display
  // Tray: only active or waiting agents; completed and failed history is excluded.
  assert.equal(T.trayState(root),"active");
  // Opening a chat (fresh updatedAt) without a new request must not wake a tray of finished agents.
  {
    const old=new Date(Date.now()-5*864e5).toISOString(),fresh=new Date().toISOString();
    const opened={latest:Date.now(),updatedAt:fresh,usageUpdatedAt:old,status:"idle",descendants:[{status:"idle",stateDetail:"completed",updatedAt:fresh,usageUpdatedAt:old,runEndedAt:old}]};
    assert.equal(T.trayState(opened),"resting");
  }
  const entries=T.trayEntries(root,T.parseQuery(""));
  assert.deepEqual(Array.from(entries,e=>e.key),["Codex:a1","Codex:a2","__total"]);
  assert.match(entries[0].html,/ag-name">Windows service<[\s\S]*5\.6-terra[\s\S]*Needs you/);
  assert.match(entries[0].html,/ag-q">Allow <code>sc\.exe create<\/code>\?/);
  assert.match(entries[0].cls,/asks/);
  assert.match(entries[1].html,/ss live[\s\S]*s-time live[\s\S]*4:07/);
  const total=entries.at(-1).html;
  assert.match(total,/Σ 2/);
  assert.match(total,/<b>2\.21M<\/b> <i>tok<\/i>/);
  assert.match(total,/<b>55<\/b> <i>tools<\/i>/);
  assert.match(total,/<b>8<\/b> <i>files<\/i>/);
  const list=node("#chat-list").innerHTML;
  assert.match(list,/class="tray active"/);
  assert.doesNotMatch(list,/class="l2/);   // layer 2 closed by default
  // Layer 2: blocker, own facts + strip, meta; the agent list stays in the tray.
  T.state.open.add("Codex:p");T.render(tree);
  const panel=T.panelHTML(root);
  assert.match(panel,/f-v t-err">238K \/ 258K/);
  assert.match(panel,/<b>88<\/b> <i>tools<\/i>[\s\S]*<b>11<\/b> <i>files<\/i>/);
  assert.match(panel,/codex resume p/);
  assert.doesNotMatch(panel,/Windows service/);
  assert.match(node("#chat-list").innerHTML,/class="l2 open"/);
  assert.match(T.panelHTML(T.state.forest.rows.find(c=>c.id==="a1")),/blk t-attn[\s\S]*Allow <code>sc\.exe create<\/code>/);
  // Running → finished removes the child from the live tray immediately.
  const a2=tree.providers[0].chats.find(c=>c.id==="a2");
  a2.status="idle";a2.runEndedAt=new Date(fixedNow).toISOString();T.render(tree);
  const fin=T.trayEntries(T.state.forest.roots[0],T.parseQuery("")).find(e=>e.key==="Codex:a2");
  assert.equal(fin,undefined);
  T.state.open.clear();
}
// A failure older than 30 minutes is history: no escalation, grey state.
{
  const old=T.buildForest(snap([{id:"x",title:"Old fail",status:"failed",stateDetail:"interrupted",updatedAt:ago(31)}]).providers).roots[0];
  assert.equal(T.rollup(old).key,"failed-old");
  assert.match(T.rowHTML(old),/r-state t-mute">Stopped</);
  const fresh=T.buildForest(snap([{id:"x",title:"New fail",status:"failed",stateDetail:"interrupted",updatedAt:ago(29)}]).providers).roots[0];
  assert.match(T.rowHTML(fresh),/g g-dot t-err/);
}
// Historical agents are hidden; active trays show live agents only and never leave an empty metrics shell.
{
  reset();
  const agents=Array.from({length:13},(_,i)=>({id:"e"+i,parentId:"r",agentName:"/root/a"+i,model:i===3?"Codex":"gpt-5.6-sol",title:i===3?"Subagent":"a"+i,status:"idle",stateDetail:"completed",tokens:100e3,toolCalls:10,filesEdited:1,runStartedAt:ago(200),runEndedAt:ago(190),updatedAt:ago(190)}));
  const resting=snap([{id:"r",title:"Redesign v2",status:"idle",stateDetail:"completed",updatedAt:ago(185)},...agents]);
  T.render(resting);
  const root=T.state.forest.roots[0];
  assert.equal(T.trayState(root),"resting");
  assert.equal(T.traySummaryHTML(root),"");
  assert.doesNotMatch(node("#chat-list").innerHTML,/class="tray/);
  // Waking one agent shows only it; the other 12 finished agents remain out of the tray.
  resting.providers[0].chats[1].status="running";resting.providers[0].chats[1].runEndedAt=null;resting.providers[0].chats[1].updatedAt=ago(0);
  T.render(resting);
  const woke=T.state.forest.roots[0];
  assert.equal(T.trayState(woke),"active");
  const entries=T.trayEntries(woke,T.parseQuery(""));
  assert.equal(entries.filter(e=>/^ag( |$)/.test(e.cls)&&!/dense|total/.test(e.cls)).length,1);
  assert.equal(entries.filter(e=>/dense/.test(e.cls)).length,0);
}

// ---------- warmth column ----------
{
  reset();
  const warm=snap([
    {id:"run",title:"Running",status:"running",usageUpdatedAt:ago(0),updatedAt:ago(0)},
    {id:"w",title:"Warm",status:"idle",usageUpdatedAt:ago(8),updatedAt:ago(8),contextTokens:60000},
    {id:"c",title:"Cooling",status:"idle",usageUpdatedAt:new Date(fixedNow-(1800-472)*1000).toISOString(),updatedAt:ago(22),contextTokens:124000},
    {id:"m",title:"Maybe",status:"idle",usageUpdatedAt:ago(45),updatedAt:ago(45)},
    {id:"x",title:"Cold",status:"idle",usageUpdatedAt:ago(200),updatedAt:ago(200)},
  ]);
  T.render(warm);
  const row=id=>T.rowHTML(T.state.forest.rows.find(c=>c.id===id));
  assert.match(row("run"),/<span class="r-warm"><\/span>/);   // no runStartedAt: nothing
  assert.match(row("w"),/r-warm w-warm[^>]*>[\s\S]*22m</);
  assert.match(row("w"),/Warm · reply by 2:52 am · going cold re-bills \+54K tokens/);
  assert.match(row("c"),/r-warm w-cooling[\s\S]*7:52</);
  assert.match(row("c"),/ring-fill/);
  assert.match(row("m"),/r-warm w-maybe[^>]*>cold\?</);
  assert.match(row("x"),/<span class="r-warm"><\/span>/);
  assert.equal(T.cacheWarmth({provider:"Claude",status:"idle",usageUpdatedAt:ago(1)}),null);
  assert.equal(node("#warm-rule").textContent,"warm ≤30m");
  assert.match(node("#warm-rule").title,/Reply within 30m to keep the cache · cold after 1h 30m · learned from 148 resumes/);
}

// ---------- cooling trigger threshold ----------
{
  reset();
  const cooling=tokens=>T.attentionItems(T.buildForest(snap([{id:"c",title:"Tune",status:"idle",usageUpdatedAt:new Date(fixedNow-(1800-472)*1000).toISOString(),updatedAt:ago(22),contextTokens:tokens}]).providers),[{name:"Codex",connected:true}]).filter(i=>i.kind==="cooling");
  T.state.snapshot=snap([]);
  assert.equal(cooling(49999).length,0);
  const [item]=cooling(50000);
  assert.equal(item.labelMono,"7:52");assert.equal(item.fact,"+45K if cold");   // 90% of the context re-billed
  T.state.dismissed.add(item.dismiss);
  assert.equal(cooling(50000).length,0);
  // Outside the last 10 minutes of the warm window: no nag.
  const early=T.attentionItems(T.buildForest(snap([{id:"c",title:"Tune",status:"idle",usageUpdatedAt:ago(15),updatedAt:ago(15),contextTokens:500000}]).providers),[{name:"Codex",connected:true}]);
  assert.equal(early.length,0);
}

// ---------- attention strip ordering ----------
{
  reset();
  const s=snap([
    {id:"ctx85",title:"Ctx 91",status:"idle",contextPercent:91,updatedAt:ago(30),usageUpdatedAt:ago(30),contextTokens:230000,contextWindow:258400},
    {id:"ctx95",title:"Ctx 95",status:"idle",contextPercent:95,updatedAt:ago(40),usageUpdatedAt:ago(40),contextTokens:245000,contextWindow:258400},
    {id:"f1",title:"Fail older",status:"failed",stateDetail:"errored",updatedAt:ago(20)},
    {id:"f2",title:"Fail newer",status:"failed",stateDetail:"interrupted",updatedAt:ago(5)},
    {id:"q1",title:"Ask newer",status:"needs-input",pendingQuestion:"B?",updatedAt:ago(1)},
    {id:"q2",title:"Ask older",status:"needs-input",pendingQuestion:"A?",updatedAt:ago(9)},
    {id:"par",title:"Parent",status:"running",updatedAt:ago(0)},
    {id:"sub",parentId:"par",agentName:"/agents/worker",title:"worker",status:"needs-input",pendingQuestion:"C?",updatedAt:ago(5)},
    {id:"cool",title:"Cooling",status:"idle",usageUpdatedAt:new Date(fixedNow-(1800-300)*1000).toISOString(),updatedAt:ago(25),contextTokens:90000},
    {id:"old",title:"Old ctx",status:"idle",contextPercent:99,updatedAt:ago(200),usageUpdatedAt:ago(200)},
  ]);
  T.state.watcher={enabled:true,running:true,providers:{Codex:{phase:"needs_login",needsLogin:true}}};
  T.state.failSince=fixedNow-15000;T.state.lastOkAt=fixedNow-180000;
  T.render(s);
  const items=T.attentionItems(T.state.forest,[{name:"Codex",connected:true}]);
  assert.deepEqual(Array.from(items,i=>i.key),["auth:Codex","offline","ask:Codex:q2","ask:Codex:sub","ask:Codex:q1","cooling:Codex:cool","failed:Codex:f2","failed:Codex:f1","context:Codex:ctx95","context:Codex:ctx85"]);
  assert.equal(items.find(i=>i.kind==="ask"&&i.sub).title,"Parent › Worker");
  assert.equal(items.find(i=>i.kind==="offline").fact,"data 3m old");
  assert.equal(items[0].copy,"codex login");
  const strip=node("#strip").innerHTML;
  assert.match(strip,/si t-attn k-auth"/);
  assert.match(strip,/k-ask over/);   // beyond three items
  assert.match(strip,/\+7 more/);
  T.state.stripOpen=true;T.render(s);
  assert.doesNotMatch(node("#strip").innerHTML,/ over"/);
  assert.match(node("#strip").innerHTML,/aria-expanded="true"/);
  T.state.stripOpen=false;
  // Watcher failure only when not an auth problem.
  T.state.failSince=null;
  T.state.watcher={enabled:true,running:true,providers:{Codex:{phase:"keepalive_scheduled",failedAttempts:2,lastError:"greeting not verified",nextRetryAt:new Date(2026,8,23,2,50).getTime()/1000}}};
  const w=T.attentionItems(T.buildForest(snap([]).providers),[{name:"Codex",connected:true}]);
  assert.deepEqual(Array.from(w,i=>[i.kind,i.label,i.fact]),[["watcher","Auto-greet failed","retry 2:50 am"]]);
  assert.match(T.watcherPopoverHTML("Codex"),/<dt>fails<\/dt><dd class="t-err">2/);
  T.state.watcher={enabled:true,running:true,providers:{Codex:{phase:"keepalive_scheduled",failedAttempts:0,unverifiedAttempts:2,lastError:"greeting was accepted but no new reset deadline was observed",nextGreetingAt:new Date(2026,8,23,2,50).getTime()/1000}}};
  const unconfirmed=T.attentionItems(T.buildForest(snap([]).providers),[{name:"Codex",connected:true}]);
  assert.equal(unconfirmed.length,0); // Verification details belong in the status popover, not the attention strip.
  assert.equal(T.watcherTone(T.state.watcher,"Codex").tone,"attn");
  T.state.watcher={enabled:true,running:false,providers:{Codex:{phase:"keepalive_scheduled",lastPollError:"disk full"}}};
  assert.equal(T.attentionItems(T.buildForest(snap([]).providers),[{name:"Codex",connected:true}])[0].label,"Auto-greet stopped");
  T.state.watcher={enabled:true,running:true,persistenceError:"state persistence failed: disk full",providers:{Codex:{phase:"keepalive_scheduled"}}};
  assert.equal(T.attentionItems(T.buildForest(snap([]).providers),[{name:"Codex",connected:true}])[0].label,"Tracker state not saved");
  assert.match(T.watcherPopoverHTML("Codex"),/state persistence failed: disk full/);
  T.state.watcher={enabled:true,running:true,providers:{Codex:{phase:"keepalive_scheduled",lastPollError:"sample timed out"}}};
  assert.equal(T.attentionItems(T.buildForest(snap([]).providers),[{name:"Codex",connected:true}])[0].label,"Quota check stale");
  // Dismissing a failure hides it until it changes.
  T.state.watcher=null;
  T.handleClick({target:{closest(sel){return sel==="[data-action]"?{dataset:{action:"dismiss",dismiss:"failed:Codex:f2:"+ago(5)}}:null;}}});
  assert.ok(!T.attentionItems(T.state.forest,[{name:"Codex",connected:true}]).some(i=>i.key==="failed:Codex:f2"));
  assert.ok(JSON.parse(saved.get("tokenTrackerDismissed")).includes("failed:Codex:f2:"+ago(5)));
}

// ---------- timeline window (section P) ----------
{
  reset();
  const s=snap([
    {id:"a",title:"Recent",status:"idle",updatedAt:ago(120)},
    {id:"b",title:"Yesterday",status:"idle",updatedAt:ago(30*60)},
    {id:"c",title:"Four days",status:"idle",updatedAt:ago(4*24*60)},
    {id:"d",title:"Nine days",status:"idle",updatedAt:ago(9*24*60)},
    {id:"k",parentId:"c",title:"kid",agentName:"/root/kid",status:"idle",updatedAt:ago(10)},   // child activity keeps "Four days" in window
  ]);
  T.render(s);
  assert.deepEqual(Array.from(T.visibleRoots(T.state.forest),c=>c.id),["c","a"]);
  assert.equal(node("#chat-count").textContent,"2");
  assert.equal(node("#chat-count").title,"2 older hidden");
  assert.deepEqual(Array.from(T.visibleRoots(T.state.forest,2),c=>c.id),["c","a","b"]);
  assert.deepEqual(Array.from(T.visibleRoots(T.state.forest,7),c=>c.id),["c","a","b"]);
  T.state.pins.add("Codex:d");
  assert.deepEqual(Array.from(T.visibleRoots(T.state.forest),c=>c.id),["c","a","d"]);   // pinned stays visible without jumping above newer activity
  T.state.pins.clear();
  // Search spans every loaded chat regardless of the window.
  T.state.query="nine";assert.deepEqual(Array.from(T.visibleRoots(T.state.forest),c=>c.id),["d"]);
  T.state.query="is:running";assert.equal(T.visibleRoots(T.state.forest).length,0);
  T.state.query="";
  // Empty window suggests the next window that has chats.
  const only=snap([{id:"b",title:"Yesterday",status:"idle",updatedAt:ago(30*60)},{id:"c",title:"Four days",status:"idle",updatedAt:ago(4*24*60)}]);
  T.render(only);
  assert.match(node("#chat-list").innerHTML,/Nothing in 1d<button class="ghost" data-action="window" data-days="2">2d · 1 chat<\/button>/);
  T.handleClick({target:{closest(sel){return sel==="[data-action]"?{dataset:{action:"window",days:"5"}}:null;}}});
  assert.equal(T.state.windowDays,5);assert.equal(saved.get("tokenTrackerWindow"),"5");
  assert.equal(node("#window-seg").style["--i"],"2");
  assert.match(node("#chat-list").innerHTML,/Four days/);
  const far=snap([{id:"d",title:"Nine days",status:"idle",updatedAt:ago(9*24*60)}]);
  T.setWindow(1);T.render(far);
  assert.match(node("#chat-list").innerHTML,/<div class="empty">Nothing in 1d<\/div>/);
  T.render(snap([]));
  assert.match(node("#chat-list").innerHTML,/No Codex chats/);
  // The window survives a reload; bad values fall back to 1d.
  const reloaded={...context,window:{__TOKEN_TRACKER_TEST__:true}};
  vm.createContext(reloaded);vm.runInContext(app,reloaded);
  assert.equal(reloaded.window.__tokenTrackerTest.state.windowDays,1);
  saved.set("tokenTrackerWindow","7");
  const again={...context,window:{__TOKEN_TRACKER_TEST__:true}};
  vm.createContext(again);vm.runInContext(app,again);
  assert.equal(again.window.__tokenTrackerTest.state.windowDays,7);
}

// ---------- pins, request URL, live overlay ----------
{
  reset();
  const s=snap([{id:"old",title:"Older",status:"idle",updatedAt:ago(60)},{id:"root",title:"Current",status:"idle",updatedAt:ago(1)},{id:"kid",parentId:"root",title:"kid",status:"idle",updatedAt:ago(1)}]);
  T.render(s);
  const click=(action,key)=>T.handleClick({target:{closest(sel){return sel==="[data-action]"?{dataset:{action,key}}:null;}}});
  click("pin","Codex:old");click("pin","Codex:kid");   // pinning a child pins its root
  assert.deepEqual(JSON.parse(saved.get("tokenTrackerPinsV2")),["Codex:old","Codex:root"]);
  assert.match(node("#chat-list").innerHTML,/class="chat pinned"/);
  click("pin","Codex:root");
  assert.ok(node("#chat-list").innerHTML.indexOf("Current")<node("#chat-list").innerHTML.indexOf("Older"));   // latest stays first when older chat is pinned
  assert.equal(T.statsUrl(true),"/api/stats?limit=50&pins=Codex%3Aold&refresh=1");
  T.state.query="old & agent is:needs";
  assert.equal(T.statsUrl(false),"/api/stats?limit=50&pins=Codex%3Aold&q=old%20%26%20agent&status=attention");
  T.state.query="";T.state.pins.clear();
  click("open","Codex:root");assert.ok(!T.state.open.has("Codex:root")); // open is reserved for opening the Codex thread
  click("toggle-chat","Codex:root");assert.ok(T.state.open.has("Codex:root"));
  click("toggle-chat","Codex:root");assert.ok(!T.state.open.has("Codex:root"));

  const live={structureStamp:"a",generatedAt:new Date(fixedNow-5000).toISOString(),providers:[{name:"Codex",connected:true,activity:[],chats:[{id:"x",title:"Loaded",status:"idle",updatedAt:ago(10)}]}]};
  const liveAt=new Date(fixedNow).toISOString();
  const result=T.applyLive(live,{generatedAt:liveAt,chats:[{key:"Codex:x",status:"needs input",pendingQuestion:"Ship it?",activityAt:liveAt,toolCalls:12,filesEdited:3,runStartedAt:ago(4),runEndedAt:null},{key:"Codex:new",status:"running",activityAt:liveAt}]});
  const chat=live.providers[0].chats[0];
  assert.equal(result.changed,true);
  assert.equal(chat.status,"needs input");assert.equal(chat.pendingQuestion,"Ship it?");assert.equal(chat.updatedAt,liveAt);
  assert.equal(chat.toolCalls,12);assert.equal(chat.filesEdited,3);assert.equal(chat.runStartedAt,ago(4));assert.equal(chat.runEndedAt,null);
  assert.deepEqual(Array.from(result.outside,pair=>pair[0]),["Codex:new"]);
  assert.equal(T.applyLive(live,{generatedAt:liveAt,chats:[{key:"Codex:x",status:"needs input",pendingQuestion:"Ship it?",activityAt:liveAt,toolCalls:12}]}).changed,false);
}

// ---------- provider down / offline / totals ----------
{
  reset();
  const down={enabledProviders:["Codex"],providers:[{name:"Codex",connected:false,error:"codex app-server exited",activity:[],chats:[]}]};
  T.render(down);
  const items=T.attentionItems(T.state.forest,down.providers);
  assert.deepEqual(Array.from(items,i=>[i.kind,i.label]),[["provider","Unavailable"]]);
  assert.match(node("#chat-list").innerHTML,/No data/);
  assert.match(node("#totals").innerHTML,/Today<\/span> <b>—<\/b>/);
  const expired={enabledProviders:["Codex"],providers:[{name:"Codex",connected:false,error:"401 Unauthorized: token expired",activity:[],chats:[]}]};
  assert.equal(T.attentionItems(T.buildForest(expired.providers),expired.providers)[0].kind,"auth");
  const ok=snap([],{activity:[{date:"2026-09-23",value:153000},{date:"2026-09-20",value:105e6}],cache:{percent:97,read:422e6,uncachedInput:15.1e6,output:1.35e6,cacheSavings:1816,priceBasis:"list prices"}});
  T.render(ok);
  assert.match(node("#totals").innerHTML,/Today<\/span> <b>153K<\/b>[\s\S]*7d<\/span> <b>105M<\/b>[\s\S]*saved<\/span> <b>\$1\.82K<\/b>/);
  assert.match(node("#totals").innerHTML,/97% hit · 422M cached · 15\.1M fresh · 1\.35M out/);
  assert.equal(node("#sync-age").textContent,"now");
}

// ---------- disabled providers never render ----------
{
  reset();
  const codexOnly={enabledProviders:["Codex"],providers:[{name:"Codex",connected:true,activity:[],chats:[{id:"c",title:"Only codex",status:"idle",updatedAt:ago(1)}]},{name:"Claude",connected:true,activity:[],chats:[{id:"z",title:"Hidden claude",status:"idle",updatedAt:ago(1)}]}]};
  T.render(codexOnly);
  for(const selector of ["#chat-list","#strip","#totals","#resets"])assert.doesNotMatch(node(selector).innerHTML,/Claude|claude/);
}

// ---------- provider policy overrides stale roster and limit lists ----------
{
  reset();
  const codex={name:"Codex",connected:true,activity:[],chats:[{id:"c",title:"Visible Codex",status:"idle",updatedAt:ago(1)}]};
  const claude={name:"Claude",connected:true,activity:[],chats:[{id:"z",title:"Policy-visible Claude",status:"idle",updatedAt:ago(1)}]};
  const policies=(claudeShow)=>({schemaVersion:1,generation:claudeShow?"show-claude":"hide-claude",providers:[
    {name:"Codex",collect:true,show:true,send:false,sendOrigin:"default",capabilities:{greet:false,continue:false}},
    {name:"Claude",collect:true,show:claudeShow,send:false,sendOrigin:"default",capabilities:{greet:false,continue:false}}
  ]});
  // Policy only restricts; the backend roster decides which providers carry chats.
  T.render({providerPolicies:policies(true),enabledProviders:["Codex"],limitProviders:[],providers:[codex,claude]});
  assert.doesNotMatch(node("#chat-list").innerHTML,/Policy-visible Claude/,"policy show cannot add a provider the roster omits");
  T.render({providerPolicies:policies(true),enabledProviders:["Codex","Claude"],limitProviders:[],providers:[codex,claude]});
  assert.match(node("#chat-list").innerHTML,/Policy-visible Claude/);
  T.render({providerPolicies:policies(false),enabledProviders:["Codex","Claude"],limitProviders:["Claude"],providers:[codex,claude]});
  for(const selector of ["#chat-list","#strip","#totals","#resets"])assert.doesNotMatch(node(selector).innerHTML,/Claude|Policy-visible/);
  assert.deepEqual(T.state.snapshot.providers.map(provider=>provider.name),["Codex"],"hidden provider payload is removed from the UI snapshot");
  T.state.pins=new Set(["Codex:c","Claude:z"]);
  assert.match(T.statsUrl(false),/pins=Codex%3Ac/);
  assert.doesNotMatch(T.statsUrl(false),/Claude|Claude%3A/);
}

// ---------- limits-only providers show quota meters and nothing else ----------
{
  reset();
  const withClaudeLimits={enabledProviders:["Codex"],limitProviders:["Claude"],providers:[
    {name:"Codex",connected:true,activity:[{date:new Date(fixedNow).toISOString().slice(0,10),value:1000}],chats:[{id:"c",title:"Only codex",status:"idle",updatedAt:ago(1)}],
     fiveHour:{percent:100,resetAt:Math.floor(fixedNow/1000)+3600},weekly:{percent:32,resetAt:Math.floor(fixedNow/1000)+86400},limitsUpdatedAt:new Date(fixedNow).toISOString()},
    {name:"Claude",connected:true,limitsOnly:true,activity:[],chats:[],cache:null,
     fiveHour:{percent:4,resetAt:Math.floor(fixedNow/1000)+7200},weekly:{percent:32,resetAt:Math.floor(fixedNow/1000)+172800},limitsUpdatedAt:new Date(fixedNow).toISOString()}]};
  T.render(withClaudeLimits);
  const resets=node("#resets").innerHTML;
  assert.match(resets,/id="r0-fiveHour-meter"[\s\S]*id="r0-weekly-meter"/);
  assert.doesNotMatch(resets,/id="r1-fiveHour-meter"|id="r1-weekly-meter"/);
  assert.doesNotMatch(resets,/Claude|claude/);
  assert.doesNotMatch(node("#strip").innerHTML,/Claude|claude/);
  assert.equal(node("#brand-name").textContent,"Codex");
  for(const selector of ["#chat-list","#totals"])assert.doesNotMatch(node(selector).innerHTML,/Claude|claude/);
}

// ---------- native resize bridge ----------
{
  const listeners=new Map(),sizes=[];
  const edge={dataset:{resize:"corner"},setPointerCapture(){},addEventListener(n,fn){listeners.set(n,fn)},removeEventListener(n){listeners.delete(n)}};
  context.document.querySelectorAll=()=>[edge];
  let resolveSize;
  context.window.pywebview={api:{resize_start:()=>new Promise(resolve=>{resolveSize=resolve}),resize:(w,h)=>sizes.push([w,h])}};
  T.setupResize();
  const drag=listeners.get("pointerdown")({button:0,pointerId:1,screenX:400,screenY:800,preventDefault(){}});
  listeners.get("pointermove")({screenX:300,screenY:600});
  listeners.get("pointerup")();
  resolveSize({width:420,height:880});
  await drag;
  assert.deepEqual(sizes,[[320,680]]);
  context.document.querySelectorAll=()=>[];
}
console.log("Token Tracker UI tests passed");
{
  // A question's answer options are shown as copyable chips, not just the question.
  reset();
  T.render(snap([{id:"q",title:"Improve wake word",model:"gpt-5.6-luna",status:"needs input",pendingQuestion:"Did SoundDrum play it?",pendingOptions:["Yes, both worked","Heard it, no duck","No"],updatedAt:ago(1),usageUpdatedAt:ago(1)}]));
  const strip=node("#strip").innerHTML;
  assert.match(strip,/Did SoundDrum play it\?/);
  assert.match(strip,/<span class="q-action-note">Open Codex to answer · queue is a later message<\/span>[\s\S]*class="q-opt" data-action="answer" data-answer="Yes, both worked" data-thread="q"[^>]*aria-label="Open Codex to answer: Yes, both worked"[^>]*title="Open this task in Codex and select the choice in its question prompt\. The choice is also copied for reference\."><b>1<\/b>Yes, both worked/);
  assert.match(strip,/class="q-open q-queue" data-action="queue-answer" data-answer="Yes, both worked" data-thread="q" title="Queue this as a message for the next turn\. It will not answer the pending question\."><span>Queue to chat<\/span>/);
  assert.match(strip,/<b>3<\/b>No/);
  T.state.open.add("Codex:q");T.render(T.state.snapshot);
  assert.match(node("#chat-list").innerHTML,/blk t-attn[\s\S]*q-opt[\s\S]*Heard it, no duck/);
  console.log("Token Tracker question options UI tests passed");
}
{
  // Historical agent rows stay out of the live tray; active subagents remain visible at compact width.
  reset();
  T.render(snap([
    {id:"p",title:"Parent",status:"running",updatedAt:ago(0)},
    {id:"old-review",parentId:"p",agentRole:"auto-review",title:"Old review",status:"idle",tokens:12000,toolCalls:4,updatedAt:ago(120)},
    {id:"quiet-review",parentId:"p",agentRole:"auto-review",title:"Subagent",model:"Codex",status:"quiet",tokens:24800,toolCalls:0,updatedAt:ago(7)},
    {id:"done",parentId:"p",agentName:"/agents/done",title:"Done",status:"idle",stateDetail:"completed",tokens:3300,updatedAt:ago(0)},
    {id:"failed",parentId:"p",agentName:"/agents/failed",title:"Failed",status:"failed",tokens:4300,updatedAt:ago(0)},
    {id:"worker",parentId:"p",agentName:"/agents/worker",title:"Worker",status:"running",tokens:900,toolCalls:3,updatedAt:ago(0)}
  ]));
  const parent=T.state.forest.roots[0];
  assert.equal(T.trayState(parent),"active");
  const tray=T.trayEntries(parent,{active:false});
  assert.ok(tray.some(entry=>entry.html.includes("Worker")));
  assert.ok(!tray.some(entry=>entry.html.includes("Old review")));
  assert.ok(!tray.some(entry=>entry.html.includes("Auto-review")));
  assert.ok(!tray.some(entry=>entry.html.includes("Done")||entry.html.includes("Failed")));
  assert.ok(!T.chatBlockHTML({...parent,descendants:[parent.descendants.find(agent=>agent.id==="quiet-review")]},{active:false}).includes("tray"));
  assert.equal(T.normalizeStatus("compacting"),"compacting");
  assert.equal(T.ownState({...parent,status:"compacting"}).word,"Compacting");
  assert.match(T.chatBlockHTML(parent,{active:false}),/data-action="toggle-chat"/);
  console.log("Token Tracker active agents / compacting UI tests passed");
}
{
  // A selected choice is copied and the exact Codex chat is opened; the widget never claims it sent a reply.
  const calls=[],clipboard=[];
  context.AbortSignal={timeout:ms=>({ms})};
  context.navigator={clipboard:{writeText:async text=>clipboard.push(text)}};
  context.fetch=async (url,options={})=>{calls.push([url,options]);return {ok:true,json:async()=>({ok:true})};};
  const choiceButton={disabled:false,title:"",classList:{add(name){this[name]=true}}};
  await T.copyAndOpenAnswer("01a0cad8-b212-72f2-88d3-2c887f7d4445","Yes, both worked",choiceButton);
  assert.deepEqual(clipboard,["Yes, both worked"]);
  assert.match(choiceButton.title,/Select this choice in its question prompt/);
  assert.doesNotMatch(choiceButton.title,/paste and send/i);
  assert.deepEqual(calls.map(call=>call[0]),["/api/open?thread=01a0cad8-b212-72f2-88d3-2c887f7d4445"]);
  assert.doesNotMatch(app,/fetch\("\/api\/answer"/);
  assert.match(T.optionsHTML(["Yes, both worked"],"q"),/Open Codex to answer[\s\S]*select the choice in its question prompt/);
  assert.match(T.optionsHTML(["Yes, both worked"],"q"),/data-action="queue-answer"[\s\S]*Queue to chat/);
  console.log("Token Tracker question copy-and-open UI tests passed");
}
{
  // Queue submits a normal message for the next turn and never reports that the question was answered.
  const calls=[];let label="Queue to chat";
  context.AbortSignal={timeout:ms=>({ms})};
  context.fetch=async (url,options={})=>{calls.push([url,options]);return {ok:true,json:async()=>({ok:true,status:"queued",detail:"Queued for the next turn. This does not answer the pending question."})};};
  const button={disabled:false,title:"",classList:{add(name){this[name]=true},contains(name){return Boolean(this[name])}},querySelector(){return {set textContent(value){label=value}}}};
  const result=await T.queueAnswer("01a0cad8-b212-72f2-88d3-2c887f7d4445","Yes, both worked",button);
  assert.equal(result.status,"queued");
  assert.equal(label,"Queued");
  assert.equal(button.disabled,true);
  assert.equal(calls[0][0],"/api/queue");
  assert.equal(calls[0][1].method,"POST");
  assert.deepEqual(JSON.parse(calls[0][1].body),{thread:"01a0cad8-b212-72f2-88d3-2c887f7d4445",message:"Yes, both worked"});
  assert.match(button.title,/does not answer/);
  console.log("Token Tracker next-turn queue UI tests passed");
}
{
  // Codex avatar hash, verified against the Codex app: Terra foundations shows the blue avatar #2.
  assert.equal(T.avatarIndex("01a0975c-2855-74c0-83e1-8d7f825b8449",28),2);
  const html=T.filesHTML({files:6,changes:{created:2,modified:3,deleted:1}});
  assert.match(html,/fc fc-add"[^>]*>\+2<[\s\S]*fc fc-mod"[^>]*>~3<[\s\S]*fc fc-del"[^>]*>−1<[\s\S]*files/);
  assert.match(T.filesHTML({files:1,changes:{created:0,modified:1,deleted:0}}),/fc fc-add zero/);
  assert.match(T.filesHTML({files:4,changes:null}),/<b>4<\/b> <i>files<\/i>/);
  // A free-text question still gets an action: Open in Codex.
  assert.match(T.optionsHTML([],"abc"),/data-action="open" data-thread="abc"[\s\S]*Answer in Codex/);
  console.log("Token Tracker avatar / file changes UI tests passed");
}
{
  // Audit fixes: one outage = one strip item; free-text asks carry an inline Answer action.
  reset();
  T.state.failSince=fixedNow-60000;T.state.lastOkAt=fixedNow-300000;
  const items=T.attentionItems({rows:[]},[{name:"Codex",connected:false,error:"app-server exited"}]);
  assert.equal(JSON.stringify(items.map(i=>i.kind)),JSON.stringify(["offline"]));
  T.state.failSince=null;
  T.render(snap([{id:"f",title:"Free text",status:"needs input",pendingQuestion:"What did you say?",updatedAt:ago(1)}]));
  const strip=node("#strip").innerHTML;
  assert.match(strip,/si-q si-ask[\s\S]*What did you say\?[\s\S]*data-action="open" data-thread="f"[\s\S]*Answer/);
  assert.doesNotMatch(strip,/si-opts/);
  console.log("Token Tracker audit-fix UI tests passed");
}
