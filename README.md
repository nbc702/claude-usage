# Claude usage dashboard
Shows your Claude plan limits (5-hour session, weekly, per-model weekly) and extra-usage credits. Runs only when you start it.

**First time:** put this folder at `~/claude-usage` (`git clone https://github.com/nbc702/claude-usage ~/claude-usage`). Install Claude Code and sign in once (`claude`, then `/login`). The first run asks macOS to let Python read the "Claude Code-credentials" Keychain item: click Always Allow. If macOS blocks the launcher, right-click it and choose Open.

**Run:** double-click `Claude Usage.command`. Terminal equivalent:
`python3 ~/claude-usage/claude_usage.py`

**Files:** the dashboard is `~/claude-usage/dashboard.html` (overwritten each run); raw numbers from each run are in `~/claude-usage/history.json` (last 90 days). Your sign-in token is never written anywhere.

**Delete:** move the `~/claude-usage` folder to the Trash. Nothing else was installed.

How each number is sourced: see `DATA_SOURCES.md`.
