"""Local checks that mirror the Johto writer guide, AutoQC rubric and reviewer feedback.

These only flag problems. They never suggest replacement wording: every field has to be
written by Austin, and Studio runs AI detection on all of them.
"""
from __future__ import annotations

import re

from . import config

ERROR, WARN = "error", "warn"

GREETING = re.compile(r"^\s*(hi|hey|hello|yo|good (morning|afternoon|evening)|greetings)\b", re.I)
POLITE = re.compile(r"\b(please|could you|would you mind|can you please|kindly)\b", re.I)
ROLEPLAY = re.compile(r"\byou are (a|an|the) (senior|expert|staff|principal|experienced|skilled)\b", re.I)
FORM_LABEL = re.compile(r"^\s*(opening prompt|ticket|description|task|prompt|summary|context)\s*:", re.I | re.M)
LIST_LINE = re.compile(r"^\s*([-*•]|\d+[.)])\s+", re.M)
MD_TELL = re.compile(r"\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\)|^#{1,6} ", re.M)
STEPS = re.compile(r"\bstep\s*\d+\b", re.I)
GRADING = re.compile(r"\b(you will be graded|rubric|grading|checklist|decision point)\b", re.I)
CRITERIA = re.compile(r"\b(must|should|make sure|ensure|needs? to|has to|have to|done when|acceptance)\b", re.I)
GENERIC = re.compile(r"\b(large,? complex codebase|many moving parts|complex codebase|large codebase)\b", re.I)
# Common tells of model-written prose. Heuristic only.
AI_TELLS = re.compile(
    r"\b(comprehensive|robust|seamless(ly)?|leverag(e|es|ing)|delve|furthermore|moreover|additionally|"
    r"it'?s worth noting|in summary|in conclusion|holistic|meticulous(ly)?|underscores?|pivotal|"
    r"nuanced|streamlin(e|ed|ing)|facilitat(e|es|ing)|ensuring that|not only .{1,40} but also)\b", re.I)


def words(s: str) -> int:
    return len(re.findall(r"\b[\w'/.-]+\b", s or ""))


def sentences(s: str) -> int:
    s = re.sub(r"`[^`]*`", "x", s or "")
    s = re.sub(r"\b(e\.g|i\.e|etc|vs)\.", "x", s)
    return len([p for p in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(`])", s.strip()) if p.strip()])


def ngrams(s: str, n: int = 6) -> set:
    toks = re.findall(r"[a-z0-9]+", (s or "").lower())
    return {" ".join(toks[i:i + n]) for i in range(len(toks) - n + 1)}


def check_prompt(p: str, style: str) -> list[tuple]:
    out = []
    if not p.strip():
        return [(ERROR, "Opening Prompt", "empty")]
    n = words(p)
    first = p.strip().splitlines()[0]
    if GREETING.search(first):
        out.append((ERROR, "Opening Prompt", "starts with a greeting"))
    if POLITE.search(p):
        out.append((ERROR, "Opening Prompt", f"'please'/'could you' framing: {POLITE.search(p).group(0)!r}"))
    if ROLEPLAY.search(p):
        out.append((ERROR, "Opening Prompt", "role-play opener ('You are a ...')"))
    if FORM_LABEL.search(p):
        out.append((ERROR, "Opening Prompt", f"form label: {FORM_LABEL.search(p).group(0).strip()!r}"))
    if STEPS.search(p):
        out.append((ERROR, "Opening Prompt", "step-by-step procedure ('Step N')"))
    if GRADING.search(p):
        out.append((ERROR, "Opening Prompt", f"grading/rubric language: {GRADING.search(p).group(0)!r}"))
    if style == "Casual prose":
        if n < 15 or n > 80:
            out.append((ERROR if n > 120 else WARN, "Opening Prompt", f"{n} words; casual prose is about 20-60"))
        elif n < 20 or n > 60:
            out.append((WARN, "Opening Prompt", f"{n} words; casual prose is about 20-60"))
        if LIST_LINE.search(p):
            out.append((ERROR, "Opening Prompt", "has a list; casual prose has no lists"))
        if MD_TELL.search(p):
            out.append((WARN, "Opening Prompt", "markdown bold/links/headings read as a template"))
        c = len(CRITERIA.findall(p))
        if c > 1:
            out.append((WARN, "Opening Prompt", f"{c} requirement words (must/should/ensure...); at most one checkable clause"))
    elif style == "Explicit spec":
        if n < 450 or n > 1200:
            out.append((WARN, "Opening Prompt", f"{n} words; explicit spec is about 450-1,200"))
        if p == p.lower():
            out.append((ERROR, "Opening Prompt", "all lowercase; explicit spec needs clean writing"))
        if not re.search(r"^\s*(\d+[.)]|\(\d+\))\s+", p, re.M) and not re.search(r"\(\d\)", p):
            out.append((WARN, "Opening Prompt", "no numbered requirements found"))
        if not re.search(r"\b(done when|acceptance criteria|definition of done|done:)", p, re.I):
            out.append((WARN, "Opening Prompt", "no clear 'done when' / acceptance criteria"))
    elif style.startswith("Error"):
        artifact = re.search(r"```.+?```", p, re.S)
        loglike = len(re.findall(r"^\s*(at |File \"|Traceback|\w*Error|\w*Exception|panic:|FAIL|ERR|E\s)", p, re.M))
        if not artifact and loglike < 2:
            out.append((ERROR, "Opening Prompt", "no pasted error/log/stack trace found"))
        rest = re.sub(r"```.+?```", "", p, flags=re.S)
        if artifact and words(rest) > 40:
            out.append((WARN, "Opening Prompt", f"{words(rest)} words outside the artifact; it should be one short line"))
        if CRITERIA.search(rest):
            out.append((WARN, "Opening Prompt", "criteria words outside the artifact; error briefs have no criteria"))
    return out


def check_sections(sec: dict, slot: dict) -> list[tuple]:
    out = []
    goal = sec.get("High-Level Task Goal", "")
    dps = sec.get("Decision Points", "")
    cb = sec.get("Difficulty & Codebase Understanding", "")
    notes = sec.get("Validation Notes", "")
    prompt = sec.get("Opening Prompt", "")
    if not goal:
        out.append((ERROR, "Goal", "empty"))
    else:
        ns = sentences(goal)
        if ns > 4 and slot.get("prompt_style") != "Explicit spec":
            out.append((WARN, "Goal", f"{ns} sentences; aim for 1-3 (more is OK only to add context)"))
        if words(goal) < 15:
            out.append((WARN, "Goal", "very short; say what a proper solution must do and what doesn't count"))
        if not re.search(r"\b(not|no|without|never|doesn't|don't|isn't)\b", goal, re.I):
            out.append((WARN, "Goal", "no must-nots (shortcuts that don't count, e.g. loosening tests)"))
    if not dps:
        out.append((ERROR, "Decision Points", "empty"))
    else:
        b = len(re.findall(r"\b(better|stronger|strong)\b", dps, re.I))
        w = len(re.findall(r"\b(worse|weaker|weak)\b", dps, re.I))
        if min(b, w) < 2:
            out.append((ERROR, "Decision Points", f"needs 2-3 better-vs-worse pairs (found {b} better, {w} worse)"))
        if max(b, w) > 4:
            out.append((WARN, "Decision Points", "more than 3 points; keep 2-3"))
        if words(dps) < 15:
            out.append((ERROR, "Decision Points", "under 15 words (Studio minimum)"))
    if not cb:
        out.append((ERROR, "Difficulty & Codebase", "empty"))
    else:
        if words(cb) < 15:
            out.append((ERROR, "Difficulty & Codebase", "under 15 words (Studio minimum)"))
        ns = sentences(cb)
        if ns < 3 or ns > 6:
            out.append((WARN, "Difficulty & Codebase", f"{ns} sentences; aim for 3-5"))
        if GENERIC.search(cb):
            out.append((WARN, "Difficulty & Codebase", f"generic phrase {GENERIC.search(cb).group(0)!r}; name the mechanism"))
    if not notes:
        out.append((ERROR, "Validation Notes", "empty"))
    elif sentences(notes) < 2:
        out.append((WARN, "Validation Notes", "say where it struggled, what went wrong, and how much you steered"))
    if slot.get("action") == "Operate" and not sec.get("Initial State / Setup"):
        out.append((WARN, "Initial State", "Operate tasks should describe how to reach the starting state"))
    # The prompt must not repeat the private fields.
    shared = ngrams(prompt) & (ngrams(goal) | ngrams(dps))
    if shared:
        out.append((WARN, "Opening Prompt", f"repeats private goal/decision-point wording: {sorted(shared)[0]!r}"))
    for name, text in (("Opening Prompt", prompt), ("Goal", goal), ("Decision Points", dps),
                       ("Difficulty & Codebase", cb), ("Validation Notes", notes)):
        hits = sorted({m.group(0).lower() for m in AI_TELLS.finditer(text)})
        if hits:
            out.append((WARN, name, f"words that read as AI-written: {', '.join(hits)}"))
        if "—" in text:
            out.append((WARN, name, "em dashes read as AI-written"))
    return out


def check_ratings(ratings: dict, slot: dict) -> list[tuple]:
    out = []
    one = (ratings.get("one-shot") or "").strip().lower()
    ts = re.match(r"\s*(\d)", ratings.get("task success") or "")
    st = re.match(r"\s*(\d)", ratings.get("steering") or "")
    ts, st = (int(ts.group(1)) if ts else None), (int(st.group(1)) if st else None)
    if one not in ("yes", "no"):
        out.append((ERROR, "Ratings", "One-shot must be Yes or No"))
    elif one == "yes":
        out.append((ERROR, "Ratings", "One-shot = Yes: the task is too easy. Drop it and log it in the Discarded Run Log"))
    if ts is None:
        out.append((ERROR, "Ratings", "Task Success (1-5) missing"))
        return out
    ent = slot.get("codebase_type", "").startswith("Enterprise")
    if ts == 5:
        out.append((ERROR, "Ratings", "Task Success 5 is always rejected"))
    elif ent and ts == 4:
        out.append((ERROR, "Ratings", "Enterprise Task Success 4 is rejected"))
    elif ent and ts == 3 and (st is None or st > 2):
        out.append((ERROR, "Ratings", "Enterprise Task Success 3 passes only with Steering 1-2"))
    elif not ent and ts == 4 and (st is None or st > 2):
        out.append((ERROR, "Ratings", "Personal-scale Task Success 4 passes only with Steering 1-2"))
    needs_steering = (ent and ts == 3) or (not ent and ts in (3, 4))
    if needs_steering and st is None:
        out.append((ERROR, "Ratings", "Steering is required for this Task Success"))
    return out


def check_repo(repo: dict) -> list[tuple]:
    out = []
    url = repo.get("pinned_url") or ""
    if not re.fullmatch(r"https://github\.com/[^/\s]+/[^/\s]+/tree/[0-9a-f]{40}/?", url):
        out.append((ERROR, "Repo", f"URL is not pinned to a 40-char commit: {url!r}"))
    if (repo.get("full_name") or "").lower() in config.SWE_BENCH_REPOS:
        out.append((ERROR, "Repo", "SWE-bench-Verified repo"))
    if not repo.get("language"):
        out.append((ERROR, "Repo", "primary language not set"))
    return out


def run_all(meta: dict, sub: dict) -> list[tuple]:
    slot = meta["slot"]
    sec = sub["sections"]
    out = check_repo(meta["repo"])
    out += check_prompt(sec.get("Opening Prompt", ""), slot.get("prompt_style", ""))
    out += check_sections(sec, slot)
    out += check_ratings(sub["ratings"], slot)
    return out


def render(problems: list[tuple]) -> str:
    if not problems:
        return "No problems found."
    order = sorted(problems, key=lambda p: (p[0] != ERROR, p[1]))
    return "\n".join(f"  {'ERROR' if lvl == ERROR else 'warn '}  {field}: {msg}" for lvl, field, msg in order)
