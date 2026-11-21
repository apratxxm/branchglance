(() => {
  const $ = (s) => document.querySelector(s);
  const embedded=new URLSearchParams(window.location.search).get("embedded")==="1";
  let policyFailed=false;
  if(embedded){document.querySelectorAll("#ledger-history .ledger-row").forEach(row=>row.remove());document.documentElement.classList.add("settings-embedded");document.body.classList.add("settings-embedded");document.body.dataset.source="Codex";}
  else { $("#modes").hidden=true;$("#picker").hidden=true;$("#oneshot-note").hidden=true;$("footer").hidden=true;$("#message").hidden=true; }
  let settingsToken="";
  try {
    settingsToken=new URLSearchParams((window.location?.hash||"").replace(/^#/g,"")).get("t")||"";
    window.history?.replaceState?.(null,"",`${window.location.pathname}${window.location.search}`);
  } catch {}
  const state = { action: null, threads: [], selected: new Set(), busy: false, loaded: false, actionReady: false, threadsReady: false, dirty: false, actionChanged: false, policy: null, policyReady: false, canGreet: false, canContinue: false, claude: null, claudeBusy: false, page: 0, ledger: null, purge: {}, purgeSequence: 0, exportBusy: false, exportStatus: "", exportError: false };
  function setEmbeddedState(text,unavailable=false){if(!embedded)return;const box=$("#embedded-policy-state"),message=$("#embedded-policy-message"),retry=$("#embedded-policy-retry");if(box)box.hidden=false;if(message)message.textContent=text;if(retry)retry.hidden=!unavailable;}
  // Permission names follow the Providers line: display (shown), collecting, auto-send. Only permissions that are really off are named.
  const PERMISSION_LABELS=[["show","display"],["collect","collection"],["send","greeting"]];
  const missingPermissions=()=>PERMISSION_LABELS.filter(([key])=>state.policy?.[key]!==true).map(([,label])=>label);
  const joinLabels=items=>items.length<2?items.join(""):`${items.slice(0,-1).join(", ")} and ${items.at(-1)}`;
  const reactionsDenied=()=>state.policyReady&&!state.canGreet&&!state.canContinue;
  function renderEmbeddedPolicy(){if(!embedded)return;if(!state.policyReady){setEmbeddedState(policyFailed?"Couldn't load reset permissions.":"Checking reset permissions…",policyFailed);return;}if(reactionsDenied()){const missing=missingPermissions();setEmbeddedState(missing.length===1?`Off: ${missing[0]} is off for Codex in startup settings.`:missing.length?`Off: turn on ${joinLabels(missing)} for Codex in startup settings.`:"Off: Codex startup permissions do not allow automatic actions.");}else{setEmbeddedState("Available");const box=$("#embedded-policy-state");if(box)box.hidden=true;}}
  // Saved usage history: the purge needs the settings token, so the embedded frame shows its rows read-only.
  const LEDGER_PROVIDERS = ["Claude", "Codex"];
  const PURGE_POLL_MS = 2000, PURGE_POLL_LIMIT = 30;
  const purgeTimers = {};
  // The embedded frame never scrolls: the chat list shows a few rows at a time with Previous and Next.
  const PAGE_SIZE = 3;
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const message = (text, error=false) => { const el=$("#message"); el.textContent=text||""; el.classList.toggle("error",error); };
  const canChangeClaude = () => Boolean(settingsToken);
  let featureValues={}, statuslineConnected=false;
  async function loadFeatures() {
    if(embedded)return;
    try {
      const [f,s]=await Promise.all([fetch('/api/features',{cache:'no-store'}),fetch('/api/statusline',{cache:'no-store'})]);
      const data=await f.json(), line=await s.json();
      if(!f.ok||!data.ok||!s.ok||!line.ok)throw new Error('Feature settings are unavailable.');
      featureValues=data;statuslineConnected=Boolean(line.connected);
      for(const name of ['live_usage','ledger','claude_send']) {
        const input=$(`[data-feature="${name}"]`), note=$(`[data-feature-status="${name}"]`), value=data[name];
        if(!input||!value)continue;
        input.checked=Boolean(value.effective);input.disabled=!settingsToken||value.env_locked;
        if(note)note.textContent=[value.env_locked?'Set by environment.':'',value.restart_pending?'Applies after restart.':'',name==='claude_send'?'Requires Claude collection consent; applies after restart.':''].filter(Boolean).join(' ');
      }
      const button=$('#statusline-toggle'),label=$('#statusline-state');
      if(button){button.disabled=!settingsToken;button.textContent=statuslineConnected?'Disconnect Claude status line':'Connect Claude status line';}
      if(label)label.textContent=statuslineConnected?'Status line: connected':'Status line: not connected';
    } catch(e){const note=$('#feature-message');if(note)note.textContent=e.message;}
  }
  async function changeFeature(name,value) {
    try {
      const response=await fetch('/api/features',{method:'POST',headers:{'Content-Type':'application/json','X-Branchglance-Settings-Token':settingsToken},body:JSON.stringify({name,value})});
      const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||'Setting could not be saved.');
      $('#feature-message').textContent='Setting saved.';
    } catch(e){$('#feature-message').textContent=e.message;}
    await loadFeatures();
  }
  async function toggleStatusline() {
    try {
      const response=await fetch('/api/statusline',{method:'POST',headers:{'Content-Type':'application/json','X-Branchglance-Settings-Token':settingsToken},body:JSON.stringify({action:statuslineConnected?'disconnect':'connect'})});
      const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||'Status line could not be changed.');
      $('#feature-message').textContent=data.reason||'Status line updated.';
    } catch(e){$('#feature-message').textContent=e.message;}
    await loadFeatures();
  }
  async function loadClaudeGreeting() {
    try {
      const response=await fetch('/api/limit-reset',{cache:'no-store'}), data=await response.json();
      const button=$('#claude-rearm');if(button)button.hidden=!(settingsToken && (!response.ok || data?.providers?.Claude?.phase==='account_changed'));
    } catch { const button=$('#claude-rearm');if(button)button.hidden=!settingsToken; }
  }
  async function rearmClaudeGreeting() {
    try {
      const response=await fetch('/api/claude-greeting-rearm',{method:'POST',headers:{'Content-Type':'application/json','X-Branchglance-Settings-Token':settingsToken},body:'{}'});
      if(!response.ok)throw new Error('Claude greeting could not be armed.');
      message('Claude greeting re-armed.'); await loadClaudeGreeting();
    } catch(e) { message(e.message,true); }
  }
  function renderClaude() {
    const value=state.claude,status=$("#claude-status"),button=$("#claude-toggle"),explanation=$("#claude-explanation"),readOnly=$("#claude-readonly-note");
    if(!value){status.textContent="Unavailable";button.textContent="Turn on Claude Code";button.disabled=true;explanation.hidden=true;readOnly.hidden=canChangeClaude();return;}
    const labels={off:"Off",on:"On",partial:"Partially enabled",blocked:"Blocked",unavailable:"Unavailable",vetoed:"Turned off by an environment setting",checking:"Checking?"};
    status.textContent=value.state==="checking"?"Checking… · Status is not confirmed.":[labels[value.state]||"Unavailable",value.reason,value.restartRequired?"Restart Branchglance to apply changes.":""].filter(Boolean).join(" · ");status.title=status.textContent;
    const cleanup=(value.state==="blocked"||value.state==="vetoed")&&Boolean(value.consent||(value.hooks?.installed||0)>0);
    const turningOff=["on","partial"].includes(value.state)||cleanup;
    button.textContent=turningOff?"Turn off":"Turn on Claude Code";
    button.disabled=state.claudeBusy||!canChangeClaude()||["checking","unavailable"].includes(value.state)||(["blocked","vetoed"].includes(value.state)&&!cleanup);
    explanation.hidden=value.state!=="off";
    readOnly.hidden=canChangeClaude();
  }
  async function loadClaude({reconcile=false}={}) {
    try {
      const response=await fetch("/api/claude-collection",{cache:"no-store",signal:AbortSignal.timeout(10000)});
      const data=await response.json();if(!response.ok)throw new Error(data.error||"Claude Code status is unavailable.");
      if(!data?.ok)throw new Error(data?.error||"Claude Code status is unavailable.");
      state.claude=data;
    } catch(e) { state.claude=reconcile?{state:"checking",reason:"Status is not confirmed.",restartRequired:false}:{state:"unavailable",reason:e.message||"Claude Code status is unavailable.",restartRequired:false}; }
    renderClaude();
    return state.claude.state!=="checking";
  }
  async function toggleClaude() {
    if(!state.claude||!canChangeClaude())return;
    const cleanup=(state.claude.state==="blocked"||state.claude.state==="vetoed")&&Boolean(state.claude.consent||(state.claude.hooks?.installed||0)>0);
    if(["unavailable","checking"].includes(state.claude.state)||(["blocked","vetoed"].includes(state.claude.state)&&!cleanup))return;
    const action=["on","partial"].includes(state.claude.state)||cleanup?"disable":"enable";
    state.claudeBusy=true;renderClaude();
    try {
      const response=await fetch("/api/claude-collection",{method:"POST",headers:{"Content-Type":"application/json","X-Branchglance-Settings-Token":settingsToken},body:JSON.stringify({action}),cache:"no-store",signal:AbortSignal.timeout(20000)});
      const data=await response.json();if(!response.ok||!data?.ok)throw new Error(data?.reason||data?.error||"Claude Code setting could not be changed.");
      state.claude=data;
    } catch(e) {
      state.claude={state:"checking",reason:"Status is not confirmed.",restartRequired:false};renderClaude();
      await loadClaude({reconcile:true});
    } finally { state.claudeBusy=false;renderClaude(); }
  }
  function mode() { return document.querySelector('input[name="mode"]:checked')?.value || "off"; }
  function setEmbeddedPart(id,text,unavailable=false){if(!embedded)return;const box=$(id),key=id.replace(/-state$/,"");const message=$(key+"-message"),retry=$(key+"-retry");if(box)box.hidden=!text;if(message)message.textContent=text||"";if(retry)retry.hidden=!unavailable;}
  function readPolicy(value) {
    if(!value||value.schemaVersion!==1||typeof value.generation!=="string"||!value.generation||!Array.isArray(value.providers))return null;
    const codex=value.providers.find(provider=>provider?.name==="Codex");
    if(!codex||typeof codex.collect!=="boolean"||typeof codex.show!=="boolean"||typeof codex.send!=="boolean"||typeof codex.sendOrigin!=="string"||!codex.capabilities||typeof codex.capabilities.greet!=="boolean"||typeof codex.capabilities.continue!=="boolean")return null;
    return {generation:value.generation,collect:codex.collect,show:codex.show,send:codex.send,sendOrigin:codex.sendOrigin,capabilities:codex.capabilities};
  }
  function applyPolicy(value) {
    const previousGeneration=state.policy?.generation;
    state.policy=readPolicy(value);state.policyReady=Boolean(state.policy);
    const policy=state.policy;
    state.canGreet=Boolean(policy?.collect&&policy.show&&policy.send&&policy.capabilities.greet&&["provider_env","legacy_global"].includes(policy.sendOrigin));
    state.canContinue=Boolean(policy?.collect&&policy.show&&policy.send&&policy.sendOrigin==="provider_env"&&policy.capabilities.continue);
    if(!state.canContinue||previousGeneration&&previousGeneration!==policy?.generation){state.selected.clear();if(state.action?.mode==="continue"){state.action={...state.action,threadIds:[],lastRun:state.action.lastRun?.targets?{...state.action.lastRun,targets:[]}:state.action.lastRun};}}
    renderEmbeddedPolicy();
  }
  // Embedded mode names a denial once, in the policy state line. Both radios keep no help text when both actions are denied.
  function continueBlockedCopy(){const missing=missingPermissions();return missing.length?`Needs ${joinLabels(missing)} on for Codex.`:state.policy?.sendOrigin!=="provider_env"?"Needs Codex send turned on by environment variable. The policy file allows greetings only.":"Continue is unavailable under startup permissions.";}
  function updatePermissionCopy() {
    const greet=$("#greet-help"),continuation=$("#continue-help"),note=$("#permission"),denied=embedded&&reactionsDenied();
    const pending=!state.policyReady?(embedded?"":policyFailed?"Unavailable until permission status loads.":"Checking permission status."):null;
    if(greet)greet.textContent=pending??(denied?"":state.canGreet?"Sends one short request that uses Codex quota and repeats after each eligible reset.":"Unavailable: Codex greeting permission is off in startup settings.");
    if(continuation)continuation.textContent=pending??(denied?"":state.canContinue?"Send “continue” to the chats you pick.":embedded?continueBlockedCopy():"Unavailable: enable Codex collection, display, and explicit send permission in startup settings.");
    if(note){note.hidden=embedded;note.textContent=embedded?"":!state.policyReady?"Permission status is unavailable. No action settings were loaded.":!state.policy?.show||!state.policy?.collect?"Codex action settings are hidden because collection or display is disabled.":!state.canGreet&&!state.canContinue?"Automatic actions are disabled by startup permissions.":!state.canContinue?"Chat continuation requires explicit Codex send permission.":"";}
  }
  function savedMode() { return ["off","greet","continue"].includes(state.action?.mode)?state.action.mode:state.canGreet?"greet":state.canContinue?"continue":"off"; }
  function validSavedAction(action){
    if(!action||action.schemaVersion!==2||!(["off","greet","continue"].includes(action.mode))||typeof action.armed!=="boolean"||!Array.isArray(action.threadIds))return false;
    if(action.threadIds.some(id=>typeof id!=="string"||id!==id.toLowerCase()||!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id))||new Set(action.threadIds).size!==action.threadIds.length)return false;
    if(action.mode!=="continue"&&(action.threadIds.length!==0||action.armed))return false;
    if(action.mode==="continue"&&action.threadIds.length===0)return false;
    return action.lastRun==null||typeof action.lastRun==="object"&&!Array.isArray(action.lastRun);
  }
  function render() {
    const cont=mode()==="continue"; $("#picker").hidden=!cont;
    const resetNote=$("#oneshot-note");
    resetNote.textContent=cont?"Queues ‘continue’ once at the next detected reset. A queued chat has not necessarily started.":"";resetNote.hidden=!cont||policyFailed||!state.policyReady;
    const offRadio=document.querySelector('input[name="mode"][value="off"]');
    const greetRadio=document.querySelector('input[name="mode"][value="greet"]'),continueRadio=document.querySelector('input[name="mode"][value="continue"]');
    const actionReadReady=state.actionReady&&!state.actionLoadError;
    if(offRadio)offRadio.disabled=state.busy||!state.policyReady||!actionReadReady;
    if(greetRadio)greetRadio.disabled=state.busy||!state.policyReady||!actionReadReady||!state.canGreet;
    if(continueRadio)continueRadio.disabled=state.busy||!state.policyReady||!actionReadReady||!state.canContinue;
    updatePermissionCopy();
    if(embedded){
      $("#modes").hidden=policyFailed;$("#message").hidden=!state.policyReady;
      $("#picker").hidden=policyFailed||mode()!=="continue";
      $("#save").textContent="Save";
      if(!state.policyReady)renderEmbeddedPolicy();
      const actionUnavailable=state.policyReady&&state.policy?.collect&&state.policy?.show&&!state.actionReady;
      $("#saved-action-load-state").hidden=!state.actionChanged&&!state.actionLoadError&&!actionUnavailable;
      if(state.actionChanged)setEmbeddedPart("#saved-action-load-state","Saved action changed elsewhere. Discard this draft to load it.");
      else if(state.actionLoadError)setEmbeddedPart("#saved-action-load-state","Saved action is unavailable.",true);
      else if(actionUnavailable)setEmbeddedPart("#saved-action-load-state","Saved action is unavailable.");
    }
    const query=$("#search").value.trim().toLocaleLowerCase();
    const available=state.threads.filter(t=>t.status!=="archived"&&String(t.title||"").toLocaleLowerCase().includes(query));
    const pages=embedded?Math.max(1,Math.ceil(available.length/PAGE_SIZE)):1;
    state.page=Math.min(Math.max(0,state.page),pages-1);
    const visible=embedded?available.slice(state.page*PAGE_SIZE,(state.page+1)*PAGE_SIZE):available;
    $("#threads").innerHTML=!state.threadsReady?"":visible.length?visible.map(t=>`<label class="thread"><input type="checkbox" data-id="${esc(t.id)}" ${state.selected.has(t.id)?"checked":""}><span class="thread-title" title="${esc(t.title)}">${esc(t.title||"Untitled chat")}</span><span class="status">${esc(t.status||"unknown")}</span></label>`).join(""):'<div class="empty">No matching chats.</div>';
    const pager=$("#pager");
    if(pager){pager.hidden=!embedded||available.length<=PAGE_SIZE;$("#page-info").textContent=available.length?`${state.page*PAGE_SIZE+1}–${state.page*PAGE_SIZE+visible.length} of ${available.length}`:"";$("#page-prev").disabled=state.page<=0;$("#page-next").disabled=state.page>=pages-1;}
    renderSelection();
    renderLastRun(state.action?.lastRun);
    const saved=$("#saved-action-state");if(saved){saved.hidden=!state.actionReady;saved.textContent=state.action?.mode==="continue"&&state.action.armed?`Scheduled for the next reset · ${state.action.threadIds?.length||0} chats`:"No continuation scheduled";}
    if(!embedded){$("#picker").hidden=true;$("#last-run").hidden=true;$("#saved-action-state").hidden=true;$("#permission").hidden=true;}
  }
  // Selection controls update in place so a toggled chat keeps keyboard focus (no list rebuild).
  function renderSelection() {
    if(embedded)$("footer").hidden=!state.policyReady||!state.dirty;
    $("#selected-count").textContent=`${state.selected.size} selected`;
    $("#save").disabled=state.busy||!state.policyReady||!state.actionReady||state.actionChanged||mode()==="greet"&&!state.canGreet||mode()==="continue"&&(!state.canContinue||!state.threadsReady);
  }
  function renderLastRun(run) {
    const box=$("#last-run"); if(!run){box.hidden=true;box.textContent="";return;}
    const all=Array.isArray(run.targets)?run.targets:[],rows=embedded?all.slice(0,2):all,more=all.length-rows.length;
    if(embedded){const when=new Date(run.at||""),stamp=Number.isNaN(when.getTime())?String(run.at||"Time unavailable"):when.toLocaleString([],{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}),skipped=all.filter(t=>t.status==="skipped").length;
      const queued=all.filter(t=>t.status==="queued").length,unknown=all.filter(t=>t.status==="unknown").length;
      box.hidden=false;box.innerHTML=`<h2>Last reset action</h2><span>${esc(stamp)} · ${all.length} ${all.length===1?"chat":"chats"}${queued?`, ${queued} queued`:""}${skipped?`, ${skipped} skipped`:""}${unknown?`, ${unknown} unknown`:""}</span>${all.length?`<details><summary>Show details</summary><ul>${all.map(t=>`<li>${esc(titleFor(t.threadId))}: ${esc(t.status==="queued"?"Queued":t.status||"unknown")}${t.reason?` — ${esc(t.reason)}`:""}</li>`).join("")}</ul></details>`:""}`;return;}
    box.hidden=false; box.innerHTML=`<h2>Last reset action</h2><div>${esc(run.at||"Time unavailable")}</div>${rows.length?`<ul>${rows.map(t=>`<li>${esc(titleFor(t.threadId))}: ${esc(t.status||"unknown")}${t.reason?` — ${esc(t.reason)}`:""}</li>`).join("")}</ul>${more>0?`<div>+${more} more</div>`:""}`:"<div>No target details recorded.</div>"}`;
  }
  function titleFor(id) { return state.threads.find(t=>t.id===id)?.title || id || "Unknown chat"; }
  async function load() {
    const draft=state.dirty?{mode:mode(),selected:new Set(state.selected)}:null;
    const previousSavedAction=state.action?JSON.stringify(state.action):null;
    state.busy=true;state.policyReady=false;state.actionReady=false;state.actionLoadError=false;state.threadsReady=false;policyFailed=false; render(); message("Loading settings…");if(embedded)setEmbeddedState("Checking reset permissions…");
    try {
      const policyResponse=await fetch("/api/provider-policy",{cache:"no-store",signal:AbortSignal.timeout(10000)});
      if(!policyResponse.ok)throw new Error("Permission status is unavailable.");
      applyPolicy(await policyResponse.json());
      if(!state.policyReady)throw new Error("Permission status is unavailable.");
      if(!state.policy.collect||!state.policy.show){state.action=null;state.threads=[];state.selected.clear();state.loaded=true;message("");return;}
      render();
      const response=await fetch("/api/reset-action",{cache:"no-store",signal:AbortSignal.timeout(10000)});
      const data=await response.json(); if(!response.ok||!data.ok)throw new Error(data.error||"Settings are unavailable.");
      const action=data.action&&typeof data.action==="object"?{...data.action}:null;
      if(!validSavedAction(action))throw new Error("Saved action is malformed.");
      state.actionReady=true;state.actionLoadError=false;state.threadsReady=data.threadsAvailable!==false;
      if(draft&&state.loaded&&previousSavedAction!==JSON.stringify(action))state.actionChanged=true;
      else if(!draft)state.actionChanged=false;
      const reusable=Boolean(state.canContinue&&action?.mode==="continue"&&action?.schemaVersion===2&&action?.armed===true);
      if(action){if(action.mode!=="continue"||!reusable)action.threadIds=[];if(!state.canContinue||action.mode==="continue"&&!reusable){if(action.lastRun?.targets)action.lastRun={...action.lastRun,targets:[]};}}
      state.action=action;state.threads=Array.isArray(data.threads)?data.threads:[];
      if(draft){const radio=document.querySelector(`input[name="mode"][value="${draft.mode}"]`);if(radio)radio.checked=true;state.selected=draft.selected;}
      if(!state.dirty){
        state.selected=reusable?new Set(state.action.threadIds):new Set();
        const radio=document.querySelector(`input[name="mode"][value="${state.action.mode}"]`);if(radio)radio.checked=true;
      }
      state.loaded=true;
      renderEmbeddedPolicy();
      const staleSelection=action?.mode==="continue"&&!reusable;
      const retry=state.actionChanged?"Saved action changed elsewhere. Discard this draft to load it.":staleSelection?"Saved chat selections were not restored. Re-select chats and save to arm a new one-shot action.":"";
      message(retry,retry!=="");if(embedded){setEmbeddedPart("#chat-list-state",data.threadsAvailable===false?"Chat list unavailable.":"",data.threadsAvailable===false);}
    } catch(e) { if(!state.policyReady){policyFailed=true;state.canGreet=false;state.canContinue=false;state.action=null;state.threads=[];state.selected.clear();if(embedded)setEmbeddedState("Couldn't load reset permissions.",true);}else{state.actionReady=false;state.actionLoadError=true;}message(e.message||"Could not load settings. Retry to try again.",true); }
    finally { state.busy=false; render(); }
  }
  async function save() {
    const chosenMode=mode(); if(!state.policyReady||!state.actionReady||state.actionChanged){message("Permission or saved action is unavailable or changed. Discard this draft before saving.",true);return;}
    if(chosenMode==="greet"&&!state.canGreet||chosenMode==="continue"&&(!state.canContinue||!state.threadsReady)){message("This action is disabled or chat candidates are unavailable.",true);return;}
    if(chosenMode==="continue"&&state.selected.size===0){message("Select at least one chat to continue.",true);return;}
    if(state.selected.size>20){message("Choose no more than 20 chats.",true);return;}
    state.busy=true;render();message("Saving action…");
    try {
      const response=await fetch("/api/reset-action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:chosenMode,threadIds:chosenMode==="continue"?[...state.selected]:[]}),cache:"no-store",signal:AbortSignal.timeout(15000)});
      const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||"Could not save reset action.");
      if(!validSavedAction(data.action))throw new Error("Saved action response was invalid.");
      state.action=data.action;state.actionReady=true;state.actionLoadError=false;state.selected=new Set(data.action?.mode==="continue"&&data.action?.armed===true?data.action.threadIds||[]:[]);state.dirty=false;state.actionChanged=false;message("Choice saved. It runs only while startup permissions allow it.");
    } catch(e){message(e.message||"Could not save action. Your draft is still here.",true);}
    finally {state.busy=false;render();}
  }
  function ledgerReason(provider) {
    if(!settingsToken)return "Open settings from the Branchglance widget to change this.";
    if(!state.ledger)return "Ledger status is unavailable.";
    if(state.ledger.state==="disabled")return "Ledger is off";
    if(state.ledger.collect?.[provider])return "Turn off collection first";
    return "";
  }
  function renderLedger() {
    const exportButton=$("#export-history"),exportNote=$("#export-note"),exportStatus=$("#export-status");if(exportButton)exportButton.disabled=state.exportBusy||!state.ledger||state.ledger.state!=="live";if(exportNote)exportNote.textContent=!state.ledger?"History status unavailable or starting.":state.ledger.state==="disabled"?"History is off. Turn on usage history before exporting.":"Exports visible saved history as a CSV file.";if(exportStatus){exportStatus.textContent=state.exportStatus;exportStatus.classList.toggle("error",state.exportError);}
    for(const provider of LEDGER_PROVIDERS){
      const flow=state.purge[provider]||{phase:"idle"},reason=ledgerReason(provider),busy=flow.phase==="deleting"||flow.phase==="pending";
      const reasonEl=document.querySelector(`[data-reason="${provider}"]`),button=document.querySelector(`[data-purge="${provider}"]`),status=document.querySelector(`[data-status="${provider}"]`);
      if(reasonEl){reasonEl.textContent=reason;reasonEl.hidden=!reason;}
      const confirm=document.querySelector(`[data-confirm="${provider}"]`);
      if(confirm)confirm.hidden=flow.phase!=="confirm";
      const confirmButton=confirm?.querySelector?.(`[data-confirm-purge="${provider}"]`),cancelButton=confirm?.querySelector?.(`[data-cancel-purge="${provider}"]`);if(confirmButton)confirmButton.dataset.confirmationId=flow.confirmationId||"";if(cancelButton)cancelButton.dataset.confirmationId=flow.confirmationId||"";
      if(button){button.disabled=Boolean(reason)||busy||flow.phase==="confirm"||flow.phase==="checking";}
      if(status){
        const text=flow.phase==="deleting"?"Deleting…":flow.phase==="pending"?"Still deleting…":flow.phase==="done"?"Deleted":flow.phase==="error"?flow.text:"";
        status.textContent=text;status.classList.toggle("error",flow.phase==="error");
      }
    }
  }
  async function loadLedger() {
    try {
      const response=await fetch("/api/ledger/status",{cache:"no-store",signal:AbortSignal.timeout(10000)});
      const data=await response.json();
      state.ledger=response.ok&&data?.ok?data:null;
    } catch { state.ledger=null; }
    renderLedger();
    return state.ledger;
  }
  async function exportHistory(){
    if(state.exportBusy||!state.ledger||state.ledger.state!=="live")return;
    const api=window.pywebview?.api;
    if(typeof api?.save_ledger_csv!=="function"){
      window.location.href="/api/ledger/export?range=all&format=csv";
      return;
    }
    state.exportBusy=true;state.exportStatus="Choose where to save the CSV.";state.exportError=false;renderLedger();
    try{
      const result=await api.save_ledger_csv();
      if(result?.ok){state.exportStatus="Saved CSV.";}
      else if(result?.cancelled){state.exportStatus="Export cancelled.";}
      else throw new Error(result?.error||"Could not export saved history.");
    }catch(error){state.exportStatus=error?.message||"Could not export saved history.";state.exportError=true;}
    finally{state.exportBusy=false;renderLedger();}
  }
  function purgeClick(provider) {
    const flow=state.purge[provider]||{phase:"idle"};
    if(ledgerReason(provider)||flow.phase==="checking"||flow.phase==="deleting"||flow.phase==="pending")return;
    state.purge[provider]={phase:"confirm",confirmationId:++state.purgeSequence};renderLedger();
  }
  function cancelPurge(provider,confirmationId){
    const flow=state.purge[provider];if(flow?.phase!=="confirm"||flow.confirmationId!==confirmationId)return;
    state.purgeSequence++;state.purge[provider]={phase:"idle"};renderLedger();document.querySelector(`[data-purge="${provider}"]`)?.focus?.();
  }
  async function confirmPurge(provider,confirmationId) {
    const flow=state.purge[provider];
    if(flow?.phase!=="confirm"||flow.confirmationId!==confirmationId||ledgerReason(provider))return;
    state.purge[provider]={phase:"checking",confirmationId};renderLedger();
    await loadLedger();
    if(state.purge[provider]?.phase!=="checking"||state.purge[provider]?.confirmationId!==confirmationId)return;
    const reason=ledgerReason(provider);
    if(reason){state.purge[provider]={phase:"error",text:reason};renderLedger();document.querySelector(`[data-status="${provider}"]`)?.focus?.();return;}
    state.purge[provider]={phase:"deleting",confirmationId};renderLedger();
    try {
      const response=await fetch("/api/ledger/purge",{method:"POST",headers:{"Content-Type":"application/json","X-Branchglance-Settings-Token":settingsToken},body:JSON.stringify({provider}),cache:"no-store",signal:AbortSignal.timeout(15000)});
      const data=await response.json().catch(()=>({}));
      // A 202 carries the ticket; only an explicit done result for that provider and ticket reads as Deleted.
      if(response.status===202&&data.pending){state.purge[provider]={phase:"pending",ticket:data.ticket};renderLedger();pollPurge(provider,0);return;}
      if(!response.ok||!data.ok)throw new Error(response.status===500||!data.error?"Saved history could not be deleted.":data.error);
      state.purge[provider]={phase:"done",ticket:data.ticket};
    } catch(e) { state.purge[provider]={phase:"error",text:e.message||"Saved history could not be deleted."}; }
    renderLedger();
    await loadLedger();
  }
  // A 202 means the writer still owns the purge. Re-check health until this provider leaves purging and its own
  // ticket has a result. A missing or mismatched result is never success: it stays pending until the poll limit.
  function pollPurge(provider, polls) {
    purgeTimers[provider]=setTimeout(async()=>{
      await loadLedger();
      const flow=state.purge[provider];
      if(flow?.phase!=="pending")return;
      const busy=!state.ledger||state.ledger.purging?.includes(provider);
      const result=state.ledger?.lastPurge?.[provider];
      if(!busy&&result&&flow.ticket&&result.ticket===flow.ticket){
        state.purge[provider]=result.result==="done"?{phase:"done",ticket:flow.ticket}:{phase:"error",text:result.result==="cancelled"?"Not deleted; Branchglance is closing.":"Saved history could not be deleted."};
      }else if(polls+1<PURGE_POLL_LIMIT){renderLedger();pollPurge(provider,polls+1);return;}
      else{state.purge[provider]={phase:"error",text:"Status unknown — reopen to check"};}
      renderLedger();
    },PURGE_POLL_MS);
  }
  async function reloadAll(){await Promise.all([load(),loadClaude(),loadLedger(),loadClaudeGreeting(),loadFeatures()]);}
  document.addEventListener("change",e=>{if(e.target.matches('input[name="mode"]')){state.dirty=true;render();}if(e.target.matches('#threads input[type="checkbox"]')){state.dirty=true;e.target.checked?state.selected.add(e.target.dataset.id):state.selected.delete(e.target.dataset.id);renderSelection();}});
  function discardDraft(){const radio=document.querySelector(`input[name="mode"][value="${savedMode()}"]`);if(radio)radio.checked=true;state.selected=new Set(state.action?.schemaVersion===2&&state.action?.armed===true?state.action.threadIds||[]:[]);state.dirty=false;state.actionChanged=false;message("");render();}
  document.addEventListener("keydown",event=>{if(embedded&&event.key==="Escape"){event.preventDefault();if(state.dirty){discardDraft();$("#modes")?.focus?.();}else window.parent.postMessage({type:"dashboard-reset-cancel"},window.location.origin);}});
  window.addEventListener?.("message",event=>{if(embedded&&event.origin===window.location.origin&&event.source===window.parent&&event.data?.type==="dashboard-reset-refresh"&&!state.dirty)load();});
  $("#search").addEventListener("input",()=>{state.page=0;render();}); $("#page-prev")?.addEventListener("click",()=>{state.page--;render();}); $("#page-next")?.addEventListener("click",()=>{state.page++;render();}); $("#refresh").addEventListener("click",reloadAll); $("#save").addEventListener("click",save);
  $("#embedded-policy-retry")?.addEventListener("click",reloadAll);
  $("#saved-action-retry")?.addEventListener("click",load);$("#chat-list-retry")?.addEventListener("click",load);
  $("#cancel").addEventListener("click",()=>{if(embedded)discardDraft();else window.close();});$("#export-history")?.addEventListener("click",exportHistory); $("#claude-toggle").addEventListener("click",toggleClaude);
  for(const provider of LEDGER_PROVIDERS){document.querySelector(`[data-purge="${provider}"]`)?.addEventListener("click",()=>purgeClick(provider));document.querySelector(`[data-cancel-purge="${provider}"]`)?.addEventListener("click",event=>cancelPurge(provider,event.currentTarget?.dataset?.confirmationId));document.querySelector(`[data-confirm-purge="${provider}"]`)?.addEventListener("click",event=>void confirmPurge(provider,event.currentTarget?.dataset?.confirmationId));}
  $('#claude-rearm')?.addEventListener('click',rearmClaudeGreeting);
  for(const name of ['live_usage','ledger','claude_send']) $(`[data-feature="${name}"]`)?.addEventListener('change',e=>changeFeature(name,e.target.checked));
  $('#statusline-toggle')?.addEventListener('click',toggleStatusline);
  if(embedded)setEmbeddedState("Checking reset permissions…");
  // The parent sizes the frame to this height, so the embedded form never scrolls on its own.
  if(embedded&&typeof ResizeObserver==="function"){const form=document.querySelector(".settings"),report=()=>window.parent.postMessage({type:"dashboard-reset-height",height:Math.ceil(form.getBoundingClientRect().height)},window.location.origin);new ResizeObserver(report).observe(form);report();}
  loadFeatures();
  load();loadClaude();loadLedger();loadClaudeGreeting();
  if(window.__RESET_ACTIONS_TEST__)window.__resetActionsTest={state,PAGE_SIZE,render,load,save,mode,esc,renderLastRun,loadClaude,renderClaude,toggleClaude,discardDraft,purgeClick,cancelPurge,confirmPurge,exportHistory,validSavedAction};
})();
