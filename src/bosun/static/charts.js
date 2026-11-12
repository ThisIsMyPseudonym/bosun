// Today's two charts: open updates over time, and the projects that need the most work.
"use strict";
(() => {
const { el, svg } = MC;
const W = 330, H = 150;

function chartCard(title, sub) {
  const tip = el("div", { class: "tip", hidden: true });
  const card = el("section", { class: "panel chart" }, el("h3", { text: title }), el("div", { class: "cs", text: sub }));
  const show = (ev, text) => {
    const r = card.getBoundingClientRect();
    tip.textContent = text; tip.hidden = false;
    tip.style.left = `${ev.clientX - r.left + 12}px`; tip.style.top = `${ev.clientY - r.top - 34}px`;
  };
  return { card, tip, show, hide: () => { tip.hidden = true; } };
}

// one point per day (the last refresh of each day), every open update outside dormant projects
MC.views.trendChart = function trendChart(S) {
  const perDay = new Map();
  for (const h of S.history.filter((x) => x.lanes)) perDay.set(new Date(h.ts * 1000).toDateString(), h);
  const pts = [...perDay.values()].slice(-30).map((h) => ({ ts: h.ts,
    v: Object.entries(h.lanes).filter(([l]) => l !== "dormant").reduce((n, [, c]) => n + c, 0) }));
  const c = chartCard("Open updates", pts.length > 1 ? `Last ${pts.length} days, everything Bosun watches` : "One point per day");
  if (pts.length < 2) {
    c.card.append(el("p", { class: "empty", text: "The line appears after refreshes on two different days." }));
    return c.card;
  }
  const X0 = 30, X1 = 322, Y0 = 128, Y1 = 12;
  const vs = pts.map((p) => p.v), lo = Math.max(0, Math.min(...vs) - 10), hi = Math.max(...vs) + 10;
  const x = (i) => X0 + i * (X1 - X0) / (pts.length - 1), y = (v) => Y0 - (v - lo) / (hi - lo) * (Y0 - Y1);
  const day = (ts) => new Date(ts * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Open updates per day: ${vs.join(", ")}` });
  const g = svg("linearGradient", { id: "fadeMint", x1: 0, x2: 0, y1: 0, y2: 1 }, svg("defs", {}, s));
  svg("stop", { offset: 0, class: "stop-a" }, g); svg("stop", { offset: 1, class: "stop-b" }, g);
  for (const v of [lo, Math.round((lo + hi) / 2), hi]) {
    svg("line", { class: "grid", x1: X0, x2: X1, y1: y(v), y2: y(v) }, s);
    svg("text", { class: "axis", x: X0 - 6, y: y(v) + 4, "text-anchor": "end" }, s).textContent = String(Math.round(v));
  }
  const last = pts.length - 1;
  [[0, "start"], [last, "end"]].forEach(([i, a]) => { svg("text", { class: "axis", x: x(i), y: H - 4, "text-anchor": a }, s).textContent = day(pts[i].ts); });
  const line = pts.map((p, i) => `${x(i)},${y(p.v)}`).join(" ");
  svg("polygon", { points: `${X0},${Y0} ${line} ${X1},${Y0}`, fill: "url(#fadeMint)" }, s);
  svg("polyline", { class: "trendline", points: line }, s);
  svg("circle", { class: "pt", cx: x(last), cy: y(vs[last]), r: 4.5 }, s);
  svg("text", { class: "label", x: x(last) - 4, y: y(vs[last]) + (y(vs[last]) < 40 ? 20 : -10), "text-anchor": "end" }, s).textContent = `${vs[last]} now`;
  const cross = svg("line", { class: "cross", y1: Y1, y2: Y0, visibility: "hidden" }, s);
  const dot = svg("circle", { class: "pt", r: 4.5, visibility: "hidden" }, s);
  const hit = svg("rect", { x: X0, y: Y1, width: X1 - X0, height: Y0 - Y1, fill: "transparent" }, s);
  hit.addEventListener("mousemove", (ev) => {
    const r = s.getBoundingClientRect(), sx = (ev.clientX - r.left) / r.width * W;
    const i = Math.max(0, Math.min(last, Math.round((sx - X0) / (X1 - X0) * last)));
    for (const [n, a] of [[cross, { x1: x(i), x2: x(i) }], [dot, { cx: x(i), cy: y(vs[i]) }]]) {
      for (const [k, v] of Object.entries(a)) n.setAttribute(k, v);
      n.setAttribute("visibility", "visible");
    }
    c.show(ev, `${vs[i]} open · ${day(pts[i].ts)}`);
  });
  hit.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); c.hide(); });
  c.card.append(s, c.tip);
  return c.card;
};

// stacked bars: exploited / needs migration work / drop-in, for the six busiest active projects
MC.views.projectChart = function projectChart(scopes) {
  const c = chartCard("Projects", "Dependency updates by what they take");
  const rows = scopes.active.map((sc) => {
    const l = sc.sum.lanes, nonKev = (ps) => ps.filter((p) => !p.kev).length;
    const kev = [...l.now, ...l.plan, ...l.watch, ...l.batch, ...l.later].filter((p) => p.kev).length;
    return { name: sc.name, v: [kev, nonKev(l.plan) + nonKev(l.later), nonKev(l.now) + nonKev(l.batch)] };
  }).filter((r) => r.v.some(Boolean)).sort((a, b) => b.v[0] - a.v[0] || b.v[1] - a.v[1] || b.v[2] - a.v[2]).slice(0, 6);
  if (!rows.length) {
    c.card.append(el("p", { class: "empty", text: "No project has outdated dependencies." }));
    return c.card;
  }
  const CLS = ["c-brick", "c-ochre", "c-teal"], LBL = ["exploited", "need work", "drop-in"];
  const X0 = 116, BW = 180, max = Math.max(...rows.map((r) => r.v[0] + r.v[1] + r.v[2]));
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": rows.map((r) => `${r.name}: ${r.v.map((n, k) => `${n} ${LBL[k]}`).join(", ")}`).join("; ") });
  rows.forEach((r, i) => {
    const yy = 6 + i * 22.5;
    svg("text", { class: "axis", x: X0 - 8, y: yy + 10, "text-anchor": "end" }, s).textContent = r.name.length > 17 ? `${r.name.slice(0, 16)}…` : r.name;
    let xx = X0;
    r.v.forEach((n, k) => {
      if (!n) return;
      const w = Math.max(3, n / max * BW - 2);
      const b = svg("rect", { class: CLS[k], x: xx, y: yy, width: w, height: 13, rx: 3 }, s);
      b.addEventListener("mousemove", (ev) => c.show(ev, `${r.name} · ${n} ${LBL[k]}`));
      b.addEventListener("mouseleave", c.hide);
      xx += w + 2;
    });
    svg("text", { class: "label", x: xx + 5, y: yy + 10 }, s).textContent = String(r.v[0] + r.v[1] + r.v[2]);
  });
  c.card.append(s, c.tip, el("div", { class: "legend" }, ...LBL.map((l, k) => el("span", { class: "key" }, el("b", { class: CLS[k] }), l[0].toUpperCase() + l.slice(1)))));
  return c.card;
};
})();
