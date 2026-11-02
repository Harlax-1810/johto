"""A task's stage, derived from its files plus the lane and job snapshots. Never stored.

Build-plan 9.1, with three additions the CLI flow needs:
- the submission run may be a calibration run Austin chose (task.json "submission_run"), not only the
  latest validate-N; a chosen calibration run asks for the details first, then the ratings;
- a run that ended without being collected is "collecting" (a collect job or the lane watcher is on it)
  or a "collect_failed" blocker, whatever its kind;
- a lane prepared but never used (no transcript, container gone) doesn't hold the task.
"""
from __future__ import annotations

from datetime import datetime

LABELS = {
    "prompt": "Prompt", "waiting_calib": "Queued", "waiting_validate": "Queued",
    "calibrating": "Calibrating", "validating": "Validating", "collecting": "Collecting",
    "collect_failed": "Not collected", "decide": "Decide", "details": "Details", "rate": "Rate",
    "ready_to_save": "Save", "saving": "Saving", "qc_running": "AutoQC", "fix_qc": "Fix QC",
    "ready_to_submit": "Submit", "submitting": "Submitting", "in_review": "In review",
    "sent_back": "Sent back", "accepted": "Accepted", "rejected": "Rejected", "dropped": "Dropped",
    "discarded": "Dropped", "unclaimed": "Unclaimed", "claiming": "Claiming",
}

LANE_ACTIVE = {"preparing", "setup_failed", "needs_login", "ready_for_prompt", "working", "waiting",
               "permission", "exited", "collecting"}
DETAIL_KEYS = ("goal", "decision_points", "codebase")
ACTIVE_JOB = {"queued", "running"}


def ts(s) -> float | None:
    """Epoch seconds from the ISO forms the files hold (-0700, +00:00, Z, fractions), else None."""
    if not s:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip().replace("Z", "+00:00")
    if len(t) >= 5 and t[-5] in "+-" and t[-3] != ":":
        t = t[:-2] + ":" + t[-2:]
    try:
        return datetime.fromisoformat(t).timestamp()
    except ValueError:
        return None


def after(a, b) -> bool:
    """a is later than b (a missing a is never later; a missing b always is)."""
    ta, tb = ts(a), ts(b)
    if ta is None:
        return False
    return tb is None or ta > tb


CLAIM_SLACK_S = 3600  # feedback first seen this long after the claim is a review of Austin's work


def _fb_hash(v) -> str:
    """sync.h, the hash the claim recorded ("" for no text)."""
    from ... import sync
    return sync.h(v) if sync.norm(v) else ""


def sent_back_since(meta: dict) -> str | None:
    """When the reviewer sent the task back to Austin ("" when the time is unknown), or None.

    A task in Writing whose reviewer feedback is on Austin's work counts, whether he submitted from the app
    or in Studio. The feedback is his when any of these hold: Studio moved the task from review back to
    Writing while the app watched (studio.sent_back_at); he submitted (app or Studio's counter); the
    feedback differs from what Studio held at his claim (feedback_at_claim); Studio held nothing at the
    claim (an empty studio_at_claim); or, for tasks claimed before those records, the app first saw the
    feedback more than an hour after the claim. Not when he resubmitted after it came back."""
    st = meta.get("studio") or {}
    fb = st.get("feedback")
    if st.get("status") != "Writing" or not str(fb or "").strip():
        return None
    since = st.get("sent_back_at") or st.get("feedback_at") or st.get("polled_at") or ""
    sub_at = meta.get("submitted_at") or st.get("submitted_at")
    if sub_at and since and after(sub_at, since):
        return None
    if st.get("sent_back_at") or sub_at or (st.get("times_submitted") or 0) > 0:
        return since
    if "feedback_at_claim" in meta:
        return since if _fb_hash(fb) != (meta.get("feedback_at_claim") or "") else None
    at_claim = meta.get("studio_at_claim")
    if isinstance(at_claim, dict) and not at_claim:
        return since
    claimed, seen = ts(meta.get("claimed_at")), ts(st.get("feedback_at"))
    if claimed is not None and seen is not None and seen > claimed + CLAIM_SLACK_S:
        return since
    return None


def _job(jobs: list[dict], short: str, *kinds: str) -> dict | None:
    for j in jobs or []:
        if j.get("state") in ACTIVE_JOB and j.get("kind") in kinds and _job_short(j) == short:
            return j
    return None


def _job_short(j: dict) -> str | None:
    if j.get("short"):
        return j["short"]
    t = j.get("target") or ""
    return t[2:] if t.startswith("t:") else (t or None)


def details_complete(texts: dict, slot: dict) -> bool:
    keys = list(DETAIL_KEYS) + (["initial_state"] if slot.get("action") == "Operate" else [])
    return all((texts.get(k) or "").strip() for k in keys)


def ratings_complete(t: dict) -> bool:
    r = t.get("ratings") or {}
    return (r.get("one_shot") == "No" and bool(r.get("task_success")) and bool(r.get("steering"))
            and r.get("passes") is True and bool((t["texts"].get("validation_notes") or "").strip()))


def runs_of(t: dict, kind: str | None = None) -> list[dict]:
    rs = [r for r in t.get("runs") or [] if kind is None or r["kind"] == kind]
    return sorted(rs, key=lambda r: (r.get("created_at") or "", r.get("n") or 0))


def used(r: dict) -> bool:
    """A run counts once Claude started in it or it is still live; a lane set up and never used doesn't.
    `remote_live`: the run monitor sees its lane running on the PC (no local transcript or container)."""
    return bool(r.get("collected_at") or r.get("has_transcript") or r.get("live") or r.get("container_up")
                or r.get("remote_live"))


def submission_run(t: dict) -> dict | None:
    """The run the submission uses. `t["submission_run"]`, when the caller resolved it (data.py, by
    actions.submission_run, the one shared rule), wins. Without one: the chosen or submitted run, else the newest
    validation run Claude started in, so the checklist can say it isn't collected yet."""
    meta = t["meta"]
    resolved = t.get("submission_run")
    if resolved:
        r = next((r for r in t.get("runs") or [] if r["name"] == resolved), None)
        if r:
            return r
    want = meta.get("submission_run") or (meta.get("submission") or {}).get("run")
    if want:
        r = next((r for r in t.get("runs") or [] if r["name"] == want), None)
        if r:
            return r
    vs = [r for r in runs_of(t, "validate") if used(r)]
    return vs[-1] if vs else None


def decision(meta: dict, run: str | None) -> str | None:
    d = (meta.get("decisions") or {}).get(run or "") or {}
    return d.get("decision") if isinstance(d, dict) else d


def _qc_latest(meta: dict) -> dict | None:
    qs = [q for q in meta.get("qc") or [] if (q.get("spec") or "AutoQC") == "AutoQC"]
    return qs[-1] if qs else None


def stage(t: dict) -> dict:
    """t: {short, meta, slot, texts, ratings, runs, lane, jobs, changed_since_push}.

    Returns {stage, label, run, since, why}. `run` is the run the step is about (a decide or rate row
    names it in its id); `since` is when the state began, for ordering and reasons.
    """
    s = _stage(t)
    s["label"] = LABELS.get(s["stage"], s["stage"])
    return s


def _out(stage: str, run: str | None = None, since: str | None = None, **kw) -> dict:
    return {"stage": stage, "run": run, "since": since, **kw}


def _stage(t: dict) -> dict:
    meta, short = t["meta"], t["short"]
    lane, jobs = t.get("lane") or {}, t.get("jobs") or []
    # 1-2. Dropped, unclaimed.
    disc = meta.get("discard")
    if disc or meta.get("discarded"):
        disc = disc if isinstance(disc, dict) else {}
        return _out("discarded" if disc.get("logged_at") else "dropped", since=disc.get("at") or meta.get("discarded_at"))
    if meta.get("unclaimed_at"):
        return _out("unclaimed", since=meta["unclaimed_at"])
    # 3. Studio status, as the actions layer cached it.
    st = meta.get("studio") or {}
    status = st.get("status")
    if status == "Accepted":
        return _out("accepted", since=st.get("polled_at"))
    if status == "Rejected":
        return _out("rejected", since=st.get("polled_at"), feedback=st.get("feedback"))
    if status in ("Awaiting Review", "In Review"):
        # Without submitted_at the time is Studio's last poll, not the submit time ("checked").
        return _out("in_review", since=meta.get("submitted_at") or st.get("polled_at"), checked=not meta.get("submitted_at"))
    back = sent_back_since(meta)
    if back is not None:
        return _out("sent_back", since=back or None, feedback=st.get("feedback"))
    if not status and meta.get("submitted_at"):
        return _out("in_review", since=meta["submitted_at"])
    # 4. Claim or submit running.
    j = _job(jobs, short, "claim")
    if j:
        return _out("claiming", since=j.get("started_at") or j.get("created_at"))
    j = _job(jobs, short, "submit")
    if j:
        return _out("submitting", since=j.get("started_at") or j.get("created_at"))
    # 5. The lane is running one of this task's runs.
    lr = lane.get("run") or {}
    if lr.get("short") == short and lane.get("state") in LANE_ACTIVE:
        kind = lr.get("kind") or (lr.get("run") or "").split("-")[0]
        if lane["state"] in ("exited", "collecting"):
            return _out("collecting", run=lr.get("run"), since=lane.get("since"))
        return _out("validating" if kind == "validate" else "calibrating", run=lr.get("run"), since=lane.get("since"))
    live = [r for r in t.get("runs") or [] if r.get("live") or r.get("remote_live")]
    if live:
        r = live[-1]
        return _out("validating" if r["kind"] == "validate" else "calibrating", run=r["name"], since=r.get("created_at"))
    # A run that ended and isn't collected yet (any kind), newest first.
    latest = [r for r in runs_of(t) if used(r)]
    if latest and not latest[-1].get("collected_at"):
        r = latest[-1]
        if r.get("collecting") or _job(jobs, short, "lane_collect", "collect"):
            return _out("collecting", run=r["name"], since=r.get("ended_at") or r.get("created_at"))
        if r.get("has_transcript") and not r.get("container_up"):
            return _out("collect_failed", run=r["name"], since=r.get("ended_at") or r.get("created_at"))
        if r.get("has_transcript"):
            # Container still up, Claude gone: the lane watcher collects it once the transcript is quiet.
            return _out("collecting", run=r["name"], since=r.get("ended_at") or r.get("created_at"))
    # 6. The submission run is collected.
    sub = submission_run(t)
    if sub and sub.get("collected_at"):
        chosen = sub["kind"] == "calib"
        # Writing a field never moves the task on by itself (the surface would change under his hands):
        # the details and the ratings each wait for their Done (or an earlier save to Studio).
        saved = bool((meta.get("pushed") or {}).get("at"))
        if chosen and not (details_complete(t["texts"], t["slot"]) and (meta.get("details_ready") or meta.get("rated") or saved)):
            return _out("details", run=sub["name"], since=sub["collected_at"], chosen=True)
        if not ratings_complete(t) or not (meta.get("rated") or saved):
            return _out("rate", run=sub["name"], since=sub["collected_at"], chosen=chosen)
        if _job(jobs, short, "save", "studio_save"):
            return _out("saving", run=sub["name"])
        qc = _qc_latest(meta)
        if qc and qc.get("status") == "running":
            return _out("qc_running", run=sub["name"], since=qc.get("started_at"), qc=qc)
        pushed = meta.get("pushed") or {}
        changed = t.get("changed_since_push")
        if qc and qc.get("status") == "completed" and (qc.get("counts") or {}).get("fail"):
            if not changed:
                return _out("fix_qc", run=sub["name"], since=qc.get("finished_at") or qc.get("started_at"), qc=qc)
        elif (qc and qc.get("status") == "completed" and pushed.get("at") and changed == []
              and not after(pushed.get("at"), qc.get("started_at"))):
            return _out("ready_to_submit", run=sub["name"], since=qc.get("finished_at") or qc.get("started_at"), qc=qc)
        return _out("ready_to_save", run=sub["name"], since=pushed.get("at") or sub["collected_at"])
    # 8-9. Queued for the lane (a "ready" older than the newest run of its kind was used already).
    ready = meta.get("ready") or {}
    if ready.get("kind") in ("calib", "validate"):
        newest = [r for r in runs_of(t, ready["kind"]) if used(r)]
        if not newest or after(ready.get("at"), newest[-1].get("created_at")):
            return _out("waiting_validate" if ready["kind"] == "validate" else "waiting_calib", since=ready.get("at"),
                        kind=ready["kind"])
    # 10-11. After calibration.
    calibs = [r for r in runs_of(t, "calib") if used(r)]
    last_c = calibs[-1] if calibs else None
    dec = decision(meta, last_c["name"]) if last_c else None
    if last_c and last_c.get("collected_at") and dec is None:
        return _out("decide", run=last_c["name"], since=last_c["collected_at"])
    if dec == "drop":
        return _out("dropped", run=last_c["name"], since=((meta.get("decisions") or {}).get(last_c["name"]) or {}).get("at"))
    if dec == "keep" or meta.get("skipped_calibration"):
        kept_at = ((meta.get("decisions") or {}).get(last_c["name"]) or {}).get("at") if last_c else None
        return _out("details", run=last_c["name"] if last_c else None, since=kept_at or meta.get("skipped_calibration_at"),
                    skipped=bool(meta.get("skipped_calibration")) and dec != "keep")
    # 12. Write (or rework) the prompt.
    return _out("prompt", run=last_c["name"] if (last_c and dec == "rework") else None,
                since=meta.get("created_at"), rework=dec == "rework")
