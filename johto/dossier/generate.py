"""One dossier generation (docs/dossier-spec.md section 4).

Clone the repo at the pinned commit, run headless Claude Code in the clone with read-only tools, take
the JSON between the <<<DOSSIER / DOSSIER>>> lines of its final message, validate it, send the errors
back to the same session once if it fails, and delete the clone and the session's files afterwards.

Claude runs with `--output-format stream-json` (the json result plus live events) so the worker can
show progress; the final `result` event carries the same cost and text as `--output-format json`.
"""
from __future__ import annotations

import atexit
import collections
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import config
from . import candidates, schema, store
from .prompt import PROMPT, SCHEMA

CLAUDE_BIN = Path(config.CLAUDE_BIN)   # JOHTO_CLAUDE_BIN; a dead path in fake mode, so nothing paid runs
MODEL = "opus"
EFFORT = "high"
TIMEOUT_S = 35 * 60  # one deadline for the whole generation: clone, first pass and repair pass
REPAIR_TIMEOUT_S = 10 * 60  # the repair pass gets at most this, and never past the deadline
MIN_REPAIR_S = 60
GIT_TIMEOUT_S = 15 * 60
BEGIN, END = "<<<DOSSIER", "DOSSIER>>>"

ALLOWED_TOOLS = [
    "Read", "Grep", "Glob",
    "Bash(git log:*)", "Bash(git show:*)", "Bash(ls:*)", "Bash(wc:*)", "Bash(find:*)", "Bash(rg:*)",
    "Bash(gh issue list:*)", "Bash(gh issue view:*)", "Bash(gh pr list:*)", "Bash(gh pr view:*)",
    "Bash(gh search:*)",
]
# Deny rules win over allow rules. They close the write and exec escapes of the allowed commands.
DENIED_TOOLS = [
    "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent",
    "Bash(find * -delete*)", "Bash(find * -exec*)", "Bash(find * -execdir*)", "Bash(find * -ok*)",
    "Bash(find * -fprint*)", "Bash(find * -fls*)", "Bash(find -delete*)", "Bash(find -exec*)",
    "Bash(rg * --pre*)", "Bash(rg --pre*)", "Bash(git * --output*)", "Bash(gh * --web*)",
]
# Variables of the Claude Code session that started this process; a nested run must not inherit them.
_ENV_KEEP = {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"}
# ANTHROPIC_* (API key, auth token, base URL, model overrides) would bill the API or change the model
# instead of using the subscription. Names listed in JOHTO_DOSSIER_KEEP_ENV (comma-separated) pass through.
KEEP_ENV_VAR = "JOHTO_DOSSIER_KEEP_ENV"
# Matched only against the text of an error result event, never against the model's own messages.
RATE_LIMIT = re.compile(r"usage limit|rate.?limit|hit your limit|limit reached|overloaded|\b429\b|\b529\b", re.I)

Progress = Callable[[dict], None]


@dataclass
class Result:
    ok: bool = False
    doc: dict | None = None
    errors: list[str] = field(default_factory=list)
    error: str | None = None
    cost_usd: float = 0.0
    elapsed_s: int = 0
    model: str | None = None
    passes: int = 0
    retryable: bool = False  # hit a usage limit: requeue instead of failing
    cancelled: bool = False  # stopped on request (pause, shutdown): requeue, not a failure
    skipped: bool = False  # run_item only: the item was no longer queued, nothing ran


class Cancel:
    """Lets another thread stop a generation: kills the claude or git process it is running and makes
    generate() return a cancelled Result. cancel() may come before, during or between the steps."""

    def __init__(self):
        self.reason: str | None = None
        self._event = threading.Event()
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self.reason = reason
            self._event.set()
            procs = list(self._procs)
        for p in procs:
            if p.poll() is None:
                _killpg(p)

    def attach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.add(proc)
            late = self._event.is_set()
        if late and proc.poll() is None:
            _killpg(proc)

    def detach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)


# --- progress ---------------------------------------------------------------------------

class _Progress:
    """Writes data/dossiers/runs/<id>.log and reports {step, elapsed_s, log_tail} at most every 5 s."""

    def __init__(self, did: str, cb: Progress | None, echo: Callable[[str], None] | None = None):
        self.f = open(store.run_log(did), "a", buffering=1)
        self.tail: collections.deque[str] = collections.deque(maxlen=8)
        self.step = "starting"
        self.t0 = time.time()
        self.cb, self.echo = cb, echo
        self.lock = threading.Lock()
        self._last = 0.0
        self.f.write(f"\n=== {store.now_iso()} generation of {did}\n")

    def snapshot(self) -> dict:
        return {"step": self.step, "elapsed_s": round(time.time() - self.t0), "log_tail": list(self.tail)}

    def raw(self, text: str) -> None:
        with self.lock:
            self.f.write(text.rstrip("\n") + "\n")

    def line(self, text: str, step: str | None = None, force: bool = False) -> None:
        with self.lock:
            changed = step is not None and step != self.step
            if step:
                self.step = step
            self.f.write(f"{time.strftime('%H:%M:%S')} [{self.step}] {text}\n")
            self.tail.append(text[:160])
            due = force or changed or time.time() - self._last >= 5
            if due:
                self._last = time.time()
        if self.echo:
            self.echo(f"{round(time.time() - self.t0):>5}s  {text}")
        if due and self.cb:
            try:
                self.cb(self.snapshot())
            except Exception as e:  # progress must never break a run
                self.raw(f"progress callback failed: {e}")

    def close(self) -> None:
        if self.cb:
            try:
                self.cb(self.snapshot())
            except Exception:
                pass
        self.f.close()


def _rel(path: str, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root.resolve()))
    except (ValueError, OSError):
        return path


def describe(ev: dict, root: Path) -> list[tuple[str, str]]:
    """(step, log line) pairs for one stream-json event."""
    out = []
    t = ev.get("type")
    if t == "assistant":
        for c in (ev.get("message") or {}).get("content") or []:
            if c.get("type") == "tool_use":
                name, inp = c.get("name"), c.get("input") or {}
                if name == "Read":
                    where = _rel(inp.get("file_path", ""), root)
                    if inp.get("offset"):
                        where += f":{inp['offset']}"
                    out.append(("reading the code", f"Read {where}"))
                elif name in ("Grep", "Glob"):
                    extra = f" in {_rel(inp['path'], root)}" if inp.get("path") else ""
                    out.append(("searching the code", f"{name} {inp.get('pattern', '')}{extra}"))
                elif name == "Bash":
                    cmd = " ".join(str(inp.get("command", "")).split())
                    step = ("reading issues and PRs" if cmd.startswith("gh ")
                            else "reading history" if cmd.startswith("git ") else "listing files")
                    out.append((step, f"$ {cmd[:150]}"))
                else:
                    out.append(("reading the code", f"{name}"))
            elif c.get("type") == "text" and BEGIN in (c.get("text") or ""):
                out.append(("writing the dossier", "dossier written"))
    elif t == "system" and ev.get("subtype") == "permission_denied":
        out.append(("", f"denied {ev.get('tool_name')}"))
    return out


# --- clone ----------------------------------------------------------------------------------

class HardTimeout(Exception):
    pass


def _git(*args, cwd: Path | None = None, timeout: float = GIT_TIMEOUT_S,
         cancel: Cancel | None = None) -> subprocess.CompletedProcess:
    """git in its own process group, so a timeout or a cancel kills it and its helpers."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = subprocess.Popen(["git", *args], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
    if cancel:
        cancel.attach(proc)
    try:
        out, err = proc.communicate(timeout=max(1, timeout))
    except subprocess.TimeoutExpired:
        _killpg(proc)
        proc.communicate()
        raise HardTimeout(f"git {args[0]} timed out after {round(timeout)}s")
    finally:
        if cancel:
            cancel.detach(proc)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _left(deadline: float | None, cap: float) -> float:
    if deadline is None:
        return cap
    left = deadline - time.time()
    if left <= 0:
        raise HardTimeout("timed out")
    return min(cap, left)


def clone_repo(full_name: str, commit: str, dest: Path, deadline: float | None = None,
               cancel: Cancel | None = None) -> Path:
    """Blobless clone (full history, blobs on demand) checked out at the pinned commit. Each git step gets
    at most GIT_TIMEOUT_S and never runs past `deadline`."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    def git(*args, cwd=None):
        if cancel and cancel.cancelled:
            raise RuntimeError(cancel.reason or "cancelled")
        return _git(*args, cwd=cwd, timeout=_left(deadline, GIT_TIMEOUT_S), cancel=cancel)

    r = git("clone", "--quiet", "--filter=blob:none", "--no-checkout", f"https://github.com/{full_name}.git",
            str(dest))
    if r.returncode != 0:
        raise RuntimeError(f"git clone failed: {r.stderr.strip()[-400:]}")
    co = git("checkout", "--quiet", "--detach", commit, cwd=dest)
    if co.returncode != 0:
        f = git("fetch", "--quiet", "--filter=blob:none", "origin", commit, cwd=dest)
        co = git("checkout", "--quiet", "--detach", commit, cwd=dest)
        if co.returncode != 0:
            raise RuntimeError(f"checkout of {commit[:8]} failed: {(f.stderr + co.stderr).strip()[-400:]}")
    head = git("rev-parse", "HEAD", cwd=dest).stdout.strip()
    if head != commit:
        raise RuntimeError(f"clone is at {head[:8]}, not {commit[:8]}")
    # The repo's own Claude settings could add hooks or wider permissions; they are not ours to run.
    for name in ("settings.json", "settings.local.json"):
        (dest / ".claude" / name).unlink(missing_ok=True)
    return dest


def cleanup(clone: Path, session_id: str | None) -> None:
    """Delete the clone and every file Claude Code kept for this session."""
    shutil.rmtree(clone, ignore_errors=True)
    if not session_id:
        return
    cfg = claude_config_dir()
    for f in (cfg / "projects").glob(f"*/{session_id}.jsonl"):
        if "dossier-clones" in f.parent.name:
            shutil.rmtree(f.parent, ignore_errors=True)
        else:
            f.unlink(missing_ok=True)
            shutil.rmtree(f.parent / session_id, ignore_errors=True)
    for sub in ("todos", "file-history", "session-env", "debug", "tasks", "shell-snapshots"):
        for p in (cfg / sub).glob(f"*{session_id}*"):
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _project_dir_name(path: Path | str) -> str:
    """Claude Code names a project's folder after its path with every other character than [A-Za-z0-9] as '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def clean_stale_sessions(clones: Path) -> list[Path]:
    """Session folders of generations whose process died before cleanup() ran: the project folders named
    after a clone inside `clones`. Call only while holding the generation lock."""
    prefixes = {_project_dir_name(clones) + "-", _project_dir_name(Path(clones).resolve()) + "-"}
    gone = []
    for d in (claude_config_dir() / "projects").glob("*dossier-clones*"):
        if d.is_dir() and any(d.name.startswith(p) for p in prefixes):
            shutil.rmtree(d, ignore_errors=True)
            gone.append(d)
    return gone


# --- prompt -------------------------------------------------------------------------------

def _n(x) -> str:
    return f"{x:,}" if isinstance(x, int) else str(x)


def repo_facts(entry: dict, rec: dict, did: str) -> str:
    c = rec.get("counts") or {}
    exts = ", ".join(f"{k} {v}" for k, v in sorted((c.get("top_extensions") or {}).items(), key=lambda kv: -kv[1]))
    verdicts = "; ".join(
        f"{k} {v.get('overall')}" + (f" ({', '.join(v.get('fails') or [])})" if v.get("fails") else "")
        for k, v in (rec.get("verdicts") or {}).items())
    fits = entry.get("fits") or (candidates.fits_for(candidates.ok_types(rec)) if rec else [])
    lines = [
        f"- Repository: {entry['full_name']} at commit {entry['commit']}",
        f"- Pinned URL: {entry.get('pinned_url') or ''}",
        f"- Dossier id: {did}",
    ]
    if rec.get("description"):
        lines.append(f"- Description: {rec['description']}")
    if rec.get("topics"):
        lines.append(f"- Topics: {', '.join(rec['topics'])}")
    lines.append(f"- Primary language: {entry.get('language') or rec.get('studio_language') or rec.get('language')}"
                 + (f"; file extensions: {exts}" if exts else ""))
    if c:
        lines.append(f"- Size: {_n(c.get('source_files'))} source files, {_n(c.get('test_files'))} test files, "
                     f"{_n(c.get('tracked_files'))} tracked files")
    v = rec.get("verify") or {}
    if v.get("commands"):
        lines.append(f"- Test and build commands found: {', '.join(v['commands'])}")
    if v.get("ci_workflows"):
        lines.append(f"- CI workflows: {', '.join(v['ci_workflows'][:8])}")
    if rec.get("docker"):
        lines.append(f"- Docker files: {', '.join(rec['docker'])}")
    act = [f"{_n(rec['stars'])} stars" if rec.get("stars") is not None else None,
           f"{_n(rec['open_issues'])} open issues" if rec.get("open_issues") is not None else None,
           f"last push {rec['pushed_at'][:10]}" if rec.get("pushed_at") else None,
           f"default branch {rec['default_branch']}" if rec.get("default_branch") else None]
    if any(act):
        lines.append("- Activity: " + ", ".join(a for a in act if a))
    if verdicts:
        lines.append(f"- Studio codebase check by codebase type: {verdicts}")
    if entry.get("reasons"):
        lines.append(f"- Why it was picked: {'; '.join(entry['reasons'])}")
    if fits:
        lines.append("- Open Johto slots for the codebase types it passes (action | domain | codebase type: open):")
        lines += [f"  - {f['action']} | {f['domain']} | {f['codebase_type']}: {f.get('open', '?')}" for f in fits]
    lines.append("- The working directory is the clone, checked out at the pinned commit with full history "
                 "(file contents of old commits download on demand). `gh` resolves to the upstream repository.")
    return "\n".join(lines)


def output_rules(entry: dict, did: str, rec: dict | None = None) -> str:
    ctx = validation_context(rec or {})
    labels = []
    if isinstance(ctx["source_files"], int) and ctx["source_files"] < schema.UA_MIN_FILES:
        labels.append(f"Understand / Assess is not available: the repository has {ctx['source_files']} source "
                      f"files, under {schema.UA_MIN_FILES}.")
    if ctx["codebase_types"]:
        labels.append(f"codebase_type is only {' or '.join(ctx['codebase_types'])}, the type"
                      f"{'s' if len(ctx['codebase_types']) != 1 else ''} that passed the Studio codebase check.")
    return "\n".join([
        "- Tools: Read, Grep, Glob, and Bash for git log, git show, ls, wc, find, rg, gh issue list/view, "
        "gh pr list/view and gh search. Nothing can be edited.",
        f"- End your final message with the dossier JSON alone between a line `{BEGIN}` and a line `{END}`: "
        "no code fence, nothing else between them.",
        f'- Use these values: "schema": "{schema.SCHEMA}", "id": "{did}", "repo": "{entry["full_name"]}", '
        f'"commit": "{entry["commit"]}", "pinned_url": "{entry.get("pinned_url")}". Set generated_at, model, '
        "effort, elapsed_s and cost_usd to null; they are filled in afterwards.",
        f"- slot_fit and best_fits: action is one of {', '.join(schema.ACTIONS)}; domain is one of "
        f"{', '.join(schema.DOMAINS)}; codebase_type is one of {', '.join(schema.CODEBASE_TYPES)}.",
        "- Pointers are \"path:line\" or \"path:start-end\" relative to the repository root, naming a file you "
        "read and a line range inside it. Paths in architecture nodes and subsystems must exist.",
        "- Word limits are hard limits. The document is validated before anyone sees it: types, enums, word "
        "limits, every pointer checked against the files, at most 30 blocks, 3 to 7 opportunity blocks, edges "
        "that reference existing node ids, block titles of at most 10 words, html blocks with no <script, no "
        "on*= handlers and no URLs of any kind: href only to #ids, src only data: (inline SVG needs no xmlns attribute). No em dashes anywhere, "
        "and no text addressed to a reader.",
        "- Text checks: each where_models_fail item names the file the hazard is in and starts with the code "
        "element (a file, function, table, caller or test), never with a verb or an -ing word; ambition, shape "
        "and where_models_fail never use 'instead of', 'rather than', 'better', 'worse' or 'should'; ambition "
        "and shape never open with a verb such as Add, Fix, Make or Ensure and never use 'must'; every "
        "questions item is a question ending in '?'.",
        *(f"- {s}" for s in labels),
        "- Page order: overview, architecture, flow, subsystems, the data model block, then the opportunities "
        "best first; code, facts, callout and questions blocks where they help.",
    ])


FOCUS_STYLE = {
    "Casual prose": "a short casual message from the engineer, about 20 to 60 words",
    "Explicit spec": "a fuller written spec",
    "Error / artifact brief": "a pasted error or file with a short note",
}
FOCUS_MODE = {
    "Async/autonomous": "Claude works alone until it is done, with no replies from the engineer",
    "Interactive/pairing": "the engineer answers Claude while it works",
}


def focus_section(entry: dict) -> str | None:
    """The slot groups this research was started for, or None. Each focus is "Action|Domain|Codebase type" and
    may add "|Prompt style|Interaction mode"."""
    rows = []
    for f in entry.get("focus") or []:
        parts = [p.strip() for p in str(f).split("|")]
        if len(parts) < 3:
            continue
        how = []
        if len(parts) > 3 and parts[3] in FOCUS_STYLE:
            how.append(f"written as {FOCUS_STYLE[parts[3]]}")
        if len(parts) > 4 and parts[4] in FOCUS_MODE:
            how.append(FOCUS_MODE[parts[4]])
        rows.append(f"- {parts[0]} | {parts[1]} | {parts[2]}" + (f" ({'; '.join(how)})" if how else ""))
    if not rows:
        return None
    return ("## Open slots this research is for\n\n"
            "The engineer has open slots of these kinds and few good problems for them. Besides covering the "
            "project as usual, find at least two opportunities for each slot below whose `slot_fit` names exactly "
            "that action, domain and codebase type, and that really are that kind of task (an Operate opportunity "
            "is about running, configuring or diagnosing the system; an Understand / Assess one has ground truth a "
            "reviewer can check). When a slot's task is a short casual message run without replies, prefer problems "
            "a user could state in a few plain sentences and whose finished state can be checked at the end of one "
            "unattended session. If the project has no honest opportunity for a slot, say so in a `callout` block "
            "instead of forcing one.\n\n" + "\n".join(rows))


def build_prompt(entry: dict, rec: dict, did: str) -> str:
    focus = focus_section(entry)
    return "\n\n".join([
        PROMPT,
        "## The dossier schema (johto.dossier/1)\n\n" + SCHEMA,
        "## Repository facts\n\n" + repo_facts(entry, rec, did),
        *([focus] if focus else []),
        "## Output\n\n" + output_rules(entry, did, rec),
    ])


def repair_prompt(errors: list[str]) -> str:
    shown = errors[:60]
    more = f"\n- ... and {len(errors) - len(shown)} more" if len(errors) > len(shown) else ""
    return (f"The dossier failed validation with {len(errors)} error{'s' if len(errors) != 1 else ''}:\n"
            + "\n".join(f"- {e}" for e in shown) + more + "\n\n"
            "Fix every error: re-read the file where a pointer or path is wrong instead of guessing, and cut "
            "text to its word limit. Then output the whole corrected dossier again, alone between a line "
            f"`{BEGIN}` and a line `{END}`.")


# --- running Claude ------------------------------------------------------------------------

def _env() -> dict:
    keep = _ENV_KEEP | {k.strip() for k in os.environ.get(KEEP_ENV_VAR, "").split(",") if k.strip()}
    env = {}
    for k, v in os.environ.items():
        if k in keep or not (k.startswith("CLAUDE") or k.startswith("ANTHROPIC_")):
            env[k] = v
    return env


def argv(prompt: str, session: list[str], model: str = MODEL, effort: str = EFFORT) -> list[str]:
    return [str(CLAUDE_BIN), "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--model", model, "--effort", effort,
            "--tools", "Read,Grep,Glob,Bash",
            "--allowedTools", ",".join(ALLOWED_TOOLS),
            "--disallowedTools", ",".join(DENIED_TOOLS),
            "--permission-mode", "dontAsk",
            "--setting-sources", "project", "--strict-mcp-config",
            "--disable-slash-commands", "--no-chrome", *session]


@dataclass
class _Pass:
    result: dict | None = None
    text: str = ""
    model: str | None = None
    timed_out: bool = False
    exit: int | None = None
    api_error: str | None = None  # the `error` tag on a synthetic assistant message, e.g. "rate_limit"

    @property
    def cost(self) -> float:
        return float((self.result or {}).get("total_cost_usd") or 0)

    @property
    def is_error(self) -> bool:
        return self.result is None or bool(self.result.get("is_error")) or self.result.get("subtype") != "success"

    @property
    def rate_limited(self) -> bool:
        """Only structured signals or the text of an error result; a timed-out pass never is."""
        if self.timed_out:
            return False
        r = self.result
        if r is None:
            return self.api_error == "rate_limit"
        if r.get("api_error_status") in (429, 529):
            return True
        return bool(r.get("is_error")) and (self.api_error == "rate_limit"
                                            or bool(RATE_LIMIT.search(str(r.get("result") or ""))))

    def error_text(self) -> str:
        if self.timed_out:
            return "timed out"
        r = self.result or {}
        return (f"claude ended with {r.get('subtype') or 'no result'} (exit {self.exit})"
                + (f": {self.text.strip()[:300]}" if self.text.strip() else ""))


_LIVE: set[subprocess.Popen] = set()


def _killpg(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(10)
            return
        except subprocess.TimeoutExpired:
            continue


@atexit.register
def _kill_live() -> None:
    """Claude runs in its own process group; don't leave it running when this process exits."""
    for p in list(_LIVE):
        if p.poll() is None:
            _killpg(p)


def run_claude(args: list[str], cwd: Path, timeout: float, prog: _Progress, cancel: Cancel | None = None) -> _Pass:
    out = _Pass()
    proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True, env=_env())
    _LIVE.add(proc)
    if cancel:
        cancel.attach(proc)

    def kill():
        out.timed_out = True
        prog.line(f"hard timeout ({TIMEOUT_S // 60}-minute limit); stopping claude", step="timed out", force=True)
        _killpg(proc)

    timer = threading.Timer(max(1, timeout), kill)
    timer.daemon = True
    timer.start()

    def pump_stderr():
        for line in proc.stderr:
            prog.raw("stderr: " + line)

    th = threading.Thread(target=pump_stderr, daemon=True)
    th.start()
    last_text = ""
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                prog.raw("stdout: " + line[:500])
                continue
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                out.model = ev.get("model")
                prog.line(f"claude started ({out.model}, session {str(ev.get('session_id'))[:8]})",
                          step="reading the code", force=True)
            elif t == "result":
                out.result = ev
            elif t == "assistant":
                if ev.get("error"):
                    out.api_error = str(ev["error"])
                for c in (ev.get("message") or {}).get("content") or []:
                    if c.get("type") == "text" and c.get("text"):
                        last_text = c["text"]
            for step, text in describe(ev, cwd):
                prog.line(text, step=step or None)
    finally:
        if proc.poll() is None and not out.result:  # interrupted (Ctrl-C, exception): stop claude too
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                _killpg(proc)
        proc.wait()
        timer.cancel()
        th.join(5)
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass
        _LIVE.discard(proc)
        if cancel:
            cancel.detach(proc)
    out.exit = proc.returncode
    out.text = (out.result or {}).get("result") or last_text
    return out


# --- parsing --------------------------------------------------------------------------------

def extract(text: str) -> tuple[dict | None, str | None]:
    """The JSON between the last <<<DOSSIER line and the DOSSIER>>> line after it."""
    if not text:
        return None, f"the final message was empty; it must contain the dossier between `{BEGIN}` and `{END}`"
    starts = list(re.finditer(rf"^[ \t]*{re.escape(BEGIN)}[ \t]*$", text, re.M))
    if starts:
        body_start = starts[-1].end()
    else:
        i = text.rfind(BEGIN)
        if i < 0:
            return None, f"the final message has no `{BEGIN}` line; put the dossier between `{BEGIN}` and `{END}`"
        body_start = i + len(BEGIN)
    rest = text[body_start:]
    m = re.search(rf"^[ \t]*{re.escape(END)}[ \t]*$", rest, re.M)
    j = m.start() if m else rest.find(END)
    body = (rest[:j] if j >= 0 else rest).strip()
    body = re.sub(r"^```(?:json)?\s*\n", "", body)
    body = re.sub(r"\n```\s*$", "", body).strip()
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as e:
        return None, f"the dossier JSON does not parse: {e.msg} at line {e.lineno} column {e.colno}"
    if not isinstance(doc, dict):
        return None, "the dossier must be a JSON object"
    return doc, None


def finalize(doc: dict, entry: dict, did: str, model: str | None, elapsed_s: int, cost_usd: float) -> dict:
    """Stamp the fields the generator knows better than the model, in the spec's key order."""
    head = {
        "schema": schema.SCHEMA, "id": did, "repo": entry["full_name"], "commit": entry["commit"],
        "pinned_url": entry.get("pinned_url") or f"https://github.com/{entry['full_name']}/tree/{entry['commit']}",
        "generated_at": store.now_iso(), "model": model or "claude-opus-5-5", "effort": EFFORT,
        "elapsed_s": elapsed_s, "cost_usd": round(cost_usd, 4),
    }
    rest = {k: v for k, v in doc.items() if k not in head}
    return {**head, **rest}


# --- one generation ---------------------------------------------------------------------------

def validation_context(rec: dict) -> dict:
    """What the validator checks slot labels against: source file count and the codebase types that
    passed the Studio codebase check (empty when unknown)."""
    return {"source_files": (rec.get("counts") or {}).get("source_files"),
            "codebase_types": candidates.ok_types(rec) if rec else []}


def generate(entry: dict, did: str | None = None, on_progress: Progress | None = None,
             echo: Callable[[str], None] | None = None, timeout: int = TIMEOUT_S,
             repair_timeout: int = REPAIR_TIMEOUT_S, keep_clone: bool = False,
             cancel: Cancel | None = None) -> Result:
    """Generate and validate one dossier for a pool entry. Never raises; see Result.

    `timeout` is one deadline for the whole run (clone, first pass, repair pass)."""
    did = did or store.make_id(entry["full_name"], entry["commit"])
    rec = candidates.record(entry["full_name"]) or {}
    prog = _Progress(did, on_progress, echo)
    clone = store.P.clones / did
    sid = str(uuid.uuid4())
    res = Result()
    t0 = time.time()
    deadline = t0 + timeout
    cancel = cancel or Cancel()
    expect = {"id": did, "repo": entry["full_name"], "commit": entry["commit"]}
    ctx = validation_context(rec)

    def check(p: _Pass) -> tuple[dict | None, list[str]]:
        doc, perr = extract(p.text)
        if perr:
            return None, [perr]
        doc = finalize(doc, entry, did, res.model, round(time.time() - t0), res.cost_usd)
        prog.line("validating", step="validating", force=True)
        return doc, schema.validate(doc, clone, expect, context=ctx)

    def was_cancelled() -> bool:
        if cancel.cancelled:
            res.cancelled, res.error = True, cancel.reason or "stopped"
            prog.line(f"stopped: {res.error}", step="stopped", force=True)
            return True
        return False

    def stopped(p: _Pass) -> bool:
        res.cost_usd += p.cost
        res.model = res.model or p.model
        if was_cancelled():
            return True
        if p.timed_out:  # before the usage-limit check: a killed pass's text says nothing about limits
            res.error = p.error_text()
            return True
        if p.rate_limited:
            res.retryable, res.error = True, "hit the Claude usage limit; requeued"
            return True
        if p.is_error:
            res.error = p.error_text()
            return True
        return False

    try:
        if was_cancelled():
            return res
        prog.line(f"cloning {entry['full_name']} at {entry['commit'][:8]}", step="cloning", force=True)
        clone_repo(entry.get("display_name") or rec.get("display_name") or entry["full_name"], entry["commit"], clone,
                   deadline=deadline, cancel=cancel)
        if was_cancelled():
            return res
        prog.line("clone ready; starting claude", step="starting claude", force=True)
        p1 = run_claude(argv(build_prompt(entry, rec, did), ["--session-id", sid]), clone,
                        _left(deadline, timeout), prog, cancel)
        res.passes = 1
        if stopped(p1):
            return res
        doc, errors = check(p1)
        if errors:
            left = deadline - time.time()
            if left < MIN_REPAIR_S:
                res.errors = errors
                res.error = f"timed out: {len(errors)} validation errors and no time left for the repair pass"
                return res
            prog.line(f"{len(errors)} validation error{'s' if len(errors) != 1 else ''}; one repair pass",
                      step="repairing", force=True)
            for e in errors:
                prog.raw("  invalid: " + e)
            p2 = run_claude(argv(repair_prompt(errors), ["--resume", sid]), clone, min(repair_timeout, left),
                            prog, cancel)
            res.passes = 2
            if stopped(p2):
                res.errors = errors
                return res
            doc, errors = check(p2)
            if errors:
                for e in errors:
                    prog.raw("  still invalid: " + e)
                res.errors = errors
                res.error = f"failed validation after the repair pass ({len(errors)} errors)"
                return res
        res.ok, res.doc = True, doc
        return res
    except HardTimeout as e:
        if not was_cancelled():
            res.error = str(e)
        return res
    except Exception as e:  # clone failures, missing binary, ...
        if not was_cancelled():
            res.error = f"{type(e).__name__}: {e}"
        return res
    finally:
        res.elapsed_s = round(time.time() - t0)
        if res.doc:
            res.doc["elapsed_s"] = res.elapsed_s
            res.doc["cost_usd"] = round(res.cost_usd, 4)
        if not keep_clone:
            cleanup(clone, sid)
        outcome = ("ready" if res.ok else f"stopped: {res.error}" if res.cancelled
                   else f"requeued: {res.error}" if res.retryable else f"failed: {res.error}")
        prog.line(outcome + f" in {res.elapsed_s}s, ${res.cost_usd:.2f}",
                  step="done" if res.ok else "stopped" if res.cancelled else "failed", force=True)
        prog.close()
