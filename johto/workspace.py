"""Local task folders: tasks/<short-name>/ with task.json, brief.md, submission.md and runs/."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import threading
import time
from pathlib import Path

from . import config

# submission.md heading -> Studio field id. Austin writes every one of these himself.
SECTIONS = {
    "Opening Prompt": "field_opening_prompt",
    "High-Level Task Goal": "field_high_level_goal",
    "Decision Points": "field_rationale_difficulty",
    "Difficulty & Codebase Understanding": "field_codebase_overview",
    "Initial State / Setup": "field_initial_state",
    "Validation Notes": "field_self_check",
    "Notes for the Reviewer": "field_writer_submission_notes",
}

STYLE_HINTS = {
    "Casual prose": "Slack-message style, about 20-60 words. No greeting, 'please', lists or form labels. "
                    "At most one checkable clause. Describe the need or symptom, not the fix.",
    "Explicit spec": "Written ticket, about 450-1,200 words: summary line, context, numbered requirements "
                     "(not steps), file paths, and a complete 'done when' list. Clean spelling, not all lowercase.",
    "Error / artifact brief": "Paste a real error/log/stack trace from the pinned commit, then one short casual line. "
                              "No criteria, no diagnosis.",
}

ACTION_HINTS = {
    "Create": "Build something new (feature, endpoint, service, small app).",
    "Modify": "Change existing code: bug fix, refactor, migration, upgrade.",
    "Operate": "Deploy, run, configure or diagnose a running system (CI, infra, compose). "
               "If the core ask is an app code fix, it's Modify, not Operate.",
    "Understand / Assess": "Read and judge only: review, audit, explain, plan. No code changes.",
}

TEMPLATE = """<!--
Write every section yourself, in your own words. Johto runs AI detection on all of them,
and reviewers reject LLM-written text. HTML comments like this one are ignored.
Slot: {slot_line}
-->

## Opening Prompt
<!-- {style}: {style_hint} -->


## High-Level Task Goal
<!-- 1-3 sentences, more specific than the prompt. A checkable outcome, not a method.
Name the shortcuts that don't count. Add the context a reviewer new to this repo needs. -->


## Decision Points
<!-- 2-3 points specific to this repo, each with what a better and a worse model would do.
e.g. "Better: ... Worse: ..." Tie each to scope, verification, honesty, root cause,
clarifying vs guessing, or repo conventions. -->


## Difficulty & Codebase Understanding
<!-- 3-5 sentences: what the app does, stack and rough size, the files/flows this touches,
and the concrete mechanism that makes it hard. Disclose related upstream PRs/issues. -->

{initial_state}
## Validation
<!-- Fill after the validation run (a fresh lane, not the calibration one). -->
One-shot:
Task Success:
Steering:
Claude minutes: auto

## Validation Notes
<!-- What happened in the validation run and why you gave these ratings: did it solve it,
where it struggled or went wrong, how much you steered. -->


## Notes for the Reviewer
<!-- Optional. -->

"""

INITIAL_STATE = """## Initial State / Setup
<!-- Operate tasks: how to reach the starting state (e.g. docker compose up, then run a seed script). -->

"""


def short_name(task_name: str) -> str:
    return task_name.replace("Task ", "").strip()


def task_dir(name: str) -> Path:
    return config.TASKS / short_name(name)


def resolve(name_or_short: str) -> Path:
    """Find a local task folder by short name, 'Task xxx', or task id."""
    s = name_or_short.strip()
    d = task_dir(s)
    if d.exists():
        return d
    for p in config.TASKS.glob("*/task.json"):
        meta = json.loads(p.read_text())
        if s in (meta.get("task_id"), meta.get("task_name"), short_name(meta.get("task_name", ""))):
            return p.parent
    raise FileNotFoundError(f"No local task folder for {s!r} under {config.TASKS}")


def load(d: Path) -> dict:
    return json.loads((d / "task.json").read_text())


def save(d: Path, meta: dict) -> None:
    """Write task.json atomically (a temp file, then rename), so a reader never sees half a file."""
    p = Path(d) / "task.json"
    tmp = p.with_name(f".task.json.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(meta, indent=1))
    os.replace(tmp, p)


@contextlib.contextmanager
def locked(d: Path):
    """Exclusive lock on the task folder (d/.lock) around a read-modify-write of task.json. Shared by the CLI,
    the web actions, the lane service and the lane watcher (each looks for workspace.locked)."""
    with open(Path(d) / ".lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def create(task: dict, slot: dict, repo: dict, lead: dict | None) -> Path:
    """Make tasks/<short>/ with task.json, brief.md and a blank submission.md."""
    d = task_dir(task["task_name"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "runs").mkdir(exist_ok=True)
    meta = {
        "task_id": task["task_id"],
        "task_name": task["task_name"],
        "slot": slot,
        "repo": repo,  # {full_name, url, commit, pinned_url, language}
        "lead_id": (lead or {}).get("id"),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "runs": [],
        "gists": {},
    }
    if (d / "task.json").exists():
        # A retried claim of the same task: keep everything the folder already records (runs, gists, pushed,
        # qc, decisions, submission_run, label); only fill in what it lacks.
        old = load(d)
        meta = {**meta, **old, "runs": old.get("runs", []), "gists": old.get("gists", {})}
    save(d, meta)
    if not (d / "brief.md").exists():
        (d / "brief.md").write_text(render_brief(meta, lead))
    sub = d / "submission.md"
    if not sub.exists():
        slot_line = " | ".join(str(slot[k]) for k in
                               ("action", "domain", "codebase_type", "prompt_style", "interaction_mode"))
        sub.write_text(TEMPLATE.format(
            slot_line=slot_line, style=slot["prompt_style"],
            style_hint=STYLE_HINTS.get(slot["prompt_style"], ""),
            initial_state=INITIAL_STATE if slot["action"] == "Operate" else ""))
    return d


def render_brief(meta: dict, lead: dict | None) -> str:
    s, r = meta["slot"], meta["repo"]
    lines = [
        f"# {meta['task_name']} brief",
        "",
        "Research notes for you only. Nothing here goes into Studio.",
        "",
        f"- Studio: {config.STUDIO}/annotator/tasks/{meta['task_id']}/?campaignId={config.CAMPAIGN_ID}",
        f"- Repo: {r.get('pinned_url')}",
        f"- Language: {r.get('language')}",
        "",
        "## Slot",
        f"- Task type: **{s['action']}**: {ACTION_HINTS.get(s['action'], '')}",
        f"- Domain: {s['domain']}",
        f"- Codebase: {s['codebase_type']}",
        f"- Prompt style: **{s['prompt_style']}**: {STYLE_HINTS.get(s['prompt_style'], '')}",
        f"- Mode: {s['interaction_mode']}"
        + (" (paste once, no follow-ups)" if s["interaction_mode"].startswith("Async")
           else " (reply like a colleague; no hints, no new requirements)"),
        "",
        "## Passing difficulty",
        "- One-shot = Yes means drop it (log it in the Discarded Run Log).",
        "- Enterprise: Task Success 1-2 passes; 3 needs Steering 1-2; 4 or 5 rejected.",
        "- Personal-scale: Task Success 1-3 passes; 4 needs Steering 1-2; 5 rejected.",
        "- Reviewers rerun with Opus 5.5 high. Tasks it finishes in ~5 minutes get rejected.",
    ]
    if lead:
        lines += ["", "## Lead", f"**{lead.get('title')}**", ""]
        lines += [f"- {f}" for f in lead.get("facts", [])]
        if lead.get("files"):
            lines += ["", "Files: " + ", ".join(f"`{f}`" for f in lead["files"])]
        for k, label in (("mechanism", "Why it's hard"), ("verify", "How to verify")):
            if lead.get(k):
                lines += [f"{label}: {lead[k]}"]
        risk = lead.get("one_shot_risk") or {}
        if risk:
            lines += [f"One-shot risk: {risk.get('level')} ({risk.get('why')})"]
        up = lead.get("upstream") or {}
        if up.get("related"):
            lines += ["", "Related upstream work (disclose in Difficulty or reviewer notes):"]
            lines += [f"- [{x.get('state')}] {x.get('title')} {x.get('url')}" for x in up["related"]]
        if lead.get("issue_url"):
            lines += [f"Issue: {lead['issue_url']}"]
    return "\n".join(lines) + "\n"


_COMMENT = re.compile(r"<!--.*?-->", re.S)


def parse_submission(d: Path) -> dict:
    """Read submission.md into {heading: text} plus parsed ratings."""
    text = _COMMENT.sub("", (d / "submission.md").read_text())
    parts = re.split(r"^## +(.+?)\s*$", text, flags=re.M)
    sections = {}
    for i in range(1, len(parts), 2):
        sections[parts[i].strip()] = parts[i + 1].strip()
    ratings = {}
    for line in sections.pop("Validation", "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            ratings[k.strip().lower()] = v.strip()
    return {"sections": sections, "ratings": ratings}


def _num(v: str | None) -> int | None:
    m = re.match(r"\s*(\d)", v or "")
    return int(m.group(1)) if m else None


_RUN_MINUTES: dict[tuple, float | None] = {}


def run_minutes(rec: dict | None, d: Path | None = None) -> float | None:
    """Claude's working minutes for a run record in task.json. Runs collected before working minutes existed
    (2026-10-31) stored the older, larger measure, so their transcript is read again (looked for in the task
    folder's own run first). None when the run has no minutes yet."""
    rec = rec or {}
    if rec.get("working_minutes") is not None:
        return rec["working_minutes"]
    stored = Path(rec["transcript"]) if rec.get("transcript") else None
    if stored is not None:
        here = Path(d) / rec["dir"] / "prework_output" / stored.name if d is not None and rec.get("dir") else None
        p = here if here is not None and here.exists() else stored
        try:
            st = p.stat()
        except OSError:
            st = None
        if st is not None:
            key = (str(p), st.st_mtime_ns, st.st_size)
            if key not in _RUN_MINUTES:
                from . import transcript
                try:
                    _RUN_MINUTES[key] = transcript.summarize(p).get("working_minutes")
                except Exception:   # an unreadable transcript keeps the stored number
                    _RUN_MINUTES[key] = None
            if _RUN_MINUTES[key] is not None:
                return _RUN_MINUTES[key]
    return rec.get("active_minutes")


def to_fields(meta: dict, sub: dict, claude_minutes: float | None = None) -> dict:
    """Studio custom_fields from the task folder. Empty values are left out."""
    f: dict = {}
    r = meta["repo"]
    f["field_repo_name"] = r.get("full_name")
    f["field_repo_url"] = r.get("pinned_url")
    lang = r.get("language")
    if lang in config.LANGUAGES:
        f["field_primary_language"] = lang
    elif lang:
        f["field_primary_language"], f["field_primary_language_other"] = "Other", lang
    for heading, field in SECTIONS.items():
        v = sub["sections"].get(heading, "").strip()
        if v:
            f[field] = v
    rt = sub["ratings"]
    one = (rt.get("one-shot") or "").strip().capitalize()
    if one in ("Yes", "No"):
        f["field_one_shot"] = one
    ts, st = _num(rt.get("task success")), _num(rt.get("steering"))
    if ts in config.TASK_SUCCESS:
        f["field_task_success"] = config.TASK_SUCCESS[ts]
    if st in config.STEERING:
        f["field_steering"] = config.STEERING[st]
    mins = rt.get("claude minutes", "auto")
    if mins and mins != "auto" and re.fullmatch(r"\d+(\.\d+)?", mins):
        f["field_claude_minutes"] = round(float(mins))
    elif claude_minutes is not None:
        f["field_claude_minutes"] = round(claude_minutes)
    g = meta.get("gists", {})
    if g.get("conversation_log"):
        f["field_conversation_log"] = g["conversation_log"]
    if g.get("final_output"):
        f["field_final_output"] = g["final_output"]
    return {k: v for k, v in f.items() if v not in (None, "")}
