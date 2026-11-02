"""Task actions over HTTP (build-plan 5.2): claim, decisions with undo, the submitted run, collect,
the checklist, Studio save / AutoQC / submit / unclaim / refresh, discard, lane cleanup, undo, jobs.

app.py mounts `routes` and calls start() and stop() from its lifespan; set_publisher(hub.publish)
connects job, toast, task and studio events to the SSE hub. Every route checks the session cookie,
and every non-GET also checks Origin and Host (build-plan 0.7).

Studio writes happen only on Austin's request to these routes. Submit and unclaim need a second
request carrying the nonce from the first one's 409 needs_confirm (4 s). Drop (the decision route, from
the page's d key) waits 10 s so ⌘Z can cancel it; there is no one-POST discard route for scripts. A claim
can be undone for 30 s. Nothing here writes text into Austin's fields.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .. import actions, config, studio, workspace
from ..actions import ActionError
from . import jobs

UNDO_DROP_S = 10
UNDO_CLAIM_S = 30
NONCE_S = 4
QC_POLL_S = float(os.environ.get("JOHTO_QC_POLL_S", "15"))
QC_TIMEOUT_S = 900
CODEBASE_CHECK_DELAY_S = UNDO_CLAIM_S + 1
SHORT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# --- plumbing -----------------------------------------------------------------------------------------

_auth = None


def set_auth(fn) -> None:
    """fn(request) -> bool. Default: johto.web.app.authed (the session cookie)."""
    global _auth
    _auth = fn


def _authed(req: Request) -> bool:
    fn = _auth
    if fn is None:
        from .app import authed as fn  # late: app imports this module
    return fn(req)


def _origin_ok(req: Request) -> bool:
    host = req.headers.get("host", "")
    name, _, port = host.rpartition(":")
    if name not in ("127.0.0.1", "localhost") or not port.isdigit():
        return False
    return req.headers.get("origin") in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")


def err(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "details": details or {}}}, status_code=status)


def guarded(fn):
    async def wrap(req: Request):
        if not _authed(req):
            return err(401, "unauthenticated", "Open the link johto web printed.")
        if req.method not in ("GET", "HEAD") and not _origin_ok(req):
            return err(403, "forbidden", "This request didn't come from the app's own page.")
        try:
            return await fn(req)
        except ActionError as e:
            return err(e.status, e.code, e.message, e.details)
        except studio.StudioError as e:
            d = actions.studio_error_dict(e)
            return err(502, d["code"], d["message"], d["details"])
    wrap.__name__ = fn.__name__
    return wrap


async def _body(req: Request) -> dict:
    raw = await req.body()
    if not raw:
        return {}
    try:
        b = json.loads(raw)
    except ValueError:
        raise ActionError("invalid", "The request body isn't JSON.") from None
    if not isinstance(b, dict):
        raise ActionError("invalid", "The request body must be a JSON object.")
    return b


def _dir(short: str) -> Path:
    if not SHORT.match(short or ""):
        raise ActionError("invalid", "Bad task name.")
    d = config.TASKS / short
    if not (d / "task.json").exists():
        raise ActionError("not_found", f"No task folder for {short}.")
    return d


def _label(d: Path) -> str:
    try:
        meta = workspace.load(d)
    except (OSError, ValueError):
        return d.name
    if meta.get("label"):
        return meta["label"]
    try:   # the same name the rail and the task page use (lead title, matched seed), never "Task t4sk0002"
        from . import data
        label = data._label(meta, data._lead_by_id(meta.get("lead_id")), d)
        head, _, problem = label.partition(" · ")
        if problem and not re.fullmatch(r"Task [a-z0-9]{6,}", problem, re.I):
            return label
    except Exception:  # noqa: BLE001
        pass
    repo = (meta.get("repo") or {}).get("full_name", "").split("/")[-1]
    action = ((meta.get("slot") or {}).get("action") or "").split("/")[0].strip()
    return f"{repo} · {action} task" if repo and action else repo or "the task"


def _summary(d: Path) -> dict | None:
    try:
        from . import data
        return data.real_task(d)["summary"]
    except Exception:  # the read model belongs to another module; events still go out without it
        return None


def task_changed(short: str, changed: list[str]) -> None:
    """A `task` event. The SSE engine recomputes the summary itself, so this carries only what changed."""
    jobs.publish("task", {"short": short, "changed": changed})


def _accepted(job: dict, **extra) -> JSONResponse:
    return JSONResponse({"job": job, **extra}, status_code=202)


def _existing(kind: str, short: str) -> dict | None:
    live = jobs.active(kind=kind, target=f"t:{short}")
    return live[0] if live else None


async def _thread(fn, *a, **kw):
    return await asyncio.to_thread(fn, *a, **kw)


# --- two-press nonces ---------------------------------------------------------------------------------

_NONCES: dict[str, tuple[str, str, float]] = {}


def _confirmed(route: str, short: str, body: dict, summary: str, rows: list | None = None) -> bool:
    """True when body.confirm is this route's live nonce. Otherwise raises needs_confirm with a new one."""
    c = body.get("confirm")
    now = time.time()
    for k in [k for k, v in _NONCES.items() if v[2] < now - 60]:
        _NONCES.pop(k, None)
    if isinstance(c, str) and c in _NONCES:
        r, s, exp = _NONCES.pop(c)
        if r == route and s == short and now <= exp + 1:   # one second of grace for the round trip
            return True
        if r == route and s == short:
            summary = "That second press came too late. " + summary
    nonce = secrets.token_urlsafe(12)
    _NONCES[nonce] = (route, short, now + NONCE_S)
    details = {"nonce": nonce, "expires_in": NONCE_S, "summary": summary}
    if rows is not None:
        details["rows"] = rows
    raise ActionError("needs_confirm", summary, details)


# --- job bodies ---------------------------------------------------------------------------------------

def _open_url(url: str) -> None:
    if not config.FAKE_STUDIO and os.environ.get("JOHTO_NO_OPEN") != "1":
        subprocess.Popen(["open", url])


def _qc_wait_job(short: str, audit_id: str, spec: str, parent: str | None) -> dict:
    d = config.TASKS / short

    def fn(ctx: jobs.JobCtx):
        ctx.step("polling", ["polling"])
        t0 = time.time()

        def sleep(s):
            ctx.log(f"{spec} running {int(time.time() - t0)} s")
            ctx.sleep(s)
        try:
            res = actions.poll_qc(d, audit_id, every=QC_POLL_S, timeout=QC_TIMEOUT_S, sleep=sleep)
        except jobs.Cancelled:
            raise
        except Exception as e:   # the poll died (signed out too long, Studio error): the row offers Rerun
            msg = actions.studio_error_dict(e)["message"] if isinstance(e, studio.StudioError) else getattr(e, "message", str(e))
            actions.fail_qc(d, audit_id, f"Checking on it failed: {msg}")
            task_changed(short, ["qc"])
            raise
        res.pop("audit", None)
        if res["status"] == "running":
            actions.fail_qc(d, audit_id, "No result after 15 minutes. Rerun it.")
        c = res["counts"]
        what = "Mercor's automatic checks" if spec == "AutoQC" else f"Mercor's {spec[:1].lower() + spec[1:]}"
        if res["status"] == "completed":
            if c["fail"]:   # a passing check stays silent; only what needs him toasts
                n = c["fail"]
                jobs.toast(f"{what} found {'a problem' if n == 1 else f'{n} problems'} in {_label(d)}.", level="warn")
        elif res["status"] == "running":
            jobs.toast(f"{what} for {_label(d)} are still running after 15 minutes. Run them again later.", level="warn")
        else:
            jobs.toast(f"{what} for {_label(d)} stopped. {res.get('failure_reason') or ''}".strip(), level="error")
        task_changed(short, ["qc"])
        return {"audit_id": audit_id, "spec": spec, **res}

    return jobs.submit("qc_wait", f"t:{short}", fn, pool="studio", priority=1, parent=parent,
                       params={"audit_id": audit_id, "spec": spec}, steps=["polling"])


def _teardown_after(d: Path, ctx: jobs.JobCtx, run: str | None = None) -> dict:
    try:
        res = actions.clean(d, run)
    except ActionError as e:
        if e.code != "lane_state":
            raise
        res = {"removed": [], "skipped": [{"run": run, "reason": e.message}]}
    for s in res["skipped"]:
        ctx.log(f"kept {s['run']}: {s['reason']}")
    return res


# --- claim --------------------------------------------------------------------------------------------

@guarded
async def claim(req: Request):
    """Claim one open slot in Studio (his press). Optional `origin` (spec 7.2): {"kind": "seed", "opportunity_id"}
    or {"kind": "place", "place_id", "name"}, stored in task.json with the dossier id and naming the task's label.
    A claim from a dossier may name the slot group he chose (`slot`: action, domain, codebase type)."""
    b = await _body(req)
    lead = actions.load_lead(b["lead_id"]) if b.get("lead_id") else None
    origin = b.get("origin") if isinstance(b.get("origin"), dict) else None
    if origin is not None and not b.get("dossier_id"):
        raise ActionError("invalid", "A claim from a seed or a place needs its repo's research.")
    if origin is not None and origin.get("kind") == "seed":
        oid = origin.get("opportunity_id") or b.get("opportunity_id")
        if b.get("opportunity_id") and b["opportunity_id"] != oid:
            raise ActionError("invalid", "The seed and the problem it names don't match.")
        b["opportunity_id"] = oid
        origin = {**origin, "opportunity_id": oid}
    elif origin is not None and origin.get("kind") == "place" and b.get("opportunity_id"):
        raise ActionError("invalid", "A claim from a place has no research problem attached.")
    doc = opp = None
    if b.get("dossier_id"):
        doc, opp = await _thread(actions.load_opportunity, b["dossier_id"], b.get("opportunity_id"))
    stored_origin = None
    origin_label = None
    if origin is not None:
        from .api_discover import claim_label
        try:
            origin_label, stored_origin = await _thread(claim_label, doc, origin)
        except ValueError as e:
            raise ActionError("invalid", str(e)) from None
    chosen = b.get("slot") if isinstance(b.get("slot"), dict) else {}
    chosen = {k: chosen.get(k) for k in ("action", "domain", "codebase_type")}
    if lead:
        fit = lead.get("slot_fit") or {}
    elif doc is not None and all(chosen.values()):
        fit = chosen    # the slot group he picked in the claim panel (one of the seed's or place's fits)
    else:
        fit = ((opp or {}).get("slot_fit") or [{}])[0] or b.get("slot") or {}
    slot = {"action": fit.get("action"), "domain": fit.get("domain"), "codebase_type": fit.get("codebase_type"),
            "prompt_style": b.get("prompt_style"), "interaction_mode": b.get("interaction_mode")}
    task_id = b.get("task_id")
    if not task_id:
        if None in slot.values():
            raise ActionError("invalid", "Pick a slot variant (prompt style and mode) to claim.")
        open_ = actions.slot_tasks(slot)
        if not open_:
            raise ActionError("not_found", f"No open slot for {actions.slot_key(slot)}. Sync slots and try again.")
        task_id = open_[0]["task_id"]
    if lead:
        repo_args = (lead["repo"], lead.get("commit"), b.get("language") or lead.get("primary_language"))
    elif doc:
        repo_args = (doc["repo"], doc.get("commit"), b.get("language"))
    elif b.get("repo"):
        repo_args = (b["repo"], b.get("commit"), b.get("language"))
    else:
        raise ActionError("invalid", "Claim needs a lead, a dossier or a repo.")
    expect = b.get("expect_status") or "Ready to Claim"
    label = None
    if origin_label:
        label = origin_label
    elif not lead and opp:
        label = f"{repo_args[0].split('/')[-1]} · {opp.get('title')}"
    want_check = b.get("codebase_check", True) is not False

    def fn(ctx: jobs.JobCtx):
        ctx.step("repo", ["repo", *actions.CLAIM_STEPS])
        repo = actions.repo_info(*repo_args)
        warnings = actions.check_repo_allowed(repo)
        for w in warnings:
            ctx.log(w)
        d = actions.claim(task_id, repo, lead, on_step=ctx.step, dossier=doc, opportunity=opp,
                          expect_status=expect, label=label, origin=stored_origin)
        short = d.name
        meta = workspace.load(d)
        child = None
        if want_check:
            child = _codebase_check_later(short, ctx.job_id)
        try:   # the task understanding is researched while he reads the brief, ready for the Prompt step
            from .. import understand
            understand.ensure(d)
        except Exception as e:  # noqa: BLE001
            ctx.log(f"The task understanding didn't start: {e}")
        # A claim from a seed or a place marks the repo's research as started (a place with no problem id). The
        # undo puts it back when this claim was what started it.
        marked = False
        if stored_origin is not None and doc is not None:
            try:
                from ..dossier import worker
                worker.act(doc["id"], stored_origin.get("opportunity_id"), short)
                marked = True
            except Exception:   # already started from another seed, or a fixture dossier: nothing to mark
                marked = False

        def undo_fn():
            if marked:
                try:
                    from ..dossier import worker
                    worker.restore(doc["id"])
                except Exception:
                    pass
            return _undo_claim(short, child)

        # The task's label, not Studio's "Task <id>" (no ids on a first layer, spec rule 5).
        undo = jobs.register_undo("claim", short, UNDO_CLAIM_S, undo_fn, "Unclaim")
        jobs.toast(f"Claimed {_label(d)}.", level="ok", undo=undo)
        task_changed(short, ["studio", "sections"])
        jobs.publish("leads", {"changed": True})
        return {"short": short, "task_id": meta["task_id"], "task_name": meta["task_name"], "warnings": warnings,
                "undo": undo, "codebase_check_job": (child or {}).get("id")}

    job = jobs.submit("claim", f"l:{b.get('lead_id') or b.get('dossier_id') or repo_args[0]}", fn, pool="studio",
                      priority=0, params={"task_id": task_id}, steps=["repo", *actions.CLAIM_STEPS])
    return _accepted(job)


def _codebase_check_later(short: str, parent: str) -> dict:
    """Studio's Codebase Check, started once the claim's undo window has passed."""
    d = config.TASKS / short

    def fn(ctx: jobs.JobCtx):
        ctx.step("starting", ["starting"])
        try:
            aid = actions.start_qc(d, "Codebase Check")
        except ActionError as e:   # submitted, unclaimed or dropped inside the window: nothing to check
            if e.code in ("precondition_failed", "not_found"):
                return {"skipped": e.message}
            raise
        task_changed(short, ["qc"])
        if aid:
            _qc_wait_job(short, aid, "Codebase Check", ctx.job_id)
        return {"audit_id": aid}

    return jobs.submit("qc", f"t:{short}", fn, pool="studio", priority=2, parent=parent,
                       run_at=time.time() + CODEBASE_CHECK_DELAY_S, params={"spec": "Codebase Check"},
                       steps=["starting"])


def _undo_claim(short: str, child: dict | None) -> dict:
    if child:
        try:
            jobs.cancel(child["id"])
        except ActionError:
            pass
    d = config.TASKS / short
    label = _label(d)     # read now: the undo moves the folder out of tasks/

    def fn(ctx: jobs.JobCtx):
        ctx.step("unclaiming", ["unclaiming"])
        res = actions.undo_claim(d)
        jobs.publish("task.removed", {"short": short})
        jobs.publish("leads", {"changed": True})
        jobs.toast(f"Unclaimed {label}. The slot is back in the pool.", level="ok")
        return res

    job = jobs.submit("unclaim", f"t:{short}", fn, pool="studio", priority=0, cancellable=False,
                      steps=["unclaiming"])
    return {"undone": True, "job": job}


# --- decisions, drop, discard --------------------------------------------------------------------------

def _drop(d: Path, reason: str, run: str | None) -> tuple[dict, dict]:
    short = d.name
    label = _label(d)
    meta = workspace.load(d)

    def fn(ctx: jobs.JobCtx):
        res = actions.discard(d, reason=reason, unclaim_first=True, on_step=ctx.step, run=run)
        ctx.step("opening_form")
        _open_url(res["url"])
        task_changed(short, ["studio", "decisions", "runs"])
        jobs.publish("leads", {"changed": True})
        jobs.toast(f"Dropped {label}. Log it in the Discarded Run Log: you write the three short answers.",
                   level="ok", link={"label": "Discarded Run Log", "href": res["url"]})
        return res

    job = jobs.submit("drop", f"t:{short}", fn, pool="studio", priority=0, cancellable=False,
                      run_at=time.time() + UNDO_DROP_S, params={"run": run, "reason": reason},
                      steps=["waiting", *actions.DISCARD_STEPS, "opening_form"], step="waiting")

    def undo():
        j = jobs.get(job["id"])
        if j and j["state"] != "queued":
            raise ActionError("expired", "Too late: the drop has started.")
        jobs.cancel(job["id"])
        return {"undone": True}

    u = jobs.register_undo("drop", short, UNDO_DROP_S, undo,
                           f"Dropping: unclaims {_label(config.TASKS / short)} in Studio and removes its lanes")
    return job, u


@guarded
async def decision(req: Request):
    short = req.path_params["short"]
    d = _dir(short)
    b = await _body(req)
    run, dec = b.get("run"), b.get("decision")
    actions.check_run_name(run)
    if dec not in ("keep", "rework", "drop"):
        raise ActionError("invalid", "decision must be keep, rework or drop.")
    if not run or not (d / "runs" / str(run)).is_dir():
        raise ActionError("not_found", f"No run {run} in {short}.")
    if dec == "drop":
        if _existing("drop", short):
            raise ActionError("busy", "A drop for this task is already counting down.", {"job": _existing("drop", short)})
        job, undo = await _thread(_drop, d, b.get("reason") or "easy", run)
        return _accepted(job, undo=undo)
    decisions = await _thread(actions.decide, d, run, dec)
    out = {"decisions": decisions, "summary": await _thread(_summary, d)}
    if dec == "keep":
        def fn(ctx: jobs.JobCtx):
            ctx.step(f"tearing_down {run}", [f"tearing_down {run}"])
            return _teardown_after(d, ctx, run)
        out["job"] = jobs.submit("lane_teardown", f"t:{short}", fn, pool="lane", priority=1, cancellable=False)
    task_changed(short, ["decisions"])
    return JSONResponse(out)


@guarded
async def discard_logged(req: Request):
    short = req.path_params["short"]
    d = _dir(short)
    res = await _thread(actions.discard_logged, d)
    task_changed(short, ["decisions"])
    return JSONResponse({"discard": res, "logged_at": res.get("logged_at"), "summary": await _thread(_summary, d)})


@guarded
async def skip_calibration(req: Request):
    short = req.path_params["short"]
    d = _dir(short)
    await _thread(actions.skip_calibration, d)
    task_changed(short, ["decisions"])
    return JSONResponse({"summary": await _thread(_summary, d)})


@guarded
async def undo(req: Request):
    res = await _thread(jobs.run_undo, req.path_params["action_id"])
    return JSONResponse({"undone": True, **res})


# --- runs -------------------------------------------------------------------------------------------------

@guarded
async def submission_run(req: Request):
    short = req.path_params["short"]
    d = _dir(short)
    if req.method == "GET":
        run, _ = await _thread(actions.validation_run, d)
        meta = await _thread(workspace.load, d)
        return JSONResponse({"submission_run": meta.get("submission_run"), "run": run,
                             "runs": await _thread(actions.runs_overview, d)})
    b = await _body(req)
    if "run" not in b:
        raise ActionError("invalid", "Send {\"run\": \"calib-1\"}, or {\"run\": null} for the latest validation run.")
    res = await _thread(actions.set_submission_run, d, b["run"])
    task_changed(short, ["runs"])
    return JSONResponse({**res, "runs": await _thread(actions.runs_overview, d),
                         "checklist": await _thread(actions.checklist, d)})


def _watcher_busy(short: str, run: str) -> bool:
    try:
        from . import lane_watch
    except Exception:
        return False
    return (lane_watch.STATE.get(f"{short}/{run}") or {}).get("phase") in ("collecting", "packing")


@guarded
async def collect(req: Request):
    short = req.path_params["short"]
    d = _dir(short)
    b = await _body(req)
    actions.check_run_name(b.get("run"))
    run = b.get("run") or await _thread(actions.latest_run, d)
    if not run or not (d / "runs" / run).is_dir():
        raise ActionError("not_found", f"No run {run or ''} in {short}.".replace("  ", " "))
    if _watcher_busy(short, run):
        raise ActionError("busy", f"{run} is being collected right now (the lane watcher started it).")
    live = [j for j in jobs.active(kind="lane_collect", target=f"t:{short}") if j["params"].get("run") == run]
    if live:
        return _accepted(live[0], existing=True)
    want_pack = bool(b.get("pack"))

    def fn(ctx: jobs.JobCtx):
        ctx.step("collecting", ["collecting"] + (["packing"] if want_pack else []))
        info = actions.collect(d, run, log=ctx.log)
        s = info.get("summary") or {}
        out = {"run": run, "diff_stat": info["diff_stat"], "problems": info["problems"], "note": info["note"],
               "transcripts": info["transcripts"], "human_turns": s.get("human_turns"),
               "active_minutes": s.get("active_minutes"), "follow_ups": len(s.get("follow_ups") or [])}
        task_changed(short, ["runs"])
        from .domain.queue import run_word
        jobs.toast(f"Collected {run_word(run)} · {_label(d)}: {s.get('human_turns', '?')} turns · "
                   f"{s.get('active_minutes', '?')} working min", level="warn" if info["problems"] else "ok")
        if want_pack:
            ctx.step("packing")
            from . import api_pack
            pj = api_pack._start(short, run, ai=True)
            out["pack_job"] = pj["id"]
        return out

    job = jobs.submit("lane_collect", f"t:{short}", fn, pool="lane", priority=0, cancellable=False,
                      params={"run": run}, steps=["collecting"])
    return _accepted(job)


@guarded
async def lane_start(req: Request):
    short = req.path_params["short"]
    d = _dir(short)
    b = await _body(req)
    kind = b.get("kind")
    if kind not in ("calib", "validate"):
        raise ActionError("invalid", "kind must be calib or validate.")
    # With the lane service running this is /api/lane/start: set up the lane and start Claude in it. The
    # setup-only path below is for servers without it (JOHTO_NO_WATCH).
    try:
        from .lane.service import LaneError, svc
    except Exception:
        svc = None
    if svc is not None and svc.running:
        try:
            return _accepted(await svc.start_lane(short, kind))
        except LaneError as e:
            raise ActionError(e.code, e.message, e.details) from None
    if jobs.active(kind="lane_prepare"):
        raise ActionError("lane_busy", "A lane is already being set up.", {"job": jobs.active(kind="lane_prepare")[0]})

    def fn(ctx: jobs.JobCtx):
        ctx.step("setup", ["setup"])
        run_dir = actions.start_lane(d, kind, log=ctx.log)
        task_changed(short, ["runs"])
        from .. import lanes
        jobs.toast(f"Lane ready for {_label(d)}: {run_dir.name}", level="ok")
        return {"run": run_dir.name, "launch": lanes.launch_cmd(run_dir)}

    return _accepted(jobs.submit("lane_prepare", f"t:{short}", fn, pool="lane", priority=0, cancellable=False,
                                 params={"kind": kind}, steps=["setup"]))


@guarded
async def clean(req: Request):
    short = req.path_params["short"]
    d = _dir(short)
    b = await _body(req) if req.method != "DELETE" else {}
    run = req.path_params.get("run") or b.get("run")
    actions.check_run_name(run)
    force = bool(b.get("force"))
    if run and not (d / "runs" / run).is_dir():
        raise ActionError("not_found", f"No run {run} in {short}.")

    def fn(ctx: jobs.JobCtx):
        ctx.step("tearing_down", ["tearing_down"])
        res = actions.clean(d, run, force=force)
        for s in res["skipped"]:
            ctx.log(f"kept {s['run']}: {s['reason']}")
        task_changed(short, ["runs"])
        kept = f"; kept {', '.join(s['run'] for s in res['skipped'])}" if res["skipped"] else ""
        jobs.toast(f"Removed lanes for {_label(d)}{kept}", level="ok")
        return res

    return _accepted(jobs.submit("lane_teardown", f"t:{short}", fn, pool="lane", priority=1, cancellable=False,
                                 params={"run": run}, steps=["tearing_down"]))


# --- checks -----------------------------------------------------------------------------------------------

@guarded
async def checklist(req: Request):
    d = _dir(req.path_params["short"])
    run = req.query_params.get("run") or None
    actions.check_run_name(run)
    rows = await _thread(actions.checklist, d, run)
    vrun, _ = await _thread(actions.validation_run, d, run)
    return JSONResponse({"rows": rows, "run": vrun, "blocking": len(actions.submit_blockers(rows)),
                         "ready": not actions.submit_blockers(rows)})


@guarded
async def check(req: Request):
    d = _dir(req.path_params["short"])
    run = req.query_params.get("run") or None
    actions.check_run_name(run)
    probs = await _thread(actions.check, d, run)
    order = sorted(probs, key=lambda p: (p[0] != "error", p[1]))
    return JSONResponse({"flags": [{"level": a, "field": b, "message": c} for a, b, c in order],
                         "errors": sum(p[0] == "error" for p in probs)})


# --- Studio -----------------------------------------------------------------------------------------------

def _guard_overlap(d: Path) -> None:
    """Unaccepted red overlap blocks Save (build-plan 5.2). If the guard can't run, Save is refused too."""
    red = actions.red_overlaps(d)
    if red:
        raise ActionError("blocked_overlap", "Red overlap with AI text blocks saving. Reword it or mark it as your own words.",
                          {"overlaps": [o for v in red.values() for o in v]})


@guarded
async def overlap_accept(req: Request):
    """POST /api/tasks/{short}/overlaps/{overlap_id}/accept: "My own words" for one span (logged)."""
    short, oid = req.path_params["short"], req.path_params["overlap_id"]
    d = _dir(short)
    from .guard import overlap
    from . import data
    sub = await _thread(workspace.parse_submission, d)
    texts = actions.protected_texts(sub)
    key = None
    for k, t in texts.items():
        if t.strip() and any(o["id"] == oid for o in await _thread(overlap.check, d, k, t)):
            key = k
            break
    if not key:
        raise ActionError("not_found", "That overlap is gone (the text changed).")
    await _thread(overlap.accept, d, key, oid)
    task_changed(short, ["sections"])
    t = await _thread(data.real_task, d)
    return JSONResponse({"section": t["sections"].get(key), "checklist": t["checklist"]})


@guarded
async def studio_save(req: Request):
    short = req.path_params["short"]
    d = _dir(short)
    if _existing("save", short):
        return _accepted(_existing("save", short), existing=True)
    await _thread(_guard_overlap, d)
    run = (await _body(req)).get("run")
    actions.check_run_name(run)

    def fn(ctx: jobs.JobCtx):
        ctx.step("recheck", ["recheck", "gh_account", "gists", "saving_fields", "starting_qc"])
        t = actions.recheck_mine(d)
        vrun, rec = actions.validation_run(d, run)
        gists = None
        if rec and rec.get("transcript") and rec.get("diff"):
            gists = actions.upload_gists(d, run, on_step=ctx.step)
        else:
            ctx.log("No collected run with a diff yet, so the gists wait.")
        ctx.check_cancel()
        ctx.step("saving_fields")
        pushed = actions.push(d, run, checked=True, task=t)
        task_changed(short, ["studio"] + (["sections", "ratings"] if pushed.get("pulled") else []))
        if pushed.get("pulled"):
            jobs.toast(f"Brought your Studio edits in first: {', '.join(pushed['pulled'])}", level="info")
        ctx.check_cancel()
        ctx.step("starting_qc")
        aid = actions.start_qc(d, "AutoQC", checked=True)
        child = _qc_wait_job(short, aid, "AutoQC", ctx.job_id) if aid else None
        jobs.toast(f"Saved {_label(d)} to Studio. Mercor's automatic checks are running.", level="ok")
        return {"saved": pushed["saved"], "fields": sorted(pushed["fields"]), "at": pushed["at"],
                "gists": (gists or {}).get("gists"), "run": vrun, "audit_id": aid, "qc_job": (child or {}).get("id")}

    return _accepted(jobs.submit("save", f"t:{short}", fn, pool="studio", priority=0,
                                 steps=["recheck", "gh_account", "gists", "saving_fields", "starting_qc"]))


@guarded
async def studio_qc(req: Request):
    short = req.path_params["short"]
    d = _dir(short)
    spec = (await _body(req)).get("spec") or "AutoQC"
    if spec not in config.QC_SPECS:
        raise ActionError("invalid", f"spec must be one of {', '.join(config.QC_SPECS)}.")
    live = [j for j in jobs.active(target=f"t:{short}") if j["kind"] in ("qc", "qc_wait")
            and j["params"].get("spec") == spec and j["state"] == "running"]
    if live:
        return _accepted(live[0], existing=True)

    def fn(ctx: jobs.JobCtx):
        ctx.step("starting", ["starting"])
        aid = actions.start_qc(d, spec)
        task_changed(short, ["qc"])
        child = _qc_wait_job(short, aid, spec, ctx.job_id) if aid else None
        return {"audit_id": aid, "spec": spec, "qc_job": (child or {}).get("id")}

    return _accepted(jobs.submit("qc", f"t:{short}", fn, pool="studio", priority=0, cancellable=False,
                                 params={"spec": spec}, steps=["starting"]))


@guarded
async def studio_submit(req: Request):
    short = req.path_params["short"]
    d = _dir(short)
    b = await _body(req)
    if _existing("submit", short):
        return _accepted(_existing("submit", short), existing=True)
    rows = await _thread(actions.checklist, d)
    blockers = actions.submit_blockers(rows)
    if blockers:
        raise ActionError("checklist_failed", f"{len(blockers)} checklist row{'s' if len(blockers) > 1 else ''} "
                          f"must pass before submitting: {', '.join(r['title'] for r in blockers)}.", {"rows": rows})
    label = _label(d)
    _confirmed("submit", short, b, f"Press again to submit {label} for review", rows)

    def fn(ctx: jobs.JobCtx):
        ctx.step("recheck", ["recheck", "submitting", "tearing_down"])
        again = actions.submit_blockers(actions.checklist(d))
        if again:
            raise ActionError("checklist_failed", "The checklist changed: " + ", ".join(r["title"] for r in again),
                              {"rows": again})
        ctx.step("submitting")
        actions.submit(d)
        task_changed(short, ["studio"])
        ctx.step("tearing_down")
        res = _teardown_after(d, ctx)
        meta = workspace.load(d)
        jobs.toast(f"Submitted {label} for review", level="ok",
                   link={"label": "Open in Studio", "href": studio.task_url(meta["task_id"])})
        return {"submitted_at": meta.get("submitted_at"), **res}

    return _accepted(jobs.submit("submit", f"t:{short}", fn, pool="studio", priority=0, cancellable=False,
                                 steps=["recheck", "submitting", "tearing_down"]))


@guarded
async def studio_unclaim(req: Request):
    short = req.path_params["short"]
    d = _dir(short)
    b = await _body(req)
    meta = await _thread(workspace.load, d)
    _confirmed("unclaim", short, b, f"Press again to unclaim {_label(d)} in Studio. The slot goes back to the pool.")

    def fn(ctx: jobs.JobCtx):
        ctx.step("recheck", ["recheck", "unclaiming"])
        ctx.step("unclaiming")
        actions.unclaim(d)
        task_changed(short, ["studio"])
        jobs.toast(f"Unclaimed {_label(d)}. The slot is back in the pool.", level="ok")
        return {"unclaimed_at": workspace.load(d).get("unclaimed_at")}

    return _accepted(jobs.submit("unclaim", f"t:{short}", fn, pool="studio", priority=0, cancellable=False,
                                 steps=["recheck", "unclaiming"]))


@guarded
async def studio_refresh(req: Request):
    short = req.path_params["short"]
    _dir(short)
    live = _existing("studio_refresh", short)
    if live:
        return _accepted(live, existing=True)
    return _accepted(_refresh(short))


def _refresh(short: str, priority: int = 1) -> dict:
    """A job that re-reads the task in Studio and, while it is mine and in Writing, brings Studio's edits
    into submission.md (johto.sync: only fields that changed in Studio alone)."""
    d = _dir(short)

    def fn(ctx: jobs.JobCtx):
        from . import data  # late: data loads the domain modules
        ctx.step("get", ["get"])
        before = workspace.load(d)
        prev = set(((before.get("sync") or {}).get("conflicts") or {}))
        was = before.get("studio") or {}
        res = actions.pull_from_studio(
            d, write_section=lambda heading, text: data.write_section(short, data.HEADING_KEY[heading], text),
            write_ratings=lambda r: data.write_ratings(short, r))
        task_changed(short, ["studio"] + (["sections", "ratings"] if res["pulled"] else []))
        if res["pulled"]:
            jobs.toast(f"Brought your Studio edits into {_label(d)}: {', '.join(res['pulled'])}", level="ok")
        new = [k for k in res["conflicts"] if k not in prev]
        if new:
            jobs.toast(f"{', '.join(new)} changed both here and in Studio, so neither copy was touched. "
                       f"Make them match in {_label(d)}.", level="warn")
        _review_news(d, was, (workspace.load(d).get("studio") or {}))
        try:   # a sent-back task: its checklist (a new review) or a check (his fields changed)
            from . import api_feedback
            api_feedback.ensure(d)
        except Exception as e:  # noqa: BLE001
            print(f"feedback: {e}", flush=True)
        return {k: v for k, v in res.items()}

    return jobs.submit("studio_refresh", f"t:{short}", fn, pool="studio", priority=priority, steps=["get"])


def _review_news(d: Path, was: dict, now: dict) -> None:
    """A submitted task's review result reached the app: say so (toast, and a notification when he isn't
    looking). The queue's feedback and rejected rows come from the same task.json fields."""
    st0, st1 = was.get("status"), now.get("status")
    fb_new = now.get("feedback") and now.get("feedback") != was.get("feedback")
    if st0 == st1 and not fb_new:
        return
    label = _label(d)
    if st1 == "Writing" and st0 in ("Awaiting Review", "In Review") or (st1 == "Writing" and fb_new):
        text, level = f"The reviewer sent {label} back", "warn"
    elif st1 == "Accepted":
        text, level = f"{label} was accepted", "ok"
    elif st1 == "Rejected":
        text, level = f"{label} was rejected in review", "error"
    else:
        return
    href = f"/task/{d.name}"
    jobs.toast(text, level=level, link={"label": "Open the task", "href": href})
    try:
        from . import lane_watch
        lane_watch._notify(f"Johto: {text}", (now.get("feedback") or "")[:180], href, tag=f"review-{d.name}")
    except Exception:
        pass


_SYNCED_AT: dict[str, float] = {}
SYNC_EVERY_S = 60       # the background check
SYNC_MIN_GAP_S = 8      # focus refreshes closer together than this are skipped
REVIEW_EVERY_S = 600    # submitted tasks: their review result, at low priority
REVIEW_FOCUS_GAP_S = 60
SUBMITTED = ("Awaiting Review", "In Review")


def _shorts_by_status() -> tuple[list[str], list[str]]:
    """(tasks still in Writing, tasks submitted and waiting for review), from task.json's cached status."""
    writing, submitted = [], []
    for tj in sorted(config.TASKS.glob("*/task.json")):
        try:
            meta = json.loads(tj.read_text())
        except (OSError, ValueError):
            continue
        if meta.get("unclaimed_at") or meta.get("discarded"):
            continue
        st = (meta.get("studio") or {}).get("status", "Writing")
        if st == "Writing":
            writing.append(tj.parent.name)
        elif st in SUBMITTED:
            submitted.append(tj.parent.name)
    return writing, submitted


def _writing_shorts() -> list[str]:
    return _shorts_by_status()[0]


def sync_all(min_gap: float = SYNC_MIN_GAP_S, review_gap: float = REVIEW_FOCUS_GAP_S) -> list[dict]:
    """Refresh every task still in Writing (skipping ones refreshed in the last few seconds), and submitted
    tasks at most every `review_gap` seconds, so a reviewer's result reaches the app."""
    started = []
    now = time.time()
    writing, submitted = _shorts_by_status()
    for short in writing + submitted:
        gap = min_gap if short in writing else review_gap
        if now - _SYNCED_AT.get(short, 0) < gap:
            continue
        live = _existing("studio_refresh", short)
        if live:
            continue
        _SYNCED_AT[short] = now
        try:
            started.append(_refresh(short, priority=1 if short in writing else 2))
        except ActionError:
            continue
    return started


@guarded
async def studio_sync(req: Request):
    """POST /api/studio/sync: the page sends this when it regains focus (back from editing in Studio)."""
    started = await _thread(sync_all)
    return JSONResponse({"jobs": started}, status_code=202)


@guarded
async def studio_fields(req: Request):
    d = _dir(req.path_params["short"])
    return JSONResponse(await _thread(actions.studio_fields_view, d))


@guarded
async def studio_state(req: Request):
    return JSONResponse(jobs.worker.state())


@guarded
async def studio_wake(req: Request):
    jobs.worker.wake()
    return Response(status_code=204)


# --- slots, scout, repo vetting ----------------------------------------------------------------------------

SCOUT_USD_PER_REPO = 0.77     # one headless lead scout (johto/scout/leads.py agent_leads), measured


def _spend_file() -> Path:
    return config.DATA / "web-spend.json"


def spend_today() -> float:
    try:
        d = json.loads(_spend_file().read_text())
    except (OSError, ValueError):
        return 0.0
    return float(d.get(time.strftime("%Y-%m-%d")) or 0.0)


def _add_spend(usd: float) -> None:
    p = _spend_file()
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        d = {}
    day = time.strftime("%Y-%m-%d")
    d[day] = round(float(d.get(day) or 0) + usd, 4)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d))


@guarded
async def slots_sync(req: Request):
    """POST /api/slots/sync: pull every Studio task again (one read through the Studio worker)."""
    live = jobs.active(kind="slots_sync")
    if live:
        return _accepted(live[0], existing=True)

    def fn(ctx: jobs.JobCtx):
        from .. import slots
        ctx.step("get", ["get"])
        out = slots.sync(api=actions.S())
        jobs.publish("leads", {"changed": True})
        return {"synced_at": out["synced_at"], "groups": len(out["groups"]),
                "open": sum(g["count"] for g in out["groups"])}

    return _accepted(jobs.submit("slots_sync", "Studio", fn, pool="studio", priority=1, steps=["get"]))


@guarded
async def repos_vet(req: Request):
    """POST /api/repos/vet {full_name}: vet one repo again (GitHub reads only)."""
    b = await _body(req)
    name = (b.get("full_name") or "").strip()
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", name):
        raise ActionError("invalid", "full_name must look like owner/repo.")

    def fn(ctx: jobs.JobCtx):
        from ..scout import vet
        ctx.step("vetting", ["vetting"])
        rec = vet.vet(name, force=True, keep_commit=True)
        jobs.publish("leads", {"changed": True})
        return {"full_name": rec.get("full_name"), "verdicts": rec.get("verdicts")}

    return _accepted(jobs.submit("vet_repo", f"r:{name}", fn, pool="github", priority=1, params={"full_name": name},
                                 steps=["vetting"]))


@guarded
async def scout(req: Request):
    """POST /api/scout {combo, repos, agent, confirm?}: discover and vet repos for a slot combo, mine their
    issues, and (agent) run the headless lead scout on the best few. No spend cap: Austin is on a Claude
    subscription. Leads are research: facts, files, mechanisms; never field text."""
    b = await _body(req)
    combo = b.get("combo") or {}
    slot = {k: combo.get(k) for k in ("action", "domain", "codebase_type")}
    if None in slot.values():
        raise ActionError("invalid", "combo needs action, domain and codebase_type.")
    n = max(1, min(6, int(b.get("repos") or 3)))
    agent = b.get("agent", True) is not False
    if jobs.active(kind="scout"):
        return _accepted(jobs.active(kind="scout")[0], existing=True)
    est = round(n * SCOUT_USD_PER_REPO, 2) if agent else 0.0

    def fn(ctx: jobs.JobCtx):
        from ..scout import cli as scli, leads
        steps = ["discovering", "mining issues"] + (["lead scout"] if agent else [])
        ctx.step("discovering", steps)
        recs = scli.vet_slot({**slot, "prompt_style": None, "interaction_mode": None}, limit=12, vet_limit=8, quiet=True)
        ct = slot["codebase_type"]
        rank = {"ok": 0, "maybe": 1}
        picked = sorted([r for r in recs if ((r.get("verdicts") or {}).get(ct) or {}).get("overall") in rank],
                        key=lambda r: rank[r["verdicts"][ct]["overall"]])[:n]
        ctx.log(f"{len(recs)} repos vetted, {len(picked)} picked")
        ctx.step("mining issues")
        found = 0
        for rec in picked:
            ctx.check_cancel()
            try:
                found += len(leads.upsert(leads.mine_issues(rec, slot)))
            except Exception as e:
                ctx.log(f"{rec.get('display_name') or rec.get('full_name')}: issues failed: {e}")
        if agent:
            ctx.step("lead scout")
            for rec in picked:
                ctx.check_cancel()
                ctx.log(f"lead scout on {rec.get('display_name') or rec.get('full_name')}")
                try:
                    found += len(leads.upsert(leads.agent_leads(rec, slot)))
                except (RuntimeError, OSError, ValueError) as e:
                    ctx.log(f"  failed: {e}")
                    continue
                finally:
                    _add_spend(SCOUT_USD_PER_REPO)
                    ctx.add_cost(SCOUT_USD_PER_REPO)
        jobs.publish("leads", {"changed": True})
        jobs.publish("spend", {"today_usd": spend_today(), "paused_until": None})
        return {"repos": [r.get("full_name") for r in picked], "leads": found}

    return _accepted(jobs.submit("scout", "combo:" + "|".join(slot.values()), fn, pool="ai", priority=1, estimate_usd=est,
                                 params={"combo": slot},
                                 steps=["discovering", "mining issues"] + (["lead scout"] if agent else [])))


# --- jobs -------------------------------------------------------------------------------------------------

@guarded
async def list_jobs(req: Request):
    try:
        limit = max(1, min(500, int(req.query_params.get("limit") or 50)))
    except ValueError:
        raise ActionError("invalid", "limit must be a number.") from None
    return JSONResponse({"jobs": jobs.list_jobs(req.query_params.get("state") or None, limit)})


@guarded
async def get_job(req: Request):
    j = jobs.get(req.path_params["id"])
    if not j:
        raise ActionError("not_found", "No such job.")
    return JSONResponse({"job": j})


@guarded
async def cancel_job(req: Request):
    return JSONResponse({"job": jobs.cancel(req.path_params["id"])})


# --- run notes (Austin's own words, typed in the lane drawer) ----------------------------------------------

_NOTES_LOCK = __import__("threading").Lock()


def _notes_file(d: Path, run: str) -> Path:
    actions.check_run_name(run)
    if not (d / "runs" / run).is_dir():
        raise ActionError("not_found", f"No run {run} in {d.name}.")
    return d / "app" / f"notes-{run}.jsonl"


def _note_clock(d: Path, run: str) -> tuple[int, int | None]:
    """(seconds into the run, Claude's turn) from the lane's live facts, else from when the lane was set up."""
    try:
        from .lane.service import svc
        st = svc.state()
        r, f = st.get("run") or {}, st.get("facts") or {}
        if r.get("short") == d.name and r.get("run") == run and f:
            return int(round((f.get("wall_min") or 0) * 60)), f.get("turns")
    except Exception:
        pass
    rec = actions.run_record(d, run) or {}
    try:
        from datetime import datetime
        t0 = datetime.strptime(rec.get("created_at") or "", "%Y-%m-%dT%H:%M:%S%z").timestamp()
        return max(0, int(time.time() - t0)), None
    except ValueError:
        return 0, None


def _append_note(p: Path, rec: dict) -> None:
    with _NOTES_LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as fh:
            fh.write(json.dumps(rec) + "\n")


def _note_text(b: dict) -> str:
    text = b.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ActionError("invalid", "A note needs some text.")
    return text.strip()


@guarded
async def note_add(req: Request):
    d = _dir(req.path_params["short"])
    run = req.path_params["run"]
    p = _notes_file(d, run)
    text = _note_text(await _body(req))
    t_run_s, turn = _note_clock(d, run)
    note = {"id": secrets.token_hex(4), "at": actions.now_iso(), "t_run_s": t_run_s, "turn": turn, "text": text}
    _append_note(p, note)
    task_changed(d.name, ["notes"])
    return JSONResponse(note)


@guarded
async def note_edit(req: Request):
    d = _dir(req.path_params["short"])
    run, nid = req.path_params["run"], req.path_params["id"]
    p = _notes_file(d, run)
    from . import data
    old = next((n for n in data.read_notes(d, run) if n["id"] == nid), None)
    if not old:
        raise ActionError("not_found", "No such note.")
    if req.method == "DELETE":
        _append_note(p, {"id": nid, "deleted": True, "at": actions.now_iso()})
        task_changed(d.name, ["notes"])
        return Response(status_code=204)
    note = {**old, "text": _note_text(await _body(req))}
    _append_note(p, note)
    task_changed(d.name, ["notes"])
    return JSONResponse(note)


# --- table and lifecycle -----------------------------------------------------------------------------------

routes = [
    Route("/api/claim", claim, methods=["POST"]),
    Route("/api/tasks/{short}/decision", decision, methods=["POST"]),
    Route("/api/tasks/{short}/discard-logged", discard_logged, methods=["POST"]),
    Route("/api/tasks/{short}/skip-calibration", skip_calibration, methods=["POST"]),
    Route("/api/undo/{action_id}", undo, methods=["POST"]),
    Route("/api/tasks/{short}/submission-run", submission_run, methods=["GET", "POST"]),
    Route("/api/tasks/{short}/collect", collect, methods=["POST"]),
    Route("/api/tasks/{short}/lane", lane_start, methods=["POST"]),
    Route("/api/tasks/{short}/clean", clean, methods=["POST"]),
    Route("/api/tasks/{short}/runs/{run}/lane", clean, methods=["DELETE"]),
    Route("/api/tasks/{short}/runs/{run}/notes", note_add, methods=["POST"]),
    Route("/api/tasks/{short}/runs/{run}/notes/{id}", note_edit, methods=["PUT", "DELETE"]),
    Route("/api/tasks/{short}/overlaps/{overlap_id}/accept", overlap_accept, methods=["POST"]),
    Route("/api/tasks/{short}/checklist", checklist),
    Route("/api/tasks/{short}/lint", check),   # the old local lint; /check is the draft check (api_task)
    Route("/api/tasks/{short}/studio/save", studio_save, methods=["POST"]),
    Route("/api/tasks/{short}/studio/qc", studio_qc, methods=["POST"]),
    Route("/api/tasks/{short}/studio/submit", studio_submit, methods=["POST"]),
    Route("/api/tasks/{short}/studio/unclaim", studio_unclaim, methods=["POST"]),
    Route("/api/tasks/{short}/studio/refresh", studio_refresh, methods=["POST"]),
    Route("/api/studio/sync", studio_sync, methods=["POST"]),
    Route("/api/tasks/{short}/studio/fields", studio_fields),
    Route("/api/studio", studio_state),
    Route("/api/studio/wake", studio_wake, methods=["POST"]),
    Route("/api/slots/sync", slots_sync, methods=["POST"]),
    Route("/api/repos/vet", repos_vet, methods=["POST"]),
    Route("/api/scout", scout, methods=["POST"]),
    Route("/api/jobs", list_jobs),
    Route("/api/jobs/{id}", get_job),
    Route("/api/jobs/{id}", cancel_job, methods=["DELETE"]),
]


def set_publisher(fn) -> None:
    """fn(name, data) -> any: the SSE hub's publish. Job, toast, task, task.removed, leads and studio events."""
    jobs.set_publisher(fn)


def studio_state_now() -> dict:
    """For GET /api/state's `studio` key: {state, pending, last_ok_at}."""
    s = jobs.worker.state()
    return {"state": s["state"], "pending": s["pending"], "last_ok_at": s["last_ok_at"]}


_POLL_STOP = None


def _poll_loop(stop) -> None:
    while not stop.wait(SYNC_EVERY_S):
        try:
            sync_all(min_gap=SYNC_EVERY_S / 2, review_gap=REVIEW_EVERY_S)
        except Exception as e:  # the next round tries again
            print(f"studio sync: {e}", flush=True)


def start() -> None:
    global _POLL_STOP
    jobs.start()
    if not os.environ.get("JOHTO_NO_STUDIO_POLL"):
        import threading
        _POLL_STOP = threading.Event()
        threading.Thread(target=_poll_loop, args=(_POLL_STOP,), name="studio-sync", daemon=True).start()


def stop() -> None:
    if _POLL_STOP is not None:
        _POLL_STOP.set()
    jobs.stop()
