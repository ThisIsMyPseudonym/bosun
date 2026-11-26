# Automations: when this happens, do that

Status: design, agreed 2026-11-26. Project 3 of 4. Builds on [the provider layer](2026-11-25-ai-providers-design.md)
(project 1) and [error triage](2026-11-25-error-triage-design.md) (project 2) and changes neither.

## Why

Bosun already does the work: upgrades, re-scans, Branch + test, "Will this break?", triage. What it can't do is
do one of them *because* something happened. A Homebrew lock fails an upgrade at 08:31 and the item waits until
you come back and press **Try again**. A project's Do-now lane needs a Branch + test run, and you start it by
hand. Project 2 decided that actions driven by a verdict belong here, with approval.

An automation is one owner-written rule: **when** a trigger fires, **if** its conditions hold, **do** one action
Bosun already has. It is not a workflow engine: one trigger, a short list of conditions, one action. No branches,
no loops, no steps.

## Decisions

| Question | Decision |
|---|---|
| Triggers | Four kinds: a verdict, a refresh, a schedule, an in-app update waiting (with project 4). |
| Conditions | A fixed set of fields per trigger, all of which must hold. No expressions. |
| Actions | A short allow-list of registry actions, plus "notify". Nothing `user_only`, nothing that leaves the machine. |
| Approval | Each automation either **runs on its own** (approved once, at creation, for an exact scope) or **asks first** (a card that waits for your click). The Risk tier decides which are allowed. |
| Quota | Every model job an automation starts is background work: the 75% line and the router apply. |
| Runaways | Dedup per event, rate limits per automation, a chain limit, a circuit breaker and one owner-only pause switch. |
| Presets | Four ship, all off. |
| Autopilot | Unchanged. An automation can't turn it on or off, and installs nothing Autopilot wouldn't. |
| Who makes them | Only you, in the dashboard. Agents and the assistant can read them, never create, change or approve. |

## Triggers

| Trigger | Fires when | Event subject (for dedup) |
|---|---|---|
| `verdict` | A triage verdict is stored (`rules`, `cache` or `model`) for a failure in `v_failures` | The failure id (`job:<id>`, `fix:<id>`, `run:<trace>`) |
| `refresh` | A full refresh finishes `ok` or `partial` (rechecks and project rechecks don't count) | The snapshot id |
| `schedule` | A local time is reached: daily at HH:MM, or on chosen weekdays at HH:MM | The automation and the slot (`2026-11-27T02:00`) |
| `in_app_update` | Project 4 reports an app whose update waits inside the app | The app and the version |

Why these four: they are the moments Bosun already records (a verdict row, a snapshot, the clock, project 4's
state), so no new event bus is needed. One tick in the dashboard, every 30 seconds next to triage's, reads each
source after a cursor, the way `notify.py` reads jobs. Cron syntax, item-level events ("newly exploited") and
"a fix finished" are future work.

**Missed schedules.** The dashboard has to be running. A slot missed while the Mac slept or Bosun was closed fires
on start if it is under two hours late, once; otherwise it is recorded as `missed`. Each slot fires once per local
date, so a daylight-saving change neither skips nor doubles it.

## Conditions

All must hold. Each is a choice from a list, never typed text.

| Trigger | Conditions |
|---|---|
| `verdict` | category in a list; `transient` yes/no; `needs_person` yes/no; confidence at least; next step kind; run kind (`upgrade`, `fix`, `refresh`); item source; project |
| `refresh` | something new in chosen lanes; something newly exploited; any update failed |
| `schedule` | a project has items in a chosen lane (`now`, `batch`) that the action can take |
| `in_app_update` | app in a list |

A condition is checked when the event arrives and again when a delayed action is due. If it no longer holds (the
item was updated, a later run of the same kind and subject succeeded), the firing is `skipped` with the reason.

## Actions

| Action | Tier | Runs on its own | Asks first | Scope fixed at creation |
|---|---|---|---|---|
| `notify` (Bosun's own, below) | none | yes | n/a | the digest kind |
| `refresh` | LOCAL | yes | yes | which collectors; always `auto_upgrade: false` |
| `recheck_project` | LOCAL | yes | yes | the project, or "the failed run's project" |
| `triage` (project 2) | LOCAL | yes | yes | "the failure that fired it" |
| `explain_item` | LOCAL | yes | yes | a project and lane |
| `upgrade` | INSTALL | yes, within scope | yes | "the failed item, same version", or listed item keys |
| `start_fix` | INSTALL | yes, within scope | yes | a project and lane (`now` or `batch`) |
| `pin_mcp` | INSTALL | yes, within scope | yes | listed MCP finding keys |
| `relaunch_app`, `open_app`, project 4's action | SYSTEM | no | yes | the app |

**Never available:** every `user_only` action (`set_pref`, so no acknowledge, snooze or ignore; `set_autopilot`;
`set_notify`; budgets and project 1's quota override; `set_agent_policy`; `restore_app`; the terminal and Ask Bosun
panels), the automation actions themselves, `open_pr` and anything EXTERNAL, `cancel_job`, `discard_fix`, and
reads (an automation that only reads does nothing). `retry` in the presets is not a new action: it is the action
that made the failed run, with that run's recorded input narrowed to the failed item.

**`notify`** puts a line in the next notification through `Notifier`: quiet while you are looking, at most one per
ten minutes, merged with the rest, and silent when notifications are off (the line still shows on the Automations
page). The "what changed" digest is built by code from the two latest snapshots and the jobs between them: new in
Do now, newly exploited, updated, failed with the verdict's cause. No model call.

**INSTALL limits.** An automation's install is unattended (`actions.unattended` is true for `automation:*`), so
every guard Autopilot has applies: the three-day wait (24 hours for a KEV fix), `hands_off`, `caskguard`,
`storeguard`, and `autopilot.qualifies` for upgrades: no OS, firmware, drivers or hardware tools, no major jumps,
nothing ignored or snoozed. An automation chooses *when*; it never widens *what*. `start_fix` keeps its sandbox
and pushes nothing.

## Approval

There is no one at the screen when most automations fire, so the approval is given when you save one.

**Runs on its own.** Saving is `save_automation`, `user_only`, from the signed-in browser. For anything above
LOCAL, the editor first shows one sentence with the exact scope ("Upgrade the item that failed, same version, at
most 6 times a day; Bosun's waiting period and checks still apply") and a checkbox. The saved row carries a
**grant**: a digest of the trigger, conditions, action and scope, who saved it and when. Any edit re-grants; a
firing of the old definition that is still waiting is cancelled.

**Asks first.** When it fires, Bosun stores an **offer**: a card on Today and on the Automations page with the
action in plain words, the items and versions, the exact input and why it fired, plus one notification line.
**Approve** requests, decides and spends a one-call approval in the same click (`approvals.py`), so `permit` sees
the same path as the assistant's card. An offer nobody answers expires after 24 hours, or sooner when it no
longer applies. Silence is never a yes.

**In `permit`.** One new branch for actors `automation:<id>`, in the dashboard like the others:

1. The automation exists, is on, isn't paused, and its grant digest matches its current definition.
2. The action is on the allow-list and not `user_only`.
3. The input is inside the granted scope (the keys are the failed item, or in the project and lane, and so on).
4. SYSTEM, and any "asks first" automation, also needs a spent approval, as for the assistant.

Otherwise `Forbidden`, recorded like any refusal.

## Quota

Automations are background work in project 1's sense: `start_fix`, `explain_item` and `triage` started by an
automation route as background whatever job they are, so a login at or past the 75% line is skipped, and "balance
by pace" spreads the load. Your owner-only override covers them as it covers all background work; they can't set it.

With no candidate, the firing waits as `waiting_quota` until the earliest reset, if that is before its deadline
(two hours for a delayed retry, six hours for a schedule slot); otherwise it is `skipped`. Each automation also has
a daily cap on model jobs (default 3) on top of its firing limit.

## Runaways

| Guard | Rule |
|---|---|
| Dedup | One firing per automation and event subject (a unique index). A second verdict for the same failure does nothing. |
| Same work twice | Two automations reaching the same action and input within an hour: the second is `skipped`, naming the first. |
| Rate limit | Per automation: at most `max_per_day` firings (default 6, at most 48) and a cooldown between them (default 15 minutes). |
| Merging | Firings of one automation due in the same minute for the same action become one call (`upgrade` takes a list), at most 20 items. A burst of 12 locked casks is one retry. |
| Chain limit | A run started by an automation carries its depth. Its events can't fire that automation again, and fire others only at depth 1. A retry that fails again is not retried again. |
| Circuit breaker | Three failed firings in a row pause that automation and say so once. Refusals by the waiting period or `hands_off` are skips, not failures. |
| Global cap | At most 30 firings a day across all automations, and one automation INSTALL in flight at a time. |
| Kill switch | **Pause all automations**, owner-only, on the page header and as `bosun automations pause` in your terminal. Waiting firings and offers stay, frozen, until you resume. |

## Autopilot

Autopilot stays as it is: one switch, its own careful rules, on the daily refresh. It is not rewritten as a
preset, because its rules are the safety reference the INSTALL limits above borrow. An automation can't turn it on
or off, and a refresh an automation starts never runs Autopilot, so Autopilot keeps its daily rhythm.

## Presets

Shipped, all off. Turning one on is the same save, with the same sentence and checkbox.

| Preset | Rule | Mode |
|---|---|---|
| Retry when busy | When a verdict is `busy_or_locked`, `transient`, not `needs_person`, on an upgrade: retry that item in 15 minutes | Runs on its own |
| Nightly Branch + test | Daily at 02:00, if project *P* has Do-now items a fix can take: `start_fix` on them | Runs on its own (pick *P*) |
| What changed | After a full refresh: notify with the digest | Runs on its own |
| Offer in-app updates | When project 4 finds an in-app update waiting: offer it | Asks first (arrives with project 4) |

## Where they live

**The Automations page** (sidebar, developer view). Each automation is one sentence ("When a failure is busy or
locked, retry it in 15 minutes"), a switch, its mode, last fired, next due and today's count. The editor is a form
of dropdowns. History lists each firing: the event, why it fired or was skipped, and a link to the run. Waiting
offers sit at the top.

**Everywhere else.** The run an automation starts is on the Runs page as "Automation: Retry when busy"; `v_actions`
records actor `automation:<id>`, and `approved_by` names you for an offer you approved. Agents and the assistant can
read automations (`list_automations`, `Risk.READ`) and may suggest one in words, pointing at the page.

## Storage

`automations`, one row per rule:

| Column | Notes |
|---|---|
| `id`, `name`, `preset` | `preset` is null for your own |
| `enabled`, `paused_reason` | `paused_reason` is set by the circuit breaker |
| `trigger`, `conditions`, `action`, `scope` | JSON, validated by Pydantic models with `extra="forbid"` |
| `mode` | `run` or `ask` |
| `delay_s`, `max_per_day`, `cooldown_s`, `model_jobs_per_day` | |
| `grant_digest`, `granted_by`, `granted_at` | The approval given at save |
| `created`, `updated` | |

`automation_firings`, one row per event that matched:

| Column | Notes |
|---|---|
| `id`, `automation_id`, `automation_name` | The name is copied, so history survives a delete |
| `event_kind`, `event_ref`, `dedup_key` | Unique on (`automation_id`, `dedup_key`) |
| `depth` | 0 when the event came from you, Autopilot, the schedule or an agent |
| `status` | `scheduled`, `waiting_quota`, `offered`, `running`, `done`, `failed`, `skipped`, `missed`, `declined`, `expired`, `refused` |
| `due`, `deadline`, `input` | `input` is the resolved action input |
| `trace_id`, `reason` | The action's run; why it was skipped or refused |
| `decided_by`, `decided` | For offers |
| `created`, `finished` | |

The setting `automations_paused` holds the kill switch. `v_automations` and `v_automation_firings` expose both
tables to `query`; `data-model.md` follows. Firings are kept a year, like runs.

## Untrusted text

A verdict may come from a model reading output a hostile install script wrote. It can only decide *whether* an
automation fires, never *what* it does: the action and scope come from your definition and the failed run's own
recorded input, never from verdict text. The worst a forged `busy_or_locked` can do is one retry, under every
unattended guard, of something that already ran. Owners who want more care can require confidence `high` or
`source: rules`.

## Failures and edge cases

| Case | Behaviour |
|---|---|
| Dashboard restarts mid-firing | `running` with a run recorded is linked to it; without one it returns to `scheduled`. The unique `dedup_key` stops a second firing. |
| Item updated before a delayed action is due | Conditions re-checked: `skipped`, "fixed since". |
| Scope resolves to nothing (lane emptied) | `skipped`, "nothing to do"; not a failure. |
| Action refused by the waiting period or `hands_off` | `skipped` with the reason, shown on the page. |
| Automation edited or deleted with firings waiting | Waiting firings and offers are cancelled; history stays. |
| Notifications off | `notify` lines go to the page only. |
| Demo mode | The engine doesn't run; presets show, switches are disabled. |
| Database edited by hand to add a grant | Trusted as far as the Autopilot or agent switches in the same file: whoever can write it can already change those. |

## Testing

Test first, 80% coverage or better.

- **Pure functions:** condition matching per trigger, scope resolution, dedup keys, merging, chain depth, slot
  computation across daylight-saving changes, rate limits, cooldown, circuit breaker, grant digests.
- **Permit:** `automation:*` is refused every `user_only` action, EXTERNAL, SYSTEM without a spent approval, input
  outside scope, an edited definition with an old grant, a disabled or paused automation, and everything while
  the kill switch is on.
- **Integration:** in a temp database, a `busy_or_locked` verdict makes one delayed `upgrade` job with actor
  `automation:1`; its failure fires nothing; 12 verdicts make one call; an item updated meanwhile is skipped; the
  nightly fix waits as `waiting_quota` past 75% (with project 1's fake CLIs) and skips after its deadline; an offer
  approved runs once and an unanswered one expires.
- **Guards:** an automation upgrade of a major jump, a driver or an item in its waiting period is refused exactly
  as Autopilot's would be.
- **Migration:** the two tables and views, and `data-model.md` lists them.

## Build order

1. Tables, views, Pydantic models, `permit` branch, the engine tick with `schedule` and `refresh` triggers, `notify`.
2. The Automations page, editor, grants, kill switch; presets "What changed" and "Nightly Branch + test".
3. `verdict` trigger (needs project 2), delays, merging, chain limit; preset "Retry when busy".
4. Offers ("asks first"); `in_app_update` and its preset with project 4.

## Decisions on review (agreed 2026-11-26)

| Question | Decision |
|---|---|
| Do presets ship off? | Yes, all four. |
| Do "runs on its own" INSTALL grants expire? | Yes, after 90 days, with one click to renew ("Still want this?"). LOCAL grants never expire. |
| May an automation's install skip the three-day wait? | No, same as Autopilot. A click on the item is the way past the wait. |
| SYSTEM actions on their own? | No. Quitting, reopening apps and in-app updates always ask first. |
| Retries per failure | One (the chain limit). Revisit with data from the firings. |
| "What changed" written by a model? | No, a code-built digest. Model wording can come later. |
| May agents or the assistant pause an automation? | No. Every automation change is `user_only`. |
| Schedules | Daily and chosen weekdays only; no cron syntax. |

## Future work

- More triggers: an item newly exploited, a fix finished (offer **Push & create PR**), a quota window reset.
- The assistant drafts an automation from a conversation; you review and save it on the page.
- Two retries with backoff, if the firings show one isn't enough.
- A per-automation login preference for its model jobs, on top of the router.
- A dry-run view: "this would have fired 4 times last week", from past runs and verdicts.
