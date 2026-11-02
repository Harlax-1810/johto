"""Local checks that mirror the Johto writer guide, AutoQC rubric and reviewer feedback.

These only flag problems. They never suggest replacement wording: every field has to be
written by Austin, and Studio runs AI detection on all of them. check_saved_fields looks at what
Studio actually saved (app/studio-fields.json) for text cut off by a paste.

Each flag is a (level, field, message) tuple. It also carries the docs/rules.md rule it comes from, a kind
(what sort of problem it is) and, when there is one, the words it is about, so johto/draftcheck.py can sort
flags into "needs attention" and "optional" without reading the messages. Word counters (sentences, better
and worse markers) only point at a structure to look at; they never block.
"""
from __future__ import annotations

import functools
import json
import re
from pathlib import Path

from . import config, workspace

ERROR, WARN = "error", "warn"

# What a flag is about. draftcheck treats missing, ratings, repo, cut and unsaved as needing attention, form
# as needing attention only at ERROR level, and the rest (word and sentence counts, structure counters,
# wording tells, prompt and goal sharing words) as optional.
KINDS = ("missing", "form", "length", "structure", "wording", "leak", "ratings", "repo", "cut", "unsaved")


class Flag(tuple):
    """One flag. A (level, field, message) tuple, so every caller that unpacks three values keeps working,
    plus `rule` (a docs/rules.md id or None), `kind` (one of KINDS) and `quote` (the words it is about, copied
    from the text, or None)."""

    def __new__(cls, level: str, field: str, message: str, rule: str | None = None, kind: str = "form",
                quote: str | None = None):
        f = super().__new__(cls, (level, field, message))
        f.rule, f.kind, f.quote = rule, kind, quote
        return f

    def __getnewargs__(self):
        return (*self, self.rule, self.kind, self.quote)


def flag_info(f) -> dict:
    """{level, field, message, rule, kind, quote} for a Flag or a plain 3-tuple (kind "form" when unknown)."""
    return {"level": f[0], "field": f[1], "message": f[2], "rule": getattr(f, "rule", None),
            "kind": getattr(f, "kind", "form"), "quote": getattr(f, "quote", None)}


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
# Words that mark the better and the worse side of a decision point. Austin writes good/bad as often as
# strong/weak, so both count; the count only says whether the structure is worth a look.
BETTER = re.compile(r"\b(better|stronger|strong|good|best)\b", re.I)
WORSE = re.compile(r"\b(worse|weaker|weak|bad|worst|poor)\b", re.I)
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


def _hit(rx: re.Pattern, text: str) -> str | None:
    m = rx.search(text or "")
    return m.group(0).strip() if m else None


def _sentence_with(text: str, needle: str, limit: int = 120) -> str | None:
    """The stretch of `text` around `needle` (up to `limit` characters), copied as it is."""
    i = (text or "").find(needle)
    if i < 0:
        return None
    a = max(0, i - (limit - len(needle)) // 2)
    return text[a:a + limit].strip() or None


def check_prompt(p: str, style: str) -> list[tuple]:
    out = []
    F = "Opening Prompt"
    if not p.strip():
        return [Flag(ERROR, F, "empty", "prompt-only-text", "missing")]
    n = words(p)
    first = p.strip().splitlines()[0]
    if GREETING.search(first):
        out.append(Flag(ERROR, F, "starts with a greeting", "prompt-no-tells", "form", _hit(GREETING, first)))
    if POLITE.search(p):
        q = _hit(POLITE, p)
        out.append(Flag(ERROR, F, f"'please'/'could you' framing: {POLITE.search(p).group(0)!r}", "prompt-no-tells",
                        "form", q))
    if ROLEPLAY.search(p):
        out.append(Flag(ERROR, F, "role-play opener ('You are a ...')", "prompt-no-tells", "form", _hit(ROLEPLAY, p)))
    if FORM_LABEL.search(p):
        q = _hit(FORM_LABEL, p)
        out.append(Flag(ERROR, F, f"form label: {q!r}", "prompt-no-tells", "form", q))
    if STEPS.search(p):
        out.append(Flag(ERROR, F, "step-by-step procedure ('Step N')", "spec-no-steps", "form", _hit(STEPS, p)))
    if GRADING.search(p):
        out.append(Flag(ERROR, F, f"grading/rubric language: {GRADING.search(p).group(0)!r}", "prompt-no-leak",
                        "form", _hit(GRADING, p)))
    if style == "Casual prose":
        if n < 15 or n > 80:
            out.append(Flag(ERROR if n > 120 else WARN, F, f"{n} words; casual prose is about 20-60", "casual-length",
                            "form" if n > 120 else "length"))
        elif n < 20 or n > 60:
            out.append(Flag(WARN, F, f"{n} words; casual prose is about 20-60", "casual-length", "length"))
        if LIST_LINE.search(p):
            out.append(Flag(ERROR, F, "has a list; casual prose has no lists", "casual-voice", "form",
                            _hit(re.compile(r"^\s*([-*•]|\d+[.)])\s+.{0,60}", re.M), p)))
        if MD_TELL.search(p):
            out.append(Flag(WARN, F, "markdown bold/links/headings read as a template", "prompt-no-tells", "form",
                            _hit(MD_TELL, p)))
        c = len(CRITERIA.findall(p))
        if c > 1:
            out.append(Flag(WARN, F, f"{c} requirement words (must/should/ensure...); at most one checkable clause",
                            "casual-length", "wording"))
    elif style == "Explicit spec":
        if n < 450 or n > 1200:
            out.append(Flag(WARN, F, f"{n} words; explicit spec is about 450-1,200", "spec-clean", "length"))
        if p == p.lower():
            out.append(Flag(ERROR, F, "all lowercase; explicit spec needs clean writing", "spec-clean", "form"))
        if not re.search(r"^\s*(\d+[.)]|\(\d+\))\s+", p, re.M) and not re.search(r"\(\d\)", p):
            out.append(Flag(WARN, F, "no numbered requirements found", "spec-ticket", "structure"))
        if not re.search(r"\b(done when|acceptance criteria|definition of done|done:)", p, re.I):
            out.append(Flag(WARN, F, "no clear 'done when' / acceptance criteria", "spec-done-when", "structure"))
    elif style.startswith("Error"):
        artifact = re.search(r"```.+?```", p, re.S)
        loglike = len(re.findall(r"^\s*(at |File \"|Traceback|\w*Error|\w*Exception|panic:|FAIL|ERR|E\s)", p, re.M))
        if not artifact and loglike < 2:
            out.append(Flag(ERROR, F, "no pasted error/log/stack trace found", "error-artifact-first", "form"))
        rest = re.sub(r"```.+?```", "", p, flags=re.S)
        if artifact and words(rest) > 40:
            out.append(Flag(WARN, F, f"{words(rest)} words outside the artifact; it should be one short line",
                            "error-artifact-first", "length"))
        if CRITERIA.search(rest):
            out.append(Flag(WARN, F, "criteria words outside the artifact; error briefs have no criteria",
                            "error-no-criteria", "wording", _hit(CRITERIA, rest)))
    return out


def check_sections(sec: dict, slot: dict) -> list[tuple]:
    out = []
    goal = sec.get("High-Level Task Goal", "")
    dps = sec.get("Decision Points", "")
    cb = sec.get("Difficulty & Codebase Understanding", "")
    notes = sec.get("Validation Notes", "")
    prompt = sec.get("Opening Prompt", "")
    if not goal:
        out.append(Flag(ERROR, "Goal", "empty", "goal-specific", "missing"))
    else:
        ns = sentences(goal)
        if ns > 4 and slot.get("prompt_style") != "Explicit spec":
            out.append(Flag(WARN, "Goal", f"{ns} sentences; aim for 1-3 (more is OK only to add context)",
                            "goal-specific", "length"))
        if words(goal) < 15:
            out.append(Flag(WARN, "Goal", "very short; say what a proper solution must do and what doesn't count",
                            "goal-specific", "length"))
        if not re.search(r"\b(not|no|without|never|doesn't|don't|isn't)\b", goal, re.I):
            out.append(Flag(WARN, "Goal", "no must-nots (shortcuts that don't count, e.g. loosening tests)",
                            "goal-must-nots", "wording"))
    if not dps:
        out.append(Flag(ERROR, "Decision Points", "empty", "dp-count", "missing"))
    else:
        # Lexical counters can't tell whether the decision points exist (GPT review 2026-10-31): they only
        # point at a structure worth a look, so they never block.
        b, w = len(BETTER.findall(dps)), len(WORSE.findall(dps))
        if min(b, w) < 2:
            out.append(Flag(WARN, "Decision Points", f"found {b} better and {w} worse markers (better/strong/good, "
                                                     "worse/weak/bad); check it reads as 2-3 better-vs-worse points",
                            "dp-better-worse", "structure"))
        if max(b, w) > 4:
            out.append(Flag(WARN, "Decision Points", "more than 3 points; keep 2-3", "dp-count", "structure"))
        if words(dps) < 15:
            out.append(Flag(ERROR, "Decision Points", "under 15 words (Studio minimum)", "dp-count", "missing"))
    if not cb:
        out.append(Flag(ERROR, "Difficulty & Codebase", "empty", "cb-length", "missing"))
    else:
        if words(cb) < 15:
            out.append(Flag(ERROR, "Difficulty & Codebase", "under 15 words (Studio minimum)", "cb-length", "missing"))
        ns = sentences(cb)
        if ns < 3 or ns > 6:
            out.append(Flag(WARN, "Difficulty & Codebase", f"{ns} sentences; aim for 3-5", "cb-length", "length"))
        if GENERIC.search(cb):
            q = _hit(GENERIC, cb)
            out.append(Flag(WARN, "Difficulty & Codebase", f"generic phrase {GENERIC.search(cb).group(0)!r}; name the "
                                                           "mechanism", "cb-mechanism", "wording", q))
    if not notes:
        out.append(Flag(ERROR, "Validation Notes", "empty", "vn-content", "missing"))
    elif sentences(notes) < 2:
        out.append(Flag(WARN, "Validation Notes", "say where it struggled, what went wrong, and how much you steered",
                        "vn-content", "length"))
    if slot.get("action") == "Operate" and not sec.get("Initial State / Setup"):
        out.append(Flag(WARN, "Initial State", "Operate tasks should describe how to reach the starting state",
                        None, "missing"))
    # The prompt must not repeat the private fields.
    shared = ngrams(prompt) & (ngrams(goal) | ngrams(dps))
    if shared:
        out.append(Flag(WARN, "Opening Prompt", f"repeats private goal/decision-point wording: {sorted(shared)[0]!r}",
                        "prompt-no-leak", "leak"))
    for name, text in (("Opening Prompt", prompt), ("Goal", goal), ("Decision Points", dps),
                       ("Difficulty & Codebase", cb), ("Validation Notes", notes)):
        hits = sorted({m.group(0).lower() for m in AI_TELLS.finditer(text)})
        if hits:
            out.append(Flag(WARN, name, f"words that read as AI-written: {', '.join(hits)}", "llm-ai-detection",
                            "wording", _hit(AI_TELLS, text)))
        if "—" in text:
            out.append(Flag(WARN, name, "em dashes read as AI-written", "llm-ai-detection", "wording",
                            _sentence_with(text, "—", 60)))
    return out


def check_ratings(ratings: dict, slot: dict) -> list[tuple]:
    out = []
    one = (ratings.get("one-shot") or "").strip().lower()
    ts = re.match(r"\s*(\d)", ratings.get("task success") or "")
    st = re.match(r"\s*(\d)", ratings.get("steering") or "")
    ts, st = (int(ts.group(1)) if ts else None), (int(st.group(1)) if st else None)
    R = "Ratings"
    if one not in ("yes", "no"):
        out.append(Flag(ERROR, R, "One-shot must be Yes or No", "rate-one-shot", "missing"))
    elif one == "yes":
        out.append(Flag(ERROR, R, "One-shot = Yes: the task is too easy. Drop it and log it in the Discarded Run Log",
                        "rate-one-shot", "ratings"))
    if ts is None:
        out.append(Flag(ERROR, R, "Task Success (1-5) missing", "rate-task-success", "missing"))
        return out
    ent = slot.get("codebase_type", "").startswith("Enterprise")
    if ts == 5:
        out.append(Flag(ERROR, R, "Task Success 5 is always rejected", "reject-ent-4-any-5", "ratings"))
    elif ent and ts == 4:
        out.append(Flag(ERROR, R, "Enterprise Task Success 4 is rejected", "reject-ent-4-any-5", "ratings"))
    elif ent and ts == 3 and (st is None or st > 2):
        out.append(Flag(ERROR, R, "Enterprise Task Success 3 passes only with Steering 1-2", "pass-enterprise-3",
                        "ratings"))
    elif not ent and ts == 4 and (st is None or st > 2):
        out.append(Flag(ERROR, R, "Personal-scale Task Success 4 passes only with Steering 1-2", "pass-personal-4",
                        "ratings"))
    needs_steering = (ent and ts == 3) or (not ent and ts in (3, 4))
    if needs_steering and st is None:
        out.append(Flag(ERROR, R, "Steering is required for this Task Success", "rate-steering-required", "missing"))
    return out


def check_repo(repo: dict) -> list[tuple]:
    out = []
    url = repo.get("pinned_url") or ""
    if not re.fullmatch(r"https://github\.com/[^/\s]+/[^/\s]+/tree/[0-9a-f]{40}/?", url):
        out.append(Flag(ERROR, "Repo", f"URL is not pinned to a 40-char commit: {url!r}", "repo-pinned", "repo"))
    if (repo.get("full_name") or "").lower() in config.SWE_BENCH_REPOS:
        out.append(Flag(ERROR, "Repo", "SWE-bench-Verified repo", "repo-avoid", "repo"))
    if not repo.get("language"):
        out.append(Flag(ERROR, "Repo", "primary language not set", None, "repo"))
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


# --- what Studio saved ----------------------------------------------------------------------------------
# Studio's copy of each field lives in tasks/<t>/app/studio-fields.json (refreshed by the app and by
# `johto pull`). A paste into Studio can lose its end: t4sk0003's Validation Notes were saved ending
# "...when I ship the second half it fo" and the reviewer flagged it. These checks compare what Studio holds with
# the words around it and with the local copy in submission.md.

# Prose fields checked for a cut-off end: Studio field id -> label.
SAVED_PROSE = {
    "field_high_level_goal": "Goal",
    "field_rationale_difficulty": "Decision Points",
    "field_codebase_overview": "Difficulty & Codebase",
    "field_self_check": "Validation Notes",
    "field_writer_submission_notes": "Notes for the Reviewer",
}
# Fields compared with the local copy: the prose fields plus the prompt and Initial State.
SAVED_FIELDS = {"field_opening_prompt": "Opening Prompt", **SAVED_PROSE, "field_initial_state": "Initial State"}
SHORTER = 0.85  # Studio's text under 85% of the local copy's length is "clearly shorter"

# Short words a field can end on. A short last word outside this list (and not used anywhere else in the
# task) reads as a cut: "it fo".
SHORT_WORDS = frozenset("""
    a i am an as at be by do go he if in is it me my no of oh ok on or so to up us we
    add ago aim all and any are ask bad big bit bug but can did due end etc far few fix for get got had has
    her him his how its job key let log lot may new nor not now off old one our out own per put ran raw red
    row run saw say see set she sum tab ten the too top try two use via was way who why yes yet you
    cut fit hid hit led lit met sat won
    api app cli css csv db git npm php sql ui url ux vs lol tbh imo btw pls thx
""".split())
# Endings that make a longer word from a whole one: "fix" with "fixed" elsewhere is not a cut.
SUFFIXES = frozenset("s es ed d ing er ers est ly y al ally ment ments ness ion ions ation ations able ive ity".split())
_LAST_WORD = re.compile(r"(?:^|\s)([A-Za-z]+)$")
DICT_PATH = Path("/usr/share/dict/words")


def _norm(v) -> str:
    if not isinstance(v, str):
        return ""
    return "\n".join(line.rstrip() for line in v.replace("\r\n", "\n").strip().split("\n"))


def _vocab(texts) -> set[str]:
    return {w.lower() for t in texts for w in re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)*", t or "")}


def _tail(s: str, n: int = 7) -> str:
    return " ".join(s.split()[-n:])


def _tail_text(s: str, n: int = 7) -> str:
    """The last `n` words of `s` exactly as written (line breaks kept), at most 120 characters."""
    starts = [m.start() for m in re.finditer(r"\S+", s or "")]
    return (s[starts[-n]:] if len(starts) >= n else (s or "")).strip()[-120:]


def _extends(low: str, w: str, seen: set[str]) -> bool:
    """True when `w` continues `low` past a whole-word ending: not an inflection ("fix" to "fixed",
    "split" to "splitting") and not a compound ("with" to "without")."""
    if len(w) <= len(low) or not w.startswith(low):
        return False
    rest = w[len(low):]
    if rest in SUFFIXES or (rest[0] == low[-1] and rest[1:] in SUFFIXES):
        return False
    return not (len(rest) >= 3 and (rest in SHORT_WORDS or rest in seen))


def cut_off_word(text: str, others=(), known: frozenset | None = None) -> str | None:
    """The last word of `text` when it looks like the start of a word that got cut off, else None.

    Text ending in punctuation, a digit, a symbol or code is fine, and so is a last bullet or line that ends
    on a whole word with no period. A bare last word that isn't used anywhere else in the task (`others`
    plus the rest of this text) counts as cut when it is short, lowercase and not a common word ("it fo"),
    or when it is the start of a longer word used elsewhere ("permissi" before "permission"). `known` is a
    set of real words of four letters or more (default: the system word list, when there is one) that are
    never counted as cut; for shorter words the list holds too many rare ones ("fo", "tha")."""
    known = _dictionary() if known is None else known
    t = (text or "").rstrip()
    m = _LAST_WORD.search(t)
    if not m:
        return None
    tok = m.group(1)
    low = tok.lower()
    if low in SHORT_WORDS or (tok.isupper() and tok != low):
        return None  # a common word, an acronym (API, CSV) or a label like "reviewer B"
    seen = _vocab([t[:m.start(1)], *others])
    if low in seen:
        return None
    if tok == low and len(low) <= 3:
        return tok
    if len(low) >= 4 and low in known:
        return None
    return tok if any(_extends(low, w, seen) for w in seen) else None


@functools.lru_cache(maxsize=1)
def _dictionary() -> frozenset:
    """Lowercase entries of the system word list (macOS /usr/share/dict/words); empty where there is none."""
    try:
        return frozenset(w for w in DICT_PATH.read_text(errors="replace").split() if w.islower())
    except OSError:
        return frozenset()


def _last_pulled_chars(d: Path) -> dict:
    """{field id: chars} of the last text johto pulled from Studio per field (app/authorship.jsonl)."""
    heading_field = {h: f for h, f in workspace.SECTIONS.items()}
    out = {}
    try:
        lines = (d / "app" / "authorship.jsonl").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        f = heading_field.get(ev.get("field"))
        if f and ev.get("event") == "pulled_from_studio" and isinstance(ev.get("chars"), int):
            out[f] = ev["chars"]
    return out


def check_saved_fields(task_dir: Path) -> list[tuple]:
    """Problems with what Studio saved: a prose field that ends mid-word, or a field clearly shorter than the
    local copy in submission.md. [] when there is no Studio snapshot (app/studio-fields.json)."""
    d = Path(task_dir)
    try:
        snap = json.loads((d / "app" / "studio-fields.json").read_text())
    except (OSError, ValueError):
        return []
    studio = {f: _norm(v) for f, v in (snap.get("fields") or {}).items() if f in SAVED_FIELDS}
    try:
        secs = workspace.parse_submission(d)["sections"]
    except OSError:
        secs = {}
    local = {f: _norm(secs.get(h)) for h, f in workspace.SECTIONS.items() if f in SAVED_FIELDS}
    pulled = _last_pulled_chars(d)
    out, cut = [], set()
    for f, label in SAVED_FIELDS.items():
        s, loc = studio.get(f, ""), local.get(f, "")
        if not loc or s == loc:
            continue
        ws, wl = words(s), words(loc)
        if s and loc.startswith(s) and s[-1].isalpha() and loc[len(s)].isalpha():
            cut.add(f)
            out.append(Flag(ERROR, label, f"Studio's copy stops mid-word at \"...{_tail(s)}\"; your local copy goes "
                                          "on. Save the field to Studio again.", "submit-saved-whole", "cut",
                            _tail_text(s)))
        elif len(s) >= SHORTER * len(loc):
            continue
        elif not s:
            out.append(Flag(WARN, label, f"Studio has none of your local copy ({wl} words). Save the field to Studio.",
                            "submit-saved-whole", "unsaved"))
        elif loc.startswith(s):
            out.append(Flag(WARN, label, f"Studio's copy stops early at \"...{_tail(s)}\"; your local copy has "
                                         f"{wl - ws} more words. Save the field to Studio again.", "submit-saved-whole",
                            "unsaved", _tail_text(s)))
        elif abs(pulled.get(f, -1) - len(loc)) <= max(2, len(loc) // 100):
            out.append(Flag(WARN, label, f"Studio has less than your local copy ({ws} vs {wl} words), and it changed "
                                         "in Studio after johto last pulled it. If you cut it on purpose, run johto "
                                         "pull; if not, save the field to Studio again.", "submit-saved-whole",
                            "unsaved"))
        else:
            out.append(Flag(WARN, label, f"Studio has less than your local copy ({ws} vs {wl} words). Save the field "
                                         "to Studio.", "submit-saved-whole", "unsaved"))
    texts = [*studio.values(), *local.values()]
    for f, label in SAVED_PROSE.items():
        s = studio.get(f, "")
        if f in cut or not s:
            continue
        others = [t for t in texts if t not in (s, local.get(f, ""))]
        if cut_off_word(s, others):
            out.append(Flag(WARN, label, f"Studio's text looks cut off: it ends \"...{_tail(s)}\". Check the end of "
                                         "the field in Studio and paste the rest.", "submit-saved-whole", "cut",
                            _tail_text(s)))
    return out
