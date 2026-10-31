"""johto: discovery, lanes, checks and Studio submission for Project Johto pre-work tasks.

Austin writes the prompt, goal, decision points, codebase notes and validation notes.
Everything else (finding slots and repos, claiming, lanes, collecting, gists, filling
Studio fields, AutoQC, submitting) is automated here.
"""
from __future__ import annotations

import argparse
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
          f"(saved {slots.SLOTS_FILE.relative_to(config.ROOT)})")


def cmd_slots(a):
    s = slots.sync() if a.refresh else slots.load(max_age_s=3600)
    rows = [g for g in s["groups"] if not a.grep or a.grep.lower() in slots.slot_key(g["slot"]).lower()]
    print(f"Open slots (synced {s['synced_at']}):")
    for g in rows[: a.top]:
        print(f"  {g['count']:4}  {slots.slot_key(g['slot'])}")
    if len(rows) > a.top:
        print(f"  ... {len(rows) - a.top} more (use --top)")


def cmd_status(a):
    me = actions.me()
    groups = [("Writing (mine)", "mine"), ("Submitted", "submitted"), ("Accepted", "accepted"),
              ("Rejected", "rejected")]
    for label, view in groups:
        ts = S().list_tasks(view=view)
        if view == "mine":
            ts = [t for t in ts if t.get("owned_by") == me]
        print(f"\n{label}: {len(ts)}")
        for t in ts:
            cf = t.get("custom_fields") or {}
            local = (config.TASKS / workspace.short_name(t["task_name"])).exists()
            print(f"  {t['task_name']:16} {slots.slot_key(studio.slot_of(t))}"
                  f"{'  [local]' if local else ''}")
            print(f"  {'':16} {cf.get('field_repo_url') or '(no repo yet)'}")
            fb = cf.get("field_review_writer_feedback")
            if fb:
                print(textwrap.indent(textwrap.fill("Reviewer: " + fb, 100), " " * 19))
    print()


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


def cmd_lane(a):
    d = task_dir_arg(a.task)
    if a.stop_others:  # your explicit flag: stops other lanes even with Claude running (files are kept)
        try:
            run_dir = lanes.new_lane(d, a.kind, stop_others=True)
        except RuntimeError as e:
            raise SystemExit(str(e))
    else:
        run_dir = actions.start_lane(d, a.kind, log=print)
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
    d = task_dir_arg(a.task)
    run = _run_arg(a, d)
    meta = workspace.load(d)
    problems = actions.check(d, run)
    vrun, _ = actions.validation_run(d, run)
    print(f"{d.name}: {meta['task_name']}  {slots.slot_key(meta['slot'])}" + (f"  (run {vrun})" if vrun else ""))
    print(lint.render(problems))
    return problems


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
    g = meta.get("gists") or {}
    stale = bool(g.get("run") and g["run"] != run)
    need = [k for k in ("conversation_log", "final_output") if stale or not g.get(k)]
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
    out = pack.build(d, a.run, max_tokens=a.max_tokens, ai=not a.no_ai)
    if a.copy:
        subprocess.run(["pbcopy"], input=out.read_text(), text=True)
        print("Copied to the clipboard.")
    subprocess.run(["open", "-R", str(out)])


def cmd_open(a):
    d = task_dir_arg(a.task)
    meta = workspace.load(d)
    print(studio.task_url(meta["task_id"]))


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

    s = sub.add_parser("status", help="your tasks in Studio, with reviewer feedback")
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
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("lane", help="set up a fresh lane (new folder) for a run")
    s.add_argument("task")
    s.add_argument("kind", choices=lanes.KINDS)
    s.add_argument("--stop-others", action="store_true",
                   help="stop every other running lane first (otherwise a second lane starts beside one running lane)")
    s.add_argument("--no-go", action="store_true", help="set up only; don't start Claude")
    s.set_defaults(func=cmd_lane)

    s = sub.add_parser("resume", help="re-enter a lane's Claude session")
    s.add_argument("task")
    s.add_argument("run", nargs="?", help="e.g. validate-1 (default: most recent)")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("collect", help="collect a run's diff + transcript and summarize it")
    s.add_argument("task")
    s.add_argument("run", nargs="?", help="e.g. calib-1 (default: latest validate, else calib)")
    s.set_defaults(func=cmd_collect)

    s = sub.add_parser("check", help="lint submission.md and the validation run")
    s.add_argument("task")
    s.add_argument("--run", help="the run to submit (default: the chosen run, else latest validate-N), e.g. calib-1")
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
    s.add_argument("--no-ai", action="store_true", help="skip the outside Claude review")
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
