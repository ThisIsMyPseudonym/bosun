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
    assert {"ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY"} <= set(spec("claude").override_env)
    assert "OPENAI_BASE_URL" in spec("codex").override_env
    assert {"GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT"} <= set(spec("gemini").override_env)
    assert "XAI_API_KEY" in spec("grok").override_env and "META_API_KEY" in spec("muse").override_env
    assert spec("antigravity").binary == "agy" and spec("claude").light_model == "haiku"
    assert spec("claude").windows == "output" and spec("codex").windows == "output"
    assert spec("antigravity").windows == "probe" and spec("gemini").windows == "estimated"


def test_a_clis_models_can_be_overridden_per_tier():
    from bosun.providers.catalog import model

    assert model("codex", "light", env={}) == "gpt-5.6-luna" and model("codex", "strong", env={}) == "gpt-5.6-terra"
    assert model("grok", "strong", env={"BOSUN_MODEL_GROK_STRONG": "grok-9"}) == "grok-9"
    assert model("muse", "light", env={"BOSUN_MODEL_MUSE_LIGHT": "  "}) == ""  # blank: the CLI's own default


def test_a_cli_is_offered_only_the_jobs_its_capabilities_allow():
    import pytest

    from bosun.providers.catalog import capable

    assert capable("summarize") == ("claude", "codex", "gemini")
    assert capable("fix") == ("claude",)
    assert capable("assistant") == ("claude",)
    assert capable("computer_use") == ("claude",)
    with pytest.raises(KeyError):
        capable("no-such-job")


def test_a_cli_that_is_not_locked_down_never_gets_untrusted_text():
    from dataclasses import replace

    from bosun.providers.catalog import CATALOG, capable, spec

    assert {j for j, job in JOBS.items() if job.untrusted} == {"summarize", "explain", "triage"}
    # No proven per-call way to turn their tools and MCP servers off: Antigravity has no such flag, Grok's tool
    # lists cover built-in tools only, and Muse loads the user's MCP servers with an LLM approval judge on.
    loose_clis = {"antigravity", "grok", "muse"}
    assert {p for p, s in CATALOG.items() if not s.locked_down} == loose_clis
    for job in ("summarize", "explain", "triage"):
        assert not loose_clis & set(capable(job))
    every = frozenset(C)
    loose = {"loose": replace(spec("claude"), id="loose", capabilities=every, locked_down=False)}
    assert capable("summarize", loose) == () and capable("triage", loose) == ()
    assert capable("assistant", loose) == ("loose",)  # its own job, with no release-note text in it


def test_bosun_model_variables_still_choose_claudes_models(monkeypatch):
    from bosun.config import load_settings
    from bosun.providers import catalog

    monkeypatch.setenv("BOSUN_MODEL", "opus")
    monkeypatch.setenv("BOSUN_MODEL_SUMMARIZE", "haiku")
    monkeypatch.setenv("BOSUN_MODEL_CODEX_LIGHT", "gpt-x-mini")
    monkeypatch.delenv("BOSUN_MODEL_CLAUDE_STRONG", raising=False)
    monkeypatch.delenv("BOSUN_MODEL_CLAUDE_LIGHT", raising=False)
    s = load_settings()
    assert catalog.models_for("claude", "strong", s) == "opus"
    assert catalog.models_for("claude", "light", s) == "haiku"
    assert catalog.models_for("claude", "strong") == "opus"
    assert catalog.models_for("claude", "light") == "haiku"
    assert catalog.models_for("codex", "light", s) == "gpt-x-mini"
    assert catalog.models_for("codex", "strong", s) == "gpt-5.6-terra"
