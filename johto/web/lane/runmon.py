"""The run monitor: one run's clocks, its last turn and its review, for the lane drawer, the lane surface and Home.

Design section 6 and the GPT Pro UX review, section 3F. For a run on this Mac or on the PC it reports:

- who: task, run and machine (Mac or PC), and whether the lane is up there;
- Claude's working minutes for the run, and for the task across all its runs (the 90-minute budget);
- background work as its own duration and state (jobs Claude started that are still running);
- human wait: how long Claude has waited on Austin since its last ended turn;
- what that turn was (budget.classify_turn: question, permission, substantive, progress, background, problem);
- the run's review, naming the turn it covers: queued (pack built, not sent), reviewing (sent), ready (saved).

budget.status does the measuring. For a PC run it copies the live transcript over ssh, so in the web app this
module never measures on a request: start() runs one background thread, get() returns the last value at once
and asks the thread for a fresh one when it is stale. Without the thread (the CLI, tests) get() measures
inline and leaves the network alone unless asked (`network=True`).

Reads only. The one thing written is budget's own copy of a PC transcript under runs/<run>/.live-transcripts.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from ... import config, lanes, transcript, workspace

CLASSES = ("question", "permission", "substantive", "progress", "background", "problem")
# Seconds a value stays fresh: a turn that is running counts up, a PC run costs an ssh, a finished run rarely moves.
TTL_OPEN, TTL_LIVE, TTL_REMOTE, TTL_DONE = 5.0, 10.0, 30.0, 120.0
PROBE_TTL = 60.0
TAIL_BYTES = 512 * 1024
HOSTS = {"build-box": "PC"}

_cache: dict[tuple[str, str], dict] = {}     # (task dir, run) -> {"data", "sig", "at"}
_probes: dict[str, tuple[float, dict]] = {}  # host -> (at, turnwatch.probe_host result)
_lock = threading.Lock()
_due: dict[tuple[str, str], float] = {}      # keys the thread should measure, with when they were asked for
_hints: dict[tuple[str, str], bool | None] = {}   # the caller's word on whether a Mac lane is up
_wake = threading.Event()
_thread: threading.Thread | None = None
_stop = threading.Event()
_listeners: list = []


# --- which run ---------------------------------------------------------------------------------------------

def run_name(rec: dict) -> str:
    return (rec.get("dir") or "").removeprefix("runs/")


def record(meta: dict, run: str) -> dict:
    return next((r for r in meta.get("runs") or [] if run_name(r) == run), {})


def submission_run(meta: dict, task_dir: Path) -> str | None:
    """actions.submission_run when STATE has added it, else the same rule: the run Austin chose (task.json
    submission_run) if its folder exists, else the latest collected validation run."""
    try:
        from ... import actions
        fn = getattr(actions, "submission_run", None)
        if callable(fn):
            return fn(meta, Path(task_dir))
    except Exception:
        pass
    want = meta.get("submission_run")
    if want and (Path(task_dir) / "runs" / str(want)).is_dir():
        return want
    done = [run_name(r) for r in meta.get("runs") or [] if r.get("collected_at") and run_name(r).startswith("validate-")]
    return done[-1] if done else None


def current_run(meta: dict, task_dir: Path) -> str | None:
    """The run Home and the drawer watch for a task: the newest one that is neither collected nor removed (on
    any machine), else the submission run, else the newest run."""
    task_dir = Path(task_dir)
    for rec in reversed(meta.get("runs") or []):
        if rec.get("torn_down_at") or rec.get("collected_at"):
            continue
        if (task_dir / (rec.get("dir") or "-")).is_dir():
            return run_name(rec)
    sub = submission_run(meta, task_dir)
    if sub:
        return sub
    names = [run_name(r) for r in meta.get("runs") or [] if run_name(r)]
    return names[-1] if names else None


# --- pieces -----------------------------------------------------------------------------------------------

def run_word(run: str | None) -> str:
    kind, _, n = (run or "").partition("-")
    return f"{'calibration' if kind == 'calib' else 'validation' if kind == 'validate' else kind} {n}".strip()


def host_label(host: str | None) -> str:
    return "Mac" if not host else HOSTS.get(host, host)


def _iso(t: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t)) if t else None


def _ts(s) -> float | None:
    d = transcript._ts(s) if isinstance(s, str) else None
    return d.timestamp() if d else None


def tail_facts(path: Path | None) -> dict:
    """The last assistant text and any usage or API problem, from the end of a transcript (cheap)."""
    out = {"text": None, "problem": None}
    if not path:
        return out
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return out
    from . import monitor
    red = monitor.Reducer()
    entries = []
    for line in raw.splitlines():
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict):
            entries.append(v)
    red.feed(entries)
    out["text"] = red.last_claude_text
    if red.rate:
        out["problem"] = "usage limit" + (f", resets {red.rate['until']}" if red.rate.get("until") else "")
    elif red.last and red.last.get("kind") == "claude" and entries:
        last = next((e for e in reversed(entries) if e.get("type") == "assistant" and not e.get("isSidechain")), {})
        if last.get("isApiErrorMessage"):
            out["problem"] = (red.last_claude_text or "API error")[:120]
    return out


def _asks(text: str | None) -> bool:
    """Claude's last paragraph asks Austin something."""
    if not text:
        return False
    paras = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
    last = paras[-1] if paras else text.strip()
    return "?" in last[-400:]


def classify(st: dict, tail: dict | None = None) -> tuple[str | None, str | None, dict]:
    """(class, source, facts) of the run's last ended turn. budget.turn_facts / classify_turn (REVIEWLOOP's)
    when present, else a small rule over the transcript's tail. facts carries `asks` (the question Claude put,
    even on a substantive turn) and `why`. (None, None, {}) while a turn runs or before the first prompt."""
    last = st.get("last_turn")
    if not last or last.get("ended") == "open":
        return None, None, {}
    try:
        from ... import budget
        facts_fn = getattr(budget, "turn_facts", None)
        fn = getattr(budget, "classify_turn", None)
        if callable(facts_fn):
            f = facts_fn(st, last.get("index"))
            if f.get("kind") in CLASSES:
                return f["kind"], "budget", {"asks": f.get("asks"), "why": f.get("why"),
                                             "problem": f.get("problem")}
        elif callable(fn):
            try:
                got = fn(st, last.get("index"))
            except TypeError:
                got = fn(st, last)
            if got in CLASSES:
                return got, "budget", {}
    except Exception:
        traceback.print_exc()
    tail = tail or {}
    if st.get("background_running"):
        return "background", "rule", {}
    if tail.get("problem"):
        return "problem", "rule", {"problem": tail["problem"]}
    if _asks(tail.get("text")):
        return "question", "rule", {}
    return "substantive", "rule", {}


def _question(cls: str | None, facts: dict, tail: dict) -> str | None:
    if facts.get("asks"):
        return str(facts["asks"])[:400]
    if cls == "question":
        return (tail.get("text") or "")[-400:] or None
    return None


def review_status(task_dir: Path, run: str, turns: int | None = None) -> dict:
    """The run's outside review: reviewing (sent, reply not saved), queued (a pack newer than the last saved
    review, not sent), ready (saved) or none. `turn` is the checkpoint it covers."""
    out = {"state": "none", "turn": None, "at": None, "text": "", "covers_latest": None, "path": None,
           "unconfirmed": False}
    try:
        from ... import reviews
    except Exception:
        return out
    try:
        waiting = [e for e in reviews.pending(task_dir) if e.get("run") == run]
        latest = reviews.latest(task_dir, run)
    except Exception:
        waiting, latest = [], None
    try:
        from ... import delta
        packs = delta.packs(task_dir, run)
    except Exception:
        packs = []
    if waiting:
        e = sorted(waiting, key=lambda e: e.get("sent_at") or "")[-1]
        out.update(state="reviewing", turn=e.get("turn"), at=e.get("sent_at"), unconfirmed=bool(e.get("unconfirmed")))
    else:
        rturn = (latest or {}).get("turn") or 0
        # A pack built before the automation (backfilled) went out by hand, if at all: it isn't queued.
        newer = [p for p in packs if not p.get("sent_at") and not p.get("backfilled") and (p.get("turn") or 0) > rturn
                 and Path(p.get("path") or "").name not in {Path((latest or {}).get("pack") or "").name}]
        if newer:
            p = newer[-1]
            out.update(state="queued", turn=p.get("turn"), at=p.get("built_at"), path=p.get("path"))
        elif latest:
            out.update(state="ready", turn=latest.get("turn"), at=latest.get("saved_at"), path=latest.get("path"))
    if out["turn"] is not None and turns:
        out["covers_latest"] = int(out["turn"]) >= int(turns)
        out["of"] = int(turns)
    t = f"turn {out['turn']}" if out["turn"] is not None else "the run"
    # A ready review of an earlier turn says so: its results are as of that turn, not the run's.
    ready = f"Review of turn {out['turn']} of {turns}" if out["covers_latest"] is False else f"Review ready for {t}"
    out["text"] = {"reviewing": f"Reviewing {t}", "queued": f"Review queued for {t}",
                   "ready": ready, "none": ""}[out["state"]]
    return out


def _probe(host: str, container: str | None, network: bool) -> dict:
    """{"up": True|False|None, "why": str|None} for a PC lane: one ssh per host (turnwatch.probe_host), cached."""
    if config.FAKE:
        return {"up": None, "why": "not checked in fake mode"}
    if not network:
        return {"up": None, "why": "not checked"}
    at, res = _probes.get(host, (0.0, None))
    if res is None or time.time() - at > PROBE_TTL:
        try:
            from ... import turnwatch
            res = turnwatch.probe_host(host, [lanes.lane_key(container)] if container else [])
        except Exception as e:
            res = {"error": f"{host}: {e}", "running": set()}
        _probes[host] = (time.time(), res)
    if res.get("error"):
        return {"up": None, "why": res["error"]}
    return {"up": bool(container) and container in (res.get("running") or set()), "why": None}


def _local_up(container: str | None) -> bool | None:
    if not container:
        return None
    try:
        from .. import data
        return container in data.running_containers()
    except Exception:
        return None


def _status(task_dir: Path, run: str, rec: dict, up: bool | None, network: bool) -> dict:
    """budget.status, without the network for a PC run unless allowed, and without counting up to now when the
    local lane is down (budget would see its config-dir transcript and treat the run as live)."""
    from ... import budget
    remote = lanes.remote_of(rec)
    if remote and not network:
        st = _collected_status(task_dir, run)
    else:
        st = budget.status(task_dir, run)
    # Whoever knows the lane better (the lane service, a probe) wins over budget's own docker check.
    if st.get("transcript") and up is not None and bool(st.get("live")) != bool(up):
        live = bool(up)
        s = transcript.summarize(Path(st["transcript"]), now=datetime.now(timezone.utc) if live else None)
        turns = s["turns"]
        st.update(live=live, working_minutes=s["working_minutes"], background_minutes=s["background_minutes"],
                  idle_minutes=s["idle_minutes"], turns=turns, last_turn=turns[-1] if turns else None,
                  turn_open=live and bool(turns) and turns[-1]["ended"] == "open",
                  background_running=s["background_running"] if live else 0)
        st["line"] = budget.line(st)
    return st


def _collected_status(task_dir: Path, run: str) -> dict:
    """budget.status's shape from the collected transcript only (no ssh)."""
    from ... import budget
    st = {"run": run, "live": False, "transcript": None, "working_minutes": 0.0, "background_minutes": 0.0,
          "idle_minutes": 0.0, "turns": [], "last_turn": None, "turn_open": False, "background_running": 0,
          "budget": budget.budget_minutes()}
    logs = transcript.find(Path(task_dir) / "runs" / run)
    if logs:
        s = transcript.summarize(logs[-1])
        turns = s["turns"]
        st.update(transcript=str(logs[-1]), working_minutes=s["working_minutes"],
                  background_minutes=s["background_minutes"], idle_minutes=s["idle_minutes"], turns=turns,
                  last_turn=turns[-1] if turns else None)
    st["line"] = budget.line(st)
    return st


# --- one run ----------------------------------------------------------------------------------------------

def compute(task_dir: Path, run: str, *, network: bool = True, up: bool | None = None,
            now: float | None = None) -> dict:
    """The monitor for one run, measured now. `up` overrides whether its lane is running (tests)."""
    task_dir = Path(task_dir)
    now = now or time.time()
    meta = workspace.load(task_dir)
    rec = record(meta, run)
    remote = lanes.remote_of(rec)
    host = remote[0] if remote else None
    run_dir = task_dir / (rec.get("dir") or f"runs/{run}")
    container = rec.get("container") or (lanes.manifest(run_dir).get("lane") or {}).get("container")
    why = None
    if up is None and not rec.get("torn_down_at"):
        if remote:
            p = _probe(host, container, network)
            up, why = p["up"], p["why"]
        else:
            up = _local_up(container)
    if rec.get("torn_down_at"):
        up = False
    st = _status(task_dir, run, rec, up, network)
    last = st.get("last_turn") or None
    tail = tail_facts(Path(st["transcript"])) if st.get("transcript") and last and last.get("ended") != "open" else {}
    cls, src, facts = classify(st, tail) if (st.get("live") or up) else (None, None, {})
    collected = bool(rec.get("collected_at"))
    if rec.get("torn_down_at"):
        state = "collected" if collected else "removed"
    elif up:
        if not st.get("turns"):
            state = "ready"
        elif st.get("turn_open"):
            state = "working"
        elif st.get("background_running"):
            state = "background"
        else:
            state = "waiting"
    elif up is False:
        state = "collected" if collected else ("stopped" if st.get("transcript") else "not_started")
    else:   # can't tell: the PC didn't answer, Docker is stopped there, or fake mode
        state = "collected" if collected and not st.get("live") else ("paused" if "Docker" in (why or "") else "unknown")
    others = sum((workspace.run_minutes(r, task_dir) or 0) for r in meta.get("runs") or []
                 if run_name(r) and run_name(r) != run)
    task_min = round(others + (st.get("working_minutes") or 0), 1)
    budget_min = st.get("budget") or 90
    wait = None
    if state == "waiting" and last and last.get("end"):
        since = _ts(last["end"])
        if since:
            wait = {"since": last["end"], "minutes": round(max(0.0, now - since) / 60, 1)}
    bg = {"minutes": st.get("background_minutes") or 0.0, "running": st.get("background_running") or 0}
    rv = review_status(task_dir, run, len(st.get("turns") or []))
    return {
        "short": task_dir.name, "run": run, "run_word": run_word(run), "kind": run.partition("-")[0],
        "host": host_label(host), "host_name": host, "port_slot": rec.get("port_slot"), "container": container,
        "up": up, "why": why, "state": state, "live": bool(st.get("live")), "collected": collected,
        "turns": len(st.get("turns") or []), "turn": {k: last.get(k) for k in ("index", "ended", "start", "end",
                                                                                 "working_minutes", "background_minutes",
                                                                                 "no_reply")}
        if last else None,
        "working_minutes": st.get("working_minutes") or 0.0, "task_minutes": task_min, "budget": budget_min,
        "budget_tone": "red" if task_min >= budget_min else "amber" if task_min >= budget_min * 2 / 3 else "",
        "background": bg, "human_wait": wait, "turn_class": cls if state in ("waiting", "background") else None,
        "turn_class_source": src if state in ("waiting", "background") else None,
        # What Claude asked: a question turn, or a substantive one that ends by asking something.
        "question": _question(cls, facts, tail) if state == "waiting" else None,
        "turn_why": facts.get("why") if state in ("waiting", "background") else None,
        "problem": (facts.get("problem") or tail.get("problem")) if cls == "problem" else None,
        "review": rv, "line": st.get("line") or "",
        "method": "measured turn by turn from the transcript",
        "command": f"johto time {task_dir.name} {run}",
        "computed_at": _iso(now),
    }


def public(d: dict | None) -> dict | None:
    """The monitor without what changes on every measurement (the page counts the wait up from its start), so
    a task or lane event goes out only when something real changed."""
    if not d:
        return None
    out = {k: v for k, v in d.items() if k not in ("computed_at",)}
    if out.get("human_wait"):
        out["human_wait"] = {"since": out["human_wait"].get("since")}
    return out


# --- cache and the background thread ----------------------------------------------------------------------

def _sig(task_dir: Path, run: str) -> tuple:
    """What a measurement depends on, short of the clock: task.json, the run's transcripts, reviews, packs."""
    def st(p: Path):
        try:
            s = p.stat()
            return (s.st_mtime_ns, s.st_size)
        except OSError:
            return None
    run_dir = Path(task_dir) / "runs" / run
    files = [Path(task_dir) / "task.json", run_dir / "packs.json", Path(task_dir) / "reviews",
             Path(task_dir) / "reviews" / "pending.json"]
    files += sorted((run_dir / "prework_output").glob("*.jsonl"))
    return tuple(st(p) for p in files)


def _ttl(d: dict | None) -> float:
    if not d:
        return 0.0
    if d.get("state") == "working":
        return TTL_OPEN
    if d.get("host_name") and d.get("state") not in ("collected", "removed"):
        return TTL_REMOTE
    if d.get("state") in ("waiting", "background", "ready"):
        return TTL_LIVE
    return TTL_DONE


def _fresh(hit: dict | None, sig: tuple) -> bool:
    return bool(hit) and hit["sig"] == sig and time.time() - hit["at"] < _ttl(hit["data"])


def running() -> bool:
    return bool(_thread and _thread.is_alive())


def get(task_dir: Path, run: str | None, *, wait: float = 0.0, network: bool | None = None,
        up: bool | None = None) -> dict | None:
    """The monitor for a run. With the background thread running (the web app) this never measures: it
    returns the last value (None the first time, unless `wait` seconds are allowed) and asks for a fresh one.
    Without it, it measures inline, leaving the network alone unless network=True. `up`: whether this Mac's
    lane container for the run is running, when the caller already knows (the web app's Docker check)."""
    if not run:
        return None
    task_dir = Path(task_dir)
    key = (str(task_dir), run)
    try:
        sig = _sig(task_dir, run) + (up,)
    except Exception:
        sig = (up,)
    with _lock:
        hit = _cache.get(key)
    if _fresh(hit, sig):
        return hit["data"]
    if running():
        with _lock:
            _due.setdefault(key, time.time())
            _hints[key] = up
        _wake.set()
        if hit is None and wait > 0:
            end = time.time() + wait
            while time.time() < end:
                time.sleep(0.05)
                with _lock:
                    hit = _cache.get(key)
                if hit:
                    break
        return hit["data"] if hit else None
    try:
        d = compute(task_dir, run, network=bool(network), up=up)
    except Exception:
        traceback.print_exc()
        return hit["data"] if hit else None
    with _lock:
        _cache[key] = {"data": d, "sig": sig, "at": time.time()}
    return d


def for_task(task_dir: Path, meta: dict | None = None, local_up: dict | None = None, **kw) -> dict | None:
    """The monitor for the task's current run (current_run). `local_up`: {run: container running} for this Mac's
    lanes, from the caller's Docker check (a PC run's state comes from the PC)."""
    task_dir = Path(task_dir)
    try:
        meta = meta or workspace.load(task_dir)
    except (OSError, ValueError):
        return None
    run = current_run(meta, task_dir)
    up = None
    if run and local_up is not None and not lanes.remote_of(record(meta, run)):
        up = local_up.get(run)
    return get(task_dir, run, up=up, **kw)


def listen(fn) -> None:
    """fn() after the thread measured something that changed (the web app pokes its events engine)."""
    if fn not in _listeners:
        _listeners.append(fn)


def _same(a: dict | None, b: dict | None) -> bool:
    strip = lambda d: {k: v for k, v in (d or {}).items() if k not in ("computed_at", "human_wait")}
    wa, wb = (a or {}).get("human_wait"), (b or {}).get("human_wait")
    return strip(a) == strip(b) and (wa or {}).get("since") == (wb or {}).get("since")


def _work() -> None:
    while not _stop.is_set():
        _wake.wait(timeout=5.0)
        _wake.clear()
        with _lock:
            keys = sorted(_due, key=_due.get)
            _due.clear()
        changed = False
        for key in keys:
            if _stop.is_set():
                return
            task_dir, run = Path(key[0]), key[1]
            if not (task_dir / "task.json").exists():
                with _lock:
                    _cache.pop(key, None)
                continue
            try:
                with _lock:
                    hit, up = _cache.get(key), _hints.get(key)
                sig = _sig(task_dir, run) + (up,)
                if _fresh(hit, sig):
                    continue
                d = compute(task_dir, run, network=True, up=up)
            except Exception:
                traceback.print_exc()
                continue
            with _lock:
                _cache[key] = {"data": d, "sig": sig, "at": time.time()}
            if not _same((hit or {}).get("data"), d):
                changed = True
        if changed:
            for fn in list(_listeners):
                try:
                    fn()
                except Exception:
                    traceback.print_exc()


def start() -> None:
    """Run the measuring thread (the web app's lifespan, through api_lane.start)."""
    global _thread
    if running():
        return
    _stop.clear()
    _thread = threading.Thread(target=_work, name="johto-runmon", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    _wake.set()


def clear() -> None:
    """Forget every measurement (tests)."""
    with _lock:
        _cache.clear()
        _due.clear()
    _probes.clear()
