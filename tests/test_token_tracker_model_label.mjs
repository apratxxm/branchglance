import assert from "node:assert/strict";
import fs from "node:fs";

// Load runwayModelLabel from the shipped app.js without running the rest of the page.
const source = fs.readFileSync(new URL("../token_tracker/static/app.js", import.meta.url), "utf8");
const body = source.match(/function runwayModelLabel\(chat\) \{[\s\S]*?\n\}/)?.[0];
assert.ok(body, "runwayModelLabel found in app.js");
const runwayModelLabel = new Function("isAutoReview", body.replace(/^function runwayModelLabel\(chat\)/, "return function runwayModelLabel(chat)") + ";")(() => false);
const label = model => runwayModelLabel({model});

// Long-context tier: base model plus a tier note, never "Context" as a family.
assert.equal(label("gpt-6.1|long-context"), "GPT-6.1 · long context", "server pipe-suffixed tier key");
assert.equal(label("gpt-6.1-long-context"), "GPT-6.1 · long context", "hyphenated tier key");
assert.equal(label("gpt-6.1-sol|long-context"), "GPT-6.1 Sol · long context", "model family kept before tier");
assert.equal(label("gpt-6-luna|long-context"), "GPT-6 Luna · long context");
assert.equal(label("gpt-6-long-context"), "GPT-6 · long context");
for (const raw of ["gpt-6.1|long-context", "gpt-6.1-sol|long-context", "gpt-6-luna|long-context", "gpt-6.1-long-context"]) {
  assert.doesNotMatch(label(raw), /^GPT-[\d.]+ Context$/, `no bare Context family for ${raw}`);
}

// Unchanged labels: base models without a tier keep their existing output.
assert.equal(label("gpt-6.1-terra"), "GPT-6.1 Terra");
assert.equal(label("gpt-6.1-sol"), "GPT-6.1 Sol");
assert.equal(label("gpt-6-luna"), "GPT-6 Luna");
assert.equal(label("claude-opus-5-5"), "Opus 5.5");
assert.equal(label("claude-opus-4-1-20250805"), "Opus 4.1");
assert.equal(label("codex"), "");
assert.equal(label(""), "");
assert.equal(label("some-unknown-model"), "some-unknown-model");

console.log("Model label contracts passed");
