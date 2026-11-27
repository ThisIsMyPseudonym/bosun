# Setting up Codex for Bosun

**Install:** install OpenAI Codex CLI (`npm install -g @openai/codex` or Homebrew) so `codex` is on `PATH`.

**Sign in:** run `codex login` in a terminal and finish the browser sign-in. Bosun unsets `CODEX_API_KEY`, `OPENAI_API_KEY`, and `OPENAI_BASE_URL` on every call so Bosun uses your subscription login and never bills an API key silently.

**Add a login folder:** to pool another Codex login, sign in with `CODEX_HOME`:

```sh
mkdir -p ~/.codex-work
CODEX_HOME=~/.codex-work codex login
```

Add `~/.codex-work` in the dashboard (`add_ai_login`). Bosun never reads files in that folder; it only sets `CODEX_HOME` when calling `codex`.

**What fails closed:** Bosun checks `codex --version`, `codex exec --help`, and `codex features list`. Every lockdown flag (`--sandbox`, `--ephemeral`, `--skip-git-repo-check`, `--ignore-user-config`, `--ignore-rules`, `--strict-config`, `--disable`, `-c`, `--output-schema`, `--json`) and every disabled feature flag (`shell_tool`, `unified_exec`, `apps`, `browser_use`, `plugins`, `memories`, and others) must be present. If a flag or feature is missing, Bosun sends Codex no prompt and reports it in `bosun doctor`.
