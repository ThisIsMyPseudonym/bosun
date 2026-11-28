// Automations: owner-written rules, shipped-off presets, kill switch, renewal and history. #/automations
// All text goes through MC.el({ text }) / textContent, never innerHTML.
"use strict";
(() => {
const { el } = MC;

MC.views.automations = function automations() {
  return el("div", { class: "automations" },
    el("div", { class: "pagehead" },
      el("h1", { text: "Automations" }),
      el("p", { class: "hint", text: "When a trigger fires and its conditions hold, run one Bosun action." })));
};
})();
