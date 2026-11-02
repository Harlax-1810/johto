"""The rules johto knows, in one place: docs/rules.md, plus lessons from reviewer feedback on Austin's tasks.

Rules come from several sources that changed under Austin (the onboarding doc, Studio's form, AutoQC, Slack,
his own task order agreed with Mercor staff), so each entry in docs/rules.md carries its source, date and
status, and a newer rule marks the older one "superseded by <id>". This module parses that file for the
CLI (`johto rules`), the writing kit and the gates. lessons() reads the "what to improve" part of each
reviewer's feedback from tasks/*/task.json.

Nothing here writes or suggests field text: rules and lessons are shown as they are written.

File format (docs/rules.md): a `## Group` heading per stage, then one bullet per rule. The bullet's text
may wrap; its last line is a code span `id · applies_to · source · date · status`. applies_to is one name
or a comma list; status is "active" or "superseded by <id>".
"""
from __future__ import annotations

import json
import re
import sys
import textwrap
from pathlib import Path

from . import config

FILE = config.CHECKOUT / "docs" / "rules.md"

# The AutoQC dimensions that judge the opening prompt (docs/reference/qc_rubrics.md, "## Opening Prompt"),
# plus AI Detection, which runs on every field. A prompt QC gate looks at these.
PROMPT_QC_DIMENSIONS: tuple[str, ...] = (
    "Prompt Style Fit", "Nothing Private Leaks", "Doesn't Solve It", "No Formatting Tells", "AI Detection")

# Names applies_to may use. "fields" means every field Austin writes.
WRITTEN = ("opening_prompt", "goal", "decision_points", "codebase", "initial_state", "validation_notes",
           "reviewer_notes")
STAGES = ("slot", "repo", "task", "run", "lane", "mode", "minutes", "ratings", "submit")
NAMES = (*WRITTEN, *STAGES, "fields")

# Other spellings of the same names: submission.md headings, Studio field ids, lint's field labels, app keys.
ALIASES = {
    "prompt": "opening_prompt", "field_opening_prompt": "opening_prompt",
    "high_level_task_goal": "goal", "high_level_goal": "goal", "field_high_level_goal": "goal",
    "dps": "decision_points", "field_rationale_difficulty": "decision_points",
    "difficulty_codebase_understanding": "codebase", "difficulty_codebase": "codebase",
    "codebase_understanding": "codebase", "field_codebase_overview": "codebase",
    "initial_state_setup": "initial_state", "field_initial_state": "initial_state",
    "notes": "validation_notes", "self_check": "validation_notes", "field_self_check": "validation_notes",
    "notes_for_the_reviewer": "reviewer_notes", "writer_submission_notes": "reviewer_notes",
    "field_writer_submission_notes": "reviewer_notes",
    "runs": "run", "validation": "run", "calib": "run", "lanes": "lane", "interaction_mode": "mode",
    "claude_minutes": "minutes", "field_claude_minutes": "minutes", "budget": "minutes",
    "one_shot": "ratings", "task_success": "ratings", "steering": "ratings", "field_one_shot": "ratings",
    "field_task_success": "ratings", "field_steering": "ratings",
    "submitting": "submit", "trajectory_logs": "submit", "field_trajectory_log_files": "submit",
    "scope": "task", "codebase_type": "slot", "domain": "slot", "action": "slot",
}

_GROUP = re.compile(r"^##\s+(.+?)\s*$")
_BULLET = re.compile(r"^-\s+(.*)$")
_TAIL = re.compile(r"^\s*`([^`]+)`\s*$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class RulesError(ValueError):
    """docs/rules.md has an entry johto can't read."""


def key(name: str) -> str:
    """The canonical applies_to name for any spelling ("High-Level Task Goal", "field_self_check", "Goal")."""
    k = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return ALIASES.get(k, k)


def _entry(group: str, lines: list[str], lineno: int) -> dict:
    m = _TAIL.match(lines[-1]) if len(lines) > 1 else None
    if not m:
        raise RulesError(f"rules.md line {lineno}: the rule has no `id · applies_to · source · date · status` line")
    parts = [p.strip() for p in m.group(1).split("·")]
    if len(parts) != 5:
        raise RulesError(f"rules.md line {lineno}: expected 5 parts separated by ·, got {len(parts)}")
    rid, applies, source, date, status = parts
    targets = [key(a) for a in applies.split(",") if a.strip()]
    bad = [t for t in targets if t not in NAMES]
    if bad or not targets:
        raise RulesError(f"rules.md line {lineno}: unknown applies_to {', '.join(bad) or '(empty)'}")
    if not _DATE.match(date):
        raise RulesError(f"rules.md line {lineno}: date {date!r} is not YYYY-MM-DD")
    sm = re.fullmatch(r"(active)|superseded by ([\w-]+)", status)
    if not sm:
        raise RulesError(f"rules.md line {lineno}: status must be 'active' or 'superseded by <id>'")
    text = " ".join(ln.strip() for ln in lines[:-1])
    return {"id": rid, "applies_to": targets[0], "fields": targets, "text": text, "source": source,
            "date": date, "status": "active" if sm.group(1) else "superseded", "superseded_by": sm.group(2),
            "group": group}


def parse(text: str) -> list[dict]:
    """Rules in file order. Raises RulesError on a malformed entry, a duplicate id or a dangling pointer."""
    out: list[dict] = []
    group, cur, start = "", None, 0

    def flush():
        if cur:
            out.append(_entry(group, cur, start))

    for i, line in enumerate(text.splitlines(), 1):
        g, b = _GROUP.match(line), _BULLET.match(line)
        if g:
            flush()
            group, cur = g.group(1), None
        elif b and group:
            flush()
            cur, start = [b.group(1)], i
        elif cur is not None and line.startswith(" ") and line.strip():
            cur.append(line)
        elif cur is not None and not line.strip():
            flush()
            cur = None
    flush()
    ids = [r["id"] for r in out]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise RulesError(f"rules.md: duplicate ids {', '.join(dup)}")
    for r in out:
        if r["superseded_by"] and r["superseded_by"] not in ids:
            raise RulesError(f"rules.md: {r['id']} is superseded by {r['superseded_by']}, which doesn't exist")
    return out


def load(path: Path | None = None) -> list[dict]:
    """Every rule in docs/rules.md: {id, applies_to, fields, text, source, date, status, superseded_by, group}."""
    return parse(Path(path or FILE).read_text(encoding="utf-8"))


def for_field(name: str, include_superseded: bool = False, rules: list[dict] | None = None) -> list[dict]:
    """Active rules for one field or stage (any spelling). Rules for "fields" count for every written field."""
    k = key(name)
    out = []
    for r in rules if rules is not None else load():
        if r["status"] != "active" and not include_superseded:
            continue
        if k in r["fields"] or ("fields" in r["fields"] and k in WRITTEN):
            out.append(r)
    return sorted(out, key=lambda r: r["status"] != "active")


# --- reviewer lessons -----------------------------------------------------------------------------------

# A line that opens the reviewer's "what to improve" part ("**What to fix next time:**",
# "One small thing to improve is the goal.").
_IMPROVE = re.compile(
    r"\b(what to (fix|improve|change)|things? to (fix|improve|change)|to improve|improvements?\b|"
    r"for next time|next time|could be improved|suggestions?\b|areas? (for|of) improvement)", re.I)
_REVIEW_HEAD = re.compile(r"^\W*review\s+\d+\b", re.I)
_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")

# Which field a lesson is about, from its wording. First match wins.
_LESSON_FIELD = (
    (re.compile(r"validation notes?|self.check", re.I), "validation_notes"),
    (re.compile(r"\bgoal\b", re.I), "goal"),
    (re.compile(r"decision points?", re.I), "decision_points"),
    (re.compile(r"codebase|difficulty (rationale|notes|&)", re.I), "codebase"),
    (re.compile(r"opening prompt|\bprompt\b", re.I), "opening_prompt"),
    (re.compile(r"steering|task success|one.shot|rating", re.I), "ratings"),
)


def _clean(s: str) -> str:
    s = re.sub(r"\*\*|__", "", s)
    return re.sub(r"\s+", " ", s).strip(" \t:")


def extract_lessons(feedback: str) -> list[str]:
    """The "what to improve" items in one reviewer feedback text, one string per bullet (or per paragraph
    when the part has no bullets). Feedback without such a part gives []."""
    out: list[str] = []
    part: list[str] | None = None   # lines of the current improve part
    head = ""                       # what the opening line says after the marker

    def close():
        if part is None:
            return
        bullets, paras, cur = [], [], None
        for ln in part:
            m = _ITEM.match(ln)
            if m:
                cur = [m.group(1)]
                bullets.append(cur)
            elif ln.strip() and cur is not None:
                cur.append(ln)   # a bullet's wrapped line
            elif ln.strip():
                paras.append(ln)
            else:
                cur = None
        if bullets:
            texts = [_clean(" ".join(b)) for b in bullets]
        else:
            texts = [head] + [_clean(p) for p in paras]
        out.extend(t for t in texts if t)

    for line in (feedback or "").splitlines():
        if _REVIEW_HEAD.match(line):
            close()
            part = None
            continue
        m = _IMPROVE.search(line)
        if m and not _ITEM.match(line):
            close()
            part = []
            # "What to fix next time: X" keeps X; a sentence with no colon is a lesson on its own.
            rest = line[m.end():]
            head = _clean(rest.split(":", 1)[1]) if ":" in rest else _clean(line)
            continue
        if part is not None:
            part.append(line)
    close()
    return out


def lesson_field(text: str) -> str | None:
    for pat, name in _LESSON_FIELD:
        if pat.search(text):
            return name
    return None


def lessons(tasks_dir: Path | None = None) -> list[dict]:
    """[{task, date, text, field}] from reviewer feedback in tasks/*/task.json, newest first."""
    out = []
    for p in sorted(Path(tasks_dir or config.TASKS).glob("*/task.json")):
        try:
            st = json.loads(p.read_text()).get("studio") or {}
        except (OSError, ValueError):
            continue
        fb = st.get("feedback")
        if not isinstance(fb, str) or not fb.strip():
            continue
        date = (st.get("feedback_at") or st.get("polled_at") or "")[:10] or None
        for t in extract_lessons(fb):
            out.append({"task": p.parent.name, "date": date, "text": t, "field": lesson_field(t)})
    return sorted(out, key=lambda x: x["date"] or "", reverse=True)


# --- CLI ------------------------------------------------------------------------------------------------

def _wrap(text: str, indent: str) -> str:
    return textwrap.fill(text, width=100, initial_indent=indent, subsequent_indent=indent + "  ",
                         break_on_hyphens=False, break_long_words=False)


def render_rule(r: dict) -> str:
    parts = [r["id"], *([f"replaced by {r['superseded_by']}"] if r["status"] != "active" else []),
             r["source"], r["date"]]
    return _wrap("- " + r["text"], "  ") + "\n      " + " · ".join(parts)  # one line, so ids stay greppable


def render(rules: list[dict]) -> str:
    """Active rules under their group headings, then the superseded ones."""
    blocks, group = [], None
    for r in [r for r in rules if r["status"] == "active"]:
        if r["group"] != group:
            group = r["group"]
            blocks.append(("\n" if blocks else "") + group)
        blocks.append(render_rule(r))
    old = [r for r in rules if r["status"] != "active"]
    if old:
        blocks.append("\nSuperseded")
        blocks += [render_rule(r) for r in old]
    return "\n".join(blocks)


def render_lessons(items: list[dict]) -> str:
    if not items:
        return "No reviewer lessons yet."
    return "\n".join(_wrap(f"- {x['task']} ({x['date'] or 'no date'}): {x['text']}", "  ") for x in items)


def cmd_rules(a) -> None:
    if a.lessons:
        items = lessons()
        if a.field:
            items = [x for x in items if x["field"] == key(a.field)]
        print(render_lessons(items))
        return
    try:
        rules = load()
    except (OSError, RulesError) as e:
        raise SystemExit(str(e))
    if a.field:
        k = key(a.field)
        if k not in NAMES:
            raise SystemExit(f"Unknown field {a.field!r}. Use one of: {', '.join(NAMES)}")
        rules = for_field(k, include_superseded=True, rules=rules)
        if not rules:
            print(f"No rules for {k}.")
            return
    print(render(rules))
    if a.field:
        items = [x for x in lessons() if x["field"] == key(a.field)]
        if items:
            print("\nFrom your reviews\n" + render_lessons(items))


def register(sub) -> None:
    s = sub.add_parser("rules", help="the rules johto knows (docs/rules.md), or reviewer lessons")
    s.add_argument("--field", help="one field or stage, e.g. goal, decision_points, validation_notes, submit")
    s.add_argument("--lessons", action="store_true", help="what reviewers asked you to improve")
    s.set_defaults(func=cmd_rules)


if __name__ == "__main__":  # python -m johto.rules: check the file parses
    try:
        print(f"{len(load())} rules")
    except RulesError as e:
        sys.exit(str(e))
