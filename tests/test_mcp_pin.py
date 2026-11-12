"""Pinning MCP servers: the right arg changes, backups first, Claude Code through its own command."""

import json

import httpx
import pytest

from bosun import autopilot, mcp_pin
from bosun.collectors.mcp import collect_mcp
from bosun.mcp_pin import PinError, pin, pin_args


def registry():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "registry.npmjs.org" in url:
            return httpx.Response(200, json={"dist-tags": {"latest": "2026.10.1"}}) if "missing" not in url else httpx.Response(404)
        return httpx.Response(200, json={"info": {"version": "1.4.0"}})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_pin_args_for_each_runner():
    assert pin_args("npx", ["-y", "@playwright/mcp@latest"], "1.2.3") == ["-y", "@playwright/mcp@1.2.3"]
    assert pin_args("npx", ["-y", "--package", "@x/y", "y-cmd"], "2.0.0") == ["-y", "--package", "@x/y@2.0.0", "y-cmd"]
    assert pin_args("uvx", ["mcp-server-fetch"], "1.4.0") == ["mcp-server-fetch@1.4.0"]
    assert pin_args("uvx", ["--from", "mcp-server-git>=0.5", "mcp-server-git"], "1.4.0") == ["--from", "mcp-server-git==1.4.0", "mcp-server-git"]
    assert pin_args("pnpm", ["dlx", "srv"], "3.0.0") == ["dlx", "srv@3.0.0"]
    assert pin_args("node", ["server.js"], "1") is None


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("bosun.collectors.mcp.Path.home", lambda: tmp_path)
    monkeypatch.setattr("bosun.mcp_pin.Path.home", lambda: tmp_path)
    (tmp_path / ".cursor").mkdir()
    return tmp_path


def test_cursor_config_pinned_in_place_with_a_backup(home):
    cfg = home / ".cursor" / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"pw": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
                                              "local": {"command": "node", "args": ["srv.js"]}}}))
    [item] = [i for i in collect_mcp(None) if i.package_id == "Cursor/pw/unpinned"]
    assert item.fix_kind == "pin" and "Pin lets Bosun do it" in item.manual
    d = {"package_id": item.package_id, "location": item.location}
    done: set[str] = set()
    assert pin(d, registry(), done) == "@playwright/mcp@latest → @playwright/mcp 2026.10.1"
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["pw"]["args"] == ["-y", "@playwright/mcp@2026.10.1"] and data["mcpServers"]["local"]["args"] == ["srv.js"]
    [backup] = list(cfg.parent.glob("mcp.json.bosun-backup-*"))
    assert "@latest" in backup.read_text() and done == {str(cfg)}
    assert pin(d, registry(), done) == "already pinned"
    assert not [i for i in collect_mcp(None) if i.package_id == "Cursor/pw/unpinned"]  # the finding is gone


def test_configs_with_comments_stay_manual(home):
    cfg = home / ".cursor" / "mcp.json"
    cfg.write_text('{"mcpServers": {"pw": {"command": "npx", "args": ["-y", "pkg"]}}}')
    [item] = collect_mcp(None)
    cfg.write_text('// mine\n{"mcpServers": {"pw": {"command": "npx", "args": ["-y", "pkg"]}}}')
    with pytest.raises(PinError, match="comments"):
        pin({"package_id": item.package_id, "location": item.location}, registry(), set())


def test_claude_code_goes_through_claude_mcp_and_rolls_back(home, monkeypatch):
    proj_a, proj_b = home / "work" / "app", home / "old" / "app"
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)
    cfg = home / ".claude.json"
    cfg.write_text(json.dumps({"mcpServers": {"ctx": {"type": "stdio", "command": "npx", "args": ["-y", "@upstash/context7-mcp"]}},
                               "projects": {str(proj_a): {"mcpServers": {"fs": {"command": "npx", "args": ["-y", "@x/fs"]}}},
                                            str(proj_b): {"mcpServers": {"fs": {"command": "npx", "args": ["--yes", "@x/fs@latest"]}}}}}))
    calls = []
    def fake_run(argv, timeout=0, cwd=None, **kw):
        calls.append((argv[2:], cwd))
        return mcp_pin.proc.Result(tuple(argv), 1 if "add-json" in argv and "fail" in json.dumps(calls) else 0, "", "")
    monkeypatch.setattr(mcp_pin, "claude_bin", lambda: "/usr/bin/claude")
    monkeypatch.setattr(mcp_pin.proc, "run", fake_run)
    assert pin({"package_id": "Claude Code/ctx/unpinned", "location": str(cfg)}, registry(), set()).endswith("2026.10.1")
    assert calls[0] == (["remove", "ctx", "-s", "user"], str(home))
    assert calls[1][0][:2] == ["add-json", "ctx"] and calls[1][0][3:] == ["-s", "user"]
    assert json.loads(calls[1][0][2]) == {"type": "stdio", "command": "npx", "args": ["-y", "@upstash/context7-mcp@2026.10.1"]}
    calls.clear()
    pin({"package_id": "Claude Code (app)/fs/unpinned", "location": str(cfg)}, registry(), set())
    adds = {cwd: json.loads(argv[2])["args"] for argv, cwd in calls if argv[0] == "add-json"}
    assert adds == {str(proj_a): ["-y", "@x/fs@2026.10.1"], str(proj_b): ["--yes", "@x/fs@2026.10.1"]}  # each keeps its own flags
    assert all(argv[-2:] == ["-s", "local"] for argv, _ in calls)
    calls.clear()
    calls.append((["fail"], None))  # make the next add-json fail
    with pytest.raises(PinError, match="original is back"):
        pin({"package_id": "Claude Code/ctx/unpinned", "location": str(cfg)}, registry(), set())
    assert json.loads(calls[-1][0][2])["args"] == ["-y", "@upstash/context7-mcp"]  # the original went back in


def test_left_over_windows_project_keys_are_skipped():
    from bosun.collectors.mcp import parse_config

    data = {"projects": {"C:/AI/app": {"mcpServers": {"fs": {"command": "npx", "args": ["@x/fs"]}}},
                         "C:\\AI\\app": {"mcpServers": {"old": {"command": "npx", "args": ["@x/old"]}}},
                         "C:\\Other": {"mcpServers": {"kept": {"command": "npx", "args": ["@x/kept"]}}}}}
    names = [s.name for s in parse_config("Claude Code", "c.json", "claude-code", json.dumps(data))]
    assert names == ["fs", "kept"]  # the C:\\AI\\app twin never loads; a backslash key with no twin still counts


def test_unpublished_packages_explain_themselves():
    with pytest.raises(PinError, match="can't start this server either"):
        mcp_pin.settled("npm", "missing-pkg", registry())


NOW = 1_800_000_000.0
DAY = 86400


def iso(days_ago: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(NOW - days_ago * DAY, dt.UTC).isoformat().replace("+00:00", "Z")


def serving(record: dict) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=record)))


def npm(latest: str, ages: dict[str, float | None], deprecated: tuple[str, ...] = ()) -> httpx.Client:
    return serving({"dist-tags": {"latest": latest},
                    "versions": {v: ({"deprecated": "do not use"} if v in deprecated else {}) for v in ages},
                    "time": {"created": iso(900), "modified": iso(0), **{v: iso(a) for v, a in ages.items() if a is not None}}})


def pypi(latest: str, files: dict[str, list[dict]]) -> httpx.Client:
    return serving({"info": {"version": latest}, "releases": files})


@pytest.fixture
def wait(monkeypatch):
    monkeypatch.setattr(autopilot, "WAIT_DAYS", 3)


def test_a_release_that_has_been_out_three_days_is_pinned_as_is(wait):
    assert mcp_pin.settled("npm", "srv", npm("1.4.2", {"1.4.1": 40, "1.4.2": 3.1}), NOW) == ("1.4.2", "")


def test_a_new_release_is_passed_over_for_the_one_before_it(wait):
    version, note = mcp_pin.settled("npm", "srv", npm("1.4.2", {"1.3.0": 90, "1.4.1": 40, "1.4.2": 1}), NOW)
    assert version == "1.4.1" and note == "1.4.2 is a day old, so this is the newest release that has been out 3 days"


def test_the_pick_is_by_version_not_by_date_and_never_above_the_current_release(wait):
    # 1.9.9 is a later fix to the old line; 3.0.0-beta.1 and 3.0.0 are not what the registry calls current
    ages = {"1.9.8": 300, "2.0.0": 30, "1.9.9": 10, "2.0.1": 0.5, "3.0.0-beta.1": 20, "3.0.0": 20}
    assert mcp_pin.settled("npm", "srv", npm("2.0.1", ages), NOW)[0] == "2.0.0"


def test_test_releases_count_only_when_the_current_release_is_one(wait):
    ages = {"1.0.0": 50, "2.0.0-rc.1": 20, "2.0.0-rc.2": 1}
    assert mcp_pin.settled("npm", "srv", npm("2.0.0-rc.2", ages), NOW)[0] == "2.0.0-rc.1"
    assert mcp_pin.settled("npm", "srv", npm("2.0.0", {**ages, "2.0.0": 1}), NOW)[0] == "1.0.0"


def test_deprecated_undated_and_future_dated_releases_are_not_candidates(wait):
    ages = {"1.0.0": 50, "1.1.0": 30, "1.2.0": None, "1.3.0": -2, "1.4.0": 1}
    assert mcp_pin.settled("npm", "srv", npm("1.4.0", ages, deprecated=("1.1.0",)), NOW)[0] == "1.0.0"


def test_nothing_old_enough_is_not_pinned_and_says_when(wait):
    with pytest.raises(PinError, match="no release of srv has been out 3 days yet.*try again after"):
        mcp_pin.settled("npm", "srv", npm("0.1.1", {"0.1.0": 2, "0.1.1": 1}), NOW)
    with pytest.raises(PinError, match="doesn't list srv 9.9.9"):
        mcp_pin.settled("npm", "srv", npm("9.9.9", {"0.1.0": 20}), NOW)


def test_a_pypi_release_is_as_old_as_its_newest_file_and_withdrawn_ones_are_skipped(wait):
    def f(days_ago, yanked=False):
        return {"upload_time_iso_8601": iso(days_ago), "yanked": yanked}
    files = {"1.2.0": [f(200)], "1.3.0": [f(100), f(0.2)],  # a file added to an old release this morning
             "1.4.0": [f(60, yanked=True)], "1.5.0": [], "1.6.0": [f(1)], "1.6.0.dev1": [f(30)]}
    version, note = mcp_pin.settled("PyPI", "srv", pypi("1.6.0", files), NOW)
    assert version == "1.2.0" and note.startswith("1.6.0 is a day old")


def test_a_version_that_is_not_a_version_never_reaches_the_config(wait):
    with pytest.raises(PinError, match="doesn't look like one"):
        mcp_pin.settled("npm", "srv", npm("--registry=evil", {"1.0.0": 30}), NOW)


def test_the_pin_says_why_it_stepped_back(home, wait, monkeypatch):
    cfg = home / ".cursor" / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"pw": {"command": "npx", "args": ["-y", "srv@latest"]}}}))
    monkeypatch.setattr(mcp_pin.time, "time", lambda: NOW)
    said = pin({"package_id": "Cursor/pw/unpinned", "location": str(cfg)}, npm("1.4.2", {"1.4.1": 40, "1.4.2": 2}), set())
    assert said == "srv@latest → srv 1.4.1 (1.4.2 is 2 days old, so this is the newest release that has been out 3 days)"
    assert json.loads(cfg.read_text())["mcpServers"]["pw"]["args"] == ["-y", "srv@1.4.1"]


def test_an_age_is_counted_in_whole_days(wait):
    note = mcp_pin.settled("npm", "srv", npm("1.4.2", {"1.4.1": 40, "1.4.2": 2.6}), NOW)[1]
    assert note.startswith("1.4.2 is 2 days old")
    assert mcp_pin.settled("npm", "srv", npm("1.4.2", {"1.4.1": 40, "1.4.2": 0.3}), NOW)[1].startswith("1.4.2 is less than a day old")


def test_a_package_deprecated_throughout_is_still_pinned_and_says_so(wait):
    ages = {"1.0.0": 400, "1.1.0": 300, "1.2.0": 200}
    assert mcp_pin.settled("npm", "srv", npm("1.2.0", ages, deprecated=tuple(ages)), NOW) == (
        "1.2.0", "its maker has deprecated this package")
    # only an old release deprecated: it is passed over, and nothing is said about the package
    assert mcp_pin.settled("npm", "srv", npm("1.2.0", {**ages, "1.2.0": 1}, deprecated=("1.1.0",)), NOW)[0] == "1.0.0"
    with pytest.raises(PinError, match="pin it by hand"):
        mcp_pin.settled("npm", "srv", npm("9.9.9", ages), NOW)
