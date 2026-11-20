"""Phase D tests: CveRecords lookup budget, container digest/tag parsing, EOL cycle matching,
MC.freshToken() concurrency, server CSP/security headers, and BosunWindow.swift hardening."""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import signed_in

from bosun import launcher, machine
from bosun.collectors.containers import image_item, parse_compose, parse_dockerfile, parse_ref
from bosun.config import load_settings
from bosun.enrich import cverecord
from bosun.enrich.cverecord import CveRecords
from bosun.enrich.eol import EolClient, match_cycle
from bosun.server import create_app
from bosun.store import Store

TOKEN = "t" * 32


# --- 1. CveRecords overall lookup budget ------------------------------------------------------------------------

def _cve_json(cve: str) -> dict:
    return {
        "dataType": "CVE_RECORD",
        "cveMetadata": {"cveId": cve, "state": "PUBLISHED"},
        "containers": {
            "cna": {
                "affected": [{"vendor": "Acme", "product": "Widget",
                              "versions": [{"version": "0", "lessThan": "2.0", "status": "affected"}]}],
                "descriptions": [{"lang": "en", "value": f"flaw {cve}"}],
            }
        },
    }


def test_cverecords_budget_ignores_unrelated_wall_time_and_enforces_lookup_time(tmp_path):
    now = [1000.0]
    seen_timeouts: list[httpx.Timeout] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_timeouts.append(request.extensions["timeout"])
        cve = request.url.path.rsplit("/", 1)[-1]
        now[0] += 4.0  # simulate a 4-second lookup
        return httpx.Response(200, json=_cve_json(cve))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rec = CveRecords(Store(tmp_path / "db.sqlite"), client, budget_s=6.0, clock=lambda: now[0])

    # 120 seconds pass in unrelated refresh steps (e.g. Claude summarization) before any CVE lookup.
    now[0] += 120.0
    assert not rec.spent
    assert rec.remaining == 6.0

    # First lookup: remaining=6.0 -> timeout clamped to min(10, 6)=6 and connect=min(5, 6)=5.
    r1 = rec.record("CVE-2026-1001")
    assert r1 is not None and r1.id == "CVE-2026-1001"
    assert rec.elapsed_s == 4.0 and rec.remaining == 2.0 and not rec.spent
    assert seen_timeouts[0]["read"] == 6.0 and seen_timeouts[0]["connect"] == 5.0

    # Second lookup: remaining=2.0 -> both read and connect timeouts clamped to 2.0.
    r2 = rec.record("CVE-2026-1002")
    assert r2 is not None and r2.id == "CVE-2026-1002"
    assert seen_timeouts[1]["read"] == 2.0 and seen_timeouts[1]["connect"] == 2.0
    assert rec.elapsed_s == 8.0 and rec.spent

    # Budget is now spent: uncached lookup is skipped, cached lookup still succeeds.
    assert rec.record("CVE-2026-1003") is None
    assert rec.lookups == 2
    assert rec.record("CVE-2026-1001") is not None


def test_cverecords_failed_lookup_still_counts_toward_budget(tmp_path):
    now = [10.0]

    def handler(request: httpx.Request) -> httpx.Response:
        now[0] += 5.0
        raise httpx.ReadTimeout("timed out")

    rec = CveRecords(Store(tmp_path / "db.sqlite"), httpx.Client(transport=httpx.MockTransport(handler)),
                     budget_s=4.0, clock=lambda: now[0])
    assert rec.record("CVE-2026-2001") is None
    assert rec.elapsed_s == 5.0 and rec.spent
    assert rec.record("CVE-2026-2002") is None
    assert rec.lookups == 1


# --- 2. Container :latest / digest comparisons ------------------------------------------------------------------

def test_digest_pinned_images_are_not_floating_and_keep_eol_checks(tmp_path):
    digest = "sha256:" + "a1b2c3d4" * 8
    bare = parse_ref(f"ubuntu@{digest}")
    assert (bare.repo, bare.tag, bare.digest, bare.floating) == ("ubuntu", "", digest, False)

    tagged_latest = parse_ref(f"redis:latest@{digest}")
    assert (tagged_latest.repo, tagged_latest.tag, tagged_latest.floating) == ("redis", "latest", False)

    tagged_alpine = parse_ref(f"nginx:alpine@{digest}")
    assert tagged_alpine.floating is False

    bad_digest = parse_ref("ubuntu@not-a-digest")
    assert bad_digest.digest == "" and bad_digest.floating is True

    data = {
        "nodejs": [{"cycle": "24", "eol": "2028-04-30", "lts": True, "latest": "24.9.0"},
                   {"cycle": "18", "eol": "2025-04-30", "lts": True, "latest": "18.20.8"}],
        "debian": [{"cycle": "12", "codename": "Bookworm", "eol": "2026-06-10", "latest": "12.12"},
                   {"cycle": "11", "codename": "Bullseye", "eol": "2026-08-31", "latest": "11.11"}],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        product = req.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        return httpx.Response(200, json=data[product]) if product in data else httpx.Response(404)

    eol = EolClient(Store(tmp_path / "db"), httpx.Client(transport=httpx.MockTransport(handler)),
                    today=dt.date(2026, 10, 23))
    # Digest-pinned images without EOL cycles are healthy (not falsely flagged as floating :latest)
    assert image_item(bare, Path("/r"), Path("/r/Dockerfile"), eol) is None
    assert image_item(tagged_latest, Path("/r"), Path("/r/Dockerfile"), eol) is None
    assert image_item(tagged_alpine, Path("/r"), Path("/r/Dockerfile"), eol) is None
    # Digest-pinned image on an EOL runtime still reports unsupported, with floating=False
    old_pinned = image_item(parse_ref(f"node:18-bullseye@{digest}"), Path("/r"), Path("/r/Dockerfile"), eol)
    assert old_pinned is not None and old_pinned.unsupported and not old_pinned.floating


def test_compose_unset_tag_and_dockerfile_self_alias():
    compose = "services:\n  a:\n    image: org/app:${TAG}\n  b:\n    image: org/app:${TAG:-1.2}\n"
    assert parse_compose(compose) == ["org/app:1.2"]
    df = "FROM alpine:3.20 AS alpine\nFROM alpine AS final\nFROM python:${UNSET}\n"
    assert parse_dockerfile(df) == ["alpine:3.20"]


# --- 3. EOL multi-digit major / minor matching ------------------------------------------------------------------

def test_eol_match_cycle_handles_multidigit_prefixes_revisions_and_unlisted_minors():
    macos = [
        {"cycle": "15", "codename": "Sequoia", "eol": False},
        {"cycle": "14", "codename": "Sonoma", "eol": "2026-09-15"},
        {"cycle": "10.15", "codename": "Catalina", "eol": "2022-09-12"},
        {"cycle": "10.13", "codename": "High Sierra", "eol": "2020-12-01"},
    ]
    assert match_cycle(macos, "10.13.6")["cycle"] == "10.13"
    assert match_cycle(macos, "14.7.2")["cycle"] == "14"
    # Unlisted ancient macOS 10.9 must NOT silently match 10.15
    assert match_cycle(macos, "10.9.5") is None

    python = [{"cycle": "3.14", "eol": "2030-10-31"}, {"cycle": "3.11", "eol": "2027-10-31"}]
    assert match_cycle(python, "3.11.9")["cycle"] == "3.11"
    # Unlisted EOL minor 3.5 must NOT fall back to newest 3.x (3.14)
    assert match_cycle(python, "3.5") is None
    assert match_cycle(python, "3.5.10") is None
    # Floating major "3" still resolves to newest 3.x
    assert match_cycle(python, "3")["cycle"] == "3.14"

    mysql = [{"cycle": "8.4", "eol": "2032-04-30"}, {"cycle": "8.0", "eol": "2026-04-30"}]
    assert match_cycle(mysql, "8.0_1")["cycle"] == "8.0"

    # Multi-digit minor comparison when cycles are not pre-sorted (10.15 > 10.9 > 10.2)
    unsorted = [{"cycle": "10.9"}, {"cycle": "10.2"}, {"cycle": "10.15"}, {"cycle": "1.2"}]
    assert match_cycle(unsorted, "10")["cycle"] == "10.15"
    assert match_cycle(unsorted, "1")["cycle"] == "1.2"
    assert match_cycle(unsorted, "") is None


# --- 4. MC.freshToken() concurrency race ------------------------------------------------------------------------

def test_fresh_token_deduplicates_concurrent_403s_without_locking_stale():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    ui_js = Path(__file__).resolve().parents[1] / "src" / "bosun" / "static" / "ui.js"
    script = f"""
    const fs = require("fs");
    globalThis.window = globalThis;
    globalThis.document = {{
      querySelector: () => ({{ content: "old-tok" }}),
      createElement: () => ({{ addEventListener() {{}}, setAttribute() {{}}, append() {{}}, style: {{}} }}),
    }};
    let serverToken = "new-tok";
    let rootFetches = 0;
    let apiCalls = [];
    let releaseRoot;
    const rootGate = new Promise((r) => {{ releaseRoot = r; }});
    globalThis.fetch = async (url, opts = {{}}) => {{
      if (url === "/") {{
        rootFetches++;
        await rootGate;
        return {{ ok: true, status: 200, text: async () => `<meta name="bosun-token" content="${{serverToken}}">` }};
      }}
      const tok = opts.headers && opts.headers["x-bosun-token"];
      apiCalls.push({{ url, tok }});
      if (tok !== serverToken) {{
        return {{ ok: false, status: 403, json: async () => ({{ error: "missing or invalid token" }}) }};
      }}
      return {{ ok: true, status: 200, json: async () => ({{ ok: true, url }}) }};
    }};
    eval(fs.readFileSync({json.dumps(str(ui_js))}, "utf8"));
    (async () => {{
      // 1. Two concurrent requests both hit 403 -> single `/` fetch, both succeed, MC.stale stays false.
      const p1 = MC.api("/api/one", {{ method: "POST" }});
      const p2 = MC.api("/api/two", {{ method: "POST" }});
      await new Promise((r) => setTimeout(r, 20));
      releaseRoot();
      const [r1, r2] = await Promise.all([p1, p2]);
      if (!r1.ok || !r2.ok) throw new Error("concurrent retries failed");
      if (rootFetches !== 1) throw new Error("expected 1 root fetch, got " + rootFetches);
      if (MC.stale) throw new Error("MC.stale was falsely set");
      if (MC.TOKEN !== "new-tok") throw new Error("token not updated");

      // 2. Staggered call: freshToken("old-tok") after MC.TOKEN already moved -> returns true without refetching `/`.
      const again = await MC.freshToken("old-tok");
      if (!again || rootFetches !== 1) throw new Error("staggered refresh refetched or failed");

      // 3. Genuine failure: `/` returns the same token -> MC.stale becomes true and rejects.
      serverToken = "different-from-page";
      globalThis.fetch = async (url) => {{
        if (url === "/") return {{ ok: true, status: 200, text: async () => '<meta name="bosun-token" content="new-tok">' }};
        return {{ ok: false, status: 403, json: async () => ({{ error: "missing or invalid token" }}) }};
      }};
      let threw = false;
      try {{ await MC.api("/api/fail", {{ method: "POST" }}); }} catch (e) {{ threw = true; }}
      if (!threw || !MC.stale) throw new Error("genuine mismatch did not mark stale");
      console.log("OK");
    }})().catch((e) => {{ console.error(e); process.exit(1); }});
    """
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert r.stdout.strip() == "OK"


# --- 5. CSP & security response headers in server.py ------------------------------------------------------------

def test_server_sets_strict_csp_security_headers_and_cache_control(tmp_path):
    s = load_settings()
    app = create_app(s, Store(tmp_path / "db.sqlite"), TOKEN)
    client = TestClient(app, base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))

    for path in ("/", "/api/state"):
        r = client.get(path)
        assert r.status_code == 200
        csp = r.headers["content-security-policy"]
        for directive in ("default-src 'self'", "script-src 'self'", "style-src 'self'", "img-src 'self'",
                          "font-src 'self'", "connect-src 'self'", "object-src 'none'", "base-uri 'none'",
                          "form-action 'self'", "frame-ancestors 'none'"):
            assert directive in csp, (path, directive, csp)
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["cross-origin-opener-policy"] == "same-origin"
        assert r.headers["cross-origin-resource-policy"] == "same-origin"
        assert "camera=()" in r.headers["permissions-policy"]
        assert r.headers["cache-control"] == "no-store"

    # Static assets still revalidate via ETag (`no-cache`), with global nosniff & CSP
    static = client.get("/static/ui.js")
    assert static.status_code == 200
    assert static.headers["cache-control"] == "no-cache"
    assert static.headers["x-content-type-options"] == "nosniff"
    assert "object-src 'none'" in static.headers["content-security-policy"]


# --- 6. BosunWindow.swift hardening -----------------------------------------------------------------------------

def test_bosun_window_swift_hardening_invariants_and_typecheck():
    src = launcher.MAC_SOURCE.read_text(encoding="utf-8")
    # Origin + main-frame validation for clipboard bridge
    assert "message.frameInfo.isMainFrame" in src
    assert "isHomeOrigin(message.frameInfo.securityOrigin)" in src
    assert "func isHome(_ url: URL) -> Bool" in src
    # Safe download filename + Downloads containment + reveal file + failure cleanup
    assert "func safeFilename(_ raw: String) -> String" in src
    assert "hasPrefix(root + \"/\")" in src
    assert "activateFileViewerSelecting" in src
    assert "didFailWithError" in src
    # Single-flight signIn, cancellation ignore, bounded retries, WebContent termination recovery
    assert "var signingIn = false" in src
    assert "NSURLErrorCancelled" in src
    assert "maxRetries" in src
    assert "webViewWebContentProcessDidTerminate" in src
    # Proper JSON encoding and non-zero exit on self-test failure
    assert "JSONSerialization.data" in src
    assert 'exit((obj["ok"] as? Bool) == true ? 0 : 1)' in src
    # Preserve current route on Cmd+R when already on home
    assert "web.reload()" in src
    # Standard Undo/Redo & Zoom menus
    assert 'withTitle: "Undo"' in src and 'withTitle: "Redo"' in src and '"Zoom In"' in src

    if machine.IS_MAC and (compiler := launcher.swiftc()):
        r = subprocess.run(
            ["xcrun", compiler, "-swift-version", "5", "-typecheck", str(launcher.MAC_SOURCE),
             "-framework", "Cocoa", "-framework", "WebKit"],
            capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0, f"swiftc -typecheck failed:\n{r.stderr}\n{r.stdout}"


def test_cverecord_uses_default_monotonic_when_clock_omitted(tmp_path):
    assert cverecord.CveRecords(Store(tmp_path / "db.sqlite")).remaining == cverecord.BUDGET_S
    assert sys.version_info >= (3, 13)
