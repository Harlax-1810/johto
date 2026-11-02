"""Task understanding (johto.understand/1): what Austin needs on the Prompt step to write the opening prompt
himself, at two levels: a glance beside the prompt box and a page he clicks into.

tasks/<t>/understand.json holds one document per task. Headless Claude (opus, high effort) writes it in a clone of
the repo at the pinned commit, with read-only code tools, read-only git and gh commands, and web search for
upstream threads. Python fetches the upstream facts first (each related issue and pull request, whether the pinned
commit includes a merged fix, the first release that shipped it), so dates and "already fixed?" never rest on the
model alone. The document is validated (shape, word limits, the never-list with per-field allowances, code paths
that exist in the clone), repaired once in the same session, pruned of list items that still break a rule, then
written atomically. An overwritten document moves to tasks/<t>/understand-history/ so the overlap guard still knows
every text Austin may have read.

The content follows the slot's task type (Create, Modify, Operate, Understand / Assess) and the questions the
prompt's worked example answers: the plain story first, defined terms, what is real and what is staged, what exists
in the lane now, what each upstream thread changed and whether it touches the premise, broken versus difficult, and
the hard part kept apart from the prompt.

Read side: view(d) is the GET /api/tasks/{short}/understand payload: the document, its state, the job's progress, a
fallback from the repo brief while it is written, the Mercor rules for the type and the precedent (accepted and
rejected tasks of the same type, johto/precedent.py). ensure(d) starts the background job (web only).

Nothing here writes Austin's fields or suggests wording for them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from . import config
from . import understand_prompt as P
from .plain import problems, words

SCHEMA_ID = "johto.understand/1"
FILE = "understand.json"
STATUS_FILE = "understand.status.json"
HISTORY_DIR = "understand-history"
MODEL = "opus"
EFFORT = "high"
TIMEOUT_S = 25 * 60
REPAIR_TIMEOUT_S = 6 * 60
RETRY_FAILED_S = 30 * 60      # a failed generation is retried on page load only after this long
GH_TIMEOUT_S = 40
MAX_THREADS = 8
TOOLS = ["Read", "Grep", "Glob", "Bash", "WebSearch", "WebFetch"]
ALLOWED_TOOLS = [
    "Read", "Grep", "Glob", "WebSearch", "WebFetch",
    "Bash(git log:*)", "Bash(git show:*)", "Bash(git merge-base:*)", "Bash(git tag:*)", "Bash(git describe:*)",
    "Bash(ls:*)", "Bash(wc:*)", "Bash(rg:*)", "Bash(grep:*)", "Bash(head:*)",
    "Bash(gh issue list:*)", "Bash(gh issue view:*)", "Bash(gh pr list:*)", "Bash(gh pr view:*)",
    "Bash(gh search:*)", "Bash(gh release list:*)", "Bash(gh release view:*)",
]
DENIED_TOOLS = [
    "Edit", "Write", "NotebookEdit", "Task", "Agent", "TodoWrite",
    "Bash(rg * --pre*)", "Bash(rg --pre*)", "Bash(git * --output*)", "Bash(gh * --web*)",
    "Bash(git tag -d*)", "Bash(git tag --delete*)", "Bash(git tag -a*)", "Bash(git tag -s*)", "Bash(git tag -f*)",
]
CLAUDE_BIN: str | None = None   # None: config.CLAUDE_BIN. Tests point it at a stub.

Log = Callable[[str], None]
Step = Callable[[str], None]

_GH_URL = re.compile(r"^https?://github\.com/([\w.-]+)/([\w.-]+)/(issues|pull)/(\d+)\b")
_MAINTAINER = {"OWNER", "MEMBER", "COLLABORATOR"}


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# --- files -----------------------------------------------------------------------------------------------

def path(d: Path) -> Path:
    return Path(d) / FILE


def load(d: Path) -> dict | None:
    try:
        doc = json.loads(path(d).read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == SCHEMA_ID else None


def history(d: Path) -> list[Path]:
    return sorted((Path(d) / HISTORY_DIR).glob("*.json"))


def _status(d: Path) -> dict:
    try:
        s = json.loads((Path(d) / STATUS_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return s if isinstance(s, dict) else {}


def _set_status(d: Path, **fields) -> None:
    p = Path(d) / STATUS_FILE
    if not Path(d).is_dir():   # the claim was undone while the research ran
        return
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({**fields, "at": now_iso()}, indent=1))
        os.replace(tmp, p)
    except OSError:
        pass


def _write(d: Path, doc: dict) -> Path:
    d = Path(d)
    p = path(d)
    if p.exists():
        h = d / HISTORY_DIR
        h.mkdir(exist_ok=True)
        shutil.copy2(p, h / f"{time.strftime('%Y%m%dT%H%M%S')}.json")
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    os.replace(tmp, p)
    (d / STATUS_FILE).unlink(missing_ok=True)
    return p


# --- inputs ----------------------------------------------------------------------------------------------

def _read_json(p: Path):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


def _meta(d: Path) -> dict:
    return _read_json(Path(d) / "task.json") or {}


def action_of(meta: dict) -> str:
    a = ((meta.get("slot") or {}).get("action") or "").strip()
    return a if a in P.ACTIONS else "Modify"


def _ids(meta: dict, d: Path | None = None) -> tuple[str | None, str | None, dict]:
    """(dossier id, opportunity id, origin). A task claimed outside the brief has none recorded; then the brief
    seed it matches (web.data.seed_match, the same match that names it on the rail) stands in."""
    origin = meta.get("origin") if isinstance(meta.get("origin"), dict) else {}
    dossier = meta.get("dossier") if isinstance(meta.get("dossier"), dict) else {}
    did = origin.get("dossier_id") or dossier.get("id")
    oid = origin.get("opportunity_id") or dossier.get("opportunity_id")
    if not did and d is not None and meta.get("repo"):
        try:
            from .web import data as wdata
            hit = wdata.seed_match(Path(d), meta) or {}
        except Exception:  # noqa: BLE001 - a missing match only means less research
            hit = {}
        did, oid = hit.get("dossier_id"), hit.get("opportunity_id")
    return did, oid, origin


LEAD_KEYS = ("title", "mechanism", "facts", "verify", "one_shot", "one_shot_risk", "files", "upstream", "issue_url")


def _lead(meta: dict) -> dict | None:
    """The scout lead the task was claimed from: meta["lead"], else the lead_id looked up in data/scout/leads.json."""
    lead = meta.get("lead") if isinstance(meta.get("lead"), dict) else None
    if lead is None and meta.get("lead_id"):
        leads = _read_json(config.DATA / "scout" / "leads.json")
        lead = next((x for x in leads if isinstance(x, dict) and x.get("id") == meta["lead_id"]), None) \
            if isinstance(leads, list) else None
    return {k: lead[k] for k in LEAD_KEYS if lead.get(k)} if lead else None


NOTE_SKIP = {"slot", "passing difficulty", "lane tools"}
NOTES_MAX = 6000


def _task_notes(d: Path) -> str | None:
    """The research sections of the task's own brief.md (a lead, an upstream check), without the slot and
    passing-difficulty boilerplate every brief repeats. Capped at NOTES_MAX characters."""
    try:
        text = (Path(d) / "brief.md").read_text()
    except OSError:
        return None
    keep, on = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            on = line[3:].strip().lower() not in NOTE_SKIP
        if on:
            keep.append(line)
    out = "\n".join(keep).strip()
    return out[:NOTES_MAX] or None


def _dossier(did: str | None) -> dict | None:
    if not did or not re.fullmatch(r"[\w.-]+", did):
        return None
    return _read_json(config.DATA / "dossiers" / f"{did}.json")


def _brief(did: str | None) -> dict | None:
    if not did or not re.fullmatch(r"[\w.-]+", did):
        return None
    return _read_json(config.DATA / "dossiers" / f"{did}.brief.json")


def _opportunity(dossier: dict | None, oid: str | None) -> dict | None:
    for b in (dossier or {}).get("blocks") or []:
        if isinstance(b, dict) and b.get("type") == "opportunity" and b.get("id") == oid:
            return b
    return None


def _overview(dossier: dict | None) -> dict | None:
    for b in (dossier or {}).get("blocks") or []:
        if isinstance(b, dict) and b.get("type") == "overview":
            return {k: v for k, v in b.items() if k != "type"}
    return None


def research(d: Path) -> dict:
    """What the task came from, without GitHub calls: {action, slot, repo, opportunity, seed, place, lead,
    overview, how_it_works, starting_state}. Also the base of the inputs hash."""
    meta = _meta(d)
    did, oid, origin = _ids(meta, d)
    dossier = _dossier(did)
    brief = _brief(did) or {}
    opp = _opportunity(dossier, oid) or (meta.get("opportunity") if isinstance(meta.get("opportunity"), dict) else None)
    seed = next((s for s in brief.get("seeds") or [] if s.get("opportunity_id") == oid), None) if oid else None
    place = None
    if origin.get("kind") == "place":
        place = next((p for p in brief.get("places") or [] if p.get("id") == origin.get("place_id")), None)
    lead = _lead(meta)
    repo = meta.get("repo") or {}
    return {
        "action": action_of(meta),
        "slot": meta.get("slot") or {},
        "repo": {k: repo.get(k) for k in ("full_name", "commit", "description", "language")},
        "dossier_id": did,
        "opportunity_id": oid,
        "opportunity": opp,
        "seed": seed,
        "place": place,
        "lead": lead,
        "task_notes": _task_notes(d),
        "project": {"name": brief.get("name"), "one_liner": brief.get("one_liner"),
                    "how_it_works": brief.get("how_it_works")} if brief else None,
        "overview": _overview(dossier),
        "starting_state": ((opp or {}).get("bounded_task") or {}).get("check_in_session"),
    }


def has_subject(r: dict) -> bool:
    """Whether there is research to explain: a dossier opportunity, brief seed or place, a scout lead, or notes in
    the task's own brief. Without any, Claude would have to invent the task, so nothing runs."""
    return bool(r.get("opportunity") or r.get("seed") or r.get("place") or r.get("lead") or r.get("task_notes"))


NO_SUBJECT = "Nothing to research yet: the task has no brief idea, scout lead or research notes."


def inputs_sha(r: dict) -> str:
    """Changes when the research behind the task, the slot or the prompt changes (not upstream facts)."""
    base = json.dumps({"r": r, "v": P.VERSION}, sort_keys=True, default=str)
    return hashlib.sha256(base.encode()).hexdigest()[:16]


_GH_LINK = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+")


def _related_urls(r: dict) -> list[str]:
    """Issue and PR links the research already names: the opportunity's related threads, the lead's issue and
    upstream links, and links in the task's own notes."""
    out = []
    for u in (((r.get("opportunity") or {}).get("upstream") or {}).get("related") or []):
        if isinstance(u, dict) and u.get("url"):
            out.append(u["url"])
    lead = r.get("lead") or {}
    out += _GH_LINK.findall(json.dumps([lead.get("issue_url"), lead.get("upstream")], default=str))
    out += _GH_LINK.findall(r.get("task_notes") or "")
    return list(dict.fromkeys(out))


# --- GitHub facts ----------------------------------------------------------------------------------------

def _gh(args: list[str]):
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=GH_TIMEOUT_S,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def _git(clone: Path | None, *args: str) -> subprocess.CompletedProcess | None:
    if not clone:
        return None
    try:
        return subprocess.run(["git", *args], cwd=clone, capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None


def contains(clone: Path | None, commit: str | None, ref: str = "HEAD") -> bool | None:
    """Whether `ref` includes `commit` (None when git can't tell, e.g. the commit isn't in the clone)."""
    if not commit or not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        return None
    r = _git(clone, "merge-base", "--is-ancestor", commit, ref)
    if r is None:
        return None
    return True if r.returncode == 0 else False if r.returncode == 1 else None


def _excerpt(s: str | None, n: int) -> str:
    s = re.sub(r"<!--.*?-->", "", s or "", flags=re.S).strip()
    return s if len(s) <= n else s[:n].rstrip() + " …"


def _comments(cs) -> list[dict]:
    out = []
    for c in cs or []:
        if not isinstance(c, dict):
            continue
        assoc = c.get("authorAssociation") or ""
        out.append({"by": "maintainer" if assoc in _MAINTAINER else "user", "at": (c.get("createdAt") or "")[:10],
                    "text": _excerpt(c.get("body"), 700)})
    maint = [c for c in out if c["by"] == "maintainer"][:4]
    users = [c for c in out if c["by"] != "maintainer"][:3]
    return sorted(maint + users, key=lambda c: c["at"])


RELEASE_LIMIT = 200   # releases read from GitHub; the prompt gets the newest PROMPT_RELEASES
PROMPT_RELEASES = 30


def first_release(clone: Path | None, commit: str | None, releases: list[dict]) -> str | None:
    """The oldest release whose tag includes `commit`. None when that is the oldest release read and the list was
    cut at RELEASE_LIMIT: an older release, not read, may have shipped it first."""
    if not commit or not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        return None
    r = _git(clone, "tag", "--contains", commit)          # one call instead of one per release
    if r is None or r.returncode != 0:
        return None
    tags = set(r.stdout.split())
    rels = sorted(releases, key=lambda x: x.get("publishedAt") or "")
    for n, rel in enumerate(rels):
        if rel.get("tagName") in tags:
            return None if n == 0 and len(rels) >= RELEASE_LIMIT else rel["tagName"]
    return None


def thread_facts(url: str, *, clone: Path | None = None, releases: list[dict] | None = None,
                 nested: bool = True) -> dict | None:
    """{url, kind, number, title, state, opened, closed, merged, merge_commit, in_pinned, first_release, body,
    comments, files, closed_by} for one GitHub issue or pull request (None when gh can't read it)."""
    m = _GH_URL.match(url or "")
    if not m:
        return None
    owner, name, kind, num = m.groups()
    repo = f"{owner}/{name}"
    canon = f"https://github.com/{repo}/{kind}/{num}"
    if kind == "pull":
        j = _gh(["pr", "view", num, "-R", repo, "--json",
                 "number,title,state,createdAt,closedAt,mergedAt,mergeCommit,body,files,comments,url"])
        if not isinstance(j, dict):
            return None
        oid = (j.get("mergeCommit") or {}).get("oid") if j.get("mergedAt") else None
        return {"url": canon, "kind": "pull", "number": j.get("number"), "title": j.get("title"),
                "state": (j.get("state") or "").lower(), "opened": (j.get("createdAt") or "")[:10],
                "closed": (j.get("closedAt") or "")[:10] or None, "merged": (j.get("mergedAt") or "")[:10] or None,
                "merge_commit": oid, "in_pinned": contains(clone, oid) if oid else None,
                "first_release": first_release(clone, oid, releases or []) if oid else None,
                "body": _excerpt(j.get("body"), 1200), "comments": _comments(j.get("comments")),
                "files": [f.get("path") for f in (j.get("files") or [])[:25] if isinstance(f, dict)]}
    j = _gh(["issue", "view", num, "-R", repo, "--json",
             "number,title,state,stateReason,createdAt,closedAt,body,comments,url,closedByPullRequestsReferences"])
    if not isinstance(j, dict):
        return None
    closing = [p.get("url") for p in j.get("closedByPullRequestsReferences") or [] if isinstance(p, dict)
               and p.get("url")]
    out = {"url": canon, "kind": "issue", "number": j.get("number"), "title": j.get("title"),
           "state": (j.get("state") or "").lower(), "state_reason": (j.get("stateReason") or "").lower() or None,
           "opened": (j.get("createdAt") or "")[:10], "closed": (j.get("closedAt") or "")[:10] or None,
           "body": _excerpt(j.get("body"), 1500), "comments": _comments(j.get("comments")), "closed_by": closing[:3]}
    return out


def releases(full_name: str) -> list[dict]:
    j = _gh(["release", "list", "-R", full_name, "--limit", str(RELEASE_LIMIT), "--json", "tagName,publishedAt"])
    return [{"tagName": x.get("tagName"), "publishedAt": (x.get("publishedAt") or "")[:10]}
            for x in j or [] if isinstance(x, dict) and x.get("tagName")]


def pinned_facts(clone: Path | None, commit: str | None, rels: list[dict]) -> dict:
    r = _git(clone, "show", "-s", "--format=%cI %s", commit or "HEAD")
    line = (r.stdout.strip() if r is not None and r.returncode == 0 else "")
    date, _, subject = line.partition(" ")
    after = None
    for rel in sorted(rels, key=lambda x: x.get("publishedAt") or "", reverse=True):
        if rel.get("tagName") and contains(clone, _tag_commit(clone, rel["tagName"]), commit or "HEAD"):
            after = rel["tagName"]
            break
    return {"commit": commit, "date": date[:10] or None, "subject": subject or None, "after_release": after}


def _tag_commit(clone: Path | None, tag: str) -> str | None:
    r = _git(clone, "rev-list", "-n", "1", f"refs/tags/{tag}")
    s = r.stdout.strip() if r is not None and r.returncode == 0 else ""
    return s or None


def upstream_facts(urls: list[str], full_name: str, clone: Path | None, commit: str | None) -> dict:
    rels = releases(full_name) if full_name else []
    threads: dict[str, dict] = {}
    queue = list(dict.fromkeys(urls))
    while queue and len(threads) < MAX_THREADS:
        u = queue.pop(0)
        m = _GH_URL.match(u or "")
        if not m:
            continue
        canon = f"https://github.com/{m.group(1)}/{m.group(2)}/{m.group(3)}/{m.group(4)}"
        if canon in threads:
            continue
        f = thread_facts(canon, clone=clone, releases=rels)
        if not f:
            continue
        threads[canon] = f
        queue += [x for x in f.get("closed_by") or [] if x not in threads]
    return {"pinned": pinned_facts(clone, commit, rels), "releases": rels[:15], "threads": threads}


# --- the prompt inputs -----------------------------------------------------------------------------------

def prompt_inputs(r: dict, facts: dict) -> dict:
    opp = r.get("opportunity") or {}
    return {
        "task_type": r["action"],
        "slot": r.get("slot"),
        "repository": r.get("repo"),
        "project": r.get("project"),
        "project_overview": r.get("overview"),
        "the_research_this_task_came_from": {
            "title": opp.get("title") or (r.get("seed") or {}).get("title") or (r.get("lead") or {}).get("title"),
            "dossier_opportunity": opp or None,
            "brief_seed": r.get("seed"),
            "brief_place": r.get("place"),
            "scout_lead": r.get("lead"),
            "task_research_notes": r.get("task_notes"),
        },
        "starting_state_recipe_from_research": r.get("starting_state"),
        "pinned_commit": facts.get("pinned"),
        "releases_newest_first": (facts.get("releases") or [])[:PROMPT_RELEASES],
        "upstream_threads_fetched": list((facts.get("threads") or {}).values()),
    }


# --- validation ------------------------------------------------------------------------------------------

UPSTREAM_OK = ("has a version number", "has an issue or PR number")
CODE_OK = ("has backticks", "names a file or path", "has a code identifier", "has a line number",
           "has an issue or PR number", "has a version number")
VERDICTS = {"real": ("real", "staged", "mixed"), "upstream": ("open", "partly", "fixed", "none_found")}
REPRO = ("confirmed", "not_yet_confirmed", "does_not_reproduce")
CONFIRMED = ("confirmed", "not_yet_confirmed")
_PATH = re.compile(r"^(?!/)(?!.*\.\.)([\w@.+~ -]+(?:/[\w@.+~ -]+)*)(?::(\d+)(?:-(\d+))?)?$")


class _V:
    def __init__(self):
        self.errors: list[tuple[str, str]] = []

    def err(self, where: str, msg: str) -> None:
        self.errors.append((where, msg))

    def text(self, where: str, v, maxw: int, *, allow: tuple = (), questions: bool = False,
             claude: bool = False) -> None:
        if not isinstance(v, str) or not v.strip():
            self.err(where, "is missing or empty")
            return
        n = words(v)
        if n > maxw:
            self.err(where, f"has {n} words, at most {maxw}")
        for p in problems(v, questions=questions):
            if any(p.startswith(a) for a in allow):
                continue
            # A release tag reads as a dotted code name to the never-list; it is a version where versions are allowed.
            m = re.match(r"has a code identifier \((v?\d+(?:\.\d+)+[\w.-]*)\)$", p)
            if m and "has a version number" in allow:
                continue
            self.err(where, p)
        if claude and not re.search(r"\bClaude\b", v):
            self.err(where, "has to name Claude as the one doing the work")

    def items(self, where: str, v, lo: int, hi: int) -> list:
        if not isinstance(v, list):
            self.err(where, "has to be a list")
            return []
        if not lo <= len(v) <= hi:
            self.err(where, f"has {len(v)} items, needs {lo} to {hi}")
        return v

    def obj(self, where: str, v) -> dict:
        if not isinstance(v, dict):
            self.err(where, "has to be an object")
            return {}
        return v

    def enum(self, where: str, v, allowed: tuple) -> None:
        if v not in allowed:
            self.err(where, f"is {v!r}, has to be one of {', '.join(allowed)}")


def _kind(v: _V, action: str, k: dict) -> None:
    if action == "Operate":
        v.text("kind.lane_now", k.get("lane_now"), 30)
        for i, x in enumerate(v.items("kind.starting_state", k.get("starting_state"), 2, 6)):
            x = v.obj(f"kind.starting_state[{i}]", x)
            v.text(f"kind.starting_state[{i}].part", x.get("part"), 5)
            v.text(f"kind.starting_state[{i}].what", x.get("what"), 32)
        v.text("kind.rebuild", k.get("rebuild"), 36)
        v.text("kind.system_not_code", k.get("system_not_code"), 32)
    elif action == "Create":
        for i, x in enumerate(v.items("kind.exists", k.get("exists"), 1, 4)):
            v.text(f"kind.exists[{i}]", x, 26)
        v.text("kind.missing", k.get("missing"), 36)
        v.text("kind.fits_into", k.get("fits_into"), 36)
        for i, x in enumerate(v.items("kind.similar", k.get("similar", []), 0, 3)):
            v.text(f"kind.similar[{i}]", x, 26)
    elif action == "Understand / Assess":
        v.text("kind.question", k.get("question"), 32)
        for i, x in enumerate(v.items("kind.ground_truth", k.get("ground_truth"), 1, 4)):
            v.text(f"kind.ground_truth[{i}]", x, 32)
        v.enum("kind.confirmed", k.get("confirmed"), CONFIRMED)
        v.text("kind.check", k.get("check"), 36)
    else:
        v.text("kind.symptom", k.get("symptom"), 36)
        rep = v.obj("kind.reproduces", k.get("reproduces"))
        v.enum("kind.reproduces.status", rep.get("status"), REPRO)
        v.text("kind.reproduces.how", rep.get("how"), 32)
        for i, x in enumerate(v.items("kind.keep_working", k.get("keep_working"), 1, 4)):
            v.text(f"kind.keep_working[{i}]", x, 22)
        v.text("kind.who_hits_it", k.get("who_hits_it"), 30)


def _code_path_ok(p: str, clone: Path | None) -> str | None:
    m = _PATH.match(p or "")
    if not m:
        return "is not a relative path with an optional :start-end line range"
    if clone is not None and not (Path(clone) / m.group(1)).is_file():
        return f"names {m.group(1)}, which is not a file in the clone"
    return None


def validate(doc, action: str, *, clone: Path | None = None, repo: str | None = None) -> list[tuple[str, str]]:
    """[(where, problem)] for a model-written document (empty when it passes)."""
    v = _V()
    if not isinstance(doc, dict):
        return [("document", "has to be a JSON object")]
    known = set(P.COMMON_SCHEMA)
    for key in doc:
        if key not in known:
            v.err(key, "is not a key in the schema")
    v.text("headline", doc.get("headline"), 28, claude=True)
    v.text("story", doc.get("story"), 60)
    v.text("job", doc.get("job"), 45, claude=True)
    v.text("done", doc.get("done"), 45)
    for i, t in enumerate(v.items("terms", doc.get("terms", []), 0, 6)):
        t = v.obj(f"terms[{i}]", t)
        v.text(f"terms[{i}].term", t.get("term"), 5)
        v.text(f"terms[{i}].means", t.get("means"), 24)
    today = v.obj("today", doc.get("today"))
    for i, x in enumerate(v.items("today.works", today.get("works", []), 0, 4)):
        v.text(f"today.works[{i}]", x, 28)
    for i, x in enumerate(v.items("today.missing", today.get("missing"), 1, 4)):
        v.text(f"today.missing[{i}]", x, 28)
    real = v.obj("real", doc.get("real"))
    v.enum("real.verdict", real.get("verdict"), VERDICTS["real"])
    for i, x in enumerate(v.items("real.real", real.get("real"), 1, 4)):
        v.text(f"real.real[{i}]", x, 32, allow=UPSTREAM_OK)
    for i, x in enumerate(v.items("real.made_up", real.get("made_up", []), 0, 4)):
        v.text(f"real.made_up[{i}]", x, 32, allow=UPSTREAM_OK)
    up = v.obj("upstream", doc.get("upstream"))
    v.enum("upstream.verdict", up.get("verdict"), VERDICTS["upstream"])
    v.text("upstream.line", up.get("line"), 32, allow=UPSTREAM_OK)
    for i, t in enumerate(v.items("upstream.threads", up.get("threads", []), 0, 6)):
        t = v.obj(f"upstream.threads[{i}]", t)
        m = _GH_URL.match(t.get("url") or "")
        if not m:
            v.err(f"upstream.threads[{i}].url", "is not a GitHub issue or pull request URL")
        elif repo and f"{m.group(1)}/{m.group(2)}".lower() != repo.lower():
            v.err(f"upstream.threads[{i}].url", f"is not in {repo}")
        v.text(f"upstream.threads[{i}].changed", t.get("changed"), 32, allow=UPSTREAM_OK)
        v.text(f"upstream.threads[{i}].bearing", t.get("bearing"), 32, allow=UPSTREAM_OK)
    _kind(v, action, v.obj("kind", doc.get("kind")))
    expected = set(P.KIND_SCHEMA.get(action) or {})
    for key in (doc.get("kind") or {}) if isinstance(doc.get("kind"), dict) else {}:
        if key not in expected:
            v.err(f"kind.{key}", f"is not a key for {action} tasks")
    for i, x in enumerate(v.items("hard", doc.get("hard"), 2, 5)):
        v.text(f"hard[{i}]", x, 36)
    for i, c in enumerate(v.items("code", doc.get("code", []), 0, 10)):
        c = v.obj(f"code[{i}]", c)
        bad = _code_path_ok(c.get("path"), clone)
        if bad:
            v.err(f"code[{i}].path", bad)
        v.text(f"code[{i}].what", c.get("what"), 26, allow=CODE_OK)
    for i, x in enumerate(v.items("unsure", doc.get("unsure", []), 0, 3)):
        v.text(f"unsure[{i}]", x, 28, questions=True)
    return v.errors


_IDX = re.compile(r"^(.*)\[(\d+)\](?:\.[\w]+)?$")
_MIN = {"terms": 0, "today.works": 0, "today.missing": 1, "real.real": 1, "real.made_up": 0, "upstream.threads": 0,
        "hard": 2, "code": 0, "unsure": 0, "kind.starting_state": 2, "kind.exists": 1, "kind.similar": 0,
        "kind.ground_truth": 1, "kind.keep_working": 1}


def _get(doc: dict, dotted: str):
    cur = doc
    for part in dotted.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def prune(doc: dict, errors: list[tuple[str, str]]) -> dict:
    """Drop list items that still break a rule, while each list keeps its minimum length. Unknown keys go too."""
    doc = json.loads(json.dumps(doc))
    drop: dict[str, set[int]] = {}
    for where, msg in errors:
        if msg == "is not a key in the schema" and "." not in where and "[" not in where:
            doc.pop(where, None)
            continue
        if msg.startswith("is not a key for") and where.startswith("kind."):
            (doc.get("kind") or {}).pop(where.split(".", 1)[1], None)
            continue
        m = _IDX.match(where)
        if m and m.group(1) in _MIN:
            drop.setdefault(m.group(1), set()).add(int(m.group(2)))
    for lst, idx in drop.items():
        arr = _get(doc, lst)
        if not isinstance(arr, list):
            continue
        keep = [x for i, x in enumerate(arr) if i not in idx]
        if len(keep) >= _MIN[lst]:
            arr[:] = keep
    return doc


def error_lines(errors: list[tuple[str, str]]) -> list[str]:
    return [f"{w}: {m}" for w, m in errors]


# --- running Claude --------------------------------------------------------------------------------------

def _bin() -> str:
    return str(CLAUDE_BIN or config.CLAUDE_BIN)


def available() -> bool:
    """Whether a generation may run: never in fake mode unless a stub claude is configured."""
    return not config.FAKE or config.CLAUDE_BIN_SET or CLAUDE_BIN is not None


def argv(prompt: str, session: list[str]) -> list[str]:
    return [_bin(), "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--model", MODEL, "--effort", EFFORT,
            "--tools", ",".join(TOOLS),
            "--allowedTools", ",".join(ALLOWED_TOOLS),
            "--disallowedTools", ",".join(DENIED_TOOLS),
            "--permission-mode", "dontAsk",
            "--setting-sources", "project", "--strict-mcp-config",
            "--disable-slash-commands", "--no-chrome", *session]


class _Progress:
    """The generator's progress contract (johto.dossier.generate.run_claude): line() and raw()."""

    STEPS = {"reading the code": "researching", "searching the code": "researching", "listing files": "researching",
             "reading issues and PRs": "researching", "reading history": "researching"}

    def __init__(self, log_p: Path, log: Log | None, step: Step | None):
        self.f = open(log_p, "a", buffering=1)
        self.log, self.on_step = log, step
        self.step = "starting"
        self._last = 0.0
        self.f.write(f"\n=== {now_iso()} task understanding\n")

    def raw(self, text: str) -> None:
        self.f.write(text.rstrip("\n") + "\n")

    def line(self, text: str, step: str | None = None, force: bool = False) -> None:
        step = self.STEPS.get(step, step) if step else None
        if step and step != self.step:
            self.step = step
            if self.on_step:
                try:
                    self.on_step(step if step in ("researching", "writing") else "researching")
                except Exception:
                    pass
        self.f.write(f"{time.strftime('%H:%M:%S')} [{self.step}] {text}\n")
        if self.log and (force or time.time() - self._last >= 4):
            self._last = time.time()
            try:
                self.log(text)
            except Exception:
                pass

    def close(self) -> None:
        self.f.close()


def extract(text: str) -> tuple[dict | None, str | None]:
    """The JSON between the last BEGIN line and the END line after it."""
    if not text:
        return None, f"the final message was empty; it must hold the document between `{P.BEGIN}` and `{P.END}`"
    i = text.rfind(P.BEGIN)
    if i < 0:
        return None, f"the final message has no `{P.BEGIN}` line"
    rest = text[i + len(P.BEGIN):]
    j = rest.find(P.END)
    body = (rest[:j] if j >= 0 else rest).strip()
    body = re.sub(r"^```(?:json)?\s*\n", "", body)
    body = re.sub(r"\n```\s*$", "", body).strip()
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as e:
        return None, f"the JSON does not parse: {e.msg} at line {e.lineno} column {e.colno}"
    if not isinstance(doc, dict):
        return None, "the document has to be a JSON object"
    return doc, None


Runner = Callable[[list[str], Path, float, object], object]   # generate.run_claude's shape


def _clone_root() -> Path:
    return config.DATA / "understand-clones"


def _cleanup(clone: Path, sid: str | None) -> None:
    from .dossier import generate as gen
    gen.cleanup(clone, sid)
    shutil.rmtree(gen.claude_config_dir() / "projects" / gen._project_dir_name(clone), ignore_errors=True)
    shutil.rmtree(gen.claude_config_dir() / "projects" / gen._project_dir_name(Path(clone).resolve()),
                  ignore_errors=True)


def generate(d: Path, *, log: Log | None = None, step: Step | None = None, cancel=None,
             runner: Runner | None = None, clone_fn=None, facts_fn=None) -> dict:
    """Write tasks/<t>/understand.json. {ok, error, cost_usd, path}. `runner`, `clone_fn` and `facts_fn` are
    seams for tests (generate.run_claude, generate.clone_repo and upstream_facts)."""
    from .dossier import generate as gen
    d = Path(d)
    r = research(d)
    action = r["action"]
    full_name = (r.get("repo") or {}).get("full_name") or ""
    commit = (r.get("repo") or {}).get("commit") or ""
    if not full_name or not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        return _fail(d, "The task has no pinned repo commit yet.")
    if not has_subject(r):
        return _fail(d, NO_SUBJECT)
    runner = runner or gen.run_claude
    clone_fn = clone_fn or gen.clone_repo
    facts_fn = facts_fn or upstream_facts
    t0 = time.time()
    deadline = t0 + TIMEOUT_S + REPAIR_TIMEOUT_S + 300
    clone = _clone_root() / f"{d.name}-{uuid.uuid4().hex[:8]}"
    sid = str(uuid.uuid4())
    log_p = d / "understand.log"
    prog = _Progress(log_p, log, step)
    cost = 0.0
    try:
        if step:
            step("cloning")
        prog.line(f"cloning {full_name} at {commit[:8]}", force=True)
        clone_fn(full_name, commit, clone, deadline, cancel)
        if step:
            step("upstream")
        prog.line("reading upstream threads", force=True)
        facts = facts_fn(_related_urls(r), full_name, clone, commit)
        prompt = P.build(action, prompt_inputs(r, facts))
        if step:
            step("researching")
        p1 = runner(argv(prompt, ["--session-id", sid]), clone, TIMEOUT_S, prog, cancel)
        cost += float(getattr(p1, "cost", 0) or 0)
        if cancel is not None and getattr(cancel, "cancelled", False):
            return _fail(d, "Stopped.", cost)
        if getattr(p1, "is_error", False) and not getattr(p1, "text", ""):
            return _fail(d, p1.error_text() if hasattr(p1, "error_text") else "Claude failed.", cost)
        doc, perr = extract(getattr(p1, "text", ""))
        errors = [("document", perr)] if perr else validate(doc, action, clone=clone, repo=full_name)
        if errors:
            if step:
                step("writing")
            prog.line(f"repairing {len(errors)} problems", force=True)
            for e in error_lines(errors)[:20]:
                prog.raw("problem: " + e)
            p2 = runner(argv(P.repair(error_lines(errors)), ["--resume", sid]), clone, REPAIR_TIMEOUT_S, prog, cancel)
            cost += float(getattr(p2, "cost", 0) or 0)
            doc2, perr2 = extract(getattr(p2, "text", ""))
            if doc2 is not None:
                doc = doc2
            if doc is None:
                return _fail(d, f"Claude's document didn't parse: {perr2 or perr}", cost)
            errors = validate(doc, action, clone=clone, repo=full_name)
            if errors:
                doc = prune(doc, errors)
                errors = validate(doc, action, clone=clone, repo=full_name)
        if errors:
            for e in error_lines(errors):
                prog.raw("still failing: " + e)
            return _fail(d, "The research didn't pass its checks: " + "; ".join(error_lines(errors)[:4]), cost)
        # Threads Claude found on its own get the same fetched facts as the ones the research named.
        known = facts.setdefault("threads", {})
        for t in (doc.get("upstream") or {}).get("threads") or []:
            m = _GH_URL.match(t.get("url") or "")
            if m:
                canon = f"https://github.com/{m.group(1)}/{m.group(2)}/{m.group(3)}/{m.group(4)}"
                t["url"] = canon
                if canon not in known and facts_fn is upstream_facts:
                    f = thread_facts(canon, clone=clone, releases=facts.get("releases") or [])
                    if f:
                        known[canon] = f
        out = {
            "schema": SCHEMA_ID, "short": d.name, "action": action, "repo": full_name, "commit": commit,
            "dossier_id": r.get("dossier_id"), "opportunity_id": r.get("opportunity_id"),
            "title": ((r.get("opportunity") or {}).get("title") or (r.get("seed") or {}).get("title")
                      or (r.get("lead") or {}).get("title")),
            "generated_at": now_iso(), "model": getattr(p1, "model", None) or MODEL, "effort": EFFORT,
            "elapsed_s": round(time.time() - t0), "cost_usd": round(cost, 4), "inputs_sha": inputs_sha(r),
            "facts": {"pinned": facts.get("pinned"), "releases": facts.get("releases"),
                      "threads": {u: {k: v for k, v in f.items() if k not in ("body", "comments", "files")}
                                  for u, f in known.items()}},
            "doc": doc,
        }
        p = _write(d, out)
        prog.line(f"written in {out['elapsed_s']} s", force=True)
        return {"ok": True, "error": None, "cost_usd": cost, "path": str(p)}
    except Exception as e:  # noqa: BLE001  (a failed generation is a state on the page, never a crash)
        return _fail(d, f"{type(e).__name__}: {e}", cost)
    finally:
        prog.close()
        _cleanup(clone, sid)


def _fail(d: Path, error: str, cost: float = 0.0) -> dict:
    _set_status(d, state="failed", error=error[:600])
    return {"ok": False, "error": error, "cost_usd": cost, "path": None}


# --- jobs (web) ------------------------------------------------------------------------------------------

def _target(d: Path) -> str:
    return f"t:{Path(d).name}"


def live_job(d: Path) -> dict | None:
    try:
        from .web import jobs
    except Exception:  # noqa: BLE001
        return None
    j = jobs.active(kind="understand", target=_target(d))
    return j[0] if j else None


def state(d: Path) -> str:
    """generating, queued, ready, stale, failed or missing."""
    j = live_job(d)
    if j:
        return "generating" if j.get("state") == "running" else "queued"
    doc = load(d)
    if doc:
        return "ready" if doc.get("inputs_sha") == inputs_sha(research(d)) else "stale"
    return "failed" if _status(d).get("state") == "failed" else "missing"


def _failed_recently(d: Path) -> bool:
    s = _status(d)
    if s.get("state") != "failed":
        return False
    try:
        at = time.mktime(time.strptime(s.get("at", "")[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return False
    return time.time() - at < RETRY_FAILED_S


def prompt_written(d: Path) -> bool:
    """Whether Austin has written the opening prompt or started a run. After that, research only on his press:
    the overlap guard reads this document, and text written after his fields must not flag them."""
    meta = _meta(d)
    if meta.get("runs"):
        return True
    try:
        from . import workspace
        return bool((workspace.parse_submission(Path(d))["sections"].get("Opening Prompt") or "").strip())
    except Exception:  # noqa: BLE001
        return False


def wanted(d: Path) -> bool:
    """Whether a page load should start a generation: no document, or one written from older research, and only
    while the prompt is still unwritten."""
    meta = _meta(d)
    if not ((meta.get("repo") or {}).get("commit")) or prompt_written(d) or not has_subject(research(d)):
        return False
    st = state(d)
    return st == "stale" or (st in ("missing", "failed") and not _failed_recently(d))


def ensure(d: Path, *, force: bool = False) -> dict | None:
    """Start the background job when the task has no current document (or `force`); the running job when one
    is already going; None when nothing needs to run or a generation can't run (fake mode)."""
    d = Path(d)
    if not available():
        return None
    live = live_job(d)
    if live:
        return live
    if not force and not wanted(d):
        return None
    from .web import jobs
    steps = ["cloning", "upstream", "researching", "writing"]

    def work(ctx):
        from .dossier import generate as gen
        cancel = gen.Cancel()
        done = threading.Event()

        def watch():
            while not done.wait(2):
                if ctx.cancelled:
                    cancel.cancel("stopped from the page")
                    return
        threading.Thread(target=watch, daemon=True).start()
        try:
            res = generate(d, log=ctx.log, step=ctx.step, cancel=cancel)
        finally:
            done.set()
        ctx.add_cost(res.get("cost_usd") or 0)
        try:
            jobs.publish("task", {"short": d.name, "changed": ["understand"]})
        except Exception:  # noqa: BLE001
            pass
        if not res["ok"]:
            raise RuntimeError(res["error"])
        return {"path": res["path"]}
    return jobs.submit("understand", _target(d), work, pool="research", priority=1, steps=steps, step="waiting")


# --- read side -------------------------------------------------------------------------------------------

TYPE_RULES = {
    "Operate": ["slot-modify-vs-operate", "task-not-solved", "repo-self-contained", "task-in-session", "task-fair"],
    "Modify": ["slot-modify-vs-operate", "task-not-solved", "task-one-problem", "repo-verifiable", "task-fair"],
    "Create": ["task-one-problem", "task-not-solved", "repo-verifiable", "task-in-session", "task-fair"],
    "Understand / Assess": ["slot-understand-no-code", "task-better-worse", "task-not-solved", "task-fair"],
}
# Facts about a type that docs/rules.md doesn't hold as one rule: Studio's form and what reviewers said.
TYPE_NOTES = {
    "Operate": ["Studio has an Initial State field only for Operate tasks. It records the starting state the run "
                "begins from.",
                "Reviewers rebuild the starting state themselves. A task was rejected because its setup "
                "lived only on the writer's machine."],
    "Understand / Assess": ["Enterprise-grade repos only. The answer needs ground truth a reviewer can check."],
}


def rules_for(action: str) -> list[dict]:
    try:
        from . import rules
        allr = {r.get("id"): r for r in rules.load()}
    except Exception:  # noqa: BLE001
        return []
    out = []
    for rid in TYPE_RULES.get(action) or []:
        r = allr.get(rid)
        if r:
            out.append({"id": rid, "text": r.get("text")})
    return out


def _fallback(r: dict) -> dict:
    """What the page shows while the document is written: the repo brief's seed (already plain words)."""
    seed = r.get("seed") or {}
    det = seed.get("detail") or {}
    opp = r.get("opportunity") or {}
    return {"title": seed.get("title") or opp.get("title") or (r.get("lead") or {}).get("title"),
            "problem": seed.get("problem"), "today": [x for x in det.get("today") or [] if isinstance(x, str)][:4]}


def view(d: Path) -> dict:
    d = Path(d)
    r = research(d)
    doc = load(d)
    st = state(d)
    j = live_job(d)
    prec = {}
    try:
        from . import precedent
        prec = precedent.for_action(r["action"], exclude=d.name)
    except Exception:  # noqa: BLE001
        prec = {}
    return {
        "short": d.name,
        "action": r["action"],
        "slot": r.get("slot"),
        "repo": r.get("repo"),
        "state": st,
        "auto": not prompt_written(d),
        "subject": has_subject(r),
        "error": _status(d).get("error") if st == "failed" else None,
        "job": {"id": j.get("id"), "step": (j.get("progress") or {}).get("step"),
                "started_at": j.get("started_at"), "log": ((j.get("progress") or {}).get("log_tail") or [])[-3:]}
        if j else None,
        "doc": doc.get("doc") if doc else None,
        "meta": {k: doc.get(k) for k in ("generated_at", "elapsed_s", "title", "commit", "repo")} if doc else None,
        "facts": doc.get("facts") if doc else None,
        "fallback": _fallback(r),
        "rules": rules_for(r["action"]),
        "notes": TYPE_NOTES.get(r["action"], []),
        "precedent": prec,
    }


# --- CLI -------------------------------------------------------------------------------------------------

def cmd_understand(a) -> None:
    from . import workspace
    d = workspace.resolve(a.task)
    if getattr(a, "refresh", False) or not load(d):
        if not available():
            raise SystemExit("Headless Claude is off in fake mode (set JOHTO_CLAUDE_BIN to a stub).")
        print(f"Researching {d.name} (a clone, upstream threads, then Claude; usually 2 to 5 minutes)…")
        res = generate(d, log=lambda s: print("  " + s), step=lambda s: print(f"[{s}]"))
        if not res["ok"]:
            raise SystemExit(f"failed: {res['error']}")
    doc = load(d) or {}
    print(json.dumps(doc.get("doc") or {}, indent=1, ensure_ascii=False))


def register(sub) -> None:
    p = sub.add_parser("understand", help="the task understanding behind the Prompt step (writes it if missing)")
    p.add_argument("task")
    p.add_argument("--refresh", action="store_true", help="research and write it again")
    p.set_defaults(func=cmd_understand)
