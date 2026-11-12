import json

from bosun.lanes import classify, risk
from bosun.manifests import annotate, in_range, npm_declared, npm_lock_dev, py_declared
from bosun.models import Item, Source, Vuln


def repo_item(tmp_path, name, eco, installed, available, lock, cvss=9.0):
    return Item(Source.REPO, name, name, installed, available, location=str(tmp_path), ecosystem=eco,
                lockfile=str(tmp_path / lock), vulns_fixed=(Vuln("X", cvss=cvss),))


def test_in_range():
    assert in_range("npm", "^4.9.0", "4.14.2") is True
    assert in_range("npm", "^4.9.0", "5.0.0") is False
    assert in_range("npm", "~16.3.4", "16.4.1") is False
    assert in_range("npm", "workspace:*", "1.0.0") is None
    assert in_range("PyPI", ">=4.0,<5", "4.15.1") is True
    assert in_range("PyPI", "==1.0.0", "1.3.1") is False
    assert in_range("PyPI", "^1.2", "1.9.0") is True  # poetry caret
    assert in_range("npm", "", "1.0.0") is None


def test_npm_direct_dev_and_transitive(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"next": "^16.3.4"}, "devDependencies": {"vitest": "~3.1.0"}}))
    (tmp_path / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {
        "": {}, "node_modules/next": {}, "node_modules/vitest": {"dev": True},
        "node_modules/esbuild": {"dev": True}, "node_modules/glob": {"dev": True},
        "node_modules/foo/node_modules/glob": {}}}))
    npm_declared.cache_clear(); npm_lock_dev.cache_clear()
    items = annotate([
        repo_item(tmp_path, "next", "npm", "16.3.4", "16.4.1", "package-lock.json"),
        repo_item(tmp_path, "vitest", "npm", "3.1.0", "3.2.0", "package-lock.json"),
        repo_item(tmp_path, "esbuild", "npm", "0.24.0", "0.25.0", "package-lock.json"),
        repo_item(tmp_path, "glob", "npm", "10.4.0", "10.5.0", "package-lock.json"),
    ])
    nxt, vitest, esbuild, glob = items
    assert (nxt.direct, nxt.dev, nxt.in_range, nxt.fix_kind) == (True, False, True, "lockfile refresh")
    assert (vitest.direct, vitest.dev, vitest.in_range, vitest.fix_kind) == (True, True, False, "manifest edit")
    assert (esbuild.direct, esbuild.dev, esbuild.fix_kind) == (False, True, "parent bump or override")
    assert glob.dev is False  # one runtime copy makes it runtime


def test_python_pyproject_groups_and_requirements(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["anyio>=4.0,<5", "certifi"]\n'
        '[project.optional-dependencies]\ntest = ["pytest>=8"]\n'
        '[dependency-groups]\ndev = ["ruff==0.5.0"]\n')
    (tmp_path / "requirements-dev.txt").write_text("black==24.1.0  # formatter\n-e .\n")
    py_declared.cache_clear()
    d = py_declared(str(tmp_path))
    assert not d["anyio"].dev and d["pytest"].dev and d["ruff"].dev and d["black"].dev
    (anyio,) = annotate([repo_item(tmp_path, "AnyIO", "PyPI", "4.9.0", "4.14.2", "uv.lock")])
    assert anyio.direct and anyio.in_range and anyio.fix_kind == "lockfile refresh"


def test_dev_only_caps_risk_unless_exploited(tmp_path):
    dev = Item(Source.REPO, "vitest", "vitest", "1", "1.1", vulns_fixed=(Vuln("X", cvss=9.8),), dev=True)
    assert risk(dev) == 6.9
    assert risk(dev.with_(vulns_fixed=(Vuln("X", cvss=9.8, kev=True),))) == 10.0
    (c,) = classify([dev], {})
    assert c.lane == "batch" and any("dev-only" in r for r in c.reasons)
