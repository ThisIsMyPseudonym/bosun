import json
import socket
import subprocess
import time

import pytest

from bosun import selfupdate
from bosun.cli import wait_for_port
from bosun.store import Store


def git(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=t@example.com", "-c", "init.defaultBranch=main",
                        *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def commit(repo, name, text):
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"change {name}")
    return git(repo, "rev-parse", "HEAD")


def keypair(tmp_path, name):
    key = tmp_path / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)], check=True)
    return key, (tmp_path / f"{name}.pub").read_text().strip()


def sign_with(repo, key):
    for k, v in (("gpg.format", "ssh"), ("user.signingkey", f"{key}.pub"), ("commit.gpgsign", "true")):
        git(repo, "config", k, v)


@pytest.fixture
def repos(tmp_path):
    """origin (bare), the machine's checkout, and a developer clone that pushes signed commits."""
    origin, dev, here = tmp_path / "origin.git", tmp_path / "dev", tmp_path / "here"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(dev))
    key, pub = keypair(tmp_path, "maintainer")
    sign_with(dev, key)
    (dev / selfupdate.TRUSTED_SIGNERS).write_text(f'maintainer namespaces="git" {pub}\n')
    git(dev, "add", selfupdate.TRUSTED_SIGNERS)
    commit(dev, "app.py", "v1\n")
    git(dev, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", str(origin), str(here))
    return dev, here, Store(tmp_path / "db")


def ok(root, deps):
    return ""


def test_fast_forwards_to_what_was_pushed(repos):
    dev, here, store = repos
    new = commit(dev, "app.py", "v2\n")
    git(dev, "push", "-q", "origin", "main")
    out = selfupdate.update(store, here, check=ok, channel="main")
    assert out.status == "updated" and out.new == new and selfupdate.head(here) == new
    assert (here / "app.py").read_text() == "v2\n"
    assert selfupdate.update(store, here, check=ok, channel="main").detail == "checked recently"  # the daily refresh checks once
    assert selfupdate.update(store, here, force=True, check=ok, channel="main").status == "current"


def test_only_commits_signed_by_a_trusted_key_are_installed(repos, tmp_path):
    """Someone who can push (a stolen token, a compromised account) still can't make every machine run their code."""
    dev, here, store = repos
    old = selfupdate.head(here)
    refused = lambda: selfupdate.update(store, here, force=True, check=ok, channel="main")  # noqa: E731

    git(dev, "config", "commit.gpgsign", "false")  # 1. not signed at all
    commit(dev, "app.py", "evil\n")
    git(dev, "push", "-q", "origin", "main")
    out = refused()
    assert out.status == "failed" and "not signed by a trusted key" in out.detail and selfupdate.head(here) == old

    stranger, stranger_pub = keypair(tmp_path, "stranger")  # 2. signed, but by a key nobody trusted
    sign_with(dev, stranger)
    commit(dev, "app.py", "evil 2\n")
    git(dev, "push", "-q", "origin", "main")
    assert "not signed by a trusted key" in refused().detail and selfupdate.head(here) == old

    # 3. the incoming commit adds the attacker's key to the list and is signed with it: new code can't vouch for itself
    (dev / selfupdate.TRUSTED_SIGNERS).write_text(f'x namespaces="git" {stranger_pub}\n')
    commit(dev, selfupdate.TRUSTED_SIGNERS, f'x namespaces="git" {stranger_pub}\n')
    git(dev, "push", "-q", "origin", "main")
    assert refused().status == "failed" and selfupdate.head(here) == old
    assert (here / "app.py").read_text() == "v1\n"


def test_a_trusted_signer_can_hand_over_to_a_new_key(repos, tmp_path):
    dev, here, store = repos
    successor, successor_pub = keypair(tmp_path, "successor")
    commit(dev, selfupdate.TRUSTED_SIGNERS, f'successor namespaces="git" {successor_pub}\n')  # signed by the old key
    git(dev, "push", "-q", "origin", "main")
    assert selfupdate.update(store, here, force=True, check=ok, channel="main").status == "updated"
    sign_with(dev, successor)
    new = commit(dev, "app.py", "v3\n")
    git(dev, "push", "-q", "origin", "main")
    assert selfupdate.update(store, here, force=True, check=ok, channel="main").status == "updated" and selfupdate.head(here) == new


def test_no_trusted_signers_means_no_updates(tmp_path):
    origin, dev, here = tmp_path / "origin.git", tmp_path / "dev", tmp_path / "here"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(dev))
    commit(dev, "app.py", "v1\n")
    git(dev, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", str(origin), str(here))
    commit(dev, "app.py", "v2\n")
    git(dev, "push", "-q", "origin", "main")
    out = selfupdate.update(Store(tmp_path / "db"), here, force=True, check=ok, channel="main")
    assert out.status == "failed" and "no trusted_signers file" in out.detail


def test_new_code_that_doesnt_start_is_rolled_back(repos):
    dev, here, store = repos
    old = selfupdate.head(here)
    commit(dev, "uv.lock", "broken\n")
    git(dev, "push", "-q", "origin", "main")
    seen = []
    out = selfupdate.update(store, here, channel="main",
                            check=lambda root, deps: seen.append(deps) or ("ImportError: x" if len(seen) == 1 else ""))
    assert out.status == "failed" and "ImportError" in out.detail and selfupdate.head(here) == old
    assert seen == [True, True]  # dependencies changed: put the old ones back too
    assert not (here / "uv.lock").exists()


def test_leaves_local_work_alone(repos):
    dev, here, store = repos
    commit(dev, "app.py", "v2\n")
    git(dev, "push", "-q", "origin", "main")
    (here / "app.py").write_text("edited here\n", encoding="utf-8")
    assert selfupdate.update(store, here, force=True, check=ok, channel="main").detail == "the checkout has uncommitted changes"
    git(here, "checkout", "-q", "app.py")
    (here / "notes.txt").write_text("untracked is fine\n", encoding="utf-8")
    commit(here, "local.py", "mine\n")
    out = selfupdate.update(store, here, force=True, check=ok, channel="main")
    assert out.status == "skipped" and "commits that aren't on origin/main" in out.detail
    git(here, "checkout", "-q", "-b", "experiment")
    assert "no upstream" in selfupdate.update(store, here, force=True, check=ok, channel="main").detail


def test_not_a_checkout(tmp_path):
    assert selfupdate.update(Store(tmp_path / "db"), tmp_path, check=ok).detail == "not a git checkout"


def test_dashboard_restarts_only_once_idle(repos, monkeypatch):
    dev, here, store = repos
    commits = iter(["aaa", "aaa", "bbb", "bbb", "bbb"])
    monkeypatch.setattr(selfupdate, "head", lambda root: next(commits))
    busy = iter([True, False])
    restarted = []
    selfupdate.restart_when_idle(lambda: next(busy), here, then=lambda: restarted.append(True), sleep=lambda s: None,
                                 check=ok)
    assert restarted == [True]  # new code seen twice; waited out the busy check once


def test_dashboard_never_restarts_onto_code_that_does_not_start(repos, monkeypatch):
    """A `git pull` or an update cut off half way can move the checkout without a check: the dashboard keeps
    running the old code and says why, instead of exiting into a crash loop."""
    dev, here, store = repos
    commits = iter(["aaa", "bbb", "bbb", "ccc"])
    monkeypatch.setattr(selfupdate, "head", lambda root: next(commits))
    checked, said, restarted = [], [], []
    selfupdate.restart_when_idle(lambda: False, here, then=lambda: restarted.append(True), sleep=lambda s: None,
                                 check=lambda root, deps: checked.append(1) or ("ImportError: x" if len(checked) == 1 else ""),
                                 report=said.append)
    assert restarted == [True] and len(checked) == 2  # bbb refused once (not re-checked every minute); ccc starts
    assert said and "bbb doesn't start (ImportError: x)" in said[0]


def test_who_restarts_the_dashboard(monkeypatch):
    for var in ("INVOCATION_ID", "XPC_SERVICE_NAME"):
        monkeypatch.delenv(var, raising=False)
    assert not selfupdate.supervised()  # Windows Startup shortcut or a terminal: it starts its own successor
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.bosun.serve")
    assert selfupdate.supervised()
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.apple.Terminal")
    monkeypatch.setenv("INVOCATION_ID", "8f1c")
    assert selfupdate.supervised()
    argv = selfupdate.successor_argv()
    assert argv[-2:] == ["bosun", "serve"] and argv[1] == "run"
    assert "conhost.exe" not in argv  # on Windows, conhost started detached took the new dashboard down with the old


def test_store_knows_when_work_is_running(tmp_path):
    store = Store(tmp_path / "db")
    assert not store.busy()
    job = store.create_job("apt:tmux", ["apt-get"], status="running")
    assert store.busy()
    store.finish_job(job, "ok", "")
    store.create_job("brew-cask:zoom", ["brew"], status="waiting")  # waits for Zoom to close; survives a restart
    assert not store.busy()


def test_serve_waits_for_the_port_to_free_up():
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    port = holder.getsockname()[1]
    assert wait_for_port("127.0.0.1", port, seconds=0.6) is False
    holder.close()
    assert wait_for_port("127.0.0.1", port, seconds=2) is True


# --- the default channel: signed release tags ------------------------------------------------------------------

def tag(repo, name, at="HEAD", how="signed", push=True):
    """A release tag in the developer clone: signed (with the clone's key), annotated (unsigned) or lightweight."""
    flags = {"signed": ["-s", "-m", name], "annotated": ["-a", "-m", name], "lightweight": []}[how]
    git(repo, "tag", "-f", *flags, name, at)
    if push:
        git(repo, "push", "-q", "-f", "origin", f"refs/tags/{name}")
    return git(repo, "rev-parse", f"{at}^{{commit}}")


def release(store, here, **kw):
    return selfupdate.update(store, here, force=True, check=ok, **kw)


def test_releases_is_the_default_channel(monkeypatch):
    from bosun.config import load_settings

    monkeypatch.delenv("BOSUN_UPDATE_CHANNEL", raising=False)
    assert load_settings().update_channel == "releases"
    monkeypatch.setenv("BOSUN_UPDATE_CHANNEL", "main")
    assert load_settings().update_channel == "main"


def test_moves_only_to_the_newest_signed_release_compared_numerically(repos):
    dev, here, store = repos
    start = selfupdate.head(here)
    commit(dev, "app.py", "v2\n")
    git(dev, "push", "-q", "origin", "main")
    out = release(store, here)  # pushed to main, not released: nothing moves
    assert out.status == "current" and selfupdate.head(here) == start and "newest release (none yet)" in out.detail

    v1_10 = tag(dev, "v1.10.0")  # numerically higher than v1.9.0, though lower as text
    commit(dev, "app.py", "v3\n")
    git(dev, "push", "-q", "origin", "main")
    tag(dev, "v1.9.0")
    out = release(store, here)
    assert out.status == "updated" and out.new == v1_10 and "(v1.10.0)" in out.detail
    assert selfupdate.head(here) == v1_10 and (here / "app.py").read_text() == "v2\n"
    out = release(store, here)  # v1.9.0 is newer code but an older version: Bosun stays on the higher version
    assert out.status == "current" and "v1.10.0, the newest" in out.detail and selfupdate.head(here) == v1_10
    assert selfupdate.last_status(store)["detail"] == out.detail and selfupdate.last_status(store)["channel"] == "releases"


def test_a_tag_older_than_the_checkout_or_on_another_line_is_never_installed(repos):
    dev, here, store = repos
    first = selfupdate.head(here)
    tag(dev, "v1.0.0")  # the commit the checkout is already past, once it moves on below
    git(dev, "checkout", "-q", "-b", "side", first)
    commit(dev, "side.py", "elsewhere\n")
    tag(dev, "v5.0.0")  # a higher version on another line: a sideways move
    git(dev, "checkout", "-q", "main")
    commit(here, "local.py", "ahead of every release\n")
    ahead = selfupdate.head(here)
    out = release(store, here)
    assert out.status == "current" and selfupdate.head(here) == ahead
    assert "newer than the newest release (v5.0.0)" in out.detail and "stays here" in out.detail
    assert not (here / "side.py").exists()


def test_refuses_a_release_tag_signed_by_an_untrusted_key(repos, tmp_path):
    dev, here, store = repos
    start = selfupdate.head(here)
    commit(dev, "app.py", "v2\n")
    git(dev, "push", "-q", "origin", "main")
    stranger, _ = keypair(tmp_path, "stranger")
    git(dev, "config", "user.signingkey", f"{stranger}.pub")
    tag(dev, "v2.0.0")
    out = release(store, here)
    assert out.status == "failed" and "v2.0.0" in out.detail and "not signed by a trusted key" in out.detail
    assert selfupdate.head(here) == start and out.new  # the refresh lists it as an error (refresh.py)
    assert selfupdate.last_status(store)["status"] == "failed"  # the sidebar says so until it is resolved


def test_refuses_lightweight_and_unsigned_tags(repos):
    dev, here, store = repos
    start = selfupdate.head(here)
    commit(dev, "app.py", "v2\n")
    git(dev, "push", "-q", "origin", "main")
    tag(dev, "v2.0.0", how="lightweight")
    tag(dev, "v1.5.0", how="annotated")
    out = release(store, here)
    assert out.status == "failed" and selfupdate.head(here) == start
    assert "v2.0.0 is a lightweight tag" in out.detail and "v1.5.0" in out.detail and "not signed" in out.detail


def test_refuses_a_signed_release_with_an_unsigned_commit_in_it(repos):
    dev, here, store = repos
    start = selfupdate.head(here)
    git(dev, "config", "commit.gpgsign", "false")
    commit(dev, "app.py", "slipped in\n")
    git(dev, "config", "commit.gpgsign", "true")
    commit(dev, "app.py", "v2\n")  # the tip is signed, and so is the tag
    git(dev, "push", "-q", "origin", "main")
    tag(dev, "v2.0.0")
    out = release(store, here)
    assert out.status == "failed" and "not signed by a trusted key" in out.detail and selfupdate.head(here) == start


def test_falls_back_to_the_newest_release_that_passes_and_says_what_it_refused(repos):
    dev, here, store = repos
    good = commit(dev, "app.py", "v2\n")
    tag(dev, "v1.1.0")
    git(dev, "config", "commit.gpgsign", "false")
    commit(dev, "app.py", "evil\n")
    git(dev, "push", "-q", "origin", "main")
    tag(dev, "v1.2.0")
    out = release(store, here)
    assert out.status == "updated" and out.new == good and "refused v1.2.0" in out.detail


def test_refuses_a_release_tag_that_was_moved_after_it_was_seen(repos):
    dev, here, store = repos
    first = tag(dev, "v1.1.0", at=commit(dev, "app.py", "v2\n"))
    git(dev, "push", "-q", "origin", "main")
    assert release(store, here).new == first
    later = commit(dev, "app.py", "v3, signed by the maintainer\n")
    git(dev, "push", "-q", "origin", "main")
    tag(dev, "v1.1.0", at=later)  # re-pointed and re-signed: a release never changes, so this is not one
    out = release(store, here)
    assert out.status == "failed" and "v1.1.0 was moved" in out.detail and selfupdate.head(here) == first


def test_a_tag_the_clone_already_had_counts_as_seen(repos, tmp_path):
    dev, here, store = repos
    first = tag(dev, "v1.0.0")
    clone = tmp_path / "fresh"
    git(tmp_path, "clone", "-q", str(dev.parent / "origin.git"), str(clone))  # brings refs/tags/v1.0.0 with it
    assert selfupdate.head(clone) == first
    later = commit(dev, "app.py", "v2\n")
    git(dev, "push", "-q", "origin", "main")
    tag(dev, "v1.0.0", at=later)  # moved before this machine's first check, but after it was cloned
    out = selfupdate.update(Store(tmp_path / "db2"), clone, force=True, check=ok)
    assert out.status == "failed" and "v1.0.0 was moved" in out.detail


def test_refuses_a_signed_tag_pushed_under_another_name(repos):
    dev, here, store = repos
    start = selfupdate.head(here)
    commit(dev, "app.py", "v2\n")
    git(dev, "push", "-q", "origin", "main")
    tag(dev, "v1.0.0", push=False)
    git(dev, "push", "-q", "origin", "refs/tags/v1.0.0:refs/tags/v9.0.0")  # an old signed release, relabelled
    out = release(store, here)
    assert out.status == "failed" and "v9.0.0 is named 'v1.0.0' inside" in out.detail and selfupdate.head(here) == start


def test_ignores_tags_that_are_not_plain_versions(repos):
    dev, here, store = repos
    start = selfupdate.head(here)
    commit(dev, "app.py", "v2\n")
    git(dev, "push", "-q", "origin", "main")
    for name in ("v2.0", "v2.0.0-rc1", "v02.0.0", "2.0.0", "v2.0.0.1", "latest"):
        tag(dev, name)
    out = release(store, here)
    assert out.status == "current" and selfupdate.head(here) == start and "none yet" in out.detail
    assert selfupdate.release_key("v10.0.3") == (10, 0, 3) and selfupdate.release_key("v1.2.3-beta") is None


def test_a_failed_fetch_changes_nothing(repos, tmp_path):
    dev, here, store = repos
    start = selfupdate.head(here)
    git(here, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    out = release(store, here)
    assert out.status == "failed" and out.detail.startswith("git fetch origin") and selfupdate.head(here) == start
    assert not store.cache_get("selfupdate", "checked")  # tries again on the next refresh


def test_a_release_can_hand_over_to_a_new_key_but_cannot_bring_its_own(repos, tmp_path):
    dev, here, store = repos
    start = selfupdate.head(here)
    stranger, stranger_pub = keypair(tmp_path, "stranger")  # 1. a key nobody trusted adds itself and signs
    git(dev, "checkout", "-q", "-b", "attack")
    sign_with(dev, stranger)
    commit(dev, selfupdate.TRUSTED_SIGNERS, f'x namespaces="git" {stranger_pub}\n')
    commit(dev, "app.py", "evil\n")
    tag(dev, "v6.6.6")
    assert release(store, here).status == "failed" and selfupdate.head(here) == start

    git(dev, "checkout", "-q", "main")  # 2. the maintainer's key hands over, then the new key signs a release
    git(dev, "tag", "-d", "v6.6.6")
    git(dev, "push", "-q", "origin", ":refs/tags/v6.6.6")
    maintainer = tmp_path / "maintainer"
    sign_with(dev, maintainer)
    successor, successor_pub = keypair(tmp_path, "successor")
    commit(dev, selfupdate.TRUSTED_SIGNERS, f'successor namespaces="git" {successor_pub}\n')
    sign_with(dev, successor)
    new = commit(dev, "app.py", "v2\n")
    git(dev, "push", "-q", "origin", "main")
    tag(dev, "v2.0.0")
    out = release(store, here)
    assert out.status == "updated" and selfupdate.head(here) == new


def test_a_detached_checkout_follows_releases_too(repos):
    dev, here, store = repos
    git(here, "checkout", "-q", "--detach")
    new = tag(dev, "v1.0.0", at=commit(dev, "app.py", "v2\n"))
    git(dev, "push", "-q", "origin", "main")
    assert release(store, here).new == new and selfupdate.head(here) == new


def test_unknown_channel_installs_nothing(repos):
    dev, here, store = repos
    out = release(store, here, channel="nightly")
    assert out.status == "failed" and "unknown update channel" in out.detail


# --- a failed or half-finished update leaves the old version ---------------------------------------------------

def test_a_check_that_raises_still_puts_the_old_version_back(repos):
    dev, here, store = repos
    old = selfupdate.head(here)
    tag(dev, "v1.0.0", at=commit(dev, "app.py", "v2\n"))
    git(dev, "push", "-q", "origin", "main")

    def boom(root, deps):
        raise OSError("disk full")

    out = selfupdate.update(store, here, force=True, check=boom)
    assert out.status == "failed" and "OSError: disk full" in out.detail and selfupdate.head(here) == old
    assert (here / "app.py").read_text() == "v1\n" and not store.setting(selfupdate.PENDING)
    assert selfupdate.last_status(store)["status"] == "failed"


def test_an_update_cut_off_after_moving_is_checked_or_undone_next_time(repos, monkeypatch):
    dev, here, store = repos
    old = selfupdate.head(here)
    new = commit(dev, "uv.lock", "broken\n")
    git(dev, "push", "-q", "origin", "main")
    git(here, "fetch", "-q")
    git(here, "merge", "--ff-only", "-q", new)  # as if the process died right after moving HEAD
    store.set_setting(selfupdate.PENDING, json.dumps({"old": old, "new": new, "deps": True, "at": 0}))
    seen = []
    out = selfupdate.update(store, here, check=lambda root, deps: seen.append(deps) or ("ImportError" if len(seen) == 1 else ""))
    assert out.status == "failed" and "interrupted" in out.detail and selfupdate.head(here) == old
    assert seen == [True, True] and not store.setting(selfupdate.PENDING)

    store.set_setting(selfupdate.PENDING, json.dumps({"old": old, "new": new, "deps": False, "at": time.time()}))
    assert "still being installed" in selfupdate.update(store, here, check=ok).detail  # a live one is left alone


def test_only_an_ssh_signature_counts_never_one_the_persons_own_keyring_would_accept(repos, monkeypatch):
    """git picks the checker from the signature itself: a PGP signature would go to gpg, which calls any key in
    the person's keyring good. So a commit with anything but an SSH signature is refused before git is asked,
    and git is told to use no PGP or X.509 checker at all."""
    dev, here, _ = repos
    head = git(dev, "rev-parse", "HEAD")
    tree = git(dev, "rev-parse", "HEAD^{tree}")
    pgp = (f"tree {tree}\nparent {head}\nauthor T <t@example.com> 1700000000 +0000\n"
           "committer T <t@example.com> 1700000000 +0000\n"
           "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEE\n -----END PGP SIGNATURE-----\n\nsigned with a pgp key\n")
    made = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=dev, input=pgp.encode(),
                          capture_output=True, check=True).stdout.decode().strip()  # bytes: text mode would write CRLF on Windows
    assert "no SSH signature" in selfupdate.verify(dev, made)
    assert "no SSH signature" in selfupdate.verify_chain(dev, head, made)
    seen = []
    real = selfupdate.git
    monkeypatch.setattr(selfupdate, "git", lambda root, *args, **kw: seen.append(args) or real(root, *args, **kw))
    assert selfupdate.verify(dev, head) == ""  # the maintainer's SSH-signed commit still passes
    checked = next(a for a in seen if any(str(x).startswith("verify-") for x in a))
    for name in ("gpg.openpgp.program", "gpg.x509.program", "gpg.program"):
        assert any(str(x).startswith(f"{name}=/nonexistent/") for x in checked), name
