"""Task actions shared by the CLI and the web app.

Every function here is synchronous, never calls input() or print(), returns data, and raises
ActionError(code, message, details) with a plain-words message, or studio.StudioError with Studio's
response verbatim. Nothing here writes, suggests or rewrites the text of Austin's fields: the only
text that goes to Studio is what submission.md already holds.

Studio access goes through S(). Outside the web app that is johto.studio (or the fake with
JOHTO_FAKE_STUDIO=1). Inside a web job it is a proxy that queues each call on the single Studio
worker (johto/web/jobs.py), so calls never overlap in the one Studio tab.
"""
from __future__ import annotations

import contextlib
import contextvars
import fcntl
import hashlib
import io
import json
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from . import config, gists, lanes, lint, studio, sync, transcript, workspace

# --- errors -------------------------------------------------------------------------------------------

HTTP_STATUS = {
    "invalid": 400, "budget": 402, "not_found": 404, "expired": 410,
    "needs_confirm": 409, "precondition_failed": 409, "lane_busy": 409, "lane_state": 409, "taken": 409,
    "claim_partial": 409, "busy": 409, "gh_account": 409, "conflict": 409, "sync_conflict": 409,
    "missing_fields": 422, "checklist_failed": 422, "guard_missing": 423, "blocked_overlap": 423,
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
    return recheck(workspace.load(d)["task_id"], "Writing")


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
          expect_status: str = "Ready to Claim", label: str | None = None) -> Path:
    """Claim the slot, save repo name/URL/language in Studio, and make tasks/<short>/.

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
        extra = {"claimed_at": now_iso(), "studio_at_claim": sync.claim_baseline(cf)}
        if lead:
            extra["lead_prev_status"] = lead.get("status")
        if dossier:
            extra["dossier"] = {"id": dossier.get("id"), "opportunity_id": (opportunity or {}).get("id")}
            if "## Dossier (AI research)" not in (d / "brief.md").read_text():
                with open(d / "brief.md", "a") as fh:
                    fh.write(opportunity_brief(dossier, opportunity))
        if label:
            extra["label"] = label
        update_meta(d, lambda m: m.update(extra))
    except Exception as e:
        raise ActionError("claim_partial", f"Claimed {name} in Studio, but making the task folder failed: {e}",
                          {**partial, "step": "making_folder"}) from e
    on_step("marking_lead")
    if lead:
        mark_lead(lead["id"], "claimed", name)
    drop_from_slots(task_id)
    return d


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


def start_lane(d: Path, kind: str, log=lambda line: None) -> Path:
    """A new lane folder for a calibration or validation run (setup only; Claude is started by Austin).

    While a lane slot is free, the new lane takes it and running lanes are left alone (two fit on one
    machine; see lanes.SLOTS). With both slots taken, setting up means stopping a lane, so it refuses while
    any lane's Claude is alive, while a running lane's run isn't collected, or while a prework container the
    app doesn't know is running: the app never stops a lane whose run isn't collected.
    """
    if kind not in lanes.KINDS:
        raise ActionError("invalid", f"kind must be one of {', '.join(lanes.KINDS)}.")
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
    with capture_prints(log):
        try:
            return L().new_lane(d, kind, stop_others=False)
        except RuntimeError as e:
            raise ActionError("setup_failed", str(e).splitlines()[0], {"log": str(e)}) from e


def latest_run(d: Path) -> str | None:
    return L().latest_run(d, "validate") or L().latest_run(d, "calib")


def run_record(d: Path, run: str, meta: dict | None = None) -> dict | None:
    meta = meta or workspace.load(d)
    return next((r for r in meta.get("runs", []) if r["dir"] == f"runs/{run}"), None)


def collect(d: Path, run: str | None = None, log=lambda line: None) -> dict:
    """prework collect for one run: diff, transcript and summary, recorded in task.json."""
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
    s = info.get("summary")
    n = len(transcript.find(d / "runs" / run))
    return {**info, "run": run, "transcripts": n, "diff_stat": lanes.diff_stat(info.get("diff")),
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


def validation_run(d: Path, run: str | None = None) -> tuple[str | None, dict | None]:
    """The run being submitted: `run` if given, else task.json's submission_run, else the latest validate-N."""
    check_run_name(run)
    meta = workspace.load(d)
    run = run or meta.get("submission_run")
    if run and (not RUN_NAME.match(run) or not (d / "runs" / run).is_dir()):
        run = None
    run = run or L().latest_run(d, "validate")
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
                    "active_minutes": rec.get("active_minutes"), "human_turns": rec.get("human_turns"),
                    "submitted": name == cur})
    return out


# --- checks ---------------------------------------------------------------------------------------------

def run_problems(d: Path, run: str | None = None) -> list[tuple]:
    """Checks on the submitted run: collected, model and effort, async = one prompt, prompt matches."""
    meta = workspace.load(d)
    sub = workspace.parse_submission(d)
    run, rec = validation_run(d, run)
    out = []
    if not run:
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
            out.append((lint.WARN, "Runs", f"Claude ran only {s['active_minutes']} minutes; reviewers reject tasks Opus "
                                           "finishes in about 5 minutes"))
    return out


def check(d: Path, run: str | None = None) -> list[tuple]:
    """Local lint plus run checks: [(level, field, message)]. Flags only, never wording."""
    meta = workspace.load(d)
    sub = workspace.parse_submission(d)
    return lint.run_all(meta, sub) + run_problems(d, run)


def fields(d: Path, run: str | None = None) -> dict:
    """The Studio custom_fields this task folder holds right now."""
    meta = workspace.load(d)
    sub = workspace.parse_submission(d)
    _, rec = validation_run(d, run)
    return workspace.to_fields(meta, sub, (rec or {}).get("active_minutes"))


def push(d: Path, run: str | None = None, checked: bool = False, task: dict | None = None) -> dict:
    """Save every field to Studio (patch_fields verifies the seeds are unchanged), then record hashes.
    `checked`: the caller re-read the task a moment ago (one recheck per composite job); pass that read as
    `task` so Studio's current text is compared without another GET."""
    meta = workspace.load(d)
    t = task or (recheck(meta["task_id"], "Writing") if not checked else S().task(meta["task_id"]))
    # Edits made in Studio come in first, so saving never overwrites them.
    pulled = sync.pull(d, t.get("custom_fields") or {}, update_meta=update_meta)
    if pulled["conflicts"]:
        names = ", ".join(sync.FIELD_NAMES[k] for k in pulled["conflicts"])
        raise ActionError("sync_conflict", f"Changed both here and in Studio since the last save: {names}. "
                          "Keep one version in both places, then save again.",
                          {"fields": {sync.FIELD_NAMES[k]: v for k, v in pulled["conflicts"].items()}})
    f = fields(d, run)
    after = S().patch_fields(meta["task_id"], f)
    at = now_iso()
    update_meta(d, lambda m: m.update(pushed={"at": at, "fields": {k: sha12(v) for k, v in f.items()}}))
    sync.mark_pushed(d, f, update_meta)
    save_studio_snapshot(d, after if isinstance(after, dict) else None)
    return {"fields": f, "saved": len(f), "at": at, "pulled": pulled["pulled"]}


def pull_from_studio(d: Path, write_section=None, write_ratings=None) -> dict:
    """Refresh the task's Studio status and, while it is mine and in Writing, bring Studio's edits in."""
    st = studio_status(d)
    if st["status"] != "Writing" or not st["mine"]:
        return {"status": st["status"], "pulled": [], "conflicts": {}}
    res = sync.pull(d, st["fields"], write_section, write_ratings, update_meta=update_meta)
    return {"status": st["status"], "pulled": res["pulled"],
            "conflicts": {sync.FIELD_NAMES[k]: v["why"] for k, v in res["conflicts"].items()}}


def upload_gists(d: Path, run: str | None = None, on_step=lambda step: None) -> dict:
    """Secret gists for the submitted run's transcript and diff; uploads only what is missing (or what
    belongs to a different run than the one now chosen)."""
    meta = workspace.load(d)
    run, rec = validation_run(d, run)
    if not rec or not rec.get("transcript") or not rec.get("diff"):
        raise ActionError("lane_state", "Need a collected run (transcript and diff) before uploading gists.",
                          {"run": run})
    g = dict(meta.get("gists") or {})
    if g.get("run") and g["run"] != run:
        g = {}
    need = [k for k in ("conversation_log", "final_output") if not g.get(k)]
    if not need:
        return {"gists": g, "uploaded": [], "run": run}
    on_step("gh_account")
    acct = G().active_account()
    if acct != gists.GH_ACCOUNT:
        raise ActionError("gh_account", f"gh is logged in as {acct!r}; switch with `gh auth switch -u {gists.GH_ACCOUNT}`.",
                          {"account": acct, "want": gists.GH_ACCOUNT})
    on_step("gists")
    short = d.name
    try:
        if "conversation_log" in need:
            g["conversation_log"] = G().create(rec["transcript"], f"{short}-conversation.jsonl",
                                               f"Johto {meta['task_name']} conversation log")
        if "final_output" in need:
            g["final_output"] = G().create(rec["diff"], f"{short}-final.diff", f"Johto {meta['task_name']} final output")
    except RuntimeError as e:
        raise ActionError("github", str(e)) from e
    finally:
        g["run"] = run
        update_meta(d, lambda m: m.update(gists=g))
    return {"gists": g, "uploaded": need, "run": run}


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
    if not checked:
        recheck(meta["task_id"], "Writing")
    res = S().run_qc(meta["task_id"], spec) or {}
    aid = res.get("qc_audit_id") or res.get("id")
    entry = {"audit_id": aid, "spec": spec, "started_at": now_iso(), "status": "running",
             "counts": {"pass": 0, "fail": 0, "neutral": 0}, "dims": [], "dedup": res.get("dedup")}
    update_meta(d, lambda m: m.setdefault("qc", []).append(entry))
    return aid


def record_qc(d: Path, audit_id: str, audit: dict) -> dict:
    summ = qc_summary(audit)

    def put(m):
        for q in m.setdefault("qc", []):
            if q.get("audit_id") == audit_id:
                q.update(status=summ["status"], counts=summ["counts"], dims=summ["dims"],
                         finished_at=now_iso() if summ["status"] != "running" else None,
                         failure_reason=summ.get("failure_reason"))
    update_meta(d, put)
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


# --- submit, unclaim, discard ----------------------------------------------------------------------------

def submit(d: Path) -> None:
    """Re-read the task (Writing and mine), then Submit for Review."""
    meta = workspace.load(d)
    recheck(meta["task_id"], "Writing")
    S().transition(meta["task_id"], config.SUBMIT)

    def put(m):
        m["submitted_at"] = now_iso()
        st = m.setdefault("studio", {})
        st.update(status="Awaiting Review", polled_at=now_iso(), times_submitted=int(st.get("times_submitted") or 0) + 1)
    update_meta(d, put)


def unclaim(d: Path) -> None:
    meta = workspace.load(d)
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
        q.append(("entry.1910140360", str(round(sum(r.get("active_minutes") or 0 for r in recs)))))
    return config.DISCARD_FORM + "?" + urllib.parse.urlencode(q)


DISCARD_STEPS = ["recheck", "unclaiming", "tearing_down", "marking_lead"]


def discard(d: Path, reason: str = "easy", unclaim_first: bool = True, on_step=lambda step: None,
            run: str | None = None) -> dict:
    """Unclaim (when still mine), tear down the task's lanes, mark the lead discarded, and return the
    pre-filled Discarded Run Log link. {"url", "removed", "skipped", "unclaimed"}."""
    meta = workspace.load(d)
    unclaimed = False
    if unclaim_first:
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
    """What Studio holds, for the "as saved" view: tasks/<short>/app/studio-fields.json."""
    if custom_fields is None:
        return
    p = d / "app" / "studio-fields.json"
    p.parent.mkdir(exist_ok=True)
    _write_json(p, {"at": now_iso(), "fields": custom_fields})


def studio_snapshot(d: Path) -> dict | None:
    p = d / "app" / "studio-fields.json"
    return json.loads(p.read_text()) if p.exists() else None


def studio_status(d: Path) -> dict:
    """GET the task: {status, owner, mine, feedback, times_submitted, fields}; cached in task.json."""
    meta = workspace.load(d)
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


def checklist(d: Path, run: str | None = None) -> list[dict]:
    """CheckRow[] for the Studio surface and the submit gate: failures first, passing rows after."""
    meta = workspace.load(d)
    sub = workspace.parse_submission(d)
    slot = meta["slot"]
    rows = []

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
    if not pushed.get("at"):
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
    kind, _, n = (vrun or "").partition("-")
    title = f"{'Calibration' if kind == 'calib' else 'Validation'} {n}" if vrun else "Validation run"
    if errs:
        rows.append(_row("validation_run", "fail", f"{title}{chosen}", "; ".join(p[2] for p in errs),
                         fix={"action": "collect", "args": {"run": vrun}} if vrun and not (rec or {}).get("transcript") else None,
                         run=vrun))
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

    # Gists
    g = meta.get("gists") or {}
    if g.get("conversation_log") and g.get("final_output") and (not g.get("run") or g["run"] == vrun):
        rows.append(_row("gists", "pass", "Gists", f"conversation log, final diff (secret, {gists.GH_ACCOUNT})"))
    elif pushed.get("at") or (g.get("run") and g["run"] != vrun):
        why = f"they are for {g['run']}; Save to Studio uploads {vrun}'s" if g.get("run") and g["run"] != vrun \
            else "Save to Studio uploads them"
        rows.append(_row("gists", "pending", "Gists not uploaded", why, fix={"action": "studio_save", "args": {}}))

    # Local lint (rating flags have their own row)
    flags = [f for f in lint.run_all(meta, sub) if f[1] != "Ratings"]
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
