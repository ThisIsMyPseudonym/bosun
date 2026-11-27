# Setting up Grok for Bosun

**Install:** install the Grok CLI so `grok` is on `PATH`.

**Sign in:** run `grok login` in a terminal. Bosun unsets `XAI_API_KEY` and `GROK_CODE_XAI_API_KEY` on every call so Bosun uses your account login rather than billing an API key.

**Add a login folder:** to pool another Grok login, sign in with `GROK_HOME`:

```sh
mkdir -p ~/.grok-work
GROK_HOME=~/.grok-work grok login
```

Add `~/.grok-work` in the dashboard (`add_ai_login`). Each added Grok login gets its own private leader socket so it never shares the default login's daemon. Bosun never opens files in the login folder.

**What fails closed:** Bosun checks `grok --version` and `grok --help` for every required lockdown flag (`--permission-mode`, `--disable-web-search`, `--tools`, `--disallowed-tools`, `--no-subagents`, `--no-plan`, `--no-memory`, `--verbatim`, `--prompt-file`, `--output-format`, `--json-schema`, `--system-prompt-override`). If any flag is missing, Bosun sends Grok no prompt. Because Grok's tool flags cover built-in tools only and do not switch off MCP servers in `GROK_HOME`, Bosun keeps Grok off jobs that carry untrusted release notes until a per-call MCP switch is verified. Usage is counted as estimated.
