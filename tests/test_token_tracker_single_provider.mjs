import assert from "node:assert/strict";
import fs from "node:fs";

const root = new URL("../token_tracker/static/", import.meta.url);
const css = fs.readFileSync(new URL("runway_refined.css", root), "utf8");
const html = fs.readFileSync(new URL("runway.html", root), "utf8");
const app = fs.readFileSync(new URL("app.js", root), "utf8");

// Quota bands: a single provider takes the full band width (label column, then one value column; no empty half).
assert.match(css, /#resets\[data-provider-count="1"\],\s*body\.runway-refined\.runway-compact:not\(\.phone\) \.weekly-resets\[data-provider-count="1"\]\{grid-template-columns:54px minmax\(0,1fr\);\}/,
  "one-provider bands use a label column plus one full-width value column");
assert.match(css, /#resets\[data-provider-count="1"\] \.rr,\s*body\.runway-refined\.runway-compact:not\(\.phone\) \.weekly-resets\[data-provider-count="1"\] \.rr\{grid-column:2;\}/,
  "the single provider row occupies the value column");
// Both providers: no override exists for two providers, so the two-column layout is unchanged.
assert.doesNotMatch(css, /data-provider-count="2"/, "two-provider layout has no special-case override");
assert.doesNotMatch(css, /data-provider-count="[2-9]"/, "no count override for two or more providers");

// The band reports how many providers it renders (both single and multi-provider paths).
assert.match(app, /node\.dataset\.providerCount=String\(providers\.length\);if\(weeklyNode\)weeklyNode\.dataset\.providerCount=String\(providers\.length\);/,
  "both bands record the rendered provider count");
// Each band row is rendered only for a provider in the snapshot (no placeholder row for a missing provider).
assert.match(app, /providers\.map\(\(p,i\)=>\{/, "band rows come from the providers actually present");

// Source switch: no switch markup in the page, and it is hidden unless two or more sources are visible.
assert.doesNotMatch(html, /id="source-switch"/, "runway page has no source switch markup");
assert.match(app, /visible=names\.length>=2/, "source switch is visible only with two or more sources");
assert.match(app, /const group=\$\('#source-switch'\);if\(!group\)\{/, "missing switch group returns early");

// Both providers present: the source list still offers both, in fixed order (nothing changes).
assert.match(app, /function sourceNames\(snapshot\) \{ const visible=visibleProviderNames\(snapshot\);return SOURCES\.filter\(name=>visible\.has\(name\)\); \}/,
  "source names are the visible providers in fixed order");

// Empty tree: one quiet line when the visible provider(s) have no chats; nothing changes when chats exist.
const emptyStart = app.indexOf("function emptyHTML(forest, providers) {");
const emptyEnd = app.indexOf("function updateChatEl(", emptyStart);
assert.ok(emptyStart >= 0 && emptyEnd > emptyStart, "emptyHTML found");
const emptyBranch = app.slice(emptyStart, emptyEnd);
assert.ok(emptyBranch.includes("if(parseQuery(state.query).active)return '<div class=\"empty\">No matches</div>'"), "search keeps the compact no-matches state");
assert.ok(emptyBranch.includes("'<div class=\"empty tree-empty\">No active chats</div>'"), "quiet line text, no icon or button");
assert.ok(emptyBranch.includes("'<div class=\"empty\">No data</div>'"), "disconnected provider keeps its No data text");
assert.ok(!emptyBranch.includes("<button"), "compact empty states have no window expansion button");
assert.equal(app.split("tree-empty").length - 1, 1, "single quiet-line branch");
const rule = css.slice(css.indexOf(".chat-list .empty.tree-empty{"));
const ruleBody = rule.slice(0, rule.indexOf("}") + 1);
assert.ok(ruleBody.includes("color:#8d93a8"), "muted colour");
assert.ok(ruleBody.includes("font-size:13px"), "13px");
assert.ok(ruleBody.includes("font-weight:400"), "regular weight");
assert.ok(ruleBody.includes("display:flex") && ruleBody.includes("align-items:center") && ruleBody.includes("justify-content:center"), "centred in the tree area");

console.log("Single-provider contracts passed");
