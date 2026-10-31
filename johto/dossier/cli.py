"""`johto dossier run|once|list|pause|resume`."""
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
    with store.edit() as idx:
        it = store.find(idx, did)
        if it is None:
            old = store.find_repo(idx, entry["full_name"])
            if old is not None and old["status"] in ("queued", "failed"):
                idx["items"].remove(old)  # replaced by this run (same repo, maybe another commit)
            pool = [e["full_name"].lower() for e in candidates.ranked(worker.POOL_LIMIT)]
            rank = pool.index(entry["full_name"].lower()) + 1 if entry["full_name"].lower() in pool else None
            it = store.new_item(entry, rank, manual=True)
            idx["items"].append(it)
        else:  # 'generating' here is stale: this process holds the lock
            it.update(status="queued", manual=True, started_at=None, progress=None)
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


def register(sub) -> None:
    p = sub.add_parser("dossier", help="repo dossiers: research pages about promising repos")
    ds = p.add_subparsers(dest="dossier_cmd", required=True)
    s = ds.add_parser("run", help="run the worker in the foreground (respects pause)")
    s.set_defaults(func=cmd_run)
    s = ds.add_parser("once", help="generate one dossier now for owner/name, even when paused")
    s.add_argument("repo")
    s.add_argument("--commit", help="pin another commit than the vetted one")
    s.set_defaults(func=cmd_once)
    s = ds.add_parser("list", help="the queue and its states")
    s.set_defaults(func=cmd_list)
    s = ds.add_parser("pause", help="stop generating: the running generation stops and is queued again")
    s.add_argument("--let-finish", action="store_true", help="let a running generation finish")
    s.set_defaults(func=cmd_pause)
    s = ds.add_parser("resume", help="let the worker start generations again")
    s.set_defaults(func=cmd_resume)
