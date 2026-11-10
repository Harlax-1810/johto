"""Read models for the web app, built from the files the CLI already writes.

Everything here is read-only except section and rating writes for real tasks, which go to
tasks/<short>/submission.md so the CLI and vim keep working.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from .. import config, lint, workspace
from ..scout import rank as scout_rank

SECTION_KEYS = {
    "opening_prompt": "Opening Prompt",
    "goal": "High-Level Task Goal",
    "decision_points": "Decision Points",
    "codebase": "Difficulty & Codebase Understanding",
    "initial_state": "Initial State / Setup",
    "validation_notes": "Validation Notes",
    "reviewer_notes": "Notes for the Reviewer",
}
HEADING_KEY = {v: k for k, v in SECTION_KEYS.items()}

STYLE_RULES = {
    "Casual prose": ("casual 20-60", "Reads like a Slack message or ticket from a colleague. Short, direct, points at "
                     "something concrete in the repo. No step lists, greetings, \"please\", role-play or form labels. "
                     "No diagnosis or fix. About 20-60 words, at most one checkable clause."),
    "Explicit spec": ("spec 450-1,200", "A written ticket: a clear title or summary line, context, numbered "
                      "requirements and relevant file paths. Complete acceptance criteria. Requirements, not "
                      "procedures. Proper spelling and punctuation. About 450-1,200 words."),
    "Error / artifact brief": ("error brief", "Paste a real error, stack trace or log, then add one short, casual "
                               "line. No acceptance criteria. Context is fine; the diagnosis is not."),
}


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def short_repo(full: str) -> str:
    return (full or "").split("/")[-1]


def short_slot(slot: dict) -> str:
    a = {"Understand / Assess": "Understand"}.get(slot.get("action"), slot.get("action"))
    d = (slot.get("domain") or "").replace(" / API", "").replace("Web frontend", "Web")
    c = (slot.get("codebase_type") or "").replace("-scale", "").replace("-grade", "")
    return f"{a} · {d} · {c}"


def slot_line(slot: dict) -> str:
    return " · ".join(str(slot.get(k) or "") for k in
                      ("action", "domain", "codebase_type", "prompt_style", "interaction_mode"))


# --- Sections --------------------------------------------------------------

def section(key: str, text: str, slot: dict, *, locked: str | None = None, rev: str = "0",
            others: dict | None = None) -> dict:
    """A Section with counters and lint flags for one field."""
    text = text or ""
    flags: list[tuple] = []
    if text.strip():
        if key == "opening_prompt":
            flags = lint.check_prompt(text, slot.get("prompt_style", ""))
        else:
            sec = {SECTION_KEYS[k]: v for k, v in (others or {}).items()}
            sec[SECTION_KEYS[key]] = text
            heading = SECTION_KEYS[key]
            short_field = {"goal": "Goal", "decision_points": "Decision Points",
                           "codebase": "Difficulty & Codebase", "validation_notes": "Validation Notes",
                           "initial_state": "Initial State"}.get(key)
            flags = [f for f in lint.check_sections(sec, slot) if f[1] == short_field and "empty" not in f[2]]
            del heading
    pairs = None
    if key == "decision_points":
        # lint's words (better/strong/good..., worse/weak/bad...): a structure hint, never a verdict
        pairs = {"better": len(lint.BETTER.findall(text)), "worse": len(lint.WORSE.findall(text))}
    style = None
    if key == "opening_prompt":
        label, rules = STYLE_RULES.get(slot.get("prompt_style"), ("", ""))
        style = {"label": label, "rules_md": rules}
    return {
        "key": key, "heading": SECTION_KEYS[key], "text": text, "rev": rev,
        "words": lint.words(text), "sentences": lint.sentences(text) if text.strip() else 0,
        "pairs": pairs,
        "lint": [{"level": lvl, "field": fld, "message": plain_lint(msg)} for lvl, fld, msg in flags],
        "overlap": [], "style": style,
        "locked": {"reason": locked} if locked else None,
    }


# The editors show lint in plain sentences (the CLI keeps lint.py's short forms). First match wins; a message with
# no rule here shows as lint.py wrote it.
_LINT_WORDS = [
    (r"starts with a greeting", lambda m: "It starts with a greeting."),
    (r"'please'/'could you' framing: (.+)", lambda m: f"Polite framing: {m[1]}."),
    (r"role-play opener", lambda m: "It opens with a role, like \"You are a ...\"."),
    (r"form label: (.+)", lambda m: f"A form label: {m[1]}."),
    (r"step-by-step procedure", lambda m: "It lists numbered steps."),
    (r"grading/rubric language: (.+)", lambda m: f"Grading words: {m[1]}."),
    (r"(\d+) words; casual prose is about 20-60", lambda m: f"{m[1]} words. Casual prompts run about 20 to 60."),
    (r"has a list; casual prose has no lists", lambda m: "It has a list. Casual prompts are plain prose."),
    (r"markdown bold/links/headings", lambda m: "Bold, links or headings make it read like a template."),
    (r"(\d+) requirement words", lambda m: f"{m[1]} requirement words, like must or should. Casual prompts keep one at most."),
    (r"(\d+) words; explicit spec is about 450-1,200", lambda m: f"{m[1]} words. Specs run about 450 to 1,200."),
    (r"all lowercase; explicit spec", lambda m: "All lowercase. Specs need clean writing."),
    (r"no numbered requirements", lambda m: "No numbered requirements."),
    (r"no clear 'done when'", lambda m: "It doesn't say when the work is done."),
    (r"no pasted error/log/stack trace", lambda m: "No pasted error or log."),
    (r"(\d+) words outside the artifact", lambda m: f"{m[1]} words around the pasted error. Keep that to one short line."),
    (r"criteria words outside the artifact", lambda m: "Criteria words around the pasted error. Error reports have none."),
    (r"(\d+) sentences; aim for 1-3", lambda m: f"{m[1]} sentences. Aim for 1 to 3, more only for context."),
    (r"very short; say what a proper solution", lambda m: "Very short. Mercor asks what a proper solution must do and what doesn't count."),
    (r"no must-nots", lambda m: "The goal names no shortcut that doesn't count, like loosening the tests."),
    (r"found (\d+) better and (\d+) worse markers", lambda m: "Few better or worse words. Mercor asks for 2 to 3 points, "
                                                           "each a better and a worse choice."),
    (r"more than 3 points", lambda m: "More than 3 points. Mercor asks for 2 to 3."),
    (r"under 15 words \(Studio minimum\)", lambda m: "Under 15 words, Studio's minimum."),
    (r"(\d+) sentences; aim for 3-5", lambda m: f"{m[1]} sentences. Aim for 3 to 5."),
    (r"generic phrase (.+?); name the mechanism", lambda m: f"A generic phrase, {m[1]}. Mercor asks for the mechanism by name."),
    (r"say where it struggled", lambda m: "Short. Mercor asks where Claude struggled, what went wrong and how much you steered."),
    (r"Operate tasks should describe", lambda m: "Operate tasks describe how to reach the starting state."),
    (r"repeats private goal/decision-point wording: (.+)", lambda m: f"It repeats wording from your goal or decision points: {m[1]}."),
    (r"words that read as AI-written: (.+)", lambda m: f"Words that read as AI-written: {m[1]}."),
    (r"em dashes read as AI-written", lambda m: "Em dashes read as AI-written."),
]


def plain_lint(msg: str) -> str:
    """lint.py's message for one flag, as the editors say it."""
    for rx, words in _LINT_WORDS:
        m = re.search(rx, msg or "")
        if m:
            return words(m)
    return msg


def ratings(r: dict, slot: dict) -> dict:
    one, ts, st = r.get("one_shot"), r.get("task_success"), r.get("steering")

    def check(o, t, s):
        rt = {"one-shot": o or "", "task success": str(t) if t else "", "steering": str(s) if s else ""}
        return lint.check_ratings(rt, slot)

    grid = []
    for t in range(1, 6):
        row = []
        for s in range(1, 6):
            errs = [p for p in check("No", t, s) if p[0] == lint.ERROR]
            row.append(not errs)
        grid.append(row)
    flags = check(one, ts, st) if (one or ts) else []
    errors = [p for p in flags if p[0] == lint.ERROR]
    return {"one_shot": one, "task_success": ts, "steering": st, "claude_minutes": r.get("claude_minutes"),
            "rev": "0", "check": [{"level": a, "field": b, "message": c} for a, b, c in flags],
            "passes": (not errors) if (one and ts) else None, "grid": grid}


# --- Leads, slots, repos -----------------------------------------------------

def _one_shot(lead: dict) -> dict:
    if lead.get("source") == "issue":
        return {"word": "not_checked", "source": "estimate", "why": None}
    level = (lead.get("one_shot_risk") or {}).get("level")
    word = {"low": "unlikely", "medium": "possible", "high": "likely"}.get(level, "not_checked")
    return {"word": word, "source": "estimate", "why": (lead.get("one_shot_risk") or {}).get("why")}


def ranked_leads() -> dict:
    # No leads yet (a fresh checkout): skip ranking, which would sync slots from Studio first.
    has_leads = (config.DATA / "scout" / "leads.json").exists()
    rows = scout_rank.rank(include_dropped=True) if has_leads else []
    out = {"leads": [], "set_aside": [], "claimed": []}
    for i, r in enumerate(rows):
        lead, repo = r["lead"], r.get("repo") or {}
        groups = r.get("groups") or []
        variants = [{"prompt_style": g["slot"]["prompt_style"], "interaction_mode": g["slot"]["interaction_mode"],
                     "count": g["count"], "first_task": (g.get("tasks") or [None])[0],
                     "note": "short to write; you reply to Claude during runs"
                     if g["slot"]["prompt_style"] == "Casual prose" and g["slot"]["interaction_mode"].startswith("Inter")
                     else "long to write; runs alone" if g["slot"]["prompt_style"] == "Explicit spec"
                     else "paste a real error; you reply during runs" if g["slot"]["prompt_style"].startswith("Error")
                     else "short to write; runs alone"}
                    for g in sorted(groups, key=lambda g: -g["count"])]
        fit = lead.get("slot_fit") or {}
        ct = fit.get("codebase_type")
        verdict = (repo.get("verdicts") or {}).get(ct) or {}
        set_aside = None
        if (lead.get("upstream") or {}).get("looks_solved"):
            set_aside = {"reason": "looks_solved", "label": "looks solved upstream"}
        elif verdict.get("overall") == "reject":
            set_aside = {"reason": "repo_fails", "label": "repo fails " + (ct or "")}
        elif lead.get("status") in ("dropped", "discarded"):
            set_aside = {"reason": "hidden", "label": "hidden"}
        elif not groups:
            set_aside = {"reason": "no_slots", "label": "no open slots"}
        chips = []
        if lead.get("live_services"):
            chips.append("needs mocks: " + ", ".join(lead["live_services"]))
        item = {
            "lead": lead, "score": r.get("score"), "rank": i + 1,
            "label": f"{short_repo(lead['repo'])} · {lead.get('title')}",
            "one_shot": _one_shot(lead),
            "combo": {**fit, "open": r.get("open", 0)},
            "variants": variants,
            "repo": {"full_name": repo.get("full_name") or lead["repo"], "language": repo.get("studio_language")
                     or lead.get("primary_language"), "source_files": (repo.get("counts") or {}).get("source_files"),
                     "test_files": (repo.get("counts") or {}).get("test_files"), "license": repo.get("license"),
                     "studio_usage": repo.get("studio_usage", 0), "pinned_url": lead.get("repo_url_pinned"),
                     "commit": lead.get("commit"), "verdict": verdict,
                     "verify": (repo.get("verify") or {}).get("commands", [])},
            "set_aside": set_aside, "check": None, "check_job": None, "chips": chips,
        }
        if lead.get("status") == "claimed":
            out["claimed"].append(item)
        elif set_aside:
            out["set_aside"].append(item)
        else:
            out["leads"].append(item)
    combos = {}
    for g in _slots_file()["groups"]:
        s = g["slot"]
        k = (s["action"], s["domain"], s["codebase_type"])
        c = combos.setdefault(k, {"action": k[0], "domain": k[1], "codebase_type": k[2], "open": 0, "leads": 0})
        c["open"] += g["count"]
    for it in out["leads"]:
        f = it["lead"].get("slot_fit") or {}
        k = (f.get("action"), f.get("domain"), f.get("codebase_type"))
        if k in combos:
            combos[k]["leads"] += 1
    out["combos"] = sorted(combos.values(), key=lambda c: -c["open"])
    return out


def _slots_file() -> dict:
    """data/slots.json, or no slots before the first `johto slots` sync."""
    p = config.DATA / "slots.json"
    return json.loads(p.read_text()) if p.exists() else {"synced_at": None, "groups": []}


def draft_open(meta: dict) -> int:
    """Open Studio tasks with exactly a draft's five seeds, as of the last slots sync."""
    want = slot_line(meta.get("slot") or {})
    return sum(int(g.get("count") or 0) for g in _slots_file().get("groups") or []
               if slot_line(g.get("slot") or {}) == want)


def draft_of(meta: dict) -> dict | None:
    """{since, open} for a task folder with no Studio slot yet, else None."""
    if not workspace.is_draft(meta):
        return None
    return {"since": (meta.get("draft") or {}).get("at") or meta.get("created_at"), "open": draft_open(meta)}


def slots_summary() -> dict:
    s = _slots_file()
    total = sum(g["count"] for g in s["groups"])
    return {"synced_at": s.get("synced_at"), "open": total, "groups": s["groups"]}


def repos() -> list[dict]:
    p = config.DATA / "scout" / "repos.json"
    if not p.exists():
        return []
    leads = json.loads((config.DATA / "scout" / "leads.json").read_text()) if (config.DATA / "scout" / "leads.json").exists() else []
    out = []
    for name, r in json.loads(p.read_text()).items():
        mine = [x for x in leads if x.get("repo", "").lower() == name]
        out.append({
            "full_name": r.get("full_name", name), "language": r.get("studio_language") or r.get("language"),
            "description": r.get("description"),
            "counts": {"source_files": (r.get("counts") or {}).get("source_files"),
                       "test_files": (r.get("counts") or {}).get("test_files")},
            "ai_level": (r.get("ai") or {}).get("level"), "studio_usage": r.get("studio_usage", 0),
            "verdicts": {k: {"overall": v.get("overall"), "fails": v.get("fails", []),
                             "borderline": v.get("borderline", [])} for k, v in (r.get("verdicts") or {}).items()},
            "leads": len(mine), "claimed": sum(1 for x in mine if x.get("status") == "claimed"),
            "pinned_url": r.get("pinned_url"), "stars": r.get("stars"),
        })
    order = {"ok": 0, "maybe": 1, "reject": 2}

    def best(r):
        return min(order.get(v["overall"], 3) for v in r["verdicts"].values()) if r["verdicts"] else 3
    return sorted(out, key=lambda r: (r["ai_level"] == "heavy", best(r), -r["leads"]))


# --- Real tasks ---------------------------------------------------------------

def _lead_by_id(lead_id: str | None) -> dict | None:
    if not lead_id:
        return None
    p = config.DATA / "scout" / "leads.json"
    if not p.exists():
        return None
    return next((x for x in json.loads(p.read_text()) if x.get("id") == lead_id), None)


def _label(meta: dict, lead: dict | None, d: Path | None = None) -> str:
    repo = short_repo(meta["repo"].get("full_name", ""))
    if meta.get("label"):
        return meta["label"]
    title = (lead or {}).get("title") or (_seed_title(d, meta) if d is not None else None) or meta["task_name"]
    return f"{repo} · {title}"


_SEED_TITLES: dict = {}   # short -> (at, title)


def seed_match(d: Path, meta: dict) -> dict | None:
    """For a task claimed outside the brief (no origin or dossier recorded): the brief seed it matches
    (api_discover.match_seed: same action, shared words with his prompt and goal) as {dossier_id, opportunity_id,
    title}, so the rail, the page title and "Where this task came from" name the problem instead of "Task
    t4sk0002". Cached for a minute."""
    if (meta.get("origin") or {}).get("kind") or (meta.get("dossier") or {}).get("id"):
        return None
    hit = _SEED_TITLES.get(d.name)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    found = None
    try:
        from . import api_discover as disc
        repo = (meta.get("repo") or {}).get("full_name", "").lower()
        for bp in sorted((config.DATA / "dossiers").glob("*.brief.json")):
            b = _json(bp) or {}
            if str(b.get("repo") or "").lower() != repo:
                continue
            did = b.get("dossier_id") or bp.name[:-len(".brief.json")]
            doc = _json(config.DATA / "dossiers" / f"{did}.json") or {}
            opps = {o.get("id"): o for o in disc._opps(doc)}
            seeds = [{**x, "fits": [{"action": f.get("action")} for f in (opps.get(x.get("opportunity_id")) or {})
                                    .get("slot_fit") or []]} for x in b.get("seeds") or []]
            m = disc.match_seed((meta.get("slot") or {}).get("action"), disc._task_text(d.name), seeds)
            if m and m.get("title"):
                found = {"dossier_id": did, "opportunity_id": m.get("opportunity_id"), "title": m["title"]}
                break
    except Exception as e:  # noqa: BLE001  (a label is never worth a broken page)
        print(f"johto web: seed match for {d.name} failed: {e}", flush=True)
    _SEED_TITLES[d.name] = (time.time(), found)
    return found


def _seed_title(d: Path, meta: dict) -> str | None:
    m = seed_match(d, meta)
    return m["title"] if m else None


# --- Providers: other builders' modules, used when they are present ---------------------------------------

_MODS: dict[str, tuple] = {}


def _mod(name: str):
    """johto.web.<name> (or johto.<name> for "actions"), or None while it doesn't exist or doesn't import.
    A miss is remembered for 30 s, so a module that lands later is picked up without a restart."""
    hit = _MODS.get(name)
    if hit is not None and (hit[0] is not None or time.time() - hit[1] < 30):
        return hit[0]
    import importlib
    try:
        m = importlib.import_module(f"johto.{name}" if name == "actions" else f"johto.web.{name}")
    except Exception as e:  # missing, or mid-edit: the read model keeps working without it
        if not isinstance(e, ModuleNotFoundError):
            print(f"johto web: {name} did not import: {e}", flush=True)
        m = None
    _MODS[name] = (m, time.time())
    return m


def _lanes():
    a = _mod("actions")
    try:
        return a.L() if a and hasattr(a, "L") else lanes_mod()
    except Exception:
        return lanes_mod()


def lanes_mod():
    from .. import lanes
    return lanes


_RUNNING = {"at": 0.0, "val": set(), "ok": True}


def running_containers() -> set[str]:
    """Running prework containers (cached 2 s). Sets docker_ok()."""
    now = time.time()
    if now - _RUNNING["at"] < 2.0:
        return _RUNNING["val"]
    from .lane import monitor
    L = _lanes()
    ok = True
    try:
        if L is lanes_mod() or not monitor.docker_free():
            # Real lanes, or fake Studio with test lanes under their own prefix: ask Docker, by the lane prefix.
            import subprocess
            r = subprocess.run(["docker", "ps", "--filter", f"name=^{monitor.PREFIX}", "--format", "{{.Names}}"],
                               capture_output=True, text=True, timeout=15)
            ok = r.returncode == 0
            val = {n for n in r.stdout.split() if n.startswith(monitor.PREFIX)} if ok else set()
        else:
            val = set(L.running_lanes())
    except Exception:
        ok, val = False, set()
    _RUNNING.update(at=now, val=val, ok=ok)
    return val


def docker_ok() -> bool:
    running_containers()
    return _RUNNING["ok"]


def claude_alive(container: str) -> bool:
    from .lane import monitor
    L = _lanes()
    if L is not lanes_mod() and monitor.docker_free():
        try:
            return bool(L.claude_active(container))
        except Exception:
            return False
    from .livecheck import claude_alive as alive
    return alive(container)


def jobs_snapshot() -> list[dict]:
    """Every job the app knows, as contract Jobs: the jobs runner, review packs and the lane watcher."""
    out: list[dict] = []
    j = _mod("jobs")
    if j is not None and hasattr(j, "list_jobs"):
        try:
            out += j.list_jobs(limit=200)
        except Exception:
            pass
    from . import api_pack, lane_watch
    for p in list(api_pack.JOBS.values()):
        st = {"running": "running", "done": "succeeded", "failed": "failed"}.get(p.get("state"), p.get("state"))
        out.append({"id": f"pack-{p['id']}", "kind": "pack", "target": f"t:{p['short']}", "short": p["short"],
                    "run": p.get("run"), "pool": "local", "state": st,
                    "progress": {"step": (p.get("log") or [None])[-1], "steps": [], "log_tail": (p.get("log") or [])[-5:]},
                    "created_at": p.get("started_at"), "run_at": None, "started_at": p.get("started_at"),
                    "ended_at": None, "cost_usd": None, "estimate_usd": None,
                    "error": {"code": "pack_failed", "message": p["error"], "details": {}} if p.get("error") else None,
                    "result": {"out": p.get("out"), "tokens": p.get("tokens")} if p.get("out") else None,
                    "cancellable": False})
    for key, w in list(lane_watch.STATE.items()):
        short, _, run = key.partition("/")
        phase = w.get("phase")
        st = {"collecting": "running", "packing": "running", "done": "succeeded", "failed": "failed"}.get(phase, phase)
        at = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(w.get("at") or time.time()))
        # The step is the phase ("collecting" or "packing"), so the UI labels it by phase; the message is a tooltip.
        out.append({"id": f"watch-{short}-{run}", "kind": "lane_collect", "target": f"t:{short}", "short": short,
                    "run": run, "pool": "lane", "state": st, "phase": phase,
                    "progress": {"step": phase if phase in ("collecting", "packing") else None,
                                 "steps": ["collecting", "packing"], "log_tail": [w.get("message") or ""]},
                    "created_at": at, "run_at": None, "started_at": at, "ended_at": at if st in ("succeeded", "failed") else None,
                    "cost_usd": None, "estimate_usd": None,
                    "error": {"code": "collect_failed", "message": w.get("message") or "", "details": {}} if st == "failed" else None,
                    "result": {"pack": w.get("pack")} if w.get("pack") else None, "cancellable": False})
    return out


def studio_state() -> dict:
    j = _mod("jobs")
    w = getattr(j, "worker", None) if j else None
    if w is not None and hasattr(w, "state"):
        try:
            s = w.state()
            return {"state": s.get("state", "ok"), "pending": s.get("pending", 0), "last_ok_at": s.get("last_ok_at")}
        except Exception:
            pass
    return {"state": "ok", "pending": 0, "last_ok_at": None}


def settings() -> dict:
    # calibrate_first: False means Done on the prompt goes straight to the goal, then one validation run.
    s = {"auto_prepare_lane": True, "studio_filling": "automatic", "wip_before_validation": 2, "wip_max": 4,
         "calibrate_first": False}
    p = config.DATA / "web-settings.json"
    try:
        s.update(json.loads(p.read_text()))
    except (OSError, ValueError):
        pass
    return s


# --- Lane --------------------------------------------------------------------------------------------------

def idle_lane(foreign: str | None = None) -> dict:
    return {"state": "free", "rate_limited": None, "since": None, "run": None, "pty": {"alive": False, "clients": 0},
            "question": None, "evidence": None, "facts": None, "input_locked": False, "queue": [],
            "auto_prepare": settings().get("auto_prepare_lane", True),
            "foreign": {"container": foreign} if foreign else None}


def _lane_provider():
    """The lane builder's monitor: api_lane.lane_state (None until its monitor runs), else the service."""
    m = _mod("api_lane")
    fn = getattr(m, "lane_state", None) if m is not None else None
    if callable(fn):
        return fn
    try:
        from .lane import service
        svc = service.svc
        if getattr(svc, "running", False):
            return svc.state
    except Exception:
        pass
    return None


def service_lane() -> dict | None:
    """LaneState from the running lane monitor, or None (then real_lane derives it from files and Docker)."""
    fn = _lane_provider()
    if fn is None:
        return None
    try:
        s = fn()
    except Exception as e:
        print(f"johto web: lane monitor failed: {e}", flush=True)
        return None
    return s if isinstance(s, dict) and s.get("state") else None


def has_lane_service() -> bool:
    return service_lane() is not None


def lane_state(bases: list[dict]) -> dict:
    return service_lane() or real_lane(bases)


def live_lane_state(tpath: Path) -> tuple[str, str | None, str | None]:
    """(state, since, question) for a live transcript: 'waiting' after Claude ends its turn, else 'working'."""
    from .. import transcript as _t
    last_assistant, last_human, last_text = None, None, None
    for e in _domain().facts.entries(tpath)[-400:]:
        if e.get("isSidechain"):
            continue
        if e.get("type") == "assistant":
            last_assistant = e
            for x in (e.get("message") or {}).get("content") or []:
                if isinstance(x, dict) and x.get("type") == "text" and x.get("text", "").strip():
                    last_text = x["text"].strip()
        elif e.get("type") == "user" and _t._human_text(e) is not None:
            last_human, last_assistant = e, None
    if last_assistant and (last_assistant.get("message") or {}).get("stop_reason") == "end_turn":
        return "waiting", last_assistant.get("timestamp"), (last_text or "")[-300:]
    return "working", (last_human or {}).get("timestamp"), None


def real_lane(bases: list[dict]) -> dict:
    """Lane state from the files and Docker, until the lane monitor replaces it: a run is in the lane while its
    container runs and Claude runs in it (or it has ended and isn't collected yet)."""
    running = running_containers()
    known = set()
    for b in bases:
        for r in b.get("runs") or []:
            if r.get("container"):
                known.add(r["container"])
            if not r.get("container_up"):
                continue
            mode = "async" if (b["slot"].get("interaction_mode") or "").startswith("Async") else "interactive"
            run = {"short": b["short"], "run": r["name"], "kind": r["kind"], "label": b["label"], "mode": mode,
                   "container": r.get("container")}
            base = {**idle_lane(), "run": run, "pty": {"alive": bool(r.get("live")), "clients": 0},
                    "facts": _domain().facts.public(r.get("facts"))}
            if r.get("live"):
                tpath = r.get("_tpath")
                if not tpath:
                    cfg = r.get("_cc_config")
                    creds = bool(cfg) and (Path(cfg) / ".credentials.json").exists()
                    return {**base, "state": "ready_for_prompt" if creds else "needs_login", "since": r.get("created_at"),
                            "evidence": "no transcript yet"}
                state, since, q = live_lane_state(Path(tpath))
                return {**base, "state": state, "since": since or r.get("created_at"), "question": q}
            if r.get("has_transcript") and not r.get("collected_at"):
                return {**base, "state": "collecting" if r.get("collecting") else "exited",
                        "since": r.get("ended_at") or r.get("created_at")}
    foreign = sorted(c for c in running if c not in known)
    return idle_lane(foreign[0] if foreign else None)


# --- Real tasks: per-run views ---------------------------------------------------------------------------

def _domain():
    from . import domain
    return domain


def _iso(t: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t)) if t else None


def _jsonl(p: Path) -> list[dict]:
    out = []
    try:
        for line in p.read_text(errors="ignore").splitlines():
            try:
                v = json.loads(line)
            except ValueError:
                continue
            if isinstance(v, dict):
                out.append(v)
    except OSError:
        pass
    return out


def _json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def read_notes(d: Path, run: str) -> list[dict]:
    """Run notes (his words) from tasks/<short>/app/notes-<run>.jsonl; the last line for an id wins."""
    by_id: dict[str, dict] = {}
    for n in _jsonl(d / "app" / f"notes-{run}.jsonl"):
        if n.get("id"):
            by_id[n["id"]] = {**by_id.get(n["id"], {}), **n}
    notes = [{"id": n["id"], "at": n.get("at"), "t_run_s": int(n.get("t_run_s") or 0), "turn": n.get("turn"),
              "text": n.get("text") or ""} for n in by_id.values() if not n.get("deleted")]
    return sorted(notes, key=lambda n: (n["t_run_s"], n["at"] or ""))


def _cc_config(container: str | None) -> Path | None:
    """The lane's Claude config dir on the host (any lane prefix, JOHTO_PREWORK_HOME)."""
    key = lanes_mod().lane_key(container)
    return Path(lanes_mod().HOME) / "cc-config" / f"{key}_model_a" if key else None


def _run_view(d: Path, rec: dict, texts: dict, slot: dict, running: set[str]) -> dict:
    from .. import transcript
    from . import lane_watch
    dom = _domain()
    run_dir = d / rec["dir"]
    name = run_dir.name
    kind, _, n = name.partition("-")
    lane_info = lanes_mod().manifest(run_dir).get("lane") or {}
    container = rec.get("container") or lane_info.get("container")
    up = bool(container) and container in running
    alive = up and claude_alive(container)
    cfg = _cc_config(container)
    live_ts = sorted(((cfg / "projects").glob("*/*.jsonl")) if cfg and (cfg / "projects").is_dir() else [],
                     key=lambda p: p.stat().st_mtime)
    logs = transcript.find(run_dir)
    collected_at = rec.get("collected_at") or (rec.get("auto") or {}).get("collected_at")
    cands = [p for p in (live_ts[-1] if live_ts else None, logs[-1] if logs else None) if p is not None]
    tpath = max(cands, key=lambda p: p.stat().st_mtime) if cands else None
    if alive and live_ts:
        tpath = live_ts[-1]
    n_tx = len(logs) if (collected_at and logs and tpath in logs) else len(live_ts) or len(logs)
    watch = lane_watch.STATE.get(f"{d.name}/{name}") or {}
    diff_p = rec.get("diff") or (run_dir / "prework_output" / "final.diff")
    diff_text = dom.facts.read_diff(diff_p) if collected_at else None
    f = dom.facts.run_facts(tpath, diff_text=diff_text, transcripts=n_tx, opening_prompt=texts.get("opening_prompt"),
                            workspace=lane_info.get("workspace")) if tpath else None
    ended = None
    if tpath and not alive:
        try:
            ended = _iso(tpath.stat().st_mtime)
        except OSError:
            pass
    if rec.get("torn_down_at"):
        state = "torn_down"
    elif alive:
        state = "active"
    elif collected_at:
        state = "collected"
    else:
        state = "exited"
    app = d / "app"
    return {
        "name": name, "kind": kind, "n": int(n) if n.isdigit() else 1, "created_at": rec.get("created_at"),
        "collected_at": collected_at, "state": state, "facts": f, "moments": _json(app / f"moments-{name}.json"),
        "audit": _json(app / "research" / f"audit-{name}.json"), "prompt_sha": rec.get("prompt_sha"),
        "notes": read_notes(d, name), "recommend": None, "live": alive, "container": container,
        "container_up": up, "has_transcript": tpath is not None, "ended_at": ended,
        "collecting": watch.get("phase") == "collecting", "auto": {k: v for k, v in watch.items() if k != "at"} or None,
        "has_diff": bool(collected_at and diff_text is not None), "diff": str(diff_p) if diff_text is not None else None,
        "packs": sorted(p.name for p in run_dir.glob("review-pack-*.xml")),
        "_tpath": str(tpath) if tpath else None, "_cc_config": str(cfg) if cfg else None,
        "_workspace": lane_info.get("workspace"),
    }


def _ratings_of(sub: dict, slot: dict, minutes) -> dict:
    rt = sub["ratings"]

    def num(v):
        m = re.match(r"\s*(\d)", v or "")
        return int(m.group(1)) if m else None
    one = (rt.get("one-shot") or "").strip().capitalize() or None
    rdata = {"one_shot": one if one in ("Yes", "No") else None,
             "task_success": num(rt.get("task success")), "steering": num(rt.get("steering"))}
    cm = (rt.get("claude minutes") or "").strip()
    rdata["claude_minutes"] = float(cm) if re.fullmatch(r"\d+(\.\d+)?", cm) else minutes
    r = ratings(rdata, slot)
    r["rev"] = hashlib.sha1(json.dumps(rdata, sort_keys=True).encode()).hexdigest()[:12]
    return r


FIELD_SECTION = {"field_opening_prompt": "opening_prompt", "field_high_level_goal": "goal",
                 "field_rationale_difficulty": "decision_points", "field_codebase_overview": "codebase",
                 "field_initial_state": "initial_state", "field_self_check": "validation_notes",
                 "field_writer_submission_notes": "reviewer_notes", "field_one_shot": "ratings",
                 "field_task_success": "ratings", "field_steering": "ratings", "field_claude_minutes": "ratings",
                 "field_conversation_log": "gists", "field_final_output": "gists"}


def sha12(v) -> str:
    a = _mod("actions")
    if a is not None and hasattr(a, "sha12"):
        return a.sha12(v)
    s = v if isinstance(v, str) else json.dumps(v, sort_keys=True)
    return hashlib.sha1(s.strip().encode()).hexdigest()[:12]


def changed_since_push(meta: dict, sub: dict, minutes) -> list[str] | None:
    """Section keys ("ratings", "gists" for those fields) whose Studio value changed since the last save,
    or None when nothing was saved yet. Same hashes as actions.push."""
    pushed = meta.get("pushed") or {}
    if not pushed.get("at"):
        return None
    try:
        local = workspace.to_fields(meta, sub, minutes)
    except Exception:
        return None
    old = pushed.get("fields") or {}
    keys = []
    for k, v in local.items():
        if sha12(v) != old.get(k):
            sk = FIELD_SECTION.get(k, k)
            if sk not in keys:
                keys.append(sk)
    return keys


def _qc_view(meta: dict) -> dict | None:
    qs = [q for q in meta.get("qc") or [] if (q.get("spec") or "AutoQC") == "AutoQC"]
    if not qs:
        return None
    q = qs[-1]
    fk = _domain().checklist.field_for_dimension
    dims = [{**x, "field_key": x.get("field_key") or fk(x.get("dimension") or "", x.get("justification") or "")}
            for x in q.get("dims") or []]
    return {"audit_id": q.get("audit_id"), "spec": q.get("spec") or "AutoQC", "status": q.get("status") or "running",
            "started_at": q.get("started_at"), "finished_at": q.get("finished_at"),
            "counts": q.get("counts") or {"pass": 0, "fail": 0, "neutral": 0}, "dims": dims}


def _my_min(d: Path) -> float | None:
    ms = sum(int(e.get("focus_ms") or 0) for e in _jsonl(d / "app" / "authorship.jsonl"))
    return round(ms / 60000, 1) if ms else None


def _updated_at(d: Path) -> str | None:
    ts = [p.stat().st_mtime for p in (d / "task.json", d / "submission.md") if p.exists()]
    return _iso(max(ts)) if ts else None


def _runmon():
    from .lane import runmon
    return runmon


def submission_run(meta: dict, d: Path) -> str | None:
    """The run the submission uses: actions.submission_run (the one rule), else the same rule here: the run Austin
    chose (task.json submission_run), else the latest collected validation run."""
    return _runmon().submission_run(meta, d)


def monitor_of(d: Path, meta: dict, local_up: dict | None = None) -> dict | None:
    """The run monitor for a task's current run (lane/runmon.py). Never waits on the PC: in the web app it is the
    last measurement; without the monitor thread (tests, the CLI) it measures this Mac's runs inline."""
    try:
        return _runmon().for_task(d, meta, local_up=local_up)
    except Exception as e:
        print(f"johto web: run monitor failed for {d.name}: {e}", flush=True)
        return None


def task_base(d: Path, running: set[str] | None = None) -> dict:
    """What every view of a task needs: files read, runs with facts. Cheap enough for each queue rebuild."""
    meta = workspace.load(d)
    sub = workspace.parse_submission(d)
    lead = _lead_by_id(meta.get("lead_id"))
    slot = meta["slot"]
    texts = {k: sub["sections"].get(h, "") for k, h in SECTION_KEYS.items()}
    running = running_containers() if running is None else running
    runs = [_run_view(d, rec, texts, slot, running) for rec in meta.get("runs", [])]
    total = round(sum((r.get("facts") or {}).get("active_min") or 0 for r in runs), 1)
    dom = _domain()
    for r in runs:
        if r["facts"]:
            r["facts"]["task_claude_min"] = total
        if r["moments"] is None:
            r["moments"] = dom.facts.moments(r["facts"], kind=r["kind"], slot=slot)
        if r["kind"] == "calib":
            r["recommend"] = dom.facts.recommend(r["facts"], slot)
    base = {"short": d.name, "dir": d, "meta": meta, "sub": sub, "lead": lead, "slot": slot, "texts": texts,
            "runs": runs, "label": _label(meta, lead, d), "claude_min": total, "draft": draft_of(meta)}
    mon = monitor_of(d, meta, {r["name"]: r["container_up"] for r in runs})
    base["monitor"] = mon
    if mon and mon.get("host_name") and mon.get("up") and not mon.get("collected") \
            and mon.get("state") in ("working", "waiting", "background", "ready"):
        for r in runs:
            if r["name"] == mon.get("run") and not r["live"]:
                r["remote_live"] = True     # a lane on the PC (or one the Mac's Docker check missed)
    base["submission_run"] = submission_run(meta, d)   # actions.submission_run: the one rule
    sr = dom.derive.submission_run(base)
    rec = next((x for x in meta.get("runs", []) if Path(x["dir"]).name == (sr or {}).get("name")), {})
    minutes = (workspace.run_minutes(rec, d) if rec else None) or ((sr or {}).get("facts") or {}).get("active_min")
    base["ratings"] = _ratings_of(sub, slot, minutes)
    base["changed_since_push"] = changed_since_push(meta, sub, minutes)
    base["sub_run"] = sr
    return base


def task_state(base: dict, lane: dict, jobs: list[dict]) -> dict:
    """The base plus its derived stage (derive.stage)."""
    st = _domain().derive.stage({**base, "lane": lane, "jobs": jobs})
    return {**base, "stage": st, "claimed_at": base["meta"].get("created_at")}


def summary_of(ts: dict) -> dict:
    meta, st = ts["meta"], ts["stage"]
    studio = meta.get("studio") or {}
    draft = draft_of(meta)
    return {
        "short": ts["short"], "task_id": meta.get("task_id"), "task_name": meta["task_name"], "label": ts["label"],
        "repo": meta["repo"], "slot": ts["slot"], "stage": st["stage"], "stage_label": st["label"],
        "stage_run": st.get("run"),
        "studio": {"status": "Draft" if draft else (studio.get("status") or "Writing"),
                   "url": (f"{config.STUDIO}/annotator/tasks/{meta['task_id']}/?campaignId={config.CAMPAIGN_ID}"
                           if meta.get("task_id") else None)},
        "draft": draft,
        "lead_id": meta.get("lead_id"), "claude_min": ts["claude_min"], "my_min": _my_min(ts["dir"]),
        "updated_at": _updated_at(ts["dir"]), "submission_run": submission_run(meta, ts["dir"]),
        "submission_run_chosen": meta.get("submission_run"),
        "dossier": _dossier_ref(meta), "monitor": _runmon().public(ts.get("monitor")),
    }


def _opportunity(meta: dict) -> dict | None:
    """The repo-research opportunity a task was claimed from (AI research for the reference pane), with the
    task's repo and commit, so its evidence pointers open in the code view."""
    d = meta.get("dossier") or {}
    if not d.get("id") or not d.get("opportunity_id"):
        return None
    a = _mod("actions")
    try:
        _, opp = a.load_opportunity(d["id"], d["opportunity_id"])
    except Exception:
        return None
    if not opp:
        return None
    repo = meta.get("repo") or {}
    return {**opp, "repo": repo.get("full_name"), "commit": repo.get("commit")}


def _dossier_ref(meta: dict) -> dict | None:
    """Links to the repo page and the chosen opportunity, for tasks claimed from a dossier."""
    d = meta.get("dossier") or {}
    if not d.get("id"):
        return None
    repo_href = f"/repos/{d['id']}"
    opp = d.get("opportunity_id")
    return {"id": d["id"], "opportunity_id": opp, "repo_href": repo_href,
            "opportunity_href": f"{repo_href}#o={opp}" if opp else None}


def _public_run(r: dict) -> dict:
    out = {k: v for k, v in r.items() if not k.startswith("_")}
    out["facts"] = _domain().facts.public(r.get("facts"))
    return out


def _prompt_lock(ts: dict) -> str | None:
    dom = _domain()
    meta = ts["meta"]
    used = [r for r in ts["runs"] if dom.derive.used(r)]
    if not used:
        return None
    last = max((r.get("created_at") for r in used), key=lambda x: dom.derive.ts(x) or 0)
    calibs = [r for r in used if r["kind"] == "calib"]
    if calibs:
        d = (meta.get("decisions") or {}).get(calibs[-1]["name"]) or {}
        if isinstance(d, dict) and d.get("decision") == "rework" and not dom.derive.after(last, d.get("at")):
            return None
    if dom.derive.after(meta.get("prompt_unlocked_at"), last):
        return None
    names = [dom.queue.run_word(r["name"]) for r in used]
    return "sent in " + (", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0])


def _discard_view(ts: dict) -> dict | None:
    st = ts["stage"]["stage"]
    meta = ts["meta"]
    if st not in ("dropped", "discarded"):
        return None
    disc = meta.get("discard") if isinstance(meta.get("discard"), dict) else {}
    reason = disc.get("reason") or "easy"
    a = _mod("actions")
    url = disc.get("url")
    if not url and a is not None and hasattr(a, "discard_link"):
        try:
            url = a.discard_link(ts["dir"], meta, reason)
        except Exception:
            url = None
    reasons = getattr(a, "DISCARD_REASONS", {}) if a else {}
    runs = [r for r in ts["runs"] if r.get("facts")]
    f = (runs[-1]["facts"] if runs else None) or {}
    repo = meta.get("repo") or {}
    facts = {"Task": meta["task_name"], "Repo": f"{repo.get('full_name')} @{(repo.get('commit') or '')[:8]}",
             "Reason": reasons.get(reason, reason), "Slot": slot_line(ts["slot"])}
    if f:
        facts["Model and effort"] = "Opus 5.5 - high" if f.get("model_ok") and f.get("effort_ok") else "check the run"
        facts["Turns after the prompt"] = str(f.get("follow_ups", 0))
        facts["Minutes"] = str(round(ts["claude_min"] or 0))
        if reason == "easy" and not f.get("follow_ups"):
            facts["Outcome"] = "Solved on the first try (one-shot)"
    return {"url": url, "logged_at": disc.get("logged_at"), "at": disc.get("at"), "reason": reason, "facts": facts,
            "yours": ["In one or two lines: what specifically was the task?",
                      "In one or two lines: why did you think the task would be hard?",
                      "In one or two lines: did the model make any mistakes at all? If so what?"]}


def _upstream(ts: dict) -> list[dict]:
    text = (ts["texts"].get("codebase") or "") + "\n" + (ts["texts"].get("reviewer_notes") or "")
    out = []
    for x in ((ts["lead"] or {}).get("upstream") or {}).get("related", []) or []:
        url = x.get("url") or ""
        m = re.search(r"/(?:issues|pull)/(\d+)", url)
        hit = bool(url and url in text) or bool(m and re.search(rf"#{m.group(1)}\b", text))
        out.append({"url": url, "title": x.get("title"), "state": x.get("state"), "mentioned": hit})
    return out


def _checklist(ts: dict, sections: dict, qc: dict | None) -> list[dict]:
    a = _mod("actions")
    if a is not None and hasattr(a, "checklist"):
        try:
            return a.checklist(ts["dir"])
        except Exception as e:
            print(f"johto web: actions.checklist failed for {ts['short']}: {e}", flush=True)
    flags = [f for s in sections.values() for f in s.get("lint") or []]
    return _domain().checklist.rows(slot=ts["slot"], sections=sections, ratings=ts["ratings"], sub_run=ts["sub_run"],
                                    qc=qc, pushed=ts["meta"].get("pushed"), changed=ts["changed_since_push"],
                                    gists=ts["meta"].get("gists") or {}, upstream=_upstream(ts), lint_flags=flags)


def task_detail(ts: dict) -> dict:
    """TaskDetail (build-plan section 4) for one task state."""
    meta, slot, texts = ts["meta"], ts["slot"], ts["texts"]
    lock = _prompt_lock(ts)
    sections = {k: section(k, v, slot, others=texts, locked=lock if k == "opening_prompt" else None,
                           rev=workspace_rev(v))
                for k, v in texts.items() if k != "initial_state" or slot.get("action") == "Operate"}
    try:   # the provenance guard: red 6-word overlap with the AI text this task showed him
        from .guard import overlap as _overlap
        for k, sec in sections.items():
            sec["overlap"] = _overlap.check(ts["dir"], k, sec["text"])
    except Exception as e:
        print(f"johto web: overlap check failed for {ts['short']}: {e}", flush=True)
    qc = _qc_view(meta)
    st = meta.get("studio") or {}
    summary = summary_of(ts)
    snap = _json(ts["dir"] / "app" / "studio-fields.json") or {}
    return {
        "summary": summary, "sections": sections, "ratings": ts["ratings"],
        "runs": [_public_run(r) for r in ts["runs"]],
        "decisions": meta.get("decisions") or {}, "ready": meta.get("ready"),
        "studio": {"status": summary["studio"]["status"], "saved_at": (meta.get("pushed") or {}).get("at"),
                   "changed_since_save": ts["changed_since_push"] or [], "feedback": st.get("feedback"),
                   "qc": qc, "url": summary["studio"]["url"], "polled_at": st.get("polled_at"),
                   "times_submitted": st.get("times_submitted") or 0, "fields": snap.get("fields"),
                   "fields_at": snap.get("at"), "draft": summary["draft"]},
        "gists": meta.get("gists", {}), "lead": ts["lead"], "opportunity": None if ts["lead"] else _opportunity(meta),
        "checklist": _checklist(ts, sections, qc),
        "upstream_mentions": _upstream(ts), "discard": _discard_view(ts),
        "fingerprints": {"algo": "fnv1a32-6gram-v1", "hashes": []},
        "submission_run": summary["submission_run"], "submission_run_chosen": meta.get("submission_run"),
        "monitor": ts.get("monitor"),
        "stage_info": {k: v for k, v in ts["stage"].items() if k in ("run", "since", "chosen", "skipped", "rework")},
        "brief_md": (ts["dir"] / "brief.md").read_text() if (ts["dir"] / "brief.md").exists() else "",
    }


def workspace_rev(text: str) -> str:
    fn = getattr(workspace, "rev", None)
    if callable(fn):
        try:
            return fn(text)
        except Exception:
            pass
    return hashlib.sha1((text or "").strip().encode()).hexdigest()[:12]


# --- Real tasks: the snapshot every view and the queue come from -------------------------------------------

_LAST: dict = {"at": 0.0, "lane": None}


def _bases() -> list[dict]:
    running = running_containers()
    out = []
    for p in sorted(config.TASKS.glob("*/task.json")):
        try:
            out.append(task_base(p.parent, running))
        except Exception as e:  # a broken folder should not take the app down
            print(f"johto web: task {p.parent.name} unreadable: {e}", flush=True)
    return out


def blockers(bases: list[dict], lane: dict) -> list[dict]:
    """Global tier-0 rows: Studio signed out, Docker down while a run needs it, disk under 10 GB."""
    q = _domain().queue
    out = []
    ss = studio_state()
    if ss["state"] in ("signed_out", "error"):
        out.append(q._item("blocker_studio", 0, "blocker", "Studio tab is signed out" if ss["state"] == "signed_out"
                           else "Studio calls are failing", label="Open Studio",
                           reason=f"{ss.get('pending') or 0} Studio calls are waiting",
                           primary={"label": "Open Studio", "action": "studio_open_tab", "args": {}},
                           facts=(ss["state"],)))
    need_docker = any(r.get("container") and not r.get("collected_at") and not r.get("torn_down_at") for b in bases
                      for r in b["runs"]) or any((b["meta"].get("ready") or {}).get("kind") for b in bases)
    if need_docker and not docker_ok():
        out.append(q._item("blocker_docker", 0, "blocker", "Docker isn't running", label="Open Docker",
                           reason="Lanes run in Docker containers",
                           primary={"label": "Open Docker", "action": "open_docker", "args": {}}, facts=("docker",)))
    try:
        import shutil
        free = shutil.disk_usage(config.ROOT).free / 1e9
        if free < 10:
            out.append(q._item("blocker_disk", 0, "blocker", f"Disk has {free:.0f} GB free", label="Free some space",
                               reason="Lanes and clones need room; under 10 GB is risky", facts=("disk", int(free))))
    except OSError:
        pass
    return out


def item_prefs() -> dict:
    p = config.DATA / "web-item-prefs.json"
    v = _json(p) or {}
    now = time.time()
    held = {k: t for k, t in (v.get("held") or {}).items() if t > now}
    return {"hidden": set(v.get("hidden") or {}), "held": held}


def real_snapshot() -> dict:
    """Everything real mode shows: task states, summaries, lane, jobs, queue and running rows."""
    bases = _bases()
    lane = lane_state(bases)
    jobs = jobs_snapshot()
    states = [task_state(b, lane, jobs) for b in bases]
    summaries = [summary_of(s) for s in states]
    try:
        lr = ranked_leads()
        usable = len(lr["leads"])
    except Exception:
        usable = 0
    try:
        slots_open = slots_summary()["open"]
    except Exception:
        slots_open = 0
    svc = service_lane() is not None
    snap = {"tasks": states, "lane": lane, "blockers": blockers(bases, lane),
            "leads": {"usable": usable, "open": slots_open}, "prefs": item_prefs(), "settings": settings(),
            "lane_service": svc, "studio": studio_state(), "now": time.time()}
    q, running = _domain().queue.build_queue(snap)
    _LAST.update(at=time.time(), lane=lane, jobs=jobs)
    return {"states": states, "summaries": summaries, "lane": lane, "jobs": jobs, "queue": q, "running": running,
            "lane_service": svc}


def overview() -> list[dict]:
    """Home's task list as one plain row per task, in Home's order ({short, title, needs_you, machine, action,
    command, priority}): the shared helper STATE's where.overview() uses, so `johto where` ranks like Home."""
    snap = real_snapshot()
    return _domain().queue.overview_rows(snap["queue"], snap["running"])


def real_task(d: Path) -> dict:
    """TaskDetail for one real task. Uses the last lane and job snapshot when it is fresh (under 3 s)."""
    if time.time() - _LAST["at"] < 3 and _LAST.get("lane") is not None:
        lane, jobs = _LAST["lane"], _LAST.get("jobs") or []
        base = task_base(d)
    else:
        bases = _bases()
        lane, jobs = lane_state(bases), jobs_snapshot()
        base = next((b for b in bases if b["short"] == d.name), None) or task_base(d)
    st = task_state(base, lane, jobs)
    out = task_detail(st)
    out["lane_need"] = lane_need_of(st, lane)
    return out


def lane_need_of(st: dict, lane: dict | None) -> dict | None:
    """What this task's live run asks of Austin, from the same rule Now's queue uses (queue._lane_need), so the
    task page and Now agree on whether Claude waits on him: {kind, question, run} or None."""
    try:
        need = _domain().queue._lane_need(st, lane or {}, time.time())
    except Exception as e:  # noqa: BLE001
        print(f"johto web: lane need failed for {st.get('short')}: {e}", flush=True)
        return None
    if not need:
        return None
    mon = st.get("monitor") or {}
    q = need.get("question") or mon.get("question") or ((lane or {}).get("question") if need.get("local") else None)
    return {"kind": need["kind"], "question": q, "run": need.get("run")}


def real_tasks() -> list[dict]:
    """TaskDetail for every real task."""
    snap = real_snapshot()
    return [task_detail(s) for s in snap["states"]]


def real_run(d: Path, run: str) -> dict | None:
    r = next((r for r in task_base(d)["runs"] if r["name"] == run), None)
    return _public_run(r) if r else None


def run_transcript_path(d: Path, run: str) -> tuple[Path | None, str | None]:
    """The transcript a run's views read (live while Claude runs, else the collected copy) and its workspace."""
    r = next((r for r in task_base(d)["runs"] if r["name"] == run), None)
    if not r:
        raise KeyError(run)
    return (Path(r["_tpath"]) if r.get("_tpath") else None), r.get("_workspace")


def write_section(short: str, key: str, text: str) -> None:
    """Replace one section's text in submission.md, keeping its HTML comments."""
    with _write_lock(short):
        _write_section(short, key, text)


def _write_section(short: str, key: str, text: str) -> None:
    d = config.TASKS / short
    path = d / "submission.md"
    src = path.read_text()
    heading = SECTION_KEYS[key]
    pat = re.compile(rf"(^## {re.escape(heading)}[ \t]*\n)(.*?)(?=^## |\Z)", re.S | re.M)
    m = pat.search(src)
    if not m:
        src = src.rstrip() + f"\n\n## {heading}\n{text.strip()}\n\n"
    else:
        comments = "".join(c + "\n" for c in re.findall(r"<!--.*?-->", m.group(2), re.S))
        body = (text.strip() + "\n" if text.strip() else "") + comments + "\n"
        src = src[:m.start()] + m.group(1) + body + src[m.end():]
    _replace(path, src)


_WRITE_LOCKS: dict[str, threading.Lock] = {}


def _write_lock(short: str) -> threading.Lock:
    return _WRITE_LOCKS.setdefault(short, threading.Lock())


def _replace(path: Path, text: str) -> None:
    """Atomic write with a temp name of its own (two saves at once must not share one temp file)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_ratings(short: str, r: dict) -> None:
    with _write_lock(short):
        _write_ratings(short, r)


def _write_ratings(short: str, r: dict) -> None:
    d = config.TASKS / short
    path = d / "submission.md"
    src = path.read_text()
    vals = {"One-shot": r.get("one_shot") or "", "Task Success": str(r.get("task_success") or ""),
            "Steering": str(r.get("steering") or "")}
    for k, v in vals.items():
        src = re.sub(rf"^{re.escape(k)}:.*$", f"{k}: {v}".rstrip(), src, count=1, flags=re.M)
    _replace(path, src)
