import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const root=new URL("../token_tracker/static/",import.meta.url);
const source=fs.readFileSync(new URL("dashboard.js",root),"utf8");
const css=fs.readFileSync(new URL("dashboard.css",root),"utf8");
const values=new Map();
let responseQueue=[];
const localStorage={getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,String(value))};
const document={querySelector(){return null;},querySelectorAll(){return [];},addEventListener(){}};
const window={__TOKEN_TRACKER_DASHBOARD_TEST__:true,addEventListener(){},TokenTrackerFormatModel:({model})=>String(model).replace(/^claude-(opus|sonnet|haiku)-([0-9.]+).*$/i,(_,family,version)=>family[0].toUpperCase()+family.slice(1)+" "+version).replace(/^gpt-([0-9.]+)-([a-z]+).*$/i,(_,version,family)=>"GPT-"+version+" "+family[0].toUpperCase()+family.slice(1))};
const context={window,document,Intl,Date,Math,Number,JSON,Map,Set,Array,Promise,localStorage,location:{origin:"http://127.0.0.1:8765"},requestAnimationFrame:f=>f(),setTimeout:f=>f(),fetch:async()=>responseQueue.shift()||{ok:false,status:404,json:async()=>({})}};
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
assert.match(dashboard.insightsHTML(),/dash-hero-unit">B tokens/);
assert.doesNotMatch(dashboard.insightsHTML(),/dash-cost-note/);

dashboard.setMetric("cost");
assert.equal(dashboard.state.metric,"cost","the user can still choose Cost");
responseQueue=[{ok:true,json:async()=>payload([codexUnpriced()])}];
await dashboard.loadInsights();
assert.equal(dashboard.state.metric,"cost","an explicit Cost choice survives later loads");
const partial=dashboard.insightsHTML();
assert.match(partial,/dash-hero-currency/);
assert.match(partial,/<p class="dash-cost-note">Excludes GPT-6 Luna and GPT-6\.1 Sol \(no published price\)<\/p>/);

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

// (3) Model table shows the top five with a "+N more" line; fewer than six rows show no line.
const many=Array.from({length:7},(_,index)=>({name:`claude-opus-${index}.5`,tokens:(7-index)*1e8,apiEquivalentCost:(7-index)}));
dashboard.state.insights=payload([{name:"Claude",totals:{tokens:2.8e9,apiEquivalentCost:28,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null},previous:null,series:series(2.8e9),models:many,limitPeaks:{fiveHour:null,weekly:null}}]);
dashboard.state.metric="cost";
let html=dashboard.insightsHTML();
assert.equal((html.match(/class="dash-model-row"/g)||[]).length,5);
assert.match(html,/<div class="dash-model-more">\+2 more<\/div>/);
const few=many.slice(0,5);
dashboard.state.insights=payload([{name:"Claude",totals:{tokens:2.8e9,apiEquivalentCost:28,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null},previous:null,series:series(2.8e9),models:few,limitPeaks:{fiveHour:null,weekly:null}}]);
html=dashboard.insightsHTML();
assert.equal((html.match(/class="dash-model-row"/g)||[]).length,5);
assert.doesNotMatch(html,/dash-model-more/);

// (4) Insights body scrolls inside the window with a thin, subtle scrollbar; the old hidden-scrollbar rules no longer apply to it.
assert.match(css,/\.dashboard-content\{[^}]*overflow:auto[^}]*scrollbar-width:thin/);
assert.match(css,/\.dashboard-content::-webkit-scrollbar-thumb\{background:#2b2f38/);
assert.doesNotMatch(css,/\.dashboard-content::-webkit-scrollbar\{display:none/);
assert.match(css,/\.dashboard-view :not\(\.dashboard-content\)\{scrollbar-width:none;\}/);
assert.match(css,/\.dash-model-more\{/);

console.log("Insights polish contracts passed");
