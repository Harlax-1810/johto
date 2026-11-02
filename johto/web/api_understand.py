"""Task understanding API (johto/understand.py): the Prompt step's glance and the page behind it.

GET  /api/tasks/{short}/understand          the document, its state and progress, the type's rules and precedent.
                                            Starts the research when the task has no current document, and a
                                            precedent refresh when that cache is old.
POST /api/tasks/{short}/understand/refresh  research it again (his press).

Mounted by app.py through EXTENSIONS (session cookie, Origin check, demo guard). Nothing here writes Austin's
fields or hands back wording for them.
"""
from __future__ import annotations

import asyncio
import json
import re

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .. import config

SHORT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _dir(short: str):
    if not SHORT.match(short or ""):
        return None
    d = config.TASKS / short
    return d if (d / "task.json").exists() else None


def _payload(d) -> dict:
    from .. import understand
    try:
        understand.ensure(d)
    except Exception as e:  # noqa: BLE001  (the page still shows what exists)
        print(f"johto web: understanding for {d.name} didn't start: {e}", flush=True)
    try:
        from .. import precedent
        precedent.ensure()
    except Exception as e:  # noqa: BLE001
        print(f"johto web: precedent refresh didn't start: {e}", flush=True)
    return understand.view(d)


async def get_understand(req: Request):
    if req.cookies.get("johto_mode") == "demo":
        return JSONResponse({"state": "missing", "doc": None, "demo": True, "fallback": {}, "rules": [], "notes": [],
                             "precedent": {}})
    d = _dir(req.path_params["short"])
    if d is None:
        return _err(404, "not_found", "No such task.")
    out = await asyncio.to_thread(_payload, d)
    return JSONResponse(json.loads(json.dumps(out, default=str)))


async def post_refresh(req: Request):
    d = _dir(req.path_params["short"])
    if d is None:
        return _err(404, "not_found", "No such task.")
    from .. import understand
    if not understand.available():
        return _err(409, "unavailable", "Headless Claude is off in fake mode.")
    job = await asyncio.to_thread(lambda: understand.ensure(d, force=True))
    return JSONResponse({"job": job, "toast": {"level": "info", "text": "Researching the task again. Often 10 to 20 minutes."}},
                        status_code=202)


routes = [
    Route("/api/tasks/{short}/understand", get_understand),
    Route("/api/tasks/{short}/understand/refresh", post_refresh, methods=["POST"]),
]
