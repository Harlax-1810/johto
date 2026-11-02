#!/usr/bin/env python3
"""Log a new prework lane in to Claude with Austin's long-lived token, so he doesn't type /login in every lane.

    python3 -I lane_login.py [run folder]     (default: the current folder)

The mercor-prework wrapper (Mac and PC) runs this after every successful `setup`. It reads the lane's container
from the run folder's prework-manifest.json and writes <home>/cc-config/<key>_model_a/.credentials.json: the file
a /login writes, holding the token from <home>/lane-token instead (made once with `claude setup-token`).

- It never overwrites a login that is already there, and does nothing without a valid token file.
- Claude Code 2.1.311 reads the file as a Max login and never refreshes it: there is no refresh token, and the
  expiry is about 11 months out (tested 2026-11-01).
- It stays in the lane's config dir like a /login: prework.py's collect copies only transcripts, and
  archiving a run deletes the login first.

Standard library only: the PC runs a copy at ~/.local/share/mercor-prework/lane_login.py, so copy it again
after changing this file. <home> is ~/.mercor-prework, or PREWORK_HOME for tests.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

TOKEN_FILE = "lane-token"
LOGIN_FILE = ".credentials.json"          # prework.CLAUDE_LOGIN_FILE
TOKEN_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{40,}")
CONTAINER_RE = re.compile(r"^prework-(.+)-model_a$")
LOGIN_DAYS = 330                           # inside the token's one-year life
# Austin's plan, as his own /login records it. Claude Code shows "Claude Max" and applies the Max limits from these.
PLAN = {"subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"}


def home() -> Path:
    return Path(os.environ.get("PREWORK_HOME") or Path.home() / ".mercor-prework").expanduser()


def read_token(h: Path) -> str | None:
    try:
        t = (h / TOKEN_FILE).read_text().strip()
    except OSError:
        return None
    return t if TOKEN_RE.fullmatch(t) else None


def login(token: str, now: float | None = None) -> dict:
    expires = ((now if now is not None else time.time()) + LOGIN_DAYS * 86400) * 1000
    return {"claudeAiOauth": {"accessToken": token, "refreshToken": None, "expiresAt": int(expires),
                              "scopes": ["user:inference"], **PLAN}}


def config_dir(run_dir: Path, h: Path) -> Path | None:
    try:
        container = json.loads((run_dir / "prework-manifest.json").read_text())["lane"]["container"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    m = CONTAINER_RE.match(container or "")
    return h / "cc-config" / f"{m.group(1)}_model_a" if m else None


def seed(run_dir: Path, h: Path | None = None) -> tuple[str, str]:
    """(status, message). status: seeded, kept (a login is already there), no_token, no_lane."""
    h = h or home()
    cfg = config_dir(Path(run_dir), h)
    if cfg is None or not cfg.is_dir():
        return "no_lane", "no lane config folder found from prework-manifest.json"
    target = cfg / LOGIN_FILE
    if target.exists():
        return "kept", f"{target} already has a login"
    token = read_token(h)
    if token is None:
        return "no_token", f"no valid token in {h / TOKEN_FILE}; type /login in the lane"
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return "kept", f"{target} already has a login"
    with os.fdopen(fd, "w") as f:
        json.dump(login(token), f, indent=2)
        f.write("\n")
    return "seeded", f"logged in from {h / TOKEN_FILE}, so no /login is needed"


def main(argv: list[str]) -> int:
    status, msg = seed(Path(argv[1]) if len(argv) > 1 else Path.cwd())
    print(f"[{'OK' if status in ('seeded', 'kept') else 'INFO'}] Claude login: {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
