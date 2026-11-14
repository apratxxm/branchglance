const $ = (selector, root = document) => root.querySelector(selector);
const RUNWAY_REFRESH_ICON='<svg class="ic" viewBox="0 0 12 12" aria-hidden="true"><path d="M10 4.8A4.5 4.5 0 1 0 10 7"/><path d="M7.7 2.2H10v2.3"/></svg>';
function preferredClockCycle(){try{return localStorage.getItem('branchglanceClockFormat')==='24'?'h23':'h12';}catch{return 'h12';}}
let compactClockFormatter=new Intl.DateTimeFormat('en-US',{hour:'numeric',minute:'2-digit',hourCycle:preferredClockCycle()});
const compact = new Intl.NumberFormat("en", { notation: "compact", maximumSignificantDigits: 3 });
function loadPins() { try { const current=JSON.parse(localStorage.getItem("tokenTrackerPinsV2") || "null"); if(Array.isArray(current))return new Set(current.filter(k=>typeof k==="string"));const old=JSON.parse(localStorage.getItem("tokenTrackerPins") || "{}");return new Set(Object.entries(old||{}).filter(([,id])=>typeof id==="string").map(([provider,id])=>provider+":"+id)); } catch { return new Set(); } }
const WINDOWS=[1,7,30];
function loadWindow() { try { const days=Number(localStorage.getItem("tokenTrackerRunwayWindow")); return WINDOWS.includes(days)?days:1; } catch { return 1; } }
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
  windowDays:loadWindow(), pins:loadPins(), watcher:null,
  snapshotCache:null,snapshotCacheInvalid:false,watcherFetchError:null,watcherAcceptedSequence:new Map(),
  liveBusy:false, structuralRefreshAt:0, lastWatcherFetchAt:0, outsideSeen:new Map(), pendingOutside:new Map(), refreshPending:false, refreshPendingForce:false, pointerInList:false, lastOrder:null, agentLive:new Map(), finishedAt:new Map(), analyticsOpen:false,
  hiddenChats:loadHiddenChats(), showHidden:false, bootstrapRetryTimer:null, bootstrapRetryMs:2000, graphlessBootstrapTimer:null, graphlessBootstrapSnapshot:null, graphlessBootstrapAttempts:0, graphlessBootstrapTimedOut:false, graphlessBootstrapFallback:false, refreshing:false, initialBootstrapPending:!window.__TOKEN_TRACKER_TEST__, foregroundLivePending:false, modelVersions:new Map(), runwaySnapshotPainted:false, startupCalmUntil:STARTUP_STARTED_AT+STARTUP_CALM_MS, liveAcceptedGeneratedAt:null, liveSampleAccepted:false, liveRecoveryPending:false, seenQuestions:new Set(), providerPolicyGeneration:null,providerPolicy:null,providerPolicyReady:false,
  source:loadSource(), sourceManualAt:loadSourceManualAt(), sourceAttention:new Set(), sourceLive:new Set(), sourceBase:new Map(), sourceSwitching:false, sourceHeightHold:null, sourceRelease:null, notchOpeningTimer:null, policyWarnings:[] };
const watcherLoader={promise:null,pending:false,sequence:0,successSequence:0,errorSequence:0};
const watcherAcceptedQuota=new Map();
const lastKnownFollowup={timer:null,attempts:0};
const codexQuotaBootstrap={name:'Codex'};
const quotaBootstrap={provider:codexQuotaBootstrap,providers:{Codex:codexQuotaBootstrap,Claude:{name:'Claude'}},metadata:null,fetchError:null,unsupported:false,samplePayload:null,sampleSequence:0};
const quotaLoader={promise:null,sequence:0,retryAfter:0,retryMs:2000,unsupported:false};
const providerPolicyLoader={promise:null};
const RUNWAY_EVIDENCE_MAX_AGE_MS=8000;
function runwayCurrentEvidenceFresh(now=Date.now()) {
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
const widgetLock={known:Boolean(window.__TOKEN_TRACKER_TEST__),locked:false,expanded:false,revision:-1,pending:false};
const keepOpenState={known:false,value:false,revision:-1,pending:false,loading:false,error:false};
const compactBridge={busy:false,pending:null,inFlightSignature:null,appliedSignature:null};
const dashboardState={open:false,returnMode:null,restoreMode:null};
let compactInputModality='pointer';
const lockIcon=locked=>locked?'<svg class="ic" viewBox="0 0 12 12" aria-hidden="true"><rect x="2.5" y="5" width="7" height="5.5" rx="1"/><path d="M4 5V3.8a2 2 0 0 1 4 0V5"/></svg>':'<svg class="ic" viewBox="0 0 12 12" aria-hidden="true"><rect x="2.5" y="5" width="7" height="5.5" rx="1"/><path d="M4 5V3.8a2 2 0 0 1 3.6-1.2"/></svg>';
function updateWidgetLockButton() {
  const button=$('#widget-lock');if(!button)return;
  const label=widgetLock.locked?'Release position':'Hold position';
  const title=label+'. Prevents automatic repositioning; you can still drag.';
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
  if(!value||typeof value.keepOpen!=='boolean'||!Number.isSafeInteger(value.keepOpenRevision)||value.keepOpenRevision<0||value.keepOpenRevision<keepOpenState.revision)return false;
  keepOpenState.known=true;keepOpenState.value=value.keepOpen;keepOpenState.revision=value.keepOpenRevision;keepOpenState.error=false;
  updateKeepOpenButton();setNotch(notch.expanded,true);return true;
}
async function syncKeepOpen() {
  const api=window.pywebview?.api?.get_keep_open;
  if(typeof api!=='function'){updateKeepOpenButton();return null;}
  keepOpenState.loading=true;updateKeepOpenButton();
  try{const result=await api();applyKeepOpen(result);return result;}
  catch{keepOpenState.error=true;return null;}
  finally{keepOpenState.loading=false;updateKeepOpenButton();}
}
async function setKeepOpen(value) {
  if(keepOpenState.pending||keepOpenState.loading||!keepOpenState.known||typeof value!=='boolean')return false;
  const api=window.pywebview?.api?.set_keep_open;if(typeof api!=='function')return false;
  const expectedRevision=keepOpenState.revision;keepOpenState.pending=true;keepOpenState.error=false;updateKeepOpenButton();
  try{const result=await api(value,expectedRevision);if(!applyKeepOpen(result))return false;return keepOpenState.value===value;}
  catch{keepOpenState.error=true;void syncKeepOpen();return false;}
  finally{keepOpenState.pending=false;updateKeepOpenButton();}
}
async function toggleKeepOpen(){if(keepOpenState.pending||keepOpenState.loading)return false;return setKeepOpen(!keepOpenState.value);}
function compactHasFocus(){
  const widget=$('.widget'),active=document.activeElement;if(!widget?.contains?.(active))return false;
  const tag=String(active?.tagName||'').toUpperCase(),editable=tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT'||Boolean(active?.isContentEditable);
  return editable||Boolean(active?.closest?.('.popover,.ch-search'))||compactInputModality==='keyboard';
}
function compactModeForState(){
  if(dashboardState.open)return 'dashboard';
  if(dashboardState.restoreMode)return dashboardState.restoreMode;
  const focus=compactHasFocus(),explicit=notch.manual||notch.keyboardInspecting||state.searchOpen||Boolean(state.popover);
  // With no active chats there is nothing to show: keep-open, hover and focus stay on the toolbar.
  if(explicit||notch.active>0&&(keepOpenState.value||notch.autoExpanded||notch.inspecting||focus))return 'inspect';
  return 'controls';
}
function blurPointerToolbarFocus(mode){
  if(mode==='inspect'||mode==='controls'||compactInputModality!=='pointer')return;
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
function applyWidgetLock(value) {
  if(!value||typeof value.locked!=='boolean'||typeof value.expanded!=='boolean'||!Number.isSafeInteger(value.revision)||value.revision<0||value.revision<widgetLock.revision)return false;
  widgetLock.known=true;widgetLock.revision=value.revision;widgetLock.locked=value.locked;widgetLock.expanded=value.expanded;
  if(typeof value.keepOpen==='boolean'&&Number.isSafeInteger(value.keepOpenRevision))applyKeepOpen({keepOpen:value.keepOpen,keepOpenRevision:value.keepOpenRevision});
  updateWidgetLockButton();setNotch(notch.expanded,true);return true;
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
    const result=await api(locked,notch.expanded,null,expectedRevision);
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
function openDashboard(tab){
  if(dashboardState.open)return false;
  dashboardState.returnMode=notch.mode||'controls';dashboardState.restoreMode=null;dashboardState.open=true;
  window.TokenTrackerDashboard?.open?.({source:effectiveSource(state.snapshot),tab:tab==='settings'?'settings':'insights'});
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
// The dashboard sizes the window (720 wide, 608 tall, or the shorter 24h panel). The window may not shrink below that while it is open.
window.addEventListener?.('dashboard-size',()=>{if(dashboardState.open)setNotch(notch.expanded);});
window.addEventListener?.('resize',()=>{
  if(!dashboardState.open)return;
  const height=window.TokenTrackerDashboard?.windowHeight?.()||608;
  if(window.innerWidth<720||window.innerHeight<height){const api=window.pywebview?.api?.set_notch;if(api)Promise.resolve(api(true,height,reducedMotion(),'dashboard')).catch(()=>{});}
});
window.tokenTrackerOpenDashboard=tab=>openDashboard(tab);
window.tokenTrackerRecenter=()=>recenterWidget();
window.tokenTrackerSetWidgetLock=value=>applyWidgetLock(value);
// The compact widget never grows past this height (CSS px).
const COMPACT_MAX_NOTCH_HEIGHT=420;
// Height of everything around the tree list: the window is sized to this plus the list's content.
function notchChromeHeight() {
  const visibleHeight=selector=>{const node=$(selector);if(!node||node.hidden||node.getAttribute?.('aria-hidden')==='true')return 0;const rects=node.getClientRects?.();if(rects&&rects.length===0)return 0;return node.offsetHeight||0;};
  const fixed=['.titlebar','.resets','#weekly-resets','.totals'].reduce((sum,selector)=>sum+visibleHeight(selector),0)+
    (state.searchOpen?visibleHeight('#chats-head'):0);
  return fixed+16;
}
// Tallest the compact widget can become on the current monitor, in CSS px.
function runwayMaxNotchHeight() {
  let max=COMPACT_MAX_NOTCH_HEIGHT;
  const monitor=monitorState.current,scale=Number(monitor?.scale),height=Number(monitor?.workArea?.height)/scale;
  if(Number.isFinite(height)&&height>0)max=Math.min(max,height);
  return max;
}
function notchHeight() {
  if(notch.mode==='dashboard')return window.TokenTrackerDashboard?.windowHeight?.()||608;
  if(notch.mode==='inspect'&&state.sourceHeightHold)return state.sourceHeightHold;
  if(notch.mode==='idle'||notch.mode==='rest')return 100;
  if(notch.mode==='controls')return 110;
  const list=$('#chat-list');
  if(!notch.expanded||!list?.querySelectorAll)return 148;
  const chrome=notchChromeHeight();
  // The list's own content height includes variable wrapped rows and the empty state,
  // while avoiding the flex-filled height of its parent layout.
  const children=[...list.children||[]];
  const content=(children.length?children.reduce((sum,node)=>sum+(node.offsetHeight||0),0):0)||list.scrollHeight||[...list.querySelectorAll('.chat')].reduce((sum,row)=>sum+(row.offsetHeight||0),0)||children.reduce((sum,node)=>sum+(node.offsetHeight||0),0);
  const measured=Math.max(148,Math.ceil(chrome+content));
  return Math.min(runwayMaxNotchHeight(),Math.max(114,measured));
}
function scheduleNotchSize() {
  if(typeof requestAnimationFrame!=='function')return;
  cancelAnimationFrame(notch.frame);notch.frame=requestAnimationFrame(()=>{const changed=syncRunwayTreeConnectors();if(changed){scheduleNotchSize();return;}setNotch(notch.expanded,true);});
}
function runwayTreePathGeometry(rootRect, entries) {
  const byIdentity=new Map(entries.map(entry=>[entry.provider+':'+entry.id,entry])),paths=[],dots=new Map();let bottom=0;
  for(const child of entries){
    const parentKey=Object.prototype.hasOwnProperty.call(child,'parentKey')?child.parentKey:child.parentId?child.provider+':'+child.parentId:null;
    if(!parentKey)continue;
    const parent=entries.find(entry=>entry.key===parentKey)||byIdentity.get(parentKey);
    const parentRect=parent?.glanceRect||parent?.rowRect;
    if(!parent?.nameRect||!parentRect||!child.nameRect||!child.glanceRect||parentRect.width<=0||child.glanceRect.width<=0)continue;
    const parentDot=parent.dotRect,childDot=child.dotRect;
    const px=Math.max(0,parentDot?.width?parentDot.left-rootRect.left+parentDot.width/2:parentRect.left-rootRect.left+2),py=parentDot?.height?parentDot.top-rootRect.top+parentDot.height/2:parent.nameRect.top-rootRect.top+Math.min(parent.nameRect.height||19,19)/2;
    const cx=Math.max(0,childDot?.width?childDot.left-rootRect.left+childDot.width/2:child.glanceRect.left-rootRect.left+2),cy=childDot?.height?childDot.top-rootRect.top+childDot.height/2:child.nameRect.top-rootRect.top+Math.min(child.nameRect.height||19,19)/2;
    if(![px,py,cx,cy].every(Number.isFinite)||cy<0)continue;
    const direction=Math.sign(cx-px)||1,r=Math.max(0,Math.min(8,Math.abs(cx-px),Math.abs(cy-py)/2));
    paths.push({d:r?`M${px} ${py}V${cy-r}Q${px} ${cy} ${px+direction*r} ${cy}H${cx}`:`M${px} ${py}V${cy}H${cx}`,from:parentKey,to:child.key||child.id,endY:cy});
    for(const [x,y] of [[px,py],[cx,cy]])dots.set(x+':'+y,{cx:x,cy:y});
    bottom=Math.max(bottom,cy);
  }
  return {width:Math.max(1,rootRect.width),height:Math.max(1,Math.ceil(bottom+2)),paths,dots:[...dots.values()]};
}
let treeScrollFrame=null,treeScrollListenerAttached=false;
function scheduleRunwayTreeScrollSync(){
  if(treeScrollFrame!==null||typeof requestAnimationFrame!=='function')return;
  treeScrollFrame=requestAnimationFrame(()=>{treeScrollFrame=null;syncRunwayRootStuck();});
}
// Scrolling only changes which parent rows are stuck; connector geometry is measured from the unstuck position, so it never needs redrawing here.
function syncRunwayRootStuck(){
  const list=$('#chat-list');if(!realDom(list))return;
  const inspect=notch.mode==='inspect',rows=[];
  for(const root of list.children){if(!root.classList?.contains('chat'))continue;const row=root.querySelector?.('.row');if(row)rows.push([row,inspect&&row.getBoundingClientRect().top-root.getBoundingClientRect().top>1]);}
  for(const [row,stuck] of rows)cls(row,'runway-root-stuck',stuck);
}
function setupRunwayTreeScrollSync(){
  if(treeScrollListenerAttached)return;
  const list=$('#chat-list');if(!list?.addEventListener)return;
  list.addEventListener('scroll',scheduleRunwayTreeScrollSync,{passive:true});
  $('.layout')?.addEventListener?.('scroll',scheduleRunwayTreeScrollSync,{passive:true});treeScrollListenerAttached=true;
}
function syncRunwayTreeConnectors({updateTallRoot=true}={}) {
  const list=$('#chat-list');if(!realDom(list)||typeof document.createElementNS!=='function')return;
  const ns='http://www.w3.org/2000/svg',layoutHeight=updateTallRoot?($('.layout')?.clientHeight||0):0;let tallRootChanged=false;
  for(const root of [...list.children].filter(el=>el.classList?.contains('chat'))){
    let rootRect=root.getBoundingClientRect?.();const row=root.querySelector?.('.row');let rowRect=row?.getBoundingClientRect?.();
    if(!rootRect||!row||!rowRect)continue;
    if(updateTallRoot){
      const tallRoot=notch.mode==='inspect'&&layoutHeight>0&&rowRect.height>Math.max(50,layoutHeight/3),hasTallClass=Boolean(row.classList?.contains?.('runway-tall-root'));
      if(tallRoot!==hasTallClass){row.classList?.toggle?.('runway-tall-root',tallRoot);tallRootChanged=true;rootRect=root.getBoundingClientRect?.()||rootRect;rowRect=row.getBoundingClientRect?.()||rowRect;}
    }
    const stuckOffset=notch.mode==='inspect'?rowRect.top-rootRect.top:0;
    row.classList?.toggle?.('runway-root-stuck',stuckOffset>1);
    // Measure a stuck parent where it sits unstuck, so the tree does not change as the list scrolls.
    const unstick=rect=>rect&&stuckOffset>1?{left:rect.left,right:rect.right,width:rect.width,height:rect.height,top:rect.top-stuckOffset,bottom:rect.bottom-stuckOffset}:rect;
    rowRect=unstick(rowRect);
    const visible=[...root.querySelectorAll('.ag.runway-agent[data-sub]')].filter(el=>!el._leaving&&el.getClientRects?.().length!==0);
    const entries=[{key:root.dataset.key,...(()=>{const item=state.forest?.rows.find(candidate=>candidate.key===root.dataset.key),name=row.querySelector?.('.r-title'),anchor=row.querySelector?.('.runway-refined-tree-anchor');return {provider:item?.provider,id:item?.id,parentKey:treeParentKey(item),rowRect,messageRect:rowRect,nameRect:unstick(name?.getBoundingClientRect?.()),dotRect:unstick(anchor?.getBoundingClientRect?.())};})()}];
    for(const el of visible){const item=state.forest?.rows.find(candidate=>candidate.key===el.dataset.sub),glance=el.querySelector?.('.runway-agent-glance'),name=el.querySelector?.('.runway-agent-name'),anchor=glance?.querySelector?.('.runway-refined-tree-anchor');if(item)entries.push({key:item.key,provider:item.provider,id:item.id,parentKey:el.dataset.visibleParent||treeParentKey(item),rowRect:el.getBoundingClientRect?.(),messageRect:glance?.getBoundingClientRect?.(),nameRect:name?.getBoundingClientRect?.(),glanceRect:glance?.getBoundingClientRect?.(),dotRect:anchor?.getBoundingClientRect?.()});}
    const geometry=runwayTreePathGeometry(rootRect,entries),paths=geometry.paths.map(path=>path.d),dots=geometry.dots;
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
// Idle ticks re-render the same values; skip identical writes so they don't churn the DOM.
function put(el,key,value){if(el&&el[key]!==value)el[key]=value;}
function attr(el,name,value){if(el&&el.getAttribute?.(name)!==value)el.setAttribute?.(name,value);}
function cls(el,name,on){on=Boolean(on);if(el?.classList&&el.classList.contains?.(name)!==on)el.classList.toggle(name,on);}
function setNotch(expanded, treeSynced=false) {
  const mode=compactModeForState();
  if(notch.mode&&notch.mode!==mode)closeToolbarMenu();
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
  if(dashboardView){put(dashboardView,'hidden',!dashboardVisible);put(dashboardView,'inert',!dashboardVisible);attr(dashboardView,'aria-hidden',String(!dashboardVisible));}
  const hidden=!notch.expanded,sections=[$('.chats'),$('.totals')].filter(Boolean);
  if(hidden&&sections.some(section=>section.contains?.(document.activeElement)))$('#sync-age')?.focus?.({preventScroll:true});
  for(const section of sections){if(section.inert!==hidden)section.inert=hidden;if(section.getAttribute?.('aria-hidden')!==String(hidden))section.setAttribute?.('aria-hidden',String(hidden));}
  for(const name of ['idle','rest','controls','inspect','dashboard'])cls(document.body,'compact-'+name,mode===name);
  if(document.body?.dataset&&document.body.dataset.notchMode!==mode)document.body.dataset.notchMode=mode;
  cls(document.body,'notch-expanded',notch.expanded);
  cls(document.body,'notch-collapsed',!notch.expanded);
  cls(document.body,'notch-auto-open',notch.autoExpanded&&notch.expanded);
  cls(document.body,'notch-inspecting',mode==='inspect');
  if(notch.expanded&&!treeSynced){if(typeof requestAnimationFrame==='function'){scheduleNotchSize();return;}syncRunwayTreeConnectors();}
  renderRunwayCounts(notch.counts);
  const api=window.pywebview?.api?.set_notch,height=notchHeight(),reduced=reducedMotion(),signature=mode+':'+notch.expanded+':'+height+':'+reduced;
  if(api&&widgetLock.known){if(mode==='inspect')noteInspectRequest(signature);sendCompactNotch(api,[notch.expanded||mode==='dashboard',height,reduced,mode],signature);}
  if(dashboardState.restoreMode===mode&&(mode!=='inspect'||treeSynced))dashboardState.restoreMode=null;
}
function expandForNewWork() {
  if(notch.autoTimer!=null){notch.autoExpanded=true;setNotch(true);return;}
  notch.autoExpanded=true;setNotch(true);notch.autoClosePending=false;
  notch.autoTimer=setTimeout(()=>{notch.autoTimer=null;if(state.popover){notch.autoClosePending=true;return;}notch.autoExpanded=false;setNotch(false);},3000);
}
// Reveal only on subagent lifecycle (started, finished, back after finishing) and on new questions;
// messages, turns and token updates never pop the widget. A status flap shorter than
// LIFECYCLE_SETTLE_MS counts as neither a finish nor a return.
const LIFECYCLE_SETTLE_MS=8000;
function lifecycleStates(current,now=Date.now()) {
  const next=new Map();let changed=false;const changedItems=[];
  const baseline=notch.activeKeys===null;
  const keys=new Set([...current.keys(),...notch.workStates.keys()]);
  for(const key of keys){
    const previous=notch.workStates.get(key),item={...(current.get(key)||{...previous,active:false})};
    if(item.active){
      const born=!baseline&&!previous&&item.subagent,back=Boolean(item.subagent&&previous&&!previous.active&&(previous.finished||!previous.wasActive));
      const asked=Boolean(item.waiting&&(!previous?.waiting||previous.questionToken!==item.questionToken));
      item.wasActive=true;item.finished=false;item.inactiveSince=0;
      if(!baseline&&(born||back||asked)){changed=true;changedItems.push(item);}
    }else{
      item.wasActive=Boolean(previous?.wasActive||previous?.active);item.finished=Boolean(previous?.finished);
      item.inactiveSince=previous?.active?now:(previous?.inactiveSince||0);
      if(item.subagent&&item.wasActive&&!item.finished&&item.inactiveSince&&now-item.inactiveSince>=LIFECYCLE_SETTLE_MS){
        item.finished=true;item.wasActive=false;if(!baseline){changed=true;changedItems.push(item);}
      }
    }
    next.set(key,item);
  }
  return {next,changed,changedItems};
}
function syncCompactNotch(count,items) {
  const fresh=runwayCurrentEvidenceFresh(),current=fresh?new Map(items.map(item=>typeof item==="string"?[item,{key:item,active:true}]:[item.key,item])):new Map();
  const activeKeys=new Set([...current.values()].filter(item=>item.active).map(item=>item.key));
  const lifecycle=lifecycleStates(current);
  let newWork=notch.activeKeys===null?activeKeys.size>0:lifecycle.changed;
  const newWorkItems=notch.activeKeys===null?[...current.values()].filter(item=>item.active):lifecycle.changedItems;
  const nextStates=lifecycle.next;
  const previousActive=notch.activeKeys;
  notch.workStates=nextStates;notch.activeKeys=activeKeys;notch.active=fresh?count:0;
  if(!fresh){
    clearTimeout(notch.inspectTimer);clearTimeout(notch.exitTimer);clearTimeout(notch.autoTimer);clearTimeout(notch.timer);
    notch.inspectTimer=notch.exitTimer=notch.autoTimer=notch.timer=null;notch.autoExpanded=false;notch.autoClosePending=false;
    if(!notch.manual&&!notch.keyboardInspecting&&!state.popover&&!(notch.hover&&!notch.hoverSuppressed))notch.inspecting=false;
    if(!notch.hover&&!compactHasFocus())notch.contentHover=false;
    setNotch(false);return;
  }
  if(newWork&&!notch.active)notch.autoExpanded=true;
  // The source still follows any chat that starts, even when that start doesn't reveal the widget.
  const started=previousActive===null?[]:[...current.values()].filter(item=>item.active&&!previousActive.has(item.key));
  if(newWork||started.length)selectSourceForNewWork(newWork?newWorkItems:started);
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
  syncCompactNotch(count,items);
}
function setupNotch() {
  setupRunwayTreeScrollSync();
  const widget=$('.widget');
  const pointerPoint=event=>Number.isFinite(event.screenX)&&Number.isFinite(event.screenY)?{x:event.screenX,y:event.screenY}:{x:event.clientX,y:event.clientY};
  const inHeader=event=>{
    if(event.target?.closest?.('.titlebar'))return true;
    const rect=$('.titlebar')?.getBoundingClientRect?.();
    return Boolean(rect&&event.clientX>=rect.left&&event.clientX<=rect.right&&event.clientY>=rect.top&&event.clientY<=rect.bottom);
  };
  const scheduleInspect=()=>{if(notch.inspectTimer!=null||!state.runwaySnapshotPainted)return;notch.inspectTimer=setTimeout(()=>{notch.inspectTimer=null;if(notch.hover&&!notch.hoverSuppressed&&state.runwaySnapshotPainted){notch.inspecting=true;setNotch(true);}},60);};
  const leave=()=>{clearTimeout(notch.exitTimer);notch.exitTimer=setTimeout(()=>{
    if(!notch.hover&&!compactHasFocus())closeToolbarMenu();
    const held=keepOpenState.value||state.searchOpen||Boolean(state.popover);
    if(!notch.hover&&!compactHasFocus()&&!notch.keyboardInspecting&&!held){const wasInspecting=notch.inspecting||notch.manual;notch.inspecting=false;notch.manual=false;if(wasInspecting){clearTimeout(notch.autoTimer);notch.autoTimer=null;notch.autoExpanded=false;}}
    setNotch(false);
  },110);};
  widget.addEventListener('pointerenter',event=>{const point=pointerPoint(event),deliberateEntry=notch.globalPointerX!=null&&notch.globalPointerY!=null&&Math.hypot(point.x-notch.globalPointerX,point.y-notch.globalPointerY)>=4;notch.hover=true;notch.contentHover=!inHeader(event);notch.pointerX=point.x;notch.pointerY=point.y;clearTimeout(notch.exitTimer);clearTimeout(notch.timer);if(!notch.hoverSuppressed&&deliberateEntry)scheduleInspect();setNotch(false);});
  widget.addEventListener('pointermove',event=>{
    const point=pointerPoint(event),moved=notch.pointerX!=null&&Math.hypot(point.x-notch.pointerX,point.y-notch.pointerY)>=4;
    if(moved||notch.inspecting){notch.pointerX=point.x;notch.pointerY=point.y;}
    notch.contentHover=notch.hover&&!inHeader(event);
    if(moved&&!notch.keyboardInspecting&&!notch.inspecting&&!notch.hoverSuppressed)scheduleInspect();
    setNotch(false);
  });
  widget.addEventListener('pointerleave',()=>{notch.hover=false;notch.hoverSuppressed=false;notch.contentHover=false;clearTimeout(notch.inspectTimer);notch.inspectTimer=null;leave();});
  widget.addEventListener('focusin',()=>{clearTimeout(notch.exitTimer);notch.exitTimer=null;setNotch(false);});
  widget.addEventListener('focusout',event=>{if(event.relatedTarget&&widget.contains?.(event.relatedTarget))return;leave();});
  const closeInspection=()=>{notch.keyboardInspecting=false;notch.inspecting=false;notch.manual=false;notch.contentHover=false;clearTimeout(notch.inspectTimer);clearTimeout(notch.autoTimer);clearTimeout(notch.exitTimer);clearTimeout(notch.timer);notch.inspectTimer=notch.autoTimer=notch.exitTimer=notch.timer=null;notch.autoExpanded=false;notch.autoClosePending=false;notch.hoverSuppressed=notch.hover;if(compactHasFocus())document.activeElement?.blur?.();if(!state.popover)setNotch(false);};
  document.addEventListener('pointerdown',()=>{compactInputModality='pointer';},true);document.addEventListener('keydown',()=>{compactInputModality='keyboard';},true);
  document.addEventListener('pointermove',event=>{const point=pointerPoint(event);notch.globalPointerX=point.x;notch.globalPointerY=point.y;},true);
  window.tokenTrackerInspect=()=>{if(!state.runwaySnapshotPainted)return false;notch.keyboardInspecting=true;notch.manual=true;notch.inspecting=true;clearTimeout(notch.exitTimer);setNotch(true);if(widget&&!widget.hasAttribute?.('tabindex'))widget.setAttribute?.('tabindex','-1');widget?.focus?.({preventScroll:true});return true;};
  window.tokenTrackerCloseInspection=closeInspection;
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!state.popover&&!state.searchOpen){closeInspection();}});
  window.addEventListener('pywebviewready',()=>{notch.sent=null;compactBridge.appliedSignature=null;compactBridge.pending=null;void syncKeepOpen();void syncWidgetLock().then(()=>syncMonitors());});
  if(window.pywebview?.api?.get_widget_lock)void syncWidgetLock().then(()=>syncMonitors());
  $('#monitor-toggle')?.addEventListener('click',toggleMonitor);
  updateWidgetLockButton();updateKeepOpenButton();void syncKeepOpen();
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
  if(notch.mode!=='inspect'||signature!==inspectExpectedSignature
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
  return '<div class="q-opts">'+(opts.length?'<span class="q-action-note">Open Codex to answer · queue is a later message</span>':'')+opts.map((text,i)=>'<div class="q-choice"><button class="q-opt" data-action="answer" data-answer="'+esc(text)+'"'+t+' aria-label="Open Codex to answer: '+esc(text)+'" title="Open this task in Codex and select the choice in its question prompt. The choice is also copied for reference."><b>'+(i+1)+'</b><span>Open in Codex</span> · '+esc(text)+ICON.check+'</button><button class="q-open q-queue" data-action="queue-answer" data-answer="'+esc(text)+'"'+t+' title="Queue this as a message for the next turn. It will not answer the pending question."><span>Queue to chat</span></button></div>').join("")+
    (thread?'<button class="q-open" data-action="open"'+t+' title="Open this chat in Codex">'+ICON.open+'<span>'+(opts.length?"Open":"Answer in Codex")+'</span>'+ICON.check+'</button>':'')+'</div>';
}
const QUESTION_STALE_MS=24*60*60*1000;
const isAsking = chat => (!chat.pendingQuestionExpired&&!(stamp(chat.pendingQuestionAt)&&Date.now()-stamp(chat.pendingQuestionAt)>=QUESTION_STALE_MS))&&(Boolean(chat.pendingQuestion)||normalizeStatus(chat.status)==="needs-input");
// A failure is only actionable while it is recent (30 min).
const recentFail = chat => chat.status==="failed"&&Date.now()-stamp(chat.updatedAt)<FAIL_WINDOW_MS;
const needsAttention = chat => isAsking(chat)||recentFail(chat);
const agentLive = chat => chat.status==="running"||chat.status==="compacting"||chat.status==="quiet"||isAsking(chat);
const runwayTerminated = chat => Boolean(chat.deleted||chat.isDeleted)||[chat.stateDetail,chat.sourceStatus].some(value=>/^(?:completed|finished|stopped|interrupted|deleted|expired)$/i.test(String(value||'')));
const runwayWaiting = chat => !chat.telemetryUnavailable&&!runwayTerminated(chat)&&isAsking(chat);
function runwayExecutionView(chat) {
  if(runwayWaiting(chat)||runwayTerminated(chat)||chat.telemetryUnavailable||stamp(chat.executionTerminalAt))return null;
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
  if(chat.executionStatus!==undefined)return runwayWaiting(chat)?'needs-input':['idle','failed'].includes(normalizeStatus(chat.executionStatus))?normalizeStatus(chat.executionStatus):'unknown';
  return normalizeStatus(chat.status);
};
const runwayRunning = chat => !runwayTerminated(chat)&&!runwayWaiting(chat)&&(chat.executionStatus!==undefined?Boolean(runwayExecutionView(chat)):['running','compacting'].includes(chat.status));
const runwayActive = chat => runwayRunning(chat)||runwayWaiting(chat);
function runwayActivityEvidence(chat) {
  if(runwayTerminated(chat))return {status:'inactive',activity:null};
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
const runwayVisibleActive=chat=>chat.retainedRun===true&&Date.now()-stamp(chat.stoppedAt)<900000||runwayCurrentEvidenceFresh()&&(chat.runBindingUnresolved===true||runwayActive(chat)||runwayRecentlyUnavailable(chat));
function runwayCounts(forest) {
  const roots=forest.roots.filter(chat=>!state.hiddenChats.has(chat.key));
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
  const nodes=[$('#notch-counts'),...[$('#compact-counts')]].filter(Boolean);if(!nodes.length)return;
  if(!counts){for(const node of nodes){if(node._runwayCountHTML!==''){node.innerHTML='';node._runwayCountHTML='';}if(node.title)node.title='';if(node.getAttribute?.('aria-label'))node.removeAttribute?.('aria-label');}return;}
  const chatsWord=counts.roots===1?'chat':'chats';
  const label=counts.roots+' chat group'+(counts.roots===1?'':'s')+', '+counts.total+' active, '+counts.waiting+' waiting';
  const badge=(slot,value,unit,tone,path,meaning,visible=true)=>{if(unit==='chat'||unit==='chats')meaning='visible chat groups';const hidden=visible?'':' aria-hidden="true"';const jump=slot==='waiting'&&visible?' data-action="jump-waiting" role="button" tabindex="0"':'';return '<span data-count-slot="'+slot+'" class="runway-count runway-count-'+slot+' t-'+tone+(visible?'':' is-empty')+'"'+hidden+jump+' aria-label="'+value+' '+meaning+(jump?', show the first':'')+'" title="'+value+' '+meaning+(jump?' · click to show':'')+'"><svg class="ic" viewBox="0 0 12 12" aria-hidden="true"><path d="'+path+'"/></svg><span class="count-value">'+(visible?value:'')+'</span><span class="count-unit">'+(visible?unit:'')+'</span></span>';};
  const html=badge('groups',counts.roots,chatsWord,'live','M2 2h8v6H5l-3 2Z',chatsWord,counts.roots>0)+badge('active',counts.total,'active','live','M3 1v8h6M3 4h6M9 2v4M9 7v4','active',counts.total>0)+badge('waiting',counts.waiting,'waiting','attn','M6 2v5M6 9h.01','waiting',Boolean(counts.waiting));
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
  // long-context is a pricing tier (server key suffix "|long-context"), not a model family: label the base model plus a tier note.
  const tier=model.match(/^(.*?)(?:\|long-context|-long-context)$/i);
  if(tier){const base=runwayModelLabel({model:tier[1]});return base?base+" · long context":"";}
  let match=model.match(/^gpt-(\d+(?:\.\d+)?)(?:-(.+))?$/i);
  if(match){const family=(match[2]||"").split("-").filter(Boolean).at(-1)||"";return "GPT-"+match[1]+(family?" "+family[0].toUpperCase()+family.slice(1).toLowerCase():"");}
  match=model.match(/^claude-(?:(opus|sonnet|haiku)-(\d+)[.-](\d+)|(\d+)[.-](\d+)-(opus|sonnet|haiku))(?:-.+)?$/i);
  if(match){const family=match[1]||match[6],major=match[2]||match[4],minor=match[3]||match[5];return family[0].toUpperCase()+family.slice(1).toLowerCase()+" "+major+"."+minor;}
  match=model.match(/^claude-(opus|sonnet|haiku)(?:-.+)?$/i);
  if(match)return match[1][0].toUpperCase()+match[1].slice(1).toLowerCase();
  return model;
}
window.TokenTrackerFormatModel=runwayModelLabel;
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
  // Claude subagents carry the Agent tool's description as their title; the type is the fallback.
  if(chat.recordSource==="hook"&&chat.parentId&&chat.title){const described=String(chat.title).trim();if(described)return described.charAt(0).toUpperCase()+described.slice(1);}
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
const runwaySource = snapshot => sourceNames(snapshot);
function providersFor(snapshot) {
  const visible=visibleProviderNames(snapshot),available=(snapshot?.providers||[]);
  return runwaySource(snapshot).filter(name=>visible.has(name)).map(name=>available.find(p=>p.name===name)||{name,connected:false,chats:[]});
}
// Chat providers plus providers shown only as quota meters (reset block and alerts).
function limitProvidersFor(snapshot) {
  const visible=visibleProviderNames(snapshot),available=snapshot?.providers||[];
  const names=sourceNames(snapshot).filter(name=>visible.has(name));
  return names.flatMap(name=>{
    const provider=available.find(item=>item.name===name),bootstrap=quotaBootstrap.providers[name];
    const hasQuota=item=>Boolean(item&&["fiveHour","weekly"].some(key=>{
      const value=item[key];return value&&typeof value==="object"&&(safeNumber(value.percent)!=null||resetAt(value,item.limitsUpdatedAt)!=null);
    }));
    const selected=hasQuota(provider)?provider:hasQuota(bootstrap)?bootstrap:null;
    return selected?[selected]:[];
  });
}
function snapshotParentKey(provider, raw, edges=null) {
  const key=keyOf({...raw,provider:provider.name});
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
// State precedence: Needs you (0, 1) > Running (2) > Finished (2.5, idle after a run that ended in the last 10 minutes) > Idle (3).
function runwayRank(chat) {
  if(chat.pendingQuestion)return 0;
  const rank=statusRank(chat.status);
  return rank===3&&stamp(chat.runEndedAt)>Date.now()-600000?2.5:rank;
}
function buildForest(providers, graph=undefined) {
  if(!Array.isArray(providers)&&Array.isArray(providers?.providers)){graph=providers.graph;providers=providers.providers;}
  const graphValue=graph,edges=graphValue&&typeof graphValue.edges==="object"&&!Array.isArray(graphValue.edges)?graphValue.edges:null;
  const map=new Map();
  for(const provider of providers)for(const raw of provider.chats||[]) {
    const chat={...raw,provider:provider.name,status:normalizeStatus(raw.status),children:[]};
    chat.sourceStatus=raw.status;
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
    chat.priority=Math.min(...[chat,...chat.descendants].map(runwayRank));
    const list=$('#chat-list'),interacting=state.pointerInList||list?.contains?.(document.activeElement)||state.popover?.dataset?.action==='runway-question';
    if(!interacting)chat.children.sort((a,b)=>b.latest-a.latest);
  }
  roots.forEach(root=>aggregate(root,root));
  {
    placeRows([...map.values()]);
    const byPlace=(a,b)=>rowPlace.rank.get(a.key)-rowPlace.rank.get(b.key);
    const visit=chat=>{chat.children.sort(byPlace);chat.descendants=[];for(const child of chat.children){visit(child);chat.descendants.push(child,...child.descendants);}};
    roots.forEach(visit);
  }
  return {roots,rows:[...map.values()]};
}
// Rows keep the place they first appeared in, so activity never shuffles the tree.
// The first load orders by latest activity; rows seen later go on top.
const rowPlace={rank:new Map(),next:-1};
function placeRows(rows) {
  const fresh=rows.filter(chat=>!rowPlace.rank.has(chat.key));
  if(!rowPlace.rank.size)fresh.sort((a,b)=>(a.priority??3)-(b.priority??3)||b.latest-a.latest).forEach((chat,index)=>rowPlace.rank.set(chat.key,index));
  else fresh.sort((a,b)=>a.latest-b.latest).forEach(chat=>rowPlace.rank.set(chat.key,rowPlace.next--));
}
function stabilizeRunwayForest(forest, previous) {
  const container=$('#chat-list'),interacting=state.pointerInList||container?.contains?.(document.activeElement)||state.popover?.dataset?.action==='runway-question';
  if(!interacting||!previous)return;
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
  return !query.text||[chat.title,agentLabel(chat),chat.model,modelLabel(chat),chat.workspace,chat.assignment,chat.objective,chat.latestRequest,chat.latestUpdate].join(" ").toLowerCase().includes(query.text);
}
const inWindow = (chat, days) => Date.now()-chat.latest<=days*86400000;
// Latest tree-wide activity first. Pins keep older chats in view but never reorder the timeline.
function visibleRoots(forest, days=state.windowDays) {
  const query=parseQuery(state.query);
  return [...forest.roots].sort((a,b)=>(rowPlace.rank.get(a.key)??0)-(rowPlace.rank.get(b.key)??0)).filter(chat=>{
    const rows=[chat,...chat.descendants],matches=!query.active||rows.some(row=>matchesQuery(row,query));
    const hiddenMatch=query.flags.has("hidden")&&rows.some(row=>state.hiddenChats.has(row.key)&&matchesQuery(row,query));
    return (state.showHidden||!state.hiddenChats.has(chat.key)||hiddenMatch)&&
      (hiddenMatch||rows.some(row=>row.retainedRun===true||runwayVisibleActive(row))||chat.descendants.some(runwayActionable))&&matches;
  });
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
// One shared staleness rule: after STALE_AFTER_MS without a successful refresh the widget says so once ("Updated 3m ago")
// and elapsed timers stop at the last good update instead of counting on or going blank.
const STALE_AFTER_MS=60000;
const dataIsStale=()=>Boolean(state.lastOkAt&&Date.now()-state.lastOkAt>=STALE_AFTER_MS);
const clockNow=()=>dataIsStale()?state.lastOkAt:Date.now();
function runInfo(chat) {
  if(chat.retainedRun===true){
    const start=stamp(chat.activeRunStartedAt||chat.runStartedAt||chat.executionSince),end=stamp(chat.stoppedAt);
    return start&&end>=start?{live:false,seconds:(end-start)/1000}:null;
  }
  const execution=runwayExecutionView(chat);
  if(execution){
    const start=stamp(chat.executionSince),now=clockNow();
    return start&&start<=now?{seconds:Math.max(0,(now-start)/1000),live:true}:null;
  }
  if(chat.executionStatus!==undefined)return null;
  const start=stamp(agentLive(chat)?chat.activeRunStartedAt||chat.runStartedAt:chat.runStartedAt);
  if(!start)return null;
  const live=agentLive(chat)&&!chat.runEndedAt;
  const end=live?clockNow():stamp(chat.runEndedAt)||chat.latest||stamp(chat.updatedAt)||start;
  return {live,seconds:Math.max(0,(end-start)/1000)};
}

function questionDismissToken(chat) {
  return "question:"+chat.key+":"+JSON.stringify([
    chat.pendingQuestionId??chat.questionId??null,
    chat.pendingQuestion||"",chat.pendingOptions||[],chat.pendingQuestionAt||null,chat.attentionSince||null
  ]);
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
  let milliseconds=383;
  try{
    const raw=window.getComputedStyle?.(body)?.getPropertyValue?.('--notch-open-suppression')?.trim();
    const match=raw?.match(/^(\d+(?:\.\d+)?)(ms|s)$/);
    if(match)milliseconds=Number(match[1])*(match[2]==='s'?1000:1);
  }catch{}
  state.notchOpeningTimer=setTimeout(()=>{state.notchOpeningTimer=null;body.classList.remove?.('notch-opening');},milliseconds);
}

/* ---------- keyed DOM patching (falls back to innerHTML without a real DOM, e.g. tests) ---------- */
const realDom = node => Boolean(node&&node.children&&typeof document.createElement==="function");
function attrString(attrs) { return Object.entries(attrs||{}).filter(([,v])=>v!=null).map(([k,v])=>' '+k+'="'+esc(v)+'"').join(""); }
function syncPatchedAttributes(current,next) {
  const oldEvent=current.getAttribute?.('data-model-change-at'),newEvent=next.getAttribute?.('data-model-change-at');
  if(oldEvent&&newEvent&&oldEvent!==newEvent&&current.matches?.('.runway-model-name.model-changed')&&next.matches?.('.runway-model-name.model-changed')){current.classList.remove('model-changed');void current.offsetWidth;}
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
  if(!state.runwaySnapshotPainted||state.sourceSwitching)return;
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
  const focused=container.contains?.(document.activeElement)?document.activeElement:null;
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
    if(el._html!==entry.html){if(entry.cls.includes('runway-agent'))runwayPatchContent(el,entry.html);else el.innerHTML=entry.html;el._html=entry.html;syncBreath(el);}
    el._entry=entry;
    const next=prev?prev.nextSibling:container.firstChild;
    if(next!==el)container.insertBefore(el,next);
    prev=el;
    if(fresh&&enter)enter(el);
  }
  if(focused?.isConnected&&document.activeElement!==focused)focused.focus?.({preventScroll:true});
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
  if(!watcher||!w)return {tone:"mute",tip:""};
  if(!watcher.enabled||!watcher.running)return {tone:"mute",tip:"Auto-greet off"};
  const next=safeNumber(w.nextGreetingAt),last=stamp(w.lastTriggerAt),verified=w.verificationEvidence==="usage_and_deadline"&&last&&stamp(w.lastVerifiedAt)>=last;
  const tip=["Auto-greet",next?clock(next*1000):null,last?"last "+clock(last)+(verified?" ✓":""):null].filter(Boolean).join(" · ");
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
  // A sample stamped up to a minute ahead of this clock (clock skew, a paused page) counts as fresh, not as missing.
  const sampled=stamp(quotaAt),rawAge=sampled?Date.now()-sampled:null,age=rawAge!=null&&rawAge<0&&rawAge>=-60000?0:rawAge;
  const unverified=provider.name==="Claude"&&provider.identityVerified!==true;
  const measured=!unverified&&item.lastKnown!==true&&provider.connected!==false&&used!=null&&sampled&&age!=null&&age>=0&&age<=600000;
  const observedParts=used!=null&&sampled&&age!=null&&age>=0?resetParts(item,quotaAt,key):{day:"",time:"",countdown:"",due:false,target:null,seconds:null};
  const maxAge=key==="weekly"?7*86400000:86400000;
  const lastKnown=Boolean(!measured&&used!=null&&sampled&&age!=null&&age>=0&&(unverified?age<maxAge:item.lastKnown===true?(observedParts.target!=null&&observedParts.seconds>0):age>600000&&age<maxAge&&(observedParts.target==null||observedParts.seconds>0)));
  const expired=Boolean(!measured&&used!=null&&sampled&&age!=null&&age>600000&&observedParts.target!=null&&observedParts.seconds<=0);
  const shownUsed=(measured||lastKnown)&&used!=null?Math.max(0,Math.min(100,used)):null;
  const parts=measured||lastKnown?observedParts:{day:"",time:"",countdown:"",due:false,target:null,seconds:null};
  const hasReset=Boolean((measured&&parts.target)||(lastKnown&&parts.target&&parts.seconds>0));
  const runwayCountdown=parts.seconds==null?"":parts.seconds<=0?"due":key==="weekly"?(parts.seconds>=86400?Math.floor(parts.seconds/86400)+"d "+Math.floor(parts.seconds%86400/3600)+"h":Math.floor(parts.seconds/3600)+"h "+pad2(Math.floor(parts.seconds%3600/60))+"m"):parts.seconds>=3600?Math.floor(parts.seconds/3600)+"h "+pad2(Math.floor(parts.seconds%3600/60))+"m":parts.seconds>=60?Math.floor(parts.seconds/60)+"m "+pad2(parts.seconds%60)+"s":parts.seconds+"s";
  const usedLabel=expired?"reset":shownUsed==null?"":Math.round(shownUsed)+"%",usedText=expired?"reset":usedLabel?usedLabel+" used":"";
  const lastKnownAge=lastKnown?quotaAgeLabel(sampled)+" ago":"";
  return {measured,lastKnown,lastKnownAge,expired,hasValue:shownUsed!=null,usedPercent:shownUsed,usedLabel,usedText,resetTarget:hasReset&&parts.target!=null?Number(parts.target):null,
    tone:shownUsed==null||lastKnown?"":shownUsed>80?"err":shownUsed>50?"attn":"",day:!hasReset?"":parts.day,time:!hasReset?"":parts.time,countdown:hasReset?runwayCountdown:"",due:parts.due,
    // Keep the last-known age beside each independently rendered quota value.
    stale:"",
    tip:(provider.name==="Claude"&&provider.identityVerified!==true?"Account unverified · ":provider.name==="Claude"&&provider.identityLabel?provider.identityLabel+" · ":"")+(lastKnown?"Last reported by "+provider.name+" "+lastKnownAge:sampled&&measured?"Quota sample at "+clock(sampled):"")};
}
function clearRunwayArrival(el) {
  if(!el)return;
  clearTimeout(el._runwayArrivalTimer);el._runwayArrivalTimer=null;
  if(el._runwayArrivalEnd)el.removeEventListener?.('animationend',el._runwayArrivalEnd);
  el._runwayArrivalEnd=null;el.classList?.remove('runway-row-enter');
}
function runwayArrivalEnter(el) {
  if(!el||reducedMotion()||state.sourceSwitching||document.body?.classList?.contains?.('notch-opening')||Date.now()<state.startupCalmUntil)return;
  clearRunwayArrival(el);el.classList.add('runway-row-enter');
  const finish=event=>{if(event&& (event.target!==el||event.animationName!=='runway-row-arrival'))return;clearRunwayArrival(el);};
  el._runwayArrivalEnd=finish;el.addEventListener?.('animationend',finish);
  el._runwayArrivalTimer=setTimeout(()=>finish(),350);
}
function runwayMotionOnce(el,className,animationName,targetSelector=null) {
  if(!el||reducedMotion()||Date.now()<state.startupCalmUntil)return;
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
  if(reducedMotion()||Date.now()<state.startupCalmUntil||el.getClientRects&&el.getClientRects().length===0){el.remove();return;}
  el._leaving=true;clearRunwayArrival(el);el.classList?.add('runway-exit');
  const finish=event=>{if(event?.animationName!=='runway-row-exit')return;el.removeEventListener?.('animationend',finish);el.removeEventListener?.('animationcancel',finish);el.remove();scheduleNotchSize();};
  el.addEventListener?.('animationend',finish);el.addEventListener?.('animationcancel',finish);
  setTimeout(()=>{if(el.isConnected)finish({animationName:'runway-row-exit'});},250);
}
// Resets the provider granted but the user has not spent yet; the soonest expiry leads the tooltip.
function bankedResetView(banked) {
  const reported=safeNumber(banked?.available);if(!reported||reported<1)return {text:"",tip:""};
  // A cached or restored snapshot can outlive a reset's expiry, so expired entries are dropped here too.
  const listed=Array.isArray(banked.items)?banked.items:[],items=listed.filter(item=>{const at=safeNumber(item?.expiresAt);return at==null||at*1000>Date.now();});
  const count=Math.round(reported)-(listed.length-items.length);if(count<1)return {text:"",tip:""};
  const lines=items.map(item=>{const at=safeNumber(item?.expiresAt);return String(item?.title||"Limit reset")+(at?" · expires "+new Date(at*1000).toLocaleDateString([], {month:"short",day:"numeric"}):"");});
  return {text:"↻ "+count,tip:Math.round(count)+" banked reset"+(count===1?"":"s")+(lines.length?": "+lines.join("; "):"")};
}
function renderResets(providers) {
  const node=$("#resets"),weeklyNode=$("#weekly-resets");if(!node)return;
  const shape=providers.map(p=>p.name).join("|");
  node.dataset.providerCount=String(providers.length);if(weeklyNode)weeklyNode.dataset.providerCount=String(providers.length);
  if(node.dataset.shape!==shape) {
    node.dataset.shape=shape;
    const markup=key=>providers.map((p,i)=>{
      const id="r"+i+"-"+key,providerClass=p.name==="Claude"?"provider-claude":"provider-codex";
      return '<div id="'+id+'-row" class="rr rr-'+key+' '+providerClass+'"><div class="rr-head"><span class="rr-label">'+(key==="fiveHour"?"5-hour":"Weekly")+'</span><span class="rr-provider" id="'+id+'-name">'+esc(p.name)+'</span>'+
        (key==="fiveHour"&&p.name==="Codex"?'<button class="greet t-mute" id="'+id+'-greet" data-action="watcher" data-provider="'+esc(p.name)+'" aria-label="Auto-greet status"><i></i></button>':'')+
        '<span class="rr-stale" id="'+id+'-stale"></span>'+(key==="weekly"?'<span class="rr-banked" id="'+id+'-banked" hidden></span>':'')+'<span class="rr-left" id="'+id+'-left"><span class="rr-value"><b>\uFFFD</b><i>used</i></span></span></div>'+
        '<div class="rr-moment"><span class="rr-day" id="'+id+'-day"></span><span class="rr-time" id="'+id+'-time">\uFFFD:\uFFFD</span><span class="rr-cd" id="'+id+'-cd"></span></div>'+
        '<div class="meter" role="progressbar" aria-label="'+(key==="fiveHour"?"5-hour":"weekly")+' limit used" aria-valuemin="0" aria-valuemax="100" id="'+id+'-meter"><i id="'+id+'-bar"></i></div></div>';
    }).join("");
    node.innerHTML=markup("fiveHour");if(weeklyNode)weeklyNode.innerHTML=markup("weekly");
  }
  providers.forEach((p,i)=>["fiveHour","weekly"].forEach(key=>{
    const id="#r"+i+"-"+key,view=resetView(p,key),row=$(id+"-row");put(row,"title",view.tip);
    const set=(suffix,text)=>{const el=$(id+suffix);if(el&&el.textContent!==text)el.textContent=text;};
    const left=$(id+"-left");if(left){const html=view.hasValue?'<span class="rr-value"><b>'+esc(view.usedLabel)+'</b><i>'+esc(view.lastKnown?view.lastKnownAge.split(' ')[0]+' ago':'used')+'</i></span>':view.expired?'<span class="rr-reset-label">reset</span>':'<span class="rr-value"><b></b><i></i></span>';if(left.innerHTML!==html)left.innerHTML=html;put(left,"title",view.tip);}
    set("-day",view.day);set("-time",view.time);set("-cd",view.countdown);set("-stale",view.stale);
    put(left,"className","rr-left"+(view.lastKnown?" last-known":"")+(view.tone?" t-"+view.tone:""));put(left,"title",view.tip);
    const cd=$(id+"-cd");if(cd){put(cd,"className","rr-cd"+(view.due?" t-attn":""));const countdownText=view.countdown||'';if(cd.textContent!==countdownText)cd.textContent=countdownText;put(cd,"title",countdownText?'Resets '+view.day+' '+view.time:'');attr(cd,'aria-hidden',String(!countdownText));}
    const meter=$(id+"-meter");if(meter){put(meter,"className","meter"+(view.lastKnown?" last-known":""));if(!view.hasValue){meter.removeAttribute?.('aria-valuenow');meter.removeAttribute?.('aria-valuetext');meter.setAttribute?.('aria-hidden','true');}else{attr(meter,"aria-valuenow",String(Math.round(view.usedPercent)));attr(meter,'aria-valuetext',(view.lastKnown?"Last known: ":"")+Math.round(view.usedPercent)+"% used");meter.removeAttribute?.('aria-hidden');}}
    const bar=$(id+"-bar");if(bar){const width=(view.usedPercent??0)+"%";if(bar.style.width!==width)bar.style.width=width;put(bar,"className",view.lastKnown?"last-known":view.tone?"t-"+view.tone:"");}
    if(key==="fiveHour"){const dot=$(id+"-greet"),w=watcherTone(state.watcher,p.name);if(dot){dot.className="greet t-"+w.tone;dot.title=w.tip;}}
    else{const banked=$(id+"-banked"),view=bankedResetView(p.bankedResets);if(banked){if(banked.textContent!==view.text)banked.textContent=view.text;put(banked,"title",view.tip);attr(banked,"aria-label",view.tip);banked.hidden=!view.text;}}
  }));
  renderGreeterStatus(providers);
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
function toolbarMenu(open) {
  const menu=$("#toolbar-menu"),button=$("#toolbar-more");if(!menu||!button)return;
  const next=open??menu.hidden;
  menu.hidden=!next;button.setAttribute?.("aria-expanded",String(next));
}
const toggleToolbarMenu=()=>toolbarMenu();
function closeToolbarMenu(){toolbarMenu(false);}
document.addEventListener?.("pointerdown",event=>{const menu=$("#toolbar-menu");if(menu&&!menu.hidden&&!event.target?.closest?.(".toolbar-more"))closeToolbarMenu();});
document.addEventListener?.("pointerdown",event=>{const view=$("#dashboard-view");if(dashboardState.open&&view&&!view.contains?.(event.target))closeDashboard();});
window.addEventListener?.("blur",closeToolbarMenu);
document.addEventListener?.("keydown",event=>{if(event.key==="Escape"){const menu=$("#toolbar-menu");if(menu&&!menu.hidden){closeToolbarMenu();$("#toolbar-more")?.focus?.({preventScroll:true});}}});
/* ---------- chat rows (layer 1) ---------- */
function runwayContextPair(chat) {
  const tokens=safeNumber(chat.contextTokens),windowSize=safeNumber(chat.contextWindow),observed=stamp(chat.contextObservedAt),compacted=stamp(chat.compactionStartedAt);
  // A known window must be positive; an unknown window keeps the recorded tokens (shown as "tokens / —").
  const valid=Boolean((runwayCurrentEvidenceFresh()||chat.retainedRun===true)&&!chat.telemetryUnavailable&&!chat.metricsUnavailable&&chat.model&&chat.contextModel===chat.model&&observed&&observed<=Date.now()&&(windowSize==null||windowSize>0)&&tokens!=null&&(!compacted||observed>=compacted));
  return {tokens:valid?tokens:null,windowSize:valid?windowSize:null,observed:valid?observed:null};
}
function runwayContextDelta(chat,pair=runwayContextPair(chat)) {
  const observed=stamp(chat.contextDeltaObservedAt),delta=chat.contextDelta;
  return Boolean(pair.observed&&observed===pair.observed&&chat.contextModel===chat.model&&!chat.telemetryUnavailable&&!chat.metricsUnavailable&&typeof delta==='number'&&Number.isFinite(delta))?delta:null;
}
function contextHTML(chat) {
  const pair=runwayContextPair(chat),tokens=pair.tokens,windowSize=pair.windowSize;
  if(tokens==null)return '';
  const pct=windowSize>0?tokens*100/windowSize:null;
  const tone=pct==null?'':pct>75?'err':pct>50?'attn':'';
  const delta=runwayContextDelta(chat,pair),deltaText=delta==null?'':'; change since previous sample '+(delta>0?'+':'')+fmt(delta)+' tokens';
  const title='Context occupancy: '+String(tokens)+' / '+(windowSize==null?'window size unknown':String(windowSize))+' tokens'+deltaText;
  return '<span class="ctx-tokens" title="'+esc(title)+'"><span class="runway-context-fraction"><span class="runway-context-used'+(tone?' t-'+tone:'')+'">'+fmt(tokens)+'</span><span class="runway-context-separator"> / </span><span class="runway-context-total">'+(windowSize==null?'—':fmt(windowSize))+'</span></span></span>';
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
function runwayStateNote(chat) {
  if(isAsking(chat))return "Waiting for your input in this chat.";
  if(chat.status==="failed")return chat.failureReason||chat.stateDetail||"This run failed; open the chat to inspect it.";
  if(chat.status==="compacting")return "Compacting context; recorded work may resume afterward.";
  if(chat.status==="quiet")return "No transcript writes for 3+ minutes; a long tool call may still be running.";
  if(chat.stateDetail==="inactive")return "No completion recorded and no transcript writes for 30+ minutes.";
  if(chat.status==="unknown")return "";
  return chat.dataError||"";
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
  return runwayTelemetryHTML(chat,false);
}

/* ---------- stat strip (section N) ---------- */
function runwayStateElapsed(chat) {
  if(chat.retainedRun===true){const run=runInfo(chat);return run?runSpan(run.seconds):'—';}
  const execution=runwayExecutionView(chat);
  const status=runwayEffectiveStatus(chat);
  const waiting=runwayWaiting(chat);
  const candidates=execution&&!waiting?(status==='running'?[execution.since,chat.activeRunStartedAt,chat.runStartedAt,chat.firstSeenAt]:[execution.since]):[waiting?chat.pendingQuestionAt||(status==='needs-input'?chat.stateSince:null):chat.executionStatus===undefined?status==='compacting'?chat.compactionStartedAt||chat.stateSince:status==='running'?chat.stateSince||chat.activeRunStartedAt:chat.stateSince:['idle','failed'].includes(status)?chat.stateSince:null];
  const now=Date.now(),since=candidates.map(stamp).find(value=>value&&value<=now+5000);
  return since?runSpan(Math.max(0,now-since)/1000):'—';
}
const runwayMessageDirection = chat => ({"parent-to-agent":"from parent","agent-to-parent":"to parent","agent-to-agent":"between agents"})[chat.latestMessageDirection]||'';
const runwayMessageAge = chat => { const sent=stamp(chat.latestMessageAt),age=Date.now()-sent;return runwayMessageDirection(chat)&&sent&&age>=0&&age<10000?age:null; };
const runwayAgentHasNewMessage = chat => runwayMessageAge(chat)!=null;
// macOS paths have no drive: the Mac app reports POSIX paths, and Claude hooks a bare folder name.
const MAC_HOST=/^Mac/i.test(globalThis.navigator?.platform||'');
function runwayWorkspacePath(workspace) {
  {const raw=String(workspace||'').trim();if(/^\//.test(raw))return raw.replace(/\/+$/,'')||'/';if(MAC_HOST&&raw&&!/[\\/:]/.test(raw))return raw;}
  const normalized=String(workspace||'').trim().replace(/\//g,'\\');
  const path=/^[A-Za-z]:\\+$/.test(normalized)?normalized.slice(0,3):normalized.replace(/\\+$/,'');
  // Show the absolute Windows location from its volume root. If telemetry is not
  // drive-qualified, do not imply a root that the provider did not report.
  return /^[A-Za-z]:\\/.test(path)?path:'';
}
function runwayWorkspaceLeaf(workspace) {
  const path=runwayWorkspacePath(workspace);
  return path?path.split(path.startsWith('/')?'/':'\\').at(-1)||path:'';
}
const runwayScratch = (chat,path) => {const kind=chat.workspace?chat.workspaceKind:chat.root?.workspaceKind;return kind!=='project'&&(['codex-chat','per-chat'].includes(kind)||/(?:^|[\\/])\.t3[\\/]scratch(?:[\\/]|$)/i.test(path||''));};
function runwayActivityMarkup(label,fullText=label) {
  if(!label)return '';
  return '<span data-runway-slot="activity" class="runway-workspace runway-activity" title="'+esc(fullText)+'" aria-label="'+esc(label)+'">'+esc(label)+'</span>';
}
function runwayActivityHTML(chat) {
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
  const currentEvidence=runwayCurrentEvidenceFresh()||chat.retainedRun===true;
  const st=ownState(chat);
  const childWaiting=(chat.descendants||[]).some(runwayWaiting),childRunning=(chat.descendants||[]).some(runwayRunning);
  const ownCurrent=runwayWaiting(chat)||runwayRunning(chat)||(!runwayTerminated(chat)&&recentFail(chat));
  const effectiveStatus=runwayEffectiveStatus(chat);
  const tone=runwayWaiting(chat)?'attn':effectiveStatus==='compacting'&&runwayRunning(chat)?'info':runwayRunning(chat)?'live':ownCurrent?'err':childWaiting?'attn':childRunning?'live':'mute';
  const statusUnavailable=!currentEvidence||((chat.status==='unknown'||chat.statusSource==='unavailable')&&!childWaiting&&!childRunning&&!runwayRunning(chat));
  const status=chat.retainedRun===true&&chat.stateDetail==='stopped'?'Stopped':statusUnavailable?'':runwayWaiting(chat)?'Waiting':!ownCurrent&&childWaiting?'Waiting':!ownCurrent&&childRunning?'Agents running':runwayRunning(chat)?effectiveStatus==='compacting'?'Compacting':'Running':chat.executionStatus!==undefined?effectiveStatus==='needs-input'?'Waiting':effectiveStatus==='idle'?'Idle':effectiveStatus==='failed'?runwayStatus({...chat,status:'failed'},ownState({...chat,status:'failed'})):'':runwayStatus(chat,st);
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
  const messageLane='<span data-runway-slot="message-lane" class="runway-message-slot">'+messageContents+'</span>';
  const activityHTML=runwayActivityHTML(chat);
  const displayTitle=runwayTitle(chat,agent);
  const resumeBadge=Number.isSafeInteger(chat.resumeCount)&&chat.resumeCount>=2?'<span class="runway-resume-badge" title="Resumed '+chat.resumeCount+' times">\u21bb'+chat.resumeCount+'</span>':'';
  const titleHTML='<span data-runway-slot="title" class="'+(agent?'runway-agent-name':'r-title')+'" title="'+esc(agent?agentLabel(chat):displayTitle)+'">'+'<span data-runway-slot="label" class="runway-agent-label">'+esc(displayTitle)+'</span>'+resumeBadge+messageLane+'</span>';
  const context=contextHTML(chat);
  const waitingTarget=runwayWaiting(chat)?chat:(!ownCurrent&&childWaiting&&!hasTreeParent(chat)?(chat.descendants||[]).find(runwayWaiting):null);
  const waitingLabel=waitingTarget?(isCodexChat(waitingTarget)?'Open waiting chat to answer ':'Show question for ')+(waitingTarget.title||runwayTitle(waitingTarget,hasTreeParent(waitingTarget))):'';
  {
    const stateIcon=runwayStatusGlyph(status==='Waiting'?'needs-input':status==='Compacting'?'compacting':status==='Running'||status==='Agents running'?'running':statusCode);
    const visibleStatus=status==='Agents running'?'Agents active':status;
     const stateContent=status?((status==='Waiting'&&waitingTarget?stateIcon+'<button class="runway-state-word runway-waiting-link" data-action="runway-question" data-key="'+esc(waitingTarget.key)+'" aria-label="'+esc(waitingLabel)+'" title="'+(isCodexChat(waitingTarget)?'Open this waiting chat':'Show the pending question')+'">Waiting</button>':stateIcon+'<span class="runway-state-word"'+(visibleStatus!==status?' aria-label="'+esc(status)+'" title="'+esc(status)+'"':'')+'>'+esc(visibleStatus)+'</span>')):'';
    const treeAnchor='<span data-runway-slot="tree-anchor" class="runway-name-status runway-refined-tree-anchor t-'+tone+'" aria-hidden="true">'+(status?runwayStatusDot(status,tone):'')+'</span>';
    const elapsedTarget=ownCurrent||chat.retainedRun===true?chat:waitingTarget;
    const elapsed=currentEvidence&&elapsedTarget&&!statusUnavailable?runwayStateElapsed(elapsedTarget):'';
    const request=chat.lastRequest||chat.latestRequest||{},input=safeNumber(request.input),cached=safeNumber(request.cached);
    const cachePercent=currentEvidence&&!chat.telemetryUnavailable&&!chat.metricsUnavailable&&input>0&&cached!=null?Math.round(cached*100/input):null;
    const workspace=chat.workspace||chat.root?.workspace,workspacePath=runwayWorkspacePath(workspace),workspaceLeaf=runwayWorkspaceLeaf(workspace);
    const inherited=!chat.workspace&&Boolean(chat.root?.workspace);
    const workspaceTitle=(chat.workspaceKind==='codex-chat'?'Per-chat working folder; not a project checkout · ':inherited?'Inherited workspace · ':'Workspace · ')+workspacePath;
    const workspaceLabel=(inherited?'Inherited workspace: ':'Workspace: ')+workspacePath;
    // Drive first, one line: the leaf keeps the drive as its start and truncates at the end.
    const drive=/^[A-Za-z]:[\\/]/.test(workspacePath)?workspacePath.slice(0,2):'';
    const leaf=runwayScratch(chat,workspacePath)?'scratch':workspaceLeaf;
    const cleanPath=workspacePath&&leaf?'<span data-runway-slot="workspace" class="runway-workspace" title="'+esc(workspaceTitle)+'" aria-label="'+esc(workspaceLabel)+'"><span class="runway-path-leaf">'+esc(drive&&!runwayScratch(chat,workspacePath)?drive+' ':'')+esc(leaf)+'</span></span>':'';
    const contextValue=context?'<span data-runway-slot="context" class="runway-refined-context">'+context+'</span>':'';
    const cacheValue=cachePercent==null?'':'<span data-runway-slot="cache" class="runway-refined-cache">Cache '+cachePercent+'%</span>';
    // The status column already says Compacting / Waiting / Agents running; don't repeat it as activity.
    const visibleActivityHTML=status&&/^(?:Compacting|Waiting for input|Subagents running)$/.test(activityHTML.replace(/<[^>]*>/g,''))?'':activityHTML;
    // Never a silent blank: an active run without a usable sample shows a quiet dash.
    const awaitingSample=!contextValue&&!cacheValue&&ownCurrent&&currentEvidence&&!chat.telemetryUnavailable;
    const tokenPlaceholder=awaitingSample?'<span class="runway-refined-context t-mute" title="No usable context sample yet for this run">—</span>':'';
     return '<div class="runway-refined-row" data-runway-slot="row">'+treeAnchor+'<span class="runway-refined-title">'+titleHTML+modelHTML+'</span><span data-runway-slot="timing" class="runway-refined-timing"><span data-runway-slot="elapsed" class="runway-refined-elapsed">'+esc(elapsed||'')+'</span></span><span data-runway-slot="status" class="runway-refined-status t-'+tone+'" data-own-waiting="'+runwayWaiting(chat)+'" data-child-waiting="'+childWaiting+'">'+stateContent+'</span><span data-runway-slot="activity-column" class="runway-refined-activity">'+visibleActivityHTML+'</span><span data-runway-slot="path-column" class="runway-refined-path">'+(agent?'':cleanPath)+'</span><span data-runway-slot="tokens" class="runway-refined-tokens">'+contextValue+cacheValue+tokenPlaceholder+'</span></div>';
  }
}

function runwayQuestionPopover(chat) {
  if(!runwayCurrentEvidenceFresh()||chat?.telemetryUnavailable)return '';
  if(!chat||!runwayWaiting(chat))return '';
  if(!isCodexChat(chat)&&!chat.pendingQuestion)return '<div class="runway-question-panel"><button class="runway-question-close" data-action="close-popover" aria-label="Close" title="Close">×</button><div class="runway-question-full">Waiting for your approval in Claude.</div></div>';
  if(!chat.pendingQuestion)return '';
  if(!isCodexChat(chat)){
    const command=resumeCommand(chat);
    return '<div class="runway-question-panel"><button class="runway-question-close" data-action="close-popover" aria-label="Close question" title="Close question">×</button><div class="runway-question-full">'+richText(chat.pendingQuestion)+'</div>'+plainOptionsHTML(chat.pendingOptions)+(command?'<button class="q-open" data-action="copy" data-copy="'+esc(command)+'" title="'+esc(command)+'">'+ICON.copy+'<span>Copy resume command</span>'+ICON.check+'</button>':'')+'</div>';
  }
  const choices=(chat.pendingOptions||[]).length?optionsHTML(chat.pendingOptions,chat.id):'<button class="q-open" data-action="open" data-thread="'+esc(chat.id)+'" title="Open this chat in Codex">'+ICON.open+'<span>Open in Codex to answer</span>'+ICON.check+'</button>';
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
const runwayActionable = a => runwayCurrentEvidenceFresh()&&!runwayTerminated(a)&&(runwayWaiting(a)||recentFail(a)||runwayEffectiveStatus(a)==='failed');
const isVisibleAgent = a => a.retainedRun===true||runwayVisibleActive(a)||runwayActionable(a);
function trayState(chat) {
  return chat.descendants.some(isVisibleAgent)?"active":"resting";
}
function agentRowEntry(agent, query, visibleParent=null, visibleDepth=null) {
  return {key:agent.key,cls:'ag runway-agent'+(query.active&&matchesQuery(agent,query)?' match':''),attrs:{'data-sub':agent.key,'data-provider':agent.provider,'data-visible-parent':visibleParent,style:'--agent-depth:'+Math.max(0,(visibleDepth??agent.depth??1)-1)},html:'<div class="runway-agent-glance">'+runwayTelemetryHTML(agent,true)+'</div>'};
}
function trayEntries(chat, query) {
  const allDescendants=chat.descendants||[],byParent=new Map([chat,...allDescendants].map(a=>[a.key,a]));
  const hiddenStopped=a=>{const parent=byParent.get(treeParentKey(a));return Boolean(parent&&runwayRunning(parent)&&(runwayTerminated(a)||a.stateDetail==='stopped'||a.executionTerminalAt));};
  const needed=new Set(allDescendants.filter(a=>!hiddenStopped(a)&&isVisibleAgent(a)).map(a=>a.key));
  const byKey=new Map(allDescendants.map(a=>[a.key,a]));
  for(const key of [...needed]){let row=byKey.get(key),seen=new Set();while(row){const parent=treeParentKey(row);if(!parent||seen.has(parent)||!byKey.has(parent))break;seen.add(parent);needed.add(parent);row=byKey.get(parent);}}
  const agents=allDescendants.filter(a=>needed.has(a.key));
  agents.forEach(a=>{if(state.agentLive.get(a.key)!==agentStats(a).live)noteLiveness(a,agentStats(a).live);});
  const all=new Map(allDescendants.map(a=>[a.key,a])),visible=new Map(agents.map(a=>[a.key,a]));
  const children=new Map();
  for(const agent of agents){let parent=treeParentKey(agent),seen=new Set([agent.key]);while(parent!==chat.key&&!visible.has(parent)&&all.has(parent)&&!seen.has(parent)){seen.add(parent);parent=treeParentKey(all.get(parent));}if(!visible.has(parent))parent=chat.key;if(!children.has(parent))children.set(parent,[]);children.get(parent).push(agent);}
  const entries=[];
  function visit(parent,depth=1){for(const agent of children.get(parent.key)||[]){entries.push(agentRowEntry(agent,query,parent.key,depth));visit(agent,depth+1);}}
  visit(chat);return entries;
}
function traySummaryHTML(chat) { return ''; }

/* ---------- layer 2 ---------- */
/* ---------- chat list ---------- */
function chatBlockHTML(chat, query) {
  const tray=chat.descendants.some(isVisibleAgent)?'<div class="tray runway-expanded"><div class="tray-sum"></div><div class="tray-full"><div class="tray-clip">'+trayEntries(chat,query).map(e=>'<div class="'+e.cls+'" data-key="'+esc(e.key)+'"'+attrString(e.attrs)+'>'+e.html+'</div>').join('')+'</div></div></div>':'';
  return '<div class="chat" data-key="'+esc(chat.key)+'" data-provider="'+esc(chat.provider)+'"><div class="row">'+rowHTML(chat)+'</div>'+tray+'</div>';
}
function emptyHTML(forest, providers) {
  if(parseQuery(state.query).active)return '<div class="empty">No matches</div>';
  const down=providers.filter(p=>p.connected===false);
  return down.length?'<div class="empty">No data</div>':'<div class="empty tree-empty">No active chats</div>';
}
function updateChatEl(el, chat, query) {
  const [row,,tray]=el.children;
  if(el.dataset.provider!==chat.provider)el.dataset.provider=chat.provider;
  const html=rowHTML(chat);
  if(row._html!==html) {
    const next=rollup(chat).key,prevState=el.dataset.state;
    runwayPatchContent(row,html);row._html=html;syncBreath(row);
    if(prevState&&prevState!==next)runwayMotionOnce(el,'state-changed','runway-status-change');
    el.dataset.state=next;
  }
  const previousQuestionToken=el.dataset.questionToken;
  const questionToken=runwayWaiting(chat)?questionDismissToken(chat):'';
  if(state.runwaySnapshotPainted&&previousQuestionToken!==undefined&&questionToken&&previousQuestionToken!==questionToken)
    runwayMotionOnce(el,'question-arrived','runway-question-arrival','.runway-refined-title>.r-title,.runway-refined-title>.runway-agent-name,.runway-refined-status .runway-state-glyph');
  el.dataset.questionToken=questionToken;
  row.title=chat.title||"";
  const has=chat.descendants.some(isVisibleAgent);
  tray.hidden=!has;
  if(!has)return;
  const [sum,full]=tray.children;
  if(tray.className!=='tray runway-expanded')tray.className='tray runway-expanded';
  const summary=traySummaryHTML(chat);
  if(sum.firstElementChild._html!==summary){runwayPatchContent(sum.firstElementChild,summary);sum.firstElementChild._html=summary;}
  patchKeyed(full.firstElementChild,trayEntries(chat,query),{enter:el=>{if(state.runwaySnapshotPainted)runwayArrivalEnter(el);},exit:runwayExit});
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
  window.TokenTrackerDashboard?.setSource?.(current);
}
function setSource(name,{persist=true,manual=true}={}) {
  if(!SOURCES.includes(name)||!policyForSources(state.snapshot)||!sourceNames(state.snapshot).includes(name))return false;
  if(manual){state.sourceManualAt=Date.now();if(persist)try{localStorage.setItem(SOURCE_KEY,name);localStorage.setItem(SOURCE_MANUAL_AT_KEY,String(state.sourceManualAt));}catch{}}
  state.source=name;state.sourceAttention.delete(name);updateSourceSwitch();
  if(!state.snapshot)renderQuotaState();
  return true;
}
function selectSourceForNewWork(items) {
  // Fresh work determines the auto-open source; only an explicit active inspection locks the current view.
  if(!state.snapshot||notch.manual||notch.keyboardInspecting||state.searchOpen||state.popover||(widgetLock.locked&&widgetLock.expanded))return false;
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
  let roots=visibleRoots(forest);const query=parseQuery(state.query);
  state.forest=forest;
  if(state.popover?.dataset?.action==='runway-question'&&!runwayCurrentEvidenceFresh())closePopover({restoreFocus:true});
  if(state.popover?.dataset?.action==='runway-question'){
    const source=forest.rows.find(item=>item.key===state.popover.dataset.key);
    if(!source||!runwayWaiting(source)||!source.pendingQuestion||state.popover.__questionId!==(source.pendingQuestionId??source.questionId??null)||state.popover.__questionAt!==(source.pendingQuestionAt||null)||state.popover.__questionText!==source.pendingQuestion||state.popover.__questionOptions!==JSON.stringify(source.pendingOptions||[]))closePopover({restoreFocus:true});
  }
  {
    const counts=runwayCounts(forest);

    {
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
    const available=activeCount>0||roots.length>0||state.hiddenChats.size>0||keepOpenState.value||state.searchOpen||Boolean(state.popover)||compactHasFocus();
    const header=$('#chats-head'),searchToggle=$('#search-toggle');
    if(header)header.hidden=!available;if(searchToggle)searchToggle.hidden=!available;
    if(!available){state.query='';state.searchOpen=false;const input=$('#chat-search');if(input)input.value='';header?.classList?.remove('searching');}
    }
  }
  const hidden=forest.roots.length-roots.length;
  const count=$("#chat-count");
  if(count){count.textContent=String(roots.length);count.title=hidden>0&&!query.active?hidden+" older hidden":"";}
  const result=$("#result-count");if(result)result.textContent=roots.length+" / "+forest.roots.length;
  const hiddenToggle=$('#hidden-chats-toggle');if(hiddenToggle){const hiddenCount=state.hiddenChats.size;hiddenToggle.hidden=!hiddenCount;hiddenToggle.textContent=(state.showHidden?'Hide hidden':'Show hidden')+' ('+hiddenCount+')';hiddenToggle.setAttribute('aria-pressed',String(state.showHidden));}
  const warm=providers.find(p=>p.cacheWarmth)?.cacheWarmth,rule=$("#warm-rule");
  if(rule){rule.textContent=warm?"warm ≤"+Math.round(warm.warmSeconds/60)+"m":"";rule.title=warm?"Reply within "+Math.round(warm.warmSeconds/60)+"m to keep the cache · cold after "+runSpan(warm.coldSeconds).replace(" 00m","h").replace(/^(\d+)h (\d+)m$/,"$1h $2m")+(warm.learned?" · learned from "+warm.samples+" resumes":""):"";}
  const seg=$("#window-seg");if(seg?.style)seg.style.setProperty("--i",String(WINDOWS.indexOf(state.windowDays)));
  document.querySelectorAll?.("[data-days]").forEach(b=>b.setAttribute?.("aria-pressed",String(Number(b.dataset.days)===state.windowDays)));
  const list=$("#chat-list");if(!list)return;
  // The compact tree always paints its empty state (the list is hidden by CSS while the widget is collapsed), never a blank panel.
  const searchCanShowEmpty=true;
  if(!realDom(list)) {
    const html=(roots.length?roots.map(c=>chatBlockHTML(c,query)).join(""):emptyHTML(forest,providers));
    if(list.innerHTML!==html)list.innerHTML=html;
    return;
  }
  if(!roots.length){
    const html=emptyHTML(forest,providers);
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
  const scroller=notch.mode!=='inspect'?list.closest?.('.layout'):list;
  const scrollTopBefore=scroller?.scrollTop||0,scrollRect=scroller?.getBoundingClientRect?.();
  const anchor=[...existing].find(([key,el])=>byKey.has(key)&&(()=>{const rect=el.getBoundingClientRect?.();return rect&&(!scrollRect||rect.bottom>scrollRect.top);})())?.[1];
  const anchorTop=anchor?.getBoundingClientRect?.().top;
  for(const [k,el] of existing)if(!byKey.has(k))runwayExit(el);
  let prev=null;
  for(const k of order) {
    let el=existing.get(k);
    const fresh=!el;
    if(!el){el=document.createElement("div");el.className="chat";el.dataset.key=k;
      el.innerHTML='<div class="row"></div><div class="l2"><div class="l2-in"></div></div><div class="tray"><div class="tray-sum"><div class="tray-clip"></div></div><div class="tray-full"><div class="tray-clip"></div></div></div>';}
    updateChatEl(el,byKey.get(k),query);
    if(fresh&&state.runwaySnapshotPainted&&!state.sourceSwitching)runwayArrivalEnter(el);
    const next=prev?prev.nextSibling:list.firstChild;
    if(next!==el)list.insertBefore(el,next);
    prev=el;
  }
  state.lastOrder=order;
  if(anchor&&anchor.isConnected&&Number.isFinite(anchorTop)&&scroller){const moved=anchor.getBoundingClientRect().top-anchorTop;if(Math.abs(moved)>1)scroller.scrollTop=scrollTopBefore+moved;}
}

/* ---------- footer, title bar, popover ---------- */
function renderTotals(providers) {
  const node=$('#totals');if(node){node.innerHTML='';node.hidden=true;}
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
function syncCompactClock(){const localTime=compactClockFormatter.format(new Date());for(const node of [$('#compact-clock'),$('#toolbar-clock')])if(node&&node.textContent!==localTime)node.textContent=localTime;}
window.TokenTrackerSetClockFormat=value=>{if(!['12','24'].includes(String(value)))return false;try{localStorage.setItem('branchglanceClockFormat',String(value));}catch{}compactClockFormatter=new Intl.DateTimeFormat('en-US',{hour:'numeric',minute:'2-digit',hourCycle:String(value)==='24'?'h23':'h12'});syncCompactClock();return true;};
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
function renderUpdatedMarker() {
  const marker=$("#updated-marker");if(!marker)return;
  const stale=dataIsStale(),text=stale?"Updated "+ageShort(state.lastOkAt)+" ago":"";
  if(marker.textContent!==text)marker.textContent=text;
  marker.hidden=!stale;
  if(marker.title!==(stale?"Elapsed times are frozen at the last update":""))marker.title=stale?"Elapsed times are frozen at the last update":"";
  document.body?.classList?.toggle("data-stale",stale);
}
function renderTitle(providers) {
  const name=$("#brand-name");if(name)name.textContent="Branchglance";
  syncCompactClock();
  renderUpdatedMarker();
  const age=$("#sync-age");if(!age)return;
  if(state.refreshing||state.loading||state.initialBootstrapPending){if(age.innerHTML!==RUNWAY_REFRESH_ICON)age.innerHTML=RUNWAY_REFRESH_ICON;age.title=refreshControlTitle();age.className="sync-age is-loading";age.setAttribute?.("aria-busy","true");return;}
  if(age.innerHTML!==RUNWAY_REFRESH_ICON)age.innerHTML=RUNWAY_REFRESH_ICON;age.title=refreshControlTitle();age.className="sync-age";age.setAttribute?.("aria-busy","false");
}
const PHASES={account_changed:"Account changed, re-arm to keep greeting",waiting_for_exhaustion:"waiting for limit",armed_waiting_for_reset:"armed for reset",reset_observed_sending:"sending",needs_login:"sign-in expired",blocked_weekly:"held · weekly used",greeting_sent:"greeting sent",keepalive_scheduled:"scheduled",greeting_verifying:"verifying"};
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
  if(!watcher||!w)return policyWarningHTML()+'<button class="ghost" data-action="reset-settings">Reset action settings</button>';
  const next=safeNumber(w.nextGreetingAt),last=stamp(w.lastTriggerAt),verified=w.verificationEvidence==="usage_and_deadline"&&last&&stamp(w.lastVerifiedAt)>=last;
  const phase=!watcher.enabled?"off":!watcher.running?"stopped":esc(PHASES[w.phase]||w.phase);
  const err=watcher.persistenceError||w.lastPollError||w.lastError;
  const rows=[];if(phase)rows.push(['phase',phase]);if(next)rows.push(['next',dayWord(next*1000)+' '+clock(next*1000)]);if(last)rows.push(['last',clock(last)+(verified?' <span class="t-live">✓</span>':'')]);const fails=safeNumber(w.failedAttempts);if(fails!=null)rows.push(['fails',String(fails)]);
  return (rows.length?'<dl class="pop-facts">'+rows.map(([label,value])=>'<dt>'+label+'</dt><dd>'+value+'</dd>').join('')+(err?'<dt>error</dt><dd class="t-err" title="'+esc(err)+'">'+esc(err)+'</dd>':'')+'</dl>':'')+policyWarningHTML()+'<button class="ghost" data-action="reset-settings">Reset action settings</button>';
}
function openPopover(html, anchor) {
  const pop=$("#popover");if(!pop)return;
  cleanupPopoverPlacement(pop);
  pop.innerHTML=html;pop.hidden=false;
  state.popover=anchor;
  const place=()=>{const r=anchor.getBoundingClientRect?.(),w=pop.offsetWidth||240,h=pop.offsetHeight||0;if(r){pop.style.left=Math.max(8,Math.min(window.innerWidth-w-8,r.left))+"px";pop.style.top=Math.max(8,Math.min(window.innerHeight-h-8,r.bottom+6))+"px";}};
  place();
  if(anchor?.dataset?.action==='runway-question'){
    const scroller=notch.mode==='inspect'?$('#chat-list'):(anchor.closest?.('.layout')||$('.layout'));
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
function initialActiveSource(snapshot){
  if(!runwayCurrentEvidenceFresh())return null;
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
  const firstRunwayPaint=!state.runwaySnapshotPainted;
  const graph=snapshot?.graph?.edges;
  const hasGraph=Boolean(graph&&typeof graph==='object'&&!Array.isArray(graph));
  const graphlessHold=firstRunwayPaint&&state.initialBootstrapPending&&!hasGraph;
  if(graphlessHold){
    state.graphlessBootstrapSnapshot=snapshot;
    if(state.graphlessBootstrapTimer==null)state.graphlessBootstrapTimer=setTimeout(()=>{
      state.graphlessBootstrapTimer=null;state.graphlessBootstrapTimedOut=true;
      if(state.graphlessBootstrapSnapshot&&!state.runwaySnapshotPainted)render(state.graphlessBootstrapSnapshot);
      if(state.graphlessBootstrapFallback)scheduleBootstrapRetry();
    },2000);
  }
  const graphlessFallback=(graphlessHold&&(++state.graphlessBootstrapAttempts>=2||state.graphlessBootstrapTimedOut))||(!hasGraph&&state.graphlessBootstrapFallback);
  if(graphlessHold&&!graphlessFallback)return false;
  if(hasGraph){if(state.graphlessBootstrapTimer!=null)clearTimeout(state.graphlessBootstrapTimer);state.graphlessBootstrapTimer=null;state.graphlessBootstrapSnapshot=null;state.graphlessBootstrapAttempts=0;state.graphlessBootstrapTimedOut=false;state.graphlessBootstrapFallback=false;}
  else if(graphlessFallback){state.graphlessBootstrapFallback=true;if(state.graphlessBootstrapTimer!=null)clearTimeout(state.graphlessBootstrapTimer);state.graphlessBootstrapTimer=null;state.graphlessBootstrapSnapshot=null;}
  syncProviderPolicyGeneration(snapshot);
  restrictSnapshotProviders(snapshot);
  acceptSnapshotCache(snapshot);
  reconcileSnapshotQuotas(snapshot);
  state.snapshot=snapshot;
  state.initialBootstrapPending=false;
  if(firstRunwayPaint){const activeSource=initialActiveSource(snapshot);if(activeSource)state.source=activeSource;}
  if(!state.lastOkAt)state.lastOkAt=Date.now();
  state.runwaySnapshotPainted=true;
  const providers=providersFor(snapshot),limits=limitProvidersFor(snapshot);
  const chatProviders=graphlessFallback?providers.map(provider=>({...provider,chats:(provider.chats||[]).map(chat=>({...chat,
    parentKey:null,parentId:null,status:'unknown',statusSource:'unavailable',telemetryUnavailable:true,
    executionStatus:'unknown',executionSource:'unavailable',executionActivity:null,activity:null,activityUnavailable:true,
    statusObservedAt:null,executionObservedAt:null
  }))})):providers;
  $(".widget")?.classList?.remove("loading");
  const priorSourceSwitching=state.sourceSwitching;if(firstRunwayPaint)state.sourceSwitching=true;
  try{renderTitle(providers);renderResets(limits);renderChats(chatProviders);renderTotals(providers);renderAnalytics(snapshot);}
  finally{state.sourceSwitching=priorSourceSwitching;}
  window.TokenTrackerDashboard?.setSnapshot?.(snapshot);
  syncLastKnownFollowup();
  return true;
}
function renderLoading() {
  syncCompactClock();
  const list=$("#chat-list");
  if(list)list.innerHTML='';
  const header=$('#chats-head'),search=$('#search-toggle');if(header)header.hidden=true;if(search)search.hidden=true;
  updateSourceSwitch();
  renderQuotaState();
  const greeter=$("#runway-greeter-status");if(greeter)greeter.textContent="";
}
function tick() {
  syncCompactClock();
  if(!state.snapshot){renderTitle([]);renderQuotaState();return;}
  const providers=providersFor(state.snapshot),limits=limitProvidersFor(state.snapshot);
  renderTitle(providers);renderResets(limits);renderChats(providers);renderAnalytics(state.snapshot);
  window.TokenTrackerDashboard?.setSnapshot?.(state.snapshot);
  syncLastKnownFollowup();
}
function applyQuotaSamples(samples,sequence,providers,{requireDated=false}={}) {
  if(!samples||!Array.isArray(providers))return false;
  let changed=false;const now=Date.now();
  for(const provider of providers) {
    const sample=samples[provider.name]?.lastStatus;if(!sample)continue;
    if(provider.name==='Claude'&&(!provider.identityVerified||!provider.identityGeneration||sample.identity_generation!==provider.identityGeneration))continue;
    const hasCheckedAt=sample.checkedAt!==undefined&&sample.checkedAt!==null&&sample.checkedAt!=='';
    const checkedAt=hasCheckedAt?isoObservationTime(sample.checkedAt,now):null;
    if(hasCheckedAt&&checkedAt===null||requireDated&&checkedAt===null)continue;
    initializeQuotaWindowTimes(provider);
    const retained=watcherAcceptedQuota.get(provider.name)||{};
    for(const [key,percentField,resetField] of [['fiveHour','five_hour_percent','reset_at'],['weekly','weekly_percent','weekly_reset_at']]){
      if(!Object.prototype.hasOwnProperty.call(sample,percentField))continue;
      const percent=sample[percentField];if(percent!==null&&safeNumber(percent)===null)continue;
      const existing=provider[key];
      if(percent===null&&existing?.lastKnown===true&&safeNumber(existing.resetAt)!==null&&Number(existing.resetAt)*1000>now)continue;
      const hasWindowTime=Object.prototype.hasOwnProperty.call(provider._quotaWindowObservedAt,key),observed=provider._quotaWindowObservedAt[key],statsAt=isoObservationTime(hasWindowTime?observed:provider.limitsUpdatedAt,now),prior=retained[key];
      const acceptedSequence=state.watcherAcceptedSequence.get(provider.name+':'+key)||0;
      if(checkedAt!==null){if(statsAt!==null&&checkedAt<=statsAt||prior?.at!=null&&checkedAt<=prior.at)continue;}
      else if(requireDated||statsAt!==null||prior?.at!=null||sequence<=acceptedSequence)continue;
      if(percent===null){provider[key]=null;}
      else {
        const previous=provider[key]&&typeof provider[key]==='object'?provider[key]:{},next={...previous,percent:safeNumber(percent)};
        if(previous.lastKnown===true){delete next.resetAt;delete next.reset;delete next.lastKnownResetAt;}
        delete next.lastKnown;
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
function initializeQuotaWindowTimes(provider){
  if(!provider._quotaWindowObservedAt||typeof provider._quotaWindowObservedAt!=='object')provider._quotaWindowObservedAt={};
  for(const key of ['fiveHour','weekly'])if(!Object.prototype.hasOwnProperty.call(provider._quotaWindowObservedAt,key))provider._quotaWindowObservedAt[key]=Object.prototype.hasOwnProperty.call(provider,key)?provider.limitsUpdatedAt??null:null;
}
function reconcileSnapshotQuotas(snapshot,now=Date.now()){
  if(!snapshot||!Array.isArray(snapshot.providers))return;
  const currentProviders=state.snapshot?.providers||[];
  for(const incoming of snapshot.providers){
    const current=currentProviders.find(provider=>provider.name===incoming.name),retained=watcherAcceptedQuota.get(incoming.name)||{};
    if(incoming.name==='Claude'&&(!incoming.identityVerified||!incoming.identityGeneration||current?.identityGeneration!==incoming.identityGeneration)){
      watcherAcceptedQuota.delete('Claude');
      state.watcherAcceptedSequence.delete('Claude:fiveHour');state.watcherAcceptedSequence.delete('Claude:weekly');
      continue;
    }
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
      for(const candidate of candidates.slice(1)){
        const fallback=selected.value?.lastKnown===true&&safeNumber(selected.value?.resetAt)!=null&&Number(selected.value.resetAt)*1000>now;
        if(fallback&&safeNumber(candidate.value?.percent)===null)continue;
        if(candidate.at!==null&&(selected.at===null||candidate.at>selected.at||candidate.at===selected.at&&selected.value?.lastKnown===true&&candidate.value?.lastKnown!==true&&safeNumber(candidate.value?.percent)!==null))selected=candidate;
      }
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
  if(document.hidden||quotaLoader.unsupported||Date.now()<quotaLoader.retryAfter)return Promise.resolve(false);
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
  if(!state.snapshot)return Promise.resolve(false);
  if(watcherLoader.promise){watcherLoader.pending=true;return watcherLoader.promise;}
  const drain=async()=>{let result=false;do{watcherLoader.pending=false;result=await fetchWatcherOnce();}while(watcherLoader.pending);return result;};
  const operation=drain();let shared;
  shared=operation.finally(()=>{if(watcherLoader.promise===shared)watcherLoader.promise=null;});
  watcherLoader.promise=shared;return shared;
}

/* ---------- network ---------- */
const markOk = () => { state.lastOkAt=Date.now();state.failSince=null; };
const markFail = () => { if(!state.failSince)state.failSince=Date.now(); };
function syncLastKnownFollowup() {
  const visible=state.snapshot&&limitProvidersFor(state.snapshot).some(provider=>["fiveHour","weekly"].some(key=>resetView(provider,key).lastKnown));
  if(!visible){if(lastKnownFollowup.timer!=null)clearTimeout(lastKnownFollowup.timer);lastKnownFollowup.timer=null;lastKnownFollowup.attempts=0;return;}
  if(document.hidden||lastKnownFollowup.timer!=null||lastKnownFollowup.attempts>=4)return;
  const delay=2000*2**lastKnownFollowup.attempts++;
  lastKnownFollowup.timer=setTimeout(()=>{lastKnownFollowup.timer=null;if(!document.hidden)void refresh();},delay);
}
function clearBootstrapRetry(reset=false) {
  if(state.bootstrapRetryTimer!=null)clearTimeout(state.bootstrapRetryTimer);
  state.bootstrapRetryTimer=null;
  if(reset)state.bootstrapRetryMs=2000;
}
function scheduleBootstrapRetry() {
  if(state.snapshot&&!state.graphlessBootstrapFallback||document.hidden||state.bootstrapRetryTimer!=null)return;
  const delay=state.bootstrapRetryMs;
  state.bootstrapRetryMs=Math.min(15000,state.bootstrapRetryMs*2);
  state.bootstrapRetryTimer=setTimeout(()=>{
    state.bootstrapRetryTimer=null;
    if((!state.snapshot||state.graphlessBootstrapFallback)&&!document.hidden)refresh();
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
  syncMonitors();
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
  if(state.snapshot)await loadWatcher();
  if(state.snapshot)tick();
  if(state.refreshPending){const queuedForce=state.refreshPendingForce;state.refreshPending=false;state.refreshPendingForce=false;refresh(queuedForce);return;}
  if(!state.snapshot||state.graphlessBootstrapFallback)scheduleBootstrapRetry();

}
function statsUrl(force) {
  // Runway needs every active root, including an older chat waiting for a reply.
  // Any server-side trimming can erase passive counts or their parent anchors.
  return "/api/stats?background=1"+(force?"&refresh=1":"");
}
function handleVisibilityChange() {
  if(document.hidden)return;
  void loadQuota();

  return refresh();
}
const LIVE_FIELDS=["resumeCount","firstSeenAt","runPid","runCreated","runVerified","runBindingUnresolved","retainedRun","stoppedAt","contextTokens","contextWindow","contextPercent","recentTokens","usageUpdatedAt","lastRequest","toolCalls","filesEdited","fileChanges","runStartedAt","runEndedAt","pendingQuestionId","questionId","attentionSince","pendingQuestionAt","model",...["latestRequest","latestRequestAt","latestUpdate","latestUpdateAt","trackerUpdate","trackerUpdateAt","activeRunStartedAt","stateSince","compactionStartedAt","executionStatus","executionSource","executionObservedAt","executionSince","executionCompactionStartedAt","executionTerminalAt","executionActivity","statusObservedAt","telemetryUnavailable","metricsUnavailable","previousStatus","lastValidAt","statusSource","latestMessageAt","latestMessageDirection","latestMessageSourceId","latestMessageTargetId","contextObservedAt","contextModel","contextDelta","contextDeltaObservedAt","modelChangeAt","stateDetail","failureReason","activity","activityUnavailable","resumable","sessionId"],...["parentId","parentKey","title","agentName","agentRole","workspace","workspaceKind","source","pendingQuestionIds","questionResolutionAt"]];
// A live row is qualified as "<Provider>:<id>"; ids may themselves contain ":" (Claude subagents).
function liveIdentity(item) {
  if(!item||typeof item.id!=='string'||!item.id||typeof item.key!=='string')return null;
  for(const name of SOURCES)if(item.key===keyOf({...item,provider:name}))return !item.provider||item.provider===name?name:null;
  return null;
}
// Overlay seconds-fresh activity/state from /api/live onto the last full snapshot.
function applyLive(snapshot, live) {
  if(!snapshot||!live||stamp(live.generatedAt)<stamp(snapshot.generatedAt))return {changed:false,outside:[]};
  restrictSnapshotProviders(snapshot);
  let graphChanged=false;
  if(live.graph&&typeof live.graph.edges==="object"&&!Array.isArray(live.graph.edges)){
    const incoming=Number(live.graph.revision),current=Number(snapshot.graph?.revision);
    if(!snapshot.graph||Number.isFinite(incoming)&&(!Number.isFinite(current)||incoming>current)){snapshot.graph=live.graph;graphChanged=true;}
  }
  const validKnown=item=>liveIdentity(item)!==null;
  const byKey=new Map((live.chats||[]).filter(validKnown).map(item=>[item.key,item]));
  let changed=graphChanged,oldestShown=Infinity;
  const initiallyLoaded=new Set((snapshot.providers||[]).flatMap(provider=>(provider.chats||[]).map(chat=>keyOf({...chat,provider:provider.name}))));
  // Unseen rows are inserted per provider; providers hidden by policy are already absent from the snapshot.
  const liveTargets=new Map();
  for(const name of SOURCES){const target=(snapshot.providers||[]).find(provider=>provider.name===name);if(target&&Array.isArray(target.chats))liveTargets.set(name,{target,known:new Set(target.chats.map(chat=>keyOf({...chat,provider:name})))});}
  if(liveTargets.size){
    for(const item of live.chats||[]){
      const owner=liveIdentity(item),entry=owner&&liveTargets.get(owner);
      if(!entry)continue;
      const {target,known}=entry;
      if(known.has(item.key))continue;
       // A detail-pending row carries identity and lineage only: it inserts without a status, so it cannot read as a transition.
       const record={id:item.id,key:item.key};
       for(const field of ['resumeCount','firstSeenAt','runPid','runCreated','runVerified','runBindingUnresolved','retainedRun','stoppedAt'])if(item[field]!==undefined)record[field]=item[field];
       const fields=item.detailPending?['parentId','parentKey','title','agentName','agentRole','workspace','workspaceKind','source']:['parentId','parentKey','title','agentName','agentRole','workspace','workspaceKind','source','status','statusSource','telemetryUnavailable','metricsUnavailable','previousStatus','lastValidAt','stateDetail','failureReason','activity','activityUnavailable','executionStatus','executionSource','executionObservedAt','executionSince','executionCompactionStartedAt','executionTerminalAt','executionActivity','activeRunStartedAt','stateSince','runStartedAt','runEndedAt','updatedAt','activityAt','pendingQuestion','pendingQuestionAt','pendingQuestionId','pendingQuestionIds','questionId','questionResolutionAt','pendingOptions','model','contextTokens','contextWindow','contextPercent','contextObservedAt','contextModel','contextDelta','contextDeltaObservedAt','latestRequest','latestRequestAt','latestUpdate','latestUpdateAt','trackerUpdate','trackerUpdateAt','latestMessageAt','latestMessageDirection','latestMessageSourceId','latestMessageTargetId','toolCalls','filesEdited','fileChanges','recentTokens','usageUpdatedAt','lastRequest','attentionSince','compactionStartedAt','statusObservedAt','modelChangeAt','resumable','sessionId'];for(const field of fields)if(item[field]!==undefined)record[field]=item[field];
      target.chats.push(record);known.add(item.key);changed=true;
    }
  }
  for(const provider of snapshot.providers||[])for(const chat of provider.chats||[]){
    const key=keyOf({...chat,provider:provider.name});
    const next=byKey.get(key);
    // A detail-pending row is only a placeholder: rows on screen keep their detail, and no transition is read from it.
    if(next&&!next.detailPending){
      if(next.telemetryUnavailable!==undefined&&chat.telemetryUnavailable!==Boolean(next.telemetryUnavailable)){chat.telemetryUnavailable=Boolean(next.telemetryUnavailable);changed=true;}
      if(next.status){if((chat.statusSource!=="unavailable"||next.telemetryUnavailable===false||next.statusSource!=="unavailable")&&normalizeStatus(next.status)!==normalizeStatus(chat.status)){chat.status=next.status;changed=true;}chat.sourceStatus=next.status;}
      if((next.pendingQuestion||null)!==(chat.pendingQuestion||null)||(next.pendingQuestionAt||null)!==(chat.pendingQuestionAt||null)){
        chat.pendingQuestion=next.pendingQuestion||null;chat.pendingQuestionAt=next.pendingQuestionAt||null;changed=true;
      }
      if(JSON.stringify(next.pendingOptions||[])!==JSON.stringify(chat.pendingOptions||[])){chat.pendingOptions=next.pendingOptions||[];changed=true;}
      if(stamp(next.activityAt)>stamp(chat.updatedAt)){chat.updatedAt=next.activityAt;changed=true;}
      for(const field of ['resumeCount','firstSeenAt'])if(next[field]===undefined&&chat[field]!==undefined){delete chat[field];changed=true;}
      for(const field of LIVE_FIELDS){
        if(next[field]!==undefined&&JSON.stringify(next[field])!==JSON.stringify(chat[field])){chat[field]=next[field];changed=true;}
      }
    }
    const parent=snapshotParentKey(provider,chat,snapshot.graph?.edges||null);
    if(!parent)oldestShown=Math.min(oldestShown,stamp(chat.updatedAt));
  }
  // Retention is decided server-side: a retained row carries its thread key, so
  // dropping it here when a live poll omits the thread would remove the whole thread.
  // A valid unseen identity or newer outside activity means the structure needs enrichment.
  const roots=(snapshot.providers||[]).reduce((n,p)=>n+(p.chats||[]).filter(c=>!snapshotParentKey(p,c,snapshot.graph?.edges||null)).length,0);
  const outside=(live.chats||[]).filter(item=>!initiallyLoaded.has(item.key)&&(()=>{const owner=liveIdentity(item);return Boolean(owner&&(snapshot.providers||[]).some(provider=>provider.name===owner));})()&&(roots<CHAT_LIMIT||stamp(item.activityAt)>oldestShown)).map(item=>[item.key,item.activityAt]);
  return {changed,outside};
}
async function pollLive() {
  if(document.hidden)return;
  void loadQuota();
  if(state.liveBusy||(state.loading&&(!state.snapshot)))return;
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
    if(Date.now()-state.lastWatcherFetchAt>10000)void loadWatcher();
    const structural=Boolean(state.live.structureStamp&&state.snapshot?.structureStamp&&state.live.structureStamp!==state.snapshot.structureStamp);
    const fresh=result.outside.filter(([key,at])=>state.outsideSeen.get(key)!==at);
    fresh.forEach(([key,at])=>state.pendingOutside.set(key,at));
    if((structural||fresh.length||state.pendingOutside.size)&&Date.now()-state.structuralRefreshAt>STRUCTURE_REFRESH_MS){
      state.structuralRefreshAt=Date.now();
      if(state.loading){state.refreshPending=true;state.refreshPendingForce=true;}
      else void refresh(true);
    }
  }catch{markFail();}finally{state.liveBusy=false;}
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
  try{localStorage.setItem("tokenTrackerRunwayWindow",String(days));}catch{}
}
function openSearch(open) {
  state.searchOpen=open;
  const header=$("#chats-head"),toggle=$("#search-toggle"),input=$("#chat-search");
  if(open){if(header)header.hidden=false;if(toggle)toggle.hidden=false;}
  header?.classList?.toggle("searching",open);
  setNotch(false);
  if(open)input?.focus?.();
  else {if(state.query){state.query="";if(input)input.value="";queueSelectionRefresh();}if($('.widget')?.contains?.(document.activeElement))$('#sync-age')?.focus?.({preventScroll:true});}
}
function jumpTo(target, sub) {
  if(parseQuery(state.query).active&&state.forest&&!visibleRoots(state.forest).some(c=>c.key===target))openSearch(false);
  const chat=state.forest?.roots.find(c=>c.key===target);
  if(chat&&!inWindow(chat,state.windowDays)&&!state.pins.has(chat.key))setWindow(WINDOWS.find(d=>inWindow(chat,d))||7);
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
function handleClick(event) {
  const button=event.target.closest("[data-action]");
  const pop=$("#popover");
  if(pop&&!pop.hidden&&!(pop.contains?.(event.target))&&button?.dataset.action!=="watcher")closePopover();
  if(!button)return;
  const {action,key}=button.dataset;
  if(action==='widget-lock'){void toggleWidgetLock();return;}
  if(action==='toolbar-more'){toggleToolbarMenu();return;}
  if(action==='open-settings'){closeToolbarMenu();openDashboard('settings');return;}
  if(action==='jump-waiting'){jumpToWaiting();return;}
  if(action==='source'){setSource(button.dataset.source);return;}
  if(action==='dashboard'){openDashboard();return;}
  if(action==='keep-open'){void toggleKeepOpen();return;}

  if(action==="pin"){togglePin(key);closePopover();queueSelectionRefresh();}
  else if(action==="copy"){copyText(button.dataset.copy,button);closePopover();return;}
  // Answering: copy the chosen option, then open that exact chat in Codex so it is one paste away.
  else if(action==="answer"){copyAndOpenAnswer(button.dataset.thread,button.dataset.answer,button);return;}
  else if(action==="queue-answer"){queueAnswer(button.dataset.thread,button.dataset.answer,button);return;}
  else if(action==="open"){openInCodex(button.dataset.thread,button);return;}
  else if(action==="runway-question"){
    const chat=state.forest?.rows.find(item=>item.key===key);if(!chat||!runwayWaiting(chat))return;
    if(!isCodexChat(chat)){
      notch.manual=true;setNotch(true);openPopover(runwayQuestionPopover(chat),button);button.__questionId=chat.pendingQuestionId??chat.questionId??null;button.__questionAt=chat.pendingQuestionAt||null;button.__questionText=chat.pendingQuestion;button.__questionOptions=JSON.stringify(chat.pendingOptions||[]);return;
    }
    openInCodex(chat.id,button);return;
  }
  else if(action==="close-popover"){closePopover({restoreFocus:true});return;}
  else if(action==="jump"){jumpTo(button.dataset.target,button.dataset.sub);return;}
  else if(action==='hide-chat'){state.hiddenChats.has(key)?state.hiddenChats.delete(key):state.hiddenChats.add(key);saveHiddenChats();if(state.snapshot)renderChats(providersFor(state.snapshot));return;}
  else if(action==='show-hidden'){state.showHidden=!state.showHidden;if(state.snapshot)renderChats(providersFor(state.snapshot));queueSelectionRefresh();return;}
  else if(action==="analytics"){state.analyticsOpen=!state.analyticsOpen;if(state.snapshot)renderAnalytics(state.snapshot);return;}
  else if(action==="window"){setWindow(Number(button.dataset.days));queueSelectionRefresh();}
  else if(action==="watcher"){if(state.popover===button)closePopover();else{const html=watcherPopoverHTML(button.dataset.provider);if(html){openPopover(html,button);void loadIdentityPolicyWarnings(button);}else closePopover();}return;}
  else if(action==="reset-settings"){closePopover();const api=window.pywebview?.api;if(api?.open_reset_actions)api.open_reset_actions();else window.open?.("/reset_actions.html","_blank","noopener");return;}
  else if(action==="search"){openSearch(!state.searchOpen);}
  else if(action==="search-close"){openSearch(false);}
  else if(action==="refresh"){refresh(true);return;}
  if(state.snapshot)renderChats(providersFor(state.snapshot));
}
function handleKey(event) {
  const typing=/INPUT|TEXTAREA/.test(event.target?.tagName||"");
  if(event.key==="Escape"){
    if(dashboardState.open){event.preventDefault();event.stopImmediatePropagation?.();closeDashboard();return;}
    if(state.popover){closePopover({restoreFocus:true});event.preventDefault();event.stopImmediatePropagation?.();if(state.snapshot)renderChats(providersFor(state.snapshot));return;}
    if(state.searchOpen){openSearch(false);event.preventDefault();event.stopImmediatePropagation?.();if(state.snapshot)renderChats(providersFor(state.snapshot));return;}
    return;
  }
  if(!typing&&(event.key==="/"||(event.ctrlKey&&event.key.toLowerCase()==="k"))){event.preventDefault();openSearch(true);return;}
  if(!typing&&(event.key==="Enter"||event.key===" ")&&event.target?.getAttribute?.("role")==="button"){event.preventDefault();event.target.click();}
}
function queueSelectionRefresh(){if(window.__TOKEN_TRACKER_TEST__)return;clearTimeout(state.selectionTimer);state.selectionTimer=setTimeout(()=>refresh(),250);}
window.tokenTrackerRefresh=refresh;
window.__tokenTrackerTest={state,render,buildForest,visibleRoots,renderRunwayCounts,jumpToWaiting,tick,renderChats,renderResets,renderTotals,handleClick,handleKey,applyLive,pollLive,runwayRank,dataIsStale,clockNow,renderUpdatedMarker,toggleToolbarMenu,refresh,scheduleBootstrapRetry,loadWatcher,applyWatcherLimits,applyQuotaSamples,reconcileSnapshotQuotas,acceptSnapshotCache,validateSnapshotCache,isoObservationTime,watcherLoader,statsUrl,runway:true,compact:true,keepOpenState,handleVisibilityChange,trackerSnippet,notch,widgetLock,setNotch,syncNotch,notchHeight,syncMonitors,toggleMonitor,applyMonitorResult,monitorSignature,predictedInspectLayoutWidth,writeInspectLayoutWidth,applyInspectLayoutWidth,refreshInspectCssScale,noteInspectViewportResize,cancelInspectLayoutReconcile,getMonitorState:()=>monitorState,getInspectWidthState:()=>({width:inspectLayoutWidth,signature:inspectMonitorSignature,cssScale:inspectCssScaleSignature,cacheKey:inspectWidthCacheKey,generation:inspectLayoutGeneration,correctionUsed:inspectWidthCorrectionUsed,correctedWidths:new Map(correctedInspectWidths),expectedSignature:inspectExpectedSignature,requestGeneration:inspectRequestGeneration,resizeGeneration:inspectResizeGeneration,requestResizeGeneration:inspectRequestResizeGeneration,bridgeBusy:compactBridge.busy,appliedSignature:compactBridge.appliedSignature,pendingSignature:compactBridge.pending?.signature||null}),syncWidgetLock,applyWidgetLock,setWidgetLock,toggleWidgetLock,recenterWidget,syncKeepOpen,applyKeepOpen,setKeepOpen,toggleKeepOpen,runwayCurrentEvidenceFresh,RUNWAY_EVIDENCE_MAX_AGE_MS,runwayStateElapsed,runwayCounts,runwayTitle,runwayAgentHasNewMessage,runwayMessageAge,runwayQuestionPopover,runwayVisibleActive,runwayRecentlyUnavailable,runwayPatchContent,patchKeyed,isAsking,runwayWaiting,runwayExecutionView,runwayEffectiveStatus,runwayRunning,runInfo,setupWindowDrag,
  renderLoading,
  nextWindowWithChats,parseQuery,cacheWarmth,warmthView,resetAt,resetParts,resetView,countdown,runSpan,ageShort,dayWord,providersFor,limitProvidersFor,sourceNames,effectiveSource,treeParentKey,runwayModelLabel,syncCompactClock,
  setupNotch,questionDismissToken,watcherTone,rollup,ownState,rowHTML,trayEntries,traySummaryHTML,trayState,agentStats,totalStats,statStripHTML,watcherPopoverHTML,isVisibleAgent,chatBlockHTML,runwayTreePathGeometry,
  normalizeStatus,statusRank,contextTone,fmt,money,safeNumber,agentLabel,modelLabel,runwayStatus,runwayActivityAge,runwayActivityEvidence,runwayDetailHTML,runwayTelemetryHTML,jumpTo,isAutoReview,setWindow,avatarIndex,markFor,filesHTML,optionsHTML,copyAndOpenAnswer,queueAnswer,analyticsHTML,renderAnalytics,renderGreeterStatus};
Object.assign(window.__tokenTrackerTest,{sourceNames,effectiveSource,setSource,updateSourceSwitch,providersFor,limitProvidersFor,liveIdentity,resumeCommand,runwayExecutionView,runwayWaiting,isAsking,runwayVisibleActive,applyIdentityPayload,policyWarningHTML,validPublicProviderPolicy,initialActiveSource,openDashboard,closeDashboard,dashboardState});window.__tokenTrackerTest.bankedResetView=bankedResetView;window.__tokenTrackerTest.widgetLock=widgetLock;window.__tokenTrackerTest.loadQuota=loadQuota;window.__tokenTrackerTest.loadProviderPolicy=loadProviderPolicy;window.__tokenTrackerTest.quotaBootstrap=quotaBootstrap;window.__tokenTrackerTest.quotaLoader=quotaLoader;window.__tokenTrackerTest.validQuotaMetadata=validQuotaMetadata;
if(!window.__TOKEN_TRACKER_TEST__){
  loadAvatars();
  document.addEventListener("click",handleClick);
  document.addEventListener("keydown",handleKey);
  window.addEventListener?.('resize',()=>{noteInspectViewportResize();refreshInspectCssScale();scheduleNotchSize();},{passive:true});
  window.visualViewport?.addEventListener?.('resize',()=>{if(refreshInspectCssScale()&&notch.mode==='inspect'){noteInspectViewportResize();scheduleNotchSize();}},{passive:true});
  $("#chat-search")?.addEventListener("input",event=>{state.query=event.target.value;if(state.snapshot)renderChats(providersFor(state.snapshot));queueSelectionRefresh();});
  $("#chat-list")?.addEventListener("pointerenter",()=>{state.pointerInList=true;});
  $("#chat-list")?.addEventListener("pointerleave",()=>{state.pointerInList=false;if(state.snapshot)renderChats(providersFor(state.snapshot));});
  {
    $("#window-minimize")?.addEventListener("click",()=>window.pywebview?.api?.minimize?.());
    $("#window-fit")?.addEventListener("click",()=>window.pywebview?.api?.fit?.());
    $("#window-maximize")?.addEventListener("click",()=>window.pywebview?.api?.toggle_maximize?.());
    $("#window-close")?.addEventListener("click",()=>window.pywebview?.api?.close?.());
  }
  $(".widget").classList.add("loading");
  setupNotch();setupWindowDrag();renderLoading();void loadProviderPolicy();void loadQuota();refresh();setInterval(tick,1000);setInterval(()=>{if(!document.hidden)refresh();},FULL_REFRESH_MS);setInterval(pollLive,LIVE_MS);setInterval(pollToday,TODAY_REFRESH_MS);
  document.addEventListener("visibilitychange",handleVisibilityChange);
}
