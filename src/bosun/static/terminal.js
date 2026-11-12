// Bosun's terminal: one manual update, run here. The server builds the command (recipes.py) from an item and a
// recipe name; this page never names a command. It shows the output as it comes and passes on what you type:
// a line at a time, to that one running command. When the command asks for a password the terminal stops echoing,
// the server says so, and the field hides what you type. Nothing typed is kept here or there.
//
// The screen is a small renderer of our own, not a terminal library: installers need carriage returns (progress
// bars), "erase line", cursor up/down (Homebrew redraws several lines), and the 16 colours. That is ~150 lines,
// against several hundred kilobytes of vendored code in a tool whose job is to flag outdated dependencies.
// Output is text from programs on this machine: textContent only, as everywhere.
"use strict";
(() => {
const { el, api } = MC;
const $ = (s) => document.querySelector(s);
const MAX_LINES = 2000;

// ---------- the screen ----------
// Lines of cells; each cell is a character and a style ("f1" red, "b" bold...). The cursor moves as the output says.
function Screen(box) {
  const lines = [];
  let row = 0, col = 0, style = "", pending = "", frame = 0;
  const dirty = new Set();
  const line = (r) => { while (lines.length <= r) lines.push({ cells: [], node: null }); return lines[r]; };
  const touch = (r) => { dirty.add(line(r)); if (!frame) frame = requestAnimationFrame(paint); };

  function paint() {
    frame = 0;
    const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    for (const ln of lines) if (!ln.node) { ln.node = el("div", { class: "tl" }); box.append(ln.node); dirty.add(ln); }
    for (const ln of dirty) {
      const spans = [];
      let run = "", cls = null;
      for (const cell of ln.cells) {
        if (cell.s !== cls) { if (run) spans.push(el("span", { class: cls || null, text: run })); run = ""; cls = cell.s; }
        run += cell.c;
      }
      if (run) spans.push(el("span", { class: cls || null, text: run }));
      ln.node.replaceChildren(...spans);
    }
    dirty.clear();
    if (stick) box.scrollTop = box.scrollHeight;
  }

  function put(ch) {
    const ln = line(row);
    while (ln.cells.length < col) ln.cells.push({ c: " ", s: "" });
    ln.cells[col] = { c: ch, s: style };
    col += 1; touch(row);
  }

  function sgr(params) {
    const ps = params === "" ? [0] : params.split(";").map((p) => parseInt(p, 10) || 0);
    let fg = (style.match(/f(\d+)/) || [])[1] ?? null, bold = style.includes("b"), dim = style.includes("d");
    for (let i = 0; i < ps.length; i++) {
      const p = ps[i];
      if (p === 0) { fg = null; bold = dim = false; }
      else if (p === 1) bold = true;
      else if (p === 2) dim = true;
      else if (p === 22) bold = dim = false;
      else if (p >= 30 && p <= 37) fg = p - 30;
      else if (p >= 90 && p <= 97) fg = p - 90 + 8;
      else if (p === 39) fg = null;
      else if (p === 38 || p === 48) i += ps[i + 1] === 5 ? 2 : ps[i + 1] === 2 ? 4 : 0;  // 256 and true colour: skipped
    }
    style = [fg != null ? `f${fg}` : "", bold ? "b" : "", dim ? "d" : ""].filter(Boolean).join(" ");
  }

  function csi(params, final) {
    // a command's output is not trusted: "move the cursor a billion cells" must not allocate a billion
    const n = Math.min(parseInt(params, 10) || 0, 500), ln = line(row);
    if (final === "m") sgr(params);
    else if (final === "A") row = Math.max(0, row - Math.max(n, 1));
    else if (final === "B") { row = Math.min(row + Math.max(n, 1), 20000); line(row); }
    else if (final === "C") col = Math.min(col + Math.max(n, 1), 2000);
    else if (final === "D") col = Math.max(0, col - Math.max(n, 1));
    else if (final === "G") col = Math.min(Math.max(0, n - 1), 2000);
    else if (final === "K") {
      if (n === 0) ln.cells.length = Math.min(ln.cells.length, col);
      else if (n === 1) for (let i = 0; i <= col && i < ln.cells.length; i++) ln.cells[i] = { c: " ", s: "" };
      else ln.cells.length = 0;
      touch(row);
    } else if (final === "J" && (n === 0 || n === 2 || n === 3)) {
      ln.cells.length = n === 0 ? Math.min(ln.cells.length, col) : 0; touch(row);
      for (const gone of lines.splice(row + 1)) { if (gone.node) gone.node.remove(); dirty.delete(gone); }
    }
  }

  this.write = function write(text) {
    text = pending + text; pending = "";
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (ch === "\x1b") {
        const rest = text.slice(i);
        const m = /^\x1b\[([0-9;?]*)([@-~])/.exec(rest) || /^\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/.exec(rest) || /^\x1b[@-Z\\^_]/.exec(rest);
        if (!m) {  // a sequence cut in two by the stream: wait for the rest (but not for ever)
          if (rest.length < 64) { pending = rest; return; }
          continue;
        }
        if (m[2]) csi(m[1], m[2]);
        i += m[0].length - 1;
      } else if (ch === "\n") { row += 1; col = 0; touch(row); }
      else if (ch === "\r") col = 0;
      else if (ch === "\b") col = Math.max(0, col - 1);
      else if (ch === "\t") col = (Math.floor(col / 8) + 1) * 8;
      else if (ch >= " ") put(ch);
      if (lines.length > MAX_LINES) {
        for (const gone of lines.splice(0, lines.length - MAX_LINES)) { if (gone.node) gone.node.remove(); dirty.delete(gone); row -= 1; }
        row = Math.max(0, row);
      }
    }
  };
  this.text = () => lines.map((ln) => ln.cells.map((c) => c.c).join("").trimEnd()).join("\n");
}
MC.TermScreen = Screen;  // for tests of the renderer

// ---------- one run ----------
const T = { job: null, key: null, name: "", es: null, screen: null, running: false, hidden: false, done: null };
const panel = () => $("#terminal");
const STATUS = { queued: "Waiting its turn…", running: "Running", succeeded: "Finished", failed: "Failed", cancelled: "Stopped",
                 abandoned: "Interrupted", unknown: "Gone" };

function closeStream() { if (T.es) { T.es.close(); T.es = null; } }

function setState() {
  const p = panel(); if (!p || p.hidden) return;
  const input = p.querySelector(".termin"), note = p.querySelector(".termnote");
  const status = T.done ? T.done.status : T.running ? "running" : "queued";
  const chip = p.querySelector(".termstatus");
  chip.textContent = STATUS[status] || status;
  chip.className = `chip termstatus ${status === "failed" ? "job-failed" : status === "succeeded" ? "job-succeeded" : "job-running"}`;
  p.querySelector(".termrow").hidden = !!T.done;
  input.disabled = !T.running;
  input.classList.toggle("secret", T.hidden);
  input.placeholder = T.hidden ? "Password (hidden as you type)" : T.running ? "Type an answer, then Enter" : "";
  note.textContent = T.done ? "" : !T.running ? "It starts when the update ahead of it has finished."
    : T.hidden ? "The command is asking for a password. What you type goes straight to it and is not shown, logged or kept by Bosun."
    : "What you type goes to this command only. There is no shell here: Bosun built this command, and nothing else can be run.";
  if (T.running && document.activeElement !== input && p.contains(document.activeElement)) input.focus();
}

async function send() {
  const input = panel().querySelector(".termin");
  const data = `${input.value}\n`;
  input.value = "";
  if (!T.running) return;
  try { await api(`/api/terminal/${T.job}/input`, { method: "POST", body: JSON.stringify({ data }) }); }
  catch (e) { panel().querySelector(".termnote").textContent = e.message; }
}

async function stop() {
  if (!T.job || T.done) return true;
  if (T.running && !confirm("Stop this command?\n\nIf it is in the middle of installing, the app can be left half-installed. Stop only if it is stuck or you started it by mistake.")) return false;
  try { await api(`/api/terminal/${T.job}/stop`, { method: "POST" }); } catch { /* it had just finished */ }
  return true;
}

// How it ended, what Bosun makes of it, and what one click can do next.
async function finished(d) {
  closeStream();
  T.done = d; T.running = false; T.hidden = false;
  const p = panel(), foot = p.querySelector(".termdone");
  if (d.ended === "gone" && d.output) T.screen.write(d.output.replace(/\n/g, "\r\n"));  // re-opened later: what was kept
  const said = d.status === "succeeded" ? "Done. Bosun is checking it again now."
    : d.status === "cancelled" ? (d.note || "Stopped.")
    : d.status === "failed" ? (d.note || `It ended with an error${d.returncode != null ? ` (exit ${d.returncode})` : ""}.`)
    : d.status === "unknown" ? "Bosun has no record of this run any more." : (d.note || "");
  const acts = el("div", { class: "actions" });
  foot.replaceChildren(...[el("p", { class: d.status === "failed" ? "termresult bad" : "termresult", text: said }),
    d.hint ? el("div", { class: "callout crit", text: d.hint }) : null, acts].filter(Boolean));
  foot.hidden = false;
  setState();
  if (d.status === "failed" && T.key) {
    acts.append(el("span", { class: "hint termwait", text: "Looking for a fix…" }));
    let found = { recipes: [] };
    try { found = await api(`/api/recipes?key=${encodeURIComponent(T.key)}`); } catch { /* the item may be gone */ }
    acts.querySelector(".termwait")?.remove();
    for (const r of found.recipes) acts.append(recipeButton(r, T.name, r.follow_up ? "primary" : "quiet"));
  }
  if (d.status === "failed" && d.trace_id) acts.append(el("button", { class: "quiet", text: "Why did this fail?", title: "Ask Bosun: it reads this run and explains",
    onclick: () => MC.ask("Why did this fail?", { run: d.trace_id, ...(T.key ? { item: T.key } : {}) }) }));
  if (d.trace_id) acts.append(el("a", { class: "tracelink", href: MC.runHref(d.trace_id), text: "Every step of this run", onclick: () => MC.termClose(true) }));
  acts.append(el("button", { class: d.status === "succeeded" ? "primary" : "quiet", text: "Close", onclick: () => MC.termClose(true) }));
  if (MC.refreshNow) MC.refreshNow().catch(() => {});
}

function listen(job) {
  closeStream();
  const es = T.es = new EventSource(`/api/terminal/${job}/events`);
  const on = (kind, fn) => es.addEventListener(kind, (e) => { if (T.es === es) fn(JSON.parse(e.data)); });
  on("state", (d) => {
    T.running = d.status === "running"; T.hidden = !!d.hidden;
    if (d.command) panel().querySelector(".termcmd").textContent = `$ ${d.command}`;
    setState();
  });
  on("out", (d) => T.screen.write(d.text));
  on("done", finished);
  es.onerror = () => { if (es.readyState === EventSource.CLOSED && T.es === es) { T.es = null; } };
}

function render(name) {
  const p = panel();
  const box = el("div", { class: "term", role: "log", "aria-label": "Command output", tabindex: "0" });
  const input = el("input", { type: "text", class: "termin", autocomplete: "off", autocapitalize: "off", autocorrect: "off",
    spellcheck: "false", "aria-label": "Type an answer for the command", disabled: true,
    onkeydown: (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); send(); } } });
  p.replaceChildren(
    el("div", { class: "termhead" }, el("h2", { text: name ? `Updating ${name}` : "Running here" }), el("span", { class: "chip termstatus" }),
      el("span", { class: "spacer" }), el("button", { class: "link", text: "Close ✕", onclick: () => MC.termClose() })),
    el("p", { class: "termcmd", text: "" }), box,
    el("div", { class: "termfoot" },
      el("div", { class: "termrow" }, input, el("button", { class: "primary", text: "Send", onclick: send }),
        el("button", { class: "quiet", text: "Stop", onclick: stop })),
      el("p", { class: "hint termnote" }),
      el("div", { class: "termdone", hidden: true })));
  T.screen = new Screen(box);
}

// Show a run: one that was just started, or one still going after the page was reloaded.
MC.termOpen = function termOpen(job, key, name) {
  if (T.job === job && !panel().hidden) return;
  Object.assign(T, { job, key, name: name || "", running: false, hidden: false, done: null });
  panel().hidden = false;
  render(T.name);
  setState();
  listen(job);
  panel().querySelector(".termin").focus();
};

// Closing ends a command that is still running (after asking): nobody would be there to answer it.
MC.termClose = async function termClose(sure) {
  if (!sure && !(await stop())) return;
  closeStream();
  panel().hidden = true; T.job = null;
};

// Start a recipe for an item. The request names the item and the recipe; the server builds the command.
MC.runRecipe = async function runRecipe(key, recipe, name) {
  if (T.job && !T.done && !panel().hidden) { MC.banner("Bosun's terminal is busy: finish or stop the command that is running first."); return; }
  let r;
  try { r = await api("/api/terminal/run", { method: "POST", body: JSON.stringify({ key, recipe }) }); }
  catch (e) { MC.banner(`Could not start it: ${e.message}`); return; }
  if (r.terminal) MC.termOpen(r.job, key, name);
  else MC.banner(`${name || "It"} is open. Bosun updates it by itself the moment you quit it.`, "info");
  if (MC.refreshNow) MC.refreshNow().catch(() => {});
};

// A suite's button: one recipe for several items, as one command. The server checks each item and builds it.
MC.runSuite = async function runSuite(keys, recipe, names) {
  if (T.job && !T.done && !panel().hidden) { MC.banner("Bosun's terminal is busy: finish or stop the command that is running first."); return; }
  if (!confirm(`Update ${names.length} apps?\n\n${names.join(", ")}\n\nOne command, here in Bosun's terminal. An app that is open is updated once you quit it.`)) return;
  let r;
  try { r = await api("/api/terminal/run-all", { method: "POST", body: JSON.stringify({ keys, recipe }) }); }
  catch (e) { MC.banner(`Could not start it: ${e.message}`); return; }
  if (r.terminal) MC.termOpen(r.terminal, keys[0], `${names.length - r.waiting.length} apps`);
  if (r.waiting.length) MC.banner(`${r.waiting.length} of them ${r.waiting.length === 1 ? "is" : "are"} open. Bosun updates ${r.waiting.length === 1 ? "it" : "each"} the moment you quit it.`, "info");
  if (MC.refreshNow) MC.refreshNow().catch(() => {});
};

// A recipe as a button. `r` is what the server's list_recipes said: a name, a label and what it does.
function recipeButton(r, name, cls) {
  return el("button", { class: cls || "primary", text: r.label, title: `${r.what}\n\n${r.command}`,
    onclick: (e) => { e.stopPropagation(); MC.runRecipe(r.key, r.recipe, name); } });
}
MC.recipeButton = recipeButton;

// The recipes for an item or a failed run, fetched as you (not taken from anything a model said), as buttons.
MC.recipeButtons = function recipeButtons(query) {
  const box = el("div", { class: "recipes" });
  const q = new URLSearchParams(Object.entries(query || {}).filter(([k, v]) => ["key", "trace_id"].includes(k) && typeof v === "string"));
  api(`/api/recipes?${q}`).then((found) => {
    if (!found.recipes.length) return;
    box.append(...found.recipes.map((r) => el("div", { class: "recipe" }, recipeButton(r, found.name, "primary"),
      el("span", { class: "hint", text: r.what }))));
  }, () => {});
  return box;
};

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !panel().hidden && panel().contains(document.activeElement)) { e.stopImmediatePropagation(); MC.termClose(); }
}, true);
})();
