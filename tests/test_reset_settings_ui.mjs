import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source=fs.readFileSync(new URL("../token_tracker/static/reset_actions.js",import.meta.url),"utf8");
const html=fs.readFileSync(new URL("../token_tracker/static/reset_actions.html",import.meta.url),"utf8");
const app=fs.readFileSync(new URL("../token_tracker/static/app.js",import.meta.url),"utf8");
assert.match(html,/id="picker" hidden/);
assert.match(html,/one-shot action for the next genuine quota reset/i);
assert.match(html,/queued chat has not necessarily started/i);
assert.match(app,/data-action="reset-settings"/);
assert.match(app,/api\?\.open_reset_actions/);
assert.match(app,/window\.open\?\.\("\/reset_actions\.html","_blank","noopener"\)/);

const elements=new Map();
function element(selector){if(!elements.has(selector))elements.set(selector,{value:"",textContent:"",innerHTML:"",hidden:false,disabled:false,classList:{toggle(){}},addEventListener(type,fn){this[`on${type}`]=fn;}});return elements.get(selector);}
const radios=[{value:"greet",checked:true,disabled:false},{value:"continue",checked:false,disabled:false}];
const listeners={};
const document={querySelector:selector=>selector==='input[name="mode"]:checked'?radios.find(r=>r.checked):selector==='input[name="mode"][value="greet"]'?radios[0]:selector==='input[name="mode"][value="continue"]'?radios[1]:element(selector),querySelectorAll:()=>[],addEventListener:(type,fn)=>listeners[type]=fn};
const calls=[];
const gets=[];
const requests=[];
let failSave=false;
let policy={schemaVersion:1,generation:"codex-send-v1",providers:[
  {name:"Codex",collect:true,show:true,send:true,sendOrigin:"provider_env",capabilities:{greet:true,continue:true}},
  {name:"Claude",collect:false,show:false,send:false,sendOrigin:"default",capabilities:{greet:false,continue:false}}
]};
let actionRecord={schemaVersion:2,mode:"greet",threadIds:[],armed:false,lastRun:null};
const context={document,window:{__RESET_ACTIONS_TEST__:true,close(){}},AbortSignal:{timeout(){}},fetch:async(url,options={})=>{
  requests.push(url);
  if(url==="/api/provider-policy")return {ok:true,json:async()=>policy};
  gets.push(url);
  if(options.method==="POST"){
    calls.push(JSON.parse(options.body));
    if(failSave)return {ok:false,json:async()=>({error:"Could not save"})};
    return {ok:true,json:async()=>({ok:true,action:{schemaVersion:2,mode:calls.at(-1).mode,threadIds:calls.at(-1).threadIds,armed:true,oneShot:true,lastRun:null}})};
  }
  return {ok:true,json:async()=>({ok:true,action:actionRecord,threads:[
    {id:"11111111-1111-4111-8111-111111111111",title:"<img src=x onerror=alert(1)>",status:"idle"},
    {id:"22222222-2222-4222-8222-222222222222",title:"Busy chat",status:"active"},
    {id:"33333333-3333-4333-8333-333333333333",title:"Archived",status:"archived"}
  ]})};
}};
vm.createContext(context);vm.runInContext(source,context);
await new Promise(resolve=>setTimeout(resolve,0));
const T=context.window.__resetActionsTest;
assert.deepEqual(requests.slice(0,2),["/api/provider-policy","/api/reset-action"],"policy is checked before action settings are fetched");
assert.match(element("#threads").innerHTML,/&lt;img/);
assert.doesNotMatch(element("#threads").innerHTML,/<img/);
radios[0].checked=false;radios[1].checked=true;T.state.selected=new Set(["11111111-1111-4111-8111-111111111111","22222222-2222-4222-8222-222222222222"]);T.render();
assert.equal(element("#selected-count").textContent,"2 selected");
await T.load();
assert.equal(radios[1].checked,true,"refresh preserves the unsaved mode draft");
assert.equal(T.state.selected.size,2,"refresh preserves the unsaved chat selection");
await T.save();
assert.deepEqual(calls[0],{mode:"continue",threadIds:["11111111-1111-4111-8111-111111111111","22222222-2222-4222-8222-222222222222"]});
radios[0].checked=true;radios[1].checked=false;T.render();await T.save();
assert.deepEqual(calls[1],{mode:"greet",threadIds:[]});
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
actionRecord={schemaVersion:1,mode:"continue",threadIds:["11111111-1111-4111-8111-111111111111"],armed:true,lastRun:null};
await T.load();
assert.equal(T.state.selected.size,0,"legacy selections are not re-armed from the server record");
assert.equal(T.state.action.threadIds.length,0);
assert.match(element("#message").textContent,/re-select chats/i);

policy={schemaVersion:1,generation:"codex-hidden",providers:[
  {name:"Codex",collect:false,show:false,send:false,sendOrigin:"default",capabilities:{greet:true,continue:true}},
  {name:"Claude",collect:false,show:false,send:false,sendOrigin:"default",capabilities:{greet:false,continue:false}}
]};
const actionReadsBeforeHidden=gets.length;
await T.load();
assert.equal(gets.length,actionReadsBeforeHidden,"hidden Codex settings are not fetched");
assert.equal(T.state.threads.length,0);
