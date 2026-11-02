"""data/dossiers/: index.json (the queue and its states) and one <id>.json per dossier.

Several processes touch the index (the web server's worker thread and `johto dossier ...`), so every
read-modify-write holds an exclusive flock on a sidecar lock file and writes atomically. Plain reads
need no lock because writes are atomic renames.

A second lock file serialises generations across processes: whoever holds it is generating. Status
probes take it shared and non-blocking and never write to it, so they can't make a generation miss it.

index["stop"] maps an item id to the reason a stop was asked for (pause, shutdown). The process
generating that item polls for it and stops; it works across processes.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import threading
import time
from pathlib import Path

from .. import config

TARGET = 10
ACTIVE = ("ready", "generating", "queued")
CLOSED = ("actioned", "declined")
STATUSES = ("queued", "generating", "ready", "failed", "actioned", "declined")
DECLINE_REASONS = ("too_easy", "too_big", "cant_run", "not_interested", "ai_generated", "other")


class Paths:
    """Resolved lazily so tests can point DATA somewhere else."""

    def __init__(self, data: Path | None = None):
        self.data = data or config.DATA

    @property
    def dir(self) -> Path:
        return self.data / "dossiers"

    @property
    def index(self) -> Path:
        return self.dir / "index.json"

    @property
    def runs(self) -> Path:
        return self.dir / "runs"

    @property
    def clones(self) -> Path:
        return self.data / "cache" / "dossier-clones"

    @property
    def index_lock(self) -> Path:
        return self.dir / ".index.lock"

    @property
    def gen_lock(self) -> Path:
        return self.dir / ".generate.lock"


P = Paths()


def use_data_dir(data: Path) -> None:
    """Point the store at another data directory (tests)."""
    global P
    P = Paths(data)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def make_id(repo: str, commit: str) -> str:
    owner, _, name = repo.partition("/")
    return re.sub(r"[^a-z0-9_.-]", "-", f"{owner}__{name}__{commit[:8]}".lower())


def _default() -> dict:
    # A new index starts paused: Austin unpauses the worker himself.
    return {"target": TARGET, "paused": True, "items": []}


def load() -> dict:
    try:
        idx = json.loads(P.index.read_text())
    except FileNotFoundError:
        return _default()
    except json.JSONDecodeError:
        time.sleep(0.05)  # never happens with atomic writes; one retry for safety
        idx = json.loads(P.index.read_text())
    idx.setdefault("target", TARGET)
    idx.setdefault("paused", True)
    idx.setdefault("items", [])
    return idx


def _write(idx: dict) -> None:
    P.dir.mkdir(parents=True, exist_ok=True)
    tmp = P.index.with_name(f".index.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(idx, indent=1))
    os.replace(tmp, P.index)


@contextlib.contextmanager
def edit():
    """Exclusive read-modify-write of index.json. Creates it (paused) on first use."""
    P.dir.mkdir(parents=True, exist_ok=True)
    with open(P.index_lock, "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            idx = load()
            yield idx
            _write(idx)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def find(idx: dict, did: str) -> dict | None:
    return next((it for it in idx["items"] if it["id"] == did), None)


def find_repo(idx: dict, repo: str) -> dict | None:
    r = repo.lower()
    return next((it for it in idx["items"] if it["repo"].lower() == r), None)


def get(did: str) -> dict | None:
    return find(load(), did)


def update(did: str, **fields) -> dict | None:
    with edit() as idx:
        it = find(idx, did)
        if it is None:
            return None
        it.update(fields)
        return dict(it)


def new_item(entry: dict, rank: int | None, manual: bool = False) -> dict:
    return {
        "id": make_id(entry["full_name"], entry["commit"]), "repo": entry["full_name"], "commit": entry["commit"],
        "status": "queued", "rank": rank, "promise": entry.get("promise"),
        "promise_reasons": list(entry.get("reasons") or []), "fits": list(entry.get("fits") or []),
        "display_name": entry.get("display_name") or entry["full_name"],
        "pinned_url": entry.get("pinned_url"), "language": entry.get("studio_language") or entry.get("language"),
        "queued_at": now_iso(), "started_at": None, "finished_at": None, "closed_at": None,
        "progress": None, "elapsed_s": None, "cost_usd": None, "error": None, "errors": None,
        "decline": None, "actioned": None, "manual": manual,
        # Slot groups ("Action|Domain|Codebase type") this research was started for: the prompt asks for
        # opportunities that fit them, and the brief picks seeds for them.
        "focus": list(entry.get("focus") or []),
    }


# --- dossier files ----------------------------------------------------------------

def dossier_path(did: str) -> Path:
    return P.dir / f"{did}.json"


def load_dossier(did: str) -> dict | None:
    p = dossier_path(did)
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_dossier(doc: dict) -> Path:
    P.dir.mkdir(parents=True, exist_ok=True)
    p = dossier_path(doc["id"])
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    os.replace(tmp, p)
    return p


def run_log(did: str) -> Path:
    P.runs.mkdir(parents=True, exist_ok=True)
    return P.runs / f"{did}.log"


# --- the generation lock --------------------------------------------------------------

_held = 0  # how many generation locks this process holds (any thread)
_held_lock = threading.Lock()


@contextlib.contextmanager
def generation_lock(blocking: bool = False, wait_s: float = 0.0):
    """Yields True when this process now holds the one-generation-at-a-time lock, else False.
    `wait_s` retries a non-blocking attempt for that long (rides over a status probe)."""
    global _held
    P.dir.mkdir(parents=True, exist_ok=True)
    with open(P.gen_lock, "a+") as lf:
        until = time.time() + wait_s
        while True:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                break
            except BlockingIOError:
                if time.time() >= until:
                    yield False
                    return
                time.sleep(0.05)
        with _held_lock:
            _held += 1
        try:
            lf.seek(0)
            lf.truncate()
            lf.write(f"{os.getpid()}\n")
            lf.flush()
            yield True
        finally:
            with _held_lock:
                _held -= 1
            fcntl.flock(lf, fcntl.LOCK_UN)


def held_here() -> bool:
    return _held > 0


def generation_busy() -> bool:
    """True when any process (this one included) holds the generation lock. Never writes the lock file."""
    if held_here():
        return True
    if not P.gen_lock.exists():
        return False
    try:
        with open(P.gen_lock, "r") as lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lf, fcntl.LOCK_UN)
            return False
    except OSError:
        return False


generating_elsewhere = generation_busy  # the old name


# --- stop requests ----------------------------------------------------------------------

def request_stop(idx: dict, did: str, reason: str) -> None:
    """Inside an edit(): ask the process generating `did` to stop."""
    idx.setdefault("stop", {})[did] = reason


def stop_reason(did: str) -> str | None:
    return (load().get("stop") or {}).get(did)


def clear_stop(idx: dict, did: str) -> None:
    stops = idx.get("stop") or {}
    stops.pop(did, None)
    if not stops:
        idx.pop("stop", None)


def requeue_stale() -> list[str]:
    """Items left 'generating' by a process that died go back to 'queued'. Returns their ids."""
    if not P.index.exists():
        return []
    with generation_lock(blocking=False) as got:
        if not got:  # a live generation holds the lock; its item is not stale
            return []
        moved = []
        with edit() as idx:
            for it in idx["items"]:
                if it["status"] == "generating":
                    it.update(status="queued", started_at=None, progress=None,
                              error="interrupted; requeued")
                    moved.append(it["id"])
            idx.pop("stop", None)  # nothing is generating, so no stop request is live
        return moved
