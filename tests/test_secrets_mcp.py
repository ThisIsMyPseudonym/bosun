"""Secrets in your repos, and the MCP servers your AI tools load."""

import json
import subprocess
from pathlib import Path

import pytest

from bosun import handoff
from bosun.collectors import mcp
from bosun.collectors.mcp import Server, package_spec, parse_config, pinned, server_items, strip_jsonc
from bosun.collectors.secrets import env_has_secrets, is_env_file, parse_grep, scan_repo
from bosun.lanes import classify
from bosun.models import Source
from bosun.serialize import item_to_dict

# realistic-looking but fake credentials, assembled so this file never holds one verbatim
GH = "ghp_" + "a1B2c3D4e5" * 3 + "F6g7H8"
AWS = "AKIA" + "Q3XJ7RT2" + "LMNPZW45"
KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "b3BlbnNzaC1rZXktdjE" + "A" * 50 + "\n-----END OPENSSH PRIVATE KEY-----\n"


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def make_repo(tmp_path):
    """A repo full of fake credentials: committed, in history, untracked. Also used by the tracing tests."""
    r = tmp_path / "app"
    (r / "config").mkdir(parents=True)
    (r / "src").mkdir()
    git(tmp_path, "init", "-q", str(r))
    (r / ".env").write_text(f"DATABASE_URL=postgres://localhost/app\nSTRIPE_SECRET_KEY=sk_test_{'9' * 24}\n")
    (r / ".env.example").write_text("STRIPE_SECRET_KEY=\n")
    (r / "config" / ".env.local").write_text("API_TOKEN=abcdef123456789\n")
    (r / "src" / "client.ts").write_text(f"const docs = 'AKIAIOSFODNN7EXAMPLE';\nconst gh = '{GH}';\n")
    (r / "src" / "aws.py").write_text(f"KEY = '{AWS}'\n")
    (r / "id_rsa").write_text(KEY)
    (r / "deploy.key").write_text("-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n")  # docs placeholder
    (r / "tests" / "fixtures").mkdir(parents=True)
    (r / "tests" / "fixtures" / "server.key").write_text(KEY)
    (r / "tests" / "test_api.py").write_text(f"TOKEN = '{GH}'\n")
    (r / "cert.pem").write_text("-----BEGIN CERTIFICATE-----\nxx\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    git(r, "rm", "-q", "--cached", "config/.env.local")  # removed from the repo, still in history
    (r / ".gitignore").write_text("config/.env.local\n.env.production\n")
    (r / ".env.production").write_text("SECRET=ignored-on-purpose\n")
    (r / "web").mkdir()
    (r / "web" / ".env").write_text("NEXT_PUBLIC_KEY=abcdefghijklmnop\n")  # untracked and not ignored
    git(r, "add", ".gitignore")
    git(r, "commit", "-q", "-m", "stop tracking the local env")
    return r


def test_scan_repo_finds_committed_historic_and_unignored_secrets(repo):
    found = {i.package_id: i for i in scan_repo(repo)}
    assert set(found) == {"env-committed:.env", "env-history:config/.env.local", "env-unignored:web/.env",
                          "key-file:id_rsa", "token-github:src/client.ts:2", "token-aws:src/aws.py:1",
                          "key-file:tests/fixtures/server.key", "token-github:tests/test_api.py:1"}
    assert found["env-committed:.env"].flag_risk == 9.0 and "git rm --cached .env" in found["env-committed:.env"].manual
    assert found["env-unignored:web/.env"].flag_risk == 5.0
    assert all(i.source == Source.SECRET and i.project_scoped and i.available is None for i in found.values())


def test_secret_values_never_leave_the_scan(repo):
    blob = json.dumps([item_to_dict(i) for i in scan_repo(repo)])
    assert GH not in blob and AWS not in blob and "sk_test_" not in blob and "abcdef123456789" not in blob


def test_secrets_land_in_the_right_lanes_and_hand_off_without_values(repo):
    items = [i.with_(project="p", project_name="app", checkouts=(str(repo),)) for i in scan_repo(repo)]
    out = {i.package_id: i for i in classify(items, {})}
    assert out["env-committed:.env"].lane == "now" and out["env-unignored:web/.env"].lane == "batch"
    assert out["key-file:id_rsa"].lane == "now"
    # test code: fixtures rank low, a real-looking token is still worth a look but not today
    assert out["key-file:tests/fixtures/server.key"].flag_risk == 2.5 and out["key-file:tests/fixtures/server.key"].installed == "test fixture"
    assert out["token-github:tests/test_api.py:1"].flag_risk == 6.0 and out["token-github:tests/test_api.py:1"].lane == "batch"
    text = handoff.prompt([out["token-github:src/client.ts:2"]])
    assert text.startswith("# Clean up a secret committed to this repo")
    assert f"`{Path('src/client.ts')}` line 2" in text and "Never print" in text and "fix/remove-secret" in text and GH not in text


def test_env_helpers_and_fakes():
    assert is_env_file("a/.env") and is_env_file(".env.local") and not is_env_file(".env.example")
    assert not is_env_file("environment.ts") and not is_env_file(".envrc")
    assert env_has_secrets("OPENAI_API_KEY=sk-live-value-123\n") and not env_has_secrets("PORT=3000\nAPI_KEY=\n")
    assert not env_has_secrets("API_KEY=your-api-key-here\n") and not env_has_secrets("TOKEN=${TOKEN}\n")
    hits = parse_grep(f"a.md:3:use AKIAIOSFODNN7EXAMPLE\nb.ts:9:x = '{GH}'\nb.ts:12:y = '{GH}'\n")
    assert hits == [("b.ts", 9, "github", "GitHub token", False)]  # the documented AWS example is not a key; one per file
    slack = "xoxb-" + "1234567890" + "-" + "2345678901" + "-" + "aB3dE5fG7hJ9kL1mN3pQ5rS7"
    assert [h[2] for h in parse_grep(f"c.py:1:token='{slack}'\nd.md:4:SLACK_BOT_TOKEN=xoxb-your-bot-token-here\n"
                                     "e.py:2:token=\"xoxb-fake-test-token\"\n")] == ["slack"]


def test_private_key_markers_need_a_key_under_them(tmp_path):
    from bosun.collectors.secrets import in_tests, placeholder_key, token_items

    assert placeholder_key('write_text("-----BEGIN OPENSSH PRIVATE KEY-----\\nxx\\n")') and not placeholder_key(KEY)
    assert placeholder_key("-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----") and not placeholder_key("x")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "fixture.ts").write_text('const k = "-----BEGIN RSA PRIVATE KEY-----\\nMIIE...";\n')
    (tmp_path / "src" / "real.py").write_text('K = """' + KEY + '"""\n')
    hits = parse_grep('src/fixture.ts:1:const k = "-----BEGIN RSA PRIVATE KEY-----\\nMIIE...";\n'
                      'src/real.py:1:K = """-----BEGIN OPENSSH PRIVATE KEY-----\n')
    assert [i.package_id for i in token_items(tmp_path, hits, set())] == ["token-private-key:src/real.py:1"]
    assert all(in_tests(p) for p in ("tests/a.py", "src/test/java/x/GatewayTest.kt", "pkg/a.test.ts", "x/__mocks__/k.js",
                                      "a/fixtures/k.pem", "test_x.py", "pkg/x_test.go", "web/api.spec.js"))
    assert not any(in_tests(p) for p in ("src/app.ts", "trace-test-agent/traces/t.json", "contest/a.py", "src/latest.py"))


def test_google_keys_are_judged_by_context(tmp_path):
    from bosun.collectors.secrets import token_items

    key = "AIza" + "Sy" + "B1c2D3e4F5g6H7i8J9k0L1m2N3o4P5q6R"
    hits = parse_grep(f"src/firebase.ts:4:  apiKey: '{key}',\nsrc/gemini.py:2:client = genai.Client(api_key='{key}')\n")
    assert [(h[0], h[4]) for h in hits] == [("src/firebase.ts", True), ("src/gemini.py", False)]
    web, gemini = token_items(tmp_path, hits, set())
    assert web.flag_risk == 3.0 and "Firebase" in web.manual and gemini.flag_risk == 6.0 and "Gemini" in gemini.manual


# --- MCP ------------------------------------------------------------------------------------

def test_package_specs_and_pinning():
    assert package_spec("npx", ("-y", "@modelcontextprotocol/server-github")) == ("npm", "@modelcontextprotocol/server-github")
    assert package_spec("C:\\Program Files\\nodejs\\npx.cmd", ("-y", "pkg@latest")) == ("npm", "pkg@latest")
    assert package_spec("pnpm", ("dlx", "pkg@2.0.1")) == ("npm", "pkg@2.0.1")
    assert package_spec("uvx", ("--from", "mcp-server-git==0.6.2", "mcp-server-git")) == ("PyPI", "mcp-server-git==0.6.2")
    assert package_spec("node", ("server.js",)) is None and package_spec("docker", ("run", "x")) is None
    assert not pinned("npm", "@scope/pkg") and not pinned("npm", "pkg@latest") and not pinned("npm", "pkg@^1.2")
    assert pinned("npm", "@scope/pkg@1.2.3") and pinned("PyPI", "mcp-server-git==0.6.2") and pinned("PyPI", "x@1.0")
    assert not pinned("PyPI", "mcp-server-fetch") and pinned("npm", "/Users/me/server") and pinned("npm", "./local")


def test_server_findings():
    risky = Server("Cursor", "github", "/h/.cursor/mcp.json", "npx", ("-y", "@modelcontextprotocol/server-github"),
                   env=(("GITHUB_PERSONAL_ACCESS_TOKEN", GH),))
    found = {i.package_id.rsplit("/", 1)[1]: i for i in server_items(risky)}
    assert set(found) == {"unpinned", "plaintext"}
    assert "`@modelcontextprotocol/server-github@<version>`" in found["unpinned"].manual
    assert found["unpinned"].flag_risk == 5.5 and found["plaintext"].installed == "GITHUB_PERSONAL_ACCESS_TOKEN"
    assert GH not in json.dumps([item_to_dict(i) for i in found.values()])
    safe = Server("Cursor", "gh", "f", "npx", ("-y", "@modelcontextprotocol/server-github@2025.4.8"),
                  env=(("GITHUB_TOKEN", "${GITHUB_TOKEN}"),))
    assert server_items(safe) == []
    remote = server_items(Server("Claude Code", "api", "f", url="http://mcp.example.com/sse"))
    assert [i.package_id for i in remote] == ["Claude Code/api/http"]
    assert server_items(Server("Claude Code", "dev", "f", url="http://localhost:3000/mcp")) == []
    out = classify(server_items(risky), {})
    assert {i.lane for i in out} == {"batch"} and {i.kind for i in out} == {"dev"}  # worth fixing, not an emergency


def test_a_plain_http_server_is_listed_by_host_and_port_never_with_its_password():
    for url, shown in (("http://alice:hunter2@mcp.example.com:8080/sse", "mcp.example.com:8080"),
                       ("http://tok3n@mcp.example.com/sse", "mcp.example.com"),
                       ("http://bob:s3cret@[2001:db8::5]:9000/mcp", "[2001:db8::5]:9000")):
        found, = server_items(Server("Claude Code", "api", "f", url=url))
        assert found.installed == shown, url
        stored = json.dumps(item_to_dict(found))
        assert all(secret not in stored for secret in ("hunter2", "alice", "tok3n", "s3cret", "bob")), url


def test_a_password_that_ends_the_address_early_withholds_the_whole_address():
    """An unencoded / ? or # in a password ends urlparse's netloc early: what it finds as host or port is then part
    of the password."""
    for url, secret in (("http://u:secret/x@h.example/", "secret"), ("http://tok:12345#@h.example/", "12345"),
                        ("http://tok:12345?@h.example/", "12345"), ("http://u:p@ssw0rd/x@h.example/mcp", "ssw0rd"),
                        ("http://u:secret@/mcp", "secret")):
        found, = server_items(Server("Claude Code", "api", "f", url=url))
        assert found.installed == "(address withheld)", url
        assert secret not in json.dumps(item_to_dict(found)), url
    found, = server_items(Server("Claude Code", "api", "f", url="http://mcp.example.com:8080/sse?v=1#top"))
    assert found.installed == "mcp.example.com:8080"  # nothing to withhold


def test_old_snapshots_lose_the_http_addresses_they_kept(tmp_path):
    import sqlite3

    from bosun import migrations
    from bosun.store import Store

    path = tmp_path / "bosun.db"
    db = sqlite3.connect(path)
    migrations.migrate(db, None, tuple(m for m in migrations.MIGRATIONS if m.version <= 7))
    db.close()
    old = Store(path)  # (already at the latest: the rows are written as the previous release wrote them)
    leaked = {"key": "mcp:Claude Code/api/http", "source": "mcp", "name": "api", "installed": "alice:hunter2@h:8080"}
    plain = {"key": "brew:jq", "source": "brew", "name": "jq", "installed": "alice:hunter2@1.7"}  # not an address
    for _ in range(2):
        old.save_snapshot([leaked, plain], [])
    old._db.execute("DELETE FROM schema_migrations WHERE version >= 8")
    old._db.commit()
    old._db.close()
    store = Store(path)
    assert migrations.current_version(store._db) == migrations.latest_version()
    blobs = [json.loads(r[0]) for r in store._db.execute("SELECT items FROM snapshots")]
    assert len(blobs) == 2 and all(b[0]["installed"] == "(address withheld)" and b[1] == plain for b in blobs)
    rows = store._db.execute("SELECT item_key, installed FROM snapshot_items ORDER BY item_key").fetchall()
    assert [tuple(r) for r in rows] == [("brew:jq", "alice:hunter2@1.7")] * 2 + [
        ("mcp:Claude Code/api/http", "(address withheld)")] * 2


def _v7_database_with_a_leaked_address(path, snapshots=2):
    """A database as the release before v8 left it: snapshots that kept a plain-http address with a password,
    among a few hundred other items (so each snapshot spans several pages, as a real one does). Past KEEP_BLOBS,
    saving one prunes the oldest, as on a Mac that has run Bosun for a while."""
    import sqlite3

    from bosun import migrations
    from bosun.store import Store

    db = sqlite3.connect(path)
    migrations.migrate(db, None, tuple(m for m in migrations.MIGRATIONS if m.version <= 7))
    db.close()
    old = Store(path)
    leaked = {"key": "mcp:Claude Code/api/http", "source": "mcp", "name": "api", "installed": "alice:s3cretpw@h:8080"}
    others = [{"key": f"brew:tool{n}", "source": "brew", "name": f"tool{n}", "installed": "1.0"} for n in range(300)]
    for _ in range(snapshots):
        old.save_snapshot([*others[:150], leaked, *others[150:]], [])
    old._db.execute("DELETE FROM schema_migrations WHERE version >= 8")
    old._db.commit()
    old._db.close()


def test_the_backups_and_free_pages_lose_the_http_addresses_too(tmp_path):
    """Migrating copies the database to bosun.db.bak-v7 first, with the passwords in it. The old text is also in
    pages pruning freed long before the migration, in the spare room of pages a row was split across, and in the
    WAL. No file beside the database may keep a password."""
    import shutil
    import sqlite3

    from bosun.store import KEEP_BLOBS, Store

    path = tmp_path / "bosun.db"
    _v7_database_with_a_leaked_address(path, snapshots=KEEP_BLOBS + 40)
    shutil.copy(path, tmp_path / "bosun.db.bak-v6")  # an older backup, made by an earlier migration
    store = Store(path)
    assert sorted(p.name for p in tmp_path.glob("bosun.db.bak-v*")) == ["bosun.db.bak-v6", "bosun.db.bak-v7"]
    for bak in tmp_path.glob("bosun.db.bak-v*"):  # kept, scrubbed
        con = sqlite3.connect(bak)
        assert [d["installed"] for r in con.execute("SELECT items FROM snapshots") for d in json.loads(r[0])
                if d["source"] == "mcp"] == ["(address withheld)"] * KEEP_BLOBS
        assert {r[0] for r in con.execute("SELECT installed FROM snapshot_items WHERE source = 'mcp'")} == {
            "(address withheld)"}
        con.close()
    on_disk = {f.name: f.read_bytes().count(b"s3cretpw") for f in tmp_path.iterdir() if f.is_file()}
    assert on_disk and not any(on_disk.values()), on_disk  # the database, its WAL, the backups
    store._db.close()


def test_a_backup_copied_before_another_process_migrated_never_lands_over_the_scrubbed_one(tmp_path, monkeypatch):
    """Two processes open a v7 database at once. One copies it aside and, before that copy is put into place, the
    other backs it up, migrates and scrubs bosun.db.bak-v7. The first copy, put into place then, would be the
    backup with the password, and nothing would scrub it after."""
    import threading

    from bosun import migrations
    from bosun.store import Store

    path = tmp_path / "bosun.db"
    _v7_database_with_a_leaked_address(path)
    real, other = migrations._put_into_place, []

    def the_other_process_meanwhile(tmp, dest):
        if not other:
            other.append(threading.Thread(target=lambda: other.append(Store(path)), daemon=True))
            other[0].start()
            other[0].join(2)  # it finishes here unless this copy holds the write lock it needs
        real(tmp, dest)

    monkeypatch.setattr(migrations, "_put_into_place", the_other_process_meanwhile)
    store = Store(path)
    other[0].join(60)
    assert not other[0].is_alive()
    assert migrations.current_version(store._db) == migrations.latest_version()
    on_disk = {f.name: f.read_bytes().count(b"s3cretpw") for f in tmp_path.iterdir() if f.is_file()}
    assert "bosun.db.bak-v7" in on_disk and not any(on_disk.values()), on_disk
    store._db.close()
    other[1]._db.close()


def test_what_another_machine_sent_loses_the_http_addresses_too(tmp_path):
    """Another machine's summary kept the address it had for its own plain-http server: a machine that never
    reports again would keep it there for good."""
    import sqlite3

    from bosun.store import Store

    path = tmp_path / "bosun.db"
    _v7_database_with_a_leaked_address(path)
    con = sqlite3.connect(path)
    sent = {"v": 1, "machine": {"id": "m2", "name": "laptop", "kind": "mac"}, "projects": [], "items": [
        {"key": "mcp:Cursor/api/http", "source": "mcp", "name": "api", "installed": "bob:s3cretpw@h:9090"},
        {"key": "brew:jq", "source": "brew", "name": "jq", "installed": "1.7"}]}
    con.execute("INSERT INTO peers VALUES ('m2', 'laptop', 'mac', ?, 0)", (json.dumps(sent),))
    con.commit()
    con.close()
    store = Store(path)
    (raw,) = store._db.execute("SELECT data FROM peers WHERE id='m2'").fetchone()
    data = json.loads(raw)
    assert [d["installed"] for d in data["items"]] == ["(address withheld)", "1.7"]
    assert data["machine"] == sent["machine"]
    assert not any(f.read_bytes().count(b"s3cretpw") for f in tmp_path.iterdir() if f.is_file())
    store._db.close()


def test_a_backup_that_cannot_be_opened_does_not_stop_the_migration(tmp_path, caplog):
    from bosun import migrations
    from bosun.store import Store

    path = tmp_path / "bosun.db"
    _v7_database_with_a_leaked_address(path)
    (tmp_path / "bosun.db.bak-v5").write_bytes(b"not a database, but bigger than its header " * 50)
    (tmp_path / "bosun.db.bak-v4").mkdir()  # cannot be opened at all
    caplog.set_level("WARNING", logger="bosun.migrations")
    store = Store(path)
    assert migrations.current_version(store._db) == migrations.latest_version()
    assert b"s3cretpw" not in path.read_bytes()
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert "bosun.db.bak-v5" in warned and "bosun.db.bak-v4" in warned
    assert (tmp_path / "bosun.db.bak-v5").read_bytes().startswith(b"not a database")  # left as it was
    assert (tmp_path / "bosun.db.bak-v4").is_dir()
    assert b"s3cretpw" not in (tmp_path / "bosun.db.bak-v7").read_bytes()  # and the one it could open is scrubbed
    store._db.close()


def test_withholding_the_http_addresses_twice_changes_nothing_more(tmp_path):
    import sqlite3

    from bosun import migrations
    from bosun.store import Store

    path = tmp_path / "bosun.db"
    _v7_database_with_a_leaked_address(path)
    store = Store(path)

    def everything():
        out = []
        for f in sorted([path, *tmp_path.glob("bosun.db.bak-v*")]):
            con = sqlite3.connect(f)
            out.append([tuple(r) for t in ("snapshots", "snapshot_items") for r in con.execute(f"SELECT * FROM {t}")])
            con.close()
        return out

    once, was = everything(), store._db.execute("PRAGMA secure_delete").fetchone()[0]
    with store._lock:
        store._db.execute("BEGIN IMMEDIATE")
        migrations._withhold_http_addresses(store._db)
        store._db.execute("COMMIT")
        migrations._vacuum(store._db)
    assert everything() == once
    assert store._db.execute("PRAGMA secure_delete").fetchone()[0] == was  # the connection is left as it was
    store._db.close()


def test_config_formats():
    claude = json.dumps({"mcpServers": {"fs": {"command": "npx", "args": ["-y", "@x/fs"]}},
                         "projects": {"/Users/me/app": {"mcpServers": {"db": {"type": "http", "url": "http://10.0.0.5/mcp"}}}}})
    found = parse_config("Claude Code", "~/.claude.json", "claude-code", claude)
    assert [(s.client, s.name) for s in found] == [("Claude Code", "fs"), ("Claude Code (app)", "db")]
    vscode = '{\n // my servers\n "servers": {"web": {"url": "https://x.dev//mcp", /* note */ "type": "http",},},\n}'
    assert parse_config("VS Code", "mcp.json", "servers", vscode)[0].url == "https://x.dev//mcp"
    assert json.loads(strip_jsonc('{"a": "http://b", // c\n}')) == {"a": "http://b"}
    codex = '[mcp_servers.fetch]\ncommand = "uvx"\nargs = ["mcp-server-fetch"]\n[mcp_servers.fetch.env]\nKEY = "x"\n'
    [fetch] = parse_config("Codex", "config.toml", "codex", codex)
    assert fetch.command == "uvx" and fetch.args == ("mcp-server-fetch",) and fetch.env == (("KEY", "x"),)


def test_collect_mcp_reads_every_tool_and_skips_broken_files(tmp_path, monkeypatch):
    good, broken = tmp_path / "mcp.json", tmp_path / "broken.json"
    good.write_text(json.dumps({"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}))
    broken.write_text("{ not json")
    monkeypatch.setattr(mcp, "config_files", lambda: [("Cursor", good, "mcpServers"), ("Windsurf", broken, "mcpServers"),
                                                      ("Gemini CLI", tmp_path / "missing.json", "mcpServers")])
    [it] = mcp.collect_mcp(None)
    assert it.key == "mcp:Cursor/fetch/unpinned" and it.installed == "uvx mcp-server-fetch" and it.location == str(good)
