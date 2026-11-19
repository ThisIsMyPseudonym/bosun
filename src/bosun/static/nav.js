// Sidebar: Today, this machine, active projects (dormant ones folded away), Packages.
"use strict";
(() => {
const { el } = MC;

function badge(sc) {
  const c = sc.sum.counts;
  if (sc.project && sc.project.dormant && !sc.sum.counts.now) return el("span", { class: "badge muted", text: String(sc.sum.total) });
  if (c.now || c.plan) return el("span", { class: `badge${sc.sum.kev ? " hot" : ""}`, title: `${c.now + c.plan} high-risk`, text: String(c.now + c.plan) });
  // this machine: how many updates wait, as a plain number (nothing urgent among them)
  if (sc.kind === "pc" && sc.sum.total) return el("span", { class: "badge ok", title: `${sc.sum.total} waiting, none urgent`, text: String(sc.sum.total) });
  return null;
}

function version() {  // which Bosun this dashboard runs, and how its last self-update went
  const v = MC.MACHINE.bosun;
  if (!v || !v.commit) return null;
  const when = Date.parse(v.date);
  const u = v.update;  // {status, detail, at, channel}; a refused or failed update stays visible until resolved
  const stopped = u && u.status === "failed";
  return el("div", { class: `version${stopped ? " warn" : ""}`,
    title: `Bosun ${v.commit}${v.date ? `, committed ${v.date}` : ""}${u ? `\nSelf-update (${u.channel}): ${u.detail}` : ""}`,
    text: `Bosun ${v.commit}${when ? ` · ${MC.ago(when / 1000)}` : ""}${stopped ? " · update stopped" : ""}` });
}

MC.views.nav = function nav(S, scopes, route) {
  const link = (href, label, extra, current, icon) => el("a", { href, class: `navlink${current ? " current" : ""}`,
    "aria-current": current ? "page" : null }, icon ? MC.icon(icon) : null, el("span", { class: "navname", text: label }), extra);
  const isCur = (id) => route.page === "scope" && route.id === id;
  const dormantOpen = S.dormantOpen || (route.page === "scope" && scopes.dormant.some((d) => d.id === route.id));
  // the simple view lists no projects, so its count leaves them out too
  const needs = MC.needsYou(S, scopes).filter((n) => !MC.SIMPLE || n.kind !== "project").length;
  const viewSwitch = el("button", { class: "viewswitch",
    title: MC.SIMPLE ? "Show projects, packages and the technical detail" : "Hide developer screens and use plain words",
    onclick: () => MC.actions.setView(MC.SIMPLE ? "developer" : "simple") }, MC.SIMPLE ? "Switch to developer view" : "Switch to simple view");
  if (MC.SIMPLE) {  // a computer, not a codebase: Today and this machine
    return [
      link("#/", "Today", needs ? el("span", { class: "badge", text: String(needs) }) : null, route.page === "today", "today"),
      el("div", { class: "navgroup", text: "Machine" }),
      link(MC.route.href("scope", "pc"), MC.MACHINE.label, badge(scopes.pc), isCur("pc"), MC.MACHINE.kind === "mac" ? "laptop" : "machine"),
      viewSwitch,
      version(),
    ];
  }
  return [
    link("#/", "Today", needs ? el("span", { class: "badge", text: String(needs) }) : null, route.page === "today", "today"),
    link("#/packages", "Packages", null, route.page === "packages", "packages"),
    link("#/runs", "Runs", null, route.page === "runs" || route.page === "trace", "runs"),
    link("#/usage", "AI usage", null, route.page === "usage", "usage"),
    el("div", { class: "navgroup", text: "Machine" }),
    link(MC.route.href("scope", "pc"), MC.MACHINE.label, badge(scopes.pc), isCur("pc"), MC.MACHINE.kind === "mac" ? "laptop" : "machine"),
    el("div", { class: "navgroup", text: `Projects · ${scopes.active.length}` }),
    ...scopes.active.map((sc) => link(MC.route.href("scope", sc.id), sc.name, badge(sc), isCur(sc.id))),
    scopes.dormant.length ? el("button", { class: "navgroup toggle", "aria-expanded": String(!!dormantOpen),
      onclick: () => { S.dormantOpen = !dormantOpen; MC.render(); } },
      `${dormantOpen ? "▾" : "▸"} Dormant · ${scopes.dormant.length}`) : null,
    ...(dormantOpen ? scopes.dormant.map((sc) => link(MC.route.href("scope", sc.id), sc.name, badge(sc), isCur(sc.id))) : []),
    scopes.ignoredProjects.length ? el("button", { class: "navgroup toggle", "aria-expanded": String(!!S.showIgnored),
      title: "Projects you ignored. Shown, they appear in the lists above again.",
      onclick: () => MC.actions.showIgnored(!S.showIgnored) },
      `${S.showIgnored ? "▾ Hide" : "▸ Show"} ignored · ${scopes.ignoredProjects.length}`) : null,
    viewSwitch,
    version(),
  ];
};
})();
