import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const app=fs.readFileSync(new URL("../token_tracker/static/app.js",import.meta.url),"utf8");
const page=fs.readFileSync(new URL("../token_tracker/static/runway.html",import.meta.url),"utf8");
const css=fs.readFileSync(new URL("../token_tracker/static/runway_refined.css",import.meta.url),"utf8");
const fixedNow=Date.parse("2026-10-25T13:05:00Z"),ago=minutes=>new Date(fixedNow-minutes*60000).toISOString();

function boot(){
  const nodes=new Map(),timers=new Map();let sequence=0;
  const node=selector=>{
    if(!nodes.has(selector))nodes.set(selector,{innerHTML:"",textContent:"",value:"",hidden:false,inert:false,title:"",className:"",dataset:{},attributes:{},style:{setProperty(key,value){this[key]=value;}},
      classList:{add(){},remove(){},toggle(){}},querySelector(){return null;},querySelectorAll(){return [];},contains(){return false;},setAttribute(key,value){this.attributes[key]=String(value);},getAttribute(key){return this.attributes[key]??null;},removeAttribute(key){delete this.attributes[key];},
      addEventListener(){},removeEventListener(){},focus(){},blur(){},getClientRects(){return [];}});
    return nodes.get(selector);
  };
  class TestDate extends Date{constructor(...args){super(...(args.length?args:[fixedNow]));}static now(){return fixedNow;}}
  const context={window:{__TOKEN_TRACKER_TEST__:true,__TOKEN_TRACKER_RUNWAY__:true,__TOKEN_TRACKER_REFINED__:true,__TOKEN_TRACKER_COMPACT__:true},
    document:{body:{dataset:{},classList:{add(){},remove(){},toggle(){}}},querySelector:selector=>selector==="#source-switch"?null:node(selector),querySelectorAll:()=>[],documentElement:{style:{setProperty(){}}},hidden:false,activeElement:null,addEventListener(){}},
    localStorage:{getItem:()=>null,setItem(){}},fetch:async()=>({ok:true,json:async()=>({})}),Intl,Date:TestDate,Math,Number,JSON,AbortController,AbortSignal,URLSearchParams,
    setTimeout(callback,delay=0){timers.set(++sequence,{callback,delay});return sequence;},clearTimeout(id){timers.delete(id);},setInterval(){return 0;}};
  context.window.addEventListener=()=>{};
  vm.createContext(context);vm.runInContext(app,context);
  return {T:context.window.__tokenTrackerTest,node,nodes,context,timers};
}

const policy={schemaVersion:1,generation:"tree-fixture",providers:[
  {name:"Codex",collect:true,show:true,send:false,capabilities:{}},
  {name:"Claude",collect:true,show:true,send:false,capabilities:{}}]};
const quota={fiveHour:{percent:42,resetAt:fixedNow/1000+5400},weekly:{percent:31,resetAt:fixedNow/1000+190000},limitsUpdatedAt:ago(1)};
const claudeParent={id:"op",key:"Claude:op",parentKey:null,title:"Branchglance lead",model:"claude-opus-5-5",status:"running",updatedAt:ago(0),workspace:"D:\\work\\sample-app",...quota};
const codexLuna={id:"luna",key:"Codex:luna",parentKey:"Claude:op",title:"dashboard pass",agentName:"luna",model:"gpt-6-luna",status:"running",updatedAt:ago(1),workspace:"D:\\work\\sample-app",...quota};
const codexSol={id:"sol",key:"Codex:sol",parentKey:"Codex:luna",title:"review pass",agentName:"sol",model:"gpt-6.1-sol",status:"running",updatedAt:ago(2),workspace:"D:\\work\\sample-app",...quota};
const claudeHaiku={id:"haiku",key:"Claude:haiku",parentKey:"Claude:op",title:"test pass",agentName:"haiku",model:"claude-haiku-5-5",status:"running",updatedAt:ago(3),workspace:"D:\\work\\sample-app",...quota};
const providers=[{name:"Codex",connected:true,chats:[codexLuna,codexSol],...quota},{name:"Claude",connected:true,chats:[claudeParent,claudeHaiku],...quota}];
const graph={revision:7,edges:{"Claude:op":null,"Codex:luna":"Claude:op","Codex:sol":"Codex:luna","Claude:haiku":"Claude:op"}};

// Mixed provider lineage uses canonical keys and the graph across provider boundaries.
{
  const {T,node}=boot(),forest=T.buildForest(providers,graph);
  assert.deepEqual(Array.from(forest.roots,chat=>chat.key),["Claude:op"]);
  assert.deepEqual(Array.from(forest.roots[0].descendants,chat=>chat.key),["Codex:luna","Codex:sol","Claude:haiku"]);
  assert.deepEqual(Array.from(forest.roots[0].descendants,chat=>chat.depth),[1,2,1]);
  const snapshot={generatedAt:ago(0),providerPolicies:policy,enabledProviders:["Codex","Claude"],providers,graph};
  T.render(snapshot);
  const rootHTML=T.rowHTML(T.state.forest.rows.find(chat=>chat.key==="Claude:op"));
  assert.match(rootHTML,/Opus 5\.5/);
  assert.doesNotMatch(rootHTML,/Claude 5\.5 Opus/);
  assert.match(T.rowHTML(T.state.forest.rows.find(chat=>chat.key==="Codex:luna")),/GPT-6 Luna/);
  assert.match(T.rowHTML(T.state.forest.rows.find(chat=>chat.key==="Codex:sol")),/GPT-6\.1 Sol/);
  assert.match(T.rowHTML(T.state.forest.rows.find(chat=>chat.key==="Claude:haiku")),/Haiku 5\.5/);
  assert.match(node("#chat-list").innerHTML,/data-provider="Claude"/);
  assert.match(node("#chat-list").innerHTML,/data-provider="Codex"/);
}

// Explicit null removes a stale row parent; a cycle is broken into safe roots.
{
  const removed=T=>T.buildForest(providers,{revision:8,edges:{...graph.edges,"Codex:luna":null}});
  const forest=removed(boot().T);
  assert.ok(forest.roots.some(chat=>chat.key==="Codex:luna"),"graph null overrides the stale row parentKey");
  assert.ok(forest.roots.find(chat=>chat.key==="Codex:luna").descendants.some(chat=>chat.key==="Codex:sol"));
  const cyclic=boot().T.buildForest(providers,{revision:9,edges:{"Claude:op":"Codex:luna","Codex:luna":"Claude:op","Codex:sol":"Codex:luna","Claude:haiku":"Claude:op"}});
  assert.ok(cyclic.roots.some(chat=>chat.key==="Claude:op"));
  assert.ok(cyclic.roots.some(chat=>chat.key==="Codex:luna"));
  assert.ok(cyclic.rows.every(chat=>Number.isFinite(chat.depth)||chat.depth===undefined));
  const liveBoot=boot(),snapshot={generatedAt:ago(1),providerPolicies:policy,providers:providers.map(provider=>({...provider,chats:provider.chats.map(chat=>({...chat}))})),graph};
  const liveResult=liveBoot.T.applyLive(snapshot,{generatedAt:ago(0),chats:[],graph:{revision:8,edges:{...graph.edges,"Codex:luna":null}}});
  assert.equal(liveResult.changed,true,"a newer live graph revision patches the full snapshot");
  assert.equal(snapshot.graph.revision,8);
  assert.ok(liveBoot.T.buildForest(snapshot.providers,snapshot.graph).roots.some(chat=>chat.key==="Codex:luna"));
}

// Legacy snapshots without graph edges still use provider-local parentId values.
{
  const local=boot().T.buildForest([{name:"Claude",chats:[{...claudeParent,key:undefined,parentKey:undefined},{id:"local",parentId:"op",title:"local child",model:"claude-sonnet-5-5",status:"running"}]}]);
  assert.equal(local.roots.length,1);
  assert.equal(local.roots[0].descendants[0].key,"Claude:local");
  const unlinked=boot().T.buildForest([{name:"Codex",chats:[{id:"u1",title:"unlinked one",model:"gpt-6-luna"},{id:"u2",title:"unlinked two",model:"gpt-6.1-sol"}]}]);
  assert.deepEqual(Array.from(unlinked.roots,chat=>chat.key),["Codex:u1","Codex:u2"]);
}

// A graph containing only cross-provider edges must preserve native Claude subagent lineage.
{
  const {T}=boot(),parent={id:"k1",key:"Claude:k1",title:"Orchestrator",model:"claude-opus-5-5",status:"running",updatedAt:ago(0)},
    explore={id:"explore",key:"Claude:explore",parentId:"k1",title:"Explore",model:"claude-haiku-5-5",status:"running",updatedAt:ago(1)},
    c1={id:"c1",key:"Codex:c1",title:"Luna",model:"gpt-6-luna",status:"running",updatedAt:ago(2)},
    c2={id:"c2",key:"Codex:c2",title:"Sol",model:"gpt-6.1-sol",status:"running",updatedAt:ago(3)},
    graphOnlyCrossLinks={revision:2,edges:{"Codex:c1":"Claude:k1","Codex:c2":"Claude:k1"}};
  assert.equal(Object.hasOwn(explore,"parentKey"),false,"the regression fixture has native parentId only");
  const forest=T.buildForest([{name:"Claude",chats:[parent,explore]},{name:"Codex",chats:[c1,c2]}],graphOnlyCrossLinks);
  assert.deepEqual(Array.from(forest.roots,chat=>chat.key),["Claude:k1"]);
  assert.deepEqual(Array.from(forest.roots[0].descendants,chat=>chat.key),["Claude:explore","Codex:c1","Codex:c2"]);

  const snapshot={generatedAt:ago(1),providerPolicies:policy,providers:[{name:"Claude",connected:true,chats:[parent]},{name:"Codex",connected:true,chats:[c1,c2]}],graph:graphOnlyCrossLinks};
  const live={generatedAt:ago(0),chats:[{id:"explore",key:"Claude:explore",provider:"Claude",parentId:"k1",title:"Explore",model:"claude-haiku-5-5",status:"running"}],graph:graphOnlyCrossLinks};
  assert.equal(T.applyLive(snapshot,live).changed,true,"live insertion applies native parentId even with a graph present");
  const liveForest=T.buildForest(snapshot.providers,snapshot.graph);
  assert.equal(liveForest.roots[0].descendants.find(chat=>chat.key==="Claude:explore").treeParentKey,"Claude:k1");
}

// Policy-filtered single-provider payloads paint only their provider's rows and quota band.
{
  const {T,node}=boot();
  const codexOnly={...policy,generation:"codex-only",providers:[{...policy.providers[0]},{...policy.providers[1],collect:false,show:false}]};
  T.render({generatedAt:ago(0),providerPolicies:codexOnly,providers:[{name:"Codex",connected:true,chats:[codexLuna],...quota},{name:"Claude",connected:true,chats:[claudeParent],...quota}]});
  assert.deepEqual(Array.from(T.providersFor(T.state.snapshot),provider=>provider.name),["Codex"]);
  assert.deepEqual(Array.from(T.state.forest.roots,chat=>chat.provider),["Codex"]);
  assert.equal(T.limitProvidersFor(T.state.snapshot).length,1);
  const claudeOnly={...policy,generation:"claude-only",providers:[{...policy.providers[0],collect:false,show:false},{...policy.providers[1]}]};
  T.render({generatedAt:ago(0),providerPolicies:claudeOnly,providers:[{name:"Codex",connected:true,chats:[codexLuna],...quota},{name:"Claude",connected:true,chats:[claudeParent],...quota}]});
  assert.deepEqual(Array.from(T.providersFor(T.state.snapshot),provider=>provider.name),["Claude"]);
  assert.deepEqual(Array.from(T.state.forest.roots,chat=>chat.provider),["Claude"]);
  assert.equal(T.limitProvidersFor(T.state.snapshot).length,1);
  assert.doesNotMatch(node("#chat-list").innerHTML,/Codex:/);
}

// Reset cells have stable provider colors and no visible provider text; the fixed frame stays Codex-purple.
{
  const {T,node,context}=boot();
  T.render({generatedAt:ago(0),providerPolicies:policy,providers,graph});
  assert.match(node("#resets").innerHTML,/rr-fiveHour provider-codex/);
  assert.match(node("#resets").innerHTML,/rr-fiveHour provider-claude/);
  assert.match(node("#weekly-resets").innerHTML,/rr-weekly provider-codex/);
  assert.match(node("#weekly-resets").innerHTML,/rr-weekly provider-claude/);
  assert.doesNotMatch(node("#resets").innerHTML,/class="rr-label">Codex|class="rr-label">Claude/);
  assert.equal(context.document.body.dataset.source,undefined,"the selected dashboard provider never recolors the widget frame");
  assert.equal(T.setSource("Claude"),true);
  assert.equal(context.document.body.dataset.source,undefined);
  assert.equal(node("#resets").dataset.providerCount,"2");
  const codexCss=css.match(/\.rr\.provider-codex \.rr-cd\{[^}]+\}/)?.[0]||"";
  const claudeCss=css.match(/\.rr\.provider-claude \.rr-cd\{[^}]+\}/)?.[0]||"";
  assert.match(codexCss,/#[a-f0-9]+/i);assert.match(claudeCss,/#[a-f0-9]+/i);
}

// 12-hour local time, removed source switch, and the shared five information-column axes.
{
  const {T,node}=boot();
  T.syncCompactClock();
  assert.match(node("#toolbar-clock").textContent,/\d{1,2}:\d{2} (AM|PM)/);
  assert.match(node("#compact-clock").textContent,/\d{1,2}:\d{2} (AM|PM)/);
  assert.doesNotMatch(page,/id="source-switch"|data-action="source"/);
  assert.match(page,/id="brand-name">Branchglance/);
  const template=css.match(/\.runway-refined-row\{\s*grid-template-columns:([^;]+);/)?.[1];
  assert.ok(template,"the inspect row declares shared tracks");
  const minimums=[120,46,82,70,80,78],gap=8,padLeft=30,centers=[];let x=padLeft;
  for(const width of minimums){centers.push(x+width/2);x+=width+gap;}
  assert.equal(centers.length,6);
  assert.equal(x-gap+10,556,"the six fixed-minimum tracks fit the inspect viewport exactly");
  for(const selector of ['.runway-refined-timing{','.runway-refined-status{','.runway-refined-activity{','.runway-refined-path{','.runway-refined-tokens{'])assert.ok(css.includes(selector),selector+" uses a shared row track");
  assert.match(css,/\.ag\.runway-agent \.runway-refined-row\{padding-left:30px/,"children keep the exact parent row grid inset");
  assert.match(css,/\.runway-model-name\.is-codex-model\{color:#b3a8ff;text-shadow:/);
  assert.match(css,/\.runway-model-name\.is-claude-model\{color:#ad826f/);
  assert.match(css,/\.chat\[data-provider="Claude"\]>.row>.runway-refined-row,\s*body[^\n]*\.ag\[data-provider="Claude"\].*var\(--claude-accent\)/,"Claude row glow uses the warm Claude accent token");
  assert.match(css,/\.chat\[data-provider="Codex"\].*var\(--source-glow-primary\)/,"OpenAI row glow remains blue-violet");
  assert.match(css,/linear-gradient\(180deg,var\(--tree-row-neutral\) 0%,color-mix\([^;]+var\(--tree-claude-surface\)[^;]+var\(--tree-row-neutral\) 100%\)/,"Claude tint fades into the shared neutral at both row edges");
  assert.match(css,/linear-gradient\(180deg,var\(--tree-row-neutral\) 0%,color-mix\([^;]+var\(--tree-codex-surface\)[^;]+var\(--tree-row-neutral\) 100%\)/,"OpenAI tint fades into the shared neutral at both row edges");
  const shortTitle=T.runwayTelemetryHTML({...codexLuna,title:"Luna · dashboard"},true);
  const longTitle=T.runwayTelemetryHTML({...codexLuna,title:"Luna · dashboard review dashboard updates and coordination"},true);
  assert.equal((shortTitle.match(/class="runway-refined-row"/g)||[]).length,(longTitle.match(/class="runway-refined-row"/g)||[]).length,"title length does not add rows or change the fixed row tracks");
  assert.match(css,/runway-refined-title>\.runway-agent-name\{[^}]*white-space:nowrap[^}]*overflow:hidden[^}]*text-overflow:ellipsis/);
  assert.match(css,/runway-refined-title \.runway-agent-label\{[^}]*overflow:hidden[^}]*text-overflow:ellipsis[^}]*white-space:nowrap/);
  assert.match(css,/grid-template-rows:23px 21px;column-gap:8px;row-gap:2px;align-items:center;\s*width:100%;min-width:0;min-height:50px/);
  assert.match(css,/runway-compact\[data-notch-mode="inspect"\][^\n]+\.runway-refined-title \.runway-agent-label\{[^}]*white-space:nowrap/);
  assert.match(css,/grid-template-rows:23px 21px;column-gap:8px;row-gap:2px;align-items:center/);
}

console.log("unified agent tree contract checks passed");
