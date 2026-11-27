// AI usage: quota windows, pace, Bosun's share, routing, and token detail. #/usage
// All untrusted or provider text goes through MC.el({ text }) / textContent, never innerHTML.
"use strict";
(() => {
const { el, svg, api, plural } = MC;

const JOB_LABEL = {
  summarize: "Release-note summaries", explain: "Will this break?", triage: "Error triage",
  fix: "Branch + test fixes", assistant: "Ask Bosun", computer_use: "In-app updates",
};
const U = { data: null, days: 30, at: 0, loading: false, error: "" };

const usd = (n) => n == null ? "—" : n === 0 ? "$0" : n < 0.01 ? `$${n.toFixed(4)}` : n < 10 ? `$${n.toFixed(2)}` : `$${Math.round(n).toLocaleString()}`;
const tok = (n) => !n ? "0" : n < 1000 ? String(n) : n < 1e6 ? `${(n / 1000).toFixed(n < 1e4 ? 1 : 0)}K` : `${(n / 1e6).toFixed(1)}M`;
const allIn = (r) => (r.input_tokens || 0) + (r.cache_read_tokens || 0) + (r.cache_write_tokens || 0);
const fmtPct = (v) => v == null || !Number.isFinite(v) ? "unknown" : `${Math.min(100, Math.max(0, Math.round(v * 100)))}%`;
const fmtPace = (v) => v == null || !Number.isFinite(v) ? "—" : `${v.toFixed(2)}×`;
const fmtDraw = (v) => v == null || !Number.isFinite(v) ? "—" : `${Math.min(100, Math.max(0, Math.round(v * 100)))}%`;
const loginName = (lg) => lg.label && lg.label !== "Default" ? `${lg.provider_label} (${lg.label})` : lg.provider_label;

async function fetchUsage(force) {
  if (U.loading || (!force && U.data && U.data.days === U.days && Date.now() - U.at < 15000)) return;
  U.loading = true;
  try { U.data = await api(`/api/usage?days=${U.days}`); U.at = Date.now(); U.error = ""; }
  catch (e) { U.error = e.message; }
  U.loading = false;
  if (MC.route.parse().page === "usage") MC.render();
}

function tile(label, value, sub) {
  return el("div", { class: "utile" }, el("div", { class: "ulabel", text: label }), el("div", { class: "uvalue", text: value }),
    sub ? el("div", { class: "usub", text: sub }) : null);
}

function niceStep(raw) {
  if (raw <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(raw)), f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}

function table(head, rows, num) {
  return el("table", { class: "utable" },
    el("thead", {}, el("tr", {}, ...head.map((h, i) => el("th", { class: num && num[i] ? "num" : null, text: h })))),
    el("tbody", {}, ...rows.map((r) => el("tr", {}, ...r.map((c, i) => el("td", { class: num && num[i] ? "num" : null }, c))))));
}

const shareBar = (part, total) => el("span", { class: "ushare", title: total ? `${Math.round(part / total * 100)}% of the total` : "" },
  el("i", { style: { width: `${total ? Math.max(2, part / total * 100) : 0}%` } }));

function windowRow(w) {
  const unknown = w.stale || w.share == null;
  const pct = unknown ? null : Math.min(100, Math.max(0, w.share * 100));
  const fillCls = unknown ? "qfill unk" : (w.status === "rejected" || w.share >= 0.75 ? "qfill qover" : "qfill");
  const metaParts = unknown ? ["Stale or unknown reading"] : [
    `Pace ${fmtPace(w.pace)}`,
    w.reset ? `Resets ${w.reset}` : "Reset unknown",
    w.bosun_share != null ? `Bosun's share: ${fmtDraw(w.bosun_share)}` : null,
    w.source && w.source !== "output" ? `source: ${w.source}` : null,
  ].filter(Boolean);
  return el("div", { class: "qwin" },
    el("div", { class: "qwinhead" },
      el("span", { class: "qwinlbl", text: w.label || w.window }),
      el("span", { class: "qwinpct", text: unknown ? "unknown" : fmtPct(w.share) })),
    el("div", { class: "qbar", role: "img", "aria-label": `${w.label || w.window}: ${unknown ? "unknown" : fmtPct(w.share)}` },
      unknown ? null : el("i", { class: fillCls, style: { width: `${pct}%` } }),
      el("span", { class: "qt75", title: "75% line: background work pauses here", style: { left: "75%" } })),
    el("div", { class: "qmeta", text: metaParts.join(" · ") }));
}

async function setOverride(bucket, on) {
  try {
    await api("/api/ai/override", { method: "POST", body: JSON.stringify({ bucket, on }) });
    fetchUsage(true);
  } catch (err) { alert(`Could not update override: ${err.message}`); }
}

function loginCard(lg, isDemo) {
  const badges = [];
  if (lg.estimated) badges.push(el("span", { class: "qbadge", text: "Estimated" }));
  if (lg.excluded) badges.push(el("span", { class: "qbadge", text: "Excluded" }));
  if (lg.limited) badges.push(el("span", { class: "qbadge qbad", text: "At limit" }));
  else if (lg.line_hit) badges.push(el("span", { class: "qbadge qwarn", text: "Past 75%" }));
  const body = lg.windows && lg.windows.length
    ? lg.windows.map(windowRow)
    : [el("p", { class: "hint", text: lg.estimated
        ? `Estimated from calls and tokens: ${plural(lg.calls || 0, "call")} · ${tok(lg.tokens)} tokens.`
        : "No current window reading yet — share unknown." })];
  const foot = [];
  if (lg.override) {
    foot.push(el("div", { class: "qfoot" },
      el("span", { class: "hint", text: `Background work may pass 75% until ${lg.override.reset}.` }),
      el("button", { class: "quiet", disabled: isDemo || null, text: "Clear override",
        onclick: () => setOverride(lg.bucket, false) })));
  } else if (lg.line_hit) {
    foot.push(el("div", { class: "qfoot" },
      el("button", { class: "quiet", disabled: isDemo || null,
        text: "Let background work go past 75% until it resets",
        onclick: () => setOverride(lg.bucket, true) })));
  }
  return el("section", { class: "panel qcard" },
    el("div", { class: "qhead" },
      el("h3", { text: loginName(lg) }),
      el("span", { class: "qbadges" }, ...badges)),
    ...body, ...foot);
}

function retiredBanner(retired) {
  const old = [];
  if (retired.refresh_usd != null) old.push(`refresh $${retired.refresh_usd}`);
  if (retired.daily_usd != null) old.push(`24h $${retired.daily_usd}`);
  if (retired.assistant_usd != null) old.push(`assistant $${retired.assistant_usd}`);
  const prev = old.length ? ` Your previous caps (${old.join(", ")}) are no longer enforced.` : "";
  return el("section", { class: "panel qnotice" },
    el("div", { class: "qnoticerow" },
      el("div", {},
        el("strong", { text: "Dollar budgets have been retired" }),
        el("p", { class: "hint", text: `Bosun now measures use by each plan's own quota windows and pauses background work at 75%.${prev}` })),
      el("button", { class: "quiet", text: "Dismiss", onclick: async () => {
        try { await api("/api/ai/notice/dismiss", { method: "POST", body: "{}" }); fetchUsage(true); }
        catch (err) { alert(`Could not dismiss: ${err.message}`); }
      } })));
}

function noticesPanel(notices) {
  if (!notices || !notices.length) return null;
  return el("section", { class: "panel qnotice" },
    el("h3", { text: "Paused at 75%" }),
    ...notices.map((n) => el("div", { class: "qnoticeitem" },
      el("strong", { text: n.title }),
      el("p", { class: "hint", text: n.body }))));
}

async function saveRouting(body) {
  try {
    await api("/api/ai/routing", { method: "POST", body: JSON.stringify(body) });
    fetchUsage(true);
  } catch (err) { alert(`Could not save routing: ${err.message}`); }
}

function routingPanel(routing, logins, isDemo) {
  const r = routing || { mode: "default", order: [], pins: {}, jobs: [] };
  const buckets = logins.map((lg) => lg.bucket);
  const labelFor = new Map(logins.map((lg) => [lg.bucket, loginName(lg)]));
  const ordered = [...new Set([...(r.order || []).filter((b) => labelFor.has(b)), ...buckets])];
  const modeBtns = el("div", { class: "switch", role: "group", "aria-label": "Routing mode" },
    ...[["default", "Default order"], ["balance", "Balance by pace"]].map(([m, lbl]) =>
      el("button", { class: r.mode === m ? "on" : null, "aria-pressed": String(r.mode === m), disabled: isDemo || null,
        text: lbl, onclick: () => saveRouting({ mode: m }) })));
  const move = (idx, delta) => {
    const next = [...ordered];
    const swap = idx + delta;
    if (swap < 0 || swap >= next.length) return;
    [next[idx], next[swap]] = [next[swap], next[idx]];
    saveRouting({ order: next });
  };
  const orderRows = ordered.length > 1 ? el("div", { class: "qorder" },
    el("div", { class: "ulabel", text: "Preference order" }),
    ...ordered.map((b, idx) => el("div", { class: "qorderrow" },
      el("span", { text: `${idx + 1}. ${labelFor.get(b) || b}` }),
      el("span", { class: "qorderbtns" },
        el("button", { class: "quiet", disabled: isDemo || idx === 0 || null, "aria-label": `Move ${b} up`,
          text: "↑", onclick: () => move(idx, -1) }),
        el("button", { class: "quiet", disabled: isDemo || idx === ordered.length - 1 || null, "aria-label": `Move ${b} down`,
          text: "↓", onclick: () => move(idx, 1) }))))) : null;
  const pinRows = (r.jobs || []).map((job) => {
    const cur = (r.pins && r.pins[job]) || "";
    const sel = el("select", { "aria-label": `Pin for ${JOB_LABEL[job] || job}`, disabled: isDemo || null,
      onchange: () => saveRouting({ pins: { [job]: sel.value || null } }) },
      el("option", { value: "", selected: !cur || null, text: "Auto (follow mode)" }),
      ...ordered.map((b) => el("option", { value: b, selected: cur === b || null, text: labelFor.get(b) || b })));
    return el("label", { class: "qpin" }, el("span", { text: JOB_LABEL[job] || job }), sel);
  });
  return el("section", { class: "panel" },
    el("h3", { text: "Routing" }),
    el("p", { class: "hint", text: isDemo
      ? "Demo mode: routing controls show made-up settings and cannot be changed here."
      : "Choose your preferred login order or balance by pace. Any job can be pinned to one login." }),
    modeBtns, orderRows,
    pinRows.length ? el("div", { class: "qpins" }, el("div", { class: "ulabel", text: "Per-job pins" }), ...pinRows) : null);
}

function topJobsPanel(jobs) {
  if (!jobs || !jobs.length) return null;
  return el("section", { class: "panel" },
    el("h3", { text: "Top jobs by draw" }),
    table(["Job", "Calls", "Window draw", "Tokens"],
      jobs.map((j) => [JOB_LABEL[j.job] || j.job, String(j.calls), fmtDraw(j.window_draw), tok(j.tokens)]),
      [false, true, true, true]));
}

function dailyChart(d) {
  const days = [], byDay = new Map();
  for (const r of d.by_day) {
    const e = byDay.get(r.day) || { day: r.day, cost: 0, calls: 0, parts: {} };
    e.cost += r.cost_usd || 0; e.calls += r.calls; e.parts[r.purpose] = (e.parts[r.purpose] || 0) + (r.cost_usd || 0);
    byDay.set(r.day, e);
  }
  const end = new Date(); end.setHours(0, 0, 0, 0);
  const n = Math.min(Math.ceil(d.days), 60);
  for (let i = n - 1; i >= 0; i--) {
    const t = new Date(end); t.setDate(end.getDate() - i);
    const key = `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}`;
    days.push(byDay.get(key) || { day: key, cost: 0, calls: 0, parts: {} });
  }
  const tip = el("div", { class: "tip", hidden: true });
  const card = el("section", { class: "panel chart" }, el("h3", { text: "List-price cost per day" }),
    el("div", { class: "cs", text: `Last ${n} days, detail only` }));
  const W = 720, H = 190, X0 = 44, X1 = 712, Y0 = 164, Y1 = 10;
  const max = Math.max(...days.map((x) => x.cost), 0);
  if (!max) { card.append(el("p", { class: "empty", text: "No calls in this period." })); return card; }
  const step = niceStep(max / 3), top = Math.ceil(max / step) * step;
  const y = (v) => Y0 - v / top * (Y0 - Y1), slot = (X1 - X0) / days.length, bw = Math.min(24, slot - 2);
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `Cost per day: ${days.filter((x) => x.cost).map((x) => `${x.day} ${usd(x.cost)}`).join(", ")}` });
  for (let v = 0; v <= top + 1e-9; v += step) {
    svg("line", { class: "grid", x1: X0, x2: X1, y1: y(v), y2: y(v) }, s);
    svg("text", { class: "axis", x: X0 - 6, y: y(v) + 3.5, "text-anchor": "end" }, s).textContent = usd(v);
  }
  const lbl = (k) => new Date(`${k}T00:00`).toLocaleDateString([], { month: "short", day: "numeric" });
  [[0, "start"], [days.length - 1, "end"]].forEach(([i, a]) => {
    svg("text", { class: "axis", x: X0 + slot * i + slot / 2 + (a === "start" ? -bw / 2 : bw / 2), y: H - 6, "text-anchor": a }, s).textContent = lbl(days[i].day);
  });
  days.forEach((x, i) => {
    const cx = X0 + slot * i + slot / 2;
    if (x.cost > 0) {
      const h = Math.max(2, Y0 - y(x.cost)), r = Math.min(4, h / 2, bw / 2);
      svg("path", { class: "ubar", d: `M${cx - bw / 2},${Y0}V${Y0 - h + r}q0,-${r} ${r},-${r}h${bw - 2 * r}q${r},0 ${r},${r}V${Y0}z` }, s);
    }
    const hit = svg("rect", { x: X0 + slot * i, y: Y1, width: slot, height: Y0 - Y1, fill: "transparent" }, s);
    hit.addEventListener("mousemove", (ev) => {
      const parts = Object.entries(x.parts).sort((a, b) => b[1] - a[1]).map(([p, c]) => `${JOB_LABEL[p] || p} ${usd(c)}`);
      const rect = card.getBoundingClientRect();
      tip.textContent = `${lbl(x.day)}: ${usd(x.cost)} · ${plural(x.calls, "call")}${parts.length ? `\n${parts.join("\n")}` : ""}`;
      tip.hidden = false; tip.style.left = `${ev.clientX - rect.left + 12}px`; tip.style.top = `${ev.clientY - rect.top - 34}px`;
    });
    hit.addEventListener("mouseleave", () => { tip.hidden = true; });
  });
  card.append(s, tip);
  return card;
}

function breakdown(title, rows, total, name) {
  if (!rows.length) return null;
  return el("section", { class: "panel" }, el("h3", { text: title }),
    table(["", "Calls", "Tokens in", "Out", "Cost", ""], rows.map((r) => [name(r), String(r.calls), tok(allIn(r)), tok(r.output_tokens),
      usd(r.cost_usd), shareBar(r.cost_usd || 0, total)]), [false, true, true, true, true, false]));
}

function agentPolicy(S) {
  const row = (key, label, hint) => {
    const box = el("input", { type: "checkbox", checked: S.agents[key] ? "" : null, "aria-label": label, onchange: async () => {
      try { S.agents = await api("/api/agents/policy", { method: "POST", body: JSON.stringify({ [key]: box.checked }) }); }
      catch (err) { alert(`Could not save: ${err.message}`); }
      MC.render();
    } });
    return el("label", { class: "uswitch" }, box, el("span", {}, el("span", { text: label }), el("span", { class: "hint", text: hint })));
  };
  return el("section", { class: "panel" }, el("h3", { text: "Agents" }),
    el("p", { class: "hint", text: "Claude Code can ask Bosun questions and update software through it (bosun mcp install). It asks you before each action. These two it can't do at all unless you turn them on, and it can't change these switches, AI routing, overrides or Autopilot." }),
    row("system", "Let agents quit and reopen apps and open system updaters", "Relaunching an app to apply its update, opening a Mac app, opening Software Update or Windows Update."),
    row("external", "Let agents push branches and open pull requests", "The one step that leaves this computer: pushing a Branch + test fix and opening its pull request."));
}

function detailSection(S, det) {
  const t = det.totals, total = t.cost_usd || 0, cache = det.cache;
  const name = (r) => r.item_key ? MC.nameOf(S, r.item_key) : "—";
  const runLink = (r, text) => r.trace_id ? el("a", { href: MC.runHref(r.trace_id), text }) : text;
  return MC.details("usage:detail", false, { class: "panel udetail" },
    el("summary", {}, el("h3", { text: "Detail" }), el("span", { class: "hint", text: ` · ${plural(t.calls || 0, "call")} · ${tok(allIn(t))} in, ${tok(t.output_tokens)} out · ${usd(total)} list price` })),
    el("div", { class: "utiles" },
      tile(`Last ${det.days} days`, usd(total), `${plural(t.calls || 0, "call")} · ${tok(allIn(t))} in, ${tok(t.output_tokens)} out`),
      tile("Summaries from cache", cache.hit_rate == null ? "—" : `${Math.round(cache.hit_rate * 100)}%`,
        cache.summaries_asked ? `${cache.summaries_from_cache} of ${cache.summaries_asked} needed no call` : "none asked yet"),
      tile("Cache reads", allIn(t) ? `${Math.round((t.cache_read_tokens || 0) / allIn(t) * 100)}%` : "—", "of input tokens came from prompt cache")),
    dailyChart(det),
    el("div", { class: "ugrid" },
      breakdown("By job", det.by_purpose, total, (r) => JOB_LABEL[r.purpose] || r.purpose),
      breakdown("By model", det.by_model, total, (r) => r.model),
      breakdown("By source", det.by_source.slice(0, 10), total, (r) => r.source)),
    det.top_items.length ? el("section", { class: "panel" }, el("h3", { text: "Costliest items" }),
      table(["Item", "For", "Calls", "Tokens in", "Cost", ""], det.top_items.slice(0, 15).map((r) => [name(r), JOB_LABEL[r.purpose] || r.purpose,
        String(r.calls), tok(allIn(r)), usd(r.cost_usd), shareBar(r.cost_usd || 0, total)]), [false, false, true, true, true, false])) : null,
    det.outliers.length ? el("section", { class: "panel" }, el("h3", { text: "Outliers" }),
      table(["Item", "For", "Prompt", "Cost", "vs usual", ""], det.outliers.map((r) => [name(r), JOB_LABEL[r.purpose] || r.purpose,
        r.prompt_chars ? `${tok(r.prompt_chars)} chars` : "—", usd(r.cost_usd), `${r.times_median}×`, runLink(r, "run")]), [false, false, true, true, true, false])) : null,
    det.top_calls.length ? el("section", { class: "panel" }, el("h3", { text: "Recent costly calls" }),
      table(["When", "For", "Item", "Model", "Tokens in", "Out", "Cost", ""], det.top_calls.map((r) => [MC.ago(r.ts), JOB_LABEL[r.purpose] || r.purpose, name(r),
        Object.keys(r.models || {}).join(", ") || r.model, tok(allIn(r)), tok(r.output_tokens), usd(r.cost_usd), runLink(r, "run")]),
        [false, false, false, false, true, true, true, false])) : null);
}

MC.usageHelpers = { fmtPct, fmtPace, fmtDraw, windowRow, loginCard, retiredBanner, routingPanel, topJobsPanel };

MC.views.usage = function usage(S) {
  fetchUsage();
  const range = el("div", { class: "switch", role: "group", "aria-label": "Period" }, ...[7, 30, 90].map((n) =>
    el("button", { class: U.days === n ? "on" : null, "aria-pressed": String(U.days === n), text: `${n} days`,
      onclick: () => { U.days = n; fetchUsage(true); MC.render(); } })));
  const head = el("div", { class: "pagehead" }, el("h1", {}, el("span", { text: "AI usage" }),
      el("button", { class: "quiet", text: "Why is this expensive?", title: "Ask Bosun",
        onclick: () => MC.ask("Why is this expensive? Where is Bosun's AI quota going, and what would cut it?", { page: "usage" }) })),
    el("p", { class: "hint", text: "Each installed AI login's plan windows, pace and Bosun's share. Background work pauses at the 75% line; jobs you click keep going." }));
  const d = U.data;
  if (!d || d.days !== U.days) return el("div", { class: "runs usage" }, head, el("div", { class: "runfilters" }, range), el("p", { class: "empty", text: U.error || "Loading…" }));
  const logins = d.logins || [];
  const cards = logins.length
    ? el("div", { class: "ugrid" }, ...logins.map((lg) => loginCard(lg, !!d.demo)))
    : el("section", { class: "panel" }, el("p", { class: "empty", text: "No AI logins found yet. Install an AI CLI and run a refresh." }));
  return el("div", { class: "runs usage" }, head, el("div", { class: "runfilters" }, range),
    d.budgets_retired ? retiredBanner(d.budgets_retired) : null,
    noticesPanel(d.notices),
    cards,
    el("div", { class: "ugrid" }, topJobsPanel(d.top_jobs), routingPanel(d.routing, logins, !!d.demo), agentPolicy(S)),
    detailSection(S, d.detail || d));
};
})();
