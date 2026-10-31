"""Starlette app for `johto web`: the real UI over real files, plus the demo afternoon.

Real mode derives everything from tasks/ and data/ (johto.web.data and johto.web.domain) and keeps the
browser current over SSE (johto.web.events). Writes come from the builders' modules mounted below:
api_actions (claim, decisions, Studio save / AutoQC / submit / unclaim, collect, discard, undo, jobs),
api_lane (the lane, its terminals) and api_pack (review packs), plus api_dossier. In demo mode those
writes answer with a "Not connected yet" toast, as they always have, so the demo can't touch real tasks.

Lifespan: start the actions jobs runner, the lane monitor, the lane watcher (skipped with JOHTO_NO_WATCH),
the dossier worker (skipped with JOHTO_NO_WORKER) and the SSE engine with its file watcher.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import re
import secrets
import subprocess
import time
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute, websocket_session
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket

from .. import config, workspace
from . import data, demo, events
from . import api_dossier, api_pack, lane_watch

STATIC = Path(__file__).parent / "static"
TOKEN = secrets.token_urlsafe(24)
STARTED = data.now_iso()
PORT = config.PORT
_demo = demo.build()
EXTENSIONS = ("api_actions", "api_lane")   # builder modules mounted behind the demo guard
PRESENCE: dict = {}
NONCES: dict[str, tuple[str, str, float]] = {}


# --- mode and auth ---------------------------------------------------------

def mode(req) -> str:
    return "demo" if req.cookies.get("johto_mode") == "demo" else "real"


def demo_lane(req) -> str:
    v = req.cookies.get("johto_lane", "working")
    return v if v in ("working", "waiting", "free", "needs_login", "ready_for_prompt") else "working"


def authed(req) -> bool:
    """The token cookie, or a request from this machine's own browser tab (see local_ok)."""
    return secrets.compare_digest(req.cookies.get("johto", ""), TOKEN) or local_ok(req.headers)


def err(status: int, code: str, message: str, details: dict | None = None):
    return JSONResponse({"error": {"code": code, "message": message, "details": details or {}}}, status_code=status)


LOCAL = ("127.0.0.1", "localhost")


def origin_ok(headers) -> bool:
    """Build-plan 0.7 for writes and sockets: Host is 127.0.0.1 or localhost, and Origin is this same server.
    Browsers always send Origin on writes and WebSockets, so a missing Origin is refused (curl, scripts)."""
    host = headers.get("host")
    if not host:
        return False
    name, _, port = host.rpartition(":") if ":" in host else (host, "", "")
    if name not in LOCAL:
        return False
    origin = headers.get("origin")
    if not origin:
        return False
    return origin in {f"http://{h}:{port}" if port else f"http://{h}" for h in LOCAL}


def local_ok(headers, ws: bool = False) -> bool:
    """No token needed for this machine's own browser tab: the request is addressed to 127.0.0.1 or localhost
    (a DNS-rebinding page has its own Host), the browser's fetch metadata says it came from this page or a
    typed URL (Sec-Fetch-Site same-origin or none; curl, urllib, requests and websockets send none of it), and
    Origin, when sent, is this server. Writes and sockets also need Origin (origin_ok).

    WebSocket handshakes carry no fetch metadata (Chromium sends only Origin), so a socket passes on a present,
    matching Origin alone; python websockets, curl and friends send no Origin by default."""
    if ws:
        return origin_ok(headers)
    host = headers.get("host")
    if not host:
        return False
    name = host.rpartition(":")[0] if ":" in host else host
    if name not in LOCAL:
        return False
    if headers.get("sec-fetch-site") not in ("same-origin", "none"):
        return False
    if headers.get("origin") is not None and not origin_ok(headers):
        return False
    return True


class _Static(StaticFiles):
    """Static files the browser revalidates on every load (by ETag), so app edits show without a hard reload."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


class _ASGI:
    """A plain ASGI app (Starlette treats functions as request handlers, instances as ASGI apps)."""

    def __init__(self, fn):
        self.fn = fn

    async def __call__(self, scope, receive, send):
        await self.fn(scope, receive, send)


def guarded(fn):
    async def wrap(req: Request):
        if not authed(req):
            return err(401, "unauthenticated", "Open the link johto web printed.")
        if req.method not in ("GET", "HEAD") and not origin_ok(req.headers):
            return err(403, "forbidden", "This request did not come from the Johto page.")
        try:
            return await fn(req)
        except KeyError as e:
            return err(404, "not_found", f"Not found: {e.args[0] if e.args else e}")
        except FileNotFoundError as e:
            return err(404, "not_found", str(e))
    wrap.__name__ = getattr(fn, "__name__", "wrap")
    return wrap


# --- builder modules ---------------------------------------------------------

def _ext(name: str):
    try:
        return importlib.import_module(f"johto.web.{name}")
    except ModuleNotFoundError as e:
        if e.name != f"johto.web.{name}":
            print(f"johto web: {name} did not load: {e}", flush=True)
        return None
    except Exception as e:  # a module mid-edit must not keep the app from starting
        print(f"johto web: {name} did not load: {e}", flush=True)
        return None


def _not_wired(req: Request) -> JSONResponse:
    text = ("Demo data: nothing was written. /?demo=0 shows your real tasks." if mode(req) == "demo"
            else "That action isn't connected yet")
    return JSONResponse({"toast": {"id": secrets.token_hex(4), "level": "info", "text": text, "undo": None, "link": None}},
                        status_code=202)


def _guard_ext(route: Route, demo_override=None) -> Route:
    """Wrap a builder route: session cookie, Origin/Host on writes, and the demo guard."""
    inner = route.app

    async def app(scope, receive, send):
        req = Request(scope, receive)
        if not authed(req):
            resp = err(401, "unauthenticated", "Open the link johto web printed.")
        elif req.method not in ("GET", "HEAD") and not origin_ok(req.headers):
            resp = err(403, "forbidden", "This request did not come from the Johto page.")
        elif mode(req) == "demo" and req.method not in ("GET", "HEAD"):
            resp = await demo_override(req) if demo_override else _not_wired(req)
        else:
            await inner(scope, receive, send)
            return
        await resp(scope, receive, send)
    return Route(route.path, endpoint=_ASGI(app), methods=sorted(route.methods or []) or None, name=route.name)


def _ext_routes() -> tuple[list, dict]:
    """(http routes, {ws path: asgi app}) from the builder modules that are present."""
    http, ws = [], {}
    for name in EXTENSIONS:
        m = _ext(name)
        for r in getattr(m, "routes", None) or []:
            if isinstance(r, WebSocketRoute):
                ws[r.path] = r.app
            elif isinstance(r, Route):
                http.append(_guard_ext(r, DEMO_OVERRIDES.get((r.path, frozenset(r.methods or ())))))
            else:
                http.append(r)
    return http, ws


# --- state -------------------------------------------------------------------

def _server(req) -> dict:
    if mode(req) == "demo":  # exactly what the demo always said
        return {"version": "0.1", "fake": False, "read_only": True, "started_at": STARTED, "port": 8765, "mode": "demo"}
    return {"version": "0.2", "fake": config.FAKE_STUDIO, "read_only": _ext("api_actions") is None,
            "started_at": STARTED, "port": PORT, "mode": "real"}


def _demo_state(req) -> dict:
    ls = demo_lane(req)
    tasks = _demo["tasks"]
    q, running = demo.queue(tasks, ls, req.cookies.get("johto_more") == "1")
    lane = demo.lane(tasks, ls)
    if ls in ("free", "needs_login", "ready_for_prompt"):
        lane["state"] = ls
        if ls == "free":
            lane["run"] = None
            lane["facts"] = None
    hidden = set() if req.cookies.get("johto_more") == "1" else {"d3m0aaa4", "d3m0aaa8"}
    summaries = [t["summary"] for k, t in tasks.items() if k not in hidden]
    slots = data.slots_summary()
    leads = data.ranked_leads()
    for it in q:
        if it["kind"] == "pick":
            it["label"] = f"{len(leads['leads'])} leads fit · {slots['open']:,} open"
    return {
        "server": _server(req),
        "settings": {"auto_prepare": True, "studio_filling": "automatic", "daily_cap_usd": 10},
        "lane": lane, "queue": q, "running": running, "tasks": summaries, "jobs": [],
        "spend": {"today_usd": 1.54, "cap_usd": 10, "reserve_usd": 2, "paused_until": None},
        "studio": {"state": "ok", "pending": 0, "last_ok_at": data.now_iso()},
        "slots": {"open": slots["open"], "synced_at": slots["synced_at"]},
    }


def _lane_jobs() -> list[dict]:
    try:
        from .lane import service
        return service.svc.job_list()
    except Exception:
        return []


def _real_state(req) -> dict:
    snap = data.real_snapshot()
    slots = data.slots_summary()
    st = data.settings()
    jobs = snap["jobs"] + [j for j in _lane_jobs() if j.get("id") not in {x.get("id") for x in snap["jobs"]}]
    return {
        "server": _server(req),
        "settings": {**st, "auto_prepare": st.get("auto_prepare_lane", True), "daily_cap_usd": st.get("daily_ai_cap_usd", 10)},
        "lane": snap["lane"], "queue": snap["queue"], "running": snap["running"], "tasks": snap["summaries"],
        "jobs": jobs,
        "spend": {"today_usd": _spend_today(), "cap_usd": st.get("daily_ai_cap_usd", 10), "reserve_usd": st.get("ai_reserve_usd", 2),
                  "paused_until": None},
        "studio": data.studio_state(),
        "slots": {"open": slots["open"], "synced_at": slots["synced_at"]},
    }


def _spend_today() -> float:
    m = _ext("api_actions")
    try:
        return m.spend_today() if m else 0.0
    except Exception:
        return 0.0


def state(req) -> dict:
    return _demo_state(req) if mode(req) == "demo" else _real_state(req)


# --- pages -------------------------------------------------------------------

async def index(req: Request):
    t = req.query_params.get("t")
    if t and secrets.compare_digest(t, TOKEN):
        resp = RedirectResponse(req.url.path + ("?" + "&".join(f"{k}={v}" for k, v in req.query_params.items() if k != "t")
                                                if len(req.query_params) > 1 else ""))
        resp.set_cookie("johto", TOKEN, httponly=True, samesite="strict")
        return resp
    resp = FileResponse(STATIC / "index.html")
    # Plain local URLs need no token, so no other site may frame the app and click through it.
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Cache-Control"] = "no-cache"
    if "demo" in req.query_params:
        resp.set_cookie("johto_mode", "demo" if req.query_params["demo"] == "1" else "real", samesite="strict")
    if "more" in req.query_params:
        resp.set_cookie("johto_more", req.query_params["more"], samesite="strict")
    if "lane" in req.query_params:
        resp.set_cookie("johto_lane", req.query_params["lane"], samesite="strict")
    return resp


# --- reads ---------------------------------------------------------------------

@guarded
async def api_state(req):
    return JSONResponse(await asyncio.to_thread(state, req))


def _task_dir(short: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", short or "") or not (config.TASKS / short / "task.json").exists():
        raise KeyError(short)
    return config.TASKS / short


def _task(req, short) -> dict:
    if mode(req) == "demo":
        return _demo["tasks"][short]
    return data.real_task(_task_dir(short))


@guarded
async def api_task(req):
    return JSONResponse(await asyncio.to_thread(_task, req, req.path_params["short"]))


@guarded
async def api_tasks(req):
    if mode(req) == "demo":
        return JSONResponse({"tasks": _demo_state(req)["tasks"]})
    snap = await asyncio.to_thread(data.real_snapshot)
    return JSONResponse({"tasks": snap["summaries"]})


@guarded
async def api_run(req):
    short, run = req.path_params["short"], req.path_params["run"]
    if mode(req) == "demo":
        r = next((r for r in _demo["tasks"][short]["runs"] if r["name"] == run), None)
    else:
        r = await asyncio.to_thread(data.real_run, _task_dir(short), run)
    if r is None:
        raise KeyError(run)
    return JSONResponse(r)


def _demo_transcript() -> dict:
    lines = [l for l in demo.TERMINAL if l and not l.startswith("\x1b[2m╭") and not l.startswith("\x1b[2m╰")]
    clean = [re.sub(r"\x1b\[[0-9;]*m", "", l) for l in lines]
    return {"entries": [{"i": i, "ts": None, "role": "claude", "turn": 1, "text": t, "tool": None}
                        for i, t in enumerate(clean)], "next": None}


@guarded
async def api_transcript(req):
    if mode(req) == "demo":
        return JSONResponse(_demo_transcript())
    short, run = req.path_params["short"], req.path_params["run"]
    after = int(req.query_params.get("after") or 0)
    limit = max(1, min(2000, int(req.query_params.get("limit") or 200)))

    def build():
        tpath, ws = data.run_transcript_path(_task_dir(short), run)
        if not tpath:
            return {"entries": [], "next": None, "total": 0}
        return data._domain().facts.transcript_view(tpath, after=after, limit=limit, workspace=ws)
    return JSONResponse(await asyncio.to_thread(build))


@guarded
async def api_diff(req):
    short, run = req.path_params["short"], req.path_params["run"]
    if mode(req) == "demo":
        raise KeyError(run)

    def read():
        r = data.real_run(_task_dir(short), run)
        if not r or not r.get("diff"):
            raise KeyError(f"{run} has no collected diff")
        return Path(r["diff"]).read_text(errors="replace")
    return PlainTextResponse(await asyncio.to_thread(read))


@guarded
async def api_leads(req):
    return JSONResponse(await asyncio.to_thread(data.ranked_leads))


@guarded
async def api_slots(req):
    return JSONResponse(await asyncio.to_thread(data.slots_summary))


@guarded
async def api_repos(req):
    return JSONResponse({"repos": await asyncio.to_thread(data.repos)})


FINISHED_STAGES = {"in_review": "In review", "sent_back": "Sent back", "accepted": "Accepted", "rejected": "Rejected",
                   "dropped": "Dropped", "discarded": "Dropped", "unclaimed": "Unclaimed"}


def _real_finished() -> dict:
    snap = data.real_snapshot()
    rows = []
    for st, s in zip(snap["states"], snap["summaries"]):
        if s["stage"] not in FINISHED_STAGES:
            continue
        meta = st["meta"]
        studio = meta.get("studio") or {}
        runs = [f"{'c' if r['kind'] == 'calib' else 'v'}{r['n']}" for r in st["runs"]]
        last = {"in_review": "submitted", "sent_back": "sent back", "accepted": "accepted", "rejected": "rejected",
                "dropped": "dropped", "discarded": "dropped", "unclaimed": "unclaimed"}[s["stage"]]
        when = data._domain().queue.clock(st["stage"].get("since"))
        rows.append({"short": s["short"], "label": s["label"], "repo": s["repo"].get("full_name"),
                     "slot": data.short_slot(s["slot"]), "status": FINISHED_STAGES[s["stage"]],
                     "submitted_at": meta.get("submitted_at"), "times_submitted": studio.get("times_submitted") or 0,
                     "my_min": s.get("my_min"), "claude_min": s.get("claude_min"), "runs": runs,
                     "last": f"{last} {when}".strip(), "feedback": studio.get("feedback")})
    count = lambda *ks: sum(1 for r in rows if r["status"] in ks)  # noqa: E731
    week = {"accepted": count("Accepted"), "in_review": count("In review"), "sent_back": count("Sent back"),
            "dropped": count("Dropped"), "dropped_reasons": {}, "hours_per_accepted": None, "lane_busy_pct": None,
            "claude_waited_min": None}
    return {"rows": rows, "week": week if rows else None, "ledger": [], "spend": None}


@guarded
async def api_finished(req):
    if mode(req) == "demo":
        return JSONResponse(demo.FINISHED)
    return JSONResponse(await asyncio.to_thread(_real_finished))


# Real code at the pinned commit, through gh, cached on disk.
CODE_CACHE = config.DATA / "cache" / "code"


def _target_repo(req, target: str) -> tuple[str, str]:
    kind, _, ident = target.partition(":")
    if kind == "d":
        for base in (config.DATA / "dossiers", Path(__file__).parent / "fixtures" / "dossiers"):
            p = base / f"{ident}.json"
            if p.exists():
                d = json.loads(p.read_text())
                return d["repo"], d["commit"]
        raise KeyError(target)
    if kind == "l":
        lead = data._lead_by_id(ident)
        if not lead:
            raise KeyError(target)
        return lead["repo"], lead["commit"]
    if mode(req) == "demo":
        r = _demo["tasks"][ident]["summary"]["repo"]
    else:
        r = workspace.load(_task_dir(ident))["repo"]
    return r["full_name"], r["commit"]


def _code_file(repo: str, commit: str, path: str) -> str:
    p = CODE_CACHE / repo.replace("/", "__") / commit / path
    if p.exists():
        return p.read_text(errors="replace")
    r = subprocess.run(["gh", "api", f"repos/{repo}/contents/{path}?ref={commit}",
                        "-H", "Accept: application/vnd.github.raw"], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise FileNotFoundError(r.stderr.strip()[:300])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(r.stdout)
    return r.stdout


@guarded
async def api_code_file(req):
    target = req.path_params["target"]
    path = req.query_params.get("path", "")
    repo, commit = _target_repo(req, target)
    try:
        text = await asyncio.to_thread(_code_file, repo, commit, path)
    except FileNotFoundError as e:
        return err(404, "not_found", f"{path} is not in {repo} at {commit[:8]}", {"detail": str(e)})
    lines = text.splitlines()
    start = int(req.query_params.get("start") or 1)
    end = int(req.query_params.get("end") or len(lines))
    ext = path.rsplit(".", 1)[-1] if "." in path else ""
    return JSONResponse({"commit": commit, "path": path, "lang": ext, "total_lines": len(lines),
                         "lines": [[i, lines[i - 1]] for i in range(max(1, start), min(len(lines), end) + 1)]})


def _code_tree(repo: str, commit: str) -> list[dict]:
    """Every path at the commit (one `gh api` call, cached on disk): [{path, type: blob|tree, size}]."""
    p = CODE_CACHE / repo.replace("/", "__") / commit / ".johto-tree.json"
    if p.exists():
        return json.loads(p.read_text())
    r = subprocess.run(["gh", "api", f"repos/{repo}/git/trees/{commit}?recursive=1"], capture_output=True, text=True,
                       timeout=60)
    if r.returncode != 0:
        raise FileNotFoundError(r.stderr.strip()[:300])
    tree = [{"path": e["path"], "type": e["type"], "size": e.get("size")} for e in json.loads(r.stdout).get("tree", [])
            if e.get("type") in ("blob", "tree")]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tree))
    return tree


@guarded
async def api_code_tree(req):
    target = req.path_params["target"]
    path = (req.query_params.get("path") or "").strip("/")
    repo, commit = _target_repo(req, target)
    try:
        tree = await asyncio.to_thread(_code_tree, repo, commit)
    except FileNotFoundError as e:
        return err(404, "not_found", f"Couldn't read {repo} at {commit[:8]}", {"detail": str(e)})
    pre = f"{path}/" if path else ""
    entries = [{"name": e["path"][len(pre):], "type": "dir" if e["type"] == "tree" else "file", "size": e.get("size")}
               for e in tree if e["path"].startswith(pre) and "/" not in e["path"][len(pre):] and e["path"] != path]
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return JSONResponse({"commit": commit, "path": path, "entries": entries})


@guarded
async def api_watch(req):
    return JSONResponse({"runs": lane_watch.STATE})


async def api_events(req):
    if not authed(req):
        return err(401, "unauthenticated", "Open the link johto web printed.")
    m = mode(req)

    async def initial():
        return await asyncio.to_thread(state, req)
    gen = events.stream(m, req.headers.get("last-event-id"), initial, req.is_disconnected)
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# --- local writes (files only; Studio writes live in api_actions) -------------------------------------------

def _rev_of(short: str, key: str) -> str:
    sub = workspace.parse_submission(_task_dir(short))
    return data.workspace_rev(sub["sections"].get(data.SECTION_KEYS[key], ""))


def _section_locked(short: str, key: str) -> str | None:
    if key != "opening_prompt":
        return None
    return data._prompt_lock(data.task_base(_task_dir(short)))


_WRITTEN: dict[tuple[str, str], list[str]] = {}   # revs this server wrote, so its own saves never conflict


def _log_authorship(d: Path, key: str, a) -> None:
    """Design 8.7: characters typed, pasted and deleted per save (the page sends the counts since its last
    save), in tasks/<short>/app/authorship.jsonl. It never leaves the machine."""
    if not isinstance(a, dict):
        return
    counts = {k: int(a.get(k) or 0) for k in ("typed", "pasted", "deleted", "focus_ms") if isinstance(a.get(k), (int, float))}
    if not any(counts.values()):
        return
    (d / "app").mkdir(exist_ok=True)
    with open(d / "app" / "authorship.jsonl", "a") as fh:
        fh.write(json.dumps({"at": data.now_iso(), "field": key, "event": "save", **counts}) + "\n")


@guarded
async def api_section(req):
    short, key = req.path_params["short"], req.path_params["key"]
    if key not in data.SECTION_KEYS:
        raise KeyError(key)
    body = await req.json()
    text = body.get("text", "")
    if mode(req) == "demo":
        t = _demo["tasks"][short]
        texts = {k: s["text"] for k, s in t["sections"].items()}
        texts[key] = text
        old = t["sections"][key]
        t["sections"][key] = data.section(key, text, t["summary"]["slot"], others=texts,
                                          locked=(old.get("locked") or {}).get("reason"))
        return JSONResponse({"section": t["sections"][key], "checklist": t.get("checklist", [])})
    _task_dir(short)
    reason = await asyncio.to_thread(_section_locked, short, key)
    if reason:
        return err(423, "locked", f"The opening prompt is locked: {reason}. Unlock it first; editing means a new run.",
                   {"reason": reason})
    base = body.get("base_rev")
    cur = await asyncio.to_thread(_rev_of, short, key)
    mine = _WRITTEN.get((short, key), [])
    if base not in (None, "", "0") and base != cur and cur not in mine and data.workspace_rev(text) != cur:
        sub = await asyncio.to_thread(workspace.parse_submission, config.TASKS / short)
        return err(409, "conflict_rev", "This field changed outside the app (vim or the CLI).",
                   {"text": sub["sections"].get(data.SECTION_KEYS[key], ""), "rev": cur})
    await asyncio.to_thread(data.write_section, short, key, text)
    await asyncio.to_thread(_log_authorship, config.TASKS / short, key, body.get("authorship"))
    _WRITTEN[(short, key)] = (mine + [data.workspace_rev(text)])[-8:]
    events.poke("section")
    t = await asyncio.to_thread(data.real_task, config.TASKS / short)
    return JSONResponse({"section": t["sections"][key], "checklist": t["checklist"]})


@guarded
async def api_ratings(req):
    short = req.path_params["short"]
    body = await req.json()
    r = {k: body.get(k) for k in ("one_shot", "task_success", "steering")}
    if mode(req) == "demo":
        t = _demo["tasks"][short]
        t["ratings"] = data.ratings(r, t["summary"]["slot"])
        return JSONResponse({"ratings": t["ratings"]})
    _task_dir(short)
    await asyncio.to_thread(data.write_ratings, short, r)
    events.poke("ratings")
    t = await asyncio.to_thread(data.real_task, config.TASKS / short)
    return JSONResponse({"ratings": t["ratings"]})


def _update_meta(d: Path, fn) -> dict:
    a = data._mod("actions")
    if a is not None and hasattr(a, "update_meta"):
        return a.update_meta(d, fn)
    meta = workspace.load(d)
    fn(meta)
    workspace.save(d, meta)
    return meta


def _nonce(route: str, short: str, body: dict) -> str | None:
    """Two-step confirm (contract 5.1): None means confirmed; else the nonce to send back."""
    c = body.get("confirm")
    now = time.time()
    for k in [k for k, v in NONCES.items() if v[2] < now]:
        NONCES.pop(k, None)
    if isinstance(c, str) and c in NONCES and NONCES[c][:2] == (route, short):
        NONCES.pop(c, None)
        return None
    n = secrets.token_urlsafe(12)
    NONCES[n] = (route, short, now + 4)
    return n


@guarded
async def api_unlock(req):
    """Unlock the opening prompt after a run used it. Local only; the next Done queues a new run."""
    short, key = req.path_params["short"], req.path_params["key"]
    if mode(req) == "demo":
        return _not_wired(req)
    if key != "opening_prompt":
        return err(400, "invalid", "Only the opening prompt locks.")
    d = _task_dir(short)
    body = await req.json() if await req.body() else {}
    if body.get("confirm") is not True:
        n = _nonce("unlock", short, body)
        if n:
            return err(409, "needs_confirm", "Press again: editing the prompt means a new run.",
                       {"nonce": n, "expires_in": 4, "summary": "Unlock the opening prompt"})
    await asyncio.to_thread(_update_meta, d, lambda m: m.update(prompt_unlocked_at=data.now_iso()))
    events.poke("unlock")
    t = await asyncio.to_thread(data.real_task, d)
    return JSONResponse({"section": t["sections"]["opening_prompt"]})


def _done(short: str, surface: str, confirm, kind: str | None = None) -> tuple[int, dict]:
    """Done on a writing surface (contract 5.2). Local only: it records that a step is finished and, for the
    prompt and the details, queues the next run for the lane ("ready"). It never touches Studio or text."""
    d = _task_dir(short)
    t = data.real_task(d)
    sections, slot = t["sections"], t["summary"]["slot"]
    now = data.now_iso()
    if surface == "prompt":
        keys = ["opening_prompt"]
    elif surface == "details":
        keys = ["goal", "decision_points", "codebase"] + (["initial_state"] if slot.get("action") == "Operate" else [])
    elif surface == "rate":
        keys = ["validation_notes"]
    else:
        return 400, {"error": {"code": "invalid", "message": "surface must be prompt, details or rate.", "details": {}}}
    missing = [k for k in keys if not (sections.get(k) or {}).get("text", "").strip()]
    if surface == "rate":
        r = t["ratings"]
        missing = [k for k in ("one_shot", "task_success", "steering") if not r.get(k)] + missing
    if missing:
        names = {**data.SECTION_KEYS, "one_shot": "One-shot", "task_success": "Task Success", "steering": "Steering"}
        return 422, {"error": {"code": "missing_fields", "message": "Fill these first: " + ", ".join(
            names.get(k, k) for k in missing), "details": {"fields": missing}}}
    if surface == "rate":
        r = t["ratings"]
        if r.get("one_shot") != "No" or not r.get("passes"):
            rows = [c for c in t["checklist"] if c.get("key") == "ratings"]
            msg = "One-shot is Yes: drop the task instead." if r.get("one_shot") == "Yes" else \
                next((c["message"] for c in r.get("check") or [] if c.get("level") == "error"), "These ratings are rejected.")
            return 422, {"error": {"code": "checklist_failed", "message": msg, "details": {"rows": rows}}}
    errors = [f for k in keys for f in (sections.get(k) or {}).get("lint") or [] if f.get("level") == "error"]
    if errors and surface != "rate":
        n = _nonce(f"done:{surface}", short, {"confirm": confirm})
        if n:
            return 409, {"error": {"code": "needs_confirm",
                                   "message": f"{len(errors)} lint error{'s' if len(errors) != 1 else ''} · press again to go on anyway",
                                   "details": {"nonce": n, "expires_in": 4, "summary": errors[0]["message"],
                                               "rows": errors}}}
    texts = {k: (sections.get(k) or {}).get("text", "") for k in keys}

    def put(m):
        if surface == "prompt" and (kind == "calib" or (kind is None and data.settings().get("calibrate_first"))):
            m["ready"] = {"kind": "calib", "at": now, "prompt_sha": data.workspace_rev(texts["opening_prompt"])}
        elif surface == "prompt":
            # No calibration: the goal comes next, and its Done queues the validation run.
            m["skipped_calibration"], m["skipped_calibration_at"] = True, now
        elif surface == "details":
            m["details_ready"] = {"at": now, "sha": {k: data.workspace_rev(v) for k, v in texts.items()}}
            sub_run = m.get("submission_run") or ""
            if not sub_run.startswith("calib-"):
                m["ready"] = {"kind": "validate", "at": now}
        else:
            m["rated"] = {"at": now, "rev": t["ratings"].get("rev")}
    _update_meta(d, put)
    try:
        from .lane import service
        service.svc.poke()
    except Exception:
        pass
    events.poke("done")
    snap = data.real_snapshot()
    summ = next((s for s in snap["summaries"] if s["short"] == short), t["summary"])
    return 200, {"summary": summ, "lane": snap["lane"]}


@guarded
async def api_done(req):
    short = req.path_params["short"]
    if mode(req) == "demo":
        return _not_wired(req)
    body = await req.json() if await req.body() else {}
    status, out = await asyncio.to_thread(_done, short, body.get("surface"), body.get("confirm"), body.get("kind"))
    return JSONResponse(out, status_code=status)


@guarded
async def api_ready_delete(req):
    """Take a queued run back off the lane queue."""
    short = req.path_params["short"]
    if mode(req) == "demo":
        return _not_wired(req)
    await asyncio.to_thread(_update_meta, _task_dir(short), lambda m: m.pop("ready", None))
    events.poke("ready")
    return Response(status_code=204)


@guarded
async def api_label(req):
    short = req.path_params["short"]
    if mode(req) == "demo":
        body = await req.json()
        _demo["tasks"][short]["summary"]["label"] = (body.get("label") or "").strip() or _demo["tasks"][short]["summary"]["label"]
        return JSONResponse({"summary": _demo["tasks"][short]["summary"]})
    d = _task_dir(short)
    label = ((await req.json()).get("label") or "").strip()[:120]

    def put(m):
        if label:
            m["label"] = label
        else:
            m.pop("label", None)
    await asyncio.to_thread(_update_meta, d, put)
    events.poke("label")
    t = await asyncio.to_thread(data.real_task, d)
    return JSONResponse({"summary": t["summary"]})


def _prefs_write(fn) -> None:
    p = config.DATA / "web-item-prefs.json"
    try:
        v = json.loads(p.read_text())
    except (OSError, ValueError):
        v = {}
    v.setdefault("hidden", {})
    v.setdefault("held", {})
    fn(v)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(v, indent=1))
    tmp.replace(p)


@guarded
async def api_item_hide(req):
    iid = req.path_params["id"]
    if mode(req) == "demo":
        return Response(status_code=204)
    if req.method == "DELETE":
        await asyncio.to_thread(_prefs_write, lambda v: v["hidden"].pop(iid, None))
    else:
        await asyncio.to_thread(_prefs_write, lambda v: v["hidden"].__setitem__(iid, data.now_iso()))
    events.poke("hide")
    return Response(status_code=204)


@guarded
async def api_item_hold(req):
    iid = req.path_params["id"]
    body = await req.json() if await req.body() else {}
    minutes = float(body.get("minutes") or 10)
    if mode(req) != "demo":
        await asyncio.to_thread(_prefs_write, lambda v: v["held"].__setitem__(iid, time.time() + minutes * 60))
    return Response(status_code=204)


@guarded
async def api_presence(req):
    body = await req.json() if await req.body() else {}
    PRESENCE.update({k: body.get(k) for k in ("visible", "focused", "drawer_open", "terminal_focused", "surface_item")},
                    at=time.time())
    return Response(status_code=204)


@guarded
async def api_not_wired(req):
    return _not_wired(req)


DEMO_OVERRIDES: dict = {}


# --- sockets ---------------------------------------------------------------------

async def _ws_demo_lane(ws: WebSocket):
    await ws.accept()
    if not (secrets.compare_digest(ws.cookies.get("johto", ""), TOKEN) or local_ok(ws.headers, ws=True)):
        await ws.close(code=4401)
        return
    if ws.cookies.get("johto_mode") == "demo" and ws.cookies.get("johto_lane", "working") in ("working", "waiting"):
        await ws.send_text(json.dumps({"type": "hello", "run": {"short": "d3m0aab3", "run": "validate-1"}, "locked": False}))
        lines = list(demo.TERMINAL)
        if ws.cookies.get("johto_lane", "working") == "working":
            lines = lines[:-6] + ["", "\x1b[38;5;208m✻\x1b[0m Writing handler for lock and unlock… \x1b[2m(esc to interrupt)\x1b[0m"]
        await ws.send_bytes("\r\n".join(lines).encode())
    else:
        await ws.send_text(json.dumps({"type": "idle"}))
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            b = msg.get("bytes")
            if b and ws.cookies.get("johto_mode") == "demo":  # echo typing so the demo terminal feels alive
                await ws.send_bytes(b.replace(b"\r", b"\r\n"))
    except Exception:
        pass


ws_lane_demo = _ws_demo_lane   # api_lane hands demo sockets back here


def _ws_dispatch(real_app):
    """Demo sockets get the demo terminal; real ones go to the lane module's socket when it exists."""
    demo_app = websocket_session(_ws_demo_lane)

    async def app(scope, receive, send):
        cookies = {}
        for k, v in scope.get("headers") or []:
            if k == b"cookie":
                c = SimpleCookie()
                try:
                    c.load(v.decode("latin-1"))
                except Exception:
                    pass
                cookies.update({m.key: m.value for m in c.values()})
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers") or []}
        if not origin_ok(headers):
            await send({"type": "websocket.close", "code": 4403})
            return
        if cookies.get("johto_mode") == "demo" or real_app is None:
            await demo_app(scope, receive, send)
        else:
            await real_app(scope, receive, send)
    return _ASGI(app)


# --- app -------------------------------------------------------------------------------

async def _call(fn, *a):
    r = fn(*a)
    if inspect.isawaitable(r):
        await r


def _hook(m, *names):
    for n in names:
        fn = getattr(m, n, None)
        if callable(fn):
            return fn
    return None


def create_app() -> Starlette:
    ext_http, ext_ws = _ext_routes()
    routes = [
        Route("/", index), Route("/pick", index), Route("/scout", index), Route("/finished", index),
        Route("/task/{short}", index), Route("/repos", index), Route("/repos/{id}", index),
        Route("/api/state", api_state), Route("/api/events", api_events),
        Route("/api/tasks", api_tasks),
        Route("/api/tasks/{short}", api_task),
        Route("/api/tasks/{short}/runs/{run}", api_run),
        Route("/api/tasks/{short}/runs/{run}/transcript", api_transcript),
        Route("/api/tasks/{short}/runs/{run}/diff", api_diff),
        Route("/api/tasks/{short}/sections/{key}", api_section, methods=["PUT"]),
        Route("/api/tasks/{short}/ratings", api_ratings, methods=["PUT"]),
        Route("/api/leads", api_leads), Route("/api/slots", api_slots), Route("/api/repos", api_repos),
        Route("/api/finished", api_finished),
        Route("/api/code/{target}/file", api_code_file),
        Route("/api/code/{target}/tree", api_code_tree),
        Route("/api/watch", api_watch),
        Route("/api/presence", api_presence, methods=["POST"]),
        *[Route(r.path, guarded(r.endpoint), methods=list(r.methods - {"HEAD"})) for r in api_dossier.routes],
        *[Route(r.path, guarded(r.endpoint), methods=list(r.methods - {"HEAD"})) for r in api_pack.routes],
        *ext_http,
        # Local fallbacks the builder modules may also provide (theirs above win).
        Route("/api/tasks/{short}/sections/{key}/unlock", api_unlock, methods=["POST"]),
        Route("/api/tasks/{short}/done", api_done, methods=["POST"]),
        Route("/api/tasks/{short}/ready", api_ready_delete, methods=["DELETE"]),
        Route("/api/tasks/{short}/label", api_label, methods=["PUT"]),
        Route("/api/items/{id}/hide", api_item_hide, methods=["POST", "DELETE"]),
        Route("/api/items/{id}/hold", api_item_hold, methods=["POST"]),
        Route("/api/{rest:path}", api_not_wired, methods=["POST", "PUT", "DELETE"]),
        WebSocketRoute("/ws/lane", _ws_dispatch(ext_ws.get("/ws/lane"))),
        *[WebSocketRoute(p, _ws_dispatch(a)) for p, a in ext_ws.items() if p != "/ws/lane"],
        Mount("/static", _Static(directory=STATIC), name="static"),
    ]

    @asynccontextmanager
    async def lifespan(app):
        started = []
        # Events from the builder modules reach the browser through the SSE hub.
        for name in EXTENSIONS:
            m = _ext(name)
            if m is None:
                continue
            pub = _hook(m, "set_publisher")
            if pub:
                pub(events.from_jobs)
            if name == "api_lane" and config.NO_WATCH:
                continue  # its monitor watches (and may prepare) lanes; tests run without it
            fn = _hook(m, "start", "startup")
            if fn:
                try:
                    await _call(fn)
                    started.append(m)
                except Exception as e:
                    print(f"johto web: {name} did not start: {e}", flush=True)
        try:
            from .lane import service
            service.svc.listen(lambda name, d: events.poke(name) if name in ("lane", "job") else None)
        except Exception:
            pass
        if not config.NO_WORKER:
            try:
                api_dossier.start_worker()
            except Exception as e:  # the UI must come up even if the worker can't
                print(f"dossier worker did not start: {e}", flush=True)
        if not config.NO_WATCH:
            lane_watch.start()
        events.start(watch=True)
        try:
            yield
        finally:
            events.stop()
            if not config.NO_WATCH:
                lane_watch.stop()
            for m in reversed(started):
                fn = _hook(m, "stop", "shutdown")
                if fn:
                    try:
                        await _call(fn)
                    except Exception as e:
                        print(f"johto web: {m.__name__} did not stop cleanly: {e}", flush=True)
            if not config.NO_WORKER:
                try:
                    api_dossier.stop_worker()
                except Exception as e:
                    print(f"dossier worker did not stop cleanly: {e}", flush=True)

    return Starlette(routes=routes, lifespan=lifespan)


def run(port: int | None = None, open_browser: bool = True) -> None:
    """Bind first, then publish the URL, so a second launch can't overwrite the live server's token."""
    import socket
    import uvicorn
    global PORT
    PORT = port or config.PORT
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if config.FAKE and config.ROOT == config.CHECKOUT:
        # A fake server over the real checkout would serve his real tasks with local writes on.
        raise SystemExit("johto web: fake mode needs JOHTO_ROOT pointing at a scratch copy, not this checkout.")
    try:
        sock.bind(("127.0.0.1", PORT))
    except OSError:
        print(f"johto web is already running on port {PORT}: http://127.0.0.1:{PORT}/", flush=True)
        if open_browser and not config.FAKE:
            subprocess.Popen(["open", f"http://127.0.0.1:{PORT}/"])
        return
    url = f"http://127.0.0.1:{PORT}/?t={TOKEN}"
    print(f"johto web: http://127.0.0.1:{PORT}/", flush=True)
    # The plain URL: this machine's browser needs no token, and copies of data/ shouldn't carry one.
    config.DATA.mkdir(parents=True, exist_ok=True)
    (config.DATA / "web-url.txt").write_text(f"http://127.0.0.1:{PORT}/")
    if open_browser and not config.FAKE:
        subprocess.Popen(["open", url])
    # SSE streams never end on their own; give them 3 s on shutdown instead of waiting forever.
    server = uvicorn.Server(uvicorn.Config(create_app(), log_level="warning", timeout_graceful_shutdown=3))
    server.run(sockets=[sock])
