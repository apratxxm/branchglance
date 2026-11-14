import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const app=fs.readFileSync(new URL("../token_tracker/static/app.js",import.meta.url),"utf8");
const css=fs.readFileSync(new URL("../token_tracker/static/runway.css",import.meta.url),"utf8");
const refined=fs.readFileSync(new URL("../token_tracker/static/runway_refined.css",import.meta.url),"utf8");
const widgetPage=fs.readFileSync(new URL("../token_tracker/static/runway.html",import.meta.url),"utf8");

assert.match(refined,/width:var\(--runway-inspect-layout-width,556px\)/,"inspect wrapping uses the frozen logical width before native expansion");
assert.doesNotMatch(refined,/scrollbar-gutter:stable/);
assert.match(refined,/scrollbar-width:none!important;-ms-overflow-style:none;scrollbar-gutter:auto!important/);
assert.match(refined,/::-webkit-scrollbar\{[^}]*display:none!important[^}]*\}/);
// The chat surface stays in flow between the toolbar (order 1) and the weekly row (order 3);
// pinning it absolutely let the weekly row jump above the chats.
assert.match(refined,/data-notch-mode="controls"\]:not\(\.phone\) \.layout\{display:none;\}/);
assert.match(refined,/data-notch-mode="inspect"\]:not\(\.phone\) \.layout\{order:2;display:flex;[^}]*animation:compact-layout-reveal/);
assert.match(widgetPage,/data-notch-mode="controls"/,"the static first paint matches the native controls size");
assert.doesNotMatch(refined,/\.layout\{position:absolute/);
assert.match(refined,/compact-toolbar\{[^}]*align-items:center/);
assert.match(refined,/data-notch-mode="inspect"\]:not\(\.phone\) \.runway-refined-row\{[^}]*align-items:center/);
assert.match(css,/\.ch-search\{display:flex;align-items:center/);
assert.match(refined,/@keyframes compact-layout-reveal\{from\{opacity:0;transform:translateY\(3px\);\}to\{opacity:1;transform:translateY\(0\);\}\}/);
assert.doesNotMatch(refined,/compact-content-enter/);
assert.match(app,/notch\.inspectTimer=setTimeout\([\s\S]*?,60\);/);assert.match(app,/notch\.exitTimer=setTimeout\([\s\S]*?,110\);/);
assert.match(css,/--motion-fast:110ms; --motion-info:160ms; --motion-panel:200ms/);
assert.match(css,/--ease-standard:cubic-bezier\(\.16,1,\.3,1\)/);
assert.match(css,/--motion-open-list:240ms; --notch-open-delay:91ms; --notch-open-suppression:331ms/);
assert.match(refined,/--notch-open-delay:143ms;--notch-open-suppression:383ms/);
assert.match(css,/notch-expanded \.chat-list\{opacity:1;transform:none;transition:opacity var\(--motion-open-list\)[^}]*var\(--notch-open-delay\)/,"the whole list fades together after the native expansion midpoint");
assert.match(css,/notch-opening \.runway-row-enter\{animation:none!important\}/,"open motion suppresses individual row arrivals");
const rowArrivalRule=css.match(/body:not\(\.phone\) \.runway-row-enter\{([^}]*)\}/)?.[1]||"";
assert.doesNotMatch(rowArrivalRule,/animation-delay/,"single-row arrivals have no stagger delay");
const openingMotion=app.slice(app.indexOf("function beginNotchOpening("),app.indexOf("const realDom",app.indexOf("function beginNotchOpening(")));
assert.match(openingMotion,/if\(reducedMotion\(\)\)return/,"reduced-motion opening skips the temporary animation state");
assert.match(css,/@media\(prefers-reduced-motion:reduce\)[\s\S]*?animation:none!important;transition:none!important/);
assert.match(app,/notchOpeningTimer:null/);
assert.match(app,/Date\.now\(\)<state\.startupCalmUntil/,"first-thirty-second row arrivals stay still");
assert.match(refined,/--motion-info:110ms;--motion-panel:140ms;--motion-reveal:220ms;--motion-source:240ms/,"compact source and panel motion are aligned to the reveal");
assert.match(refined,/compact-edge-drift 8s var\(--ease-standard\) infinite alternate/,"the approved quota edge drift remains");
assert.match(css,/animation:runway-message-expiry 10s linear both/);
assert.match(css,/nth-child\(2\)\{animation-delay:60ms\}/);assert.match(css,/nth-child\(3\)\{animation-delay:120ms\}/);
assert.doesNotMatch(css+refined,/transition:[^;}]*(?:border-color|\bheight\b|\bmargin)/,"no layout-property transitions besides the two quota bars");
assert.match(fs.readFileSync(new URL("../token_tracker/static/runway.html",import.meta.url),"utf8"),/id="hidden-chats-toggle"[^>]*data-action="show-hidden"/);
const setNotchSource=app.slice(app.indexOf("function setNotch("),app.indexOf("function expandForNewWork("));
assert.ok(setNotchSource.indexOf("applyInspectLayoutWidth(monitorState.current)")<setNotchSource.indexOf("notch.mode=mode"),"the predicted width is applied before inspect styles activate");

const nodes=new Map(),timers=new Map();let timerId=0,fakeNow=0;
const node=selector=>{
  if(!nodes.has(selector))nodes.set(selector,{innerHTML:"",textContent:"",value:"",hidden:false,title:"",className:"",dataset:{},attributes:{},offsetHeight:0,
    style:{setProperty(key,value){this[key]=value;}},contains(){return false;},querySelectorAll(){return [];},setAttribute(key,value){this.attributes[key]=String(value);},getAttribute(key){return this.attributes[key]??null;},
    classList:{toggle(){},add(){},remove(){}},addEventListener(){},removeEventListener(){},hasAttribute(){return true;},focus(){}});
  return nodes.get(selector);
};
const root=node("html");root.clientWidth=560;
const context={window:{__TOKEN_TRACKER_TEST__:true,__TOKEN_TRACKER_RUNWAY__:true,__TOKEN_TRACKER_REFINED__:true,__TOKEN_TRACKER_COMPACT__:true},
  document:{querySelector:node,querySelectorAll(){return [];},documentElement:root,hidden:false,activeElement:null,addEventListener(){}},
  localStorage:{getItem(){return null;},setItem(){}},fetch:async()=>({ok:true,json:async()=>({ok:true})}),Intl,Date,Math,Number,JSON,AbortController,AbortSignal,
  setTimeout(callback,delay){const id=++timerId;timers.set(id,{callback,delay,due:fakeNow+delay});return id;},clearTimeout(id){timers.delete(id);},setInterval(){return 0;}};
context.window.devicePixelRatio=1.25;context.window.visualViewport={scale:1};
context.window.addEventListener=()=>{};
vm.createContext(context);vm.runInContext(app,context);
const T=context.window.__tokenTrackerTest;
T.applyIdentityPayload({});
assert.equal(T.state.policyWarnings.length,0,"identity payloads without policyWarnings remain supported");
assert.doesNotMatch(T.policyWarningHTML(),/managed policy overrides/);
T.applyIdentityPayload({policyWarnings:["managed policy overrides some settings."]});
assert.match(T.watcherPopoverHTML("Codex"),/managed policy overrides some settings\./,"policy warnings appear in the settings popover");
const monitor=(id,scale,width,selected=false)=>({id,scale,selected,workArea:{x:0,y:0,width,height:900}});
assert.equal(T.predictedInspectLayoutWidth(monitor("fractional",1.25,1000)),556,"560 logical pixels at 125% scale, less the 4px inset");
assert.equal(T.predictedInspectLayoutWidth(monitor("narrow",.8,320)),396,"narrow work areas cap physical width before scale conversion");
assert.equal(T.predictedInspectLayoutWidth(monitor("invalid",0,1000)),null,"invalid DPI metadata is rejected");

const before=T.getInspectWidthState();
const first=monitor("display-a",1.25,1000,true),second=monitor("display-b",1.25,600,true);
assert.equal(T.applyMonitorResult([first],0),true);
assert.equal(root.style["--runway-inspect-layout-width"],"556px");
const pending=[],deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});pending.push(resolve);return promise;};
const stale=deferred(),current=deferred();let reads=0;
context.window.pywebview={api:{get_monitors:()=>++reads===1?stale:current}};
const staleRead=T.syncMonitors(),currentRead=T.syncMonitors();
pending[1]([second]);await currentRead;
assert.equal(T.getMonitorState().selected,"display-b");
pending[0]([first]);await staleRead;
assert.equal(T.getMonitorState().selected,"display-b","an older monitor reply cannot reverse a newer selection");
assert.equal(root.style["--runway-inspect-layout-width"],"476px","selection and its predicted width are applied together");

let finishSelection;
let selections=0;context.window.pywebview.api.select_monitor=()=>{selections++;return new Promise(resolve=>{finishSelection=resolve;});};
context.window.pywebview.api.get_monitors=async()=>[second,first];
T.getMonitorState().monitors=[first,second];T.getMonitorState().selected="display-a";
const selecting=T.selectMonitor("display-b"),obsoleteRefresh=T.syncMonitors();
await obsoleteRefresh;
assert.equal(T.getMonitorState().selected,"display-a","metadata fetched during a monitor selection is ignored");
finishSelection([{...first,selected:false},{...second,selected:true}]);await selecting;
assert.equal(T.getMonitorState().selected,"display-b");
T.widgetLock.known=true;T.widgetLock.locked=true;
await T.selectMonitor("display-a");assert.equal(selections,1,"monitor selection is blocked while the widget is locked");
T.widgetLock.locked=false;

context.window.pywebview.api.get_monitors=async()=>[{id:"bad-metadata",scale:0,workArea:{x:0,y:0,width:1000,height:900}}];
await T.syncMonitors();
assert.equal(root.style["--runway-inspect-layout-width"],"556px","missing or invalid metadata uses the bounded temporary 560px fallback");
context.window.pywebview.api.get_monitors=async()=>[first];
await T.syncMonitors();
assert.equal(root.style["--runway-inspect-layout-width"],"556px","late valid metadata converges back to the monitor prediction");

const advanceFakeTime=milliseconds=>{
  fakeNow+=milliseconds;
  for(const [id,timer] of [...timers])if(timer.due<=fakeNow){timers.delete(id);timer.callback();}
};
assert.equal(before.correctedWidths.size,0);
let finishSizing;
context.window.pywebview.api.set_notch=()=>new Promise(resolve=>{finishSizing=resolve;});
root.clientWidth=540;T.notch.mode="controls";T.notch.manual=true;T.notch.inspecting=true;
T.setNotch(true,true);
assert.equal(T.notch.mode,"inspect");
assert.equal(T.getInspectWidthState().bridgeBusy,true);
advanceFakeTime(200);
assert.equal(root.style["--runway-inspect-layout-width"],"556px","an unresolved native sizing promise cannot start the 180ms correction clock");
assert.equal(T.getInspectWidthState().correctionUsed,false);
finishSizing(true);
for(let i=0;i<6;i++)await Promise.resolve();
let settle=[...timers].find(([,timer])=>timer.delay===180);
assert.ok(settle,"the quiet interval begins only after the matching bridge acknowledgement");
assert.equal(T.getInspectWidthState().appliedSignature,T.getInspectWidthState().expectedSignature);
advanceFakeTime(180);
assert.equal(root.style["--runway-inspect-layout-width"],"536px","the post-ack correction uses settled clientWidth minus the inset");
assert.equal(T.getInspectWidthState().correctionUsed,true);
await T.syncMonitors();
assert.equal(root.style["--runway-inspect-layout-width"],"536px","unchanged monitor replies reuse the corrected width instead of oscillating");

const physicalSignature=T.getInspectWidthState().signature,oldCacheKey=T.getInspectWidthState().cacheKey,oldGeneration=T.getInspectWidthState().generation;
context.window.devicePixelRatio=1.5;
T.applyInspectLayoutWidth(T.getMonitorState().current);
assert.equal(T.getInspectWidthState().signature,physicalSignature,"CSS scale changes do not masquerade as physical monitor changes");
assert.notEqual(T.getInspectWidthState().cacheKey,oldCacheKey);
assert.ok(T.getInspectWidthState().generation>oldGeneration);
assert.equal(T.getInspectWidthState().correctedWidths.size,0,"a real CSS scale change invalidates the previous correction cache");
assert.equal(T.getInspectWidthState().correctionUsed,false,"a changed CSS scale permits one fresh settled correction");
assert.equal(root.style["--runway-inspect-layout-width"],"556px","the frozen width returns to the current monitor prediction before remeasurement");
context.window.pywebview.api.set_notch=async()=>true;
root.clientWidth=540;T.setNotch(true,true);
for(let i=0;i<6;i++)await Promise.resolve();
settle=[...timers].find(([,timer])=>timer.delay===180);
assert.ok(settle,"the fresh CSS-scale correction still waits for its matching bridge acknowledgement");
advanceFakeTime(180);
assert.equal(root.style["--runway-inspect-layout-width"],"536px");

// Dashboard swaps keep the native frame and page state in step.
{
  const flags=new Set(),content=node('#dashboard-content'),view=node('#dashboard-view'),calls=[],frames=[];
  content.classList={toggle(name,on){on?flags.add(name):flags.delete(name);},add(name){flags.add(name);},remove(name){flags.delete(name);}};
  let focused=false,reduced=false,requestedHeight=478;
  context.requestAnimationFrame=callback=>{frames.push(callback);return frames.length;};
  context.cancelAnimationFrame=()=>{};
  context.window.matchMedia=()=>({matches:reduced});
  context.Event=class{constructor(type){this.type=type;}};
  const widget=node('.widget'),widgetListeners=new Map();let leaves=0;
  widget.addEventListener=(name,fn)=>widgetListeners.set(name,fn);
  widget.dispatchEvent=event=>{if(event.type==='pointerleave')leaves++;widgetListeners.get(event.type)?.(event);};
  widget.getBoundingClientRect=()=>({left:0,top:0,width:300,height:100});
  context.window.screenX=100;context.window.screenY=200;T.notch.manual=false;T.notch.inspecting=false;T.notch.keyboardInspecting=false;T.setupNotch();
  context.window.pywebview.api.set_notch=async(...args)=>{calls.push(args);};
  context.window.TokenTrackerDashboard={windowHeight:()=>requestedHeight,open(){view.hidden=false;},close(){view.hidden=true;context.requestAnimationFrame(()=>{focused=true;});}};
  T.widgetLock.known=true;T.notch.mode='controls';T.notch.manual=false;T.notch.inspecting=false;
  T.notch.globalPointerX=500;T.notch.globalPointerY=250;T.notch.hover=true;
  assert.equal(T.openDashboard(),true);
  assert.equal(T.dashboardState.height,478);requestedHeight=608;T.setNotch(false,true);assert.equal(T.dashboardState.height,478,"range changes preserve the opening height");
  assert.ok(flags.has('dashboard-transition-hidden'));
  const firstOpen=T.dashboardState.generation;
  context.window.tokenTrackerNotchSettled('dashboard',true,null);assert.ok(flags.has('dashboard-transition-hidden'));
  context.window.tokenTrackerNotchSettled('dashboard',true,firstOpen);
  assert.ok(!flags.has('dashboard-transition-hidden'));
  assert.equal(T.closeDashboard(),true);
  assert.equal(T.dashboardState.open,false);assert.equal(T.dashboardState.closing,true);
  assert.equal(T.closeDashboard(),false,"a second close does not restart the transition");
  advanceFakeTime(90);assert.equal(T.notch.mode,'dashboard');
  context.window.tokenTrackerNotchSettled('controls',false,T.dashboardState.generation);
  assert.equal(T.notch.mode,'controls');assert.equal(focused,false);assert.equal(leaves,0);advanceFakeTime(90);assert.equal(leaves,1,"an outside pointer follows the existing leave path once after the swap");assert.equal(T.notch.hover,false);assert.ok(T.notch.exitTimer!=null);frames.splice(0).forEach(callback=>callback());assert.equal(focused,true);
  for(let i=0;i<6;i++)await Promise.resolve();
  T.notch.globalPointerX=150;T.notch.globalPointerY=250;T.openDashboard();assert.ok(flags.has('dashboard-transition-hidden'));context.window.tokenTrackerNotchSettled('dashboard',true,firstOpen);assert.ok(flags.has('dashboard-transition-hidden'));advanceFakeTime(406);assert.ok(!flags.has('dashboard-transition-hidden'));
  T.closeDashboard();const closeA=T.dashboardState.generation;advanceFakeTime(90);T.openDashboard();context.window.tokenTrackerNotchSettled('controls',false,closeA);
  assert.equal(T.notch.mode,'dashboard');assert.equal(view.hidden,false);
  T.closeDashboard();advanceFakeTime(90);context.window.tokenTrackerNotchSettled('controls',false,closeA);assert.equal(T.notch.mode,'dashboard');advanceFakeTime(340);assert.equal(T.notch.mode,'controls');advanceFakeTime(90);assert.equal(leaves,1,"an inside pointer does not synthesize a leave");
  reduced=true;T.openDashboard();assert.ok(!flags.has('dashboard-transition-hidden'));T.closeDashboard();assert.equal(T.notch.mode,'controls');
  assert.ok(calls.some(args=>args[3]==='dashboard'));
}

// The header controls follow the requested tab as soon as the body fade starts.
{
  const dashboard=fs.readFileSync(new URL('../token_tracker/static/dashboard.js',import.meta.url),'utf8');
  const view={dataset:{tab:'insights'},addEventListener(){}},content={classList:{add(){},remove(){}}},pending=new Map();let next=0;
  const win={__TOKEN_TRACKER_DASHBOARD_TEST__:true,addEventListener(){}};
  const doc={querySelector:s=>s==='#dashboard-view'?view:s==='#dashboard-content'?content:null,querySelectorAll:()=>[],addEventListener(){}};
  const sandbox={window:win,document:doc,Intl,Date,Math,Number,JSON,Map,Set,Array,Promise,localStorage:{getItem(){return null;},setItem(){}},location:{origin:'http://localhost'},fetch:async()=>({ok:false}),setTimeout:fn=>{pending.set(++next,fn);return next;},clearTimeout:id=>pending.delete(id),setInterval:()=>1,clearInterval(){},requestAnimationFrame(){}};
  vm.createContext(sandbox);vm.runInContext(dashboard,sandbox);
  const state=win.__tokenTrackerDashboardTest.state;state.open=true;
  win.TokenTrackerDashboard.selectTab('settings');assert.equal(view.dataset.tab,'settings');assert.equal(state.tab,'insights');
  win.TokenTrackerDashboard.selectTab('insights');assert.equal(view.dataset.tab,'insights');assert.equal(pending.size,0,'cancelling the pending switch restores the controls');
  state.tab='settings';view.dataset.tab='settings';win.TokenTrackerDashboard.selectTab('insights');assert.equal(view.dataset.tab,'insights');assert.equal(state.tab,'settings');
}
