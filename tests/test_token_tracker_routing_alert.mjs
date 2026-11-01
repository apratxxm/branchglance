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
  const context={window:{__TOKEN_TRACKER_TEST__:true,__TOKEN_TRACKER_RUNWAY__:true},
    document:{querySelector:node,querySelectorAll(){return [];},documentElement:{style:{setProperty(key,value){this[key]=value;}}},hidden:false,activeElement:null,addEventListener(){}},
    localStorage:{getItem:key=>saved.get(key)??null,setItem:(key,value)=>saved.set(key,String(value))},
    fetch:async()=>({ok:true,json:async()=>({ok:true})}),Intl,Date:TestDate,Math,Number,JSON,AbortController,AbortSignal,
    setTimeout:(callback,delay)=>{const id=++timerSequence;timers.set(id,{callback,delay});return id;},
    clearTimeout:id=>timers.delete(id),setInterval(){return 0;}};
  context.window.addEventListener=()=>{};context.window.window=context.window;
  vm.createContext(context);vm.runInContext(app,context);
  return {T:context.window.__tokenTrackerTest,nodes};
}

// Codex quota as the client holds it: "used" percentages plus reset targets (unix seconds).
const codex=({fiveHour=30,weekly=30,fiveReset=testNow/1000+3600,weeklyReset=testNow/1000+3*86400,sampledAt=testNow}={})=>({
  name:"Codex",connected:true,limitsUpdatedAt:new Date(sampledAt).toISOString(),
  fiveHour:{percent:fiveHour,resetAt:fiveReset},weekly:{percent:weekly,resetAt:weeklyReset}});
const claudeP=({fiveHour=20,weekly=20,fiveReset=testNow/1000+3600,weeklyReset=testNow/1000+3*86400,sampledAt=testNow}={})=>({
  name:"Claude",connected:true,limitsUpdatedAt:new Date(sampledAt).toISOString(),
  fiveHour:{percent:fiveHour,resetAt:fiveReset},weekly:{percent:weekly,resetAt:weeklyReset}});
// A healthy Claude (20% used) unless a test says otherwise.
const claude=claudeP();
// Wording uses the bars' framing: percent USED, the window, and when it resets.
const used=(name,window,percent)=>new RegExp("^"+name+" "+window+" "+percent+"% used · resets [^—]+ — ");
const codexMsg=percent=>new RegExp("^Codex 5-hour "+percent+"% used · resets \\d+h \\d{2}m — Haiku workers, Sol reviews only$");
const claudeMsg=percent=>new RegExp("^Claude 5-hour "+percent+"% used · resets \\d+h \\d{2}m — Terra builders, Sol reviews$");

function widget({unlocked=false}={}){
  testNow=fixedNow;timers.clear();
  const {T,nodes}=loadApp();
  T.routingStates.clear();
  if(unlocked){T.widgetLock.known=true;T.widgetLock.locked=false;T.state.runwaySnapshotPainted=true;}
  return {T,nodes,alert:()=>T.getRoutingAlert()?.message??null,evaluate:provider=>T.evaluateRoutingAlerts([provider,claude])};
}

// Launch baseline: a healthy reading sets state without alerting.
{
  const w=widget();
  w.evaluate(codex({fiveHour:30,weekly:30}));
  assert.equal(w.alert(),null,"healthy launch does not alert");
}

// No alert at or above 40% left (used 60 and below), including the 40.1 edge.
{
  const w=widget();
  for(const used of [40,45,55,59.9,60]){w.evaluate(codex({fiveHour:used,weekly:20}));assert.equal(w.alert(),null,"used "+used+" must not alert");}
}

// Fires once on the crossing below 40% left, shows the rounded-down value, and does not repeat.
{
  const w=widget({unlocked:true});
  w.evaluate(codex({fiveHour:50}));
  w.evaluate(codex({fiveHour:61}));
  assert.match(w.alert(),codexMsg(61),"crossing below 40% left fires once, in used terms");
  assert.equal(w.T.getRoutingAlert().provider,"Codex");
  assert.equal(w.T.getRoutingNotch().autoExpanded,true,"the crossing reveals the widget like a new question");
  w.T.dismissRoutingAlert();
  assert.equal(w.alert(),null,"dismiss clears the banner");
  w.T.getRoutingNotch().autoExpanded=false;
  for(const used of [65,70,80,66]){w.evaluate(codex({fiveHour:used}));}
  assert.equal(w.alert(),null,"polls while still below do not bring the banner back");
  assert.equal(w.T.getRoutingNotch().autoExpanded,false,"polls while still below do not reveal again");
  assert.equal(w.T.routingStates.get("Codex").fired,true);
}

// Re-arm only after recovering above threshold + 5 points (left above 45).
{
  const w=widget();
  w.evaluate(codex({fiveHour:50}));
  w.evaluate(codex({fiveHour:61}));
  assert.match(w.alert(),codexMsg(61));
  w.T.dismissRoutingAlert();
  w.evaluate(codex({fiveHour:56}));
  assert.equal(w.T.routingStates.get("Codex").fired,true,"left 44 stays inside the hysteresis band");
  w.evaluate(codex({fiveHour:55}));
  assert.equal(w.T.routingStates.get("Codex").fired,true,"left exactly 45 is not above threshold + 5");
  w.evaluate(codex({fiveHour:54}));
  assert.equal(w.T.routingStates.get("Codex").fired,false,"left 46 re-arms");
  assert.equal(w.alert(),null,"re-arming alone does not show a banner");
  w.evaluate(codex({fiveHour:63}));
  assert.match(w.alert(),codexMsg(63),"crossing again after re-arm fires");
}

// The lower of the 5-hour and weekly windows decides the crossing and the shown value.
{
  const w=widget();
  w.evaluate(codex({fiveHour:10,weekly:30}));
  w.evaluate(codex({fiveHour:10,weekly:63}));
  assert.match(w.alert(),used("Codex","weekly",63),"weekly at 63% used alerts and is named, even though 5-hour has 90% left");
}
{
  const w=widget();
  w.evaluate(codex({fiveHour:30,weekly:10}));
  w.evaluate(codex({fiveHour:62,weekly:10}));
  assert.match(w.alert(),codexMsg(62),"5-hour at 62% used alerts even though weekly has 90% left");
}
{
  const w=widget();
  w.evaluate(codex({fiveHour:20,weekly:45}));
  assert.equal(w.alert(),null,"min(80, 55) = 55 left does not alert");
}

// A window reset re-arms the alert. Same reset target: no re-arm. New target: re-arm and fire again if still low.
{
  const w=widget();
  w.evaluate(codex({fiveHour:50}));
  w.evaluate(codex({fiveHour:61}));
  assert.match(w.alert(),codexMsg(61));
  w.T.dismissRoutingAlert();
  w.evaluate(codex({fiveHour:62}));
  assert.equal(w.T.routingStates.get("Codex").fired,true,"same reset target does not re-arm");
  assert.equal(w.alert(),null,"no repeat banner for the same window");
  w.evaluate(codex({fiveHour:62,fiveReset:fixedNow/1000+5*3600}));
  assert.match(w.alert(),/^Codex 5-hour 62% used · resets 5h 00m/,"a new reset target re-arms and fires for the still-low window");
}

// Stale samples (over 10 minutes old) never fire.
{
  const w=widget();
  w.evaluate(codex({fiveHour:50}));
  w.evaluate(codex({fiveHour:61,sampledAt:fixedNow-11*60000}));
  assert.equal(w.alert(),null,"a stale 61% reading does not alert");
  assert.equal(w.T.routingStates.get("Codex").fired,false);
}

// Launch baseline already below threshold: no reveal, and no repeat while it stays low.
{
  const w=widget({unlocked:true});
  w.evaluate(codex({fiveHour:70}));
  assert.equal(w.alert(),null,"already-low launch does not alert");
  assert.equal(w.T.getRoutingNotch().autoExpanded,false);
  w.evaluate(codex({fiveHour:75}));
  assert.equal(w.alert(),null,"a repeat low reading does not alert");
}

// Claude low and Codex at least 50% left: fire once, then re-arm on recovery or reset.
{
  const w=widget();
  const sample=(claudeUsed,codexUsed=50,reset=fixedNow/1000+3600)=>w.T.evaluateRoutingAlerts([
    codex({fiveHour:codexUsed,weekly:codexUsed}),
    claudeP({fiveHour:claudeUsed,fiveReset:reset,weekly:20})
  ]);
  sample(50); sample(61);
  assert.match(w.alert(),claudeMsg(61));
  assert.doesNotMatch(w.alert(),/Haiku/,"Claude low never points at Claude's own small model");
  w.T.dismissRoutingAlert(); sample(70); assert.equal(w.alert(),null);
  sample(55); assert.equal(w.T.routingStates.get("Claude").fired,true);
  sample(54); assert.equal(w.T.routingStates.get("Claude").fired,false);
  sample(65); assert.ok(w.alert()); w.T.dismissRoutingAlert();
  sample(65,50,fixedNow/1000+7200); assert.ok(w.alert(),"reset re-arms Claude rule");
}
// Neither condition fires when Codex has less than 50% but at least 40% left.
{
  const w=widget();
  const sample=used=>w.T.evaluateRoutingAlerts([codex({fiveHour:55}),claudeP({fiveHour:used,weekly:20})]);
  sample(50); sample(70); assert.equal(w.alert(),null);
}
// The production rules cannot overlap: exercise precedence using synthetic overlapping rules.
{
  const w=widget();
  const rules=[
    {id:"general",provider:"Codex",threshold:40,message:()=>"general"},
    {id:"specific",provider:"Codex",threshold:40,minimums:{Claude:50},message:()=>"specific"}
  ];
  const sample=used=>w.T.evaluateRoutingAlerts([codex({fiveHour:used}),claudeP({fiveHour:20,weekly:20})],rules);
  sample(50); sample(61); assert.equal(w.alert(),"specific");
}

// Rule matrix. Never suggests a lane that is out of quota.
{
  // Codex low, Claude ok: Haiku workers, Sol reviews only.
  const w=widget();
  w.T.evaluateRoutingAlerts([codex({fiveHour:50}),claudeP({fiveHour:30})]);
  w.T.evaluateRoutingAlerts([codex({fiveHour:61}),claudeP({fiveHour:30})]);
  assert.match(w.alert(),codexMsg(61));assert.equal(w.T.getRoutingAlert().provider,"Codex");
  assert.deepEqual(Array.from(w.T.getRoutingAlert().providers),["Codex"]);
}
{
  // Codex low while Claude is nearly out (96% used): the Haiku advice must not appear. Both are low, so hold big jobs.
  const w=widget();
  const sample=(c,l)=>w.T.evaluateRoutingAlerts([codex({fiveHour:c,fiveReset:fixedNow/1000+15120}),claudeP({fiveHour:l,fiveReset:fixedNow/1000+3420})]);
  sample(50,30);sample(61,96);
  assert.equal(w.alert(),"Both low — hold big jobs · Claude frees up in 57m, Codex in 4h 12m","names whichever resets first, then the other");
  assert.doesNotMatch(w.alert(),/Haiku|Terra/,"neither lane is suggested when both are low");
  assert.deepEqual(Array.from(w.T.getRoutingAlert().providers),["Codex","Claude"]);
}
{
  // Claude out of quota but Codex healthy-ish: the Claude rule (Terra builders) applies only with Codex >= 50% left.
  const w=widget();
  const sample=(c,l)=>w.T.evaluateRoutingAlerts([codex({fiveHour:c}),claudeP({fiveHour:l})]);
  sample(55,30);sample(55,96);
  assert.equal(w.alert(),null,"Codex at 45% left is not plenty, so no Terra advice");
}
{
  // Weigh the window that matters: a 5-hour dip that resets in minutes is ignored; the weekly window still counts.
  const w=widget();
  const sample=(five,weekly)=>w.T.evaluateRoutingAlerts([codex({fiveHour:five,weekly,fiveReset:fixedNow/1000+600}),claude]);
  sample(30,30);sample(90,30);
  assert.equal(w.alert(),null,"5-hour at 10% left but resetting in 10 minutes does not alert");
  sample(90,70);
  assert.match(w.alert(),used("Codex","weekly",70),"the weekly window that matters is the one named");
}
{
  // A 5-hour window with a distant reset still counts.
  const w=widget();
  const sample=five=>w.T.evaluateRoutingAlerts([codex({fiveHour:five,fiveReset:fixedNow/1000+2*3600}),claude]);
  sample(30);sample(90);
  assert.match(w.alert(),/^Codex 5-hour 90% used · resets 2h 00m — Haiku/);
}
// Framing: the alert speaks in "used" like the bars and never in "left".
{
  const w=widget();
  w.T.evaluateRoutingAlerts([codex({fiveHour:50}),claude]);w.T.evaluateRoutingAlerts([codex({fiveHour:61}),claude]);
  assert.match(w.alert(),/61% used/);assert.doesNotMatch(w.alert(),/left/);
  assert.match(w.alert(),/resets \d/,"the reset is named");
}
// Placement: the alert lives in the toolbar band, never in the chat layout, so it cannot cover a row.
{
  const w=widget();
  w.T.evaluateRoutingAlerts([codex({fiveHour:50}),claude]);w.T.evaluateRoutingAlerts([codex({fiveHour:61}),claude]);
  assert.equal(w.nodes.get("#routing-alert").hidden,false);
  assert.match(w.nodes.get("#routing-alert").innerHTML,/routing-dot codex/);assert.match(w.nodes.get("#routing-alert").innerHTML,/data-action="routing-dismiss"/);
  assert.equal(w.nodes.get(".compact-toolbar").classes["has-routing-alert"],true,"the toolbar swaps its icon row for the alert");
  assert.equal(w.nodes.has(".layout")?w.nodes.get(".layout").innerHTML:"","","the chat layout is untouched");
  w.T.dismissRoutingAlert();
  assert.equal(w.nodes.get("#routing-alert").hidden,true);
  assert.equal(w.nodes.get(".compact-toolbar").classes["has-routing-alert"],false,"dismiss restores the icons");
}
{
  const page=fs.readFileSync(new URL("../token_tracker/static/runway.html",import.meta.url),"utf8");
  const css=fs.readFileSync(new URL("../token_tracker/static/runway_refined.css",import.meta.url),"utf8");
  const toolbar=page.match(/<header class="titlebar compact-toolbar[\s\S]*?<\/header>/)[0];
  assert.match(toolbar,/<time id="toolbar-clock"[^>]*><\/time>\s*<div id="routing-alert"/,"the alert sits in the toolbar right after the clock");
  const rules=css.match(/\.routing-alert[^{]*\{[^}]*\}/g).join("\n");
  assert.doesNotMatch(rules,/position:sticky|border:1px|box-shadow:inset|linear-gradient|margin:0 6px -34px/,"no overlay, outline, gradient or card");
  assert.match(css,/\.compact-toolbar\.has-routing-alert>\*:not\(#toolbar-clock\):not\(#routing-alert\)\{display:none!important;\}/);
}

console.log("Token Tracker routing alert tests passed");
