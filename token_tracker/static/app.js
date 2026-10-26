const $ = (selector, root = document) => root.querySelector(selector);
const PHONE = Boolean(window.__TOKEN_TRACKER_PHONE__);
const RUNWAY = Boolean(window.__TOKEN_TRACKER_RUNWAY__||PHONE);
const REFINED = RUNWAY&&!PHONE&&window.__TOKEN_TRACKER_REFINED__===true;
const COMPACT = REFINED&&window.__TOKEN_TRACKER_COMPACT__===true;
const RUNWAY_REFRESH_ICON='<svg class="ic" viewBox="0 0 12 12" aria-hidden="true"><path d="M10 4.8A4.5 4.5 0 1 0 10 7"/><path d="M7.7 2.2H10v2.3"/></svg>';
const compactClockFormatter=COMPACT?new Intl.DateTimeFormat('en-US',{hour:'numeric',minute:'2-digit',hourCycle:'h12'}):null;
const compact = new Intl.NumberFormat("en", { notation: "compact", maximumSignificantDigits: 3 });
function loadPins() { try { const current=JSON.parse(localStorage.getItem("tokenTrackerPinsV2") || "null"); if(Array.isArray(current))return new Set(current.filter(k=>typeof k==="string"));const old=JSON.parse(localStorage.getItem("tokenTrackerPins") || "{}");return new Set(Object.entries(old||{}).filter(([,id])=>typeof id==="string").map(([provider,id])=>provider+":"+id)); } catch { return new Set(); } }
function loadDismissed() { try { const list=JSON.parse(localStorage.getItem("tokenTrackerDismissed")||"[]"); return new Set(Array.isArray(list)?list.slice(-200):[]); } catch { return new Set(); } }
function saveDismissed() { while(state.dismissed.size>200)state.dismissed.delete(state.dismissed.values().next().value);try{localStorage.setItem("tokenTrackerDismissed",JSON.stringify([...state.dismissed]));}catch{} }
const WINDOWS=RUNWAY?[1,7,30]:[1,2,5,7];
function loadWindow() { try { const days=Number(localStorage.getItem(RUNWAY?"tokenTrackerRunwayWindow":"tokenTrackerWindow")); return WINDOWS.includes(days)?days:1; } catch { return 1; } }
const SOURCES=["Codex","Claude"],SOURCE_KEY="branchglanceSource",SOURCE_MANUAL_AT_KEY="branchglanceSourceManualAt";
const STARTUP_CALM_MS=30000,STARTUP_STARTED_AT=Date.now();
function loadSource() { try { const value=localStorage.getItem(SOURCE_KEY); return SOURCES.includes(value)?value:"Codex"; } catch { return "Codex"; } }
function loadSourceManualAt() { try {const value=Number(localStorage.getItem(SOURCE_MANUAL_AT_KEY));return Number.isFinite(value)&&value>0&&value<=Date.now()?value:0;}catch{return 0;} }
function loadHiddenChats() {
  try {
    const list=JSON.parse(localStorage.getItem('tokenTrackerRunwayHiddenChats')||'[]');
    return new Set(Array.isArray(list)?list.filter(key=>typeof key==='string'&&/^(?:Codex|Claude):[^:]+(?::[^:]+)*$/.test(key)):[]);
  } catch { return new Set(); }
}
function saveHiddenChats() { try { localStorage.setItem('tokenTrackerRunwayHiddenChats',JSON.stringify([...state.hiddenChats])); } catch {} }
// Live state every 2s; the full snapshot is throttled. limit=50 so a 7d window is not truncated server-side.
const CHAT_LIMIT = 50, LIVE_MS = 2000, STRUCTURE_REFRESH_MS = LIVE_MS,
  FULL_REFRESH_MS = 120000, TODAY_REFRESH_MS = 60000;
const FAIL_WINDOW_MS = 30*60000, ACTIVE_WINDOW_MS = 2*3600000, REST_AFTER_MS = 3600000,
  COOLING_SECONDS = 600, COOLING_MIN_TOKENS = 50000, OFFLINE_AFTER_MS = 10000, FULL_AGENT_ROWS = 6;
const state = { snapshot:null, refreshedAt:null, lastOkAt:null, failSince:null, loading:false, query:"", searchOpen:false,
  windowDays:loadWindow(), open:new Set(), pins:loadPins(), dismissed:loadDismissed(), stripOpen:false, watcher:null,
  snapshotCache:null,snapshotCacheInvalid:false,watcherFetchError:null,watcherAcceptedSequence:new Map(),
  liveBusy:false, structuralRefreshAt:0, lastWatcherFetchAt:0, outsideSeen:new Map(), pendingOutside:new Map(), refreshPending:false, refreshPendingForce:false, pointerInList:false, lastOrder:null, agentLive:new Map(), finishedAt:new Map(), analyticsOpen:false,
  agentOpen:new Set(), agentCollapsed:new Set(), agentDetails:new Set(), agentSearchClosed:new Set(), rosterQuery:"", expandedQuestions:new Set(),
  hiddenChats:RUNWAY?loadHiddenChats():new Set(), showHidden:false, bootstrapRetryTimer:null, bootstrapRetryMs:2000, refreshing:false, initialBootstrapPending:RUNWAY&&!PHONE&&!window.__TOKEN_TRACKER_TEST__, foregroundLivePending:false, modelVersions:new Map(), runwaySnapshotPainted:false, startupCalmUntil:STARTUP_STARTED_AT+STARTUP_CALM_MS, liveAcceptedGeneratedAt:null, liveSampleAccepted:false, liveRecoveryPending:false, seenQuestions:new Set(), providerPolicyGeneration:null,providerPolicy:null,providerPolicyReady:false,
  source:loadSource(), sourceManualAt:loadSourceManualAt(), sourceAttention:new Set(), sourceLive:new Set(), sourceBase:new Map(), sourceSwitching:false, sourceHeightHold:null, sourceRelease:null, notchOpeningTimer:null, policyWarnings:[] };
const watcherLoader={promise:null,pending:false,sequence:0,successSequence:0,errorSequence:0};
const watcherAcceptedQuota=new Map();
const codexQuotaBootstrap={name:'Codex'};
const quotaBootstrap={provider:codexQuotaBootstrap,providers:{Codex:codexQuotaBootstrap,Claude:{name:'Claude'}},metadata:null,fetchError:null,unsupported:false,samplePayload:null,sampleSequence:0};
const quotaLoader={promise:null,sequence:0,retryAfter:0,retryMs:2000,unsupported:false};
const providerPolicyLoader={promise:null};
const RUNWAY_EVIDENCE_MAX_AGE_MS=8000;
function runwayCurrentEvidenceFresh(now=Date.now()) {
  if(!RUNWAY||PHONE)return true;
  const anchor=state.liveSampleAccepted?state.liveAcceptedGeneratedAt:stamp(state.snapshot?.generatedAt);
  return Number.isFinite(anchor)&&anchor>0&&anchor<=now&&now-anchor<=RUNWAY_EVIDENCE_MAX_AGE_MS;
}

/* ---------- formatting ---------- */
function trackerSnippet(text) {
  if(typeof text!=="string")return null;
  const matches=[...text.matchAll(/\[tracker\]([\s\S]*?)\[\/tracker\]/g)];
  if(!matches.length)return null;
  try {
    const value=JSON.parse(matches.at(-1)[1]);
    if(!value||Array.isArray(value)||typeof value!=="object")return null;
    for(const [key,max] of Object.entries({task:48,doing:80,next:80,blocked:100})) {
      if(value[key]===null&&(key==='next'||key==='blocked'))continue;
      if(typeof value[key]!=="string"||!value[key].trim()||value[key].length>max)return null;
    }
    return value;
  }catch{return null;}
}
const notch={active:0,expanded:false,mode:'idle',manual:false,keyboardInspecting:false,inspecting:false,hover:false,hoverSuppressed:false,contentHover:false,timer:null,inspectTimer:null,exitTimer:null,autoTimer:null,autoExpanded:false,autoClosePending:false,sent:null,activeKeys:null,workStates:new Map(),pointerX:null,pointerY:null,globalPointerX:null,globalPointerY:null};
const widgetLock={known:Boolean(window.__TOKEN_TRACKER_TEST__),locked:false,expanded:false,revision:-1,pending:false,dragRegions:[]};
const keepOpenState={known:false,value:false,revision:-1,pending:false,loading:false,error:false};
const compactBridge={busy:false,pending:null,inFlightSignature:null,appliedSignature:null};
const dashboardState={open:false,returnMode:null,restoreMode:null};
let compactInputModality='pointer';
const lockIcon=locked=>locked?'<svg class="ic" viewBox="0 0 12 12" aria-hidden="true"><rect x="2.5" y="5" width="7" height="5.5" rx="1"/><path d="M4 5V3.8a2 2 0 0 1 4 0V5"/></svg>':'<svg class="ic" viewBox="0 0 12 12" aria-hidden="true"><rect x="2.5" y="5" width="7" height="5.5" rx="1"/><path d="M4 5V3.8a2 2 0 0 1 3.6-1.2"/></svg>';
function updateWidgetLockButton() {
  const button=$('#widget-lock');if(!button)return;
  const label=COMPACT?(widgetLock.locked?'Release position':'Hold position'):(widgetLock.locked?'Unlock widget':'Lock widget');
  const title=COMPACT?label+'. Prevents automatic repositioning; you can still drag.':label;
  button.disabled=widgetLock.pending||!widgetLock.known;
  button.setAttribute?.('aria-pressed',String(widgetLock.locked));button.setAttribute?.('aria-label',label);button.title=title;
  if(button.innerHTML!==lockIcon(widgetLock.locked))button.innerHTML=lockIcon(widgetLock.locked);
  updateMonitorButton();
}
function updateKeepOpenButton() {
  const button=$('#widget-keep-open');if(!button)return;
  const label=keepOpenState.loading?'Checking keep-open setting':keepOpenState.pending?'Saving keep-open setting':keepOpenState.error||!keepOpenState.known?'Keep-open setting unavailable':keepOpenState.value?'Close automatically':'Keep open';
  button.disabled=keepOpenState.pending||keepOpenState.loading||!keepOpenState.known;
  button.setAttribute?.('aria-pressed',String(keepOpenState.value));button.setAttribute?.('aria-busy',String(keepOpenState.pending||keepOpenState.loading));
  button.setAttribute?.('aria-label',label);button.title=label;
}
function applyKeepOpen(value) {
  if(!COMPACT||!value||typeof value.keepOpen!=='boolean'||!Number.isSafeInteger(value.keepOpenRevision)||value.keepOpenRevision<0||value.keepOpenRevision<keepOpenState.revision)return false;
  keepOpenState.known=true;keepOpenState.value=value.keepOpen;keepOpenState.revision=value.keepOpenRevision;keepOpenState.error=false;
  updateKeepOpenButton();setNotch(notch.expanded,true);return true;
}
async function syncKeepOpen() {
  if(!COMPACT)return null;
  const api=window.pywebview?.api?.get_keep_open;
  if(typeof api!=='function'){updateKeepOpenButton();return null;}
  keepOpenState.loading=true;updateKeepOpenButton();
  try{const result=await api();applyKeepOpen(result);return result;}
  catch{keepOpenState.error=true;return null;}
  finally{keepOpenState.loading=false;updateKeepOpenButton();}
}
async function setKeepOpen(value) {
  if(!COMPACT||keepOpenState.pending||keepOpenState.loading||!keepOpenState.known||typeof value!=='boolean')return false;
  const api=window.pywebview?.api?.set_keep_open;if(typeof api!=='function')return false;
  const expectedRevision=keepOpenState.revision;keepOpenState.pending=true;keepOpenState.error=false;updateKeepOpenButton();
  try{const result=await api(value,expectedRevision);if(!applyKeepOpen(result))return false;return keepOpenState.value===value;}
  catch{keepOpenState.error=true;void syncKeepOpen();return false;}
  finally{keepOpenState.pending=false;updateKeepOpenButton();}
}
async function toggleKeepOpen(){if(keepOpenState.pending||keepOpenState.loading)return false;return setKeepOpen(!keepOpenState.value);}
function compactHasFocus(){
  if(!COMPACT)return false;
  const widget=$('.widget'),active=document.activeElement;if(!widget?.contains?.(active))return false;
  const tag=String(active?.tagName||'').toUpperCase(),editable=tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT'||Boolean(active?.isContentEditable);
  return editable||Boolean(active?.closest?.('.popover,.ch-search'))||compactInputModality==='keyboard';
}
function compactModeForState(){
  if(!COMPACT)return notch.expanded?'inspect':'idle';
  if(dashboardState.open)return 'dashboard';
  if(dashboardState.restoreMode)return dashboardState.restoreMode;
  const focus=compactHasFocus(),explicit=notch.manual||notch.keyboardInspecting||state.searchOpen||Boolean(state.popover);
  // With no active chats there is nothing to show: keep-open, hover and focus stay on the toolbar.
  if(explicit||notch.active>0&&(keepOpenState.value||notch.autoExpanded||notch.inspecting||focus))return 'inspect';
  return 'controls';
}
function blurPointerToolbarFocus(mode){
  if(!COMPACT||mode==='inspect'||mode==='controls'||compactInputModality!=='pointer')return;
  const active=document.activeElement,tag=String(active?.tagName||'').toUpperCase();
  if(active?.closest?.('.compact-toolbar')&&!['INPUT','TEXTAREA','SELECT'].includes(tag)&&!active?.isContentEditable&&!active?.closest?.('.popover,.ch-search'))active.blur?.();
}
function sendCompactNotch(api,args,signature){
  if(compactBridge.busy&&compactBridge.inFlightSignature===signature){notch.sent=signature;compactBridge.pending=null;return;}
  if(compactBridge.pending?.signature===signature){notch.sent=signature;return;}
  if(!compactBridge.busy&&compactBridge.appliedSignature===signature){notch.sent=signature;armInspectLayoutReconcile(signature);return;}
  notch.sent=signature;compactBridge.pending={api,args,signature};if(compactBridge.busy)return;
  compactBridge.busy=true;
  const drain=async()=>{
    while(compactBridge.pending){
      const request=compactBridge.pending;compactBridge.pending=null;
      if(notch.sent!==request.signature)continue;
      compactBridge.inFlightSignature=request.signature;
      try{await request.api(...request.args);if(notch.sent===request.signature)compactBridge.appliedSignature=request.signature;else compactBridge.appliedSignature=null;}
      catch{if(notch.sent===request.signature){notch.sent=null;if(compactBridge.appliedSignature===request.signature)compactBridge.appliedSignature=null;if(inspectExpectedSignature===request.signature){clearTimeout(inspectLayoutTimer);inspectLayoutTimer=null;}}}
      finally{compactBridge.inFlightSignature=null;}
    }
    compactBridge.busy=false;if(compactBridge.pending)void drain();else armInspectLayoutReconcile(compactBridge.appliedSignature);
  };
  void drain();
}
function syncWidgetDragRegions(locked) {
  if(COMPACT)return;
  if(locked){
    if(!widgetLock.dragRegions.length)widgetLock.dragRegions=Array.from(document.querySelectorAll?.('.pywebview-drag-region')||[]).filter(node=>node.classList?.contains?.('pywebview-drag-region'));
    for(const node of widgetLock.dragRegions)node.classList?.remove?.('pywebview-drag-region');
  }else{
    for(const node of widgetLock.dragRegions)node.classList?.add?.('pywebview-drag-region');
    widgetLock.dragRegions=[];
  }
}
function applyWidgetLock(value) {
  if(!RUNWAY||PHONE||!value||typeof value.locked!=='boolean'||typeof value.expanded!=='boolean'||!Number.isSafeInteger(value.revision)||value.revision<0||value.revision<widgetLock.revision)return false;
  widgetLock.known=true;widgetLock.revision=value.revision;widgetLock.locked=value.locked;widgetLock.expanded=value.expanded;
  if(COMPACT&&typeof value.keepOpen==='boolean'&&Number.isSafeInteger(value.keepOpenRevision))applyKeepOpen({keepOpen:value.keepOpen,keepOpenRevision:value.keepOpenRevision});
  syncWidgetDragRegions(widgetLock.locked);updateWidgetLockButton();
  if(widgetLock.locked&&!COMPACT){
    clearTimeout(notch.inspectTimer);clearTimeout(notch.exitTimer);clearTimeout(notch.autoTimer);clearTimeout(notch.timer);
    notch.inspectTimer=notch.exitTimer=notch.autoTimer=notch.timer=null;
    notch.manual=false;notch.keyboardInspecting=false;notch.inspecting=false;notch.autoExpanded=false;notch.autoClosePending=false;
  }
  if(!COMPACT&&!state.runwaySnapshotPainted&&widgetLock.locked&&widgetLock.expanded){
    notch.expanded=false;setNotch(false);return true;
  }
  setNotch(COMPACT?notch.expanded:widgetLock.expanded,true);return true;
}
async function syncWidgetLock() {
  const initial=!widgetLock.known;
  try{
    const result=await window.pywebview?.api?.get_widget_lock?.();if(!applyWidgetLock(result))return null;
    if(initial&&!widgetLock.locked&&notch.active>0&&state.forest){notch.activeKeys=null;syncNotch(notch.active,[...state.forest.rows].filter(runwayVisibleActive).map(chat=>({key:chat.key,provider:chat.provider,active:true,runStartedAt:chat.activeRunStartedAt,latestMessageAt:chat.latestMessageAt,latestMessageDirection:chat.latestMessageDirection,subagent:hasTreeParent(chat),waiting:runwayWaiting(chat),questionToken:runwayWaiting(chat)?questionDismissToken(chat):null,activityAt:stamp(chat.activityAt)||Math.max(stamp(chat.updatedAt),stamp(chat.latestMessageAt),stamp(chat.statusObservedAt),stamp(chat.executionObservedAt),stamp(chat.activity?.observedAt),stamp(chat.executionActivity?.observedAt))})));}
    return result;
  }catch{return null;}
}
async function setWidgetLock(locked) {
  const button=$('#widget-lock'),api=window.pywebview?.api?.set_widget_lock;
  if(widgetLock.pending||!widgetLock.known||typeof locked!=='boolean'||typeof api!=='function')return false;
  const expectedRevision=widgetLock.revision;
  widgetLock.pending=true;updateWidgetLockButton();
  try{
    const result=COMPACT?await api(locked,notch.expanded,null,expectedRevision):await api(locked,notch.expanded,notch.expanded?notchHeight():null,expectedRevision);
    if(!applyWidgetLock(result))return false;
    return widgetLock.locked===locked;
  }catch{return false;}
  finally{widgetLock.pending=false;updateWidgetLockButton();}
}
async function toggleWidgetLock(){if(widgetLock.pending)return false;return setWidgetLock(!widgetLock.locked);}
async function recenterWidget(){
  const api=window.pywebview?.api;
  if(!api?.recenter)return false;
  try{
    const result=await api.recenter();
    if(!applyWidgetLock(result)&&typeof api.get_widget_lock==='function')await syncWidgetLock();
    return true;
  }catch{return false;}
}
function openDashboard(){
  if(!COMPACT||PHONE||dashboardState.open)return false;
  dashboardState.returnMode=notch.mode||'controls';dashboardState.restoreMode=null;dashboardState.open=true;
  window.TokenTrackerDashboard?.open?.({source:effectiveSource(state.snapshot)});
  setNotch(notch.expanded);return true;
}
function closeDashboard(){
  if(!dashboardState.open)return false;
  const previous=dashboardState.returnMode;dashboardState.open=false;dashboardState.returnMode=null;
  dashboardState.restoreMode=['idle','rest','controls','inspect'].includes(previous)?previous:'controls';
  window.TokenTrackerDashboard?.close?.();
  const view=$('#dashboard-view');if(view){view.hidden=true;view.inert=true;view.setAttribute?.('aria-hidden','true');}
  setNotch(notch.expanded);return true;
}
window.tokenTrackerCloseDashboard=closeDashboard;
window.tokenTrackerOpenDashboard=()=>openDashboard();
window.tokenTrackerRecenter=()=>recenterWidget();
window.tokenTrackerSetWidgetLock=value=>applyWidgetLock(value);
function notchHeight() {
  if(COMPACT){
    if(notch.mode==='dashboard')return 540;
    if(notch.mode==='inspect'&&state.sourceHeightHold)return state.sourceHeightHold;
    if(notch.mode==='idle')return 100;
    if(notch.mode==='rest')return 100;
    if(notch.mode==='controls')return 110;
  }
  const list=$('#chat-list');
  if(!notch.expanded||!list?.querySelectorAll)return REFINED?148:96;
  const visibleHeight=selector=>{const node=$(selector);if(!node||node.hidden||node.getAttribute?.('aria-hidden')==='true')return 0;const rects=node.getClientRects?.();if(rects&&rects.length===0)return 0;return node.offsetHeight||0;};
  const fixed=['.titlebar','.resets','#weekly-resets','.totals','#strip'].reduce((sum,selector)=>sum+visibleHeight(selector),0)+
    (state.searchOpen?visibleHeight('#chats-head'):0);
  // The list's own content height includes variable wrapped rows and the empty state,
  // while avoiding the flex-filled height of its parent layout.
  const content=list.scrollHeight||[...list.querySelectorAll('.chat')].reduce((sum,row)=>sum+(row.offsetHeight||0),0)||[...list.children||[]].reduce((sum,node)=>sum+(node.offsetHeight||0),0);
  const measured=Math.max(REFINED?148:96,Math.ceil(fixed+content+(REFINED?16:0)));
  return COMPACT?Math.min(420,Math.max(114,measured)):measured;
}
function scheduleNotchSize() {
  if(!RUNWAY||typeof requestAnimationFrame!=='function')return;
  cancelAnimationFrame(notch.frame);notch.frame=requestAnimationFrame(()=>{const changed=!PHONE&&syncRunwayTreeConnectors();if(changed){scheduleNotchSize();return;}setNotch(notch.expanded,true);});
}
function runwayTreePathGeometry(rootRect, entries, refined=false) {
  const byIdentity=new Map(entries.map(entry=>[entry.provider+':'+entry.id,entry])),paths=[],dots=new Map(),messagePositions=[];let bottom=0;
  for(const entry of entries){
    const rowRect=entry.messageRect||entry.rowRect,nameRect=entry.nameRect;
    if(!rowRect||!nameRect||rowRect.width<=0||nameRect.width<=0)continue;
    const x=rootRect.left-rowRect.left+2,y=nameRect.top-rowRect.top+Math.min(nameRect.height||19,19)/2-5;
    if(Number.isFinite(x)&&Number.isFinite(y))messagePositions.push({key:entry.key,x,y});
  }
  for(const child of entries){
    const parentKey=Object.prototype.hasOwnProperty.call(child,'parentKey')?child.parentKey:child.parentId?child.provider+':'+child.parentId:null;
    if(!parentKey)continue;
    const parent=entries.find(entry=>entry.key===parentKey)||byIdentity.get(parentKey);
    const parentRect=parent?.glanceRect||parent?.rowRect;
    if(!parent?.nameRect||!parentRect||!child.nameRect||!child.glanceRect||parentRect.width<=0||child.glanceRect.width<=0)continue;
    const parentDot=refined?parent.dotRect:null,childDot=refined?child.dotRect:null;
    const px=Math.max(0,parentDot?.width?parentDot.left-rootRect.left+parentDot.width/2:parentRect.left-rootRect.left+2),py=parentDot?.height?parentDot.top-rootRect.top+parentDot.height/2:parent.nameRect.top-rootRect.top+Math.min(parent.nameRect.height||19,19)/2;
    const cx=Math.max(0,childDot?.width?childDot.left-rootRect.left+childDot.width/2:child.glanceRect.left-rootRect.left+2),cy=childDot?.height?childDot.top-rootRect.top+childDot.height/2:child.nameRect.top-rootRect.top+Math.min(child.nameRect.height||19,19)/2;
    if(![px,py,cx,cy].every(Number.isFinite)||cy<0)continue;
    if(refined){const direction=Math.sign(cx-px)||1,r=Math.max(0,Math.min(8,Math.abs(cx-px),Math.abs(cy-py)/2));paths.push({d:r?`M${px} ${py}V${cy-r}Q${px} ${cy} ${px+direction*r} ${cy}H${cx}`:`M${px} ${py}V${cy}H${cx}`,from:parentKey,to:child.key||child.id,endY:cy});}
    else paths.push({d:`M${px} ${py}V${cy}H${cx}`,from:parentKey,to:child.key||child.id,endY:cy});
    for(const [x,y] of [[px,py],[cx,cy]])dots.set(x+':'+y,{cx:x,cy:y});
    bottom=Math.max(bottom,cy);
  }
  return {width:Math.max(1,rootRect.width),height:Math.max(1,Math.ceil(bottom+2)),paths,dots:[...dots.values()],messagePositions};
}
let treeScrollFrame=null,treeScrollListenerAttached=false;
function scheduleRunwayTreeScrollSync(){
  if(treeScrollFrame!==null||typeof requestAnimationFrame!=='function')return;
  treeScrollFrame=requestAnimationFrame(()=>{treeScrollFrame=null;syncRunwayTreeConnectors({updateTallRoot:false});});
}
function setupRunwayTreeScrollSync(){
  if(treeScrollListenerAttached||!RUNWAY||PHONE||!COMPACT)return;
  const layout=$('.layout');if(!layout?.addEventListener)return;
  layout.addEventListener('scroll',scheduleRunwayTreeScrollSync,{passive:true});treeScrollListenerAttached=true;
}
function syncRunwayTreeConnectors({updateTallRoot=true}={}) {
  const list=$('#chat-list');if(PHONE||!realDom(list)||typeof document.createElementNS!=='function')return;
  const ns='http://www.w3.org/2000/svg',layoutHeight=updateTallRoot?($('.layout')?.clientHeight||0):0;let tallRootChanged=false;
  for(const root of [...list.children].filter(el=>el.classList?.contains('chat'))){
    let rootRect=root.getBoundingClientRect?.();const row=root.querySelector?.('.row');let rowRect=row?.getBoundingClientRect?.();
    if(!rootRect||!row||!rowRect)continue;
    if(updateTallRoot){
      const tallRoot=COMPACT&&notch.mode==='inspect'&&layoutHeight>0&&rowRect.height>Math.max(50,layoutHeight/3),hasTallClass=Boolean(row.classList?.contains?.('runway-tall-root'));
      if(tallRoot!==hasTallClass){row.classList?.toggle?.('runway-tall-root',tallRoot);tallRootChanged=true;rootRect=root.getBoundingClientRect?.()||rootRect;rowRect=row.getBoundingClientRect?.()||rowRect;}
    }
    row.classList?.toggle?.('runway-root-stuck',COMPACT&&notch.mode==='inspect'&&rowRect.top-rootRect.top>1);
    const visible=[...root.querySelectorAll('.ag.runway-agent[data-sub]')].filter(el=>!el._leaving&&el.getClientRects?.().length!==0);
    const entries=[{key:root.dataset.key,...(()=>{const item=state.forest?.rows.find(candidate=>candidate.key===root.dataset.key),name=row.querySelector?.('.r-title'),anchor=REFINED?row.querySelector?.('.runway-refined-tree-anchor'):null;return {provider:item?.provider,id:item?.id,parentKey:treeParentKey(item),rowRect,messageRect:rowRect,nameRect:name?.getBoundingClientRect?.(),dotRect:anchor?.getBoundingClientRect?.()};})()}];
    for(const el of visible){const item=state.forest?.rows.find(candidate=>candidate.key===el.dataset.sub),glance=el.querySelector?.('.runway-agent-glance'),name=el.querySelector?.('.runway-agent-name'),anchor=REFINED?glance?.querySelector?.('.runway-refined-tree-anchor'):null;if(item)entries.push({key:item.key,provider:item.provider,id:item.id,parentKey:treeParentKey(item),rowRect:el.getBoundingClientRect?.(),messageRect:glance?.getBoundingClientRect?.(),nameRect:name?.getBoundingClientRect?.(),glanceRect:glance?.getBoundingClientRect?.(),dotRect:anchor?.getBoundingClientRect?.()});}
    const geometry=runwayTreePathGeometry(rootRect,entries,REFINED),paths=geometry.paths.map(path=>path.d),dots=geometry.dots;
    if(!REFINED){
      for(const target of [row,...visible.map(el=>el.querySelector?.('.runway-agent-glance'))])target?.querySelector?.('[data-runway-slot="message-lane"]')?.removeAttribute('data-message-positioned');
      for(const position of geometry.messagePositions){
        const entry=entries.find(item=>item.key===position.key),target=position.key===root.dataset.key?row:visible.find(el=>el.dataset.sub===position.key)?.querySelector?.('.runway-agent-glance'),slot=target?.querySelector?.('[data-runway-slot="message-lane"]');
        if(!entry||!target?.style||!slot)continue;
        target.style.setProperty('--message-offset-x',position.x+'px');target.style.setProperty('--message-offset-y',position.y+'px');slot.setAttribute('data-message-positioned','true');
      }
    }
    let svg=[...root.children].find(el=>el.classList?.contains('runway-tree-connectors'));
    if(!paths.length){svg?.remove();continue;}
    const {width,height}=geometry;
    const signature=width+':'+height+':'+paths.join('|')+':'+dots.map(dot=>dot.cx+','+dot.cy).join('|');
    if(!svg){svg=document.createElementNS(ns,'svg');svg.setAttribute('class','runway-tree-connectors');svg.setAttribute('aria-hidden','true');svg.setAttribute('focusable','false');root.appendChild(svg);}
    if(svg.dataset.geometry===signature)continue;
    svg.dataset.geometry=signature;svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.setAttribute('width',String(width));svg.setAttribute('height',String(height));svg.replaceChildren();
    for(const d of paths){const path=document.createElementNS(ns,'path');path.setAttribute('class','runway-tree-link');path.setAttribute('d',d);svg.appendChild(path);}
    for(const dot of dots){const circle=document.createElementNS(ns,'circle');circle.setAttribute('class','runway-tree-dot');circle.setAttribute('cx',String(dot.cx));circle.setAttribute('cy',String(dot.cy));circle.setAttribute('r','1.7');svg.appendChild(circle);}
  }
  return tallRootChanged;
}
function setNotch(expanded, treeSynced=false) {
  if(!RUNWAY||PHONE)return;
  if(COMPACT){
    const mode=compactModeForState();
    if(mode==='dashboard')document.documentElement?.style?.removeProperty?.('--runway-inspect-layout-width');
    if(mode==='inspect'){
      if(notch.mode!=='inspect'){inspectLayoutGeneration++;inspectWidthCorrectionUsed=false;clearTimeout(inspectLayoutTimer);inspectLayoutTimer=null;}
      applyInspectLayoutWidth(monitorState.current);
    }else if(notch.mode==='inspect')cancelInspectLayoutReconcile();
    blurPointerToolbarFocus(mode);
    const wasExpanded=notch.expanded;
    notch.mode=mode;notch.expanded=mode==='inspect';
    if(notch.expanded&&!wasExpanded)beginNotchOpening();else if(!notch.expanded)clearNotchOpening();
    const dashboardView=$('#dashboard-view'),dashboardVisible=mode==='dashboard';
    if(dashboardView){dashboardView.hidden=!dashboardVisible;dashboardView.inert=!dashboardVisible;dashboardView.setAttribute?.('aria-hidden',String(!dashboardVisible));}
    const hidden=!notch.expanded,sections=[$('.chats'),$('.totals'),$('#strip')].filter(Boolean);
    if(hidden&&sections.some(section=>section.contains?.(document.activeElement)))$('#sync-age')?.focus?.({preventScroll:true});
    for(const section of sections){if(section.inert!==hidden)section.inert=hidden;if(section.getAttribute?.('aria-hidden')!==String(hidden))section.setAttribute?.('aria-hidden',String(hidden));}
    for(const name of ['idle','rest','controls','inspect','dashboard'])document.body?.classList?.toggle('compact-'+name,mode===name);
    if(document.body?.dataset)document.body.dataset.notchMode=mode;
    document.body?.classList?.toggle('notch-expanded',notch.expanded);
    document.body?.classList?.toggle('notch-collapsed',!notch.expanded);
    document.body?.classList?.toggle('notch-auto-open',Boolean(notch.autoExpanded&&notch.expanded));
    document.body?.classList?.toggle('notch-inspecting',mode==='inspect');
    if(notch.expanded&&!PHONE&&!treeSynced){if(typeof requestAnimationFrame==='function'){scheduleNotchSize();return;}syncRunwayTreeConnectors();}
    renderRunwayCounts(notch.counts);
    const api=window.pywebview?.api?.set_notch,height=notchHeight(),reduced=reducedMotion(),signature=mode+':'+notch.expanded+':'+height+':'+reduced;
    if(api&&widgetLock.known){if(mode==='inspect')noteInspectRequest(signature);sendCompactNotch(api,[notch.expanded,height,reduced,mode],signature);}
    if(dashboardState.restoreMode===mode&&(mode!=='inspect'||treeSynced))dashboardState.restoreMode=null;
    return;
  }
  const wasExpanded=notch.expanded;
  const restoreLockedOpen=Boolean(widgetLock.known&&widgetLock.locked&&widgetLock.expanded&&!state.runwaySnapshotPainted);
  if(widgetLock.known&&widgetLock.locked)expanded=widgetLock.expanded;
  const canOpen=Boolean(widgetLock.known&&state.runwaySnapshotPainted&&(notch.active>0||(widgetLock.locked&&widgetLock.expanded)));
  if(!canOpen)expanded=false;
  if(!expanded){clearTimeout(notch.autoTimer);notch.autoTimer=null;notch.autoExpanded=false;notch.autoClosePending=false;if(!state.popover){notch.inspecting=false;if(!notch.keyboardInspecting)notch.manual=false;}}
  const closingQuestion=!expanded&&state.popover?.dataset?.action==='runway-question';
  if(closingQuestion)closePopover({restoreFocus:false,updateNotch:false});
  notch.expanded=Boolean(expanded&&canOpen&&(widgetLock.known&&widgetLock.locked?widgetLock.expanded:(notch.active||notch.manual||notch.inspecting||notch.autoExpanded||state.popover)));
  if(notch.expanded&&!wasExpanded)beginNotchOpening();else if(!notch.expanded)clearNotchOpening();
  const hidden=!notch.expanded,sections=[$('.chats'),$('.totals'),$('#strip')].filter(Boolean);
  if(hidden&&(closingQuestion||sections.some(section=>section.contains?.(document.activeElement))))$('#sync-age')?.focus?.({preventScroll:true});
  for(const section of sections){if(section.inert!==hidden)section.inert=hidden;if(section.getAttribute?.('aria-hidden')!==String(hidden))section.setAttribute?.('aria-hidden',String(hidden));}
  document.body?.classList?.toggle('notch-expanded',notch.expanded);
  document.body?.classList?.toggle('notch-collapsed',!notch.expanded);
  document.body?.classList?.toggle('notch-auto-open',Boolean(notch.autoExpanded&&notch.expanded));
  document.body?.classList?.toggle('notch-inspecting',Boolean((notch.inspecting||notch.manual||state.popover)&&notch.expanded));
  if(notch.expanded&&!PHONE&&!treeSynced){if(typeof requestAnimationFrame==='function'){scheduleNotchSize();return;}syncRunwayTreeConnectors();}
  renderRunwayCounts(notch.counts);
  const api=window.pywebview?.api?.set_notch;
  const height=notchHeight(),reduced=reducedMotion(),signature=notch.expanded+':'+height+':'+reduced;
  if(api&&widgetLock.known&&!restoreLockedOpen&&notch.sent!==signature){notch.sent=signature;Promise.resolve(api(notch.expanded,height,reduced)).catch(()=>{notch.sent=null;});}
}
function expandForNewWork() {
  if((!widgetLock.known||widgetLock.locked)&&!COMPACT)return;
  if(COMPACT){
    if(notch.autoTimer!=null){notch.autoExpanded=true;setNotch(true);return;}
    notch.autoExpanded=true;setNotch(true);notch.autoClosePending=false;
    notch.autoTimer=setTimeout(()=>{notch.autoTimer=null;if(state.popover){notch.autoClosePending=true;return;}notch.autoExpanded=false;setNotch(false);},3000);
    return;
  }
  if(notch.autoTimer!=null){notch.autoExpanded=true;setNotch(true);return;}
  setNotch(true);
  notch.autoExpanded=true;setNotch(true);
  notch.autoClosePending=false;
  notch.autoTimer=setTimeout(()=>{
    notch.autoTimer=null;
    // New-work reveals end on schedule unless the user explicitly inspected it.
    if(state.popover){notch.autoClosePending=true;return;}
    notch.autoExpanded=false;
    if(notch.inspecting||notch.manual)setNotch(true);else setNotch(false);
  },3000);
}
function syncCompactNotch(count,items) {
  const fresh=runwayCurrentEvidenceFresh(),current=fresh?new Map(items.map(item=>typeof item==="string"?[item,{key:item,active:true}]:[item.key,item])):new Map();
  const activeKeys=new Set([...current.values()].filter(item=>item.active).map(item=>item.key));
  let newWork=notch.activeKeys===null?activeKeys.size>0:[...activeKeys].some(key=>!notch.activeKeys.has(key));
  const newWorkItems=[];
  if(notch.activeKeys===null){for(const item of current.values())if(item.active)newWorkItems.push(item);}
  else for(const [key,item] of current)if(item.active&&!notch.activeKeys.has(key))newWorkItems.push(item);
  for(const [key,item] of current){
    if(!item.active)continue;
    const previous=notch.workStates.get(key);
    const restarted=Boolean(previous?.active&&previous.runStartedAt&&item.runStartedAt&&previous.runStartedAt!==item.runStartedAt);
    const messageArrived=Boolean(item.subagent&&item.latestMessageDirection&&["parent-to-agent","agent-to-parent","agent-to-agent"].includes(item.latestMessageDirection)&&item.latestMessageAt&&previous&&(!previous.latestMessageAt||item.latestMessageAt>previous.latestMessageAt));
    const questionArrived=Boolean(item.waiting&&(!previous?.waiting||previous.questionToken!==item.questionToken));
    if(!previous||!previous.active||restarted||messageArrived||questionArrived){newWork=true;if(!newWorkItems.some(candidate=>candidate.key===key))newWorkItems.push(item);}
  }
  const nextStates=new Map(current);for(const [key,item] of notch.workStates)if(!current.has(key))nextStates.set(key,{...item,active:false});
  notch.workStates=nextStates;notch.activeKeys=activeKeys;notch.active=fresh?count:0;
  if(!fresh){
    clearTimeout(notch.inspectTimer);clearTimeout(notch.exitTimer);clearTimeout(notch.autoTimer);clearTimeout(notch.timer);
    notch.inspectTimer=notch.exitTimer=notch.autoTimer=notch.timer=null;notch.autoExpanded=false;notch.autoClosePending=false;
    if(!notch.manual&&!notch.keyboardInspecting&&!state.popover&&!(notch.hover&&!notch.hoverSuppressed))notch.inspecting=false;
    if(!notch.hover&&!compactHasFocus())notch.contentHover=false;
    setNotch(false);return;
  }
  if(newWork&&!notch.active)notch.autoExpanded=true;
  if(newWork)selectSourceForNewWork(newWorkItems);
  if(!notch.active){
    clearTimeout(notch.inspectTimer);clearTimeout(notch.exitTimer);clearTimeout(notch.autoTimer);clearTimeout(notch.timer);
    notch.inspectTimer=notch.exitTimer=notch.autoTimer=notch.timer=null;notch.autoExpanded=false;notch.autoClosePending=false;
    if(!notch.manual&&!notch.keyboardInspecting&&!state.popover&&!(notch.hover&&!notch.hoverSuppressed))notch.inspecting=false;
    if(!notch.hover&&!compactHasFocus())notch.contentHover=false;
    setNotch(false);return;
  }
  if(newWork)expandForNewWork();else setNotch(false);
}
function syncNotch(count,items=[]) {
  if(PHONE)return;
  if(COMPACT){syncCompactNotch(count,items);return;}
  const current=new Map(items.map(item=>typeof item==="string"?[item,{key:item,active:true}]:[item.key,item]));
  const activeKeys=new Set([...current.values()].filter(item=>item.active).map(item=>item.key));
  if(!runwayCurrentEvidenceFresh()){
    notch.active=0;
    clearTimeout(notch.inspectTimer);clearTimeout(notch.exitTimer);clearTimeout(notch.autoTimer);clearTimeout(notch.timer);
    notch.inspectTimer=notch.exitTimer=notch.autoTimer=notch.timer=null;
    notch.hover=false;notch.contentHover=false;notch.manual=false;notch.inspecting=false;notch.keyboardInspecting=false;notch.autoExpanded=false;notch.autoClosePending=false;
    if(state.popover?.dataset?.action==='runway-question')closePopover({restoreFocus:false,updateNotch:false});
    setNotch(Boolean(widgetLock.known&&widgetLock.locked&&widgetLock.expanded));return;
  }
  let newWork=notch.activeKeys===null?activeKeys.size>0:[...activeKeys].some(key=>!notch.activeKeys.has(key));
  for(const [key,item] of current){
    if(!item.active)continue;
    const previous=notch.workStates.get(key);
    const restarted=Boolean(previous?.active&&previous.runStartedAt&&item.runStartedAt&&previous.runStartedAt!==item.runStartedAt);
    const messageArrived=Boolean(item.subagent&&item.latestMessageDirection&&["parent-to-agent","agent-to-parent","agent-to-agent"].includes(item.latestMessageDirection)&&item.latestMessageAt&&previous&&(!previous.latestMessageAt||item.latestMessageAt>previous.latestMessageAt));
    const questionArrived=Boolean(item.waiting&&(!previous?.waiting||previous.questionToken!==item.questionToken));
    if(!previous||!previous.active||restarted||messageArrived||questionArrived)newWork=true;
  }
  const nextStates=new Map(current);
  for(const [key,item] of notch.workStates)if(!current.has(key))nextStates.set(key,{...item,active:false});
  notch.workStates=nextStates;
  notch.activeKeys=activeKeys;
  notch.active=count;
  if(widgetLock.known&&widgetLock.locked){
    clearTimeout(notch.inspectTimer);clearTimeout(notch.exitTimer);clearTimeout(notch.autoTimer);clearTimeout(notch.timer);
    notch.inspectTimer=notch.exitTimer=notch.autoTimer=notch.timer=null;
    notch.hover=false;notch.contentHover=false;notch.manual=false;notch.inspecting=false;notch.keyboardInspecting=false;notch.autoExpanded=false;notch.autoClosePending=false;
    if(state.popover?.dataset?.action==='runway-question')closePopover({restoreFocus:false,updateNotch:false});
    setNotch(widgetLock.expanded);return;
  }
  if(!count){
    clearTimeout(notch.inspectTimer);clearTimeout(notch.exitTimer);clearTimeout(notch.autoTimer);clearTimeout(notch.timer);
    notch.inspectTimer=notch.exitTimer=notch.autoTimer=notch.timer=null;
    notch.hover=false;notch.contentHover=false;notch.manual=false;notch.inspecting=false;notch.keyboardInspecting=false;notch.autoExpanded=false;notch.autoClosePending=false;
    if(state.popover?.dataset?.action==='runway-question')closePopover({restoreFocus:false,updateNotch:false});
    setNotch(false);return;
  }
  if(!count&&!notch.keyboardInspecting&&!state.popover)notch.manual=false;
  if(count&&newWork)expandForNewWork();
  else if(!count&&notch.autoExpanded&&notch.autoTimer!=null&&!notch.keyboardInspecting&&!notch.inspecting&&!state.popover)setNotch(true);
  else setNotch((count||notch.keyboardInspecting||notch.inspecting||state.popover)&&(notch.expanded||notch.inspecting||notch.manual));
}
function setupNotch() {
  if(!RUNWAY||PHONE)return;
  setupRunwayTreeScrollSync();
  if(!widgetLock.known)syncWidgetDragRegions(true);
  const widget=$('.widget');
  const pointerPoint=event=>COMPACT&&Number.isFinite(event.screenX)&&Number.isFinite(event.screenY)?{x:event.screenX,y:event.screenY}:{x:event.clientX,y:event.clientY};
  const inHeader=event=>{
    if(event.target?.closest?.('.titlebar'))return true;
    const rect=$('.titlebar')?.getBoundingClientRect?.();
    return Boolean(rect&&event.clientX>=rect.left&&event.clientX<=rect.right&&event.clientY>=rect.top&&event.clientY<=rect.bottom);
  };
  const scheduleInspect=()=>{if((!COMPACT&&(!widgetLock.known||widgetLock.locked))||notch.inspectTimer!=null||!state.runwaySnapshotPainted||(!COMPACT&&!notch.active))return;notch.inspectTimer=setTimeout(()=>{notch.inspectTimer=null;if((COMPACT||widgetLock.known&&!widgetLock.locked)&&notch.hover&&(!COMPACT?notch.contentHover:!notch.hoverSuppressed)&&state.runwaySnapshotPainted&&(COMPACT||notch.active)){notch.inspecting=true;setNotch(true);}},COMPACT?60:120);};
  const leave=()=>{if(!COMPACT&&(!widgetLock.known||widgetLock.locked))return;clearTimeout(notch.exitTimer);notch.exitTimer=setTimeout(()=>{
    if(COMPACT){
      const held=keepOpenState.value||state.searchOpen||Boolean(state.popover);
      if(!notch.hover&&!compactHasFocus()&&!notch.keyboardInspecting&&!held){const wasInspecting=notch.inspecting||notch.manual;notch.inspecting=false;notch.manual=false;if(wasInspecting){clearTimeout(notch.autoTimer);notch.autoTimer=null;notch.autoExpanded=false;}}
      setNotch(false);return;
    }
    if(widgetLock.known&&!widgetLock.locked&&!notch.hover&&!notch.keyboardInspecting&&!state.popover){const wasInspecting=notch.inspecting||notch.manual;notch.inspecting=false;notch.manual=false;if(wasInspecting){clearTimeout(notch.autoTimer);notch.autoTimer=null;notch.autoExpanded=false;setNotch(false);}else if(!notch.autoExpanded)setNotch(false);}
  },COMPACT?110:160);};
  widget.addEventListener('pointerenter',event=>{if(!COMPACT&&(!widgetLock.known||widgetLock.locked))return;const point=pointerPoint(event),deliberateEntry=notch.globalPointerX!=null&&notch.globalPointerY!=null&&Math.hypot(point.x-notch.globalPointerX,point.y-notch.globalPointerY)>=4;notch.hover=true;notch.contentHover=!inHeader(event);notch.pointerX=point.x;notch.pointerY=point.y;clearTimeout(notch.exitTimer);clearTimeout(notch.timer);if(COMPACT){if(!notch.hoverSuppressed&&deliberateEntry)scheduleInspect();setNotch(false);}else if(notch.contentHover&&(!notch.autoExpanded||deliberateEntry))scheduleInspect();});
  widget.addEventListener('pointermove',event=>{
    if(!COMPACT&&(!widgetLock.known||widgetLock.locked))return;
    const point=pointerPoint(event),moved=notch.pointerX!=null&&Math.hypot(point.x-notch.pointerX,point.y-notch.pointerY)>=4;
    if(!COMPACT||moved||notch.inspecting){notch.pointerX=point.x;notch.pointerY=point.y;}
    notch.contentHover=notch.hover&&!inHeader(event);
    if(!notch.contentHover&&!COMPACT){clearTimeout(notch.inspectTimer);notch.inspectTimer=null;}
    else if(moved&&!notch.keyboardInspecting&&!notch.inspecting&&(!COMPACT||!notch.hoverSuppressed))scheduleInspect();
    if(COMPACT)setNotch(false);
  });
  widget.addEventListener('pointerleave',()=>{if(!COMPACT&&(!widgetLock.known||widgetLock.locked))return;notch.hover=false;notch.hoverSuppressed=false;notch.contentHover=false;clearTimeout(notch.inspectTimer);notch.inspectTimer=null;leave();});
  widget.addEventListener('focusin',()=>{if(COMPACT){clearTimeout(notch.exitTimer);notch.exitTimer=null;setNotch(false);}});
  widget.addEventListener('focusout',event=>{if(COMPACT&&event.relatedTarget&&widget.contains?.(event.relatedTarget))return;leave();});
  const closeInspection=()=>{if(!COMPACT&&(!widgetLock.known||widgetLock.locked))return;notch.keyboardInspecting=false;notch.inspecting=false;notch.manual=false;notch.contentHover=false;if(COMPACT){clearTimeout(notch.inspectTimer);clearTimeout(notch.autoTimer);clearTimeout(notch.exitTimer);clearTimeout(notch.timer);notch.inspectTimer=notch.autoTimer=notch.exitTimer=notch.timer=null;notch.autoExpanded=false;notch.autoClosePending=false;notch.hoverSuppressed=notch.hover;if(compactHasFocus())document.activeElement?.blur?.();}if(!state.popover)setNotch(false);};
  if(COMPACT){document.addEventListener('pointerdown',()=>{compactInputModality='pointer';},true);document.addEventListener('keydown',()=>{compactInputModality='keyboard';},true);}
  document.addEventListener('pointermove',event=>{const point=pointerPoint(event);notch.globalPointerX=point.x;notch.globalPointerY=point.y;},true);
  window.tokenTrackerInspect=()=>{if((!COMPACT&&(!widgetLock.known||widgetLock.locked))||!state.runwaySnapshotPainted||(!COMPACT&&!notch.active))return false;notch.keyboardInspecting=true;notch.manual=true;notch.inspecting=true;clearTimeout(notch.exitTimer);setNotch(true);if(widget&&!widget.hasAttribute?.('tabindex'))widget.setAttribute?.('tabindex','-1');widget?.focus?.({preventScroll:true});return true;};
  window.tokenTrackerCloseInspection=closeInspection;
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!state.popover&&!state.searchOpen){closeInspection();}});
  window.addEventListener('pywebviewready',()=>{notch.sent=null;if(COMPACT){compactBridge.appliedSignature=null;compactBridge.pending=null;void syncKeepOpen();}void syncWidgetLock().then(()=>syncMonitors());});
  if(window.pywebview?.api?.get_widget_lock)void syncWidgetLock().then(()=>syncMonitors());
  $('#monitor-toggle')?.addEventListener('click',toggleMonitor);
  updateWidgetLockButton();if(COMPACT){updateKeepOpenButton();void syncKeepOpen();}
  syncMonitors();
  setInterval(syncMonitors,10000);
  setNotch(false);
}
const INSPECT_TARGET_WIDTH=560,INSPECT_WIDTH_INSET=4,INSPECT_FALLBACK_WIDTH=INSPECT_TARGET_WIDTH-INSPECT_WIDTH_INSET;
let monitorState={known:false,pending:false,error:false,monitors:[],selected:null,current:null,signature:null};
let monitorRequestGeneration=0,monitorSelectionGeneration=0,monitorSelectionPending=false;
let inspectMonitorSignature='fallback',inspectLayoutWidth=INSPECT_FALLBACK_WIDTH,inspectLayoutGeneration=0;
let inspectCssScaleSignature=null,inspectWidthCacheKey='',inspectWidthCorrectionUsed=false,inspectLayoutTimer=null;
let inspectExpectedSignature=null,inspectRequestGeneration=0,inspectResizeGeneration=0,inspectRequestResizeGeneration=-1;
const correctedInspectWidths=new Map();
function monitorSignature(monitor){
  if(!monitor||typeof monitor.id!=='string'||!monitor.id)return null;
  const scale=typeof monitor.scale==='number'?monitor.scale:Number(monitor.scale),area=monitor.workArea;
  if(!Number.isFinite(scale)||scale<=0||!area)return null;
  const values=[area.x,area.y,area.width,area.height].map(value=>typeof value==='number'?value:Number(value));
  if(values.some(value=>!Number.isFinite(value))||values[2]<=0||values[3]<=0)return null;
  return JSON.stringify([monitor.id,scale,...values]);
}
function updateMonitorButton(){
  const button=$('#monitor-toggle');if(!button)return;
  const knownSingle=monitorState.known&&!monitorState.pending&&!monitorSelectionPending&&monitorState.monitors.length<=1;
  button.hidden=knownSingle;
  const current=monitorState.current;
  if(current)button.textContent=current.kind==='internal'?'Laptop':current.kind==='external'?'Monitor':current.label||current.name||'Monitor';
  button.disabled=!monitorState.known||monitorState.pending||!widgetLock.known||widgetLock.pending||widgetLock.locked||monitorSelectionPending||monitorState.monitors.length<2;
  button.title=monitorSelectionPending?'Changing monitor':monitorState.pending?'Checking available monitors':!monitorState.known?(monitorState.error?'Monitor information unavailable':'Checking available monitors'):!widgetLock.known||widgetLock.pending?'Checking widget lock':widgetLock.locked?'Unlock the widget to move monitors':monitorState.monitors.length<2?'One monitor available':'Move to next monitor';
  if(current?.label&&monitorState.known&&!monitorState.pending&&!monitorSelectionPending)button.title=current.label+' · '+button.title;
}
function predictedInspectLayoutWidth(monitor){
  const signature=monitorSignature(monitor);if(!signature)return null;
  const scale=Number(monitor.scale),workWidth=Number(monitor.workArea.width);
  const physicalWidth=Math.min(Math.round(INSPECT_TARGET_WIDTH*scale),workWidth);
  const width=physicalWidth/scale-INSPECT_WIDTH_INSET;
  return Number.isFinite(width)&&width>0?width:null;
}
function writeInspectLayoutWidth(width){
  if(typeof width!=='number'||!Number.isFinite(width)||width<=0)return false;
  const normalized=Math.max(1,width),changed=Math.abs(normalized-inspectLayoutWidth)>0.01;
  inspectLayoutWidth=normalized;
  document.documentElement?.style?.setProperty?.('--runway-inspect-layout-width',normalized+'px');
  document.documentElement?.style?.setProperty?.('--runway-inspect-clock-axis',(normalized/2)+'px');
  return changed;
}
function cssScaleSignature(){
  const positive=value=>typeof value==='number'&&Number.isFinite(value)&&value>0?value:null;
  return JSON.stringify([positive(window.devicePixelRatio),positive(window.visualViewport?.scale)]);
}
function clearInspectLayoutTimer(){clearTimeout(inspectLayoutTimer);inspectLayoutTimer=null;}
function invalidateInspectBridgeRequest(){
  inspectExpectedSignature=null;inspectRequestGeneration++;inspectRequestResizeGeneration=-1;
  compactBridge.appliedSignature=null;notch.sent=null;
}
function refreshInspectCssScale(){
  const next=cssScaleSignature();
  if(inspectCssScaleSignature===null){inspectCssScaleSignature=next;inspectWidthCacheKey=JSON.stringify([inspectMonitorSignature,next]);return false;}
  if(next===inspectCssScaleSignature)return false;
  inspectCssScaleSignature=next;inspectWidthCacheKey=JSON.stringify([inspectMonitorSignature,next]);
  correctedInspectWidths.clear();inspectLayoutGeneration++;inspectWidthCorrectionUsed=false;clearInspectLayoutTimer();invalidateInspectBridgeRequest();
  return true;
}
function noteInspectViewportResize(){
  if(notch.mode!=='inspect')return;
  inspectResizeGeneration++;clearInspectLayoutTimer();
}
function noteInspectRequest(signature){
  if(notch.mode!=='inspect')return null;
  if(inspectExpectedSignature!==signature||inspectRequestResizeGeneration!==inspectResizeGeneration){
    inspectExpectedSignature=signature;inspectRequestResizeGeneration=inspectResizeGeneration;inspectRequestGeneration++;clearInspectLayoutTimer();
  }
  return inspectRequestGeneration;
}
function cancelInspectLayoutReconcile(){
  clearInspectLayoutTimer();inspectLayoutGeneration++;inspectResizeGeneration++;invalidateInspectBridgeRequest();
}
function applyInspectLayoutWidth(monitor){
  const cssScaleChanged=refreshInspectCssScale(),signature=monitorSignature(monitor),key=signature||'fallback';
  const monitorChanged=key!==inspectMonitorSignature;
  if(monitorChanged){
    inspectMonitorSignature=key;inspectLayoutGeneration++;inspectWidthCorrectionUsed=false;clearInspectLayoutTimer();invalidateInspectBridgeRequest();
  }
  const cacheKey=JSON.stringify([inspectMonitorSignature,inspectCssScaleSignature]);
  if(cacheKey!==inspectWidthCacheKey){inspectWidthCacheKey=cacheKey;inspectLayoutGeneration++;inspectWidthCorrectionUsed=false;clearInspectLayoutTimer();}
  const predicted=predictedInspectLayoutWidth(monitor),target=correctedInspectWidths.get(cacheKey)??predicted??INSPECT_FALLBACK_WIDTH;
  const changed=writeInspectLayoutWidth(target);
  if((changed||monitorChanged||cssScaleChanged)&&notch.mode==='inspect')scheduleNotchSize();
  return changed;
}
function armInspectLayoutReconcile(signature){
  if(!COMPACT||notch.mode!=='inspect'||signature!==inspectExpectedSignature
      ||inspectRequestResizeGeneration!==inspectResizeGeneration||compactBridge.busy||compactBridge.pending
      ||compactBridge.inFlightSignature||compactBridge.appliedSignature!==signature||inspectWidthCorrectionUsed)return;
  clearInspectLayoutTimer();
  const generation=inspectLayoutGeneration,request=inspectRequestGeneration,resize=inspectResizeGeneration,
    monitor=inspectMonitorSignature,scale=inspectCssScaleSignature,cacheKey=inspectWidthCacheKey;
  inspectLayoutTimer=setTimeout(()=>{
    inspectLayoutTimer=null;
    if(notch.mode!=='inspect'||generation!==inspectLayoutGeneration||request!==inspectRequestGeneration
        ||resize!==inspectResizeGeneration||monitor!==inspectMonitorSignature||scale!==inspectCssScaleSignature
        ||cacheKey!==inspectWidthCacheKey||signature!==inspectExpectedSignature||compactBridge.appliedSignature!==signature
        ||compactBridge.busy||compactBridge.pending||compactBridge.inFlightSignature||inspectWidthCorrectionUsed)return;
    if(cssScaleSignature()!==inspectCssScaleSignature){refreshInspectCssScale();scheduleNotchSize();return;}
    const viewport=Number(document.documentElement?.clientWidth)||Number(window.innerWidth);
    if(!Number.isFinite(viewport)||viewport<=0)return;
    const settledWidth=Math.max(1,viewport-INSPECT_WIDTH_INSET);
    if(Math.abs(settledWidth-inspectLayoutWidth)<=1)return;
    inspectWidthCorrectionUsed=true;correctedInspectWidths.set(cacheKey,settledWidth);
    writeInspectLayoutWidth(settledWidth);
    scheduleNotchSize();
  },180);
}
function applyMonitorResult(result,requestGeneration,selectionGeneration){
  if(requestGeneration!==monitorRequestGeneration||selectionGeneration!==monitorSelectionGeneration)return false;
  const monitors=Array.isArray(result)?result:Array.isArray(result?.monitors)?result.monitors:null;
  if(!monitors||!monitors.every(m=>m&&typeof m==='object'&&typeof m.id==='string'&&m.id.trim())){
    monitorState={...monitorState,known:false,pending:false,error:true};updateMonitorButton();return false;
  }
  const selectedId=Array.isArray(result)?null:result?.selected??result?.selectedId??null;
  const current=monitors.find(m=>m.id===selectedId||m.selected)||monitors[0]||null;
  monitorState={known:true,pending:false,error:false,monitors,selected:current?.id??selectedId,current,signature:monitorSignature(current)};
  applyInspectLayoutWidth(current);
  updateMonitorButton();
  return true;
}
function setupWindowDrag() {
  if(!RUNWAY||PHONE)return;
  if(COMPACT){
    let gesture=null,queue=Promise.resolve();
    document.addEventListener('mousedown',event=>{
      if(!widgetLock.known||event.button!==0||gesture||!event.target?.closest?.('.compact-toolbar.pywebview-drag-region,.compact-summary.pywebview-drag-region')||event.target.closest('button,input,textarea,select,a,[role="button"],[data-action],[contenteditable="true"]'))return;
      const api=window.pywebview?.api;
      if(typeof api?.begin_drag!=='function'||typeof api?.end_drag!=='function')return;
      let release;
      const current={ended:false,commit:false,released:new Promise(resolve=>{release=resolve;})};current.release=release;gesture=current;
      const operation=queue.then(async()=>{
        const result=await api.begin_drag('compact-v1');
        if(!result||result.accepted!==true||!Number.isSafeInteger(result.generation))return false;
        if(!current.ended)await current.released;
        await api.end_drag(result.generation,current.commit);return true;
      });
      queue=operation.catch(()=>false);
    },true);
    const endCompact=event=>{
      if(!gesture||(event?.type==='mouseup'&&event.button!==0))return;
      const current=gesture;gesture=null;current.commit=event?.type==='mouseup'&&event.button===0;current.ended=true;current.release();
    };
    document.addEventListener('mouseup',endCompact,true);window.addEventListener('mouseup',endCompact,true);
    document.addEventListener('pointercancel',endCompact,true);window.addEventListener('blur',endCompact,true);
    return;
  }
  let gesture=null,queue=Promise.resolve();
  document.addEventListener('mousedown',event=>{
    if(!widgetLock.known||widgetLock.locked||event.button!==0||gesture||!event.target?.closest?.('.pywebview-drag-region')||event.target.closest('button,input,textarea,select,a,[role="button"]'))return;
    const api=window.pywebview?.api;
    if(!api?.begin_drag||!api?.end_drag)return;
    const current={x:event.clientX,y:event.clientY,moved:false,api};
    current.started=queue.then(()=>api.begin_drag()).catch(()=>false);
    queue=current.started;gesture=current;
  },true);
  document.addEventListener('mousemove',event=>{
    if(gesture&&Math.hypot(event.clientX-gesture.x,event.clientY-gesture.y)>=2)gesture.moved=true;
  },true);
  const end=event=>{
    if(!gesture||(event?.type==='mouseup'&&event.button!==0))return;
    const current=gesture;gesture=null;
    queue=current.started.then(started=>started===false?false:current.api.end_drag(current.moved)).catch(()=>false);
  };
  document.addEventListener('mouseup',end,true);
  window.addEventListener('mouseup',end,true);
  document.addEventListener('pointercancel',end,true);
  window.addEventListener('blur',end,true);
}
async function syncMonitors() {
  const requestGeneration=++monitorRequestGeneration,selectionGeneration=monitorSelectionGeneration;
  monitorState={...monitorState,pending:true,error:false};updateMonitorButton();
  try {
    const result=await window.pywebview?.api?.get_monitors?.();
    if(requestGeneration!==monitorRequestGeneration||selectionGeneration!==monitorSelectionGeneration||monitorSelectionPending)return false;
    const applied=applyMonitorResult(result,requestGeneration,selectionGeneration);
    if(!applied&&requestGeneration===monitorRequestGeneration){monitorState={...monitorState,known:false,pending:false,error:true};updateMonitorButton();}
    return applied;
  }catch{
    if(requestGeneration===monitorRequestGeneration&&selectionGeneration===monitorSelectionGeneration){monitorState={...monitorState,known:false,pending:false,error:true};updateMonitorButton();}
    return false;
  }
}
async function toggleMonitor() {
  if(!monitorState.known||monitorState.pending||!widgetLock.known||widgetLock.pending||widgetLock.locked||monitorSelectionPending||monitorState.monitors.length<2)return;
  const index=monitorState.monitors.findIndex(m=>m.id===monitorState.selected);
  const next=monitorState.monitors[(index+1)%monitorState.monitors.length];
  if(!next)return;
  const selectionGeneration=++monitorSelectionGeneration;
  monitorSelectionPending=true;monitorRequestGeneration++;
  updateMonitorButton();
  try{
    const result=await window.pywebview?.api?.select_monitor?.(next.id);
    if(selectionGeneration!==monitorSelectionGeneration)return;
    monitorSelectionPending=false;
    if(result){const requestGeneration=++monitorRequestGeneration;if(!applyMonitorResult(result,requestGeneration,selectionGeneration))void syncMonitors();}
    else await syncMonitors();
  }catch{
    if(selectionGeneration===monitorSelectionGeneration){monitorSelectionPending=false;void syncMonitors();}
  }
}
const safeNumber = value => value == null || value === "" || typeof value === "boolean" ? null : Number.isFinite(Number(value)) ? Number(value) : null;
const fmt = value => safeNumber(value) == null ? "—" : compact.format(Number(value));
const money = value => { const n=safeNumber(value); return n==null ? "—" : "$"+(n>=1000 ? compact.format(n) : n>=100 ? Math.round(n) : n.toFixed(n>=10?1:2)); };
const esc = value => String(value ?? "").replace(/[&<>'"]/g, char => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", "'":"&#39;", '"':"&quot;" })[char]);
const stamp = value => typeof value==="number" ? (value<1e12?value*1000:value) : Number.isFinite(Date.parse(value)) ? Date.parse(value) : 0;
const keyOf = chat => chat.provider + ":" + chat.id;
const treeParentKey = chat => Object.prototype.hasOwnProperty.call(chat||{},"treeParentKey") ? (typeof chat.treeParentKey==="string"&&chat.treeParentKey?chat.treeParentKey:null) : chat?.parentId!=null ? chat.provider+":"+chat.parentId : null;
const hasTreeParent = chat => Boolean(treeParentKey(chat));
const pad2 = n => String(n).padStart(2,"0");
const localISODate = () => { const date=new Date(); return date.getFullYear()+"-"+pad2(date.getMonth()+1)+"-"+pad2(date.getDate()); };
const clock = ms => new Date(ms).toLocaleTimeString("en-US",{hour:"numeric",minute:"2-digit",hour12:true}).toLowerCase();
const mmss = seconds => { const s=Math.max(0,Math.floor(seconds)); return Math.floor(s/60)+":"+pad2(s%60); };
// Reset countdown: "5d 19h", "3h 02m", "42m", then m:ss in the last 10 minutes.
function countdown(seconds) {
  const s=Math.max(0,Math.floor(seconds));
  if(s>=86400)return Math.floor(s/86400)+"d "+pad2(Math.floor(s%86400/3600))+"h";
  if(s>=3600)return Math.floor(s/3600)+"h "+pad2(Math.floor(s%3600/60))+"m";
  if(s>=600)return Math.floor(s/60)+"m";
  return mmss(s);
}
// Run time: m:ss under 10 minutes, then 12m, then 1h 04m.
function runSpan(seconds) {
  const s=Math.max(0,Math.floor(seconds));
  return s<600 ? mmss(s) : s<3600 ? Math.floor(s/60)+"m" : s<86400 ? Math.floor(s/3600)+"h "+pad2(Math.floor(s%3600/60))+"m" : Math.floor(s/86400)+"d "+Math.floor(s%86400/3600)+"h";
}
function ageShort(ms) {
  if(!ms)return "—";
  const m=Math.max(0,Math.floor((Date.now()-ms)/60000));
  return m<1?"now":m<60?m+"m":m<1440?Math.floor(m/60)+"h":Math.floor(m/1440)+"d";
}
function quotaAgeLabel(ms) {
  const minutes=Math.max(0,Math.floor((Date.now()-ms)/60000));
  if(minutes<60)return minutes+"m";
  const hours=Math.floor(minutes/60),remaining=minutes%60;
  if(hours<24)return remaining?hours+"h "+remaining+"m":hours+"h";
  const days=Math.floor(hours/24),remainingHours=hours%24;
  return remainingHours?days+"d "+remainingHours+"h":days+"d";
}
function dayWord(ms, alwaysDate=false) {
  const today=new Date();today.setHours(0,0,0,0);
  const d=new Date(ms),start=new Date(d);start.setHours(0,0,0,0);
  const days=Math.round((start-today)/864e5);
  if(!alwaysDate&&days===0)return "Today";
  if(!alwaysDate&&days===1)return "Tomorrow";
  return ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"][d.getDay()]+" "+d.getDate()+" "+["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][d.getMonth()];
}
// `code` spans in questions render mono.
const richText = text => esc(text).replace(/`([^`]+)`/g,'<code>$1</code>');

/* ---------- glyphs (12px, currentColor, 1.25 stroke) ---------- */
const svg = (body, cls="ic", box=12) => '<svg class="'+cls+'" viewBox="0 0 '+box+' '+box+'" aria-hidden="true">'+body+'</svg>';
const ICON = {
  branch: svg('<circle cx="3.5" cy="2.6" r="1.35"/><circle cx="3.5" cy="9.4" r="1.35"/><circle cx="8.8" cy="3.6" r="1.35"/><path d="M3.5 3.95v4.1M8.8 4.95c0 2.3-5.3 1.6-5.3 3.1"/>',"ic ic-branch"),
  copy: svg('<rect x="4" y="4" width="6.5" height="6.5" rx="1.25"/><path d="M8 4V2.75C8 2.06 7.44 1.5 6.75 1.5h-4c-.69 0-1.25.56-1.25 1.25v4c0 .69.56 1.25 1.25 1.25H4"/>',"ic ic-copy"),
  check: svg('<path d="m2.5 6.3 2.3 2.2 4.7-5"/>',"ic ic-check"),
  close: svg('<path d="m3 3 6 6M9 3 3 9"/>'),
  message: svg('<path d="M2 2h8v6H5l-3 2Z"/><path d="M4 4h4M4 6h2"/>',"ic ic-message"),
  open: svg('<path d="M6.5 2.5h3v3M9.5 2.5 5.5 6.5M8.5 7v2a1 1 0 0 1-1 1h-4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h2"/>',"ic ic-open"),
  chevron: svg('<path d="m4.5 2.8 3.2 3.2-3.2 3.2"/>',"ic ic-chev"),
  clock: svg('<circle cx="6" cy="6" r="4.5"/><path d="M6 3.6V6l1.7 1.1"/>',"ic ic-clock"),
  pin: svg('<path d="M4.5 1.8h3M5 1.8v3L3.2 6.8h5.6L7 4.8v-3M6 6.8v3.4"/>'),
};
const MARK = {
  openai: svg('<g transform="translate(7 7)"><rect x="-2.3" y="-5.6" width="4.6" height="11.2" rx="2.3"/><rect x="-2.3" y="-5.6" width="4.6" height="11.2" rx="2.3" transform="rotate(60)"/><rect x="-2.3" y="-5.6" width="4.6" height="11.2" rx="2.3" transform="rotate(120)"/></g>',"mk",14),
  claude: svg('<g transform="translate(7 7)"><path d="M0-5.6V5.6M-4.85-2.8 4.85 2.8M-4.85 2.8 4.85-2.8"/><path d="M-2.8-4.85 2.8 4.85M-5.6 0H5.6M-2.8 4.85 2.8-4.85" opacity=".55"/></g>',"mk",14),
  review: svg('<path d="M7 1.6 11.6 3.3v3.4c0 2.7-1.9 4.8-4.6 5.7-2.7-.9-4.6-3-4.6-5.7V3.3Z"/><path d="m4.9 7 1.5 1.5 2.8-3"/>',"mk",14),
};

/* ---------- status semantics ---------- */
function normalizeStatus(value) {
  const status=String(value || "unknown").toLowerCase().replace(/[ _]+/g,"-");
  if(["active","working","in-progress"].includes(status))return "running";
  if(["waiting","blocked","needs-answer","needs-input"].includes(status))return "needs-input";
  if(["error","errored"].includes(status))return "failed";
  if(["completed","done","inactive"].includes(status))return "idle";
  return ["running","compacting","quiet","needs-input","failed","idle"].includes(status) ? status : "unknown";
}
const statusRank = status => ({"needs-input":0,failed:1,running:2,compacting:2,quiet:2,idle:3,unknown:4})[status] ?? 4;
// Extra cost of going cold, in full-price-token equivalents. The next turn's cacheable prefix is the
// whole context: warm, it bills at the cached rate (10% of input for GPT models); cold, at full price.
// So the penalty is the discount lost on the context, not the context size itself.
const CACHED_RATE = 0.1;
function coldPenalty(chat) {
  const context=safeNumber(chat.contextTokens);
  return context==null?null:Math.round(context*(1-CACHED_RATE));
}
// Resume command per provider. Claude offers one only for a resumable CLI session; never a Codex command.
function resumeCommand(chat) {
  if(chat?.provider==="Claude"){
    if(chat.resumable!==true)return "";
    const id=typeof chat.sessionId==="string"&&chat.sessionId?chat.sessionId:String(chat.id||"");
    const match=/^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?::[A-Za-z0-9_-]+)?$/i.exec(id);
    return match?"claude --resume "+match[1]:"";
  }
  return "codex resume "+chat.id;
}
const isCodexChat = chat => chat?.provider==="Codex";
// Read-only choice list for sources that cannot be answered through Codex.
function plainOptionsHTML(options) {
  const opts=Array.isArray(options)?options:[];
  return opts.length?'<ol class="q-plain">'+opts.map(text=>'<li>'+esc(text)+'</li>').join('')+'</ol>':'';
}
// Choices can be copied/opened, or queued as a normal Codex message for the next turn.
// Queueing does not resolve the pending structured question.
function optionsHTML(options, thread) {
  const opts=Array.isArray(options)?options:[];
  const t=thread?' data-thread="'+esc(thread)+'"':'';
  return '<div class="q-opts">'+(opts.length?'<span class="q-action-note">Open Codex to answer · queue is a later message</span>':'')+opts.map((text,i)=>'<div class="q-choice"><button class="q-opt" data-action="answer" data-answer="'+esc(text)+'"'+t+' aria-label="Open Codex to answer: '+esc(text)+'" title="Open this task in Codex and select the choice in its question prompt. The choice is also copied for reference."><b>'+(i+1)+'</b>'+(RUNWAY?'<span>Open in Codex</span> · ':'')+esc(text)+ICON.check+'</button><button class="q-open q-queue" data-action="queue-answer" data-answer="'+esc(text)+'"'+t+' title="Queue this as a message for the next turn. It will not answer the pending question."><span>Queue to chat</span></button></div>').join("")+ 
    (thread?'<button class="q-open" data-action="open"'+t+' title="Open this chat in Codex">'+ICON.open+'<span>'+(opts.length?"Open":"Answer in Codex")+'</span>'+ICON.check+'</button>':'')+'</div>';
}
const QUESTION_STALE_MS=24*60*60*1000;
const isAsking = chat => (!RUNWAY||(!chat.pendingQuestionExpired&&!(stamp(chat.pendingQuestionAt)&&Date.now()-stamp(chat.pendingQuestionAt)>=QUESTION_STALE_MS)))&&(Boolean(chat.pendingQuestion)||chat.status==="needs-input");
// A failure is only actionable while it is recent (30 min).
const recentFail = chat => chat.status==="failed"&&Date.now()-stamp(chat.updatedAt)<FAIL_WINDOW_MS;
const needsAttention = chat => isAsking(chat)||recentFail(chat);
const agentLive = chat => chat.status==="running"||chat.status==="compacting"||chat.status==="quiet"||isAsking(chat);
const runwayTerminated = chat => Boolean(chat.deleted||chat.isDeleted)||[chat.stateDetail,chat.sourceStatus].some(value=>/^(?:completed|finished|stopped|interrupted|deleted|expired)$/i.test(String(value||'')));
const runwayWaiting = chat => !chat.telemetryUnavailable&&!runwayTerminated(chat)&&isAsking(chat);
function runwayExecutionView(chat) {
  if(!RUNWAY||runwayWaiting(chat)||runwayTerminated(chat)||chat.telemetryUnavailable||stamp(chat.executionTerminalAt))return null;
  const status=normalizeStatus(chat.executionStatus),source=chat.executionSource;
  if(!['running','compacting'].includes(status)||!['transcript','app','hook'].includes(source))return null;
  const observed=stamp(chat.executionObservedAt),age=Date.now()-observed;
  if(!observed||age<0||age>150000)return null;
  return {status,since:status==='compacting'?chat.executionCompactionStartedAt:chat.executionSince,
    activity:(source==='transcript'||source==='hook')&&chat.executionActivity&&typeof chat.executionActivity==='object'?chat.executionActivity:null,source};
}
const runwayEffectiveStatus = chat => {
  const execution=runwayExecutionView(chat);
  if(execution)return execution.status;
  if(RUNWAY&&chat.executionStatus!==undefined)return runwayWaiting(chat)?'needs-input':['idle','failed'].includes(normalizeStatus(chat.executionStatus))?normalizeStatus(chat.executionStatus):'unknown';
  return normalizeStatus(chat.status);
};
const runwayRunning = chat => !runwayTerminated(chat)&&!runwayWaiting(chat)&&(chat.executionStatus!==undefined?Boolean(runwayExecutionView(chat)):['running','compacting'].includes(chat.status));
const runwayActive = chat => runwayRunning(chat)||runwayWaiting(chat);
function runwayActivityEvidence(chat) {
  if(!RUNWAY||PHONE||runwayTerminated(chat))return {status:'inactive',activity:null};
  if(!runwayCurrentEvidenceFresh())return {status:chat.telemetryUnavailable?'unavailable':'stale',activity:null};
  if(Object.prototype.hasOwnProperty.call(chat,'executionStatus')){
    const normalized=normalizeStatus(chat.executionStatus),status=normalized==='needs-input'?'waiting':normalized,source=chat.executionSource;
    if(status==='unavailable'||source==='unavailable')return {status:'unavailable',activity:null};
    if(!['running','compacting','waiting'].includes(status))return {status,activity:null};
    const observed=stamp(chat.executionObservedAt),age=Date.now()-observed;
    if(!['transcript','app','hook'].includes(source)||!observed||age<0||age>150000||stamp(chat.executionTerminalAt))return {status:'stale',activity:null};
    return {status,activity:(source==='transcript'||source==='hook')&&chat.executionActivity&&typeof chat.executionActivity==='object'?chat.executionActivity:null};
  }
  if(chat.telemetryUnavailable||chat.activityUnavailable)return {status:'unavailable',activity:null};
  const status=normalizeStatus(chat.status);
  if(status==='compacting'){
    const observed=stamp(chat.compactionStartedAt),age=Date.now()-observed;
    return observed&&age>=0&&age<=150000?{status:'compacting',activity:null}:{status:'stale',activity:null};
  }
  if(status==='needs-input'||runwayWaiting(chat))return {status:'waiting',activity:null};
  if(status!=='running')return {status:['idle','failed'].includes(status)?status:'unknown',activity:null};
  const activity=chat.activity&&typeof chat.activity==='object'?chat.activity:null;
  const observed=stamp(activity?.observedAt),age=Date.now()-observed;
  return observed&&age>=0&&age<=150000?{status:'running',activity}:{status:'stale',activity:null};
}
function runwayRecentlyUnavailable(chat) {
  if(!chat.telemetryUnavailable||runwayTerminated(chat))return false;
  const age=Date.now()-stamp(chat.lastValidAt),prior=normalizeStatus(chat.previousStatus);
  return age>=0&&age<=150000&&['running','compacting','needs-input','quiet'].includes(prior);
}
const runwayVisibleActive=chat=>runwayCurrentEvidenceFresh()&&(runwayActive(chat)||runwayRecentlyUnavailable(chat));
function runwayCounts(forest) {
  const roots=forest.roots.filter(chat=>PHONE||!state.hiddenChats.has(chat.key));
  const unique=new Map(roots.flatMap(chat=>[chat,...chat.descendants]).map(chat=>[chat.key,chat]));
  const active=chat=>runwayCurrentEvidenceFresh()&&(runwayRunning(chat)||runwayWaiting(chat));
  return {roots:roots.filter(chat=>[chat,...chat.descendants].some(active)).length,total:[...unique.values()].filter(active).length,waiting:runwayCurrentEvidenceFresh()?[...unique.values()].filter(runwayWaiting).length:0};
}
// The yellow waiting count opens the widget on the first waiting chat and shows its question.
function jumpToWaiting() {
  const target=[...(state.forest?.rows||[])].find(chat=>runwayWaiting(chat));if(!target)return;
  notch.manual=true;setNotch(true);
  const reveal=()=>{const link=[...(document.querySelectorAll?.('.runway-waiting-link')||[])].find(item=>item.dataset?.key===target.key);if(!link)return;link.scrollIntoView?.({block:'nearest'});if(state.popover===link&&!$('#popover')?.hidden)return;link.click?.();};
  if(typeof requestAnimationFrame==='function')requestAnimationFrame(()=>requestAnimationFrame(reveal));else reveal();
}
function renderRunwayCounts(counts) {
  const nodes=[$('#notch-counts'),...(COMPACT?[$('#compact-counts')]:[])].filter(Boolean);if(!nodes.length)return;
  if(!counts){for(const node of nodes){if(node._runwayCountHTML!==''){node.innerHTML='';node._runwayCountHTML='';}if(node.title)node.title='';if(node.getAttribute?.('aria-label'))node.removeAttribute?.('aria-label');}return;}
  const chatsWord=counts.roots===1?'chat':'chats';
  const label=counts.roots+' chat group'+(counts.roots===1?'':'s')+', '+counts.total+' active, '+counts.waiting+' waiting';
  const badge=(slot,value,unit,tone,path,meaning,visible=true)=>{if(unit==='chat'||unit==='chats')meaning='visible chat groups';const hidden=visible?'':' aria-hidden="true"';const jump=slot==='waiting'&&visible&&!PHONE?' data-action="jump-waiting" role="button" tabindex="0"':'';return '<span data-count-slot="'+slot+'" class="runway-count runway-count-'+slot+' t-'+tone+(visible?'':' is-empty')+'"'+hidden+jump+' aria-label="'+value+' '+meaning+(jump?', show the first':'')+'" title="'+value+' '+meaning+(jump?' · click to show':'')+'"><svg class="ic" viewBox="0 0 12 12" aria-hidden="true"><path d="'+path+'"/></svg><span class="count-value">'+(visible?value:'')+'</span><span class="count-unit">'+(visible?unit:'')+'</span></span>';};
  const html=badge('groups',counts.roots,chatsWord,'live','M2 2h8v6H5l-3 2Z',chatsWord,!COMPACT||counts.roots>0)+badge('active',counts.total,'active','live','M3 1v8h6M3 4h6M9 2v4M9 7v4','active',!COMPACT||counts.total>0)+badge('waiting',counts.waiting,'waiting','attn','M6 2v5M6 9h.01','waiting',Boolean(counts.waiting));
  for(const node of nodes){
    if(node._runwayCountHTML!==html){node.innerHTML=html;node._runwayCountHTML=html;}
    if(node.title!==label)node.title=label;
    if(node.getAttribute?.('aria-label')!==label)node.setAttribute('aria-label',label);
  }
}
// Context meter tone: neutral < 75, amber 75–89, red >= 90.
const contextTone = percent => percent>=90 ? "err" : percent>=75 ? "attn" : "";
function ownState(chat) {
  if(isAsking(chat))return {key:"needs",glyph:"dot",tone:"attn",word:"Needs you"};
  if(chat.status==="failed") {
    const word=chat.stateDetail==="interrupted"?"Stopped":"Failed";
    return recentFail(chat)?{key:"failed",glyph:"dot",tone:"err",word}:{key:"failed-old",glyph:"dot",tone:"mute",word};
  }
  if(chat.status==="running")return {key:"running",glyph:"dot",tone:"live",word:"Running"};
  if(chat.status==="compacting")return {key:"compacting",glyph:"dot",tone:"attn",word:"Compacting"};
  if(chat.status==="quiet")return {key:"quiet",glyph:"ring",tone:"live",wordTone:"mute",word:"Quiet",title:"No transcript writes for 3+ minutes; may be in a long tool call"};
  if(chat.status==="unknown")return {key:"unknown",glyph:"dash",tone:"mute",word:"Status unavailable",title:"Status unavailable"};
  if(chat.stateDetail==="inactive")return {key:"inactive",glyph:"ring",tone:"mute",word:"Inactive",title:"Recorded as working with no completion and no writes for 30+ minutes"};
  return {key:"idle",glyph:"dot",tone:"mute",word:""};
}
// The parent shows the most urgent state in its tree: needs-you > failed(<30m) > running > own.
function rollup(chat) {
  const tree=[chat,...(chat.descendants||[])];
  for(const test of [isAsking,recentFail,c=>c.status==="compacting"||c.status==="running"]) {
    const hit=tree.find(test);
    if(hit)return {...ownState(hit),via:hit!==chat};
  }
  return {...ownState(chat),via:false};
}
const isAutoReview = chat => chat.agentRole==="auto-review"||/auto-review/i.test(chat.model||"")||hasTreeParent(chat)&&chat.title==="Subagent"&&(!chat.model||chat.model==="Codex");
function shortModel(value) {
  const text=String(value||"").trim();
  if(!text)return "—";
  if(/auto-review/i.test(text))return "review";
  return text.replace(/^claude-/,"").replace(/^gpt-/,"").replace(/-20\d{6}$/,"");
}
const modelLabel = chat => isAutoReview(chat)&&(!chat.model||chat.model==="Codex") ? "review" : shortModel(chat.model);
const agentModelLabel = chat => /^gpt-/i.test(chat.model||"") ? "GPT-"+modelLabel(chat).replace(/-(astra|sol|luna|terra)$/i,(_,family)=>" "+family[0].toUpperCase()+family.slice(1)) : modelLabel(chat);
function runwayModelLabel(chat) {
  if(isAutoReview(chat))return "Auto-review";
  const model=String(chat.model||"").trim().replace(/-20\d{6}$/,'');if(!model||model.toLowerCase()==='codex')return "";
  let match=model.match(/^gpt-(\d+(?:\.\d+)?)(?:-(.+))?$/i);
  if(match){const family=(match[2]||"").split("-").filter(Boolean).at(-1)||"";return "GPT-"+match[1]+(family?" "+family[0].toUpperCase()+family.slice(1).toLowerCase():"");}
  match=model.match(/^claude-(?:(opus|sonnet|haiku)-(\d+)[.-](\d+)|(\d+)[.-](\d+)-(opus|sonnet|haiku))(?:-.+)?$/i);
  if(match){const family=match[1]||match[6],major=match[2]||match[4],minor=match[3]||match[5];return family[0].toUpperCase()+family.slice(1).toLowerCase()+" "+major+"."+minor;}
  match=model.match(/^claude-(opus|sonnet|haiku)(?:-.+)?$/i);
  if(match)return match[1][0].toUpperCase()+match[1].slice(1).toLowerCase();
  return model;
}
function runwayModelRecentlyChanged(chat) {
  const key=chat.key||[chat.provider,chat.id].join(':'),model=chat.telemetryUnavailable?'':String(chat.model||'').trim(),changedAt=stamp(chat.modelChangeAt);
  const previous=state.modelVersions.get(key),now=Date.now();
  let highlightUntil=previous?.highlightUntil||0,highlightEventAt=previous?.highlightEventAt||0,highlightModel=previous?.highlightModel||null;
  const previousEventHighWater=previous?.eventHighWater??(previous?.changedAt>0&&previous.changedAt<=now?previous.changedAt:0);
  const ignoredFutureEvents=new Set(previous?.ignoredFutureEvents||[]);if(changedAt>now)ignoredFutureEvents.add(changedAt);
  const validEvent=Boolean(changedAt&&changedAt<=now&&!ignoredFutureEvents.has(changedAt)),actualChange=Boolean(previous&&previous.model&&model&&previous.model!==model&&validEvent&&changedAt>previousEventHighWater&&now-changedAt<3000);
  const eventHighWater=validEvent?Math.max(previousEventHighWater,changedAt):previousEventHighWater;
  if(actualChange){highlightUntil=changedAt+3000;highlightEventAt=changedAt;highlightModel=model;}
  else if(previous&&previous.model&&model&&previous.model!==model){highlightUntil=0;highlightEventAt=0;highlightModel=null;}
  state.modelVersions.set(key,{model,changedAt,eventHighWater,ignoredFutureEvents,highlightUntil,highlightEventAt,highlightModel});
  // A first-seen row establishes the baseline; only a later model transition
  // with a newer provider-recorded event time gets the short highlight.
  return Boolean(previous&&previous.model&&model&&highlightModel===model&&highlightUntil>now);
}
function agentLabel(chat) {
  if(isAutoReview(chat))return "Auto-review";
  const name=String(chat.agentName||"").replace(/^\/?root\//,"").split(/[\\/]/).filter(Boolean).pop();
  // Same display as the Codex app: "terra_foundations" -> "Terra foundations"; nicknames stay as is.
  const label=String(name||chat.title||"agent").replace(/[_-]+/g," ").trim();
  return label.charAt(0).toUpperCase()+label.slice(1);
}
// Codex subagents wear the avatar the Codex app gives them: avatars[hash(threadId) % n], same
// hash as the app. Colour while working, grey when idle (CSS). Falls back to monochrome marks.
function avatarIndex(seed, count) {
  let value=0;
  for(const char of String(seed))value=(value*31+char.charCodeAt(0))%2147483647;
  return value%count;
}
function markFor(chat) {
  if(isAutoReview(chat))return MARK.review;
  const avatars=state.avatars;
  if(chat.provider!=="Claude"&&hasTreeParent(chat)&&avatars?.length)
    return '<img class="av'+(isWorking(chat)?" on":"")+'" alt="" draggable="false" src="'+avatars[avatarIndex(chat.id,avatars.length)]+'">';
  return chat.provider==="Claude" ? MARK.claude : MARK.openai;
}
async function loadAvatars() {
  try {
    const response=await fetch("/api/avatars",{signal:AbortSignal.timeout(60000)});
    const result=response.ok?await response.json():null;
    if(Array.isArray(result?.avatars)&&result.avatars.length){state.avatars=result.avatars;if(state.snapshot)render(state.snapshot);}
  } catch {}
}

/* ---------- data ---------- */
function providerActivity(provider, days) {
  if(!Array.isArray(provider.activity))return null;
  const end=new Date();end.setHours(0,0,0,0);
  const start=new Date(end);start.setDate(start.getDate()-days+1);
  return provider.activity.reduce((sum,item)=>{const date=new Date(item.date+"T00:00:00");return date>=start && date<=end ? sum+(safeNumber(item.value)||0) : sum;},0);
}
const connectedActivity = (provider, days) => provider.connected===false ? null : providerActivity(provider, days);
const knownSum = values => values.every(value=>value==null) ? null : values.reduce((sum,value)=>sum+(value||0),0);
function resetAt(item, observedAt) {
  if(safeNumber(item.resetAt)!=null)return Number(item.resetAt)*1000;
  const raw=String(item.reset || "").replace(/^resets\s*/i,"").trim();
  if(/^\d{4}-\d\d-\d\dT/.test(raw))return stamp(raw)||null;
  const m=raw.match(/^(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m)?$/i);
  const base=stamp(observedAt);
  return m && raw && base ? base+(Number(m[1]||0)*86400+Number(m[2]||0)*3600+Number(m[3]||0)*60)*1000 : null;
}
// Day word + time + countdown for one limit. Weekly always carries its date (it can be 6 days out).
function resetParts(item, observedAt, period="fiveHour") {
  if(typeof item==="string")item={reset:item};
  item=item||{};
  const known=resetAt(item,observedAt),historical=known==null&&safeNumber(item.lastKnownResetAt)!=null;
  const target=known??(historical?Number(item.lastKnownResetAt)*1000:null);
  if(target==null)return {day:"",time:"—:—",countdown:"",due:false,historical:false,target:null,seconds:null};
  const seconds=Math.ceil((target-Date.now())/1000);
  return {day:dayWord(target,period==="weekly"),time:clock(target),countdown:seconds>0?countdown(seconds):"due",due:seconds<=0,historical,target,seconds};
}
function validPublicProviderPolicy(metadata) {
  if(!metadata||typeof metadata!=='object'||Array.isArray(metadata)||metadata.schemaVersion!==1||typeof metadata.generation!=='string'||!metadata.generation||!Array.isArray(metadata.providers))return false;
  const rows=metadata.providers.filter(row=>row&&typeof row==='object'&&!Array.isArray(row));
  return rows.length===metadata.providers.length&&rows.some(row=>row.name==='Codex')&&rows.every(row=>SOURCES.includes(row.name)&&typeof row.collect==='boolean'&&typeof row.show==='boolean');
}
function policyForSources(snapshot) {
  if(validPublicProviderPolicy(state.providerPolicy))return state.providerPolicy;
  if(validPublicProviderPolicy(snapshot?.providerPolicies))return snapshot.providerPolicies;
  return null;
}
function visibleProviderNames(snapshot) {
  const metadata=policyForSources(snapshot);
  if(!metadata||metadata.schemaVersion!==1||typeof metadata.generation!=="string"||!metadata.generation||!Array.isArray(metadata.providers))return new Set(["Codex"]);
  const known=new Set(["Codex","Claude"]),policies=metadata.providers.filter(policy=>policy&&known.has(policy.name)&&typeof policy.collect==="boolean"&&typeof policy.show==="boolean");
  if(!policies.some(policy=>policy.name==="Codex"))return new Set(["Codex"]);
  return new Set(policies.filter(policy=>policy.collect&&policy.show).map(policy=>policy.name));
}
function restrictSnapshotProviders(snapshot) {
  if(!snapshot||!Array.isArray(snapshot.providers))return snapshot;
  const visible=visibleProviderNames(snapshot);
  snapshot.providers=snapshot.providers.filter(provider=>provider&&visible.has(provider.name));
  if(Array.isArray(snapshot.enabledProviders))snapshot.enabledProviders=snapshot.enabledProviders.filter(name=>visible.has(name));
  if(Array.isArray(snapshot.limitProviders))snapshot.limitProviders=snapshot.limitProviders.filter(name=>visible.has(name));
  return snapshot;
}
function syncProviderPolicyGeneration(snapshot) {
  const metadata=policyForSources(snapshot);
  const generation=metadata?.schemaVersion===1&&typeof metadata.generation==="string"&&metadata.generation?metadata.generation:"fallback-codex-only";
  if(state.providerPolicyGeneration!==generation){
    if(state.providerPolicyGeneration!==null||state.watcher){
      state.watcher=null;state.watcherFetchError=null;watcherAcceptedQuota.clear();state.watcherAcceptedSequence.clear();
      watcherLoader.sequence++;watcherLoader.successSequence=watcherLoader.sequence;watcherLoader.errorSequence=watcherLoader.sequence;
    }
    state.providerPolicyGeneration=generation;
  }
}
function loadProviderPolicy() {
  if(!RUNWAY||PHONE)return Promise.resolve(false);
  if(providerPolicyLoader.promise)return providerPolicyLoader.promise;
  const operation=(async()=>{
    let policy=null;
    try{
      const response=await fetch('/api/provider-policy',{cache:'no-store',signal:AbortSignal.timeout(5000)});
      if(response.ok){const value=await response.json();if(validPublicProviderPolicy(value))policy=value;}
    }catch{}
    state.providerPolicy=policy;state.providerPolicyReady=true;
    if(policy&&quotaBootstrap.samplePayload)applyQuotaSamples(quotaBootstrap.samplePayload,quotaBootstrap.sampleSequence,quotaBootstrapTargets(),{requireDated:true});
    if(state.snapshot){
      syncProviderPolicyGeneration(state.snapshot);
      restrictSnapshotProviders(state.snapshot);
      if(!sourceNames(state.snapshot).includes(state.source))state.source=effectiveSource(state.snapshot);
      tick();
    }else{updateSourceSwitch();renderQuotaState();}
    return Boolean(policy);
  })();
  let shared;shared=operation.finally(()=>{if(providerPolicyLoader.promise===shared)providerPolicyLoader.promise=null;});
  providerPolicyLoader.promise=shared;return shared;
}
// Sources the switch can offer: visible chat providers under policy, in a fixed order.
function sourceNames(snapshot) { const visible=visibleProviderNames(snapshot);return SOURCES.filter(name=>visible.has(name)); }
function effectiveSource(snapshot) { const names=sourceNames(snapshot);return names.includes(state.source)?state.source:(names[0]||"Codex"); }
const runwaySource = snapshot => RUNWAY&&!PHONE?sourceNames(snapshot):["Codex"];
function providersFor(snapshot) {
  const visible=visibleProviderNames(snapshot),available=(snapshot?.providers||[]);
  const names=RUNWAY?runwaySource(snapshot):(snapshot?.enabledProviders||["Codex"]);
  return names.filter(name=>visible.has(name)).map(name=>available.find(p=>p.name===name)||{name,connected:false,chats:[]});
}
// Chat providers plus providers shown only as quota meters (reset block and alerts).
function limitProvidersFor(snapshot) {
  const visible=visibleProviderNames(snapshot),available=snapshot?.providers||[];
  const enabled=RUNWAY?runwaySource(snapshot):(snapshot?.enabledProviders||["Codex"]);
  if(RUNWAY&&!PHONE){
    const names=sourceNames(snapshot);
    return names.flatMap(name=>{
      const provider=available.find(item=>item.name===name),bootstrap=quotaBootstrap.providers[name];
      const hasQuota=item=>Boolean(item&&["fiveHour","weekly"].some(key=>{
        const value=item[key];return value&&typeof value==="object"&&(safeNumber(value.percent)!=null||resetAt(value,item.limitsUpdatedAt)!=null);
      }));
      const selected=hasQuota(provider)?provider:hasQuota(bootstrap)?bootstrap:null;
      return selected?[selected]:[];
    });
  }
  const names=[...enabled,...(RUNWAY?[]:(snapshot?.limitProviders||[]).filter(name=>!enabled.includes(name)))].filter(name=>visible.has(name));
  return [...new Set(names)].map(name=>{
    const provider=available.find(item=>item.name===name);if(provider)return provider;
    return {...(quotaBootstrap.providers[name]||{name,connected:false,chats:[]}),name,limitsOnly:!enabled.includes(name)};
  });
}
function snapshotParentKey(provider, raw, edges=null) {
  const key=typeof raw.key==="string"&&raw.key===provider.name+":"+raw.id?raw.key:provider.name+":"+raw.id;
  const nativeParent=raw.parentId!=null?provider.name+":"+raw.parentId:null;
  const rowParent=typeof raw.parentKey==="string"&&raw.parentKey?raw.parentKey:null;
  const localRowParent=rowParent?.startsWith(provider.name+":")?rowParent:null;
  if(edges&&Object.prototype.hasOwnProperty.call(edges,key)){
    const edge=edges[key];
    if(typeof edge==="string"&&edge)return edge.startsWith(provider.name+":")?(nativeParent||localRowParent||edge):edge;
    return nativeParent||localRowParent;
  }
  return rowParent||nativeParent;
}
function buildForest(providers, graph=undefined) {
  if(!Array.isArray(providers)&&Array.isArray(providers?.providers)){graph=providers.graph;providers=providers.providers;}
  const graphValue=graph,edges=graphValue&&typeof graphValue.edges==="object"&&!Array.isArray(graphValue.edges)?graphValue.edges:null;
  const map=new Map();
  for(const provider of providers)for(const raw of provider.chats||[]) {
    const chat={...raw,provider:provider.name,status:normalizeStatus(raw.status),children:[]};
    if(RUNWAY)chat.sourceStatus=raw.status;
    if(raw.statusSource==="unavailable"){chat.status="unknown";chat.telemetryUnavailable=true;}
    const fallbackKey=keyOf(chat),validKey=typeof raw.key==="string"&&raw.key===fallbackKey?raw.key:fallbackKey;
    chat.key=validKey;
    const parent=snapshotParentKey(provider,raw,edges);
    chat.treeParentKey=typeof parent==="string"&&parent?parent:null;
    map.set(chat.key,chat);
  }
  const roots=[];
  for(const chat of map.values()) {
    const parent=chat.treeParentKey?map.get(chat.treeParentKey):null;
    // Malformed lineage must not hide a task or create a recursive render.
    let ancestor=parent, cycle=false;const seen=new Set([chat.key]);
    while(ancestor){if(seen.has(ancestor.key)){cycle=true;break;}seen.add(ancestor.key);ancestor=ancestor.treeParentKey?map.get(ancestor.treeParentKey):null;}
    if(parent && !cycle)parent.children.push(chat);else roots.push(chat);
  }
  const today=localISODate();
  function aggregate(chat, root, depth=0) {
    chat.root=root;chat.depth=depth;chat.descendants=[];
    chat.todayTokens=chat.dailyTokens?safeNumber(chat.dailyTokens[today])??0:null;
    chat.latest=Math.max(stamp(chat.updatedAt),stamp(chat.usageUpdatedAt));
    for(const child of chat.children){aggregate(child,root,depth+1);chat.descendants.push(child,...child.descendants);chat.latest=Math.max(chat.latest,child.latest);}
    chat.parentTitle=map.get(chat.treeParentKey)?.title;
    chat.highestContext=Math.max(-1,...[chat,...chat.descendants].map(c=>safeNumber(c.contextPercent)??-1));
    chat.priority=Math.min(...[chat,...chat.descendants].map(c=>c.pendingQuestion?0:statusRank(c.status)));
    const list=$('#chat-list'),interacting=state.pointerInList||list?.contains?.(document.activeElement)||state.popover?.dataset?.action==='runway-question';
    if(!RUNWAY||!interacting)chat.children.sort((a,b)=>b.latest-a.latest);
  }
  roots.forEach(root=>aggregate(root,root));
  return {roots,rows:[...map.values()]};
}
function stabilizeRunwayForest(forest, previous) {
  const container=$('#chat-list'),interacting=state.pointerInList||container?.contains?.(document.activeElement)||state.popover?.dataset?.action==='runway-question';
  if(!RUNWAY||!interacting||!previous)return;
  const oldRows=new Map(previous.rows.map(chat=>[chat.key,chat]));
  for(const parent of forest.rows){
    if(parent.children.length<2)continue;
    const old=oldRows.get(parent.key),rank=new Map((old?.children||[]).map((child,index)=>[child.key,index]));
    parent.children.sort((a,b)=>{
      const ai=rank.has(a.key)?rank.get(a.key):Infinity,bi=rank.has(b.key)?rank.get(b.key):Infinity;
      return ai-bi||b.latest-a.latest;
    });
  }
  const visit=chat=>{chat.descendants=[];for(const child of chat.children){visit(child);chat.descendants.push(child,...child.descendants);}};
  forest.roots.forEach(visit);
}
// Search: plain text over title, agent, model, workspace, plus is:needs / is:running / is:failed.
function parseQuery(text) {
  const flags=new Set(),words=[];
  for(const part of String(text||"").trim().toLowerCase().split(/\s+/).filter(Boolean)) {
    const m=part.match(/^is:(needs|running|failed|hidden)$/);
    if(m)flags.add(m[1]);else words.push(part);
  }
  return {flags,text:words.join(" "),active:Boolean(flags.size||words.length)};
}
function matchesQuery(chat, query) {
  if(!query.active)return true;
  if(query.flags.has("needs")&&!isAsking(chat))return false;
  if(query.flags.has("running")&&chat.status!=="running")return false;
  if(query.flags.has("failed")&&chat.status!=="failed")return false;
  if(query.flags.has("hidden")&&!state.hiddenChats.has(chat.key))return false;
  return !query.text||[chat.title,agentLabel(chat),chat.model,modelLabel(chat),chat.workspace,...(RUNWAY?[chat.assignment,chat.objective,chat.latestRequest,chat.latestUpdate]:[])].join(" ").toLowerCase().includes(query.text);
}
const inWindow = (chat, days) => Date.now()-chat.latest<=days*86400000;
// Latest tree-wide activity first. Pins keep older chats in view but never reorder the timeline.
function visibleRoots(forest, days=state.windowDays) {
  const query=parseQuery(state.query);
  const byLatest=[...forest.roots].sort((a,b)=>b.latest-a.latest);
  if(RUNWAY)return byLatest.filter(chat=>{
    const rows=[chat,...chat.descendants],matches=!query.active||rows.some(row=>matchesQuery(row,query));
    const hiddenMatch=query.flags.has("hidden")&&rows.some(row=>state.hiddenChats.has(row.key)&&matchesQuery(row,query));
    return (PHONE||state.showHidden||!state.hiddenChats.has(chat.key)||hiddenMatch)&&
      (hiddenMatch||rows.some(runwayVisibleActive))&&matches;
  });
  const keep=chat=>query.active ? [chat,...chat.descendants].some(c=>matchesQuery(c,query)) : state.pins.has(chat.key)||(RUNWAY&&state.showHidden&&state.hiddenChats.has(chat.key))||inWindow(chat,days);
  return byLatest.filter(chat=>(!RUNWAY||state.showHidden||!state.hiddenChats.has(chat.key))&&keep(chat));
}
function nextWindowWithChats(forest) {
  for(const days of WINDOWS.filter(d=>d>state.windowDays)) {
    const count=forest.roots.filter(c=>inWindow(c,days)).length;
    if(count)return {days,count};
  }
  return null;
}
// Prompt-cache warmth from idle time since the last recorded request, using horizons learned from this user's resumes.
function cacheWarmth(chat, providers=state.snapshot?.providers||[]) {
  const model=providers.find(p=>p.name===chat.provider)?.cacheWarmth;
  const warm=safeNumber(model?.warmSeconds),cold=safeNumber(model?.coldSeconds),last=stamp(chat.usageUpdatedAt);
  if(warm==null||cold==null||!last)return null;
  const idle=Math.max(0,(Date.now()-last)/1000);
  if(chat.status==="running"||idle<warm)return {level:"warm",left:chat.status==="running"?warm:warm-idle,idle};
  if(idle<cold)return {level:"cooling",left:cold-idle,idle};
  return {level:"cold",idle};
}
// Row warmth column: running → run timer; warm → "22m"; last 10 min → "7:52" amber; past warm → "cold?"; cold → nothing.
function warmthView(chat) {
  if(chat.status==="running")return {kind:"running"};
  const warmth=cacheWarmth(chat);
  if(!warmth)return {kind:"none"};
  if(warmth.level==="warm")return warmth.left<=COOLING_SECONDS
    ? {kind:"cooling",left:warmth.left,text:mmss(warmth.left),fraction:warmth.left/COOLING_SECONDS}
    : {kind:"warm",left:warmth.left,text:Math.ceil(warmth.left/60)+"m"};
  if(warmth.level==="cooling")return {kind:"maybe",text:"cold?"};
  return {kind:"cold"};
}
function runInfo(chat) {
  const execution=RUNWAY?runwayExecutionView(chat):null;
  if(execution){
    const start=stamp(chat.executionSince);
    return start&&start<=Date.now()?{seconds:Math.max(0,(Date.now()-start)/1000),live:true}:null;
  }
  if(RUNWAY&&chat.executionStatus!==undefined)return null;
  const start=stamp(RUNWAY&&agentLive(chat)?chat.activeRunStartedAt||chat.runStartedAt:chat.runStartedAt);
  if(!start)return null;
  const live=agentLive(chat)&&!chat.runEndedAt;
  const end=live?Date.now():stamp(chat.runEndedAt)||chat.latest||stamp(chat.updatedAt)||start;
  return {live,seconds:Math.max(0,(end-start)/1000)};
}

/* ---------- attention strip (layer 0) ---------- */
const KIND_ORDER = {auth:0,provider:1,offline:1,ask:2,cooling:3,failed:4,context:5,watcher:6};
const pathTitle = chat => hasTreeParent(chat) ? (chat.root?.title||chat.parentTitle||"Chat")+" › "+agentLabel(chat) : chat.title||"Untitled";
// Stable while polling, scoped to this chat and this exact prompt. Never use
// updatedAt: transcript activity can change while the same question is pending.
function questionDismissToken(chat) {
  return "question:"+chat.key+":"+JSON.stringify([
    chat.pendingQuestionId??chat.questionId??null,
    chat.pendingQuestion||"",chat.pendingOptions||[],chat.pendingQuestionAt||null,chat.attentionSince||null
  ]);
}
function clearResolvedQuestionDismissals(chat) {
  const prefix="question:"+chat.key+":";let changed=false;
  for(const token of state.dismissed)if(typeof token==="string"&&token.startsWith(prefix)){state.dismissed.delete(token);changed=true;}
  if(changed)saveDismissed();
}
function attentionItems(forest, providers, watcher=state.watcher, now=Date.now()) {
  const items=[],rows=forest?.rows||[],wp=watcher?.providers||{};
  for(const p of providers) {
    const w=wp[p.name]||{};
    const unconfirmed=!w.failedAttempts&&w.unverifiedAttempts>0&&/no new reset deadline was observed/i.test(w.lastError||"");
    const auth=w.needsLogin||w.phase==="needs_login"||p.connected===false&&/auth|log.?in|sign.?in|401|unauthori[sz]ed|expired/i.test(p.error||"");
    if(auth)items.push({key:"auth:"+p.name,kind:"auth",tone:"attn",label:"Sign-in expired",title:p.name,copy:p.name.toLowerCase()+" login",sort:0});
    else if(p.connected===false)items.push({key:"provider:"+p.name,kind:"provider",tone:"err",label:"Unavailable",title:p.name,tip:p.error||"",sort:0});
    else if(watcher?.enabled&&(!watcher.running||watcher.persistenceError||(w.lastError&&!unconfirmed)||w.lastPollError||w.failedAttempts>0)) {
      const retry=safeNumber(w.nextRetryAt);
      const label=!watcher.running?"Auto-greet stopped":watcher.persistenceError?"Tracker state not saved":w.lastPollError?"Quota check stale":"Auto-greet failed";
      items.push({key:"watcher:"+p.name,kind:"watcher",tone:"err",label,title:providers.length>1?p.name:"",fact:retry?"retry "+clock(retry*1000):"",tip:watcher.persistenceError||w.lastPollError||w.lastError||"",sort:0});
    }
  }
  if(state.failSince&&now-state.failSince>OFFLINE_AFTER_MS) {
    // One outage, one item: "Offline" already covers a provider that is unavailable because of it.
    for(let i=items.length-1;i>=0;i--)if(items[i].kind==="provider")items.splice(i,1);
    items.push({key:"offline",kind:"offline",tone:"err",label:"Offline",title:"",fact:state.lastOkAt?"data "+ageShort(state.lastOkAt)+" old":"no data",sort:0});
  }
  for(const chat of rows) {
    const target=(chat.root||chat).key,sub=hasTreeParent(chat)?chat.key:"";
    if(RUNWAY&&!state.showHidden&&state.hiddenChats.has(target))continue;
    if(RUNWAY&&runwayTerminated(chat))continue;
    if(isAsking(chat)) {
      const since=stamp(chat.attentionSince)||stamp(chat.pendingQuestionAt)||stamp(chat.updatedAt);
      const dismiss=questionDismissToken(chat);
      if(!state.dismissed.has(dismiss))items.push({key:"ask:"+chat.key,kind:"ask",tone:"attn",label:"Needs you",title:pathTitle(chat),fact:since?ageShort(since):"",line2:chat.pendingQuestion||"",options:chat.pendingOptions||[],thread:isCodexChat(chat)?chat.id:null,target,sub,dismiss,sort:since||now});
      continue;
    }
    // Only a positively observed resolved chat clears its prompt history.
    // A chat absent from the selected window must keep its stored dismissal.
    const providerUnavailable=providers.some(provider=>provider.name===chat.provider&&provider.connected===false);
    if(["idle","running","compacting","quiet","failed"].includes(chat.status)&&chat.statusSource!=="unavailable"&&!chat.dataError&&!providerUnavailable)
      clearResolvedQuestionDismissals(chat);
    if(recentFail(chat)) {
      const dismiss="failed:"+chat.key+":"+chat.updatedAt;
      if(!state.dismissed.has(dismiss))items.push({key:"failed:"+chat.key,kind:"failed",tone:"err",label:chat.stateDetail==="interrupted"?"Stopped":"Failed",title:pathTitle(chat),fact:ageShort(stamp(chat.updatedAt)),tip:chat.failureReason||chat.stateDetail||"",target,sub,...(RUNWAY&&isCodexChat(chat)?{thread:chat.id}:{}),dismiss,sort:-stamp(chat.updatedAt)});
    }
    const pct=safeNumber(chat.contextPercent);
    if(pct!=null&&pct>=90&&now-chat.latest<ACTIVE_WINDOW_MS) {
      const dismiss="context:"+chat.key+":"+(chat.compactions||0);
      const left=safeNumber(chat.contextWindow)!=null&&safeNumber(chat.contextTokens)!=null?fmt(Math.max(0,chat.contextWindow-chat.contextTokens))+" left":"";
      if(!state.dismissed.has(dismiss))items.push({key:"context:"+chat.key,kind:"context",tone:"err",label:"Usage",labelMono:Math.round(pct)+"%",title:pathTitle(chat),fact:left,target,sub,...(RUNWAY&&isCodexChat(chat)?{thread:chat.id}:{}),dismiss,sort:-pct});
    }
    if(!RUNWAY&&!chat.parentId) {
      const warmth=warmthView(chat),penalty=coldPenalty(chat);
      const dismiss="cooling:"+chat.key+":"+chat.usageUpdatedAt;
      if(warmth.kind==="cooling"&&safeNumber(chat.contextTokens)>=COOLING_MIN_TOKENS&&!state.dismissed.has(dismiss))
        items.push({key:"cooling:"+chat.key,kind:"cooling",tone:"attn",label:"Cold in",labelMono:warmth.text,title:chat.title||"Untitled",fact:"+"+fmt(penalty)+" if cold",target,dismiss,ring:warmth.fraction,sort:warmth.left});
    }
  }
  return items.sort((a,b)=>KIND_ORDER[a.kind]-KIND_ORDER[b.kind]||a.sort-b.sort);
}
function stripItemHTML(item) {
  const glyph=item.kind==="cooling"?ringHTML(item.ring,"sg"):'<i class="sg"></i>';
  const dismissLabel=item.kind==="ask"?"Hide this question in widget":"Dismiss";
  return '<div class="si-in"><div class="si-line"><span class="sgw">'+glyph+ICON.check.replace('class="ic ic-check"','class="ic si-check"')+'</span>'+
    '<span class="si-label">'+esc(item.label)+(item.labelMono?' <b>'+esc(item.labelMono)+'</b>':'')+'</span>'+
    '<span class="si-title">'+esc(item.title)+'</span><span class="si-fact">'+esc(item.fact||"")+'</span>'+
    (item.copy?'<button class="chip" data-action="copy" data-copy="'+esc(item.copy)+'" title="Copy">'+esc(item.copy)+ICON.copy+ICON.check+'</button>':'')+
    (item.dismiss?'<button class="icon-btn si-x" data-action="dismiss" data-dismiss="'+esc(item.dismiss)+'" aria-label="'+dismissLabel+'" title="'+dismissLabel+'">'+ICON.close+'</button>':'')+
    '</div>'+(item.kind==="ask"?askHTML(item):item.line2?'<div class="si-q">'+richText(item.line2)+'</div>':'')+
    (RUNWAY&&item.sub&&item.thread&&item.kind!=='ask'?'<div class="runway-alert-actions"><button class="q-open" data-action="open" data-thread="'+esc(item.thread)+'">'+ICON.open+'Open agent'+ICON.check+'</button></div>':'')+'</div>';
}
// A free-text question keeps its action on the question line; only answer choices get a chip row.
function askHTML(item) {
  if(RUNWAY) {
    const expanded=state.expandedQuestions.has(item.key);
    const summary='<button class="runway-question-toggle" data-action="toggle-question" data-question="'+esc(item.key)+'" aria-expanded="'+expanded+'"><span class="runway-question-title">'+(item.line2?richText(item.line2):'Waiting for your input')+'</span><span class="runway-expand-label">'+(expanded?'Hide':'Details')+'</span></button>';
    if(!expanded)return '<div class="runway-question">'+summary+'</div>';
    const open=item.thread?'<button class="q-open" data-action="open" data-thread="'+esc(item.thread)+'" title="Open this chat in Codex">'+ICON.open+'<span>Open in Codex</span>'+ICON.check+'</button>':'';
    const choices=item.thread?((item.options||[]).length?optionsHTML(item.options,item.thread):open):plainOptionsHTML(item.options);
    return '<div class="runway-question">'+summary+'<div class="runway-question-detail"><div class="runway-question-full">'+(item.line2?richText(item.line2):'Waiting for your input')+'</div>'+choices+'</div></div>';
  }
  const open=item.thread?'<button class="q-open" data-action="open" data-thread="'+esc(item.thread)+'" title="Open this chat in Codex">'+ICON.open+'<span>'+(item.options?.length?"Open":"Answer")+'</span>'+ICON.check+'</button>':'';
  const question='<div class="si-q si-ask"><span class="si-qt">'+(item.line2?richText(item.line2):'Waiting for your input')+'</span>'+(item.options?.length?'':open)+'</div>';
  return question+(item.options?.length?'<div class="si-opts">'+(item.thread?optionsHTML(item.options,item.thread):plainOptionsHTML(item.options))+'</div>':'');
}
function renderStrip(items) {
  const node=$("#strip");if(!node)return;
  if(RUNWAY){node.replaceChildren?.();node.innerHTML="";node.hidden=true;node.classList?.remove("open");return;}
  node.hidden=false;
  const limit=RUNWAY?1:3,over=items.length-limit;
  const entries=items.map((item,i)=>({key:item.key,cls:"si t-"+item.tone+" k-"+item.kind+(i>=limit&&!state.stripOpen?" over":""),
    attrs:{role:item.target?"button":null,tabindex:item.target?"0":null,"data-action":item.target?"jump":null,"data-target":item.target||null,"data-sub":item.sub||null,title:item.tip||item.line2||item.title||null},
    html:stripItemHTML(item),item}));
  if(over>0)entries.push({key:"__more",cls:"si si-more-row",attrs:{},html:'<div class="si-in"><button class="si-more" data-action="strip-more" aria-expanded="'+state.stripOpen+'">'+ICON.chevron+(state.stripOpen?"Show less":"+"+over+" more")+'</button></div>'});
  patchKeyed(node,entries,{enter:stripEnter,exit:stripExit});
  node.classList?.toggle("open",state.stripOpen&&over>0);
}
const reducedMotion = () => Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);
function clearNotchOpening() {
  clearTimeout(state.notchOpeningTimer);state.notchOpeningTimer=null;
  document.body?.classList?.remove?.('notch-opening');
}
function beginNotchOpening() {
  clearNotchOpening();
  if(reducedMotion())return;
  const body=document.body;if(typeof body?.classList?.add!=='function')return;
  for(const row of document.querySelectorAll?.('.runway-row-enter')||[])clearRunwayArrival(row);
  body.classList.add('notch-opening');
  let milliseconds=COMPACT?383:331;
  try{
    const raw=window.getComputedStyle?.(body)?.getPropertyValue?.('--notch-open-suppression')?.trim();
    const match=raw?.match(/^(\d+(?:\.\d+)?)(ms|s)$/);
    if(match)milliseconds=Number(match[1])*(match[2]==='s'?1000:1);
  }catch{}
  state.notchOpeningTimer=setTimeout(()=>{state.notchOpeningTimer=null;body.classList.remove?.('notch-opening');},milliseconds);
}
function stripEnter(el) {
  if(reducedMotion())return;
  el.classList.add("pre");void el.offsetHeight;el.classList.remove("pre");el.classList.add("arrive");
  setTimeout(()=>el.classList.remove("arrive"),420);
}
function stripExit(el, entry) {
  if(reducedMotion()||el._dismissed){el.remove();return;}
  const chat=entry?.item?.target&&state.forest?.rows.find(c=>c.key===entry.item.target);
  const wentCold=entry?.item?.kind==="cooling"&&chat&&["maybe","cold"].includes(warmthView(chat).kind);
  if(wentCold){el.classList.add("cold");const label=el.querySelector(".si-label");if(label)label.textContent="Cold";}
  else el.classList.add("resolved");
  setTimeout(()=>{el.classList.add("pre","leaving");setTimeout(()=>el.remove(),200);},wentCold?3000:600);
}

/* ---------- keyed DOM patching (falls back to innerHTML without a real DOM, e.g. tests) ---------- */
const realDom = node => Boolean(node&&node.children&&typeof document.createElement==="function");
function attrString(attrs) { return Object.entries(attrs||{}).filter(([,v])=>v!=null).map(([k,v])=>' '+k+'="'+esc(v)+'"').join(""); }
function syncPatchedAttributes(current,next) {
  const oldEvent=current.getAttribute?.('data-model-change-at'),newEvent=next.getAttribute?.('data-model-change-at');
  if(RUNWAY&&!PHONE&&oldEvent&&newEvent&&oldEvent!==newEvent&&current.matches?.('.runway-model-name.model-changed')&&next.matches?.('.runway-model-name.model-changed')){current.classList.remove('model-changed');void current.offsetWidth;}
  const message=current.matches?.('.runway-message-indicator')&&next.matches?.('.runway-message-indicator');
  const sameMessage=message&&current.getAttribute('data-message-at')===next.getAttribute('data-message-at');
  if(message&&!sameMessage&&state.runwaySnapshotPainted)runwayMotionOnce(current,'message-arrived','runway-message-dot-arrival','.runway-message-dots i:nth-child(3)');
  for(const attr of [...current.attributes])if(!next.hasAttribute(attr.name))current.removeAttribute(attr.name);
  for(const attr of [...next.attributes]){
    if(sameMessage&&attr.name==='style')continue;
    if(current.getAttribute(attr.name)!==attr.value)current.setAttribute(attr.name,attr.value);
  }
}
function animateInsertedMessageBadges(el) {
  if(!RUNWAY||PHONE||!state.runwaySnapshotPainted||state.sourceSwitching)return;
  const messages=[...(el.matches?.('.runway-message-indicator')?[el]:[]),...(el.querySelectorAll?.('.runway-message-indicator')||[])];
  for(const message of messages)runwayMotionOnce(message,'message-arrived','runway-message-dot-arrival','.runway-message-dots i:nth-child(3)');
}
// Patch recorded text in place so polling does not replace the focused agent action.
function runwayPatchContent(el, html) {
  if(!realDom(el)){el.innerHTML=html;return;}
  const template=document.createElement('template');template.innerHTML=html;
  function patch(parent, source) {
    const wanted=[...source.childNodes];
    const keyed=wanted.some(node=>node.nodeType===1&&node.dataset?.runwaySlot)||[...parent.childNodes].some(node=>node.nodeType===1&&node.dataset?.runwaySlot);
    if(keyed){
      let cursor=parent.firstChild;
      for(const next of wanted){
        const slot=next.nodeType===1?next.dataset?.runwaySlot:null;
        let current=slot?[...parent.children].find(node=>node.dataset?.runwaySlot===slot):cursor;
        if(!current){current=next.cloneNode(true);parent.insertBefore(current,cursor);animateInsertedMessageBadges(current);}
        else {if(current.nodeType!==next.nodeType||current.nodeName!==next.nodeName){const replacement=next.cloneNode(true);parent.replaceChild(replacement,current);current=replacement;}else {
          if(next.nodeType===1)syncPatchedAttributes(current,next);
          patch(current,next);
        }if(current!==cursor)parent.insertBefore(current,cursor);}
        cursor=current.nextSibling;
      }
      while(cursor){const next=cursor.nextSibling;cursor.remove();cursor=next;}
      return;
    }
    for(let i=0;i<wanted.length;i++) {
      const next=wanted[i];let current=parent.childNodes[i];
      if(!current){current=next.cloneNode(true);parent.appendChild(current);animateInsertedMessageBadges(current);continue;}
      if(current.nodeType!==next.nodeType||current.nodeName!==next.nodeName){parent.replaceChild(next.cloneNode(true),current);continue;}
      if(next.nodeType===3){if(current.nodeValue!==next.nodeValue)current.nodeValue=next.nodeValue;continue;}
      if(next.nodeType!==1)continue;
      syncPatchedAttributes(current,next);
      patch(current,next);
    }
    while(parent.childNodes.length>wanted.length)parent.lastChild.remove();
  }
  patch(el,template.content);
}
function patchKeyed(container, entries, {enter,exit}={}) {
  if(!container)return;
  if(!realDom(container)) {
    const html=entries.map(e=>'<div class="'+e.cls+'" data-key="'+esc(e.key)+'"'+attrString(e.attrs)+'>'+e.html+'</div>').join("");
    if(container.innerHTML!==html)container.innerHTML=html;
    return;
  }
  const existing=new Map();
  for(const el of [...container.children]) {
    if(el._leaving)continue;
    if(el.dataset.key==null){el.remove();continue;}
    existing.set(el.dataset.key,el);
  }
  const wanted=new Set(entries.map(e=>e.key));
  for(const [key,el] of existing)if(!wanted.has(key)){el._leaving=true;clearRunwayArrival(el);exit?exit(el,el._entry):el.remove();}
  let prev=null;
  for(const entry of entries) {
    let el=existing.get(entry.key),fresh=false;
    if(!el){el=document.createElement("div");el.dataset.key=entry.key;fresh=true;}
    if(el.className!==entry.cls)el.className=entry.cls;
    for(const [k,v] of Object.entries(entry.attrs||{})){if(v==null)el.removeAttribute(k);else if(el.getAttribute(k)!==String(v))el.setAttribute(k,v);}
    if(el._html!==entry.html){if(RUNWAY&&entry.cls.includes('runway-agent'))runwayPatchContent(el,entry.html);else el.innerHTML=entry.html;el._html=entry.html;syncBreath(el);}
    el._entry=entry;
    const next=prev?prev.nextSibling:container.firstChild;
    if(next!==el)container.insertBefore(el,next);
    prev=el;
    if(fresh&&enter)enter(el);
  }
}
// Re-created "live" glyphs join the global breathing phase instead of restarting it.
function syncBreath(el) {
  if(typeof performance==="undefined")return;
  const offset=-(performance.now()%1600)+"ms";
  el.querySelectorAll?.(".breathe").forEach(g=>{g.style.animationDelay=offset;});
}

/* ---------- reset block ---------- */
function watcherTone(watcher, name) {
  const w=watcher?.providers?.[name];
  if(!watcher||!w)return {tone:"mute",tip:RUNWAY?"":"Auto-greet unavailable"};
  if(!watcher.enabled||!watcher.running)return {tone:"mute",tip:"Auto-greet off"};
  const next=safeNumber(w.nextGreetingAt),last=stamp(w.lastTriggerAt),verified=w.verificationEvidence==="usage_and_deadline"&&last&&stamp(w.lastVerifiedAt)>=last;
  const tip=RUNWAY?["Auto-greet",next?clock(next*1000):null,last?"last "+clock(last)+(verified?" ✓":""):null].filter(Boolean).join(" · "):"Auto-greet "+(next?clock(next*1000):"—")+(last?" · last "+clock(last)+(verified?" ✓":""):"");
  if(w.needsLogin||w.phase==="needs_login"||w.lastPollError||w.failedAttempts>0)return {tone:"err",tip};
  if(w.lastError){
    if(w.unverifiedAttempts>0&&/no new reset deadline was observed/i.test(w.lastError))return {tone:"attn",tip:tip+" · greeting sent, reset unconfirmed"};
    return {tone:"err",tip};
  }
  if(["greeting_verifying","reset_observed_sending"].includes(w.phase))return {tone:"attn",tip};
  return {tone:"live",tip};
}
function resetView(provider, key) {
  const item=provider[key]||{},used=safeNumber(item.percent);
  const quotaAt=provider._quotaWindowObservedAt&&Object.prototype.hasOwnProperty.call(provider._quotaWindowObservedAt,key)?provider._quotaWindowObservedAt[key]:provider.limitsUpdatedAt;
  const sampled=stamp(quotaAt),age=sampled?Date.now()-sampled:null;
  const measured=!RUNWAY||(provider.connected!==false&&used!=null&&sampled&&age!=null&&age>=0&&age<=600000);
  const observedParts=used!=null&&sampled&&age!=null&&age>=0?resetParts(item,quotaAt,key):{day:"",time:"",countdown:"",due:false,target:null,seconds:null};
  const maxAge=key==="weekly"?7*86400000:86400000;
  const lastKnown=Boolean(RUNWAY&&!measured&&used!=null&&sampled&&age!=null&&age>600000&&age<maxAge&&observedParts.target!=null&&observedParts.seconds>0);
  const expired=Boolean(RUNWAY&&!measured&&used!=null&&sampled&&age!=null&&age>600000&&observedParts.target!=null&&observedParts.seconds<=0);
  const shownUsed=(measured||lastKnown)&&used!=null?Math.max(0,Math.min(100,used)):null;
  const parts=measured||lastKnown?observedParts:{day:"",time:"",countdown:"",due:false,target:null,seconds:null};
  const hasReset=Boolean((measured&&parts.target)||(lastKnown&&parts.target&&parts.seconds>0));
  const runwayCountdown=parts.seconds==null?"":parts.seconds<=0?"due":key==="weekly"?(parts.seconds>=86400?Math.floor(parts.seconds/86400)+"d "+Math.floor(parts.seconds%86400/3600)+"h":Math.floor(parts.seconds/3600)+"h "+pad2(Math.floor(parts.seconds%3600/60))+"m"):parts.seconds>=3600?Math.floor(parts.seconds/3600)+"h "+pad2(Math.floor(parts.seconds%3600/60))+"m "+pad2(parts.seconds%60)+"s":parts.seconds>=60?Math.floor(parts.seconds/60)+"m "+pad2(parts.seconds%60)+"s":parts.seconds+"s";
  const usedLabel=expired?"reset":shownUsed==null?"":Math.round(shownUsed)+"%",usedText=expired?"reset":usedLabel?usedLabel+" used":"";
  const lastKnownAge=lastKnown?quotaAgeLabel(sampled)+" ago":"";
  return {measured,lastKnown,lastKnownAge,expired,hasValue:shownUsed!=null,usedPercent:shownUsed,usedLabel,usedText,
    tone:shownUsed==null||lastKnown?"":shownUsed>=70?"err":shownUsed>=50?"attn":"",day:RUNWAY&&!hasReset?"":parts.day,time:RUNWAY&&!hasReset?"":parts.time,countdown:RUNWAY?(hasReset&&(!PHONE||key==="fiveHour")?runwayCountdown:""):parts.countdown,due:parts.due,
    // Keep the last-known age beside each independently rendered quota value.
    stale:RUNWAY?"":key==="fiveHour"&&provider.connected!==false&&used!=null&&(age==null||age>600000)?"sampled "+(sampled?ageShort(sampled)+" ago":"\u2014"):"",
    tip:RUNWAY?(lastKnown?"Last reported by "+(provider.name==="Claude"?"Claude Desktop":provider.name)+" "+lastKnownAge:sampled&&measured?"Quota sample at "+clock(sampled):""):"Quota sampled "+(sampled?ageShort(sampled)+" ago":"\u2014")};
}
function clearRunwayArrival(el) {
  if(!el)return;
  clearTimeout(el._runwayArrivalTimer);el._runwayArrivalTimer=null;
  if(el._runwayArrivalEnd)el.removeEventListener?.('animationend',el._runwayArrivalEnd);
  el._runwayArrivalEnd=null;el.classList?.remove('runway-row-enter');
}
function runwayArrivalEnter(el) {
  if(!el||reducedMotion()||state.sourceSwitching||document.body?.classList?.contains?.('notch-opening')||COMPACT&&Date.now()<state.startupCalmUntil)return;
  clearRunwayArrival(el);el.classList.add('runway-row-enter');
  const finish=event=>{if(event&& (event.target!==el||event.animationName!=='runway-row-arrival'))return;clearRunwayArrival(el);};
  el._runwayArrivalEnd=finish;el.addEventListener?.('animationend',finish);
  el._runwayArrivalTimer=setTimeout(()=>finish(),350);
}
function runwayMotionOnce(el,className,animationName,targetSelector=null) {
  if(!el||reducedMotion()||COMPACT&&Date.now()<state.startupCalmUntil)return;
  el._runwayMotionHandlers||=new Map();
  const previous=el._runwayMotionHandlers.get(className);if(previous){el.removeEventListener?.('animationend',previous.finish);el.removeEventListener?.('animationcancel',previous.cancel);}
  el.classList?.remove(className);void el.offsetWidth;
  const pending=targetSelector?new Set(el.querySelectorAll?.(targetSelector)||[]):null;
  if(pending&&!pending.size){el._runwayMotionHandlers.delete(className);return;}
  let cancel;
  const finish=event=>{
    if(event?.animationName!==animationName)return;
    if(pending){if(!pending.has(event.target))return;pending.delete(event.target);if(pending.size)return;}
    el.classList?.remove(className);el.removeEventListener?.('animationend',finish);el.removeEventListener?.('animationcancel',cancel);el._runwayMotionHandlers.delete(className);
  };
  cancel=event=>{if(event?.animationName!==animationName)return;el.classList?.remove(className);el.removeEventListener?.('animationend',finish);el.removeEventListener?.('animationcancel',cancel);el._runwayMotionHandlers.delete(className);};
  el._runwayMotionHandlers.set(className,{finish,cancel});el.addEventListener?.('animationend',finish);el.addEventListener?.('animationcancel',cancel);el.classList?.add(className);
}
function runwayExit(el) {
  if(!el)return;
  if(reducedMotion()||COMPACT&&Date.now()<state.startupCalmUntil||el.getClientRects&&el.getClientRects().length===0){el.remove();return;}
  el._leaving=true;clearRunwayArrival(el);el.classList?.add('runway-exit');
  const finish=event=>{if(event?.animationName!=='runway-row-exit')return;el.removeEventListener?.('animationend',finish);el.removeEventListener?.('animationcancel',finish);el.remove();if(COMPACT)scheduleNotchSize();};
  el.addEventListener?.('animationend',finish);el.addEventListener?.('animationcancel',finish);
  setTimeout(()=>{if(el.isConnected)finish({animationName:'runway-row-exit'});},250);
}
function renderResets(providers) {
  const node=$("#resets"),weeklyNode=REFINED?$("#weekly-resets"):null;if(!node)return;
  const shape=providers.map(p=>p.name).join("|");
  node.dataset.providerCount=String(providers.length);if(weeklyNode)weeklyNode.dataset.providerCount=String(providers.length);
  if(node.dataset.shape!==shape) {
    node.dataset.shape=shape;
    const markup=key=>providers.map((p,i)=>{
      const id="r"+i+"-"+key,providerClass=p.name==="Claude"?"provider-claude":"provider-codex";
      return '<div id="'+id+'-row" class="rr rr-'+key+' '+providerClass+'"><div class="rr-head"><span class="rr-label">'+(key==="fiveHour"?"5-hour":"Weekly")+'</span>'+
        (key==="fiveHour"&&!PHONE&&p.name==="Codex"?'<button class="greet t-mute" id="'+id+'-greet" data-action="watcher" data-provider="'+esc(p.name)+'" aria-label="Auto-greet status"><i></i></button>':'')+
        '<span class="rr-stale" id="'+id+'-stale"></span><span class="rr-left" id="'+id+'-left">'+(RUNWAY?'<span class="rr-value"><b>\uFFFD</b><i>used</i></span>':'\uFFFD')+'</span></div>'+
        '<div class="rr-moment"><span class="rr-day" id="'+id+'-day"></span><span class="rr-time" id="'+id+'-time">\uFFFD:\uFFFD</span><span class="rr-cd" id="'+id+'-cd"></span></div>'+
        '<div class="meter" role="progressbar" aria-label="'+(key==="fiveHour"?"5-hour":"weekly")+' limit used" aria-valuemin="0" aria-valuemax="100" id="'+id+'-meter"><i id="'+id+'-bar"></i></div></div>';
    }).join("");
    if(REFINED){node.innerHTML=markup("fiveHour");if(weeklyNode)weeklyNode.innerHTML=markup("weekly");}
    else node.innerHTML=markup("fiveHour")+markup("weekly");
  }
  providers.forEach((p,i)=>["fiveHour","weekly"].forEach(key=>{
    const id="#r"+i+"-"+key,view=resetView(p,key),row=$(id+"-row");if(row)row.title=view.tip;
    const set=(suffix,text)=>{const el=$(id+suffix);if(el&&el.textContent!==text)el.textContent=text;};
    if(RUNWAY){const left=$(id+"-left");if(left){const html=view.hasValue?'<span class="rr-value"><b>'+esc(view.usedLabel)+'</b><i>'+esc(view.lastKnown?view.lastKnownAge.split(' ')[0]+' ago':'used')+'</i></span>':view.expired?'<span class="rr-reset-label">reset</span>':'<span class="rr-value"><b></b><i></i></span>';if(left.innerHTML!==html)left.innerHTML=html;left.title=view.tip;}}
    else set("-left",view.usedText);
    set("-day",view.day);set("-time",view.time);set("-cd",view.countdown);set("-stale",view.stale);
    const left=$(id+"-left");if(left){left.className="rr-left"+(view.lastKnown?" last-known":"")+(view.tone?" t-"+view.tone:"");left.title=view.tip;}
    const cd=$(id+"-cd");if(cd){cd.className="rr-cd"+(view.due?" t-attn":"");if(RUNWAY){const countdownText=view.countdown||'';if(cd.textContent!==countdownText)cd.textContent=countdownText;cd.title=countdownText?'Resets '+view.day+' '+view.time:'';cd.setAttribute?.('aria-hidden',String(!countdownText));}}
    const meter=$(id+"-meter");if(meter){meter.className="meter"+(view.lastKnown?" last-known":"");if(!view.hasValue){meter.removeAttribute?.('aria-valuenow');meter.removeAttribute?.('aria-valuetext');meter.setAttribute?.('aria-hidden','true');}else{meter.setAttribute("aria-valuenow",String(Math.round(view.usedPercent)));meter.setAttribute?.('aria-valuetext',(view.lastKnown?"Last known: ":"")+Math.round(view.usedPercent)+"% used");meter.removeAttribute?.('aria-hidden');}}
    const bar=$(id+"-bar");if(bar){bar.style.width=(view.usedPercent??0)+"%";bar.className=view.lastKnown?"last-known":view.tone?"t-"+view.tone:"";}
    if(key==="fiveHour"&&!PHONE){const dot=$(id+"-greet"),w=watcherTone(state.watcher,p.name);if(dot){dot.className="greet t-"+w.tone;dot.title=w.tip;}}
  }));
  if(RUNWAY&&!PHONE)renderGreeterStatus(providers);
}
function renderGreeterStatus(providers) {
  const node=$("#runway-greeter-status");if(!node)return;
  const provider=providers.find(p=>p.name==="Codex"),watcher=state.watcher,w=watcher?.providers?.Codex;
  if(!provider||!watcher||!w){node.textContent="";node.className="greeter-status muted";return;}
  const error=watcher.persistenceError||w.lastPollError||w.lastError;
  const attempted=stamp(w.lastAttemptAt||w.lastTriggerAt);
  let label;
  if(w.needsLogin||w.phase==="needs_login")label="Sign-in needed";
  else if(error)label=watcher.persistenceError?"Greeter state not saved":w.lastPollError?"Quota check error":"Greeter error";
  else if(!watcher.enabled||!watcher.running)label="Greeter stopped";
  else if(attempted)label="Last attempt "+runwayActivityAge({latest:attempted});
  else label="";
  node.textContent=label;node.className="greeter-status"+(w.needsLogin||error?" error":"");
  node.title=(error?String(error)+" · ":"")+(attempted?"Last greeting attempt at "+clock(attempted):"Codex quota recovery status");
}

/* ---------- chat rows (layer 1) ---------- */
function ringHTML(fraction, cls="ring") {
  const f=Math.max(0,Math.min(1,fraction??0)),c=2*Math.PI*1.6;
  return '<svg class="'+cls+'" viewBox="0 0 10 10" aria-hidden="true"><circle class="ring-track" cx="5" cy="5" r="4.25"/>'+(f>0?'<circle class="ring-fill" cx="5" cy="5" r="1.6" stroke-dasharray="'+(c*f).toFixed(2)+' '+c.toFixed(2)+'" transform="rotate(-90 5 5)"/>':'')+'</svg>';
}
function glyphHTML(st) { return '<i class="g g-'+st.glyph+' t-'+st.tone+(st.key==="running"?" breathe":"")+'"></i>'; }
function runwayContextPair(chat) {
  const tokens=safeNumber(chat.contextTokens),windowSize=safeNumber(chat.contextWindow),observed=stamp(chat.contextObservedAt),compacted=stamp(chat.compactionStartedAt);
  const valid=Boolean(runwayCurrentEvidenceFresh()&&!chat.telemetryUnavailable&&!chat.metricsUnavailable&&chat.model&&chat.contextModel===chat.model&&observed&&observed<=Date.now()&&windowSize>0&&tokens!=null&&(!compacted||observed>=compacted));
  return {tokens:valid?tokens:null,windowSize:valid?windowSize:null,observed:valid?observed:null};
}
function runwayContextDelta(chat,pair=runwayContextPair(chat)) {
  const observed=stamp(chat.contextDeltaObservedAt),delta=chat.contextDelta;
  return Boolean(pair.observed&&observed===pair.observed&&chat.contextModel===chat.model&&!chat.telemetryUnavailable&&!chat.metricsUnavailable&&typeof delta==='number'&&Number.isFinite(delta))?delta:null;
}
function contextHTML(chat) {
  if(RUNWAY) {
    const pair=runwayContextPair(chat),tokens=pair.tokens,windowSize=pair.windowSize;
    if(tokens==null||windowSize==null)return '';
    const pct=windowSize>0&&tokens!=null?tokens*100/windowSize:null;
    const tone=pct==null?'':pct>75?'err':pct>50?'attn':'';
    const delta=runwayContextDelta(chat,pair),deltaText=delta==null?'':'; change since previous sample '+(delta>0?'+':'')+fmt(delta)+' tokens';
    const title=String(tokens)+' / '+String(windowSize)+deltaText;
    return '<span class="ctx-tokens" title="'+esc(title)+'"><span class="runway-context-fraction"><span class="runway-context-used'+(tone?' t-'+tone:'')+'">'+fmt(tokens)+'</span><span class="runway-context-separator"> / </span><span class="runway-context-total">'+fmt(windowSize)+'</span></span></span>';
  }
  const pct=safeNumber(chat.contextPercent);
  const title='';
  if(pct==null)return '<span class="ctx"'+title+'><span class="meter-s"></span><span class="ctx-pct t-mute">—</span></span>';
  const tone=contextTone(pct);
  return '<span class="ctx'+(tone?" t-"+tone:"")+'"'+title+'><span class="meter-s"><i style="width:'+Math.max(0,Math.min(100,pct))+'%"></i></span><span class="ctx-pct">'+Math.round(pct)+'%</span></span>';
}
function warmCellHTML(chat) {
  const view=warmthView(chat);
  if(view.kind==="running") {
    const run=runInfo(chat);
    return run?'<span class="r-warm w-run" title="Running for '+runSpan(run.seconds)+'">'+ICON.clock+runSpan(run.seconds)+'</span>':'<span class="r-warm"></span>';
  }
  if(!["warm","cooling","maybe"].includes(view.kind))return '<span class="r-warm"></span>';
  const until=stamp(chat.usageUpdatedAt)+(state.snapshot?.providers?.find(p=>p.name===chat.provider)?.cacheWarmth?.warmSeconds||1800)*1000;
  const tip=view.kind==="maybe"?"Past the warm window · a cold resume re-bills +"+fmt(coldPenalty(chat))+" tokens":"Warm · reply by "+clock(until)+" · going cold re-bills +"+fmt(coldPenalty(chat))+" tokens";
  return '<span class="r-warm w-'+view.kind+'" title="'+esc(tip)+'">'+(view.kind==="maybe"?"":ringHTML(view.kind==="cooling"?view.fraction:0))+esc(view.text)+'</span>';
}
function subCountHTML(chat) {
  const agents=chat.descendants;
  if(!agents.length)return '<span class="r-subs"></span>';
  const needs=agents.filter(isAsking).length,failed=agents.filter(recentFail).length,running=agents.filter(c=>c.status==="running").length;
  const done=agents.length-needs-failed-running;
  const tip=[running&&running+" running",needs&&needs+" needs you",failed&&failed+" failed",done&&done+" done"].filter(Boolean).join(" · ");
  return '<span class="r-subs'+(needs?" t-attn":failed?" t-err":"")+'" title="'+tip+'">'+ICON.branch+agents.length+'</span>';
}
function runwayStatus(chat, rolled=rollup(chat)) {
  if(rolled.word&&rolled.word!=="—")return rolled.word;
  const source=String(chat.sourceStatus||chat.status||"").toLowerCase();
  if((chat.status==="idle"||chat.status==="unknown")&&(stamp(chat.runEndedAt)||/complete|finished|success|\bdone\b/i.test(source+" "+String(chat.stateDetail||""))))return "Finished";
  if(chat.status==="idle")return "Idle";
  return chat.status==="unknown"?"Status unavailable":ownState(chat).word||"Idle";
}
function runwayActivityAge(chat) {
  if(!chat.latest)return "";
  const age=ageShort(chat.latest);return age==="now"?"now":age+" ago";
}
const runwayRecordedAt = chat => Math.max(stamp(chat.updatedAt),stamp(chat.usageUpdatedAt));
const runwayInstructionText = text => /^(?:#\s*)?(?:AGENTS\.md instructions|<INSTRUCTIONS>|<environment_context>|subagent\b|spawned\b)/i.test(String(text||'').trim());
const runwayRequest = chat => runwayInstructionText(chat.latestRequest)?'':chat.latestRequest||'';
function runwayCurrentSnippet(chat) {
  const runStarted=stamp(chat.activeRunStartedAt)||stamp(chat.stateSince);
  for(const [text,at] of [[chat.trackerUpdate,chat.trackerUpdateAt],[chat.latestUpdate,chat.latestUpdateAt]]) {
    const observedAt=stamp(at);
    if(!observedAt||observedAt>Date.now()||(runStarted&&observedAt<runStarted))continue;
    const snippet=trackerSnippet(text);
    if(snippet)return snippet;
  }
  return null;
}
const runwayAssignment = chat => runwayCurrentSnippet(chat)?.task||[chat.assignment,chat.objective,runwayRequest(chat)].find(text=>text&&!runwayInstructionText(text))||"";
function runwayObjective(chat) {
  const snippet=runwayCurrentSnippet(chat);
  if(snippet?.task)return snippet.task;
  const recorded=[chat.assignment,chat.objective].find(text=>typeof text==='string'&&text.trim()&&!runwayInstructionText(text));
  return recorded?recorded.trim():'';
}
function runwayStateNote(chat) {
  if(isAsking(chat))return "Waiting for your input in this chat.";
  if(chat.status==="failed")return chat.failureReason||chat.stateDetail||"This run failed; open the chat to inspect it.";
  if(chat.status==="compacting")return "Compacting context; recorded work may resume afterward.";
  if(chat.status==="quiet")return "No transcript writes for 3+ minutes; a long tool call may still be running.";
  if(chat.stateDetail==="inactive")return "No completion recorded and no transcript writes for 30+ minutes.";
  if(chat.status==="unknown")return "";
  return chat.dataError||"";
}
function runwayPreviewHTML(label, text, cls="", at=null) {
  const snippet=trackerSnippet(text);
  if(snippet)text=snippet.doing||snippet.task;
  const preview=String(text||'').replace(/\[([^\]]+)\]\([^)]*\)/g,'$1').replace(/[*`#]/g,'').replace(/\s+/g,' ').trim();
  const age=at?runwayActivityAge({latest:stamp(at)}):'';
  return '<div class="runway-preview '+cls+'"'+(!text?' hidden':'')+(text?' title="'+esc(label+(age?' · '+age:''))+'"':'')+'><span class="runway-field-label">'+esc(label)+(age?' · '+esc(age):'')+'</span><span class="runway-preview-text">'+esc(preview)+'</span></div>';
}
function runwayContextFactsHTML(chat) {
  const pair=runwayContextPair(chat),tokens=pair.tokens,windowSize=pair.windowSize;
  if(tokens==null||windowSize==null)return '';
  const pct=windowSize>0&&tokens!=null?tokens*100/windowSize:null;
  const tone=pct==null?'':pct>75?'err':pct>50?'attn':'';
  const headroom=tokens!=null&&windowSize!=null?Math.max(0,windowSize-tokens):null;
  const observed=pair.observed;
  return '<div class="facts runway-context-facts"><div><span class="f-l">Recorded usage</span><span class="f-v"><span class="runway-context-used'+(tone?' t-'+tone:'')+'">'+fmt(tokens)+'</span><span class="runway-context-separator"> / </span><span class="runway-context-total">'+fmt(windowSize)+'</span></span></div><div><span class="f-l">Headroom</span><span class="f-v">'+fmt(headroom)+'</span></div></div>';
}
function runwayDetailHTML(chat, agent=false) {
  const currentEvidence=runwayCurrentEvidenceFresh(),run=currentEvidence?runInfo(chat):null,note=currentEvidence?runwayStateNote(chat):'';
  const snippet=currentEvidence?trackerSnippet(chat.latestUpdate):null;
  const parent=hasTreeParent(chat)?(chat.root?.key===treeParentKey(chat)?chat.root:(chat.root?.descendants||[]).find(c=>c.key===treeParentKey(chat))):null;
  const owner=hasTreeParent(chat)?(parent?.title||chat.parentTitle):null;
  const metadata=[currentEvidence?runwayModelLabel(chat):'',currentEvidence&&chat.reasoningEffort?'Reasoning '+chat.reasoningEffort:null,run?'Run '+runSpan(run.seconds)+(run.live?' elapsed':' recorded'):null].filter(Boolean).join(' · ');
  const assignment=agent?runwayAssignment(chat):chat.objective;
  const workspace=chat.workspace||chat.root?.workspace;
  const update=snippet?Object.entries({Task:snippet.task,Doing:snippet.doing,Next:snippet.next,Blocked:snippet.blocked}).filter(([,value])=>value).map(([label,value])=>'<div class="runway-full-field"><span class="runway-field-label">'+label+'</span><div>'+esc(value)+'</div></div>').join(''):chat.latestUpdate?'<div class="runway-full-field"><span class="runway-field-label">Last recorded update'+(chat.latestUpdateAt?' · '+esc(runwayActivityAge({latest:stamp(chat.latestUpdateAt)})):'')+'</span><div>'+esc(chat.latestUpdate)+'</div></div>':'';
  return (note?'<div class="runway-state-note">'+esc(note)+'</div>':'')+
    (assignment?'<div class="runway-full-field"><span class="runway-field-label">'+(agent?'Assignment':'Objective')+'</span><div>'+esc(assignment)+'</div></div>':'')+
    (runwayRequest(chat)?'<div class="runway-full-field"><span class="runway-field-label">Request</span><div>'+esc(runwayRequest(chat))+'</div></div>':'')+
    update+
    runwayContextFactsHTML(chat)+(metadata?'<div class="meta">'+esc(metadata)+'</div>':'')+
    (workspace?'<div class="runway-full-field"><span class="runway-field-label">Workspace</span><bdi>'+esc(workspace)+'</bdi></div>':'')+
    (agent&&owner?'<div class="runway-owner">'+esc('Delegated by '+owner)+'</div>':'');
}
function rowHTML(chat) {
  const st=rollup(chat);
  if(RUNWAY)return runwayTelemetryHTML(chat,false);
  const stateText=RUNWAY?runwayStatus(chat,st):st.word;
  return glyphHTML(st)+
    '<span class="r-title">'+esc(chat.title||"Untitled")+'</span>'+subCountHTML(chat)+
    '<span class="r-age" title="Latest activity">'+ageShort(chat.latest)+'</span>'+ 
    '<span class="r-meta"><span class="r-model">'+esc(modelLabel(chat))+'</span>'+contextHTML(chat)+'</span>'+ 
    '<span class="r-state t-'+(st.wordTone||st.tone)+'"'+(st.title?' title="'+esc(st.title)+'"':'')+'>'+esc(stateText)+(st.via&&stateText?ICON.branch:"")+'</span>'+ 
    warmCellHTML(chat);
}

/* ---------- stat strip (section N) ---------- */
function runwayStateElapsed(chat) {
  const execution=runwayExecutionView(chat);
  const status=runwayEffectiveStatus(chat);
  const waiting=runwayWaiting(chat);
  const since=stamp(waiting?chat.pendingQuestionAt||(status==='needs-input'?chat.stateSince:null):execution?execution.since:chat.executionStatus===undefined?status==='compacting'?chat.compactionStartedAt||chat.stateSince:status==='running'?chat.stateSince||chat.activeRunStartedAt:chat.stateSince:['idle','failed'].includes(status)?chat.stateSince:null);
  return since&&since<=Date.now()?runSpan((Date.now()-since)/1000):'—';
}
const runwayMessageDirection = chat => ({"parent-to-agent":"from parent","agent-to-parent":"to parent","agent-to-agent":"between agents"})[chat.latestMessageDirection]||'';
const runwayMessageAge = chat => { const sent=stamp(chat.latestMessageAt),age=Date.now()-sent;return runwayMessageDirection(chat)&&sent&&age>=0&&age<10000?age:null; };
const runwayAgentHasNewMessage = chat => runwayMessageAge(chat)!=null;
function runwayQuestionButton(chat) {
  const waiting=chat=>runwayWaiting(chat);
  const source=PHONE?(waiting(chat)&&chat.pendingQuestion?chat:(!hasTreeParent(chat)?(chat.descendants||[]).find(item=>waiting(item)&&item.pendingQuestion):null)):
    (waiting(chat)?chat:(!hasTreeParent(chat)?(chat.descendants||[]).find(item=>waiting(item)):null));
  if(!source)return '';
  if(PHONE)return '<button data-runway-slot="question" class="runway-question-trigger" data-action="runway-question" data-key="'+esc(source.key)+'" aria-label="Show exact question for '+esc(runwayTitle(source,hasTreeParent(source)))+'" title="Show exact pending question">?</button>';
  return '';
}
function runwayWorkspacePath(workspace) {
  const normalized=String(workspace||'').trim().replace(/\//g,'\\');
  const path=/^[A-Za-z]:\\+$/.test(normalized)?normalized.slice(0,3):normalized.replace(/\\+$/,'');
  // Show the absolute Windows location from its volume root. If telemetry is not
  // drive-qualified, do not imply a root that the provider did not report.
  return /^[A-Za-z]:\\/.test(path)?path:'';
}
function runwayWorkspaceLeaf(workspace) {
  const path=runwayWorkspacePath(workspace);
  return path?path.split('\\').at(-1)||path:'';
}
function runwayWorkspaceHTML(chat) {
  if(!chat.workspace&&!chat.root?.workspace)return '';
  const own=Boolean(chat.workspace),workspace=chat.workspace||chat.root?.workspace,path=runwayWorkspacePath(workspace);
  if(!path)return '';
  const inherited=!own&&Boolean(chat.root?.workspace),full=workspace;
  const title=(chat.workspaceKind==='codex-chat'?'Per-chat working folder; not a project checkout · ':inherited?'Inherited workspace · ':'Workspace · ')+full;
  const drive=path.slice(0,3),parts=path.slice(3).split('\\').filter(Boolean),leafPart=parts.pop()||'',middle=parts.join('\\'),leaf=(middle?'\\':'')+leafPart;
  return '<span data-runway-slot="workspace" class="runway-workspace" title="'+esc(title)+'" aria-label="'+esc((inherited?'Inherited workspace: ':'Workspace: ')+path)+'">'+(inherited?'<span class="runway-workspace-inherited" aria-label="Inherited workspace">'+ICON.branch+'</span>':'')+'<span class="runway-path-drive">'+esc(drive)+'</span><span class="runway-path-middle">'+esc(middle)+'</span><span class="runway-path-leaf">'+esc(leaf)+'</span></span>';
}
function runwayActivityMarkup(label,fullText=label) {
  if(!label)return '';
  return '<span data-runway-slot="activity" class="runway-workspace runway-activity" title="'+esc(fullText)+'" aria-label="'+esc(label)+'">'+esc(label)+'</span>';
}
function runwayPhoneActivityHTML(chat) {
  if(!runwayRunning(chat)||chat.activityUnavailable||chat.telemetryUnavailable)return '';
  const activity=chat.activity&&typeof chat.activity==='object'?chat.activity:null;
  if(!activity)return '';
  const kind=activity.kind,labels={searching:'Searching files',reading:'Reading file',editing:'Editing file',testing:'Running checks',running_command:'Running command',running_code:'Running code',inspecting:'Inspecting page',coordinating:'Coordinating agents',checking_data:'Checking data',using_tool:'Using tool',waiting:'Waiting for result',reading_output:'Reading command output',searching_web:'Searching web',thinking:'Thinking'};
  if(!labels[kind])return '';
  let label=typeof activity.label==='string'&&activity.label===labels[kind]?activity.label:labels[kind],paths=[];
  if((kind==='reading'||kind==='editing'||kind==='searching')&&Array.isArray(activity.files)){
    paths=activity.files.filter(path=>typeof path==='string'&&path.trim());
    const names=paths.map(path=>path.replace(/\\/g,'/').split('/').filter(Boolean).at(-1)).filter(Boolean);
    if(names.length)label=(kind==='reading'?'Reading ':kind==='editing'?'Editing ':'Searching ')+names.join(', ');
  }
  return '<span data-runway-slot="workspace" class="runway-workspace runway-activity"'+(paths.length?' title="'+esc(paths.join('\n'))+'"':'')+' aria-label="'+esc(label)+'">'+esc(label)+'</span>';
}
function runwayActivityHTML(chat) {
  if(PHONE)return runwayPhoneActivityHTML(chat);
  const evidence=runwayActivityEvidence(chat),labels={searching:'Searching files',reading:'Reading file',editing:'Editing file',testing:'Running checks',running_command:'Running command',running_code:'Running code',inspecting:'Inspecting page',coordinating:'Messaging agent',checking_data:'Checking data',using_tool:'Using tool',waiting:'Waiting for result',reading_output:'Checking output',searching_web:'Searching web',thinking:'Reasoning'};
  const activeChild=(chat.descendants||[]).some(child=>['running','compacting'].includes(runwayActivityEvidence(child).status));
  if(evidence.status==='running'&&evidence.activity){
    const {kind}=evidence.activity;let label=labels[kind];
    if(!label)return '';
    const paths=['reading','editing','searching'].includes(kind)&&Array.isArray(evidence.activity.files)?evidence.activity.files.filter(path=>typeof path==='string'&&path.trim()):[];
    if(paths.length){const names=paths.map(path=>path.replace(/\\/g,'/').split('/').filter(Boolean).at(-1)).filter(Boolean);if(names.length)label=(kind==='reading'?'Reading ':kind==='editing'?'Editing ':'Searching ')+names.join(', ');return runwayActivityMarkup(label,label+' · '+paths.join('\n'));}
    return runwayActivityMarkup(label);
  }
  if(evidence.status==='running')return '';
  if(evidence.status==='compacting')return runwayActivityMarkup('Compacting');
  if(evidence.status==='waiting'||(runwayCurrentEvidenceFresh()&&runwayWaiting(chat)))return runwayActivityMarkup('Waiting for input');
  if(['idle','failed','inactive'].includes(evidence.status)&&activeChild)return runwayActivityMarkup('Subagents running');
  return '';
}
function runwayStatusGlyph(status) {
  const type=status==='needs-input'?'waiting':status==='compacting'?'compacting':status==='running'?'running':'other';
  const glyph=type==='waiting'?'<circle cx="6" cy="6" r="4.2"/><path d="M6 3.5v2.8M6 8.2h.01"/>':type==='compacting'?'<path d="M2 6a4 4 0 0 1 6.5-3.1M10 6a4 4 0 0 1-6.5 3.1"/><path d="M7.8 1.8h1v1M4.2 10.2h-1v-1"/>':type==='running'?'<circle cx="6" cy="6" r="3.7"/>':'<path d="M3 6h6"/>';
  return '<svg class="runway-state-glyph is-'+type+'" viewBox="0 0 12 12" aria-hidden="true">'+glyph+'</svg>';
}
function runwayStatusDot(status,tone) {
  return '<svg class="runway-status-dot" width="8" height="8" viewBox="0 0 8 8" role="img" aria-label="'+esc(status)+'" title="'+esc(status)+'"><circle cx="4" cy="4" r="3.2"/></svg>';
}
// Orchestration job titles ("# [Project] Luna · task — long goal (continue #2)") become "Luna · task".
// Codex titles a thread with its whole first prompt; keep only the first line/heading.
function runwayFirstLine(title) {
  return String(title||'').trim().split(/\r?\n|\s#{2,}\s|\s(?:Goal|Stage):\s/)[0].trim();
}
function runwayCapTitle(title,max=72) {
  const text=runwayFirstLine(title);
  return text.length>max?text.slice(0,max-1).trimEnd()+'…':text;
}
function runwayJobTitle(title) {
  // '?' covers briefs whose '·'/'—' were lost to a non-UTF-8 pipe.
  const match=/^#?\s*\[[^\]]{1,40}\]\s*(Luna|Sol|Haiku|Opus|Sonnet|Builder|Engineer|Scout)\s*[·?•|:]\s*(.+)$/i.exec(runwayFirstLine(title));
  if(!match)return null;
  const task=match[2].split(/\s+[—–?]\s+|\s+-\s+|\s*\((?:continue|retry)\b/i)[0].trim();
  return task?match[1]+' · '+runwayCapTitle(task,40):null;
}
function runwayTitle(chat,agent=false) {
  const job=runwayJobTitle(chat.title)||(agent?runwayJobTitle(chat.agentName):null);
  let title=String(agent?agentLabel(chat):chat.title||'Untitled');
  if(job)return job;
  if(!agent)return runwayCapTitle(title)||'Untitled';
  const project=runwayWorkspaceLeaf(chat.workspace||chat.root?.workspace);
  title=title.replace(/^\[(?:Memoir|Token Tracker)\]\s+(?:Builder|Engineer|Scout)\s*[·:—-]\s*/i,'');
  for(const role of ['sol','luna']) {
    const prefix=project+' '+role+' ';
    if(project&&title.toLowerCase().startsWith(prefix.toLowerCase()))title=title.slice(prefix.length).replace(/^(?:builder|engineer|scout)\s*[·:—-]?\s*/i,'');
  }
  return runwayCapTitle(title||chat.title)||'Untitled';
}
function runwayTelemetryHTML(chat,agent=false) {
  const currentEvidence=runwayCurrentEvidenceFresh();
  const st=ownState(chat);
  const childWaiting=(chat.descendants||[]).some(runwayWaiting),childRunning=(chat.descendants||[]).some(runwayRunning);
  const ownCurrent=runwayWaiting(chat)||runwayRunning(chat)||(!runwayTerminated(chat)&&recentFail(chat));
  const effectiveStatus=runwayEffectiveStatus(chat);
  const tone=runwayWaiting(chat)?'attn':effectiveStatus==='compacting'&&runwayRunning(chat)?'info':runwayRunning(chat)?'live':ownCurrent?'err':childWaiting?'attn':childRunning?'live':'mute';
  const statusUnavailable=!currentEvidence||((chat.status==='unknown'||chat.statusSource==='unavailable')&&!childWaiting&&!childRunning&&!runwayRunning(chat));
  const status=statusUnavailable?'':runwayWaiting(chat)?'Waiting':!ownCurrent&&childWaiting?(PHONE?'Agent waiting':'Waiting'):!ownCurrent&&childRunning?'Agents running':runwayRunning(chat)?effectiveStatus==='compacting'?'Compacting':'Running':chat.executionStatus!==undefined?effectiveStatus==='needs-input'?'Waiting':effectiveStatus==='idle'?'Idle':effectiveStatus==='failed'?runwayStatus({...chat,status:'failed'},ownState({...chat,status:'failed'})):'':runwayStatus(chat,st);
  const modelText=runwayModelLabel(chat);
  const modelChanged=runwayModelRecentlyChanged(chat);
  const modelEvent=state.modelVersions.get(chat.key||[chat.provider,chat.id].join(':'))?.highlightEventAt;
   const modelColor=chat.provider==='Claude'?'is-claude-model':'is-codex-model';
   const modelHTML=currentEvidence&&modelText&&chat.model&&!chat.telemetryUnavailable?'<span data-runway-slot="model" class="runway-telemetry-model" title="'+esc(modelText)+'"><span class="runway-model-name '+modelColor+(modelChanged?' model-changed':'')+'"'+(modelChanged&&modelEvent?' data-model-change-at="'+modelEvent+'"':'')+'>'+esc(modelText)+'</span></span>':'';
  const directionText=runwayMessageDirection(chat);
  const statusCode=runwayWaiting(chat)?'needs-input':effectiveStatus;
  const messageCapable=Boolean(agent&&hasTreeParent(chat)),hasRecentMessage=currentEvidence&&messageCapable&&runwayAgentHasNewMessage(chat);
  const messageAge=hasRecentMessage?runwayMessageAge(chat):null,messageAt=hasRecentMessage?stamp(chat.latestMessageAt):0;
  const messageContents=hasRecentMessage?'<span data-runway-slot="message" class="runway-message-indicator" data-message-at="'+messageAt+'" style="--message-age:-'+Math.max(0,messageAge||0)+'ms" title="Recent message'+(directionText?' · '+directionText:'')+' · visible for 10 seconds" aria-label="Recent message'+(directionText?' · '+directionText:'')+'">'+ICON.message+'<span class="runway-message-dots" aria-hidden="true"><i></i><i></i><i></i></span></span>':'';
  const phoneMessageSlot=messageCapable?'<span class="runway-message-slot">'+messageContents+'</span>':'';
  const messageLane='<span data-runway-slot="message-lane" class="runway-message-slot">'+messageContents+'</span>';
  const childDotTone=runwayWaiting(chat)?'attn':runwayRunning(chat)?'live':tone;
  const childNameStatus=agent&&!PHONE&&!REFINED?'<span data-runway-slot="name-status" class="runway-name-status'+(childDotTone?' t-'+childDotTone:'')+'"'+(!status?' aria-hidden="true"':'')+'>'+(status?runwayStatusDot(status,childDotTone):'')+'</span>':'';
  const activityHTML=RUNWAY&&!PHONE?runwayActivityHTML(chat):'';
  const displayTitle=runwayTitle(chat,agent);
  const titleHTML='<span data-runway-slot="title" class="'+(agent?'runway-agent-name':'r-title')+'" title="'+esc(displayTitle)+'">'+(PHONE?phoneMessageSlot:'')+'<span data-runway-slot="label" class="runway-agent-label">'+esc(displayTitle)+'</span>'+(REFINED?messageLane:'')+childNameStatus+runwayQuestionButton(chat)+'</span>';
  const context=contextHTML(chat);
  const elapsed=currentEvidence&&ownCurrent&&!statusUnavailable?runwayStateElapsed(chat):'—';
  const elapsedHTML=elapsed!=='—'?'<span data-runway-slot="elapsed" class="runway-state-elapsed" title="Elapsed in current state">'+esc(elapsed)+'</span>':'';
  const run=currentEvidence?runInfo(chat):null,runTimeHTML=run?'<span data-runway-slot="run-time" class="runway-run-time runway-run-clock" title="'+(run.live?'Elapsed in current run':'Recorded run duration')+'">'+ICON.clock+esc(runSpan(run.seconds))+'</span>':'';
  const modelTiming=(modelHTML||runTimeHTML)?'<span data-runway-slot="model-timing" class="runway-model-timing">'+modelHTML+runTimeHTML+'</span>':'';
  const waitingTarget=runwayWaiting(chat)?chat:(!ownCurrent&&childWaiting&&!hasTreeParent(chat)?(chat.descendants||[]).find(runwayWaiting):null);
  const waitingLabel=waitingTarget?(isCodexChat(waitingTarget)?'Open waiting chat to answer ':'Show question for ')+(waitingTarget.title||runwayTitle(waitingTarget,hasTreeParent(waitingTarget))):'';
  const stateContents=status==='Waiting'&&waitingTarget&&!PHONE?'<button class="runway-state-word runway-waiting-link" data-action="runway-question" data-key="'+esc(waitingTarget.key)+'" aria-label="'+esc(waitingLabel)+'" title="'+(isCodexChat(waitingTarget)?'Open this waiting chat':'Show the pending question')+'">Waiting</button>':!PHONE?((agent||!status?'':runwayStatusDot(status,tone))+activityHTML):(status?runwayStatusGlyph(statusCode)+'<span class="runway-state-word">'+esc(status)+'</span>':'');
  const stateHTML=status||activityHTML?'<span data-runway-slot="state" class="runway-telemetry-state t-'+tone+(agent&&!PHONE||activityHTML?' runway-agent-state':'')+'" data-own-waiting="'+runwayWaiting(chat)+'" data-child-waiting="'+childWaiting+'"'+(!PHONE&&status?' aria-label="'+esc(status)+'" title="'+esc(status)+'"':'')+'>'+(!statusUnavailable||PHONE?stateContents:'')+(!PHONE&&(status==='Waiting'||statusUnavailable)?activityHTML:'')+'</span>':'';
  const primary='<span data-runway-slot="primary" class="runway-primary">'+titleHTML+
    stateHTML+(PHONE?elapsedHTML:'')+'</span>'+ 
    (agent?(PHONE?runwayActivityHTML(chat):''):runwayWorkspaceHTML(chat));
  const lower=modelHTML+(context?'<span data-runway-slot="context" class="runway-telemetry-context">'+context+'</span>':'');
  if(PHONE)return primary+lower;
  const topline='<span class="runway-topline">'+primary+'</span>';
  const contextSlot=context?'<span data-runway-slot="context" class="runway-telemetry-context">'+context+'</span>':'';
  const objective=agent?runwayObjective(chat):'';
  const visibleActivity=activityHTML.replace(/<[^>]*>/g,'');
  const objectiveSlot=objective&&esc(objective)!==visibleActivity?'<div data-runway-slot="objective" class="runway-agent-objective">'+esc(objective)+'</div>':'';
  if(REFINED){
    const stateIcon=runwayStatusGlyph(status==='Waiting'?'needs-input':status==='Compacting'?'compacting':status==='Running'||status==='Agents running'?'running':statusCode);
    const visibleStatus=COMPACT&&status==='Agents running'?'Agents active':status;
     const stateContent=status?((status==='Waiting'&&waitingTarget?stateIcon+'<button class="runway-state-word runway-waiting-link" data-action="runway-question" data-key="'+esc(waitingTarget.key)+'" aria-label="'+esc(waitingLabel)+'" title="'+(isCodexChat(waitingTarget)?'Open this waiting chat':'Show the pending question')+'">Waiting</button>':stateIcon+'<span class="runway-state-word"'+(visibleStatus!==status?' aria-label="'+esc(status)+'" title="'+esc(status)+'"':'')+'>'+esc(visibleStatus)+'</span>')):'';
    const treeAnchor='<span data-runway-slot="tree-anchor" class="runway-name-status runway-refined-tree-anchor t-'+tone+'" aria-hidden="true">'+(status?runwayStatusDot(status,tone):'')+'</span>';
    const elapsedTarget=ownCurrent?chat:waitingTarget;
    const elapsed=currentEvidence&&elapsedTarget&&!statusUnavailable?runwayStateElapsed(elapsedTarget):'';
    const request=chat.lastRequest||chat.latestRequest||{},input=safeNumber(request.input),cached=safeNumber(request.cached);
    const cachePercent=currentEvidence&&!chat.telemetryUnavailable&&!chat.metricsUnavailable&&input>0&&cached!=null?Math.round(cached*100/input):null;
    const workspace=chat.workspace||chat.root?.workspace,workspacePath=runwayWorkspacePath(workspace),workspaceLeaf=runwayWorkspaceLeaf(workspace);
    const inherited=!chat.workspace&&Boolean(chat.root?.workspace);
    const workspaceTitle=(chat.workspaceKind==='codex-chat'?'Per-chat working folder; not a project checkout · ':inherited?'Inherited workspace · ':'Workspace · ')+workspacePath;
    const workspaceLabel=(inherited?'Inherited workspace: ':'Workspace: ')+workspacePath;
    const cleanPath=workspacePath&&workspaceLeaf?'<span data-runway-slot="workspace" class="runway-workspace" title="'+esc(workspaceTitle)+'" aria-label="'+esc(workspaceLabel)+'"><span class="runway-path-leaf">'+esc(workspaceLeaf)+'</span></span>':'';
    const contextValue=context?'<span data-runway-slot="context" class="runway-refined-context">'+context+'</span>':'';
    const cacheValue=cachePercent==null?'':'<span data-runway-slot="cache" class="runway-refined-cache">Cache '+cachePercent+'%</span>';
    // The status column already says Compacting / Waiting / Agents running; don't repeat it as activity.
    const visibleActivityHTML=status&&/^(?:Compacting|Waiting for input|Subagents running)$/.test(activityHTML.replace(/<[^>]*>/g,''))?'':activityHTML;
     return '<div class="runway-refined-row" data-runway-slot="row">'+treeAnchor+'<span class="runway-refined-title">'+titleHTML+modelHTML+'</span><span data-runway-slot="timing" class="runway-refined-timing"><span data-runway-slot="elapsed" class="runway-refined-elapsed">'+esc(elapsed||'')+'</span></span><span data-runway-slot="status" class="runway-refined-status t-'+tone+'" data-own-waiting="'+runwayWaiting(chat)+'" data-child-waiting="'+childWaiting+'">'+stateContent+'</span><span data-runway-slot="activity-column" class="runway-refined-activity">'+visibleActivityHTML+'</span><span data-runway-slot="path-column" class="runway-refined-path">'+cleanPath+'</span><span data-runway-slot="tokens" class="runway-refined-tokens">'+contextValue+cacheValue+'</span></div>';
  }
  return agent?messageLane+topline+'<span class="runway-agent-secondary">'+modelTiming+'</span>'+contextSlot+objectiveSlot:messageLane+topline+modelTiming+contextSlot;
}
function runwayQuestionPopover(chat) {
  if(!runwayCurrentEvidenceFresh()||chat?.telemetryUnavailable)return '';
  if(!chat||!runwayWaiting(chat))return '';
  if(!PHONE&&!isCodexChat(chat)&&!chat.pendingQuestion)return '<div class="runway-question-panel"><button class="runway-question-close" data-action="close-popover" aria-label="Close" title="Close">×</button><div class="runway-question-full">Waiting for your approval in Claude.</div></div>';
  if(!chat.pendingQuestion)return '';
  if(!PHONE&&!isCodexChat(chat)){
    const command=resumeCommand(chat);
    return '<div class="runway-question-panel"><button class="runway-question-close" data-action="close-popover" aria-label="Close question" title="Close question">×</button><div class="runway-question-full">'+richText(chat.pendingQuestion)+'</div>'+plainOptionsHTML(chat.pendingOptions)+(command?'<button class="q-open" data-action="copy" data-copy="'+esc(command)+'" title="'+esc(command)+'">'+ICON.copy+'<span>Copy resume command</span>'+ICON.check+'</button>':'')+'</div>';
  }
  const choices=PHONE?((chat.pendingOptions||[]).length?'<ul class="phone-question-options">'+chat.pendingOptions.map(option=>'<li>'+esc(option)+'</li>').join('')+'</ul>':''):((chat.pendingOptions||[]).length?optionsHTML(chat.pendingOptions,chat.id):'<button class="q-open" data-action="open" data-thread="'+esc(chat.id)+'" title="Open this chat in Codex">'+ICON.open+'<span>Open in Codex to answer</span>'+ICON.check+'</button>');
  return '<div class="runway-question-panel"><button class="runway-question-close" data-action="close-popover" aria-label="Close question" title="Close question">×</button><div class="runway-question-full">'+richText(chat.pendingQuestion)+'</div>'+choices+'</div>';
}
function statStripHTML(stats, {live, fin=false, lead=""}={}) {
  const num=(value,label,cls)=>'<span class="s '+cls+'"><b>'+(value==null?"—":esc(value))+'</b> <i>'+label+'</i></span>';
  let time='<span class="s s-time">—</span>';
  if(stats.run)time=live?'<span class="s s-time live">'+ICON.clock+runSpan(stats.run)+'</span>':'<span class="s s-time">ran '+runSpan(stats.run)+'</span>';
  return '<span class="ss'+(live?" live":" dim")+(fin?" fin":"")+(lead?" lead":"")+'">'+lead+num(stats.tokens==null?null:fmt(stats.tokens),"tok","s-tok")+num(stats.tools,"tools","s-tool")+filesHTML(stats)+time+'</span>';
}
// Git-style net file changes, as Codex shows diffs: +created (green) ~modified −deleted (red).
function filesHTML(stats) {
  const c=stats.changes;
  if(!c)return '<span class="s s-file"><b>'+(stats.files==null?"—":esc(stats.files))+'</b> <i>files</i></span>';
  const part=(n,sign,cls,word)=>'<b class="fc '+cls+(n?"":" zero")+'" title="'+n+" "+word+'">'+sign+n+'</b>';
  return '<span class="s s-file">'+part(c.created,"+","fc-add","created")+part(c.modified,"~","fc-mod","modified")+part(c.deleted,"−","fc-del","deleted")+' <i>files</i></span>';
}
function agentStats(chat) {
  const run=runInfo(chat),fc=chat.fileChanges;
  const changes=fc&&typeof fc==="object"?{created:safeNumber(fc.created)||0,modified:safeNumber(fc.modified)||0,deleted:safeNumber(fc.deleted)||0}:null;
  return {tokens:safeNumber(chat.tokens),tools:safeNumber(chat.toolCalls),files:safeNumber(chat.filesEdited),changes,run:run?.seconds??null,live:Boolean(run?.live)||chat.status==="running"||chat.status==="compacting"};
}
function totalStats(agents) {
  const stats=agents.map(agentStats);
  const sum=field=>knownSum(stats.map(s=>s[field]));
  const withChanges=stats.filter(s=>s.changes);
  const changes=withChanges.length?["created","modified","deleted"].reduce((acc,k)=>(acc[k]=withChanges.reduce((n,s)=>n+s.changes[k],0),acc),{}):null;
  return {tokens:sum("tokens"),tools:sum("tools"),files:sum("files"),changes,run:sum("run"),live:stats.some(s=>s.live)};
}
// A strip that just went from running to finished cross-fades once.
function noteLiveness(chat, live) {
  const was=state.agentLive.get(chat.key);
  state.agentLive.set(chat.key,live);
  if(was&&!live)state.finishedAt.set(chat.key,Date.now());
  return Date.now()-(state.finishedAt.get(chat.key)||0)<1000;
}
const sortAgents = agents => [...agents].sort((a,b)=>(isAsking(b)-isAsking(a))||(recentFail(b)-recentFail(a))||((b.status==="running")-(a.status==="running"))||b.latest-a.latest);
// The tray is a live control surface, not agent history. Never retain completed or failed children.
const isWorking = a => agentLive(a)||isAsking(a);
// Auto-review/guardian sessions are internal bookkeeping; surface them only when they need a reply.
const isVisibleAgent = a => RUNWAY?runwayVisibleActive(a):isAutoReview(a)?isAsking(a):isWorking(a);
function trayState(chat) {
  return chat.descendants.some(isVisibleAgent)?"active":"resting";
}
function agentRowEntry(agent, query) {
  if(RUNWAY)return {key:agent.key,cls:'ag runway-agent'+(query.active&&matchesQuery(agent,query)?' match':''),attrs:{'data-sub':agent.key,'data-provider':agent.provider,style:'--agent-depth:'+Math.max(0,(agent.depth||1)-1)},html:'<div class="runway-agent-glance">'+runwayTelemetryHTML(agent,true)+'</div>'};
  const st=ownState(agent),stats=agentStats(agent),fin=noteLiveness(agent,stats.live);
  const tip=["context "+(safeNumber(agent.contextPercent)==null?"—":Math.round(agent.contextPercent)+"%"),ageShort(agent.latest)+(agent.latest&&ageShort(agent.latest)!=="now"?" ago":""),agent.reasoningEffort?"effort "+agent.reasoningEffort:null,"click to copy resume"].filter(Boolean).join(" · ");
  return {key:agent.key,cls:"ag"+(stats.live?" is-live":" is-done")+(isAsking(agent)?" asks":"")+(query.active&&matchesQuery(agent,query)?" match":""),
    attrs:{role:"button",tabindex:"0","data-action":resumeCommand(agent)?"copy":null,"data-copy":resumeCommand(agent)||null,"data-sub":agent.key,title:tip},
    html:'<span class="ag-1">'+glyphHTML(st)+'<span class="ag-mark">'+markFor(agent)+'</span><span class="ag-name">'+esc(agentLabel(agent))+'</span><span class="ag-model" title="'+esc(agent.model||"")+'">'+esc(agentModelLabel(agent))+'</span><span class="ag-state t-'+(st.wordTone||st.tone)+'">'+esc(st.word)+'</span>'+ICON.check+'</span>'+
      '<span class="ag-2">'+statStripHTML(stats,{live:stats.live,fin})+'</span>'+
      (agent.pendingQuestion?'<span class="ag-q">'+richText(agent.pendingQuestion)+'</span>':'')};
}
function denseRowEntry(agent) {
  const stats=agentStats(agent);
  return {key:agent.key,cls:"ag dense",attrs:{role:"button",tabindex:"0","data-action":resumeCommand(agent)?"copy":null,"data-copy":resumeCommand(agent)||null,"data-sub":agent.key,title:modelLabel(agent)+" · "+(stats.tools??"—")+" tools · "+(stats.files??"—")+" files"},
    html:'<span class="ag-mark">'+markFor(agent)+'</span><span class="ag-name">'+esc(agentLabel(agent))+'</span>'+(stats.tokens==null&&!stats.tools?'<span class="s s-time">'+esc(modelLabel(agent))+'</span>':'<span class="s s-tok"><b>'+fmt(stats.tokens)+'</b> <i>tok</i></span>'+filesHTML(stats)+'<span class="s s-time">'+(stats.run==null?"—":"ran "+runSpan(stats.run))+'</span>')};
}
function trayEntries(chat, query) {
  const agents=RUNWAY?chat.descendants.filter(isVisibleAgent):sortAgents(chat.descendants.filter(isVisibleAgent));
  // Record liveness first so an agent that just stopped is still "working" for its fade minute.
  agents.forEach(a=>{if(state.agentLive.get(a.key)!==agentStats(a).live)noteLiveness(a,agentStats(a).live);});
  if(RUNWAY){const entries=[];const visit=parent=>{for(const agent of agents.filter(a=>treeParentKey(a)===parent.key)){entries.push(agentRowEntry(agent,query));visit(agent);}};visit(chat);for(const agent of agents)if(!entries.some(e=>e.key===agent.key))entries.push(agentRowEntry(agent,query));return entries;}
  // Agents with no recorded usage (bare auto-review shells) never get a stats strip.
  const empty=a=>!agentLive(a)&&!recentFail(a)&&!isAsking(a)&&agentStats(a).tokens==null&&!agentStats(a).tools;
  const full=agents.filter(a=>!empty(a)&&isWorking(a));
  const totals=totalStats(agents);
  return [...full.map(a=>agentRowEntry(a,query)),
    {key:"__total",cls:"ag total",attrs:{},html:'<span class="ag-2">'+statStripHTML(totals,{live:totals.live,lead:'<span class="s s-n" title="'+agents.length+' agents"><b>Σ '+agents.length+'</b></span>'})+'</span>'}];
}
function runwayAgentsExpanded(chat) {
  if(RUNWAY)return true;
  const query=parseQuery(state.query);
  return state.agentOpen.has(chat.key)||!state.agentCollapsed.has(chat.key)||(!state.agentSearchClosed.has(chat.key)&&query.active&&chat.descendants.some(a=>isVisibleAgent(a)&&matchesQuery(a,query)));
}
function traySummaryHTML(chat) {
  if(RUNWAY)return '';
  const agents=sortAgents(chat.descendants.filter(isVisibleAgent)),totals=totalStats(agents);
  if(!agents.length)return "";
  const marks=agents.slice(0,5).map(a=>'<span class="stk">'+markFor(a)+'</span>').join("")+(agents.length>5?'<span class="stk-more">+'+(agents.length-5)+'</span>':'');
  return '<div class="sum-line"><span class="stack">'+marks+'</span>'+statStripHTML({...totals,live:false},{live:false})+'</div>';
}

/* ---------- layer 2 ---------- */
function blockerHTML(chat) {
  const command=resumeCommand(chat),resume=command?'<button class="chip" data-action="copy" data-copy="'+esc(command)+'" title="Copy resume command">'+esc(command.slice(0,command.length-String(chat.id).length)+String(chat.id).slice(0,4))+'…'+ICON.copy+ICON.check+'</button>':'';
  if(isAsking(chat))return '<div class="blk t-attn"><span class="blk-text">'+(chat.pendingQuestion?richText(chat.pendingQuestion):'<span class="t-attn">Needs you</span>')+(isCodexChat(chat)?optionsHTML(chat.pendingOptions,chat.id):plainOptionsHTML(chat.pendingOptions))+'</span>'+resume+'</div>';
  if(chat.status==="failed")return '<div class="blk t-'+(recentFail(chat)?"err":"mute")+'"><span class="blk-text">'+esc(chat.failureReason||chat.stateDetail||"failed")+'</span>'+resume+'</div>';
  if(chat.dataError)return '<div class="blk t-mute"><span class="blk-text">'+esc(chat.dataError)+'</span></div>';
  if(chat.status==="unknown")return '<div class="blk t-mute"><span class="blk-text">status unavailable</span></div>';
  return "";
}
function cacheFact(chat) {
  const request=chat.lastRequest||{},input=safeNumber(request.input),cached=safeNumber(request.cached);
  const hit=input>0&&cached!=null?Math.round(cached*100/input)+"%":null;
  const view=warmthView(chat);
  if(view.kind==="running")return {text:["warm",hit].filter(Boolean).join(" · "),tone:""};
  if(view.kind==="warm")return {text:["warm "+view.text,hit].filter(Boolean).join(" · "),tone:""};
  if(view.kind==="cooling")return {text:["warm "+view.text,hit].filter(Boolean).join(" · "),tone:"attn"};
  if(view.kind==="maybe")return {text:"cold?",tone:""};
  if(view.kind==="cold")return {text:"cold",tone:""};
  return {text:"—",tone:""};
}
function panelHTML(chat) {
  if(RUNWAY)return '';
  const pct=safeNumber(chat.contextPercent),tone=contextTone(pct),cache=cacheFact(chat),stats=agentStats(chat);
  const lead=chat.status==="running"&&chat.latestUpdate?'<div class="l2-update" title="'+esc(chat.latestUpdate)+'">'+esc(chat.latestUpdate)+'</div>'
    :chat.status==="quiet"?'<div class="l2-update">no writes '+ageShort(chat.latest)+'</div>':"";
  const facts='<div class="facts"><div><span class="f-l">Usage</span><span class="f-v'+(tone?" t-"+tone:"")+'">'+fmt(chat.contextTokens)+' / '+fmt(chat.contextWindow)+'</span></div>'+
    '<div title="Cache warmth · last request hit rate"><span class="f-l">Cache</span><span class="f-v'+(cache.tone?" t-"+cache.tone:"")+'">'+esc(cache.text)+'</span></div>'+
    '<div title="'+esc(fmt(chat.contextTokens))+' prompt re-billed at full price instead of the 90%-off cached rate"><span class="f-l">If cold</span><span class="f-v">+'+fmt(coldPenalty(chat))+'</span></div></div>';
  const meta=[RUNWAY?runwayModelLabel(chat):modelLabel(chat),chat.reasoningEffort,chat.turns!=null?chat.turns+" turns":null,!RUNWAY&&chat.todayTokens?fmt(chat.todayTokens)+" today":null,chat.compactions?chat.compactions+" compaction"+(chat.compactions===1?"":"s"):null].filter(Boolean).join(" · ");
  const pinned=state.pins.has(chat.key);
  return blockerHTML(chat)+lead+facts+(RUNWAY?"":'<div class="l2-strip">'+statStripHTML(stats,{live:stats.live})+'</div>')+ 
    '<div class="meta">'+esc(meta)+'</div>'+
    '<div class="l2-foot"><span class="path" title="'+esc(chat.workspace||"")+'"><bdi>'+esc(chat.workspace||"")+'</bdi></span>'+
    '<button class="ghost" data-action="pin" data-key="'+esc(chat.key)+'" aria-pressed="'+pinned+'">'+(pinned?"Unpin":"Pin")+'</button>'+
    (resumeCommand(chat)?'<button class="ghost" data-action="copy" data-copy="'+esc(resumeCommand(chat))+'" title="Copy '+esc(resumeCommand(chat))+'">'+ICON.copy+ICON.check+'resume</button>':'')+'</div>';
}

/* ---------- chat list ---------- */
function chatBlockHTML(chat, query) {
  // Fallback markup (tests / no DOM): same structure the keyed patcher builds.
  const open=state.open.has(chat.key);
  const tray=chat.descendants.some(isVisibleAgent)?(RUNWAY?'<div class="tray '+(runwayAgentsExpanded(chat)?'runway-expanded':'runway-collapsed')+'"><div class="tray-sum">'+traySummaryHTML(chat)+'</div><div class="tray-full"><div class="tray-clip">'+(runwayAgentsExpanded(chat)?trayEntries(chat,query).map(e=>'<div class="'+e.cls+'" data-key="'+esc(e.key)+'"'+attrString(e.attrs)+'>'+e.html+'</div>').join(''):'')+'</div></div></div>':trayState(chat)==="resting"?'<div class="tray resting"><div class="tray-sum">'+traySummaryHTML(chat)+'</div></div>'
    :'<div class="tray active"><div class="tray-full">'+trayEntries(chat,query).map(e=>'<div class="'+e.cls+'" data-key="'+esc(e.key)+'"'+attrString(e.attrs)+'>'+e.html+'</div>').join("")+'</div></div>'):"";
  if(RUNWAY)return '<div class="chat" data-key="'+esc(chat.key)+'" data-provider="'+esc(chat.provider)+'"><div class="row">'+rowHTML(chat)+'</div>'+tray+'</div>';
  return '<div class="chat'+(state.pins.has(chat.key)?" pinned":"")+'" data-key="'+esc(chat.key)+'"><button class="row" data-action="toggle-chat" data-key="'+esc(chat.key)+'" aria-expanded="'+open+'" title="'+esc(chat.title||"")+'">'+rowHTML(chat)+'</button>'+
    (open?'<div class="l2 open"><div class="l2-in">'+panelHTML(chat)+'</div></div>':'')+tray+'</div>';
}
function emptyHTML(forest, providers) {
  if(COMPACT)return parseQuery(state.query).active?'<div class="empty">No matches</div>':'';
  if(!forest.rows.length) {
    const down=providers.filter(p=>p.connected===false);
    return '<div class="empty">'+(down.length?"No data":"No "+esc(providers.map(p=>p.name).join(" or "))+" chats")+'</div>';
  }
  if(parseQuery(state.query).active)return '<div class="empty">No matches</div>';
  const next=nextWindowWithChats(forest);
  return '<div class="empty">Nothing in '+state.windowDays+'d'+(next?'<button class="ghost" data-action="window" data-days="'+next.days+'">'+next.days+'d · '+next.count+' chat'+(next.count===1?"":"s")+'</button>':'')+'</div>';
}
function updateChatEl(el, chat, query) {
  const [row,l2,tray]=el.children,inner=l2.firstElementChild;
  const pinned=!RUNWAY&&state.pins.has(chat.key);
  el.classList.toggle("pinned",pinned);
  if(RUNWAY&&el.dataset.provider!==chat.provider)el.dataset.provider=chat.provider;
  const html=rowHTML(chat);
  if(row._html!==html) {
    const next=rollup(chat).key,prevState=el.dataset.state;
    if(RUNWAY)runwayPatchContent(row,html);else row.innerHTML=html;row._html=html;syncBreath(row);
    if(!RUNWAY&&prevState&&prevState!==next&&!reducedMotion()){row.classList.remove("changed");void row.offsetWidth;row.classList.add("changed");}
    if(RUNWAY&&REFINED&&prevState&&prevState!==next)runwayMotionOnce(el,'state-changed','runway-status-change');
    el.dataset.state=next;
  }
  const previousQuestionToken=el.dataset.questionToken;
  const questionToken=RUNWAY&&runwayWaiting(chat)?questionDismissToken(chat):'';
  if(RUNWAY&&REFINED&&state.runwaySnapshotPainted&&previousQuestionToken!==undefined&&questionToken&&previousQuestionToken!==questionToken)
    runwayMotionOnce(el,'question-arrived','runway-question-arrival','.runway-refined-title>.r-title,.runway-refined-title>.runway-agent-name,.runway-refined-status .runway-state-glyph');
  if(RUNWAY)el.dataset.questionToken=questionToken;
  row.title=chat.title||"";
  const open=!RUNWAY&&state.open.has(chat.key);
  if(!RUNWAY)row.setAttribute("aria-expanded",String(open));
  if(open) {
    const panel=panelHTML(chat);
    if(inner._html!==panel){if(RUNWAY)runwayPatchContent(inner,panel);else inner.innerHTML=panel;inner._html=panel;syncBreath(inner);}
    if(!l2.classList.contains("open")){clearTimeout(l2._timer);void l2.offsetHeight;l2.classList.add("open");}
  } else if(l2.classList.contains("open")) {
    l2.classList.remove("open");clearTimeout(l2._timer);
    l2._timer=setTimeout(()=>{if(!l2.classList.contains("open")){inner.innerHTML="";inner._html="";}},260);
  }
  // Agent tray: always visible; resting trays collapse to one summary line and expand when the tree wakes.
  const has=chat.descendants.some(isVisibleAgent),mode=has?trayState(chat):"none";
  tray.hidden=!has;
  if(!has)return;
  const [sum,full]=tray.children;
  if(RUNWAY){
    const expanded=runwayAgentsExpanded(chat);
    if(tray.dataset.previewExpanded!==String(expanded)){tray.dataset.previewExpanded=String(expanded);tray.className="tray "+(expanded?"runway-expanded":"runway-collapsed");}
    const summary=traySummaryHTML(chat);
    if(sum.firstElementChild._html!==summary){runwayPatchContent(sum.firstElementChild,summary);sum.firstElementChild._html=summary;}
  patchKeyed(full.firstElementChild,expanded?trayEntries(chat,query):[],{enter:el=>{if(state.runwaySnapshotPainted)runwayArrivalEnter(el);},exit:runwayExit});
    return;
  }
  if(tray.dataset.mode!==mode){tray.dataset.mode=mode;if(tray._init)void tray.offsetHeight;tray.className="tray "+mode;}
  tray._init=true;
  const summary=traySummaryHTML(chat);
  if(sum.firstElementChild._html!==summary){sum.firstElementChild.innerHTML=summary;sum.firstElementChild._html=summary;}
    patchKeyed(full.firstElementChild,mode==="active"||full.firstElementChild.children.length?trayEntries(chat,query):[],{exit:runwayExit});
}
// Attention per source, independent of which forest is on screen. A new run or waiting question in an
// unselected source flags that segment (never switches); selecting the source clears the flag.
function trackSourceAttention(name, items, selected, fresh) {
  const base=state.sourceBase.get(name)||new Map();
  if(!fresh)return;
  // Ongoing work in a source that isn't on screen keeps a quieter live dot on its segment.
  if(!selected&&items.some(item=>item.active))state.sourceLive.add(name);else state.sourceLive.delete(name);
  let newWork=false;
  for(const item of items){
    if(!item.active)continue;
    const previous=base.get(item.key);
    const restarted=Boolean(previous?.active&&previous.runStartedAt&&item.runStartedAt&&previous.runStartedAt!==item.runStartedAt);
    const questionArrived=Boolean(item.waiting&&(!previous?.waiting||previous.questionToken!==item.questionToken));
    if(!previous||!previous.active||restarted||questionArrived)newWork=true;
  }
  state.sourceBase.set(name,new Map(items.map(item=>[item.key,{active:item.active,runStartedAt:item.runStartedAt,waiting:item.waiting,questionToken:item.questionToken}])));
  if(selected||!items.some(item=>item.active))state.sourceAttention.delete(name);
  else if(newWork)state.sourceAttention.add(name);
}
function updateSourceSwitch() {
  if(!RUNWAY||PHONE)return;
  const group=$('#source-switch');if(!group){window.TokenTrackerDashboard?.setSource?.(effectiveSource(state.snapshot));return;}
  const policyReady=validPublicProviderPolicy(state.snapshot?.providerPolicies)||state.providerPolicyReady,names=policyReady?sourceNames(state.snapshot):SOURCES,visible=names.length>=2;
  const current=state.snapshot?effectiveSource(state.snapshot):policyReady&&names.includes(state.source)?state.source:'Codex';
  if(group.hidden!==!visible)group.hidden=!visible;
  group.setAttribute?.('aria-busy',String(!policyReady));
  const segments=[...(document.querySelectorAll?.('#source-switch [data-source]')||[])];
  for(const segment of segments){
    const name=segment.dataset?.source,enabled=policyReady&&names.includes(name),pressed=String(name===current),flagged=visible&&name!==current&&state.sourceAttention.has(name);
    segment.disabled=!enabled;
    segment.setAttribute?.('aria-disabled',String(!enabled));
    if(segment.getAttribute?.('aria-pressed')!==pressed)segment.setAttribute?.('aria-pressed',pressed);
    const live=visible&&name!==current&&!flagged&&state.sourceLive.has(name);
    segment.classList?.toggle?.('has-attention',flagged);
    segment.classList?.toggle?.('has-live',live);
    const label=name+(flagged?' (needs attention)':live?' (active)':'');
    if(segment.getAttribute?.('aria-label')!==label)segment.setAttribute?.('aria-label',label);
    const dot=segment.querySelector?.('.source-dot');dot?.setAttribute?.('aria-hidden',String(!flagged&&!live));
  }
  const body=document.body;if(!(RUNWAY&&!PHONE)&&body?.dataset&&body.dataset.source!==current)body.dataset.source=current;
  window.TokenTrackerDashboard?.setSource?.(current);
}
function setSource(name,{persist=true,manual=true}={}) {
  if(!RUNWAY||PHONE||!SOURCES.includes(name)||!policyForSources(state.snapshot)||!sourceNames(state.snapshot).includes(name))return false;
  if(manual){state.sourceManualAt=Date.now();if(persist)try{localStorage.setItem(SOURCE_KEY,name);localStorage.setItem(SOURCE_MANUAL_AT_KEY,String(state.sourceManualAt));}catch{}}
  state.source=name;state.sourceAttention.delete(name);updateSourceSwitch();
  if(!state.snapshot)renderQuotaState();
  return true;
}
function captureSourceSnapshot(list) {
  if(!list?.innerHTML||!list.appendChild||reducedMotion()||typeof document.createElement!=="function")return null;
  const snapshot=document.createElement("div");snapshot.className="source-switch-snapshot";
  snapshot.innerHTML=[...list.children].filter(child=>!child.classList?.contains("source-switch-snapshot")).map(child=>child.outerHTML).join("");
  snapshot.setAttribute("aria-hidden","true");snapshot.inert=true;
  const computed=window.getComputedStyle?.(document.body);
  for(const property of ["--source-accent","--source-accent-soft","--source-hover","--source-focus","--source-muted","--source-glow-primary","--source-glow-secondary","--source-edge","--source-edge-strong","--blue","--blue-soft"]){
    const value=computed?.getPropertyValue?.(property)?.trim();if(value)snapshot.style.setProperty(property,value);
  }
  snapshot.removeAttribute("id");for(const element of snapshot.querySelectorAll?.("[id]")||[])element.removeAttribute("id");
  return snapshot;
}
function revealSourceSnapshot(list,snapshot,done) {
  if(!snapshot||!list?.appendChild)return;
  for(const previous of list.querySelectorAll?.(".source-switch-snapshot")||[])previous.remove();
  const finish=()=>{snapshot.removeEventListener?.("animationend",finish);snapshot.removeEventListener?.("animationcancel",finish);snapshot.remove?.();done?.();};
  snapshot.addEventListener?.("animationend",finish);snapshot.addEventListener?.("animationcancel",finish);
  list.appendChild(snapshot);
  const reveal=()=>snapshot.classList.add("is-fading");
  if(typeof requestAnimationFrame==="function")requestAnimationFrame(reveal);else reveal();
}
function selectSourceForNewWork(items) {
  // Fresh work determines the auto-open source; only an explicit active inspection locks the current view.
  if(!COMPACT||!state.snapshot||notch.manual||notch.keyboardInspecting||state.searchOpen||state.popover||(widgetLock.locked&&widgetLock.expanded))return false;
  const available=sourceNames(state.snapshot),candidates=(items||[]).filter(item=>available.includes(item.provider));
  if(!candidates.length)return false;
  candidates.sort((a,b)=>Number(Boolean(b.waiting))-Number(Boolean(a.waiting))||(b.activityAt||0)-(a.activityAt||0));
  const chosen=candidates[0]?.provider;
  if(!chosen||chosen===effectiveSource(state.snapshot))return false;
  return setSource(chosen,{persist:false,manual:false});
}
function renderChats(providers) {
  scheduleNotchSize();
  const previousForest=state.forest,forest=buildForest(providers,state.snapshot?.graph);stabilizeRunwayForest(forest,previousForest);
  const roots=visibleRoots(forest),query=parseQuery(state.query);
  if(RUNWAY&&state.rosterQuery!==state.query){state.agentSearchClosed.clear();state.rosterQuery=state.query;}
  state.forest=forest;
  if(RUNWAY&&state.popover?.dataset?.action==='runway-question'&&!runwayCurrentEvidenceFresh())closePopover({restoreFocus:true});
  if(RUNWAY&&state.popover?.dataset?.action==='runway-question'){
    const source=forest.rows.find(item=>item.key===state.popover.dataset.key);
    if(!source||!runwayWaiting(source)||!source.pendingQuestion||state.popover.__questionId!==(source.pendingQuestionId??source.questionId??null)||state.popover.__questionAt!==(source.pendingQuestionAt||null)||state.popover.__questionText!==source.pendingQuestion||state.popover.__questionOptions!==JSON.stringify(source.pendingOptions||[]))closePopover({restoreFocus:true});
  }
  if(RUNWAY){
    const counts=runwayCounts(forest);
    if(PHONE)renderRunwayCounts(counts);
    else {
    notch.counts=counts;
    const activeRoots=forest.roots.filter(chat=>state.showHidden||!state.hiddenChats.has(chat.key));
    // Trigger reveals for active lifecycle transitions and new parent messages,
    // not for every transcript or token update on an already-running chat.
      const evidenceFresh=runwayCurrentEvidenceFresh();
      const itemsOf=roots=>roots.flatMap(chat=>[chat,...chat.descendants]).map(chat=>({
        key:chat.key,provider:chat.provider,active:evidenceFresh&&runwayActive(chat)&&!chat.telemetryUnavailable&&chat.statusSource!=='unavailable',subagent:hasTreeParent(chat),
        waiting:runwayWaiting(chat),questionToken:runwayWaiting(chat)?questionDismissToken(chat):null,questionId:runwayWaiting(chat)?chat.key+'|'+(chat.pendingQuestionId??chat.questionId??chat.pendingQuestion??''):null,
      runStartedAt:stamp(chat.activeRunStartedAt)||(chat.telemetryUnavailable?notch.workStates.get(chat.key)?.runStartedAt:0),latestMessageAt:stamp(chat.latestMessageAt),latestMessageDirection:chat.latestMessageDirection,
      activityAt:stamp(chat.activityAt)||Math.max(stamp(chat.updatedAt),stamp(chat.latestMessageAt),stamp(chat.statusObservedAt),stamp(chat.executionObservedAt),stamp(chat.activity?.observedAt),stamp(chat.executionActivity?.observedAt)),
    }));
      const activeIn=roots=>evidenceFresh?roots.filter(chat=>[chat,...chat.descendants].some(item=>runwayActive(item)&&!item.telemetryUnavailable&&item.statusSource!=='unavailable')).length:0;
      const selectedItems=itemsOf(activeRoots),activeCount=activeIn(activeRoots),workItems=selectedItems;
      for(const provider of providers)trackSourceAttention(provider.name,selectedItems.filter(item=>item.provider===provider.name),true,evidenceFresh);
      if(state.liveRecoveryPending){
        // Recovery baselines replayed state, but a question never seen before the gap must still reveal.
        for(const item of workItems){const previous=notch.workStates.get(item.key);if(previous?.active&&item.active){previous.latestMessageAt=item.latestMessageAt;if(!item.questionId||state.seenQuestions.has(item.questionId)){previous.waiting=item.waiting;previous.questionToken=item.questionToken;}}}
        state.liveRecoveryPending=false;
      }
      if(evidenceFresh)for(const item of workItems)if(item.questionId){if(state.seenQuestions.size>500)state.seenQuestions.clear();state.seenQuestions.add(item.questionId);}
      syncNotch(activeCount,workItems);
      updateSourceSwitch();
    const available=activeCount>0||state.hiddenChats.size>0||COMPACT&&(keepOpenState.value||state.searchOpen||Boolean(state.popover)||compactHasFocus());
    const header=$('#chats-head'),searchToggle=$('#search-toggle');
    if(header)header.hidden=!available;if(searchToggle)searchToggle.hidden=!available;
    if(!available){state.query='';state.searchOpen=false;const input=$('#chat-search');if(input)input.value='';header?.classList?.remove('searching');}
    }
  }
  const hidden=forest.roots.length-roots.length;
  const count=$("#chat-count");
  if(count){count.textContent=String(roots.length);count.title=hidden>0&&!query.active?hidden+" older hidden":"";}
  const result=$("#result-count");if(result)result.textContent=roots.length+" / "+forest.roots.length;
  const hiddenToggle=$('#hidden-chats-toggle');if(RUNWAY&&hiddenToggle){const hiddenCount=state.hiddenChats.size;hiddenToggle.hidden=!hiddenCount;hiddenToggle.textContent=(state.showHidden?'Hide hidden':'Show hidden')+' ('+hiddenCount+')';hiddenToggle.setAttribute('aria-pressed',String(state.showHidden));}
  const warm=providers.find(p=>p.cacheWarmth)?.cacheWarmth,rule=$("#warm-rule");
  if(rule){rule.textContent=warm?"warm ≤"+Math.round(warm.warmSeconds/60)+"m":"";rule.title=warm?"Reply within "+Math.round(warm.warmSeconds/60)+"m to keep the cache · cold after "+runSpan(warm.coldSeconds).replace(" 00m","h").replace(/^(\d+)h (\d+)m$/,"$1h $2m")+(warm.learned?" · learned from "+warm.samples+" resumes":""):"";}
  const seg=$("#window-seg");if(seg?.style)seg.style.setProperty("--i",String(WINDOWS.indexOf(state.windowDays)));
  document.querySelectorAll?.("[data-days]").forEach(b=>b.setAttribute?.("aria-pressed",String(Number(b.dataset.days)===state.windowDays)));
  const list=$("#chat-list");if(!list)return;
  const searchCanShowEmpty=COMPACT&&state.searchOpen&&query.active;
  if(!realDom(list)) {
    const html=roots.length?roots.map(c=>chatBlockHTML(c,query)).join(""):RUNWAY&&!PHONE&&!notch.active&&!searchCanShowEmpty?'':emptyHTML(forest,providers);
    if(list.innerHTML!==html)list.innerHTML=html;
    return;
  }
  if(!roots.length){
    const noActive=RUNWAY&&!PHONE&&!notch.active&&!searchCanShowEmpty,html=noActive?'':emptyHTML(forest,providers);
    if(list._empty!==html){
      for(const el of [...list.children])if(el.classList.contains('chat')){if(!el._leaving)runwayExit(el);}else el.remove();
      if(html)list.insertAdjacentHTML?.('beforeend',html);list._empty=html;
    }
    state.lastOrder=[];return;
  }
  if(list._empty!==null&&list._empty!==undefined){for(const el of [...list.children])if(!el.classList.contains('chat'))el.remove();list._empty=null;}
  [...list.children].forEach(el=>{if(!el.classList.contains("chat"))el.remove();});
  // Search auto-opens nothing; a subagent match is visible in the always-on tray and outlined there.
  const byKey=new Map(roots.map(c=>[c.key,c]));
  let order=roots.map(c=>c.key);
  // Keep established rows stable while the user points, focuses, or reads a question.
  const focused=list.contains?.(document.activeElement),readingQuestion=Boolean(state.popover&&state.popover.dataset?.action==='runway-question');
  if((state.pointerInList||focused||readingQuestion)&&state.lastOrder){const kept=state.lastOrder.filter(k=>byKey.has(k));order=[...kept,...order.filter(k=>!kept.includes(k))];}
  const existing=new Map([...list.children].filter(el=>!el._leaving).map(el=>[el.dataset.key,el]));
  const scroller=RUNWAY?list.closest?.('.layout'):list;
  const scrollTopBefore=scroller?.scrollTop||0,scrollRect=scroller?.getBoundingClientRect?.();
  const anchor=[...existing].find(([key,el])=>byKey.has(key)&&(()=>{const rect=el.getBoundingClientRect?.();return rect&&(!scrollRect||rect.bottom>scrollRect.top);})())?.[1];
  const anchorTop=anchor?.getBoundingClientRect?.().top;
  for(const [k,el] of existing)if(!byKey.has(k))runwayExit(el);
  let prev=null;
  for(const k of order) {
    let el=existing.get(k);
    const fresh=!el;
    if(!el){el=document.createElement("div");el.className="chat";el.dataset.key=k;
      el.innerHTML=(RUNWAY?'<div class="row"></div>':'<button class="row" data-action="toggle-chat" data-key="'+esc(k)+'"></button>')+'<div class="l2"><div class="l2-in"></div></div><div class="tray"><div class="tray-sum"><div class="tray-clip"></div></div><div class="tray-full"><div class="tray-clip"></div></div></div>';}
    updateChatEl(el,byKey.get(k),query);
    if(fresh&&RUNWAY&&!PHONE&&state.runwaySnapshotPainted&&!state.sourceSwitching)runwayArrivalEnter(el);
    const next=prev?prev.nextSibling:list.firstChild;
    if(next!==el)list.insertBefore(el,next);
    prev=el;
  }
  state.lastOrder=order;
  if(anchor&&anchor.isConnected&&Number.isFinite(anchorTop)&&scroller){const moved=anchor.getBoundingClientRect().top-anchorTop;if(Math.abs(moved)>1)scroller.scrollTop=scrollTopBefore+moved;}
}

/* ---------- footer, title bar, popover ---------- */
function renderTotals(providers) {
  if(RUNWAY){const node=$('#totals');if(node){node.innerHTML='';node.hidden=true;}return;}
  const node=$("#totals");if(!node)return;
  const cells=[["Today",1],["7d",7],["30d",30]].map(([label,days])=>'<span class="tt"><span>'+label+'</span> <b>'+fmt(knownSum(providers.map(p=>connectedActivity(p,days))))+'</b></span>').join("");
  const savings=knownSum(providers.map(p=>p.connected===false?null:safeNumber(p.cache?.cacheSavings)));
  const cache=providers.find(p=>p.cache)?.cache;
  const tip=cache?(cache.percent!=null?cache.percent+"% hit · ":"")+fmt(cache.read)+" cached · "+fmt(cache.uncachedInput)+" fresh · "+fmt(cache.output)+" out"+(cache.priceBasis?" · "+cache.priceBasis:""):"";
  const html=(RUNWAY?"":cells)+'<span class="tt tt-saved" title="'+esc(tip)+'"><span>'+(RUNWAY?"cache est.":"saved")+'</span> <b>'+money(savings)+'</b></span>';
  if(node.innerHTML!==html)node.innerHTML=html;
}
function analyticsHTML(snapshot) {
  const provider=(snapshot?.providers||[]).find(item=>item.name==="Codex");
  const connected=provider&&provider.connected!==false;
  const chats=connected&&Array.isArray(provider.chats)?provider.chats:[];
  const contexts=chats.filter(chat=>safeNumber(chat.contextPercent)!=null).sort((a,b)=>safeNumber(b.contextPercent)-safeNumber(a.contextPercent));
  const peak=contexts[0],cache=connected?provider.cache:null;
  const models=connected&&Array.isArray(provider.modelUsage)?provider.modelUsage.filter(item=>item&&typeof item.model==="string").sort((a,b)=>(safeNumber(b.tokens)||0)-(safeNumber(a.tokens)||0)):[];
  const total=connected?safeNumber(provider.tracked31Days):null;
  const coverage=provider?.modelUsageNote||provider?.activityNote||"Local session telemetry; coverage depends on observed token events.";
  const contextValue=peak?safeNumber(peak.contextPercent)+"%":connected?"unavailable":"offline";
  let advice="";
  if(peak&&safeNumber(peak.contextPercent)>=85)advice='<div class="an-advice">'+esc((peak.title||"A chat")+" is at "+Math.round(safeNumber(peak.contextPercent))+"% of its reported context. Compact or start a fresh chat before adding more context.")+'</div>';
  else if(cache&&safeNumber(cache.percent)!=null&&safeNumber(cache.percent)<50)advice='<div class="an-advice">Only '+Math.round(safeNumber(cache.percent))+"% of recorded input was cache reads. Reusing a stable prompt prefix may improve reuse; this is a suggestion, not a measured savings guarantee.</div>";
  else if(provider?.cacheWarmth?.learned&&safeNumber(provider.cacheWarmth.warmSeconds)>0)advice='<div class="an-advice">Observed cache warmth is about '+Math.round(provider.cacheWarmth.warmSeconds/60)+'m after a request ('+fmt(provider.cacheWarmth.samples)+' resume samples). Resume within that window to preserve more prompt reuse.</div>';
  const modelHTML=models.length?'<div class="an-models">'+models.slice(0,4).map(model=>{
    const tokens=safeNumber(model.tokens),requests=safeNumber(model.observedRequests),sub=safeNumber(model.subagentTokens),subRequests=safeNumber(model.subagentRequests);
    const share=tokens>0&&sub!=null?Math.round(sub*100/tokens)+"% subagent tokens":sub===0?"0% subagent tokens":"subagent split unavailable";
    const requestShare=requests>0&&subRequests!=null?" · "+Math.round(subRequests*100/requests)+"% subagent requests":"";
    return '<div class="an-model"><span class="an-model-name" title="'+esc(model.model)+'">'+esc(shortModel(model.model))+'</span><span class="an-model-tokens">'+fmt(tokens)+' tok</span><span class="an-model-meta">'+(requests==null?"— requests observed":fmt(requests)+" requests observed")+' · '+share+requestShare+'</span></div>';
  }).join("")+ '</div><div class="an-note">'+esc(provider.modelUsageNote||"Model totals count local token-counter deltas; first-seen baselines may be incomplete.")+'</div>':'<div class="an-empty">Model and subagent totals unavailable. They appear after local token-counter events are recorded.</div>';
  const html='<button class="an-head" data-action="analytics" aria-expanded="'+state.analyticsOpen+'" aria-controls="analytics-panel"><span class="an-title">Analytics</span><span class="an-range">local · 31d</span></button>'+ 
    '<div id="analytics-panel" class="an-panel"'+(state.analyticsOpen?"":" hidden")+'><div class="an-grid">'+
    '<div class="an-card" title="Recorded local session token counters; may omit usage before a session first appeared"><span class="an-label">Tracked tokens</span><span class="an-value">'+fmt(total)+'</span></div>'+ 
    '<div class="an-card" title="Highest current window usage among returned Codex chats; not a historical peak"><span class="an-label">Usage now · '+contexts.length+' sampled</span><span class="an-value">'+esc(contextValue)+'</span></div>'+ 
    '<div class="an-card" title="Share of recorded input represented by cache reads"><span class="an-label">Cache reuse</span><span class="an-value">'+(cache&&safeNumber(cache.percent)!=null?Math.round(cache.percent)+"% · "+fmt(cache.read)+" tokens":"unavailable")+'</span></div>'+ 
    '<div class="an-card" title="List-price estimate from observed cache counters; not billed cost"><span class="an-label">Estimated cache savings</span><span class="an-value">'+(cache&&safeNumber(cache.cacheSavings)!=null?money(cache.cacheSavings):"unavailable")+'</span></div></div>'+ 
    '<div class="an-note">'+esc(coverage)+(provider?.source?" Source: "+esc(provider.source)+".":"")+'</div>'+advice+modelHTML+'</div>';
  return html;
}
function renderAnalytics(snapshot) { const node=$("#analytics");if(node&&!node.hidden){const html=analyticsHTML(snapshot);if(node.innerHTML!==html)node.innerHTML=html;} }
function syncCompactClock(){if(!COMPACT)return;const localTime=compactClockFormatter.format(new Date());for(const node of [$('#compact-clock'),$('#toolbar-clock')])if(node&&node.textContent!==localTime)node.textContent=localTime;}
const SNAPSHOT_FAILURE_CATEGORIES=new Set(['timeout','io_error','invalid_snapshot','worker_start_failed','build_failed']);
function isoObservationTime(value,now=Date.now()){
  if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/i.test(value))return null;
  const parsed=Date.parse(value);return Number.isFinite(parsed)&&parsed>0&&parsed<=now?parsed:null;
}
function validateSnapshotCache(snapshot,now=Date.now()){
  if(!Object.prototype.hasOwnProperty.call(snapshot||{},'snapshotCache'))return {present:false,valid:false,metadata:null,builtAt:null};
  const metadata=snapshot.snapshotCache;
  if(!metadata||typeof metadata!=='object'||Array.isArray(metadata))return {present:true,valid:false,metadata:null,builtAt:null};
  const builtAt=isoObservationTime(metadata.builtAt,now),age=metadata.ageSeconds;
  const failureAt=metadata.lastFailureAt===null?null:isoObservationTime(metadata.lastFailureAt,now);
  const category=metadata.lastFailureCategory;
  const valid=Number.isSafeInteger(metadata.generation)&&metadata.generation>=0&&builtAt!==null&&typeof age==='number'&&Number.isFinite(age)&&age>=0&&typeof metadata.building==='boolean'&&typeof metadata.stale==='boolean'&&(metadata.lastFailureAt===null||failureAt!==null)&&(category===null||SNAPSHOT_FAILURE_CATEGORIES.has(category));
  return {present:true,valid,metadata:valid?metadata:null,builtAt:valid?builtAt:null};
}
function acceptSnapshotCache(snapshot,now=Date.now()){
  const result=validateSnapshotCache(snapshot,now);
  state.snapshotCacheInvalid=result.present&&!result.valid;
  state.snapshotCache=result.valid?result.metadata:null;
  if(!result.present){state.refreshedAt=now;return {kind:'legacy',advanced:true};}
  if(!result.valid)return {kind:'invalid',advanced:false};
  if(!result.metadata.stale)state.refreshedAt=Math.max(state.refreshedAt||0,result.builtAt);
  return {kind:'metadata',advanced:!result.metadata.stale,builtAt:result.builtAt};
}
function refreshControlTitle(){
  const parts=[];if(state.refreshing||state.loading||state.initialBootstrapPending)parts.push('Loading usage');
  parts.push(state.refreshedAt?'Last successful refresh at '+clock(state.refreshedAt):'Refresh usage');
  const cache=state.snapshotCache;
  if(state.snapshotCacheInvalid)parts.push('Snapshot metadata unavailable');
  else if(cache){if(cache.stale)parts.push('Snapshot stale · '+runSpan(cache.ageSeconds));if(cache.building)parts.push('Snapshot building');if(cache.lastFailureCategory)parts.push('Snapshot build failed: '+cache.lastFailureCategory);}
  const quota=quotaBootstrap.metadata;
  if(quota){if(!quota.ready)parts.push(quota.refreshing?'Quota sample loading':'Quota sample unavailable');else if(!quota.fresh)parts.push('Quota sample stale'+(quota.ageSeconds==null?'':' · '+runSpan(quota.ageSeconds)));if(quota.refreshing)parts.push('Quota refresh in progress');if(quota.errorCategory)parts.push('Quota refresh failed: '+quota.errorCategory);}
  if(quotaBootstrap.fetchError)parts.push('Quota bootstrap failed '+ageShort(quotaBootstrap.fetchError.at)+' ago');
  if(state.watcherFetchError)parts.push('Greeter status fetch failed '+ageShort(state.watcherFetchError.at)+' ago');
  return parts.join(' · ');
}
function renderTitle(providers) {
  const name=$("#brand-name");if(name)name.textContent=RUNWAY&&!PHONE?"Branchglance":providers.map(p=>p.name).join(" · ")||"Codex";
  syncCompactClock();
  const age=$("#sync-age");if(!age)return;
  if(state.refreshing||state.loading||state.initialBootstrapPending){if(COMPACT){if(age.innerHTML!==RUNWAY_REFRESH_ICON)age.innerHTML=RUNWAY_REFRESH_ICON;age.title=refreshControlTitle();}else age.textContent="";age.className="sync-age is-loading";age.setAttribute?.("aria-busy","true");return;}
  if(RUNWAY){if(age.innerHTML!==RUNWAY_REFRESH_ICON)age.innerHTML=RUNWAY_REFRESH_ICON;age.title=REFINED&&!PHONE?refreshControlTitle():(state.lastOkAt?"Last successful refresh at "+clock(state.lastOkAt):"Refresh usage");age.className="sync-age";age.setAttribute?.("aria-busy","false");return;}
  age.className="sync-age";age.setAttribute?.("aria-busy","false");
  if(!state.lastOkAt){age.textContent=state.failSince?"offline":"—";age.className="sync-age"+(state.failSince?" t-err":"");return;}
  const seconds=Math.floor((Date.now()-state.lastOkAt)/1000);
  age.textContent=seconds<10?"now":seconds<60?seconds+"s":"stale "+ageShort(state.lastOkAt);
  age.className="sync-age"+(seconds>=60?" t-attn":"");
}
const PHASES={waiting_for_exhaustion:"waiting for limit",armed_waiting_for_reset:"armed for reset",reset_observed_sending:"sending",needs_login:"sign-in expired",blocked_weekly:"held · weekly used",greeting_sent:"greeting sent",keepalive_scheduled:"scheduled",greeting_verifying:"verifying"};
function applyIdentityPayload(payload) {
  state.policyWarnings=Array.isArray(payload?.policyWarnings)?payload.policyWarnings.filter(value=>typeof value==='string'&&value.trim()).map(value=>value.trim().slice(0,160)):[];
  return state.policyWarnings;
}
function policyWarningHTML() {
  const warning=esc(state.policyWarnings.join(' · '));
  return '<p class="policy-notice" data-policy-warning'+(warning?'':' hidden')+'>'+warning+'</p>';
}
async function loadIdentityPolicyWarnings(anchor) {
  try {
    const response=await fetch('/api/identity',{cache:'no-store'});
    if(!response.ok)return;
    applyIdentityPayload(await response.json());
    const pop=$('#popover');if(!pop||pop.hidden||state.popover!==anchor)return;
    const note=pop.querySelector?.('[data-policy-warning]');
    if(note){note.textContent=state.policyWarnings.join(' · ');note.hidden=!state.policyWarnings.length;}
  } catch {}
}
function watcherPopoverHTML(name) {
  const watcher=state.watcher,w=watcher?.providers?.[name];
  if(!watcher||!w)return (RUNWAY?'':'<dl class="pop-facts"><dt>phase</dt><dd>unavailable</dd></dl>')+policyWarningHTML()+'<button class="ghost" data-action="reset-settings">Reset action settings</button>';
  const next=safeNumber(w.nextGreetingAt),last=stamp(w.lastTriggerAt),verified=w.verificationEvidence==="usage_and_deadline"&&last&&stamp(w.lastVerifiedAt)>=last;
  const phase=!watcher.enabled?"off":!watcher.running?"stopped":PHASES[w.phase]||w.phase||(RUNWAY?"":"watching");
  const err=watcher.persistenceError||w.lastPollError||w.lastError;
  if(RUNWAY){const rows=[];if(phase)rows.push(['phase',phase]);if(next)rows.push(['next',dayWord(next*1000)+' '+clock(next*1000)]);if(last)rows.push(['last',clock(last)+(verified?' <span class="t-live">✓</span>':'')]);const fails=safeNumber(w.failedAttempts);if(fails!=null)rows.push(['fails',String(fails)]);return (rows.length?'<dl class="pop-facts">'+rows.map(([label,value])=>'<dt>'+label+'</dt><dd>'+value+'</dd>').join('')+(err?'<dt>error</dt><dd class="t-err" title="'+esc(err)+'">'+esc(err)+'</dd>':'')+'</dl>':'')+policyWarningHTML()+'<button class="ghost" data-action="reset-settings">Reset action settings</button>';}
  return '<dl class="pop-facts"><dt>phase</dt><dd>'+esc(phase)+'</dd><dt>next</dt><dd>'+(next?dayWord(next*1000)+" "+clock(next*1000):"—")+'</dd>'+ 
    '<dt>last</dt><dd>'+(last?clock(last)+(verified?' <span class="t-live">✓ verified</span>':" unverified"):"—")+'</dd><dt>fails</dt><dd'+(w.failedAttempts?' class="t-err"':'')+'>'+(w.failedAttempts||0)+'</dd>'+ 
    (err?'<dt>error</dt><dd class="t-err" title="'+esc(err)+'">'+esc(err)+'</dd>':'')+'</dl>'+
    (err&&watcher.logPath?'<button class="ghost" data-action="copy" data-copy="'+esc(watcher.logPath)+'">'+ICON.copy+ICON.check+'log</button>':'')+
    policyWarningHTML()+'<button class="ghost" data-action="reset-settings">Reset action settings</button>';
}
function openPopover(html, anchor) {
  const pop=$("#popover");if(!pop)return;
  cleanupPopoverPlacement(pop);
  pop.innerHTML=html;pop.hidden=false;
  state.popover=anchor;
  const place=()=>{const r=anchor.getBoundingClientRect?.(),w=pop.offsetWidth||240,h=pop.offsetHeight||0;if(r){pop.style.left=Math.max(8,Math.min(window.innerWidth-w-8,r.left))+"px";pop.style.top=Math.max(8,Math.min(window.innerHeight-h-8,r.bottom+6))+"px";}};
  place();
  if(anchor?.dataset?.action==='runway-question'){
    const scroller=anchor.closest?.('.layout')||$('.layout');
    pop.__questionResize=place;window.addEventListener?.('resize',place);scroller?.addEventListener?.('scroll',place,{passive:true});
    pop.__questionCleanup=()=>{window.removeEventListener?.('resize',place);scroller?.removeEventListener?.('scroll',place);};
    pop.querySelector?.('.runway-question-close')?.focus?.();
  }
}
function cleanupPopoverPlacement(pop) { if(!pop)return;if(typeof pop.__questionCleanup==='function')pop.__questionCleanup();pop.__questionCleanup=null;pop.__questionResize=null; }
function restoreQuestionFocus(anchor) {
  const key=anchor?.dataset?.key;
  setTimeout(()=>{
    const usable=anchor?.isConnected&&!anchor.closest?.('[inert]');
    if(usable){anchor.focus?.({preventScroll:true});return;}
    const current=key?[...document.querySelectorAll?.('[data-action="runway-question"]')||[]].find(item=>item.dataset.key===key):null;
    const target=current&&!current.closest?.('[inert]')?current:$('#sync-age');target?.focus?.({preventScroll:true});
  },0);
}
function closePopover({restoreFocus=false,updateNotch=true}={}) {
  const pop=$("#popover");if(!pop||pop.hidden)return;
  const anchor=state.popover,wasQuestion=anchor?.dataset?.action==='runway-question';
  cleanupPopoverPlacement(pop);pop.hidden=true;state.popover=null;
  if(wasQuestion&&updateNotch){notch.manual=notch.keyboardInspecting;if(notch.keyboardInspecting||notch.inspecting)setNotch(true);else if(notch.autoExpanded&&!notch.autoClosePending)setNotch(true);else setNotch(false);}
  if(notch.autoClosePending&&updateNotch){notch.autoClosePending=false;notch.autoExpanded=false;if(notch.keyboardInspecting||notch.inspecting)setNotch(true);else setNotch(false);}
  if(restoreFocus&&wasQuestion)restoreQuestionFocus(anchor);
}

/* ---------- render ---------- */
function renderStripNow() { if(state.snapshot)renderStrip(attentionItems(state.forest,limitProvidersFor(state.snapshot))); }
function initialActiveSource(snapshot){
  if(!COMPACT||!runwayCurrentEvidenceFresh())return null;
  const candidates=[];
  for(const provider of snapshot?.providers||[]){
    if(!sourceNames(snapshot).includes(provider.name))continue;
       const forest=buildForest([provider],snapshot?.graph);
    for(const root of forest.roots){
      if(!state.showHidden&&state.hiddenChats.has(root.key))continue;
      for(const chat of [root,...root.descendants]){
        if(!runwayActive(chat)||chat.telemetryUnavailable||chat.statusSource==='unavailable')continue;
        candidates.push({provider:provider.name,waiting:runwayWaiting(chat),activityAt:stamp(chat.activityAt)||Math.max(stamp(chat.updatedAt),stamp(chat.latestMessageAt),stamp(chat.statusObservedAt),stamp(chat.executionObservedAt),stamp(chat.activity?.observedAt),stamp(chat.executionActivity?.observedAt))});
      }
    }
  }
  candidates.sort((a,b)=>Number(Boolean(b.waiting))-Number(Boolean(a.waiting))||(b.activityAt||0)-(a.activityAt||0));
  return candidates[0]?.provider||null;
}
function render(snapshot) {
  const firstRunwayPaint=COMPACT&&!state.runwaySnapshotPainted;
  const graph=snapshot?.graph?.edges;
  if(firstRunwayPaint&&state.initialBootstrapPending&&(!graph||typeof graph!=='object'||Array.isArray(graph)))return false;
  syncProviderPolicyGeneration(snapshot);
  restrictSnapshotProviders(snapshot);
  acceptSnapshotCache(snapshot);
  reconcileSnapshotQuotas(snapshot);
  state.snapshot=snapshot;
  if(RUNWAY&&!PHONE)state.initialBootstrapPending=false;
  if(firstRunwayPaint){const activeSource=initialActiveSource(snapshot);if(activeSource)state.source=activeSource;}
  if(!state.lastOkAt)state.lastOkAt=Date.now();
  if(RUNWAY&&!PHONE)state.runwaySnapshotPainted=true;
  const providers=providersFor(snapshot),limits=limitProvidersFor(snapshot);
  $(".widget")?.classList?.remove("loading");
  const priorSourceSwitching=state.sourceSwitching;if(firstRunwayPaint)state.sourceSwitching=true;
  try{renderTitle(providers);renderResets(limits);renderChats(providers);renderTotals(providers);renderAnalytics(snapshot);}
  finally{state.sourceSwitching=priorSourceSwitching;}
  renderStrip(attentionItems(state.forest,limits));
  window.TokenTrackerDashboard?.setSnapshot?.(snapshot);
  return true;
}
function renderLoading() {
  syncCompactClock();
  const list=$("#chat-list");
  if(list)list.innerHTML=RUNWAY&&!PHONE?'':Array.from({length:6},()=>'<div class="sk"><i></i><i></i></div>').join("");
  if(RUNWAY&&!PHONE){const header=$('#chats-head'),search=$('#search-toggle');if(header)header.hidden=true;if(search)search.hidden=true;}
  if(RUNWAY&&!PHONE)updateSourceSwitch();
  renderQuotaState();
  if(RUNWAY){const greeter=$("#runway-greeter-status");if(greeter)greeter.textContent="";}
}
function tick() {
  syncCompactClock();
  if(!state.snapshot){renderTitle([]);renderQuotaState();if(state.failSince)renderStrip(attentionItems(null,[]));return;}
  const providers=providersFor(state.snapshot),limits=limitProvidersFor(state.snapshot);
  renderTitle(providers);renderResets(limits);renderChats(providers);renderAnalytics(state.snapshot);
  renderStrip(attentionItems(state.forest,limits));
  window.TokenTrackerDashboard?.setSnapshot?.(state.snapshot);
}
function applyQuotaSamples(samples,sequence,providers,{requireDated=false}={}) {
  if(!samples||!Array.isArray(providers))return false;
  let changed=false;const now=Date.now();
  for(const provider of providers) {
    const sample=samples[provider.name]?.lastStatus;if(!sample)continue;
    const hasCheckedAt=sample.checkedAt!==undefined&&sample.checkedAt!==null&&sample.checkedAt!=='';
    const checkedAt=hasCheckedAt?isoObservationTime(sample.checkedAt,now):null;
    if(hasCheckedAt&&checkedAt===null||requireDated&&checkedAt===null)continue;
    initializeQuotaWindowTimes(provider);
    const retained=watcherAcceptedQuota.get(provider.name)||{};
    for(const [key,percentField,resetField] of [['fiveHour','five_hour_percent','reset_at'],['weekly','weekly_percent','weekly_reset_at']]){
      if(!Object.prototype.hasOwnProperty.call(sample,percentField))continue;
      const percent=sample[percentField];if(percent!==null&&safeNumber(percent)===null)continue;
      const hasWindowTime=Object.prototype.hasOwnProperty.call(provider._quotaWindowObservedAt,key),observed=provider._quotaWindowObservedAt[key],statsAt=isoObservationTime(hasWindowTime?observed:provider.limitsUpdatedAt,now),prior=retained[key];
      const acceptedSequence=state.watcherAcceptedSequence.get(provider.name+':'+key)||0;
      if(checkedAt!==null){if(statsAt!==null&&checkedAt<=statsAt||prior?.at!=null&&checkedAt<=prior.at)continue;}
      else if(requireDated||statsAt!==null||prior?.at!=null||sequence<=acceptedSequence)continue;
      if(percent===null){provider[key]=null;}
      else {
        const previous=provider[key]&&typeof provider[key]==='object'?provider[key]:{},next={...previous,percent:safeNumber(percent)};
        if(Object.prototype.hasOwnProperty.call(sample,resetField)){
          const reset=sample[resetField];if(safeNumber(reset)!==null)next.resetAt=safeNumber(reset);
        }
        provider[key]=next;
      }
      const timestamp=checkedAt!==null?sample.checkedAt:null;
      provider._quotaWindowObservedAt={...(provider._quotaWindowObservedAt||{}),[key]:timestamp};
      if(checkedAt!==null){
        retained[key]={at:checkedAt,timestamp,value:copyQuotaValue(provider[key])};
        const aggregate=isoObservationTime(provider.limitsUpdatedAt,now);if(aggregate===null||checkedAt>aggregate)provider.limitsUpdatedAt=timestamp;
      }else state.watcherAcceptedSequence.set(provider.name+':'+key,sequence);
      watcherAcceptedQuota.set(provider.name,retained);changed=true;
    }
  }
  return changed;
}
function applyWatcherLimits(watcher,sequence=watcherLoader.sequence) {
  return state.snapshot&&watcher?.providers?applyQuotaSamples(watcher.providers,sequence,limitProvidersFor(state.snapshot)):false;
}
function copyQuotaValue(value){
  if(value===null||typeof value!=='object')return value;
  if(Array.isArray(value))return value.map(copyQuotaValue);
  const copy={};for(const key of Object.keys(value))copy[key]=copyQuotaValue(value[key]);return copy;
}
function copyQuotaFields(provider){
  const values={};for(const key of ['fiveHour','weekly','limitsUpdatedAt','_quotaWindowObservedAt'])if(Object.prototype.hasOwnProperty.call(provider,key))values[key]=copyQuotaValue(provider[key]);return values;
}
function initializeQuotaWindowTimes(provider){
  if(!provider._quotaWindowObservedAt||typeof provider._quotaWindowObservedAt!=='object')provider._quotaWindowObservedAt={};
  for(const key of ['fiveHour','weekly'])if(!Object.prototype.hasOwnProperty.call(provider._quotaWindowObservedAt,key))provider._quotaWindowObservedAt[key]=Object.prototype.hasOwnProperty.call(provider,key)?provider.limitsUpdatedAt??null:null;
}
function reconcileSnapshotQuotas(snapshot,now=Date.now()){
  if(!snapshot||!Array.isArray(snapshot.providers))return;
  const currentProviders=state.snapshot?.providers||[];
  for(const incoming of snapshot.providers){
    const current=currentProviders.find(provider=>provider.name===incoming.name),retained=watcherAcceptedQuota.get(incoming.name)||{};
    const selectedTimes={};let aggregateAt=null;
    for(const key of ['fiveHour','weekly']){
      const candidates=[];
      for(const source of [current,incoming])if(source&&Object.prototype.hasOwnProperty.call(source,key)){
      const timestamp=source._quotaWindowObservedAt&&Object.prototype.hasOwnProperty.call(source._quotaWindowObservedAt,key)?source._quotaWindowObservedAt[key]:source.limitsUpdatedAt??null;
        candidates.push({value:source[key],timestamp,at:isoObservationTime(timestamp,now)});
      }
      if(retained[key])candidates.push({value:retained[key].value,timestamp:retained[key].timestamp,at:retained[key].at});
      if(!candidates.length)continue;
      let selected=candidates[0];
      for(const candidate of candidates.slice(1))if(candidate.at!==null&&(selected.at===null||candidate.at>selected.at))selected=candidate;
      incoming[key]=copyQuotaValue(selected.value);
      selectedTimes[key]=selected;
      if(selected.at!==null&&(aggregateAt===null||selected.at>aggregateAt))aggregateAt=selected.at;
    }
    const observedTimes={};
    for(const [key,selected] of Object.entries(selectedTimes))if(selected.at===null||selected.at!==aggregateAt)observedTimes[key]=selected.timestamp??null;
    if(Object.keys(observedTimes).length)incoming._quotaWindowObservedAt=observedTimes;
    else delete incoming._quotaWindowObservedAt;
    if(aggregateAt!==null)incoming.limitsUpdatedAt=new Date(aggregateAt).toISOString();
    else if(Object.prototype.hasOwnProperty.call(incoming,'limitsUpdatedAt'))incoming.limitsUpdatedAt=copyQuotaValue(incoming.limitsUpdatedAt);
  }
}
async function fetchWatcherOnce(){
  const sequence=++watcherLoader.sequence;state.lastWatcherFetchAt=Date.now();let failureKind='unavailable';
  try{
    const response=await fetch('/api/limit-reset',{cache:'no-store',signal:AbortSignal.timeout(5000)});
    if(!response.ok){failureKind='http_error';throw new Error();}
    const watcher=await response.json();
    if(!watcher||typeof watcher!=='object'||Array.isArray(watcher)||!watcher.providers||typeof watcher.providers!=='object'||Array.isArray(watcher.providers)){failureKind='invalid_response';throw new Error();}
    if(sequence!==watcherLoader.sequence||sequence<watcherLoader.successSequence)return false;
    const visible=visibleProviderNames(state.snapshot||{}),safeWatcher={...watcher,providers:Object.fromEntries(Object.entries(watcher.providers).filter(([name])=>visible.has(name)))};
    state.watcher=safeWatcher;watcherLoader.successSequence=sequence;
    if(sequence>=watcherLoader.errorSequence){state.watcherFetchError=null;watcherLoader.errorSequence=sequence;}
    applyWatcherLimits(safeWatcher,sequence);if(state.snapshot)tick();return true;
  }catch{
    if(sequence===watcherLoader.sequence&&sequence>=watcherLoader.successSequence&&sequence>=watcherLoader.errorSequence){state.watcherFetchError={at:Date.now(),kind:failureKind};watcherLoader.errorSequence=sequence;if(state.snapshot)tick();}
    return false;
  }
}
function validQuotaMetadata(payload,now=Date.now()){
  if(!payload||typeof payload!=='object'||Array.isArray(payload)||!payload.providers||typeof payload.providers!=='object'||Array.isArray(payload.providers))return null;
  const metadata=payload.quotaCache;if(!metadata||typeof metadata!=='object'||Array.isArray(metadata))return null;
  const age=metadata.ageSeconds,error=metadata.errorCategory;
  if(typeof metadata.ready!=='boolean'||typeof metadata.fresh!=='boolean'||typeof metadata.refreshing!=='boolean'||
     !(age===null||typeof age==='number'&&Number.isFinite(age)&&age>=0)||
     !(error===null||['timeout','io_error','unavailable','invalid_response','read_failed','worker_start_failed'].includes(error)))return null;
  let sample=payload.providers.Codex?.lastStatus||null;
  if(metadata.ready){
    if(!sample||typeof sample!=='object'||Array.isArray(sample)||age===null||isoObservationTime(sample.checkedAt,now)===null)return null;
    const observed=safeNumber(sample.observed_at),observedMs=observed==null?null:(observed<1e12?observed*1000:observed);
    if(observedMs===null||observedMs<=0||observedMs>now)return null;
  }else sample=null;
  return {metadata:{ready:metadata.ready,fresh:metadata.fresh,ageSeconds:age,refreshing:metadata.refreshing,errorCategory:error},sample};
}
function renderQuotaState(){
  renderTitle(state.snapshot?providersFor(state.snapshot):[]);
  const bootstrapSnapshot={providers:Object.values(quotaBootstrap.providers),providerPolicies:state.providerPolicy};
  renderResets(state.snapshot?limitProvidersFor(state.snapshot):limitProvidersFor(bootstrapSnapshot));
}
function quotaBootstrapTargets(){
  const providers=state.snapshot?.providers||[],present=new Set(providers.map(provider=>provider.name));
  return [...providers,...sourceNames(state.snapshot).filter(name=>!present.has(name)).map(name=>quotaBootstrap.providers[name]).filter(Boolean)];
}
function loadQuota(){
  if(!RUNWAY||PHONE||document.hidden||quotaLoader.unsupported||Date.now()<quotaLoader.retryAfter)return Promise.resolve(false);
  if(quotaLoader.promise)return quotaLoader.promise;
  const sequence=++quotaLoader.sequence;
  const request=(async()=>{
    let kind='unavailable';
    try{
      const response=await fetch('/api/quota',{cache:'no-store',signal:AbortSignal.timeout(5000)});
      if(response.status===404||response.status===405){quotaLoader.unsupported=true;quotaBootstrap.unsupported=true;return false;}
      if(!response.ok){kind='http_error';throw new Error();}
      const payload=await response.json(),validated=validQuotaMetadata(payload);
      if(!validated){kind='invalid_response';throw new Error();}
      if(sequence!==quotaLoader.sequence)return false;
      quotaBootstrap.metadata=validated.metadata;quotaBootstrap.fetchError=null;
      if(validated.sample){quotaBootstrap.samplePayload=payload.providers;quotaBootstrap.sampleSequence=sequence;applyQuotaSamples(payload.providers,sequence,quotaBootstrapTargets(),{requireDated:true});}
      quotaLoader.retryAfter=0;quotaLoader.retryMs=2000;renderQuotaState();return true;
    }catch{
      if(sequence===quotaLoader.sequence){quotaBootstrap.fetchError={at:Date.now(),kind};quotaLoader.retryAfter=Date.now()+quotaLoader.retryMs;quotaLoader.retryMs=Math.min(30000,quotaLoader.retryMs*2);renderQuotaState();}
      return false;
    }
  })();
  let shared;shared=request.finally(()=>{if(quotaLoader.promise===shared)quotaLoader.promise=null;});quotaLoader.promise=shared;return shared;
}
function loadWatcher(){
  if(PHONE||!state.snapshot)return Promise.resolve(false);
  if(watcherLoader.promise){watcherLoader.pending=true;return watcherLoader.promise;}
  const drain=async()=>{let result=false;do{watcherLoader.pending=false;result=await fetchWatcherOnce();}while(watcherLoader.pending);return result;};
  const operation=drain();let shared;
  shared=operation.finally(()=>{if(watcherLoader.promise===shared)watcherLoader.promise=null;});
  watcherLoader.promise=shared;return shared;
}

/* ---------- network ---------- */
const markOk = () => { state.lastOkAt=Date.now();state.failSince=null; };
const markFail = () => { if(!state.failSince)state.failSince=Date.now(); };
function clearBootstrapRetry(reset=false) {
  if(state.bootstrapRetryTimer!=null)clearTimeout(state.bootstrapRetryTimer);
  state.bootstrapRetryTimer=null;
  if(reset)state.bootstrapRetryMs=2000;
}
function scheduleBootstrapRetry() {
  if(state.snapshot||document.hidden||state.bootstrapRetryTimer!=null)return;
  const delay=state.bootstrapRetryMs;
  state.bootstrapRetryMs=Math.min(15000,state.bootstrapRetryMs*2);
  state.bootstrapRetryTimer=setTimeout(()=>{
    state.bootstrapRetryTimer=null;
    if(!state.snapshot&&!document.hidden)refresh();
  },delay);
}
async function pollToday() {
  if(!state.snapshot||document.hidden)return;
  try {
    const response=await fetch("/api/today",{cache:"no-store",signal:AbortSignal.timeout(10000)});
    if(!response.ok)throw new Error();
    const result=await response.json(),today=localISODate();
    for(const update of result.providers||[]) {
      const provider=(state.snapshot.providers||[]).find(item=>item.name===update.name);
      if(!provider||update.todayTokens==null)continue;
      const activity=(provider.activity||[]).filter(item=>item.date!==today);
      activity.push({date:today,value:update.todayTokens});provider.activity=activity;
    }
    renderTotals(providersFor(state.snapshot));
  } catch {}
}
async function refresh(force=false) {
  if(state.loading){state.refreshPending=true;state.refreshPendingForce=state.refreshPendingForce||force;return;}
  clearBootstrapRetry();
  if(RUNWAY)syncMonitors();
  const requestedUrl=statsUrl(force),selection=statsUrl(false);
  state.loading=true;state.refreshing=true;renderTitle(providersFor(state.snapshot||{providers:[]}));
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),25000);
  try {
    const response=await fetch(requestedUrl,{cache:"no-store",signal:controller.signal});
    if(!response.ok)throw new Error("HTTP "+response.status);
    const snapshot=await response.json();if(!snapshot||typeof snapshot!=="object"||!Array.isArray(snapshot.providers))throw new Error("Invalid stats snapshot");applyLive(snapshot,state.live);markOk();
    const present=new Set((snapshot.providers||[]).flatMap(provider=>(provider.chats||[]).map(chat=>provider.name+':'+chat.id)));
    for(const [key,at] of state.pendingOutside)if(present.has(key)){state.outsideSeen.set(key,at);state.pendingOutside.delete(key);}
    if(selection===statsUrl(false)){if(render(snapshot)===false)scheduleBootstrapRetry();else clearBootstrapRetry(true);}
    else state.refreshPending=true;
  } catch { markFail(); tick(); }
  finally{clearTimeout(timeout);state.loading=false;}
  state.refreshing=false;
  renderTitle(providersFor(state.snapshot||{providers:[]}));
  if(state.snapshot&&!PHONE)await loadWatcher();
  if(state.snapshot)tick();
  if(state.refreshPending){const queuedForce=state.refreshPendingForce;state.refreshPending=false;state.refreshPendingForce=false;refresh(queuedForce);return;}
  if(!state.snapshot)scheduleBootstrapRetry();
  if(PHONE&&state.foregroundLivePending&&state.snapshot){state.foregroundLivePending=false;await pollLive();}
}
function statsUrl(force) {
  // Runway needs every active root, including an older chat waiting for a reply.
  // Any server-side trimming can erase passive counts or their parent anchors.
  if(RUNWAY&&!PHONE)return "/api/stats?background=1"+(force?"&refresh=1":"");
  if(RUNWAY)return "/api/stats"+(force?"?refresh=1":"");
  const visible=visibleProviderNames(state.snapshot),pins=[...new Set([...state.pins,...(RUNWAY&&state.showHidden?state.hiddenChats:[])])]
    .filter(key=>{const split=String(key).indexOf(":");return split>0&&visible.has(String(key).slice(0,split));}).join(",");
  const query=parseQuery(state.query);
  const status=query.flags.has("running")?"running":query.flags.size?"attention":"";
  return "/api/stats?limit="+CHAT_LIMIT+(RUNWAY?"&range="+state.windowDays+"d":"")+(pins?"&pins="+encodeURIComponent(pins):"")+ 
    (query.text?"&q="+encodeURIComponent(query.text):"")+(status?"&status="+status:"")+(force?"&refresh=1":"");
}
function handleVisibilityChange() {
  if(document.hidden)return;
  if(RUNWAY&&!PHONE)void loadQuota();
  if(PHONE)state.foregroundLivePending=true;
  return refresh();
}
const LIVE_FIELDS=["contextTokens","contextWindow","contextPercent","recentTokens","usageUpdatedAt","lastRequest","toolCalls","filesEdited","fileChanges","runStartedAt","runEndedAt","pendingQuestionId","questionId","attentionSince","pendingQuestionAt","model",...(RUNWAY?["latestRequest","latestRequestAt","latestUpdate","latestUpdateAt","trackerUpdate","trackerUpdateAt","activeRunStartedAt","stateSince","compactionStartedAt","executionStatus","executionSource","executionObservedAt","executionSince","executionCompactionStartedAt","executionTerminalAt","executionActivity","statusObservedAt","telemetryUnavailable","metricsUnavailable","previousStatus","lastValidAt","statusSource","latestMessageAt","latestMessageDirection","latestMessageSourceId","latestMessageTargetId","contextObservedAt","contextModel","contextDelta","contextDeltaObservedAt","modelChangeAt","stateDetail","failureReason","activity","activityUnavailable","resumable","sessionId"]:[]),...(RUNWAY&&!PHONE?["parentId","parentKey","title","agentName","agentRole","workspace","workspaceKind","source","pendingQuestionIds","questionResolutionAt"]:[])];
// A live row is qualified as "<Provider>:<id>"; ids may themselves contain ":" (Claude subagents).
function liveIdentity(item) {
  if(!item||typeof item.id!=='string'||!item.id||typeof item.key!=='string')return null;
  for(const name of SOURCES)if(item.key===name+':'+item.id)return !item.provider||item.provider===name?name:null;
  return null;
}
// Overlay seconds-fresh activity/state from /api/live onto the last full snapshot.
function applyLive(snapshot, live) {
  if(!snapshot||!live||stamp(live.generatedAt)<stamp(snapshot.generatedAt))return {changed:false,outside:[]};
  restrictSnapshotProviders(snapshot);
  let graphChanged=false;
  if(RUNWAY&&!PHONE&&live.graph&&typeof live.graph.edges==="object"&&!Array.isArray(live.graph.edges)){
    const incoming=Number(live.graph.revision),current=Number(snapshot.graph?.revision);
    if(!snapshot.graph||Number.isFinite(incoming)&&(!Number.isFinite(current)||incoming>current)){snapshot.graph=live.graph;graphChanged=true;}
  }
  const validKnown=item=>!RUNWAY||PHONE||liveIdentity(item)!==null;
  const byKey=new Map((live.chats||[]).filter(validKnown).map(item=>[item.key,item]));
  let changed=graphChanged,oldestShown=Infinity;
  const initiallyLoaded=new Set((snapshot.providers||[]).flatMap(provider=>(provider.chats||[]).map(chat=>provider.name+':'+chat.id)));
  // Unseen rows are inserted per provider; providers hidden by policy are already absent from the snapshot.
  const liveTargets=new Map();
  if(RUNWAY&&!PHONE)for(const name of SOURCES){const target=(snapshot.providers||[]).find(provider=>provider.name===name);if(target&&Array.isArray(target.chats))liveTargets.set(name,{target,known:new Set(target.chats.map(chat=>name+':'+chat.id))});}
  if(liveTargets.size){
    for(const item of live.chats||[]){
      const owner=liveIdentity(item),entry=owner&&liveTargets.get(owner);
      if(!entry)continue;
      const {target,known}=entry;
      if(known.has(item.key))continue;
       const record={id:item.id,key:item.key};
       for(const field of ['parentId','parentKey','title','agentName','agentRole','workspace','workspaceKind','source','status','statusSource','telemetryUnavailable','metricsUnavailable','previousStatus','lastValidAt','stateDetail','failureReason','activity','activityUnavailable','executionStatus','executionSource','executionObservedAt','executionSince','executionCompactionStartedAt','executionTerminalAt','executionActivity','activeRunStartedAt','stateSince','runStartedAt','runEndedAt','updatedAt','activityAt','pendingQuestion','pendingQuestionAt','pendingQuestionId','pendingQuestionIds','questionId','questionResolutionAt','pendingOptions','model','contextTokens','contextWindow','contextPercent','contextObservedAt','contextModel','contextDelta','contextDeltaObservedAt','latestRequest','latestRequestAt','latestUpdate','latestUpdateAt','trackerUpdate','trackerUpdateAt','latestMessageAt','latestMessageDirection','latestMessageSourceId','latestMessageTargetId','toolCalls','filesEdited','fileChanges','recentTokens','usageUpdatedAt','lastRequest','attentionSince','compactionStartedAt','statusObservedAt','modelChangeAt','resumable','sessionId'])if(item[field]!==undefined)record[field]=item[field];
      target.chats.push(record);known.add(item.key);changed=true;
    }
  }
  for(const provider of snapshot.providers||[])for(const chat of provider.chats||[]){
    const key=provider.name+":"+chat.id;
    const next=byKey.get(key);
    if(next&&(RUNWAY||chat.statusSource!=="app"||stamp(next.activityAt)>stamp(snapshot.generatedAt))){
      if(next.telemetryUnavailable!==undefined&&chat.telemetryUnavailable!==Boolean(next.telemetryUnavailable)){chat.telemetryUnavailable=Boolean(next.telemetryUnavailable);changed=true;}
      if(next.status){if((chat.statusSource!=="unavailable"||next.telemetryUnavailable===false||next.statusSource!=="unavailable")&&normalizeStatus(next.status)!==normalizeStatus(chat.status)){chat.status=next.status;changed=true;}if(RUNWAY)chat.sourceStatus=next.status;}
      if((next.pendingQuestion||null)!==(chat.pendingQuestion||null)||(next.pendingQuestionAt||null)!==(chat.pendingQuestionAt||null)){
        chat.pendingQuestion=next.pendingQuestion||null;chat.pendingQuestionAt=next.pendingQuestionAt||null;changed=true;
      }
      if(JSON.stringify(next.pendingOptions||[])!==JSON.stringify(chat.pendingOptions||[])){chat.pendingOptions=next.pendingOptions||[];changed=true;}
      if(stamp(next.activityAt)>stamp(chat.updatedAt)){chat.updatedAt=next.activityAt;changed=true;}
      for(const field of LIVE_FIELDS){
        if(next[field]!==undefined&&JSON.stringify(next[field])!==JSON.stringify(chat[field])){chat[field]=next[field];changed=true;}
      }
    }
    const parent=snapshotParentKey(provider,chat,snapshot.graph?.edges||null);
    if(!parent)oldestShown=Math.min(oldestShown,stamp(chat.updatedAt));
  }
  // A valid unseen identity or newer outside activity means the structure needs enrichment.
  const roots=(snapshot.providers||[]).reduce((n,p)=>n+(p.chats||[]).filter(c=>!snapshotParentKey(p,c,snapshot.graph?.edges||null)).length,0);
  const outside=(live.chats||[]).filter(item=>!initiallyLoaded.has(item.key)&&(!RUNWAY||PHONE||(()=>{const owner=liveIdentity(item);return Boolean(owner&&(snapshot.providers||[]).some(provider=>provider.name===owner));})())&&(roots<CHAT_LIMIT||stamp(item.activityAt)>oldestShown)).map(item=>[item.key,item.activityAt]);
  return {changed,outside};
}
async function pollLive() {
  if(document.hidden)return;
  if(RUNWAY&&!PHONE)void loadQuota();
  if(state.liveBusy||(state.loading&&(PHONE||!state.snapshot)))return;
  state.liveBusy=true;
  try{
    const response=await fetch("/api/live",{cache:"no-store",signal:AbortSignal.timeout(5000)});
    if(!response.ok)throw new Error("HTTP "+response.status);
    const sample=await response.json(),sampleAt=stamp(sample?.generatedAt),now=Date.now();
    if(!sample||typeof sample!=='object'||!Array.isArray(sample.chats)||!Number.isFinite(sampleAt)||sampleAt<=0||sampleAt>now)throw new Error('Invalid live sample timestamp');
    if(state.liveAcceptedGeneratedAt==null||sampleAt>=state.liveAcceptedGeneratedAt){
      if(!runwayCurrentEvidenceFresh(now))state.liveRecoveryPending=true;
      state.live=sample;state.liveAcceptedGeneratedAt=sampleAt;state.liveSampleAccepted=true;
    }
    markOk();
    const result=applyLive(state.snapshot,state.live);
    if(state.snapshot&&(result.changed||state.loading))tick();
    if(!PHONE&&Date.now()-state.lastWatcherFetchAt>10000)void loadWatcher();
    const structural=Boolean(state.live.structureStamp&&state.snapshot?.structureStamp&&state.live.structureStamp!==state.snapshot.structureStamp);
    const fresh=result.outside.filter(([key,at])=>state.outsideSeen.get(key)!==at);
    fresh.forEach(([key,at])=>state.pendingOutside.set(key,at));
    if((structural||fresh.length||state.pendingOutside.size)&&Date.now()-state.structuralRefreshAt>STRUCTURE_REFRESH_MS){
      state.structuralRefreshAt=Date.now();
      if(state.loading){state.refreshPending=true;state.refreshPendingForce=true;}
      else void refresh(true);
    }
  }catch{markFail();}finally{state.liveBusy=false;if(PHONE&&state.foregroundLivePending&&!state.loading&&!document.hidden){state.foregroundLivePending=false;pollLive();}}
}
function handleVisibilityChange() {
  if(document.hidden)return;
  if(RUNWAY&&!PHONE)void loadQuota();
  if(PHONE)state.foregroundLivePending=true;
  return refresh();
}
function setupResize() {
  document.querySelectorAll(".resize-edge").forEach(edge=>edge.addEventListener("pointerdown",async event=>{
    if(event.button!==0||!window.pywebview?.api?.resize_start)return;
    event.preventDefault();edge.setPointerCapture(event.pointerId);
    const startX=event.screenX,startY=event.screenY;
    let size=null,lastX=startX,lastY=startY;
    const apply=()=>{if(size&&(lastX!==startX||lastY!==startY))window.pywebview.api.resize(size.width+(edge.dataset.resize==="bottom"?0:lastX-startX),size.height+(edge.dataset.resize==="right"?0:lastY-startY));};
    const move=next=>{lastX=next.screenX;lastY=next.screenY;apply();};
    const cleanup=()=>{edge.removeEventListener("pointermove",move);edge.removeEventListener("pointerup",cleanup);edge.removeEventListener("pointercancel",cleanup);};
    edge.addEventListener("pointermove",move);edge.addEventListener("pointerup",cleanup);edge.addEventListener("pointercancel",cleanup);
    try{size=await window.pywebview.api.resize_start();apply();}catch{cleanup();}
  }));
}

/* ---------- interaction ---------- */
async function openInCodex(thread, button) {
  if(!thread)return false;
  try {
    const response=await fetch("/api/open?thread="+encodeURIComponent(thread),{cache:"no-store",signal:AbortSignal.timeout(5000)});
    const result=await response.json();
    if(!response.ok||!result.ok)return false;
    if(button?.classList){button.classList.add("copied");setTimeout(()=>button.classList.remove("copied"),1200);}
    return true;
  } catch {return false;}
}
async function copyAndOpenAnswer(thread, answer, button) {
  if(!thread||!answer||button?.disabled)return;
  if(button)button.disabled=true;
  const copied=await copyText(answer);
  const opened=await openInCodex(thread);
  if(button){
    button.classList.add(opened?"opened":"send-failed");
    button.title=opened?"Codex task opened. Select this choice in its question prompt; the choice was "+(copied?"also copied.":"not copied."):"Could not open Codex. Open the task and select this choice in its question prompt.";
    setTimeout(()=>{button.disabled=false;},1000);
  }
}
async function queueAnswer(thread, message, button) {
  if(!thread||!message||button?.disabled)return;
  if(button)button.disabled=true;
  try {
    const response=await fetch("/api/queue",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({thread,message}),cache:"no-store",signal:AbortSignal.timeout(20000)});
    const result=await response.json();
    if(!response.ok||!result.ok){
      if(button){button.classList.add("send-failed");button.title=result.error||"Could not queue this message.";}
      return result;
    }
    if(button){button.classList.add("sent");button.title=result.detail||"Queued for the next turn. This does not answer the pending question.";const label=button.querySelector("span");if(label)label.textContent="Queued";}
    return result;
  } catch {
    if(button){button.classList.add("send-failed");button.title="Queue status unknown. Check the Codex task before retrying.";}
    return {ok:false,status:"unknown"};
  } finally {if(button&&!button.classList.contains("sent"))setTimeout(()=>{button.disabled=false;},1000);}
}
async function copyText(text, button) {
  let copied=false;
  try { await navigator.clipboard.writeText(text);copied=true; }
  catch {
    let area;
    try { area=document.createElement("textarea");area.value=text;document.body.appendChild(area);area.select();copied=Boolean(document.execCommand("copy")); }
    catch {copied=false;}
    finally {area?.remove?.();}
  }
  if(button?.classList){button.classList.add("copied");setTimeout(()=>button.classList.remove("copied"),1200);}
  return copied;
}
function togglePin(key) {
  let chat=state.forest?.rows.find(c=>c.key===key);
  const visited=new Set();
  while(hasTreeParent(chat)&&!visited.has(chat.key)){visited.add(chat.key);const parent=state.forest.rows.find(c=>c.key===treeParentKey(chat));if(!parent)break;chat=parent;}
  if(!chat)return;
  state.pins.has(chat.key)?state.pins.delete(chat.key):state.pins.add(chat.key);
  try{localStorage.setItem("tokenTrackerPinsV2",JSON.stringify([...state.pins]));}catch{}
}
function setWindow(days) {
  if(!WINDOWS.includes(days))return;
  state.windowDays=days;
  try{localStorage.setItem(RUNWAY?"tokenTrackerRunwayWindow":"tokenTrackerWindow",String(days));}catch{}
}
function openSearch(open) {
  state.searchOpen=open;
  const header=$("#chats-head"),toggle=$("#search-toggle"),input=$("#chat-search");
  if(COMPACT&&open){if(header)header.hidden=false;if(toggle)toggle.hidden=false;}
  header?.classList?.toggle("searching",open);
  if(COMPACT)setNotch(false);
  if(open)input?.focus?.();
  else {if(state.query){state.query="";if(input)input.value="";queueSelectionRefresh();}if(COMPACT&&$('.widget')?.contains?.(document.activeElement))$('#sync-age')?.focus?.({preventScroll:true});}
}
function jumpTo(target, sub) {
  if(parseQuery(state.query).active&&state.forest&&!visibleRoots(state.forest).some(c=>c.key===target))openSearch(false);
  const chat=state.forest?.roots.find(c=>c.key===target);
  if(chat&&!inWindow(chat,state.windowDays)&&!state.pins.has(chat.key))setWindow(WINDOWS.find(d=>inWindow(chat,d))||7);
  if(RUNWAY)closeRunwayDetails();
  if(!RUNWAY||!sub)state.open.add(target);
  if(RUNWAY&&sub){state.agentOpen.add(target);state.agentCollapsed.delete(target);state.agentSearchClosed.delete(target);state.agentDetails.add(sub);}
  if(state.snapshot)renderChats(providersFor(state.snapshot));
  const list=$("#chat-list");if(!realDom(list))return;
  const el=[...list.children].find(e=>e.dataset.key===target);if(!el)return;
  const focus=sub?[...el.querySelectorAll("[data-sub]")].find(e=>e.dataset.sub===sub)||el:el;
  const behavior=reducedMotion()?"auto":"smooth";
  focus.scrollIntoView?.({block:"nearest",behavior});
  // Once layer 2 has opened, bring the whole chat (row, panel, tray) into view.
  setTimeout(()=>{(focus===el?el:focus).scrollIntoView?.({block:"nearest",behavior});},reducedMotion()?0:220);
  focus.classList.remove("flash");void focus.offsetWidth;focus.classList.add("flash");
  setTimeout(()=>focus.classList.remove("flash"),1300);
}
function closeRunwayDetails() { state.open.clear();state.agentDetails.clear();state.expandedQuestions.clear(); }
function handleClick(event) {
  const button=event.target.closest("[data-action]");
  const pop=$("#popover");
  if(pop&&!pop.hidden&&!(pop.contains?.(event.target))&&button?.dataset.action!=="watcher")closePopover();
  if(!button)return;
  const {action,key}=button.dataset;
  if(RUNWAY&&!PHONE&&action==='widget-lock'){void toggleWidgetLock();return;}
  if(RUNWAY&&!PHONE&&action==='jump-waiting'){jumpToWaiting();return;}
  if(RUNWAY&&!PHONE&&action==='source'){setSource(button.dataset.source);return;}
  if(RUNWAY&&!PHONE&&action==='dashboard'){openDashboard();return;}
  if(COMPACT&&action==='keep-open'){void toggleKeepOpen();return;}
  if(PHONE&&["answer","queue-answer","open","pin","hide-chat","show-hidden"].includes(action))return;
  if(action==="toggle-chat"){const was=state.open.has(key);if(RUNWAY)closeRunwayDetails();was?state.open.delete(key):state.open.add(key);if(RUNWAY)renderStripNow();}
  else if(action==="pin"){togglePin(key);closePopover();queueSelectionRefresh();}
  else if(action==="copy"){copyText(button.dataset.copy,button);closePopover();return;}
  // Answering: copy the chosen option, then open that exact chat in Codex so it is one paste away.
  else if(action==="answer"){copyAndOpenAnswer(button.dataset.thread,button.dataset.answer,button);return;}
  else if(action==="queue-answer"){queueAnswer(button.dataset.thread,button.dataset.answer,button);return;}
  else if(action==="open"){openInCodex(button.dataset.thread,button);return;}
  else if(RUNWAY&&action==="runway-question"){
    if(PHONE){
      const chat=state.forest?.rows.find(item=>item.key===key);if(!chat)return;
      notch.manual=true;setNotch(true);openPopover(runwayQuestionPopover(chat),button);button.__questionId=chat.pendingQuestionId??chat.questionId??null;button.__questionAt=chat.pendingQuestionAt||null;button.__questionText=chat.pendingQuestion;button.__questionOptions=JSON.stringify(chat.pendingOptions||[]);return;
    }
    const chat=state.forest?.rows.find(item=>item.key===key);if(!chat||!runwayWaiting(chat))return;
    if(!isCodexChat(chat)){
      notch.manual=true;setNotch(true);openPopover(runwayQuestionPopover(chat),button);button.__questionId=chat.pendingQuestionId??chat.questionId??null;button.__questionAt=chat.pendingQuestionAt||null;button.__questionText=chat.pendingQuestion;button.__questionOptions=JSON.stringify(chat.pendingOptions||[]);return;
    }
    openInCodex(chat.id,button);return;
  }
  else if(RUNWAY&&action==="close-popover"){closePopover({restoreFocus:true});return;}
  else if(action==="jump"){jumpTo(button.dataset.target,button.dataset.sub);return;}
  else if(action==="dismiss"){state.dismissed.add(button.dataset.dismiss);saveDismissed();const item=button.closest?.(".si");if(item)item._dismissed=true;renderStripNow();return;}
  else if(action==="strip-more"){state.stripOpen=!state.stripOpen;renderStripNow();return;}
  else if(action==="toggle-question"){const question=button.dataset.question,was=state.expandedQuestions.has(question);if(RUNWAY)closeRunwayDetails();if(!was)state.expandedQuestions.add(question);renderStripNow();if(RUNWAY&&state.snapshot)renderChats(providersFor(state.snapshot));return;}
  else if(action==="toggle-agents"){const target=button.dataset.key;const chat=state.forest?.roots.find(c=>c.key===target);const expanded=RUNWAY&&chat?runwayAgentsExpanded(chat):state.agentOpen.has(target);if(expanded){state.agentOpen.delete(target);state.agentCollapsed.add(target);state.agentSearchClosed.add(target);}else{state.agentOpen.add(target);state.agentCollapsed.delete(target);state.agentSearchClosed.delete(target);}if(state.snapshot)renderChats(providersFor(state.snapshot));return;}
  else if(RUNWAY&&action==='hide-chat'){state.hiddenChats.has(key)?state.hiddenChats.delete(key):state.hiddenChats.add(key);saveHiddenChats();if(state.snapshot)renderChats(providersFor(state.snapshot));renderStripNow();return;}
  else if(RUNWAY&&action==='show-hidden'){state.showHidden=!state.showHidden;if(state.snapshot)renderChats(providersFor(state.snapshot));renderStripNow();queueSelectionRefresh();return;}
  else if(action==="toggle-agent-details"){const was=state.agentDetails.has(key);if(RUNWAY)closeRunwayDetails();was?state.agentDetails.delete(key):state.agentDetails.add(key);if(RUNWAY)renderStripNow();if(state.snapshot)renderChats(providersFor(state.snapshot));return;}
  else if(action==="analytics"){state.analyticsOpen=!state.analyticsOpen;if(state.snapshot)renderAnalytics(state.snapshot);return;}
  else if(action==="window"){setWindow(Number(button.dataset.days));if(RUNWAY)queueSelectionRefresh();}
  else if(action==="watcher"){if(state.popover===button)closePopover();else{const html=watcherPopoverHTML(button.dataset.provider);if(html){openPopover(html,button);if(RUNWAY)void loadIdentityPolicyWarnings(button);}else closePopover();}return;}
  else if(action==="reset-settings"){closePopover();const api=window.pywebview?.api;if(api?.open_reset_actions)api.open_reset_actions();else window.open?.("/reset_actions.html","_blank","noopener");return;}
  else if(action==="search"){openSearch(!state.searchOpen);}
  else if(action==="search-close"){openSearch(false);}
  else if(action==="refresh"){refresh(true);return;}
  if(state.snapshot)renderChats(providersFor(state.snapshot));
}
function handleContextMenu(event) {
  if(RUNWAY)return;
  const row=event.target.closest?.(".chat > .row");
  if(!row)return;
  event.preventDefault();
  const chat=state.forest?.rows.find(c=>c.key===row.dataset.key);if(!chat)return;
  const pinned=state.pins.has(chat.key);
  openPopover('<div class="menu"><button data-action="pin" data-key="'+esc(chat.key)+'">'+ICON.pin+(pinned?"Unpin":"Pin")+'</button>'+(resumeCommand(chat)?'<button data-action="copy" data-copy="'+esc(resumeCommand(chat))+'">'+ICON.copy+ICON.check+'Copy resume</button>':'')+'</div>',{getBoundingClientRect:()=>({left:event.clientX,bottom:event.clientY})});
}
function handleKey(event) {
  const typing=/INPUT|TEXTAREA/.test(event.target?.tagName||"");
  if(event.key==="Escape"){
    if(dashboardState.open){event.preventDefault();event.stopImmediatePropagation?.();closeDashboard();return;}
    if(state.popover){closePopover({restoreFocus:true});if(COMPACT){event.preventDefault();event.stopImmediatePropagation?.();}if(state.snapshot)renderChats(providersFor(state.snapshot));return;}
    if(state.searchOpen){openSearch(false);if(COMPACT){event.preventDefault();event.stopImmediatePropagation?.();}if(state.snapshot)renderChats(providersFor(state.snapshot));return;}
    if(!COMPACT&&state.snapshot)renderChats(providersFor(state.snapshot));return;
  }
  if(!typing&&(event.key==="/"||(event.ctrlKey&&event.key.toLowerCase()==="k"))){event.preventDefault();openSearch(true);return;}
  if(!typing&&(event.key==="Enter"||event.key===" ")&&event.target?.getAttribute?.("role")==="button"){event.preventDefault();event.target.click();}
}
function queueSelectionRefresh(){if(window.__TOKEN_TRACKER_TEST__)return;clearTimeout(state.selectionTimer);state.selectionTimer=setTimeout(()=>refresh(),250);}
window.tokenTrackerRefresh=refresh;
window.__tokenTrackerTest={state,render,renderRunwayCounts,jumpToWaiting,tick,renderChats,renderStrip,renderResets,renderTotals,handleClick,handleKey,setupResize,applyLive,pollLive,refresh,loadWatcher,applyWatcherLimits,applyQuotaSamples,reconcileSnapshotQuotas,acceptSnapshotCache,validateSnapshotCache,isoObservationTime,watcherLoader,statsUrl,runway:RUNWAY,phone:PHONE,compact:COMPACT,keepOpenState,handleVisibilityChange,trackerSnippet,notch,widgetLock,setNotch,syncNotch,notchHeight,syncMonitors,toggleMonitor,applyMonitorResult,monitorSignature,predictedInspectLayoutWidth,writeInspectLayoutWidth,applyInspectLayoutWidth,refreshInspectCssScale,noteInspectViewportResize,cancelInspectLayoutReconcile,getMonitorState:()=>monitorState,getInspectWidthState:()=>({width:inspectLayoutWidth,signature:inspectMonitorSignature,cssScale:inspectCssScaleSignature,cacheKey:inspectWidthCacheKey,generation:inspectLayoutGeneration,correctionUsed:inspectWidthCorrectionUsed,correctedWidths:new Map(correctedInspectWidths),expectedSignature:inspectExpectedSignature,requestGeneration:inspectRequestGeneration,resizeGeneration:inspectResizeGeneration,requestResizeGeneration:inspectRequestResizeGeneration,bridgeBusy:compactBridge.busy,appliedSignature:compactBridge.appliedSignature,pendingSignature:compactBridge.pending?.signature||null}),syncWidgetLock,applyWidgetLock,setWidgetLock,toggleWidgetLock,recenterWidget,syncKeepOpen,applyKeepOpen,setKeepOpen,toggleKeepOpen,runwayCurrentEvidenceFresh,RUNWAY_EVIDENCE_MAX_AGE_MS,runwayStateElapsed,runwayCounts,runwayTitle,runwayAgentHasNewMessage,runwayMessageAge,runwayQuestionPopover,runwayVisibleActive,runwayRecentlyUnavailable,runwayPatchContent,isAsking,runwayWaiting,runwayExecutionView,runwayEffectiveStatus,runwayRunning,runInfo,captureSourceSnapshot,revealSourceSnapshot,setupWindowDrag,
  renderLoading,
  buildForest,visibleRoots,nextWindowWithChats,parseQuery,cacheWarmth,warmthView,resetAt,resetParts,resetView,countdown,runSpan,ageShort,dayWord,providersFor,limitProvidersFor,sourceNames,effectiveSource,treeParentKey,runwayModelLabel,syncCompactClock,
  setupNotch,attentionItems,questionDismissToken,watcherTone,rollup,ownState,rowHTML,panelHTML,trayEntries,traySummaryHTML,trayState,agentStats,totalStats,statStripHTML,watcherPopoverHTML,isVisibleAgent,chatBlockHTML,runwayTreePathGeometry,
  normalizeStatus,statusRank,contextTone,providerActivity,fmt,money,safeNumber,agentLabel,modelLabel,runwayModelLabel,runwayStatus,runwayActivityAge,runwayActivityEvidence,runwayAgentsExpanded,runwayDetailHTML,runwayTelemetryHTML,runwayPatchContent,jumpTo,isAutoReview,setWindow,avatarIndex,markFor,filesHTML,optionsHTML,copyAndOpenAnswer,queueAnswer,analyticsHTML,renderAnalytics,renderGreeterStatus};
Object.assign(window.__tokenTrackerTest,{sourceNames,effectiveSource,setSource,updateSourceSwitch,providersFor,limitProvidersFor,liveIdentity,resumeCommand,runwayExecutionView,runwayWaiting,applyIdentityPayload,policyWarningHTML,validPublicProviderPolicy,initialActiveSource,openDashboard,closeDashboard,dashboardState});window.__tokenTrackerTest.loadQuota=loadQuota;window.__tokenTrackerTest.loadProviderPolicy=loadProviderPolicy;window.__tokenTrackerTest.quotaBootstrap=quotaBootstrap;window.__tokenTrackerTest.quotaLoader=quotaLoader;window.__tokenTrackerTest.validQuotaMetadata=validQuotaMetadata;
if(!window.__TOKEN_TRACKER_TEST__){
  if(!PHONE)loadAvatars();
  document.addEventListener("click",handleClick);
  document.addEventListener("contextmenu",handleContextMenu);
  document.addEventListener("keydown",handleKey);
  if(RUNWAY&&!PHONE)window.addEventListener?.('resize',()=>{if(COMPACT){noteInspectViewportResize();refreshInspectCssScale();}scheduleNotchSize();},{passive:true});
  if(COMPACT)window.visualViewport?.addEventListener?.('resize',()=>{if(refreshInspectCssScale()&&notch.mode==='inspect'){noteInspectViewportResize();scheduleNotchSize();}},{passive:true});
  $("#chat-search")?.addEventListener("input",event=>{state.query=event.target.value;if(state.snapshot)renderChats(providersFor(state.snapshot));queueSelectionRefresh();});
  $("#chat-list")?.addEventListener("pointerenter",()=>{state.pointerInList=true;});
  $("#chat-list")?.addEventListener("pointerleave",()=>{state.pointerInList=false;if(state.snapshot)renderChats(providersFor(state.snapshot));});
  if(!PHONE){
    $("#window-minimize")?.addEventListener("click",()=>window.pywebview?.api?.minimize?.());
    $("#window-fit")?.addEventListener("click",()=>window.pywebview?.api?.fit?.());
    $("#window-maximize")?.addEventListener("click",()=>window.pywebview?.api?.toggle_maximize?.());
    $("#window-close")?.addEventListener("click",()=>window.pywebview?.api?.close?.());
  }
  $(".widget").classList.add("loading");
  if(RUNWAY&&!PHONE){setupNotch();setupWindowDrag();}else if(!PHONE)setupResize();renderLoading();if(RUNWAY&&!PHONE){void loadProviderPolicy();void loadQuota();}refresh();setInterval(tick,1000);setInterval(()=>{if(!document.hidden)refresh();},FULL_REFRESH_MS);setInterval(pollLive,LIVE_MS);if(!PHONE)setInterval(pollToday,TODAY_REFRESH_MS);
  document.addEventListener("visibilitychange",handleVisibilityChange);
}
