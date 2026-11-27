"""The test suite never spawns a real AI CLI to check its contract, unless a test asks for the real check."""

import pytest

from bosun import proc
from bosun.providers import contract
from bosun.store import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "db.sqlite") as s:
        yield s


def test_the_suite_treats_every_cli_as_installed_and_passing_unless_asked(store, monkeypatch):
    def spawned(*a, **k):
        raise AssertionError(f"a test started {a[0] if a else '?'}")
    monkeypatch.setattr(proc, "run", spawned)
    got = contract.check(store, "codex", "codex")
    assert got.ok and got.provider == "codex" and got.missing == ()


@pytest.mark.real_contract
def test_real_contract_marker_turns_the_shortcut_off(store, monkeypatch):
    monkeypatch.setattr(proc, "run", lambda *a, **k: proc.Result(("x",), 127, "", "command not found"))
    assert not contract.check(store, "codex", "codex").ok


def test_the_suite_sees_every_catalog_cli_as_installed_without_looking_at_path(monkeypatch):
    from bosun.providers import accounts
    from bosun.providers.catalog import CATALOG

    monkeypatch.setenv("PATH", "")
    assert set(accounts.installed()) == set(CATALOG)


@pytest.mark.real_contract
def test_the_real_installed_looks_at_path(monkeypatch):
    from bosun.providers import accounts

    monkeypatch.setenv("PATH", "")
    assert accounts.installed() == {}
