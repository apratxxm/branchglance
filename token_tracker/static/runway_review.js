const sizes={default:{width:680,height:560},compact:{width:300,height:560},wide:{width:900,height:560}};
const board=document.querySelector("#board"),scenario=document.querySelector("#scenario"),size=document.querySelector("#size"),paired=document.querySelector("#paired"),notice=document.querySelector("#notice");
const params=new URLSearchParams(location.search);
scenario.value=[...scenario.options].some(option=>option.value===params.get("scenario"))?params.get("scenario"):"normal";
size.value=sizes[params.get("size")]?params.get("size"):"default";
let showPair=params.get("paired")!=="0";
paired.setAttribute("aria-pressed",String(showPair));
function frame(title,wire){
  const {width,height}=sizes[size.value],card=document.createElement("section");
  card.className="frame-card";card.style.width=(width+22)+"px";
  const heading=document.createElement("div");heading.className="frame-head";
  heading.innerHTML="<span>"+title+"</span><small>"+width+" × "+height+" cap</small>";
  const iframe=document.createElement("iframe");iframe.title=title;iframe.width=width;iframe.height=height;
  const query=new URLSearchParams({review:"1",scenario:scenario.value,size:size.value,wire:wire?"1":"0"});
  iframe.src="runway.html?"+query.toString();
  iframe.addEventListener("load",()=>{
    try{
      const doc=iframe.contentDocument,win=iframe.contentWindow,T=win.__tokenTrackerTest;
      if(!doc||!T){notice.textContent="Preview fixture hook unavailable; reload after the preview is ready.";return;}
      const sizeLabel=heading.querySelector("small"),keepOpen={value:false,revision:0};
      const motionCapture={current:null};
      const tools=document.createElement("div");tools.className="frame-tools";
      const replay=document.createElement("button");replay.type="button";replay.textContent="Replay expansion";replay.setAttribute("aria-label","Replay expansion motion model");
      const metrics=document.createElement("output");metrics.setAttribute("aria-live","polite");metrics.setAttribute("aria-label","Motion replay metrics");
      tools.append(replay,metrics);
      replay.addEventListener("click",()=>{void replayMotion({replay,metrics,iframe,card,T,win,width,height,sizeLabel,scenario:scenario.value,motionCapture});});
      win.pywebview={api:{
        set_notch:(expanded,measuredHeight,reducedMotion,mode)=>{
          if(motionCapture.current){
            const capture=motionCapture.current,requested=Math.min(420,Math.max(114,Number(measuredHeight)||114));
            capture.records.push({height:requested,time:capture.modelMs});
            const settled=Promise.resolve(true);capture.pendingRequests.push(settled);return settled;
          }
          const compactMode=mode|| (expanded?"inspect":"idle"),compactWidth=compactMode==="inspect"?560:420;
          const fittedWidth=Math.min(width,compactWidth);
          const requestedHeight=compactMode==="idle"?100:compactMode==="rest"?100:compactMode==="controls"?110:Math.min(420,Math.max(114,Number(measuredHeight)||114));
          const fittedHeight=Math.min(height,requestedHeight);
          if(Number(iframe.width)!==fittedWidth)iframe.width=String(fittedWidth);
          if(card.style.width!==(fittedWidth+22)+"px")card.style.width=(fittedWidth+22)+"px";
          if(Number(iframe.height)!==fittedHeight)iframe.height=String(fittedHeight);
          const capped=fittedWidth<compactWidth||compactMode==="inspect"&&(Number(measuredHeight)>height||Number(measuredHeight)>420);
          const label=fittedWidth+" × "+fittedHeight+(capped?" cap":"")+" · "+compactMode;
          if(sizeLabel&&sizeLabel.textContent!==label)sizeLabel.textContent=label;
          return Promise.resolve(true);
        },
        get_keep_open:()=>Promise.resolve({keepOpen:keepOpen.value,keepOpenRevision:keepOpen.revision}),
        set_keep_open:(value,expectedRevision)=>{
          if(typeof value!=="boolean"||expectedRevision!==keepOpen.revision)return Promise.reject(new Error("stale review setting"));
          keepOpen.value=value;keepOpen.revision+=1;
          return Promise.resolve({keepOpen:keepOpen.value,keepOpenRevision:keepOpen.revision});
        }
      }};
      installReviewInteractions(doc,win,T);
      const payload=fixtures(scenario.value);
      T.state.windowDays=7;
      T.state.open.clear();T.state.agentOpen?.clear();T.state.stripOpen=scenario.value==="question";
      T.state.watcher=payload.watcher;
      T.render(payload.snapshot);
      T.setupNotch();win.addEventListener("resize",()=>T.setNotch(T.notch.expanded,true),{passive:true});T.setNotch(false);
      notice.textContent="Illustrative data · no actions sent";
      card.insertBefore(tools,iframe);
    }catch(error){notice.textContent="Fixture load failed: "+String(error.message||error);}
  });
  card.append(heading,iframe);return card;
}
async function replayMotion({replay,metrics,iframe,card,T,win,width,height,sizeLabel,scenario,motionCapture}){
  if(!win.__TOKEN_TRACKER_TEST__||!T?.notchHeight||!win.pywebview?.api)return;
  if(scenario!=="single-agent"){metrics.textContent="Choose the single-agent fixture before replaying";return;}
  if(motionCapture.current){metrics.textContent="Replay already running";return;}
  const records=[],pendingRequests=[],stepsLog=[],priorCapture=motionCapture.current,priorNotch={...T.notch};
  const bounds={width:iframe.width,height:iframe.height,cardWidth:card.style.width,sizeText:sizeLabel?.textContent||""};
  const steps=[420,448,476,504,532,560,532,504,476,448,420];
  let modelMs=0;
  replay.disabled=true;metrics.textContent="Replay running…";
  motionCapture.current={records,pendingRequests,modelMs};
  try{
    T.notch.manual=true;T.notch.inspecting=true;T.notch.autoExpanded=false;T.notch.autoClosePending=false;
    T.setNotch(true,true);
    await new Promise(resolve=>win.requestAnimationFrame?win.requestAnimationFrame(()=>resolve()):resolve());
    await Promise.all(pendingRequests.splice(0));
    records.length=0;
    const initialLayout=win.document.querySelector(".layout");
    if(!T.notch.expanded||!initialLayout?.clientWidth)throw new Error("Inspect fixture did not become visible; no motion metrics recorded");
    const childFrame=()=>new Promise(resolve=>typeof win.requestAnimationFrame==="function"?win.requestAnimationFrame(()=>resolve()):resolve());
    const parentFrame=()=>new Promise(resolve=>typeof requestAnimationFrame==="function"?requestAnimationFrame(()=>resolve()):resolve());
    for(let index=0;index<steps.length;index++){
      const target=steps[index];
      modelMs=index*20;
      motionCapture.current.modelMs=modelMs;
      const expectedWidth=Math.min(width,target);
      iframe.width=String(expectedWidth);
      card.style.width=(expectedWidth+22)+"px";
      await parentFrame();
      await childFrame();
      await childFrame();
      if(win.innerWidth!==expectedWidth)throw new Error(`Viewport stayed ${win.innerWidth}px; expected ${expectedWidth}px`);
      const recordStart=records.length;
      T.setNotch(true,true);
      await Promise.all(pendingRequests.splice(0));
      const layout=win.document.querySelector(".layout"),titles=[...win.document.querySelectorAll(".r-title,.runway-agent-name")];
      if(!T.notch.expanded||!layout?.clientWidth||!titles.length)throw new Error("Inspect fixture content was hidden or unmeasurable");
      const lineCounts=titles.map(node=>Math.max(1,Math.round(node.getBoundingClientRect().height/(parseFloat(win.getComputedStyle(node).lineHeight)||16))));
      stepsLog.push({viewport:win.innerWidth,contentWidth:Math.round(layout.clientWidth),lines:lineCounts,heightTargets:[Math.min(420,Math.max(114,Number(T.notchHeight())||114))],bridgeTargets:records.slice(recordStart).map(record=>record.height)});
    }
    const distinctWidths=new Set(stepsLog.map(step=>step.viewport));
    if(distinctWidths.size<5||!distinctWidths.has(560))throw new Error("Viewport did not traverse the expansion range");
    const contentWidths=stepsLog.map(step=>step.contentWidth),allLineCounts=stepsLog.flatMap(step=>step.lines),targetHeights=stepsLog.flatMap(step=>step.heightTargets),distinct=new Set(targetHeights).size;
    metrics.dataset.steps=JSON.stringify(stepsLog);
    metrics.title=stepsLog.map(step=>`${step.viewport}:${step.contentWidth}px / ${step.lines.join("+")} lines / height ${step.heightTargets.join(",")}px / bridge ${step.bridgeTargets.join(",")||"dedup"}`).join(" | ");
    metrics.textContent=`height targets ${distinct} · bridge ${records.length} requests · viewport ${Math.min(...distinctWidths)}–${Math.max(...distinctWidths)}px · content ${Math.min(...contentWidths)}–${Math.max(...contentWidths)}px · title lines ${Math.min(...allLineCounts)}–${Math.max(...allLineCounts)} · height ${Math.min(...targetHeights)}–${Math.max(...targetHeights)}px · model ${modelMs}ms (browser-only; no native timing)`;
  }catch(error){metrics.textContent="Replay failed: "+String(error?.message||error).slice(0,100);}
  finally{
    motionCapture.current=priorCapture;
    try{T.setNotch(priorNotch.expanded,true);}catch{}
    Object.assign(T.notch,priorNotch);
    iframe.width=String(bounds.width);iframe.height=String(bounds.height);card.style.width=bounds.cardWidth;
    if(sizeLabel)sizeLabel.textContent=bounds.sizeText;
    replay.disabled=false;
  }
}
function renderBoard(){
  board.replaceChildren();
  board.append(frame(showPair?"Color preview":"Preview",false));
  if(showPair)board.append(frame("Wire review",true));
}
scenario.addEventListener("change",renderBoard);
size.addEventListener("change",renderBoard);
paired.addEventListener("click",()=>{showPair=!showPair;paired.setAttribute("aria-pressed",String(showPair));renderBoard();});
function installReviewInteractions(doc,win,T){
  if(doc.__runwayReviewInstalled)return;
  doc.__runwayReviewInstalled=true;
  doc.addEventListener("click",event=>{
    const button=event.target.closest?.("[data-action]");
    if(!button)return;
    const action=button.dataset.action;
    if(["answer","queue-answer","copy","open","refresh","watcher","runway-question","reset-settings"].includes(action)){
      event.preventDefault();event.stopImmediatePropagation();
      notice.textContent=action==="queue-answer"?"Review mock · queued action blocked · pending question unchanged":"Review mock · "+action+" action blocked";
      button.classList.add("review-blocked");
      return;
    }
    T.handleClick(event);
  },true);
  doc.addEventListener("keydown",event=>T.handleKey(event),true);
  doc.querySelector("#chat-search")?.addEventListener("input",event=>{
    T.state.query=event.target.value;
    if(T.state.snapshot)T.render(T.state.snapshot);
  });
  doc.querySelector("#chat-list")?.addEventListener("pointerleave",()=>{
    T.state.pointerInList=false;if(T.state.snapshot)T.renderChats(T.state.snapshot.providers.filter(p=>p.name==="Codex"));
  });
}
function fixtures(which){
  const now=Date.now(),iso=minutes=>new Date(now-minutes*60000).toISOString(),seconds=minutes=>Math.floor((now+minutes*60000)/1000);
  const fixtureFiveHourReset=4*60+18+2/60,fixtureWeeklyReset=6*24*60+23*60;
  const observed=chat=>({...chat,contextObservedAt:chat.usageUpdatedAt,contextModel:chat.model});
  const root=(id,title,minutes,extra={})=>observed({id,title,model:"gpt-6.1-sol",status:"running",contextPercent:62,contextTokens:160000,contextWindow:258400,tokens:1_140_000,turns:12,toolCalls:42,filesEdited:6,runStartedAt:iso(64),activeRunStartedAt:iso(12),stateSince:iso(12),updatedAt:iso(minutes),usageUpdatedAt:iso(minutes),lastRequest:{input:1_000_000,cached:910_000},activity:{kind:"coordinating"},workspace:"A:\\pisha\\memoir",dailyTokens:{[new Date(now).toLocaleDateString("en-CA")]:142000},...extra});
  const child=(id,parentId,agentName,title,minutes,extra={})=>observed({id,parentId,agentName,title,assignment:title,model:"gpt-6.1-luna",status:"running",contextPercent:34,contextTokens:87000,contextWindow:258400,tokens:180000,turns:4,toolCalls:11,filesEdited:2,runStartedAt:iso(21),updatedAt:iso(minutes),usageUpdatedAt:iso(minutes),lastRequest:{input:100_000,cached:84_000},activity:{kind:"reading",files:["A:\\pisha\\memoir\\token_tracker\\static\\app.js"]},workspace:"A:\\pisha\\memoir",...extra});
  const chats=[
    root("run-main","Runway preview · review the Codex activity panel",1),
    root("voice","Diagnose missed wake word activations on the laptop",52,{model:"gpt-6.1-sol",status:"idle",contextPercent:48,contextTokens:124000,tokens:284000,runEndedAt:iso(45)}),
    root("schema","Keep provider snapshots isolated from live updates",138,{model:"gpt-6.1-sol",contextPercent:79,contextTokens:204000,tokens:640000}),
    root("docs","Document the narrow widget layout and data provenance",204,{status:"needs input",pendingQuestion:"Should the review use the compact or default window?",pendingOptions:["Compact, 300 × 600","Default, 420 × 880","Both sizes"],contextPercent:66}),
    root("history","Reconcile local token counts with the daily activity chart",294,{status:"idle",contextPercent:39,contextTokens:101000,tokens:910000,runEndedAt:iso(260)})
  ];
  chats.push(
    child("ask-review","run-main","/root/interaction_audit","Check pending question and preview controls",3,{status:"needs input",pendingQuestion:"Should this preview include active child agents?",pendingOptions:["Yes, show the active roster","Keep the roster collapsed","Use a longer choice that wraps onto multiple lines in the compact preview"] ,model:"gpt-6.1-sol"}),
    child("work-api","run-main","/root/api_contract","Review range handling and snapshot fields",5,{model:"gpt-6.1-luna"}),
    child("work-layout","run-main","/root/layout_review","Inspect narrow frame and quota meter layout",8,{model:"gpt-6.1-sol",contextPercent:81}),
    child("done-child","run-main","/root/completed","Finished fixture work",42,{status:"idle",stateDetail:"completed",runEndedAt:iso(40),updatedAt:iso(40)}),
    child("stopped-child","run-main","/root/stopped","Stopped historical fixture",120,{status:"failed",stateDetail:"interrupted",updatedAt:iso(120)}),
    child("empty-review","run-main","/root/auto-review","Subagent",122,{status:"idle",stateDetail:"completed",agentRole:"auto-review",model:"Codex",tokens:null,toolCalls:null,filesEdited:null,updatedAt:iso(122)})
  );
  if(which==="idle")chats.length=0;
  if(which==="question"){
    chats.find(chat=>chat.id==="run-main").status="running";
    chats.find(chat=>chat.id==="ask-review").pendingQuestion="Did the 30-day selection load the correct activity window?";
    chats.find(chat=>chat.id==="ask-review").pendingOptions=["Yes, the selected range is correct","No, check the server range mapping"];
  }
  if(which==="agents"){
    const baselineActive=new Set(["ask-review","work-api","work-layout"]);
    for(let i=chats.length-1;i>=0;i--)if(baselineActive.has(chats[i].id))chats.splice(i,1);
    const tasks=["Check live updates", "Review compact layout", "Verify question actions", "Inspect agent lineage"];
    const updates=["Checking that fresh activity preserves the focused control.", "Measuring text contrast and spacing at the narrow width.", "Verifying that closing a banner preserves its pending question.", "Checking each child stays grouped under its owning chat."];
    for(let i=0;i<12;i++)chats.push(child("active-"+i,"run-main","/root/"+tasks[i%4].toLowerCase().replaceAll(" ","_"),tasks[i%4],i+1,{
      model:i%2?"gpt-6-luna":"gpt-6.1-sol",assignment:tasks[i%4],
      status:i===2?"compacting":i===3?"quiet":"running",contextPercent:i===2?93:i===1?79:34,
      activeRunStartedAt:iso(i+2),latestUpdate:updates[i%4],latestUpdateAt:iso(i===3?4:1)
    }));
  }
  if(which==="single-agent"||which==="agents-23"){
    for(let i=chats.length-1;i>=0;i--)if(chats[i].id!=="run-main")chats.splice(i,1);
    if(which==="agents-23")chats[0].title="Review live activity, verify context after compaction, and confirm every active agent remains visible";
    const tasks=["Inspect live updates","Review compact layout","Check question handling","Trace agent ownership","Measure context use"];
    const count=which==="single-agent"?1:23;
    for(let i=0;i<count;i++){
      const state=i%5,awaitingParentMessage=which==="single-agent"||i%3===0;
      const longTitle=which==="agents-23"&&i===0?"VerifyContextSynchronizationAcrossCompaction":null;
      const task=longTitle||tasks[i%tasks.length],agentName=longTitle||"/root/"+task.toLowerCase().replaceAll(" ","_");
      const latestRequestAt=awaitingParentMessage?iso((1+i%8)/60):iso(5);
      const latestUpdateAt=awaitingParentMessage?iso(2):iso(1);
      const status=state===1?"compacting":state===2?"needs input":"running";
      chats.push(child("sim-agent-"+(i+1),"run-main",agentName,task,i+1,{
        ...(which==="agents-23"&&i===1?{workspace:"A:\\a-very-long-middle-directory-for-layout-verification\\memoir"}:{}),
        ...(which==="agents-23"&&i===2?{workspace:"A:\\short\\AnExceptionallyLongWorkspaceFolderThatCannotFitInOneColumn"}:{}),
        ...(which==="agents-23"&&i===3?{workspace:null}:{}),
        model:i%2?"gpt-6-luna":"gpt-6.1-sol",assignment:task,status,
        pendingQuestion:status==="needs input"?"Which implementation should I verify next?":null,
        pendingOptions:status==="needs input"?["Check the live update path","Review context calculations"]:[],
        activeRunStartedAt:iso(i+2),latestRequestAt,latestUpdateAt,
        ...(which==="single-agent"?{latestMessageAt:iso(0),latestMessageDirection:"parent-to-agent",latestMessageSourceId:"run-main",latestMessageTargetId:"sim-agent-"+(i+1)}:{}),
        latestUpdate:awaitingParentMessage?"Working on the parent request":"Replied to the parent; continuing assigned work",
        contextPercent:[24,51,68,76,91][i%5],contextTokens:Math.round(258400*[24,51,68,76,91][i%5]/100),
        toolCalls:4+i,turns:2+(i%7)
      }));
    }
  }
  if(which==="single-agent"){
    const recipient=chats.find(chat=>chat.parentId);
    if(recipient)Object.assign(recipient,{latestMessageAt:new Date(now).toISOString(),latestMessageDirection:"parent-to-agent",latestMessageSourceId:"run-main",latestMessageTargetId:recipient.id});
  }
  if(which==="compacting"){
    chats.find(chat=>chat.id==="run-main").status="compacting";
    chats.find(chat=>chat.id==="work-api").status="compacting";
  }
  const limits=which==="idle"?{
    fiveHour:{percent:0,resetAt:seconds(4*60+59+59/60)},weekly:{percent:54,resetAt:seconds(fixtureWeeklyReset)},limitsUpdatedAt:new Date(now-4*60000).toISOString()
  }:which==="missing"?{
    fiveHour:{percent:42,resetAt:seconds(fixtureFiveHourReset)},weekly:{percent:null,resetAt:null},limitsUpdatedAt:new Date(now-72*60000).toISOString()
  }:{
    fiveHour:{percent:92,resetAt:seconds(fixtureFiveHourReset)},weekly:{percent:54,resetAt:seconds(fixtureWeeklyReset)},limitsUpdatedAt:new Date(now-4*60000).toISOString()
  };
  if(which==="idle"){
    limits.fiveHour={percent:0,resetAt:seconds(299+59/60)};
    limits.weekly.percent=0;
  }
  if(which==="missing"){
    const schema=chats.find(chat=>chat.id==="schema");
    schema.contextPercent=null;schema.contextObservedAt=iso(240);schema.contextModel="gpt-older-model";
    schema.lastRequest={input:null,cached:null};schema.activity=null;schema.activityUnavailable=true;
    schema.updatedAt=null;
    chats.find(chat=>chat.id==="history").status="unknown";
    chats.find(chat=>chat.id==="history").statusSource="unavailable";
  }
  return {snapshot:{generatedAt:new Date(now).toISOString(),enabledProviders:["Codex"],providers:[{name:"Codex",connected:true,activity:[],chats,cacheWarmth:{warmSeconds:1800,coldSeconds:5400,samples:94,learned:true},...limits}]},watcher:{enabled:true,running:true,providers:{Codex:{phase:"keepalive_scheduled",nextGreetingAt:seconds(30)}}}};
}
renderBoard();
