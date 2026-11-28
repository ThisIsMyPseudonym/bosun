// Automations: owner-written rules, shipped-off presets, kill switch, renewal and history. #/automations
// All text goes through MC.el({ text }) / textContent, never innerHTML.
"use strict";
(() => {
const { el, api } = MC;

const A = { data: null, firings: null, at: 0, loading: false, error: "", editing: null };
const MODE_LABEL = { run: "Runs on its own", ask: "Asks first" };
const STATUS_CLASS = {
  done: "auto-ok",
  failed: "auto-bad",
  refused: "auto-bad",
  skipped: "auto-muted",
  missed: "auto-warn",
  waiting_quota: "auto-warn",
  scheduled: "auto-info",
  running: "auto-info",
  offered: "auto-warn",
  declined: "auto-muted",
  expired: "auto-muted",
};

async function fetchAutomations(force) {
  if (A.loading || (!force && A.data && Date.now() - A.at < 15000)) return;
  A.loading = true;
  try {
    const [listRes, firingsRes] = await Promise.all([
      api("/api/automations"),
      api("/api/automations/firings?limit=100"),
    ]);
    A.data = listRes;
    A.firings = firingsRes.firings || [];
    A.at = Date.now();
    A.error = "";
  } catch (err) {
    A.error = err.message;
  }
  A.loading = false;
  if (MC.route.parse().page === "automations") MC.render();
}

async function setPaused(paused) {
  try {
    await api("/api/automations/pause", { method: "POST", body: JSON.stringify({ on: paused }) });
    fetchAutomations(true);
  } catch (err) {
    alert(`Could not update pause state: ${err.message}`);
    fetchAutomations(true);
  }
}

async function toggleRule(rule, enabled, onEdit) {
  if (enabled && (rule.needs_project || (rule.needs_grant && (!rule.grant_digest || rule.grant_expired)))) {
    if (onEdit) onEdit(rule);
    fetchAutomations(true);
    return;
  }
  try {
    await api(`/api/automations/${rule.id}/enabled`, { method: "POST", body: JSON.stringify({ enabled }) });
    fetchAutomations(true);
  } catch (err) {
    alert(`Could not update automation: ${err.message}`);
    fetchAutomations(true);
  }
}

async function renewRule(rule) {
  try {
    await api(`/api/automations/${rule.id}/renew`, { method: "POST", body: "{}" });
    fetchAutomations(true);
  } catch (err) {
    alert(`Could not renew automation: ${err.message}`);
    fetchAutomations(true);
  }
}

async function deleteRule(rule) {
  if (!confirm(`Delete automation "${rule.name}"? Firing history will stay.`)) return;
  try {
    await api(`/api/automations/${rule.id}/delete`, { method: "POST", body: "{}" });
    if (A.editing && A.editing.id === rule.id) A.editing = null;
    fetchAutomations(true);
  } catch (err) {
    alert(`Could not delete automation: ${err.message}`);
    fetchAutomations(true);
  }
}

function headerBar(data, onNew) {
  const paused = !!data.paused;
  const isDemo = !!data.demo;
  const killBtn = el("button", {
    class: paused ? "primary" : "quiet auto-pausebtn",
    disabled: isDemo || null,
    text: paused ? "Resume automations" : "Pause all automations",
    onclick: () => setPaused(!paused),
  });
  const newBtn = el("button", {
    class: "primary",
    disabled: isDemo || null,
    text: "New automation",
    onclick: () => onNew && onNew(),
  });
  return el("div", { class: "pagehead auto-head" },
    el("div", {},
      el("h1", { text: "Automations" }),
      el("p", { class: "hint", text: "Owner-written rules: when a trigger fires and its conditions hold, run one Bosun action." })),
    el("div", { class: "auto-head-actions" }, killBtn, newBtn));
}

function ruleMeta(rule) {
  const parts = [`Today: ${rule.today_count || 0} / ${rule.max_per_day || 6}`];
  if (rule.last_fired) parts.push(`Last fired: ${MC.ago(rule.last_fired)}`);
  if (rule.next_due) parts.push(`Next due: ${MC.clock(rule.next_due)}`);
  if (rule.paused_reason) parts.push(`Paused: ${rule.paused_reason}`);
  return el("div", { class: "auto-meta", text: parts.join(" · ") });
}

function ruleCard(rule, isDemo, onEdit) {
  const toggle = el("input", {
    type: "checkbox",
    checked: rule.enabled || null,
    disabled: isDemo || null,
    "aria-label": `Enable ${rule.name}`,
    onchange: (e) => toggleRule(rule, !!e.target.checked, onEdit),
  });
  const chips = [
    el("span", { class: "auto-chip", text: MODE_LABEL[rule.mode] || rule.mode }),
    rule.preset ? el("span", { class: "auto-chip auto-preset", text: "Preset" }) : null,
    rule.paused_reason ? el("span", { class: "auto-chip auto-warn", text: "Paused" }) : null,
    rule.grant_expired ? el("span", { class: "auto-chip auto-bad", text: "Expired" }) : null,
  ];
  const renewal = rule.grant_expired ? el("div", { class: "auto-renew" },
    el("span", { text: "Still want this? Its 90-day install grant expired and it will not fire until renewed." }),
    el("button", {
      class: "primary",
      disabled: isDemo || null,
      text: "Renew",
      onclick: () => renewRule(rule),
    })) : null;
  const actions = el("div", { class: "auto-row-actions" },
    el("button", { class: "quiet", disabled: isDemo || null, text: "Edit", onclick: () => onEdit && onEdit(rule) }),
    el("button", { class: "quiet", disabled: isDemo || null, text: "Delete", onclick: () => deleteRule(rule) }));
  return el("article", { class: `panel auto-card${rule.grant_expired ? " auto-expired" : ""}` },
    el("div", { class: "auto-card-top" },
      el("label", { class: "auto-switch" }, toggle, el("strong", { text: rule.name })),
      el("div", { class: "auto-chips" }, ...chips),
      actions),
    el("p", { class: "auto-sentence", text: rule.sentence || rule.name }),
    ruleMeta(rule),
    renewal);
}

function historyRow(f) {
  const href = f.trace_id && MC.runHref ? MC.runHref(f.trace_id) : null;
  const statusCls = STATUS_CLASS[f.status] || "auto-muted";
  return el("div", { class: "auto-hrow" },
    el("span", { class: `auto-status ${statusCls}`, text: f.status || "unknown" }),
    el("div", { class: "auto-hbody" },
      el("div", { class: "auto-htop" },
        el("strong", { text: f.automation_name || `Automation #${f.automation_id}` }),
        el("span", { class: "hint", text: `${f.event_kind}: ${f.event_ref}` })),
      f.reason ? el("div", { class: "auto-hreason", text: f.reason }) : null),
    el("div", { class: "auto-hright" },
      href ? el("a", { class: "tracelink", href, text: "Run" }) : null,
      f.created ? el("span", { class: "hint", text: MC.ago(f.created) }) : null));
}

function historyPanel(firings) {
  const rows = firings || [];
  return el("section", { class: "auto-section" },
    el("h2", { text: "Firing history" }),
    rows.length
      ? el("div", { class: "panel auto-history" }, ...rows.map(historyRow))
      : el("p", { class: "empty", text: "No automation firings recorded yet." }));
}

function rulesSection(title, items, isDemo, onEdit) {
  if (!items.length) return null;
  return el("section", { class: "auto-section" },
    el("h2", { text: title }),
    el("div", { class: "auto-list" }, ...items.map((r) => ruleCard(r, isDemo, onEdit))));
}

MC.automationsHelpers = {
  headerBar,
  ruleCard,
  historyPanel,
  fetchAutomations,
  setPaused,
  toggleRule,
  renewRule,
  deleteRule,
};

MC.views.automations = function automations() {
  fetchAutomations(false);
  if (!A.data) {
    return el("div", { class: "automations" },
      el("div", { class: "pagehead" }, el("h1", { text: "Automations" })),
      el("p", { class: "empty", text: A.error ? `Could not load automations: ${A.error}` : "Loading…" }));
  }
  const data = A.data;
  const isDemo = !!data.demo;
  const all = data.automations || [];
  const custom = all.filter((r) => !r.preset);
  const presets = all.filter((r) => !!r.preset);
  const openEdit = (rule) => { A.editing = rule || { id: null }; MC.render(); };
  const closeEdit = () => { A.editing = null; MC.render(); };
  const pausedBanner = data.paused
    ? el("div", { class: "callout auto-paused-banner", text: "All automations are paused. Waiting firings stay frozen until you resume them." })
    : null;
  const demoBanner = isDemo
    ? el("div", { class: "callout auto-demo-banner", text: "Demo mode: switches and editing are disabled. Run `bosun serve` to manage your own automations." })
    : null;
  const editorNode = A.editing && MC.automationsHelpers.editorPanel
    ? MC.automationsHelpers.editorPanel(A.editing, data.projects || [], isDemo, closeEdit, () => { A.editing = null; fetchAutomations(true); })
    : null;
  return el("div", { class: "automations" },
    headerBar(data, () => openEdit(null)),
    pausedBanner,
    demoBanner,
    editorNode,
    rulesSection("Your automations", custom, isDemo, openEdit),
    rulesSection("Presets", presets, isDemo, openEdit),
    historyPanel(A.firings || []));
};
})();
