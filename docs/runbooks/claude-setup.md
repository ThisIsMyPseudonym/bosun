# Setting up Claude Code for Bosun

**Install:** install Claude Code (`npm install -g @anthropic-ai/claude-code` or the native installer) so `claude` is on `PATH`.

**Sign in:** run `claude` in a terminal and type `/login`. On macOS, sign in inside your desktop session so `claude` can read the login keychain. Bosun unsets `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_BASE_URL`, and cloud-backend variables so Bosun uses your subscription login rather than billing an API key.

**Add a login folder:** to pool a second Claude subscription, create a folder and sign in with `CLAUDE_CONFIG_DIR`:

```sh
mkdir -p ~/.claude-work
CLAUDE_CONFIG_DIR=~/.claude-work claude
```

Type `/login`, then add `~/.claude-work` in the dashboard (`add_ai_login`). Bosun never opens files inside that folder; it only passes `CLAUDE_CONFIG_DIR` to `claude`.

**What fails closed:** before sending any prompt, Bosun checks `claude --version` and `claude --help`. Every required lockdown flag (`-p`, `--tools`, `--disallowedTools`, `--strict-mcp-config`, `--setting-sources`, `--no-session-persistence`, `--json-schema`, `--output-format`, `--verbose`, `--disable-slash-commands`, `--permission-mode`, `--model`, `--system-prompt`) must appear in `--help`. If any flag is missing or `--help` fails, Bosun sends Claude Code no prompt and names the missing flag in `bosun doctor` and the dashboard.
