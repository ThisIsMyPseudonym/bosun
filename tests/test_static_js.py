"""The dashboard's scripts have no build step and no linter, so a function deleted while it is still called only
fails when someone opens that page ("Can't find variable: strip", November 2026). These checks read the scripts
as text: every function called is defined in its file, every MC.<name> used is defined somewhere, and the
spinner's style cannot land on a row that merely has a "busy" tone."""
from __future__ import annotations

import pathlib
import re

STATIC = pathlib.Path(__file__).resolve().parents[1] / "src" / "bosun" / "static"
SCRIPTS = sorted(STATIC.glob("*.js"))

GLOBALS=set("""if for while switch catch function return typeof await async new delete void in of do else super
fetch setTimeout clearTimeout setInterval clearInterval confirm alert prompt String Number Boolean Array Object Map Set Date Math JSON Promise Error
URLSearchParams EventSource encodeURIComponent decodeURIComponent parseInt parseFloat isNaN requestAnimationFrame queueMicrotask getComputedStyle
Symbol RegExp URL Intl WeakMap TextDecoder TextEncoder structuredClone matchMedia Blob""".split())
def code_only(src):
    """The script with comments, strings and regex literals blanked; a template literal keeps its ${...} code."""
    out=[];i=0;n=len(src)
    while i<n:
        c=src[i]
        if src.startswith("//",i):
            j=src.find("\n",i); i=n if j<0 else j
        elif src.startswith("/*",i):
            j=src.find("*/",i); i=n if j<0 else j+2
        elif c in "\"'":
            j=i+1
            while j<n and src[j]!=c: j+=2 if src[j]=="\\" else 1
            out.append('""'); i=j+1
        elif c=="`":
            j=i+1; depth=0; code=[]
            while j<n and not (src[j]=="`" and depth==0):
                if src[j]=="\\": j+=2; continue
                if src.startswith("${",j) and depth==0: depth=1; j+=2; code.append(" "); continue
                if depth:
                    if src[j]=="{": depth+=1
                    elif src[j]=="}": depth-=1
                    if depth: code.append(src[j])
                j+=1
            out.append('"" '+code_only("".join(code))+" "); i=j+1
        elif c=="/" and re.search(r"(^|[(,=:\[!&|?{};]|return)\s*$","".join(out[-12:])):
            j=i+1; cls=False
            while j<n and (cls or src[j]!="/") and src[j]!="\n":
                if src[j]=="\\": j+=2; continue
                cls = True if src[j]=="[" else False if src[j]=="]" else cls
                j+=1
            out.append("/x/"); i=j+1
        else: out.append(c); i+=1
    return "".join(out)
def undefined(src):
    s=code_only(src); bad=set()
    for m in re.finditer(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(",s):
        name=m.group(1)
        if name in GLOBALS: continue
        bound=re.search(rf"(function\s+{re.escape(name)}\b|(?<![.\w$]){re.escape(name)}\s*(=(?!=)|=>|,|\)|\}}|:)|[,({{\[]\s*{re.escape(name)}\s*[,)}}\]=]|[{{,]\s*(async\s+)?{re.escape(name)}\s*\([^()]*\)\s*\{{)",s)
        if not bound: bad.add(name)
    return bad


def test_every_function_a_script_calls_is_defined_in_it():
    missing = {f.name: sorted(undefined(f.read_text(encoding="utf-8"))) for f in SCRIPTS}
    assert {k: v for k, v in missing.items() if v} == {}


def test_the_check_catches_a_call_to_a_deleted_function():
    assert undefined("const el = MC.el;\nMC.views.scope = function scope(sc) { return el(\"div\", {}, strip(sc)); };") == {"strip"}
    assert undefined("function strip(sc) { return sc; }\nconst x = { open(key) { return strip(key); } };\nx.open(`${strip(1)}`);") == set()


def test_every_shared_function_used_is_defined_somewhere():
    code = "\n".join(code_only(f.read_text(encoding="utf-8")) for f in SCRIPTS)
    used = set(re.findall(r"\bMC\.([A-Za-z_]\w*)", code))
    defined = set(re.findall(r"\bMC\.([A-Za-z_]\w*)\s*(?:\|\|)?=(?!=)", code))
    assert sorted(used - defined) == []
    for space in ("actions", "views"):
        block = set(re.findall(rf"\bMC\.{space}\.([A-Za-z_]\w*)\s*=(?!=)", code))
        m = re.search(rf"\bMC\.{space}\s*=\s*\{{", code)
        if m:  # an object literal: its method names
            block |= set(re.findall(r"(?:^|[{,])\s*(?:async\s+)?([A-Za-z_]\w*)\s*\(", code[m.end() - 1:], flags=re.M))
        assert sorted(set(re.findall(rf"\bMC\.{space}\.([A-Za-z_]\w*)", code)) - block) == [], space


def test_a_tone_word_is_never_a_style_of_its_own():
    """Rows and dots take a tone as a class ("ok", "bad", "busy", "warn"). A bare rule for one of those words
    would restyle every one of them: the spinner was `.busy`, and a running run's row spun."""
    css = "\n".join(re.sub(r"/\*.*?\*/", "", f.read_text(encoding="utf-8"), flags=re.S) for f in STATIC.glob("*.css"))
    selectors = [s.strip() for block in re.findall(r"([^{}]+)\{", css) for s in block.split(",")]
    assert [s for s in selectors if s in (".ok", ".bad", ".busy", ".warn", ".hot", ".open", ".on", ".zero")] == []


def test_usage_page_never_uses_innerhtml_or_retired_budget_endpoint():
    src = (STATIC / "usage.js").read_text(encoding="utf-8")
    assert "innerHTML" not in code_only(src)
    assert "/api/usage/budget" not in src
    assert "/api/ai/override" in src
    assert "/api/ai/routing" in src
    assert "/api/ai/notice/dismiss" in src


def test_usage_rendering_helpers_handle_windows_stale_overrides_and_escaping():
    import json
    import shutil
    import subprocess
    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    ui_js = str(STATIC / "ui.js")
    usage_js = str(STATIC / "usage.js")
    script = f"""
    const fs = require("fs");
    function mkNode(tag) {{
      const n = {{
        tagName: tag.toUpperCase(), attrs: {{}}, style: {{}}, children: [], listeners: {{}},
        _text: "", className: "", hidden: false, open: false, disabled: false, value: "", checked: false,
        get textContent() {{ return this._text || this.children.map((c) => typeof c === "string" ? c : c.textContent).join(""); }},
        set textContent(v) {{ this._text = String(v); this.children = []; }},
        setAttribute(k, v) {{ this.attrs[k] = String(v); if (k === "value") this.value = String(v); }},
        getAttribute(k) {{ return this.attrs[k] ?? null; }},
        addEventListener(ev, fn) {{ (this.listeners[ev] = this.listeners[ev] || []).push(fn); }},
        append(...kids) {{ for (const k of kids) this.children.push(k); }},
      }};
      return n;
    }}
    globalThis.window = globalThis;
    globalThis.document = {{
      querySelector: () => ({{ content: "tok" }}),
      createElement: (tag) => mkNode(tag),
      createElementNS: (_ns, tag) => mkNode(tag),
    }};
    eval(fs.readFileSync({json.dumps(ui_js)}, "utf8"));
    MC.route = {{ parse: () => ({{ page: "usage" }}) }};
    MC.render = () => {{}};
    MC.nameOf = (_S, k) => k;
    MC.runHref = (id) => "#/runs/" + id;
    MC.ago = () => "1m ago";
    eval(fs.readFileSync({json.dumps(usage_js)}, "utf8"));

    const H = MC.usageHelpers;
    if (!H) throw new Error("MC.usageHelpers missing");
    if (H.fmtPct(null) !== "unknown") throw new Error("null share must be unknown, got " + H.fmtPct(null));
    if (H.fmtPct(0.41) !== "41%") throw new Error("expected 41%, got " + H.fmtPct(0.41));
    if (H.fmtPace(null) !== "—") throw new Error("expected —, got " + H.fmtPace(null));
    if (H.fmtPace(1.25) !== "1.25×") throw new Error("expected 1.25×, got " + H.fmtPace(1.25));

    // Stale reading renders as unknown, never 0%
    const staleRow = H.windowRow({{ window: "seven_day", label: "weekly limit", share: null, pace: null, reset: "", stale: true, bosun_share: null }});
    const staleText = staleRow.textContent;
    if (!staleText.includes("unknown") || staleText.includes("0%")) throw new Error("stale row wrong: " + staleText);

    // Current reading renders share, 75% tick, pace, reset, Bosun's share
    const okRow = H.windowRow({{ window: "five_hour", label: "5-hour limit", share: 0.41, pace: 0.82, reset: "Fri 2:00 PM", stale: false, bosun_share: 0.08 }});
    const okText = okRow.textContent;
    if (!okText.includes("41%") || !okText.includes("0.82×") || !okText.includes("Fri 2:00 PM") || !okText.includes("8%")) {{
      throw new Error("current row missing fields: " + okText);
    }}
    const allNodes = (n) => [n, ...(n.children || []).filter((c) => typeof c === "object").flatMap(allNodes)];
    if (!allNodes(okRow).some((n) => n.className.includes("qt75"))) throw new Error("missing 75% tick");

    // Login card with line_hit shows the owner-only override button and escapes hostile labels as text
    const card = H.loginCard({{
      bucket: "claude", provider: "claude", provider_label: "<b>Claude</b>", label: "<img onerror=alert(1)>",
      estimated: false, excluded: false, pace: 1.4, limited: false,
      line_hit: {{ window: "five_hour", label: "5-hour limit", used: 0.82, reset: "Fri 3:00 PM" }},
      override: null, calls: 5, tokens: 1200,
      windows: [{{ window: "five_hour", label: "5-hour limit", share: 0.82, pace: 1.4, reset: "Fri 3:00 PM", stale: false, bosun_share: 0.1 }}],
    }}, false);
    const cardText = card.textContent;
    if (!cardText.includes("Let background work go past 75% until it resets")) throw new Error("missing override button");
    if (!cardText.includes("<b>Claude</b>")) throw new Error("provider label should be plain text");

    // Retired-budgets banner has a Dismiss button
    const ret = H.retiredBanner({{ daily_usd: 4.0 }});
    if (!ret.textContent.includes("retired") || !ret.textContent.includes("Dismiss")) throw new Error("bad retired banner: " + ret.textContent);

    // Routing panel renders mode switch and per-job pins
    const rpanel = H.routingPanel({{ mode: "default", order: ["claude", "codex"], pins: {{ fix: "claude" }}, job_modes: {{}}, jobs: ["summarize", "explain", "fix"] }},
      [{{ bucket: "claude", provider_label: "Claude Code", label: "Default" }}, {{ bucket: "codex", provider_label: "Codex", label: "Default" }}], false);
    if (!rpanel.textContent.includes("Balance by pace") || !rpanel.textContent.includes("Branch + test")) throw new Error("routing panel missing controls");

    console.log("OK");
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert r.stdout.strip() == "OK"
