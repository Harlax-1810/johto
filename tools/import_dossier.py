"""Register a hand-researched dossier (from research/hard/*.md) so it shows on the Repos screen.

Usage:
  .venv/bin/python tools/import_dossier.py DOSSIER.json --root CLONE_AT_PINNED_COMMIT [--merge] [--dry-run]

- Validates with johto.dossier.schema.validate (pointers checked against the clone, text policy on).
- --merge: if a generated dossier already exists for the same id, keep its blocks and add this file's
  opportunities that aren't already there (matched by area or title), up to the 7-opportunity cap.
- Writes data/dossiers/<id>.json and marks the index item ready (source: research). The worker never
  regenerates a repo that has a ready item.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from johto.dossier import candidates, schema, store  # noqa: E402


def merge(existing: dict, new: dict) -> dict:
    ops = schema.opportunities(existing)
    seen = {(o.get("area") or "").lower() for o in ops} | {(o.get("title") or "").lower() for o in ops}
    ids = {o.get("id") for o in ops}
    add = [o for o in schema.opportunities(new)
           if (o.get("area") or "").lower() not in seen and (o.get("title") or "").lower() not in seen]
    room = schema.MAX_OPPS - len(ops)
    n = 0
    for o in add[:max(0, room)]:
        o = dict(o)
        while o["id"] in ids:
            o["id"] = f"r{n + 1}"
            n += 1
        ids.add(o["id"])
        existing["blocks"].append(o)
    # Carry the research's warnings, facts, questions and code excerpts too. Callouts go right after the
    # overview so they're seen first; the rest go at the end. Stays within the block cap.
    have = {json.dumps(b, sort_keys=True) for b in existing["blocks"]}
    extra = [b for b in new.get("blocks", []) if b.get("type") in ("callout", "facts", "questions", "code")
             and json.dumps(b, sort_keys=True) not in have]
    room = schema.MAX_BLOCKS - len(existing["blocks"])
    extra = extra[:max(0, room)]
    callouts = [b for b in extra if b.get("type") == "callout"]
    rest = [b for b in extra if b.get("type") != "callout"]
    at = next((i + 1 for i, b in enumerate(existing["blocks"]) if b.get("type") == "overview"), 0)
    existing["blocks"][at:at] = callouts
    existing["blocks"].extend(rest)
    existing["sources"] = sorted(set(existing.get("sources", ["generated"])) | {"research"})
    return existing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dossier")
    ap.add_argument("--root", required=True, help="clone of the repo at the pinned commit")
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    doc = json.loads(Path(a.dossier).read_text())
    doc.setdefault("sources", ["research"])
    errs = schema.validate(doc, root=Path(a.root), policy=True)
    if errs:
        print(f"{len(errs)} validation errors:")
        for e in errs:
            print("  -", e)
        sys.exit(1)
    did = doc["id"]
    if did != store.make_id(doc["repo"], doc["commit"]):
        sys.exit(f"id must be {store.make_id(doc['repo'], doc['commit'])}")
    existing = store.load_dossier(did)
    if existing and not a.merge:
        sys.exit(f"{did} already has a dossier; pass --merge to add the research opportunities to it")
    if existing:
        doc = merge(existing, doc)
        errs = schema.validate(doc, root=Path(a.root), policy=True)
        if errs:
            sys.exit("merged document fails validation:\n  - " + "\n  - ".join(errs))
    if a.dry_run:
        print(f"OK (dry run): {did}, {len(schema.opportunities(doc))} opportunities")
        return
    store.save_dossier(doc)
    entry = candidates.find(doc["repo"]) or {"full_name": doc["repo"], "commit": doc["commit"],
                                              "pinned_url": doc["pinned_url"], "fits": []}
    entry = {**entry, "commit": doc["commit"], "pinned_url": doc["pinned_url"]}
    with store.edit() as idx:
        it = store.find(idx, did)
        if it is None:
            it = store.new_item(entry, rank=0, manual=True)
            idx["items"].insert(0, it)
        it.update(status="ready", finished_at=store.now_iso(), error=None, errors=None, source="research",
                  promise=(doc.get("verdict") or {}).get("promise"), headline=doc.get("headline"))
        if it.get("rank") is None:
            it["rank"] = 0
    print(f"Imported {did}: {len(schema.opportunities(doc))} opportunities, status ready")


if __name__ == "__main__":
    main()
