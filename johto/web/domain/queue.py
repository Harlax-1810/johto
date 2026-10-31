"""The Home queue: a pure function of the tasks' stages, the lane, blockers, leads and hides.

Design section 2 (tiers) and build-plan 9.2. Each task appears once, as its single next human step;
tasks with only machine work pending go to the muted Running group. Reasons state facts, never
predictions, and name clock times rather than "N min ago" so they don't go stale on screen.
"""
from __future__ import annotations

import hashlib
import time

from .derive import ts

EST = {"write_prompt": {"Casual prose": 600, "Explicit spec": 2400, "Error / artifact brief": 900},
       "write_details": 1500, "decide": 120, "rate": 480, "save": 120, "fix_qc": 300, "submit": 60,
       "feedback": 900, "log_discard": 120, "rejected": 60, "pick": 180, "lane_start": 10}
DETAILS_S = 1500
BEFORE_VALIDATION = {"prompt", "waiting_calib", "calibrating", "decide", "details"}
NOT_CLAIMED = {"accepted", "rejected", "dropped", "discarded", "unclaimed", "in_review"}
RUNNING = {"claiming": "Claiming", "waiting_calib": None, "waiting_validate": None, "calibrating": None,
           "validating": None, "collecting": None, "saving": "Saving to Studio", "qc_running": "AutoQC running",
           "submitting": "Submitting", "in_review": "In review"}
LANE_ROWS = {  # lane state -> (kind, title, order within tier 1)
    "permission": ("lane_permission", "Answer a permission prompt", 0),
    "waiting": ("lane_waiting", "Reply to Claude", 1),
    "needs_login": ("lane_login", "Log in to Claude in the lane", 3),
    "ready_for_prompt": ("lane_paste", "Paste your prompt", 4),
}


def run_word(run: str | None) -> str:
    if not run:
        return ""
    kind, _, n = run.partition("-")
    return f"{'calibration' if kind == 'calib' else 'validation' if kind == 'validate' else kind} {n}".strip()


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
    from .derive import details_complete
    if not details_complete(texts, slot):
        s += DETAILS_S
    return s


def _task_row(t: dict, now: float) -> dict | None:
    st = t["stage"]
    stage, run, since = st["stage"], st.get("run"), st.get("since")
    short, label, slot = t["short"], t["label"], t.get("slot") or {}
    at = clock(since, now)
    common = {"short": short, "label": label, "since": since, "run": run}
    if stage == "decide":
        return _item("decide", 3, "decide", "Keep or drop calibration", **common, est=est_s("decide"),
                     reason=f"{run_word(run).capitalize()} was collected at {at}" if at else "",
                     primary={"label": "Keep", "action": "decision", "args": {"run": run, "decision": "keep"}},
                     facts=(stage, run))
    if stage == "rate":
        if st.get("chosen"):
            title = f"Rate {run_word(run)}"
            reason = f"You chose {run_word(run)} as the run to submit"
        else:
            title = f"Rate {run_word(run)}"
            reason = f"{run_word(run).capitalize()} was collected at {at}" if at else ""
        return _item("rate", 3, "rate", title, **common, est=est_s("rate"), reason=reason,
                     primary={"label": "Done", "action": "done", "args": {"surface": "rate"}}, facts=(stage, run))
    if stage == "dropped":
        return _item("log_discard", 3, "log_discard", "Log the discarded run", **common, est=est_s("log_discard"),
                     reason=f"Dropped at {at}" if at else "Dropped",
                     primary={"label": "Logged", "action": "discard_logged", "args": {}}, facts=(stage,))
    if stage == "sent_back":
        fb = st.get("feedback") or ""
        return _item("feedback", 4, "feedback", "Reviewer sent it back", **common, est=est_s("feedback"),
                     reason=f"Sent back at {at}" if at else "Sent back",
                     primary={"label": "Save to Studio and rerun AutoQC", "action": "save", "args": {}},
                     facts=(stage, hashlib.sha1(fb.encode()).hexdigest()[:8]))
    if stage == "fix_qc":
        qc = st.get("qc") or {}
        c = qc.get("counts") or {}
        n = c.get("fail") or 0
        total = sum(c.get(k) or 0 for k in ("pass", "fail", "neutral"))
        return _item("fix_qc", 4, "studio", f"Fix {n} AutoQC check{'s' if n != 1 else ''}", **common, est=est_s("fix_qc"),
                     reason=f"AutoQC failed {n} of {total} checks at {at}" if at else f"AutoQC failed {n} checks",
                     primary={"label": "Save to Studio and rerun AutoQC", "action": "save", "args": {}},
                     facts=(stage, qc.get("audit_id")))
    if stage == "ready_to_submit":
        qc = st.get("qc") or {}
        return _item("submit", 4, "studio", "Submit for review", **common, est=est_s("submit"),
                     reason=f"AutoQC passed at {at}" if at else "AutoQC passed",
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
                     reason=f"Rejected at {at}" if at else "Rejected", facts=(stage,))
    if stage == "prompt":
        if st.get("rework"):
            reason = f"You chose to edit the prompt after {run_word(run)}"
        else:
            reason = f"Claimed at {at}" if at else ""
        return _item("write_prompt", 5, "write_prompt", "Write the opening prompt", short=short, label=label,
                     since=since, est=est_s("write_prompt", slot), reason=reason,
                     primary={"label": "Done, queue calibration", "action": "done", "args": {"surface": "prompt"}},
                     facts=(stage, run or ""))
    if stage == "details":
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
    return None


def _running_row(t: dict, lane: dict, now: float) -> dict | None:
    st = t["stage"]
    stage, run = st["stage"], st.get("run")
    if stage not in RUNNING:
        return None
    title = RUNNING[stage]
    if stage in ("calibrating", "validating"):
        ls = lane.get("state")
        rw = run_word(run)
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
                 lane=stage in ("calibrating", "validating"), facts=(stage, run or ""))


def _lane_rows(lane: dict, labels: dict, now: float) -> list[dict]:
    ls = lane.get("state")
    run = lane.get("run") or {}
    out = []
    short = run.get("short")
    label = run.get("label") or labels.get(short, "")
    if ls in LANE_ROWS:
        kind, title, order = LANE_ROWS[ls]
        if ls == "waiting" and (run.get("mode") == "async"):
            facts = lane.get("facts") or {}
            if (facts.get("turns") or 0) <= 1:
                kind, title, order = "lane_async_done", "End the async run", 2
        if ls == "ready_for_prompt" and (lane.get("input") or {}).get("empty") is False:
            title = "Send your prompt"     # it is in Claude's input box; Enter in the lane sends it
        since = lane.get("since")
        at = clock(since, now)
        reason = {"lane_waiting": f"Claude has been waiting since {at}" if at else "Claude is waiting for you",
                  "lane_permission": "Claude asks permission" + (f": {lane.get('evidence')}" if lane.get("evidence") else ""),
                  "lane_login": "The lane's Claude needs /login before the run",
                  "lane_paste": "Your prompt is in the lane's input box; press Enter there to send it"
                  if title == "Send your prompt" else "The lane is ready for your opening prompt",
                  "lane_async_done": f"Claude ended its turn at {at}" if at else "Claude ended its turn"}[kind]
        primary = {"label": "Open the lane", "action": "lane_open", "args": {}}
        it = _item(kind, 1, "lane", title, short=short, label=label, since=since, run=run.get("run"), reason=reason,
                   lane=True, primary=primary, facts=(ls, run.get("run"), since))
        it["_order"] = order
        out.append(it)
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


def build_queue(snap: dict) -> tuple[list[dict], list[dict]]:
    """(queue, running), ordered. snap: {tasks, lane, blockers, leads, prefs, settings, now}."""
    now = snap.get("now") or time.time()
    lane = snap.get("lane") or {}
    tasks = snap.get("tasks") or []
    settings = snap.get("settings") or {}
    hidden = set((snap.get("prefs") or {}).get("hidden") or ())
    labels = {t["short"]: t["label"] for t in tasks}
    items: list[dict] = list(snap.get("blockers") or [])
    running: list[dict] = []

    lane_rows = _lane_rows(lane, labels, now)
    items += lane_rows
    lane_short = next((r["short"] for r in lane_rows if r["tier"] == 1), None)

    queued_for_lane = []
    for t in tasks:
        row = _task_row(t, now)
        if row:
            items.append(row)
            continue
        if t["short"] == lane_short:
            continue  # its tier-1 lane row stands for it
        stage = t["stage"]["stage"]
        if stage in ("waiting_calib", "waiting_validate"):
            queued_for_lane.append(t)
        rr = _running_row(t, lane, now)
        if rr:
            running.append(rr)

    # Tier 2: start the lane, when nothing will start it on its own.
    auto = lane.get("auto_prepare", settings.get("auto_prepare_lane", True)) and snap.get("lane_service", False)
    if lane.get("state", "free") in ("free", "collected") and queued_for_lane and not auto:
        q = sorted(queued_for_lane, key=lambda t: (t["stage"]["stage"] != "waiting_validate", ts(t["stage"].get("since")) or 0))
        t = q[0]
        kind = t["stage"].get("kind") or ("validate" if t["stage"]["stage"] == "waiting_validate" else "calib")
        word = "validation" if kind == "validate" else "calibration"
        items.append(_item("lane_start", 2, "lane", f"Start {word}", short=t["short"], label=t["label"],
                           since=t["stage"].get("since"), est=est_s("lane_start"), reason="The lane is free",
                           primary={"label": f"Start {word}", "action": "lane_start", "args": {"short": t["short"], "kind": kind}},
                           facts=("lane_start", t["short"], kind)))
        running = [r for r in running if r["short"] != t["short"]]

    # Tier 6: pick, when work in progress is low.
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

    items = [i for i in items if i["id"] not in hidden and i.get("hide_key") not in hidden]
    order3 = {"decide": 0, "rate": 1, "log_discard": 2}
    order4 = {"feedback": 0, "fix_qc": 1, "submit": 2, "save": 3, "rejected": 4}
    rem = {t["short"]: remaining_writing_s(t) for t in tasks}
    claimed_at = {t["short"]: ts(t.get("claimed_at")) or 0 for t in tasks}

    def key(i):
        tier = i["tier"]
        if tier == 1:
            sub = (i.get("_order", 9),)
        elif tier == 3:
            sub = (order3.get(i["kind"], 9), ts(i.get("since")) or 0)
        elif tier == 4:
            sub = (order4.get(i["kind"], 9), ts(i.get("since")) or 0)
        elif tier == 5:
            sub = (rem.get(i["short"], 0), claimed_at.get(i["short"], 0))
        else:
            sub = (ts(i.get("since")) or 0,)
        return (tier, *sub, i["id"])
    items.sort(key=key)
    for i in items:
        i.pop("_order", None)
    running.sort(key=lambda r: (0 if r["lane"] else 1, ts(r.get("since")) or 0))
    return items, running
