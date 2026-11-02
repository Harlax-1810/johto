"""A reviewer's feedback on a sent-back task, as a checklist Austin can work through.

- `plan(d)`: once per review text, headless Claude splits the review into items. Each item says what the
  reviewer asked, quotes the reviewer's words, names the kind of work (write a field, run again, attach a
  file, check a fact, decide, rate), the fields it touches, the span of Austin's text it is about, and facts
  the app gathered (run minutes, the diff's files, what Studio holds, GitHub issue states).
- `check(d)`: once per version of his fields, each item is marked addressed, partly or open, with a short
  reason. Critique only.
- `mark(d, id, done)`: Austin ticks an item himself.

The plan never holds wording for his fields: asks whose wording isn't the reviewer's fall back to the
reviewer's own words, and a check's reason that suggests wording is dropped (draftcheck.wording_leak).
Stored in tasks/<t>/app/feedback-plan.json.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import time
from pathlib import Path

from . import actions, draftcheck, sync, transcript, workspace

FILE = "feedback-plan.json"
KINDS = ("write", "run", "attach", "fact", "decide", "rate")
FIELDS = ("opening_prompt", "goal", "decision_points", "codebase", "validation_notes", "ratings",
          "claude_minutes", "final_output", "trajectory_logs")
STATES = ("addressed", "partly", "open")
MAX_ITEMS = 10
MAX_REFS = 6
ENTERPRISE_MINUTES = 30
_LOCK = threading.Lock()

FIELD_LABEL = {**{k: v[2] for k, v in draftcheck.TEXT_FIELDS.items()}, "ratings": "Ratings",
               "claude_minutes": "Claude minutes", "final_output": "Final Output", "trajectory_logs": "Trajectory Logs"}


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def sha(v) -> str:
    s = v if isinstance(v, str) else json.dumps(v, sort_keys=True, default=str)
    return hashlib.sha1(sync.norm(s).encode()).hexdigest()[:12]


def _ws(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _clip(s, n: int) -> str:
    s = _ws(s)
    if len(s) <= n:
        return s
    return s[: n - 3].rsplit(" ", 1)[0].rstrip(",;:") + "..."


# --- the stored file ------------------------------------------------------------------------------------

def path(d: Path) -> Path:
    return d / "app" / FILE


def load(d: Path) -> dict:
    try:
        v = json.loads(path(d).read_text())
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: Path, data: dict) -> None:
    p = path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(p)


def _update(d: Path, fn) -> dict:
    with _LOCK:
        data = load(d)
        fn(data)
        _save(d, data)
        return data


# --- what the review is about ----------------------------------------------------------------------------

def review_of(meta: dict, studio_fields: dict | None = None) -> dict:
    """{text, sha, difficulty_ok, understanding_ok}: the reviewer's words and their two yes/no fields."""
    sf = studio_fields or {}
    text = sync.norm((meta.get("studio") or {}).get("feedback") or sf.get("field_review_writer_feedback"))
    return {"text": text, "sha": sha(text) if text else "",
            "difficulty_ok": _ws(sf.get("field_review_difficulty_ok")) or None,
            "understanding_ok": _ws(sf.get("field_review_understanding_ok")) or None}


def texts_of(d: Path, meta: dict) -> dict:
    """{key: text} of Austin's fields as the app holds them now (the app's copy, else Studio's)."""
    draft = draftcheck.read_draft(d, meta)
    out = {}
    for k, v in draft["fields"].items():
        t = v.get("local") or v.get("studio") or ""
        if t:
            out[k] = t
    r = draft["ratings"]
    out["ratings"] = "; ".join(f"{k}: {r[k]}" for k in ("one-shot", "task success", "steering") if r.get(k))
    m = draft["minutes"]
    out["claude_minutes"] = str(m.get("studio") or m.get("local") or "")
    return {k: v for k, v in out.items() if v}


def fields_sha(texts: dict, facts: dict | None = None) -> str:
    return sha({"texts": texts, "facts": (facts or {}).get("check_key")})


# --- facts the app gathers ------------------------------------------------------------------------------

_TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]+$|_test\.[a-z]+$|Test\.php$|\.(spec|test)\.[jt]sx?$")
_REF = re.compile(r"(?<![\w/])(?:#|PR\s*#?|issues?\s*#?|pull\s*#?)(\d{2,6})\b", re.I)
_TALKS_UPSTREAM = re.compile(r"\b(issue|issues|PR|PRs|pull request|upstream|merged|closed)\b", re.I)


def _diff_files(p: Path | None) -> list[str]:
    if not p:
        return []
    try:
        return [m.group(1) for m in re.finditer(r"^diff --git a/(\S+) b/", p.read_text(errors="ignore"), re.M)]
    except OSError:
        return []


def _gh_json(args: list[str], timeout: int = 20):
    try:
        r = subprocess.run(["gh", "api", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def _codeberg(url: str):
    """State of an issue or pull request on Codeberg (a repo that moved there), or None."""
    m = re.match(r"https://codeberg\.org/([\w.-]+)/([\w.-]+)/(issues|pulls)/(\d+)", url or "")
    if not m:
        return None
    import urllib.request
    api = f"https://codeberg.org/api/v1/repos/{m.group(1)}/{m.group(2)}/issues/{m.group(4)}"
    try:
        with urllib.request.urlopen(urllib.request.Request(api, headers={"Accept": "application/json"}), timeout=20) as r:
            v = json.loads(r.read().decode())
    except Exception:  # noqa: BLE001  (a fact we couldn't read is left out)
        return None
    return {"url": url, "state": v.get("state"), "closed_at": v.get("closed_at"), "title": v.get("title")}


def github_refs(full_name: str, numbers: list[int], gh=_gh_json, codeberg=_codeberg) -> list[dict]:
    """Each issue or PR's state on GitHub: {n, kind, state, reason, closed_at, merged_at, title, prs, moved}.
    `prs` are pull requests that link to it; `moved` is the issue's state where the project moved (Codeberg)."""
    out = []
    for n in numbers[:MAX_REFS]:
        v = gh([f"repos/{full_name}/issues/{n}"])
        if not isinstance(v, dict) or not v.get("number"):
            continue
        rec = {"n": n, "kind": "pr" if v.get("pull_request") else "issue", "state": v.get("state"),
               "reason": v.get("state_reason"), "closed_at": v.get("closed_at"), "title": _clip(v.get("title"), 100),
               "merged_at": (v.get("pull_request") or {}).get("merged_at"), "prs": [], "moved": None}
        tl = gh([f"repos/{full_name}/issues/{n}/timeline?per_page=100"]) or []
        for e in tl if isinstance(tl, list) else []:
            src = ((e or {}).get("source") or {}).get("issue") or {}
            if e.get("event") == "cross-referenced" and src.get("pull_request"):
                rec["prs"].append({"n": src.get("number"), "state": src.get("state"),
                                   "merged_at": (src.get("pull_request") or {}).get("merged_at"),
                                   "repo": (src.get("repository") or {}).get("full_name"),
                                   "title": _clip(src.get("title"), 80)})
        if rec["state"] == "closed":
            cm = gh([f"repos/{full_name}/issues/{n}/comments?per_page=100"]) or []
            last = cm[-1] if isinstance(cm, list) and cm else {}
            body = str((last or {}).get("body") or "")
            url = re.search(r"https://codeberg\.org/[\w.-]+/[\w.-]+/(?:issues|pulls)/\d+", body)
            if url:
                rec["moved"] = codeberg(url.group(0))
                rec["closing_comment"] = _clip(body, 200)
        out.append(rec)
    return out


def gather(d: Path, meta: dict, review: dict | None = None, gh=_gh_json, codeberg=_codeberg) -> dict:
    """Facts the plan and the check may cite: {lines: [str], run, studio, refs, check_key}. Each line is one short
    fact in plain words."""
    review = review or review_of(meta)
    snap = actions.studio_snapshot(d) or {}
    sf = snap.get("fields") or {}
    lines: list[str] = []
    run = draftcheck.submission_run(d, meta)
    info = draftcheck._run_info(d, meta, run) if run else {"rec": None, "summary": None}
    summ = info.get("summary") or {}
    diff = draftcheck._diff_path(d, info.get("rec"))
    files = _diff_files(diff)
    tests = [f for f in files if _TEST_PATH.search(f)]
    mins = summ.get("working_minutes")
    run_f = {"name": run, "working_minutes": mins, "human_turns": summ.get("human_turns"),
             "diff": str(diff) if diff else None, "files": files[:40], "tests": tests[:20]}
    if run:
        if mins is not None:
            n = summ.get("human_turns")
            lines.append(f"{run}: Claude worked {round(mins)} minutes over "
                         + (f"{n} prompt{'s' if n != 1 else ''}." if n else "an unknown number of prompts."))
        if files:
            lines.append(f"{run}'s diff changes {len(files)} file{'s' if len(files) != 1 else ''}"
                         + (f"; test files: {', '.join(tests[:4])}" if tests else "; none of them tests") + ".")
        elif diff:
            lines.append(f"{run}'s diff is empty.")
    others = [r for r in meta.get("runs") or [] if r.get("dir") and Path(r["dir"]).name != run]
    for r in others:
        nm = Path(r["dir"]).name
        lines.append(f"{nm} ({r.get('kind') or 'run'}) " + ("is collected." if r.get("collected_at") else "was never collected."))
    final = _ws(sf.get("field_final_output"))
    conv = _ws(sf.get("field_conversation_log"))
    traj = actions.trajectory_files(sf)
    studio_f = {"final_output": final or None, "conversation_log": conv or None, "trajectory": traj,
                "claude_minutes": sf.get("field_claude_minutes"), "at": snap.get("at")}
    if snap:
        lines.append("Studio's Final Output field " + (f"holds {final}." if final else "is empty."))
        lines.append("Studio's Trajectory Logs: " + (", ".join(traj) if traj else "none") + ".")
        if sf.get("field_claude_minutes") not in (None, ""):
            lines.append(f"Studio's Claude minutes field says {sf.get('field_claude_minutes')}.")
    try:
        gs = actions.gist_state(d, run, meta) if run else {"need": []}
    except Exception:  # noqa: BLE001
        gs = {"need": []}
    if snap and not final and "final_output" in (gs.get("need") or []):
        lines.append("The app hasn't made a gist of the diff for this run yet; Save to Studio makes one.")
    refs: list[dict] = []
    full = (meta.get("repo") or {}).get("full_name")
    if full and _TALKS_UPSTREAM.search(review.get("text") or ""):
        texts = texts_of(d, meta)
        nums: list[int] = []
        for src in (review.get("text") or "", texts.get("codebase", ""), texts.get("goal", "")):
            for m in _REF.finditer(src):
                n = int(m.group(1))
                if n not in nums:
                    nums.append(n)
        refs = github_refs(full, nums, gh=gh, codeberg=codeberg)
        for r in refs:
            s = f"{full} #{r['n']} ({r['kind']}) is {r['state']} on GitHub"
            if r["state"] == "closed":
                s += f" since {str(r.get('closed_at') or '')[:10]}"
                if r.get("merged_at"):
                    s += ", merged"
            mv = r.get("moved")
            if mv and mv.get("state"):
                s += f"; GitHub closed it when the project moved to Codeberg, where it is {mv['state']}"
            prs = r.get("prs") or []
            s += (f"; linked PRs: " + ", ".join(f"#{p['n']} {p['state']}{' merged' if p.get('merged_at') else ''}"
                                               for p in prs[:4])) if prs else "; no PRs link to it on GitHub"
            lines.append(s + ".")
    key = {"final": final, "traj": traj, "runs": [(Path(r["dir"]).name, r.get("collected_at")) for r in
                                                  meta.get("runs") or [] if r.get("dir")]}
    return {"lines": lines, "run": run_f, "studio": studio_f, "refs": refs, "check_key": sha(key), "at": now_iso()}


# --- the plan --------------------------------------------------------------------------------------------

PLAN_SYSTEM = """You turn a reviewer's feedback on a writer's coding-agent evaluation task into a checklist of \
what the writer has to do. The writer must write every word of his fields himself; an evaluator runs AI-writing \
detection on them. You list work. You never write it.

One item for each separate request in the review, in the review's order. Praise and greetings are not items. \
Each item has:
- ask: at most 140 characters in plain words: what has to change, as a task for the writer. It restates only \
what the reviewer asked; never add a request the reviewer didn't make.
- quote: the reviewer's words the item comes from, copied exactly from the review, at most 220 characters.
- kind: write (change the text of a field), run (a new or longer Claude run), attach (a file or link in \
Studio, such as the diff or the tests Claude wrote), fact (a factual claim the reviewer disputes; say what the \
FACTS show), decide (a choice only the writer can make, such as whether to drop the task), rate (a rating).
- fields: the fields it touches, from: opening_prompt, goal, decision_points, codebase, validation_notes, \
ratings, claude_minutes, final_output, trajectory_logs.
- where_field and where: when the request is about particular words of the writer's, the field and an exact \
span copied from that field, at most 120 characters. Otherwise null.
- facts: up to 3 lines copied from FACTS that bear on the item. Never invent a fact.

Also:
- summary: at most 220 characters, plain words: how much work this is and what matters most.
- drop_question: when the review says the task might have to be dropped, one sentence (at most 200 \
characters) on what decides it and what the FACTS show. Otherwise null.

You may not, anywhere in your answer:
- write a sentence, phrase, checklist point, decision point, example or any text the writer could put in a field;
- say what a field should say beyond the reviewer's own words; offer examples with "such as", "for example", \
"like" or "e.g.";
- advise a rating, or predict whether the task will be accepted.

Write for the writer: never name the sections of this message (REVIEW, FACTS, RULES); say "the run", \
"Studio" or "GitHub" instead."""

CHECK_SYSTEM = """You check whether a writer has dealt with each item of a reviewer's feedback on his \
coding-agent evaluation task. You are a checker, not an editor. The writer must write every word himself; an \
evaluator runs AI-writing detection on his fields.

For each item give:
- id: the item's id.
- state: addressed (the writer's current text or the FACTS show the request is met), partly (some of it is \
met), or open (not yet).
- why: at most 160 characters: what is still missing or what shows it is met, stated as a problem or a fact. \
Never a fix or new wording.
- where_field and where: an exact span copied from the writer's field that the state is about, at most 120 \
characters, or null.

Write for the writer: never name the sections of this message (REVIEW, ITEMS, FACTS); say "the run" or \
"Studio" instead.

You may not: write or suggest any sentence, phrase or example for his fields; say what a field should say or \
how to word it; rephrase his text; advise a rating."""


def plan_schema() -> dict:
    item = {"type": "object", "additionalProperties": False,
            "required": ["ask", "quote", "kind", "fields", "where_field", "where", "facts"],
            "properties": {
                "ask": {"type": "string", "maxLength": 160},
                "quote": {"type": "string", "maxLength": 260},
                "kind": {"type": "string", "enum": list(KINDS)},
                "fields": {"type": "array", "items": {"type": "string", "enum": list(FIELDS)}, "maxItems": 4},
                "where_field": {"type": ["string", "null"], "enum": [*FIELDS, None]},
                "where": {"type": ["string", "null"], "maxLength": 140},
                "facts": {"type": "array", "items": {"type": "string", "maxLength": 400}, "maxItems": 3}}}
    return {"type": "object", "additionalProperties": False, "required": ["items", "summary", "drop_question"],
            "properties": {"items": {"type": "array", "items": item, "maxItems": MAX_ITEMS},
                           "summary": {"type": "string", "maxLength": 260},
                           "drop_question": {"type": ["string", "null"], "maxLength": 240}}}


def check_schema() -> dict:
    item = {"type": "object", "additionalProperties": False, "required": ["id", "state", "why", "where_field", "where"],
            "properties": {"id": {"type": "string", "maxLength": 20},
                           "state": {"type": "string", "enum": list(STATES)},
                           "why": {"type": "string", "maxLength": 200},
                           "where_field": {"type": ["string", "null"], "enum": [*FIELDS, None]},
                           "where": {"type": ["string", "null"], "maxLength": 140}}}
    return {"type": "object", "additionalProperties": False, "required": ["items"],
            "properties": {"items": {"type": "array", "items": item, "maxItems": MAX_ITEMS}}}


def _fields_block(texts: dict) -> str:
    return "\n\n".join(f"## {k} ({FIELD_LABEL.get(k, k)})\n{v}" for k, v in texts.items())


def _rules() -> str:
    try:
        return draftcheck._rules_text()
    except Exception:  # noqa: BLE001
        return ""


def plan_prompt(review: dict, texts: dict, facts: dict, meta: dict) -> str:
    slot = meta.get("slot") or {}
    scale = slot.get("codebase_type") or ""
    parts = [
        "# REVIEW\n" + review["text"],
        f"# REVIEWER'S CHECKS\nDifficulty acceptable: {review.get('difficulty_ok') or 'not set'}\n"
        f"Codebase understanding acceptable: {review.get('understanding_ok') or 'not set'}",
        f"# TASK\nRepo: {(meta.get('repo') or {}).get('full_name') or '?'}\nScale: {scale or '?'}\n"
        f"Interaction: {slot.get('interaction_mode') or '?'}",
        "# FACTS\n" + ("\n".join(f"- {x}" for x in facts.get("lines") or []) or "(none)"),
        "# THE WRITER'S FIELDS\n" + _fields_block(texts),
    ]
    r = _rules()
    if r:
        parts.append("# RULES (Mercor's)\n" + r)
    return "\n\n".join(parts)


# Phrases that hand over wording in an ask. The full draftcheck.WORDING is too broad for asks: restating
# "add a success checklist" is the reviewer's request, not wording.
ASK_WORDING = re.compile(
    r"\b(something like|along the lines of|for (example|instance)|e\.g\.|such as|should read|could read|"
    r"would read|you could (write|say)|change (it|this|that) to|replace\w* .{1,40} with|phrased? as\s*[\"“'])", re.I)


def _corpus(*parts) -> str:
    return " ".join(_ws(p).lower() for p in parts if p)


def _quoted_outside(text: str, corpus: str) -> bool:
    for q in draftcheck._quotes(text):
        if _ws(q).lower().strip(" .,;:") not in corpus:
            return True
    return False


_SECTION_NAME = re.compile(r"\b(?:the )?(FACTS|ITEMS|REVIEW)\b")


def plain_refs(text) -> str:
    """The prompt's section names ("FACTS show ...") in the writer's words."""
    t = _SECTION_NAME.sub(lambda m: "the review" if m.group(1) == "REVIEW" else "the record", str(text or ""))
    return t[:1].upper() + t[1:] if t else t


def item_id(quote: str, ask: str) -> str:
    return "f" + sha(_ws(quote or ask).lower())[:8]


def post_plan(raw: dict, review: dict, texts: dict, facts: dict) -> tuple[list[dict], dict]:
    """Items with verified spans and no wording. Returns (items, counts of what was changed)."""
    counts: dict[str, int] = {}

    def note(k):
        counts[k] = counts.get(k, 0) + 1

    corpus = _corpus(review["text"], *texts.values(), *(facts.get("lines") or []))
    fact_lines = facts.get("lines") or []
    out, seen = [], set()
    for x in (raw or {}).get("items") or []:
        if not isinstance(x, dict) or x.get("kind") not in KINDS:
            note("malformed")
            continue
        quote = draftcheck._span(review["text"], x.get("quote")) if x.get("quote") else None
        if x.get("quote") and not quote:
            # _span caps at 120 characters; a longer exact quote still counts when it is in the review.
            q = _ws(x.get("quote"))
            quote = q if q and q.lower() in _ws(review["text"]).lower() else None
            if not quote:
                note("quote_not_in_review")
        ask = _clip(plain_refs(x.get("ask")), 160)
        if not ask or ASK_WORDING.search(ask) or _quoted_outside(ask, corpus) or draftcheck.RATING_ADVICE.search(ask):
            note("ask_wording")
            ask = None   # the UI shows the reviewer's words instead
        if not ask and not quote:
            note("dropped")
            continue
        wf = x.get("where_field")
        where = draftcheck._span(texts.get(wf, ""), x.get("where")) if wf and x.get("where") else None
        if x.get("where") and not where:
            note("where_not_in_field")
        facts_ok = []
        for f in x.get("facts") or []:
            hit = next((ln for ln in fact_lines if _ws(f).lower() in _ws(ln).lower() or _ws(ln).lower() in _ws(f).lower()), None)
            if hit and hit not in facts_ok:
                facts_ok.append(hit)
            elif not hit:
                note("fact_not_gathered")
        iid = item_id(quote or "", ask or "")
        if iid in seen:
            note("duplicate")
            continue
        seen.add(iid)
        flds = [f for f in x.get("fields") or [] if f in FIELDS]
        out.append({"id": iid, "ask": ask, "quote": quote, "kind": x["kind"], "fields": flds,
                    "where": {"field": wf, "text": where} if where else None, "facts": facts_ok[:3],
                    "action": _action(x["kind"], flds, wf if where else None, facts)})
    return out[:MAX_ITEMS], counts


def _action(kind: str, flds: list[str], where_field: str | None, facts: dict) -> dict | None:
    """What the app can open or start for the item. Nothing here writes Studio or his fields."""
    text_fields = [f for f in flds if f in draftcheck.TEXT_FIELDS]
    run = facts.get("run") or {}
    if kind == "run":
        return {"type": "run", "run": run.get("name")}
    if kind == "attach" or (kind != "write" and any(f in ("final_output", "trajectory_logs") for f in flds)):
        return {"type": "attach", "run": run.get("name"), "diff": bool(run.get("diff")), "tests": run.get("tests") or []}
    if kind == "rate" or (kind != "write" and "ratings" in flds):
        return {"type": "field", "field": "ratings"}
    field = where_field if where_field in draftcheck.TEXT_FIELDS else (text_fields[0] if text_fields else None)
    return {"type": "field", "field": field} if field else None


def plan(d: Path, runner=None, log=print, gh=_gh_json, codeberg=_codeberg, add_cost=None) -> dict:
    """Make the checklist for the review Studio holds now. Keeps the manual ticks of items that come back."""
    meta = workspace.load(d)
    snap = actions.studio_snapshot(d) or {}
    review = review_of(meta, snap.get("fields"))
    if not review["text"]:
        raise ValueError("No reviewer feedback on this task.")
    log("gathering facts")
    facts = gather(d, meta, review, gh=gh, codeberg=codeberg)
    texts = texts_of(d, meta)
    log("reading the review")
    runner = runner or draftcheck.claude_runner
    res = runner(PLAN_SYSTEM, plan_prompt(review, texts, facts, meta), plan_schema())
    if add_cost and res.get("cost_usd"):
        add_cost(res["cost_usd"])
    if res.get("error") or not isinstance(res.get("data"), dict):
        err = res.get("error") or "no answer"

        def failed(data):
            data.update(feedback_sha=review["sha"], error=err, error_at=now_iso())
        return _update(d, failed)
    items, counts = post_plan(res["data"], review, texts, facts)
    summary = plain_refs(_ws(res["data"].get("summary")))
    corpus = _corpus(review["text"], *texts.values(), *(facts.get("lines") or []))
    if summary and (ASK_WORDING.search(summary) or _quoted_outside(summary, corpus)):
        summary, counts["summary_wording"] = "", 1
    drop_q = plain_refs(_ws(res["data"].get("drop_question")))
    if drop_q and (ASK_WORDING.search(drop_q) or _quoted_outside(drop_q, corpus)):
        drop_q, counts["drop_wording"] = "", 1

    def put(data):
        done = {k: v for k, v in (data.get("done") or {}).items() if any(i["id"] == k for i in items)}
        data.clear()
        data.update(feedback_sha=review["sha"], feedback=review["text"],
                    review={"difficulty_ok": review["difficulty_ok"], "understanding_ok": review["understanding_ok"]},
                    planned_at=now_iso(), summary=_clip(summary, 240) or None, drop_question=_clip(drop_q, 240) or None,
                    items=items, facts=facts, filtered=counts, cost_usd=res.get("cost_usd"), done=done,
                    check=None, error=None)
    out = _update(d, put)
    log(f"{len(items)} items")
    return out


# --- the check -------------------------------------------------------------------------------------------

def check_prompt(data: dict, texts: dict, facts: dict) -> str:
    items = [{"id": i["id"], "kind": i["kind"], "fields": i["fields"], "reviewer": i.get("quote"),
              "ask": i.get("ask")} for i in data.get("items") or []]
    return "\n\n".join([
        "# REVIEW\n" + (data.get("feedback") or ""),
        "# ITEMS\n" + json.dumps(items, indent=1, ensure_ascii=False),
        "# FACTS (now)\n" + ("\n".join(f"- {x}" for x in facts.get("lines") or []) or "(none)"),
        "# THE WRITER'S FIELDS NOW\n" + _fields_block(texts),
    ])


def check(d: Path, runner=None, log=print, gh=_gh_json, codeberg=_codeberg, add_cost=None) -> dict:
    """Mark each item addressed, partly or open against his fields as they are now."""
    data = load(d)
    if not data.get("items"):
        raise ValueError("No checklist to check yet.")
    meta = workspace.load(d)
    log("gathering facts")
    facts = gather(d, meta, {"text": data.get("feedback") or ""}, gh=gh, codeberg=codeberg)
    texts = texts_of(d, meta)
    fsha = fields_sha(texts, facts)
    log("checking your fields")
    runner = runner or draftcheck.claude_runner
    res = runner(CHECK_SYSTEM, check_prompt(data, texts, facts), check_schema())
    if add_cost and res.get("cost_usd"):
        add_cost(res["cost_usd"])
    if res.get("error") or not isinstance(res.get("data"), dict):
        err = res.get("error") or "no answer"
        return _update(d, lambda x: x.update(check={**(x.get("check") or {}), "error": err, "error_at": now_iso(),
                                                    "fields_sha": fsha}))
    corpus = _corpus(data.get("feedback"), *texts.values(), *(facts.get("lines") or []))
    ids = {i["id"] for i in data["items"]}
    got: dict[str, dict] = {}
    dropped = 0
    for x in res["data"].get("items") or []:
        if not isinstance(x, dict) or x.get("id") not in ids or x.get("state") not in STATES:
            continue
        why = _clip(plain_refs(x.get("why")), 200)
        if why and draftcheck.wording_leak(why, corpus):
            why, dropped = "", dropped + 1
        wf = x.get("where_field")
        where = draftcheck._span(texts.get(wf, ""), x.get("where")) if wf and x.get("where") else None
        got[x["id"]] = {"state": x["state"], "why": why or None,
                        "where": {"field": wf, "text": where} if where else None}

    def put(dd):
        dd["check"] = {"at": now_iso(), "fields_sha": fsha, "items": got, "error": None, "filtered": dropped,
                       "cost_usd": res.get("cost_usd")}
        dd["facts"] = facts
    out = _update(d, put)
    log(f"{sum(1 for v in got.values() if v['state'] == 'addressed')} of {len(ids)} addressed")
    return out


def mark(d: Path, iid: str, done: bool) -> dict:
    def put(data):
        if not any(i["id"] == iid for i in data.get("items") or []):
            raise KeyError(iid)
        dn = data.setdefault("done", {})
        if done:
            dn[iid] = now_iso()
        else:
            dn.pop(iid, None)
    return _update(d, put)


# --- what the app shows ----------------------------------------------------------------------------------

def view(d: Path, meta: dict | None = None, data: dict | None = None) -> dict | None:
    """The checklist for the page: {state, summary, drop_question, review, items: [...], counts, planned_at,
    check: {at, stale, error}, error}. None when there is no review."""
    meta = meta if meta is not None else workspace.load(d)
    data = data if data is not None else load(d)
    review = review_of(meta)
    if not review["text"]:
        return None
    if not data.get("items") and not data.get("error"):
        return {"state": "planning", "items": [], "counts": {"todo": 0, "done": 0, "total": 0}}
    if data.get("feedback_sha") != review["sha"]:
        return {"state": "planning", "items": [], "counts": {"todo": 0, "done": 0, "total": 0}, "outdated": True}
    if data.get("error") and not data.get("items"):
        return {"state": "error", "error": data["error"], "items": [], "counts": {"todo": 0, "done": 0, "total": 0}}
    chk = data.get("check") or {}
    ci = chk.get("items") or {}
    done = data.get("done") or {}
    items = []
    for i in data.get("items") or []:
        c = ci.get(i["id"]) or {}
        state = "done" if i["id"] in done else c.get("state") or "open"
        items.append({**i, "state": state, "manual": i["id"] in done, "checked": c.get("state"),
                      "why": c.get("why"), "check_where": c.get("where"),
                      "label": {k: FIELD_LABEL.get(k, k) for k in i.get("fields") or []}})
    n_done = sum(1 for i in items if i["state"] in ("done", "addressed"))
    studio_only: list[str] = []
    try:
        draft = draftcheck.read_draft(d, meta)
        fl = draft["fields"]
        touched = {f for i in items for f in (i.get("fields") or []) if f in fl}
        studio_only = [f for f in draftcheck.TEXT_FIELDS if f in touched and not fl[f].get("local") and fl[f].get("studio")]
        for i in items:
            for key in ("where", "check_where"):
                w = i.get(key)
                if w and w.get("field") in fl:
                    i[key] = {**w, "in": "app" if draftcheck._span(fl[w["field"]].get("local") or "", w.get("text")) else "studio"}
    except Exception:  # noqa: BLE001
        pass
    try:
        texts = texts_of(d, meta)
        stale = bool(chk.get("at")) and chk.get("fields_sha") != fields_sha(texts, {"check_key": (data.get("facts") or {}).get("check_key")})
    except Exception:  # noqa: BLE001
        stale = False
    return {"state": "ready", "summary": data.get("summary"), "drop_question": data.get("drop_question"),
            "review": data.get("review") or {}, "items": items, "planned_at": data.get("planned_at"),
            "counts": {"todo": len(items) - n_done, "done": n_done, "total": len(items)},
            "studio_only": [{"key": f, "label": FIELD_LABEL.get(f, f)} for f in studio_only],
            "check": {"at": chk.get("at"), "stale": stale, "error": chk.get("error")} if chk else None,
            "facts": (data.get("facts") or {}).get("lines") or [], "error": data.get("error")}


def needs_plan(d: Path, meta: dict, data: dict | None = None, retry_s: float = 1800) -> bool:
    """A review is there and the stored checklist isn't for it (a failed try waits `retry_s`)."""
    review = review_of(meta)
    if not review["text"]:
        return False
    data = data if data is not None else load(d)
    if data.get("feedback_sha") == review["sha"] and data.get("items"):
        return False
    if data.get("feedback_sha") == review["sha"] and data.get("error_at"):
        from .web.domain.derive import ts
        t = ts(data["error_at"])
        if t is not None and time.time() - t < retry_s:
            return False
    return True


def needs_check(d: Path, meta: dict, data: dict | None = None, gap_s: float = 180) -> bool:
    """The checklist exists and his fields changed since the last check (at most every `gap_s`)."""
    data = data if data is not None else load(d)
    if not data.get("items") or data.get("feedback_sha") != review_of(meta)["sha"]:
        return False
    chk = data.get("check") or {}
    from .web.domain.derive import ts
    t = ts(chk.get("error_at") or chk.get("at"))
    if t is not None and time.time() - t < gap_s:
        return False
    texts = texts_of(d, meta)
    return chk.get("fields_sha") != fields_sha(texts, {"check_key": (data.get("facts") or {}).get("check_key")})


# --- CLI -------------------------------------------------------------------------------------------------

def register(sub) -> None:
    p = sub.add_parser("feedback", help="the reviewer's feedback on a sent-back task, as a checklist")
    p.add_argument("task")
    p.add_argument("--plan", action="store_true", help="make the checklist again")
    p.add_argument("--check", action="store_true", help="check the items against your fields now")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cli)


def _cli(args) -> int:
    d = workspace.resolve(args.task)
    if args.plan or not load(d).get("items"):
        plan(d)
    if args.check:
        check(d)
    v = view(d)
    if args.json:
        print(json.dumps(v, indent=2, ensure_ascii=False))
        return 0
    if not v:
        print("No reviewer feedback on this task.")
        return 0
    print(v.get("summary") or "")
    for i in v["items"]:
        mark_ = {"done": "[x]", "addressed": "[x]", "partly": "[~]"}.get(i["state"], "[ ]")
        print(f"{mark_} {i['kind']:6} {i.get('ask') or i.get('quote')}")
        if i.get("why"):
            print(f"         {i['why']}")
    return 0


def brief(d: Path, meta: dict | None = None) -> dict | None:
    """For Now's card, without reading his fields: {state, todo, total, summary, first}."""
    meta = meta if meta is not None else workspace.load(d)
    review = review_of(meta)
    if not review["text"]:
        return None
    data = load(d)
    if data.get("feedback_sha") != review["sha"] or not data.get("items"):
        failed = data.get("feedback_sha") == review["sha"] and data.get("error")
        return {"state": "error" if failed else "planning", "todo": None, "total": None, "summary": None, "first": None}
    ci = (data.get("check") or {}).get("items") or {}
    done = data.get("done") or {}
    open_ = [i for i in data["items"] if i["id"] not in done and (ci.get(i["id"]) or {}).get("state") != "addressed"]
    first = open_[0] if open_ else None
    return {"state": "ready", "todo": len(open_), "total": len(data["items"]), "summary": data.get("summary"),
            "first": (first.get("ask") or first.get("quote")) if first else None}
