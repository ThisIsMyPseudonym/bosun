// AI usage: where Claude tokens go, so Bosun can be made cheaper. #/usage
// Costs are list price as Claude Code reports them; on a subscription they measure plan usage, not a bill.
"use strict";
(() => {
const { el, svg, api, plural } = MC;

const PURPOSE = { summarize: "Release-note summaries", explain: "Will this break?", fix: "Branch + test fixes", assistant: "Ask Bosun" };
const U = { data: null, days: 30, at: 0, loading: false, error: "" };

const usd = (n) => n == null ? "—" : n === 0 ? "$0" : n < 0.01 ? `$${n.toFixed(4)}` : n < 10 ? `$${n.toFixed(2)}` : `$${Math.round(n).toLocaleString()}`;
const tok = (n) => !n ? "0" : n < 1000 ? String(n) : n < 1e6 ? `${(n / 1000).toFixed(n < 1e4 ? 1 : 0)}K` : `${(n / 1e6).toFixed(1)}M`;
const allIn = (r) => (r.input_tokens || 0) + (r.cache_read_tokens || 0) + (r.cache_write_tokens || 0);

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

// one column per day, list-price cost; hover shows that day's split by purpose
function dailyChart(d) {
  const days = [];
  const byDay = new Map();
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
  const card = el("section", { class: "panel chart" }, el("h3", { text: "Cost per day" }),
    el("div", { class: "cs", text: `Last ${n} days, list price, every Claude call Bosun made` }));
  const W = 720, H = 190, X0 = 44, X1 = 712, Y0 = 164, Y1 = 10;
  const max = Math.max(...days.map((x) => x.cost), 0);
  if (!max) { card.append(el("p", { class: "empty", text: "No Claude calls in this period." })); return card; }
  const step = niceStep(max / 3), top = Math.ceil(max / step) * step;
  const y = (v) => Y0 - v / top * (Y0 - Y1);
  const slot = (X1 - X0) / days.length, bw = Math.min(24, slot - 2);
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
  const peak = days.reduce((a, b) => (b.cost > a.cost ? b : a));
  days.forEach((x, i) => {
    const cx = X0 + slot * i + slot / 2;
    if (x.cost > 0) {
      const h = Math.max(2, Y0 - y(x.cost)), r = Math.min(4, h / 2, bw / 2);
      // 4px rounded top, square at the baseline
      svg("path", { class: "ubar", d: `M${cx - bw / 2},${Y0}V${Y0 - h + r}q0,-${r} ${r},-${r}h${bw - 2 * r}q${r},0 ${r},${r}V${Y0}z` }, s);
    }
    const hit = svg("rect", { x: X0 + slot * i, y: Y1, width: slot, height: Y0 - Y1, fill: "transparent" }, s);
    hit.addEventListener("mousemove", (ev) => {
      const parts = Object.entries(x.parts).sort((a, b) => b[1] - a[1]).map(([p, c]) => `${PURPOSE[p] || p} ${usd(c)}`);
      const rect = card.getBoundingClientRect();
      tip.textContent = `${lbl(x.day)}: ${usd(x.cost)} · ${plural(x.calls, "call")}${parts.length ? `\n${parts.join("\n")}` : ""}`;
      tip.hidden = false; tip.style.left = `${ev.clientX - rect.left + 12}px`; tip.style.top = `${ev.clientY - rect.top - 34}px`;
    });
    hit.addEventListener("mouseleave", () => { tip.hidden = true; });
  });
  const px = X0 + slot * days.indexOf(peak) + slot / 2;
  svg("text", { class: "label", x: Math.min(Math.max(px, X0 + 30), X1 - 30), y: y(peak.cost) - 6, "text-anchor": "middle" }, s).textContent = usd(peak.cost);
  card.append(s, tip,
    MC.details("usage:daytable", false, {}, el("summary", { text: "As a table" }), table(["Day", "Calls", "Cost"],
      days.filter((x) => x.calls).reverse().map((x) => [lbl(x.day), String(x.calls), usd(x.cost)]), [false, true, true])));
  return card;
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

// a share bar: one hue, the row's part of the period's total
const share = (part, total) => el("span", { class: "ushare", title: total ? `${Math.round(part / total * 100)}% of the total` : "" },
  el("i", { style: { width: `${total ? Math.max(2, part / total * 100) : 0}%` } }));

function breakdown(title, rows, total, name) {
  if (!rows.length) return null;
  return el("section", { class: "panel" }, el("h3", { text: title }),
    table(["", "Calls", "Tokens in", "Out", "Cost", ""], rows.map((r) => [name(r), String(r.calls), tok(allIn(r)), tok(r.output_tokens),
      usd(r.cost_usd), share(r.cost_usd || 0, total)]), [false, true, true, true, true, false]));
}

function budgetForm(d) {
  const b = d.budgets;
  const field = (key, label, hint) => el("label", { class: "ufield" }, el("span", { text: label }),
    el("span", { class: "uinput" }, "$", el("input", { type: "number", min: "0", step: "0.5", value: String(b[key]), name: key, "aria-label": label })),
    el("span", { class: "hint", text: hint }));
  const form = el("form", { class: "ubudget", onsubmit: async (e) => {
    e.preventDefault();
    const body = {};
    for (const k of ["refresh_usd", "daily_usd", "assistant_usd"]) body[k] = Number(form.elements[k].value);
    try { await api("/api/usage/budget", { method: "POST", body: JSON.stringify(body) }); fetchUsage(true); }
    catch (err) { alert(`Could not save: ${err.message}`); }
  } },
    field("refresh_usd", "Per refresh", "Release-note summaries in one refresh. Past it, the rest use built-in rules."),
    field("daily_usd", "Per 24 hours", "Summaries and “Will this break?” together. 0 means no cap."),
    field("assistant_usd", "Ask Bosun, per 24 hours", `The assistant's own cap. Used in the last 24 hours: ${usd(d.assistant_today_usd || 0)}. Past it, the panel says so and doesn't call Claude.`),
    el("button", { class: "quiet", type: "submit", text: "Save" }),
    d.env_overrides.length ? el("p", { class: "hint", text: `Set by environment, which wins: ${d.env_overrides.join(", ")}` }) : null);
  return el("section", { class: "panel" }, el("h3", { text: "Budgets" }),
    el("p", { class: "hint", text: "Branch + test fixes have their own cap per run and only count toward the totals." }), form);
}

// What agents (Claude Code, through `bosun mcp`) may do beyond reading, changing Bosun's own state and installing.
// Only this page can change it: the server refuses the same request from an agent.
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
    el("p", { class: "hint", text: "Claude Code can ask Bosun questions and update software through it (bosun mcp install). It asks you before each action. These two it can't do at all unless you turn them on, and it can't change these switches, the budgets or Autopilot." }),
    row("system", "Let agents quit and reopen apps and open system updaters", "Relaunching an app to apply its update, opening a Mac app, opening Software Update or Windows Update."),
    row("external", "Let agents push branches and open pull requests", "The one step that leaves this computer: pushing a Branch + test fix and opening its pull request."));
}

MC.views.usage = function usage(S) {
  fetchUsage();
  const range = el("div", { class: "switch", role: "group", "aria-label": "Period" }, ...[7, 30, 90].map((n) =>
    el("button", { class: U.days === n ? "on" : null, "aria-pressed": String(U.days === n), text: `${n} days`,
      onclick: () => { U.days = n; fetchUsage(true); MC.render(); } })));
  const head = el("div", { class: "pagehead" }, el("h1", {}, el("span", { text: "AI usage" }),
      el("button", { class: "quiet", text: "Why is this expensive?", title: "Ask Bosun",
        onclick: () => MC.ask("Why is this expensive? Where do the Claude tokens go, and what would cut it?", { page: "usage" }) })),
    el("p", { class: "hint", text: "Every Claude call Bosun makes, what it was for and what it cost at list price. On a Claude subscription nothing is billed per call; this is how much of your plan's usage Bosun takes." }));
  const d = U.data;
  if (!d || d.days !== U.days) return el("div", { class: "runs usage" }, head, el("div", { class: "runfilters" }, range), el("p", { class: "empty", text: U.error || "Loading…" }));
  const t = d.totals, total = t.cost_usd || 0, cache = d.cache;
  const name = (r) => r.item_key ? MC.nameOf(S, r.item_key) : "—";
  const runLink = (r, text) => r.trace_id ? el("a", { href: MC.runHref(r.trace_id), text }) : text;
  return el("div", { class: "runs usage" }, head, el("div", { class: "runfilters" }, range),
    el("div", { class: "utiles" },
      tile(`Last ${d.days} days`, usd(total), `${plural(t.calls || 0, "call")} · ${tok(allIn(t))} tokens in, ${tok(t.output_tokens)} out`),
      tile("Last 24 hours", usd(d.today_usd), d.budgets.daily_usd ? `of ${usd(d.budgets.daily_usd)} budget` : "no budget set"),
      tile("Summaries from cache", cache.hit_rate == null ? "—" : `${Math.round(cache.hit_rate * 100)}%`,
        cache.summaries_asked ? `${cache.summaries_from_cache} of ${cache.summaries_asked} needed no call` : "none asked yet"),
      tile("Cache reads", allIn(t) ? `${Math.round((t.cache_read_tokens || 0) / allIn(t) * 100)}%` : "—", "of input tokens came from Claude's prompt cache")),
    dailyChart(d),
    el("div", { class: "ugrid" },
      breakdown("By purpose", d.by_purpose, total, (r) => PURPOSE[r.purpose] || r.purpose),
      breakdown("By model", d.by_model, total, (r) => r.model),
      breakdown("By source", d.by_source.slice(0, 10), total, (r) => r.source),
      budgetForm(d), agentPolicy(S)),
    d.top_items.length ? el("section", { class: "panel" }, el("h3", { text: "Costliest items" }),
      table(["Item", "For", "Calls", "Tokens in", "Cost", ""], d.top_items.slice(0, 15).map((r) => [name(r), PURPOSE[r.purpose] || r.purpose,
        String(r.calls), tok(allIn(r)), usd(r.cost_usd), share(r.cost_usd || 0, total)]), [false, false, true, true, true, false])) : null,
    d.outliers.length ? el("section", { class: "panel" }, el("h3", { text: "Outliers" }),
      el("p", { class: "hint", text: "Calls that cost more than three times the usual for their purpose: the first place to look for savings." }),
      table(["Item", "For", "Prompt", "Cost", "vs usual", ""], d.outliers.map((r) => [name(r), PURPOSE[r.purpose] || r.purpose,
        r.prompt_chars ? `${tok(r.prompt_chars)} chars` : "—", usd(r.cost_usd), `${r.times_median}×`, runLink(r, "run")]), [false, false, true, true, true, false])) : null,
    d.top_calls.length ? MC.details("usage:calls", false, { class: "panel" }, el("summary", {}, el("h3", { text: "Costliest calls" })),
      table(["When", "For", "Item", "Model", "Tokens in", "Out", "Cost", ""], d.top_calls.map((r) => [MC.ago(r.ts), PURPOSE[r.purpose] || r.purpose, name(r),
        Object.keys(r.models || {}).join(", ") || r.model, tok(allIn(r)), tok(r.output_tokens), usd(r.cost_usd), runLink(r, "run")]),
        [false, false, false, false, true, true, true, false])) : null);
};
})();
