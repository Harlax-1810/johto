"""The Studio checklist (design 3.6, build-plan 9.3), failures first.

Used when the actions layer doesn't provide its own rows. Rows state facts; none offers wording.
"""
from __future__ import annotations

from .queue import clock, run_word

QC_FIELD = {
    "Prompt Style Fit": "opening_prompt", "Nothing Private Leaks": "opening_prompt", "Doesn't Solve It": "opening_prompt",
    "No Formatting Tells": "opening_prompt", "Task Scope": "opening_prompt", "Seeded Domain Match": "opening_prompt",
    "Seeded Action Match": "opening_prompt", "High-Level Task Goal": "goal", "Decision Points": "decision_points",
    "Difficulty & Codebase Understanding": "codebase", "Codebase Eligibility": None,
}
HEADING_HINTS = [("opening prompt", "opening_prompt"), ("goal", "goal"), ("decision point", "decision_points"),
                 ("codebase", "codebase"), ("validation", "validation_notes"), ("reviewer", "reviewer_notes")]
NAMES = {"opening_prompt": "Opening Prompt", "goal": "High-Level Task Goal", "decision_points": "Decision Points",
         "codebase": "Difficulty & Codebase Understanding", "initial_state": "Initial State",
         "validation_notes": "Validation Notes", "reviewer_notes": "Notes for the Reviewer", "ratings": "Ratings",
         "gists": "Gist links"}


def field_for_dimension(dimension: str, justification: str = "") -> str | None:
    if dimension in QC_FIELD:
        return QC_FIELD[dimension]
    if dimension == "AI Detection":
        j = (justification or "").lower()
        return next((k for h, k in HEADING_HINTS if h in j), None)
    return None


def _row(key, status, title, detail="", blocking=False, fix=None) -> dict:
    return {"key": key, "status": status, "title": title, "detail": detail, "blocking": blocking, "fix": fix}


def rows(*, slot: dict, sections: dict, ratings: dict, sub_run: dict | None, qc: dict | None, pushed: dict | None,
         changed: list | None, gists: dict, upstream: list, lint_flags: list, reviewer_notes_rev_at: str | None = None,
         now: float | None = None) -> list[dict]:
    out = []
    # AutoQC
    if qc is None:
        out.append(_row("autoqc", "pending", "AutoQC has not run", "Save to Studio runs it", True))
    elif qc.get("status") == "running":
        out.append(_row("autoqc", "pending", f"AutoQC running since {clock(qc.get('started_at'), now)}", "", True))
    else:
        fails = [d for d in qc.get("dims") or [] if d.get("status") == "fail"]
        for d in fails:
            fk = d.get("field_key") or field_for_dimension(d.get("dimension", ""), d.get("justification", ""))
            justified = bool((sections.get("reviewer_notes") or {}).get("text", "").strip()) and \
                "reviewer_notes" in (changed or [])
            fixed = fk is not None and fk in (changed or [])
            out.append(_row("autoqc", "warn" if (fixed or justified) else "fail",
                            f"AutoQC {clock(qc.get('finished_at') or qc.get('started_at'), now)} · {d.get('dimension')}",
                            d.get("justification") or "", not (fixed or justified),
                            {"action": "focus_field", "field": fk, "span": None} if fk else None))
        if not fails:
            c = qc.get("counts") or {}
            out.append(_row("autoqc", "pass", f"AutoQC passed {c.get('pass', 0)} checks",
                            f"at {clock(qc.get('finished_at') or qc.get('started_at'), now)}"))
    # Studio sync
    if pushed and pushed.get("at"):
        if changed:
            out.append(_row("studio_sync", "warn", f"Saved to Studio {clock(pushed['at'], now)}",
                            f"{len(changed)} changed since: " + ", ".join(NAMES.get(k, k) for k in changed)))
        else:
            out.append(_row("studio_sync", "pass", f"Saved to Studio {clock(pushed['at'], now)}", "nothing changed since"))
    else:
        out.append(_row("studio_sync", "pending", "Not saved to Studio yet", "", False))
    # Ratings
    ct = slot.get("codebase_type") or ""
    if ratings.get("one_shot") and ratings.get("task_success") and ratings.get("steering"):
        msg = f"{ct} · Task Success {ratings['task_success']} · Steering {ratings['steering']}"
        if ratings.get("passes"):
            out.append(_row("ratings", "pass", "Ratings pass", msg))
        else:
            errs = [c["message"] for c in ratings.get("check") or [] if c.get("level") == "error"]
            out.append(_row("ratings", "fail", "Ratings fail the pass grid", errs[0] if errs else msg, True,
                            {"action": "focus_field", "field": "ratings", "span": None}))
    else:
        out.append(_row("ratings", "fail", "Ratings not set", "One-shot, Task Success and Steering", True,
                        {"action": "focus_field", "field": "ratings", "span": None}))
    # Validation run
    if not sub_run:
        out.append(_row("validation_run", "fail", "No validation run", "", True))
    elif not sub_run.get("collected_at"):
        out.append(_row("validation_run", "fail", f"{run_word(sub_run['name']).capitalize()} not collected", "", True))
    else:
        f = sub_run.get("facts") or {}
        bits, bad = [], []
        if f.get("model_ok") and f.get("effort_ok") is not False:
            bits.append("Opus 5.5 · high")
        else:
            bad.append("model or effort wrong")
        mode_async = (slot.get("interaction_mode") or "").startswith("Async")
        if mode_async:
            if (f.get("turns") or 0) > 1:
                bad.append(f"{f.get('turns')} prompts in an async run")
            else:
                bits.append("1 prompt (async)")
        if f.get("active_min") is not None:
            bits.append(f"{f['active_min']:g} min")
        if f.get("first_prompt_matches") is True:
            bits.append("sent prompt = Opening Prompt")
        elif f.get("first_prompt_matches") is False:
            bad.append("sent prompt differs from the Opening Prompt")
        if (f.get("transcripts") or 0) > 1:
            bad.append(f"{f['transcripts']} transcripts in its lane")
        elif f.get("transcripts") == 1:
            bits.append("its own lane")
        title = run_word(sub_run["name"]).capitalize()
        if bad:
            out.append(_row("validation_run", "fail", title, " · ".join(bad + bits),
                            any("model" in b or "async" in b for b in bad)))
        else:
            out.append(_row("validation_run", "pass", title, " · ".join(bits)))
    # Provenance (the guard fills overlaps; without them there is nothing to show)
    red = [o for s in sections.values() for o in (s.get("overlap") or []) if o.get("level") == "red" and not o.get("accepted")]
    if red:
        out.append(_row("provenance", "fail", "Your words", f"{len(red)} span{'s' if len(red) != 1 else ''} match AI text", True))
    # Gists
    have = [n for k, n in (("conversation_log", "conversation log"), ("final_output", "final diff")) if (gists or {}).get(k)]
    if len(have) == 2:
        out.append(_row("gists", "pass", "Gists", "conversation log, final diff (secret)"))
    else:
        out.append(_row("gists", "pending", "Gists", "uploaded as secret gists on save"))
    # Lint
    errs = [f for f in lint_flags if f.get("level") == "error"]
    warns = [f for f in lint_flags if f.get("level") != "error"]
    out.append(_row("lint", "warn" if (errs or warns) else "pass", "Local checks",
                    f"{len(errs)} error{'s' if len(errs) != 1 else ''} · {len(warns)} warning{'s' if len(warns) != 1 else ''}"))
    # Upstream disclosure
    missing = [u for u in upstream or [] if not u.get("mentioned")]
    if missing:
        out.append(_row("upstream", "warn", "Upstream work not mentioned",
                        "Mention in Difficulty or reviewer notes: " + ", ".join(u.get("url") or "" for u in missing),
                        False, {"action": "focus_field", "field": "codebase", "span": None}))
    order = {"fail": 0, "pending": 1, "warn": 2, "pass": 3}
    return sorted(out, key=lambda r: order.get(r["status"], 9))
