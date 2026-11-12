import json
from pathlib import Path

import httpx

from bosun import proc
from bosun.config import load_settings
from bosun.enrich.exposure import EpssClient, impact, profile, reach
from bosun.enrich.explain import Explainer, facts
from bosun.models import Item, Source, Summary, Vuln
from bosun.projects import Project
from bosun.store import Store
from bosun.usage import (
    annotate_usage, dependency_chain, import_index, pip_required_by, project_tools, tool_for,
)

NET = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"


def test_reach_categories_v3_and_v4():
    assert reach(Vuln("a", vector=NET)) == "remote"
    assert reach(Vuln("a", vector="CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H")) == "remote_auth"
    assert reach(Vuln("a", vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:H/A:H")) == "needs_action"
    assert reach(Vuln("a", vector="CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H")) == "local"
    assert reach(Vuln("a", vector="CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:P/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N")) == "needs_action"
    assert reach(Vuln("a", vector="CVSS:3.1/AV:A/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N")) == "adjacent"
    assert reach(Vuln("a")) == "unknown"


def test_impact_words():
    assert impact(Vuln("a", vector=NET)).startswith("full compromise")
    assert impact(Vuln("a", vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H")) == "crash or hang (denial of service)"
    assert "reads" in impact(Vuln("a", vector="CVSS:3.1/AV:L/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N"))


def test_profile_and_epss(tmp_path):
    item = Item(Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0", vulns_fixed=(
        Vuln("CVE-1", vector=NET, cvss=9.8), Vuln("GHSA-x", aliases=("CVE-2",), vector="CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H")))

    def handler(req):
        cves = req.url.params["cve"].split(",")
        data = [{"cve": c, "epss": "0.5" if c == "CVE-1" else "0.001", "percentile": "0.99" if c == "CVE-1" else "0.2"} for c in cves]
        return httpx.Response(200, json={"data": data})

    store = Store(tmp_path / "db")
    (tagged,) = EpssClient(store, httpx.Client(transport=httpx.MockTransport(handler))).apply([item])
    assert tagged.vulns_fixed[0].epss == 0.5 and tagged.vulns_fixed[1].epss == 0.001  # alias CVE used for GHSA
    prof = profile(tagged)
    assert prof["reach"] == "remote" and prof["reach_counts"] == {"remote": 1, "local": 1}
    assert prof["max_epss"] == 0.5 and prof["max_epss_pct"] == 0.99
    assert store.cache_get("epss", "CVE-2") == [0.001, 0.2]  # cached for next time


def test_project_tools_and_tool_mapping(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"packageManager": "pnpm@9.12.0", "engines": {"node": ">=20"}}))
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (tmp_path / ".python-version").write_text("3.12\n")
    (tmp_path / "docker-compose.yml").write_text("services:\n  db:\n    image: postgres:16\n")
    project_tools.cache_clear()
    t = project_tools(str(tmp_path))
    assert t["pnpm"] == "pins pnpm@9.12.0, lockfile v9.0" and t["node"] == "engines >=20"
    assert t["python"] == ".python-version 3.12" and "postgres" in t and "docker" in t
    assert tool_for(Item(Source.NPM_GLOBAL, "pnpm", "pnpm", "9", "12")) == "pnpm"
    assert tool_for(Item(Source.WINGET, "OpenJS.NodeJS.22", "Node", "22.1", "22.2")) == "node"
    assert tool_for(Item(Source.WINGET, "Zoom.Zoom", "Zoom", "1", "2")) is None


def test_npm_and_uv_dependency_chains(tmp_path):
    (tmp_path / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {
        "": {"dependencies": {"next": "^16"}},
        "node_modules/next": {"dependencies": {"@next/env": "1"}},
        "node_modules/@next/env": {"dependencies": {"dotenv": "1"}},
        "node_modules/dotenv": {}}}))
    item = Item(Source.REPO, "dotenv", "dotenv", "1", "2", ecosystem="npm", lockfile=str(tmp_path / "package-lock.json"))
    assert dependency_chain(item) == ["next", "@next/env", "dotenv"]
    assert dependency_chain(item.with_(package_id="next")) == ["next"]
    (tmp_path / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "app"\nsource = { editable = "." }\ndependencies = [{ name = "httpx" }]\n'
        '[[package]]\nname = "httpx"\ndependencies = [{ name = "anyio" }, { name = "certifi" }]\n'
        '[[package]]\nname = "anyio"\n[[package]]\nname = "certifi"\n')
    uv = Item(Source.REPO, "anyio", "anyio", "4.9", "4.14", ecosystem="PyPI", lockfile=str(tmp_path / "uv.lock"))
    assert dependency_chain(uv) == ["httpx", "anyio"]


def test_import_index_js_and_python(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("import x from 'next/router'\nconst y = require(\"@scope/pkg/sub\")\nimport './local'\n")
    (tmp_path / "app.py").write_text("import httpx\nfrom bs4 import BeautifulSoup\n")
    (tmp_path / "node_modules" / "z").mkdir(parents=True)
    (tmp_path / "node_modules" / "z" / "i.js").write_text("import 'next'\n")
    import_index.cache_clear()
    idx = import_index(str(tmp_path))
    assert idx["next"] == ("src/a.ts",) and idx["@scope/pkg"] == ("src/a.ts",)
    assert idx["httpx"] == ("app.py",) and idx["bs4"] == ("app.py",) and "./local" not in idx


def test_pip_required_by(monkeypatch):
    out = "Name: certifi\nVersion: 1\nRequired-by: httpcore, httpx\n---\nName: rich\nRequired-by: \n"
    monkeypatch.setattr("bosun.usage.proc.run", lambda *a, **k: proc.Result(("py",), 0, out, ""))
    assert pip_required_by(["certifi", "rich"], ("py",)) == {"certifi": ["httpcore", "httpx"], "rich": []}


def test_annotate_usage_system_and_repo(tmp_path, monkeypatch):
    repo = tmp_path / "web"
    (repo / ".git").mkdir(parents=True)
    (repo / "package.json").write_text(json.dumps({"packageManager": "pnpm@9.1.0"}))
    (repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (repo / "index.ts").write_text("import next from 'next'\n")
    (repo / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {
        "": {"dependencies": {"next": "1"}}, "node_modules/next": {"dependencies": {"postcss": "1"}}, "node_modules/postcss": {}}}))
    project_tools.cache_clear(); import_index.cache_clear()
    proj = Project("k", "web", True, 3, (str(repo),), False, False)
    monkeypatch.setattr("bosun.usage.proc.run", lambda *a, **k: proc.Result(("py",), 0, "", ""))
    items = [Item(Source.NPM_GLOBAL, "pnpm", "pnpm", "9.12.0", "12.15.0"),
             Item(Source.REPO, "postcss", "postcss", "8.4.0", "8.4.31", ecosystem="npm", project="k",
                  lockfile=str(repo / "package-lock.json"), direct=False)]
    pnpm, postcss = annotate_usage(items, {"k": proj}, load_settings())
    assert pnpm.used_by == ("web: pins pnpm@9.1.0, lockfile v9.0",)
    assert postcss.dep_path == "next > postcss" and postcss.imports == ("index.ts",)  # imports of the top-level parent


def test_explainer_facts_and_cache(tmp_path, monkeypatch):
    item = Item(Source.NPM_GLOBAL, "pnpm", "pnpm", "9.12.0", "12.15.0",
                vulns_fixed=(Vuln("GHSA-1", vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:H/A:H", cvss=8.8, epss=0.002, epss_pct=0.4),),
                used_by=("web: pins pnpm@9.1.0",), summary=Summary(headline="h", breaking_changes=("new defaults",)))
    text = facts(item)
    assert "needs you to act" in text and "pins pnpm@9.1.0" in text and "new defaults" in text and "EPSS 0.20%" in text
    calls = []
    answer = {"exposure": "needs_your_action", "answer": "Only if you install a malicious package.", "attack_story": "s",
              "preconditions": ["you run pnpm install on a hostile repo"], "worst_case": "w", "urgency": "this_week",
              "break_risk": [{"where": "web", "risk": "medium", "why": "lockfile v9 -> v10"}], "before_upgrading": ["x"]}

    def fake(argv, timeout=300, cwd=None, stdin=None, env=None):
        calls.append(argv)
        assert argv[argv.index("--tools") + 1] == "" and "--strict-mcp-config" in argv
        return proc.Result(tuple(argv), 0, json.dumps({"subtype": "success", "structured_output": answer}), "")

    monkeypatch.setattr("bosun.enrich.explain.proc.run", fake)
    ex = Explainer(Store(tmp_path / "db"), "sonnet", tmp_path / "sb", claude="claude")
    assert ex.explain(item)["exposure"] == "needs_your_action"
    assert ex.explain(item)["break_risk"][0]["where"] == "web" and len(calls) == 1  # second call served from cache
