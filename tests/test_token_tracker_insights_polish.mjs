import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const root=new URL("../token_tracker/static/",import.meta.url);
const source=fs.readFileSync(new URL("dashboard.js",root),"utf8");
const css=fs.readFileSync(new URL("dashboard.css",root),"utf8");
const values=new Map();
let responseQueue=[];
const localStorage={getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,String(value)),removeItem:key=>values.delete(key)};
const document={querySelector(){return null;},querySelectorAll(){return [];},addEventListener(){}};
const window={__TOKEN_TRACKER_DASHBOARD_TEST__:true,addEventListener(){},TokenTrackerFormatModel:({model})=>String(model).replace(/^claude-(opus|sonnet|haiku)-([0-9.]+).*$/i,(_,family,version)=>family[0].toUpperCase()+family.slice(1)+" "+version).replace(/^gpt-([0-9.]+)-([a-z]+).*$/i,(_,version,family)=>"GPT-"+version+" "+family[0].toUpperCase()+family.slice(1))};
const context={window,document,Intl,Date,Math,Number,JSON,Map,Set,Array,Promise,localStorage,AbortSignal:{timeout:ms=>({timeout:ms})},location:{origin:"http://127.0.0.1:8765"},requestAnimationFrame:f=>f(),setTimeout:f=>f(),clearTimeout(){},setInterval:()=>1,clearInterval(){},fetch:async url=>String(url).startsWith("/api/ledger")?{ok:false,status:503}:responseQueue.shift()||{ok:false,status:404,json:async()=>({})}};
vm.createContext(context);vm.runInContext(source,context);
const dashboard=window.__tokenTrackerDashboardTest;

// (1) Monotone curve: every segment stays between its end values and never leaves the data range.
function parse(d){
  const nums=d.replace(/[MC]/g," ").trim().split(/\s+/).map(Number);
  const pts=[];for(let i=0;i<nums.length;i+=2)pts.push([nums[i],nums[i+1]]);
  const segs=[];for(let i=1;i+2<pts.length;i+=3)segs.push([pts[i-1],pts[i],pts[i+1],pts[i+2]]);
  return {start:pts[0],segs};
}
const bezier=(p,t)=>(1-t)**3*p[0][1]+3*(1-t)**2*t*p[1][1]+3*(1-t)*t*t*p[2][1]+t**3*p[3][1];
const plot=values=>values.map((v,i)=>[i*10,100-v]);  // SVG y grows downward; value 0 sits on the floor at y=100
for(const data of [[0,0,10,0,0,4],[0,8,0,0,2,0,9],[3,3,3,3],[0,0,0,5,5,0,0]]){
  const points=plot(data);
  for(const reverse of [false,true]){
    const {segs,start}=parse(dashboard.curvePath(points,reverse));
    if(!reverse)assert.deepEqual(start,points[0]);
    for(const seg of segs){
      const lo=Math.min(seg[0][1],seg[3][1])-1e-6,hi=Math.max(seg[0][1],seg[3][1])+1e-6;
      for(let k=0;k<=40;k++){const y=bezier(seg,k/40);assert.ok(y>=lo&&y<=hi,`overshoot ${y} outside [${lo},${hi}] for ${JSON.stringify(data)}`);}
    }
    // Never below the floor (value 0) and never above the highest point.
    const ys=segs.flatMap(seg=>Array.from({length:21},(_,k)=>bezier(seg,k/20)));
    assert.ok(Math.max(...ys)<=100+1e-6,"no dip below the $0 floor");
    assert.ok(Math.min(...ys)>=100-Math.max(...data)-1e-6,"no overshoot above the highest point");
  }
}
// Spike case: the curve passes through its own data points.
const spike=parse(dashboard.curvePath(plot([0,0,10,0,0]),false));
assert.deepEqual(spike.segs.map(seg=>seg[3]),plot([0,0,10,0,0]).slice(1));
assert.equal(dashboard.curvePath([[0,5]]),"","single point draws nothing");
assert.ok(!/NaN/.test(dashboard.curvePath(plot([2,2,2]))),"flat series has no NaN");

// Partial helpers for the metric and model-cap tests.
const today=new Date();today.setHours(12,0,0,0);
const dateOffset=days=>{const d=new Date(today.getTime()-days*86400000);return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;};
const series=tokens=>Array.from({length:30},(_,index)=>({date:dateOffset(29-index),tokens:tokens/30+index,apiEquivalentCost:null}));
const codexUnpriced=()=>({name:"Codex",totals:{tokens:1e9,apiEquivalentCost:31.51,sessions:12,subagentsActive:0,activeHours:0,cacheHitRate:null,costPartial:true,unpricedModels:["gpt-6-luna","gpt-6.1-sol"]},previous:null,series:series(1e9),models:[{name:"gpt-6-luna",tokens:0.8e9,apiEquivalentCost:null},{name:"gpt-6.1-sol",tokens:0.16e9,apiEquivalentCost:null},{name:"gpt-6-codex",tokens:0.04e9,apiEquivalentCost:31.51}],limitPeaks:{fiveHour:null,weekly:null}});
const codexMostlyPriced=()=>({...codexUnpriced(),totals:{tokens:1e9,apiEquivalentCost:31.51,sessions:12,subagentsActive:0,activeHours:0,cacheHitRate:null,costPartial:true,unpricedModels:["gpt-6.1-sol"]},models:[{name:"gpt-6-codex",tokens:0.8e9,apiEquivalentCost:31.51},{name:"gpt-6.1-sol",tokens:0.2e9,apiEquivalentCost:null}]});
const payload=providers=>({range:"30d",generatedAt:today.toISOString(),providers});

// (2) Mostly-unpriced ranges default to Tokens; the user can still pick Cost and sees the partial note.
values.clear();
responseQueue=[{ok:true,json:async()=>payload([codexUnpriced()])}];
dashboard.state.metric="cost";dashboard.state.metricExplicit=false;
await dashboard.loadInsights();
assert.equal(dashboard.state.metric,"tokens","96% unpriced tokens default the toggle to Tokens");
assert.match(dashboard.insightsHTML(),/<span class="dash-hero-mag">B<\/span><\/strong><span class="dash-hero-unit">tokens/);
assert.doesNotMatch(dashboard.insightsHTML(),/dash-cost-note/);

dashboard.setMetric("cost");
assert.equal(dashboard.state.metric,"cost","the user can still choose Cost");
responseQueue=[{ok:true,json:async()=>payload([codexUnpriced()])}];
await dashboard.loadInsights();
assert.equal(dashboard.state.metric,"cost","an explicit Cost choice survives later loads");
const partial=dashboard.insightsHTML();
assert.match(partial,/dash-hero-currency/);
assert.doesNotMatch(partial,/Recorded subtotal \/ estimate/,"the subtotal line under the figure is removed");

// A reopen with the stored explicit Cost choice keeps Cost.
values.set("branchglanceInsightsMetric","cost");
responseQueue=[{ok:true,json:async()=>payload([codexUnpriced()])},{ok:true,json:async()=>payload([codexUnpriced()])}];
window.TokenTrackerDashboard.open({source:"Codex"});
await dashboard.loadInsights();
assert.equal(dashboard.state.metric,"cost");
values.clear();

// Mostly priced ranges keep Cost as the default.
values.clear();
responseQueue=[{ok:true,json:async()=>payload([codexMostlyPriced()])}];
dashboard.state.metric="cost";dashboard.state.metricExplicit=false;
await dashboard.loadInsights();
assert.equal(dashboard.state.metric,"cost","unpriced share is under half, so Cost stays the default");

// (3) Model table shows up to seven rows, which fit the fixed panel, with a "+N more" line beyond that; fewer rows show no line.
const many=Array.from({length:8},(_,index)=>({name:`claude-opus-${index}.5`,tokens:(8-index)*1e8,apiEquivalentCost:(8-index)}));
dashboard.state.insights=payload([{name:"Claude",totals:{tokens:2.8e9,apiEquivalentCost:28,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null},previous:null,series:series(2.8e9),models:many,limitPeaks:{fiveHour:null,weekly:null}}]);
dashboard.state.metric="cost";
let html=dashboard.insightsHTML();
assert.equal((html.match(/class="dash-model-row"/g)||[]).length,7,"top six models plus one Other row");
assert.match(html,/Other · 2 models/,"the models beyond the top six sum into one Other row");
const few=many.slice(0,5);
dashboard.state.insights=payload([{name:"Claude",totals:{tokens:2.8e9,apiEquivalentCost:28,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null},previous:null,series:series(2.8e9),models:few,limitPeaks:{fiveHour:null,weekly:null}}]);
html=dashboard.insightsHTML();
assert.equal((html.match(/class="dash-model-row"/g)||[]).length,5);
assert.doesNotMatch(html,/Other ·/,"no Other row when every model fits");

// (4) Insights and Settings never scroll: the shared body is clipped to the fixed panel at every range and on both tabs.
assert.match(css,/\.dashboard-content\{[^}]*overflow:hidden/);
assert.doesNotMatch(css,/\.dashboard-content\{[^}]*overflow:auto/);
assert.doesNotMatch(css,/data-tab="settings"\] \.dashboard-content\{[^}]*overflow:auto/);
assert.match(css,/\.dashboard-view :not\(\.dashboard-content\)\{scrollbar-width:none;\}/);

// (5) Plan multiple: rolling 30-day API-equivalent cost over the stored plan price, shown per provider in the legend. An unset price offers "Set plan" instead of assuming a price. Unpriced models give a lower bound, and unpriced providers are named, never a silent dash.
dashboard.state.month={providers:[
  {name:"Claude",totals:{costPartial:false},models:[],series:[{date:"2026-10-05",tokens:1e6,apiEquivalentCost:68}]},
  {name:"Codex",totals:{costPartial:true,unpricedModels:["gpt-6.1-terra"]},models:[],series:[{date:"2026-10-05",tokens:5e5,apiEquivalentCost:null}]}
]};
dashboard.state.monthError=false;
values.delete("branchglancePlanPrice.Claude");
let planCells=dashboard.planCellsHTML("Claude");
assert.match(planCells,/<button type="button" class="dash-plan-set" data-dashboard-action="set-plan" data-dashboard-plan-provider="Claude" aria-label="Set Claude monthly plan price">Set plan<\/button>/,"an unset plan price asks for a price instead of assuming $20");
assert.doesNotMatch(planCells,/×/,"no multiple is computed without a plan price");
assert.equal((planCells.match(/class="dash-plan-(cell|note-cell)"/g)||[]).length,2,"one action cell and one marker cell per row");
assert.doesNotMatch(dashboard.settingsHTML(),/placeholder=/,"no placeholder: an empty field is the unset cue");assert.match(dashboard.settingsHTML(),/id="dash-plan-price"[^>]*value=""/,"the unset plan input is empty");
values.set("branchglancePlanPrice.Claude","20");
planCells=dashboard.planCellsHTML("Claude");
assert.match(planCells,/>3\.4×</,"a stored $20 plan price gives the 3.4× multiple");
assert.doesNotMatch(planCells,/≥|some unpriced/,"fully priced provider has no lower-bound marker");
assert.equal((planCells.match(/class="dash-plan-(cell|note-cell)"/g)||[]).length,2,"one multiple cell and one marker cell per row");
assert.match(dashboard.planCellsHTML("Codex"),/>Unpriced</,"a provider with no priced cost is named, not dashed");
dashboard.state.month.providers[0].totals.unpricedModels=["claude-haiku-5-5"];
planCells=dashboard.planCellsHTML("Claude");
assert.match(planCells,/≥3\.4×/,"unpriced models make the multiple a lower bound");
assert.match(planCells,/some unpriced/);
dashboard.state.month.providers[0].totals.unpricedModels=[];
values.set("branchglancePlanPrice.Claude","40");
assert.match(dashboard.planCellsHTML("Claude"),/>1\.7×</,"the editable plan price is used");
dashboard.setPlanPrice("Claude","0");
assert.equal(dashboard.state.settingsError,true,"a plan price of zero is refused");
assert.equal(values.get("branchglancePlanPrice.Claude"),"40","the stored price is kept when the input is refused");
dashboard.setPlanPrice("Claude","abc");
assert.equal(dashboard.state.settingsError,true,"a non-number is refused");
dashboard.setPlanPrice("Claude","");
assert.equal(values.has("branchglancePlanPrice.Claude"),false,"an empty input clears the stored price");
assert.equal(dashboard.state.settingsError,false,"clearing is not an error");
assert.match(dashboard.planCellsHTML("Claude"),/data-dashboard-action="set-plan" data-dashboard-plan-provider="Claude"[^>]*>Set plan</,"a cleared price returns to Set plan");
values.set("branchglancePlanPrice.Claude","20");
assert.match(dashboard.planCellsHTML("Claude"),/>3\.4×</,"set 20 gives the multiple again");
dashboard.setPlanPrice("Claude","");
assert.match(dashboard.planCellsHTML("Claude"),/data-dashboard-action="set-plan" data-dashboard-plan-provider="Claude"[^>]*>Set plan</,"set 20, then clear, expects Set plan");
dashboard.state.monthError=true;
assert.match(dashboard.planCellsHTML("Claude"),/Unavailable/,"a failed rolling-30-day load says so");
dashboard.state.monthError=false;

// (6) Chart style switch: stacked by default, lines on request, remembered, and rendered as a two-button group in the chart header.
values.delete("branchglanceChartStyle");
let styled=dashboard.insightsHTML();
assert.match(styled,/data-dashboard-chart-style="stacked" aria-pressed="true"/);assert.match(styled,/data-dashboard-chart-style="lines" aria-pressed="false"/);
assert.doesNotMatch(styled,/class="dash-chart is-lines"/);
dashboard.setChartStyle("lines");
assert.equal(values.get("branchglanceChartStyle"),"lines","the chart style is remembered");
styled=dashboard.insightsHTML();
assert.match(styled,/data-dashboard-chart-style="lines" aria-pressed="true"/);assert.match(styled,/class="dash-chart is-lines"/);
dashboard.setChartStyle("area");assert.equal(values.get("branchglanceChartStyle"),"lines","unknown styles are ignored");
dashboard.setChartStyle("stacked");

// (7) Review round: "30d / plan" header, bars that follow the metric, provider chips, collapsed Highest usage.
styled=dashboard.insightsHTML();
assert.match(styled,/<i class="dash-model-bar[^"]*" aria-hidden="true"><em class="[a-z]+" style="width:100\.0%"/,"the largest row fills its bar; others follow a linear scale");
for(const p of dashboard.state.insights.providers)p.limitPeaks={fiveHour:100,weekly:100};
styled=dashboard.insightsHTML();
assert.match(styled,/dash-limits is-collapsed/);assert.doesNotMatch(styled,/dash-peaks-table/,"everything at 100% collapses Highest usage to one line");

// (8) Bars scale linearly: half the largest row's tokens draws half the width.
dashboard.state.insights=payload([{name:"Claude",totals:{tokens:3e8,apiEquivalentCost:null,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null},previous:null,series:series(3e8),models:[{name:"claude-opus-5.5",tokens:2e8,apiEquivalentCost:null},{name:"claude-haiku-5.5",tokens:1e8,apiEquivalentCost:null}],limitPeaks:{fiveHour:null,weekly:null}}]);
dashboard.state.metric="tokens";
assert.match(dashboard.insightsHTML(),/<em class="claude" style="width:50\.0%"/,"bars are linear: half the largest row is half the width");

// (9) Round 3: quota follows the provider chips and shows the weekly column; stamped hours follow the clock; work figures; partial pricing; one point in the tab order.
const provider=(name,extra={})=>({name,totals:{tokens:1e6,apiEquivalentCost:10,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null},previous:null,series:series(1e6),models:[],limitPeaks:{fiveHour:null,weekly:null},...extra});
window.tokenTrackerQuotaNow=()=>[{name:"Claude",usedPercent:40,countdown:"2h 40m",resetTarget:1,weekly:{usedPercent:20,countdown:"3d 2h",resetTarget:2}},{name:"Codex",usedPercent:62,countdown:"1h 5m",resetTarget:3,weekly:{usedPercent:null,countdown:"",resetTarget:null}}];
dashboard.state.insights=payload([provider("Claude"),provider("Codex")]);
dashboard.state.range="30d";dashboard.state.metric="tokens";
let quota=dashboard.insightsHTML();
assert.match(quota,/data-quota-part="percent">40%/,"the 5-hour figure is shown");
assert.match(quota,/5h · Week/,"the quota header names both windows");
assert.match(quota,/resets 2h 40m/,"the 5-hour sub-line shows its reset");
assert.match(quota,/resets 3d 2h/,"the weekly sub-line shows its reset");
assert.match(quota,/Not available/,"a provider without a weekly reading shows Not available");
dashboard.toggleProvider("Claude");
quota=dashboard.insightsHTML();
assert.doesNotMatch(quota,/data-quota-key="Claude"/,"quota rows exclude a hidden provider");
assert.match(quota,/data-quota-key="Codex"/,"the shown provider keeps its quota row");
dashboard.toggleProvider("Claude");

// Stamped hours (24h) follow the clock preference; the range line has no minutes on 12-hour and keeps hh:mm on 24-hour.
const hourlyRows=[{date:"2026-11-13T23:00:00",tokens:5e5,apiEquivalentCost:null},{date:"2026-11-14T22:00:00",tokens:5e5,apiEquivalentCost:null}];
dashboard.state.insights={range:"24h",generatedAt:today.toISOString(),providers:[provider("Claude",{series:hourlyRows})]};
dashboard.state.range="24h";dashboard.state.metric="tokens";
values.set("branchglanceClockFormat","12");
let hourly=dashboard.insightsHTML();
assert.match(hourly,/Nov 13, 11 PM – Nov 14, 10 PM/,"12-hour stamps read 11 PM with no minutes");
assert.doesNotMatch(hourly,/local time/,"the range line has no local-time suffix");
values.set("branchglanceClockFormat","24");
hourly=dashboard.insightsHTML();
assert.match(hourly,/Nov 13, 23:00 – Nov 14, 22:00/,"24-hour stamps read 23:00");
values.set("branchglanceClockFormat","12");

// Work table: one decimal from a million ("2.0M"), whole thousands below ("800K").
dashboard.state.range="30d";dashboard.state.metric="tokens";
dashboard.state.ledger={rows:[{provider:"Claude",modelId:"claude-opus-5.5",displayName:"Opus 5.5",kind:"build",totalTokens:2e6,knownCostUsd:null,costComplete:false,unpricedTokens:2e6},{provider:"Claude",modelId:"claude-opus-5.5",displayName:"Opus 5.5",kind:"review",totalTokens:8e5,knownCostUsd:null,costComplete:false,unpricedTokens:8e5}],coverage:{},service:{}};
dashboard.state.ledgerRange="30d";
const workTable=dashboard.workHTML(dashboard.workData());
assert.match(workTable,/>2\.0M</,"work table: a million or more has one decimal");
assert.match(workTable,/>800K</,"work table: thousands have no decimal");
dashboard.state.ledger=null;

// Partial pricing: the unit line names an excluded provider and partial models; partial model amounts carry a ≥ prefix.
dashboard.state.metric="cost";
dashboard.state.insights=payload([provider("Claude",{totals:{tokens:1e6,apiEquivalentCost:10,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null,costPartial:true,unpricedModels:["claude-haiku-5-5"]},models:[{name:"claude-opus-5.5",tokens:8e5,apiEquivalentCost:9,unpriced:true},{name:"claude-haiku-5.5",tokens:2e5,apiEquivalentCost:null}]}),provider("Codex",{totals:{tokens:1e6,apiEquivalentCost:null,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null}})]);
const partialHTML=dashboard.insightsHTML();
assert.match(partialHTML,/estimated · excl\. Codex · some models unpriced/,"the unit line names the excluded provider and the partial models");
assert.match(partialHTML,/≥ \$9\.00/,"a partially priced model row shows its amount with a ≥ prefix");
dashboard.state.insights=payload([provider("Claude",{totals:{tokens:1e6,apiEquivalentCost:10,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null,costPartial:true,unpricedModels:["claude-haiku-5-5"]}})]);
assert.match(dashboard.insightsHTML(),/<span class="dash-hero-unit">estimated · some models unpriced<\/span>/,"partial models alone give their own unit line");
dashboard.state.metric="tokens";

// Chart: exactly one point sits in the tab order (the roving tabindex).
dashboard.state.insights=payload([provider("Claude")]);
assert.equal((dashboard.insightsHTML().match(/class="dash-point" tabindex="0"/g)||[]).length,1,"exactly one chart point is in the tab order");
values.delete("branchglanceHiddenProviders");

console.log("Insights polish contracts passed");
