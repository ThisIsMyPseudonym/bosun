// Today: what needs you (one sentence + one verb each), overall health, and the Autopilot rail.
"use strict";
(() => {
const { el, api, plural } = MC;
const STALE_STEPS = new Set();

function rescanFromToday() {
  return api("/api/refresh", { method: "POST" })
    .then(() => { STALE_STEPS.clear(); if (MC.refreshNow) MC.refreshNow().catch(() => {}); })
    .catch((err) => { if (MC.banner) MC.banner(`Could not start re-scan: ${err.message}`); });
}

async function runTodayStep(staleKey, fn) {
  try {
    await fn();
  } catch (err) {
    STALE_STEPS.add(staleKey);
    if (MC.banner) MC.banner(`This step no longer applies: ${err.message}. Re-scan to refresh.`);
    MC.render();
  }
}

function failedEntry(e, lead, job) {
  const staleKey = `${lead.key}|${job.id}`;
  if (STALE_STEPS.has(staleKey)) {
    return { ...e, line: "This step no longer applies. Re-scan to refresh.", verb: "Re-scan", act: rescanFromToday };
  }
  const tri = job.triage;
  if (!tri || tri.status !== "done" || !tri.cause) {
    return { ...e, line: job.hint || "The last try failed.", verb: "Retry", act: () => MC.actions.upgrade([lead.key]) };
  }
  const step = tri.next_step || { kind: "none" };
  const base = { ...e, line: tri.cause };
  if (step.kind === "runbook" && step.runbook) {
    return { ...base, verb: "Open guide", act: () => MC.triageHelpers && MC.triageHelpers.openRunbook(step.runbook) };
  }
  if (step.kind === "recipe" && step.recipe) {
    return { ...base, verb: "Run fix", act: () => runTodayStep(staleKey, async () => {
      const r = await api("/api/terminal/run", { method: "POST", body: JSON.stringify({ key: lead.key, recipe: step.recipe }) });
      if (r && r.terminal && MC.termOpen) MC.termOpen(r.job, lead.key, e.name);
      if (MC.refreshNow) MC.refreshNow().catch(() => {});
    }) };
  }
  if (step.kind === "retry") {
    return { ...base, verb: "Try again", act: () => runTodayStep(staleKey, async () => {
      await api("/api/upgrade", { method: "POST", body: JSON.stringify({ keys: [lead.key] }) });
      if (MC.refreshNow) MC.refreshNow().catch(() => {});
    }) };
  }
  if (step.kind === "rescan") return { ...base, verb: "Re-scan", act: rescanFromToday };
  if (step.kind === "agent_prompt") {
    return { ...base, verb: "Details", act: () => { location.hash = MC.runHref(job.trace_id) || "#/runs"; } };
  }
  return { ...base, status: "Nothing to do" };
}
const HIGH = ["now", "plan", "watch"];
const queuedSet = (S) => new Set(S.autopilot.on ? S.autopilot.queued : []);
const runTime = (S) => S.autopilot.time ? `at ${S.autopilot.time}` : "on the next daily check";
// held by the waiting period: a release Bosun first saw too recently for an install nobody watches
const heldFor = (S, key) => S.autopilot.on ? (S.autopilot.waiting || []).find((w) => w.key === key) : null;
// when the wait ends; the install is at Autopilot's first daily check after that
const waitEnds = (w, days) => w.dated
  ? `at its first check after ${new Date(w.until * 1000).toLocaleString(undefined, { weekday: "long", hour: "numeric", minute: "2-digit" })}`
  : `once Bosun has seen it on offer for ${w.wait || `${days || 3} days`}`;

// ---------- what needs a human ----------
function machineEntry(S, p) {
  const lead = p.members[0], job = MC.jobFor(S, lead.key);
  const e = { kind: "machine", key: lead.key, name: p.name, kev: p.kev, risk: p.risk, item: lead,
              ver: `${p.installed} → ${p.available || "no fix yet"}`, open: () => MC.actions.open(lead.key) };
  if (job && job.status === "waiting") return { ...e, line: "In use right now. Bosun updates it the moment it's free.", status: "Waiting" };
  if (job && ["queued", "running", "succeeded"].includes(job.status)) return { ...e, line: "Updating now…", status: MC.jobLabel(job) };
  if (job && job.status === "failed") return failedEntry(e, lead, job);
  if (lead.malicious) return { ...e, line: "Reported as malware. Remove it, then change the keys and tokens on this machine.", verb: "Details", act: e.open };
  if (p.lane === "watch") return { ...e, line: "No fixed version yet. Bosun checks again every day.", status: "Watching" };
  // the same button the row and the details show; where there is none, one sentence says the next step
  const a = MC.itemAction(S, lead);
  if (a && lead.run === "msupdate-install") return { ...e, line: `${p.headline || "Security fixes."} Microsoft AutoUpdate has it ready: no password.`,
    verb: a.short, act: a.act };
  if (a && MC.withPassword(lead)) return { ...e, line: `${p.headline || "Security fixes."} ${MC.passwordHint()}.`, verb: a.short, act: a.act };
  if (lead.opens && (lead.unsupported || lead.ends_soon)) return { ...e, verb: "Details", act: e.open,  // the OS release itself
    line: `${lead.unsupported ? "No longer gets security fixes" : `Stops getting security fixes on ${lead.support_ends}`}. See your options.` };
  if (lead.unsupported) return { ...e, line: "No longer supported. Move to a maintained version, or remove it.", verb: "Details", act: e.open };
  if (a && lead.run && !lead.upgradable) return { ...e, line: "Needs your password. Bosun runs it here, in its own terminal, and you type it in.", verb: a.short, act: a.act };
  if (lead.manual) return { ...e, line: MC.itemWhy(lead), verb: a ? a.short : "Details", act: a ? a.act : e.open };
  if (p.lane === "plan") return { ...e, line: "Big update with security fixes. Check what changes first.", verb: "Details", act: e.open };
  const held = heldFor(S, lead.key);
  const auto = queuedSet(S).has(lead.key) ? ` Autopilot does it ${runTime(S)} otherwise.`
    : held && held.exploited ? ` It fixes a flaw attackers are using, but this release is new, so Autopilot holds it ${held.wait}: it installs it ${waitEnds(held, S.autopilot.wait_days)}. Update now to skip the hold.`
    : held ? ` This release is new, so Autopilot waits: it installs it ${waitEnds(held, S.autopilot.wait_days)}.` : "";
  return { ...e, line: `${p.headline || "Security fix, drop-in."}${auto}`, verb: "Update", act: () => MC.actions.upgrade([lead.key]) };
}

function projectEntry(S, sc) {
  const l = sc.sum.lanes, risky = [...l.now, ...l.plan];
  const work = l.plan.length;
  const fx = MC.latestFix(S, sc.items.map((i) => i.key));
  const e = { kind: "project", key: sc.id, name: sc.name, kev: sc.sum.kev > 0, risk: Math.max(0, ...risky.map((p) => p.risk)),
              line: `${plural(risky.length, "high-risk dependency", "high-risk dependencies")}${work ? `, ${work} need migration work` : ", all drop-in"}.`,
              open: () => { location.hash = MC.route.href("scope", sc.id); } };
  if (fx && ["queued", "running"].includes(fx.status)) return { ...e, line: "Claude Code is fixing these on a branch…", status: MC.FIX_LABEL[fx.status] };
  const malware = risky.filter((p) => p.members.some((m) => m.malicious)).length;
  const secrets = risky.filter((p) => p.source === "secret").length, deps = risky.length - secrets;
  const worst = risky.find((p) => p.kev && p.source !== "secret");
  const line = [worst ? `${worst.name} ${worst.installed} is ${MC.words("under active attack", "being attacked right now")}.` : "",
    malware ? `${plural(malware, "package")} reported as malware: remove ${malware === 1 ? "it" : "them"} now.` : "",
    secrets ? `${plural(secrets, "secret")} committed to the repo: rotate ${secrets === 1 ? "that key" : "those keys"}.` : "",
    deps - (worst ? 1 : 0) > 0 ? `${worst ? `${deps - 1} more` : deps} high-risk ${deps - (worst ? 1 : 0) === 1 ? "dependency" : "dependencies"}${work ? `, ${work} need${work === 1 ? "s" : ""} migration work` : ", all drop-in"}.`
      : worst && work ? "It needs migration work." : ""]
    .filter(Boolean).join(" ");
  return { ...e, line, verb: "Copy prompt", act: () => MC.actions.copyPrompt(risky.flatMap((p) => p.members).map((m) => m.key), sc.name) };
}

// a browser that has downloaded its update and only needs a restart
function relaunchEntry(S, r) {
  const job = (S.jobs || []).find((j) => j.item_key === `relaunch:${r.app}` && j.status === "running");
  return { kind: "machine", key: `relaunch:${r.app}`, name: r.name, kev: false, risk: 9, ver: `→ ${r.version}`,
           line: job ? "Restarting…" : "Update downloaded. Relaunch to finish; your tabs come back.",
           status: job ? "Restarting" : "", verb: job ? null : "Relaunch", act: () => MC.actions.relaunch(r), open: () => MC.actions.relaunch(r) };
}

// everything waiting in Windows Update / Software Update is one trip there, so one entry
function systemUpdatesEntry(pkgs) {
  const names = pkgs.map((p) => p.name);
  return { kind: "machine", key: "system-updates", name: MC.MACHINE.updates, kev: pkgs.some((p) => p.kev),
           risk: Math.max(...pkgs.map((p) => p.risk)), ver: plural(pkgs.length, "item"),
           line: `${names.slice(0, 2).join(" · ")}${names.length > 2 ? ` and ${names.length - 2} more` : ""}`,
           verb: `Open ${MC.MACHINE.updates}`, act: () => MC.actions.openUpdates(), open: () => { location.hash = "#/pc"; } };
}

async function decideTodayOffer(offer, approve) {
  try {
    await api(`/api/automations/firings/${offer.id}/decide`, {
      method: "POST", body: JSON.stringify({ approve: !!approve }),
    });
    if (MC.refreshNow) MC.refreshNow().catch(() => {});
  } catch (err) {
    if (MC.banner) MC.banner(`Could not ${approve ? "approve" : "decline"} offer: ${err.message}`);
    if (MC.refreshNow) MC.refreshNow().catch(() => {});
  }
}

const cleanOffer = (raw, limit = 80) => (MC.automationsHelpers && MC.automationsHelpers.cleanOfferText
  ? MC.automationsHelpers.cleanOfferText(raw, limit) : String(raw == null ? "" : raw).replace(/\s+/g, " ").trim().slice(0, limit));

function offerCard(offer, isDemo) {
  const expired = !!(offer.expired || offer.status === "expired" || (offer.deadline && Date.now() / 1000 >= offer.deadline));
  const ah = MC.automationsHelpers || {};
  const items = ah.offerItemsText ? ah.offerItemsText(offer.items, 4) : "";
  const target = ah.offerTargetText ? ah.offerTargetText(offer) : cleanOffer(offer.action, 40);
  const sub = [cleanOffer(offer.why || "Waiting for approval", 120), target].filter(Boolean).join(" · ");
  const stop = (fn) => (ev) => { ev.stopPropagation(); fn(); };
  return el("div", { class: `need need-offer${expired ? " need-offer-expired" : ""}` }, MC.glyph("bolt", "md"),
    el("div", { class: "body" },
      el("div", { class: "title" },
        el("strong", { class: "name", text: cleanOffer(offer.automation_name || `Automation #${offer.automation_id}`, 80) }),
        el("span", { class: "chip", text: expired ? "Expired" : "Automation offer" })),
      el("div", { class: "line", text: cleanOffer(offer.action_label || offer.action || "Run automation", 140) }),
      items ? el("div", { class: "line offer-sub", text: items }) : null,
      el("div", { class: "line offer-sub", text: expired ? "This offer expired after 24 hours and can no longer be approved." : sub })),
    el("div", { class: "offer-btns" },
      el("button", { class: "quiet", disabled: isDemo || expired || null, text: "Decline", onclick: stop(() => decideTodayOffer(offer, false)) }),
      el("button", { class: "primary", disabled: isDemo || expired || null, text: "Approve", onclick: stop(() => decideTodayOffer(offer, true)) })));
}

MC.todayHelpers = { machineEntry, failedEntry, offerCard, decideTodayOffer };

MC.needsYou = function needsYou(S, scopes) {
  const staged = S.relaunch || [], covered = new Set(staged.flatMap((r) => r.keys));
  const high = HIGH.flatMap((l) => scopes.pc.sum.lanes[l]).filter((p) => !p.members.some((m) => covered.has(m.key)));
  const viaOs = (p) => p.members[0].opens && !p.members[0].unsupported && !p.members[0].ends_soon;
  const system = high.filter(viaOs);
  const machine = [...staged.map((r) => relaunchEntry(S, r)), ...(system.length ? [systemUpdatesEntry(system)] : []),
    ...high.filter((p) => !viaOs(p)).map((p) => machineEntry(S, p))];
  const projects = scopes.active.filter((sc) => sc.sum.counts.now + sc.sum.counts.plan > 0).map((sc) => projectEntry(S, sc));
  return [...machine, ...projects].sort((a, b) => (b.kev - a.kev) || (b.risk - a.risk) || a.name.localeCompare(b.name));
};

// `filled`: the one mint button in the panel goes to the most urgent entry; the rest are outlined
function needRow(n, filled) {
  const stop = (fn) => (ev) => { ev.stopPropagation(); fn(ev); };
  return el("div", { class: "need", onclick: n.open },
    n.kind === "project" ? MC.glyph("folder", `md${n.kev ? " warm" : ""}`) : n.item ? MC.tile(n.item, "md") : MC.glyph("machine", "md"),
    el("div", { class: "body" },
      el("div", { class: "title" }, el("button", { class: "pkgname name", "data-fk": `need:${n.key}`, title: n.kind === "project" ? "Open the project" : "Details",
          text: n.name, onclick: stop(n.open) }),
        n.kind !== "machine" ? el("span", { class: "chip", text: "Project" }) : MC.SIMPLE ? null : el("span", { class: "ver", text: n.ver }),
        n.kev ? el("span", { class: "chip kev", text: MC.words("Exploited", "Under attack") }) : n.risk >= 4 ? MC.sev(n.risk) : null,
        n.item ? MC.backChip(n.item) : null),
      el("div", { class: "line", text: n.line })),
    n.verb ? el("button", { class: filled ? "primary" : "quiet", text: n.verb, onclick: stop(n.act) })
      : el("span", { class: "status", text: n.status || "" }));
}

function needsPanel(S, scopes, needs) {
  const offers = S.automationOffers || [];
  const shown = S.needsAll ? needs : needs.slice(0, 6);
  const first = shown.findIndex((n) => n.verb);
  return el("section", { class: "panel needs" },
    el("div", { class: "sec" }, el("h3", { text: "Needs you" }),
      needs.length ? el("button", { class: "link accent", text: "What should I do first?", title: "Ask Bosun",
        onclick: () => MC.ask("What should I do first?", { page: "today" }) }) : null),
    offers.length ? el("div", { class: "need-offers" }, ...offers.map((o) => offerCard(o, !!S.demo))) : null,
    needs.length ? el("div", {}, ...shown.map((n, i) => needRow(n, !offers.length && i === first)))
      : offers.length ? null : el("p", { class: "empty", text: "Nothing urgent. Bosun will say when something is." }),
    needs.length > shown.length ? el("button", { class: "link", text: `Show all ${needs.length}`, onclick: () => { S.needsAll = true; MC.render(); } }) : null);
}

// ---------- this machine, by what each update asks of you ----------
const names = (pkgs, n = 8) => `${pkgs.slice(0, n).map((p) => p.name).join(", ")}${pkgs.length > n ? ` and ${pkgs.length - n} more` : ""}`;

// One row per way of updating. A way with a single item shows that item and its own button; with several, the
// group and the one button that covers them (or a way to see them, where each needs its own click).
MC.machineDigest = function machineDigest(S, scopes) {
  const pkgs = MC.groupByPackage(scopes.pc.items.filter((i) => i.lane !== "dormant"));
  const rows = [];
  const goThere = (id) => () => { location.hash = MC.route.href("scope", "pc"); setTimeout(() => document.getElementById(`group-${id}`)?.scrollIntoView(), 80); };
  for (const r of MC.ROUTES) {
    const mine = pkgs.filter((p) => MC.routeOf(S, p.members[0]) === r.id);
    if (!mine.length) continue;
    const info = MC.routeInfo(r.id);
    for (const part of MC.suites ? MC.suites(S, mine) : mine.map((p) => ({ one: p }))) rows.push({ route: r.id, info, ...part, all: mine });
  }
  // fold the single items of one route back into a group row when there is more than one of them
  const out = [];
  for (const r of MC.ROUTES) {
    const mine = rows.filter((x) => x.route === r.id);
    const singles = mine.filter((x) => x.one), suites = mine.filter((x) => x.suite);
    if (singles.length === 1) out.push({ ...singles[0], kind: "item" });
    else if (singles.length) out.push({ route: r.id, info: singles[0].info, kind: "group", pkgs: singles.map((x) => x.one), go: goThere(r.id) });
    out.push(...suites.map((x) => ({ ...x, kind: "suite", go: goThere(r.id) })));
  }
  return { pkgs, rows: out };
};

function digestRow(S, row) {
  const btn = (label, act, title) => el("button", { class: "quiet", text: label, title: title || null, onclick: act });
  if (row.kind === "item") {
    const p = row.one, lead = p.members[0], a = MC.itemAction(S, lead), job = MC.jobFor(S, lead.key);
    const busy = job && ["waiting", "queued", "running", "succeeded"].includes(job.status);
    return el("div", { class: "macrow" }, MC.tile(lead, "md"),
      el("div", { class: "mb" }, el("div", { class: "t" }, el("button", { class: "pkgname", "data-fk": `mac:${lead.key}`, title: "Details", text: p.name,
          onclick: () => MC.actions.open(lead.key) }), MC.backChip(lead)),
        el("div", { class: "d", text: busy ? "Updating now…" : a ? a.how : row.route === "watch" ? row.info.why : MC.itemWhy(lead) || row.info.why })),
      busy ? el("span", { class: "status", text: MC.jobLabel(job) }) : a ? btn(a.short, a.act)
        : row.route === "watch" ? el("span", { class: "state", text: "No fix yet" }) : btn("Details", () => MC.actions.open(lead.key)));
  }
  const suite = row.kind === "suite" ? row.suite : null;
  const pkgs = suite ? suite.pkgs : row.pkgs, members = pkgs.flatMap((p) => p.members);
  const bulk = MC.bulkAction(S, members.filter((m) => ["now", "batch"].includes(m.lane)), !suite);
  const action = row.route === "system" ? btn(`Open ${MC.MACHINE.updates}`, () => MC.actions.openUpdates())
    : bulk ? btn(bulk.label, bulk.act, bulk.title)
    : row.route === "watch" ? el("span", { class: "state", text: "No fix yet" }) : btn("Show", row.go, `See these on ${MC.MACHINE.label}`);
  const what = suite && suite.tools ? "tools" : MC.kindOf(members[0]) === "app" || MC.kindOf(members[0]) === "browser" ? "apps" : "items";
  return el("div", { class: "macrow" },
    suite && suite.tools ? MC.glyph("term", "md")
      : suite ? el("span", { class: "stack2 md", "aria-hidden": "true" }, MC.tile(pkgs[0].members[0], "sm"), MC.tile(pkgs[1].members[0], "sm"))
      : MC.glyph(row.route === "bosun" ? "bolt" : row.route === "system" ? "machine" : MC.glyphFor(members[0]), "md"),
    el("div", { class: "mb" }, el("div", { class: "t" }, suite ? suite.name : row.info.name, el("span", { class: "n", text: suite ? `· ${pkgs.length} ${what}` : `· ${pkgs.length}` })),
      el("div", { class: "d", title: names(pkgs, 40), text: suite && suite.tools ? "Small programs other software relies on. Safe to keep current."
        : suite ? `${row.info.name}. The same update for all of them.` : names(pkgs) })),
    action);
}

function machinePanel(S, scopes) {
  const { pkgs, rows } = MC.machineDigest(S, scopes);
  const aside = MC.groupByPackage(scopes.pc.aside || []);
  if (!pkgs.length && !aside.length) return null;
  const kev = pkgs.filter((p) => p.kev).length, high = pkgs.filter((p) => !p.kev && p.risk >= 7).length;
  const risk = kev ? `${kev} ${MC.words("exploited", "under attack")}` : high ? `${high} ${MC.words("high risk", "urgent")}` : "nothing risky";
  const jumps = pkgs.filter((p) => MC.bigJump(p.members[0])).length;
  return el("section", { class: "panel macpanel" },
    el("div", { class: "sec" }, el("h3", {}, MC.MACHINE.label, el("span", { class: "n", text: pkgs.length ? ` · ${pkgs.length} waiting, ${risk}${jumps ? `, ${jumps} big jump${jumps === 1 ? "" : "s"}` : ""}` : " · nothing waiting" })),
      el("a", { class: "seclink", href: MC.route.href("scope", "pc"), text: `Open ${MC.MACHINE.label}` })),
    ...rows.map((r) => digestRow(S, r)),
    aside.length ? el("div", { class: "macfoot" }, el("span", { text: `${aside.length} ${MC.words("acknowledged", "set aside")}: ${names(aside, 6)}` }),
      el("a", { class: "seclink", href: MC.route.href("scope", "pc"), text: "Show",
        onclick: () => { MC.fold.set("pc:aside", true); setTimeout(() => document.getElementById("group-aside")?.scrollIntoView(), 80); } })) : null);
}

// ---------- health: everything open, by who deals with it ----------
function health(S, scopes) {
  const seg = { kev: 0, review: 0, bosun: 0, optional: 0, aside: 0 };
  const sets = MC.SIMPLE ? [scopes.pc] : [scopes.pc, ...scopes.active];
  for (const sc of sets) {
    for (const p of MC.groupByPackage(sc.items.filter((i) => i.lane !== "dormant"))) {
      const k = p.kev ? "kev" : HIGH.includes(p.lane) ? "review" : !p.project && MC.bulkMode(S, p.members[0]) === "upgrade" ? "bosun" : "optional";
      seg[k] += 1;
    }
    seg.aside += MC.groupByPackage(sc.aside || []).length;
  }
  const day = Date.now() / 1000 - 86400;
  const updated = (S.jobs || []).filter((j) => j.status === "succeeded" && (j.finished || 0) > day).length;
  const total = seg.kev + seg.review + seg.bosun + seg.optional;
  const parts = [["kev", MC.words("exploited", "under attack"), "c-brick"], ["review", MC.words("high risk", "need a look"), "c-ochre"],
    ["bosun", "Bosun can install", "c-teal"], ["optional", "routine", "c-teal-soft"], ["aside", MC.words("acknowledged", "set aside"), "c-quiet"]];
  const projects = MC.SIMPLE ? 0 : scopes.active.length;
  // findings as well as updates: a committed secret or an unpinned server is open, but it is not an update
  return el("section", { class: "panel health" },
    el("div", { class: "numline" }, el("span", { class: "big", text: String(total) }),
      el("span", { text: `open on ${MC.MACHINE.label === "This Mac" ? "this Mac" : MC.MACHINE.label === "This PC" ? "this PC" : "this computer"}${projects ? ` and in ${plural(projects, "project")}` : ""}${MC.SIMPLE ? "" : ": updates and findings"}` })),
    total + seg.aside ? el("div", { class: "stack", role: "img", "aria-label": parts.map(([k, l]) => `${seg[k]} ${l}`).join(", ") },
      ...parts.filter(([k]) => seg[k]).map(([k, l, c]) => el("i", { class: c, style: { flexGrow: String(seg[k]) }, title: `${seg[k]} ${l}` }))) : null,
    el("div", { class: "legend" }, ...parts.filter(([k]) => seg[k] || k !== "aside").map(([k, l, c]) => el("span", { class: "key" }, el("b", { class: c }), el("strong", { text: String(seg[k]) }), l)),
      el("span", { class: "spacer" }), el("span", { text: `${updated} updated in the last 24 h` })));
}

// ---------- right rail ----------
function autopilotCard(S) {
  const a = S.autopilot, n = a.queued.length;
  const day = Date.now() / 1000 - 86400;
  const recent = (S.jobs || []).filter((j) => (j.finished || 0) > day);
  const ok = recent.filter((j) => j.status === "succeeded").length, bad = recent.filter((j) => j.status === "failed").length;
  const toggle = (on) => el("button", { class: a.on === on ? "on" : null, "aria-pressed": String(a.on === on),
    text: on ? "On" : "Off", onclick: () => MC.actions.setAutopilot(on) });
  return el("section", { class: `autopilot${a.on ? "" : " off"}` },
    el("div", { class: "aphead" }, el("span", { text: "Autopilot" }), el("div", { class: "switch", role: "group", "aria-label": "Autopilot" }, toggle(true), toggle(false))),
    el("div", { class: "apsub", text: `Drop-in updates, daily ${a.time ? `at ${a.time}` : "with the refresh"}` }),
    a.on ? el("div", {}, el("div", { class: "big", text: String(n) }), el("div", { class: "apline", text: `queued for ${a.time || "the next run"}` }))
      : el("p", { class: "apline", text: n ? `Turn on to install ${plural(n, "safe update")} without asking.` : "Turn on to install safe updates without asking." }),
    waitingLine(a),
    handsOff(a),
    el("div", { class: "aprun" }, MC.icon("check", "currentColor", 2.6),
      el("span", { text: recent.length ? `Last 24 h: ${ok} updated, ${bad} failed` : "Nothing installed in the last 24 h" })));
}

// new releases Autopilot is holding back for a few days, in case one is withdrawn
function waitingLine(a) {
  const held = a.on ? a.waiting || [] : [];
  if (!held.length) return null;
  return el("div", { class: "apwait", title: held.map((w) => `${w.name}: ${waitEnds(w, a.wait_days)}`).join("\n") },
    el("span", { text: `${plural(held.length, "new release")} waiting ${new Set(held.map((w) => w.wait)).size === 1
      ? `${held[0].wait || `${a.wait_days || 3} days`} ` : ""}before installing` }),
    el("button", { class: "apbtn", text: "Update now",
      onclick: () => MC.actions.upgradeKeys(held.map((w) => w.key), held.map((w) => w.name)) }));
}

// updates Autopilot stopped trying because they need a person there (an admin prompt, a password)
function handsOff(a) {
  const left = a.hands_off || [];
  if (!left.length) return null;
  const where = MC.MACHINE.kind === "mac" ? "at the Mac" : "at the PC";
  return el("div", { class: "aphands", title: left.map((h) => `${h.name}: ${h.reason}`).join("\n") },
    el("span", { text: `${plural(left.length, "update")} need${left.length === 1 ? "s" : ""} you ${where}` }),
    el("button", { class: "apbtn", text: left.length === 1 ? "Update it" : "Update these",
      onclick: () => MC.actions.upgradeKeys(left.map((h) => h.key), left.map((h) => h.name)) }));
}

function recentCard(S) {
  const name = (key) => MC.nameOf(S, key);
  const tone = { succeeded: "ok", fixed: "ok", partial: "ok", failed: "bad", abandoned: "bad",
                  waiting: "busy", queued: "busy", running: "busy", needs_human: "busy" };
  const rows = [...(S.jobs || []), ...(S.fixes || [])].sort((a, b) => b.started - a.started).slice(0, 6);
  const fix = (r) => "branch" in r || "worktree" in r;  // a Claude Code fix, not an update
  const did = (r) => (fix(r) ? { fixed: "Fixed", partial: "Partly fixed", failed: "Fix failed", needs_human: "Fix needs you", queued: "Fix queued",
      running: "Fixing", no_changes: "No change needed", discarded: "Discarded" }
    : { succeeded: "Updated", failed: "Failed", abandoned: "Gave up", waiting: "Waiting", queued: "Queued", running: "Updating" })[r.status] || r.status;
  return el("section", { class: "panel recent" },
    el("div", { class: "sec" }, el("h3", { text: "Recent" }),
      rows.length ? el("button", { class: "link accent", text: "All", onclick: () => MC.actions.showActivity() }) : null),
    rows.length ? el("div", {}, ...rows.map((r) => el(r.trace_id ? "a" : "div", { class: "rr", href: MC.runHref(r.trace_id) },
      el("span", { class: `dot ${tone[r.status] || ""}`, title: r.status }),
      el("span", { class: "rname" }, el("span", { class: `rverb ${tone[r.status] || ""}`, text: `${did(r)} ` }), name(r.item_key)),
      el("span", { class: "tm", text: MC.clock(r.finished || r.started) }))))
      : el("p", { class: "empty", text: "Updates and fixes show up here." }));
}

function greeting() {
  const h = new Date().getHours();
  return h < 5 ? "Good evening." : h < 12 ? "Good morning." : h < 18 ? "Good afternoon." : "Good evening.";
}

MC.views.today = function today(S, scopes) {
  const all = MC.needsYou(S, scopes);
  const hiddenProjects = MC.SIMPLE ? all.filter((n) => n.kind === "project").length : 0;
  const needs = MC.SIMPLE ? all.filter((n) => n.kind !== "project") : all;  // the simple view has no project pages to send you to
  const q = S.autopilot.on ? S.autopilot.queued.length : 0;
  const { pkgs } = MC.machineDigest(S, scopes);
  const can = pkgs.filter((p) => MC.bulkMode(S, p.members[0]) === "upgrade").length;
  const kev = needs.filter((n) => n.kev).length;
  const here = MC.MACHINE.label.replace(/^This/, "this");
  const sub = [needs.length ? `${plural(needs.length, "thing")} need${needs.length === 1 ? "s" : ""} you${kev ? `: ${kev === 1 ? "one is" : `${kev} are`} ${MC.words("under active attack", "being attacked")}` : ""}.` : "Nothing urgent.",
    pkgs.length ? `${here[0].toUpperCase()}${here.slice(1)} has ${plural(pkgs.length, "update")} waiting${can ? `; Bosun can install ${can === pkgs.length ? (can === 1 ? "it" : "all of them") : can} right now` : ""}.` : "",
    q ? `Autopilot installs ${q} ${runTime(S)}.` : ""].filter(Boolean).join(" ");
  const days = new Set((S.history || []).filter((h) => h.lanes).map((h) => new Date(h.ts * 1000).toDateString())).size;
  const trend = days >= 2 ? MC.views.trendChart(S) : null;  // one point is not a trend: no empty card
  const project = MC.SIMPLE ? null : MC.views.projectChart(scopes);
  return el("div", { class: "today" },
    el("div", { class: "main" },
      el("div", { class: "hello" }, el("h1", { text: greeting() }), el("p", { text: sub })),
      needsPanel(S, scopes, needs), machinePanel(S, scopes),
      hiddenProjects ? el("p", { class: "hint", text: `${plural(hiddenProjects, "code project")} ${hiddenProjects === 1 ? "has" : "have"} findings. Switch to developer view to see ${hiddenProjects === 1 ? "it" : "them"}.` }) : null,
      health(S, scopes),
      trend || project ? el("div", { class: `charts${trend && project ? "" : " one"}` }, trend, project) : null),
    el("aside", { class: "rail" }, autopilotCard(S), recentCard(S)));
};
})();
