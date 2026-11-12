from bosun import redact
from bosun.redact import MASK

GH = "ghp_" + "a1B2c3D4e5" * 3 + "F6g7H8"
AWS = "AKIA" + "Q3XJ7RT2" + "LMNPZW45"
ANT = "sk-ant-api03-" + "x" * 40
STRIPE = "sk_test_" + "9" * 24
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjE" + "A" * 50 + "\n-----END OPENSSH PRIVATE KEY-----"


def test_known_token_shapes():
    for secret in (GH, AWS, ANT, STRIPE, JWT, "xoxb-1234567890-abcdefghij", "github_pat_" + "A1" * 20):
        out = redact.text(f"before {secret} after")
        assert secret not in out and out.startswith("before ") and out.endswith(" after"), secret


def test_assignments_headers_urls_and_keys():
    cases = {
        "API_TOKEN=abcdef123456789": "abcdef123456789",
        "export OPENAI_API_KEY='sk-zzzzzzzzzzzzzzzzzzzzzzzz'": "sk-zzzz",
        '{"client_secret": "s3cr3t-value-here"}': "s3cr3t-value-here",
        "Authorization: Bearer abc.def.ghi-jklmnop": "abc.def.ghi-jklmnop",
        "curl -H 'authorization: token 0123456789abcdef'": "0123456789abcdef",
        "https://user:hunter2hunter2@github.com/x/y.git": "hunter2hunter2",
        "--password supersecret1": "supersecret1",
        "DB_PASSWORD: correct-horse-battery": "correct-horse-battery",
        PEM: "b3BlbnNzaC1rZXktdjE",
    }
    for raw, secret in cases.items():
        assert secret not in redact.text(raw), raw
    assert "https://user:[redacted]@github.com" in redact.text("https://user:pw123456@github.com/x")


def test_ordinary_text_survives():
    for plain in ("brew upgrade --cask orcaslicer", "Error: Permission denied @ apply2files - /opt/homebrew/x",
                  "collector brew: 11 items", "npm ERR! code E404", "winget upgrade --id Git.Git --exact",
                  "refresh done in 96s: 69 items, 0 errors", "https://github.com/astral-sh/uv/releases"):
        assert redact.text(plain) == plain


def test_argv_masks_flag_values_and_cuts_long_args():
    out = redact.argv(["tool", "--token", "abc123456789", "--api-key=zzzzzzzzzzzz", "--json-schema", "{" + "x" * 1000 + "}"])
    assert out[:3] == ["tool", "--token", MASK]
    assert "zzzzzzzzzzzz" not in out[3]
    assert out[5].endswith("(1002 chars)") and len(out[5]) < 400


def test_nested_values_and_secret_named_keys():
    data = {"argv": ["git", f"https://x:{GH}@github.com"], "env": {"NVD_API_KEY": "plainvalue1234", "PATH": "/usr/bin"},
            "n": 3, "ok": True}
    out = redact.value(data)
    assert GH not in str(out) and out["env"]["NVD_API_KEY"] == MASK and out["env"]["PATH"] == "/usr/bin"
    assert out["n"] == 3 and out["ok"] is True


def test_this_processes_secret_env_values_are_masked(monkeypatch):
    monkeypatch.setattr(redact, "_env_values", None)
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "q9w8e7r6t5y4")
    assert redact.text("value q9w8e7r6t5y4 leaked") == f"value {MASK} leaked"
    redact.add_secrets(["askpass-channel-xyz"])
    assert "askpass-channel-xyz" not in redact.text("fd askpass-channel-xyz")
    monkeypatch.setattr(redact, "_env_values", None)
