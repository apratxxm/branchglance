// Turns the "Agent tree" section of a debug report into buildForest input.
// Every value is synthetic: aliases become ids, titles and questions are placeholders, and models are fixed stand-ins.
const TREE_OPEN = "## Agent tree\n\n```json\n";
const PLACEHOLDER_MODEL = {Claude: "claude-opus-4-7", Codex: "gpt-6.1-sol"};
// Validated booleans carried through from the report row. hasPendingQuestion is handled with the question placeholder.
const FLAG_KEYS = ["pendingQuestionExpired", "telemetryUnavailable", "activityUnavailable", "metricsUnavailable",
  "retainedRun"];

export function reportTree(text) {
  const start = text.indexOf(TREE_OPEN);
  if (start < 0) throw new Error("report has no agent tree section");
  const body = text.slice(start + TREE_OPEN.length).split("\n```", 1)[0];
  return JSON.parse(body);
}

export function forestProviders(rows, {now}) {
  const at = seconds => new Date(now + seconds * 1000).toISOString();
  const chats = rows.map(row => {
    const provider = row.provider;
    const chat = {
      id: row.alias,
      title: `Synthetic ${row.alias}`,
      model: PLACEHOLDER_MODEL[provider],
      status: row.status ?? "idle",
      stateDetail: row.stateDetail,
      executionStatus: row.executionStatus,
      executionSource: row.executionSource,
      source: row.kind === "exec" ? "exec" : row.kind === "subagent" ? "subagent" : "cli",
      parentLinkMethod: row.linkSource === "process" ? "cmdline" : row.linkSource === "receipt" ? "receipt" : undefined,
      resumeCount: row.resumeCount,
      updatedAt: at(0),
    };
    if (typeof row.runStartedAt === "number") chat.runStartedAt = at(row.runStartedAt);
    if (typeof row.stoppedAt === "number") chat.stoppedAt = at(row.stoppedAt);
    if (typeof row.firstSeenAt === "number") chat.firstSeenAt = at(row.firstSeenAt);
    if (typeof row.executionObservedAt === "number") chat.executionObservedAt = at(row.executionObservedAt);
    for (const key of FLAG_KEYS) if (typeof row[key] === "boolean") chat[key] = row[key];
    if (row.hasPendingQuestion) chat.pendingQuestion = "Synthetic question";
    if (row.activityKind) chat.activity = {kind: row.activityKind, label: "Synthetic activity"};
    if (row.parent) chat.parentId = row.parent;
    return [provider, chat];
  });
  const names = [...new Set(rows.map(row => row.provider))];
  return names.map(name => ({
    name,
    chats: chats.filter(([provider]) => provider === name).map(([, chat]) => chat),
  }));
}
