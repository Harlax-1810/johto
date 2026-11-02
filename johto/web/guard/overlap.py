"""Red overlap: any 6-word run (after masking code-like words) shared with the task's ledger. One Overlap per
maximal matching span (build-plan section 4 shape). "My own words" accepts are kept in app/accepted.json."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

from . import ledger, normalize

QUOTE_MAX_WORDS = 12
_INDEX: dict[str, tuple[int, dict]] = {}
_LOCK = threading.Lock()
_ACC_LOCK = threading.Lock()


# AI text made after (and from) his fields: AutoQC and the outside reviews quote them, Claude echoes his prompt,
# and the grading table, writing kit and checks are built from the run. Helper-chat notes (kind "notes") count
# as research shown before he writes, like brief.md, so they count against every field.
DOWNSTREAM = {"autoqc", "audit", "transcript_claude", "review", "grading", "kit", "check"}
_QUOTED = re.compile(r'"[^"\n]{0,600}"|`[^`\n]{0,600}`')


def _prompt_text(task_dir: Path) -> str:
    try:
        from ... import workspace
        return workspace.parse_submission(Path(task_dir))["sections"].get("Opening Prompt", "")
    except Exception:
        return ""


def _index(task_dir: Path) -> dict:
    """{"up": {6-gram: (item, start, end)}, "down": {...}} over the ledger, rebuilt when it changes.

    Upstream (research shown before he wrote) counts for every field. Downstream text loses its quoted parts
    and every 6-gram that is also in his prompt or his own lane turns (those are his words coming back), and
    never counts against the opening prompt, which was written before any of it existed."""
    items = ledger.items(task_dir)
    prompt = _prompt_text(task_dir)
    key = str(task_dir)
    sig = (id(items), hashlib.sha1(prompt.encode()).hexdigest())
    with _LOCK:
        hit = _INDEX.get(key)
        if hit and hit[0] == sig:
            return {"items": items, **hit[1]}
    mine: set[str] = set()
    for t in [prompt] + [it["text"] for it in items if it["kind"] == "_human"]:
        for seq in normalize.sequences(t):
            mine.update(normalize.grams(seq))
    up: dict[str, tuple[int, int, int]] = {}
    down: dict[str, tuple[int, int, int]] = {}
    for n, it in enumerate(items):
        if it["kind"] == "_human":
            continue
        text = it["text"]
        is_down = it["kind"] in DOWNSTREAM
        if is_down:   # blank the quoted parts, keeping offsets
            text = _QUOTED.sub(lambda m: " " * len(m.group(0)), normalize.clean(text))
        target = down if is_down else up
        for seq in normalize.sequences(text):
            for i, g in enumerate(normalize.grams(seq)):
                if is_down and g in mine:
                    continue
                target.setdefault(g, (n, seq[i][1], seq[i + normalize.N - 1][2]))
    with _LOCK:
        _INDEX[key] = (sig, {"up": up, "down": down})
    return {"items": items, "up": up, "down": down}


def span_sha(words: list[str]) -> str:
    return hashlib.sha1(" ".join(words).encode()).hexdigest()[:16]


def _accepted_file(task_dir: Path) -> Path:
    return Path(task_dir) / "app" / "accepted.json"


def accepted(task_dir: Path) -> dict:
    p = _accepted_file(task_dir)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in re.finditer(r'"[^"\n]{1,400}"', text)
            if len(m.group(0).split()) <= QUOTE_MAX_WORDS]


def check(task_dir: Path, key: str, text: str) -> list[dict]:
    """[Overlap] for one field's text. Offsets are into the NFKC text (what the editor holds)."""
    if not (text or "").strip():
        return []
    idx = _index(Path(task_dir))
    items = idx["items"]
    grams = idx["up"] if key == "opening_prompt" else {**idx["down"], **idx["up"]}
    if not grams:
        return []
    acc = set(accepted(task_dir).get(key) or [])
    clean = normalize.clean(text)
    quotes = _quoted_spans(clean)
    out = []
    n = normalize.N
    for seq in normalize.sequences(text):
        hits = [i for i, g in enumerate(normalize.grams(seq)) if g in grams]
        if not hits:
            continue
        # maximal spans: runs of hit windows that overlap or touch
        spans, a, b = [], hits[0], hits[0]
        for i in hits[1:]:
            if i < b + n:
                b = i
            else:
                spans.append((a, b))
                a = b = i
        spans.append((a, b))
        for a, b in spans:
            words = [w for w, _, _ in seq[a:b + n]]
            start, end = seq[a][1], seq[b + n - 1][2]
            src_i, ms, me = grams[normalize.grams(seq)[a]]
            src = items[src_i]
            if src["kind"] == "transcript_claude" and any(qs <= start and end <= qe for qs, qe in quotes):
                continue   # a short quote of what Claude said, in quotation marks
            sha = span_sha(words)
            excerpt = src["text"][max(0, ms - 60):me + 60]
            out.append({"id": sha, "level": "red", "start": start, "end": end, "text": clean[start:end],
                        "accepted": sha in acc,
                        "source": {"kind": src["kind"], "ref": src["ref"], "label": src["label"], "excerpt": excerpt,
                                   "match_start": ms - max(0, ms - 60), "match_end": me - max(0, ms - 60)}})
    return out


def accept(task_dir: Path, key: str, overlap_id: str) -> None:
    """Austin marks a span as his own words (logged in authorship.jsonl)."""
    p = _accepted_file(task_dir)
    with _ACC_LOCK:
        acc = accepted(task_dir)
        ids = set(acc.get(key) or [])
        ids.add(overlap_id)
        acc[key] = sorted(ids)
        p.parent.mkdir(exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(acc, indent=1))
        tmp.replace(p)
        import time
        with open(p.parent / "authorship.jsonl", "a") as fh:
            fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "field": key, "event": "accept_overlap",
                                 "detail": overlap_id}) + "\n")


def unaccepted_red(task_dir: Path, texts: dict[str, str]) -> dict[str, list[dict]]:
    """{key: [Overlap]} of unaccepted red spans across the given fields."""
    out = {}
    for k, t in texts.items():
        red = [o for o in check(task_dir, k, t) if o["level"] == "red" and not o["accepted"]]
        if red:
            out[k] = red
    return out
