# "Claude Code isn't logged in"

**Looks like:** a refresh's errors say "Claude Code isn't logged in for this session ... so summaries used
built-in rules", or "Will this break?" and Branch + test fail with "Claude Code isn't logged in".

**Why:** Bosun has no API key. It runs the `claude` command as you, and that command must be logged in for the
session Bosun runs in. On macOS the login is in the login keychain, which an SSH session or a background
session before first login can't read.

**Effect:** nothing is lost. Summaries fall back to built-in rules for that refresh (an item's lane may be more
cautious), and Bosun stops calling Claude for the rest of that refresh.

**Fix:** the user runs `claude` in a terminal on that machine and types `/login`. On a Mac, do it in the
desktop session, and keep the dashboard running in that session (the LaunchAgent from `bosun install-tasks`).
Then run `refresh`.

**Check:** `llm_usage` shows calls and their outcomes; `query` over `v_llm_spend` shows when calls stopped.
