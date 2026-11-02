"""`johto dossier run|once|list|pause|resume|brief|brief-show`."""
from __future__ import annotations

import sys

from . import candidates, store, worker


def _say(s: str) -> None:
    print(s, flush=True)


def cmd_run(a) -> None:
    w = worker.Worker(echo=_say)
    moved = store.requeue_stale()
    if moved:
        _say(f"requeued {', '.join(moved)}")
    w.queue_briefs()
    worker.clean_stale_clones()
    if store.load().get("paused"):
        _say("The worker is paused; it fills the queue but starts nothing until `johto dossier resume`.")
    try:
        w.loop()
    except KeyboardInterrupt:
        _say("stopped")


def cmd_once(a) -> None:
    entry = candidates.find(a.repo)
    if entry is None:
        sys.exit(f"{a.repo} is not a vetted repo (data/scout/repos.json). Run `johto scout vet {a.repo}` first.")
    if a.commit:
        entry = {**entry, "commit": a.commit,
                 "pinned_url": f"https://github.com/{entry['full_name']}/tree/{a.commit}"}
    if a.focus:
        entry = {**entry, "focus": list(a.focus)}
    did = store.make_id(entry["full_name"], entry["commit"])
    with store.generation_lock(wait_s=1.0) as got:
        if got:
            return _once(entry, did)
    _say("Another generation is running; waiting for it to finish.")
    with store.generation_lock(blocking=True):
        _once(entry, did)


def _once(entry: dict, did: str) -> None:
    """Queue and run one item. The caller holds the generation lock, so no worker can start this item (or
    any other) between queueing it here and running it."""
    worker.queue_manual(entry)
    _say(f"generating {did} ({entry['full_name']} at {entry['commit'][:8]})")
    res = worker.run_item(did, echo=_say)
    if res.ok:
        _say(f"ready: {store.dossier_path(did)}  ({res.elapsed_s}s, ${res.cost_usd:.2f}, {res.passes} pass"
             f"{'es' if res.passes != 1 else ''})")
    elif res.cancelled or res.retryable:
        _say(f"{res.error}; {did} is queued again")
        sys.exit(1)
    else:
        _say(f"failed: {res.error}")
        for e in res.errors[:30]:
            _say(f"  {e}")
        sys.exit(1)


def cmd_list(a) -> None:
    idx = store.load()
    order = {"generating": 0, "ready": 1, "queued": 2, "failed": 3, "actioned": 4, "declined": 5}
    items = sorted(idx["items"], key=lambda it: (order.get(it["status"], 9), it.get("rank") or 10**6))
    st = worker.status()
    _say(f"target {idx.get('target')}  {'PAUSED' if idx.get('paused') else 'running'}"
         + (f" ({idx['paused_reason']})" if idx.get("paused_reason") else "")
         + (f"  generating {st['current']}" if st["current"] else ""))
    for it in items:
        d = store.load_dossier(it["id"]) if it["status"] in ("ready", "actioned", "declined") else None
        cost = f"${it['cost_usd']:.2f}" if it.get("cost_usd") is not None else ""
        took = f"{round(it['elapsed_s'] / 60)}m" if it.get("elapsed_s") else ""
        note = (d or {}).get("headline") or it.get("error") or ", ".join((it.get("promise_reasons") or [])[:2])
        if it["status"] == "generating" and it.get("progress"):
            p = it["progress"]
            note = f"{p.get('step')} ({round((p.get('elapsed_s') or 0) / 60)}m)"
        _say(f"  {it['status']:<10} {str(it.get('rank') or '-'):>3}  {it['repo']:<34} {it['commit'][:8]} "
             f"{took:>4} {cost:>6}  {note or ''}")


def cmd_pause(a) -> None:
    st = worker.set_paused(True, "paused from the command line", stop=not a.let_finish)
    _say("paused" + (f"; stopping {', '.join(st['stopping'])}" if st.get("stopping") else ""))


def cmd_resume(a) -> None:
    worker.set_paused(False)
    _say("resumed; a running worker picks this up within a minute")


# --- repo briefs --------------------------------------------------------------------------------------

def _brief_targets(target: str) -> list[str]:
    from . import brief
    ids = brief.dossier_ids()
    if target == "all":
        idx = {it["id"]: it for it in store.load()["items"]}
        return sorted(ids, key=lambda d: ((idx.get(d) or {}).get("rank") or 10**6, d))
    if target in ids:
        return [target]
    t = target.lower().strip("/")
    hits = []
    for did in ids:
        d = store.load_dossier(did) or {}
        if (d.get("repo") or "").lower() == t or did.startswith(t.replace("/", "__") + "__"):
            hits.append(did)
    if not hits:
        sys.exit(f"No dossier for {target}. `johto dossier list` shows them.")
    return hits


def cmd_brief(a) -> None:
    """Write repo briefs now, one at a time, even while the worker is paused. Skips briefs that are ready unless
    --force. No spend cap: Austin is on a Claude subscription."""
    from . import brief
    targets = _brief_targets(a.target)
    spent, done, failed, t0 = 0.0, [], [], __import__("time").time()
    for did in targets:
        st = brief.state(did)
        if st == "ready" and not a.force:
            _say(f"{did}: ready, skipped")
            continue
        _say(f"\n{did}: {st}; writing the brief")
        res = brief.generate(did, echo=_say if a.verbose else None)
        spent += res["cost_usd"]
        line = (f"{did}: {'ready' if res['ok'] else 'FAILED: ' + str(res['error'])}  "
                f"({res['elapsed_s']}s, ${res['cost_usd']:.2f}, {res['passes']} pass{'es' if res['passes'] != 1 else ''}"
                + (f", {len(res['dropped_rivals'])} rivals dropped" if res.get("dropped_rivals") else "") + ")")
        _say(line)
        for e in (res.get("errors") or [])[:20]:
            _say(f"  {e}")
        (done if res["ok"] else failed).append(did)
    mins = (__import__("time").time() - t0) / 60
    _say(f"\nbriefs: {len(done)} written, {len(failed)} failed, {mins:.1f} min, ${spent:.2f}"
         + (f"; failed: {', '.join(failed)}" if failed else ""))
    if failed:
        sys.exit(1)


def cmd_brief_show(a) -> None:
    import json as _json
    from . import brief
    for did in _brief_targets(a.target):
        st = brief.state(did)
        doc = brief.load(did)
        if a.json:
            print(_json.dumps(brief.view(did), indent=1, ensure_ascii=False))
            continue
        _say(f"{did}  [{st}]")
        if not doc:
            fb = brief.fallback(store.load_dossier(did) or {})
            _say(f"  (no brief; the dossier says) {fb['one_liner']}")
            for s in fb["seeds"]:
                _say(f"  - {s['title']}")
            continue
        v = doc["verdict"]
        _say(f"  {doc['name']}: {doc['one_liner']}")
        _say(f"  {v['level']}: {v['lede']}")
        _say(f"  Why: {doc['list_line']}")
        _say(f"  How it works: {doc['how_it_works']}")
        for h in doc.get("heading") or []:
            _say(f"  {h['when']}  {h['what']}")
        _say("  Start from one of these:")
        for s in doc["seeds"]:
            _say(f"   [{s['opportunity_id']}] {s['title']}\n      {s['problem']}\n      ({s['models_miss']})")
        _say("  Or find your own:")
        for p in doc["places"]:
            _say(f"   [{p['id']}] {p['name']}: {p['today']}")
            for q in p["questions"]:
                _say(f"      ? {q}")
            for r in p["rivals"]:
                _say(f"      Rival: {r['who']} {r['has']}  <{r['source']}>")
        _say(f"  ({doc.get('model')}, {doc.get('elapsed_s')}s, ${doc.get('cost_usd') or 0:.2f}, "
             f"rivals checked: {doc.get('rivals_checked')})")


def register(sub) -> None:
    p = sub.add_parser("dossier", help="repo dossiers: research pages about promising repos")
    ds = p.add_subparsers(dest="dossier_cmd", required=True)
    s = ds.add_parser("run", help="run the worker in the foreground (respects pause)")
    s.set_defaults(func=cmd_run)
    s = ds.add_parser("once", help="generate one dossier now for owner/name, even when paused")
    s.add_argument("repo")
    s.add_argument("--commit", help="pin another commit than the vetted one")
    s.add_argument("--focus", action="append", metavar="SLOT",
                   help='a slot group to find problems for, "Action|Domain|Codebase type" (with optional '
                        '"|Prompt style|Interaction mode"); repeat for more')
    s.set_defaults(func=cmd_once)
    s = ds.add_parser("list", help="the queue and its states")
    s.set_defaults(func=cmd_list)
    s = ds.add_parser("pause", help="stop generating: the running generation stops and is queued again")
    s.add_argument("--let-finish", action="store_true", help="let a running generation finish")
    s.set_defaults(func=cmd_pause)
    s = ds.add_parser("resume", help="let the worker start generations again")
    s.set_defaults(func=cmd_resume)
    s = ds.add_parser("brief", help="write the plain-words repo brief for a dossier now (id, owner/name or all)")
    s.add_argument("target", help="a dossier id, owner/name, or all")
    s.add_argument("--force", action="store_true", help="write it again even when it is ready")
    s.add_argument("-v", "--verbose", action="store_true", help="print the generation's progress")
    s.set_defaults(func=cmd_brief)
    s = ds.add_parser("brief-show", help="print a repo brief (id or owner/name)")
    s.add_argument("target")
    s.add_argument("--json", action="store_true", help="the API's JSON, with the read-time facts")
    s.set_defaults(func=cmd_brief_show)
