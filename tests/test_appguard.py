"""appguard: who signed an app, whether its signature holds, and a kept copy to go back to."""

import os

import sys

import pytest

from bosun import appguard
from bosun.proc import Result

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Mac app guarding uses os.getuid, symlinks and /bin/cp; it runs on macOS and Linux only")

DESCRIBED = """Executable=/Applications/Blender.app/Contents/MacOS/Blender
Identifier=org.blenderfoundation.blender
Format=app bundle with Mach-O thin (arm64)
Authority=Developer ID Application: Stichting Blender Foundation (68UA947AUU)
Authority=Developer ID Certification Authority
Authority=Apple Root CA
TeamIdentifier=68UA947AUU
"""
AD_HOC = "Identifier=example\nSignature=adhoc\nTeamIdentifier=not set\n"


def ok(stdout="", stderr=""):
    return Result((), 0, stdout, stderr)


def failed(stderr="nope"):
    return Result((), 1, "", stderr)


def app(root, name="Blender.app", version="5.2.3"):
    path = root / "Applications" / name
    (path / "Contents").mkdir(parents=True)
    (path / "Contents" / "Info.plist").write_text(version)
    return path


def copying(argv, **kw):
    """`cp -cpR src dst` as a plain recursive copy."""
    import shutil
    shutil.copytree(argv[-2], argv[-1], symlinks=True)
    return ok()


def test_signer_is_the_team_id_and_the_leaf_certificate():
    who = appguard.parse_codesign(DESCRIBED)
    assert who == appguard.Signer("68UA947AUU", "Developer ID Application: Stichting Blender Foundation (68UA947AUU)")
    assert appguard.parse_codesign(AD_HOC) is None and appguard.parse_codesign("") is None


def test_signer_reads_codesigns_stderr_with_a_fixed_command(tmp_path):
    path, seen = app(tmp_path), []
    who = appguard.signer(path, run=lambda argv, **kw: seen.append(argv) or ok(stderr=DESCRIBED))
    assert who.team == "68UA947AUU" and seen == [["/usr/bin/codesign", "-dv", "--verbose=2", str(path)]]
    assert appguard.signer(path, run=lambda argv, **kw: failed("code object is not signed at all")) is None


def test_same_signer_needs_a_team_on_both_sides():
    a, b = appguard.Signer("AAAAAAAAAA"), appguard.Signer("BBBBBBBBBB")
    assert appguard.same_signer(a, appguard.Signer("AAAAAAAAAA", "another certificate, same team"))
    assert not appguard.same_signer(a, b) and not appguard.same_signer(None, a) and not appguard.same_signer(a, None)


def test_assess_needs_the_signature_and_gatekeeper(tmp_path):
    path, seen = app(tmp_path), []
    assert appguard.assess(path, run=lambda argv, **kw: seen.append(argv[:2]) or ok()) == ""
    assert seen == [["/usr/bin/codesign", "--verify"], ["/usr/sbin/spctl", "--assess"]]
    assert "signature does not verify" in appguard.assess(path, run=lambda argv, **kw: failed("a sealed resource is missing"))
    rejected = lambda argv, **kw: ok() if argv[0].endswith("codesign") else failed("rejected")  # noqa: E731
    assert appguard.assess(path, run=rejected) == "Gatekeeper does not accept it (rejected)"


def test_bundle_is_only_an_app_directly_inside_an_applications_folder(tmp_path):
    path = app(tmp_path)
    assert appguard.bundle(str(path), home=tmp_path) == path
    nested = path / "Contents" / "Helper.app"
    nested.mkdir()
    elsewhere = tmp_path / "Downloads" / "Evil.app"
    elsewhere.mkdir(parents=True)
    link = tmp_path / "Applications" / "Link.app"
    os.symlink(elsewhere, link)
    for bad in (nested, elsewhere, link, tmp_path / "Applications" / "Missing.app", tmp_path / "Applications",
                f"{tmp_path}/Applications/../Downloads/Evil.app", "Blender.app", ""):
        assert appguard.bundle(str(bad), home=tmp_path) is None, bad


def test_running_means_a_process_inside_the_bundle():
    ps = "/Applications/Blender.app/Contents/MacOS/Blender\n/usr/sbin/cfprefsd\n"
    assert appguard.running(appguard.Path("/Applications/Blender.app"), ps)
    assert not appguard.running(appguard.Path("/Applications/Blend.app"), ps)  # a name that is only a prefix


def test_keep_holds_one_version_per_app_and_restore_puts_it_back(tmp_path):
    path, root = app(tmp_path), tmp_path / "kept"
    first = appguard.keep(path, root, "org.blenderfoundation.blender", "5.2.3", run=copying)
    assert first == root / "org.blenderfoundation.blender" / "5.2.3" / "Blender.app"
    assert appguard.kept(root, "org.blenderfoundation.blender") == (first, "5.2.3")
    (path / "Contents" / "Info.plist").write_text("5.3.0")  # the update happened
    second = appguard.keep(path, root, "org.blenderfoundation.blender", "5.3.0", run=copying)
    assert not first.exists() and appguard.kept(root, "org.blenderfoundation.blender") == (second, "5.3.0")
    (path / "Contents" / "Info.plist").write_text("broken")
    appguard.restore(second, path, run=copying)
    assert (path / "Contents" / "Info.plist").read_text() == "5.3.0"
    assert [p.name for p in path.parent.iterdir()] == ["Blender.app"]  # nothing left beside it


def test_a_copy_that_fails_changes_nothing(tmp_path):
    path, root = app(tmp_path), tmp_path / "kept"
    with pytest.raises(appguard.GuardError):
        appguard.keep(path, root, "org.example.app", "1.0", run=lambda argv, **kw: failed("No space left on device"))
    assert appguard.kept(root, "org.example.app") is None
    copy = appguard.keep(path, root, "org.example.app", "1.0", run=copying)
    (path / "Contents" / "Info.plist").write_text("what is there now")
    with pytest.raises(appguard.GuardError):
        appguard.restore(copy, path, run=lambda argv, **kw: failed("No space left on device"))
    assert (path / "Contents" / "Info.plist").read_text() == "what is there now"


def test_keep_falls_back_to_a_plain_copy_where_the_volume_cannot_clone(tmp_path):
    path, seen = app(tmp_path), []

    def run(argv, **kw):
        seen.append(argv[1])
        return failed("clonefile: Operation not supported") if argv[1] == "-cpR" else copying(argv)

    assert appguard.keep(path, tmp_path / "kept", "org.example.app", "1.0", run=run).is_dir() and seen == ["-cpR", "-pR"]


@pytest.mark.parametrize("bundle_id", ["../../etc", "", "a/b"])
def test_keep_refuses_a_bundle_id_that_is_not_plain(tmp_path, bundle_id):
    with pytest.raises(appguard.GuardError):
        appguard.keep(app(tmp_path), tmp_path / "kept", bundle_id, "1.0", run=copying)


@pytest.mark.parametrize("version, folder", [("../1.0", "1.0"), ("1.0/x", "1.0_x"), ("4.0 beta (345)", "4.0_beta_345"),
                                             ("", "unknown"), ("../..", "unknown")])
def test_any_version_is_kept_under_a_plain_folder_inside_the_apps_own(tmp_path, version, folder):
    root = tmp_path / "kept"
    copy = appguard.keep(app(tmp_path), root, "org.example.app", version, run=copying)
    assert copy.parent == root / "org.example.app" / folder


def test_prune_drops_copies_past_their_time(tmp_path):
    path, root = app(tmp_path), tmp_path / "kept"
    old = appguard.keep(path, root, "org.example.old", "1.0", run=copying)
    new = appguard.keep(path, root, "org.example.new", "1.0", run=copying)
    long_ago = old.parent.stat().st_mtime - 15 * 86400
    os.utime(old.parent, (long_ago, long_ago))
    assert appguard.prune(root) == 1
    assert not (root / "org.example.old").exists() and new.is_dir()
    assert appguard.prune(tmp_path / "never-made") == 0


def test_set_aside_keeps_only_the_five_newest_and_they_are_listed_newest_first(tmp_path, monkeypatch):
    root = tmp_path / "kept"
    real = appguard.time.strftime
    stamps = iter(f"2026010{n}-120000" for n in range(1, 8))
    monkeypatch.setattr(appguard.time, "strftime", lambda fmt, *t: next(stamps) if not t else real(fmt, *t))
    for n in range(7):
        appguard.set_aside(app(tmp_path, f"App{n}.app"), root)
    listed = appguard.rejected(root)
    assert [r["name"] for r in listed] == [f"App{n}" for n in (6, 5, 4, 3, 2)]
    assert all(r["size"] > 0 and r["path"].endswith(".rejected") and r["when"] > 0 for r in listed)
    assert len(list((root / "rejected").iterdir())) == 5
    assert appguard.rejected(tmp_path / "never-made") == []


def test_prune_drops_the_oldest_kept_copies_first_when_they_pass_the_size_cap(tmp_path):
    path, root = app(tmp_path), tmp_path / "kept"
    homes = [appguard.keep(path, root, f"org.example.n{n}", "1.0", run=copying) for n in range(3)]
    for n, copy in enumerate(homes):
        when = copy.parent.stat().st_mtime - (5 - n) * 86400  # n0 is the oldest; all are more than a day old
        os.utime(copy.parent, (when, when))
    (root / "rejected").mkdir()
    (root / "rejected" / "X-20260101-120000.rejected").mkdir()
    one = appguard.disk_size(homes[0].parent)
    assert appguard.prune(root, max_bytes=2 * one + 1) == 1
    assert not (root / "org.example.n0").exists() and homes[1].is_dir() and homes[2].is_dir()
    assert (root / "rejected").is_dir()
    assert appguard.prune(root, max_bytes=0) == 2 and (root / "rejected").is_dir()


def test_disk_size_does_not_follow_a_symlink(tmp_path):
    big = tmp_path / "big"
    big.mkdir()
    (big / "f").write_bytes(b"x" * 1_000_000)
    d = tmp_path / "d"
    d.mkdir()
    (d / "link").symlink_to(big)
    assert appguard.disk_size(d) < 100_000
