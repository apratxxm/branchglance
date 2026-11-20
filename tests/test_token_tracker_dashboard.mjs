import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source=fs.readFileSync(new URL("../token_tracker/static/dashboard.js",import.meta.url),"utf8");
const settingsCss=fs.readFileSync(new URL("../token_tracker/static/dashboard.css",import.meta.url),"utf8");
const fixedNow=new Date(2026,10,14,12,0,0,0).getTime();
class TestDate extends Date {constructor(...args){super(...(args.length?args:[fixedNow]));}static now(){return fixedNow;}}
const values=new Map();
let requests=[];
let responseQueue=[];
const localStorage={getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,String(value)),removeItem:key=>values.delete(key)};
const events={},animationFrames=[],flushAnimationFrames=()=>animationFrames.splice(0).forEach(fn=>fn()),tabNodes={"#dashboard-tab-insights":{tabIndex:0,focus(){this.focused=true;document.activeElement=this;},setAttribute(k,v){this[k]=v;},classList:{toggle(){}}},"#dashboard-tab-settings":{tabIndex:-1,focus(){this.focused=true;document.activeElement=this;},setAttribute(k,v){this[k]=v;},classList:{toggle(){}}},"#dashboard-view":{dataset:{},setAttribute(){}}};
const document={activeElement:null,querySelector(selector){return tabNodes[selector]||null;},querySelectorAll(){return [];},addEventListener(name,fn){events[name]=fn;}};
const window={__TOKEN_TRACKER_DASHBOARD_TEST__:true,addEventListener(){},dispatchEvent(){},TokenTrackerFormatModel:({model})=>String(model).replace(/^claude-(opus|sonnet|haiku)-([0-9.]+).*$/i,(_,family,version)=>family[0].toUpperCase()+family.slice(1)+" "+version).replace(/^gpt-([0-9.]+)-([a-z]+).*$/i,(_,version,family)=>"GPT-"+version+" "+family[0].toUpperCase()+family.slice(1))};
const context={window,document,Intl,Date:TestDate,Math,Number,JSON,Map,Set,Array,Promise,localStorage,AbortSignal:{timeout:ms=>({timeout:ms})},requestAnimationFrame:fn=>{animationFrames.push(fn);},setInterval:()=>1,clearInterval(){},setTimeout:()=>1,clearTimeout(){},location:{origin:"http://127.0.0.1:8765"},fetch:async url=>{if(String(url).startsWith("/api/ledger"))return {ok:false,status:503,json:async()=>({})};requests.push(String(url));return responseQueue.shift()||{ok:false,status:404,json:async()=>({})};}};
vm.createContext(context);vm.runInContext(source,context);
const dashboard=window.__tokenTrackerDashboardTest;
const today=new Date(fixedNow);
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
assert.match(insights,/<button type="button" data-dashboard-models="models"[^>]*>Models<\/button>/,"Models stays available as the first segment of the breakdown toggle");assert.match(insights,/Peak in this range/);
assert.doesNotMatch(insights,/local time/,"range lines no longer carry a local-time suffix");assert.match(insights,/Nov 12 – 14/,"same-month daily range reads Nov 12 – 14");
assert.match(insights,/<dt>Subagents<\/dt><dd>6<\/dd>/);
assert.doesNotMatch(insights,/vs previous/,"no period delta at 30 days");
assert.match(insights,/area-fill/);assert.match(insights,/area-edge/);
assert.match(insights,/data-tip-html=/);
assert.match(insights,/Opus 5\.5/);assert.match(insights,/GPT-6 Luna/);
assert.match(insights,/class="dash-model-name codex"/);assert.match(insights,/class="dash-model-name claude"/);

dashboard.state.range="7d";
assert.match(dashboard.insightsHTML(),/vs previous week/,"delta shown when the API supplies previous totals");
dashboard.state.range="30d";

dashboard.state.metric="tokens";
const tokens=dashboard.insightsHTML();
assert.match(tokens,/<span class="dash-hero-mag">M<\/span><\/strong><span class="sr-only"> tokens<\/span>/);
assert.match(tokens,/<span class="dash-section-title">Daily tokens<\/span>/);

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
assert.match(fallbackHtml,/<dt>Sessions<\/dt><dd><span aria-label="Not available">\u2014<\/span><\/dd>/);assert.match(fallbackHtml,/<dt>Subagents<\/dt><dd><span aria-label="Not available">\u2014<\/span><\/dd>/);assert.match(fallbackHtml,/<dt>Active hours<\/dt><dd><span aria-label="Not available">\u2014<\/span><\/dd>/);
assert.doesNotMatch(fallbackHtml,/>\?/);
assert.match(fallbackHtml,/<span class="dash-none" role="img" aria-label="No published price">&mdash;<\/span>/,"unpriced model costs are explicitly marked");
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
assert.match(settings,/<span class="dash-h2-note">v1\.0\.0\+abc123<\/span>/);
dashboard.state.identity={version:"<script>",build:"&"};
assert.match(dashboard.settingsHTML(),/v&lt;script&gt;\+&amp;/,"identity values are escaped");
dashboard.state.identity=null;
assert.match(dashboard.settingsHTML(),/Version unavailable/,"missing identity is described without fabricating a version");
assert.match(settings,/Lock widget position/);assert.match(settings,/Keep widget open/);
assert.match(settings,/<span class="dash-setting-label">Codex<\/span>/);assert.match(settings,/Shown \u00b7 collecting \u00b7 does not send automatically/,"provider rows state their flags in one plain-text line");assert.match(settings,/Shown \u00b7 not collecting \u00b7 does not send automatically/);assert.match(settings,/<span class="dash-setting-label">Widget position<\/span>/,"one monitor is labelled Widget position");assert.match(settings,/Recenter/);assert.match(settings,/Move the widget to the center of this screen/,"the position row describes the recenter action");assert.doesNotMatch(settings,/Move the widget back to the center/);
assert.doesNotMatch(settings,/Launch at startup|Always on top|Quiet hours|Check now|Open logs/,"controls without a backend stay out of the UI");
assert.match(settings,/<span class="dash-setting-label">Codex<\/span>[\s\S]*?<input id="dash-plan-price"[^>]*data-dashboard-plan="Codex"[\s\S]*?<span class="dash-setting-label">Claude<\/span>[\s\S]*?<input(?! id="dash-plan-price")[^>]*data-dashboard-plan="Claude"/,"Codex is first and receives the shared plan input id by provider name");
assert.match(settings,/reset_actions\.html\?embedded=1/);assert.match(settings,/Manage providers, automation and history/);assert.match(settings,/<h2>Plan price<\/h2>/);assert.match(settings,/Compares the last 30 days of estimated API cost with your monthly plan price/);assert.doesNotMatch(settings,/dash-setting-description">Monthly/,"rows are label-only");assert.doesNotMatch(settings,/placeholder=/,"an unset plan price is shown by an empty field, not a placeholder");assert.match(settings,/aria-label="Codex plan price per month"/);assert.match(settings,/Set by policy file/);assert.match(settings,/Create report/,"the bug report row replaces Preview");assert.match(settings,/No chat text, file paths, account details or tokens\./);assert.doesNotMatch(settings,/Review it before sharing/);assert.match(settings,/After a Codex quota reset/);
assert.match(settingsCss,/\.dash-setting-row\+\.dash-setting-row\{border-top:1px solid var\(--d-edge\);\}/,"Settings rows use the hairline edge token");assert.match(settingsCss,/\.dash-toggle\.is-on::after\{[^}]*background:var\(--d-codex\);/,"the on switch uses Codex indigo");assert.doesNotMatch(settingsCss,/\.dash-toggle\.is-on::after\{[^}]*#425df6/i,"the on switch has no hard-coded blue");
assert.match(source,/open_reset_actions/);
assert.doesNotMatch(source,/X-Branchglance-Settings-Token|settingsToken/,"dashboard never reads or stores the Claude settings token");

dashboard.state.monitors=[{id:"display-1",label:"Laptop",selected:true},{id:"display-2",label:"Desktop",selected:false}];
assert.match(dashboard.settingsHTML(),/<span class="dash-setting-label">Monitor<\/span>/,"two monitors keep the Monitor label");
dashboard.state.monitors=[{id:"display-1",label:"Laptop",selected:true}];

// A refused switch keeps its checked state, disables only itself while saving, and explains the failure beside its own row.
{
  let rejectLock;
  window.pywebview={api:{set_widget_lock:()=>new Promise((_,reject)=>{rejectLock=reject;})}};
  dashboard.state.lock={locked:false,revision:1};dashboard.state.keepOpen={keepOpen:false};dashboard.state.settingsRenderKey="";
  events.click({target:{closest:selector=>selector==="[data-dashboard-action]"?{dataset:{dashboardAction:"lock"}}:null}});
  const saving=dashboard.settingsHTML();
  assert.match(saving,/aria-busy="true" disabled data-dashboard-action="lock"/,"the switch being saved is disabled");
  assert.equal((saving.match(/aria-busy="true"/g)||[]).length,1,"only the saving switch is busy");
  rejectLock(new Error("refused"));
  await new Promise(resolve=>globalThis.setTimeout(resolve,0));
  assert.equal(dashboard.state.lock.locked,false,"a refused change keeps the previous checked state");
  const failed=dashboard.settingsHTML();
  assert.equal((failed.match(/<p class="dash-setting-error" role="alert">Couldn't save\. Try again\.<\/p>/g)||[]).length,1,"the failure is explained once, beside its row");
  assert.doesNotMatch(failed,/aria-busy="true"/,"the switch is released once the request settles");
  delete window.pywebview;
}

// "Set plan" in Insights opens the Settings tab.
dashboard.state.tab="insights";
events.click({target:{closest:selector=>selector==="[data-dashboard-action]"?{dataset:{dashboardAction:"set-plan"}}:null}});
assert.equal(dashboard.state.tab,"settings","Set plan switches to the Settings tab");

// Set plan for Codex opens Settings, names Codex in its button and focuses the Codex input, not the first field.
dashboard.state.month={providers:[{name:"Codex",totals:{costPartial:false},models:[],series:[{date:dateOffset(1),tokens:5e5,apiEquivalentCost:10}]}]};
dashboard.state.monthError=false;values.delete("branchglancePlanPrice.Codex");
assert.match(dashboard.planCellsHTML("Codex"),/<button type="button" class="dash-plan-set" data-dashboard-action="set-plan" data-dashboard-plan-provider="Codex" aria-label="Set Codex monthly plan price">Set plan<\/button>/);
{
  const codexInput={focus(){this.focused=true;},scrollIntoView(){this.scrolled=true;}},claudeInput={focus(){this.focused=true;},scrollIntoView(){this.scrolled=true;}};
  tabNodes['#dashboard-settings [data-dashboard-plan="Codex"]']=codexInput;tabNodes['#dashboard-settings [data-dashboard-plan="Claude"]']=claudeInput;
  dashboard.state.open=true;dashboard.state.tab="insights";
  events.click({target:{closest:selector=>selector==="[data-dashboard-action]"?{dataset:{dashboardAction:"set-plan",dashboardPlanProvider:"Codex"}}:null}});
  assert.equal(dashboard.state.tab,"settings","Set plan for Codex switches to Settings");
  assert.equal(codexInput.focused,true,"Set plan for Codex focuses the Codex input");
  assert.equal(codexInput.scrolled,true,"the focused input is scrolled into view");
  assert.notEqual(claudeInput.focused,true,"the Claude input is not focused");
}

// A switch that settles after the user moved on does not take focus back; one that lost focus to the page gets it back.
{
  let resolveLock;
  window.pywebview={api:{set_widget_lock:()=>new Promise(resolve=>{resolveLock=resolve;})}};
  const lockSwitch={dataset:{dashboardAction:"lock"},focus(){this.focused=true;}},elsewhere={dataset:{dashboardAction:"recenter"},focus(){this.focused=true;}};
  tabNodes['#dashboard-settings [data-dashboard-action="lock"]']=lockSwitch;
  dashboard.state.open=true;dashboard.state.tab="settings";dashboard.state.lock={locked:false,revision:1};dashboard.state.keepOpen={keepOpen:false};
  document.activeElement=lockSwitch;
  events.click({target:{closest:selector=>selector==="[data-dashboard-action]"?{dataset:{dashboardAction:"lock"}}:null}});
  document.activeElement=elsewhere;
  resolveLock({locked:true,expanded:false,revision:2});
  await new Promise(resolve=>globalThis.setTimeout(resolve,0));
  assert.notEqual(lockSwitch.focused,true,"a switch that settles after focus moved on does not take focus");
  assert.notEqual(elsewhere.focused,true,"focus stays where the user put it");
  dashboard.state.lock={locked:true,revision:2};
  document.activeElement=lockSwitch;
  events.click({target:{closest:selector=>selector==="[data-dashboard-action]"?{dataset:{dashboardAction:"lock"}}:null}});
  document.activeElement=null;
  resolveLock({locked:false,expanded:false,revision:3});
  await new Promise(resolve=>globalThis.setTimeout(resolve,0));
  assert.equal(lockSwitch.focused,true,"focus lost to the page returns to the switch that settled");
  document.activeElement=null;delete window.pywebview;
}

// Persisted All migrates to the supported 30-day range; provider selection scopes each section.
values.set("branchglanceInsightsRange","all");
assert.equal(dashboard.storedRange(),"30d");assert.equal(values.get("branchglanceInsightsRange"),"30d");
dashboard.state.range="7d";dashboard.state.metric="tokens";
dashboard.state.insights={range:"7d",providers:[provider("Claude"),provider("Codex")]};
dashboard.toggleProvider("Codex");
assert.equal(dashboard.insightsData().totals.tokens,1000000);
assert.equal(dashboard.insightsData().models.length,1);
assert.match(dashboard.insightsHTML(),/data-dashboard-provider="Codex" aria-pressed="false"/);
dashboard.toggleProvider("Claude");assert.equal(dashboard.insightsData().totals.tokens,1000000,"at least one provider remains visible");
dashboard.toggleProvider("Codex");

// Lines mode places one marker at each visible provider value; its tooltip still contains the total.
dashboard.setChartStyle("lines");
assert.match(dashboard.insightsHTML(),/data-dashboard-chart-style="lines" aria-pressed="true"/,"multiple visible providers keep the Lines/Stacked control");
const lineChart=dashboard.seriesChart(dashboard.insightsData());
assert.equal((lineChart.match(/class="dash-marker /g)||[]).length,6);
assert.match(lineChart,/Total:/);
assert.equal(dashboard.planMultiple(1.29,true),"1.2");
assert.equal(dashboard.planMultiple(10.9,true),"10");
assert.doesNotMatch(dashboard.insightsHTML(),/excludes unpriced usage/);

// Lock state disables both placement controls and gives the required explanation.
dashboard.state.lock={locked:true};
const lockedSettings=dashboard.settingsHTML();
assert.match(lockedSettings,/Unlock the widget to move it/);
assert.match(lockedSettings,/data-dashboard-action="recenter" disabled/);
assert.doesNotMatch(lockedSettings,/data-dashboard-action="monitor"/,"one monitor renders no select");
dashboard.state.monitors=[{id:"display-1",label:"Laptop",selected:true},{id:"display-2",label:"Desktop",selected:false}];
assert.match(dashboard.settingsHTML(),/data-dashboard-action="monitor" disabled/,"a locked two-monitor layout disables the select");
dashboard.state.monitors=[{id:"display-1",label:"Laptop",selected:true}];
dashboard.state.lock=null;
dashboard.state.insights={generatedAt:new Date(fixedNow-120000).toISOString(),providers:[provider("Claude")]};
dashboard.state.loading=false;dashboard.state.loadError=false;
assert.match(dashboard.statusText(),/Updated 2m ago/);

// Tabs use roving focus, arrows/Home/End select the tab, and close restores the opener.
const tabEvent={key:"End",target:{closest:selector=>selector.includes("role=tab")?{dataset:{dashboardTab:"insights"}}:null},preventDefault(){this.prevented=true;}};
events.keydown(tabEvent);
assert.equal(dashboard.state.tab,"settings");assert.equal(tabNodes["#dashboard-tab-settings"].tabIndex,0);
tabEvent.key="Home";tabEvent.target={closest:selector=>selector.includes("role=tab")?{dataset:{dashboardTab:"settings"}}:null};events.keydown(tabEvent);
assert.equal(dashboard.state.tab,"insights");assert.equal(tabNodes["#dashboard-tab-insights"].tabIndex,0);
events.click({target:{closest:selector=>selector==="[data-dashboard-tab]"?{dataset:{dashboardTab:"settings"}}:null}});
assert.equal(dashboard.state.tab,"settings","native Enter/Space button activation reaches the tab click handler");
events.click({target:{closest:selector=>selector==="[data-dashboard-tab]"?{dataset:{dashboardTab:"insights"}}:null}});
const insightsOpener={id:"dashboard-open",focus(){this.restored=true;document.activeElement=this;}},settingsOpener={id:"dashboard-settings-open",focus(){this.restored=true;document.activeElement=this;}};document.activeElement=insightsOpener;
window.TokenTrackerDashboard.open({tab:"insights"});
flushAnimationFrames();
assert.equal(tabNodes["#dashboard-tab-insights"].focused,true);
window.TokenTrackerDashboard.close();assert.notEqual(document.activeElement,insightsOpener,"focus waits for the next animation frame after closing");flushAnimationFrames();assert.equal(document.activeElement,insightsOpener,"the dashboard close control restores the Insights opener");
document.activeElement=settingsOpener;window.TokenTrackerDashboard.open({tab:"settings"});flushAnimationFrames();window.TokenTrackerDashboard.close();assert.notEqual(document.activeElement,settingsOpener,"Escape focus restoration waits for the next animation frame");flushAnimationFrames();assert.equal(document.activeElement,settingsOpener,"closing with Escape restores the Settings opener");
{
  let selections=0;window.pywebview={api:{select_monitor:async id=>{selections++;assert.equal(id,"display-2");return [];}}};
  window.tokenTrackerSelectMonitor=id=>window.pywebview.api.select_monitor(id);
  const select={value:"display-2",dataset:{dashboardAction:"monitor"}};
  events.change({target:select});
  await new Promise(resolve=>globalThis.setImmediate(resolve));
  assert.equal(selections,1,"one Settings monitor change sends exactly one native selection");
  delete window.pywebview;delete window.tokenTrackerSelectMonitor;
}

console.log("Insights and Settings dashboard contracts passed");

// Every Insights attempt has a bounded signal; the header Refresh button retries a failed load.
let timedOptions=null,insightCalls=0;
context.fetch=async(url,options={})=>{if(String(url).startsWith("/api/ledger"))return {ok:false,status:503};if(String(url).startsWith("/api/insights")){timedOptions=options;insightCalls++;return {ok:false,status:503};}return {ok:false,status:503};};
dashboard.state.insights=null;dashboard.state.loadError=false;dashboard.state.range="30d";
await dashboard.loadInsights();
assert.equal(timedOptions.signal.timeout,12000,"Insights fetch uses AbortSignal.timeout");
assert.equal(dashboard.state.loadError,true,"failed initial load is reported");
const callsBeforeRetry=insightCalls;
events.click({target:{closest:selector=>selector==="#dashboard-refresh"?{}:null}});
await new Promise(resolve=>globalThis.setTimeout(resolve,0));
assert.ok(insightCalls>callsBeforeRetry,"Refresh starts a retry after failure");

// A Settings preference update keeps the mounted reset iframe in place (moving it would reload it) and restores focus to its initiating control.
{
  // A minimal DOM: nodes with ordered children, remove/before/after, and a move counter.
  const mk=(name,extra={})=>({name,parentNode:null,childNodes:[],moves:0,...extra,
    remove(){const p=this.parentNode;if(p){p.childNodes.splice(p.childNodes.indexOf(this),1);this.parentNode=null;}},
    insert(at,nodes){for(const n of nodes){n.remove?.();n.parentNode=this.parentNode;n.moves++;this.parentNode.childNodes.splice(at++,0,n);}},
    before(...nodes){this.insert(this.parentNode.childNodes.indexOf(this),nodes);},
    after(...nodes){this.insert(this.parentNode.childNodes.indexOf(this)+1,nodes);}});
  const frameNode=src=>({getAttribute:name=>name==="src"?src:null});
  const columns=src=>{const cols=mk("cols"),control={dataset:{dashboardAction:"lock"},focus(){this.focused=true;}};const nodes=[mk("widget",{control}),mk("group",{frame:frameNode(src),querySelector(sel){return sel==="iframe.reset-embed"?this.frame:null;}}),mk("about")];nodes.forEach(n=>{n.parentNode=cols;cols.childNodes.push(n);});return cols;};
  const find=(cols,name)=>cols?.childNodes.find(n=>n.name===name)||null;
  const root={dataset:{},addEventListener(){}};
  const insightPanel={_html:"",control:null,set innerHTML(value){this._html=value;this.control={dataset:{dashboardProvider:"Codex"},focus(){this.focused=true;}};},querySelectorAll(selector){return selector==="[data-dashboard-provider]"?[this.control]:[];},querySelector(){return null;}};
  let cols=null,replaced=0;
  const settings={querySelector(selector){if(selector===".dash-reset-group")return find(cols,"group");if(selector==="iframe.reset-embed")return find(cols,"group")?.frame||null;if(selector.includes("data-dashboard-action"))return find(cols,"widget")?.control||null;return null;},replaceChildren(fragment){cols=fragment.cols;replaced++;}};
  const docs={activeElement:{dataset:{dashboardProvider:"Codex"}},querySelector(selector){return selector==="#dashboard-settings"?settings:selector==="#dashboard-insights"?insightPanel:selector==="#dashboard-view"?root:null;},querySelectorAll:()=>[],addEventListener(){}};
  const win={__TOKEN_TRACKER_DASHBOARD_TEST__:true,addEventListener(){},dispatchEvent(){}};
  const ctx={window:win,document:docs,Intl,Date,Math,Number,JSON,Map,Set,Array,Promise,localStorage,AbortSignal:{timeout:ms=>({timeout:ms})},location:{origin:"http://localhost"},fetch:async()=>({ok:false}),setInterval:()=>1,clearInterval(){},requestAnimationFrame:fn=>fn(),Event};
  docs.createElement=tag=>{assert.equal(tag,"template");const template={content:null,set innerHTML(value){const c=columns((value.match(/src="([^"]+)"/)||[])[1]);template.content={cols:c,querySelector:sel=>sel===".dash-reset-group"?find(c,"group"):sel==="iframe.reset-embed"?find(c,"group")?.frame||null:null};}};return template;};
  vm.createContext(ctx);vm.runInContext(source,ctx);
  const local=win.__tokenTrackerDashboardTest;local.state.open=true;local.state.tab="insights";local.state.range="7d";local.state.insights={range:"7d",providers:[provider("Claude"),provider("Codex")]};
  win.TokenTrackerDashboard.render();
  assert.equal(insightPanel.control.focused,true,"Insights rerender restores focus to the provider chip that triggered it");
  local.state.tab="settings";local.state.settingsLoaded=true;docs.activeElement={dataset:{dashboardAction:"lock"}};local.state.renderKey="";
  win.TokenTrackerDashboard.render();
  const mountedGroup=find(cols,"group"),mountedFrame=mountedGroup.frame;
  assert.equal(replaced,1,"first Settings render mounts the whole panel");
  docs.activeElement={dataset:{dashboardAction:"lock"}};local.state.lock={locked:true};local.state.settingsRenderKey="";win.TokenTrackerDashboard.render();
  assert.equal(replaced,1,"a preference update does not replace the panel around the reset form");
  assert.equal(find(cols,"group"),mountedGroup,"the reset form group remains the same node");
  assert.equal(mountedGroup.moves,0,"the reset form group is never moved, so its iframe does not reload");
  assert.equal(settings.querySelector("iframe.reset-embed"),mountedFrame,"the reset form iframe remains the same mounted node");
  assert.deepEqual(cols.childNodes.map(n=>n.name),["widget","group","about"],"siblings are replaced in their original order");
  assert.equal(find(cols,"widget").control.focused,true,"settings rerender returns focus to the control that triggered it");
  local.state.source="Claude";local.state.settingsRenderKey="";win.TokenTrackerDashboard.render();
  assert.equal(replaced,2,"a different reset form source replaces the panel");
}

// Accepted data stays labeled with its captured range during a later request.
let resolvePending;
dashboard.state.range="7d";
dashboard.state.metric="tokens";
context.fetch=async url=>String(url).startsWith("/api/ledger")?{ok:false}:String(url).includes("range=7d")?{ok:true,json:async()=>({range:"7d",providers:[provider("Claude")]})}:{ok:false};
await dashboard.loadInsights();
context.fetch=url=>String(url).startsWith("/api/ledger")?Promise.resolve({ok:false}):new Promise(resolve=>{resolvePending=resolve;});
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
const zeroHTML=dashboard.insightsHTML();assert.match(zeroHTML,/No usage /,"observed zero says no usage");assert.match(zeroHTML,/Show 30 days/,"observed zero offers the 30-day range");assert.doesNotMatch(zeroHTML,/<svg/,"zero usage renders no chart svg");assert.doesNotMatch(zeroHTML,/dash-model-row/,"zero usage renders no model rows");

// A mismatched response cannot be accepted as the requested period.
dashboard.state.range="7d";dashboard.state.insights=null;
let replies=[{ok:true,json:async()=>({range:"30d",providers:[provider("Wrong")]})},{ok:true,json:async()=>({providers:[{name:"Fallback",tracked31Days:5,activity:[{date:dateOffset(0),value:5}]}]})}];
context.fetch=async url=>String(url).startsWith("/api/ledger")?{ok:false}:replies.shift();
await dashboard.loadInsights();
assert.equal(dashboard.state.insights.providers[0].name,"Fallback");
assert.match(dashboard.insightsHTML(),/Daily fallback/);

// Closing invalidates in-flight requests even if the network responds later.
let late;
context.fetch=url=>String(url).startsWith("/api/ledger")?Promise.resolve({ok:false}):new Promise(resolve=>{late=resolve;});
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
assert.match(scopedChart,/estimated · excl\. Codex/);
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
assert.match(dashboard.insightsHTML().replace(/<[^>]+>/g,""),/Update failed\. Use Refresh to retry\./,"full render carries the retry hint when no timestamp is available");assert.match(dashboard.insightsHTML(),/<span class="dash-fail">Update failed<\/span>/,"the failure name is marked for strong neutral error styling");
assert.match(dashboard.statusText(),/^Update failed\. Use Refresh to retry\./,"status-line path carries the same retry hint");
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
const fake={window:win,document:docs,Intl,Date,Math,Number,JSON,Map,Set,Array,Promise,localStorage,AbortSignal:{timeout:ms=>({timeout:ms})},location:{origin:"http://localhost"},fetch:async()=>({ok:false}),setTimeout:fn=>{const id=++nextTimer;scheduled.set(id,fn);return id;},clearTimeout:id=>scheduled.delete(id),setInterval:()=>1,clearInterval(){},requestAnimationFrame:fn=>frames.push(fn)};
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
context.fetch=async url=>String(url).startsWith("/api/ledger")?{ok:false}:fallbackResponses.shift();
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
assert.match(summary,/<th scope="row" class="codex-color">Codex<\/th><td><span aria-hidden="true">—<\/span><span class="sr-only">Not recorded<\/span><\/td><td>80%/);
assert.match(summary,/<th scope="row" class="claude-color">Claude<\/th><td>50%<\/td><td><span aria-hidden="true">—<\/span><span class="sr-only">Not recorded/);

// Insights refresh: an identical payload keeps the rendered panel; the status line is inserted or updated without a full render.
{
  const panelDom={_html:"",renders:0,statusEl:null,
    get innerHTML(){return this._html;},set innerHTML(value){this.renders++;this._html=String(value);},
    insertAdjacentHTML(){this.statusEl={textContent:"",classList:{toggle(){}},remove(){this.statusEl=null;}};},
    querySelector(selector){return selector===".dash-status-line"?this.statusEl:null;}};
  const docsDom={querySelector:sel=>sel==="#dashboard-insights"?panelDom:null,querySelectorAll:()=>[],addEventListener(){}};
  const responses=[];
  const ctx={window:{__TOKEN_TRACKER_DASHBOARD_TEST__:true,addEventListener(){},TokenTrackerFormatModel:({model})=>String(model)},
    document:docsDom,Intl,Date,Math,Number,JSON,Map,Set,Array,Promise,localStorage,AbortSignal:{timeout:ms=>({timeout:ms})},location:{origin:"http://localhost"},
    setTimeout:fn=>{fn();return 0;},clearTimeout(){},setInterval:()=>1,clearInterval(){},
    fetch:async url=>String(url).startsWith("/api/ledger")?{ok:false,status:503,json:async()=>({})}:responses.shift()||{ok:false,status:404,json:async()=>({})}};
  vm.createContext(ctx);vm.runInContext(source,ctx);
  const ui=ctx.window.__tokenTrackerDashboardTest;
  ui.state.open=true;ui.state.range="30d";ui.state.metric="tokens";ui.state.tab="insights";
  const payload=()=>({range:"30d",generatedAt:"2026-10-30T10:00:00.000Z",providers:[
    {name:"Claude",totals:{tokens:1000,apiEquivalentCost:null,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null},previous:null,series:[{date:"2026-10-29",tokens:400,apiEquivalentCost:null},{date:"2026-10-30",tokens:600,apiEquivalentCost:null}],models:[],limitPeaks:null},
    {name:"Codex",totals:{tokens:500,apiEquivalentCost:null,sessions:1,subagentsActive:0,activeHours:1,cacheHitRate:null},previous:null,series:[{date:"2026-10-30",tokens:500,apiEquivalentCost:null}],models:[],limitPeaks:null}]});
  responses.push({ok:true,json:async()=>payload()});
  await ui.loadInsights();
  assert.equal(panelDom.renders,2,"first load mounts the skeleton, then renders the data once");
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
  assert.match(dashboard.settingsHTML(),/dash-add-provider">Add Claude to track its agents too\./);assert.match(dashboard.settingsHTML(),/Hidden/);
  dashboard.state.policy={providers:[{name:"Codex",show:true,collect:true,send:false},{name:"Claude",show:true,collect:true,send:false}]};
  assert.doesNotMatch(dashboard.settingsHTML(),/dash-add-provider/);assert.match(dashboard.settingsHTML(),/Shown \u00b7 collecting \u00b7 does not send automatically/);
}

// Stage 2: lower bounds, empty cells, chart range, model folding and quota copy.
{
  dashboard.state.range="7d";dashboard.state.resultValue=null;dashboard.state.metric="cost";dashboard.state.ledgerRange="7d";
  const row=(modelId,kind,knownCostUsd,costComplete=true)=>({provider:"Claude",modelId,kind,totalTokens:100,knownCostUsd,costComplete});
  dashboard.state.ledger={rows:[row("long model name for truncation","build",1.6,false),row("long model name for truncation","review",0.49,false),row("long model name for truncation","research",0.009,false),row("another model","build",null,false)],coverage:{},service:{}};
  let html=dashboard.workHTML(dashboard.workData());
  for(const value of ["\u2265$1.60","\u2265$0.49","\u2265$0.00"])assert.ok(html.includes(value),value);
  assert.match(html,/\$0\.009 API-equivalent cost/);
  assert.doesNotMatch(html,/\u2265&lt;/);
  assert.match(html,/aria-label="No usage"><span aria-hidden="true">\u2014<\/span>/);
  assert.match(html,/aria-label="Unpriced"/);assert.match(html,/\u00b7 = no published price/);
  assert.match(html,/title="Claude \u00b7 long model name for truncation"/);
  dashboard.state.ledger={rows:[row("a","build",1),row("b","review",2)],coverage:{},service:{}};
  html=dashboard.workHTML(dashboard.workData());assert.doesNotMatch(html,/\u00b7 = no published price/);
  const many=Array.from({length:6},(_,i)=>({name:`model-${i}`,tokens:600-i*100,apiEquivalentCost:6-i}));
  const chartProvider=provider("Claude",{models:many});chartProvider.series=[{date:dateOffset(2),tokens:0,apiEquivalentCost:0},{date:dateOffset(1),tokens:null,apiEquivalentCost:null},{date:dateOffset(0),tokens:5,apiEquivalentCost:1}];
  dashboard.state.insights={range:"7d",providers:[chartProvider]};dashboard.state.metric="tokens";
  html=dashboard.insightsHTML();assert.equal((html.match(/class="dash-model-row"/g)||[]).length,5);
  assert.match(html,/aria-label="Other \(2 models\): model-4: 200 tokens; model-5: 100 tokens"/);
  assert.match(html,/role="group" aria-label="Daily tokens by provider"/);
  assert.match(html,new RegExp(`aria-label="${dateOffset(2)}\\. Total: 0`));
  assert.match(html,/Missing provider data/);
  dashboard.state.range="24h";dashboard.state.insights={range:"24h",providers:[{...chartProvider,series:[{date:"2026-11-14T12:00:00",tokens:1,apiEquivalentCost:1},{date:"2026-11-14T13:00:00",tokens:2,apiEquivalentCost:2}]}]};
  assert.match(dashboard.insightsHTML(),/aria-label="Hourly tokens by provider"/);dashboard.state.metric="cost";assert.match(dashboard.insightsHTML(),/aria-label="Hourly API-equivalent cost by provider"/);
  window.tokenTrackerQuotaNow=()=>[{name:"Claude",usedPercent:40,countdown:"2h 40m",weekly:{usedPercent:60,countdown:"3d 2h"}}];
  html=dashboard.insightsHTML();assert.match(html,/>Quota used<\/span><small>% used \u00b7 resets in<\/small>/);assert.match(html,/title="Peak 55% in this range"[^>]*>.*data-quota-part="five">2h 40m<\/small><span class="sr-only">Peak 55% in this range<\/span>/);assert.match(html,/data-quota-part="week">3d 2h/);
  delete window.tokenTrackerQuotaNow;dashboard.state.ledger=null;
}

// R8: quota, activity, breakdown segments, provider pricing and model rate.
{
  dashboard.state.resultValue=null;dashboard.state.range="7d";dashboard.state.metric="cost";
  const priced=provider("Claude",{cost:8.02,models:[{name:"rate-model",tokens:1000000,apiEquivalentCost:8.02}]});
  const unpriced=provider("Codex",{cost:null,models:[]});
  dashboard.state.insights={range:"7d",providers:[priced,unpriced]};
  if(/data-dashboard-provider="Codex" aria-pressed="false"/.test(dashboard.insightsHTML()))dashboard.toggleProvider("Codex");
  dashboard.state.ledgerRange="7d";dashboard.state.ledger={rows:[{provider:"Claude",modelId:"rate-model",kind:"build",totalTokens:100,knownCostUsd:1,costComplete:true}],coverage:{},service:{}};
  window.tokenTrackerQuotaNow=()=>[{name:"Claude",usedPercent:40,countdown:"2h 40m",weekly:{usedPercent:20,countdown:"3d 2h"}}];
  let html=dashboard.insightsHTML();
  assert.match(html,/<span>Quota used<\/span><small>% used \u00b7 resets in<\/small>/);
  assert.match(html,/<td title="Peak 55% in this range"><span class="dash-quota-main"[^>]*>40%<\/span><small[^>]*>2h 40m<\/small><span class="sr-only">Peak 55% in this range<\/span><\/td>/);
  assert.match(html,/class="codex-color">Codex/,"O401 keeps a row for the visible provider with no current reading");
  assert.match(html,/aria-label="No live reading"/,"O401 exposes the missing live reading to assistive technology");
  assert.match(html,/data-quota-key="Codex" data-quota-part="five">No live reading<\/small>/,"O401 shows the missing live reading under the dash");
  assert.match(html,/<tr class="dash-quota-row"><th scope="row" class="claude-color">Claude<\/th><td title="Peak 55% in this range">/,"O401 preserves live quota rows alongside missing providers");
  assert.doesNotMatch(html,/data-quota-part="peak"/);
  assert.match(html,/<dl class="dash-summary">/);assert.doesNotMatch(html,/class="dash-activity/);
  assert.ok(html.indexOf('dash-model-block')<html.indexOf('dash-summary')&&html.indexOf('dash-summary')<html.indexOf('dash-chart-section'));
  for(const label of ["Sessions","Subagents","Cache hit rate","Active hours"])assert.match(html,new RegExp(`<dt>${label}<\\/dt>`));
  assert.match(html,/<span class="dash-breakdown dash-segment" role="group" aria-label="Breakdown"><button type="button" data-dashboard-models="models"[^>]*>Models<\/button><button type="button" data-dashboard-models="work"[^>]*>By work<\/button><\/span>/,"both breakdown choices remain visible when work data is available");
  assert.match(html,/<div class="dash-split-legend no-share" style="grid-template-columns:minmax\(0,max-content\) max-content max-content">/);assert.doesNotMatch(html,/<span class="dash-legend-head">Share<\/span>/);
  assert.match(html,/class="dash-legend-cell dash-legend-value"><span class="dash-legend-unpriced">Unpriced<\/span><\/span><span class="dash-legend-cell dash-legend-plan"><span class="dash-plan-cell"><\/span><span class="dash-plan-note-cell"><\/span><\/span>/,"unpriced value and plan remain in separate legend columns");
  assert.match(html,/<td title="\$8\.02 per 1M tokens"><span class="sr-only">\$8\.02 per 1M tokens\. <\/span>\$8\.02<\/td>/);
  const partialModel=provider("Claude",{cost:1.6,models:[{name:"partial-model",tokens:1000000,apiEquivalentCost:1.6,unpriced:true}]});
  dashboard.state.insights={range:"7d",providers:[partialModel]};html=dashboard.insightsHTML();
  assert.match(html,/At least \$1\.60/);assert.doesNotMatch(html,/title="\$1\.60 per 1M tokens"|<span class="sr-only">\$1\.60 per 1M tokens/);
  dashboard.state.insights={range:"7d",providers:[priced,provider("Codex",{cost:5})]};html=dashboard.insightsHTML();assert.match(html,/<span class="dash-legend-head">Share<\/span>/);
  delete window.tokenTrackerQuotaNow;dashboard.state.ledger=null;
}
