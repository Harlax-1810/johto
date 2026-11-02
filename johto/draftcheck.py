"""The draft check: what stops Austin's saved draft from being a faithful, supported account of the task and run.

GPT Pro's review (docs/reviews/2026-10-31-gptpro-ux.md, section 3E) asked for one whole-draft check after the
fields are written, then checks of only what changed. `johto check <t>` runs it and the web app's Check view
shows the same stored result (run, latest).

Two parts:

- Deterministic, every time. The exact text Studio saved (app/studio-fields.json) against the local copy in
  submission.md and against the previous check; lint.check_saved_fields; required fields; the Trajectory Logs
  upload and whether it holds the submission run's transcript; Claude minutes in Studio against the run's
  working minutes; the run itself (collected, model and effort, one prompt when async, the same prompt); the
  grading rows Austin marked; and the existing lint rules, run on the saved text.
- Deep, on request (deep=True): one headless Claude run on the critique-only route below. For fields unchanged
  since the last AI check its issues are reused and only changed fields (and how they relate to the rest) are
  checked again, unless full=True.

Results come in four groups, in this order: needs attention, resolved since the last check, needs your
decision, optional. Every issue names a field, a verbatim span of Austin's saved text when there is one, a
category, its evidence (a rule id, grading row, file:line or run turn) and its consequence. Never wording.

The critique-only route. docs/design.md section 8 keeps Austin's protected text away from model jobs; this is the
one documented exception. The deep check sends his saved fields, the grading rows he marked, measured run facts,
the condensed run and the rules for those fields to a headless Claude with no tools, under a system prompt that
allows only flags. The output schema has issue locations, categories, evidence and consequences, and no field
for replacement text. A post-filter then drops any issue whose consequence or evidence suggests wording,
advises a rating or leans on a grading row he did not mark, and counts what it dropped. Nothing else may use
this route to send protected text to a model.

Every check is stored as tasks/<t>/checks/draft-<stamp>.json (the full result plus the text it read, for the
next check's delta) and .md, which holds only the AI-written consequences and evidence. In the .md Austin's own
words are in double quotes, which the overlap guard (johto/web/guard/, it reads checks/*.md) skips, so his text
coming back in a consequence never counts as AI text.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import actions, config, lint, rules, slots, stages, sync, transcript, workspace

CHECKS = "checks"
CLAUDE_BIN = None          # None: config.CLAUDE_BIN at call time. Tests point this at a stub.
AI_MODEL = "opus"
AI_EFFORT = "high"
AI_TIMEOUT_S = 15 * 60
AI_MAX_ISSUES = 8
TRANSCRIPT_CHARS = 60_000  # the condensed run sent to the deep check
DIFF_CHARS = 30_000

# App key -> (Studio field id, submission.md heading, label). Austin writes every one of these.
TEXT_FIELDS = {
    "opening_prompt": ("field_opening_prompt", "Opening Prompt", "Opening Prompt"),
    "goal": ("field_high_level_goal", "High-Level Task Goal", "Goal"),
    "decision_points": ("field_rationale_difficulty", "Decision Points", "Decision Points"),
    "codebase": ("field_codebase_overview", "Difficulty & Codebase Understanding", "Difficulty & Codebase"),
    "initial_state": ("field_initial_state", "Initial State / Setup", "Initial State"),
    "validation_notes": ("field_self_check", "Validation Notes", "Validation Notes"),
    "reviewer_notes": ("field_writer_submission_notes", "Notes for the Reviewer", "Notes for the Reviewer"),
}
LABELS = {k: v[2] for k, v in TEXT_FIELDS.items()} | {
    "ratings": "Ratings", "claude_minutes": "Claude minutes", "trajectory_logs": "Trajectory Logs",
    "repo": "Repo", "run": "Run", "grading": "Grading table", "text_source": "Where the text comes from"}
FIELD_OF_ID = {v[0]: k for k, v in TEXT_FIELDS.items()} | {
    "field_one_shot": "ratings", "field_task_success": "ratings", "field_steering": "ratings",
    "field_claude_minutes": "claude_minutes", "field_repo_url": "repo", "field_repo_name": "repo"}
# lint's field labels -> app keys
LINT_KEY = {"Opening Prompt": "opening_prompt", "Goal": "goal", "Decision Points": "decision_points",
            "Difficulty & Codebase": "codebase", "Initial State": "initial_state",
            "Validation Notes": "validation_notes", "Notes for the Reviewer": "reviewer_notes",
            "Ratings": "ratings", "Repo": "repo", "Runs": "run", "Studio": "run"}

CATEGORIES = ("save_mismatch", "incomplete", "missing_required", "missing_upload", "contradiction", "unsupported",
              "wrong_run", "terminology", "selected_missing", "scope_question", "style")
CATEGORY_LABEL = {"save_mismatch": "not saved", "incomplete": "looks unfinished", "missing_required": "missing",
                  "missing_upload": "upload missing", "contradiction": "contradicts the record",
                  "unsupported": "not backed by the evidence", "wrong_run": "wrong run", "terminology": "terms",
                  "selected_missing": "marked row missing", "scope_question": "scope", "style": "style"}
# What the AI may report. Saves, uploads and required fields are checked exactly, and style is never its job.
AI_CATEGORIES = ("contradiction", "unsupported", "terminology", "selected_missing", "incomplete", "scope_question",
                 "wrong_run")
AI_FIELDS = (*TEXT_FIELDS, "ratings", "claude_minutes")
GROUPS = (("attention", "Needs attention"), ("resolved", "Resolved since the last check"),
          ("decide", "Needs your decision"), ("optional", "Optional"))
# lint kinds (lint.KINDS) that need attention; "form" only at ERROR level; everything else is optional.
LINT_ATTENTION = {"missing": "missing_required", "ratings": "contradiction", "repo": "missing_required",
                  "cut": "incomplete", "unsaved": "save_mismatch"}
_SESSION = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", re.I)


# --- small helpers ---------------------------------------------------------------------------------------------

def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _sha(v) -> str:
    return hashlib.sha1(sync.norm(v if isinstance(v, str) else json.dumps(v, sort_keys=True)).encode()).hexdigest()[:12]


def _ws(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _clip(s: str, n: int) -> str:
    """At most n characters, cut at a word boundary, with "..." when cut."""
    s = _ws(s)
    if len(s) <= n:
        return s
    cut = s[: n - 3].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "..."


def _span(text: str, quote: str | None) -> str | None:
    """`quote` as it appears in `text` (whitespace may differ), at most 120 characters, else None."""
    toks = (quote or "").replace("...", " ").replace("…", " ").split()
    if not toks or not text:
        return None
    m = re.search(r"\s+".join(re.escape(t) for t in toks), text)
    if not m:
        m = re.search(r"\s+".join(re.escape(t) for t in toks), text, re.I)
    return m.group(0)[:120] if m else None


def _tail(text: str, n: int = 7) -> str:
    starts = [m.start() for m in re.finditer(r"\S+", text or "")]
    return (text[starts[-n]:] if len(starts) >= n else text or "").strip()[-120:]


def _when(iso: str | None) -> str:
    """'22:30' today, else 'Oct 30 22:30'."""
    dt = _dt(iso)
    if dt is None:
        return "?"
    local = dt.astimezone()
    return local.strftime("%H:%M") if local.date() == datetime.now().astimezone().date() else \
        local.strftime("%b %d %H:%M").replace(" 0", " ")


def _dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(iso.replace("Z", "+0000"), fmt)
        except ValueError:
            continue
    return None


def _ago(iso: str | None) -> str:
    dt = _dt(iso)
    if dt is None:
        return ""
    m = max(0, int((datetime.now().astimezone() - dt).total_seconds() // 60))
    return "just now" if m < 1 else f"{m} min ago" if m < 90 else f"{m // 60} h ago" if m < 48 * 60 else \
        f"{m // 1440} days ago"


def _sentence(msg: str) -> str:
    """A lint message as a sentence: capital first letter, full stop at the end."""
    msg = (msg or "").strip()
    if not msg:
        return msg
    msg = msg[0].upper() + msg[1:]
    return msg if msg[-1] in ".!?)\"'" else msg + "."


def issue_id(field: str, category: str, quote: str | None, evidence: str = "") -> str:
    """Stable id: field + category + quote (the evidence stands in when there is no quote)."""
    basis = _ws(quote).lower() if quote else "|" + _ws(evidence).lower()
    return "D" + hashlib.sha1(f"{field}|{category}|{basis}".encode()).hexdigest()[:10]


def make_issue(field: str, category: str, consequence: str, evidence: str = "", quote: str | None = None,
               source: str = "rule") -> dict:
    q = quote[:120] if quote else None
    return {"id": issue_id(field, category, q, evidence), "field": field, "quote": q, "category": category,
            "evidence": _clip(evidence, 160), "consequence": _clip(consequence, 200), "source": source}


# --- what the check reads ------------------------------------------------------------------------------------

def _snapshot(d: Path) -> dict | None:
    try:
        snap = json.loads((d / "app" / "studio-fields.json").read_text())
    except (OSError, ValueError):
        return None
    return snap if isinstance(snap, dict) and isinstance(snap.get("fields"), dict) else None


def _local(d: Path) -> dict:
    try:
        return workspace.parse_submission(d)
    except OSError:
        return {"sections": {}, "ratings": {}}


def read_draft(d: Path, meta: dict) -> dict:
    """The saved draft: per text field Studio's copy when the snapshot has one, else the local copy.

    {"fields": {key: {text, source: "studio"|"local"|None, studio, local}}, "ratings": {...lint keys},
     "minutes": {studio, local}, "trajectory": [...]|None, "studio_at", "snapshot": bool, "studio": {...}}"""
    snap = _snapshot(d)
    sf = (snap or {}).get("fields") or {}
    sub = _local(d)
    fields = {}
    for key, (fid, heading, _) in TEXT_FIELDS.items():
        studio = sync.norm(sf.get(fid)) if snap else None
        local = sync.norm(sub["sections"].get(heading))
        text, src = (studio, "studio") if studio else (local, "local") if local else ("", None)
        fields[key] = {"text": text, "source": src, "studio": studio, "local": local}
    lr = sub.get("ratings") or {}
    ratings = {}
    for rkey, fid in (("one-shot", "field_one_shot"), ("task success", "field_task_success"),
                      ("steering", "field_steering")):
        ratings[rkey] = str(sf.get(fid) or "").strip() or (lr.get(rkey) or "").strip()
    return {"fields": fields, "ratings": ratings,
            "minutes": {"studio": sf.get("field_claude_minutes"), "local": lr.get("claude minutes")},
            "trajectory": sf.get("field_trajectory_log_files") if snap else None,
            "studio_at": (snap or {}).get("at"), "snapshot": snap is not None, "studio": sf}


def _sub_view(draft: dict) -> dict:
    """The draft as lint.run_all wants it: {"sections": {heading: text}, "ratings": {...}}."""
    return {"sections": {TEXT_FIELDS[k][1]: v["text"] for k, v in draft["fields"].items() if v["text"]},
            "ratings": draft["ratings"]}


def submission_run(d: Path, meta: dict, run: str | None = None) -> str | None:
    """The run this submission uses: `run` when given, else actions.submission_run (the one shared rule),
    else actions.validation_run."""
    if run:
        return run
    shared = getattr(actions, "submission_run", None)
    if shared is not None:
        try:
            return shared(meta, d)
        except Exception:
            pass
    try:
        return actions.validation_run(d)[0]
    except Exception:
        return None


def _transcript_path(d: Path, rec: dict | None, run: str | None) -> Path | None:
    """The run's collected transcript, preferring the copy in this task folder."""
    rec = rec or {}
    stored = Path(rec["transcript"]) if rec.get("transcript") else None
    if stored is not None and rec.get("dir"):
        here = d / rec["dir"] / "prework_output" / stored.name
        if here.exists():
            return here
    if run:
        found = transcript.find(d / "runs" / run)
        if found:
            return found[-1]
    return stored if stored is not None and stored.exists() else None


def _diff_path(d: Path, rec: dict | None) -> Path | None:
    rec = rec or {}
    if rec.get("dir"):
        here = d / rec["dir"] / "prework_output" / "final.diff"
        if here.exists():
            return here
    p = Path(rec["diff"]) if rec.get("diff") else None
    return p if p is not None and p.exists() else None


def _run_info(d: Path, meta: dict, run: str | None) -> dict:
    """{run, rec, transcript: Path|None, summary: dict|None, session}"""
    rec = actions.run_record(d, run, meta) if run else None
    tp = _transcript_path(d, rec, run)
    summary = None
    if tp is not None:
        try:
            summary = transcript.summarize(tp)
        except Exception:
            summary = None
    return {"run": run, "rec": rec, "transcript": tp, "summary": summary,
            "session": tp.stem if tp is not None else None}


def _grading(d: Path) -> dict:
    """grading.json through johto.grading when it exists, else read directly. {} when there is none."""
    try:
        from . import grading
        data = grading.load(d)
    except Exception:
        try:
            data = json.loads((d / "grading.json").read_text())
        except (OSError, ValueError):
            data = {}
    return data if isinstance(data, dict) else {}


def _marks(row: dict) -> dict:
    m = row.get("marks") if isinstance(row.get("marks"), dict) else {}
    return {"selected": bool(m.get("selected")), "goal": bool(m.get("goal")), "dp": m.get("dp"),
            "notes": bool(m.get("notes"))}


def marked_rows(grading_data: dict) -> list[dict]:
    """Rows Austin selected or marked for a field."""
    out = []
    for row in grading_data.get("rows") or []:
        if isinstance(row, dict):
            m = _marks(row)
            if m["selected"] or m["goal"] or m["dp"] or m["notes"]:
                out.append(row)
    return out


def _marked_fields(row: dict) -> list[str]:
    m = _marks(row)
    return [k for k, on in (("goal", m["goal"]), ("decision_points", bool(m["dp"])),
                            ("validation_notes", m["notes"])) if on]


def _grading_sha(rows: list[dict]) -> str:
    keep = [{k: r.get(k) for k in ("id", "marks", "scope", "result", "basis", "verify")} for r in rows]
    return _sha(keep)


# --- the deterministic part ----------------------------------------------------------------------------------

def _lint_issues(meta: dict, draft: dict, d: Path) -> list[tuple[str, dict]]:
    """lint.run_all on the saved draft, sorted into groups by each flag's kind."""
    out = []
    for f in lint.run_all(meta, _sub_view(draft)):
        info = lint.flag_info(f)
        key = LINT_KEY.get(info["field"], "run")
        text = draft["fields"].get(key, {}).get("text", "")
        quote = _span(text, info["quote"]) if info["quote"] else None
        kind = info["kind"]
        if kind in LINT_ATTENTION:
            group, cat = "attention", LINT_ATTENTION[kind]
        elif kind == "form" and info["level"] == lint.ERROR:
            group, cat = "attention", "style"
        else:
            group, cat = "optional", "style"
        msg = _sentence(info["message"])
        if info["message"] == "empty":
            src = "Studio and your local copy" if draft["snapshot"] else "your local copy"
            msg = f"{LABELS.get(key, info['field'])} is empty in {src}."
        out.append((group, make_issue(key, cat, msg, info["rule"] or "", quote)))
    return out


def _saved_issues(d: Path, meta: dict, draft: dict) -> list[tuple[str, dict]]:
    """What Studio saved against the local copy: lint.check_saved_fields, then an exact comparison of every field
    (sync.plan says which copy changed)."""
    out, flagged = [], set()
    for f in lint.check_saved_fields(d):
        info = lint.flag_info(f)
        key = LINT_KEY.get(info["field"], "run")
        flagged.add(key)
        text = draft["fields"].get(key, {}).get("studio") or ""
        quote = _span(text, info["quote"]) if info["quote"] else None
        cat = "incomplete" if info["kind"] == "cut" else "save_mismatch"
        out.append(("attention", make_issue(key, cat, info["message"], info["rule"] or "submit-saved-whole", quote)))
    if not draft["snapshot"]:
        return out
    try:
        local_fields = workspace.to_fields(meta, _local(d))
    except Exception:
        local_fields = {}
    remote = draft["studio"]
    plan = sync.plan(local_fields, remote, meta)
    at_claim = meta.get("studio_at_claim") or {}
    for fid in (*sync.SECTION_FIELDS, *sync.RATING_FIELDS):
        key = FIELD_OF_ID.get(fid, "ratings")
        loc, rem = sync.norm(local_fields.get(fid)), sync.norm(remote.get(fid))
        label = sync.FIELD_NAMES.get(fid, fid)
        prev_writer = bool(at_claim) and rem and sync.h(rem) == at_claim.get(fid)
        if prev_writer and fid not in plan["agreed"]:
            msg = (f"Studio's {label} is the text that was there when you claimed the task, not yours. "
                   + ("Saving replaces it with your local copy." if loc else "Write your own in Studio."))
            out.append(("attention", make_issue(key, "save_mismatch", msg, f"{fid} · studio_at_claim")))
            continue
        if not loc or not rem or loc == rem or key in flagged:
            continue
        if fid in plan["pull"]:
            out.append(("optional", make_issue(key, "save_mismatch",
                                               f"Your local {label} is older than Studio's. The check read Studio's; "
                                               "johto pull brings it into submission.md.", f"{fid} · sync")))
        elif fid in plan["conflicts"]:
            why = plan["conflicts"][fid]["why"]
            out.append(("decide", make_issue(key, "save_mismatch",
                                             f"{label} differs between Studio and your local copy ({why}). The check "
                                             "read Studio's. Keep one version in both places.", f"{fid} · sync")))
        elif fid in plan["foreign"]:
            out.append(("decide", make_issue(key, "save_mismatch",
                                             f"{label} differs between Studio and your local copy, and johto can't "
                                             "tell which is newer. The check read Studio's.", f"{fid} · sync")))
        else:
            out.append(("attention", make_issue(key, "save_mismatch",
                                                f"Your local {label} has edits Studio doesn't have. The check read "
                                                "Studio's copy; save to Studio so reviewers see yours.",
                                                f"{fid} · sync")))
    url = (meta.get("repo") or {}).get("pinned_url")
    if remote.get("field_repo_url") and url and sync.norm(remote["field_repo_url"]) != sync.norm(url):
        out.append(("attention", make_issue("repo", "save_mismatch",
                                            "Studio's Repo URL isn't the pinned URL in task.json. Save to Studio.",
                                            "repo-pinned")))
    return out


def _shrink_issues(draft: dict, prev: dict | None, already: set[str]) -> list[tuple[str, dict]]:
    """A field that is now a strict prefix of its text at the last check: cut off, or cut on purpose."""
    out = []
    old_texts = (prev or {}).get("texts") or {}
    for key, v in draft["fields"].items():
        new, old = v["text"], sync.norm(old_texts.get(key))
        if key in already or not new or not old or new == old or not old.startswith(new):
            continue
        when = _when(prev.get("at"))
        lost = lint.words(old) - lint.words(new)
        if new[-1].isalpha() and old[len(new)].isalpha():
            out.append(("attention", make_issue(key, "incomplete",
                                                f"{LABELS[key]} now stops partway through a word that was whole at the "
                                                f"last check ({when}).", f"check {when}", _tail(new))))
        elif lost >= 3:
            out.append(("decide", make_issue(key, "incomplete",
                                             f"{LABELS[key]} is {lost} words shorter than at the last check ({when}) and "
                                             "ends where it used to go on. If you cut it on purpose, ignore this.",
                                             f"check {when}", _tail(new))))
    return out


def _required_issues(d: Path, meta: dict, draft: dict, info: dict) -> list[tuple[str, dict]]:
    """Claude minutes and the Trajectory Logs upload, from Studio's snapshot."""
    out = []
    if not draft["snapshot"]:
        return out
    run, s = info["run"], info["summary"]
    mins = draft["minutes"]["studio"]
    if s is not None:
        w = float(s.get("working_minutes") or 0)
        b = float(s.get("background_minutes") or 0)
        if mins in (None, ""):
            out.append(("attention", make_issue("claude_minutes", "missing_required",
                                                f"Studio has no Claude minutes. {run} measures {w:g} working minutes.",
                                                f"minutes-working · {run}")))
        else:
            try:
                m = float(mins)
            except (TypeError, ValueError):
                m = None
            if m is not None and abs(m - w) > max(2.0, 0.1 * w):
                if w < m <= w + b + 2:
                    out.append(("decide", make_issue(
                        "claude_minutes", "contradiction",
                        f"Studio says {m:g} Claude minutes; {run} measures {w:g} working, plus {b:g} waiting on "
                        "background jobs. Studio's number counts that waiting.", f"minutes-working · {run}")))
                else:
                    out.append(("attention", make_issue(
                        "claude_minutes", "contradiction",
                        f"Studio says {m:g} Claude minutes; {run}'s transcript measures {w:g} working minutes"
                        + (f" (plus {b:g} waiting on background jobs)." if b else "."), f"minutes-working · {run}")))
    files = draft["trajectory"]
    urls = [str(x.get("file_s3_url") or x.get("url") or x.get("name") or "") if isinstance(x, dict) else str(x)
            for x in (files if isinstance(files, list) else [])]
    logs = [u for u in urls if u.lower().endswith(".jsonl")]
    if not logs:
        out.append(("attention", make_issue("trajectory_logs", "missing_upload",
                                            "Studio has no Trajectory Logs file. Studio refuses Submit for Review "
                                            "without the run's .jsonl transcript.", "submit-trajectory-logs")))
        return out
    sessions = {m.group(1).lower() for u in logs for m in [_SESSION.search(u)] if m}
    want = (info["session"] or "").lower()
    if want and want not in sessions:
        others = {}
        for rec in meta.get("runs") or []:
            name = Path(rec.get("dir") or "").name
            tp = _transcript_path(d, rec, name)
            if tp is not None:
                others[tp.stem.lower()] = name
        hit = sorted({others[x] for x in sessions if x in others})
        if hit:
            out.append(("attention", make_issue("trajectory_logs", "wrong_run",
                                                f"Trajectory Logs holds {', '.join(hit)}'s transcript; the submission "
                                                f"uses {run}.", f"submit-from-validation · {run}")))
        else:
            out.append(("decide", make_issue("trajectory_logs", "wrong_run",
                                             f"Trajectory Logs holds a transcript johto can't match to {run} "
                                             f"(its file is {want[:8]}...jsonl).", f"submit-from-validation · {run}")))
    return out


def _run_issues(d: Path, meta: dict, draft: dict, info: dict) -> list[tuple[str, dict]]:
    """The run being submitted: collected, Opus 5.5 at high effort, one prompt when async, the saved prompt
    (actions.run_problems' checks, read from the task folder's own transcript and Studio's saved prompt)."""
    out = []
    run, rec, s = info["run"], info["rec"], info["summary"]
    short = d.name
    try:
        newer = actions.newer_uncollected(meta, d, run if (run or "").startswith("validate-") else None)
    except Exception:
        newer = []
    if not run and newer:
        return [("attention", make_issue("run", "missing_required",
                                         f"{newer[-1]} isn't collected, so the check can't read its transcript. "
                                         f"Collect it: johto collect {short} {newer[-1]}.",
                                         f"submit-from-validation · {newer[-1]}"))]
    if not run:
        return [("attention", make_issue("run", "missing_required", "No validation run yet.", "no-calib"))]
    if newer:
        out.append(("optional", make_issue("run", "wrong_run",
                                           f"{', '.join(newer)} started after {run} and "
                                           f"{'is' if len(newer) == 1 else 'are'}n't collected; this check and the "
                                           f"submission use {run}.", f"submission run · {run}")))
    if s is None:
        how = f"johto collect {short} {run}" if (rec or {}).get("dir") else f"johto lane {short} validate"
        return out + [("attention", make_issue("run", "missing_required",
                                               f"{run} isn't collected, so the check can't read its transcript. "
                                               f"Collect it: {how}.", f"submit-from-validation · {run}"))]
    for p in transcript.model_problems(s):
        out.append(("attention", make_issue("run", "contradiction", f"{run}: {p}.", f"lane-model · {run}")))
    mode = (meta.get("slot") or {}).get("interaction_mode") or ""
    if mode.startswith("Async") and s["human_turns"] > 1:
        out.append(("attention", make_issue("run", "contradiction",
                                            f"Async slot, but {run} has {s['human_turns']} prompts. Async is one "
                                            "prompt, unchanged.", f"async-one-prompt · {run}")))
    prompt = draft["fields"]["opening_prompt"]["text"]
    if s.get("first_prompt") and prompt and _ws(s["first_prompt"]) != _ws(prompt):
        out.append(("attention", make_issue("opening_prompt", "wrong_run",
                                            f"{run}'s first prompt differs from the saved Opening Prompt. Reviewers "
                                            "compare the two.", f"{run} turn 1")))
    w = s.get("working_minutes") or 0
    if w < 8 and (draft["ratings"].get("one-shot") or "").strip().lower() == "no":
        out.append(("optional", make_issue("run", "scope_question",
                                           f"Claude worked {w:g} minutes in {run}. Reviewers have dropped tasks Opus "
                                           "finishes in about 5 minutes; short runs with real misses have passed.",
                                           f"lesson-too-fast · {run}")))
    return out


def _grading_issues(rows: list[dict], run: str | None) -> list[tuple[str, dict]]:
    """Austin's marks on grading rows: from the run being submitted, a scope set, not refuted."""
    out = []
    for r in rows:
        rid = str(r.get("id") or "?")
        fields = _marked_fields(r)
        where = fields[0] if fields else "grading"
        res = r.get("result") if isinstance(r.get("result"), dict) else {}
        if run and res.get("run") and res["run"] != run and fields:
            out.append(("attention", make_issue(where, "wrong_run",
                                                f"{rid}'s result is from {res['run']}, but the submission uses {run}.",
                                                rid)))
        m = _marks(r)
        # Scope is a question for what the run missed or a behavior the request may imply, not for credits or claims.
        scoped = r.get("suggested_scope") == "uncertain" or (
            r.get("kind") in ("requirement", "defect") and res.get("value") != "credit")
        if (m["goal"] or m["dp"]) and r.get("scope") in (None, "") and scoped:
            out.append(("decide", make_issue(where, "scope_question",
                                             f"{rid} is marked for the {'goal' if m['goal'] else 'decision points'}, "
                                             "but its scope isn't set.", rid)))
        elif (m["goal"] or m["dp"]) and r.get("scope") == "outside":
            out.append(("decide", make_issue(where, "scope_question",
                                             f"{rid} is marked for the {'goal' if m['goal'] else 'decision points'}, "
                                             "but you set its scope to outside the task.", rid)))
        v = r.get("verify") if isinstance(r.get("verify"), dict) else {}
        if fields and v.get("status") == "refuted":
            out.append(("attention", make_issue(where, "unsupported",
                                                f"{rid} is marked for {LABELS.get(where, where)}, but a later check "
                                                "refuted it.", f"{rid} · {v.get('path') or 'verify'}")))
    return out


# --- the deep part: the critique-only route ------------------------------------------------------------------

SYSTEM = """You check a writer's saved draft of a coding-agent evaluation task against the record of one Claude \
Code run. You are a checker, not an editor or co-writer. The writer must write every word of the draft himself; \
an evaluator runs AI-writing detection on it.

Report only these, and only when material (a reviewer would likely reject the task, or the writer would have to \
rework a field):
- contradiction: a field says something the run record or another field contradicts (what Claude did or did not \
do, the number of prompts or turns, minutes, which run, ratings against the notes).
- unsupported: a field claims something was executed, reproduced or verified, but the record has only static \
evidence (source reading, tracing) or Claude's own claim.
- terminology: one thing is named inconsistently across fields, or a term the goal depends on is never defined.
- selected_missing: a grading row the writer marked for the goal (marks.goal) or a decision point (marks.dp) is \
consequential and does not appear in that field.
- incomplete: a sentence that stops partway, or a field that ends as if text was lost.
- scope_question: a tension only the writer can decide, for example the goal needs information the task input \
does not provide, or a field grades something the opening prompt did not ask for.
- wrong_run: a field describes a different run or turn than the one being submitted.
If nothing material remains, return no issues and set nothing_material to true.

You may not, anywhere in your answer:
- write a replacement sentence, ending, paragraph, phrase or example of what the writer could say;
- rephrase, summarize or outline any field;
- tell the writer what to write, add, say, open or end with;
- say what the writer probably meant, or offer another term, reading or interpretation for his words;
- advise a rating, or say which rating would pass or be eligible;
- add a requirement because a review or a grading row found another possible defect the writer did not mark;
- report style, tone, length, grammar or polish.

Each issue has:
- field: the field it is in.
- quote: an exact span copied from that field, at most 120 characters, or null.
- category: one of the categories above.
- evidence: references only: grading row ids (R3), run turns (turn 2), file:line, or rule ids. At most 160 \
characters.
- consequence: at most 200 characters in plain words: what is wrong and why it matters to a reviewer. State the \
problem, never a fix or new wording.
- id: "" and source: "ai".
Report at most 8 issues, most consequential first."""


def schema() -> dict:
    """JSON schema for the deep check's answer. Issue objects have exactly the stored issue's keys and no field
    for replacement text."""
    issue = {"type": "object", "additionalProperties": False,
             "required": ["id", "field", "quote", "category", "evidence", "consequence", "source"],
             "properties": {
                 "id": {"type": "string", "maxLength": 20},
                 "field": {"type": "string", "enum": list(AI_FIELDS)},
                 "quote": {"type": ["string", "null"], "maxLength": 120},
                 "category": {"type": "string", "enum": list(AI_CATEGORIES)},
                 "evidence": {"type": "string", "maxLength": 160},
                 "consequence": {"type": "string", "maxLength": 200},
                 "source": {"type": "string", "enum": ["ai"]}}}
    return {"type": "object", "additionalProperties": False,
            "required": ["issues", "resolved_ids", "nothing_material"],
            "properties": {"issues": {"type": "array", "maxItems": AI_MAX_ISSUES, "items": issue},
                           "resolved_ids": {"type": "array", "items": {"type": "string"}},
                           "nothing_material": {"type": "boolean"}}}


def claude_runner(system: str, prompt: str, schema_: dict) -> dict:
    """One headless Claude run with no tools, in an empty temp folder (so no project settings or CLAUDE.md), the way
    pack.outside_review runs. {"data": dict|None, "error": str|None, "cost_usd": float|None}."""
    binary = CLAUDE_BIN or config.CLAUDE_BIN
    cmd = [binary, "-p", "--model", AI_MODEL, "--effort", AI_EFFORT, "--output-format", "json",
           "--json-schema", json.dumps(schema_), "--system-prompt", system, "--tools", "",
           "--strict-mcp-config", "--setting-sources", "project", "--no-session-persistence"]
    with tempfile.TemporaryDirectory(prefix="johto-check-") as cwd:
        try:
            r = subprocess.run(cmd, cwd=cwd, input=prompt, capture_output=True, text=True, timeout=AI_TIMEOUT_S)
        except FileNotFoundError:
            return {"data": None, "error": f"Claude isn't available here ({binary}).", "cost_usd": None}
        except subprocess.TimeoutExpired:
            return {"data": None, "error": f"The AI check took longer than {AI_TIMEOUT_S // 60} minutes.",
                    "cost_usd": None}
    try:
        out = json.loads(r.stdout)
    except ValueError:
        return {"data": None, "error": _clip(r.stderr or r.stdout or f"exit {r.returncode}", 300), "cost_usd": None}
    cost = out.get("total_cost_usd") if isinstance(out, dict) else None
    data = out.get("structured_output") if isinstance(out, dict) else None
    if not isinstance(data, dict):
        data = _json_in(str(out.get("result") or "")) if isinstance(out, dict) else None
    if not isinstance(data, dict):
        err = (out.get("result") or out.get("subtype") or "no answer") if isinstance(out, dict) else "no answer"
        return {"data": None, "error": _clip(f"The AI check gave no usable answer: {err}", 300), "cost_usd": cost}
    return {"data": data, "error": None, "cost_usd": cost}


def _json_in(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


# Phrases that hand Austin wording: a suggestion of what to write, say, add or end with.
WORDING = re.compile(
    r"\b(consider(ing)? (writing|saying|adding|rephrasing|rewording|changing|replacing|mentioning|stating|noting|"
    r"including|phrasing|clarifying|naming|specifying)|"
    r"you (could|might|may|can|should|would) (write|say|add|phrase|rephrase|reword|mention|state|note|include|"
    r"change|replace|end|finish|open|start|begin|clarify|specify)|"
    r"try (writing|saying|adding|phrasing)|"
    r"something like|along the lines of|re-?phras\w*|re-?word\w*|re-?writ\w*|"
    r"replac\w* (it|this|that|the [\w-]+) with|instead,? (write|say|use)|change (it|this|that) to|"
    r"should (read|say)|could (read|say)|would read|a better (way|phrasing|wording|sentence|version)|"
    r"wording (like|such as)|suggested (wording|text|sentence)|"
    r"add(ing)? (a|the|one|another) (sentence|line|clause|phrase|paragraph|mention)|"
    r"(end|finish|close|start|begin|open) (it|this|the [\w-]+|your [\w-]+) with|complete (it|the sentence) with)\b"
    r"|(^|[.;:!?]\s+)(end|finish|close|start|begin|open|add|write|say|mention|state|explain) (with|that|a|the)\b"
    r"|\bfor (example|instance)\s*:|\be\.g\.\s*[:,]?\s*[\"“']|\bsuch as\s*:"
    # What a field should say, an order to say it, or a ready-made better/worse sentence.
    r"|\b(should|must|needs? to|ought to) (name|mention|include|say|state|define|specify|explain|cover|add|list|"
    r"describe|clarify|spell out|call|use|give|note)\b"
    r"|(^|[.;:!?]\s+)(name|define|use|include|clarify|specify|spell out|describe|list|cover|mention|call)\b"
    r"|\b(would be|is|reads?) clearer\b|\bmissing from (the )?[\w &-]+:"
    r"|\b(a|the) (weaker|stronger|better|worse|weak|strong|good|bad) (model|run|answer|solution|engineer|agent) "
    r"(would|might|could|will)\b"
    # Offering readings of a word ("whether 'freezing' means X or Y").
    r"|\bwhether\s+[\"“‘'][^\"”’']+[\"”’']\s+(means|refers to)\b.+\bor\b"
    # Saying what he probably meant offers him another term or reading to adopt.
    r"|\b((seems|appears) to (be about|be describing|mean|refer to)|(probably|likely|presumably) (means|meant|"
    r"refers to|intended)|meant to (say|refer)|what (the writer|the author|he) (means|meant|intended)|"
    r"(is|was) (likely|probably) (intended|meant)|a clearer term|the right term)\b",
    re.I)
# Advice that moves a rating, or talks about eligibility. Saying a rating contradicts the notes is allowed.
RATING_ADVICE = re.compile(
    r"\b((lower|raise|bump|change|drop|move|adjust|reconsider)\w* (the |your |its )?(task success|steering|"
    r"rating|score)|(should|could|would) (be )?(re)?rated?\b|rate (it|this|the task) (a |as )?\d|"
    r"\b(a|an) [1-5] (would|fits|is more)|would (pass|be eligible|qualify)|pass(ing|es)? only with|"
    r"pass(ing)? matrix|\w*eligib\w*)", re.I)
_QUOTED = re.compile(r"\"([^\"]{3,})\"|“([^”]{3,})”|(?<![\w])'([^']{3,}?)'(?![\w])|`([^`]{3,})`")
_CODEISH = re.compile(r"[/_.:#@=()\[\]{}<>\\]|[a-z][A-Z]|\d")


def _quotes(text: str) -> list[str]:
    out = []
    for m in _QUOTED.finditer(text or ""):
        q = next(g for g in m.groups() if g is not None)
        if m.group(4) is not None and (_CODEISH.search(q) or len(q.split()) < 3):
            continue   # code in backticks is evidence, not wording
        if m.group(3) is not None and len(q.split()) < 2:
            continue
        out.append(q)
    return out


def wording_leak(text: str, corpus: str) -> str | None:
    """Why `text` hands over wording, else None. `corpus` (whitespace-collapsed, lowercase) is what a quote may
    come from: Austin's fields and the run record."""
    if WORDING.search(text or ""):
        return "wording"
    if RATING_ADVICE.search(text or ""):
        return "rating_advice"
    for q in _quotes(text):
        if _ws(q).lower().strip(" .,;:") not in corpus:
            return "wording"
    return None


def post_filter(raw: list, draft: dict, marked: list[dict], corpus: str) -> tuple[list[dict], dict]:
    """Keep only flags: drop issues that suggest wording, advise ratings, name rows Austin didn't mark, or don't fit
    the schema. Quotes must be verbatim in his fields. Returns (issues, counts)."""
    counts: dict[str, int] = {}

    def drop(why):
        counts[why] = counts.get(why, 0) + 1

    marked_ids = {str(r.get("id")) for r in marked if _marked_fields(r)}
    texts = {k: v["text"] for k, v in draft["fields"].items()}
    out, seen = [], set()
    for x in raw if isinstance(raw, list) else []:
        if not isinstance(x, dict):
            drop("malformed")
            continue
        field, cat = x.get("field"), x.get("category")
        cons, ev = str(x.get("consequence") or "").strip(), str(x.get("evidence") or "").strip()
        if field not in AI_FIELDS:
            drop("bad_field")
            continue
        if cat not in AI_CATEGORIES:
            drop("bad_category")
            continue
        if not cons:
            drop("malformed")
            continue
        why = wording_leak(cons, corpus) or wording_leak(ev, corpus)
        if why:
            drop(why)
            continue
        if cat == "selected_missing" and not (set(re.findall(r"\bR\d+\b", ev + " " + cons)) & marked_ids):
            drop("unmarked_row")
            continue
        quote = x.get("quote") if isinstance(x.get("quote"), str) and x.get("quote").strip() else None
        if quote:
            span = _span(texts.get(field, ""), quote)
            if span is None:
                other = next((k for k, t in texts.items() if _span(t, quote)), None)
                if other is not None:
                    field, span = other, _span(texts[other], quote)
            if span is None:
                counts["quote_cleared"] = counts.get("quote_cleared", 0) + 1
            quote = span
        if len(_ws(cons)) > 200 or len(_ws(ev)) > 160:
            counts["trimmed"] = counts.get("trimmed", 0) + 1
        it = make_issue(field, cat, cons, ev, quote, source="ai")
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        out.append(it)
    return out, counts


def _rules_text() -> str:
    keep = {"goal", "decision_points", "codebase", "validation_notes"}
    extra = {"rate-one-shot", "rate-task-success", "rate-steering-scale", "minutes-working", "async-one-prompt",
             "interactive-colleague"}
    lines = []
    try:
        for r in rules.load():
            if r["status"] != "active" or r["group"] == "LLM policy":
                continue
            if r["id"] in extra or (set(r["fields"]) & keep):
                lines.append(f"- {r['id']}: {r['text']}")
    except Exception:
        pass
    return "\n".join(lines)


def _run_facts(info: dict, draft: dict, d: Path) -> dict:
    s = info["summary"] or {}
    facts = {"run": info["run"], "human_turns": s.get("human_turns"),
             "working_minutes": s.get("working_minutes"), "background_minutes": s.get("background_minutes"),
             "models": s.get("models"), "efforts": s.get("efforts"),
             "human_prompts": [s.get("first_prompt"), *(s.get("follow_ups") or [])] if s else []}
    try:
        from .web.domain import facts as fx
        diff = _diff_path(d, info["rec"])
        f = fx.public(fx.run_facts(info["transcript"], diff_text=diff.read_text(errors="replace") if diff else None,
                                   opening_prompt=draft["fields"]["opening_prompt"]["text"]))
        if f:
            facts.update(tests=f.get("tests"), files_edited=f.get("files_edited"), diffstat=f.get("diffstat"),
                         final_message_excerpt=f.get("final_message_excerpt"))
    except Exception:
        pass
    return facts


def _condensed(info: dict) -> str:
    if info["transcript"] is None:
        return ""
    try:
        from . import pack
        text, _ = pack.condense(info["transcript"], out_lines=10)
    except Exception:
        return ""
    if len(text) <= TRANSCRIPT_CHARS:
        return text
    head = TRANSCRIPT_CHARS * 2 // 3
    return text[:head] + "\n\n[... middle of the run left out ...]\n\n" + text[-(TRANSCRIPT_CHARS - head):]


def build_prompt(meta: dict, draft: dict, info: dict, marked: list[dict], focus: list[str] | None,
                 standing: list[dict], d: Path) -> tuple[str, str]:
    """(prompt, corpus): the deep check's input, and the text a quote in its answer may come from."""
    slot = meta.get("slot") or {}
    repo = meta.get("repo") or {}
    parts = ["=== TASK ===",
             f"Slot: {slots.slot_key(slot)}", f"Repository: {repo.get('full_name')} at {repo.get('commit') or repo.get('pinned_url')}",
             f"Run being submitted: {info['run']}", ""]
    parts.append("=== WHAT TO CHECK ===")
    if focus is None:
        parts.append("Check every field and how the fields relate to each other and to the run.")
    else:
        names = ", ".join(focus)
        parts.append(f"Since the last check only these changed: {names}. Report issues in those fields, or between "
                     "them and another field. STANDING ISSUES were found earlier on fields that did not change; do "
                     "not report them again. If a change resolved one, put its id in resolved_ids.")
    parts.append("")
    parts.append("=== SAVED FIELDS (the writer's own text) ===")
    for key, v in draft["fields"].items():
        if v["text"]:
            parts += [f"## {key}", v["text"], ""]
    r = draft["ratings"]
    parts += ["## ratings", f"One-shot: {r.get('one-shot') or '-'}", f"Task Success: {r.get('task success') or '-'}",
              f"Steering: {r.get('steering') or '-'}", "",
              "## claude_minutes", str(draft["minutes"]["studio"] or draft["minutes"]["local"] or "-"), ""]
    parts.append("=== GRADING ROWS THE WRITER MARKED ===")
    if marked:
        keep = ("id", "finding", "kind", "scope", "result", "basis", "refs", "limits", "marks", "verify")
        parts.append(json.dumps([{k: row.get(k) for k in keep if k in row} for row in marked], indent=1,
                                ensure_ascii=False))
    else:
        parts.append("None. The writer has not marked grading rows, so report no selected_missing issues.")
    parts.append("")
    if standing:
        parts.append("=== STANDING ISSUES ===")
        parts.append(json.dumps([{k: i[k] for k in ("id", "field", "quote", "category", "evidence", "consequence")}
                                 for i in standing], indent=1, ensure_ascii=False))
        parts.append("")
    facts = _run_facts(info, draft, d)
    parts += ["=== RUN FACTS (measured from the transcript and diff) ===", json.dumps(facts, indent=1,
                                                                                      ensure_ascii=False), ""]
    parts += ["=== RULES FOR THESE FIELDS ===", _rules_text(), ""]
    record = _condensed(info)
    diff_p = _diff_path(d, info["rec"])
    diff = diff_p.read_text(errors="replace")[:DIFF_CHARS] if diff_p else ""
    parts += ["=== RUN RECORD (condensed transcript) ===", record or "(not available)", ""]
    parts += ["=== FINAL DIFF (start) ===", diff or "(not available)"]
    prompt = "\n".join(parts)
    corpus = _ws(" ".join([*(v["text"] for v in draft["fields"].values()), record, diff,
                           json.dumps(facts, ensure_ascii=False), json.dumps(marked, ensure_ascii=False)])).lower()
    return prompt, corpus


def _ai_state(prev_checks: list[dict]) -> dict | None:
    """The newest check that carries an AI evaluation ({at, path, fields_sha, issues, run})."""
    for c in prev_checks:
        st = c.get("ai_state")
        if isinstance(st, dict) and st.get("fields_sha"):
            return st
    return None


def _ai_shas(shas: dict) -> dict:
    return {k: shas.get(k) for k in (*AI_FIELDS, "grading")}


def _changed_for_ai(now: dict, then: dict) -> list[str]:
    changed = [k for k in AI_FIELDS if now.get(k) != then.get(k)]
    if now.get("grading") != then.get("grading"):
        changed += [k for k in ("goal", "decision_points", "validation_notes") if k not in changed]
    return changed


def _still_clean(i: dict) -> bool:
    """An issue kept from an earlier check passes today's wording and rating filters (quotes were checked then)."""
    text = f"{i.get('consequence') or ''} {i.get('evidence') or ''}"
    return not WORDING.search(text) and not RATING_ADVICE.search(text)


def _carry(state: dict | None, shas: dict, run: str | None, draft: dict) -> tuple[list[dict], list[dict]]:
    """(still standing, stale): AI issues from the last AI evaluation, split by whether their field is unchanged
    (and their quote still in it)."""
    if not state:
        return [], []
    if state.get("run") != run:
        return [], list(state.get("issues") or [])
    keep, stale = [], []
    for i in state.get("issues") or []:
        if not _still_clean(i):
            continue   # caught by a filter rule added since that check
        same = shas.get(i.get("field")) == (state.get("fields_sha") or {}).get(i.get("field"))
        quote_ok = not i.get("quote") or _span(draft["fields"].get(i["field"], {}).get("text", ""), i["quote"])
        (keep if same and quote_ok else stale).append(i)
    return keep, stale


DEEP_NEEDS = ("opening_prompt", "goal", "decision_points", "codebase", "validation_notes")


def _empty_for_deep(meta: dict, draft: dict) -> list[str]:
    """The written fields the deep check waits for: it runs once, on a whole draft."""
    keys = list(DEEP_NEEDS) + (["initial_state"] if (meta.get("slot") or {}).get("action") == "Operate" else [])
    return [k for k in keys if not (draft["fields"].get(k) or {}).get("text")]


def deep_blocker(task_dir: Path) -> str | None:
    """Why the deep check won't run on the saved draft now (empty required fields), else None."""
    d = Path(task_dir)
    meta = workspace.load(d)
    empty = _empty_for_deep(meta, read_draft(d, meta))
    if not empty:
        return None
    names = [LABELS.get(k, k) for k in empty]
    words = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return f"{words} {'is' if len(names) == 1 else 'are'} empty. The AI check runs once every field is written."


def _deep(d: Path, meta: dict, draft: dict, info: dict, marked: list[dict], shas: dict, state: dict | None,
          full: bool, log, runner) -> tuple[list[dict], list[dict], dict, dict | None]:
    """(AI issues, AI issues resolved by this run, ai info, new ai_state). Falls back to the carried issues when
    the AI can't run."""
    standing, stale = _carry(state, shas, info["run"], draft)
    reuse_ok = state is not None and state.get("run") == info["run"] and not full
    focus = _changed_for_ai(shas, state.get("fields_sha") or {}) if reuse_ok else None
    ai = {"called": False, "at": now_iso(), "focus": focus, "reused_from": state.get("path") if reuse_ok else None,
          "dropped": {}, "dropped_total": 0, "cost_usd": None, "error": None, "nothing_material": None}
    if focus == []:
        ai["at"] = state.get("at")
        log("No field changed since the last AI check; reusing its issues.")
        return standing, [], ai, dict(state, issues=standing)
    present = [k for k in (focus or AI_FIELDS)
               if k in ("ratings", "claude_minutes") or (draft["fields"].get(k) or {}).get("text")]
    if not any(draft["fields"][k]["text"] for k in TEXT_FIELDS):
        ai["error"] = "There is no saved text to check yet."
        return standing, [], ai, state
    if _empty_for_deep(meta, draft):
        # A deep check of a half-written draft costs money and repeats the exact "missing" issues.
        ai["error"] = deep_blocker(d)
        return standing, [], ai, state
    prompt, corpus = build_prompt(meta, draft, info, marked, focus, standing if reuse_ok else [], d)
    what = "every field" if focus is None else ", ".join(LABELS.get(k, k) for k in present) or "the changed fields"
    log(f"Running the AI check on {what} (a few minutes)...")
    res = (runner or claude_runner)(SYSTEM, prompt, schema())
    ai.update(called=True, cost_usd=res.get("cost_usd"), error=res.get("error"))
    if res.get("data") is None:
        log(f"The AI check didn't finish: {res.get('error')}")
        return standing, [], ai, state
    data = res["data"]
    issues, counts = post_filter(data.get("issues"), draft, marked, corpus)
    ai["dropped"] = counts
    ai["dropped_total"] = sum(v for k, v in counts.items() if k not in ("quote_cleared", "trimmed"))
    ai["nothing_material"] = bool(data.get("nothing_material")) and not issues
    resolved_ids = {str(x) for x in data.get("resolved_ids") or []}
    kept_standing = [i for i in standing if i["id"] not in resolved_ids] if reuse_ok else []
    resolved = [i for i in standing if i["id"] in resolved_ids] if reuse_ok else []
    seen = {i["id"] for i in kept_standing}
    merged = kept_standing + [i for i in issues if i["id"] not in seen]
    # Issues on re-checked fields that the new answer no longer has are resolved too.
    checked = set(focus) if focus is not None else set(AI_FIELDS)
    new_ids = {i["id"] for i in merged}
    resolved += [i for i in (state or {}).get("issues") or [] if state.get("run") == info["run"]
                 and i.get("field") in checked and i["id"] not in new_ids and i not in resolved]
    new_state = {"at": ai["at"], "path": None, "run": info["run"], "fields_sha": _ai_shas(shas), "issues": merged}
    return merged, resolved, ai, new_state


# --- storing and reading checks ------------------------------------------------------------------------------

_NAME = re.compile(r"^draft-(\d{8}-\d{6})(?:-(\d+))?\.json$")


def _order(p: Path) -> tuple:
    m = _NAME.match(p.name)
    return (m.group(1), int(m.group(2) or 1)) if m else ("", 0)


def _checks(d: Path) -> list[dict]:
    """Stored checks, newest first (two in the same second are draft-<stamp>.json, then -2, -3)."""
    out = []
    for p in sorted((Path(d) / CHECKS).glob("draft-*.json"), key=_order, reverse=True):
        try:
            c = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(c, dict):
            c["path"] = str(p)
            out.append(c)
    return out


def latest(task_dir: Path) -> dict | None:
    """The newest stored check (without the text it read), or None."""
    cs = _checks(Path(task_dir))
    if not cs:
        return None
    c = cs[0]
    c.pop("texts", None)
    return c


def summary(task_dir: Path) -> dict:
    """{latest_at, attention, decide, stale, path} for the task card: counts from the newest check, and whether the
    saved draft (text, ratings, minutes, uploads, run, marked grading rows) changed since it. Never writes."""
    d = Path(task_dir)
    c = latest(d)
    if c is None:
        return {"latest_at": None, "attention": None, "decide": None, "stale": True, "path": None}
    try:
        meta = workspace.load(d)
        draft = read_draft(d, meta)
        info = _run_info(d, meta, submission_run(d, meta))
        now = _field_shas(draft, info, marked_rows(_grading(d)))
        then = c.get("fields_sha") or {}
        stale = any(k in then and then[k] != v for k, v in now.items())   # keys an older check lacks don't count
    except Exception:
        stale = True
    return {"latest_at": c.get("at"), "attention": len(c.get("attention") or []), "decide": len(c.get("decide") or []),
            "stale": stale, "path": c.get("path")}


def _new_path(d: Path) -> Path:
    folder = d / CHECKS
    folder.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for n in range(1, 100):
        p = folder / (f"draft-{stamp}.json" if n == 1 else f"draft-{stamp}-{n}.json")
        try:
            with open(p, "x"):
                return p
        except FileExistsError:
            continue
    raise OSError(f"no free check name in {folder}")


def _mine_grams(texts: list[str]) -> set[str]:
    from .web.guard import normalize
    out: set[str] = set()
    for t in texts:
        for seq in normalize.sequences(t):
            out.update(normalize.grams(seq))
    return out


def quote_echoes(text: str, mine: set[str]) -> str:
    """`text` with every stretch of 6 or more words that also appears in Austin's fields put in double quotes,
    so the overlap guard reads it as his words coming back, not as AI text."""
    from .web.guard import normalize
    s = _ws(normalize.clean(text)).replace('"', "'")
    spans = []
    for seq in normalize.sequences(s):
        for i, g in enumerate(normalize.grams(seq)):
            if g in mine:
                spans.append([seq[i][1], seq[i + normalize.N - 1][2]])
    if not spans:
        return s
    spans.sort()
    merged = [spans[0]]
    for a, b in spans[1:]:
        if a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out, at = [], 0
    for a, b in merged:
        out += [s[at:a], '"', s[a:b], '"']
        at = b
    return "".join(out + [s[at:]])


def render_md(result: dict, draft: dict) -> str:
    """The .md the overlap guard reads: only AI-written consequences and evidence, Austin's words in quotes."""
    try:
        mine = _mine_grams([v["text"] for v in draft["fields"].values() if v["text"]])
    except Exception:
        mine = set()
    lines = [f"# Draft check, {_when(result['at'])}{' (with the AI check)' if result['deep'] else ''}", "",
             "AI-written text: the consequence and evidence of each AI issue. Text in double quotes is Austin's "
             "own, copied from his fields.", ""]
    n = 0
    for group, title in GROUPS:
        items = [i for i in result.get(group) or [] if i.get("source") == "ai"]
        if not items or group == "resolved":
            continue
        lines += [f"## {title}", ""]
        for i in items:
            n += 1
            q = f' "{_ws(i["quote"]).replace(chr(34), chr(39))}" (Austin\'s words)' if i.get("quote") else ""
            lines.append(f"- {LABELS.get(i['field'], i['field'])} · {CATEGORY_LABEL.get(i['category'])}{q}")
            lines.append(f"  {quote_echoes(i['consequence'], mine)}")
            if i.get("evidence"):
                lines.append(f"  Evidence: {quote_echoes(i['evidence'], mine)}")
        lines.append("")
    if not n:
        lines.append("No AI issues in this check.")
    return "\n".join(lines).rstrip() + "\n"


# --- the check -----------------------------------------------------------------------------------------------

def _refresh(d: Path, log) -> None:
    """Read Studio's current text into app/studio-fields.json (actions.pull_from_studio). Not in fake mode: the fake
    Studio would overwrite a copied task folder with fake data."""
    if config.FAKE:
        log("Studio isn't read again in fake mode; the check uses the last snapshot.")
        return
    try:
        res = actions.pull_from_studio(d)
    except config.RealWriteRefused as e:
        log(f"Studio wasn't read again: {e}")
        return
    except Exception as e:   # signed out, network: the check still runs on the last snapshot
        log(f"Studio wasn't read again ({getattr(e, 'message', None) or e}); the check uses the last snapshot.")
        return
    if res.get("pulled"):
        log(f"Brought in from Studio: {', '.join(res['pulled'])}")


def _field_shas(draft: dict, info: dict, marked: list[dict]) -> dict:
    shas = {k: _sha(v["text"]) for k, v in draft["fields"].items()}
    shas["ratings"] = _sha(draft["ratings"])
    shas["claude_minutes"] = _sha(str(draft["minutes"]["studio"] if draft["snapshot"] else draft["minutes"]["local"]))
    shas["trajectory_logs"] = _sha(draft["trajectory"] or [])
    shas["run"] = _sha(f"{info['run']}|{info['session']}")
    shas["grading"] = _grading_sha(marked)
    # Where each field's text came from (Studio, local or none) and which Studio snapshot: the same words read
    # from another place are a change, not "nothing changed".
    shas["text_source"] = _sha({k: [v["source"], _sha(v["studio"] or "")] for k, v in draft["fields"].items()})
    return shas


def run(task_dir: Path, *, deep: bool = False, full: bool = False, log=print, refresh: bool = False,
        run_name: str | None = None, runner=None) -> dict:
    """Check the saved draft and store the result (contract 4). `refresh` reads Studio again first; `run_name`
    overrides the submission run; `runner(system, prompt, schema) -> {data, error, cost_usd}` replaces the headless
    Claude call (tests)."""
    d = Path(task_dir)
    if refresh:
        _refresh(d, log)
    meta = workspace.load(d)
    draft = read_draft(d, meta)
    info = _run_info(d, meta, submission_run(d, meta, run_name))
    grading_data = _grading(d)
    marked = marked_rows(grading_data)
    shas = _field_shas(draft, info, marked)
    history = _checks(d)
    prev = history[0] if history else None

    found: list[tuple[str, dict]] = []
    saved = _saved_issues(d, meta, draft)
    found += saved
    cut = {i["field"] for _, i in saved if i["category"] == "incomplete"}
    found += _shrink_issues(draft, prev, cut)
    found += _required_issues(d, meta, draft, info)
    found += _run_issues(d, meta, draft, info)
    found += _lint_issues(meta, draft, d)
    found += _grading_issues(marked, info["run"])

    state = _ai_state(history)
    ai_resolved: list[dict] = []
    if deep:
        ai_issues, ai_resolved, ai, new_state = _deep(d, meta, draft, info, marked, shas, state, full, log, runner)
        stale = [] if ai["called"] and ai["error"] is None else _carry(state, shas, info["run"], draft)[1]
    else:
        ai_issues, stale = _carry(state, shas, info["run"], draft)
        ai = {"called": False, "at": (state or {}).get("at"), "reused_from": (state or {}).get("path"),
              "focus": None, "dropped": {}, "dropped_total": 0, "cost_usd": None, "error": None,
              "nothing_material": None}
        new_state = state
    found += [("decide" if i["category"] == "scope_question" else "attention", i) for i in ai_issues]

    groups: dict[str, list[dict]] = {"attention": [], "decide": [], "optional": []}
    seen: set[str] = set()
    for group, it in found:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        groups[group].append(it)
    stale = [i for i in stale if i["id"] not in seen]
    # Issues today's filters withdraw from an earlier AI check weren't fixed by Austin: never "resolved".
    stale_ids = {i["id"] for i in stale} | {i["id"] for i in (state or {}).get("issues") or [] if not _still_clean(i)}

    resolved = []
    if prev:
        before = [*(prev.get("attention") or []),
                  *[i for i in prev.get("stale") or [] if i.get("category") != "scope_question"]]
        rids = set()
        for i in before + [i for i in ai_resolved if i.get("category") != "scope_question"]:
            if i["id"] not in seen and i["id"] not in stale_ids and i["id"] not in rids:
                rids.add(i["id"])
                resolved.append(i)
    changed = [k for k in shas if prev and (prev.get("fields_sha") or {}).get(k) not in (None, shas[k])]
    path = _new_path(d)
    if new_state is not None and deep and ai["called"] and ai["error"] is None:
        new_state = dict(new_state, path=str(path))
        ai["reused_from"] = None if ai["focus"] is None else ai["reused_from"]
    result = {
        "at": now_iso(), "fields_sha": shas, "attention": groups["attention"], "resolved": resolved,
        "decide": groups["decide"], "optional": groups["optional"], "deep": bool(deep), "path": str(path),
        "run": info["run"], "studio_at": draft["studio_at"],
        "sources": {k: v["source"] for k, v in draft["fields"].items()},
        "since": {"at": prev.get("at"), "changed": changed} if prev else None,
        "stale": stale, "ai": ai, "ai_state": new_state,
    }
    stored = dict(result, texts={k: v["text"] for k, v in draft["fields"].items()})
    path.write_text(json.dumps(stored, indent=1, ensure_ascii=False))
    path.with_suffix(".md").write_text(render_md(result, draft))
    stages.log(d, "draft_checked", deep=bool(deep), attention=len(groups["attention"]), run=info["run"],
               path=path.name)
    return result


# --- text output and the CLI ---------------------------------------------------------------------------------

def _issue_lines(i: dict) -> list[str]:
    head = f"  {LABELS.get(i['field'], i['field'])}, {CATEGORY_LABEL.get(i['category'], i['category'])}"
    if i.get("source") == "ai":
        head += " (AI)"
    if i.get("quote"):
        head += f': "{_clip(i["quote"], 90)}"'
    body = f"      {i['consequence']}"
    if i.get("evidence"):
        body += f"  [{i['evidence']}]"
    return [head, body]


def render(result: dict, all: bool = False, short: str | None = None) -> str:
    """The four groups, main point first. Optional collapses to a count unless `all`."""
    lines = []
    if result.get("studio_at"):
        lines.append(f"Checked Studio's saved text from {_when(result['studio_at'])} ({_ago(result['studio_at'])}).")
    else:
        lines.append("No Studio snapshot yet, so this checked your local copy.")
    since = result.get("since")
    if since:
        names = [LABELS.get(k, k) for k in since.get("changed") or [] if k in LABELS or k == "run"]
        lines.append(f"Since the last check ({_when(since['at'])}): "
                     + (f"{', '.join(names)} changed." if names else "nothing changed."))
    ai = result.get("ai") or {}
    cmd = f"johto check {short or 'TASK'}"
    if ai.get("error"):
        lines.append(f"AI check didn't run: {ai['error']}")
    elif ai.get("called"):
        on = "every field" if ai.get("focus") is None else ", ".join(LABELS.get(k, k) for k in ai["focus"])
        dropped = ai.get("dropped_total") or 0
        lines.append(f"AI check on {on}" + (f"; {dropped} AI issue{'s' if dropped != 1 else ''} dropped for "
                                             "suggesting wording or ratings." if dropped else "."))
    elif ai.get("at"):
        lines.append(f"AI issues from the check at {_when(ai['at'])}.")
    else:
        lines.append(f"No AI check yet ({cmd} --deep).")
    stale = result.get("stale") or []
    if stale:
        fields = sorted({LABELS.get(i["field"], i["field"]) for i in stale})
        lines.append(f"{len(stale)} earlier AI issue{'s' if len(stale) != 1 else ''} on {', '.join(fields)} "
                     f"wait{'s' if len(stale) == 1 else ''} for the next {cmd} --deep.")
    lines.append("")
    if not result["attention"] and not result["decide"]:
        lines.append("No material issues under these checks.")
        lines.append("")
    for group, title in GROUPS:
        items = result.get(group) or []
        if not items:
            continue
        if group == "optional" and not all:
            lines.append(f"Optional: {len(items)}. Show them with {cmd} --all.")
            continue
        lines.append(f"{title} ({len(items)})")
        for i in items:
            if group == "resolved":
                lines.append(f"  {LABELS.get(i['field'], i['field'])}, "
                             f"{CATEGORY_LABEL.get(i['category'], i['category'])}"
                             + (f': "{_clip(i["quote"], 60)}"' if i.get("quote") else f": {_clip(i['consequence'], 80)}"))
            else:
                lines += _issue_lines(i)
        lines.append("")
    return "\n".join(lines).rstrip()


# Issues read from Studio's snapshot that `johto submit` fixes itself (it saves every field, minutes included) or
# that Studio's own Submit refuses (no Trajectory Logs). A stale snapshot must not stop submit on them.
SNAPSHOT_FIELDS = ("claude_minutes", "trajectory_logs")


def legacy(result: dict) -> list[tuple]:
    """The result as the old check's (level, field, message) list, for `johto submit`: attention is ERROR, except
    unsaved edits, Claude minutes and Trajectory Logs (see SNAPSHOT_FIELDS); everything else WARN."""
    out = []
    for group in ("attention", "decide", "optional"):
        for i in result.get(group) or []:
            err = group == "attention" and i["category"] != "save_mismatch" and i["field"] not in SNAPSHOT_FIELDS
            out.append((lint.ERROR if err else lint.WARN, LABELS.get(i["field"], i["field"]), i["consequence"]))
    return out


def add_check_args(p) -> None:
    """Arguments for `johto check <t> [--run R] [--deep] [--full] [--all] [--refresh]`. Adds only what `p` lacks,
    so it works on a fresh parser or on the existing check parser."""
    have = {a.dest for a in p._actions}
    if "task" not in have:
        p.add_argument("task")
    if "run" not in have:
        p.add_argument("--run", help="the run to submit (default: the chosen run, else latest validate-N)")
    p.add_argument("--deep", action="store_true",
                   help="also run the AI check (flags only; on fields changed since the last one)")
    p.add_argument("--full", action="store_true", help="with --deep: check every field again, not only changed ones")
    p.add_argument("--all", action="store_true", help="show optional issues too")
    p.add_argument("--refresh", action="store_true", help="read Studio's saved text again first")


def cmd_check(a) -> list[tuple]:
    """`johto check`: run the check, print the four groups, return the old (level, field, message) list."""
    try:
        d = workspace.resolve(a.task)
    except FileNotFoundError as e:
        raise SystemExit(str(e))
    run_name = getattr(a, "run", None)
    if run_name and not (d / "runs" / run_name).is_dir():
        raise SystemExit(f"No run {run_name} in {d}")
    meta = workspace.load(d)
    res = run(d, deep=getattr(a, "deep", False), full=getattr(a, "full", False), refresh=getattr(a, "refresh", False),
              run_name=run_name, log=print)
    print(f"{d.name}: {meta.get('task_name')}  {slots.slot_key(meta.get('slot') or {})}"
          + (f"  (run {res['run']})" if res.get("run") else ""))
    print(render(res, all=getattr(a, "all", False), short=d.name))
    return legacy(res)
