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
assert.match(dashboard.insightsHTML(),/<span class="dash-hero-mag">B<\/span><\/strong><span class="sr-only"> tokens/);
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

// (3) Model table shows up to five rows, folding the rest into Other.
const many=Array.from({length:8},(_,index)=>({name:`claude-opus-${index}.5`,tokens:(8-index)*1e8,apiEquivalentCost:(8-index)}));
dashboard.state.insights=payload([{name:"Claude",totals:{tokens:2.8e9,apiEquivalentCost:28,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null},previous:null,series:series(2.8e9),models:many,limitPeaks:{fiveHour:null,weekly:null}}]);
dashboard.state.metric="cost";
let html=dashboard.insightsHTML();
assert.equal((html.match(/class="dash-model-row"/g)||[]).length,5,"top four models plus one Other row");
assert.match(html,/Other \(4\)/,"the models beyond the top four sum into one Other row");
const few=many.slice(0,5);
dashboard.state.insights=payload([{name:"Claude",totals:{tokens:2.8e9,apiEquivalentCost:28,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null},previous:null,series:series(2.8e9),models:few,limitPeaks:{fiveHour:null,weekly:null}}]);
html=dashboard.insightsHTML();
assert.equal((html.match(/class="dash-model-row"/g)||[]).length,5);
assert.doesNotMatch(html,/Other \(\d+\)/,"no Other row when every model fits");

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
assert.match(quota,/<th scope="col">5h<\/th><th scope="col">Week<\/th>/,"the quota head row names both windows");
assert.match(quota,/data-quota-part="five">2h 40m</,"the 5-hour column shows its countdown with no seconds");assert.match(quota,/data-quota-part="week">3d 2h</,"the week column shows its countdown");
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

// (10) Round 4: countdowns carry no seconds; work figures round on whole increments; a partly priced Other row says at least; a zero range keeps the two-column grid with one line on the left; first load mounts the skeleton.
const soon=Date.now()+(2*3600+40*60+30)*1000;
window.tokenTrackerQuotaNow=()=>[{name:"Claude",usedPercent:10,countdown:"2h 40m",resetTarget:soon,weekly:{usedPercent:5,countdown:"3d 2h",resetTarget:Date.now()+(3*86400+2*3600+30)*1000}}];
dashboard.state.insights=payload([provider("Claude")]);dashboard.state.range="30d";dashboard.state.metric="tokens";
let countdowns=dashboard.insightsHTML();
assert.match(countdowns,/data-quota-part="five">2h 40m</,"a reset time under a day reads hours and minutes");
assert.match(countdowns,/data-quota-part="week">3d 2h</,"a reset time of days reads days and hours");
assert.doesNotMatch(countdowns,/data-quota-part="(five|week)">[^<]*\d+s</,"no countdown shows seconds");
window.tokenTrackerQuotaNow=()=>[{name:"Claude",usedPercent:10,countdown:"46m 58s",resetTarget:1,weekly:{usedPercent:5,countdown:"05:12",resetTarget:2}}];
countdowns=dashboard.insightsHTML();
assert.match(countdowns,/data-quota-part="five">46m</,"a widget countdown with seconds drops them");
assert.match(countdowns,/data-quota-part="week">5m</,"an m:ss widget countdown reads whole minutes");

dashboard.state.ledger={rows:[{provider:"Claude",modelId:"claude-opus-5.5",displayName:"Opus 5.5",kind:"build",totalTokens:5350000,knownCostUsd:null,costComplete:false,unpricedTokens:5350000}]};
dashboard.state.ledgerRange="30d";dashboard.state.range="30d";
assert.match(dashboard.workHTML(dashboard.workData()),/>5\.4M</,"5,350,000 rounds to 5.4M on whole increments");
dashboard.state.ledger=null;

const manyModels=[1,2,3,4,5,6].map(i=>({name:`claude-zeta-${i}`,tokens:(10-i)*1e5,apiEquivalentCost:2}));
manyModels.push({name:"claude-zeta-7",tokens:3e4,apiEquivalentCost:1},{name:"claude-zeta-8",tokens:1e4,apiEquivalentCost:null});
dashboard.state.insights=payload([provider("Claude",{models:manyModels})]);dashboard.state.metric="tokens";
const otherRow=dashboard.insightsHTML();
assert.match(otherRow,/Other \(4\)/,"the four smallest models fold into Other");
assert.match(otherRow,/class="dash-model-name other"[^>]*tabindex="0" data-tip-html=/,"Other exposes its details to keyboard focus");
assert.match(otherRow,/≥ \$5\.00/,"an Other row with an unpriced constituent reads at least");
assert.match(otherRow,/At least \$5\.00; some usage is unpriced/,"the lower bound says why, for screen readers and as a title");

dashboard.state.insights={range:"7d",generatedAt:today.toISOString(),providers:[{name:"Claude",totals:{tokens:0,apiEquivalentCost:0,sessions:0,subagentsActive:0,activeHours:0,cacheHitRate:null},previous:null,series:series(0),models:[],limitPeaks:{fiveHour:null,weekly:null}}]};
dashboard.state.range="7d";dashboard.state.metric="tokens";
const zeroRange=dashboard.insightsHTML();
assert.match(zeroRange,/<section class="dash-lower is-zero"><div class="dash-lower-main"><p class="dash-zero-line">No usage /,"zero state: the one line sits in the left column where Models starts");
assert.match(zeroRange,/<\/p><\/div><div class="dash-side">/,"zero state: Quota keeps the right column");
assert.doesNotMatch(zeroRange,/dash-chart-section/,"zero state: no chart band");

dashboard.state.insights=payload([provider("Claude",{totals:{tokens:1e6,apiEquivalentCost:10,sessions:0,subagentsActive:0,activeHours:0,cacheHitRate:0}})]);
dashboard.state.range="30d";dashboard.state.metric="tokens";
const zeroStats=dashboard.insightsHTML();
assert.match(zeroStats,/<dt>Sessions<\/dt><dd>0<\/dd>/,"reported zero sessions render as zero with positive token usage");
assert.match(zeroStats,/<dt>Cache hit rate<\/dt><dd>0%<\/dd>/,"reported zero cache rate renders as 0%");
assert.match(zeroStats,/<dt>Active hours<\/dt><dd>0h<\/dd>/,"reported zero hours render as 0h");
dashboard.state.insights=payload([provider("Claude",{totals:{tokens:1e6,apiEquivalentCost:10,sessions:null,subagentsActive:1,activeHours:1,cacheHitRate:null}})]);
const missingStats=dashboard.insightsHTML();
assert.match(missingStats,/<dt>Sessions<\/dt><dd><span aria-label="Not available">/,"a null stat stays unavailable beside a reported stat");

dashboard.state.insights=null;dashboard.state.loading=true;dashboard.state.metric="tokens";
const loading=dashboard.insightsHTML();
assert.equal((loading.match(/dash-skel-row/g)||[]).length,4,"first load reserves four Models rows");
assert.match(loading,/dash-skel-period[\s\S]*dash-skel-head[\s\S]*dash-skel-summary[\s\S]*dash-skel-chart/,"first load reserves period, section head, summary and chart slots");
assert.match(loading,/Tokens over/,"first load shows the token eyebrow, not Cost");
dashboard.state.loading=false;

// (11) Round 5: the failure notice sits on the hero eyebrow row, out of the lower flow; the skeleton carries the final eyebrow; Activity moves left for four or fewer models; empty work cells read an em dash; a zero total takes the compact height.
dashboard.state.loading=false;dashboard.state.loadError=true;dashboard.state.insightsUnavailable=false;
dashboard.state.insights={range:"7d",generatedAt:today.toISOString(),providers:[provider("Claude")]};dashboard.state.range="7d";dashboard.state.metric="tokens";
const failed=dashboard.insightsHTML();
assert.match(failed,/<div class="dash-hero-meta">(?:(?!<\/div>)[\s\S])*class="dash-notice" role="status"/,"the failure notice sits on the hero eyebrow row");
assert.doesNotMatch(failed,/<p class="dash-status-line[^"]*" role="status">[^<]*Update failed/,"the failure notice is out of the lower flow");
const failureTime=today.toLocaleTimeString("en-US",{hour:"numeric",minute:"2-digit",hour12:true}).replace(/\u202f/g," ");
assert.ok(failed.includes(`title="Update failed. Data from ${failureTime}. Use Refresh to retry."`),"the notice title contains the full sentence");
assert.match(failed,new RegExp(`<span class="sr-only">\\. data from ${failureTime}\\. Use Refresh to retry\\.<\\/span>`),"screen-reader text includes the data time and retry hint");
assert.ok(failed.includes(`<span class="dash-fail-time" aria-hidden="true"> \u00b7 data from ${failureTime}</span>`),"the visible notice names the exact time its data is from");
assert.ok(failed.includes(`<span class="sr-only">. data from ${failureTime}. Use Refresh to retry.</span>`),"screen readers hear where the displayed data is from");
dashboard.state.loadError=false;

dashboard.state.insights=null;dashboard.state.loading=true;dashboard.state.range="7d";dashboard.state.metric="tokens";
const skeleton=dashboard.insightsHTML();
assert.match(skeleton,/<span class="dash-eyebrow">Tokens over 7 days<\/span>/,"the skeleton eyebrow matches the final template");
assert.match(skeleton,/<p class="dash-status-line dash-loading-status" role="status">Loading usage…<\/p>/,"the loading text is visible");
dashboard.state.metric="cost";
assert.match(dashboard.insightsHTML(),/API-equivalent cost · 7 days/,"the cost skeleton uses the cost eyebrow");
dashboard.state.metric="tokens";dashboard.state.loading=false;

dashboard.state.insights=payload([provider("Claude",{models:[1,2,3].map(i=>({name:`claude-act-${i}`,tokens:i*1e5,apiEquivalentCost:1}))})]);
dashboard.state.range="30d";dashboard.state.metric="tokens";
assert.match(dashboard.insightsHTML(),/<dl class="dash-summary">/,"four or fewer models: activity uses the full-width strip");
dashboard.state.insights=payload([provider("Claude",{models:[1,2,3,4,5].map(i=>({name:`claude-many-${i}`,tokens:i*1e5,apiEquivalentCost:1}))})]);
assert.match(dashboard.insightsHTML(),/<dl class="dash-summary">/,"more than four models: activity uses the full-width strip");assert.doesNotMatch(dashboard.insightsHTML(),/dash-activity/,"no Activity block");

dashboard.state.ledger={rows:[{provider:"Claude",modelId:"claude-opus-5.5",displayName:"Opus 5.5",kind:"build",totalTokens:2e6,knownCostUsd:null,costComplete:false,unpricedTokens:2e6},{provider:"Claude",modelId:"claude-sonnet-5.5",displayName:"Sonnet 5.5",kind:"review",totalTokens:1e6,knownCostUsd:null,costComplete:false,unpricedTokens:1e6}]};
dashboard.state.ledgerRange="30d";dashboard.state.range="30d";dashboard.state.metric="tokens";
const emptyWork=dashboard.workHTML(dashboard.workData());
assert.match(emptyWork,/class="dash-work-cell is-zero is-none" aria-label="No usage"><span aria-hidden="true">—<\/span>/,"an empty work cell shows an em dash labelled No usage");
dashboard.state.ledger=null;

dashboard.state.insights={range:"7d",generatedAt:today.toISOString(),providers:[{name:"Claude",totals:{tokens:0,apiEquivalentCost:0,sessions:0,subagentsActive:0,activeHours:0,cacheHitRate:null},previous:null,series:series(0),models:[],limitPeaks:{fiveHour:null,weekly:null}}]};
dashboard.state.range="7d";dashboard.state.tab="insights";dashboard.state.metric="tokens";
assert.equal(dashboard.compactPanel(),true,"a zero total takes the compact window height");
assert.match(dashboard.insightsHTML(),/<section class="dash-hero is-zero-hero">/,"the zero hero puts its chips on the figure row");

// (12) Round 5 C9: the By work cost grid marks unpriced cells with a faint dot labelled Unpriced, explains it once under the grid, and writes whole dollars from $10 up.
dashboard.state.ledger={rows:[{provider:"Claude",modelId:"claude-opus-5.5",displayName:"Opus 5.5",kind:"build",totalTokens:1e6,knownCostUsd:14.2,costComplete:true,unpricedTokens:0},{provider:"Claude",modelId:"claude-sonnet-5.5",displayName:"Sonnet 5.5",kind:"review",totalTokens:5e5,knownCostUsd:null,costComplete:false,unpricedTokens:5e5}]};
dashboard.state.ledgerRange="30d";dashboard.state.range="30d";dashboard.state.metric="cost";
const costGrid=dashboard.workHTML(dashboard.workData());
assert.match(costGrid,/aria-label="Unpriced" title="No published price">·<\/span>/,"an unpriced cost cell is a faint dot labelled Unpriced");
assert.match(costGrid,/<p class="dash-work-legend">· = no published price<\/p>/,"the unpriced marker is explained once under the grid");
assert.match(costGrid,/>\$14<\/span>/,"whole dollars from $10 up read without cents");
dashboard.state.ledger=null;
