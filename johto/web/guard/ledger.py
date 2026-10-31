"""The AI text the app showed for a task (build-plan 9.4 ledger), derived from files and cached by mtime.

Sources: the AI sections of brief.md (lead and dossier research), every lead on the same repo (title, facts,
mechanism, verify, one-shot reason, repo notes), the task's dossier (and any dossier for its repo), AutoQC
justifications, Claude's messages in every run's transcript (collected or live), and the outside review in
each review pack. Items of kind "_human" are Austin's own turns in the lane: never a source, only used to
drop echoes of his words from Claude's text. Nothing is written.
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
_REVIEW = re.compile(r'<file path="1-review/04-outside-review\.md">\n?(.*?)</file>', re.S)


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


def _sources(d: Path, meta: dict) -> tuple[list[Path], list[Path], Path]:
    packs = sorted((d / "runs").glob("*/review-pack-*.xml"))
    return _transcripts(d, meta), packs, config.DATA / "scout" / "leads.json"


def items(task_dir: Path) -> list[dict]:
    """[{kind, ref, label, text}] for one task, rebuilt when a source file changes."""
    d = Path(task_dir)
    meta = workspace.load(d)
    trans, packs, leads_p = _sources(d, meta)
    docs = _dossier_docs(meta)
    sig = (_mtime(d / "brief.md"), _mtime(d / "task.json"), _mtime(leads_p),
           tuple((str(p), _mtime(p)) for p in trans), tuple((str(p), _mtime(p)) for p in packs),
           tuple((i, _mtime(p)) for i, p, _ in docs))
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
    # the outside review in each review pack
    for p in packs:
        try:
            m = _REVIEW.search(p.read_text(errors="replace"))
        except OSError:
            m = None
        if m:
            out.append({"kind": "audit", "ref": str(p), "label": f"Outside review ({p.parent.name})", "text": m.group(1)})
    with _LOCK:
        _CACHE[key] = (sig, out)
    return out
