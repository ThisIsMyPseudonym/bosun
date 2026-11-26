"""The catalog: which AI jobs Bosun has, what each needs, and which CLI can do it."""

from bosun.providers.catalog import JOBS, Capability as C


def test_jobs_declare_what_they_need_and_who_starts_them():
    assert JOBS["summarize"].needs == frozenset({C.JSON}) and JOBS["summarize"].background
    assert JOBS["explain"].needs == frozenset({C.JSON}) and not JOBS["explain"].background
    assert JOBS["fix"].needs == frozenset({C.SANDBOXED_AGENT})
    assert JOBS["assistant"].needs == frozenset({C.MCP}) and not JOBS["assistant"].background
    assert JOBS["computer_use"].needs == frozenset({C.MCP, C.VISION})
    assert set(JOBS) == {"summarize", "explain", "triage", "fix", "assistant", "computer_use"}
