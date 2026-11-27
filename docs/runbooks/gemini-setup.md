# Setting up Gemini CLI for Bosun

**Install:** install Gemini CLI (`npm install -g @google/gemini-cli` or Homebrew) so `gemini` is on `PATH`.

**Sign in:** run `gemini` in a terminal and sign in with your Google account. Bosun unsets `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_APPLICATION_CREDENTIALS`, and custom base URL variables so Bosun uses your account login rather than an API key or Vertex billing.

**Add a login folder:** to pool another Gemini login, sign in with `GEMINI_CLI_HOME`:

```sh
mkdir -p ~/.gemini-work
GEMINI_CLI_HOME=~/.gemini-work gemini
```

Add `~/.gemini-work` in the dashboard (`add_ai_login`). Bosun never reads files in that folder; it only passes `GEMINI_CLI_HOME`.

**What fails closed:** Bosun checks `gemini --version`, `gemini --help`, and the presence of Bosun's bundled `gemini-no-tools.toml` policy file. Every lockdown flag (`-p`, `--approval-mode`, `--policy`, `--output-format`, `--allowed-mcp-server-names`, `--extensions`) must appear in `--help`. If any flag or the policy file is missing, Bosun sends Gemini CLI no prompt. Gemini CLI does not report plan windows yet, so Bosun counts its calls and tokens as estimated.
