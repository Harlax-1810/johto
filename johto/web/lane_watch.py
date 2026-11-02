"""Watches lanes. When Claude exits in a lane, collect the run, get its review pack, tell Austin and open
the task page where he writes the fields.

The pack is the next one for the run (delta.next_pack: a full pack the first time, then a follow-up), with an
outside AI review only when settings "pack_outside_review" is on. When a pack already shows the run as it
ended (the turn watcher packed that checkpoint), that pack is used and nothing is built.

A run counts as ended when its container is running, its transcript has at least one prompt from Austin, no
Claude process is left in the container, and the transcript hasn't changed for 20 s. Each transcript version
is handled once; if he resumes the session (./claude-dev) and exits again, the newer transcript is collected
again.

Other modules see the phases: STATE["<short>/<run>"] = {phase: collecting|packing|done|failed, message, at,
pack, short, run}; listen(fn) calls fn(key, entry) on every change (from worker threads); OPEN_HOOK, when set
(the lane service sets it), gets the first chance to open the task page in a tab that is already open.
collect_run() is the one way to collect, used by the watcher, the lane service and its retry button.

Notifications: one per run (when its pack is ready, or when the collect ends without one), never while a Johto
tab is visible and focused; a hidden tab gets a web notification, and with no tab open macOS shows it.
JOHTO_NO_NOTIFY=1, and every fake-mode server, turn off macOS notifications and the browser `open`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import secrets
import threading
import time
import traceback
import urllib.parse
from pathlib import Path
from typing import Callable

from .. import config, lanes, workspace
from .lane import monitor

STATE: dict[str, dict] = {}       # "short/run" -> {phase, message, at, pack, short, run}
OPEN_HOOK: Callable[[str, str], bool] | None = None
_LISTENERS: list[Callable[[str, dict], None]] = []
_LOCK = threading.Lock()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None
QUIET_S = 20
EVERY_S = 6


def listen(fn: Callable[[str, dict], None]) -> None:
    if fn not in _LISTENERS:
        _LISTENERS.append(fn)


def unlisten(fn: Callable[[str, dict], None]) -> None:
    if fn in _LISTENERS:
        _LISTENERS.remove(fn)


def snapshot() -> dict[str, dict]:
    return {k: dict(v) for k, v in STATE.items()}


def phase(short: str, run: str) -> str | None:
    return (STATE.get(f"{short}/{run}") or {}).get("phase")


def _set(key: str, **entry) -> None:
    short, _, run = key.partition("/")
    STATE[key] = {"short": short, "run": run, "at": time.time(), **entry}
    for fn in list(_LISTENERS):
        try:
            fn(key, dict(STATE[key]))
        except Exception:
            traceback.print_exc()


def _claim(key: str) -> bool:
    """Mark a run as collecting unless something is already collecting or packing it."""
    with _LOCK:
        if (STATE.get(key) or {}).get("phase") in ("collecting", "packing"):
            return False
        _set(key, phase="collecting", message="Collecting the run")
        return True


def _quiet() -> bool:
    """No macOS notifications and no `open` of his browser: tests, scratch servers and every fake-mode server."""
    return config.FAKE or os.environ.get("JOHTO_NO_NOTIFY", "").strip().lower() in ("1", "true", "yes")


def _claude_running(container: str) -> bool:
    # A probe that fails or times out counts as running: the watcher waits for the next round.
    return monitor.claude_alive(container, unknown=True)


def _tabs() -> int:
    try:
        from . import events
        return events.hub.clients()
    except Exception:
        return 0


def _looking() -> bool:
    """A Johto tab is connected, visible and focused (design 6: then the in-app toast is enough)."""
    try:
        from . import app
        p = app.PRESENCE
    except Exception:
        return False
    return _tabs() > 0 and bool(p.get("visible")) and bool(p.get("focused"))


def _base_url() -> str:
    try:
        from . import app
        return f"http://127.0.0.1:{app.PORT}"
    except Exception:
        return f"http://127.0.0.1:{config.PORT}"


def _notify(title: str, body: str, href: str = "/", tag: str | None = None) -> None:
    """One notification, only when he isn't looking: a hidden tab gets a web notification through SSE; with no
    tab open, macOS shows one (terminal-notifier when installed, so a click opens the page)."""
    if _looking():
        return
    if _tabs():
        try:
            from . import events
            events.hub.publish("notify", {"id": tag or title, "title": title, "body": body, "href": href,
                                          "tag": tag or title, "sound": False})
        except Exception:
            traceback.print_exc()
        return
    if _quiet():
        return
    tn = shutil.which("terminal-notifier")
    if tn:
        subprocess.run([tn, "-title", title, "-message", body, "-open", _base_url() + href, "-group", tag or title],
                       capture_output=True, timeout=10)
        return
    script = f'display notification "{body.replace(chr(34), "")}" with title "{title.replace(chr(34), "")}"'
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)


def _next_href(short: str) -> str:
    """Where to land: the task's next step on Home (/#item=<id>), else its task page."""
    try:
        from . import data
        snap = data.real_snapshot()
        steps = {"decide", "rate", "write_details", "write_prompt", "save"}
        item = next((i for i in snap.get("queue") or [] if i.get("short") == short and i.get("kind") in steps), None)
        if item:
            return f"/#item={urllib.parse.quote(item['id'])}"
    except Exception:
        traceback.print_exc()
    return f"/task/{short}"


def _open_task(short: str, fragment: str = "") -> None:
    """Show him the run's next step: an open tab moves there itself (OPEN_HOOK); with no tab, open one at the
    task's next step on Home, the same place the tab would go."""
    hook = OPEN_HOOK
    if hook:
        try:
            if hook(short, fragment):
                return
        except Exception:
            traceback.print_exc()
    if _quiet() or _tabs():
        return
    subprocess.Popen(["open", _base_url() + _next_href(short)])


def _run_word(run: str) -> str:
    try:
        from .domain.queue import run_word
        return run_word(run)
    except Exception:
        return run


def _mark(d: Path, run: str, **fields) -> None:
    lock = getattr(workspace, "locked", None)
    ctx = lock(d) if lock else None
    if ctx:
        ctx.__enter__()
    try:
        meta = workspace.load(d)
        for rec in meta.get("runs", []):
            if rec["dir"] == f"runs/{run}":
                rec.setdefault("auto", {}).update(fields)
        workspace.save(d, meta)
    finally:
        if ctx:
            ctx.__exit__(None, None, None)


def _newest_transcript(run_dir: Path) -> Path | None:
    files = monitor.transcripts(monitor.lane_paths(run_dir)["projects"])
    return files[-1] if files else None


def _has_prompt(tpath: Path) -> bool:
    """At least one message from Austin (a /login and /exit alone write a transcript too)."""
    red = monitor.Reducer()
    red.feed(monitor.Tail(tpath).read())
    return red.humans > 0


def _lanes():
    try:
        from .. import actions
        return actions.L()
    except Exception:
        return lanes


def _wait(job: dict) -> None:
    done = job.get("_done")
    while job["state"] == "running" and not _STOP.is_set():
        if done is not None:
            done.wait(0.5)
        else:
            time.sleep(0.5)


def _build(d: Path, run: str) -> dict:
    """delta.next_pack in this thread, shown as a pack job in the UI (api_pack.JOBS). A pack job already
    running for the run is waited on instead. Returns the job: state done (out) or failed (error)."""
    from .. import delta
    from . import api_pack
    with api_pack._LOCK:
        live = next((j for j in api_pack.JOBS.values()
                     if j["short"] == d.name and j["run"] == run and j["state"] == "running"), None)
        if live is None:
            job = {"id": secrets.token_hex(4), "short": d.name, "run": run, "ai": None, "state": "running", "log": [],
                   "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "out": None, "tokens": None, "error": None,
                   "_done": threading.Event()}
            api_pack.JOBS[job["id"]] = job
    if live is not None:
        _wait(live)
        return live
    try:
        job.update(state="done", out=str(delta.next_pack(d, run, log=job["log"].append)))
    except BaseException as e:  # SystemExit carries plain-words messages
        job.update(state="failed", error=str(e) or e.__class__.__name__)
    finally:
        job["_done"].set()
    return job


def _pack(d: Path, run: str, key: str, label: str, summary: str = "") -> None:
    """Get the run's review pack: the one that already shows this checkpoint, else the next one (built here).
    Then mark the run done and send the run's one notification."""
    from .. import delta
    rec = None
    try:
        rec = delta.checkpoint_packed(d, run)
    except Exception:
        traceback.print_exc()
    if rec:
        job = {"state": "done", "out": rec["path"]}
    else:
        _set(key, phase="packing", message="Building the review pack")
        job = _build(d, run)
    rw = _run_word(run)
    if job["state"] == "done":
        _mark(d, run, pack=job["out"])
        _set(key, phase="done", message="Review pack ready", pack=job["out"])
        _notify(f"Johto: {rw} ready", f"{label} · {summary + ' · ' if summary else ''}review pack ready",
                _next_href(d.name), tag=key)
    else:
        _set(key, phase="failed", message=job.get("error") or "pack failed", collected=True)
        _notify(f"Johto: {rw} collected", f"{label} · {summary + ' · ' if summary else ''}the review pack failed: "
                f"{job.get('error') or 'see the task page'}", _next_href(d.name), tag=key)


def _plural(n, one: str) -> str:
    return f"{n} {one}" + ("" if n == 1 else "s")


def _summary_line(s: dict) -> str:
    bits = []
    if "follow_ups" in s:
        bits.append(_plural(len(s.get("follow_ups") or []), "follow-up"))
    if s.get("active_minutes") is not None:
        m = s["active_minutes"]
        bits.append(f"{m} working minute{'' if m == 1 else 's'}")
    return ", ".join(bits)


def collect_run(d: Path, run: str, mtime: float | None = None, pack: bool | str = True) -> dict:
    """Collect a run, open its task page, then build its review pack.

    pack: True waits for the pack, "background" builds it in a thread and returns after the collect, False
    skips it. Returns the run's STATE entry; phase "failed" carries the message. Safe to call from any thread;
    a run already being collected is left alone (its current entry comes back).
    """
    d = Path(d)
    key = f"{d.name}/{run}"
    if not _claim(key):
        return dict(STATE.get(key) or {})
    try:
        label = workspace.load(d)["task_name"]
        if mtime is None:
            t = _newest_transcript(d / "runs" / run)
            mtime = t.stat().st_mtime if t else time.time()
        info = _lanes().collect(d, run)
        _mark(d, run, transcript_mtime=mtime, collected_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        summary = _summary_line(info.get("summary") or {})
        # The page to fill in opens now; the run's one notification goes out when its pack is ready.
        _open_task(d.name, "field=validation" if run.startswith("validate") else "field=goal")
        if not pack:
            _set(key, phase="done", message="Collected")
            _notify(f"Johto: {_run_word(run)} collected", f"{label}{' · ' + summary if summary else ''}",
                    _next_href(d.name), tag=key)
        elif pack == "background":
            _set(key, phase="packing", message="Building the review pack")
            threading.Thread(target=_pack_safe, args=(d, run, key, label, summary), daemon=True, name=f"pack-{key}").start()
        else:
            _pack(d, run, key, label, summary)
    except BaseException as e:  # never let a caller (or the watcher) die on one run
        _set(key, phase="failed", message=str(e) or e.__class__.__name__)
        traceback.print_exc()
    return dict(STATE.get(key) or {})


def _pack_safe(d: Path, run: str, key: str, label: str, summary: str = "") -> None:
    try:
        _pack(d, run, key, label, summary)
    except BaseException as e:
        _set(key, phase="failed", message=str(e) or e.__class__.__name__, collected=True)
        traceback.print_exc()


def _process(d: Path, run: str, mtime: float) -> None:
    collect_run(d, run, mtime, pack=True)


def tick() -> None:
    try:
        running = set(monitor.running_containers())
    except Exception:
        return
    for tj in sorted(config.TASKS.glob("*/task.json")):
        d = tj.parent
        try:
            meta = workspace.load(d)
        except Exception:
            continue
        for rec in meta.get("runs", []):
            run_dir = d / rec["dir"]
            container = rec.get("container") or (lanes.manifest(run_dir).get("lane") or {}).get("container")
            key = f"{d.name}/{run_dir.name}"
            if not container or container not in running:
                continue
            if STATE.get(key, {}).get("phase") in ("collecting", "packing"):
                continue
            tpath = _newest_transcript(run_dir)
            if not tpath:
                continue                      # Claude never started in this lane
            mtime = tpath.stat().st_mtime
            if (rec.get("auto") or {}).get("transcript_mtime", 0) >= mtime:
                continue                      # this version was already collected
            if time.time() - mtime < QUIET_S:
                continue
            if not _has_prompt(tpath):
                continue                      # only /login or /exit: nothing ran
            if _claude_running(container):
                continue
            threading.Thread(target=_process, args=(d, run_dir.name, mtime), daemon=True,
                             name=f"collect-{key}").start()


def _loop() -> None:
    while not _STOP.wait(EVERY_S):
        try:
            tick()
        except Exception:
            traceback.print_exc()


def start() -> None:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, daemon=True, name="lane-watch")
    _THREAD.start()


def stop() -> None:
    _STOP.set()
