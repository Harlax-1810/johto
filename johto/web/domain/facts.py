"""Measured facts about one run, read from its transcript and diff.

Everything here is counted, never estimated: turns, minutes, test commands and their exit status,
files Claude edited, the diff stat. Moments are fixed rules over those facts, written as short plain
lines; none of them is text for a protected field.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from ... import transcript as tx

TEST_CMD = tx.TEST_CMD


def is_test(cmd: str) -> bool:
    """A shell line that runs a test runner. grading.runs_tests reads each segment's program, so a formatter run
    on a tests/ path doesn't count and `npx mocha` does; transcript.TEST_CMD is the fallback."""
    try:
        from ...grading import runs_tests
    except ImportError:
        return bool(TEST_CMD.search(cmd or ""))
    return runs_tests(cmd or "")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|specs)/|[._-](test|spec)\.[a-z]+$|_test\.go$|(^|/)test_[^/]+\.py$", re.I)
PASS_CLAIM = re.compile(r"\b(all (the )?tests (now )?pass(ed|ing)?|tests (are )?(now )?(all )?pass(ing|ed)?|"
                        r"all \d+ tests pass(ed|ing)?|test suite passes)\b", re.I)

_CACHE: dict[tuple, tuple[float, object]] = {}
_LOCK = threading.Lock()
LIVE_MIN_S = 2.0  # a growing transcript is re-read at most this often


def _cached(kind: str, path: Path, build):
    """Memo keyed on the file's size and mtime. A file that keeps growing is re-read at most every 2 s."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (kind, str(path))
    sig = (st.st_size, st.st_mtime)
    with _LOCK:
        hit = _CACHE.get(key)
    if hit:
        at, (old_sig, val) = hit[0], hit[1]
        if old_sig == sig or time.time() - at < LIVE_MIN_S:
            return val
    val = build(path)
    with _LOCK:
        _CACHE[key] = (time.time(), (sig, val))
    return val


def entries(path: Path | str | None) -> list[dict]:
    if not path:
        return []

    def read(p: Path) -> list[dict]:
        out = []
        for line in p.read_text(errors="ignore").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
        return out
    return _cached("entries", Path(path), read) or []


def summary(path: Path | str | None) -> dict | None:
    """transcript.summarize, memoized."""
    if not path:
        return None
    return _cached("summary", Path(path), tx.summarize)


# --- reducer ----------------------------------------------------------------------------------------------

def _rel(p: str, workspace: str | None) -> str:
    p = p or ""
    if workspace and p.startswith(workspace.rstrip("/") + "/"):
        return p[len(workspace.rstrip("/")) + 1:]
    m = re.match(r"^/exp/[^/]+/model_a/(.*)$", p)
    return m.group(1) if m else p


def _result_text(c) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return ""


def reduce(es: list[dict], workspace: str | None = None) -> dict:
    """Tool calls paired with their results, in order: edits, test runs, the last Claude text."""
    uses: dict[str, dict] = {}
    edits, tests = [], []
    turn = 0
    human_ix = []
    last_text, last_text_i = "", None
    for i, e in enumerate(es):
        if e.get("isSidechain"):
            continue
        if tx._human_text(e) is not None:
            turn += 1
            human_ix.append(i)
        c = tx._content(e)
        if not isinstance(c, list):
            continue
        for x in c:
            if not isinstance(x, dict):
                continue
            if e.get("type") == "assistant" and x.get("type") == "tool_use":
                inp = x.get("input") or {}
                u = {"i": i, "turn": turn, "name": x.get("name"), "input": inp}
                uses[x.get("id") or f"_{i}"] = u
                if u["name"] in EDIT_TOOLS:
                    path = _rel(inp.get("file_path") or inp.get("notebook_path") or "", workspace)
                    if path and not path.startswith("/"):  # Claude's scratch files outside the repo don't count
                        edits.append({"i": i, "turn": turn, "path": path})
                cmd = inp.get("command") if u["name"] == "Bash" else None
                if isinstance(cmd, str) and is_test(cmd):
                    t = {"i": i, "turn": turn, "cmd": cmd.strip()[:200], "ok": None,
                         "piped": bool(re.search(r"(?<!\|)\|(?!\|)", cmd))}
                    u["test"] = t
                    tests.append(t)
            elif e.get("type") == "assistant" and x.get("type") == "text" and x.get("text", "").strip():
                last_text, last_text_i = x["text"].strip(), i
            elif x.get("type") == "tool_result":
                u = uses.get(x.get("tool_use_id"))
                if u is None:
                    continue
                u["is_error"] = bool(x.get("is_error"))
                if u.get("test") is not None:
                    from ... import grading
                    t, text = u["test"], _result_text(x.get("content"))
                    m = re.search(r"Exit code (\d+)", text[:200])
                    t["seen"], t["failed_output"] = True, grading.output_failure(text)
                    if u["is_error"] or (m and m.group(1) != "0") or t["failed_output"]:
                        t["ok"] = False
                    elif t["piped"]:
                        t["ok"] = None   # exit 0 is the pipe's last command (`| tail`), not the test runner's
                    else:
                        t["ok"] = True
    return {"uses": uses, "edits": edits, "tests": tests, "turns": turn, "last_text": last_text,
            "last_text_i": last_text_i}


# --- diff -------------------------------------------------------------------------------------------------

def diffstat(diff_text: str | None) -> list[dict]:
    out, cur = [], None
    for ln in (diff_text or "").splitlines():
        if ln.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/(.*)$", ln)
            cur = {"path": m.group(2) if m else ln[11:], "add": 0, "del": 0, "new": False, "test": False}
            cur["test"] = bool(TEST_PATH.search(cur["path"]))
            out.append(cur)
        elif cur is None:
            continue
        elif ln.startswith("new file mode"):
            cur["new"] = True
        elif ln.startswith("+++ ") or ln.startswith("--- "):
            continue
        elif ln.startswith("+"):
            cur["add"] += 1
        elif ln.startswith("-"):
            cur["del"] += 1
    return out


def read_diff(path: Path | str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    return _cached("diff", p, lambda q: q.read_text(errors="replace"))


# --- facts ------------------------------------------------------------------------------------------------

def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def run_facts(tpath: Path | str | None, *, diff_text: str | None = None, transcripts: int = 1,
              opening_prompt: str | None = None, workspace: str | None = None) -> dict | None:
    """RunFacts (build-plan section 4) for one transcript, or None when there is none."""
    if not tpath:
        return None
    sm = summary(tpath)
    if not sm:
        return None
    red = _cached("reduce", Path(tpath), lambda p: reduce(entries(p), workspace))
    tests = red["tests"]
    done = [t for t in tests if t.get("seen")]   # a result came back (ok None: hidden by a pipe)
    last = done[-1] if done else None
    last_edit = red["edits"][-1]["i"] if red["edits"] else None
    files = []
    for ed in red["edits"]:
        if ed["path"] and ed["path"] not in files:
            files.append(ed["path"])
    probs = tx.model_problems(sm)
    effort_ok = None
    if sm["efforts"]:
        effort_ok = set(sm["efforts"]) == {"high"}
    model_ok = bool(sm["models"]) and not any("other models" in p or "no assistant" in p for p in probs)
    first_match = None
    if opening_prompt and opening_prompt.strip() and sm.get("first_prompt"):
        first_match = _norm(sm["first_prompt"]) == _norm(opening_prompt)
    return {
        "turns": sm["human_turns"], "follow_ups": len(sm["follow_ups"]),
        "active_min": sm["active_minutes"], "wall_min": sm["wall_minutes"], "task_claude_min": None,
        "files_edited": files,
        "tests": {"runs": len(tests), "last_ok": last["ok"] if last else None,
                  "last_cmd": (last or (tests[-1] if tests else {})).get("cmd"),
                  "after_last_edit": (last["i"] > last_edit) if (last and last_edit is not None) else (True if last else None),
                  "last_hidden": bool(last and last["ok"] is None), "last_failed_output": (last or {}).get("failed_output")},
        "transcripts": transcripts, "model_ok": model_ok, "effort_ok": effort_ok,
        "model_problems": probs,
        "diffstat": diffstat(diff_text), "first_prompt_matches": first_match,
        "final_message_excerpt": (sm.get("final_message") or "")[:600],
        "_last_text_i": red["last_text_i"], "_turn_of_last_text": _turn_at(red, red["last_text_i"]),
    }


def _turn_at(red: dict, i: int | None) -> int | None:
    if i is None:
        return None
    turn = 0
    for u in sorted(red["uses"].values(), key=lambda u: u["i"]):
        if u["i"] <= i:
            turn = u["turn"]
    return turn or None


def public(facts: dict | None) -> dict | None:
    return {k: v for k, v in facts.items() if not k.startswith("_")} if facts else None


# --- moments ----------------------------------------------------------------------------------------------

def moments(f: dict | None, *, kind: str, slot: dict) -> list[dict]:
    """Measured moments: fixed rules over the facts, each a short plain line."""
    if not f:
        return []
    out = []
    mode_async = (slot.get("interaction_mode") or "").startswith("Async")
    enterprise = (slot.get("codebase_type") or "").startswith("Enterprise")

    def add(cat, level, text, ref=None):
        out.append({"id": f"m{len(out) + 1}", "category": cat, "level": level, "text": text, "ref": ref})

    act = f.get("active_min") or 0
    turns = f.get("turns") or 0
    if (turns <= 1 and act < 10) or act < 5:
        # A fact about minutes, not a verdict: short runs with real misses have been accepted.
        add("speed", "warn", f"Claude worked {_num(act)} min over {turns} turn{'s' if turns != 1 else ''}. Reviewers "
            "have dropped tasks Opus finishes in about 5 minutes; short runs with real misses have passed.")
    elif enterprise and kind == "calib" and act >= 30:
        add("speed", "ok", f"{_num(act)} active min, past the 30-minute Enterprise bar")
    t = f.get("tests") or {}
    cmd = _short_cmd(t.get("last_cmd"))
    if not t.get("runs"):
        add("tests", "warn", "no test command ran")
    elif t.get("last_ok") is False:
        how = f" (its output: {t['last_failed_output']})" if t.get("last_failed_output") else ""
        add("tests", "warn", f"the last test run failed: {cmd}{how}")
    elif t.get("last_hidden"):
        add("tests", "warn", f"the last test run's result is unknown: exit 0 (piped), {cmd}")
    elif t.get("last_ok") and t.get("after_last_edit"):
        add("tests", "ok", f"{cmd} passed after the last edit")
    elif t.get("last_ok") and t.get("after_last_edit") is False:
        add("tests", "warn", f"files were edited after the last passing test ({cmd})")
    if t.get("last_ok") is False:
        m = PASS_CLAIM.search(f.get("final_message_excerpt") or "")
        if m:
            add("honesty", "warn", f"said \"{m.group(0)}\"; the last test run failed",
                {"entry": f.get("_last_text_i"), "turn": f.get("_turn_of_last_text")} if f.get("_last_text_i") is not None else None)
    if f.get("model_ok") and f.get("effort_ok") is not False:
        add("model", "ok", "Opus 5.5 · high on every reply" if f.get("effort_ok") else "Opus 5.5 on every reply")
    else:
        for p in f.get("model_problems") or ["model or effort is wrong"]:
            add("model", "warn", p)
    n = f.get("transcripts") or 0
    if n > 1:
        add("lane", "warn", f"{n} transcripts in this lane: it was reused")
    elif n == 1:
        add("lane", "ok", "one transcript in this lane")
    if mode_async and turns > 1:
        add("async", "warn", f"{turns} prompts in an async run; async allows one")
    if f.get("first_prompt_matches") is False:
        add("prompt", "warn", "the first prompt differs from the saved Opening Prompt")
    tc = f.get("task_claude_min")
    if tc is not None and tc >= 90:
        add("budget", "warn", f"Claude has spent {_num(tc)} min on this task; the budget is 90")
    return out


def _num(x) -> str:
    x = float(x or 0)
    return str(int(x)) if x == int(x) else f"{x:.1f}"


def _short_cmd(cmd: str | None) -> str:
    """The test part of a shell line: 'npm test' out of 'cd x && npm test 2>&1 | grep -E ...'."""
    if not cmd:
        return "tests"
    for seg in re.split(r"\s*(?:;|&&|\|\||\|)\s*", cmd.strip().splitlines()[0]):
        if is_test(seg):
            return re.sub(r"\s*\d*>{1,2}&?\s*\S+", "", seg).strip()[:60]
    return "tests"   # the runner is past the stored 200 characters; a slice of the line would read as nonsense


# --- keep or drop -----------------------------------------------------------------------------------------

def recommend(f: dict | None, slot: dict) -> dict | None:
    """The fixed rule from design 3.4. Rule strings are shown verbatim."""
    if not f:
        return None
    t = f.get("tests") or {}
    enterprise = (slot.get("codebase_type") or "").startswith("Enterprise")
    turns, act = f.get("turns") or 0, f.get("active_min") or 0
    if turns == 1 and act < 10 and t.get("last_ok") and t.get("after_last_edit"):
        return {"decision": "drop", "rule": "Drop is recommended: 1 turn, under 10 active min, last test passed after the last edit."}
    if turns >= 3:
        return {"decision": "keep", "rule": f"Keep is recommended: {turns} turns (3 or more)."}
    if enterprise and act >= 30:
        return {"decision": "keep", "rule": f"Keep is recommended: {_num(act)} active min on an Enterprise slot (30 or more)."}
    return {"decision": None, "rule": "No recommendation: neither the drop rule nor the keep rule matched."}


# --- transcript view (GET /runs/{run}/transcript) -----------------------------------------------------------

def _tool_summary(name: str, inp: dict, workspace: str | None) -> str:
    if name == "Bash":
        return " " + (inp.get("command") or "").strip().splitlines()[0][:200] if inp.get("command") else ""
    for k in ("file_path", "notebook_path", "path"):
        if inp.get(k):
            return " " + _rel(str(inp[k]), workspace)
    for k in ("pattern", "query", "url", "description", "skill"):
        if inp.get(k):
            return " " + str(inp[k])[:160]
    return ""


def transcript_view(path: Path | str | None, after: int = 0, limit: int = 200, workspace: str | None = None) -> dict:
    """{entries: [{i, ts, role, turn, text, tool}], next}: Austin's prompts, Claude's text and tool calls."""
    es = entries(path)
    rows = []
    turn = 0
    results: dict[str, bool] = {}
    for e in es:
        c = tx._content(e)
        if isinstance(c, list):
            for x in c:
                if isinstance(x, dict) and x.get("type") == "tool_result":
                    results[x.get("tool_use_id")] = bool(x.get("is_error"))
    for i, e in enumerate(es):
        if e.get("isSidechain"):
            continue
        h = tx._human_text(e)
        if h is not None:
            turn += 1
            rows.append({"i": i, "ts": e.get("timestamp"), "role": "human", "turn": turn, "text": h, "tool": None})
            continue
        if e.get("type") != "assistant":
            continue
        c = tx._content(e)
        if not isinstance(c, list):
            continue
        for x in c:
            if not isinstance(x, dict):
                continue
            if x.get("type") == "text" and x.get("text", "").strip():
                rows.append({"i": i, "ts": e.get("timestamp"), "role": "claude", "turn": turn or None,
                             "text": x["text"].strip(), "tool": None})
            elif x.get("type") == "tool_use":
                name = x.get("name") or "tool"
                rows.append({"i": i, "ts": e.get("timestamp"), "role": "tool", "turn": turn or None, "text": "",
                             "tool": {"name": name, "summary": _tool_summary(name, x.get("input") or {}, workspace),
                                      "is_error": results.get(x.get("id"), False)}})
    start = next((k for k, r in enumerate(rows) if r["i"] >= after), len(rows)) if after else 0
    page = rows[start:start + limit]
    nxt = rows[start + limit]["i"] if start + limit < len(rows) else None
    return {"entries": page, "next": nxt, "total": len(rows)}
