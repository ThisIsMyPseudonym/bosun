// Shared finding rows + the cross-project Packages view.
"use strict";
(() => {
const { el, sev, plural } = MC;
const stop = (fn) => (e) => { e.stopPropagation(); fn(e); };
const REACH_CHIP = { remote: "remote", remote_auth: "remote, login", adjacent: "same network", needs_action: "needs your action",
                     local: "local only", physical: "physical only" };

function chips(p, machine) {
  const all = (pred) => p.members.every(pred), any = (pred) => p.members.some(pred);
  return [
    any((m) => m.malicious) && el("span", { class: "chip kev", title: "Reported as a malicious package", text: "Malware" }),
    p.kev && el("span", { class: "chip kev", text: MC.words("Exploited", "Attackers are using this") }),
    any((m) => m.unsupported) && el("span", { class: "chip kev", text: MC.words("End of life", "No longer supported") }),
    any((m) => m.ends_soon) && el("span", { class: "chip effort", text: MC.words("EOL soon", "Support ending soon") }),
    any((m) => m.floating) && el("span", { class: "chip", text: "floating tag" }),
    p.effort === "high" && el("span", { class: "chip effort", text: MC.words("migration work", "big change") }),
    !p.project && p.effort !== "high" && (p.lane === "plan" || MC.bigJump(p.members[0])) && el("span", { class: "chip effort",
      title: MC.bigJump(p.members[0]) ? MC.jumpNote(p.members[0]) : "A big jump: check what changes first", text: "big jump" }),
    p.project && all((m) => m.dev === true) && el("span", { class: "chip", text: "dev only" }),
    p.project && all((m) => m.direct === false) && el("span", { class: "chip", text: "transitive" }),
    p.project && all((m) => m.fix_kind === "lockfile refresh") && el("span", { class: "chip", text: "lockfile refresh" }),
    // the summary usually says "Fixes 3 known vulnerabilities" already; the chip only adds a count when it doesn't
    p.n_fixed > 0 && !/vulnerabilit/i.test(p.headline || "") && el("span", { class: "chip", text: MC.words(`${plural(p.n_fixed, "vuln")} fixed`, "security fix") }),
    !machine && !p.project && MC.heldBack(p.members[0]) && el("span", { class: "chip", title: p.members[0].manual, text: "not offered yet" }),
    !p.project && p.members[0].updater === "confirmed" && el("span", { class: "chip", title: `${p.members[0].updater_name || "The app's own updater"} has this update`, text: "confirmed by its updater" }),
    !machine && !p.project && p.members[0].manual && !MC.heldBack(p.members[0]) && !p.action && el("span", { class: "chip effort", title: p.members[0].manual, text: MC.words("you do this", "needs you") }),
    !machine && !p.project && p.members[0].self_updates && el("span", { class: "chip", title: "Has its own updater", text: "updates itself" }),
    !p.project && p.members[0].upgradable && ["hardware", "driver", "firmware"].includes(MC.kindOf(p.members[0]))
      && el("span", { class: "chip", title: "Drivers and hardware tools can reset device settings or need a restart, so Autopilot leaves them to you", text: "asks first" }),
    !MC.SIMPLE && p.reach && REACH_CHIP[p.reach] && el("span", { class: `chip reach-${p.reach}`, title: "How the most exposed vulnerability is reached", text: REACH_CHIP[p.reach] }),
  ];
}

// A small menu that opens under a button (the row's "⋯", the details' "Not now"). One at a time; it lives outside
// the page's main element, so the periodic re-render doesn't close it. `items`: { label, note, act } | "sep" |
// { head } for a caption. Arrow keys move, Escape or a click elsewhere closes, focus returns to the button.
let openMenu = null;
MC.closeMenu = function closeMenu(refocus) {
  if (!openMenu) return;
  const { box, anchor, off } = openMenu;
  openMenu = null;
  box.remove(); off();
  if (anchor.isConnected) { anchor.setAttribute("aria-expanded", "false"); if (refocus) anchor.focus(); }
};
MC.menu = function menu(anchor, items) {
  const again = openMenu && openMenu.anchor === anchor;
  MC.closeMenu();
  if (again) return;
  const box = el("div", { class: "menu", role: "menu" }, ...items.filter(Boolean).map((it) =>
    it === "sep" ? el("hr", {}) : it.head ? el("div", { class: "mh", text: it.head })
    : el("button", { class: "mi", role: "menuitem", disabled: !!it.disabled, title: it.title || null,
        onclick: () => { MC.closeMenu(true); it.act(); } },
        el("span", { text: it.label }), it.note ? el("small", { text: it.note }) : null)));
  document.body.append(box);
  const r = anchor.getBoundingClientRect(), w = box.offsetWidth, h = box.offsetHeight;
  const below = r.bottom + 6 + h <= window.innerHeight || r.top < h + 12;
  Object.assign(box.style, { left: `${Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8)) + window.scrollX}px`,
    top: `${(below ? r.bottom + 6 : r.top - h - 6) + window.scrollY}px` });
  const mis = () => [...box.querySelectorAll(".mi:not(:disabled)")];
  const key = (e) => {
    if (e.key === "Escape") { e.stopPropagation(); MC.closeMenu(true); return; }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const all = mis(), at = all.indexOf(document.activeElement);
    (e.key === "Home" ? all[0] : e.key === "End" ? all[all.length - 1]
      : all[(at + (e.key === "ArrowDown" ? 1 : -1) + all.length) % all.length]).focus();
  };
  const away = (e) => { if (!box.contains(e.target) && !anchor.contains(e.target)) MC.closeMenu(); };
  box.addEventListener("keydown", key);
  document.addEventListener("mousedown", away, true);
  window.addEventListener("hashchange", away);
  anchor.setAttribute("aria-expanded", "true");
  openMenu = { box, anchor, off: () => { document.removeEventListener("mousedown", away, true); window.removeEventListener("hashchange", away); } };
  (mis()[0] || box).focus();
};

// What you can decide about an item without updating it. The same list is the row's "⋯" and the details' "Not now".
// Putting an item away never needs a confirmation: it is one click to bring back, and it comes back by itself
// when it gets riskier. Only you can do this; no agent and not the assistant.
MC.notNow = function notNow(S, lead) {
  const st = MC.standing(lead), key = lead.key;
  const inDays = (n) => MC.day(Date.now() / 1000 + n * 86400);
  if (st.state) return [{ label: st.state === "ignored" ? "Stop ignoring" : "Bring back", note: "into the list", act: () => MC.actions.setState(key, "open") }];
  return [
    { head: "Not now" },
    { label: MC.words("Acknowledge", "Got it"), note: "until it gets riskier",
      title: "Seen it, not acting now. Off Today, the counts and notifications. A newer version does not bring it back; it comes back if it becomes exploited, is reported as malware, or reaches high risk. Autopilot still installs it.",
      act: () => MC.actions.setState(key, "ack") },
    { label: MC.words("Snooze for a week", "Remind me in a week"), note: `back ${inDays(7)}`, title: "Autopilot waits too", act: () => MC.actions.setState(key, "snoozed", 7) },
    { label: MC.words("Snooze for a month", "Remind me in a month"), note: `back ${inDays(30)}`, title: "Autopilot waits too", act: () => MC.actions.setState(key, "snoozed", 30) },
    lead.available && !MC.SIMPLE && !MC.isFinding(lead) ? { label: "Skip this version", note: "back at the next one",
      title: `Hide ${lead.available}; any other version brings it back. Autopilot waits too.`, act: () => MC.actions.setState(key, "skip") } : null,
    "sep",
    { label: MC.words("Ignore for good", "Don't show this again"), note: "unless it is attacked",
      title: "Never shown, never installed by Autopilot. It still comes back if it becomes exploited or is reported as malware.",
      act: () => MC.actions.setState(key, "ignored") },
  ];
};
MC.itemMenu = function itemMenu(S, lead) {
  const pref = S.prefs[lead.key] || {};
  const verbs = MC.notNow(S, lead);
  const auto = lead.upgradable && !MC.SIMPLE ? { label: "Update automatically", note: pref.auto_upgrade ? "on" : "off",
    title: "Install this one's updates on each daily check, also when Autopilot is off",
    act: () => MC.actions.setPref(lead.key, { auto_upgrade: !pref.auto_upgrade }) } : null;
  // "Update automatically" sits above "Ignore for good", after the not-now verbs
  const cut = verbs.lastIndexOf("sep");
  return [{ label: "Details", act: () => MC.actions.open(lead.key) }, cut < 0 ? "sep" : null,
    ...(cut < 0 ? [...verbs, auto] : [...verbs.slice(0, cut + 1), auto, ...verbs.slice(cut + 1)])];
};

// An item that came back by itself says why, and what you had said: "Back: now exploited"
MC.backChip = function backChip(i) {
  const st = MC.standing(i);
  if (!st.back) return null;
  return el("span", { class: "chip kev", title: `You ${MC.saidPast(st)}${st.at ? ` ${MC.day(st.at)}` : ""}.`, text: `Back: ${st.back}` });
};

// The tile at the start of a row: the app's own icon where this machine has one (the server read it from the
// app's bundle; `icon` says so), else a neutral glyph by kind. The image is this origin's, behind the session.
MC.tile = function tile(i, cls) {
  const glyph = MC.glyph(MC.glyphFor(i), cls);
  if (!i.icon || i.project) return glyph;
  const img = el("img", { class: `ic${cls ? ` ${cls}` : ""}`, alt: "", width: "28", height: "28", decoding: "async",
    src: `/api/icon?key=${encodeURIComponent(i.key)}&v=${encodeURIComponent(i.installed || "")}` });
  img.addEventListener("error", () => img.replaceWith(glyph), { once: true });  // gone since the page loaded: the glyph
  return img;
};

// What stands where the button would be while there is nothing to click: a job under way, or why there is none.
function standIn(S, lead, route) {
  const job = MC.jobFor(S, lead.key);
  if (job && ["waiting", "queued", "running", "succeeded"].includes(job.status)) {
    return [el("span", { class: `status job-${job.status}`, title: job.hint || job.output || "", text: MC.jobLabel(job) }),
      job.status === "waiting" ? el("button", { class: "link", text: "Cancel", onclick: stop(() => MC.actions.cancelJob(job.id)) }) : null];
  }
  if (route === "watch") return [el("span", { class: "state", text: "No fix yet" })];
  if (route === "system") return [];  // one trip to the system's updater covers them: its button is on the group
  if (MC.heldBack(lead)) return [el("span", { class: "state", title: lead.manual, text: "Not offered yet" })];
  return [el("button", { class: "quiet", text: "Details", onclick: stop(() => MC.actions.open(lead.key)) })];
}

// A machine item: tile, name, version, one line, one button, "⋯". `ctx.scope` "peer" is another machine's
// finding: read-only, so no button and no menu.
function machineRow(S, p, ctx) {
  const lead = p.members[0], peer = ctx.scope === "peer";
  const route = peer ? "" : MC.routeOf(S, lead);
  const job = peer ? null : MC.jobFor(S, lead.key);
  const action = peer ? null : MC.itemAction(S, lead);
  const failed = job && job.status === "failed";
  const pref = S.prefs[lead.key] || {};
  const acts = el("div", { class: "acts" });
  const away = ctx.aside ? MC.standing(lead) : null;
  if (away) {
    acts.append(el("button", { class: "small", text: away.state === "ignored" ? "Stop ignoring" : "Bring back",
        onclick: stop(() => MC.actions.setState(lead.key, "open")) }),
      el("button", { class: "morebtn", "aria-label": `More for ${p.name}`, "aria-haspopup": "menu", "aria-expanded": "false",
        "data-fk": `more:${lead.key}`, onclick: stop((e) => MC.menu(e.currentTarget, MC.itemMenu(S, lead))) }, MC.icon("more", "currentColor", 2.6)));
  } else if (!peer) {
    acts.append(...(action ? [el("button", { class: "quiet", text: action.short, title: action.title, onclick: stop(action.act) })]
      : standIn(S, lead, route)).filter(Boolean),
      el("button", { class: "morebtn", "aria-label": `More for ${p.name}`, "aria-haspopup": "menu", "aria-expanded": "false",
        "data-fk": `more:${lead.key}`, onclick: stop((e) => MC.menu(e.currentTarget, MC.itemMenu(S, lead))) }, MC.icon("more", "currentColor", 2.6)));
  }
  const name = peer ? el("span", { class: "pkgname", text: p.name })
    : el("button", { class: "pkgname", "data-fk": `row:${lead.key}`, title: "Details", text: p.name, onclick: stop(() => MC.actions.open(lead.key)) });
  return el("div", { class: `row mach${peer ? " peer" : ""}`, onclick: peer ? null : () => MC.actions.open(lead.key) },
    MC.tile(lead),
    el("div", { class: "nm" },
      el("div", { class: "pkg" }, name, p.risk >= 4 ? sev(p.risk) : null, peer ? null : MC.backChip(lead), ...chips(p, true),
        !peer && pref.auto_upgrade ? el("span", { class: "chip", title: "Updates automatically on each daily check", text: "auto" }) : null),
      el("div", { class: "eco", text: whatItIs(p) })),
    el("div", { class: "ver" }, ...versionCell(p)),
    away ? whenCell(away) : el("div", { class: `head${failed ? " bad" : ""}`, title: failed ? job.hint || "" : headline(p),
      text: failed ? `Last try failed. ${job.hint || "Open Details to see why."}` : headline(p) }),
    acts);
}

// In the Acknowledged section: what you said and when, and what brings it back
function whenCell(st) {
  const [verb, rest] = MC.saidText(st);
  const then = st.state === "ack" ? "back if it gets riskier" : st.state === "ignored" ? "back only if it is attacked"
    : st.until_version ? "back at the next version; Autopilot waits too" : "Autopilot waits too";
  return el("div", { class: "when" }, el("b", { text: verb }), ` ${rest} · ${then}`);
}

// A suite: one row for a product family, with one button for all of it, that opens into its members (ordinary
// rows, each with its own button and menu). `key` remembers whether it is open.
MC.suiteRow = function suiteRow(S, suite, key, forceOpen) {
  const pkgs = suite.pkgs, lead = pkgs[0], members = pkgs.flatMap((p) => p.members);
  const isOpen = forceOpen || MC.fold.open(key, false);  // Find opens a suite whose member matches
  const bulk = MC.bulkAction(S, members, false);
  const short = (n) => (n.startsWith(`${suite.name.split(" ")[0]} `) ? n.slice(suite.name.split(" ")[0].length + 1) : n);
  const kidsId = `kids-${key.replace(/[^a-z0-9]+/gi, "-")}`;
  const toggle = () => { MC.fold.set(key, !isOpen); MC.render(); };
  const worst = Math.max(...pkgs.map((p) => p.risk));
  const menu = [{ label: isOpen ? "Hide them" : `Show all ${pkgs.length}`, act: toggle }, { head: `Not now, all ${pkgs.length}` },
    { label: MC.words("Acknowledge", "Got it"), note: "until it gets riskier", act: () => MC.actions.setStateMany(members.map((m) => m.key), "ack") },
    { label: MC.words("Snooze for a week", "Remind me in a week"), note: "Autopilot waits too", act: () => MC.actions.setStateMany(members.map((m) => m.key), "snoozed", 7) },
    { label: MC.words("Snooze for a month", "Remind me in a month"), note: "Autopilot waits too", act: () => MC.actions.setStateMany(members.map((m) => m.key), "snoozed", 30) }];
  const row = el("div", { class: "row mach suite", onclick: toggle },
    suite.tools ? MC.glyph("term") : el("span", { class: "stack2", "aria-hidden": "true" }, MC.tile(pkgs[0].members[0], "sm"), MC.tile(pkgs[1].members[0], "sm")),
    el("div", { class: "nm" },
      el("div", { class: "pkg" }, el("button", { class: "pkgname", "data-fk": `suite:${key}`, "aria-expanded": String(isOpen), "aria-controls": kidsId,
          text: suite.name, onclick: stop(toggle) }),
        el("span", { class: "chip", text: `${pkgs.length} ${suite.tools ? "tools" : MC.kindOf(lead) === "app" || MC.kindOf(lead) === "browser" ? "apps" : "items"} ${isOpen ? "▾" : "▸"}` }),
        worst >= 4 ? sev(worst) : null),
      el("div", { class: "eco", text: pkgs.map((p) => short(p.name)).join(", ") })),
    el("div", { class: "ver" }, ...(suite.tools ? [`${pkgs.length} updates ready`] : versionCell(lead))),
    el("div", { class: "head", text: suite.tools ? "Small programs other software relies on. Safe to keep current."
      : MC.SIMPLE ? "The same update for all of them." : `The same update for all ${pkgs.length}. ${headline(lead)}`, title: suite.tools ? "" : headline(lead) }),
    el("div", { class: "acts" },
      bulk ? el("button", { class: "quiet", text: bulk.label, title: bulk.title, onclick: stop(bulk.act) })
        : el("button", { class: "quiet", text: isOpen ? "Hide" : "Show", title: "Each of these needs its own click", onclick: stop(toggle) }),
      el("button", { class: "morebtn", "aria-label": `More for ${suite.name}`, "aria-haspopup": "menu", "aria-expanded": "false",
        "data-fk": `more:${key}`, onclick: stop((e) => MC.menu(e.currentTarget, menu)) }, MC.icon("more", "currentColor", 2.6))));
  return isOpen ? [row, el("div", { class: "kids", id: kidsId }, ...pkgs.map((p) => MC.row(S, p, { scope: "pc", lane: p.lane, kid: true })))] : [row];
};

// "10.5.0 → 10.5.2", the old version stepping back; a finding says what it is; the simple view says whether there is a fix
function versionCell(p) {
  if (MC.isFinding(p)) return [p.installed];
  if (MC.SIMPLE) return [p.available ? "Update ready" : "No fix yet"];
  return [el("s", { text: `${p.installed} → ` }), p.available || "no fix yet"];
}

// One row. `ctx.scope` = "pc" | "peer" | "project" | "packages" decides the action and the "where" line.
MC.row = function row(S, p, ctx) {
  if (ctx.aside || (!p.project && ctx.scope !== "packages")) return machineRow(S, p, ctx);
  const lead = p.members[0];
  const btns = el("div", { class: "btns" });
  const fx = p.project ? MC.latestFix(S, p.members.map((m) => m.key)) : null;
  if (fx) btns.append(el("span", { class: `chip fix-${fx.status}`, text: MC.FIX_LABEL[fx.status] || fx.status }));
  const job = !p.project ? MC.jobFor(S, lead.key) : null;
  if (job) btns.append(el("span", { class: `chip job-${job.status}`, title: job.hint || "", text: MC.jobLabel(job) }));
  if (p.project && ctx.scope === "project" && !(fx && ["queued", "running"].includes(fx.status))) {
    // lanes have a bulk "Copy prompt for all", so the per-row action steps back visually
    btns.append(el("button", { class: "link", text: "Copy prompt",
      title: "A prompt for your coding agent with what Bosun knows about this one",
      onclick: stop(() => MC.actions.copyPrompt(p.members.map((m) => m.key), lead.project_name)) }));
  }
  const kind = whatItIs(p);
  const where = ctx.scope === "packages" && p.project ? `in ${p.places.length > 3 ? `${p.places.slice(0, 3).join(", ")} +${p.places.length - 3}` : p.places.join(", ")}` : "";
  // the row opens in place: a short read and the actions. The side panel is one more click, for everything else.
  const id = `${ctx.scope}:${lead.key}`, isOpen = MC.peek.is(id);
  const toggle = () => MC.peek.toggle(id, lead.key);
  btns.append(el("span", { class: "chev", "aria-hidden": "true", text: "›" }));
  const row = el("div", { class: `row pk ${tier(p.risk)}${isOpen ? " open" : ""}`, tabindex: "0", role: "button", "aria-expanded": String(isOpen),
      "data-fk": `row:${id}`, onclick: toggle,
      onkeydown: (e) => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggle(); } } },
    el("div", {}, sev(p.risk)),
    el("div", {}, el("div", { class: "pkg", text: p.name }), el("div", { class: "eco", text: kind })),
    el("div", {}, el("div", { class: "ver", text: version(p) }), MC.backChip(lead), ...chips(p)),
    el("div", {}, el("div", { class: "head", text: headline(p) }), where && el("div", { class: "where", text: where })),
    btns);
  if (!isOpen) return row;
  const frag = document.createDocumentFragment();
  frag.append(row, MC.views.peek(S, p, MC.peek.item(lead.key)));
  return frag;
};

// severity as a colour step: the rail down the row's edge and the pill take it
const tier = (r) => (r >= 9 ? "t-crit" : r >= 7 ? "t-high" : r >= 4 ? "t-med" : r > 0 ? "t-low" : "t-none");
MC.tier = tier;

// Which row is open in place, and its details (fetched once, refreshed when the snapshot changes)
MC.peek = (() => {
  let open = null; const got = new Map();
  const fetchItem = async (key) => {
    try { got.set(key, await MC.api(`/api/item?key=${encodeURIComponent(key)}`)); }
    catch (e) { got.set(key, { error: e.message }); }
    MC.render();
  };
  return {
    is: (id) => open === id,
    item: (key) => got.get(key) || null,
    toggle(id, key) { open = open === id ? null : id; if (open) fetchItem(key); MC.render(); },
    close() { open = null; },
    stale() { got.clear(); if (open) fetchItem(open.slice(open.indexOf(":") + 1)); },
  };
})();

// the simple view leaves version numbers to the details; it says whether there's a fix at all
const version = (p) => (MC.isFinding(p) ? p.installed : MC.SIMPLE ? (p.available ? "" : "No fix yet")
  : `${p.installed} → ${p.available || "no fix yet"}`);
// a headline that only restates the versions ("5.2.2 -> 5.2.3": no release notes were read) says nothing new
const headline = (p) => (/^\S+ -> \S+$/.test(p.headline || "") ? MC.words("No release notes read.", "Update available") : p.headline || "");

// "npm dependency", "Developer tool · Homebrew", "Driver · Windows Update"
function whatItIs(p) {
  if (p.source === "container") return "container base image";
  if (p.source === "secret") return "secret in the repo";
  if (p.source === "mcp") return "MCP server";
  if (p.project) return `${p.ecosystem} dependency`;
  const via = MC.VIA[p.source];
  return [MC.KIND[MC.kindOf(p)], via && via !== MC.KIND[MC.kindOf(p)] ? via : ""].filter(Boolean).join(" · ");
}

// Cross-project view: fix once, everywhere.
MC.views.packages = function packages(S) {
  // packages only: a committed secret is per file and per project, and belongs on its project's page
  const RANK = ["t-crit", "t-high", "t-med", "t-low", "t-none"];
  const urgent = (p) => (p.kev || p.members.some((m) => m.malicious) ? 1 : 0);
  const pk = MC.groupByPackage(S.items.filter((i) => i.lane !== "dormant" && i.source !== "secret"))
    .sort((a, b) => urgent(b) - urgent(a) || RANK.indexOf(tier(a.risk)) - RANK.indexOf(tier(b.risk))
      || b.risk * b.places.length - a.risk * a.places.length);
  const shared = pk.filter((p) => p.places.length > 1);
  const box = el("div", { class: "panel" },
    el("div", { class: "listhead" }, el("h3", { text: "Packages across projects" }),
      el("span", { class: "why", text: `One row per package: exploited and malware first, then by severity, then by how many places it is in. Click a row to open it. ${plural(shared.length, "package")} appear in more than one place: fix the pattern once.` })));
  const TIERS = [["t-crit", MC.words("critical", "urgent")], ["t-high", "high"], ["t-med", "medium"], ["t-low", "low"], ["t-none", "no known vulnerabilities"]];
  const counts = TIERS.map(([t, label]) => [t, label, pk.filter((p) => tier(p.risk) === t).length]).filter((x) => x[2]);
  if (pk.length) box.append(el("div", { class: "sevbar", role: "img", "aria-label": counts.map(([, l, n]) => `${n} ${l}`).join(", ") },
      ...counts.map(([t, , n]) => el("span", { class: t, style: { flexGrow: String(n) } }))),
    el("div", { class: "sevkey" }, ...counts.map(([t, l, n]) => el("span", { class: t }, el("i", {}), el("b", { text: String(n) }), ` ${l}`))));
  const rows = el("div", { class: "rows" });
  for (const p of pk.slice(0, 150)) rows.append(MC.row(S, p, { scope: "packages" }));
  if (pk.length > 150) rows.append(el("div", { class: "more", text: `+ ${pk.length - 150} more` }));
  box.append(rows);
  return el("div", {}, el("h1", { class: "pagetitle", text: "Packages" }), box);
};
})();
