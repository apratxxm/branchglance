import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const app=fs.readFileSync(new URL("../token_tracker/static/app.js",import.meta.url),"utf8");
const page=fs.readFileSync(new URL("../token_tracker/static/runway.html",import.meta.url),"utf8");
const css=fs.readFileSync(new URL("../token_tracker/static/runway.css",import.meta.url),"utf8");
const refinedCss=fs.readFileSync(new URL("../token_tracker/static/runway_refined.css",import.meta.url),"utf8");
const dashboardCss=fs.readFileSync(new URL("../token_tracker/static/dashboard.css",import.meta.url),"utf8");
const dashboardScript=fs.readFileSync(new URL("../token_tracker/static/dashboard.js",import.meta.url),"utf8");
const cssRule=selector=>[...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].find(([,name])=>name.replace(/\/\*[\s\S]*?\*\//g,'').trim()===selector)?.[2]||"";
assert.match(page,/window\.__TOKEN_TRACKER_RUNWAY__=true/);
assert.doesNotMatch(page,/Active work|data-action="pin"/);
assert.match(page,/id="hidden-chats-toggle"[^>]*data-action="show-hidden"/);
assert.doesNotMatch(page,/<div class="ch-normal">|class="ch-title"[^>]*>Chats/);
assert.match(page,/<div class="ch-search">[\s\S]*id="hidden-chats-toggle"[^>]*data-action="show-hidden"/);
assert.match(page,/id="sync-age"[^>]*data-action="refresh"[^>]*aria-label="Refresh usage"/);
assert.match(page,/id="monitor-toggle"[^>]*aria-label="Move to next monitor"/);
assert.match(page,/id="dashboard-open"[^>]*data-action="dashboard"[^>]*aria-label="Open Insights dashboard"/);
assert.match(page,/id="dashboard-view"[^>]*aria-label="Insights and Settings dashboard"/);
assert.match(page,/src="dashboard.js"><\/script>\s*<script src="app.js"/);
assert.match(dashboardCss,/data-notch-mode="dashboard"/);assert.match(dashboardCss,/prefers-reduced-motion/);
assert.match(dashboardScript,/31-day totals/,"fallback totals disclose their actual 31-day coverage");assert.match(dashboardScript,/Provider permissions/);
assert.match(page,/id="widget-lock"[^>]*data-action="widget-lock"[^>]*aria-pressed="false"[^>]*aria-label="Lock widget"[^>]*title="Lock widget"/);
assert.match(page,/id="widget-lock"[^>]*><svg class="ic" viewBox="0 0 12 12" aria-hidden="true"><rect x="2\.5" y="5" width="7" height="5\.5" rx="1"\/><path d="M4 5V3\.8a2 2 0 0 1 3\.6-1\.2"\/><\/svg>/,"the initial lock button shows the unlocked icon");
assert.doesNotMatch(page,/<header class="titlebar pywebview-drag-region"/);
assert.match(page,/<span id="notch-counts"/);
assert.doesNotMatch(page,/id="notch-toggle"|aria-controls="chat-list"/);
assert.doesNotMatch(page,/<footer id="totals"/);
assert.doesNotMatch(page,/Greeter status unavailable/);
assert.match(cssRule('.layout'),/overflow-y:auto/);assert.match(cssRule('.layout'),/overflow-x:hidden/);
assert.match(cssRule('.chat-list'),/flex:none/);assert.match(cssRule('.chat-list'),/overflow:visible/);
// Quota meters show usage while preserving semantic warning and error colors.
assert.match(cssRule('.meter i'),/background:var\(--source-accent\)/);
assert.match(cssRule('.meter i.t-attn'),/background:var\(--orange\)/);
assert.match(cssRule('.meter i.t-err'),/background:var\(--red\)/);
assert.doesNotMatch(css,/^\.t-live,\.t-attn,\.t-err/m); // telemetry overrides cannot recolor quotas
assert.match(css,/\.rr-left\.last-known[^{}]*\{[^}]*opacity:\.62/);
assert.match(css,/\.meter\.last-known i\{background:var\(--muted\)\}/);
assert.match(refinedCss,/\.meter\.last-known i\{background:var\(--muted\);\}/);
assert.match(refinedCss,/\.rr-left\.last-known i\{display:none;\}/,"the strip keeps the age in the tooltip so it cannot push into the countdown");
assert.match(refinedCss,/\.rr \.rr-left\.last-known b\{color:var\(--muted\);\}/,"a last-known value is grey like its meter");
assert.match(cssRule('.runway-count.t-live'),/color:var\(--green\)/);
assert.match(cssRule('.runway-count.t-attn'),/color:var\(--orange\)/);
assert.match(css,/\.row:has\(\[data-own-waiting=true\]\) \.r-title/);
assert.match(css,/\.row:has\(\[data-child-waiting=true\]\) \.r-title/);
assert.match(css,/\.runway-agent-glance:has\(\[data-own-waiting=true\]\) \.runway-agent-name/);
assert.doesNotMatch(css,/\.row:has\(\.runway-telemetry-state\.t-attn\),\.runway-agent-glance:has\(\.runway-telemetry-state\.t-attn\)/);
assert.match(cssRule('.widget'),/width:100%/);assert.match(cssRule('.widget'),/height:100%/);
assert.match(cssRule('.layout'),/flex:1/);assert.match(cssRule('.layout'),/min-height:0/);
assert.match(cssRule('.runway-count'),/width:48px/);assert.match(cssRule('.notch-counts'),/width:150px/); // Base sizing remains the phone layout.
assert.match(cssRule('body:not(.phone) .notch-counts'),/gap:4px/);assert.match(cssRule('body:not(.phone) .notch-counts'),/width:128px/);
assert.match(cssRule('body:not(.phone) .runway-count'),/min-width:40px/);assert.match(cssRule('body:not(.phone) .count-value'),/min-width:28px/);
assert.match(cssRule('.runway-count-waiting.is-empty'),/visibility:hidden/);
assert.match(cssRule('.titlebar'),/gap:1px/);assert.match(cssRule('.titlebar'),/padding:0 8px/);
assert.match(cssRule('.titlebar'),/height:30px/);assert.match(cssRule('.resets'),/order:1/);assert.match(cssRule('.resets'),/height:66px/);
assert.match(cssRule('.layout'),/order:2/);assert.match(cssRule('.layout'),/scrollbar-width:none/);
assert.doesNotMatch(css,/scrollbar-gutter:\s*stable/);
assert.match(cssRule('body:not(.phone) *'),/scrollbar-width:none/);assert.match(cssRule('body:not(.phone) *::-webkit-scrollbar'),/display:none/);
assert.match(cssRule('.row'),/display:grid/);assert.match(cssRule('.row'),/grid-template-columns:minmax\(0,1fr\) 146px/);assert.match(cssRule('.row'),/grid-template-rows:auto auto/);
assert.match(cssRule('.runway-agent-glance'),/display:grid/);assert.match(cssRule('.runway-agent-glance'),/grid-template-columns:minmax\(0,1fr\) 146px/);assert.match(cssRule('.runway-agent-glance'),/grid-template-rows:auto auto/);
assert.match(cssRule('body:not(.phone) .runway-status-dot'),/fill:currentColor/);
assert.match(cssRule('.r-title'),/flex:0 1 auto/);assert.match(cssRule('.runway-agent-name'),/flex:0 1 auto/);
assert.match(cssRule('.runway-telemetry-model'),/grid-column:1/);assert.match(cssRule('.runway-telemetry-model'),/grid-row:2/);
assert.doesNotMatch(css,/\.runway-path-(?:middle|leaf)\{[^}]*direction:rtl/);
assert.match(cssRule('.runway-telemetry-model'),/min-width:0/);assert.match(cssRule('.runway-telemetry-model'),/width:100%/);assert.match(cssRule('.runway-telemetry-model'),/display:flex/);
assert.match(cssRule('body:not(.phone) .runway-message-slot'),/left:var\(--message-offset-x/);assert.match(cssRule('body:not(.phone) .runway-message-slot'),/top:var\(--message-offset-y/);assert.match(cssRule('body:not(.phone) .runway-message-slot'),/width:18px/);assert.match(cssRule('body:not(.phone) .runway-message-slot'),/height:10px/);
assert.match(css,/body:not\(\.phone\) \.row>\.runway-message-slot\[data-message-positioned=true\],[\s\S]*?body:not\(\.phone\) \.runway-agent-glance>\.runway-message-slot\[data-message-positioned=true\]\{display:inline-flex\}/);
assert.match(cssRule('body:not(.phone) .runway-message-indicator .ic'),/width:9px/);assert.match(cssRule('body:not(.phone) .runway-message-dots i'),/width:2px/);
assert.match(cssRule('.runway-model-name.model-changed'),/animation:model-change 3s/);
assert.match(css,/body:not\(\.phone\) \.runway-row-enter\{animation:runway-row-arrival/);
assert.match(css,/\.runway-state-glyph\.is-waiting/);assert.match(css,/\.runway-state-glyph\.is-compacting/);
assert.match(cssRule('.row'),/padding:7px 6px/);assert.match(cssRule('.r-title'),/font-weight:650/);
assert.match(cssRule('.runway-agent-glance'),/padding:5px 6px 5px 5px/);
assert.match(cssRule('.runway-agent-glance'),/border-radius:7px/);
assert.match(cssRule('body:not(.phone) .runway-agent-name>.runway-name-status'),/flex:0 0 8px/);
assert.match(cssRule('.runway-message-dots i'),/border-radius:50%/);
assert.match(cssRule('body:not(.phone) .chat'),/position:relative/);
assert.match(cssRule('body:not(.phone) .runway-tree-connectors'),/pointer-events:none/);
assert.match(cssRule('body:not(.phone) .runway-tree-connectors path'),/stroke:/);
assert.match(css,/body:not\(\.phone\) \.ag\.runway-agent::before,[\s\S]*?body:not\(\.phone\) \.ag\.runway-agent::after\{content:none\}/);
const cssVars=Object.fromEntries([...css.matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})/gi)].map(([,name,value])=>[name,value]));
const luminance=hex=>{const rgb=hex.match(/[\da-f]{2}/gi).map(channel=>parseInt(channel,16)/255).map(value=>value<=.04045?value/12.92:((value+.055)/1.055)**2.4);return .2126*rgb[0]+.7152*rgb[1]+.0722*rgb[2];};
const contrast=(a,b)=>{const values=[luminance(a),luminance(b)].sort((x,y)=>y-x);return (values[0]+.05)/(values[1]+.05);};
for(const color of [cssVars.ink,cssVars.muted,cssVars.green,cssVars.orange,cssVars.red])for(const surface of [cssVars.paper,cssVars.card,"#282116"])assert.ok(contrast(color,surface)>=4.5,`${color} must meet 4.5:1 on ${surface}`);

const nodes=new Map(),saved=new Map(),scheduledTimers=new Map(),openRequests=[],dragRegionNodes=[],fixedNow=new Date("2026-10-06T12:00:00Z").getTime();let timerSequence=0,testNow=fixedNow;
const fakeSetTimeout=(callback,delay)=>{const id=++timerSequence;scheduledTimers.set(id,{callback,delay});return id;};
const fakeClearTimeout=id=>scheduledTimers.delete(id);
const node=selector=>{
  if(!nodes.has(selector))nodes.set(selector,{innerHTML:"",textContent:"",value:"",hidden:false,title:"",className:"",dataset:{},attributes:{},offsetHeight:0,
    style:{setProperty(k,v){this[k]=v;}},contains(){return false;},querySelectorAll(){return [];},setAttribute(k,v){this.attributes[k]=String(v);},getAttribute(k){return this.attributes[k]??null;},removeAttribute(k){delete this.attributes[k];},
    classList:{toggle(){},add(){},remove(){}},addEventListener(){},removeEventListener(){},hasAttribute(){return true;},focus(){}});
  return nodes.get(selector);
};
class TestDate extends Date {constructor(...args){super(...(args.length?args:[testNow]));}static now(){return testNow;}}
const context={window:{__TOKEN_TRACKER_TEST__:true,__TOKEN_TRACKER_RUNWAY__:true,__TOKEN_TRACKER_REFINED__:true,__TOKEN_TRACKER_COMPACT__:true},document:{querySelector:node,querySelectorAll(selector){return selector==='.pywebview-drag-region'?dragRegionNodes:[];},documentElement:{style:{setProperty(key,value){this[key]=value;}}},hidden:false,activeElement:null},
  localStorage:{getItem:key=>saved.get(key)??null,setItem:(key,value)=>saved.set(key,String(value))},fetch:async url=>{openRequests.push(String(url));return {ok:true,json:async()=>({ok:true})};},Intl,Date:TestDate,Math,Number,JSON,AbortController,AbortSignal,setTimeout:fakeSetTimeout,clearTimeout:fakeClearTimeout,setInterval(){return 0;}};
context.window.addEventListener=()=>{};context.document.addEventListener=()=>{};
vm.createContext(context);vm.runInContext(app,context);
const T=context.window.__tokenTrackerTest,ago=minutes=>new Date(fixedNow-minutes*60000).toISOString();
assert.match(T.runwayTelemetryHTML({title:'Thread',resumeCount:2},false), /class="runway-resume-badge" title="Resumed 2 times">↻2<\/span>/);
assert.doesNotMatch(T.runwayTelemetryHTML({title:'Thread',resumeCount:1},false), /runway-resume-badge/);
assert.doesNotMatch(T.runwayTelemetryHTML({title:'Thread'},false), /runway-resume-badge/);
 assert.equal(T.openDashboard(),true,"the production widget opens the dashboard");
assert.equal(T.closeDashboard(),true,"the dashboard returns to the widget");
assert.equal(T.writeInspectLayoutWidth(500),true);
assert.equal(context.document.documentElement.style['--runway-inspect-clock-axis'],'250px');
T.writeInspectLayoutWidth(556);
T.state.hiddenChats.clear();
assert.equal(node('#notch-counts').innerHTML,'','unknown startup counts remain blank');
T.renderLoading();assert.equal(node('#chat-list').innerHTML,'','desktop startup does not show quota-only loading skeletons');
assert.equal(node('#chats-head').hidden,true);assert.equal(node('#search-toggle').hidden,true);
T.setupNotch();assert.equal(context.window.tokenTrackerInspect(),false,'manual inspection is blocked before a valid active snapshot');
T.setNotch(true);assert.equal(T.notch.expanded,false,'the empty roster cannot expand before active work exists');
const root={id:"root",title:"Live parent",model:"gpt-6.1-sol",contextModel:"gpt-6.1-sol",contextObservedAt:ago(2),status:"running",contextTokens:160000,contextWindow:258400,contextPercent:62,
  runStartedAt:ago(300),activeRunStartedAt:ago(3),updatedAt:ago(1),usageUpdatedAt:ago(2),workspace:"D:\\work\\sample app",lastRequest:{input:1000,cached:800},latestUpdate:"Fix login test",objective:"Refactor parser"};
const child={id:"child",parentId:"root",agentName:"agent-worker",title:"Child worker",model:"gpt-6.1-sol",status:"running",
  contextTokens:10000,contextWindow:258400,runStartedAt:ago(4),updatedAt:ago(1),usageUpdatedAt:ago(4)};
const grandchild={...child,id:"grandchild",parentId:"child",agentName:"agent-worker/nested",title:"Nested worker"};
const done={...child,id:"done",status:"idle"};
const provider={name:"Codex",connected:true,chats:[root,child,grandchild,done],cacheWarmth:{warmSeconds:1800,coldSeconds:5400,learned:true,samples:100},
  fiveHour:{percent:92,resetAt:fixedNow/1000+1500},weekly:{percent:78,resetAt:fixedNow/1000+80000},limitsUpdatedAt:ago(1)};
const snapshot={generatedAt:ago(0),enabledProviders:["Codex"],providers:[provider]};
{
  const rect=(left,top,width=100,height=20)=>({left,top,width,height});
  const geometry=T.runwayTreePathGeometry(rect(0,0,200,120),[
    {key:"Codex:root",provider:"Codex",id:"root",parentKey:null,rowRect:rect(24,0),messageRect:rect(24,0),nameRect:rect(30,5,60,20)},
    {key:"Codex:child",provider:"Codex",id:"child",parentId:"root",parentKey:"Codex:root",rowRect:rect(42,30),messageRect:rect(42,36,120,50),nameRect:rect(47,42,60,40),glanceRect:rect(42,36,120,50)},
    {key:"Codex:grandchild",provider:"Codex",id:"grandchild",parentId:"child",parentKey:"Codex:child",rowRect:rect(60,70),messageRect:rect(60,75,120,50),nameRect:rect(65,82,60,40),glanceRect:rect(60,75,120,50)}
  ]);
  assert.deepEqual(Array.from(geometry.paths,({from,to})=>[from,to]),[["Codex:root","Codex:child"],["Codex:child","Codex:grandchild"]],"paths connect only visible canonical parent and child keys");
  assert.equal(geometry.paths[0].d,"M26 14.5V43.5Q26 51.5 34 51.5H44","the refined root branch curves into the child row after the message lane");
  assert.equal(geometry.paths.at(-1).d,"M44 51.5V83.5Q44 91.5 52 91.5H62","wrapped child names anchor the refined branch at their first-line center");
  assert.equal(geometry.paths.at(-1).endY,91.5);assert.equal(geometry.height,94,"the overlay leaves room past endpoint centers for the radius-1.7 dots");
  assert.equal(geometry.dots.length,3,"adjacent parent and child endpoints are deduplicated into one measured point");
  const treeX=Number(geometry.paths[0].d.match(/^M([\d.]+)/)[1]);
  assert.ok(2+18<treeX&&2+18<geometry.width,"the 18px message lane stays within the widget and left of the branch line");
  const orphan=T.runwayTreePathGeometry(rect(0,0,200,120),[
    {key:"root",provider:"Codex",id:"root",rowRect:rect(24,0),messageRect:rect(24,0),nameRect:rect(30,5,60,20)},
    {key:"grandchild",provider:"Codex",id:"grandchild",parentId:"missing",rowRect:rect(60,70),messageRect:rect(60,75,120,50),nameRect:rect(65,82,60,40),glanceRect:rect(60,75,120,50)}
  ]);
  assert.equal(orphan.paths.length,0,"a filtered or completed intermediate parent does not acquire an inferred link");
  assert.equal(orphan.dots.length,0,"an orphan has no invented decorative endpoint dots");
  const single=T.runwayTreePathGeometry(rect(0,0,200,120),[
    {key:"root",provider:"Codex",id:"root",rowRect:rect(24,0),messageRect:rect(24,0),nameRect:rect(30,5,60,20)},
    {key:"child",provider:"Codex",id:"child",parentId:"root",rowRect:rect(42,30),messageRect:rect(42,36,120,50),nameRect:rect(47,42,60,20),glanceRect:rect(42,36,120,50)}
  ]);
  assert.equal(single.paths.length,1);assert.equal(single.dots.length,2,"a single leaf path ends at its own paired endpoint without an SVG tail");
  const siblings=T.runwayTreePathGeometry(rect(0,0,200,120),[
    {key:"root",provider:"Codex",id:"root",rowRect:rect(24,0),messageRect:rect(24,0),nameRect:rect(30,5,60,20)},
    ...["one","two"].map((id,index)=>({key:id,provider:"Codex",id,parentId:"root",rowRect:rect(42,30+index*35),messageRect:rect(42,36+index*35,120,20),nameRect:rect(47,42+index*35,60,20),glanceRect:rect(42,36+index*35,120,20)}))
  ]);
  assert.equal(siblings.paths.length,2);assert.equal(siblings.dots.length,3,"sibling paths share one parent dot");
}
// Exercise the unkeyed model-name span nested inside a keyed model slot.
class PatchNode {
  constructor(name,attrs={}){this.nodeType=name==="#text"?3:1;this.nodeName=name==="#text"?name:name.toUpperCase();this.nodeValue=name==="#text"?attrs.value:null;this._attrs=name==="#text"?{}:{...attrs};this.childNodes=[];this.parentNode=null;this.reflows=0;this.classList={contains:value=>(this._attrs.class||"").split(/\s+/).includes(value),remove:value=>{const before=this._attrs.class||"";this._attrs.class=before.split(/\s+/).filter(item=>item&&item!==value).join(" ");if(value==='model-changed'&&before.includes(value))this.modelClassRemovals=(this.modelClassRemovals||0)+1;},add:value=>{const parts=(this._attrs.class||"").split(/\s+/).filter(Boolean);if(!parts.includes(value))parts.push(value);this._attrs.class=parts.join(" ");}};}
  get attributes(){return Object.entries(this._attrs).map(([name,value])=>({name,value:String(value)}));}get children(){return this.childNodes.filter(item=>item.nodeType===1);}get firstChild(){return this.childNodes[0]||null;}get firstElementChild(){return this.children[0]||null;}get nextSibling(){if(!this.parentNode)return null;return this.parentNode.childNodes[this.parentNode.childNodes.indexOf(this)+1]||null;}get dataset(){const result={};for(const [name,value] of Object.entries(this._attrs))if(name.startsWith('data-'))result[name.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=value;return result;}get offsetWidth(){if(this._attrs['data-model-change-at'])this.reflows++;return 10;}
  getAttribute(name){return Object.hasOwn(this._attrs,name)?String(this._attrs[name]):null;}hasAttribute(name){return Object.hasOwn(this._attrs,name);}setAttribute(name,value){this._attrs[name]=String(value);}removeAttribute(name){delete this._attrs[name];}matches(selector){return selector==='.runway-model-name.model-changed'&&this.classList.contains('runway-model-name')&&this.classList.contains('model-changed');}
  appendChild(child){return this.insertBefore(child,null);}insertBefore(child,before){if(child.parentNode){const old=child.parentNode.childNodes.indexOf(child);if(old>=0)child.parentNode.childNodes.splice(old,1);}const i=before?this.childNodes.indexOf(before):-1;if(i<0)this.childNodes.push(child);else this.childNodes.splice(i,0,child);child.parentNode=this;return child;}replaceChild(next,current){const i=this.childNodes.indexOf(current);this.childNodes[i]=next;next.parentNode=this;current.parentNode=null;}remove(){if(this.parentNode){const i=this.parentNode.childNodes.indexOf(this);if(i>=0)this.parentNode.childNodes.splice(i,1);this.parentNode=null;}}cloneNode(deep=false){const copy=new PatchNode(this.nodeName,this.nodeType===3?{value:this.nodeValue}:this._attrs);if(deep)for(const child of this.childNodes)copy.appendChild(child.cloneNode(true));return copy;}
}
class PatchFragment extends PatchNode{constructor(){super('#fragment');this.nodeType=11;this.nodeName='#document-fragment';}}
const makePatchFragment=eventAt=>{const fragment=new PatchFragment(),modelWrap=new PatchNode('span',{'data-runway-slot':'model'}),model=new PatchNode('span',{class:'runway-model-name model-changed','data-model-change-at':eventAt});model.appendChild(new PatchNode('#text',{value:'6 Luna'}));modelWrap.appendChild(model);fragment.appendChild(modelWrap);const message=new PatchNode('span',{'data-runway-slot':'message'}),indicator=new PatchNode('span',{class:'runway-message-indicator'}),dots=new PatchNode('span',{class:'runway-message-dots'});for(let i=0;i<3;i++)dots.appendChild(new PatchNode('i'));indicator.appendChild(dots);message.appendChild(indicator);fragment.appendChild(message);return fragment;};
const originalCreateElement=context.document.createElement;context.document.createElement=name=>{if(name!=='template')return null;let content;return {get content(){return content;},set innerHTML(value){content=makePatchFragment(value.match(/data-model-change-at="([^"]+)"/)?.[1]||'0');}};};
const patchRow=new PatchNode('div'),patchHTML=eventAt=>'<span data-runway-slot="model"><span class="runway-model-name model-changed" data-model-change-at="'+eventAt+'">6 Luna</span></span><span data-runway-slot="message"><span class="runway-message-indicator"><span class="runway-message-dots"><i></i><i></i><i></i></span></span></span>';
T.runwayPatchContent(patchRow,patchHTML('100'));const patchModel=patchRow.children[0].firstElementChild,patchMessage=patchRow.children[1],patchIndicator=patchMessage.firstElementChild;
T.runwayPatchContent(patchRow,patchHTML('200'));
assert.equal(patchModel.modelClassRemovals,1,'a child model span restarts when its recorded event changes');assert.equal(patchModel.reflows,1,'model highlight removal is flushed once before reapplying');
assert.equal(patchModel.getAttribute('data-model-change-at'),'200');assert.ok(patchModel.classList.contains('model-changed'));
assert.equal(patchRow.children[1],patchMessage);assert.equal(patchMessage.firstElementChild,patchIndicator,'unrelated message-dot nodes keep their DOM identity during a model update');
T.runwayPatchContent(patchRow,patchHTML('200'));assert.equal(patchModel.modelClassRemovals,1,'unchanged event timestamps do not restart the model highlight');
context.document.createElement=originalCreateElement;
T.render(snapshot);
assert.equal((node('#notch-counts').innerHTML.match(/data-count-slot=/g)||[]).length,3,'known snapshots render three fixed count slots');
assert.match(node('#notch-counts').innerHTML,/data-count-slot="waiting"[^>]*is-empty/,'zero waiting is a known empty fixed slot');
{
  const counts=node('#notch-counts');let writes=0,value=counts.innerHTML;
  Object.defineProperty(counts,'innerHTML',{configurable:true,get(){return value;},set(next){writes++;value=next;}});
  T.render(snapshot);assert.equal(writes,0,'an identical roster does not rewrite count slots');
}
assert.equal(node('#totals').innerHTML,'');assert.equal(node('#totals').hidden,true);
assert.match(node('#sync-age').innerHTML,/svg class="ic"/);
assert.match(node('#sync-age').title,/Last successful refresh at/);
assert.doesNotMatch(node('#sync-age').textContent,/stale|offline|unavailable|—/i);
let html=node("#chat-list").innerHTML;
assert.match(html,/Live parent/);
assert.match(html,/ctx-tokens" title="Context occupancy: 160000 \/ 258400 tokens"/);
assert.doesNotMatch(html,/title="160000 \/ 258400 tokens"/);
assert.doesNotMatch(html,/runway-context-label|>Context</,"desktop quota readings omit the redundant Context label");
assert.match(html,/runway-context-used t-attn">160K/);
assert.match(html,/runway-context-separator"> \/ /);
assert.match(html,/runway-context-total">258K/);
const parentRowMarkup=T.rowHTML(T.state.forest.roots[0]);
assert.match(parentRowMarkup,/class="runway-refined-context"/);
const compactContext=parentRowMarkup.slice(parentRowMarkup.indexOf('class="runway-refined-context">'));
assert.equal(compactContext.replace(/^class="runway-refined-context">/,"").split("Cache")[0].replace(/<[^>]*>/g,""),"160K / 258K");
assert.doesNotMatch(compactContext.replace(/<[^>]*>/g,""),/\btokens?\b/i);
assert.match(html,/data-runway-slot="elapsed" class="runway-refined-elapsed"/);
assert.match(html,/runway-refined-cache">Cache 80%/);
assert.match(html,/runway-refined-status t-live/);
assert.match(html,/runway-refined-context"><span class="ctx-tokens"/);
assert.match(parentRowMarkup,/runway-path-leaf">D: sample app/);
assert.equal((html.match(/data-runway-slot="workspace"/g)||[]).length,1,"the parent workspace is shown once in the refined tree");
assert.equal((html.match(/data-runway-slot="activity"/g)||[]).length,0,"missing execution evidence leaves the reserved activity slot empty");
assert.doesNotMatch(html,/Task:|reading file from assignment|assignment-only work/,"unobserved assignment text is not rendered as task activity");
const slotOrder=['data-runway-slot="tree-anchor"','class="runway-refined-title"','runway-refined-timing','runway-refined-status','runway-refined-activity','runway-refined-path','runway-refined-tokens'].map(slot=>parentRowMarkup.indexOf(slot));
assert.ok(slotOrder.every((index,i)=>index>=0&&(i===0||index>slotOrder[i-1])),"production rows retain the refined telemetry field order");
assert.ok(parentRowMarkup.indexOf('class="runway-refined-timing"')>0);
assert.match(parentRowMarkup,/class="runway-refined-title"[\s\S]*?runway-refined-path/);
assert.equal((html.match(/class="runway-status-dot"/g)||[]).length,3,"parent and active agent rows use distinct state dots");
assert.match(parentRowMarkup,/runway-state-word">Running/);
assert.match(parentRowMarkup,/runway-status-dot[^>]*role="img" aria-label="Running" title="Running"/);
const parentDoing='[tracker]{"task":"Recover RAM for wake training","doing":"Identifying safe cleanup targets","next":"Measure the recovered memory","blocked":null}[/tracker]';
const parentDoingRow=T.rowHTML({...root,activeRunStartedAt:ago(3),latestUpdate:parentDoing,latestUpdateAt:ago(1),activity:null});
assert.doesNotMatch(parentDoingRow,/data-runway-slot="activity"|Activity unavailable|Telemetry unavailable/);
assert.doesNotMatch(parentDoingRow,/Identifying safe cleanup targets|Measure the recovered memory/,"tracker prose is never substituted for observed activity");
const parentToolOverride=T.rowHTML({...root,activeRunStartedAt:ago(3),latestUpdate:parentDoing,latestUpdateAt:ago(1),activity:{kind:"using_tool",detail:"Inspecting measured memory usage",files:[],observedAt:ago(0)}});
assert.match(parentToolOverride,/runway-status-dot[^>]*aria-label="Running".*?title="Using tool"[^>]*>Using tool/s);
assert.doesNotMatch(parentToolOverride,/Inspecting measured memory usage|Identifying safe cleanup targets/);
const waitingParent=T.rowHTML({...root,status:"needs-input",pendingQuestion:"Choose a path",pendingQuestionAt:ago(1),latestUpdate:parentDoing,activity:null});
assert.match(waitingParent,/aria-label="Waiting"/);
assert.doesNotMatch(waitingParent,/Identifying safe cleanup targets/);
const idleParent=T.rowHTML({...root,status:"idle",stateDetail:"completed",runEndedAt:ago(1),latestUpdate:parentDoing,activity:null});
assert.doesNotMatch(idleParent,/Identifying safe cleanup targets|data-runway-slot="activity"/);
const unavailableParent=T.rowHTML({...root,status:"unknown",telemetryUnavailable:true,latestUpdate:parentDoing,activity:null});
assert.doesNotMatch(unavailableParent,/data-runway-slot="activity"|Activity unavailable|Telemetry unavailable/);
assert.doesNotMatch(unavailableParent,/Identifying safe cleanup targets/);
const oldParentDoing=T.rowHTML({...root,activeRunStartedAt:ago(1),latestUpdate:parentDoing,latestUpdateAt:ago(3),activity:null});
assert.doesNotMatch(oldParentDoing,/data-runway-slot="activity"|Activity unavailable|Telemetry unavailable/);
assert.doesNotMatch(oldParentDoing,/Identifying safe cleanup targets/);
const finishedRoot=T.rowHTML({...root,status:'idle',stateDetail:'completed',runEndedAt:ago(2)});
assert.match(finishedRoot,/runway-refined-status t-mute[^>]*>.*?runway-state-word">Finished/);
assert.match(T.rowHTML({...root,status:'idle',stateDetail:null,runEndedAt:null}),/runway-refined-status t-mute[^>]*>.*?runway-state-word">Idle/);
const stoppedRoot=T.rowHTML({...root,status:'failed',stateDetail:'interrupted',updatedAt:ago(1)});
assert.match(stoppedRoot,/runway-refined-status t-mute[^>]*>.*?runway-state-word">Stopped/);
const activityStamp=ago(1);
const childRowHTML=overrides=>{
  const parent=T.state.forest.roots.find(chat=>chat.id==="root");
  const current=parent.descendants.find(agent=>agent.id==="child");
  const updated={...current,...overrides};
  return T.trayEntries({...parent,descendants:[updated]},T.parseQuery("")).find(entry=>entry.key===updated.key)?.html||"";
};
const runningAgentWithoutActivity=childRowHTML({status:'running',activity:null});
assert.match(runningAgentWithoutActivity,/runway-status-dot[^>]*aria-label="Running"/s,"a running child has a green status dot");
assert.match(runningAgentWithoutActivity,/runway-agent-name[^>]*Agent worker/);
assert.match(runningAgentWithoutActivity,/runway-refined-status t-live[^>]*>.*?runway-state-word">Running/s,"the refined row also shows the child's running state");
assert.doesNotMatch(runningAgentWithoutActivity,/data-runway-slot="activity"|Activity unavailable|Telemetry unavailable/);
const stoppedChild={...child,status:'failed',stateDetail:'interrupted',activity:null};
assert.equal(childRowHTML(stoppedChild),"","terminated children remain filtered from the visible tray");
const stoppedChildTelemetry=T.runwayTelemetryHTML(stoppedChild,true);
assert.match(stoppedChildTelemetry,/runway-status-dot[^>]*aria-label="Stopped"/);
assert.match(stoppedChildTelemetry,/runway-refined-status t-mute[^>]*>.*?runway-state-word">Stopped/s,"the refined child row visibly labels a stopped status");
const readingRow=childRowHTML({contextModel:"gpt-6.1-sol",contextObservedAt:activityStamp,activity:{kind:"reading",files:["A:\\src\\app.js"],observedAt:activityStamp}});
assert.match(readingRow,/data-runway-slot="activity" class="runway-workspace runway-activity" title="Reading app\.js · A:\\src\\app\.js"[^>]*>Reading app\.js/);
assert.match(readingRow,/class="runway-refined-title"[\s\S]*?data-runway-slot="model"[\s\S]*?class="runway-refined-timing"/);
assert.match(readingRow,/runway-refined-status t-live[^>]*>.*?runway-state-word">Running/s,"measured child action retains its running state");
assert.match(readingRow,/runway-status-dot[^>]*aria-label="Running"/s,"the running child status dot remains accessible");
assert.equal((readingRow.match(/data-runway-slot="activity"/g)||[]).length,1,"a child displays its current activity once");
assert.ok(readingRow.indexOf('data-runway-slot="activity"')>readingRow.indexOf('class="runway-refined-status"'),"the single activity follows the refined status column");
const orderedChild=childRowHTML({activeRunStartedAt:ago(2),runStartedAt:ago(3),model:"gpt-6.1-sol",contextModel:"gpt-6.1-sol",contextObservedAt:activityStamp,contextTokens:12000,contextWindow:258400,objective:"Review the current tool result",activity:{kind:"reading",files:["A:\\src\\app.js"],observedAt:activityStamp}});
const childOrder=["class=\"runway-refined-title\"","class=\"runway-refined-timing\"","class=\"runway-refined-status","class=\"runway-refined-activity\"","class=\"runway-refined-tokens\""].map(slot=>orderedChild.indexOf(slot));
assert.ok(childOrder.every((index,i)=>index>=0&&(i===0||index>childOrder[i-1])),"child content follows the refined production row order");
assert.equal((orderedChild.match(/data-runway-slot="activity"/g)||[]).length,1,"the reordered child row keeps one activity label");
assert.match(orderedChild,/runway-state-word">Running/);
const unclassifiedThinking=childRowHTML({activeRunStartedAt:ago(2),activity:{kind:"thinking",files:[],observedAt:activityStamp}});
assert.match(unclassifiedThinking,/runway-activity[^>]*>Reasoning<\/span>/,"fresh reasoning events use a concise evidence label");
const duplicateObjective=childRowHTML({activeRunStartedAt:ago(2),objective:"Reading app.js",activity:{kind:"reading",files:["A:\\src\\app.js"],observedAt:activityStamp}});
assert.match(duplicateObjective,/runway-activity[^>]*>Reading app\.js<\/span>/);
const compactingChild=T.runwayTelemetryHTML({...child,status:'compacting',compactionStartedAt:ago(1)},true);
assert.match(compactingChild,/runway-refined-status t-info[^>]*>.*?runway-state-word">Compacting/s,"the refined row labels an active compaction");
const trackerObjective=childRowHTML({activeRunStartedAt:ago(2),latestUpdate:'[tracker]{"task":"Repair current startup sequencing","doing":"Inspecting measured telemetry","next":"Add regression cases","blocked":null}[/tracker]',latestUpdateAt:ago(1)});
const declaredPhase='[tracker]{"task":"Recover RAM for wake training","doing":"Identifying safe cleanup targets","next":"Measure the recovered memory","blocked":null}[/tracker]';
const declaredPhaseRow=childRowHTML({activeRunStartedAt:ago(3),trackerUpdate:declaredPhase,trackerUpdateAt:ago(1),latestUpdate:"A later ordinary progress comment",latestUpdateAt:ago(0)});
assert.doesNotMatch(declaredPhaseRow,/data-runway-slot="activity"|Activity unavailable|Telemetry unavailable/,"a declared tracker phase is not treated as observed activity");
assert.doesNotMatch(declaredPhaseRow,/Identifying safe cleanup targets/);
const declaredThinking=childRowHTML({activeRunStartedAt:ago(3),trackerUpdate:declaredPhase,trackerUpdateAt:ago(1),latestUpdate:"ordinary commentary",latestUpdateAt:ago(0),activity:{kind:"thinking",files:[],observedAt:ago(0)}});
assert.match(declaredThinking,/runway-activity[^>]*>Reasoning<\/span>/);
assert.doesNotMatch(declaredThinking,/Identifying safe cleanup targets/);
const declaredCommand=childRowHTML({activeRunStartedAt:ago(3),trackerUpdate:declaredPhase,trackerUpdateAt:ago(1),activity:{kind:"running_command",files:[],observedAt:ago(0)}});
assert.match(declaredCommand,/runway-activity[^>]*>Running command<\/span>/,"observed command work takes precedence over a declared phase");
const staleDeclaredPhase=childRowHTML({activeRunStartedAt:ago(1),trackerUpdate:declaredPhase,trackerUpdateAt:ago(3),latestUpdate:"ordinary stale commentary",latestUpdateAt:ago(3)});
assert.doesNotMatch(staleDeclaredPhase,/data-runway-slot="activity"|Activity unavailable|Telemetry unavailable/);
assert.doesNotMatch(staleDeclaredPhase,/runway-agent-objective|Identifying safe cleanup targets/,"an old-run declared phase is rejected");
const invalidDeclaredFallback=childRowHTML({activeRunStartedAt:ago(3),trackerUpdate:declaredPhase,trackerUpdateAt:new Date(fixedNow+60000).toISOString(),latestUpdate:'[tracker]{"task":"Use the current legacy phase","doing":"Reviewing safe cleanup targets","next":"Finish review","blocked":null}[/tracker]',latestUpdateAt:ago(1)});
assert.doesNotMatch(invalidDeclaredFallback,/data-runway-slot="activity"|Activity unavailable|Telemetry unavailable/);
assert.doesNotMatch(invalidDeclaredFallback,/Reviewing safe cleanup targets/);
assert.doesNotMatch(childRowHTML({objective:"AGENTS.md instructions\\n# Must not show policy text",latestRequest:"Useful raw prompt fallback"}),/runway-agent-objective|Useful raw prompt fallback|Must not show policy text/,"policy instructions and raw request text do not become an assignment fallback");
assert.doesNotMatch(childRowHTML({agentName:"agent-worker",title:"Generic worker",objective:"",assignment:"",latestRequest:""}),/runway-agent-objective/,"unknown assignment stays absent rather than falling back to a title");
const timedRoot=T.rowHTML({...root,stateSince:ago(2),runStartedAt:ago(10),activeRunStartedAt:ago(4)});
assert.match(timedRoot,/class="runway-refined-timing"><span data-runway-slot="elapsed" class="runway-refined-elapsed">2:00/,"the refined root row shows its measured state duration");
assert.equal((timedRoot.match(/data-runway-slot="elapsed"/g)||[]).length,1,"a root has one elapsed-time value");
const timedChild=childRowHTML({...child,stateSince:ago(1),runStartedAt:ago(7),activeRunStartedAt:ago(3)});
assert.match(timedChild,/class="runway-refined-timing"><span data-runway-slot="elapsed" class="runway-refined-elapsed">1:00/,"the refined child row shows its measured state duration");
assert.equal((timedChild.match(/data-runway-slot="elapsed"/g)||[]).length,1,"a child has one elapsed-time value");
const activityOnlyRow=childRowHTML({activity:{kind:"reading",files:["A:\\src\\app.js"],observedAt:activityStamp}});
assert.doesNotMatch(activityOnlyRow,/class="ctx-tokens"/,"action rendering alone does not invent a context measurement");
assert.match(activityOnlyRow,/No usable context sample yet/);
assert.match(childRowHTML({activity:{kind:"using_tool",detail:"Calling formatter",files:[],observedAt:activityStamp},latestUpdate:'[tracker]{"task":"A","doing":"Updating files","next":"Review output","blocked":null}[/tracker]',latestUpdateAt:ago(1),activeRunStartedAt:ago(3)}),/title="Using tool"[^>]*>Using tool/);
const actionSnippet='[tracker]{"task":"Fix Runway rows","doing":"Updating display formatter","next":"Review stale updates","blocked":null}[/tracker]';
const snippetRow=childRowHTML({status:"running",activeRunStartedAt:ago(3),latestUpdate:actionSnippet,latestUpdateAt:ago(1),activity:{kind:"thinking",files:[],observedAt:ago(0)}});
assert.match(snippetRow,/runway-activity[^>]*>Reasoning<\/span>/s);
const missingActivitySnippet=childRowHTML({status:"running",activeRunStartedAt:ago(3),latestUpdate:actionSnippet,latestUpdateAt:ago(1),activity:null});
assert.doesNotMatch(missingActivitySnippet,/data-runway-slot="activity"|Activity unavailable|Telemetry unavailable/);
assert.doesNotMatch(missingActivitySnippet,/Updating display formatter/);
for(const invalidSnippet of [
  {latestUpdateAt:ago(3),activeRunStartedAt:ago(1)},
  {latestUpdateAt:new Date(fixedNow+60000).toISOString(),activeRunStartedAt:ago(3)},
  {latestUpdateAt:ago(1),activeRunStartedAt:ago(3),latestUpdate:"ordinary private prose"},
  {latestUpdateAt:ago(1),activeRunStartedAt:ago(3),latestUpdate:actionSnippet,status:"idle"}
]){
  const row=childRowHTML({status:"running",activeRunStartedAt:ago(3),latestUpdate:actionSnippet,latestUpdateAt:ago(1),activity:{kind:"thinking",files:[],observedAt:ago(0)},...invalidSnippet});
  assert.doesNotMatch(row,/Updating display formatter/,"old-run, future, and unstructured snippets are rejected");
  if(invalidSnippet.status==="idle")assert.doesNotMatch(row,/runway-activity|>Thinking</,"ended runs omit their activity");
  else assert.match(row,/runway-activity[^>]*>Reasoning<\/span>/,"a fresh recorded reasoning event survives rejected tracker prose");
}
const editingRow=childRowHTML({activity:{kind:"editing",files:["A:\\src\\widget.py"],observedAt:activityStamp}});
assert.match(editingRow,/Editing widget\.py/);
assert.match(childRowHTML({activity:{kind:"thinking",files:[],observedAt:activityStamp}}),/runway-activity[^>]*>Reasoning<\/span>/);
assert.match(childRowHTML({agentName:"",title:"Subagent",activity:{kind:"thinking",files:[],observedAt:activityStamp}}),/runway-activity[^>]*>Reasoning<\/span>/);
assert.doesNotMatch(childRowHTML({activity:{kind:"working",files:[],observedAt:activityStamp}}),/Task:/,"unknown activity does not invent an assignment");
for(const [kind,label] of [['searching','Searching files'],['reading','Reading file'],['editing','Editing file'],['testing','Running checks'],['running_command','Running command'],['running_code','Running code'],['inspecting','Inspecting page'],['coordinating','Messaging agent'],['checking_data','Checking data'],['using_tool','Using tool'],['waiting','Waiting for result'],['reading_output','Checking output'],['searching_web','Searching web']])
  assert.match(childRowHTML({activity:{kind,label,files:[],observedAt:activityStamp}}),new RegExp(label.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')));
assert.doesNotMatch(childRowHTML({activity:{kind:'testing',label:'internalFunction(secret)',files:[],observedAt:activityStamp}}),/internalFunction|secret/);
assert.doesNotMatch(childRowHTML({...child,status:"running",activity:null}),/data-runway-slot="activity"|Activity unavailable|Telemetry unavailable/);
assert.match(childRowHTML({...child,status:"needs-input",pendingQuestion:"Wait?",pendingQuestionAt:ago(1),activity:null}),/runway-waiting-link[^>]*>Waiting<\/button>/);
assert.match(childRowHTML({...child,status:"compacting",compactionStartedAt:ago(1),activity:null}),/>Compacting<\/span>/);
assert.match(childRowHTML({activity:{kind:"reading",files:["A:\\src\\<unsafe>.js"],observedAt:activityStamp}}),/Reading &lt;unsafe&gt;\.js/);
for(const staleChild of [{activityUnavailable:true},{telemetryUnavailable:true}])
  assert.doesNotMatch(childRowHTML({activity:{kind:"editing",files:["A:\\src\\widget.py"]},...staleChild}),/Editing widget\.py|Last known|Last ·/);
assert.match(childRowHTML({activity:{kind:"editing",files:["A:\\src\\widget.py"],observedAt:activityStamp},metricsUnavailable:true}),/Editing widget\.py/);
assert.doesNotMatch(childRowHTML({status:"running",activity:null}),/Working/);
assert.match(childRowHTML({status:"needs-input",pendingQuestion:"Wait?",pendingQuestionAt:ago(1),activity:null}),/runway-waiting-link[^>]*>Waiting<\/button>/);
assert.doesNotMatch(childRowHTML({status:"compacting",activity:null}),/data-runway-slot="activity"|Activity unavailable|Telemetry unavailable/);
assert.match(T.rowHTML(root),/data-runway-slot="workspace" class="runway-workspace" title="Workspace · D:\\work\\sample app"/);
assert.match(T.rowHTML({...root,status:'compacting',compactionStartedAt:ago(1)}),/runway-status-dot[^>]*aria-label="Compacting"/);
const waitingRow=T.rowHTML({...root,status:'needs-input',pendingQuestion:'Wait?',pendingQuestionAt:ago(1)});
assert.match(waitingRow,/runway-refined-status t-attn[^>]*>.*?runway-state-word runway-waiting-link[^>]*>Waiting<\/button>/s);
assert.match(waitingRow,/runway-state-glyph is-waiting/);
assert.doesNotMatch(html,/until cold|likely warm|likely cold|~1h|cold time unknown/);
assert.match(html,/D:\\work\\sample app/);
assert.doesNotMatch(html,/Fix login test|Refactor parser|ctx-ring|ctx-pct|toggle-chat|toggle-agent-details|toggle-agents|Open chat|Copy resume|Headroom|Recorded context/);
assert.equal((html.match(/6\.1 Sol/g)||[]).length,6,"each model name appears as visible text and its matching title");
assert.match(html,/data-runway-slot="model" class="runway-telemetry-model" title="GPT-6\.1 Sol"><span class="runway-model-name is-codex-model">GPT-6\.1 Sol/);
assert.deepEqual(Array.from(T.trayEntries(T.state.forest.roots[0],T.parseQuery("")),entry=>entry.key),["Codex:child","Codex:grandchild"]);
assert.match(html,/--agent-depth:1/);
assert.doesNotMatch(html,/data-key="Codex:done"/);
T.render(snapshot);
assert.match(node("#chat-list").innerHTML,/data-key="Codex:grandchild"/); // old collapse state cannot conceal live agents
assert.equal(T.runwayStateElapsed({...root,status:"quiet",provider:"Codex"}),"—"); // session age is not quiet-state duration
assert.equal(T.runwayStateElapsed({...root,status:"compacting",provider:"Codex"}),"—"); // compaction needs its own start
assert.equal(T.runwayStateElapsed({...root,status:"needs-input",attentionSince:ago(2)}),"—");
assert.equal(T.runwayStateElapsed({...root,status:"running",pendingQuestion:"Reply?",pendingQuestionAt:ago(2)}),"2:00");
assert.equal(T.runwayStateElapsed({...root,status:"running",pendingQuestion:"Reply?",stateSince:ago(5),pendingQuestionAt:null}),"—");
assert.equal(T.runwayStateElapsed({...root,pendingQuestion:"Expired?",pendingQuestionExpired:true,pendingQuestionAt:ago(3)}),"3:00");
assert.equal(T.runwayStateElapsed({...root,status:"running",activeRunStartedAt:new Date(fixedNow+60000).toISOString()}),"—");
assert.equal(T.runwayStateElapsed({...root,status:"running",stateSince:ago(1),activeRunStartedAt:ago(50)}),"1:00");
assert.equal(T.runwayStateElapsed({...root,status:"compacting",stateSince:ago(8),compactionStartedAt:ago(2)}),"2:00");
const soloExecution={...root,descendants:[],status:"needs-input",pendingQuestion:"Old question",pendingQuestionExpired:true,pendingQuestionAt:ago(25*60*60),executionStatus:"running",executionSource:"transcript",executionObservedAt:ago(1),executionSince:ago(4),executionActivity:{kind:"testing"}};
assert.equal(T.runwayWaiting(soloExecution),false);
assert.equal(T.runwayRunning(soloExecution),true,"a fresh solo execution survives expired attention without children");
assert.equal(T.runwayStateElapsed(soloExecution),"4:00");
assert.equal(T.runInfo(soloExecution).seconds,240,"run time uses the proven current run start");
assert.equal(T.runwayCounts({roots:[soloExecution]}).total,1);
assert.match(T.rowHTML(soloExecution),/Running checks/);
const appExecution={...soloExecution,executionSource:"app"};
assert.equal(T.runwayRunning(appExecution),true,"fresh app execution may confirm Running");
assert.doesNotMatch(T.rowHTML(appExecution),/data-runway-slot="activity"|Running checks/,"app execution cannot establish tool activity");
const staleExecution={...soloExecution,executionObservedAt:ago(151)};
assert.equal(T.runwayRunning(staleExecution),false,"stale execution timestamps cannot keep a chat active");
assert.equal(T.runInfo(staleExecution),null,"stale execution never falls back to session age");
const currentQuestion={...soloExecution,pendingQuestionExpired:false,pendingQuestionAt:ago(1),executionObservedAt:ago(1)};
assert.equal(T.runwayWaiting(currentQuestion),true);
assert.equal(T.runwayRunning(currentQuestion),false);
assert.equal(T.runwayCounts({roots:[currentQuestion]}).waiting,1,"valid question attention remains Waiting during execution");
assert.equal(T.runwayActivityEvidence(currentQuestion).status,"running","activity evidence remains independent from yellow question attention");
assert.match(T.rowHTML(currentQuestion),/Running checks/);
const nullExecutionActivity={...soloExecution,activity:{kind:"reading",files:["old-file.js"],observedAt:ago(1)},executionActivity:null};
assert.doesNotMatch(T.rowHTML(nullExecutionActivity),/data-runway-slot="activity"|Activity unavailable|Telemetry unavailable/);
assert.doesNotMatch(T.rowHTML(nullExecutionActivity),/old-file\.js|Reading old-file\.js/,"explicit execution null does not resurrect legacy activity");
const snippetOnlyActivity={...nullExecutionActivity,trackerUpdate:'[tracker]{"task":"demo","doing":"Invented work","next":null,"blocked":null}[/tracker]'};
assert.doesNotMatch(T.rowHTML(snippetOnlyActivity),/data-runway-slot="activity"|Activity unavailable|Telemetry unavailable/);
assert.doesNotMatch(T.rowHTML(snippetOnlyActivity),/Invented work/);
const blockingQuestion={...soloExecution,executionStatus:"waiting",executionObservedAt:ago(1),executionSince:null,executionActivity:null};
assert.equal(T.runwayRunning(blockingQuestion),false,"a sync blocker cannot expose the old async execution as running");
assert.equal(T.runInfo(blockingQuestion),null);
assert.doesNotMatch(T.rowHTML(blockingQuestion),/Running checks/);
assert.match(T.rowHTML(blockingQuestion),/runway-activity[^>]*>Waiting for input/);
const compactExecution={...soloExecution,executionStatus:"compacting",executionCompactionStartedAt:ago(2),executionActivity:null};
assert.equal(T.runwayStateElapsed(compactExecution),"2:00");
assert.equal(T.runwayEffectiveStatus(compactExecution),"compacting");
assert.match(T.rowHTML(compactExecution),/>Compacting<\/span>/);
const compactWithoutStart={...compactExecution,executionCompactionStartedAt:null,executionSince:ago(4)};
assert.equal(T.runwayStateElapsed(compactWithoutStart),"—","compaction elapsed cannot use the whole-run start");
const endedExecution={...soloExecution,executionStatus:"idle",executionTerminalAt:ago(1)};
assert.equal(T.runwayRunning(endedExecution),false,"terminal execution cannot be revived by cached active labels");
assert.doesNotMatch(T.rowHTML({...soloExecution,executionObservedAt:ago(151)}),/data-runway-slot="activity"|Activity unavailable|Telemetry unavailable/);
assert.doesNotMatch(T.rowHTML({...soloExecution,executionStatus:"unavailable",executionSource:"unavailable",executionObservedAt:null}),/data-runway-slot="activity"|Activity unavailable|Telemetry unavailable/);
const liveChild={...soloExecution,status:"running",executionObservedAt:ago(1)};
const completedParent={...root,descendants:[liveChild],status:"idle",stateDetail:"completed",executionStatus:"idle",executionSource:"transcript",executionObservedAt:ago(1)};
assert.match(T.rowHTML(completedParent),/aria-label="Agents running" title="Agents running">Agents active/);
const freshObservation=new Date(fixedNow).toISOString();
const globallyFreshRead={...root,executionStatus:"running",executionSource:"transcript",executionObservedAt:freshObservation,executionActivity:{kind:"reading",files:["src/a.js"],observedAt:freshObservation}};
const globallyFreshReasoning={...globallyFreshRead,executionActivity:{kind:"thinking",files:[],observedAt:freshObservation}};
const globallyFreshChild={...liveChild,executionObservedAt:freshObservation,executionActivity:{kind:"testing",files:[],observedAt:freshObservation}};
const globallyFreshIdleParent={...root,status:"idle",stateDetail:null,executionStatus:"idle",executionSource:"transcript",executionObservedAt:freshObservation,descendants:[globallyFreshChild]};
assert.equal(T.runwayCurrentEvidenceFresh(),true);
assert.match(T.rowHTML(globallyFreshRead),/Reading a\.js/);
assert.match(T.rowHTML(globallyFreshReasoning),/Reasoning/);
assert.match(T.rowHTML(globallyFreshIdleParent),/Agents active/);
testNow=fixedNow+8001;
assert.equal(T.runwayCurrentEvidenceFresh(),false);
assert.doesNotMatch(T.rowHTML(globallyFreshRead),/data-runway-slot="activity"|Activity unavailable|Telemetry unavailable/);
assert.doesNotMatch(T.rowHTML(globallyFreshRead),/Reading a\.js/);
assert.doesNotMatch(T.rowHTML(globallyFreshReasoning),/data-runway-slot="activity"|Activity unavailable|Telemetry unavailable/);
assert.doesNotMatch(T.rowHTML(globallyFreshReasoning),/Reasoning/);
assert.doesNotMatch(T.rowHTML(globallyFreshIdleParent),/Agents active/);
testNow=fixedNow;
assert.equal(T.isAsking({...root,status:"needs-input",pendingQuestion:"Stale?",pendingQuestionAt:ago(24*60+1)}),false);
assert.equal(T.isAsking({...root,status:"needs-input",pendingQuestion:"Recent?",pendingQuestionAt:ago(23*60)}),true);
// Python rows carry Claude permission waits as "needs input" with no question text; they must reveal as Needs you.
assert.equal(T.isAsking({...root,status:"needs input",pendingQuestion:null}),true);
assert.equal(T.isAsking({...root,status:"running",pendingQuestion:null}),false);
assert.match(T.rowHTML({...T.state.forest.roots[0],lastRequest:{input:100,cached:0}}),/runway-refined-cache">Cache 0%/);
assert.doesNotMatch(T.rowHTML({...T.state.forest.roots[0],lastRequest:null}),/Cache|cached|runway-cache/);
assert.match(T.rowHTML({...T.state.forest.roots[0],contextPercent:92,contextTokens:92,contextWindow:100}),/runway-context-used t-err/);
assert.match(T.rowHTML({...T.state.forest.roots[0],contextPercent:80,contextTokens:80,contextWindow:100}),/runway-context-used t-err/);
assert.doesNotMatch(T.rowHTML({...root,model:'gpt-6-astra'}),/data-runway-slot="context"/,"a context sample from another model is omitted");
assert.doesNotMatch(T.rowHTML({...root,compactionStartedAt:ago(1)}),/data-runway-slot="context"/,"a pre-compaction context sample is omitted");
assert.match(T.rowHTML({...root,contextTokens:0,contextWindow:100}),/runway-context-used">0<\/span>/,"zero context is a valid paired measurement");
assert.doesNotMatch(T.rowHTML({...T.state.forest.roots[0],contextPercent:null,contextTokens:null}),/data-runway-slot="context"/);
for(const [percent,tone] of [[50,""],[51," t-attn"],[75," t-attn"],[76," t-err"]]){
  const contextRow=T.rowHTML({...root,contextTokens:percent,contextWindow:100,contextPercent:percent});
  assert.equal(contextRow.match(/<span class="runway-context-used([^"]*)">/)[1],tone);
  assert.doesNotMatch(contextRow,/runway-context-label|>Context</);
  assert.match(contextRow,/class="runway-context-total">100<\/span>/);
}
const deltaContext=T.rowHTML({...root,contextDelta:1250,contextDeltaObservedAt:root.contextObservedAt});
assert.match(deltaContext,/title="Context occupancy: 160000 \/ 258400 tokens; change since previous sample \+1\.25K tokens"/);
assert.doesNotMatch(T.rowHTML({...root,contextDelta:-500,contextDeltaObservedAt:ago(1)}),/change since previous sample/,"a delta from a different sample time is omitted");
assert.doesNotMatch(T.rowHTML({...root,contextDelta:500,contextDeltaObservedAt:root.contextObservedAt,contextModel:"gpt-6-astra"}),/change since previous sample/);
assert.match(cssRule('.runway-context-fraction,.runway-context-separator,.runway-context-total'),/color:var\(--ink\)/);
assert.match(cssRule('.runway-context-used'),/color:var\(--ink\)/);
assert.match(cssRule('.runway-context-used.t-attn'),/color:var\(--orange\)/);
assert.match(cssRule('.runway-context-used.t-err'),/color:var\(--red\)/);
const detail60=T.runwayDetailHTML({...root,contextTokens:60,contextWindow:100,contextPercent:null});
assert.match(detail60,/class="f-l">Recorded usage<\/span><span class="f-v"><span class="runway-context-used t-attn">60/);
assert.doesNotMatch(detail60,/Context/);
assert.match(detail60,/runway-context-separator"> \/ <\/span><span class="runway-context-total">100/);
const detailContext60=detail60.match(/<div class="facts runway-context-facts">([\s\S]*?)<\/div><div class="meta">/)[1];
assert.doesNotMatch(detailContext60.replace(/<[^>]*>/g,""),/\btokens?\b/i);
const detail76=T.runwayDetailHTML({...root,contextTokens:76,contextWindow:100,contextPercent:null});
assert.match(detail76,/class="f-l">Recorded usage<\/span><span class="f-v"><span class="runway-context-used t-err">76/);
assert.doesNotMatch(detail76,/Context/);
assert.match(detail76,/runway-context-separator"> \/ <\/span><span class="runway-context-total">100/);
assert.doesNotMatch(T.runwayDetailHTML({...root,metricsUnavailable:true}),/Recorded usage|Headroom|Cache|cached|Last known/);
assert.doesNotMatch(T.runwayDetailHTML({...root,model:"Codex",workspace:null,root:{workspace:null},objective:null}),/Model not recorded|Workspace unavailable|Objective unavailable|path unavailable/);
const driveRootRow=T.rowHTML({...root,workspace:"D:\\"});
assert.match(driveRootRow,/runway-path-leaf">D: D:\\/);
assert.ok(driveRootRow.includes('title="Workspace · D:\\"'));
const workspaceText=row=>row.match(/<span data-runway-slot="workspace"[^>]*>([\s\S]*?)<\/span>/)[1].replace(/<[^>]*>/g,"");
assert.equal(workspaceText(driveRootRow),"D: D:\\");
assert.equal(workspaceText(T.rowHTML({...root,workspace:"D:\\memoir"})),"D: memoir");
assert.equal(workspaceText(T.rowHTML({...root,workspace:"C:\\Projects\\memoir\\child"})),"C: child");
const longPath="C:\\Projects\\"+"folder\\".repeat(18)+"long-leaf-"+"x".repeat(120);
const longPathRow=T.rowHTML({...root,workspace:longPath});
assert.match(longPathRow,/runway-path-leaf">C: long-leaf-x{120}<\/span>/);
assert.ok(longPathRow.includes('title="Workspace · '+longPath+'"'));
const unavailablePathRow=T.rowHTML({...root,workspace:null,root:{workspace:null}});
assert.doesNotMatch(unavailablePathRow,/data-runway-slot="workspace"|Workspace unavailable|path unavailable/);
assert.equal(T.runwayTitle({...root,title:'Sample app sol wake learning'},true),'wake learning');
assert.equal(T.runwayTitle({...root,title:'Memoir sol wake learning'}),'Memoir sol wake learning');
// Orchestration job briefs collapse to worker and task for root chats and agents alike.
assert.equal(T.runwayTitle({...root,title:'# [Orchestrator] Luna · positioning — draft product positioning, brand concepts and demo plan'}),'Luna · positioning');
assert.equal(T.runwayTitle({...root,title:'[Orchestrator] Sol · review providers-diff - adversarial review (continue #2)'},true),'Sol · review providers-diff');
assert.equal(T.runwayTitle({...root,title:'[Orchestrator] Luna · frozen-probe (retry #2)'}),'Luna · frozen-probe');
assert.equal(T.runwayTitle({...root,title:'Fix the [Orchestrator] widget'}),'Fix the [Orchestrator] widget');
assert.equal(T.runwayTitle({...root,title:'# [Orchestrator] Luna ? frozen-probe ? prove whether Orchestrator can ship as a frozen single-folder Windows build ## Outcome Evidence for one decision'}),'Luna · frozen-probe');
assert.equal(T.runwayTitle({...root,title:'# [Orchestrator] Luna · ui fixes (continue)\nGoal: fix the 7 review findings\n\nDelta: long body'}),'Luna · ui fixes');
assert.equal(T.runwayTitle({...root,title:'Plain first line\n## Outcome\nlong body text'}),'Plain first line');
assert.ok(T.runwayTitle({...root,title:'x'.repeat(300)}).length<=72);
assert.equal(T.runwayModelLabel({...root,model:'Codex'}),'');
assert.doesNotMatch(T.rowHTML({...root,model:'Codex'}),/Model not recorded|data-runway-slot="model"/);
assert.match(T.rowHTML({...root,model:'gpt-6-luna'}),/title="GPT-6 Luna"><span class="runway-model-name is-codex-model">GPT-6 Luna/);
const modelChangeBase={...root,id:'model-change',key:'Codex:model-change',model:'gpt-6-luna',modelChangeAt:ago(1)};
assert.doesNotMatch(T.rowHTML(modelChangeBase),/model-changed/,"first discovery never flashes a model change");
const changedModelRow=T.rowHTML({...modelChangeBase,model:'gpt-6-astra',modelChangeAt:new Date(fixedNow).toISOString()});
assert.match(changedModelRow,/title="GPT-6 Astra"><span class="runway-model-name is-codex-model model-changed" data-model-change-at="\d+">GPT-6 Astra/);
assert.equal(Number(changedModelRow.match(/data-model-change-at="(\d+)"/)?.[1]),fixedNow,'the highlight version comes from the provider event timestamp');
testNow=fixedNow+1500;
assert.match(T.rowHTML({...modelChangeBase,model:'gpt-6-astra',modelChangeAt:new Date(fixedNow).toISOString()}),/model-changed/,"the highlight remains for its event-based window");
testNow=fixedNow+3000;
assert.doesNotMatch(T.rowHTML({...modelChangeBase,model:'gpt-6-astra',modelChangeAt:new Date(fixedNow).toISOString()}),/model-changed/,"highlight expires from the provider event time");
testNow=fixedNow+4000;
const secondModelEvent=T.rowHTML({...modelChangeBase,model:'gpt-6-sol',modelChangeAt:new Date(fixedNow+4000).toISOString()});
assert.match(secondModelEvent,/data-model-change-at="[^"]+"/,'a second measured model event carries its own animation version');
assert.match(secondModelEvent,/model-changed/);
assert.doesNotMatch(T.rowHTML({...modelChangeBase,model:'gpt-6-astra',modelChangeAt:new Date(fixedNow+1500).toISOString()}),/model-changed/,'an older event timestamp cannot highlight a later unverified model value');
const futureEventBase={...root,id:'future-model-event',key:'Codex:future-model-event',model:'gpt-6-luna',modelChangeAt:new Date(fixedNow).toISOString()};
assert.doesNotMatch(T.rowHTML(futureEventBase),/model-changed/);
testNow=fixedNow+100;
assert.doesNotMatch(T.rowHTML({...futureEventBase,model:'gpt-6-astra',modelChangeAt:new Date(fixedNow+10000).toISOString()}),/model-changed/,'future model event timestamps do not create a highlight');
const afterFuture=T.rowHTML({...futureEventBase,model:'gpt-6-sol',modelChangeAt:new Date(fixedNow+50).toISOString()});
assert.match(afterFuture,/model-changed/,'a future timestamp does not block a later valid recorded model event');
assert.equal(Number(afterFuture.match(/data-model-change-at="(\d+)"/)?.[1]),fixedNow+50);
testNow=fixedNow;
const staleMetricsRow=T.rowHTML({...root,metricsUnavailable:true});
assert.doesNotMatch(staleMetricsRow,/Current lifecycle state is available|Last cache|Last known|data-runway-slot="context"|runway-cache|Cache \d+%/);
assert.match(staleMetricsRow,/runway-refined-status t-live[^>]*>.*?runway-state-word">Running/);
assert.match(T.rowHTML({...root,workspaceKind:'codex-chat'}),/runway-workspace[^>]*title="Per-chat working folder; not a project checkout · D:\\work\\sample app"/);
const modelSnapshot={...snapshot,providers:[{...provider,chats:[{...root,model:'Codex',statusSource:'transcript'}]}]};
T.applyLive(modelSnapshot,{generatedAt:ago(0),chats:[{key:'Codex:root',id:'root',provider:'Codex',title:'Live-renamed root',parentId:null,agentName:'',agentRole:null,workspace:'A:\\projects\\renamed',workspaceKind:'project',source:'rollout',activityAt:ago(0),status:'running',model:'gpt-6-luna',latestRequestAt:ago(2),contextDelta:140,contextDeltaObservedAt:ago(0),modelChangeAt:ago(1),trackerUpdate:declaredPhase,trackerUpdateAt:ago(1),activity:{kind:'using_tool',label:'Using tool',files:[],observedAt:ago(0)}}]});
assert.equal(modelSnapshot.providers[0].chats[0].model,'gpt-6-luna');
assert.equal(modelSnapshot.providers[0].chats[0].title,'Live-renamed root');assert.equal(modelSnapshot.providers[0].chats[0].workspace,'A:\\projects\\renamed');
assert.equal(modelSnapshot.providers[0].chats[0].latestRequestAt,ago(2));
assert.equal(modelSnapshot.providers[0].chats[0].contextDelta,140);
assert.equal(modelSnapshot.providers[0].chats[0].contextDeltaObservedAt,ago(0));
assert.equal(modelSnapshot.providers[0].chats[0].modelChangeAt,ago(1));
assert.equal(modelSnapshot.providers[0].chats[0].activity.kind,'using_tool');
assert.equal(modelSnapshot.providers[0].chats[0].trackerUpdate,declaredPhase,'the live merge retains the declared tracker phase');
assert.equal(modelSnapshot.providers[0].chats[0].trackerUpdateAt,ago(1),'the live merge retains the phase observation time');
T.applyLive(modelSnapshot,{generatedAt:ago(0),chats:[{key:'Codex:root',id:'root',provider:'Codex',executionStatus:'unavailable',executionSource:'unavailable',executionObservedAt:null,executionSince:null,executionCompactionStartedAt:null,executionTerminalAt:null,executionActivity:null}]});
assert.equal(modelSnapshot.providers[0].chats[0].executionStatus,'unavailable');
assert.equal(modelSnapshot.providers[0].chats[0].executionSince,null,'live explicit nulls invalidate prior execution clocks');
const liveIdentitySnapshot={...snapshot,providers:[{name:'Codex',connected:true,chats:[{...root}]}]};
const liveIdentity={generatedAt:ago(0),chats:[
  {key:'Codex:live-parent',id:'live-parent',provider:'Codex',parentId:null,title:'Live idle anchor',agentName:'',agentRole:null,workspace:'A:\\projects\\live',workspaceKind:'project',source:'rollout',status:'idle',statusSource:'transcript',telemetryUnavailable:false,activityAt:ago(0)},
  {key:'Codex:live-child',id:'live-child',provider:'Codex',parentId:'live-parent',title:'Live child',agentName:'agent-live-child',agentRole:'worker',workspace:'A:\\projects\\live',workspaceKind:'project',source:'rollout',status:'running',statusSource:'app',telemetryUnavailable:false,activeRunStartedAt:ago(1),runStartedAt:ago(2),model:'gpt-6.1-sol',executionStatus:'running',executionSource:'transcript',executionObservedAt:ago(0),executionSince:ago(1),executionActivity:{kind:'reading',files:['A:\\projects\\live\\src\\a.js'],observedAt:ago(0)},activityAt:ago(0)},
  {key:'Codex:mismatch',id:'bad-id',provider:'Codex',parentId:null,title:'Invalid identity',status:'running',activityAt:ago(0)}
]};
T.applyLive(liveIdentitySnapshot,liveIdentity);
assert.equal(liveIdentitySnapshot.providers[0].chats.some(chat=>chat.id==='live-parent'),true,'live metadata upserts a real idle parent anchor');
assert.equal(liveIdentitySnapshot.providers[0].chats.some(chat=>chat.id==='live-child'),true,'live metadata upserts an active child before a full refresh');
assert.equal(liveIdentitySnapshot.providers[0].chats.some(chat=>chat.id==='bad-id'),false,'a mismatched live key cannot invent a chat identity');
T.render(liveIdentitySnapshot);
const liveChildRow=T.state.forest.rows.find(chat=>chat.id==='live-child');
assert.equal(liveChildRow.parentId,'live-parent');assert.equal(liveChildRow.root.id,'live-parent','the live child links to its actual idle parent anchor');
const liveChildMarkup=T.trayEntries(liveChildRow.root,T.parseQuery("")).find(entry=>entry.key===liveChildRow.key)?.html||"";
assert.match(liveChildMarkup,/class="runway-refined-activity"><span data-runway-slot="activity"[^>]*>Reading a\.js/s,"a live child’s observed reading activity renders in its refined activity column");
const priorFetch=context.fetch,priorLive=T.state.live,liveDuringRefresh={generatedAt:ago(0),chats:[{key:'Codex:live-child',id:'live-child',provider:'Codex',parentId:'live-parent',status:'running',statusSource:'app',telemetryUnavailable:false,executionStatus:'running',executionSource:'transcript',executionObservedAt:ago(0),executionActivity:{kind:'editing',files:['a.js'],observedAt:ago(0)},activityAt:new Date(fixedNow+1000).toISOString()}]};
T.state.snapshot=liveIdentitySnapshot;T.state.loading=true;T.state.refreshPending=false;T.state.refreshPendingForce=false;
context.fetch=async url=>url==='/api/live'?{ok:true,json:async()=>liveDuringRefresh}:url==='/api/limit-reset'?{ok:false}:null;
await T.pollLive();
assert.equal(T.state.snapshot.providers[0].chats.find(chat=>chat.id==='live-child').executionActivity.kind,'editing','fast live updates render while full stats are still loading');
context.fetch=priorFetch;T.state.live=priorLive;T.state.loading=false;T.state.refreshPending=false;T.state.refreshPendingForce=false;T.state.pendingOutside.clear();
// Message-age checks run in their own page instance so advancing this clock cannot
// make later independent fixture blocks move behind an accepted-live timestamp.
{
  let messageNow=fixedNow, messageTimer=0;
  class MessageDate extends Date { constructor(...args){super(...(args.length?args:[messageNow]));} static now(){return messageNow;} }
  const messageNodes=new Map(), messageNode=selector=>{if(!messageNodes.has(selector))messageNodes.set(selector,{innerHTML:"",textContent:"",value:"",hidden:false,title:"",className:"",dataset:{},style:{setProperty(k,v){this[k]=v;}},contains(){return false;},querySelectorAll(){return [];},setAttribute(){},getAttribute(){return null;},removeAttribute(){},classList:{toggle(){},add(){},remove(){},contains(){return false;}}});return messageNodes.get(selector);};
  const messageContext={window:{__TOKEN_TRACKER_TEST__:true,__TOKEN_TRACKER_RUNWAY__:true,__TOKEN_TRACKER_REFINED__:true,__TOKEN_TRACKER_COMPACT__:true},document:{hidden:false,activeElement:null,querySelector:messageNode,querySelectorAll(){return [];},body:{classList:{toggle(){}}}},localStorage:{getItem(){return null;},setItem(){}},Intl,Date:MessageDate,Math,Number,JSON,setTimeout(){return ++messageTimer;},clearTimeout(){},AbortSignal:{timeout(ms){return {ms};}}};
  messageContext.window.window=messageContext.window;vm.createContext(messageContext);vm.runInContext(app,messageContext);
  const M=messageContext.window.__tokenTrackerTest, stamp=at=>new Date(at).toISOString();
  const messages=[{...root,id:'message-parent',status:'running'},{...child,id:'message-child',parentId:'message-parent',status:'running',latestMessageAt:stamp(fixedNow-1000),latestMessageDirection:'parent-to-agent'}];
  const messageSnapshot={generatedAt:stamp(messageNow),enabledProviders:['Codex'],providers:[{...provider,chats:messages}]};
  M.render(messageSnapshot);
  const parent=M.state.forest.roots.find(chat=>chat.id==='message-parent');
  let html=M.trayEntries(parent,M.parseQuery('')).find(entry=>entry.key==='Codex:message-child').html;
  assert.match(html,/runway-message-indicator/,'the message dot appears immediately when an agent needs the parent to see a message');
  assert.ok(M.runwayAgentHasNewMessage(parent.descendants[0]),'badge remains visible before ten seconds');
  messageNow=fixedNow+9000;M.render({...messageSnapshot,generatedAt:stamp(messageNow)});M.tick();
  html=M.trayEntries(M.state.forest.roots.find(chat=>chat.id==='message-parent'),M.parseQuery('')).find(entry=>entry.key==='Codex:message-child').html;
  assert.doesNotMatch(html,/runway-message-indicator/,'a fresh full snapshot does not preserve a ten-second-old message dot');

  messageNow+=100;const replyAt=stamp(messageNow-1000);
  M.render({...messageSnapshot,generatedAt:stamp(messageNow),providers:[{...provider,chats:[{...messages[0]},{...messages[1],latestMessageAt:replyAt,latestMessageDirection:'agent-to-parent'}]}]});
  const repliedParent=M.state.forest.roots.find(chat=>chat.id==='message-parent');
  html=M.trayEntries(repliedParent,M.parseQuery('')).find(entry=>entry.key==='Codex:message-child').html;
  assert.match(html,/runway-message-indicator/,'a reply does not erase the recent-message window');
  messageNow+=9000;M.render({...M.state.snapshot,generatedAt:stamp(messageNow)});M.tick();
  html=M.trayEntries(M.state.forest.roots.find(chat=>chat.id==='message-parent'),M.parseQuery('')).find(entry=>entry.key==='Codex:message-child').html;
  assert.doesNotMatch(html,/runway-message-indicator/);
}const waitingSnapshot={...snapshot,providers:[{...provider,chats:[{...root,id:'waiting-title',title:'Waiting parent',status:'needs-input',pendingQuestion:'Choose?',pendingQuestionAt:ago(1),attentionSince:ago(1)}]}]};
T.render(waitingSnapshot);
assert.equal(T.notch.counts.waiting,1);assert.match(node('#notch-counts').innerHTML,/aria-label="1 waiting[",]/);
assert.equal(T.notch.counts.roots,1);assert.equal(T.notch.counts.total,1);
assert.match(node('#chat-list').innerHTML,/Waiting parent/);
assert.match(node('#chat-list').innerHTML,/runway-refined-status t-attn[^>]*>.*?runway-waiting-link[^>]*>Waiting<\/button>/s);
assert.match(node('#chat-list').innerHTML,/runway-refined-status t-attn[^>]*data-own-waiting="true"[^>]*>.*?runway-waiting-link[^>]*data-key="Codex:waiting-title"[^>]*>Waiting<\/button>/s);
assert.match(node('#chat-list').innerHTML,/runway-state-glyph is-waiting/);
const unavailableSnapshot={...snapshot,providers:[{...provider,chats:[{...root,status:'unknown',telemetryUnavailable:true,statusSource:'unavailable',previousStatus:'running',lastValidAt:ago(1)}]}]};
assert.equal(T.runwayVisibleActive(unavailableSnapshot.providers[0].chats[0]),true);
assert.equal(T.runwayVisibleActive({...unavailableSnapshot.providers[0].chats[0],stateDetail:'completed'}),false);
T.render(unavailableSnapshot);
assert.equal(T.notch.counts.roots,0);assert.equal(T.notch.counts.total,0);
assert.doesNotMatch(node('#chat-list').innerHTML,/data-runway-slot="state"|Status unavailable|Last known/);
assert.equal(T.state.forest.roots.length,1);
testNow=fixedNow+150001;T.render(unavailableSnapshot);
assert.equal(T.visibleRoots(T.state.forest).length,0,"expired carry-forward rows are excluded from the visible roster");
assert.doesNotMatch(node('#chat-list').innerHTML,/Live parent|Status unavailable/);
assert.equal(T.notch.active,0,"expired carry-forward rows no longer count as active");
testNow=fixedNow;
T.render(snapshot);
// Counts use unique active entities across the full forest, independent of search.
const countChats=[root,child,grandchild,{...root,id:'finished-parent',status:'idle',stateDetail:'completed'},
  {...child,id:'parent-worker',parentId:'finished-parent'},
  {...child,id:'waiting-child',parentId:'finished-parent',status:'running',pendingQuestion:'Reply?',attentionSince:ago(1)},
  {...root,id:'waiting-root',status:'needs-input',pendingQuestion:'Continue?'},
  {...root,id:'quiet-root',status:'quiet'},
  {...root,id:'idle-root',status:'idle'},
  {...root,id:'failed-only',status:'failed',stateDetail:'errored'},
  {...child,id:'stopped',status:'failed',stateDetail:'interrupted'},
  {...child,id:'deleted',deleted:true},
  {...root,id:'expired-question',status:'needs-input',pendingQuestion:'Old?',pendingQuestionExpired:true},
  {...root,id:'compacting-root',status:'compacting'}, {...child}];
countChats.push({...child,id:'live-review',agentRole:'auto-review'}, {...child,id:'completed-review',agentRole:'auto-review',status:'idle',stateDetail:'completed'});
const countSnapshot={...snapshot,providers:[{...provider,chats:countChats}]};
T.state.query='no matching title';T.render(countSnapshot);
assert.match(node('#notch-counts').innerHTML,/aria-label="4 visible chat groups"/);
assert.match(node('#notch-counts').innerHTML,/aria-label="8 active"/);
assert.match(node('#notch-counts').innerHTML,/aria-label="2 waiting[",]/);
assert.equal((node('#notch-counts').innerHTML.match(/class="count-value"/g)||[]).length,3);
assert.equal(T.notch.counts.roots,4);assert.equal(T.notch.counts.total,8);assert.equal(T.notch.counts.waiting,2);
assert.ok(T.notch.active>0);
T.state.query='';T.render(countSnapshot);
assert.doesNotMatch(node('#chat-list').innerHTML,/data-key="Codex:(?:quiet-root|idle-root|failed-only|stopped|deleted|expired-question)"/);
const parent=T.state.forest.roots.find(chat=>chat.id==='finished-parent');
assert.match(T.rowHTML(parent),/runway-refined-status t-attn[^>]*data-child-waiting="true"/);
assert.doesNotMatch(T.rowHTML(parent),/Agent waiting/);
assert.match(T.rowHTML({...parent,status:'running',stateDetail:'running'}),/runway-refined-status t-live[^>]*data-own-waiting="false" data-child-waiting="true"[^>]*>.*?runway-state-word">Running/s);
assert.doesNotMatch(T.rowHTML({...parent,status:'running',stateDetail:'running'}),/runway-waiting-link|>Agent waiting/,"a running parent remains Running while its waiting child exposes its own row control");
assert.match(T.rowHTML({...parent,status:'failed',stateDetail:'errored'}),/runway-refined-status t-err[^>]*>.*?runway-state-word">Failed/);
assert.match(node('#chat-list').innerHTML,/data-key="Codex:live-review"/);
assert.doesNotMatch(node('#chat-list').innerHTML,/data-key="Codex:completed-review"/);
T.state.query='worker is:running';T.state.windowDays=30;
const fullStatsUrl=new URL(T.statsUrl(false),'http://localhost');
for(const key of ['limit','pins','q','status','range','days','since'])assert.equal(fullStatsUrl.searchParams.has(key),false);
const desktopStatsUrl=new URL(T.statsUrl(false),'http://localhost');
assert.equal(desktopStatsUrl.pathname,'/api/stats');assert.equal(desktopStatsUrl.searchParams.get('background'),'1');
const desktopForcedStatsUrl=new URL(T.statsUrl(true),'http://localhost');
assert.equal(desktopForcedStatsUrl.searchParams.get('background'),'1');assert.equal(desktopForcedStatsUrl.searchParams.get('refresh'),'1');
T.state.query='';T.state.windowDays=1;
const resumedSnapshot={...snapshot,providers:[{...provider,chats:[{...root,pendingQuestion:'Expired?',pendingQuestionExpired:true}]}]};
T.render(resumedSnapshot);
assert.equal(T.notch.counts.roots,1);assert.equal(T.notch.counts.total,1);assert.equal(T.notch.counts.waiting,0);
assert.match(node('#chat-list').innerHTML,/runway-refined-status t-live[^>]*>.*?runway-state-word">Running/);
assert.doesNotMatch(node('#chat-list').innerHTML,/Waiting|Needs you/);
T.render(snapshot);
assert.equal(T.notch.counts.waiting,0);
const emptyWaitingSlot=node('#notch-counts').innerHTML;
assert.match(emptyWaitingSlot,/<span data-count-slot="waiting" class="[^"]*is-empty[^"]*" aria-hidden="true"/,'the zero-waiting badge keeps its fixed reserved slot');
assert.match(emptyWaitingSlot,/data-count-slot="waiting"[^]*?<span class="count-value"><\/span><span class="count-unit"><\/span>/,'the empty waiting badge has no visible count or label');

// Reveal follows lifecycle and inbound-message events, not ordinary telemetry.
T.setNotch(false);T.notch.manual=false;T.notch.inspecting=false;T.notch.keyboardInspecting=false;T.notch.autoExpanded=false;T.notch.activeKeys=null;T.notch.workStates.clear();
const lifecycleChats=[{...root,id:'lifecycle-root',status:'running',runStartedAt:ago(300),activeRunStartedAt:ago(4),updatedAt:ago(2),usageUpdatedAt:ago(3)},
  {...child,id:'paused-worker',parentId:'lifecycle-root',status:'idle',runStartedAt:ago(30),activeRunStartedAt:null,latestRequestAt:null}];
const lifecycleSnapshot={...snapshot,providers:[{...provider,chats:lifecycleChats}]};
function expireReveal(){
  const timerId=T.notch.autoTimer,timer=scheduledTimers.get(timerId);
  assert.ok(timer);assert.equal(timer.delay,3000);
  scheduledTimers.delete(timerId);timer.callback();assert.equal(T.notch.expanded,false);assert.equal(T.notch.autoTimer,null);
  return timerId;
}
T.render(lifecycleSnapshot);assert.equal(T.notch.expanded,true);expireReveal(); // first active chat
assert.equal(node('#search-toggle').hidden,false,'a valid active snapshot reveals search');
T.render({...lifecycleSnapshot,providers:[{...provider,chats:lifecycleChats.map(chat=>({...chat,updatedAt:ago(0),usageUpdatedAt:ago(0),latestMessageAt:ago(0),latestMessageDirection:'assistant-analysis'}))}]});
assert.equal(T.notch.expanded,false);assert.equal(T.notch.autoTimer,null); // ordinary activity/transcript updates do not reveal again
let lifecycleNext=lifecycleChats.map(chat=>chat.id==='lifecycle-root'?{...chat,activeRunStartedAt:ago(1)}:chat);
T.render({...lifecycleSnapshot,providers:[{...provider,chats:lifecycleNext}]});assert.equal(T.notch.autoTimer,null); // a new turn in a running chat does not pop the widget
lifecycleNext=lifecycleNext.map(chat=>chat.id==='paused-worker'?{...chat,status:'running',activeRunStartedAt:ago(2)}:chat);
T.render({...lifecycleSnapshot,providers:[{...provider,chats:lifecycleNext}]});assert.equal(T.notch.expanded,true);expireReveal(); // previously inactive chat becomes active
lifecycleNext=[...lifecycleNext,{...child,id:'new-worker',parentId:'lifecycle-root',status:'running',activeRunStartedAt:ago(1),latestRequestAt:null}];
T.render({...lifecycleSnapshot,providers:[{...provider,chats:lifecycleNext}]});assert.equal(T.notch.expanded,true);expireReveal(); // a new subagent appears
lifecycleNext=lifecycleNext.map(chat=>chat.id==='new-worker'?{...chat,latestMessageAt:ago(0),latestMessageDirection:'parent-to-agent'}:chat);
T.render({...lifecycleSnapshot,providers:[{...provider,chats:lifecycleNext}]});assert.equal(T.notch.autoTimer,null); // a message to an active subagent does not pop the widget
{ // A subagent that stops for under 8 s is a flap; once it stays stopped, its finish reveals once.
  const finished=lifecycleNext.map(chat=>chat.id==='new-worker'?{...chat,status:'idle',stateDetail:'completed',activeRunStartedAt:null}:chat);
  T.state.liveSampleAccepted=false;
  const at=testNow,shot=chats=>({...lifecycleSnapshot,generatedAt:new Date(testNow).toISOString(),providers:[{...provider,chats}]});
  T.render(shot(finished));assert.equal(T.notch.autoTimer,null,'a fresh stop is not yet a finish');
  testNow=at+8001;
  T.render(shot(finished));assert.equal(T.notch.expanded,true,'a settled finish reveals');expireReveal();
  T.render(shot(finished));assert.equal(T.notch.autoTimer,null,'a finish reveals only once');
  T.render(shot(lifecycleNext));assert.equal(T.notch.expanded,true,'a finished subagent coming back reveals');expireReveal();
  testNow=at;T.render(shot(lifecycleNext));
}
{ // Activity never reorders rows: a quiet chat that becomes busiest keeps its place; a new chat goes on top.
  const row=(id,updated,parentId)=>({...root,id,parentId,status:'running',activeRunStartedAt:ago(1),updatedAt:ago(updated),usageUpdatedAt:ago(updated)});
  const keys=forest=>JSON.stringify(T.visibleRoots(forest).map(chat=>chat.id).filter(id=>id.startsWith('order-')));
  const first=T.buildForest([{...provider,chats:[row('order-a',1),row('order-b',5),row('order-b1',6,'order-b'),row('order-b2',7,'order-b')]}]);
  assert.equal(keys(first),'["order-a","order-b"]');
  const busy=T.buildForest([{...provider,chats:[row('order-a',3),row('order-b',0),row('order-b1',6,'order-b'),row('order-b2',0,'order-b')]}]);
  assert.equal(keys(busy),'["order-a","order-b"]','roots keep their order');
  assert.equal(busy.roots.find(chat=>chat.id==='order-b').children.map(chat=>chat.id).join(),'order-b1,order-b2','children keep their order');
  const added=T.buildForest([{...provider,chats:[row('order-a',3),row('order-b',0),row('order-c',2)]}]);
  assert.equal(JSON.parse(keys(added))[0],'order-c','a new chat goes on top');
}
T.setNotch(false);T.notch.activeKeys=null;T.notch.workStates.clear();
T.render(lifecycleSnapshot);const burstTimer=T.notch.autoTimer;
const burstChats=[...lifecycleChats,{...child,id:'burst-worker',parentId:'lifecycle-root',status:'running',activeRunStartedAt:ago(1)}];
T.render({...lifecycleSnapshot,providers:[{...provider,chats:burstChats}]});
assert.equal(T.notch.autoTimer,burstTimer,"a burst of new children shares the first three-second reveal deadline");
{
  T.state.query='worker is:running';node('#chat-search').value=T.state.query;
  T.render({...lifecycleSnapshot,providers:[{...provider,chats:lifecycleChats.map(chat=>({...chat,status:'idle',stateDetail:'completed',activeRunStartedAt:null}))}]});
  assert.equal(T.notch.active,0);assert.equal(T.notch.autoTimer,null,'the final active chat clears its pending reveal timer');
  assert.equal(T.notch.expanded,false,'the roster closes as soon as active work ends');
  assert.equal(node('#chats-head').hidden,true);assert.equal(node('#search-toggle').hidden,true);
  assert.equal(T.state.query,'');assert.equal(node('#chat-search').value,'','an inactive roster closes and clears search state');
   assert.match(node('#chat-list').innerHTML,/class="empty tree-empty">No active chats/);
  T.notch.manual=true;T.notch.inspecting=true;T.notch.keyboardInspecting=true;T.setNotch(true);
   assert.equal(T.notch.expanded,true,'manual inspection can open the empty roster');
   T.syncNotch(0,[]);assert.equal(T.notch.manual,true);assert.equal(T.notch.inspecting,true);assert.equal(T.notch.keyboardInspecting,true);
}
T.setNotch(false);T.notch.manual=false;T.notch.inspecting=false;T.notch.keyboardInspecting=false;T.notch.autoExpanded=false;T.notch.activeKeys=null;T.notch.workStates.clear();
const quotas=T.resetView(provider,"fiveHour");
assert.equal(quotas.usedText,"92% used");assert.equal(quotas.countdown,"25m 00s");assert.equal(quotas.tone,"err");
for(const [seconds,expected] of [[4659,"1h 17m"],[3600,"1h 00m"],[3599,"59m 59s"],[59,"59s"]]){
  assert.equal(T.resetView({...provider,fiveHour:{percent:92,resetAt:fixedNow/1000+seconds}},"fiveHour").countdown,expected);
}
assert.equal(T.resetView(provider,"weekly").countdown,"22h 13m","weekly countdowns show hours/minutes while the target is within a day");
assert.equal(T.resetView({...provider,weekly:{percent:78,resetAt:fixedNow/1000+3*86400+7260}},"weekly").countdown,"3d 2h","weekly countdowns show day/hour remaining for distant targets");
assert.equal(T.resetView({...provider,weekly:{percent:78,resetAt:fixedNow/1000-1}},"weekly").countdown,"due");
assert.equal(T.resetView({...provider,weekly:{percent:78},limitsUpdatedAt:new Date(fixedNow).toISOString()},"weekly").countdown,"");
assert.equal(T.watcherPopoverHTML('Codex'),'<p class="policy-notice" data-policy-warning hidden></p><button class="ghost" data-action="reset-settings">Reset action settings</button>');
assert.equal(T.watcherTone(null,'Codex').tip,'');
T.state.watcher={enabled:true,running:true,providers:{Codex:{phase:'<img src=x onerror=alert(1)>',lastTriggerAt:fixedNow/1000,lastVerifiedAt:fixedNow/1000,verificationEvidence:'usage_and_deadline'}}};
const watcherMarkup=T.watcherPopoverHTML('Codex');
assert.match(watcherMarkup,/&lt;img/);
assert.doesNotMatch(watcherMarkup,/<img/);
assert.match(watcherMarkup,/<span class="t-live">✓<\/span>/,'a verified greeting carries the verified marker');
T.state.watcher={enabled:true,running:true,providers:{Codex:{phase:"keepalive_scheduled",failedAttempts:0,unverifiedAttempts:2,lastError:"greeting was accepted but no new reset deadline was observed",nextGreetingAt:fixedNow/1000+600}}};
assert.equal(T.watcherTone(T.state.watcher,"Codex").tone,"attn",'an unconfirmed greeting needs attention');
T.state.watcher={enabled:true,running:true,persistenceError:"state persistence failed: disk full",providers:{Codex:{phase:"keepalive_scheduled"}}};
assert.match(T.watcherPopoverHTML("Codex"),/state persistence failed: disk full/,'persistence errors appear in the popover');
T.state.watcher=null;
const staleQuota=T.resetView({...provider,limitsUpdatedAt:ago(11)},"fiveHour");
{
  const saved={...provider,fiveHour:{percent:92,resetAt:fixedNow/1000+3600,lastKnown:true},weekly:{percent:null},limitsUpdatedAt:ago(1)};
  const view=T.resetView(saved,"fiveHour");
  assert.equal(view.measured,false);assert.equal(view.lastKnown,true);assert.equal(view.lastKnownAge,"1m ago");
  T.renderResets([saved]);
  assert.equal(node('#r0-fiveHour-meter').className,'meter last-known');
  // Routing alerts were removed, so last-known usage has nothing to seed.
  assert.equal(T.routingStates,undefined);assert.equal(T.evaluateRoutingAlerts,undefined);
  assert.equal(T.resetView({...saved,fiveHour:{...saved.fiveHour,resetAt:fixedNow/1000-1}},"fiveHour").hasValue,false);
  const previous=T.state.snapshot;
  T.state.snapshot={providers:[saved]};
  const incoming={providers:[{...saved,fiveHour:{percent:30,resetAt:fixedNow/1000+3600},weekly:{percent:null},limitsUpdatedAt:new Date(fixedNow).toISOString()}]};
  T.reconcileSnapshotQuotas(incoming,fixedNow);
  assert.equal(incoming.providers[0].fiveHour.percent,30);
  assert.equal(incoming.providers[0].fiveHour.lastKnown,undefined);
  T.state.snapshot=previous;
  const watched={...saved,name:'Fixture'};
  T.applyQuotaSamples({Fixture:{lastStatus:{checkedAt:new Date(fixedNow).toISOString(),five_hour_percent:33}}},1,[watched]);
  assert.equal(watched.fiveHour.percent,33);assert.equal(watched.fiveHour.lastKnown,undefined);
  assert.equal(watched.fiveHour.resetAt,undefined,'a new sample cannot inherit a restored reset');
  const partial={...saved,name:'Partial',weekly:{percent:20,resetAt:fixedNow/1000+7200},limitsUpdatedAt:ago(1)};
  T.applyQuotaSamples({Partial:{lastStatus:{checkedAt:new Date(fixedNow).toISOString(),five_hour_percent:null,weekly_percent:47,weekly_reset_at:fixedNow/1000+7200}}},1,[partial]);
  assert.equal(partial.fiveHour.percent,92);assert.equal(partial.fiveHour.lastKnown,true);
  assert.equal(partial._quotaWindowObservedAt.fiveHour,ago(1));
  assert.equal(partial.weekly.percent,47);assert.equal(partial._quotaWindowObservedAt.weekly,new Date(fixedNow).toISOString());
}
assert.equal(staleQuota.measured,false);assert.equal(staleQuota.lastKnown,true);assert.equal(staleQuota.usedLabel,"92%");assert.equal(staleQuota.lastKnownAge,"11m ago");assert.equal(staleQuota.countdown,"25m 00s");assert.equal(staleQuota.tone,"");assert.equal(staleQuota.stale,"");
assert.equal(T.resetView({...provider,limitsUpdatedAt:ago(11)},"weekly").countdown,"22h 13m");
T.renderResets([{...provider,limitsUpdatedAt:ago(11)}]);
assert.equal(node('#r0-fiveHour-left').innerHTML,'<span class="rr-value"><b>92%</b><i>11m ago</i></span>');
assert.equal(node('#r0-fiveHour-left').className,'rr-left last-known');assert.equal(node('#r0-fiveHour-left').title,'Last reported by Codex 11m ago');
assert.equal(node('#r0-fiveHour-row').title,'Last reported by Codex 11m ago');
assert.equal(node('#r0-fiveHour-meter').className,'meter last-known');assert.equal(node('#r0-fiveHour-meter').getAttribute('aria-valuetext'),'Last known: 92% used');assert.equal(node('#r0-fiveHour-bar').className,'last-known');
assert.notEqual(node('#r0-fiveHour-day').textContent,'');assert.notEqual(node('#r0-fiveHour-time').textContent,'');assert.notEqual(node('#r0-fiveHour-cd').textContent,'');
assert.doesNotMatch(node('#r0-fiveHour-cd').className,/unknown/);
{
  const staleAt=ago(110),claude={name:"Claude",connected:true,identityVerified:true,identityGeneration:"account-1",fiveHour:{percent:14,resetAt:fixedNow/1000+3600},weekly:{percent:27,resetAt:fixedNow/1000+6*86400},limitsUpdatedAt:staleAt};
  const fiveHour=T.resetView(claude,"fiveHour"),weekly=T.resetView(claude,"weekly");
  assert.equal(fiveHour.measured,false);assert.equal(fiveHour.lastKnown,true);assert.equal(fiveHour.usedLabel,"14%");assert.equal(fiveHour.lastKnownAge,"1h 50m ago");assert.equal(fiveHour.tone,"");
  assert.equal(fiveHour.tip,"Last reported by Claude 1h 50m ago");assert.notEqual(fiveHour.countdown,"");
  assert.equal(T.resetView({...claude,identityVerified:false},"fiveHour").tip,"Account unverified · Last reported by Claude 1h 50m ago");
  assert.equal(weekly.lastKnown,true);assert.equal(weekly.usedLabel,"27%");
  const nearWeeklyLimit=T.resetView({...claude,limitsUpdatedAt:ago(6*24*60+23*60),weekly:{percent:27,resetAt:fixedNow/1000+3600}},"weekly");
  assert.equal(nearWeeklyLimit.lastKnown,true);assert.equal(nearWeeklyLimit.lastKnownAge,"6d 23h ago");
  const expired=T.resetView({...claude,limitsUpdatedAt:ago(60),fiveHour:{percent:14,resetAt:fixedNow/1000-1}},"fiveHour");
  assert.equal(expired.measured,false);assert.equal(expired.lastKnown,false);assert.equal(expired.expired,true);assert.equal(expired.usedLabel,"reset");assert.equal(expired.hasValue,false);assert.equal(expired.countdown,"");
  T.renderResets([{...claude,limitsUpdatedAt:ago(60),fiveHour:{percent:14,resetAt:fixedNow/1000-1}}]);
  assert.equal(node('#r0-fiveHour-left').innerHTML,'<span class="rr-reset-label">reset</span>');
  assert.equal(node('#r0-fiveHour-meter').getAttribute('aria-valuenow'),null);assert.equal(node('#r0-fiveHour-meter').getAttribute('aria-hidden'),'true');assert.equal(node('#r0-fiveHour-bar').style.width,'0%');
  const tooOld=T.resetView({...claude,limitsUpdatedAt:ago(24*60+1),fiveHour:{percent:14,resetAt:fixedNow/1000+3600}},"fiveHour");
  assert.equal(tooOld.lastKnown,false);assert.equal(tooOld.expired,false);assert.equal(tooOld.hasValue,false);assert.equal(tooOld.usedPercent,null);assert.equal(tooOld.usedLabel,"");assert.equal(tooOld.countdown,"");
  const weeklyTooOld=T.resetView({...claude,limitsUpdatedAt:ago(7*24*60+1),weekly:{percent:27,resetAt:fixedNow/1000+3600}},"weekly");
  assert.equal(weeklyTooOld.lastKnown,false);assert.equal(weeklyTooOld.usedLabel,"");
  assert.equal(T.watcherTone(null,"Codex").tone,"mute","last-known quota does not change greeter state");
}
T.renderResets([provider]);
{
  const countdown=node('#r0-fiveHour-cd');let writes=0,value=countdown.textContent;
  Object.defineProperty(countdown,'textContent',{configurable:true,get(){return value;},set(next){writes++;value=next;}});
  T.renderResets([provider]);assert.equal(writes,0,'an unchanged quota countdown does not rewrite its DOM slot');
}
{
  const expires=fixedNow/1000+30*86400;
  assert.equal(T.bankedResetView(null).text,"");
  assert.equal(T.bankedResetView({available:0,items:[]}).text,"");
  const one=T.bankedResetView({available:1,items:[{title:"Full reset (Weekly + 5 hr)",expiresAt:expires}]});
  assert.equal(one.text,"↻ 1");assert.match(one.tip,/^1 banked reset: Full reset \(Weekly \+ 5 hr\) · expires /);
  assert.match(T.bankedResetView({available:2,items:[]}).tip,/^2 banked resets$/);
  const expiredOne=T.bankedResetView({available:2,items:[{title:"Old",expiresAt:fixedNow/1000-60},{title:"New",expiresAt:expires}]});
  assert.equal(expiredOne.text,"↻ 1","an expired reset in a cached snapshot is not counted");assert.doesNotMatch(expiredOne.tip,/Old/);
  assert.equal(T.bankedResetView({available:1,items:[{title:"Old",expiresAt:fixedNow/1000-60}]}).text,"","a lone expired reset hides the badge");
  T.renderResets([{...provider,bankedResets:{available:1,items:[{title:"Full reset (Weekly + 5 hr)",expiresAt:expires}]}}]);
  assert.equal(node('#r0-weekly-banked').textContent,"↻ 1");assert.equal(node('#r0-weekly-banked').hidden,false);
  assert.match(node('#r0-weekly-banked').title,/banked reset/);
  T.renderResets([provider]);
  assert.equal(node('#r0-weekly-banked').hidden,true,'the badge hides when the provider reports no banked resets');
}
const verifiedWatcher={enabled:true,running:true,providers:{Codex:{nextGreetingAt:fixedNow/1000+100,lastTriggerAt:ago(2),lastVerifiedAt:ago(1),verificationEvidence:"usage_and_deadline"}}};
assert.match(T.watcherTone(verifiedWatcher,"Codex").tip,/✓/);
for(const verificationEvidence of ["stable_deadline","unknown",undefined]){
  const unverified={...verifiedWatcher,providers:{Codex:{...verifiedWatcher.providers.Codex,verificationEvidence}}};
  assert.doesNotMatch(T.watcherTone(unverified,"Codex").tip,/✓/);
}
assert.equal(node("#r0-fiveHour-bar").style.width,"92%");
assert.equal(node("#r0-weekly-bar").style.width,"78%");
// Healthy quotas keep an empty tone (other code treats any tone as a signal); CSS paints them green.
for(const [used,tone] of [[0,""],[50,""],[51,"attn"],[80,"attn"],[81,"err"],[100,"err"]]){
  const view=T.resetView({fiveHour:{percent:used},limitsUpdatedAt:new Date(fixedNow).toISOString()},"fiveHour");
  assert.equal(view.usedText,used+"% used");assert.equal(view.tone,tone);
}

// Expanded height tracks visible list content and excludes hidden blocks and padding buffers.
T.render(snapshot);T.setNotch(true);
node(".titlebar").offsetHeight=30;node(".resets").offsetHeight=66;
node("#chats-head").offsetHeight=18;node("#chats-head").hidden=true;
node(".totals").offsetHeight=28;node(".totals").hidden=true;
const measuredList=node("#chat-list"),originalListQuerySelectorAll=measuredList.querySelectorAll;measuredList.scrollHeight=200;measuredList.children=[];
measuredList.querySelectorAll=selector=>selector===".chat"?[{offsetHeight:900}]:[];
assert.equal(T.notchHeight(),312,"the production refined rail plus actual list content sets the height without a buffer");
measuredList.scrollHeight=265;
assert.equal(T.notchHeight(),377,"wrapped rows increase height by their measured content growth");
node("#chats-head").hidden=false;
assert.equal(T.notchHeight(),377,"the closed search heading contributes no height");
T.state.searchOpen=true;
assert.equal(T.notchHeight(),395,"the visible search row contributes its measured height");
T.state.searchOpen=false;
node("#chats-head").hidden=true;measuredList.scrollHeight=0;measuredList.children=[{className:"empty",offsetHeight:40}];measuredList.querySelectorAll=()=>[];
assert.equal(T.notchHeight(),152,"a non-chat empty-state node contributes its real height");
measuredList.querySelectorAll=originalListQuerySelectorAll;
T.syncNotch(0);assert.equal(T.notch.expanded,false);
T.state.hiddenChats.add("Codex:root");T.render(snapshot);
assert.doesNotMatch(node("#chat-list").innerHTML,/Live parent/);
T.state.query="is:hidden";
assert.deepEqual(Array.from(T.visibleRoots(T.state.forest),chat=>chat.key),["Codex:root"],"is:hidden finds a dismissed root without exposing all hidden chats");
assert.equal(T.parseQuery("is:hidden").flags.has("hidden"),true);
T.state.query="";
const dismissedIdle={...root,status:"idle"};
T.state.query="is:hidden";T.state.searchOpen=true;
T.render({...snapshot,providers:[{...provider,chats:[dismissedIdle]}]});
assert.match(node("#chat-list").innerHTML,/Live parent/,"is:hidden can render a dismissed chat with no live activity");
T.state.query="";T.state.searchOpen=false;
T.state.hiddenChats.clear();

// Exact question dismissal survives live refresh, without answering or queueing.
const ask={...child,id:"ask",status:"needs-input",pendingQuestion:"May I continue?",pendingOptions:["Yes","No"],pendingQuestionId:"q1",attentionSince:ago(2)};
const repeatedAsk={...ask,pendingQuestionAt:ago(1)};
assert.equal(T.questionDismissToken({...repeatedAsk,updatedAt:ago(0)}),T.questionDismissToken({...repeatedAsk,updatedAt:ago(3)}),"ordinary activity does not change a question dismissal token");
assert.notEqual(T.questionDismissToken(repeatedAsk),T.questionDismissToken({...repeatedAsk,pendingQuestionAt:ago(0)}),"a repeated question timestamp starts a fresh dismissal identity");
const exactAsk={...ask,id:"exact-ask",parentId:"question-parent",pendingQuestion:'Continue <now>?',pendingQuestionAt:ago(2),pendingOptions:["Yes","No"]};
const questionParent={...root,id:"question-parent",status:"running"};
const questionSnapshot={...snapshot,providers:[{...provider,chats:[questionParent,exactAsk]}]};
T.render(questionSnapshot);
const questionRoot=T.state.forest.roots.find(chat=>chat.id==="question-parent");
assert.match(T.rowHTML(questionRoot),/runway-refined-status t-live[^>]*>.*?runway-state-word">Running/);
assert.doesNotMatch(T.rowHTML(questionRoot),/runway-waiting-link|>Waiting<|data-runway-slot="question"|>\?</,"a running parent keeps its own state and has no waiting-question control");
const actualQuestionChild=T.trayEntries(questionRoot,T.parseQuery("")).find(entry=>entry.key==="Codex:exact-ask").html;
assert.match(actualQuestionChild,/runway-refined-status t-attn[^>]*data-own-waiting="true"[^>]*>.*?runway-waiting-link[^>]*data-key="Codex:exact-ask"[^>]*>Waiting<\/button>/s,"a waiting child exposes its question action in the refined status column");
assert.match(actualQuestionChild,/runway-state-glyph is-waiting/);
const exactPanel=T.runwayQuestionPopover(T.state.forest.rows.find(chat=>chat.id==="exact-ask"));
assert.match(exactPanel,/runway-question-panel/);
assert.match(exactPanel,/Continue &lt;now&gt;\?/);
assert.doesNotMatch(exactPanel,/<now>/);
assert.match(exactPanel,/Queue this as a message for the next turn\. It will not answer the pending question\./);
assert.match(exactPanel,/data-action="close-popover"/);
const questionButton={dataset:{action:"runway-question",key:"Codex:exact-ask"},getBoundingClientRect:()=>({left:12,bottom:36})};
T.handleClick({target:{closest:selector=>selector==="[data-action]"?questionButton:null}});
assert.deepEqual(openRequests,["/api/open?thread=exact-ask"]);
assert.equal(T.state.popover,null);assert.equal(T.notch.manual,false);
const ownAsk={...questionParent,status:"needs-input",pendingQuestion:"Own question"};
T.render({...snapshot,providers:[{...provider,chats:[ownAsk,exactAsk]}]});
assert.match(T.rowHTML(T.state.forest.roots.find(chat=>chat.id==="question-parent")),/runway-refined-status t-attn[^>]*data-own-waiting="true"[^>]*>.*?runway-waiting-link[^>]*data-key="Codex:question-parent"[^>]*>Waiting<\/button>/s);
const ownButton={dataset:{action:"runway-question",key:"Codex:question-parent"}};
T.handleClick({target:{closest:selector=>selector==="[data-action]"?ownButton:null}});
assert.equal(openRequests.at(-1),"/api/open?thread=question-parent","the parent's own pending question takes precedence");
const noTextWait={...exactAsk,pendingQuestion:"",status:"needs-input"};
T.render({...snapshot,providers:[{...provider,chats:[questionParent,noTextWait]}]});
assert.match(T.rowHTML(T.state.forest.roots.find(chat=>chat.id==="question-parent")),/runway-refined-status t-live[^>]*>.*?runway-state-word">Running/);
assert.doesNotMatch(T.rowHTML(T.state.forest.roots.find(chat=>chat.id==="question-parent")),/runway-waiting-link|>Waiting<|data-runway-slot="question"/);
const noTextChild=T.trayEntries(T.state.forest.roots.find(chat=>chat.id==="question-parent"),T.parseQuery("")).find(entry=>entry.key==="Codex:exact-ask").html;
assert.match(noTextChild,/runway-waiting-link[^>]*data-action="runway-question" data-key="Codex:exact-ask"[^>]*>Waiting<\/button>/,"a waiting child remains directly openable without recorded question text");
assert.match(noTextChild,/runway-status-dot[^>]*aria-label="Waiting"/s,"waiting children retain their accessible status dot");
assert.equal((noTextChild.match(/>Waiting<\/button>/g)||[]).length,1,"a waiting child exposes one direct open control beside its name dot");
const staleButton={dataset:{action:"runway-question",key:"Codex:exact-ask"}};
T.render({...snapshot,providers:[{...provider,chats:[questionParent,{...noTextWait,status:"running"}]}]});
T.handleClick({target:{closest:selector=>selector==="[data-action]"?staleButton:null}});
assert.equal(openRequests.length,2,"resolved waiting indicators safely ignore stale clicks");
assert.ok(openRequests.every(url=>url.startsWith("/api/open?thread=")),"desktop Waiting text issues only exact-chat open requests");
const futureMessage={...child,parentId:"question-parent",latestMessageAt:new Date(fixedNow+5000).toISOString()};
assert.equal(T.runwayAgentHasNewMessage(futureMessage),false);
assert.match(T.runwayQuestionPopover({...exactAsk,pendingQuestionId:"q2"}),/Continue &lt;now&gt;\?/);
const asks={...snapshot,providers:[{...provider,chats:[root,ask]}]};
T.render(asks);
assert.match(node("#chat-list").innerHTML,/data-key="Codex:ask"[\s\S]*?runway-refined-status t-attn"[\s\S]*?data-action="runway-question" data-key="Codex:ask"/,"the waiting child row shows its Waiting control");

// Selecting a display uses the native API; refresh does not alter placement.
let selected="one";const nativeCalls=[];
context.window.pywebview={api:{
  get_monitors:async()=>[{id:"one",index:1,label:"Monitor",kind:"external",selected:selected==="one"},{id:"two",index:2,label:"Laptop",kind:"internal",selected:selected==="two"}],
  select_monitor:async id=>{nativeCalls.push(["select",id]);selected=id;},
  set_notch:async (...args)=>nativeCalls.push(["size",...args]),
  recenter:async()=>nativeCalls.push(["recenter"])
}};
nativeCalls.length=0;
await T.syncMonitors();assert.equal(node("#monitor-toggle").textContent,"Monitor");
assert.equal(node("#monitor-toggle").hidden,false);assert.equal(node("#monitor-toggle").disabled,false);
nativeCalls.length=0;
await T.toggleMonitor();assert.equal(node("#monitor-toggle").textContent,"Laptop");
assert.deepEqual(nativeCalls,[["select","two"]]);
const monitorGetter=context.window.pywebview.api.get_monitors;
context.window.pywebview.api.get_monitors=async()=>[{id:'\\\\.\\DISPLAY1',index:2,label:'Laptop',kind:'internal',selected:true}];
await T.syncMonitors();assert.equal(node('#monitor-toggle').textContent,'Laptop');
context.window.pywebview.api.get_monitors=async()=>[{id:'\\\\.\\DISPLAY2',index:1,label:'Monitor',kind:'external',selected:true}];
await T.syncMonitors();assert.equal(node('#monitor-toggle').textContent,'Monitor');
context.window.pywebview.api.get_monitors=async()=>[{id:'\\\\.\\DISPLAY2',index:1,label:'Monitor 1',selected:true}];
await T.syncMonitors();assert.equal(node('#monitor-toggle').textContent,'Monitor 1');
assert.equal(node('#monitor-toggle').hidden,true,"one confirmed display hides the monitor control");
let finishPendingMonitors;
context.window.pywebview.api.get_monitors=()=>new Promise(resolve=>{finishPendingMonitors=resolve;});
const pendingMonitorSync=T.syncMonitors();
assert.equal(node('#monitor-toggle').hidden,false);assert.equal(node('#monitor-toggle').disabled,true,"pending discovery keeps the control visible and disabled");
finishPendingMonitors([{id:'one',label:'Laptop',kind:'internal',selected:true}]);await pendingMonitorSync;
assert.equal(node('#monitor-toggle').hidden,true);
context.window.pywebview.api.get_monitors=async()=>({monitors:[{label:'missing identity'}]});
await T.syncMonitors();assert.equal(node('#monitor-toggle').hidden,false);assert.equal(node('#monitor-toggle').disabled,true);
assert.match(node('#monitor-toggle').title,/unavailable/i);
context.window.pywebview.api.get_monitors=async()=>{throw new Error('discovery failed');};
await T.syncMonitors();assert.equal(node('#monitor-toggle').hidden,false);assert.equal(node('#monitor-toggle').disabled,true);
context.window.pywebview.api.get_monitors=monitorGetter;await T.syncMonitors();
nativeCalls.length=0;
selected="one"; // native selection changed after a manual cross-monitor drag
const requests=[];
const refreshedSnapshot={...asks,providers:[{...provider,chats:[{...root,title:"Refreshed parent"},ask]}]};
requests.length=0;
context.fetch=async (url,options)=>{
  requests.push([url,options]);
  assert.equal(options.signal.aborted,false);
  if(url.startsWith("/api/stats"))return {ok:true,json:async()=>refreshedSnapshot};
  assert.equal(url,"/api/limit-reset");
  return {ok:true,json:async()=>({enabled:false,running:false,providers:{}})};
};
T.handleClick({target:{closest:s=>s==="[data-action]"?{dataset:{action:"refresh"}}:null}});
await new Promise(resolve=>setImmediate(resolve));
assert.match(requests[0][0],/[?&]refresh=1/);
assert.equal(requests[0][1].cache,"no-store");
assert.equal(requests[1][0],"/api/limit-reset");
assert.equal(T.state.snapshot,refreshedSnapshot);
assert.match(node("#chat-list").innerHTML,/Refreshed parent/);
assert.equal(T.state.loading,false);
assert.equal(T.state.failSince,null);
assert.equal(node("#monitor-toggle").textContent,"Monitor");
assert.ok(!nativeCalls.some(call=>["select","recenter","fit"].includes(call[0])));

// The real hover handlers protect header targets until the pointer enters content.
T.render({...asks,providers:[{...provider,chats:[]}]});
T.notch.manual=false;T.notch.inspecting=false;T.notch.keyboardInspecting=false;T.notch.autoExpanded=false;T.setNotch(false);
const widgetEvents=new Map(),buttonEvents=new Map();
node(".widget").addEventListener=(name,handler)=>widgetEvents.set(name,handler);
node(".titlebar").getBoundingClientRect=()=>({left:0,right:400,top:0,bottom:30});
for(const selector of ["#monitor-toggle","#dashboard-open"])
  node(selector).addEventListener=(name,handler)=>buttonEvents.set(selector+":"+name,handler);
const notchDocumentEvents=new Map();
context.document.addEventListener=(name,handler)=>notchDocumentEvents.set(name,handler);
context.window.addEventListener=()=>{};
T.setupNotch();await new Promise(resolve=>setImmediate(resolve));nativeCalls.length=0;
widgetEvents.get("pointerenter")({target:node(".widget"),clientX:100,clientY:15});
assert.equal(T.notch.hover,true);assert.equal(T.notch.expanded,false);
assert.equal(T.notch.contentHover,false);
T.syncNotch(1); // telemetry renders cannot turn a header hover into expansion
assert.equal(T.notch.expanded,false);
widgetEvents.get("pointermove")({target:{closest:selector=>selector===".titlebar"?node(".titlebar"):null},clientX:100,clientY:20});
assert.equal(T.notch.expanded,false);
assert.equal(nativeCalls.length,0); // refresh, monitor and drag targets do not move
widgetEvents.get("pointermove")({target:{closest:()=>null},clientX:100,clientY:60});
assert.equal(T.notch.expanded,false,"content entry waits for deliberate inspection dwell");
const passInspectTimer=T.notch.inspectTimer;assert.equal(scheduledTimers.get(passInspectTimer).delay,60);
widgetEvents.get('pointerleave')();assert.equal(scheduledTimers.has(passInspectTimer),false,"a rapid pass cancels its pending inspection");
scheduledTimers.get(T.notch.exitTimer).callback();assert.equal(T.notch.expanded,false);
T.notch.globalPointerX=80;T.notch.globalPointerY=60;
widgetEvents.get('pointerenter')({target:{closest:()=>null},clientX:100,clientY:60});
const firstInspectTimer=T.notch.inspectTimer;assert.equal(scheduledTimers.get(firstInspectTimer).delay,60);
widgetEvents.get('pointermove')({target:{closest:()=>null},clientX:105,clientY:61});
widgetEvents.get('pointermove')({target:{closest:()=>null},clientX:111,clientY:62});
assert.equal(T.notch.inspectTimer,firstInspectTimer,"continuous movement does not keep restarting the dwell timer");
scheduledTimers.get(firstInspectTimer).callback();
assert.equal(T.notch.expanded,true);
assert.equal(nativeCalls.filter(call=>call[0]==="size").length,1);
widgetEvents.get("pointermove")({target:{closest:()=>null},clientX:100,clientY:70});
assert.equal(nativeCalls.filter(call=>call[0]==="size").length,1);
widgetEvents.get("pointermove")({target:{closest:selector=>selector===".titlebar"?node(".titlebar"):null},clientX:100,clientY:20});
assert.equal(T.notch.hover,true);assert.equal(T.notch.contentHover,false);
T.syncNotch(1);
assert.equal(T.notch.expanded,true); // header interaction holds an already open roster
assert.equal(nativeCalls.filter(call=>call[0]==="size").length,1);
assert.ok(!buttonEvents.has("#notch-toggle:click")); // counts have no dropdown/click behavior
const widget=node('.widget');let widgetFocused=false;widget.hasAttribute=()=>false;widget.setAttribute=(key,value)=>widget.tabIndex=Number(value);widget.focus=()=>{widgetFocused=true;};
assert.equal(context.window.tokenTrackerInspect(),true);assert.equal(widgetFocused,true);assert.equal(T.notch.keyboardInspecting,true);
assert.equal(T.notch.expanded,true,"the native inspection hook opens and focuses the widget");
notchDocumentEvents.get('keydown')({key:'Escape'});assert.equal(T.notch.keyboardInspecting,false);assert.equal(T.notch.expanded,false,"Escape closes deliberate inspection");
T.setNotch(false);T.notch.activeKeys=null;T.notch.workStates.clear();T.notch.hover=false;T.notch.contentHover=false;
T.notch.globalPointerX=100;T.notch.globalPointerY=60;
T.render(lifecycleSnapshot);const stationaryAutoTimer=T.notch.autoTimer;
widgetEvents.get('pointerenter')({target:{closest:()=>null},clientX:100,clientY:60});
assert.equal(T.notch.inspectTimer,null,"geometry entering under a stationary pointer is not deliberate inspection");
scheduledTimers.get(stationaryAutoTimer).callback();assert.equal(T.notch.expanded,false);
widgetEvents.get('pointerleave')();scheduledTimers.get(T.notch.exitTimer).callback();
T.setNotch(false);T.notch.activeKeys=null;T.notch.workStates.clear();T.notch.hover=false;T.notch.contentHover=false;
T.notch.globalPointerX=100;T.notch.globalPointerY=60;
T.render(lifecycleSnapshot);const deliberateAutoTimer=T.notch.autoTimer;
widgetEvents.get('pointerenter')({target:{closest:()=>null},clientX:110,clientY:60});
const deliberateEntryTimer=T.notch.inspectTimer;assert.ok(deliberateEntryTimer,"pointer entry movement can request inspection during an automatic reveal");
scheduledTimers.get(deliberateEntryTimer).callback();assert.equal(T.notch.inspecting,true);
scheduledTimers.get(deliberateAutoTimer).callback();assert.equal(T.notch.expanded,true,"deliberate inspection survives the automatic deadline");
widgetEvents.get('pointerleave')();scheduledTimers.get(T.notch.exitTimer).callback();assert.equal(T.notch.expanded,false);
// Entering content during the new-work reveal leaves its timer running; the
// timer still collapses the widget even while the pointer remains inside.
T.setNotch(false);T.notch.activeKeys=null;T.notch.workStates.clear();
T.notch.hover=false;T.notch.contentHover=false;T.notch.globalPointerX=100;T.notch.globalPointerY=60;
T.render(lifecycleSnapshot);const contentAutoTimerId=T.notch.autoTimer;
assert.ok(scheduledTimers.has(contentAutoTimerId));
widgetEvents.get("pointerenter")({target:{closest:()=>null},clientX:100,clientY:60});
assert.equal(T.notch.contentHover,true);assert.equal(T.notch.expanded,true);
assert.equal(T.notch.autoTimer,contentAutoTimerId);assert.ok(scheduledTimers.has(contentAutoTimerId));
widgetEvents.get("pointermove")({target:{closest:()=>null},clientX:100,clientY:60});
assert.equal(T.notch.contentHover,true);assert.equal(T.notch.autoTimer,contentAutoTimerId);
assert.equal(T.notch.inspectTimer,null,"a stationary pointer does not convert an automatic reveal into inspection");
scheduledTimers.get(contentAutoTimerId).callback();
assert.equal(T.notch.expanded,false);assert.equal(T.notch.autoTimer,null);
// Ordinary hover expansion and Escape behavior are exercised below.
T.setNotch(false);assert.equal(T.notch.expanded,false);
const waitingOnly={...snapshot,providers:[{...provider,chats:[{...root,status:'idle',stateDetail:'completed'},ask]}]};
T.state.query='no match';T.render(waitingOnly);T.setNotch(false);
assert.equal(T.notch.counts.roots,1);assert.equal(T.notch.counts.total,1);assert.equal(T.notch.counts.waiting,1);
assert.equal(T.notch.active,1); // a finished parent with a waiting child remains expandable
widgetEvents.get('pointerleave')();T.notch.globalPointerX=80;T.notch.globalPointerY=60;
widgetEvents.get('pointerenter')({target:node('.widget'),clientX:100,clientY:60});
const waitingHoverTimer=T.notch.inspectTimer;assert.ok(waitingHoverTimer,"ordinary hover requires the deliberate inspection dwell");
scheduledTimers.get(waitingHoverTimer).callback();
assert.equal(T.notch.expanded,true);
notchDocumentEvents.get('keydown')({key:'Escape'});assert.equal(T.notch.expanded,false);
T.syncNotch(T.notch.active);assert.equal(T.notch.expanded,false); // a refresh cannot undo Escape
T.state.query='';T.render(waitingOnly);
assert.match(node('#chat-list').innerHTML,/>Waiting<\/button>/);
assert.doesNotMatch(node('#chat-list').innerHTML,/Agent waiting/);
T.render(refreshedSnapshot);

// Compact drag requests are serialized through the native drag session.
const dragEvents=new Map(),windowDragEvents=new Map(),dragCalls=[];
context.document.addEventListener=(name,handler,capture)=>{assert.equal(capture,true);dragEvents.set(name,handler);};
context.window.addEventListener=(name,handler,capture)=>{assert.equal(capture,true);windowDragEvents.set(name,handler);};
let releaseBegin;
context.window.pywebview.api.begin_drag=profile=>{dragCalls.push(["begin",profile]);return new Promise(resolve=>{releaseBegin=resolve;});};
context.window.pywebview.api.end_drag=(generation,commit)=>{dragCalls.push(["end",generation,commit]);return true;};
T.widgetLock.known=true;T.widgetLock.locked=false;
T.setupWindowDrag();
const dragTarget={closest:selector=>selector===".compact-toolbar.pywebview-drag-region,.compact-summary.pywebview-drag-region"?{}:null};
const controlTarget={closest:()=>({})};
const down=(target=dragTarget,button=0)=>dragEvents.get("mousedown")({target,button,clientX:100,clientY:15});
const up=()=>dragEvents.get("mouseup")({type:"mouseup",button:0});
const settle=()=>new Promise(resolve=>setImmediate(resolve));
down(controlTarget);down(dragTarget,2);await settle();assert.deepEqual(dragCalls,[]);
down();await settle();
up();
await settle();assert.deepEqual(dragCalls,[["begin","compact-v1"]]); // release cannot overtake unresolved begin
releaseBegin({accepted:true,generation:7});await settle();assert.deepEqual(dragCalls,[["begin","compact-v1"],["end",7,true]]);
windowDragEvents.get("mouseup")({type:"mouseup",button:0});await settle();assert.equal(dragCalls.length,2);
down();await settle();releaseBegin({accepted:true,generation:8});await settle();up();await settle();
assert.deepEqual(dragCalls.at(-1),["end",8,true]);
down();await settle();releaseBegin({accepted:true,generation:9});await settle();
dragEvents.get("pointercancel")({type:"pointercancel"});await settle();
assert.deepEqual(dragCalls.at(-1),["end",9,false]);

// Native lock authority freezes expansion, drag markers and monitor cycling.
let nativeLock={locked:false,expanded:false,revision:0},failLockWrite=false,deferLockWrite=false,releaseLockWrite=null,lockWriteArgs=null;
const lockedClass=()=>{
  const values=new Set(['pywebview-drag-region']);
  return {values,classList:{contains:value=>values.has(value),remove:value=>values.delete(value),add:value=>values.add(value)}};
};
const dragBrand=lockedClass(),dragFill=lockedClass();dragRegionNodes.push(dragBrand,dragFill);
context.window.pywebview.api.get_widget_lock=async()=>({...nativeLock});
context.window.pywebview.api.set_widget_lock=async(locked,expanded,height,expectedRevision)=>{
  lockWriteArgs=[locked,expanded,height,expectedRevision];nativeCalls.push(['lock',locked,expanded,height,expectedRevision]);if(failLockWrite)throw new Error('native lock write failed');
  if(expectedRevision!==nativeLock.revision)return {...nativeLock};
  nativeLock={locked,expanded,revision:nativeLock.revision+1};
  const accepted={...nativeLock};
  if(deferLockWrite)return await new Promise(resolve=>{releaseLockWrite=()=>resolve(accepted);});
  return {...nativeLock};
};
context.window.pywebview.api.recenter=async()=>{
  nativeCalls.push(['recenter']);nativeLock={locked:false,expanded:false,revision:nativeLock.revision+1};
  context.window.tokenTrackerSetWidgetLock({...nativeLock});return {...nativeLock};
};
await T.syncWidgetLock();
T.render(lifecycleSnapshot);T.notch.manual=true;T.notch.inspecting=true;T.setNotch(true);assert.equal(T.notch.expanded,true);
failLockWrite=true;T.handleClick({target:{closest:selector=>selector==='[data-action]'?{dataset:{action:'widget-lock'}}:null}});await settle();failLockWrite=false;
assert.equal(T.widgetLock.locked,false);assert.equal(node('#widget-lock').getAttribute('aria-pressed'),'false','failed native writes do not optimistically change the lock button');
assert.equal(await T.setWidgetLock(true),true);assert.deepEqual(lockWriteArgs,[true,true,null,nativeLock.revision-1]);
assert.equal(T.widgetLock.locked,true);assert.equal(node('#widget-lock').getAttribute('aria-pressed'),'true');
assert.match(node('#widget-lock').innerHTML,/M4 5V3\.8a2 2 0 0 1 4 0V5/,'the locked button uses the closed shackle icon');
assert.equal(dragBrand.classList.contains('pywebview-drag-region'),true);assert.equal(dragFill.classList.contains('pywebview-drag-region'),true,"compact position lock still allows deliberate dragging");
widgetEvents.get('pointerleave')();T.syncNotch(0,[]);
assert.equal(T.notch.expanded,true,'an open lock survives zero active work and pointer exit');assert.equal(T.notch.autoTimer,null);
assert.equal(await T.toggleMonitor(),undefined);assert.equal(nativeCalls.some(call=>call[0]==='select'),false,'monitor cycling is ignored while locked');
const newerRevision=nativeLock.revision+1;
nativeLock={locked:true,expanded:false,revision:newerRevision};assert.equal(T.applyWidgetLock({...nativeLock}),true);
assert.equal(T.applyWidgetLock({locked:false,expanded:true,revision:newerRevision+0.5}),false,'lock callbacks require a safe integer revision');
assert.equal(context.window.tokenTrackerInspect(),true,'manual inspection remains available while position is locked');
notchDocumentEvents.get('keydown')({key:'Escape'});assert.equal(T.notch.expanded,false,'Escape closes deliberate inspection while position remains locked');
T.setNotch(true);T.syncNotch(1,[{key:'Codex:locked-launch',active:true,subagent:true}]);
assert.equal(T.notch.expanded,true,'new work can reveal the widget while position is locked');assert.notEqual(T.notch.autoTimer,null);
assert.equal(T.applyWidgetLock({locked:false,expanded:false,revision:newerRevision-1}),false,'older native messages cannot restore stale lock state');
assert.equal(T.widgetLock.locked,true);
await T.setWidgetLock(false);assert.equal(T.widgetLock.locked,false);
assert.equal(dragBrand.classList.contains('pywebview-drag-region'),true);assert.equal(dragFill.classList.contains('pywebview-drag-region'),true,'unlock restores original drag-region class membership');
T.notch.active=1;T.setNotch(true);assert.equal(T.notch.expanded,true);
await T.setWidgetLock(true);assert.equal(T.widgetLock.locked,true);
const recenterTrace=nativeCalls.length;
await T.recenterWidget();assert.equal(T.widgetLock.locked,false);assert.equal(T.notch.expanded,true,'recenter changes position but preserves the inspected live roster');
assert.ok(nativeCalls.some(call=>call[0]==='recenter'));
assert.deepEqual(nativeCalls.slice(recenterTrace).filter(call=>call[0]==='lock'||call[0]==='recenter').map(call=>call[0]),['recenter'],'explicit recenter delegates unlocking and movement to the native authority');
nativeLock={locked:true,expanded:true,revision:nativeLock.revision+1};context.window.tokenTrackerSetWidgetLock({...nativeLock});
nativeLock={locked:false,expanded:false,revision:nativeLock.revision+1};await T.syncWidgetLock();
assert.equal(T.widgetLock.locked,false,'a native initiated recenter clears the frontend lock through authoritative state sync');
const revisionBeforePendingRecenter=nativeLock.revision;deferLockWrite=true;const lateLockAck=T.setWidgetLock(true);await settle();assert.equal(T.widgetLock.pending,true);
await T.recenterWidget();
assert.equal(nativeLock.locked,false);assert.equal(T.widgetLock.locked,false,'recenter reaches native while a lock write is pending');
assert.equal(nativeLock.revision,revisionBeforePendingRecenter+2,'the native recenter revision supersedes an accepted queued lock write');
releaseLockWrite();assert.equal(await lateLockAck,false);deferLockWrite=false;
assert.equal(T.widgetLock.locked,false,'a late acknowledgement from before recenter cannot reinstate the lock');
assert.match(page,/id="widget-lock"/);

// A failed first snapshot retries quickly while visible, without quota fetches until data exists.
T.state.snapshot=null;T.state.loading=false;T.state.refreshPending=false;T.state.bootstrapRetryMs=2000;
const bootstrapCalls=[];
const syncAge=node("#sync-age"),syncAttributes={};syncAge.setAttribute=(name,value)=>syncAttributes[name]=String(value);
let finishInitialStats;
context.fetch=async url=>{
  bootstrapCalls.push(url);
  if(url.startsWith("/api/stats?background=1"))return bootstrapCalls.filter(call=>call.startsWith("/api/stats?background=1")).length===1?new Promise(resolve=>{finishInitialStats=resolve;}):{ok:true,json:async()=>snapshot};
  assert.equal(url,"/api/limit-reset");return {ok:true,json:async()=>({enabled:false,running:false,providers:{}})};
};
const firstRefresh=context.window.tokenTrackerRefresh();await settle();
assert.equal(syncAge.textContent,"");assert.match(syncAge.className,/is-loading/);assert.equal(syncAttributes["aria-busy"],"true");
finishInitialStats({ok:false,status:503});await firstRefresh;
assert.equal(bootstrapCalls.filter(call=>call.startsWith("/api/stats?background=1")).length,1);
assert.equal(bootstrapCalls.filter(call=>call==="/api/limit-reset").length,0);
assert.doesNotMatch(syncAge.className,/is-loading/);assert.equal(syncAttributes["aria-busy"],"false");
assert.equal(scheduledTimers.get(T.state.bootstrapRetryTimer).delay,2000);
scheduledTimers.get(T.state.bootstrapRetryTimer).callback();
await settle();await settle();
assert.equal(bootstrapCalls.filter(call=>call.startsWith("/api/stats?background=1")).length,2);
assert.equal(bootstrapCalls.filter(call=>call==="/api/limit-reset").length,1);
assert.equal(T.state.snapshot,snapshot);
assert.equal(T.state.bootstrapRetryTimer,null);

console.log("Token Tracker Runway telemetry contracts passed");

// Stopped children under a running parent are hidden in runway mode, unless a descendant still runs.
{
  const live=(id,parent)=>({id,key:'Claude:'+id,provider:'Claude',...(parent?{parentKey:'Claude:'+parent}:{}),title:id,status:'running',executionStatus:'running',executionSource:'transcript',executionObservedAt:ago(1),runStartedAt:ago(60)});
  const stopped=(id,parent)=>({id,key:'Claude:'+id,provider:'Claude',parentKey:'Claude:'+parent,title:id,status:'idle',stateDetail:'stopped',retainedRun:true,stoppedAt:ago(60)});
  const keys=chats=>Array.from(T.trayEntries(T.buildForest([{name:'Claude',chats}]).roots[0],T.parseQuery('')),entry=>entry.key);
  assert.deepEqual(keys([live('lead'),live('kid','lead'),stopped('done','lead')]),['Claude:kid'],'stopped child under a running parent is hidden');
  assert.deepEqual(keys([live('lead'),stopped('mid','lead'),live('leaf','mid')]).sort(),['Claude:leaf','Claude:mid'],'a stopped middle agent stays while its descendant runs');
}
