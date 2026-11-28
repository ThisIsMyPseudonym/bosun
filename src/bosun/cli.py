"""`bosun` command line: status | refresh | serve | doctor | open | install-tasks | query | runs | mcp..."""

from __future__ import annotations

import argparse
import os
import json
import re
import logging
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from . import machine, proc, startguard
from .config import PROJECT_ROOT, load_settings
from .store import Store

log = logging.getLogger("bosun")


def _setup_logging(s, process: str = "cli", *, open_db: bool = True) -> None:
    """bosun.log for people (each line tagged with its run's short trace id), bosun.jsonl for tools and agents,
    and warnings stored next to their run's spans in the database."""
    from . import migrations, trace

    machine.private_dir(s.data_dir)
    context = trace.ContextFilter()
    fmt = trace.RedactingFormatter("%(asctime)s %(levelname)s %(name)s%(trace)s: %(message)s")
    file = RotatingFileHandler(s.data_dir / "bosun.log", maxBytes=2_000_000, backupCount=3,
                               encoding="utf-8")
    file.setFormatter(fmt)
    jsonl = RotatingFileHandler(s.data_dir / "bosun.jsonl", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    jsonl.setFormatter(trace.JsonFormatter())
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    handlers = [file, jsonl, console, trace.EventHandler()]
    for h in handlers:
        h.addFilter(context)
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not open_db:
        return
    try:
        with Store(s.db_path):  # migrate before the tracer writes to the new tables
            pass
        trace.configure(s.db_path, process)
    except migrations.MigrationRefused:
        pass  # the command itself will say so, once
    except Exception:  # a broken database must not stop Bosun from starting and saying so
        log.exception("tracing is off: the database could not be opened")


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _printable(text: object) -> str:
    """Text from the dashboard, safe to print: no escape sequences or other control characters for the terminal."""
    return _CONTROL.sub("", str(text))


def _http_error(resp) -> str:
    try:
        data = resp.json()
    except ValueError:
        return _printable((resp.text or f"HTTP {resp.status_code}")[:2000])
    if isinstance(data, dict):
        detail = data.get("detail", data.get("error", data))
        return _printable(detail if isinstance(detail, str) else json.dumps(detail, default=str)[:2000])
    return _printable(str(data)[:2000])


REFRESH_WAIT_S = 2 * 3600  # a refresh through the dashboard is scans only (upgrades are queued): hours means stuck


def _refresh_via_dashboard(
    args, s, *,
    http: Any = None,
    up: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
) -> int | None:
    """If the dashboard for this data_dir is answering on its loopback port, trigger the refresh through it
    so `bosun serve` stays the single SQLite writer and single Upgrader queue owner. Returns the CLI exit
    code if the dashboard handled or refused the request, or None to fall back to running in-process.

    Every answer must carry the dashboard's proof that it holds this data_dir's key (auth.reply_proof): a
    program that took the port while the dashboard was down could otherwise answer "done, nothing found" every
    day, and no scan would run. An answer to the first request without it means it isn't this dashboard, and
    the refresh runs here instead."""
    import httpx

    from . import auth, launcher

    url = f"http://{s.host}:{s.port}"
    if http is None:
        if not (s.data_dir / auth.KEY_FILE).is_file():
            return None  # no session key for this data_dir: never talk to a dashboard owned by another data_dir
        if not (up or (lambda: launcher.dashboard_up(url)))():
            return None

    agent_name = "cli" if sys.stdin.isatty() else "schedule"
    agent_auth = auth.AgentAuth(lambda: auth.load_key(s.data_dir), agent_name)
    payload: dict[str, Any] = {"auto_upgrade": not args.no_auto}
    if args.only:
        only = [x.strip() for x in args.only.split(",") if x.strip()]
        if only:
            payload["only"] = only

    def genuine(resp) -> bool:
        return auth.valid_reply(auth.load_key(s.data_dir), resp.request.headers.get("authorization"),
                                resp.headers.get(auth.REPLY_HEADER))

    def not_genuine() -> int:
        print(f"Something on port {s.port} stopped answering as this Bosun dashboard; the refresh's result is unknown.",
              file=sys.stderr)
        return 1

    client = http or httpx.Client(base_url=url, timeout=httpx.Timeout(30.0, connect=5.0),
                                  follow_redirects=False, trust_env=False)
    try:
        try:
            r = client.post("/api/refresh", json=payload, auth=agent_auth)
        except httpx.ConnectError:
            return None  # dashboard stopped between /health and POST: fall back to in-process
        except httpx.TransportError as exc:
            print(f"Could not reach the Bosun dashboard: {_printable(exc)}", file=sys.stderr)
            return 1
        if r.status_code == 401:
            return None  # a different Bosun instance (different session.key) is on this port
        if not genuine(r):
            print(f"Something on port {s.port} answered as Bosun but couldn't show it is this machine's dashboard; "
                  "refreshing here instead.", file=sys.stderr)
            return None
        if r.status_code >= 400:
            print(_http_error(r), file=sys.stderr)
            return 1
        try:
            started = r.json()
        except ValueError:
            started = {}
        tid = started.get("trace_id") if isinstance(started, dict) else None
        deadline = clock() + REFRESH_WAIT_S
        while True:
            try:
                sr = client.get("/api/status", auth=agent_auth)
            except httpx.TransportError:
                print("Lost connection to the Bosun dashboard while waiting for refresh to finish.", file=sys.stderr)
                return 1
            if not genuine(sr):
                return not_genuine()
            if sr.status_code >= 400:
                print(_http_error(sr), file=sys.stderr)
                return 1
            try:
                rf = (sr.json() or {}).get("refresh") or {}
            except (ValueError, AttributeError):
                rf = {}
            if tid is None or rf.get("trace_id") == tid:
                if rf.get("running"):
                    if clock() > deadline:
                        print(f"The refresh is still running after {REFRESH_WAIT_S // 3600} hours; "
                              "see the dashboard for how it ends.", file=sys.stderr)
                        return 1
                    sleep(poll_s)
                    continue
                if rf.get("error"):
                    print(_printable(rf["error"]), file=sys.stderr)
                    return 1
                result = rf.get("last_result")
                if isinstance(result, dict):
                    print(json.dumps(result, indent=2))
                    return 0 if not result.get("errors") else 1
                print(json.dumps({"trace_id": tid, "errors": []}, indent=2))
                return 0
            # Another refresh or a recheck started right after ours finished: read our own run's outcome.
            try:
                rr = client.get(f"/api/runs/{tid}", auth=agent_auth)
            except httpx.TransportError:
                print("Lost connection to the Bosun dashboard while checking refresh result.", file=sys.stderr)
                return 1
            if not genuine(rr):
                return not_genuine()
            if rr.status_code == 200:
                run = (rr.json() or {}).get("run") or {}
                ok = run.get("status") == "ok"
                if not ok and run.get("error"):
                    print(_printable(run["error"]), file=sys.stderr)
                print(json.dumps({"trace_id": tid, "status": run.get("status", "unknown"),
                                  "summary": run.get("summary", ""),
                                  "errors": [run["error"]] if run.get("error") else []}, indent=2))
                return 0 if ok else 1
            print(json.dumps({"trace_id": tid, "errors": []}, indent=2))
            return 0
    finally:
        if http is None:
            client.close()


def cmd_refresh(args, s) -> int:
    from . import lock
    from .refresh import refresh

    routed = _refresh_via_dashboard(args, s)
    if routed is not None:
        return routed
    refresh_lock = lock.FileLock(s.db_path.parent / "refresh.lock")
    if not refresh_lock.acquire():
        print("A refresh is already running.", file=sys.stderr)
        return 1
    try:
        _setup_logging(s, "cli")
        only = {x.strip() for x in args.only.split(",") if x.strip()} if args.only else None
        # launchd, systemd and Task Scheduler run it with no terminal; a person typing it has one
        with Store(s.db_path) as store:
            result = refresh(s, store, only=only, auto_upgrade=not args.no_auto,
                             actor="cli" if sys.stdin.isatty() else "schedule")
        print(json.dumps(result, indent=2))
        return 0 if not result["errors"] else 1
    finally:
        refresh_lock.release()


def cmd_update_scanner(args, s) -> int:
    from . import scanner

    try:
        done = scanner.ensure(s, Store(s.db_path), force=True)
    except RuntimeError as exc:
        print(exc)
        return 1
    print(done or f"osv-scanner at {s.osv_scanner} isn't Bosun's copy; update it the way you installed it")
    return 0 if done and "failed" not in done else 1


def cmd_self_update(args, s) -> int:
    from . import selfupdate

    if getattr(args, "repin", None):
        try:
            print(selfupdate.repin(Store(s.db_path), args.repin))
        except (ValueError, RuntimeError) as exc:
            print(f"bosun: {exc}", file=sys.stderr)
            return 1
        return 0
    out = selfupdate.update(Store(s.db_path), force=True, channel=s.update_channel,
                            delay_hours=s.self_update_delay_hours)
    print(out)
    if out.status == "updated":
        print("The dashboard moves onto it within a minute, once nothing is running.")
    return 1 if out.status == "failed" else 0


def cmd_demo(args, s) -> int:
    """The dashboard on a made-up computer (demo.py). main gave it a data folder of its own, deleted when it stops."""
    import threading

    import uvicorn

    from . import auth, demo, launcher
    from .server import create_app

    demo.seed(Store(s.db_path))
    if not wait_for_port(s.host, s.port, seconds=3):
        # said before any link is printed: a link to a port something else holds would open that something else
        print(f"Port {s.port} is in use, so the demo can't start there. Leave BOSUN_PORT unset and it picks a free one.",
              file=sys.stderr)
        return 1
    url = f"http://{s.host}:{s.port}/"
    app = create_app(s)

    def show_link() -> None:  # the sign-in code lasts two minutes, so it is made once the dashboard answers
        for _ in range(60):
            if launcher.dashboard_up(url):
                print("Bosun demo (made-up data, nothing here is on your computer). Open this link in your browser:\n"
                      f"  {auth.login_url(url, s.data_dir)}", flush=True)
                return
            time.sleep(0.5)

    threading.Thread(target=show_link, daemon=True, name="demo-link").start()
    # the server passes a stop signal on once it has shut down, so nothing after this line runs: the demo's own
    # folder is removed as the server shuts down
    import shutil
    app.router.on_shutdown.append(lambda: shutil.rmtree(s.data_dir, ignore_errors=True))
    uvicorn.run(app, host=s.host, port=s.port, log_level="warning")
    return 0


def _free_port(host: str) -> int:
    """A port nothing is listening on, chosen by the system."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def wait_for_port(host: str, port: int, seconds: float = 30.0) -> bool:
    """A dashboard restarting onto new code: the one before it may still be letting go of the port."""
    import socket

    deadline = time.monotonic() + seconds
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if not machine.IS_WINDOWS:  # matches uvicorn; on Windows this flag would steal a port in use
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return True
            except OSError:
                if time.monotonic() > deadline:
                    return False  # let uvicorn report it
        time.sleep(0.5)


def cmd_serve(args, s) -> int:
    import uvicorn

    from .server import create_app

    from . import migrations, selfupdate

    on_probation = getattr(args, "on_probation", False)
    if not wait_for_port(s.host, s.port) and on_probation:
        # Another program holds the port. Not the new code's fault: not counted, and never a reason to go back.
        startguard.port_busy(s.data_dir, f"port {s.port} on {s.host} is in use by another program")
        on_probation = args.on_probation = False
    if on_probation:
        startguard.restart_clock()  # waiting for the port doesn't count against serving
    if selfupdate.supervised():  # the installed dashboard (launchd, systemd): this checkout owns the data
        # Claimed before the database is opened for use: opening migrates, and a checkout that was moved since it
        # last migrated would be refused by the owner guard and restarted in a loop.
        migrations.claim_file(s.db_path)
        store = Store(s.db_path)
        migrations.claim(store._db)
    print(f"Bosun on http://{s.host}:{s.port}. Sign in with `uv run bosun open` (the address alone answers 401).", flush=True)
    server = uvicorn.Server(uvicorn.Config(create_app(s), host=s.host, port=s.port, log_level="warning"))
    if (watcher := startguard.watcher_pid()) is not None:  # run by `bosun serve`'s watcher: stop when it is gone
        threading.Thread(target=startguard.stop_with_watcher, args=(server, watcher), daemon=True,
                         name="watcher-gone").start()
    if on_probation:  # the first start on new code: it counts once it answers on its port
        startguard.restart_clock()  # nor do the migrations (opening the database, above)
        threading.Thread(target=confirm_when_serving, args=(server, s), daemon=True, name="confirm-start").start()
    try:
        server.run()
    except SystemExit:  # uvicorn exits this way when it can't bind the port
        if server.started:
            raise
    if not server.started:  # as uvicorn.run does: the port could not be bound, or the app did not start
        if getattr(args, "on_probation", False) and not port_free(s.host, s.port):
            startguard.port_busy(s.data_dir, f"port {s.port} on {s.host} is in use by another program")
            args.on_probation = False
        return 3
    return 0


def port_free(host: str, port: int) -> bool:
    return wait_for_port(host, port, seconds=0)


def confirm_when_serving(server, s, seconds: float = startguard.SERVE_WITHIN_S) -> bool:
    """Wait until uvicorn has bound the port and the app answers /health, then tell startguard the new code is good.
    Asked of this very process (server.started), so another program answering on the port can't confirm it."""
    import http.client

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if server.started:
            try:
                conn = http.client.HTTPConnection(s.host, s.port, timeout=5)
                conn.request("GET", "/health")
                r = conn.getresponse()
                answered = r.status == 200 and json.loads(r.read() or b"null") == {"bosun": True}
                conn.close()
            except (OSError, ValueError, http.client.HTTPException):
                answered = False
            if answered:
                return startguard.confirm(s.data_dir)
        time.sleep(0.5)
    return False


def cmd_install_tasks(args, s) -> int:
    from . import migrations

    store = Store(s.db_path)
    store.set_setting("refresh_time", args.time)  # the dashboard shows when Autopilot runs next
    migrations.claim(store._db)  # this checkout is the installed Bosun: only it migrates the real database
    if machine.IS_MAC:
        rc = _install_launchd(args, s)
    else:
        rc = _install_systemd(args, s) if machine.IS_LINUX else _install_windows(args, s)
    rc = cmd_install_launcher(args, s) or rc
    print("Optional: `uv run bosun mcp install` lets Claude Code ask Bosun what needs attention and act through it.")
    return rc


def service_env(s) -> dict[str, str]:
    """What the background services must carry to behave as this install does: the update channel and the wait
    for a new release, when they are not the defaults. A service starts with a bare environment, so a person who
    chose `main` would lose it at the next login. The values go into plists, unit files and a cmd line, so only a
    plain word and a plain number are passed on."""
    env = {}
    channel = s.update_channel
    if channel != "releases" and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", channel):
        env["BOSUN_UPDATE_CHANNEL"] = channel
    delay = repr(float(s.self_update_delay_hours)).removesuffix(".0")  # exact: 1e-05 and 24.123456789 too
    if s.self_update_delay_hours != 24 and re.fullmatch(r"[0-9][0-9.e+-]{0,23}", delay):
        env["BOSUN_SELF_UPDATE_DELAY_HOURS"] = delay
    return env


LAUNCHD_PATH = "/opt/homebrew/bin:/usr/local/bin:{home}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def launchd_plists(uv: str, root: str, data_dir: Path, hour: int, minute: int, home: Path,
                   extra_env: dict[str, str] | None = None) -> dict[str, dict]:
    """Two per-user LaunchAgents: the dashboard (at login, restarted if it crashes) and a daily refresh."""
    env = {"PATH": LAUNCHD_PATH.format(home=home.as_posix()),  # launchd starts with a bare PATH: brew, claude, npm live here
           **(extra_env or {})}
    base = {"EnvironmentVariables": env, "WorkingDirectory": root, "ProcessType": "Background",
            "StandardOutPath": str(data_dir / "launchd.log"), "StandardErrorPath": str(data_dir / "launchd.log")}
    return {
        "com.bosun.serve": {**base, "Label": "com.bosun.serve",
                                     "ProgramArguments": [uv, "run", "--project", root, "bosun", "serve"],
                                     "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}},
        "com.bosun.refresh": {**base, "Label": "com.bosun.refresh",
                                       "ProgramArguments": [uv, "run", "--project", root, "bosun", "refresh"],
                                       "StartCalendarInterval": {"Hour": hour, "Minute": minute}},
    }


def _install_launchd(args, s) -> int:
    import plistlib
    import shutil

    home = Path.home()
    uv = shutil.which("uv") or next((str(p) for p in (home / ".local/bin/uv", Path("/opt/homebrew/bin/uv")) if p.exists()), None)
    if not uv:
        print("uv not found (brew install uv)")
        return 1
    hour, minute = (int(x) for x in args.time.split(":"))
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    ok = True
    for label, plist in launchd_plists(uv, str(PROJECT_ROOT), s.data_dir, hour, minute, home, service_env(s)).items():
        path = agents / f"{label}.plist"
        with open(path, "wb") as fh:
            plistlib.dump(plist, fh)
        proc.run(["launchctl", "bootout", domain, str(path)], timeout=30)  # replace any older copy; fails if absent
        r = proc.run(["launchctl", "bootstrap", domain, str(path)], timeout=30)
        ok &= r.ok
        print(f"{label}: {'ok' if r.ok else 'FAILED ' + r.stderr.strip()[:200]} ({path})")
    print(f"Dashboard: http://{s.host}:{s.port} · daily refresh {args.time}")
    print("To remove: launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.bosun.*.plist && rm those files")
    return 0 if ok else 1


SYSTEMD_PATH = ("{home}/.local/bin", "/home/linuxbrew/.linuxbrew/bin", "/usr/local/sbin", "/usr/local/bin", "/usr/sbin",
                "/usr/bin", "/sbin", "/bin", "/snap/bin")


def _unit_quote(value: str) -> str:
    """A systemd unit-file word: double-quoted, with % (specifiers), backslashes and quotes escaped."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def systemd_units(uv: str, root: str, hour: int, minute: int, path: str, graphical: bool,
                  extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """Per-user systemd units: the dashboard (with the desktop session, restarted if it crashes) and a
    daily refresh timer that catches up after the machine was off."""
    run = f"{_unit_quote(uv)} run --project {_unit_quote(root)} bosun"
    # WorkingDirectory takes the path as-is (no quoting); only specifiers need escaping there
    env = f"WorkingDirectory={root.replace('%', '%%')}\nEnvironment={_unit_quote('PATH=' + path)}\n"
    env += "".join(f"Environment={_unit_quote(f'{k}={v}')}\n" for k, v in (extra_env or {}).items())
    # inside the desktop session the dashboard can show notifications and the password window
    session = ("After=graphical-session.target\nPartOf=graphical-session.target\n", "graphical-session.target") \
        if graphical else ("", "default.target")
    return {
        "bosun-serve.service": (f"[Unit]\nDescription=Bosun dashboard (127.0.0.1)\n{session[0]}\n"
                                f"[Service]\n{env}ExecStart={run} serve\nRestart=on-failure\nRestartSec=10\n\n"
                                f"[Install]\nWantedBy={session[1]}\n"),
        "bosun-refresh.service": (f"[Unit]\nDescription=Bosun refresh\n\n"
                                  f"[Service]\nType=oneshot\n{env}ExecStart={run} refresh\nNice=10\n"),
        "bosun-refresh.timer": (f"[Unit]\nDescription=Bosun daily refresh\n\n"
                                f"[Timer]\nOnCalendar=*-*-* {hour:02d}:{minute:02d}:00\nPersistent=true\n\n"
                                f"[Install]\nWantedBy=timers.target\n"),
    }


def _install_systemd(args, s) -> int:
    import shutil

    home = Path.home()
    uv = shutil.which("uv") or next((str(p) for p in (home / ".local/bin/uv", home / ".cargo/bin/uv") if p.exists()), None)
    if not uv:
        print("uv not found (curl -LsSf https://astral.sh/uv/install.sh | sh)")
        return 1
    hour, minute = (int(x) for x in args.time.split(":"))
    # the user manager starts with a bare PATH: add where apt/snap/brew tools live, plus where npm and
    # claude are right now (nvm and tarball installs put them in their own folders)
    dirs = [d.format(home=home.as_posix()) for d in SYSTEMD_PATH]
    found = [str(Path(w).parent) for t in ("npm", "claude", "osv-scanner", "docker") if (w := shutil.which(t))]
    path = ":".join(dict.fromkeys([*found, *dirs]))
    graphical = bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name, text in systemd_units(uv, str(PROJECT_ROOT), hour, minute, path, graphical, service_env(s)).items():
        (unit_dir / name).write_text(text, encoding="utf-8")
        print(f"wrote {unit_dir / name}")
    ok = True
    for argv in (["daemon-reload"], ["enable", "--now", "bosun-serve.service", "bosun-refresh.timer"],
                 ["restart", "bosun-serve.service"]):  # restart: pick up a reinstall
        r = proc.run(["systemctl", "--user", *argv], timeout=60)
        ok &= r.ok
        print(f"systemctl --user {' '.join(argv)}: {'ok' if r.ok else 'FAILED ' + r.stderr.strip()[:200]}")
    print(f"Dashboard: http://{s.host}:{s.port} · daily refresh {args.time}"
          + ("" if graphical else " (no desktop session found: the dashboard starts with your user manager)"))
    print("To remove: systemctl --user disable --now bosun-serve.service bosun-refresh.timer "
          f"&& rm {unit_dir}/bosun-*")
    return 0 if ok else 1


def windows_refresh_task(uv_exe: str, env: dict[str, str]) -> str:
    """The daily refresh's command line for Task Scheduler (conhost keeps the console window hidden)."""
    from . import launcher

    return "conhost.exe " + launcher.windows_command(uv_exe, "refresh", env)


def _install_windows(args, s) -> int:
    """Register per-user scheduled tasks: dashboard at logon, refresh daily. No admin needed."""
    # prefer the standalone install; other apps bundle their own uv on PATH
    standard = Path.home() / ".local" / "bin" / "uv.exe"
    found = [u for u in proc.run(["where.exe", "uv"]).stdout.splitlines() if u.lower().endswith("uv.exe")]
    uv_exe = str(standard) if standard.exists() else next(iter(found), None)
    if not uv_exe:
        print("uv.exe not found on PATH")
        return 1
    # Daily refresh: a per-user scheduled task (runs only while you're logged on; no admin).
    # conhost --headless keeps the console window hidden.
    from . import launcher

    tr = windows_refresh_task(uv_exe, service_env(s))
    r = proc.run(["schtasks", "/Create", "/F", "/TN", "Bosun-Refresh", "/TR", tr,
                  "/SC", "DAILY", "/ST", args.time, "/RL", "LIMITED"])
    print(f"Bosun-Refresh (daily {args.time}): {'ok' if r.ok else 'FAILED'} {r.stderr.strip()}")
    ok = r.ok
    # Dashboard at logon: schtasks ONLOGON needs admin, so use a Startup-folder shortcut instead.
    startup = launcher.start_menu() / "Startup"
    (startup / "Bosun.lnk").unlink(missing_ok=True)  # its old name
    lnk = startup / launcher.STARTUP_NAME
    r2 = launcher.windows_shortcut(lnk, uv_exe, "serve", "Bosun dashboard (127.0.0.1)", service_env(s))
    print(f"Startup shortcut (dashboard at logon): {'ok' if r2.ok else 'FAILED'} {lnk} {r2.stderr.strip()[:200]}")
    print("To remove: schtasks /Delete /TN Bosun-Refresh /F, and delete the shortcut above.")
    return 0 if ok and r2.ok else 1


def cmd_doctor(args, s) -> int:
    from . import doctor

    text, rc = doctor.report(doctor.check(s, providers=bool(getattr(args, "providers", False))))
    print(text)
    return rc


def cmd_open(args, s) -> int:
    from . import launcher, notify

    url = f"http://{s.host}:{s.port}/"
    if launcher.open_dashboard(url, data_dir=s.data_dir, print_url=args.print_url):
        return 0
    log.error("open: the dashboard didn't answer on %s within %ds", url, launcher.START_WAIT_S)
    notify.deliver(notify.Note("Bosun didn't start", f"Details are in {s.data_dir / 'bosun.log'}"), url)
    return 1


def cmd_install_launcher(args, s) -> int:
    from . import launcher

    ok, lines = launcher.install(s.data_dir)
    print("\n".join(lines))
    return 0 if ok else 1


def cmd_mcp(args, s) -> int:
    """Bosun as an MCP server (stdio) for agents or Ask Bosun, or adding/removing it in Claude Code."""
    if args.mcp_cmd is None:
        from . import mcp_server

        return mcp_server.serve(s, as_role=args.as_role)  # stdout is the protocol from here on
    from . import mcp_install

    ok, lines = mcp_install.install() if args.mcp_cmd == "install" else mcp_install.uninstall()
    print("\n".join(lines))
    return 0 if ok else 1


def cmd_actions(args, s) -> int:
    """Everything Bosun can do, with its risk tier: the same list the API, and later Claude Code, are built from."""
    from .registry import describe

    rows = describe()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    width = max(len(r["name"]) for r in rows)
    for r in rows:
        print(f"{r['name']:<{width}}  {r['risk']:<8}  {r['summary']}")
    return 0


def _print_db_info(store: Store, db_path: Path) -> int:
    from . import migrations

    version = migrations.current_version(store._db)
    try:
        applied = store._db.execute("SELECT version, name, applied FROM schema_migrations ORDER BY version").fetchall()
    except sqlite3.OperationalError:
        applied = []
    print(f"{db_path}\nschema v{version} (this code knows up to v{migrations.latest_version()})")
    for row in applied:
        print(f"  v{row['version']} {row['name']}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(row['applied']))}")
    return 0


def cmd_db(args, s) -> int:
    from . import migrations

    if args.doc:  # docs/data-model.md: uv run bosun db --doc > docs/data-model.md
        from . import datamodel

        sys.stdout.write(datamodel.render())
        return 0
    if args.migrate:
        with Store(s.db_path) as store:
            return _print_db_info(store, s.db_path)
    try:
        with Store(s.db_path, read_only=True) as store:
            return _print_db_info(store, s.db_path)
    except FileNotFoundError:
        print(f"{s.db_path}\nno database yet (this code knows up to v{migrations.latest_version()})")
        return 0


def cmd_status(args, s) -> int:
    """Short summary of what needs attention today (opens the database read-only, never migrates)."""
    from . import lock, status as status_mod

    rf = {"running": True} if lock.is_locked(s.db_path.parent / "refresh.lock") else None
    try:
        with Store(s.db_path, read_only=True) as store:
            data = status_mod.build_status(store, refresh_state=rf)
    except FileNotFoundError:
        data = status_mod.build_status(None, refresh_state=rf)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(status_mod.format_status(data))
    return 0


def _table(columns: list[str], rows: list[list], width: int = 60) -> str:
    """Query results as plain aligned text; long cells are cut (--json has them whole)."""
    def cell(v) -> str:
        text = "" if v is None else str(v).replace("\n", " ")
        return text if len(text) <= width else text[:width - 1] + "…"
    cells = [[cell(v) for v in row] for row in rows]
    widths = [max(len(c), *(len(r[i]) for r in cells)) if cells else len(c) for i, c in enumerate(columns)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(columns, widths)).rstrip(), "  ".join("-" * w for w in widths)]
    lines += ["  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip() for r in cells]
    return "\n".join(lines)


def cmd_query(args, s) -> int:
    """One read-only SELECT over the views and tables in docs/data-model.md."""
    from . import query

    try:
        if args.schema:
            found = query.schema(s.db_path)
            if args.json:
                print(json.dumps(found, indent=2))
            else:
                for name, columns in found.items():
                    print(f"{name}\n    {', '.join(columns)}")
            return 0
        if not args.sql:
            print('Give it a SELECT, e.g. bosun query "SELECT item_key, lane, risk FROM v_current_items" '
                  "(--schema lists what can be read)", file=sys.stderr)
            return 2
        result = query.run(s.db_path, args.sql, limit=args.limit)
    except query.QueryError as exc:
        print(f"query refused: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(_table(result["columns"], result["rows"]))
    more = f" (there are more: the limit is {result['limit']}, raise it with --limit)" if result["truncated"] else ""
    print(f"\n{result['row_count']} row{'' if result['row_count'] == 1 else 's'}{more}", file=sys.stderr)
    return 0


def _when(ts: float | None) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"


def _took(ms: float | None) -> str:
    if ms is None:
        return "-"
    return f"{ms / 1000:.1f}s" if ms < 120_000 else f"{ms / 60000:.1f}m"


def cmd_runs(args, s) -> int:
    """Recent runs, newest first: what Bosun did and what went wrong."""
    try:
        with Store(s.db_path, read_only=True) as store:
            rows = store.runs(limit=args.n, kind=args.kind, status="problems" if args.problems else None)
    except FileNotFoundError:
        rows = []
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No runs recorded yet.")
        return 0
    for r in rows:
        line = f"{r['trace_id'][:8]}  {_when(r['started'])}  {r['status']:<11} {_took(r['duration_ms']):>6}  " \
               f"{r['kind']:<15} {r['subject'][:50]:<50}  {r['actor']}"
        print(line)
        if r["error"] and r["status"] != "ok":
            print(f"          {r['error'].splitlines()[0][:150]}")
    return 0


def _print_tree(nodes: list[dict], depth: int = 0) -> None:
    for n in nodes:
        a = n["attrs"]
        bits = []
        if n["kind"] == "subprocess":
            bits.append(" ".join(a.get("argv") or [])[:120])
            if a.get("returncode") not in (None, 0):
                bits.append(f"→ exit {a['returncode']}")
        elif n["kind"] == "http":
            bits.append(f"{a.get('path', '')} → {a.get('status', '?')}")
        elif "items" in a:
            bits.append(f"{a['items']} items")
        mark = "✗" if n["status"] == "error" else "·"
        print(f"{'  ' * depth}{mark} {n['kind']}:{n['name']}  {_took(n['duration_ms'])}  {' '.join(bits)}")
        if n["status"] == "error":
            for key in ("error", "stderr_tail", "stdout_tail"):
                if a.get(key):
                    tail = a[key].strip().splitlines()[-6:]
                    print("\n".join(f"{'  ' * depth}    {t[:200]}" for t in tail))
                    break
        _print_tree(n["children"], depth + 1)


def cmd_trace(args, s) -> int:
    """One run's timeline: every step, command and HTTP request, failures marked with their output."""
    from . import diagnose

    try:
        with Store(s.db_path, read_only=True) as store:
            t = store.trace(args.id)
    except FileNotFoundError:
        t = None
    if t is None:
        print(f"No run matches {args.id!r} (or the prefix matches more than one).", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({**t, "tree": diagnose.span_tree(t["spans"])}, indent=2, default=str))
        return 0
    r = t["run"]
    print(f"{r['kind']} {r['subject']}  [{r['status']}]  by {r['actor']}  {_when(r['started'])}  {_took(r['duration_ms'])}")
    print(f"trace {r['trace_id']}" + (f"  (started by {t['parent']['kind']} {t['parent']['trace_id'][:8]})" if t["parent"] else ""))
    if r["summary"]:
        print(r["summary"])
    if r["error"]:
        print(f"error: {r['error']}")
    print()
    _print_tree(diagnose.span_tree(t["spans"]))
    for e in t["log_events"]:
        print(f"{e['level']} {e['logger']}: {e['message'][:300]}")
    for c in t["children"]:
        print(f"→ started {c['kind']} {c['subject'][:60]} [{c['status']}]  bosun trace {c['trace_id'][:8]}")
    return 0


def cmd_diagnose(args, s) -> int:
    """Write a redacted troubleshooting bundle for one run (default: the latest that failed)."""
    from . import diagnose

    try:
        with Store(s.db_path, read_only=True) as store:
            trace_id = args.id
            if not trace_id:
                latest = store.runs(limit=1, status="problems")
                if not latest:
                    print("Nothing has failed recently.")
                    return 0
                trace_id = latest[0]["trace_id"]
            data = diagnose.bundle(store, s, trace_id)
    except FileNotFoundError:
        if not args.id:
            print("Nothing has failed recently.")
            return 0
        print(f"No run matches {args.id!r}.", file=sys.stderr)
        return 1
    if data is None:
        print(f"No run matches {trace_id!r}.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    dest = diagnose.write_zip(data, Path(args.out) if args.out else diagnose.default_path(s, data["run"]["trace_id"]))
    if not args.out:
        diagnose.trim_bundles(s)
    print(f"{data['run']['kind']} {data['run']['subject']} [{data['run']['status']}]\n{dest}")
    return 0


def cmd_usage(args, s) -> int:
    """Where AI calls and tokens went, and what Bosun's cache saved."""
    try:
        with Store(s.db_path, read_only=True) as store:
            u = store.llm_usage(days=args.days)
    except FileNotFoundError:
        if args.json:
            print(json.dumps({"days": args.days, "totals": {"calls": 0, "cost_usd": 0}}, indent=2))
        else:
            print(f"No AI calls recorded in the last {args.days:g} days.")
        return 0
    if args.json:
        print(json.dumps(u, indent=2, default=str))
        return 0
    t = u["totals"]
    if not t["calls"]:
        print(f"No AI calls recorded in the last {args.days:g} days.")
        return 0
    k = lambda n: f"{(n or 0) / 1000:,.1f}k"  # noqa: E731
    print(f"Last {args.days:g} days: {t['calls']} calls, ${t['cost_usd'] or 0:.2f} at list price · "
          f"input {k(t['input_tokens'])} + cache read {k(t['cache_read_tokens'])} + cache write "
          f"{k(t['cache_write_tokens'])} · output {k(t['output_tokens'])}")
    c = u["cache"]
    if c["summaries_asked"]:
        print(f"Summaries from Bosun's cache: {c['summaries_from_cache']} of {c['summaries_asked']} ({c['hit_rate']:.0%})\n")
    print("By purpose")
    for p in u["by_purpose"]:
        print(f"  {p['purpose']:<10} {p['calls']:>5} calls  ${p['cost_usd'] or 0:>8.3f}  avg ${p['avg_cost_usd'] or 0:.4f}"
              f"  avg prompt {int(p['avg_prompt_chars'] or 0):,} chars")
    print("By model")
    for m in u["by_model"]:
        print(f"  {m['model']:<28} {m['calls']:>5} calls  ${m['cost_usd']:>8.3f}")
    print("By source")
    for src in u["by_source"][:10]:
        print(f"  {src['source']:<14} {src['calls']:>5} calls  ${src['cost_usd'] or 0:>8.3f}")
    print("Costliest items")
    for it in u["top_items"][:10]:
        print(f"  ${it['cost_usd'] or 0:>7.4f}  {it['calls']:>3}×  {it['purpose']:<9} {it['item_key'][:70]}")
    if u["outliers"]:
        print("Outliers (over 3× their purpose's median)")
        for o in u["outliers"][:10]:
            print(f"  ${o['cost_usd']:.4f} ({o['times_median']}×)  {o['purpose']:<9} {o['item_key'][:60]}  bosun trace {o['trace_id'][:8]}")
    return 0


_TOKEN_META_RE = re.compile(r'<meta name="bosun-token" content="([^"]+)">')


def _dashboard_http(s):
    import httpx

    return httpx.Client(
        base_url=f"http://{s.host}:{s.port}",
        timeout=httpx.Timeout(15.0, connect=5.0),
        follow_redirects=False,
        trust_env=False,
    )


def _verify_dashboard_owner(client: Any, s, key: str) -> str | None:
    """Confirm the server holds this data_dir's session key, then fetch its CSRF token as owner."""
    import httpx

    from . import auth

    try:
        probe = client.get("/api/automations", auth=auth.AgentAuth(key, "cli"))
    except httpx.ConnectError:
        return None
    if probe.status_code == 401 or not auth.valid_reply(
        key, probe.request.headers.get("authorization"), probe.headers.get(auth.REPLY_HEADER),
    ):
        return None
    try:
        page = client.get("/", headers={"cookie": f"{auth.cookie_name(s.port)}={auth.cookie_value(key)}"})
    except httpx.ConnectError:
        return None
    if page.status_code != 200:
        return None
    match = _TOKEN_META_RE.search(page.text)
    return match.group(1) if match else None


def _pause_via_dashboard(
    s, pause: bool, *, http: Any = None, up: Callable[[], bool] | None = None, as_json: bool = False,
) -> int | None:
    """Route `automations pause|resume` through the running dashboard's owner-only action."""
    import httpx

    from . import auth, launcher

    url = f"http://{s.host}:{s.port}"
    if http is None:
        if not (s.data_dir / auth.KEY_FILE).is_file():
            return None
        if not (up or (lambda: launcher.dashboard_up(url)))():
            return None
    key = auth.load_key(s.data_dir)
    client = http or _dashboard_http(s)
    try:
        try:
            token = _verify_dashboard_owner(client, s, key)
            if token is None:
                return None
            r = client.post(
                "/api/automations/pause",
                json={"on": pause},
                headers={
                    "cookie": f"{auth.cookie_name(s.port)}={auth.cookie_value(key)}",
                    "x-bosun-token": token,
                },
            )
        except httpx.ConnectError:
            return None
        except httpx.TransportError as exc:
            print(f"Could not reach the Bosun dashboard: {_printable(exc)}", file=sys.stderr)
            return 1
        if r.status_code >= 400:
            print(_http_error(r), file=sys.stderr)
            return 1
        _print_pause_result(pause, as_json=as_json)
        return 0
    finally:
        if http is None:
            client.close()


def _print_pause_result(pause: bool, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"paused": pause}, indent=2))
    elif pause:
        print("Automations paused. Waiting firings stay frozen until you run `bosun automations resume`.")
    else:
        print("Automations resumed.")


def _automations_status(args, s) -> int:
    from .automations import store as astore

    try:
        with Store(s.db_path, read_only=True) as store:
            paused = astore.is_paused(store)
            rows = astore.list_all(store)
    except FileNotFoundError:
        paused, rows = False, []
    enabled = sum(1 for r in rows if r["enabled"])
    if args.json:
        print(json.dumps({"paused": paused, "enabled": enabled, "total": len(rows)}, indent=2))
    else:
        state = "paused" if paused else "active"
        extra = " (no database yet)" if not s.db_path.exists() else f" ({enabled} of {len(rows)} enabled)"
        print(f"Automations: {state}{extra}.")
    return 0


def _automations_resume(args, s) -> int:
    from . import auth, launcher

    if not sys.stdin.isatty():
        print("Resuming automations requires an interactive terminal (TTY).", file=sys.stderr)
        return 1
    url = f"http://{s.host}:{s.port}"
    if not (s.data_dir / auth.KEY_FILE).is_file() or not launcher.dashboard_up(url):
        print("Bosun is not running; start Bosun to resume automations.", file=sys.stderr)
        return 1
    try:
        reply = input("Type 'resume' to resume all automations: ")
    except EOFError:
        reply = ""
    if reply.strip().lower() != "resume":
        print("Aborted: automations remain paused.", file=sys.stderr)
        return 1
    routed = _pause_via_dashboard(s, False, as_json=args.json)
    if routed is None:
        print("Bosun is not running; start Bosun to resume automations.", file=sys.stderr)
        return 1
    return routed


def cmd_automations(args, s) -> int:
    """Inspect or toggle the owner-only automations kill switch (`status`, `pause`, `resume`)."""
    from . import trace
    from .automations import store as astore
    from .registry import Risk

    if args.action == "status":
        return _automations_status(args, s)
    if args.action == "resume":
        return _automations_resume(args, s)

    routed = _pause_via_dashboard(s, True, as_json=args.json)
    if routed is not None:
        return routed
    with Store(s.db_path) as store:
        astore.set_paused(store, True)
        store.record_action(
            actor="user", action="pause_automations", args={"on": True},
            risk=Risk.LOCAL.name.lower(), surface="cli", trace_id=trace.new_id(), outcome="ok",
        )
    _print_pause_result(True, as_json=args.json)
    return 0


_READ_ONLY_CMDS = frozenset({"actions", "doctor", "query", "runs", "trace", "usage", "diagnose", "status"})
_NO_DB_LOG_CMDS = frozenset({"open", "install-launcher", "mcp"})


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bosun", description="Bosun: updates + security for this machine")
    sub = p.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status", help="what needs attention today (read-only: never migrates the database)")
    st.add_argument("--json", action="store_true", help="machine-readable JSON (same schema as /api/status)")
    r = sub.add_parser("refresh", help="collect, enrich, score; save a snapshot")
    r.add_argument("--only", help="re-collect just these (the rest of the latest snapshot is kept): comma list, "
                   "e.g. winget,choco,npm-global,pip-global,wsl-apt,windows,repos,secrets "
                   "(macOS: brew,mac-apps,softwareupdate,runtimes,npm-global,repos; "
                   "Linux: apt,snap,brew,runtimes,npm-global,repos)")
    r.add_argument("--no-auto", action="store_true", help="skip auto-upgrades this run")
    sub.add_parser("update-scanner", help="install or update bin/osv-scanner now (refresh also does this daily)")
    su = sub.add_parser("self-update", help="move this checkout to the newest signed release now (refresh also checks daily)")
    su.add_argument("--repin", metavar="vX.Y.Z", help="a release refused as moved: hold it to what it points at now "
                    "(after you have checked that is the real release); it waits again like a new one, and nothing "
                    "is installed by this command")
    sub.add_parser("serve", help="run the dashboard on 127.0.0.1")
    sub.add_parser("demo", help="the dashboard on made-up data, with every action that changes anything refused "
                   "(for a first look and for screenshots)")
    doc = sub.add_parser("doctor", help="what this machine has and lacks for Bosun to work (changes nothing)")
    doc.add_argument("--providers", action="store_true",
                     help="opt-in live check: send one tiny prompt to each installed AI login and record its windows")
    op = sub.add_parser("open", help="open the dashboard in its own window, starting it first if it isn't running")
    op.add_argument("--print-url", action="store_true", help=argparse.SUPPRESS)  # for Bosun's own window on macOS
    sub.add_parser("install-launcher", help="a Bosun app icon: Start menu, ~/Applications, or your Linux app list "
                   "(install-tasks makes it too)")
    t = sub.add_parser("install-tasks", help="daily refresh + dashboard at login (per-user, no admin; launchd on macOS, "
                       "systemd --user on Linux)")
    t.add_argument("--time", default="08:30", help="daily refresh time (HH:MM)")
    mc = sub.add_parser("mcp", help="Bosun as an MCP server for Claude Code (stdio); `mcp install` adds it to Claude Code")
    mc.add_argument("mcp_cmd", nargs="?", choices=("install", "uninstall"),
                    help="install: add it to Claude Code for every project; uninstall: remove it")
    mc.add_argument("--as", dest="as_role", choices=("agent", "assistant", "triage"), default="agent",
                    help="who is calling: agent (default), assistant (Ask Bosun), or triage (read-only error triage)")
    mc.add_argument("--port", type=int, default=None, help=argparse.SUPPRESS)
    a = sub.add_parser("actions", help="everything Bosun can do, with its risk tier")
    a.add_argument("--json", action="store_true", help="with input schemas, as JSON")
    db = sub.add_parser("db", help="where the database is and which schema migrations it has (read-only unless --migrate)")
    db.add_argument("--doc", action="store_true", help="print docs/data-model.md, generated from the schema")
    db.add_argument("--migrate", action="store_true", help="create or apply any pending schema migrations now")
    q = sub.add_parser("query", help="ask the database a question: one read-only SELECT (see docs/data-model.md)")
    q.add_argument("sql", nargs="?", help='e.g. "SELECT item_key, lane, risk FROM v_current_items ORDER BY risk DESC"')
    q.add_argument("--limit", type=int, default=500, help="most rows to return (default 500, at most 5000)")
    q.add_argument("--json", action="store_true", help="columns and rows as JSON")
    q.add_argument("--schema", action="store_true", help="list the views and tables that can be read, with their columns")
    ru = sub.add_parser("runs", help="recent runs: refreshes, upgrades, fixes, actions; what failed and why")
    ru.add_argument("--problems", action="store_true", help="only failed, partial or interrupted runs")
    ru.add_argument("--kind", help="refresh, recheck, upgrade, fix, pin, relaunch, action...")
    ru.add_argument("-n", type=int, default=30, help="how many (default 30)")
    ru.add_argument("--json", action="store_true")
    tr = sub.add_parser("trace", help="one run's timeline: steps, commands, HTTP calls, failures with their output")
    tr.add_argument("id", help="trace id or its first 8 characters (bosun runs and bosun.log show them)")
    tr.add_argument("--json", action="store_true")
    us = sub.add_parser("usage", help="where Claude tokens went: per purpose, model, source and item; what the cache saved")
    us.add_argument("--days", type=float, default=30)
    us.add_argument("--json", action="store_true")
    dg = sub.add_parser("diagnose", help="a redacted troubleshooting bundle (zip) for one run, to read or hand to an agent")
    dg.add_argument("id", nargs="?", help="trace id or prefix (default: the latest run that failed)")
    dg.add_argument("--out", help="where to write the zip")
    dg.add_argument("--json", action="store_true", help="print the bundle instead of writing a zip")
    au = sub.add_parser("automations", help="check or toggle the owner-only automations kill switch")
    au.add_argument("action", nargs="?", choices=("status", "pause", "resume"), default="status",
                    help="status (read-only, default), pause (freeze all automations), or resume")
    au.add_argument("--json", action="store_true", help="machine-readable JSON")
    args = p.parse_args(argv)
    if args.cmd == "serve":  # before the settings, the log and the database: any of them may be what broke
        args.on_probation = startguard.guard_serve()
    try:
        s = load_settings()
    except Exception as exc:
        if getattr(args, "on_probation", False):
            startguard.failed(startguard.data_dir(), f"the settings did not load ({type(exc).__name__}: {exc})")
        raise
    demo_folder = None
    if args.cmd == "demo":  # a folder of its own, and nothing that reaches outside it: set before logging opens anything
        import dataclasses
        import tempfile
        demo_folder = tempfile.TemporaryDirectory(prefix="bosun-demo-data-")
        s = dataclasses.replace(s, data_dir=Path(demo_folder.name), demo=True, self_update=False, osv_auto_update=False)
        if not os.environ.get("BOSUN_PORT"):  # never the real dashboard's port: that one may be running right now
            s = dataclasses.replace(s, port=_free_port(s.host))
    if args.cmd == "mcp" and getattr(args, "port", None) is not None and 1 <= args.port <= 65535:
        import dataclasses
        s = dataclasses.replace(s, port=args.port)
    read_only = (
        (args.cmd in _READ_ONLY_CMDS and not (args.cmd == "doctor" and getattr(args, "providers", False)))
        or (args.cmd == "db" and not args.migrate)
        or (args.cmd == "automations" and args.action == "status")
    )
    embedded_mcp = args.cmd == "mcp" and args.mcp_cmd is None and getattr(args, "as_role", "agent") in ("assistant", "triage")
    # Read-only commands must never create directories, write logs, or open/migrate the database.
    # `mcp --as assistant|triage` logs to stderr only so it never needs BOSUN_DATA_DIR forwarded.
    # `refresh` and `automations` defer logging/DB setup until they know the dashboard is not running.
    if embedded_mcp:
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    elif not read_only and args.cmd not in ("refresh", "automations"):
        _setup_logging(s, "serve" if args.cmd == "serve" else "cli", open_db=args.cmd not in _NO_DB_LOG_CMDS)
    handler = {"status": cmd_status, "refresh": cmd_refresh, "update-scanner": cmd_update_scanner,
               "self-update": cmd_self_update, "serve": cmd_serve, "demo": cmd_demo, "doctor": cmd_doctor,
               "open": cmd_open, "install-tasks": cmd_install_tasks, "install-launcher": cmd_install_launcher,
               "mcp": cmd_mcp, "actions": cmd_actions, "db": cmd_db, "query": cmd_query, "runs": cmd_runs,
               "trace": cmd_trace, "diagnose": cmd_diagnose, "usage": cmd_usage,
               "automations": cmd_automations}[args.cmd]
    from .migrations import MigrationRefused
    try:
        code = handler(args, s)
        if code and getattr(args, "on_probation", False):
            startguard.failed(s.data_dir, f"bosun serve exited with {code} before it served")
        return code
    except MigrationRefused as exc:
        if getattr(args, "on_probation", False):
            startguard.failed(s.data_dir, f"MigrationRefused: {exc}")
        print(exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except sqlite3.OperationalError as exc:
        if not read_only:
            raise
        # read-only commands don't migrate: after an update, the database waits for the dashboard to bring it up
        print(f"bosun: could not read the database ({_printable(exc)}). If Bosun was just updated, the dashboard "
              "brings the database up to date when it starts; try again then, or run `bosun refresh`.", file=sys.stderr)
        return 2
    except Exception as exc:
        if getattr(args, "on_probation", False):
            startguard.failed(s.data_dir, f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if demo_folder is not None:
            demo_folder.cleanup()


if __name__ == "__main__":
    sys.exit(main())
