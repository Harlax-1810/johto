"""Match leads to open Studio slots and rank them."""
from __future__ import annotations

import math
import re

from .. import slots
from . import discover, leads, vet

RISK_SCORE = {"low": 3.0, "medium": 1.0, "high": -2.0}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9_]{3,}", (text or "").lower()) if w not in leads.STOP}


def originality(lead: dict, studio_tasks: list[dict]) -> tuple[float, str | None]:
    """Highest token overlap between the lead and an existing Studio prompt on the same repo."""
    mine = _tokens(" ".join([lead["title"], lead.get("mechanism", ""), *lead.get("facts", [])]))
    best, best_task = 0.0, None
    for t in studio_tasks:
        if t.get("repo") != lead["repo"] or not t.get("opening_prompt"):
            continue
        other = _tokens(t["opening_prompt"])
        if not other or not mine:
            continue
        ov = len(mine & other) / min(len(mine), len(other))
        if ov > best:
            best, best_task = ov, t["task_name"]
    return best, best_task


def matching_groups(lead: dict, groups: list[dict]) -> list[dict]:
    f = lead["slot_fit"]
    return [g for g in groups if g["slot"]["action"] == f["action"] and g["slot"]["domain"] == f["domain"]
            and g["slot"]["codebase_type"] == f["codebase_type"] and g["count"] > 0]


def rank(slot_filter: dict | None = None, include_dropped: bool = False) -> list[dict]:
    groups = slots.load()["groups"]
    repos = vet.load_repos()
    tasks = slots.studio_tasks()
    usage = slots.repo_usage()
    out = []
    for l in leads.load():
        if not include_dropped and l.get("status") in ("dropped", "used"):
            continue
        if slot_filter and any(slot_filter.get(k) and l["slot_fit"][k] != slot_filter[k]
                               for k in ("action", "domain", "codebase_type")):
            continue
        gs = matching_groups(l, groups)
        open_count = sum(g["count"] for g in gs)
        rec = repos.get(l["repo"], {})
        ctype = l["slot_fit"]["codebase_type"]
        v = (rec.get("verdicts") or {}).get(ctype, {})
        s = 1.5 * math.log1p(open_count) if open_count else -10
        s += {"ok": 3, "maybe": 1, "reject": -5}.get(v.get("overall"), 0)
        n_used = usage.get(l["repo"], 0)
        s += 1 if n_used == 0 else (0 if n_used < 3 else -1.5)
        s += RISK_SCORE.get((l.get("one_shot_risk") or {}).get("level"), 0)
        if (l.get("upstream") or {}).get("looks_solved"):
            s -= 6
        if rec.get("language") in discover.PREFERRED_LANGS:
            s += 1
        ov, ov_task = originality(l, tasks)
        if ov > 0.4:
            s -= 3
        if l["source"] == "agent":
            s += 0.5
        if l.get("live_services"):
            s -= 1.5
        out.append({"score": round(s, 2), "lead": l, "groups": gs, "open": open_count, "repo": rec,
                    "verdict": v, "studio_usage": n_used, "overlap": (round(ov, 2), ov_task)})
    out.sort(key=lambda x: -x["score"])
    return out
