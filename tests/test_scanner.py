import dataclasses
import hashlib
import sys

import httpx
import pytest

from bosun import scanner
from bosun.config import load_settings
from bosun.store import Store

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake scanner is a shell script")

ASSET = scanner.asset_name()


def fake(version: str) -> bytes:
    return f'#!/bin/sh\necho "osv-scanner version: {version}"\necho "commit: abc"\n'.encode()


class GitHub:
    """The release download; counts requests."""

    def __init__(self, body: bytes):
        self.body, self.hits = body, []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.hits.append(req.url.path)
        return httpx.Response(200, content=self.body)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def pin(body: bytes) -> dict:
    return {ASSET: hashlib.sha256(body).hexdigest()}


@pytest.fixture
def setup(tmp_path):
    target = tmp_path / "bin" / "osv-scanner"
    s = dataclasses.replace(load_settings(), data_dir=tmp_path, osv_scanner=target, osv_auto_update=True)
    return s, Store(s.db_path), target


def test_asset_names():
    assert scanner.asset_name("Linux", "x86_64") == "osv-scanner_linux_amd64"
    assert scanner.asset_name("Darwin", "arm64") == "osv-scanner_darwin_arm64"
    assert scanner.asset_name("Windows", "AMD64") == "osv-scanner_windows_amd64.exe"
    assert scanner.asset_name("Linux", "aarch64") == "osv-scanner_linux_arm64"
    assert scanner.asset_name("FreeBSD", "x86_64") is None


def test_the_shipped_pin_covers_every_platform_with_real_hashes():
    import re
    assert re.fullmatch(r"\d+\.\d+\.\d+", scanner.PIN_VERSION)
    for system, cpu in (("Linux", "x86_64"), ("Linux", "aarch64"), ("Darwin", "arm64"), ("Darwin", "x86_64"), ("Windows", "AMD64")):
        assert re.fullmatch(r"[0-9a-f]{64}", scanner.PIN_SHA256[scanner.asset_name(system, cpu)])


def test_installs_the_pinned_version_then_makes_no_more_requests(setup):
    s, store, target = setup
    gh = GitHub(fake("2.7.0"))
    kw = dict(target=target, version="2.7.0", hashes=pin(fake("2.7.0")))
    assert scanner.ensure(s, store, gh.client(), **kw) == "osv-scanner 2.7.0 installed"
    assert scanner.version_of(target) == "2.7.0" and gh.hits == [f"/google/osv-scanner/releases/download/v2.7.0/{ASSET}"]
    assert scanner.ensure(s, store, gh.client(), **kw) == "osv-scanner 2.7.0 is current"
    assert len(gh.hits) == 1


def test_updates_an_older_copy_and_never_downgrades(setup):
    s, store, target = setup
    target.parent.mkdir(parents=True)
    target.write_bytes(fake("2.3.8"))
    target.chmod(0o755)
    gh = GitHub(fake("2.7.0"))
    assert scanner.ensure(s, store, gh.client(), target=target, version="2.7.0", hashes=pin(fake("2.7.0"))) == "osv-scanner 2.3.8 → 2.7.0"
    assert scanner.version_of(target) == "2.7.0" and not list(target.parent.glob("*.download"))
    old = GitHub(fake("2.6.0"))
    assert "kept" in scanner.ensure(s, store, old.client(), target=target, version="2.6.0", hashes=pin(fake("2.6.0")))
    assert old.hits == [] and scanner.version_of(target) == "2.7.0"


def test_a_download_must_match_the_hash_in_the_repo_not_one_from_the_release(setup):
    s, store, target = setup
    target.parent.mkdir(parents=True)
    target.write_bytes(fake("2.3.8"))
    target.chmod(0o755)
    swapped = GitHub(fake("2.7.0") + b"# malicious\n")  # whoever swaps the binary can't change the repo's hash
    assert "checksum mismatch" in scanner.ensure(s, store, swapped.client(), target=target, version="2.7.0", hashes=pin(fake("2.7.0")))
    assert scanner.version_of(target) == "2.3.8"
    liar = GitHub(fake("2.6.0"))  # the hash matches, but it isn't the version it claims to be
    assert "reports version 2.6.0" in scanner.ensure(s, store, liar.client(), target=target, version="2.7.0", hashes=pin(fake("2.6.0")))
    assert scanner.version_of(target) == "2.3.8" and not list(target.parent.glob("*.download"))


def test_nothing_installed_and_the_download_fails(setup):
    s, store, target = setup
    with pytest.raises(RuntimeError, match="could not be installed"):
        scanner.ensure(s, store, GitHub(b"not it").client(), target=target, version="2.7.0", hashes=pin(fake("2.7.0")))
    assert not target.exists()


def test_leaves_other_copies_and_opt_outs_alone(setup):
    s, store, target = setup
    gh = GitHub(fake("2.7.0"))
    brew = dataclasses.replace(s, osv_scanner=target.parent / "homebrew-osv-scanner")
    assert scanner.ensure(brew, store, gh.client(), target=target) is None
    assert scanner.ensure(dataclasses.replace(s, osv_auto_update=False), store, gh.client(), target=target) is None
    assert gh.hits == [] and not target.exists()
    assert "no pinned build" in scanner.ensure(s, store, gh.client(), target=target, hashes={})
