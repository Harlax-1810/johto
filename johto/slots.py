"""Open slots in Studio, grouped by their seeded combination."""
from __future__ import annotations

import collections
import json
import re
import time

from . import config, studio



def slots_file():
    """data/slots.json, read from config at call time so a patched config.DATA (tests) is honoured."""
    return config.DATA / "slots.json"


def tasks_file():
    return config.DATA / "studio_tasks.json"


def slot_key(slot: dict) -> str:
    return " | ".join(slot[k] or "?" for k in
                      ("action", "domain", "codebase_type", "prompt_style", "interaction_mode"))


def _compact(t: dict, api=None) -> dict:
    api = api or studio
    cf = t.get("custom_fields") or {}
    url = cf.get("field_repo_url") or ""
    m = re.match(r"https://github\.com/([^/]+/[^/]+)", url)
    return {
        "task_id": t["task_id"],
        "task_name": t.get("task_name"),
        "status": api.status_name(t),
        "owned_by": t.get("owned_by"),
        "slot": api.slot_of(t),
        "repo": m.group(1).lower() if m else None,
        "repo_url": url or None,
        "opening_prompt": cf.get("field_opening_prompt"),
        "updated_at": t.get("updated_at"),
    }


def sync(api=None) -> dict:
    """Pull every task in the world; write the open-slot groups and a compact task index. `api` is the Studio
    module to use (the web app passes its queued proxy, or the fake); default johto.studio."""
    api = api or studio
    tasks = [_compact(t, api) for t in api.list_tasks()]
    config.DATA.mkdir(parents=True, exist_ok=True)
    tasks_file().write_text(json.dumps(tasks, indent=1))
    groups: dict[str, dict] = {}
    for t in tasks:
        if t["status"] != "Ready to Claim":
            continue
        g = groups.setdefault(slot_key(t["slot"]), {"slot": t["slot"], "count": 0, "tasks": []})
        g["count"] += 1
        g["tasks"].append({"task_id": t["task_id"], "task_name": t["task_name"]})
    out = {"synced_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "groups": sorted(groups.values(), key=lambda g: -g["count"])}
    slots_file().write_text(json.dumps(out, indent=1))
    return out


def load(max_age_s: float | None = None) -> dict:
    f = slots_file()
    if not f.exists() or (max_age_s is not None and time.time() - f.stat().st_mtime > max_age_s):
        return sync()
    return json.loads(f.read_text())


def studio_tasks() -> list[dict]:
    if not tasks_file().exists():
        sync()
    return json.loads(tasks_file().read_text())


def repo_usage() -> collections.Counter:
    """How many Studio tasks (any writer, any status) already use each repo."""
    return collections.Counter(t["repo"] for t in studio_tasks() if t["repo"])


def pick_task_for(slot: dict) -> dict | None:
    """First open task id whose seeds equal this slot."""
    key = slot_key(slot)
    for g in load()["groups"]:
        if slot_key(g["slot"]) == key and g["tasks"]:
            return g["tasks"][0]
    return None
