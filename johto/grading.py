"""The grading table: one row per finding about the run Austin submits, with his scope and marks beside it.

Rows come from saved outside reviews (the `findings` block GPT Pro ends each reply with; older reviews need
`refresh --extract`), the dossier's "where models fail" hazards for the task's opportunity (as unscoped
candidates), and run facts that contradict a claim Claude made (test commands as detected, not measured).
Three judgments stay apart on every row: scope (Austin's call), the run's result, and how it is known.

tasks/<t>/grading.json holds the rows. Refresh never deletes a row and never touches scope or marks; a later
review of the same chat updates the row its finding ID already has. Austin marks rows for the goal (G), a
decision point (1-3) and the validation notes (N); nothing here writes text for those fields. AI text lives
only in finding, limits and verify.summary, which the overlap guard (johto/web/guard/ledger.py) reads.

`verify` (opt-in, one row) has headless Claude reproduce a finding with a scratch test in a temporary
checkout of the run's final code, outside the lane, and saves the report under checks/.
"""
from __future__ import annotations

import bisect
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import config, stages, transcript, workspace

FILE = "grading.json"
VERSION = 1
SCOPES = ("explicit", "necessary", "uncertain", "outside")
RESULTS = ("credit", "partial", "missed", "unknown")
BASES = ("executed", "isolated", "traced", "claimed", "none")   # strongest first
KINDS = ("defect", "credit", "requirement", "claim", "limitation")
STATUSES = ("new", "unchanged", "resolved", "worsened", "withdrawn")
SEVERITIES = ("high", "medium", "low")
VERIFY_STATES = ("none", "confirmed", "refuted", "inconclusive")
FIELDS = ("goal", "decision_points", "validation_notes")
INACTIVE = ("withdrawn", "superseded")

DOSSIER_LIMITS = "Dossier hypothesis: check it against the prompt and the app"
FACTS_LIMITS = ("Test runs are detected from the command line (the program each shell segment runs), not from "
                "their output; exit status is the tool result's, and a pipe reports only its last command.")
EXTRACT_MODEL = os.environ.get("JOHTO_EXTRACT_MODEL") or "sonnet"
VERIFY_MODEL = os.environ.get("JOHTO_VERIFY_MODEL") or "opus"
VERIFY_TIMEOUT_S = 40 * 60


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()   # "not given" for mark(); None is a real value there (no decision point, no scope)


class GradingError(ValueError):
    """A plain-words problem with a grading command: an unknown row, a bad value, a run that can't be checked."""


# --- Storage -------------------------------------------------------------------------------------------

def _now() -> str:
    return stages.now_iso()


def _empty() -> dict:
    return {"version": VERSION, "run": None, "updated_at": None, "rows": []}


def load(task_dir: Path) -> dict:
    """grading.json, or an empty table. A damaged file reads as empty rather than failing the caller."""
    try:
        data = json.loads((Path(task_dir) / FILE).read_text())
    except (FileNotFoundError, ValueError):
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        return _empty()
    data["rows"] = [r for r in data["rows"] if isinstance(r, dict) and r.get("id")]
    return data


def save(task_dir: Path, data: dict) -> None:
    p = Path(task_dir) / FILE
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, p)


@contextlib.contextmanager
def _locked(task_dir: Path):
    """The CLI, the web app and the collect hook all change grading.json: one at a time."""
    with open(Path(task_dir) / ".grading.lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _rel(task_dir: Path, p) -> str:
    try:
        return str(Path(p).resolve().relative_to(Path(task_dir).resolve()))
    except ValueError:
        return str(p)


# --- Parsing the findings block (GPT Pro's reply format) ------------------------------------------------

_FENCE = re.compile(r"^(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^\n]*)\n(?P<body>.*?)(?:^(?P=fence)[ \t]*$|\Z)",
                    re.S | re.M)
_REF = re.compile(r"[\w./@-]+\.[A-Za-z0-9]{1,8}:\d+(?:\s*[-–]\s*:?\d+)?|\bturn \d+\b")
_FID = re.compile(r"^\s*[Ff]\s*-?\s*(\d+)\s*$|^\s*(\d+)\s*$")

_SYN = {
    "status": {"same": "unchanged", "carried": "unchanged", "carried forward": "unchanged", "fixed": "resolved",
               "regressed": "worsened", "worse": "worsened", "retracted": "withdrawn", "dropped": "withdrawn"},
    "severity": {"critical": "high", "major": "high", "moderate": "medium", "med": "medium", "minor": "low"},
    "result": {"pass": "credit", "passed": "credit", "correct": "credit", "supported": "credit", "done": "credit",
               "fail": "missed", "failed": "missed", "miss": "missed", "omitted": "missed", "partly": "partial"},
    "kind": {"miss": "defect", "bug": "defect", "omission": "defect", "issue": "defect", "risk": "defect",
             "strength": "credit", "unverified claim": "claim", "limit": "limitation",
             # the pack asks for "behavior" (a code fact); the table keeps its older name for it
             "behavior": "requirement", "behaviour": "requirement"},
}


def _enum(field: str, value, allowed: tuple) -> str | None:
    if not isinstance(value, str):
        return None
    v = " ".join(value.lower().replace("_", " ").split()).strip(" .*`")
    if v in allowed:
        return v
    if v in _SYN.get(field, {}):
        return _SYN[field][v]
    if field == "basis":   # "isolated execution" is isolated, "source tracing" is traced
        for word, out in (("isolat", "isolated"), ("trac", "traced"), ("source", "traced"), ("static", "traced"),
                          ("claim", "claimed"), ("unverified", "claimed"), ("assert", "claimed"),
                          ("execut", "executed"), ("ran ", "executed"), ("none", "none")):
            if word in v + " ":
                return out
        return None
    for a in allowed:   # "missed (turn 1)", "high severity"
        if re.search(rf"\b{a}\b", v):
            return a
    return None


def _fid(value) -> str | None:
    m = _FID.match(str(value or ""))
    return f"F{int(m.group(1) or m.group(2))}" if m else None


def _refs(value) -> list[str]:
    if isinstance(value, str):
        return _REF.findall(value) or ([value.strip()] if value.strip() else [])
    if isinstance(value, list):
        return [" ".join(str(x).split()) for x in value if str(x).strip()]
    return []


def normalize(f: dict) -> dict | None:
    """One finding in the contract's shape, values coerced to the allowed words. None if it has neither an
    ID nor a finding (a follow-up may give only an ID and a status: that is kept)."""
    if not isinstance(f, dict):
        return None
    text = " ".join(str(f.get("finding") or f.get("behavior") or f.get("what") or "").split()) or None
    fid = _fid(f.get("id"))
    if not text and not fid:
        return None
    sup = f.get("supersedes")
    return {"id": fid, "finding": text, "kind": _enum("kind", f.get("kind"), KINDS),
            "status": _enum("status", f.get("status"), STATUSES),
            "severity": _enum("severity", f.get("severity"), SEVERITIES),
            "result": _enum("result", f.get("result") or f.get("run_result"), RESULTS),
            "basis": _enum("basis", f.get("basis") or f.get("how_known"), BASES),
            "refs": _refs(f.get("refs") or f.get("evidence")),
            "limits": " ".join(str(f.get("limits") or "").split()) or None,
            "scope": _enum("scope", f.get("scope"), SCOPES),
            "supersedes": _fid(sup) if sup else None}


def _repairs(s: str) -> list[str]:
    fixed = re.sub(r",\s*([\]}])", r"\1", re.sub(r"^\s*//.*$", "", s, flags=re.M))
    out = [s, fixed, fixed.replace("“", '"').replace("”", '"')]
    if fixed.lstrip().startswith("{") and re.search(r"}\s*,?\s*{", fixed):   # objects with no brackets around them
        out.append("[" + re.sub(r"}\s*,?\s*{", "},{", fixed) + "]")
    return out


def _objects(s: str) -> list[str]:
    """Every outermost {...} in s, scanned with string awareness, for a block whose array is broken."""
    out, depth, start, in_str, esc = [], 0, None, False, False
    for i, ch in enumerate(s):
        if in_str:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc:
                in_str = False
            elif ch != "\\":
                esc = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                out.append(s[start:i + 1])
    return out


def _loads(body: str) -> list | None:
    for attempt in _repairs(body.strip()):
        try:
            v = json.loads(attempt)
        except ValueError:
            continue
        if isinstance(v, dict):
            v = v.get("findings") if isinstance(v.get("findings"), list) else [v]
        if isinstance(v, list):
            return v
    got = []
    for chunk in _objects(body):
        for attempt in _repairs(chunk):
            try:
                got.append(json.loads(attempt))
                break
            except ValueError:
                continue
    return got or None


def _table(text: str) -> list[dict] | None:
    """The "## Findings" markdown table, for a reply whose JSON block is missing or unreadable."""
    m = re.search(r"^#{1,4}\s*Findings\b[^\n]*\n", text, re.M | re.I)
    if not m:
        return None
    lines, rows, header = text[m.end():].splitlines(), [], None
    for line in lines:
        s = line.strip()
        if not s.startswith("|"):
            if header is not None and s:
                break
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        row = {}
        for name, cell in zip(header, cells, strict=False):
            key = next((k for k, words in (("id", ("id",)), ("finding", ("finding", "behavior")),
                                            ("status", ("status",)), ("severity", ("severity",)),
                                            ("result", ("result",)), ("basis", ("known", "basis")),
                                            ("evidence", ("evidence", "refs")))
                        if any(w == name or (w != "id" and w in name) for w in words)), None)
            if key:
                row[key] = cell.replace("`", "")
        if _fid(row.get("id")):
            rows.append(row)
    return rows or None


def parse_findings(text: str) -> list[dict] | None:
    """The findings in a review: its last ```findings block, else its "## Findings" table. None when the
    review has neither (older reviews); a block that is partly broken gives the findings that still read."""
    raw = None
    for m in _FENCE.finditer(text or ""):
        if re.search(r"\bfindings\b", m.group("info"), re.I):
            got = _loads(m.group("body"))
            if got is not None:
                raw = got
    if raw is None:
        raw = _table(text or "")
    if raw is None:
        return None
    return [n for n in (normalize(f) for f in raw) if n]


# --- Which run ------------------------------------------------------------------------------------------

def selected_run(task_dir: Path, meta: dict | None = None) -> str | None:
    """The run the submission uses: actions.submission_run when it exists (the one shared rule), else
    task.json's submission_run, else the latest collected validation run, else the latest run."""
    d = Path(task_dir)
    meta = meta if meta is not None else workspace.load(d)
    from . import actions
    fn = getattr(actions, "submission_run", None)
    if fn is not None:
        got = fn(meta, d)
        if got:
            return got
    want = meta.get("submission_run")
    if want and (d / "runs" / want).is_dir():
        return want
    runs = [r for r in meta.get("runs", []) if (d / (r.get("dir") or "-")).is_dir()]
    done = [r for r in runs if r.get("kind") == "validate" and (r.get("collected_at") or r.get("transcript"))]
    for group in (done, [r for r in runs if r.get("kind") == "validate"], runs):
        if group:
            return group[-1]["dir"].removeprefix("runs/")
    return None


def _run_rec(meta: dict, run: str | None) -> dict:
    return next((r for r in meta.get("runs", []) if r.get("dir") == f"runs/{run}"), {}) if run else {}


# --- Run evidence (shared with the writing kit) ----------------------------------------------------------

PASS_CLAIM = re.compile(
    r"\b(all (of )?(the )?(\d+ )?tests (now |still )?pass(ed|es|ing)?|(the )?(test )?suite (now |still )?pass(es|ed)?|"
    r"\d+ (\w+ ){0,2}tests? (now |still )?pass(ed|es|ing)?|tests (are )?(now |still )?(all )?pass(ing|ed)?)\b", re.I)
_EXIT = re.compile(r"Exit code (\d+)")
# A test runner as the program a shell segment runs (after env assignments and timeout), so `cat phpunit.xml`
# isn't a test run and `npx mocha` is: transcript.TEST_CMD matches anywhere in the line and misses mocha.
_SEGMENT = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
_LEAD = re.compile(r"^(?:\w+=\S*\s+|(?:timeout|time|nice|env)\s+(?:-\S+\s+)*(?:\d+[smh]?\s+)?)+")
_RUNNER = re.compile(
    r"^(?:(?:npx|bunx|pnpx|uvx|uv run|poetry run|pipenv run|bundle exec)\s+)?(?:\S*/)?(?:"
    r"(?:pytest|py\.test|mocha|jest|vitest|phpunit|pest|rspec|ava|karma|tox|nox|ctest)\b"
    r"|playwright\s+test\b|cypress\s+run\b"
    r"|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b"
    r"|(?:go|cargo|dotnet|deno|mix|swift)\s+test\b"
    r"|(?:mvn|gradle|gradlew|make)\s+(?:\S+\s+)*?(?:test|check)\b"
    r"|python[\d.]*\s+(?:-\S+\s+)*(?:-m\s+(?:pytest|unittest|nose2?)\b|manage\.py\s+test\b|\S*runtests\S*)"
    r"|php\s+(?:artisan\s+test\b|\S*(?:phpunit|pest)\b)"
    r"|(?:rails|rake)\s+test\b|node\s+--test\b)")


# Lines a test runner prints when tests fail or can't run. A pipe (`| tail`, `| grep`) hides the runner's exit
# code, so these are what tells a piped run apart.
_FAIL_OUT = re.compile(r"^.*?(\bFAILED\b(?: \((?:failures|errors)=\d+[^)]*\))?|\b[1-9]\d* (?:failed|failing|errors?)\b|"
                       r"\bTraceback \(most recent call last\)|\b(?:ModuleNotFoundError|ImportError|SyntaxError)\b|"
                       r"^(?:E\s+)?\w*Error:|\bTests?: +[1-9]\d* failed|\bFAIL\b).*$", re.M)


def output_failure(text: str) -> str | None:
    """The first line of a test run's output that shows failure (shortened), else None."""
    m = _FAIL_OUT.search(text or "")
    if not m:
        return None
    line = " ".join(m.group(0).split())
    return line if len(line) <= 80 else line[:79] + "…"


def runs_tests(cmd: str) -> bool:
    """Does this shell line run a test runner? Detected from the command only, not from its output."""
    return any(_RUNNER.match(_LEAD.sub("", seg)) for seg in _SEGMENT.split(cmd or "") if seg)


def _entries(path: Path) -> list[dict]:
    out = []
    for line in Path(path).read_text(errors="ignore").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def _turn_finder(entries: list[dict]):
    """entry -> Austin's turn number (1 = the opening prompt), counted the way `johto time` counts turns."""
    starts = [ts for ts, _ in transcript.counted_prompts(entries)]

    def turn(e: dict) -> int | None:
        ts = transcript._ts(e.get("timestamp"))
        if ts is None or not starts:
            return None
        return bisect.bisect_right(starts, ts) or None
    return turn


def _result_text(c) -> str:
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
    return str(c or "")


def scan(entries: list[dict]) -> dict:
    """Detected test commands with their tool result, and Claude's pass claims, in transcript order."""
    turn_of = _turn_finder(entries)
    calls, tests, claims = {}, [], []
    for i, e in enumerate(entries):
        if e.get("isSidechain"):
            continue
        for x in transcript._content(e) or []:
            if not isinstance(x, dict):
                continue
            if e.get("type") == "assistant" and x.get("type") == "tool_use":
                inp = x.get("input") or {}
                cmd = inp.get("command") if x.get("name") == "Bash" else None
                if isinstance(cmd, str) and runs_tests(cmd):
                    t = {"i": i, "turn": turn_of(e), "cmd": " ".join(cmd.split())[:300], "short": _short_cmd(cmd),
                         "exit": None, "piped": bool(re.search(r"(?<!\|)\|(?!\|)", cmd))}
                    calls[x.get("id")] = t
                    tests.append(t)
            elif e.get("type") == "assistant" and x.get("type") == "text":
                text = x.get("text") or ""
                for m in PASS_CLAIM.finditer(text):
                    if not re.search(r"\b(not|n't|never)\b[^.]{0,12}$", text[max(0, m.start() - 14):m.start()], re.I):
                        claims.append({"i": i, "turn": turn_of(e), "quote": m.group(0)})
            elif x.get("type") == "tool_result" and x.get("tool_use_id") in calls:
                t = calls[x["tool_use_id"]]
                text = _result_text(x.get("content"))
                code = _EXIT.search(text[:300])
                t["exit"] = int(code.group(1)) if code else (1 if x.get("is_error") else 0)
                t["output_failed"] = output_failure(text)
    return {"tests": tests, "claims": claims}


def run_evidence(task_dir: Path, run: str | None) -> dict:
    """Facts about one run from its collected transcript and diff (the task folder's own copy): Claude's
    minutes and turns, files edited, the diff stat, detected test commands and pass claims."""
    from .web.domain import facts
    d = Path(task_dir)
    meta = workspace.load(d)
    rec = _run_rec(meta, run)
    out = {"run": run, "host": rec.get("host"), "collected_at": rec.get("collected_at"), "transcript": None,
           "turns": [], "working_minutes": None, "background_minutes": None, "files_edited": [], "diffstat": [],
           "tests": [], "claims": [], "first_prompt": None, "diff_path": None}
    if not run:
        return out
    run_dir = d / "runs" / run
    diff = run_dir / "prework_output" / "final.diff"
    if diff.exists():
        out["diff_path"] = _rel(d, diff)
        out["diffstat"] = facts.diffstat(diff.read_text(errors="replace"))
    logs = transcript.find(run_dir)
    if not logs:
        return out
    tpath = logs[-1]
    es = _entries(tpath)
    sm = transcript.summarize(tpath)
    sc = scan(es)
    edits = facts.reduce(es)["edits"]   # repo files only; Claude's scratch files outside the repo don't count
    files = list(dict.fromkeys(ed["path"] for ed in edits))
    out.update(transcript=_rel(d, tpath), turns=sm["turns"], working_minutes=sm["working_minutes"],
               background_minutes=sm["background_minutes"], files_edited=files, tests=sc["tests"],
               claims=sc["claims"], edits=edits, first_prompt=sm.get("first_prompt"))
    return out


# --- Rows from each source ------------------------------------------------------------------------------

def _review_obs(task_dir: Path, log, extract: bool, runner) -> tuple[list[dict], list[str]]:
    """Observations from saved reviews, oldest first: (obs list, names of reviews with no findings to read)."""
    from . import reviews
    out, missing = [], []
    for rv in reviews.list_reviews(task_dir):
        path = Path(rv["path"])
        side = path.with_suffix(".findings.json")
        found, extracted = parse_findings(reviews.body(path)), False
        if found is None and side.exists():
            try:
                found = [n for n in (normalize(f) for f in json.loads(side.read_text()).get("findings", [])) if n]
                extracted = True
            except (OSError, ValueError, AttributeError):
                found = None
        if found is None and extract:
            found = extract_findings(task_dir, path, log=log, runner=runner)
            extracted = found is not None
        if found is None:
            missing.append(rv["name"])
            continue
        chat = rv.get("chat_url") or f"run:{rv.get('run')}"
        for f in found:
            fid = f["id"] or "F?" + hashlib.sha1((f["finding"] or "").encode()).hexdigest()[:6]
            key = f"extract:{rv['name']}#{fid}" if extracted else f"review:{chat}#{fid}"
            src = {"type": "review", "ref": f"{reviews.DIR}/{rv['name']}", "finding_id": f["id"], "at": rv.get("saved_at"),
                   "chat": rv.get("chat_url")}
            if extracted:
                src["extracted"] = True
            out.append({**f, "key": key, "chat": chat, "source": src, "run": rv.get("run"), "turn": rv.get("turn")})
    return out, missing


def _dossier_link(task_dir: Path, meta: dict) -> tuple[str, str] | None:
    """(dossier id, opportunity id) from task.json, else from brief.md's dossier section."""
    link = meta.get("dossier") or {}
    if link.get("id") and link.get("opportunity_id"):
        return link["id"], link["opportunity_id"]
    try:
        brief = (Path(task_dir) / "brief.md").read_text()
    except OSError:
        return None
    m = re.search(r"^Repo page: /repos/(\S+)", brief, re.M)
    t = re.search(r"^## Dossier.*?^### (.+?)(?: \([^)]*\))?\s*$", brief, re.M | re.S)
    if not (m and t):
        return None
    try:
        from . import actions
        doc, _ = actions.load_opportunity(m.group(1), None)
    except Exception:
        return None
    from .dossier import schema
    opp = next((o for o in schema.opportunities(doc) if o.get("title") == t.group(1).strip()), None)
    return (m.group(1), opp["id"]) if opp else None


def dossier_opportunity(task_dir: Path, meta: dict | None = None) -> tuple[dict, dict] | None:
    """(dossier, opportunity) this task came from, or None."""
    meta = meta if meta is not None else workspace.load(Path(task_dir))
    link = _dossier_link(task_dir, meta)
    if not link:
        return None
    from . import actions
    try:
        doc, opp = actions.load_opportunity(*link)
    except Exception:
        return None
    return (doc, opp) if opp else None


_FILEISH = re.compile(r"[\w./-]*\w\.(?:py|php|ts|tsx|js|jsx|go|rs|rb|java|kt|cs|yml|yaml|json|sql|md)(?::\d+(?:-\d+)?)?\b")


def _dossier_obs(task_dir: Path, meta: dict) -> list[dict]:
    got = dossier_opportunity(task_dir, meta)
    if not got:
        return []
    doc, opp = got
    out = []
    for i, text in enumerate(opp.get("where_models_fail") or []):
        text = " ".join(str(text).split())
        h = hashlib.sha1(text.lower().encode()).hexdigest()[:10]
        out.append({"key": f"dossier:{doc.get('id')}#{h}", "id": None, "finding": text, "kind": "requirement",
                    "status": None, "severity": None, "result": None, "basis": "none",
                    "refs": list(dict.fromkeys(_FILEISH.findall(text))), "limits": DOSSIER_LIMITS, "scope": None,
                    "supersedes": None, "run": None, "turn": None,
                    "source": {"type": "dossier", "ref": f"data/dossiers/{doc.get('id')}.json#{opp.get('id')}",
                               "finding_id": f"{opp.get('id')}.wmf{i + 1}", "at": doc.get("generated_at")}})
    return out


def _short_cmd(cmd: str) -> str:
    """The test-runner part of a shell line: 'npx mocha --reporter spec' out of 'timeout 300 npx mocha ... | tail'."""
    for seg in _SEGMENT.split(cmd or ""):
        seg = _LEAD.sub("", seg)
        if _RUNNER.match(seg):
            return re.sub(r"\s*\d*>{1,2}&?\s*\S+", "", seg).strip()[:80]
    return " ".join((cmd or "").split())[:80]


def claim_problems(ev: dict) -> list[dict]:
    """Claude's pass claims that the detected test commands don't back: the last claim of each turn."""
    last = {}
    for c in ev.get("claims") or []:
        last[c["turn"]] = c
    edits = ev.get("edits") or []
    out = []
    for turn, c in sorted(last.items(), key=lambda kv: kv[1]["i"]):
        before = [t for t in ev["tests"] if t["i"] < c["i"]]
        ok = [t for t in before if t["exit"] == 0]
        said = f'Claude said "{c["quote"]}" (turn {turn})'
        if not before:
            text, sev = f"{said}; no test command was detected before it.", "low"
        elif before[-1]["exit"] not in (0, None):
            text, sev = (f"{said}; the last test command detected before it, {before[-1]['short']}, "
                         f"ended with exit {before[-1]['exit']}."), "medium"
        elif not ok:
            text, sev = f"{said}; no detected test command before it shows exit 0.", "low"
        elif any(ok[-1]["i"] < e["i"] < c["i"] for e in edits):
            text, sev = f"{said}; files were edited after the last detected test command with exit 0.", "low"
        else:
            continue
        out.append({"turn": turn, "text": text, "severity": sev})
    return out


def _facts_obs(task_dir: Path, run: str | None, ev: dict | None = None) -> list[dict]:
    if not run:
        return []
    ev = ev or run_evidence(task_dir, run)
    if not ev["transcript"]:
        return []
    out = []
    for p in claim_problems(ev):
        out.append({"key": f"facts:{run}#claim-t{p['turn']}", "id": None, "finding": p["text"], "kind": "claim",
                    "status": None, "severity": p["severity"], "result": "unknown", "basis": "claimed",
                    "refs": [f"turn {p['turn']}"], "limits": FACTS_LIMITS, "scope": None, "supersedes": None,
                    "run": run, "turn": p["turn"],
                    "source": {"type": "facts", "ref": ev["transcript"], "finding_id": f"claim-t{p['turn']}",
                               "at": ev.get("collected_at")}})
    return out


# --- Building rows -------------------------------------------------------------------------------------

_STOP = set("a an the of to in on at for and or by with from as is are was were be been it its this that these "
            "those into than then so if when which who what while can could would will may might only also "
            "just one each per".split())
_NEG = {"not", "no", "never", "without", "missing", "fails", "lost", "loses", "ignores", "omits"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]+", (text or "").lower()) if w not in _STOP and len(w) > 1}


def _paths(refs: list[str]) -> set[str]:
    return {r.split(":")[0].rsplit("/", 1)[-1] for r in refs or [] if ":" in r and not r.startswith("turn")}


def similar(a: dict, b: dict) -> bool:
    """Near-identical findings from different sources: same kind, same negations, 80% of content words shared,
    and a shared file when both cite files. Strict on purpose: two findings about the same value in two
    functions must stay two rows."""
    if (a.get("kind") or "defect") != (b.get("kind") or "defect"):
        return False
    wa, wb = _words(a.get("finding")), _words(b.get("finding"))
    if not wa or not wb or (wa & _NEG) != (wb & _NEG):
        return False
    if len(wa & wb) / len(wa | wb) < 0.8:
        return False
    pa, pb = _paths(a.get("refs")), _paths(b.get("refs"))
    return not (pa and pb and not pa & pb)


def _new_row(n: int, obs: dict, now: str) -> dict:
    kind = obs.get("kind") or "defect"
    return {"id": f"R{n}", "keys": [], "finding": obs.get("finding") or "", "kind": kind, "sources": [],
            "suggested_scope": None, "scope": None,
            "result": {"run": obs.get("run"), "turn": obs.get("turn"), "value": "credit" if kind == "credit" else "unknown"},
            "basis": "none", "refs": [], "limits": "", "review_status": None, "severity": None,
            "marks": {"selected": False, "goal": False, "dp": None, "notes": False}, "marked_at": None,
            "verify": {"status": "none", "at": None, "path": None, "summary": None},
            "other_run": False, "created_at": now, "changed_at": now}


def _apply(row: dict, obs: dict, selected: str | None) -> None:
    """Lay one observation over a row: values it gives replace the row's, values it leaves out stay."""
    for k in ("finding", "kind", "severity", "limits"):
        if obs.get(k):
            row[k] = obs[k]
    if obs.get("basis"):
        row["basis"] = obs["basis"]
    if obs.get("refs"):
        row["refs"] = list(obs["refs"])
    if obs.get("scope"):
        row["suggested_scope"] = obs["scope"]
    res = row.setdefault("result", {"run": None, "turn": None, "value": "unknown"})
    if obs["source"]["type"] == "dossier":   # a hazard about the task, not about one run
        res.update(run=selected, turn=None)
    else:
        res.update(run=obs.get("run") or res.get("run"), turn=obs.get("turn") if obs.get("turn") is not None else res.get("turn"))
    if obs.get("result"):
        res["value"] = obs["result"]
    if obs.get("status"):
        row["review_status"] = obs["status"]


def _derived(row: dict) -> tuple:
    """What a finding says. Not other_run (it flips when someone looks at another run) or superseded_by (the
    review status already says so): neither means the finding changed under Austin's marks."""
    return (row.get("finding"), row.get("kind"), row.get("severity"), row.get("basis"), row.get("limits"),
            json.dumps(row.get("result"), sort_keys=True), row.get("review_status"), tuple(row.get("refs") or []))


def _strength(basis: str | None) -> int:
    return BASES.index(basis) if basis in BASES else len(BASES)


def _build(data: dict, observations: list[dict], selected: str | None, now: str) -> dict:
    rows = data["rows"]
    by_key = {k: r for r in rows for k in r.get("keys") or []}
    before = {r["id"]: _derived(r) for r in rows}
    n = max([int(r["id"][1:]) for r in rows if re.fullmatch(r"R\d+", r["id"])] + [int(data.get("next_id") or 1) - 1])
    fresh: dict[str, list[dict]] = {}
    for obs in observations:
        row = by_key.get(obs["key"])
        if row is None:   # a near-identical finding from another chat, file or source shares its row
            origin = obs["key"].split("#")[0] + "#"   # keys are "<origin>#<id>"; siblings never merge
            row = next((r for r in rows if r.get("finding") and similar(obs, r)
                        and not any(k.startswith(origin) for k in r.get("keys") or [])), None)
        if row is None and not obs.get("finding"):
            continue   # a follow-up's status for a finding no saved review describes: nothing to show
        if row is None:
            n += 1
            row = _new_row(n, obs, now)
            rows.append(row)
        if obs["key"] not in row["keys"]:
            row["keys"].append(obs["key"])
        by_key[obs["key"]] = row
        _apply(row, obs, selected)
        fresh.setdefault(row["id"], []).append(obs["source"])
        if obs.get("supersedes") and obs.get("chat"):
            old = by_key.get(f"review:{obs['chat']}#{obs['supersedes']}")
            if old is not None and old is not row:
                old["review_status"], old["superseded_by"] = "superseded", row["id"]
    for r in rows:
        if r["id"] in fresh:   # sources rebuilt from what exists now; Austin's own sources always stay
            mine = [s for s in r.get("sources") or [] if s.get("type") == "austin"]
            seen, srcs = set(), []
            for s in fresh[r["id"]]:
                sig = (s.get("type"), s.get("ref"), s.get("finding_id"))
                if sig not in seen:
                    seen.add(sig)
                    srcs.append(s)
            r["sources"] = mine + srcs
        if r.get("verify", {}).get("status") == "confirmed" and r["verify"].get("executed") \
                and _strength(r.get("basis")) > _strength("isolated"):
            r["basis"] = "isolated"
        r["other_run"] = bool(selected and r.get("result", {}).get("run") and r["result"]["run"] != selected)
        if r["id"] in before and before[r["id"]] != _derived(r):
            r["changed_at"] = now
            if r.get("marked_at"):
                r["changed_since_mark"] = True   # Austin's marks may rest on the old reading; mark() clears it
    data["next_id"] = n + 1
    return data


def refresh(task_dir: Path, run: str | None = None, log=print, *, extract: bool = False, runner=None) -> dict:
    """Rebuild the rows from every source and save. Never deletes a row; scope and marks are never touched.
    `run` overrides the selected run for this refresh. `extract` reads reviews that have no findings block
    with one headless Claude call each (opt-in; it costs a Claude call)."""
    d = Path(task_dir)
    meta = workspace.load(d)
    selected = run or selected_run(d, meta)
    obs, missing = _review_obs(d, log, extract, runner)
    ev = run_evidence(d, selected) if selected else None
    obs += _dossier_obs(d, meta) + _facts_obs(d, selected, ev)
    # Which turn of the run the reviews reach, so a turn-1 result never reads as the whole run's.
    reviewed = [o["turn"] for o in obs if o["source"]["type"] == "review" and o.get("run") == selected and o.get("turn")]
    coverage = {"run": selected, "reviewed_turn": max(reviewed) if reviewed else None,
                "run_turns": len(ev["turns"]) if ev and ev.get("turns") else None}
    with _locked(d):
        data = load(d)
        old = json.dumps(data, sort_keys=True)
        prev_run = data.get("run")
        data["version"], data["run"], data["coverage"] = VERSION, selected, coverage
        if prev_run and prev_run != selected:
            data["run_changed"] = {"from": prev_run, "to": selected, "at": _now()}
        _build(data, obs, selected, _now())
        changed = json.dumps({**data, "updated_at": None}, sort_keys=True) != json.dumps(
            {**json.loads(old), "updated_at": None}, sort_keys=True)
        if changed or not (d / FILE).exists():
            data["updated_at"] = _now()
            save(d, data)
    if missing:
        log(f"{len(missing)} saved review{'s' if len(missing) != 1 else ''} without a findings block "
            f"({', '.join(missing)}). `johto grading {d.name} refresh --extract` reads "
            + ("them with one Claude call each." if len(missing) != 1 else "it with one Claude call."))
    if changed:
        stages.log(d, "grading_written", run=selected, rows=len(data["rows"]))
    data["_missing"] = missing
    return data


# --- Austin's marks ------------------------------------------------------------------------------------

def mark(task_dir: Path, ids, *, selected=UNSET, goal=UNSET, dp=UNSET, notes=UNSET, scope=UNSET) -> list[dict]:
    """Set Austin's marks on rows: selected, goal (G), decision point 1-3 (None clears), notes (N), and scope
    (explicit, necessary, uncertain, outside; None clears). Marking G, a decision point or N selects the row;
    unselecting clears its G, decision point and N. Returns the rows that changed."""
    d = Path(task_dir)
    ids = [str(i).strip().upper() for i in ([ids] if isinstance(ids, str) else ids or [])]
    ids = [f"R{i}" if i.isdigit() else i for i in ids]
    if not ids:
        raise GradingError("Name at least one row, such as R3.")
    if dp is not UNSET and dp is not None and dp not in (1, 2, 3):
        raise GradingError(f"A decision point is 1, 2 or 3, not {dp!r}.")
    if scope is not UNSET and scope is not None and scope not in SCOPES:
        raise GradingError(f"Scope is one of {', '.join(SCOPES)}, not {scope!r}.")
    with _locked(d):
        data = load(d)
        rows = {r["id"]: r for r in data["rows"]}
        unknown = [i for i in ids if i not in rows]
        if unknown:
            raise GradingError(f"No row {', '.join(unknown)} in {d.name}'s grading table.")
        now, changed = _now(), []
        for i in ids:
            r = rows[i]
            m = r.setdefault("marks", {"selected": False, "goal": False, "dp": None, "notes": False})
            before = (dict(m), r.get("scope"))
            if goal is not UNSET:
                m["goal"] = bool(goal)
            if dp is not UNSET:
                m["dp"] = dp
            if notes is not UNSET:
                m["notes"] = bool(notes)
            if scope is not UNSET:
                r["scope"] = scope
            if selected is not UNSET:
                m["selected"] = bool(selected)
                if not selected:
                    m.update(goal=False, dp=None, notes=False)
            if m["goal"] or m["dp"] or m["notes"]:
                m["selected"] = True
            if (dict(m), r.get("scope")) != before or r.get("changed_since_mark"):   # marking again acknowledges a change
                r["marked_at"], r["changed_since_mark"] = now, False
                changed.append(r)
        if changed:
            data["updated_at"] = now
            save(d, data)
    if changed:
        stages.log(d, "grading_marked", rows=[r["id"] for r in changed])
    return changed


def _field(name: str) -> str:
    from . import rules
    k = rules.key(name)
    return {"dp": "decision_points", "notes": "validation_notes", "vn": "validation_notes"}.get(k, k)


def used_by(task_dir: Path, field: str) -> list[dict]:
    """Rows Austin marked for a field: goal, decision_points (by decision point) or validation_notes. Withdrawn,
    superseded and other-run rows are included (their review_status and other_run say so), so a check can
    point out a mark that rests on a finding that no longer stands."""
    f = _field(field)
    if f not in FIELDS:
        raise GradingError(f"{field!r} is not one of {', '.join(FIELDS)}.")
    rows = [r for r in load(task_dir)["rows"] if r.get("marks")]
    if f == "goal":
        return [r for r in rows if r["marks"].get("goal")]
    if f == "decision_points":
        return sorted((r for r in rows if r["marks"].get("dp")), key=lambda r: (r["marks"]["dp"], _num(r)))
    return [r for r in rows if r["marks"].get("notes")]


# --- Views ---------------------------------------------------------------------------------------------

def _num(r: dict) -> int:
    m = re.fullmatch(r"R(\d+)", r.get("id") or "")
    return int(m.group(1)) if m else 10**6


def _sev(r: dict) -> int:
    return SEVERITIES.index(r["severity"]) if r.get("severity") in SEVERITIES else len(SEVERITIES)


def active(r: dict) -> bool:
    """Shown by default: not withdrawn or superseded, and about the selected run."""
    return r.get("review_status") not in INACTIVE and not r.get("other_run")


def ordered(rows: list[dict]) -> list[dict]:
    """Selected first, then by severity, then by row number."""
    return sorted(rows, key=lambda r: (not (r.get("marks") or {}).get("selected"), _sev(r), _num(r)))


def needs_scope(r: dict) -> bool:
    """Whether a row's scope is Austin's call: something the run missed or a behavior the request may imply, or
    one a source calls uncertain. Credits, claims and limitations don't need a scope."""
    if r.get("suggested_scope") == "uncertain":
        return True
    return r.get("kind") in ("requirement", "defect") and (r.get("result") or {}).get("value") != "credit"


def open_decisions(data: dict) -> list[dict]:
    """Rows that still need Austin's call: a scope question (needs_scope) with no scope yet that is high severity
    or new/worsened in the latest review (a low-severity new row isn't a decision, nor a dossier hypothesis by
    itself); and rows a review made worse after he marked them."""
    out = []
    for r in data.get("rows") or []:
        if not active(r) or r.get("kind") == "limitation":
            continue
        st = r.get("review_status")
        fresh = st == "worsened" or (st == "new" and r.get("severity") != "low")
        if r.get("scope") is None and needs_scope(r) and (r.get("severity") == "high" or fresh):
            out.append(r)
        elif st == "worsened" and r.get("changed_since_mark"):
            out.append(r)
    return ordered(out)


def counts(data: dict) -> dict:
    """{rows, selected, open_decisions} for the task card."""
    rows = [r for r in data.get("rows") or [] if active(r)]
    return {"rows": len(rows), "selected": sum(1 for r in rows if (r.get("marks") or {}).get("selected")),
            "open_decisions": len(open_decisions(data))}


def marks_label(r: dict) -> str:
    m = r.get("marks") or {}
    parts = (["G"] if m.get("goal") else []) + ([str(m["dp"])] if m.get("dp") else []) + (["N"] if m.get("notes") else [])
    return " ".join(parts) or ("sel" if m.get("selected") else "")


def known_label(r: dict) -> str:
    v = (r.get("verify") or {}).get("status")
    return (r.get("basis") or "none") + (f", verify {v}" if v and v != "none" else "")


def result_label(r: dict) -> str:
    res = r.get("result") or {}
    out = res.get("value") or "unknown"
    if r.get("other_run"):
        out += f" ({res.get('run')})"
    return out


def cut(text: str, n: int) -> str:
    """At most n characters, cut at a word boundary (a word longer than the room is cut as it must be)."""
    t = " ".join((text or "").split())
    if len(t) <= n:
        return t
    head = t[: n - 1]
    if t[n - 1] != " " and " " in head and head.rfind(" ") > n // 2:
        head = head[: head.rfind(" ")]
    return head.rstrip(" ,;:") + "…"


def secondary(r: dict) -> bool:
    """Folded by default: a low-severity or credit row Austin hasn't selected or scoped."""
    return not (r.get("marks") or {}).get("selected") and not r.get("scope") and (
        r.get("severity") == "low" or r.get("kind") == "credit")


def coverage_line(data: dict) -> str | None:
    """'Results from the review of turn 1 of 3; turns 2-3 not reviewed.' when the reviews stop short of the run."""
    cov = data.get("coverage") or {}
    t, n = cov.get("reviewed_turn"), cov.get("run_turns")
    if not t or not n or t >= n or cov.get("run") != data.get("run"):
        return None
    later = f"turn {n}" if t + 1 == n else f"turns {t + 1}-{n}"
    return f"Results from the review of turn {t} of {n}; {later} not reviewed."


def render(data: dict, all: bool = False) -> str:
    """The table for the terminal: selected rows first, then by severity. Withdrawn, superseded and other-run
    rows, and unselected low-severity or credit rows, only with all=True."""
    rows = data.get("rows") or []
    shown = ordered(rows if all else [r for r in rows if active(r) and not secondary(r)])
    c = counts(data)
    head = (f"Grading table · run {data.get('run') or '(none)'} · {c['rows']} row{'s' if c['rows'] != 1 else ''}, "
            f"{c['selected']} selected, {c['open_decisions']} open")
    cov = coverage_line(data)
    if cov:
        head += "\n" + cov
    if not shown and rows:
        return head + f"\nNo rows for {data.get('run')} to show; {len(rows)} withdrawn, superseded, from another run, low " \
                      "severity or credits: --all shows them."
    if not shown:
        return head + "\nNo rows yet: no saved review findings, dossier hazards or unbacked pass claims."

    def result(r):
        turn = (r.get("result") or {}).get("turn")
        return result_label(r) + (f", turn {turn}" if turn else "")
    cols = ("ID", "Finding", "Scope", "Result", "How known", "Review", "Use")
    table = [cols] + [(r["id"], cut(r.get("finding"), 90), r.get("scope") or "-", result(r), known_label(r),
                       r.get("review_status") or "-", marks_label(r)) for r in shown]
    widths = [max(len(t[i]) for t in table) for i in range(len(cols))]
    lines = [head, ""] + ["  ".join(c.ljust(w) for c, w in zip(t, widths, strict=True)).rstrip() for t in table]
    gone = sum(1 for r in rows if not active(r))
    folded = sum(1 for r in rows if active(r) and secondary(r))
    if not all and (gone or folded):
        bits = ([f"{folded} low severity or credits"] if folded else []) + (
            [f"{gone} withdrawn, superseded or from another run"] if gone else [])
        lines += ["", f"{gone + folded} more ({'; '.join(bits)}): --all shows them."]
    return "\n".join(lines)


# --- Extracting findings from older reviews (opt-in, a Claude call each) ---------------------------------

EXTRACT_PROMPT = """Below is an outside review of one run of an AI coding agent. List its findings as data.

One object per distinct finding the review states: a defect or omission in the run, a credit (something the
run got right that the review confirms), a behavior of the code the review describes (a code fact on a path
the request touches, and whether the run covered it), a claim by the agent
that the review checks, or a limitation of the review's own checking. Keep separate findings separate even when
they look alike (a value lost in one function and the same value ignored in another are two findings), and
never move one finding's evidence or verification to another. Keep every qualification the review gives
(substitutions, what was not run, conditions) in "limits".

Fields:
- "id": "F1", "F2", ... in the order the review gives them
- "finding": one factual sentence, close to the review's own words, no advice, never framed as what a strong
  or weak model would do or what the task must do
- "kind": defect, credit, behavior, claim or limitation
- "status": "new"
- "severity": high, medium or low, from the review's emphasis
- "result": what the run did about it: credit, partial, missed or unknown
- "basis": how the review knows: executed (ran the app or the run's own tests), isolated (ran the code in
  isolation), traced (read the source), claimed (stated without evidence)
- "refs": the file:line and transcript references the review gives for this finding, verbatim
- "limits": what the review says it did not verify for this finding, or ""
- "supersedes": null

Return only a JSON array inside a fenced code block whose info string is findings.

=== REVIEW ===
@@REVIEW@@
"""


def _claude(prompt: str, cwd: Path, *, model: str, tools: str = "", effort: str | None = None,
            timeout: int = 600) -> dict:
    """One headless Claude call (the pack.outside_review pattern): {"text", "cost_usd", "error", "seconds"}."""
    cmd = [config.CLAUDE_BIN, "-p", "--model", model, "--output-format", "json", "--tools", tools,
           "--strict-mcp-config", "--setting-sources", "project", "--no-session-persistence"]
    if tools:
        cmd += ["--allowedTools", tools, "--disallowedTools", "WebFetch,WebSearch,Task"]
    if effort:
        cmd += ["--effort", effort]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, input=prompt)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"text": "", "cost_usd": None, "error": f"Claude didn't run: {e}", "seconds": time.time() - t0}
    try:
        out = json.loads(r.stdout)
    except ValueError:
        out = {"result": r.stdout}
    err = None if r.returncode == 0 else (r.stderr or r.stdout or "")[-800:]
    return {"text": out.get("result") or "", "cost_usd": out.get("total_cost_usd"), "error": err,
            "seconds": round(time.time() - t0, 1)}


def extract_findings(task_dir: Path, review_path: Path, log=print, runner=None) -> list[dict] | None:
    """Ask headless Claude (a cheap model, no tools) to list an older review's findings in the block's shape;
    saved to reviews/<name>.findings.json. None when the call fails."""
    from . import reviews
    review_path = Path(review_path)
    text = reviews.body(review_path)
    log(f"Reading {review_path.name} with a Claude call ({EXTRACT_MODEL}, no tools; usually under a minute)…")
    with tempfile.TemporaryDirectory(prefix="johto-extract-") as tmp:
        res = (runner or _claude)(EXTRACT_PROMPT.replace("@@REVIEW@@", text), Path(tmp), model=EXTRACT_MODEL,
                                  tools="", timeout=600)
    found = parse_findings(res.get("text") or "") if not res.get("error") else None
    if found is None:
        log(f"Couldn't read findings from {review_path.name}: {(res.get('error') or 'no findings block in the reply')[:300]}")
        return None
    side = review_path.with_suffix(".findings.json")
    side.write_text(json.dumps({"review": review_path.name, "extracted_at": _now(), "model": EXTRACT_MODEL,
                                "cost_usd": res.get("cost_usd"), "findings": found}, indent=1, ensure_ascii=False))
    log(f"{len(found)} finding{'s' if len(found) != 1 else ''} from {review_path.name}"
        + (f" (${res['cost_usd']:.2f})" if res.get("cost_usd") else ""))
    return found


# --- Verifying one finding (opt-in) ---------------------------------------------------------------------

VERIFY_PROMPT = """You are checking one finding about a run of an AI coding agent, from outside its sandbox. The
current directory is a temporary checkout of the repository at the task's pinned commit with the agent's final
diff applied: `git diff` shows the agent's changes.

The finding (from an outside review; it may be wrong):
@@FINDING@@

Evidence it cites: @@REFS@@
Known limits so far: @@LIMITS@@

The agent's task (the opening prompt):
@@PROMPT@@

Write the smallest scratch test or script that shows whether the finding holds for this code, in
.johto-verify/ only, and run it. Exercise the repository's own code (import it) rather than re-implementing
it. If a dependency or service is missing, either substitute a minimal stand-in and name it, or stop and
report inconclusive. Don't change files outside .johto-verify/ and don't install anything outside it (a
virtualenv inside it is fine).

Rules: research only. Facts with file:line evidence. Do not write ratings or text addressed to the engineer,
and do not suggest wording for anything.

Return JSON between the lines <<<VERIFY and VERIFY>>>:
{"status": "confirmed|refuted|inconclusive",
 "executed": true or false (true only if the test ran the repository's code and its output decides the status),
 "summary": "<= 60 words: what the test showed, as facts",
 "ran": ["each command that matters, verbatim"],
 "files": ["scratch files you wrote, relative paths"],
 "substitutions": ["each stand-in for a missing dependency or service"],
 "limits": "<= 60 words: what this does not show",
 "evidence": ["path:line or short output lines"]}
"""


def _git(args: list[str], cwd: Path | None = None, env: dict | None = None, timeout: int = 900):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **(env or {})})


def plan_verify(task_dir: Path, row_id: str) -> dict:
    """What verify would check, and whether the run's diff applies to the pinned commit (in a scratch
    index, no checkout). Raises GradingError when it can't run."""
    from . import pack
    d = Path(task_dir)
    rid = str(row_id).strip().upper()
    data = load(d)
    row = next((r for r in data["rows"] if r["id"] == rid), None)
    if row is None:
        raise GradingError(f"No row {rid} in {d.name}'s grading table.")
    meta = workspace.load(d)
    run = (row.get("result") or {}).get("run") or data.get("run") or selected_run(d, meta)
    diff_p = d / "runs" / str(run) / "prework_output" / "final.diff"
    if not run or not diff_p.exists():
        raise GradingError(f"{run or 'This task'} has no collected diff to check against; collect the run first.")
    repo = meta.get("repo") or {}
    if not (repo.get("full_name") and repo.get("commit")):
        raise GradingError(f"{d.name} has no pinned repository in task.json.")
    diff = diff_p.read_text(errors="replace")
    try:
        bare = pack.cached_repo(repo["full_name"], repo["commit"])
    except RuntimeError as e:
        raise GradingError(f"Couldn't fetch {repo['full_name']}@{repo['commit'][:8]}: {e}") from None
    files = sorted({m.group(1) for m in re.finditer(r"^diff --git a/(.*?) b/", diff, re.M)})
    if diff.strip():
        with tempfile.TemporaryDirectory(prefix="johto-verify-idx-") as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            patch = Path(tmp) / "run.diff"
            patch.write_text(diff if diff.endswith("\n") else diff + "\n")
            r = _git(["--git-dir", str(bare), "read-tree", repo["commit"]], env=env)
            if r.returncode != 0:   # an empty index would let a diff that only adds files pass by accident
                raise GradingError(f"Couldn't read {repo['commit'][:8]} from the cached clone: {r.stderr.strip()[:300]}")
            r = _git(["--git-dir", str(bare), "apply", "--cached", "--check", "--whitespace=nowarn", str(patch)], env=env)
            if r.returncode != 0:
                raise GradingError(f"{run}'s diff doesn't apply fully to {repo['commit'][:8]}, so verify stops: "
                                   f"{(r.stderr or r.stdout).strip()[:400]}")
    return {"row": row, "run": run, "repo": repo, "bare": bare, "diff": diff, "files": files,
            "estimate": f"headless Claude ({VERIFY_MODEL}, high effort) with Bash in a temporary checkout, outside "
                        f"the lane: usually 5-20 min"}


def _checkout(plan: dict, dest: Path) -> None:
    """A standalone checkout of the pinned commit (blobs from the cached clone) with the run's diff applied
    on top of a base commit, so `git diff` shows the run's changes. Raises GradingError if anything fails."""
    from . import pack
    bare, commit = str(plan["bare"]), plan["repo"]["commit"]
    r = _git(["--git-dir", bare, "worktree", "add", "--detach", "-f", str(dest), commit], timeout=1800)
    try:
        if r.returncode != 0:
            raise GradingError(f"Checkout of {commit[:8]} failed: {(r.stderr or r.stdout).strip()[-400:]}")
        (dest / ".git").unlink()
    finally:
        _git(["--git-dir", bare, "worktree", "prune"])
    pack.strip_claude_settings(dest)
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.name=johto", "-c", "user.email=johto@localhost", "commit", "-q", "--no-verify",
                  "-m", f"pinned {commit[:12]}"]):
        r = _git(args, cwd=dest, timeout=1800)
        if r.returncode != 0:
            raise GradingError(f"Preparing the checkout failed (git {args[0]}): {(r.stderr or r.stdout).strip()[-300:]}")
    if plan["diff"].strip():
        patch = dest.parent / "run.diff"
        patch.write_text(plan["diff"] if plan["diff"].endswith("\n") else plan["diff"] + "\n")
        r = _git(["apply", "--whitespace=nowarn", str(patch)], cwd=dest)
        if r.returncode != 0:
            raise GradingError(f"{plan['run']}'s diff doesn't apply fully: {(r.stderr or r.stdout).strip()[:400]}")
    pack.strip_claude_settings(dest)


def _verify_block(text: str) -> dict | None:
    m = re.search(r"<<<VERIFY\s*(\{.*\})\s*VERIFY>>>", text or "", re.S)
    if not m:
        return None
    for attempt in _repairs(m.group(1)):
        try:
            v = json.loads(attempt)
            return v if isinstance(v, dict) else None
        except ValueError:
            continue
    return None


def _scratch(dest: Path, names: list) -> list[tuple[str, str]]:
    out = []
    root = dest / ".johto-verify"
    paths = [dest / str(n) for n in names or []] + (sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else [])
    for p in dict.fromkeys(paths):
        try:
            rel = str(p.resolve().relative_to(dest.resolve()))
        except (OSError, ValueError):
            continue
        if "/venv/" in f"/{rel}/" or "/.venv/" in f"/{rel}/" or "__pycache__" in rel or not p.is_file():
            continue
        if p.stat().st_size <= 40_000:
            out.append((rel, p.read_text(errors="replace")))
    return out[:12]


def _report(task_dir: Path, plan: dict, res: dict, block: dict | None, scratch, stamp: str) -> str:
    row, repo = plan["row"], plan["repo"]
    status = (block or {}).get("status") if (block or {}).get("status") in VERIFY_STATES else "inconclusive"
    how = "executed the repository's code in isolation" if (block or {}).get("executed") else "did not execute the repository's code"
    lines = [f"# Verify {row['id']}: {cut(row.get('finding'), 120)}", "",
             f"- Task {Path(task_dir).name} · {plan['run']} · {repo['full_name']}@{repo['commit'][:12]} with "
             f"{plan['run']}'s diff ({len(plan['files'])} file{'s' if len(plan['files']) != 1 else ''})",
             f"- Headless Claude ({VERIFY_MODEL}, high effort), outside the lane, {stamp}"
             + (f", {res['seconds'] / 60:.1f} min" if res.get("seconds") else "")
             + (f", ${res['cost_usd']:.2f}" if res.get("cost_usd") else ""),
             f"- Result: {status} ({how})", ""]
    if block is None:
        lines += ["## The run failed", "", "```", (res.get("error") or res.get("text") or "no reply")[-2000:], "```", ""]
    else:
        lines += ["## What the test showed", "", str(block.get("summary") or ""), ""]
        for title, key in (("Commands", "ran"), ("Substitutions", "substitutions"), ("Evidence", "evidence")):
            items = block.get(key) or []
            if items:
                lines += [f"## {title}", ""] + [f"- `{x}`" if key == "ran" else f"- {x}" for x in items] + [""]
        lines += ["## Limits", "", str(block.get("limits") or "Not stated."), ""]
    for rel, text in scratch:
        lines += [f"## Scratch file {rel}", "", "```", text.rstrip(), "```", ""]
    return "\n".join(lines)


def verify(task_dir: Path, row_id: str, *, runner=None, log=print, plan: dict | None = None) -> dict:
    """Reproduce one row's finding with a scratch test in a temporary checkout of the run's final code,
    outside the lane. Saves checks/verify-<row>-<stamp>.md and sets the row's verify; a confirmation by
    execution makes its basis "isolated". The checkout is always removed."""
    d = Path(task_dir)
    plan = plan or plan_verify(d, row_id)
    row = plan["row"]
    meta = workspace.load(d)
    prompt_text = _studio_prompt(d)
    if not prompt_text and (d / "submission.md").exists():
        prompt_text = workspace.parse_submission(d)["sections"].get("Opening Prompt", "")
    prompt = (VERIFY_PROMPT.replace("@@FINDING@@", row.get("finding") or "")
              .replace("@@REFS@@", ", ".join(row.get("refs") or []) or "none")
              .replace("@@LIMITS@@", row.get("limits") or "none")
              .replace("@@PROMPT@@", (prompt_text or "(not saved)").strip()))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = Path(tempfile.mkdtemp(prefix=f"johto-verify-{d.name}-{row['id']}-"))
    try:
        dest = work / "repo"
        log(f"Checking out {meta['repo']['full_name']}@{meta['repo']['commit'][:8]} with {plan['run']}'s diff…")
        _checkout(plan, dest)
        log(f"Verifying {row['id']} ({VERIFY_MODEL}, Bash in the checkout)…")
        res = (runner or _claude)(prompt, dest, model=VERIFY_MODEL, tools="Bash,Read,Grep,Glob,Write,Edit",
                                  effort="high", timeout=VERIFY_TIMEOUT_S)
        block = _verify_block(res.get("text") or "") if not res.get("error") else None
        scratch = _scratch(dest, (block or {}).get("files") or [])
        report = _report(d, plan, res, block, scratch, time.strftime("%Y-%m-%d %H:%M"))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    checks = d / "checks"
    checks.mkdir(exist_ok=True)
    out = checks / f"verify-{row['id']}-{stamp}.md"
    out.write_text(report + "\n")
    status = (block or {}).get("status") if (block or {}).get("status") in VERIFY_STATES[1:] else "inconclusive"
    executed = bool((block or {}).get("executed"))
    summary = cut(str((block or {}).get("summary") or res.get("error") or "The verify run gave no result."), 400)
    with _locked(d):
        data = load(d)
        r = next(x for x in data["rows"] if x["id"] == row["id"])
        r["verify"] = {"status": status, "at": _now(), "path": _rel(d, out), "summary": summary, "executed": executed}
        if status == "confirmed" and executed and _strength(r.get("basis")) > _strength("isolated"):
            r["basis"] = "isolated"
        r["changed_at"] = data["updated_at"] = _now()
        save(d, data)
    stages.log(d, "verify_done", row=row["id"], status=status, path=_rel(d, out), cost_usd=res.get("cost_usd"))
    log(f"{row['id']}: {status}. Report: {out}")
    return {"row": row["id"], "status": status, "executed": executed, "path": str(out), "summary": summary,
            "cost_usd": res.get("cost_usd")}


def _studio_prompt(d: Path) -> str | None:
    try:
        snap = json.loads((d / "app" / "studio-fields.json").read_text())
    except (OSError, ValueError):
        return None
    return ((snap or {}).get("fields") or {}).get("field_opening_prompt") or None


# --- CLI -----------------------------------------------------------------------------------------------

def _task(name: str) -> Path:
    try:
        return workspace.resolve(name)
    except FileNotFoundError as e:
        raise SystemExit(str(e)) from None


def cmd_grading(a) -> None:
    d = _task(a.task)
    try:
        if a.action == "mark":
            if all(v is UNSET for v in (a.goal, a.dp, a.notes, a.scope, a.selected)):
                raise SystemExit("Say what to mark: --goal, --dp N, --notes, --scope, --select (or the --no- forms).")
            scope = a.scope if a.scope is UNSET or a.scope != "none" else None
            dp = None if a.dp == 0 else a.dp
            changed = mark(d, a.ids, selected=a.selected, goal=a.goal, dp=dp, notes=a.notes, scope=scope)
            data = load(d)
            ids = {r["id"] for r in changed}
            print(f"Marked {', '.join(sorted(ids, key=lambda i: int(i[1:]))) if ids else 'nothing (no change)'}.")
            if ids:
                print(render({**data, "rows": [r for r in data["rows"] if r["id"] in ids]}, all=True).split("\n", 2)[2])
        elif a.action == "verify":
            if len(a.ids) != 1:
                raise SystemExit("Verify one row at a time, such as: johto grading <task> verify R3")
            p = plan_verify(d, a.ids[0])
            print(f"Verify {p['row']['id']}: {cut(p['row'].get('finding'), 100)}")
            print(f"This runs {p['estimate']}. {p['run']}'s diff applies to the pinned commit.")
            if a.plan:
                return
            verify(d, a.ids[0], plan=p)
        else:
            if a.extract:
                print("--extract reads each saved review that has no findings block with one Claude call.")
            data = refresh(d, a.run, extract=a.extract)
            if a.action == "refresh":
                c = counts(data)
                print(f"{d.name}: {len(data['rows'])} rows ({c['rows']} for {data.get('run')}), {c['selected']} "
                      f"selected, {c['open_decisions']} open.")
                return
            print(render(data, all=a.all))
    except GradingError as e:
        raise SystemExit(str(e)) from None


def register(sub) -> None:
    s = sub.add_parser("grading", help="the grading table: review findings, dossier hazards and run facts, with your marks")
    s.add_argument("task")
    s.add_argument("action", nargs="?", default="show", choices=("show", "mark", "refresh", "verify"))
    s.add_argument("ids", nargs="*", help="rows for mark or verify, such as R3 R4")
    s.add_argument("--all", action="store_true", help="also show withdrawn, superseded and other-run rows")
    s.add_argument("--run", help="refresh against this run instead of the submission run")
    s.add_argument("--extract", action="store_true", help="read older reviews without a findings block (a Claude call each)")
    s.add_argument("--plan", action="store_true", help="verify: show the plan and check the diff applies, then stop")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--goal", dest="goal", action="store_const", const=True)
    g.add_argument("--no-goal", dest="goal", action="store_const", const=False)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--dp", dest="dp", type=int, choices=(1, 2, 3), metavar="N", help="decision point 1, 2 or 3")
    g.add_argument("--no-dp", dest="dp", action="store_const", const=0)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--notes", dest="notes", action="store_const", const=True)
    g.add_argument("--no-notes", dest="notes", action="store_const", const=False)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--select", dest="selected", action="store_const", const=True)
    g.add_argument("--unselect", dest="selected", action="store_const", const=False)
    s.add_argument("--scope", choices=(*SCOPES, "none"))
    s.set_defaults(func=cmd_grading, goal=UNSET, dp=UNSET, notes=UNSET, selected=UNSET, scope=UNSET)
