"""The sent-back checklist (johto.feedback) in the web app: its jobs, its routes, and the hook that starts it
when Studio shows a review.

GET  /api/tasks/{short}/feedback              the checklist, with the running job if any
POST /api/tasks/{short}/feedback/plan         make the checklist again
POST /api/tasks/{short}/feedback/check        check the items against his fields now
POST /api/tasks/{short}/feedback/items/{id}   {"done": bool}: his own tick
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .. import config, feedback, workspace
from . import jobs
from .api_task import SHORT, ApiError, handled

_LOCK = threading.Lock()   # the "already running?" look and the submit, as one step
# Checklists start by themselves only in the running app (start() below), never in tests or fake mode
# without a stub claude. Austin's buttons (plan, check) work either way.
AUTO = False


def start() -> None:
    """The app's lifespan hook: turn the automatic checklist on, then catch up on tasks already sent back."""
    global AUTO
    import os
    if os.environ.get("JOHTO_NO_FEEDBACK") or (config.FAKE and not config.CLAUDE_BIN_SET):
        return
    AUTO = True
    threading.Thread(target=_catch_up, name="feedback-catch-up", daemon=True).start()


def stop() -> None:
    global AUTO
    AUTO = False


def _catch_up() -> None:
    for tj in sorted(config.TASKS.glob("*/task.json")):
        try:
            ensure(tj.parent)
        except Exception as e:  # noqa: BLE001  (one task's trouble never stops the rest)
            print(f"feedback: {tj.parent.name}: {e}", flush=True)


def _dir(short: str) -> Path:
    d = config.TASKS / short
    if not SHORT.match(short or "") or not (d / "task.json").exists():
        raise ApiError(404, "not_found", f"No task folder for {short}.")
    return d


def _changed(short: str) -> None:
    try:
        from .api_actions import task_changed
        task_changed(short, ["feedback"])
    except Exception:  # noqa: BLE001
        pass


def live_job(short: str) -> dict | None:
    js = jobs.active(kind="feedback_plan", target=f"t:{short}") + jobs.active(kind="feedback_check", target=f"t:{short}")
    return js[0] if js else None


def submit_plan(d: Path) -> dict:
    short = d.name

    def work(ctx):
        ctx.step("planning")
        out = feedback.plan(d, log=ctx.log, add_cost=getattr(ctx, "add_cost", None))
        _changed(short)
        if out.get("items"):
            ensure(d)   # a check right away, so items he already dealt with show as done
        return {"items": len(out.get("items") or []), "error": out.get("error")}
    with _LOCK:
        live = jobs.active(kind="feedback_plan", target=f"t:{short}")
        if live:
            return live[0]
        return jobs.submit("feedback_plan", f"t:{short}", work, pool="ai", priority=1, steps=["planning"],
                           step="waiting")


def submit_check(d: Path) -> dict:
    short = d.name

    def work(ctx):
        ctx.step("checking")
        out = feedback.check(d, log=ctx.log, add_cost=getattr(ctx, "add_cost", None))
        _changed(short)
        return {"check": (out.get("check") or {}).get("at")}
    with _LOCK:
        live = jobs.active(kind="feedback_check", target=f"t:{short}")
        if live:
            return live[0]
        return jobs.submit("feedback_check", f"t:{short}", work, pool="ai", priority=2, steps=["checking"],
                           step="waiting")


def ensure(d: Path, meta: dict | None = None) -> dict | None:
    """Start what a sent-back task's checklist needs: a plan for a new review, a check when his fields changed.
    Returns the job started or running, else None. Only for tasks the reviewer sent back."""
    if not AUTO:
        return None
    from .domain.derive import sent_back_since
    meta = meta if meta is not None else workspace.load(d)
    if sent_back_since(meta) is None:
        return None
    live = live_job(d.name)
    if live:
        return live
    data = feedback.load(d)
    if feedback.needs_plan(d, meta, data):
        return submit_plan(d)
    if feedback.needs_check(d, meta, data):
        return submit_check(d)
    return None


def view(d: Path) -> dict:
    meta = workspace.load(d)
    v = feedback.view(d, meta)
    job = live_job(d.name)
    return {"plan": v, "job": {"id": job.get("id"), "kind": job.get("kind"), "state": job.get("state")} if job else None}


@handled
async def get_feedback(req: Request):
    d = _dir(req.path_params["short"])
    return JSONResponse(await asyncio.to_thread(view, d))


@handled
async def post_plan(req: Request):
    d = _dir(req.path_params["short"])
    job = await asyncio.to_thread(submit_plan, d)
    return JSONResponse({"job": job}, status_code=202)


@handled
async def post_check(req: Request):
    d = _dir(req.path_params["short"])
    job = await asyncio.to_thread(submit_check, d)
    return JSONResponse({"job": job}, status_code=202)


@handled
async def post_item(req: Request):
    d = _dir(req.path_params["short"])
    try:
        body = await req.json()
    except ValueError:
        body = {}
    try:
        await asyncio.to_thread(feedback.mark, d, req.path_params["item"], bool(body.get("done")))
    except KeyError:
        raise ApiError(404, "not_found", "No such item in the checklist.")
    _changed(d.name)
    return JSONResponse(await asyncio.to_thread(view, d))


routes = [
    Route("/api/tasks/{short}/feedback", get_feedback),
    Route("/api/tasks/{short}/feedback/plan", post_plan, methods=["POST"]),
    Route("/api/tasks/{short}/feedback/check", post_check, methods=["POST"]),
    Route("/api/tasks/{short}/feedback/items/{item}", post_item, methods=["POST"]),
]
