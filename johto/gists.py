"""Secret GitHub gists for the Conversation Log and Final Output links."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config

GH_ACCOUNT = "austeane"


def active_account() -> str | None:
    r = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True)
    return r.stdout.strip() or None


def create(path: str, name: str, description: str) -> str:
    """Upload one file as a secret gist under `name`; return its URL."""
    config.require_real("gh gist create")
    acct = active_account()
    if acct != GH_ACCOUNT:
        raise RuntimeError(f"gh is logged in as {acct!r}; switch with `gh auth switch -u {GH_ACCOUNT}`")
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / name
        shutil.copyfile(path, dest)
        # gh makes gists secret by default (it has --public, not --secret).
        r = subprocess.run(["gh", "gist", "create", "-d", description, str(dest)],
                           capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh gist create failed: {r.stderr.strip()}")
    url = next((ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith("https://gist.github.com/")), None)
    if not url:
        raise RuntimeError(f"gh gist create gave no URL: {r.stdout!r}")
    return url
