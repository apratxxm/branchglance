import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const app=fs.readFileSync(new URL("../token_tracker/static/app.js",import.meta.url),"utf8");
const css=fs.readFileSync(new URL("../token_tracker/static/runway.css",import.meta.url),"utf8");
const dashboardCss=fs.readFileSync(new URL("../token_tracker/static/dashboard.css",import.meta.url),"utf8");
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
assert.match(dashboardCss,/body\.dashboard-closing \.dashboard-header\{animation:dashboard-header-close 90ms ease-in 250ms both;\}/,"the header fades during the final 90ms of the native shrink");
assert.match(dashboardCss,/@keyframes dashboard-header-close\{from\{opacity:1\}to\{opacity:0\}\}/);
assert.match(dashboardCss,/body\.dashboard-crossfade \.widget\{animation:dashboard-fade-in 90ms ease both;\}/,"the widget fades in after the dashboard layer is hidden");
assert.doesNotMatch(dashboardCss,/dashboard-crossfade \.dashboard-header/);
assert.match(dashboardCss,/@media\(prefers-reduced-motion:reduce\)\{[^}]*\}body\.dashboard-closing \.dashboard-header\{animation:none;\}/,"reduced motion skips the closing header fade");
assert.match(dashboardCss,/#dashboard-view\[data-open-compact="true"\] #dashboard-insights:not\(\[hidden\]\)\{[^}]*height:max-content;max-height:100%;overflow-x:hidden;overflow-y:auto/,"a compact-open dashboard keeps taller range content scrollable inside the fixed frame");
assert.match(dashboardCss,/#dashboard-view\[data-open-compact="true"\] #dashboard-insights::-webkit-scrollbar\{display:block!important;width:4px!important;height:4px!important;\}/);
assert.match(dashboardCss,/#dashboard-view\[data-open-compact="true"\] #dashboard-insights \.dash-lower\{flex:0 0 auto;\}/,"compact-open lower content keeps its full height inside the scroll area");
assert.match(dashboardCss,/#dashboard-view\[data-open-compact="true"\] #dashboard-insights \.dash-chart-section\{min-height:194px;\}/,"compact-open scrolling preserves the chart section minimum height");
assert.match(dashboardCss,/#dashboard-insights:not\(\[hidden\]\)\{[^}]*overflow-x:hidden;overflow-y:auto;[^}]*scrollbar-width:thin;scrollbar-color:var\(--d-mute\) transparent/,"Insights scrolls vertically at every panel height");
assert.match(dashboardCss,/\.dash-insights-body\{display:flex;flex:1 1 auto;flex-direction:column;min-height:0;\}/,"the chart and fixed-height middle section share the available height");
assert.match(dashboardCss,/\.dash-chart-section\{flex:1 1 auto;[^}]*min-height:194px/,"the chart takes remaining height while retaining the V160 minimum");
const switchingSelector=dashboardCss.match(/([^{}]+)\{opacity:\.12;transform:translateY\(2px\);\}/)?.[1].trim()||"";
const hiddenSelector='body[data-notch-mode="dashboard"] #dashboard-content.dashboard-transition-hidden';
const specificity=selector=>{const normalized=selector.replace(/:not\(([^)]*)\)/g,"$1");return [(normalized.match(/#[\w-]+/g)||[]).length,(normalized.match(/\.[\w-]+|\[[^\]]+\]|:[\w-]+/g)||[]).length,(normalized.match(/(?:^|\s|[>+~])(?:[a-z][\w-]*)/gi)||[]).length];};
assert.match(switchingSelector,/#dashboard-view #dashboard-content\.is-switching:not\(\.dashboard-transition-hidden\)/,"metric switching excludes the native open/close hidden state");
assert.ok(specificity(switchingSelector).some((value,index)=>value>specificity(hiddenSelector)[index]&&specificity(switchingSelector).slice(0,index).every((part,partIndex)=>part===specificity(hiddenSelector)[partIndex])),"metric switching specificity beats the native hidden-state rule");
assert.match(dashboardCss,/\.dash-plot\{[^}]*min-height:140px/,"compact scrolling preserves the chart minimum height");assert.match(dashboardCss,/\.dash-dates\{[^}]*flex:0 0 20px;height:20px/,"compact scrolling preserves the complete date row");
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
const bodyClasses=new Set();
const context={window:{__TOKEN_TRACKER_TEST__:true,__TOKEN_TRACKER_RUNWAY__:true,__TOKEN_TRACKER_REFINED__:true,__TOKEN_TRACKER_COMPACT__:true},
  document:{querySelector:node,querySelectorAll(){return [];},documentElement:root,body:{dataset:{},classList:{add(name){bodyClasses.add(name);},remove(name){bodyClasses.delete(name);},contains(name){return bodyClasses.has(name);},toggle(name,on){on?bodyClasses.add(name):bodyClasses.delete(name);}}},hidden:false,activeElement:null,addEventListener(){}},
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
  let focused=false,reduced=false,requestedHeight=478,selectedTab=null,openedTab=null;
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
  context.window.TokenTrackerDashboard={windowHeight:()=>requestedHeight,open({tab}={}){view.hidden=false;openedTab=tab;selectedTab=tab;},selectTab(tab){selectedTab=tab;},close(){view.hidden=true;context.requestAnimationFrame(()=>{focused=true;});}};
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
  assert.ok(!bodyClasses.has('dashboard-crossfade'),"the widget fade waits until native settle");
  advanceFakeTime(90);assert.equal(T.notch.mode,'dashboard');
  context.window.tokenTrackerNotchSettled('controls',false,T.dashboardState.generation);
  assert.equal(T.notch.mode,'controls');assert.ok(bodyClasses.has('dashboard-crossfade'),"settle hides the dashboard layer before starting the 90ms widget fade");assert.equal(view.hidden,true,"no dashboard text remains visible once the window reaches widget size");assert.equal(focused,false);assert.equal(leaves,0);advanceFakeTime(90);assert.ok(!bodyClasses.has('dashboard-crossfade'));assert.equal(leaves,1,"an outside pointer follows the existing leave path once after the swap");assert.equal(T.notch.hover,false);assert.ok(T.notch.exitTimer!=null);frames.splice(0).forEach(callback=>callback());assert.equal(focused,true);
  for(let i=0;i<6;i++)await Promise.resolve();
  T.notch.globalPointerX=150;T.notch.globalPointerY=250;T.openDashboard();assert.ok(flags.has('dashboard-transition-hidden'));context.window.tokenTrackerNotchSettled('dashboard',true,firstOpen);assert.ok(flags.has('dashboard-transition-hidden'));advanceFakeTime(406);assert.ok(!flags.has('dashboard-transition-hidden'));
  T.closeDashboard();const closeA=T.dashboardState.generation;advanceFakeTime(90);T.openDashboard();context.window.tokenTrackerNotchSettled('controls',false,closeA);
  assert.equal(T.notch.mode,'dashboard');assert.equal(view.hidden,false);
  T.closeDashboard();advanceFakeTime(90);context.window.tokenTrackerNotchSettled('controls',false,closeA);assert.equal(T.notch.mode,'dashboard');assert.ok(!bodyClasses.has('dashboard-crossfade'));advanceFakeTime(340);assert.equal(T.notch.mode,'controls');assert.ok(bodyClasses.has('dashboard-crossfade'),"the fallback hides the dashboard layer before starting the widget fade");assert.equal(view.hidden,true,"the fallback leaves no dashboard text visible at widget size");advanceFakeTime(90);assert.ok(!bodyClasses.has('dashboard-crossfade'));assert.equal(leaves,1,"an inside pointer does not synthesize a leave");
  T.openDashboard();T.closeDashboard();const swapClose=T.dashboardState.generation;advanceFakeTime(90);advanceFakeTime(340);
  assert.equal(T.dashboardState.swapping,true,"the regression reopens during the 90ms widget swap");
  const returnMode=T.dashboardState.returnMode,returnHeight=T.dashboardState.returnHeight;
  assert.equal(view.hidden,true,"the widget-size swap has an empty dashboard layer");assert.equal(T.openDashboard('settings'),true);assert.equal(selectedTab,'settings');assert.equal(openedTab,'insights');
  assert.equal(T.dashboardState.returnMode,returnMode);assert.equal(T.dashboardState.returnHeight,returnHeight);
  context.window.tokenTrackerNotchSettled('controls',false,swapClose);advanceFakeTime(90);
  assert.equal(T.dashboardState.open,true);assert.equal(view.hidden,false);assert.equal(T.dashboardState.returnMode,returnMode,"a stale close cannot hide or reset the reopened dashboard");
  T.closeDashboard();advanceFakeTime(90);advanceFakeTime(340);advanceFakeTime(90);
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
  const api=win.__tokenTrackerDashboardTest,state=api.state;state.open=true;state.range='24h';
  state.tab='settings';state.range='30d';state.resultRange='30d';state.insights={range:'30d',providers:[{name:'Codex',totals:{tokens:0},series:[],models:[]}]};
  assert.equal(win.TokenTrackerDashboard.windowHeight(),478,"a cached empty selected range opens compact even when Settings is selected");
  state.insights={range:'30d',providers:[{name:'Codex',totals:{tokens:500},series:[],models:[]}]};assert.equal(win.TokenTrackerDashboard.windowHeight(),608,"a populated selected-range cache keeps the normal open height");
  state.insights=null;state.resultRange=null;state.tab='insights';state.range='24h';
  win.TokenTrackerDashboard.selectTab('settings');assert.equal(view.dataset.tab,'settings');assert.equal(state.tab,'insights');
  api.setRange('24h');assert.equal(view.dataset.tab,'insights');assert.equal(state.tab,'insights');assert.equal(pending.size,0,'an already-selected range cancels the pending tab transition and restores Insights controls');
  win.TokenTrackerDashboard.selectTab('settings');api.setRange('7d');assert.equal(view.dataset.tab,'insights');assert.equal(state.tab,'insights','a range change restores controls to the committed Insights tab');
  win.TokenTrackerDashboard.selectTab('insights');assert.equal(view.dataset.tab,'insights');assert.equal(pending.size,0,'cancelling the pending switch restores the controls');
  state.tab='settings';view.dataset.tab='settings';win.TokenTrackerDashboard.selectTab('insights');assert.equal(view.dataset.tab,'insights');assert.equal(state.tab,'settings');
}

// Range and metric changes share one pending fade, and Cost availability is checked again at commit.
{
  const dashboard=fs.readFileSync(new URL('../token_tracker/static/dashboard.js',import.meta.url),'utf8'),pending=new Map(),values=new Map();let next=0;
  const classes=new Set(),view={dataset:{tab:'insights'},addEventListener(){},setAttribute(){}},content={classList:{add(name){classes.add(name);},remove(name){classes.delete(name);}}},insights={innerHTML:'',querySelector(){return null;},querySelectorAll(){return [];},insertAdjacentHTML(){}};
  const nodes={'#dashboard-view':view,'#dashboard-content':content,'#dashboard-insights':insights};
  const win={__TOKEN_TRACKER_DASHBOARD_TEST__:true,addEventListener(){},dispatchEvent(){},TokenTrackerSetClockFormat(){}};
  const doc={activeElement:null,querySelector:s=>nodes[s]||null,querySelectorAll:()=>[],addEventListener(){}};
  const sandbox={window:win,document:doc,Intl,Date,Math,Number,JSON,Map,Set,Array,Promise,Event:class{constructor(type){this.type=type;}},localStorage:{getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,String(value))},location:{origin:'http://localhost'},fetch:async()=>({ok:false}),setTimeout:(fn,delay)=>{const id=++next;pending.set(id,{fn,delay});return id;},clearTimeout:id=>pending.delete(id),setInterval:()=>1,clearInterval(){},requestAnimationFrame(fn){fn();}};
  vm.createContext(sandbox);vm.runInContext(dashboard,sandbox);
  const api=win.__tokenTrackerDashboardTest,state=api.state;state.open=true;state.range='30d';state.resultRange='30d';state.tab='insights';state.metric='tokens';
  const priced={range:'30d',providers:[{name:'Codex',totals:{tokens:500,apiEquivalentCost:2},series:[],models:[]}]};
  const unpriced={range:'30d',providers:[{name:'Codex',totals:{tokens:500,apiEquivalentCost:null},series:[],models:[]}]};
  const finish=()=>{const [id,timer]=[...pending].find(([,item])=>item.delay===90)||[];assert.ok(timer,'one shared 90ms view-change fade is pending');pending.delete(id);timer.fn();};
  state.insights=priced;api.setMetric('cost');state.insights=unpriced;finish();
  assert.equal(state.metric,'tokens','a refresh to fully unpriced data cancels the pending Cost selection');assert.equal(values.has('branchglanceInsightsMetric'),false,'the rejected Cost selection is not persisted');
  for(const order of [['metric','range'],['range','metric']]){
    state.range='30d';state.metric='tokens';state.insights=priced;state.resultRange='30d';state.resultValue=null;values.clear();
    for(const action of order)action==='metric'?api.setMetric('cost'):api.setRange('7d');
    assert.equal(pending.size,1,'range and metric requests retain one shared fade timer');finish();
    assert.equal(state.range,'7d',`${order.join(' then ')} commits the latest range`);assert.equal(state.metric,'cost',`${order.join(' then ')} commits the latest metric`);
    assert.equal(values.get('branchglanceInsightsMetric'),'cost');assert.equal(values.get('branchglanceInsightsRange'),'7d');
  }
}

// Metric changes use the same cancellable 90ms fade as range changes.
{
  const dashboard=fs.readFileSync(new URL('../token_tracker/static/dashboard.js',import.meta.url),'utf8'),pending=new Map(),values=new Map();let next=0,rendered=0;
  const classes=new Set(),view={dataset:{},addEventListener(){},setAttribute(){}},content={classList:{add(name){classes.add(name);},remove(name){classes.delete(name);}}},insights={innerHTML:'',querySelectorAll(){return [];}};
  const metricButtons=['tokens','cost'].map(metric=>({dataset:{dashboardMetric:metric},attributes:{},classList:{toggle(name,on){on?this[name]=true:delete this[name];}},setAttribute(name,value){this.attributes[name]=String(value);}}));
  const nodes={'#dashboard-view':view,'#dashboard-content':content,'#dashboard-insights':insights};
  const win={__TOKEN_TRACKER_DASHBOARD_TEST__:true,addEventListener(){},dispatchEvent(){},TokenTrackerSetClockFormat(){}};
  const doc={activeElement:null,querySelector:s=>nodes[s]||null,querySelectorAll:s=>s==='[data-dashboard-metric]'?metricButtons:[],addEventListener(){}};
  const sandbox={window:win,document:doc,Intl,Date,Math,Number,JSON,Map,Set,Array,Promise,Event:class{constructor(type){this.type=type;}},localStorage:{getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,String(value))},location:{origin:'http://localhost'},fetch:async()=>({ok:false}),setTimeout:(fn,delay)=>{const id=++next;pending.set(id,{fn,delay});return id;},clearTimeout:id=>pending.delete(id),setInterval:()=>1,clearInterval(){},requestAnimationFrame(fn){rendered++;fn();}};
  vm.createContext(sandbox);vm.runInContext(dashboard,sandbox);
  const api=win.__tokenTrackerDashboardTest,state=api.state;state.open=true;state.metric='tokens';
  api.setMetric('cost');assert.equal(state.metric,'tokens');assert.equal(values.has('branchglanceInsightsMetric'),false,'metric preference waits for the fade-out');assert.ok(classes.has('is-switching'),'metric content waits for the fade-out');
  api.setMetric('tokens');assert.equal(pending.size,0,'a newer metric selection cancels a stale pending switch');assert.ok(!classes.has('is-switching'));
  api.setMetric('cost');const timer=[...pending.values()].find(item=>item.delay===90);assert.ok(timer,'metric selection schedules the existing 90ms switch');timer.fn();
  assert.equal(state.metric,'cost');assert.equal(values.get('branchglanceInsightsMetric'),'cost');assert.equal(metricButtons[0].attributes['aria-pressed'],'false');assert.equal(metricButtons[1].attributes['aria-pressed'],'true');assert.ok(!classes.has('is-switching'),'the new metric is revealed after the fade-out');assert.ok(insights.innerHTML.length>0,'the metric content is rendered with the new selection');assert.ok(rendered>0);
}

// A compact-open dashboard keeps its chosen native height while ranges switch from populated to empty.
{
  const dashboard=fs.readFileSync(new URL('../token_tracker/static/dashboard.js',import.meta.url),'utf8');let rangePref='24h',nativeResizes=0;
  context.localStorage.getItem=key=>key==='branchglanceInsightsRange'?rangePref:null;
  context.localStorage.setItem=(key,value)=>{if(key==='branchglanceInsightsRange')rangePref=String(value);};
  context.window.__TOKEN_TRACKER_DASHBOARD_TEST__=true;context.window.dispatchEvent=()=>{};context.Event=class{constructor(type){this.type=type;}};context.requestAnimationFrame=()=>{};context.clearInterval=()=>{};context.fetch=()=>new Promise(()=>{});
  node('#dashboard-insights').querySelector=()=>null;node('#dashboard-insights').insertAdjacentHTML=()=>{};
  context.window.pywebview.api.set_notch=async()=>{nativeResizes++;};
  vm.runInContext(dashboard,context);
  const api=context.window.__tokenTrackerDashboardTest,state=api.state;
  state.insights={providers:[{name:'Codex',totals:{tokens:0,apiEquivalentCost:null,sessions:0,activeHours:0,subagentsActive:0,cacheHitRate:null},series:[],models:[]}]};
  state.insights.range='24h';state.resultRange='24h';
  T.notch.mode='controls';T.notch.manual=false;T.notch.inspecting=false;
  assert.equal(T.openDashboard(),true);assert.equal(T.dashboardState.height,478);assert.equal(node('#dashboard-view').dataset.openCompact,'true');
  const openingResizes=nativeResizes;
  state.insights={providers:[{name:'Codex',totals:{tokens:500,apiEquivalentCost:2,sessions:1,activeHours:1,subagentsActive:0,cacheHitRate:null},series:[],models:[]}]};
  api.setRange('30d');advanceFakeTime(90);assert.equal(state.range,'30d');assert.equal(T.dashboardState.height,478);
  state.insights={providers:[{name:'Codex',totals:{tokens:0,apiEquivalentCost:null,sessions:0,activeHours:0,subagentsActive:0,cacheHitRate:null},series:[],models:[]}]};
  api.setRange('7d');advanceFakeTime(90);assert.equal(state.range,'7d');assert.equal(T.dashboardState.height,478);
  assert.equal(nativeResizes,openingResizes,'populated and empty range changes do not call set_notch to resize the OS window');
  T.closeDashboard();advanceFakeTime(90);context.window.tokenTrackerNotchSettled('controls',false,T.dashboardState.generation);advanceFakeTime(90);
  rangePref='30d';state.resultRange='7d';state.insights.range='7d';state.insights.providers[0].totals.tokens=500;
  assert.equal(T.openDashboard(),true);assert.equal(T.dashboardState.height,608,"a populated cached response for another range keeps the normal open-height rule");
  T.closeDashboard();advanceFakeTime(90);context.window.tokenTrackerNotchSettled('controls',false,T.dashboardState.generation);advanceFakeTime(90);
}
