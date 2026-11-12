// Shared helpers. All untrusted text (package names, release notes, model output) is rendered
// with textContent and safe links only - never innerHTML.
"use strict";
window.MC = window.MC || {};
MC.views = {};

MC.TOKEN = document.querySelector('meta[name="bosun-token"]').content;

MC.LANES = {
  now:     { name: "Do now",  why: "High risk, and the fix is drop-in (no migration work).", sub: "ready" },
  plan:    { name: "Plan",    why: "High risk, but the fix needs migration work. Branch, test, then merge.", sub: "needs work" },
  watch:   { name: "Watch",   why: "Vulnerable, but no fixed version has been published yet.", sub: "no fix yet" },
  batch:   { name: "Batch",   why: "Lower risk and drop-in. Hand them to your agent in one routine pass.", sub: "ready" },
  later:   { name: "Later",   why: "Lower risk and needs migration work. Optional.", sub: "needs work" },
  dormant: { name: "Dormant", why: "Projects with no commits in 6+ months. Archive or ignore them instead of updating.", sub: "" },
};

MC.el = function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k === "style") Object.assign(n.style, v); // CSSOM: allowed under our CSP, unlike style=""
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) n.append(c);
  return n;
};

// A section that stays the way you left it. The page re-renders every few seconds while updates run, and a fresh
// <details> would snap back to its default: keep each one's state (keyed by page and section) for this visit.
const kept = new Map();
MC.details = function details(key, defaultOpen, attrs, ...kids) {
  if (!kept.has(key)) kept.set(key, !!defaultOpen);  // first look decides; later changes in counts don't fold it away
  const d = MC.el("details", { ...attrs, open: kept.get(key) }, ...kids);
  d.addEventListener("toggle", () => { if (d.isConnected) kept.set(key, d.open); });
  return d;
};

// The same memory for a section that is not a <details> (a group whose header also holds a button).
// These are remembered by the server too (the list_prefs setting), so the Mac window and a browser tab agree.
const unsaved = {};
let saveTimer = null;
MC.fold = { open: (key, dflt) => { if (!kept.has(key)) kept.set(key, !!dflt); return kept.get(key); },
            set: (key, open) => {
              kept.set(key, !!open); unsaved[key] = !!open;
              clearTimeout(saveTimer);
              saveTimer = setTimeout(() => {
                const folds = { ...unsaved }; for (const k of Object.keys(unsaved)) delete unsaved[k];
                MC.api("/api/list-prefs", { method: "POST", body: JSON.stringify({ folds }) }).catch(() => {});
              }, 600);
            },
            // what the server remembers, for sections this page has not touched since it loaded
            seed: (folds) => { for (const [k, v] of Object.entries(folds || {})) if (!kept.has(k)) kept.set(k, !!v); } };

// The page's token belongs to the dashboard process that served the page. When Bosun restarts (an update, a
// crash) a window left open still holds the old one, and every action would be refused with nothing to click.
// So on that refusal the page reads its own address again for the new token and tries once more. Only this
// page can read that answer (same origin, signed-in cookie), which is what the token is there to prove.
MC.freshToken = async function freshToken() {
  try {
    const res = await fetch("/", { headers: { accept: "text/html" } });
    if (!res.ok) return false;
    const found = /<meta name="bosun-token" content="([^"]+)">/.exec(await res.text());
    if (!found || found[1] === MC.TOKEN) return false;
    MC.TOKEN = found[1];
    return true;
  } catch (e) { return false; }
};

MC.api = async function api(path, opts = {}, again = true) {
  const res = await fetch(path, { ...opts,
    headers: { "content-type": "application/json", "x-bosun-token": MC.TOKEN, ...(opts.headers || {}) } });
  const body = await res.json().catch(() => ({}));
  if (res.status === 403 && body.error === "missing or invalid token") {
    if (again && await MC.freshToken()) return MC.api(path, opts, false);
    // could not catch up by itself: from here the banner says so, with the one click that fixes it (app.js)
    MC.stale = true;
    if (MC.banner) MC.banner("");
    throw new Error("this window is out of sync with Bosun; reload it and try again");
  }
  if (!res.ok) {
    const d = body.detail || body.error;  // a string, or a validator's list of { loc, msg }
    const said = Array.isArray(d) ? d.map((x) => (x && x.msg ? `${(x.loc || []).slice(1).join(".")}: ${x.msg}`.replace(/^: /, "") : String(x))).join("; ")
      : d && typeof d === "object" ? JSON.stringify(d) : d;
    throw new Error(said || `HTTP ${res.status}`);
  }
  return body;
};

MC.plural = (n, w, ws) => `${n} ${n === 1 ? w : (ws || w + "s")}`;

// Simple view: for a computer with no code projects (or when chosen). Plain words, no developer screens.
MC.SIMPLE = false;
MC.words = (dev, simple) => (MC.SIMPLE ? simple : dev);

MC.sev = function sev(r) {
  const { el } = MC;
  if (MC.SIMPLE) {  // when to act, not how a CVE scores
    if (r >= 10) return el("span", { class: "sev crit" }, el("i", { text: "▲" }), "Under attack");
    if (r >= 7) return el("span", { class: "sev serious" }, el("i", { text: "◆" }), "Urgent");
    if (r >= 4) return el("span", { class: "sev warn" }, el("i", { text: "●" }), "Soon");
    return el("span", { class: "sev low" }, el("i", { text: "○" }), "Routine");
  }
  if (r >= 10) return el("span", { class: "sev crit" }, el("i", { text: "▲" }), "Exploited");
  if (r >= 9) return el("span", { class: "sev crit" }, el("i", { text: "▲" }), "Critical");
  if (r >= 7) return el("span", { class: "sev serious" }, el("i", { text: "◆" }), "High");
  if (r >= 4) return el("span", { class: "sev warn" }, el("i", { text: "●" }), "Medium");
  return el("span", { class: "sev low" }, el("i", { text: "○" }), r > 0 ? "Low" : "No CVEs");
};

// "No known vulnerabilities" is only true if someone looked. An item no source could look up says so.
MC.cveCheckText = function cveCheckText(d) {
  if (d.cve_check === "unchecked") {
    return MC.words("Not checked for vulnerabilities: none of Bosun's sources (OSV.dev, NVD, Homebrew) knows this one. Keeping it up to date is the protection.",
      "Bosun can't check this one for known security problems. Keeping it up to date is the protection.");
  }
  const by = (d.checked_by || []).join(", ");
  return by ? `No known vulnerabilities (checked: ${by}).` : "No known vulnerabilities.";
};

// CVE ids the release notes name that Bosun did not count: the CVE record did not confirm that the version
// installed here is affected (another product, an old CVE of this one, no versions in the record, or a record
// that couldn't be read). Shown, so nothing is hidden.
MC.notedCves = function notedCves(d) {
  const have = new Set([...(d.vulns_fixed || []), ...(d.vulns_remaining || [])].flatMap((v) => [v.id, ...(v.aliases || [])]));
  const rest = (d.notes_cves || []).filter((c) => !have.has(c));
  if (!rest.length || MC.SIMPLE) return null;
  const links = rest.slice(0, 8).flatMap((c, i) => [i ? ", " : "", MC.safeLink(`https://www.cve.org/CVERecord?id=${encodeURIComponent(c)}`, c)]);
  return MC.el("p", { class: "hint" }, "Also named in the release notes, and not counted: ",
    ...links, rest.length > 8 ? ` and ${rest.length - 8} more` : "", ". The CVE record does not confirm that the version installed here is affected, so these do not raise this item's risk.");
};

MC.ago = function ago(ts) {
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
};

// a vendor's download page in a new tab (http/https only)
MC.openExternal = function openExternal(url) {
  try {
    const u = new URL(url);
    if (u.protocol === "https:" || u.protocol === "http:") window.open(u.href, "_blank", "noopener,noreferrer");
  } catch { /* not a URL: nothing to open */ }
};

MC.safeLink = function safeLink(url, label) {
  try {
    const u = new URL(url);
    if (u.protocol !== "https:" && u.protocol !== "http:") throw new Error("scheme");
    return MC.el("a", { href: u.href, target: "_blank", rel: "noopener noreferrer", text: label || u.hostname });
  } catch { return url ? MC.el("span", { text: label || url }) : null; }
};

MC.ENV = { winget: "Desktop apps", choco: "Desktop apps", "npm-global": "Toolchains & CLIs",
           "pip-global": "Toolchains & CLIs", "wsl-apt": "WSL Ubuntu", repo: "Projects",
           container: "Projects", image: "Containers",
           brew: "Homebrew", "brew-cask": "Mac apps", "mac-app": "Mac apps", macos: "macOS", runtime: "Runtimes",
           apt: "System packages", snap: "Snaps", distro: "Operating system", "windows-update": "Windows Update",
           device: "Hardware" };
// which machine this dashboard runs on (set from /api/state)
MC.MACHINE = { kind: "pc", label: "This PC", updates: "Windows Update" };

// ---------- what kind of thing it is (kinds.py), and the groups a machine page shows ----------
MC.KIND = { os: "Operating system", firmware: "Firmware", driver: "Driver", hardware: "Hardware tool", browser: "Browser",
            app: "App", component: "Shared component", dev: "Developer tool", project: "Project" };
// how it arrived, where that adds something to the kind
MC.VIA = { winget: "winget", choco: "Chocolatey", "npm-global": "npm", "pip-global": "pip", "wsl-apt": "WSL",
           image: "Docker", brew: "Homebrew", "brew-cask": "Homebrew", apt: "apt", snap: "snap", "windows-update": "Windows Update" };
// snapshots and other machines from before kinds existed: a best guess from the source
const KIND_OF_SOURCE = { mcp: "dev", secret: "project", "npm-global": "dev", "pip-global": "dev", "wsl-apt": "dev", image: "dev", brew: "dev", runtime: "dev",
                         macos: "os", distro: "os", "windows-update": "os", repo: "project", container: "project" };
MC.kindOf = (i) => i.kind || KIND_OF_SOURCE[i.source] || "app";
MC.GROUPS = [
  { id: "system", kinds: ["os", "firmware", "driver", "hardware"],
    name: (m) => (m === "mac" ? "This Mac itself" : m === "linux" ? "This computer itself" : "This PC itself"),
    why: (u, m) => m === "linux"
      ? "The operating system, restarts and firmware. Bosun installs the system packages after your password; firmware and the release upgrade are yours to run."
      : `The operating system, restarts, drivers, firmware and hardware tools. ${u} installs the system updates; Bosun shows what's waiting and never updates drivers or hardware tools on its own.` },
  { id: "apps", kinds: ["browser", "app"], name: () => "Apps",
    why: () => "Programs you use. Bosun updates most of them in one click; browsers finish with a restart." },
  { id: "components", kinds: ["component"], name: () => "Behind the scenes",
    why: (u, m) => `Shared pieces other programs need (${m === "pc" ? "Visual C++, .NET, WebView2, Java" : m === "mac" ? "frameworks, Java" : "system libraries, Java"}). Safe to keep current.` },
  { id: "dev", kinds: ["dev"], name: () => "Developer tools",
    why: () => "Toolchains, CLIs, runtimes, package managers, editors and containers." },
];
// findings that aren't versions (a committed secret, an MCP server's config) say what's wrong instead
MC.isFinding = (i) => i.source === "secret" || i.source === "mcp";
MC.groupOf = (i) => MC.GROUPS.find((g) => g.kinds.includes(MC.kindOf(i))) || MC.GROUPS[1];

// One row per package across projects/places; keeps the worst risk and every place.
MC.groupByPackage = function groupByPackage(items) {
  const g = new Map();
  for (const i of items) {
    const k = i.project ? `${i.source}|${i.ecosystem}|${i.package_id}` : `${i.source}|${i.package_id}`;
    let cur = g.get(k);
    if (!cur) { cur = { ...i, members: [], places: [] }; g.set(k, cur); }
    cur.members.push(i);
    cur.places.push(i.project ? i.project_name : MC.ENV[i.source]);
    if (i.risk > cur.risk) Object.assign(cur, { risk: i.risk, headline: i.headline || cur.headline });
    cur.kev ||= i.kev; cur.n_fixed = Math.max(cur.n_fixed, i.n_fixed);
    if (i.effort === "high") cur.effort = "high";
    const order = ["remote", "remote_auth", "adjacent", "needs_action", "local", "physical", "unknown", ""];
    if (order.indexOf(i.reach || "") < order.indexOf(cur.reach || "")) cur.reach = i.reach;
  }
  for (const c of g.values()) c.places = [...new Set(c.places)];
  return [...g.values()].sort((a, b) => b.risk - a.risk || b.places.length - a.places.length || a.name.localeCompare(b.name));
};

MC.FIX_LABEL = { queued: "fix queued", running: "Claude Code working...", fixed: "fixed on branch", partial: "partly fixed",
                 needs_human: "needs you", failed: "fix failed", no_changes: "no changes", discarded: "discarded" };

// ---------- scopes: "This PC" + one per project ----------
MC.LANE_ORDER = ["now", "plan", "watch", "batch", "later"];
MC.latestFix = (S, keys) => (S.fixes || []).find((f) => ((f.result && f.result.keys) || [f.item_key]).some((k) => keys.includes(k)));

MC.scopeSummary = function scopeSummary(items) {
  const lanes = Object.fromEntries([...MC.LANE_ORDER, "dormant"].map((l) => [l, MC.groupByPackage(items.filter((i) => i.lane === l))]));
  const counts = Object.fromEntries(Object.entries(lanes).map(([l, xs]) => [l, xs.length]));
  const kev = MC.groupByPackage(items.filter((i) => i.kev)).length;
  const top = [...lanes.now, ...lanes.plan, ...lanes.watch, ...lanes.dormant, ...lanes.batch, ...lanes.later][0] || null;
  return { lanes, counts, kev, top, total: MC.groupByPackage(items).length };
};

MC.buildScopes = function buildScopes(S) {
  const pc = { id: "pc", kind: "pc", name: MC.MACHINE.label, items: S.items.filter((i) => !i.project) };
  const projects = S.projects.map((p) => ({ id: p.key, kind: "project", name: p.name, project: p,
    items: S.items.filter((i) => i.project === p.key),
    ignored: !!(S.prefs[`project:${p.key}`] || {}).ignored }));
  for (const sc of [pc, ...projects]) {
    sc.sum = MC.scopeSummary(sc.items);
    const mine = (i) => (sc.kind === "pc" ? !i.project : i.project === sc.id);
    sc.aside = (S.aside || []).filter(mine); sc.ignoredItems = (S.ignoredItems || []).filter(mine);
  }
  const urgency = (sc) => [sc.sum.kev, sc.sum.counts.now, sc.sum.counts.plan, sc.sum.counts.watch, sc.sum.counts.batch];
  const cmp = (a, b) => { const x = urgency(a), y = urgency(b); for (let k = 0; k < x.length; k++) if (x[k] !== y[k]) return y[k] - x[k]; return a.name.localeCompare(b.name); };
  const visible = projects.filter((p) => !p.ignored || S.showIgnored || p.items.length);  // an ignored project with an attack in it shows
  const peers = MC.peerScopes(S);
  return {
    pc, peers, ignoredProjects: projects.filter((p) => p.ignored),
    // a dormant project only lands in "now" through malware: that one can't wait in the dormant pile
    active: visible.filter((p) => !p.project.dormant || p.sum.counts.now).sort(cmp),
    dormant: visible.filter((p) => p.project.dormant && !p.sum.counts.now).sort((a, b) => b.sum.total - a.sum.total),
    byId: new Map([pc, ...projects, ...peers].map((s) => [s.id, s])),
  };
};

MC.route = {
  parse() {
    const h = decodeURIComponent(location.hash.replace(/^#\/?/, ""));
    if (h.startsWith("project/")) return { page: "scope", id: h.slice(8) };
    if (h === "pc") return { page: "scope", id: "pc" };
    if (h.startsWith("machine/")) return { page: "scope", id: `peer:${h.slice(8)}` };
    if (h === "packages") return { page: "packages" };
    if (h === "runs") return { page: "runs" };
    if (h === "usage") return { page: "usage" };
    if (h.startsWith("runs/")) return { page: "trace", id: h.slice(5) };
    return { page: "today" };  // also the old #/overview links
  },
  href(page, id) {
    if (page === "scope" && id.startsWith("peer:")) return `#/machine/${encodeURIComponent(id.slice(5))}`;
    if (page === "scope") return id === "pc" ? "#/pc" : `#/project/${encodeURIComponent(id)}`;
    return `#/${page}`;
  },
};

// The latest upgrade job for an item. Jobs that finished before the current snapshot are
// already reflected in it, so only newer ones are worth showing.
MC.jobFor = (S, key) => {
  const j = (S.jobs || []).find((x) => x.item_key === key);
  if (!j) return null;
  if (["waiting", "queued", "running"].includes(j.status)) return j;
  return (j.finished || 0) > (S.snapTs || 0) ? j : null;
};
// admin installs Bosun may run on a click: macOS (or pkexec on Linux) shows its own password window
MC.withPassword = (i) => !!((i.password || i.password_cask) && i.available && MC.MACHINE.password_window);
MC.passwordHint = () => MC.MACHINE.kind === "mac" ? "Asks for your Mac password once" : "Asks for your password once";
// what "Update all" may include: drop-in installs, and on Linux the password ones too (they run as one command)
MC.bulkReady = (S, m) => !MC.jobFor(S, m.key) && MC.kindOf(m) !== "hardware"
  && (m.upgradable || (MC.withPassword(m) && MC.MACHINE.kind === "linux"));
// a job or fix key -> the thing's name, also once an update has taken it out of the snapshot:
// "npm-global:@google/gemini-cli" -> "@google/gemini-cli", "repo:npm:next@<project>" -> "next" (a leading @ is a scope)
MC.nameOf = function nameOf(S, key) {
  if (key.startsWith("relaunch:")) return `Relaunched ${key.slice(9)}`;
  const known = (S.allItems || []).find((i) => i.key === key);
  return known ? known.name : key.split(":").slice(key.startsWith("repo:") ? 2 : 1).join(":").replace(/(.)@.*$/, "$1");
};
MC.PIN_LABEL = { queued: "pin queued", running: "pinning…", succeeded: "pinned ✓", failed: "pin failed" };
MC.jobLabel = (j) => ((Array.isArray(j.argv) && j.argv[0] === "pin" ? MC.PIN_LABEL : MC.JOB_LABEL)[j.status] || j.status);
// a Mac app with its own updater: Bosun opens it and the app does the rest
MC.openable = (m) => MC.MACHINE.kind === "mac" && m.self_updates && m.manual && /\.app$/.test(m.location || "")
  && ["mac-app", "brew-cask"].includes(m.source);
// a Mac app with no updater that is updated by hand: Bosun can watch its bundle for the new version (appwatch.py)
MC.watchable = (m) => MC.MACHINE.kind === "mac" && m.source === "mac-app" && !m.self_updates && !m.vendor && !!m.available
  && /\.app$/.test(m.location || "");
MC.watchFor = (S, m) => { const w = (S.watches || {})[m.key]; return w && !w.done ? w : null; };
MC.pinnable = (S, m) => m.source === "mcp" && m.fix_kind === "pin" && !MC.jobFor(S, m.key);
// The app's own updater doesn't offer this update yet (appupdaters.py), and nothing about it is urgent: it is not
// work for you today. Bosun holds it back (lanes.py puts it in Later) and asks again on a later check.
MC.heldBack = (i) => i.updater === "not_offered" && (i.risk || 0) < 7 && !i.kev;
const firstSentence = (s) => ((s || "").match(/^.*?[.…](?=\s|$)/) || [s || ""])[0];
// Who makes it, for grouping by publisher: from the bundle id or winget id; tools group under their package manager.
MC.publisherOf = function publisherOf(i) {
  const via = { brew: "Homebrew", "brew-cask": "Homebrew", "npm-global": "npm", "pip-global": "pip", apt: "apt", snap: "snap", "wsl-apt": "WSL",
                macos: "Apple", "windows-update": "Microsoft", distro: "The distribution", mcp: "MCP servers", runtime: "Runtimes", image: "Docker" }[i.source];
  if (via) return via;
  if (i.vendor) return i.vendor;
  const parts = String(i.package_id || "").split(".");
  const word = i.source === "mac-app" && parts.length > 2 ? parts[1] : parts.length > 1 ? parts[0] : "";
  return word ? word[0].toUpperCase() + word.slice(1) : "Other";
};
// how big the step is: 3 major, 2 minor, 1 patch, 0 unknown
MC.jumpOf = function jumpOf(i) {
  const nums = (v) => (String(v || "").match(/\d+/g) || []).map(Number);
  const a = nums(i.installed), b = nums(i.available);
  if (!a.length || !b.length) return 0;
  return a[0] !== b[0] ? 3 : a[1] !== b[1] ? 2 : 1;
};

// A major version whose release notes list breaking changes: nothing to do with security, but not "nothing to
// think about" either. The button still works; the words around it say to read first.
MC.bigJump = (i) => !i.project && !!i.available && MC.jumpOf(i) === 3 && (i.n_breaking || 0) > 0;
MC.jumpNote = (i) => `A major version, with ${MC.plural(i.n_breaking, "breaking change")} listed in the details. Read ${i.n_breaking === 1 ? "it" : "them"} first.`;

// A command inside an instruction (the first one; "Homebrew can..." must not count as `brew`), so it can be copied.
MC.manualCommand = (text) => ((text || "").match(/(?<![\w-])(?:brew|nvm|npm|softwareupdate|fwupdmgr) (?:install|upgrade|uninstall|alias|update|refresh|enable-remote|-)(?:[^,;`]*[^,;.` ])?/)
  || (text || "").match(/(?<![\w-])sudo (?:apt-get|apt|snap|do-release-upgrade|systemctl)\b(?:[^,;()`]*[^,;.()` ])?/) || [])[0] || "";

// ---------- how an item gets updated: its route ----------
// Every machine item has exactly one. It decides the group it sits in on the machine page, the wording of its one
// button, and what Today says about it. Most automatic first; the order never changes, so it can be learned.
MC.ROUTES = [
  { id: "bosun", name: ["Bosun updates these", "Bosun can update these"], chip: ["Bosun updates", "Bosun can update"],
    why: ["One click, no prompts. Autopilot can take these off your hands.", "One click. Nothing else to do."] },
  { id: "once", name: ["Needs you once", "Needs you for a moment"], chip: ["need you once", "need you for a moment"],
    why: ["Bosun runs the update. You type your password once, or quit the app so it can finish.",
          "Bosun does the update. You type your password, or quit the app so it can finish."] },
  { id: "self", name: ["Updates itself", "Updates itself"], chip: ["updates itself", "updates itself"],
    why: ["Has its own updater. Open it and it does the rest.", "Open the app and it updates on its own."] },
  { id: "system", name: [null, null], chip: [null, null], why: [null, null] },  // named after the OS's updater, below
  { id: "yours", name: ["Yours to do", "Only you can do these"], chip: ["yours to do", "only you can do"],
    why: ["Bosun can't run these: a download from the maker, or a command for you.",
          "Bosun can't install these. It shows you where to get the update."] },
  { id: "watch", name: ["Watching", "Nothing to do yet"], chip: ["watching", "nothing to do yet"],
    why: ["Vulnerable, but no fixed version yet. Bosun checks again every day.", "No fix exists yet. Bosun checks again every day."] },
];
const pick = (pair) => pair[MC.SIMPLE ? 1 : 0];
MC.routeInfo = function routeInfo(id, updates) {
  const r = MC.ROUTES.find((x) => x.id === id);
  const u = updates || MC.MACHINE.updates;
  if (id === "system") return { id, name: u, chip: `in ${u}`, why: `${u} installs these. Bosun shows what's waiting and opens it for you.` };
  return { id, name: pick(r.name), chip: pick(r.chip), why: pick(r.why) };
};
MC.routeOf = function routeOf(S, i) {
  if (i.project) return null;
  if (i.opens) return "system";
  if (i.lane === "watch") return "watch";
  if (i.source === "mcp" && i.fix_kind === "pin") return "bosun";
  if (i.vendor) return "yours";
  if (i.upgradable) return "bosun";
  if (MC.heldBack(i)) return "self";  // its own updater will offer it; nothing to do before then
  if ((i.run && MC.MACHINE.terminal) || MC.withPassword(i)) return "once";
  if (i.self_updates) return "self";
  return "yours";
};

// The one thing a click does for a machine item right now, or null. Rows, Today and the details all ask here, so
// an item shows the same button everywhere: `short` on a row, `label` in the details, and `how`, one sentence on
// exactly what the click does. A label ending in "…" means a prompt follows (a password, the app closing).
MC.itemAction = function itemAction(S, i) {
  if (i.project) return null;
  const job = MC.jobFor(S, i.key);
  if (job && ["waiting", "queued", "running", "succeeded"].includes(job.status)) return null;
  const again = job && job.status === "failed";
  const route = MC.routeOf(S, i);
  const big = MC.bigJump(i) ? `${MC.jumpNote(i)} ` : "";
  const say = (label, short, how, act) => ({ label, short: short || label, how: big + how, title: big + how, act });
  if (route === "system") return say(`Open ${MC.MACHINE.updates}`, "Open", `${MC.MACHINE.updates} installs this. Bosun opens it for you.`, () => MC.actions.openUpdates());
  if (route === "watch") return null;
  if (MC.pinnable(S, i)) return say("Pin its version", "Pin", "Pins it to the newest version that has been out three days. Bosun backs up the config file first.", () => MC.actions.pin([i.key]));
  if (route === "bosun" && i.upgradable) return say(again ? "Retry" : "Update", "", big ? "Bosun installs it when you click. No prompts." : "Bosun installs it now. No prompts.", () => MC.actions.upgrade([i.key]));
  if (route === "once") {
    const label = again ? "Retry…" : "Update…";
    if (i.run === "msupdate-install" && MC.MACHINE.terminal) return say(label, "",
      `Bosun asks Microsoft AutoUpdate to install it, here in its own terminal. No password. If ${i.name} is open, the update goes in once you quit it.`,
      () => MC.runRecipe(i.key, i.run, i.name));
    // a hand-installed app Homebrew can replace without a password (takeover.py); the click asks once, in words
    if (i.run === "brew-cask-takeover" && MC.MACHINE.terminal) return say(label, "",
      `${i.name} has no updater. Bosun can hand it to Homebrew, which installs ${i.available} and keeps it current from then on. Bosun keeps your ${i.installed} for 14 days. No password.`,
      () => {
        if (!confirm(`Update ${i.name} with Homebrew?\n\nHomebrew downloads ${i.available} and checks it against its published checksum. Bosun keeps your ${i.installed} for 14 days, and puts it back if the new app is not signed by the same developer. Your settings and files are not touched. No password.\n\nFrom then on Homebrew keeps ${i.name} current. Cancel to do it yourself: the maker's page is under "Other ways to update" in the details.`)) return;
        MC.runRecipe(i.key, i.run, i.name);
      });
    if (MC.withPassword(i)) return say(label, "", `Bosun runs the installer. ${MC.passwordHint()}; nothing else is touched.`, () => MC.actions.upgrade([i.key]));
    return say(label, "", "Bosun runs the command here, in its own terminal, and you type your password there.", () => MC.runRecipe(i.key, i.run, i.name));
  }
  if (MC.heldBack(i)) return null;
  if (MC.openable(i)) return say(`Open ${i.name}`, "Open app", "Opens the app so its own updater can run. Accept the update it offers, or restart it if one is waiting.", () => MC.actions.openApp(i));
  // a manual update Bosun is watching for (appwatch.py): the click was made, the new version isn't there yet
  const watch = MC.watchFor(S, i);
  if (watch) return say("Check now", "", `Waiting for ${i.name} ${watch.want || ""} to appear in Applications. Bosun looks every half minute for an hour; Check now looks at once.`.replace("  ", " "),
    () => MC.actions.checkApps());
  const page = (i.vendor ? (i.vendor_url || i.release_notes_url) : "") || i.download_url
    || (i.source === "mac-app" && !i.self_updates && i.release_notes_url) || "";
  if (page) {
    let host = ""; try { host = new URL(page).hostname.replace(/^www\./, ""); } catch { /* not a URL */ }
    if (MC.watchable(i)) return say(`Get it from ${host || "the maker"}`, "Get it",
      `${firstSentence(i.manual)} Bosun can't install this one: it opens the maker's page, then watches for the new version and says when it's there.`,
      () => { MC.openExternal(page); MC.actions.watchApp(i); });
    return say(`Get it from ${i.vendor || host || "the maker"}`, "Get it", "Opens the maker's download page in your browser. Bosun can't install this one.", () => MC.openExternal(page));
  }
  const cmd = MC.manualCommand(i.manual);
  if (cmd) return say("Copy command", "", "Copies the command for you to run in a terminal. Bosun re-checks afterwards.", (e) => {
    const b = e && e.currentTarget;
    navigator.clipboard.writeText(cmd).then(() => { if (b) b.textContent = "Copied"; }, () => { if (b) b.textContent = "Copy failed"; });
  });
  return null;
};
// How a suite or a group can be updated in one go: every member the same way, or not at all.
//   "upgrade": Bosun's own installer path (a password window where one is needed); "recipe:<name>": one command in
//   Bosun's terminal for all of them (only recipes that ask for nothing).
MC.bulkMode = function bulkMode(S, i) {
  if (MC.jobFor(S, i.key) || MC.kindOf(i) === "hardware") return "";
  const route = MC.routeOf(S, i);
  if (route === "bosun" && i.upgradable) return "upgrade";
  if (route !== "once") return "";
  if (i.run === "msupdate-install" && MC.MACHINE.terminal) return "recipe:msupdate-install";
  return MC.withPassword(i) ? "upgrade…" : "";
};
// Suites: packages of one product family (the item's `suite`, from its collector or kinds.py) that are in the same
// group and carry the same update show as one row. A member that differs (another version, exploited, a job under
// way that the others don't have) is never folded in: it stays on its own line.
// -> [{ one: pkg } | { suite: { name, pkgs } }], in the order given.
MC.suites = function suites(S, pkgs) {
  // Simple view: tools and shared components are one row too. Nobody there should be asked to judge lz4.
  const tool = (p) => MC.SIMPLE && ["dev", "component"].includes(MC.kindOf(p.members[0])) && !MC.isFinding(p) && p.risk < 4;
  const keyOf = (p) => (tool(p) ? "tools" : p.members[0].suite ? `${p.members[0].suite}|${p.installed}|${p.available}` : "");
  const fam = new Map();
  for (const p of pkgs) {
    const k = keyOf(p);
    if (!k || p.kev || p.members[0].malicious || p.project) continue;
    fam.set(k, [...(fam.get(k) || []), p]);
  }
  const out = [], done = new Set();
  for (const p of pkgs) {
    if (done.has(p)) continue;
    const group = fam.get(keyOf(p));
    if (group && group.length > 1 && group.includes(p)) {
      group.forEach((x) => done.add(x));
      out.push({ suite: tool(p) ? { name: "Background tools", pkgs: group, tools: true } : { name: p.members[0].suite, pkgs: group } });
    } else out.push({ one: p });
  }
  return out;
};
// What one click can do for several items: { label, prompts, act } when every one of them goes the same way.
MC.bulkAction = function bulkAction(S, members, all) {
  const ready = members.filter((m) => MC.bulkMode(S, m));
  const modes = new Set(ready.map((m) => MC.bulkMode(S, m).replace("…", "")));
  if (ready.length < 2 || modes.size !== 1) return null;
  const prompts = ready.some((m) => MC.bulkMode(S, m) !== "upgrade");
  return { ready, prompts, label: `Update ${all ? "all " : ""}${ready.length}${prompts ? "…" : ""}`,
    title: prompts ? "Needs you once: a password, or an app to quit" : "", act: () => MC.actions.updateMany(ready) };
};
// When there is no button: why Bosun can't, and the single next step, in one sentence.
MC.itemWhy = (i) => firstSentence(i.manual) || (i.available ? "" : "No fixed version yet. Bosun checks again every day.");
MC.JOB_LABEL = { waiting: "waiting until free", cancelled: "cancelled", queued: "queued", running: "updating…", succeeded: "updated ✓ re-checking", failed: "update failed", abandoned: "interrupted" };

// ---------- SVG: icons + a tiny builder for charts (attributes only; CSP-safe) ----------
MC.SVGNS = "http://www.w3.org/2000/svg";
MC.svg = function svg(tag, attrs = {}, parent = null) {
  const n = document.createElementNS(MC.SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.append(n);
  return n;
};
const ICONS = {
  today: "M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
  machine: "M3 5h18v11H3zM8 20h8M12 16v4",
  packages: "M12 3l8 4.5v9L12 21l-8-4.5v-9zM12 12l8-4.5M12 12v9M12 12L4 7.5",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  check: "M5 12l5 5 9-10",
  mark: "M4 15c3 3 13 3 16 0M12 3v12M8 7h8",
  bell: "M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15zM10 21h4",
  laptop: "M5 6h14v10H5zM2.5 19h19",
  bellOff: "M6 16V11a6 6 0 0 1 9.5-4.9M18 11v5l1.5 2h-15M10 21h4M4 4l16 16",
  runs: "M3 12h4l3-7 4 14 3-7h4",
  usage: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  ask: "M4 5h16v11h-9l-5 4v-4H4z",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  find: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4-4",
  term: "M5 8l4 4-4 4M12 16h7",
  folder: "M3 7a1 1 0 0 1 1-1h5l2 2h8a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z",
  bolt: "M13 3L5 13h6l-1 8 8-10h-6z",
  app: "M5 4h14a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1zM4 9h16",
  key: "M14.5 9.5a4 4 0 1 0-3.6 3.98L9 15.5H7v2H5v2H3v-2.6l5.02-5.02A4 4 0 0 0 14.5 9.5z",
  plug: "M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4",
};
MC.icon = function icon(name, stroke = "currentColor", width = 2) {
  const s = MC.svg("svg", { viewBox: "0 0 24 24", fill: "none", stroke, "stroke-width": width,
    "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" });
  MC.svg("path", { d: ICONS[name] }, s);
  return s;
};

// The tile at the start of a row: a neutral glyph by kind. Never a letter, and never artwork Bosun ships:
// an app's own icon, where the machine has one, is read from it at run time (see MC.tile in rows.js).
MC.glyphFor = function glyphFor(i) {
  if (i.source === "secret") return "key";
  if (i.source === "mcp") return "plug";
  if (i.project) return "folder";
  const k = MC.kindOf(i);
  return ["os", "firmware", "driver", "hardware"].includes(k) ? "machine" : k === "app" || k === "browser" ? "app" : "term";
};
MC.glyph = (name, cls) => MC.el("span", { class: `glyph${cls ? ` ${cls}` : ""}`, "aria-hidden": "true" }, MC.icon(name));

// "today", "Nov 10" (for "acknowledged Nov 10", "until Dec 10")
MC.day = function day(ts) {
  const d = new Date(ts * 1000);
  return d.toDateString() === new Date().toDateString() ? "today" : d.toLocaleDateString([], { month: "short", day: "numeric" });
};
// What you said about an item you put away, as words: "Acknowledged Nov 8", "Snoozed until Dec 10", "Skipping 12.2.2"
MC.saidText = function saidText(st) {
  const said = st.state || st.said;
  if (said === "ack") return [MC.words("Acknowledged", "Set aside"), st.at ? MC.day(st.at) : ""];
  if (said === "snoozed" && st.until_version) return ["Skipping", st.until_version];
  if (said === "snoozed") return [MC.words("Snoozed", "Reminding you"), st.until_ts ? `${MC.words("until", "on")} ${MC.day(st.until_ts)}` : ""];
  return ["Ignored", st.at ? MC.day(st.at) : ""];
};
MC.saidPast = (st) => ({ ack: MC.words("acknowledged it", "set it aside"), snoozed: st.until_version ? `skipped ${st.until_version}` : "snoozed it",
                         ignored: "ignored it", ignored_project: "ignored its project" }[st.said || st.state] || "put it away");

// "03:02" today, "Oct 21" before that
MC.clock = function clock(ts) {
  const d = new Date(ts * 1000), now = new Date();
  return d.toDateString() === now.toDateString()
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" });
};
