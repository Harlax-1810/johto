"""Lane routes (build-plan 5.2 "Lane" and 5.4): GET /api/lane, start, type, end, collect, unlock, Ghostty,
auto-prepare, and the WebSockets /ws/lane (the shared Claude terminal) and /ws/shell (a bash per socket).

Wiring for app.py:
    routes            put them before the /api/{rest:path} catch-all, and drop app.ws_lane's route
    start() / stop()  call from the lifespan (start needs the running loop)
    lane_state()      LaneState for /api/state and the events engine (data._lane_provider finds it)

Every request needs the session cookie; mutating requests and both sockets also need Origin and Host to be
this server on 127.0.0.1 or localhost (contract 5.1). In demo mode (cookie johto_mode=demo) GET /api/lane
and /ws/lane show the demo afternoon and the actions answer 409 `demo`.
"""
from __future__ import annotations

import asyncio
import json
import secrets
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .lane.service import LaneError, svc


def err(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "details": details or {}}}, status_code=status)


def _token() -> str:
    from . import app
    return app.TOKEN


def _cookie_ok(cookies) -> bool:
    return secrets.compare_digest(cookies.get("johto", ""), _token())


def _signed_in(cookies, headers, ws: bool = False) -> bool:
    """The token cookie, or this machine's own browser tab (app.local_ok; a socket by its Origin)."""
    from . import app
    return _cookie_ok(cookies) or app.local_ok(headers, ws=ws)


def _local(host: str | None) -> tuple[str, str] | None:
    """(hostname, port) when host is 127.0.0.1/localhost[:port]."""
    if not host:
        return None
    name, _, port = host.rpartition(":") if host.count(":") == 1 else (host, "", "")
    if name in ("127.0.0.1", "localhost"):
        return name, port
    return None


def _origin_ok(headers) -> bool:
    """The app's one rule when it has it (app.origin_ok), else: Host and Origin are this local server."""
    try:
        from . import app
        shared = getattr(app, "origin_ok", None)
    except Exception:
        shared = None
    if shared:
        return bool(shared(headers))
    host = _local(headers.get("host"))
    if not host:
        return False
    origin = headers.get("origin")
    if not origin:
        return False
    u = urlsplit(origin)
    if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost"):
        return False
    return str(u.port or "") == host[1]


def _demo(cookies) -> bool:
    return cookies.get("johto_mode") == "demo"


def guarded(fn, mutating: bool = True):
    async def wrap(req: Request):
        if not _signed_in(req.cookies, req.headers):
            return err(401, "unauthenticated", "Open the link johto web printed.")
        if mutating and not _origin_ok(req.headers):
            return err(403, "forbidden", "This request didn't come from the Johto page.")
        if mutating and _demo(req.cookies):
            return err(409, "demo", "The lane needs your real tasks. /?demo=0 shows them.")
        try:
            return await fn(req)
        except LaneError as e:
            return err(e.status, e.code, e.message, e.details)
        except Exception as e:  # ActionError and friends carry plain words
            code = getattr(e, "code", None)
            if code:
                return err(getattr(e, "status", 400), code, getattr(e, "message", str(e)), getattr(e, "details", {}))
            raise
    return wrap


async def _body(req: Request) -> dict:
    raw = await req.body()
    if not raw:
        return {}
    try:
        b = json.loads(raw)
    except ValueError:
        raise LaneError(400, "invalid", "The body isn't JSON.")
    if not isinstance(b, dict):
        raise LaneError(400, "invalid", "The body must be a JSON object.")
    return b


# --- HTTP -------------------------------------------------------------------------------------------------

async def get_lane(req: Request):
    if _demo(req.cookies):
        from . import app
        return JSONResponse((await asyncio.to_thread(app.state, req))["lane"])
    if not svc.running:
        await svc.refresh()
    return JSONResponse(svc.state())


async def post_start(req: Request):
    b = await _body(req)
    job = await svc.start_lane(str(b.get("short") or ""), str(b.get("kind") or ""))
    return JSONResponse({"job": job}, status_code=202)


async def post_type(req: Request):
    b = await _body(req)
    await svc.type(str(b.get("what") or ""))
    return Response(status_code=204)


async def post_end(req: Request):
    await svc.end()
    return Response(status_code=204)


async def post_collect(req: Request):
    return JSONResponse({"job": await svc.collect()}, status_code=202)


async def post_unlock(req: Request):
    await svc.unlock()
    return Response(status_code=204)


async def post_ghostty(req: Request):
    await svc.ghostty()
    return Response(status_code=204)


async def post_launch(req: Request):
    await svc.launch()
    return Response(status_code=204)


async def put_auto(req: Request):
    b = await _body(req)
    if not isinstance(b.get("on"), bool):
        raise LaneError(400, "invalid", "Send {\"on\": true} or {\"on\": false}.")
    return JSONResponse(await svc.set_auto_prepare(b["on"]))


# --- WebSockets -----------------------------------------------------------------------------------------

def _size(ws: WebSocket) -> tuple[int, int]:
    try:
        return int(ws.query_params.get("cols") or 100), int(ws.query_params.get("rows") or 32)
    except ValueError:
        return 100, 32


async def _ws_guard(ws: WebSocket) -> bool:
    await ws.accept()
    if not _signed_in(ws.cookies, ws.headers, ws=True):
        await ws.close(code=4401)
        return False
    if not _origin_ok(ws.headers):
        await ws.close(code=4403)
        return False
    return True


async def ws_lane(ws: WebSocket):
    if _demo(ws.cookies):
        from . import app   # the demo terminal (it accepts the socket itself)
        demo = getattr(app, "ws_lane_demo", None) or getattr(app, "ws_lane", None)
        if demo is not None and demo is not ws_lane:
            return await demo(ws)
    if not await _ws_guard(ws):
        return
    cols, rows = _size(ws)
    client = await svc.lane_client(ws.send_bytes, ws.send_text, cols, rows)
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                svc.lane_input(client, msg["bytes"])
            elif msg.get("text") is not None:
                try:
                    m = json.loads(msg["text"])
                except ValueError:
                    continue
                if isinstance(m, dict):
                    svc.lane_message(client, m)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        svc.lane_closed(client)


async def ws_shell(ws: WebSocket):
    if not await _ws_guard(ws):
        return
    cols, rows = _size(ws)
    pty = await svc.shell_pty(cols, rows)
    if pty is None:
        await ws.send_text(json.dumps({"type": "idle"}))
        try:
            while (await ws.receive())["type"] != "websocket.disconnect":
                pass
        except (WebSocketDisconnect, RuntimeError):
            pass
        return
    from .lane.pty_bridge import Client
    client = Client(ws.send_bytes, ws.send_text)
    client.text({"type": "hello", "run": dict((svc.state().get("run") or {})) or None, "locked": False})
    pty.attach(client)
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                pty.write(msg["bytes"])
            elif msg.get("text") is not None:
                try:
                    m = json.loads(msg["text"])
                except ValueError:
                    continue
                if isinstance(m, dict) and m.get("type") == "resize":
                    pty.resize(m.get("cols") or 100, m.get("rows") or 32)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        pty.detach(client)
        client.close()
        pty.kill()


# --- for app.py -------------------------------------------------------------------------------------------

routes = [
    Route("/api/lane", guarded(get_lane, mutating=False)),
    Route("/api/lane/start", guarded(post_start), methods=["POST"]),
    Route("/api/lane/type", guarded(post_type), methods=["POST"]),
    Route("/api/lane/end", guarded(post_end), methods=["POST"]),
    Route("/api/lane/collect", guarded(post_collect), methods=["POST"]),
    Route("/api/lane/unlock", guarded(post_unlock), methods=["POST"]),
    Route("/api/lane/ghostty", guarded(post_ghostty), methods=["POST"]),
    Route("/api/lane/launch", guarded(post_launch), methods=["POST"]),
    Route("/api/lane/auto-prepare", guarded(put_auto), methods=["PUT"]),
    WebSocketRoute("/ws/lane", ws_lane),
    WebSocketRoute("/ws/shell", ws_shell),
]


def start() -> None:
    """Lifespan hook: start the lane monitor on the running loop."""
    svc.start()


def stop() -> None:
    """Lifespan hook: stop the monitor and detach the server's terminal (Claude keeps running)."""
    svc.stop()


def lane_state() -> dict | None:
    """The current LaneState, or None until start() ran (the caller then uses its own fallback)."""
    return svc.state() if svc.running else None
