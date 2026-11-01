import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source=fs.readFileSync(new URL("../token_tracker/static/dashboard.js",import.meta.url),"utf8");
const values=new Map();
let requests=[];
let responseQueue=[];
const localStorage={getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,String(value))};
const document={querySelector(){return null;},querySelectorAll(){return [];},addEventListener(){}};
const window={__TOKEN_TRACKER_DASHBOARD_TEST__:true,addEventListener(){},TokenTrackerFormatModel:({model})=>String(model).replace(/^claude-(opus|sonnet|haiku)-([0-9.]+).*$/i,(_,family,version)=>family[0].toUpperCase()+family.slice(1)+" "+version).replace(/^gpt-([0-9.]+)-([a-z]+).*$/i,(_,version,family)=>"GPT-"+version+" "+family[0].toUpperCase()+family.slice(1))};
const context={window,document,Intl,Date,Math,Number,JSON,Map,Set,Array,Promise,localStorage,location:{origin:"http://127.0.0.1:8765"},fetch:async url=>{requests.push(String(url));return responseQueue.shift()||{ok:false,status:404,json:async()=>({})};}};
vm.createContext(context);vm.runInContext(source,context);
const dashboard=window.__tokenTrackerDashboardTest;
const today=new Date();today.setHours(12,0,0,0);
const dateOffset=days=>{const date=new Date(today.getTime()-days*86400000);return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,"0")}-${String(date.getDate()).padStart(2,"0")}`;};
const provider=(name,values={})=>({name,totals:{tokens:values.tokens??1000000,apiEquivalentCost:"cost" in values?values.cost:20,sessions:values.sessions??10,subagentsActive:values.subagentsActive??3,activeHours:values.activeHours??12,cacheHitRate:values.cacheHitRate??65},previous:"previous" in values?values.previous:{tokens:900000,apiEquivalentCost:18},series:[0,1,2].map((n)=>({date:dateOffset(n),tokens:(values.tokens??1000000)/(n+1),apiEquivalentCost:"cost" in values?values.cost:(20/(n+1))})),models:values.models??[{name:name==="Claude"?"claude-opus-5.5":"gpt-6-luna",tokens:500000,apiEquivalentCost:12}],limitPeaks:{fiveHour:55,weekly:70}});

dashboard.state.range="30d";dashboard.state.metric="cost";
dashboard.state.insights={generatedAt:today.toISOString(),providers:[provider("Claude"),provider("Codex")]};
const combined=dashboard.insightsData();
assert.equal(combined.totals.tokens,2000000);
assert.equal(combined.totals.apiEquivalentCost,40);
assert.equal(combined.totals.subagentsActive,6);
assert.equal(combined.series.length,3);
assert.equal(combined.models.length,2);
assert.equal(combined.previous.tokens,null,"previous is hidden beyond the 7-day range");
const insights=dashboard.insightsHTML();
assert.match(insights,/API-equivalent cost/);
assert.match(insights,/dash-hero-currency/);
assert.match(insights,/estimated/);
assert.match(insights,/Claude/);assert.match(insights,/Codex/);
assert.match(insights,/>Models</);assert.match(insights,/Highest usage/);
assert.match(insights,/<dt>Subagents<\/dt><dd>6<\/dd>/);
assert.doesNotMatch(insights,/vs previous/,"no period delta at 30 days");
assert.match(insights,/area-fill/);assert.match(insights,/area-edge/);
assert.match(insights,/data-tip-html=/);
assert.match(insights,/Opus 5\.5/);assert.match(insights,/GPT-6 Luna/);
assert.match(insights,/class="dash-model-name codex"/);assert.match(insights,/class="dash-model-name claude"/);

dashboard.state.range="7d";
assert.match(dashboard.insightsHTML(),/vs previous 7 days/,"delta shown when the API supplies previous totals");
dashboard.state.range="30d";

dashboard.state.metric="tokens";
const tokens=dashboard.insightsHTML();
assert.match(tokens,/M tokens/);
assert.match(tokens,/Daily · tokens/);

dashboard.state.insights={providers:[provider("Claude",{models:[{name:"claude-sonnet-5.5",tokens:800,apiEquivalentCost:2}]})]};
dashboard.state.metric="cost";
const single=dashboard.insightsHTML();
assert.doesNotMatch(single,/class="dash-split|dash-hero-providers|codex-color/,"a single provider gets no share bar, no repeated total and no empty provider entry");
assert.match(single,/dash-eyebrow">Claude · /,"a single provider is named in the hero label");
assert.match(single,/Sonnet 5\.5/);
assert.doesNotMatch(single,/GPT-/);

dashboard.state.insights={providers:[provider("Claude",{cost:null,previous:null,models:[{name:"claude-opus-5.5",tokens:800,apiEquivalentCost:null}]})]};
const unpricedHTML=dashboard.insightsHTML();
assert.match(unpricedHTML,/M tokens|K tokens|tokens<\/span>/,"unpriced totals show tokens instead of a cost");
assert.doesNotMatch(unpricedHTML,/dash-hero-currency/);

const fallback=dashboard.normalizeStats({generatedAt:today.toISOString(),providers:[{name:"Claude",tracked31Days:1200,cache:{apiEquivalentCost:1.2,percent:42},activity:[{date:dateOffset(0),value:1200}],modelUsage:[{model:"claude-haiku-5.5",tokens:1200}]}]});
dashboard.state.insights=fallback;dashboard.state.metric="cost";dashboard.state.range="30d";
const fallbackHtml=dashboard.insightsHTML();
assert.match(fallbackHtml,/dash-hero-currency">\$<\/span><strong class="dash-hero-number">1\.20/);
assert.doesNotMatch(fallbackHtml,/sessions|subagents|active/i);
assert.doesNotMatch(fallbackHtml,/>\?/);
assert.match(fallbackHtml,/<span class="dash-none" role="img" aria-label="No published price">—<\/span>/,"unpriced model costs are explicitly marked");
assert.match(fallbackHtml,/Haiku 5\.5/);
dashboard.state.range="24h";
const fallback24=dashboard.insightsHTML();
assert.doesNotMatch(fallback24,/Haiku 5\.5/,"stats-only fallback hides model totals outside the 31-day window");
dashboard.state.range="30d";

responseQueue=[{ok:false,status:404,json:async()=>({})},{ok:true,json:async()=>({providers:[{name:"Claude",tracked31Days:1200,cache:{apiEquivalentCost:1.2},activity:[]}]})}];
dashboard.state.insights=null;requests=[];await dashboard.loadInsights();
assert.deepEqual(requests,["/api/insights?range=30d","/api/stats?background=1"]);
assert.equal(dashboard.state.insights.providers[0].name,"Claude");

responseQueue=[{ok:true,json:async()=>({providers:[{name:"Claude",totals:{tokens:5,apiEquivalentCost:null},series:[],models:[]}]})}];
dashboard.state.metric="cost";
await dashboard.loadInsights();
assert.equal(dashboard.state.metric,"tokens","unpriced payloads default the toggle to Tokens");
dashboard.setMetric("cost");
assert.equal(dashboard.state.metric,"tokens","Cost stays disabled while no pricing is published");

dashboard.setRange("7d");
dashboard.state.insights={providers:[provider("Claude")]};
dashboard.setMetric("cost");dashboard.setMetric("tokens");
assert.equal(values.get("branchglanceInsightsRange"),"7d");
assert.equal(values.get("branchglanceInsightsMetric"),"tokens");
let clockValue=null;window.TokenTrackerSetClockFormat=value=>{clockValue=value;};
assert.equal(dashboard.setClockFormat("24"),true);
assert.equal(values.get("branchglanceClockFormat"),"24");
assert.equal(clockValue,"24");
assert.match(dashboard.settingsHTML(),/Clock format/);
assert.match(dashboard.settingsHTML(),/24-hour/);

dashboard.state.policy={policyFile:"D:\\work\\sample-app\\policy.json",providers:[{name:"Codex",show:true,collect:true,send:false},{name:"Claude",show:true,collect:false,send:false}]};
dashboard.state.monitors=[{id:"display-1",label:"Laptop",selected:true}];dashboard.state.settingsLoaded=true;
dashboard.state.identity={version:"1.0.0",build:"abc123"};
const settings=dashboard.settingsHTML();
assert.match(settings,/<p class="dash-setting-note">Version 1\.0\.0\+abc123<\/p>/);
dashboard.state.identity={version:"<script>",build:"&"};
assert.match(dashboard.settingsHTML(),/Version &lt;script&gt;\+&amp;/,"identity values are escaped");
dashboard.state.identity=null;
assert.doesNotMatch(dashboard.settingsHTML(),/Version /,"missing identity is not fabricated");
assert.match(settings,/Lock widget position/);assert.match(settings,/Keep widget open/);
assert.match(settings,/Current provider policy/);assert.match(settings,/Monitor/);assert.match(settings,/Recenter/);
assert.doesNotMatch(settings,/Launch at startup|Always on top|Quiet hours|Check now|Open logs/,"controls without a backend stay out of the UI");
assert.match(settings,/reset_actions\.html\?embedded=1/);assert.match(settings,/Change Claude tracking/);
assert.match(source,/open_reset_actions/);
assert.doesNotMatch(source,/X-Branchglance-Settings-Token|settingsToken/,"dashboard never reads or stores the Claude settings token");

console.log("Insights and Settings dashboard contracts passed");

// Accepted data stays labeled with its captured range during a later request.
let resolvePending;
dashboard.state.range="7d";
dashboard.state.metric="tokens";
responseQueue=[{ok:true,json:async()=>({range:"7d",providers:[provider("Claude")]})}];
await dashboard.loadInsights();
context.fetch=()=>new Promise(resolve=>{resolvePending=resolve;});
dashboard.state.range="30d";
const pending=dashboard.loadInsights();
assert.match(dashboard.insightsHTML(),/Tokens over 7 days/);
assert.match(dashboard.insightsHTML(),/Updating/);
resolvePending({ok:true,json:async()=>({range:"30d",providers:[provider("Claude")]})});
await pending;
assert.match(dashboard.insightsHTML(),/Tokens over 30 days/);

const missingProvider={name:"Codex",totals:{tokens:10,apiEquivalentCost:null},previous:{tokens:null,apiEquivalentCost:null},series:[{date:dateOffset(1),tokens:null,apiEquivalentCost:null}],models:[]};
const first=provider("Claude");
first.series=[{date:dateOffset(2),tokens:0,apiEquivalentCost:0},{date:dateOffset(1),tokens:5,apiEquivalentCost:1,costPartial:true},{date:dateOffset(0),tokens:10,apiEquivalentCost:2}];
dashboard.state.insights={range:"7d",providers:[first,missingProvider]};
dashboard.state.range="7d";dashboard.state.metric="tokens";
const gaps=dashboard.insightsHTML();
assert.match(gaps,/role="group" aria-label="Daily tokens/);
assert.match(gaps,/Missing provider data/);
assert.match(gaps,/Codex: unavailable/);
assert.doesNotMatch(gaps,/vs previous/,"different previous provider coverage has no delta");
dashboard.state.insights={range:"7d",providers:[first]};
dashboard.state.metric="cost";
const partial=dashboard.insightsHTML();
assert.match(partial,/partial estimate/);
assert.match(partial,/Recorded subtotal/);
dashboard.state.insights={range:"7d",providers:[{...first,totals:{...first.totals,tokens:0},series:[{date:dateOffset(0),tokens:0}]}]};
dashboard.state.metric="tokens";
assert.match(dashboard.insightsHTML(),/dash-chart/,"observed zero has a chart");

// A mismatched response cannot be accepted as the requested period.
dashboard.state.range="7d";dashboard.state.insights=null;
let replies=[{ok:true,json:async()=>({range:"30d",providers:[provider("Wrong")]})},{ok:true,json:async()=>({providers:[{name:"Fallback",tracked31Days:5,activity:[{date:dateOffset(0),value:5}]}]})}];
context.fetch=async()=>replies.shift();
await dashboard.loadInsights();
assert.equal(dashboard.state.insights.providers[0].name,"Fallback");
assert.match(dashboard.insightsHTML(),/Daily fallback/);

// Closing invalidates in-flight requests even if the network responds later.
let late;
context.fetch=()=>new Promise(resolve=>{late=resolve;});
const inflight=dashboard.loadInsights();
const before=dashboard.state.insights;
window.TokenTrackerDashboard.close();
late({ok:true,json:async()=>({range:"7d",providers:[provider("Late")]})});
await inflight;
assert.equal(dashboard.state.insights,before);

// Partial-provider cost charts retain priced data and disclose excluded providers.
dashboard.state.insights={range:"7d",providers:[
  {...provider("Claude"),series:[{date:dateOffset(2),apiEquivalentCost:1},{date:dateOffset(1),apiEquivalentCost:null},{date:dateOffset(0),apiEquivalentCost:2}]},
  {...provider("Codex",{cost:null}),series:[{date:dateOffset(2),apiEquivalentCost:null},{date:dateOffset(0),apiEquivalentCost:null}]}
]};
dashboard.state.range="7d";dashboard.state.metric="cost";
const scopedChart=dashboard.insightsHTML();
assert.match(scopedChart,/Daily estimate excludes Codex/);
assert.match(scopedChart,/Claude: \$1\.00/);
assert.match(scopedChart,/Recorded subtotal: \$1\.00/);
assert.match(scopedChart,/Excludes Codex: no recorded price/);
assert.match(scopedChart,/Recorded subtotal ·/);
assert.match(scopedChart,/Missing provider data/,"a missing bucket in the plotted provider remains unknown");
assert.doesNotMatch(scopedChart,/area-fill claude/,"isolated observations do not bridge a missing bucket");

dashboard.state.insights={range:"7d",providers:[{name:"Claude",totals:{tokens:null,apiEquivalentCost:null},series:[],models:[]}]};
assert.match(dashboard.insightsHTML(),/No recorded measurement available/);
dashboard.state.insights={range:"7d",providers:[]};
assert.match(dashboard.insightsHTML(),/No recorded usage in this range/);

// A failed refresh cannot replace a trusted period with lower-quality fallback data.
dashboard.state.insights={range:"7d",providers:[provider("Trusted")]};
const trusted=dashboard.state.insights;
context.fetch=async()=>({ok:false});
await dashboard.loadInsights();
assert.equal(dashboard.state.insights,trusted);
assert.match(dashboard.insightsHTML(),/Update failed/);
// Initial cached snapshot remains visibly marked as fallback after a failed request.
dashboard.state.insights=null;dashboard.state.snapshot={providers:[{name:"Cached",tracked31Days:7,activity:[]}]};
await dashboard.loadInsights();
assert.match(dashboard.insightsHTML(),/Insights unavailable · Daily fallback/);

// Exercise asynchronous cross-fades and focus/scroll without a browser.
const scheduled=new Map(),frames=[];let nextTimer=0;
const listeners={},classes=new Set(),tipClasses=new Set();
const content={classList:{add:x=>classes.add(x),remove:x=>classes.delete(x)}};
const tip={classList:{add:x=>tipClasses.add(x),remove:x=>tipClasses.delete(x)},style:{},setAttribute(k,v){this[k]=v;},getBoundingClientRect:()=>({width:100,height:40})};
const root={dataset:{},addEventListener:(name,fn)=>{listeners[name]=fn;}};
const docs={querySelector:sel=>({"#dashboard-content":content,"#dashboard-view":root,"#dash-tooltip":tip}[sel]||null),querySelectorAll:()=>[],addEventListener(){}};
const win={__TOKEN_TRACKER_DASHBOARD_TEST__:true,addEventListener(){},innerWidth:800,innerHeight:600};
const fake={window:win,document:docs,Intl,Date,Math,Number,JSON,Map,Set,Array,Promise,localStorage,location:{origin:"http://localhost"},fetch:async()=>({ok:false}),setTimeout:fn=>{const id=++nextTimer;scheduled.set(id,fn);return id;},clearTimeout:id=>scheduled.delete(id),setInterval:()=>1,clearInterval(){},requestAnimationFrame:fn=>frames.push(fn)};
vm.createContext(fake);vm.runInContext(source,fake);
const ui=win.__tokenTrackerDashboardTest;ui.state.open=true;ui.state.range="30d";ui.state.tab="insights";
ui.setRange("7d");ui.setRange("30d");
for(const fn of scheduled.values())fn();scheduled.clear();
assert.equal(ui.state.range,"30d","latest range cancels a pending fade");
win.TokenTrackerDashboard.selectTab("settings");win.TokenTrackerDashboard.selectTab("insights");
for(const fn of scheduled.values())fn();scheduled.clear();
assert.equal(ui.state.tab,"insights","latest tab cancels a pending fade");
win.TokenTrackerDashboard.render();
const point={getAttribute:()=>"<b>Focused</b>",getBoundingClientRect:()=>({right:50,bottom:50})};
point.closest=()=>point;docs.activeElement=point;
listeners.focusin({target:point});
assert.equal(tip["aria-hidden"],"false");
listeners.scroll();
assert.equal(tip["aria-hidden"],"true");
frames.splice(0).forEach(fn=>fn());
assert.equal(tip["aria-hidden"],"false","focused tooltip returns after scroll");
listeners.scroll();docs.activeElement=null;
frames.splice(0).forEach(fn=>fn());
assert.equal(tip["aria-hidden"],"true","scroll cannot restore a stale tooltip");

// Existing fallback refreshes from a newer stats snapshot, retaining it if the next refresh fails.
dashboard.state.insights=dashboard.normalizeStats({providers:[{name:"Old",tracked31Days:2,activity:[]}]});
dashboard.state.range="30d";
let fallbackResponses=[{ok:false},{ok:true,json:async()=>({providers:[{name:"New",tracked31Days:3,activity:[]}]})}];
context.fetch=async()=>fallbackResponses.shift();
await dashboard.loadInsights();
assert.equal(dashboard.state.insights.providers[0].name,"New");
const latestFallback=dashboard.state.insights;
context.fetch=async()=>({ok:false});
await dashboard.loadInsights();
assert.equal(dashboard.state.insights,latestFallback);
assert.equal(dashboard.state.loadError,true);

dashboard.state.insights={range:"30d",providers:[
  {...provider("Claude"),limitPeaks:{fiveHour:50,weekly:null}},
  {...provider("Codex"),limitPeaks:{fiveHour:null,weekly:80}}
]};
const summary=dashboard.insightsHTML();
assert.match(summary,/<table class="dash-peaks-table">/);
assert.match(summary,/Highest recorded quota usage in this period, not current allowance/);
assert.match(summary,/<th scope="row" class="codex-color">Codex<\/th><td><span aria-label="Not recorded">—<\/span><\/td><td>80%/);
assert.match(summary,/<th scope="row" class="claude-color">Claude<\/th><td>50%<\/td><td><span aria-label="Not recorded">—/);

// Insights refresh: an identical payload keeps the rendered panel; the status line is inserted or updated without a full render.
{
  const panelDom={_html:"",renders:0,statusEl:null,
    get innerHTML(){return this._html;},set innerHTML(value){this.renders++;this._html=String(value);},
    insertAdjacentHTML(){this.statusEl={textContent:"",classList:{toggle(){}},remove(){this.statusEl=null;}};},
    querySelector(selector){return selector===".dash-status-line"?this.statusEl:null;}};
  const docsDom={querySelector:sel=>sel==="#dashboard-insights"?panelDom:null,querySelectorAll:()=>[],addEventListener(){}};
  const responses=[];
  const ctx={window:{__TOKEN_TRACKER_DASHBOARD_TEST__:true,addEventListener(){},TokenTrackerFormatModel:({model})=>String(model)},
    document:docsDom,Intl,Date,Math,Number,JSON,Map,Set,Array,Promise,localStorage,location:{origin:"http://localhost"},
    setTimeout:fn=>{fn();return 0;},clearTimeout(){},setInterval:()=>1,clearInterval(){},
    fetch:async()=>responses.shift()||{ok:false,status:404,json:async()=>({})}};
  vm.createContext(ctx);vm.runInContext(source,ctx);
  const ui=ctx.window.__tokenTrackerDashboardTest;
  ui.state.open=true;ui.state.range="30d";ui.state.metric="tokens";ui.state.tab="insights";
  const payload=()=>({range:"30d",generatedAt:"2026-10-30T10:00:00.000Z",providers:[
    {name:"Claude",totals:{tokens:1000,apiEquivalentCost:null,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null},previous:null,series:[{date:"2026-10-29",tokens:400,apiEquivalentCost:null},{date:"2026-10-30",tokens:600,apiEquivalentCost:null}],models:[],limitPeaks:null},
    {name:"Codex",totals:{tokens:500,apiEquivalentCost:null,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null},previous:null,series:[{date:"2026-10-30",tokens:500,apiEquivalentCost:null}],models:[],limitPeaks:null}]});
  responses.push({ok:true,json:async()=>payload()});
  await ui.loadInsights();
  assert.equal(panelDom.renders,1,"first load renders once");
  panelDom._html="SENTINEL";panelDom.renders=0;
  responses.push({ok:true,json:async()=>payload()});
  await ui.loadInsights();
  assert.equal(panelDom.renders,0,"identical payload does not rebuild the panel");
  assert.equal(panelDom._html,"SENTINEL","identical payload leaves panel content untouched");
  assert.ok(panelDom.statusEl,"status line still updated in place");
  // A missing status element is inserted, not re-rendered.
  panelDom.statusEl=null;panelDom.renders=0;
  responses.push({ok:true,json:async()=>payload()});
  await ui.loadInsights();
  assert.equal(panelDom.renders,0,"missing status line is inserted without a full render");
  assert.ok(panelDom.statusEl,"status line inserted");
  // A new generatedAt stamp alone is a refresh, not new data: no rebuild, the stamp moves forward.
  panelDom.renders=0;
  responses.push({ok:true,json:async()=>({...payload(),generatedAt:"2026-10-30T10:02:00.000Z"})});
  await ui.loadInsights();
  assert.equal(panelDom.renders,0,"timestamp-only refresh keeps the panel");
  assert.equal(ui.state.insights.generatedAt,"2026-10-30T10:02:00.000Z","freshness stamp is carried forward");
  // Changed content still rebuilds.
  const changed=payload();changed.providers[0]={...changed.providers[0],totals:{...changed.providers[0].totals,tokens:changed.providers[0].totals.tokens+1}};
  responses.push({ok:true,json:async()=>changed});
  await ui.loadInsights();
  assert.equal(panelDom.renders,1,"changed payload rebuilds");
}

// Stats fallback fills a day with no recorded row as 0; live /api/insights keeps the gap as null.
{
  const dayA="2026-10-29",dayB="2026-10-30";
  const fallbackState=dashboard.normalizeStats({providers:[{name:"A",tracked31Days:5,activity:[{date:dayA,value:5},{date:dayB,value:2}]},{name:"B",tracked31Days:3,activity:[{date:dayB,value:3}]}]});
  dashboard.state.insights=fallbackState;dashboard.state.resultValue=fallbackState;dashboard.state.resultRange="30d";dashboard.state.range="30d";
  const fallbackRows=dashboard.insightsData(false).seriesByKey.tokens;
  assert.equal(fallbackRows.find(row=>row.date===dayA)["B"],0,"fallback missing day is zero");
  const live={range:"30d",generatedAt:"2026-10-30T10:00:00.000Z",providers:[
    {name:"A",totals:{tokens:7,apiEquivalentCost:null},series:[{date:dayA,tokens:5},{date:dayB,tokens:2}]},
    {name:"B",totals:{tokens:3,apiEquivalentCost:null},series:[{date:dayB,tokens:3}]}]};
  dashboard.state.insights=live;dashboard.state.resultValue=live;
  const liveRows=dashboard.insightsData(false).seriesByKey.tokens;
  assert.equal(liveRows.find(row=>row.date===dayA)["B"],null,"live missing day stays null");
}

// One shown provider: Settings carries one quiet line naming the other; two shown providers: no line.
{
  dashboard.state.policy={providers:[{name:"Codex",show:true,collect:true,send:false},{name:"Claude",show:false,collect:false,send:false}]};
  assert.match(dashboard.settingsHTML(),/dash-add-provider">Add Claude to track its agents too\./);
  dashboard.state.policy={providers:[{name:"Codex",show:true,collect:true,send:false},{name:"Claude",show:true,collect:true,send:false}]};
  assert.doesNotMatch(dashboard.settingsHTML(),/dash-add-provider/);
}
