import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

// Runs app.js in a sandbox with stubbed DOM and storage. Nothing here touches the real
// ~/.claude, ~/.codex or %LOCALAPPDATA% directories.
const app=fs.readFileSync(new URL("../token_tracker/static/app.js",import.meta.url),"utf8");
const fixedNow=new Date("2026-10-06T12:00:00Z").getTime();
let testNow=fixedNow;
const timers=new Map();let timerSequence=0;

function loadApp() {
  const nodes=new Map();
  const node=selector=>{
    if(!nodes.has(selector))nodes.set(selector,{innerHTML:"",textContent:"",value:"",hidden:false,title:"",className:"",dataset:{},attributes:{},offsetHeight:0,
      style:{setProperty(k,v){this[k]=v;}},contains(){return false;},querySelectorAll(){return [];},setAttribute(k,v){this.attributes[k]=String(v);},getAttribute(k){return this.attributes[k]??null;},removeAttribute(k){delete this.attributes[k];},
      classes:{},classList:{toggle(name,on){nodes.get(selector).classes[name]=Boolean(on);},add(){},remove(){}},addEventListener(){},removeEventListener(){},hasAttribute(){return true;},focus(){}});
    return nodes.get(selector);
  };
  class TestDate extends Date {constructor(...args){super(...(args.length?args:[testNow]));}static now(){return testNow;}}
  const saved=new Map();
  const context={window:{__TOKEN_TRACKER_TEST__:true,__TOKEN_TRACKER_RUNWAY__:true,__TOKEN_TRACKER_REFINED__:true,__TOKEN_TRACKER_COMPACT__:true},
    document:{querySelector:node,querySelectorAll(){return [];},documentElement:{style:{setProperty(key,value){this[key]=value;}}},hidden:false,activeElement:null,addEventListener(){}},
    localStorage:{getItem:key=>saved.get(key)??null,setItem:(key,value)=>saved.set(key,String(value))},
    fetch:async()=>({ok:true,json:async()=>({ok:true})}),Intl,Date:TestDate,Math,Number,JSON,AbortController,AbortSignal,
    setTimeout:(callback,delay)=>{const id=++timerSequence;timers.set(id,{callback,delay});return id;},
    clearTimeout:id=>timers.delete(id),setInterval(){return 0;}};
  context.window.addEventListener=()=>{};context.window.window=context.window;
  vm.createContext(context);vm.runInContext(app,context);
  return {T:context.window.__tokenTrackerTest,nodes};
}

function widget(){
  testNow=fixedNow;timers.clear();
  return loadApp();
}
{
  const w=widget();
  w.T.widgetLock.known=true; w.T.widgetLock.locked=false;
  w.T.state.runwaySnapshotPainted=true;
  for (const percent of [20, 80, 99]) {
    const p={name:"Codex",connected:true,limitsUpdatedAt:new Date(testNow).toISOString(),
      fiveHour:{percent,resetAt:testNow/1000+3600},weekly:{percent,resetAt:testNow/1000+86400}};
    w.T.renderResets([p]);
  }
  assert.equal(w.T.notch.autoExpanded,false,"low quota never opens the widget");
  assert.equal(w.nodes.has("#routing-alert"),false,"no routing alert is rendered");
  assert.doesNotMatch(app,/routingAlert|evaluateRoutingAlerts|routing-dismiss/);
  const html=fs.readFileSync(new URL("../token_tracker/static/runway.html",import.meta.url),"utf8");
  const css=fs.readFileSync(new URL("../token_tracker/static/runway_refined.css",import.meta.url),"utf8");
  assert.doesNotMatch(html,/routing-alert/); assert.doesNotMatch(css,/routing-alert|routing-dot/);
}

// Shared staleness: one "Updated … ago" marker, elapsed timers freeze at the last good update, nothing goes blank.
{
  const w=widget();
  w.T.state.lastOkAt=testNow-5000;
  assert.equal(w.T.dataIsStale(),false);w.T.renderUpdatedMarker();
  assert.equal(w.nodes.get("#updated-marker").hidden,true,"no marker while data is fresh");
  w.T.state.lastOkAt=testNow-125000;
  assert.equal(w.T.dataIsStale(),true);assert.equal(w.T.clockNow(),testNow-125000,"elapsed timers stop at the last update");
  w.T.renderUpdatedMarker();
  assert.equal(w.nodes.get("#updated-marker").hidden,false);assert.match(w.nodes.get("#updated-marker").textContent,/^Updated 2m ago$/);
}
// State precedence: Needs you > Running > Finished > Idle.
{
  const w=widget();
  const ended=new Date(testNow-60000).toISOString(),old=new Date(testNow-3*3600000).toISOString();
  const rank=chat=>w.T.runwayRank(chat);
  assert.ok(rank({pendingQuestion:"q",status:"running"})<rank({status:"failed"}));
  assert.ok(rank({status:"failed"})<rank({status:"running"}));
  assert.ok(rank({status:"running"})<rank({status:"idle",runEndedAt:ended}),"running before finished");
  assert.ok(rank({status:"idle",runEndedAt:ended})<rank({status:"idle",runEndedAt:old}),"finished before idle");
}
// Toolbar: Settings and Move-monitor live in the overflow menu; lock and pin are on the left, window actions stay on the right.
{
  const page=fs.readFileSync(new URL("../token_tracker/static/runway.html",import.meta.url),"utf8");
  const css=fs.readFileSync(new URL("../token_tracker/static/runway_refined.css",import.meta.url),"utf8");
  const toolbar=page.match(/<header class="titlebar compact-toolbar[\s\S]*?<\/header>/)[0];
  assert.doesNotMatch(toolbar,/id="toolbar-more"|id="toolbar-menu"/,"the overflow menu is gone");
  assert.match(toolbar,/id="dashboard-settings-open"[^>]*data-action="open-settings"/);
  assert.ok(toolbar.indexOf('id="widget-lock"')<toolbar.indexOf('id="dashboard-settings-open"')&&toolbar.indexOf('id="window-recenter"')<toolbar.indexOf('class="window-controls"'));
  assert.ok(toolbar.indexOf('id="widget-keep-open"')<toolbar.indexOf('id="sync-age"')&&toolbar.indexOf('id="sync-age"')<toolbar.indexOf('id="dashboard-open"'),"Refresh sits after the pin and before the Insights button");
  // One quota grid for both bands, identical tracks, "resets in" stated once.
  assert.match(css,/#resets,body\.runway-refined\.runway-compact:not\(\.phone\) \.weekly-resets\{box-sizing:border-box;grid-template-columns:68px minmax\(0,1fr\) minmax\(0,1fr\);column-gap:14px;padding:0 12px;\}/);
  assert.match(css,/\.resets::after,body\.runway-refined\.runway-compact:not\(\.phone\) \.weekly-resets::after\{content:"resets in"/);
  assert.match(css,/\.rr-provider\{/);
}
// Settings from the overflow menu opens the dashboard straight on the Settings tab.
{
  const dash=fs.readFileSync(new URL("../token_tracker/static/dashboard.js",import.meta.url),"utf8"),appSrc=fs.readFileSync(new URL("../token_tracker/static/app.js",import.meta.url),"utf8");
  assert.match(dash,/function open\(\{source,tab\}=\{\}\)\{\s*state\.open=true;state\.tab=tab==="settings"\?"settings":"insights";/);
  assert.match(appSrc,/window\.tokenTrackerOpenDashboard=tab=>openDashboard\(tab\);/);assert.match(appSrc,/action==='open-settings'\)\{openDashboard\('settings'\)/);
}
console.log("Token Tracker routing alert tests passed");
