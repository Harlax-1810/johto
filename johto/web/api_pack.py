"""Review-pack API: build a pack for a run in the background, then copy it or show it in Finder."""
from __future__ import annotations

import secrets
import subprocess
import threading
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .. import config, pack, settings

JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _job_view(j: dict) -> dict:
    return {k: v for k, v in j.items() if not k.startswith("_")}


def _start(short: str, run: str, ai: bool) -> dict:
    # A fake or test server never starts the paid outside review unless it was handed a stub claude.
    ai = bool(ai) and (not config.FAKE or config.CLAUDE_BIN_SET)
    with _LOCK:
        live = next((j for j in JOBS.values() if j["short"] == short and j["run"] == run and j["state"] == "running"), None)
        if live:
            return live
        jid = secrets.token_hex(4)
        job = {"id": jid, "short": short, "run": run, "ai": ai, "state": "running", "log": [],
               "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "out": None, "tokens": None, "error": None,
               "_done": threading.Event()}   # set when the pack ends, so waiters need no polling
        JOBS[jid] = job

    def log(line: str) -> None:
        job["log"].append(line)
        if "tokens)" in line and "Pack:" in line:
            try:
                job["tokens"] = int(line.rsplit("(", 1)[1].split()[0].replace(",", ""))
            except (ValueError, IndexError):
                pass

    def work():
        try:
            out = pack.build(config.TASKS / short, run, ai=ai, log=log)
            job.update(state="done", out=str(out))
        except BaseException as e:  # SystemExit carries plain-words messages
            job.update(state="failed", error=str(e) or e.__class__.__name__)
        finally:
            job["_done"].set()
    threading.Thread(target=work, daemon=True, name=f"pack-{short}-{run}").start()
    return job


def _packs(short: str) -> list[dict]:
    out = []
    runs = config.TASKS / short / "runs"
    found = [*runs.glob("*/review-pack-*.xml"), *runs.glob("*/review-followup-*.xml")]   # full packs and follow-ups
    for p in sorted(found, key=lambda p: -p.stat().st_mtime):
        out.append({"run": p.parent.name, "path": str(p), "name": p.name, "bytes": p.stat().st_size,
                    "made_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(p.stat().st_mtime))})
    return out


async def start(req: Request):
    short, run = req.path_params["short"], req.path_params["run"]
    if req.cookies.get("johto_mode") == "demo":
        return JSONResponse({"error": {"code": "demo", "message": "Review packs need a real run.", "details": {}}}, status_code=409)
    if not (config.TASKS / short / "runs" / run).is_dir():
        return JSONResponse({"error": {"code": "not_found", "message": f"No run {run} for {short}.", "details": {}}}, status_code=404)
    body = await req.json() if (await req.body()) else {}
    # The outside Claude review is added only when asked, else by settings pack_outside_review (off by default).
    ai = body["ai"] if isinstance(body.get("ai"), bool) else bool(settings.get("pack_outside_review"))
    return JSONResponse({"job": _job_view(_start(short, run, ai))}, status_code=202)


async def job(req: Request):
    j = JOBS.get(req.path_params["id"])
    if not j:
        return JSONResponse({"error": {"code": "not_found", "message": "No such pack job.", "details": {}}}, status_code=404)
    return JSONResponse({"job": _job_view(j)})


async def listing(req: Request):
    short = req.path_params["short"]
    running = [_job_view(j) for j in JOBS.values() if j["short"] == short and j["state"] == "running"]
    return JSONResponse({"packs": _packs(short), "running": running})


def _resolve(req: Request) -> Path | None:
    short, name = req.path_params["short"], req.path_params["name"]
    for p in (config.TASKS / short / "runs").glob(f"*/{name}"):
        if p.name.startswith(("review-pack-", "review-followup-")) and p.suffix == ".xml":
            return p
    return None


async def copy(req: Request):
    p = _resolve(req)
    if not p:
        return JSONResponse({"error": {"code": "not_found", "message": "Pack not found.", "details": {}}}, status_code=404)
    subprocess.run(["pbcopy"], input=p.read_text(), text=True)
    return JSONResponse({"toast": {"text": f"Copied {p.name} to the clipboard", "level": "ok"}})


async def reveal(req: Request):
    p = _resolve(req)
    if not p:
        return JSONResponse({"error": {"code": "not_found", "message": "Pack not found.", "details": {}}}, status_code=404)
    subprocess.Popen(["open", "-R", str(p)])
    return JSONResponse({"toast": {"text": "Shown in Finder", "level": "ok"}})


routes = [
    Route("/api/tasks/{short}/runs/{run}/pack", start, methods=["POST"]),
    Route("/api/tasks/{short}/packs", listing),
    Route("/api/tasks/{short}/packs/{name}/copy", copy, methods=["POST"]),
    Route("/api/tasks/{short}/packs/{name}/reveal", reveal, methods=["POST"]),
    Route("/api/packs/{id}", job),
]
