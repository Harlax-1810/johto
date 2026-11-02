"""Stage events for a task: one JSON line per event in tasks/<short>/events.jsonl.

Every part of johto that moves a task forward logs here, so `johto where` can say what changed since
Austin's last sitting and `johto status` can show his minutes per task and stage.

Kinds in use (add new ones freely; readers ignore kinds they don't know):
  claimed, prompt_qc, lane_started, turn_ended, claude_waiting, budget_nudge, pack_built, review_sent,
  review_unconfirmed, review_send_cleared, review_expired, review_saved, collected, kit_written,
  grading_written, grading_marked, verify_done, draft_checked, studio_pulled, studio_pushed, submitted,
  focus_start, focus_stop, touch (Austin looked at the task: `johto where <t>`, the web card's first read and
  the helper-chat skill log this)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

FILE = "events.jsonl"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def log(task_dir: Path, kind: str, **fields) -> dict:
    """Append one event. Never raises: logging must not break the command that called it."""
    ev = {"at": now_iso(), "kind": kind, **{k: v for k, v in fields.items() if v is not None}}
    try:
        p = Path(task_dir) / FILE
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass
    return ev


def read(task_dir: Path, kinds: tuple[str, ...] | None = None) -> list[dict]:
    p = Path(task_dir) / FILE
    out: list[dict] = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return out
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if kinds is None or ev.get("kind") in kinds:
            out.append(ev)
    return out


def last(task_dir: Path, kind: str, **match) -> dict | None:
    for ev in reversed(read(task_dir, (kind,))):
        if all(ev.get(k) == v for k, v in match.items()):
            return ev
    return None


def touch(task_dir: Path, source: str = "cli") -> dict:
    return log(task_dir, "touch", source=source)
