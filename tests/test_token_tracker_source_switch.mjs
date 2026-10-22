import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const app=fs.readFileSync(new URL("../token_tracker/static/app.js",import.meta.url),"utf8");
const page=fs.readFileSync(new URL("../token_tracker/static/runway.html",import.meta.url),"utf8");
const css=fs.readFileSync(new URL("../token_tracker/static/runway.css",import.meta.url),"utf8");
const fixedNow=new Date("2026-10-06T12:00:00Z").getTime();
const ago=minutes=>new Date(fixedNow-minutes*60000).toISOString();

assert.match(page,/id="source-switch"[^>]*role="group"[^>]*hidden/,"the switch ships hidden");
assert.match(page,/data-action="source" data-source="Codex" aria-pressed="true"/);
assert.match(page,/data-action="source" data-source="Claude" aria-pressed="false"/);
assert.ok(page.indexOf('id="source-switch"')>page.indexOf('id="monitor-toggle"')&&page.indexOf('id="source-switch"')<page.indexOf('class="tb-fill'),"the switch lives in the controls band");
assert.match(css,/\.source-dot\{[^}]*position:absolute/);

function boot(initialStorage={}) {
  const nodes=new Map(),saved=new Map(Object.entries(initialStorage)),timers=new Map();let sequence=0;
  const node=selector=>{
    if(!nodes.has(selector))nodes.set(selector,{innerHTML:"",textContent:"",value:"",hidden:false,title:"",className:"",dataset:{},attributes:{},offsetHeight:0,
      style:{setProperty(k,v){this[k]=v;}},contains(){return false;},querySelectorAll(){return [];},setAttribute(k,v){this.attributes[k]=String(v);},getAttribute(k){return this.attributes[k]??null;},
      classList:{toggle(){},add(){},remove(){}},addEventListener(){},removeEventListener(){},hasAttribute(){return true;},focus(){}});
    return nodes.get(selector);
  };
  const segment=name=>{
    const dot={hidden:true,attrs:{},setAttribute(k,v){this.attrs[k]=String(v);}},flags=new Set(),attrs={"aria-pressed":name==="Codex"?"true":"false"};
    return {dataset:{source:name,action:"source"},dot,flags,attrs,setAttribute(k,v){attrs[k]=String(v);},getAttribute(k){return attrs[k]??null;},
      classList:{toggle(flag,on){on?flags.add(flag):flags.delete(flag);},add(){},remove(){}},querySelector(){return dot;},closest(){return this;}};
  };
  const segments=[segment("Codex"),segment("Claude")];
  class TestDate extends Date {constructor(...args){super(...(args.length?args:[fixedNow]));}static now(){return fixedNow;}}
  const context={window:{__TOKEN_TRACKER_TEST__:true,__TOKEN_TRACKER_RUNWAY__:true},document:{body:{dataset:{},classList:{toggle(){},add(){},remove(){}}},querySelector:node,
    querySelectorAll(selector){return selector==="#source-switch [data-source]"?segments:[];},documentElement:{style:{setProperty(){}}},hidden:false,activeElement:null},
    localStorage:{getItem:key=>saved.get(key)??null,setItem:(key,value)=>saved.set(key,String(value))},fetch:async()=>({ok:true,json:async()=>({ok:true})}),Intl,Date:TestDate,Math,Number,JSON,AbortController,AbortSignal,
    setTimeout:callback=>{timers.set(++sequence,callback);return sequence;},clearTimeout:id=>timers.delete(id),setInterval(){return 0;}};
  context.window.addEventListener=()=>{};context.document.addEventListener=()=>{};
  vm.createContext(context);vm.runInContext(app,context);
  return {T:context.window.__tokenTrackerTest,node,saved,segments,context,timers};
}
const policies=(codex=true,claude=true)=>({schemaVersion:1,generation:"g"+codex+claude,providers:[
  {name:"Codex",collect:codex,show:codex,send:false,sendOrigin:"default",capabilities:{greet:false,continue:false}},
  {name:"Claude",collect:claude,show:claude,send:false,sendOrigin:"default",capabilities:{greet:false,continue:false}}]});
const codexChat=(id,extra={})=>({id,title:"Codex "+id,model:"gpt-6.1-sol",status:"idle",updatedAt:ago(1),...extra});
const claudeChat=(id,extra={})=>({id,title:"Claude "+id,model:"claude-opus-4-8",status:"idle",updatedAt:ago(1),...extra});
const running=(extra={})=>({status:"running",activeRunStartedAt:ago(2),runStartedAt:ago(2),executionStatus:"running",executionSource:"hook",executionObservedAt:ago(0),executionSince:ago(2),...extra});
const quota={fiveHour:{percent:80,resetAt:fixedNow/1000+1500},weekly:{percent:60,resetAt:fixedNow/1000+80000},limitsUpdatedAt:ago(1)};
const snapshotOf=({codex=[],claude=[],policy=policies(),claudeQuota=quota}={})=>({generatedAt:ago(0),enabledProviders:["Codex","Claude"],providerPolicies:policy,
  providers:[{name:"Codex",connected:true,chats:codex,...quota,fiveHour:{percent:92,resetAt:fixedNow/1000+1500}},{name:"Claude",connected:true,chats:claude,...claudeQuota}]});

// 1. Hidden with one provider, visible with two.
{
  const {T,node}=boot({tokenTrackerRunwayHiddenChats:JSON.stringify(["Codex:c1","Claude:s1","Claude:s2:agent1","Gemini:g1","Claude:"])});
  assert.deepEqual(Array.from(T.state.hiddenChats).sort(),["Claude:s1","Claude:s2:agent1","Codex:c1"],"hidden preferences accept qualified keys for both sources only");
  T.render(snapshotOf({codex:[codexChat("c1",running())],policy:policies(true,false)}));
  assert.equal(node("#source-switch").hidden,true,"one visible provider keeps the switch hidden");
  assert.deepEqual(Array.from(T.sourceNames(T.state.snapshot)),["Codex"]);
  const single=boot();
  single.T.render({generatedAt:ago(0),enabledProviders:["Codex"],providers:[{name:"Codex",connected:true,chats:[]}]});
  assert.equal(single.node("#source-switch").hidden,true,"a snapshot without provider policies shows Codex only");
  const both=boot();
  both.T.render(snapshotOf({codex:[codexChat("c1",running())]}));
  assert.equal(both.node("#source-switch").hidden,false,"two visible providers show the switch");
  assert.deepEqual(Array.from(both.T.sourceNames(both.T.state.snapshot)),["Codex","Claude"]);
}

// 2. Switching swaps forest, counts, quota bands and accent while leaving position state alone.
{
  const {T,node,saved,segments,context}=boot();
  T.render(snapshotOf({codex:[codexChat("c1",running())],claude:[claudeChat("s1",running()),claudeChat("s2",running({pendingQuestion:"Which file?",pendingOptions:["a.py","b.py"],status:"needs-input",executionStatus:"idle"}))],claudeQuota:{fiveHour:{percent:35,resetAt:fixedNow/1000+900},weekly:{percent:50,resetAt:fixedNow/1000+80000},limitsUpdatedAt:ago(1)}}));
  assert.deepEqual(Array.from(T.state.forest.roots,chat=>chat.key),["Codex:c1"]);
  assert.match(node("#notch-counts").innerHTML,/data-count-slot="groups"[^>]*aria-label="1 /);
  assert.equal(node("#resets").dataset.shape,"Codex");
  const lock={...T.widgetLock},keep={...T.keepOpenState};
  T.handleClick({target:{closest:()=>({dataset:{action:"source",source:"Claude"}})}});
  assert.equal(T.state.source,"Claude");assert.equal(saved.get("branchglanceSource"),"Claude","the selection is persisted as a convenience");
  assert.deepEqual(Array.from(T.state.forest.roots,chat=>chat.key).sort(),["Claude:s1","Claude:s2"]);
  assert.match(node("#notch-counts").innerHTML,/data-count-slot="groups"[^>]*aria-label="2 /,"counts follow the selected source");
  assert.match(node("#notch-counts").innerHTML,/data-count-slot="waiting"(?![^>]*is-empty)/,"the Claude waiting row counts as waiting");
  assert.equal(node("#resets").dataset.shape,"Claude","quota bands rebuild for the selected provider");
  assert.match(node("#r0-fiveHour-left").innerHTML,/65%/,"the five-hour band shows Claude's reading, not Codex's");
  assert.equal(context.document.body.dataset.source,"Claude","the provider accent follows the selected source");
  assert.equal(segments[1].attrs["aria-pressed"],"true");assert.equal(segments[0].attrs["aria-pressed"],"false");
  assert.deepEqual({...T.widgetLock},lock,"lock state is untouched by a source switch");assert.deepEqual({...T.keepOpenState},keep,"keep-open state is untouched");
  T.handleClick({target:{closest:()=>({dataset:{action:"source",source:"Codex"}})}});
  assert.deepEqual(Array.from(T.state.forest.roots,chat=>chat.key),["Codex:c1"]);
  assert.equal(saved.get("branchglanceSource"),"Codex");
  T.setSource("Nope");assert.equal(T.state.source,"Codex","unknown sources are ignored");
}

// 3. Stored selection is honored only while that source is visible.
{
  const stored=boot({branchglanceSource:"Claude"});
  stored.T.render(snapshotOf({claude:[claudeChat("s1",running())]}));
  assert.equal(stored.T.state.forest.roots[0].key,"Claude:s1");
  const hidden=boot({branchglanceSource:"Claude"});
  hidden.T.render(snapshotOf({codex:[codexChat("c1",running())],policy:policies(true,false)}));
  assert.equal(hidden.T.effectiveSource(hidden.T.state.snapshot),"Codex","a policy-hidden stored source falls back");
  assert.deepEqual(Array.from(hidden.T.state.forest.roots,chat=>chat.key),["Codex:c1"]);
  const garbage=boot({branchglanceSource:"Gemini"});
  assert.equal(garbage.T.state.source,"Codex");
}

// 4. A new run or question in the hidden source reveals the widget and flags its segment, never switching.
{
  const {T,segments}=boot();
  T.render(snapshotOf({codex:[codexChat("c1")],claude:[claudeChat("s1")]}));
  assert.equal(T.state.sourceAttention.size,0);assert.equal(T.notch.autoExpanded,false);
  T.render(snapshotOf({codex:[codexChat("c1")],claude:[claudeChat("s1",{status:"needs-input",pendingQuestion:"Proceed?",pendingQuestionAt:ago(0),executionStatus:"idle"})]}));
  assert.equal(T.state.source,"Codex","attention never auto-switches the source");
  assert.ok(T.state.sourceAttention.has("Claude"));assert.equal(segments[1].dot.attrs["aria-hidden"],"false","the Claude segment shows the attention dot");assert.equal(segments[0].dot.attrs["aria-hidden"],"true");
  assert.equal(T.notch.autoExpanded,true,"the normal reveal starts for hidden-source work");
  T.setSource("Claude");assert.equal(T.state.sourceAttention.has("Claude"),false,"viewing the source clears the flag");assert.equal(segments[1].dot.attrs["aria-hidden"],"true");
  T.render(snapshotOf({codex:[codexChat("c1",running({activeRunStartedAt:ago(0)}))],claude:[claudeChat("s1")]}));
  assert.ok(T.state.sourceAttention.has("Codex"),"the symmetric case flags Codex while Claude is selected");
}

// The runway Show hidden toggle restores explicitly hidden active chats.
{
  const {T,node}=boot({tokenTrackerRunwayHiddenChats:JSON.stringify(["Codex:c1"])});
  T.render(snapshotOf({codex:[codexChat("c1",running())],policy:policies(true,false)}));
  assert.doesNotMatch(node("#chat-list").innerHTML,/Codex c1/);
  const button={dataset:{action:"show-hidden"},closest:()=>button};
  T.handleClick({target:button});
  assert.equal(T.state.showHidden,true);
  assert.match(node("#chat-list").innerHTML,/Codex c1/);
}

// A new question on an already-running hidden session is a separate transition.
{
  const {T,segments,timers}=boot({branchglanceSource:"Claude"});
  T.render(snapshotOf({codex:[codexChat("c1")],claude:[claudeChat("s1",running())]}));
  T.setSource("Codex");
  const timer=T.notch.autoTimer;timers.get(timer)?.();
  T.render(snapshotOf({codex:[codexChat("c1")],claude:[claudeChat("s1",running({status:"needs-input",pendingQuestion:"Continue?",pendingQuestionAt:ago(0),pendingQuestionId:"q-1"}))]}));
  assert.equal(T.state.source,"Codex");
  assert.ok(T.state.sourceAttention.has("Claude"));
  assert.equal(segments[1].dot.attrs["aria-hidden"],"false","the question transition sets the hidden provider dot");
  assert.equal(T.notch.autoExpanded,true,"the question transition reveals despite the unchanged run");
}

// A question first seen on the poll that ends a telemetry gap still reveals.
{
  const {T,segments,timers}=boot({branchglanceSource:"Claude"});
  T.render(snapshotOf({codex:[codexChat("c1")],claude:[claudeChat("s1",running())]}));
  T.setSource("Codex");
  const timer=T.notch.autoTimer;timers.get(timer)?.();
  T.state.liveRecoveryPending=true;
  T.render(snapshotOf({codex:[codexChat("c1")],claude:[claudeChat("s1",running({status:"needs-input",pendingQuestion:"Continue?",pendingQuestionAt:ago(0),pendingQuestionId:"q-2"}))]}));
  assert.equal(T.state.liveRecoveryPending,false);
  assert.ok(T.state.sourceAttention.has("Claude"),"recovery does not swallow a new question");
  assert.equal(segments[1].dot.attrs["aria-hidden"],"false");
  assert.equal(T.notch.autoExpanded,true,"the recovered question reveals");
}

// 5. Hook-sourced rows use the same states as transcript rows; app evidence still establishes no activity.
{
  const {T}=boot();
  T.render(snapshotOf({codex:[codexChat("c1")]}));
  const activity={kind:"reading",detail:"Reading file",files:[],observedAt:ago(0)};
  const hook=T.buildForest([{name:"Claude",chats:[claudeChat("h1",running({executionActivity:activity}))]}]).rows[0];
  assert.equal(T.runwayExecutionView(hook).status,"running");assert.equal(T.runwayExecutionView(hook).activity.kind,"reading");
  assert.equal(T.runwayActivityEvidence(hook).status,"running");assert.equal(T.runwayActivityEvidence(hook).activity.kind,"reading");
  const compacting=T.buildForest([{name:"Claude",chats:[claudeChat("h2",running({status:"compacting",executionStatus:"compacting",executionCompactionStartedAt:ago(1),executionActivity:null}))]}]).rows[0];
  assert.equal(T.runwayExecutionView(compacting).status,"compacting");assert.equal(T.runwayActivityEvidence(compacting).status,"compacting");
  const waiting=T.buildForest([{name:"Claude",chats:[claudeChat("h3",{status:"needs-input",pendingQuestion:"Ok?",pendingQuestionAt:ago(0),executionStatus:"needs-input",executionSource:"hook",executionObservedAt:ago(0)})]}]).rows[0];
  assert.equal(T.runwayWaiting(waiting),true);assert.equal(T.runwayActivityEvidence(waiting).status,"waiting");
  const app=T.buildForest([{name:"Claude",chats:[claudeChat("h4",running({executionSource:"app",executionActivity:activity}))]}]).rows[0];
  assert.equal(T.runwayExecutionView(app).status,"running");assert.equal(T.runwayExecutionView(app).activity,null,"app evidence never carries activity");
  assert.equal(T.runwayActivityEvidence(app).activity,null);
  const old=T.buildForest([{name:"Claude",chats:[claudeChat("h5",running({executionObservedAt:ago(3)}))]}]).rows[0];
  assert.equal(T.runwayExecutionView(old),null,"the single 150 s bound applies to hook rows");
  const unknown=T.buildForest([{name:"Claude",chats:[claudeChat("h6",running({executionSource:"mystery"}))]}]).rows[0];
  assert.equal(T.runwayExecutionView(unknown),null);
}

// 6. Claude questions carry no Codex actions; resume is offered only for resumable sessions.
{
  const {T,node}=boot({branchglanceSource:"Claude"});
  const base={status:"needs-input",pendingQuestion:"Which file?",pendingQuestionAt:ago(0),pendingOptions:["a.py","b.py"],executionStatus:"needs-input",executionSource:"hook",executionObservedAt:ago(0)};
  T.render(snapshotOf({claude:[claudeChat("11111111-1111-4111-8111-111111111111",{...base,resumable:true}),claudeChat("22222222-2222-4222-8222-222222222222",{...base,resumable:false}),claudeChat("33333333-3333-4333-8333-333333333333",{...base})]}));
  const rows=T.state.forest.rows;
  const resumable=T.runwayQuestionPopover(rows.find(chat=>chat.resumable===true));
  assert.match(resumable,/Which file\?/);assert.match(resumable,/<li>a\.py<\/li>/);
  assert.match(resumable,/data-action="copy" data-copy="claude --resume 11111111-1111-4111-8111-111111111111"/);assert.match(resumable,/Copy resume command/);
  for(const html of [resumable,T.runwayQuestionPopover(rows.find(chat=>chat.resumable===false)),T.runwayQuestionPopover(rows.find(chat=>chat.resumable===undefined))])
    assert.doesNotMatch(html,/Open in Codex|data-action="(?:open|answer|queue-answer)"|data-thread|codex resume/i);
  assert.doesNotMatch(T.runwayQuestionPopover(rows.find(chat=>chat.resumable===false)),/resume/i,"a non-resumable session offers no resume command");
  assert.doesNotMatch(T.runwayQuestionPopover(rows.find(chat=>chat.resumable===undefined)),/resume/i);
  for(const chat of rows)assert.doesNotMatch(T.rowHTML(chat),/data-thread|Open in Codex|codex resume/);
  assert.equal(T.resumeCommand({provider:"Claude",id:"--help",sessionId:"--help",resumable:true}),"","option-looking IDs never reach the clipboard");
  assert.equal(T.resumeCommand({provider:"Claude",id:"11111111-1111-4111-8111-111111111111:agent_7",resumable:true}),"claude --resume 11111111-1111-4111-8111-111111111111","resume strips a valid agent suffix");
  assert.equal(T.resumeCommand({provider:"Claude",id:"not-a-uuid",resumable:true}),"");
  assert.equal(T.resumeCommand({provider:"Codex",id:"abc"}),"codex resume abc");
  const items=T.attentionItems(T.state.forest,T.limitProvidersFor(T.state.snapshot));
  assert.ok(items.filter(item=>item.kind==="ask").every(item=>!item.thread),"Claude attention items carry no thread");
  // Clicking Waiting on a Claude row opens the read-only popover rather than a Codex deep link.
  const target=rows[0];
  const button={dataset:{action:"runway-question",key:target.key},closest:()=>button,getBoundingClientRect:()=>({left:0,bottom:0})};
  node("#popover").contains=()=>false;
  T.handleClick({target:button});
  assert.equal(T.state.popover,button,"a Claude waiting click opens the question popover");
  assert.match(node("#popover").innerHTML,/Which file\?/);assert.doesNotMatch(node("#popover").innerHTML,/Open in Codex/);
  T.setSource("Claude");
}

// 7. Live merge and discovery are provider-aware and remain a pure restriction.
{
  const {T}=boot();
  const snapshot=snapshotOf({codex:[codexChat("c1")],claude:[claudeChat("s1")]});
  const live={generatedAt:ago(0),structureStamp:"x",chats:[
    {id:"c2",key:"Codex:c2",provider:"Codex",title:"New codex",status:"running",activityAt:ago(0)},
    {id:"s2:agent1",key:"Claude:s2:agent1",provider:"Claude",title:"New claude agent",parentId:"s2",status:"running",activityAt:ago(0),...running()},
    {id:"s1",key:"Claude:s1",provider:"Claude",status:"running",activityAt:ago(0),...running({executionActivity:{kind:"reading",files:[],observedAt:ago(0)}})},
    {id:"bad",key:"Claude:bad",provider:"Codex",status:"running",activityAt:ago(0)},
    {id:"other",key:"Codex:wrong",provider:"Codex",status:"running",activityAt:ago(0)}]};
  assert.equal(T.liveIdentity(live.chats[1]),"Claude");assert.equal(T.liveIdentity(live.chats[3]),null);assert.equal(T.liveIdentity(live.chats[4]),null);
  const result=T.applyLive(snapshot,live);
  assert.equal(result.changed,true);
  const claude=snapshot.providers.find(provider=>provider.name==="Claude").chats,codex=snapshot.providers.find(provider=>provider.name==="Codex").chats;
  assert.deepEqual(codex.map(chat=>chat.id),["c1","c2"]);
  assert.deepEqual(claude.map(chat=>chat.id),["s1","s2:agent1"],"qualified Claude ids, including ':' suffixes, are inserted");
  assert.equal(claude[0].executionSource,"hook");assert.equal(claude[0].executionActivity.kind,"reading","known Claude rows merge hook evidence");
  const outside=T.applyLive(snapshotOf({codex:[codexChat("c1")],claude:[claudeChat("s1")]}),{...live,chats:[]});
  assert.deepEqual(Array.from(outside.outside),[]);
  // Discovery of an unseen row is reported for either provider when the snapshot is stale.
  const stale=snapshotOf({codex:[codexChat("c1")],claude:[claudeChat("s1")]});
  const probe=T.applyLive(stale,{generatedAt:ago(0),chats:[{id:"s9",key:"Claude:s9",provider:"Claude",status:"idle",activityAt:ago(0)}]});
  assert.equal(stale.providers[1].chats.length,2);assert.equal(probe.changed,true);
  const guarded=snapshotOf({codex:[codexChat("c1")],claude:[claudeChat("s1")]});
  const malicious=T.applyLive(guarded,{generatedAt:ago(0),chats:[
    {key:"Claude:s1",provider:"Claude",executionStatus:"running",executionSource:"hook"},
    {id:"s1",key:"Claude:s1",provider:"Codex",executionStatus:"running",executionSource:"hook"}]});
  assert.equal(malicious.changed,false,"missing IDs and conflicting provider/key rows are rejected");
  assert.equal(guarded.providers[1].chats[0].executionStatus,undefined,"a conflicting Codex claim cannot update Claude:s1");

  const liveOnly=snapshotOf({codex:[codexChat("c1")],claude:[claudeChat("s1")]});
  const sessionId="44444444-4444-4444-8444-444444444444";
  T.applyLive(liveOnly,{generatedAt:ago(0),chats:[{id:sessionId,key:"Claude:"+sessionId,provider:"Claude",status:"needs-input",pendingQuestion:"Resume?",pendingOptions:["yes"],resumable:true,sessionId}]});
  T.render(liveOnly);
  const inserted=T.buildForest(liveOnly.providers.filter(provider=>provider.name==="Claude")).rows.find(chat=>chat.id===sessionId);
  assert.equal(inserted.resumable,true);assert.equal(inserted.sessionId,sessionId);
  assert.equal(T.runwayWaiting(inserted),true,"the inserted Claude session remains a waiting question");
  assert.match(T.runwayQuestionPopover(inserted),/claude --resume 44444444-4444-4444-8444-444444444444/,"live-only metadata enables the resume control");
  const invalidated=snapshotOf({codex:[codexChat("c1")],claude:[claudeChat("s1",{resumable:true,sessionId:"55555555-5555-4555-8555-555555555555"})]});
  T.applyLive(invalidated,{generatedAt:ago(0),chats:[{id:"s1",key:"Claude:s1",provider:"Claude",resumable:false,sessionId:null}]});
  assert.equal(invalidated.providers[1].chats[0].resumable,false,"live false invalidates resume consent");
  assert.equal(invalidated.providers[1].chats[0].sessionId,null,"live null invalidates the prior session ID");
  // A provider hidden by policy neither merges nor is discovered.
  const hidden=snapshotOf({codex:[codexChat("c1")],claude:[claudeChat("s1")],policy:policies(true,false)});
  const restricted=T.applyLive(hidden,live);
  assert.equal(hidden.providers.some(provider=>provider.name==="Claude"),false);
  assert.ok(Array.from(restricted.outside).every(([key])=>key.startsWith("Codex:")),"policy-hidden providers are not discovered");
}

console.log("Branchglance source switch tests passed");
