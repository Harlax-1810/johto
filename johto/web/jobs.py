"""Background jobs for the web app, the single Studio worker, and the undo registry.

Jobs run in threads (the johto actions they wrap are blocking). Each job has a kind, a target
("t:<short>" or "l:<lead>"), a pool that caps how many run at once, and progress steps. Every state
or step change is published as an SSE `job` event through the hook set by set_publisher().

Studio calls never run in job threads directly. A job's actions see a StudioProxy (through
johto.actions.using_studio), which queues each call on the one Studio worker thread: strictly one
call at a time in the one Studio tab, priority 0 (Austin's write) before 1 (his read or a poll he
is watching) before 2 (background), FIFO within a priority. A long job (a QC poll) holds the tab
only for the length of one call, so a claim never waits behind it.

If Studio's tab has no session, the worker enters "signed_out", holds the queued calls, retries
me() every 30 s (or at once on wake()), and runs them when the session is back. A call held
longer than HOLD_MAX_S fails with the signed-out message.
"""
from __future__ import annotations

import collections
import contextvars
import inspect
import itertools
import queue
import secrets
import threading
import time
import traceback
from concurrent.futures import Future
from datetime import datetime

from .. import actions, studio

POOLS = {"studio": 4, "ai": 1, "github": 2, "local": 3, "lane": 1, "research": 1}   # research: task understanding
KEEP = 300                      # finished jobs kept in memory
HOLD_MAX_S = 600
RETRY_S = 30


class Cancelled(Exception):
    pass


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _iso(ts: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts)) if ts else None


# --- events -------------------------------------------------------------------------------------------

_publishers: list = []
EVENTS: collections.deque = collections.deque(maxlen=500)   # recent (name, data), for tests and debugging


def set_publisher(fn) -> None:
    """fn(name: str, data: dict): called for every job, toast, task and studio event."""
    _publishers.clear()
    if fn:
        _publishers.append(fn)


def publish(name: str, data: dict) -> None:
    EVENTS.append((name, data))
    for fn in list(_publishers):
        try:
            fn(name, data)
        except Exception:
            traceback.print_exc()


def toast(text: str, level: str = "info", undo: dict | None = None, link: dict | None = None) -> dict:
    t = {"id": secrets.token_hex(4), "level": level, "text": text, "undo": undo, "link": link}
    publish("toast", t)
    return t


# --- the Studio worker --------------------------------------------------------------------------------

class StudioWorker:
    def __init__(self):
        self._q: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "ok"
        self._last_ok: float | None = None
        self._current: str | None = None
        self._lock = threading.Lock()

    # public
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="studio-worker")
            self._thread.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        self._wake.set()
        self._q.put((99, next(self._seq), None))
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)

    def wake(self) -> None:
        """Retry at once (after Austin opened or signed in to the Studio tab)."""
        self._wake.set()

    def state(self) -> dict:
        return {"state": self._state, "pending": self._q.qsize() + (1 if self._current else 0),
                "last_ok_at": _iso(self._last_ok), "current": self._current}

    # "busy" means calls are waiting behind the one in flight; a single call in flight is still "ok".
    # Events go out only when the state word changes, so a 30-call save is not 60 events.

    def call(self, fn_name: str, *args, priority: int = 1, **kw):
        """Run johto.studio.<fn_name>(*args, **kw) on the worker and wait for it. Blocking."""
        if threading.current_thread() is self._thread:
            return getattr(actions.base_studio(), fn_name)(*args, **kw)
        self.start()
        fut: Future = Future()
        self._q.put((priority, next(self._seq), (fn_name, args, kw, fut, time.time())))
        if self._current and self._state == "ok":
            self._set("busy")
        return fut.result()

    # internals
    def _set(self, st: str) -> None:
        if st != self._state:
            self._state = st
            self._publish_state()

    def _publish_state(self) -> None:
        s = self.state()
        publish("studio", {"state": s["state"], "pending": s["pending"]})

    def _loop(self) -> None:
        while not self._stop.is_set():
            prio, seq, item = self._q.get()
            if item is None:
                continue
            fn_name, args, kw, fut, queued_at = item
            if fut.cancelled():
                continue
            self._current = fn_name
            try:
                res = getattr(actions.base_studio(), fn_name)(*args, **kw)
            except BaseException as e:  # noqa: BLE001  (handed to the caller)
                if actions.signed_out(e) and time.time() - queued_at < HOLD_MAX_S and not self._stop.is_set():
                    self._q.put((prio, seq, item))   # same place in line
                    self._current = None
                    self._hold()
                    continue
                self._current = None
                fut.set_exception(e)
                self._set("error" if isinstance(e, studio.StudioError) and e.status >= 500 else "ok")
                continue
            self._current = None
            self._last_ok = time.time()
            fut.set_result(res)
            self._set("ok" if self._q.qsize() <= 1 else "busy")

    def _hold(self) -> None:
        """Signed out: wait for a session, checking with me() every RETRY_S (or on wake())."""
        self._set("signed_out")
        while not self._stop.is_set():
            self._wake.wait(RETRY_S)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                actions.base_studio().me()
            except BaseException as e:  # noqa: BLE001
                if actions.signed_out(e):
                    self._fail_stale()
                    continue
            self._set("ok")
            return

    def _fail_stale(self) -> None:
        keep = []
        while True:
            try:
                keep.append(self._q.get_nowait())
            except queue.Empty:
                break
        for prio, seq, item in keep:
            if item and time.time() - item[4] >= HOLD_MAX_S:
                item[3].set_exception(studio.StudioError(0, "Studio tab has no Clerk session (logged out?)", item[0]))
            else:
                self._q.put((prio, seq, item))


worker = StudioWorker()


class StudioProxy:
    """What johto.actions.S() returns inside a job: Studio calls queued on the worker."""

    PURE = {"slot_of", "status_name", "task_url", "StudioError"}

    def __init__(self, priority: int = 1):
        self.priority = priority

    def __getattr__(self, name):
        base = actions.base_studio()
        if name in self.PURE or name.isupper() or name.startswith("_"):
            return getattr(base, name)
        if not callable(getattr(base, name, None)):
            raise AttributeError(name)
        return lambda *a, **kw: worker.call(name, *a, priority=self.priority, **kw)


# --- jobs --------------------------------------------------------------------------------------------

_JOBS: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
_PRIV: dict[str, dict] = {}
_SEMS = {k: threading.BoundedSemaphore(v) for k, v in POOLS.items()}
_JLOCK = threading.RLock()


def _view(job: dict) -> dict:
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in job.items()}


def _changed(job: dict) -> None:
    publish("job", _view(job))


class JobCtx:
    def __init__(self, job: dict, priority: int):
        self.job_id = job["id"]
        self._job = job
        self._cancel: threading.Event = _PRIV[job["id"]]["cancel"]
        self.studio = StudioProxy(priority)
        self.priority = priority

    @property
    def target(self) -> str:
        return self._job["target"]

    @property
    def params(self) -> dict:
        return self._job.get("params") or {}

    def step(self, name: str, steps: list[str] | None = None) -> None:
        p = self._job["progress"]
        p["step"] = name
        if steps is not None:
            p["steps"] = list(steps)
        elif name not in p["steps"]:
            p["steps"].append(name)
        _changed(self._job)

    def log(self, line: str) -> None:
        tail = self._job["progress"]["log_tail"]
        tail.append(str(line)[:500])
        del tail[:-40]
        _changed(self._job)

    def add_cost(self, usd: float) -> None:
        self._job["cost_usd"] = round((self._job.get("cost_usd") or 0) + usd, 4)

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check_cancel(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def sleep(self, seconds: float) -> None:
        """Sleep, waking early (and raising Cancelled) on cancel."""
        if self._cancel.wait(seconds):
            raise Cancelled()

    def set_result(self, result: dict) -> None:
        """A partial result visible while the job still runs."""
        self._job["result"] = result
        _changed(self._job)


def submit(kind: str, target: str, fn, *, pool: str = "local", priority: int = 1,
           estimate_usd: float | None = None, cancellable: bool = True, run_at: float | datetime | None = None,
           parent: str | None = None, params: dict | None = None, steps: list[str] | None = None,
           step: str | None = None, on_done=None) -> dict:
    """Start fn(ctx) in the background. fn may be sync or async; its return value is the job result.

    run_at (epoch seconds or datetime) delays the start; the job stays "queued" and cancellable until
    then; a queued job can always be cancelled, a running one only when `cancellable`. `step` is the
    step shown while it waits. on_done(job) runs after the job ends, whatever the outcome.
    """
    if pool not in POOLS:
        raise ValueError(f"unknown pool {pool}")
    if isinstance(run_at, datetime):
        run_at = run_at.timestamp()
    jid = "job_" + secrets.token_hex(6)
    job = {"id": jid, "kind": kind, "target": target, "pool": pool, "priority": priority, "state": "queued",
           "progress": {"step": step, "steps": list(steps or []), "log_tail": []},
           "created_at": now_iso(), "run_at": _iso(run_at), "started_at": None, "ended_at": None,
           "cost_usd": None, "estimate_usd": estimate_usd, "error": None, "result": None,
           "cancellable": cancellable, "parent_id": parent, "params": params or {}}
    with _JLOCK:
        _JOBS[jid] = job
        _PRIV[jid] = {"cancel": threading.Event(), "done": threading.Event(), "cancellable_now": True}
        _trim()
    _changed(job)
    ctx_vars = contextvars.copy_context()
    t = threading.Thread(target=ctx_vars.run, args=(_run, job, fn, run_at, priority, on_done),
                         daemon=True, name=f"job-{kind}-{jid[-4:]}")
    t.start()
    return _view(job)


def _run(job: dict, fn, run_at, priority: int, on_done) -> None:
    priv = _PRIV[job["id"]]
    ctx = JobCtx(job, priority)
    sem = _SEMS[job["pool"]]
    acquired = False
    try:
        if run_at:
            if priv["cancel"].wait(max(0.0, run_at - time.time())):
                raise Cancelled()
        while not sem.acquire(timeout=0.5):
            if priv["cancel"].is_set():
                raise Cancelled()
        acquired = True
        if priv["cancel"].is_set():
            raise Cancelled()
        job["state"], job["started_at"] = "running", now_iso()
        priv["cancellable_now"] = job["cancellable"]
        _changed(job)
        with actions.using_studio(ctx.studio):
            res = fn(ctx)
            if inspect.isawaitable(res):
                import asyncio
                res = asyncio.run(res)
        job["result"] = res if res is not None else job.get("result")
        job["state"] = "succeeded"
    except Cancelled:
        job["state"] = "cancelled"
    except actions.ActionError as e:
        job["state"], job["error"] = "failed", e.as_dict()
    except studio.StudioError as e:
        job["state"], job["error"] = "failed", actions.studio_error_dict(e)
    except BaseException as e:  # noqa: BLE001  (a job never takes the server down)
        traceback.print_exc()
        job["state"] = "failed"
        job["error"] = {"code": "internal", "message": str(e) or e.__class__.__name__, "details": {}}
    finally:
        if acquired:
            sem.release()
        job["ended_at"] = now_iso()
        job["progress"]["step"] = None if job["state"] == "succeeded" else job["progress"]["step"]
        _changed(job)
        try:
            _fail_toast(job)
        except Exception:
            traceback.print_exc()
        priv["done"].set()
        if on_done:
            try:
                on_done(_view(job))
            except Exception:
                traceback.print_exc()


# Studio-writing jobs whose failure must reach Austin: what the toast calls them. (A claim's failure is shown
# by the claim line that started it; background refreshes fail quietly and retry.)
FAIL_TOAST = {"save": "Save to Studio", "qc": None, "qc_wait": None, "submit": "Submit for review",
              "unclaim": "Unclaim", "drop": "Drop"}


def _task_label(short: str | None) -> str:
    if not short:
        return "the task"
    try:
        import json
        from .. import config
        meta = json.loads((config.TASKS / short / "task.json").read_text())
        return meta.get("label") or meta.get("task_name") or short
    except Exception:
        return short


def _fail_toast(job: dict) -> None:
    """A failed Studio job says so, with Studio's words verbatim (job.error is studio_error_dict's shape)."""
    if job["state"] != "failed" or job["kind"] not in FAIL_TOAST:
        return
    what = FAIL_TOAST[job["kind"]] or (job.get("params") or {}).get("spec") or "AutoQC"
    tgt = job.get("target") or ""
    short = tgt[2:] if tgt.startswith("t:") else None
    msg = (job.get("error") or {}).get("message") or "it stopped without a message"
    publish("toast", {"id": f"fail-{job['id']}", "level": "error", "undo": None,
                      "text": f"{what} failed for {_task_label(short)}: {msg}",
                      "link": {"label": "Open the task", "href": f"/task/{short}"} if short else None})


def _trim() -> None:
    done = [k for k, j in _JOBS.items() if j["state"] in ("succeeded", "failed", "cancelled")]
    for k in done[:-KEEP] if len(done) > KEEP else []:
        _JOBS.pop(k, None)
        _PRIV.pop(k, None)


def get(job_id: str) -> dict | None:
    j = _JOBS.get(job_id)
    return _view(j) if j else None


def cancel(job_id: str) -> dict:
    j = _JOBS.get(job_id)
    if not j:
        raise actions.ActionError("not_found", "No such job.")
    if j["state"] in ("succeeded", "failed", "cancelled"):
        return _view(j)
    priv = _PRIV[job_id]
    if j["state"] == "running" and not priv["cancellable_now"]:
        raise actions.ActionError("conflict", "This step can't be stopped once it has started.", {"job": _view(j)})
    priv["cancel"].set()
    return _view(j)


def wait(job_id: str, timeout: float = 30) -> dict | None:
    priv = _PRIV.get(job_id)
    if priv:
        priv["done"].wait(timeout)
    return get(job_id)


def active(kind: str | None = None, target: str | None = None) -> list[dict]:
    return [_view(j) for j in list(_JOBS.values()) if j["state"] in ("queued", "running")
            and (kind is None or j["kind"] == kind) and (target is None or j["target"] == target)]


def list_jobs(state: str | None = None, limit: int = 50) -> list[dict]:
    rows = [j for j in reversed(list(_JOBS.values())) if not state or j["state"] == state]
    return [_view(j) for j in rows[:limit]]


# --- undo ---------------------------------------------------------------------------------------------

_UNDO: dict[str, dict] = {}


def register_undo(kind: str, short: str | None, seconds: float, fn, label: str) -> dict:
    """An undo for `seconds`; fn() runs on POST /api/undo/{action_id} and returns a dict."""
    aid = "undo_" + secrets.token_hex(6)
    exp = time.time() + seconds
    _UNDO[aid] = {"kind": kind, "short": short, "expires": exp, "fn": fn, "state": "open", "label": label}
    for k in [k for k, u in _UNDO.items() if u["expires"] < time.time() - 3600]:
        _UNDO.pop(k, None)
    return {"action_id": aid, "expires_at": _iso(exp), "label": label}


def run_undo(action_id: str) -> dict:
    u = _UNDO.get(action_id)
    if not u:
        raise actions.ActionError("not_found", "Nothing to undo.")
    if u["state"] != "open":
        raise actions.ActionError("expired", "That was already undone.")
    if time.time() > u["expires"] + 1:          # one second of grace for the round trip
        u["state"] = "expired"
        raise actions.ActionError("expired", "Too late to undo that.")
    u["state"] = "done"
    return u["fn"]() or {}


def close_undo(action_id: str | None) -> None:
    if action_id and action_id in _UNDO:
        _UNDO[action_id]["state"] = "closed"


# --- lifecycle ----------------------------------------------------------------------------------------

def start() -> None:
    worker.start()


def stop() -> None:
    for j in active():
        try:
            cancel(j["id"])
        except actions.ActionError:
            pass
    worker.stop()
