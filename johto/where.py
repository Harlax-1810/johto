"""`johto where [task]`: one screen that says, in about 20 seconds, where a task stands.

GPT Pro's UX review, contract 3. For one task, card() answers: which task and which run (and how that run was
chosen), what changed since Austin last looked, which copy of the text is current (Studio's or the local file),
which evidence applies (reviews and the turns they cover, the grading table, the draft check), what is left to
decide, and the exact command for each next step. Without a task, overview() lists every task in Home's order,
each row carrying both what Austin owes it and what the machine is doing, plus the deck's ready picks.

Nothing here decides anything new. The stage is johto.web.domain.derive's, the next step and the order are
the Home queue's (johto.web.domain.queue), the run is actions.submission_run's, Claude's minutes come from
budget.status through the run monitor, the text versions from sync.plan, and Austin's minutes from focus.py.
Two adjustments for this view only:
- the stage reads the best copy of each field (Studio's when the local file is blank or older), so a blank or
  stale submission.md never turns into a writing assignment;
- a task outside Writing (submitted, accepted) shows Studio's text as the current one.

card() logs a "touch" stage event (Austin looked at the task) unless touch=False; "since" lists what happened
after the touch before it.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import actions, config, stages, studio, sync, workspace

MAX_SINCE = 8
REQUIRED = ("opening_prompt", "goal", "decision_points", "codebase", "validation_notes")
RATING_KEYS = {"field_one_shot": "one_shot", "field_task_success": "task_success", "field_steering": "steering"}
LOCAL_RATING = {"field_one_shot": "one-shot", "field_task_success": "task success", "field_steering": "steering"}
FIELD_WORDS = {"opening_prompt": "opening prompt", "goal": "goal", "decision_points": "decision points",
               "codebase": "codebase notes", "initial_state": "initial state", "validation_notes": "validation notes",
               "reviewer_notes": "reviewer notes", "one_shot": "One-shot rating", "task_success": "Task Success rating",
               "steering": "Steering rating"}
DONE = {"accepted", "rejected", "dropped", "discarded", "unclaimed"}


# --- small helpers -------------------------------------------------------------------------------------------

def _data():
    from .web import data
    return data


def _runmon():
    from .web.lane import runmon
    return runmon


def _dt(v) -> datetime | None:
    from .focus import _dt as parse
    return parse(v)


def clock(v) -> str:
    t = _dt(v)
    if not t:
        return ""
    t = t.astimezone()
    days = (datetime.now().astimezone().date() - t.date()).days
    return t.strftime("%H:%M") if days == 0 else t.strftime("%a %H:%M") if days < 7 else t.strftime("%b %d %H:%M")


def run_word(run: str | None) -> str:
    kind, _, n = (run or "").partition("-")
    return f"{'calibration' if kind == 'calib' else 'validation' if kind == 'validate' else kind} {n}".strip()


def _words(keys: list[str]) -> str:
    ws = [FIELD_WORDS.get(k, k) for k in keys]
    return ws[0] if len(ws) == 1 else ", ".join(ws[:-1]) + " and " + ws[-1]


def _key_of() -> dict[str, str]:
    """Studio field id -> the web app's field key (goal, decision_points, ...)."""
    out = {workspace.SECTIONS[h]: k for k, h in _data().SECTION_KEYS.items() if h in workspace.SECTIONS}
    out.update(RATING_KEYS)
    return out


def _mins(m) -> str:
    m = float(m or 0)
    return "under 1 min" if 0 < m < 0.5 else f"{round(m)} min"


# --- which copy of the text is current -----------------------------------------------------------------------

def text_state(d: Path, meta: dict, sub: dict | None = None) -> dict:
    """Studio's snapshot against submission.md: {source, studio_at, local_at, local_differs, pull, conflicts,
    previous_writer, unpulled, local_only, missing, writable, note, best}. `best` ({key: text}) is the copy each
    field should be read from; local_differs lists fields whose local text is there and differs from Studio's."""
    sub = sub if sub is not None else workspace.parse_submission(d)
    snap = actions.studio_snapshot(d) if (d / "app" / "studio-fields.json").exists() else None
    remote = (snap or {}).get("fields") or {}
    try:
        local = workspace.to_fields(meta, sub)
    except Exception:
        local = {}
    st = meta.get("studio") or {}
    status = st.get("status") or "Writing"
    writable = status == "Writing" and st.get("mine") is not False
    plan = sync.plan(local, remote, meta) if snap else {"pull": {}, "conflicts": {}, "agreed": {}, "foreign": {}}
    at_claim = meta.get("studio_at_claim") or {}
    key_of = _key_of()
    out = {"source": "studio" if snap else "local", "studio_at": (snap or {}).get("at"),
           "local_at": _mtime(d / "submission.md"), "status": status, "writable": writable,
           "local_differs": [], "pull": [], "conflicts": [], "previous_writer": [], "unpulled": [], "local_only": [],
           "missing": [], "best": {}}
    for fid, key in key_of.items():
        loc, rem = sync.norm(local.get(fid)), sync.norm(remote.get(fid))
        prev = bool(at_claim) and rem and sync.h(rem) == at_claim.get(fid) and fid not in plan["agreed"]
        if loc and rem and loc != rem:
            out["local_differs"].append(key)
        if fid in plan["pull"]:
            out["pull"].append(key)
        elif fid in plan["conflicts"]:
            out["conflicts"].append(key)
        elif prev:
            out["previous_writer"].append(key)
        elif fid in plan["foreign"]:
            out["unpulled"].append(key)       # Studio text johto can't place: the task predates the claim record
        elif loc and loc != rem and snap:
            out["local_only"].append(key)
        if not writable:
            best = rem or loc
        else:
            best = loc or ("" if prev else rem)
        out["best"][key] = best
    if not snap:
        out["source"] = "local"
    elif not writable:
        out["source"] = "studio"
    else:
        out["source"] = "studio" if not out["local_only"] and not out["conflicts"] else "local"
    need = list(REQUIRED) + (["initial_state"] if (meta.get("slot") or {}).get("action") == "Operate" else [])
    out["missing"] = [k for k in need if not out["best"].get(k)]
    out["note"] = _text_note(d.name, out)
    return out


def _mtime(p: Path) -> str | None:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).astimezone().isoformat(timespec="seconds")
    except OSError:
        return None


def _text_note(short: str, t: dict) -> str:
    if t["studio_at"] is None:
        return "Johto hasn't read Studio for this task yet, so this is the local copy."
    if not t["writable"]:
        note = f"Studio holds the text as {t['status'].lower()}; the local copy is not work to do."
        if t["local_differs"]:
            n = len(t["local_differs"])
            note += f" The local {_words(t['local_differs'])} differ{'s' if n == 1 else ''} from it."
        return note
    parts = []
    if t["conflicts"]:
        parts.append(f"{_words(t['conflicts']).capitalize()} changed in both Studio and the local copy. "
                     "Keep one version in both places.")
    if t["pull"]:
        parts.append(f"Studio has a newer {_words(t['pull'])}; `johto pull {short}` brings it into submission.md.")
    if t["previous_writer"]:
        parts.append(f"Studio's {_words(t['previous_writer'])} is a previous writer's text, not yours.")
    if t["unpulled"]:
        parts.append(f"Studio's {_words(t['unpulled'])} is the current copy (johto doesn't pull it into the local file "
                     "for tasks claimed before it recorded Studio's starting text).")
    if t["local_only"]:
        parts.append(f"Your local {_words(t['local_only'])} has edits Studio doesn't. Save to Studio so reviewers "
                     "see them.")
    return " ".join(parts[:2]) or "Studio and the local copy agree."


# --- the task's state (the web app's derive, read here) ------------------------------------------------------

def _overlay(base: dict, ts: dict) -> None:
    """Give derive the best copy of each field and rating, so a blank or older local file isn't read as unwritten."""
    data = _data()
    texts = dict(base.get("texts") or {})
    for k, v in ts["best"].items():
        if k in texts or k in data.SECTION_KEYS:
            if v and not (texts.get(k) or "").strip():
                texts[k] = v
    base["texts"] = texts
    sub = base.get("sub") or {}
    rt = dict(sub.get("ratings") or {})
    changed = False
    for fid, key in RATING_KEYS.items():
        lk = LOCAL_RATING[fid]
        if ts["best"].get(key) and not (rt.get(lk) or "").strip():
            rt[lk] = ts["best"][key]
            changed = True
    if changed:
        minutes = (base.get("ratings") or {}).get("claude_minutes")
        try:
            base["ratings"] = data._ratings_of({**sub, "ratings": rt}, base["slot"], minutes)
        except Exception:
            pass


def task_view(d: Path, *, live: bool = True, text: dict | None = None) -> tuple[dict, dict]:
    """(task state as the web app builds it, lane) for one task, with the best text overlaid. `live` lets the
    run monitor ask the PC about a PC lane (one ssh); otherwise only this Mac and the files are read."""
    data = _data()
    meta = workspace.load(d)
    if live:
        try:
            _runmon().for_task(d, meta, network=True)   # warms the monitor's cache; task_base reads it
        except Exception:
            pass
    base = data.task_base(d)
    if text is not None:
        _overlay(base, text)
    lane = data.lane_state([base])
    try:
        jobs = data.jobs_snapshot()
    except Exception:
        jobs = []
    return data.task_state(base, lane, jobs), lane


def _queue_row(t: dict, lane: dict) -> tuple[dict | None, dict | None]:
    """(Home's row for this task, its running row) from the Home queue built for this one task."""
    data = _data()
    q = data._domain().queue
    snap = {"tasks": [t], "lane": lane, "blockers": [], "leads": {}, "prefs": {"hidden": set()},
            "settings": data.settings(), "lane_service": data.has_lane_service(), "studio": data.studio_state(),
            "now": time.time()}
    items, running = q.build_queue(snap)
    row = next((i for i in items if i.get("short") == t["short"]), None)
    rr = next((r for r in running if r.get("short") == t["short"]), None)
    return row, rr


# --- the card's parts ----------------------------------------------------------------------------------------

def run_block(d: Path, meta: dict, t: dict, *, live: bool) -> dict:
    """The run the card is about: the submission run when there is one, else the run under way."""
    info = actions.submission_run_info(meta, d)
    rm = _runmon()
    run = info["run"] or rm.current_run(meta, d)
    if not run:
        return {"name": None, "selected_by": None, "submission": False, "newer": info["newer"], "line": "No runs yet."}
    mon = t.get("monitor") if (t.get("monitor") or {}).get("run") == run else None
    if mon is None:
        try:
            mon = rm.get(d, run, network=live)
        except Exception:
            mon = None
    mon = mon or {}
    rec = actions.run_record(d, run, meta) or {}
    by = info["by"] if info["run"] == run else "current"
    return {"name": run, "word": run_word(run), "host": mon.get("host") or ("PC" if rec.get("host") else "Mac"),
            "state": mon.get("state") or ("collected" if actions.run_collected(rec) else None),
            "turns": mon.get("turns"), "working_minutes": mon.get("working_minutes"),
            "background_minutes": (mon.get("background") or {}).get("minutes"), "task_minutes": mon.get("task_minutes"),
            "budget": mon.get("budget"), "line": mon.get("line") or "", "selected_by": by,
            "submission": info["run"] == run, "collected": actions.run_collected(rec),
            "newer": info["newer"], "turn_class": mon.get("turn_class"), "review": mon.get("review"),
            "resume": actions.resume_command(d, run, rec) if not rec.get("torn_down_at") else None,
            "why": mon.get("why")}


def reviews_block(d: Path, run: dict) -> dict | None:
    try:
        from . import reviews
    except Exception:
        return None
    name = run.get("name")
    try:
        latest = reviews.latest(d, name) if name else None
        latest = latest or reviews.latest(d)
        pending = [e for e in reviews.pending(d) if not name or e.get("run") == name]
    except Exception:
        return None
    rv = run.get("review") or {}
    if not latest and not pending and rv.get("state") in (None, "none"):
        return None
    text = None
    if latest:
        src = {"gptpro": "GPT Pro"}.get(latest.get("source") or "", latest.get("source") or "review")
        text = (f"{src} review of {run_word(latest.get('run'))}"
                + (f" turn {latest['turn']}" if latest.get("turn") else "")
                + (f", saved {clock(latest.get('saved_at'))}" if latest.get("saved_at") else ""))
    covers = latest.get("turn") if latest and latest.get("run") == name else None
    return {"latest": text, "latest_path": (latest or {}).get("path"), "latest_name": (latest or {}).get("name"),
            "pending": len(pending) or None, "state": rv.get("state"), "state_text": rv.get("text") or "",
            "covers_turn": covers, "run_turns": run.get("turns")}


def grading_block(d: Path) -> dict | None:
    try:
        from . import grading
    except Exception:
        return None
    try:
        g = grading.load(d)
    except Exception:
        return None
    if not g.get("rows"):
        return None
    counts = grading.counts(g) if hasattr(grading, "counts") else {
        "rows": len(g["rows"]), "selected": sum(1 for r in g["rows"] if (r.get("marks") or {}).get("selected")),
        "open_decisions": 0}
    return {**counts, "run": g.get("run"), "updated_at": g.get("updated_at")}


def check_block(d: Path) -> dict | None:
    try:
        from . import draftcheck
    except Exception:
        return None
    try:
        if hasattr(draftcheck, "summary"):
            s = draftcheck.summary(d)
            return None if s.get("latest_at") is None else s
        c = draftcheck.latest(d)
    except Exception:
        return None
    if not c:
        return None
    return {"latest_at": c.get("at"), "attention": len(c.get("attention") or []),
            "decide": len(c.get("decide") or []), "stale": None, "path": c.get("path")}


def focus_block(d: Path) -> dict | None:
    try:
        from . import focus
        s = focus.summary(d)
    except Exception:
        return None
    return {"task_minutes": s["minutes"], "sittings": s["sittings"], "method": s["method"],
            "method_text": s["method_text"], "by_stage": s["by_stage"], "open": s["open"]}


# --- what changed since the last look -------------------------------------------------------------------------

def _event_line(ev: dict) -> str | None:
    k, run = ev.get("kind"), ev.get("run")
    rw = run_word(run)
    if k == "claimed":
        return f"Claimed {ev.get('task_name') or ''}".strip()
    if k == "prompt_qc":
        res = ev.get("prompt")
        return f"Prompt AutoQC {'passed' if res == 'pass' else 'failed: ' + ', '.join(ev.get('failed') or []) if res else ev.get('status') or 'ran'}"
    if k == "lane_started":
        host = ev.get("host")
        return f"Started {rw}" + (f" on the {'PC' if host and host != 'local' else 'Mac'}" if host else "")
    if k == "turn_ended":
        wm = ev.get("working_minutes")
        return f"Claude finished turn {ev.get('turn')} of {rw}" + (f" ({_mins(wm)})" if wm is not None else "")
    if k == "budget_nudge":
        return f"{rw.capitalize()} reached {ev.get('threshold')} Claude minutes"
    if k == "pack_built":
        return f"Review pack built for {rw}" + (f" turn {ev['turn']}" if ev.get("turn") else "")
    if k == "review_sent":
        return f"Sent {rw}" + (f" turn {ev['turn']}" if ev.get("turn") else "") + " for review"
    if k == "review_saved":
        return f"Review of {rw}" + (f" turn {ev['turn']}" if ev.get("turn") else "") + " saved"
    if k == "collected":
        wm = ev.get("working_minutes")
        return f"Collected {rw}" + (f" ({_mins(wm)} of Claude work)" if wm is not None else "")
    if k == "kit_written":
        return "Writing kit updated"
    if k == "grading_written":
        return f"Grading table refreshed ({ev.get('rows')} rows)" if ev.get("rows") is not None else "Grading table refreshed"
    if k == "grading_marked":
        n = len(ev.get("rows") or [])
        return f"You marked {n} grading row{'s' if n != 1 else ''}"
    if k == "verify_done":
        return f"Verify run on row {ev.get('row')}: {ev.get('status')}"
    if k == "draft_checked":
        a = ev.get("attention")
        return "Draft checked" + (f": {a} need attention" if a else ": nothing needs attention" if a == 0 else "")
    if k == "studio_pulled":
        fs = ev.get("fields") or []
        return f"Brought in from Studio: {', '.join(fs)}" if fs else None
    if k == "studio_pushed":
        n = len(ev.get("fields") or [])
        return f"Saved {n} field{'s' if n != 1 else ''} to Studio"
    if k == "submitted":
        return "Submitted for review"
    if k in ("touch", "focus_start", "focus_stop"):
        return None
    return f"{k.replace('_', ' ').capitalize()}" + (f" ({rw})" if run else "")


# Austin's own actions: "since your last look" is for what changed without him (fields, evidence, jobs).
OWN_ACTIONS = {"grading_marked", "draft_checked"}


def since_lines(d: Path, meta: dict, after: str | None) -> list[str]:
    """What happened since the last look (the last touch event, at `after`), oldest first, as plain lines with
    their time. Repeats of one kind of event collapse into one line. Events after that touch in events.jsonl,
    plus what the files show (collects, feedback, Studio edits) after its time, for days before events existed."""
    cut = _dt(after)
    rows: list[tuple[datetime, str, str]] = []
    seen_collect = set()
    evs = stages.read(d)
    last = max((i for i, ev in enumerate(evs) if ev.get("kind") == "touch"), default=-1) if after else -1
    for ev in evs[last + 1:]:
        t = _dt(ev.get("at"))
        if t is None or ev.get("kind") in OWN_ACTIONS:
            continue
        line = _event_line(ev)
        if ev.get("kind") == "collected":
            seen_collect.add(ev.get("run"))
        if line:
            rows.append((t, ev.get("kind") or "", line))
    for r in meta.get("runs") or []:
        name = (r.get("dir") or "").removeprefix("runs/")
        t = _dt(r.get("collected_at"))
        if t and (not cut or t > cut) and name not in seen_collect:
            rows.append((t, "collected", f"Collected {run_word(name)}"))
    st = meta.get("studio") or {}
    t = _dt(st.get("feedback_at"))
    if t and (not cut or t > cut) and st.get("feedback"):
        rows.append((t, "feedback", f"Reviewer feedback in Studio ({st.get('status') or 'status unknown'})"))
    for e in _jsonl(d / "app" / "studio-history.jsonl"):
        t = _dt(e.get("at"))
        if t and (not cut or t > cut) and not e.get("first") and e.get("changed"):
            names = [sync.FIELD_NAMES.get(f, f.removeprefix("field_").replace("_", " ")) for f in e["changed"]]
            rows.append((t, "studio_edit", f"Studio changed: {', '.join(names)}"))
    rows.sort(key=lambda r: r[0])
    out: list[tuple[datetime, str, str, int]] = []
    for t, kind, line in rows:
        if out and out[-1][1] == kind and kind in ("turn_ended", "pack_built", "studio_pulled", "studio_edit",
                                                    "grading_marked", "draft_checked"):
            pt, pk, pl, n = out[-1]
            out[-1] = (t, kind, line, n + 1)
            continue
        out.append((t, kind, line, 1))
    lines = [f"{clock(t)} · {line}" + (f" (and {n - 1} more like it)" if n > 1 else "") for t, _, line, n in out]
    if len(lines) > MAX_SINCE:
        lines = [f"{len(lines) - MAX_SINCE + 1} earlier changes"] + lines[-(MAX_SINCE - 1):]
    return lines


def _jsonl(p: Path) -> list[dict]:
    try:
        text = p.read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


# --- next step and actions -------------------------------------------------------------------------------------

def _task_url(meta: dict) -> str | None:
    return studio.task_url(meta["task_id"]) if meta.get("task_id") else None


def next_step(t: dict, lane: dict, run: dict, text: dict, traj: dict | None) -> dict:
    """{text, why, command, also}: Home's step for this task, plus what blocks submitting."""
    stage = (t.get("stage") or {}).get("stage")
    if stage == "accepted":
        return {"text": "Accepted. Nothing left to do.", "why": "", "command": None, "also": None}
    if stage == "rejected":
        return {"text": "Rejected. Read the reviewer's feedback in Studio.", "why": "",
                "command": _task_url(t["meta"]), "also": None}
    try:
        row, rr = _queue_row(t, lane)
    except Exception as e:   # the queue is the web app's; a failure there leaves the rest of the card
        row, rr = None, None
        print(f"johto where: Home's queue failed for {t['short']}: {e}")
    out = {"text": "", "why": "", "command": None, "also": None}
    if row:
        out.update(text=row["title"], why=row.get("reason") or "", command=row.get("command"), also=row.get("also"))
    elif rr:
        mtext = (rr.get("machine") or {}).get("text") or rr.get("title") or ""
        out.update(text=f"Nothing for you now: {mtext[:1].lower() + mtext[1:]}" if mtext else "Nothing for you now.",
                   command=rr.get("command"))
    else:
        out["text"] = (t.get("stage") or {}).get("label") or ""
    if traj and traj.get("status") in ("missing", "other_run") and stage in (
            "rate", "ready_to_save", "saving", "qc_running", "fix_qc", "ready_to_submit"):
        what = "Studio has no Trajectory Logs upload" if traj["status"] == "missing" else \
            f"Studio's Trajectory Logs hold {' and '.join(traj.get('other') or [])}, not {traj.get('run')}"
        out["also"] = f"{what}; upload {traj.get('path') or 'the transcript'} before submitting."
    return out


def action_list(d: Path, meta: dict, t: dict, run: dict, text: dict) -> list[dict]:
    """Exact commands for what Austin may do next, most useful first."""
    short = d.name
    stage = (t.get("stage") or {}).get("stage")
    if workspace.is_draft(meta):
        n = actions.draft_open(meta)
        acts = [{"label": ("Claim a slot and fill Studio" if stage == "ready_to_save" else "Claim a slot")
                 + (f" ({n} open)" if n else " (none open now)"), "command": f"johto claim {short}"}]
    else:
        acts = [{"label": "Open Studio", "command": _task_url(meta)}]
    if stage in DONE or stage == "in_review":
        return acts
    name = run.get("name")
    rec = actions.run_record(d, name, meta) if name else None
    if name and rec and not rec.get("torn_down_at") and not actions.run_collected(rec):
        if run.get("resume"):
            acts.append({"label": f"Resume {run_word(name)}", "command": run["resume"]})
        acts.append({"label": f"Collect {run_word(name)}", "command": f"johto collect {short} {name}"})
    for n in run.get("newer") or []:
        if n != name:
            acts.append({"label": f"Collect {run_word(n)}", "command": f"johto collect {short} {n}"})
    if text.get("pull"):
        acts.append({"label": "Bring Studio's edits in", "command": f"johto pull {short}"})
    if run.get("submission") and run.get("collected"):
        acts.append({"label": "Check the draft", "command": f"johto check {short}"})
        acts.append({"label": "Grading table", "command": f"johto grading {short}"})
        acts.append({"label": "Writing kit", "command": f"johto kit {short}"})
    rv = run.get("review") or {}
    if name and run.get("turns") and rv.get("state") in (None, "none", "ready", "queued") and not rv.get("covers_latest"):
        acts.append({"label": f"Send {run_word(name)} for review", "command": f"johto review send {short} {name}"})
    if name:
        acts.append({"label": "Claude's minutes, turn by turn", "command": f"johto time {short} {name}"})
    if stage == "ready_to_save" and not workspace.is_draft(meta):
        acts.append({"label": "Save to Studio", "command": f"johto push {short}"})
    if stage == "fix_qc":
        acts.append({"label": "AutoQC results", "command": f"johto qc {short}"})
    if stage == "ready_to_submit":
        acts.append({"label": "Submit", "command": f"johto submit {short}"})
    seen, out = set(), []
    for a in acts:
        if a["command"] not in seen:
            seen.add(a["command"])
            out.append(a)
    return out


# --- the card --------------------------------------------------------------------------------------------------

def card(task_dir: Path, *, touch: bool = True, live: bool = True, source: str = "where") -> dict:
    """The task card (contract 3): {task, short, title, stage, next, run, text, reviews, grading, check, since,
    since_at, focus, actions, submission_run, trajectory, status}. Logs a touch event unless touch=False;
    live=False keeps it off the network (a PC lane's state then shows as unknown)."""
    d = Path(task_dir)
    meta = workspace.load(d)
    last = stages.last(d, "touch")
    sub = workspace.parse_submission(d)
    text = text_state(d, meta, sub)
    try:
        t, lane = task_view(d, live=live, text=text)
    except Exception as e:   # the stage is the web app's; a failure there leaves the rest of the card
        print(f"johto where: the stage couldn't be derived for {d.name}: {e}")
        t = {"short": d.name, "meta": meta, "stage": {"stage": None, "label": "unknown"}, "label": meta.get("label")
             or meta["task_name"], "monitor": None}
        lane = {}
    run = run_block(d, meta, t, live=live)
    traj = None
    if run.get("submission"):
        try:
            traj = actions.trajectory_state(d, run["name"], meta)
        except Exception:
            traj = None
    st = t.get("stage") or {}
    out = {
        "task": meta["task_name"], "short": d.name, "title": t.get("label") or meta["task_name"],
        "status": text["status"],
        "stage": {"stage": st.get("stage"), "label": st.get("label"), "run": st.get("run"), "since": st.get("since")},
        "next": next_step(t, lane, run, text, traj),
        "run": run,
        "submission_run": run["name"] if run.get("submission") else None,
        "text": {k: v for k, v in text.items() if k != "best"},
        "trajectory": {k: traj.get(k) for k in ("status", "run", "path", "files", "other")} if traj else None,
        "reviews": reviews_block(d, run),
        "grading": grading_block(d),
        "check": check_block(d),
        "since": since_lines(d, meta, (last or {}).get("at")),
        "since_at": (last or {}).get("at"),
        "focus": focus_block(d),
        "studio_url": _task_url(meta),
        "draft": {"since": (meta.get("draft") or {}).get("at"), "open": actions.draft_open(meta),
                  "slot": actions.slot_key(meta.get("slot") or {})} if workspace.is_draft(meta) else None,
        "generated_at": stages.now_iso(),
        "offline": not live,
    }
    out["actions"] = action_list(d, meta, t, run, text)
    if touch:
        stages.touch(d, source=source)
    return out


def render_card(c: dict) -> str:
    """The card as plain terminal text, main point first."""
    lines = [f"{c['task']} · {c['title']}", f"Stage: {c['stage'].get('label') or '?'} (Studio: {c['status']})"]
    nx = c.get("next") or {}
    if nx.get("text"):
        lines.append(f"Next: {nx['text']}" + (f" ({nx['why']})" if nx.get("why") else ""))
        if nx.get("command"):
            lines.append(f"      {nx['command']}")
        if nx.get("also"):
            lines.append(f"Also: {nx['also']}")
    lines.append("")
    r = c.get("run") or {}
    if r.get("name"):
        by = {"chosen": "you chose it", "latest": "newest collected validation run",
              "current": "the run under way; nothing collected to submit yet"}.get(r.get("selected_by"), "")
        lines.append(f"Run: {r['word']} on the {r.get('host') or 'Mac'}" + (f" ({by})" if by else ""))
        if r.get("line"):
            lines.append(f"     {r['line']}")
        if r.get("state") == "unknown" and r.get("why"):
            lines.append(f"     Its lane state is unknown: {r['why']}.")
        for n in r.get("newer") or []:
            lines.append(f"     {run_word(n).capitalize()} is newer but not collected yet.")
    else:
        lines.append("Run: none yet.")
    f = c.get("focus")
    if f and f.get("method") != "none":
        lines.append(f"You: {_hm(f['task_minutes'])} over {f['sittings']} sitting{'s' if f['sittings'] != 1 else ''}"
                     f" ({f['method']}{', a session is open' if f.get('open') else ''}).")
    tx = c.get("text") or {}
    src = {"studio": "Studio's copy", "local": "the local file"}.get(tx.get("source"), tx.get("source"))
    lines.append(f"Text: {src}" + (f", Studio read {clock(tx['studio_at'])}" if tx.get("studio_at") else "") + ".")
    if tx.get("note"):
        lines.append(f"      {tx['note']}")
    if tx.get("missing") and (c["stage"].get("stage") not in DONE | {"in_review"}):
        lines.append(f"      Not written yet: {_words(tx['missing'])}.")
    tr = c.get("trajectory")
    if tr:
        word = {"uploaded": f"Studio has {run_word(tr.get('run'))}'s transcript",
                "missing": "Studio has no upload yet", "other_run": f"Studio has {', '.join(tr.get('other') or [])}'s, "
                f"not {tr.get('run')}'s", "unknown_file": "Studio has a file johto can't match to a run",
                "no_snapshot": "not read from Studio yet"}.get(tr.get("status"), tr.get("status"))
        lines.append(f"Trajectory Logs: {word}.")
    rv = c.get("reviews")
    if rv:
        bits = [rv["latest"]] if rv.get("latest") else []
        if rv.get("covers_turn") is not None and rv.get("run_turns"):
            behind = rv["covers_turn"] < rv["run_turns"]
            bits.append(f"covers turn {rv['covers_turn']} of {rv['run_turns']}" + ("; later turns not reviewed" if behind else ""))
        if rv.get("pending"):
            bits.append(f"{rv['pending']} waiting on GPT Pro")
        if bits:
            lines.append("Reviews: " + "; ".join(bits) + ".")
    g = c.get("grading")
    if g:
        lines.append(f"Grading: {g['rows']} rows, {g['selected']} selected"
                     + (f", {g['open_decisions']} open decision{'s' if g['open_decisions'] != 1 else ''}" if g.get("open_decisions") else "") + ".")
    ck = c.get("check")
    if ck:
        a = ck.get("attention")
        lines.append(f"Check: {clock(ck['latest_at'])}, " + (f"{a} need attention" if a else "nothing needs attention")
                     + ("; the fields changed since" if ck.get("stale") else "") + ".")
    if c.get("since"):
        lines.append("")
        lines.append(f"Since your last look ({clock(c['since_at'])}):" if c.get("since_at") else "Latest:")
        lines += [f"  {s}" for s in c["since"]]
    if c.get("actions"):
        lines.append("")
        w = max(len(a["label"]) for a in c["actions"])
        lines += [f"  {a['label']:<{w}}  {a['command']}" for a in c["actions"]]
    return "\n".join(lines)


def _hm(m: float | None) -> str:
    if m is None:
        return "-"
    return f"{round(m)} min" if m < 60 else f"{m / 60:.1f} h"


# --- every task ------------------------------------------------------------------------------------------------

DECK_NEEDS = "Ready when you have room"


def deck_rows(n: int = 3) -> list[dict]:
    try:
        from . import deck
        ps = deck.picks(n)
    except Exception:
        return []
    return [{"short": None, "title": f"{p['repo']} · {p['title']}", "needs_you": DECK_NEEDS,
             "machine": f"{p['open_total']} open slot{'s' if p['open_total'] != 1 else ''} fit"
                        + ("; familiar repo" if p.get("familiar") else ""),
             "action": "Claim", "command": p["command"], "url": p.get("url"), "priority": 4, "attention": False,
             "blocked": None, "badges": [], "kind": "deck", "id": p["id"]} for p in ps]


def overview() -> list[dict]:
    """One row per task in Home's order ({short, title, needs_you, machine, action, command, priority, ...}), then
    the deck's ready picks as "Ready when you have room" rows (replacing Home's generic pick row)."""
    try:
        rows = _data().overview()
    except Exception as e:
        rows = [{"short": None, "title": "", "needs_you": f"Home's queue failed: {e}", "machine": "", "action": "",
                 "command": None, "priority": 1, "attention": True, "blocked": None, "badges": []}]
    picks = deck_rows()
    at = next((i for i, r in enumerate(rows) if not r.get("short") and r.get("needs_you") == "Pick the next problem"), None)
    if at is not None and picks:
        rows = rows[:at] + picks + rows[at + 1:]
    else:
        rows = rows + picks
    return rows


GROUPS = {1: "Needs you now", 2: "Finish the submission", 3: "Continue", 4: "Start when you have time"}
WAITING = "Waiting on Claude or reviewers"   # rows with nothing for him: a run working, a task in review


def render_overview(rows: list[dict]) -> str:
    if not rows:
        return "No tasks and no ready picks."
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("kind") == "deck":
            g = DECK_NEEDS
        elif r.get("priority") is None:
            g = WAITING
        else:
            g = GROUPS.get(r["priority"], "Other")
        groups.setdefault(g, []).append(r)
    order = [*GROUPS.values(), WAITING, DECK_NEEDS, "Other"]
    lines = []
    for g in order:
        if g not in groups:
            continue
        lines.append(g)
        for r in groups[g]:
            who = r.get("short") or "-"
            if g == DECK_NEEDS:
                lines.append(f"  {r['title']}  ({r['machine']})")
            else:
                head = r["needs_you"] or r["machine"]
                lines.append(f"  {who:9} {head}" + (f"  · {r['title']}" if r.get("title") else ""))
                if r["needs_you"] and r.get("machine"):
                    lines.append(f"  {'':9} machine: {r['machine']}")
                if r.get("blocked"):
                    lines.append(f"  {'':9} blocked: {r['blocked']}")
            if r.get("command"):
                lines.append(f"  {'':9} {r['command']}")
        lines.append("")
    return "\n".join(lines).rstrip()


# --- CLI ---------------------------------------------------------------------------------------------------------

def cmd_where(a) -> None:
    if not a.task:
        rows = overview()
        print(json.dumps(rows, indent=1, default=str) if a.json else render_overview(rows))
        return
    try:
        d = workspace.resolve(a.task)
    except FileNotFoundError as e:
        raise SystemExit(str(e))
    c = card(d, touch=not a.no_touch, live=not a.offline)
    print(json.dumps(c, indent=1, default=str) if a.json else render_card(c))


def register(sub) -> None:
    s = sub.add_parser("where", help="where a task stands (one screen), or every task in Home's order")
    s.add_argument("task", nargs="?")
    s.add_argument("--offline", action="store_true", help="don't ask the PC about a PC lane")
    s.add_argument("--no-touch", action="store_true", help="don't count this as a look (since stays as it is)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_where)
