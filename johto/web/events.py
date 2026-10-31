"""SSE hub (build-plan 5.3) and the engine that keeps real-mode clients current.

The hub keeps the last 500 events. A client that reconnects with Last-Event-ID inside that window gets
the events it missed; any other connect starts with a `state` event. Event ids start from the launch
time in milliseconds, so ids from an earlier server never look like ids in this one's buffer.

Events carry a mode. Real-mode events reach only real-mode clients, so a tab showing the demo
afternoon is never overwritten by real data (and the other way round).

The engine recomputes the real snapshot (tasks, lane, jobs, queue) when files under tasks/ or data/
change, when another module calls poke(), and on a timer (every 2 s while a run or job is active,
every 10 s otherwise). It publishes only what changed: `queue`, `lane` (state changes at once, facts at
most once a second), `task` with the parts that changed, `task.removed`, `job` for review packs and
the lane watcher, `leads` when scout data, slots or dossiers change, and `toast` when a run is
collected or its review pack is ready.
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import secrets
import threading
import time
import traceback
from pathlib import Path

from .. import config

BUFFER = 500
PING_S = 15.0
_CLOSE = object()


def _for(mode_ev: str | None, mode: str) -> bool:
    return mode_ev is None or mode_ev == mode


class _Sub:
    def __init__(self, loop: asyncio.AbstractEventLoop, mode: str):
        self.loop, self.mode = loop, mode
        self.q: asyncio.Queue = asyncio.Queue(maxsize=2000)

    def offer(self, ev) -> None:
        if ev is not _CLOSE and not _for(ev[3], self.mode):
            return
        try:
            self.loop.call_soon_threadsafe(self._put, ev)
        except RuntimeError:  # the loop is closed
            pass

    def _put(self, ev) -> None:
        if self.q.full():
            try:
                self.q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self.q.put_nowait(ev)


class Hub:
    def __init__(self, size: int = BUFFER):
        self._lock = threading.Lock()
        self._buf: collections.deque = collections.deque(maxlen=size)
        self._id = int(time.time() * 1000)
        self._subs: set[_Sub] = set()

    @property
    def last_id(self) -> int:
        return self._id

    def publish(self, name: str, data: dict, mode: str | None = "real") -> int:
        """Thread-safe. mode None reaches every client."""
        with self._lock:
            self._id += 1
            ev = (self._id, name, data, mode)
            self._buf.append(ev)
            subs = list(self._subs)
        for s in subs:
            s.offer(ev)
        return ev[0]

    def replay(self, last_id: int | None, mode: str) -> list | None:
        """The events after last_id for this mode, or None when last_id is outside the buffer (a gap)."""
        if last_id is None:
            return None
        with self._lock:
            if last_id == self._id:
                return []
            if not self._buf or last_id < self._buf[0][0] - 1 or last_id > self._id:
                return None
            return [e for e in self._buf if e[0] > last_id and _for(e[3], mode)]

    def subscribe(self, mode: str) -> _Sub:
        s = _Sub(asyncio.get_running_loop(), mode)
        with self._lock:
            self._subs.add(s)
        return s

    def unsubscribe(self, s: _Sub) -> None:
        with self._lock:
            self._subs.discard(s)

    def close_all(self) -> None:
        with self._lock:
            subs = list(self._subs)
        for s in subs:
            s.offer(_CLOSE)

    def recent(self, n: int = 50) -> list:
        with self._lock:
            return list(self._buf)[-n:]

    @property
    def clients(self) -> int:
        return len(self._subs)


def fmt(ev_id: int | None, name: str, data) -> str:
    head = f"id: {ev_id}\n" if ev_id is not None else ""
    return f"{head}event: {name}\ndata: {json.dumps(data, separators=(',', ':'), default=str)}\n\n"


async def stream(mode: str, last_event_id: str | None, initial_state, disconnected=None):
    """The SSE body for one client. initial_state: async () -> GET /api/state body."""
    sub = hub.subscribe(mode)
    try:
        try:
            lid = int(last_event_id) if last_event_id not in (None, "") else None
        except ValueError:
            lid = None
        at = hub.last_id
        missed = hub.replay(lid, mode)
        if missed is None:
            yield fmt(at, "state", await initial_state())
        else:
            for ev in missed:
                yield fmt(ev[0], ev[1], ev[2])
        while True:
            try:
                ev = await asyncio.wait_for(sub.q.get(), timeout=PING_S)
            except asyncio.TimeoutError:
                if disconnected is not None and await disconnected():
                    return
                yield fmt(None, "ping", {})
                continue
            if ev is _CLOSE:
                return
            yield fmt(ev[0], ev[1], ev[2])
    finally:
        hub.unsubscribe(sub)


hub = Hub()


def publish(name: str, data: dict, mode: str | None = "real") -> int:
    return hub.publish(name, data, mode)


def toast(text: str, level: str = "info", link: dict | None = None, undo: dict | None = None,
          mode: str | None = "real") -> dict:
    t = {"id": secrets.token_hex(4), "level": level, "text": text, "undo": undo, "link": link}
    hub.publish("toast", t, mode)
    return t


# --- the engine ------------------------------------------------------------------------------------------

def _h(v) -> str:
    return hashlib.sha1(json.dumps(v, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _parts(state: dict) -> dict:
    """Signatures of the parts of a task that `task` events name in `changed`."""
    meta = state["meta"]
    return {
        "sections": _h(state["texts"]),
        "ratings": _h({k: state["ratings"].get(k) for k in ("one_shot", "task_success", "steering", "passes")}),
        "runs": _h([(r["name"], r.get("state"), r.get("collected_at"), r.get("live"),
                     (r.get("facts") or {}).get("turns"), (r.get("facts") or {}).get("active_min"),
                     r.get("packs"), (r.get("auto") or {}).get("phase")) for r in state["runs"]]),
        "notes": _h([(r["name"], r.get("notes")) for r in state["runs"]]),
        "studio": _h([meta.get("studio"), meta.get("pushed"), meta.get("gists"), meta.get("submitted_at"),
                      meta.get("unclaimed_at"), meta.get("discard")]),
        "qc": _h(meta.get("qc")),
        "decisions": _h([meta.get("decisions"), meta.get("ready"), meta.get("skipped_calibration"),
                         meta.get("submission_run"), meta.get("details_ready")]),
    }


LEADS_PARTS = ("scout", "slots.json", "studio_tasks.json", "dossiers")
IGNORE_PARTS = {"model_a", ".prework-clone", "__pycache__", ".git", "node_modules"}


def _relevant(path: str) -> str | None:
    """'tasks', 'leads' or None for a changed path."""
    p = Path(path)
    try:
        rel = p.relative_to(config.TASKS)
        parts = rel.parts
        if not parts or set(parts) & IGNORE_PARTS or p.name in (".lock", ".DS_Store") or p.name.endswith(".tmp"):
            return None
        return "tasks"
    except ValueError:
        pass
    try:
        rel = p.relative_to(config.DATA)
    except ValueError:
        return None
    if rel.parts and rel.parts[0] in LEADS_PARTS and not p.name.endswith(".tmp"):
        return "leads"
    return None


class Engine:
    def __init__(self, hub: Hub):
        self.hub = hub
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._leads_dirty = False
        self._threads: list[threading.Thread] = []
        self._last: dict = {}
        self._lane_pub_at = 0.0
        self._jobs: dict[str, tuple] = {}
        self.computes = 0
        self.watching = False

    # public
    def start(self, watch: bool = True) -> None:
        if self._threads and any(t.is_alive() for t in self._threads):
            return
        self._stop.clear()
        t = threading.Thread(target=self._loop, daemon=True, name="events-engine")
        t.start()
        self._threads = [t]
        if watch:
            w = threading.Thread(target=self._watch, daemon=True, name="events-watch")
            w.start()
            self._threads.append(w)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout)
        self._threads = []

    def poke(self, reason: str = "", leads: bool = False) -> None:
        if leads:
            self._leads_dirty = True
        self._wake.set()

    def recompute(self) -> dict | None:
        """One pass: snapshot, diff against the last pass, publish. Returns the snapshot."""
        from . import data
        snap = data.real_snapshot()
        self.computes += 1
        self._diff(snap)
        if self._leads_dirty:
            self._leads_dirty = False
            self.hub.publish("leads", {"changed": True})
        return snap

    # internals
    def _loop(self) -> None:
        last = 0.0
        busy = True
        while not self._stop.is_set():
            self._wake.wait(2.0 if busy else 10.0)
            self._wake.clear()
            if self._stop.is_set():
                return
            gap = time.time() - last
            if gap < 0.25:  # at most 4 passes a second
                time.sleep(0.25 - gap)
            last = time.time()
            try:
                snap = self.recompute()
                busy = bool(snap) and ((snap["lane"] or {}).get("state") not in (None, "free", "collected")
                                       or any(j.get("state") in ("queued", "running") for j in snap["jobs"]))
            except Exception:
                traceback.print_exc()

    def _watch(self) -> None:
        paths = [p for p in (config.TASKS, config.DATA) if p.exists()]
        try:
            import watchfiles
        except ImportError:
            watchfiles = None
        if watchfiles is not None and paths:
            self.watching = True
            try:
                for changes in watchfiles.watch(*paths, stop_event=self._stop, debounce=200, step=50,
                                                recursive=True, raise_interrupt=False,
                                                watch_filter=lambda ch, p: _relevant(p) is not None):
                    kinds = {_relevant(p) for _, p in changes}
                    self.poke("files", leads="leads" in kinds)
            except Exception:
                traceback.print_exc()
            finally:
                self.watching = False
            return
        # Fallback: poll the files the views read.
        self.watching = True
        seen: dict[str, float] = {}
        while not self._stop.wait(1.5):
            cur = {}
            for pat in ("*/task.json", "*/submission.md", "*/app/*", "*/app/research/*", "*/runs/*/summary.md",
                        "*/runs/*/review-pack-*.xml", "*/runs/*/prework_output/*"):
                for p in config.TASKS.glob(pat):
                    try:
                        cur[str(p)] = p.stat().st_mtime
                    except OSError:
                        pass
            for pat in ("scout/*.json", "slots.json", "dossiers/*.json"):
                for p in config.DATA.glob(pat):
                    try:
                        cur[str(p)] = p.stat().st_mtime
                    except OSError:
                        pass
            if seen and cur != seen:
                changed = {k for k in set(cur) | set(seen) if cur.get(k) != seen.get(k)}
                self.poke("files", leads=any(_relevant(k) == "leads" for k in changed))
            seen = cur
        self.watching = False

    def _diff(self, snap: dict) -> None:
        last = self._last
        # queue
        q = {"queue": snap["queue"], "running": snap["running"]}
        hq = _h(q)
        if hq != last.get("queue"):
            last["queue"] = hq
            self.hub.publish("queue", q)
        # lane: a state change at once, facts at most once a second
        lane = snap["lane"] or {}
        head = _h({k: v for k, v in lane.items() if k != "facts"})
        full = _h(lane)
        if head != last.get("lane_head") or (full != last.get("lane_full") and time.time() - self._lane_pub_at >= 1.0):
            last["lane_head"], last["lane_full"] = head, full
            self._lane_pub_at = time.time()
            if not snap.get("lane_service"):  # the lane monitor publishes its own lane events
                self.hub.publish("lane", lane)
        # tasks
        seen = set()
        tasks = last.setdefault("tasks", {})
        for st, summ in zip(snap["states"], snap["summaries"]):
            short = st["short"]
            seen.add(short)
            parts = _parts(st)
            hs = _h(summ)
            prev = tasks.get(short)
            if prev is None:
                tasks[short] = (hs, parts)
                if last.get("primed"):
                    self.hub.publish("task", {"summary": summ, "changed": sorted(parts)})
                continue
            changed = sorted(k for k in parts if parts[k] != prev[1].get(k))
            if hs != prev[0] or changed:
                tasks[short] = (hs, parts)
                self.hub.publish("task", {"summary": summ, "changed": changed})
        for short in [s for s in tasks if s not in seen]:
            tasks.pop(short)
            self.hub.publish("task.removed", {"short": short})
        # jobs the jobs runner doesn't publish itself (review packs, the lane watcher)
        labels = {s["short"]: s["label"] for s in snap["summaries"]}
        for j in snap["jobs"]:
            # Only the watcher's ("watch-…") and the pack button's ("pack-…") jobs: the jobs runner's own
            # lane_collect (a manual collect) already publishes its job events and its "Collected" toast.
            if j.get("kind") not in ("pack", "lane_collect") or not str(j.get("id") or "").startswith(("watch-", "pack-")):
                continue
            sig = (j.get("state"), (j.get("progress") or {}).get("step"))
            prev = self._jobs.get(j["id"])
            if sig == prev:
                continue
            self._jobs[j["id"]] = sig
            if last.get("primed") or prev is not None:
                self.hub.publish("job", j)
                self._job_toast(j, prev, labels)
        last["primed"] = True

    def _job_toast(self, j: dict, prev, labels: dict) -> None:
        from .domain.queue import run_word
        label = labels.get(j.get("short"), j.get("short") or "")
        rw = run_word(j.get("run"))
        step = (j.get("progress") or {}).get("step") or ""
        link = {"label": "Open the task", "href": f"/task/{j.get('short')}"} if j.get("short") else None
        if j["kind"] == "lane_collect":
            if j["state"] == "running" and "pack" in step.lower() and (prev is None or "pack" not in str(prev[1]).lower()):
                self.hub.publish("toast", {"id": secrets.token_hex(4), "level": "ok", "undo": None, "link": link,
                                           "text": f"Collected {rw} · {label} · review pack building"})
            elif j["state"] == "failed":
                self.hub.publish("toast", {"id": secrets.token_hex(4), "level": "error", "undo": None, "link": link,
                                           "text": f"Collecting {rw} failed: {(j.get('error') or {}).get('message') or step}"})
        elif j["kind"] == "pack" and prev is not None and prev[0] == "running":
            if j["state"] == "succeeded":
                self.hub.publish("toast", {"id": secrets.token_hex(4), "level": "ok", "undo": None, "link": link,
                                           "text": f"Review pack ready · {rw} · {label}"})
            elif j["state"] == "failed":
                self.hub.publish("toast", {"id": secrets.token_hex(4), "level": "error", "undo": None, "link": link,
                                           "text": f"Review pack failed · {rw} · {label}"
                                                   + (f": {(j.get('error') or {}).get('message')}" if (j.get("error") or {}).get("message") else "")})


engine = Engine(hub)


def poke(reason: str = "", leads: bool = False) -> None:
    """Ask for a recompute soon (after a write, a job step, a lane change). Cheap; coalesced."""
    engine.poke(reason, leads)


def from_jobs(name: str, data: dict) -> None:
    """Publisher hook for johto.web.jobs: forward its events to real-mode clients and recompute after them."""
    if name == "task":
        poke("task")          # the engine publishes task events in one shape
        return
    hub.publish(name, data)
    if name in ("job", "studio"):
        poke(name)


def start(watch: bool = True) -> None:
    engine.start(watch=watch)


def stop() -> None:
    engine.stop()
    hub.close_all()
