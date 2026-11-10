"""Lane control for the web app: state, start, typing, end, collect, Ghostty, and the lane terminals.

One lane at a time (all lanes bind the same host ports). The service:

- derives `LaneState` (build-plan section 4) every 2 s from the container, its tmux pane and the live
  transcript (monitor.py); the events engine reads it through `api_lane.lane_state()` and publishes `lane`;
- starts a lane on Austin's press: `actions.start_lane` (lanes.new_lane in a new runs/<kind>-N folder) in a
  `lane_prepare` job, then `model_a/claude-dev --model claude-opus-5-5 --effort high` in a server-side PTY.
  claude-dev creates the tmux session "lane" in the container, so Ghostty and the CLI can attach to it too;
- types exactly three things into the lane, each on a press and only in the right state: the saved opening
  prompt as a bracketed paste with no Enter, `/login` + Enter, and `/exit` + Enter;
- leaves collecting to lane_watch (collect, review pack, notification, task page) and shows its phases.

It never writes to the lane's config or workspace, never starts Claude on its own (on its own it only
attaches to a tmux session that already exists), and never stops a lane whose run has not been collected.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import subprocess
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable

from ... import config, lanes, transcript, workspace
from . import monitor
from .pty_bridge import PASTE_END, PASTE_START, Client, Pty, strip_enters

KINDS = ("calib", "validate")
LAUNCH_ARGS = ["--model", "claude-opus-5-5", "--effort", "high"]
POLL_S = 2.0


class LaneError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}


def iso(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t or time.time()))


def _parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def rev(text: str) -> str:
    f = getattr(workspace, "rev", None)
    return f(text) if f else hashlib.sha1(text.strip().encode()).hexdigest()[:12]


def _label(meta: dict | None) -> str:
    meta = meta or {}
    try:
        from .. import data as wdata
        return wdata._label(meta, wdata._lead_by_id(meta.get("lead_id")))
    except Exception:
        repo = (meta.get("repo") or {}).get("full_name", "").split("/")[-1]
        return meta.get("label") or f"{repo} · {meta.get('task_name', '')}".strip(" ·")


def _mode(meta: dict | None) -> str:
    return "async" if (((meta or {}).get("slot") or {}).get("interaction_mode") or "").startswith("Async") \
        else "interactive"


@contextlib.contextmanager
def _locked(d: Path):
    lock = getattr(workspace, "locked", None)
    if lock:
        with lock(d):
            yield
    else:
        yield


def _update_meta(d: Path, fn: Callable[[dict], None]) -> None:
    with _locked(d):
        meta = workspace.load(d)
        fn(meta)
        workspace.save(d, meta)


def launch_argv(run_dir: Path) -> list[str]:
    f = getattr(lanes, "launch_argv", None)
    if f:
        return list(f(run_dir))
    return [str(Path(run_dir) / "model_a" / "claude-dev"), *LAUNCH_ARGS]


def attach_argv(container: str, workspace_dir: str | None) -> list[str]:
    """Join the existing tmux session. Fails (and starts nothing) when there is no session."""
    return ["docker", "exec", "-it", "-u", "lane", "-e", "HOME=/home/lane",
            *(["-w", workspace_dir] if workspace_dir else []), container,
            "tmux", "-f", "/home/lane/.tmux.lane.conf", "attach-session", "-t", "lane"]


def shell_argv(container: str, workspace_dir: str | None) -> list[str]:
    return ["docker", "exec", "-it", "-u", "lane", "-e", "HOME=/home/lane",
            *(["-w", workspace_dir] if workspace_dir else []), container, "bash", "-l"]


def ghostty_argv(run_dir: Path) -> list[str]:
    """A new Ghostty window running the lane's claude-dev, which joins the tmux session "lane"."""
    model_a = Path(run_dir) / "model_a"
    return ["open", "-na", "Ghostty.app", "--args", f"--working-directory={model_a}", "-e",
            str(model_a / "claude-dev"), *LAUNCH_ARGS]


def _default_setup(d: Path, kind: str, log: Callable[[str], None]) -> Path:
    """A new runs/<kind>-N folder through the shared action (the fake lanes in fake mode)."""
    from ... import actions
    return actions.start_lane(d, kind, log=log)


def _default_gate(d: Path, kind: str) -> None:
    """The prompt checks a new validation lane must pass (same prompt as an earlier run, AutoQC); setup runs
    them again. Raises actions.ActionError."""
    from ... import actions
    actions.lane_gate(d, kind)


def _default_ghostty(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=20)


def _jobs():
    from .. import jobs
    return jobs


def _collect_and_wait(d: Path, run: str, timeout_s: float = 900) -> dict:
    """lane_watch.collect_run, or, when the watcher is already collecting this run, wait for that collect.
    Never returns while the run is still being collected (so its container isn't stopped mid-copy)."""
    from .. import lane_watch
    res = lane_watch.collect_run(d, run, pack="background")
    end = time.time() + timeout_s
    while res.get("phase") == "collecting" and time.time() < end:
        time.sleep(1)
        res = dict(lane_watch.STATE.get(f"{Path(d).name}/{run}") or {})
    if res.get("phase") == "collecting":
        return {"phase": "failed", "message": "the collect is taking too long"}
    return res


def _docker_free() -> bool:
    return monitor.docker_free()


def _action_error(code: str, message: str, details: dict | None = None) -> Exception:
    try:
        from ... import actions
        return actions.ActionError(code, message, details or {})
    except Exception:
        return RuntimeError(message)


# --- the service --------------------------------------------------------------------------------------------

class LaneService:
    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.running = False
        self.pty: Pty | None = None
        self.pty_meta: dict | None = None          # {short, run, container, kind: launch|attach, at}
        self.pty_failed: dict | None = None        # {short, run, evidence}
        self.clients: set[Client] = set()          # /ws/lane sockets, with or without a PTY
        self.nav_clients: set[Client] = set()      # sockets that can navigate the page (caps: navigate)
        self.unlocked: set[tuple[str, str]] = set()
        self._auto_override: bool | None = None
        self.setup: Callable = _default_setup       # tests replace these
        self.gate: Callable = _default_gate
        self.open_ghostty: Callable = _default_ghostty
        self._listeners: list[Callable[[str, dict], None]] = []
        self._state: dict = self._blank()
        self._probe: dict = {}
        self._held: float | None = None
        self._since: tuple = ("free", None, time.time())
        self._tails: dict[str, tuple[monitor.Tail, monitor.Reducer]] = {}
        self._sumcache: dict[str, tuple[int, dict]] = {}
        self._probe_lock: asyncio.Lock | None = None
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._last_auto: str | None = None
        self._failed_job: dict | None = None
        self._pub_head: str | None = None
        self._pub_at = 0.0
        self._pub_due = False
        self._size = (100, 32)                     # the last size a lane socket asked for

    # -- lifecycle
    def start(self) -> None:
        """Start the monitor on the running event loop (call from the app's lifespan)."""
        self._ensure_loop()
        self.running = True
        if not self._task or self._task.done():
            self._task = self.loop.create_task(self._run())
        try:
            from .. import lane_watch
            lane_watch.listen(self._watch_event)
            lane_watch.OPEN_HOOK = self._navigate
        except Exception:
            traceback.print_exc()

    def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
        if self.pty:
            self.pty.kill()   # detaches the tmux client; Claude keeps running
        for c in list(self.clients):
            c.close()
        self.clients.clear()
        self.nav_clients.clear()
        try:
            from .. import lane_watch
            if lane_watch.OPEN_HOOK == self._navigate:
                lane_watch.OPEN_HOOK = None
        except Exception:
            pass

    def _ensure_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self.loop is not loop:
            self.loop = loop
            self._probe_lock = asyncio.Lock()
            self._wake = asyncio.Event()

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                traceback.print_exc()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=POLL_S)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def poke(self) -> None:
        """Probe again soon. Safe from any thread."""
        if self._wake and self.loop and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:
                pass

    # -- events
    def listen(self, fn: Callable[[str, dict], None]) -> None:
        """fn(event_name, data): `lane` with the new LaneState on every change (tests, other hubs)."""
        if fn not in self._listeners:
            self._listeners.append(fn)

    on_change = listen

    def _changed(self) -> None:
        """Publish `lane` on the SSE hub (a state change at once, facts alone at most once a second), tell the
        listeners, and poke the events engine so the queue and task rows follow. The engine leaves `lane`
        events to this service while it runs."""
        data = self.state()
        head = json.dumps({k: v for k, v in data.items() if k != "facts"}, sort_keys=True, default=str)
        now = time.time()
        if head != self._pub_head or now - self._pub_at >= 1.0:
            self._pub_head, self._pub_at = head, now
            try:
                from .. import events
                events.hub.publish("lane", data)
            except Exception:
                traceback.print_exc()
        else:
            self._pub_due = True
        for fn in list(self._listeners):
            try:
                fn("lane", data)
            except Exception:
                traceback.print_exc()
        try:
            from .. import events
            events.poke("lane")
        except Exception:
            pass

    def _watch_event(self, key: str, entry: dict) -> None:
        """lane_watch changed a run's phase (it runs in threads)."""
        self.poke()

    def _navigate(self, short: str, fragment: str) -> bool:
        """lane_watch wants the task page open. A tab that can navigate does it in place."""
        if not self.loop or not self.nav_clients:
            return False
        href = f"/task/{short}#{fragment}"

        def send():
            for c in list(self.nav_clients):
                c.text({"type": "navigate", "href": href, "reason": "collected"})
        self.loop.call_soon_threadsafe(send)
        return True

    # -- probing (blocking; runs in a thread, one at a time)
    def _reducer_for(self, path: Path | None) -> monitor.Reducer | None:
        if path is None:
            return None
        key = str(path)
        if key not in self._tails:
            self._tails = {k: v for k, v in self._tails.items() if Path(k).parent.parent == path.parent.parent}
            self._tails[key] = (monitor.Tail(path), monitor.Reducer())
        tail, red = self._tails[key]
        red.feed(tail.read())
        return red

    def _summary(self, path: Path | None) -> dict | None:
        if path is None:
            return None
        try:
            size = path.stat().st_size
        except OSError:
            return None
        hit = self._sumcache.get(str(path))
        if hit and hit[0] == size:
            return hit[1]
        try:
            s = transcript.summarize(path)
        except Exception:
            return hit[1] if hit else None
        self._sumcache = {str(path): (size, s)}
        return s

    @staticmethod
    def _collect_phase(info: dict, newest: Path | None) -> str | None:
        """'collecting', 'done' (this transcript version is collected), 'failed' or None."""
        try:
            from .. import lane_watch
            ph = (lane_watch.STATE.get(f"{info['short']}/{info['run']}") or {}).get("phase")
        except Exception:
            ph = None
        if ph == "collecting":
            return "collecting"
        rec = info.get("rec") or {}
        if newest is not None:
            try:
                m = newest.stat().st_mtime
            except OSError:
                m = 0
            auto = (rec.get("auto") or {}).get("transcript_mtime") or 0
            col = _parse_iso(rec.get("collected_at"))
            if auto >= m - 0.001 or (col and col >= m):
                return "done"
        elif rec.get("collected_at"):
            return "done"
        if ph in ("packing", "done"):
            return "done"
        return "failed" if ph == "failed" else None

    def _probe_now(self) -> dict:
        if _docker_free():
            return {"containers": [], "known": [], "foreign": [], "alive": [], "index": {}, "cur": None,
                    "info": None, "look": None, "at": time.time()}
        containers = monitor.running_containers()
        index = monitor.run_index()
        known = [c for c in containers if c in index]
        foreign = [c for c in containers if c not in index]
        looks = {c: monitor.look(c) for c in known}
        alive = [c for c in known if looks[c]["alive"]]
        cur = alive[0] if alive else None
        if not cur and self.pty_meta and self.pty_meta.get("container") in known:
            cur = self.pty_meta["container"]
        if not cur and known:
            cur = max(known, key=lambda c: (index[c].get("created_at") or "", index[c]["run"]))
        p = {"containers": containers, "known": known, "foreign": foreign, "alive": alive, "index": index,
             "cur": cur, "info": index.get(cur) if cur else None, "look": looks.get(cur) if cur else None,
             "at": time.time()}
        info = p["info"]
        if info:
            paths = monitor.lane_paths(info["dir"])
            files = monitor.transcripts(paths["projects"])
            newest = files[-1] if files else None
            p.update(paths=paths, files=files, newest=newest, red=self._reducer_for(newest),
                     summary=self._summary(newest),
                     creds=bool(paths["cc_config"] and (paths["cc_config"] / ".credentials.json").exists()),
                     phase=self._collect_phase(info, newest))
        return p

    async def refresh(self) -> dict:
        self._ensure_loop()
        async with self._probe_lock:
            p = await asyncio.to_thread(self._probe_now)
            self._apply(p)
        return self.state()

    # -- state
    @staticmethod
    def _blank() -> dict:
        return {"state": "free", "rate_limited": None, "since": iso(), "run": None,
                "pty": {"alive": False, "clients": 0}, "question": None, "evidence": None, "facts": None,
                "input_locked": False, "queue": [], "auto_prepare": False, "foreign": None,
                "launchable": False, "session": False, "input": None, "job": None}

    def state(self) -> dict:
        s = json.loads(json.dumps(self._state, default=str))
        s["pty"] = {"alive": bool(self.pty and self.pty.alive), "clients": len(self.clients)}
        return s

    @staticmethod
    def _lane_queue() -> list[dict]:
        out = []
        for tj in sorted(config.TASKS.glob("*/task.json")):
            try:
                meta = json.loads(tj.read_text())
            except (OSError, ValueError):
                continue
            r = meta.get("ready")
            if not isinstance(r, dict) or r.get("kind") not in KINDS:
                continue
            out.append({"short": tj.parent.name, "kind": r["kind"], "label": _label(meta), "since": r.get("at")})
        out.sort(key=lambda x: (x["kind"] != "validate", x["since"] or ""))
        return out

    @staticmethod
    def _facts(p: dict) -> dict | None:
        red, s, info = p.get("red"), p.get("summary"), p.get("info")
        if not red or not s or not info:
            return None
        meta = info["meta"]
        others = sum((workspace.run_minutes(r, info.get("task_dir")) or 0) for r in meta.get("runs", [])
                     if r.get("dir") != f"runs/{info['run']}")
        ws = (p.get("paths") or {}).get("workspace") or ""
        files = [f[len(ws):].lstrip("/") if ws and f.startswith(ws) else f for f in red.files]
        models = s.get("models") or {}
        efforts = s.get("efforts") or {}
        return {"turns": s["human_turns"], "follow_ups": len(s["follow_ups"]), "active_min": s["active_minutes"],
                "wall_min": s["wall_minutes"], "task_claude_min": round(others + s["active_minutes"], 1),
                "files_edited": files, "tests": red.test_facts(), "transcripts": len(p.get("files") or []),
                "model_ok": (all("opus-5-5" in m for m in models) if models else None),
                "effort_ok": (set(efforts) == {"high"} if efforts else None)}

    @staticmethod
    def _run_name(job: dict, next_: bool) -> str:
        """The run a lane_prepare job is making: its result, the run it reuses, else the folder new_lane makes
        next (preparing) or made last (setup failed)."""
        params = job.get("params") or {}
        named = (job.get("result") or {}).get("run") or params.get("run")
        if named:
            return named
        short, kind = params.get("short") or (job.get("target") or "")[2:], params.get("kind") or "calib"
        d = config.TASKS / short
        try:
            if not next_ and lanes.latest_run(d, kind):
                return lanes.latest_run(d, kind)
            return lanes.next_run_dir(d, kind).name
        except Exception:
            return f"{kind}-1"

    def _prep_job(self) -> dict | None:
        try:
            live = _jobs().active(kind="lane_prepare")
        except Exception:
            live = []
        return live[0] if live else None

    def _apply(self, p: dict) -> None:
        self._probe = p
        now = time.time()
        info, look = p.get("info"), p.get("look") or {}
        scr = monitor.screen(look.get("pane")) if look else None
        red = p.get("red")
        prep = self._prep_job()
        run = None
        if info:
            meta = info["meta"]
            run = {"short": info["short"], "run": info["run"], "kind": info["kind"], "label": _label(meta),
                   "mode": _mode(meta), "container": p["cur"],
                   "pasted_at": (info.get("rec") or {}).get("pasted_at")}
        same = lambda m: bool(m and info and (m.get("short"), m.get("run")) == (info["short"], info["run"]))
        launched = self.pty_meta.get("at") if same(self.pty_meta) and self.pty_meta.get("kind") == "launch" else None
        failed = self.pty_failed["evidence"] if same(self.pty_failed) else None
        state, evidence, self._held = monitor.classify(
            container_running=bool(info), claude_alive=bool(look.get("alive")), scr=scr, red=red,
            has_credentials=bool(p.get("creds")), collect_phase=p.get("phase"), launched_at=launched,
            pty_alive=bool(self.pty and self.pty.alive and same(self.pty_meta)), pty_failed=failed,
            held_since=self._held, now=now)
        if state == "exited" and p.get("phase") == "failed":
            try:
                from .. import lane_watch
                w = lane_watch.STATE.get(f"{info['short']}/{info['run']}") or {}
                evidence = f"Collect failed: {w.get('message')}"
            except Exception:
                pass
        launchable = state == "idle"
        if state == "idle":
            state = "free"
        if prep and not p.get("alive"):
            params = prep.get("params") or {}
            short = params.get("short") or (prep.get("target") or "")[2:]
            meta = None
            try:
                meta = workspace.load(config.TASKS / short)
            except Exception:
                pass
            state, evidence = "preparing", ((prep.get("progress") or {}).get("step") or "Setting up the lane")
            run = {"short": short, "run": self._run_name(prep, next_=True), "kind": params.get("kind"),
                   "label": _label(meta) if meta else short, "mode": _mode(meta), "container": None,
                   "pasted_at": None}
            launchable = False
        elif state == "free" and not info and self._failed_job:
            j = self._failed_job
            params = j.get("params") or {}
            state, evidence = "setup_failed", (j.get("error") or {}).get("message") or "Setup failed"
            run = {"short": params.get("short"), "run": self._run_name(j, next_=False), "kind": params.get("kind"),
                   "label": params.get("label") or params.get("short"), "mode": "interactive", "container": None,
                   "pasted_at": None}
        rate = (scr or {}).get("rate") or (red.rate if red else None)
        question = monitor.question_of(red.last_claude_text) if state == "waiting" and red else None
        key = (state, (run or {}).get("short"), (run or {}).get("run"))
        if self._since[:2] != (state, key):
            since = now
            if state == "waiting" and red and red.last and red.last.get("ts"):
                since = _parse_iso(red.last["ts"]) or now
            elif state == "working" and red and red.last_human_ts:
                since = _parse_iso(red.last_human_ts) or now
            self._since = (state, key, since)
        locked = bool(run and run.get("run") and run.get("mode") == "async" and red and red.humans >= 1
                      and (run["short"], run["run"]) not in self.unlocked)
        new = {"state": state, "rate_limited": rate, "since": iso(self._since[2]), "run": run,
               "pty": {"alive": bool(self.pty and self.pty.alive), "clients": len(self.clients)},
               "question": question, "evidence": evidence, "facts": self._facts(p), "input_locked": locked,
               "queue": self._lane_queue(), "auto_prepare": self.auto_prepare,
               "foreign": {"container": p["foreign"][0]} if p.get("foreign") else None,
               "launchable": launchable, "session": bool(look.get("session")),
               "input": ({"visible": scr["input_visible"], "empty": not scr["input_text"]} if scr else None),
               "login_url": (scr or {}).get("login_url") if state == "needs_login" else None,
               "job": prep, "monitor": self._monitor(info)}
        old = self._state
        self._state = new
        if json.dumps(old, sort_keys=True, default=str) != json.dumps(new, sort_keys=True, default=str):
            if old.get("state") != state:
                self._log_event(new)
                if state == "exited" and info:
                    self._auto_collect(info)
            self._changed()
        elif self._pub_due and time.time() - self._pub_at >= 1.0:
            self._pub_due = False
            self._changed()                  # facts held back by the once-a-second limit
        self._maybe_auto_prepare(new)

    @staticmethod
    def _monitor(info: dict | None) -> dict | None:
        """The run monitor (runmon) for the lane's run, from its cache: never measured here, so a probe never
        waits on it. None until the run monitor's thread runs."""
        if not info:
            return None
        try:
            from . import runmon
            if not runmon.running():
                return None
            return runmon.public(runmon.get(info["task_dir"], info["run"], up=True))
        except Exception:
            return None

    @staticmethod
    def _log_event(st: dict) -> None:
        """Every state change, with its evidence, so the rules can be tuned (data/lane-events.jsonl)."""
        try:
            line = {"at": iso(), "state": st["state"], "short": (st.get("run") or {}).get("short"),
                    "run": (st.get("run") or {}).get("run"), "evidence": st.get("evidence")}
            with open(config.DATA / "lane-events.jsonl", "a") as f:
                f.write(json.dumps(line) + "\n")
        except OSError:
            pass

    # -- settings
    @property
    def auto_prepare(self) -> bool:
        """PUT /api/lane/auto-prepare for this server's life, else the app's settings (auto_prepare_lane)."""
        if self._auto_override is not None:
            return self._auto_override
        try:
            from .. import data
            return bool(data.settings().get("auto_prepare_lane", True))
        except Exception:
            return False

    async def set_auto_prepare(self, on: bool) -> dict:
        self._auto_override = bool(on)
        await self.refresh()
        return self.state()

    def _auto_collect(self, info: dict) -> None:
        """Claude just exited: collect now rather than after the watcher's quiet period (watcher on only)."""
        try:
            from .. import lane_watch
            on = bool(lane_watch._THREAD and lane_watch._THREAD.is_alive())
        except Exception:
            on = False
        if not on or not self.loop:
            return

        def go():
            self.loop.create_task(asyncio.to_thread(lane_watch.collect_run, info["task_dir"], info["run"],
                                                    None, "background"))
        self.loop.call_later(3.0, go)    # let Claude Code finish writing the transcript

    def _maybe_auto_prepare(self, st: dict) -> None:
        if not self.auto_prepare or st["state"] not in ("free", "collected") or not st["queue"] \
                or st.get("launchable") or self._prep_job() or not self.loop:
            return
        head = st["queue"][0]
        tag = f"{head['short']}:{head['kind']}:{head.get('since')}"
        if tag == self._last_auto:
            return
        self._last_auto = tag
        self.loop.create_task(self._auto_start(head))

    async def _auto_start(self, head: dict) -> None:
        try:
            await self.start_lane(head["short"], head["kind"], auto=True)
        except LaneError as e:
            print(f"lane auto-prepare skipped: {e.message}", flush=True)

    # -- PTY
    def _on_pty_exit(self, pty: Pty, code: int) -> None:
        meta = self.pty_meta or {}
        if pty is self.pty:
            if code and meta.get("kind") == "launch" and time.time() - pty.started_at < monitor.LAUNCH_GRACE_S:
                if not (self._probe.get("look") or {}).get("alive"):
                    self.pty_failed = {"short": meta.get("short"), "run": meta.get("run"),
                                       "evidence": pty.text_tail(8) or f"claude-dev exited with {code}"}
            self.pty = None
            for c in list(self.clients):
                c.text({"type": "idle"})
        self.poke()

    def _spawn(self, argv: list[str], *, cwd: Path | None, short: str, run: str, container: str | None,
               kind: str, cols: int = 100, rows: int = 32) -> Pty:
        """A new lane PTY (on the loop). Joins every connected socket to it."""
        if self.pty and self.pty.alive:
            old, self.pty = self.pty, None
            old.kill()
        if self.clients:
            cols, rows = self._size
        pty = Pty(argv, cwd=cwd, cols=cols, rows=rows, on_exit=self._on_pty_exit, tag={"short": short, "run": run})
        self.pty = pty
        self.pty_meta = {"short": short, "run": run, "container": container, "kind": kind, "at": time.time()}
        if kind == "launch":
            self.pty_failed = None
        for c in list(self.clients):
            c.text({"type": "run", "run": {"short": short, "run": run}})
            pty.attach(c, replay=False)
        self.poke()
        return pty

    async def _spawn_async(self, *a, **kw) -> None:
        self._spawn(*a, **kw)

    async def ensure_pty(self, cols: int = 100, rows: int = 32) -> Pty | None:
        """The lane PTY, attaching to the live tmux session when the server has none. Never starts Claude."""
        if self.pty and self.pty.alive:
            return self.pty
        p = self._probe or {}
        if not p.get("info"):
            await self.refresh()
            p = self._probe
        info = p.get("info")
        if not info:
            return None
        container = p["cur"]
        if not await asyncio.to_thread(monitor.session_alive, container):
            return None
        if self.pty and self.pty.alive:      # another caller won the race
            return self.pty
        ws = (p.get("paths") or {}).get("workspace")
        return self._spawn(attach_argv(container, ws), cwd=None, short=info["short"], run=info["run"],
                           container=container, kind="attach", cols=cols, rows=rows)

    # -- sockets
    async def lane_client(self, send_bytes, send_text, cols: int = 100, rows: int = 32) -> Client:
        self._ensure_loop()
        client = Client(send_bytes, send_text)
        client.cols, client.rows = cols, rows
        self._size = (cols, rows)
        self.clients.add(client)
        pty = await self.ensure_pty(cols, rows)
        run = {"short": pty.tag["short"], "run": pty.tag["run"]} if pty else None
        client.text({"type": "hello", "run": run, "locked": bool(self._state.get("input_locked"))})
        if pty:
            pty.attach(client)
            pty.resize(cols, rows)
        else:
            client.text({"type": "idle"})
        self._changed()
        return client

    def lane_input(self, client: Client, data: bytes) -> None:
        pty = self.pty
        if not pty or not pty.alive or not data:
            return
        if self._state.get("input_locked"):
            scr = monitor.screen((self._probe.get("look") or {}).get("pane"))
            if not (scr and scr["permission"]):
                data, dropped = strip_enters(data)
                if dropped:
                    client.text({"type": "swallowed", "what": "enter"})
        pty.write(data)

    def lane_message(self, client: Client, msg: dict) -> None:
        t = msg.get("type")
        if t == "resize":
            try:
                client.cols, client.rows = int(msg.get("cols") or 100), int(msg.get("rows") or 32)
            except (TypeError, ValueError):
                return
            self._size = (client.cols, client.rows)
            if self.pty:
                self.pty.resize(client.cols, client.rows)
        elif t == "hello" and "navigate" in (msg.get("caps") or []):
            self.nav_clients.add(client)

    def lane_closed(self, client: Client) -> None:
        self.clients.discard(client)
        self.nav_clients.discard(client)
        if self.pty:
            self.pty.detach(client)
        client.close()
        self._changed()

    async def shell_pty(self, cols: int = 100, rows: int = 32) -> Pty | None:
        """A bash in the lane workspace, for one socket. None when no known lane container runs."""
        p = self._probe or {}
        if not p.get("info"):
            await self.refresh()
            p = self._probe
        if not p.get("info"):
            return None
        ws = (p.get("paths") or {}).get("workspace")
        return Pty(shell_argv(p["cur"], ws), cols=cols, rows=rows, tag={"shell": True})

    # -- actions
    @staticmethod
    def _require_task(short: str) -> Path:
        if not short or "/" in str(short) or str(short).startswith("."):
            raise LaneError(404, "not_found", f"No task folder {short}.")
        d = config.TASKS / str(short)
        if not (d / "task.json").exists():
            raise LaneError(404, "not_found", f"No task folder {short}.")
        return d

    def _plan(self, p: dict, short: str, kind: str) -> tuple:
        """Sort the running lanes: (reuse, uncollected, idle). Blocking."""
        reuse, uncollected, idle = None, [], []
        for c in p.get("known", []):
            info = p["index"][c]
            if c == p.get("cur"):
                red, phase = p.get("red"), p.get("phase")
            else:
                paths = monitor.lane_paths(info["dir"])
                files = monitor.transcripts(paths["projects"])
                red = monitor.Reducer()
                if files:
                    red.feed(monitor.Tail(files[-1]).read())
                phase = self._collect_phase(info, files[-1] if files else None)
            if red and red.humans and phase != "done":
                uncollected.append(info)
            elif not (red and red.humans) and info["short"] == short and info["kind"] == kind and not reuse:
                reuse = (c, info)
            else:
                idle.append(c)
        return reuse, uncollected, idle

    async def start_lane(self, short: str, kind: str, auto: bool = False) -> dict:
        """Set up a new lane for (short, kind) and launch Claude in the server's PTY. Returns the job."""
        self._ensure_loop()
        if kind not in KINDS:
            raise LaneError(400, "invalid", "kind must be calib or validate.")
        d = self._require_task(short)
        if self._prep_job():
            raise LaneError(409, "lane_state", "The lane is already being set up.", {"state": "preparing"})
        await self.refresh()
        p = self._probe
        if p.get("alive"):
            info = p["index"][p["alive"][0]]
            raise LaneError(409, "lane_busy", f"Claude is running in {_label(info['meta'])} · {info['run']}. "
                            "End that run first, or start a second lane beside it from the terminal with "
                            "`johto lane`.", {"short": info["short"], "run": info["run"]})
        if p.get("foreign"):
            raise LaneError(409, "lane_busy", f"A lane container this app doesn't know ({p['foreign'][0]}) holds "
                            "the lane ports. Stop it in Docker first.", {"foreign": {"container": p["foreign"][0]}})
        reuse, uncollected, idle = await asyncio.to_thread(self._plan, p, short, kind)
        try:
            if not reuse:
                # Refuse on the prompt checks before anything is collected or stopped.
                await asyncio.to_thread(self.gate, d, kind)
            elif kind == "validate":
                # A prepared lane passed the gate at setup; the prompt may have changed since, and it reaches
                # Claude only in his own words.
                from ... import actions
                await asyncio.to_thread(actions._guard_prompt_overlap, d)
        except Exception as e:
            if type(e).__name__ != "ActionError":
                raise
            raise LaneError(e.status, e.code, e.message, e.details) from None
        word = "calibration" if kind == "calib" else "validation"
        steps = (["Collecting the last run"] if uncollected else []) + (["Stopping the idle lane"] if idle else []) \
            + ([] if reuse else [f"Setting up {word}"]) + ["Starting Claude"]
        meta = workspace.load(d)
        loop = self.loop

        def fn(ctx):
            from .. import lane_watch
            lines: list[str] = []

            def log(line):
                lines.append(str(line))
                ctx.log(line)
            for info in uncollected:
                ctx.step("Collecting the last run", steps)
                log(f"collecting {info['short']} {info['run']}")
                res = _collect_and_wait(info["task_dir"], info["run"])
                if res.get("phase") == "failed":
                    raise _action_error("collect_failed", f"Collecting {info['short']} {info['run']} failed: "
                                        f"{res.get('message')}")
            if idle:
                ctx.step("Stopping the idle lane", steps)
                for c in idle:
                    # Checked again now: the idle list is older than this job (it waited for the lane pool and
                    # maybe a collect), and he may have resumed that lane in Ghostty meanwhile.
                    if monitor.claude_alive(c, unknown=True):
                        raise _action_error("lane_busy", f"Claude is running again in {c} (or Docker didn't answer), "
                                            "so it wasn't stopped. End that run first.", {"foreign": None})
                    log(f"docker stop {c}")
                    if not monitor.stop(c):
                        raise _action_error("lane_busy", f"Couldn't stop the idle lane {c}.", {"foreign": None})
            if reuse:
                container, info = reuse
                run_dir = info["dir"]
                log(f"using the lane already set up in {run_dir.name}")
            else:
                ctx.step(f"Setting up {word}", steps)
                try:
                    run_dir = Path(self.setup(d, kind, log))
                except (RuntimeError, SystemExit, OSError, subprocess.SubprocessError) as e:
                    msg = str(e) or e.__class__.__name__
                    errors = [ln for ln in lines if "ERROR" in ln][-6:]
                    raise _action_error("setup_failed", msg.splitlines()[0][:300],
                                        {"errors": errors, "log": msg[-3000:]}) from e
                container = (lanes.manifest(run_dir).get("lane") or {}).get("container")
            result = {"short": short, "run": run_dir.name, "dir": str(run_dir)}
            ctx.set_result(result)
            ctx.step("Starting Claude", steps)
            if not _docker_free():
                fut = asyncio.run_coroutine_threadsafe(
                    self._spawn_async(launch_argv(run_dir), cwd=run_dir / "model_a", short=short,
                                      run=run_dir.name, container=container, kind="launch"), loop)
                fut.result(15)
            else:
                log("fake mode: no Docker, so Claude isn't started")
            try:
                _update_meta(d, lambda m: m.pop("ready", None))
            except Exception:
                traceback.print_exc()
            return result

        def done(job):
            self._failed_job = job if job.get("state") == "failed" else None
            self.poke()

        self._failed_job = None
        try:
            job = _jobs().submit("lane_prepare", f"t:{short}", fn, pool="lane", priority=0, cancellable=False,
                                 params={"short": short, "kind": kind, "auto": auto, "label": _label(meta),
                                         "run": reuse[1]["run"] if reuse else lanes.next_run_dir(d, kind).name},
                                 steps=steps, step=steps[0], on_done=done)
        except Exception as e:
            raise LaneError(500, "internal", f"Couldn't start the job: {e}")
        self.poke()
        return job

    async def launch(self) -> None:
        """Start Claude in the current lane when it is set up but Claude never ran there (no prompt yet)."""
        st = await self.refresh()
        p = self._probe
        if not st.get("launchable") or not p.get("info"):
            raise LaneError(409, "lane_state", "There is no idle lane to start Claude in.", {"state": st["state"]})
        info = p["info"]
        self._spawn(launch_argv(info["dir"]), cwd=info["dir"] / "model_a", short=info["short"], run=info["run"],
                    container=p["cur"], kind="launch")

    @staticmethod
    async def _write_slowly(pty: Pty, *parts: bytes) -> None:
        """Type, then press Enter a moment later, so Claude's input sees keys and not a paste."""
        for i, part in enumerate(parts):
            if i:
                await asyncio.sleep(0.15)
            pty.write(part)

    def _screen(self) -> tuple[dict | None, bool]:
        scr = monitor.screen((self._probe.get("look") or {}).get("pane"))
        return scr, bool(scr and scr["input_visible"] and not scr["input_text"])

    async def type(self, what: str) -> None:
        if what not in ("prompt", "login", "exit"):
            raise LaneError(400, "invalid", "what must be prompt, login or exit.")
        st = await self.refresh()
        p = self._probe
        state = st["state"]
        scr, box_empty = self._screen()
        red = p.get("red")
        if what == "prompt":
            if state != "ready_for_prompt" or (red and red.humans):
                raise LaneError(409, "lane_state", "The prompt goes in only before the first message, once Claude "
                                "shows its input box.", {"state": state})
            if not box_empty:
                raise LaneError(409, "lane_state", "The input box already has text in it. Clear it in the terminal "
                                "first.", {"state": state})
            d = p["info"]["task_dir"]
            prompt = workspace.parse_submission(d)["sections"].get("Opening Prompt", "").strip()
            if not prompt:
                raise LaneError(422, "missing_fields", "Write the opening prompt first.", {"fields": ["opening_prompt"]})
            ready = workspace.load(d).get("ready") or {}
            if ready.get("prompt_sha") and ready["prompt_sha"] != rev(prompt):
                raise LaneError(409, "lane_state", "The opening prompt changed after you pressed Done. Press Done "
                                "again to paste this version.", {"state": state})
            pty = await self.ensure_pty()
            if not pty:
                raise LaneError(409, "lane_state", "The lane has no terminal to paste into.", {"state": state})
            body = prompt.replace("\r\n", "\n").replace("\n", "\r").encode()
            pty.write(PASTE_START + body + PASTE_END)
            run, sha, at = p["info"]["run"], rev(prompt), iso()

            def mark(m):
                for r in m.get("runs", []):
                    if r.get("dir") == f"runs/{run}":
                        r["prompt_sha"], r["pasted_at"] = sha, at
            try:
                await asyncio.to_thread(_update_meta, d, mark)
            except Exception:
                traceback.print_exc()
        elif what == "login":
            if state != "needs_login":
                raise LaneError(409, "lane_state", "Claude isn't asking you to log in.", {"state": state})
            if scr and scr["login_screen"]:
                raise LaneError(409, "lane_state", "The login screen is already open in the terminal.", {"state": state})
            if not box_empty:
                raise LaneError(409, "lane_state", "Claude's input box isn't showing, or has text in it.",
                                {"state": state})
            pty = await self.ensure_pty()
            if not pty:
                raise LaneError(409, "lane_state", "The lane has no terminal.", {"state": state})
            await self._write_slowly(pty, b"/login", b"\r")
        else:
            await self._exit(st, box_empty)
        self.poke()

    async def _exit(self, st: dict, box_empty: bool) -> None:
        state = st["state"]
        if state == "working":
            raise LaneError(409, "lane_state", "Claude is working. Wait for it to finish its turn, or press Esc in "
                            "the terminal to stop it.", {"state": state})
        if state not in ("waiting", "ready_for_prompt"):
            raise LaneError(409, "lane_state", "Claude isn't at its input box, so /exit can't go in now.",
                            {"state": state})
        if not box_empty:
            raise LaneError(409, "lane_state", "The input box has text in it. Clear it in the terminal first, so "
                            "/exit isn't added to your reply.", {"state": state})
        pty = await self.ensure_pty()
        if not pty:
            raise LaneError(409, "lane_state", "The lane has no terminal.", {"state": state})
        await self._write_slowly(pty, b"/exit", b"\r")

    async def end(self) -> None:
        st = await self.refresh()
        _, box_empty = self._screen()
        await self._exit(st, box_empty)
        self.poke()

    async def collect(self) -> dict:
        """Collect the current run now (lane_watch normally does it 20 s after Claude exits)."""
        st = await self.refresh()
        p = self._probe
        info = p.get("info")
        if not info or st["state"] != "exited":
            raise LaneError(409, "lane_state", "There is no ended run to collect.", {"state": st["state"]})
        live = [j for j in _jobs().active(kind="lane_collect", target=f"t:{info['short']}")
                if (j.get("params") or {}).get("run") == info["run"]]
        if live:
            return live[0]

        def fn(ctx):
            ctx.step("Collecting", ["Collecting"])
            res = _collect_and_wait(info["task_dir"], info["run"])
            if res.get("phase") == "failed":
                raise _action_error("collect_failed", res.get("message") or "collect failed")
            return {"short": info["short"], "run": info["run"]}

        job = _jobs().submit("lane_collect", f"t:{info['short']}", fn, pool="lane", priority=0, cancellable=False,
                             params={"short": info["short"], "run": info["run"]}, steps=["Collecting"],
                             on_done=lambda j: self.poke())
        self.poke()
        return job

    async def unlock(self) -> None:
        st = await self.refresh()
        run = st.get("run")
        if not run or not run.get("run"):
            raise LaneError(409, "lane_state", "No run in the lane.", {"state": st["state"]})
        self.unlocked.add((run["short"], run["run"]))
        d = config.TASKS / run["short"]
        try:
            (d / "app").mkdir(exist_ok=True)
            with open(d / "app" / "authorship.jsonl", "a") as f:
                f.write(json.dumps({"at": iso(), "field": "lane", "event": "unlock_input", "detail": run["run"]}) + "\n")
        except OSError:
            traceback.print_exc()
        await self.refresh()
        for c in list(self.clients):
            c.text({"type": "locked", "locked": False})

    async def ghostty(self) -> None:
        st = await self.refresh()
        p = self._probe
        info = p.get("info")
        if not info:
            raise LaneError(409, "lane_state", "No lane is running.", {"state": st["state"]})
        look, red = p.get("look") or {}, p.get("red")
        if not look.get("session") and (look.get("alive") or (red and red.humans)):
            raise LaneError(409, "lane_state", "This lane's session has ended.", {"state": st["state"]})
        argv = ghostty_argv(info["dir"])
        try:
            r = await asyncio.to_thread(self.open_ghostty, argv)
            rc, err = getattr(r, "returncode", 0), getattr(r, "stderr", "") or ""
        except (OSError, subprocess.TimeoutExpired) as e:
            rc, err = 1, str(e)
        if rc != 0:
            raise LaneError(502, "ghostty", "Ghostty didn't open. Copy the attach command instead.",
                            {"command": f"cd {info['dir'] / 'model_a'} && ./claude-dev {' '.join(LAUNCH_ARGS)}",
                             "stderr": err[-400:]})


svc = LaneService()
