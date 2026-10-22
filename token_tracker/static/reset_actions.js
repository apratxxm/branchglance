(() => {
  const $ = (s) => document.querySelector(s);
  let settingsToken="";
  try {
    settingsToken=new URLSearchParams((window.location?.hash||"").replace(/^#/g,"")).get("t")||"";
    window.history?.replaceState?.(null,"",`${window.location.pathname}${window.location.search}`);
  } catch {}
  const state = { action: null, threads: [], selected: new Set(), busy: false, loaded: false, dirty: false, policy: null, policyReady: false, canGreet: false, canContinue: false, claude: null, claudeBusy: false };
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const message = (text, error=false) => { const el=$("#message"); el.textContent=text||""; el.classList.toggle("error",error); };
  const canChangeClaude = () => Boolean(settingsToken);
  function renderClaude() {
    const value=state.claude,status=$("#claude-status"),button=$("#claude-toggle"),explanation=$("#claude-explanation"),readOnly=$("#claude-readonly-note");
    if(!value){status.textContent="Unavailable";button.textContent="Turn on Claude Code";button.disabled=true;explanation.hidden=true;readOnly.hidden=canChangeClaude();return;}
    const labels={off:"Off",on:"On",partial:"Partially enabled",blocked:"Blocked",unavailable:"Unavailable",vetoed:"Turned off by an environment setting",checking:"Checking?"};
    status.textContent=value.state==="checking"?"Checking… · Status is not confirmed.":[labels[value.state]||"Unavailable",value.reason,value.restartRequired?"Restart Branchglance to apply changes.":""].filter(Boolean).join(" · ");
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
  function mode() { return document.querySelector('input[name="mode"]:checked')?.value || "greet"; }
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
  }
  function updatePermissionCopy() {
    const greet=$("#greet-help"),continuation=$("#continue-help"),note=$("#permission");
    if(greet)greet.textContent=state.canGreet?"Keep the current greeting behavior.":"Unavailable: Codex greeting permission is off in startup settings.";
    if(continuation)continuation.textContent=state.canContinue?"Queue the exact message “continue” for the next genuine reset.":"Unavailable: enable Codex collection, display, and explicit send permission in startup settings.";
    if(note)note.textContent=!state.policyReady?"Permission status is unavailable. No action settings were loaded.":!state.policy.show||!state.policy.collect?"Codex action settings are hidden because collection or display is disabled.":!state.canGreet&&!state.canContinue?"Automatic actions are disabled by startup permissions.":!state.canContinue?"Chat continuation requires explicit Codex send permission.":"";
  }
  function render() {
    const cont=mode()==="continue"; $("#picker").hidden=!cont;
    const greetRadio=document.querySelector('input[name="mode"][value="greet"]'),continueRadio=document.querySelector('input[name="mode"][value="continue"]');
    if(greetRadio)greetRadio.disabled=state.busy||!state.policyReady||!state.canGreet;
    if(continueRadio)continueRadio.disabled=state.busy||!state.policyReady||!state.canContinue;
    if(!state.canGreet&&!state.canContinue&&greetRadio)greetRadio.checked=true;
    else if(mode()==="continue"&&!state.canContinue&&state.canGreet&&greetRadio)greetRadio.checked=true;
    else if(mode()==="greet"&&!state.canGreet&&state.canContinue&&continueRadio)continueRadio.checked=true;
    updatePermissionCopy();
    const query=$("#search").value.trim().toLocaleLowerCase();
    const available=state.threads.filter(t=>t.status!=="archived"&&String(t.title||"").toLocaleLowerCase().includes(query));
    $("#threads").innerHTML=available.length?available.map(t=>`<label class="thread"><input type="checkbox" data-id="${esc(t.id)}" ${state.selected.has(t.id)?"checked":""}><span class="thread-title" title="${esc(t.title)}">${esc(t.title||"Untitled chat")}</span><span class="status">${esc(t.status||"unknown")}</span></label>`).join(""):'<div class="empty">No matching chats.</div>';
    $("#selected-count").textContent=`${state.selected.size} selected`;
    $("#save").disabled=state.busy||!state.policyReady||!state.canGreet&&!state.canContinue;
    renderLastRun(state.action?.lastRun);
  }
  function renderLastRun(run) {
    const box=$("#last-run"); if(!run){box.hidden=true;box.textContent="";return;}
    const rows=Array.isArray(run.targets)?run.targets:[];
    box.hidden=false; box.innerHTML=`<h2>Last reset action</h2><div>${esc(run.at||"Time unavailable")}</div>${rows.length?`<ul>${rows.map(t=>`<li>${esc(titleFor(t.threadId))}: ${esc(t.status||"unknown")}${t.reason?` — ${esc(t.reason)}`:""}</li>`).join("")}</ul>`:"<div>No target details recorded.</div>"}`;
  }
  function titleFor(id) { return state.threads.find(t=>t.id===id)?.title || id || "Unknown chat"; }
  async function load() {
    state.busy=true; render(); message("Loading settings…");
    try {
      const policyResponse=await fetch("/api/provider-policy",{cache:"no-store",signal:AbortSignal.timeout(10000)});
      if(!policyResponse.ok)throw new Error("Permission status is unavailable.");
      applyPolicy(await policyResponse.json());
      if(!state.policyReady)throw new Error("Permission status is unavailable.");
      if(!state.policy.collect||!state.policy.show){state.action=null;state.threads=[];state.selected.clear();state.loaded=true;message("");return;}
      const response=await fetch("/api/reset-action",{cache:"no-store",signal:AbortSignal.timeout(10000)});
      const data=await response.json(); if(!response.ok||!data.ok)throw new Error(data.error||"Settings are unavailable.");
      const action=data.action&&typeof data.action==="object"?{...data.action}:null;
      const reusable=Boolean(state.canContinue&&action?.mode==="continue"&&action?.schemaVersion===2&&action?.armed===true);
      if(action){if(action.mode!=="continue"||!reusable)action.threadIds=[];if(!state.canContinue||action.mode==="continue"&&!reusable){if(action.lastRun?.targets)action.lastRun={...action.lastRun,targets:[]};}}
      state.action=action; state.threads=Array.isArray(data.threads)?data.threads:[];
      if(!state.loaded&&!state.dirty){
        if(reusable&&Array.isArray(state.action?.threadIds))state.selected=new Set(state.action.threadIds);
        const preferred=state.action?.mode==="continue"&&reusable?"continue":state.action?.mode==="greet"&&state.canGreet?"greet":state.canGreet?"greet":state.canContinue?"continue":"greet";
        const radio=document.querySelector(`input[name="mode"][value="${preferred}"]`);if(radio)radio.checked=true;
      }
      state.loaded=true;
      const staleSelection=action?.mode==="continue"&&!reusable;
      message(data.threadsAvailable===false?`${data.error||"Chat list unavailable."} Retry to refresh the list.`:staleSelection?"Saved chat selections were not restored. Re-select chats and save to arm a new one-shot action.":"");
    } catch(e) { if(!state.policyReady){state.canGreet=false;state.canContinue=false;state.action=null;state.threads=[];state.selected.clear();}message(e.message||"Could not load settings. Retry to try again.",true); }
    finally { state.busy=false; render(); }
  }
  async function save() {
    const chosenMode=mode(); if(!state.policyReady){message("Permission status is unavailable. Action was not saved.",true);return;}
    if(chosenMode==="greet"&&!state.canGreet||chosenMode==="continue"&&!state.canContinue){message("This action is disabled by Codex startup permissions.",true);return;}
    if(chosenMode==="continue"&&state.selected.size===0){message("Select at least one chat to continue.",true);return;}
    if(state.selected.size>20){message("Choose no more than 20 chats.",true);return;}
    state.busy=true;render();message("Saving action…");
    try {
      const response=await fetch("/api/reset-action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:chosenMode,threadIds:chosenMode==="continue"?[...state.selected]:[]}),cache:"no-store",signal:AbortSignal.timeout(15000)});
      const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||"Could not save reset action.");
      state.action=data.action;state.selected=new Set(data.action?.schemaVersion===2&&data.action?.armed===true?data.action.threadIds||[]:[]);state.dirty=false;message("Choice saved. It runs at the next reset only while auto actions are enabled.");
    } catch(e){message(e.message||"Could not save action. Your draft is still here.",true);}
    finally {state.busy=false;render();}
  }
  document.addEventListener("change",e=>{if(e.target.matches('input[name="mode"]')){state.dirty=true;render();}if(e.target.matches('#threads input[type="checkbox"]')){state.dirty=true;e.target.checked?state.selected.add(e.target.dataset.id):state.selected.delete(e.target.dataset.id);render();}});
  $("#search").addEventListener("input",render); $("#refresh").addEventListener("click",load); $("#save").addEventListener("click",save);
  $("#cancel").addEventListener("click",()=>window.close()); $("#claude-toggle").addEventListener("click",toggleClaude); $("#refresh").addEventListener("click",()=>{load();loadClaude();});
  load();loadClaude();
  if(window.__RESET_ACTIONS_TEST__)window.__resetActionsTest={state,render,load,save,mode,esc,renderLastRun,loadClaude,renderClaude,toggleClaude};
})();
