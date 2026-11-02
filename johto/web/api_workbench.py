"""Task workbench API (spec.md 3.5 and 7.3): GET /api/tasks/{short}/workbench, POST /api/tasks/{short}/reviews,
POST /api/tasks/{short}/review-latest, POST /api/tasks/{short}/reveal-log.

Owned by the `workspace` builder. Mounted by app.py through EXTENSIONS (session, Origin and demo guards come for
free). GET handlers also answer in demo mode (cookie johto_mode=demo), from johto/web/demo.py's tasks.

The workbench payload is everything the task page shows around Austin's fields, in words: the step he is on, the
path strip, the run, the findings and the submit list. It never carries field text (the page reads that from
GET /api/tasks/{short} and saves through PUT .../sections/{key}), and it never drafts, rewords or suggests text for
a field. Rule ids, run ids, paths and commands stay on the server or inside a `drill` block the page shows only
behind "See it in the code" or "Details".
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .. import config
from .api_task import RUN, SHORT, ApiError, _body, handled

# --- words -----------------------------------------------------------------------------------------------

FIELD_NAME = {"opening_prompt": "Opening prompt", "goal": "Goal", "decision_points": "Decision points",
              "codebase": "Codebase understanding", "initial_state": "Initial state",
              "validation_notes": "Validation notes", "reviewer_notes": "Notes for the reviewer",
              "ratings": "Ratings", "claude_minutes": "Claude minutes", "trajectory_logs": "Conversation log",
              "run": "Validation run", "repo": "Repo link"}
# The name inside a sentence ("Write the goal.").
FIELD_LOWER = {k: (v if k == "claude_minutes" else v[0].lower() + v[1:]) for k, v in FIELD_NAME.items()}
# Write step tabs, in order. Initial state only for Operate slots.
WRITE_FIELDS = ("goal", "decision_points", "codebase", "initial_state", "ratings", "validation_notes")
TEXT_FIELDS = ("opening_prompt", "goal", "decision_points", "codebase", "initial_state", "validation_notes",
               "reviewer_notes")
STUDIO_ID = {"opening_prompt": "field_opening_prompt", "goal": "field_high_level_goal",
             "decision_points": "field_rationale_difficulty", "codebase": "field_codebase_overview",
             "initial_state": "field_initial_state", "validation_notes": "field_self_check",
             "reviewer_notes": "field_writer_submission_notes"}
LINT_FIELD = {"Opening Prompt": "opening_prompt", "Goal": "goal", "Decision Points": "decision_points",
              "Difficulty & Codebase": "codebase", "Validation Notes": "validation_notes",
              "Initial State": "initial_state", "Repo": "repo", "Ratings": "ratings"}
RESULT_WORDS = {"credit": "Got it right", "partial": "Partly right", "missed": "Claude missed it",
                "unknown": "Not known yet"}
BASIS_WORDS = {"executed": "Seen in a real run", "isolated": "Seen by running part of the code",
               "traced": "Read in the code only", "claimed": "Claude's claim, not checked", "none": "Not checked"}
SCALE = {"Enterprise-grade": "Enterprise", "Personal-scale": "Personal"}
ACTION_WORD = {"Create": "Create", "Modify": "Modify", "Operate": "Operate", "Understand / Assess": "Understand"}
FEEDBACK_HINTS = [("opening_prompt", re.compile(r"\bopening prompt\b|\bprompt\b", re.I)),
                  ("goal", re.compile(r"\bgoal\b|\bsuccess criteri|\bcriteri(on|a)\b", re.I)),
                  ("decision_points", re.compile(r"\bdecision points?\b", re.I)),
                  ("codebase", re.compile(r"\bcodebase\b|\bdifficulty\b", re.I)),
                  ("initial_state", re.compile(r"\binitial state\b|\bsetup\b", re.I)),
                  ("validation_notes", re.compile(r"\bvalidation\b", re.I))]
FALLBACK_FINDING = "Open the evidence to read this finding."

SUBMIT_STAGES = ("ready_to_save", "saving", "qc_running", "fix_qc", "ready_to_submit", "submitting", "studio")
AFTER_STAGES = ("in_review", "accepted", "rejected", "dropped", "discarded", "unclaimed")
RUN_STAGES = ("waiting_calib", "waiting_validate", "calibrating", "validating", "collecting", "collect_failed",
              "decide")


def run_words(name: str | None) -> str:
    m = re.fullmatch(r"(calib|validate)-(\d+)", name or "")
    return f"{'calibration' if m.group(1) == 'calib' else 'validation'} {m.group(2)}" if m else ""


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _when(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None


def _hhmm(iso: str | None) -> str:
    """The clock time as it was recorded (10:41)."""
    m = re.search(r"T(\d\d:\d\d)", str(iso or ""))
    return m.group(1) if m else ""


def _day(iso: str | None) -> str:
    w = _when(iso)
    return f"{w.strftime('%b')} {w.day}" if w else ""


def _n(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _list_words(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _turns_words(turns: list[int]) -> str:
    """[2, 3] -> "turns 2 and 3"; [3] -> "turn 3"; [2, 3, 4, 5] -> "turns 2 to 5"."""
    if not turns:
        return ""
    if len(turns) == 1:
        return f"turn {turns[0]}"
    if len(turns) > 3 and turns == list(range(turns[0], turns[-1] + 1)):
        return f"turns {turns[0]} to {turns[-1]}"
    return "turns " + _list_words([str(t) for t in turns])


# --- hiding code -----------------------------------------------------------------------------------------

def _plain_mod():
    try:
        return importlib.import_module("johto.plain")
    except Exception:  # noqa: BLE001  (written in parallel; a missing or mid-edit module falls back)
        return None


_TICK = re.compile(r"`[^`\n]*`")
_PATHLIKE = re.compile(r"(?<![\w/])[\w.~-]*(?:/[\w.-]+)+(?::\d+(?:-\d+)?)?|\b[\w-]+\.\w{1,5}:\d+(?:-\d+)?\b")
_IDENT = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b|\b[a-z]+[A-Z][A-Za-z0-9]*\b|#\d+\b|\b[0-9a-f]{7,40}\b")


def _fallback_hide(text: str, min_words: int) -> str:
    s = _TICK.sub(" ", text)
    s = _PATHLIKE.sub(" ", s)
    s = _IDENT.sub(" ", s)
    s = re.sub(r"\s+([.,;:!?)])", r"\1", re.sub(r"[ \t]+", " ", s)).strip(" ,;:")
    return s if len(re.findall(r"[A-Za-z]+", s)) >= min_words else ""


_RUN_ID = re.compile(r"\b(calib|validate)-(\d+)(?:'s)?\b")
# The validation-run row's summary bits ("Opus 5.5", "high", "3 prompts", "11.2 min"): CLI facts, not problems.
_RUN_SUMMARY = re.compile(r"^(?:(?:Claude |claude-)?(?:Opus|Sonnet|Haiku)\b.*|(?:low|medium|high|xhigh|max)(?:, ?\w+)*"
                          r"|effort not recorded|\d+ prompts?|[\d.]+ min|its own lane|sent prompt = Opening Prompt"
                          r"|1 prompt \(async\))$", re.I)
_CLI = re.compile(r"[^.!?\n]*\bjohto\s+[a-z][\w-]*[^.!?\n]*[.!?]?")
_EXIT = re.compile(r"[^.!?\n]*\bexit(?:ed)?(?: with)?(?: code| status)?\s+\d+\b[^.!?\n]*[.!?]?", re.I)


def words_only(text: str | None, min_words: int = 5, strict: bool = False) -> str:
    """A machine string as first-layer words: run ids become "validation 1", CLI hints and exit codes go, then
    johto.plain.hide_code strips the code tokens. "" when too little is left."""
    if not text:
        return ""
    s = _RUN_ID.sub(lambda m: (f"{'calibration' if m.group(1) == 'calib' else 'validation'} {m.group(2)}"
                               + ("'s" if m.group(0).endswith("'s") else "")), str(text))
    s = _CLI.sub(" ", s)
    s = _EXIT.sub(" ", s)
    m = _plain_mod()
    if m is not None and callable(getattr(m, "hide_code", None)):
        try:
            out = m.hide_code(s, min_words=min_words, strict=strict) if strict else m.hide_code(s, min_words=min_words)
        except TypeError:
            out = m.hide_code(s)
    else:
        out = _fallback_hide(s, min_words)
    return _drop_broken(out, min_words)


# Leftovers of code a sentence can't be read with: "only.users.empty-user" (dotted names run together).
_BROKEN = re.compile(r"(?<![A-Za-z]\.)\b(?!e\.g\b|i\.e\b)[A-Za-z]+\.[A-Za-z][\w-]*\.[\w.-]+")


def _drop_broken(s: str, min_words: int) -> str:
    """Drops the sentences hiding code left unreadable; "" when too little is left."""
    if not s or not _BROKEN.search(s):
        return s
    keep = [x for x in re.split(r"(?<=[.!?])\s+", s) if not _BROKEN.search(x)]
    out = " ".join(keep).strip()
    return out if len(out.split()) >= min_words else ""


def _whole(s: str | None) -> str | None:
    """An excerpt cut mid-sentence ends at its last full sentence."""
    if not s:
        return s
    s = s.strip()
    if s[-1] in ".!?\"')":
        return s
    cut = max(s.rfind(". "), s.rfind(".\n"), s.rfind("! "), s.rfind("? "))
    return s[:cut + 1] if cut > 40 else s + "…"


def _sentence(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    s = _cap(s)
    return s if s[-1] in ".!?" else s + "."


def _oid(*parts) -> str:
    """An opaque, stable item id (rule ids never leave the server)."""
    return "i" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:10]


# --- the rule table --------------------------------------------------------------------------------------
# rule id -> (title, why). {field} / {Field} is the field's name. A why of None uses the check's own message,
# cleaned (it is a rule, never wording). Actions come from the field: a field rule offers "Go to the {field}".

RULES: dict[str, tuple[str, str | None]] = {
    "prompt-only-text": ("The opening prompt is empty", "Studio needs the prompt you sent Claude."),
    "prompt-no-tells": ("The opening prompt reads like a template", None),
    "prompt-no-leak": ("The opening prompt gives away part of the grading", None),
    "spec-no-steps": ("The opening prompt lists steps", "A prompt says what is wanted, not each step to take."),
    "casual-length": ("The opening prompt is long for casual prose", "Casual prose is about 20 to 60 words."),
    "casual-voice": ("The opening prompt has a list", "Casual prose has no lists."),
    "spec-clean": ("The spec needs clean writing", None),
    "spec-ticket": ("The spec has no numbered requirements", "An explicit spec lists what it needs."),
    "spec-done-when": ("The spec doesn't say when it is done", "An explicit spec says how to tell it is finished."),
    "error-artifact-first": ("The error report has no pasted error",
                             "An error report starts from the error, the log or the stack trace."),
    "error-no-criteria": ("The error report doesn't say how to tell it is fixed", None),
    "goal-specific": ("The goal needs a look", None),
    "goal-must-nots": ("The goal names no shortcut that doesn't count",
                       "Reviewers look for what wouldn't count, such as loosening the tests."),
    "dp-count": ("The decision points need a look", None),
    "dp-better-worse": ("Check that each decision point reads as better against worse",
                        "The check counts better and worse words. It can't tell whether the points work."),
    "cb-length": ("Codebase understanding needs a look", None),
    "cb-mechanism": ("Codebase understanding uses a generic phrase", "Reviewers look for the mechanism by name."),
    "vn-content": ("The validation notes need a look", None),
    "llm-ai-detection": ("The {field} has words that read as AI-written", None),
    "repo-pinned": ("The repo link in Studio isn't pinned to the commit", "Save to Studio writes the pinned link."),
    "repo-avoid": ("This repo is on Mercor's avoid list", "Tasks on these repos are rejected."),
    "submit-saved-whole": ("Studio's copy of the {field} isn't whole", None),
    "rate-one-shot": ("One-shot is not rated", "Studio needs Yes or No."),
    "rate-task-success": ("Task success is not rated", "Studio needs a number from 1 to 5."),
    "rate-steering-required": ("Steering is not rated", "This task success needs a steering number."),
    "reject-ent-4-any-5": ("These ratings are rejected", None),
    "pass-enterprise-3": ("These ratings fail the Enterprise rule", None),
    "pass-personal-4": ("These ratings fail the Personal rule", None),
    "one-shot": ("Claude did it in one go", "A task Claude finishes in one go is dropped."),
    # draft check rules that are not lint
    "minutes-working": ("Studio's Claude minutes differ from the measured time", None),
    "submit-trajectory-logs": ("Upload the conversation log in Studio", "Studio takes this file only by hand."),
    "submit-from-validation": ("The conversation log is from another run", None),
    "no-calib": ("There is no validation run yet", "The submission needs a collected validation run."),
    "submission run": ("A newer run isn't collected", None),
    "lane-model": ("The run used the wrong model or effort", None),
    "async-one-prompt": ("An async run has more than one prompt", "Async is one prompt, sent unchanged."),
    "lesson-too-fast": ("Claude finished quickly", None),
    "first-prompt": ("The prompt Claude got differs from your opening prompt", "Reviewers compare the two."),
    "studio_at_claim": ("Studio still holds a previous writer's {field}", None),
    "sync": ("The {field} differs between Studio and here", None),
    "check": ("The {field} changed since the last check", None),
}
# Lint messages with no rule id (demo data, older flags): message pattern -> rule id.
RULE_BY_MESSAGE = [(re.compile(r"must-nots", re.I), "goal-must-nots"),
                   (re.compile(r"better and \d+ worse|better-vs-worse", re.I), "dp-better-worse"),
                   (re.compile(r"read as AI-written", re.I), "llm-ai-detection"),
                   (re.compile(r"casual prose is about", re.I), "casual-length"),
                   (re.compile(r"greeting|'please'|role-play|form label|read as a template", re.I), "prompt-no-tells"),
                   (re.compile(r"generic phrase", re.I), "cb-mechanism"),
                   (re.compile(r"sentences; aim for 3-5", re.I), "cb-length"),
                   (re.compile(r"more than 3 points", re.I), "dp-count")]


def _rule_key(evidence: str | None) -> str:
    """The table key of a draft-check issue's evidence: "minutes-working · validate-1" -> "minutes-working",
    "validate-1 turn 1" -> "first-prompt", "goal_fid · sync" -> "sync", "R4" -> "row"."""
    ev = (evidence or "").strip()
    if re.fullmatch(r"R\d+", ev) or re.fullmatch(r"R\d+ · .*", ev):
        return "row"
    if re.fullmatch(r"(?:calib|validate)-\d+ turn \d+", ev):
        return "first-prompt"
    if ev.startswith("check "):
        return "check"
    head, _, tail = ev.partition(" · ")
    if tail in ("studio_at_claim", "sync"):
        return tail
    return head


PLURAL = {"decision_points", "validation_notes", "reviewer_notes", "ratings", "claude_minutes"}


def _is(field: str) -> str:
    return "are" if field in PLURAL else "is"


def _fmt(s: str, field: str) -> str:
    return s.replace("{field}", FIELD_LOWER.get(field, field)).replace("{Field}", FIELD_NAME.get(field, field))


def _field_actions(field: str, verb: str = "Go to the") -> list[dict]:
    if field == "ratings":
        return [{"label": "Rate it", "action": "rate"}]
    if field in FIELD_NAME and field in TEXT_FIELDS:
        return [{"label": f"{verb} {FIELD_LOWER[field]}", "action": f"focus_field:{field}"}]
    return []


def lint_item(level: str, field: str, message: str, rule: str | None, quote: str | None = None) -> dict:
    """One lint flag as a submit item: blockers for errors, calls for warnings."""
    if not rule:
        rule = next((r for rx, r in RULE_BY_MESSAGE if rx.search(message or "")), None)
    empty = (message or "").strip().lower() == "empty"
    if empty:
        title = f"The {FIELD_LOWER.get(field, field)} {_is(field)} empty"
        why = "Studio needs it before you submit."
        actions = _field_actions(field, "Write the")
    else:
        title, why = RULES.get(rule or "", (None, None))
        title = _fmt(title, field) if title else (words_only(message, 2) and
                                                  f"{FIELD_NAME.get(field, field)}: {words_only(message, 2)}")
        why = _fmt(why, field) if why else _sentence(words_only(message, 2))
        actions = _field_actions(field)
        if rule == "repo-pinned":
            actions = [{"label": "Save to Studio", "action": "save"}]
    return {"id": _oid("lint", field, rule, message), "tone": "stop" if level == "error" else "wait",
            "title": title or "A check flagged this field", "why": why if why != title else "",
            "quote": quote or None, "field": field if field in TEXT_FIELDS or field == "ratings" else None,
            "actions": actions}


def issue_item(i: dict, tone: str) -> dict | None:
    """One draft-check issue (johto.draftcheck) as a submit item, by the rule table. Unknown rules fall back to the
    issue's message with code hidden."""
    field = i.get("field") or "run"
    key = _rule_key(i.get("evidence"))
    ai = i.get("source") == "ai"
    rule = None if ai else RULES.get(key)
    if rule:
        title = _fmt(rule[0], field)
        why = _fmt(rule[1], field) if rule[1] else _sentence(words_only(i.get("consequence"), 3))
    else:
        title = words_only(i.get("consequence"), 3) or f"Something in the {FIELD_LOWER.get(field, field)} needs a look"
        title, why = title.rstrip("."), ""
    actions = _field_actions(field)
    if key in ("submit-trajectory-logs", "submit-from-validation") and field == "trajectory_logs":
        actions = [{"label": "Show the file", "action": "reveal_log"}, {"label": "Open Studio", "action": "open_studio"}]
    elif key in ("sync", "studio_at_claim", "minutes-working", "repo-pinned", "submit-saved-whole"):
        actions = [{"label": "Save to Studio", "action": "save"}, {"label": "Open Studio", "action": "open_studio"}]
    elif field == "run":
        actions = [{"label": "Open the run", "action": "open_run"}]
    return {"id": _oid("check", i.get("id"), key), "tone": tone, "title": title, "why": why if why != title else "",
            "quote": i.get("quote") or None, "field": field if field in TEXT_FIELDS else None, "actions": actions,
            "ai": "From the deep check" if ai else None}


# --- reading a task --------------------------------------------------------------------------------------

def _texts(t: dict) -> dict:
    return {k: ((t.get("sections") or {}).get(k) or {}).get("text") or "" for k in TEXT_FIELDS}


def points(text: str) -> list[str]:
    """Decision points: blank-line paragraphs, else non-empty lines (the editor numbers them the same way)."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    if len(paras) == 1:
        lines = [x.strip() for x in paras[0].splitlines() if x.strip()]
        if len(lines) > 1:
            return lines
    return paras


def _sentences(text: str) -> int:
    try:
        from .. import lint
        return lint.sentences(text) if (text or "").strip() else 0
    except Exception:  # noqa: BLE001
        return len([s for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()])


def field_summary(key: str, sec: dict | None) -> str:
    sec = sec or {}
    text = sec.get("text") or ""
    if not text.strip():
        return "Empty"
    if sec.get("locked"):
        reason = (sec["locked"] or {}).get("reason") or ""
        return _sentence(words_only(reason, 1)).rstrip(".") if reason else "Locked"
    if key == "decision_points":
        return _n(len(points(text)), "point")
    if key == "opening_prompt":
        return _n(sec.get("words") or len(text.split()), "word")
    return _n(sec.get("sentences") or _sentences(text), "sentence")


def _slot(t: dict) -> dict:
    return (t.get("summary") or {}).get("slot") or {}


def _runs(t: dict) -> list[dict]:
    return t.get("runs") or []


def _submission_run(t: dict) -> dict | None:
    name = (t.get("summary") or {}).get("submission_run") or t.get("submission_run")
    runs = _runs(t)
    r = next((r for r in runs if r.get("name") == name), None) if name else None
    if r:
        return r
    vs = [r for r in runs if r.get("kind") == "validate"]
    return vs[-1] if vs else None


def _live_run(t: dict) -> dict | None:
    live = [r for r in _runs(t) if r.get("live") or r.get("remote_live") or r.get("state") == "active"]
    return live[-1] if live else None


def _sent_back(t: dict) -> bool:
    stage = (t.get("summary") or {}).get("stage")
    fb = ((t.get("studio") or {}).get("feedback") or "").strip()
    return stage == "sent_back" or bool(fb and stage in SUBMIT_STAGES)


def feedback_fields(t: dict) -> list[str]:
    fb = (t.get("studio") or {}).get("feedback") or ""
    secs = t.get("sections") or {}
    hit = {k for k, rx in FEEDBACK_HINTS if rx.search(fb)}
    keys = [k for k in TEXT_FIELDS if k in hit and k in secs]
    return keys or [k for k in ("goal", "validation_notes") if k in secs]


def write_fields(t: dict) -> list[str]:
    keys = [k for k in WRITE_FIELDS if k != "initial_state" or _slot(t).get("action") == "Operate"]
    return keys


def _ratings_filled(t: dict) -> bool:
    r = t.get("ratings") or {}
    return bool(r.get("one_shot") and r.get("task_success") and (r.get("steering") or r.get("passes")))


def _ratings_rejected(t: dict) -> bool:
    """Filled in, and the pass rule rejects them (Mercor: rate honestly; a task that passes only when stretched is
    too easy). The page states the rule; it never suggests other ratings."""
    return _ratings_filled(t) and (t.get("ratings") or {}).get("passes") is False


def _ratings_done(t: dict) -> bool:
    return _ratings_filled(t) and not _ratings_rejected(t)


def _first_open(t: dict) -> str | None:
    """The first Write field with nothing in it (ratings count when unrated)."""
    texts = _texts(t)
    for k in write_fields(t):
        if k == "ratings":
            if not _ratings_done(t):
                return k
        elif not texts.get(k, "").strip():
            return k
    return None


def step_of(t: dict) -> str:
    stage = (t.get("summary") or {}).get("stage") or ""
    if t.get("discard") and stage not in ("in_review", "accepted", "rejected"):
        return "review"
    if _sent_back(t):
        return "write"
    if stage in AFTER_STAGES:
        return "review"
    if stage in SUBMIT_STAGES:
        return "submit"
    if stage == "rate":
        return "write"
    if stage == "details":
        sub = _submission_run(t)
        return "write" if sub and sub.get("collected_at") else "prompt"
    if stage in RUN_STAGES:
        return "run"
    return "prompt"


# --- run -------------------------------------------------------------------------------------------------

def _run_for_page(t: dict) -> dict | None:
    return _live_run(t) or _submission_run(t) or (_runs(t)[-1] if _runs(t) else None)


def run_block(t: dict) -> dict | None:
    r = _run_for_page(t)
    stage = (t.get("summary") or {}).get("stage") or ""
    mon = (t.get("summary") or {}).get("monitor") or t.get("monitor") or {}
    if r is None:
        queued = stage in ("waiting_validate", "waiting_calib")
        return {"id": None, "name_words": "validation 1", "state": "queued" if queued else "none",
                "state_words": "Queued for the lane" if queued else "Not started", "minutes": None, "budget": 90,
                "turns": None, "tests_words": None, "changed_files": None, "claude_message": None, "lane": "none",
                "notes": [], "drill": None, "runs": []}
    f = r.get("facts") or {}
    mon = mon if mon.get("run") == r.get("name") else {}
    live = bool(r.get("live") or r.get("remote_live") or r.get("state") == "active")
    # The same rule as Now's queue (data.lane_need_of): a lane_waiting need for this run is a question for him.
    need = t.get("lane_need") or {}
    need = need if (not need.get("run") or need.get("run") == r.get("name")) else {}
    question = (need.get("question") if need.get("kind") == "lane_waiting" else None) or mon.get("question")
    if live:
        state = "asks" if (need.get("kind") == "lane_waiting" or mon.get("question")) else "working"
    elif r.get("collected_at"):
        state = "collected"
    elif stage == "collect_failed":
        state = "collect_failed"
    elif r.get("collecting") or stage == "collecting":
        state = "collecting"
    elif r.get("has_transcript"):
        state = "ended"
    else:
        state = "none"
    words = {"working": "Claude is working", "asks": "Claude asked you something", "collected": "Collected",
             "collect_failed": "Ended, not collected", "collecting": "Collecting", "ended": "Ended",
             "none": "Not started"}[state]
    minutes = mon.get("working_minutes") if mon.get("working_minutes") is not None else f.get("active_min")
    claude_min = mon.get("task_minutes") if mon.get("task_minutes") is not None else f.get("task_claude_min")
    tests = f.get("tests") or {}
    files = f.get("diffstat") or [{"path": p} for p in f.get("files_edited") or []]
    message = question if live and question else f.get("final_message_excerpt")
    host = (mon.get("host") or "").upper()
    lane = "none" if not live else ("elsewhere" if host == "PC" else "here")
    notes = [{"id": n.get("id"), "at": n.get("at") or n.get("created_at"), "text": n.get("text") or ""}
             for n in r.get("notes") or [] if isinstance(n, dict)]
    runs = [{"id": x.get("name"), "name_words": run_words(x.get("name")) or x.get("name"),
             "state_words": "Collected" if x.get("collected_at") else ("Running" if x.get("live") else "Not collected"),
             "chosen": x.get("name") == (t.get("summary") or {}).get("submission_run"),
             "collected": bool(x.get("collected_at"))} for x in _runs(t)]
    model_ok = f.get("model_ok")
    effort_ok = f.get("effort_ok")
    drill = {
        "files": [{"path": x.get("path"), "add": x.get("add"), "del": x.get("del"), "new": bool(x.get("new")),
                   "test": bool(x.get("test"))} for x in files],
        "model_words": ("Opus 5.5, high effort ✓" if model_ok and effort_ok is not False
                        else "The model or effort was wrong" if model_ok is False or effort_ok is False else ""),
        "tests_runs": tests.get("runs"),
        "tests_hidden": tests.get("last_ok") is None and bool(tests.get("runs")),
        "message": f.get("final_message_excerpt") or None,
        "packs": list(r.get("packs") or []),
        "has_transcript": bool(r.get("has_transcript")),
        "has_diff": bool(r.get("has_diff")),
    }
    return {"id": r.get("name"), "kind": r.get("kind"), "name_words": run_words(r.get("name")) or "the run",
            "state": state, "state_words": words,
            "minutes": round(minutes) if isinstance(minutes, (int, float)) else None,
            # The task's Claude minutes against its budget: the figure Now and the lane drawer show.
            "claude_min": round(claude_min) if isinstance(claude_min, (int, float)) else None,
            "budget": mon.get("budget") or 90, "turns": f.get("turns") or mon.get("turns"),
            "tests_words": {True: "tests passed", False: "tests failing"}.get(tests.get("last_ok")),
            "changed_files": len(files) if (f or files) else None,
            "claude_message": (words_only(_whole(message), 3) or None) if message else None,
            "question": (words_only(_whole(question), 1) or None) if state == "asks" and question else None,
            "lane": lane, "notes": notes, "drill": drill, "runs": runs}


def run_lede(run: dict | None, stage: str) -> str:
    if not run or run.get("state") in ("none", "queued"):
        if stage in ("waiting_validate", "waiting_calib"):
            return "The run is queued. It starts when the lane frees."
        return "Start the validation run."
    s = run["state"]
    if s == "working":
        m = run.get("minutes")
        return f"Claude is working, {_n(m, 'minute')} in." if m else "Claude is working."
    if s == "asks":
        return "Claude asked you something."
    if s in ("collecting", "ended"):
        return "Claude finished. Collecting the run."
    if s == "collect_failed":
        return "The run ended but wasn't collected."
    if stage == "decide":
        return f"{_cap(run['name_words'])} is collected. Keep the task or drop it."
    return "The run is collected."


# --- findings --------------------------------------------------------------------------------------------

def _source_words(src: dict, reviews_by_name: dict) -> str:
    kind = (src or {}).get("type")
    if kind == "review":
        name = str(src.get("ref") or "").split("/")[-1]
        rv = reviews_by_name.get(name) or {}
        who = "GPT Pro" if (rv.get("source") or "gptpro") == "gptpro" else "The outside"
        turn = rv.get("turn")
        return f"{who}'s review of turn {turn}" if turn else f"{who}'s review"
    if kind == "dossier":
        return "From Claude's research"
    if kind == "facts":
        return "From the run's own record"
    if kind == "audit":
        return "Mercor's check"
    return "The review"


def finding_rows(rows: list[dict], plain: dict, plain_pending: bool, reviews_list: list[dict],
                 run: str | None) -> list[dict]:
    by_name = {r.get("name"): r for r in reviews_list or []}
    out = []
    for r in rows:
        if r.get("review_status") in ("withdrawn", "superseded"):
            continue
        res = (r.get("result") or {}).get("value") or "unknown"
        p = plain.get(r["id"])
        shown = words_only(r.get("finding"), 5, strict=True)   # the card's fallback: whole sentences only
        marks = r.get("marks") or {}
        out.append({
            "id": r["id"],
            "plain": p or None,
            "plain_state": "ready" if p else ("pending" if plain_pending else "missing"),
            "finding": shown or None,
            "result": res if res in RESULT_WORDS else "unknown",
            "result_words": RESULT_WORDS.get(res, RESULT_WORDS["unknown"]),
            "basis_words": BASIS_WORDS.get(r.get("basis") or "none", BASIS_WORDS["none"]),
            "serious": r.get("severity") == "high",
            "scope": r.get("scope"),
            "suggested_scope": r.get("suggested_scope"),
            "kind": r.get("kind"),
            "marks": {"goal": bool(marks.get("goal")), "dp": marks.get("dp") or None, "notes": bool(marks.get("notes"))},
            "other_run": bool(r.get("other_run")),
            "changed_since_mark": bool(r.get("changed_since_mark")),
            "refuted": ((r.get("verify") or {}).get("status") == "refuted"),
            "source_words": _source_words((r.get("sources") or [{}])[0], by_name),
            "as_written": words_only(r.get("finding"), 1) or None,
            "limits_words": words_only(r.get("limits"), 3) or None,
            "drill": {"finding": r.get("finding"), "refs": list(r.get("refs") or []), "limits": r.get("limits"),
                      "sources": [{"ref": s.get("ref"), "finding_id": s.get("finding_id")} for s in r.get("sources") or []]},
        })
    order = {"missed": 0, "partial": 1, "credit": 2, "unknown": 3}
    return sorted(out, key=lambda x: (order.get(x["result"], 9), not x["serious"], _row_num(x["id"])))


def _row_num(rid: str) -> int:
    m = re.search(r"\d+", rid or "")
    return int(m.group(0)) if m else 0


def coverage(reviews_list: list[dict], run: str | None, turns: int | None) -> dict:
    """Spec 7.7: the turns with a saved review for the submission run, and the flag when the newest has none."""
    reviewed = sorted({int(r["turn"]) for r in reviews_list or [] if r.get("run") == run
                       and isinstance(r.get("turn"), int) and r["turn"] > 0})
    whole = any(r.get("run") == run and not r.get("turn") for r in reviews_list or [])
    total = int(turns or 0)
    if whole and not reviewed and total:
        reviewed = list(range(1, total + 1))
    missing = [n for n in range(1, total + 1) if n not in reviewed]
    newest_missing = bool(total) and total not in reviewed
    flag = action = None
    if run and total and not reviewed:
        flag = "No review of this run yet."
        action = "Review all " + (_n(total, "turn") if total > 1 else "1 turn") if total > 1 else "Review turn 1"
    elif newest_missing:
        flag = f"The review covers {_turns_words(reviewed)} of {total}."
        action = "Review " + _turns_words(missing)
    return {"run": run, "reviewed_turns": reviewed, "turns": total or None, "missing_turns": missing if flag else [],
            "flag": flag, "action": action}


# --- submit ----------------------------------------------------------------------------------------------

def _qc_stale(t: dict) -> bool:
    """AutoQC ran before the last save (the checklist's own row says so)."""
    return any(c.get("key") == "autoqc" and c.get("status") == "pending" and "before" in (c.get("title") or "")
               for c in t.get("checklist") or [])


def _qc_dim_field(d: dict) -> str | None:
    try:
        from .domain.checklist import field_for_dimension
    except Exception:  # noqa: BLE001
        return d.get("field_key")
    return d.get("field_key") or field_for_dimension(d.get("dimension", ""), d.get("justification", ""))


def _qc_dims(t: dict) -> list[dict]:
    """The AutoQC dimensions that speak for what Studio holds. Before the first Save to Studio from this app, the
    audit came from the prompt check (only the prompt was in Studio, the other fields were empty), so only the
    prompt's own dimensions count; the rest wait for the save."""
    st = t.get("studio") or {}
    dims = (st.get("qc") or {}).get("dims") or []
    if st.get("saved_at"):
        return dims
    try:
        from .. import actions
        prompt_dims = set(actions.prompt_dimensions())
    except Exception:  # noqa: BLE001
        prompt_dims = set()
    return [d for d in dims if _qc_dim_field(d) == "opening_prompt" or d.get("dimension") in prompt_dims]


def _qc(t: dict) -> dict:
    st = t.get("studio") or {}
    q = st.get("qc")
    saved = st.get("saved_at")
    if not q:
        return {"state": "none", "words": "Mercor's automatic checks run when you save." if not saved
                else "Mercor's automatic checks haven't run since the save."}
    if q.get("status") == "running":
        return {"state": "running", "words": f"Mercor's automatic checks are running. They started at {_hhmm(q.get('started_at'))}."}
    if q.get("status") not in (None, "completed"):
        return {"state": "failed", "words": "Mercor's automatic checks didn't finish."}
    fails = [d for d in _qc_dims(t) if d.get("status") == "fail"]
    if _qc_stale(t):
        return {"state": "stale", "words": "Mercor's automatic checks ran before your last save. Save again to rerun them."}
    if fails:
        return {"state": "failed", "words": f"{_n(len(fails), 'automatic check')} failed."}
    if not saved:
        return {"state": "none", "words": "Mercor's automatic checks run on every field when you save."}
    c = q.get("counts") or {}
    return {"state": "passed", "words": f"{_n(c.get('pass') or 0, 'automatic check')} passed at "
                                        f"{_hhmm(q.get('finished_at') or q.get('started_at'))}."}


def _lint_flags(t: dict) -> list[tuple]:
    """Live lint over the task's sections, with rule ids: (level, field key, message, rule, quote)."""
    try:
        from .. import lint
        from .data import SECTION_KEYS
    except Exception:  # noqa: BLE001
        return [(f.get("level"), k, f.get("message"), None, None)
                for k, s in (t.get("sections") or {}).items() for f in s.get("lint") or []]
    texts = _texts(t)
    slot = _slot(t)
    sec = {SECTION_KEYS[k]: v for k, v in texts.items() if k in SECTION_KEYS}
    out = []
    flags = []
    try:
        flags += list(lint.check_repo((t.get("summary") or {}).get("repo") or {}))
    except Exception:  # noqa: BLE001
        pass
    if texts["opening_prompt"].strip():
        flags += list(lint.check_prompt(texts["opening_prompt"], slot.get("prompt_style", "")))
    else:
        flags.append(("error", "Opening Prompt", "empty"))
    flags += list(lint.check_sections(sec, slot))
    for f in flags:
        key = LINT_FIELD.get(f[1])
        if key is None or key == "ratings":
            continue
        out.append((f[0], key, f[2], getattr(f, "rule", None), getattr(f, "quote", None)))
    return out


def _qc_items(t: dict) -> tuple[list[dict], list[dict]]:
    st = t.get("studio") or {}
    q = st.get("qc") or {}
    if q.get("status") != "completed":
        return [], []
    if _qc_stale(t):
        return [], []
    changed = set(st.get("changed_since_save") or [])
    blockers, calls = [], []
    for d in _qc_dims(t):
        status = d.get("status")
        if status not in ("fail", "neutral"):
            continue
        fk = _qc_dim_field(d)
        dim = d.get("dimension") or "a check"
        why = words_only(d.get("justification"), 3) or ""
        if status == "fail":
            # A failed check blocks Submit until it passes, so it is always a blocker (one colour on both
            # sides). Changed since: Save reruns it, so it needs no button of its own.
            fixed = fk in changed
            blockers.append({"id": _oid("qc", dim), "tone": "stop",
                             "title": (f"Mercor's check failed: {dim}. You changed it since. Save again to rerun it."
                                       if fixed else f"Mercor's check failed: {dim}"),
                             "why": why, "quote": None, "field": fk, "ai": "Mercor's automatic check",
                             "actions": [] if fixed else ((_field_actions(fk) if fk else [])
                                                          + [{"label": "Run the check again", "action": "rerun_qc"}])})
        else:
            calls.append({"id": _oid("qc", dim), "tone": "wait", "title": f"Mercor's check left a note: {dim}",
                          "why": why, "quote": None, "field": fk, "ai": "Mercor's automatic check",
                          "actions": _field_actions(fk) if fk else []})
    return blockers, calls


def _row_items(rows: list[dict], run: str | None) -> tuple[list[dict], list[dict]]:
    """Live grading questions: a finding placed in the goal or a decision point with no scope call, placed but
    from another run, or refuted by a later check."""
    blockers, calls = [], []
    unscoped: dict[str, list[str]] = {}
    for r in rows or []:
        m = r.get("marks") or {}
        where = "goal" if m.get("goal") else "decision points" if m.get("dp") else "notes" if m.get("notes") else None
        if not where:
            continue
        field = "goal" if m.get("goal") else "decision_points" if m.get("dp") else "validation_notes"
        show = [{"label": "Show it", "action": f"show_finding:{r['id']}"}]
        if (r.get("verify") or {}).get("status") == "refuted":
            blockers.append({"id": _oid("row", r["id"], "refuted"), "tone": "stop",
                             "title": f"A finding in your {where} was refuted by a later check",
                             "why": "Check it still holds before you rely on it.", "quote": None, "field": field,
                             "actions": show})
        res = r.get("result") or {}
        if run and res.get("run") and res.get("run") != run:
            calls.append({"id": _oid("row", r["id"], "run"), "tone": "wait",
                          "title": f"A finding in your {where} comes from another run",
                          "why": f"The submission uses {run_words(run)}.", "quote": None, "field": field,
                          "actions": show})
        if where != "notes" and not r.get("scope"):
            unscoped.setdefault(field, []).append(r["id"])
        elif where != "notes" and r.get("scope") == "outside":
            calls.append({"id": _oid("row", r["id"], "outside"), "tone": "wait",
                          "title": f"A finding in your {where} is marked outside the task",
                          "why": "Reviewers drop points the task never asked for.", "quote": None, "field": field,
                          "actions": show})
    for field, ids in unscoped.items():
        where = "goal" if field == "goal" else "decision points"
        n = len(ids)
        calls.insert(0, {"id": _oid("row", field, "scope"), "tone": "wait",
                         "title": (f"A finding in your {where} has no scope call" if n == 1 else
                                   f"{_cap(_number_word(n))} findings in your {where} have no scope call"),
                         "why": "Say whether the task asked for it, needed it, or sits outside it.", "quote": None,
                         "field": field, "rows": ids,
                         "actions": [{"label": "Show it" if n == 1 else "Show them", "action": f"show_finding:{ids[0]}"}]})
    return blockers, calls


# Checklist rows a save to Studio settles by itself: they are part of step 1, not items (Gemini, round 2).
SAVE_SETTLES = {("studio_sync", "pending"), ("studio_sync", "fail"), ("studio_sync", "warn"), ("gists", "pending"),
                ("autoqc", "pending")}
PASS_WORDS = {"autoqc": "Mercor's automatic checks pass", "studio_sync": "Studio has your latest text",
              "trajectory_logs": "The conversation log is in Studio",
              "gists": "The conversation and the changes are linked in Studio",
              "lint": "The local checks pass", "provenance": "Your fields are in your own words",
              "validation_run": "The validation run is collected and checked"}


def _checklist_items(t: dict) -> tuple[list[dict], list[dict], list[dict]]:
    blockers, calls, passes = [], [], []
    slot = _slot(t)
    for row in t.get("checklist") or []:
        key, status = row.get("key"), row.get("status")
        if (key, status) in SAVE_SETTLES or key in ("autoqc", "lint", "upstream_hidden"):
            if key == "autoqc" and status == "pass":
                passes.append({"title": PASS_WORDS["autoqc"]})
            elif key == "lint" and status == "pass":
                passes.append({"title": PASS_WORDS["lint"]})
            elif key == "autoqc" and status == "fail" and not row.get("dims") and "didn't finish" in (row.get("title") or ""):
                blockers.append({"id": _oid("cl", "qc_unfinished"), "tone": "stop", "title": "Mercor's checks didn't finish",
                                 "why": "Run them again on what Studio holds.", "quote": None, "field": None,
                                 "actions": [{"label": "Run the check again", "action": "rerun_qc"}]})
            continue
        if status == "pass":
            if key == "ratings":
                passes.append({"title": f"The ratings pass the {SCALE.get(slot.get('codebase_type'), '')} rule".replace("  ", " ")})
            elif key in PASS_WORDS:
                passes.append({"title": PASS_WORDS[key]})
            continue
        tone = "stop" if status == "fail" and row.get("blocking", True) else "wait"
        detail = row.get("detail") or ""
        if key == "trajectory_logs":
            if status == "pending":
                calls.append({"id": _oid("cl", "conv_log_check"), "tone": "wait",
                              "title": "Check that the conversation log is in Studio",
                              "why": "The app hasn't read Studio's file list yet.", "quote": None, "field": None,
                              "actions": [{"label": "Refresh from Studio", "action": "refresh"}]})
                continue
            other = "hold" in (row.get("title") or "")
            (blockers if tone == "stop" else calls).append({
                "id": _oid("cl", "conv_log"), "tone": tone,
                "title": ("Studio holds another run's conversation log" if other
                          else "Upload the conversation log in Studio"),
                "why": ("Upload this run's file and remove the other one." if other
                        else "Studio takes this file only by hand."),
                "quote": None, "field": None,
                "actions": [{"label": "Show the file", "action": "reveal_log"},
                            {"label": "Open Studio", "action": "open_studio"}]})
        elif key == "ratings":
            missing = "not set" in (row.get("title") or "").lower() or "missing" in detail.lower() or \
                "must be" in detail.lower()
            r = t.get("ratings") or {}
            title = ("One-shot is not rated" if not r.get("one_shot") else
                     "The run is not rated" if missing else "These ratings are rejected")
            blockers.append({"id": _oid("cl", "ratings"), "tone": "stop", "title": title,
                             "why": _sentence(words_only(detail.split(";")[0], 2)) if not missing or r.get("one_shot") else
                             "Studio needs one-shot, task success and steering.",
                             "quote": None, "field": "ratings", "actions": [{"label": "Rate it", "action": "rate"}]})
        elif key == "validation_run":
            fix = row.get("fix") or {}
            acts = ([{"label": "Collect the run", "action": "collect"}] if fix.get("action") == "collect"
                    else [{"label": "Open the run", "action": "open_run"}])
            # The row's summary (model, effort, prompts, minutes) is for the CLI; only the problems are his concern.
            text = words_only("; ".join(b for b in detail.split(" · ") if not _RUN_SUMMARY.match(b.strip())), 2)
            (blockers if tone == "stop" else calls).append({
                "id": _oid("cl", "validation_run"), "tone": tone,
                "title": ("The validation run isn't collected" if "collect" in detail.lower() or "not collected" in
                          (row.get("title") or "").lower() else "Check the validation run"),
                "why": _sentence(text), "quote": None, "field": None, "actions": acts})
        elif key == "provenance":
            fix = row.get("fix") or {}
            fk = fix.get("field")
            blockers.append({"id": _oid("cl", "provenance"), "tone": "stop",
                             "title": ("A field matches AI text word for word" if "overlap" in (row.get("title") or "").lower()
                                       else "The overlap check didn't run"),
                             "why": ("Reword it, or mark it as your own words. Save to Studio waits until then."
                                     if fk else _sentence(words_only(detail, 3))),
                             "quote": None, "field": fk, "actions": _field_actions(fk) if fk else []})
        elif key == "studio_saved":
            fk = (row.get("fix") or {}).get("field")
            flags = row.get("flags") or []
            cut = [f.get("field") for f in flags if f.get("level") == "error"]
            names = [FIELD_LOWER.get(LINT_FIELD.get(f.get("field"), ""), "") or (f.get("field") or "").lower()
                     for f in flags]
            names = [n for i, n in enumerate(names) if n and n not in names[:i]]
            listed = _list_words(names) if names else "field"
            (blockers if tone == "stop" else calls).append({
                "id": _oid("cl", "studio_saved"), "tone": tone,
                "title": (f"Studio's copy of the {listed} stops mid-word" if cut
                          else f"Studio holds less than your {listed}"),
                "why": "Save to Studio again so it holds all of it.", "quote": None, "field": fk,
                "actions": [{"label": "Save to Studio", "action": "save"}] + (_field_actions(fk) if fk else [])})
        elif key == "studio_text":
            (blockers if tone == "stop" else calls).append({
                "id": _oid("cl", "studio_text"), "tone": tone, "title": "Studio holds a previous writer's text",
                "why": _sentence(words_only(detail, 3)), "quote": None, "field": None,
                "actions": [{"label": "Open Studio", "action": "open_studio"}]})
        elif key == "upstream":
            calls.append({"id": _oid("cl", "upstream"), "tone": "wait", "title": "Related upstream work isn't mentioned",
                          "why": "Mention it in codebase understanding or the notes for the reviewer.", "quote": None,
                          "field": "codebase", "actions": _field_actions("codebase")})
        else:
            text = words_only(row.get("title"), 2)
            (blockers if tone == "stop" else calls).append({
                "id": _oid("row", key, status), "tone": tone, "title": text or "A check needs a look",
                "why": _sentence(words_only(detail, 3)), "quote": None, "field": None, "actions": []})
    return blockers, calls, passes


# Draft-check rules the live rows above already cover (lint, grading marks, the run and log rows).
COVERED_LIVE = {"row", "submit-trajectory-logs", "submit-from-validation", "no-calib", "lane-model",
                "async-one-prompt", "submit-saved-whole", "repo-pinned"}


def _check_items(check: dict | None) -> tuple[list[dict], list[dict]]:
    if not check:
        return [], []
    blockers, calls = [], []
    lint_rules = set(RULES) - {"minutes-working", "submit-trajectory-logs", "submit-from-validation", "no-calib",
                               "submission run", "lane-model", "async-one-prompt", "lesson-too-fast",
                               "first-prompt", "studio_at_claim", "sync", "check"}
    for group in ("attention", "decide"):
        for i in check.get(group) or []:
            if not isinstance(i, dict):
                continue
            key = _rule_key(i.get("evidence"))
            if i.get("source") != "ai" and (key in COVERED_LIVE or key in lint_rules):
                continue
            if i.get("field") == "claude_minutes" and i.get("category") == "missing_required":
                continue   # Save to Studio fills Claude minutes from the measured time
            if i.get("category") == "save_mismatch" and group == "attention" and key == "sync":
                continue   # local edits Studio doesn't have yet: Save settles it (step 1)
            item = issue_item(i, "wait")
            if item is None:
                continue
            if group == "attention" and i.get("source") != "ai" and i.get("category") in ("missing_required",
                                                                                          "missing_upload"):
                item["tone"] = "stop"
                blockers.append(item)
            else:
                calls.append(item)
    return blockers, calls


def _group_field_calls(items: list[dict]) -> list[dict]:
    """Two or more plain calls on one field become one item with its parts listed: "Three things in the opening
    prompt", one button. Calls from Mercor's checks, the deep check or the findings stay on their own."""
    plain = lambda it: it.get("field") in TEXT_FIELDS and not it.get("ai") and not it.get("rows") and not it.get("quote")
    by: dict[str, list[dict]] = {}
    for it in items:
        if plain(it):
            by.setdefault(it["field"], []).append(it)
    out, done = [], set()
    for it in items:
        f = it.get("field")
        if not plain(it) or len(by.get(f, [])) < 2:
            out.append(it)
            continue
        if f in done:
            continue
        done.add(f)
        group = by[f]
        out.append({"id": _oid("group", f, *[g["id"] for g in group]), "tone": "wait",
                    "title": f"{_cap(_number_word(len(group)))} things in the {FIELD_LOWER[f]}", "why": "",
                    "quote": None, "field": f, "parts": [{"title": g["title"], "why": g.get("why") or ""} for g in group],
                    "actions": _field_actions(f)})
    return out


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        k = (it["title"], it.get("field"))
        if it["id"] in seen or k in seen:
            continue
        seen.update({it["id"], k})
        out.append(it)
    return out


def sent_list(t: dict) -> list[dict]:
    secs = t.get("sections") or {}
    slot = _slot(t)
    out = []
    for k in ("opening_prompt", "goal", "decision_points", "codebase", "initial_state", "validation_notes",
              "reviewer_notes"):
        if k == "initial_state" and slot.get("action") != "Operate":
            continue
        s = secs.get(k) or {}
        text = (s.get("text") or "").strip()
        if k == "reviewer_notes" and not text:
            continue
        out.append({"label": FIELD_NAME[k], "value": field_summary(k, s),
                    "tone": "stop" if not text else "neutral", "field": k})
    r = t.get("ratings") or {}
    scale = SCALE.get(slot.get("codebase_type"), "")
    if r.get("one_shot") and r.get("task_success"):
        out.append({"label": "Ratings", "value": f"One-shot {r['one_shot']} · Success {r['task_success']} · "
                                                 f"Steering {r.get('steering') or '-'}",
                    "tone": "ok" if r.get("passes") else "stop", "field": "ratings",
                    "pass_rule": f"Passes the {scale} rule" if r.get("passes") else f"Rejected by the {scale} rule"})
    else:
        out.append({"label": "Ratings", "value": "Not rated", "tone": "stop", "field": "ratings"})
    mins = r.get("claude_minutes")
    if mins is None:
        sub = _submission_run(t)
        mins = ((sub or {}).get("facts") or {}).get("active_min")
    out.append({"label": "Claude minutes", "value": f"{round(mins)}, measured" if mins is not None else "Not measured",
                "tone": "neutral" if mins is not None else "stop", "field": None})
    log = next((c for c in t.get("checklist") or [] if c.get("key") == "trajectory_logs"), None)
    if log:
        out.append({"label": "Conversation log", "value": "In Studio" if log.get("status") == "pass" else
                    ("Not checked" if log.get("status") == "pending" else "Not uploaded"),
                    "tone": "ok" if log.get("status") == "pass" else "stop" if log.get("status") == "fail" else "neutral",
                    "field": None})
    return out


def submit_block(t: dict, *, rows: list[dict], check: dict | None, run: str | None) -> dict:
    flags = _lint_flags(t)
    b_lint = [lint_item(*f) for f in flags if f[0] == "error"]
    c_lint = [lint_item(*f) for f in flags if f[0] != "error"]
    b_qc, c_qc = _qc_items(t)
    b_row, c_row = _row_items(rows, run)
    b_cl, c_cl, passes = _checklist_items(t)
    b_ck, c_ck = _check_items(check)
    blockers = _dedupe(b_cl + b_qc + b_lint + b_row + b_ck)
    calls = _group_field_calls(_dedupe(c_qc + c_cl + c_row + c_lint + c_ck))
    if not b_lint and not any(p["title"] == PASS_WORDS["lint"] for p in passes):
        passes.append({"title": PASS_WORDS["lint"]}) if not c_lint else None
    stage = (t.get("summary") or {}).get("stage")
    qc = _qc(t)
    can = stage == "ready_to_submit" and not blockers and qc["state"] == "passed"
    st = t.get("studio") or {}
    changed = [FIELD_LOWER.get(k, k) for k in st.get("changed_since_save") or []]
    save_words = ("Saves your fields and ratings, adds the conversation and changes as private links, then runs "
                  "Mercor's automatic checks. About 2 minutes.")
    if st.get("saved_at"):
        save_state = (f"Saved {_hhmm(st['saved_at'])}. Changed since: {_list_words(changed)}." if changed
                      else f"Saved {_hhmm(st['saved_at'])}. Nothing changed since.")
    elif studio_block(t)["words"] != "Not saved yet":
        save_state = f"{studio_block(t)['words']}, not saved from this app yet."
    else:
        save_state = "Not saved yet."
    return {"blockers": blockers, "calls": calls, "passes": passes, "sent": sent_list(t), "can_submit": can,
            "qc": qc, "save_words": save_words, "save_state": save_state,
            "saved": bool(st.get("saved_at")) and not changed,
            "submit_why": ("" if can else
                           "Save to Studio first." if not st.get("saved_at") else
                           f"Save again first. Your {_list_words(changed)} changed since." if changed else
                           "Mercor's checks failed. Fix what they found, then save again." if qc["state"] == "failed" else
                           "Waits for Mercor's checks to pass." if qc["state"] != "passed" else
                           "Fix the items above first." if blockers else
                           "Finish writing first.")}


# --- header, fields, path, lede --------------------------------------------------------------------------

def title_of(t: dict, origin: dict) -> str:
    s = t.get("summary") or {}
    label = s.get("label") or ""
    repo_name = ((s.get("repo") or {}).get("full_name") or "").split("/")[-1] or "Task"
    head, _, problem = label.partition(" · ")
    if re.fullmatch(r"Task [a-z0-9]{6,}", problem or "", re.I):
        problem = ""
    if not problem and origin.get("title"):
        problem = origin["title"]
    name = head if head and not re.fullmatch(r"Task [a-z0-9]{6,}", head, re.I) else repo_name
    return f"{name} · {problem}" if problem else f"{name} · {ACTION_WORD.get(_slot(t).get('action'), 'Task')}"


def studio_block(t: dict) -> dict:
    st = t.get("studio") or {}
    sm = (t.get("summary") or {}).get("studio") or {}
    fields = st.get("fields") or {}
    have = [k for k in ("opening_prompt", "goal", "decision_points", "codebase", "validation_notes")
            if str(fields.get(STUDIO_ID[k]) or "").strip()]
    changed = st.get("changed_since_save") or []
    if st.get("saved_at"):
        words = f"Saved {_hhmm(st['saved_at'])}" + (f", {_n(len(changed), 'change')} since" if changed else "")
    elif have:
        names = ["the prompt" if k == "opening_prompt" else FIELD_LOWER[k] for k in have]
        words = "Studio has " + _list_words(names[:1] + [n.replace("the ", "") for n in names[1:]])
        if len(have) >= 5:
            words = "Studio has every field"
    else:
        words = "Not saved yet"
    return {"url": st.get("url") or sm.get("url"), "words": words, "saved_at": st.get("saved_at"),
            "status": st.get("status") or sm.get("status"), "changed": changed}


def fields_block(t: dict, step: str, current: str | None, fb_fields: list[str]) -> dict:
    secs = t.get("sections") or {}
    fields = (t.get("studio") or {}).get("fields") or {}
    out = {}
    for k in TEXT_FIELDS:
        if k == "initial_state" and _slot(t).get("action") != "Operate":
            continue
        s = secs.get(k) or {}
        text = (s.get("text") or "").strip()
        errs = [f for f in s.get("lint") or [] if f.get("level") == "error"]
        if s.get("locked"):
            state = "locked"
        elif k == current:
            state = "current"
        elif text and not errs:
            state = "done"
        else:
            state = "empty" if not text else "current"
        out[k] = {"state": state, "words": s.get("words") or 0, "summary": field_summary(k, s),
                  "in_studio": bool(str(fields.get(STUDIO_ID.get(k, "")) or "").strip()),
                  "points": len(points(text)) if k == "decision_points" else None,
                  "flags": len(s.get("lint") or []), "sent_back": k in fb_fields,
                  "overlaps": len([o for o in s.get("overlap") or [] if not o.get("accepted")])}
    r = t.get("ratings") or {}
    out["ratings"] = {"state": "current" if current == "ratings" else ("done" if _ratings_done(t) else "empty"),
                      "words": None, "summary": ("Passes" if r.get("passes") else "Rejected") if _ratings_filled(t)
                      else "Not rated", "in_studio": False, "points": None, "flags": 0, "sent_back": False,
                      "overlaps": 0}
    return out


def path_block(t: dict, step: str, run: dict | None, fields: dict) -> list[dict]:
    stage = (t.get("summary") or {}).get("stage") or ""
    secs = t.get("sections") or {}
    order = ["prompt", "run", "write", "submit"]
    at = order.index(step) if step in order else 4
    prompt_sec = secs.get("opening_prompt") or {}
    prompt_summary = field_summary("opening_prompt", prompt_sec)
    if not (prompt_sec.get("text") or "").strip():
        prompt_summary = "Not written"
    sub = _submission_run(t)
    if run and run.get("kind") == "calib" and at == 0:   # a calibration run is not the validation run
        run_summary = "Queued" if run.get("state") == "queued" else "Not started"
    elif run and run.get("minutes") is not None and run.get("state") not in ("none", "queued"):
        run_summary = f"{run['minutes']} min" + (f" · {_n(run['turns'], 'turn')}" if run.get("turns") else "")
    elif run and run.get("state") == "queued":
        run_summary = "Queued"
    elif run and run.get("state") == "collected":
        run_summary = "Collected"
    elif at >= 2 and not t.get("discard"):
        run_summary = "Done"
    else:
        run_summary = "Not run" if t.get("discard") else "Not started"
    wf = [k for k in write_fields(t)]
    done = [k for k in wf if fields.get(k, {}).get("state") in ("done", "locked")]
    if len(done) == len(wf):
        write_summary = "All fields" if len(wf) != 5 else "All five fields"
    elif not done:
        write_summary = "Not started" if at < 2 else "Nothing yet"
    else:
        nxt = next((k for k in wf if k not in done), None)
        write_summary = (f"{FIELD_NAME[done[-1]]} done, on {FIELD_LOWER[nxt]}" if len(done) == 1 and nxt
                         else f"{len(done)} of {len(wf)} done")
    st = t.get("studio") or {}
    if step == "review":
        since = (t.get("stage_info") or {}).get("since") or st.get("polled_at")
        last = {"in_review": f"In review since {_day(since)}" if since else "In review",
                "accepted": "Accepted", "rejected": "Rejected", "dropped": "Dropped", "discarded": "Dropped",
                "unclaimed": "Unclaimed"}.get(stage, "Submitted")
        if t.get("discard") and stage not in ("in_review", "accepted", "rejected"):
            last = "Dropped"
        sub_entry = {"id": "review", "label": "Review", "state": "done" if stage == "accepted" else
                     "blocked" if stage == "rejected" else "current", "summary": last}
    else:
        sub_entry = {"id": "submit", "label": "Submit", "state": "current" if step == "submit" else "later",
                     "summary": "Saved, not submitted" if st.get("saved_at") else studio_block(t)["words"]}
    def state(i: int) -> str:
        return "done" if i < at else "current" if i == at else "later"
    run_state = state(1)
    if step == "prompt" and run and run.get("state") == "queued":
        run_state = "wait"
    if step == "write" and sub and not sub.get("collected_at"):
        run_state = "wait"
    prompt_state, write_state = state(0), state(2)
    if step == "review":
        # After the task left him: the strip is his history, so it says only what really happened.
        submitted = stage in ("in_review", "accepted", "rejected") and not t.get("discard")
        ran = bool(run) and run.get("state") in ("collected", "ended", "collect_failed", "collecting")
        run_state = "done" if ran else "later"
        if not ran:
            run_summary = "Not run"
        written = [k for k in wf if fields.get(k, {}).get("state") in ("done", "locked")
                   or fields.get(k, {}).get("in_studio") or (k == "ratings" and _ratings_filled(t))]
        every = len(written) == len(wf)
        all_words = "All fields" if len(wf) != 5 else "All five fields"
        prompt_state = "done" if (prompt_sec.get("text") or "").strip() or submitted else "later"
        if submitted:
            write_state = "done"
            write_summary = all_words if every else "Written in Studio"
        else:
            write_state = "done" if every else "later"
            write_summary = all_words if every else ("Not written" if not written
                                                     else f"{len(written)} of {len(wf)} written")
    return [{"id": "prompt", "label": "Prompt", "state": prompt_state, "summary": prompt_summary},
            {"id": "run", "label": "Validation run", "state": run_state, "summary": run_summary},
            {"id": "write", "label": "Write", "state": write_state, "summary": write_summary},
            sub_entry]


def lede_of(t: dict, step: str, run: dict | None, current: str | None, submit: dict | None) -> str:
    stage = (t.get("summary") or {}).get("stage") or ""
    if step == "prompt":
        txt = (((t.get("sections") or {}).get("opening_prompt") or {}).get("text") or "").strip()
        return "Write the opening prompt." if not txt else "Start the validation run."
    if step == "run":
        return run_lede(run, stage)
    if step == "write":
        if _sent_back(t):
            return "The reviewer sent this back."
        if (t.get("ratings") or {}).get("one_shot") == "Yes":
            return "Claude did it in one go. Drop the task."   # Mercor's rule; the Ratings tab has the button
        if current == "ratings":
            if _ratings_rejected(t):
                scale = "Personal" if "personal" in str(_slot(t).get("codebase_type") or "").lower() else "Enterprise"
                return f"These ratings don't pass the {scale} rule."
            return "Rate the run."
        if current:
            return f"Write the {FIELD_LOWER[current]}."
        red = [k for k, s in (t.get("sections") or {}).items() if isinstance(s, dict)
               and any(o.get("level") == "red" and not o.get("accepted") for o in s.get("overlap") or [])]
        if red:
            # Save to Studio is blocked until then; the flag under that field says which words.
            name = FIELD_LOWER.get(red[0], "a field")
            return f"Every field is written. Reword the line in your {name} that repeats AI text, then save."
        return "Every field is written. Save to Studio when you're ready."
    if step == "submit":
        nb = len((submit or {}).get("blockers") or [])
        nc = len((submit or {}).get("calls") or [])
        if nb:
            return f"{_cap(_number_word(nb))} {'thing' if nb == 1 else 'things'} to fix before you submit."
        if (submit or {}).get("can_submit"):
            if nc:
                return (f"Ready to submit. {_cap(_number_word(nc))} "
                        f"{'thing is' if nc == 1 else 'things are'} worth a look first.")
            return "Ready to submit."
        lead = ("Save to Studio, then submit." if not (submit or {}).get("saved")
                else (submit or {}).get("submit_why") or "Save to Studio, then submit.")
        if nc:
            return f"{lead} {_cap(_number_word(nc))} {'thing is' if nc == 1 else 'things are'} worth a look."
        return lead
    since = (t.get("stage_info") or {}).get("since") or (t.get("studio") or {}).get("polled_at")
    if t.get("discard") and stage not in ("in_review", "accepted", "rejected"):
        return "This task was dropped."
    return {"in_review": f"In review since {_day(since)}." if since else "In review.",
            "accepted": "Accepted.", "rejected": "Rejected.", "dropped": "This task was dropped.",
            "discarded": "This task was dropped.", "unclaimed": "This task was unclaimed."}.get(stage, "Submitted.")


def _number_word(n: int) -> str:
    return ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"][n] if 0 <= n <= 10 else str(n)


# --- origin ----------------------------------------------------------------------------------------------

def _load_brief(did: str | None, demo: bool) -> dict | None:
    if not did:
        return None
    try:
        from .api_discover import load_brief
        return load_brief(did, demo=demo)
    except Exception:  # noqa: BLE001  (another builder's module; the page falls back to the dossier)
        pass
    try:
        p = config.DATA / "dossiers" / f"{did}.brief.json"
        doc = json.loads(p.read_text())
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def _strings(items, n: int = 4, min_words: int = 4) -> list[str]:
    out = []
    for x in items or []:
        s = x.get("fact") if isinstance(x, dict) else x
        w = words_only(s, min_words, strict=True)   # first-layer facts: a sentence that lost a code name goes
        if w:
            out.append(w)
        if len(out) >= n:
            break
    return out


def origin_block(t: dict, meta: dict | None, *, demo: bool) -> dict:
    meta = meta or {}
    stored = meta.get("origin") if isinstance(meta.get("origin"), dict) else {}
    dossier = meta.get("dossier") if isinstance(meta.get("dossier"), dict) else {}
    dossier = dossier or ((t.get("summary") or {}).get("dossier") or {})
    did = stored.get("dossier_id") or dossier.get("id")
    opp = t.get("opportunity") or {}
    if not did and not stored.get("kind") and not t.get("lead") and not opp and not demo:
        # Claimed outside the brief: the seed its prompt and goal match (the same match that names the task).
        try:
            from . import data
            short = (t.get("summary") or {}).get("short") or ""
            m = data.seed_match(config.TASKS / short, meta) if short else None
        except Exception:  # noqa: BLE001
            m = None
        if m:
            did, stored = m["dossier_id"], {**stored, "opportunity_id": m["opportunity_id"]}
    upstream = [{"title": words_only(u.get("title"), 1) or "A related thread", "url": u.get("url"),
                 "state": u.get("state")} for u in ((opp.get("upstream") or {}).get("related") or [])[:4]
                if isinstance(u, dict) and u.get("url")]
    brief = _load_brief(did, demo) if did else None
    if stored.get("kind") == "place" and brief:
        place = next((p for p in brief.get("places") or [] if p.get("id") == stored.get("place_id")), None) or {}
        return {"kind": "place", "dossier_id": did, "id": stored.get("place_id"),
                "title": place.get("name") or stored.get("name"), "tag": "Claude's research",
                "problem": words_only(place.get("today"), 4, strict=True) or None, "today": _strings(place.get("facts"), 4),
                "wrong": [], "upstream": upstream}
    oid = stored.get("opportunity_id") or dossier.get("opportunity_id") or opp.get("id")
    if did and oid:
        seed = next((s for s in (brief or {}).get("seeds") or [] if s.get("opportunity_id") == oid), None)
        if seed:
            det = seed.get("detail") or {}
            return {"kind": "seed", "dossier_id": did, "id": oid, "title": seed.get("title") or opp.get("title"),
                    "tag": "Claude's research", "problem": words_only(seed.get("problem"), 4, strict=True) or None,
                    "today": _strings(det.get("today"), 4), "wrong": _strings(det.get("models_wrong") or
                                                                               [seed.get("models_miss")], 4),
                    "upstream": upstream}
        if opp:
            return {"kind": "seed", "dossier_id": did, "id": oid, "title": opp.get("title"), "tag": "Claude's research",
                    "problem": words_only(opp.get("ambition"), 4, strict=True) or words_only(opp.get("why_not_done"), 4, strict=True) or None,
                    "today": _strings(opp.get("evidence"), 3), "wrong": _strings(opp.get("where_models_fail"), 3),
                    "upstream": upstream}
    lead = t.get("lead") or {}
    if lead:
        return {"kind": "lead", "dossier_id": None, "id": lead.get("id"), "title": lead.get("title"),
                "tag": "Claude's research", "problem": words_only(lead.get("mechanism"), 4, strict=True) or None,
                "today": _strings(lead.get("facts"), 4), "wrong": [], "upstream": upstream}
    return {"kind": "none", "dossier_id": None, "id": None, "title": None, "tag": None, "problem": None, "today": [],
            "wrong": [], "upstream": upstream}


# --- the whole payload -----------------------------------------------------------------------------------

def build(t: dict, *, meta: dict | None = None, grading_rows: list[dict] | None = None,
          reviews_list: list[dict] | None = None, check: dict | None = None, plain: dict | None = None,
          plain_pending: bool = False, demo: bool = False, feedback_plan: dict | None = None,
          feedback_job: dict | None = None) -> dict:
    """The Workbench for one task detail (data.real_task or a demo task). Pure: everything comes in."""
    s = t.get("summary") or {}
    stage = s.get("stage") or ""
    step = step_of(t)
    fb = _sent_back(t)
    fb_fields = feedback_fields(t) if fb else []
    current = None
    if step == "write":
        current = next((k for k in fb_fields if not (((t.get("sections") or {}).get(k) or {}).get("locked"))), None) \
            if fb else _first_open(t)
    origin = origin_block(t, meta, demo=demo)
    run = run_block(t)
    sub = _submission_run(t)
    sub_name = (sub or {}).get("name")
    rows_raw = [r for r in grading_rows or [] if isinstance(r, dict) and r.get("id")]
    rows = finding_rows(rows_raw, plain or {}, plain_pending, reviews_list or [], sub_name)
    turns = ((sub or {}).get("facts") or {}).get("turns")
    cov = coverage(reviews_list or [], sub_name, turns) if sub and sub.get("has_transcript", True) else \
        coverage([], None, None)
    if step == "review":   # submitted or gone: no review is his to start
        cov = {**cov, "flag": None, "action": None, "missing_turns": []}
    show_submit = step in ("write", "submit", "review")
    submit = submit_block(t, rows=rows_raw, check=check, run=sub_name) if show_submit else None
    fields = fields_block(t, step, current, fb_fields)
    st = t.get("studio") or {}
    if stage == "accepted":
        fstate = "accepted"
    elif stage == "rejected":
        fstate = "rejected"
    elif fb:
        fstate = "sent_back"
    else:
        fstate = "none"
    return {
        "short": s.get("short"),
        "title": title_of(t, origin),
        "slot": {k: _slot(t).get(k) for k in ("action", "domain", "codebase_type", "prompt_style", "interaction_mode")},
        "studio": studio_block(t),
        "origin": origin,
        "step": step,
        "stage": stage,
        "lede": lede_of(t, step, run, current, submit),
        # The lede names this field's job: the page may name the open tab's job instead (the same template).
        "lede_field": current if step == "write" and current and not fb
        and (t.get("ratings") or {}).get("one_shot") != "Yes" else None,
        "one_shot": (t.get("ratings") or {}).get("one_shot") == "Yes",
        "path": path_block(t, step, run, fields),
        "current_field": current,
        "write_fields": write_fields(t),
        "fields": fields,
        "run": run,
        "findings": {"coverage": cov, "rows": rows, "count": len(rows),
                     "has_review": bool(reviews_list),
                     "plain_state": "pending" if plain_pending else "ready",
                     # rows shown in the review's own words with code hidden, while plain words are missing
                     "fallback": sum(1 for r in rows if r["plain_state"] != "ready")},
        "submit": submit,
        "feedback": {"state": fstate, "words": (st.get("feedback") or "").strip() or None, "fields": fb_fields,
                     "plan": feedback_plan if fb else None, "job": feedback_job if fb else None},
        "calibrate": bool(meta and (meta.get("settings_calibrate_first"))),
        "dropped": stage in ("dropped", "discarded") or bool(t.get("discard")),
    }


# --- real and demo inputs --------------------------------------------------------------------------------

def _demo(req: Request) -> bool:
    return req.cookies.get("johto_mode") == "demo"


def _task_dir(short: str) -> Path:
    if not SHORT.match(short or ""):
        raise ApiError(400, "invalid", "Bad task name.")
    d = config.TASKS / short
    if not (d / "task.json").exists():
        raise ApiError(404, "not_found", f"No task folder for {short}.")
    return d


def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001  (one missing source never blanks the page)
        return default


def real_inputs(d: Path) -> dict:
    from . import data
    from .. import workspace
    t = data.real_task(d)
    meta = _safe(lambda: workspace.load(d), {})
    try:
        from .. import settings
        meta = {**meta, "settings_calibrate_first": bool(settings.get("calibrate_first"))}
    except Exception:  # noqa: BLE001
        pass
    rows = _safe(lambda: importlib.import_module("johto.grading").load(d).get("rows") or [], [])
    revs = _safe(lambda: importlib.import_module("johto.reviews").list_reviews(d), [])
    check = _safe(lambda: importlib.import_module("johto.draftcheck").latest(d), None)
    pm = _plain_mod()
    plain = _safe(lambda: pm.load(d), {}) if pm else {}
    pending = False
    if pm and rows:
        job = _safe(lambda: pm.ensure(d), None)
        pending = bool(job) and (job.get("state") in ("queued", "running", None))
    fplan, fjob = None, None
    if (meta.get("studio") or {}).get("feedback"):
        try:
            from .. import feedback as fbm
            from . import api_feedback
            job = _safe(lambda: api_feedback.ensure(d, meta), None)
            fplan = fbm.view(d, meta)
            if job:
                fjob = {"id": job.get("id"), "kind": job.get("kind"), "state": job.get("state")}
        except Exception:  # noqa: BLE001
            fplan = None
    return {"t": t, "meta": meta, "grading_rows": rows, "reviews_list": revs, "check": check, "plain": plain,
            "plain_pending": pending, "feedback_plan": fplan, "feedback_job": fjob}


# Demo findings for the rate-stage demo task: plain sentences only, shown with the AI mark like real ones.
DEMO_FINDINGS = {
    "d3m0aab1": [
        ("R1", "missed", "isolated", "high", "A failed tag save shows a success message, so the user thinks the tag stuck.",
         {"goal": True, "dp": 1}),
        ("R2", "missed", "traced", None, "Two quick edits to the same photo can save out of order, and the older tags win.",
         {"dp": 2}),
        ("R3", "partial", "executed", None, "Claude retried failed saves but never told the user when a retry gave up.",
         {}),
        ("R4", "missed", "traced", "high", "Offline saves are queued but dropped when the page reloads.", {}),
        ("R5", "credit", "executed", None, "Claude found that the save button stays disabled after a network error.", {}),
        ("R6", "credit", "isolated", None, "Claude kept the existing tag list order when merging the server reply.", {}),
        ("R7", "unknown", "claimed", None, "Claude said the gallery reloads tags after login. Nobody checked it.", {}),
    ],
}


def demo_inputs(short: str, lane_state: str | None = None) -> dict:
    from . import app as webapp
    t = webapp._demo["tasks"].get(short)
    if t is None:
        raise ApiError(404, "not_found", f"No demo task {short}.")
    if lane_state == "waiting":
        # The demo lane's question, as Now shows it (demo.lane), for the task whose run it is.
        from . import demo as demo_mod
        ln = demo_mod.lane(webapp._demo["tasks"], "waiting")
        if (ln.get("run") or {}).get("short") == short:
            t = {**t, "lane_need": {"kind": "lane_waiting", "question": ln.get("question"),
                                    "run": (ln.get("run") or {}).get("run")}}
    rows, revs = [], []
    for rid, res, basis, sev, plain, marks in DEMO_FINDINGS.get(short, []):
        rows.append({"id": rid, "finding": plain, "result": {"run": "validate-1", "turn": 1, "value": res},
                     "basis": basis, "severity": sev, "scope": "explicit" if marks.get("goal") else None,
                     "marks": {"selected": bool(marks), "goal": bool(marks.get("goal")), "dp": marks.get("dp"),
                               "notes": False}, "sources": [{"type": "review", "ref": "reviews/demo.md"}],
                     "refs": [], "limits": None, "review_status": "new"})
    if rows:
        revs = [{"run": "validate-1", "turn": 1, "source": "gptpro", "name": "demo.md"}]
    return {"t": t, "meta": {}, "grading_rows": rows, "reviews_list": revs, "check": None,
            "plain": {r["id"]: r["finding"] for r in rows}, "plain_pending": False}


# --- routes ----------------------------------------------------------------------------------------------

@handled
async def workbench(req: Request):
    short = req.path_params["short"]
    if _demo(req):
        from . import app as webapp
        inputs = demo_inputs(short, lane_state=webapp.demo_lane(req))
        return JSONResponse(build(inputs.pop("t"), demo=True, **inputs))
    d = _task_dir(short)
    inputs = await asyncio.to_thread(real_inputs, d)
    return JSONResponse(json.loads(json.dumps(build(inputs.pop("t"), **inputs), default=str)))


MAX_REVIEW_CHARS = 400_000


def _default_run(d: Path) -> tuple[str | None, dict]:
    from . import data
    t = data.real_task(d)
    sub = _submission_run(t)
    return (sub or {}).get("name"), t


@handled
async def review_post(req: Request):
    """Save the reviewer's reply Austin pasted (his press). The review becomes a grading source and a ledger
    source for the overlap guard, as a review saved from the CLI does."""
    d = _task_dir(req.path_params["short"])
    b = await _body(req)
    text = b.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ApiError(422, "empty", "Paste the reply first.")
    if len(text) > MAX_REVIEW_CHARS:
        raise ApiError(422, "too_long", "That reply is too long to save.")
    source = b.get("source") or "gptpro"
    if source not in ("gptpro", "outside", "manual"):
        raise ApiError(400, "invalid", "Unknown review source.")
    run = b.get("run")
    if run is not None and (not isinstance(run, str) or not RUN.match(run)):
        raise ApiError(400, "invalid", "Bad run name.")
    turn = b.get("turn")
    if turn is not None and (not isinstance(turn, int) or isinstance(turn, bool) or turn < 0 or turn > 999):
        raise ApiError(400, "invalid", "Bad turn.")

    def save():
        rv = importlib.import_module("johto.reviews")
        name, t = _default_run(d)
        r = run or name
        if not r or not (d / "runs" / r).is_dir():
            raise ApiError(404, "not_found", "There is no run to attach the review to.")
        n = turn
        if n is None:
            sub = next((x for x in t.get("runs") or [] if x.get("name") == r), {})
            n = (sub.get("facts") or {}).get("turns")
        p = rv.save(d, r, n, text, source=source)
        pm = _plain_mod()
        if pm is not None and callable(getattr(pm, "ensure", None)):
            _safe(lambda: pm.ensure(d), None)
        return p, r, n
    path, r, n = await asyncio.to_thread(save)
    try:
        from .api_actions import task_changed
        task_changed(d.name, ["grading", "reviews"])
    except Exception:  # noqa: BLE001
        pass
    words = f"Saved the review of {run_words(r)}" + (f", turn {n}" if n else "") + "."
    return JSONResponse({"toast": {"id": _oid("review", path), "level": "ok", "text": words, "undo": None,
                                   "link": None}, "review": {"name": Path(path).name, "turn": n}})


def _copy(text: str) -> bool:
    """Put the pack on the clipboard. A fake or test server leaves the clipboard alone."""
    if config.FAKE:
        return False
    subprocess.run(["pbcopy"], input=text, text=True, timeout=30)
    return True


def _review_job(short: str, d: Path, run: str, missing: list[int]) -> dict:
    from . import jobs
    live = jobs.active(kind="review_pack", target=f"t:{short}")
    if live:
        return live[0]

    def work(ctx):
        delta = importlib.import_module("johto.delta")
        ctx.step("packing")
        p = delta.next_pack(d, run, log=ctx.log, ai=False)
        ctx.step("copying")
        copied = _copy(Path(p).read_text())
        if not copied:
            ctx.log("Fake mode: the clipboard was left alone.")
        return {"text": "Copied. Paste it into GPT Pro.", "turns": missing, "pack": Path(p).name}
    return jobs.submit("review_pack", f"t:{short}", work, pool="local", params={"run": run, "turns": missing},
                       steps=["packing", "copying"], step="waiting")


@handled
async def review_latest(req: Request):
    """Pack the newest unreviewed turns of the submission run (the existing pack code) and copy the pack. The app
    never sends anything to ChatGPT; Austin pastes it there himself."""
    d = _task_dir(req.path_params["short"])

    def start():
        name, t = _default_run(d)
        if not name:
            raise ApiError(409, "no_run", "There is no run to review yet.")
        revs = _safe(lambda: importlib.import_module("johto.reviews").list_reviews(d), [])
        sub = next((x for x in t.get("runs") or [] if x.get("name") == name), {})
        cov = coverage(revs, name, (sub.get("facts") or {}).get("turns"))
        return _review_job(d.name, d, name, cov.get("missing_turns") or [])
    job = await asyncio.to_thread(start)
    return JSONResponse({"job": job}, status_code=202)


# The lane refuses a validation run until Studio holds this prompt and Mercor's AutoQC passed it (actions.lane_gate).
# The CLI's way out is `lane --qc`; this is the same step behind a button. Its reasons, in words for the page.
PROMPT_CHECK_WORDS = {
    "differs": "Studio needs this prompt, checked by Mercor, before the run.",
    "stale": "Mercor's automatic check hasn't seen this prompt yet.",
    "not_started": "Studio didn't start Mercor's check.",
    "unfinished": "Mercor's check didn't finish.",
    "running": "Mercor's check is running on this prompt.",
    "failed": "Mercor's check failed the prompt.",
}


def prompt_check_view(err: dict) -> dict:
    """A lane-gate refusal (prompt_qc, sync_conflict, same_prompt) as words and the one way out, never a command."""
    code = (err or {}).get("code")
    det = (err or {}).get("details") or {}
    if code == "sync_conflict":
        return {"reason": "conflict", "tone": "wait", "title": "The prompt differs here and in Studio.",
                "why": "It isn't clear which is newer. Make both the same, then start again.", "action": "open_studio",
                "failed": []}
    if code == "same_prompt":
        return {"reason": "same_prompt", "tone": "wait", "title": "An earlier validation run used this prompt.",
                "why": "Change the prompt for a new run, or continue the earlier one in its lane.", "action": None,
                "failed": []}
    reason = det.get("reason") or "differs"
    failed = [{"dimension": x.get("dimension"), "justification": x.get("justification")}
              for x in det.get("failed") or [] if isinstance(x, dict)]
    if reason == "failed":
        return {"reason": "failed", "tone": "stop", "title": PROMPT_CHECK_WORDS["failed"],
                "why": "Change the prompt, then check it again.", "action": "check", "failed": failed}
    return {"reason": reason, "tone": "wait", "title": PROMPT_CHECK_WORDS.get(reason, PROMPT_CHECK_WORDS["differs"]),
            "why": "This saves only the opening prompt to Studio and runs Mercor's check, a few minutes. The run "
                   "starts if it passes.", "action": "check", "failed": []}


@handled
async def prompt_check(req: Request):
    """Save the opening prompt to Studio (only the prompt, and only when Studio's copy is empty or older) and run
    Mercor's AutoQC on it: Austin's press of "Save to Studio, check, then start". The lane start follows in the
    page when this succeeds. A refusal comes back as the job's error with prompt_check_view's words in details."""
    d = _task_dir(req.path_params["short"])
    from . import jobs
    live = jobs.active(kind="prompt_check", target=f"t:{d.name}")
    if live:
        return JSONResponse({"job": live[0]}, status_code=202)

    def work(ctx):
        from .. import actions
        ctx.step("checking")
        try:
            res = actions.lane_gate(d, "validate", qc=True, log=ctx.log)
        except actions.ActionError as e:
            view = prompt_check_view({"code": e.code, "details": e.details})
            raise actions.ActionError(e.code, view["title"], {**(e.details or {}), "view": view}) from None
        from .api_actions import task_changed
        task_changed(d.name, ["studio", "qc"])
        return {"checked": res.get("checked"), "text": "The prompt passed Mercor's check."}
    job = jobs.submit("prompt_check", f"t:{d.name}", work, pool="studio", priority=0, cancellable=False,
                      steps=["checking"], step="checking")
    return JSONResponse({"job": job}, status_code=202)


@handled
async def reveal_log(req: Request):
    """Show the run's transcript file in Finder, for the Trajectory Logs upload Studio takes only by hand."""
    d = _task_dir(req.path_params["short"])

    def find():
        from . import data
        name, _ = _default_run(d)
        if not name:
            raise ApiError(409, "no_run", "There is no run yet.")
        tpath, _ws = data.run_transcript_path(d, name)
        if not tpath:
            raise ApiError(404, "not_found", "The run's conversation log isn't collected yet.")
        if not config.FAKE:
            subprocess.Popen(["open", "-R", str(tpath)])
        return Path(tpath).name
    await asyncio.to_thread(find)
    return JSONResponse({"toast": {"id": _oid("reveal", d.name), "level": "ok",
                                   "text": "Shown in Finder. Drag it into Trajectory Logs in Studio.", "undo": None,
                                   "link": None}})


routes = [
    Route("/api/tasks/{short}/workbench", workbench),
    Route("/api/tasks/{short}/reviews", review_post, methods=["POST"]),
    Route("/api/tasks/{short}/review-latest", review_latest, methods=["POST"]),
    Route("/api/tasks/{short}/reveal-log", reveal_log, methods=["POST"]),
    Route("/api/tasks/{short}/prompt-check", prompt_check, methods=["POST"]),
]
