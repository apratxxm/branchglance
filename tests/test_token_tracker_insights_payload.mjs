import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

// Live payload shape from /api/insights (contract): unpriced models, zero sessions, 30-day series.
const root=new URL("../token_tracker/static/",import.meta.url);
const source=fs.readFileSync(new URL("dashboard.js",root),"utf8");
const css=fs.readFileSync(new URL("dashboard.css",root),"utf8");
const page=fs.readFileSync(new URL("runway.html",root),"utf8");
const values=new Map();
let responseQueue=[];
const localStorage={getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,String(value))};
const document={querySelector(){return null;},querySelectorAll(){return [];},addEventListener(){}};
const window={__TOKEN_TRACKER_DASHBOARD_TEST__:true,addEventListener(){},TokenTrackerFormatModel:({model})=>String(model).replace(/^claude-(opus|sonnet|haiku)-([0-9.]+).*$/i,(_,family,version)=>family[0].toUpperCase()+family.slice(1)+" "+version).replace(/^gpt-([0-9.]+)-([a-z]+).*$/i,(_,version,family)=>"GPT-"+version+" "+family[0].toUpperCase()+family.slice(1))};
const context={window,document,Intl,Date,Math,Number,JSON,Map,Set,Array,Promise,localStorage,location:{origin:"http://127.0.0.1:8765"},requestAnimationFrame:f=>f(),setTimeout:(f)=>f(),fetch:async()=>responseQueue.shift()||{ok:false,status:404,json:async()=>({})}};
vm.createContext(context);vm.runInContext(source,context);
const dashboard=window.__tokenTrackerDashboardTest;

const today=new Date();today.setHours(12,0,0,0);
const dateOffset=days=>{const d=new Date(today.getTime()-days*86400000);return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;};
const series=(tokens)=>Array.from({length:30},(_,index)=>({date:dateOffset(29-index),tokens:tokens/30+index,apiEquivalentCost:null}));
const livePayload=(overrides={})=>({range:"30d",generatedAt:today.toISOString(),providers:[
  {name:"Claude",totals:{tokens:3.2e9,apiEquivalentCost:null,sessions:0,subagentsActive:0,activeHours:0,cacheHitRate:null},previous:null,series:series(3.2e9),models:[{name:"claude-opus-5.5",tokens:3.2e9,apiEquivalentCost:null}],limitPeaks:{fiveHour:null,weekly:null}},
  {name:"Codex",totals:{tokens:0.37e9,apiEquivalentCost:null,sessions:0,subagentsActive:0,activeHours:0,cacheHitRate:null},previous:null,series:series(0.37e9),models:[{name:"gpt-6-luna",tokens:0.37e9,apiEquivalentCost:null}],limitPeaks:{fiveHour:null,weekly:null}},
],...overrides});

// Load the payload through the real fetch path so the metric resolves exactly as in the widget.
responseQueue=[{ok:true,json:async()=>livePayload()}];
dashboard.state.metric="cost";dashboard.state.range="30d";
await dashboard.loadInsights();
assert.equal(dashboard.state.metric,"tokens","unpriced payload switches the toggle to Tokens");

// (1) Hero shows the tokens headline, never an empty or "not available" cost hero.
let html=dashboard.insightsHTML();
assert.match(html,/class="dash-hero"/);
assert.match(html,/<strong class="dash-hero-number">3\.57<\/strong><span class="dash-hero-unit">B tokens<\/span>/);
assert.match(html,/Tokens over 30 days/);
assert.doesNotMatch(html,/not available|dash-hero-currency/);

// (2) Chart renders from series in the active metric.
assert.match(html,/class="dash-chart"/);
assert.match(html,/area-fill claude/);assert.match(html,/area-fill codex/);
assert.equal((html.match(/class="dash-point"/g)||[]).length,30,"one hover point per series day");
assert.match(html,/Daily · tokens/);

// (3) Zero figures next to non-zero tokens are treated as missing, not shown as 0.
assert.match(html,/dash-stat-label">Tokens</);
assert.match(html,/dash-stat-value">3\.57B</);
assert.doesNotMatch(html,/Sessions|Subagents active|Active hours|Cache hit rate/);

// (4) Model table shows when rows exist; limit panel hidden when peaks are null; no empty lower band.
assert.match(html,/Model breakdown/);assert.match(html,/Opus 5\.5/);assert.match(html,/GPT-6 Luna/);
assert.doesNotMatch(html,/Limit peaks/);
assert.match(html,/class="dash-lower is-single"/);

// Models absent: the model table and empty panels are not rendered at all.
responseQueue=[{ok:true,json:async()=>livePayload({providers:livePayload().providers.map(p=>({...p,models:[]}))})}];
await dashboard.loadInsights();
html=dashboard.insightsHTML();
assert.doesNotMatch(html,/Model breakdown/);
assert.doesNotMatch(html,/dash-lower/,"no lower section when there are no models or peaks");
assert.match(html,/class="dash-chart"/);

// Limit peaks alone render their own panel (only when present).
responseQueue=[{ok:true,json:async()=>livePayload({providers:livePayload().providers.map((p,i)=>({...p,models:[],limitPeaks:i===0?{fiveHour:76,weekly:58}:{fiveHour:null,weekly:null}}))})}];
await dashboard.loadInsights();
html=dashboard.insightsHTML();
assert.match(html,/Limit peaks/);assert.match(html,/dash-lower is-single/);assert.match(html,/5-hour/);assert.match(html,/76%/);

// Non-zero sessions are shown.
responseQueue=[{ok:true,json:async()=>livePayload({providers:livePayload().providers.map((p,i)=>i===0?{...p,totals:{...p.totals,sessions:46,subagentsActive:18,activeHours:62}}:p)})}];
await dashboard.loadInsights();
html=dashboard.insightsHTML();
assert.match(html,/dash-stat-label">Sessions</);assert.match(html,/Subagents active/);assert.match(html,/dash-stat-value">62h</);

// Priced payload keeps cost as the hero and chart metric; the toggle is not forced to Tokens.
responseQueue=[{ok:true,json:async()=>livePayload({providers:livePayload().providers.map(p=>({...p,totals:{...p.totals,apiEquivalentCost:12.5},series:p.series.map(r=>({...r,apiEquivalentCost:1}))}))})}];
dashboard.state.metric="cost";dashboard.state.metricExplicit=true;
await dashboard.loadInsights();
assert.equal(dashboard.state.metric,"cost");
html=dashboard.insightsHTML();
assert.match(html,/dash-hero-currency/);assert.match(html,/Daily · API-equivalent cost/);

// Stats-only fallback with a cost-free window still shows tokens and no fake cost.
responseQueue=[{ok:false,status:404,json:async()=>({})},{ok:true,json:async()=>({generatedAt:today.toISOString(),providers:[{name:"Claude",tracked31Days:1200,cache:{percent:null},activity:[{date:dateOffset(0),value:1200}],modelUsage:[]}]})}];
dashboard.state.metric="cost";dashboard.state.range="24h";
await dashboard.loadInsights();
html=dashboard.insightsHTML();
assert.doesNotMatch(html,/dash-hero-currency/);
assert.match(html,/dash-hero-number">1\.2<\/strong><span class="dash-hero-unit">K tokens/);

// Partial cost: the cost hero stays, with a muted note naming the unpriced models (formatted names).
responseQueue=[{ok:true,json:async()=>livePayload({providers:livePayload().providers.map((p,i)=>({...p,totals:{...p.totals,apiEquivalentCost:i===0?9.25:null,costPartial:i===0,unpricedModels:i===0?["claude-haiku-5.5"]:[]},series:p.series.map((r,j)=>({...r,apiEquivalentCost:i===0&&j===0?1:null}))}))})}];
dashboard.state.metric="cost";dashboard.state.metricExplicit=true;
await dashboard.loadInsights();
html=dashboard.insightsHTML();
assert.match(html,/dash-hero-currency/);
assert.match(html,/<p class="dash-cost-note">Excludes Haiku 5\.5 \(no published price\)<\/p>/);
assert.doesNotMatch(html,/Excludes .*Excludes/);
// Complete pricing: no note.
responseQueue=[{ok:true,json:async()=>livePayload({providers:livePayload().providers.map(p=>({...p,totals:{...p.totals,apiEquivalentCost:4,costPartial:false,unpricedModels:[]},series:p.series.map(r=>({...r,apiEquivalentCost:0.1}))}))})}];
await dashboard.loadInsights();
assert.doesNotMatch(dashboard.insightsHTML(),/dash-cost-note/);

// Layout: no fixed section heights, so sections flow up instead of leaving blank bands.
for(const selector of ["dash-hero","dash-chart-section","dash-stats","dash-lower"]){
  const rule=css.match(new RegExp(`\\.${selector}\\{[^}]*\\}`))?.[0]||"";
  assert.doesNotMatch(rule,/(^|[;{])height:\d+px/,`.${selector} has no fixed height`);
}

// (5) Toolbar clock width follows the text in every compact notch mode, and loads after the per-mode widths.
const clockRule=css.match(/\[data-notch-mode\]\s+\.compact-toolbar #toolbar-clock\{[^}]*\}/)?.[0]||"";
assert.match(clockRule,/min-width:max-content/);assert.match(clockRule,/width:auto/);assert.match(clockRule,/white-space:nowrap/);
assert.ok(page.indexOf('href="runway_refined.css"')<page.indexOf('href="dashboard.css"'),"dashboard.css loads after the per-mode clock widths");

console.log("Insights payload contracts passed");
