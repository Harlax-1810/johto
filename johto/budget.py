"""Live Claude minutes for one run, the minutes nudges, and what kind of turn Claude just finished.

Austin enters Claude's working minutes in Studio, and a Johto task should take about 60 to 90 of them. This
reads a run's transcript wherever it is: the lane's config dir on this Mac, a fresh copy from the PC for a PC
lane, or the collected copy once the lane is gone. transcript.summarize does the measuring (working,
background, idle, per turn); status() adds one plain sentence for the terminal, the web app and notifications.
status()["live"] means the lane's container is running (when that can be told), not just that its transcript
is still on disk. `johto watch` (turnwatch.py) calls status() and nudge_due() to nudge at the budget_nudges
thresholds.

classify_turn() / turn_facts() say what a turn was, so the watcher reviews meaningful results only (GPT Pro's
UX review, section 5): a question for Austin, a permission prompt, substantive work (code changed, or a long
investigation ending in a report), progress (short, nothing changed), a background job still running, or a
problem (API error, usage limit, login prompt). A permission prompt only shows on the lane's screen, never in
the transcript (a tool call without a result is also what a long test run looks like), so it needs the pane
facts from johto.web.lane.monitor.screen.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, lanes, pack, settings, transcript, workspace
from .web.lane import monitor

BUDGET_MIN = 90          # Johto's upper bound for Claude time on one task; data/settings.json "claude_budget_minutes"
REACH_TTL_S = 60         # remember for this long whether the PC answered, so a watch loop doesn't wait on it each tick
RUNNING_TTL_S = 5        # remember this Mac's running containers this long (status() runs for every task)
_reach: dict[str, tuple[float, bool]] = {}
_running: dict[str, tuple[float, set[str] | None]] = {}


# --- finding the run and its transcript -----------------------------------------------------------------------

def _record(task_dir: Path, run: str) -> dict:
    try:
        runs = workspace.load(task_dir).get("runs", [])
    except (OSError, ValueError):
        return {}
    return next((r for r in runs if r.get("dir") == f"runs/{run}"), {})


def latest_run(task_dir: Path) -> str | None:
    """The run started last: the last one in task.json whose folder exists, else the newest run folder."""
    task_dir = Path(task_dir)
    try:
        runs = workspace.load(task_dir).get("runs", [])
    except (OSError, ValueError):
        runs = []
    for r in reversed(runs):
        name = (r.get("dir") or "").removeprefix("runs/")
        if name and (task_dir / "runs" / name).is_dir():
            return name
    dirs = sorted((p for p in (task_dir / "runs").glob("*-*") if p.is_dir()), key=lambda p: p.stat().st_mtime)
    return dirs[-1].name if dirs else None


def _reachable(host: str) -> bool:
    """Does `host` answer ssh within a few seconds? Read-only (`true`), remembered for REACH_TTL_S."""
    at, ok = _reach.get(host, (0.0, False))
    if time.time() - at < REACH_TTL_S:
        return ok
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", host, "true"],
                           capture_output=True, text=True, timeout=12, stdin=subprocess.DEVNULL)
        ok = r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    _reach[host] = (time.time(), ok)
    return ok


def _running_names(host: str | None) -> set[str] | None:
    """Names of the containers running on this Mac (host None) or on `host`, or None when that can't be told
    now (Docker doesn't answer here, the host doesn't answer, fake mode). Read-only `docker ps`, cached."""
    key = host or ""
    at, names = _running.get(key, (0.0, None))
    if time.time() - at < (REACH_TTL_S if host else RUNNING_TTL_S):
        return names
    names = None
    if not monitor.docker_free():
        cmd = ["docker", "ps", "--format", "{{.Names}}"]
        if host:
            cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", host,
                   "docker ps --format '{{.Names}}'; echo \"@@rc $?\""]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=12 if host else 15,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            r = None
        if r is not None and r.returncode == 0:
            out = r.stdout
            if host:
                _reach[host] = (time.time(), True)
                out, _, rc = out.rpartition("@@rc ")
                # The host answered but its Docker is stopped (the PC while a game runs): nothing is running.
                out = out if rc.strip() == "0" else ""
            names = set(out.split())
        elif host and r is not None and r.returncode == 255:
            _reach[host] = (time.time(), False)
    _running[key] = (time.time(), names)
    return names


def lane_state(task_dir: Path, run: str) -> str | None:
    """"running" or "stopped" for the run's lane container, "gone" once torn down, None when there is no lane or
    it can't be told now."""
    rec = _record(task_dir, run)
    if rec.get("torn_down_at"):
        return "gone"
    container = rec.get("container") or (lanes.manifest(Path(task_dir) / "runs" / run).get("lane") or {}).get("container")
    if not container:
        return None
    remote = lanes.remote_of(rec)
    if remote and config.FAKE:
        return None
    names = _running_names(remote[0] if remote else None)
    if names is None:
        return None
    return "running" if container in names else "stopped"


def live_transcript(task_dir: Path, run: str) -> Path | None:
    """The lane's own transcript for a run that hasn't been torn down: on this Mac, or copied from the PC
    into runs/<run>/.live-transcripts (pack._live_transcript, read-only on the PC). None when there is no
    lane, the PC doesn't answer, or in fake mode for a PC lane."""
    run_dir = Path(task_dir) / "runs" / run
    rec = _record(task_dir, run)
    if not run_dir.is_dir() or rec.get("torn_down_at"):
        return None
    remote = lanes.remote_of(rec)
    if remote and (config.FAKE or not _reachable(remote[0])):
        return None
    try:
        return pack._live_transcript(run_dir)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def unreached(task_dir: Path, run: str) -> str | None:
    """The host of a PC run whose lane is still there but which doesn't answer ssh now, else None."""
    rec = _record(task_dir, run)
    remote = lanes.remote_of(rec)
    if not remote or config.FAKE or rec.get("torn_down_at"):
        return None
    return None if _reachable(remote[0]) else remote[0]


# --- status ------------------------------------------------------------------------------------------------

def budget_minutes() -> int:
    try:
        return int(settings.get("claude_budget_minutes") or BUDGET_MIN)
    except (TypeError, ValueError):
        return BUDGET_MIN


def _mins(m: float) -> str:
    return "under 1 min" if 0 < m < 0.5 else f"{round(m)} min"


def _ago(iso: str | None, now: datetime) -> str:
    t = transcript._ts(iso)
    if not t:
        return ""
    m = (now - t).total_seconds() / 60
    if m < 1:
        return "just now"
    if m < 90:
        return f"{round(m)} min ago"
    return f"{m / 60:.1f} h ago"


def line(st: dict, now: datetime | None = None) -> str:
    """One plain sentence about the run, e.g. "validate-1: Claude worked 38 min (12 more waiting on background
    jobs). Turn 3 ended 4 min ago. 38 of 90 min used." """
    now = now or datetime.now(timezone.utc)
    run = st.get("run") or "This task"
    away = st.get("unreached")
    if not st.get("transcript"):
        return f"{run}: its transcript is on {away}, which didn't answer." if away else f"{run}: no transcript yet."
    last = st.get("last_turn")
    if not last:
        return f"{run}: no prompt sent yet."
    w, bg = st["working_minutes"], st["background_minutes"]
    out = f"{run}: Claude worked {_mins(w)}"
    out += f" ({_mins(bg)} more waiting on background jobs)." if bg >= 0.5 else "."
    n = last["index"]
    if last["ended"] == "open" and st.get("live") and last.get("no_reply"):
        out += f" Turn {n} has had no reply from Claude for {_mins(last['no_reply'])}; the lane or the API may be stuck."
    elif last["ended"] == "open" and st.get("live"):
        out += f" Turn {n} is running."
    elif st.get("live"):
        verb = "was interrupted" if last["ended"] == "interrupted" else "ended"
        out += f" Turn {n} {verb} {_ago(last['end'], now)}."
        if st.get("background_running"):
            k = st["background_running"]
            out += f" {k} background job{'s' if k != 1 else ''} still running."
    elif st.get("lane") == "stopped" and not st.get("collected"):
        out += f" {n} turn{'s' if n != 1 else ''}; the lane is stopped."
    else:
        out += f" {n} turn{'s' if n != 1 else ''}, collected."
    out += f" {round(w)} of {st['budget']} min used."
    if away:
        out += f" {away} didn't answer, so this is the collected copy."
    return out


def status(task_dir: Path, run: str | None = None, now: datetime | None = None) -> dict:
    """Where a run's Claude minutes stand. `now` is for tests; a live run counts up to it.

    "live" is True while the lane's container runs and its transcript can be read; a stopped lane whose
    transcript is still on disk is read too, but isn't live. "lane" is lane_state(); "collected" says the run
    has a collected transcript; "unreached" is the PC's name when the run's lane is there but the PC didn't
    answer (the line then says so instead of reading as no transcript)."""
    task_dir = Path(task_dir)
    run = run or latest_run(task_dir)
    st = {"run": run, "live": False, "lane": None, "collected": False, "transcript": None, "working_minutes": 0.0,
          "background_minutes": 0.0, "idle_minutes": 0.0, "turns": [], "last_turn": None, "turn_open": False,
          "background_running": 0, "budget": budget_minutes(), "unreached": None}
    if run:
        st["lane"] = lane_state(task_dir, run)
        path = live_transcript(task_dir, run)
        st["unreached"] = unreached(task_dir, run) if path is None else None
        st["live"] = path is not None and st["lane"] not in ("stopped", "gone")
        logs = transcript.find(task_dir / "runs" / run)
        st["collected"] = bool(logs)
        if path is None:
            path = logs[-1] if logs else None
        if path is not None:
            now_live = (now or datetime.now(timezone.utc)) if st["live"] else None
            s = transcript.summarize(path, now=now_live)
            turns = s["turns"]
            st.update(transcript=str(path), working_minutes=s["working_minutes"],
                      background_minutes=s["background_minutes"], idle_minutes=s["idle_minutes"], turns=turns,
                      last_turn=turns[-1] if turns else None, turn_open=bool(turns) and turns[-1]["ended"] == "open",
                      background_running=s["background_running"])
    st["line"] = line(st, now) if run else "No runs yet."
    return st


def nudge_due(st: dict, sent: list[int]) -> int | None:
    """The highest budget_nudges threshold that working minutes have reached, unless it (or a higher one) was
    already sent. Crossing two at once gives one nudge, for the higher."""
    try:
        marks = sorted(int(x) for x in settings.get("budget_nudges") or [])
    except (TypeError, ValueError):
        return None
    reached = [m for m in marks if (st.get("working_minutes") or 0) >= m]
    if not reached:
        return None
    top = reached[-1]
    return None if any(int(s) >= top for s in sent or []) else top


# --- what kind of turn it was --------------------------------------------------------------------------------

KINDS = ("question", "permission", "substantive", "progress", "background", "problem")
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
ASK_TOOL = "AskUserQuestion"
REPORT_CHARS = 1500        # a last message this long, after a real investigation, is a report worth reviewing
INVESTIGATION_TOOLS = 8    # "a real investigation": this many tool calls, or review_min_working_minutes of work
# Shell commands that change files in place; plain `>` redirects are too common (2>/dev/null, /tmp scratch) to count.
WRITE_CMD = re.compile(r"\bsed\s+-[a-zA-Z]*i|\bperl\s+-[a-zA-Z]*i|\bgit\s+(?:apply|am|restore|checkout\s+--|stash\s+pop)\b"
                       r"|\bpatch\s+-p")
SCRATCH = ("/tmp/", "/private/tmp/", "/var/tmp/", "/dev/")
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_ASKS = re.compile(r"\?(?=[\s*_`)\]\"'”’]*(?:$|\s))", re.M)


def _entries(path) -> list[dict]:
    out = []
    try:
        lines = Path(path).read_text(errors="ignore").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _synthetic(e: dict) -> bool:
    return (e.get("message") or {}).get("model") == "<synthetic>" or bool(e.get("isApiErrorMessage"))


def _texts(e: dict) -> list[str]:
    c = transcript._content(e)
    if isinstance(c, str):
        return [c] if c.strip() else []
    return [x.get("text", "") for x in c or [] if isinstance(x, dict) and x.get("type") == "text" and x.get("text", "").strip()]


def _uses(e: dict) -> list[dict]:
    c = transcript._content(e)
    return [x for x in c if isinstance(x, dict) and x.get("type") == "tool_use"] if isinstance(c, list) else []


def _result_ids(e: dict) -> set[str]:
    c = transcript._content(e)
    return {x.get("tool_use_id") for x in c if isinstance(x, dict) and x.get("type") == "tool_result"} \
        if isinstance(c, list) else set()


def _tool_detail(use: dict) -> str:
    inp = use.get("input") or {}
    if use.get("name") == ASK_TOOL:
        qs = inp.get("questions") or []
        q = (qs[0] or {}).get("question") if qs and isinstance(qs[0], dict) else inp.get("question")
        return " ".join(str(q or "").split())[:200]
    for k in ("command", "file_path", "notebook_path", "url", "pattern", "description"):
        if isinstance(inp.get(k), str) and inp[k].strip():
            return " ".join(inp[k].split())[:160]
    return ""


def asked(text: str | None) -> str | None:
    """The question in the last paragraph of Claude's message (code blocks aside), or None."""
    if not text:
        return None
    paras = [p for p in re.split(r"\n\s*\n", _FENCE.sub("", text).strip()) if p.strip()]
    if not paras or not _ASKS.search(paras[-1]):
        return None
    return monitor.question_of(paras[-1])


def _scratch(path: str) -> bool:
    return not path or path.startswith(SCRATCH) or "/scratchpad/" in path or "/.claude/" in path


def turn_facts(status_or_transcript, turn=None, *, screen: dict | None = None, diff_changed: bool | None = None,
               min_working_minutes: float | None = None) -> dict:
    """What one turn of a run was. `status_or_transcript` is a status() dict (which knows about background jobs
    still running) or a transcript path; `turn` is its index (or a turn dict), default the last. `screen` is
    johto.web.lane.monitor.screen() of the lane's pane, when the caller read it; `diff_changed` is whether
    Claude's diff changed during the turn, when the caller knows (otherwise edit tool calls stand in for it).

    Returns {turn, kind, why, ended, working_minutes, tool_calls, edits, edited_files, report_chars, asks,
    pending_tool, permission, problem, background_running}. kind is one of KINDS, first match wins:
    problem (the turn's last reply is an API error, a usage limit or a login prompt), permission (the screen
    shows a permission dialog), question (Claude is waiting on AskUserQuestion), background (it ended with a
    background job still running), substantive (code changed, or a report of REPORT_CHARS+ after
    INVESTIGATION_TOOLS+ tool calls or min_working_minutes of work), question (its last paragraph asks
    something), progress (anything else, including a turn still working). `asks` is set whenever Claude asked
    something, even on a substantive turn."""
    st = status_or_transcript if isinstance(status_or_transcript, dict) else None
    path = (st or {}).get("transcript") if st is not None else status_or_transcript
    entries = _entries(path) if path else []
    turns = (st or {}).get("turns") if st is not None else None
    if turns is None:
        turns = transcript.minutes(entries)["turns"] if entries else []
    index = turn.get("index") if isinstance(turn, dict) else turn
    if index is None:
        index = turns[-1]["index"] if turns else None
    tr = next((t for t in turns if t.get("index") == index), {})
    min_wm = float(min_working_minutes if min_working_minutes is not None
                   else (settings.get("review_min_working_minutes") or 3))
    out = {"turn": index, "kind": "progress", "why": "", "ended": tr.get("ended"),
           "working_minutes": float(tr.get("working_minutes") or 0), "tool_calls": 0, "edits": 0,
           "edited_files": [], "report_chars": 0, "asks": None, "pending_tool": None, "permission": None,
           "problem": None, "background_running": 0}
    prompts = transcript.counted_prompts(entries)
    if not index or index > len(prompts):
        out["why"] = "no such turn"
        return out
    start = prompts[index - 1][0]
    end = prompts[index][0] if index < len(prompts) else None
    main = [e for e in entries if not e.get("isSidechain") and transcript._ts(e.get("timestamp"))
            and transcript._ts(e["timestamp"]) >= start and (end is None or transcript._ts(e["timestamp"]) < end)]
    answered: set[str] = set()
    for e in main:
        if e.get("type") == "user":
            answered |= _result_ids(e)
    replies = [e for e in main if e.get("type") == "assistant"]
    uses = list({u.get("id") or id(u): u for e in replies for u in _uses(e)}.values())   # one per tool call
    out["tool_calls"] = len(uses)
    files = []
    for u in uses:
        inp = u.get("input") or {}
        fp = inp.get("file_path") or inp.get("notebook_path") or ""
        if u.get("name") in EDIT_TOOLS and not _scratch(str(fp)):
            files.append(str(fp))
        elif u.get("name") == "Bash" and WRITE_CMD.search(cmd := str(inp.get("command") or "")) \
                and not any(s in cmd for s in ("/tmp/", "scratchpad")):
            files.append("(shell edit)")
    out["edits"], out["edited_files"] = len(files), list(dict.fromkeys(f for f in files if f != "(shell edit)"))
    texts = [t for e in replies if not _synthetic(e) for t in _texts(e)]
    last_text = texts[-1].strip() if texts else ""
    out["report_chars"] = len(last_text)
    last_reply = replies[-1] if replies else None
    last_main = next((e for e in reversed(main) if e.get("type") in ("user", "assistant")), None)
    if last_main is not None and last_main.get("type") == "assistant":
        open_uses = [u for u in _uses(last_main) if u.get("id") not in answered]
        if open_uses:
            u = open_uses[-1]
            out["pending_tool"] = {"id": u.get("id"), "name": u.get("name"), "detail": _tool_detail(u)}
    if st is not None and turns and turns[-1].get("index") == index:
        out["background_running"] = int(st.get("background_running") or 0)

    # Problem: the last reply is Claude Code's own error line, or the screen shows a login or limit.
    if last_reply is not None and _synthetic(last_reply):
        msg = " ".join(" ".join(_texts(last_reply)).split())[:200] or "an API error"
        out["problem"] = msg
    elif screen and (screen.get("login_screen") or (screen.get("login") and not screen.get("input_visible"))):
        out["problem"] = "Claude Code is asking to log in"
    elif screen and screen.get("rate"):
        until = (screen.get("rate") or {}).get("until")
        out["problem"] = "usage limit reached" + (f" (resets {until})" if until else "")
    pending = out["pending_tool"]
    if pending and pending["name"] == ASK_TOOL:
        out["asks"] = pending["detail"] or "a question with options"
    elif out["ended"] != "open":
        out["asks"] = asked(last_text)
    if screen and screen.get("permission"):
        what = f"{pending['name']} {pending['detail']}".strip() if pending else (screen.get("last_line") or "")
        out["permission"] = what or "a tool call"

    report = out["report_chars"] >= REPORT_CHARS and (out["tool_calls"] >= INVESTIGATION_TOOLS
                                                     or out["working_minutes"] >= min_wm)
    changed = diff_changed if diff_changed is not None else out["edits"] > 0
    if out["problem"]:
        out["kind"], out["why"] = "problem", out["problem"]
    elif out["permission"]:
        out["kind"], out["why"] = "permission", f"waiting for permission: {out['permission']}"
    elif pending and pending["name"] == ASK_TOOL:
        out["kind"], out["why"] = "question", f"asked: {out['asks']}"
    elif out["ended"] == "open":
        out["kind"], out["why"] = "progress", "still working"
    elif out["background_running"]:
        n = out["background_running"]
        out["kind"], out["why"] = "background", f"{n} background job{'s' if n != 1 else ''} still running"
    elif changed:
        out["kind"] = "substantive"
        out["why"] = ("the diff changed" if diff_changed else
                      f"{out['edits']} edit{'s' if out['edits'] != 1 else ''}")
    elif report:
        out["kind"] = "substantive"
        out["why"] = f"a {out['report_chars']:,}-character report after {out['tool_calls']} tool calls"
    elif out["asks"]:
        out["kind"], out["why"] = "question", f"asked: {out['asks']}"
    else:
        out["why"] = "short, nothing changed"
    return out


def classify_turn(status_or_transcript, turn=None, *, screen: dict | None = None,
                  diff_changed: bool | None = None) -> str:
    """One of KINDS for a turn (see turn_facts)."""
    return turn_facts(status_or_transcript, turn, screen=screen, diff_changed=diff_changed)["kind"]


# --- johto time --------------------------------------------------------------------------------------------

ENDED = {"end_turn": "finished", "interrupted": "interrupted", "open": "running"}


def _clock(iso: str) -> str:
    t = transcript._ts(iso)
    return t.astimezone().strftime("%a %H:%M") if t else "?"


def table(st: dict) -> str:
    rows = [("Turn", "Started", "Working", "Background", "Ended", "Prompt")]
    for t in st["turns"]:
        prompt = " ".join(t["prompt"].split())
        rows.append((str(t["index"]), _clock(t["start"]), f"{t['working_minutes']:.1f}",
                     f"{t['background_minutes']:.1f}", ENDED.get(t["ended"], t["ended"]),
                     prompt[:48] + ("…" if len(prompt) > 48 else "")))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    out = []
    for r in rows:
        cells = [c.rjust(w) if i in (0, 2, 3) else c.ljust(w) for i, (c, w) in enumerate(zip(r, widths))]
        out.append("  ".join(cells + [r[-1]]).rstrip())
    return "\n".join(out)


def cmd_time(a) -> None:
    try:
        d = workspace.resolve(a.task)
    except FileNotFoundError as e:
        raise SystemExit(str(e))
    if a.run and not (d / "runs" / a.run).is_dir():
        raise SystemExit(f"No run {a.run} in {d}")
    st = status(d, a.run)
    print(st["line"])
    if st["turns"]:
        print()
        print(table(st))


def register(sub) -> None:
    s = sub.add_parser("time", help="Claude's working minutes for a run, turn by turn")
    s.add_argument("task")
    s.add_argument("run", nargs="?", help="e.g. validate-1 (default: the latest run)")
    s.set_defaults(func=cmd_time)
