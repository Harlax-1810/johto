"""Research aimed at slots that have no great option yet.

Austin (2026-11-01): research for weak slot groups runs "auto" for now, and he can switch it to "manual" in the
app or by asking. A weak group is an open slot group of the kind he prefers (casual, async) whose best rated
option scores below slot_options.GREAT_AT, or that has no option at all (slot_options.for_now's "weak").

In auto mode Now, or a finished rating (`after_rating`), starts one research run at a time (`for_now`): the vetted repo that fits the group best and has
not been researched yet, aimed at that group like `johto dossier once --focus`, then its brief. The next rating
picks up its problems (a finished run starts that rating). Auto waits while the lane is working, waits COOLDOWN_S between runs for one group, and
stops after MAX_AUTO_TRIES runs for a group (the button on Now still works). Manual mode starts the same run from
that button or `johto options --research "Action|Domain|Codebase type"`.

Runs are kept in data/slot_research.json: {"schema", "runs": [{group, repo, did, auto, state, started_at,
finished_at, error}]}, state "running", "ready", "failed" or "nothing" (no repo fits).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import config

SCHEMA = "johto.slot_research/1"
MODES = ("auto", "manual")
MIN_STRENGTH = 0.55          # a repo's domain match must be at least this strong (data/scout's 0..1 strength)
COOLDOWN_S = 6 * 3600        # auto: wait this long after a run before trying the same group again
MAX_AUTO_TRIES = 3           # auto: research at most this many repos for one group
KEEP_RUNS = 200
RUN_FRESH_S = 90 * 60        # a "running" run older than this, with no job, has died

_lock = threading.Lock()        # the runs file
_start_lock = threading.Lock()  # one start at a time (two Now requests may arrive together)


def _file() -> Path:
    return config.DATA / "slot_research.json"


def load() -> dict:
    try:
        d = json.loads(_file().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        d = {}
    return {"schema": SCHEMA, "runs": list(d.get("runs") or [])}


def _save(d: dict) -> None:
    p = _file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps({"schema": SCHEMA, "runs": d["runs"][-KEEP_RUNS:]}, indent=1, ensure_ascii=False))
    os.replace(tmp, p)


def _record(run: dict) -> dict:
    with _lock:
        d = load()
        d["runs"] = [r for r in d["runs"] if r.get("id") != run["id"]] + [run]
        _save(d)
    return run


def _now() -> float:
    return time.time()


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t))


# --- mode --------------------------------------------------------------------------------------------------

def mode() -> str:
    from . import settings
    m = settings.get("slot_research")
    return m if m in MODES else "auto"


def set_mode(m: str) -> str:
    if m not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    from . import settings
    settings.save({"slot_research": m})
    return m


# --- which repo --------------------------------------------------------------------------------------------

def _known_languages() -> set[str]:
    """Languages of the repos in his Johto tasks (lower case)."""
    out = set()
    for p in config.TASKS.glob("*/task.json"):
        try:
            lang = ((json.loads(p.read_text()).get("repo") or {}).get("language") or "").lower()
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if lang:
            out.add(lang)
    return out


def pick_repo(group: str, pool: list[dict] | None = None, researched: set[str] | None = None,
              tried: set[str] | None = None) -> dict | None:
    """The vetted repo to research for one group ("Action|Domain|Codebase type"): one whose domain matches at
    MIN_STRENGTH or more and whose codebase type matches, not researched or tried before. Best first by domain
    strength, then a language from his tasks, then promise. None when no repo fits."""
    _action, domain, ctype = (group.split("|") + ["", "", ""])[:3]
    if pool is None:
        from .dossier import candidates
        pool = candidates.ranked(200)
    if researched is None:
        from .dossier import store
        researched = {it["repo"].lower() for it in store.load()["items"]}
    tried = tried if tried is not None else {r["repo"].lower() for r in load()["runs"] if r.get("repo")}
    langs = _known_languages()
    best, best_key = None, None
    for e in pool:
        name = (e.get("full_name") or "").lower()
        if not name or not e.get("commit") or name in researched or name in tried:
            continue
        dom = (e.get("domains") or {}).get(domain) or {}
        strength = float(dom.get("strength") or 0)
        if strength < MIN_STRENGTH or ctype not in (e.get("codebase_types_clean") or []):
            continue
        lang = (e.get("studio_language") or e.get("language") or "").lower()
        key = (strength, lang in langs, float(e.get("promise") or 0))
        if best_key is None or key > best_key:
            best, best_key = e, key
    return best


# --- running -----------------------------------------------------------------------------------------------

def running() -> dict | None:
    """The live research for a slot group, if any: this server's job, else a run another process (the CLI)
    started in the last RUN_FRESH_S that has not ended. Shaped like a jobs record."""
    from .web import jobs
    live = jobs.active(kind="slot_research")
    if live:
        return live[0]
    for r in reversed(load()["runs"]):
        if r.get("state") == "running" and _now() - float(r.get("started") or 0) < RUN_FRESH_S:
            return {"target": r.get("repo"), "params": {"group": r.get("group")}, "state": "running",
                    "progress": {"step": "researching"}}
    return None


def start(group: str, auto: bool = False, focus_style: str | None = None,
          focus_mode: str | None = None, inline: bool = False) -> dict:
    """Research one repo for a group in the background (`inline`: now, in this thread, for the CLI). Returns
    the run ({"state": "nothing"} when no repo fits) or {"error"} when a research run is already going."""
    with _start_lock:
        return _start(group, auto, focus_style, focus_mode, inline)


def _start(group: str, auto: bool, focus_style: str | None, focus_mode: str | None, inline: bool) -> dict:
    if running():
        return {"error": "a slot research run is already going"}
    from . import slot_options
    pref = slot_options.preference()
    style = focus_style if focus_style is not None else pref.get("prompt_style") or ""
    imode = focus_mode if focus_mode is not None else pref.get("interaction_mode") or ""
    t = _now()
    run = {"id": f"{group}@{int(t)}", "group": group, "repo": None, "did": None, "auto": auto, "state": "running",
           "started_at": _iso(t), "started": t, "finished_at": None, "error": None}
    entry = pick_repo(group)
    if entry is None:
        run.update(state="nothing", finished_at=_iso(t), error="no vetted repo fits this slot")
        return _record(run)
    focus = "|".join([group] + [x for x in (style, imode) if x]) if style and imode else group
    entry = {**entry, "focus": [focus]}
    run["repo"] = entry["full_name"]
    _record(run)

    if inline:
        return _research(run, entry, _PrintCtx())
    from .web import jobs
    try:
        job = jobs.submit("slot_research", entry["full_name"], lambda ctx: _research(run, entry, ctx), pool="local", priority=3, cancellable=False,
                          steps=["researching", "brief"], step="waiting", params={"group": group})
    except Exception as e:
        return _record({**run, "state": "failed", "finished_at": _iso(_now()), "error": str(e)[:300]})
    return {**run, "job": job.get("id")}


class _PrintCtx:
    """A jobs ctx for a run in the terminal."""
    def step(self, name: str, steps=None) -> None:
        print(f"[{name}]", flush=True)

    def log(self, line: str) -> None:
        print(line, flush=True)

    def add_cost(self, usd: float) -> None:
        pass


def _research(run: dict, entry: dict, ctx) -> dict:
    """Research the repo aimed at the run's group, then write its brief; records how it ended."""
    from .dossier import brief, store, worker
    ctx.step("waiting")
    did = None
    try:
        with store.generation_lock(blocking=True):
            did = worker.queue_manual(entry)
            _record({**run, "did": did})
            ctx.step("researching")
            res = worker.run_item(did, echo=ctx.log)
            ctx.add_cost(res.cost_usd or 0)
            if not res.ok:
                raise RuntimeError(res.error or "the research failed")
            ctx.step("brief")
            b = brief.generate(did, locked=True, echo=ctx.log)
            ctx.add_cost(b.get("cost_usd") or 0)
            if not b.get("ok"):
                raise RuntimeError(f"the brief failed: {b.get('error') or 'no brief'}")
    except Exception as e:
        _record({**run, "did": did, "state": "failed", "finished_at": _iso(_now()), "error": str(e)[:300]})
        raise
    _record({**run, "did": did, "state": "ready", "finished_at": _iso(_now())})
    try:
        from . import slot_options
        if slot_options.available() and not inline_run(ctx):
            slot_options.ensure()          # rate the new problems now, not when Now is next opened
    except Exception as e:  # noqa: BLE001
        ctx.log(f"starting the rating failed: {e}")
    return {**run, "did": did, "state": "ready", "repo": entry["full_name"], "dossier_id": did}


def inline_run(ctx) -> bool:
    return isinstance(ctx, _PrintCtx)


def after_rating() -> None:
    """Auto mode without anyone on Now: once a rating lands, research the next weak slot (for_now's rules)."""
    if mode() != "auto":
        return
    from . import slot_options
    from .web import data
    weak = slot_options.for_now(start=False)["weak"]
    if not weak:
        return
    busy = (data.real_snapshot().get("lane") or {}).get("state") == "working"
    for_now(weak, lane_busy=busy)


def requeue_dead() -> None:
    """Runs left "running" by a server that stopped: mark them failed (call once at startup)."""
    with _lock:
        d = load()
        changed = False
        for r in d["runs"]:
            if r.get("state") == "running":
                r.update(state="failed", finished_at=_iso(_now()), error="the server stopped during the run")
                changed = True
        if changed:
            _save(d)


# --- what Now shows ----------------------------------------------------------------------------------------

def _runs_by_group(runs: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in runs:
        out.setdefault(r["group"], []).append(r)
    return out


def due(weak: list[dict], runs: list[dict], now: float | None = None) -> dict | None:
    """The weak group auto mode researches next: outside its cooldown, under MAX_AUTO_TRIES repos researched,
    fewest open slots first (they go soonest), then groups with no option at all. A try that found no repo
    counts only for the cooldown (the scout may vet one later)."""
    now = _now() if now is None else now
    by = _runs_by_group(runs)
    ok = []
    for g in weak:
        mine = by.get(g["key"], [])
        if sum(r.get("state") != "nothing" for r in mine) >= MAX_AUTO_TRIES:
            continue
        if mine and now - float(mine[-1].get("started") or 0) < COOLDOWN_S:
            continue
        ok.append(g)
    ok.sort(key=lambda g: (g["open"], g.get("best") is not None, g.get("best") or 0, g["key"]))
    return ok[0] if ok else None


def for_now(weak: list[dict], lane_busy: bool = False, start_auto: bool = True) -> dict:
    """{"mode", "running": {group, repo, step, state} or None, "weak": the weak groups with their last run,
    "lane_busy", "started": the repo this call started}. In auto
    mode, starts research for the group `due` picks when nothing runs and the lane is not working."""
    m = mode()
    job = running()
    runs = load()["runs"]
    started = None
    if (start_auto and m == "auto" and job is None and not lane_busy and weak
            and (not config.FAKE or config.CLAUDE_BIN_SET)):
        g = due(weak, runs)
        if g is not None:
            started = start(g["key"], auto=True)
            job = running()
            runs = load()["runs"]
    by = _runs_by_group(runs)
    live = None
    if job:
        live = {"group": (job.get("params") or {}).get("group"), "repo": job.get("target"),
                "step": (job.get("progress") or {}).get("step"), "state": job.get("state")}
    out = []
    for g in weak:
        mine = by.get(g["key"], [])
        last = mine[-1] if mine else None
        out.append({**g, "tries": sum(r.get("state") != "nothing" for r in mine),
                    "last": {k: last.get(k) for k in ("repo", "state", "finished_at", "error", "auto")}
                    if last else None})
    return {"mode": m, "running": live, "weak": out, "lane_busy": lane_busy,
            "started": started.get("repo") if started and started.get("state") == "running" else None}
