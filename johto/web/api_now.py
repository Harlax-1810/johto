"""Now API (spec.md 3.1, 3.6, 7.4 and 7.6).

GET  /api/now                       the Now screen: lede, focus, after (up to 3), slot_picks (the top three open
                                    slots to take, the slot groups with no great option and the research
                                    aimed at them), idle_pick, lessons not yet seen.
                                    ?focus=<item id> puts that queue item in focus (a toast link, j/k, Open).
GET  /api/now/lessons               every reviewer lesson, seen or not (the Submitted screen).
POST /api/now/lessons/{short}/seen  he dismissed a "What reviewers said" card: a local note in task.json
                                    (studio.feedback_seen_at). Nothing is sent to Studio.

Owned by the `home` builder. Mounted by app.py through EXTENSIONS (session, Origin and demo guards come for free).
The queue itself is not touched: tiers and order come from johto.web.domain.queue through data.real_snapshot, and
this module only turns each item into words. Every string meant for the first layer follows rule 5 (spec 1.5): a
reason becomes one plain fact (hide_code), run ids become "validation run" or "validation run 2", model ids and commands never leave here.
Reviewer lessons are the reviewer's own words picked by a fixed rule (johto.rules.extract_lessons, then 7.6), no
model. Claude's message is shown as Claude wrote it, with code, paths and commands taken out.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .. import config
from . import data

SHORT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# The lane needs him (queue kinds from domain/queue.py's LANE_ROWS and TURN_ROWS).
LANE_KINDS = {"lane_waiting", "lane_permission", "lane_login", "lane_paste", "lane_problem", "lane_answer",
              "lane_async_done"}
# Claude finished a turn: the card shows what Claude said.
TALK_KINDS = {"lane_waiting", "lane_problem", "lane_answer", "lane_async_done"}
BLOCKER_KINDS = {"setup_failed", "wrong_model", "collect_failed", "blocker_studio", "blocker_docker", "blocker_disk",
                 "blocker_gh"}
WRITING_KINDS = {"write_prompt", "write_details", "rate", "feedback", "fix_qc", "save", "submit", "rejected"}
START_KINDS = {"start_validation", "lane_start"}
NOW_ONLY = {"decide", "log_discard"}          # their controls live on the Now card

FIELD_WORDS = {"opening_prompt": "Opening prompt", "goal": "Goal", "decision_points": "Decision points",
               "codebase": "Codebase understanding", "initial_state": "Initial state",
               "validation_notes": "Validation notes"}
FIELD_LOWER = {"goal": "goal", "decision_points": "decision points", "codebase": "codebase understanding",
               "initial_state": "initial state", "validation_notes": "validation notes"}

# Stage -> the task path's current step (spec 3.5): Prompt, Validation run, Write, Submit.
STEP_OF = {
    "claiming": "prompt", "prompt": "prompt",
    "waiting_calib": "run", "calibrating": "run", "decide": "run", "waiting_validate": "run", "validating": "run",
    "collecting": "run", "collect_failed": "run", "details": "prompt",
    "rate": "write", "sent_back": "write",
    "ready_to_save": "submit", "saving": "submit", "qc_running": "submit", "fix_qc": "submit",
    "ready_to_submit": "submit", "submitting": "submit", "studio": "submit",
    "in_review": "review", "accepted": "review", "rejected": "review",
}
STEPS = (("prompt", "Prompt"), ("run", "Validation run"), ("write", "Write"), ("submit", "Submit"))


# --- plain words ------------------------------------------------------------------------------------------

# Case matters for identifiers (camelCase, snake_case, hex), so only the words at the end ignore it. A clock time
# ("13:24") is not a path:line.
_CODEISH = re.compile(
    r"`|```|\b[\w.-]+/[\w./-]+|\b\w+\.(?:py|js|ts|tsx|jsx|go|rb|php|java|kt|rs|json|jsonl|md|yml|yaml|toml|sh|sql|xml|"
    r"html|css|vue|c|h|cpp)\b|(?<=[A-Za-z_)\]]):\d+\b|#\d+|\b[0-9a-f]{7,40}\b|\b[a-z]+_[a-z0-9_]+\b|\b[a-z]+[A-Z]\w*\b|"
    r"\w+\(\)|::|(?i:\bjohto\s|\bexit\s+\d|\b(?:validate|calib)-\d+\b|\bopus\b|\bsonnet\b|\bhaiku\b|\beffort\b)")


def _local_hide_code(text: str) -> str:
    """spec 7.5, used until johto.plain lands: drop backticked spans, paths, path:line, #123, hashes and code
    identifiers, then tidy the punctuation left behind. "" when fewer than five words survive."""
    s = str(text or "")
    s = re.sub(r"```.*?```", " ", s, flags=re.S)
    s = re.sub(r"`[^`]*`", " ", s)
    s = re.sub(r"\b[\w.-]*/[\w./-]+(?::\d+(?:-\d+)?)?", " ", s)
    s = re.sub(r"\b\w+\.(?:py|js|ts|tsx|jsx|go|rb|php|java|kt|rs|json|jsonl|md|yml|yaml|toml|sh|sql|xml|html|css|vue)"
               r"(?::\d+(?:-\d+)?)?\b", " ", s)
    s = re.sub(r"#\d+\b", " ", s)
    s = re.sub(r"\b[0-9a-f]{7,40}\b", " ", s)
    s = re.sub(r"\b\w+\(\)", " ", s)
    s = re.sub(r"\b[a-z]+_[a-z0-9_]+\b", " ", s)
    s = re.sub(r"\b[a-z]+[A-Z]\w*\b", " ", s)
    return _tidy(s, min_words=5)


def _tidy(s: str, min_words: int = 0) -> str:
    s = re.sub(r"\(\s*\)|\[\s*\]", " ", s)
    s = re.sub(r"\s+([,.;:!?)])", r"\1", s)
    s = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", s)
    s = re.sub(r"\b(in|at|of|to|from|on|by|and|or|the|a|an|with)\s*([,.;:])", r"\2", s, flags=re.I)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"^[\s,.;:]+", "", s).strip()
    if min_words and len(re.findall(r"[A-Za-z]{2,}", s)) < min_words:
        return ""
    return s


def hide_code(text: str) -> str:
    """johto.plain.hide_code (the brief builder's), else the local twin above."""
    try:
        from .. import plain
        fn = getattr(plain, "hide_code", None)
        if callable(fn):
            return fn(text)
    except Exception:
        pass
    return _local_hide_code(text)


_META_FACT = re.compile(r"^(?:Claimed\b|Kept after\b|Your opening prompt is saved\b|The goal can wait\b|"
                        r"Added\b.*\bago\.?$|Queued\b.*\bago\.?$)", re.I)


def plain_fact(text: str | None) -> str:
    """One plain sentence from a queue reason: code tokens hidden, ";" split into sentences, ends with a full stop.
    A reason with nothing code-like is kept as written (hide_code wants five words; "The lane is free" has four)."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if not s:
        return ""
    s = s.replace("—", ",").replace("–", ",")
    # Run ids and numbers as words: "validate-2" and "Validation 2" read "validation run 2"; the first is "the run".
    s = re.sub(r"\b(validate|calib)-(\d+)\b", lambda m: ("validation" if m.group(1) == "validate" else "calibration")
               + f" {m.group(2)}", s)
    s = re.sub(r"\b([Vv]alidation|[Cc]alibration) (\d+)\b",
               lambda m: f"{m.group(1)} run" + ("" if m.group(2) == "1" else f" {m.group(2)}"), s)
    s = re.sub(r"\bexited(?: with)?(?: code| status)?\s*\d+", "stopped", s, flags=re.I)
    s = re.sub(r"\s*\(?\bexit(?: code| status)?\s*\d+\)?", "", s, flags=re.I)
    s = re.sub(r"[\s:,;]+$", "", s)
    parts = []
    for part in (x.strip() for x in re.split(r";\s*|:\s+(?=see\b)", s, flags=re.I)):
        if part and _CODEISH.search(part):
            part = hide_code(part)     # a clause that is mostly code goes; the plain clauses stay
        if part:
            parts.append(part)
    if not parts:
        return ""
    s = ". ".join(p[0].upper() + p[1:] for p in parts)
    return s if s.endswith((".", "?", "!")) else s + "."


def run_words(run: str | None) -> str:
    """"validation run", "validation run 2", "calibration run": no run ids on the page."""
    m = re.fullmatch(r"(calib|validate)-(\d+)", run or "")
    if not m:
        return ""
    word = "calibration run" if m.group(1) == "calib" else "validation run"
    return word if m.group(2) == "1" else f"{word} {m.group(2)}"


def split_label(label: str | None) -> tuple[str, str]:
    """("Shelfnote", "Stale reminder") from "Shelfnote · Stale reminder"; a Studio task name is no problem."""
    s = str(label or "")
    repo, _, problem = s.partition(" · ")
    if re.fullmatch(r"Task [a-z0-9]{6,}", problem, re.I):
        problem = ""
    if re.fullmatch(r"Task [a-z0-9]{6,}", repo, re.I):
        repo = ""
    return repo, problem


def task_words(label: str | None) -> str:
    repo, problem = split_label(label)
    return f"{repo} · {problem}" if repo and problem else repo or problem or "a task"


def _sentence(s: str) -> str:
    s = s.strip()
    return s if s.endswith((".", "?", "!")) else s + "."


# --- Claude's message ---------------------------------------------------------------------------------------

_MODEL = re.compile(r"\b(claude[- ])?(opus|sonnet|haiku)[\w .-]*\d[\w.]*\b", re.I)


def claude_prose(text: str | None, limit: int = 900) -> list[str]:
    """Claude's last message as plain paragraphs: code blocks, commands, paths, path:line, hashes and model ids out.
    Inline code that names a thing (a function, a setting) stays as a word. Paragraphs that lose their sense are
    dropped. The last paragraphs are kept, up to `limit` characters."""
    s = str(text or "")
    s = re.sub(r"```.*?(```|$)", "\n\n", s, flags=re.S)
    out: list[str] = []
    for para in re.split(r"\n\s*\n", s):
        lines = []
        for ln in para.splitlines():
            t = ln.strip()
            if not t or re.match(r"^(\$ |> |\|)", t) or re.fullmatch(r"[-=*_|:\s]+", t):
                continue
            t = re.sub(r"^#{1,6}\s*", "", t)
            t = re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", t)
            lines.append(t)
        p = " ".join(lines)
        if not p:
            continue
        p = re.sub(r"\*\*|__", "", p)

        def inline(m):
            v = m.group(1).strip()
            if re.search(r"[/\\]|\.\w{1,5}\b|:\d|\s-{1,2}\w|^\w+\s+\w+\s", v) or re.fullmatch(r"[0-9a-f]{7,40}", v):
                return " "
            return v
        p = re.sub(r"`([^`]*)`", inline, p)
        p = re.sub(r"\b[\w.-]*/[\w./-]+(?::\d+(?:-\d+)?)?", " ", p)
        p = re.sub(r"\b[\w-]+\.(?:py|js|ts|tsx|jsx|go|rb|php|java|kt|rs|json|jsonl|md|yml|yaml|toml|sh|sql|xml|html|"
                   r"css|vue)(?::\d+(?:-\d+)?)?\b", " ", p)
        p = re.sub(r"\b[0-9a-f]{7,40}\b", " ", p)
        p = re.sub(r"#\d+\b", " ", p)
        p = _MODEL.sub(" ", p)
        p = re.sub(r"\(?\b(?:exit(?:ed)?(?: with)?(?: code| status)?)\s*\d+\)?", " ", p, flags=re.I)
        p = _tidy(p)
        if len(re.findall(r"[A-Za-z]{2,}", p)) >= 3:
            out.append(p)
    kept, total = [], 0
    for p in reversed(out):
        if kept and total + len(p) > limit:
            break
        kept.insert(0, p)
        total += len(p)
    return kept


def split_question(paras: list[str], question: str | None) -> tuple[list[str], str | None]:
    """The paragraph that asks him something, apart from the rest (it sits in its own box on the card)."""
    if not paras:
        q = claude_prose(question, 600) if question else []
        return [], (q[-1] if q else None)
    last = paras[-1]
    if "?" in last[-300:]:
        return paras[:-1], last
    if question:
        q = claude_prose(question, 600)
        if q and q[-1] not in paras:
            return paras, q[-1]
    return paras, None


# --- the task path (Prompt, Validation run, Write, Submit) -----------------------------------------------

def path_for(ctx: dict) -> list[dict]:
    stage = ctx.get("stage") or ""
    cur = STEP_OF.get(stage, "prompt")
    texts = ctx.get("texts") or {}
    used_run = any(r.get("kind") == "validate" and (r.get("facts") or r.get("collected_at")) for r in ctx.get("runs") or [])
    if stage == "details":
        cur = "write" if used_run else "run" if (texts.get("opening_prompt") or "").strip() else "prompt"
    order = [s for s, _ in STEPS]
    at = order.index(cur) if cur in order else len(order)
    words = len(re.findall(r"\S+", texts.get("opening_prompt") or ""))
    run = next((r for r in reversed(ctx.get("runs") or []) if r.get("kind") == "validate"), None)
    f = (run or {}).get("facts") or {}
    summary = {
        "prompt": f"{words} words" if words else "",
        "run": (f"{_mins(f.get('active_min'))} min · {f.get('turns')} turn{'s' if f.get('turns') != 1 else ''}"
                if f.get("turns") else ""),
        "write": _write_summary(texts, ctx.get("slot") or {}),
        "submit": "",
    }
    out = []
    for i, (sid, label) in enumerate(STEPS):
        state = "done" if i < at else "current" if i == at else "later"
        if sid == "write" and state == "later":
            summary["write"] = "Goal written" if (texts.get("goal") or "").strip() else ""
        out.append({"id": sid, "label": label, "state": state, "summary": summary[sid] if state != "later" or sid == "write" else ""})
    return out


def _write_fields(slot: dict) -> list[str]:
    keys = ["goal", "decision_points", "codebase"]
    if (slot or {}).get("action") == "Operate":
        keys.append("initial_state")
    return keys + ["validation_notes"]


def _write_summary(texts: dict, slot: dict) -> str:
    keys = _write_fields(slot)
    done = [k for k in keys if (texts.get(k) or "").strip()]
    if len(done) == len(keys):
        return "All fields written"
    if not done:
        return ""
    nxt = next(k for k in keys if k not in done)
    return f"{len(done)} of {len(keys)} written, next {FIELD_LOWER[nxt]}"


def _mins(m) -> str:
    try:
        m = float(m or 0)
    except (TypeError, ValueError):
        return "0"
    return "under 1" if 0 < m < 0.5 else str(round(m))


# --- one queue item as a Now item -----------------------------------------------------------------------

def eff_kind(it: dict) -> str:
    """The kind the card is about: a writing row that took the lane's title is a lane card."""
    need = it.get("lane_need") or {}
    if need.get("kind") and it.get("title") == need.get("title"):
        return need["kind"]
    return it.get("kind") or ""


def _missing(ctx: dict) -> list[str]:
    texts = ctx.get("texts") or {}
    keys = ["goal", "decision_points", "codebase"] + (["initial_state"] if (ctx.get("slot") or {}).get("action") == "Operate" else [])
    return [k for k in keys if not (texts.get(k) or "").strip()]


def lede_for(it: dict, ctx: dict, lane: dict, settings: dict) -> str:
    k = eff_kind(it)
    repo = split_label(it.get("label") or ctx.get("label"))[0] or "this task"
    if k == "lane_waiting":
        return f"Claude asked you something on {repo}."
    if k == "lane_permission":
        return f"Claude is asking permission on {repo}."
    if k == "lane_login":
        return "The lane needs you to log in to Claude."
    if k == "lane_paste":
        if it.get("title") == "Send your prompt" or (it.get("lane_need") or {}).get("title") == "Send your prompt":
            return f"Send your prompt in the lane for {repo}."
        return f"Paste your prompt into the lane for {repo}."
    if k == "lane_problem":
        return f"Claude stopped on a problem on {repo}."
    if k == "lane_answer":
        return f"Claude answered on {repo}."
    if k == "lane_async_done":
        return f"Claude finished its turn on {repo}."
    if k == "write_prompt":
        return f"Write the opening prompt for {repo}."
    if k == "write_details":
        miss = _missing(ctx)
        if not miss or (it.get("title") or "").endswith("goal"):
            miss = miss or ["goal"]
        return f"Write the {FIELD_LOWER[miss[0]]} for {repo}."
    if k == "rate":
        # Rated already: what is left is his "Done writing" press on the task page.
        return f"Finish writing {repo}." if ctx.get("rated") and not _missing(ctx) else f"Rate {repo}'s validation run."
    if k == "feedback":
        return f"The reviewer sent {repo} back."
    if k == "rejected":
        return f"The reviewer rejected {repo}."
    if k == "fix_qc":
        m = re.search(r"\d+", it.get("title") or "")
        n = int(m.group(0)) if m else 1
        return f"Fix {n} automatic check{'s' if n != 1 else ''} on {repo}."
    if k == "save":
        return f"Save {repo} to Studio."
    if k == "submit":
        return f"Submit {repo} for review."
    if k in START_KINDS:
        calib = ((it.get("primary") or {}).get("args") or {}).get("kind") == "calib" and settings.get("calibrate_first")
        return f"Start {repo}'s {'calibration' if calib else 'validation'} run."
    if k == "decide":
        return f"Keep or drop {repo}'s calibration run."
    if k == "log_discard":
        return f"Log the discarded run for {repo}."
    if k == "setup_failed":
        return f"Lane setup failed for {repo}."
    if k == "wrong_model":
        return f"Claude ran on the wrong model on {repo}."
    if k == "collect_failed":
        rw = run_words(it.get("run")) or "the run"
        return f"{rw[0].upper() + rw[1:]} on {repo} was not collected."
    if k in BLOCKER_KINDS:
        return _sentence(plain_fact(it.get("title")) or "Something needs fixing")
    return _sentence(plain_fact(it.get("title")) or "Next step")


def verb_for(it: dict, ctx: dict) -> str:
    """The After-this row's verb and object."""
    k = eff_kind(it)
    t = re.sub(r"\s*·\s*(PC|Mac)$", "", it.get("title") or "")
    if k == "write_details":
        miss = _missing(ctx) or ["goal"]
        return f"Write the {FIELD_LOWER[miss[0]]}"
    if k == "fix_qc":
        m = re.search(r"\d+", t)
        n = int(m.group(0)) if m else 1
        return f"Fix {n} automatic check{'s' if n != 1 else ''}"
    if k == "feedback":
        return "Answer the reviewer"
    if k == "rejected":
        return "Read the rejection"
    if k in START_KINDS:
        return "Start the validation run"
    if k == "rate":
        return "Finish writing" if ctx.get("rated") and not _missing(ctx) else "Rate the validation run"
    if k == "wrong_model":
        return "Fix the model in the lane"
    if k == "setup_failed":
        return "Set up the lane again"
    if k == "lane_answer":
        return "Read Claude's answer"
    if k == "lane_async_done":
        return "End the run"
    return plain_fact(t).rstrip(".") or t


def fact_for(it: dict, ctx: dict) -> str:
    k = eff_kind(it)
    if it.get("blocked"):
        return "Waits until Studio is signed in."
    if k == "wrong_model":
        return "Claude ran on the wrong model or effort."
    if k == "setup_failed":
        return "The lane's setup stopped with an error."
    if k == "lane_permission":
        where = "" if it.get("lane") else f" on the {it.get('host') or 'PC'}"
        return f"Claude is waiting{where} for your answer."
    if k == "lane_problem":
        return plain_fact(re.sub(r"^Claude stopped( on the \w+)?:\s*", "", it.get("reason") or "")) or "Claude stopped on an error."
    if k == "blocker_disk":
        return "Lanes and copies of repos need room."
    if k == "lane_login":
        return "Claude in the lane is signed out."
    if k == "fix_qc":
        m = re.search(r"failed (\d+) of (\d+)", it.get("reason") or "")
        if m:   # the title already says how many; the rest is the tool's name and a time
            return ""
    return plain_fact(it.get("reason"))


def place_for(it: dict, ctx: dict) -> dict:
    """Where Open takes him: the lane, the task page at a step, or the Now card itself."""
    k = eff_kind(it)
    short = it.get("short")
    local = bool(it.get("lane")) or (k in LANE_KINDS and (it.get("host") in (None, "Mac")))
    if k in LANE_KINDS or k == "wrong_model":
        return {"kind": "lane" if local else "here"}
    if not short:
        return {"kind": "here"}
    base = f"/task/{short}"
    if k == "write_prompt":
        return {"kind": "task", "href": f"{base}#step=prompt"}
    if k == "write_details":
        miss = _missing(ctx) or ["goal"]
        during = (ctx.get("stage") or "") in ("validating", "calibrating", "collecting", "waiting_validate")
        return {"kind": "task", "href": f"{base}#step={'run' if during else 'write'}&field={miss[0]}"}
    if k == "rate":
        return {"kind": "task", "href": f"{base}#step=write&field=ratings"}
    if k == "feedback":
        return {"kind": "task", "href": f"{base}#step=write&fb=1"}
    if k in ("fix_qc", "save", "submit"):
        return {"kind": "task", "href": f"{base}#step=submit"}
    if k == "rejected":
        return {"kind": "task", "href": base}
    return {"kind": "here"}


def tone_for(it: dict) -> str:
    k = eff_kind(it)
    if k in BLOCKER_KINDS or k == "lane_problem":
        return "stop"
    if it.get("attention") or it.get("tier", 9) <= 1 or k in ("lane_waiting", "lane_permission", "lane_login", "lane_paste", "feedback"):
        return "wait"
    return "ink"


def _facts_of(ctx: dict, run: str | None, lane: dict) -> dict:
    lr = lane.get("run") or {}
    if lr.get("short") == ctx.get("short") and (not run or lr.get("run") == run) and lane.get("facts"):
        return lane["facts"]
    r = next((r for r in ctx.get("runs") or [] if r.get("name") == run), None) if run else None
    if r is None:
        r = next((r for r in reversed(ctx.get("runs") or []) if r.get("facts")), None)
    return (r or {}).get("facts") or {}


def lane_body(it: dict, ctx: dict, lane: dict, now: float, deep: bool) -> dict:
    k = eff_kind(it)
    need = it.get("lane_need") or {}
    run = need.get("run") or it.get("run") or (lane.get("run") or {}).get("run")
    mon = ctx.get("monitor") or {}
    if mon.get("run") and run and mon.get("run") != run:
        mon = {}
    f = _facts_of(ctx, run, lane)
    local = bool(it.get("lane"))
    host = "Mac" if local else (it.get("host") or mon.get("host") or "PC")
    since = need.get("since") or it.get("since")
    t0 = data._domain().derive.ts(since) if since else None
    facts = []
    turn = mon.get("turn") or {}
    if turn.get("working_minutes") is not None and k in TALK_KINDS:
        facts.append(f"This turn {_mins(turn['working_minutes'])} min")
    tests = f.get("tests") or {}
    if tests.get("runs") and k in TALK_KINDS | {"lane_permission"}:
        if tests.get("last_ok") is True:
            facts.append("Ran the tests: all passed")
        elif tests.get("last_ok") is False:
            facts.append("Ran the tests: failing")
    edits = f.get("files_edited")
    if isinstance(edits, list) and k in TALK_KINDS:
        facts.append("No edits yet" if not edits else f"Changed {len(edits)} file{'s' if len(edits) != 1 else ''}")
    claude_min = mon.get("task_minutes")
    if claude_min is None:
        claude_min = f.get("task_claude_min")
    body = {"kind": "lane", "host": host, "local": local, "run_words": run_words(run),
            "context": " · ".join(x for x in (task_words(it.get("label") or ctx.get("label")),
                                              (run_words(run) or "").capitalize()) if x),
            "waiting_s": int(max(0, now - t0)) if t0 and k in ("lane_waiting", "lane_permission", "lane_problem") else None,
            "since": since if k in ("lane_waiting", "lane_permission", "lane_problem") else None,
            "claude_min": round(float(claude_min), 1) if claude_min is not None else None,
            "budget": mon.get("budget") or 90, "facts": facts[:3], "message": [], "question": None,
            "sentence": None, "full": None, "prompt": None}
    if k == "lane_permission":
        body["sentence"] = "Claude wants to run something and is waiting for your yes or no."
    elif k == "lane_login":
        body["sentence"] = "The lane's Claude is signed out. The button starts the login; you finish it in the browser tab it opens."
    elif k == "lane_paste":
        if (it.get("title") or "") == "Send your prompt":
            body["sentence"] = "Your prompt is in Claude's input box. Press Enter in the lane to send it."
        else:
            body["sentence"] = "The lane is ready. The app can paste your saved opening prompt; you press Enter to send it."
        # What will be pasted: his opening prompt and nothing else. The private fields (goal, decision points,
        # codebase understanding, validation notes) never reach the lane or this card.
        body["prompt"] = ((ctx.get("texts") or {}).get("opening_prompt") or "").strip() or None
    if deep and k in TALK_KINDS:
        text = ctx.get("message") if "message" in ctx else None
        if text is None and callable(ctx.get("message_fn")):
            try:
                text = ctx["message_fn"](run)
            except Exception:
                text = None
        paras = claude_prose(text) if text else []
        whole = claude_prose(text, limit=12000) if text else []
        body["full"] = whole if len(whole) > len(paras) else None
        q = need.get("question") or mon.get("question") or (lane.get("question") if local else None)
        if k == "lane_problem":
            paras, qq = paras, None
            prob = mon.get("problem")
            if prob and not paras:
                paras = claude_prose(prob)
        else:
            paras, qq = split_question(paras, q)
        body["message"], body["question"] = paras, qq
    return body


def writing_body(it: dict, ctx: dict) -> dict:
    k = eff_kind(it)
    texts = ctx.get("texts") or {}
    out = {"kind": "writing", "field_words": "", "fact": ""}
    if k == "write_prompt":
        n = len(re.findall(r"\S+", texts.get("opening_prompt") or ""))
        out["field_words"] = "Opening prompt"
        out["fact"] = f"{n} words so far." if n else "Not started."
    elif k == "write_details":
        miss = _missing(ctx) or ["goal"]
        out["field_words"] = FIELD_WORDS[miss[0]]
        bits = []
        if ctx.get("placed"):
            bits.append(f"{ctx['placed']} finding{'s' if ctx['placed'] != 1 else ''} placed")
        elif ctx.get("findings"):
            bits.append(f"{ctx['findings']} findings to sort")
        if miss[0] != "goal" and (texts.get("goal") or "").strip():
            bits.append("Goal written")
        out["fact"] = ". ".join(bits) + "." if bits else ""
    elif k == "rate":
        out["field_words"] = "Every field written" if ctx.get("rated") and not _missing(ctx) else "Ratings"
        out["fact"] = plain_fact(it.get("reason"))
    elif k == "feedback":
        out["field_words"] = "Reviewer's notes"
        fp = ctx.get("fb_plan") or {}
        if fp.get("state") == "ready":
            n, tot = fp.get("todo") or 0, fp.get("total") or 0
            out["fact"] = (f"{n} of {tot} things they asked for still to do." if n
                           else f"All {tot} things they asked for look done. Save to Studio and resubmit.")
            out["checklist"] = {"todo": n, "total": tot, "summary": fp.get("summary"), "first": fp.get("first")}
        elif fp.get("state") == "planning":
            out["fact"] = "Making a checklist of what they asked for."
        else:
            out["fact"] = "Their words open at the top of the task."
    elif k == "rejected":
        out["field_words"] = "Rejected"
        out["fact"] = plain_fact(it.get("reason"))
    else:
        out["field_words"] = {"save": "Save to Studio", "submit": "Submit", "fix_qc": "Automatic checks"}.get(k, "")
        out["fact"] = plain_fact(it.get("reason"))
    out["path"] = path_for(ctx) if ctx.get("stage") else []
    return out


def primary_for(it: dict, ctx: dict, lane: dict, settings: dict) -> dict | None:
    """The card's one button, as an action the page knows ({label, action, args}). Studio writes are never
    primaries here: save, submit and AutoQC happen on the task page by his press."""
    k = eff_kind(it)
    short = it.get("short")
    local = bool(it.get("lane"))
    if k == "lane_login" and local:
        return {"label": "Log in to Claude", "action": "lane_type", "args": {"what": "login"}}
    if k == "lane_paste" and local:
        if (it.get("title") or "") == "Send your prompt":
            return {"label": "Open the lane", "action": "lane_open", "args": {}}
        return {"label": "Paste my prompt", "action": "lane_type", "args": {"what": "prompt"}}
    if k in ("lane_waiting", "lane_answer", "lane_problem", "lane_async_done") and local:
        return {"label": "Answer in the lane" if k == "lane_waiting" else "Open the lane", "action": "lane_open", "args": {}}
    if k in LANE_KINDS:
        if local:
            return {"label": "Open the lane", "action": "lane_open", "args": {}}
        return {"label": "Open the task", "action": "open", "args": {"href": f"/task/{short}#step=run"}} if short else None
    if k == "wrong_model":
        return {"label": "Open the lane", "action": "lane_open", "args": {}}
    if k == "setup_failed":
        args = (it.get("primary") or {}).get("args") or {}
        return {"label": "Set up the lane again", "action": "lane_start", "args": args}
    if k == "collect_failed":
        return {"label": "Collect the run", "action": "collect", "args": {"short": short, "run": it.get("run")}}
    if k == "blocker_studio":
        return {"label": "Open Studio", "action": "studio_open", "args": {"href": config.STUDIO}}
    if k in BLOCKER_KINDS:
        return None
    if k in START_KINDS:
        args = (it.get("primary") or {}).get("args") or {"short": short, "kind": "validate"}
        calib = args.get("kind") == "calib" and settings.get("calibrate_first")
        return {"label": f"Start the {'calibration' if calib else 'validation'} run", "action": "lane_start",
                "args": {"short": args.get("short") or short, "kind": args.get("kind") or "validate"}}
    if k == "decide":
        return {"label": "Keep", "action": "decision", "args": {"short": short, "run": it.get("run"), "decision": "keep"}}
    if k == "log_discard":
        return {"label": "Open the form", "action": "discard_form", "args": {"short": short}}
    place = place_for(it, ctx)
    if place.get("href"):
        return {"label": "See what to do" if k == "feedback" else "Open", "action": "open",
                "args": {"href": place["href"]}}
    return None


def now_item(it: dict, ctx: dict, lane: dict, settings: dict, now: float, deep: bool = False) -> dict:
    k = eff_kind(it)
    out = {key: it.get(key) for key in ("id", "kind", "short", "title", "label", "est_s", "since", "lane", "run", "host",
                                        "tier", "attention", "blocked")}
    out["kind_card"] = ("lane" if k in LANE_KINDS else "blocker" if k in BLOCKER_KINDS else "start" if k in START_KINDS
                        else "decide" if k == "decide" else "log_discard" if k == "log_discard" else "writing")
    out["eff_kind"] = k
    out["what"] = task_words(it.get("label") or ctx.get("label"))
    out["lede"] = lede_for(it, ctx, lane, settings)
    out["verb"] = verb_for(it, ctx)
    out["fact"] = fact_for(it, ctx)
    out["tone"] = tone_for(it)
    out["place"] = place_for(it, ctx)
    out["primary"] = primary_for(it, ctx, lane, settings)
    if out["kind_card"] == "lane":
        out["body"] = lane_body(it, ctx, lane, now, deep)
    elif out["kind_card"] == "blocker":
        out["body"] = {"kind": "blocker", "what": out["fact"], "fix": out["primary"]}
    elif out["kind_card"] == "start":
        lr = lane.get("run") or {}
        busy = lr.get("short") and lr.get("short") != it.get("short") and lane.get("state") not in ("free", "collected", None)
        other = task_words(lr.get("label")) if busy else None
        out["body"] = {"kind": "start", "prompt_words": len(re.findall(r"\S+", (ctx.get("texts") or {}).get("opening_prompt") or "")),
                       "busy_with": other, "path": path_for(ctx) if ctx.get("stage") else []}
    elif out["kind_card"] == "decide":
        f = _facts_of(ctx, it.get("run"), lane)
        out["body"] = {"kind": "decide", "run_words": run_words(it.get("run")),
                       "facts": [x for x in (f"{f.get('turns')} turn{'s' if f.get('turns') != 1 else ''}" if f.get("turns") else "",
                                             f"{_mins(f.get('active_min'))} min working" if f.get("active_min") is not None else "")
                                 if x]}
    elif out["kind_card"] == "log_discard":
        out["body"] = {"kind": "log_discard", "questions": list(ctx.get("discard_questions") or []),
                       "form": bool(ctx.get("discard_url")), "logged": bool(ctx.get("discard_logged"))}
    else:
        out["body"] = writing_body(it, ctx)
    return out


# --- reviewer lessons (7.6) -------------------------------------------------------------------------------

_MARKERS = (re.compile(r"\bto improve\b", re.I), re.compile(r"\bnext time\b", re.I),
            re.compile(r"\bcould be improved\b", re.I), re.compile(r"\bwhat to fix\b|\bplease fix\b|^\W*fix\b", re.I | re.M),
            re.compile(r"\bplease\b", re.I))


def _clean_md(s: str) -> str:
    s = re.sub(r"\*\*|__|`", "", s)
    s = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", s, flags=re.M)
    return re.sub(r"\s+", " ", s).strip(" \t:")


def _sentences(s: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+(?=[A-Z\"(“])", s) if x.strip()]


def _cut(s: str, n: int = 160) -> str:
    s = s.strip()
    if len(s) <= n:
        return s
    cut = s[:n - 1]
    cut = cut[:cut.rfind(" ")] if " " in cut else cut
    return cut.rstrip(" ,;:") + "…"


def lesson_sentence(feedback: str | None, limit: int = 160) -> str:
    """The improvement sentence from the reviewer's feedback, by a fixed rule: the "what to improve" part
    (johto.rules.extract_lessons), else the sentence after "to improve", "next time", "could be improved", "fix"
    or "please", else the first sentence. One sentence (two when the first is very short), at most `limit` characters
    (160 on Now; Submitted, the history, keeps the whole sentence)."""
    fb = str(feedback or "").strip()
    if not fb:
        return ""
    picked = ""
    try:
        from .. import rules
        found = rules.extract_lessons(fb)
        if found:
            picked = found[0]
    except Exception:
        picked = ""
    if not picked:
        paras = [p for p in re.split(r"\n\s*\n|\n(?=\s*[-*•]\s)", fb) if p.strip()]
        for rx in _MARKERS:
            hit = next((i for i, p in enumerate(paras) if rx.search(p)), None)
            if hit is None:
                continue
            p = _clean_md(paras[hit])
            if p.endswith(":") or paras[hit].rstrip().endswith((":", ":**")) or len(p.split()) <= 14 and hit + 1 < len(paras):
                picked = _clean_md(paras[hit + 1]) if hit + 1 < len(paras) else p
            else:
                # The marker sits inside a paragraph: start at the sentence that carries it.
                ss = _sentences(p)
                at = next((j for j, x in enumerate(ss) if rx.search(x)), 0)
                picked = " ".join(ss[at:]) if ss else p
            break
    if not picked:
        picked = _clean_md(fb)
    sents = _sentences(_clean_md(picked)) or [picked]
    # "Good work overall. Next time, name the threshold.": the lesson starts at the sentence with the marker.
    if not any(rx.search(sents[0]) for rx in _MARKERS):
        at = next((j for j, x in enumerate(sents) if any(rx.search(x) for rx in _MARKERS)), 0)
        sents = sents[at:]
    out = sents[0]
    if len(out) < 60 and len(sents) > 1 and len(out) + 1 + len(sents[1]) <= limit:
        out = f"{out} {sents[1]}"
    return _cut(out, limit)


_LESSON_STATE = {"accepted": "accepted", "sent_back": "sent_back", "rejected": "rejected"}


def lesson_of(short: str, label: str, stage: str, studio: dict) -> dict | None:
    fb = (studio or {}).get("feedback")
    if not isinstance(fb, str) or not fb.strip():
        return None
    state = _LESSON_STATE.get(stage)
    if not state:
        st = (studio.get("status") or "").lower()
        state = "accepted" if "accept" in st else "rejected" if "reject" in st else "sent_back" if "sent" in st or "return" in st else None
    if not state:
        return None
    at = studio.get("feedback_at") or studio.get("polled_at")
    seen = studio.get("feedback_seen_at")
    ts = data._domain().derive.ts
    fresh = not seen or (ts(at) or 0) > (ts(seen) or 0)
    return {"short": short, "label": task_words(label), "state": state, "sentence": lesson_sentence(fb, limit=240),
            "full": fb.strip(), "at": at, "seen": not fresh}


# --- the strongest ready repo (Keep the lane busy) ------------------------------------------------------

def _slot_counts() -> dict:
    try:
        groups = json.loads((config.DATA / "slots.json").read_text()).get("groups") or []
    except (OSError, ValueError):
        return {}
    out: dict = {}
    for g in groups:
        s = g.get("slot") or {}
        key = (s.get("action"), s.get("domain"), s.get("codebase_type"))
        out[key] = out.get(key, 0) + int(g.get("count") or 0)
    return out


def _brief(did: str) -> dict | None:
    try:
        from ..dossier import brief as bmod
        fn = getattr(bmod, "load", None)
        if callable(fn):
            b = fn(did)
            if b:
                return b
    except Exception:
        pass
    p = config.DATA / "dossiers" / f"{did}.brief.json"
    try:
        b = json.loads(p.read_text())
        return b if isinstance(b, dict) and b.get("schema", "").startswith("johto.brief") else None
    except (OSError, ValueError):
        return None


def _first_sentence(s: str | None) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return _sentences(s)[0] if s else ""


def idle_pick(task_repos: set[str], demo: bool = False) -> dict | None:
    """The strongest ready repo, in Find work's own order (so the two screens never disagree): the first ready
    repo with a brief that isn't "probably skip" and no task of his yet, else the first ready one. Falls back to
    the dossier index when Find work's rows can't be built. No model."""
    try:
        from .api_discover import discover
        rows = discover(demo)["repos"]
    except Exception as e:  # noqa: BLE001  (fall back to the index below)
        print(f"johto web: idle pick from Find work failed: {e}", flush=True)
        rows = None
    if rows is not None:
        ready = [r for r in rows if r.get("state") == "ready" and r.get("verdict") != "skip"
                 and r.get("status") == "ready"]
        fresh = [r for r in ready if not ((r.get("yours") or {}).get("done") or (r.get("yours") or {}).get("in_flight"))
                 and (r.get("repo") or "").lower() not in task_repos]
        r = (fresh or ready or [None])[0]
        if r is None:
            return None
        top = (r.get("top_fits") or [None])[0]
        return {"id": r["id"], "name": r.get("name"), "line": _first_sentence(r.get("list_line")),
                "fit": dict(top) if top else None, "brief": True, "verdict": r.get("verdict")}
    if demo:
        return None
    try:
        ix = json.loads((config.DATA / "dossiers" / "index.json").read_text())
    except (OSError, ValueError):
        return None
    counts = _slot_counts()
    best, best_key = None, None
    for i in ix.get("items") or []:
        if i.get("status") != "ready":
            continue
        did = i.get("id")
        try:
            dossier = json.loads((config.DATA / "dossiers" / f"{did}.json").read_text())
        except (OSError, ValueError):
            continue
        opps = {b.get("id"): b for b in dossier.get("blocks") or [] if b.get("type") == "opportunity"}
        brief = _brief(did)
        ids = [s.get("opportunity_id") for s in (brief or {}).get("seeds") or []] or list(opps)
        fits = []
        for oid in ids:
            for f in (opps.get(oid) or {}).get("slot_fit") or []:
                key = (f.get("action"), f.get("domain"), f.get("codebase_type"))
                fits.append({"action": key[0], "domain": key[1], "codebase_type": key[2], "open": counts.get(key, 0)})
        top = max(fits, key=lambda f: f["open"]) if fits else None
        repo = (i.get("repo") or dossier.get("repo") or "")
        dive = ((brief or {}).get("verdict") or {}).get("level") == "dive"
        skip = ((brief or {}).get("verdict") or {}).get("level") == "skip"
        if skip:
            continue
        key = (dive, repo.lower() not in task_repos, (top or {}).get("open", 0), -(i.get("rank") or 0))
        if best_key is None or key > best_key:
            name = (brief or {}).get("name") or repo.split("/")[-1]
            line = _first_sentence((brief or {}).get("list_line")) if brief else ""
            if not line:
                line = plain_fact((dossier.get("verdict") or {}).get("why")) or ""
            best_key = key
            best = {"id": did, "name": name, "line": line, "fit": top, "brief": bool(brief),
                    "verdict": ((brief or {}).get("verdict") or {}).get("level")}
    return best


# --- the whole screen -------------------------------------------------------------------------------------

def _match(q: list[dict], fid: str | None) -> dict | None:
    if not fid:
        return None
    hit = next((i for i in q if i.get("id") == fid), None)
    if hit:
        return hit
    ks = ":".join(fid.split(":")[:2])
    return next((i for i in q if ":".join((i.get("id") or "").split(":")[:2]) == ks and ks.count(":")), None)


def build(queue: list[dict], lane: dict, ctxs: dict, settings: dict, *, pick: dict | None = None,
          lessons: list[dict] | None = None, now: float | None = None, focus_id: str | None = None,
          slot_picks: dict | None = None) -> dict:
    """The /api/now body from the queue (already ordered), the lane, per-task context and settings. Pure.
    slot_picks (johto.slot_options.for_now) shows whatever the lane is doing: open slots go fast. The single
    idle pick only shows when there are none."""
    now = now or time.time()
    lane = lane or {}
    items = [i for i in queue or [] if i.get("kind") != "pick" and i.get("group", "queue") == "queue"]
    first = _match(items, focus_id) or (items[0] if items else None)
    rest = [i for i in items if i is not first]
    ctx = lambda i: ctxs.get(i.get("short") or "", {})  # noqa: E731
    focus = now_item(first, ctx(first), lane, settings, now, deep=True) if first else None
    after = [now_item(i, ctx(i), lane, settings, now) for i in rest[:3]]
    # An After row's middle fact only when it changes the choice: no bookkeeping ("Claimed 20 min ago", "Your
    # opening prompt is saved"), and never the same sentence on two rows.
    seen_facts = {(focus or {}).get("fact")} - {None, ""}
    for a in after:
        f = (a.get("fact") or "").strip()
        if not f or _META_FACT.search(f) or f in seen_facts:
            a["fact"] = ""
        else:
            seen_facts.add(f)
    lane_free = (lane.get("state") or "free") in ("free", "collected") and not lane.get("foreign")
    if focus:
        lede = {"title": focus["lede"], "support": None}
        if focus["kind_card"] == "lane" and not focus["body"].get("local"):
            lede["support"] = f"This run is on the {focus['body'].get('host') or 'PC'}."
        elif focus["body"].get("busy_with"):
            lede["support"] = f"The lane is running {focus['body']['busy_with']}. A second lane starts beside it."
    else:
        lr = lane.get("run") or {}
        if lane_free:
            title = "Nothing needs you. The lane is free."
        elif lane.get("state") == "working" and lr.get("label"):
            title = f"Nothing needs you. Claude is working on {split_label(lr.get('label'))[0] or 'a task'}."
        else:
            title = "Nothing needs you right now."
        lede = {"title": title, "support": None}
    show_pick = bool(pick) and lane_free and _before_validation(ctxs) < int(settings.get("wip_before_validation", 2))
    lessons = [x for x in (lessons or []) if not x.get("seen")]
    if focus and eff_kind(first) == "feedback":
        # The sent-back card quotes the reviewer as written; the reviews strip below doesn't repeat it.
        mine = next((x for x in lessons if x.get("short") == first.get("short") and x.get("state") == "sent_back"), None)
        if mine and focus["body"].get("checklist"):
            lessons = [x for x in lessons if x is not mine]   # the checklist says it better than one sentence
        elif mine and mine.get("sentence"):
            focus["body"]["quote"] = mine["sentence"]
            focus["body"]["fact"] = ""
            lessons = [x for x in lessons if x is not mine]
    picks = slot_picks if slot_picks and (slot_picks.get("items") or slot_picks.get("rating")
                                          or (slot_picks.get("research") or {}).get("weak")
                                          or (slot_picks.get("research") or {}).get("running")) else None
    return {"lede": lede, "focus": focus, "after": after,
            "idle_pick": pick if show_pick and not (picks or {}).get("items") else None,
            "slot_picks": picks, "lessons": lessons}


BEFORE_VALIDATION = {"prompt", "waiting_calib", "calibrating", "decide", "details", "claiming"}


def _before_validation(ctxs: dict) -> int:
    return sum(1 for c in ctxs.values() if (c.get("stage") or "") in BEFORE_VALIDATION)


# --- real and demo sources ------------------------------------------------------------------------------

def _grading_counts(d: Path) -> tuple[int | None, int | None]:
    try:
        from .. import grading
        rows = (grading.load(d) or {}).get("rows") or []
    except Exception:
        return None, None
    placed = sum(1 for r in rows if any((r.get("marks") or {}).get(k) for k in ("goal", "dp", "notes")))
    return placed, len(rows)


def _message_fn(runs: list[dict]):
    def read(run):
        r = next((x for x in runs if x.get("name") == run), None) or (runs[-1] if runs else None)
        p = (r or {}).get("_tpath")
        if not p:
            return None
        from .lane import runmon
        return runmon.tail_facts(Path(p)).get("text")
    return read


def _real_ctx(st: dict) -> dict:
    meta = st.get("meta") or {}
    stage = (st.get("stage") or {}).get("stage")
    ctx = {"short": st["short"], "label": st.get("label"), "slot": st.get("slot") or {}, "stage": stage,
           "texts": st.get("texts") or {}, "runs": st.get("runs") or [], "monitor": st.get("monitor") or {},
           "message_fn": _message_fn(st.get("runs") or []), "repo": ((meta.get("repo") or {}).get("full_name") or "")}
    rt = (st.get("sub") or {}).get("ratings") or {}
    ctx["rated"] = bool((rt.get("one-shot") or "").strip() and re.match(r"\s*\d", rt.get("task success") or "")
                        and re.match(r"\s*\d", rt.get("steering") or ""))
    if stage in ("details", "rate", "validating", "collecting", "waiting_validate"):
        ctx["placed"], ctx["findings"] = _grading_counts(st["dir"])
    if stage == "sent_back" and st.get("dir"):
        try:
            from .. import feedback
            from . import api_feedback
            api_feedback.ensure(Path(st["dir"]), meta)   # a new review: its checklist starts here too
            ctx["fb_plan"] = feedback.brief(Path(st["dir"]), meta)
        except Exception as e:  # noqa: BLE001  (Now stays up)
            print(f"johto web: feedback checklist failed: {e}", flush=True)
    if stage == "dropped":
        dv = data._discard_view(st) if hasattr(data, "_discard_view") else None
        ctx["discard_questions"] = (dv or {}).get("yours") or []
        ctx["discard_url"] = (dv or {}).get("url")
        ctx["discard_logged"] = (dv or {}).get("logged_at")
    return ctx


def real_now(focus_id: str | None = None) -> dict:
    snap = data.real_snapshot()
    states = snap["states"]
    ctxs = {st["short"]: _real_ctx(st) for st in states}
    settings = data.settings()
    lessons = real_lessons(states)
    repos = {c["repo"].lower() for c in ctxs.values() if c.get("repo")}
    try:
        pick = idle_pick(repos)
    except Exception as e:  # research files mid-write must not take Now down
        print(f"johto web: idle pick failed: {e}", flush=True)
        pick = None
    try:
        from .. import slot_options
        picks = slot_options.for_now()
    except Exception as e:  # noqa: BLE001  (same: Now stays up)
        print(f"johto web: slot picks failed: {e}", flush=True)
        picks = None
    if picks is not None:
        try:
            # Research for slot groups with no great option: auto mode starts it here, never while Claude works.
            from .. import slot_research
            busy = (snap.get("lane") or {}).get("state") == "working"
            picks["research"] = slot_research.for_now(picks.get("weak") or [], lane_busy=busy)
        except Exception as e:  # noqa: BLE001
            print(f"johto web: slot research failed: {e}", flush=True)
    return build(snap["queue"], snap["lane"], ctxs, settings, pick=pick, lessons=lessons, focus_id=focus_id,
                 slot_picks=picks)


def _with_feedback(short: str, studio: dict | None) -> dict:
    """The task's Studio block with the reviewer's feedback: task.json's copy, else the last Studio fields read
    (field_review_writer_feedback in app/studio-fields.json)."""
    studio = dict(studio or {})
    if isinstance(studio.get("feedback"), str) and studio["feedback"].strip():
        return studio
    try:
        f = json.loads((config.TASKS / short / "app" / "studio-fields.json").read_text())
        fb = ((f or {}).get("fields") or {}).get("field_review_writer_feedback")
    except (OSError, ValueError, AttributeError):
        fb = None
    if isinstance(fb, str) and fb.strip():
        studio["feedback"] = fb
        studio.setdefault("feedback_at", (f or {}).get("at"))
    return studio


def real_lessons(states: list[dict] | None = None) -> list[dict]:
    if states is None:
        out = []
        for p in sorted(config.TASKS.glob("*/task.json")):
            try:
                meta = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            label = meta.get("label") or f"{(meta.get('repo') or {}).get('full_name', '').split('/')[-1]} · {meta.get('task_name', '')}"
            status = ((meta.get("studio") or {}).get("status") or "").lower()
            stage = "accepted" if "accept" in status else "rejected" if "reject" in status else ""
            les = lesson_of(p.parent.name, label, stage, _with_feedback(p.parent.name, meta.get("studio")))
            if les:
                out.append(les)
        return sorted(out, key=lambda x: x.get("at") or "", reverse=True)
    out = []
    for st in states:
        les = lesson_of(st["short"], st.get("label"), (st.get("stage") or {}).get("stage"),
                        _with_feedback(st["short"], (st.get("meta") or {}).get("studio")))
        if les:
            out.append(les)
    return sorted(out, key=lambda x: x.get("at") or "", reverse=True)


def _demo_message() -> str:
    from . import demo
    paras, cur = [], None
    for ln in demo.TERMINAL:
        t = re.sub(r"\x1b\[[0-9;]*m", "", ln)
        if t.startswith("⏺ "):
            body = t[2:]
            if re.match(r"^(Read|Update|Bash|Write|Edit|Grep|Glob)\(", body):
                cur = None
                continue
            cur = [body]
            paras.append(cur)
        elif cur is not None and t.startswith("  ") and t.strip() and not t.strip().startswith("⎿"):
            cur.append(t.strip())
        else:
            cur = None
    return "\n\n".join(" ".join(p) for p in paras)


def demo_now(req: Request, focus_id: str | None = None) -> dict:
    from . import app as webapp
    from . import demo
    st = webapp._demo_state(req)
    tasks = webapp._demo["tasks"]
    ctxs = {}
    for short, t in tasks.items():
        s = t["summary"]
        stage = {"studio": "ready_to_save", "details": "details", "validating": "validating"}.get(s["stage"], s["stage"])
        ctxs[short] = {"short": short, "label": s["label"], "slot": s["slot"], "stage": stage,
                       "texts": {k: v.get("text") or "" for k, v in (t.get("sections") or {}).items()},
                       "runs": t.get("runs") or [], "monitor": {}, "message": _demo_message() if short == "d3m0aab3" else None,
                       "discard_questions": (t.get("discard") or {}).get("yours") or [],
                       "discard_url": (t.get("discard") or {}).get("url"), "repo": s["repo"].get("full_name") or ""}
    lessons = []
    for r in demo.FINISHED.get("rows") or []:
        stage = {"Accepted": "accepted", "Sent back": "sent_back", "Rejected": "rejected"}.get(r.get("status"))
        les = lesson_of(r["short"], r.get("label"), stage, {"feedback": r.get("feedback"), "feedback_at": None})
        if les:
            lessons.append(les)
    q = st["queue"]
    if not (st.get("settings") or {}).get("calibrate_first"):
        # Austin's order: no calibration runs. The demo afternoon still has some; Now doesn't offer them.
        q = [i for i in q if i.get("kind") != "decide"]
    try:
        pick = idle_pick(set(), demo=True)
    except Exception:
        pick = None
    lane = dict(st["lane"])
    if lane.get("state") == "free" and lane.get("queue"):
        lane["queue"] = [x for x in lane["queue"] if x.get("kind") != "calib"]
    q = _demo_lane_need(q, lane)
    return build(q, lane, ctxs, st.get("settings") or {}, pick=pick, lessons=lessons, focus_id=focus_id)


_DEMO_NEEDS = {"needs_login": ("lane_login", "Log in to Claude in the lane", "The lane's Claude needs /login before the run"),
               "ready_for_prompt": ("lane_paste", "Paste your prompt", "The lane is ready for your opening prompt")}


def _demo_lane_need(q: list[dict], lane: dict) -> list[dict]:
    """The demo queue has no row for a lane that needs a login or the prompt; the real queue (domain.queue
    LANE_ROWS) does. Add the same row on top so the demo shows what Now does then."""
    need = _DEMO_NEEDS.get(lane.get("state"))
    run = lane.get("run") or {}
    if not need or not run.get("short") or any(i.get("lane") for i in q):
        return q
    kind, title, reason = need
    row = {"id": f"{kind}:{run['short']}:{run.get('run')}:demo", "kind": kind, "tier": 1, "short": run["short"],
           "title": title, "label": run.get("label"), "reason": reason, "est_s": 60, "surface": "lane",
           "lane": True, "since": lane.get("since"), "run": run.get("run"), "host": "Mac", "attention": True,
           "primary": None}
    return [row, *q]


def demo_lessons() -> list[dict]:
    from . import demo
    out = []
    for r in demo.FINISHED.get("rows") or []:
        stage = {"Accepted": "accepted", "Sent back": "sent_back", "Rejected": "rejected"}.get(r.get("status"))
        les = lesson_of(r["short"], r.get("label"), stage, {"feedback": r.get("feedback"), "feedback_at": None})
        if les:
            out.append(les)
    return out


# --- routes -----------------------------------------------------------------------------------------------

# --- Submitted (/finished): what happened to what he sent ------------------------------------------------

_SUB_STATE = {"in_review": "in_review", "sent_back": "sent_back", "accepted": "accepted", "rejected": "rejected",
              "dropped": "dropped", "discarded": "dropped", "unclaimed": "dropped"}
_DEMO_STATE = {"In review": "in_review", "Awaiting Review": "in_review", "Sent back": "sent_back", "Accepted": "accepted",
               "Rejected": "rejected", "Dropped": "dropped", "Discarded": "dropped", "Unclaimed": "dropped"}
_NUM = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"]


def _count_words(n: int, rest: str) -> str:
    return f"{_NUM[n] if n < len(_NUM) else n} {rest}"


def _day(iso: str | None, now: float, since: bool = False) -> str:
    """"Oct 31" ("since Oct 31" for a row still waiting on a reviewer); "today" for today."""
    t = data._domain().derive.ts(iso) if iso else None
    if not t:
        return ""
    d, today = time.localtime(t), time.localtime(now)
    day = "today" if (d.tm_year, d.tm_yday) == (today.tm_year, today.tm_yday) else time.strftime("%b ", d) + str(d.tm_mday)
    return f"since {day}" if since else day[:1].upper() + day[1:]


def submitted_lede(rows: list[dict], now: float) -> dict:
    """The Submitted lede: only the counts that matter, none of them zero (spec 3.6)."""
    ts = data._domain().derive.ts
    week = sum(1 for r in rows if r["state"] == "accepted" and (ts(r.get("at")) or 0) >= now - 7 * 86400)
    back = sum(1 for r in rows if r["state"] == "sent_back")
    review = sum(1 for r in rows if r["state"] == "in_review")
    parts = [(week, "accepted this week."), (back, "sent back."), (review, "in review.")]
    said = [(n, rest) for n, rest in parts if n]
    if not rows:
        return {"title": "Nothing submitted yet.", "support": ""}
    if not said:
        return {"title": "Nothing is waiting on a reviewer.", "support": ""}
    title = _count_words(*said[0])
    support = " ".join(_count_words(n, rest) for n, rest in said[1:])
    return {"title": title, "support": support}


def _sub_row(short: str, label: str, state: str, at: str | None, when: str, feedback, times: int) -> dict:
    fb = feedback.strip() if isinstance(feedback, str) and feedback.strip() else None
    return {"short": short, "label": task_words(label), "state": state, "at": at, "when": when,
            "sentence": lesson_sentence(fb, limit=420) if fb else None, "full": fb, "times": times}


def real_submitted() -> dict:
    snap = data.real_snapshot()
    now = time.time()
    rows = []
    for st, s in zip(snap["states"], snap["summaries"]):
        state = _SUB_STATE.get(s.get("stage"))
        if not state:
            continue
        meta = st.get("meta") or {}
        studio = _with_feedback(s["short"], meta.get("studio"))
        stage = st.get("stage") or {}
        at = stage.get("since") or meta.get("submitted_at")
        # Without a submit time the stage's time is Studio's last poll, which is no date to show him.
        when = "" if stage.get("checked") else _day(at, now, since=state == "in_review")
        rows.append(_sub_row(s["short"], s.get("label"), state, at, when, studio.get("feedback"),
                             int(studio.get("times_submitted") or 0)))
    rows.sort(key=lambda r: data._domain().derive.ts(r.get("at")) or 0, reverse=True)
    return {"lede": submitted_lede(rows, now), "rows": rows}


def demo_submitted() -> dict:
    from . import demo
    now = time.time()
    rows = []
    for i, r in enumerate(demo.FINISHED.get("rows") or []):
        state = _DEMO_STATE.get(r.get("status"))
        if not state:
            continue
        at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - (i + 1) * 86400 * 0.8)) + "Z"
        rows.append(_sub_row(r["short"], r.get("label"), state, at, _day(at, now, since=state == "in_review"), r.get("feedback"),
                             int(r.get("times_submitted") or 0)))
    return {"lede": submitted_lede(rows, now), "rows": rows}


async def get_submitted(req: Request):
    try:
        body = demo_submitted() if _is_demo(req) else await asyncio.to_thread(real_submitted)
    except Exception as e:  # noqa: BLE001
        return _err(500, "failed", f"Couldn't read what you submitted: {e.__class__.__name__}")
    return JSONResponse(body)


def _is_demo(req: Request) -> bool:
    return req.cookies.get("johto_mode") == "demo"


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "details": {}}}, status_code=status)


async def get_now(req: Request):
    fid = req.query_params.get("focus") or None
    if fid and len(fid) > 200:
        fid = None
    try:
        body = await asyncio.to_thread(demo_now, req, fid) if _is_demo(req) else await asyncio.to_thread(real_now, fid)
    except Exception as e:  # noqa: BLE001  (Now must say something rather than show a stack)
        return _err(500, "failed", f"Couldn't build Now: {e.__class__.__name__}")
    return JSONResponse(body)


async def get_lessons(req: Request):
    if _is_demo(req):
        return JSONResponse({"lessons": demo_lessons()})
    return JSONResponse({"lessons": await asyncio.to_thread(real_lessons)})


def _mark_seen(short: str) -> dict:
    d = config.TASKS / short
    if not (d / "task.json").exists():
        raise KeyError(short)
    from .. import actions

    def put(m):
        m.setdefault("studio", {})["feedback_seen_at"] = data.now_iso()
    actions.update_meta(d, put)
    try:
        from . import events
        events.poke("lesson_seen")
    except Exception:
        pass
    return {"toast": {"id": f"seen:{short}", "level": "info", "text": "Put away. It stays on Submitted.", "undo": None,
                      "link": None}}


async def post_seen(req: Request):
    short = req.path_params["short"]
    if not SHORT.match(short or ""):
        return _err(400, "invalid", "That is not a task.")
    try:
        return JSONResponse(await asyncio.to_thread(_mark_seen, short))
    except KeyError:
        return _err(404, "not_found", "No such task.")


routes: list = [
    Route("/api/now", get_now, methods=["GET"]),
    Route("/api/now/lessons", get_lessons, methods=["GET"]),
    Route("/api/now/lessons/{short}/seen", post_seen, methods=["POST"]),
    Route("/api/now/submitted", get_submitted, methods=["GET"]),
]
