"""The ranked pool of repos to write dossiers about.

`ranked(limit)` uses `johto.scout.pool.ranked_pool` when it exists and works, else ranks the vetted
records in data/scout/repos.json itself (ok verdict for some codebase type, no AI-authorship signals).
Both return entries shaped like the spec's pool rows:
{"full_name", "commit", "pinned_url", "language", "promise", "reasons", "fits"}.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys

from .. import config

LANE_LANGS = {"go", "typescript", "javascript", "python"}
STATEFUL = re.compile(r"database|postgres|sqlite|mysql|sync|mail|calendar|gallery|photo|scrobbl|chat|"
                      r"task|workflow|queue|job|schedul|multi-?user|self-?host|server|media|library|"
                      r"inventory|finance|budget|notes|wiki|forum|crm|erp|monitor|backup", re.I)


def _repos_file():
    return config.DATA / "scout" / "repos.json"


def _slots_file():
    return config.DATA / "slots.json"


def records() -> dict[str, dict]:
    try:
        return json.loads(_repos_file().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def record(full_name: str) -> dict | None:
    recs = records()
    key = full_name.lower()
    if key in recs:
        return recs[key]
    return next((r for r in recs.values() if r.get("full_name", "").lower() == key
                 or r.get("display_name", "").lower() == key), None)


def open_combos() -> dict[tuple, int]:
    """(action, domain, codebase_type) -> open slot count, from data/slots.json."""
    try:
        groups = json.loads(_slots_file().read_text())["groups"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return {}
    out: dict[tuple, int] = {}
    for g in groups:
        s = g["slot"]
        k = (s["action"], s["domain"], s["codebase_type"])
        out[k] = out.get(k, 0) + int(g.get("count") or 0)
    return out


def fits_for(ctypes: list[str], combos: dict[tuple, int] | None = None, top: int = 12) -> list[dict]:
    combos = open_combos() if combos is None else combos
    rows = [{"action": a, "domain": d, "codebase_type": c, "open": n}
            for (a, d, c), n in combos.items() if c in ctypes and n > 0]
    rows.sort(key=lambda r: -r["open"])
    return rows[:top]


def ok_types(rec: dict) -> list[str]:
    return [k for k, v in (rec.get("verdicts") or {}).items() if v.get("overall") == "ok"]


def entry_from_record(rec: dict, combos: dict[tuple, int] | None = None) -> dict:
    """A pool-shaped entry for one vetted record, scored by the spec's promise rules."""
    combos = open_combos() if combos is None else combos
    ctypes = ok_types(rec)
    c = rec.get("counts") or {}
    src, tests = c.get("source_files") or 0, c.get("test_files") or 0
    lang = rec.get("studio_language") or rec.get("language") or "?"
    reasons, score = [], 0.0

    open_by_type = {t: sum(n for (a, d, ct), n in combos.items() if ct == t) for t in ctypes}
    total_open = sum(combos.values()) or 1
    slot_w = sum(open_by_type.values()) / total_open
    score += 0.20 * min(1.0, slot_w * 1.6)
    for t in ctypes:
        reasons.append(f"passes the {t} check ({open_by_type[t]:,} open slots)")

    if lang.lower() in LANE_LANGS:
        score += 0.18
        reasons.append(f"{lang}, which the lane image supports")
    else:
        score += 0.05
    if 150 <= src <= 3000:
        score += 0.16
        reasons.append(f"{src:,} source files")
    elif src > 3000:
        score += 0.12
        reasons.append(f"{src:,} source files (large)")
    elif src >= 80:
        score += 0.07
    if tests >= 20:
        score += 0.10
        reasons.append(f"{tests:,} test files")
    elif tests > 0:
        score += 0.05
    if rec.get("docker"):
        score += 0.08
        reasons.append("runs locally with Docker")
    pushed = (rec.get("pushed_at") or "")[:10]
    try:
        days = (dt.date.today() - dt.date.fromisoformat(pushed)).days
    except ValueError:
        days = 9999
    if days <= 183:
        score += 0.08
        reasons.append(f"pushed {pushed}")
    issues = rec.get("open_issues") or 0
    if issues:
        score += 0.06 * min(1.0, issues / 60)
        if issues >= 20:
            reasons.append(f"{issues} open issues")
    blob = " ".join([rec.get("description") or "", " ".join(rec.get("topics") or [])])
    if STATEFUL.search(blob):
        score += 0.14
        reasons.append("stateful app (" + STATEFUL.search(blob).group(0).lower() + ")")
    used = rec.get("studio_usage") or 0
    if used:
        score -= 0.05 * used
        reasons.append(f"used in Studio {used}x")
    return {
        "full_name": rec["full_name"].lower(), "display_name": rec.get("display_name") or rec["full_name"],
        "commit": rec["commit"],
        "pinned_url": rec.get("pinned_url") or f"https://github.com/{rec['full_name']}/tree/{rec['commit']}",
        "language": lang, "promise": round(max(0.0, min(1.0, score)), 3), "reasons": reasons[:6],
        "fits": fits_for(ctypes, combos),
    }


def fallback(limit: int = 40) -> list[dict]:
    combos = open_combos()
    out = []
    for rec in records().values():
        if (rec.get("ai") or {}).get("level") != "none":
            continue
        if rec.get("fork") or rec.get("swe_bench") or rec.get("archived"):
            continue
        if (rec.get("studio_usage") or 0) > 2:
            continue
        ctypes = ok_types(rec)
        if not ctypes or not any(open_combos_for(t, combos) for t in ctypes):
            continue
        if not rec.get("commit"):
            continue
        out.append(entry_from_record(rec, combos))
    out.sort(key=lambda e: -e["promise"])
    return out[:limit]


def open_combos_for(ctype: str, combos: dict[tuple, int]) -> int:
    return sum(n for (a, d, c), n in combos.items() if c == ctype)


def ranked(limit: int = 40) -> list[dict]:
    """The pool, best first. Prefers johto.scout.pool; falls back to repos.json."""
    try:
        from ..scout import pool  # written by the scout agent; may not exist yet
        fn = getattr(pool, "ranked_pool", None)
    except Exception:
        fn = None
    if fn is not None:
        try:
            rows = fn(limit)
            if rows:
                return [r for r in rows if r.get("full_name") and r.get("commit")]
        except Exception as e:  # a half-written pool must not stop the worker
            print(f"dossier: johto.scout.pool.ranked_pool failed ({e}); using repos.json", file=sys.stderr)
    return fallback(limit)


def find(full_name: str) -> dict | None:
    """A pool entry for one repo: from the ranked pool if it is there, else built from its record."""
    key = full_name.lower()
    for e in ranked(200):
        if e["full_name"].lower() == key:
            return e
    rec = record(full_name)
    return entry_from_record(rec) if rec and rec.get("commit") else None
