"""What the lane is doing, read from the container, its tmux pane and the live transcript.

Everything here only reads: `docker ps`, one `docker exec` per probe that lists processes and captures the
tmux pane, and the transcript files the lane writes to its config dir on the host. Nothing writes to the lane.

`classify()` is a pure function over one probe, so the rules can be tested against recorded panes
(tests/fixtures/panes/) and transcripts.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

from ... import config, lanes, transcript

# Lane containers are named prework-<task>-<run id>-model_a. Tests use their own prefix so they never see
# (or stop) a real lane.
PREFIX = os.environ.get("JOHTO_LANE_PREFIX", "prework-")
SUFFIX = "-model_a"

# --- screen rules (tuned on Claude Code 2.1.311 captures in tests/fixtures/panes) ------------------------

LOGIN_RE = re.compile(r"Not logged in|Run /login|Please run /login|Select login method|Paste code here|"
                      r"Choose the text style|Do you trust the files|Invalid API key|Login interrupted|OAuth error",
                      re.I)
PERMISSION_RE = re.compile(r"^\s*[│|]?\s*❯\s*1\.\s*Yes\b|No, and tell Claude", re.M)
WORKING_RE = re.compile(r"esc to interrupt", re.I)
RATE_RE = re.compile(r"usage limit|limit (?:will )?reset|hit your limit|limit reached|rate[ _]?limit|overloaded", re.I)
# Claude Code's own limit lines (shown under ⎿, or as "API Error: ..."), not Claude's prose about rate limits.
LIMIT_LINE_RE = re.compile(r"(?:\d+-hour|weekly|usage|session|opus) limit reached|limit reached\s*[·∙]|"
                           r"hit your (?:usage )?limit|usage limit|limit will reset|"
                           r"API Error.*(?:429|529|rate.?limit|overloaded)", re.I)
RESETS_RE = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", re.I)
RULE_RE = re.compile(r"^\s*[─━]{12,}\s*$")
# The input box: `❯` at column 0 (followed by a no-break space) between two rules. Menu rows and dialog
# options are indented, so they never match.
INPUT_RE = re.compile(r"^[❯>](?:\s(.*))?$")
LOGIN_SCREEN_RE = re.compile(r"Select login method|Paste code here|Choose the text style|Do you trust the files", re.I)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
# A Claude Code process: argv[0] is `claude` (the native binary). Never matches tmux's
# `... new-session ... claude-dev`, the `ps` that lists it, or a zombie shown as `[claude]`.
CLAUDE_PROC_RE = re.compile(r"^(?:\S*/)?claude(?:\s|$)")
WAIT_HOLD_S = 3.0
LAUNCH_GRACE_S = 60.0


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


# --- docker -------------------------------------------------------------------------------------------

def _docker(args: list[str], timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def docker_free() -> bool:
    """Fake mode on the real lane prefix: no Docker at all (johto/fakes/lanes.py stands in for lanes)."""
    return bool(getattr(config, "FAKE", False)) and PREFIX == "prework-"


def running_containers() -> list[str]:
    """Running lane containers (any task), by name."""
    if docker_free():
        return []
    try:
        r = _docker(["ps", "--filter", f"name=^{PREFIX}", "--format", "{{.Names}}"], timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    return sorted(n for n in r.stdout.split() if n.startswith(PREFIX))


def claude_in(ps_lines: list[str]) -> bool:
    """True when `ps -eo stat=,args=` output shows a live Claude Code process."""
    for line in ps_lines:
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        stat, args = parts
        if stat.startswith("Z"):
            continue
        if CLAUDE_PROC_RE.match(args):
            return True
    return False


def claude_alive(container: str, unknown: bool = False) -> bool:
    """Is a Claude process alive in the container? `unknown` is the answer when `docker exec` fails or times
    out: False for display, True wherever a wrong "no" would stop, tear down or collect a live session."""
    try:
        r = _docker(["exec", container, "ps", "-eo", "stat=,args="], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return unknown
    if r.returncode != 0:
        if not unknown:
            return False
        try:   # a stopped container has no Claude; anything else is unknown
            st = _docker(["inspect", "-f", "{{.State.Running}}", container], timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return True
        return not (st.returncode == 0 and st.stdout.strip() == "false")
    return claude_in(r.stdout.splitlines())


def look(container: str) -> dict:
    """One `docker exec`: is Claude alive, is the tmux session there, and the visible pane."""
    mark = "@@" + secrets.token_hex(6) + "@@"
    script = (f"ps -eo stat=,args=; echo {mark}; "
              f"if tmux has-session -t lane 2>/dev/null; then echo yes; tmux capture-pane -p -J -t lane; "
              f"else echo no; fi")
    try:
        r = _docker(["exec", "-u", "lane", "-e", "HOME=/home/lane", container, "sh", "-c", script], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "alive": False, "session": False, "pane": None}
    if r.returncode != 0 or mark not in r.stdout:
        return {"ok": False, "alive": False, "session": False, "pane": None}
    ps, _, rest = r.stdout.partition(mark + "\n")
    session, _, pane = rest.partition("\n")
    return {"ok": True, "alive": claude_in(ps.splitlines()), "session": session.strip() == "yes",
            "pane": pane if session.strip() == "yes" else None}


def session_alive(container: str) -> bool:
    try:
        r = _docker(["exec", "-u", "lane", "-e", "HOME=/home/lane", container, "tmux", "has-session", "-t", "lane"],
                    timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def stop(container: str) -> bool:
    """`docker stop` (files and volumes kept). Only lane containers, never in Docker-free fake mode, never
    Austin's prework-* lanes from a server on a scratch copy, and never while Claude is (or may be) alive:
    the check runs again right here, since callers decide which lanes are idle well before they stop them."""
    if docker_free() or not container.startswith(PREFIX):
        return False
    if container.startswith("prework-") and (config.FAKE or (config.ROOT != config.CHECKOUT
                                                             and not getattr(config, "REAL_OK", False))):
        return False
    if claude_alive(container, unknown=True):
        return False
    try:
        return _docker(["stop", container], timeout=90).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# --- paths --------------------------------------------------------------------------------------------

def prework_home() -> Path:
    home = getattr(config, "PREWORK_HOME", None) or os.environ.get("JOHTO_PREWORK_HOME")
    return Path(home).expanduser() if home else lanes.HOME


def lane_key(container: str) -> str | None:
    if not container or not container.endswith(SUFFIX):
        return None
    for p in (PREFIX, "prework-"):
        if container.startswith(p):
            return container[len(p):-len(SUFFIX)]
    return None


def lane_paths(run_dir: Path) -> dict:
    m = lanes.manifest(run_dir)
    lane = m.get("lane") or {}
    container = lane.get("container")
    key = lane_key(container or "")
    cc = prework_home() / "cc-config" / f"{key}_model_a" if key else None
    return {"container": container, "workspace": lane.get("workspace"), "key": key, "cc_config": cc,
            "projects": cc / "projects" if cc else None}


def transcripts(projects: Path | None) -> list[Path]:
    if not projects or not projects.is_dir():
        return []
    return sorted(projects.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime)


# --- run index: which task folder and run each container belongs to ------------------------------------

def run_index() -> dict[str, dict]:
    """container -> {short, run, kind, dir, task_dir, rec, created_at}, from every task folder."""
    out: dict[str, dict] = {}
    for tj in sorted(config.TASKS.glob("*/task.json")):
        d = tj.parent
        try:
            meta = json.loads(tj.read_text())
        except (OSError, ValueError):
            continue
        recs = {r.get("dir"): r for r in meta.get("runs", []) if r.get("dir")}
        dirs = {f"runs/{p.parent.name}" for p in (d / "runs").glob("*/prework-manifest.json")} | set(recs)
        for rel in dirs:
            run_dir = d / rel
            rec = recs.get(rel) or {}
            container = rec.get("container") or (lanes.manifest(run_dir).get("lane") or {}).get("container")
            if not container:
                continue
            name = run_dir.name
            kind = name.split("-")[0]
            prev = out.get(container)
            if prev and (prev.get("rec") and not rec):
                continue
            out[container] = {"short": d.name, "run": name, "kind": kind, "dir": run_dir, "task_dir": d,
                              "rec": rec, "meta": meta, "created_at": rec.get("created_at")}
    return out


# --- transcript reducer ---------------------------------------------------------------------------------

class Tail:
    """Reads complete JSONL lines appended to one file."""

    def __init__(self, path: Path):
        self.path, self.offset, self._part = Path(path), 0, b""

    def read(self) -> list[dict]:
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except OSError:
            return []
        self.offset += len(chunk)
        data = self._part + chunk
        lines = data.split(b"\n")
        self._part = lines.pop()
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out


_INTERRUPTED = ("[Request interrupted by user", )


def _text_blocks(e: dict) -> list[str]:
    c = transcript._content(e)
    if isinstance(c, str):
        return [c]
    if isinstance(c, list):
        return [x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text"]
    return []


class Reducer:
    """Incremental facts from a live transcript: the main chain's last event, turns, tests, edits."""

    EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

    def __init__(self):
        self.humans = 0
        self.human_texts: list[str] = []
        self.last: dict | None = None           # {kind: human|claude|tool_use|tool_result|interrupted, ts, stop}
        self.last_human_ts: str | None = None
        self.last_claude_text: str | None = None
        self.claude_seen = False
        self.tools: dict[str, dict] = {}
        self.tests: list[dict] = []             # {cmd, ok, at_event}
        self.edits = 0
        self.last_edit_event = -1
        self.events = 0
        self.files: list[str] = []
        self.rate: dict | None = None
        self.models: dict[str, int] = {}
        self.efforts: dict[str, int] = {}

    def feed(self, entries: list[dict]) -> None:
        for e in entries:
            self._one(e)

    def _one(self, e: dict) -> None:
        t = e.get("type")
        if e.get("isSidechain"):
            return
        msg = e.get("message") or {}
        oc = msg.get("output_config")
        if isinstance(oc, dict) and oc.get("effort"):
            self.efforts[oc["effort"]] = self.efforts.get(oc["effort"], 0) + 1
        if t == "user":
            if e.get("isMeta"):
                return
            h = transcript._human_text(e)
            if h is not None:
                self.humans += 1
                self.human_texts.append(h)
                self.last_human_ts = e.get("timestamp")
                self._ev("human", e)
                return
            c = transcript._content(e)
            if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
                for x in c:
                    if isinstance(x, dict) and x.get("type") == "tool_result":
                        tu = self.tools.get(x.get("tool_use_id") or "")
                        if tu and tu.get("test"):
                            self.tests.append({"cmd": tu["command"], "ok": not x.get("is_error"), "at": self.events})
                self._ev("tool_result", e)
                return
            if any(b.strip().startswith(_INTERRUPTED) for b in _text_blocks(e)):
                self._ev("interrupted", e)
            return
        if t == "assistant":
            model = msg.get("model")
            text = "\n".join(b for b in _text_blocks(e) if b.strip()).strip()
            if model == "<synthetic>" or e.get("isApiErrorMessage"):
                if text and RATE_RE.search(text):
                    m = RESETS_RE.search(text)
                    self.rate = {"until": m.group(1) if m else None}
                if text:
                    self.last_claude_text = text
                self._ev("claude", e, stop="end_turn")
                return
            if model:
                self.models[model] = self.models.get(model, 0) + 1
                self.claude_seen = True
            self.rate = None
            content = transcript._content(e) or []
            used = False
            for x in content if isinstance(content, list) else []:
                if not isinstance(x, dict) or x.get("type") != "tool_use":
                    continue
                used = True
                inp = x.get("input") or {}
                cmd = inp.get("command") if isinstance(inp.get("command"), str) else ""
                self.tools[x.get("id") or ""] = {"name": x.get("name"), "command": cmd[:200],
                                                 "test": bool(cmd and transcript.TEST_CMD.search(cmd))}
                if x.get("name") in self.EDIT_TOOLS:
                    self.edits += 1
                    self.last_edit_event = self.events + 1
                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if fp and fp not in self.files:
                        self.files.append(fp)
            if text:
                self.last_claude_text = text
            self._ev("tool_use" if used else "claude", e, stop=msg.get("stop_reason"))

    def _ev(self, kind: str, e: dict, stop: str | None = None) -> None:
        self.events += 1
        self.last = {"kind": kind, "ts": e.get("timestamp"), "stop": stop}

    def turn_ended(self) -> bool:
        """Claude finished its turn (or was interrupted) and nothing has happened since."""
        if not self.last:
            return False
        if self.last["kind"] == "interrupted":
            return True
        return self.last["kind"] == "claude" and self.last.get("stop") in ("end_turn", "stop_sequence", "max_tokens")

    def test_facts(self) -> dict:
        if not self.tests:
            return {"runs": 0, "last_ok": None, "last_cmd": None, "after_last_edit": None}
        last = self.tests[-1]
        return {"runs": len(self.tests), "last_ok": last["ok"], "last_cmd": last["cmd"],
                "after_last_edit": last["at"] > self.last_edit_event if self.edits else True}


def question_of(text: str | None) -> str | None:
    """The last sentence ending in '?' in Claude's last message, else its last line; at most 160 chars."""
    if not text:
        return None
    flat = " ".join(text.split())
    qs = re.findall(r"[^.!?\n]*\?", text)
    q = " ".join(qs[-1].split()) if qs else ""
    if not q:
        lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
        q = lines[-1] if lines else flat
    q = q.strip(" -*>`")
    if len(q) > 160:
        cut = q[:159].rsplit(" ", 1)[0]
        q = cut.rstrip(",;:") + "…"
    return q or None


# --- the screen -------------------------------------------------------------------------------------------

def screen(pane: str | None) -> dict | None:
    """Facts from a pane capture: the input box and its text, working, permission, login, rate limit."""
    if pane is None:
        return None
    lines = [strip_ansi(ln).rstrip() for ln in pane.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    text = "\n".join(lines)
    tail = lines[-15:]
    permission = bool(PERMISSION_RE.search("\n".join(lines[-25:])))
    input_visible, input_text = False, ""
    for i in range(len(lines) - 1, 0, -1):
        m = INPUT_RE.match(lines[i])
        if not m or not RULE_RE.match(lines[i - 1]) or permission:
            continue
        body, closed = [m.group(1) or ""], False
        for ln in lines[i + 1:i + 40]:
            if RULE_RE.match(ln):
                closed = True
                break
            body.append(ln.strip())
        if closed:
            input_visible, input_text = True, "\n".join(body).strip()
            break
    rate = None
    for ln in tail:
        s = ln.strip()
        if (s.startswith("⎿") or "API Error" in s) and LIMIT_LINE_RE.search(s):
            m = RESETS_RE.search(s)
            rate = {"until": m.group(1) if m else None}
    last = next((ln.strip() for ln in reversed(lines) if ln.strip() and not RULE_RE.match(ln)), "")
    return {"input_visible": input_visible, "input_text": input_text,
            "working": bool(WORKING_RE.search("\n".join(lines[-12:]))),
            "permission": permission, "login": bool(LOGIN_RE.search(text)),
            "login_screen": bool(LOGIN_SCREEN_RE.search("\n".join(lines[-25:]))),
            "rate": rate, "last_line": last[:200]}


# --- the classifier ---------------------------------------------------------------------------------------

def classify(*, container_running: bool, claude_alive: bool, scr: dict | None, red: Reducer | None,
             has_credentials: bool, collect_phase: str | None, launched_at: float | None, pty_alive: bool,
             pty_failed: str | None, held_since: float | None, now: float) -> tuple[str, str | None, float | None]:
    """(state, evidence, held_since) for one lane. The first rule that matches wins (build-plan 10.2)."""
    humans = red.humans if red else 0
    evidence = (scr or {}).get("last_line") or None
    if not container_running:
        return ("collected" if collect_phase == "done" else "free"), None, None
    if not claude_alive:
        if collect_phase in ("collecting", "packing"):
            return "collecting", None, None
        if collect_phase == "done":
            return "collected", None, None
        if humans:
            return "exited", "Claude exited", None
        if pty_alive and launched_at and now - launched_at < LAUNCH_GRACE_S:
            return "preparing", evidence or "Starting Claude", None
        if pty_failed:
            return "setup_failed", pty_failed, None
        return "idle", "Claude is not running in this lane", None
    if not humans:
        if scr and (scr["login"] or scr["login_screen"]) or not has_credentials:
            return "needs_login", evidence, None
        if scr and scr["permission"]:
            return "permission", evidence, None
        if scr is None or scr["input_visible"]:
            return "ready_for_prompt", evidence, None
        return "preparing", evidence or "Starting Claude", None
    if scr and scr["permission"]:
        return "permission", evidence, None
    if red and red.turn_ended() and not (scr and scr["working"]):
        held = held_since or now
        if now - held >= WAIT_HOLD_S:
            return "waiting", evidence, held
        return "working", evidence, held
    return "working", evidence, None


def now() -> float:
    return time.time()
