"""johto: discovery, lanes, checks and Studio submission for Project Johto pre-work tasks.

Austin writes the prompt, goal, decision points, codebase notes and validation notes.
Everything else (finding slots and repos, claiming, lanes, collecting, gists, filling
Studio fields, AutoQC, submitting) is automated here.
"""
from __future__ import annotations

import argparse
import importlib
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

from . import actions, config, gists, lanes, lint, slots, studio, transcript, workspace
from .actions import ActionError, S


# --- helpers ----------------------------------------------------------------

def confirm(question: str, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"{question} [y/N] -> no (not a terminal; pass --yes)")
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def parse_slot(text: str) -> dict:
    parts = [p.strip() for p in text.split("|")]
    keys = ("action", "domain", "codebase_type", "prompt_style", "interaction_mode")
    return {k: (parts[i] if i < len(parts) and parts[i] else None) for i, k in enumerate(keys)}


def slot_matches(want: dict, have: dict) -> bool:
    return all(v is None or (have.get(k) or "").lower() == v.lower() for k, v in want.items())


LANG_MAP = actions.LANG_MAP
repo_info = actions.repo_info
load_lead = actions.load_lead


def task_dir_arg(name: str) -> Path:
    try:
        return workspace.resolve(name)
    except FileNotFoundError as e:
        raise SystemExit(str(e))


def print_qc(audit: dict) -> int:
    """Print an AutoQC outcome; return the number of failed dimensions."""
    fails = 0
    out = audit.get("outcome") or {}
    for sec in out.get("sections", []):
        for d in sec.get("dimensions", []):
            st = d.get("status")
            mark = {"pass": "pass", "fail": "FAIL", "neutral": "neut"}.get(st, st or "?")
            print(f"  {mark:5} {d.get('dimension')}")
            if st in ("fail", "neutral"):
                fails += st == "fail"
                j = d.get("justification") or d.get("analysis") or ""
                print(textwrap.indent(textwrap.fill(j, 100), "        "))
    if not out.get("sections"):
        print(f"  status={audit.get('status')} {audit.get('failure_reason') or ''}")
    return fails


# --- commands ---------------------------------------------------------------

def cmd_sync(a):
    s = slots.sync()
    print(f"{sum(g['count'] for g in s['groups'])} open slots in {len(s['groups'])} combinations "
          f"(saved {slots.slots_file().relative_to(config.ROOT)})")


def cmd_slots(a):
    s = slots.sync() if a.refresh else slots.load(max_age_s=3600)
    rows = [g for g in s["groups"] if not a.grep or a.grep.lower() in slots.slot_key(g["slot"]).lower()]
    print(f"Open slots (synced {s['synced_at']}):")
    for g in rows[: a.top]:
        print(f"  {g['count']:4}  {slots.slot_key(g['slot'])}")
    if len(rows) > a.top:
        print(f"  ... {len(rows) - a.top} more (use --top)")


STATUS_GROUPS = [("Writing (mine)", "mine"), ("Submitted", "submitted"), ("Accepted", "accepted"),
                 ("Rejected", "rejected")]
LOCAL_VIEW = {"Writing": "mine", "Awaiting Review": "submitted", "In Review": "submitted", "Accepted": "accepted",
              "Rejected": "rejected"}


def _focus_of(short: str, chats) -> dict | None:
    """focus.summary for a local task folder, or None (no folder, or the estimate failed)."""
    from . import focus
    d = config.TASKS / short
    if not (d / "task.json").exists():
        return None
    try:
        return focus.summary(d, chats=chats)
    except Exception:
        return None


def _local_status_tasks() -> dict[str, list[dict]]:
    """Task folders grouped like Studio's views, from the status task.json cached at the last Studio read."""
    out: dict[str, list[dict]] = {v: [] for _, v in STATUS_GROUPS}
    out["dropped"] = []
    for p in sorted(config.TASKS.glob("*/task.json")):
        try:
            meta = workspace.load(p.parent)
        except (OSError, ValueError):
            continue
        st = meta.get("studio") or {}
        view = "dropped" if meta.get("discard") or meta.get("discarded") or meta.get("unclaimed_at") \
            else LOCAL_VIEW.get(st.get("status") or "Writing", "mine")
        out[view].append({"task_name": meta["task_name"], "slot": meta.get("slot") or {},
                          "repo_url": (meta.get("repo") or {}).get("pinned_url"), "feedback": st.get("feedback"),
                          "polled_at": st.get("polled_at")})
    return out


def cmd_status(a):
    from . import focus
    chats = None
    try:
        chats = focus.chat_messages()
    except Exception:
        chats = {}
    if a.local:
        listed = _local_status_tasks()
        print("From the task folders (Studio status as of each task's last Studio read; drop --local to ask Studio).")
    else:
        me = actions.me()
        listed = {}
        for _, view in STATUS_GROUPS:
            ts = S().list_tasks(view=view)
            if view == "mine":
                ts = [t for t in ts if t.get("owned_by") == me]
            listed[view] = [{"task_name": t["task_name"], "slot": studio.slot_of(t),
                             "repo_url": (t.get("custom_fields") or {}).get("field_repo_url"),
                             "feedback": (t.get("custom_fields") or {}).get("field_review_writer_feedback")}
                            for t in ts]
    mins: dict[str, list[float]] = {}
    for label, view in STATUS_GROUPS + ([("Dropped or unclaimed", "dropped")] if listed.get("dropped") else []):
        ts = listed.get(view) or []
        print(f"\n{label}: {len(ts)}")
        for t in ts:
            short = workspace.short_name(t["task_name"])
            f = _focus_of(short, chats)
            you = f"  you {focus.fmt_minutes(f['minutes'])}" if f and f["method"] != "none" else ""
            if f:
                mins.setdefault(view, []).append(f["minutes"])
            print(f"  {t['task_name']:16} {slots.slot_key(t['slot'])}{'  [local]' if f is not None else ''}{you}")
            print(f"  {'':16} {t.get('repo_url') or '(no repo yet)'}")
            if t.get("feedback"):
                print(textwrap.indent(textwrap.fill("Reviewer: " + t["feedback"], 100), " " * 19))
    print()
    print(focus_totals(mins))
    print()


def focus_totals(mins: dict[str, list[float]]) -> str:
    """Austin's focused minutes per accepted task: every settled task's minutes (accepted, rejected, dropped)
    over the accepted ones, so time spent on tasks that didn't make it counts against the ones that did."""
    from . import focus
    settled = [m for v in ("accepted", "rejected", "dropped") for m in mins.get(v, [])]
    n_acc = len(mins.get("accepted", []))
    open_ = [m for v in ("mine", "submitted") for m in mins.get(v, [])]
    lines = []
    if n_acc:
        lines.append(f"Your focused time per accepted task: {focus.fmt_minutes(sum(settled) / n_acc)} "
                     f"({focus.fmt_minutes(sum(settled))} over {len(settled)} settled task"
                     f"{'s' if len(settled) != 1 else ''}, {n_acc} accepted).")
    elif settled:
        lines.append(f"No accepted task yet; {focus.fmt_minutes(sum(settled))} on settled tasks.")
    if open_:
        lines.append(f"Open tasks so far: {focus.fmt_minutes(sum(open_))} over {len(open_)} "
                     f"(writing and submitted).")
    if lines:
        lines.append("Estimated from your messages, lane prompts, Studio edits and johto commands unless you "
                     "used `johto focus <task> start|stop`; `johto focus <task>` shows how.")
    else:
        lines.append("No focused time recorded in the task folders yet.")
    return "\n".join(lines)


CLAIM_STEP_TEXT = {"claiming": "Claiming in Studio...", "saving_repo": "Saving repo name, pinned URL and language in Studio...",
                   "making_folder": "Making the task folder..."}


def cmd_start(a):
    """Claim a slot for a lead (or repo) and make the local task folder."""
    lead = load_lead(a.lead) if a.lead else None
    if lead:
        fit = lead.get("slot_fit") or {}
        want = {"action": fit.get("action"), "domain": fit.get("domain"),
                "codebase_type": fit.get("codebase_type"),
                "prompt_style": a.style, "interaction_mode": a.mode}
        repo = repo_info(lead["repo"], lead.get("commit"), a.lang or lead.get("primary_language"))
    else:
        if not a.repo:
            raise SystemExit("Pass --lead ID, or --repo owner/name with --slot or --task.")
        want = parse_slot(a.slot) if a.slot else {}
        if a.style:
            want["prompt_style"] = a.style
        if a.mode:
            want["interaction_mode"] = a.mode
        repo = repo_info(a.repo, a.commit, a.lang)
    for w in actions.check_repo_allowed(repo):
        print(f"warning: {w}")

    if a.draft:
        # No Studio: the folder alone. The slot is claimed later with `johto claim`, which also fills Studio.
        if not want.get("action"):
            raise SystemExit("A draft needs its seeds: pass --lead ID, or --slot 'Action | Domain | Codebase type'.")
        want["prompt_style"] = want.get("prompt_style") or "Casual prose"
        want["interaction_mode"] = want.get("interaction_mode") or "Async/autonomous"
        d = actions.draft(want, repo, lead)
        print(f"\nDraft: {d.name}  ({slots.slot_key(want)}; no Studio slot yet)")
        print(f"Repo:  {repo['pinned_url']}  [{repo['language']}]")
        if lead:
            print(f"Lead:  {lead.get('title')}")
        n = actions.draft_open(workspace.load(d))
        print(f"Open slots with these seeds right now: {n}")
        print(f"\nTask folder: {d}")
        print(f"  brief:      {d / 'brief.md'}")
        print(f"  write here: {d / 'submission.md'}")
        print(f"Next: write the Opening Prompt, then `johto lane {d.name} validate`. When a slot opens, `johto claim "
              f"{d.name}` claims it and saves everything to Studio.")
        return

    me = actions.me()
    if a.task:
        t = S().find_task(a.task)
        st = S().status_name(t)
        if st == "Writing" and t.get("owned_by") == me:
            claimed = True
        elif st == "Ready to Claim":
            claimed = False
        else:
            raise SystemExit(f"{t['task_name']} is {st}; can't start it.")
        if want and not slot_matches(want, S().slot_of(t)):
            print(f"warning: {t['task_name']} is {slots.slot_key(S().slot_of(t))}, not the lead's slot.")
    else:
        if not want.get("action"):
            raise SystemExit("Need a slot: pass --slot 'Action | Domain | Codebase type' or --task.")
        want.setdefault("prompt_style", None)
        want.setdefault("interaction_mode", None)
        if want["prompt_style"] is None:
            want["prompt_style"] = "Casual prose"
        avail = [t for t in S().list_tasks(view="available") if slot_matches(want, S().slot_of(t))]
        if not avail:
            raise SystemExit(f"No open slot for {want}. See `johto slots`.")
        # Without an explicit mode, prefer interactive slots (most of the pool).
        avail.sort(key=lambda t: S().slot_of(t)["interaction_mode"] != "Interactive/pairing")
        t, claimed = avail[0], False
        print(f"{len(avail)} open slots match; using {t['task_name']}.")
    slot = S().slot_of(t)
    print(f"\nTask:  {t['task_name']}  ({slots.slot_key(slot)})")
    print(f"Repo:  {repo['pinned_url']}  [{repo['language']}]")
    if lead:
        print(f"Lead:  {lead.get('title')}")
    if not claimed and not confirm("Claim this task in Studio?", a.yes):
        raise SystemExit("Not claimed.")
    d = actions.claim(t["task_id"], repo, lead, expect_status="Writing" if claimed else "Ready to Claim",
                      on_step=lambda step: CLAIM_STEP_TEXT.get(step) and print(CLAIM_STEP_TEXT[step]))
    print(f"\nTask folder: {d}")
    print(f"  brief:      {d / 'brief.md'}")
    print(f"  write here: {d / 'submission.md'}")
    print(f"Next: write the Opening Prompt, goal and decision points, then `johto lane {d.name} validate`.")
    if a.codebase_check:
        print("\nRunning Studio's Codebase Check (advisory)...")
        cmd_qc(argparse.Namespace(task=d.name, spec="Codebase Check", no_wait=False))


BIND_STEP_TEXT = {"recheck": "Checking the slot in Studio...", "claiming": "Claiming in Studio...",
                  "saving_repo": "Saving repo name, pinned URL and language in Studio...",
                  "binding": "Attaching the slot to the task folder..."}


def cmd_claim(a):
    """Claim an open slot for a draft, then save everything the folder holds to Studio."""
    d = task_dir_arg(a.task)
    meta = workspace.load(d)
    if not workspace.is_draft(meta):
        raise SystemExit(f"{d.name} already has a Studio slot ({meta['task_name']}). `johto push {d.name}` saves it.")
    slot = meta["slot"]
    hit = None
    if not a.task_id:
        hit = actions.open_slot_for(slot)
        if not hit:
            raise SystemExit(f"No open slot for {slots.slot_key(slot)} right now. `johto slots --refresh` looks again.")
    print(f"Task:   {a.task_id or hit['task_name']}  ({slots.slot_key(slot)})")
    print(f"Folder: {d}  ({meta.get('label') or meta['task_name']})")
    vrun, rec = actions.validation_run(d, None)
    has_run = bool(rec and rec.get("transcript") and rec.get("diff"))
    fields = actions.fields(d, None)
    print(f"Ready:  {len(fields)} fields" + (f", {vrun} collected" if has_run else ", no collected run yet"))
    if not confirm("Claim this slot in Studio and save everything there?", a.yes):
        raise SystemExit("Not claimed.")
    res = actions.bind(d, a.task_id, on_step=lambda step: BIND_STEP_TEXT.get(step) and print(BIND_STEP_TEXT[step]))
    print(f"Claimed {res['task_name']} for {d.name}.")
    if a.no_fill:
        print("Not saved (--no-fill). `johto push` saves the fields; `johto submit` does the rest.")
        return
    if has_run:
        g = actions.upload_gists(d)
        print("Gists: " + (", ".join(g["uploaded"]) + " uploaded" if g.get("uploaded") else "already current"))
    else:
        print("No collected run with a diff yet, so the gists wait.")
    pushed = actions.push(d, None, checked=True)
    print(f"Saved {pushed['saved']} fields to {res['task_name']}.")
    aid = actions.start_qc(d, "AutoQC", checked=True)
    print("AutoQC started." if aid else "Studio didn't start AutoQC; run `johto qc` later.")
    print(studio.task_url(res["task_id"]))


def cmd_lane(a):
    d = task_dir_arg(a.task)
    # --stop-others is your explicit flag: it stops other lanes even with Claude running (files are kept).
    # A validation lane checks the prompt first (actions.lane_gate): --again, --qc, --skip-qc.
    run_dir = actions.start_lane(d, a.kind, log=print, again=a.again, qc=a.qc, skip_qc=a.skip_qc,
                                 stop_others=a.stop_others, need_prompt=True)
    sub = workspace.parse_submission(d)
    prompt = sub["sections"].get("Opening Prompt", "")
    print(f"\nLane ready: {run_dir}")
    if prompt and lanes.copy_to_clipboard(prompt):
        print(f"Your Opening Prompt ({lint.words(prompt)} words) is on the clipboard.")
    elif not prompt:
        print("No Opening Prompt in submission.md yet; write it before you start Claude.")
    print(lanes.launch_hint(run_dir))
    mode = workspace.load(d)["slot"]["interaction_mode"]
    if mode.startswith("Async"):
        print("Async slot: paste once and let it run to the end. No follow-ups.")
    else:
        print("Interactive slot: reply like a colleague. Answer questions; don't hand it the fix.")
    print(f"When done: /exit, then `johto collect {d.name} {run_dir.name}`.")
    if not a.no_go and sys.stdin.isatty():
        input("\nPress Enter to start Claude in the lane (Ctrl+C to stop here)... ")
        _exec_lane(run_dir)


def _exec_lane(run_dir: Path):
    import os
    os.chdir(run_dir / "model_a")
    args = ["./claude-dev", *lanes.LAUNCH_ARGS.split()]
    os.execv("./claude-dev", args)


def cmd_resume(a):
    d = task_dir_arg(a.task)
    run = a.run or max((p for p in (d / "runs").glob("*-*") if p.is_dir()),
                       key=lambda p: p.stat().st_mtime, default=None)
    if run is None:
        raise SystemExit("No runs yet.")
    run_dir = d / "runs" / (run if isinstance(run, str) else run.name)
    rec = actions.run_record(d, run_dir.name) or {}
    if lanes.remote_of(rec):
        # A lane on another machine (the PC): reattach over ssh. This Mac's lanes don't hold its ports.
        import os
        import shlex
        cmd = actions.resume_command(d, run_dir.name, rec)
        print(f"{run_dir.name} runs on {rec['host']}: {cmd}")
        os.execvp("ssh", shlex.split(cmd))
        return
    me = (lanes.manifest(run_dir).get("lane") or {}).get("container")
    running = lanes.running_lanes()
    slot = lanes.run_slot(actions.run_record(d, run_dir.name))
    # Only a lane on this run's slot holds its ports; one on the other slot can keep running.
    busy = [] if me in running else [c for c in running if c != me and lanes.container_slot(c) in (slot, None)]
    if busy:
        print(f"Another lane holds this lane's ports ({lanes.slot_label(slot)}): " + ", ".join(busy))
        if not confirm("Stop them? Their files are kept.", False):
            raise SystemExit(1)
        for c in busy:
            subprocess.run(["docker", "stop", c], capture_output=True)
    _exec_lane(run_dir)


def cmd_collect(a):
    d = task_dir_arg(a.task)
    if not (a.run or actions.latest_run(d)):
        raise SystemExit("No runs yet. Start one with `johto lane <task> calib`.")
    info = actions.collect(d, a.run, log=print)
    run = info["run"]
    if info["note"]:
        print(f"note: {info['note']}")
    print(f"\n{run}: {info['diff_stat']}")
    if info["summary"]:
        print(transcript.render(info["summary"]))
        for p in info["problems"]:
            print(f"  PROBLEM: {p}")
        print(f"\nSaved {d / 'runs' / run / 'summary.md'}")
        if run.startswith("calib"):
            print("Calibration notes go in your own words. If it one-shot the task, drop it: "
                  f"`johto discard {d.name}`.")


def _run_arg(a, d: Path) -> str | None:
    run = getattr(a, "run", None)
    if run and not (d / "runs" / run).is_dir():
        raise SystemExit(f"No run {run} in {d}")
    return run


def cmd_check(a):
    """`johto check`: the saved-draft check (johto/draftcheck.py). It covers the old lint and run checks, reads
    Studio's saved text, and returns the old (level, field, message) list so `submit` can stop on errors."""
    from . import draftcheck
    return draftcheck.cmd_check(a)


def cmd_pull(a):
    d = task_dir_arg(a.task)
    res = actions.pull_from_studio(d)
    if res["status"] != "Writing":
        print(f"{d.name} is {res['status']} in Studio; nothing to bring in.")
        return
    print(f"Brought in from Studio: {', '.join(res['pulled'])}" if res["pulled"] else "Nothing new in Studio.")
    for k, why in res["conflicts"].items():
        print(f"  {k}: {why}; left both copies alone")


def cmd_push(a):
    d = task_dir_arg(a.task)
    run = _run_arg(a, d)
    meta = workspace.load(d)
    for k, v in actions.fields(d, run).items():
        s = v if isinstance(v, str) else json.dumps(v)
        print(f"  {k:34} {s[:70].replace(chr(10), ' ')}{'...' if len(s) > 70 else ''}")
    res = actions.push(d, run)
    if res.get("pulled"):
        print(f"Brought in Studio edits first: {', '.join(res['pulled'])}")
    print(f"Saved {res['saved']} fields to {meta['task_name']} (not submitted).")


def cmd_qc(a):
    d = task_dir_arg(a.task)
    audit_id = actions.start_qc(d, a.spec)
    print(f"{a.spec} started.")
    if a.no_wait or not audit_id:
        q = actions.latest_qc(workspace.load(d), a.spec) or {}
        if q.get("dedup"):
            print(f"Studio reused a previous run: {q['dedup']}")
        return 0
    res = actions.poll_qc(d, audit_id)
    print(f"{a.spec}: {res['audit'].get('status')}")
    return print_qc(res["audit"])


def cmd_submit(a):
    d = task_dir_arg(a.task)
    run = _run_arg(a, d)
    meta = workspace.load(d)
    actions.need_slot(meta, d)
    problems = cmd_check(a)
    errors = [p for p in problems if p[0] == lint.ERROR]
    if errors and not a.force:
        raise SystemExit(f"\n{len(errors)} errors. Fix them (or --force to submit anyway).")
    t = S().task(meta["task_id"])
    if S().status_name(t) != "Writing":
        raise SystemExit(f"{meta['task_name']} is {S().status_name(t)}, not Writing.")
    run, rec = actions.validation_run(d, run)
    if not rec or not rec.get("transcript") or not rec.get("diff"):
        raise SystemExit("Need a collected validation run (transcript and diff) before submitting.")
    need = actions.gist_state(d, run)["need"]
    print(f"\nValidation run: {run}  ({lanes.diff_stat((rec or {}).get('diff'))})")
    if need:
        print(f"Will upload as secret gists on github.com/{gists.GH_ACCOUNT}: "
              + ", ".join(Path(rec[k2]).name for k2 in ("transcript", "diff") if rec.get(k2)))
    if not confirm("Upload gists, save every field and run AutoQC?", a.yes):
        raise SystemExit("Stopped before uploading.")
    short = d.name
    g = actions.upload_gists(d, run)["gists"]
    print(f"  Conversation Log: {g['conversation_log']}\n  Final Output:     {g['final_output']}")
    cmd_push(argparse.Namespace(task=a.task, run=run))
    print("\nRunning AutoQC (a few minutes)...")
    fails = cmd_qc(argparse.Namespace(task=a.task, spec="AutoQC", no_wait=False))
    if fails and not a.force:
        raise SystemExit(f"\nAutoQC failed {fails} checks. Fix the text and rerun `johto submit {short}`, "
                         "or resolve them in Studio. Submit needs AutoQC failures resolved.")
    ts = actions.trajectory_state(d, run)
    if ts["status"] == "uploaded":
        print(f"Trajectory Logs: {run}'s transcript is in Studio.")
    else:
        print(f"\nTrajectory Logs: {actions.trajectory_row(d, run)['detail']}\n"
              "Upload it now if you haven't; submitting reads Studio again first.")
    if not confirm(f"\nSubmit {meta['task_name']} for review? This can't be undone from the writer side.", a.yes_submit):
        print(f"Saved but not submitted. Review it at {studio.task_url(meta['task_id'])}")
        return
    try:
        actions.submit(d)
    except studio.StudioError as e:
        raise SystemExit(f"Studio refused the submit: {e.body}\nOpen {studio.task_url(meta['task_id'])}")
    print(f"Submitted. {studio.task_url(meta['task_id'])}")
    print(f"Free the disk when you're done with it: `johto clean {short}`.")


def cmd_submission_run(a):
    d = task_dir_arg(a.task)
    if a.run or a.clear:
        res = actions.set_submission_run(d, None if a.clear else a.run)
        print(f"{d.name} submits {res['run'] or '(no run yet)'}" + ("" if res["submission_run"] else " (the latest validation run)"))
    for r in actions.runs_overview(d):
        mark = "*" if r["submitted"] else " "
        state = "collected" if r["collected"] else "not collected"
        print(f" {mark} {r['run']:12} {state:14} {r['human_turns'] or '-'} turns  {r['active_minutes'] or '-'} min")


def cmd_discard(a):
    d = task_dir_arg(a.task)
    meta = workspace.load(d)
    print(f"Discard {meta['task_name']} ({meta['repo'].get('pinned_url')}).")
    ok = confirm("Unclaim it in Studio so the slot goes back to the pool?", a.yes)
    res = actions.discard(d, a.reason, unclaim_first=ok)
    if res["unclaimed"]:
        print("Unclaimed.")
    print("\nLog it in the Discarded Run Log (pre-filled; you write the three short answers):")
    print(res["url"])
    print("Removed this task's lanes." + "".join(f"\n  kept {x['run']}: {x['reason']}" for x in res["skipped"]))


DISCARD_REASONS = actions.DISCARD_REASONS
discard_link = actions.discard_link


def cmd_clean(a):
    d = task_dir_arg(a.task)
    res = actions.clean(d, a.run)
    counts: dict[str, int] = {}
    for x in res["removed"]:
        counts[x.split(":", 1)[0]] = counts.get(x.split(":", 1)[0], 0) + 1
    runs = [a.run] if a.run else sorted(p.name for p in (d / "runs").glob("*-*") if p.is_dir())
    for run in runs:
        skip = next((x for x in res["skipped"] if x["run"] == run), None)
        print(f"{run}: " + (f"kept ({skip['reason']})" if skip else f"removed {counts.get(run, 0)} lane resources"))


def cmd_pack(a):
    from . import pack
    d = task_dir_arg(a.task)
    from . import settings
    ai = False if a.no_ai else (True if a.ai else bool(settings.get("pack_outside_review")))
    out = pack.build(d, a.run, max_tokens=a.max_tokens, ai=ai)
    if a.copy:
        subprocess.run(["pbcopy"], input=out.read_text(), text=True)
        print("Copied to the clipboard.")
    subprocess.run(["open", "-R", str(out)])


def cmd_open(a):
    d = task_dir_arg(a.task)
    meta = workspace.load(d)
    actions.need_slot(meta, d)
    print(studio.task_url(meta["task_id"]))


# Modules that add their own commands with register(sub). One that doesn't exist yet is skipped.
MODULES = ("budget", "reviews", "turnwatch", "rules", "where", "kit", "grading", "verify", "coverage", "deck", "focus",
           "slot_options", "understand", "feedback")


def register_modules(sub, names=MODULES) -> list[str]:
    """Import johto.<name> for each name and call its register(sub). Only a missing module is skipped; an
    error inside one stops the CLI, so it gets fixed. A command name already taken is reported and skipped."""
    done = []
    for name in names:
        full = f"{__package__ or 'johto'}.{name}"
        try:
            mod = importlib.import_module(full)
        except ModuleNotFoundError as e:
            if e.name == full:
                continue
            raise
        reg = getattr(mod, "register", None)
        if reg is None:
            continue
        try:
            reg(sub)
        except ValueError as e:   # argparse: conflicting subparser
            if "conflicting subparser" not in str(e):
                raise
            print(f"johto: {name} wants a command name that is already taken ({e}); skipped it.", file=sys.stderr)
            continue
        done.append(name)
    return done


def main(argv=None):
    p = argparse.ArgumentParser(prog="johto", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="pull all Studio tasks and open slots")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("slots", help="open slots grouped by seeds")
    s.add_argument("--grep", help="filter, e.g. 'Personal-scale' or 'Games'")
    s.add_argument("--top", type=int, default=40)
    s.add_argument("--refresh", action="store_true")
    s.set_defaults(func=cmd_slots)

    s = sub.add_parser("status", help="your tasks in Studio, with reviewer feedback and your focused minutes")
    s.add_argument("--local", action="store_true",
                   help="read the task folders' cached Studio status instead of asking Studio")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("start", help="claim a slot for a lead or repo; make the task folder")
    s.add_argument("--lead", help="lead id from `johto next`")
    s.add_argument("--repo", help="owner/name (instead of --lead)")
    s.add_argument("--commit", help="commit to pin (default: HEAD of default branch)")
    s.add_argument("--slot", help="'Action | Domain | Codebase type[ | Prompt style | Mode]'")
    s.add_argument("--task", help="a specific Studio task (name or id) instead of auto-pick")
    s.add_argument("--style", help="prompt style (default Casual prose)")
    s.add_argument("--mode", help="interaction mode")
    s.add_argument("--lang", help="primary language override")
    s.add_argument("--codebase-check", action="store_true", help="run Studio's Codebase Check after")
    s.add_argument("--draft", action="store_true",
                   help="make the task folder without claiming a slot (no Studio); `johto claim` later claims one "
                        "and saves everything. Needs the full slot: --lead or --slot, with --style and --mode "
                        "(default Casual prose, Async/autonomous)")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("claim", help="claim an open slot for a draft task, then save every field and the gists to Studio")
    s.add_argument("task")
    s.add_argument("--task-id", help="a specific open Studio task (id) instead of the first open one with the seeds")
    s.add_argument("--no-fill", action="store_true", help="claim only; don't save the fields and gists")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_claim)

    s = sub.add_parser("lane", help="set up a fresh lane (new folder) for a run")
    s.add_argument("task")
    s.add_argument("kind", choices=lanes.KINDS)
    s.add_argument("--stop-others", action="store_true",
                   help="stop every other running lane first (otherwise a second lane starts beside one running lane)")
    s.add_argument("--no-go", action="store_true", help="set up only; don't start Claude")
    s.add_argument("--again", action="store_true",
                   help="validate: start a new run even though an earlier one used the same prompt")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--qc", action="store_true",
                   help="validate: bring Studio's edits in, save the prompt if only yours changed, run AutoQC, "
                        "then set up the lane if the prompt passes")
    g.add_argument("--skip-qc", action="store_true", help="validate: start without the prompt AutoQC check")
    s.set_defaults(func=cmd_lane)

    s = sub.add_parser("resume", help="re-enter a lane's Claude session")
    s.add_argument("task")
    s.add_argument("run", nargs="?", help="e.g. validate-1 (default: most recent)")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("collect", help="collect a run's diff + transcript and summarize it")
    s.add_argument("task")
    s.add_argument("run", nargs="?", help="e.g. calib-1 (default: latest validate, else calib)")
    s.set_defaults(func=cmd_collect)

    from . import draftcheck
    s = sub.add_parser("check", help="check the saved draft: Studio's text, uploads, minutes, the run, lint; "
                                     "--deep adds AI flags")
    s.add_argument("task")
    s.add_argument("--run", help="the run to submit (default: the chosen run, else latest validate-N), e.g. calib-1")
    draftcheck.add_check_args(s)
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("pull", help="bring fields you edited in Studio into submission.md")
    s.add_argument("task")
    s.set_defaults(func=cmd_pull)

    s = sub.add_parser("push", help="save submission.md fields to Studio without submitting")
    s.add_argument("task")
    s.add_argument("--run", help="the run whose minutes are reported (default: the chosen or latest validate-N)")
    s.set_defaults(func=cmd_push)

    s = sub.add_parser("submission-run", help="choose which collected run is submitted (e.g. calib-1)")
    s.add_argument("task")
    s.add_argument("run", nargs="?", help="e.g. calib-1; omit to list the runs")
    s.add_argument("--clear", action="store_true", help="go back to the latest validation run")
    s.set_defaults(func=cmd_submission_run)

    s = sub.add_parser("qc", help="run Studio AutoQC (or Codebase Check) and show results")
    s.add_argument("task")
    s.add_argument("--spec", default="AutoQC", choices=list(config.QC_SPECS))
    s.add_argument("--no-wait", action="store_true")
    s.set_defaults(func=cmd_qc)

    s = sub.add_parser("submit", help="check, upload gists, fill Studio, AutoQC, then submit")
    s.add_argument("task")
    s.add_argument("--run", help="the run to submit (default: the chosen run, else latest validate-N), e.g. calib-1")
    s.add_argument("--yes", action="store_true", help="skip the upload/save confirmation")
    s.add_argument("--yes-submit", action="store_true", help="also skip the final submit confirmation")
    s.add_argument("--force", action="store_true", help="continue past lint errors and AutoQC failures")
    s.set_defaults(func=cmd_submit)

    s = sub.add_parser("discard", help="unclaim, log as discarded, remove lanes")
    s.add_argument("task")
    s.add_argument("--reason", default="easy", help="easy | unfair | repo | slow (default easy)")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_discard)

    s = sub.add_parser("clean", help="remove a task's lane containers, volumes and lane logins")
    s.add_argument("task")
    s.add_argument("run", nargs="?")
    s.set_defaults(func=cmd_clean)

    s = sub.add_parser("pack", help="make a review pack (repomix, <= 200k tokens) of a run for pre-grading")
    s.add_argument("task")
    s.add_argument("run", nargs="?", help="e.g. calib-1 (default: most recent; works mid-run)")
    s.add_argument("--max-tokens", type=int, default=200_000)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--ai", action="store_true",
                   help="add the outside Claude review (default: settings pack_outside_review, off)")
    g.add_argument("--no-ai", action="store_true", help="skip the outside Claude review")
    s.add_argument("--copy", action="store_true", help="copy the pack to the clipboard")
    s.set_defaults(func=cmd_pack)

    s = sub.add_parser("web", help="start the local web app")
    s.add_argument("--port", type=int, default=config.PORT)
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(func=lambda a: __import__("johto.web.app", fromlist=["run"]).run(a.port, not a.no_open))

    s = sub.add_parser("open", help="print the Studio URL for a task")
    s.add_argument("task")
    s.set_defaults(func=cmd_open)

    try:
        from .scout import cli as scout_cli  # added by the scout module
        scout_cli.register(sub)
    except ImportError:
        pass
    try:
        from .dossier import cli as dossier_cli; dossier_cli.register(sub)  # noqa: E702  (dossier engine)
    except ImportError:
        pass
    register_modules(sub)
    s = sub.add_parser("grading-plain", help="write the grading table's findings in plain words "
                                              "(also `johto grading plain <task>`)")
    s.add_argument("task")
    s.add_argument("--force", action="store_true", help="write every row again, not only the missing ones")
    s.set_defaults(func=lambda a: __import__("johto.plain", fromlist=["cmd_plain"]).cmd_plain(a))

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:2] == ["grading", "plain"]:   # `johto grading plain <task>` (johto/plain.py)
        argv = ["grading-plain", *argv[2:]]
    a = p.parse_args(argv)
    try:
        a.func(a)
    except ActionError as e:
        raise SystemExit(e.message)
    except studio.StudioError as e:
        raise SystemExit(f"Studio error {e.status} on {e.path}: {e.body}")
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
