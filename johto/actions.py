"""Task actions shared by the CLI and the web app.

Every function here is synchronous, never calls input() or print(), returns data, and raises
ActionError(code, message, details) with a plain-words message, or studio.StudioError with Studio's
response verbatim. Nothing here writes, suggests or rewrites the text of Austin's fields: the only
text that goes to Studio is what submission.md already holds.

Studio access goes through S(). Outside the web app that is johto.studio (or the fake with
JOHTO_FAKE_STUDIO=1). Inside a web job it is a proxy that queues each call on the single Studio
worker (johto/web/jobs.py), so calls never overlap in the one Studio tab.

A validation lane passes two gates first (lane_gate): no earlier validation run used the same prompt, and
Studio's AutoQC has passed the opening prompt as it is now. Steps that move a task forward log a stage event
(johto.stages) on the way.
"""
from __future__ import annotations

import contextlib
import contextvars
import fcntl
import hashlib
import importlib
import io
import json
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from . import config, gists, lanes, lint, stages, studio, sync, transcript, workspace

# --- errors -------------------------------------------------------------------------------------------

HTTP_STATUS = {
    "invalid": 400, "budget": 402, "not_found": 404, "expired": 410,
    "needs_confirm": 409, "precondition_failed": 409, "lane_busy": 409, "lane_state": 409, "taken": 409,
    "claim_partial": 409, "busy": 409, "gh_account": 409, "conflict": 409, "sync_conflict": 409,
    "missing_fields": 422, "checklist_failed": 422, "guard_missing": 423, "blocked_overlap": 423,
    "same_prompt": 409, "prompt_qc": 409, "no_slot": 409, "no_open_slot": 409, "slot_mismatch": 409,
    "studio_error": 502, "github": 502, "setup_failed": 500, "collect_failed": 500, "internal": 500,
}


class ActionError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}

    @property
    def status(self) -> int:
        return HTTP_STATUS.get(self.code, 400)

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


def studio_error_dict(e: studio.StudioError) -> dict:
    """A Studio failure as an API error, with Studio's body verbatim."""
    body = e.body
    text = body.get("detail") if isinstance(body, dict) and isinstance(body.get("detail"), str) else body
    text = text if isinstance(text, str) else json.dumps(text)
    return {"code": "studio_error", "message": f"Studio {e.status}: {text}",
            "details": {"status": e.status, "body": body, "path": e.path}}


def signed_out(e: BaseException) -> bool:
    return isinstance(e, studio.StudioError) and e.status == 0 and "Clerk" in str(e.body)


# --- Studio, gh and lanes switch points -----------------------------------------------------------------

_STUDIO: contextvars.ContextVar = contextvars.ContextVar("johto_studio", default=None)


def base_studio():
    """The module that really talks to Studio: johto.studio, or the fake."""
    if config.FAKE_STUDIO:
        from .fakes import studio as fake
        return fake
    return studio


def S():
    """Studio for the current context: the web worker's proxy inside a job, else base_studio()."""
    return _STUDIO.get() or base_studio()


@contextlib.contextmanager
def using_studio(obj):
    token = _STUDIO.set(obj)
    try:
        yield obj
    finally:
        _STUDIO.reset(token)


def G():
    """Gists: johto.gists, or the fake."""
    if config.FAKE_STUDIO:
        from .fakes import gh
        return gh
    return gists


def L():
    """Lanes: johto.lanes, or the Docker-free fake."""
    if config.FAKE_STUDIO:
        from .fakes import lanes as fake
        return fake
    return lanes


# --- small helpers ------------------------------------------------------------------------------------

def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def sha12(v) -> str:
    s = v if isinstance(v, str) else json.dumps(v, sort_keys=True)
    return hashlib.sha1(s.strip().encode()).hexdigest()[:12]


def task_dir(name: str) -> Path:
    try:
        return workspace.resolve(name)
    except FileNotFoundError:
        raise ActionError("not_found", f"No task folder for {name}.") from None


@contextlib.contextmanager
def locked(d: Path):
    """Exclusive lock on the task folder (d/.lock) around a read-modify-write of task.json."""
    shared = getattr(workspace, "locked", None)
    if shared is not None:
        with shared(d):
            yield
        return
    with open(d / ".lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def update_meta(d: Path, fn) -> dict:
    """Load task.json, let fn change it in place, save it; all under the folder lock."""
    with locked(d):
        meta = workspace.load(d)
        fn(meta)
        workspace.save(d, meta)
        return meta


def _write_json(p: Path, data) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(p)


class _ThreadStdout(io.TextIOBase):
    """Sends print() output from registered threads to a callback; everything else to the real stdout."""

    def __init__(self, real):
        self.real, self.sinks, self.bufs = real, {}, {}

    def write(self, s):
        tid = threading.get_ident()
        sink = self.sinks.get(tid)
        if sink is None:
            return self.real.write(s)
        buf = self.bufs.get(tid, "") + s
        *lines, rest = buf.split("\n")
        self.bufs[tid] = rest
        for ln in lines:
            sink(ln)
        return len(s)

    def flush(self):
        self.real.flush()


_STDOUT_LOCK = threading.Lock()


@contextlib.contextmanager
def capture_prints(log):
    """Route this thread's print() calls (from johto.lanes) into log(line)."""
    if log is print:
        yield
        return
    with _STDOUT_LOCK:
        if not isinstance(sys.stdout, _ThreadStdout):
            sys.stdout = _ThreadStdout(sys.stdout)
        out = sys.stdout
    tid = threading.get_ident()
    out.sinks[tid] = log
    try:
        yield
    finally:
        rest = out.bufs.pop(tid, "")
        out.sinks.pop(tid, None)
        if rest:
            log(rest)


# --- repos, leads, dossiers ------------------------------------------------------------------------------

LANG_MAP = {"TypeScript": "Typescript", "JavaScript": "JavaScript", "Python": "Python", "Go": "Go",
            "Rust": "Rust", "Java": "Java", "Kotlin": "Kotlin", "C#": "C#", "Swift": "Swift",
            "PHP": "PHP", "Ruby": "Ruby", "Dart": "Dart", "Shell": "Shell", "Haskell": "Haskell",
            "PLpgSQL": "SQL", "TSQL": "SQL"}


def _gh_json(path: str):
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise ActionError("github", f"GitHub said no to {path}: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout)


def repo_info(full_name: str, commit: str | None = None, language: str | None = None) -> dict:
    """{full_name, url, commit (40 hex), pinned_url, language, fork, description} from GitHub."""
    if config.FAKE_STUDIO:
        from .fakes import gh
        return gh.repo_info(full_name, commit, language)
    meta = _gh_json(f"repos/{full_name}")
    full = meta["full_name"]
    if not commit:
        commit = _gh_json(f"repos/{full}/commits/{meta['default_branch']}")["sha"]
    elif len(commit) != 40:
        commit = _gh_json(f"repos/{full}/commits/{commit}")["sha"]
    lang = language or LANG_MAP.get(meta.get("language") or "", meta.get("language"))
    return {"full_name": full, "url": meta["html_url"], "commit": commit,
            "pinned_url": f"https://github.com/{full}/tree/{commit}", "language": lang,
            "fork": meta.get("fork"), "description": meta.get("description")}


def _leads_file() -> Path:
    return config.DATA / "scout" / "leads.json"


def load_lead(lead_id: str) -> dict:
    p = _leads_file()
    if not p.exists():
        raise ActionError("not_found", "No leads yet. Run `johto scout run` first.")
    for lead in json.loads(p.read_text()):
        if lead.get("id") == lead_id or str(lead.get("id", "")).startswith(lead_id):
            return lead
    raise ActionError("not_found", f"No lead {lead_id!r}.")


def mark_lead(lead_id: str, status: str, task_name: str | None = None, hidden_reason: str | None = None) -> None:
    p = _leads_file()
    if not p.exists() or not lead_id:
        return
    with open(p.with_name(f".{p.name}.lock"), "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        leads = json.loads(p.read_text())
        for lead in leads:
            if lead.get("id") == lead_id:
                lead["status"] = status
                if task_name:
                    lead["task_name"] = task_name
                if hidden_reason:
                    lead["hidden_reason"] = hidden_reason
        _write_json(p, leads)


def load_opportunity(dossier_id: str, opportunity_id: str | None) -> tuple[dict, dict | None]:
    """(dossier, opportunity) from data/dossiers/ (or the fixtures the app ships)."""
    from .dossier import schema, store
    doc = store.load_dossier(dossier_id)
    if doc is None:
        p = Path(__file__).parent / "web" / "fixtures" / "dossiers" / f"{dossier_id}.json"
        doc = json.loads(p.read_text()) if p.exists() else None
    if doc is None:
        raise ActionError("not_found", f"No dossier {dossier_id}.")
    opp = next((o for o in schema.opportunities(doc) if o.get("id") == opportunity_id), None)
    if opportunity_id and opp is None:
        raise ActionError("not_found", f"Dossier {dossier_id} has no opportunity {opportunity_id}.")
    return doc, opp


def opportunity_brief(doc: dict, opp: dict | None) -> str:
    """Research context for brief.md from a dossier opportunity. AI research, labelled as such."""
    lines = ["", "## Dossier (AI research)", "",
             f"Repo page: /repos/{doc.get('id')}  ·  generated {doc.get('generated_at') or '?'}"]
    if doc.get("headline"):
        lines += [f"Project: {doc['headline']}"]
    if not opp:
        return "\n".join(lines) + "\n"
    lines += ["", f"### {opp.get('title')}" + (f" ({opp['area']})" if opp.get("area") else "")]
    for k, label in (("ambition", "What an expert team would do"), ("why_not_done", "Why it isn't done")):
        if opp.get(k):
            lines += [f"- {label}: {opp[k]}"]
    ev = opp.get("evidence") or []
    if ev:
        lines += ["", "Evidence:"] + [f"- `{e.get('pointer')}` {e.get('fact')}" for e in ev]
    traps = opp.get("where_models_fail") or []
    if traps:
        lines += ["", "Where models fail:"] + [f"- {t}" for t in traps]
    bt = opp.get("bounded_task") or {}
    if bt.get("shape"):
        lines += ["", f"Bounded job (size {bt.get('size') or '?'}): {bt['shape']}"]
    if bt.get("check_in_session"):
        lines += [f"Check in session: {bt['check_in_session']}"]
    os_ = opp.get("one_shot") or {}
    if os_:
        lines += [f"One-shot (estimate): {os_.get('likelihood')} ({os_.get('why')})"]
    up = opp.get("upstream") or {}
    if up.get("related"):
        lines += ["", "Related upstream work (disclose in Difficulty or reviewer notes):"]
        lines += [f"- [{x.get('state')}] {x.get('title')} {x.get('url')}" for x in up["related"]]
    sc = opp.get("self_contained") or {}
    if sc.get("needs"):
        lines += [f"Needs: {', '.join(sc['needs'])}"]
    return "\n".join(lines) + "\n"


# --- slots ----------------------------------------------------------------------------------------------

SLOT_KEYS = ("action", "domain", "codebase_type", "prompt_style", "interaction_mode")


def slot_key(slot: dict) -> str:
    return " | ".join(str(slot.get(k) or "?") for k in SLOT_KEYS)


def _slots_file() -> Path:
    return config.DATA / "slots.json"


def slot_tasks(slot: dict) -> list[dict]:
    """Open tasks for an exact slot, from data/slots.json (as of the last sync)."""
    p = _slots_file()
    if not p.exists():
        return []
    key = slot_key(slot)
    for g in json.loads(p.read_text()).get("groups", []):
        if slot_key(g["slot"]) == key:
            return list(g.get("tasks") or [])
    return []


def next_open_in_group(slot: dict, exclude: set[str], live_checks: int = 3) -> dict | None:
    """The next open task with these seeds, re-checked live in Studio (at most `live_checks` reads)."""
    for t in slot_tasks(slot):
        if t["task_id"] in exclude:
            continue
        if live_checks <= 0:
            return None
        live_checks -= 1
        try:
            live = S().task(t["task_id"])
        except studio.StudioError:
            continue
        if S().status_name(live) == "Ready to Claim":
            return {"task_id": t["task_id"], "task_name": t["task_name"]}
        exclude = exclude | {t["task_id"]}
    return None


def drop_from_slots(task_id: str) -> None:
    """Remove a claimed task from its group in slots.json so counts update before the next sync."""
    p = _slots_file()
    if not p.exists():
        return
    with open(p.with_name(f".{p.name}.lock"), "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        s = json.loads(p.read_text())
        changed = False
        for g in s.get("groups", []):
            before = len(g.get("tasks") or [])
            g["tasks"] = [t for t in g.get("tasks") or [] if t["task_id"] != task_id]
            if len(g["tasks"]) != before:
                g["count"] = max(0, g.get("count", before) - 1)
                changed = True
        if changed:
            s["groups"] = [g for g in s["groups"] if g.get("count")]
            _write_json(p, s)


# --- Studio rechecks ------------------------------------------------------------------------------------

_ME: dict[int, str] = {}


def me() -> str:
    """Austin's Studio user id (read once per process: it never changes)."""
    key = id(base_studio())
    if key not in _ME:
        _ME[key] = S().me()["user_id"]
    return _ME[key]


RUN_NAME = re.compile(r"^(calib|validate)-\d+$")


def check_run_name(run: str | None) -> None:
    if run is not None and not RUN_NAME.match(str(run)):
        raise ActionError("invalid", f"{run!r} isn't a run name (calib-N or validate-N).")


def recheck(task_id: str, expect_status: str, me_id: str | None = None) -> dict:
    """GET the task; refuse unless it is in `expect_status` (and, for Writing, owned by me).

    Raises ActionError("taken") when a Ready to Claim slot was claimed by someone else, else
    ActionError("precondition_failed") with {status, owner}.
    """
    me_id = me_id or me()
    t = S().task(task_id)
    st, owner = S().status_name(t), t.get("owned_by")
    name = t.get("task_name") or task_id
    if st == expect_status and (expect_status != "Writing" or owner == me_id):
        return t
    if expect_status == "Ready to Claim":
        if st == "Writing" and owner == me_id:
            return t  # already ours (a retried claim)
        raise ActionError("taken", f"{name} was just claimed by someone else.",
                          {"status": st, "owner": owner, "alternative": None})
    if st == "Writing" and owner != me_id:
        raise ActionError("precondition_failed", f"{name} is no longer yours in Studio.",
                          {"status": st, "owner": owner})
    raise ActionError("precondition_failed", f"{name} is {st} in Studio, not {expect_status}.",
                      {"status": st, "owner": owner})


def recheck_mine(d: Path) -> dict:
    meta = workspace.load(d)
    need_slot(meta, d)
    return recheck(meta["task_id"], "Writing")


def need_slot(meta: dict, d: Path | None = None) -> None:
    """Refuse a Studio action on a draft (a task folder with no Studio slot yet)."""
    if workspace.is_draft(meta):
        short = d.name if d is not None else workspace.short_name(meta.get("task_name") or "")
        raise ActionError("no_slot", f"{short} has no Studio slot yet. Claim one first, from the task page's Submit "
                          f"step or with `johto claim {short}`.", {"short": short, "slot": meta.get("slot")})


def drafts() -> list[tuple[Path, dict]]:
    """Every draft folder under tasks/ (no Studio slot yet, not dropped), with its task.json."""
    out = []
    for tj in sorted(config.TASKS.glob("*/task.json")):
        try:
            meta = json.loads(tj.read_text())
        except (OSError, ValueError):
            continue
        if workspace.is_draft(meta) and not (meta.get("discarded") or meta.get("discard")):
            out.append((tj.parent, meta))
    return out


def draft_open(meta: dict) -> int:
    """How many open Studio tasks have exactly this draft's seeds, as of the last slots sync."""
    return len(slot_tasks(meta.get("slot") or {}))


# --- claim ----------------------------------------------------------------------------------------------

CLAIM_STEPS = ["recheck", "claiming", "saving_repo", "making_folder", "marking_lead"]


def repo_fields(repo: dict) -> dict:
    fields = {"field_repo_name": repo["full_name"], "field_repo_url": repo["pinned_url"]}
    if repo.get("language") in config.LANGUAGES:
        fields["field_primary_language"] = repo["language"]
    elif repo.get("language"):
        fields["field_primary_language"] = "Other"
        fields["field_primary_language_other"] = repo["language"]
    return fields


def check_repo_allowed(repo: dict) -> list[str]:
    """Refuses SWE-bench repos; returns warnings (fork)."""
    if repo["full_name"].lower() in config.SWE_BENCH_REPOS:
        raise ActionError("invalid", f"{repo['full_name']} is a SWE-bench-Verified repo; Johto doesn't allow it.")
    return [f"{repo['full_name']} is a fork; Johto wants the upstream project."] if repo.get("fork") else []


def claim(task_id: str, repo: dict, lead: dict | None = None, on_step=lambda step: None, *,
          dossier: dict | None = None, opportunity: dict | None = None,
          expect_status: str = "Ready to Claim", label: str | None = None, origin: dict | None = None) -> Path:
    """Claim the slot, save repo name/URL/language in Studio, and make tasks/<short>/.

    `origin` (a claim from the repo brief: a seed or a place, spec 7.2) is stored in task.json as given, with the
    dossier id; a place has no opportunity, so `dossier.opportunity_id` stays null.

    Raises ActionError("taken", details={"alternative": ...}) before any write, or
    ActionError("claim_partial", details={"step", "task_id", "task_name", "task_dir"}) after the claim.
    """
    check_repo_allowed(repo)
    on_step("recheck")
    me_id = me()
    try:
        t = recheck(task_id, expect_status, me_id)
    except ActionError as e:
        if e.code == "taken":
            slot = S().slot_of(S().task(task_id))
            e.details["alternative"] = next_open_in_group(slot, {task_id})
        raise
    slot = S().slot_of(t)
    name = t["task_name"]
    retry = S().status_name(t) == "Writing" and t.get("owned_by") == me_id
    if retry:
        # Already ours: only a retry of this same claim (its repo fields empty or this repo, and no task folder
        # holding another repo) goes on. Anything else would overwrite a task he is working on.
        have = ((t.get("custom_fields") or {}).get("field_repo_name") or "").lower()
        folder = config.TASKS / workspace.short_name(name) / "task.json"
        local = ((json.loads(folder.read_text()).get("repo") or {}).get("full_name") or "").lower() if folder.exists() else ""
        want = repo["full_name"].lower()
        if (have and have != want) or (local and local != want):
            raise ActionError("conflict", f"{name} is already yours in Studio, with {have or local}. Open it instead.",
                              {"task_id": task_id, "repo": have or local})
    if not retry:
        on_step("claiming")
        try:
            S().transition(task_id, config.CLAIM)
        except studio.StudioError:
            live = S().task(task_id)
            if live.get("owned_by") not in (None, me_id):
                raise ActionError("taken", f"{name} was just claimed by someone else.",
                                  {"status": S().status_name(live), "owner": live.get("owned_by"),
                                   "alternative": next_open_in_group(slot, {task_id})}) from None
            raise
    short = workspace.short_name(name)
    partial = {"task_id": task_id, "task_name": name, "short": short}
    on_step("saving_repo")
    try:
        after = S().patch_fields(task_id, repo_fields(repo))
    except Exception as e:
        raise ActionError("claim_partial", f"Claimed {name} in Studio, but saving the repo fields failed: {e}",
                          {**partial, "step": "saving_repo"}) from e
    on_step("making_folder")
    try:
        d = workspace.create(t, slot, repo, lead)
        # What Studio already held (a previous writer's text): sync never pulls it in as Austin's.
        cf = after if isinstance(after, dict) else (t.get("custom_fields") or {})
        _finish_folder(d, lead=lead, dossier=dossier, opportunity=opportunity, label=label, origin=origin,
                       extra={"claimed_at": now_iso(), **_claim_baseline(cf)})
    except Exception as e:
        raise ActionError("claim_partial", f"Claimed {name} in Studio, but making the task folder failed: {e}",
                          {**partial, "step": "making_folder"}) from e
    on_step("marking_lead")
    if lead:
        mark_lead(lead["id"], "claimed", name)
    drop_from_slots(task_id)
    stages.log(d, "claimed", task_id=task_id, task_name=name, repo=repo["full_name"], lead=(lead or {}).get("id"),
               dossier=(dossier or {}).get("id"))
    return d


def _claim_baseline(cf: dict) -> dict:
    return {"studio_at_claim": sync.claim_baseline(cf),
            "feedback_at_claim": sync.h(cf.get("field_review_writer_feedback")) if sync.norm(
                cf.get("field_review_writer_feedback")) else ""}


def _finish_folder(d: Path, *, lead: dict | None, dossier: dict | None, opportunity: dict | None,
                   label: str | None, origin: dict | None, extra: dict) -> None:
    """What a new task folder records beyond workspace.create: the lead's old status, the dossier and origin
    (with the research appended to brief.md), the label, and `extra`."""
    if lead:
        extra["lead_prev_status"] = lead.get("status")
    if dossier:
        extra["dossier"] = {"id": dossier.get("id"), "opportunity_id": (opportunity or {}).get("id")}
        if origin:
            extra["origin"] = {**origin, "dossier_id": dossier.get("id")}
        if "## Dossier (AI research)" not in (d / "brief.md").read_text():
            with open(d / "brief.md", "a") as fh:
                fh.write(opportunity_brief(dossier, opportunity))
    if label:
        extra["label"] = label
    update_meta(d, lambda m: m.update(extra))


# --- drafts: a task folder before any slot ------------------------------------------------------------------
# A draft is prepared end to end without Studio (prompt, run, fields, reviews, grading, kit, checks). When a slot
# with its seeds opens, bind() claims that slot; the web job around it then saves everything in one go.

def draft(slot: dict, repo: dict, lead: dict | None = None, *, dossier: dict | None = None,
          opportunity: dict | None = None, label: str | None = None, origin: dict | None = None) -> Path:
    """Make tasks/<d-xxxxxx>/ with no Studio task behind it. `slot` names all five seeds the task is written for."""
    check_repo_allowed(repo)
    missing = [k for k in SLOT_KEYS if not (slot or {}).get(k)]
    if missing:
        raise ActionError("invalid", f"A draft needs every slot field; missing {', '.join(missing)}.")
    short = workspace.new_draft_short()
    t = {"task_id": None, "task_name": f"Draft {short}"}
    d = workspace.create(t, {k: slot[k] for k in SLOT_KEYS}, repo, lead)
    _finish_folder(d, lead=lead, dossier=dossier, opportunity=opportunity, label=label, origin=origin,
                   extra={"draft": {"at": now_iso()}})
    if lead:
        mark_lead(lead["id"], "claimed", t["task_name"])
    stages.log(d, "drafted", repo=repo["full_name"], lead=(lead or {}).get("id"), dossier=(dossier or {}).get("id"),
               slot=slot_key(slot))
    return d


def shelve_draft(d: Path) -> dict:
    """Delete a draft (his press): the folder moves to tasks/.drafts/<short>-<time>, so nothing is lost. Its lanes
    must be gone first (clean()). The lead goes back to the status it had."""
    meta = workspace.load(d)
    if not workspace.is_draft(meta):
        raise ActionError("invalid", f"{d.name} has a Studio slot; unclaim it in Studio instead.")
    live = [r for r in meta.get("runs") or [] if r.get("container") and not r.get("torn_down_at")
            and not r.get("collected_at")]
    if live:
        raise ActionError("lane_state", f"{d.name} still has a lane that isn't collected. Collect or clean it first.")
    if meta.get("lead_id"):
        mark_lead(meta["lead_id"], meta.get("lead_prev_status") or "new")
    dest = config.TASKS / ".drafts" / f"{d.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta["deleted_at"] = now_iso()
    workspace.save(d, meta)
    d.rename(dest)
    return {"short": d.name, "moved_to": str(dest)}


def open_slot_for(slot: dict, exclude: set[str] | frozenset = frozenset(), live: bool = True) -> dict | None:
    """The first open Studio task with exactly these seeds: the slots file first (rechecked live), then Studio's
    available view. None when nothing is open (or Studio can't be read)."""
    hit = next_open_in_group(slot, set(exclude))
    if hit or not live:
        return hit
    key = slot_key(slot)
    try:
        avail = [t for t in S().list_tasks(view="available")
                 if slot_key(S().slot_of(t)) == key and t["task_id"] not in exclude]
    except studio.StudioError:
        return None
    return {"task_id": avail[0]["task_id"], "task_name": avail[0]["task_name"]} if avail else None


BIND_STEPS = ["recheck", "claiming", "saving_repo", "binding"]


def bind(d: Path, task_id: str | None = None, on_step=lambda step: None) -> dict:
    """Claim an open Studio slot with the draft's seeds and attach it to this folder (the folder keeps its name).

    Raises ActionError("no_open_slot") when nothing is open, ("taken", details={"alternative"}) when the slot went
    to someone else first, ("slot_mismatch") when a named task has other seeds, or ("claim_partial") after the
    claim. Returns {"task_id", "task_name"}."""
    meta = workspace.load(d)
    if not workspace.is_draft(meta):
        raise ActionError("invalid", f"{d.name} already has a Studio slot ({meta.get('task_name')}).")
    slot, repo = meta["slot"], meta["repo"]
    check_repo_allowed(repo)
    on_step("recheck")
    me_id = me()
    if not task_id:
        hit = open_slot_for(slot)
        if not hit:
            raise ActionError("no_open_slot", f"No open slot for {slot_key(slot)} right now. Johto looks again every "
                              "few minutes and says when one opens.", {"slot": slot})
        task_id = hit["task_id"]
    try:
        t = recheck(task_id, "Ready to Claim", me_id)
    except ActionError as e:
        if e.code == "taken":
            e.details["alternative"] = open_slot_for(slot, {task_id})
        raise
    have = S().slot_of(t)
    if slot_key(have) != slot_key(slot):
        raise ActionError("slot_mismatch", f"{t['task_name']} is {slot_key(have)}, not this task's {slot_key(slot)}.",
                          {"task_id": task_id, "slot": have})
    name = t["task_name"]
    retry = S().status_name(t) == "Writing" and t.get("owned_by") == me_id
    if not retry:
        on_step("claiming")
        try:
            S().transition(task_id, config.CLAIM)
        except studio.StudioError:
            live = S().task(task_id)
            if live.get("owned_by") not in (None, me_id):
                raise ActionError("taken", f"{name} was just claimed by someone else.",
                                  {"status": S().status_name(live), "owner": live.get("owned_by"),
                                   "alternative": open_slot_for(slot, {task_id})}) from None
            raise
    partial = {"task_id": task_id, "task_name": name, "short": d.name}
    on_step("saving_repo")
    try:
        after = S().patch_fields(task_id, repo_fields(repo))
    except Exception as e:
        raise ActionError("claim_partial", f"Claimed {name} in Studio for {d.name}, but saving the repo fields "
                          f"failed: {e}", {**partial, "step": "saving_repo"}) from e
    on_step("binding")
    cf = after if isinstance(after, dict) else (t.get("custom_fields") or {})
    at = now_iso()

    def put(m):
        m["task_id"], m["task_name"], m["claimed_at"] = task_id, name, at
        m.update(_claim_baseline(cf))
        m["drafted"] = {**(m.pop("draft", None) or {}), "bound_at": at}
        m.pop("studio", None)
    update_meta(d, put)
    try:
        b = d / "brief.md"
        b.write_text(re.sub(r"^- Studio: no slot yet\..*$", f"- Studio: {studio.task_url(task_id)}", b.read_text(),
                            count=1, flags=re.M))
    except OSError:
        pass
    if meta.get("lead_id"):
        mark_lead(meta["lead_id"], "claimed", name)
    drop_from_slots(task_id)
    stages.log(d, "claimed", task_id=task_id, task_name=name, repo=repo["full_name"], lead=meta.get("lead_id"),
               dossier=(meta.get("dossier") or {}).get("id"), bound=True)
    return {"task_id": task_id, "task_name": name}


def undo_claim(d: Path) -> dict:
    """⌘Z after a claim: unclaim in Studio, put the lead back, and move the folder out of tasks/."""
    meta = workspace.load(d)
    recheck(meta["task_id"], "Writing")
    S().transition(meta["task_id"], config.UNCLAIM)
    if meta.get("lead_id"):
        mark_lead(meta["lead_id"], meta.get("lead_prev_status") or "new")
    dest = config.TASKS / ".unclaimed" / f"{d.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta["unclaimed_at"] = now_iso()
    workspace.save(d, meta)
    d.rename(dest)
    return {"short": d.name, "moved_to": str(dest)}


# --- lanes ----------------------------------------------------------------------------------------------

def _known_containers() -> dict[str, tuple[Path, dict]]:
    out = {}
    for tj in config.TASKS.glob("*/task.json"):
        try:
            meta = json.loads(tj.read_text())
        except (OSError, ValueError):
            continue
        for rec in meta.get("runs", []):
            c = rec.get("container") or (lanes.manifest(tj.parent / rec["dir"]).get("lane") or {}).get("container")
            if c:
                out[c] = (tj.parent, rec)
    return out


def start_lane(d: Path, kind: str, log=lambda line: None, *, again: bool = False, qc: bool = False,
               skip_qc: bool = False, stop_others: bool = False, need_prompt: bool = False) -> Path:
    """A new lane folder for a calibration or validation run (setup only; Claude is started by Austin).

    While a lane slot is free, the new lane takes it and running lanes are left alone (two fit on one
    machine; see lanes.SLOTS). With both slots taken, setting up means stopping a lane, so it refuses while
    any lane's Claude is alive, while a running lane's run isn't collected, or while a prework container the
    app doesn't know is running: the app never stops a lane whose run isn't collected. stop_others (the
    CLI's --stop-others, Austin's explicit flag) skips those checks and stops every running lane.

    A validation lane then passes lane_gate (again, qc, skip_qc and need_prompt go to it). The run's entry
    records the prompt it started with, and a lane_started event is logged.
    """
    if kind not in lanes.KINDS:
        raise ActionError("invalid", f"kind must be one of {', '.join(lanes.KINDS)}.")
    if not stop_others:
        _check_lanes_free()
    gate = lane_gate(d, kind, again=again, qc=qc, skip_qc=skip_qc, need_prompt=need_prompt, log=log)
    if qc and not stop_others:
        _check_lanes_free()   # AutoQC took minutes; a lane may have started meanwhile
    with capture_prints(log):
        try:
            run_dir = L().new_lane(d, kind, stop_others=stop_others)
        except RuntimeError as e:
            raise ActionError("setup_failed", str(e).splitlines()[0], {"log": str(e)}) from e
    _record_lane_start(d, run_dir, kind, gate)
    return run_dir


def _record_lane_start(d: Path, run_dir: Path, kind: str, gate: dict) -> None:
    """prompt_sha and prompt_head on the run (lanes.new_lane records them; the fake lanes don't), and the event."""
    started = lanes.prompt_record(d)
    name = run_dir.name
    rec: dict = {}

    def put(m):
        for r in m.get("runs", []):
            if r.get("dir") == f"runs/{name}":
                for k, v in started.items():
                    r.setdefault(k, v)
                rec.update(r)
    try:
        update_meta(d, put)
    except (OSError, ValueError):
        pass
    stages.log(d, "lane_started", run=name, lane_kind=kind, host=rec.get("host") or "local",
               port_slot=rec.get("port_slot"), prompt_sha=rec.get("prompt_sha"), qc_audit=gate.get("audit_id"),
               again=gate.get("again") or None, skip_qc=gate.get("skip_qc") or None)


def _check_lanes_free() -> None:
    """With both lane slots taken, refuse unless every running lane is idle, collected and known."""
    if config.FAKE_STUDIO is False and lanes.free_slot() is None:
        running = lanes.running_lanes()
        active = [c for c in running if lanes.claude_active(c)]
        if active:
            raise ActionError("lane_busy", "Claude is still running in another lane: " + ", ".join(active),
                              {"active": active})
        known = _known_containers()
        for c in running:
            if c not in known:
                raise ActionError("lane_busy", f"A lane this app doesn't know is running ({c}). Stop it yourself first.",
                                  {"active": [c], "foreign": c})
            td, rec = known[c]
            if not rec.get("collected_at"):
                raise ActionError("lane_state", f"{td.name} {Path(rec['dir']).name} isn't collected yet. Collect it first.",
                                  {"short": td.name, "run": Path(rec["dir"]).name})


def latest_run(d: Path) -> str | None:
    return L().latest_run(d, "validate") or L().latest_run(d, "calib")


def resume_command(d: Path, run: str, rec: dict | None = None) -> str:
    """The exact command that re-enters a run's Claude session. A PC lane: ssh into its folder there and run
    claude-dev (running it again reattaches to the same tmux session; a second lane's ports are already in its
    launcher). A lane on this Mac: `johto resume <task> <run>`."""
    rec = rec or run_record(d, run) or {}
    remote = lanes.remote_of(rec)
    if remote:
        host, rdir = remote
        return f"ssh -t {host} 'cd {rdir}/model_a && ./claude-dev {lanes.LAUNCH_ARGS}'"
    return f"johto resume {d.name} {run}"


def run_record(d: Path, run: str, meta: dict | None = None) -> dict | None:
    meta = meta or workspace.load(d)
    return next((r for r in meta.get("runs", []) if r["dir"] == f"runs/{run}"), None)


def collect(d: Path, run: str | None = None, log=lambda line: None) -> dict:
    """prework collect for one run: diff, transcript and summary, recorded in task.json. Then the "collected"
    event and the writing kit (lanes.after_collect); "kit" in the result is its path, or None."""
    check_run_name(run)
    run = run or latest_run(d)
    if not run:
        raise ActionError("not_found", "No runs yet. Start one with a calibration lane.")
    if not (d / "runs" / run).is_dir():
        raise ActionError("not_found", f"No run {run} in {d.name}.")
    with capture_prints(log):
        try:
            info = L().collect(d, run)
        except RuntimeError as e:
            raise ActionError("collect_failed", str(e)) from e
        if "after" not in info:   # lanes.collect runs it; the fake lanes don't
            info["after"] = lanes.after_collect(d, run, info.get("summary"))
    s = info.get("summary")
    n = len(transcript.find(d / "runs" / run))
    return {**info, "run": run, "transcripts": n, "diff_stat": lanes.diff_stat(info.get("diff")),
            "kit": (info.get("after") or {}).get("kit"),
            "problems": transcript.model_problems(s) if s else ["no transcript found"],
            "note": (f"{n} transcripts in {run}/prework_output; using the newest. More than one usually "
                     "means the lane was reused.") if n > 1 else None}


def _lane_has_work(container: str) -> bool:
    """Does the lane's config dir hold a transcript with a prompt from Austin? (True when unsure.) A lane that
    was set up but never used has nothing to lose."""
    if config.FAKE_STUDIO:
        return True
    key = lanes.lane_key(container)
    if not key:
        return True
    projects = Path(config.PREWORK_HOME) / "cc-config" / f"{key}_model_a" / "projects"
    try:
        for t in projects.glob("*/*.jsonl"):
            if transcript.summarize(t).get("human_turns"):
                return True
    except Exception:
        return True
    return False


def clean(d: Path, run: str | None = None, force: bool = False) -> dict:
    """Remove lane containers, volumes and lane logins. Skips a run whose Claude is still running, and a run
    that isn't collected (unless force) whether or not its container is running: a stopped lane's config dir
    holds the only copy of its transcript. Returns {"removed": [...], "skipped": [{run, reason}]}."""
    check_run_name(run)
    meta = workspace.load(d)
    runs = [run] if run else sorted(p.name for p in (d / "runs").glob("*-*") if p.is_dir() and RUN_NAME.match(p.name))
    if run and not (d / "runs" / run).is_dir():
        raise ActionError("not_found", f"No run {run} in {d.name}.")
    running = set(L().running_lanes())
    removed, skipped = [], []
    for r in runs:
        rec = run_record(d, r, meta) or {}
        c = rec.get("container") or (lanes.manifest(d / "runs" / r).get("lane") or {}).get("container")
        if c and c in running and L().claude_active(c):
            skipped.append({"run": r, "reason": "Claude is still running in this lane"})
            continue
        if c and not (rec.get("collected_at") or rec.get("transcript")) and not force and _lane_has_work(c):
            reason = ("this run isn't collected yet" if c in running
                      else "this run isn't collected yet; start the lane and collect it first")
            skipped.append({"run": r, "reason": reason})
            continue
        removed += [f"{r}: {x}" for x in L().teardown(d, r)]
        update_meta(d, lambda m: [x.update(torn_down_at=now_iso()) for x in m.get("runs", [])
                                  if x["dir"] == f"runs/{r}"])
    if run and skipped:
        raise ActionError("lane_state", f"Can't remove {run}: {skipped[0]['reason']}.", {"run": run})
    return {"removed": removed, "skipped": skipped}


# --- which run is submitted ---------------------------------------------------------------------------


def _run_number(name: str) -> int:
    tail = name.rpartition("-")[2]
    return int(tail) if tail.isdigit() else 0


def run_collected(rec: dict | None) -> bool:
    """A run whose transcript and diff are on this Mac (collect wrote them)."""
    rec = rec or {}
    return bool(rec.get("transcript") or rec.get("collected_at") or (rec.get("auto") or {}).get("collected_at"))


def submission_run(meta: dict, d: Path) -> str | None:
    """The one rule for which run a submission uses. Everything that means "the run" calls it: validation_run,
    the checklist, gists, the Claude-minutes field, `johto where`, and the web app (through submission_run_info).

    The run Austin chose (task.json "submission_run") while its folder exists, else the newest collected
    validation run. A validation run that is still going or isn't collected is never picked by default: its
    transcript and diff aren't here yet. None when there is neither."""
    chosen = meta.get("submission_run")
    if chosen and RUN_NAME.match(str(chosen)) and (Path(d) / "runs" / chosen).is_dir():
        return chosen
    best = None
    for rec in meta.get("runs") or []:
        name = Path(rec.get("dir") or "").name
        if not name.startswith("validate-") or not RUN_NAME.match(name) or not run_collected(rec):
            continue
        if not (Path(d) / "runs" / name).is_dir():
            continue
        if best is None or _run_number(name) > _run_number(best):
            best = name
    return best


def newer_uncollected(meta: dict, d: Path, than: str | None) -> list[str]:
    """Validation runs started after `than` (or any, when None) that aren't collected, oldest first: the lane
    still running, or a run that ended without a collect. The submission doesn't use them until collected."""
    floor = _run_number(than) if than and than.startswith("validate-") else 0
    out = []
    for rec in meta.get("runs") or []:
        name = Path(rec.get("dir") or "").name
        if (name.startswith("validate-") and RUN_NAME.match(name) and _run_number(name) > floor
                and not run_collected(rec) and (Path(d) / "runs" / name).is_dir()):
            out.append(name)
    return sorted(out, key=_run_number)


def submission_run_info(meta: dict, d: Path) -> dict:
    """{run, by, collected, newer}: the submission run (submission_run), how it was picked ("chosen" when
    Austin picked it, "latest" for the newest collected validation run, None), and newer validation runs that
    aren't collected yet. For the web app's task data and `johto where`."""
    run = submission_run(meta, d)
    by = None if run is None else ("chosen" if meta.get("submission_run") == run else "latest")
    rec = run_record(d, run, meta) if run else None
    return {"run": run, "by": by, "collected": run_collected(rec),
            "newer": newer_uncollected(meta, d, run if (run or "").startswith("validate-") else None)}


def validation_run(d: Path, run: str | None = None) -> tuple[str | None, dict | None]:
    """The run being submitted: `run` if given, else submission_run (the chosen run, else the newest
    collected validation run)."""
    check_run_name(run)
    meta = workspace.load(d)
    if run and not (d / "runs" / run).is_dir():
        run = None
    run = run or submission_run(meta, d)
    if not run:
        return None, None
    return run, run_record(d, run, meta)


def set_submission_run(d: Path, run: str | None) -> dict:
    """Choose the run whose transcript and diff are submitted (any collected run). None goes back to the
    default (the latest validation run)."""
    if run is not None:
        if not RUN_NAME.match(str(run)) or not (d / "runs" / run).is_dir():
            raise ActionError("not_found", f"No run {run} in {d.name}.")
        rec = run_record(d, run)
        if not rec or not rec.get("transcript"):
            raise ActionError("lane_state", f"{run} isn't collected yet. Collect it first.", {"run": run})

    def put(m):
        if run is None:
            m.pop("submission_run", None)
        else:
            m["submission_run"] = run
    update_meta(d, put)
    cur, rec = validation_run(d)
    return {"submission_run": run, "run": cur, "collected": bool((rec or {}).get("transcript"))}


def runs_overview(d: Path) -> list[dict]:
    meta = workspace.load(d)
    cur, _ = validation_run(d)
    out = []
    for rec in meta.get("runs", []):
        name = Path(rec["dir"]).name
        out.append({"run": name, "kind": rec.get("kind"), "collected": bool(rec.get("transcript")),
                    "active_minutes": workspace.run_minutes(rec, d), "human_turns": rec.get("human_turns"),
                    "submitted": name == cur})
    return out


# --- checks ---------------------------------------------------------------------------------------------

def run_problems(d: Path, run: str | None = None) -> list[tuple]:
    """Checks on the submitted run: collected, model and effort, async = one prompt, prompt matches."""
    meta = workspace.load(d)
    sub = workspace.parse_submission(d)
    asked = run
    run, rec = validation_run(d, run)
    out = []
    waiting = newer_uncollected(meta, d, run if (run or "").startswith("validate-") else None)
    if not run and waiting:
        out.append((lint.ERROR, "Runs", f"{waiting[-1]} isn't collected yet (collect {waiting[-1]})"))
    elif not run:
        out.append((lint.ERROR, "Runs", "no validation run yet (start a validation lane)"))
    elif not (rec or {}).get("transcript"):
        out.append((lint.ERROR, "Runs", f"{run} not collected yet (collect {run})"))
    else:
        s = transcript.summarize(Path(rec["transcript"]))
        for p in transcript.model_problems(s):
            out.append((lint.ERROR, "Runs", f"{run}: {p}"))
        mode = meta["slot"]["interaction_mode"]
        if mode.startswith("Async") and s["human_turns"] > 1:
            out.append((lint.ERROR, "Runs", f"async slot but {s['human_turns']} prompts in {run}; async is one prompt"))
        prompt = sub["sections"].get("Opening Prompt")
        if s["first_prompt"] and prompt and s["first_prompt"].strip() != prompt.strip():
            out.append((lint.WARN, "Runs", f"{run}'s first prompt differs from your Opening Prompt"))
        if s["active_minutes"] < 8 and (sub["ratings"].get("one-shot") or "").lower() == "no":
            out.append((lint.WARN, "Runs", f"Claude worked {s['active_minutes']} minutes; reviewers have dropped tasks "
                                           "Opus finishes in about 5 minutes, and short runs with real misses have "
                                           "passed"))
        if waiting and not asked and meta.get("submission_run") != run:
            out.append((lint.WARN, "Runs", f"{waiting[-1]} is newer but not collected; the submission uses {run} "
                                           f"until it is (collect {waiting[-1]}, or choose a run with "
                                           f"`johto submission-run {d.name} <run>`)"))
    return out


def check(d: Path, run: str | None = None) -> list[tuple]:
    """Local lint plus run checks: [(level, field, message)]. Flags only, never wording."""
    meta = workspace.load(d)
    sub = workspace.parse_submission(d)
    return lint.run_all(meta, sub) + run_problems(d, run) + saved_text_flags(d)


def saved_text_flags(d: Path) -> list[tuple]:
    """lint.check_saved_fields: Studio's saved text cut off, or shorter than the local copy. Never raises."""
    saved = getattr(lint, "check_saved_fields", None)
    if saved is None:
        return []
    try:
        return list(saved(d))
    except Exception as e:
        return [(lint.WARN, "Studio", f"the saved-text check didn't run: {e}")]


def fields(d: Path, run: str | None = None) -> dict:
    """The Studio custom_fields this task folder holds right now."""
    meta = workspace.load(d)
    sub = workspace.parse_submission(d)
    _, rec = validation_run(d, run)
    return workspace.to_fields(meta, sub, workspace.run_minutes(rec, d))


def push(d: Path, run: str | None = None, checked: bool = False, task: dict | None = None) -> dict:
    """Save every field to Studio (patch_fields verifies the seeds are unchanged), then record hashes.
    `checked`: the caller re-read the task a moment ago (one recheck per composite job); pass that read as
    `task` so Studio's current text is compared without another GET."""
    meta = workspace.load(d)
    need_slot(meta, d)
    t = task or (recheck(meta["task_id"], "Writing") if not checked else S().task(meta["task_id"]))
    # Edits made in Studio come in first, so saving never overwrites them.
    pulled = sync.pull(d, t.get("custom_fields") or {}, update_meta=update_meta)
    if pulled["conflicts"]:
        names = ", ".join(sync.FIELD_NAMES[k] for k in pulled["conflicts"])
        raise ActionError("sync_conflict", f"Changed both here and in Studio since the last save: {names}. "
                          "Keep one version in both places, then save again.",
                          {"fields": {sync.FIELD_NAMES[k]: v for k, v in pulled["conflicts"].items()}})
    if pulled["pulled"]:
        stages.log(d, "studio_pulled", fields=pulled["pulled"])
    f = fields(d, run)
    after = S().patch_fields(meta["task_id"], f)
    at = now_iso()
    update_meta(d, lambda m: m.update(pushed={"at": at, "fields": {k: sha12(v) for k, v in f.items()}}))
    sync.mark_pushed(d, f, update_meta)
    save_studio_snapshot(d, after if isinstance(after, dict) else None)
    stages.log(d, "studio_pushed", fields=sorted(f), run=run)
    return {"fields": f, "saved": len(f), "at": at, "pulled": pulled["pulled"]}


def pull_from_studio(d: Path, write_section=None, write_ratings=None) -> dict:
    """Refresh the task's Studio status and, while it is mine and in Writing, bring Studio's edits in. A draft
    has nothing in Studio to read: {"status": "Draft", ...}."""
    if workspace.is_draft(workspace.load(d)):
        return {"status": "Draft", "pulled": [], "conflicts": {}}
    st = studio_status(d)
    if st["status"] != "Writing" or not st["mine"]:
        return {"status": st["status"], "pulled": [], "conflicts": {}}
    res = sync.pull(d, st["fields"], write_section, write_ratings, update_meta=update_meta)
    if res["pulled"]:   # only when something came in: the web app's poller pulls every few minutes
        stages.log(d, "studio_pulled", fields=res["pulled"])
    return {"status": st["status"], "pulled": res["pulled"],
            "conflicts": {sync.FIELD_NAMES[k]: v["why"] for k, v in res["conflicts"].items()}}


GIST_SOURCES = {"conversation_log": "transcript", "final_output": "diff"}


def file_sha(path) -> str | None:
    """sha256 (16 hex) of a file's bytes, or None when it can't be read."""
    if not path:
        return None
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError:
        return None


def gist_state(d: Path, run: str | None = None, meta: dict | None = None) -> dict:
    """Which gists the submission run needs uploaded: {run, rec, gists, need: [key], why: {key: reason}, sha}.

    A gist link counts only for the run it was made from and the exact file it holds: links are bound to the
    sha of the transcript and diff at upload time, so a run resumed and collected again gets new gists.
    Links saved before johto recorded a sha are uploaded again once (what they hold can't be told)."""
    meta = meta or workspace.load(d)
    run, rec = validation_run(d, run)
    g = dict(meta.get("gists") or {})
    shas = {k: file_sha((rec or {}).get(src)) for k, src in GIST_SOURCES.items()}
    old = g.get("sha") or {}
    need, why = [], {}
    for k in GIST_SOURCES:
        if not g.get(k):
            reason = None if not g.get("run") or g["run"] == run else f"the gists are for {g['run']}"
            why[k] = reason or "not uploaded yet"
        elif g.get("run") and g["run"] != run:
            why[k] = f"the gists are for {g['run']}"
        elif not old.get(k):
            why[k] = "uploaded before johto recorded which file it holds"
        elif shas[k] and old[k] != shas[k]:
            why[k] = f"{run}'s {GIST_SOURCES[k]} changed since the upload"
        else:
            continue
        need.append(k)
    return {"run": run, "rec": rec, "gists": g, "need": need, "why": why, "sha": shas}


def upload_gists(d: Path, run: str | None = None, on_step=lambda step: None) -> dict:
    """Secret gists for the submitted run's transcript and diff. Uploads what is missing, what belongs to a
    different run than the one now chosen, and what no longer matches the run's files (gist_state)."""
    meta = workspace.load(d)
    gs = gist_state(d, run, meta)
    run, rec = gs["run"], gs["rec"]
    if not rec or not rec.get("transcript") or not rec.get("diff"):
        raise ActionError("lane_state", "Need a collected run (transcript and diff) before uploading gists.",
                          {"run": run})
    g = gs["gists"]
    if g.get("run") and g["run"] != run:
        g = {}
    need = gs["need"]
    if not need:
        return {"gists": g, "uploaded": [], "run": run}
    sha = dict(g.get("sha") or {})
    on_step("gh_account")
    acct = G().active_account()
    if acct != gists.GH_ACCOUNT:
        raise ActionError("gh_account", f"gh is logged in as {acct!r}; switch with `gh auth switch -u {gists.GH_ACCOUNT}`.",
                          {"account": acct, "want": gists.GH_ACCOUNT})
    on_step("gists")
    # The Studio task's code, not the folder: a task started as a draft keeps its d-xxxxxx folder after claiming.
    short = workspace.short_name(meta.get("task_name")) if meta.get("task_id") else d.name
    try:
        if "conversation_log" in need:
            g["conversation_log"] = G().create(rec["transcript"], f"{short}-conversation.jsonl",
                                               f"Johto {meta['task_name']} conversation log")
            sha["conversation_log"] = gs["sha"]["conversation_log"]
        if "final_output" in need:
            g["final_output"] = G().create(rec["diff"], f"{short}-final.diff", f"Johto {meta['task_name']} final output")
            sha["final_output"] = gs["sha"]["final_output"]
    except RuntimeError as e:
        raise ActionError("github", str(e)) from e
    finally:
        g["run"] = run
        g["sha"] = {k: v for k, v in sha.items() if v}
        g["uploaded_at"] = now_iso()
        update_meta(d, lambda m: m.update(gists=g))
    return {"gists": g, "uploaded": need, "run": run, "why": {k: gs["why"][k] for k in need}}


# --- AutoQC ---------------------------------------------------------------------------------------------

DIM_FIELD = {
    "Prompt Style Fit": "opening_prompt", "Nothing Private Leaks": "opening_prompt",
    "Doesn't Solve It": "opening_prompt", "No Formatting Tells": "opening_prompt",
    "Task Scope": "goal", "High-Level Task Goal": "goal", "Decision Points": "decision_points",
    "Difficulty & Codebase Understanding": "codebase", "Seeded Action Match": "opening_prompt",
}


def qc_summary(audit: dict) -> dict:
    """{status, counts: {pass, fail, neutral}, dims: [{section, dimension, status, justification, field_key}]}"""
    out = audit.get("outcome") or {}
    dims, counts = [], {"pass": 0, "fail": 0, "neutral": 0}
    for sec in out.get("sections", []) or []:
        name = sec.get("section") or sec.get("name") or sec.get("title") or ""
        for dd in sec.get("dimensions", []) or []:
            st = dd.get("status")
            if st in counts:
                counts[st] += 1
            dims.append({"section": name, "dimension": dd.get("dimension"), "status": st,
                         "justification": dd.get("justification") or dd.get("analysis") or "",
                         "field_key": DIM_FIELD.get(dd.get("dimension"))})
    status = audit.get("status") or "running"
    if status not in ("completed", "failed", "cancelled"):
        status = "running"
    return {"status": status, "counts": counts, "dims": dims, "failure_reason": audit.get("failure_reason")}


def start_qc(d: Path, spec: str = "AutoQC", checked: bool = False) -> str | None:
    """Start AutoQC (or Codebase Check) in Studio; record it in task.json; return the audit id."""
    if spec not in config.QC_SPECS:
        raise ActionError("invalid", f"spec must be one of {', '.join(config.QC_SPECS)}.")
    meta = workspace.load(d)
    need_slot(meta, d)
    # Studio's prompt as the audit starts, so lane_gate can tell which prompt it checked. `checked` callers
    # read or saved the task a moment ago, and that left the snapshot current.
    t = recheck(meta["task_id"], "Writing") if not checked else None
    cf = (t or {}).get("custom_fields") if t else (studio_snapshot(d) or {}).get("fields")
    prompt = (cf or {}).get("field_opening_prompt")
    res = S().run_qc(meta["task_id"], spec) or {}
    aid = res.get("qc_audit_id") or res.get("id")
    entry = {"audit_id": aid, "spec": spec, "started_at": now_iso(), "status": "running",
             "counts": {"pass": 0, "fail": 0, "neutral": 0}, "dims": [], "dedup": res.get("dedup"),
             "prompt_sha": lanes.prompt_sha(prompt) if prompt else None}
    update_meta(d, lambda m: m.setdefault("qc", []).append(entry))
    return aid


def record_qc(d: Path, audit_id: str, audit: dict) -> dict:
    summ = qc_summary(audit)
    ended: dict = {}

    def put(m):
        for q in m.setdefault("qc", []):
            if q.get("audit_id") == audit_id:
                if q.get("status") == "running" and summ["status"] != "running":
                    ended.update(spec=q.get("spec"), prompt_sha=q.get("prompt_sha"))
                q.update(status=summ["status"], counts=summ["counts"], dims=summ["dims"],
                         finished_at=now_iso() if summ["status"] != "running" else None,
                         failure_reason=summ.get("failure_reason"))
    update_meta(d, put)
    if ended.get("spec") == "AutoQC":   # once per audit, when it finishes
        failed = prompt_failures(summ["dims"])
        stages.log(d, "prompt_qc", audit_id=audit_id, status=summ["status"], prompt_sha=ended.get("prompt_sha"),
                   prompt=("fail" if failed else "pass") if summ["status"] == "completed" else None,
                   failed=[x["dimension"] for x in failed] or None)
    return summ


def fail_qc(d: Path, audit_id: str, reason: str) -> None:
    """The app stopped waiting on an audit: its checklist row offers Rerun instead of "running" forever."""
    def put(m):
        for q in m.get("qc") or []:
            if q.get("audit_id") == audit_id and q.get("status") == "running":
                q.update(status="failed", failure_reason=reason, finished_at=now_iso())
    update_meta(d, put)


def poll_qc(d: Path, audit_id: str, every: float = 15, timeout: float = 900, sleep=time.sleep) -> dict:
    """Poll one audit until it finishes (or times out); keep task.json's qc entry current."""
    end = time.time() + timeout
    while True:
        audit = S().qc_audit(audit_id)
        summ = record_qc(d, audit_id, audit)
        if summ["status"] != "running" or time.time() > end:
            return {**summ, "audit": audit}
        sleep(every)


def latest_qc(meta: dict, spec: str = "AutoQC") -> dict | None:
    return next((q for q in reversed(meta.get("qc") or []) if q.get("spec") == spec), None)


# --- validation gates -----------------------------------------------------------------------------------
# Two checks before a validation lane, from real incidents: a second validation run because AutoQC asked
# for a prompt change after the first (t4sk0003), and a validation run that repeated the earlier run's
# prompt by mistake (t4sk0004 validate-2). Calibration lanes have no gate.

PROMPT_DIMS_FALLBACK = ("Prompt Style Fit", "Nothing Private Leaks", "Doesn't Solve It", "No Formatting Tells",
                        "AI Detection")
PROMPT_FIELD = "field_opening_prompt"
QC_GATE_POLL_S = 15
QC_GATE_TIMEOUT_S = 900


def prompt_dimensions() -> tuple[str, ...]:
    """The AutoQC dimensions that judge the opening prompt: johto.rules' list, else the built-in one."""
    try:
        rules = importlib.import_module(f"{__package__}.rules")
        dims = tuple(getattr(rules, "PROMPT_QC_DIMENSIONS", ()) or ())
    except Exception:   # rules is optional; any trouble there falls back
        dims = ()
    return dims or PROMPT_DIMS_FALLBACK


# AutoQC dimensions that score the whole task (every field), not the prompt alone. A failure there blocks a
# validation lane only when its justification puts it on the opening prompt; a flag caused by the goal or the
# notes is a warning, because those are written after the run and fixing them never needs another run.
WHOLE_TASK_DIMS = ("AI Detection",)
_PROMPT_REF = re.compile(r"\bopening[ _-]?prompt\b|\bfield_opening_prompt\b|\bprompt\b", re.I)
_PROMPT_CLEARED = re.compile(r"\b(?:is|was|are|were)?\s*not\s+(?:flagged|ai|likely|detected|generated|an issue)\b|"
                             r"\bisn't\s+(?:flagged|ai|likely)\b|\bhuman[- ]written\b|\breads?\s+(?:as|like)\s+(?:a\s+)?human\b|"
                             r"\b(?:passes|passed|fine|clean|unflagged)\b|\bscored?\s+low\b|\blow\s+(?:ai|score|likelihood)\b", re.I)


def points_at_prompt(justification: str | None) -> bool:
    """Does a whole-task dimension's justification blame the opening prompt? True when a sentence names the
    prompt without clearing it ("the opening prompt reads as human" clears it). An empty justification can't
    say, so it is False."""
    for sent in re.split(r"(?<=[.;!?])\s+|\n+", justification or ""):
        if _PROMPT_REF.search(sent) and not _PROMPT_CLEARED.search(sent):
            return True
    return False


def prompt_failures(dims: list[dict]) -> list[dict]:
    """The failed dimensions in a qc_summary dims list that block a validation lane: the prompt dimensions,
    with a whole-task one (AI Detection) only when its justification points at the opening prompt."""
    names = set(prompt_dimensions())
    out = []
    for x in dims or []:
        if x.get("dimension") not in names or x.get("status") != "fail":
            continue
        if x["dimension"] in WHOLE_TASK_DIMS and not points_at_prompt(x.get("justification")):
            continue
        out.append(x)
    return out


def prompt_warnings(dims: list[dict]) -> list[dict]:
    """Failed whole-task dimensions that don't point at the opening prompt: shown as warnings, never blocking."""
    return [x for x in dims or [] if x.get("dimension") in WHOLE_TASK_DIMS and x.get("status") == "fail"
            and not points_at_prompt(x.get("justification"))]


def _log_prompt_warnings(dims: list[dict], log) -> None:
    for x in prompt_warnings(dims):
        why = _one_line(x.get("justification") or "", 140)
        log(f"warning: AutoQC's {x['dimension']} flagged the task{f' ({why})' if why else ''}. It scores every field "
            "and doesn't name the opening prompt here, so the lane starts. Check the goal and notes before you submit.")


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _when(iso: str | None) -> str:
    """'22:10' today, else 'Oct 31 22:10', in local time."""
    t = _ts(iso)
    if t is None:
        return "at an unknown time"
    dt = datetime.fromtimestamp(t)
    return f"{dt:%H:%M}" if dt.date() == datetime.now().date() else f"{dt:%b} {dt.day} {dt:%H:%M}"


def _one_line(text: str, limit: int = 160) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "..."


def _local_prompt(d: Path) -> str:
    return workspace.parse_submission(d)["sections"].get("Opening Prompt", "").strip()


PROMPT_OVERLAP_MSG = ("Your opening prompt repeats AI text word for word. Reword it or mark it as your own words, "
                      "then start the run.")


def _guard_prompt_overlap(d: Path) -> None:
    """An unaccepted red overlap in the opening prompt blocks the validation run and the prompt's save to Studio
    (the same rule as Save to Studio): the prompt reaches Studio and Claude only in his own words. Fails closed
    when the guard can't run."""
    text = workspace.parse_submission(d)["sections"].get("Opening Prompt", "")
    if not text.strip():
        return
    try:
        from .web.guard import overlap
        red = overlap.unaccepted_red(d, {"opening_prompt": text}).get("opening_prompt")
    except Exception as e:
        raise ActionError("guard_missing", f"The overlap check didn't run ({e}), so the run waits until it does.") from e
    if red:
        raise ActionError("blocked_overlap", PROMPT_OVERLAP_MSG, {"overlaps": red, "fields": ["opening_prompt"]})


def _run_prompt(d: Path, rec: dict) -> tuple[str | None, str | None]:
    """(sha, head) of the prompt a run used: its transcript's first prompt when the run folder holds one
    (what Claude really got), else what was recorded at lane start. head is set only when that is all there is."""
    logs = transcript.find(d / rec["dir"])
    if logs:
        try:
            first = transcript.summarize(logs[-1]).get("first_prompt")
        except (OSError, ValueError):
            first = None
        if first:
            return lanes.prompt_sha(first), None
    if rec.get("prompt_sha"):
        return rec["prompt_sha"], None
    if rec.get("prompt_head"):
        return None, " ".join(rec["prompt_head"].split())
    return None, None


def same_prompt_runs(d: Path, prompt: str, meta: dict | None = None) -> list[dict]:
    """Earlier validation runs that used this prompt: [{run, created_at, torn_down, by, host, rec}], oldest first.
    host is the PC's ssh name for a PC run, else None."""
    meta = meta or workspace.load(d)
    sha, flat = lanes.prompt_sha(prompt), " ".join(prompt.split())
    out = []
    for rec in meta.get("runs") or []:
        if rec.get("kind") != "validate" or not rec.get("dir"):
            continue
        rsha, head = _run_prompt(d, rec)
        if rsha == sha or (head and flat.startswith(head)):
            remote = lanes.remote_of(rec)
            out.append({"run": Path(rec["dir"]).name, "created_at": rec.get("created_at"),
                        "torn_down": bool(rec.get("torn_down_at")), "by": "start" if head else "prompt",
                        "host": remote[0] if remote else None, "rec": rec})
    return out


def _review_note(d: Path, run: str) -> str:
    """" It has a GPT Pro review (saved HH:MM)." when one is saved for the run, else ""."""
    try:
        from . import reviews
        r = reviews.latest(d, run)
    except Exception:
        return ""
    if not r:
        return ""
    turn = f" of turn {r['turn']}" if r.get("turn") else ""
    return f" It has a saved review{turn} ({_when(r.get('saved_at'))}): `johto review show {d.name} {Path(r['name']).stem}`."


def _same_prompt_error(d: Path, hits: list[dict]) -> ActionError:
    last = hits[-1]
    names = " and ".join(h["run"] for h in hits)
    how = " (its first 200 characters match)" if last["by"] == "start" else ""
    who = "it" if len(hits) == 1 else last["run"]
    where = f" on {last['host']}" if last.get("host") else ""
    msg = f"{names} already ran this prompt{how}; {who} started {_when(last['created_at'])}{where}."
    msg += _review_note(d, last["run"])
    if not last["torn_down"]:
        msg += f" To steer it instead: `{resume_command(d, last['run'], last.get('rec'))}`."
    msg += f" To start a new run with the same prompt anyway: `johto lane {d.name} validate --again`."
    return ActionError("same_prompt", msg, {"runs": [{k: v for k, v in h.items() if k != "rec"} for h in hits],
                                            "run": last["run"]})


def _qc_error(d: Path, reason: str, message: str, **details) -> ActionError:
    """message names the way out as {fix}. A plain replace, not format(): it may quote AutoQC's text."""
    fix = f"`johto lane {d.name} validate --qc`"
    return ActionError("prompt_qc", message.replace("{fix}", fix), {"reason": reason, "fix": fix, **details})


def _prompt_history(task_id: str) -> list[tuple[int, float | None, str]]:
    """[(version, updated_at, prompt sha)] oldest first, from Studio's task history (one row per version).
    Empty when Studio has no such history (the fake keeps transitions only)."""
    try:
        h = S().history(task_id)
    except studio.StudioError:
        return []
    rows = h.get("history", []) if isinstance(h, dict) else h
    out = []
    for r in rows or []:
        if isinstance(r, dict) and isinstance(r.get("custom_fields"), dict) and r.get("version") is not None:
            out.append((int(r["version"]), _ts(r.get("updated_at")),
                        lanes.prompt_sha(r["custom_fields"].get(PROMPT_FIELD))))
    return sorted(out, key=lambda x: x[0])


def _audit_saw_prompt(entry: dict, audit: dict | None, sha: str, history) -> bool | None:
    """Did this AutoQC audit check the prompt whose sha is `sha`? None when it can't be told.

    In order: the prompt this app recorded when it started the audit (start_qc); the audited task version
    (subject_version) in Studio's history; else whether the prompt took its current text, by that history,
    before the audit was created."""
    if entry.get("prompt_sha"):
        return entry["prompt_sha"] == sha
    rows = history()
    ver = (audit or {}).get("subject_version")
    if ver is not None:
        for v, _, s in rows:
            if v == int(ver):
                return s == sha
    since = None
    for _, at, s in rows:   # the start of the last stretch of versions holding this prompt
        since = (since if since is not None else at) if s == sha else None
    created = _ts((audit or {}).get("created_at")) or _ts(entry.get("started_at"))
    if since is not None and created is not None:
        return created >= since
    return None


def prompt_qc_status(d: Path, prompt: str) -> dict:
    """Has AutoQC passed this opening prompt? {"ok", "reason", "audit_id", "created_at", "failed", "dims"}.

    The rule: take the latest completed AutoQC audit of the task (Studio's list, plus the ones this app
    started). It counts only when it is known to have checked this exact prompt (see _audit_saw_prompt);
    otherwise there has been no AutoQC since the prompt changed. A counted audit passes when no prompt
    dimension failed. reason: "passed", "failed", "stale" (no audit of this prompt) or "running"."""
    meta = workspace.load(d)
    tid = meta["task_id"]
    sha = lanes.prompt_sha(prompt)
    local = {q["audit_id"]: q for q in meta.get("qc") or [] if q.get("spec") == "AutoQC" and q.get("audit_id")}
    remote: dict[str, dict] = {}
    try:
        for a in S().qc_audits(tid) or []:
            aid = a.get("qc_audit_id") or a.get("id")
            if aid and (a.get("qc_spec_id") == config.QC_SPECS["AutoQC"] or a.get("spec") == "AutoQC"):
                remote[aid] = a
    except studio.StudioError:
        pass   # what this app recorded still counts
    rows = []
    for aid in set(local) | set(remote):
        a, q = remote.get(aid), local.get(aid) or {}
        status = (a or {}).get("status") or q.get("status")
        created = (a or {}).get("created_at") or q.get("started_at")
        rows.append((_ts(created) or 0.0, aid, a, q, status, created))
    rows.sort(key=lambda r: r[0])
    done = [r for r in rows if r[4] == "completed"]
    running = [r for r in rows if r[4] not in ("completed", "failed", "cancelled") and (not done or r[0] > done[-1][0])]
    hist: list | None = None

    def history():
        nonlocal hist
        if hist is None:
            hist = _prompt_history(tid)
        return hist
    base = {"ok": False, "audit_id": None, "created_at": None, "failed": [], "dims": [],
            "running": running[-1][5] if running else None}
    if not done:
        return {**base, "reason": "running" if running else "stale"}
    _, aid, a, q, _, created = done[-1]
    out = {**base, "audit_id": aid, "created_at": created}
    if _audit_saw_prompt(q, a, sha, history) is not True:
        return {**out, "reason": "running" if running else "stale"}
    dims = q.get("dims") if q.get("status") == "completed" else None
    if not dims:
        try:
            dims = qc_summary(a if a and a.get("outcome") else S().qc_audit(aid))["dims"]
        except studio.StudioError:
            dims = []
    failed = prompt_failures(dims)
    return {**out, "ok": not failed, "reason": "failed" if failed else "passed", "failed": failed, "dims": dims,
            "warnings": prompt_warnings(dims)}


def _studio_prompt(d: Path, log) -> str | None:
    """Studio's opening prompt now (GET the task). None when Studio is off for this copy of the data."""
    try:
        return studio_status(d)["fields"].get(PROMPT_FIELD) or ""
    except config.RealWriteRefused:
        log("Studio is off for this copy of the data, so the prompt AutoQC check is skipped.")
        return None
    except studio.StudioError as e:
        raise ActionError("prompt_qc", f"Couldn't read Studio to check the prompt's AutoQC ({studio_error_dict(e)['message']}). "
                          f"Open the Studio tab and try again, or start without the check: "
                          f"`johto lane {d.name} validate --skip-qc`.", {"reason": "unreadable"}) from e


def _take_studio_prompt(d: Path, text: str) -> None:
    """Studio's prompt into submission.md (only Studio's copy changed since they last matched)."""
    sync.write_section_file(d, "Opening Prompt", text)

    def put(m):
        m.setdefault("synced", {})[PROMPT_FIELD] = sync.h(text)
        conflicts = (m.get("sync") or {}).get("conflicts") or {}
        conflicts.pop("Opening Prompt", None)
    update_meta(d, put)
    (d / "app").mkdir(exist_ok=True)
    with open(d / "app" / "authorship.jsonl", "a") as fh:
        fh.write(json.dumps({"at": now_iso(), "field": "Opening Prompt", "event": "pulled_from_studio",
                             "chars": len(text)}) + "\n")
    stages.log(d, "studio_pulled", fields=["Opening Prompt"])


def _save_prompt_only(d: Path, text: str) -> None:
    """Save only the opening prompt to Studio. The other fields are left as Studio has them."""
    meta = workspace.load(d)
    after = S().patch_fields(meta["task_id"], {PROMPT_FIELD: text})
    sync.mark_pushed(d, {PROMPT_FIELD: text}, update_meta)

    def put(m):
        if (m.get("pushed") or {}).get("fields") is not None:
            m["pushed"]["fields"][PROMPT_FIELD] = sha12(text)
        conflicts = (m.get("sync") or {}).get("conflicts") or {}
        conflicts.pop("Opening Prompt", None)
    update_meta(d, put)
    save_studio_snapshot(d, after if isinstance(after, dict) else None)
    stages.log(d, "studio_pushed", fields=[PROMPT_FIELD])


def _match_prompt_with_studio(d: Path, log) -> str:
    """--qc, first step: Studio's edits come in, then the two copies of the opening prompt are made the same
    without ever saving over text Austin wrote in Studio. Returns the prompt both now hold."""
    recheck_mine(d)
    res = pull_from_studio(d)
    if res["pulled"]:
        log(f"Brought in from Studio: {', '.join(res['pulled'])}")
    for k, why in res["conflicts"].items():
        if k != "Opening Prompt":
            log(f"  {k}: {why}; left both copies alone")
    rem = ((studio_snapshot(d) or {}).get("fields") or {}).get(PROMPT_FIELD) or ""
    loc = _local_prompt(d)
    if sync.norm(rem) == sync.norm(loc):
        return loc
    meta = workspace.load(d)
    base = {**((meta.get("pushed") or {}).get("fields") or {}), **(meta.get("synced") or {})}.get(PROMPT_FIELD)
    at_claim = meta.get("studio_at_claim")
    foreign = base is None and at_claim is not None and sync.h(rem) == at_claim.get(PROMPT_FIELD)
    if foreign and not sync.norm(loc):
        raise ActionError("missing_fields", "Studio's opening prompt is a previous writer's text. Write your own "
                          "first, in Studio or submission.md.", {"fields": ["opening_prompt"]})
    if not sync.norm(rem) or foreign:
        how = "Studio had none" if not sync.norm(rem) else "Studio held a previous writer's"
        log(f"Saving your opening prompt to Studio ({how}).")
        _save_prompt_only(d, loc)
        return loc
    if not sync.norm(loc) or (base is not None and sync.h(loc) == base):
        log("Took Studio's opening prompt into submission.md (only Studio's copy changed).")
        _take_studio_prompt(d, rem)
        return _local_prompt(d)
    if base is not None and sync.h(rem) == base:
        log("Saving your opening prompt to Studio (only this copy changed). Other fields are left alone.")
        _save_prompt_only(d, loc)
        return loc
    raise ActionError("sync_conflict", "The opening prompt differs here and in Studio, and it isn't clear which one "
                      "is newer. Put the version you want in both places, then run "
                      f"`johto lane {d.name} validate --qc` again.",
                      {"fields": {"Opening Prompt": {"local": loc, "studio": rem}}})


def _log_prompt_dims(dims: list[dict], log) -> None:
    names = prompt_dimensions()
    warned = {id(x) for x in prompt_warnings(dims)}
    for x in sorted((x for x in dims if x.get("dimension") in names), key=lambda x: names.index(x["dimension"])):
        mark = "warn" if id(x) in warned else "FAIL" if x["status"] == "fail" else x["status"] or "?"
        log(f"  {mark:5} {x['dimension']}")
        if x.get("status") == "fail" and x.get("justification"):
            log(f"        {_one_line(x['justification'])}")


def _failed_error(d: Path, failed: list[dict], audit_id: str | None, created_at: str | None) -> ActionError:
    what = "; ".join(f"{x['dimension']}: {_one_line(x.get('justification') or 'failed')}" for x in failed)
    return _qc_error(d, "failed", f"AutoQC failed the opening prompt ({_when(created_at)}). {what}. "
                     "Change the prompt, then {fix}.", audit_id=audit_id,
                     failed=[{"dimension": x["dimension"], "justification": x.get("justification")} for x in failed])


def _run_prompt_qc(d: Path, log) -> dict:
    """--qc, last step: AutoQC on what Studio holds now; refuse if a prompt dimension fails."""
    log("Running AutoQC on the opening prompt (a few minutes)...")
    aid = start_qc(d, "AutoQC", checked=True)
    if not aid:
        raise _qc_error(d, "not_started", "Studio didn't start AutoQC. Try {fix} again.")
    res = poll_qc(d, aid, every=QC_GATE_POLL_S, timeout=QC_GATE_TIMEOUT_S)
    if res["status"] != "completed":
        raise _qc_error(d, "unfinished", f"AutoQC didn't finish ({res['status']}"
                        + (f": {res['failure_reason']}" if res.get("failure_reason") else "") + "). "
                        f"When it does, `johto lane {d.name} validate` checks it; or run {{fix}} again.", audit_id=aid)
    _log_prompt_dims(res["dims"], log)
    failed = prompt_failures(res["dims"])
    if failed:
        raise _failed_error(d, failed, aid, (res.get("audit") or {}).get("created_at") or now_iso())
    return {"audit_id": aid, "dims": res["dims"]}


def lane_gate(d: Path, kind: str, *, again: bool = False, qc: bool = False, skip_qc: bool = False,
              need_prompt: bool = False, log=lambda line: None) -> dict:
    """The checks before a validation lane. Calibration lanes pass straight through.

    1. Studio's opening prompt must be the one in submission.md (the one the lane gets). With qc, Studio's
       edits come in first and the prompt alone is saved when only this copy changed.
    2. No earlier validation run used this prompt, unless again.
    3. AutoQC has passed this prompt (prompt_qc_status); with qc it runs now, unless the latest audit already
       passed this exact prompt. skip_qc skips 1 and 3. Only the prompt dimensions count (prompt_failures):
       empty post-run fields (goal, decision points, notes) never block, and AI Detection, which scores the
       whole task, blocks only when its justification points at the opening prompt; otherwise it is logged as
       a warning.
    With no opening prompt anywhere there is nothing to check: need_prompt (the CLI) refuses, otherwise the
    lane is set up and the prompt goes in later. Raises ActionError: same_prompt, prompt_qc, sync_conflict,
    missing_fields. Returns {"checked", "audit_id", "again", "skip_qc"}."""
    out = {"checked": False, "audit_id": None, "again": again, "skip_qc": skip_qc}
    if kind != "validate":
        return out
    _guard_prompt_overlap(d)   # before anything reaches Studio or the lane
    if workspace.is_draft(workspace.load(d)):
        out["draft"] = True
        log("No Studio slot yet, so Mercor's prompt check waits until you claim one. The run starts now.")
        studio_prompt = None
    elif skip_qc:
        log("warning: starting without the prompt AutoQC check (--skip-qc). If AutoQC later asks for a prompt "
            "change, you'll need another validation run.")
        studio_prompt = None
    elif qc:
        studio_prompt = _match_prompt_with_studio(d, log)
    else:
        studio_prompt = _studio_prompt(d, log)
    prompt = _local_prompt(d)
    if not prompt and not (studio_prompt or "").strip():
        if need_prompt:
            msg = ("Write the opening prompt first, in Studio or submission.md." if studio_prompt is not None else
                   f"No opening prompt in submission.md. Write it there, or bring Studio's in with `johto pull {d.name}`.")
            raise ActionError("missing_fields", msg, {"fields": ["opening_prompt"]})
        return out
    if studio_prompt is not None and lanes.prompt_sha(studio_prompt) != lanes.prompt_sha(prompt):
        raise _qc_error(d, "differs", "Studio's opening prompt isn't the one in submission.md, so AutoQC hasn't "
                        "checked the prompt this lane would get. {fix} brings Studio's text in (or saves yours if "
                        "only yours changed), runs AutoQC, and sets up the lane if the prompt passes.")
    if not again:
        hits = same_prompt_runs(d, prompt)
        if hits:
            raise _same_prompt_error(d, hits)
    if studio_prompt is None:   # skip_qc, or Studio is off for this copy
        return out
    out["checked"] = True
    st = prompt_qc_status(d, prompt)
    if qc and st["reason"] != "passed":
        ran = _run_prompt_qc(d, log)
        out["audit_id"] = ran["audit_id"]
        log("The prompt passed AutoQC.")
        _log_prompt_warnings(ran["dims"], log)
        return out
    if qc:
        log(f"The prompt already passed AutoQC ({_when(st['created_at'])}), so it isn't run again.")
    if st["reason"] == "failed":
        raise _failed_error(d, st["failed"], st["audit_id"], st["created_at"])
    if st["reason"] == "running":
        raise _qc_error(d, "running", f"AutoQC is running now (started {_when(st['running'])}). When it "
                        f"finishes, run `johto lane {d.name} validate` again; or {{fix}} waits for a fresh one.")
    if st["reason"] != "passed":
        raise _qc_error(d, "stale", "No AutoQC since the opening prompt last changed. {fix} runs it and sets up "
                        "the lane if the prompt passes.", audit_id=st["audit_id"])
    log(f"The prompt passed AutoQC ({_when(st['created_at'])}).")
    _log_prompt_warnings(st.get("dims") or [], log)
    out["audit_id"] = st["audit_id"]
    return out


# --- submit, unclaim, discard ----------------------------------------------------------------------------

def submit(d: Path) -> None:
    """Re-read the task (Writing and mine), check that Trajectory Logs holds the submission run's transcript
    (from that same read), then Submit for Review."""
    meta = workspace.load(d)
    need_slot(meta, d)
    t = recheck(meta["task_id"], "Writing")
    cf = t.get("custom_fields") if isinstance(t, dict) else None
    if isinstance(cf, dict):
        save_studio_snapshot(d, cf)
        ts = trajectory_state(d, None, meta, cf)
        if ts["status"] in ("missing", "other_run"):
            what = ("has no Trajectory Logs upload" if ts["status"] == "missing"
                    else f"has {' and '.join(ts['other'])}'s transcript in Trajectory Logs, not {ts['run']}'s")
            raise ActionError("missing_fields", f"Studio {what}. Upload {ts['path'] or 'the transcript'} there, then "
                              "submit again.", {"fields": [TRAJECTORY_FIELD], "trajectory": ts})
    S().transition(meta["task_id"], config.SUBMIT)

    def put(m):
        m["submitted_at"] = now_iso()
        st = m.setdefault("studio", {})
        st.update(status="Awaiting Review", polled_at=now_iso(), times_submitted=int(st.get("times_submitted") or 0) + 1)
    meta = update_meta(d, put)
    stages.log(d, "submitted", task_id=meta["task_id"], times=(meta.get("studio") or {}).get("times_submitted"))


def unclaim(d: Path) -> None:
    meta = workspace.load(d)
    need_slot(meta, d)
    recheck(meta["task_id"], "Writing")
    S().transition(meta["task_id"], config.UNCLAIM)
    update_meta(d, lambda m: m.update(unclaimed_at=now_iso(), studio={**(m.get("studio") or {}),
                                                                       "status": "Ready to Claim", "polled_at": now_iso()}))


DISCARD_REASONS = {
    "easy": "Too easy: the model solved it",
    "unfair": "Model failed, but the task was unfair or ambiguous",
    "repo": "Repo or environment problems",
    "slow": "Took too long to set up or run",
}


def discard_link(d: Path, meta: dict, reason: str) -> str:
    """The Discarded Run Log URL with the factual fields filled. The three free-text answers stay empty."""
    slot, repo = meta["slot"], meta["repo"]
    q = [("usp", "pp_url"),
         ("entry.688359476", config.WRITER_NAME),
         ("entry.1629887431", meta["task_name"]),
         ("entry.1393495501", repo.get("pinned_url") or ""),
         ("entry.1449916946", DISCARD_REASONS.get(reason, reason)),
         ("entry.143860987", slot["codebase_type"]),
         ("entry.1012973942", slot["action"]),
         ("entry.190885126", slot["prompt_style"]),
         ("entry.1716787012", slot["interaction_mode"])]
    for dom in (slot.get("domain") or "").split(", "):
        q.append(("entry.524197427", dom))
    if repo.get("language") in config.LANGUAGES[:10]:
        q.append(("entry.577175295", repo["language"]))
    recs = [r for r in meta.get("runs", []) if r.get("transcript") and Path(r["transcript"]).exists()]
    if recs:
        s = transcript.summarize(Path(recs[-1]["transcript"]))
        eff = next(iter(s["efforts"]), "high")
        if any("opus-5-5" in m for m in s["models"]) and eff in ("high", "xhigh", "medium"):
            q.append(("entry.1265722918", f"Opus 5.5 - {eff}"))
        q.append(("entry.56055811", str(len(s["follow_ups"]))))
        if reason == "easy" and not s["follow_ups"]:
            q += [("entry.1167518327", "Solved on the first try (one-shot)"),
                  ("entry.1292663648", "None"), ("entry.1149220368", "It didn't struggle")]
        q.append(("entry.1910140360", str(round(sum(workspace.run_minutes(r, d) or 0 for r in recs)))))
    return config.DISCARD_FORM + "?" + urllib.parse.urlencode(q)


DISCARD_STEPS = ["recheck", "unclaiming", "tearing_down", "marking_lead"]


def discard(d: Path, reason: str = "easy", unclaim_first: bool = True, on_step=lambda step: None,
            run: str | None = None) -> dict:
    """Unclaim (when still mine), tear down the task's lanes, mark the lead discarded, and return the
    pre-filled Discarded Run Log link. {"url", "removed", "skipped", "unclaimed"}."""
    meta = workspace.load(d)
    unclaimed = False
    if unclaim_first and not workspace.is_draft(meta):
        on_step("recheck")
        t = S().task(meta["task_id"])
        if S().status_name(t) == "Writing" and t.get("owned_by") == me():
            on_step("unclaiming")
            S().transition(meta["task_id"], config.UNCLAIM)
            unclaimed = True
    on_step("tearing_down")
    cleaned = clean(d)
    on_step("marking_lead")
    if meta.get("lead_id"):
        mark_lead(meta["lead_id"], "discarded")
    url = discard_link(d, meta, reason)

    def put(m):
        m["discarded"] = True
        m["discard"] = {"at": now_iso(), "url": url, "reason": reason, "logged_at": None, "run": run}
        if unclaimed:
            m["unclaimed_at"] = now_iso()
        if run:
            m.setdefault("decisions", {})[run] = {"decision": "drop", "at": now_iso()}
    update_meta(d, put)
    return {"url": url, "unclaimed": unclaimed, **cleaned}


def discard_logged(d: Path) -> dict:
    meta = update_meta(d, lambda m: (m.setdefault("discard", {}).update(logged_at=now_iso())))
    return meta["discard"]


# --- decisions -----------------------------------------------------------------------------------------

def decide(d: Path, run: str, decision: str) -> dict:
    """Record keep or rework for a calibration run (drop goes through discard)."""
    if decision not in ("keep", "rework"):
        raise ActionError("invalid", "decision must be keep, rework or drop.")
    check_run_name(run)
    if not (d / "runs" / run).is_dir():
        raise ActionError("not_found", f"No run {run} in {d.name}.")

    def put(m):
        m.setdefault("decisions", {})[run] = {"decision": decision, "at": now_iso()}
        if decision == "rework":
            m.pop("ready", None)
    return update_meta(d, put)["decisions"]


def skip_calibration(d: Path) -> None:
    update_meta(d, lambda m: m.update(skipped_calibration=True))


# --- Studio status --------------------------------------------------------------------------------------

def save_studio_snapshot(d: Path, custom_fields: dict | None) -> None:
    """What Studio holds, for the "as saved" view: tasks/<short>/app/studio-fields.json. A change to any of
    Austin's fields since the last snapshot also adds a line to app/studio-history.jsonl."""
    if custom_fields is None:
        return
    p = d / "app" / "studio-fields.json"
    p.parent.mkdir(exist_ok=True)
    at = now_iso()
    _write_json(p, {"at": at, "fields": custom_fields})
    _note_studio_change(d, custom_fields, at)


def _history_fields() -> tuple[str, ...]:
    return (*sync.SECTION_FIELDS, *sync.RATING_FIELDS, TRAJECTORY_FIELD)


def _note_studio_change(d: Path, cf: dict, at: str) -> None:
    """app/studio-history.jsonl: {at, fields: {field: sha}, changed: [field]} each time a snapshot of Studio
    differs from the last line in Austin's fields or the Trajectory Logs upload. focus.py reads the lines as signs
    that he worked in Studio; `johto where` names the fields that changed. Never raises."""
    try:
        shas = {f: sync.h(json.dumps(cf[f], sort_keys=True) if not isinstance(cf[f], str) else cf[f])
                for f in _history_fields() if cf.get(f) not in (None, "", [])}
        p = d / "app" / "studio-history.jsonl"
        last: dict = {}
        if p.exists():
            lines = p.read_text().splitlines()
            if lines:
                last = json.loads(lines[-1]).get("fields") or {}
        if shas == last:
            return
        changed = sorted(f for f in set(shas) | set(last) if shas.get(f) != last.get(f))
        line = {"at": at, "fields": shas, "changed": changed}
        if not p.exists():
            line["first"] = True    # the first look at Studio: what was there, not an edit
        with open(p, "a") as fh:
            fh.write(json.dumps(line) + "\n")
    except Exception:
        pass


def studio_snapshot(d: Path) -> dict | None:
    p = d / "app" / "studio-fields.json"
    return json.loads(p.read_text()) if p.exists() else None


def studio_status(d: Path) -> dict:
    """GET the task: {status, owner, mine, feedback, times_submitted, fields}; cached in task.json."""
    meta = workspace.load(d)
    need_slot(meta, d)
    t = S().task(meta["task_id"])
    cf = t.get("custom_fields") or {}
    out = {"status": S().status_name(t), "owner": t.get("owned_by"), "mine": t.get("owned_by") == me(),
           "feedback": cf.get("field_review_writer_feedback"),
           "times_submitted": cf.get("field_times_submitted"), "fields": cf}

    def put(m):
        st = m.setdefault("studio", {})
        if out["feedback"] != st.get("feedback"):
            st["feedback_seen_at"] = None
            if out["feedback"]:
                st["feedback_at"] = now_iso()   # derive: feedback newer than the last submit = sent back
        if st.get("status") in ("Awaiting Review", "In Review") and out["status"] == "Writing":
            st["sent_back_at"] = now_iso()      # Studio moved it from review back to Austin
        st.update(status=out["status"], polled_at=now_iso(), feedback=out["feedback"],
                  times_submitted=out["times_submitted"] if out["times_submitted"] is not None else st.get("times_submitted", 0),
                  mine=out["mine"])
    update_meta(d, put)
    save_studio_snapshot(d, cf)
    return out


def studio_fields_view(d: Path) -> dict:
    """{local, studio, diff, studio_at}: the local fields beside the last snapshot of what Studio holds."""
    local = fields(d)
    snap = studio_snapshot(d)
    remote = (snap or {}).get("fields")
    diff = sorted(k for k in set(local) | set(remote or {}) if remote is not None and k in local
                  and str(local.get(k)).strip() != str((remote or {}).get(k)).strip())
    return {"local": local, "studio": remote, "diff": diff, "studio_at": (snap or {}).get("at")}


# --- Trajectory Logs ---------------------------------------------------------------------------------------
# Studio's required upload (docs/rules.md submit-trajectory-logs): the submission run's transcript (.jsonl) in
# the Trajectory Logs file field. Austin uploads it in Studio; Studio keeps the original file name after its own
# "file_<id>_" prefix (s3://.../file_<32 hex>_<name>), so the receipt names the run whose transcript it is.

TRAJECTORY_FIELD = "field_trajectory_log_files"
_STUDIO_FILE_PREFIX = re.compile(r"^file_[0-9a-f]{32}_")


def trajectory_files(cf: dict | None) -> list[str]:
    """The original file names in Studio's Trajectory Logs field."""
    out = []
    for x in (cf or {}).get(TRAJECTORY_FIELD) or []:
        url = x.get("file_s3_url") or x.get("url") or x.get("name") if isinstance(x, dict) else x
        if isinstance(url, str) and url.strip():
            out.append(_STUDIO_FILE_PREFIX.sub("", url.rstrip("/").rsplit("/", 1)[-1]))
    return out


def trajectory_state(d: Path, run: str | None = None, meta: dict | None = None, cf: dict | None = None) -> dict:
    """Does Studio hold the submission run's transcript as a Trajectory Log? `cf` is Studio's custom_fields
    (a fresh read); without it the last snapshot is used.

    {status, run, want, path, files, other, at}: status is "uploaded" (the run's transcript is there),
    "other_run" (another run's transcript, named in `other`), "unknown_file" (files johto can't match to a run),
    "missing" (nothing uploaded) or "no_snapshot" (Studio not read yet)."""
    meta = meta or workspace.load(d)
    run, rec = validation_run(d, run)
    at = None
    if cf is None:
        snap = studio_snapshot(d)
        cf, at = (snap or {}).get("fields"), (snap or {}).get("at")
    want = Path(rec["transcript"]).name if rec and rec.get("transcript") else None
    files = trajectory_files(cf)
    by_name = {Path(r["transcript"]).name: Path(r["dir"]).name for r in meta.get("runs") or []
               if r.get("transcript") and r.get("dir")}
    other = sorted({by_name[f] for f in files if f in by_name and by_name[f] != run})
    if cf is None:
        status = "no_snapshot"
    elif not files:
        status = "missing"
    elif want and want in files:
        status = "uploaded"
    elif other:
        status = "other_run"
    else:
        status = "unknown_file"
    return {"status": status, "run": run, "want": want, "path": (rec or {}).get("transcript"), "files": files,
            "other": other, "at": at}


def trajectory_row(d: Path, run: str | None = None, meta: dict | None = None) -> dict:
    """The checklist row for the Trajectory Logs upload. Missing, or another run's transcript, blocks submit."""
    meta = meta or workspace.load(d)
    ts = trajectory_state(d, run, meta)
    url = studio.task_url(meta["task_id"]) if meta.get("task_id") else None
    when = f" (Studio as of {_hhmm(ts['at'])})" if ts.get("at") else ""
    fix = {"action": "open_studio", "url": url, "path": ts["path"], "then": "studio_refresh"}
    run = ts["run"]
    if ts["status"] == "uploaded":
        return _row("trajectory_logs", "pass", "Trajectory Logs", f"{run}'s transcript is in Studio{when}",
                    files=ts["files"])
    if workspace.is_draft(meta):
        return _row("trajectory_logs", "pending", "Trajectory Logs wait for a slot",
                    f"Upload {ts['path'] or 'the transcript'} to Trajectory Logs in Studio once a slot is claimed.",
                    path=ts["path"])
    if ts["status"] == "no_snapshot":
        return _row("trajectory_logs", "pending", "Trajectory Logs not checked",
                    "Refresh from Studio to see whether the transcript is uploaded.",
                    fix={"action": "studio_refresh", "args": {}})
    upload = f"Upload {ts['path']} to Trajectory Logs in Studio" if ts["path"] else f"Upload {run}'s transcript"
    if ts["status"] == "missing":
        return _row("trajectory_logs", "fail", "Trajectory Logs not uploaded",
                    f"Studio won't submit without it{when}. {upload}.", fix=fix, files=[])
    if ts["status"] == "other_run":
        which = " and ".join(ts["other"])
        return _row("trajectory_logs", "fail", f"Trajectory Logs hold {which}'s transcript",
                    f"The submission uses {run}{when}. {upload} and remove the other one.", fix=fix, files=ts["files"])
    return _row("trajectory_logs", "warn", "Trajectory Logs", f"Studio holds {', '.join(ts['files'])}{when}; johto "
                f"can't tell which run that is. The submission uses {run} ({ts['want']}).", blocking=False, fix=fix,
                files=ts["files"])


# --- the submit checklist --------------------------------------------------------------------------------

def _row(key, status, title, detail="", blocking=True, fix=None, **extra) -> dict:
    return {"key": key, "status": status, "title": title, "detail": detail,
            "blocking": blocking and status != "pass", "fix": fix, **extra}


def _hhmm(iso: str | None) -> str:
    return (iso or "")[11:16]


def model_label(m: str) -> str:
    """claude-opus-5-5 -> Opus 5.5"""
    mm = re.fullmatch(r"claude-([a-z]+)-(\d+)-(\d+)(?:-\d{8})?", m or "")
    return f"{mm.group(1).title()} {mm.group(2)}.{mm.group(3)}" if mm else m


FIELD_KEY = {v: k for k, v in {
    "opening_prompt": "field_opening_prompt", "goal": "field_high_level_goal",
    "decision_points": "field_rationale_difficulty", "codebase": "field_codebase_overview",
    "initial_state": "field_initial_state", "validation_notes": "field_self_check",
    "reviewer_notes": "field_writer_submission_notes"}.items()}
LINT_FIELD = {"Opening Prompt": "opening_prompt", "Goal": "goal", "Decision Points": "decision_points",
              "Difficulty & Codebase": "codebase", "Validation Notes": "validation_notes",
              "Initial State": "initial_state"}
FIELD_LABEL = {"field_opening_prompt": "Opening Prompt", "field_high_level_goal": "High-Level Task Goal",
               "field_rationale_difficulty": "Decision Points", "field_codebase_overview": "Difficulty & Codebase",
               "field_self_check": "Validation Notes", "field_writer_submission_notes": "Notes for the Reviewer",
               "field_initial_state": "Initial State", "field_one_shot": "One-shot", "field_task_success": "Task Success",
               "field_steering": "Steering", "field_claude_minutes": "Claude minutes",
               "field_conversation_log": "Conversation Log", "field_final_output": "Final Output",
               "field_repo_name": "Repo name", "field_repo_url": "Repo URL", "field_primary_language": "Language",
               "field_primary_language_other": "Language (other)"}


def _lint_view(d: Path, meta: dict, sub: dict) -> dict:
    """submission.md as lint reads it, with each blank section taken from Studio's saved snapshot (draftcheck's
    read_draft). A blank local copy of a field Studio holds is not an empty field; local text, which the editor
    may have changed since the last save, still wins."""
    try:
        from . import draftcheck
        draft = draftcheck.read_draft(d, meta)
    except Exception:
        return sub
    sections = dict(sub.get("sections") or {})
    for key, (_, heading, _) in draftcheck.TEXT_FIELDS.items():
        studio = draft["fields"].get(key, {}).get("studio")
        if studio and not (sections.get(heading) or "").strip():
            sections[heading] = studio
    return {**sub, "sections": sections}


def checklist(d: Path, run: str | None = None) -> list[dict]:
    """CheckRow[] for the Studio surface and the submit gate: failures first, passing rows after."""
    meta = workspace.load(d)
    sub = workspace.parse_submission(d)
    slot = meta["slot"]
    rows = []
    draft = workspace.is_draft(meta)
    if draft:
        n = draft_open(meta)
        rows.append(_row("slot", "pending", "No Studio slot yet",
                         (f"{n} open slot{'s' if n != 1 else ''} match{'es' if n == 1 else ''}. " if n else
                          "No open slot matches right now. ") + "Claiming one saves every field and the gists to "
                         "Studio and runs AutoQC.", fix={"action": "claim_slot", "args": {}}, open=n))

    # AutoQC
    q = latest_qc(meta)
    pushed = meta.get("pushed") or {}
    if not q and not pushed.get("at"):
        pass   # before the first save one row says it all (studio_sync below)
    elif not q:
        rows.append(_row("autoqc", "pending", "AutoQC not run yet", "Save to Studio runs it.",
                         fix={"action": "studio_save", "args": {}}))
    elif q.get("status") == "running":
        rows.append(_row("autoqc", "pending", "AutoQC running", f"started {_hhmm(q.get('started_at'))}"))
    elif q.get("status") != "completed":
        rows.append(_row("autoqc", "fail", "AutoQC didn't finish", q.get("failure_reason") or q.get("status") or "",
                         fix={"action": "studio_qc", "args": {"spec": "AutoQC"}}))
    elif pushed.get("at") and q.get("started_at", "") < pushed["at"]:
        rows.append(_row("autoqc", "pending", "AutoQC ran before your last save", "Rerun it on what Studio holds now.",
                         fix={"action": "studio_qc", "args": {"spec": "AutoQC"}}))
    else:
        fails = [x for x in q.get("dims") or [] if x.get("status") == "fail"]
        c = q.get("counts") or {}
        if fails:
            f0 = fails[0]
            fix = ({"action": "focus_field", "field": f0["field_key"], "span": None} if f0.get("field_key")
                   else {"action": "focus_field", "field": "reviewer_notes", "span": None})
            rows.append(_row("autoqc", "fail", f"{len(fails)} AutoQC check{'s' if len(fails) > 1 else ''} failed",
                             " · ".join(x["dimension"] for x in fails), blocking=False, fix=fix, dims=fails))
        else:
            rows.append(_row("autoqc", "pass", f"AutoQC {_hhmm(q.get('finished_at') or q.get('started_at'))}",
                             f"{c.get('pass', 0)} passed"))

    # Studio sync
    now_fields = fields(d, run)
    if draft:
        pass   # the slot row above says it: nothing reaches Studio before a slot is claimed
    elif not pushed.get("at"):
        rows.append(_row("studio_sync", "pending", "Not saved to Studio yet",
                         "Saving uploads the gists, saves every field and runs AutoQC",
                         fix={"action": "studio_save", "args": {}}))
    else:
        old = pushed.get("fields") or {}
        changed = sorted(k for k in now_fields if sha12(now_fields[k]) != old.get(k))
        if changed:
            names = [FIELD_LABEL.get(k, k) for k in changed]
            rows.append(_row("studio_sync", "fail", f"Saved to Studio {_hhmm(pushed['at'])}",
                             f"{len(changed)} field{'s' if len(changed) > 1 else ''} changed since: {', '.join(names)}",
                             fix={"action": "studio_save", "args": {}}, changed=[FIELD_KEY.get(k, k) for k in changed]))
        else:
            rows.append(_row("studio_sync", "pass", f"Saved to Studio {_hhmm(pushed['at'])}", "no changes since"))

    # What Studio saved: a paste cut off, or less than the local copy. Only a confirmed cut blocks.
    saved = saved_text_flags(d)
    if saved:
        bad = [f for f in saved if f[0] == lint.ERROR]
        key = (LINT_FIELD | {"Notes for the Reviewer": "reviewer_notes"}).get((bad or saved)[0][1])
        rows.append(_row("studio_saved", "fail" if bad else "warn", "Studio's saved text",
                         "; ".join(f"{f[1]}: {f[2]}" for f in saved), blocking=bool(bad),
                         fix={"action": "focus_field", "field": key, "span": None} if key else None,
                         flags=[{"level": a, "field": b, "message": c} for a, b, c in saved]))

    # Text a previous writer left in Studio (sync never pulls it in; see johto/sync.py).
    fo = (meta.get("sync") or {}).get("foreign") or {}
    fo = {f: why for f, why in fo.items() if not (f in now_fields and why == "empty here")}
    if fo:
        names = ", ".join(FIELD_LABEL.get(f, f) for f in fo)
        n = f"{len(fo)} field{'s' if len(fo) > 1 else ''}"
        empty = [f for f, why in fo.items() if why == "empty here" and f not in now_fields]
        if empty:
            rows.append(_row("studio_text", "fail", "Studio holds a previous writer's text",
                             f"{n}: {names}. Save overwrites the ones you have written; write your own text in the "
                             "others or clear them in Studio.", foreign=sorted(fo)))
        else:
            rows.append(_row("studio_text", "warn", "Studio holds a previous writer's text",
                             f"{n}: {names}. Save overwrites it with yours.", blocking=False,
                             fix={"action": "studio_save", "args": {}}, foreign=sorted(fo)))

    # Ratings
    rflags = lint.check_ratings(sub["ratings"], slot)
    if rflags:
        rows.append(_row("ratings", "fail", "Ratings", "; ".join(f[2] for f in rflags),
                         fix={"action": "focus_field", "field": "ratings", "span": None}))
    else:
        r = sub["ratings"]
        rows.append(_row("ratings", "pass", "Ratings pass",
                         f"{slot.get('codebase_type')} · Task Success {r.get('task success')} · Steering {r.get('steering') or '-'}"))

    # Validation run
    vrun, rec = validation_run(d, run)
    probs = run_problems(d, run)
    errs = [p for p in probs if p[0] == lint.ERROR]
    warns = [p for p in probs if p[0] == lint.WARN]
    chosen = " (chosen)" if meta.get("submission_run") == vrun and vrun else ""
    waiting = newer_uncollected(meta, d, None) if not vrun else []
    kind, _, n = (vrun or (waiting[-1] if waiting else "")).partition("-")
    title = f"{'Calibration' if kind == 'calib' else 'Validation'} {n}" if n else "Validation run"
    if errs:
        to_collect = vrun if vrun and not (rec or {}).get("transcript") else (waiting[-1] if waiting else None)
        rows.append(_row("validation_run", "fail", f"{title}{chosen}", "; ".join(p[2] for p in errs),
                         fix={"action": "collect", "args": {"run": to_collect}} if to_collect else None,
                         run=vrun or to_collect))
    else:
        try:
            s = transcript.summarize(Path(rec["transcript"]))
        except OSError:
            s = {"efforts": {}, "models": {}, "human_turns": rec.get("human_turns") or 0,
                 "active_minutes": rec.get("active_minutes") or 0}
        eff = ", ".join(s["efforts"]) or "effort not recorded"
        detail = (f"{', '.join(model_label(m) for m in s['models'])} · {eff} · "
                  f"{s['human_turns']} prompt{'s' if s['human_turns'] != 1 else ''} · {s['active_minutes']} min")
        if warns:
            rows.append(_row("validation_run", "warn", f"{title}{chosen}", detail + " · " + "; ".join(p[2] for p in warns),
                             blocking=False, run=vrun))
        else:
            rows.append(_row("validation_run", "pass", f"{title}{chosen}", detail, run=vrun))

    # Trajectory Logs: the upload Studio requires (its own file field, separate from the gist links).
    if vrun:
        rows.append(trajectory_row(d, vrun, meta))

    # Gists: links in Conversation Log / Final Output. Studio's current form doesn't require them (accepted
    # tasks went through without), so this row never blocks; Save to Studio keeps them current.
    gs = gist_state(d, run, meta) if vrun else None
    g = meta.get("gists") or {}
    if gs and not gs["need"]:
        rows.append(_row("gists", "pass", "Gists", f"conversation log, final diff (secret, {gists.GH_ACCOUNT})"))
    elif gs and (pushed.get("at") or g.get("conversation_log") or g.get("final_output")):
        whys = sorted(set(gs["why"].values()))
        title = "Gists out of date" if g.get("conversation_log") or g.get("final_output") else "Gists not uploaded"
        rows.append(_row("gists", "pending", title, "; ".join(whys).capitalize() + f". Save to Studio uploads {vrun}'s.",
                         blocking=False, fix={"action": "studio_save", "args": {}}, need=gs["need"]))

    # Local lint (rating flags have their own row), with blank local sections read from Studio's saved copy
    flags = [f for f in lint.run_all(meta, _lint_view(d, meta, sub)) if f[1] != "Ratings"]
    le = [f for f in flags if f[0] == lint.ERROR]
    lw = [f for f in flags if f[0] == lint.WARN]
    flag_dicts = [{"level": a, "field": b, "message": c, "field_key": LINT_FIELD.get(b)} for a, b, c in flags]
    detail = f"{len(le)} error{'s' if len(le) != 1 else ''} · {len(lw)} warning{'s' if len(lw) != 1 else ''}"
    if le:
        f0 = next((f for f in flag_dicts if f["level"] == lint.ERROR), None)
        rows.append(_row("lint", "fail", "Local checks", detail,
                         fix={"action": "focus_field", "field": f0["field_key"], "span": None} if f0 and f0["field_key"] else None,
                         flags=flag_dicts))
    else:
        rows.append(_row("lint", "warn" if lw else "pass", "Local checks", detail, blocking=False, flags=flag_dicts))

    rows.append(provenance_row(d, sub))

    order = {"fail": 0, "pending": 1, "warn": 2, "pass": 3}
    return sorted(rows, key=lambda r: order.get(r["status"], 4))


def protected_texts(sub: dict) -> dict[str, str]:
    """{section key: text} of the protected fields in submission.md."""
    return {FIELD_KEY[fid]: sub["sections"].get(heading, "") for heading, fid in workspace.SECTIONS.items()
            if fid in FIELD_KEY}


def red_overlaps(d: Path, sub: dict | None = None) -> dict[str, list[dict]]:
    """{key: [unaccepted red Overlap]}. Raises ActionError when the guard can't run (fail closed)."""
    sub = sub or workspace.parse_submission(d)
    try:
        from .web.guard import overlap
        return overlap.unaccepted_red(d, {k: t for k, t in protected_texts(sub).items() if t.strip()})
    except Exception as e:
        raise ActionError("guard_missing", f"The overlap check didn't run ({e}), so saving is blocked until it does.") from e


def _authorship_counts(d: Path) -> tuple[int, int]:
    typed = pasted = 0
    try:
        for line in (d / "app" / "authorship.jsonl").read_text().splitlines():
            e = json.loads(line)
            if e.get("event") == "save":
                typed += int(e.get("typed") or 0)
                pasted += int(e.get("pasted") or 0)
    except (OSError, ValueError):
        pass
    return typed, pasted


def provenance_row(d: Path, sub: dict) -> dict:
    """Design 3.6: no unaccepted red overlap with AI text in any protected field."""
    try:
        red = red_overlaps(d, sub)
    except ActionError as e:
        return _row("provenance", "fail", "Overlap check didn't run", e.message)
    typed, pasted = _authorship_counts(d)
    counts = f"typed {typed:,} characters, pasted {pasted:,}"
    if red:
        n = sum(len(v) for v in red.values())
        labels = {v: k for k, v in LINT_FIELD.items()} | {"goal": "Goal", "reviewer_notes": "Notes for the Reviewer",
                                                          "opening_prompt": "Opening Prompt"}
        where = ", ".join(labels.get(k, k) for k in red)
        first = next(iter(red))
        return _row("provenance", "fail", "Red overlap with AI text",
                    f"{n} span{'s' if n > 1 else ''} in {where}. Reword it or mark it as your own words.",
                    fix={"action": "focus_field", "field": first, "span": None})
    return _row("provenance", "pass", "Your words", f"no red overlap · {counts}")


def submit_blockers(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("blocking") and r.get("status") != "pass"]
