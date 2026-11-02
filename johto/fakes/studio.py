"""A fake Mercor Studio with the same functions as johto/studio.py, held in memory.

Seeded from data/studio_tasks.json (every task, compact) plus the local task folders (each one
becomes a Writing task owned by the fake "me"). With JOHTO_ROOT set, the state is also saved to
data/fake/studio.json so the CLI and the web app (separate processes) see the same Studio.

Test hooks: reset(), inject(fn, exc), take(task_id), sign_out()/sign_in(), QC_FAIL (dimension names
that fail), QC_DELAY_S, DELAY_S, CALLS (every call in order).
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid

from .. import config
from ..studio import StudioError, slot_of, status_name, task_url  # noqa: F401  (same names as johto.studio)
from . import guard_paths

ME = "user_fake_austin"
OTHER = "user_fake_someone_else"
STATUS_IDS = {
    "Not Ready to Claim": config.STATUS["not_ready"],
    "Ready to Claim": config.STATUS["ready"],
    "Writing": config.STATUS["writing"],
    "Awaiting Review": config.STATUS["awaiting_review"],
    "In Review": config.STATUS["in_review"],
    "Accepted": config.STATUS["accepted"],
    "Rejected": config.STATUS["rejected"],
}
# What Submit for Review needs. Studio's current form doesn't require the gist link fields (accepted tasks went
# through without them); its Trajectory Logs upload is checked by johto (actions.trajectory_state), not here.
REQUIRED = ("field_repo_name", "field_repo_url", "field_primary_language", "field_opening_prompt",
            "field_high_level_goal", "field_rationale_difficulty", "field_codebase_overview",
            "field_self_check", "field_one_shot", "field_task_success")
EDGES = {  # status -> [(transition name, next status, needs ownership)]
    "Ready to Claim": [(config.CLAIM, "Writing", False)],
    "Writing": [(config.SUBMIT, "Awaiting Review", True), (config.UNCLAIM, "Ready to Claim", True)],
}
QC_DIMS = {
    "AutoQC": [
        ("Codebase", "Codebase Eligibility", "field_repo_url"),
        ("Opening Prompt", "Prompt Style Fit", "field_opening_prompt"),
        ("Opening Prompt", "Nothing Private Leaks", "field_opening_prompt"),
        ("Opening Prompt", "Doesn't Solve It", "field_opening_prompt"),
        ("Opening Prompt", "No Formatting Tells", "field_opening_prompt"),
        ("Task & Rationale", "Task Scope", "field_high_level_goal"),
        ("Task & Rationale", "High-Level Task Goal", "field_high_level_goal"),
        ("Task & Rationale", "Decision Points", "field_rationale_difficulty"),
        ("Task & Rationale", "Difficulty & Codebase Understanding", "field_codebase_overview"),
        ("Task & Rationale", "Seeded Domain Match", "field_repo_name"),
        ("Task & Rationale", "Seeded Action Match", "field_opening_prompt"),
        ("Authenticity", "AI Detection", "field_opening_prompt"),
    ],
    "Codebase Check": [
        ("Codebase", "Repo & Commit Reachable", "field_repo_url"),
        ("Codebase", "Application, Not Library/Tool Infrastructure", "field_repo_name"),
        ("Codebase", "Size Matches Codebase Type", "field_repo_name"),
        ("Codebase", "Upstream & Not Benchmarked", "field_repo_name"),
        ("Codebase", "Runnable Verification Path", "field_repo_name"),
    ],
}
SPEC_NAME = {v: k for k, v in config.QC_SPECS.items()}

QC_DELAY_S = float(os.environ.get("JOHTO_FAKE_QC_S", "2"))
DELAY_S = float(os.environ.get("JOHTO_FAKE_STUDIO_DELAY_S", "0"))
QC_FAIL: set[str] = set(filter(None, os.environ.get("JOHTO_FAKE_QC_FAIL", "").split(",")))
CALLS: list[tuple[str, tuple]] = []

_LOCK = threading.RLock()
_STATE: dict | None = None
_STATE_MTIME = 0.0
_FAULTS: dict[str, list[BaseException]] = {}
_SIGNED_OUT = False


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _state_file():
    if config.ROOT == config.CHECKOUT:
        return None
    return config.DATA / "fake" / "studio.json"


def _record(t: dict) -> dict:
    """A full task record (the shape GET /tasks/{id} returns) from a compact studio_tasks.json row."""
    s = t.get("slot") or {}
    dom = s.get("domain") or ""
    cf = {"field_action": s.get("action"), "field_domain": [d for d in dom.split(", ") if d],
          "field_codebase_type": s.get("codebase_type"), "field_prompt_style": s.get("prompt_style"),
          "field_interaction_mode": s.get("interaction_mode"), "field_version": "v2"}
    if t.get("repo_url"):
        cf["field_repo_url"] = t["repo_url"]
    if t.get("opening_prompt"):
        cf["field_opening_prompt"] = t["opening_prompt"]
    st = t.get("status") or "Ready to Claim"
    return {"task_id": t["task_id"], "task_name": t["task_name"], "owned_by": t.get("owned_by"),
            "task_status_id": STATUS_IDS.get(st, config.STATUS["ready"]),
            "task_status_defn": {"status_id": STATUS_IDS.get(st), "status_name": st},
            "custom_fields": cf, "version": 1, "updated_at": t.get("updated_at") or _now(),
            "review_feedback": None}


def _seed() -> dict:
    tasks: dict[str, dict] = {}
    p = config.DATA / "studio_tasks.json"
    if p.exists():
        for t in json.loads(p.read_text()):
            tasks[t["task_id"]] = _record(t)
    for tj in sorted(config.TASKS.glob("*/task.json")):
        try:
            meta = json.loads(tj.read_text())
        except (OSError, ValueError):
            continue
        rec = tasks.get(meta["task_id"]) or _record({"task_id": meta["task_id"], "task_name": meta["task_name"],
                                                    "slot": meta.get("slot"), "status": "Writing"})
        rec["owned_by"] = ME
        rec["task_status_id"] = STATUS_IDS["Writing"]
        rec["task_status_defn"] = {"status_id": STATUS_IDS["Writing"], "status_name": "Writing"}
        repo = meta.get("repo") or {}
        rec["custom_fields"].update({k: v for k, v in (("field_repo_name", repo.get("full_name")),
                                                       ("field_repo_url", repo.get("pinned_url")),
                                                       ("field_primary_language", repo.get("language"))) if v})
        tasks[meta["task_id"]] = rec
    return {"me": ME, "tasks": tasks, "audits": {}, "history": {}}


def _load() -> dict:
    global _STATE, _STATE_MTIME
    guard_paths()
    f = _state_file()
    if f and f.exists() and f.stat().st_mtime > _STATE_MTIME:
        _STATE = json.loads(f.read_text())
        _STATE_MTIME = f.stat().st_mtime
    if _STATE is None:
        _STATE = _seed()
        _save()
    return _STATE


def _save() -> None:
    global _STATE_MTIME
    f = _state_file()
    if not f or _STATE is None:
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(_STATE))
    tmp.replace(f)
    _STATE_MTIME = f.stat().st_mtime


def _call(name: str, *args):
    """Bookkeeping every public call does: record, latency, sign-out and injected faults."""
    CALLS.append((name, args))
    if DELAY_S:
        time.sleep(DELAY_S)
    if _SIGNED_OUT:
        raise StudioError(0, "Studio tab has no Clerk session (logged out?)", name)
    q = _FAULTS.get(name)
    if q:
        raise q.pop(0)


def _get(task_id: str) -> dict:
    t = _load()["tasks"].get(task_id)
    if t is None:
        raise StudioError(404, {"detail": "Task not found"}, f"/tasks/{task_id}")
    return t


def _set_status(t: dict, st: str, owner) -> None:
    t["task_status_id"] = STATUS_IDS[st]
    t["task_status_defn"] = {"status_id": STATUS_IDS[st], "status_name": st}
    t["owned_by"] = owner
    t["version"] = t.get("version", 1) + 1
    t["updated_at"] = _now()


# --- test hooks ------------------------------------------------------------------------------------------

def reset() -> None:
    """Forget everything and reseed from the files on next use."""
    global _STATE, _STATE_MTIME, _SIGNED_OUT
    with _LOCK:
        _STATE, _STATE_MTIME, _SIGNED_OUT = None, 0.0, False
        _FAULTS.clear()
        CALLS.clear()
        QC_FAIL.clear()
        f = _state_file()
        if f and f.exists():
            f.unlink()


def inject(fn_name: str, exc: BaseException, times: int = 1) -> None:
    _FAULTS.setdefault(fn_name, []).extend([exc] * times)


def take(task_id: str, owner: str = OTHER) -> None:
    """Someone else claims the slot (for 'taken' paths)."""
    with _LOCK:
        _set_status(_get(task_id), "Writing", owner)
        _save()


def sign_out() -> None:
    global _SIGNED_OUT
    _SIGNED_OUT = True


def sign_in() -> None:
    global _SIGNED_OUT
    _SIGNED_OUT = False


def set_status(task_id: str, st: str, owner=ME, feedback: str | None = None) -> None:
    """Move a task, as a reviewer would (sent back, accepted...)."""
    with _LOCK:
        t = _get(task_id)
        _set_status(t, st, owner)
        if feedback is not None:
            t["custom_fields"]["field_review_writer_feedback"] = feedback
        _save()


# --- the johto.studio interface ----------------------------------------------------------------------

def request(method: str, path: str, body=None, query: dict | None = None, timeout: float = 180):
    raise StudioError(501, f"the fake Studio has no raw {method} {path}", path)


def get(path, **query):
    return request("GET", path, query=query or None)


def list_tasks(view: str | None = None, status: str | None = None, page_size: int = 200,
               max_pages: int = 50) -> list[dict]:
    with _LOCK:
        _call("list_tasks", view, status)
        out = []
        for t in _load()["tasks"].values():
            st, own = t["task_status_defn"]["status_name"], t.get("owned_by")
            if status and t["task_status_id"] != status:
                continue
            v = config.VIEWS.get(view, view) if view else None
            if v == config.VIEWS["available"] and st != "Ready to Claim":
                continue
            if v == config.VIEWS["mine"] and not (st == "Writing" and own == ME):
                continue
            if v == config.VIEWS["submitted"] and not (st in ("Awaiting Review", "In Review") and own == ME):
                continue
            if v == config.VIEWS["accepted"] and not (st == "Accepted" and own == ME):
                continue
            if v == config.VIEWS["rejected"] and not (st == "Rejected" and own == ME):
                continue
            out.append(copy.deepcopy(t))
        return out


def task(task_id: str) -> dict:
    with _LOCK:
        _call("task", task_id)
        return copy.deepcopy(_get(task_id))


def find_task(name_or_id: str) -> dict:
    s = name_or_id.strip()
    if s.startswith("task_"):
        return task(s)
    name = s if s.lower().startswith("task ") else f"Task {s}"
    with _LOCK:
        _call("find_task", s)
        for t in _load()["tasks"].values():
            if t["task_name"].lower() == name.lower():
                return copy.deepcopy(t)
    raise LookupError(f"No task named {name!r}")


def history(task_id: str):
    with _LOCK:
        _call("history", task_id)
        return {"history": copy.deepcopy(_load()["history"].get(task_id, []))}


def me() -> dict:
    with _LOCK:
        _call("me")
        return {"user_id": _load()["me"], "email": "fake@example.invalid", "first_name": "Fake", "last_name": "Austin"}


def eligible_transitions(task_id: str) -> list[dict]:
    with _LOCK:
        _call("eligible_transitions", task_id)
        t = _get(task_id)
        st = t["task_status_defn"]["status_name"]
        return [{"edge_id": f"edge-{name}", "transition_name": name}
                for name, _, own in EDGES.get(st, []) if not own or t.get("owned_by") == ME]


def transition(task_id: str, name: str) -> dict:
    with _LOCK:
        _call("transition", task_id, name)
        t = _get(task_id)
        st = t["task_status_defn"]["status_name"]
        edges = [(n, nxt) for n, nxt, own in EDGES.get(st, []) if not own or t.get("owned_by") == ME]
        nxt = next((x for n, x in edges if n == name), None)
        if nxt is None:
            raise StudioError(409, f"'{name}' is not available now; available: {[n for n, _ in edges]}", task_id)
        cf = t["custom_fields"]
        if name == config.SUBMIT:
            missing = [k for k in REQUIRED if not str(cf.get(k) or "").strip()]
            if missing:
                raise StudioError(422, {"detail": f"Required fields are missing: {', '.join(missing)}"},
                                  f"/tasks/{task_id}/transition")
            done = [a for a in _load()["audits"].values()
                    if a["subject_id"] == task_id and a["spec"] == "AutoQC" and a["status"] == "completed"]
            if not done:
                raise StudioError(422, {"detail": "AutoQC must run before this transition"},
                                  f"/tasks/{task_id}/transition")
            cf["field_times_submitted"] = int(cf.get("field_times_submitted") or 0) + 1
        _set_status(t, nxt, ME if nxt in ("Writing", "Awaiting Review") else None)
        _load()["history"].setdefault(task_id, []).append(
            {"at": _now(), "transition": name, "to": nxt, "by": ME})
        _save()
        return {"task_id": task_id, "task_status_id": t["task_status_id"], "status": nxt}


def patch_fields(task_id: str, fields: dict) -> dict:
    with _LOCK:
        _call("patch_fields", task_id, tuple(sorted(fields)))
        t = _get(task_id)
        if t["task_status_defn"]["status_name"] != "Writing" or t.get("owned_by") != ME:
            raise StudioError(403, {"detail": "You can only edit tasks you are writing"}, f"/tasks/{task_id}")
        seeds = [k for k in config.SLOT_FIELDS if k in fields and fields[k] != t["custom_fields"].get(k)]
        if seeds:
            raise StudioError(500, f"seeded fields changed after save: {seeds}. Check the task in Studio.", task_id)
        t["custom_fields"].update(copy.deepcopy(fields))
        t["version"] = t.get("version", 1) + 1
        t["updated_at"] = _now()
        _save()
        return copy.deepcopy(t["custom_fields"])


def run_qc(task_id: str, spec: str = "AutoQC") -> dict:
    with _LOCK:
        _call("run_qc", task_id, spec)
        _get(task_id)
        aid = "qca_fake_" + uuid.uuid4().hex[:12]
        _load()["audits"][aid] = {"id": aid, "subject_id": task_id, "spec": spec, "status": "running",
                                  "started": time.time(), "created_at": _now(), "fields": None}
        _save()
        return {"qc_audit_id": aid, "status": "pending"}


def _finish(a: dict) -> None:
    t = _get(a["subject_id"])
    cf = t["custom_fields"]
    sections: dict[str, list] = {}
    for sec, dim, field in QC_DIMS[a["spec"]]:
        v = str(cf.get(field) or "").strip()
        if dim in QC_FAIL:
            status, why = "fail", f"The {dim} check failed on this text (fake AutoQC, forced)."
        elif not v:
            status, why = "fail", f"{field} is empty."
        else:
            status, why = "pass", "Looks fine (fake AutoQC)."
        sections.setdefault(sec, []).append({"dimension": dim, "status": status, "justification": why})
    a["status"] = "completed"
    a["completed_at"] = _now()
    a["outcome"] = {"sections": [{"section": s, "name": s, "dimensions": d} for s, d in sections.items()]}


def qc_audit(audit_id: str) -> dict:
    with _LOCK:
        _call("qc_audit", audit_id)
        a = _load()["audits"].get(audit_id)
        if a is None:
            raise StudioError(404, {"detail": "Audit not found"}, f"/qc-audits/{audit_id}")
        if a["status"] == "running" and time.time() - a["started"] >= QC_DELAY_S:
            _finish(a)
            _save()
        out = {k: v for k, v in a.items() if k not in ("started", "fields")}
        out["qc_spec_id"] = config.QC_SPECS[a["spec"]]
        return copy.deepcopy(out)


def qc_audits(task_id: str, status: str | None = None) -> list[dict]:
    with _LOCK:
        _call("qc_audits", task_id, status)
        ids = [a["id"] for a in _load()["audits"].values() if a["subject_id"] == task_id]
    return [a for a in (qc_audit(i) for i in ids) if not status or a["status"] == status]


def wait_qc(audit_id: str, timeout: float = 900, every: float = 15) -> dict:
    end = time.time() + timeout
    while True:
        a = qc_audit(audit_id)
        if a.get("status") in ("completed", "failed", "cancelled") or time.time() > end:
            return a
        time.sleep(min(every, max(0.05, QC_DELAY_S / 4)))
