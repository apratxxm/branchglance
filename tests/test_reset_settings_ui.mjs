import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
const resetCss=fs.readFileSync(new URL("../token_tracker/static/reset_actions.css",import.meta.url),"utf8");

const source=fs.readFileSync(new URL("../token_tracker/static/reset_actions.js",import.meta.url),"utf8");
const html=fs.readFileSync(new URL("../token_tracker/static/reset_actions.html",import.meta.url),"utf8");
const app=fs.readFileSync(new URL("../token_tracker/static/app.js",import.meta.url),"utf8");
assert.match(html,/id="picker" hidden/);
assert.match(html,/id="claude-collection"/);
assert.match(html,/data-feature="live_usage"/);assert.match(html,/data-feature="ledger"/);assert.match(html,/data-feature="claude_send"/);
assert.match(html,/undocumented endpoint could break or be disallowed/i);
assert.match(html,/Enabling adds Branchglance hooks to Claude Code settings \(with a backup\) and reads Claude session activity locally\./);
assert.match(html,/Open settings from the Branchglance widget to change this\./);
assert.match(html,/value="off"/);assert.match(html,/Do not send anything after resets\. Quota tracking continues\./);
assert.match(source,/Queues ‘continue’ once at the next detected reset/);
assert.match(source,/queued chat has not necessarily started/i);
// The embedded frame pages its chat list and now has a reachable vertical scroll path.
assert.match(html,/id="pager"[^>]*hidden/);assert.match(html,/id="page-prev"/);assert.match(html,/id="page-next"/);
assert.match(source,/PAGE_SIZE = 3/);assert.match(source,/embedded\?available\.slice\(state\.page\*PAGE_SIZE/);
const frameCss=fs.readFileSync(new URL("../token_tracker/static/reset_actions.css",import.meta.url),"utf8");
assert.match(frameCss,/\.settings-embedded #threads\{[^}]*overflow:visible/);assert.doesNotMatch(frameCss.slice(frameCss.indexOf("Round 3")),/overflow:auto/);
assert.match(frameCss,/\.settings-embedded \.mode\+\.mode\{border-top:1px solid var\(--d-edge\);padding-top:8px;\}/,"embedded reset choices use the Settings separator token");
assert.match(frameCss,/\.settings-embedded #oneshot-note\{padding-top:8px;border-top:1px solid var\(--d-edge\);color:var\(--d-sub\);font-size:12px;\}/,"the conditional reset footer is a quiet Settings note");
assert.match(app,/data-action="reset-settings"/);
assert.match(app,/api\?\.open_reset_actions/);
assert.match(app,/window\.open\?\.\("\/reset_actions\.html","_blank","noopener"\)/);

const elements=new Map();
const listenerCounts=new Map();
function element(selector){if(!elements.has(selector))elements.set(selector,{value:"",textContent:"",innerHTML:"",hidden:false,disabled:false,dataset:{},classList:{toggle(){}},querySelector:child=>element(child),addEventListener(type,fn){this[`on${type}`]=fn;listenerCounts.set(`${selector}:${type}`,(listenerCounts.get(`${selector}:${type}`)||0)+1);}});return elements.get(selector);}
const radios=[];
const makeRadio=(value,checked)=>{const radio={value,disabled:false,_checked:checked};Object.defineProperty(radio,"checked",{get(){return this._checked;},set(value){this._checked=value;if(value)for(const other of radios)if(other!==this)other._checked=false;}});radios.push(radio);return radio;};
makeRadio("greet",true);makeRadio("continue",false);makeRadio("off",false);
const listeners={};
const document={querySelector:selector=>selector==='input[name="mode"]:checked'?radios.find(r=>r.checked):selector==='input[name="mode"][value="greet"]'?radios[0]:selector==='input[name="mode"][value="continue"]'?radios[1]:selector==='input[name="mode"][value="off"]'?radios[2]:element(selector),querySelectorAll:()=>[],addEventListener:(type,fn)=>listeners[type]=fn};
const calls=[];
const claudeCalls=[];
const gets=[];
const requests=[];
const requestMethods=[];
const requestHeaders=[];
const settingsToken="a-test-settings-token-with-no-url-leak";
const location={hash:`#t=${settingsToken}`,pathname:"/reset_actions.html",search:""};
const historyUrls=[];
let failSave=false,failActionRead=false,threadsAvailable=true;
let policy={schemaVersion:1,generation:"codex-send-v1",providers:[
  {name:"Codex",collect:true,show:true,send:true,sendOrigin:"provider_env",capabilities:{greet:true,continue:true}},
  {name:"Claude",collect:false,show:false,send:false,sendOrigin:"default",capabilities:{greet:false,continue:false}}
]};
let actionRecord={schemaVersion:2,mode:"greet",threadIds:[],armed:false,lastRun:null};
let claudeStatus={ok:true,state:"off",reason:null,hooks:{state:"missing",installed:0},consent:false,grant:false,restartRequired:false};
let failClaudeMutation=false,failClaudeStatus=false;
const features={live_usage:{effective:false,env_locked:false,restart_pending:false},ledger:{effective:false,env_locked:true,restart_pending:false},claude_send:{effective:false,env_locked:false,restart_pending:true}};
let statuslineConnected=false;
const window={__RESET_ACTIONS_TEST__:true,close(){},location,history:{replaceState(_state,_title,url){historyUrls.push(url);location.hash="";}}};
const context={document,window,AbortSignal:{timeout(){}},URLSearchParams,fetch:async(url,options={})=>{
  requests.push(url);
  requestMethods.push(options.method||"GET");
  requestHeaders.push(options.headers||{});
  if(url==="/api/provider-policy")return {ok:true,json:async()=>policy};
  if(url==="/api/features"){
    if(options.method==="POST"){
      assert.equal(options.headers["X-Branchglance-Settings-Token"],settingsToken);
      const {name,value}=JSON.parse(options.body);features[name].effective=value;
    }
    return {ok:true,json:async()=>({ok:true,...features})};
  }
  if(url==="/api/statusline"){
    if(options.method==="POST"){
      assert.equal(options.headers["X-Branchglance-Settings-Token"],settingsToken);
      statuslineConnected=JSON.parse(options.body).action==="connect";
    }
    return {ok:true,json:async()=>({ok:true,connected:statuslineConnected})};
  }
  if(url==="/api/claude-collection"){
    if(options.method==="POST"){
      assert.equal(options.headers["X-Branchglance-Settings-Token"],settingsToken);
      claudeCalls.push(JSON.parse(options.body).action);
      claudeStatus={ok:true,state:claudeCalls.at(-1)==="enable"?"on":"off",reason:null,
        hooks:{state:claudeCalls.at(-1)==="enable"?"installed":"missing",installed:claudeCalls.at(-1)==="enable"?14:0},
        consent:claudeCalls.at(-1)==="enable",grant:claudeCalls.at(-1)==="enable",restartRequired:true};
      if(failClaudeMutation){failClaudeMutation=false;throw new Error("response lost after commit");}
    } else if(failClaudeStatus)throw new Error("status unavailable");
    return {ok:true,json:async()=>claudeStatus};
  }
  gets.push(url);
  if(options.method==="POST"){
    calls.push(JSON.parse(options.body));
    if(failSave)return {ok:false,json:async()=>({error:"Could not save"})};
    return {ok:true,json:async()=>({ok:true,action:{schemaVersion:2,mode:calls.at(-1).mode,threadIds:calls.at(-1).threadIds,armed:calls.at(-1).mode==="continue",oneShot:true,lastRun:null}})};
  }
  if(url==="/api/reset-action"&&failActionRead)return {ok:false,json:async()=>({ok:false,error:"Read failed"})};
  return {ok:true,json:async()=>({ok:true,action:actionRecord,threadsAvailable,threads:threadsAvailable?[
    {id:"11111111-1111-4111-8111-111111111111",title:"<img src=x onerror=alert(1)>",status:"idle"},
    {id:"22222222-2222-4222-8222-222222222222",title:"Busy chat",status:"active"},
    {id:"33333333-3333-4333-8333-333333333333",title:"Archived",status:"archived"}
  ]:[],error:threadsAvailable?undefined:"Chat list is unavailable."})};
}};
vm.createContext(context);vm.runInContext(source,context);
await new Promise(resolve=>setTimeout(resolve,0));
const T=context.window.__resetActionsTest;
assert.equal(element('[data-feature="ledger"]').disabled,true);
assert.match(element('[data-feature-status="ledger"]').textContent,/environment/);
assert.match(element('[data-feature-status="claude_send"]').textContent,/restart/);
await element('[data-feature="live_usage"]').onchange({target:{checked:true}});
assert.equal(features.live_usage.effective,true);
await element('#statusline-toggle').onclick();
assert.equal(element('#statusline-toggle').textContent,'Disconnect Claude status line');
assert.deepEqual(historyUrls,["/reset_actions.html"]);
assert.equal(location.hash,"");
assert.ok(requests.indexOf("/api/provider-policy")<requests.indexOf("/api/reset-action"),"policy is checked before action settings are fetched");
assert.ok(requests.includes("/api/claude-collection"),"Claude Code status is loaded independently");
assert.equal(element("#claude-status").textContent,"Off");
assert.equal(element("#claude-toggle").textContent,"Turn on Claude Code");
assert.equal(element("#claude-explanation").hidden,false);
assert.equal(element("#claude-toggle").disabled,false,"window fragment enables its scoped control");
assert.equal(element("#claude-readonly-note").hidden,true);
assert.equal(requestMethods[requests.indexOf("/api/claude-collection")],"GET","HTTP status is read-only");
await T.toggleClaude();
assert.equal(claudeCalls[0],"enable");
assert.equal(element("#claude-status").textContent,"On · Restart Branchglance to apply changes.");
assert.equal(element("#claude-toggle").textContent,"Turn off");
T.state.claude={state:"partial",reason:"Some hooks are missing.",restartRequired:false};T.renderClaude();
assert.equal(element("#claude-status").textContent,"Partially enabled · Some hooks are missing.");
assert.equal(element("#claude-toggle").textContent,"Turn off");
assert.equal(element("#claude-toggle").disabled,false);
T.state.claude={state:"blocked",reason:"Policy file is malformed.",restartRequired:false};T.renderClaude();
assert.equal(element("#claude-toggle").disabled,true);
assert.match(element("#claude-status").textContent,/Policy file is malformed/);
T.state.claude={state:"blocked",reason:"Policy file is malformed.",hooks:{installed:14},consent:true};T.renderClaude();
assert.equal(element("#claude-toggle").disabled,false,"blocked collection with existing hooks can be revoked");
assert.equal(element("#claude-toggle").textContent,"Turn off");
await T.toggleClaude();
assert.equal(claudeCalls.at(-1),"disable");
T.state.claude={state:"vetoed",reason:"turned off by an environment setting",hooks:{installed:14},consent:true,restartRequired:false};T.renderClaude();
assert.match(element("#claude-status").textContent,/turned off by an environment setting/);
assert.doesNotMatch(element("#claude-status").textContent,/Restart Branchglance/);
assert.equal(element("#claude-toggle").textContent,"Turn off");
claudeStatus={ok:true,state:"on",reason:null,hooks:{state:"installed",installed:14},consent:true,grant:true,restartRequired:false};
await T.loadClaude();
failClaudeMutation=true;failClaudeStatus=true;await T.toggleClaude();
assert.equal(element("#claude-status").textContent,"Checking\u2026 \u00b7 Status is not confirmed.");
assert.equal(element("#claude-toggle").disabled,true,"uncertain mutation stays disabled until reconciliation succeeds");
failClaudeStatus=false;claudeStatus={ok:true,state:"off",reason:null,hooks:{state:"missing",installed:0},consent:false,grant:false,restartRequired:false};
await T.loadClaude({reconcile:true});
assert.equal(element("#claude-status").textContent,"Off");
assert.equal(element("#claude-toggle").disabled,false);
assert.equal(requestMethods.filter((method,index)=>requests[index]==="/api/claude-collection"&&method==="GET").length,4,"status is refreshed after uncertain mutations");
assert.equal(requestMethods.filter((method,index)=>requests[index]==="/api/claude-collection"&&method==="POST").length,3);
assert.equal(listenerCounts.get("#refresh:click"),1,"Refresh registers one listener");
assert.ok(requests.every(url=>!url.includes(settingsToken)),"the settings token is never put in the URL");
assert.ok(requestHeaders.some(headers=>headers["X-Branchglance-Settings-Token"]===settingsToken),"mutation carries the token in a header");
assert.match(element("#threads").innerHTML,/&lt;img/);
assert.doesNotMatch(element("#threads").innerHTML,/<img/);
radios[0].checked=false;radios[1].checked=true;T.state.selected=new Set(["11111111-1111-4111-8111-111111111111","22222222-2222-4222-8222-222222222222"]);T.render();
T.state.dirty=true;
assert.equal(element("#selected-count").textContent,"2 selected");
await T.load();
assert.equal(radios[1].checked,true,"refresh preserves the unsaved mode draft");
assert.equal(T.state.selected.size,2,"refresh preserves the unsaved chat selection");
await T.save();
assert.deepEqual(calls[0],{mode:"continue",threadIds:["11111111-1111-4111-8111-111111111111","22222222-2222-4222-8222-222222222222"]});
radios[0].checked=true;radios[1].checked=false;T.render();await T.save();
assert.deepEqual(calls[1],{mode:"greet",threadIds:[]});

actionRecord={schemaVersion:2,mode:"off",threadIds:[],armed:false,oneShot:true,lastRun:null};
await T.load();
assert.equal(radios[2].checked,true,"a clean reload checks the saved Off choice");
assert.equal(radios[0].checked,false);assert.equal(radios[1].checked,false);
radios[2].checked=false;radios[0].checked=true;actionRecord={schemaVersion:2,mode:"greet",threadIds:[],armed:false,oneShot:true,lastRun:null};await T.load();

actionRecord={schemaVersion:2,mode:"unknown",threadIds:[],armed:false,oneShot:true,lastRun:null};
const postsBeforeMalformed=calls.length;await T.load();
assert.equal(T.state.actionReady,false,"malformed saved state does not enable writes");
assert.equal(element("#save").disabled,true,"Save stays disabled for malformed state");
assert.ok(radios.every(radio=>radio.disabled),"malformed saved state disables every mode radio");
await T.save();assert.equal(calls.length,postsBeforeMalformed,"malformed state is never overwritten silently");
actionRecord={schemaVersion:2,mode:"greet",threadIds:[],armed:false,oneShot:true,lastRun:null};await T.load();

failActionRead=true;await T.load();
assert.ok(radios.every(radio=>radio.disabled),"failed saved-action load disables every mode radio");
assert.equal(element("#save").disabled,true,"failed saved-action load keeps Save blocked");
assert.equal(T.state.actionLoadError,true);
assert.equal(element("#message").textContent,"Read failed");
failActionRead=false;await T.load();
assert.equal(radios[0].checked,true,"a valid reload checks the saved greeting mode");

radios[0].checked=false;radios[2].checked=true;T.state.dirty=true;await T.save();
assert.deepEqual(calls[2],{mode:"off",threadIds:[]},"the saved Off option writes the backend Off mode");
assert.equal(T.state.action.mode,"off");assert.equal(T.state.action.armed,false);
actionRecord={schemaVersion:2,mode:"greet",threadIds:[],armed:false,oneShot:true,lastRun:null};await T.load();

radios[0].checked=false;radios[1].checked=true;T.state.dirty=true;T.state.selected=new Set(["11111111-1111-4111-8111-111111111111"]);
actionRecord={schemaVersion:2,mode:"off",threadIds:[],armed:false,oneShot:true,lastRun:null};
const postsBeforeConflict=calls.length;
await T.load();
assert.equal(T.state.actionChanged,true,"a competing Off save marks the local Continue draft stale");
assert.equal(element("#save").disabled,true,"a stale draft cannot overwrite the newer saved action");
await T.save();assert.equal(calls.length,postsBeforeConflict,"stale Continue draft is not posted");
T.discardDraft();
assert.equal(radios[2].checked,true,"discard loads the current saved Off mode");
assert.equal(T.state.actionChanged,false);
actionRecord={schemaVersion:2,mode:"greet",threadIds:[],armed:false,oneShot:true,lastRun:null};
await T.load();assert.equal(radios[0].checked,true,"a clean reload returns to the currently saved greeting mode");

actionRecord={schemaVersion:2,mode:"continue",threadIds:["11111111-1111-4111-8111-111111111111"],armed:false,oneShot:true,
  lastRun:{at:1,targets:[{threadId:"11111111-1111-4111-8111-111111111111",status:"queued"}]}};
await T.load();
assert.equal(T.state.action.threadIds.length,0,"the unarmed server selection is filtered from the view");
radios[1].checked=false;radios[2].checked=true;T.state.dirty=true;
threadsAvailable=false;await T.load();
assert.equal(T.state.actionChanged,false,"the same validated server action is not stale after display filtering");
assert.equal(element("#save").disabled,false,"Off can be saved while chat discovery is unavailable");
const postsBeforeFilteredRetry=calls.length;await T.save();
assert.equal(calls.length,postsBeforeFilteredRetry+1,"the unchanged server action does not block the Off save");
assert.deepEqual(calls.at(-1),{mode:"off",threadIds:[]});
threadsAvailable=true;actionRecord={schemaVersion:2,mode:"off",threadIds:[],armed:false,oneShot:true,lastRun:{at:1,targets:[]}};await T.load();

radios[0].checked=false;radios[1].checked=true;T.state.selected=new Set(["11111111-1111-4111-8111-111111111111"]);failSave=true;await T.save();
assert.equal(T.state.selected.size,1,"failed save retains the user's draft");
assert.match(element("#message").textContent,/Could not save/);

policy={schemaVersion:1,generation:"codex-no-send",providers:[
  {name:"Codex",collect:true,show:true,send:false,sendOrigin:"default",capabilities:{greet:true,continue:true}},
  {name:"Claude",collect:false,show:false,send:false,sendOrigin:"default",capabilities:{greet:false,continue:false}}
]};
await T.load();
assert.equal(radios[1].disabled,true,"continuation stays disabled without explicit send consent");
assert.equal(T.state.selected.size,0,"old selected ids are not restored without current permission");
const postsBeforeDeniedSave=calls.length;
radios[0].checked=false;radios[1].checked=true;await T.save();
assert.equal(calls.length,postsBeforeDeniedSave,"denied continuation is not posted");

policy={schemaVersion:1,generation:"codex-send-again",providers:[
  {name:"Codex",collect:true,show:true,send:true,sendOrigin:"provider_env",capabilities:{greet:true,continue:true}},
  {name:"Claude",collect:false,show:false,send:false,sendOrigin:"default",capabilities:{greet:false,continue:false}}
]};
actionRecord={schemaVersion:2,mode:"continue",threadIds:["11111111-1111-4111-8111-111111111111"],armed:false,oneShot:true,lastRun:null};
await T.load();
assert.equal(T.state.selected.size,0,"unarmed continuation selections are not restored");
assert.equal(T.state.action.threadIds.length,0);
assert.match(element("#message").textContent,/re-select chats/i);

policy={schemaVersion:1,generation:"codex-hidden",providers:[
  {name:"Codex",collect:false,show:false,send:false,sendOrigin:"default",capabilities:{greet:true,continue:true}},
  {name:"Claude",collect:false,show:false,send:false,sendOrigin:"default",capabilities:{greet:false,continue:false}}
]};
const actionReadsBeforeHidden=gets.length;
await T.load();
assert.equal(gets.length,actionReadsBeforeHidden,"hidden Codex settings are not fetched");

const plainWindow={__RESET_ACTIONS_TEST__:true,close(){},location:{hash:"",pathname:"/reset_actions.html",search:""},history:{replaceState(){}}};
const plainContext={...context,window:plainWindow};
vm.createContext(plainContext);vm.runInContext(source,plainContext);
await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(element("#claude-readonly-note").hidden,false,"browser without a token stays read-only");
assert.equal(element("#claude-toggle").disabled,true);
assert.equal(T.state.threads.length,0);

// Saved usage history: the delete button is gated by collection and ledger state, and sends only on the second click.
{
  const nodes=new Map();
  const node=(selector)=>{if(!nodes.has(selector))nodes.set(selector,{value:"",textContent:"",innerHTML:"",hidden:false,disabled:false,dataset:{},focused:false,focus(){this.focused=true;},classList:{toggle(){}},querySelector:child=>node(child),addEventListener(type,fn){this[`on${type}`]=fn;}});return nodes.get(selector);};
  const sent=[];
  const ledgerState={ok:true,state:"live",purging:[],lastPurge:null,collect:{Claude:true,Codex:false}};
  let currentLedger=ledgerState;
  const ledgerDocument={querySelector:node,querySelectorAll:()=>[],addEventListener(){}};
  const ledgerWindow={__RESET_ACTIONS_TEST__:true,location:{hash:"#t=ledger-test-token",pathname:"/reset_actions.html",search:"",href:""},history:{replaceState(){}}};
  const ledgerContext={document:ledgerDocument,window:ledgerWindow,AbortSignal:{timeout(){}},URLSearchParams,setTimeout,clearTimeout,fetch:async(url,options={})=>{
    if(url==="/api/ledger/status")return {ok:true,status:200,json:async()=>currentLedger};
    if(url==="/api/ledger/purge"){sent.push({url,method:options.method,token:options.headers["X-Branchglance-Settings-Token"],body:JSON.parse(options.body)});return {ok:true,status:200,json:async()=>({ok:true})};}
    return {ok:false,status:404,json:async()=>({ok:false,error:"not used"})};
  }};
  vm.createContext(ledgerContext);vm.runInContext(source,ledgerContext);
  const ledgerTest=ledgerWindow.__resetActionsTest;
  const settle=async()=>{for(let i=0;i<8;i++)await new Promise(resolve=>setTimeout(resolve,0));};
  await settle();
  let exportReply={ok:true};ledgerWindow.pywebview={api:{save_ledger_csv:async()=>exportReply}};
  await ledgerTest.exportHistory();assert.equal(node("#export-status").textContent,"Saved CSV.");
  exportReply={ok:false,cancelled:true};await ledgerTest.exportHistory();assert.equal(node("#export-status").textContent,"Export cancelled.");
  exportReply={ok:false,error:"Could not save the CSV file."};await ledgerTest.exportHistory();assert.equal(node("#export-status").textContent,"Could not save the CSV file.");
  delete ledgerWindow.pywebview;await ledgerTest.exportHistory();assert.equal(ledgerWindow.location.href,"/api/ledger/export?range=all&format=csv","browser fallback uses the existing filtered CSV route");
  assert.equal(node('[data-purge="Claude"]').disabled,true,"collection on: delete is disabled");
  assert.equal(node('[data-reason="Claude"]').textContent,"Turn off collection first");
  assert.equal(node('[data-reason="Claude"]').hidden,false);
  assert.equal(node('[data-purge="Codex"]').disabled,false,"collection off and ledger on: delete is enabled");
  assert.equal(node('[data-reason="Codex"]').hidden,true);
  node('[data-confirm-purge="Codex"]').onclick({currentTarget:{dataset:{confirmationId:"0"}}});
  await settle();
  assert.equal(sent.length,0,"confirmation from idle is ignored");

  node('[data-purge="Codex"]').onclick();
  const cancelButton=node('[data-cancel-purge="Codex"]'),cancelledId=cancelButton.dataset.confirmationId;
  assert.equal(typeof cancelledId,"string","the confirmation token uses the DOM dataset string type");
  cancelButton.onclick({currentTarget:cancelButton});
  assert.equal(node('[data-purge="Codex"]').focused,true,"Cancel restores focus to the provider's delete action");
  node('[data-confirm-purge="Codex"]').onclick({currentTarget:{dataset:{confirmationId:cancelledId}}});
  await settle();
  assert.equal(sent.length,0,"a stale confirmation after Cancel is ignored");

  node('[data-purge="Codex"]').onclick();
  const ineligibleButton=node('[data-confirm-purge="Codex"]'),ineligibleId=ineligibleButton.dataset.confirmationId;
  assert.equal(typeof ineligibleId,"string");
  currentLedger={...ledgerState,collect:{Claude:true,Codex:true}};
  ineligibleButton.onclick({currentTarget:ineligibleButton});
  await settle();
  assert.equal(sent.length,0,"eligibility is checked again immediately before deletion");
  currentLedger=ledgerState;ledgerTest.state.ledger=ledgerState;ledgerTest.state.purge.Codex={phase:"idle"};ledgerTest.render();

  node('[data-purge="Codex"]').onclick();
  await settle();
  assert.equal(node('[data-confirm="Codex"]').hidden,false);
  assert.equal(sent.length,0,"the first click sends nothing");
  const confirmButton=node('[data-confirm-purge="Codex"]'),confirmId=confirmButton.dataset.confirmationId;
  assert.equal(typeof confirmId,"string","Delete receives the DOM dataset string type");
  confirmButton.onclick({currentTarget:confirmButton});
  confirmButton.onclick({currentTarget:confirmButton});
  await settle();
  assert.deepEqual(sent,[{url:"/api/ledger/purge",method:"POST",token:"ledger-test-token",body:{provider:"Codex"}}]);
  assert.equal(node('[data-status="Codex"]').textContent,"Deleted");
  ledgerState.state="disabled";
  node("#refresh").onclick();
  await settle();
  assert.equal(node('[data-reason="Codex"]').textContent,"Ledger is off");
  assert.equal(node('[data-purge="Codex"]').disabled,true);
  assert.equal(sent.length,1,"nothing is sent while the ledger is off");
}

// Embedded policy failures read as Unavailable with an inline Retry; a known missing prerequisite reads Off.
{
  const nodes=new Map(),node=selector=>{if(!nodes.has(selector))nodes.set(selector,{value:"",textContent:"",innerHTML:"",hidden:false,disabled:false,checked:false,classList:{toggle(){}},addEventListener(type,fn){this[`on${type}`]=fn;}});return nodes.get(selector);};
  let policyAttempts=0,actionReads=0;
  const embeddedPolicy={schemaVersion:1,generation:"off",providers:[{name:"Codex",collect:false,show:true,send:false,sendOrigin:"default",capabilities:{greet:false,continue:false}}]};
  const embeddedDoc={querySelector:node,getElementById:id=>node(`#${id}`),querySelectorAll:()=>[],addEventListener(){},documentElement:{classList:{add(){}}},body:{classList:{add(){}},dataset:{}}};
  const embeddedWindow={location:{hash:"",pathname:"/reset_actions.html",search:"?embedded=1",origin:"http://localhost"},parent:{postMessage(){}},history:{replaceState(){}}};
  const embeddedRequests=[];
  const embeddedContext={document:embeddedDoc,window:embeddedWindow,AbortSignal:{timeout(){}},URLSearchParams,setTimeout,clearTimeout,fetch:async url=>{
    embeddedRequests.push(url);
    if(url==="/api/provider-policy"){policyAttempts++;if(policyAttempts===1)throw new Error("policy offline");return {ok:true,json:async()=>embeddedPolicy};}
    if(url==="/api/reset-action"){actionReads++;return {ok:true,json:async()=>({ok:true,action:null,threads:[]})};}
    if(url==="/api/claude-collection")return {ok:true,json:async()=>({ok:true,state:"off"})};
    if(url==="/api/ledger/status")return {ok:true,json:async()=>({ok:true,state:"disabled",collect:{}})};
    if(url==="/api/features")return {ok:true,json:async()=>({ok:true})};
    if(url==="/api/statusline")return {ok:true,json:async()=>({ok:true,connected:false})};
    return {ok:false,status:404,json:async()=>({})};
  }};
  vm.createContext(embeddedContext);vm.runInContext(source,embeddedContext);
  assert.equal(node("#embedded-policy-message").textContent,"Checking reset permissions…");
  assert.equal(node("footer").hidden,true,"footer is hidden while permissions load");
  assert.equal(node("#message").hidden,true,"loading copy is shown once");
  assert.equal(node("#greet-help").textContent,"");
  assert.equal(node("#continue-help").textContent,"");
  const settle=async()=>{for(let i=0;i<8;i++)await new Promise(resolve=>setTimeout(resolve,0));};
  await settle();
  assert.equal(node("#embedded-policy-message").textContent,"Couldn't load reset permissions.");
  for(const selector of ["#modes","#oneshot-note","#message","footer"])assert.equal(node(selector).hidden,true,`${selector} hidden after policy failure`);
  assert.equal(node("#greet-help").textContent,"");assert.equal(node("#continue-help").textContent,"");
  assert.equal(node("#embedded-policy-retry").hidden,false);
  assert.equal(actionReads,0,"unknown permission state never loads reset actions as Off");
  node("#embedded-policy-retry").onclick();await settle();
  assert.equal(node("#embedded-policy-message").textContent,"Off: turn on collection and greeting for Codex in startup settings.","several missing permissions are named once");
  assert.equal(node("#embedded-policy-retry").hidden,true);
  assert.equal(actionReads,0,"known Off prerequisite prevents action settings from loading");
  const beforeRefresh=embeddedRequests.length,policiesBefore=policyAttempts;
  node("#refresh").onclick();await settle();
  assert.equal(policyAttempts,policiesBefore+1,"Refresh makes one permission request");
  assert.equal(embeddedRequests.slice(beforeRefresh).filter(url=>url==="/api/ledger/status").length,1,"Refresh makes one coordinated ledger request");
  assert.equal(embeddedRequests.slice(beforeRefresh).filter(url=>url==="/api/claude-collection").length,1,"Refresh makes one coordinated Claude status request");
}


// Deny state: one policy line names the one permission that is off; the radios carry no help text and the action notes stay hidden.
// Continue-only blocks keep greet selectable with no denial text. Auto-send off also blocks greet, so no "Needs" line is reachable here.
{
  const nodes=new Map(),node=selector=>{if(!nodes.has(selector))nodes.set(selector,{value:"",textContent:"",innerHTML:"",hidden:false,disabled:false,classList:{toggle(){}},addEventListener(type,fn){this[`on${type}`]=fn;}});return nodes.get(selector);};
  const radios=[{value:"greet",checked:true,disabled:false},{value:"continue",checked:false,disabled:false}];
  let policy={schemaVersion:1,generation:"auto-send-off",providers:[{name:"Codex",collect:true,show:true,send:false,sendOrigin:"default",capabilities:{greet:true,continue:true}}]};
  const doc={querySelector:selector=>selector==='input[name="mode"]:checked'?radios.find(r=>r.checked):selector==='input[name="mode"][value="greet"]'?radios[0]:selector==='input[name="mode"][value="continue"]'?radios[1]:node(selector),getElementById:id=>node(`#${id}`),querySelectorAll:()=>[],addEventListener(){},documentElement:{classList:{add(){}}},body:{classList:{add(){}},dataset:{}}};
  const win={__RESET_ACTIONS_TEST__:true,location:{hash:"",pathname:"/reset_actions.html",search:"?embedded=1",origin:"http://localhost"},parent:{postMessage(){}},history:{replaceState(){}}};
  const ctx={document:doc,window:win,AbortSignal:{timeout(){}},URLSearchParams,setTimeout,clearTimeout,fetch:async url=>({ok:true,json:async()=>url==="/api/provider-policy"?policy:url==="/api/reset-action"?{ok:true,action:{schemaVersion:2,mode:"greet",threadIds:[],armed:false,oneShot:true,lastRun:null},threads:[],threadsAvailable:true}:{ok:true}})};
  vm.createContext(ctx);vm.runInContext(source,ctx);
  const settle=async()=>{for(let i=0;i<8;i++)await new Promise(resolve=>setTimeout(resolve,0));};
  await settle();
  assert.equal(node("#embedded-policy-message").textContent,"Off: greeting is off for Codex in startup settings.","one missing permission is named alone");
  assert.equal(node("#embedded-policy-state").hidden,false);
  assert.equal(radios[0].disabled,true,"greet is disabled when both actions are denied");
  assert.equal(radios[1].disabled,true,"continue is disabled when both actions are denied");
  assert.equal(node("#greet-help").textContent,"","no per-radio help in the deny state");
  assert.equal(node("#continue-help").textContent,"","no per-radio help in the deny state");
  assert.equal(node("#permission").hidden,true);assert.equal(node("#permission").textContent,"","the automatic-actions note is not repeated");
  assert.equal(node("#oneshot-note").hidden,true,"the one-shot note is hidden in the deny state");

  policy={schemaVersion:1,generation:"legacy-origin",providers:[{name:"Codex",collect:true,show:true,send:true,sendOrigin:"legacy_global",capabilities:{greet:true,continue:true}}]};
  await win.__resetActionsTest.load();
  assert.equal(radios[0].disabled,false,"greet stays selectable when only continue is blocked");
  assert.equal(radios[1].disabled,true,"continue is blocked for a legacy send origin");
  assert.equal(node("#greet-help").textContent,"Sends one short request that uses Codex quota and repeats after each eligible reset.","the greeting discloses quota use and repetition");
  assert.equal(node("#continue-help").textContent,"Needs Codex send turned on by environment variable. The policy file allows greetings only.","continue denial distinguishes greeting-only permission");
  assert.equal(node("#permission").textContent,"","no other denial text appears when only continue is blocked");
  assert.equal(node("#embedded-policy-state").hidden,true,"the permitted state shows no policy line");
}

// A reset-action read failure disables every mode and shows its own Retry message.
{
  const nodes=new Map(),node=selector=>{if(!nodes.has(selector))nodes.set(selector,{value:"",textContent:"",innerHTML:"",hidden:false,disabled:false,classList:{toggle(){}},addEventListener(type,fn){this[`on${type}`]=fn;}});return nodes.get(selector);};
  const radios=[{value:"off",checked:false},{value:"greet",checked:false},{value:"continue",checked:false}];
  let failRead=true;
  const policy={schemaVersion:1,generation:"saved-action-read",providers:[{name:"Codex",collect:true,show:true,send:true,sendOrigin:"provider_env",capabilities:{greet:true,continue:true}}]};
  const doc={querySelector:selector=>selector==='input[name="mode"]:checked'?radios.find(r=>r.checked):selector.startsWith('input[name="mode"][value=')?radios.find(r=>selector.includes(`value="${r.value}"`)):node(selector),getElementById:id=>node(`#${id}`),querySelectorAll:()=>[],addEventListener(){},documentElement:{classList:{add(){}}},body:{classList:{add(){}},dataset:{}}};
  const win={location:{hash:"",pathname:"/reset_actions.html",search:"?embedded=1",origin:"http://localhost"},parent:{postMessage(){}},history:{replaceState(){}}};
  const fetch=async url=>{if(url==="/api/provider-policy")return {ok:true,json:async()=>policy};if(url==="/api/reset-action")return failRead?{ok:false,json:async()=>({ok:false,error:"Read failed"})}:{ok:true,json:async()=>({ok:true,action:{schemaVersion:2,mode:"off",threadIds:[],armed:false,oneShot:true,lastRun:null},threads:[],threadsAvailable:true})};return {ok:true,json:async()=>({ok:true})};};
  const ctx={document:doc,window:win,AbortSignal:{timeout(){}},URLSearchParams,setTimeout,clearTimeout,fetch};
  vm.createContext(ctx);vm.runInContext(source,ctx);
  for(let i=0;i<8;i++)await new Promise(resolve=>setTimeout(resolve,0));
  assert.ok(radios.every(radio=>radio.disabled),"saved action read failure disables all modes");
  assert.equal(node("#save").disabled,true,"saved action read failure blocks Save");
  assert.equal(node("#saved-action-load-message").textContent,"Saved action is unavailable.");
  assert.equal(node("#saved-action-retry").hidden,false);
  failRead=false;node("#saved-action-retry").onclick();
  for(let i=0;i<8;i++)await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(radios[0].checked,true,"a valid reload checks the saved Off mode");
  assert.ok(radios.every(radio=>radio.disabled===false),"valid policy and saved state restore only permission-allowed options");
}

// Chat checkboxes update in place: toggling one keeps focus on the same chat and does not rebuild the list.
{
  const nodes=new Map(),node=selector=>{if(!nodes.has(selector))nodes.set(selector,{value:"",textContent:"",innerHTML:"",hidden:false,disabled:false,classList:{toggle(){}},addEventListener(type,fn){this[`on${type}`]=fn;}});return nodes.get(selector);};
  const threads=node("#threads");let html="",renders=0;
  Object.defineProperty(threads,"innerHTML",{get(){return html;},set(value){html=value;renders++;}});
  const listeners={},radios=[{value:"greet",checked:true,disabled:false},{value:"continue",checked:false,disabled:false}];
  const doc={activeElement:null,querySelector:selector=>selector==='input[name="mode"]:checked'?radios.find(r=>r.checked):selector==='input[name="mode"][value="greet"]'?radios[0]:selector==='input[name="mode"][value="continue"]'?radios[1]:node(selector),getElementById:id=>node(`#${id}`),querySelectorAll:()=>[],addEventListener:(type,fn)=>listeners[type]=fn,documentElement:{classList:{add(){}}},body:{classList:{add(){}},dataset:{}}};
  const win={__RESET_ACTIONS_TEST__:true,location:{hash:"",pathname:"/reset_actions.html",search:"?embedded=1",origin:"http://localhost"},parent:{postMessage(){}},history:{replaceState(){}}};
  const policy={schemaVersion:1,generation:"chat-toggle",providers:[{name:"Codex",collect:true,show:true,send:true,sendOrigin:"provider_env",capabilities:{greet:true,continue:true}}]};
  const actionThreads=[{id:"11111111-1111-4111-8111-111111111111",title:"First",status:"idle"},{id:"22222222-2222-4222-8222-222222222222",title:"Second",status:"idle"}];
  const ctx={document:doc,window:win,AbortSignal:{timeout(){}},URLSearchParams,setTimeout,clearTimeout,fetch:async url=>({ok:true,json:async()=>url==="/api/provider-policy"?policy:url==="/api/reset-action"?{ok:true,action:{schemaVersion:2,mode:"greet",threadIds:[],armed:false,oneShot:true,lastRun:null},threads:actionThreads}:{ok:true}})};
  vm.createContext(ctx);vm.runInContext(source,ctx);
  for(let i=0;i<8;i++)await new Promise(resolve=>setTimeout(resolve,0));
  const first={dataset:{id:actionThreads[0].id},checked:false,focus(){doc.activeElement=this;}};
  first.focus();
  const rendersBefore=renders;
  first.checked=true;
  listeners.change({target:{...first,matches:selector=>selector==='#threads input[type="checkbox"]'}});
  assert.equal(renders,rendersBefore,"toggling a chat does not rebuild the list");
  assert.equal(doc.activeElement,first,"focus stays on the same chat after a toggle");
  assert.equal(node("#selected-count").textContent,"1 selected");
  assert.equal(node("#save").disabled,false,"the saved state follows the selection in place");
  first.checked=false;
  listeners.change({target:{...first,matches:selector=>selector==='#threads input[type="checkbox"]'}});
  assert.equal(node("#selected-count").textContent,"0 selected");
  assert.equal(doc.activeElement,first,"focus stays on the chat after it is cleared");
}

// Embedded cancel restores the saved mode without closing the parent; standalone cancel still closes.
{
  const nodes=new Map(),node=selector=>{if(!nodes.has(selector))nodes.set(selector,{value:"",textContent:"",innerHTML:"",hidden:false,disabled:false,classList:{toggle(){}},addEventListener(type,fn){this[`on${type}`]=fn;}});return nodes.get(selector);};
  const radios=[{value:"greet",checked:true},{value:"continue",checked:false}];
  const listeners={},posts=[];
  const doc={querySelector:selector=>selector==='input[name="mode"]:checked'?radios.find(r=>r.checked):selector==='input[name="mode"][value="greet"]'?radios[0]:selector==='input[name="mode"][value="continue"]'?radios[1]:node(selector),getElementById:id=>node(`#${id}`),querySelectorAll:()=>[],addEventListener:(type,fn)=>listeners[type]=fn,documentElement:{classList:{add(){}}},body:{classList:{add(){}},dataset:{}}};
  const win={__RESET_ACTIONS_TEST__:true,location:{hash:"",pathname:"/reset_actions.html",search:"?embedded=1",origin:"http://localhost"},parent:{postMessage:message=>posts.push(message)},history:{replaceState(){}}};
  const policy={schemaVersion:1,generation:"on",providers:[{name:"Codex",collect:true,show:true,send:true,sendOrigin:"provider_env",capabilities:{greet:true,continue:true}}]};
  const ctx={document:doc,window:win,AbortSignal:{timeout(){}},URLSearchParams,setTimeout,clearTimeout,fetch:async url=>({ok:true,json:async()=>url==="/api/provider-policy"?policy:url==="/api/reset-action"?{ok:true,action:{schemaVersion:2,mode:"greet",threadIds:[],armed:false,oneShot:true,lastRun:null},threads:[]}:{ok:true}})};
  vm.createContext(ctx);vm.runInContext(source,ctx);
  for(let i=0;i<8;i++)await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(node("footer").hidden,true,"saved mode hides footer");
  assert.equal(node("#save").textContent,"Save");
  assert.equal(node("#greet-help").textContent,"Sends one short request that uses Codex quota and repeats after each eligible reset.");
  assert.equal(node("#oneshot-note").hidden,true,"one-shot text is absent in greeting mode");
  radios[0].checked=false;radios[1].checked=true;
  listeners.change({target:{matches:selector=>selector==='input[name="mode"]'}});
  assert.equal(node("footer").hidden,false,"changed mode shows footer");
  assert.equal(node("#oneshot-note").textContent,"Queues ‘continue’ once at the next detected reset. A queued chat has not necessarily started.");
  posts.length=0;node("#cancel").onclick();
  assert.equal(radios[0].checked,true,"cancel restores saved radio");
  assert.equal(node("footer").hidden,true);assert.equal(posts.length,0,"cancel sends no parent message");
  let closed=false;plainWindow.close=()=>{closed=true;};element("#cancel").onclick();assert.equal(closed,true,"standalone cancel closes its window");
}

assert.match(resetCss,/\.mode:has\(input:disabled\) b,\.mode:has\(input:disabled\) small\{color:#8d929c;\}/,"disabled option label and description share disabled ink");

// Purge results are per provider and per ticket: Deleted needs an explicit matching done result. Polls are shortened here.
{
  const realTimeout=setTimeout;
  const pause=()=>new Promise(resolve=>realTimeout(resolve,0));
  const settle=async()=>{for(let i=0;i<20;i++)await pause();};
  const until=async(predicate,limit=200000)=>{for(let i=0;i<limit&&!predicate();i++)await pause();};
  const harness=(initialStatus,posts)=>{
    const nodes=new Map();
    const node=(selector)=>{if(!nodes.has(selector))nodes.set(selector,{value:"",textContent:"",innerHTML:"",hidden:false,disabled:false,dataset:{},classList:{toggle(){}},querySelector:child=>node(child),addEventListener(type,fn){this[`on${type}`]=fn;}});return nodes.get(selector);};
    const statusRef={value:initialStatus};
    const requests=[];
    const ctx={document:{querySelector:node,querySelectorAll:()=>[],addEventListener(){}},
      window:{__RESET_ACTIONS_TEST__:true,location:{hash:"#t=ledger-test-token",pathname:"/reset_actions.html",search:""},history:{replaceState(){}}},
      AbortSignal:{timeout(){}},URLSearchParams,clearTimeout,
      setTimeout:(fn,ms,...rest)=>realTimeout(fn,ms===2000?50:ms,...rest),
      fetch:async(url,options={})=>{
        if(url==="/api/ledger/status")return {ok:true,status:200,json:async()=>statusRef.value};
        if(url==="/api/ledger/purge"){const provider=JSON.parse(options.body).provider;requests.push(provider);const reply=posts[provider]();return {ok:reply.status<300,status:reply.status,json:async()=>reply.body};}
        return {ok:false,status:404,json:async()=>({ok:false,error:"not used"})};
      }};
    vm.createContext(ctx);vm.runInContext(source,ctx);
    return {node,statusRef,test:ctx.window.__resetActionsTest,requests};
  };
  const idle={ok:true,state:"live",purging:[],lastPurge:{},collect:{Claude:false,Codex:false}};
  const sendTwice=async(h,provider)=>{h.node(`[data-purge="${provider}"]`).onclick();await settle();assert.equal(h.node(`[data-confirm="${provider}"]`).hidden,false,"inline confirmation is shown");const confirm=h.node(`[data-confirm-purge="${provider}"]`),id=confirm.dataset.confirmationId;assert.equal(typeof id,"string","the registered delete listener receives the DOM string token");const before=h.requests.length;confirm.onclick({currentTarget:confirm});confirm.onclick({currentTarget:confirm});await settle();assert.equal(h.requests.length,before+1,"duplicate registered clicks consume one confirmation");};

  // Sol's replay: Claude's purge fails and Codex's succeeds afterwards. Claude must never read as Deleted.
  const A=harness({...idle,purging:["Claude"]},{
    Claude:()=>({status:202,body:{ok:true,pending:true,ticket:"claude-ticket"}}),
    Codex:()=>({status:200,body:{ok:true,ticket:"codex-ticket"}})});
  await settle();
  await sendTwice(A,"Claude");
  assert.equal(A.node('[data-status="Claude"]').textContent,"Still deleting…");
  A.statusRef.value={ok:true,state:"live",purging:[],collect:{Claude:false,Codex:false},lastPurge:{
    Claude:{ticket:"claude-ticket",result:"failed",error:"RuntimeError"},
    Codex:{ticket:"codex-ticket",result:"done",error:null}}};
  await until(()=>A.node('[data-status="Claude"]').textContent!=="Still deleting…");
  assert.equal(A.node('[data-status="Claude"]').textContent,"Saved history could not be deleted.");
  await sendTwice(A,"Codex");
  assert.equal(A.node('[data-status="Codex"]').textContent,"Deleted");
  assert.equal(A.node('[data-status="Claude"]').textContent,"Saved history could not be deleted.","Codex's success never changes Claude's row");

  // A result for another ticket, or none at all, is not success: the row stays pending, then reads unknown.
  const B=harness({...idle,purging:[],lastPurge:{Claude:{ticket:"stale-ticket",result:"done",error:null}}},{
    Claude:()=>({status:202,body:{ok:true,pending:true,ticket:"claude-ticket"}})});
  await settle();
  await sendTwice(B,"Claude");
  await until(()=>B.node('[data-status="Claude"]').textContent!=="Still deleting…");
  assert.equal(B.node('[data-status="Claude"]').textContent,"Status unknown — reopen to check");
  assert.notEqual(B.node('[data-status="Claude"]').textContent,"Deleted");
}
