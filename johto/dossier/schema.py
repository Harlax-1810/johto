"""Validator for johto.dossier/1 documents (docs/dossier-spec.md section 3).

`validate(doc, root)` returns a list of plain error strings; an empty list means valid. With `root`
(the clone at the pinned commit) every pointer's file must exist and its line range must be inside it,
and every listed path must exist. Without `root` only the shape is checked.

The text-policy checks (POLICY below: no imperative openings, no fix comparisons, where_models_fail
items stated as hazards in a named file, questions ending in "?") keep research text from taking the
shape of the fields Austin must write himself. They run when `policy` is true, which defaults to "a
root was given", i.e. for every generated dossier; shape-only validation (fixtures, old dossiers) skips
them. `context` adds the repo's facts: {"source_files": int, "codebase_types": [types that passed]}.
"""
from __future__ import annotations

import re
from pathlib import Path

SCHEMA = "johto.dossier/1"

ACTIONS = ("Create", "Modify", "Operate", "Understand / Assess")
DOMAINS = ("Backend / API", "Web frontend", "DevOps / cloud", "Dev tooling", "Data / DB", "Games",
           "Other software", "AI agents")
CODEBASE_TYPES = ("Personal-scale", "Enterprise-grade")
PROMISE = ("high", "medium", "low")
NODE_KINDS = ("service", "module", "store", "ui", "worker", "external", "config")
HEALTH = ("solid", "fragile", "neglected")
SIZES = ("S", "M", "L")
LIKELIHOOD = ("unlikely", "possible", "likely")
UPSTREAM_STATES = ("open", "closed", "merged")
TONES = ("info", "warn")
BLOCK_TYPES = ("overview", "architecture", "flow", "subsystems", "opportunity", "code", "facts", "callout",
               "questions", "html")

MAX_BLOCKS = 30
MIN_OPPS, MAX_OPPS = 3, 7
MAX_NODES = 14
MAX_STEPS = 10
MAX_STACK = 8
MAX_QUESTIONS = 6
MAX_HTML_BYTES = 12 * 1024
TITLE_WORDS = 10  # block titles (not limited in the spec table; kept to a label)

POINTER = re.compile(r"^(?P<path>[^:\s][^:]*):(?P<start>\d+)(?:-(?P<end>\d+))?$")
SHA = re.compile(r"^[0-9a-f]{40}$")
EM_DASH = "—"
ADDRESSING = re.compile(r"\bplease\b|^\s*you\b", re.I)
HTML_BANNED = [
    (re.compile(r"<\s*script", re.I), "contains <script"),
    (re.compile(r"[\s/\"'`]on[a-z]+\s*=", re.I), "contains an on*= event handler"),
    (re.compile(r"https?://", re.I), "contains an http(s):// URL"),
    (re.compile(r"(?:\bsrc|\bhref)\s*=\s*['\"]?\s*//|url\(\s*['\"]?\s*//", re.I), "contains a protocol-relative URL"),
    (re.compile(r"\b(?:xlink:)?href\s*=\s*(?!['\"]?#)", re.I), "contains an href that is not a #fragment"),
    (re.compile(r"\bsrc(?:set)?\s*=\s*(?!['\"]?data:)", re.I), "contains a src that is not a data: URL"),
    (re.compile(r"url\(\s*(?!['\"]?\s*(?:#|data:))", re.I), "contains a url() that is not #fragment or data:"),
    (re.compile(r"javascript\s*:", re.I), "contains a javascript: URL"),
    (re.compile(r"<\s*(iframe|object|embed|link|meta|base|form)\b", re.I), "contains an embedding or meta tag"),
    (re.compile(r"@import", re.I), "contains @import"),
]

# --- text policy (generated dossiers) -----------------------------------------------------------
IMPERATIVE = re.compile(r"^\s*(?:Add|Fix|Make|Implement|Ensure|Create|Write|Update|Build|Refactor|Replace|Rewrite|"
                        r"Remove|Introduce|Use|Move|Change|Support|Handle|Prevent|Avoid|Stop|Let|Keep)\b")
COMPARISON = re.compile(r"\binstead of\b|\brather than\b|(?<![\w-])(?:better|worse)(?![\w-])|\bshould\b", re.I)
MUST = re.compile(r"\bmust\b", re.I)
FIRST_WORD = re.compile(r"^\W*([A-Za-z]+)")
NOT_GERUNDS = {"nothing", "something", "everything", "anything", "string", "strings", "thing", "things", "ring",
               "spring", "ping", "king", "ceiling", "sibling", "siblings", "wing", "swing"}
FILE_NAME = re.compile(r"[\w-]+\.[A-Za-z][A-Za-z0-9]{0,5}\b|\w/\w")
UA = "Understand / Assess"
UA_MIN_FILES = 200


def words(s: str) -> int:
    return len(s.split())


class _V:
    def __init__(self, root: Path | None, policy: bool = False, context: dict | None = None):
        self.root = root.resolve() if root else None
        self.policy = policy
        self.context = context or {}
        self.errors: list[str] = []
        self._nlines: dict[str, int | None] = {}

    def err(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    # --- primitives ---------------------------------------------------------------

    def obj(self, where: str, v, required: bool = True) -> dict | None:
        if v is None and not required:
            return None
        if not isinstance(v, dict):
            self.err(where, "missing" if v is None else f"must be an object, got {type(v).__name__}")
            return None
        return v

    def lst(self, where: str, v, min_items: int = 0, max_items: int | None = None) -> list:
        if not isinstance(v, list):
            self.err(where, "missing" if v is None else f"must be a list, got {type(v).__name__}")
            return []
        if len(v) < min_items:
            self.err(where, f"needs at least {min_items} item{'s' if min_items != 1 else ''}, has {len(v)}")
        if max_items is not None and len(v) > max_items:
            self.err(where, f"has {len(v)} items, limit {max_items}")
        return v

    def text(self, where: str, v, max_words: int | None = None, required: bool = True) -> None:
        if v is None or v == "":
            if required:
                self.err(where, "missing")
            return
        if not isinstance(v, str):
            self.err(where, f"must be a string, got {type(v).__name__}")
            return
        if not v.strip():
            if required:
                self.err(where, "is blank")
            return
        if max_words is not None and words(v) > max_words:
            self.err(where, f"{words(v)} words, limit {max_words}")
        self.style(where, v)

    def style(self, where: str, v: str) -> None:
        if EM_DASH in v:
            self.err(where, "contains an em dash")
        if ADDRESSING.search(v):
            self.err(where, "addresses a reader ('please' or a leading 'you')")

    def strings(self, where: str, v, max_words: int | None = None, min_items: int = 0,
                max_items: int | None = None) -> None:
        for i, s in enumerate(self.lst(where, v, min_items, max_items)):
            self.text(f"{where}[{i}]", s, max_words)

    def enum(self, where: str, v, options: tuple) -> None:
        if v not in options:
            self.err(where, f"{v!r} is not one of {', '.join(options)}")

    def boolean(self, where: str, v) -> None:
        if not isinstance(v, bool):
            self.err(where, "must be true or false")

    def count(self, where: str, v) -> None:
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            self.err(where, "must be a whole number >= 0")

    # --- files -------------------------------------------------------------------

    def _safe_rel(self, where: str, path: str) -> bool:
        if path.startswith("/") or path.startswith("~") or ".." in Path(path).parts:
            self.err(where, f"{path!r} must be relative to the repo root")
            return False
        return True

    def _lines(self, rel: str) -> int | None:
        if rel not in self._nlines:
            p = (self.root / rel)
            try:
                rp = p.resolve()
                if self.root not in rp.parents and rp != self.root:
                    self._nlines[rel] = None
                elif not rp.is_file():
                    self._nlines[rel] = None
                else:
                    data = rp.read_bytes()
                    self._nlines[rel] = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
            except OSError:
                self._nlines[rel] = None
        return self._nlines[rel]

    def pointer(self, where: str, v) -> None:
        if not isinstance(v, str) or not v.strip():
            self.err(where, "missing pointer")
            return
        m = POINTER.match(v.strip())
        if not m:
            self.err(where, f"{v!r} is not 'path:line' or 'path:start-end'")
            return
        path, start = m["path"], int(m["start"])
        end = int(m["end"]) if m["end"] else start
        if not self._safe_rel(where, path):
            return
        if start < 1 or end < start:
            self.err(where, f"{v!r} has a bad line range")
            return
        if self.root is None:
            return
        n = self._lines(path)
        if n is None:
            self.err(where, f"{path} is not a file in the repo at the pinned commit")
        elif end > n:
            self.err(where, f"{v!r} ends past the end of {path} ({n} lines)")

    def path(self, where: str, v) -> None:
        if not isinstance(v, str) or not v.strip():
            self.err(where, "missing path")
            return
        v = v.strip()
        if POINTER.match(v):
            self.pointer(where, v)
            return
        if not self._safe_rel(where, v):
            return
        if self.root is None:
            return
        if any(c in v for c in "*?["):
            try:
                hit = next(iter(self.root.glob(v.rstrip("/"))), None)
            except (ValueError, OSError):
                hit = None
            if hit is None:
                self.err(where, f"{v!r} matches nothing in the repo")
            return
        if not (self.root / v.rstrip("/")).exists():
            self.err(where, f"{v} does not exist in the repo at the pinned commit")

    def paths(self, where: str, v, min_items: int = 1) -> None:
        for i, p in enumerate(self.lst(where, v, min_items)):
            self.path(f"{where}[{i}]", p)

    def slot(self, where: str, v, why_words: int | None = None) -> None:
        s = self.obj(where, v)
        if s is None:
            return
        self.enum(f"{where}.action", s.get("action"), ACTIONS)
        self.enum(f"{where}.domain", s.get("domain"), DOMAINS)
        self.enum(f"{where}.codebase_type", s.get("codebase_type"), CODEBASE_TYPES)
        if why_words is not None:
            self.text(f"{where}.why", s.get("why"), why_words)
        n = self.context.get("source_files")
        if s.get("action") == UA and isinstance(n, int) and n < UA_MIN_FILES:
            self.err(f"{where}.action", f"Understand / Assess needs {UA_MIN_FILES}+ source files; this repo has {n}")
        ok = self.context.get("codebase_types") or []
        if ok and s.get("codebase_type") in CODEBASE_TYPES and s.get("codebase_type") not in ok:
            self.err(f"{where}.codebase_type", f"{s.get('codebase_type')!r} failed the Studio codebase check for this "
                                              f"repo; use {' or '.join(map(repr, ok))}")

    # --- text policy ---------------------------------------------------------------

    def no_directive(self, where: str, v, must: bool = True) -> None:
        """ambition and shape: a statement of what is missing or broken, not an instruction or a goal."""
        if not self.policy or not isinstance(v, str):
            return
        m = IMPERATIVE.match(v)
        if m:
            self.err(where, f"starts with the instruction {m.group(0).strip()!r}; state what is missing or broken now")
        self.no_comparison(where, v)
        if must and MUST.search(v):
            self.err(where, "uses 'must'; state what the code does now, not a requirement")

    def no_comparison(self, where: str, v) -> None:
        if not self.policy or not isinstance(v, str):
            return
        m = COMPARISON.search(v)
        if m:
            self.err(where, f"uses {m.group(0)!r}; no comparisons of fixes or approaches")

    def hazard(self, where: str, v) -> None:
        """where_models_fail items: a fact about the code in a named file, never a candidate fix."""
        if not self.policy or not isinstance(v, str):
            return
        m = FIRST_WORD.match(v)
        if m and m.group(1).lower().endswith("ing") and m.group(1).lower() not in NOT_GERUNDS:
            self.err(where, f"starts with {m.group(1)!r}, which reads as an approach; start with the code element "
                            "the hazard is in and state it as a fact")
        self.no_comparison(where, v)
        if not FILE_NAME.search(v):
            self.err(where, "names no file; say which file the hazard is in")

    def question(self, where: str, v) -> None:
        if self.policy and isinstance(v, str) and not v.rstrip().endswith("?"):
            self.err(where, "must be a question ending in '?'")


# --- blocks ----------------------------------------------------------------------------

def _overview(v: _V, w: str, b: dict) -> None:
    v.text(f"{w}.what", b.get("what"), 60)
    v.text(f"{w}.users", b.get("users"), 30)
    v.strings(f"{w}.stack", b.get("stack"), 6, min_items=1, max_items=MAX_STACK)
    size = v.obj(f"{w}.size", b.get("size"))
    if size is not None:
        v.count(f"{w}.size.source_files", size.get("source_files"))
        v.count(f"{w}.size.test_files", size.get("test_files"))
        v.strings(f"{w}.size.languages", size.get("languages"), 4, min_items=1)
    run = v.obj(f"{w}.run", b.get("run"))
    if run is not None:
        for k in ("setup", "test"):
            for i, c in enumerate(v.lst(f"{w}.run.{k}", run.get(k))):
                if not isinstance(c, str) or not c.strip():
                    v.err(f"{w}.run.{k}[{i}]", "must be a command string")
                elif EM_DASH in c:
                    v.err(f"{w}.run.{k}[{i}]", "contains an em dash")
        v.text(f"{w}.run.notes", run.get("notes"), 40, required=False)


def _architecture(v: _V, w: str, b: dict) -> None:
    v.text(f"{w}.summary", b.get("summary"), 50)
    ids = set()
    for i, n in enumerate(v.lst(f"{w}.nodes", b.get("nodes"), 1, MAX_NODES)):
        nw = f"{w}.nodes[{i}]"
        if v.obj(nw, n) is None:
            continue
        nid = n.get("id")
        if not isinstance(nid, str) or not nid.strip():
            v.err(f"{nw}.id", "missing")
        elif nid in ids:
            v.err(f"{nw}.id", f"duplicate node id {nid!r}")
        else:
            ids.add(nid)
        v.text(f"{nw}.label", n.get("label"), 4)
        v.enum(f"{nw}.kind", n.get("kind"), NODE_KINDS)
        v.paths(f"{nw}.paths", n.get("paths"), min_items=0)
        v.text(f"{nw}.note", n.get("note"), 15, required=False)
    for i, e in enumerate(v.lst(f"{w}.edges", b.get("edges"))):
        ew = f"{w}.edges[{i}]"
        if v.obj(ew, e) is None:
            continue
        for end in ("from", "to"):
            if e.get(end) not in ids:
                v.err(f"{ew}.{end}", f"{e.get(end)!r} is not a node id")
        v.text(f"{ew}.label", e.get("label"), 5, required=False)


def _flow(v: _V, w: str, b: dict) -> None:
    v.text(f"{w}.summary", b.get("summary"), 30)
    for i, s in enumerate(v.lst(f"{w}.steps", b.get("steps"), 1, MAX_STEPS)):
        sw = f"{w}.steps[{i}]"
        if v.obj(sw, s) is None:
            continue
        v.text(f"{sw}.label", s.get("label"), 8)
        v.pointer(f"{sw}.pointer", s.get("pointer"))
        v.text(f"{sw}.note", s.get("note"), 20, required=False)


def _subsystems(v: _V, w: str, b: dict) -> None:
    for i, s in enumerate(v.lst(f"{w}.items", b.get("items"), 1)):
        sw = f"{w}.items[{i}]"
        if v.obj(sw, s) is None:
            continue
        v.text(f"{sw}.name", s.get("name"), 4)
        v.paths(f"{sw}.paths", s.get("paths"))
        v.text(f"{sw}.what", s.get("what"), 20)
        v.enum(f"{sw}.health", s.get("health"), HEALTH)
        for j, p in enumerate(v.lst(f"{sw}.evidence", s.get("evidence"), 1)):
            v.pointer(f"{sw}.evidence[{j}]", p)


def _opportunity(v: _V, w: str, b: dict) -> None:
    v.text(f"{w}.title", b.get("title"), 10)
    v.text(f"{w}.area", b.get("area"), 4)
    v.text(f"{w}.ambition", b.get("ambition"), 45)
    v.no_directive(f"{w}.ambition", b.get("ambition"))
    v.text(f"{w}.why_not_done", b.get("why_not_done"), 25)
    for i, e in enumerate(v.lst(f"{w}.evidence", b.get("evidence"), 1)):
        ew = f"{w}.evidence[{i}]"
        if v.obj(ew, e) is None:
            continue
        v.pointer(f"{ew}.pointer", e.get("pointer"))
        v.text(f"{ew}.fact", e.get("fact"), 22)
    v.strings(f"{w}.where_models_fail", b.get("where_models_fail"), 25, min_items=1)
    for i, s in enumerate(b.get("where_models_fail") or [] if isinstance(b.get("where_models_fail"), list) else []):
        v.hazard(f"{w}.where_models_fail[{i}]", s)
    bt = v.obj(f"{w}.bounded_task", b.get("bounded_task"))
    if bt is not None:
        v.boolean(f"{w}.bounded_task.exists", bt.get("exists"))
        need = bt.get("exists") is True
        v.text(f"{w}.bounded_task.shape", bt.get("shape"), 35, required=need)
        v.no_directive(f"{w}.bounded_task.shape", bt.get("shape"))
        if need or bt.get("size") is not None:
            v.enum(f"{w}.bounded_task.size", bt.get("size"), SIZES)
        v.text(f"{w}.bounded_task.check_in_session", bt.get("check_in_session"), 30, required=need)
    for i, s in enumerate(v.lst(f"{w}.slot_fit", b.get("slot_fit"), 1)):
        v.slot(f"{w}.slot_fit[{i}]", s)
    os_ = v.obj(f"{w}.one_shot", b.get("one_shot"))
    if os_ is not None:
        v.enum(f"{w}.one_shot.likelihood", os_.get("likelihood"), LIKELIHOOD)
        v.text(f"{w}.one_shot.why", os_.get("why"), 25)
    up = v.obj(f"{w}.upstream", b.get("upstream"))
    if up is not None:
        for i, r in enumerate(v.lst(f"{w}.upstream.related", up.get("related"))):
            rw = f"{w}.upstream.related[{i}]"
            if v.obj(rw, r) is None:
                continue
            url = r.get("url")
            if not isinstance(url, str) or not re.match(r"^https://github\.com/\S+$", url):
                v.err(f"{rw}.url", f"{url!r} must be an https://github.com/ link")
            t = r.get("title")
            if not isinstance(t, str) or not t.strip():
                v.err(f"{rw}.title", "missing")
            elif len(t) > 200:
                v.err(f"{rw}.title", "longer than 200 characters")
            v.enum(f"{rw}.state", r.get("state"), UPSTREAM_STATES)
        v.boolean(f"{w}.upstream.looks_solved", up.get("looks_solved"))
    sc = v.obj(f"{w}.self_contained", b.get("self_contained"))
    if sc is not None:
        v.boolean(f"{w}.self_contained.ok", sc.get("ok"))
        v.strings(f"{w}.self_contained.needs", sc.get("needs", []), 6)


def _code(v: _V, w: str, b: dict) -> None:
    v.pointer(f"{w}.pointer", b.get("pointer"))
    v.text(f"{w}.caption", b.get("caption"), 20)


def _facts(v: _V, w: str, b: dict) -> None:
    for i, f in enumerate(v.lst(f"{w}.items", b.get("items"), 1)):
        fw = f"{w}.items[{i}]"
        if v.obj(fw, f) is None:
            continue
        v.text(f"{fw}.label", f.get("label"), 4)
        v.text(f"{fw}.value", f.get("value"), 20)


def _callout(v: _V, w: str, b: dict) -> None:
    v.enum(f"{w}.tone", b.get("tone"), TONES)
    v.text(f"{w}.text", b.get("text"), 40)


def _questions(v: _V, w: str, b: dict) -> None:
    v.strings(f"{w}.items", b.get("items"), 25, min_items=1, max_items=MAX_QUESTIONS)
    for i, q in enumerate(b.get("items") if isinstance(b.get("items"), list) else []):
        v.question(f"{w}.items[{i}]", q)


def _html(v: _V, w: str, b: dict) -> None:
    h = b.get("html")
    if not isinstance(h, str) or not h.strip():
        v.err(f"{w}.html", "missing")
    else:
        if len(h.encode()) > MAX_HTML_BYTES:
            v.err(f"{w}.html", f"{len(h.encode())} bytes, limit {MAX_HTML_BYTES}")
        for rx, msg in HTML_BANNED:
            if rx.search(h):
                v.err(f"{w}.html", msg)
    v.text(f"{w}.caption", b.get("caption"), 20)


CHECKS = {"overview": _overview, "architecture": _architecture, "flow": _flow, "subsystems": _subsystems,
          "opportunity": _opportunity, "code": _code, "facts": _facts, "callout": _callout,
          "questions": _questions, "html": _html}


# --- document --------------------------------------------------------------------------

def validate(doc, root: Path | None = None, expect: dict | None = None, policy: bool | None = None,
             context: dict | None = None) -> list[str]:
    """Errors in a johto.dossier/1 document. `expect` may pin id, repo and commit; `policy` (default: a root
    was given) adds the text-policy checks; `context` adds the repo-fact checks on slot labels."""
    v = _V(root, policy=root is not None if policy is None else policy, context=context)
    if not isinstance(doc, dict):
        return ["document: must be a JSON object"]
    if doc.get("schema") != SCHEMA:
        v.err("schema", f"must be {SCHEMA!r}")
    for k in ("id", "repo", "commit", "pinned_url", "generated_at", "model", "effort"):
        if not isinstance(doc.get(k), str) or not doc.get(k):
            v.err(k, "missing")
    for k, want in (expect or {}).items():
        if doc.get(k) != want:
            v.err(k, f"must be {want!r}, got {doc.get(k)!r}")
    commit = doc.get("commit")
    if isinstance(commit, str) and not SHA.match(commit):
        v.err("commit", "must be a 40-character lowercase hex sha")
    pu = doc.get("pinned_url")
    if isinstance(pu, str) and isinstance(commit, str) and commit not in pu:
        v.err("pinned_url", "must contain the commit")
    for k in ("elapsed_s", "cost_usd"):
        x = doc.get(k)
        if x is not None and (not isinstance(x, (int, float)) or isinstance(x, bool) or x < 0):
            v.err(k, "must be a number >= 0 or null")
    v.text("headline", doc.get("headline"), 16)
    vd = v.obj("verdict", doc.get("verdict"))
    if vd is not None:
        v.enum("verdict.promise", vd.get("promise"), PROMISE)
        v.text("verdict.why", vd.get("why"), 30)
        for i, s in enumerate(v.lst("verdict.best_fits", vd.get("best_fits"), 1)):
            v.slot(f"verdict.best_fits[{i}]", s, why_words=20)

    blocks = v.lst("blocks", doc.get("blocks"), 1, MAX_BLOCKS)
    opp_ids: set[str] = set()
    n_opps = 0
    for i, b in enumerate(blocks):
        t = b.get("type") if isinstance(b, dict) else None
        w = f"blocks[{i}]" + (f" ({t} {b.get('id')})" if t == "opportunity" else f" ({t})" if t else "")
        if not isinstance(b, dict):
            v.err(w, "must be an object")
            continue
        if t not in BLOCK_TYPES:
            v.err(f"{w}.type", f"{t!r} is not one of {', '.join(BLOCK_TYPES)}")
            continue
        if "title" in b and b["title"] is not None:
            v.text(f"{w}.title", b["title"], TITLE_WORDS)
        if t == "opportunity":
            n_opps += 1
            oid = b.get("id")
            if not isinstance(oid, str) or not oid.strip():
                v.err(f"{w}.id", "missing")
            elif oid in opp_ids:
                v.err(f"{w}.id", f"duplicate opportunity id {oid!r}")
            else:
                opp_ids.add(oid)
        CHECKS[t](v, w, b)
    if isinstance(doc.get("blocks"), list) and not (MIN_OPPS <= n_opps <= MAX_OPPS):
        v.err("blocks", f"has {n_opps} opportunity blocks, needs {MIN_OPPS} to {MAX_OPPS}")
    return v.errors


def opportunities(doc: dict) -> list[dict]:
    return [b for b in (doc or {}).get("blocks", []) if isinstance(b, dict) and b.get("type") == "opportunity"]
