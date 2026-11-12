# "Open Bosun from its icon" / "not signed in"

**Looks like:** the browser shows a page titled "Open Bosun from its icon", or a request is answered
`401 not signed in: open Bosun from its icon, or run bosun open`. An agent sees `invalid agent credential`.

**Why:** the dashboard only answers this user. Every request needs a session cookie, and the only way to get
one is a login code made from `session.key` in Bosun's data folder, a file only this account can read. The
code works once, within two minutes. An agent (`bosun mcp`) sends a different value made from the same file.

**Fix for a browser:** open Bosun from its app icon, or run `uv run bosun open` in its folder. That browser is
let in from then on. A bookmark of the address alone works only in a browser that has been let in before.

**When it comes back:**
- `session.key` was deleted or replaced: every browser must be let in again, the same way.
- A different browser or profile: each one is let in separately.
- A second Bosun on another port has its own cookie; opening one does not sign you in to the other.

**For an agent:** the MCP server reads the key file itself, so this error means it runs as a different user
than the dashboard, or with a different `BOSUN_DATA_DIR`. Run both as the same user with the same data folder.
Never copy the key, the cookie or the agent credential anywhere, and never put them in a command line.
