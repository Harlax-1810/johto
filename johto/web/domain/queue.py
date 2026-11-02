"""The Home queue ("what needs me now"): a pure function of the tasks' stages, the lane, the run monitor,
blockers, leads and hides.

Design section 2 and the GPT Pro UX review, section 3A. Each task appears once, as one row that carries both
what Austin owes it (the title, its surface) and what the machine is doing for it (`machine`): "Claude is
running, and you can write the goal" is one row. A task with nothing for him sits in the muted Running group.

Order, highest first (`priority`):
1. a real question, a permission prompt or a failure that blocks work (tiers 0 and 1, the attention signal);
2. finishing a submission that is close (decide, rate, the fields after the run, save, fix AutoQC, submit);
3. continuing work under way (the goal while Claude runs, reading Claude's answer, a started prompt);
4. starting prepared work (a new prompt, a lane to start, picking the next problem).

An ended turn alone isn't a question: the run monitor (budget.classify_turn) says whether Claude asked
something, needs permission, hit a problem, answered, posted progress or left background work running. Only
the first three take the attention signal. A finished review is a quiet badge. A signed-out Studio holds back
the Studio rows only. Reasons state facts, never predictions, and name clock times so they don't go stale.

`build_queue` is the one place this ordering lives; `overview_rows` turns its output into one plain row per
task for `johto where` (STATE's where.overview) so the CLI and the web app agree.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from .derive import details_complete, ts

EST = {"write_prompt": {"Casual prose": 600, "Explicit spec": 2400, "Error / artifact brief": 900},
       "write_details": 1500, "write_goal": 420, "decide": 120, "rate": 480, "save": 120, "fix_qc": 300,
       "submit": 60, "feedback": 900, "log_discard": 120, "rejected": 60, "pick": 180, "lane_start": 10,
       "start_validation": 10}
DETAILS_S = 1500
BEFORE_VALIDATION = {"prompt", "waiting_calib", "calibrating", "decide", "details"}
NOT_CLAIMED = {"accepted", "rejected", "dropped", "discarded", "unclaimed", "in_review"}
RUNNING = {"claiming": "Claiming", "waiting_calib": None, "waiting_validate": None, "calibrating": None,
           "validating": None, "collecting": None, "saving": "Saving to Studio", "qc_running": "AutoQC running",
           "submitting": "Submitting", "in_review": "In review"}
# While Claude runs (or the run waits for the lane), the goal can be written; the rest comes after the run.
DURING_RUN = {"validating", "calibrating", "collecting", "waiting_validate"}
DETAIL_NAMES = {"goal": "goal", "decision_points": "decision points", "codebase": "codebase notes",
                "initial_state": "initial state"}
LANE_ROWS = {  # lane state -> (kind, title, order within priority 1)
    "permission": ("lane_permission", "Answer a permission prompt", 0),
    "needs_login": ("lane_login", "Log in to Claude in the lane", 3),
    "ready_for_prompt": ("lane_paste", "Paste your prompt", 4),
}
# What an ended turn asks of Austin (run monitor classes) -> (kind, title, tier, priority, order).
TURN_ROWS = {
    "question": ("lane_waiting", "Answer Claude's question", 1, 1, 1),
    "permission": ("lane_permission", "Answer a permission prompt", 1, 1, 0),
    "problem": ("lane_problem", "Claude hit a problem", 1, 1, 2),
    "substantive": ("lane_answer", "Read Claude's answer", 5, 3, 0),
}
FINISH = {"decide", "rate", "log_discard", "feedback", "fix_qc", "submit", "save", "rejected", "lane_async_done",
          "finish_details"}
START = {"lane_start", "pick", "new_prompt"}
STUDIO_KINDS = {"save", "fix_qc", "submit", "feedback", "pick"}   # their primary action needs the Studio tab
# Within "finish what is close": the step nearest to submitted first (by what is left, not by when it arrived).
CLOSE = {"submit": 0, "save": 1, "fix_qc": 2, "log_discard": 3, "decide": 4, "lane_async_done": 5, "rate": 6,
         "feedback": 7, "write_details": 8, "rejected": 9}


def run_word(run: str | None) -> str:
    if not run:
        return ""
    kind, _, n = run.partition("-")
    return f"{'calibration' if kind == 'calib' else 'validation' if kind == 'validate' else kind} {n}".strip()


def at_phrase(at: str) -> str:
    """clock()'s text after a verb: "at 14:31", "yesterday at 14:31", "on Oct 28 at 14:31"."""
    if not at or at[:1].isdigit():
        return f"at {at}"
    day, _, hm = at.rpartition(" ")
    return f"{day} at {hm}" if day == "yesterday" else f"on {day} at {hm}"


def clock(iso, now: float | None = None) -> str:
    """14:31 today, "yesterday 14:31", or "Oct 28 14:31"."""
    t = ts(iso)
    if t is None:
        return ""
    lt, nt = time.localtime(t), time.localtime(now or time.time())
    hm = time.strftime("%H:%M", lt)
    days = (time.mktime((nt.tm_year, nt.tm_mon, nt.tm_mday, 0, 0, 0, 0, 0, -1))
            - time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))) / 86400
    if round(days) == 0:
        return hm
    if round(days) == 1:
        return f"yesterday {hm}"
    return time.strftime("%b %d ", lt).replace(" 0", " ") + hm


def item_id(kind: str, short: str | None, run: str | None, *facts) -> str:
    """kind:short:run:h. The front end reads the run name from the third part (decide and rate rows)."""
    h = hashlib.sha1("|".join(str(f) for f in (kind, short, run, *facts)).encode()).hexdigest()[:6]
    return f"{kind}:{short or '-'}:{run or '-'}:{h}"


def _item(kind, tier, surface, title, *, short=None, label="", reason="", est=None, since=None, run=None,
          primary=None, lane=False, group="queue", facts=()) -> dict:
    return {"id": item_id(kind, short, run, *facts), "kind": kind, "tier": tier, "short": short, "title": title,
            "label": label, "reason": reason, "est_s": est, "surface": surface,
            "primary": primary, "lane": lane, "since": since, "group": group, "run": run}


def est_s(kind: str, slot: dict | None = None, estimates: dict | None = None) -> int | None:
    if estimates and kind in estimates:
        return estimates[kind]
    v = EST.get(kind)
    if isinstance(v, dict):
        return v.get((slot or {}).get("prompt_style"), 600)
    return v


def remaining_writing_s(t: dict) -> int:
    texts, slot = t.get("texts") or {}, t.get("slot") or {}
    s = 0
    if not (texts.get("opening_prompt") or "").strip():
        s += est_s("write_prompt", slot)
    if not details_complete(texts, slot):
        s += DETAILS_S
    return s


def details_missing(texts: dict, slot: dict) -> list[str]:
    keys = ["goal", "decision_points", "codebase"] + (["initial_state"] if (slot or {}).get("action") == "Operate" else [])
    return [k for k in keys if not ((texts or {}).get(k) or "").strip()]


def _write_title(missing: list[str]) -> str:
    if len(missing) == 1:
        return f"Write the {DETAIL_NAMES[missing[0]]}"
    if missing == ["decision_points", "codebase"]:
        return "Write decision points and codebase notes"
    return "Write the details"


def _used_validation(t: dict) -> bool:
    return any(r.get("kind") == "validate" and (r.get("collected_at") or r.get("has_transcript") or r.get("live")
                                                 or r.get("container_up") or r.get("remote_live"))
               for r in t.get("runs") or [])


def priority_of(item: dict) -> int:
    """1 blocking, 2 finish what is close, 3 continue work under way, 4 start prepared work."""
    if item.get("priority"):
        return item["priority"]
    if item["tier"] <= 1:
        return 1
    if item["kind"] in FINISH:
        return 2
    if item["kind"] in START:
        return 4
    return 3


# --- the human step for one task ------------------------------------------------------------------------

def _task_row(t: dict, now: float) -> dict | None:
    st = t["stage"]
    stage, run, since = st["stage"], st.get("run"), st.get("since")
    short, label, slot = t["short"], t["label"], t.get("slot") or {}
    texts = t.get("texts") or {}
    at = clock(since, now)
    common = {"short": short, "label": label, "since": since, "run": run}
    if stage == "decide":
        return _item("decide", 3, "decide", "Keep or drop calibration", **common, est=est_s("decide"),
                     reason=f"{run_word(run).capitalize()} was collected {at_phrase(at)}" if at else "",
                     primary={"label": "Keep", "action": "decision", "args": {"run": run, "decision": "keep"}},
                     facts=(stage, run))
    if stage == "rate":
        missing = details_missing(texts, slot)
        if missing and not st.get("chosen"):
            # Austin's order: the goal may wait until the run starts, the rest until it ends. Both come before
            # the ratings, on the details surface.
            it = _item("write_details", 3, "details", _write_title(missing), **common, est=est_s("write_details"),
                       reason=f"{run_word(run).capitalize()} was collected {at_phrase(at)}" if at else "",
                       primary={"label": f"Done, rate {run_word(run)}", "action": "done", "args": {"surface": "details"}},
                       facts=(stage, run, "after"))
            it["priority"] = 2
            return it
        title = f"Rate {run_word(run)}"
        if st.get("chosen"):
            reason = f"You chose {run_word(run)} as the run to submit"
        else:
            reason = f"{run_word(run).capitalize()} was collected {at_phrase(at)}" if at else ""
        return _item("rate", 3, "rate", title, **common, est=est_s("rate"), reason=reason,
                     primary={"label": "Done", "action": "done", "args": {"surface": "rate"}}, facts=(stage, run))
    if stage == "dropped":
        return _item("log_discard", 3, "log_discard", "Log the discarded run", **common, est=est_s("log_discard"),
                     reason=f"Dropped {at_phrase(at)}" if at else "Dropped",
                     primary={"label": "Logged", "action": "discard_logged", "args": {}}, facts=(stage,))
    if stage == "sent_back":
        fb = st.get("feedback") or ""
        return _item("feedback", 4, "feedback", "Reviewer sent it back", **common, est=est_s("feedback"),
                     reason=f"Sent back {at_phrase(at)}" if at else "Sent back",
                     primary={"label": "Save to Studio and rerun AutoQC", "action": "save", "args": {}},
                     facts=(stage, hashlib.sha1(fb.encode()).hexdigest()[:8]))
    if stage == "fix_qc":
        qc = st.get("qc") or {}
        c = qc.get("counts") or {}
        n = c.get("fail") or 0
        total = sum(c.get(k) or 0 for k in ("pass", "fail", "neutral"))
        return _item("fix_qc", 4, "studio", f"Fix {n} AutoQC check{'s' if n != 1 else ''}", **common, est=est_s("fix_qc"),
                     reason=f"AutoQC failed {n} of {total} checks {at_phrase(at)}" if at else f"AutoQC failed {n} checks",
                     primary={"label": "Save to Studio and rerun AutoQC", "action": "save", "args": {}},
                     facts=(stage, qc.get("audit_id")))
    if stage == "ready_to_submit":
        qc = st.get("qc") or {}
        return _item("submit", 4, "studio", "Submit for review", **common, est=est_s("submit"),
                     reason=f"AutoQC passed {at_phrase(at)}" if at else "AutoQC passed",
                     primary={"label": "Submit (press twice)", "action": "submit", "args": {}},
                     facts=(stage, qc.get("audit_id")))
    if stage == "ready_to_save":
        changed = t.get("changed_since_push")
        if changed:
            reason = f"{len(changed)} field{'s' if len(changed) != 1 else ''} changed since the last save"
        elif changed is None:
            reason = "Ratings pass; nothing saved to Studio yet"
        else:
            reason = "AutoQC has not run on the saved fields"
        return _item("save", 4, "studio", "Save to Studio", **common, est=est_s("save"), reason=reason,
                     primary={"label": "Save to Studio and run AutoQC", "action": "save", "args": {}}, facts=(stage,))
    if stage == "rejected":
        return _item("rejected", 4, "feedback", "Rejected in review", **common, est=None,
                     reason=f"Rejected {at_phrase(at)}" if at else "Rejected", facts=(stage,))
    if stage == "prompt":
        if st.get("rework"):
            reason = f"You chose to edit the prompt after {run_word(run)}"
        else:
            reason = f"Claimed {at_phrase(at)}" if at else ""
        it = _item("write_prompt", 5, "write_prompt", "Write the opening prompt", short=short, label=label,
                   since=since, est=est_s("write_prompt", slot), reason=reason,
                   primary={"label": "Done, go to validation", "action": "done", "args": {"surface": "prompt"}},
                   facts=(stage, run or ""))
        # A prompt he has started is work under way; an empty one is new work.
        it["priority"] = 3 if (texts.get("opening_prompt") or "").strip() or st.get("rework") else 4
        return it
    if stage == "details":
        if st.get("skipped") and not _used_validation(t):
            # No calibration and no pre-run field gate (Austin's order): the prompt is enough to start the
            # validation run; the goal can be written once it runs.
            return _item("start_validation", 5, "lane", "Start validation", short=short, label=label, since=since,
                         est=est_s("start_validation"), reason="Your opening prompt is saved; the goal can wait until "
                         "the run starts", primary={"label": "Start validation", "action": "lane_start",
                                                    "args": {"short": short, "kind": "validate"}},
                         facts=(stage, "start"))
        if st.get("chosen"):
            reason = f"You chose {run_word(run)} as the run to submit"
            label_done = "Done, rate the run"
        elif st.get("skipped"):
            reason, label_done = "Calibration skipped", "Done, queue validation"
        else:
            reason = f"Kept after {run_word(run)}" if run else ""
            label_done = "Done, queue validation"
        return _item("write_details", 5, "details", "Write details", short=short, label=label, since=since,
                     est=est_s("write_details"), reason=reason,
                     primary={"label": label_done, "action": "done", "args": {"surface": "details"}},
                     facts=(stage, run or "", bool(st.get("chosen"))))
    if stage == "collect_failed":
        return _item("collect_failed", 0, "blocker", f"Collect {run_word(run)}", **common, est=None,
                     reason=f"{run_word(run).capitalize()} ended but its transcript and diff were not collected",
                     primary={"label": "Collect the run", "action": "lane_collect", "args": {"short": short, "run": run}},
                     facts=(stage, run))
    if stage in DURING_RUN:
        missing = details_missing(texts, slot)
        if "goal" in missing:
            it = _item("write_details", 5, "details", "Write the goal", short=short, label=label, since=since,
                       run=run, est=est_s("write_goal"),
                       reason=f"{run_word(run).capitalize()} is under way; the rest comes after it" if run
                       else "The run is queued; the rest comes after it",
                       primary={"label": "Open the goal", "action": "focus", "args": {"field": "goal"}},
                       facts=(stage, run or "", "during"))
            it["priority"] = 3
            return it
    return None


def _running_row(t: dict, lane: dict, now: float) -> dict | None:
    st = t["stage"]
    stage, run = st["stage"], st.get("run")
    if stage not in RUNNING:
        return None
    title = RUNNING[stage]
    mine = (lane.get("run") or {}).get("short") == t["short"]
    if stage in ("calibrating", "validating"):
        ls = lane.get("state") if mine else None
        rw = run_word(run)
        mon = t.get("monitor") or {}
        if not mine and mon.get("host_name"):
            title = f"Claude working · {rw} · {mon.get('host')}" if mon.get("state") == "working" else \
                f"{rw.capitalize()} on the {mon.get('host')}"
        else:
            title = {"preparing": f"Setting up {rw}", "working": f"Claude working · {rw}",
                     "exited": f"Collecting {rw}", "collecting": f"Collecting {rw}",
                     "setup_failed": f"Setup failed · {rw}"}.get(ls, f"Claude working · {rw}" if ls is None else f"{rw.capitalize()} in the lane")
    elif stage == "collecting":
        title = f"Collecting {run_word(run)}"
    elif stage in ("waiting_calib", "waiting_validate"):
        title = f"Waiting for the lane · {'validation' if stage == 'waiting_validate' else 'calibration'}"
    elif stage == "qc_running":
        title = f"AutoQC running since {clock(st.get('since'), now)}" if st.get("since") else "AutoQC running"
    return _item(stage, 9, "lane" if stage in ("calibrating", "validating") else "studio", title, short=t["short"],
                 label=t["label"], since=st.get("since"), run=run, group="running",
                 lane=stage in ("calibrating", "validating") and mine, facts=(stage, run or ""))


# --- what the machine is doing for a task ------------------------------------------------------------------

def _mins(m) -> str:
    m = float(m or 0)
    return "under 1 min" if 0 < m < 0.5 else f"{round(m)} min"


def machine(t: dict, lane: dict, now: float) -> dict | None:
    """One line about the machine work for a task: {text, dot (work|busy|wait|idle|bad), host, run, budget}.
    Dots follow the lane strip: work = Claude working, busy = a job, wait = Claude stopped for Austin."""
    st = t.get("stage") or {}
    stage, srun = st.get("stage"), st.get("run")
    mon = t.get("monitor") or {}
    lr = lane.get("run") or {}
    ls = lane.get("state")
    mine = lr.get("short") == t["short"] and ls not in (None, "free", "collected")
    out = None
    if stage in NOT_CLAIMED:
        # Out of his hands: the stage says it. A lane left on another machine is worth a word (it needs cleaning).
        at = clock(st.get("since"), now)
        text = {"in_review": (f"In review (Studio checked {at})" if st.get("checked") else f"In review since {at}")
                if at else "In review"}.get(stage)
        left = bool(text and mon.get("host_name") and mon.get("state") not in ("collected", "removed", None))
        if left:
            text += f" · a lane is still on the {mon.get('host')}"
        return {"text": text, "dot": "idle", "run": srun, "left_on": mon.get("host") if left else None} if text else None
    if mine:
        rw = run_word(lr.get("run"))
        cls = (turn_of(mon) if mon.get("run") == lr.get("run") else None) or _class_of_lane(lane)
        at = clock(lane.get("since"), now)
        text, dot = {
            "preparing": (f"Setting up {rw}", "busy"),
            "needs_login": (f"Lane needs /login · {rw}", "wait"),
            "ready_for_prompt": (f"Lane ready for your prompt · {rw}", "wait"),
            "working": (f"Claude working · {rw} · Mac", "work"),
            "permission": (f"Claude asks permission · {rw}", "wait"),
            "exited": (f"Collecting {rw}", "busy"), "collecting": (f"Collecting {rw}", "busy"),
            "setup_failed": (f"Setup failed · {rw}", "bad"),
        }.get(ls, (None, None))
        if ls == "waiting":
            text, dot = _ended_text(cls, rw, at, "Mac", mon if mon.get("run") == lr.get("run") else {})
        out = {"text": text or f"{rw.capitalize()} in the lane", "dot": dot or "busy", "host": "Mac", "run": lr.get("run")}
    elif mon.get("state") in ("working", "waiting", "background", "ready", "paused", "unknown", "stopped") \
            and (mon.get("host_name") or mon.get("up")):
        # A run the lane service isn't showing: on the PC, or a second lane on this Mac.
        rw, host = mon.get("run_word") or run_word(mon.get("run")), mon.get("host") or "Mac"
        s = mon["state"]
        if s == "working":
            text, dot = f"Claude working · {rw} · {host}", "work"
        elif s in ("waiting", "background"):
            text, dot = _ended_text(turn_of(mon), rw, clock((mon.get("turn") or {}).get("end"), now), host, mon)
        elif s == "ready":
            text, dot = f"Lane ready for your prompt · {rw} · {host}", "wait"
        elif s == "paused":
            text, dot = f"{rw.capitalize()} paused on the {host}: Docker isn't running there", "bad"
        elif s == "stopped":
            text, dot = f"{rw.capitalize()} stopped on the {host}, not collected", "bad"
        else:
            text, dot = f"{rw.capitalize()} on the {host} · can't check it now", "idle"
        out = {"text": text, "dot": dot, "host": host, "run": mon.get("run")}
    elif stage == "collecting":
        out = {"text": f"Collecting {run_word(srun)}", "dot": "busy", "run": srun}
    elif stage == "collect_failed":
        out = {"text": f"{run_word(srun).capitalize()} not collected", "dot": "bad", "run": srun}
    elif stage in ("saving", "qc_running", "submitting", "claiming"):
        at = clock(st.get("since"), now)
        text = {"saving": "Saving to Studio", "qc_running": f"AutoQC running since {at}" if at else "AutoQC running",
                "submitting": "Submitting", "claiming": "Claiming in Studio"}[stage]
        out = {"text": text, "dot": "busy", "run": srun}
    elif stage in ("waiting_validate", "waiting_calib"):
        out = {"text": "Waiting for the lane", "dot": "idle", "run": None}
    elif mon.get("run") and mon.get("collected"):
        rw = mon.get("run_word") or run_word(mon["run"])
        out = {"text": f"{rw.capitalize()} collected · {_mins(mon.get('working_minutes'))} · {mon.get('host') or 'Mac'}",
               "dot": "idle", "host": mon.get("host"), "run": mon["run"]}
    if out is not None and mon.get("run"):
        out["budget"] = {"task_minutes": mon.get("task_minutes"), "of": mon.get("budget"), "tone": mon.get("budget_tone")}
    return out


def turn_of(mon: dict) -> str | None:
    """What the run's last turn asks of Austin: the monitor's class, except that a turn which did real work
    and then asked something is a question (he owes the answer)."""
    cls = mon.get("turn_class")
    if cls in ("substantive", "progress") and mon.get("question"):
        return "question"
    return cls


def _class_of_lane(lane: dict) -> str | None:
    """A rule for the local lane's ended turn when the run monitor hasn't measured it yet."""
    if lane.get("rate_limited"):
        return "problem"
    q = (lane.get("question") or "").strip()
    return "question" if q.endswith("?") else "substantive" if lane.get("state") == "waiting" else None


def _ended_text(cls: str | None, rw: str, at: str, host: str, mon: dict) -> tuple[str, str]:
    when = f" {at_phrase(at)}" if at else ""
    bg = (mon.get("background") or {}).get("running") or 0
    if cls == "background" or bg:
        return f"Background work running · {rw} · {host}", "busy"
    return {
        "question": (f"Claude asked a question{when} · {host}", "wait"),
        "permission": (f"Claude asks permission · {rw} · {host}", "wait"),
        "problem": (f"Claude stopped on a problem{when} · {host}", "bad"),
        "progress": (f"Claude posted progress{when} · {rw} · {host}", "idle"),
    }.get(cls, (f"Claude answered{when} · {rw} · {host}", "wait" if cls == "substantive" else "idle"))


def badges(t: dict) -> list[dict]:
    """Quiet badges: the run's outside review (queued, reviewing, ready), named by the turn it covers."""
    if (t.get("stage") or {}).get("stage") in NOT_CLAIMED:
        return []
    rv = (t.get("monitor") or {}).get("review") or {}
    if rv.get("state") in ("queued", "reviewing", "ready") and rv.get("text"):
        return [{"kind": "review", "state": rv["state"], "text": rv["text"], "turn": rv.get("turn"),
                 "at": rv.get("at"), "covers_latest": rv.get("covers_latest"), "of": rv.get("of")}]
    return []


# --- lane needs -------------------------------------------------------------------------------------------

def _lane_need(t: dict, lane: dict, now: float) -> dict | None:
    """What the task's live run asks of Austin, from the local lane (screen and transcript) or, for a run on
    another machine, from the run monitor. None when the lane owes nothing (working, progress, background)."""
    short = t["short"]
    lr = lane.get("run") or {}
    mon = t.get("monitor") or {}
    mode = "async" if ((t.get("slot") or {}).get("interaction_mode") or "").startswith("Async") else "interactive"
    if lr.get("short") == short:
        ls = lane.get("state")
        since, run = lane.get("since"), lr.get("run")
        at = clock(since, now)
        if ls in LANE_ROWS:
            kind, title, order = LANE_ROWS[ls]
            if ls == "ready_for_prompt" and (lane.get("input") or {}).get("empty") is False:
                title = "Send your prompt"     # it is in Claude's input box; Enter in the lane sends it
            reason = {"lane_permission": "Claude asks permission" + (f": {lane.get('evidence')}" if lane.get("evidence") else ""),
                      "lane_login": "The lane's Claude needs /login before the run",
                      "lane_paste": "Your prompt is in the lane's input box; press Enter there to send it"
                      if title == "Send your prompt" else "The lane is ready for your opening prompt"}[kind]
            return {"kind": kind, "title": title, "tier": 1, "priority": 1, "order": order, "since": since,
                    "run": run, "reason": reason, "host": "Mac", "local": True, "facts": (ls, run, since)}
        if ls == "waiting":
            cls = (turn_of(mon) if mon.get("run") == run else None) or _class_of_lane(lane)
            facts = lane.get("facts") or {}
            if lr.get("mode", mode) == "async" and (facts.get("turns") or 0) <= 1 and cls != "permission":
                return {"kind": "lane_async_done", "title": "End the async run", "tier": 3, "priority": 2, "order": 0,
                        "since": since, "run": run, "reason": f"Claude ended its turn {at_phrase(at)}" if at else "Claude ended its turn",
                        "host": "Mac", "local": True, "facts": ("waiting", run, since)}
            return _turn_need(cls, since, run, "Mac", True, lane.get("question"), mon.get("problem"), at)
        return None
    if mon.get("state") == "waiting" and turn_of(mon) and (mon.get("host_name") or mon.get("up")):
        since = (mon.get("turn") or {}).get("end")
        at = clock(since, now)
        if mode == "async" and (mon.get("turns") or 0) <= 1 and turn_of(mon) != "permission":
            return {"kind": "lane_async_done", "title": "End the async run", "tier": 3, "priority": 2, "order": 0,
                    "since": since, "run": mon.get("run"), "reason": f"Claude ended its turn {at_phrase(at)} on the "
                    f"{mon.get('host')}" if at else "Claude ended its turn", "host": mon.get("host"), "local": False,
                    "facts": ("waiting", mon.get("run"), since)}
        return _turn_need(turn_of(mon), since, mon.get("run"), mon.get("host"), False, mon.get("question"),
                          mon.get("problem"), at)
    return None


def _turn_need(cls, since, run, host, local, question, problem, at) -> dict | None:
    if cls not in TURN_ROWS:
        return None          # progress or background: Claude owes the next step, not Austin
    kind, title, tier, prio, order = TURN_ROWS[cls]
    where = "" if local else f" on the {host}"
    reason = {"lane_waiting": f"Claude asked{where} {at_phrase(at)}" if at else "Claude asked a question",
              "lane_permission": f"Claude is waiting{where} for a permission answer",
              "lane_problem": f"Claude stopped{where}: {problem}" if problem else f"Claude stopped{where} on an error",
              "lane_answer": f"Claude answered{where} {at_phrase(at)}; nothing asked of you" if at else "Claude answered"}[kind]
    if not local:
        title += f" · {host}"
    return {"kind": kind, "title": title, "tier": tier, "priority": prio, "order": order, "since": since, "run": run,
            "reason": reason, "host": host, "local": local, "question": question, "facts": (cls, run, since)}


def _lane_row(t: dict, need: dict) -> dict:
    it = _item(need["kind"], need["tier"], "lane", need["title"], short=t["short"], label=t["label"],
               since=need.get("since"), run=need.get("run"), reason=need["reason"], lane=need["local"],
               primary={"label": "Open the lane", "action": "lane_open", "args": {}} if need["local"]
               else {"label": "Copy the command", "action": "copy_command", "args": {}},
               facts=need["facts"])
    it["priority"], it["_order"] = need["priority"], need.get("order", 9)
    return it


def _lane_blockers(lane: dict, labels: dict) -> list[dict]:
    """Global tier-0 rows from the local lane: setup failed, wrong model or effort."""
    ls = lane.get("state")
    run = lane.get("run") or {}
    short = run.get("short")
    label = run.get("label") or labels.get(short, "")
    out = []
    if ls == "setup_failed":
        out.append(_item("setup_failed", 0, "blocker", "Lane setup failed", short=short, label=label,
                         since=lane.get("since"), run=run.get("run"), reason=lane.get("evidence") or "",
                         primary={"label": "Retry in a new folder", "action": "lane_start",
                                  "args": {"short": short, "kind": run.get("kind")}},
                         facts=("setup_failed", run.get("run"), lane.get("since"))))
    f = lane.get("facts") or {}
    if ls in ("working", "waiting", "permission") and (f.get("model_ok") is False or f.get("effort_ok") is False):
        out.append(_item("wrong_model", 0, "blocker", "Wrong model or effort in the lane", short=short, label=label,
                         since=lane.get("since"), run=run.get("run"),
                         reason="Johto requires Opus 5.5 at effort high on every reply",
                         primary={"label": "Open the lane", "action": "lane_open", "args": {}},
                         facts=("wrong_model", run.get("run"))))
    return out


def _command(t: dict, item: dict, need: dict | None) -> str | None:
    """The CLI twin of a row's action."""
    short, kind = t["short"], item["kind"]
    mon = t.get("monitor") or {}
    if need and not need.get("local") and need.get("run"):
        # A PC run is reached over ssh; `johto resume` is for a second lane on this Mac.
        rec = next((r for r in (t.get("meta") or {}).get("runs") or [] if r.get("dir") == f"runs/{need['run']}"), None)
        if rec and rec.get("host") and rec.get("remote_dir"):
            from ... import actions
            return actions.resume_command(Path(short), need["run"], rec)
        return f"johto resume {short} {need['run']}"
    if kind in ("start_validation", "lane_start"):
        return f"johto lane {short} validate"
    if kind in ("save", "fix_qc", "feedback"):
        return f"johto push {short}"
    if kind == "submit":
        return f"johto submit {short}"
    if kind == "collect_failed":
        return f"johto collect {short} {item.get('run') or ''}".strip()
    if kind == "write_details":
        return f"johto kit {short}"      # the facts to write from; `johto check` comes after the writing
    if kind == "rate":
        return f"johto check {short}"
    if kind == "write_prompt":
        return f"johto where {short}"
    if mon.get("run") and mon.get("state") in ("working", "waiting", "background"):
        return f"johto time {short} {mon['run']}"
    return None


# --- the queue --------------------------------------------------------------------------------------------

def build_queue(snap: dict) -> tuple[list[dict], list[dict]]:
    """(queue, running), ordered. snap: {tasks, lane, blockers, leads, prefs, settings, studio, now}.
    Each task (t["monitor"] is the run monitor for its current run, when known) gives at most one row."""
    now = snap.get("now") or time.time()
    lane = snap.get("lane") or {}
    tasks = snap.get("tasks") or []
    settings = snap.get("settings") or {}
    hidden = set((snap.get("prefs") or {}).get("hidden") or ())
    labels = {t["short"]: t["label"] for t in tasks}
    studio_down = (snap.get("studio") or {}).get("state") in ("signed_out", "error")
    items: list[dict] = list(snap.get("blockers") or [])
    items += _lane_blockers(lane, labels)
    running: list[dict] = []

    # Start the lane, when nothing will start it on its own: that task's row is the start (the goal can wait
    # until the run is under way).
    auto = lane.get("auto_prepare", settings.get("auto_prepare_lane", True)) and snap.get("lane_service", False)
    starter = None
    queued = [t for t in tasks if t["stage"]["stage"] in ("waiting_calib", "waiting_validate")]
    if lane.get("state", "free") in ("free", "collected") and queued and not auto:
        starter = sorted(queued, key=lambda t: (t["stage"]["stage"] != "waiting_validate",
                                                ts(t["stage"].get("since")) or 0))[0]

    for t in tasks:
        if t is starter:
            kind = t["stage"].get("kind") or ("validate" if t["stage"]["stage"] == "waiting_validate" else "calib")
            word = "validation" if kind == "validate" else "calibration"
            it = _item("lane_start", 2, "lane", f"Start {word}", short=t["short"], label=t["label"],
                       since=t["stage"].get("since"), est=est_s("lane_start"), reason="The lane is free",
                       primary={"label": f"Start {word}", "action": "lane_start", "args": {"short": t["short"], "kind": kind}},
                       facts=("lane_start", t["short"], kind))
            it["priority"] = 3
            it["machine"], it["badges"], it["command"] = machine(t, lane, now), badges(t), f"johto lane {t['short']} {kind}"
            items.append(it)
            continue
        human = _task_row(t, now)
        if human and human["kind"] == "write_prompt" and settings.get("calibrate_first"):
            human["primary"]["label"] = "Done, queue calibration"   # only when calibration is turned on
        need = _lane_need(t, lane, now)
        mach = machine(t, lane, now)
        row = None
        if need and human:
            # One row per task: the writing step keeps its surface (the caret stays put), and takes the lane's
            # title and urgency when the lane needs him more.
            row = human
            if need["priority"] < priority_of(human):
                row["also"] = human["title"]
                row["title"], row["reason"] = need["title"], need["reason"]
                row["priority"], row["tier"], row["_order"] = need["priority"], need["tier"], need.get("order", 9)
                row["lane"] = need["local"]
                row["since"] = need.get("since") or row.get("since")
            else:
                row["also"] = need["title"]
            row["lane_need"] = {k: need.get(k) for k in ("kind", "title", "host", "run", "since", "question")}
        elif need:
            row = _lane_row(t, need)
            row["lane_need"] = {k: need.get(k) for k in ("kind", "title", "host", "run", "since", "question")}
        elif human:
            row = human
        if row is not None:
            row["machine"], row["badges"] = mach, badges(t)
            row["command"] = _command(t, row, need)
            if need and not need.get("local"):
                row["host"] = need.get("host")
            items.append(row)
            continue
        rr = _running_row(t, lane, now)
        if rr:
            rr["machine"], rr["badges"], rr["command"] = mach, badges(t), _command(t, rr, None)
            running.append(rr)

    # Pick, when work in progress is low.
    before_val = sum(1 for t in tasks if t["stage"]["stage"] in BEFORE_VALIDATION)
    claimed = sum(1 for t in tasks if t["stage"]["stage"] not in NOT_CLAIMED)
    leads = snap.get("leads") or {}
    if before_val < settings.get("wip_before_validation", 2) and claimed < settings.get("wip_max", 4):
        middle = any(2 <= i["tier"] <= 5 for i in items)
        n = before_val
        reason = (f"{n} task{'s' if n != 1 else ''} before validation" if n else "Nothing is queued for the lane")
        label = f"{leads.get('usable', 0)} leads fit · {leads.get('open', 0):,} open" if leads else ""
        pick = _item("pick", 2 if not middle else 6, "pick", "Pick the next problem", label=label,
                     reason=reason, est=est_s("pick"), primary={"label": "Claim", "action": "claim", "args": {}},
                     facts=(leads.get("usable", 0), before_val, claimed))
        # The id stays "pick" so the selection never jumps while he reads leads; hiding uses the hashed key.
        pick["hide_key"], pick["id"] = pick["id"], "pick"
        items.append(pick)

    # A signed-out Studio holds back only what needs Studio. With nothing waiting on it, its row is quiet.
    if studio_down:
        needs = [i for i in items if i["kind"] in STUDIO_KINDS]
        for i in needs:
            i["blocked"] = "Studio is signed out"
        for i in items:
            if i["kind"] == "blocker_studio":
                i["blocks"] = [x["short"] for x in needs if x.get("short")]
                if not needs:
                    i["tier"], i["priority"] = 6, 4

    items = [i for i in items if i["id"] not in hidden and i.get("hide_key") not in hidden]
    rem = {t["short"]: remaining_writing_s(t) for t in tasks}
    claimed_at = {t["short"]: ts(t.get("claimed_at")) or 0 for t in tasks}

    def key(i):
        p, tier = priority_of(i), i["tier"]
        if p == 1:
            # Fixes for things nothing works without, then the lane, then Studio (it never takes over the lane).
            band = 2 if i["kind"] == "blocker_studio" else 0 if tier == 0 else 1
            sub = (band, i.get("_order", 9), ts(i.get("since")) or 0)
        elif p == 2:
            # Closest to submitted first: Studio steps, then the ratings, then the fields after the run.
            sub = (CLOSE.get(i["kind"], 9), ts(i.get("since")) or 0)
        elif p == 3:
            # Claude's answer, then a run to start, then writing (least writing left first).
            first = {"lane_answer": 0, "start_validation": 1, "lane_start": 1}
            sub = (first.get(i["kind"], 2), rem.get(i["short"], 0), claimed_at.get(i["short"], 0))
        else:
            sub = ({"start_validation": 0, "lane_start": 1, "write_prompt": 2, "blocker_studio": 3, "pick": 4}.get(i["kind"], 5),
                   claimed_at.get(i["short"], 0) or ts(i.get("since")) or 0)
        return (p, *sub, i["id"])
    items.sort(key=key)
    for i in items:
        i["priority"] = priority_of(i)
        i["attention"] = i["tier"] <= 1
        i.pop("_order", None)
    running.sort(key=lambda r: (0 if r["lane"] else 1, ts(r.get("since")) or 0))
    for r in running:
        r["priority"], r["attention"] = None, False
    return items, running


# --- one row per task, for `johto where` -----------------------------------------------------------------

NEEDS = {1: "Needs you now", 2: "Finish the submission", 3: "Continue", 4: "Start when you have time"}


def overview_rows(items: list[dict], running: list[dict]) -> list[dict]:
    """The queue as one plain row per task (plus global rows), in queue order:
    {short, title, needs_you, machine, action, command, priority}. A pure helper: STATE's where.overview() calls
    it (through data.overview()) so `johto where` and Home always rank tasks the same way."""
    out = []
    for i in items:
        out.append({"short": i.get("short"), "title": i.get("label") or "", "needs_you": i["title"],
                    "machine": (i.get("machine") or {}).get("text") or "", "action": (i.get("primary") or {}).get("label") or "",
                    "command": i.get("command"), "priority": i.get("priority") or priority_of(i),
                    "attention": bool(i.get("attention")), "blocked": i.get("blocked"),
                    "badges": [b["text"] for b in i.get("badges") or []]})
    for r in running:
        out.append({"short": r.get("short"), "title": r.get("label") or "", "needs_you": "",
                    "machine": (r.get("machine") or {}).get("text") or r["title"], "action": "",
                    "command": r.get("command"), "priority": None, "attention": False, "blocked": None,
                    "badges": [b["text"] for b in r.get("badges") or []]})
    return out
