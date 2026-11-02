"""Repo brief API (spec.md 6 and 7.1): GET /api/briefs, GET /api/briefs/{id}, POST /api/briefs/{id}/regenerate.

Owned by the `brief` builder. Mounted by app.py through EXTENSIONS, so every route here already gets the session
check, the Origin check on writes and the demo guard (writes answer "nothing was written" in demo mode). GET
handlers also answer in demo mode (cookie johto_mode=demo) from research files only (the fixture dossiers, then
the dossiers and briefs on disk), never from the queue or Austin's tasks.

GET /api/briefs/{id} -> {"id", "state", "brief": brief + read-time fields | null, "fallback": {...} | null,
"error": plain words | null, "progress": {"step", "elapsed_s"} | null} (johto.dossier.brief.view). The
regenerate POST is Austin's press: it runs even while research is paused, waits for any other generation to
finish (one at a time), and answers 409 while this brief is already generating.
"""
from __future__ import annotations

import asyncio
import threading
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .. import config
from ..dossier import brief, store


def err(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "details": details or {}}}, status_code=status)


def _demo(req: Request) -> bool:
    return req.cookies.get("johto_mode") == "demo"


async def list_briefs(req: Request):
    return JSONResponse({"items": await asyncio.to_thread(brief.items, demo=_demo(req))})


async def get_brief(req: Request):
    did = req.path_params["id"]
    try:
        return JSONResponse(await asyncio.to_thread(brief.view, did, demo=_demo(req)))
    except KeyError:
        return err(404, "not_found", "There is no research for that repo.")


_LOCK = threading.Lock()   # the "already running?" look and the submit, as one step (a double click)


def _start(did: str) -> dict | None:
    """The job, or None when this brief is already generating."""
    from . import jobs
    target = f"b:{did}"
    with _LOCK:
        if jobs.active(kind="brief", target=target) or brief.state(did) == "generating":
            return None
        asked = time.time()

        def work(ctx):
            from ..dossier import generate as dgen
            cancel = dgen.Cancel()
            stop = threading.Event()

            def follow():   # the job's cancel stops the claude process too
                while not stop.wait(1.0):
                    if ctx.cancelled:
                        cancel.cancel("stopped in the app")
                        return
            threading.Thread(target=follow, daemon=True, name="brief-job-cancel").start()
            ctx.step("waiting", ["waiting", "researching", "checking"])
            last = {"step": "waiting"}

            def on_progress(snap):
                step = snap.get("step")
                if step in ("researching", "checking") and last["step"] != step:
                    last["step"] = step
                    ctx.step(step)
            try:
                res = brief.generate(did, cancel=cancel, on_progress=on_progress, not_before=asked)
            finally:
                stop.set()
            ctx.add_cost(res.get("cost_usd") or 0)
            if res.get("cancelled"):
                raise jobs.Cancelled()
            if not res.get("ok"):
                from ..actions import ActionError
                raise ActionError("brief_failed", "The brief could not be written.",
                                  {"errors": (res.get("errors") or [])[:10]})
            return {"state": brief.state(did), "skipped": bool(res.get("skipped"))}
        return jobs.submit("brief", target, work, pool="ai", priority=1, params={"dossier_id": did},
                           steps=["waiting", "researching", "checking"], step="waiting")


async def regenerate(req: Request):
    did = req.path_params["id"]
    if store.load_dossier(did) is None:
        return err(404, "not_found", "There is no research for that repo.")
    if config.FAKE and not config.CLAUDE_BIN_SET and brief.CLAUDE_BIN is None:
        return JSONResponse({"toast": {"id": f"brief-{did[-8:]}", "level": "info",
                                       "text": "Research is off in this copy of the app.", "undo": None,
                                       "link": None}}, status_code=202)
    job = await asyncio.to_thread(_start, did)
    if job is None:
        return err(409, "conflict", "This brief is being written right now.")
    return JSONResponse({"job": job}, status_code=202)


routes = [
    Route("/api/briefs", list_briefs),
    Route("/api/briefs/{id}", get_brief),
    Route("/api/briefs/{id}/regenerate", regenerate, methods=["POST"]),
]
