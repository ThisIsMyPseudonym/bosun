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
