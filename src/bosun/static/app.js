// State, routing, actions, and the activity tray.
"use strict";
(() => {
const { el, api, plural } = MC;
const $ = (s) => document.querySelector(s);
const S = { raw: false, demo: false, prefs: {}, items: [], allItems: [], projects: [], history: [], openKey: null,
            list: { group: "route", sort: "risk" }, find: "", jobs: [], fixes: [], trayOpen: false, dormantOpen: false, showIgnored: false, needsAll: false,
            autopilot: { on: false, time: null, queued: [] }, relaunch: [], automationOffers: [], notify: { on: true }, agents: { system: false, external: false } };

// What you said about an item, and whether it is back: worked out by the server (attention.py), never here.
MC.standing = (i) => i.standing || { state: "", back: "", said: "" };
// a project you ignored stays out of sight, except for what is being attacked (the server's rule: attention.py)
const projectIgnored = (st, i) => !!(i.project && (st.prefs[`project:${i.project}`] || {}).ignored) && !(i.kev || i.malicious);
MC.isIgnored = (st, i) => MC.standing(i).state === "ignored" || projectIgnored(st, i);

// `action`: one thing to do about what was just said, e.g. { label: "Undo", act }
function banner(msg, kind = "error", action = null) {
  if (MC.stale) {  // nothing else matters until the window is reloaded, and whatever failed failed because of this
    msg = "Bosun restarted since this window was opened, so this window is out of sync and nothing you click will work.";
    kind = "error";
    action = { label: "Reload this window", act: () => location.reload() };
  }
  const b = $("#banner");
  b.replaceChildren(msg || "", action ? el("button", { class: "link accent", text: action.label,
    onclick: () => { banner(""); action.act(); } }) : "");
  b.className = `banner ${kind}`; b.hidden = !msg;
  clearTimeout(banner.timer);
  if (msg && kind === "info" && !action) banner.timer = setTimeout(() => { if (b.textContent === msg) b.hidden = true; }, 6000);
}

MC.banner = banner;

MC.render = function render() {
  // in front of you: not put away, and not in an ignored project (unless you asked to see those)
  S.items = S.allItems.filter((i) => !MC.standing(i).state && (S.showIgnored || !projectIgnored(S, i)));
  // put away: acknowledged and snoozed, listed at the foot of their page; ignored, behind "Show N ignored"
  S.aside = S.allItems.filter((i) => ["ack", "snoozed"].includes(MC.standing(i).state) && !projectIgnored(S, i));
  S.ignoredItems = S.allItems.filter((i) => MC.standing(i).state === "ignored" && !projectIgnored(S, i));
  const route = MC.route.parse();
  const scopes = MC.buildScopes(S);
  $("#nav").replaceChildren(...MC.views.nav(S, scopes, route).filter(Boolean));
  if (!S.raw) { $("#main").replaceChildren(el("p", { class: "hint", text: "No data yet. Click the refresh button at the top right (the first check takes a few minutes)." })); return; }
  const view = route.page === "scope" ? MC.views.scope(S, scopes, route.id)
    : route.page === "packages" ? MC.views.packages(S)
    : route.page === "runs" ? MC.views.runs(S)
    : route.page === "usage" ? MC.views.usage(S)
    : route.page === "automations" ? MC.views.automations(S)
    : route.page === "trace" ? MC.views.trace(S, route.id) : MC.views.today(S, scopes);
  // the page is rebuilt every few seconds while updates run: whoever had the keyboard keeps it
  const had = document.activeElement && $("#main").contains(document.activeElement) ? document.activeElement : null;
  const fk = had && had.getAttribute("data-fk"), caret = had && "selectionStart" in had ? [had.selectionStart, had.selectionEnd] : null;
  $("#main").replaceChildren(view);
  if (fk) {
    const again = [...$("#main").querySelectorAll("[data-fk]")].find((n) => n.getAttribute("data-fk") === fk);
    if (again) { again.focus({ preventScroll: true }); if (caret && again.setSelectionRange) { try { again.setSelectionRange(...caret); } catch { /* not a text field */ } } }
  }
};

async function load() {
  try {
    // "visible" tells the server you're looking, so it doesn't send a notification about what's on screen
    const st = await api(`/api/state${document.visibilityState === "visible" ? "?visible=1" : ""}`);
    S.notify = st.notify || S.notify; renderBell();
    S.demo = !!st.demo; S.automationOffers = st.automation_offers || [];
    S.claude = st.claude_enabled; S.view = st.view || "auto";
    S.agents = st.agents || S.agents;
    MC.assistantInfo = st.assistant || null;  // the Ask Bosun panel's model and login
    S.prefs = st.prefs; S.history = st.history || []; S.autopilot = st.autopilot || S.autopilot; S.relaunch = st.relaunch || [];
    if (st.machine) MC.MACHINE = st.machine;
    S.watches = st.watches || {}; announceWatches();
    if (st.list_prefs) { MC.fold.seed(st.list_prefs.folds); if (!S.listTouched) S.list = st.list_prefs.pc || S.list; }
    // a manual update that is still going (the page was reloaded, or opened in another window): show it again
    if (st.terminal && MC.termOpen) MC.termOpen(st.terminal.job, st.terminal.key, MC.nameOf({ allItems: (st.snapshot || {}).items || [] }, st.terminal.key));
    const snap = st.snapshot;
    if (snap) { const moved = S.snapTs && S.snapTs !== snap.ts; S.allItems = snap.items; S.projects = snap.projects; S.raw = true; S.snapTs = snap.ts; if (moved) MC.peek.stale(); }
    // "auto": simple when this computer has no code projects; a choice made in the sidebar sticks
    MC.SIMPLE = S.view === "simple" || (S.view === "auto" && S.raw && !S.projects.length);
    document.body.classList.toggle("simple", MC.SIMPLE);
    const r = st.refresh;
    S.refreshRunning = r.running;
    const what = r.rechecking && r.rechecking.length ? ` · re-checking ${r.rechecking.join(", ")}…` : r.running ? " · refreshing…" : "";
    $("#scope").textContent = snap ? `Checked ${MC.ago(snap.ts)}${what}` : (r.running ? "First check running…" : "No data yet");
    $("#claude").textContent = st.claude_enabled ? `Claude Code summaries (${st.model})` : "Basic summaries (claude CLI not found)";
    $("#refresh").disabled = r.running;
    $("#refresh").title = r.running ? "Checking…" : "Check for updates now";
    const errs = [...(snap?.errors || []), ...(r.error ? [r.error] : [])];
    if (errs.length) banner(`Some sources had problems: ${errs.join(" · ")}`);
  } catch (e) { banner(`Could not load dashboard data: ${e.message}`); return; }
  try { MC.render(); }
  catch (e) { console.error(e); banner(`This page could not be drawn (a bug in Bosun, not in your data): ${e.message}`); }
}

// A manual update Bosun was watching for has arrived (appwatch.py): say so once, and loudly if its signer changed.
const announced = new Set();
function announceWatches() {
  for (const [key, w] of Object.entries(S.watches || {})) {
    if (!w.done || announced.has(`${key}|${w.done}`)) continue;
    announced.add(`${key}|${w.done}`);
    if (w.signer_changed) banner(`${w.name} is now ${w.done}, but it is signed by a different developer than the copy it replaced. Bosun did not install it. Check where it came from before you open it.`);
    else banner(`${w.name} is now ${w.done}. Updated ✓`, "info");
  }
}
const watching = () => Object.values(S.watches || {}).some((w) => !w.done);

let pollTimer = null;
async function loadJobs() {
  try { [S.jobs, S.fixes] = await Promise.all([api("/api/jobs"), api("/api/fixes")]); } catch { return; }
  renderTray();
  const busy = [...S.jobs, ...S.fixes].some((j) => j.status === "queued" || j.status === "running")
    || S.refreshRunning || S.jobs.some((j) => j.status === "succeeded" && (j.finished || 0) > (S.snapTs || 0));
  const waiting = S.jobs.some((j) => j.status === "waiting");
  clearTimeout(pollTimer);
  // while upgrades run (and until the follow-up re-check lands), poll both jobs and state;
  // a job waiting for an app to close changes rarely, so check on it slowly
  if (busy || waiting || watching()) pollTimer = setTimeout(async () => { await load(); await loadJobs(); if (S.openKey) MC.actions.open(S.openKey); }, busy ? 3000 : 20000);
}

function renderBell() {
  const b = $("#notify"), on = S.notify.on;
  b.setAttribute("aria-pressed", String(on));
  b.title = on ? "Notifications on: only when something needs you. Click to turn off." : "Notifications off. Click to turn on.";
  b.replaceChildren(MC.icon(on ? "bell" : "bellOff"));
}

// A small chip in the corner while something runs; the list opens only when you ask for it
function renderTray() {
  const tray = $("#tray");
  const fixActive = S.fixes.filter((f) => f.status === "queued" || f.status === "running");
  const active = S.jobs.filter((j) => j.status === "queued" || j.status === "running");
  const waiting = S.jobs.filter((j) => j.status === "waiting");
  // the tray is for "something is running" or "show me the log"; Today's Recent card covers the rest
  if (!active.length && !fixActive.length && !S.trayOpen) { tray.hidden = true; return; }
  const recentFail = S.jobs.filter((j) => j.status === "failed" && Date.now() / 1000 - (j.finished || 0) < 3600).length;
  tray.hidden = false;
  tray.classList.toggle("open", S.trayOpen);
  const running = active.length || fixActive.length;
  const parts = [];
  if (active.length) parts.push(`${plural(active.length, "update")} running`);
  if (waiting.length) parts.push(`${waiting.length} waiting for an app to close`);
  if (fixActive.length) parts.push(`${plural(fixActive.length, "Claude Code fix", "Claude Code fixes")} running`);
  if (!parts.length) parts.push("Activity");
  const notes = [];
  if (active.length) notes.push(`One at a time${MC.MACHINE.kind === "pc" ? "; approve the UAC prompt if one appears" : ""}.`);
  if (!running && S.jobs.length) notes.push(`Last update ${MC.ago(S.jobs[0].finished || S.jobs[0].started)}${recentFail ? ` · ${recentFail} failed in the last hour` : ""}.`);
  const projName = (key) => (S.projects.find((p) => p.key === key) || {}).name || "project";
  const toggle = () => { S.trayOpen = !S.trayOpen; renderTray(); };
  const scrolled = tray.querySelector(".traybody")?.scrollTop || 0;  // it re-renders every few seconds: stay put
  tray.replaceChildren(
    el("button", { class: "trayhead", "aria-expanded": String(S.trayOpen), onclick: toggle,
      title: S.trayOpen ? "Fold this away" : "Show what's running and what ran" },
      running ? el("span", { class: "spinner", "aria-hidden": "true" }) : null,
      el("strong", { text: parts.join(" · ") }),
      el("span", { class: "traytoggle", text: S.trayOpen ? (running ? "Hide" : "Close") : "Show" })),
    S.trayOpen ? el("div", { class: "traybody" },
      notes.length ? el("p", { class: "hint", text: notes.join(" ") }) : null,
      el("ul", {},
        ...S.fixes.slice(0, 15).map((f) => {
          const n = ((f.result || {}).keys || []).length;
          const label = n > 1 ? `${n} dependencies in ${projName(f.project)}` : f.item_key.replace(/^(repo|container):[^:]*:?/, "").split("@")[0];
          return el("li", { class: f.status }, el("strong", { text: `${MC.FIX_LABEL[f.status] || f.status} ` }),
            el("a", { href: MC.route.href("scope", f.project), text: label }),
            el("span", { text: ` · ${MC.ago(f.started)}${f.pr_url ? " · PR opened" : ""}` }));
        }),
        ...S.jobs.slice(0, 30).map((j) => el("li", { class: j.status },
          el("strong", { text: `${MC.jobLabel(j)} ` }), el("span", { text: `${MC.nameOf(S, j.item_key)} · ${MC.ago(j.started)}` }),
          j.status === "waiting" ? el("button", { class: "link", text: "Cancel", onclick: () => MC.actions.cancelJob(j.id) }) : null,
          j.status === "waiting" && j.output ? el("div", { class: "hint", text: j.output }) : null,
          j.hint ? el("div", { class: "hint", text: j.hint }) : null,
          j.trace_id && j.status !== "waiting" && j.status !== "queued" ? el("a", { class: "tracelink", href: MC.runHref(j.trace_id), text: "Details" }) : null,
          j.trace_id && j.status === "failed" ? el("button", { class: "link accent", text: "Why did this fail?", onclick: () => MC.ask("Why did this fail?", { run: j.trace_id, item: j.item_key }) }) : null,
          j.output ? MC.details(`job:${j.id}`, false, {}, el("summary", { text: "output" }), el("pre", { text: j.output })) : null)))) : "");
  const body = tray.querySelector(".traybody");
  if (body) body.scrollTop = scrolled;
}

let opener = null;  // what had the keyboard when the details opened
// Tab stays inside the open details (a dialog over the page), wrapping at both ends
$("#drawer").addEventListener("keydown", (e) => {
  if (e.key !== "Tab") return;
  const all = [...$("#drawer").querySelectorAll("a[href], button:not(:disabled), summary, input, select, textarea, [tabindex='0']")]
    .filter((n) => n.offsetParent !== null);
  if (!all.length) return;
  const first = all[0], last = all[all.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});

MC.actions = {
  async setAutopilot(on) {
    try { S.autopilot = await api("/api/autopilot", { method: "POST", body: JSON.stringify({ on }) }); MC.render(); }
    catch (e) { banner(`Could not change Autopilot: ${e.message}`); }
  },
  showActivity() { S.trayOpen = true; renderTray(); },
  async setView(mode) {
    try { await api("/api/view", { method: "POST", body: JSON.stringify({ mode }) }); await load(); }
    catch (e) { banner(`Could not switch the view: ${e.message}`); }
  },
  async setNotify(on) {
    try {
      S.notify = await api("/api/notify", { method: "POST", body: JSON.stringify({ on }) }); renderBell();
      banner(on ? "Notifications on. Bosun sent a test one; it only speaks up when something needs you." : "Notifications off.", "info");
    } catch (e) { banner(`Could not change notifications: ${e.message}`); }
  },
  async relaunch(r) {
    if (!confirm(`Restart ${r.name} to finish its update?

It closes and reopens with your tabs. Text typed into a form that isn't saved may be lost.`)) return;
    try { await api("/api/relaunch", { method: "POST", body: JSON.stringify({ app: r.app }) }); }
    catch (e) { banner(`Could not relaunch: ${e.message}`); }
    await loadJobs(); MC.render();
  },
  async cancelJob(id) {
    try { await api(`/api/jobs/${id}/cancel`, { method: "POST" }); } catch (e) { banner(`Could not cancel: ${e.message}`); }
    await loadJobs(); MC.render();
  },
  async upgrade(keys) {
    try {
      const r = await api("/api/upgrade", { method: "POST", body: JSON.stringify({ keys }) });
      const skipped = r.skipped.length ? ` ${r.skipped.length} skipped: ${r.skipped.map((s) => s.reason).join("; ")}` : "";
      banner(`Started ${plural(r.queued.length, "update")}. They run one at a time${MC.MACHINE.kind === "pc" ? " (approve any UAC prompt)" : ""}; anything in use waits until it's free.${skipped}`, "info");
      loadJobs();
    } catch (e) { banner(`Could not start the update: ${e.message}`); }
  },
  async openApp(item) {
    try {
      await api("/api/open-app", { method: "POST", body: JSON.stringify({ key: item.key }) });
      banner(`Opened ${item.name}. Accept its update if it offers one (or restart it if it's waiting). Bosun re-checks on its next run.`, "info");
    } catch (e) { banner(`Could not open ${item.name}: ${e.message}`); }
  },
  async goBack(k) {
    if (!confirm(`Go back to ${k.name} ${k.kept}?\n\nBosun puts the copy it kept in place of ${k.now}. Quit ${k.name} first. `
      + `Anything ${k.now} changed in your settings or files stays as it is. Bosun lists ${k.now} again as an update: Autopilot leaves it alone, and you can install it with a click.`)) return;
    try {
      const r = await api("/api/restore-app", { method: "POST", body: JSON.stringify({ bundle_id: k.bundle_id }) });
      banner(`${r.name} is back on ${r.version}.`, "info");
      await load(); await loadJobs(); MC.render();
    } catch (e) { banner(`Could not go back: ${e.message}`); }
  },
  async watchApp(item) {
    try {
      const r = await api("/api/watch-app", { method: "POST", body: JSON.stringify({ key: item.key }) });
      banner(`Opened the maker's page. Bosun is watching for ${r.watching} ${r.want} and will say when it's there.`, "info");
      await load(); await loadJobs(); MC.render();
    } catch (e) { /* the page is open either way; without a watch the next check picks the update up */ }
  },
  async checkApps() {
    try {
      const r = await api("/api/check-app", { method: "POST", body: "{}" });
      if (!r.changed.length) banner("Not there yet. Bosun keeps looking.", "info");
      await load(); await loadJobs(); MC.render();
    } catch (e) { banner(`Could not check: ${e.message}`); }
  },
  async pin(keys) {
    if (keys.length > 1 && !confirm(`Pin ${keys.length} MCP servers, each to its newest version that has been out three days?\n\n`
      + "Bosun backs up each config file first. Claude Code's servers go through the claude command; the rest are edited in place.")) return;
    try {
      const r = await api("/api/mcp/pin", { method: "POST", body: JSON.stringify({ keys }) });
      banner(`Pinning ${plural(r.queued.length, "MCP server")}. Restart the AI tool afterwards to load the pinned version.`, "info");
      loadJobs();
    } catch (e) { banner(`Could not pin: ${e.message}`); }
  },
  // A group's or a suite's button: every member the same way (MC.bulkMode), after one confirmation.
  updateMany(members) {
    const mode = MC.bulkMode(S, members[0]);
    if (mode.startsWith("recipe:")) return MC.runSuite(members.map((m) => m.key), mode.slice(7), members.map((m) => m.name));
    return MC.actions.upgradeKeys(members.map((m) => m.key), members.map((m) => m.name));
  },
  async setStateMany(keys, state, days) {
    try { for (const key of keys) await api("/api/prefs", { method: "POST", body: JSON.stringify({ key, state, ...(days ? { days } : {}) }) }); }
    catch (e) { banner(`Could not save: ${e.message}`); }
    await load();
    if (state !== "open") banner(`${plural(keys.length, "item")} put away.`, "info", { label: "Undo", act: () => MC.actions.bringBack(keys) });
  },
  upgradeKeys(keys, names) {
    const mac = MC.MACHINE.kind === "mac";
    const linux = MC.MACHINE.kind === "linux";
    if (!confirm(`Update ${plural(keys.length, "app or tool", "apps and tools")} on ${mac ? "this Mac" : "this computer"}?\n\n${names.join(", ")}\n\n${
      linux ? "System packages go in one go after one password window; the rest run one at a time."
      : `They run one at a time${MC.MACHINE.kind === "pc" ? "; some need a UAC prompt." : "."}`}`)) return;
    MC.actions.upgrade(keys);
  },
  // Put an item away, or bring it back ("open"). `state`: ack | snoozed (with days) | skip | ignored | open.
  async setState(key, state, days) {
    const name = MC.nameOf(S, key), before = MC.standing(S.allItems.find((i) => i.key === key) || {});
    try { await api("/api/prefs", { method: "POST", body: JSON.stringify({ key, state, ...(days ? { days } : {}) }) }); }
    catch (e) { banner(`Could not save: ${e.message}`); return; }
    await load(); if (S.openKey) MC.actions.open(S.openKey);
    const said = { ack: MC.words("acknowledged", "set aside"), snoozed: `snoozed for ${days === 7 ? "a week" : days === 30 ? "a month" : `${days} days`}`,
                   skip: "set aside until the next version", ignored: "ignored", open: "is back in the list" }[state];
    // undo only where it is exact: something that was open goes back to open
    banner(`${name} ${said}.`, "info", state !== "open" && !before.state ? { label: "Undo", act: () => MC.actions.setState(key, "open") } : null);
  },
  async bringBack(keys) {
    try { for (const key of keys) await api("/api/prefs", { method: "POST", body: JSON.stringify({ key, state: "open" }) }); }
    catch (e) { banner(`Could not save: ${e.message}`); }
    await load(); if (S.openKey) MC.actions.open(S.openKey);
  },
  showIgnored(on) { S.showIgnored = on; MC.render(); },
  // how the machine page is grouped and sorted: applied at once, remembered by the server
  setList(change) {
    S.list = { ...S.list, ...change }; S.listTouched = true; MC.render();
    api("/api/list-prefs", { method: "POST", body: JSON.stringify({ page: "pc", ...change }) }).catch(() => {});
  },
  find(text) { S.find = text; MC.render(); },
  async setPref(key, change) {
    try { await api("/api/prefs", { method: "POST", body: JSON.stringify({ key, ...change }) }); await load(); if (S.openKey) MC.actions.open(S.openKey); }
    catch (e) { banner(`Could not save: ${e.message}`); }
  },
  async open(key) {
    const drawer = $("#drawer"), fresh = S.openKey !== key || drawer.hidden;
    if (fresh && !drawer.contains(document.activeElement)) opener = document.activeElement;  // where focus goes back to
    S.openKey = key;
    try {
      const item = await api(`/api/item?key=${encodeURIComponent(key)}`);
      if (S.openKey !== key) return;  // closed, or another one opened, while this loaded
      // it is rebuilt while updates run: keep the keyboard where it was
      const had = drawer.contains(document.activeElement) ? document.activeElement.getAttribute("data-fk") : null;
      drawer.replaceChildren(...MC.views.drawer(S, item).filter(Boolean));
      drawer.hidden = false;
      drawer.setAttribute("aria-labelledby", "drawerTitle");
      const want = fresh ? "drawer:close" : had;
      const target = want && [...drawer.querySelectorAll("[data-fk]")].find((n) => n.getAttribute("data-fk") === want);
      if (target) target.focus({ preventScroll: true });
      if (fresh) drawer.scrollTop = 0;
    } catch (e) { banner(`Could not load details: ${e.message}`); }
  },
  close() {
    const was = S.openKey;
    S.openKey = null; $("#drawer").hidden = true;
    if (!was) return;
    const fk = opener && opener.getAttribute && opener.getAttribute("data-fk");
    const back = opener && opener.isConnected ? opener : fk && [...document.querySelectorAll("[data-fk]")].find((n) => n.getAttribute("data-fk") === fk);
    if (back) back.focus({ preventScroll: true });
    opener = null;
  },
  async explain(key) {
    try { await api("/api/explain", { method: "POST", body: JSON.stringify({ keys: [key] }) }); }
    catch (e) { banner(`Could not assess: ${e.message}`); }
    if (S.openKey === key) await MC.actions.open(key);
  },
  async fix(keys) {
    if (keys.length > 1 && !confirm(`Fix ${keys.length} dependencies in one Claude Code run?\n\nOne branch, one commit, tests run once. Nothing is pushed until you click Create PR.`)) return;
    try {
      await api("/api/fix", { method: "POST", body: JSON.stringify({ keys }) });
      banner("Claude Code is working in a separate git worktree. Nothing is pushed until you click Create PR.", "info");
      await loadJobs(); MC.render();
      if (keys.length === 1) MC.actions.open(keys[0]);
    } catch (e) { banner(`Could not start fix: ${e.message}`); }
  },
  // Bosun diagnoses, your own coding agent does the work: copy a prompt carrying the facts
  async copyPrompt(keys, where) {
    let r;
    try { r = await api("/api/prompt", { method: "POST", body: JSON.stringify({ keys }) }); }
    catch (e) { banner(`Could not build the prompt: ${e.message}`); return; }
    const copied = await navigator.clipboard.writeText(r.prompt).then(() => true, () => false);
    const box = $("#promptbox");
    const area = el("textarea", { readonly: true, "aria-label": "Prompt", spellcheck: "false" });
    area.value = r.prompt;
    const again = el("button", { class: "quiet", text: copied ? "Copy again" : "Copy", onclick: () =>
      navigator.clipboard.writeText(r.prompt).then(() => { again.textContent = "Copied"; }, () => { area.select(); again.textContent = "Press Ctrl+C / ⌘C"; }) });
    box.replaceChildren(
      el("h3", { id: "promptTitle", text: copied ? "Copied: paste it into your coding agent" : "Prompt for your coding agent" }),
      el("p", { class: "hint", text: `${plural(r.count, "finding")}${where ? ` in ${where}` : ""}. Paste into Claude Code, Cursor, Codex or whichever agent you use, opened in that repository. When it's done, click Recheck on the project page.` }),
      area,
      el("div", { class: "actions" }, again, el("button", { class: "primary", text: "Done", onclick: () => box.close() })));
    box.showModal();
    if (!copied) area.select();
  },
  async openUpdates() {
    try { const r = await api("/api/open-updates", { method: "POST" }); banner(`Opened ${r.opened}.`, "info"); }
    catch (e) { banner(`Could not open ${MC.MACHINE.updates}: ${e.message}`); }
  },
  async recheckProject(key, name, btn) {
    if (btn) { btn.disabled = true; btn.textContent = "Re-checking…"; }
    try {
      const r = await api("/api/projects/recheck", { method: "POST", body: JSON.stringify({ project: key }) });
      const fixed = r.fixed.length ? `${r.fixed.length} fixed (${r.fixed.slice(0, 5).join(", ")}${r.fixed.length > 5 ? ", …" : ""})` : "nothing fixed yet";
      const fresh = r.new.length ? `, ${r.new.length} new (${r.new.slice(0, 5).join(", ")})` : "";
      banner(`Re-checked ${name}: ${fixed}${fresh}, ${plural(r.remaining, "finding")} open.`, "info");
    } catch (e) { banner(`Could not re-check ${name}: ${e.message}`); }
    await load();
  },
  async createPr(id) {
    if (!confirm("Push this branch to the project's remote and open a pull request?")) return;
    try { const r = await api(`/api/fixes/${id}/pr`, { method: "POST" }); banner(`Pull request opened: ${r.url}`, "info"); }
    catch (e) { banner(`Could not create PR: ${e.message}`); }
    await loadJobs(); MC.render(); if (S.openKey) MC.actions.open(S.openKey);
  },
  async discardFix(id) {
    if (!confirm("Delete this fix branch and its worktree?")) return;
    try { await api(`/api/fixes/${id}/discard`, { method: "POST" }); } catch (e) { banner(`Could not discard: ${e.message}`); }
    await loadJobs(); MC.render(); if (S.openKey) MC.actions.open(S.openKey);
  },
};

MC.openItem = () => S.openKey;  // the item whose details are open, for the Ask Bosun panel's context
MC.refreshNow = () => Promise.all([load(), loadJobs()]);

function setMenu(open) {  // small screens: the sidebar is a Menu that folds away
  document.querySelector(".side").classList.toggle("open", open);
  $("#menubtn").setAttribute("aria-expanded", String(open));
}
$("#menubtn").addEventListener("click", () => setMenu(!document.querySelector(".side").classList.contains("open")));
window.addEventListener("hashchange", () => { setMenu(false); MC.actions.close(); MC.render(); window.scrollTo(0, 0); });
$("#notify").addEventListener("click", () => MC.actions.setNotify(!S.notify.on));
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") load(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") MC.actions.close(); });
// "/" goes to Find, unless you are typing somewhere
document.addEventListener("keydown", (e) => {
  if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey || /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "")) return;
  const box = document.querySelector(".find input");
  if (box) { e.preventDefault(); box.focus(); box.select(); }
});
$("#refresh").addEventListener("click", async () => {
  try { await api("/api/refresh", { method: "POST" }); banner("Refresh started: a few minutes. The page updates when it finishes.", "info"); }
  catch (e) { banner(e.message); }
  load();
});
load().then(() => {  // deep link: ?open=<item key> opens that item's details
  const key = new URLSearchParams(location.search).get("open");
  if (key) MC.actions.open(key);
});
loadJobs();
setInterval(load, 15000);
})();
