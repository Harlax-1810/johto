"""Fake gh: secret gists saved under data/fake/gists/, and repo metadata without the network."""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid

from .. import config
from ..gists import GH_ACCOUNT
from . import guard_paths

ACCOUNT = GH_ACCOUNT          # tests may set another login to exercise the gh_account check
CREATED: list[dict] = []


def active_account() -> str | None:
    return ACCOUNT


def create(path: str, name: str, description: str) -> str:
    guard_paths()
    if active_account() != GH_ACCOUNT:
        raise RuntimeError(f"gh is logged in as {active_account()!r}; switch with `gh auth switch -u {GH_ACCOUNT}`")
    gid = uuid.uuid4().hex[:20]
    dest = config.DATA / "fake" / "gists" / gid / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, dest)
    url = f"https://gist.github.com/{GH_ACCOUNT}/{gid}"
    CREATED.append({"url": url, "name": name, "description": description, "path": str(dest)})
    return url


def repo_info(full_name: str, commit: str | None = None, language: str | None = None) -> dict:
    """What johto.actions.repo_info returns, from data/scout/repos.json when the repo is there."""
    vetted = {}
    p = config.DATA / "scout" / "repos.json"
    if p.exists():
        vetted = json.loads(p.read_text()).get(full_name.lower()) or {}
    full = vetted.get("full_name") or full_name
    if not commit or len(commit) != 40:
        commit = (vetted.get("commit") if not commit else None) or hashlib.sha1(
            f"{full}@{commit or 'HEAD'}".encode()).hexdigest()
    lang = language or vetted.get("studio_language") or vetted.get("language") or "Other"
    return {"full_name": full, "url": f"https://github.com/{full}", "commit": commit,
            "pinned_url": f"https://github.com/{full}/tree/{commit}", "language": lang,
            "fork": bool(vetted.get("fork")), "description": vetted.get("description")}
