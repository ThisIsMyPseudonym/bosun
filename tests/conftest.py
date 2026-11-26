"""Shared test setup."""

import os

import tempfile

# Tests never touch the real data folder: several build Settings from the environment, and opening the real
# database with newer code would migrate it under a running dashboard.
os.environ["BOSUN_DATA_DIR"] = tempfile.mkdtemp(prefix="bosun-tests-")
os.environ.setdefault("BOSUN_OSV_AUTO_UPDATE", "0")  # a test refresh never downloads into bin/
os.environ.setdefault("BOSUN_SELF_UPDATE", "0")  # never git-pull the checkout under test
os.environ.setdefault("BOSUN_NO_ICONS", "1")  # never read this machine's app bundles (tests/test_icons.py passes its own reader)

# Test repos must not see this machine's git setup: a global excludesFile that ignores .env (common on
# Macs) would hide the very files the secrets tests commit. Identity comes from env instead of ~/.gitconfig.
os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
# git reads ~/.config/git/ignore even with no global config, so point excludesFile at nothing explicitly
os.environ["GIT_CONFIG_COUNT"] = "1"
os.environ["GIT_CONFIG_KEY_0"] = "core.excludesFile"
os.environ["GIT_CONFIG_VALUE_0"] = os.devnull
for _var, _val in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
                   ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t")):
    os.environ.setdefault(_var, _val)


import pytest  # noqa: E402

SESSION_KEY = "k" * 48

from bosun import auth as _auth  # noqa: E402

REAL_LOAD_KEY = _auth.load_key  # for the tests of the key file itself


@pytest.fixture(autouse=True)
def _test_session_key(monkeypatch):
    """The dashboard under test never reads or creates the real session key."""
    monkeypatch.setattr("bosun.auth.load_key", lambda data_dir: SESSION_KEY)


def signed_in(port: int = 8787) -> dict:
    """Cookies for a TestClient that has opened Bosun from its icon."""
    from bosun import auth

    return {auth.cookie_name(port): auth.cookie_value(SESSION_KEY)}


@pytest.fixture(autouse=True)
def _no_real_app_bundle(monkeypatch, tmp_path):
    """Tests never open, rebuild or overwrite the Bosun app in ~/Applications."""
    monkeypatch.setattr("bosun.launcher.MAC_APP", tmp_path / "Applications" / "Bosun.app")


@pytest.fixture(autouse=True)
def _no_waiting_period(monkeypatch):
    """Autopilot's waiting period is off unless a test is about it (tests/test_autopilot.py turns it back on):
    most tests build an item and ask what Autopilot does with it, without saying how long it has been on offer."""
    monkeypatch.setattr("bosun.autopilot.WAIT_DAYS", 0)


@pytest.fixture(autouse=True)
def _no_real_lock_scan(monkeypatch, tmp_path):
    """A self-update test never runs this machine's osv-scanner (which asks OSV.dev) on the uv.lock it commits:
    the scanner is missing unless a test puts one in (tests/test_selfupdate.py)."""
    monkeypatch.setattr("bosun.selfupdate._scanner", lambda: tmp_path / "no-osv-scanner")


@pytest.fixture(autouse=True)
def _no_notification_loop(monkeypatch):
    """Every dashboard a test builds would start the notifier's loop, a daemon thread that outlives the test and
    ticks every two minutes: listing processes and reading apps while a later test has swapped proc.run for a
    recorder. Tests call Notifier.tick themselves (tests/test_notify.py)."""
    monkeypatch.setattr("bosun.notify.Notifier.run", lambda self, interval=0: None)


@pytest.fixture(autouse=True)
def _no_real_store_apps(monkeypatch, tmp_path):
    """The check for an App Store copy before a cask install looks in an empty folder, never this Mac's
    /Applications (tests/test_storeguard.py gives it its own)."""
    monkeypatch.setattr("bosun.storeguard.roots", lambda home=None: (tmp_path / "no-store-apps",))


@pytest.fixture(autouse=True)
def providers_ready(request, monkeypatch):
    """Every AI CLI is installed and passes its contract check without a process being started or PATH being read:
    tests never run a real CLI's --version or --help. A test of the check itself is marked `real_contract`
    (tests/test_providers_contract.py)."""
    if request.node.get_closest_marker("real_contract"):
        return
    from bosun.providers import accounts, contract
    from bosun.providers.catalog import CATALOG

    monkeypatch.setattr(contract, "check", lambda store, provider, binary, now=None:
                        contract.ContractStatus(provider, "test", True, (), ""))
    monkeypatch.setattr(accounts, "installed", lambda: {p: f"/test/bin/{s.binary}" for p, s in CATALOG.items()})
