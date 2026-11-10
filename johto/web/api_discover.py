"""Find work and repo brief API (spec.md 3.2 to 3.4 and 7.2): GET /api/discover, GET /api/discover/{id},
GET /api/slots/map.

Owned by the `discover` builder. Mounted by app.py through EXTENSIONS (session, Origin and demo guards come for
free). GET handlers also answer in demo mode (cookie johto_mode=demo): from the fixture dossiers the demo ships
(and the real open slots, as the demo always used), never from Austin's tasks or research. The claim itself stays
POST /api/claim in api_actions.py, extended with `origin`.

Briefs are read through johto.dossier.brief (load, state, enrich, fallback), written by the brief builder. It is
imported lazily and each call has a small local stand-in, so this module works before that one exists. First-layer
payloads carry no machine detail (spec rule 5): code-like text from a dossier goes through johto.plain.hide_code.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .. import config, workspace

FIXTURES = Path(__file__).parent / "fixtures" / "dossiers"
ACTIONS = ("Create", "Modify", "Operate", "Understand / Assess")
SCALES = ("Enterprise-grade", "Personal-scale")
VERDICT_RANK = {"dive": 0, "maybe": 1, None: 2, "skip": 3}
SCARCE_AT = 3                      # this many open slots or fewer: take it soon (Find work's "1 slot left")
DECLINE_WORDS = {"too_easy": "Too easy", "too_big": "Too big", "cant_run": "Can't run it",
                 "not_interested": "Not interested", "ai_generated": "AI-written code", "other": "Other reason"}
DONE_STAGES = {"in_review": "in review", "accepted": "accepted", "rejected": "rejected"}


def err(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "details": details or {}}}, status_code=status)


def _demo(req: Request) -> bool:
    return req.cookies.get("johto_mode") == "demo"


def _store():
    from ..dossier import store
    return store


# --- shared helpers (lazy imports of the brief builder's modules) ---------------------------------------

def _brief_mod():
    try:
        from ..dossier import brief as m   # the brief builder's module; may not exist yet
        return m
    except Exception:
        return None


_CODE = [
    re.compile(r"`[^`]*`"),
    re.compile(r"\b[\w.-]+(?:/[\w.-]+)*\.[A-Za-z]{1,5}:\d+(?:-\d+)?\b"),            # path:line
    re.compile(r"\b(?:[\w.-]+/){2,}[\w.-]*"),                                        # a/b/c
    re.compile(r"\b[\w.-]+/[\w-]+\.[a-z]{1,5}\b"),                                    # dir/file.ext
    re.compile(r"\b[\w-]+\.(?:py|js|ts|tsx|jsx|go|rb|php|java|rs|json|ya?ml|toml|sql|sh|md|vue|cs|cpp|rego)\b"),
    re.compile(r"(?i)\b(?:issue|pr|pull request)\s+#?\d+\b"),
    re.compile(r"#\d+\b"),
    re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b"),                                # commit hashes
    re.compile(r"\b\w+(?:\.\w+)+\(\)"), re.compile(r"\b\w+\(\)"),                  # calls
    re.compile(r"\b[A-Za-z]\w*(?:::\w+)+"),                                          # Ns::thing
    re.compile(r"\b[A-Za-z][A-Za-z0-9]*\.[a-z_]\w*\b"),                               # Class.method
    re.compile(r"\b[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+\b"),                                # snake_case, UPPER_CASE
    re.compile(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b"),                                       # camelCase
    re.compile(r"\b(?:line|lines)\s+\d+(?:\s*(?:-|to)\s*\d+)?\b"),
]


# drop_commands and plain_title live in johto.plain (shared with Now and the task page); kept here by name.
from ..plain import drop_commands, plain_title  # noqa: E402,F401


def hide_code(text, min_words: int = 5, strict: bool = False) -> str:
    """Code tokens and command sentences removed (spec 7.5). johto.plain.hide_code when it exists; else the same
    rules here. "" when fewer than five words survive."""
    text = drop_commands(text)
    try:
        from ..plain import hide_code as real
        return real(text, min_words=min_words, strict=strict)
    except Exception:
        pass
    s = str(text or "")
    for rx in _CODE:
        s = rx.sub(" ", s)
    s = re.sub(r"\(\s*[,;:]*\s*\)", " ", s)
    s = re.sub(r"\s+([,.;:)])", r"\1", s)
    s = re.sub(r"([(])\s+", r"\1", s)
    s = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" ,;:-")
    s = re.sub(r"\s+[—–]\s+", ", ", s)
    return s if len(re.findall(r"[A-Za-z]{2,}", s)) >= min_words else ""


def plain_or(text, fallback="") -> str:
    """hide_code(text), else the fallback (for short titles that would vanish)."""
    return hide_code(text) or fallback


def _read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _sha16(p: Path | None) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p else None
    except OSError:
        return None


def _brief_path(did: str, demo: bool) -> Path:
    return (FIXTURES if demo else _store().P.dir) / f"{did}.brief.json"


def _dossier_file(did: str, demo: bool) -> Path:
    return (FIXTURES if demo else _store().P.dir) / f"{did}.json"


def load_brief(did: str, demo: bool = False) -> dict | None:
    if not demo:
        m = _brief_mod()
        if m is not None and hasattr(m, "load"):
            try:
                doc = m.load(did)
                if doc is None or isinstance(doc, dict):
                    return doc
            except Exception:
                pass
    doc = _read_json(_brief_path(did, demo))
    return doc if isinstance(doc, dict) and doc.get("schema") == "johto.brief/1" else None


def brief_state(did: str, demo: bool, doc: dict | None) -> str:
    """ready | stale | generating | queued | missing | failed (brief.state, or the file and its dossier sha)."""
    if not demo:
        m = _brief_mod()
        if m is not None and hasattr(m, "state"):
            try:
                s = m.state(did)
                if s in ("ready", "generating", "queued", "missing", "failed", "stale"):
                    return s
            except Exception:
                pass
    if doc is None:
        return "missing"
    sha = _sha16(_dossier_file(did, demo))
    return "stale" if sha and doc.get("dossier_sha") and doc["dossier_sha"] != sha else "ready"


def brief_progress(did: str) -> dict | None:
    m = _brief_mod()
    fn = getattr(m, "progress", None) if m else None
    if fn is None:
        return None
    try:
        return fn(did)
    except Exception:
        return None


# --- slots -----------------------------------------------------------------------------------------------

class Slots:
    """data/slots.json as open counts per slot group and per variant."""

    def __init__(self):
        raw = _read_json(config.DATA / "slots.json") or {}
        self.synced_at = raw.get("synced_at")
        self.combos: dict[tuple, int] = {}
        self.variants: dict[tuple, list[dict]] = {}
        self.cells: list[dict] = []      # every open slot kind, all five fields (Find work's filters count these)
        for g in raw.get("groups") or []:
            s = g.get("slot") or {}
            k = (s.get("action"), s.get("domain"), s.get("codebase_type"))
            if None in k:
                continue
            n = int(g.get("count") or 0)
            self.combos[k] = self.combos.get(k, 0) + n
            self.variants.setdefault(k, []).append(
                {"prompt_style": s.get("prompt_style"), "interaction_mode": s.get("interaction_mode"), "open": n})
            if n:
                self.cells.append({"action": k[0], "domain": k[1], "codebase_type": k[2],
                                   "prompt_style": s.get("prompt_style"), "interaction_mode": s.get("interaction_mode"),
                                   "open": n})
        for v in self.variants.values():
            v.sort(key=lambda x: -x["open"])

    def fit(self, a, d, c) -> dict:
        k = (a, d, c)
        return {"action": a, "domain": d, "codebase_type": c, "open": self.combos.get(k, 0),
                "variants": [dict(v) for v in self.variants.get(k, [])]}

    def fits(self, keys, drop_zero: bool = False) -> list[dict]:
        seen, out = set(), []
        for k in keys:
            if None in k or k in seen:
                continue
            seen.add(k)
            f = self.fit(*k)
            if drop_zero and not f["open"]:
                continue
            out.append(f)
        out.sort(key=lambda f: -f["open"])
        return out


def _key(f: dict) -> tuple:
    return (f.get("action"), f.get("domain"), f.get("codebase_type"))


def key_str(f: dict) -> str:
    return "|".join(str(x) for x in _key(f))


def synced_words(iso: str | None, now: float | None = None) -> str:
    if not iso:
        return "Not synced yet"
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except ValueError:
        try:
            t = datetime.fromisoformat(iso).timestamp()
        except ValueError:
            return "Not synced yet"
    s = max(0, (now or time.time()) - t)
    if s < 90:
        return "Synced just now"
    if s < 3600:
        m = round(s / 60)
        return f"Synced {m} minutes ago"
    if s < 86400:
        h = round(s / 3600)
        return f"Synced {h} hour{'s' if h != 1 else ''} ago"
    d = round(s / 86400)
    return "Synced yesterday" if d == 1 else f"Synced {d} days ago"


# --- dossiers and tasks ------------------------------------------------------------------------------------

def _opps(doc: dict | None) -> list[dict]:
    return [b for b in (doc or {}).get("blocks") or [] if b.get("type") == "opportunity"]


def _pairs(doc: dict | None) -> list[tuple]:
    """(domain, codebase type) pairs the dossier names, in its order."""
    out = []
    src = list(((doc or {}).get("verdict") or {}).get("best_fits") or [])
    for o in _opps(doc):
        src += list(o.get("slot_fit") or [])
    for f in src:
        p = (f.get("domain"), f.get("codebase_type"))
        if None not in p and p not in out:
            out.append(p)
    return out


def _dossier_keys(doc: dict | None) -> list[tuple]:
    src = list(((doc or {}).get("verdict") or {}).get("best_fits") or [])
    for o in _opps(doc):
        src += list(o.get("slot_fit") or [])
    return [_key(f) for f in src]


def _name(doc: dict | None, brief: dict | None, repo: str) -> str:
    """The product's name: the brief's, else the repo's name with a capital when it has none."""
    if brief and brief.get("name"):
        return brief["name"]
    n = (repo or "").split("/")[-1] or repo or ""
    return n[:1].upper() + n[1:] if n == n.lower() else n


def _stage_word(meta: dict) -> tuple[str, str] | None:
    """(stage, words) from task.json alone, cheap enough for every list call. None for a task that is gone."""
    if meta.get("discard") or meta.get("discarded") or meta.get("unclaimed_at"):
        return None
    st = meta.get("studio") or {}
    status = st.get("status")
    if status == "Accepted":
        return "accepted", "accepted"
    if status == "Rejected":
        return "rejected", "rejected"
    if status in ("Awaiting Review", "In Review"):
        return "in_review", "in review"
    from .domain.derive import sent_back_since
    if sent_back_since(meta) is not None:
        return "sent_back", "sent back"
    if not status and meta.get("submitted_at"):
        return "in_review", "in review"
    return "in_flight", "in progress"


def _task_problem(label: str | None) -> str:
    s = str(label or "")
    at = s.find(" · ")
    p = s[at + 3:] if at >= 0 else ""
    return "" if re.match(r"^Task [a-z0-9]{6,}$", p, re.I) else p


def load_tasks() -> list[dict]:
    """One small record per task folder: repo, dossier and origin, label, stage word."""
    out = []
    for p in sorted(config.TASKS.glob("*/task.json")):
        meta = _read_json(p)
        if not isinstance(meta, dict):
            continue
        sw = _stage_word(meta)
        if sw is None:
            continue
        origin = meta.get("origin") if isinstance(meta.get("origin"), dict) else {}
        dz = meta.get("dossier") if isinstance(meta.get("dossier"), dict) else {}
        short_repo = ((meta.get("repo") or {}).get("full_name") or "").split("/")[-1]
        out.append({"short": p.parent.name, "repo": ((meta.get("repo") or {}).get("full_name") or "").lower(),
                    "dossier_id": origin.get("dossier_id") or dz.get("id"),
                    "opportunity_id": origin.get("opportunity_id") or dz.get("opportunity_id"),
                    "place_id": origin.get("place_id") if origin.get("kind") == "place" else None,
                    "label": meta.get("label") or (f"{short_repo} · {lt}" if (lt := _lead_title(meta)) else None),
                    "slot": meta.get("slot") or {},
                    "stage": sw[0], "stage_word": sw[1]})
    return out


def demo_tasks() -> list[dict]:
    """The demo's own in-flight tasks in load_tasks()'s shape, so demo briefs show what is already yours."""
    try:
        from . import demo as demo_mod
        leads = {x["id"]: x for x in json.loads((config.DATA / "scout" / "leads.json").read_text())}
    except Exception:
        return []
    out = []
    for row in getattr(demo_mod, "TASKS", []):
        try:
            short, _tid, lead_id, slot_t, _stage, label = row
        except ValueError:
            continue
        lead = leads.get(lead_id) or {}
        out.append({"short": short, "repo": str(lead.get("repo") or "").lower(), "dossier_id": None,
                    "opportunity_id": None, "place_id": None, "label": label,
                    "slot": dict(zip(("action", "domain", "codebase_type"), slot_t)),
                    "stage": "in_flight", "stage_word": "in progress"})
    return out


def _tasks_for(did: str, repo: str, tasks: list[dict]) -> list[dict]:
    r = (repo or "").lower()
    return [t for t in tasks if t["dossier_id"] == did or (r and t["repo"] == r)]


_STOP = set("that this with from into when what which where while their there these those have been were will "
            "would could should does done each only also than then them they your ours about after before over "
            "under every other some more most much many make sure want need just know".split())


def _stem(w: str) -> str:
    for suf in ("ings", "ing", "ies", "ied", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def _wordset(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _STOP}


def _action_of(a: str | None) -> str:
    return (a or "").split("/")[0].strip().lower()


def match_seed(action: str | None, text: str, seeds: list[dict]) -> dict | None:
    """The seed a task with no recorded origin most likely came from: a seed whose slot fits the task's action
    and that shares the most words with his opening prompt and goal (at least two, and more than any other
    such seed). None when nothing clearly matches."""
    act = _action_of(action)
    cands = [x for x in seeds or [] if any(_action_of(f.get("action")) == act for f in x.get("fits") or [])]
    mine = _wordset(text)
    if not cands or not mine:
        return None
    scored = sorted(((len(mine & _wordset(f"{x.get('title') or ''} {x.get('problem') or ''}")), n, x)
                     for n, x in enumerate(cands)), key=lambda v: (-v[0], v[1]))
    best = scored[0][0]
    if best < 2 or (len(scored) > 1 and scored[1][0] == best):
        return None
    return scored[0][2]


def _lead_title(meta: dict) -> str:
    """The title of the scout lead a task was claimed from ("" when it has none)."""
    lid = (meta or {}).get("lead_id")
    if not lid:
        return ""
    try:
        leads = json.loads((config.DATA / "scout" / "leads.json").read_text())
    except (OSError, ValueError):
        return ""
    return next((str(x.get("title") or "") for x in leads if isinstance(x, dict) and x.get("id") == lid), "")


def _task_text(short: str) -> str:
    """His opening prompt and goal, plus the title of the lead it was claimed from, for matching a task to the
    seed it came from. A task written in Studio keeps only its prompt here, so the lead title matters."""
    meta = _read_json(config.TASKS / short / "task.json")
    lead = _lead_title(meta if isinstance(meta, dict) else {})
    try:
        sub = workspace.parse_submission(config.TASKS / short)
    except Exception:  # noqa: BLE001
        return lead
    secs = sub.get("sections") or {}
    return " ".join([*(secs.get(k, "") for k in ("Opening Prompt", "High-Level Task Goal")), lead])


def _used(did: str, repo: str, tasks: list[dict], brief: dict | None) -> list[dict]:
    seeds = {s.get("opportunity_id"): s for s in (brief or {}).get("seeds") or []}
    places = {p.get("id"): p for p in (brief or {}).get("places") or []}
    out = []
    for t in _tasks_for(did, repo, tasks):
        if t["place_id"]:
            kind, ident = "place", t["place_id"]
        elif t["opportunity_id"] and t["dossier_id"] == did:
            kind, ident = "seed", t["opportunity_id"]
        else:
            kind, ident = "repo", None
            # A task claimed outside the brief (no origin recorded): the seed it matches counts as used.
            hit = match_seed(t["slot"].get("action"), t.get("text") if "text" in t else _task_text(t["short"]),
                             list(seeds.values()))
            if hit:
                kind, ident = "seed", hit.get("opportunity_id")
        title = (seeds.get(ident) or {}).get("title") if kind == "seed" else \
            (places.get(ident) or {}).get("name") if kind == "place" else None
        out.append({"kind": kind, "id": ident, "short": t["short"], "label": title or _task_problem(t["label"]) or None,
                    "action": t["slot"].get("action"), "stage": t["stage"], "stage_word": t["stage_word"]})
    return out


def _yours(did: str, repo: str, tasks: list[dict]) -> dict:
    mine = _tasks_for(did, repo, tasks)
    done = sum(1 for t in mine if t["stage"] in DONE_STAGES)
    return {"done": done, "in_flight": len(mine) - done}


# --- the brief, enriched --------------------------------------------------------------------------------

def _facts(doc: dict | None) -> dict:
    """Repo-level facts; `fails` lists only what fails, in words (passing rules stay silent)."""
    needs, fails = [], []
    paid = gpu = False
    for o in _opps(doc):
        sc = o.get("self_contained") or {}
        needs += [n for n in sc.get("needs") or [] if n not in needs]
    run = next((b.get("run") or {} for b in (doc or {}).get("blocks") or [] if b.get("type") == "overview"), {})
    text = " ".join([*needs, run.get("notes") or ""]).lower()
    if re.search(r"\b(paid|subscription|license key|licensed|api key|saas account|billing)\b", text):
        paid = True
        fails.append("Needs a paid service")
    if re.search(r"\b(gpu|cuda)\b", text):
        gpu = True
        fails.append("Tests need a GPU")
    ai = None
    for b in (doc or {}).get("blocks") or []:
        if b.get("type") == "callout" and re.search(r"\bAI\b", b.get("title") or ""):
            ai = not re.match(r"^\s*No\b", b.get("title") or "") and b.get("tone") == "warn"
    if ai:
        fails.append("Has AI-written code")
    return {"runs_locally": bool(run.get("setup") or run.get("test")) or None, "needs_paid_service": paid,
            "needs_gpu": gpu, "needs": [n for n in needs if re.match(r"^[A-Za-z][\w ,.-]*$", n) and len(n.split()) <= 6][:4],
            "ai_code": ai, "fails": fails}


def repo_facts(doc: dict | None) -> dict:
    """brief.facts when the module exists (its `warnings` become `fails`), else the local rules."""
    m = _brief_mod()
    if doc and m is not None and hasattr(m, "facts"):
        try:
            f = dict(m.facts(doc))
            f.setdefault("fails", list(f.get("warnings") or []))
            return f
        except Exception:
            pass
    return _facts(doc)


def _upstream(o: dict | None) -> list[dict]:
    up = (o or {}).get("upstream") or {}
    rows = [{"title": plain_title(r.get("title")), "url": r.get("url"), "state": r.get("state") or None}
            for r in up.get("related") or [] if r.get("url")]
    rows.sort(key=lambda r: r["state"] != "open")
    return rows


def opp_title(o: dict) -> str:
    """A research problem's title for a first layer: code hidden, else its area, else a plain stand-in."""
    t = hide_code(o.get("title"), min_words=2, strict=True)
    if t and not re.search(r"[`()\[\]{}<>=_]|\w\.\w", t):
        return t
    a = hide_code(o.get("area"), min_words=1) or ""
    return (a[:1].upper() + a[1:]) or "A problem the research found"


def _opp_view(o: dict, slots: Slots) -> dict:
    """A dossier opportunity in plain words for a card or the inspector (no paths, code or numbers)."""
    bt = o.get("bounded_task") or {}
    return {"opportunity_id": o.get("id"), "title": opp_title(o),
            "area": plain_or(o.get("area"), ""), "summary": hide_code(o.get("ambition")) or None,
            "one_shot": (o.get("one_shot") or {}).get("likelihood") or "not_checked", "size": bt.get("size"),
            "fits": slots.fits([_key(f) for f in o.get("slot_fit") or []]), "upstream": _upstream(o)}


def _local_fallback(doc: dict) -> dict:
    """spec 6.8: the brief's shape from the dossier alone."""
    v = doc.get("verdict") or {}
    return {"name": _name(doc, None, doc.get("repo") or ""), "one_liner": doc.get("headline") or "",
            "list_line": None, "verdict": {"level": None, "lede": hide_code(v.get("why")) or None},
            "how_it_works": None, "heading": [], "places": [],
            "seeds": [{"opportunity_id": o.get("id"), "title": o.get("title"), "area": o.get("area")}
                      for o in _opps(doc)[:3]]}


_PLUMBING = re.compile(r"\b(?:docker|tests?|testable|sink|fixtures?|harness|CI|ground truth|emulat\w*)\b", re.I)


def _no_plumbing(text) -> str:
    """The research verdict without its clauses about how things were tested (the fallback lede)."""
    parts = re.split(r"(?<=[.;])\s+", str(text or "").strip())
    keep = [p for p in parts if p and not _PLUMBING.search(p)]
    out = " ".join(keep).strip()
    return re.sub(r";$", ".", out)


def fallback(doc: dict) -> dict:
    m = _brief_mod()
    fb = None
    if m is not None and hasattr(m, "fallback"):
        try:
            fb = m.fallback(doc)
        except Exception:
            fb = None
    fb = dict(fb) if isinstance(fb, dict) else _local_fallback(doc)
    fb["name"] = _name(doc, None, doc.get("repo") or "")
    v = dict(fb.get("verdict") or {})
    v["lede"] = hide_code(_no_plumbing(v.get("lede"))) or None
    fb["verdict"] = v
    fb["one_liner"] = drop_commands(fb.get("one_liner")) or None
    return fb


def enrich(brief: dict, doc: dict, slots: Slots, tasks: list[dict], did: str, demo: bool) -> dict:
    """The read-time fields of spec 6.3, from brief.enrich when it exists; anything it leaves out is filled here."""
    out = None
    m = _brief_mod()
    if not demo and m is not None and hasattr(m, "enrich"):
        try:
            raw_slots = _read_json(config.DATA / "slots.json") or {}
            # Tasks are matched here (_used), so the module gets none and skips reading every task.json.
            out = m.enrich(brief, doc, raw_slots, [])
        except Exception:
            out = None
    out = json.loads(json.dumps(out if isinstance(out, dict) else brief))
    opps = {o.get("id"): o for o in _opps(doc)}
    pairs = _pairs(doc)
    for s in out.get("seeds") or []:
        o = opps.get(s.get("opportunity_id")) or {}
        s.setdefault("one_shot", (o.get("one_shot") or {}).get("likelihood") or "not_checked")
        if isinstance(s.get("one_shot"), dict):
            s["one_shot"] = s["one_shot"].get("likelihood") or "not_checked"
        s.setdefault("size", (o.get("bounded_task") or {}).get("size"))
        if not s.get("fits") or not all("variants" in f for f in s["fits"]):
            s["fits"] = slots.fits([_key(f) for f in o.get("slot_fit") or []])
        det = s.get("detail")
        if isinstance(det, dict):
            # A brief written before the rule (spec 6.5): a funded-team or reviewer line that names how the fix
            # would be built is left out, so the fix never sits in front of him before he writes his prompt.
            try:
                from ..dossier.brief import names_mechanism
            except Exception:  # noqa: BLE001
                names_mechanism = None
            if names_mechanism:
                for k in ("funded_team", "reviewer_check"):
                    if names_mechanism(det.get(k)):
                        det.pop(k, None)
        if "upstream" not in s:
            s["upstream"] = _upstream(o)
        else:
            s["upstream"] = [{**u, "title": plain_title(u.get("title"))} for u in s["upstream"] or [] if u.get("url")]
    for p in out.get("places") or []:
        if not p.get("fits") or not all("variants" in f for f in p["fits"]):
            keys = [(a, d, c) for a in p.get("actions") or [] for d, c in pairs]
            p["fits"] = slots.fits(keys, drop_zero=True)
        p["fits"] = [f for f in p["fits"] if f.get("open")][:4]
    facts = out.get("facts") if isinstance(out.get("facts"), dict) else repo_facts(doc)
    if "fails" not in facts:
        facts = {**facts, "fails": list(facts.get("warnings") or [])}
    out["facts"] = facts
    out["stale"] = bool(out.get("stale"))
    out.setdefault("used", None)
    return out


def _seed_keys(brief: dict | None, doc: dict | None) -> list[tuple]:
    """Every slot group a seed or place fits (the fits strip and chip filtering)."""
    keys = []
    opps = {o.get("id"): o for o in _opps(doc)}
    for s in (brief or {}).get("seeds") or []:
        keys += [_key(f) for f in s.get("fits") or (opps.get(s.get("opportunity_id")) or {}).get("slot_fit") or []]
    for p in (brief or {}).get("places") or []:
        keys += [_key(f) for f in p.get("fits") or []]
    return keys


# --- GET /api/discover -----------------------------------------------------------------------------------

def _items(demo: bool) -> tuple[dict, list[tuple[dict, dict | None]]]:
    """(index, [(item, dossier)]) for the real research, or the demo's fixture dossiers."""
    if demo:
        pairs = []
        for p in sorted(FIXTURES.glob("*.json")):
            if p.name.endswith(".brief.json"):
                continue
            d = _read_json(p)
            if not isinstance(d, dict) or not d.get("id"):
                continue
            pairs.append(({"id": d["id"], "repo": d.get("repo"), "status": "ready", "rank": len(pairs) + 1,
                           "finished_at": d.get("generated_at"), "decline": None, "actioned": None}, d))
        return {"target": 10, "paused": False, "items": [i for i, _ in pairs]}, pairs
    store = _store()
    idx = store.load()
    pairs = [(it, store.load_dossier(it["id"]) if it.get("status") in ("ready", "actioned", "declined") else None)
             for it in idx.get("items") or []]
    return idx, pairs


def _row(it: dict, doc: dict | None, slots: Slots, tasks: list[dict], demo: bool) -> dict:
    did, repo = it["id"], (doc or {}).get("repo") or it.get("repo") or ""
    if it.get("status") == "generating" or doc is None:
        return {"id": did, "name": _name(None, None, repo), "owner": repo.split("/")[0], "repo": repo,
                "one_liner": None, "list_line": None, "tags": [], "problems": [], "top_fits": [], "scales": [],
                "verdict": None,
                "state": "researching", "generating": True, "yours": _yours(did, repo, tasks), "fits_any": [],
                "seeds": [], "fit_keys": [], "open_seeds": 0, "scarce": False, "status": it.get("status")}
    brief = load_brief(did, demo)
    bstate = brief_state(did, demo, brief)
    if brief is not None:
        b = enrich(brief, doc, slots, tasks, did, demo)
        keys = _seed_keys(b, doc)
        seed_keys = [_key(f) for s in b.get("seeds") or [] for f in s.get("fits") or []]
        row = {"one_liner": b.get("one_liner"), "list_line": b.get("list_line"),
               "tags": [p.get("name") for p in (b.get("places") or [])[:3] if p.get("name")],
               "verdict": (b.get("verdict") or {}).get("level"), "state": "ready", "generating": False, "problems": []}
        top = slots.fits(seed_keys or keys, drop_zero=True)[:2]
        # Each row lists its suggestions with the slot groups they fit, so the page names the problem a slot comes
        # from and recounts it under his filters. Seeds already his are left out.
        used = {u.get("id") for u in _used(did, repo, tasks, b) if u.get("kind") == "seed"}
        counted, seeds = [], []
        for x in b.get("seeds") or []:
            if x.get("opportunity_id") in used or not x.get("title"):
                continue
            # Every slot group the seed fits, so the page can recount it under his filters.
            seeds.append({"title": x["title"], "seed": x.get("opportunity_id"),
                          "keys": list(dict.fromkeys(key_str(f) for f in x.get("fits") or []))})
            best = max((f.get("open") or 0 for f in x.get("fits") or []), default=0)
            if best:
                counted.append(best)
        row["seeds"] = seeds
    else:
        fb = fallback(doc)
        keys = _dossier_keys(doc)
        claimed = {t.get("opportunity_id") for t in _tasks_for(did, repo, tasks) if t.get("dossier_id") == did}
        row = {"one_liner": fb.get("one_liner") or doc.get("headline"), "list_line": None, "tags": [],
               "problems": [opp_title(o) for o in _opps(doc) if o.get("id") not in claimed][:2],
               "verdict": None, "state": "brief_coming", "generating": bstate in ("generating", "queued"),
               "seeds": []}
        top = slots.fits(keys, drop_zero=True)[:2]
        seed_keys, counted = keys, []
    allfits = slots.fits(keys)
    # Each suggestion's largest open group (or each fitting group, for a row with no seeds yet).
    counted = counted or [f["open"] for f in slots.fits(keys, drop_zero=True)]
    return {"id": did, "name": _name(doc, brief, repo), "owner": repo.split("/")[0], "repo": repo, **row,
            "top_fits": [{k: f[k] for k in ("action", "domain", "codebase_type", "open")} for f in top],
            "scales": [s.replace("-grade", "").replace("-scale", "") for s in SCALES
                       if any(f["codebase_type"] == s for f in allfits)],
            "yours": _yours(did, repo, tasks), "fits_any": sorted({key_str(f) for f in allfits}),
            "fit_keys": list(dict.fromkeys("|".join(str(x) for x in k) for k in (seed_keys or keys) if None not in k)),
            "open_seeds": len(counted), "scarce": any(n <= SCARCE_AT for n in counted), "status": it.get("status")}


def _sort_rows(rows: list[dict]) -> list[dict]:
    """Ready first, then the verdict, then a suggestion with only a few slots left (someone else can take them), then
    how many suggestions have an open slot, then the name. One open slot is enough: a big group never ranks higher.
    Now's idle pick follows this order too. Find work reorders the same way under his filters."""
    state = {"ready": 0, "brief_coming": 1, "researching": 2}
    return sorted(rows, key=lambda r: (state.get(r["state"], 3), VERDICT_RANK.get(r["verdict"], 2),
                                       0 if r["scarce"] else 1, -r["open_seeds"], r["name"].lower()))


def _lede(rows: list[dict]) -> dict:
    alldive = [r for r in rows if r["state"] == "ready" and r["verdict"] == "dive"]
    dive = alldive[:2]
    ready = [r for r in rows if r["state"] in ("ready", "brief_coming")]
    if len(alldive) > 2:
        # Many look good: naming two would rank them by nothing but the order.
        title, support = f"{len(alldive)} repos look worth diving into.", None
    elif len(dive) == 2:
        title = f"{dive[0]['name']} and {dive[1]['name']} look strongest today."
        support = None
    elif len(dive) == 1:
        title = f"{dive[0]['name']} looks strongest today."
        support = None
    elif ready:
        n = len(ready)
        title = f"{'One repo is' if n == 1 else f'{n} repos are'} ready to look at."
        support = None
    elif any(r["state"] == "researching" for r in rows):
        title, support = "Research is under way. Nothing is ready yet.", None
    else:
        title, support = "No repos are researched yet.", None
    return {"title": title, "support": support}


def _research(idx: dict, pairs: list, demo: bool) -> dict:
    items = [i for i, _ in pairs]
    gen = next((i for i in items if i.get("status") == "generating"), None)
    ready = sum(1 for i in items if i.get("status") == "ready")
    target = idx.get("target") or 10
    paused = bool(idx.get("paused"))
    w = {}
    if not demo:
        try:
            from ..dossier import worker
            w = worker.status() or {}
        except Exception:
            w = {}
    name = _name(None, None, gen.get("repo") or "") if gen else None
    if gen:
        line = f"Researching {name} now."
    elif paused:
        line = "Research is paused."
    elif w.get("backoff_until"):
        line = "Research is waiting a while before the next repo."
    elif ready >= target:
        line = f"Research rests while {target} repos are ready."
    else:
        line = f"Research is filling up: {ready} of {target} ready."
    model = w.get("model") or "claude"
    return {"paused": paused, "ready": ready, "target": target, "generating": name,
            "queued": sum(1 for i in items if i.get("status") == "queued"), "today_line": line,
            "model": model, "model_word": MODEL_WORDS.get(model, model)}


def _set_aside(pairs: list) -> list[dict]:
    out = []
    for it, doc in pairs:
        st = it.get("status")
        if st not in ("declined", "failed"):
            continue
        repo = (doc or {}).get("repo") or it.get("repo") or ""
        if st == "declined":
            why = DECLINE_WORDS.get((it.get("decline") or {}).get("reason"), "Set aside")
        else:
            why = "Research didn't finish"
        out.append({"id": it["id"], "name": _name(doc, None, repo), "owner": repo.split("/")[0], "why": why,
                    "status": st, "at": it.get("closed_at") or it.get("finished_at")})
    out.sort(key=lambda r: r.get("at") or "", reverse=True)
    return out[:30]


def discover(demo: bool) -> dict:
    slots = Slots()
    tasks = demo_tasks() if demo else load_tasks()
    idx, pairs = _items(demo)
    rows = [_row(it, doc, slots, tasks, demo) for it, doc in pairs
            if it.get("status") in ("ready", "actioned") or it.get("status") == "generating"]
    rows = _sort_rows(rows)
    return {"lede": _lede(rows),
            "slots": slots.cells,
            "open_total": sum(slots.combos.values()),
            "repos": rows, "research": _research(idx, pairs, demo), "set_aside": [] if demo else _set_aside(pairs)}


# --- GET /api/discover/{id} -------------------------------------------------------------------------------

def _find(did: str, demo: bool) -> tuple[dict, dict | None]:
    if demo:
        p = FIXTURES / f"{did}.json"
        d = _read_json(p) if re.match(r"^[a-z0-9._-]+$", did) and p.exists() else None
        if not isinstance(d, dict):
            raise KeyError(did)
        return {"id": did, "repo": d.get("repo"), "status": "ready"}, d
    store = _store()
    it = store.get(did)
    if it is None:
        raise KeyError(did)
    doc = store.load_dossier(did) if it.get("status") in ("ready", "actioned", "declined") else None
    return it, doc


def detail(did: str, demo: bool) -> dict:
    it, doc = _find(did, demo)
    repo = (doc or {}).get("repo") or it.get("repo") or ""
    base = {"id": did, "repo": repo, "owner": repo.split("/")[0], "github_url": f"https://github.com/{repo}",
            "status": it.get("status"), "decline": it.get("decline"),
            "decline_words": DECLINE_WORDS.get((it.get("decline") or {}).get("reason")) if it.get("decline") else None}
    if doc is None:
        state = {"generating": "researching", "queued": "queued", "failed": "failed"}.get(it.get("status"), "missing")
        return {**base, "name": _name(None, None, repo), "state": state, "brief": None, "fits": [], "used": [],
                "also_researched": [], "generated_at": None, "drill": {"dossier_available": False},
                "progress": (it.get("progress") or {}).get("step") and {"step": it["progress"]["step"]} or None}
    slots = Slots()
    tasks = demo_tasks() if demo else load_tasks()
    brief = load_brief(did, demo)
    bstate = brief_state(did, demo, brief)
    if brief is not None:
        b = enrich(brief, doc, slots, tasks, did, demo)
        b["stale"] = b.get("stale") or bstate == "stale"
        state = "stale" if bstate == "stale" else "ready"
        seed_ids = {s.get("opportunity_id") for s in b.get("seeds") or []}
    else:
        b = fallback(doc)
        b["seeds"] = [_opp_view(o, slots) for o in _opps(doc) if o.get("id") in
                      {s.get("opportunity_id") for s in b.get("seeds") or []}][:3] or \
            [_opp_view(o, slots) for o in _opps(doc)[:3]]
        b["facts"] = repo_facts(doc)
        state = bstate if bstate in ("generating", "queued", "failed") else "missing"
        seed_ids = {s["opportunity_id"] for s in b["seeds"]}
    used = _used(did, repo, tasks, b)
    b["used"] = used
    keys = _seed_keys(b, doc) or _dossier_keys(doc)
    fits = slots.fits(keys, drop_zero=True) or slots.fits(keys)
    also = [_opp_view(o, slots) for o in _opps(doc) if o.get("id") not in seed_ids]
    return {**base, "name": _name(doc, brief, repo), "state": state, "brief": b, "fits": fits, "used": used,
            "also_researched": also, "generated_at": (brief or {}).get("generated_at") or doc.get("generated_at"),
            "drill": {"dossier_available": True},
            "progress": brief_progress(did) if state in ("generating", "queued") else None}


# --- GET /api/slots/map -----------------------------------------------------------------------------------

def slot_map(demo: bool) -> dict:
    slots = Slots()
    d = discover(demo)
    by_key: dict[str, list[str]] = {}
    for r in d["repos"]:
        if r["state"] == "researching":
            continue
        for k in r["fits_any"]:
            by_key.setdefault(k, []).append(r["id"])
    totals: dict[str, int] = {}
    for (a, dom, c), n in slots.combos.items():
        totals[dom] = totals.get(dom, 0) + n
    domains = sorted(totals, key=lambda x: -totals[x])
    actions = [a for a in ACTIONS if any(k[0] == a for k in slots.combos)] + \
        sorted({k[0] for k in slots.combos} - set(ACTIONS))
    cells = []
    for c in SCALES:
        for a in actions:
            for dom in domains:
                n = slots.combos.get((a, dom, c), 0)
                cells.append({"action": a, "domain": dom, "codebase_type": c, "open": n,
                              "repos": by_key.get(f"{a}|{dom}|{c}", [])})
    names = {r["id"]: r["name"] for r in d["repos"]}
    return {"synced_at": slots.synced_at, "synced_words": synced_words(slots.synced_at), "actions": actions,
            "domains": domains, "scales": list(SCALES), "cells": cells, "names": names,
            "open_total": sum(slots.combos.values())}


# --- GET /api/research: every researched problem, one row each ---------------------------------------------

SIZE_RANK = {"L": 0, "M": 1, "S": 2}
MODEL_WORDS = {"claude": "Claude Code", "codex": "Codex CLI"}


def researcher(doc: dict | None) -> str:
    """Who wrote a dossier, from its model field: "GPT" (an OpenAI model, through the Codex CLI) or "Claude"."""
    m = str((doc or {}).get("model") or "").lower()
    return "GPT" if m.startswith(("gpt", "o1", "o3", "o4", "codex")) else "Claude"
ONE_SHOT_RANK = {"unlikely": 0, "possible": 1, "likely": 2, "not_checked": 3}


def _overview(doc: dict | None) -> dict:
    b = next((b for b in (doc or {}).get("blocks") or [] if b.get("type") == "overview"), {})
    size = b.get("size") or {}
    return {"languages": [l for l in size.get("languages") or [] if isinstance(l, str)][:3],
            "source_files": size.get("source_files") if isinstance(size.get("source_files"), int) else None}


def _problem_rows(it: dict, doc: dict, slots: Slots, tasks: list[dict], demo: bool) -> list[dict]:
    """One row per opportunity in a dossier: the brief's words when it has them, the dossier's otherwise."""
    did, repo = it["id"], doc.get("repo") or it.get("repo") or ""
    brief = load_brief(did, demo)
    b = enrich(brief, doc, slots, tasks, did, demo) if brief is not None else None
    seeds = {x.get("opportunity_id"): x for x in (b or {}).get("seeds") or []}
    mine = {u.get("id"): u for u in _used(did, repo, tasks, b) if u.get("kind") == "seed" and u.get("id")}
    facts = (b or {}).get("facts") if isinstance((b or {}).get("facts"), dict) else repo_facts(doc)
    ov = _overview(doc)
    verdict = ((b or {}).get("verdict") or {}).get("level")
    promise = (doc.get("verdict") or {}).get("promise")
    name = _name(doc, brief, repo)
    who = researcher(doc)
    out = []
    for o in _opps(doc):
        oid = o.get("id")
        seed = seeds.get(oid) or {}
        view = _opp_view(o, slots)
        fits = seed.get("fits") if seed.get("fits") and all("variants" in f for f in seed["fits"]) else view["fits"]
        traps = [t for t in o.get("where_models_fail") or [] if isinstance(t, str)]
        trap = seed.get("models_miss") or (hide_code(traps[0]) if traps else None) or None
        up = view["upstream"]
        used = mine.get(oid)
        out.append({
            "id": f"{did}:{oid}", "dossier_id": did, "opportunity_id": oid,
            "repo": repo, "owner": repo.split("/")[0], "name": name,
            "verdict": verdict, "promise": promise, "seed": oid in seeds,
            "title": seed.get("title") or view["title"],
            "problem": seed.get("problem") or view["summary"],
            "trap": trap, "area": view["area"],
            "one_shot": view["one_shot"], "size": view["size"],
            "fits": fits,
            "fit_keys": list(dict.fromkeys(key_str(f) for f in fits)),
            "actions": list(dict.fromkeys(f.get("action") for f in fits if f.get("action"))),
            "domains": list(dict.fromkeys(f.get("domain") for f in fits if f.get("domain"))),
            "scales": list(dict.fromkeys(f.get("codebase_type") for f in fits if f.get("codebase_type"))),
            "open": max((int(f.get("open") or 0) for f in fits), default=0),
            "traps": len(traps), "evidence": len(o.get("evidence") or []),
            "needs": [n for n in (o.get("self_contained") or {}).get("needs") or [] if isinstance(n, str)][:4],
            "upstream": len(up), "upstream_open": sum(1 for u in up if u.get("state") == "open"),
            "yours": {"short": used["short"], "stage": used.get("stage"), "stage_word": used.get("stage_word")} if used else None,
            "languages": ov["languages"], "source_files": ov["source_files"],
            "fails": list(facts.get("fails") or []),
            "generated_at": (brief or {}).get("generated_at") or doc.get("generated_at"),
            "researcher": who,
        })
    return out


def _research_lede(rows: list[dict], repos: int) -> dict:
    if not rows:
        return {"title": "No problems are researched yet.", "support": None}
    hard = sum(1 for r in rows if r["one_shot"] == "unlikely")
    withslot = sum(1 for r in rows if r["open"])
    n, k = len(rows), repos
    title = f"{n} researched problem{'s' if n != 1 else ''} across {k} repo{'s' if k != 1 else ''}."
    parts = []
    if hard:
        parts.append(f"{hard} where Claude is unlikely to get it in one go")
    parts.append(f"{withslot} with an open slot right now" if withslot else "none with an open slot right now")
    support = ", ".join(parts) + "."
    return {"title": title, "support": support[:1].upper() + support[1:]}


def research(demo: bool) -> dict:
    """Every problem the research found, across every ready repo, flat: the research dashboard's rows."""
    slots = Slots()
    tasks = demo_tasks() if demo else load_tasks()
    _, pairs = _items(demo)
    rows: list[dict] = []
    repos = 0
    for it, doc in pairs:
        if doc is None or it.get("status") not in ("ready", "actioned"):
            continue
        repos += 1
        rows += _problem_rows(it, doc, slots, tasks, demo)
    rows.sort(key=lambda r: (VERDICT_RANK.get(r["verdict"], 2), ONE_SHOT_RANK.get(r["one_shot"], 3),
                             0 if r["seed"] else 1, SIZE_RANK.get(r["size"], 3), -r["open"], r["name"].lower(), r["opportunity_id"]))
    return {"lede": _research_lede(rows, repos), "slots": slots.cells, "synced": synced_words(slots.synced_at),
            "repos": repos, "problems": rows}


# --- routes ------------------------------------------------------------------------------------------------

async def get_research(req: Request):
    return JSONResponse(await asyncio.to_thread(research, _demo(req)))


async def get_discover(req: Request):
    return JSONResponse(await asyncio.to_thread(discover, _demo(req)))


async def get_detail(req: Request):
    did = req.path_params["id"]
    try:
        return JSONResponse(await asyncio.to_thread(detail, did, _demo(req)))
    except KeyError:
        return err(404, "not_found", "That repo isn't in the research list.")


async def get_slot_map(req: Request):
    return JSONResponse(await asyncio.to_thread(slot_map, _demo(req)))


routes: list = [
    Route("/api/discover", get_discover),
    Route("/api/discover/{id}", get_detail),
    Route("/api/slots/map", get_slot_map),
    Route("/api/research", get_research),
]


# --- for POST /api/claim (api_actions.claim) -----------------------------------------------------------------

def claim_label(doc: dict, origin: dict) -> tuple[str, dict]:
    """The task label and the stored origin for a claim from a seed or a place (spec 7.2). Raises ValueError."""
    did = doc.get("id")
    brief = load_brief(did) if did else None
    name = _name(doc, brief, doc.get("repo") or "")
    kind = origin.get("kind")
    if kind == "seed":
        oid = origin.get("opportunity_id")
        opp = next((o for o in _opps(doc) if o.get("id") == oid), None)
        if opp is None:
            raise ValueError(f"No problem {oid} in this repo's research.")
        seed = next((s for s in (brief or {}).get("seeds") or [] if s.get("opportunity_id") == oid), None)
        title = (seed or {}).get("title") or opp_title(opp)
        return f"{name} · {title}", {"kind": "seed", "dossier_id": did, "opportunity_id": oid, "title": title}
    if kind == "place":
        pid = origin.get("place_id")
        if not isinstance(pid, str) or not re.match(r"^p[1-9]$", pid):
            raise ValueError("A place needs its id (p1 to p9).")
        place = next((p for p in (brief or {}).get("places") or [] if p.get("id") == pid), None)
        pname = (place or {}).get("name") or str(origin.get("name") or "").strip()[:80]
        if not pname:
            raise ValueError("A place needs its name.")
        return f"{name} · {pname}", {"kind": "place", "dossier_id": did, "place_id": pid, "name": pname}
    raise ValueError("origin.kind must be seed or place.")
