"""Fake lanes for JOHTO_FAKE_STUDIO=1: the same functions as johto/lanes.py, with no Docker at all.

new_lane() makes the run folder and a manifest with a made-up container name; collect() reads what is
already in runs/<run>/prework_output/ (tests copy a transcript and a diff there, standing in for
Claude); teardown() records the call and removes nothing.
"""
from __future__ import annotations

import json
import re
import os
import shutil
import time
import uuid
from pathlib import Path

from .. import lanes as real, transcript, workspace
from . import guard_paths

KINDS = real.KINDS
TORN_DOWN: list[tuple[str, str]] = []
latest_run, diff_stat, manifest, next_run_dir = real.latest_run, real.diff_stat, real.manifest, real.next_run_dir


def running_lanes() -> list[str]:
    return []


def claude_active(container: str) -> bool:
    return False


def new_lane(task_dir: Path, kind: str, stop_others: bool = False) -> Path:
    guard_paths()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    meta = workspace.load(task_dir)
    lane_task = workspace.lane_task(meta, task_dir)
    # Mercor's prework.py takes 'Task <8 letters or digits>' or a Studio id; the fixtures' names are shorter, so this
    # checks the shape (no other words, no hyphens), which is what a draft's 'Draft d-xxxxxx' used to fail.
    if not re.fullmatch(r"(?:Task )?[a-z0-9]{1,8}|task_[0-9a-f]{32}", lane_task, re.I):
        raise RuntimeError("[ERROR] The task must look like 'Task l50u5a0c', 'l50u5a0c' or 'task_<32 hex characters>'.")
    run_dir = next_run_dir(task_dir, kind)
    (run_dir / "prework_output").mkdir(parents=True)
    container = f"prework-FAKE_{task_dir.name}-{uuid.uuid4().hex[:12]}-model_a"
    (run_dir / "prework-manifest.json").write_text(json.dumps({"lane": {"container": container}}))
    print(f"Setting up {run_dir.name} for {lane_task} (fake lane, no Docker)...")
    meta.setdefault("runs", []).append({"kind": kind, "dir": str(run_dir.relative_to(task_dir)),
                                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                        "container": container})
    workspace.save(task_dir, meta)
    return run_dir


def collect(task_dir: Path, run_name: str) -> dict:
    guard_paths()
    run_dir = task_dir / "runs" / run_name
    out = run_dir / "prework_output"
    seed = os.environ.get("JOHTO_FAKE_TRANSCRIPT")
    if seed and not transcript.find(run_dir):
        out.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(seed, out / f"{uuid.uuid4()}.jsonl")
    if not out.is_dir():
        raise RuntimeError(f"prework collect failed in {run_dir}")
    diff = out / "final.diff"
    logs = transcript.find(run_dir)
    summary = transcript.summarize(logs[-1]) if logs else None
    info = {"diff": str(diff) if diff.exists() else None, "diff_bytes": diff.stat().st_size if diff.exists() else 0,
            "transcript": str(logs[-1]) if logs else None, "summary": summary}
    meta = workspace.load(task_dir)
    for run in meta.get("runs", []):
        if run["dir"] == f"runs/{run_name}":
            run["collected_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            run["diff"], run["transcript"] = info["diff"], info["transcript"]
            if summary:
                run["active_minutes"] = summary["active_minutes"]
                run["working_minutes"] = summary.get("working_minutes", summary["active_minutes"])
                run["human_turns"] = summary["human_turns"]
    workspace.save(task_dir, meta)
    if summary:
        (run_dir / "summary.md").write_text(f"# {run_name} summary\n\n" + transcript.render(summary) + "\n")
    return info


def teardown(task_dir: Path, run_name: str) -> list[str]:
    TORN_DOWN.append((task_dir.name, run_name))
    return []
