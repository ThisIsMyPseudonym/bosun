# A cask, Mac app or Linux package needs the user's password

**Looks like:** the `upgrade` tool answers `<name> needs your password: run it yourself (<command>)`, or a job
fails with "no password was provided", "a password is required", "User cancelled", "Request dismissed",
"Not authorized" or "No authentication agent found".

**Why:** some updates install with admin rights (pkg installers, privileged helpers, `apt`, `snap`). Bosun can
show the system password window (macOS: a standard dialog through `SUDO_ASKPASS`; Linux: polkit through
`pkexec`), but only when a person clicked Update in the dashboard, in their logged-in desktop session. An agent,
Autopilot and the daily refresh never get a password window: nobody may be there to answer it. Bosun never
sees, stores or logs the password.

**Fix:** ask the user to click Update on that item in the dashboard and answer the password window, or to run
the command shown in the item's details themselves. Do not retry from an agent: it fails the same way, and
Autopilot stops retrying these for the same reason.

**If the window never appears:** on macOS the dashboard must run in the logged-in session (the LaunchAgent from
`bosun install-tasks`), not over SSH. On Linux a polkit agent must be running in the desktop session.
