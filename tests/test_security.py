"""Bosun must not make the machine it watches less safe. Each test here pins one finding of the security review."""

import subprocess
import sys
from pathlib import Path

from bosun import proc, projects
from bosun.collectors.secrets import scan_repo


def _hostile_repo(tmp_path):
    """A repo whose own config runs a command on git status/ls-files/grep (fsmonitor) and on commit/checkout (hooks)."""
    repo, marker = tmp_path / "unzipped", tmp_path / "ran"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    (repo / "a.txt").write_text("x\n")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    # git runs these through sh, which takes a backslash as an escape: give it forward slashes, as Windows accepts
    payload = f'"{Path(sys.executable).as_posix()}" -c "open(r\'{marker.as_posix()}\', \'a\').write(\'x\')"'
    run("config", "core.fsmonitor", payload)
    hooks = repo / "hooks"
    hooks.mkdir()
    for name in ("post-checkout", "pre-commit", "post-commit"):
        (hooks / name).write_text(f"#!/bin/sh\n{payload}\n")
        (hooks / name).chmod(0o755)
    run("config", "core.hooksPath", str(hooks))
    return repo, marker


def test_the_trap_is_real(tmp_path):
    repo, marker = _hostile_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True)
    assert marker.exists()  # plain git runs the repo's command: this is what Bosun must never do


def test_a_repo_cannot_run_code_through_bosuns_git_calls(tmp_path):
    repo, marker = _hostile_repo(tmp_path)
    for args in (("status", "--porcelain"), ("ls-files", "-z"), ("ls-files", "--others", "--exclude-standard"),
                 ("grep", "-n", "x"), ("log", "-1", "--format=%ct"), ("remote", "get-url", "origin"),
                 ("worktree", "add", "-b", "bosun/fix-x", str(tmp_path / "wt"), "HEAD"),
                 ("-c", "user.name=B", "-c", "user.email=b@b", "commit", "-q", "--allow-empty", "-m", "x")):
        proc.git(repo, *args)
    scan_repo(repo)  # the secrets scan, as the daily refresh runs it
    projects.build_projects({str(repo)})
    assert not marker.exists()


def _signed_commit_repo(tmp_path):
    """A repo whose own config shows signatures (log.showSignature) with a program it names (gpg.program), and whose
    last commit carries a signature: `git log` then runs that program to check it."""
    repo, marker = tmp_path / "signed", tmp_path / "ran"
    repo.mkdir()
    run = lambda *a, **kw: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,  # noqa: E731
                                          text=True, **kw).stdout.strip()
    run("init", "-q")
    (repo / "a.txt").write_text("x\n")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    (repo / ".env").write_text("A=1\n")  # the secrets scan's `git log` lists commits that added one
    run("add", "-A")
    tree, parent = run("write-tree"), run("rev-parse", "HEAD")
    body = (f"tree {tree}\nparent {parent}\nauthor t <t@t> 1700000000 +0000\ncommitter t <t@t> 1700000000 +0000\n"
            "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEEnotarealsignature\n -----END PGP SIGNATURE-----\n"
            "\nsigned\n")
    made = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"], check=True,
                          capture_output=True, input=body.encode())  # bytes: text mode on Windows writes CRLF
    run("update-ref", "HEAD", made.stdout.decode().strip())
    fake_gpg = tmp_path / "fake-gpg"
    fake_gpg.write_text(f"#!/bin/sh\n\"{Path(sys.executable).as_posix()}\" -c "
                        f"\"open(r'{marker.as_posix()}', 'a').write('x')\"\n")
    fake_gpg.chmod(0o755)
    run("config", "log.showSignature", "true")
    run("config", "gpg.program", fake_gpg.as_posix())
    return repo, marker


def test_a_repo_cannot_run_code_through_bosuns_git_log(tmp_path):
    repo, marker = _signed_commit_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%ct"], capture_output=True)
    assert marker.exists()  # the trap is real: plain git runs the repo's program
    marker.unlink()
    scan_repo(repo)  # the secrets scan's git log --all ... --name-only
    projects.build_projects({str(repo)})  # the project's age: git log -1 --format=%ct
    assert not marker.exists()


def test_the_docs_name_every_git_call_on_a_fix_branch_that_a_repository_can_still_reach():
    """GIT_SAFE can't stop `git push` running a program the repository's config names for it (receivepack,
    sshCommand, credential.helper), nor `git commit` running the signing program it names when it turns on
    commit.gpgsign, and `gh pr create` runs git without GIT_SAFE at all. Both docs say so, and
    stay two different documents (f89a8bf once wrote the policy over the guide)."""
    root = Path(__file__).parent.parent
    guide, policy = (root / "docs" / "security.md").read_text(encoding="utf-8"), (root / "SECURITY.md").read_text(
        encoding="utf-8")
    # Two different documents: the guide explains Bosun's safety in plain terms, the policy is the threat model.
    assert "\n## Security in plain terms\n" in guide and "\n## Safety\n" in guide
    assert "\n## Threat model\n" in policy and "\n## Reporting a vulnerability\n" in policy
    assert "## Threat model" not in guide and "## Security in plain terms" not in policy
    for doc, raw in (("docs/security.md", guide), ("SECURITY.md", policy)):
        text = " ".join(raw.split())
        text = text[text.index("Fix branches"):]  # the exceptions (the guide names `gpg.program` before, for git log)
        for name in ("`status`", "`add`", "`commit`", "`worktree add`", "`git push`", "`gh pr create`",
                     "receivepack", "`core.sshCommand`", "`credential.helper`", "`gpg.program`",
                     "`gpg.ssh.program`", "`gpg.x509.program`", "`commit.gpgsign`", "a person's click"):
            assert name in text, (doc, name)


def test_autopilot_never_installs_a_major_jump_on_a_models_word():
    from bosun import autopilot

    def item(pid, installed, available, source="brew"):
        return {"key": f"{source}:{pid}", "source": source, "package_id": pid, "installed": installed,
                "available": available, "upgradable": True, "lane": "batch"}  # lane says drop-in: the model's rating

    assert autopilot.eligible(item("jq", "1.7.0", "1.7.1"), {})
    assert not autopilot.eligible(item("pnpm", "9.1.0", "12.0.0"), {})
    assert autopilot.eligible(item("google-chrome", "141.0", "142.0", "brew-cask"), {})  # a browser's major is routine
    assert autopilot.eligible(item("gcloud-cli", "560.0.0", "561.0.0", "brew-cask"), {})
    assert not autopilot.eligible(item("chrome-remote-thing", "1.0", "2.0", "repo"), {})


def test_redaction_covers_the_shapes_the_review_found():
    from bosun import redact

    leaks = {
        "https://ghp_abcdefghijklmnopqrstuvwxyz0123456789@dev.azure.com/x": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "https://abcdefghijklmnop1234@dev.azure.com/o/p": "abcdefghijklmnop1234",
        "redis://:hunter2pass@cache:6379": "hunter2pass",
        "//registry.npmjs.org/:_auth=dXNlcjpwYXNzd29yZA==": "dXNlcjpwYXNzd29yZA",
        "curl -u deploy:s3cr3tpass https://x": "s3cr3tpass",
        "AccountKey=abc123def456ghi789==;EndpointSuffix=core": "abc123def456ghi789",
        'PASSPHRASE="correct horse battery staple"': "battery staple",
        "GH_PAT=abcdef123456": "abcdef123456",
        "dop_v1_" + "a1" * 32: "a1a1a1a1",
        "lin_api_" + "Z9" * 20: "Z9Z9Z9",
    }
    for raw, secret in leaks.items():
        assert secret not in redact.text(raw), raw
    for plain in ("ssh://git@github.com/x/y.git", "git@github.com:x/y.git", "author: Alex Example",
                  "pattern matched 12 files", "path: /usr/local/bin", "brew upgrade --cask orcaslicer"):
        assert redact.text(plain) == plain
    assert redact.value({"auth": "abcdef1234", "cookie": "zzzzzzzz", "GH_PAT": "qqqqqqqq", "path": "/x"}) == {
        "auth": redact.MASK, "cookie": redact.MASK, "GH_PAT": redact.MASK, "path": "/x"}
    assert redact.argv(["curl", "-u", "user:pass1234", "x"]) == ["curl", "-u", redact.MASK, "x"]


def test_a_remote_with_a_token_in_it_never_becomes_a_project_key():
    assert projects.normalise_remote("https://deploy:ghp_tok123456@github.com/Acme/App.git") == "https://github.com/acme/app"
    assert projects.normalise_remote("git@github.com:Acme/App.git") == "https://github.com/acme/app"


def test_everything_written_down_is_redacted_and_stays_parseable(tmp_path):
    import logging

    from bosun import trace
    from bosun.store import Store

    store = Store(tmp_path / "data" / "bosun.db")
    trace.configure(store.path)
    try:
        with trace.run("refresh") as r:
            r.partial("brew: failed with token=abcdef1234567890")
            with trace.span("step", "big", blob="x" * 500_000, many=list(range(5000))):
                pass
            proc.run([sys.executable, "-c", "pass", "name", '{"env": {"NOTION_INTEGRATION": "opaque-value-1"}}'], private=(4,))
    finally:
        trace.configure(None)
    t = store.trace(r.trace_id)  # the oversized span must not make the run unreadable
    assert "abcdef1234567890" not in t["run"]["error"]
    assert "opaque-value-1" not in str(t["spans"]) and "[not recorded]" in str(t["spans"])
    rec = logging.LogRecord("bosun.x", logging.INFO, __file__, 1, "pushed to https://u:tok12345678@github.com/x", None, None)
    rec.trace = ""
    assert "tok12345678" not in trace.RedactingFormatter("%(message)s").format(rec)


def test_the_data_directory_is_private(tmp_path):
    import os
    import stat

    from bosun.store import Store

    loose = tmp_path / "data"
    loose.mkdir(mode=0o755)
    Store(loose / "bosun.db")
    if os.name != "nt":
        assert stat.S_IMODE(loose.stat().st_mode) == 0o700  # an existing one is tightened too


def test_mcp_pinning_keeps_a_config_private_and_checks_the_version(tmp_path):
    import os
    import stat

    import httpx
    import pytest

    from bosun import mcp_pin

    cfg = tmp_path / "mcp.json"
    cfg.write_text("{}")
    cfg.chmod(0o600)
    mcp_pin._write_json(cfg, {"mcpServers": {}})
    if os.name != "nt":
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    hostile = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"dist-tags": {"latest": "--registry=http://evil"}})))
    with pytest.raises(mcp_pin.PinError, match="doesn't look like one"):
        mcp_pin.settled("npm", "pkg", hostile)


def test_redaction_is_fast_on_hostile_output():
    """A command Bosun runs can print anything. Redaction once took minutes on one long line."""
    import time

    from bosun import redact

    start = time.perf_counter()
    for s in ("x" * 500_000, "x_" * 250_000, "a://" * 100_000, "token " * 100_000, "a." * 250_000):
        redact.text(s)
    assert time.perf_counter() - start < 5


def test_release_notes_are_never_fetched_from_this_machine_or_network():
    import httpx

    from bosun.enrich import release_notes
    from bosun.enrich.release_notes import ReleaseNotes, public_https

    fake_dns = {"example.org": "93.184.215.14", "intranet.example.org": "10.0.0.5", "rebind.example.org": "127.0.0.1"}
    resolve = lambda host, port, **kw: [(2, 1, 6, "", (fake_dns.get(host, host), port))]  # noqa: E731
    assert public_https("https://example.org/changelog", resolve)
    for bad in ("http://example.org/x", "https://127.0.0.1:8787/api/state", "https://[::1]/x", "https://192.168.1.1/",
                "https://169.254.169.254/latest/meta-data", "https://intranet.example.org/", "https://rebind.example.org/",
                "file:///etc/passwd", "https://"):
        assert not public_https(bad, resolve), bad

    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.url.path == "/hop":  # a public page that redirects inward
            return httpx.Response(302, headers={"location": "https://127.0.0.1:8787/api/state"})
        return httpx.Response(200, content=b"x" * 5_000_000, headers={"content-type": "text/plain"})

    rn = ReleaseNotes(http=httpx.Client(transport=httpx.MockTransport(handler)))
    orig = release_notes.public_https
    release_notes.public_https = lambda url: orig(url, resolve)
    try:
        assert rn._page("https://example.org/hop") == "" and seen == ["https://example.org/hop"]
        assert len(rn._page("https://example.org/big")) == release_notes.MAX_PAGE_BYTES
        assert rn._page("http://example.org/plain") == ""
    finally:
        release_notes.public_https = orig


# --- only this user may talk to the dashboard ---------------------------------------------------------------
def _dashboard(tmp_path):
    from fastapi.testclient import TestClient

    from bosun.config import load_settings
    from bosun.models import Item, Source
    from bosun.serialize import item_to_dict
    from bosun.server import create_app
    from bosun.store import Store

    s = load_settings()
    store = Store(tmp_path / "bosun.db")
    store.save_snapshot([item_to_dict(Item(Source.BREW, "jq", "jq", "1.6", "1.7"))], [])
    app = create_app(s, store, "t" * 32, session_key="s" * 48)
    return s, TestClient(app, base_url=f"http://127.0.0.1:{s.port}", follow_redirects=False)


def test_nothing_about_this_machine_is_served_without_a_session(tmp_path):
    s, c = _dashboard(tmp_path)
    for path in ("/api/state", "/api/item?key=brew:jq", "/api/jobs", "/api/fixes", "/api/runs", "/api/usage",
                 "/api/runs/abcdef12", "/api/runs/abcdef12/diagnose", "/api/state?visible=1"):
        assert c.get(path).status_code == 401, path
    page = c.get("/")
    assert page.status_code == 401 and "t" * 32 not in page.text and "Open Bosun from its icon" in page.text
    assert c.post("/api/autopilot", json={"on": True}, headers={"x-bosun-token": "t" * 32}).status_code == 401
    assert c.get("/health").json() == {"bosun": True}  # all `bosun open` needs to know
    assert c.get("/static/app.js").status_code == 200 and c.get("/favicon.ico").status_code == 200
    assert c.get("/api/state", headers={"host": "evil.example:8787"}).status_code == 403


def test_a_login_code_works_once_briefly_and_only_from_the_key(tmp_path):
    from bosun import auth

    s, c = _dashboard(tmp_path)
    name = auth.cookie_name(s.port)
    assert c.get("/login?code=nonsense").status_code == 401
    assert c.get(f"/login?code={auth.login_code('wrong-key' * 6)}").status_code == 401
    assert c.get(f"/login?code={auth.login_code('s' * 48, now=1)}").status_code == 401  # long expired
    code = auth.login_code("s" * 48)
    r = c.get(f"/login?code={code}")
    assert r.status_code == 303 and r.headers["location"] == "/"
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=strict" in set_cookie and "s" * 48 not in set_cookie
    assert c.get(f"/login?code={code}").status_code == 401  # the URL in the browser's history is now useless
    c.cookies.clear()
    assert c.get("/api/state").status_code == 401
    c.cookies.set(name, auth.cookie_value("s" * 48))
    assert c.get("/api/state").status_code == 200 and "t" * 32 in c.get("/").text
    c.cookies.set(name, "forged")
    assert c.get("/api/state").status_code == 401


def test_a_non_ascii_cookie_login_code_or_page_token_is_refused_not_an_error(tmp_path):
    from bosun import auth

    s, c = _dashboard(tmp_path)
    name = auth.cookie_name(s.port)
    ts, nonce, _ = auth.login_code("s" * 48).split(".")
    assert c.get(f"/login?code={ts}.{nonce}.caf%C3%A9").status_code == 401  # compare_digest refused str like this
    signed = auth.cookie_value("s" * 48)
    # header bytes arrive as latin-1 text: UTF-8 bytes above 0x7f become non-ASCII characters
    assert c.get("/api/state", headers={"cookie": f"{name}=café".encode()}).status_code == 401
    c.cookies.set(name, signed)
    assert c.post("/api/autopilot", json={"on": True},
                  headers={"x-bosun-token": "té".encode() + b"t" * 30}).status_code == 403
    assert auth.Sessions("s" * 48).valid_cookie("café") is False


def test_the_key_file_is_private_and_stable(tmp_path):
    import os
    import stat

    from conftest import REAL_LOAD_KEY

    key = REAL_LOAD_KEY(tmp_path / "data")
    path = tmp_path / "data" / "session.key"
    assert len(key) >= 48 and REAL_LOAD_KEY(tmp_path / "data") == key
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_bosun_open_signs_the_browser_in(tmp_path, monkeypatch):
    from bosun import auth, launcher

    opened = []
    monkeypatch.setattr(launcher, "dashboard_up", lambda url: True)
    monkeypatch.setattr(launcher, "open_window", lambda url: opened.append(url) or "test")
    assert launcher.open_dashboard("http://127.0.0.1:8787/", data_dir=tmp_path)
    code = opened[0].split("/login?code=")[1]
    assert opened[0].startswith("http://127.0.0.1:8787/login?code=") and auth.Sessions("k" * 48).redeem(code)


# --- Branch + test: the agent and whatever it installs are contained -------------------------------------------
def _dep(tmp_path, **kw):
    from bosun.models import Item, Source

    return Item(Source.REPO, kw.pop("pid", "minimist"), "x", "1.2.5", "1.2.6", location=str(tmp_path), ecosystem=kw.pop("eco", "npm"),
                lockfile=str(tmp_path / "package-lock.json"), checkouts=(str(tmp_path),), project="p", **kw)


def test_the_fixer_cannot_run_git_open_ended_scripts_or_add_from_a_url(tmp_path):
    from bosun import fixer

    npm, py = fixer.patterns([_dep(tmp_path)]), fixer.patterns([_dep(tmp_path, eco="PyPI")])
    assert not any(p.startswith(("git", "pnpm run *", "npm run *", "uv add", "curl", "sh", "node", "python -c")) for p in npm + py)
    argv = fixer.claude_fix_argv("claude", [_dep(tmp_path)], "sonnet", 3.0)
    assert "Bash(git" not in " ".join(argv)
    docker = fixer.claude_fix_argv("claude", [_dep(tmp_path, eco="docker")], "sonnet", 3.0)
    assert "Bash" not in docker[docker.index("--tools") + 1]  # a base-image bump is file edits: no shell at all


def test_removing_malware_never_runs_its_code(tmp_path):
    from bosun import fixer
    from bosun.models import Vuln

    bad = _dep(tmp_path, pid="evil-pkg", vulns_fixed=(Vuln("MAL-2026-1", "malware"),))
    assert bad.malicious
    allowed = fixer.patterns([bad, _dep(tmp_path)])  # one malicious item puts the whole run on the strict list
    for p in allowed:
        assert "test" not in p and "run" not in p and "npx" not in p, p
        if "install" in p or p.startswith("npm ci"):
            assert "--ignore-scripts" in p or "--package-lock-only" in p or "--lockfile-only" in p or "skip-build" in p, p
    prompt = fixer.fix_prompt([bad], "")
    assert "Never run its code" in prompt and "Do not run tests or builds" in prompt


def test_sandbox_settings_confine_writes_network_and_credentials(tmp_path, monkeypatch):
    import json

    from bosun import fixer

    wt, cache, data = tmp_path / "wt", tmp_path / ".cache-1", tmp_path / "data"
    sb = fixer.sandbox_settings(wt, cache, [_dep(tmp_path)], data)["sandbox"]
    assert sb["enabled"] is True and sb["allowUnsandboxedCommands"] is False
    assert sb["network"]["allowedDomains"] == ["registry.npmjs.org", "registry.yarnpkg.com"]
    assert sb["filesystem"]["allowWrite"] == [str(wt), str(cache)]
    deny = sb["filesystem"]["denyRead"]
    assert "~/.ssh" in deny and "~/.aws" in deny and "~/.git-credentials" in deny and str(data) in deny
    assert not fixer.worktrees_root(data).is_relative_to(data)
    monkeypatch.setenv("BOSUN_FIX_ALLOW_HOSTS", "npm.internal.example, ")
    assert "npm.internal.example" in fixer.sandbox_settings(wt, cache, [_dep(tmp_path)], data)["sandbox"]["network"]["allowedDomains"]
    argv = fixer.claude_fix_argv("claude", [_dep(tmp_path)], "sonnet", 3.0, {"sandbox": sb})
    assert json.loads(argv[argv.index("--settings") + 1])["sandbox"]["enabled"] is True
    assert all(str(cache) in v for k, v in fixer.cache_env(cache).items() if k != "npm_config_update_notifier")


def test_permit_refuses_unknown_actors():
    import pytest
    from bosun.registry import ActionContext, Actions, Forbidden, SPECS

    actions = Actions()
    for actor in ("", "stranger", "admin", "root"):
        with pytest.raises(Forbidden, match="unknown actor"):
            actions.permit(SPECS["list_jobs"], ActionContext(actor=actor))


def test_the_test_suite_cannot_reach_the_real_data_folder():
    from bosun import machine
    from bosun.config import load_settings

    assert load_settings().data_dir != machine.default_data_dir() and "bosun-tests-" in str(load_settings().data_dir)


def test_ci_installs_exactly_what_the_lockfile_pins():
    """A bare `uv sync`, or a bare `uv run` (which syncs first), rewrites uv.lock when it is out of date, so CI could
    test packages nobody reviewed. `--frozen` is not enough: it installs the lock without checking that it still
    matches pyproject.toml. `--locked` fails instead."""
    import re

    workflows = sorted((Path(__file__).parent.parent / ".github" / "workflows").glob("*.yml"))
    calls = [(w.name, line.strip()) for w in workflows for line in w.read_text(encoding="utf-8").splitlines()
             if re.search(r"\buv (sync|run)\b", line)]
    assert any("uv sync" in line for _, line in calls), calls
    uv_flag = r"\buv (sync|run)( -[-\w]+(=\S+)?)* --locked(\s|$)"  # uv's own flag, before what `uv run` runs
    assert all(re.search(uv_flag, line) for _, line in calls), calls
