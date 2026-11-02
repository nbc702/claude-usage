# Where each number comes from

Checked against Claude Code 2.1.316 (the binary itself) on 2026-11-02.

| Metric | Source | Reliability |
|---|---|---|
| % used / % remaining per window | `GET https://api.anthropic.com/api/oauth/usage` | Undocumented. Same call Claude Code's `/usage` makes, so it matches what you see there. Can change without notice. |
| Which windows exist (session, weekly, per-model weekly) | Same endpoint: the `limits[]` array (`kind` = `session`, `weekly_all`, `weekly_scoped`), falling back to the older `five_hour` / `seven_day` / `seven_day_opus` / `seven_day_sonnet` fields | Same as above. Only windows the server returns are shown; none are assumed. |
| Reset time | Same endpoint (`resets_at`) | Same as above. |
| Extra-usage spend this period, monthly cap | Same endpoint (`extra_usage.used_credits`, `monthly_limit`, in cents) | Same as above. |
| Prepaid credit balance | Not available. The usage endpoint has no balance field and no documented API exposes one. | Shown as "unavailable". |
| Plan name (Pro/Max) | `subscriptionType` in Claude Code's stored sign-in | Reliable; written by Claude Code at login. |
| Burn rate, time-to-limit | Computed: change since your last run in the same window, or average since the window opened | Estimate, labelled as such. Assumes usage continues at that pace. |
| Spend by day | Computed from differences between runs in `history.json` | Estimate. Only as granular as how often you run it. Assumes the billing period is the calendar month. |

**Not used, and why**
- **Absolute limits (messages or tokens).** Anthropic publishes none for subscription plans and the endpoint returns percentages only, so the dashboard shows percentages.
- **`~/.claude/projects/*.jsonl` logs.** They record per-message token counts for Claude Code only, not claude.ai web, desktop or mobile, and carry no limit or reset data. They can't back a plan-wide number.
- **Documented Anthropic APIs.** The Admin Usage & Cost API covers API-key (Console) billing for organizations, not Pro/Max/Team plan limits. No documented endpoint covers plan usage.

**Auth.** The script reads the OAuth token Claude Code stored (macOS Keychain item `Claude Code-credentials`, else `~/.claude/.credentials.json`), keeps it in memory for the one request, and never writes it out. It does not refresh an expired token itself, since that would rotate the token Claude Code depends on; running `claude` once renews it.

**When it breaks.** All endpoint-specific code is in `fetch_plan_usage()` (request) and `parse_windows()` / `parse_extra()` (response shape) in `claude_usage.py`.
