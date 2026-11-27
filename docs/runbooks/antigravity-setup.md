# Setting up Antigravity for Bosun

**Install:** install the Antigravity CLI so `agy` is on `PATH`.

**Sign in:** open Antigravity or sign in through the `agy` CLI in your desktop session.

**Add a login folder:** Antigravity holds one login per machine. Bosun cannot add a second login folder for it; only the default login is available.

**What fails closed:** Bosun checks `agy --version` and `agy --help` for every required lockdown flag (`--sandbox`, `--disable-slash-commands`, `--input-format`, `--output-format`, `--print-timeout`, `--json-schema`, `--mode`, `--model`, `--print`). If any flag is missing or `--help` fails, Bosun sends Antigravity no prompt. Because `agy` does not document a per-call flag that disables built-in tools and MCP servers, Bosun keeps Antigravity off jobs that carry untrusted text (such as release notes) until one is verified.
