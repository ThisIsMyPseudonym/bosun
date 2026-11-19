# Contributing

Bosun is provided as is. Issues and pull requests are welcome, and no reply or review date is promised. A small,
focused patch with a test is the most likely to be merged.

macOS is the supported platform. Windows and Linux are experimental: the tests run there in CI, and a change that
only helps them is fine, but a change must not break macOS.

To report a vulnerability, do not open an issue. See `SECURITY.md`.

## Set up

You need Python 3.13 and [`uv`](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest -q -p no:cacheprovider
```

The suite is offline and must pass before you send a patch. The tests marked `contract`, which ask real outside
services whether they still answer as Bosun reads them, are deselected by default (`-m contract` runs them). JavaScript has no build step: check a file you edited with `node --check`.

## Try a change without touching a real Bosun

Every `bosun` command opens the database in the data folder and migrates it. Point it at an empty one, on its
own port, with nothing that reaches out:

```sh
BOSUN_DATA_DIR=$(mktemp -d) BOSUN_PORT=8794 BOSUN_SELF_UPDATE=0 uv run bosun serve
```

`uv run bosun demo` shows the dashboard on made-up data, with every action that changes anything refused.

## Rules a patch must follow

- **Commits are signed.** The repository requires it. Do not use `--no-gpg-sign`. (A fork that wants
  self-update needs its own `trusted_signers`; see "Staying current" in the README.)
- **Every git call goes through `proc.git`.** It switches off a repository's own `core.fsmonitor` and hooks.
  Bosun runs git in repositories it did not create.
- **Every Claude call goes through `llm.claude`.** It records the call, the tokens and the budget.
- **Every new capability is an `ActionSpec` in `registry.py`,** with a typed input and a risk tier. The HTTP
  API and the MCP tools come from it, and a contract test fails for a route that is not declared there.
  `Actions.permit` decides who may run what; do not add a second place that decides.
- **Commands are argv lists.** Use `proc.run([...])`. Never build a shell string, and validate any id that
  comes from outside before it reaches a command.
- **Text from outside is data.** Release notes, package names, repository files and model output reach the
  page through `textContent`, never `innerHTML`.
- **Tests are named as sentences,** for example `test_demo_refuses_an_install`. A bug fix comes with a test
  that fails without it.
- **Comments say why,** in plain language. Write prose in plain English: short sentences, no marketing words.
- **Never write a personal name, email address, host or key** in a file, a test or a fixture. Invent one.

## Before you open a pull request

- Run the whole suite. If you changed the schema or a view, regenerate `docs/data-model.md` with
  `uv run bosun db --doc > docs/data-model.md` (a test fails when it is stale).
- Describe what changed and why, and what you ran.
