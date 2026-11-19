// One scope's page: this machine, grouped by how each item gets updated
// (or, one click away, by what kind of software it is); or a single project, with its own decision lanes.
"use strict";
(() => {
const { el, plural, LANES } = MC;

function header(S, sc) {
  const p = sc.project;
  const parts = [];
  const machine = sc.kind === "pc";
  if (!machine) {
    parts.push(p.dormant ? `dormant · ${p.age_days == null ? "no commits" : `${Math.round(p.age_days / 30)} months since last commit`}`
      : `active · last commit ${p.age_days} d ago`);
    if (p.checkouts.length > 1) parts.push(`${p.checkouts.length} checkouts (worktrees/clones merged)`);
    parts.push(p.docker ? "has Dockerfile/compose" : "no containers", p.tests ? "has tests" : "no test folder found");
  }
  const remote = p && p.has_remote ? MC.safeLink(p.key, p.key.replace(/^https?:\/\//, "")) : null;
  const ignored = sc.kind === "project" && sc.ignored;
  return el("section", { class: "scopehead" },
    el("div", { class: "crumbs" }, el("a", { href: "#/", text: "Today" }), el("span", { text: " / " }),
      el("span", { text: sc.kind === "pc" ? MC.MACHINE.label : "Projects" })),
    el("div", { class: "titlerow" }, el("h1", { class: "pagetitle", text: sc.name }),
      sc.kind === "project" ? el("button", { class: "quiet", text: "Recheck",
        title: "Scan this project again now (seconds), e.g. after your agent fixed something",
        onclick: (e) => MC.actions.recheckProject(p.key, sc.name, e.currentTarget) }) : null,
      sc.kind === "project" ? el("button", { class: "link", text: ignored ? "Unignore project" : "Ignore project",
        onclick: () => MC.actions.setPref(`project:${p.key}`, { ignored: !ignored }) }) : null),
    machine ? statusLine(S, sc) : el("div", { class: "cardmeta", text: parts.join(" · ") }),
    remote ? el("div", { class: "cardmeta" }, remote) : null,
    p ? MC.details(`${sc.id}:paths`, false, { class: "paths" }, el("summary", { text: p.checkouts.length > 1 ? "Checkouts" : "Location" }),
      el("ul", {}, ...p.checkouts.map((c) => el("li", { text: c })))) : null);
}

const HIGH = 7;
const byRisk = (a, b) => (b.kev - a.kev) || b.risk - a.risk || a.name.localeCompare(b.name);
const open = (sc) => MC.groupByPackage(sc.items.filter((i) => i.lane !== "dormant"));

// "15 updates waiting. Nothing risky. Bosun can install 8 of them right now."
function statusLine(S, sc) {
  const pkgs = open(sc);
  const away = MC.groupByPackage(sc.aside || []).length;
  if (!pkgs.length) return el("div", { class: "pagesub", text: away ? `Nothing waiting. ${away} ${MC.words("acknowledged", "set aside")}.` : "Nothing outdated." });
  const kev = pkgs.filter((p) => p.kev).length, high = pkgs.filter((p) => !p.kev && p.risk >= HIGH).length;
  const risk = kev ? `${kev} ${kev === 1 ? "is" : "are"} ${MC.words("exploited", "under attack")}${high ? `, ${high} more ${high === 1 ? "is" : "are"} high risk` : ""}.`
    : high ? `${high} ${high === 1 ? "is" : "are"} ${MC.words("high risk", "urgent")}.` : "Nothing risky.";
  const jumps = pkgs.filter((p) => MC.bigJump(p.members[0])).length;
  const can = pkgs.filter((p) => MC.bulkMode(S, p.members[0]) === "upgrade").length;
  return el("div", { class: "pagesub" }, el("b", { text: `${plural(pkgs.length, "update")} waiting.` }), ` ${risk}`,
    jumps ? ` ${jumps} ${jumps === 1 ? "is" : "are"} a big jump: read what changes first.` : "",
    can ? ` Bosun can install ${can === pkgs.length ? (can === 1 ? "it" : "all of them") : `${can} of them`} right now.` : "");
}

// ---------- the groups a machine page shows: by how it updates (the default here), by what it is, by publisher,
// or one flat list; rows filtered by Find and sorted as chosen ----------
const SORTS = {
  risk: byRisk,
  name: (a, b) => a.name.localeCompare(b.name),
  newest: (a, b) => (b.members[0].first_seen || 0) - (a.members[0].first_seen || 0) || byRisk(a, b),
  jump: (a, b) => MC.jumpOf(b) - MC.jumpOf(a) || byRisk(a, b),
};
const matches = (p, q) => !q || [p.name, p.members[0].suite || "", p.package_id || ""].some((t) => t.toLowerCase().includes(q));

function grouping(S, sc) {
  const mode = MC.SIMPLE ? "route" : S.list.group;
  const q = (S.find || "").trim().toLowerCase();
  const pkgs = open(sc).filter((p) => matches(p, q)).sort(SORTS[!MC.SIMPLE ? S.list.sort : "risk"] || byRisk);
  if (mode === "kind") {
    const m = MC.MACHINE.kind;
    return MC.GROUPS.map((g) => ({ id: g.id, name: g.name(m), why: g.why(MC.MACHINE.updates, m), kind: g,
      pkgs: pkgs.filter((p) => MC.groupOf(p) === g) })).filter((g) => g.pkgs.length);
  }
  if (mode === "publisher") {
    const by = new Map();
    for (const p of pkgs) { const k = MC.publisherOf(p.members[0]); by.set(k, [...(by.get(k) || []), p]); }
    return [...by.entries()].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))
      .map(([name, ps]) => ({ id: `pub-${name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`, name, why: "", pkgs: ps }));
  }
  if (mode === "none") return pkgs.length ? [{ id: "all", name: "Everything", why: "One list, in the order chosen above.", pkgs, flat: true }] : [];
  return MC.ROUTES.map((r) => ({ ...MC.routeInfo(r.id), route: r.id,
    pkgs: pkgs.filter((p) => MC.routeOf(S, p.members[0]) === r.id) })).filter((g) => g.pkgs.length);
}

// A group's one button, where one exists: everything in it that can go in one go.
function groupButton(S, sc, g, filled) {
  const members = g.pkgs.flatMap((p) => p.members);
  const stopFold = (fn) => (e) => { e.preventDefault(); fn(); };
  const pins = members.filter((m) => MC.pinnable(S, m));
  const pinAll = pins.length > 1 ? el("button", { class: "quiet", text: `Pin ${pins.length} MCP servers`,
    title: "Pin each to its newest version that has been out three days (Bosun backs up the configs first)",
    onclick: stopFold(() => MC.actions.pin(pins.map((m) => m.key))) }) : null;
  const openOs = members.some((m) => m.opens) && (g.route === "system" || g.id === "system")
    ? el("button", { class: "quiet", text: `Open ${MC.MACHINE.updates}`, onclick: stopFold(() => MC.actions.openUpdates()) }) : null;
  // drop-in ones only: migrations are one at a time; hardware tools wait for a deliberate click each.
  // One button only when one call covers them: all through Bosun's installer, or all through one terminal command.
  const bulk = MC.bulkAction(S, members.filter((m) => ["now", "batch"].includes(m.lane)), true);
  if (!bulk) return el("span", { class: "btns" }, pinAll, openOs);
  return el("span", { class: "btns" }, pinAll, openOs,
    el("button", { class: filled ? "primary" : "quiet", text: bulk.label, title: bulk.title, onclick: stopFold(bulk.act) }));
}

// One group: a header that folds it (a real button, with the group's action beside it, not inside it), then rows.
function groupSection(S, sc, g, filled) {
  const key = `${sc.id}:${g.route ? "route" : "group"}-${g.id}`;
  const urgent = g.pkgs.filter((p) => p.kev || p.risk >= HIGH).length;
  const worst = g.pkgs.reduce((m, p) => Math.max(m, p.kev ? 10 : p.risk), 0);
  const finding = sc.kind === "pc" && !!(S.find || "").trim();
  const isOpen = finding || g.flat || MC.fold.open(key, urgent > 0 || g.pkgs.length < 9);
  const bodyId = `rows-${g.id}`;
  const parts = sc.kind === "pc" && S.list.sort === "risk" ? MC.suites(S, g.pkgs.slice(0, 120)) : g.pkgs.slice(0, 120).map((p) => ({ one: p }));
  const rows = el("div", { class: "rows", id: bodyId }, ...parts.flatMap((x) => (x.one ? [MC.row(S, x.one, { scope: sc.kind, lane: x.one.lane })]
    : MC.suiteRow(S, x.suite, `${key}:suite-${x.suite.name}`, finding))));
  if (g.pkgs.length > 120) rows.append(el("div", { class: "more", text: `+ ${g.pkgs.length - 120} more` }));
  const names = g.pkgs.map((p) => p.name);
  return el("section", { class: `panel group${isOpen ? "" : " shut"}`, id: `group-${g.id}` },
    el("div", { class: "listhead" },
      el("h3", {}, el("button", { class: "foldbtn", "aria-expanded": String(isOpen), "aria-controls": bodyId, "data-fk": `fold:${key}`,
        onclick: () => { MC.fold.set(key, !isOpen); MC.render(); } },
        g.name, el("span", { class: "n", text: ` · ${g.pkgs.length}` }))),
      urgent ? el("span", { class: "worst" }, MC.sev(worst), urgent > 1 || worst < 10 ? ` ${urgent}` : "") : null,
      isOpen ? el("span", { class: "why", text: g.why })
        : el("span", { class: "names", text: `${names.slice(0, 6).join(", ")}${names.length > 6 ? ` and ${names.length - 6} more` : ""}` }),
      el("span", { class: "act" }, groupButton(S, sc, g, filled))),
    isOpen ? rows : null);
}

// Put away, one click to look at: acknowledged and snoozed items, each with "Bring back"; ignored ones behind a link.
function asideSection(S, sc) {
  const aside = sc.aside || [], ignored = sc.ignoredItems || [];
  if (!aside.length && !ignored.length) return null;
  const key = `${sc.id}:aside`;
  const isOpen = MC.fold.open(key, false) || (S.showIgnored && ignored.length > 0);
  const pkgs = MC.groupByPackage(aside), hidden = S.showIgnored ? MC.groupByPackage(ignored) : [];
  const label = (p) => { const st = MC.standing(p.members[0]);
    return st.state === "snoozed" ? `${p.name} ${st.until_version ? `skipping ${st.until_version}` : `snoozed until ${MC.day(st.until_ts)}`}` : p.name; };
  const names = pkgs.map(label);
  const body = el("div", { class: "rows", id: "rows-aside" },
    ...[...pkgs, ...hidden].map((p) => MC.row(S, p, { scope: sc.kind, aside: true })),
    el("div", { class: "footlinks" },
      el("span", { text: "Not on Today, not in the sidebar count, no notifications. A newer version alone doesn't bring an acknowledged item back; "
        + "more risk does: exploited, reported as malware, or high risk. Autopilot still installs acknowledged items and waits for snoozed ones." }),
      pkgs.length > 1 ? el("button", { class: "link accent", text: "Bring all back", onclick: () => MC.actions.bringBack(aside.map((i) => i.key)) }) : null,
      ignored.length ? el("button", { class: "link accent", text: S.showIgnored ? "Hide ignored" : `Show ${ignored.length} ignored`,
        title: "Ignored items stay hidden unless they become exploited or are reported as malware",
        onclick: () => MC.actions.showIgnored(!S.showIgnored) }) : null));
  const title = pkgs.length || !ignored.length ? MC.words("Acknowledged", "Set aside") : "Ignored";
  return el("section", { class: `panel group aside${isOpen ? "" : " shut"}`, id: "group-aside" },
    el("div", { class: "listhead" },
      el("h3", {}, el("button", { class: "foldbtn", "aria-expanded": String(isOpen), "aria-controls": "rows-aside", "data-fk": `fold:${key}`,
        onclick: () => { MC.fold.set(key, !isOpen); if (isOpen && S.showIgnored) S.showIgnored = false; MC.render(); } },
        title, el("span", { class: "n", text: ` · ${pkgs.length || ignored.length}` }))),
      isOpen ? el("span", { class: "why", text: MC.words("Seen, not acting now.", "You decided these can wait.") })
        : el("span", { class: "names", text: names.length ? `${names.slice(0, 6).join(", ")}${names.length > 6 ? ` and ${names.length - 6} more` : ""}`
          : `${plural(ignored.length, "item")} hidden for good` })),
    isOpen ? body : null);
}

// the groups as chips with counts, each a jump to its section; anything exploited or end-of-life comes first
function groupStrip(S, sc, groups) {
  const all = groups.flatMap((g) => g.pkgs);
  const kev = all.filter((p) => p.kev).length, eol = all.filter((p) => p.members.some((m) => m.unsupported)).length;
  const jump = (id) => (e) => { e.preventDefault(); const t = document.getElementById(id); if (t) t.scrollIntoView({ behavior: "smooth" }); };
  const hot = (n, label) => (n ? el("span", { class: "lanechip hot" }, el("b", { text: `▲ ${n}` }), ` ${label}`) : null);
  return el("nav", { class: "lanestrip", "aria-label": groups[0] && groups[0].route ? "How these get updated" : "What these are" },
    hot(kev, MC.words("exploited", "under attack")), hot(eol, MC.words("end of life", "no longer supported")),
    ...groups.map((g) => el("a", { href: `#group-${g.id}`, class: "lanechip", onclick: jump(`group-${g.id}`) },
      el("b", { text: String(g.pkgs.length) }), ` ${g.route ? g.chip : g.name}`)),
    asideChip(sc));
}

// a project's lanes, as chips that jump to each
function strip(sc) {
  const c = sc.sum.counts;
  return el("nav", { class: "lanestrip", "aria-label": "Lanes" }, ...MC.LANE_ORDER.map((l) =>
    el("a", { href: `#lane-${l}`, class: `lanechip${c[l] ? "" : " zero"}`,
      onclick: (e) => { e.preventDefault(); const t = document.getElementById(`lane-${l}`); if (t) { t.open = true; t.scrollIntoView({ behavior: "smooth" }); } } },
      el("b", { text: String(c[l]) }), ` ${LANES[l].name}`)));
}

function asideChip(sc) {
  const n = MC.groupByPackage(sc.aside || []).length;
  return n ? el("a", { href: "#group-aside", class: "lanechip muted",
    onclick: (e) => { e.preventDefault(); MC.fold.set(`${sc.id}:aside`, true); MC.render(); document.getElementById("group-aside")?.scrollIntoView({ behavior: "smooth" }); } },
    el("b", { text: String(n) }), ` ${MC.words("acknowledged", "set aside")}`) : null;
}

// the list's own controls, under the title: find, and (developer view) how to group and sort
function listBar(S, sc) {
  if (sc.kind !== "pc") return null;
  const choice = (label, value, options, set) => el("label", { class: "pick" }, `${label} `,
    el("select", { "data-fk": `pick:${label}`, onchange: (e) => set(e.target.value) },
      ...options.map(([v, text]) => el("option", { value: v, selected: v === value, text }))));
  const box = el("input", { type: "search", "data-fk": "find", placeholder: "Find an app or tool", "aria-label": "Find an app or tool",
    autocomplete: "off", spellcheck: "false", oninput: (e) => MC.actions.find(e.target.value),
    onkeydown: (e) => { if (e.key === "Escape" && e.target.value) { e.stopPropagation(); MC.actions.find(""); } } });
  box.value = S.find || "";
  return el("div", { class: "listbar" },
    el("label", { class: "find" }, MC.icon("find"), box, el("kbd", { "aria-hidden": "true", text: "/" })),
    MC.SIMPLE ? null : choice("Group", S.list.group, [["route", "How it updates"], ["kind", "What it is"], ["publisher", "Publisher"], ["none", "None"]],
      (v) => MC.actions.setList({ group: v })),
    MC.SIMPLE ? null : choice("Sort", S.list.sort, [["risk", "Risk"], ["name", "Name"], ["newest", "Newest first"], ["jump", "Biggest jump"]],
      (v) => MC.actions.setList({ sort: v })));
}

// a project lane's action (machine pages have groups instead: groupButton)
function laneAction(S, sc, l, pkgs) {
  const members = pkgs.flatMap((p) => p.members);
  if (l === "dormant") return null;  // the decision there is archive or ignore
  // the prompt asks for the right work per lane: fix drop-ins, plan migrations, assess what has no fix
  const copy = el("button", { class: l === "now" ? "primary" : "quiet", text: members.length > 1 ? `Copy prompt for all ${members.length}` : "Copy prompt",
    title: "A prompt for Claude Code, Cursor, Codex or any coding agent, with what Bosun knows about each finding",
    onclick: (e) => { e.preventDefault(); MC.actions.copyPrompt(members.map((m) => m.key), sc.name); } });
  if (!["now", "batch"].includes(l)) return copy;  // Plan items are migrations: one branch each
  const fixable = members.filter((m) => m.available || m.malicious);
  const busy = MC.latestFix(S, fixable.map((m) => m.key));
  if (fixable.length < 2 || (busy && ["queued", "running"].includes(busy.status))) return copy;
  return el("span", {}, copy, el("button", { class: "link", text: "or let Claude Code try",
    title: "One Claude Code run in a separate git worktree, one branch, one commit. Nothing is pushed until you create a PR.",
    onclick: (e) => { e.preventDefault(); MC.actions.fix(fixable.map((m) => m.key)); } }));
}

function laneSection(S, sc, l) {
  const pkgs = sc.sum.lanes[l];
  if (!pkgs.length) return null;
  const open = l === "now" || l === "plan" || l === "watch" || sc.project?.dormant;
  const rows = el("div", { class: "rows" }, ...pkgs.slice(0, 80).map((p) => MC.row(S, p, { scope: sc.kind, lane: l })));
  if (pkgs.length > 80) rows.append(el("div", { class: "more", text: `+ ${pkgs.length - 80} more` }));
  const name = l === "dormant" ? "All findings" : LANES[l].name;
  const why = l === "dormant" ? "This project is dormant: archive it, or ignore it to hide these findings." : LANES[l].why;
  return MC.details(`${sc.id}:lane-${l}`, open, { class: `panel lane lane-${l}`, id: `lane-${l}` },
    el("summary", { class: "listhead" }, el("h3", { text: `${name} · ${pkgs.length}` }), el("span", { class: "why", text: why }),
      el("span", { class: "act" }, laneAction(S, sc, l, pkgs))),
    rows);
}

function activeFix(S, sc) {
  if (sc.kind !== "project") return null;
  const fx = MC.latestFix(S, sc.items.map((i) => i.key));
  if (!fx || fx.status === "discarded") return null;
  return el("section", { class: "panel fixbox" }, ...MC.fixPanel(fx));
}

// What Bosun looked at, not only what it found: an app nobody can ask about never shows up as an update.
function coverageLine() {
  const c = MC.MACHINE.coverage;
  if (!c || !c.apps) return null;
  const missed = c.unchecked || [];
  if (!missed.length) return el("p", { class: "coverage", text: `Bosun can check all ${plural(c.apps, "app")} on this computer for updates.` });
  const names = `${missed.slice(0, 12).join(", ")}${missed.length > 12 ? ` and ${missed.length - 12} more` : ""}`;
  return el("p", { class: "coverage", title: missed.join("\n") },
    el("b", { text: `Bosun can check ${c.checked} of ${plural(c.apps, "app")} on this computer for updates.` }),
    ` It has no way to ask about ${missed.length === 1 ? "this one" : `these ${missed.length}`} (or to compare ${missed.length === 1 ? "its" : "their"} version with Homebrew's), so ${missed.length === 1 ? "it" : "they"} will never show up here: ${names}. `
    + `Update ${missed.length === 1 ? "it" : "them"} from inside the app.`);
}

// Earlier copies of apps Bosun kept before an update replaced them: the way back, while the copy lasts.
function keptLines() {
  const kept = MC.MACHINE.kept || [];
  if (!kept.length) return null;
  return el("div", { class: "kept" }, ...kept.map((k) => el("p", {},
    `${k.name} ${k.now} replaced ${k.kept}. Bosun keeps ${k.kept} until ${MC.day(k.until)}. `,
    el("button", { class: "small", text: `Go back to ${k.kept}`,
      title: `Puts ${k.name} ${k.kept} back in place of ${k.now}. Quit ${k.name} first. ${k.now} is then listed again as an update that only you start.`,
      onclick: () => MC.actions.goBack(k) }))));
}

// Apps Bosun moved out of Applications because they did not pass the developer check. Only listed: deleting
// them is the person's call, so there is no button.
function rejectedLines() {
  const gone = MC.MACHINE.rejected || [];
  if (!gone.length) return null;
  const size = (n) => n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(n / 1e6))} MB`;
  return el("div", { class: "kept" }, ...gone.map((r) => el("p", {},
    `${r.name}: on ${MC.day(r.when)} Bosun moved it out of Applications because it did not pass the developer check. ` +
    `It is at ${r.path} (${size(r.size)}). Deleting it is your call; Bosun keeps the newest few and does not open it.`)));
}

MC.views.scope = function scope(S, scopes, id) {
  const sc = scopes.byId.get(id);
  if (!sc) return el("p", { class: "hint", text: "That project isn't in the latest snapshot (it may have been ignored or cleaned up)." });
  const nothing = el("div", { class: "panel" }, el("p", { class: "hint", text: "Nothing to do here." }));
  if (sc.kind === "pc") {
    const groups = grouping(S, sc);
    // one filled button on the page: the first group that has one
    let filled = false;
    const sections = groups.map((g) => {
      const sec = groupSection(S, sc, g, !filled);
      if (sec.querySelector("button.primary")) filled = true;
      return sec;
    });
    return el("div", {}, header(S, sc), groups.length || (sc.aside || []).length ? groupStrip(S, sc, groups) : null,
      open(sc).length ? listBar(S, sc) : null,
      ...(sections.length ? sections : [(S.find || "").trim()
        ? el("div", { class: "panel" }, el("p", { class: "hint", text: `Nothing here matches "${S.find.trim()}".` })) : nothing]),
      asideSection(S, sc),
      !(S.find || "").trim() ? keptLines() : null,
      !(S.find || "").trim() ? rejectedLines() : null,
      !(S.find || "").trim() ? coverageLine() : null);
  }
  const lanes = sc.project?.dormant ? ["now", "dormant"] : MC.LANE_ORDER;
  const sections = lanes.map((l) => laneSection(S, sc, l)).filter(Boolean);
  return el("div", {}, header(S, sc), sc.project?.dormant ? null : strip(sc), activeFix(S, sc),
    ...(sections.length ? sections : [nothing]), asideSection(S, sc));
};
})();
