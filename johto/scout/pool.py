"""Promise-ranked pool of vetted repos that feeds the dossier worker.

Spec: docs/dossier-spec.md section 6. Ranking reads only local files (data/scout/repos.json,
data/slots.json, the awesome-selfhosted clone), so `ranked_pool()` is fast and never touches
the network or Studio. `grow()` fills repos.json with the existing discover and vet code
(GitHub API through `gh` only; it never runs Claude).

    python -m johto.scout.pool              # top of the pool
    python -m johto.scout.pool explain owner/name
    python -m johto.scout.pool grow --target 40
    python -m johto.scout.pool snapshot     # write data/scout/pool.json
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import math
import re
import sys
import time

from .. import config
from . import discover, github, vet

POOL_FILE = config.DATA / "scout" / "pool.json"
SLOTS_FILE = config.DATA / "slots.json"
CTYPES = ("Personal-scale", "Enterprise-grade")
CTYPE_SHORT = {"Personal-scale": "personal-scale", "Enterprise-grade": "enterprise-grade"}
DOMAINS = ("Backend / API", "Web frontend", "DevOps / cloud", "Dev tooling", "Data / DB", "Games",
           "Other software", "AI agents")

LANE_LANGS = {"Go", "TypeScript", "JavaScript", "Python"}
JS_FAMILY = {"Vue", "Svelte", "Astro", "MDX"}  # primary language reported by GitHub, built with Node

# Share of the promise score each factor can earn (sums to 1).
WEIGHTS = {"slots": 0.24, "language": 0.14, "size": 0.14, "stateful": 0.16, "tests": 0.12,
           "run": 0.06, "activity": 0.07, "issues": 0.07}

# Words in topics, description or awesome-selfhosted tags that place a repo in a domain.
DOMAIN_WORDS = {
    "Games": r"games?|gaming|game[- ]engine|roguelike|roguelite|chess|puzzle[- ]games?|card[- ]games?|"
             r"board[- ]games?|multiplayer[- ]games?|mmorpg|rpg|godot|phaser|pygame|ebiten|io[- ]game|"
             r"html5[- ]games?|browser[- ]games?|game[- ]development|gamedev",
    "Dev tooling": r"developer[- ]tools?|devtools?|developers?|ide|code[- ]review|git|gitops|github|gitlab|lsp|"
                   r"language[- ]server|linter|debugger|api[- ]client|http[- ]client|postman|code[- ]search|"
                   r"snippets?|load[- ]testing|mock[- ]server|package[- ]registry|cli|terminal|tui|"
                   r"software development[\w ,&-]*|git[- ]server|issue[- ]tracker",
    "DevOps / cloud": r"devops|kubernetes|k8s|docker|containers?|monitoring|observability|uptime|status[- ]page|"
                      r"paas|gitops|ci[/-]?cd|continuous integration|deploy(ment)?s?|infrastructure|homelab|"
                      r"backups?|reverse[- ]proxy|proxy|dns|vpn|log[- ]management|logging|incident[\w-]*|"
                      r"alerting|feature[- ]flags?|feature toggle|secrets?[- ]management|self-hosting solutions|"
                      r"web servers|network utilities|remote access|automation|serverless|cloud|ansible|"
                      r"terraform|helm|configuration management|infrastructure as code|iac|sre|"
                      r"container management|server management|self-hosting|vps",
    "Data / DB": r"databases?|database management|sql|analytics|business[- ]intelligence|etl|elt|"
                 r"data[- ]pipelines?|data[- ]visuali[sz]ation|dashboards?|personal dashboards|time[- ]series|"
                 r"data[- ]catalog|spreadsheets?|low[- ]code|no[- ]code|nocode|airtable|search[- ]engines?|"
                 r"olap|data[- ]warehouse|reporting|web[- ]analytics|product[- ]analytics",
    "AI agents": r"llms?|ai[- ]agents?|agents?|agentic|rag|chatgpt|gpt|openai|ollama|langchain|chatbots?|genai|"
                 r"generative (ai|artificial intelligence)[\w ()-]*|large language models?|mcp",
    "Other software": r"home[- ]automation|iot|internet of things[\w ()-]*|smart[- ]home|media[\w ,&-]*|photos?|"
                      r"photo galleries|gallery|music|audio|podcasts?|e-?books?|document management[\w ,&-]*|"
                      r"desktop[\w-]*|electron|tauri|password[- ]managers?|file[- ]sharing|file transfer[\w ,&-]*|"
                      r"file[- ]sync|calendar[\w ,&-]*|contacts|caldav|carddav|recipes?|recipe management|"
                      r"personal[- ]finance|finance|budget\w*|money[\w ,&-]*|fitness|health[\w ,&-]*|"
                      r"discord[- ]bot|telegram[- ]bot|bots?|video[\w ,&-]*|streaming|maps?|gps|scrobbl\w*|"
                      r"feed readers?|bookmarks[\w ,&-]*",
    "Web frontend": r"react|reactjs|vue|vuejs|svelte|sveltekit|nextjs|next\.js|nuxt|angular|pwa|web[- ]app|"
                    r"frontend|kanban|whiteboard|markdown[- ]editor|note[- ]taking[\w ,&-]*|notes|"
                    r"local[- ]first|offline[- ]first",
    "Backend / API": r"api|rest[- ]?api|graphql|server|backend|self-?hosted|platform|crm|erp|cms|"
                     r"content management systems[\w ()]*|helpdesk|ticketing|e-?commerce|forum|wiki|wikis|"
                     r"activitypub|fediverse|email[\w ,&-]*|mail|chat|messaging|communication[\w ,&-]*|booking|"
                     r"invoic\w*|accounting|inventory[\w ,&-]*|project[- ]management|resource planning|"
                     r"pastebins?|polls and events|task management[\w ,&-]*",
}
# Words that many apps use in passing ("ships a docker image", "has a dashboard"): weak evidence.
WEAK_WORDS = {
    "DevOps / cloud": r"docker|docker compose|containers?|cloud|automation|proxy|logging|secrets?|deploy|"
                      r"homelab|self-hosting|vps",
    "Data / DB": r"dashboards?|reporting|sql|personal dashboards",
    "AI agents": r"mcp|agents?|gpt|ai agents?",
    "Dev tooling": r"cli|terminal|developers?|github|gitlab|ide",
    "Backend / API": r"platform|server|api",
    "Other software": r"bots?|maps?|video|media|desktop|health",
    "Games": r"rpg|steam",
}
_WEAK_RX = {d: re.compile(rf"^({w})$", re.I) for d, w in WEAK_WORDS.items()}
_DOMAIN_RX = {d: re.compile(rf"^({w})$") for d, w in DOMAIN_WORDS.items()}
_DOMAIN_RX_TEXT = {d: re.compile(rf"\b({w})\b", re.I) for d, w in DOMAIN_WORDS.items()}


# --- Inputs --------------------------------------------------------------------------

def _open_slots() -> dict:
    """Open slot counts by (action, domain, ctype) and (domain, ctype), from data/slots.json only."""
    try:
        groups = json.loads(SLOTS_FILE.read_text()).get("groups", [])
    except (OSError, json.JSONDecodeError):
        groups = []
    combo: dict[tuple, int] = {}
    dc: dict[tuple, int] = {}
    for g in groups:
        s, n = g["slot"], g.get("count", 0)
        if not n:
            continue
        combo[(s["action"], s["domain"], s["codebase_type"])] = combo.get(
            (s["action"], s["domain"], s["codebase_type"]), 0) + n
        dc[(s["domain"], s["codebase_type"])] = dc.get((s["domain"], s["codebase_type"]), 0) + n
    return {"combo": combo, "dc": dc}


_ash_cache: dict = {"mtime": None, "tags": {}}


def _ash_tags() -> dict[str, list[str]]:
    """full_name -> awesome-selfhosted tags, read from the local clone (never refreshed here)."""
    root = discover.ASH_DIR / "software"
    marker = discover.ASH_DIR / ".fetched"
    try:
        mtime = marker.stat().st_mtime
    except OSError:
        return {}
    if _ash_cache["mtime"] != mtime:
        tags: dict[str, list[str]] = {}
        for f in root.glob("*.yml"):
            d = discover._parse_yml(f.read_text(errors="ignore"))
            m = re.match(r"https://github\.com/([^/]+/[^/#?]+)", d.get("source_code_url") or "")
            if m:
                tags[m.group(1).removesuffix(".git").lower()] = list(d.get("tags") or [])
        _ash_cache.update(mtime=mtime, tags=tags)
    return _ash_cache["tags"]


def _days_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 86400


# --- Domain fit ----------------------------------------------------------------------

def domain_fit(rec: dict, ash: dict | None = None) -> dict[str, dict]:
    """Domain -> {"strength": 0..1, "why": str}. Strength >= 0.5 counts as a fit."""
    ash = _ash_tags() if ash is None else ash
    topics = [t.lower() for t in rec.get("topics") or []]
    tags = list(ash.get(rec["full_name"], []))
    for f in rec.get("found_via") or []:
        tags += (f.get("ash_tags") or [])
    tags = [t.lower() for t in dict.fromkeys(tags)]
    tags_split = [p.strip() for t in tags for p in re.split(r"\s+-\s+|\(|\)", t) if p.strip()]
    desc = rec.get("description") or ""
    tr = rec.get("traits") or {}
    out: dict[str, dict] = {}

    def put(d: str, s: float, why: str) -> None:
        if s > out.get(d, {}).get("strength", 0):
            out[d] = {"strength": round(min(s, 1.0), 2), "why": why}

    for d in DOMAINS:
        rx, weak = _DOMAIN_RX[d], _WEAK_RX.get(d)

        def strong(t: str) -> bool:
            return not (weak and weak.match(t.replace("-", " ")))
        hits = [t for t in topics if rx.match(t.replace("-", " ")) or rx.match(t)]
        hit_topic = next((t for t in hits if strong(t)), None)
        hit_tags = [t for t in tags_split if rx.match(t)]
        hit_tag = next((t for t in hit_tags if strong(t)), None)
        descs = [m.group(0).lower() for m in _DOMAIN_RX_TEXT[d].finditer(desc)]
        hit_desc = next((t for t in descs if strong(t)), None)
        if hit_topic:
            put(d, 1.0, f"topic {hit_topic}")
        elif hits:
            put(d, 0.55, f"topic {hits[0]}")
        if hit_tag:
            put(d, 1.0, f"listed under {hit_tag}")
        elif hit_tags:
            put(d, 0.55, f"listed under {hit_tags[0]}")
        if hit_desc:
            put(d, 0.7, f"described as {hit_desc}")
        elif descs:
            put(d, 0.5, f"described as {descs[0]}")

    fe, srv = tr.get("frontend_files", 0), tr.get("server_files", 0)
    if fe >= 60:
        put("Web frontend", 1.0, f"{fe} frontend files")
    elif fe >= 15:
        put("Web frontend", 0.6, f"{fe} frontend files")
    has_data = bool(tr.get("stores") or tr.get("orm") or tr.get("migrations"))
    if srv >= 40 and has_data:
        put("Backend / API", 1.0, f"{srv} server files with a database")
    elif srv >= 15:
        put("Backend / API", 0.6, f"{srv} server files")
    if tr.get("migrations") and (tr.get("orm") or len(tr.get("stores") or []) >= 2 or tr.get("sql_files", 0) >= 10):
        put("Data / DB", 0.55, "schema migrations and a data layer")
    if tr.get("ai_deps"):
        put("AI agents", 0.55, "calls LLM APIs (" + ", ".join(tr["ai_deps"][:2]) + ")")
    if tr.get("game_deps") or tr.get("godot"):
        put("Games", 1.0, "game engine " + ", ".join(tr.get("game_deps") or ["Godot"]))
    if any("administrative utilities" in t for t in tags) and out.get("Games", {}).get("why", "").startswith(
            "listed under"):
        # awesome-selfhosted "Games - Administrative Utilities": game-server panels, not games.
        out["Games"] = {"strength": 0.5, "why": "game-server administration"}
        put("DevOps / cloud", 1.0, "game-server administration")
    # Any real application can host an "Other software" task.
    put("Other software", 0.5, "end-user application")
    return out


# --- Scoring -------------------------------------------------------------------------

def _lang_score(lang: str | None) -> tuple[float, str]:
    if lang in LANE_LANGS:
        return 1.0, f"{lang}, which the lane can build"
    if lang in JS_FAMILY:
        return 0.85, f"{lang} (Node toolchain)"
    if lang:
        return 0.3, f"{lang}: the lane image lacks its toolchain"
    return 0.2, "primary language unknown"


def _size_score(n: int) -> float:
    if 150 <= n <= 3000:
        return 1.0
    if 3000 < n <= 6000:
        return 0.75
    if n > 6000:
        return 0.5
    if n >= 80:
        return 0.65
    if n >= 40:
        return 0.4
    return 0.2 if n >= 20 else 0.0


def _tests_score(rec: dict) -> float:
    t = rec["counts"]["test_files"]
    s = 1.0 if t >= 50 else 0.8 if t >= 20 else 0.6 if t >= 8 else 0.35 if t >= 1 else (
        0.1 if rec["verify"]["commands"] else 0.0)
    if rec["verify"].get("ci_workflows"):
        s += 0.1
    return min(s, 1.0)


def _run_score(rec: dict) -> tuple[float, str | None]:
    tr = rec.get("traits") or {}
    compose = tr.get("compose_files") or [p for p in rec.get("docker") or [] if p != "Dockerfile"]
    if compose:
        root = [c for c in compose if "/" not in c]
        return 1.0, f"runs locally with {(root or compose)[0]}"
    if "Dockerfile" in (rec.get("docker") or []):
        return 0.7, "runs locally from its Dockerfile"
    if rec["verify"]["commands"]:
        return 0.5, None
    return 0.2, None


def _stateful(rec: dict) -> tuple[float, list[str]]:
    tr = rec.get("traits") or {}
    s, bits = 0.0, []
    stores = [x for x in tr.get("stores") or [] if x != "browser database"]
    if stores:
        s += min(0.25 * len(stores), 0.5)
        bits.append(" + ".join(stores[:3]))
    if "browser database" in (tr.get("stores") or []):
        s += 0.15
        bits.append("browser-side database")
    if tr.get("orm") or tr.get("migrations"):
        s += 0.25
        bits.append("schema migrations" if tr.get("migrations") else "ORM " + tr["orm"][0])
    elif tr.get("sql_files", 0) >= 10:
        s += 0.1
        bits.append(f"{tr['sql_files']} SQL files")
    if tr.get("jobs"):
        s += 0.15
        bits.append("background jobs")
    if tr.get("realtime"):
        s += 0.15
        bits.append("realtime (" + tr["realtime"][0] + ")")
    if tr.get("sync_paths", 0) >= 3:
        s += 0.1
        bits.append("sync code")
    if tr.get("multi_user_paths", 0) >= 5:
        s += 0.15
        bits.append("users and permissions")
    return min(s, 1.0), bits


def _activity(rec: dict) -> tuple[float, float, str]:
    d = _days_since(rec.get("pushed_at"))
    act = 0.0 if d is None else 1.0 if d <= 30 else 0.85 if d <= 183 else 0.4 if d <= 365 else 0.0
    o = rec.get("open_issues") or 0
    iss = 0.0 if rec.get("has_issues") is False else (1.0 if o >= 100 else 0.8 if o >= 30 else 0.55 if o >= 10
                                                       else 0.3 if o >= 1 else 0.0)
    if d is None:
        when = "push date unknown"
    elif d < 1:
        when = "pushed today"
    elif d < 2:
        when = "pushed yesterday"
    elif d < 60:
        when = f"pushed {int(d)} days ago"
    else:
        when = f"pushed {int(d // 30)} months ago"
    return act, iss, f"{when}, " + ("1 open issue or PR" if o == 1 else f"{o:,} open issues and PRs")


def _passing_ctypes(rec: dict, dc_open: dict) -> dict[str, dict]:
    """Codebase types whose verdict has no failed check and that have open slots."""
    out = {}
    for ct, v in (rec.get("verdicts") or {}).items():
        if v.get("fails"):
            continue
        if not any(n for (d, c), n in dc_open.items() if c == ct):
            continue
        out[ct] = v
    return out


FAST_GROWTH_EXCLUDE = 5.0   # source files per day since the repo was created
FAST_GROWTH_PENALTY = 1.5


def _velocity(rec: dict) -> dict | None:
    """Source files per day of repo age, for repos under two years old with 300+ files."""
    age = _days_since(rec.get("created_at"))
    n = rec["counts"]["source_files"]
    if age is None or age > 730 or n < 300:
        return None
    return {"files": n, "months": max(1, int(age // 30)), "files_per_day": round(n / max(age, 30), 2)}


LIBRARY_DESC = re.compile(
    r"\b(sdk|library|libraries|framework for|addon|add-on|plugin for|bindings|wrapper|python package|npm package|"
    r"(streams?|utilities|helpers|primitives) for (python|go|golang|node(\.js)?|javascript|typescript|rust)|"
    r"fine-?tun\w+|emnlp|neurips|iclr|icml|acl 20\d\d|arxiv|our paper)\b", re.I)
LIBRARY_TOPICS = {"library", "python-library", "sdk", "framework", "npm-package", "ember-addon", "addon",
                  "component-library", "react-component", "vue-component", "golang-library", "research"}
APP_DESC = re.compile(r"\b(self-?hosted|application|web app|server|platform|dashboard|game)\b", re.I)


def _library_like(rec: dict) -> str | None:
    desc = rec.get("description") or ""
    topics = {t.lower() for t in rec.get("topics") or []}
    hit = topics & LIBRARY_TOPICS
    if hit:
        return f"reads as a library (topic {sorted(hit)[0]})"
    m = LIBRARY_DESC.search(desc)
    if m and not (APP_DESC.search(desc) and m.group(0).lower() in ("framework for", "plugin for", "wrapper")):
        return f"reads as a library or research code ('{m.group(0)}' in the description)"
    return None


def _soft_flags(rec: dict) -> list[dict]:
    """Penalties for repos that are real apps but awkward for a lane task."""
    desc = (rec.get("description") or "").lower()
    topics = {t.lower() for t in rec.get("topics") or []}
    out = []
    if "mcp server" in desc or topics & {"mcp-server", "mcp-servers"}:
        out.append({"points": 0.15, "detail": "an MCP server: a thin tool around another service"})
    if "roblox" in topics or "roblox" in desc:
        out.append({"points": 0.2, "detail": "a Roblox game: it only runs inside Roblox Studio"})
    if topics & {"gtk", "gtk4", "gnome-desktop", "gioui", "qt", "pyqt", "pyside6", "winui", "swiftui"}:
        out.append({"points": 0.08, "detail": "a native desktop GUI, hard to exercise in the lane"})
    return out


def _exclusions(rec: dict, passing: dict) -> list[str]:
    why = []
    if rec.get("fork"):
        why.append("fork of " + (rec.get("fork_parent") or "another repo"))
    if rec.get("swe_bench"):
        why.append("SWE-bench repo")
    if rec.get("archived"):
        why.append("archived")
    ai = rec.get("ai") or {}
    if ai.get("level", "none") != "none":
        bits = list(ai.get("agent_files") or [])[:3]
        if ai.get("ai_commits"):
            bits.append(f"{ai['ai_commits']} of {ai.get('commits_sampled', '?')} recent commits AI co-authored")
        why.append("AI-authorship signals: " + ", ".join(bits))
    if (rec.get("studio_usage") or 0) > 2:
        why.append(f"already used in {rec['studio_usage']} Studio tasks")
    if rec.get("tree_error"):
        why.append("pinned commit unreadable")
    lib = _library_like(rec)
    if lib:
        why.append(lib)
    vel = _velocity(rec)
    if vel and vel["files_per_day"] > FAST_GROWTH_EXCLUDE:
        why.append(f"grew to {vel['files']:,} source files in {vel['months']} months, which suggests AI generation")
    if not passing:
        fails = sorted({f for v in (rec.get("verdicts") or {}).values() for f in v.get("fails", [])} - {"ai_generated"})
        if fails or not why:
            why.append("fails the codebase check: " + ", ".join(fails or ["no codebase type with open slots"]))
    return why


def assess(rec: dict, opened: dict | None = None, ash: dict | None = None) -> dict:
    """Score one vetted repo. Returns eligibility, promise, reasons, fits and the factor breakdown."""
    opened = opened or _open_slots()
    dc_open, combo_open = opened["dc"], opened["combo"]
    passing = _passing_ctypes(rec, dc_open)
    excluded = _exclusions(rec, passing)
    domains = domain_fit(rec, ash)

    # Slot fit: open slots for every (domain, codebase type) the repo fits, weighted by strength.
    # A clean pass (no borderline check) beats a borderline one: a 1,300-file repo that only
    # squeaks under the personal-scale limit should not be pitched for personal-scale slots.
    clean = {ct: v for ct, v in passing.items() if v.get("overall") == "ok"}
    pairs = []
    for ct, v in (clean or passing).items():
        w_ct = 1.0 if v.get("overall") == "ok" else 0.7
        for d, info in domains.items():
            if info["strength"] < 0.5:
                continue
            n = dc_open.get((d, ct), 0)
            if n:
                pairs.append({"domain": d, "codebase_type": ct, "open": n, "weight": n * info["strength"] * w_ct})
    pairs.sort(key=lambda p: -p["weight"])
    best = pairs[0]["weight"] if pairs else 0.0
    total = sum(p["weight"] for p in pairs)
    top_dc = max(dc_open.values()) if dc_open else 1
    slot_s = (0.75 * min(1.0, math.log1p(best) / math.log1p(top_dc))
              + 0.25 * min(1.0, math.log1p(total) / math.log1p(3 * top_dc))) if pairs else 0.0
    if not pairs and not excluded:
        excluded.append("no open slots in any domain it fits")

    fits = []
    for p in pairs:
        for (a, d, ct), n in combo_open.items():
            if d == p["domain"] and ct == p["codebase_type"]:
                fits.append({"action": a, "domain": d, "codebase_type": ct, "open": n,
                             "strength": domains[d]["strength"],
                             **({} if ct in clean else {"borderline": True})})
    fits.sort(key=lambda f: (-f["open"] * f["strength"] * (0.7 if f.get("borderline") else 1), f["action"]))

    lang_s, lang_why = _lang_score(rec.get("language"))
    n_src = rec["counts"]["source_files"]
    size_s = _size_score(n_src)
    tests_s = _tests_score(rec)
    run_s, run_why = _run_score(rec)
    state_s, state_bits = _stateful(rec)
    act_s, iss_s, act_why = _activity(rec)

    if pairs:
        p0 = pairs[0]
        others = [f"{p['domain']} {p['open']}" for p in pairs[1:4] if p["domain"] != p0["domain"]]
        fit_why = (f"Fits {p0['open']} open {p0['domain']} {CTYPE_SHORT[p0['codebase_type']]} slots"
                   + (f"; also {', '.join(dict.fromkeys(others))}" if others else ""))
    else:
        fit_why = "No open slots it fits"
    components = [
        ("slots", slot_s, fit_why),
        ("language", lang_s, lang_why),
        ("size", size_s, f"{n_src:,} source files, {rec['counts']['test_files']:,} test files"),
        ("stateful", state_s, ", ".join(state_bits) if state_bits else "no database or background work found"),
        ("tests", tests_s, f"{rec['counts']['test_files']:,} test files"
         + (", CI" if rec["verify"].get("ci_workflows") else "")),
        ("run", run_s, run_why or "no docker-compose or Dockerfile"),
        ("activity", act_s, act_why),
        ("issues", iss_s, act_why),
    ]
    breakdown = [{"factor": f, "weight": WEIGHTS[f], "score": round(s, 3), "points": round(WEIGHTS[f] * s, 4),
                  "detail": why} for f, s, why in components]

    penalties = []
    best_ct = pairs[0]["codebase_type"] if pairs else None
    borders = (passing.get(best_ct) or {}).get("borderline", []) if best_ct else []
    for b, pts, why in (("size", 0.03, "size is borderline for the codebase type"),
                        ("english", 0.06, "README may not be in English"),
                        ("self_contained", 0.05, "README mentions paid API keys"),
                        ("application", 0.06, "may be a library rather than an application")):
        if b in borders:
            penalties.append({"points": pts, "detail": why})
    penalties += _soft_flags(rec)
    used = rec.get("studio_usage") or 0
    if used:
        penalties.append({"points": 0.04 * used, "detail": f"already used in {used} Studio task{'s' * (used > 1)}"})
    age = _days_since(rec.get("created_at"))
    vel = _velocity(rec)
    if vel and vel["files_per_day"] > FAST_GROWTH_PENALTY:
        penalties.append({"points": 0.12, "detail": f"grew to {vel['files']:,} source files in {vel['months']} "
                                                    "months; check the history for AI generation"})
    elif age is not None and age < 365:
        penalties.append({"points": 0.05, "detail": f"created {max(1, int(age // 30))} months ago"})

    promise = sum(b["points"] for b in breakdown) - sum(p["points"] for p in penalties)
    promise = round(max(0.0, min(1.0, promise)), 3)

    # Reasons: slot fit first, then the strongest other factors, then penalties.
    reasons = [fit_why, lang_why]
    reasons.append(f"{n_src:,} source files, {rec['counts']['test_files']:,} test files")
    if state_bits:
        reasons.append(", ".join(state_bits[:4]))
    if run_why:
        reasons.append(run_why)
    reasons.append(act_why)
    reasons += [p["detail"] for p in penalties]
    reasons = [r[:1].upper() + r[1:] for r in reasons if r]

    return {
        "full_name": rec["full_name"], "display_name": rec.get("display_name", rec["full_name"]),
        "commit": rec.get("commit"), "pinned_url": rec.get("pinned_url"),
        "language": rec.get("language"), "studio_language": rec.get("studio_language"),
        "eligible": not excluded, "excluded": excluded, "promise": promise, "reasons": reasons,
        "fits": fits[:12], "codebase_types": sorted(passing),
        "codebase_types_clean": sorted(ct for ct, v in passing.items() if v.get("overall") == "ok"),
        "domains": {d: v for d, v in sorted(domains.items(), key=lambda kv: -kv[1]["strength"])},
        "stars": rec.get("stars"), "source_files": n_src, "test_files": rec["counts"]["test_files"],
        "breakdown": breakdown, "penalties": penalties,
    }


# --- Public API ----------------------------------------------------------------------

def _all(include_excluded: bool = False) -> list[dict]:
    opened, ash = _open_slots(), _ash_tags()
    out = [assess(r, opened, ash) for r in vet.load_repos().values() if r.get("counts")]
    if not include_excluded:
        out = [a for a in out if a["eligible"]]
    out.sort(key=lambda a: (-a["promise"], -(a["stars"] or 0), a["full_name"]))
    return out


def ranked_pool(limit: int = 40) -> list[dict]:
    """Eligible repos, best promise first.

    Each item: full_name, display_name, commit, pinned_url, language, studio_language,
    promise (0-1), reasons [str], fits [{action, domain, codebase_type, open, strength}],
    codebase_types [str] (no failed check), codebase_types_clean [str] (no borderline check either),
    domains {domain: {strength, why}}, stars, source_files, test_files, rank.
    """
    keep = ("full_name", "display_name", "commit", "pinned_url", "language", "studio_language", "promise",
            "reasons", "fits", "codebase_types", "codebase_types_clean", "domains", "stars", "source_files",
            "test_files")
    ranked = _all()
    out = []
    for i, a in enumerate(ranked[:limit] if limit else ranked, 1):
        item = {k: a[k] for k in keep}
        item["rank"] = i
        out.append(item)
    return out


def explain(full_name: str) -> dict:
    """Why a repo has its promise score, or why it is not in the pool."""
    key = full_name.lower().strip().removeprefix("https://github.com/").strip("/")
    rec = vet.load_repos().get(key)
    if not rec:
        return {"full_name": key, "eligible": False, "excluded": ["not vetted yet"], "rank": None}
    a = assess(rec)
    ranked = _all()
    a["rank"] = next((i for i, x in enumerate(ranked, 1) if x["full_name"] == key), None)
    a["pool_size"] = len(ranked)
    a["verdicts"] = rec.get("verdicts")
    a["traits"] = rec.get("traits")
    a["ai"] = rec.get("ai")
    a["found_via"] = rec.get("found_via")
    return a


def snapshot(path=POOL_FILE) -> dict:
    """Write the full ranked pool and the excluded repos (with reasons) to data/scout/pool.json."""
    everything = _all(include_excluded=True)
    eligible = [a for a in everything if a["eligible"]]
    try:
        synced = json.loads(SLOTS_FILE.read_text()).get("synced_at")
    except (OSError, json.JSONDecodeError):
        synced = None
    doc = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "slots_synced_at": synced,
        "vetted": len(everything),
        "eligible": len(eligible),
        "items": ranked_pool(limit=0),
        "excluded": [{"full_name": a["full_name"], "why": a["excluded"]} for a in everything if not a["eligible"]],
        "stats": stats(eligible),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=1))
    tmp.replace(path)
    return doc


def stats(eligible: list[dict] | None = None) -> dict:
    eligible = _all() if eligible is None else eligible
    by_lang: dict[str, int] = {}
    by_ct: dict[str, int] = {}
    by_dom: dict[str, int] = {}
    for a in eligible:
        by_lang[a["language"] or "?"] = by_lang.get(a["language"] or "?", 0) + 1
        for ct in a["codebase_types"]:
            label = ct if ct in a["codebase_types_clean"] else f"{ct} (borderline)"
            by_ct[label] = by_ct.get(label, 0) + 1
        for d, v in a["domains"].items():
            if v["strength"] >= 0.5 and (d != "Other software" or v["strength"] > 0.5):
                by_dom[d] = by_dom.get(d, 0) + 1
    srt = lambda d: dict(sorted(d.items(), key=lambda kv: -kv[1]))  # noqa: E731
    return {"count": len(eligible), "by_language": srt(by_lang), "by_codebase_type": srt(by_ct),
            "by_domain": srt(by_dom),
            "lane_friendly": sum(1 for a in eligible if a["language"] in LANE_LANGS | JS_FAMILY)}


# --- Growing the pool ----------------------------------------------------------------

def _candidate_priority(c: dict, dc_open: dict, ash: dict) -> float:
    """Cheap pre-vet guess at how much a candidate is worth vetting."""
    hint = c.get("hint") or {}
    if hint.get("domain"):
        demand = dc_open.get((hint["domain"], hint["codebase_type"]), 0)
    else:  # awesome-selfhosted: best domain its tags suggest, either codebase type
        fake = {"full_name": c["full_name"], "topics": c.get("topics") or [], "description": c["description"],
                "found_via": [{"ash_tags": hint.get("ash_tags") or []}]}
        demand = max((dc_open.get((d, ct), 0) * v["strength"] for d, v in domain_fit(fake, ash).items()
                      for ct in CTYPES), default=0)
    s = math.log1p(demand)
    s *= 1.0 if c.get("language") in LANE_LANGS else 0.8 if c.get("language") in JS_FAMILY else 0.25
    stars = c.get("stars") or 0
    s *= 1.0 if 150 <= stars <= 40000 else 0.8 if stars >= 40 else 0.5
    age = _days_since(c.get("created_at"))
    if age is not None and age < 365:
        s *= 0.35  # young repos carry the most AI-authorship risk
    if re.search(r"\bai[- ]powered\b|\bvibe\b|🤖|✨", c.get("description") or "", re.I) and hint.get(
            "domain") != "AI agents":
        s *= 0.5
    if re.search(r"\b(self-?hosted|platform|open[- ]source alternative|team|collaborat\w*|sync)\b",
                 c.get("description") or "", re.I):
        s *= 1.15
    return s


def candidates(min_open: int = 15, use_search: bool = True, use_ash: bool = True,
               domains: list[str] | None = None, log=print) -> list[dict]:
    """Discovery for the pool: pool queries per (domain, codebase type) with open slots, plus
    awesome-selfhosted. Highest-demand groups are searched first (search results cache 3 days).
    `domains` limits both sources to those domains."""
    opened = _open_slots()
    dc_open = opened["dc"]
    ash = _ash_tags()
    seen: dict[str, dict] = {}

    def add(c: dict) -> None:
        prev = seen.get(c["full_name"])
        if prev is None:
            c["hints"] = [c["hint"]]
            seen[c["full_name"]] = c
        else:
            prev["hints"].append(c["hint"])
            if "awesome-selfhosted" in c["source"] and "awesome-selfhosted" not in prev["source"]:
                prev["source"] += "+awesome-selfhosted"

    if use_search:
        groups = sorted(((n, d, ct) for (d, ct), n in dc_open.items()
                         if n >= min_open and (not domains or d in domains)), reverse=True)
        for n, d, ct in groups:
            got = discover.search_pool(d, ct)
            log(f"  search {d} | {ct} (open {n}): {len(got)} results")
            for c in got:
                add(c)
    if use_ash:
        for c in discover.ash_pool():
            if domains:
                fake = {"full_name": c["full_name"], "topics": c.get("topics") or [],
                        "description": c["description"], "found_via": [{"ash_tags": c["hint"]["ash_tags"]}]}
                fit = domain_fit(fake, ash)
                best = max(domains, key=lambda d: fit.get(d, {}).get("strength", 0))
                if fit.get(best, {}).get("strength", 0) < 0.7:
                    continue
                c["hint"] = {**c["hint"], "domain": best, "codebase_type": "Enterprise-grade"
                             if (c["stars"] or 0) >= 1000 else "Personal-scale"}
            add(c)
    out = []
    for c in seen.values():
        if not discover._plausible(c) or not discover._recent(c, 365):
            continue
        c["priority"] = max(_candidate_priority({**c, "hint": h}, dc_open, ash) for h in c["hints"])
        out.append(c)
    out.sort(key=lambda c: -c["priority"])
    return out


def _lane(a: dict) -> bool:
    return a["language"] in LANE_LANGS | JS_FAMILY


def _eligible_count() -> int:
    return sum(1 for a in _all() if _lane(a))


def domain_counts(min_strength: float = 0.7) -> dict[str, int]:
    """Eligible lane-friendly repos per domain with real evidence (not the 'any app' default)."""
    out: dict[str, int] = {}
    for a in _all():
        if not _lane(a):
            continue
        for d, v in a["domains"].items():
            if v["strength"] >= min_strength:
                out[d] = out.get(d, 0) + 1
    return out


def grow(target: int = 40, max_vet: int = 250, workers: int = 4, min_open: int = 15,
         use_search: bool = True, use_ash: bool = True, domains: list[str] | None = None, log=print) -> dict:
    """Vet new candidates, best first, until `target` lane-friendly repos are eligible (or, with
    `domains`, until each of those domains has `target` of them).

    Buckets of candidates (one per domain and codebase type) are interleaved in proportion to
    their open slots, so one big group does not eat the budget. Only GitHub GETs through `gh`;
    never Claude, never Studio.
    """
    refresh_traits(log=log)

    def short() -> list[str]:
        if domains:
            c = domain_counts()
            return [d for d in domains if c.get(d, 0) < target]
        return [] if _eligible_count() >= target else ["all"]

    missing = short()
    log(f"pool: {_eligible_count()} eligible lane-friendly repos; by domain {domain_counts()}")
    if not missing:
        return {"eligible": _eligible_count(), "vetted": 0, "errors": 0}
    repos = vet.load_repos()
    cands = [c for c in candidates(min_open, use_search, use_ash, domains, log=log) if c["full_name"] not in repos]
    log(f"pool: {len(cands)} new candidates")
    dc_open = _open_slots()["dc"]
    buckets: dict[str, list[dict]] = {}
    for c in cands:
        hs = [h for h in c["hints"] if not domains or h.get("domain") in domains] or c["hints"]
        h = max(hs, key=lambda h: dc_open.get((h.get("domain"), h.get("codebase_type")), 0))
        key = f"{h['domain']}|{h['codebase_type']}" if h.get("domain") else "awesome-selfhosted"
        buckets.setdefault(key, []).append(c)
    weight = {k: (dc_open.get(tuple(k.split("|")), 0) if "|" in k else 120) for k in buckets}
    credit = {k: 0.0 for k in buckets}

    def next_batch(n: int) -> list[dict]:
        out = []
        while len(out) < n:
            live = [k for k in buckets if buckets[k] and (not domains or k.split("|")[0] in missing)]
            if not live:
                break
            for k in live:
                credit[k] += weight[k]
            k = max(live, key=lambda k: credit[k])
            credit[k] -= sum(weight[k2] for k2 in live)
            out.append(buckets[k].pop(0))
        return out

    vetted, errors, start = 0, 0, time.time()
    batch = max(workers * 3, 8)
    while missing and vetted < max_vet:
        chunk = next_batch(min(batch, max_vet - vetted))
        if not chunk:
            break
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_vet_one, c): c for c in chunk}
            for f in cf.as_completed(futs):
                c = futs[f]
                rec, err = f.result()
                vetted += 1
                if err:
                    errors += 1
                    log(f"  ERR  {c['full_name']}: {err[:120]}")
                    continue
                a = assess(rec)
                mark = "POOL" if a["eligible"] else "no  "
                why = f"{a['promise']:.2f}" if a["eligible"] else "; ".join(a["excluded"])[:110]
                log(f"  {mark} {rec['display_name']:<40} {rec.get('language') or '?':<11} "
                    f"src={rec['counts']['source_files']:<5} {why}")
        missing = short()
        log(f"pool: {_eligible_count()} eligible after {vetted} vetted ({errors} errors, "
            f"{int(time.time() - start)} s); short: {missing or 'none'}")
    snapshot()
    return {"eligible": _eligible_count(), "vetted": vetted, "errors": errors, "by_domain": domain_counts()}


def _vet_one(c: dict) -> tuple[dict | None, str | None]:
    h = c["hints"][0]
    found = {"source": c["source"], "query": h.get("query"), "domain": h.get("domain"),
             "codebase_type": h.get("codebase_type"), "ash_tags": h.get("ash_tags") or None}
    found = {k: v for k, v in found.items() if v}
    try:
        return vet.vet(c["full_name"], found_via=found), None
    except github.GitHubError as e:
        return None, str(e)
    except (KeyError, TypeError, ValueError, OSError) as e:  # odd repo metadata; skip it
        return None, f"{type(e).__name__}: {e}"


def refresh_traits(all_repos: bool = False, workers: int = 4, log=print) -> int:
    """Re-vet stored records (those without traits, or all of them), keeping their pinned commit.
    Cheap: trees, commit lists and manifests at a pinned commit are cached forever."""
    keys = [k for k, r in vet.load_repos().items() if all_repos or "traits" not in r]

    def one(k: str) -> None:
        try:
            vet.vet(k, force=True, keep_commit=True)
        except github.GitHubError as e:
            log(f"  ERR  {k}: {e}")
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, keys))
    if keys:
        log(f"pool: re-vetted {len(keys)} stored repos at their pinned commits")
    return len(keys)


# --- CLI -----------------------------------------------------------------------------

def _print_pool(items: list[dict]) -> None:
    for a in items:
        fit = a["fits"][0] if a["fits"] else None
        fit_s = f"{fit['action']} | {fit['domain']} | {fit['codebase_type']} ({fit['open']})" if fit else "-"
        print(f"{a['rank']:>3}. {a['promise']:.2f}  {a['display_name']:<40} {a['language'] or '?':<11} "
              f"src {a['source_files']:<5} {fit_s}")
        print("       " + " · ".join(a["reasons"][2:5]))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m johto.scout.pool")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("show")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("explain")
    p.add_argument("repo")
    sub.add_parser("snapshot")
    sub.add_parser("stats")
    p = sub.add_parser("grow")
    p.add_argument("--target", type=int, default=40)
    p.add_argument("--max-vet", type=int, default=250)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--min-open", type=int, default=15)
    p.add_argument("--no-search", action="store_true")
    p.add_argument("--no-ash", action="store_true")
    p.add_argument("--domain", action="append", choices=DOMAINS,
                   help="grow only these domains, until each has --target eligible repos")
    p = sub.add_parser("refresh", help="re-vet every stored repo at its pinned commit")
    a = ap.parse_args(argv)
    log = lambda *x: print(*x, flush=True)  # noqa: E731
    if a.cmd == "explain":
        print(json.dumps(explain(a.repo), indent=1))
    elif a.cmd == "snapshot":
        d = snapshot()
        print(f"wrote {POOL_FILE}: {d['eligible']} eligible of {d['vetted']} vetted")
    elif a.cmd == "stats":
        print(json.dumps({**stats(), "by_domain_evidence": domain_counts()}, indent=1))
    elif a.cmd == "grow":
        print(json.dumps(grow(a.target, a.max_vet, a.workers, a.min_open, not a.no_search, not a.no_ash,
                              a.domain, log=log)))
    elif a.cmd == "refresh":
        refresh_traits(all_repos=True, log=log)
        snapshot()
    else:
        items = ranked_pool(getattr(a, "limit", 40))
        if getattr(a, "json", False):
            print(json.dumps(items, indent=1))
        else:
            _print_pool(items)


if __name__ == "__main__":
    main(sys.argv[1:])
