# Computer use: updating apps from their own menu

Status: design, agreed 2026-11-25. Project 4 of 4. Builds on
[AI providers](2026-11-25-ai-providers-design.md): the `computer_use` job, its router, quota and approvals.

## Why

Many Mac apps update only from their own menu: Slack, Zoom, Chrome, Discord, Cursor, Claude, Docker and more. Bosun
knows where the menu item is (`appupdaters.MENUS`) and shows it as text: "Slack menu > Check for Updates…". You
click it, wait for the download, click "Restart", and Bosun notices the new version at the next check.

This design does that for you, in two tiers:

1. **Tier 1, no AI.** A small helper uses the macOS accessibility API to press the known menu item and the known
   buttons that follow, for the apps in a fixed table. Bosun's relaunch and app-watch logic then checks the new
   version and its signature.
2. **Tier 2, fallback.** When tier 1 can't finish, you can let an agent try, on one of your AI subscriptions. It
   gets a Bosun-hosted MCP server that can see and press things in that one app and nothing else.

Microsoft apps are out of scope: Bosun already asks Microsoft AutoUpdate directly (`msupdate`).

## Decisions

| Question | Decision |
|---|---|
| Who clicks inside apps | One native helper, `Bosun Helper.app`, built with the window app. Python never gets Accessibility. |
| Permissions | Accessibility for both tiers. Screen Recording only for tier 2 screenshots, and optional. |
| Which agent | Any CLI the router offers for `computer_use` (`mcp`, `vision`), with Bosun's MCP server as its only tool. Never a CLI's own computer use, never the API computer-use tool (it needs an API key). |
| How the agent acts | Accessibility actions on elements of the target app (`AXPress`) and a few keys sent to that app's process. No typing, no mouse coordinates. |
| Who starts it | Tier 1: your click, or an agent if "Let agents quit and reopen apps" is on. Tier 2: your click only, or your approval of the assistant's card. |
| Success | Bosun's own check: a newer version on disk, signed by the same developer. Never the agent's word. |

## Research

What macOS and the subscription CLIs offer today (checked 2026-11-25):

- **Accessibility** is a TCC permission granted to the *responsible process*. A child process inherits its parent's;
  an app opened through LaunchServices (`open`) is responsible for itself
  ([Qt: the responsible process](https://www.qt.io/blog/the-curious-case-of-the-responsible-process)). Bosun's
  dashboard is a `uv` Python process started by launchd, so a grant would land on a Python binary whose path
  changes with each Python upgrade. A small app bundle with its own bundle id, opened with `open`, is the stable
  holder. `AXIsProcessTrustedWithOptions` checks the grant and can show the system prompt
  ([Apple](https://developer.apple.com/documentation/applicationservices/1459186-axisprocesstrustedwithoptions)).
- **AppleScript through System Events** needs Accessibility *and* an Automation (Apple Events) grant to control System
  Events ([OS X Daily](https://osxdaily.com/2024/02/25/fix-not-authorized-to-send-apple-events-to-system-events-mac-error/)).
  Calling the AX API directly from the helper needs only Accessibility.
- **Ad-hoc signed code** (what `swiftc` produces) is matched by TCC on its code hash, so every rebuild silently loses
  the grant while System Settings still shows it on
  ([Barkeep](https://barkeepmac.com/macos-permissions-lost-after-rebuild)). The helper must be rebuilt rarely.
- **Screen Recording.** `CGWindowListCreateImage` is obsoleted in the macOS 15 SDK; ScreenCaptureKit replaces it and
  can capture single windows ([Nonstrict](https://nonstrict.eu/blog/2023/a-look-at-screencapturekit-on-macos-sonoma/)).
  Since macOS 15, apps that capture without the system picker are asked to re-confirm about monthly
  ([9to5Mac](https://9to5mac.com/2024/08/14/macos-sequoia-screen-recording-prompt-monthly/)).
- **Claude Code** has a built-in `computer-use` MCP server on macOS: a research preview for Pro, Max and Ultra,
  interactive sessions only, not with `-p`
  ([docs](https://code.claude.com/docs/en/computer-use)). Bosun runs CLIs headless, so it can't use it, and it
  approves whole apps per session rather than one app per task.
- **Codex** computer use lives in the ChatGPT desktop app (Work or Codex), with per-app approvals; it can't automate
  terminals or approve admin prompts ([OpenAI](https://learn.chatgpt.com/docs/computer-use)). It is not a
  `codex exec` feature.
- **Gemini CLI** declined built-in computer use
  ([issue #10799](https://github.com/google-gemini/gemini-cli/issues/10799), closed "not planned"). Its browser agent's
  visual mode needs an API key or Vertex AI, not "Sign in with Google"
  ([docs](https://github.com/google-gemini/gemini-cli/blob/main/docs/core/subagents.md)). The Gemini Mac app has
  computer use ([Google](https://blog.google/innovation-and-ai/products/gemini-app/computer-use-mac/)), but it's a
  desktop app, not a CLI. Google's documented CLI route is an external MCP driver
  ([AI Studio](https://aistudio.google.com/learn/partner-cua-computer-use-gemini)).
- **Anthropic's computer-use tool** is an API tool sent with `x-api-key`; screenshots are scanned by injection
  classifiers ([docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)). It needs
  an API key, so it's out.

So no subscription CLI offers headless, one-app computer use today. A Bosun-hosted MCP server works with every CLI
that has `mcp` and `vision`, and the scope is Bosun's to enforce.

## The helper

`src/bosun/mac/BosunHelper.swift`, built by `bosun install-launcher` into
`Bosun.app/Contents/Helpers/Bosun Helper.app` (bundle id `com.bosun.helper`, `LSUIElement`). It is rebuilt only
when its source's hash changes; otherwise the built copy is carried over, so the grant survives.

- Bosun runs it with `open -W -n -g -a … --args <request>` and `--stdout <file>`, one request per run, so
  LaunchServices makes it its own responsible process. A request is JSON in a private temporary folder: a verb, the
  target bundle id and path, and a MAC made from `session.key`. Without a valid MAC it does nothing.
- Verbs: `trusted` (and prompt once), `tree`, `press`, `key`, `screenshot`, `menu`, `state`. Each one finds the
  target by bundle path, never by name, and acts only on processes whose executable is inside that bundle.
- Keys go to the app's process (`CGEvent.postToPid`), not to whatever is in front.
- Electron apps get `AXManualAccessibility` set, so their window contents appear in the tree.
- AX calls time out after 2 seconds (`AXUIElementSetMessagingTimeout`).

## Tier 1: scripted updates

A plan per app family, in a new `src/bosun/inapp.py`: steps of "find an element by role and title, press it, then
wait for a condition". Plans are data, so they can be tested against recorded trees. `MENUS` stays the text shown
when there is no plan.

| Family | Apps | Steps |
|---|---|---|
| Sparkle | apps with `SUFeedURL` | App menu > "Check for Updates…" → Sparkle window "Install Update" → wait for "Install and Relaunch" → press. |
| Squirrel / Electron | Slack, Discord, Cursor, Claude, VS Code | App menu > "Check for Updates…" → poll the app menu until an item reads "Restart to Update" (or the app's wording) → press. |
| Chromium | Chrome, Brave | App menu > "About …" starts the download → wait until `relaunch.staged()` sees it → `relaunch.relaunch()` (keeps your tabs). Brave joins `relaunch.APPS`. |
| Firefox | Firefox | App menu > "About Firefox" → wait for the "Restart to Update Firefox" button → press. |
| Zoom | Zoom | zoom.us menu > "Check for Updates…" → Zoom's window "Update" → it restarts itself. |
| Menu bar | Docker Desktop | The app's menu bar extra > "Check for Updates" → its window "Download update" → "Update and restart". |
| Spotify | Spotify | App menu > "About Spotify" → the download link if shown. |

The flow, for every family:

1. **Before.** Read the bundle's version and signer (`appguard.signer`, `signed_by`), and whether it passes
   `appguard.assess`. Keep a copy (`appguard.keep`, an APFS clone) so Go back works.
2. **Start.** If the app isn't running, open it in the background, and quit it again at the end. Bring it to the
   front only while a menu is pressed.
3. **Steps.** Each step has a time limit (10 minutes in all, for slow downloads). Known "nothing to do" texts
   ("You're up to date") end the run as `not_offered`, which also feeds the item's updater verdict.
4. **After.** The same check as `appwatch.check` and `caskguard`: a newer version, `signed_by` the same team, and
   `assess` if the old copy passed it. If the signer changed, Bosun asks the app to quit (`relaunch`'s graceful
   quit), puts the kept copy back, and says so. If the app won't quit, the item says so, with Go back.

Anything unexpected (a step's element missing or disabled, an unknown window, a password prompt) stops tier 1 with
the reason. The item then offers **Try with AI** and keeps the menu text.

## Tier 2: an agent scoped to one app

`bosun mcp --as computer-use`, started by the router's chosen CLI with project 1's lockdown: no shell, file, web or
other MCP tools, an empty folder, a per-run token in the environment. Every tool call goes to the dashboard, which
calls the helper. The target app is fixed by the run, never by a tool argument.

| Tool | What it does |
|---|---|
| `app_state` | App name, installed and expected versions, its windows (titles, sizes), whether a sheet or dialog is open. |
| `ax_tree` | The app's menu bar and windows as a pruned tree. Each element has a short id (`e12`), role, title, value (cut to 200 characters), enabled, actions. Ids last until the next read. |
| `screenshot` | PNG of the app's own windows only (ScreenCaptureKit window filter), downscaled. Off without Screen Recording. |
| `press` | `AXPress` on an element id from the last tree. |
| `key` | One of Return, Escape, Tab, Shift-Tab, Space and the arrows, to the app's process. |
| `wait` | Up to 10 seconds, then the new `app_state`. |
| `finish` | The agent says it is done or stuck, with one sentence. Bosun then runs its own check. |

There is no tool to type text, open an app or a URL, click a coordinate, or reach another app.

**Guards, enforced in the dashboard and the helper, not in the prompt:**

- Before every `press`, the element is read again: it must still belong to the target's processes, with the same
  role and title.
- Pressing an element titled like sign out, delete, discard, don't save, buy, purchase, subscribe, pay or allow is
  refused, and the run stops.
- Pressing one titled like restart, relaunch, install or quit raises an approval card (below).
- The run stops when a password field (`AXSecureTextField`) appears in the app, when a SecurityAgent or authorization
  window appears anywhere, or when a payment or sign-in page is detected. The item says what to finish by hand.
- At most 25 actions (`press`, `key`) and 4 minutes per run. One computer-use run at a time on the machine.
- Browsers are not offered tier 2: their windows hold any web page's text, and tier 1 covers them.

**Every step is shown.** The item opens a live panel: each tool call, the element pressed, and the latest
screenshot. **Stop** ends the CLI and the helper and declines any waiting card. The run's trace keeps the steps as
text. Screenshots are not stored (see open questions).

## Approvals

The existing one-call approvals (`approvals.py`), unchanged.

| Who | Tier 1 | Tier 2 start | Restart inside tier 2 |
|---|---|---|---|
| You, on the item | Your click ("Update Slack and restart it") | Your click ("Try with AI", naming the login it will use) | A card in the panel |
| Ask Bosun | A card | A card | A card |
| An agent over MCP | Only with "Let agents quit and reopen apps" on | Refused | — |
| The tier-2 agent itself | — | — | It can only ask; it has no way to answer |

New registry actions: `update_in_app` (`Risk.SYSTEM`) and `computer_use_start`, `computer_use_stop` (`user_only`,
`panel`). The agent's MCP calls carry its run token, so they are recorded as `computer-use:<run>` and can never call
`decide`.

## Failures and edge cases

| Case | Behaviour |
|---|---|
| Accessibility not granted | The item shows **Allow Bosun to update apps**, which calls the helper's `trusted` verb (system prompt) and opens the Accessibility pane. Doctor checks it. |
| Granted but not working | `trusted` says no while Settings shows it on (helper rebuilt). Doctor says: remove Bosun Helper from the list and add it again. |
| Screen Recording missing | Tier 2 runs on the tree only and says so. |
| App not responding | AX calls time out; three tries, then "Slack isn't responding". |
| A dialog or sheet already open | Menu items are disabled: "Slack has a window open that needs you; close it and try again". |
| Menu item not found | The plan is out of date for this version. Tier 1 stops, the run records the app version, Try with AI is offered. |
| Menus not in English | Tier 1 matches English titles, and Sparkle's own string from the app's bundle. Otherwise tier 2. |
| Password or admin prompt | Both tiers stop. Bosun never types a password. "Zoom is asking for your password: finish in Zoom." |
| The app restarts | Followed by bundle id, not pid. The check runs after the new process is up. |
| Screen locked or another user active | Not started; "Unlock your Mac and try again". |
| Same version after the run | "Slack is still 4.40. Its updater may not offer 4.41 yet." Updater verdict set to `not_offered`. |
| Every AI login out | Tier 2 button says which login resets when, like any clicked job. |

## Safety

Threat model, worst first:

- **Prompt injection from the screen.** Slack messages, release notes in an update window and web content are in
  the tree and the screenshots. Tool results say this text is data. What actually limits it is the tool surface: no
  typing, one app, a deny list, a card before anything that restarts, a step limit, and Bosun's own check at the end.
  A hijacked agent can at worst press harmless buttons in that one app until the limit.
- **The helper as a way in.** Accessibility lets the helper control any app. Its requests need a MAC from
  `session.key`, so programs that can't read Bosun's key file get nothing. A program running as you can read the
  key, as with `bosun mcp` today. If the helper binary is replaced, its code hash changes and macOS drops its grant.
- **A bad update.** The app's own updater checks its download. Bosun adds the same-signer check and the kept copy.
- **Privacy.** Screenshots of a chat app show your messages. They go only to the provider of the login you chose,
  and are not stored by Bosun.

`docs/security.md` and `docs/ai-tools.md` gain a section each when this ships.

## Testing

Test first, 80% coverage or better.

- **Pure functions:** plan matching against recorded AX trees, title rules (deny, card, stop), step and time limits,
  the after-check table (version, signer, assess), request MACs, MCP input validation.
- **Recorded trees:** `bosun dev record-ax <bundle id>` writes a redacted tree to
  `tests/fixtures/ax/<app>/<version>.json`. One per app in the table, plus before and after states.
- **Fake app:** `tests/fixtures/FakeUpdater.swift`, an AppKit app with "Check for Updates…" and modes: Sparkle-like
  window, slow download, modal open, password field, a "Buy Pro" button, on-screen text telling the agent to sign
  out. Run with the real helper under `pytest -m macos_ui`, locally only (hosted CI can't grant Accessibility).
- **Tier 2 without a model:** project 1's fake CLIs replay scripted tool calls: a run that succeeds, one that tries
  the deny list after reading injected text (refused, stopped), one that runs out of steps, one stopped by you.
- **Contract:** a `vision` CLI must pass an MCP image result to its model. `bosun doctor --providers` shows a test
  image and checks the answer. A CLI that fails loses `vision`.
- **Manual checklist**, per app in the table, before release: not running; running with a document open; already up
  to date; update offered; Accessibility off; Screen Recording off (tier 2); non-English system language; Stop
  pressed mid-run. Record the macOS and app versions with the result.

## Build order

1. The helper: build, `trusted`, `tree`, `press`, grant handling in doctor.
2. Recorder and fake app.
3. Tier 1 for Squirrel and Sparkle apps, with the before and after checks.
4. Chromium, Firefox, Zoom, Docker and Spotify plans.
5. Tier 2 MCP server, guards and the live panel.
6. Screenshots (Screen Recording).

## Decisions on review (agreed 2026-11-25)

| Question | Decision |
|---|---|
| How the helper is signed | Ad-hoc, rebuilt only when its source changes. A changed binary losing its grant is a safety property. |
| Screen Recording | Optional, asked for on the first **Try with AI**. |
| New version signed by a different developer | Quit the app and put the kept copy back, as `caskguard` does, and say why. |
| Restart in tier 1 | Covered by the first click; the button says "and restart". |
| Screenshots in the record | None kept. They go to the provider for that step only; the run keeps a text trace. |
| Browsers in tier 2 | Excluded for now; browsers use tier 1 only. |
| Agents starting tier 1 | Under the existing "Let agents quit and reopen apps" switch. |

## Future work

- Tier 1 under Autopilot, at idle times, for apps not in use.
- A successful tier-2 run proposes a tier-1 plan for that app, for you to review.
- Coordinate clicks for apps whose updater has no accessibility tree, with a hit test against the target.
- The system window picker (`SCContentSharingPicker`) as a per-run alternative to Screen Recording.
- Windows: UI Automation behind the same plans and MCP tools.
