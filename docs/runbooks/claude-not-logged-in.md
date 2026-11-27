# "Claude Code isn't logged in"

**Looks like:** a refresh's errors say "Claude Code isn't logged in for this session ... so summaries used built-in rules", or "Will this break?" and Branch + test fail with "Claude Code isn't logged in".

**Why:** Bosun has no API key. It runs `claude` with your subscription login (and unsets `ANTHROPIC_API_KEY`), so that login must be signed in for the session Bosun runs in. On macOS the login lives in the login keychain, which an SSH session or a background session before first login cannot read.

**Effect:** if you have another signed-in AI login configured (such as another Claude folder, Codex, or Gemini CLI), background summaries fail over to it automatically, and the signed-out login leaves the pool for an hour. When no login can answer, summaries fall back to built-in rules for that refresh.

**Fix:** run `claude` in a terminal on that machine and type `/login` (see [`claude-setup.md`](claude-setup.md)). On a Mac, sign in from the desktop session and run the dashboard in that session (the LaunchAgent from `bosun install-tasks`). Then run `refresh`.

**Check:** `llm_usage` shows each login's windows and recent calls; `query` over `v_ai_spend` and `quota_windows` shows when calls stopped and which login ran.
