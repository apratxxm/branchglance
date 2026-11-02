import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

// By work: ledger rows become a model x kind matrix whose cells, rows and columns add up to the ledger totals.
const source=fs.readFileSync(new URL("../token_tracker/static/dashboard.js",import.meta.url),"utf8");
const values=new Map();
const localStorage={getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,String(value))};
const document={querySelector(){return null;},querySelectorAll(){return [];},addEventListener(){}};
const window={__TOKEN_TRACKER_DASHBOARD_TEST__:true,addEventListener(){},TokenTrackerFormatModel:({model})=>String(model).replace(/^claude-(opus|sonnet|haiku)-(\d+)-(\d+)$/i,(_,f,a,b)=>f[0].toUpperCase()+f.slice(1)+" "+a+"."+b)};
let responses=[];
const context={window,document,Intl,Date,Math,Number,JSON,Map,Set,Array,Promise,localStorage,location:{origin:"http://127.0.0.1:8765"},fetch:async()=>responses.shift()||{ok:false,status:503,json:async()=>({})}};
vm.createContext(context);vm.runInContext(source,context);
const ui=window.__tokenTrackerDashboardTest;
const row=(provider,modelId,kind,tokens,cost=tokens/1e6,extra={})=>({provider,modelId,displayName:modelId,kind,totalTokens:tokens,knownCostUsd:cost,costComplete:true,unpricedTokens:0,...extra});
const ledger=rows=>({rows,coverage:{status:"complete"},service:{state:"live"},ledgerRevision:1});

ui.state.range="30d";ui.state.ledgerRange="30d";
ui.state.ledger=ledger([
  row("Claude","claude-opus-5-5","build",600),row("Claude","claude-opus-5-5","lead",400),
  row("Codex","gpt-6.1-sol","review",500),row("Codex","gpt-6.1-terra","build",300),
  row("Claude","claude-haiku-5-5","debug",50),row("Codex","unknown","other",20,null,{costComplete:false,unpricedTokens:20}),
  row("Claude","unknown","other",10,null,{costComplete:false,unpricedTokens:10}),
]);
const work=ui.workData();
assert.equal(work.rows.length,4,"top three plus Other");
assert.equal(work.rows[0].name,"Opus 5.5");
assert.equal(work.rows[3].other,true);
assert.match(work.rows[3].name,/Other · 3 models/);
assert.ok(!work.kinds.includes("debug"),"unknown kinds fold into Other");
assert.ok(work.kinds.includes("other"));
const grand=1880;
assert.equal(work.totals.all.tokens,grand);
for(const r of work.rows)assert.equal(r.sum.tokens,work.kinds.reduce((t,k)=>t+(r.cells[k]?.tokens||0),0));
assert.equal(work.kinds.reduce((t,k)=>t+work.totals.cells[k].tokens,0),grand,"columns add up");
assert.equal(work.rows.reduce((t,r)=>t+r.sum.tokens,0),grand,"rows add up");
// The Other row mixes priced and unpriced usage: its cost is a lower bound, never complete.
const otherCell=work.rows[3].cells.other;
assert.equal(otherCell.unpriced,true);
const html=ui.workHTML(work);
assert.match(html,/≥ \$0\.00 API-equivalent|No published price/);
assert.doesNotMatch(html,/dash-work-foot/);

// Same name under two providers gets the provider appended, when both are shown.
ui.state.ledger=ledger([row("Claude","unknown","build",5,null,{costComplete:false}),row("Codex","unknown","build",4,null,{costComplete:false})]);
const twins=ui.workData();
assert.equal(JSON.stringify(twins.rows.map(r=>r.name).sort()),JSON.stringify(["unknown · Claude","unknown · Codex"]));
assert.match(ui.workHTML(twins),/No published price/);

// Coverage short of complete is always qualified, with its causes.
assert.equal(ui.workNote({coverage:{status:"complete"},service:{state:"live"}}),null);
assert.equal(ui.workNote({coverage:{status:"catching-up"},service:{}}).text,"Catching up");
assert.equal(ui.workNote({coverage:{status:"verifying"},service:{}}).text,"Verifying");
assert.equal(ui.workNote({coverage:{status:"complete"},service:{state:"error"}}).text,"Ledger paused");
const partial=ui.workNote({coverage:{status:"partial",gapCount:0,unknownUsageRecords:8,readFailures:0,providersNotCollected:[]},service:{}});
assert.equal(partial.text,"Partial · 8 turns without usage");

// Another range's ledger is never shown; an empty or failed ledger hides the view.
ui.state.ledgerRange="24h";assert.equal(ui.workData(),null);
ui.state.ledgerRange="30d";ui.state.ledger=ledger([]);assert.equal(ui.workData(),null);
responses=[{ok:false,status:503,json:async()=>({ok:false})}];
ui.state.ledger=ledger([row("Claude","claude-opus-5-5","build",1)]);
await ui.loadLedger("30d");
assert.equal(ui.state.ledger,null,"a 503 drops the ledger view");
responses=[{ok:true,status:200,json:async()=>ledger([row("Claude","claude-opus-5-5","build",7)])}];
await ui.loadLedger("30d");
assert.equal(ui.workData().totals.all.tokens,7);
// A bucket ageing out of a rolling range clears the view even when the ledger revision is unchanged.
responses=[{ok:true,status:200,json:async()=>({...ledger([row("Claude","claude-opus-5-5","build",100)]),ledgerRevision:5})}];
await ui.loadLedger("30d");
assert.equal(ui.workData().totals.all.tokens,100);
responses=[{ok:true,status:200,json:async()=>({...ledger([]),ledgerRevision:5})}];
await ui.loadLedger("30d");
assert.equal(ui.workData(),null,"an empty response with the same revision clears the matrix");
console.log("dashboard by-work ok");