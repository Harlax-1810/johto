"""Facts from a Claude Code session transcript (.jsonl): turns, minutes, model, effort, tools."""
from __future__ import annotations

import ast
import collections
import json
import re
from datetime import datetime
from pathlib import Path

TEST_CMD = re.compile(
    r"\b(pytest|go test|cargo test|npm (run )?test|pnpm (run )?test|yarn test|bun test|vitest|jest|"
    r"rspec|bundle exec|phpunit|mvn test|gradle\w* test|dotnet test|make test|tox|unittest|ctest|"
    r"manage\.py test)\b")


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
    # Claude Code injects background-task notices and reminders as user-role entries; they are not Austin.
    if not t or t.startswith(("<command-", "<local-command", "Caveat:", "[Request interrupted", "<task-notification",
                              "<system-reminder", "<bash-notification", "<agent-message", "<cross-session")):
        return None
    return t


def _typed_by_austin(e: dict) -> bool:
    """A prompt or a local command (/exit, /login and its caveat and output), not Claude's work. Tool results
    and the notices Claude Code injects as user entries still count as the model's turn."""
    if e.get("type") != "user" or e.get("isSidechain"):
        return False
    if _human_text(e) is not None:
        return True
    c = _content(e)
    if isinstance(c, list):
        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return isinstance(c, str) and c.lstrip().startswith(("<command-", "<local-command"))


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


def summarize(path: Path) -> dict:
    entries = []
    for line in Path(path).read_text(errors="ignore").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    models, efforts, tools = collections.Counter(), collections.Counter(), collections.Counter()
    tests, prompts, stamps, work = [], [], [], []
    last_text = ""
    for e in entries:
        ts = _ts(e.get("timestamp"))
        if ts:
            stamps.append(ts)
            if not _typed_by_austin(e):
                work.append(ts)
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
        h = _human_text(e)
        if h is not None and ts:
            prompts.append((ts, h))

    prompts = counted_prompts(entries)

    # Active minutes: from each counted prompt to the model's last entry before the next one. What Austin
    # typed doesn't extend a turn: an /exit 20 minutes after the last answer is not Claude working.
    active = 0.0
    bounds = [p[0] for p in prompts]
    for i, start in enumerate(bounds):
        end_limit = bounds[i + 1] if i + 1 < len(bounds) else None
        in_turn = [s for s in work if s >= start and (end_limit is None or s < end_limit)]
        if in_turn:
            active += (max(in_turn) - start).total_seconds()
    wall = (max(stamps) - min(stamps)).total_seconds() if stamps else 0
    return {
        "file": str(path),
        "human_turns": len(prompts),
        "first_prompt": prompts[0][1] if prompts else None,
        "follow_ups": [p[1][:300] for p in prompts[1:]],
        "active_minutes": round(active / 60, 1),
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
        f"- Claude active minutes: {s['active_minutes']} (wall clock {s['wall_minutes']})",
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
