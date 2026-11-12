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
