// Dropdown-only automation editor with scope grant confirmation.
// All text goes through MC.el({ text }) / textContent, never innerHTML.
"use strict";
(() => {
const { el, api } = MC;

const UNSET_PROJECT = "__pick_project__";
const INSTALL_ACTIONS = new Set(["upgrade", "start_fix", "pin_mcp"]);
const SYSTEM_ACTIONS = new Set(["relaunch_app", "open_app"]);
const PROJECT_ACTIONS = new Set(["start_fix", "explain_item", "recheck_project"]);
const TRIGGERS = [["refresh", "After a full refresh"], ["schedule", "On a schedule"]];
const ACTIONS = [
  ["notify", "Notify (What changed digest)"],
  ["refresh", "Re-scan (no Autopilot)"],
  ["recheck_project", "Re-check one project"],
  ["explain_item", "Run 'Will this break?'"],
  ["start_fix", "Run Branch + test"],
  ["relaunch_app", "Offer to relaunch app (asks first)"],
  ["open_app", "Offer to open app updater (asks first)"],
];
const TIMES = ["00:00", "01:00", "02:00", "03:00", "04:00", "06:00", "08:00", "12:00", "18:00", "22:00"];
const CADENCES = [
  ["daily", "Every day", []],
  ["weekdays", "Weekdays (Mon–Fri)", ["mon", "tue", "wed", "thu", "fri"]],
  ["weekly_mon", "Mondays", ["mon"]],
  ["weekly_sun", "Sundays", ["sun"]],
];
const LANES = [["now", "Do now"], ["batch", "Batch"]];
const COLLECTORS = [["", "All collectors"], ["brew", "Homebrew"], ["repos", "Code projects"], ["mac-apps", "Mac apps"]];
const APPS = [["chrome", "Chrome"], ["firefox", "Firefox"], ["edge", "Edge"], ["brave", "Brave"]];
const MAX_PER_DAY = [1, 2, 3, 6, 12, 24];
const COOLDOWNS = [[0, "None"], [300, "5 minutes"], [900, "15 minutes"], [3600, "1 hour"]];
const WAIT_OPTS = [[0, "Immediately"], [300, "5 minutes"], [900, "15 minutes"]];

function selectEl(label, options, value, disabled, onChange) {
  return el("select", {
    "aria-label": label,
    disabled: disabled || null,
    onchange: (e) => onChange(e.target.value),
  }, ...options.map(([val, text]) => el("option", { value: String(val), text, selected: String(val) === String(value) || null })));
}

function field(label, control) {
  return el("div", { class: "auto-field" }, el("label", { text: label }), control);
}

function cadenceKey(days) {
  const joined = (days || []).join(",");
  if (!joined) return "daily";
  if (joined === "mon,tue,wed,thu,fri") return "weekdays";
  if (joined === "mon") return "weekly_mon";
  if (joined === "sun") return "weekly_sun";
  return "daily";
}

function initState(rule) {
  const r = rule || {};
  const trig = r.trigger || { kind: "refresh" };
  const cond = r.conditions || {};
  const scope = r.scope || {};
  const action = r.action || "notify";
  const rawProj = scope.project || cond.project || "";
  const project = rawProj === UNSET_PROJECT ? "" : rawProj;
  const mode = SYSTEM_ACTIONS.has(action) ? "ask" : action === "notify" ? "run" : (r.mode || "run");
  return {
    id: r.id || null,
    name: r.name || "New automation",
    triggerKind: trig.kind === "schedule" ? "schedule" : "refresh",
    time: trig.time || "02:00",
    cadence: cadenceKey(trig.days),
    refreshCond: cond.newly_exploited ? "kev" : cond.any_failed ? "failed" : (cond.new_in_lanes && cond.new_in_lanes.length) ? "now" : "any",
    action,
    mode,
    project,
    lane: scope.lane || cond.lane || "now",
    collector: Array.isArray(scope.only) && scope.only.length ? scope.only[0] : "",
    app: scope.app || "chrome",
    maxPerDay: Number(r.max_per_day || 6),
    cooldownS: Number(r.cooldown_s != null ? r.cooldown_s : 900),
    waitS: Number(r.wait_s || 0),
    modelJobs: Number(r.model_jobs_per_day != null ? r.model_jobs_per_day : 3),
    enabled: !!r.enabled,
    confirmGrant: false,
    saving: false,
  };
}

function scopeSentence(st, projects) {
  const projName = (projects.find((p) => p.key === st.project) || {}).name || st.project || "a chosen project";
  const cap = `at most ${st.maxPerDay} time${st.maxPerDay === 1 ? "" : "s"} a day`;
  if (st.action === "start_fix") {
    return `Run Branch + test on ${st.lane} items in ${projName}, ${cap} (sandboxed, never pushes; Bosun's checks and 75% quota line still apply).`;
  }
  if (st.action === "explain_item") return `Run 'Will this break?' on ${st.lane} items in ${projName}, ${cap}.`;
  if (st.action === "recheck_project") return `Re-check project ${projName}, ${cap}.`;
  if (st.action === "refresh") return `Re-scan ${st.collector || "all collectors"} without Autopilot, ${cap}.`;
  if (st.action === "notify") return `Send a 'What changed' summary line after a refresh, ${cap}.`;
  return `Offer to ${st.action.replace("_", " ")} ${st.app} (asks for your approval each time).`;
}

function buildPayload(st) {
  const cad = CADENCES.find((c) => c[0] === st.cadence) || CADENCES[0];
  const trigger = st.triggerKind === "schedule" ? { kind: "schedule", time: st.time, days: cad[2] } : { kind: "refresh" };
  let conditions = {};
  if (st.triggerKind === "refresh") {
    conditions = {
      new_in_lanes: st.refreshCond === "now" ? ["now"] : [],
      newly_exploited: st.refreshCond === "kev",
      any_failed: st.refreshCond === "failed",
    };
  } else {
    conditions = PROJECT_ACTIONS.has(st.action) ? { project: st.project || null, lane: st.lane } : {};
  }
  let scope = { digest: "what_changed" };
  if (st.action === "refresh") scope = { only: st.collector ? [st.collector] : null };
  else if (st.action === "recheck_project") scope = { mode: "project", project: st.project };
  else if (st.action === "explain_item" || st.action === "start_fix") scope = { project: st.project, lane: st.lane };
  else if (SYSTEM_ACTIONS.has(st.action)) scope = { app: st.app };
  return {
    id: st.id,
    name: st.name.trim(),
    trigger,
    conditions,
    action: st.action,
    scope,
    mode: SYSTEM_ACTIONS.has(st.action) ? "ask" : st.action === "notify" ? "run" : st.mode,
    enabled: !!st.enabled,
    wait_s: st.waitS,
    max_per_day: st.maxPerDay,
    cooldown_s: st.cooldownS,
    model_jobs_per_day: st.modelJobs,
    confirm_grant: !!st.confirmGrant,
  };
}

function modeOptions(action) {
  if (SYSTEM_ACTIONS.has(action)) return [["ask", "Asks first (required for system actions)"]];
  if (action === "notify") return [["run", "Runs on its own"]];
  return [["run", "Runs on its own"], ["ask", "Asks first"]];
}

function buildDropdownControls(st, projects, isDemo, update) {
  const projOpts = [["", "Pick a project…"], ...(projects || []).map((p) => [p.key, p.name || p.key])];
  const timeOpts = TIMES.includes(st.time) ? TIMES.map((t) => [t, t]) : [[st.time, st.time], ...TIMES.map((t) => [t, t])];
  const set = (key, resetGrant = true) => (v) => { st[key] = v; if (resetGrant) st.confirmGrant = false; update(); };
  return [
    field("When (trigger)", selectEl("Trigger", TRIGGERS, st.triggerKind, isDemo, set("triggerKind"))),
    st.triggerKind === "schedule"
      ? field("Time (local)", selectEl("Schedule time", timeOpts, st.time, isDemo, set("time")))
      : field("Condition", selectEl("Refresh condition", [
          ["any", "Every full refresh"], ["now", "New item in Do now"], ["kev", "Newly exploited item"], ["failed", "Any update failed"],
        ], st.refreshCond, isDemo, set("refreshCond"))),
    st.triggerKind === "schedule"
      ? field("Days", selectEl("Schedule days", CADENCES.map(([k, l]) => [k, l]), st.cadence, isDemo, set("cadence")))
      : null,
    field("Do (action)", selectEl("Action", ACTIONS, st.action, isDemo, (v) => {
      st.action = v;
      if (SYSTEM_ACTIONS.has(v)) st.mode = "ask";
      else if (v === "notify") st.mode = "run";
      st.confirmGrant = false;
      update();
    })),
    field("Approval", selectEl("Approval mode", modeOptions(st.action), st.mode, isDemo, set("mode"))),
    PROJECT_ACTIONS.has(st.action) ? field("Project", selectEl("Project", projOpts, st.project, isDemo, set("project"))) : null,
    (st.action === "start_fix" || st.action === "explain_item")
      ? field("Lane", selectEl("Lane", LANES, st.lane, isDemo, set("lane"))) : null,
    st.action === "refresh" ? field("Collectors", selectEl("Collectors", COLLECTORS, st.collector, isDemo, set("collector"))) : null,
    SYSTEM_ACTIONS.has(st.action) ? field("App", selectEl("App", APPS, st.app, isDemo, set("app"))) : null,
    field("Max per day", selectEl("Max per day", MAX_PER_DAY.map((n) => [n, `${n} / day`]), st.maxPerDay, isDemo, (v) => {
      st.maxPerDay = Number(v); st.confirmGrant = false; update();
    })),
    field("Cooldown", selectEl("Cooldown", COOLDOWNS, st.cooldownS, isDemo, (v) => {
      st.cooldownS = Number(v); st.confirmGrant = false; update();
    })),
    field("Wait before running", selectEl("Wait before running", WAIT_OPTS, st.waitS, isDemo, (v) => {
      st.waitS = Number(v); st.confirmGrant = false; update();
    })),
  ].filter(Boolean);
}

function buildGrantNodes(st, projects, isDemo, onUpdate) {
  const needsGrant = INSTALL_ACTIONS.has(st.action) && st.mode === "run" && st.enabled;
  return [
    el("div", { text: scopeSentence(st, projects || []) }),
    el("label", { class: "auto-grant-check" },
      el("input", {
        type: "checkbox", checked: st.enabled || null, disabled: isDemo || null, "aria-label": "Turn on after saving",
        onchange: (e) => { st.enabled = !!e.target.checked; onUpdate(); },
      }),
      el("span", { text: "Turn on after saving" })),
    needsGrant ? el("label", { class: "auto-grant-check" },
      el("input", {
        type: "checkbox", checked: st.confirmGrant || null, disabled: isDemo || null, "aria-label": "Confirm scope grant",
        onchange: (e) => { st.confirmGrant = !!e.target.checked; onUpdate(); },
      }),
      el("span", { text: "I approve Bosun to run this exact scope on its own (renews every 90 days)." })) : null,
  ].filter(Boolean);
}

function editorPanel(rule, projects, isDemo, onCancel, onSaved, existingState) {
  const st = existingState || initState(rule);
  const host = el("section", { class: "panel auto-editor" });
  const gridHost = el("div", { class: "auto-grid" });
  const grantHost = el("div", { class: "auto-grant" });
  const actionsHost = el("div", { class: "auto-editor-actions" });

  function renderDependent() {
    const needsGrant = INSTALL_ACTIONS.has(st.action) && st.mode === "run" && st.enabled;
    const missingProject = PROJECT_ACTIONS.has(st.action) && !st.project;
    const saveDisabled = isDemo || st.saving || !st.name.trim() || missingProject || (needsGrant && !st.confirmGrant);
    grantHost.replaceChildren(...buildGrantNodes(st, projects, isDemo, renderDependent));
    const saveBtn = el("button", {
      class: "primary",
      disabled: saveDisabled || null,
      text: st.saving ? "Saving…" : "Save",
      onclick: async () => {
        st.saving = true;
        renderDependent();
        try {
          await api("/api/automations", { method: "POST", body: JSON.stringify(buildPayload(st)) });
          if (onSaved) onSaved();
        } catch (err) {
          st.saving = false;
          alert(`Could not save automation: ${err.message}`);
          renderDependent();
        }
      },
    });
    actionsHost.replaceChildren(
      el("button", { class: "quiet", text: "Cancel", onclick: () => onCancel && onCancel() }),
      saveBtn);
  }

  const nameField = field("Name", el("input", {
    type: "text", value: st.name, maxlength: "120", disabled: isDemo || null,
    "aria-label": "Automation name", "data-fk": "auto-name",
    oninput: (e) => { st.name = e.target.value; renderDependent(); },
  }));

  function renderControls() {
    gridHost.replaceChildren(nameField, ...buildDropdownControls(st, projects, isDemo, renderControls));
    renderDependent();
  }

  host.replaceChildren(
    el("h2", { text: st.id ? `Edit automation: ${rule.name}` : "New automation" }),
    gridHost,
    grantHost,
    actionsHost);
  renderControls();
  return host;
}

if (MC.automationsHelpers) {
  MC.automationsHelpers.editorPanel = editorPanel;
  MC.automationsHelpers.initEditorState = initState;
}
})();
