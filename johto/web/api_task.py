"""Task workspace API: the state card, the grading table, the writing kit and the draft check.

The task page's State, Grading, Kit and Check views each read one of these routes, and each route calls
the module its CLI twin uses: johto.where (`johto where`), johto.grading (`johto grading`), johto.kit
(`johto kit`) and johto.draftcheck (`johto check`). Home's task list reads GET /api/overview.

Those modules are imported when a request needs them, so a module that is missing or mid-edit answers
503 "not available yet" for its own routes and never keeps the app from starting.

Nothing here writes text into Austin's fields or hands back wording for them. Grading rows and check
results pass through allowlists of the shared contract's keys, so a key such as "suggestion" or
"replacement" never reaches the page even if a module adds one. The only writes are Austin's own marks
(scope, selected, goal, decision point, notes), a refresh of the table from saved reviews, and checks.
A deep check runs as a background job (johto.web.jobs, pool "ai"); the page waits for the job.

app.py mounts `routes` ahead of api_actions (EXTENSIONS order), behind the session cookie, the Origin
check and the demo guard. GET /api/tasks/{short}/check therefore answers with the draft check here.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .. import config

SHORT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ROW_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
RUN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SCOPES = ("explicit", "necessary", "uncertain", "outside")
MARKS = ("selected", "goal", "dp", "notes", "scope")

# What each lazily loaded module is, in the words a "not available yet" answer uses.
WHAT = {"where": "The task card", "grading": "The grading table", "kit": "The writing kit",
        "draftcheck": "The draft check"}

# The shared contract's keys. Anything else a module returns is dropped before it reaches the page.
ROW_KEYS = ("id", "finding", "kind", "sources", "suggested_scope", "scope", "result", "basis", "refs", "limits",
            "review_status", "severity", "marks", "marked_at", "verify",
            # grading.py's extras the views use: a row from another run, a marked row whose facts changed since
            # the mark, the row that replaced a superseded one, and when its facts last changed
            "other_run", "changed_since_mark", "superseded_by", "changed_at")
SOURCE_KEYS = ("type", "ref", "finding_id", "at")
RESULT_KEYS = ("run", "turn", "value")
MARK_KEYS = ("selected", "goal", "dp", "notes")
VERIFY_KEYS = ("status", "at", "path", "summary", "executed")
GRADING_KEYS = ("version", "run", "updated_at", "coverage")
CHECK_KEYS = ("at", "fields_sha", "deep", "path", "run", "studio_at")
CHECK_LISTS = ("attention", "resolved", "decide", "optional", "stale")   # stale: earlier AI issues on changed fields
CHECK_AI_KEYS = ("called", "at", "reused_from", "focus", "dropped_total", "cost_usd", "error", "nothing_material")
ISSUE_KEYS = ("id", "field", "quote", "category", "evidence", "consequence", "source")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}


def err(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "details": details or {}}}, status_code=status)


def handled(fn):
    """Plain-words JSON errors for every way a handler can fail."""
    async def wrap(req: Request):
        try:
            return await fn(req)
        except ApiError as e:
            return err(e.status, e.code, e.message, e.details)
        except Exception as e:  # noqa: BLE001  (a module's failure is shown, never a stack page)
            return err(500, "failed", f"{e.__class__.__name__}: {e}" if str(e) else e.__class__.__name__)
    wrap.__name__ = fn.__name__
    return wrap


# --- plumbing ------------------------------------------------------------------------------------------

def _mod(name: str, *fns: str):
    """johto.<name>, imported now. Missing (or missing one of `fns`) raises the 503 "not available yet"."""
    full = f"johto.{name}"
    what = WHAT.get(name, name)
    try:
        m = importlib.import_module(full)
    except ModuleNotFoundError as e:
        if e.name in (full, None):
            raise ApiError(503, "not_available", f"{what} isn't available yet.", {"module": f"johto/{name}.py"}) from None
        raise
    for fn in fns:
        if not callable(getattr(m, fn, None)):
            raise ApiError(503, "not_available", f"{what} isn't available yet.", {"module": f"johto/{name}.py", "missing": fn})
    return m


def _dir(req: Request) -> Path:
    short = req.path_params["short"]
    if req.cookies.get("johto_mode") == "demo":
        raise ApiError(404, "demo", "The demo data has no saved task record.")
    if not SHORT.match(short or ""):
        raise ApiError(400, "invalid", "Bad task name.")
    d = config.TASKS / short
    if not (d / "task.json").exists():
        raise ApiError(404, "not_found", f"No task folder for {short}.")
    return d


async def _body(req: Request) -> dict:
    raw = await req.body()
    if not raw:
        return {}
    try:
        b = json.loads(raw)
    except ValueError:
        raise ApiError(400, "invalid", "The request body isn't JSON.") from None
    if not isinstance(b, dict):
        raise ApiError(400, "invalid", "The request body must be a JSON object.")
    return b


def _run_param(v) -> str | None:
    if v in (None, ""):
        return None
    if not isinstance(v, str) or not RUN.match(v):
        raise ApiError(400, "invalid", "Bad run name.")
    return v


def _plain(v):
    """JSON-safe copy (paths and dates become strings)."""
    return json.loads(json.dumps(v, default=str))


def _pick(d, keys) -> dict:
    return {k: d[k] for k in keys if isinstance(d, dict) and k in d}


# --- allowlists ----------------------------------------------------------------------------------------

def clean_row(r: dict) -> dict:
    out = _pick(r, ROW_KEYS)
    if isinstance(out.get("sources"), list):
        out["sources"] = [_pick(s, SOURCE_KEYS) for s in out["sources"] if isinstance(s, dict)]
    if isinstance(out.get("result"), dict):
        out["result"] = _pick(out["result"], RESULT_KEYS)
    if isinstance(out.get("marks"), dict):
        out["marks"] = _pick(out["marks"], MARK_KEYS)
    if isinstance(out.get("verify"), dict):
        out["verify"] = _pick(out["verify"], VERIFY_KEYS)
    return out


def clean_grading(g: dict | None) -> dict:
    g = g if isinstance(g, dict) else {}
    out = {"version": 1, "run": None, "updated_at": None, **_pick(g, GRADING_KEYS)}
    out["rows"] = [clean_row(r) for r in (g.get("rows") or []) if isinstance(r, dict)]
    return out


def clean_issue(i: dict) -> dict:
    return _pick(i, ISSUE_KEYS)


def clean_check(c: dict | None) -> dict | None:
    if not isinstance(c, dict):
        return None
    out = _pick(c, CHECK_KEYS)
    for k in CHECK_LISTS:
        out[k] = [clean_issue(i) for i in (c.get(k) or []) if isinstance(i, dict)]
    if isinstance(c.get("sources"), dict):   # where each field's text came from: "studio", "local" or null
        out["sources"] = {str(k): v for k, v in c["sources"].items() if v is None or isinstance(v, str)}
    if isinstance(c.get("since"), dict):     # the check before this one, and which fields changed after it
        since = c["since"]
        out["since"] = {"at": since.get("at"), "changed": [str(x) for x in since.get("changed") or [] if isinstance(x, str)]}
    if isinstance(c.get("ai"), dict):
        out["ai"] = _pick(c["ai"], CHECK_AI_KEYS)
    return out


def _mark_args(b: dict) -> dict:
    """The marks a request sets. A key that is absent stays as it was; null clears dp or scope."""
    kw = {}
    for k in ("selected", "goal", "notes"):
        if k in b:
            if not isinstance(b[k], bool):
                raise ApiError(400, "invalid", f"{k} must be true or false.")
            kw[k] = b[k]
    if "dp" in b:
        if b["dp"] is not None and (isinstance(b["dp"], bool) or b["dp"] not in (1, 2, 3)):
            raise ApiError(400, "invalid", "dp must be 1, 2, 3 or null.")
        kw["dp"] = b["dp"]
    if "scope" in b:
        if b["scope"] is not None and b["scope"] not in SCOPES:
            raise ApiError(400, "invalid", "scope must be explicit, necessary, uncertain, outside or null.")
        kw["scope"] = b["scope"]
    if not kw:
        raise ApiError(400, "invalid", "Nothing to mark.")
    return kw


def _ids(b: dict) -> list[str]:
    ids = b.get("ids")
    if not isinstance(ids, list) or not ids or len(ids) > 500 or not all(isinstance(i, str) and ROW_ID.match(i) for i in ids):
        raise ApiError(400, "invalid", "ids must be a list of row ids.")
    return list(dict.fromkeys(ids))


# --- routes --------------------------------------------------------------------------------------------

@handled
async def overview(req: Request):
    if req.cookies.get("johto_mode") == "demo":
        return JSONResponse({"rows": []})
    where = _mod("where", "overview")
    rows = await asyncio.to_thread(where.overview)
    return JSONResponse({"rows": _plain(rows or [])})


@handled
async def card(req: Request):
    """The card. Only the page's first read of a task sends ?touch=1, so refetches don't count as visits (a
    touch resets "since your last visit" and is a focus sign)."""
    d = _dir(req)
    where = _mod("where", "card")
    touch = req.query_params.get("touch") == "1"
    return JSONResponse(_plain(await asyncio.to_thread(lambda: where.card(d, touch=touch, source="web"))))


@handled
async def grading_get(req: Request):
    d = _dir(req)
    g = _mod("grading", "load")
    return JSONResponse(clean_grading(await asyncio.to_thread(g.load, d)))


@handled
async def grading_refresh(req: Request):
    d = _dir(req)
    b = await _body(req)
    run = _run_param(b.get("run"))
    g = _mod("grading", "refresh")
    lines: list[str] = []
    data = await asyncio.to_thread(lambda: g.refresh(d, run=run, log=lines.append))
    # Saved reviews with no findings block: the CLI's `refresh --extract` reads them (one Claude call each).
    missing = [str(x) for x in ((data or {}).get("_missing") or []) if isinstance(x, (str, Path))]
    return JSONResponse({**clean_grading(data), "log": [str(x) for x in lines[-20:]], "missing": missing})


@handled
async def grading_mark(req: Request):
    d = _dir(req)
    b = await _body(req)
    ids, kw = _ids(b), _mark_args(b)
    g = _mod("grading", "mark")
    try:
        rows = await asyncio.to_thread(lambda: g.mark(d, ids, **kw))
    except KeyError as e:
        raise ApiError(404, "not_found", f"No grading row {e.args[0] if e.args else ''}".strip() + ".") from None
    except ValueError as e:   # grading.GradingError: "No row R9 ..." is an unknown row, the rest bad input
        if str(e).startswith("No row"):
            raise ApiError(404, "not_found", str(e)) from None
        raise ApiError(400, "invalid", str(e) or "Those marks aren't valid.") from None
    return JSONResponse({"rows": [clean_row(r) for r in (rows or []) if isinstance(r, dict)]})


@handled
async def kit_get(req: Request):
    d = _dir(req)
    run = _run_param(req.query_params.get("run"))
    k = _mod("kit", "render")
    text = await asyncio.to_thread(lambda: k.render(d, run=run))
    return JSONResponse({"markdown": str(text or ""), "run": run})


def _deep_blocker(d: Path) -> str | None:
    """Why the deep check won't run now (required fields empty), else None."""
    c = _mod("draftcheck", "run")
    fn = getattr(c, "deep_blocker", None)
    try:
        return fn(d) if fn else None
    except Exception:
        return None


@handled
async def check_get(req: Request):
    d = _dir(req)
    c = _mod("draftcheck", "latest")
    check = clean_check(await asyncio.to_thread(c.latest, d))
    return JSONResponse({"check": check, "deep_blocked": await asyncio.to_thread(_deep_blocker, d)})


_DEEP_LOCK = threading.Lock()   # the "already running?" look and the submit, as one step (a double click)


def _deep_job(short: str, d: Path, full: bool) -> dict:
    from . import jobs
    c = _mod("draftcheck", "run")

    def work(ctx):
        ctx.step("checking")
        return {"check": clean_check(c.run(d, deep=True, full=full, log=ctx.log))}
    with _DEEP_LOCK:
        live = jobs.active(kind="check_deep", target=f"t:{short}")
        if live:
            return live[0]
        return jobs.submit("check_deep", f"t:{short}", work, pool="ai", params={"deep": True, "full": full},
                           steps=["checking"], step="waiting")


def _studio_old(d: Path, max_age_s: float = 60) -> bool:
    """True when the Studio snapshot is older than max_age_s (or missing), so a check reads Studio first."""
    try:
        at = json.loads((d / "app" / "studio-fields.json").read_text()).get("at")
        when = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.astimezone()
        return (datetime.now(timezone.utc) - when).total_seconds() > max_age_s
    except (OSError, ValueError, TypeError, AttributeError):
        return False


@handled
async def check_post(req: Request):
    d = _dir(req)
    b = await _body(req)
    deep, full = bool(b.get("deep")), bool(b.get("full"))
    if deep:
        _mod("draftcheck", "run")
        why = await asyncio.to_thread(_deep_blocker, d)
        if why:
            raise ApiError(422, "missing_fields", why)
        return JSONResponse({"job": await asyncio.to_thread(_deep_job, d.name, d, full)}, status_code=202)
    c = _mod("draftcheck", "run")
    lines: list[str] = []
    # Check again reads Studio first when its snapshot is over a minute old (the app syncs every 60 s).
    refresh = await asyncio.to_thread(_studio_old, d)
    kw = {"refresh": True} if refresh else {}
    res = await asyncio.to_thread(lambda: c.run(d, deep=False, full=full, log=lines.append, **kw))
    return JSONResponse({"check": clean_check(res), "log": [str(x) for x in lines[-20:]]})


@handled
async def review_get(req: Request):
    """One saved review (the web twin of `johto review show`): its digest first, then the full text. Research
    only: it is GPT's reading of the run, shown as saved; nothing here writes or rewords a field."""
    d = _dir(req)
    name = req.path_params["name"]
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name or "") or name.startswith("."):
        raise ApiError(400, "invalid", "Bad review name.")
    rv = _mod("reviews", "read")
    out = await asyncio.to_thread(rv.read, d, name)
    if out is None:
        raise ApiError(404, "not_found", f"No saved review {name} for {d.name}.")
    return JSONResponse(_plain(out))


routes = [
    Route("/api/overview", overview),
    Route("/api/tasks/{short}/card", card),
    Route("/api/tasks/{short}/grading", grading_get),
    Route("/api/tasks/{short}/grading/refresh", grading_refresh, methods=["POST"]),
    Route("/api/tasks/{short}/grading/mark", grading_mark, methods=["POST"]),
    Route("/api/tasks/{short}/kit", kit_get),
    Route("/api/tasks/{short}/check", check_get),
    Route("/api/tasks/{short}/check", check_post, methods=["POST"]),
    Route("/api/tasks/{short}/reviews/{name}", review_get),
]
