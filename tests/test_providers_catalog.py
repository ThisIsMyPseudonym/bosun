"""The catalog: which AI jobs Bosun has, what each needs, and which CLI can do it."""

from bosun.providers.catalog import JOBS, Capability as C


def test_jobs_declare_what_they_need_and_who_starts_them():
    assert JOBS["summarize"].needs == frozenset({C.JSON}) and JOBS["summarize"].background
    assert JOBS["explain"].needs == frozenset({C.JSON}) and not JOBS["explain"].background
    assert JOBS["fix"].needs == frozenset({C.SANDBOXED_AGENT})
    assert JOBS["assistant"].needs == frozenset({C.MCP}) and not JOBS["assistant"].background
    assert JOBS["computer_use"].needs == frozenset({C.MCP, C.VISION})
    assert set(JOBS) == {"summarize", "explain", "triage", "fix", "assistant", "computer_use"}


def test_every_cli_has_a_spec_with_its_login_folder_variable():
    from bosun.providers.catalog import CATALOG, spec

    assert list(CATALOG) == ["claude", "codex", "gemini", "grok", "antigravity", "muse"]
    assert {p: s.home_env for p, s in CATALOG.items()} == {
        "claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "gemini": "GEMINI_CLI_HOME", "grok": "GROK_HOME",
        "antigravity": None, "muse": None}
    assert "ANTHROPIC_API_KEY" in spec("claude").override_env and spec("antigravity").override_env == ()
    assert spec("antigravity").binary == "agy" and spec("claude").light_model == "haiku"
    assert spec("claude").windows == "output" and spec("codex").windows == "output"
    assert spec("antigravity").windows == "probe" and spec("gemini").windows == "estimated"


def test_a_clis_models_can_be_overridden_per_tier():
    from bosun.providers.catalog import model

    assert model("codex", "light", env={}) == "gpt-5.6-luna" and model("codex", "strong", env={}) == "gpt-5.6-terra"
    assert model("grok", "strong", env={"BOSUN_MODEL_GROK_STRONG": "grok-9"}) == "grok-9"
    assert model("muse", "light", env={"BOSUN_MODEL_MUSE_LIGHT": "  "}) == ""  # blank: the CLI's own default
