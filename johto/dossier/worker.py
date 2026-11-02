"""The dossier worker: keeps `target` dossiers ready or generating, one generation at a time.

The queue lives in data/dossiers/index.json. `fill()` tops the active set (ready + generating + queued)
up to the target from the ranked pool, best first, skipping repos that already have an item. The worker
then generates the best-ranked queued item. Declining or acting on a dossier removes it from the active
set and kicks the worker, so the next repo starts right away. Pausing stops new generations and, unless
asked to let it finish, stops the running one too (it shares Austin's subscription with his lanes); a
stopped item goes back to 'queued' and does not count as a failure. Items left 'generating' by a dead
process go back to 'queued' when a worker starts.

Several generations never overlap: whoever holds store.generation_lock is the one generating, across
the web server's thread and `johto dossier ...` processes.

Repo briefs (johto/dossier/brief.py) ride on the same loop and lock: a dossier written ready queues its brief,
the worker's start queues briefs for ready dossiers with none or a stale one, and each step runs one queued
brief before the next dossier (a brief takes minutes, a dossier most of an hour). Pause stops both.
"""
from __future__ import annotations

import shutil
import threading
import time
import traceback
from typing import Callable

from . import candidates, generate, store


def _brief():
    from . import brief   # late: brief imports generate and store, like this module
    return brief


def queue_brief(did: str, reason: str = "") -> bool:
    """Queue the repo brief for a dossier (never raises: a brief must not break the dossier worker)."""
    try:
        return _brief().queue(did, reason=reason)
    except Exception:
        traceback.print_exc()
        return False

POLL_S = 60
FILL_EVERY_S = 600
FAIL_STREAK = 3
BACKOFF_S = 30 * 60
POOL_LIMIT = 40
MAX_RETRIES = 3  # usage-limit requeues of one item before it fails
WATCH_S = 2.0  # how often a generation checks the index for a stop request


# --- queue -------------------------------------------------------------------------------

def _entry(it: dict) -> dict:
    return {"full_name": it["repo"], "display_name": it.get("display_name") or it["repo"],
            "commit": it["commit"], "pinned_url": it.get("pinned_url"),
            "language": it.get("language"), "promise": it.get("promise"),
            "reasons": it.get("promise_reasons") or [], "fits": it.get("fits") or [],
            "focus": it.get("focus") or []}


def _from_pool(it: dict, rank: int, p: dict) -> None:
    # Re-vetted at a newer commit. A manual item keeps the commit it was asked for.
    if p["commit"] != it["commit"] and it["status"] == "queued" and not it.get("manual"):
        it.update(commit=p["commit"], id=store.make_id(p["full_name"], p["commit"]), pinned_url=p.get("pinned_url"))
    it.update(rank=rank, promise=p.get("promise"), promise_reasons=list(p.get("reasons") or []),
              fits=list(p.get("fits") or []), language=p.get("studio_language") or p.get("language"),
              display_name=p.get("display_name") or p["full_name"])


def fill(pool: list[dict] | None = None) -> dict:
    """Rebuild the queue from the current ranking: the best-ranked repos without an item, until the active
    set (ready + generating + queued) reaches the target. Queued items hold no work, so they are re-picked
    every time; ready and generating items keep their place and get their current rank."""
    pool = candidates.ranked(POOL_LIMIT) if pool is None else pool
    with store.edit() as idx:
        target = int(idx.get("target") or store.TARGET)
        pos = {p["full_name"].lower(): (rank, p) for rank, p in enumerate(pool, 1)}
        was_queued = {it["repo"].lower(): it for it in idx["items"]
                      if it["status"] == "queued" and not it.get("manual")}
        keep = [it for it in idx["items"] if it["repo"].lower() not in was_queued]
        for it in keep:
            if it["status"] in ("ready", "generating", "queued") and it["repo"].lower() in pos:
                _from_pool(it, *pos[it["repo"].lower()])
        have = {it["repo"].lower() for it in keep}
        active = sum(1 for it in keep if it["status"] in store.ACTIVE)
        queued_ids = []
        for rank, p in enumerate(pool, 1):
            if active >= target:
                break
            key = p["full_name"].lower()
            if key in have:
                continue
            it = was_queued.get(key) or store.new_item(p, rank)
            _from_pool(it, rank, p)
            keep.append(it)
            have.add(key)
            queued_ids.append(it["id"])
            active += 1
        before = {it["id"] for it in was_queued.values()}
        idx["items"] = keep
        idx["filled_at"] = store.now_iso()
    return {"added": [i for i in queued_ids if i not in before], "dropped": sorted(before - set(queued_ids))}


def next_queued(idx: dict) -> dict | None:
    q = [it for it in idx["items"] if it["status"] == "queued"]
    q.sort(key=lambda it: (not it.get("manual"), it.get("rank") or 10**6))
    return q[0] if q else None


def fail_streak(idx: dict) -> int:
    """Failures in a row among items finished since the worker was last resumed."""
    since = idx.get("resumed_at") or ""
    done = sorted((it for it in idx["items"] if it.get("finished_at") and it["finished_at"] > since
                   and it["status"] in ("ready", "failed", "actioned", "declined")),
                  key=lambda it: it["finished_at"], reverse=True)
    n = 0
    for it in done:
        if it["status"] != "failed":
            break
        n += 1
    return n


_current: dict[str, generate.Cancel] = {}  # generations running in this process
_current_lock = threading.Lock()


def current() -> str | None:
    """The id this process is generating, if any."""
    with _current_lock:
        return next(iter(_current), None)


def cancel_running(reason: str) -> list[str]:
    """Stop the generations running in this process now. Returns their ids."""
    with _current_lock:
        running = list(_current.items())
    for _, c in running:
        c.cancel(reason)
    return [did for did, _ in running]


def _watch(did: str, cancel: generate.Cancel, done: threading.Event) -> None:
    """Stops the generation when any process asks for it in the index, or the item stops being 'generating'."""
    while not done.wait(WATCH_S):
        try:
            idx = store.load()
        except Exception:
            continue
        reason = (idx.get("stop") or {}).get(did)
        it = store.find(idx, did)
        if reason or it is None or it["status"] != "generating":
            cancel.cancel(reason or "the item left the queue")
            return


def queue_manual(entry: dict) -> str:
    """Queue one repo by hand (a candidates entry, maybe with "focus") and return its id. The caller holds
    store.generation_lock, so no worker can start this item (or any other) before the caller runs it."""
    did = store.make_id(entry["full_name"], entry["commit"])
    with store.edit() as idx:
        it = store.find(idx, did)
        if it is None:
            old = store.find_repo(idx, entry["full_name"])
            if old is not None and old["status"] in ("queued", "failed"):
                idx["items"].remove(old)  # replaced by this run (same repo, maybe another commit)
            pool = [e["full_name"].lower() for e in candidates.ranked(POOL_LIMIT)]
            rank = pool.index(entry["full_name"].lower()) + 1 if entry["full_name"].lower() in pool else None
            it = store.new_item(entry, rank, manual=True)
            idx["items"].append(it)
        else:  # 'generating' here is stale: the caller holds the lock
            it.update(status="queued", manual=True, started_at=None, progress=None)
        if entry.get("focus"):
            it["focus"] = list(entry["focus"])
    return did


def run_item(did: str, echo: Callable[[str], None] | None = None,
             cancel: generate.Cancel | None = None) -> generate.Result:
    """Generate one item now. The caller holds store.generation_lock. Does nothing (a `skipped` Result)
    unless the item is still queued when it starts, so a decline or another run can't be overwritten."""
    cancel = cancel or generate.Cancel()
    with store.edit() as idx:
        it = store.find(idx, did)
        if it is None:
            raise KeyError(did)
        if it["status"] != "queued":
            return generate.Result(error=f"it is {it['status']}, not queued", skipped=True)
        store.clear_stop(idx, did)
        it.update(status="generating", started_at=store.now_iso(), finished_at=None, error=None, errors=None,
                  cost_usd=None, elapsed_s=None, progress={"step": "cloning", "elapsed_s": 0, "log_tail": []})
        entry = _entry(it)
    with _current_lock:
        _current[did] = cancel
    done = threading.Event()
    threading.Thread(target=_watch, args=(did, cancel, done), name="dossier-stop-watch", daemon=True).start()
    try:
        res = generate.generate(entry, did, on_progress=lambda snap: store.update(did, progress=snap), echo=echo,
                                cancel=cancel)
    finally:
        done.set()
        with _current_lock:
            _current.pop(did, None)
    if res.ok and res.doc:
        store.save_dossier(res.doc)
        queue_brief(did, "the dossier is ready")
    with store.edit() as idx:
        store.clear_stop(idx, did)
        it = store.find(idx, did)
        if it is not None:
            common = dict(cost_usd=round(res.cost_usd, 4), elapsed_s=res.elapsed_s, model=res.model,
                          passes=res.passes)
            retries = (it.get("retries") or 0) + (1 if res.retryable else 0)
            if res.ok:
                it.update(status="ready", finished_at=store.now_iso(), error=None, errors=None, **common,
                          progress={"step": "done", "elapsed_s": res.elapsed_s, "log_tail": []})
            elif res.cancelled:
                it.update(status="queued", started_at=None, error=res.error, progress=None)
            elif res.retryable and retries < MAX_RETRIES:
                it.update(status="queued", started_at=None, error=res.error, progress=None, retries=retries)
            elif res.retryable:
                it.update(status="failed", finished_at=store.now_iso(), retries=retries, **common,
                          error=f"{res.error}; gave up after {retries} tries", errors=None,
                          progress={**(it.get("progress") or {}), "step": "failed"})
            else:
                it.update(status="failed", finished_at=store.now_iso(), error=res.error,
                          errors=res.errors[:60] or None, **common,
                          progress={**(it.get("progress") or {}), "step": "failed"})
    return res


def clean_stale_clones() -> None:
    """Clones and Claude session folders left by a process that died mid-run. Only while nothing is
    generating."""
    with store.generation_lock(blocking=False) as got:
        if not got:
            return
        if store.P.clones.exists():
            for d in store.P.clones.iterdir():
                shutil.rmtree(d, ignore_errors=True)
        generate.clean_stale_sessions(store.P.clones)


# --- actions (used by the API and the CLI) ------------------------------------------------------

def _kick() -> None:
    WORKER.kick()


STOPPED_BY_PAUSE = "stopped by pause"


def set_paused(paused: bool, reason: str | None = None, stop: bool = False) -> dict:
    """Pause or resume. With `stop`, a pause also stops the running generation, in whichever process it
    runs (the item goes back to queued; nothing counts as a failure). Returns status() plus "stopping"."""
    stopping = []
    with store.edit() as idx:
        idx["paused"] = bool(paused)
        if paused:
            idx["paused_reason"] = reason
            if stop:
                for it in idx["items"]:
                    if it["status"] == "generating":
                        store.request_stop(idx, it["id"], STOPPED_BY_PAUSE)
                        stopping.append(it["id"])
        else:
            idx.pop("paused_reason", None)
            idx["resumed_at"] = store.now_iso()
    if paused and stop:
        cancel_running(STOPPED_BY_PAUSE)  # this process: now rather than at the next poll
        try:
            stopping += _brief().request_stop(STOPPED_BY_PAUSE)  # briefs, in any process
        except Exception:
            traceback.print_exc()
    if not paused:
        _kick()
    return {**status(), "stopping": stopping}


class Conflict(Exception):
    pass


def decline(did: str, reason: str, note: str = "") -> dict:
    if reason not in store.DECLINE_REASONS:
        raise ValueError(f"reason must be one of {', '.join(store.DECLINE_REASONS)}")
    with store.edit() as idx:
        it = store.find(idx, did)
        if it is None:
            raise KeyError(did)
        if it["status"] == "generating":
            raise Conflict("it is generating right now")
        it.update(status="declined", decline={"reason": reason, "note": note or ""}, closed_at=store.now_iso())
        out = dict(it)
    _kick()
    return out


def act(did: str, opportunity_id: str | None = None, task_short: str | None = None) -> dict:
    with store.edit() as idx:
        it = store.find(idx, did)
        if it is None:
            raise KeyError(did)
        if it["status"] != "ready":
            raise Conflict(f"it is {it['status']}, not ready")
        it.update(status="actioned", actioned={"task_short": task_short, "opportunity_id": opportunity_id},
                  closed_at=store.now_iso())
        out = dict(it)
    _kick()
    return out


def restore(did: str) -> dict:
    """Back to ready (or to queued when it never produced a dossier)."""
    with store.edit() as idx:
        it = store.find(idx, did)
        if it is None:
            raise KeyError(did)
        if it["status"] == "generating":
            raise Conflict("it is generating right now")
        has_doc = store.dossier_path(did).exists()
        it.update(status="ready" if has_doc else "queued", decline=None, actioned=None, closed_at=None)
        if not has_doc:
            it.update(started_at=None, finished_at=None, progress=None, error=None, errors=None, manual=True)
        out = dict(it)
    _kick()
    return out


def status() -> dict:
    idx = store.load()
    cur = current()
    busy = cur is not None or store.generation_busy()
    if busy and cur is None:
        cur = next((it["id"] for it in idx["items"] if it["status"] == "generating"), None)
    # a brief holds the same lock: busy stays true, and brief.current() names it (the keys here are fixed)
    return {"running": WORKER.alive or busy, "current": cur, "busy": busy, "paused": bool(idx.get("paused")),
            "paused_reason": idx.get("paused_reason"),
            "backoff_until": WORKER.backoff_until_iso()}


# --- the loop ----------------------------------------------------------------------------------

class Worker:
    def __init__(self, echo: Callable[[str], None] | None = None):
        self.echo = echo
        self._kick = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_fill = 0.0
        self._force_fill = True
        self._backoff_until = 0.0
        self._said_paused = False

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def backoff_until_iso(self) -> str | None:
        if self._backoff_until <= time.time():
            return None
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self._backoff_until))

    def log(self, msg: str) -> None:
        line = f"dossier worker: {msg}"
        (self.echo or (lambda s: print(s, flush=True)))(line)

    def start(self) -> None:
        """Start the background thread (idempotent)."""
        if self.alive:
            return
        moved = store.requeue_stale()
        if moved:
            self.log(f"requeued {', '.join(moved)}")
        self.queue_briefs()
        clean_stale_clones()
        self._stop.clear()
        self._thread = threading.Thread(target=self.loop, name="dossier-worker", daemon=True)
        self._thread.start()

    def queue_briefs(self) -> list[str]:
        """Briefs left generating by a dead process go back to the queue; ready dossiers with no brief or a
        stale one get one queued (spec 7.1)."""
        try:
            b = _brief()
            moved = b.requeue_stale()
            added = b.queue_missing()
        except Exception:
            self.log("brief queue failed:\n" + traceback.format_exc())
            return []
        if moved or added:
            self.log(f"briefs: requeued {len(moved)}, queued {len(added)}")
        return added

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()

    def kick(self) -> None:
        self._kick.set()

    def loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.step()
            except Exception:
                self.log("step failed:\n" + traceback.format_exc())
                worked = False
            if not worked:
                woke = self._kick.wait(POLL_S)
                self._kick.clear()
                self._force_fill = self._force_fill or woke

    def step(self) -> bool:
        """Fill the queue when due, then run one generation if allowed. True when it generated."""
        if self._force_fill or time.time() - self._last_fill >= FILL_EVERY_S:
            self._force_fill = False
            self._last_fill = time.time()
            r = fill()
            if r["added"] or r["dropped"]:
                self.log(f"queue: +{len(r['added'])} -{len(r['dropped'])}")
        idx = store.load()
        if idx.get("paused"):
            if not self._said_paused:
                self.log("paused")
                self._said_paused = True
            return False
        self._said_paused = False
        if time.time() < self._backoff_until:
            return False
        if fail_streak(idx) >= FAIL_STREAK:
            set_paused(True, f"{FAIL_STREAK} generations failed in a row")
            self.log(f"paused after {FAIL_STREAK} failures in a row")
            return False
        if self.brief_step():
            return True
        nxt = next_queued(idx)
        if nxt is None:
            return False
        with store.generation_lock(wait_s=1.0) as got:
            if not got:  # `johto dossier once` or another worker is generating
                return False
            idx = store.load()
            it = store.find(idx, nxt["id"])
            if idx.get("paused") or it is None or it["status"] != "queued":
                return True
            self.log(f"generating {it['id']}")
            res = run_item(it["id"], echo=self.echo if self.echo else None)
            if res.skipped:
                return True
            self.log(f"{it['id']}: " + ("ready" if res.ok else res.error or "failed")
                     + f" ({res.elapsed_s}s, ${res.cost_usd:.2f})")
            if res.retryable:
                self._backoff_until = time.time() + BACKOFF_S
        self._force_fill = True
        return True


    def brief_step(self) -> bool:
        """Run one queued brief (the caller checked pause and backoff). True when it ran one."""
        try:
            b = _brief()
            did = b.next_queued()
        except Exception:
            self.log("brief queue failed:\n" + traceback.format_exc())
            return False
        if did is None:
            return False
        with store.generation_lock(wait_s=1.0) as got:
            if not got:
                return False
            if store.load().get("paused") or b.next_queued() != did:
                return True
            self.log(f"writing the brief for {did}")
            res = b.generate(did, locked=True, echo=self.echo if self.echo else None)
            self.log(f"{did} brief: " + ("ready" if res["ok"] else res["error"] or "failed")
                     + f" ({res['elapsed_s']}s, ${res['cost_usd']:.2f})")
            if res["retryable"]:
                self._backoff_until = time.time() + BACKOFF_S
        return True


WORKER = Worker()


def start() -> None:
    WORKER.start()


SHUTDOWN_REASON = "stopped when the server shut down"


def shutdown(timeout: float = 20.0) -> None:
    """Stop the loop and the generation running in this process (its item goes back to queued), then wait
    for its clone and session cleanup. For the web server's lifespan shutdown."""
    WORKER.stop()
    cancel_running(SHUTDOWN_REASON)
    try:
        _brief().cancel_running(SHUTDOWN_REASON)
    except Exception:
        pass
    t = WORKER._thread
    if t is not None and t.is_alive() and t is not threading.current_thread():
        t.join(timeout)
    generate._kill_live()
