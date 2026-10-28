# Insights and Settings mockup review notes

These are static design studies. Values are illustrative and do not represent a live account.

The displayed sample values include 46 sessions, 18 subagents, and 62 active hours; these are placeholders for visual review. They remain listed as **needs data** below. The cost and token series are synthetic.

## Backend coverage

| Panel or metric | Current source support | Review note |
| --- | --- | --- |
| Daily usage chart and heatmap | Per-provider `activity` contains daily date/value rows, with optional model breakdowns | Available for the recorded 31-day range. The mock chart is synthetic. |
| Tokens over 31 days | `tracked31Days` | Available as a provider total. |
| Lifetime tokens | `lifetimeTokens` | Available, though omitted from the requested compact stat strip. |
| Model breakdown | Aggregate `modelUsage` | Available as a model mix, but does not provide per-day model splits. Mock tokens/cost/share figures are illustrative. |
| Cache hit rate and savings | `cache.percent`, `cache.apiEquivalentCost`, `cache.cacheSavings` | Available as aggregate provider values; no historical daily cost series. |
| Current five-hour and weekly usage | `fiveHour` and `weekly` | Current values are available. Historical period peak usage / peak sparklines need data. |
| Limit reset events | `/api/limit-reset`, recent events | Recent events are available, but they do not establish historical limit peaks. |
| Sessions | No period session count in the dashboard data | **Needs data**: session count by selected period. |
| Subagents spawned | Codex exposes `subagentRequests`; no distinct spawned-session count or corresponding Claude metric | **Needs data**: consistent provider-level spawned subagent count. |
| Active hours | No hourly activity series | **Needs data**: active hours in the selected period. |
| Period cost comparison / delta | No previous-period aggregate or daily cost series | **Needs data**: previous-period cost and comparable daily cost. |
| All-time chart | Daily activity is limited to 31 days | **Needs data**: unbounded/long-range daily series. |

The model names and cost allocations in the table are placeholder values for layout review, not claims that those exact model labels or per-model costs are returned by the current API. Provider-level observed model names and aggregate model token counts are the available source.

## Files

- `insights.html`: default Insights treatment.
- `insights-hover.html`: chart crosshair and tooltip state.
- `insights-empty.html`: no-activity state with Claude selected.
- `settings.html`: grouped settings, including a focused select state.
