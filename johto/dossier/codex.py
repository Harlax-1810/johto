"""One dossier generation by GPT through the Codex CLI (dossier_model "codex"), the counterpart of generate.py.

`codex exec` runs headless inside the clone with a read-only sandbox, so GPT reads the code itself the way
Claude does, with the same prompt and schema. The sandbox has no network, so the open issues are listed in
the prompt instead of read with `gh`. The dossier is the JSON between the <<<DOSSIER and DOSSIER>>> lines of
the final message (read from --output-last-message), validated against the clone; a failed validation gets
one repair run, a fresh `codex exec` carrying the previous dossier and the errors (nothing here depends on
Codex's resume command, whose shape differs between versions). Progress comes from the --json event stream
when Codex prints one; both the current item events and the older msg events are understood.

The binary is config.CODEX_BIN (JOHTO_CODEX_BIN, `codex` on PATH); the model and reasoning effort come from the
codex_model and codex_effort settings, else from Codex's own config.toml (the doc records the same). Codex bills
the ChatGPT plan, not an API key, so cost is recorded as 0. Checked against codex-cli 0.167.0's `exec --json`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import config, settings
from . import candidates, schema, store
from .generate import (BEGIN, END, RATE_LIMIT, Cancel, HardTimeout, Result, _Progress, _env, _killpg, _LIVE,
                       cleanup, clone_repo, extract, finalize, focus_section, output_rules, repair_prompt,
                       repo_facts, validation_context)
from .prompt import PROMPT, SCHEMA

MODEL_FALLBACK = "gpt-via-codex"   # the doc's model when neither the settings nor Codex's config name one
EFFORT_FALLBACK = "high"
TIMEOUT_S = 75 * 60
REPAIR_TIMEOUT_S = 30 * 60
MIN_REPAIR_S = 2 * 60
GH_TIMEOUT_S = 40


def codex_bin() -> str:
    return str(config.CODEX_BIN)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def config_value(key: str) -> str:
    """A top-level string value from Codex's own config.toml ("" when absent)."""
    try:
        text = (codex_home() / "config.toml").read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("["):
            break  # top-level keys come before the first table
        m = re.match(rf'^\s*{re.escape(key)}\s*=\s*"([^"]*)"', line)
        if m:
            return m.group(1)
    return ""


def model_in_use() -> str:
    return settings.get("codex_model") or config_value("model") or MODEL_FALLBACK


def effort_in_use() -> str:
    return settings.get("codex_effort") or config_value("model_reasoning_effort") or EFFORT_FALLBACK


# --- upstream issues (the sandbox has no network, so they go into the prompt) ---------------------

def _gh(args: list[str]) -> str | None:
    """gh's stdout, or None when it isn't there, isn't logged in or fails. Never in fake mode."""
    if config.FAKE:
        return None
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=GH_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def issue_list(full_name: str, limit: int = 40) -> str:
    out = _gh(["issue", "list", "--repo", full_name, "--state", "open", "--limit", str(limit),
               "--json", "number,title,labels,comments,updatedAt"])
    try:
        rows = json.loads(out) if out else []
    except ValueError:
        rows = []
    lines = []
    for it in rows:
        labels = ", ".join(l.get("name", "") for l in it.get("labels") or [] if isinstance(l, dict))
        n = len(it.get("comments") or [])
        lines.append(f"#{it.get('number')} {it.get('title')}" + (f" [{labels}]" if labels else "")
                     + (f" ({n} comments)" if n else ""))
    return "\n".join(lines)


def argv(clone: Path, last_message: Path, model: str | None = None, effort: str | None = None) -> list[str]:
    """`codex exec` reading the prompt from stdin, read-only in the clone, JSON events on stdout."""
    model = model if model is not None else (settings.get("codex_model") or "")
    effort = effort if effort is not None else (settings.get("codex_effort") or "")
    args = [codex_bin(), "exec", "--sandbox", "read-only", "-C", str(clone), "--json",
            "--output-last-message", str(last_message)]
    if model:
        args += ["-m", model]
    if effort:
        args += ["-c", f'model_reasoning_effort="{effort}"']
    return args + ["-"]


def codex_output_rules(entry: dict, did: str, rec: dict | None = None) -> str:
    rules = output_rules(entry, did, rec).split("\n")
    for i, l in enumerate(rules):
        if l.startswith("- Tools:"):
            rules[i] = ("- Tools: read any file and run read-only shell commands (git log, git show, ls, wc, find, rg). "
                        "The sandbox has no network: the open issues are listed above, and nothing can be edited. "
                        "Do not try to install, build or run the project.")
    return "\n".join(rules)


def build_prompt(entry: dict, rec: dict, did: str, issues: str = "") -> str:
    focus = focus_section(entry)
    facts = repo_facts(entry, rec, did).replace(
        " `gh` resolves to the upstream repository.", " There is no network; the open issues are listed below.")
    return "\n\n".join([
        PROMPT,
        "## The dossier schema (johto.dossier/1)\n\n" + SCHEMA,
        "## Repository facts\n\n" + facts,
        *(["## Open issues upstream\n\n```\n" + issues + "\n```"] if issues else []),
        *([focus] if focus else []),
        "## Output\n\n" + codex_output_rules(entry, did, rec),
    ])


def repair_run_prompt(first: str, previous: dict, errors: list[str]) -> str:
    """A fresh run's prompt: the whole task again, the previous dossier, and what was wrong with it."""
    return "\n\n".join([
        first,
        "## Your previous attempt\n\n" + BEGIN + "\n" + json.dumps(previous, ensure_ascii=False) + "\n" + END,
        repair_prompt(errors),
    ])


# --- running Codex ------------------------------------------------------------------------------

@dataclass
class _Run:
    text: str = ""
    model: str | None = None
    exit: int | None = None
    timed_out: bool = False
    error: str | None = None       # an error event's message
    stderr_tail: str = ""

    @property
    def rate_limited(self) -> bool:
        if self.timed_out:
            return False
        return bool(RATE_LIMIT.search(self.error or "")) or (self.exit not in (0, None)
                                                             and bool(RATE_LIMIT.search(self.stderr_tail)))

    def error_text(self) -> str:
        if self.timed_out:
            return "timed out"
        tail = self.stderr_tail.strip().splitlines()
        why = self.error or (tail[-1] if tail else "")
        return f"codex ended with exit {self.exit}" + (f": {why[:300]}" if why else "")


def _event(ev: dict) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """(step, log line, agent text, model, error) from one Codex JSON event, in either format."""
    msg = ev.get("msg") if isinstance(ev.get("msg"), dict) else None
    t = (msg or ev).get("type") or ""
    item = ev.get("item") if isinstance(ev.get("item"), dict) else None
    if item is not None:
        it = item.get("type") or ""
        if it == "command_execution" and t in ("item.started", "item.completed"):
            cmd = item.get("command")
            cmd = " ".join(cmd) if isinstance(cmd, list) else " ".join(str(cmd or "").split())
            step = "reading history" if cmd.startswith("git ") else "listing files" if cmd.split(" ")[0] in ("ls", "find", "wc") \
                else "searching the code" if cmd.split(" ")[0] in ("rg", "grep") else "reading the code"
            return (step, f"$ {cmd[:150]}", None, None, None) if t == "item.started" else (None, None, None, None, None)
        if it == "agent_message" and t == "item.completed":
            text = item.get("text") or ""
            return (("writing the dossier", "dossier written") if BEGIN in text else (None, None)) + (text, None, None)
        if it == "error":
            return None, None, None, None, item.get("message") or "codex reported an error"
        return None, None, None, None, None
    if t == "thread.started":
        return "reading the code", f"codex started (thread {str(ev.get('thread_id'))[:8]})", None, ev.get("model"), None
    if t in ("error", "turn.failed"):
        err = ev.get("error") if isinstance(ev.get("error"), dict) else None
        return None, None, None, None, (err or {}).get("message") or ev.get("message") or "codex failed"
    if msg is not None:
        if t == "session_configured":
            return "reading the code", f"codex started ({msg.get('model')})", None, msg.get("model"), None
        if t == "exec_command_begin":
            cmd = msg.get("command")
            cmd = " ".join(cmd) if isinstance(cmd, list) else str(cmd or "")
            return "reading the code", f"$ {cmd[:150]}", None, None, None
        if t == "agent_message":
            text = msg.get("message") or ""
            return (("writing the dossier", "dossier written") if BEGIN in text else (None, None)) + (text, None, None)
        if t == "task_complete":
            return None, None, msg.get("last_agent_message") or None, None, None
        if t == "error":
            return None, None, None, None, msg.get("message") or "codex reported an error"
    return None, None, None, None, None


def run_codex(args: list[str], prompt: str, cwd: Path, timeout: float, prog: _Progress,
              cancel: Cancel | None = None, last_message: Path | None = None) -> _Run:
    out = _Run()
    proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1, start_new_session=True, env=_env())
    _LIVE.add(proc)
    if cancel:
        cancel.attach(proc)

    def kill():
        out.timed_out = True
        prog.line(f"hard timeout ({round(timeout / 60)}-minute limit); stopping codex", step="timed out", force=True)
        _killpg(proc)

    timer = threading.Timer(max(1, timeout), kill)
    timer.daemon = True
    timer.start()
    err_lines: list[str] = []

    def pump_stderr():
        for line in proc.stderr:
            err_lines.append(line)
            prog.raw("stderr: " + line)

    th = threading.Thread(target=pump_stderr, daemon=True)
    th.start()

    def feed():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    threading.Thread(target=feed, daemon=True).start()
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
            if not isinstance(ev, dict):
                continue
            step, text, agent, model, error = _event(ev)
            if model:
                out.model = model
            if error:
                out.error = error
                prog.raw("error: " + error)
            if agent:
                last_text = agent
            if text:
                prog.line(text, step=step or None, force=bool(step and step != prog.step))
    finally:
        if proc.poll() is None:
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
    out.stderr_tail = "".join(err_lines)[-2000:]
    final = ""
    if last_message is not None:
        try:
            final = last_message.read_text(encoding="utf-8", errors="replace")
        except OSError:
            final = ""
    out.text = final if END in final or not last_text else last_text
    return out


# --- one generation -----------------------------------------------------------------------------

def generate(entry: dict, did: str | None = None, on_progress: Callable[[dict], None] | None = None,
             echo: Callable[[str], None] | None = None, timeout: int = TIMEOUT_S,
             repair_timeout: int = REPAIR_TIMEOUT_S, keep_clone: bool = False,
             cancel: Cancel | None = None) -> Result:
    """Generate and validate one dossier with GPT in the Codex CLI. Same contract as generate.generate."""
    did = did or store.make_id(entry["full_name"], entry["commit"])
    rec = candidates.record(entry["full_name"]) or {}
    prog = _Progress(did, on_progress, echo)
    clone = store.P.clones / did
    res = Result()
    t0 = time.time()
    deadline = t0 + timeout
    cancel = cancel or Cancel()
    expect = {"id": did, "repo": entry["full_name"], "commit": entry["commit"]}
    ctx = validation_context(rec)
    effort = effort_in_use()
    outdir = Path(tempfile.mkdtemp(prefix="johto-codex-"))

    def model_name(run: _Run) -> str:
        return run.model or model_in_use()

    def check(run: _Run) -> tuple[dict | None, list[str]]:
        doc, perr = extract(run.text)
        if perr:
            return None, [perr]
        doc = finalize(doc, entry, did, model_name(run), round(time.time() - t0), 0.0, effort=effort)
        prog.line("validating", step="validating", force=True)
        return doc, schema.validate(doc, clone, expect, context=ctx)

    def was_cancelled() -> bool:
        if cancel.cancelled:
            res.cancelled, res.error = True, cancel.reason or "stopped"
            prog.line(f"stopped: {res.error}", step="stopped", force=True)
            return True
        return False

    def stopped(run: _Run) -> bool:
        res.model = res.model or model_name(run)
        if was_cancelled():
            return True
        if run.timed_out:
            res.error = run.error_text()
            return True
        if run.rate_limited:
            res.retryable, res.error = True, "hit the ChatGPT usage limit in Codex; requeued"
            return True
        if run.exit not in (0, None) and END not in run.text:
            res.error = run.error_text()
            return True
        return False

    def left(cap: float) -> float:
        l = deadline - time.time()
        if l <= 0:
            raise HardTimeout("timed out")
        return min(cap, l)

    try:
        if was_cancelled():
            return res
        prog.line(f"cloning {entry['full_name']} at {entry['commit'][:8]}", step="cloning", force=True)
        clone_repo(entry.get("display_name") or rec.get("display_name") or entry["full_name"], entry["commit"], clone,
                   deadline=deadline, cancel=cancel)
        if was_cancelled():
            return res
        prog.line("clone ready; reading the open issues", step="starting codex", force=True)
        issues = issue_list(entry["full_name"])
        first = build_prompt(entry, rec, did, issues)
        prog.line("starting codex", step="starting codex", force=True)
        r1 = run_codex(argv(clone, outdir / "pass1.md"), first, clone, left(timeout), prog, cancel,
                       last_message=outdir / "pass1.md")
        res.passes = 1
        if stopped(r1):
            return res
        doc, errors = check(r1)
        if errors:
            l = deadline - time.time()
            if l < MIN_REPAIR_S:
                res.errors = errors
                res.error = f"timed out: {len(errors)} validation errors and no time left for the repair run"
                return res
            prog.line(f"{len(errors)} validation error{'s' if len(errors) != 1 else ''}; one repair run",
                      step="repairing", force=True)
            for e in errors:
                prog.raw("  invalid: " + e)
            prev, _ = extract(r1.text)
            r2 = run_codex(argv(clone, outdir / "pass2.md"), repair_run_prompt(first, prev or {}, errors), clone,
                           min(repair_timeout, l), prog, cancel, last_message=outdir / "pass2.md")
            res.passes = 2
            if stopped(r2):
                res.errors = errors
                return res
            doc, errors = check(r2)
            if errors:
                for e in errors:
                    prog.raw("  still invalid: " + e)
                res.errors = errors
                res.error = f"failed validation after the repair run ({len(errors)} errors)"
                return res
        res.ok, res.doc = True, doc
        return res
    except HardTimeout as e:
        if not was_cancelled():
            res.error = str(e)
        return res
    except Exception as e:  # clone failures, a missing binary, ...
        if not was_cancelled():
            res.error = f"{type(e).__name__}: {e}"
        return res
    finally:
        res.elapsed_s = round(time.time() - t0)
        if res.doc:
            res.doc["elapsed_s"] = res.elapsed_s
            res.doc["cost_usd"] = 0.0
        if not keep_clone:
            cleanup(clone, None)
        for f in outdir.glob("*"):
            f.unlink(missing_ok=True)
        try:
            outdir.rmdir()
        except OSError:
            pass
        outcome = ("ready" if res.ok else f"stopped: {res.error}" if res.cancelled
                   else f"requeued: {res.error}" if res.retryable else f"failed: {res.error}")
        prog.line(outcome + f" in {res.elapsed_s}s", step="done" if res.ok else "stopped" if res.cancelled else "failed",
                  force=True)
        prog.close()
