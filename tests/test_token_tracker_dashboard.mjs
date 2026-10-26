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
assert.match(insights,/Model breakdown/);assert.match(insights,/Limit peaks/);
assert.match(insights,/Subagents active/);
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
const split=single.slice(single.indexOf('class="dash-split"'),single.indexOf("</section>",single.indexOf('class="dash-split"')));
assert.match(split,/claude-fill/);assert.doesNotMatch(split,/codex-fill|dash-dot codex/,"single-provider split has no empty provider legend entry");
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
assert.doesNotMatch(fallbackHtml,/Sessions|Subagents|Active hours/);
assert.doesNotMatch(fallbackHtml,/>\?|—/);
assert.match(fallbackHtml,/Haiku 5\.5/);
dashboard.state.range="24h";
const fallback24=dashboard.insightsHTML();
assert.doesNotMatch(fallback24,/Haiku 5\.5/,"stats-only fallback hides model totals outside the 31-day window");
dashboard.state.range="30d";

responseQueue=[{ok:false,status:404,json:async()=>({})},{ok:true,json:async()=>({providers:[{name:"Claude",tracked31Days:1200,cache:{apiEquivalentCost:1.2},activity:[]}]})}];
requests=[];await dashboard.loadInsights();
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
const settings=dashboard.settingsHTML();
assert.match(settings,/Lock widget position/);assert.match(settings,/Keep widget open/);
assert.match(settings,/Current provider policy/);assert.match(settings,/Monitor/);assert.match(settings,/Recenter/);
assert.doesNotMatch(settings,/Launch at startup|Always on top|Quiet hours|Check now|Open logs/,"controls without a backend stay out of the UI");
assert.match(settings,/reset_actions\.html\?embedded=1/);assert.match(settings,/Change Claude tracking/);
assert.match(source,/open_reset_actions/);
assert.doesNotMatch(source,/X-Branchglance-Settings-Token|settingsToken/,"dashboard never reads or stores the Claude settings token");

console.log("Insights and Settings dashboard contracts passed");
