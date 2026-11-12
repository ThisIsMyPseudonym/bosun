# Homebrew upgrade fails with a permission error

**Looks like:** a failed `brew upgrade` whose output says `Permission denied`, `is not writable` or
`Could not symlink`, usually naming a path under `/opt/homebrew` or `/usr/local`.

**Why:** Bosun runs `brew upgrade --formula <name>` or `brew upgrade --cask <name>` as you, never with `sudo`
and never with `--force`. Homebrew expects your account to own its folders. A file there owned by `root` or
another account (left by an earlier `sudo brew`, a pkg installer, or a second user) stops the upgrade.

**Check:** `diagnose` shows the command and the end of its output; the path Homebrew names is the one to look at.
`brew doctor` lists every folder it can't write to and prints the `chown` command to fix them.

**Fix (the user does this, in a terminal):** run the command `brew doctor` prints, then upgrade again from
Bosun. Bosun has no action that changes file ownership, and an agent should not run `sudo` for it.

**Not this:** "needs your password" is a different case, see `cask-needs-password`.
