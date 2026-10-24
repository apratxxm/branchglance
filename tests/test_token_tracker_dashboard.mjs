import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source=fs.readFileSync(new URL("../token_tracker/static/dashboard.js",import.meta.url),"utf8");
const document={querySelector(){return null;},addEventListener(){}};
const window={__TOKEN_TRACKER_DASHBOARD_TEST__:true,addEventListener(){}};
const context={window,document,Intl,Date,Math,Number,JSON,location:{origin:"http://127.0.0.1:8765"},fetch:async()=>({ok:true,json:async()=>({events:[]})})};
vm.createContext(context);vm.runInContext(source,context);
const dashboard=window.__tokenTrackerDashboardTest;
const dayOffset=days=>{const date=new Date();date.setHours(12,0,0,0);date.setDate(date.getDate()-days);return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,"0")}-${String(date.getDate()).padStart(2,"0")}`;};

const rows=dashboard.dailySeries({activity:[{date:dayOffset(0),value:900},{date:dayOffset(1),value:400}]});
assert.equal(rows.length,31);
assert.equal(rows.at(-1).value,900);
assert.equal(rows.at(-2).value,400);
const streak=dashboard.streaks(rows);
assert.equal(streak.current,2);
assert.ok(streak.longest>=2);

const chart=dashboard.dailyChart([{date:"2026-10-24",value:10},{date:"2026-10-23",value:2,models:{"model <unsafe>":2}}]);
assert.match(chart,/aria-label="Daily tokens over the last 31 days"/);
assert.match(chart,/usage-stack/);
assert.match(chart,/dash-crosshair/);assert.match(chart,/rx="4"/);
assert.doesNotMatch(chart,/<script>/);
assert.match(dashboard.heatmap(rows),/is-today/);

dashboard.state.source="Claude";
dashboard.state.snapshot={generatedAt:"2026-10-24T00:00:00Z",providers:[{
  name:"Claude",tracked31Days:1200,lifetimeTokens:2400,currentStreakDays:2,longestStreakDays:4,
  activity:[{date:dayOffset(0),value:1200}],cache:{percent:42,apiEquivalentCost:1.23,cacheSavings:.45},
  modelUsage:[{model:"Claude Sonnet",tokens:1200}],
}]};
const insights=dashboard.insightsHTML();
assert.match(insights,/Last 31 days/);
assert.match(insights,/dash-hero-number/);assert.match(insights,/dash-sparkline/);
assert.match(insights,/Claude Sonnet/);
assert.match(insights,/\$1\.23/);
assert.match(insights,/Activity heatmap|Activity/);
assert.equal((insights.match(/class="dash-card dash-secondary-stat"/g)||[]).length,3);

dashboard.state.policy={origin:"policy_file",policyFile:"D:\\work\\sample-app\\policy.json",warnings:[],providers:[
  {name:"Codex",show:true,collect:true,send:false},{name:"Claude",show:true,collect:false,send:false},
]};
const policy=dashboard.policyHTML();
assert.match(policy,/Provider permissions/);
assert.match(policy,/Read only/);
assert.match(policy,/D:\\work\\sample-app\\policy\.json/);
assert.match(policy,/collect/);
assert.match(policy,/send/);

console.log("Token Tracker dashboard contracts passed");
