// Other machines, read-only: their scopes (for the sidebar and their own page) and the Machines card.
"use strict";
(() => {
const { el, plural } = MC;
const HIGH = ["now", "plan", "watch"];
const STALE_S = 3600;  // summaries come every 15 minutes; an hour of silence usually means asleep or offline

// Each machine Bosun has heard from becomes a scope, like "This PC", built from the summary it sent.
MC.peerScopes = function peerScopes(S) {
  return (S.peers || []).filter((p) => p.data).map((p) => {
    const items = p.data.items.map((i) => ({ ...i, project: "", project_name: "" }));
    const sc = { id: `peer:${p.id}`, kind: "peer", name: p.name, peer: p, items, sum: MC.scopeSummary(items) };
    // the same count Today uses: risky machine updates, projects with risky dependencies, pending relaunches
    sc.needs = HIGH.reduce((n, l) => n + sc.sum.counts[l], 0) + p.data.projects.length + p.data.relaunch.length;
    sc.kevs = sc.sum.kev + p.data.projects.filter((x) => x.kev).length;
    return sc;
  });
};

MC.peerHeard = function peerHeard(p) {
  const stale = !p.received || Date.now() / 1000 - p.received > STALE_S;
  return { stale, text: !p.received ? "" : stale ? `last heard from ${MC.ago(p.received)}`
    : p.data.snapshot_ts ? `checked ${MC.ago(p.data.snapshot_ts)}` : "no check yet",
    short: !p.received ? "" : stale ? `silent ${MC.ago(p.received).replace(" ago", "")}` : p.data.snapshot_ts ? MC.ago(p.data.snapshot_ts) : "" };
};

const needsText = (n) => n ? `${n} need${n === 1 ? "s" : ""} you` : "All clear";

function machineRow({ name, kind, sub, tone, href, kev, title }) {
  const body = el("div", { class: "mbody" },
    el("div", { class: "mname" }, el("span", { text: name }), kev ? el("span", { class: "chip kev", text: `${kev} exploited` }) : null),
    el("div", { class: "msub", text: sub }));
  const kids = [el("span", { class: "micon" }, MC.icon(kind === "mac" ? "laptop" : "machine")), body, el("span", { class: `dot ${tone}` })];
  return href ? el("a", { class: "mrow", href, title }, ...kids) : el("div", { class: "mrow", title }, ...kids);
}

MC.views.machinesCard = function machinesCard(S, scopes) {
  if (!(S.peers || []).length) return null;
  const here = MC.needsYou(S, scopes);
  const hereKev = here.filter((n) => n.kev).length;
  const rows = [machineRow({ name: MC.MACHINE.name || MC.MACHINE.label, kind: MC.MACHINE.kind, href: "#/pc",
    sub: `${needsText(here.length)} · here`, tone: hereKev ? "bad" : here.length ? "busy" : "ok", kev: hereKev })];
  for (const sc of scopes.peers) {
    const h = MC.peerHeard(sc.peer);
    rows.push(machineRow({ name: sc.name, kind: sc.peer.kind, href: MC.route.href("scope", sc.id), kev: sc.kevs,
      sub: [needsText(sc.needs), h.short].filter(Boolean).join(" · "), tone: h.stale ? "" : sc.kevs ? "bad" : sc.needs ? "busy" : "ok",
      title: h.stale && sc.peer.error ? `Last try: ${sc.peer.error}` : null }));
  }
  for (const p of S.peers.filter((x) => !x.data)) {  // linked, but never reached
    rows.push(machineRow({ name: p.name, kind: "", sub: "Not reached yet", tone: "", title: p.error }));
  }
  return el("section", { class: "panel machines" }, el("div", { class: "sec" }, el("h3", { text: "Machines" })), ...rows);
};

// The extra panel on another machine's page: what can't be shown as rows, and how to act on it.
MC.views.peerExtras = function peerExtras(sc) {
  const d = sc.peer.data, h = MC.peerHeard(sc.peer);
  const lines = [
    ...d.relaunch.map((r) => `${r.name} ${r.version} has downloaded: relaunch it there to finish.`),
    ...d.projects.map((p) => `${p.name}: ${plural(p.now + p.plan, "high-risk dependency", "high-risk dependencies")}`
      + `${p.plan ? `, ${p.plan} need migration work` : ""}${p.kev ? " (exploited)" : ""}.`),
  ];
  const ap = d.autopilot;
  return el("section", { class: "panel peerinfo" },
    el("p", { class: "hint", text: `Read-only here. Updates run on ${sc.name} itself: open Bosun there (http://127.0.0.1:8787).` }),
    el("p", { class: "cardmeta", text: [
      h.text && h.text[0].toUpperCase() + h.text.slice(1),
      ap.on ? `Autopilot on${ap.time ? ` at ${ap.time}` : ""}, ${ap.queued} queued${ap.hands_off ? `, ${ap.hands_off} left for you` : ""}` : "Autopilot off",
      `last 24 h: ${d.jobs24.succeeded} updated, ${d.jobs24.failed} failed`,
      sc.peer.error && h.stale ? `last try: ${sc.peer.error}` : "",
    ].filter(Boolean).join(" · ") }),
    lines.length ? el("ul", { class: "peerlines" }, ...lines.map((t) => el("li", { text: t }))) : null);
};
})();
