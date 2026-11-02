"""Precedent: how Mercor's reviewers judged tasks of each type, from every writer's accepted and rejected tasks.

data/precedent.json caches, per Studio task that is Accepted or Rejected, the reviewer's feedback, the opening
prompt and (for Operate) the initial state, read through the Studio worker. data/studio_tasks.json (johto sync)
says which tasks to read; a task is read again only when its Studio updated_at changes.

for_action(action) is what the task understanding page shows: the counts for that type and each judged task with
its reviewer feedback. The texts are human-written (writers and reviewers), shown as they are. Nothing here
writes Austin's fields.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import config

SCHEMA_ID = "johto.precedent/1"
STALE_S = 6 * 3600
JUDGED = ("Accepted", "Rejected")
FIELDS = {"feedback": "field_review_writer_feedback", "initial_state": "field_initial_state",
          "prompt": "field_opening_prompt"}


def path() -> Path:
    return config.DATA / "precedent.json"


def _tasks_file() -> Path:
    return config.DATA / "studio_tasks.json"


def load() -> dict:
    try:
        d = json.loads(path().read_text())
    except (OSError, ValueError):
        return {"schema": SCHEMA_ID, "at": None, "tasks": {}}
    return d if isinstance(d, dict) and isinstance(d.get("tasks"), dict) else {"schema": SCHEMA_ID, "at": None,
                                                                               "tasks": {}}


def _studio_tasks() -> list[dict]:
    try:
        t = json.loads(_tasks_file().read_text())
    except (OSError, ValueError):
        return []
    return [x for x in t if isinstance(x, dict)] if isinstance(t, list) else []


def _value(v):
    if isinstance(v, dict):
        v = v.get("value", v.get("text"))
    return v if isinstance(v, str) and v.strip() else None


def _short(name: str | None) -> str:
    return (name or "").split()[-1] if name else ""


def age_s() -> float | None:
    at = load().get("at")
    if not at:
        return None
    try:
        return time.time() - time.mktime(time.strptime(at[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def stale() -> bool:
    a = age_s()
    return a is None or a > STALE_S


def refresh(api=None, log=None) -> dict:
    """Read every judged task whose Studio record changed since the last read. {read, kept, failed}."""
    if api is None:
        from . import studio as api
    cur = load()
    cached = cur.get("tasks") or {}
    out: dict[str, dict] = {}
    read = kept = failed = 0
    for t in _studio_tasks():
        if t.get("status") not in JUDGED:
            continue
        tid = t.get("task_id")
        old = cached.get(tid)
        if old and old.get("updated_at") == t.get("updated_at") and old.get("status") == t.get("status"):
            out[tid] = old
            kept += 1
            continue
        try:
            full = api.task(tid)
        except Exception as e:  # noqa: BLE001  (one unreadable task never blanks the rest)
            failed += 1
            if log:
                log(f"{t.get('task_name')}: {e}")
            if old:
                out[tid] = old
            continue
        cf = (full or {}).get("custom_fields") or {}
        rec = {"task_id": tid, "short": _short(t.get("task_name")), "status": t.get("status"),
               "slot": t.get("slot") or {}, "repo": t.get("repo"), "updated_at": t.get("updated_at")}
        for k, f in FIELDS.items():
            rec[k] = _value(cf.get(f))
        out[tid] = rec
        read += 1
    doc = {"schema": SCHEMA_ID, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tasks": out}
    p = path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    os.replace(tmp, p)
    return {"read": read, "kept": kept, "failed": failed}


def _mine() -> set[str]:
    return {p.name for p in config.TASKS.iterdir() if (p / "task.json").exists()} if config.TASKS.exists() else set()


def for_action(action: str, *, exclude: str | None = None) -> dict:
    """{at, counts: {status: n}, judged: [{short, status, repo, slot, prompt, initial_state, feedback, mine,
    updated_at}]} for one task type, accepted first, newest first within each."""
    counts: dict[str, int] = {}
    for t in _studio_tasks():
        if (t.get("slot") or {}).get("action") == action:
            counts[t.get("status") or "?"] = counts.get(t.get("status") or "?", 0) + 1
    cur = load()
    mine = _mine()
    judged = []
    for rec in (cur.get("tasks") or {}).values():
        if (rec.get("slot") or {}).get("action") != action or rec.get("short") == exclude:
            continue
        judged.append({**{k: rec.get(k) for k in ("short", "status", "repo", "slot", "prompt", "initial_state",
                                                    "feedback", "updated_at")}, "mine": rec.get("short") in mine})
    judged.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    judged.sort(key=lambda x: 0 if x.get("status") == "Accepted" else 1)
    return {"at": cur.get("at"), "counts": counts, "judged": judged}


def ensure() -> dict | None:
    """Start a background refresh through the Studio worker when the cache is older than STALE_S (web only)."""
    if config.FAKE or not stale():
        return None
    from .web import jobs
    live = jobs.active(kind="precedent")
    if live:
        return live[0]

    def work(ctx):
        from . import actions
        return refresh(api=actions.S(), log=ctx.log)
    return jobs.submit("precedent", "Studio", work, pool="studio", priority=2, steps=["reading"], step="waiting")
