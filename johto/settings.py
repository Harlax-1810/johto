"""Austin's johto settings: data/settings.json, merged over these defaults. Missing file = defaults."""
from __future__ import annotations

import json

from . import config

DEFAULTS = {
    # Send each finished lane turn's pack to GPT Pro automatically (johto watch).
    "auto_review": True,
    # Only auto-review turns where Claude worked at least this long (short clarifying turns are skipped).
    "review_min_working_minutes": 3,
    # Claude-minutes nudges per run (macOS notification once per threshold).
    "budget_nudges": [45, 75, 90],
    # Chrome profile whose ChatGPT has Pro. Studio runs in the same profile.
    "chatgpt_account": config.BROWSER_ACCOUNT,
    # Note in the review notification when ChatGPT's weekly usage banner shows less than this percent left. None:
    # never (Austin, 2026-11-01: reviews always go out; the ChatGPT app's usage isn't a limit for him).
    "chatgpt_warn_usage_pct": None,
    # Which finished turns johto watch sends for review: "meaningful" (async: each substantive turn; interactive:
    # the first substantive turn, then only when the diff changed) or "every_turn".
    "review_policy": "meaningful",
    # Add the outside Claude review (a paid headless run) to review packs. Off: GPT Pro reads the run first.
    "pack_outside_review": False,
    # Research for an open slot group of the kind he prefers that has no option rated GREAT_AT or better
    # (johto/slot_research.py): "auto" starts it by itself, "manual" offers a button on Now.
    "slot_research": "auto",
    # What Austin knows, for the slot rating's "edge" (how much faster he grasps a problem's hard part than an
    # equally experienced developer with a different background). His Johto tasks are added by the rating.
    "slot_background": "",
}


def path():
    return config.DATA / "settings.json"


def load() -> dict:
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(path().read_text()))
    except (FileNotFoundError, ValueError):
        pass
    return s


def get(key: str):
    return load().get(key, DEFAULTS.get(key))


def save(patch: dict) -> dict:
    """Merge patch into data/settings.json (a key set to None goes back to its default). Returns the merged
    settings."""
    import os
    p = path()
    try:
        cur = json.loads(p.read_text())
    except (FileNotFoundError, ValueError):
        cur = {}
    for k, v in patch.items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, p)
    return load()
