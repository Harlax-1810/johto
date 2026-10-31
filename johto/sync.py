"""Bring edits made in Studio back into submission.md.

Per field, three-way: the base is the value both sides last agreed on (meta["synced"], else the hash
`push` recorded). Studio changed and the app didn't: take Studio's text. The app changed and Studio
didn't: keep it (the next save sends it). Both changed: touch nothing and report a conflict. A field with
no base takes Studio's text only if it changed after the claim (meta["studio_at_claim"]); text a previous
writer left there is reported as `foreign` and never written into submission.md.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from . import workspace

SECTION_FIELDS = {field: heading for heading, field in workspace.SECTIONS.items()}
RATING_FIELDS = ("field_one_shot", "field_task_success", "field_steering")
FIELD_NAMES = {**SECTION_FIELDS, "field_one_shot": "One-shot", "field_task_success": "Task Success",
               "field_steering": "Steering"}
_LOCK = threading.Lock()  # one pull at a time (the app's poller and a focus refresh can overlap)


def norm(v) -> str:
    if v is None:
        return ""
    s = v if isinstance(v, str) else str(v)
    return "\n".join(line.rstrip() for line in s.replace("\r\n", "\n").strip().split("\n"))


def h(v) -> str:
    """Same hash as actions.sha12, so the hashes `push` recorded serve as a base."""
    return hashlib.sha1(norm(v).encode()).hexdigest()[:12]


def claim_baseline(custom_fields: dict | None) -> dict:
    """Hashes of the text Studio held when Austin claimed the task (a previous writer's, or nothing)."""
    cf = custom_fields or {}
    return {f: h(cf.get(f)) for f in (*SECTION_FIELDS, *RATING_FIELDS) if norm(cf.get(f))}


def plan(local: dict, remote: dict, meta: dict) -> dict:
    """{pull: {field: studio text}, conflicts: {field: {local, studio, why}}, agreed: {field: hash},
    foreign: {field: studio text}}.

    A field the app never saved or agreed on is pulled only when Studio's text changed after the claim
    (meta["studio_at_claim"]): text that was already there belongs to a previous writer and is never taken
    as Austin's. Tasks claimed before that record existed have no baseline, so nothing unagreed is pulled."""
    base = {**((meta.get("pushed") or {}).get("fields") or {}), **(meta.get("synced") or {})}
    at_claim = meta.get("studio_at_claim")
    pull, conflicts, agreed, foreign = {}, {}, {}, {}
    for f in (*SECTION_FIELDS, *RATING_FIELDS):
        loc, rem = norm(local.get(f)), norm(remote.get(f))
        if loc == rem:
            if rem:
                agreed[f] = h(rem)
            continue
        if not rem:
            continue  # nothing in Studio: keep the app's text, saving sends it
        b = base.get(f)
        if b is None and (at_claim is None or h(rem) == at_claim.get(f)):
            foreign[f] = rem  # there before the claim: never pulled; Save overwrites it when the app has text
            continue
        if f == "field_opening_prompt" and meta.get("runs") and loc:
            conflicts[f] = {"local": loc, "studio": rem, "why": "a run already used the app's prompt"}
        elif not loc or (b is not None and h(loc) == b):
            pull[f] = rem
        elif b is not None and h(rem) == b:
            continue  # only the app changed
        else:
            conflicts[f] = {"local": loc, "studio": rem, "why": "changed in both places"}
    return {"pull": pull, "conflicts": conflicts, "agreed": agreed, "foreign": foreign}


def _lead_digit(v: str) -> int | None:
    m = re.match(r"\s*(\d)", v or "")
    return int(m.group(1)) if m else None


def _replace(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.sync.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_section_file(d: Path, heading: str, text: str) -> None:
    """Replace one section's text in submission.md, keeping its HTML comments (as the app's editor does)."""
    path = d / "submission.md"
    src = path.read_text()
    pat = re.compile(rf"(^## {re.escape(heading)}[ \t]*\n)(.*?)(?=^## |\Z)", re.S | re.M)
    m = pat.search(src)
    if not m:
        src = src.rstrip() + f"\n\n## {heading}\n{text.strip()}\n\n"
    else:
        comments = "".join(c + "\n" for c in re.findall(r"<!--.*?-->", m.group(2), re.S))
        body = (text.strip() + "\n" if text.strip() else "") + comments + "\n"
        src = src[:m.start()] + m.group(1) + body + src[m.end():]
    _replace(path, src)


def write_ratings_file(d: Path, r: dict) -> None:
    path = d / "submission.md"
    src = path.read_text()
    vals = {"One-shot": r.get("one_shot") or "", "Task Success": str(r.get("task_success") or ""),
            "Steering": str(r.get("steering") or "")}
    for k, v in vals.items():
        src = re.sub(rf"^{re.escape(k)}:.*$", f"{k}: {v}".rstrip(), src, count=1, flags=re.M)
    _replace(path, src)


def pull(d: Path, remote: dict, write_section=None, write_ratings=None, update_meta=None) -> dict:
    """Apply plan() to the task folder. `remote` is Studio's custom_fields.

    write_section(heading, text) and write_ratings(r) default to plain file writes; the app passes its
    own (they share its per-task write lock). Returns plan() plus `pulled`: the field names taken.
    """
    write_section = write_section or (lambda heading, text: write_section_file(d, heading, text))
    write_ratings = write_ratings or (lambda r: write_ratings_file(d, r))
    with _LOCK:
        meta = workspace.load(d)
        sub = workspace.parse_submission(d)
        local = workspace.to_fields(meta, sub)
        p = plan(local, remote, meta)
        for f, text in p["pull"].items():
            if f in SECTION_FIELDS:
                write_section(SECTION_FIELDS[f], text)
        rated = {f: v for f, v in p["pull"].items() if f in RATING_FIELDS}
        if rated:
            cur = sub["ratings"]
            r = {"one_shot": (cur.get("one-shot") or "").strip().capitalize() or None,
                 "task_success": _lead_digit(cur.get("task success")), "steering": _lead_digit(cur.get("steering"))}
            if "field_one_shot" in rated:
                r["one_shot"] = rated["field_one_shot"]
            if "field_task_success" in rated:
                r["task_success"] = _lead_digit(rated["field_task_success"])
            if "field_steering" in rated:
                r["steering"] = _lead_digit(rated["field_steering"])
            write_ratings(r)
        now = datetime.now().astimezone().isoformat(timespec="seconds")

        def put(m):
            synced = dict(m.get("synced") or {})
            synced.update(p["agreed"])
            synced.update({f: h(v) for f, v in p["pull"].items()})
            m["synced"] = synced
            m["sync"] = {"at": now, "pulled": [FIELD_NAMES[f] for f in p["pull"]],
                         "conflicts": {FIELD_NAMES[f]: c["why"] for f, c in p["conflicts"].items()},
                         "foreign": {f: ("empty here" if not norm(local.get(f)) else "Save overwrites it")
                                     for f in p["foreign"]}}
        if update_meta:
            update_meta(d, put)
        else:
            meta = workspace.load(d)
            put(meta)
            workspace.save(d, meta)
        if p["pull"]:
            (d / "app").mkdir(exist_ok=True)
            with open(d / "app" / "authorship.jsonl", "a") as fh:
                for f, text in p["pull"].items():
                    fh.write(json.dumps({"at": now, "field": FIELD_NAMES[f], "event": "pulled_from_studio",
                                         "chars": len(text)}) + "\n")
    return {**p, "pulled": [FIELD_NAMES[f] for f in p["pull"]]}


def mark_pushed(d: Path, sent: dict, update_meta) -> None:
    """After a save to Studio, the sent values are what both sides agree on (and replaced any text a previous
    writer left in those fields)."""
    def put(m):
        synced = dict(m.get("synced") or {})
        synced.update({f: h(v) for f, v in sent.items() if f in FIELD_NAMES})
        m["synced"] = synced
        fo = (m.get("sync") or {}).get("foreign")
        if fo:
            m["sync"]["foreign"] = {f: why for f, why in fo.items() if f not in sent}
    update_meta(d, put)
