// Detail drawer + the Claude Code fix panel (also shown at the top of a project page).
"use strict";
(() => {
const { el, sev } = MC;
const REACH_ORDER = ["remote", "remote_auth", "adjacent", "needs_action", "local", "physical", "unknown"];
const EXPOSURE = { internet: "Reachable from the internet", needs_your_action: "Needs you to do something",
                   local_only: "Needs local access", mixed: "Mixed", unclear: "Unclear" };
const URGENCY = { today: "Fix today", this_week: "This week", next_maintenance: "Next maintenance window", optional: "Optional" };
const pct = (x) => x == null ? "n/a" : x < 0.001 ? "<0.1%" : `${(x * 100).toFixed(x < 0.1 ? 1 : 0)}%`;

MC.fixPanel = function fixPanel(fx) {
  const r = fx.result || {}, rep = r.report || {};
  const n = (r.keys || []).length;
  const cost = r.cost_usd != null ? ` · $${Number(r.cost_usd).toFixed(3)}` : "";
  const out = [el("h4", { text: `Branch + test${n > 1 ? ` · ${n} dependencies` : ""}` }),
    el("p", {}, el("span", { class: `chip fix-${fx.status}`, text: MC.FIX_LABEL[fx.status] || fx.status }),
      el("span", { class: "hint", text: ` ${fx.branch || ""}${cost}` }))];
  if (rep.summary) out.push(el("p", { text: rep.summary }));
  if (rep.tests) {
    const verdict = rep.tests_passed === true ? " (passed)" : rep.tests_passed === false ? " (FAILED)" : "";
    out.push(el("p", { class: "hint", text: `Tests: ${rep.tests}${verdict}` }));
  }
  if (rep.follow_ups && rep.follow_ups.length) out.push(el("ul", {}, ...rep.follow_ups.map((f) => el("li", { text: f }))));
  if (r.diffstat) out.push(el("pre", { text: r.diffstat }));
  if (r.sandboxed === false) out.push(el("p", { class: "hint", text: "Ran without an OS sandbox (not available on this computer): its commands had your access." }));
  if (r.denied && r.denied.length) out.push(el("p", { class: "hint", text: `Sandbox denied: ${r.denied.join(" | ")}` }));
  if (r.error) out.push(el("pre", { text: r.error }));
  if (fx.worktree && fx.status !== "discarded") out.push(el("p", { class: "hint", text: `Worktree: ${fx.worktree}` }));
  const acts = el("div", { class: "actions" });
  if (fx.pr_url) acts.append(MC.safeLink(fx.pr_url, "Open pull request"));
  else if (["fixed", "partial"].includes(fx.status)) {
    acts.append(el("button", { class: "primary", text: "Push & create PR", onclick: () => MC.actions.createPr(fx.id) }));
  }
  if (!["queued", "running", "discarded"].includes(fx.status) && !fx.pr_url) {
    acts.append(el("button", { class: "quiet", text: "Discard branch", onclick: () => MC.actions.discardFix(fx.id) }));
  }
  out.push(acts);
  return out;
};

// ---- security, in plain terms ----
function reachBar(ex) {
  const counts = ex.reach_counts || {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  if (!total) return null;
  const order = REACH_ORDER.filter((r) => counts[r]);
  return el("div", {},
    el("div", { class: "reachbar", role: "img", "aria-label": order.map((r) => `${counts[r]} ${ex.labels[r]}`).join(", ") },
      ...order.map((r) => el("span", { class: `rb rb-${r}`, style: { flexGrow: String(counts[r]) }, title: `${counts[r]} ${ex.labels[r]}` }))),
    el("div", { class: "reachlegend" }, ...order.map((r) => el("span", { class: "key" }, el("b", { class: `rb rb-${r}` }),
      `${counts[r]} ${ex.labels[r]}`))));
}

function assessmentBlock(S, d) {
  const a = d.assessment;
  if (!a) {
    if (!(d.vulns_fixed || []).length && !(d.vulns_remaining || []).length && d.source !== "container") return null;
    if (!S.claude) return null;  // the assessment runs on Claude Code
    return el("div", { class: "assess pending" },
      el("p", { class: "hint", text: "Ask Claude Code to weigh these facts against how this machine actually uses it: can it be attacked here, and could updating break something?" }),
      el("button", { class: "quiet", id: "assessBtn", text: "Assess impact on this machine (~15 s)", onclick: async (e) => {
        e.target.disabled = true; e.target.textContent = "Assessing…";
        await MC.actions.explain(d.key);
      } }));
  }
  return el("div", { class: `assess exp-${a.exposure}` },
    el("div", { class: "assesshead" }, el("span", { class: "chip", text: EXPOSURE[a.exposure] || a.exposure }),
      el("span", { class: "chip", text: URGENCY[a.urgency] || a.urgency })),
    el("p", { class: "answer", text: a.answer }),
    el("p", { text: a.attack_story }),
    a.preconditions?.length ? el("div", {}, el("h5", { text: "For an attack to work" }), el("ul", {}, ...a.preconditions.map((x) => el("li", { text: x })))) : null,
    el("p", { class: "hint", text: `Worst case: ${a.worst_case}` }),
    el("p", { class: "hint", text: `Assessed by ${a.model || "Claude Code"} from the facts below.` }));
}

function securitySection(S, d) {
  const ex = d.exposure || {};
  const hasVulns = (d.vulns_fixed || []).length + (d.vulns_remaining || []).length > 0;
  const out = [el("h4", { text: "Security, in plain terms" })];
  if (d.malicious) out.push(el("div", { class: "callout crit" }, el("strong", { text: "Malicious package. " }),
    "This isn't a bug in a legitimate package: it was published to attack whoever installs it, often by stealing tokens and keys. ",
    "Remove it, delete the installed copy (node_modules or the virtualenv) and reinstall, then rotate any credentials this machine ",
    "holds that it could have read: .env files, cloud and API keys, SSH keys, npm or GitHub tokens."));
  if (d.kev) out.push(el("div", { class: "callout crit" }, el("strong", { text: "Actively exploited. " }),
    "CISA lists at least one of these as exploited in real attacks. Treat it as urgent no matter how it's reached."));
  if (d.unsupported || d.ends_soon) out.push(el("p", { text: d.unsupported
    ? `${d.name} no longer gets security fixes${d.support_ends ? ` (end of life ${d.support_ends})` : ""}. New vulnerabilities in it will never be patched, so this risk grows over time.`
    : `${d.name} stops getting security fixes on ${d.support_ends}.` }));
  if (hasVulns) {
    out.push(reachBar(ex));
    if (ex.reach) {
      const total = Object.values(ex.reach_counts || {}).reduce((x, y) => x + y, 0);
      const n = (ex.reach_counts || {})[ex.reach] || 0;
      out.push(el("p", { class: "hint", text: `Most exposed: ${n === total ? "all" : `${n} of ${total}`} ${ex.labels[ex.reach]}. ${ex.explain[ex.reach]}` }));
    }
    if (!MC.SIMPLE) out.push(el("p", { class: "hint", text: ex.max_epss != null
      ? `Likelihood of exploitation in the next 30 days (EPSS): highest is ${pct(ex.max_epss)}, which is higher than ${pct(ex.max_epss_pct)} of all known CVEs.`
      : "No EPSS score (it covers CVE ids only; some advisories have none)." }));
  }
  if (!hasVulns && out.length === 1) return [];  // nothing to say: no known vulnerabilities or support issues
  out.push(assessmentBlock(S, d));
  return out;
}

function usageSection(d) {
  const a = d.assessment;
  const lines = [];
  if (d.used_by?.length) lines.push(...d.used_by.map((u) => el("li", { text: u })));
  if (d.dep_path) lines.push(el("li", {}, el("span", { text: d.dep_path.includes(">") ? "Pulled in via " : "Declared directly: " }), el("code", { text: d.dep_path })));
  if (d.imports?.length) {
    const top = d.dep_path ? d.dep_path.split(" > ")[0] : d.name;
    lines.push(el("li", {}, el("span", { text: `${top} is imported in ${d.imports.length === 1 ? "1 file" : `${d.imports.length} files`}: ` }),
      el("code", { text: d.imports.join(", ") })));
  } else if (d.source === "repo" && d.dep_path) {
    lines.push(el("li", { class: "hint", text: "No source file imports it directly (likely build tooling or an indirect runtime dependency)." }));
  }
  if (!lines.length && !a?.break_risk?.length) {
    if (d.project || MC.SIMPLE) return [];
    return [el("h4", { text: "Where it's used" }), el("p", { class: "hint", text: "No projects on this machine reference it. It's a standalone app or tool, so an update only affects the app itself." })];
  }
  const out = [el("h4", { text: "Where it's used" }), el("ul", { class: "usage" }, ...lines)];
  if (a?.break_risk?.length) {
    out.push(el("h5", { text: "Could updating break it?" }), el("ul", { class: "usage" }, ...a.break_risk.map((b) =>
      el("li", {}, el("span", { class: `chip br-${b.risk}`, text: `${b.risk} risk` }), el("strong", { text: ` ${b.where}: ` }), el("span", { text: b.why })))));
  }
  if (a?.before_upgrading?.length) {
    out.push(el("h5", { text: "Before and after updating" }), el("ul", {}, ...a.before_upgrading.map((x) => el("li", { text: x }))));
  }
  return out;
}

const allVulns = new Set();  // items whose whole table you asked for
function vulnTable(d) {
  const rows = [...(d.vulns_fixed || []).map((v) => [v, true]), ...(d.vulns_remaining || []).map((v) => [v, false])]
    .sort((a, b) => (b[0].kev - a[0].kev) || REACH_ORDER.indexOf(a[0].reach) - REACH_ORDER.indexOf(b[0].reach) || (b[0].cvss || 0) - (a[0].cvss || 0));
  if (!rows.length) return [MC.notedCves(d)].filter(Boolean);
  return [el("h4", { text: `Vulnerabilities (${rows.length})` }),
    el("table", { class: "vulns" },
      el("thead", {}, el("tr", {}, ...["", "CVSS", "How it's reached", "What an attacker gets", "EPSS", ""].map((h) => el("th", { text: h })))),
      el("tbody", {}, ...rows.slice(0, allVulns.has(d.key) ? 200 : 10).map(([v, fixed]) => el("tr", {},
        el("td", {}, MC.safeLink(v.url, v.id), v.kev ? el("span", { class: "chip kev", text: "Exploited" }) : null),
        el("td", { class: "num", text: v.cvss != null ? String(v.cvss) : "?" }),
        el("td", {}, el("span", { class: `rb-dot rb-${v.reach}` }), el("span", { text: v.reach_label })),
        el("td", { text: v.impact }),
        el("td", { class: "num", text: v.epss != null ? pct(v.epss) : "" }),
        el("td", { class: "hint", text: fixed ? "fixed by the update" : "not fixed yet" })))),
    ),
    rows.length > 10 && !allVulns.has(d.key) ? el("button", { class: "link accent", text: `Show all ${rows.length}`,
      onclick: () => { allVulns.add(d.key); MC.actions.open(d.key); } }) : null,
    el("p", { class: "hint", text: "The most exposed come first. EPSS is the estimated probability of exploitation in the next 30 days; most CVEs score under 1%." }),
    MC.notedCves(d)].filter(Boolean);
}

function jobPanel(S, d) {
  const j = (S.jobs || []).find((x) => x.item_key === d.key);
  if (!j) return [];
  const when = MC.ago(j.finished || j.started);
  return [el("h4", { text: "Last update attempt" }),
    el("p", {}, el("span", { class: `chip job-${j.status}`, text: MC.jobLabel(j) }), el("span", { class: "hint", text: ` ${when}` })),
    j.hint ? el("div", { class: `callout${j.status === "failed" ? " crit" : ""}`, text: j.hint }) : null,
    j.trace_id && (j.status === "failed" || j.status === "succeeded" || j.status === "abandoned")
      ? el("p", {}, el("a", { href: MC.runHref(j.trace_id), text: j.status === "failed" ? "What happened: every step and command" : "Every step and command" })) : null,
    j.status === "failed" ? el("div", { class: "actions" },
      // what one click can try next: the fix this failure calls for, or the same update again (the server decides which)
      ...(MC.MACHINE.terminal ? (d.recipes || []) : []).map((r) => MC.recipeButton(r, d.name, r.follow_up ? "primary" : "quiet")),
      j.trace_id && S.claude ? el("button", { class: "quiet", text: "Why did this fail?", title: "Ask Bosun: it reads this run and explains",
        onclick: () => MC.ask("Why did this fail?", { run: j.trace_id, item: d.key }) }) : null) : null,
    j.output ? MC.details(`${d.key}:output`, false, {}, el("summary", { text: "Installer output" }), el("pre", { text: j.output })) : null];
}

// Every way to update the item other than the one button above, as a numbered list: the app's own updater, a
// command Bosun can run here (recipes.py builds it; the text is for reading), a command to copy, the maker's page.
function otherWays(S, d, action) {
  if (d.project) return [];
  const job = MC.jobFor(S, d.key), busy = !!job && ["waiting", "queued", "running"].includes(job.status);
  const main = action ? action.label : "";
  const ways = [];
  if (MC.openable(d) && !/^Open /.test(main)) ways.push(el("li", {}, `Open ${d.name} and accept the update it offers. `,
    el("button", { class: "link accent", text: `Open ${d.name}`, onclick: () => MC.actions.openApp(d) })));
  const viaRecipe = action && d.run && MC.routeOf(S, d) === "once" && !(MC.withPassword(d) && d.run !== "msupdate-install");
  for (const r of MC.MACHINE.terminal ? (d.recipes || []).filter((x) => !x.follow_up) : []) {
    const same = viaRecipe && r.recipe === d.run;  // the button above already does this: show only what it runs
    ways.push(el("li", {}, same ? "What the button above runs, here in Bosun's terminal: " : `${r.what} `, el("code", { text: r.command }),
      same || busy ? null : el("span", {}, " ", MC.recipeButton({ ...r, label: MC.heldBack(d) ? "Install it anyway…" : "Update…" }, d.name, "link accent"))));
  }
  if (!action && MC.heldBack(d) && MC.withPassword(d) && !job) ways.push(el("li", {},
    "Install the version from Homebrew's catalog now instead of waiting for the app's own updater. ",
    el("button", { class: "link accent", text: "Install it anyway…", title: MC.passwordHint(), onclick: () => MC.actions.upgrade([d.key]) })));
  const cmd = MC.manualCommand(d.manual);
  if (cmd && main !== "Copy command" && !(d.recipes || []).some((r) => r.command.endsWith(cmd.replace(/^sudo /, "")))) ways.push(el("li", {}, "Run it yourself in a terminal: ", el("code", { text: cmd }), " ",
    el("button", { class: "link accent", text: "Copy", onclick: (e) => {
      navigator.clipboard.writeText(cmd).then(() => { e.target.textContent = "Copied"; }, () => { e.target.textContent = "Copy failed"; }); } })));
  const page = d.release_notes_url && /^https?:/.test(d.release_notes_url) && (d.vendor || d.source === "mac-app" || d.source === "brew-cask") && !/^Get it/.test(main)
    ? MC.safeLink(d.release_notes_url, d.vendor ? `${d.vendor}'s page` : "the maker's page") : null;
  if (page) ways.push(el("li", {}, "Download it from ", page, "."));
  const note = d.opens ? `${MC.MACHINE.updates} installs this; Bosun shows what's waiting and opens it for you.`
    : d.vendor ? `${d.vendor} publishes these. Bosun can't install them or tell which version is newest, only how old this one is.`
    : MC.heldBack(d) ? "Held back: an update the app's own updater doesn't offer yet isn't work for you today."
    : ways.some((w) => w.querySelector("code")) && MC.MACHINE.terminal ? "A command Bosun runs is built from the item, in Bosun's own terminal on this page: you see the output and type your password there. Nothing else can be run there, and what you type is not kept."
    : MC.MACHINE.kind === "pc" && d.manual ? "Bosun doesn't run commands in a terminal on Windows: an update that needs administrator rights asks through the UAC prompt when you click Update." : "";
  if (!ways.length && !d.manual) return [];
  return [MC.details(`${d.key}:ways`, !action, { class: "fold" }, el("summary", { text: action ? "Other ways to update" : "How to update" }),
    d.manual && (!action || MC.heldBack(d) || !ways.length) ? el("p", { text: d.manual }) : null,
    ways.length ? el("ol", {}, ...ways) : null,
    note ? el("p", { class: "hint", text: note }) : null)];
}

// "Not now ▾": acknowledge, snooze, skip this version, ignore. For an item already put away: bring it back.
function notNowButton(S, d) {
  const st = MC.standing(d);
  if (st.state) return el("button", { class: "quiet", text: st.state === "ignored" ? "Stop ignoring" : "Bring back", onclick: () => MC.actions.setState(d.key, "open") });
  return el("button", { class: "quiet", "aria-haspopup": "menu", "aria-expanded": "false", text: "Not now ▾",
    onclick: (e) => MC.menu(e.currentTarget, MC.notNow(S, d)) });
}

// "Part of Microsoft Office. All 5 apps have the same update waiting." with the suite's one button
function suiteLine(S, d) {
  if (d.project || !d.suite) return null;
  const mine = (S.items || []).filter((i) => !i.project && i.suite === d.suite && i.installed === d.installed && i.available === d.available
    && MC.routeOf(S, i) === MC.routeOf(S, d));
  if (mine.length < 2) return null;
  const bulk = MC.bulkAction(S, mine, true);
  return el("div", { class: "suiteline" }, el("span", {}, el("b", { text: `Part of ${d.suite}. ` }), `All ${mine.length} have the same update waiting.`),
    bulk ? el("button", { class: "quiet", text: bulk.label, title: bulk.title, onclick: bulk.act }) : null);
}

function standingLine(d) {
  const st = MC.standing(d);
  if (!st.state && !st.back) return null;
  const [verb, rest] = MC.saidText(st);
  if (st.back) return el("div", { class: "callout crit" }, el("strong", { text: `Back: ${st.back}. ` }),
    `You ${MC.saidPast(st)}${st.at ? ` ${MC.day(st.at)}` : ""}.${st.said === "ignored" ? " Ignoring can't hide something that is being attacked." : ""}`);
  return el("div", { class: "callout" }, el("strong", { text: `${verb} ${rest}. ` }),
    st.state === "ignored" ? "Hidden for good, and Autopilot leaves it alone. It comes back only if it becomes exploited or is reported as malware."
    : `Not on Today, not counted, no notifications. It comes back if it gets riskier (exploited, malware, high risk)${
      st.state === "snoozed" ? (st.until_version ? ", or when another version is out. Autopilot waits too." : ", or when the snooze ends. Autopilot waits too.")
      : "; a newer version alone doesn't bring it back. Autopilot still installs it."}`);
}

// a long path, down to the part inside the project
function shortPath(d, path) {
  const root = (d.checkouts || []).find((c) => path.startsWith(`${c}/`));
  if (root) return path.slice(root.length + 1);
  const parts = path.split(/[\\/]/);
  return parts.length > 3 ? `…/${parts.slice(-2).join("/")}` : path;
}

// a section that folds: its heading is the handle
function fold(key, open, nodes) {
  if (!nodes.length) return [];
  const [head, ...rest] = nodes;
  return [MC.details(key, open, { class: "fold" }, el("summary", { text: head.textContent }), ...rest.filter(Boolean))];
}

MC.views.drawer = function drawer(S, d) {
  const s = d.summary || {};
  const fx = d.project ? MC.latestFix(S, [d.key]) : null;
  const action = MC.itemAction(S, d);  // the same one button the row and Today show
  const route = MC.routeOf(S, d);
  const ctx = d.source === "container"
    ? [d.base_os ? `OS layer: ${d.base_os}` : "", d.floating ? "Floating tag" : "", d.lockfile ? `File: ${shortPath(d, d.lockfile)}` : ""]
    : d.source === "repo"
      ? [d.direct === true ? `Declared ${d.dev ? "as a dev dependency" : "as a runtime dependency"}${d.spec ? ` (${d.spec})` : ""}`
          : d.direct === false ? `Transitive${d.dev === true ? ", dev-only" : d.dev === false ? ", ships at runtime" : ""}` : "Manifest not found",
         d.fix_kind ? `Fix: ${d.fix_kind}` : "", d.lockfile ? `Lockfile: ${shortPath(d, d.lockfile)}` : ""]
      : d.binaries && d.binaries.length > 1 ? [`Packages: ${d.binaries.join(", ")}`] : [];
  const nvulns = (d.vulns_fixed || []).length + (d.vulns_remaining || []).length;
  const what = d.project ? (d.source === "container" ? "base image" : d.source === "secret" ? "secret in the repo" : `${d.ecosystem} dependency`)
    : d.source === "mcp" ? "MCP server" : MC.KIND[MC.kindOf(d)];
  const versions = MC.isFinding(d) ? `${d.installed}${d.source === "mcp" ? ` · ${d.location}` : ""}`
    : MC.SIMPLE ? (d.available ? "Update ready" : "No fix yet") : `${d.installed} → ${d.available || "no fixed version yet"}`;
  // a summary that is only the keyword fallback says nothing about this update: say that instead
  const lead = s.generated_by === "heuristic" && /^\S+ -> \S+$/.test(s.headline || "") ? "No release notes read for this update." : s.headline || "";
  const job = MC.jobFor(S, d.key);
  const busy = job && ["waiting", "queued", "running"].includes(job.status);
  const how = d.project ? "" : action ? action.how
    : busy ? (job.status === "waiting" ? "Queued. It starts by itself once the app is closed." : "Updating now.")
    : route === "watch" ? "No fixed version exists yet. Bosun checks again every day." : MC.itemWhy(d);
  const brings = [el("h4", { text: "What the update brings" }),
    el("p", { text: MC.SIMPLE ? (s.generated_by === "heuristic" ? s.headline || "" : s.rationale || "")
      : s.generated_by === "heuristic" ? `No release notes were read: this is a guess from the version numbers. Migration effort: ${s.migration_effort || "unknown"}.`
      : `${s.rationale || ""} Migration effort: ${s.migration_effort || "unknown"}.` }),
    ...list("Notable changes", s.notable_features), ...list("Breaking changes / cautions", s.breaking_changes)];
  return [
    el("div", { class: "top" },
      el("div", { class: "crumbs" }, el("a", { href: MC.route.href("scope", d.project || "pc"), text: d.project ? d.project_name : MC.MACHINE.label }),
        route ? ` / ${MC.routeInfo(route).name}` : "", !d.project && d.suite ? ` / ${d.suite}` : ""),
      el("button", { class: "link closebtn", "data-fk": "drawer:close", text: "Close ✕", onclick: () => MC.actions.close() })),
    el("div", { class: "dhead" }, MC.tile(d, "lg"),
      el("div", {}, el("h2", { id: "drawerTitle", text: d.name }),
        el("div", { class: "ver" }, `${versions} · ${what}${nvulns || d.risk > 0 ? "" : d.cve_check === "unchecked" ? " · not checked for vulnerabilities" : " · no known vulnerabilities"} `, d.risk > 0 ? sev(d.risk) : null))),
    lead ? el("p", { class: "dlead", text: lead }) : null,
    standingLine(d),
    el("div", { class: "dact" },
      action ? el("button", { class: "primary", "data-fk": "drawer:act", text: action.label, onclick: action.act }) : null,
      !action && busy ? el("span", { class: `status job-${job.status}`, text: MC.jobLabel(job) }) : null,
      d.project ? el("button", { class: "primary", "data-fk": "drawer:act", text: "Copy prompt for your agent",
        title: "Bosun's findings as a prompt for Claude Code, Cursor, Codex or any coding agent",
        onclick: () => MC.actions.copyPrompt([d.key], d.project_name) }) : null,
      d.project && (d.available || d.malicious) && d.lane !== "dormant" && !(fx && ["queued", "running"].includes(fx.status))
        ? el("button", { class: "quiet", text: "Let Claude Code try it",
          title: "Bosun runs Claude Code in a separate git worktree and runs the tests. Nothing is pushed until you create a PR.",
          onclick: () => MC.actions.fix([d.key]) }) : null,
      S.claude && d.available && !MC.isFinding(d) ? el("button", { class: "quiet", text: "Will this break anything?", title: "Ask Bosun about this update",
        onclick: () => MC.ask("Will this break anything?", { item: d.key }) }) : null,
      notNowButton(S, d)),
    how ? el("p", { class: "dhow", text: how }) : null,
    suiteLine(S, d),
    ctx.filter(Boolean).length ? el("p", { class: "hint dctx", text: ctx.filter(Boolean).join(" · ") }) : null,
    ...otherWays(S, d, action),
    ...(fx ? MC.fixPanel(fx) : []),
    ...jobPanel(S, d),
    ...securitySection(S, d),
    ...fold(`${d.key}:brings`, !!d.project && d.risk < 7, brings),
    ...fold(`${d.key}:used`, false, usageSection(d)),
    ...technical(d, s),
  ];
};

// A package row opened in place: what the update brings, what it fixes, where it is, and the actions. `p` is the
// row's package (every place it appears); `d` the details of its first place, or null while they load.
MC.views.peek = function peek(S, p, d) {
  const lead = p.members[0], keys = p.members.map((m) => m.key);
  const box = (...kids) => el("div", { class: `peek ${MC.tier(p.risk)}`, onclick: (e) => e.stopPropagation() }, ...kids);
  if (!d) return box(el("p", { class: "hint", text: "Loading…" }));
  if (d.error) return box(el("p", { class: "hint", text: `Could not load details: ${d.error}` }));
  const s = d.summary || {};
  const guess = s.generated_by === "heuristic";
  const fixed = d.vulns_fixed || [], left = d.vulns_remaining || [];
  const vulns = [...fixed.map((v) => [v, true]), ...left.map((v) => [v, false])]
    .sort((x, y) => (y[0].kev - x[0].kev) || (y[0].cvss || 0) - (x[0].cvss || 0));
  const vtier = (v) => (v.kev || v.cvss >= 9 ? "t-crit" : v.cvss >= 7 ? "t-high" : v.cvss >= 4 ? "t-med" : "t-low");
  const brings = el("div", { class: "pcol" }, el("h4", { text: "What the update brings" }),
    el("p", { text: guess ? "No release notes were read: this is a guess from the version numbers." : s.rationale || s.headline || "Nothing recorded." }),
    (s.breaking_changes || []).length ? el("ul", { class: "pbreak" }, ...s.breaking_changes.slice(0, 3).map((x) => el("li", { text: x }))) : null,
    s.migration_effort ? el("p", { class: "hint", text: `Migration effort: ${s.migration_effort}.` }) : null);
  const security = el("div", { class: "pcol" }, el("h4", { text: "Security" }),
    d.malicious ? el("div", { class: "callout crit" }, el("strong", { text: "Malicious package. " }), "Remove it and rotate any credentials it could have read.") : null,
    d.kev ? el("div", { class: "callout crit" }, el("strong", { text: "Actively exploited. " }), "Treat it as urgent.") : null,
    vulns.length ? el("ul", { class: "pvulns" }, ...vulns.slice(0, 4).map(([v, ok]) => el("li", {},
        el("span", { class: `vscore ${vtier(v)}`, text: v.cvss != null ? Number(v.cvss).toFixed(1) : "?" }),
        MC.safeLink(v.url, v.id), el("span", { class: "hint", text: ` ${v.reach_label || ""}${ok ? "" : " · not fixed yet"}` }))))
      : d.malicious || d.kev ? null : el("p", { class: "hint", text: d.unsupported ? "No longer gets security fixes." : MC.cveCheckText(d) }),
    vulns.length > 4 ? el("p", { class: "hint", text: `+ ${vulns.length - 4} more in the details` }) : null,
    vulns.length ? el("p", { class: "hint", text: `${fixed.length} fixed by the update${left.length ? `, ${left.length} not fixed yet` : ""}.` }) : null,
    MC.notedCves(d));
  const places = el("div", { class: "pcol" }, el("h4", { text: p.members.length > 1 ? `Where · ${p.members.length} places` : "Where" }),
    el("ul", { class: "pplaces" }, ...p.members.slice(0, 8).map((m) => el("li", {},
      m.project ? el("a", { href: MC.route.href("scope", m.project), text: m.project_name }) : el("span", { text: MC.MACHINE.label }),
      el("span", { class: "hint", text: ` ${m.installed}${m.available ? ` → ${m.available}` : ""}${m.direct === false ? " · transitive" : ""}${m.dev === true ? " · dev only" : ""} ` }),
      p.members.length > 1 ? el("button", { class: "link accent", text: "details", onclick: () => MC.actions.open(m.key) }) : null))),
    p.members.length > 8 ? el("p", { class: "hint", text: `+ ${p.members.length - 8} more` }) : null,
    d.dep_path && d.dep_path.includes(">") ? el("p", { class: "hint" }, "Pulled in via ", el("code", { text: d.dep_path })) : null);
  const action = d.project ? null : MC.itemAction(S, d);
  const oneProject = d.project && p.members.every((m) => m.project === d.project);
  const fx = d.project ? MC.latestFix(S, keys) : null;
  const acts = el("div", { class: "dact" },
    action ? el("button", { class: "primary", text: action.label, title: action.title, onclick: action.act }) : null,
    d.project ? el("button", { class: "primary", text: keys.length > 1 ? `Copy prompt for all ${keys.length}` : "Copy prompt for your agent",
      title: "Bosun's findings as a prompt for Claude Code, Cursor, Codex or any coding agent",
      onclick: () => MC.actions.copyPrompt(keys, oneProject ? d.project_name : p.name) }) : null,
    oneProject && (d.available || d.malicious) && d.lane !== "dormant" && !(fx && ["queued", "running"].includes(fx.status))
      ? el("button", { class: "quiet", text: "Let Claude Code try it",
        title: "Bosun runs Claude Code in a separate git worktree and runs the tests. Nothing is pushed until you create a PR.",
        onclick: () => MC.actions.fix(keys) }) : null,
    S.claude && d.available && !MC.isFinding(d) ? el("button", { class: "quiet", text: "Will this break anything?", title: "Ask Bosun about this update",
      onclick: () => MC.ask("Will this break anything?", { item: d.key }) }) : null,
    notNowButton(S, d),
    el("button", { class: "link accent pall", text: "All details ›", title: "Everything Bosun knows, in the side panel", onclick: () => MC.actions.open(lead.key) }));
  return box(el("div", { class: "pgrid" }, brings, security, places), acts);
};

// CVEs, scores and provenance: the developer view shows them; the simple view folds them away
function technical(d, s) {
  const parts = [...vulnTable(d), ...list("Why it's ranked here", d.reasons),
    d.checkouts?.length > 1 ? MC.details(`${d.key}:checkouts`, false, { class: "paths" }, el("summary", { text: `${d.checkouts.length} checkouts` }),
      el("ul", {}, ...d.checkouts.map((c) => el("li", { text: c })))) : null,
    el("p", { class: "hint" }, el("span", { text: `Release notes assessed by ${s.generated_by || "n/a"} · ` }),
      d.release_notes_url && /^https?:/.test(d.release_notes_url) ? MC.safeLink(d.release_notes_url, d.vendor ? "download page" : "release notes") : el("span", { text: "no release notes link" }))];
  const n = (d.vulns_fixed || []).length + (d.vulns_remaining || []).length;
  return [MC.details(`${d.key}:technical`, !MC.SIMPLE && n > 0, { class: "fold technical" },
    el("summary", { text: n ? `Vulnerabilities and ranking · ${n}` : "Technical details" }), ...parts.filter(Boolean))];
}

function list(title, xs) {
  return xs && xs.length ? [el("h4", { text: title }), el("ul", {}, ...xs.map((x) => el("li", { text: x })))] : [];
}
})();
