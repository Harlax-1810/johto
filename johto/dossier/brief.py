"""The repo brief (redesign spec 6): what Austin needs from a dossier to decide in seconds, in product words.

data/dossiers/<id>.brief.json holds one johto.brief/1 document per dossier (schema in brief_prompt.SCHEMA_JSON).
It is written by headless Claude (opus, medium effort) from the dossier and a few GitHub facts fetched here, with
WebSearch and WebFetch as its only tools (for what rival products have). The output is validated (schema, word
limits, the never-list of spec 6.5, ids that exist in the dossier); one repair pass in the same session; then the
file is written atomically. A rival whose source URL did not appear in the run's web results is dropped, never
kept on trust. Generations share store.generation_lock with the dossier generator, so the two never overlap.

data/dossiers/briefs.json tracks the queue (queued, generating, failed) across processes, with a flock like the
dossier index. runs/<id>.brief.log is each generation's log. A brief that is overwritten moves to
runs/<id>.brief.<time>.json first, so the overlap guard still knows every brief text Austin may have read.

Read side: state(did), load(did), enrich(doc, dossier, slots, tasks) adds the server's read-time facts (open slot
counts, one-shot, size, upstream threads, what Austin already used), fallback(dossier) is the dossier-only shape
the API serves while no brief exists, and view(did) is the GET /api/briefs/{id} payload.
"""
from __future__ import annotations

import base64
import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .. import config
from ..plain import hide_code, problems, words
from . import brief_prompt, schema, store
from . import generate as _gen

SCHEMA_ID = "johto.brief/1"
MODEL = "opus"
EFFORT = "medium"
TIMEOUT_S = 10 * 60
REPAIR_TIMEOUT_S = 6 * 60
MIN_REPAIR_S = 60
BEGIN, END = brief_prompt.BEGIN, brief_prompt.END
TOOLS = ["WebSearch", "WebFetch"]
DENIED_TOOLS = ["Bash", "Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "NotebookEdit", "Task", "Agent",
                "TodoWrite"]
STATES = ("ready", "generating", "queued", "missing", "failed", "stale")
CLAUDE_BIN: Path | None = None   # None: the dossier generator's binary (JOHTO_CLAUDE_BIN). Tests point it at a stub.
WATCH_S = 2.0
GH_TIMEOUT_S = 40

Progress = Callable[[dict], None]
Fetch = Callable[[dict], dict]

# Keys whose strings are not prose the model wrote for Austin: ids, enums, dates, URLs, provenance.
_NOT_PROSE = {"opportunity_id", "near_seed", "id", "when", "source", "actions", "from", "level"}
_QUESTIONS_OK = {("places", "questions"), ("detail", "check_by_hand")}
# One thought per line: no semicolon stapling two facts together in these short fields.
_ONE_THOUGHT = {("name",), ("one_liner",), ("verdict", "lede"), ("heading", "what"), ("seeds", "title"),
                ("places", "name")}
# How the research or a reviewer reproduces something belongs only in the two reviewer fields.
_REPRO_OK = {"reviewer_check", "check_by_hand"}
# The first-layer lines say why the product is interesting, never that its problems reproduce: the page shows
# "runs locally" as a fact, and the sandbox is assumed.
_FIRST_LAYER = {("one_liner",), ("list_line",), ("verdict", "lede")}
_WHERE_IT_RUNS = re.compile(r"\b(reproduc\w*|locally|in Docker|in a sandbox)\b", re.I)
# What a fix would be made of (spec 6.5, the one-shot paradox): funded_team and reviewer_check say what users get
# and what a reviewer sees, never the mechanism, which would hand the fix to his prompt and so to Claude.
MECHANISM = re.compile(r"\b(?:server[- ]side|client[- ]side|enforcement|index(?:es|ed|ing)?|re-?index\w*|rebuild(?:s|ing)?|"
                       r"rebuilt|migrations?|upgrade steps?|clean[- ]?ups?|report mode|dry[- ]run|schemas?|endpoints?|"
                       r"middleware|backfill\w*|rewrit(?:e|es|ten|ing)|refactor\w*|shared rule|algorithms?|parsers?|"
                       r"tokeni[sz]\w*|transactions?|dedup\w*|checksums?|hashes|hashing|regex\w*|debounc\w*|in place|"
                       r"per[- ]request|single source of truth|one shared|word splitting|splitting|idempoten\w*|mutex|"
                       r"foreign keys?|caching|retry logic|enrollment-only session|session scope)\b", re.I)
_NO_MECHANISM = {"funded_team", "reviewer_check"}


def names_mechanism(text: str | None) -> str | None:
    """The first mechanism word in a funded_team or reviewer_check text, or None."""
    m = MECHANISM.search(str(text or ""))
    return m.group(0) if m else None


_REPRO = re.compile(r"\b((?:mail|smtp|email|local) sinks?|seed data|mock(?:s|ed)? (?:servers?|services?|providers?)|"
                    r"stub(?:s|bed)?|test (?:servers?|harness(?:es)?|doubles?))\b", re.I)


# --- files ---------------------------------------------------------------------------------------------

def path(did: str) -> Path:
    return store.P.dir / f"{did}.brief.json"


def log_path(did: str) -> Path:
    store.P.runs.mkdir(parents=True, exist_ok=True)
    return store.P.runs / f"{did}.brief.log"


def history(did: str) -> list[Path]:
    """Earlier versions of this brief (moved aside when a new one was written)."""
    return sorted(store.P.runs.glob(f"{did}.brief.*.json")) if store.P.runs.exists() else []


def _status_path() -> Path:
    return store.P.dir / "briefs.json"


def _status_lock() -> Path:
    return store.P.dir / ".briefs.lock"


def dossier_ids() -> list[str]:
    """Every dossier on disk (data/dossiers/<id>.json)."""
    if not store.P.dir.exists():
        return []
    out = []
    for p in sorted(store.P.dir.glob("*__*__*.json")):
        if p.name.endswith(".brief.json") or p.name.startswith("."):
            continue
        out.append(p.name[:-5])
    return out


def dossier_sha(did: str) -> str | None:
    """sha256 of the dossier file's bytes, first 16 hex (spec 6.3)."""
    try:
        return hashlib.sha256(store.dossier_path(did).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def load(did: str) -> dict | None:
    try:
        doc = json.loads(path(did).read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == SCHEMA_ID else None


def _write(doc: dict) -> Path:
    """Atomic write; the version it replaces moves to runs/ for the overlap guard."""
    p = path(doc["dossier_id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        store.P.runs.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(p.stat().st_mtime))
        dest = store.P.runs / f"{doc['dossier_id']}.brief.{stamp}.json"
        n = 1
        while dest.exists():
            dest = store.P.runs / f"{doc['dossier_id']}.brief.{stamp}-{n}.json"
            n += 1
        shutil.copy2(p, dest)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    os.replace(tmp, p)
    return p


# --- the queue (briefs.json) -------------------------------------------------------------------------------

def _load_status() -> dict:
    try:
        data = json.loads(_status_path().read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("items", {})
    return data


@contextlib.contextmanager
def _edit_status():
    store.P.dir.mkdir(parents=True, exist_ok=True)
    with open(_status_lock(), "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = _load_status()
            yield data
            tmp = _status_path().with_name(f".briefs.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(data, indent=1))
            os.replace(tmp, _status_path())
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def status(did: str) -> dict:
    return dict(_load_status()["items"].get(did) or {})


def _set(did: str, **fields) -> dict:
    with _edit_status() as data:
        rec = data["items"].setdefault(did, {})
        rec.update(fields)
        return dict(rec)


def _rank() -> dict[str, int]:
    try:
        return {it["id"]: it.get("rank") or 10**6 for it in store.load()["items"]}
    except Exception:
        return {}


def queue(did: str, *, force: bool = False, reason: str = "") -> bool:
    """Ask for a brief. False when it is already queued or generating (a force upgrades a queued one)."""
    with _edit_status() as data:
        rec = data["items"].setdefault(did, {})
        if rec.get("status") == "generating" and store.generation_busy():
            return False
        if rec.get("status") == "queued":
            rec["force"] = bool(rec.get("force") or force)
            return False
        rec.update(status="queued", queued_at=store.now_iso(), force=bool(force), reason=reason or None,
                   progress=None)
    return True


def queue_missing() -> list[str]:
    """Queue briefs for ready dossiers with none or a stale one (spec 7.1, the worker's start). A brief that
    failed for this same dossier is not queued again on its own."""
    try:
        items = store.load()["items"]
    except Exception:
        return []
    out = []
    for it in items:
        did = it["id"]
        if it["status"] != "ready" or not store.dossier_path(did).exists():
            continue
        st = state(did)
        if st in ("ready", "queued", "generating"):
            continue
        if st == "failed" and status(did).get("dossier_sha") == dossier_sha(did):
            continue
        if queue(did, reason="no brief" if st in ("missing", "failed") else "the dossier changed"):
            out.append(did)
    return out


def requeue_stale() -> list[str]:
    """'generating' records left by a dead process go back to 'queued'. Only while nothing generates."""
    with store.generation_lock(blocking=False) as got:
        if not got:
            return []
        moved = []
        with _edit_status() as data:
            for did, rec in data["items"].items():
                if rec.get("status") == "generating":
                    rec.update(status="queued", progress=None, error="interrupted; queued again")
                    moved.append(did)
            data.pop("stop", None)
        return moved


def next_queued() -> str | None:
    """The queued brief to run next: forced ones first, then by the dossier's rank."""
    items = _load_status()["items"]
    rank = _rank()
    q = [(not rec.get("force"), rank.get(did, 10**6), rec.get("queued_at") or "", did)
         for did, rec in items.items() if rec.get("status") == "queued" and store.dossier_path(did).exists()]
    return min(q)[3] if q else None


def unqueue(did: str) -> None:
    with _edit_status() as data:
        rec = data["items"].get(did)
        if rec and rec.get("status") == "queued":
            rec.update(status="failed" if not load(did) else "ready", error="removed from the queue")


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def state(did: str) -> str:
    """ready | generating | queued | missing | failed | stale."""
    rec = status(did)
    st = rec.get("status")
    if st == "generating":
        if did in _current:
            return "generating"
        pid = rec.get("pid")
        if pid and pid != os.getpid() and _pid_alive(pid) and store.generation_busy():
            return "generating"
        return "queued"
    if st == "queued":
        return "queued"
    doc = load(did)
    if doc is not None:
        cur = dossier_sha(did)
        return "stale" if cur and doc.get("dossier_sha") != cur else "ready"
    if st == "failed":
        return "failed"
    return "missing"


# --- stop requests and running generations ----------------------------------------------------------------

_current: dict[str, _gen.Cancel] = {}
_current_lock = threading.Lock()


def current() -> str | None:
    with _current_lock:
        return next(iter(_current), None)


def cancel_running(reason: str) -> list[str]:
    """Stop the brief generations running in this process. Their briefs go back to the queue."""
    with _current_lock:
        running = list(_current.items())
    for _, c in running:
        c.cancel(reason)
    return [d for d, _ in running]


def request_stop(reason: str) -> list[str]:
    """Ask every process generating a brief to stop (worker pause)."""
    out = []
    with _edit_status() as data:
        for did, rec in data["items"].items():
            if rec.get("status") == "generating":
                data.setdefault("stop", {})[did] = reason
                out.append(did)
    cancel_running(reason)
    return out


def _watch(did: str, cancel: _gen.Cancel, done: threading.Event) -> None:
    while not done.wait(WATCH_S):
        try:
            data = _load_status()
        except Exception:
            continue
        reason = (data.get("stop") or {}).get(did)
        if reason:
            cancel.cancel(reason)
            return


def _clear_stop(did: str) -> None:
    with _edit_status() as data:
        stops = data.get("stop") or {}
        stops.pop(did, None)
        if not stops:
            data.pop("stop", None)


# --- the validator -------------------------------------------------------------------------------------------

_TYPES = {"string": str, "object": dict, "array": list, "boolean": bool, "null": type(None)}


def _type_ok(v, t: str) -> bool:
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, _TYPES[t])


def _date_time_ok(s: str) -> bool:
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return True
    except ValueError:
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?[+-]\d{4}", s))


def _walk(v, sch: dict, where: str, errs: list[str], model: bool, keys: tuple[str, ...]) -> None:
    model = model or sch.get("x-writer") == "model"
    if "const" in sch and v != sch["const"]:
        errs.append(f"{where}: must be {sch['const']!r}")
        return
    if "enum" in sch and v not in sch["enum"]:
        errs.append(f"{where}: {v!r} is not one of {', '.join(map(str, sch['enum']))}")
        return
    t = sch.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not any(_type_ok(v, x) for x in ts):
            errs.append(f"{where}: must be {' or '.join(ts)}, not {type(v).__name__}")
            return
    if isinstance(v, str):
        if "minLength" in sch and len(v) < sch["minLength"]:
            errs.append(f"{where}: {len(v)} characters, at least {sch['minLength']}")
        if "maxLength" in sch and len(v) > sch["maxLength"]:
            errs.append(f"{where}: {len(v)} characters, at most {sch['maxLength']}")
        if "pattern" in sch and not re.search(sch["pattern"], v):
            errs.append(f"{where}: {v[:60]!r} does not match {sch['pattern']}")
        if sch.get("format") == "date-time" and not _date_time_ok(v):
            errs.append(f"{where}: not a date-time")
        if "x-maxWords" in sch and words(v) > sch["x-maxWords"]:
            errs.append(f"{where}: {words(v)} words, limit {sch['x-maxWords']}")
        if model and not (set(keys) & _NOT_PROSE):
            qok = any(a in keys and b in keys for a, b in _QUESTIONS_OK)
            for p in problems(v, questions=qok):
                if keys[-1] == "who" and p.startswith("has a code identifier"):
                    continue    # a rival is a product name: stats.fm, Customer.io
                errs.append(f"{where}: {p}")
            if ";" in v and any(keys[-len(k):] == k and (len(k) > 1 or len(keys) == 1) for k in _ONE_THOUGHT):
                errs.append(f"{where}: has a semicolon (one thought per line)")
            m = _WHERE_IT_RUNS.search(v)
            if m and keys in _FIRST_LAYER:
                errs.append(f"{where}: says where its problems show up ({m.group(0)}); the page shows that already")
            if keys and keys[-1] in _NO_MECHANISM and names_mechanism(v):
                errs.append(f"{where}: names how a fix would be built ({names_mechanism(v)}); say what users get or "
                            "what a reviewer sees, never the mechanism")
            m = _REPRO.search(v)
            if m and not (set(keys) & _REPRO_OK):
                errs.append(f"{where}: says how it is reproduced ({m.group(0)}); only reviewer_check and "
                            "check_by_hand may")
    elif isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in sch and v < sch["minimum"]:
            errs.append(f"{where}: below {sch['minimum']}")
    elif isinstance(v, list):
        if "minItems" in sch and len(v) < sch["minItems"]:
            errs.append(f"{where}: {len(v)} items, at least {sch['minItems']}")
        if "maxItems" in sch and len(v) > sch["maxItems"]:
            errs.append(f"{where}: {len(v)} items, at most {sch['maxItems']}")
        if sch.get("uniqueItems"):
            seen = [json.dumps(x, sort_keys=True) for x in v]
            if len(set(seen)) != len(seen):
                errs.append(f"{where}: has repeated items")
        if isinstance(sch.get("items"), dict):
            for i, x in enumerate(v):
                _walk(x, sch["items"], f"{where}[{i}]", errs, model, keys)
    elif isinstance(v, dict):
        props = sch.get("properties") or {}
        for k in sch.get("required") or []:
            if k not in v:
                errs.append(f"{where}: missing {k}")
        if sch.get("additionalProperties") is False:
            for k in v:
                if k not in props:
                    errs.append(f"{where}: unknown field {k}")
        for k, x in v.items():
            if k in props:
                _walk(x, props[k], f"{where}.{k}" if where else k, errs, model, keys + (k,))


def validate(doc, dossier: dict | None) -> list[str]:
    """Errors in a brief, as short lines (empty when it may be shown): the schema with its word limits, the
    never-list in every model-written string, and ids that must exist in the dossier."""
    if not isinstance(doc, dict):
        return ["the brief must be a JSON object"]
    errs: list[str] = []
    _walk(doc, brief_prompt.SCHEMA, "", errs, False, ())
    if not dossier:
        return errs
    if doc.get("dossier_id") not in (None, dossier.get("id")):
        errs.append(f"dossier_id: {doc.get('dossier_id')!r} is not this dossier ({dossier.get('id')})")
    if doc.get("repo") not in (None, dossier.get("repo")):
        errs.append(f"repo: {doc.get('repo')!r} is not {dossier.get('repo')}")
    opp_ids = {o.get("id") for o in schema.opportunities(dossier)}
    seeds = [s for s in doc.get("seeds") or [] if isinstance(s, dict)]
    seed_ids = [s.get("opportunity_id") for s in seeds]
    for i, oid in enumerate(seed_ids):
        if oid not in opp_ids:
            errs.append(f"seeds[{i}].opportunity_id: {oid!r} is not an opportunity in the dossier "
                        f"({', '.join(sorted(x for x in opp_ids if x))})")
    if len(set(seed_ids)) != len(seed_ids):
        errs.append("seeds: the same opportunity is used twice")
    places = [p for p in doc.get("places") or [] if isinstance(p, dict)]
    pids = [p.get("id") for p in places]
    if len(set(pids)) != len(pids):
        errs.append("places: two places share an id")
    for i, p in enumerate(places):
        ns = p.get("near_seed")
        if ns is not None and ns not in opp_ids:
            errs.append(f"places[{i}].near_seed: {ns!r} is not an opportunity in the dossier")
        for j, r in enumerate(p.get("rivals") or []):
            if isinstance(r, dict) and not str(r.get("source") or "").startswith(("http://", "https://")):
                errs.append(f"places[{i}].rivals[{j}]: no source URL")
    return errs


# --- read-time enrichment (spec 6.3) ----------------------------------------------------------------------

def _slots_doc() -> dict:
    try:
        return json.loads((store.P.data / "slots.json").read_text())
    except (OSError, ValueError):
        return {"groups": []}


def _short_of(meta: dict, d: Path | None = None) -> str:
    name = meta.get("task_name") or ""
    if name.startswith("Task "):
        return name[5:]
    return meta.get("short") or (d.name if d else "")


def task_metas() -> list[dict]:
    """Every task.json under tasks/, each with its folder name as "short"."""
    out = []
    if not config.TASKS.exists():
        return out
    for p in sorted(config.TASKS.glob("*/task.json")):
        try:
            meta = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict):
            out.append({**meta, "short": meta.get("short") or p.parent.name})
    return out


def _variants_universe(groups: list[dict]) -> list[tuple[str, str]]:
    seen = []
    for g in groups:
        s = g.get("slot") or {}
        k = (s.get("prompt_style"), s.get("interaction_mode"))
        if None not in k and k not in seen:
            seen.append(k)
    return seen


def fits_for(triples, groups: list[dict]) -> list[dict]:
    """[{action, domain, codebase_type, open, variants: [{prompt_style, interaction_mode, open}]}] for the
    (action, domain, codebase_type) triples, open counts from data/slots.json, largest first."""
    uni = _variants_universe(groups)
    out, seen = [], set()
    for t in triples:
        k = (t.get("action"), t.get("domain"), t.get("codebase_type")) if isinstance(t, dict) else tuple(t)
        if None in k or k in seen:
            continue
        seen.add(k)
        counts: dict[tuple, int] = {}
        for g in groups:
            s = g.get("slot") or {}
            if (s.get("action"), s.get("domain"), s.get("codebase_type")) == k:
                v = (s.get("prompt_style"), s.get("interaction_mode"))
                counts[v] = counts.get(v, 0) + int(g.get("count") or 0)
        variants = [{"prompt_style": ps, "interaction_mode": im, "open": counts.get((ps, im), 0)}
                    for ps, im in uni + [v for v in counts if v not in uni]]
        variants.sort(key=lambda v: -v["open"])
        out.append({"action": k[0], "domain": k[1], "codebase_type": k[2], "open": sum(counts.values()),
                    "variants": variants})
    out.sort(key=lambda f: -f["open"])
    return out


def _stage_word(meta: dict) -> str:
    st = ((meta.get("studio") or {}).get("status") or "").lower()
    if st in ("awaiting review", "in review", "in_review", "awaiting_review"):
        return "in review"
    if st == "accepted":
        return "accepted"
    if st == "rejected":
        return "rejected"
    if st == "writing" and (meta.get("studio") or {}).get("feedback"):
        return "sent back"
    return "in flight"


def used(did: str, repo: str, tasks: list[dict]) -> list[dict]:
    """What Austin already claimed on this repo: [{kind: seed|place|repo, id, short, label, stage_word}]."""
    out = []
    for m in tasks or []:
        if (((m.get("repo") or {}).get("full_name")) or "").lower() != (repo or "").lower():
            continue
        if m.get("unclaimed_at") or m.get("discarded_at"):
            continue
        origin = m.get("origin") if isinstance(m.get("origin"), dict) else {}
        dos = m.get("dossier") if isinstance(m.get("dossier"), dict) else {}
        other = (origin.get("dossier_id") or dos.get("id")) not in (None, did)
        kind, oid = "repo", None
        if not other:
            if origin.get("kind") == "place" and origin.get("place_id"):
                kind, oid = "place", origin["place_id"]
            elif origin.get("opportunity_id") or dos.get("opportunity_id"):
                kind, oid = "seed", origin.get("opportunity_id") or dos.get("opportunity_id")
        out.append({"kind": kind, "id": oid, "short": _short_of(m), "label": m.get("label") or repo.split("/")[-1],
                    "stage_word": _stage_word(m)})
    return out


def _overview(dossier: dict) -> dict:
    return next((b for b in dossier.get("blocks") or [] if isinstance(b, dict) and b.get("type") == "overview"), {})


def _ai_code(dossier: dict) -> bool | None:
    """True only when the dossier calls the code mostly AI-written; False when it says there is little or none."""
    for b in dossier.get("blocks") or []:
        if not isinstance(b, dict) or b.get("type") != "callout":
            continue
        title, text = b.get("title") or "", b.get("text") or ""
        if not re.search(r"\bAI\b|AI-|LLM|co-?author", title + " " + text[:80], re.I):
            continue
        if re.search(r"\bmostly AI|\bAI-generated repo|\bmostly (?:LLM|machine)", title + " " + text, re.I):
            return True
        if re.match(r"\W*(?:No|Human)", title) or re.match(r"\W*(?:No|Human)", text) or "minor" in title.lower():
            return False
        return None
    return None


def facts(dossier: dict) -> dict:
    """Repo-level facts (spec 6.3) in words; raw needs and run notes only under "drill"."""
    run = _overview(dossier).get("run") or {}
    notes = run.get("notes") or ""
    if re.search(r"\b(?:ran|passed|passing|pass|green)\b", notes, re.I):
        runs_locally = True
    elif re.search(r"\b(?:could not|couldn't|cannot|can't|failed to|did not|didn't) (?:run|build|start)", notes, re.I):
        runs_locally = False
    else:
        runs_locally = None
    needs, paid = [], False
    for o in schema.opportunities(dossier):
        sc = o.get("self_contained") or {}
        if sc.get("ok") is False:
            paid = True
        for n in sc.get("needs") or []:
            if n not in needs:
                needs.append(n)
    ai = _ai_code(dossier)
    warnings = []
    if paid:
        warnings.append("Needs a paid service")
    if any(re.search(r"\bGPU\b|\bCUDA\b", n) for n in needs):
        warnings.append("Tests need a GPU")
    if runs_locally is False:
        warnings.append("Its tests did not run in research")
    if ai is True:
        warnings.append("Much of the code looks AI-written")
    return {"runs_locally": runs_locally, "needs_paid_service": paid, "ai_code": ai, "warnings": warnings,
            "drill": {"needs": needs, "run_notes": notes}}


def _opp_title(o: dict) -> str:
    """A dossier opportunity's title fit for a first layer: code tokens hidden, else its area."""
    area = o.get("area") or ""
    return hide_code(o.get("title"), min_words=3, strict=True) or (area[:1].upper() + area[1:]) or (o.get("id") or "")


def _pairs(dossier: dict) -> list[tuple[str, str]]:
    src = list((dossier.get("verdict") or {}).get("best_fits") or [])
    for o in schema.opportunities(dossier):
        src += list(o.get("slot_fit") or [])
    out = []
    for f in src:
        k = (f.get("domain"), f.get("codebase_type"))
        if None not in k and k not in out:
            out.append(k)
    return out


def enrich(doc: dict, dossier: dict, slots: dict | list | None = None, tasks: list[dict] | None = None) -> dict:
    """The brief plus the read-time server fields of spec 6.3 (not stored): seeds[].one_shot, size, fits, upstream,
    used; places[].fits, near, used; facts, used, fits (every slot group a seed or place fits), also (the
    dossier's other opportunities) and stale. `slots` is data/slots.json (or its groups), `tasks` the task.json
    dicts (each with "short"); both are loaded when None."""
    out = copy.deepcopy(doc)
    sl = _slots_doc() if slots is None else slots
    groups = sl.get("groups") or [] if isinstance(sl, dict) else list(sl)
    tasks = task_metas() if tasks is None else tasks
    did = doc.get("dossier_id") or dossier.get("id")
    opps = {o.get("id"): o for o in schema.opportunities(dossier)}
    mine = used(did, doc.get("repo") or dossier.get("repo") or "", tasks)
    seed_ids = []
    for s in out.get("seeds") or []:
        o = opps.get(s.get("opportunity_id")) or {}
        seed_ids.append(s.get("opportunity_id"))
        s["one_shot"] = (o.get("one_shot") or {}).get("likelihood")
        s["size"] = (o.get("bounded_task") or {}).get("size")
        s["fits"] = fits_for(o.get("slot_fit") or [], groups)
        s["upstream"] = [{"title": hide_code(u.get("title"), min_words=1) or u.get("title") or "",
                          "url": u.get("url"), "state": u.get("state")}
                         for u in (o.get("upstream") or {}).get("related") or [] if isinstance(u, dict)]
        s["used"] = next((u for u in mine if u["kind"] == "seed" and u["id"] == s.get("opportunity_id")), None)
    pairs = _pairs(dossier)
    seeds_by_id = {s.get("opportunity_id"): s for s in out.get("seeds") or []}
    for p in out.get("places") or []:
        triples = [(a, d, c) for a in p.get("actions") or [] for d, c in pairs]
        p["fits"] = [f for f in fits_for(triples, groups) if f["open"] > 0]
        ns = p.get("near_seed")
        if ns:
            seed = seeds_by_id.get(ns)
            p["near"] = {"opportunity_id": ns, "is_seed": seed is not None,
                         "title": seed.get("title") if seed else _opp_title(opps.get(ns) or {"id": ns})}
        else:
            p["near"] = None
        p["used"] = next((u for u in mine if u["kind"] == "place" and u["id"] == p.get("id")), None)
    allf: dict[tuple, dict] = {}
    for f in [f for s in out.get("seeds") or [] for f in s["fits"]] + \
             [f for p in out.get("places") or [] for f in p["fits"]]:
        allf.setdefault((f["action"], f["domain"], f["codebase_type"]), f)
    out["fits"] = sorted(allf.values(), key=lambda f: -f["open"])
    out["facts"] = facts(dossier)
    out["used"] = mine
    out["also"] = [{"opportunity_id": oid, "title": _opp_title(o)} for oid, o in opps.items()
                   if oid not in seed_ids and oid]
    cur = dossier_sha(did) if did else None
    out["stale"] = bool(cur and doc.get("dossier_sha") != cur)
    return out


def fallback(dossier: dict) -> dict:
    """What the API serves while no brief exists (spec 6.8): from the dossier only, code tokens hidden."""
    ops = [o for o in schema.opportunities(dossier)][:3]
    repo = dossier.get("repo") or ""
    return {"name": repo.split("/")[-1], "repo": repo,
            "one_liner": hide_code(dossier.get("headline"), min_words=3),
            "verdict": {"level": None, "lede": hide_code((dossier.get("verdict") or {}).get("why"), min_words=5)},
            "seeds": [{"opportunity_id": o.get("id"), "title": _opp_title(o),
                       "area": hide_code(o.get("area"), min_words=1) or None} for o in ops],
            "places": [], "heading": []}


# --- the API payloads ---------------------------------------------------------------------------------------

FIXTURES = Path(__file__).resolve().parent.parent / "web" / "fixtures" / "dossiers"

STEP_WORDS = {"starting": "Starting", "fetching": "Reading the project's releases", "researching": "Researching rivals",
              "writing": "Writing the brief", "checking": "Checking the brief", "repairing": "Fixing the brief",
              "done": "Done", "failed": "Stopped", "stopped": "Stopped", "waiting": "Waiting for other research"}


def _error_words(rec: dict) -> str | None:
    e = rec.get("error") or ""
    if not e or rec.get("status") not in ("failed",):
        return None
    if "usage limit" in e:
        return "Claude hit its usage limit. The brief is tried again later."
    if "timed out" in e:
        return "The research took too long and stopped."
    if "validation" in e:
        return "The brief did not pass its checks."
    if "dossier" in e and "no " in e:
        return "The research for this repo is missing."
    return "The brief could not be written."


def _fixture(did: str) -> tuple[dict | None, dict | None]:
    d = FIXTURES / f"{did}.json"
    b = FIXTURES / f"{did}.brief.json"
    try:
        dossier = json.loads(d.read_text()) if d.exists() else None
        doc = json.loads(b.read_text()) if b.exists() else None
    except (OSError, ValueError):
        return None, None
    return dossier, doc


def view(did: str, *, demo: bool = False) -> dict:
    """GET /api/briefs/{id}: {"id", "state", "brief" (enriched) | null, "fallback" | null, "error", "progress"}.
    In demo mode only research files are read (the fixtures first, then the dossiers on disk), never the queue,
    the tasks or anything Austin did. KeyError when there is no dossier."""
    if demo:
        dossier, doc = _fixture(did)
        if dossier is None:
            dossier, doc = store.load_dossier(did), load(did)
        if dossier is None:
            raise KeyError(did)
        brief = enrich(doc, dossier, tasks=[]) if doc else None
        st = ("stale" if brief and brief["stale"] else "ready") if brief else "missing"
        return {"id": did, "state": st, "brief": brief, "fallback": None if brief else fallback(dossier),
                "error": None, "progress": None}
    dossier = store.load_dossier(did)
    if dossier is None:
        raise KeyError(did)
    st = state(did)
    doc = load(did)
    brief = enrich(doc, dossier) if doc else None
    rec = status(did)
    prog = None
    if st == "generating":
        p = rec.get("progress") or {}
        prog = {"step": STEP_WORDS.get(p.get("step"), "Researching"), "elapsed_s": p.get("elapsed_s") or 0}
    return {"id": did, "state": st, "brief": brief, "fallback": None if brief else fallback(dossier),
            "error": _error_words(rec) if st in ("failed", "stale", "ready", "missing") else None,
            "progress": prog}


def progress(did: str) -> dict | None:
    """{"step": words, "elapsed_s"} while this brief is generating, else None."""
    if state(did) != "generating":
        return None
    p = status(did).get("progress") or {}
    return {"step": STEP_WORDS.get(p.get("step"), "Researching"), "elapsed_s": p.get("elapsed_s") or 0}


def items(*, demo: bool = False) -> list[dict]:
    """GET /api/briefs: [{"id", "state", "generated_at", "verdict"}] for every dossier."""
    out = []
    if demo:
        ids = sorted({p.name[:-5] for p in FIXTURES.glob("*.json") if not p.name.endswith(".brief.json")}
                     | set(dossier_ids()))
        for did in ids:
            fx_d, fx_b = _fixture(did)
            doc = fx_b if fx_d is not None else load(did)
            out.append({"id": did, "state": "ready" if doc else "missing",
                        "generated_at": (doc or {}).get("generated_at"),
                        "verdict": ((doc or {}).get("verdict") or {}).get("level")})
        return out
    for did in dossier_ids():
        doc = load(did)
        out.append({"id": did, "state": state(did), "generated_at": (doc or {}).get("generated_at"),
                    "verdict": ((doc or {}).get("verdict") or {}).get("level")})
    return out


# --- generation inputs ---------------------------------------------------------------------------------------

def _gh(args: list[str]) -> str | None:
    if config.FAKE:
        return None   # fake mode never reaches GitHub
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=GH_TIMEOUT_S,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def fetch_inputs(dossier: dict) -> dict:
    """README (first 8,000 characters), releases of the last 24 months and merged PR titles of the last 12, read
    from GitHub with gh. Any failure leaves that input empty."""
    repo = dossier.get("repo") or ""
    out = {"readme": "", "releases": [], "prs": []}
    raw = _gh(["api", f"repos/{repo}/readme"])
    if raw:
        try:
            j = json.loads(raw)
            out["readme"] = base64.b64decode(j.get("content") or "").decode("utf-8", "replace")[:8000]
        except (ValueError, TypeError):
            pass
    now = datetime.now(timezone.utc)
    raw = _gh(["api", f"repos/{repo}/releases?per_page=100"])
    if raw:
        try:
            for r in json.loads(raw):
                when = r.get("published_at") or r.get("created_at") or ""
                if not when or datetime.fromisoformat(when.replace("Z", "+00:00")) < now - timedelta(days=730):
                    continue
                out["releases"].append(f"{when[:10]} {r.get('tag_name') or ''} {(r.get('name') or '').strip()} "
                                       f"{r.get('html_url') or ''}".strip())
        except (ValueError, TypeError, AttributeError):
            pass
    raw = _gh(["pr", "list", "--repo", repo, "--state", "merged", "--limit", "150", "--json", "title,mergedAt"])
    if raw:
        try:
            for p in json.loads(raw):
                when = p.get("mergedAt") or ""
                if when and datetime.fromisoformat(when.replace("Z", "+00:00")) >= now - timedelta(days=365):
                    out["prs"].append(f"{when[:10]} {p.get('title') or ''}")
        except (ValueError, TypeError, AttributeError):
            pass
    return out


def _ctype_ok(dossier: dict) -> set[str]:
    return {f.get("codebase_type") for f in (dossier.get("verdict") or {}).get("best_fits") or []} | \
        {f.get("codebase_type") for o in schema.opportunities(dossier) for f in o.get("slot_fit") or []}


def _input_lines(did: str, dossier: dict, groups: list[dict], tasks: list[dict]) -> dict:
    mine = used(did, dossier.get("repo") or "", tasks)
    used_ids = {u["id"] for u in mine if u["kind"] == "seed"}
    opp_lines = []
    for o in schema.opportunities(dossier):
        fits = fits_for(o.get("slot_fit") or [], groups)
        opp_lines.append(f"{o.get('id')}: {o.get('title')} | one-shot {(o.get('one_shot') or {}).get('likelihood')}"
                         f" | size {(o.get('bounded_task') or {}).get('size')} | open slots "
                         + (", ".join(f"{f['action']} / {f['domain']} / {f['codebase_type']}: {f['open']}"
                                      for f in fits) or "none")
                         + (" | ALREADY USED by Austin" if o.get("id") in used_ids else ""))
    ctypes = _ctype_ok(dossier)
    combos: dict[tuple, int] = {}
    for g in groups:
        s = g.get("slot") or {}
        if s.get("codebase_type") in ctypes:
            k = (s.get("action"), s.get("domain"), s.get("codebase_type"))
            combos[k] = combos.get(k, 0) + int(g.get("count") or 0)
    slot_lines = [f"{a} | {d} | {c}: {n}" for (a, d, c), n in sorted(combos.items(), key=lambda kv: -kv[1])[:24]]
    task_lines = [f"{u['label']} ({u['stage_word']})" for u in mine]
    return {"opportunities": opp_lines, "slots": slot_lines, "tasks": task_lines}


# --- running Claude --------------------------------------------------------------------------------------------

def _bin() -> str:
    return str(CLAUDE_BIN or _gen.CLAUDE_BIN)


def argv(prompt: str, session: list[str]) -> list[str]:
    return [_bin(), "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--model", MODEL, "--effort", EFFORT,
            "--tools", ",".join(TOOLS),
            "--allowedTools", ",".join(TOOLS),
            "--disallowedTools", ",".join(DENIED_TOOLS),
            "--permission-mode", "dontAsk",
            "--setting-sources", "project", "--strict-mcp-config",
            "--disable-slash-commands", "--no-chrome", *session]


_STEP_MAP = {"reading the code": "researching", "searching the code": "researching", "listing files": "researching",
             "reading issues and PRs": "researching", "reading history": "researching",
             "writing the dossier": "writing", "timed out": "failed"}


class _Progress:
    """runs/<id>.brief.log and {step, elapsed_s} for the queue, at most every 5 s (generate._Progress's shape,
    with brief step names). Also notes whether a web tool ran."""

    def __init__(self, did: str, cb: Progress | None, echo: Callable[[str], None] | None = None):
        self.did = did
        self.f = open(log_path(did), "a", buffering=1)
        self.step = "starting"
        self.t0 = time.time()
        self.cb, self.echo = cb, echo
        self.lock = threading.Lock()
        self._last = 0.0
        self.web = False
        self.f.write(f"\n=== {store.now_iso()} brief of {did}\n")

    def snapshot(self) -> dict:
        return {"step": self.step, "elapsed_s": round(time.time() - self.t0)}

    def raw(self, text: str) -> None:
        with self.lock:
            self.f.write(text.rstrip("\n") + "\n")

    def line(self, text: str, step: str | None = None, force: bool = False) -> None:
        if text in TOOLS:
            self.web = True
        step = _STEP_MAP.get(step, step) if step else None
        with self.lock:
            changed = step is not None and step != self.step
            if step:
                self.step = step
            self.f.write(f"{time.strftime('%H:%M:%S')} [{self.step}] {text}\n")
            due = force or changed or time.time() - self._last >= 5
            if due:
                self._last = time.time()
        if self.echo:
            self.echo(f"{round(time.time() - self.t0):>5}s  {text}")
        if due and self.cb:
            try:
                self.cb(self.snapshot())
            except Exception as e:
                self.raw(f"progress callback failed: {e}")

    def close(self) -> None:
        if self.cb:
            try:
                self.cb(self.snapshot())
            except Exception:
                pass
        self.f.close()


def extract(text: str) -> tuple[dict | None, str | None]:
    """The JSON between the last <<<BRIEF line and the BRIEF>>> line after it."""
    if not text:
        return None, f"the final message was empty; it must contain the brief between `{BEGIN}` and `{END}`"
    starts = list(re.finditer(rf"^[ \t]*{re.escape(BEGIN)}[ \t]*$", text, re.M))
    if starts:
        body_start = starts[-1].end()
    else:
        i = text.rfind(BEGIN)
        if i < 0:
            return None, f"the final message has no `{BEGIN}` line; put the brief between `{BEGIN}` and `{END}`"
        body_start = i + len(BEGIN)
    rest = text[body_start:]
    m = re.search(rf"^[ \t]*{re.escape(END)}[ \t]*$", rest, re.M)
    j = m.start() if m else rest.find(END)
    body = (rest[:j] if j >= 0 else rest).strip()
    body = re.sub(r"^```(?:json)?\s*\n", "", body)
    body = re.sub(r"\n```\s*$", "", body).strip()
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as e:
        return None, f"the brief JSON does not parse: {e.msg} at line {e.lineno} column {e.colno}"
    if not isinstance(doc, dict):
        return None, "the brief must be a JSON object"
    return doc, None


_URL_RX = re.compile(r"https?://[^\s\"'<>)\]\\}]+")


def _norm_url(u: str) -> str:
    u = u.strip().rstrip(".,;:")
    u = re.sub(r"#.*$", "", u)
    u = re.sub(r"^https?://(?:www\.)?", "", u, flags=re.I)
    host, _, rest = u.partition("/")
    return (host.lower() + "/" + rest).rstrip("/")


def web_seen(work: Path, sid: str) -> tuple[bool | None, set[str]]:
    """(did a web tool run, every URL in the session's web tool calls and results) from the session transcript.
    (None, empty) when the transcript is not there."""
    cfg = _gen.claude_config_dir() / "projects"
    cands = [cfg / _gen._project_dir_name(work) / f"{sid}.jsonl",
             cfg / _gen._project_dir_name(Path(work).resolve()) / f"{sid}.jsonl"]
    cands += list(cfg.glob(f"*/{sid}.jsonl")) if cfg.exists() else []
    p = next((c for c in cands if c.exists()), None)
    if p is None:
        return None, set()
    ran, urls = False, set()
    try:
        lines = p.read_text(errors="replace").splitlines()
    except OSError:
        return None, set()
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        msg = ev.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if ev.get("type") == "assistant" and isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in TOOLS:
                    ran = True
                    inp = c.get("input") or {}
                    if inp.get("url"):
                        urls.add(_norm_url(str(inp["url"])))
        elif ev.get("type") == "user":
            blob = json.dumps(content) + json.dumps(ev.get("toolUseResult") or "")
            if "tool_result" in blob or ev.get("toolUseResult"):
                urls.update(_norm_url(u) for u in _URL_RX.findall(blob.replace("\\/", "/")))
    return ran, urls


def keep_checked_rivals(doc: dict, ran: bool | None, urls: set[str], web_flag: bool) -> list[str]:
    """Drop every rival without a source URL, and every rival whose URL this run's web tools never showed (when
    the transcript was readable) or every rival when no web tool ran. Returns 'who (url)' for each dropped."""
    dropped = []
    checked = ran if ran is not None else web_flag
    for p in doc.get("places") or []:
        if not isinstance(p, dict) or not isinstance(p.get("rivals"), list):
            continue
        keep = []
        for r in p["rivals"]:
            src = str((r or {}).get("source") or "") if isinstance(r, dict) else ""
            ok = src.startswith(("http://", "https://")) and checked
            if ok and ran is not None:
                n = _norm_url(src)
                ok = any(n == u or u.startswith(n + "/") or n.startswith(u + "/") and u.count("/") >= 2
                         for u in urls)
            if ok:
                keep.append(r)
            else:
                dropped.append(f"{(r or {}).get('who') if isinstance(r, dict) else r} ({src or 'no source'})")
        p["rivals"] = keep
    return dropped


_VERDICT_WORD = re.compile(r"^(?:worth diving into|worth a dive|dive in|dive|maybe|probably skip|skip)\s*[:.,]\s*", re.I)


def strip_verdict_word(lede):
    """The lede without a leading verdict word ("Worth diving into: ..."): the page shows the verdict as a pill
    right beside it. Deterministic, so it never costs a repair pass."""
    if not isinstance(lede, str):
        return lede
    m = _VERDICT_WORD.match(lede.strip())
    if not m:
        return lede
    rest = lede.strip()[m.end():]
    return rest[:1].upper() + rest[1:] if rest else lede


def finalize(doc: dict, did: str, sha: str, repo: str, *, model: str | None, elapsed_s: int, cost_usd: float,
             rivals_checked: bool) -> dict:
    """Stamp the server's fields (spec 6.3), in schema order."""
    if isinstance(doc.get("verdict"), dict) and "lede" in doc["verdict"]:
        doc["verdict"]["lede"] = strip_verdict_word(doc["verdict"]["lede"])
    head = {"schema": SCHEMA_ID, "dossier_id": did, "dossier_sha": sha, "repo": repo,
            "generated_at": store.now_iso(), "model": model or "claude-opus-5-5", "elapsed_s": int(elapsed_s),
            "cost_usd": round(float(cost_usd), 4), "rivals_checked": bool(rivals_checked)}
    order = [k for k in brief_prompt.SCHEMA["properties"] if k not in head]
    rest = {k: doc[k] for k in order if k in doc}
    rest.update({k: v for k, v in doc.items() if k not in head and k not in rest})
    return {**head, **rest}


def _empty_result() -> dict:
    return {"ok": False, "doc": None, "errors": [], "error": None, "cost_usd": 0.0, "elapsed_s": 0, "passes": 0,
            "model": None, "retryable": False, "cancelled": False, "skipped": False, "dropped_rivals": []}


def generate_brief(did: str, *, cancel: _gen.Cancel | None = None, on_progress: Progress | None = None,
                   echo: Callable[[str], None] | None = None, fetch: Fetch | None = None,
                   timeout: int = TIMEOUT_S, repair_timeout: int = REPAIR_TIMEOUT_S) -> dict:
    """One brief generation. The caller holds store.generation_lock. Never raises; returns
    {ok, doc, errors, error, cost_usd, elapsed_s, passes, model, retryable, cancelled, dropped_rivals}. Writes the
    brief only when it validates; a failure leaves the brief that was there."""
    res = _empty_result()
    t0 = time.time()
    deadline = t0 + timeout
    dossier = store.load_dossier(did)
    if dossier is None:
        res["error"] = f"no dossier {did}"
        _set(did, status="failed", error=res["error"], finished_at=store.now_iso())
        return res
    sha = dossier_sha(did)
    repo = dossier.get("repo") or ""
    cancel = cancel or _gen.Cancel()
    prog = _Progress(did, lambda snap: _set(did, progress=snap), echo)
    if on_progress:
        outer = prog.cb
        prog.cb = lambda snap: (outer(snap), on_progress(snap))
    work = store.P.clones / f"{did}.brief"
    sid = str(uuid.uuid4())
    _clear_stop(did)
    _set(did, status="generating", started_at=store.now_iso(), finished_at=None, error=None, errors=None,
         pid=os.getpid(), dossier_sha=sha, progress=prog.snapshot())
    with _current_lock:
        _current[did] = cancel
    done = threading.Event()
    threading.Thread(target=_watch, args=(did, cancel, done), name="brief-stop-watch", daemon=True).start()

    def was_cancelled() -> bool:
        if cancel.cancelled:
            res["cancelled"], res["error"] = True, cancel.reason or "stopped"
            prog.line(f"stopped: {res['error']}", step="stopped", force=True)
            return True
        return False

    def stopped(p) -> bool:
        res["cost_usd"] += p.cost
        res["model"] = res["model"] or p.model
        if was_cancelled():
            return True
        if p.timed_out:
            res["error"] = "timed out"
            return True
        if p.rate_limited:
            res["retryable"], res["error"] = True, "hit the Claude usage limit; queued again"
            return True
        if p.is_error:
            res["error"] = p.error_text()
            return True
        return False

    def check(p) -> tuple[dict | None, list[str]]:
        doc, perr = extract(p.text)
        if perr:
            return None, [perr]
        prog.line("checking", step="checking", force=True)
        ran, urls = web_seen(work, sid)
        dropped = keep_checked_rivals(doc, ran, urls, prog.web)
        for d in dropped:
            prog.raw(f"  dropped rival without a source seen in this run: {d}")
        res["dropped_rivals"] = dropped
        doc = finalize(doc, did, sha or "", repo, model=res["model"], elapsed_s=round(time.time() - t0),
                       cost_usd=res["cost_usd"], rivals_checked=bool(ran if ran is not None else prog.web))
        return doc, validate(doc, dossier)

    try:
        if was_cancelled():
            return res
        work.mkdir(parents=True, exist_ok=True)
        prog.line(f"reading {repo}'s readme, releases and merged work", step="fetching", force=True)
        try:
            inputs = (fetch or fetch_inputs)(dossier)
        except Exception as e:
            prog.raw(f"fetch failed: {e}")
            inputs = {}
        prog.raw(f"inputs: readme {len(inputs.get('readme') or '')} chars, {len(inputs.get('releases') or [])} "
                 f"releases, {len(inputs.get('prs') or [])} merged PRs")
        sl = _slots_doc()
        lines = _input_lines(did, dossier, sl.get("groups") or [], task_metas())
        item = store.find(store.load(), did) or {}
        focus = [" | ".join(str(f).split("|")[:3]) for f in item.get("focus") or [] if str(f).count("|") >= 2]
        prompt = brief_prompt.build(did=did, sha=sha or "", repo=repo, dossier=dossier,
                                    readme=inputs.get("readme") or "", releases=inputs.get("releases"),
                                    prs=inputs.get("prs"), focus=focus, **lines)
        if was_cancelled():
            return res
        prog.line("starting claude", step="researching", force=True)
        p1 = _gen.run_claude(argv(prompt, ["--session-id", sid]), work, _gen._left(deadline, timeout),
                                 prog, cancel)
        res["passes"] = 1
        if stopped(p1):
            return res
        doc, errors = check(p1)
        if errors:
            left = deadline - time.time()
            if left < MIN_REPAIR_S:
                res["errors"] = errors
                res["error"] = f"timed out: {len(errors)} validation errors and no time left for the repair pass"
                return res
            prog.line(f"{len(errors)} validation error{'s' if len(errors) != 1 else ''}; one repair pass",
                      step="repairing", force=True)
            for e in errors:
                prog.raw("  invalid: " + e)
            p2 = _gen.run_claude(argv(brief_prompt.repair(errors, res["dropped_rivals"]), ["--resume", sid]),
                                     work, min(repair_timeout, left), prog, cancel)
            res["passes"] = 2
            if stopped(p2):
                res["errors"] = errors
                return res
            doc, errors = check(p2)
            if errors:
                for e in errors:
                    prog.raw("  still invalid: " + e)
                res["errors"] = errors
                res["error"] = f"failed validation after the repair pass ({len(errors)} errors)"
                return res
        doc["elapsed_s"] = round(time.time() - t0)
        doc["cost_usd"] = round(res["cost_usd"], 4)
        _write(doc)
        res["ok"], res["doc"] = True, doc
        return res
    except _gen.HardTimeout as e:
        if not was_cancelled():
            res["error"] = str(e)
        return res
    except Exception as e:
        if not was_cancelled():
            res["error"] = f"{type(e).__name__}: {e}"
        return res
    finally:
        done.set()
        with _current_lock:
            _current.pop(did, None)
        res["elapsed_s"] = round(time.time() - t0)
        res["cost_usd"] = round(res["cost_usd"], 4)
        _gen.cleanup(work, sid)
        common = dict(finished_at=store.now_iso(), cost_usd=res["cost_usd"], elapsed_s=res["elapsed_s"],
                      passes=res["passes"], pid=None, force=False)
        if res["ok"]:
            _set(did, status="ready", error=None, errors=None, progress=None, **common)
        elif res["cancelled"] or res["retryable"]:
            _set(did, status="queued", error=res["error"], progress=None, started_at=None, **common)
        else:
            _set(did, status="failed", error=res["error"], errors=(res["errors"] or [])[:60] or None,
                 progress=None, **common)
        _clear_stop(did)
        outcome = ("ready" if res["ok"] else f"stopped: {res['error']}" if res["cancelled"]
                   else f"queued again: {res['error']}" if res["retryable"] else f"failed: {res['error']}")
        prog.line(outcome + f" in {res['elapsed_s']}s, ${res['cost_usd']:.2f}",
                  step="done" if res["ok"] else "stopped" if res["cancelled"] else "failed", force=True)
        prog.close()


def generate(did: str, *, cancel: _gen.Cancel | None = None, on_progress: Progress | None = None,
             echo: Callable[[str], None] | None = None, fetch: Fetch | None = None, locked: bool = False,
             wait: bool = True, not_before: float | None = None) -> dict:
    """Generate one brief now, holding store.generation_lock (it waits for a running dossier or brief unless
    `wait` is False; `locked` means the caller already holds it). `not_before` (epoch seconds) skips the run
    when the brief file was written after that time while this call waited. Returns generate_brief's dict."""
    if locked:
        return generate_brief(did, cancel=cancel, on_progress=on_progress, echo=echo, fetch=fetch)
    cancel = cancel or _gen.Cancel()
    while True:
        with store.generation_lock(wait_s=2.0) as got:
            if got:
                if not_before is not None:
                    try:
                        if path(did).stat().st_mtime > not_before:
                            r = _empty_result()
                            r.update(ok=True, skipped=True, doc=load(did))
                            return r
                    except OSError:
                        pass
                return generate_brief(did, cancel=cancel, on_progress=on_progress, echo=echo, fetch=fetch)
        if not wait or cancel.cancelled:
            r = _empty_result()
            r.update(error=cancel.reason or "another generation is running", cancelled=cancel.cancelled,
                     skipped=not cancel.cancelled)
            return r

