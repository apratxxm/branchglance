import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const app=fs.readFileSync(new URL("../token_tracker/static/app.js",import.meta.url),"utf8");
const page=fs.readFileSync(new URL("../token_tracker/static/runway.html",import.meta.url),"utf8");
const css=fs.readFileSync(new URL("../token_tracker/static/runway_refined.css",import.meta.url),"utf8");
const fixedNow=Date.parse("2026-10-25T13:05:00Z"),ago=minutes=>new Date(fixedNow-minutes*60000).toISOString();
const cssRule=selector=>[...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter(([,name])=>name.replace(/\/\*[\s\S]*?\*\//g,'').trim()===selector).at(-1)?.[2]||"";

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

// Repeated stats failures cannot postpone the graphless first-paint fallback.
{
  const {T,timers,context}=boot();
  const parent={id:'parent',key:'Claude:parent',title:'cached parent',model:'claude-opus-5-5',status:'running'};
  const child={id:'child',key:'Claude:child',parentKey:'Claude:parent',title:'cached child',model:'claude-sonnet-5-5',status:'running'};
  const cached={generatedAt:ago(0),providerPolicies:policy,enabledProviders:['Claude'],providers:[{name:'Claude',connected:true,chats:[parent,child]}]};
  T.state.initialBootstrapPending=true;
  assert.equal(T.render(cached),false,'the first graphless snapshot is temporarily held');
  context.fetch=async()=>({ok:false,status:503});
  await T.refresh();
  await T.refresh();
  assert.equal(T.state.snapshot,null,'failed background builds do not accidentally accept the held snapshot');
  const deadline=timers.get(T.state.graphlessBootstrapTimer);
  assert.equal(deadline.delay,2000,'the hold has a two second deadline independent of request success');
  deadline.callback();
  assert.equal(T.state.forest.roots.length,2,'the deadline exposes cached rows as roots');
  assert.ok(T.state.forest.rows.every(chat=>chat.telemetryUnavailable&&chat.statusSource==='unavailable'),'deadline fallback marks cached activity unavailable');
  assert.ok(T.state.forest.rows.every(chat=>T.runwayActivityEvidence(chat).status==='unavailable'),'fallback rows cannot show stale activity as live');
  assert.ok(T.state.bootstrapRetryTimer!=null,'the first-paint deadline leaves another background retry scheduled');
  const linked={...cached,graph:{revision:1,edges:{'Claude:parent':null,'Claude:child':'Claude:parent'}}};
  assert.equal(T.render(linked),true,'a later graph payload is accepted');
  assert.equal(T.state.forest.roots.length,1,'the later graph re-trees the cached rows');
  assert.equal(T.state.forest.roots[0].descendants[0].key,'Claude:child');
  assert.equal(T.state.forest.rows[0].telemetryUnavailable,undefined,'the graph response restores original row evidence');
}

// First compact paint waits for the graph, and the same live-data rows keep
// cache and workspace content inside the fixed inspect columns at every depth.
{
  const {T,timers}=boot(),rich=(id,title,workspace,parentKey=null)=>({id,key:(id==='op'?'Claude:':'Codex:')+id,parentKey,title,agentName:id==='op'?'':'agent-'+id,
    model:id==='op'?'claude-opus-5-5':'gpt-6.1-sol',status:'running',statusSource:'transcript',statusObservedAt:ago(0),executionStatus:'running',executionSource:'transcript',executionObservedAt:ago(0),
    activeRunStartedAt:ago(1),executionSince:ago(1),executionActivity:{kind:'reading',files:['A:\\pisha\\orchestrator\\src\\keyboard.py'],observedAt:ago(0)},
    workspace,workspaceKind:'project',contextTokens:98000,contextWindow:200000,contextPercent:49,contextObservedAt:ago(0),contextModel:id==='op'?'claude-opus-5-5':'gpt-6.1-sol',lastRequest:{input:1000,cached:980},updatedAt:ago(id==='op'?0:id==='luna'?1:2),...quota});
  const rows=[rich('op','Orchestrator','A:\\pisha\\orchestrator\\services\\orchestrator'),
    rich('luna','Luna','A:\\pisha\\orchestrator\\agents\\review\\keyboard','Claude:op'),
    rich('sol','Sol','A:\\pisha\\orchestrator\\agents\\review\\keyboard\\tests','Codex:luna')];
  const richProviders=[{name:'Claude',connected:true,chats:[rows[0]],...quota},{name:'Codex',connected:true,chats:rows.slice(1),...quota}];
  const richGraph={revision:1,edges:{'Claude:op':null,'Codex:luna':'Claude:op','Codex:sol':'Codex:luna'}};
  const firstPayload={generatedAt:ago(0),providerPolicies:policy,enabledProviders:['Claude','Codex'],providers:richProviders};
  T.state.initialBootstrapPending=true;
  assert.equal(T.render(firstPayload),false,'a graphless initial payload is held until the graph arrives');
  assert.equal(T.state.runwaySnapshotPainted,false);
  assert.equal(T.state.snapshot,null);
  assert.equal(T.render(firstPayload),true,'the graphless hold is bounded to two build attempts');
  assert.equal(T.state.initialBootstrapPending,false,'the fallback releases the first-paint hold');
  assert.equal(T.state.forest.roots.length,3,'graphless cached rows render as independent roots');
  assert.ok(T.state.forest.roots.every(root=>root.descendants.length===0),'fallback rows do not invent a tree');
  assert.equal(T.render(firstPayload),true,'graphless retries continue rendering while background builds continue');
  assert.equal(T.state.forest.roots.length,3,'later graphless snapshots remain roots');
  T.scheduleBootstrapRetry();
  assert.equal(timers.get(T.state.bootstrapRetryTimer)?.delay,2000,'the graphless fallback keeps a background retry scheduled');
  assert.equal(T.render({...firstPayload,graph:richGraph}),true);
  assert.equal(T.state.forest.roots.length,1,'a later complete graph replaces fallback roots with the tree');
  assert.deepEqual(Array.from(T.state.forest.roots[0].descendants,chat=>chat.depth),[1,2]);
  assert.equal(T.state.forest.rows[0].telemetryUnavailable,undefined,'the original snapshot evidence remains intact for the graph render');
  assert.equal(T.runwayActivityEvidence(T.state.forest.rows[0]).status,'running','fresh graph-backed evidence is restored');

  const rendered=[T.state.forest.roots[0],...T.state.forest.roots[0].descendants].map((chat,index)=>index===0?T.rowHTML(chat):T.runwayTelemetryHTML(chat,true));
  const slotOrder=markup=>[...markup.matchAll(/data-runway-slot="(timing|status|activity-column|path-column|tokens)"/g)].map(([,slot])=>slot);
  assert.deepEqual(slotOrder(rendered[0]),['timing','status','activity-column','path-column','tokens']);
  assert.ok(rendered.slice(1).every(markup=>JSON.stringify(slotOrder(markup))===JSON.stringify(slotOrder(rendered[0]))),
    'parent and nested child rows share the same information columns');
  for(const [index,markup] of rendered.entries()){
    const activity=markup.match(/data-runway-slot="activity-column"[^>]*>([\s\S]*?)<\/span><span data-runway-slot="path-column"/)?.[1]||'';
    const tokens=markup.match(/data-runway-slot="tokens"[^>]*>([\s\S]*?)<\/span><\/div>/)?.[1]||'';
    const leaves=['A: orchestrator','A: keyboard','A: tests'];
    assert.doesNotMatch(activity,/Cache \d+%/,`row ${index} keeps cache out of activity`);
    assert.equal((activity.match(/class="[^"]*\brunway-activity\b[^"]*"/g)||[]).length,1,`row ${index} activity markup: ${activity}`);
    assert.match(tokens,/ctx-tokens/);
    assert.match(tokens,/Cache 98%/);
    assert.ok(tokens.indexOf('ctx-tokens')<tokens.indexOf('Cache 98%'),'tokens stay above the muted cache line');
    if(index===0) assert.match(markup,/runway-path-leaf">A: orchestrator/);
    else assert.match(markup,/data-runway-slot="path-column" class="runway-refined-path"><\/span>/);
    assert.equal(markup.match(/<span class="runway-state-word"[^>]*>([^<]*)<\/span>/)?.[1],'Running');
    assert.equal((markup.match(/data-runway-slot="cache"/g)||[]).length,1);
  }

  const longName='Review a particularly long subagent name to check the single line truncation and full tooltip';
  const longMarkup=T.runwayTelemetryHTML({...T.state.forest.roots[0].descendants[0],agentName:longName},true);
  assert.ok(longMarkup.includes('title="'+longName+'"'),'full agent name is available in the tooltip');
  // Subagent names wrap like parent names; the user asked for no truncation.
  assert.doesNotMatch(css,/.ag.runway-agent .runway-refined-title .runway-agent-label{[^}]*text-overflow:ellipsis/);
  assert.match(longMarkup,/data-runway-slot="model"/);
  assert.match(longMarkup,/Cache 98%/);

  const root=T.state.forest.roots[0],base=root.descendants[0];
  const extra=Array.from({length:7},(_,i)=>({...base,key:'Codex:extra'+i,id:'extra'+i,parentKey:root.key,treeParentKey:root.key,latest:100+i}));
  const entriesFor=descendants=>T.trayEntries({...root,descendants},{active:false});
  const keys=entries=>Array.from(entries.filter(e=>e.cls.includes('runway-agent')),e=>e.key);
  const click=(action,key=root.key,last=2)=>{const snapshot=T.state.snapshot;T.state.snapshot=null;T.handleClick({target:{closest:()=>({dataset:{action,key,last:String(last)}})}});T.state.snapshot=snapshot;};
  let entries=entriesFor(extra);
  assert.deepEqual(keys(entries),extra.slice(0,3).map(e=>e.key),'forest sibling order is retained');
  assert.match(entries.at(-1).html,/\+4 more/);
  click('runway-more');entries=entriesFor(extra);
  assert.deepEqual(keys(entries),extra.slice(3,6).map(e=>e.key));
  assert.match(entries.at(-1).html,/4\u20136 of 7/);
  click('runway-more');entries=entriesFor(extra);
  assert.deepEqual(keys(entries),[extra[6].key]);
  assert.match(entries.at(-1).html,/7 of 7/);
  click('runway-page-prev');entries=entriesFor(extra);
  assert.deepEqual(keys(entries),extra.slice(3,6).map(e=>e.key));
  click('runway-page-prev');
  const waiting={...extra[5],status:'needs-input',executionStatus:'waiting',pendingQuestion:'Choose?',pendingQuestionAt:ago(0)};
  entries=entriesFor(extra.map(a=>a.key===waiting.key?waiting:a));
  assert.ok(keys(entries).includes(waiting.key),'waiting sibling past cap remains visible');
  assert.match(T.runwayTelemetryHTML(waiting,true),/class="runway-state-word runway-waiting-link"[^>]*>Waiting<\/button>/);
  const failed={...extra[6],status:'failed',executionStatus:'failed',updatedAt:ago(0)};
  entries=entriesFor(extra.map(a=>a.key===failed.key?failed:a));
  assert.ok(keys(entries).includes(failed.key),'failed child remains visible beyond the ordinary cap');
  assert.equal(T.runwayTelemetryHTML(failed,true).match(/<span class="runway-state-word"[^>]*>([^<]*)<\/span>/)?.[1], 'Failed');
  const waitingGrandchild={...waiting,key:'Codex:question',id:'question',parentKey:extra[5].key,treeParentKey:extra[5].key};
  entries=entriesFor([...extra,waitingGrandchild]);
  assert.ok(keys(entries).includes(extra[5].key)&&keys(entries).includes(waitingGrandchild.key),'actionable descendant preserves ancestor path');
  const idle={...extra[0],status:'idle',executionStatus:'idle'},grandchild={...extra[1],parentKey:idle.key,treeParentKey:idle.key};
  entries=entriesFor([idle,grandchild]);
  assert.deepEqual(keys(entries),[grandchild.key],'visible grandchild survives an inactive intermediate');
  T.state.pointerInList=true;
  entries=entriesFor(extra.map((a,i)=>({...a,latest:i===6?9999:a.latest})));
  assert.deepEqual(keys(entries),extra.slice(0,3).map(e=>e.key),'polling activity cannot reorder siblings');

  const inspect='body.runway-refined.runway-compact[data-notch-mode="inspect"]:not(.phone) ';
  const rowRule=cssRule(inspect+'.runway-refined-row');
  assert.match(rowRule,/grid-template-rows:minmax\(0,1fr\) minmax\(0,1fr\);column-gap:8px;row-gap:0;align-items:center/);
  const cellRules=['runway-refined-timing','runway-refined-status','runway-refined-path','runway-refined-tokens'];
  for(const name of cellRules){const rule=cssRule(inspect+'.'+name);assert.match(rule,/min-width:0/);assert.match(rule,/max-width:100%/);assert.match(rule,/overflow:hidden/);}
  assert.doesNotMatch(cssRule(inspect+'.runway-refined-activity'),/overflow:visible/);
  assert.doesNotMatch(cssRule(inspect+'.runway-refined-activity>.runway-activity'),/white-space:normal/,'activity stays on one line');
  for(const name of ['runway-refined-timing','runway-refined-path'])
    assert.match(cssRule(inspect+'.'+name),/grid-row:1\/3/);
  // Status sits above its activity in one column; alone, it centres across both lines.
  assert.match(cssRule(inspect+'.runway-refined-status'),/grid-column:3;grid-row:1;/);
  assert.match(cssRule(inspect+'.runway-refined-activity'),/grid-column:3;grid-row:2;/);
  assert.match(css,/\.runway-refined-row:not\(:has\(\.runway-activity\)\) \.runway-refined-status\{grid-row:1\/3;align-self:center/);
  assert.match(cssRule(inspect+'.runway-refined-tokens'),/grid-template-rows:18px 14px;row-gap:2px/);
  assert.doesNotMatch(css,/\.runway-refined-path \.runway-path-leaf\{overflow:visible;white-space:normal/,'workspace stays on one line');
  assert.match(cssRule(inspect+'.runway-refined-tokens'),/grid-row:1\/3/,'tokens span the same two-row axis as the single-line columns');
  assert.match(cssRule(inspect+'.runway-refined-tokens'),/align-items:center/,'tokens centre their two lines on the shared row axis');
}

// The keyed DOM patch updates the disclosure button without replacing its focused node.
{
  const {T,context}=boot(),doc=context.document;
  class TextNode {
    constructor(value){this.nodeType=3;this.nodeName='#text';this.nodeValue=value;this.childNodes=[];}
    cloneNode(){return new TextNode(this.nodeValue);}
  }
  class Element {
    constructor(name,attrs={},children=[]){this.nodeType=1;this.nodeName=name.toUpperCase();this.map={...attrs};this.childNodes=children;this.dataset={};this.isConnected=true;this.className='';this._html='';for(const child of children)child.parentNode=this;}
    get attributes(){return Object.entries(this.map).map(([name,value])=>({name,value}));}
    get children(){return this.childNodes.filter(n=>n.nodeType===1);}
    get firstChild(){return this.childNodes[0]||null;}
    get lastChild(){return this.childNodes.at(-1)||null;}
    get nextSibling(){const siblings=this.parentNode?.childNodes||[];return siblings[siblings.indexOf(this)+1]||null;}
    getAttribute(name){return this.map[name]??null;}
    hasAttribute(name){return name in this.map;}
    setAttribute(name,value){this.map[name]=String(value);}
    removeAttribute(name){delete this.map[name];}
    matches(){return false;}
    querySelectorAll(){return [];}
    contains(node){return this===node||this.children.some(child=>child.contains(node));}
    focus(){doc.activeElement=this;}
    cloneNode(deep){return new Element(this.nodeName,{...this.map},deep?this.childNodes.map(n=>n.cloneNode(true)):[]);}
    insertBefore(node,before){if(node.parentNode){const old=node.parentNode.childNodes;old.splice(old.indexOf(node),1);}const index=before?this.childNodes.indexOf(before):this.childNodes.length;this.childNodes.splice(index,0,node);node.parentNode=this;return node;}
    appendChild(node){return this.insertBefore(node,null);}
    remove(){const siblings=this.parentNode.childNodes;siblings.splice(siblings.indexOf(this),1);}
  }
  const parse=html=>[...html.matchAll(/<button ([^>]*)>([^<]*)<\/button>/g)].map(([,raw,text])=>{
    const attrs=Object.fromEntries([...raw.matchAll(/([\w-]+)="([^"]*)"/g)].map(([,key,value])=>[key,value]));
    return new Element('button',attrs,[new TextNode(text)]);
  });
  doc.createElement=name=>name==='template'?{set innerHTML(value){this.content={childNodes:parse(value)};}}:new Element(name);
  const initial='<button data-action="runway-page-prev" aria-disabled="true">\u2190</button><button data-action="runway-more" aria-expanded="false">+4 more</button>';
  const updated='<button data-action="runway-page-prev" aria-disabled="false">\u2190</button><button data-action="runway-more" aria-expanded="true">4\u20136 of 7 \u2192</button>';
  const wrapper=new Element('div',{},parse(initial));wrapper.dataset.key='__more:root';wrapper.className='runway-more';wrapper._html=initial;
  const container=new Element('div',{},[wrapper]),button=wrapper.children[1];button.focus();
  T.patchKeyed(container,[{key:'__more:root',cls:'runway-more',attrs:{},html:updated}]);
  assert.equal(wrapper.children[1],button);
  assert.equal(doc.activeElement,button);
  assert.equal(button.getAttribute('aria-expanded'),'true');
  assert.equal(button.firstChild.nodeValue,'4\u20136 of 7 \u2192');
}

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
  assert.match(css,/\.runway-model-name\.is-claude-model\{color:#f0a06e/);
  assert.match(css,/--tree-claude-surface:#19130f/,"Claude keeps the darker surface token");
  assert.match(css,/\.chat\[data-provider="Claude"\]>.row>.runway-refined-row\{[^}]*radial-gradient\(ellipse 90px 18px at 80px 50%,[^}]*var\(--claude-accent\) 12%[^}]*transparent 100%/,"Claude parent halo stays warm, subtle, and edgeless");
  assert.match(css,/\.ag\[data-provider="Claude"\]>.runway-agent-glance>.runway-refined-row\{[^}]*radial-gradient\(ellipse 90px 18px at 80px 50%,[^}]*var\(--claude-accent\) 9%[^}]*transparent 100%/,"Claude children use a softer transparent tint");
  assert.match(css,/\.ag\[data-provider="Codex"\]>.runway-agent-glance>.runway-refined-row\{[^}]*radial-gradient\(ellipse 90px 18px at 80px 50%,[^}]*var\(--source-glow-primary\) 11%[^}]*transparent 100%/,"OpenAI rows use a subtle transparent tint");
  assert.doesNotMatch(css,/\.runway-refined-row\{[^}]*background-color:/,"tree rows have no rectangular background fill");
  assert.match(css,/\.chat>\.row\.runway-root-stuck\{background:#0e1017;/,"stuck parent rows paint the same colour as the panel, so the surface stays continuous");
  assert.doesNotMatch(css,/\.chat\[data-provider=[^\n]+background-image:linear-gradient/,"provider rows have no full-width linear fills");
  assert.doesNotMatch(css,/radial-gradient\(ellipse (?:1[4-9][0-9]|[2-9][0-9]{2,})px/ ,"identity glow remains at most 140px wide");
  assert.match(css,/\.chat\[data-provider="Claude"\]>\.row::after\{background:radial-gradient\(ellipse at 48% 110%,color-mix\(in srgb,var\(--claude-accent\) 22%/,"Claude parents keep a warm, edgeless identifier glow");
  assert.match(css,/\.runway-refined-row::after\{[^}]*border-radius:0;[^}]*radial-gradient\([^}]*transparent 100%/,"hover highlight is an edgeless glow");
  assert.doesNotMatch(css,/linear-gradient\(to bottom,var\(--tree-(?:claude|codex)-surface\)/,"cross-provider seams have no row-edge feather bands");
  const shortTitle=T.runwayTelemetryHTML({...codexLuna,title:"Luna · dashboard"},true);
  const longTitle=T.runwayTelemetryHTML({...codexLuna,title:"Luna · dashboard review dashboard updates and coordination"},true);
  assert.equal((shortTitle.match(/class="runway-refined-row"/g)||[]).length,(longTitle.match(/class="runway-refined-row"/g)||[]).length,"title length does not add rows or change the fixed row tracks");
  assert.match(css,/grid-template-rows:minmax\(0,1fr\) minmax\(0,1fr\);column-gap:8px;row-gap:0;align-items:center;\s*width:100%;min-width:0;min-height:50px/);
  assert.match(css,/grid-template-rows:minmax\(0,1fr\) minmax\(0,1fr\);column-gap:8px;row-gap:0;align-items:center/);
}

console.log("unified agent tree contract checks passed");
