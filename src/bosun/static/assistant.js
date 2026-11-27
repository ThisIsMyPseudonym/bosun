// Ask Bosun: the assistant's panel. A slide-over on every page; MC.ask(question, context) opens it from a button.
// The assistant reads Bosun's data on its own. Anything it wants to change arrives here as a card, and runs only if
// you press Approve: that click is a POST with your cookie and page token, like every other button.
// Everything shown (answers, tool input and output, item names) is text from a model or from this machine:
// textContent only, as everywhere.
"use strict";
(() => {
const { el, api, plural } = MC;
const $ = (s) => document.querySelector(s);
const A = { open: false, view: "chat", cid: null, running: false, es: null, list: null, live: null, nodes: new Map(),
            timers: new Set(), error: "" };
const BASE = "/api/assistant";
const usd = (n) => (n == null ? "" : n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`);
const TOOL_WORDS = { read: "Looked at", local: "Ran", install: "Ran", system: "Ran", external: "Ran" };
const CHIP = { approved: "Approved", used: "Approved", declined: "Declined", expired: "Not answered", stopped: "Stopped", void: "Not run" };
const OUTCOME = { approved: "You approved this", declined: "You declined this", expired: "Not answered in time: not run",
                  stopped: "Stopped: not run", pending: "Waiting for you", void: "Not run", used: "You approved this" };

function panel() { return $("#assistant"); }
function log() { return panel().querySelector(".asklog"); }

// ---------- pieces ----------
// An answer is plain text; a fenced block is something to copy (a prompt for Claude Code, a command).
function prose(text) {
  const parts = String(text || "").split(/```[a-zA-Z]*\n?/);
  return parts.map((part, i) => {
    if (i % 2 === 0) return part.trim() ? rich(part.trim()) : null;
    const copy = el("button", { class: "link", text: "Copy", onclick: () =>
      navigator.clipboard.writeText(part.trim()).then(() => { copy.textContent = "Copied"; }, () => { copy.textContent = "Select and copy"; }) });
    return el("div", { class: "askcode" }, el("pre", { text: part.trim() }), copy);
  });
}

// The model writes light Markdown whatever it is told. Show it as formatting: headings, lists, tables, bold,
// italics and `code`. Built node by node from text, never parsed as markup; a link shows as its words, not a link.
function inline(s) {
  const out = [], re = /`([^`\n]+)`|\*\*([^*\n]+)\*\*|__([^_\n]+)__|(?<![\w*])\*([^*\s][^*\n]*)\*(?![\w*])|\[([^\]\n]+)\]\([^)\s]+\)/g;
  let at = 0, m;
  while ((m = re.exec(s))) {
    if (m.index > at) out.push(s.slice(at, m.index));
    out.push(m[1] != null ? el("code", { text: m[1] }) : m[2] != null || m[3] != null ? el("strong", { text: m[2] ?? m[3] })
      : m[4] != null ? el("em", { text: m[4] }) : m[5]);
    at = m.index + m[0].length;
  }
  if (at < s.length) out.push(s.slice(at));
  return out;
}
function rich(text) {
  const box = el("div", { class: "askrich" }), lines = text.split("\n");
  const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  let para = [], list = null;
  const flush = () => { if (para.length) box.append(el("p", { class: "asktext" }, ...inline(para.join("\n")))); para = []; };
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i], h = /^\s{0,3}(#{1,6})\s+(.*)$/.exec(l), li = /^(\s*)([-*•]|\d+[.)])\s+(.*)$/.exec(l);
    if (!li) list = null;
    if (!l.trim()) { flush(); continue; }
    if (h) { flush(); box.append(el("h4", { class: "askh" }, ...inline(h[2].replace(/\s+#+\s*$/, "")))); continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)) { flush(); box.append(el("hr", {})); continue; }
    if (l.includes("|") && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1] || "")) {
      flush();
      const body = [];
      let j = i + 2;
      for (; j < lines.length && lines[j].includes("|") && lines[j].trim(); j++) body.push(cells(lines[j]));
      box.append(el("div", { class: "asktable" }, el("table", {},
        el("thead", {}, el("tr", {}, ...cells(l).map((c) => el("th", {}, ...inline(c))))),
        el("tbody", {}, ...body.map((r) => el("tr", {}, ...r.map((c) => el("td", {}, ...inline(c)))))))));
      i = j - 1; continue;
    }
    if (li) {
      flush();
      const ordered = /\d/.test(li[2]);
      if (!list || list.ordered !== ordered) { list = { ordered, node: el(ordered ? "ol" : "ul", { class: "asklistmd" }) }; box.append(list.node); }
      list.node.append(el("li", { class: li[1].length >= 2 ? "sub" : null }, ...inline(li[3])));
      continue;
    }
    if (/^\s*>\s?/.test(l)) { para.push(l.replace(/^\s*>\s?/, "")); continue; }
    para.push(l);
  }
  flush();
  return box;
}

function bubble(role, text) {
  return el("div", { class: `askmsg ${role}` }, ...(role === "user" ? [el("p", { class: "asktext", text })] : prose(text)));
}

function brief(input) {
  const s = Object.entries(input || {}).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ");
  return s.length > 90 ? `${s.slice(0, 90)}…` : s;
}

// One quiet line per tool call; open it for the exact input and what came back.
function toolLine(t) {
  const status = el("span", { class: "askstate", text: t.outcome && t.outcome !== "running" ? "" : "…" });
  const body = el("div", { class: "asktoolbody" }, el("pre", { text: JSON.stringify(t.input || {}, null, 2) }));
  const node = el("details", { class: "asktool" },
    el("summary", {}, el("span", { class: "askverb", text: `${TOOL_WORDS[t.risk] || "Ran"} ` }),
      el("span", { class: "askname", text: t.name.replace(/_/g, " ") }), el("span", { class: "askbrief", text: ` ${brief(t.input)}` }), status),
    body);
  node.finish = (r) => {
    const bad = r.outcome !== "ok";
    status.textContent = r.outcome === "ok" ? "" : r.outcome === "refused" ? " · refused" : " · failed";
    node.classList.toggle("bad", bad);
    if (r.result) body.append(el("pre", { class: "out", text: r.result }));
    if (r.trace_id) body.append(el("a", { class: "tracelink", href: MC.runHref(r.trace_id), text: "See this on the Runs page" }));
    if (r.trace_id) node.querySelector("summary").append(el("a", { class: "askrun", href: MC.runHref(r.trace_id), text: "run" }));
    // The assistant looked up what Bosun can run for an item. It can't start any of it: the buttons are yours.
    // They are built from the server's answer to this browser for the same item, not from what the model received.
    if (!bad && t.name === "list_recipes" && MC.recipeButtons) node.after(MC.recipeButtons(t.input));
  };
  // a finished call from the record: its buttons go in once the line is in the page
  if (t.outcome && t.outcome !== "running") queueMicrotask(() => node.finish({ outcome: t.outcome, result: t.text, trace_id: t.trace_id }));
  return node;
}

// What the assistant wants to do, exactly, and the only two buttons that decide it.
function approvalCard(a) {
  const settled = a.outcome && a.outcome !== "pending";
  const reason = el("input", { type: "text", class: "askreason", maxlength: "500", placeholder: "Why not? (optional, the assistant is told)", "aria-label": "Reason for declining" });
  const state = el("p", { class: "askstate", text: settled ? OUTCOME[a.outcome] || a.outcome : "" });
  const left = el("span", { class: "hint" });
  const chip = el("span", { class: "chip", text: settled ? CHIP[a.outcome] || "Asked" : "Needs your OK" });
  const answer = async (approve) => {
    yes.disabled = no.disabled = true;
    try { await api(`${BASE}/approvals/${encodeURIComponent(a.id)}`, { method: "POST", body: JSON.stringify({ approve, reason: approve ? "" : reason.value }) }); }
    catch (e) { state.textContent = e.message; node.settle(null); }
  };
  const yes = el("button", { class: "primary", text: "Approve", onclick: () => answer(true) });
  const no = el("button", { class: "quiet", text: "Decline", onclick: () => answer(false) });
  const controls = el("div", { class: "askdecide" }, reason, el("div", { class: "actions" }, yes, no, left));
  const items = (a.items || []).map((i) => el("li", {}, i.unknown ? el("span", { text: `${i.key} (Bosun doesn't know this item)` })
    : [el("strong", { text: i.name || i.key }), el("span", { text: ` ${i.installed || "?"}${i.available ? ` → ${i.available}` : ""}` })]));
  const node = el("div", { class: `askcard${settled ? " settled" : ""}`, role: "group", "aria-label": "Approval needed" },
    el("div", { class: "askcardhead" }, chip, el("strong", { text: (a.tool || "").replace(/_/g, " ") })),
    a.summary ? el("p", { text: `${a.summary}.` }) : null,
    a.tier ? el("p", { class: "hint", text: a.tier }) : null,
    items.length ? el("ul", { class: "askitems" }, ...items) : null,
    el("details", { open: !items.length }, el("summary", { text: "Exact input" }), el("pre", { text: JSON.stringify(a.input || {}, null, 2) })),
    settled ? null : controls, state);
  let timer = null;
  node.settle = (outcome, why) => {
    if (timer) { clearInterval(timer); A.timers.delete(timer); }
    controls.remove(); node.classList.add("settled");
    chip.textContent = CHIP[outcome] || "Not run";
    if (outcome) state.textContent = `${OUTCOME[outcome] || outcome}${why && outcome === "declined" ? `: ${why}` : ""}`;
  };
  if (!settled && a.expires) {
    const tick = () => {
      const s = Math.max(0, Math.round(a.expires - Date.now() / 1000));
      left.textContent = `Counts as declined in ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    };
    tick(); timer = setInterval(tick, 1000); A.timers.add(timer);
  }
  if (settled && a.text && a.outcome === "declined") state.textContent = `${OUTCOME.declined}: ${a.text}`;
  return node;
}

function notice(text, kind) { return el("p", { class: `asknote ${kind || ""}`, text }); }

function add(node) {
  const box = log();
  const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.append(node);
  if (stick) box.scrollTop = box.scrollHeight;
  return node;
}

// ---------- a turn, live ----------
const DONE = { stopped: "Stopped.", budget: "This answer reached today's budget for the assistant and was cut short.", error: "" };

function closeStream() {
  if (A.es) { A.es.close(); A.es = null; }
}

function listen(cid) {
  closeStream();
  const es = A.es = new EventSource(`${BASE}/conversations/${encodeURIComponent(cid)}/events`);
  const on = (kind, fn) => es.addEventListener(kind, (e) => { if (A.es === es) fn(JSON.parse(e.data)); });
  on("turn", (d) => { add(bubble("user", d.text)); setRunning(true); });
  on("text", (d) => {
    if (!A.live) { A.live = add(el("div", { class: "askmsg assistant live" })); A.liveText = ""; }
    A.liveText += d.delta;
    A.live.replaceChildren(...prose(A.liveText).filter(Boolean));  // formatted as it arrives
    const box = log(); if (box.scrollHeight - box.scrollTop - box.clientHeight < 120) box.scrollTop = box.scrollHeight;
  });
  on("message", (d) => {
    const done = bubble("assistant", d.text);
    if (A.live) { A.live.replaceWith(done); A.live = null; } else add(done);
  });
  on("tool", (d) => { A.live = null; A.nodes.set(`t${d.id}`, add(toolLine(d))); });
  on("tool_result", (d) => { const n = A.nodes.get(`t${d.id}`); if (n) n.finish(d); });
  // Focus stays where it is: a card that grabbed it could be approved by the Enter of someone still typing.
  on("approval", (d) => { A.live = null; A.nodes.set(`a${d.id}`, add(approvalCard(d))); });
  on("approval_done", (d) => { const n = A.nodes.get(`a${d.id}`); if (n) n.settle(d.outcome, d.reason); });
  on("done", (d) => {
    closeStream(); A.live = null;
    if (d.status === "idle") { show(cid); return; }  // nothing is running (Bosun restarted): show what was kept
    const said = d.status === "error" ? d.error || "Something went wrong." : DONE[d.status];
    if (said) add(notice(said, d.status === "error" ? "bad" : ""));
    setRunning(false);
    // an approved action may have changed what the page shows, and the turn used some of today's budget
    if (MC.refreshNow) MC.refreshNow().then(renderFoot, () => {});
  });
  es.onerror = () => { if (es.readyState === EventSource.CLOSED && A.es === es) { A.es = null; setRunning(false); } };
}

function setRunning(on) {
  A.running = on;
  const p = panel();
  p.querySelector(".asksend").hidden = on;
  p.querySelector(".askstop").hidden = !on;
  p.querySelector(".askbusy").hidden = !on;
}

async function send(text, context) {
  text = (text || "").trim();
  if (!text || A.running) return;
  A.error = ""; renderFoot();
  setRunning(true);
  try {
    const r = await api(`${BASE}/ask`, { method: "POST", body: JSON.stringify({
      text, conversation_id: A.cid, context: context || currentContext(), view: MC.SIMPLE ? "simple" : "developer" }) });
    A.cid = r.conversation_id;
    panel().querySelector("textarea").value = "";
    log().querySelector(".askempty")?.remove();
    listen(A.cid);
  } catch (e) { setRunning(false); A.error = e.message; renderFoot(); }
}

async function stop() {
  if (!A.cid) return;
  try { await api(`${BASE}/conversations/${A.cid}/stop`, { method: "POST" }); } catch { /* it had just finished */ }
}

// What you are looking at, as identifiers: the server looks them up itself and drops what it doesn't know.
function currentContext() {
  const r = MC.route.parse(), c = { page: r.page };
  if (r.page === "trace") { c.page = "runs"; c.run = r.id; }
  if (r.page === "scope" && r.id !== "pc") c.project = r.id;
  const item = MC.openItem && MC.openItem();
  if (item) c.item = item;
  return c;
}

// ---------- a conversation, from what was kept ----------
async function show(cid) {
  closeStream(); A.view = "chat"; A.cid = cid; A.live = null; A.nodes.clear();
  for (const t of A.timers) clearInterval(t);
  A.timers.clear();
  render();
  if (!cid) return;
  let c;
  try { c = await api(`${BASE}/conversations/${cid}`); }
  catch (e) { A.cid = null; A.error = e.message; render(); return; }
  if (A.cid !== cid) return;
  log().replaceChildren();
  // a turn that is still running is replayed from its stream, so that its approval card is live
  for (const m of c.messages.filter((x) => !c.running || x.turn < c.turns)) {
    if (m.role === "user" || m.role === "assistant") add(bubble(m.role, m.text));
    else if (m.role === "tool") add(toolLine({ id: m.id, name: m.tool, input: m.input, risk: m.risk, outcome: m.outcome, text: m.text, trace_id: m.trace_id }));
    else if (m.role === "approval") add(approvalCard({ id: m.approval_id, tool: m.tool, input: m.input, outcome: m.outcome === "pending" ? "expired" : m.outcome, text: m.text }));
    else if (m.role === "notice") add(notice(m.outcome === "error" ? m.text : DONE[m.outcome] || m.outcome, m.outcome === "error" ? "bad" : ""));
  }
  log().scrollTop = log().scrollHeight;
  if (c.running) listen(cid);
}

async function showList() {
  A.view = "list"; A.list = null; render();
  try { A.list = await api(`${BASE}/conversations`); } catch (e) { A.list = []; A.error = e.message; }
  if (A.view === "list") render();
}

async function remove(c) {
  if (!confirm(`Delete this conversation?\n\n“${c.title}”\n\nIts messages are removed from this computer. What its actions did stays on the Runs page.`)) return;
  try { await api(`${BASE}/conversations/${c.id}/delete`, { method: "POST" }); if (A.cid === c.id) A.cid = null; }
  catch (e) { A.error = e.message; }
  showList();
}

// ---------- the panel ----------
const STARTERS = ["What should I do first?", "What failed lately, and why?", "What changed since the last check?"];

function renderFoot() {
  const p = panel(); if (!p) return;
  const info = MC.assistantInfo || {};
  const who = info.login ? ` Using ${info.login}.` : "";
  p.querySelector(".askhint").textContent = A.error || `It reads Bosun's own data. Any change waits for your Approve.${who}`;
  p.querySelector(".askhint").classList.toggle("bad", !!A.error);
}

function render() {
  const p = panel();
  const head = el("div", { class: "askhead" }, el("h2", { text: "Ask Bosun" }), el("span", { class: "spacer" }),
    el("button", { class: "link", text: "New", title: "Start a new conversation", onclick: () => { show(null); focusInput(); } }),
    el("button", { class: "link", text: A.view === "list" ? "Back" : "Earlier", title: "Earlier conversations",
      onclick: () => (A.view === "list" ? show(A.cid) : showList()) }),
    el("button", { class: "link", text: "Close ✕", onclick: () => MC.askClose() }));
  if (A.view === "list") {
    const rows = A.list == null ? [el("p", { class: "empty", text: "Loading…" })]
      : !A.list.length ? [el("p", { class: "empty", text: "No conversations yet." })]
      : A.list.map((c) => el("div", { class: "askrow" },
          el("button", { class: "askopen", onclick: () => show(c.id) }, el("strong", { text: c.title || "Conversation" }),
            el("span", { class: "hint", text: `${MC.ago(c.updated)} · ${plural(c.turns, "question")}${c.cost_usd ? ` · ${usd(c.cost_usd)}` : ""}${c.running ? " · answering…" : ""}` })),
          el("button", { class: "link", text: "Delete", "aria-label": `Delete conversation: ${c.title}`, onclick: () => remove(c) })));
    p.replaceChildren(head, el("div", { class: "asklist" },
      el("p", { class: "hint", text: "Kept on this computer until you delete them. Deleting removes the messages; what was done stays on the Runs page." }), ...rows));
    return;
  }
  const input = el("textarea", { rows: "2", maxlength: "8000", placeholder: "Ask about this computer…", "aria-label": "Your question",
    onkeydown: (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(input.value); } } });
  const empty = el("div", { class: "askempty" },
    el("p", { class: "hint", text: MC.words("Ask about what's outdated, what failed and why, or what Claude costs. It answers from Bosun's own records.",
      "Ask what needs doing on this computer and why. It answers from what Bosun has checked.") }),
    ...STARTERS.map((q) => el("button", { class: "quiet", text: q, onclick: () => send(q) })));
  p.replaceChildren(head,
    el("div", { class: "asklog", role: "log", "aria-live": "polite", "aria-label": "Conversation" }, A.cid ? null : empty),
    el("div", { class: "askfoot" },
      el("div", { class: "askbusy", hidden: true }, el("span", { class: "spinner", "aria-hidden": "true" }), el("span", { text: "Working…" })),
      el("div", { class: "askrowin" }, input,
        el("button", { class: "primary asksend", text: "Send", onclick: () => send(input.value) }),
        el("button", { class: "quiet askstop", text: "Stop", hidden: true, onclick: stop })),
      el("p", { class: "hint askhint" })));
  renderFoot();
  setRunning(A.running && !!A.es);
}

function focusInput() { panel().querySelector("textarea")?.focus(); }

MC.askOpen = function askOpen() {
  if (!A.open) { A.open = true; panel().hidden = false; $("#ask").setAttribute("aria-expanded", "true"); render(); if (A.cid) show(A.cid); }
  focusInput();
};
MC.askClose = function askClose() {
  A.open = false; panel().hidden = true; $("#ask").setAttribute("aria-expanded", "false"); $("#ask").focus();
  // a running turn keeps running on the server; opening the panel again picks its stream back up
  closeStream();
};
// A page's own button: a new conversation about what that button is on, asked at once.
MC.ask = function ask(question, context) {
  if (A.running) { MC.askOpen(); return; }
  A.open = true; panel().hidden = false; $("#ask").setAttribute("aria-expanded", "true");
  show(null);
  send(question, { ...currentContext(), ...(context || {}) });
  focusInput();
};

$("#ask").replaceChildren(MC.icon("ask"));
$("#ask").addEventListener("click", () => (A.open ? MC.askClose() : MC.askOpen()));
// Escape closes the panel first; the details drawer underneath keeps its own Escape for the next press
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && A.open) { e.stopImmediatePropagation(); MC.askClose(); }
}, true);
})();
