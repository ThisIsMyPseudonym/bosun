# Setting up Muse for Bosun

**Install:** install the Muse CLI so `muse` is on `PATH`.

**Sign in:** run `muse login` in a terminal. Bosun unsets `META_API_KEY` on every call because `META_API_KEY` overrides the account login.

**Add a login folder:** Muse holds one login per machine. Bosun cannot add a second login folder for it; only the default login is available.

**What fails closed:** Bosun checks `muse --version` and `muse exec --help` for every required lockdown flag (`--provider`, `--json`, `--prompt-file`, `--workspace`, `--disable-shell`, `--disable-write`, `--disable-web-tools`, `--no-session-log`, `--no-foreign-personal-context`, `--disable-request-user-input`, `--output-schema`, `--model`). If any flag is missing, Bosun sends Muse no prompt. Because Muse has no per-call flag to disable user MCP servers, Bosun keeps Muse off jobs carrying untrusted text until one is verified. Usage is counted as estimated.
