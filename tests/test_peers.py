"""Other machines: read-only summaries traded over SSH, validated on the way in."""

import io
import json
import sys

import pytest

from bosun import peers, proc
from bosun.models import Item, Source
from bosun.serialize import item_to_dict
from bosun.store import Store

GIT = item_to_dict(Item(Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0", lane="now"))
DEP = item_to_dict(Item(Source.REPO, "next", "next", "14.0", "14.2", ecosystem="npm", project="shop",
                        project_name="Shop", lane="plan"))
OLD = item_to_dict(Item(Source.WINGET, "Old.App", "Old", "1", "2", lane="dormant"))


def store_with(tmp_path, name="db", items=(GIT, DEP, OLD)):
    store = Store(tmp_path / name)
    store.save_snapshot(list(items), [], meta={"projects": [{"key": "shop", "name": "Shop", "dormant": False}]})
    return store


def test_summary_shares_machine_findings_and_project_counts_only(tmp_path):
    store = store_with(tmp_path)
    store.set_pref("winget:Old.App", ignored=True)
    s = peers.summary(store, pending=lambda: [{"name": "Google Chrome", "version": "131.0", "app": "chrome", "keys": []}])
    assert [i["key"] for i in s["items"]] == ["winget:Git.Git"]  # no project deps, nothing dormant or ignored
    assert s["projects"] == [{"name": "Shop", "now": 0, "plan": 1, "kev": False}]
    assert s["relaunch"] == [{"name": "Google Chrome", "version": "131.0"}]
    assert peers.parse(json.dumps(s))["machine"]["id"] == peers.identity(store)["id"]


def test_parse_rejects_anything_that_isnt_a_summary(tmp_path):
    good = peers.summary(store_with(tmp_path))
    for bad in ("not json", json.dumps({**good, "v": 2}),
                json.dumps({**good, "machine": {**good["machine"], "id": "../../etc"}}),
                json.dumps({**good, "items": [{**good["items"][0], "name": "x" * 5000}]}),
                b"x" * (peers.MAX_BYTES + 1)):
        with pytest.raises(ValueError):
            peers.parse(bad)
    extra = peers.parse(json.dumps({**good, "items": [{**good["items"][0], "argv": ["rm", "-rf"]}]}))
    assert "argv" not in extra["items"][0]  # unknown fields are dropped


def test_receive_keeps_theirs_and_never_our_own_echo(tmp_path):
    pc, mac = store_with(tmp_path, "pc"), store_with(tmp_path, "mac", items=(GIT,))
    theirs = peers.parse(json.dumps(peers.summary(mac)))
    assert peers.receive(pc, theirs) and [p["id"] for p in pc.peers()] == [theirs["machine"]["id"]]
    assert not peers.receive(pc, peers.parse(json.dumps(peers.summary(pc))))


def test_links_are_validated_and_ssh_cant_take_options_from_them():
    with pytest.raises(ValueError):
        peers.check_link(peers.Link("Mac", "-oProxyCommand=evil"))
    with pytest.raises(ValueError):
        peers.check_link(peers.Link("Mac; rm", "me@host"))
    argv = peers.ssh_argv(peers.Link("Mac", "me@100.64.0.2", "/k", "bosun peer-exchange"))
    assert "BatchMode=yes" in argv and argv[argv.index("--") + 1:] == ["me@100.64.0.2", "bosun peer-exchange"]


def test_exchange_over_ssh_both_ways(tmp_path, monkeypatch):
    pc, mac = store_with(tmp_path, "pc"), store_with(tmp_path, "mac", items=(GIT,))
    sent = {}

    def fake_ssh(argv, timeout=0, stdin=None, **kw):  # what `bosun peer-exchange` does on the other end
        if "--" not in argv:  # not ssh (on a Mac: ps, scutil)
            return proc.Result(tuple(argv), 1, "", "")
        sent["argv"] = argv
        peers.receive(mac, peers.parse(stdin))
        return proc.Result(tuple(argv), 0, json.dumps(peers.summary(mac)), "")

    monkeypatch.setattr(peers.proc, "run", fake_ssh)
    peers.save_links(pc, [peers.Link("MacBook", "me@100.64.0.2")])
    peers.exchange_all(pc)
    assert [p["id"] for p in pc.peers()] == [peers.identity(mac)["id"]]
    assert [p["id"] for p in mac.peers()] == [peers.identity(pc)["id"]]  # the Mac sees the PC too
    v = peers.view(pc)
    assert v[0]["link"] == "MacBook" and v[0]["error"] == "" and v[0]["data"]["items"][0]["key"] == "winget:Git.Git"


def test_unreachable_link_is_shown_with_the_reason(tmp_path, monkeypatch):
    pc = store_with(tmp_path)
    monkeypatch.setattr(peers.proc, "run", lambda argv, **kw: proc.Result(tuple(argv), 255 if "--" in argv else 1, "",
                                                                          "ssh: connect to host 100.64.0.2 port 22: Operation timed out"))
    peers.save_links(pc, [peers.Link("MacBook", "me@100.64.0.2")])
    peers.exchange_all(pc)
    v = peers.view(pc)
    assert v == [{"id": "", "name": "MacBook", "kind": "", "received": None, "data": None, "link": "MacBook",
                  "error": "ssh: connect to host 100.64.0.2 port 22: Operation timed out", "error_at": v[0]["error_at"]}]


def test_peer_exchange_command_reads_stdin_and_answers_on_stdout(tmp_path, monkeypatch, capsys):
    from bosun import cli
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path / "here"))
    other = store_with(tmp_path, "other", items=(GIT,))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(peers.summary(other)).encode())))
    assert cli.main(["peer-exchange"]) == 0
    answer = peers.parse(capsys.readouterr().out)
    assert answer["machine"]["id"] != peers.identity(other)["id"]
    assert [p["name"] for p in Store(tmp_path / "here" / "bosun.db").peers()] == [peers.identity(other)["name"]]
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    assert cli.main(["peer-exchange"]) == 2
