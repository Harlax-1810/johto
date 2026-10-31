"""Dossier API (docs/dossier-spec.md section 5), backed by johto.dossier.

Demo mode (cookie johto_mode=demo) also lists the fixture dossiers in johto/web/fixtures/dossiers/;
actions on a fixture change only an in-memory copy.

PUT /api/dossiers/worker {"paused": true} also stops a running generation (it shares Austin's
subscription with his lanes) and queues it again; {"paused": true, "stop": false} lets it finish.
The app's lifespan calls start_worker() on startup and stop_worker() on shutdown.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..dossier import candidates, schema, store, worker

FIXTURES = Path(__file__).parent / "fixtures" / "dossiers"
_demo_state: dict[str, dict] = {}  # fixture id -> item overrides from demo actions


def err(status: int, code: str, message: str, details: dict | None = None):
    return JSONResponse({"error": {"code": code, "message": message, "details": details or {}}}, status_code=status)


def _authed(req: Request) -> bool:
    from .app import authed  # late: app imports this module
    return authed(req)


def guarded(fn):
    async def wrap(req: Request):
        if not _authed(req):
            return err(401, "unauthenticated", "Open the link johto web printed.")
        try:
            return await fn(req)
        except KeyError as e:
            return err(404, "not_found", f"No dossier {e.args[0] if e.args else ''}")
        except worker.Conflict as e:
            return err(409, "conflict", str(e))
        except ValueError as e:
            return err(400, "bad_request", str(e))
    return wrap


def _demo(req) -> bool:
    return req.cookies.get("johto_mode") == "demo"


def _fixture_items() -> list[tuple[dict, dict]]:
    out = []
    for p in sorted(FIXTURES.glob("*.json")):
        d = json.loads(p.read_text())
        item = {"id": d["id"], "repo": d["repo"], "commit": d["commit"], "status": "ready", "rank": len(out) + 1,
                "promise": 0.8, "promise_reasons": [], "queued_at": d.get("generated_at"), "started_at": None,
                "finished_at": d.get("generated_at"), "progress": None, "cost_usd": d.get("cost_usd"),
                "error": None, "decline": None, "actioned": None, "demo": True}
        item.update(_demo_state.get(d["id"], {}))
        out.append((item, d))
    return out


def _fixture(did: str) -> tuple[dict, dict] | None:
    return next(((i, d) for i, d in _fixture_items() if i["id"] == did), None)


def _fixture_for(req: Request, did: str) -> tuple[dict, dict] | None:
    """The fixture this request means: in demo mode a fixture wins; otherwise only when no real item has
    that id, so actions on a real dossier never land on its demo copy."""
    if _demo(req) or store.get(did) is None:
        return _fixture(did)
    return None


def _fits(item: dict, d: dict | None, combos: dict) -> list[dict]:
    """Slot combinations this repo fits, with open counts from data/slots.json."""
    seen, rows = set(), []
    if d:
        src = list((d.get("verdict") or {}).get("best_fits") or [])
        for o in schema.opportunities(d):
            src += list(o.get("slot_fit") or [])
    else:
        src = list(item.get("fits") or [])
    for f in src:
        k = (f.get("action"), f.get("domain"), f.get("codebase_type"))
        if None in k or k in seen:
            continue
        seen.add(k)
        rows.append({"action": k[0], "domain": k[1], "codebase_type": k[2], "open": combos.get(k, 0)})
    return rows


def _summary(item: dict, d: dict | None, combos: dict) -> dict:
    s = dict(item)
    s.update(headline=None, verdict=None, top=[])
    if d:
        ops = schema.opportunities(d)
        s.update(headline=d.get("headline"), verdict=d.get("verdict"),
                 top=[{"id": o.get("id"), "title": o.get("title"), "area": o.get("area"),
                       "one_shot": o.get("one_shot"), "slot_fit": o.get("slot_fit"),
                       "size": (o.get("bounded_task") or {}).get("size")} for o in ops[:3]])
    s["fits"] = _fits(item, d, combos)
    return s


ORDER = {"ready": 0, "generating": 1, "queued": 2, "failed": 3}


def _sorted(items: list[dict]) -> list[dict]:
    live = [it for it in items if it["status"] in ORDER]
    live.sort(key=lambda it: (ORDER[it["status"]], it.get("rank") or 10**6))
    closed = [it for it in items if it["status"] in store.CLOSED]
    closed.sort(key=lambda it: it.get("closed_at") or it.get("finished_at") or "", reverse=True)
    return live + closed[:20]


def _list(demo: bool) -> dict:
    idx = store.load()
    combos = candidates.open_combos()
    pairs = [(it, store.load_dossier(it["id"]) if it["status"] in ("ready", "actioned", "declined") else None)
             for it in idx["items"]]
    if demo:  # the fixture wins over a real item with the same id
        fx = _fixture_items()
        fx_ids = {i["id"] for i, _ in fx}
        pairs = fx + [(it, d) for it, d in pairs if it["id"] not in fx_ids]
    by_id = {it["id"]: d for it, d in pairs}
    items = [_summary(it, by_id[it["id"]], combos) for it in _sorted([it for it, _ in pairs])]
    return {"target": idx.get("target", store.TARGET), "paused": bool(idx.get("paused")),
            "worker": worker.status(), "items": items}


@guarded
async def list_dossiers(req: Request):
    return JSONResponse(await asyncio.to_thread(_list, _demo(req)))


@guarded
async def get_dossier(req: Request):
    did = req.path_params["id"]
    fx = _fixture_for(req, did)
    if fx:
        return JSONResponse({"item": fx[0], "dossier": fx[1]})
    it = store.get(did)
    if not it:
        fx = _fixture(did)  # links to a fixture keep working outside demo mode
        if fx:
            return JSONResponse({"item": fx[0], "dossier": fx[1]})
        raise KeyError(did)
    return JSONResponse({"item": it, "dossier": store.load_dossier(did)})


async def _body(req: Request) -> dict:
    try:
        b = await req.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return b if isinstance(b, dict) else {}


def _demo_action(did: str, **fields) -> JSONResponse:
    _demo_state.setdefault(did, {}).update(fields)
    return JSONResponse({"item": _fixture(did)[0]})


@guarded
async def decline(req: Request):
    did, b = req.path_params["id"], await _body(req)
    reason, note = b.get("reason"), b.get("note") or ""
    if _fixture_for(req, did):
        if reason not in store.DECLINE_REASONS:
            raise ValueError(f"reason must be one of {', '.join(store.DECLINE_REASONS)}")
        return _demo_action(did, status="declined", decline={"reason": reason, "note": note})
    return JSONResponse({"item": await asyncio.to_thread(worker.decline, did, reason, note)})


@guarded
async def act(req: Request):
    did, b = req.path_params["id"], await _body(req)
    if _fixture_for(req, did):
        return _demo_action(did, status="actioned", actioned={"task_short": b.get("task_short"),
                                                              "opportunity_id": b.get("opportunity_id")})
    return JSONResponse({"item": await asyncio.to_thread(worker.act, did, b.get("opportunity_id"),
                                                         b.get("task_short"))})


@guarded
async def restore(req: Request):
    did = req.path_params["id"]
    if _fixture_for(req, did):
        _demo_state.pop(did, None)
        return JSONResponse({"item": _fixture(did)[0]})
    return JSONResponse({"item": await asyncio.to_thread(worker.restore, did)})


@guarded
async def refill(req: Request):
    start_worker()
    worker.WORKER.kick()
    return JSONResponse({"worker": await asyncio.to_thread(worker.status)}, status_code=202)


@guarded
async def put_worker(req: Request):
    b = await _body(req)
    if not isinstance(b.get("paused"), bool):
        raise ValueError("paused must be true or false")
    stop = b.get("stop", True)
    if not isinstance(stop, bool):
        raise ValueError("stop must be true or false")
    st = await asyncio.to_thread(worker.set_paused, b["paused"],
                                 "paused in the app" if b["paused"] else None, stop and b["paused"])
    stopping = st.pop("stopping", [])
    return JSONResponse({"worker": st, "paused": st["paused"], "stopping": stopping})


routes = [
    Route("/api/dossiers", list_dossiers),
    Route("/api/dossiers/refill", refill, methods=["POST"]),
    Route("/api/dossiers/worker", put_worker, methods=["PUT"]),
    Route("/api/dossiers/{id}", get_dossier),
    Route("/api/dossiers/{id}/decline", decline, methods=["POST"]),
    Route("/api/dossiers/{id}/act", act, methods=["POST"]),
    Route("/api/dossiers/{id}/restore", restore, methods=["POST"]),
]


def start_worker() -> None:
    """Started from the app lifespan: the background thread that keeps the ready set filled."""
    worker.start()


def stop_worker(timeout: float = 20.0) -> None:
    """For the app lifespan's shutdown: stops the worker thread and a generation running in this process
    (the item goes back to queued), and waits up to `timeout` seconds for its cleanup."""
    worker.shutdown(timeout)
