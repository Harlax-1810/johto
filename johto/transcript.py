"""Facts from a Claude Code session transcript (.jsonl): turns, minutes, model, effort, tools.

`summarize()` feeds collect (summary.md, the run's `active_minutes` in task.json), the Studio Claude-minutes
field, packs, lint and the web app; `budget.py` runs it on live lanes. Its minutes are Claude's working time,
measured turn by turn (see "Claude minutes" below), so a compaction or a background job hours later doesn't
stretch a turn.
"""
from __future__ import annotations

import ast
import bisect
import collections
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

TEST_CMD = re.compile(
    r"\b(pytest|go test|cargo test|npm (run )?test|pnpm (run )?test|yarn test|bun test|vitest|jest|"
    r"rspec|bundle exec|phpunit|mvn test|gradle\w* test|dotnet test|make test|tox|unittest|ctest|"
    r"manage\.py test)\b")


COMPACT_PREFIX = "This session is being continued from a previous conversation"


def _ts(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _content(e: dict):
    c = (e.get("message") or {}).get("content")
    if isinstance(c, str) and c.startswith("[{") and "'type'" in c:
        try:
            return ast.literal_eval(c)
        except (ValueError, SyntaxError):
            return c
    return c


def _human_text(e: dict) -> str | None:
    """Text of a prompt Austin typed, or None for tool results, meta and system entries."""
    if e.get("type") != "user" or e.get("isMeta") or e.get("isSidechain"):
        return None
    c = _content(e)
    if isinstance(c, str):
        text = c
    elif isinstance(c, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return None
        text = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    else:
        return None
    t = text.strip()
    # A compaction writes the summary of the conversation so far as a user entry; it is not Austin either.
    if e.get("isCompactSummary") or t.startswith(COMPACT_PREFIX):
        return None
    # Claude Code injects background-task notices and reminders as user-role entries; they are not Austin.
    if not t or t.startswith(("<command-", "<local-command", "Caveat:", "[Request interrupted", "<task-notification",
                              "<system-reminder", "<bash-notification", "<agent-message", "<cross-session")):
        return None
    return t


def human_messages(entries: list[dict]) -> list[dict]:
    """Every message Austin typed, with whether the model replied before his next one.

    Each item: {ts, text, replied, counted, note}. Counting rules (so the run starts when the real
    prompt went in, and follow-ups match Johto's "your messages after the opening prompt"):
    - a message the model never answered (login error, interrupted at once, sent twice) merges
      into the next message;
    - a resend of the same text as the previous counted message replaces it (the run restarts there).
    """
    msgs = []
    for e in entries:
        if e.get("isSidechain"):
            continue
        h = _human_text(e)
        if h is not None and _ts(e.get("timestamp")):
            msgs.append({"ts": _ts(e.get("timestamp")), "text": h, "replied": False, "counted": True, "note": None})
        elif msgs and e.get("type") == "assistant":
            model = (e.get("message") or {}).get("model")
            if model and model != "<synthetic>":
                msgs[-1]["replied"] = True
    counted = []
    for i, m in enumerate(msgs):
        if not m["replied"] and i + 1 < len(msgs):
            m["counted"], m["note"] = False, "no reply before your next message"
            continue
        if counted and counted[-1]["text"].strip() == m["text"].strip():
            counted[-1]["counted"], counted[-1]["note"] = False, "sent again"
            counted.pop()
        counted.append(m)
    return msgs


def counted_prompts(entries: list[dict]) -> list[tuple]:
    return [(m["ts"], m["text"]) for m in human_messages(entries) if m["counted"]]


# --- Claude minutes ----------------------------------------------------------------------------------------
#
# Austin enters Claude's working minutes in Studio. A turn runs from one of his counted prompts to the next.
# Inside it, the time between two main-thread entries is:
# - working while Claude's loop runs: from a prompt, tool result or task notice to the model's reply, and while
#   a foreground tool call runs;
# - background while Claude has stopped but a background shell or subagent it started is still running, and
#   past TOOL_CAP_MIN on one foreground tool call (a long build or test run Claude is only waiting on);
# - idle otherwise: Claude ended its turn and is waiting for Austin.
# A compaction, a task notice nothing answers, or a command Austin types (/exit) never extends a turn.
# Thresholds, checked against every real transcript so far (longest foreground tool call 3.4 min, longest wait
# on the model 2.4 min):
TOOL_CAP_MIN = 10       # a foreground tool call counts as working for its first 10 minutes, the rest as background
MODEL_STALL_MIN = 30    # a wait on the model longer than this is a stalled lane or API: the rest is idle
# Live only: a prompt with no assistant entry after it yet (a hung API request, a login screen, a stalled lane)
# counts as working for this long, then as idle until a reply comes; the turn carries no_reply. Once the reply
# is written the whole wait counts as above. Longest real wait on the model so far: 2.4 min.
NO_REPLY_GRACE_MIN = 3

END_STOPS = ("end_turn", "stop_sequence", "max_tokens")
INTERRUPTED = "[Request interrupted by user"
NOTICE_MARKS = ("<task-notification>", "<agent-message", "<bash-notification", "<cross-session")
KILL_TOOLS = {"KillShell", "KillBash", "TaskStop"}
NOTICE_ID_RE = re.compile(r"<(tool-use-id|task-id)>\s*([^<\s]+)\s*</\1>")


def _all_text(e: dict) -> str:
    c = _content(e)
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return ""


def _result_text(x: dict) -> str:
    c = x.get("content")
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict))
    return str(c or "")


def _events(entries: list[dict]) -> list[tuple]:
    """Main-thread entries that move Claude's loop, in file order: (ts, kind, data). Kinds: prompt (Austin),
    notice (a background job or subagent reporting back), result (tool results), model (an assistant entry),
    interrupt, end (Claude Code's own end-of-turn marker). Everything else is ignored here."""
    out = []
    for e in entries:
        ts = _ts(e.get("timestamp"))
        if not ts or e.get("isSidechain"):
            continue
        t = e.get("type")
        if t == "assistant":
            msg = e.get("message") or {}
            c = _content(e)
            uses = [x.get("id") for x in c if isinstance(x, dict) and x.get("type") == "tool_use"] if isinstance(c, list) else []
            error = msg.get("model") == "<synthetic>" or bool(e.get("isApiErrorMessage"))
            out.append((ts, "model", {"stop": msg.get("stop_reason"), "id": msg.get("id"), "uses": uses, "error": error}))
        elif t == "user":
            if _human_text(e) is not None:
                out.append((ts, "prompt", None))
                continue
            c = _content(e)
            results = [x for x in c if isinstance(x, dict) and x.get("type") == "tool_result"] if isinstance(c, list) else []
            if results:
                out.append((ts, "result", [x.get("tool_use_id") for x in results]))
                if any(_result_text(x).lstrip().startswith(INTERRUPTED) for x in results):
                    out.append((ts, "interrupt", None))
                continue
            text = _all_text(e).lstrip()
            if text.startswith(INTERRUPTED):
                out.append((ts, "interrupt", None))
            elif any(m in text for m in NOTICE_MARKS):
                out.append((ts, "notice", None))
        elif t == "queue-operation" and e.get("operation") == "dequeue":
            out.append((ts, "notice", None))   # a queued notice or message handed to an idle Claude
        elif t == "system" and e.get("subtype") == "turn_duration":
            out.append((ts, "end", None))
    return out


def _jobs(entries: list[dict], end: datetime) -> tuple[list[tuple], int]:
    """Background shells and subagents: ([(start, end)], how many had not reported back by `end`). A job starts
    at the tool call that launched it and ends at its first task notice (the queue time, which is when it
    finished), or when Claude stops it."""
    launched, uses, done = {}, {}, {}
    for e in entries:
        ts = _ts(e.get("timestamp"))
        if not ts or e.get("isSidechain"):
            continue
        t, text = e.get("type"), ""
        if t == "assistant":
            c = _content(e)
            for x in c if isinstance(c, list) else []:
                if isinstance(x, dict) and x.get("type") == "tool_use":
                    uses[x.get("id")] = ts
                    if x.get("name") in KILL_TOOLS:
                        for v in (x.get("input") or {}).values():
                            if isinstance(v, str):
                                done[v] = min(done.get(v, ts), ts)
            continue
        if t == "user":
            r = e.get("toolUseResult")
            c = _content(e)
            if isinstance(r, dict) and isinstance(c, list):
                task = r.get("backgroundTaskId") or (r.get("agentId") if r.get("isAsync") or r.get("status") == "async_launched" else None)
                for x in c:
                    if task and isinstance(x, dict) and x.get("type") == "tool_result":
                        launched[x.get("tool_use_id")] = (uses.get(x.get("tool_use_id"), ts), task)
            text = _all_text(e)
        elif t == "queue-operation":
            text = e.get("content") if isinstance(e.get("content"), str) else ""
        elif t == "attachment":
            text = str((e.get("attachment") or {}).get("prompt") or "")
        if "<task-notification>" in text:
            for _, ident in NOTICE_ID_RE.findall(text):
                done[ident] = min(done.get(ident, ts), ts)
    out, running = [], 0
    for use_id, (start, task) in launched.items():
        ends = [d for d in (done.get(use_id), done.get(task)) if d]
        stop = min(ends) if ends else end
        running += not ends
        out.append((start, max(start, min(stop, end))))
    return out, running


def _merge(spans: list[tuple]) -> list[tuple]:
    out: list[list] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [tuple(x) for x in out]


def _overlap(a: datetime, b: datetime, spans: list[tuple]) -> float:
    return sum(max(0.0, (min(b, y) - max(a, x)).total_seconds()) for x, y in spans)


def _within(a: datetime, b: datetime, since: datetime, cap_min: float) -> float:
    """Seconds of [a, b] that fall in the first `cap_min` minutes after `since`."""
    return max(0.0, (min(b, since + timedelta(minutes=cap_min)) - a).total_seconds())


def minutes(entries: list[dict], now: datetime | None = None) -> dict:
    """Claude's working, background and idle minutes per counted prompt. `now` (a timezone-aware datetime)
    means the session is live: the time since the last entry counts too, and a turn still running is "open".
    Time before the first counted prompt (a login error, a prompt sent again) is not counted."""
    prompts = counted_prompts(entries)
    stamps = [t for t in (_ts(e.get("timestamp")) for e in entries) if t]
    if not prompts:
        return {"working_minutes": 0.0, "background_minutes": 0.0, "idle_minutes": 0.0, "turns": [],
                "background_running": 0}
    end = max(stamps + ([now] if now else []))
    starts = [p[0] for p in prompts]
    turns = [{"start": s, "work": 0.0, "fg": 0.0, "off": [], "status": None, "last": None} for s in starts]

    def turn_at(t: datetime) -> dict | None:
        i = bisect.bisect_right(starts, t) - 1
        return turns[i] if i >= 0 else None

    def add(a: datetime, b: datetime, label: str) -> None:
        # split at turn starts so each piece lands in its own turn; time before the first prompt is dropped
        while a < b:
            i = bisect.bisect_right(starts, a) - 1
            nxt = starts[i + 1] if i + 1 < len(starts) else None
            piece = min(b, nxt) if nxt else b
            if i >= 0:
                tr = turns[i]
                if label == "off":
                    tr["off"].append((a, piece))
                else:
                    tr["work" if label == "work" else "fg"] += (piece - a).total_seconds()
                    tr["last"] = piece
            a = piece

    def spend(a: datetime, b: datetime, since: datetime, cap: float, beyond: str) -> None:
        cut = a + timedelta(seconds=_within(a, b, since, cap))
        add(a, cut, "work")
        add(cut, b, beyond)

    def mark(t: datetime, status: str) -> None:
        tr = turn_at(t)
        if tr is not None:
            tr["status"] = status

    # state: ended (Claude stopped), pending (a prompt or notice it hasn't answered yet), model (waiting on
    # the model), tool (a tool call in flight). `since` is when the current wait began.
    state, since, pending_kind, outstanding, last_id, prev = "ended", None, None, set(), None, None
    for ts, kind, data in _events(entries):
        t = ts if prev is None or ts >= prev else prev
        more = kind == "model" and state == "ended" and bool(data["id"]) and data["id"] == last_id
        if prev is not None and t > prev:
            if kind == "prompt" or state == "pending" and kind != "model":
                add(prev, t, "off")
            elif state == "ended":
                add(prev, t, "work" if more else "off")   # `more`: the rest of a reply that already ended
            elif state == "tool":
                spend(prev, t, since, TOOL_CAP_MIN, "fg")
            else:
                spend(prev, t, since, MODEL_STALL_MIN, "off")
        if kind == "prompt":
            state, since, pending_kind = "pending", t, "prompt"
            outstanding.clear()
        elif kind == "notice":
            if state in ("ended", "pending"):
                state, since, pending_kind = "pending", t, "notice"
        elif kind == "result":
            outstanding.difference_update(data)
            if state == "model" or state == "tool" and not outstanding:
                state, since = "model", t
        elif kind == "model":
            if state in ("ended", "pending") and not more:
                mark(t, "open")
            if data["error"] or data["stop"] in END_STOPS:
                if not more:
                    mark(t, "end_turn")
                state = "ended"
                outstanding.clear()
            elif data["uses"]:
                if state != "tool":
                    since = t
                outstanding.update(data["uses"])
                state = "tool"
            elif not outstanding:
                state, since = "model", t
            last_id = data["id"]
        elif kind in ("interrupt", "end"):
            if state in ("model", "tool"):
                mark(t, "interrupted" if kind == "interrupt" else "end_turn")
            state = "ended"
            outstanding.clear()
        prev = t

    # After the last entry: a live session keeps counting; a collected one has stopped.
    no_reply = bool(now) and state == "pending" and pending_kind == "prompt" and since is not None \
        and end - since > timedelta(minutes=NO_REPLY_GRACE_MIN)
    if prev is not None and end > prev:
        if now and state == "tool":
            spend(prev, end, since, TOOL_CAP_MIN, "fg")
        elif now and state == "model":
            spend(prev, end, since, MODEL_STALL_MIN, "off")
        elif now and state == "pending" and pending_kind == "prompt":
            spend(prev, end, since, NO_REPLY_GRACE_MIN, "off")
        else:
            add(prev, end, "off")
    still_running = bool(now) and (state in ("model", "tool") or state == "pending" and pending_kind == "prompt")
    if still_running and turns[-1]["status"] != "open":
        turns[-1]["status"] = "open"

    jobs, running = _jobs(entries, end)
    jobs = _merge(jobs)
    out_turns, tw, tb, ti = [], 0.0, 0.0, 0.0
    for i, (tr, (pts, text)) in enumerate(zip(turns, prompts)):
        bg_jobs = sum(_overlap(a, b, jobs) for a, b in tr["off"])
        off = sum((b - a).total_seconds() for a, b in tr["off"])
        work, bg, idle = tr["work"], tr["fg"] + bg_jobs, off - bg_jobs
        status = tr["status"] or "end_turn"
        if status == "open" and not (i == len(turns) - 1 and still_running):
            status = "interrupted"   # cut off by the next prompt, or the session stopped mid-turn
        tw, tb, ti = tw + work, tb + bg, ti + idle
        out_turns.append({"index": i + 1, "start": pts.isoformat(timespec="seconds"),
                          "end": (tr["last"] or pts).isoformat(timespec="seconds"),
                          "working_minutes": round(work / 60, 1), "background_minutes": round(bg / 60, 1),
                          "idle_minutes": round(idle / 60, 1), "ended": status, "prompt": text[:200]})
        if no_reply and i == len(turns) - 1:
            out_turns[-1]["no_reply"] = round((end - since).total_seconds() / 60, 1)   # minutes since the prompt
    return {"working_minutes": round(tw / 60, 1), "background_minutes": round(tb / 60, 1),
            "idle_minutes": round(ti / 60, 1), "turns": out_turns, "background_running": running if now else 0}


def summarize(path: Path, now: datetime | None = None) -> dict:
    entries = []
    for line in Path(path).read_text(errors="ignore").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    models, efforts, tools = collections.Counter(), collections.Counter(), collections.Counter()
    tests, stamps = [], []
    last_text = ""
    for e in entries:
        ts = _ts(e.get("timestamp"))
        if ts:
            stamps.append(ts)
        msg = e.get("message") or {}
        if e.get("type") == "assistant" and not e.get("isSidechain"):
            if msg.get("model") and msg["model"] != "<synthetic>":
                models[msg["model"]] += 1
            for x in _content(e) or []:
                if not isinstance(x, dict):
                    continue
                if x.get("type") == "tool_use":
                    tools[x.get("name")] += 1
                    cmd = (x.get("input") or {}).get("command", "")
                    if isinstance(cmd, str) and TEST_CMD.search(cmd):
                        tests.append(cmd[:200])
                elif x.get("type") == "text" and x.get("text", "").strip():
                    last_text = x["text"].strip()
        oc = msg.get("output_config")
        if isinstance(oc, str):
            try:
                oc = ast.literal_eval(oc)
            except (ValueError, SyntaxError):
                oc = None
        if isinstance(oc, dict) and oc.get("effort"):
            efforts[oc["effort"]] += 1

    prompts = counted_prompts(entries)
    m = minutes(entries, now)
    wall = (max(stamps) - min(stamps)).total_seconds() if stamps else 0
    return {
        "file": str(path),
        "human_turns": len(prompts),
        "first_prompt": prompts[0][1] if prompts else None,
        "follow_ups": [p[1][:300] for p in prompts[1:]],
        "active_minutes": m["working_minutes"],   # what Austin enters as Claude minutes
        "working_minutes": m["working_minutes"],
        "background_minutes": m["background_minutes"],
        "idle_minutes": m["idle_minutes"],
        "background_running": m["background_running"],
        "turns": m["turns"],
        "wall_minutes": round(wall / 60, 1),
        "models": dict(models),
        "efforts": dict(efforts),
        "tools": dict(tools.most_common()),
        "test_commands": tests[:20],
        "final_message": last_text[:1500],
    }


def find(run_dir: Path) -> list[Path]:
    return sorted((Path(run_dir) / "prework_output").glob("*.jsonl"))


def model_problems(s: dict, want_model: str = "opus-5-5", want_effort: str = "high") -> list[str]:
    out = []
    if not s["models"]:
        out.append("no assistant messages found")
    other = [m for m in s["models"] if want_model not in m]
    if other:
        out.append(f"replies from other models: {other} (Johto requires Claude Opus 5.5)")
    if s["efforts"] and want_effort not in s["efforts"]:
        out.append(f"effort was {list(s['efforts'])}, not '{want_effort}'")
    elif len(s["efforts"]) > 1:
        out.append(f"effort changed during the run: {s['efforts']}")
    return out


def render(s: dict) -> str:
    lines = [
        f"- Human turns: {s['human_turns']} (follow-ups: {len(s['follow_ups'])})",
        f"- Claude minutes: {s['active_minutes']} working, {s.get('background_minutes', 0)} waiting on background jobs, "
        f"{s.get('idle_minutes', 0)} idle (wall clock {s['wall_minutes']})",
        f"- Models: {s['models']}  Effort: {s['efforts'] or 'not recorded'}",
        f"- Tools: {s['tools']}",
        f"- Test/build commands run: {len(s['test_commands'])}",
    ]
    lines += [f"    `{c}`" for c in s["test_commands"][:8]]
    if s["follow_ups"]:
        lines.append("- Your follow-ups:")
        lines += [f"    {i + 1}. {f}" for i, f in enumerate(s["follow_ups"])]
    if s["final_message"]:
        lines += ["- Claude's last message (excerpt):", "", "> " + s["final_message"][:800].replace("\n", "\n> ")]
    return "\n".join(lines)
