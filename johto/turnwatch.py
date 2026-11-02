"""Turn watcher: notices when Claude finishes a turn in a live lane, on this Mac or the PC, and gets the GPT Pro
review going without Austin asking.

Each tick (every 20 s by default):
- finds the live runs: every run in tasks/*/task.json that isn't torn down and whose container is running,
  here (`docker ps`) or on its host (one ssh per host, which also stats the lanes' transcripts there). A host
  that doesn't answer, or whose Docker is stopped (the PC while a game runs), is skipped for that tick.
- reads a run's turns through johto.budget, only when its transcript changed. A turn has ended when budget
  says end_turn and the transcript has been quiet for QUIET_S.
- on a newly ended turn: classifies it (budget.turn_facts: question, permission, substantive, progress,
  background or problem) and logs turn_ended with its kind. Under settings review_policy "meaningful" (the
  default), it reviews only a substantive turn: in an async slot (task.json interaction_mode "Async/..."),
  each one; in an interactive slot, the first, then one whose diff changed since the pack GPT last saw
  (delta.diff_changed), or a new report in an "Understand / Assess" task. Any other turn is a checkpoint:
  state and event, no pack. "every_turn" reviews each turn of review_min_working_minutes or more, as before.
  A review builds the next pack (johto.delta.next_pack) and, with auto_review on, sends it to GPT Pro
  (johto.reviews.send_review). A failed step is retried with backoff, up to MAX_ATTEMPTS tries.
- while a turn is open and the transcript is still: a pending AskUserQuestion means Claude is asking Austin
  something; another pending tool call gets one read-only look at the lane's screen (tmux capture-pane), for
  a permission dialog or a login or usage-limit stop.
- saves GPT Pro replies that have landed (reviews.fetch_pending) and says so, and stops waiting on a send
  with no reply after reviews.PENDING_TTL_S (reviews.expire_pending).
- nudges once per run at each Claude-minutes threshold (settings budget_nudges).

Notifications: Claude asking something, waiting on a permission or stopped by a problem make a sound; a turn
finished, a review sent or a review ready are silent.

It never collects or stops a lane, and reads one only through read-only commands (the diff, the screen):
the web app's lane watcher (johto/web/lane_watch.py) collects a run when Claude exits.

State lives in data/turnwatch.json, per "<short>/<run>": the transcript version last read, the last turn
handled, the nudges sent and each turn's pack and review status, so a restart never re-packs or re-sends a
turn. A run first seen with turns that ended before the watcher was looking marks them as seen, without a
review. One watcher at a time: data/turnwatch.lock (flock, with the pid inside; a dead holder's lock is free).

`johto watch --dry-run` prints what it sees and would do, and writes nothing. JOHTO_NO_NOTIFY=1, and fake
mode, turn off notifications. With JOHTO_ROOT on a copy of data/ and tasks/, packs are built but nothing goes
to ChatGPT unless JOHTO_REAL_STUDIO=1. Tests swap the hooks (_budget, _delta, _reviews, _running_local, _ssh,
_screen, _notify).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import config, lanes, settings, stages, workspace
from .web.lane import monitor

QUIET_S = 20             # a turn counts as ended once the transcript has been still this long
BUSY_READ_S = 60         # while Claude works, read its turns this often (for the nudges)
FRESH_S = 30 * 60        # turns that ended longer ago than this, when the watcher first sees a run, are not reviewed
FETCH_EVERY_S = 60       # how often to ask ChatGPT about replies still pending
BACKGROUND_WAIT_S = 5 * 60   # a turn that ended with a background job running waits this long for it, then goes
BACKOFF_S = (60, 300, 900)
QUEUED_EVERY_S = 60      # a pack waiting on the run's last review checks again this often
MAX_ATTEMPTS = len(BACKOFF_S) + 1
SSH_TIMEOUT_S = 20
CONNECT_S = 6
REMOTE_PREFIX = "prework-"
_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_REVIEW_RE = re.compile(r"^(?P<run>.+?)-t(?P<turn>\d+)-(?P<source>.+?)(?:-\d+)?\.md$")
ASK_TOOL = "AskUserQuestion"
POLICIES = ("meaningful", "every_turn")
# What other johto modules may raise; SystemExit too, since a missing piece (no chatgpt module) exits that way
# and must not stop the watcher.
_TROUBLE = (Exception, SystemExit)


# --- hooks (tests replace these) ------------------------------------------------------------------------

def _budget():
    from . import budget
    return budget


def _delta():
    from . import delta
    return delta


def _reviews():
    from . import reviews
    return reviews


def _running_local() -> set[str]:
    return set(monitor.running_containers())


def _ssh(host: str, script: str, timeout: float = SSH_TIMEOUT_S) -> subprocess.CompletedProcess | None:
    """One read-only command on another machine. None when ssh can't run or times out."""
    try:
        return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_S}", host, script],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _may_send() -> bool:
    """Real GPT Pro sends only from the checkout's own data, as with Studio and lanes (JOHTO_REAL_STUDIO=1
    allows a scratch copy); fake mode goes to the fakes."""
    return bool(config.FAKE or config.ROOT == config.CHECKOUT or getattr(config, "REAL_OK", False))


def _quiet() -> bool:
    return config.FAKE or os.environ.get("JOHTO_NO_NOTIFY", "").strip().lower() in ("1", "true", "yes")


def _screen(r: dict) -> dict | None:
    """The lane's screen (monitor.screen of its tmux pane), read-only, or None when it can't be read."""
    if r.get("host"):
        cmd = shlex.join(["docker", "exec", "-u", "lane", "-e", "HOME=/home/lane", r["container"],
                          "tmux", "capture-pane", "-p", "-J", "-t", "lane"])
        res = _ssh(r["host"], cmd)
        pane = res.stdout if res is not None and res.returncode == 0 else None
    elif monitor.docker_free():
        return None
    else:
        pane = monitor.look(r["container"]).get("pane")
    return monitor.screen(pane)


def _notify(title: str, body: str, href: str | None = None, attention: bool = False) -> None:
    """A macOS notification, the way lane_watch sends one with no tab open (terminal-notifier when installed,
    so a click opens href). Silent unless `attention` (Claude is waiting on Austin)."""
    if _quiet():
        return
    try:
        tn = shutil.which("terminal-notifier")
        if tn:
            cmd = [tn, "-title", title, "-message", body, "-group", f"johto-watch-{title}"]
            cmd += ["-sound", "default"] if attention else []
            subprocess.run(cmd + (["-open", href] if href else []), capture_output=True, timeout=10)
            return
        clean = lambda s: s.replace('"', "").replace("\\", "")  # noqa: E731
        script = f'display notification "{clean(body)}" with title "{clean(title)}"'
        script += ' sound name "Glass"' if attention else ""
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


# --- state and lock -------------------------------------------------------------------------------------

def state_path() -> Path:
    return config.DATA / "turnwatch.json"


def lock_path() -> Path:
    return config.DATA / "turnwatch.lock"


def load_state() -> dict:
    try:
        s = json.loads(state_path().read_text())
    except (FileNotFoundError, ValueError):
        s = {}
    if not isinstance(s, dict):
        s = {}
    s.setdefault("runs", {})
    s.setdefault("reviews_notified", [])
    return s


def save_state(s: dict) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(s, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, p)


def acquire() -> int | None:
    """Take the single-watcher lock; returns its fd, or None while another live watcher holds it. The kernel
    drops the lock when its holder dies, so a stale pid in the file never blocks a new watcher."""
    p = lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def release(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.ftruncate(fd, 0)
    finally:
        os.close(fd)


def holder() -> int | None:
    """The pid written by the watcher holding (or last holding) the lock."""
    try:
        return int(lock_path().read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


# --- finding live runs ----------------------------------------------------------------------------------

def _local_sig(run_dir: Path) -> list | None:
    """[newest mtime, total size] of the lane's transcripts on this Mac, or None before Claude starts."""
    files = monitor.transcripts(monitor.lane_paths(run_dir)["projects"])
    if not files:
        return None
    stats = [f.stat() for f in files]
    return [max(s.st_mtime for s in stats), sum(s.st_size for s in stats)]


def probe_host(host: str, keys: list[str]) -> dict:
    """One ssh: the host's clock, its running lane containers and a stat of each lane's transcripts.
    {"error": str|None, "now": float, "running": set, "sigs": {key: [mtime, size]}}."""
    keys = [k for k in keys if k and _KEY_RE.match(k)]
    script = ("date +%s.%N; echo @@docker; "
              f"docker ps --filter name=^{REMOTE_PREFIX} --format '{{{{.Names}}}}' 2>/dev/null; echo \"@@rc $?\"; ")
    if keys:
        script += ("for k in " + " ".join(shlex.quote(k) for k in keys) + "; do "
                   "find \"$HOME/.mercor-prework/cc-config/${k}_model_a/projects\" -mindepth 2 -maxdepth 2 "
                   "-name '*.jsonl' -printf \"$k %T@ %s\\n\" 2>/dev/null; done; ")
    script += "true"
    out = {"error": None, "now": time.time(), "running": set(), "sigs": {}}
    r = _ssh(host, script)
    if r is None:
        out["error"] = f"{host} didn't answer"
        return out
    if r.returncode != 0 or "@@docker" not in r.stdout or "@@rc " not in r.stdout:
        out["error"] = f"ssh to {host} failed"
        return out
    head, _, rest = r.stdout.partition("@@docker\n")
    names, _, tail = rest.partition("@@rc ")
    rc, _, finds = tail.partition("\n")
    if rc.strip() != "0":
        out["error"] = f"Docker isn't running on {host}"
        return out
    try:
        out["now"] = float(head.strip().splitlines()[-1])
    except (ValueError, IndexError):
        pass
    out["running"] = {n for n in names.split() if n.startswith(REMOTE_PREFIX)}
    for line in finds.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            mtime, size = float(parts[1]), int(parts[2])
        except ValueError:
            continue
        sig = out["sigs"].setdefault(parts[0], [0.0, 0])
        sig[0], sig[1] = max(sig[0], mtime), sig[1] + size
    return out


def discover() -> list[dict]:
    """Every run that isn't torn down: where its lane is, and live True / False / None (can't tell this tick).
    A live run also has sig ([mtime, size] of its transcripts, or None) and now (that machine's clock)."""
    runs: list[dict] = []
    for tj in sorted(config.TASKS.glob("*/task.json")):
        d = tj.parent
        try:
            meta = workspace.load(d)
        except (OSError, ValueError):
            continue
        for rec in meta.get("runs", []):
            if rec.get("torn_down_at") or not rec.get("dir"):
                continue
            run_dir = d / rec["dir"]
            container = rec.get("container") or (lanes.manifest(run_dir).get("lane") or {}).get("container")
            if not container:
                continue
            remote = lanes.remote_of(rec)
            runs.append({"key": f"{d.name}/{run_dir.name}", "short": d.name, "run": run_dir.name, "task_dir": d,
                         "run_dir": run_dir, "container": container, "host": remote[0] if remote else None,
                         "where": remote[0] if remote else "this Mac", "live": False, "sig": None,
                         "now": time.time(), "why": None})
    local = [r for r in runs if not r["host"]]
    if local:
        running = _running_local()
        for r in local:
            r["live"] = r["container"] in running
            if r["live"]:
                r["sig"], r["now"] = _local_sig(r["run_dir"]), time.time()
    for host in sorted({r["host"] for r in runs if r["host"]}):
        mine = [r for r in runs if r["host"] == host]
        if config.FAKE:
            for r in mine:
                r["live"], r["why"] = None, "not checked in fake mode"
            continue
        probe = probe_host(host, [lanes.lane_key(r["container"]) for r in mine])
        for r in mine:
            if probe["error"]:
                r["live"], r["why"] = None, probe["error"]
                continue
            r["live"] = r["container"] in probe["running"]
            if r["live"]:
                r["sig"], r["now"] = probe["sigs"].get(lanes.lane_key(r["container"]) or ""), probe["now"]
    return runs


# --- one tick -------------------------------------------------------------------------------------------

@dataclass
class _Tick:
    state: dict
    sets: dict
    dry_run: bool
    log: Callable[[str], None]
    now: float = field(default_factory=time.time)
    actions: list = field(default_factory=list)
    reported: set = field(default_factory=set)

    def persist(self) -> None:
        if not self.dry_run:
            save_state(self.state)

    def act(self, r: dict, action: str, turn: int | None = None, detail: str = "") -> dict:
        a = {"key": r["key"], "short": r["short"], "run": r["run"], "action": action, "turn": turn,
             "detail": detail, "dry_run": self.dry_run}
        self.actions.append(a)
        if not self.dry_run and action not in ("wait",):
            self.log(f"{r['short']} {r['run']}: {detail or action}")
        return a


def _label(r: dict) -> str:
    return f"{r['short']} {r['run']}"


def _mins(x) -> str:
    """12 minutes, 2.5 minutes, 1 minute."""
    try:
        m = float(x or 0)
    except (TypeError, ValueError):
        m = 0.0
    if m < 10 and round(m, 1) != round(m):
        return f"{m:.1f} minutes"
    n = round(m)
    return f"{n} minute{'' if n == 1 else 's'}"


def _span_of(seconds: float) -> str:
    """How long, in minutes up to 90, then hours."""
    m = seconds / 60
    return _mins(m) if m < 90 else f"{m / 60:.1f} hours"


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _span(nums: list[int]) -> str:
    nums = sorted(nums)
    if len(nums) == 1:
        return f"turn {nums[0]}"
    if nums == list(range(nums[0], nums[-1] + 1)):
        return f"turns {nums[0]}-{nums[-1]}"
    return "turns " + ", ".join(map(str, nums))


def _new_entry(r: dict) -> dict:
    return {"where": r["where"], "first_seen": stages.now_iso(), "seen": None, "read_at": 0, "last_turn": 0,
            "baselined": False, "nudges": [], "turns": {}}


def _status(t: _Tick, r: dict, ent: dict) -> dict | None:
    try:
        st = _budget().status(r["task_dir"], r["run"])
    except _TROUBLE as e:   # one run's trouble never stops the others
        msg = f"couldn't read its turns: {e}"
        if ent.get("last_error") != msg or t.dry_run:
            t.act(r, "skip", detail=msg)
        ent["last_error"] = msg
        return None
    ent.pop("last_error", None)
    ent["read_at"] = time.time()
    return st


def _check_run(t: _Tick, r: dict) -> None:
    runs = t.state["runs"]
    ent = runs.get(r["key"])
    if ent is None:
        ent = runs[r["key"]] = _new_entry(r)
    ent["where"] = r["where"]
    _retry_due(t, r, ent)
    sig = r.get("sig")
    if not sig:
        if t.dry_run:
            t.act(r, "wait", detail="no transcript yet (Claude hasn't had a prompt)")
        return
    quiet = r["now"] - sig[0]
    changed = ent.get("seen") != list(sig)
    if quiet < QUIET_S:
        # Claude is working (or the turn just ended): read its minutes now and then, for the nudges.
        if not t.dry_run and time.time() - (ent.get("read_at") or 0) < BUSY_READ_S:
            return
        st = _status(t, r, ent)
        if st is not None:
            _baseline(t, r, ent, st, quiet)
            _nudges(t, r, ent, st)
            if t.dry_run:
                _report(t, r, ent, st, quiet, busy=True)
        return
    recheck = ent.get("recheck_at") and time.time() >= ent["recheck_at"]
    if not changed and not recheck and not t.dry_run:
        return
    st = _status(t, r, ent)
    if st is None:
        return
    _baseline(t, r, ent, st, quiet)
    _waiting(t, r, ent, st)
    later = _turns(t, r, ent, st)
    if later:
        ent["recheck_at"] = later                 # a background job may finish without writing to the transcript
    else:
        ent.pop("recheck_at", None)
    _nudges(t, r, ent, st)
    ent["seen"] = list(sig)
    if t.dry_run:
        _report(t, r, ent, st, quiet, busy=False)


def _baseline(t: _Tick, r: dict, ent: dict, st: dict, quiet: float) -> None:
    """The first time the watcher reads a run: turns that ended before it was looking (all but a last turn
    that ended within FRESH_S) are marked as seen, not reviewed. Nudges already passed on a run that has been
    still for FRESH_S are marked sent without a notification."""
    if ent.get("baselined"):
        return
    ent["baselined"] = True
    turns = st.get("turns") or []
    last = turns[-1] if turns else None
    old = []
    for tr in turns:
        if tr is last and tr.get("ended") == "open":
            continue
        end = _epoch(tr.get("end"))
        age = time.time() - end if end else quiet
        if tr is not last or age > FRESH_S:
            old.append(tr)
    if quiet > FRESH_S:
        try:
            ent["nudges"] = sorted(set(ent.get("nudges") or []) | set(_crossed(st, ent)))
        except _TROUBLE:   # _nudges reports budget trouble
            pass
    if not old:
        return
    ent["last_turn"] = max(tr["index"] for tr in old)
    for tr in old:
        ent["turns"][str(tr["index"])] = {"status": "baseline", "end": tr.get("end"),
                                          "working_minutes": tr.get("working_minutes")}
    t.act(r, "baseline", ent["last_turn"],
          f"{'would mark' if t.dry_run else 'marked'} {_span([tr['index'] for tr in old])} as seen without a review "
          "(ended before the watcher was looking)")


def _turns(t: _Tick, r: dict, ent: dict, st: dict) -> float | None:
    """Handle the turns that ended since the last look. Returns when to look again although the transcript
    hasn't changed (the last turn ended while a background job runs), else None."""
    turns = st.get("turns") or []
    if not turns:
        return None
    last = turns[-1]
    min_wm = float(t.sets.get("review_min_working_minutes") or 0)
    for tr in turns:
        i = tr.get("index")
        if tr.get("ended") != "end_turn" or not isinstance(i, int) or i < ent["last_turn"]:
            continue
        if tr is last and (tr.get("background_open") or st.get("background_running")):
            # Claude ended its turn with a background job running (tests, a build): give the job a few
            # minutes to finish and wake Claude up. A dev server never finishes, so after that, review anyway.
            end = _epoch(tr.get("end")) or time.time()
            if time.time() - end < BACKGROUND_WAIT_S:
                if t.dry_run:
                    t.act(r, "wait", i, f"turn {i} ended with a background job running; would wait for it until "
                          + time.strftime("%H:%M", time.localtime(end + BACKGROUND_WAIT_S)))
                return min(end + BACKGROUND_WAIT_S, time.time() + BUSY_READ_S)
        rec = ent["turns"].get(str(i))
        continued = False
        if i == ent["last_turn"]:
            # The same turn ending again later: Claude picked up after a background job and worked more.
            if not rec or rec.get("status") == "baseline" or tr.get("end") == rec.get("end"):
                continue
            grew = float(tr.get("working_minutes") or 0) - float(rec.get("working_minutes") or 0)
            if grew < max(min_wm, 1.0) or tr is not last:
                if not t.dry_run:
                    rec["end"] = tr.get("end")
                continue
            continued = True
        if tr is not last:
            # Austin already sent his next prompt: note the turn, nothing to review.
            if not t.dry_run:
                stages.log(r["task_dir"], "turn_ended", run=r["run"], turn=i,
                           working_minutes=tr.get("working_minutes"))
                ent["turns"][str(i)] = {"status": "moved_on", "end": tr.get("end"),
                                        "working_minutes": tr.get("working_minutes")}
            ent["last_turn"] = i
            continue
        _handle(t, r, ent, tr, continued, st)
    return None


def _slot(r: dict) -> dict:
    try:
        return workspace.load(r["task_dir"]).get("slot") or {}
    except (OSError, ValueError):
        return {}


def _facts(st: dict, tr: dict, min_wm: float, screen: dict | None = None) -> dict:
    """budget.turn_facts for the turn. A budget without it (an older one) gives the old rule: work of at least
    review_min_working_minutes is worth a review."""
    fn = getattr(_budget(), "turn_facts", None)
    if fn is None:
        short = float(tr.get("working_minutes") or 0) < min_wm
        return {"kind": "progress" if short else "substantive", "why": "short" if short else "", "short": short,
                "asks": None, "report_chars": 0, "pending_tool": None, "permission": None, "problem": None}
    return fn(st, tr, screen=screen, min_working_minutes=min_wm)


def _diff_moved(r: dict) -> bool | None:
    """delta.diff_changed: did Claude's diff change since the pack GPT last saw (None: GPT saw none yet)."""
    fn = getattr(_delta(), "diff_changed", None)
    if fn is None:
        return None
    try:
        return fn(r["task_dir"], r["run"])
    except _TROUBLE:
        return None


def _decide(t: _Tick, r: dict, facts: dict, wm: float, min_wm: float) -> tuple[bool, str]:
    """(review it?, why not). See the module docstring for the two policies."""
    kind = facts.get("kind")
    if kind == "problem":
        return False, f"Claude stopped: {facts.get('problem') or facts.get('why')}"
    policy = t.sets.get("review_policy") if t.sets.get("review_policy") in POLICIES else "meaningful"
    if policy == "every_turn" or facts.get("short"):
        return (True, "") if wm >= min_wm else (False, "too short to review")
    if kind != "substantive":
        return False, {"question": "Claude asked a question", "permission": "Claude is waiting for permission",
                       "background": "a background job is still running"}.get(kind, "a progress update")
    slot = _slot(r)
    if "async" in str(slot.get("interaction_mode") or "").lower():
        return True, ""
    moved = _diff_moved(r)
    if moved is None or moved:
        return True, ""
    if "assess" in str(slot.get("action") or "").lower() and facts.get("report_chars"):
        return True, ""                            # an assessment task's answer is its report
    return False, "the code hasn't changed since the last review"


def _ask_line(facts: dict) -> str | None:
    """What to tell Austin when Claude waits on him, or None."""
    if facts.get("kind") == "permission":
        return f"Claude is waiting for permission: {facts.get('permission')}"
    if facts.get("kind") == "problem":
        return f"Claude needs you: {facts.get('problem')}"
    if facts.get("asks"):
        return f"Claude asked you something: {facts['asks']}"
    return None


def _handle(t: _Tick, r: dict, ent: dict, tr: dict, continued: bool, st: dict | None = None) -> None:
    i = tr["index"]
    wm = round(float(tr.get("working_minutes") or 0), 1)
    min_wm = float(t.sets.get("review_min_working_minutes") or 0)
    # The background wait is over by now (see _turns): judge the turn on what it did.
    facts = _facts(dict(st or {}, background_running=0), tr, min_wm)
    kind = facts.get("kind") or "substantive"
    review, why_not = _decide(t, r, facts, wm, min_wm)
    ask = _ask_line(facts)
    what = f"Claude {'finished turn' if not continued else 'finished more work in turn'} {i} ({_mins(wm)}, {kind})"
    t.act(r, "turn_ended", i, ("would log: " if t.dry_run else "") + what)
    status = "pack" if review else ("short" if why_not == "too short to review" else
                                    "problem" if kind == "problem" else "checkpoint")
    rec = {"status": status, "end": tr.get("end"), "working_minutes": wm, "ended_at": stages.now_iso(),
           "attempts": 0, "kind": kind}
    if t.dry_run:
        if ask:
            t.act(r, "ask", i, f"would say: {ask}")
        if not review:
            t.act(r, "no_review", i, f"would save a checkpoint without a review: {why_not}")
        else:
            t.act(r, "pack", i, "would build the review pack")
            if t.sets.get("auto_review") and not _may_send():
                t.act(r, "send", i, "would not send it: JOHTO_ROOT is a copy, not the checkout's data")
            elif t.sets.get("auto_review"):
                busy = _busy(r)
                t.act(r, "send", i, f"would send it to GPT Pro once the review of {busy} is back" if busy
                      else "would send it to GPT Pro")
        return
    stages.log(r["task_dir"], "turn_ended", run=r["run"], turn=i, working_minutes=wm,
               continued=True if continued else None, turn_kind=kind, why=facts.get("why") or None,
               review=review, asked=facts.get("asks") or None)
    for k, old in ent["turns"].items():           # this turn's pack covers whatever an older retry would send
        if old.get("status") in ("failed", "sending") and k != str(i):
            old["status"] = "superseded"
    ent["turns"][str(i)] = rec
    ent["last_turn"] = i
    t.persist()
    if ask:
        t.act(r, "ask", i, ask)
        _notify(f"Johto: {_label(r)} needs you", ask, attention=True)
    if not review:
        t.act(r, "no_review", i, f"turn {i}: checkpoint saved, no review ({why_not})")
        if not ask:
            _notify(f"Johto: turn {i} done", f"{_label(r)} · {_mins(wm)} of work · {why_not}"
                    if why_not == "too short to review" else f"{_label(r)} · {_mins(wm)} of work · no review: {why_not}")
        return
    _advance(t, r, ent, i, rec)


def _waiting(t: _Tick, r: dict, ent: dict, st: dict) -> None:
    """The last turn is open and the transcript is still: Claude may be waiting on Austin. A pending
    AskUserQuestion says so; another pending tool call gets one read of the screen, for a permission dialog,
    a login or a usage limit. One notification per tool call."""
    turns = st.get("turns") or []
    if not turns or turns[-1].get("ended") != "open" or getattr(_budget(), "turn_facts", None) is None:
        return
    tr = turns[-1]
    min_wm = float(t.sets.get("review_min_working_minutes") or 0)
    try:
        facts = _facts(st, tr, min_wm)
        pend = facts.get("pending_tool") or {}
        if not pend.get("id") or ent.get("asked_tool") == pend["id"]:
            return
        if pend.get("name") != ASK_TOOL:
            if t.dry_run:
                t.act(r, "wait", tr.get("index"), f"would look at the lane's screen: {pend.get('name')} has no result yet")
                return
            scr = _screen(r)
            facts = _facts(st, tr, min_wm, screen=scr) if scr else facts
    except _TROUBLE as e:
        t.act(r, "skip", tr.get("index"), f"couldn't tell whether Claude is waiting: {e}")
        return
    ask = _ask_line(facts) if facts.get("kind") in ("question", "permission", "problem") else None
    if t.dry_run:
        if ask:
            t.act(r, "ask", tr.get("index"), f"would say: {ask}")
        return
    ent["asked_tool"] = pend["id"]
    t.persist()
    if not ask:
        return
    stages.log(r["task_dir"], "claude_waiting", run=r["run"], turn=tr.get("index"), turn_kind=facts.get("kind"),
               what=ask)
    t.act(r, "ask", tr.get("index"), ask)
    _notify(f"Johto: {_label(r)} needs you", ask, attention=True)


def _advance(t: _Tick, r: dict, ent: dict, i: int, rec: dict) -> None:
    """Do the turn's remaining steps: build the pack, then send it (with auto_review on). Each step is saved
    as soon as it's done, so a retry or a restart picks up after it."""
    d, run = r["task_dir"], r["run"]
    if not rec.get("pack"):
        try:
            # A run with no GPT Pro chat yet starts one with a full pack, as `johto review send` does (a
            # follow-up after packs made by hand would reach a new chat without the full context).
            chat_for = getattr(_reviews(), "chat_for", None)
            full = bool(t.sets.get("auto_review") and _may_send() and chat_for and chat_for(d, run) is None)
            path = _delta().next_pack(d, run, log=t.log, **({"full": True} if full else {}))
        except _TROUBLE as e:
            return _fail(t, r, i, rec, "building the pack", e)
        rec["pack"] = str(path)
        if not _pack_logged(d, path):             # delta.record_pack logs the packs it builds
            stages.log(d, "pack_built", run=run, turn=i, path=str(path), source="watch")
        t.act(r, "pack", i, f"pack for turn {i}: {path}")
        t.persist()
    if not t.sets.get("auto_review") or not _may_send():
        rec["status"] = "packed"
        t.persist()
        _notify(f"Johto: turn {i} done", f"{_label(r)} · {_mins(rec.get('working_minutes'))} of work · "
                "review pack ready", _file_url(rec["pack"]))
        return
    if _pack_sent(d, run, rec["pack"]):
        # Nothing new since a pack that already went to ChatGPT (he may have sent it himself).
        rec["status"] = "sent"
        t.persist()
        t.act(r, "send", i, f"turn {i}: its pack already went to GPT Pro")
        return
    was_queued = rec.get("status") == "queued"
    rec["status"], rec["pid"] = "sending", os.getpid()
    t.persist()
    try:
        res = _reviews().send_review(d, run=run, pack=Path(rec["pack"]), log=t.log) or {}
    except _TROUBLE as e:
        return _fail(t, r, i, rec, "the GPT Pro send", e)
    rec.pop("pid", None)
    if res.get("sent") is False:
        reason = str(res.get("reason") or "")
        why = res.get("why")
        busy = why == "busy" or (why is None and "isn't back yet" in reason)
        if busy and res.get("pack") and Path(str(res["pack"])).name == Path(rec["pack"]).name:
            # The pack waited on is this one: an interrupted send that did reach ChatGPT.
            rec["status"], rec["note"] = "sent", reason
            rec.pop("next_try", None)
            t.persist()
            t.act(r, "send", i, f"turn {i}: its pack is with GPT Pro already ({reason})")
            return
        if why == "partial":
            rec["status"], rec["note"] = "partial", reason
            rec.pop("next_try", None)
            t.persist()
            t.act(r, "partial", i, f"turn {i}: not sent, {reason}")
            _notify(f"Johto: turn {i} done", f"{_label(r)} · the pack is partial, so it wasn't sent. Check it, then "
                    f"johto review send {r['short']} --force", _file_url(rec["pack"]))
            return
        if busy:
            # One review at a time per run: send this pack once the last reply has landed.
            rec["status"], rec["next_try"], rec["waiting_on"] = "queued", time.time() + QUEUED_EVERY_S, reason
            t.persist()
            if not was_queued:
                t.act(r, "queued", i, f"turn {i} waits: {reason}")
                _notify(f"Johto: turn {i} done", f"{_label(r)} · {_mins(rec.get('working_minutes'))} of work · "
                        "it goes to GPT Pro when the last review is back")
            return
        if why == "nothing_new" or reason.startswith("nothing new"):
            rec["status"], rec["note"] = "sent", reason
            t.persist()
            t.act(r, "send", i, f"turn {i}: {reason}")
            return
        return _fail(t, r, i, rec, "the GPT Pro send", RuntimeError(reason or "ChatGPT didn't take it"))
    rec.pop("waiting_on", None)
    rec["status"], rec["chat_url"], rec["sent_at"] = "sent", res.get("chat_url"), stages.now_iso()
    rec.pop("error", None)
    rec.pop("next_try", None)
    t.persist()
    t.act(r, "send", i, f"sent turn {i} to GPT Pro" + (f": {res['chat_url']}" if res.get("chat_url") else ""))
    tail = ""
    usage, warn = res.get("usage_pct"), t.sets.get("chatgpt_warn_usage_pct")
    if isinstance(usage, (int, float)) and isinstance(warn, (int, float)) and usage < warn:
        tail = f" · ChatGPT has {usage:.0f}% of its weekly use left"
    _notify(f"Johto: turn {i} done", f"{_label(r)} · {_mins(rec.get('working_minutes'))} of work · "
            f"sent to GPT Pro{tail}", res.get("chat_url"))


def _fail(t: _Tick, r: dict, i: int, rec: dict, step: str, e: Exception) -> None:
    rec["attempts"] = int(rec.get("attempts") or 0) + 1
    err = (str(e) or e.__class__.__name__).strip().splitlines()[0][:240]
    rec["error"] = f"{step} failed: {err}"
    rec.pop("pid", None)
    if rec["attempts"] >= MAX_ATTEMPTS:
        rec["status"] = "gave_up"
        rec.pop("next_try", None)
        t.persist()
        t.act(r, "gave_up", i, f"turn {i}: {rec['error']}; gave up after {rec['attempts']} tries")
        _notify("Johto: review not sent", f"{_label(r)} turn {i}: {rec['error']}. "
                f"Try johto review send {r['short']}.")
        return
    wait = BACKOFF_S[min(rec["attempts"], len(BACKOFF_S)) - 1]
    rec["status"], rec["next_try"] = "failed", time.time() + wait
    t.persist()
    t.act(r, "failed", i, f"turn {i}: {rec['error']}; trying again in {_mins(wait / 60)}")
    if rec["attempts"] == 1:
        _notify(f"Johto: turn {i} done", f"{_label(r)} · {step} failed: {err}. Trying again in {_mins(wait / 60)}.")


def _busy(r: dict) -> str | None:
    """The pack whose review the run is still waiting on (send_review sends one at a time per run)."""
    try:
        rows = [p for p in _reviews().pending(r["task_dir"]) or [] if isinstance(p, dict) and p.get("run") == r["run"]]
    except _TROUBLE:
        return None
    return Path(str(rows[-1].get("pack") or "a pack")).name if rows else None


def _pack_logged(d: Path, path) -> bool:
    name = Path(path).name
    return any(Path(str(ev.get("path") or "")).name == name for ev in stages.read(d, ("pack_built",)))


def _pack_sent(d: Path, run: str, path: str) -> bool:
    """The pack's record in runs/<run>/packs.json says it went to ChatGPT already."""
    try:
        rec = _delta().pack_record(d, run, path)
    except _TROUBLE:   # an older delta without pack_record: assume not sent
        return False
    return bool(rec and rec.get("sent_at"))


def _already_sent(r: dict, rec: dict) -> bool:
    """A send cut off by a restart went through: the reviews' pending list has its pack as sent. One still
    "sending" or "unconfirmed" is decided by send_review instead, which looks at the chat first."""
    try:
        pending = _reviews().pending(r["task_dir"]) or []
    except _TROUBLE:
        return False
    name = Path(rec.get("pack") or "").name   # pending.json stores the pack relative to the task folder
    return bool(name) and any(Path(str(p.get("pack") or "")).name == name and (p.get("state") or "sent") == "sent"
                              and not p.get("unconfirmed") for p in pending if isinstance(p, dict))


def _retry_due(t: _Tick, r: dict, ent: dict) -> None:
    rec = ent["turns"].get(str(ent.get("last_turn")))
    if not rec:
        return
    interrupted = rec.get("status") == "sending" and rec.get("pid") != os.getpid()
    due = rec.get("status") in ("failed", "queued") and (rec.get("next_try") or 0) <= time.time()
    if not (due or interrupted):
        return
    i = ent["last_turn"]
    if t.dry_run:
        why = rec.get("waiting_on") or rec.get("error") or "the watcher stopped mid-send"
        t.act(r, "retry", i, f"would try sending turn {i} again ({why})")
        return
    if rec["status"] == "queued":
        return _advance(t, r, ent, i, rec)
    if interrupted and _already_sent(r, rec):
        rec["status"] = "sent"
        rec.pop("pid", None)
        t.persist()
        return
    # An interrupted send whose outcome isn't known goes back through send_review, which reads the chat
    # before sending anything (reviews.settle).
    if interrupted:
        rec["status"] = "failed"
    t.act(r, "retry", i, f"trying turn {i} again (try {int(rec.get('attempts') or 0) + 1} of {MAX_ATTEMPTS})")
    _advance(t, r, ent, i, rec)


def _crossed(st: dict, ent: dict) -> list[int]:
    """Thresholds budget says are due, given the ones already sent (budget.nudge_due one at a time)."""
    sent = list(ent.get("nudges") or [])
    due: list[int] = []
    for _ in range(12):
        n = _budget().nudge_due(st, sent + due)
        if n is None or n in sent or n in due:
            break
        due.append(n)
    return due


def _nudges(t: _Tick, r: dict, ent: dict, st: dict) -> None:
    try:
        due = _crossed(st, ent)
    except _TROUBLE as e:
        if t.dry_run:
            t.act(r, "skip", detail=f"couldn't check the minutes budget: {e}")
        return
    if not due:
        return
    top, wm = max(due), st.get("working_minutes")
    if t.dry_run:
        t.act(r, "nudge", detail=f"would nudge: Claude has worked {_mins(wm)} (past {top})")
        return
    ent["nudges"] = sorted(set(ent.get("nudges") or []) | set(due))
    stages.log(r["task_dir"], "budget_nudge", run=r["run"], threshold=top, working_minutes=wm)
    t.persist()
    t.act(r, "nudge", detail=f"nudged at {top} Claude minutes")
    line = (st.get("line") or "").strip() or f"Claude has worked {_mins(wm)} of the {st.get('budget') or 90}."
    _notify(f"Johto: {top} Claude minutes", f"{_label(r)} · {line}")


def _file_url(p: str | None) -> str | None:
    return Path(p).resolve().as_uri() if p else None


def _review_bits(p: Path) -> tuple[str, str, int | None]:
    """(short, run, turn) from tasks/<short>/reviews/<run>-t<NN>-<source>.md."""
    p = Path(p)
    m = _REVIEW_RE.match(p.name)
    short = p.parent.parent.name if p.parent.name == "reviews" else p.parent.name
    return (short, m["run"], int(m["turn"])) if m else (short, "", None)


def _fetch(t: _Tick) -> None:
    if not t.dry_run and t.now - float(t.state.get("fetched_at") or 0) < FETCH_EVERY_S:
        return
    try:
        rv = _reviews()
    except _TROUBLE:
        return
    waiting = []
    for tj in sorted(config.TASKS.glob("*/task.json")):
        try:
            waiting += [(tj.parent.name, p) for p in rv.pending(tj.parent) or []]
        except _TROUBLE:
            continue
    if not waiting:
        return
    if t.dry_run:
        for short, p in waiting:
            run = p.get("run") if isinstance(p, dict) else None
            turn = p.get("turn") if isinstance(p, dict) else None
            t.actions.append({"key": f"{short}/{run or ''}", "short": short, "run": run or "", "action": "fetch",
                              "turn": turn, "detail": "would check ChatGPT for this reply", "dry_run": True})
        return
    t.state["fetched_at"] = t.now
    t.persist()
    try:
        saved = rv.fetch_pending(None, log=t.log) or []
    except _TROUBLE as e:
        msg = f"checking GPT Pro replies failed: {e}"
        if t.state.get("fetch_error") != msg:
            t.log(msg)
        t.state["fetch_error"] = msg
        return
    t.state.pop("fetch_error", None)
    seen = t.state.setdefault("reviews_notified", [])
    for p in saved:
        if str(p) in seen:
            continue
        seen.append(str(p))
        short, run, turn = _review_bits(Path(p))
        rel = f"reviews/{Path(p).name}"
        what = f"{short} {run}" + (f" turn {turn}" if turn else "")
        t.actions.append({"key": f"{short}/{run}", "short": short, "run": run, "action": "review_saved",
                          "turn": turn, "detail": f"GPT Pro review saved: {p}", "dry_run": False})
        t.log(f"{what}: GPT Pro review saved: {p}")
        _notify("Johto: GPT Pro review ready", f"{what} · {rel}", _file_url(str(p)))
    del seen[:-200]
    # Only after ChatGPT was read: a reply that finished while the Mac slept is saved above, not dropped here.
    expire = getattr(rv, "expire_pending", None)
    try:
        gone = expire(None, log=t.log) or [] if expire else []
    except _TROUBLE as e:
        gone = []
        t.log(f"checking for stale GPT Pro sends failed: {e}")
    for e in gone:
        what = f"{e.get('task')} {e.get('run')}" + (f" turn {e['turn']}" if e.get("turn") else "")
        t.actions.append({"key": f"{e.get('task')}/{e.get('run')}", "short": e.get("task"), "run": e.get("run"),
                          "action": "review_expired", "turn": e.get("turn"), "detail": "no GPT Pro reply; stopped waiting",
                          "dry_run": False})
        hours = float(getattr(rv, "PENDING_TTL_S", 3 * 3600)) / 3600
        _notify("Johto: no GPT Pro reply", f"{what} · nothing after {hours:g} hours, so johto stopped waiting. "
                "Open the chat to check.", e.get("chat_url"))
    t.persist()


def _report(t: _Tick, r: dict, ent: dict, st: dict, quiet: float, busy: bool) -> None:
    """Dry run: one line per live run (where, turns, the last ended turn)."""
    turns = st.get("turns") or []
    ended = [tr for tr in turns if tr.get("ended") == "end_turn"]
    bits = [f"{len(turns)} turn{'' if len(turns) == 1 else 's'}",
            f"Claude worked {_mins(st.get('working_minutes'))}"]
    if st.get("background_minutes"):
        bits.append(f"{_mins(st['background_minutes'])} on background jobs")
    if ended:
        e = ended[-1]
        when = _epoch(e.get("end"))
        at = time.strftime("%b %d %H:%M", time.localtime(when)) if when else "?"
        bits.append(f"turn {e['index']} ended {at} after {_mins(e.get('working_minutes'))}")
    if st.get("turn_open"):
        bits.append("a turn is open")
    bits.append("Claude is working now" if busy else f"transcript still for {_span_of(quiet)}")
    nothing = not any(a for a in t.actions if a["key"] == r["key"] and a["action"] != "wait")
    t.reported.add(r["key"])
    t.log(f"{_label(r)} on {r['where']}: " + "; ".join(bits) + (". Nothing to do." if nothing else ""))


def tick(dry_run: bool = False, log: Callable[[str], None] = print) -> list[dict]:
    """One look at every live run. Returns the actions taken (or, with dry_run, that would be taken), each
    {key, short, run, action, turn, detail, dry_run}. dry_run writes nothing: no state, events, packs or sends."""
    t = _Tick(state=load_state(), sets=settings.load(), dry_run=dry_run, log=log)
    runs = discover()
    if dry_run:
        n = lambda v: sum(1 for r in runs if r["live"] is v)  # noqa: E731
        log(f"{len(runs)} run{'' if len(runs) == 1 else 's'} not torn down: {n(True)} live, {n(False)} stopped"
            + (f", {n(None)} can't be checked now" if n(None) else ""))
    for r in runs:
        ent = t.state["runs"].get(r["key"])
        if r["live"] is None:
            # Can't tell this tick (the host is off, or its Docker is stopped). Say so once, not every tick.
            msg = f"skipped this tick: {r['why']}"
            if dry_run:
                t.act(r, "skip", detail=msg)
                log(f"{_label(r)} on {r['where']}: {msg}")
            elif ent is not None and ent.get("host_error") != msg:
                ent["host_error"] = msg
                t.act(r, "skip", detail=msg)
            continue
        if ent is not None:
            ent.pop("host_error", None)
        if not r["live"]:
            continue
        before = len(t.actions)
        try:
            _check_run(t, r)
        except _TROUBLE as e:   # one run's trouble never stops the others
            traceback.print_exc()
            t.act(r, "error", detail=f"{e.__class__.__name__}: {e}")
        if dry_run:
            if r["key"] not in t.reported:
                log(f"{_label(r)} on {r['where']}:")
            for a in t.actions[before:]:
                log(f"  {a['detail']}")
    if dry_run:
        idle = [_label(r) for r in runs if r["live"] is False]
        if idle:
            log("Not running: " + ", ".join(idle))
    before = len(t.actions)
    _fetch(t)
    if dry_run:
        for a in t.actions[before:]:
            log(f"{a['short']} {a['run']}" + (f" turn {a['turn']}" if a["turn"] else "") + f": {a['detail']}")
    t.persist()
    return t.actions


def _stamped(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def run(interval: float = 20, dry_run: bool = False) -> None:
    """Tick until Ctrl+C. Holds the single-watcher lock (a dry run takes none and writes nothing)."""
    fd = None if dry_run else acquire()
    if fd is None and not dry_run:
        raise SystemExit(f"johto watch is already running (pid {holder() or '?'}).")
    log = print if dry_run else _stamped
    log(f"Watching lanes every {interval:g} s{' (dry run)' if dry_run else ''}. Ctrl+C stops.")
    try:
        while True:
            try:
                tick(dry_run=dry_run, log=log)
            except _TROUBLE:
                traceback.print_exc()
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        release(fd)


_STOP = threading.Event()
_THREAD: threading.Thread | None = None


def start(interval: float = 20) -> bool:
    """Run the watcher in this process (the web app starts it, so reviews go out whenever Johto runs). False when
    another watcher (a `johto watch` in a terminal) holds the lock; that one keeps watching."""
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return True
    fd = acquire()
    if fd is None:
        _stamped(f"turn watcher: johto watch is already running (pid {holder() or '?'}); not starting another")
        return False
    _STOP.clear()

    def loop():
        _stamped(f"turn watcher: watching lanes every {interval:g} s")
        try:
            while not _STOP.is_set():
                try:
                    tick(log=_stamped)
                except _TROUBLE:
                    traceback.print_exc()
                _STOP.wait(interval)
        finally:
            release(fd)
    _THREAD = threading.Thread(target=loop, name="turnwatch", daemon=True)
    _THREAD.start()
    return True


def stop() -> None:
    _STOP.set()


def cmd_watch(a) -> None:
    if not a.once:
        return run(interval=a.interval, dry_run=a.dry_run)
    if a.dry_run:
        tick(dry_run=True)
        return
    fd = acquire()
    if fd is None:
        raise SystemExit(f"johto watch is already running (pid {holder() or '?'}).")
    try:
        acts = tick()
    finally:
        release(fd)
    if not [x for x in acts if x["action"] != "wait"]:
        print("Nothing new.")


def register(sub) -> None:
    s = sub.add_parser("watch", help="watch lanes: pack and send each finished turn to GPT Pro, nudge on minutes")
    s.add_argument("--once", action="store_true", help="one look, then exit")
    s.add_argument("--dry-run", action="store_true", help="print what it sees and would do; write nothing")
    s.add_argument("--interval", type=float, default=20, help="seconds between looks (default 20)")
    s.set_defaults(func=cmd_watch)
