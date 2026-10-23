import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const app=fs.readFileSync(new URL("../token_tracker/static/app.js",import.meta.url),"utf8");
const css=fs.readFileSync(new URL("../token_tracker/static/runway.css",import.meta.url),"utf8");
const refined=fs.readFileSync(new URL("../token_tracker/static/runway_refined.css",import.meta.url),"utf8");
const script=fs.readFileSync(new URL("../token_tracker/static/runway_review.js",import.meta.url),"utf8");
const page=fs.readFileSync(new URL("../token_tracker/static/runway_review.html",import.meta.url),"utf8");
const widgetPage=fs.readFileSync(new URL("../token_tracker/static/runway.html",import.meta.url),"utf8");

assert.match(page,/runway_review\.js/);
assert.match(script,/review:"1"/,"the replay is only available in the review fixture");
assert.match(script,/textContent="Replay expansion"/);
assert.match(script,/aria-label","Replay expansion motion model"/);
assert.match(script,/height targets \$\{distinct\} · bridge \$\{records\.length\} requests · viewport/);
assert.match(script,/\[420,448,476,504,532,560,532,504,476,448,420\]/,"the model covers expansion and return over each intermediate width");
assert.match(script,/capture\.records\.push\(\{height:requested,time:capture\.modelMs\}\)/,"the stable mock records requested targets");
assert.match(script,/for\(let index=0;index<steps\.length;index\+\+\)/,"steps wait serially for each browser viewport");
assert.match(script,/await parentFrame\(\);\s*await childFrame\(\);\s*await childFrame\(\);/,"outer and preview layouts settle before each width measurement");
assert.match(script,/win\.innerWidth!==expectedWidth/,"a replay refuses to report if the actual preview viewport did not change");
assert.match(script,/await Promise\.all\(pendingRequests\.splice\(0\)\)/,"every mocked bridge request settles before the next measurement");
assert.match(script,/stepsLog\.push\(\{viewport:win\.innerWidth,contentWidth:[^\n]+heightTargets:\[Math\.min/,
  "each replay step captures actual viewport, content, title lines and measured height target even when bridge deduplicates");
assert.match(script,/metrics\.dataset\.steps=JSON\.stringify\(stepsLog\)/,"the bounded per-step measurements remain available in the output DOM");
assert.match(script,/browser-only; no native timing/,"reported elapsed time is clearly a browser model, not native performance");
assert.match(script,/scenario!=="single-agent"/,"the replay is limited to the short single-agent fixture");
assert.match(script,/if\(motionCapture\.current\)/,"the stable review mock observes requests even through a cached compact bridge callback");
assert.match(script,/motionCapture\.current=priorCapture/,"the prior QA capture state is restored after replay");
assert.match(script,/T\.notch\.manual=true;T\.notch\.inspecting=true/,"the fixture is held in visible inspect mode during replay");
assert.match(script,/await new Promise\(resolve=>win\.requestAnimationFrame/,"replay waits for the inspect layout before measuring");
assert.match(script,/Math\.min\(420,Math\.max\(114/,"the existing native height cap remains represented");
assert.match(script,/data-action/);
assert.match(script,/\["answer","queue-answer","copy","open","refresh","watcher","runway-question","reset-settings"\]/,"review fixtures continue blocking real actions");
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
assert.match(app,/COMPACT\?60:120/);assert.match(app,/COMPACT\?110:160/);
assert.match(css,/--motion-fast:110ms; --motion-info:160ms; --motion-panel:200ms/);
assert.match(css,/--ease-standard:cubic-bezier\(\.16,1,\.3,1\)/);
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
assert.equal(T.applyMonitorResult([first],0,0),true);
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
context.window.pywebview.api.select_monitor=()=>new Promise(resolve=>{finishSelection=resolve;});
context.window.pywebview.api.get_monitors=async()=>[second,first];
T.getMonitorState().monitors=[first,second];T.getMonitorState().selected="display-a";
const selecting=T.toggleMonitor(),obsoleteRefresh=T.syncMonitors();
await obsoleteRefresh;
assert.equal(T.getMonitorState().selected,"display-a","metadata fetched during a monitor selection is ignored");
finishSelection([{...first,selected:false},{...second,selected:true}]);await selecting;
assert.equal(T.getMonitorState().selected,"display-b");

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
