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
    assert "/api/triage/settings" in src


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

    // Triage panel renders owner-only switch, privacy explanation, and daily limit control
    if (typeof H.triagePanel !== "function") throw new Error("MC.usageHelpers.triagePanel missing");
    const tpanel = H.triagePanel({{ auto: true, daily_limit: 10, daily_used: 3, daily_remaining: 7 }}, false);
    const ttext = tpanel.textContent;
    if (!ttext.includes("Triage failures automatically") || !ttext.includes("redacted") || !ttext.includes("3 / 10")) {{
      throw new Error("triage panel missing label, privacy text, or count: " + ttext);
    }}
    const tdemo = H.triagePanel({{ auto: true, daily_limit: 10, daily_used: 0, daily_remaining: 10 }}, true);
    const demoInputs = allNodes(tdemo).filter((n) => n.tagName === "INPUT");
    if (demoInputs.length < 2 || !demoInputs.every((n) => n.attrs.disabled !== undefined || n.disabled)) {{
      throw new Error("triage panel inputs must be disabled in demo mode");
    }}

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
        setAttribute(k, v) {{ this.attrs[k] = String(v); if (k === "disabled") this.disabled = true; if (k === "title") this.title = String(v); }},
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


def test_triage_send_feedback_posts_to_run_feedback_route():
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
        setAttribute(k, v) {{ this.attrs[k] = String(v); if (k === "disabled") this.disabled = true; if (k === "title") this.title = String(v); }},
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
    const calls = [];
    MC.api = async (path, opts) => {{
      calls.push({{ path, opts }});
      return {{ status: "disputed", feedback: JSON.parse(opts.body).feedback }};
    }};
    MC.route = {{ parse: () => ({{ page: "trace" }}) }};
    MC.render = () => {{}};
    MC.nameOf = (_S, k) => k;
    MC.ago = () => "1m ago";
    MC.jobLabel = (j) => j.status;
    MC.ask = () => {{}};
    MC.banner = () => {{}};
    MC.actions = {{ upgrade: () => {{}}, fix: () => {{}}, recheckProject: () => {{}}, copyPrompt: () => {{}} }};
    eval(fs.readFileSync({json.dumps(runs_js)}, "utf8"));

    const allNodes = (n) => [n, ...(n.children || []).filter((c) => c && typeof c === "object").flatMap(allNodes)];
    const trace = {{
      run: {{ trace_id: "deadbeef1234", kind: "upgrade", subject: "brew:jq", status: "failed", started: 1700000000 }},
      spans: [], failures: [], jobs: [], fixes: [],
    }};
    const triage = {{
      status: "done", source: "rules", feedback: "",
      verdict: {{
        category: "permissions", cause: "Folder not writable.", confidence: "high", evidence: [],
        next_step: {{ kind: "runbook", recipe: null, runbook: "homebrew-permissions", why: "Fix ownership." }},
        needs_person: true, transient: false,
      }},
    }};
    const card = MC.triageHelpers.verdictCard(triage, trace, {{}});
    const btns = allNodes(card).filter((n) => n.tagName === "BUTTON");
    const helpful = btns.find((b) => b.textContent === "Helpful");
    const wrong = btns.find((b) => b.textContent === "Wrong");
    if (!helpful || !wrong) throw new Error("missing feedback buttons");

    (async () => {{
      for (const fn of helpful.listeners.click || []) await fn();
      for (const fn of wrong.listeners.click || []) await fn();
      if (calls.length !== 2) throw new Error("expected 2 api calls, got " + calls.length);
      if (calls[0].path !== "/api/runs/deadbeef1234/triage/feedback" || calls[0].opts.method !== "POST") {{
        throw new Error("wrong helpful request: " + JSON.stringify(calls[0]));
      }}
      if (JSON.stringify(JSON.parse(calls[0].opts.body)) !== JSON.stringify({{ feedback: "helpful" }})) {{
        throw new Error("wrong helpful body: " + calls[0].opts.body);
      }}
      if (calls[1].path !== "/api/runs/deadbeef1234/triage/feedback" || calls[1].opts.method !== "POST") {{
        throw new Error("wrong feedback request: " + JSON.stringify(calls[1]));
      }}
      if (JSON.stringify(JSON.parse(calls[1].opts.body)) !== JSON.stringify({{ feedback: "wrong" }})) {{
        throw new Error("wrong feedback body: " + calls[1].opts.body);
      }}
      console.log("OK");
    }})().catch((e) => {{ console.error(e); process.exit(1); }});
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert r.stdout.strip() == "OK"


def test_triage_buttons_look_deeper_disabled_provenance_and_related_seen():
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
        _text: "", className: "", disabled: false, title: "",
        get textContent() {{ return this._text || this.children.map((c) => typeof c === "string" ? c : c.textContent).join(""); }},
        set textContent(v) {{ this._text = String(v); this.children = []; }},
        setAttribute(k, v) {{ this.attrs[k] = String(v); if (k === "disabled") this.disabled = true; if (k === "title") this.title = String(v); }},
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
    const calls = [];
    MC.api = async (path, opts) => {{
      calls.push({{ path, opts }});
      return {{
        status: "done", source: "model", feedback: "",
        model_info: {{ provider: "codex", provider_label: "Codex", account: "work", login_label: "work", model: "gpt-5.6-terra", tier: "strong" }},
        verdict: {{
          category: "unknown", cause: "Unclear exit 1.", confidence: "low", evidence: [],
          next_step: {{ kind: "none", recipe: null, runbook: null, why: "Check output." }},
          needs_person: false, transient: false,
        }},
      }};
    }};
    MC.route = {{ parse: () => ({{ page: "trace" }}) }};
    MC.render = () => {{}};
    MC.nameOf = (_S, k) => k;
    MC.ago = () => "1m ago";
    MC.jobLabel = (j) => j.status;
    MC.ask = () => {{}};
    MC.banner = () => {{}};
    MC.actions = {{ upgrade: () => {{}}, fix: () => {{}}, recheckProject: () => {{}}, copyPrompt: () => {{}} }};
    eval(fs.readFileSync({json.dumps(runs_js)}, "utf8"));

    const H = MC.triageHelpers;
    if (!H.triageAction) throw new Error("missing MC.triageHelpers.triageAction");
    const allNodes = (n) => n ? [n, ...(n.children || []).filter((c) => c && typeof c === "object").flatMap(allNodes)] : [];

    // 1. Triage button when no verdict or failed/skipped, wired to POST /api/runs/<id>/triage
    const baseTrace = {{
      run: {{ trace_id: "cafe0001", kind: "upgrade", subject: "brew:jq", status: "failed", started: 1700500000 }},
      spans: [], failures: [], jobs: [], fixes: [], triage: null,
      triage_available: true, triage_unavailable_reason: "",
      related_failures: [
        {{ trace_id: "prev1", started: 1700000000 }},
        {{ trace_id: "prev2", started: 1700200000 }},
        {{ trace_id: "prev3", started: 1700300000 }},
      ],
    }};
    const tb = H.triageAction(baseTrace);
    const triBtn = allNodes(tb).filter((n) => n.tagName === "BUTTON").find((b) => b.textContent === "Triage");
    if (!triBtn || triBtn.disabled) throw new Error("expected enabled Triage button");

    // 2. Disabled state when triage_available is false names why
    const disabledTrace = {{ ...baseTrace, triage_available: false, triage_unavailable_reason: "No locked-down AI CLI installed" }};
    const tbOff = H.triageAction(disabledTrace);
    const offBtn = allNodes(tbOff).filter((n) => n.tagName === "BUTTON").find((b) => b.textContent === "Triage");
    if (!offBtn || !offBtn.disabled) throw new Error("expected disabled Triage button");
    if (!tbOff.textContent.includes("No locked-down AI CLI installed") && !offBtn.title.includes("No locked-down AI CLI installed")) {{
      throw new Error("disabled Triage button must explain why");
    }}

    // 3. Look deeper button appears on rules and cache verdicts (and not on model verdicts)
    const rulesTriage = {{
      status: "done", source: "rules", feedback: "",
      verdict: {{
        category: "permissions", cause: "Not writable.", confidence: "high", evidence: [],
        next_step: {{ kind: "runbook", recipe: null, runbook: "homebrew-permissions", why: "Fix it." }},
        needs_person: true, transient: false,
      }},
    }};
    const rulesCard = H.verdictCard(rulesTriage, baseTrace, {{}});
    const lookBtn = allNodes(rulesCard).filter((n) => n.tagName === "BUTTON").find((b) => b.textContent === "Look deeper");
    if (!lookBtn || lookBtn.disabled) throw new Error("expected enabled Look deeper button on rules verdict");
    if (!rulesCard.textContent.includes("seen 4 times since")) {{
      throw new Error("expected related_failures count in card: " + rulesCard.textContent);
    }}

    // 4. Model verdict shows provenance ("Codex, work login, strong model") and "Not sure" for unknown
    const modelTriage = {{
      status: "done", source: "model", feedback: "",
      model_info: {{ provider: "codex", provider_label: "Codex", account: "work", login_label: "work", model: "gpt-5.6-terra", tier: "strong" }},
      verdict: {{
        category: "unknown", cause: "Unclear failure.", confidence: "low", evidence: [],
        next_step: {{ kind: "none", recipe: null, runbook: null, why: "Inspect output." }},
        needs_person: false, transient: false,
      }},
    }};
    const modelCard = H.verdictCard(modelTriage, baseTrace, {{}});
    if (!modelCard.textContent.includes("Not sure")) throw new Error("unknown category must show Not sure heading");
    if (!modelCard.textContent.includes("Codex, work login, strong model")) {{
      throw new Error("expected model provenance, got: " + modelCard.textContent);
    }}
    if (allNodes(modelCard).some((n) => n.tagName === "BUTTON" && n.textContent === "Look deeper")) {{
      throw new Error("model verdict should not show Look deeper");
    }}

    // 5. Failed Look deeper keeps showing earlier rules verdict with a note explaining the failure
    const rulesWithErr = {{ ...rulesTriage, last_error: "Claude timed out" }};
    const errCard = H.verdictCard(rulesWithErr, baseTrace, {{}});
    if (!errCard.textContent.includes("Not writable.") || !errCard.textContent.includes("Look deeper failed: Claude timed out")) {{
      throw new Error("failed Look deeper must keep rules verdict and note failure: " + errCard.textContent);
    }}

    // 6. Copy for an agent appends Bosun's triage (may be wrong) as quoted data
    const handoffText = H.agentText({{ ...baseTrace, triage: rulesTriage }});
    if (!handoffText.includes("Bosun's triage (may be wrong") || !handoffText.includes('"Not writable."')) {{
      throw new Error("agentText missing quoted triage verdict: " + handoffText);
    }}

    (async () => {{
      for (const fn of triBtn.listeners.click || []) await fn();
      for (const fn of lookBtn.listeners.click || []) await fn();
      if (calls.length !== 2) throw new Error("expected 2 triage calls, got " + calls.length);
      if (calls[0].path !== "/api/runs/cafe0001/triage" || calls[0].opts.method !== "POST") {{
        throw new Error("bad Triage call: " + JSON.stringify(calls[0]));
      }}
      if (JSON.parse(calls[0].opts.body).deep !== false) throw new Error("Triage should send deep:false");
      if (calls[1].path !== "/api/runs/cafe0001/triage" || JSON.parse(calls[1].opts.body).deep !== true) {{
        throw new Error("Look deeper must send deep:true: " + JSON.stringify(calls[1]));
      }}
      console.log("OK");
    }})().catch((e) => {{ console.error(e); process.exit(1); }});
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert r.stdout.strip() == "OK"


def test_today_shows_verdict_cause_step_button_and_rescan_fallback_without_innerhtml():
    import json
    import shutil
    import subprocess
    import pytest

    src = (STATIC / "today.js").read_text(encoding="utf-8")
    assert "innerHTML" not in code_only(src)

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    ui_js = str(STATIC / "ui.js")
    today_js = str(STATIC / "today.js")
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
    globalThis.location = {{ hash: "#/" }};
    globalThis.document = {{
      querySelector: () => ({{ content: "tok" }}),
      createElement: (tag) => mkNode(tag),
      createElementNS: (_ns, tag) => mkNode(tag),
    }};
    eval(fs.readFileSync({json.dumps(ui_js)}, "utf8"));
    let openedRunbook = "", rendered = 0;
    const apiCalls = [];
    MC.api = async (path, opts) => {{
      apiCalls.push({{ path, opts }});
      if (path === "/api/terminal/run") throw new Error("recipe vanished");
      return {{}};
    }};
    MC.render = () => {{ rendered += 1; }};
    MC.banner = () => {{}};
    MC.runHref = (id) => "#/runs/" + id;
    MC.triageHelpers = {{ openRunbook: (topic) => {{ openedRunbook = topic; }} }};
    MC.actions = {{ open: () => {{}}, upgrade: () => {{}}, upgradeKeys: () => {{}}, copyPrompt: () => {{}}, relaunch: () => {{}}, openUpdates: () => {{}}, showActivity: () => {{}}, setAutopilot: () => {{}} }};
    eval(fs.readFileSync({json.dumps(today_js)}, "utf8"));

    if (!MC.todayHelpers) throw new Error("MC.todayHelpers missing");
    const mkPkg = (key, name) => ({{
      key, name, installed: "1.0", available: "2.0", lane: "now", risk: 8, kev: false,
      members: [{{ key, name, installed: "1.0", available: "2.0", lane: "now", risk: 8, source: "brew", upgradable: true }}],
    }});
    const S = {{ snapTs: 100, autopilot: {{ on: false, queued: [], waiting: [] }}, jobs: [] }};

    // 1. Runbook verdict shows cause (escaped as text) and Open guide button
    S.jobs = [{{
      id: 1, item_key: "brew:jq", status: "failed", finished: 200, trace_id: "aa11bb22", hint: "See the output",
      triage: {{
        status: "done", source: "rules", category: "permissions",
        cause: "Folder <img onerror=alert(1)> not writable.", confidence: "high",
        next_step: {{ kind: "runbook", recipe: null, runbook: "homebrew-permissions", why: "Fix ownership." }},
      }},
    }}];
    const e1 = MC.todayHelpers.machineEntry(S, mkPkg("brew:jq", "jq"));
    if (e1.line !== "Folder <img onerror=alert(1)> not writable." || e1.verb !== "Open guide") {{
      throw new Error("bad runbook entry: " + JSON.stringify(e1));
    }}
    e1.act();
    if (openedRunbook !== "homebrew-permissions") throw new Error("expected openRunbook call");

    // 2. None verdict shows "Nothing to do" status with no action button
    S.jobs = [{{
      id: 2, item_key: "brew:rg", status: "failed", finished: 200, trace_id: "bb22cc33",
      triage: {{
        status: "done", source: "rules", category: "already_current",
        cause: "App updates itself.", confidence: "high",
        next_step: {{ kind: "none", recipe: null, runbook: null, why: "Nothing to do." }},
      }},
    }}];
    const e2 = MC.todayHelpers.machineEntry(S, mkPkg("brew:rg", "rg"));
    if (e2.line !== "App updates itself." || e2.verb || e2.status !== "Nothing to do") {{
      throw new Error("bad none entry: " + JSON.stringify(e2));
    }}

    // 3. Recipe verdict whose action refuses switches the card to Re-scan
    S.jobs = [{{
      id: 3, item_key: "apt:curl", status: "failed", finished: 200, trace_id: "cc33dd44",
      triage: {{
        status: "done", source: "rules", category: "busy_or_locked",
        cause: "dpkg was interrupted.", confidence: "high",
        next_step: {{ kind: "recipe", recipe: "dpkg-configure", runbook: null, why: "Finish dpkg." }},
      }},
    }}];
    const e3 = MC.todayHelpers.machineEntry(S, mkPkg("apt:curl", "curl"));
    if (e3.verb !== "Run fix") throw new Error("expected Run fix, got " + e3.verb);
    (async () => {{
      await e3.act();
      const after = MC.todayHelpers.machineEntry(S, mkPkg("apt:curl", "curl"));
      if (after.verb !== "Re-scan" || !after.line.includes("Re-scan")) {{
        throw new Error("expected Re-scan fallback after refusal, got: " + JSON.stringify(after));
      }}
      console.log("OK");
    }})().catch((err) => {{ console.error(err); process.exit(1); }});
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert r.stdout.strip() == "OK"


def test_usage_triage_switch_refetches_and_restores_server_state_on_failed_save():
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
      return {{
        tagName: tag.toUpperCase(), attrs: {{}}, style: {{}}, children: [], listeners: {{}},
        _text: "", className: "", hidden: false, open: false, disabled: false, value: "", checked: false,
        get textContent() {{ return this._text || this.children.map((c) => typeof c === "string" ? c : c.textContent).join(""); }},
        set textContent(v) {{ this._text = String(v); this.children = []; }},
        setAttribute(k, v) {{
          this.attrs[k] = String(v);
          if (k === "value") this.value = String(v);
          if (k === "checked") this.checked = true;
        }},
        getAttribute(k) {{ return this.attrs[k] ?? null; }},
        addEventListener(ev, fn) {{ (this.listeners[ev] = this.listeners[ev] || []).push(fn); }},
        append(...kids) {{ for (const k of kids) this.children.push(k); }},
      }};
    }}
    globalThis.window = globalThis;
    const alerts = [];
    globalThis.alert = (msg) => alerts.push(msg);
    globalThis.document = {{
      querySelector: () => ({{ content: "tok" }}),
      createElement: (tag) => mkNode(tag),
      createElementNS: (_ns, tag) => mkNode(tag),
    }};
    eval(fs.readFileSync({json.dumps(ui_js)}, "utf8"));
    let usageFetches = 0;
    const serverUsage = () => ({{
      days: 30, demo: false, logins: [], top_jobs: [], routing: {{ mode: "default", order: [], pins: {{}}, jobs: [] }},
      notices: [], budgets_retired: null,
      triage: {{ auto: true, daily_limit: 10, daily_used: 1, daily_remaining: 9 }},
      detail: {{ days: 30, totals: {{}}, cache: {{}}, by_day: [], by_purpose: [], by_model: [], by_source: [], top_items: [], outliers: [], top_calls: [] }},
    }});
    MC.api = async (path) => {{
      if (path.startsWith("/api/usage")) {{ usageFetches += 1; return serverUsage(); }}
      if (path === "/api/triage/settings") throw new Error("server rejected");
      return {{}};
    }};
    let renders = 0;
    let lastTree = null;
    const S = {{ agents: {{ system: false, external: false }} }};
    MC.route = {{ parse: () => ({{ page: "usage" }}) }};
    MC.nameOf = (_S, k) => k;
    MC.runHref = (id) => "#/runs/" + id;
    MC.ago = () => "1m ago";
    MC.render = () => {{ renders += 1; lastTree = MC.views.usage(S); }};
    eval(fs.readFileSync({json.dumps(usage_js)}, "utf8"));

    const allNodes = (n) => n ? [n, ...(n.children || []).filter((c) => c && typeof c === "object").flatMap(allNodes)] : [];
    (async () => {{
      MC.views.usage(S);
      await new Promise((r) => setTimeout(r, 10));
      if (usageFetches !== 1 || !lastTree) throw new Error("initial usage fetch did not render");
      const findBox = (tree) => allNodes(tree).find((n) => n.tagName === "INPUT" && n.attrs["aria-label"] === "Triage failures automatically");
      const box = findBox(lastTree);
      if (!box || !box.checked) throw new Error("initial switch must be checked");

      // User unchecks the box, but POST /api/triage/settings fails
      box.checked = false;
      const rendersBefore = renders;
      for (const fn of box.listeners.change || []) await fn();
      await new Promise((r) => setTimeout(r, 10));

      if (alerts.length !== 1 || !alerts[0].includes("server rejected")) {{
        throw new Error("expected alert on failed save, got: " + JSON.stringify(alerts));
      }}
      if (usageFetches !== 2) {{
        throw new Error("expected fetchUsage(true) after failed save, got usageFetches=" + usageFetches);
      }}
      if (renders <= rendersBefore) {{
        throw new Error("expected re-render after failed save");
      }}
      const refreshedBox = findBox(lastTree);
      if (!refreshedBox || !refreshedBox.checked) {{
        throw new Error("switch did not revert to server value (auto=true) after failed save");
      }}
      console.log("OK");
    }})().catch((err) => {{ console.error(err); process.exit(1); }});
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert r.stdout.strip() == "OK"



def test_runs_view_renders_actor_label_as_text():
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
    let lastTree = null;
    MC.api = async (path) => {{
      if (path.startsWith("/api/runs?")) {{
        return [{{
          trace_id: "aabbccdd1122", kind: "action", subject: "start_fix", status: "ok",
          actor: "automation:7", actor_label: "Automation: <img onerror=alert(1)>", started: 1700000000, duration_ms: 120,
        }}];
      }}
      return {{}};
    }};
    MC.route = {{ parse: () => ({{ page: "runs" }}) }};
    MC.nameOf = (_S, k) => k;
    MC.ago = () => "1m ago";
    MC.jobLabel = (j) => j.status;
    MC.ask = () => {{}};
    MC.banner = () => {{}};
    MC.actions = {{ upgrade: () => {{}}, fix: () => {{}}, recheckProject: () => {{}}, copyPrompt: () => {{}} }};
    MC.render = () => {{ lastTree = MC.views.runs({{}}); }};
    eval(fs.readFileSync({json.dumps(runs_js)}, "utf8"));

    (async () => {{
      MC.views.runs({{}});
      await new Promise((r) => setTimeout(r, 10));
      if (!lastTree || !lastTree.textContent.includes("Automation: <img onerror=alert(1)>")) {{
        throw new Error("runs list did not render actor_label as plain text: " + (lastTree && lastTree.textContent));
      }}
      console.log("OK");
    }})().catch((e) => {{ console.error(e); process.exit(1); }});
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert r.stdout.strip() == "OK"


def test_automations_route_sidebar_developer_only_and_no_innerhtml():
    import json
    import shutil
    import subprocess
    import pytest

    index_html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '/static/automations.css' in index_html
    assert '/static/automations.js' in index_html
    auto_js_path = STATIC / "automations.js"
    assert auto_js_path.is_file()
    assert "innerHTML" not in code_only(auto_js_path.read_text(encoding="utf-8"))

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    ui_js = str(STATIC / "ui.js")
    nav_js = str(STATIC / "nav.js")
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
    globalThis.location = {{ hash: "#/automations" }};
    globalThis.document = {{
      querySelector: () => ({{ content: "tok" }}),
      createElement: (tag) => mkNode(tag),
      createElementNS: (_ns, tag) => mkNode(tag),
    }};
    eval(fs.readFileSync({json.dumps(ui_js)}, "utf8"));
    const parsed = MC.route.parse();
    if (parsed.page !== "automations") throw new Error("expected page automations, got " + JSON.stringify(parsed));

    MC.needsYou = () => [];
    MC.actions = {{ setView: () => {{}}, showIgnored: () => {{}} }};
    eval(fs.readFileSync({json.dumps(nav_js)}, "utf8"));
    const scopes = {{
      pc: {{ id: "pc", kind: "pc", sum: {{ counts: {{ now: 0, plan: 0 }}, total: 0, kev: 0 }} }},
      active: [], dormant: [], ignoredProjects: [],
    }};
    const S = {{ dormantOpen: false, showIgnored: false }};

    MC.SIMPLE = false;
    const devText = MC.views.nav(S, scopes, {{ page: "automations" }}).filter(Boolean).map((n) => n.textContent).join(" ");
    if (!devText.includes("Automations")) throw new Error("developer nav missing Automations: " + devText);

    MC.SIMPLE = true;
    const simpleText = MC.views.nav(S, scopes, {{ page: "today" }}).filter(Boolean).map((n) => n.textContent).join(" ");
    if (simpleText.includes("Automations")) throw new Error("simple nav must hide Automations: " + simpleText);

    console.log("OK");
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert r.stdout.strip() == "OK"


def test_automations_page_helpers_render_kill_switch_rules_renewal_history_and_escape():
    import json
    import shutil
    import subprocess
    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    ui_js = str(STATIC / "ui.js")
    auto_js = str(STATIC / "automations.js")
    script = f"""
    const fs = require("fs");
    function mkNode(tag) {{
      return {{
        tagName: tag.toUpperCase(), attrs: {{}}, style: {{}}, children: [], listeners: {{}},
        _text: "", className: "", hidden: false, open: false, disabled: false, value: "", checked: false, href: "",
        get textContent() {{ return this._text || this.children.map((c) => typeof c === "string" ? c : c.textContent).join(""); }},
        set textContent(v) {{ this._text = String(v); this.children = []; }},
        setAttribute(k, v) {{
          this.attrs[k] = String(v);
          if (k === "disabled") this.disabled = true;
          if (k === "checked") this.checked = true;
          if (k === "href") this.href = String(v);
          if (k === "value") this.value = String(v);
        }},
        getAttribute(k) {{ return this.attrs[k] ?? null; }},
        addEventListener(ev, fn) {{ (this.listeners[ev] = this.listeners[ev] || []).push(fn); }},
        append(...kids) {{ for (const k of kids) this.children.push(k); }},
      }};
    }}
    globalThis.window = globalThis;
    globalThis.alert = () => {{}};
    globalThis.confirm = () => true;
    globalThis.document = {{
      querySelector: () => ({{ content: "tok" }}),
      createElement: (tag) => mkNode(tag),
      createElementNS: (_ns, tag) => mkNode(tag),
    }};
    eval(fs.readFileSync({json.dumps(ui_js)}, "utf8"));
    const calls = [];
    MC.api = async (path, opts) => {{ calls.push({{ path, opts }}); return {{}}; }};
    MC.route = {{ parse: () => ({{ page: "automations" }}) }};
    MC.render = () => {{}};
    MC.ago = () => "5 min ago";
    MC.clock = () => "02:00";
    MC.runHref = (id) => "#/runs/" + id;
    eval(fs.readFileSync({json.dumps(auto_js)}, "utf8"));

    const H = MC.automationsHelpers;
    if (!H) throw new Error("MC.automationsHelpers missing");
    const allNodes = (n) => n ? [n, ...(n.children || []).filter((c) => c && typeof c === "object").flatMap(allNodes)] : [];

    // 1. Kill switch header: active -> Pause all automations; paused -> Resume automations; disabled in demo
    const hdrActive = H.headerBar({{ paused: false, demo: false }}, () => {{}});
    const pauseBtn = allNodes(hdrActive).find((n) => n.tagName === "BUTTON" && n.textContent === "Pause all automations");
    if (!pauseBtn || pauseBtn.disabled) throw new Error("expected enabled Pause all automations button");
    const hdrPaused = H.headerBar({{ paused: true, demo: false }}, () => {{}});
    if (!hdrPaused.textContent.includes("Resume automations")) throw new Error("expected Resume automations when paused");
    const hdrDemo = H.headerBar({{ paused: false, demo: true }}, () => {{}});
    const demoBtns = allNodes(hdrDemo).filter((n) => n.tagName === "BUTTON");
    if (!demoBtns.length || !demoBtns.every((b) => b.disabled)) throw new Error("header buttons must be disabled in demo mode");

    // 2. Rule card: sentence, mode chip, counts, renewal notice, escaping, demo disabled switch
    const rule = {{
      id: 7, name: "<b>Nightly</b>", preset: "nightly_fix", enabled: true, paused_reason: "",
      mode: "run", action: "start_fix", sentence: "Daily at 02:00, run Branch + test on App.",
      scope_sentence: "Run Branch + test on now items in p1.",
      grant_expired: true, grant_expires_at: 1700000000, needs_project: false, needs_grant: true,
      today_count: 2, max_per_day: 6, last_fired: 1700000000, next_due: 1700090000,
    }};
    const card = H.ruleCard(rule, false, () => {{}});
    const ctext = card.textContent;
    if (!ctext.includes("<b>Nightly</b>") || !ctext.includes("Daily at 02:00") || !ctext.includes("Runs on its own")) {{
      throw new Error("rule card missing escaped name, sentence, or mode: " + ctext);
    }}
    if (!ctext.includes("2 / 6") || !ctext.includes("5 min ago") || !ctext.includes("02:00")) {{
      throw new Error("rule card missing today count, last fired, or next due: " + ctext);
    }}
    if (!ctext.includes("Still want this?") || !ctext.includes("Renew")) {{
      throw new Error("expired rule card missing Still want this? / Renew: " + ctext);
    }}
    const renewBtn = allNodes(card).find((n) => n.tagName === "BUTTON" && n.textContent === "Renew");
    const demoCard = H.ruleCard(rule, true, () => {{}});
    const demoSwitch = allNodes(demoCard).find((n) => n.tagName === "INPUT" && n.attrs.type === "checkbox");
    if (!demoSwitch || !demoSwitch.disabled) throw new Error("rule switch must be disabled in demo mode");

    // 3. Firing history: event, status/reason, trace link, escaping
    const hist = H.historyPanel([{{
      id: 11, automation_id: 7, automation_name: "<script>bad()</script>", event_kind: "schedule",
      event_ref: "2026-11-27T02:00", status: "done", reason: "Fixed <img src=x>", trace_id: "deadbeef1234",
      created: 1700000000, finished: 1700000005,
    }}]);
    if (!hist.textContent.includes("<script>bad()</script>") || !hist.textContent.includes("Fixed <img src=x>")) {{
      throw new Error("history did not escape hostile name/reason: " + hist.textContent);
    }}
    const runLink = allNodes(hist).find((n) => n.tagName === "A" && n.href === "#/runs/deadbeef1234");
    if (!runLink) throw new Error("history missing #/runs/<trace_id> link");

    (async () => {{
      for (const fn of pauseBtn.listeners.click || []) await fn();
      for (const fn of renewBtn.listeners.click || []) await fn();
      if (!calls.some((c) => c.path === "/api/automations/pause" && JSON.parse(c.opts.body).on === true)) {{
        throw new Error("pause click did not POST /api/automations/pause: " + JSON.stringify(calls));
      }}
      if (!calls.some((c) => c.path === "/api/automations/7/renew" && c.opts.method === "POST")) {{
        throw new Error("renew click did not POST /api/automations/7/renew: " + JSON.stringify(calls));
      }}
      console.log("OK");
    }})().catch((e) => {{ console.error(e); process.exit(1); }});
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert r.stdout.strip() == "OK"


def test_automations_editor_dropdowns_grant_confirmation_and_system_mode():
    import json
    import shutil
    import subprocess
    import pytest

    index_html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '/static/automations_editor.js' in index_html
    editor_js_path = STATIC / "automations_editor.js"
    assert editor_js_path.is_file()
    assert "innerHTML" not in code_only(editor_js_path.read_text(encoding="utf-8"))

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    ui_js = str(STATIC / "ui.js")
    auto_js = str(STATIC / "automations.js")
    editor_js = str(editor_js_path)
    script = f"""
    const fs = require("fs");
    function mkNode(tag) {{
      return {{
        tagName: tag.toUpperCase(), attrs: {{}}, style: {{}}, children: [], listeners: {{}},
        _text: "", className: "", hidden: false, open: false, disabled: false, value: "", checked: false,
        get textContent() {{ return this._text || this.children.map((c) => typeof c === "string" ? c : c.textContent).join(""); }},
        set textContent(v) {{ this._text = String(v); this.children = []; }},
        setAttribute(k, v) {{
          this.attrs[k] = String(v);
          if (k === "disabled") this.disabled = true;
          if (k === "checked") this.checked = true;
          if (k === "value") this.value = String(v);
        }},
        getAttribute(k) {{ return this.attrs[k] ?? null; }},
        addEventListener(ev, fn) {{ (this.listeners[ev] = this.listeners[ev] || []).push(fn); }},
        append(...kids) {{ for (const k of kids) this.children.push(k); }},
        replaceChildren(...kids) {{ this.children = [...kids]; }},
      }};
    }}
    globalThis.window = globalThis;
    globalThis.alert = () => {{}};
    globalThis.document = {{
      querySelector: () => ({{ content: "tok" }}),
      createElement: (tag) => mkNode(tag),
      createElementNS: (_ns, tag) => mkNode(tag),
    }};
    eval(fs.readFileSync({json.dumps(ui_js)}, "utf8"));
    const calls = [];
    MC.api = async (path, opts) => {{ calls.push({{ path, opts }}); return {{ id: 99 }}; }};
    MC.route = {{ parse: () => ({{ page: "automations" }}) }};
    MC.render = () => {{}};
    MC.ago = () => "1m ago";
    MC.clock = () => "02:00";
    MC.runHref = (id) => "#/runs/" + id;
    eval(fs.readFileSync({json.dumps(auto_js)}, "utf8"));
    eval(fs.readFileSync({json.dumps(editor_js)}, "utf8"));

    const H = MC.automationsHelpers;
    if (!H || typeof H.editorPanel !== "function") throw new Error("MC.automationsHelpers.editorPanel missing");
    const allNodes = (n) => n ? [n, ...(n.children || []).filter((c) => c && typeof c === "object").flatMap(allNodes)] : [];
    const projects = [{{ key: "p1", name: "orchard-api", active: true }}, {{ key: "p2", name: "tidepool-web", active: true }}];

    // 1. Nightly Branch + test preset: needs project pick and INSTALL + run grant checkbox
    const nightly = {{
      id: 2, name: "Nightly Branch + test", preset: "nightly_fix", enabled: false,
      trigger: {{ kind: "schedule", time: "02:00", days: [] }},
      conditions: {{ project: "__pick_project__", lane: "now" }},
      action: "start_fix",
      scope: {{ project: "__pick_project__", lane: "now" }},
      mode: "run", wait_s: 0, max_per_day: 6, cooldown_s: 900, model_jobs_per_day: 3,
    }};
    let savedCount = 0;
    const panel = H.editorPanel(nightly, projects, false, () => {{}}, () => {{ savedCount += 1; }});
    const selects = allNodes(panel).filter((n) => n.tagName === "SELECT");
    if (selects.length < 4) throw new Error("expected dropdown selects for trigger, condition/scope, action, mode; got " + selects.length);

    const findSave = () => allNodes(panel).find((n) => n.tagName === "BUTTON" && n.textContent === "Save");
    const findByLabel = (lbl) => allNodes(panel).find((n) => n.attrs["aria-label"] === lbl);

    // Save disabled until project picked; turning on requires INSTALL grant checkbox
    if (!findSave().disabled) throw new Error("Save must be disabled before project is picked");
    const projSel = findByLabel("Project");
    if (!projSel) throw new Error("missing Project dropdown");
    projSel.value = "p1";
    (projSel.listeners.change || []).forEach((fn) => fn({{ target: projSel }}));
    const turnOnBox = findByLabel("Turn on after saving");
    if (!turnOnBox || turnOnBox.checked) throw new Error("Turn on after saving must start unchecked for disabled preset");
    turnOnBox.checked = true;
    (turnOnBox.listeners.change || []).forEach((fn) => fn({{ target: turnOnBox }}));
    if (!findSave().disabled) throw new Error("Save must stay disabled until INSTALL grant checkbox is checked");
    if (!panel.textContent.includes("Branch + test") || !panel.textContent.includes("orchard-api")) {{
      throw new Error("grant sentence missing action or project: " + panel.textContent);
    }}
    const grantBox = findByLabel("Confirm scope grant");
    if (!grantBox) throw new Error("missing Confirm scope grant checkbox");
    grantBox.checked = true;
    (grantBox.listeners.change || []).forEach((fn) => fn({{ target: grantBox }}));
    if (findSave().disabled) throw new Error("Save should be enabled after picking project and checking grant");

    // 2. SYSTEM action forbids choosing mode="run"
    const sysRule = {{
      id: null, name: "Offer relaunch", enabled: false,
      trigger: {{ kind: "refresh" }}, conditions: {{}},
      action: "relaunch_app", scope: {{ app: "chrome" }}, mode: "ask",
    }};
    const sysPanel = H.editorPanel(sysRule, projects, false, () => {{}}, () => {{}});
    const modeSel = allNodes(sysPanel).find((n) => n.attrs["aria-label"] === "Approval mode");
    if (!modeSel) throw new Error("missing Approval mode dropdown");
    const modeValues = allNodes(modeSel).filter((n) => n.tagName === "OPTION").map((o) => o.value);
    if (modeValues.includes("run") || !modeValues.includes("ask")) {{
      throw new Error("SYSTEM action must forbid mode=run, got options: " + JSON.stringify(modeValues));
    }}

    // 3. Submitting posts /api/automations with wait_s and confirm_grant (never delay_s)
    (async () => {{
      const btn = findSave();
      for (const fn of btn.listeners.click || []) await fn();
      if (calls.length !== 1 || calls[0].path !== "/api/automations" || calls[0].opts.method !== "POST") {{
        throw new Error("bad save request: " + JSON.stringify(calls));
      }}
      const sent = JSON.parse(calls[0].opts.body);
      if ("delay_s" in sent || sent.wait_s !== 0 || sent.confirm_grant !== true) {{
        throw new Error("bad save body (must use wait_s and confirm_grant): " + JSON.stringify(sent));
      }}
      if (sent.scope.project !== "p1" || sent.conditions.project !== "p1" || sent.action !== "start_fix") {{
        throw new Error("bad scope/conditions in save body: " + JSON.stringify(sent));
      }}
      if (savedCount !== 1) throw new Error("onSaved callback was not called");
      console.log("OK");
    }})().catch((e) => {{ console.error(e); process.exit(1); }});
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert r.stdout.strip() == "OK"


def test_automations_editor_preserves_enabled_state_and_has_turn_on_control():
    import json
    import shutil
    import subprocess
    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    ui_js = str(STATIC / "ui.js")
    auto_js = str(STATIC / "automations.js")
    editor_js = str(STATIC / "automations_editor.js")
    script = f"""
    const fs = require("fs");
    function mkNode(tag) {{
      return {{
        tagName: tag.toUpperCase(), attrs: {{}}, style: {{}}, children: [], listeners: {{}},
        _text: "", className: "", hidden: false, open: false, disabled: false, value: "", checked: false,
        get textContent() {{ return this._text || this.children.map((c) => typeof c === "string" ? c : c.textContent).join(""); }},
        set textContent(v) {{ this._text = String(v); this.children = []; }},
        setAttribute(k, v) {{
          this.attrs[k] = String(v);
          if (k === "disabled") this.disabled = true;
          if (k === "checked") this.checked = true;
          if (k === "value") this.value = String(v);
        }},
        removeAttribute(k) {{
          delete this.attrs[k];
          if (k === "disabled") this.disabled = false;
          if (k === "checked") this.checked = false;
        }},
        getAttribute(k) {{ return this.attrs[k] ?? null; }},
        addEventListener(ev, fn) {{ (this.listeners[ev] = this.listeners[ev] || []).push(fn); }},
        append(...kids) {{ for (const k of kids) this.children.push(k); }},
        replaceChildren(...kids) {{ this.children = [...kids]; }},
      }};
    }}
    globalThis.window = globalThis;
    globalThis.alert = () => {{}};
    globalThis.document = {{
      querySelector: () => ({{ content: "tok" }}),
      createElement: (tag) => mkNode(tag),
      createElementNS: (_ns, tag) => mkNode(tag),
    }};
    eval(fs.readFileSync({json.dumps(ui_js)}, "utf8"));
    const calls = [];
    MC.api = async (path, opts) => {{ calls.push({{ path, opts }}); return {{ id: 1 }}; }};
    MC.route = {{ parse: () => ({{ page: "automations" }}) }};
    MC.render = () => {{}};
    MC.ago = () => "1m ago";
    MC.clock = () => "02:00";
    MC.runHref = (id) => "#/runs/" + id;
    eval(fs.readFileSync({json.dumps(auto_js)}, "utf8"));
    eval(fs.readFileSync({json.dumps(editor_js)}, "utf8"));

    const H = MC.automationsHelpers;
    const allNodes = (n) => n ? [n, ...(n.children || []).filter((c) => c && typeof c === "object").flatMap(allNodes)] : [];
    const findByLabel = (root, lbl) => allNodes(root).find((n) => n.attrs["aria-label"] === lbl);
    const findSave = (root) => allNodes(root).find((n) => n.tagName === "BUTTON" && n.textContent === "Save");

    // 1. Disabled preset: "Turn on after saving" starts unchecked; renaming and saving sends enabled: false
    const disabledRule = {{
      id: 1, name: "What changed", preset: "what_changed", enabled: false,
      trigger: {{ kind: "refresh" }}, conditions: {{}},
      action: "notify", scope: {{ digest: "what_changed" }},
      mode: "run", wait_s: 0, max_per_day: 6, cooldown_s: 900, model_jobs_per_day: 3,
    }};
    const p1 = H.editorPanel(disabledRule, [], false, () => {{}}, () => {{}});
    const turnOn1 = findByLabel(p1, "Turn on after saving");
    if (!turnOn1 || turnOn1.checked) throw new Error("Turn on after saving must exist and start unchecked for disabled rule");
    const name1 = findByLabel(p1, "Automation name");
    name1.value = "Renamed digest";
    (name1.listeners.input || []).forEach((fn) => fn({{ target: name1 }}));

    // 2. Already-enabled rule: starts checked and sends enabled: true
    const enabledRule = {{ ...disabledRule, id: 2, enabled: true }};
    const p2 = H.editorPanel(enabledRule, [], false, () => {{}}, () => {{}});
    const turnOn2 = findByLabel(p2, "Turn on after saving");
    if (!turnOn2 || !turnOn2.checked) throw new Error("Turn on after saving must start checked for already-enabled rule");

    // 3. Disabled rule with "Turn on after saving" explicitly checked sends enabled: true
    const p3 = H.editorPanel(disabledRule, [], false, () => {{}}, () => {{}});
    const turnOn3 = findByLabel(p3, "Turn on after saving");
    turnOn3.checked = true;
    (turnOn3.listeners.change || []).forEach((fn) => fn({{ target: turnOn3 }}));

    (async () => {{
      for (const fn of findSave(p1).listeners.click || []) await fn();
      for (const fn of findSave(p2).listeners.click || []) await fn();
      for (const fn of findSave(p3).listeners.click || []) await fn();
      if (calls.length !== 3) throw new Error("expected 3 saves, got " + calls.length);
      const b1 = JSON.parse(calls[0].opts.body);
      const b2 = JSON.parse(calls[1].opts.body);
      const b3 = JSON.parse(calls[2].opts.body);
      if (b1.enabled !== false || b1.name !== "Renamed digest") throw new Error("disabled rule flipped on: " + JSON.stringify(b1));
      if (b2.enabled !== true) throw new Error("enabled rule flipped off: " + JSON.stringify(b2));
      if (b3.enabled !== true) throw new Error("explicitly turned-on rule stayed off: " + JSON.stringify(b3));
      console.log("OK");
    }})().catch((e) => {{ console.error(e); process.exit(1); }});
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert r.stdout.strip() == "OK"


def test_automations_editor_keeps_unsaved_edits_across_rerender():
    import json
    import shutil
    import subprocess
    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    ui_js = str(STATIC / "ui.js")
    auto_js = str(STATIC / "automations.js")
    editor_js = str(STATIC / "automations_editor.js")
    script = f"""
    const fs = require("fs");
    function mkNode(tag) {{
      return {{
        tagName: tag.toUpperCase(), attrs: {{}}, style: {{}}, children: [], listeners: {{}},
        _text: "", className: "", hidden: false, open: false, disabled: false, value: "", checked: false,
        get textContent() {{ return this._text || this.children.map((c) => typeof c === "string" ? c : c.textContent).join(""); }},
        set textContent(v) {{ this._text = String(v); this.children = []; }},
        setAttribute(k, v) {{
          this.attrs[k] = String(v);
          if (k === "disabled") this.disabled = true;
          if (k === "checked") this.checked = true;
          if (k === "value") this.value = String(v);
        }},
        removeAttribute(k) {{
          delete this.attrs[k];
          if (k === "disabled") this.disabled = false;
          if (k === "checked") this.checked = false;
        }},
        getAttribute(k) {{ return this.attrs[k] ?? null; }},
        addEventListener(ev, fn) {{ (this.listeners[ev] = this.listeners[ev] || []).push(fn); }},
        append(...kids) {{ for (const k of kids) this.children.push(k); }},
        replaceChildren(...kids) {{ this.children = [...kids]; }},
      }};
    }}
    globalThis.window = globalThis;
    globalThis.alert = () => {{}};
    globalThis.document = {{
      querySelector: () => ({{ content: "tok" }}),
      createElement: (tag) => mkNode(tag),
      createElementNS: (_ns, tag) => mkNode(tag),
    }};
    eval(fs.readFileSync({json.dumps(ui_js)}, "utf8"));
    const rules = [
      {{
        id: 1, name: "What changed", preset: "what_changed", enabled: false,
        trigger: {{ kind: "refresh" }}, conditions: {{}},
        action: "notify", scope: {{ digest: "what_changed" }},
        mode: "run", wait_s: 0, max_per_day: 6, cooldown_s: 900, model_jobs_per_day: 3,
      }},
      {{
        id: 2, name: "Nightly", preset: null, enabled: false,
        trigger: {{ kind: "schedule", time: "02:00", days: [] }}, conditions: {{}},
        action: "refresh", scope: {{ only: null }},
        mode: "run", wait_s: 0, max_per_day: 6, cooldown_s: 900, model_jobs_per_day: 3,
      }},
    ];
    MC.api = async (path) => {{
      if (path === "/api/automations") return {{ paused: false, demo: false, automations: rules.map((r) => ({{ ...r }})), projects: [] }};
      if (path.startsWith("/api/automations/firings")) return {{ firings: [] }};
      return {{ id: 1 }};
    }};
    MC.route = {{ parse: () => ({{ page: "automations" }}) }};
    let tree = null;
    MC.render = () => {{ tree = MC.views.automations(); }};
    MC.ago = () => "1m ago";
    MC.clock = () => "02:00";
    MC.runHref = (id) => "#/runs/" + id;
    eval(fs.readFileSync({json.dumps(auto_js)}, "utf8"));
    eval(fs.readFileSync({json.dumps(editor_js)}, "utf8"));

    const allNodes = (n) => n ? [n, ...(n.children || []).filter((c) => c && typeof c === "object").flatMap(allNodes)] : [];
    const findByLabel = (lbl) => allNodes(tree).find((n) => n.attrs["aria-label"] === lbl);
    const editBtns = () => allNodes(tree).filter((n) => n.tagName === "BUTTON" && n.textContent === "Edit");

    (async () => {{
      MC.render();
      await new Promise((r) => setTimeout(r, 20));

      // Open editor on rule 1 and fill fields
      const b1 = editBtns()[1]; // presets come after custom rules
      for (const fn of b1.listeners.click || []) fn();
      const nameIn = findByLabel("Automation name");
      nameIn.value = "Unsaved edit";
      (nameIn.listeners.input || []).forEach((fn) => fn({{ target: nameIn }}));
      const maxSel = findByLabel("Max per day");
      maxSel.value = "12";
      (maxSel.listeners.change || []).forEach((fn) => fn({{ target: maxSel }}));
      const turnOn = findByLabel("Turn on after saving");
      turnOn.checked = true;
      (turnOn.listeners.change || []).forEach((fn) => fn({{ target: turnOn }}));

      // Simulate 15-second background fetch + full MC.render()
      await MC.automationsHelpers.fetchAutomations(true);
      MC.render();

      const afterName = findByLabel("Automation name");
      const afterMax = findByLabel("Max per day");
      const afterTurnOn = findByLabel("Turn on after saving");
      if (!afterName || afterName.value !== "Unsaved edit") throw new Error("name wiped on re-render: " + (afterName && afterName.value));
      const selectedMax = allNodes(afterMax).find((o) => o.tagName === "OPTION" && o.attrs.selected !== undefined);
      if (!selectedMax || selectedMax.value !== "12") throw new Error("maxPerDay wiped on re-render");
      if (!afterTurnOn || !afterTurnOn.checked) throw new Error("turnOn wiped on re-render");

      // Switching to another rule drops old edits
      const b2 = editBtns()[0];
      for (const fn of b2.listeners.click || []) fn();
      if (findByLabel("Automation name").value !== "Nightly") throw new Error("switching rules did not reset state");

      // Cancel drops edits
      const name2 = findByLabel("Automation name");
      name2.value = "Temp";
      (name2.listeners.input || []).forEach((fn) => fn({{ target: name2 }}));
      const cancelBtn = allNodes(tree).find((n) => n.tagName === "BUTTON" && n.textContent === "Cancel");
      for (const fn of cancelBtn.listeners.click || []) fn();
      for (const fn of editBtns()[0].listeners.click || []) fn();
      if (findByLabel("Automation name").value !== "Nightly") throw new Error("cancel did not drop state");

      console.log("OK");
    }})().catch((e) => {{ console.error(e); process.exit(1); }});
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert r.stdout.strip() == "OK"


def test_automations_editor_name_input_keeps_same_node_while_typing():
    import json
    import shutil
    import subprocess
    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    ui_js = str(STATIC / "ui.js")
    auto_js = str(STATIC / "automations.js")
    editor_js = str(STATIC / "automations_editor.js")
    script = f"""
    const fs = require("fs");
    function mkNode(tag) {{
      return {{
        tagName: tag.toUpperCase(), attrs: {{}}, style: {{}}, children: [], listeners: {{}},
        _text: "", className: "", hidden: false, open: false, disabled: false, value: "", checked: false,
        get textContent() {{ return this._text || this.children.map((c) => typeof c === "string" ? c : c.textContent).join(""); }},
        set textContent(v) {{ this._text = String(v); this.children = []; }},
        setAttribute(k, v) {{
          this.attrs[k] = String(v);
          if (k === "disabled") this.disabled = true;
          if (k === "checked") this.checked = true;
          if (k === "value") this.value = String(v);
        }},
        removeAttribute(k) {{
          delete this.attrs[k];
          if (k === "disabled") this.disabled = false;
          if (k === "checked") this.checked = false;
        }},
        getAttribute(k) {{ return this.attrs[k] ?? null; }},
        addEventListener(ev, fn) {{ (this.listeners[ev] = this.listeners[ev] || []).push(fn); }},
        append(...kids) {{ for (const k of kids) this.children.push(k); }},
        replaceChildren(...kids) {{ this.children = [...kids]; }},
      }};
    }}
    globalThis.window = globalThis;
    globalThis.alert = () => {{}};
    globalThis.document = {{
      querySelector: () => ({{ content: "tok" }}),
      createElement: (tag) => mkNode(tag),
      createElementNS: (_ns, tag) => mkNode(tag),
    }};
    eval(fs.readFileSync({json.dumps(ui_js)}, "utf8"));
    const calls = [];
    MC.api = async (path, opts) => {{ calls.push({{ path, opts }}); return {{ id: 1 }}; }};
    MC.route = {{ parse: () => ({{ page: "automations" }}) }};
    MC.render = () => {{}};
    MC.ago = () => "1m ago";
    MC.clock = () => "02:00";
    MC.runHref = (id) => "#/runs/" + id;
    eval(fs.readFileSync({json.dumps(auto_js)}, "utf8"));
    eval(fs.readFileSync({json.dumps(editor_js)}, "utf8"));

    const allNodes = (n) => n ? [n, ...(n.children || []).filter((c) => c && typeof c === "object").flatMap(allNodes)] : [];
    const rule = {{
      id: 1, name: "Digest", enabled: false,
      trigger: {{ kind: "refresh" }}, conditions: {{}},
      action: "notify", scope: {{ digest: "what_changed" }},
      mode: "run", wait_s: 0, max_per_day: 6, cooldown_s: 900, model_jobs_per_day: 3,
    }};
    const panel = MC.automationsHelpers.editorPanel(rule, [], false, () => {{}}, () => {{}});
    const findInput = () => allNodes(panel).find((n) => n.attrs["aria-label"] === "Automation name");
    const findSave = () => allNodes(panel).find((n) => n.tagName === "BUTTON" && n.textContent === "Save");

    const origInput = findInput();
    if (!origInput) throw new Error("missing name input");
    for (const nextVal of ["D", "Di", "Dig", "   ", "My Digest"]) {{
      origInput.value = nextVal;
      (origInput.listeners.input || []).forEach((fn) => fn({{ target: origInput }}));
      const nowInput = findInput();
      if (nowInput !== origInput) throw new Error("name input node was rebuilt while typing: " + nextVal);
      const wantDisabled = !nextVal.trim();
      if (!!findSave().disabled !== wantDisabled) {{
        throw new Error(`Save disabled mismatch for "${{nextVal}}": got ${{findSave().disabled}}`);
      }}
    }}

    (async () => {{
      for (const fn of findSave().listeners.click || []) await fn();
      if (calls.length !== 1 || JSON.parse(calls[0].opts.body).name !== "My Digest") {{
        throw new Error("save did not send typed name: " + JSON.stringify(calls));
      }}
      console.log("OK");
    }})().catch((e) => {{ console.error(e); process.exit(1); }});
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert r.stdout.strip() == "OK"


def test_automations_editor_opens_unrepresentable_rules_readonly_and_grant_includes_cadence():
    import json
    import shutil
    import subprocess
    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    ui_js = str(STATIC / "ui.js")
    auto_js = str(STATIC / "automations.js")
    editor_js = str(STATIC / "automations_editor.js")
    script = f"""
    const fs = require("fs");
    function mkNode(tag) {{
      return {{
        tagName: tag.toUpperCase(), attrs: {{}}, style: {{}}, children: [], listeners: {{}},
        _text: "", className: "", hidden: false, open: false, disabled: false, value: "", checked: false,
        get textContent() {{ return this._text || this.children.map((c) => typeof c === "string" ? c : c.textContent).join(""); }},
        set textContent(v) {{ this._text = String(v); this.children = []; }},
        setAttribute(k, v) {{
          this.attrs[k] = String(v);
          if (k === "disabled") this.disabled = true;
          if (k === "checked") this.checked = true;
          if (k === "value") this.value = String(v);
        }},
        removeAttribute(k) {{
          delete this.attrs[k];
          if (k === "disabled") this.disabled = false;
          if (k === "checked") this.checked = false;
        }},
        getAttribute(k) {{ return this.attrs[k] ?? null; }},
        addEventListener(ev, fn) {{ (this.listeners[ev] = this.listeners[ev] || []).push(fn); }},
        append(...kids) {{ for (const k of kids) this.children.push(k); }},
        replaceChildren(...kids) {{ this.children = [...kids]; }},
      }};
    }}
    globalThis.window = globalThis;
    globalThis.alert = () => {{}};
    globalThis.document = {{
      querySelector: () => ({{ content: "tok" }}),
      createElement: (tag) => mkNode(tag),
      createElementNS: (_ns, tag) => mkNode(tag),
    }};
    eval(fs.readFileSync({json.dumps(ui_js)}, "utf8"));
    MC.api = async () => ({{}});
    MC.route = {{ parse: () => ({{ page: "automations" }}) }};
    MC.render = () => {{}};
    MC.ago = () => "1m ago";
    MC.clock = () => "02:00";
    MC.runHref = (id) => "#/runs/" + id;
    eval(fs.readFileSync({json.dumps(auto_js)}, "utf8"));
    eval(fs.readFileSync({json.dumps(editor_js)}, "utf8"));

    const H = MC.automationsHelpers;
    const allNodes = (n) => n ? [n, ...(n.children || []).filter((c) => c && typeof c === "object").flatMap(allNodes)] : [];
    const projects = [{{ key: "p1", name: "orchard-api", active: true }}];

    // Unrepresentable rules must open read-only with the note and no Save button
    const uneditableRules = [
      {{
        id: 10, name: "Mon/Wed scan", enabled: false, sentence: "On Mon, Wed at 02:00, re-scan everything.",
        trigger: {{ kind: "schedule", time: "02:00", days: ["mon", "wed"] }}, conditions: {{}},
        action: "refresh", scope: {{ only: null }}, mode: "run",
      }},
      {{
        id: 11, name: "Two refresh conditions", enabled: false, sentence: "After a full refresh, notify.",
        trigger: {{ kind: "refresh" }}, conditions: {{ newly_exploited: true, any_failed: true, new_in_lanes: [] }},
        action: "notify", scope: {{ digest: "what_changed" }}, mode: "run",
      }},
      {{
        id: 12, name: "Plan lane condition", enabled: false, sentence: "After a full refresh, notify.",
        trigger: {{ kind: "refresh" }}, conditions: {{ newly_exploited: false, any_failed: false, new_in_lanes: ["plan"] }},
        action: "notify", scope: {{ digest: "what_changed" }}, mode: "run",
      }},
      {{
        id: 13, name: "Two collectors", enabled: false, sentence: "After a full refresh, re-scan brew, repos.",
        trigger: {{ kind: "refresh" }}, conditions: {{}},
        action: "refresh", scope: {{ only: ["brew", "repos"] }}, mode: "run",
      }},
      {{
        id: 14, name: "Multi-category verdict rule", enabled: false, sentence: "When a failure verdict arrives, upgrade.",
        trigger: {{ kind: "verdict" }}, conditions: {{ categories: ["busy_or_locked", "network"], transient: true }},
        action: "upgrade", scope: {{ mode: "failed_item", keys: [] }}, mode: "run",
      }},
      {{
        id: 15, name: "Listed keys upgrade", enabled: false, sentence: "When a failure verdict arrives, upgrade.",
        trigger: {{ kind: "verdict" }}, conditions: {{ categories: ["busy_or_locked"] }},
        action: "upgrade", scope: {{ mode: "keys", keys: ["brew:jq"] }}, mode: "run",
      }},
    ];
    for (const r of uneditableRules) {{
      let closed = 0;
      const p = H.editorPanel(r, projects, false, () => {{ closed += 1; }}, () => {{}});
      if (!p.textContent.includes("Edit isn't available for this automation here")) {{
        throw new Error(`rule ${{r.id}} missing read-only note: ` + p.textContent);
      }}
      const nodes = allNodes(p);
      if (nodes.some((n) => n.tagName === "BUTTON" && n.textContent === "Save")) {{
        throw new Error(`rule ${{r.id}} must not render Save button`);
      }}
      if (nodes.some((n) => n.tagName === "SELECT" || n.tagName === "INPUT")) {{
        throw new Error(`rule ${{r.id}} must not render editable controls`);
      }}
      const closeBtn = nodes.find((n) => n.tagName === "BUTTON");
      if (!closeBtn) throw new Error(`rule ${{r.id}} missing Close button`);
      for (const fn of closeBtn.listeners.click || []) fn();
      if (closed !== 1) throw new Error(`rule ${{r.id}} Close did not invoke onCancel`);
    }}

    // Editable schedule rule: grant sentence includes trigger and cadence
    const schedRule = {{
      id: 20, name: "Weekday fix", enabled: true,
      trigger: {{ kind: "schedule", time: "02:00", days: ["mon", "tue", "wed", "thu", "fri"] }},
      conditions: {{ project: "p1", lane: "now" }},
      action: "start_fix", scope: {{ project: "p1", lane: "now" }},
      mode: "run", wait_s: 0, max_per_day: 6, cooldown_s: 900, model_jobs_per_day: 3,
    }};
    const p = H.editorPanel(schedRule, projects, false, () => {{}}, () => {{}});
    const grantBox = allNodes(p).find((n) => n.className === "auto-grant");
    if (!grantBox || !grantBox.textContent.includes("Weekdays") || !grantBox.textContent.includes("02:00")) {{
      throw new Error("grant sentence missing schedule cadence/time: " + (grantBox && grantBox.textContent));
    }}
    const trigSel = allNodes(p).find((n) => n.attrs["aria-label"] === "Trigger");
    trigSel.value = "refresh";
    (trigSel.listeners.change || []).forEach((fn) => fn({{ target: trigSel }}));
    if (!grantBox.textContent.includes("full refresh")) {{
      throw new Error("grant sentence missing refresh trigger after switch: " + grantBox.textContent);
    }}
    console.log("OK");
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert r.stdout.strip() == "OK"


def test_automations_editor_supports_verdict_trigger_and_retry_when_busy_preset():
    import json
    import shutil
    import subprocess
    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    ui_js = str(STATIC / "ui.js")
    auto_js = str(STATIC / "automations.js")
    editor_js = str(STATIC / "automations_editor.js")
    script = f"""
    const fs = require("fs");
    function mkNode(tag) {{
      return {{
        tagName: tag.toUpperCase(), attrs: {{}}, style: {{}}, children: [], listeners: {{}},
        _text: "", className: "", hidden: false, open: false, disabled: false, value: "", checked: false,
        get textContent() {{ return this._text || this.children.map((c) => typeof c === "string" ? c : c.textContent).join(""); }},
        set textContent(v) {{ this._text = String(v); this.children = []; }},
        setAttribute(k, v) {{
          this.attrs[k] = String(v);
          if (k === "disabled") this.disabled = true;
          if (k === "checked") this.checked = true;
          if (k === "value") this.value = String(v);
        }},
        removeAttribute(k) {{
          delete this.attrs[k];
          if (k === "disabled") this.disabled = false;
          if (k === "checked") this.checked = false;
        }},
        getAttribute(k) {{ return this.attrs[k] ?? null; }},
        addEventListener(ev, fn) {{ (this.listeners[ev] = this.listeners[ev] || []).push(fn); }},
        append(...kids) {{ for (const k of kids) this.children.push(k); }},
        replaceChildren(...kids) {{ this.children = [...kids]; }},
      }};
    }}
    globalThis.window = globalThis;
    globalThis.alert = () => {{}};
    globalThis.document = {{
      querySelector: () => ({{ content: "tok" }}),
      createElement: (tag) => mkNode(tag),
      createElementNS: (_ns, tag) => mkNode(tag),
    }};
    eval(fs.readFileSync({json.dumps(ui_js)}, "utf8"));
    const calls = [];
    MC.api = async (path, opts) => {{ calls.push({{ path, opts }}); return {{}}; }};
    MC.route = {{ parse: () => ({{ page: "automations" }}) }};
    MC.render = () => {{}};
    MC.ago = () => "1m ago";
    MC.clock = () => "02:00";
    MC.runHref = (id) => "#/runs/" + id;
    eval(fs.readFileSync({json.dumps(auto_js)}, "utf8"));
    eval(fs.readFileSync({json.dumps(editor_js)}, "utf8"));

    const H = MC.automationsHelpers;
    const allNodes = (n) => n ? [n, ...(n.children || []).filter((c) => c && typeof c === "object").flatMap(allNodes)] : [];

    const preset = {{
      id: 3, name: "Retry when busy", preset: "retry_when_busy", enabled: false,
      trigger: {{ kind: "verdict" }},
      conditions: {{ categories: ["busy_or_locked"], transient: true, needs_person: false, run_kind: "upgrade" }},
      action: "upgrade", scope: {{ mode: "failed_item", keys: [] }}, mode: "run",
      wait_s: 900, max_per_day: 6, cooldown_s: 900,
    }};
    const panel = H.editorPanel(preset, [], false, () => {{}}, () => {{}});
    if (panel.textContent.includes("Edit isn't available")) {{
      throw new Error("Retry when busy must be editable, got read-only");
    }}
    const selects = allNodes(panel).filter((n) => n.tagName === "SELECT");
    const labels = selects.map((s) => s.getAttribute("aria-label"));
    for (const req of ["Trigger", "Verdict category", "Failure type", "Transient", "Needs a person", "Verdict source", "Action"]) {{
      if (!labels.includes(req)) throw new Error("missing verdict dropdown " + req + " in " + JSON.stringify(labels));
    }}
    const turnOn = allNodes(panel).find((n) => n.tagName === "INPUT" && n.getAttribute("aria-label") === "Turn on after saving");
    turnOn.checked = true;
    turnOn.listeners.change[0]({{ target: turnOn }});
    const saveBefore = allNodes(panel).find((n) => n.tagName === "BUTTON" && n.textContent === "Save");
    if (!saveBefore || !saveBefore.disabled) throw new Error("Save must stay disabled until grant checkbox is checked");
    const grantBox = allNodes(panel).find((n) => n.tagName === "INPUT" && n.getAttribute("aria-label") === "Confirm scope grant");
    grantBox.checked = true;
    grantBox.listeners.change[0]({{ target: grantBox }});
    const saveAfter = allNodes(panel).find((n) => n.tagName === "BUTTON" && n.textContent === "Save");
    if (!saveAfter || saveAfter.disabled) throw new Error("Save should be enabled after confirming grant");
    saveAfter.listeners.click[0]();
    setTimeout(() => {{
      if (calls.length !== 1) throw new Error("expected 1 save POST");
      const payload = JSON.parse(calls[0].opts.body);
      if (payload.trigger.kind !== "verdict") throw new Error("bad trigger: " + JSON.stringify(payload.trigger));
      if (payload.conditions.categories[0] !== "busy_or_locked" || payload.conditions.transient !== true || payload.conditions.needs_person !== false || payload.conditions.run_kind !== "upgrade") {{
        throw new Error("bad conditions: " + JSON.stringify(payload.conditions));
      }}
      if (payload.action !== "upgrade" || payload.scope.mode !== "failed_item" || payload.wait_s !== 900 || payload.confirm_grant !== true) {{
        throw new Error("bad payload: " + JSON.stringify(payload));
      }}
      if ("delay_s" in payload) throw new Error("must never send delay_s");
      console.log("VERDICT_EDITOR_OK");
    }}, 0);
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0 and "VERDICT_EDITOR_OK" in r.stdout, f"stdout={r.stdout}\nstderr={r.stderr}"


def test_offer_cards_render_on_automations_and_today_with_approve_decline_expiry_and_demo():
    import json
    import shutil
    import subprocess
    import pytest

    for name in ("automations.js", "today.js"):
        assert "innerHTML" not in code_only((STATIC / name).read_text(encoding="utf-8")), name
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    ui_js = str(STATIC / "ui.js")
    auto_js = str(STATIC / "automations.js")
    today_js = str(STATIC / "today.js")
    script = f"""
    const fs = require("fs");
    function mkNode(tag) {{
      return {{
        tagName: tag.toUpperCase(), attrs: {{}}, style: {{}}, children: [], listeners: {{}},
        _text: "", className: "", disabled: false,
        get textContent() {{ return this._text || this.children.map((c) => typeof c === "string" ? c : c.textContent).join(""); }},
        set textContent(v) {{ this._text = String(v); this.children = []; }},
        setAttribute(k, v) {{ this.attrs[k] = String(v); if (k === "disabled") this.disabled = true; }},
        getAttribute(k) {{ return this.attrs[k] ?? null; }},
        addEventListener(ev, fn) {{ (this.listeners[ev] = this.listeners[ev] || []).push(fn); }},
        append(...kids) {{ for (const k of kids) this.children.push(k); }},
        replaceChildren(...kids) {{ this.children = []; this.append(...kids); }},
      }};
    }}
    globalThis.window = globalThis;
    globalThis.location = {{ hash: "#/automations" }};
    globalThis.alert = () => {{}};
    globalThis.confirm = () => true;
    globalThis.document = {{
      querySelector: () => ({{ content: "tok" }}),
      createElement: (tag) => mkNode(tag),
      createElementNS: (_ns, tag) => mkNode(tag),
    }};
    eval(fs.readFileSync({json.dumps(ui_js)}, "utf8"));
    const calls = [];
    MC.api = async (path, opts) => {{
      calls.push({{ path, opts }});
      if (path === "/api/automations") return {{ paused: false, demo: false, automations: [], projects: [], waiting_offers: [] }};
      if (path.startsWith("/api/automations/firings?")) return {{ firings: [] }};
      return {{ status: "done" }};
    }};
    MC.route = {{ parse: () => ({{ page: "automations" }}), href: (p, id) => `#/${{p}}/${{id}}` }};
    MC.render = () => {{}};
    MC.banner = () => {{}};
    MC.refreshNow = async () => {{}};
    MC.ago = () => "2m ago";
    MC.clock = () => "14:00";
    MC.runHref = (id) => "#/runs/" + id;
    MC.ask = () => {{}};
    eval(fs.readFileSync({json.dumps(auto_js)}, "utf8"));
    eval(fs.readFileSync({json.dumps(today_js)}, "utf8"));

    const allNodes = (n) => n ? [n, ...(n.children || []).filter((c) => c && typeof c === "object").flatMap(allNodes)] : [];
    const liveOffer = {{
      id: 42, automation_id: 7, automation_name: "<b>Relaunch</b>",
      action: "relaunch_app", action_label: "Quit and reopen Google Chrome to finish its update",
      items: [{{ key: "brew-cask:google-chrome", name: "Google Chrome", installed: "130.0", available: "131.0" }}],
      input: {{ app: "chrome" }}, why: "Triggered after a full refresh",
      status: "offered", expired: false, created: 1700000000, deadline: 9999999999,
    }};
    const expiredOffer = {{
      ...liveOffer, id: 43, status: "expired", expired: true, deadline: 1000,
    }};

    // 1. Automations offersPanel renders plain text, input, why, Approve & Decline, disables in demo, and flags expired
    const H = MC.automationsHelpers;
    if (typeof H.offersPanel !== "function") throw new Error("MC.automationsHelpers.offersPanel missing");
    const panel = H.offersPanel([liveOffer, expiredOffer], false);
    const ptxt = panel.textContent;
    if (!ptxt.includes("Quit and reopen Google Chrome") || !ptxt.includes("130.0") || !ptxt.includes("131.0")
        || !ptxt.includes("chrome") || !ptxt.includes("Triggered after a full refresh")
        || !ptxt.includes("<b>Relaunch</b>")) {{
      throw new Error("missing offer card text: " + ptxt);
    }}
    if (!ptxt.toLowerCase().includes("expired")) throw new Error("missing expired notice: " + ptxt);
    const cards = allNodes(panel).filter((n) => n.tagName === "ARTICLE" && (n.className || "").includes("auto-offer"));
    if (cards.length !== 2) throw new Error("expected 2 offer cards, got " + cards.length);
    const liveApprove = allNodes(cards[0]).find((n) => n.tagName === "BUTTON" && n.textContent === "Approve");
    const liveDecline = allNodes(cards[0]).find((n) => n.tagName === "BUTTON" && n.textContent === "Decline");
    const expApprove = allNodes(cards[1]).find((n) => n.tagName === "BUTTON" && n.textContent === "Approve");
    if (!liveApprove || liveApprove.disabled || !liveDecline || liveDecline.disabled) {{
      throw new Error("live offer buttons must be enabled");
    }}
    if (!expApprove || !expApprove.disabled) throw new Error("expired offer Approve button must be disabled");
    (async () => {{
      await liveApprove.listeners.click[0]();
      await liveDecline.listeners.click[0]();

      const demoPanel = H.offersPanel([liveOffer], true);
      const demoBtns = allNodes(demoPanel).filter((n) => n.tagName === "BUTTON");
      if (!demoBtns.length || demoBtns.some((b) => !b.disabled)) throw new Error("demo offer buttons must be disabled");

      // 2. Today offer card helper renders Approve/Decline and posts the right decide_offer bodies
      const TH = MC.todayHelpers;
      if (typeof TH.offerCard !== "function") throw new Error("MC.todayHelpers.offerCard missing");
      const tLive = TH.offerCard(liveOffer, false);
      const tTxt = tLive.textContent;
      if (!tTxt.includes("Quit and reopen Google Chrome") || !tTxt.includes("Triggered after a full refresh")) {{
        throw new Error("today offer card missing text: " + tTxt);
      }}
      const tDemo = TH.offerCard(liveOffer, true);
      if (allNodes(tDemo).filter((n) => n.tagName === "BUTTON").some((b) => !b.disabled)) {{
        throw new Error("today demo offer buttons must be disabled");
      }}
      const tApprove = allNodes(tLive).find((n) => n.tagName === "BUTTON" && n.textContent === "Approve");
      const tDecline = allNodes(tLive).find((n) => n.tagName === "BUTTON" && n.textContent === "Decline");
      await tApprove.listeners.click[0]({{ stopPropagation() {{}} }});
      await tDecline.listeners.click[0]({{ stopPropagation() {{}} }});

      const approveCall = calls.find((c) => c.path === "/api/automations/firings/42/decide" && JSON.parse(c.opts.body).approve === true);
      const declineCall = calls.find((c) => c.path === "/api/automations/firings/42/decide" && JSON.parse(c.opts.body).approve === false);
      if (!approveCall || !declineCall) throw new Error("decide calls mismatch: " + JSON.stringify(calls));
      console.log("OFFER_CARDS_OK");
    }})().catch((err) => {{ console.error(err && err.stack || err); process.exit(1); }});
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0 and "OFFER_CARDS_OK" in r.stdout, f"stdout={r.stdout}\nstderr={r.stderr}"


def test_offer_cards_never_stringify_raw_input_and_cap_fields():
    import json
    import shutil
    import subprocess
    import pytest

    for name in ("automations.js", "today.js"):
        src = (STATIC / name).read_text(encoding="utf-8")
        assert "JSON.stringify(offer.input" not in src, f"{name} must never JSON.stringify(offer.input)"

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    ui_js = str(STATIC / "ui.js")
    auto_js = str(STATIC / "automations.js")
    today_js = str(STATIC / "today.js")
    script = f"""
    const fs = require("fs");
    function mkNode(tag) {{
      return {{
        tagName: tag.toUpperCase(), attrs: {{}}, style: {{}}, children: [], listeners: {{}},
        _text: "", className: "", disabled: false,
        get textContent() {{ return this._text || this.children.map((c) => typeof c === "string" ? c : c.textContent).join(""); }},
        set textContent(v) {{ this._text = String(v); this.children = []; }},
        setAttribute(k, v) {{ this.attrs[k] = String(v); if (k === "disabled") this.disabled = true; }},
        getAttribute(k) {{ return this.attrs[k] ?? null; }},
        addEventListener(ev, fn) {{ (this.listeners[ev] = this.listeners[ev] || []).push(fn); }},
        append(...kids) {{ for (const k of kids) this.children.push(k); }},
        replaceChildren(...kids) {{ this.children = []; this.append(...kids); }},
      }};
    }}
    globalThis.window = globalThis;
    globalThis.location = {{ hash: "#/automations" }};
    globalThis.alert = () => {{}};
    globalThis.confirm = () => true;
    globalThis.document = {{
      querySelector: () => ({{ content: "tok" }}),
      createElement: (tag) => mkNode(tag),
      createElementNS: (_ns, tag) => mkNode(tag),
    }};
    eval(fs.readFileSync({json.dumps(ui_js)}, "utf8"));
    MC.api = async () => ({{}});
    MC.route = {{ parse: () => ({{ page: "automations" }}), href: (p, id) => `#/${{p}}/${{id}}` }};
    MC.render = () => {{}};
    MC.banner = () => {{}};
    MC.refreshNow = async () => {{}};
    MC.ago = () => "2m ago";
    MC.clock = () => "14:00";
    MC.runHref = (id) => "#/runs/" + id;
    MC.ask = () => {{}};
    eval(fs.readFileSync({json.dumps(auto_js)}, "utf8"));
    eval(fs.readFileSync({json.dumps(today_js)}, "utf8"));

    const hostileOffer = {{
      id: 99, automation_id: 7, automation_name: "Offer\\u202e" + "R".repeat(200),
      action: "relaunch_app", action_label: "Quit and reopen Chrome " + "L".repeat(300),
      items: [{{ key: "mac-app:" + "k".repeat(400), name: "Chrome\\u202e" + "N".repeat(200), installed: "1.0" + "0".repeat(80), available: "2.0" + "9".repeat(80) }}],
      input: {{ app: "chrome", secret: "RAW_JSON_LEAK_" + "X".repeat(1000), nested: {{ boom: true }} }},
      why: "Triggered\\u202e " + "W".repeat(300),
      status: "offered", expired: false, created: 1700000000, deadline: 9999999999,
    }};
    const keyOnlyOffer = {{
      id: 100, automation_id: 8, automation_name: "Open app",
      action: "open_app", action_label: "Open app",
      items: [],
      input: {{ key: "mac-app:" + "a".repeat(400), extra: "SHOULD_NOT_SHOW" }},
      why: "In-app update waiting",
      status: "offered", expired: false, created: 1700000000, deadline: 9999999999,
    }};

    for (const [label, node] of [
      ["auto-hostile", MC.automationsHelpers.offerCard(hostileOffer, false)],
      ["today-hostile", MC.todayHelpers.offerCard(hostileOffer, false)],
      ["auto-keyonly", MC.automationsHelpers.offerCard(keyOnlyOffer, false)],
      ["today-keyonly", MC.todayHelpers.offerCard(keyOnlyOffer, false)],
    ]) {{
      const txt = node.textContent;
      if (txt.includes("RAW_JSON_LEAK") || txt.includes("SHOULD_NOT_SHOW") || txt.includes("secret") || txt.includes("nested") || txt.includes("{{")) {{
        throw new Error(label + " leaked raw input JSON: " + txt.slice(0, 300));
      }}
      if (txt.includes("\\u202e")) throw new Error(label + " leaked bidi char");
      if (txt.length > 700) throw new Error(label + " uncapped text length: " + txt.length);
    }}
    console.log("OFFER_SANITIZED_OK");
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0 and "OFFER_SANITIZED_OK" in r.stdout, f"stdout={r.stdout}\nstderr={r.stderr}"


def test_offer_buttons_disable_in_flight_and_treat_409_as_quiet_refresh():
    import json
    import shutil
    import subprocess
    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    ui_js = str(STATIC / "ui.js")
    auto_js = str(STATIC / "automations.js")
    today_js = str(STATIC / "today.js")
    script = f"""
    const fs = require("fs");
    function mkNode(tag) {{
      return {{
        tagName: tag.toUpperCase(), attrs: {{}}, style: {{}}, children: [], listeners: {{}},
        _text: "", className: "", disabled: false,
        get textContent() {{ return this._text || this.children.map((c) => typeof c === "string" ? c : c.textContent).join(""); }},
        set textContent(v) {{ this._text = String(v); this.children = []; }},
        setAttribute(k, v) {{ this.attrs[k] = String(v); if (k === "disabled") this.disabled = true; }},
        getAttribute(k) {{ return this.attrs[k] ?? null; }},
        addEventListener(ev, fn) {{ (this.listeners[ev] = this.listeners[ev] || []).push(fn); }},
        append(...kids) {{ for (const k of kids) this.children.push(k); }},
        replaceChildren(...kids) {{ this.children = []; this.append(...kids); }},
      }};
    }}
    const alerts = [], banners = [];
    let refreshes = 0, listFetches = 0;
    globalThis.window = globalThis;
    globalThis.location = {{ hash: "#/automations" }};
    globalThis.alert = (m) => alerts.push(String(m));
    globalThis.confirm = () => true;
    globalThis.document = {{
      querySelector: () => ({{ content: "tok" }}),
      createElement: (tag) => mkNode(tag),
      createElementNS: (_ns, tag) => mkNode(tag),
    }};
    globalThis.fetch = async () => ({{
      ok: false, status: 409,
      json: async () => ({{ detail: "This offer was already decided or expired" }}),
    }});
    eval(fs.readFileSync({json.dumps(ui_js)}, "utf8"));

    (async () => {{
      // Verify MC.api preserves HTTP status on thrown errors
      let caught = null;
      try {{ await MC.api("/api/automations/firings/1/decide", {{ method: "POST", body: "{{}}" }}); }}
      catch (e) {{ caught = e; }}
      if (!caught || caught.status !== 409) throw new Error("MC.api must attach status=409: " + (caught && caught.status));

      let pendingResolve = null;
      let decideMode = "defer";
      let decideCalls = 0;
      MC.api = async (path, opts) => {{
        if (path === "/api/automations") {{ listFetches++; return {{ paused: false, demo: false, automations: [], projects: [], waiting_offers: [] }}; }}
        if (path.startsWith("/api/automations/firings?")) return {{ firings: [] }};
        if (path.endsWith("/decide")) {{
          decideCalls++;
          if (decideMode === "defer") return await new Promise((res) => {{ pendingResolve = res; }});
          if (decideMode === "409") throw Object.assign(new Error("This offer was already decided or expired"), {{ status: 409 }});
          if (decideMode === "403") throw Object.assign(new Error("All automations are paused"), {{ status: 403 }});
        }}
        return {{ status: "done" }};
      }};
      MC.route = {{ parse: () => ({{ page: "automations" }}), href: (p, id) => `#/${{p}}/${{id}}` }};
      MC.render = () => {{}};
      MC.banner = (m) => banners.push(String(m));
      MC.refreshNow = async () => {{ refreshes++; }};
      MC.ago = () => "2m ago";
      MC.clock = () => "14:00";
      MC.runHref = (id) => "#/runs/" + id;
      MC.ask = () => {{}};
      eval(fs.readFileSync({json.dumps(auto_js)}, "utf8"));
      eval(fs.readFileSync({json.dumps(today_js)}, "utf8"));

      const allNodes = (n) => n ? [n, ...(n.children || []).filter((c) => c && typeof c === "object").flatMap(allNodes)] : [];
      const offer = {{
        id: 77, automation_id: 3, automation_name: "Offer Chrome",
        action: "relaunch_app", action_label: "Quit and reopen Chrome",
        items: [], input: {{ app: "chrome" }}, why: "Refresh",
        status: "offered", expired: false, created: 1700000000, deadline: 9999999999,
      }};

      // 1. Automations card: both buttons disabled while in flight, second click ignored
      const aCard = MC.automationsHelpers.offerCard(offer, false);
      const aApp = allNodes(aCard).find((n) => n.tagName === "BUTTON" && n.textContent === "Approve");
      const aDec = allNodes(aCard).find((n) => n.tagName === "BUTTON" && n.textContent === "Decline");
      const p1 = aApp.listeners.click[0]();
      if (!aApp.disabled || !aDec.disabled) throw new Error("automations buttons must be disabled in flight");
      aDec.listeners.click[0]();
      if (decideCalls !== 1) throw new Error("expected 1 in-flight decide call, got " + decideCalls);
      pendingResolve({{ status: "done" }});
      await p1;
      if (aApp.disabled || aDec.disabled) throw new Error("automations buttons should re-enable after finish");

      // 2. Automations card: 409 is quiet refresh (no alert); 403 alerts
      decideMode = "409";
      const beforeList = listFetches;
      await aDec.listeners.click[0]();
      if (alerts.length !== 0) throw new Error("409 must not alert: " + JSON.stringify(alerts));
      if (listFetches <= beforeList) throw new Error("409 must trigger quiet refresh");
      decideMode = "403";
      await aApp.listeners.click[0]();
      if (alerts.length !== 1) throw new Error("403 must alert: " + JSON.stringify(alerts));

      // 3. Today card: both buttons disabled in flight, 409 quiet refresh (no banner), 403 banner
      decideMode = "defer";
      decideCalls = 0;
      const tCard = MC.todayHelpers.offerCard(offer, false);
      const tApp = allNodes(tCard).find((n) => n.tagName === "BUTTON" && n.textContent === "Approve");
      const tDec = allNodes(tCard).find((n) => n.tagName === "BUTTON" && n.textContent === "Decline");
      const p2 = tApp.listeners.click[0]({{ stopPropagation() {{}} }});
      if (!tApp.disabled || !tDec.disabled) throw new Error("today buttons must be disabled in flight");
      tDec.listeners.click[0]({{ stopPropagation() {{}} }});
      if (decideCalls !== 1) throw new Error("today expected 1 in-flight decide call, got " + decideCalls);
      pendingResolve({{ status: "done" }});
      await p2;
      if (tApp.disabled || tDec.disabled) throw new Error("today buttons should re-enable after finish");

      decideMode = "409";
      const beforeRef = refreshes;
      await tDec.listeners.click[0]({{ stopPropagation() {{}} }});
      if (banners.length !== 0) throw new Error("today 409 must not banner: " + JSON.stringify(banners));
      if (refreshes <= beforeRef) throw new Error("today 409 must refresh");
      decideMode = "403";
      await tApp.listeners.click[0]({{ stopPropagation() {{}} }});
      if (banners.length !== 1) throw new Error("today 403 must banner: " + JSON.stringify(banners));

      console.log("OFFER_INFLIGHT_409_OK");
    }})().catch((err) => {{ console.error(err && err.stack || err); process.exit(1); }});
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0 and "OFFER_INFLIGHT_409_OK" in r.stdout, f"stdout={r.stdout}\nstderr={r.stderr}"


