"""Read-only GitHub access through the `gh` CLI, with an on-disk cache.

Only GET requests are made. Search calls are throttled to stay under GitHub's
30-per-minute search limit, and 403/429 responses back off and retry.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
import urllib.parse
from pathlib import Path

from .. import config

CACHE = config.DATA / "cache" / "gh"
DAY = 24 * 3600
_search_calls: list[float] = []


class GitHubError(RuntimeError):
    def __init__(self, status: int | None, message: str, path: str):
        self.status, self.path = status, path
        super().__init__(f"GitHub {status or '?'} on {path}: {message[:300]}")


def _key(path: str, accept: str | None) -> Path:
    h = hashlib.sha1(f"{accept or ''} {path}".encode()).hexdigest()
    return CACHE / h[:2] / f"{h}.json"


def _throttle_search() -> None:
    now = time.time()
    while _search_calls and now - _search_calls[0] > 60:
        _search_calls.pop(0)
    if len(_search_calls) >= 25:
        time.sleep(61 - (now - _search_calls[0]))
    _search_calls.append(time.time())


def _status_from(stderr: str) -> int | None:
    for token in ("HTTP 404", "HTTP 403", "HTTP 429", "HTTP 422", "HTTP 409", "HTTP 451", "HTTP 500", "HTTP 502"):
        if token in stderr:
            return int(token.split()[1])
    return None


def get(path: str, params: dict | None = None, ttl: float | None = DAY, accept: str | None = None,
        retries: int = 4):
    """GET a GitHub API path. `ttl=None` caches forever (use for immutable SHAs)."""
    if params:
        path += ("&" if "?" in path else "?") + urllib.parse.urlencode(params)
    cache_file = _key(path, accept)
    if cache_file.exists():
        try:
            entry = json.loads(cache_file.read_text())
            if ttl is None or time.time() - entry["ts"] < ttl:
                if entry.get("error"):
                    raise GitHubError(entry["error"], "cached error", path)
                return entry["data"]
        except (json.JSONDecodeError, KeyError):
            pass
    argv = ["gh", "api", "--method", "GET", path]
    if accept:
        argv += ["-H", f"Accept: {accept}"]
    delay = 5.0
    for attempt in range(retries + 1):
        if path.startswith(("search/", "/search/")):
            _throttle_search()
        out = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        if out.returncode == 0:
            if accept and "raw" in accept:
                data = out.stdout
            else:
                data = json.loads(out.stdout) if out.stdout.strip() else None
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"ts": time.time(), "data": data}))
            return data
        status = _status_from(out.stderr)
        msg = out.stderr.strip()
        if status in (403, 429) and ("rate limit" in msg.lower() or "secondary" in msg.lower()
                                      or status == 429) and attempt < retries:
            time.sleep(delay)
            delay = min(delay * 2, 90)
            continue
        if status in (500, 502) and attempt < retries:
            time.sleep(delay)
            continue
        if status in (404, 409, 451):  # remember stable misses briefly
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"ts": time.time(), "error": status}))
        raise GitHubError(status, msg, path)
    raise GitHubError(None, "retries exhausted", path)


# --- Helpers ---------------------------------------------------------------

def repo(full_name: str) -> dict:
    return get(f"repos/{full_name}")


def head_sha(full_name: str, branch: str) -> str:
    return get(f"repos/{full_name}/commits/{urllib.parse.quote(branch, safe='')}", ttl=6 * 3600)["sha"]


def _slim_commits(cs: list) -> list[dict]:
    def person(p: dict | None) -> dict:
        p = p or {}
        return {k: p[k] for k in ("name", "email", "date") if k in p}
    return [{"sha": c.get("sha"),
             "commit": {"message": (c.get("commit") or {}).get("message", ""),
                        "author": person((c.get("commit") or {}).get("author")),
                        "committer": person((c.get("commit") or {}).get("committer"))},
             "author": {"login": (c.get("author") or {}).get("login")} if c.get("author") else None,
             "parents": [{"sha": x.get("sha")} for x in c.get("parents") or []]}
            for c in cs or [] if isinstance(c, dict)]


def commits(full_name: str, sha: str, n: int = 100) -> list[dict]:
    """Commit history from `sha`, cached forever with only sha, message, author/committer and
    parents kept (the full API response is ~10x larger)."""
    data = get(f"repos/{full_name}/commits", {"sha": sha, "per_page": n}, ttl=None)
    if data and isinstance(data[0], dict) and ("url" in data[0] or "node_id" in data[0]):
        data = _slim_commits(data)
        f = _key(f"repos/{full_name}/commits?" + urllib.parse.urlencode({"sha": sha, "per_page": n}), None)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "data": data}))
        tmp.replace(f)
    return data


def commits_since(full_name: str, branch: str, since_iso: str, n: int = 100) -> list[dict]:
    return get(f"repos/{full_name}/commits", {"sha": branch, "since": since_iso, "per_page": n},
               ttl=6 * 3600)


def _slim_tree(data: dict) -> dict:
    return {"sha": data.get("sha"), "truncated": data.get("truncated"),
            "tree": [{k: e[k] for k in ("path", "type", "size") if k in e} for e in data.get("tree") or []]}


def tree(full_name: str, sha: str) -> dict:
    """Recursive tree at a commit. Cached forever, keeping only path, type and size per entry
    (the full API response is ~4x larger and the cache holds hundreds of repos)."""
    data = get(f"repos/{full_name}/git/trees/{sha}", {"recursive": "1"}, ttl=None)
    entries = (data or {}).get("tree") or []
    if entries and ("url" in entries[0] or "sha" in entries[0]):
        data = _slim_tree(data)
        f = _key(f"repos/{full_name}/git/trees/{sha}?recursive=1", None)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "data": data}))
        tmp.replace(f)
    return data


def readme(full_name: str) -> str:
    try:
        return get(f"repos/{full_name}/readme", accept="application/vnd.github.raw") or ""
    except GitHubError:
        return ""


def file_text(full_name: str, path: str, sha: str) -> str:
    try:
        return get(f"repos/{full_name}/contents/{urllib.parse.quote(path)}", {"ref": sha},
                   accept="application/vnd.github.raw", ttl=None) or ""
    except GitHubError:
        return ""


def search_repos(q: str, sort: str = "stars", per_page: int = 50, page: int = 1) -> list[dict]:
    data = get("search/repositories", {"q": q, "sort": sort, "order": "desc",
                                       "per_page": per_page, "page": page}, ttl=3 * DAY)
    return data.get("items", [])


def search_issues(q: str, per_page: int = 20) -> list[dict]:
    data = get("search/issues", {"q": q, "per_page": per_page}, ttl=DAY)
    return data.get("items", [])


def open_issues(full_name: str, labels: str | None = None, per_page: int = 50) -> list[dict]:
    params = {"state": "open", "per_page": per_page, "sort": "comments", "direction": "desc"}
    if labels:
        params["labels"] = labels
    items = get(f"repos/{full_name}/issues", params, ttl=DAY)
    return [i for i in items if "pull_request" not in i]


def labels(full_name: str) -> list[str]:
    try:
        return [l["name"] for l in get(f"repos/{full_name}/labels", {"per_page": 100}, ttl=7 * DAY)]
    except GitHubError:
        return []


def issue_timeline(full_name: str, number: int) -> list[dict]:
    return get(f"repos/{full_name}/issues/{number}/timeline", {"per_page": 100}, ttl=DAY)
