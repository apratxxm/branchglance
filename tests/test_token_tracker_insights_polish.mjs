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
assert.match(html,/Other \(4 models\)/,"the models beyond the top four sum into one Other row");
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
assert.match(css,/#dashboard-view\{[^}]*--d-right:232px/,"Insights and Settings share the fixed right rail");
assert.match(css,/\.dash-hero\{[^}]*grid-template-columns:minmax\(0,1fr\) var\(--d-right\)/,"hero providers use the shared right rail");
assert.match(css,/\.dash-hero\.dash-hero-loading\{[^}]*grid-template-columns:minmax\(0,1fr\) var\(--d-right\)/,"loading providers reserve the shared right rail");
assert.match(css,/\.dash-lower\{[^}]*grid-template-columns:minmax\(0,1fr\) var\(--d-right\)/,"lower Insights sections use the shared right rail");
assert.match(css,/\.dash-model-name\{[^}]*color:var\(--d-ink\);text-shadow:none/ ,"Models and By-work names are plain neutral text");
assert.doesNotMatch(css,/\.dash-model-name\.(claude|codex)\{color:|\.dash-model-name\.(claude|codex)\{[^}]*text-shadow/ ,"provider colours and glow stay off model names");
assert.match(css,/\.dash-model-name\.claude i\{background:var\(--d-claude\)/,"Claude model dots retain their provider colour");
assert.match(css,/\.dash-model-name\.codex i\{background:var\(--d-codex\)/,"Codex model dots retain their provider colour");
assert.match(css,/\.dash-peaks-table tbody th\{[^}]*color:var\(--d-ink\);text-shadow:none/ ,"Quota provider names are plain neutral text");
assert.doesNotMatch(css,/\.dash-peaks-table tbody th\.(claude|codex)-color\{[^}]*text-shadow/ ,"Quota provider names have no glow");
assert.match(css,/\.dash-legend-name\{color:var\(--d-ink\)[^}]*text-shadow:none/ ,"hero legend names are plain neutral text");
assert.doesNotMatch(css,/\.(claude|codex)-color \.dash-legend-name\{/ ,"provider colours and glow stay off hero legend names");
assert.match(css,/\.dash-legend-item \.dash-legend-name::before\{[^}]*border-top:2px solid var\(--d-codex\)/,"Codex keeps the provider-coloured legend line");
assert.match(css,/\.dash-legend-item\.claude-color \.dash-legend-name::before\{[^}]*border-top-color:var\(--d-claude\)/,"Claude keeps the provider-coloured dashed legend line");
assert.match(css,/\.dash-plan-set\{[^}]*color:var\(--d-sub\)/ ,"Set plan is a quiet text button");
assert.match(css,/\.dash-plan-set:hover,\.dash-plan-set:focus-visible\{text-decoration:underline;\}/ ,"Set plan underlines only on hover or focus");
assert.match(css,/\.dash-plan-set:focus-visible\{outline:2px solid var\(--d-codex-text\);outline-offset:2px;\}/ ,"Set plan uses the dashboard's Codex focus ring");
assert.match(css,/\.dash-summary\{[^}]*grid-template-columns:repeat\(2,minmax\(0,1fr\)\) repeat\(2,98px\);column-gap:36px/,"activity aligns two columns left and keeps two narrow stat columns");
assert.match(css,/\.dash-work-row\{[^}]*minmax\(105px,1fr\) repeat\(var\(--work-cols\),52px\) 56px;column-gap:8px/,"four By work kinds use 52px columns and a 56px Total");
const gridColumns=css.match(/\.dash-work-row\{[^}]*minmax\((\d+)px,1fr\) repeat\(var\(--work-cols\),(\d+)px\) (\d+)px;column-gap:(\d+)px/);
assert.ok(gridColumns,"By work grid sizing is explicit in CSS");
const gridMinimum=Number(gridColumns[1])+4*Number(gridColumns[2])+Number(gridColumns[3])+5*Number(gridColumns[4]);
const leftColumnWidth=720-20-232-36-20;
assert.equal(gridMinimum,409,"By work grid minimum includes four kind columns and five gaps");
assert.ok(gridMinimum<=leftColumnWidth,`By work grid minimum ${gridMinimum}px fits the ${leftColumnWidth}px left column`);
assert.match(css,/\.dash-work-row\.is-head\{[^}]*color:var\(--d-mute\)[^}]*font-size:11px[^}]*line-height:14px/ ,"By work heads use the 11px by 14px muted style");
assert.doesNotMatch(css,/\.dash-work-row\.is-head[^}]*8\.5px/,"By work has no 8.5px head style");
assert.match(css,/\.dash-notice\{[^}]*line-height:18px/,"the failure notice fits the 18px hero meta row");
assert.match(css,/\.dash-fail\{[^}]*color:var\(--d-ink\);font-weight:600/,"the failure label uses strong neutral error styling");
assert.match(css,/\.dash-status-line\.error\{[^}]*color:var\(--d-ink\);font-weight:600/,"Insights status errors use strong neutral error styling");
assert.match(css,/\.dash-setting-error\{[^}]*color:var\(--d-ink\)[^}]*font-weight:600/,"Settings errors use strong neutral error styling");
assert.match(css,/\.dash-work-cell\.is-none\[aria-label="Unpriced"\]\{color:var\(--d-sub\);font-weight:700;\}/,"unpriced By work markers use the specified stronger muted style");
assert.match(css,/\.dash-work-row\.is-head>span,\.dash-work-cell\{[^}]*font-variant-numeric:tabular-nums/ ,"By work cells use tabular numerals");
assert.match(css,/\.dash-work-note\.is-qualified\{color:var\(--d-sub\);font-size:12px;line-height:16px/ ,"all ledger status notes use one quiet 12px line");
assert.match(css,/\.dash-work-note\.is-qualified strong\{font-weight:600;\}/,"Partial is the only bold label in the ledger note");
assert.match(css,/\.dash-status-line\.is-freshness\{[^}]*position:static;flex:0 0 22px[^}]*color:var\(--d-mute\);font-size:12px[^}]*text-align:right/ ,"freshness has a reserved, right-aligned line below the plot");
assert.match(css,/\.dash-plot\{[^}]*min-height:140px/ ,"moving freshness below the plot keeps the chart's 140px minimum");

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
// (6) A single provider has one rendered series, so the chart-style switch is omitted.
values.delete("branchglanceChartStyle");
let styled=dashboard.insightsHTML();
assert.doesNotMatch(styled,/class="dash-chart-style"/,"Lines/Stacked is hidden when one provider is visible");
assert.match(styled,/class="dash-chart(?: is-lines)?"/,"the single-provider chart remains rendered");

// With two visible providers, chart-style choice remains available and remembered.
const soloProvider=dashboard.state.insights.providers[0];
dashboard.state.insights=payload([soloProvider,{...soloProvider,name:"Codex"}]);
values.delete("branchglanceChartStyle");
styled=dashboard.insightsHTML();
assert.match(styled,/data-dashboard-chart-style="stacked" aria-pressed="true"/,"stacked remains the default with multiple providers");
assert.match(styled,/data-dashboard-chart-style="lines" aria-pressed="false"/);
dashboard.setChartStyle("lines");
assert.equal(values.get("branchglanceChartStyle"),"lines","Lines is remembered with multiple providers");
styled=dashboard.insightsHTML();
assert.match(styled,/data-dashboard-chart-style="lines" aria-pressed="true"/);
assert.match(styled,/class="dash-chart is-lines"/,"Lines renders the line-chart class");
dashboard.setChartStyle("area");
assert.equal(values.get("branchglanceChartStyle"),"lines","unknown chart styles are ignored");
dashboard.setChartStyle("stacked");
styled=dashboard.insightsHTML();
assert.match(styled,/data-dashboard-chart-style="stacked" aria-pressed="true"/);
assert.doesNotMatch(styled,/class="dash-chart is-lines"/);
values.delete("branchglanceChartStyle");
dashboard.state.insights=payload([soloProvider]);

// (7) Review round: "30d / plan" header, bars that follow the metric, provider chips, collapsed Highest usage.
styled=dashboard.insightsHTML();
assert.match(styled,/<i class="dash-model-bar[^"]*" aria-hidden="true"><em class="[a-z]+" style="width:100\.0%"/,"the largest row fills its bar; others follow a linear scale");
for(const p of dashboard.state.insights.providers)p.limitPeaks={fiveHour:100,weekly:100};
styled=dashboard.insightsHTML();
assert.match(styled,/class="dash-limits dash-quota"[\s\S]*>Quota used<\/span>[\s\S]*>No live reading<\/small>/,"O401 keeps current quota rows explicit when only historical peaks exist");
assert.match(styled,/dash-quota-peak[\s\S]*Peak in this range[\s\S]*dash-peaks-table/,"O401 labels historical peaks as secondary range data");
assert.doesNotMatch(styled,/dash-limits is-collapsed/);

// (8) Bars scale linearly: half the largest row's tokens draws half the width.
dashboard.state.insights=payload([{name:"Claude",totals:{tokens:3e8,apiEquivalentCost:null,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null},previous:null,series:series(3e8),models:[{name:"claude-opus-5.5",tokens:2e8,apiEquivalentCost:null},{name:"claude-haiku-5.5",tokens:1e8,apiEquivalentCost:null}],limitPeaks:{fiveHour:null,weekly:null}}]);
dashboard.state.metric="tokens";
assert.match(dashboard.insightsHTML(),/<em class="claude" style="width:50\.0%"/,"bars are linear: half the largest row is half the width");

// (9) Round 3: quota follows the provider chips and shows the weekly column; stamped hours follow the clock; work figures; partial pricing; one point in the tab order.
const provider=(name,extra={})=>({name,totals:{tokens:1e6,apiEquivalentCost:10,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null},previous:null,series:series(1e6),models:[],limitPeaks:{fiveHour:null,weekly:null},...extra});
dashboard.state.range="30d";dashboard.state.metric="tokens";dashboard.state.resultValue=null;
dashboard.state.insights={range:"30d",providers:[provider("Claude",{totals:{tokens:35e6,apiEquivalentCost:null,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null},series:Array.from({length:30},(_,index)=>({date:dateOffset(29-index),tokens:36e6,apiEquivalentCost:null}))})]};
const tickScale=dashboard.insightsHTML();
assert.match(tickScale,/class="dash-axis-value"[^>]*>40M<\/span>/,"O403 axis ticks trim compact-format padding zeroes");
assert.match(tickScale,/class="dash-axis-value"[^>]*>20M<\/span>/,"O403 midpoint tick stays compact without padding zeroes");
assert.match(tickScale,/class="dash-hero-number">35\.0<span class="dash-hero-mag">M<\/span>/,"O403 leaves three-significant-digit values unchanged outside the axis");
assert.doesNotMatch(tickScale,/dash-axis-value[^>]*>40\.0M|dash-axis-value[^>]*>20\.0M/);

// Legend headers, provider cells and inline grid tracks come from one column set across all content states.
function assertLegendColumns(html,expectedRows,label){
  const legend=html.match(/<div class="dash-split-legend(?: no-share)?" style="grid-template-columns:([^"]+)">([\s\S]*?)<\/div>/);
  assert.ok(legend,`${label}: split legend exists`);
  const tracks=legend[1].trim().split(/\s+/).length,markup=legend[2];
  const heads=(markup.match(/class="dash-legend-head(?: [^"]*)?"/g)||[]).length;
  const rows=(markup.match(/<span class="dash-legend-item [^"]+">/g)||[]).length;
  const cells=(markup.match(/<span class="dash-legend-cell /g)||[]).length;
  assert.equal(rows,expectedRows,`${label}: provider row count`);
  assert.equal(heads,tracks,`${label}: header cells equal grid tracks`);
  assert.equal(cells,rows*tracks,`${label}: provider cells equal row count times grid tracks`);
  assert.match(legend[1],/^minmax\(0,max-content\)/,`${label}: provider name has its own leading track`);
}
const legendProviders=[provider("Claude"),provider("Codex")],legendMonth={providers:[
  {name:"Claude",series:[{tokens:1e6,apiEquivalentCost:10}]},
  {name:"Codex",series:[{tokens:1e6,apiEquivalentCost:10}]}
]};
dashboard.state.range="30d";dashboard.state.resultValue=null;dashboard.state.month=legendMonth;dashboard.state.monthError=false;
for(const layoutSize of ["default","compact"]){
  for(const range of ["24h","7d","30d"]){
    dashboard.state.range=range;
    for(const metric of ["tokens","cost"]){
      dashboard.state.metric=metric;
      for(const hasPlanPrice of [false,true]){
        for(const name of ["Claude","Codex"])values[hasPlanPrice?"set":"delete"](`branchglancePlanPrice.${name}`,"20");
        dashboard.state.insights=payload(legendProviders);
        const legendHTML=dashboard.insightsHTML();
        assertLegendColumns(legendHTML,2,`${layoutSize} ${range} ${metric} ${hasPlanPrice?"priced":"unpriced plan"}`);
        if(hasPlanPrice)assert.match(legendHTML,/class="dash-plan-cell"><span title="[^"]+">0\.5/);
        else assert.match(legendHTML,/data-dashboard-action="set-plan"/);

        dashboard.toggleProvider("Codex");
        assert.doesNotMatch(dashboard.insightsHTML(),/class="dash-split-legend/,`${layoutSize} ${range} ${metric}: one visible provider uses the single-provider plan layout`);
        dashboard.toggleProvider("Codex");
        dashboard.toggleProvider("Claude");
        assert.doesNotMatch(dashboard.insightsHTML(),/class="dash-split-legend/,`${layoutSize} ${range} ${metric}: the other provider filter also uses the single-provider layout`);
        dashboard.toggleProvider("Claude");
      }
    }
    dashboard.state.metric="cost";
    dashboard.state.insights=payload([legendProviders[0],provider("Codex",{totals:{tokens:1e6,apiEquivalentCost:null,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null}})]);
    assertLegendColumns(dashboard.insightsHTML(),2,`${layoutSize} ${range} Cost with one unpriced provider`);
  }
}
values.delete("branchglancePlanPrice.Claude");values.delete("branchglancePlanPrice.Codex");
dashboard.state.month=null;dashboard.state.range="30d";dashboard.state.metric="tokens";

// Compact token precision is shared by the hero, summary, chart tooltip and model table.
dashboard.state.range="30d";dashboard.state.metric="tokens";
dashboard.state.insights=payload([provider("Claude",{totals:{tokens:3.89e9,apiEquivalentCost:10,sessions:571e6,subagentsActive:0,activeHours:1.1e9,cacheHitRate:null},series:[{date:today.toISOString().slice(0,10),tokens:6.95e6,apiEquivalentCost:null}],models:[{name:"claude-opus-5.5",tokens:1.1e9,apiEquivalentCost:1}]})]);
const precisionHTML=dashboard.insightsHTML();
assert.match(precisionHTML,/>3\.89<span class="dash-hero-mag">B<\/span>/,"hero uses three significant digits");
assert.match(precisionHTML,/<dd>571M<\/dd>/,"summary numbers use three significant digits");
assert.match(precisionHTML,/6\.95M/,"chart tooltip uses the same three-digit format as its series");
assert.match(precisionHTML,/>1\.10B</,"model table retains trailing zeroes at three significant digits");
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

// Work table: three significant digits above 100, no trailing decimal, and exact values stay accessible.
dashboard.state.range="30d";dashboard.state.metric="tokens";
dashboard.state.ledger={rows:[{provider:"Claude",modelId:"claude-opus-5.5",displayName:"Opus 5.5",kind:"build",totalTokens:2e6,knownCostUsd:null,costComplete:false,unpricedTokens:2e6},{provider:"Claude",modelId:"claude-opus-5.5",displayName:"Opus 5.5",kind:"review",totalTokens:8e5,knownCostUsd:null,costComplete:false,unpricedTokens:8e5}],coverage:{},service:{}};
dashboard.state.ledgerRange="30d";
const workTable=dashboard.workHTML(dashboard.workData());
assert.match(workTable,/>2\.00M</ ,"work table uses three significant digits, including trailing zeroes");
assert.match(workTable,/>800K</,"work table: thousands have no decimal");
assert.match(workTable,/title="2,000,000 tokens"/ ,"work cells retain their exact token value in the title");
assert.match(workTable,/<span class="sr-only">2,000,000 tokens<\/span>/ ,"work cells retain their exact token value for screen readers");
dashboard.state.ledger=null;

dashboard.state.ledger={rows:[
  {provider:"Claude",modelId:"An unusually long model name that needs truncation",kind:"build",totalTokens:10,knownCostUsd:1,costComplete:true,unpricedTokens:0},
  {provider:"Claude",modelId:"Another long model name that needs truncation",kind:"review",totalTokens:20,knownCostUsd:1,costComplete:true,unpricedTokens:0},
  {provider:"Claude",modelId:"Third long model name that needs truncation",kind:"check",totalTokens:30,knownCostUsd:1,costComplete:true,unpricedTokens:0},
  {provider:"Claude",modelId:"Fourth long model name that needs truncation",kind:"research",totalTokens:40,knownCostUsd:1,costComplete:true,unpricedTokens:0},
]};dashboard.state.ledgerRange="30d";dashboard.state.range="30d";dashboard.state.metric="tokens";
const fourKinds=dashboard.workHTML(dashboard.workData());
assert.match(fourKinds,/An unusually long model name that needs truncation/,"By work preserves a long model name for its title and accessible label");
assert.match(fourKinds,/class="dash-work-row is-head"[\s\S]*>Review<\/span>[\s\S]*>Check<\/span>[\s\S]*>Other<\/span>/,"By work names the folded kind column without a model count");
dashboard.state.ledger=null;

const overflowRows=[
  ["claude-custom-long-model-name-for-overflow-fixture", "build", 603400000],
  ["claude-custom-long-model-name-for-overflow-fixture", "review", 87100000],
  ["claude-custom-long-model-name-for-overflow-fixture", "check", 609500000],
  ["claude-research-model", "research", 455500000],
  ["claude-research-model", "design", 317300000],
  ["claude-research-model", "chore", 179500000],
  ["claude-research-model", "build", 15600000],
  ["claude-research-model", "lead", 100000],
  ["claude-check-model", "build", 680500000],
  ...Array.from({length:7},(_,i)=>[`claude-overflow-other-${i+1}`,["explore","lead","design","chore","research","check","build"][i],30000000+(i+1)*1000000]),
];
dashboard.state.ledger={coverage:{status:"partial",gapCount:3},rows:overflowRows.map(([modelId,kind,totalTokens])=>({provider:"Claude",modelId,kind,totalTokens,knownCostUsd:null,costComplete:true,unpricedTokens:0}))};
dashboard.state.ledgerRange="30d";dashboard.state.range="30d";dashboard.state.metric="tokens";
const realScale=dashboard.workHTML(dashboard.workData());
assert.match(realScale,/style="--work-cols:4"/ ,"real-scale fixture retains four visible kind columns");
assert.match(realScale,/columnheader" title="[^"]+">Other<\/span>/,"the Other kind column has no model-count label and names folded kinds in its title");
assert.match(realScale,/<span role="rowheader" class="dash-model-name other" title="6 models folded"><i><\/i>Other \(6\)<\/span>/,"O402 keeps the folded By work label short and exposes the folded count in its title");
assert.match(realScale,/title="603,400,000 tokens"[\s\S]*603M/ ,"real-scale values use three significant digits while exact tokens stay in each title");
for(const formatted of ["603M","87.1M","1.30B","968M","15.6M","456M","681M"]){assert.match(realScale,new RegExp(`>${formatted}<`),`real-scale value compacts to ${formatted}`);}
const visibleWorkCells=[...realScale.matchAll(/<span role="cell"[^>]*>([\s\S]*?)<\/span>/g)].map(([,html])=>html.replace(/<span class="sr-only">[\s\S]*/,"").replace(/<[^>]+>/g,"").trim());
assert.ok(visibleWorkCells.length>0,"real-scale fixture renders work cells");
assert.ok(visibleWorkCells.every(value=>value.length<=5),`every visible real-scale work-cell figure fits in five characters: ${visibleWorkCells.filter(value=>value.length>5).join(", ")}`);
assert.match(realScale,/<div class="dash-work-note is-qualified"[^>]*><strong>Partial<\/strong> · 3 gaps<\/div>/,"partial coverage copy stays intact with only Partial bold");
dashboard.state.ledger=null;

dashboard.state.metric="cost";dashboard.state.ledgerRange="30d";dashboard.state.ledger={rows:[
  {provider:"Claude",modelId:"cost-1240",kind:"build",totalTokens:100,knownCostUsd:1240,costComplete:true,unpricedTokens:0},
  {provider:"Claude",modelId:"cost-15000",kind:"review",totalTokens:100,knownCostUsd:15000,costComplete:true,unpricedTokens:0},
  {provider:"Claude",modelId:"cost-14",kind:"check",totalTokens:100,knownCostUsd:14.2,costComplete:true,unpricedTokens:0},
]};
const costFormats=dashboard.workHTML(dashboard.workData());
assert.match(costFormats,/>\$1,240</,"costs below $10,000 keep whole-dollar comma formatting");
assert.match(costFormats,/>\$15k</,"costs at $10,000 and above use compact thousands");
assert.match(costFormats,/>\$14\.20</,"costs below $100 retain cents");
assert.match(costFormats,/title="\$1,240\.00 API-equivalent cost"/ ,"cost cells retain the exact amount in their title");
assert.match(costFormats,/<span class="sr-only">\$1,240\.00 API-equivalent cost<\/span>/ ,"cost cells retain the exact amount for screen readers");
dashboard.state.ledger={rows:[
  {provider:"Claude",modelId:"cost-55",kind:"build",totalTokens:100,knownCostUsd:55.54,costComplete:true,unpricedTokens:0},
  {provider:"Claude",modelId:"cost-4",kind:"review",totalTokens:100,knownCostUsd:4.12,costComplete:false,unpricedTokens:10},
  {provider:"Claude",modelId:"cost-023",kind:"check",totalTokens:100,knownCostUsd:0.23,costComplete:false,unpricedTokens:10},
]};
const lowerBoundFormats=dashboard.workHTML(dashboard.workData());
assert.match(lowerBoundFormats,/>\$55\.54</,"exact costs below $100 keep cents");
assert.match(lowerBoundFormats,/\u2265\$4\.12/,"a lower bound from four dollars keeps cents");
assert.match(lowerBoundFormats,/\u2265\$0\.23/,"a lower bound below one dollar uses dollars and cents");
assert.doesNotMatch(lowerBoundFormats,/\u00a2/,"cost columns never switch to cents notation");
dashboard.state.ledger=null;
dashboard.state.ledger=null;dashboard.state.metric="tokens";

// Partial pricing: the unit line names an excluded provider and partial models; partial model amounts carry a ≥ prefix.
dashboard.state.metric="cost";
dashboard.state.insights=payload([provider("Claude",{totals:{tokens:1e6,apiEquivalentCost:10,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null,costPartial:true,unpricedModels:["claude-haiku-5-5"]},models:[{name:"claude-opus-5.5",tokens:8e5,apiEquivalentCost:9,unpriced:true},{name:"claude-haiku-5.5",tokens:2e5,apiEquivalentCost:null}]}),provider("Codex",{totals:{tokens:1e6,apiEquivalentCost:null,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null}})]);
const partialHTML=dashboard.insightsHTML();
assert.match(partialHTML,/estimated · excl\. Codex · partly unpriced/,"the single-line unit note names the excluded provider and partially priced models");
assert.match(partialHTML,/\u2265\$9\.00/,"a partially priced model row shows a compact lower-bound amount");
assert.match(partialHTML,/title="\u2265\$9\.00; some usage is unpriced"/,"the partial row title starts with the compact lower-bound amount");
assert.doesNotMatch(partialHTML,/title="\$[\d.]+ per 1M tokens"|<span class="sr-only">\$[\d.]+ per 1M tokens/,"partial pricing does not show an unqualified per-token rate");
dashboard.state.insights=payload([provider("Claude",{totals:{tokens:1e6,apiEquivalentCost:10,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null,costPartial:true,unpricedModels:["claude-haiku-5-5"]}})]);
assert.match(dashboard.insightsHTML(),/<span class="dash-hero-unit">estimated · partly unpriced<\/span>/,"partial models alone keep the same meaning in a shorter note");
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
assert.match(dashboard.workHTML(dashboard.workData()),/>5\.35M</,"5,350,000 uses three significant digits");
dashboard.state.ledger=null;

const manyModels=[1,2,3,4,5,6].map(i=>({name:`claude-zeta-${i}`,tokens:(10-i)*1e5,apiEquivalentCost:2}));
manyModels.push({name:"claude-zeta-7",tokens:3e4,apiEquivalentCost:1},{name:"claude-zeta-8",tokens:1e4,apiEquivalentCost:null});
dashboard.state.insights=payload([provider("Claude",{models:manyModels})]);dashboard.state.metric="tokens";
const otherRow=dashboard.insightsHTML();
assert.match(otherRow,/Other \(4 models\)/,"the four folded model rows are counted as models");
assert.match(otherRow,/class="dash-model-name other"[^>]*tabindex="0" data-tip-html=/,"Other exposes its details to keyboard focus");
assert.match(otherRow,/data-tip-html="[^"]*tip-row/,"Other tooltip uses structured chart-style rows");
const foldedTip=otherRow.match(/data-tip-html="([^"]*tip-total[^"]*)"/)?.[1]||"";assert.equal((foldedTip.match(/&lt;span class=&quot;tip-row&quot;&gt;/g)||[]).length,4,"each folded model has its own name and value row in the tooltip");
assert.match(otherRow,/\u2265\$5\.00/,"an Other row with an unpriced constituent shows a compact lower bound");
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
dashboard.state.insights={range:"7d",generatedAt:new Date(Date.now()-61000).toISOString(),providers:[provider("Claude")]};dashboard.state.range="7d";dashboard.state.metric="tokens";
const freshness=dashboard.insightsHTML();
assert.match(freshness,/<p class="dash-status-line is-freshness" role="status">Updated 1m ago<\/p>/,"freshness is rendered on its own reserved line below the chart");
assert.ok(freshness.indexOf("dash-chart-section")<freshness.indexOf("dash-status-line is-freshness"),"freshness follows the complete chart section in document flow");

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
assert.match(costGrid,/>\$14\.20<\/span>/,"costs below $100 retain cents");
dashboard.state.ledger=null;
