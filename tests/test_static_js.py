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
      bucket: "claude", provider: "claude", installed: true, provider_label: "<b>Claude</b>", label: "<img onerror=alert(1)>",
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
      [{{ bucket: "claude", installed: true, provider_label: "Claude Code", label: "Default" }}, {{ bucket: "codex", installed: true, provider_label: "Codex", label: "Default" }}, {{ bucket: "gone", installed: false, provider_label: "Gone CLI", label: "Default" }}], false);
    if (!rpanel.textContent.includes("Balance by pace") || !rpanel.textContent.includes("Branch + test")) throw new Error("routing panel missing controls");
    if (rpanel.textContent.includes("Gone CLI")) throw new Error("an uninstalled login must not be offered for routing");

    console.log("OK");
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert r.stdout.strip() == "OK"


def test_routing_and_override_controls_offer_only_installed_logins():
    js = (STATIC / "usage.js").read_text()
    panel = js[js.index("function routingPanel"):]
    assert "logins = logins.filter((lg) => lg.installed);" in panel[:400]
    assert "} else if (lg.line_hit && lg.installed) {" in js


def test_triage_card_renders_cause_evidence_step_and_never_uses_innerhtml():
    import json
    import shutil
    import subprocess
    import pytest

    src = (STATIC / "runs.js").read_text(encoding="utf-8")
    assert "innerHTML" not in code_only(src)

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    ui_js = str(STATIC / "ui.js")
    runs_js = str(STATIC / "runs.js")
    script = f"""
    const fs = require("fs");
    const byId = new Map();
    function mkNode(tag) {{
      const n = {{
        tagName: tag.toUpperCase(), attrs: {{}}, style: {{}}, children: [], listeners: {{}},
        _text: "", className: "", hidden: false, open: false, disabled: false, value: "", checked: false,
        id: "", href: "", scrolled: false,
        get textContent() {{ return this._text || this.children.map((c) => typeof c === "string" ? c : c.textContent).join(""); }},
        set textContent(v) {{ this._text = String(v); this.children = []; }},
        setAttribute(k, v) {{
          this.attrs[k] = String(v);
          if (k === "id") {{ this.id = String(v); byId.set(this.id, this); }}
          if (k === "href") this.href = String(v);
        }},
        getAttribute(k) {{ return this.attrs[k] ?? null; }},
        addEventListener(ev, fn) {{ (this.listeners[ev] = this.listeners[ev] || []).push(fn); }},
        append(...kids) {{ for (const k of kids) this.children.push(k); }},
        replaceChildren(...kids) {{ this.children = [...kids]; }},
        scrollIntoView() {{ this.scrolled = true; }},
        showModal() {{ this.open = true; }},
        close() {{ this.open = false; }},
        remove() {{}},
      }};
      return n;
    }}
    globalThis.window = globalThis;
    globalThis.document = {{
      body: mkNode("body"),
      querySelector: () => ({{ content: "tok" }}),
      getElementById: (id) => byId.get(id) || null,
      createElement: (tag) => mkNode(tag),
      createElementNS: (_ns, tag) => mkNode(tag),
    }};
    eval(fs.readFileSync({json.dumps(ui_js)}, "utf8"));
    MC.route = {{ parse: () => ({{ page: "trace" }}) }};
    MC.render = () => {{}};
    MC.nameOf = (_S, k) => k.split(":").pop();
    MC.ago = () => "1m ago";
    MC.jobLabel = (j) => j.status;
    MC.ask = () => {{}};
    MC.banner = () => {{}};
    MC.actions = {{ upgrade: () => {{}}, fix: () => {{}}, recheckProject: () => {{}}, copyPrompt: () => {{}} }};
    eval(fs.readFileSync({json.dumps(runs_js)}, "utf8"));

    const H = MC.triageHelpers;
    if (!H) throw new Error("MC.triageHelpers missing");
    const allNodes = (n) => [n, ...(n.children || []).filter((c) => c && typeof c === "object").flatMap(allNodes)];

    const spanNode = mkNode("details");
    spanNode.setAttribute("id", "span-a1b2c3d4");
    spanNode.open = false;

    const trace = {{
      run: {{ trace_id: "abcdef12", kind: "upgrade", subject: "brew:jq", status: "failed", started: 1700000000 }},
      spans: [{{ span_id: "a1b2c3d4", kind: "subprocess", name: "brew", status: "error", attrs: {{}} }}],
      failures: [],
      jobs: [],
      fixes: [],
    }};
    const rulesTriage = {{
      status: "done",
      source: "rules",
      feedback: "",
      verdict: {{
        category: "permissions",
        cause: "Folder <script>alert(1)</script> not writable.",
        confidence: "high",
        evidence: [
          {{ source: "output", ref: "a1b2c3d4", line: "Permission denied @ <script>x</script>" }},
          {{ source: "output", ref: "", line: "Fallback line with no span" }},
        ],
        next_step: {{ kind: "runbook", recipe: null, runbook: "homebrew-permissions", why: "Fix ownership." }},
        needs_person: true,
        transient: false,
      }},
    }};
    const card = H.verdictCard(rulesTriage, trace, {{}});
    const text = card.textContent;
    if (!text.includes("Folder <script>alert(1)</script> not writable.")) throw new Error("cause not escaped as text: " + text);
    if (!text.includes("High confidence")) throw new Error("missing confidence: " + text);
    if (!text.includes("Permission denied @ <script>x</script>")) throw new Error("missing evidence: " + text);
    if (!text.includes("Rules")) throw new Error("missing provenance Rules: " + text);
    if (!text.includes("Helpful") || !text.includes("Wrong")) throw new Error("missing feedback buttons: " + text);
    if (!text.includes("Open guide")) throw new Error("missing runbook button: " + text);

    // Evidence with a real span id links to #span-<span_id> and opens + scrolls to the span node
    const evLinks = allNodes(card).filter((n) => n.tagName === "A" && n.className.includes("triage-evref"));
    if (evLinks.length !== 1) throw new Error("expected 1 evidence link (empty ref must not be a link), got " + evLinks.length);
    if (evLinks[0].href !== "#span-a1b2c3d4") throw new Error("wrong evidence href: " + evLinks[0].href);
    (evLinks[0].listeners.click || []).forEach((fn) => fn({{ preventDefault() {{}} }}));
    if (!spanNode.open || !spanNode.scrolled) throw new Error("clicking evidence link did not open and scroll to span");

    const cacheTriage = {{
      status: "disputed",
      source: "cache",
      same_as: 1,
      same_as_run: {{ trace_id: "11223344", kind: "upgrade", subject: "brew:jq", started: 1700000000 }},
      feedback: "disputed",
      verdict: {{
        category: "network",
        cause: "Registry timed out.",
        confidence: "medium",
        evidence: [],
        next_step: {{ kind: "retry", recipe: null, runbook: null, why: "Try again." }},
        needs_person: false,
        transient: true,
      }},
    }};
    const cacheCard = H.verdictCard(cacheTriage, trace, {{}});
    if (!cacheCard.textContent.includes("Same as") || !cacheCard.textContent.includes("Try again")) {{
      throw new Error("cache card missing provenance or retry: " + cacheCard.textContent);
    }}
    if (!cacheCard.textContent.includes("Disputed")) throw new Error("missing disputed marker: " + cacheCard.textContent);

    const fixedCard = H.verdictCard({{ status: "fixed_since", source: "", verdict: null }}, trace, {{}});
    if (!fixedCard.textContent.includes("Fixed since")) throw new Error("missing fixed_since text: " + fixedCard.textContent);

    const rb = H.renderRunbookMarkdown("# Title\\n\\nParagraph with <script>bad</script>\\n\\n```\\nbrew doctor\\n```\\n\\n| A | B |\\n|---|---|\\n| 1 | 2 |");
    const rbText = rb.textContent;
    if (!rbText.includes("Title") || !rbText.includes("brew doctor") || !rbText.includes("<script>bad</script>")) {{
      throw new Error("bad runbook render: " + rbText);
    }}

    console.log("OK");
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert r.stdout.strip() == "OK"


def test_triage_retry_uses_fix_item_keys_and_omits_button_for_non_retryable_kinds():
    import json
    import shutil
    import subprocess
    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    ui_js = str(STATIC / "ui.js")
    runs_js = str(STATIC / "runs.js")
    script = f"""
    const fs = require("fs");
    function mkNode(tag) {{
      return {{
        tagName: tag.toUpperCase(), attrs: {{}}, style: {{}}, children: [], listeners: {{}},
        _text: "", className: "",
        get textContent() {{ return this._text || this.children.map((c) => typeof c === "string" ? c : c.textContent).join(""); }},
        set textContent(v) {{ this._text = String(v); this.children = []; }},
        setAttribute(k, v) {{ this.attrs[k] = String(v); }},
        getAttribute(k) {{ return this.attrs[k] ?? null; }},
        addEventListener(ev, fn) {{ (this.listeners[ev] = this.listeners[ev] || []).push(fn); }},
        append(...kids) {{ for (const k of kids) this.children.push(k); }},
      }};
    }}
    globalThis.window = globalThis;
    globalThis.document = {{
      body: mkNode("body"),
      querySelector: () => ({{ content: "tok" }}),
      getElementById: () => null,
      createElement: (tag) => mkNode(tag),
      createElementNS: (_ns, tag) => mkNode(tag),
    }};
    eval(fs.readFileSync({json.dumps(ui_js)}, "utf8"));
    const apiCalls = [];
    MC.api = async (path, opts) => {{ apiCalls.push({{ path, opts }}); return {{}}; }};
    const fixCalls = [];
    MC.route = {{ parse: () => ({{ page: "trace" }}) }};
    MC.render = () => {{}};
    MC.nameOf = (_S, k) => k;
    MC.ago = () => "1m ago";
    MC.jobLabel = (j) => j.status;
    MC.ask = () => {{}};
    MC.banner = () => {{}};
    MC.actions = {{ upgrade: () => {{}}, fix: (keys) => fixCalls.push(keys), recheckProject: () => {{}}, copyPrompt: () => {{}} }};
    eval(fs.readFileSync({json.dumps(runs_js)}, "utf8"));

    const H = MC.triageHelpers;
    const allNodes = (n) => [n, ...(n.children || []).filter((c) => c && typeof c === "object").flatMap(allNodes)];
    const retryBtn = (card) => allNodes(card).find((n) => n.tagName === "BUTTON" && n.textContent === "Try again");
    const retryTriage = (extra = {{}}) => ({{
      status: "done", source: "rules", feedback: "", ...extra,
      verdict: {{
        category: "network", cause: "Registry timed out.", confidence: "high", evidence: [],
        next_step: {{ kind: "retry", recipe: null, runbook: null, why: "Try again." }},
        needs_person: false, transient: true,
      }},
    }});

    // 1. Single-item failed fix passes the item key from trace.fixes, not the project name
    const fixTrace = {{
      run: {{ trace_id: "fix00001", kind: "fix", subject: "orchard-api", status: "failed", started: 1700000000 }},
      spans: [], failures: [], jobs: [],
      fixes: [{{ id: 1, trace_id: "fix00001", item_key: "repo:npm:lodash@orchard-api", result: {{ keys: ["repo:npm:lodash@orchard-api"] }} }}],
    }};
    const fixCard = H.verdictCard(retryTriage(), fixTrace, {{}});
    const btn1 = retryBtn(fixCard);
    if (!btn1) throw new Error("expected Try again button for fix with item key");
    (btn1.listeners.click || []).forEach((fn) => fn());
    if (JSON.stringify(fixCalls[0]) !== JSON.stringify(["repo:npm:lodash@orchard-api"])) {{
      throw new Error("fix retry sent wrong keys: " + JSON.stringify(fixCalls[0]));
    }}

    // 2. Batch fix passes all item keys from result.keys (never batch:proj)
    const batchTrace = {{
      run: {{ trace_id: "fix00002", kind: "fix", subject: "orchard-api", status: "failed", started: 1700000000 }},
      spans: [], failures: [], jobs: [],
      fixes: [{{ id: 2, trace_id: "fix00002", item_key: "batch:orchard-api", result: {{ keys: ["repo:npm:a@orchard-api", "repo:npm:b@orchard-api"] }} }}],
    }};
    const btn2 = retryBtn(H.verdictCard(retryTriage(), batchTrace, {{}}));
    if (!btn2) throw new Error("expected Try again button for batch fix");
    (btn2.listeners.click || []).forEach((fn) => fn());
    if (JSON.stringify(fixCalls[1]) !== JSON.stringify(["repo:npm:a@orchard-api", "repo:npm:b@orchard-api"])) {{
      throw new Error("batch fix retry sent wrong keys: " + JSON.stringify(fixCalls[1]));
    }}

    // 3. Fix without item keys shows no Try again button and does not call /api/refresh
    const noKeysFix = {{
      run: {{ trace_id: "fix00003", kind: "fix", subject: "orchard-api", status: "failed", started: 1700000000 }},
      spans: [], failures: [], jobs: [], fixes: [],
    }};
    if (retryBtn(H.verdictCard(retryTriage(), noKeysFix, {{}}))) {{
      throw new Error("fix without item keys must not show Try again button");
    }}

    // 4. Non-retryable kinds show no Try again button and never fall through to /api/refresh
    for (const kind of ["action", "relaunch", "pin"]) {{
      const tr = {{
        run: {{ trace_id: "other001", kind, subject: "something", status: "failed", started: 1700000000 }},
        spans: [], failures: [], jobs: [], fixes: [],
      }};
      if (retryBtn(H.verdictCard(retryTriage(), tr, {{}}))) {{
        throw new Error(`kind ${{kind}} must not show Try again button`);
      }}
    }}
    if (apiCalls.length !== 0) throw new Error("unexpected api calls: " + JSON.stringify(apiCalls));

    // 5. Refresh kind DOES show Try again and posts to /api/refresh
    const refTrace = {{
      run: {{ trace_id: "ref00001", kind: "refresh", subject: "everything", status: "failed", started: 1700000000 }},
      spans: [], failures: [], jobs: [], fixes: [],
    }};
    const btnRef = retryBtn(H.verdictCard(retryTriage(), refTrace, {{}}));
    if (!btnRef) throw new Error("refresh should show Try again button");
    (btnRef.listeners.click || []).forEach((fn) => fn());
    if (apiCalls.length !== 1 || apiCalls[0].path !== "/api/refresh") {{
      throw new Error("refresh retry did not POST /api/refresh: " + JSON.stringify(apiCalls));
    }}

    // 6. Retry step with an attached runbook (e.g. apt-lock) shows both Try again and Open guide
    const aptTrace = {{
      run: {{ trace_id: "apt00001", kind: "upgrade", subject: "apt:curl", status: "failed", started: 1700000000 }},
      spans: [], failures: [], jobs: [], fixes: [],
    }};
    const aptTriage = {{
      status: "done", source: "rules", feedback: "",
      verdict: {{
        category: "busy_or_locked", cause: "Lock held.", confidence: "high", evidence: [],
        next_step: {{ kind: "retry", recipe: null, runbook: "apt-lock", why: "Wait and retry." }},
        needs_person: false, transient: true,
      }},
    }};
    const aptCard = H.verdictCard(aptTriage, aptTrace, {{}});
    const aptBtns = allNodes(aptCard).filter((n) => n.tagName === "BUTTON").map((n) => n.textContent);
    if (!aptBtns.includes("Try again") || !aptBtns.includes("Open guide")) {{
      throw new Error("retry+runbook must show both Try again and Open guide, got: " + JSON.stringify(aptBtns));
    }}

    console.log("OK");
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert r.stdout.strip() == "OK"

