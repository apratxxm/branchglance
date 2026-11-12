import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const app=fs.readFileSync(new URL("../token_tracker/static/app.js",import.meta.url),"utf8");
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
const context={window:{__TOKEN_TRACKER_TEST__:true,__TOKEN_TRACKER_RUNWAY__:true,__TOKEN_TRACKER_REFINED__:true,__TOKEN_TRACKER_COMPACT__:true},document:{querySelector:node,querySelectorAll(){return []}},localStorage:{getItem(key){return saved.get(key)??null},setItem(key,value){saved.set(key,String(value))}},Intl,Date:TestDate,Math,Number,JSON,setTimeout:fn=>fn(),clearTimeout(){}};
context.window.window=context.window;
vm.createContext(context);
vm.runInContext(app,context);
const T=context.window.__tokenTrackerTest;
const ago=m=>new Date(fixedNow-m*60000).toISOString();
const warmModel={warmSeconds:1800,coldSeconds:5400,samples:148,learned:true};
function snap(chats,extra={}){return {generatedAt:new Date(fixedNow).toISOString(),enabledProviders:["Codex"],providers:[{name:"Codex",connected:true,cacheWarmth:warmModel,activity:[],chats,...extra}]};}
function reset(){T.state.pins.clear();T.state.query="";T.state.windowDays=1;T.state.watcher=null;T.state.failSince=null;T.state.lastOkAt=fixedNow;T.state.agentLive.clear();T.state.finishedAt.clear();}

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
const runwayContext={window:{__TOKEN_TRACKER_TEST__:true,__TOKEN_TRACKER_RUNWAY__:true,__TOKEN_TRACKER_REFINED__:true,__TOKEN_TRACKER_COMPACT__:true},document:{hidden:false,activeElement:null,querySelector:runwayNode,querySelectorAll(){return [];},body:{classList:{toggle(){}}}},
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
  assert.match(rootMarkup,/class="runway-refined-timing"/);
  assert.match(rootMarkup,/title="Context occupancy: 80000 \/ 200000 tokens"/);
  assert.notEqual(R.runwayStateElapsed(root),"—");
  assert.match(childMarkup,/runway-refined-status t-attn[^>]*data-own-waiting="true"/);
  assert.match(childMarkup,/runway-status-dot[^>]*aria-label="Waiting"/);
  assert.match(childMarkup,/data-action="runway-question" data-key="Codex:run-worker"/);
  assert.notEqual(R.runwayQuestionPopover(child),"");
  assert.equal(R.notch.active,1);
  assert.notEqual(R.notch.autoTimer,null,"compact mode schedules the existing run's delayed reveal");
  assert.equal(R.notch.autoExpanded,true,"the production compact widget keeps the active waiting run expanded");

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
  assert.equal(low.usedText,"94% used");assert.equal(low.usedPercent,94);assert.equal(low.tone,"err");assert.equal(low.countdown,"38m 00s");assert.equal(low.stale,"");
  const weeklyLow=T.resetView({weekly:{percent:88,resetAt:at(2,8,30)},limitsUpdatedAt:ago(120)},"weekly");
  assert.equal(weeklyLow.tone,"");assert.equal(weeklyLow.usedText,"88% used");assert.equal(weeklyLow.stale,"");
  assert.equal(T.resetView({weekly:{percent:81},limitsUpdatedAt:new Date(fixedNow).toISOString()},"weekly").tone,"err");
  assert.equal(T.resetView({weekly:{percent:80},limitsUpdatedAt:new Date(fixedNow).toISOString()},"weekly").tone,"attn");
  assert.equal(T.resetView({weekly:{percent:51},limitsUpdatedAt:new Date(fixedNow).toISOString()},"weekly").tone,"attn");
  assert.equal(T.resetView({weekly:{percent:50},limitsUpdatedAt:new Date(fixedNow).toISOString()},"weekly").tone,"");
  assert.equal(T.resetView({weekly:{percent:49},limitsUpdatedAt:new Date(fixedNow).toISOString()},"weekly").tone,"");
  assert.equal(T.resetView({fiveHour:{percent:12},limitsUpdatedAt:ago(120)},"fiveHour").tone,"");
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
  {id:"p",title:"Port reset watcher",model:"gpt-5.6-sol",contextModel:"gpt-5.6-sol",contextObservedAt:ago(0),status:"running",contextPercent:92,contextTokens:238000,contextWindow:258400,tokens:8.4e6,turns:18,toolCalls:88,filesEdited:11,runStartedAt:ago(12),activeRunStartedAt:ago(12),updatedAt:ago(0),usageUpdatedAt:ago(0),workspace:"A:\\work"},
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
  assert.match(row,/runway-refined-status[^>]*data-child-waiting="true"/,"the refined row exposes its needs-you child state");
  assert.match(row,/runway-refined-context"><span class="ctx-tokens" title="Context occupancy: 238000 \/ 258400 tokens"><span class="runway-context-fraction"><span class="runway-context-used t-err">238K/);
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
  assert.deepEqual(Array.from(entries,e=>e.key),["Codex:a1","Codex:a4","Codex:a2"]);
  assert.match(entries[0].html,/runway-agent-name[^>]*Windows service/);
  assert.match(entries[0].html,/runway-refined-status t-attn[^>]*>.*?runway-waiting-link/s);
  assert.match(entries[0].cls,/runway-agent/);
  assert.match(entries[1].html,/runway-agent-name[^>]*Auto-review/);
  assert.match(entries[2].html,/runway-refined-status t-live/);
  const list=node("#chat-list").innerHTML;
  assert.match(list,/class="tray runway-expanded"/);
  assert.doesNotMatch(list,/class="l2/);   // layer 2 closed by default
  const a2=tree.providers[0].chats.find(c=>c.id==="a2");
  a2.status="idle";a2.runEndedAt=new Date(fixedNow).toISOString();T.render(tree);
  const fin=T.trayEntries(T.state.forest.roots[0],T.parseQuery("")).find(e=>e.key==="Codex:a2");
  assert.equal(fin,undefined);
}
// A failure older than 30 minutes is history: no escalation, grey state.
{
  const old=T.buildForest(snap([{id:"x",title:"Old fail",status:"failed",stateDetail:"interrupted",updatedAt:ago(31)}]).providers).roots[0];
  assert.equal(T.rollup(old).key,"failed-old");
  assert.match(T.rowHTML(old),/runway-refined-status t-mute[^>]*>.*?Stopped/s);
  const fresh=T.buildForest(snap([{id:"x",title:"New fail",status:"failed",stateDetail:"interrupted",updatedAt:ago(29)}]).providers).roots[0];
  assert.equal(T.rollup(fresh).key,"failed");
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
  resting.providers[0].chats[1].status="running";resting.providers[0].chats[1].stateDetail=null;resting.providers[0].chats[1].runEndedAt=null;resting.providers[0].chats[1].updatedAt=ago(0);resting.providers[0].chats[1].activeRunStartedAt=ago(0);resting.providers[0].chats[1].executionStatus="running";resting.providers[0].chats[1].executionSource="transcript";resting.providers[0].chats[1].executionObservedAt=ago(0);resting.providers[0].chats[1].executionSince=ago(0);
  T.render(resting);
  const woke=T.state.forest.roots[0];
  assert.equal(T.trayState(woke),"active");
  const entries=T.trayEntries(woke,T.parseQuery(""));
  assert.equal(entries.filter(e=>/^ag( |$)/.test(e.cls)&&!/dense|total/.test(e.cls)).length,1);
  assert.equal(entries.filter(e=>/dense/.test(e.cls)).length,0);
}

// ---------- live overlay ----------
{
  reset();
  const live={structureStamp:"a",generatedAt:new Date(fixedNow-5000).toISOString(),providers:[{name:"Codex",connected:true,activity:[],chats:[{id:"x",title:"Loaded",status:"idle",updatedAt:ago(10)}]}]};
  T.render(live);
  const liveAt=new Date(fixedNow).toISOString();
  const result=T.applyLive(live,{generatedAt:liveAt,chats:[{key:"Codex:x",id:"x",provider:"Codex",status:"needs input",pendingQuestion:"Ship it?",activityAt:liveAt,toolCalls:12,filesEdited:3,runStartedAt:ago(4),runEndedAt:null},{key:"Codex:new",id:"new",provider:"Codex",status:"running",activityAt:liveAt}]});
  const chat=live.providers[0].chats[0];
  assert.equal(result.changed,true);
  assert.equal(chat.status,"needs input");assert.equal(chat.pendingQuestion,"Ship it?");assert.equal(chat.updatedAt,liveAt);
  assert.equal(chat.toolCalls,12);assert.equal(chat.filesEdited,3);assert.equal(chat.runStartedAt,ago(4));assert.equal(chat.runEndedAt,null);
  assert.deepEqual(Array.from(result.outside,pair=>pair[0]),["Codex:new"]);
  assert.equal(T.applyLive(live,{generatedAt:liveAt,chats:[{key:"Codex:x",id:"x",provider:"Codex",status:"needs input",pendingQuestion:"Ship it?",activityAt:liveAt,toolCalls:12}]}).changed,false);
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
  assert.ok(T.state.snapshot.providers.some(provider=>provider.name==="Claude"&&provider.chats.some(chat=>chat.id==="z")),"an allowed provider remains in the active roster snapshot");
  T.render({providerPolicies:policies(false),enabledProviders:["Codex","Claude"],limitProviders:["Claude"],providers:[codex,claude]});
  for(const selector of ["#chat-list","#strip","#totals","#resets"])assert.doesNotMatch(node(selector).innerHTML,/Claude|Policy-visible/);
  assert.deepEqual(T.state.snapshot.providers.map(provider=>provider.name),["Codex"],"hidden provider payload is removed from the UI snapshot");
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
  assert.match(resets,/id="r0-fiveHour-meter"/);
  assert.match(node("#weekly-resets").innerHTML,/id="r0-weekly-meter"/);
  assert.doesNotMatch(resets,/id="r1-fiveHour-meter"|id="r1-weekly-meter"/);
  assert.doesNotMatch(resets,/Claude|claude/);
  assert.doesNotMatch(node("#strip").innerHTML,/Claude|claude/);
  assert.equal(node("#brand-name").textContent,"Branchglance");
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
  // Waiting choices are available through the compact question popover.
  reset();
  T.render(snap([{id:"q",title:"Improve wake word",model:"gpt-5.6-luna",status:"needs input",pendingQuestion:"Did SoundDrum play it?",pendingOptions:["Yes, both worked","Heard it, no duck","No"],updatedAt:ago(1),usageUpdatedAt:ago(1)}]));
  const question=T.state.forest.rows.find(chat=>chat.id==="q");
  const popover=T.runwayQuestionPopover(question);
  assert.match(popover,/Did SoundDrum play it\\?/);
  assert.match(popover,/class="q-opt" data-action="answer" data-answer="Yes, both worked" data-thread="q"[^>]*aria-label="Open Codex to answer: Yes, both worked"/);
  assert.match(popover,/class="q-open q-queue" data-action="queue-answer" data-answer="Yes, both worked" data-thread="q"/);
  assert.match(popover,/<b>3<\/b><span>Open in Codex<\/span> · No/);
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
  assert.ok(!tray.some(entry=>entry.html.includes("Done")));
  assert.ok(tray.some(entry=>entry.html.includes("Failed")),"recent failures remain visible as actionable agent rows");
  assert.ok(!T.chatBlockHTML({...parent,descendants:[parent.descendants.find(agent=>agent.id==="quiet-review")]},{active:false}).includes("tray"));
  assert.equal(T.normalizeStatus("compacting"),"compacting");
  assert.equal(T.ownState({...parent,status:"compacting"}).word,"Compacting");
  assert.match(T.chatBlockHTML(parent,{active:false}),/class="runway-refined-row"/);
  console.log("Token Tracker active agents / compacting UI tests passed");
}
{
  // A selected choice is copied and the exact Codex chat is opened; the widget never claims it sent a reply.
  const calls=[],clipboard=[];
  context.AbortSignal={timeout:ms=>({ms})};
  context.navigator={clipboard:{writeText:async text=>clipboard.push(text)}};
  context.fetch=async (url,options={})=>{calls.push([url,options]);return {ok:true,json:async()=>({ok:true})};};
  const choiceButton={disabled:false,title:"",classList:{add(name){this[name]=true}}};
  await T.copyAndOpenAnswer("01a0cad8-0000-7000-8000-000000000001","Yes, both worked",choiceButton);
  assert.deepEqual(clipboard,["Yes, both worked"]);
  assert.match(choiceButton.title,/Select this choice in its question prompt/);
  assert.doesNotMatch(choiceButton.title,/paste and send/i);
  assert.deepEqual(calls.map(call=>call[0]),["/api/open?thread=01a0cad8-0000-7000-8000-000000000001"]);
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
  const result=await T.queueAnswer("01a0cad8-0000-7000-8000-000000000001","Yes, both worked",button);
  assert.equal(result.status,"queued");
  assert.equal(label,"Queued");
  assert.equal(button.disabled,true);
  assert.equal(calls[0][0],"/api/queue");
  assert.equal(calls[0][1].method,"POST");
  assert.deepEqual(JSON.parse(calls[0][1].body),{thread:"01a0cad8-0000-7000-8000-000000000001",message:"Yes, both worked"});
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
  // A free-text ask keeps an Answer action in the compact question popover.
  reset();
  T.render(snap([{id:"f",title:"Free text",status:"needs input",pendingQuestion:"What did you say?",updatedAt:ago(1)}]));
  const question=T.state.forest.rows.find(chat=>chat.id==="f");
  const popover=T.runwayQuestionPopover(question);
  assert.match(popover,/What did you say\?/);
  assert.match(popover,/data-action="open" data-thread="f"[\s\S]*Open in Codex to answer/);
  assert.doesNotMatch(popover,/data-action="answer"|data-action="queue-answer"/);
}
