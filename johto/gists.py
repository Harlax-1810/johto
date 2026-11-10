"""Secret GitHub gists for the Conversation Log and Final Output links."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import config

GH_ACCOUNT = config.GH_ACCOUNT   # studio-ids.json "gh_account" or JOHTO_GH_ACCOUNT; the author's by default

ATTEMPTS = 4
BACKOFF = (2, 5, 10)
TRANSIENT = ("HTTP 400", "HTTP 502", "HTTP 503", "HTTP 504", "timeout", "connection reset", "EOF", "TLS handshake",
             "no such host", "temporarily unavailable")


def _transient(stderr: str) -> bool:
    low = (stderr or "").lower()
    return any(t.lower() in low for t in TRANSIENT)


def _size(path) -> str:
    try:
        n = Path(path).stat().st_size
    except OSError:
        return "size unknown"
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n // 1000} kB"


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
        # gh makes gists secret by default (it has --public, not --secret). A transcript is a megabyte or more,
        # and an upload cut off by the network comes back from GitHub as "HTTP 400 malformed request" or a
        # timeout, so a failed attempt is retried a few times before it counts.
        for attempt in range(ATTEMPTS):
            r = subprocess.run(["gh", "gist", "create", "-d", description, str(dest)],
                               capture_output=True, text=True)
            if r.returncode == 0 or not _transient(r.stderr) or attempt == ATTEMPTS - 1:
                break
            time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
    if r.returncode != 0:
        err = " ".join(r.stderr.split())[-600:]
        hint = (f" GitHub dropped the upload {ATTEMPTS} times in a row ({_size(path)}); check the connection and save again."
                if _transient(r.stderr) else "")
        raise RuntimeError(f"gh gist create failed: {err}{hint}")
    # gh prints the gist URL on stdout and its progress on stderr; which stream holds the link has moved between
    # gh releases, so read both, and as a last resort ask gh for the gist just made under this description.
    url = _url_in(r.stdout) or _url_in(r.stderr) or _find(description)
    if not url:
        raise RuntimeError(f"gh gist create gave no URL. stdout: {r.stdout.strip()[-300:]!r}; stderr: {r.stderr.strip()[-300:]!r}")
    return url


def _url_in(text: str) -> str | None:
    m = re.search(r"https://gist\.github\.com/\S+", text or "")
    return m.group(0).rstrip(".,)") if m else None


def _find(description: str) -> str | None:
    """The newest gist with this description, by `gh gist list` (newest first; the id is its first column)."""
    r = subprocess.run(["gh", "gist", "list", "--limit", "10"], capture_output=True, text=True)
    for ln in r.stdout.splitlines():
        cols = ln.split("\t")
        if len(cols) >= 2 and cols[1].strip() == description:
            return f"https://gist.github.com/{GH_ACCOUNT}/{cols[0].strip()}"
    return None
