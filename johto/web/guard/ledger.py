"""The AI text the app showed for a task (build-plan 9.4 ledger), derived from files and cached by mtime.

Sources: the AI sections of brief.md (lead and dossier research), every lead on the same repo (title, facts,
mechanism, verify, one-shot reason, repo notes), the task's dossier (and any dossier for its repo), AutoQC
justifications, Claude's messages in every run's transcript (collected or live), the outside review in each
review pack and follow-up pack, saved outside reviews (reviews/*.md, such as GPT Pro's), the grading table's
AI text (grading.json, not the columns Austin marks), the writing kit (kit.md), checks (checks/*.md) and the
facts helper chats showed him (notes/ai-*.md), the repo brief of each of those dossiers with every earlier
version of it (data/dossiers/<id>.brief.json and runs/<id>.brief.*.json: research he reads right before the
prompt, so kind "lead"), the task understanding beside the prompt with its earlier versions (understand.json and
understand-history/*.json, kind "lead"), and the findings in plain words (plain.json, kind "grading"). Items of kind "_human"
are Austin's own turns in the lane: never a source, only used to drop echoes of his words from Claude's text.
Nothing is written.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from ... import config, workspace

_CACHE: dict[str, tuple[tuple, list[dict]]] = {}
_LOCK = threading.Lock()
_AI_BRIEF = re.compile(r"^## (Lead|Dossier\b.*)$", re.M)
_REVIEW = re.compile(r'<file path="[^"]*outside-review[^"]*\.md">\n?(.*?)</file>', re.S)
_FRONT = re.compile(r"\A---\n.*?\n---\n", re.S)
_REVIEW_NAME = re.compile(r"^(?P<run>.+?)-t(?P<turn>\d+)-(?P<source>[\w.-]+)\.md$")
SOURCE_NAMES = {"gptpro": "GPT Pro", "gpt": "ChatGPT", "chatgpt": "ChatGPT", "claude": "Claude"}
# The grading.json row keys that hold AI text (johto/grading.py): the finding, its verification limits and the
# verify run's summary. Austin's own columns (scope, marks) and the bookkeeping keys are never read.
_GRADING_AI = ("finding", "limits")


def _mtime(p: Path | None) -> float:
    try:
        return p.stat().st_mtime if p else 0.0
    except OSError:
        return 0.0


def _strings(v, out: list[str]) -> None:
    """Every string of 6+ words in a JSON value (dossiers hold prose in many keys)."""
    if isinstance(v, str):
        if len(v.split()) >= 6:
            out.append(v)
    elif isinstance(v, dict):
        for x in v.values():
            _strings(x, out)
    elif isinstance(v, list):
        for x in v:
            _strings(x, out)


def _human_texts(path: Path) -> list[str]:
    """Austin's own turns in a transcript (Claude's replies echo them; those echoes are his words)."""
    out = []
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                if '"user"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") != "user":
                    continue
                c = (e.get("message") or {}).get("content")
                if isinstance(c, str):
                    out.append(c)
                elif isinstance(c, list):
                    out += [b.get("text") for b in c if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
    except OSError:
        pass
    return out


def _claude_texts(path: Path) -> list[str]:
    out = []
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                if '"assistant"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") != "assistant":
                    continue
                for b in (e.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                        out.append(b["text"])
    except OSError:
        pass
    return out


def _transcripts(d: Path, meta: dict) -> list[Path]:
    paths = []
    for rec in meta.get("runs") or []:
        if rec.get("transcript") and Path(rec["transcript"]).exists():
            paths.append(Path(rec["transcript"]))
            continue
        try:
            from ..lane import monitor
            proj = monitor.lane_paths(d / rec["dir"])["projects"]
            paths += monitor.transcripts(proj)
        except Exception:
            pass
    return paths


def _dossier_docs(meta: dict) -> list[tuple[str, Path | None, dict]]:
    try:
        from ...dossier import store
    except Exception:
        return []
    ids = []
    did = (meta.get("dossier") or {}).get("id")
    if did:
        ids.append(did)
    try:
        it = store.find_repo(store.load(), (meta.get("repo") or {}).get("full_name") or "")
        if it and it["id"] not in ids:
            ids.append(it["id"])
    except Exception:
        pass
    out = []
    for i in ids:
        try:
            p = store.dossier_path(i)
            doc = store.load_dossier(i)
        except Exception:
            p, doc = None, None
        if doc is None:
            fx = Path(__file__).resolve().parent.parent / "fixtures" / "dossiers" / f"{i}.json"
            if fx.exists():
                p, doc = fx, json.loads(fx.read_text())
        if doc:
            out.append((i, p, doc))
    return out


# Brief keys that hold no prose the model wrote for Austin: the server's stamps, ids, enums, dates, URLs and
# provenance. Every other string in a brief is a source.
_BRIEF_SKIP = {"schema", "dossier_id", "dossier_sha", "repo", "generated_at", "model", "elapsed_s", "cost_usd",
               "rivals_checked", "opportunity_id", "id", "near_seed", "from", "actions", "when", "source", "level"}


def _brief_strings(v, out: list[str], key: str | None = None) -> None:
    """Every model-written string in a repo brief, whatever its length."""
    if key in _BRIEF_SKIP:
        return
    if isinstance(v, str):
        if v.strip():
            out.append(v)
    elif isinstance(v, dict):
        for k, x in v.items():
            _brief_strings(x, out, k)
    elif isinstance(v, list):
        for x in v:
            _brief_strings(x, out, key)


def _brief_files(docs: list[tuple[str, Path | None, dict]]) -> list[tuple[str, Path]]:
    """(dossier id, brief file) for each dossier's current brief and its earlier versions."""
    try:
        from ...dossier import brief
    except Exception:
        return []
    out = []
    for i, _, _ in docs:
        try:
            paths = [brief.path(i), *brief.history(i)]
        except Exception:
            continue
        out += [(i, p) for p in paths if p.exists()]
    return out


def _brief_items(files: list[tuple[str, Path]]) -> list[dict]:
    out = []
    for i, p in files:
        try:
            doc = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        texts: list[str] = []
        _brief_strings(doc, texts)
        for t in dict.fromkeys(texts):
            out.append({"kind": "lead", "ref": f"{i}.brief", "label": "Repo brief", "text": t})
    return out


def _plain_items(p: Path) -> list[dict]:
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return []
    rows = data.get("rows") if isinstance(data, dict) else None
    out = []
    for rid, rec in (rows or {}).items() if isinstance(rows, dict) else []:
        text = rec.get("plain") if isinstance(rec, dict) else None
        if isinstance(text, str) and text.strip():
            out.append({"kind": "grading", "ref": f"plain.json#{rid}", "label": "Finding in plain words", "text": text})
    return out


# Keys of a task understanding that hold ids, enums and paths, not prose the model wrote for Austin.
_UNDERSTAND_SKIP = {"url", "path", "verdict", "status", "confirmed"}


def _understand_files(d: Path) -> list[Path]:
    return [p for p in (d / "understand.json",) if p.exists()] + sorted((d / "understand-history").glob("*.json"))


def _understand_items(files: list[Path]) -> list[dict]:
    out, seen = [], set()
    for p in files:
        try:
            doc = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        texts: list[str] = []

        def walk(v, key=None):
            if key in _UNDERSTAND_SKIP:
                return
            if isinstance(v, str):
                if v.strip():
                    texts.append(v)
            elif isinstance(v, dict):
                for k, x in v.items():
                    walk(x, k)
            elif isinstance(v, list):
                for x in v:
                    walk(x, key)
        walk((doc or {}).get("doc") if isinstance(doc, dict) else None)
        for t in texts:
            if t not in seen:
                seen.add(t)
                out.append({"kind": "lead", "ref": p.name, "label": "Task understanding", "text": t})
    return out


def _sources(d: Path, meta: dict) -> tuple[list[Path], list[Path], Path]:
    packs = sorted([*(d / "runs").glob("*/review-pack-*.xml"), *(d / "runs").glob("*/review-followup-*.xml")])
    return _transcripts(d, meta), packs, config.DATA / "scout" / "leads.json"


def _ai_files(d: Path) -> dict[str, list[Path]]:
    """The files the newer features write AI text into, by item kind."""
    return {"review": sorted((d / "reviews").glob("*.md")),
            "grading": [p for p in (d / "grading.json",) if p.exists()],
            "kit": [p for p in (d / "kit.md",) if p.exists()],
            "check": sorted((d / "checks").glob("*.md")),
            "notes": sorted((d / "notes").glob("ai-*.md"))}


def _read(p: Path) -> str:
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


def _review_label(p: Path, text: str) -> str:
    """'Outside review: GPT Pro, validate-1 turn 2' from its front matter (else the file name)."""
    m = _REVIEW_NAME.match(p.name)
    fm = _FRONT.match(text)
    front = dict(re.findall(r"^(\w+):[ \t]*(\S.*?)\s*$", fm.group(0), re.M)) if fm else {}
    src = front.get("source") or (m.group("source") if m else "") or "outside"
    run = front.get("run") or (m.group("run") if m else "")
    turn = front.get("turn") or (str(int(m.group("turn"))) if m else "")
    where = f"{run} turn {turn}" if run and turn else run
    return f"Outside review: {SOURCE_NAMES.get(src.lower(), src)}" + (f", {where}" if where else "")


def _grading_texts(row: dict) -> list[str]:
    """The AI text in one grading row: finding, limits and verify.summary (strings of 6+ words)."""
    texts: list[str] = []
    for k in _GRADING_AI:
        _strings(row.get(k), texts)
    v = row.get("verify")
    if isinstance(v, dict):
        _strings(v.get("summary"), texts)
    return texts


def _grading_items(p: Path) -> list[dict]:
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return []
    rows = data.get("rows") if isinstance(data, dict) else data
    out = []
    for i, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        name = row.get("finding") or row.get("id") or f"row {i + 1}"
        for t in _grading_texts(row):
            out.append({"kind": "grading", "ref": f"{p}#{row.get('id') or i + 1}",
                        "label": f"Grading table: {str(name)[:60]}", "text": t})
    return out


def _file_items(files: dict[str, list[Path]]) -> list[dict]:
    out = []
    for p in files["review"]:
        text = _read(p)
        body = _FRONT.sub("", text, count=1)
        if body.strip():
            out.append({"kind": "review", "ref": str(p), "label": _review_label(p, text), "text": body})
    for p in files["grading"]:
        out += _grading_items(p)
    for kind, label in (("kit", "Writing kit (kit.md)"), ("check", "Check"), ("notes", "Helper chat notes")):
        for p in files[kind]:
            text = _read(p)
            if text.strip():
                lab = label if kind == "kit" else f"{label} ({p.name})"
                out.append({"kind": kind, "ref": str(p), "label": lab, "text": _FRONT.sub("", text, count=1)})
    return out


def items(task_dir: Path) -> list[dict]:
    """[{kind, ref, label, text}] for one task, rebuilt when a source file changes."""
    d = Path(task_dir)
    meta = workspace.load(d)
    trans, packs, leads_p = _sources(d, meta)
    docs = _dossier_docs(meta)
    files = _ai_files(d)
    briefs = _brief_files(docs)
    plain_p = d / "plain.json"
    und = _understand_files(d)
    sig = (tuple((str(p), _mtime(p)) for p in und), _mtime(d / "brief.md"), _mtime(d / "task.json"), _mtime(leads_p),
           tuple((str(p), _mtime(p)) for p in trans), tuple((str(p), _mtime(p)) for p in packs),
           tuple((i, _mtime(p)) for i, p, _ in docs),
           tuple((str(p), _mtime(p)) for ps in files.values() for p in ps),
           tuple((str(p), _mtime(p)) for _, p in briefs), _mtime(plain_p))
    key = str(d)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] == sig:
            return hit[1]
    out: list[dict] = []
    # brief.md: only the AI research sections (the slot and rules text is the app's own)
    try:
        brief = (d / "brief.md").read_text()
        m = _AI_BRIEF.search(brief)
        if m:
            out.append({"kind": "lead", "ref": "brief.md", "label": "Research notes (brief.md)", "text": brief[m.start():]})
    except OSError:
        pass
    # leads on the same repo
    repo = ((meta.get("repo") or {}).get("full_name") or "").lower()
    try:
        leads = json.loads(leads_p.read_text()) if leads_p.exists() else []
    except (OSError, ValueError):
        leads = []
    for lead in leads:
        if (lead.get("repo") or "").lower() != repo:
            continue
        parts = [lead.get("title") or "", *(lead.get("facts") or []), lead.get("mechanism") or "",
                 lead.get("verify") or "", (lead.get("one_shot_risk") or {}).get("why") or "",
                 *(lead.get("repo_notes") or [] if isinstance(lead.get("repo_notes"), list) else [lead.get("repo_notes") or ""])]
        for t in parts:
            if t:
                out.append({"kind": "lead", "ref": lead.get("id") or "", "label": f"Lead: {lead.get('title') or ''}"[:80], "text": t})
    # dossiers
    for i, _, doc in docs:
        texts: list[str] = []
        _strings(doc, texts)
        for t in texts:
            out.append({"kind": "lead", "ref": i, "label": "Repo research (dossier)", "text": t})
    # repo briefs (current and earlier versions) of those dossiers
    out += _brief_items(briefs)
    # AutoQC justifications
    for q in meta.get("qc") or []:
        for dim in q.get("dims") or []:
            if dim.get("justification"):
                out.append({"kind": "autoqc", "ref": q.get("audit_id") or "", "label": f"AutoQC: {dim.get('dimension')}",
                            "text": dim["justification"]})
    # Claude's messages in the lanes
    for p in trans:
        run = next((Path(r["dir"]).name for r in meta.get("runs") or [] if str(p).startswith(str(d / r["dir"]))), None)
        for t in _claude_texts(p):
            out.append({"kind": "transcript_claude", "ref": str(p), "label": f"Claude in {run or 'the lane'}", "text": t})
        for t in _human_texts(p):
            out.append({"kind": "_human", "ref": str(p), "label": "", "text": t})
    # the outside review in each review pack and follow-up pack
    for p in packs:
        for m in _REVIEW.finditer(_read(p)):
            out.append({"kind": "audit", "ref": str(p), "label": f"Outside review ({p.parent.name})", "text": m.group(1)})
    # what the newer features saved: outside reviews, grading rows, the kit, checks, helper-chat facts
    out += _file_items(files)
    # the findings in plain words (spec 7.5)
    out += _plain_items(plain_p)
    # the task understanding beside the prompt (johto/understand.py)
    out += _understand_items(und)
    with _LOCK:
        _CACHE[key] = (sig, out)
    return out
