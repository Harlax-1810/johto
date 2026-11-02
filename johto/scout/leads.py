"""Problem leads for a vetted repository and a slot.

Two sources:
- issue mining: open GitHub issues that fit the slot's action, without a merged fix;
- agent leads: headless Claude Code reads a shallow clone at the pinned commit and returns
  terse factual leads as JSON.

Johto requires the writer's own words in every submitted field and runs AI detection on
them. Leads are therefore facts and pointers only (files, mechanisms, how to verify). They
never contain a drafted prompt, goal, or decision points.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from .. import config
from . import github, vet

LEADS_FILE = config.DATA / "scout" / "leads.json"
CLONES = config.DATA / "cache" / "clones"
CLAUDE_BIN = Path(config.CLAUDE_BIN)   # JOHTO_CLAUDE_BIN; a dead path in fake mode
RUNS_LOG = config.DATA / "scout" / "agent_runs.jsonl"
LIKELIHOOD_TO_RISK = {"unlikely": "low", "possible": "medium", "likely": "high"}
LIVE_SERVICES = re.compile(
    r"\b(discord|spotify|last\.?fm|youtube|ytmusic|plex|jellyfin|google|gmail|aws|s3|stripe|paypal|twitter|"
    r"telegram|slack|github api|openai|anthropic|oauth|api key|webhook from|apple music|tidal|deezer|"
    r"steam|twitch|reddit|mastodon instance)\b", re.I)

ACTION_LABELS = {
    "Modify": r"bug|defect|regression|crash|perf|performance|broken|data loss|sync",
    "Create": r"enhancement|feature|feature[- ]request|idea|proposal|new feature",
    "Operate": r"docker|deploy|deployment|ci|infra|helm|kubernetes|k8s|config|installation|install|ops|packaging",
    "Understand / Assess": r"security|performance|architecture|discussion|question|tech[- ]debt|refactor",
}
SKIP_LABELS = re.compile(r"good first issue|beginner|easy|wontfix|won't fix|duplicate|invalid|"
                         r"needs[- ]info|stale|question|support|documentation|docs|translation|i18n", re.I)
STOP = set("a an the to of in on for and or with is are be not no it its this that from by at as "
           "when if into after before should can cannot can't doesn't does don't do via add support "
           "allow option new feature bug issue request make use using".split())

# --- Store -------------------------------------------------------------------


def load() -> list[dict]:
    return json.loads(LEADS_FILE.read_text()) if LEADS_FILE.exists() else []


def save(leads: list[dict]) -> None:
    LEADS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEADS_FILE.write_text(json.dumps(leads, indent=1))


def upsert(new: list[dict]) -> list[dict]:
    leads = load()
    by_id = {l["id"]: l for l in leads}
    for l in new:
        if l["id"] in by_id:
            keep = by_id[l["id"]].get("status", "new")
            by_id[l["id"]].update(l)
            by_id[l["id"]]["status"] = keep
        else:
            leads.append(l)
            by_id[l["id"]] = l
    save(leads)
    return new


def lead_id(repo: str, title: str, source: str) -> str:
    return "L" + hashlib.sha1(f"{repo}|{source}|{title.lower()}".encode()).hexdigest()[:7]


def find(lead_id_: str) -> dict | None:
    return next((l for l in load() if l["id"] == lead_id_ or l["id"].startswith(lead_id_)), None)


def set_status(lead_id_: str, status: str) -> dict | None:
    leads = load()
    for l in leads:
        if l["id"] == lead_id_ or l["id"].startswith(lead_id_):
            l["status"] = status
            save(leads)
            return l
    return None


# --- Helpers -----------------------------------------------------------------

def _keywords(text: str, n: int = 5) -> list[str]:
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", text.lower()) if w not in STOP]
    seen, out = set(), []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:n]


def _overlap(a: str, b: str) -> float:
    ka, kb = set(_keywords(a, 50)), set(_keywords(b, 50))
    return len(ka & kb) / max(len(ka), 1)


def _commit_date(repo: str, sha: str) -> str | None:
    try:
        cs = github.commits(repo, sha, 1)
        return cs[0]["commit"]["committer"]["date"] if cs else None
    except github.GitHubError:
        return None


def upstream_check(repo: str, title: str, pinned_sha: str, branch: str, extra: str = "",
                   exclude_issue: int | None = None) -> dict:
    """Search issues, PRs and later commits for related work on this lead."""
    kws = _keywords(title + " " + extra, 4)
    related, solved = [], False
    if kws:
        q = f"repo:{repo} " + " ".join(kws[:3])
        try:
            items = github.search_issues(q, per_page=8)
        except github.GitHubError:
            items = []
        for it in items:
            if exclude_issue and it.get("number") == exclude_issue:
                continue
            is_pr = "pull_request" in it
            merged = bool(is_pr and (it.get("pull_request") or {}).get("merged_at"))
            state = "merged" if merged else it.get("state")
            ov = _overlap(title, it.get("title", ""))
            if ov >= 0.25:
                related.append({"url": it["html_url"], "title": it["title"][:120],
                                "state": state, "kind": "pr" if is_pr else "issue"})
                if (merged or (not is_pr and it.get("state") == "closed"
                               and it.get("state_reason") == "completed")) and ov >= 0.5:
                    solved = True
    since = _commit_date(repo, pinned_sha)
    if since and kws:
        try:
            later = github.commits_since(repo, branch, since, 100)
        except github.GitHubError:
            later = []
        for c in later:
            if c["sha"] == pinned_sha:
                continue
            msg = c["commit"]["message"].splitlines()[0]
            if _overlap(title, msg) >= 0.5:
                related.append({"url": c["html_url"], "title": msg[:120], "state": "commit-after-pin",
                                "kind": "commit"})
                solved = True
    return {"related": related[:6], "looks_solved": solved, "keywords": kws}


# --- Issue mining ------------------------------------------------------------

def mine_issues(rec: dict, slot: dict, max_leads: int = 5) -> list[dict]:
    repo = rec["display_name"]
    if not rec.get("has_issues", True):
        return []
    want = re.compile(ACTION_LABELS.get(slot["action"], ACTION_LABELS["Modify"]), re.I)
    labels = [l for l in github.labels(repo) if want.search(l) and not SKIP_LABELS.search(l)]
    issues: dict[int, dict] = {}
    for lab in labels[:3]:
        try:
            for i in github.open_issues(repo, lab, 50):
                issues[i["number"]] = i
        except github.GitHubError:
            pass
    if not labels:
        try:
            for i in github.open_issues(repo, None, 50):
                issues[i["number"]] = i
        except github.GitHubError:
            pass
    cutoff = (dt.date.today() - dt.timedelta(days=3 * 365)).isoformat()
    paths = set()
    try:
        paths = {e["path"] for e in github.tree(repo, rec["commit"]).get("tree", [])}
    except github.GitHubError:
        pass
    scored = []
    for i in issues.values():
        lab_names = [l["name"] for l in i.get("labels", [])]
        if any(SKIP_LABELS.search(l) for l in lab_names):
            continue
        body = i.get("body") or ""
        if len(body) < 300 or len(i["title"].split()) < 4 or i["created_at"][:10] < cutoff:
            continue
        if re.search(r"\b(typo|spelling|rename|translation|readme|docs?)\b", i["title"], re.I):
            continue
        reactions = (i.get("reactions") or {}).get("total_count", 0)
        s = min(i.get("comments", 0), 20) * 0.5 + min(reactions, 20) * 0.7 + min(len(body), 3000) / 1000
        if re.search(r"steps to reproduce|expected|actual|stack ?trace|traceback|error", body, re.I):
            s += 1.5
        scored.append((s, i, lab_names))
    scored.sort(key=lambda x: -x[0])
    out = []
    for s, i, lab_names in scored[: max_leads * 2]:
        # Skip issues with a linked PR that is merged, or an open PR in flight.
        linked = []
        try:
            for ev in github.issue_timeline(repo, i["number"]):
                src = (ev.get("source") or {}).get("issue") or {}
                if ev.get("event") == "cross-referenced" and src.get("pull_request"):
                    linked.append({"url": src["html_url"], "title": src["title"][:120],
                                   "state": "merged" if src["pull_request"].get("merged_at") else src.get("state"),
                                   "kind": "pr"})
        except github.GitHubError:
            pass
        if any(l["state"] in ("merged", "open") for l in linked):
            continue
        body = i.get("body") or ""
        mentioned = sorted({m for m in re.findall(r"[\w./-]+\.[A-Za-z]{1,5}", body) if m in paths})[:6]
        up = upstream_check(repo, i["title"], rec["commit"], rec["default_branch"],
                            exclude_issue=i["number"])
        up["related"] = (linked + up["related"])[:6]
        risk = "medium"
        why = "issue lead, not triaged; check scope at the pinned commit"
        if len(body) < 600 and i.get("comments", 0) < 2:
            risk, why = "high", "short issue, few comments; may be a small change"
        facts = [
            f"Issue #{i['number']} opened {i['created_at'][:10]}; {i.get('comments', 0)} comments; "
            f"{(i.get('reactions') or {}).get('total_count', 0)} reactions",
            f"Labels: {', '.join(lab_names) or 'none'}",
        ]
        if mentioned:
            facts.append("Files named in issue: " + ", ".join(mentioned))
        live = sorted({m.group(0).lower() for m in LIVE_SERVICES.finditer(i["title"] + " " + body)})[:4]
        if live:
            facts.append("Mentions live services (must be mockable): " + ", ".join(live))
        title = " ".join(i["title"].split()[:12])
        out.append({
            "id": lead_id(rec["full_name"], i["title"], "issue"),
            "repo": rec["full_name"], "commit": rec["commit"], "repo_url_pinned": rec["pinned_url"],
            "primary_language": rec["studio_language"],
            "slot_fit": {k: slot[k] for k in ("action", "domain", "codebase_type")},
            "title": title, "facts": facts, "files": mentioned,
            "mechanism": "unknown until triaged (read the issue thread)",
            "verify": "; ".join(rec["verify"]["commands"][:3]) or "no test command found",
            "one_shot_risk": {"level": risk, "why": why},
            "upstream": up, "source": "issue", "issue_url": i["html_url"], "live_services": live,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": "new",
        })
        if len(out) >= max_leads:
            break
    return out


# --- Agent leads -------------------------------------------------------------

LEAD_SCHEMA = {
    "type": "object",
    "properties": {
        "leads": {
            "type": "array", "minItems": 1, "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "<= 12 words, a label not a sentence"},
                    "facts": {"type": "array", "items": {"type": "string"}, "maxItems": 5,
                              "description": "short factual bullets, <= 25 words each"},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "mechanism": {"type": "string", "description": "one line: what makes it hard"},
                    "verify": {"type": "string", "description": "commands/tests to check in-session"},
                    "one_shot": {"type": "object", "properties": {
                        "likelihood": {"type": "string", "enum": ["unlikely", "possible", "likely"],
                                       "description": "chance Opus at high effort fully solves it on the first try"},
                        "why": {"type": "string"}}, "required": ["likelihood", "why"]},
                    "search_terms": {"type": "array", "items": {"type": "string"}, "maxItems": 4,
                                     "description": "keywords to search upstream issues/PRs"},
                },
                "required": ["title", "facts", "files", "mechanism", "verify", "one_shot",
                             "search_terms"],
            },
        },
        "repo_notes": {"type": "array", "items": {"type": "string"}, "maxItems": 4,
                       "description": "setup facts: how to install/run tests, services needed"},
    },
    "required": ["leads", "repo_notes"],
}

ACTION_GUIDE = {
    "Create": "build something new in this app (a feature, endpoint, service, view) that has to fit "
              "existing data model, API and UI conventions.",
    "Modify": "change existing code: a real bug, refactor, migration or upgrade. Bugs must be real and "
              "reproducible at this commit (cite the code that shows it).",
    "Operate": "deploy, run, configure or diagnose the running system (CI, Docker/compose, config, "
               "infra, runtime recovery). The main deliverable is ops work, not an app code fix.",
    "Understand / Assess": "read and judge with no code changes: a review, audit, architecture "
                           "explanation or plan with answers that can be checked against the code.",
}

AGENT_PROMPT = """You are scouting this repository (checked out at commit {commit}) for candidate problems for a coding-model evaluation. A human expert will choose one and write everything about it in their own words. Your output is private research notes for that person.

Slot to fit:
- Task type: {action} — {action_guide}
- Domain: {domain}
- Codebase type: {codebase_type}
- The opening prompt will be: {prompt_style}; interaction: {interaction_mode}

Repo facts: {lang}; ~{src} source files, {tests} test files; test/build commands: {verify}.

What a good candidate looks like:
- Hard for an expert: as hard as the hardest task an expert in this domain faces in a typical week. Claude Opus at high effort should NOT one-shot it. The difficulty must come from this real codebase, not from contrived requirements.
- One bounded problem with one clear target, though it may span several layers (data model + API + UI, or a chain of calls).
- True at this commit (cite the code), not already fixed upstream, reproducible, and verifiable within one session (tests, build, before/after output). No "works at scale" or multi-machine checks.
- Self-contained: runs from the repo or Docker; no paid services, private data, special hardware or live third-party APIs (they can be mocked).
- A clear better-vs-worse approach that senior engineers would agree on.
- Areas where models cut corners: swallowed errors and broad try/catch; scheduled/background work that overlaps or over-processes; N+1 queries, missing indexes, loading everything into memory; ignoring repo conventions or duplicating existing helpers; UI missing loading/empty/error states; state and concurrency (two clients editing, partial saves, races); rules that must hold across a chain of calls; platform quirks (timezones, encoding, type coercion, DB semantics); implied edge cases (deletes, empty or huge inputs, undo, existing data that must keep working); existing behavior that must not change.

How to work: read the code (Read, Grep, Glob, git log/show). Do not modify files. Spend your effort confirming each candidate against the actual code. Prefer 3 strong candidates over 5 weak ones.

STRICT OUTPUT RULES (the person is not allowed to submit AI-written text, and their fields are checked by AI detectors):
- Do NOT write an opening prompt, ticket, user story, task goal, acceptance criteria, rubric, or better/worse decision points. Do not phrase anything as a request to a model.
- No prose paragraphs. Titles are labels (<= 12 words). Facts are terse bullets (<= 25 words) stating what the code does now, with file paths and function names.
- "mechanism" is one line naming what makes it hard. "verify" names commands or tests. "one_shot.likelihood" is your honest estimate of the chance that Opus at high effort solves it fully on the first try: "likely" if it is mostly pattern-copying or a small change, "unlikely" only if there is a concrete trap most first attempts would miss (say which in "why").
- "search_terms": 2–4 keywords to look for related upstream issues/PRs.
- "repo_notes": up to 4 setup facts (how to install, run tests, services needed).
Return only the JSON object matching the schema."""


def _clone(rec: dict) -> Path:
    dest = CLONES / rec["full_name"].replace("/", "__")
    if dest.exists():
        head = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True)
        if head.stdout.strip() == rec["commit"]:
            _strip_claude_settings(dest)
            return dest
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{rec['display_name']}.git"
    r = subprocess.run(["git", "clone", "--quiet", "--depth", "50", "--single-branch", "--branch",
                        rec["default_branch"], url, str(dest)], capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"clone failed: {r.stderr[-500:]}")
    co = subprocess.run(["git", "-C", str(dest), "checkout", "--quiet", rec["commit"]],
                        capture_output=True, text=True)
    if co.returncode != 0:
        subprocess.run(["git", "-C", str(dest), "fetch", "--quiet", "--depth", "1", "origin", rec["commit"]],
                       check=True, timeout=600)
        subprocess.run(["git", "-C", str(dest), "checkout", "--quiet", rec["commit"]], check=True)
    _strip_claude_settings(dest)
    return dest


def _strip_claude_settings(dest: Path) -> None:
    """The repo's own Claude settings could add hooks or wider permissions; headless Claude runs here with
    project settings on, so they are removed (as johto/dossier/generate.py does)."""
    for name in ("settings.json", "settings.local.json"):
        (dest / ".claude" / name).unlink(missing_ok=True)


def remove_clone(rec: dict) -> None:
    dest = CLONES / rec["full_name"].replace("/", "__")
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)


def agent_leads(rec: dict, slot: dict, timeout: int = 600, model: str = "opus", effort: str = "high",
                keep_clone: bool = False) -> list[dict]:
    clone = _clone(rec)
    prompt = AGENT_PROMPT.format(
        commit=rec["commit"], action=slot["action"], action_guide=ACTION_GUIDE.get(slot["action"], ""),
        domain=slot["domain"], codebase_type=slot["codebase_type"],
        prompt_style=slot.get("prompt_style") or "casual prose",
        interaction_mode=slot.get("interaction_mode") or "interactive",
        lang=rec.get("language"), src=rec["counts"]["source_files"], tests=rec["counts"]["test_files"],
        verify=", ".join(rec["verify"]["commands"]) or "none found")
    argv = [str(CLAUDE_BIN), "-p", prompt, "--output-format", "json", "--model", model,
            "--effort", effort, "--json-schema", json.dumps(LEAD_SCHEMA),
            "--allowedTools", "Read,Grep,Glob,Bash(git log:*),Bash(git show:*),Bash(ls:*),Bash(wc:*)",
            "--disallowedTools", "Edit,Write,NotebookEdit,WebFetch,WebSearch,Task,Agent",
            "--setting-sources", "project", "--strict-mcp-config", "--no-session-persistence"]
    started = time.time()
    try:
        r = subprocess.run(argv, cwd=str(clone), capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    finally:
        if not keep_clone:
            remove_clone(rec)
    elapsed = time.time() - started
    try:
        res = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude returned non-JSON (exit {r.returncode}): {r.stdout[-800:]} {r.stderr[-800:]}")
    payload = res.get("structured_output")
    if payload is None:
        txt = res.get("result") or ""
        m = re.search(r"\{.*\}", txt, re.S)
        payload = json.loads(m.group(0)) if m else {"leads": [], "repo_notes": []}
    RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RUNS_LOG.open("a") as f:
        f.write(json.dumps({"repo": rec["full_name"], "slot": slot, "elapsed_s": round(elapsed),
                            "cost_usd": res.get("total_cost_usd"), "num_turns": res.get("num_turns"),
                            "is_error": res.get("is_error"), "n_leads": len(payload.get("leads", [])),
                            "at": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
    out = []
    for l in payload.get("leads", []):
        up = upstream_check(rec["display_name"], l["title"], rec["commit"], rec["default_branch"],
                            extra=" ".join(l.get("search_terms", [])))
        out.append({
            "id": lead_id(rec["full_name"], l["title"], "agent"),
            "repo": rec["full_name"], "commit": rec["commit"], "repo_url_pinned": rec["pinned_url"],
            "primary_language": rec["studio_language"],
            "slot_fit": {k: slot[k] for k in ("action", "domain", "codebase_type")},
            "title": " ".join(l["title"].split()[:12]), "facts": l["facts"][:5], "files": l["files"][:10],
            "mechanism": l["mechanism"], "verify": l["verify"],
            "one_shot_risk": {"level": LIKELIHOOD_TO_RISK[l["one_shot"]["likelihood"]],
                              "why": l["one_shot"]["why"]},
            "upstream": up, "source": "agent", "issue_url": None,
            "repo_notes": payload.get("repo_notes", []),
            "agent_run": {"elapsed_s": round(elapsed), "cost_usd": res.get("total_cost_usd")},
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": "new",
        })
    return out
