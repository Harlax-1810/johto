"""Saved outside reviews of lane runs (GPT Pro replies), and sending review packs to ChatGPT.

Austin sends each lane turn's review pack to GPT Pro. He used to paste the long reply into a helper chat, where it
vanished when the chat compacted and the overlap guard couldn't see it. Replies now live in the task folder:

- tasks/<t>/reviews/<run>-t<NN>-<source>.md: one reply, after a small front-matter block (run, turn, source,
  saved_at, pack, chat_url, sha). The same text (ignoring whitespace) is never saved twice.
- reviews/chats.json: run -> ChatGPT conversation, so follow-up packs for a run go to the same chat.
- reviews/pending.json: sends whose reply isn't saved yet, each with a state. "sending" is written before
  johto.chatgpt starts (pack, title, the chat once known); "sent" once ChatGPT took it; "unconfirmed" when
  Send was clicked but didn't show working. `settle` decides "sending" entries whose process is gone and
  "unconfirmed" ones by reading the chat (chatgpt.inspect): the pack is there, so wait on the reply, or it
  isn't, so clear the entry and let it go again. `johto review fetch` (and the turn watcher) saves each reply
  once ChatGPT has finished it; an entry with no reply after PENDING_TTL_S is dropped (expire_pending).

`send_review` picks or builds the next pack (delta.next_pack) and hands it to johto.chatgpt, which drives the
ChatGPT tab. It never sends a pack twice, a second pack while the run's last review is due, or a PARTIAL pack
(unless forced); its result says why with "why". `findings` reads the ```findings block a reply ends with, and
`last_findings` merges a run's findings by ID for the next follow-up pack.

A saved review is AI research under Johto's LLM policy: Austin reads it, and nothing here writes or suggests
wording for his fields.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import config, delta, settings, stages, transcript
from . import pack as packmod

DIR = "reviews"
CHATS = "chats.json"
PENDING = "pending.json"
FIELDS = ("run", "turn", "source", "saved_at", "pack", "chat_url", "sha", "message_id")
PENDING_TTL_S = 3 * 3600      # a send with no reply saved after this long is dropped, with a notice
SENDING_STALE_S = 45 * 60     # a "sending" entry this old is settled even if its process still runs


class ChatGPTUnavailable(RuntimeError):
    """johto.chatgpt can't be loaded here, so nothing can be sent or fetched."""


# --- Storage -------------------------------------------------------------------------------

def reviews_dir(task_dir: Path) -> Path:
    return Path(task_dir) / DIR


@contextlib.contextmanager
def _locked(task_dir: Path):
    """Saves, pending.json and chats.json change from the CLI, the web app and the turn watcher: one at a time."""
    d = reviews_dir(task_dir)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, ValueError):
        return default


def _write(p: Path, text: str) -> None:
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, p)


def _sha(text: str) -> str:
    """Whitespace-normalized, so a reply copied from the page and the same reply fetched as markdown match."""
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()[:12]


def _rel(task_dir: Path, p) -> str:
    try:
        return str(Path(p).resolve().relative_to(Path(task_dir).resolve()))
    except ValueError:
        return str(p)


def _front(fm: dict) -> str:
    lines = ["---"] + [f"{k}: {'' if fm.get(k) is None else ' '.join(str(fm[k]).split())}" for k in FIELDS] + ["---", ""]
    return "\n".join(lines) + "\n"


def _parse(path: Path) -> tuple[dict, str]:
    text = Path(path).read_text(errors="replace")
    fm: dict = {}
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            for line in text[4:end].splitlines():
                k, _, v = line.partition(":")
                fm[k.strip()] = v.strip() or None
            text = text[end + 5:]
    if fm.get("turn") and str(fm["turn"]).isdigit():
        fm["turn"] = int(fm["turn"])
    return fm, text.lstrip("\n")


def body(path: Path) -> str:
    """A saved review's text without its front matter."""
    return _parse(path)[1]


def _first_line(text: str) -> str:
    for line in text.splitlines():
        t = line.strip().lstrip("#").strip().strip("*").strip()
        if t:
            return t
    return ""


def list_reviews(task_dir: Path) -> list[dict]:
    """Every saved review, oldest first: front matter plus name, path, chars and first_line."""
    out = []
    for p in reviews_dir(task_dir).glob("*.md"):
        fm, text = _parse(p)
        out.append({**{k: fm.get(k) for k in FIELDS}, "name": p.name, "path": str(p), "chars": len(text),
                    "first_line": _first_line(text), "_mtime": p.stat().st_mtime_ns})
    out.sort(key=lambda r: (r.get("saved_at") or "", r["_mtime"]))   # saved_at is to the second
    for r in out:
        del r["_mtime"]
    return out


_DIGEST = re.compile(r"^#{1,3}\s*Digest\s*$(.*?)(?=^#{1,3}\s|^```|\Z)", re.M | re.S | re.I)


def digest(text: str) -> str | None:
    """The reply's "## Digest" section (GPT's short summary, research only), or None for an older reply without one."""
    m = _DIGEST.search(text or "")
    return m.group(1).strip() or None if m else None


def read(task_dir: Path, name: str) -> dict | None:
    """One saved review by its file name (as list_reviews gives it): front matter, digest and full text.
    None when no saved review has that name (a name from the request never becomes a path by itself)."""
    row = next((r for r in list_reviews(task_dir) if r["name"] in (name, f"{name}.md")), None)
    if row is None:
        return None
    text = body(Path(row["path"]))
    return {**{k: v for k, v in row.items() if k != "path"}, "digest": digest(text), "text": text}


def latest(task_dir: Path, run: str | None = None) -> dict | None:
    rows = [r for r in list_reviews(task_dir) if run is None or r.get("run") == run]
    return rows[-1] if rows else None


def save(task_dir: Path, run: str, turn: int | None, text: str, source: str = "gptpro", pack=None,
         chat_url: str | None = None, *, message_id: str | None = None) -> Path:
    """Save one review. Text already saved (ignoring whitespace) returns the existing file instead."""
    text = (text or "").strip()
    if not text:
        raise ValueError("an empty review")
    sha = _sha(text)
    d = reviews_dir(task_dir)
    with _locked(task_dir):
        same = next((r for r in list_reviews(task_dir) if r.get("sha") == sha), None)
        if same:
            return Path(same["path"])
        slug = re.sub(r"[^a-z0-9]+", "-", (source or "review").lower()).strip("-") or "review"
        stem = f"{run}-t{int(turn or 0):02d}-{slug}"
        path, n = d / f"{stem}.md", 2
        while path.exists():
            path, n = d / f"{stem}-{n}.md", n + 1
        fm = {"run": run, "turn": turn, "source": source, "saved_at": stages.now_iso(),
              "pack": _rel(task_dir, pack) if pack else None, "chat_url": chat_url, "sha": sha, "message_id": message_id}
        _write(path, _front(fm) + text + "\n")
    stages.log(task_dir, "review_saved", run=run, turn=turn, source=source, path=_rel(task_dir, path))
    _refresh_grading(task_dir)
    return path


def _refresh_grading(task_dir: Path) -> None:
    """Put a saved review's findings in the grading table now (local sources only, no Claude call), so the web
    views, the card and the kit show them without a Refresh. A failure never stops the save."""
    try:
        from . import grading
        grading.refresh(Path(task_dir), log=lambda *a: None)
    except Exception:
        pass


# --- Chats and in-flight sends ------------------------------------------------------------------

def chat_for(task_dir: Path, run: str) -> str | None:
    return _read_json(reviews_dir(task_dir) / CHATS, {}).get(run)


def set_chat(task_dir: Path, run: str, chat_url: str) -> None:
    with _locked(task_dir):
        p = reviews_dir(task_dir) / CHATS
        chats = _read_json(p, {})
        chats[run] = chat_url
        _write(p, json.dumps(chats, indent=1))


def pending(task_dir: Path) -> list[dict]:
    """Packs sent to ChatGPT whose reply isn't saved yet: [{run, turn, pack, chat_url, sent_at, title, ...}]."""
    rows = _read_json(reviews_dir(task_dir) / PENDING, [])
    return rows if isinstance(rows, list) else []


def _set_pending(task_dir: Path, change) -> None:
    with _locked(task_dir):
        p = reviews_dir(task_dir) / PENDING
        rows = _read_json(p, [])
        _write(p, json.dumps(change(rows if isinstance(rows, list) else []), indent=1))


def add_pending(task_dir: Path, entry: dict) -> None:
    """Record a send; one for the same pack replaces it."""
    _set_pending(task_dir, lambda rows: [r for r in rows if r.get("pack") != entry.get("pack")] + [dict(entry)])


def state_of(entry: dict) -> str:
    """"sending", "sent" or "unconfirmed" (entries written before states existed are "sent" or "unconfirmed")."""
    return entry.get("state") or ("unconfirmed" if entry.get("unconfirmed") else "sent")


def _same(a: dict, b: dict) -> bool:
    if a.get("id") or b.get("id"):
        return a.get("id") == b.get("id")
    return a.get("pack") == b.get("pack") and a.get("chat_url") == b.get("chat_url")


def _drop_pending(task_dir: Path, entry: dict) -> None:
    _set_pending(task_dir, lambda rows: [r for r in rows if not _same(r, entry)])


def _update(task_dir: Path, entry: dict, fields: dict, drop: tuple = ()) -> None:
    def change(rows):
        for r in rows:
            if _same(r, entry):
                r.update(fields)
                for k in drop:
                    r.pop(k, None)
        return rows
    _set_pending(task_dir, change)


def _mark_pending(task_dir: Path, entry: dict, **fields) -> None:
    _update(task_dir, entry, fields)


def resolve_pending(task_dir: Path, chat_url: str, text: str, *, message_id: str | None = None) -> Path:
    """Save the reply to the oldest send waiting on that chat and take it off pending.json."""
    entry = next((e for e in pending(task_dir) if e.get("chat_url") == chat_url and state_of(e) != "sending"), None)
    if entry is None:
        raise ValueError(f"no review is waiting on {chat_url}")
    path = save(task_dir, entry["run"], entry.get("turn"), text, source=entry.get("source") or "gptpro",
                pack=Path(task_dir) / entry["pack"] if entry.get("pack") else None, chat_url=chat_url,
                message_id=message_id)
    _drop_pending(task_dir, entry)
    return path


# --- Defaults -------------------------------------------------------------------------------------

def _runs(task_dir: Path) -> list[str]:
    return sorted(p.name for p in (Path(task_dir) / "runs").glob("*-*") if p.is_dir())


def _default_run(task_dir: Path) -> str:
    """The run a review most likely answers: the newest send waiting, else the run of the newest pack, else the
    newest run."""
    waiting = sorted(pending(task_dir), key=lambda e: e.get("sent_at") or "")
    if waiting:
        return waiting[-1]["run"]
    newest = [(delta._when(r.get("built_at")), run) for run in _runs(task_dir) for r in delta.packs(task_dir, run)]
    if newest:
        return max(newest)[1]
    return packmod._pick_run(Path(task_dir), None).name


def _transcript_turn(task_dir: Path, run: str) -> int | None:
    """The latest of Austin's counted prompts in the run's transcript (collected, else the live one)."""
    run_dir = Path(task_dir) / "runs" / run
    logs = transcript.find(run_dir)
    tpath = logs[-1] if logs else packmod._live_transcript(run_dir)
    if not tpath:
        return None
    return delta.state(tpath, "")["turn"] or None


def _pack_for(task_dir: Path, run: str, turn: int | None) -> dict | None:
    """The pack a review of that turn answers: the newest one covering it, else the newest pack."""
    recs = delta.packs(task_dir, run)
    if turn is not None:
        hit = [r for r in recs if r.get("turn") == turn]
        if hit:
            return hit[-1]
    return recs[-1] if recs else None


def _clipboard() -> str:
    try:
        return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        raise SystemExit("Couldn't read the clipboard (pbpaste). Use --file.")


def add(task_dir: Path, text: str | None = None, file=None, run: str | None = None, turn: int | None = None,
        source: str = "gptpro") -> Path:
    """Save a review Austin copied (the clipboard by default; `file`, or "-" for stdin). The run and turn default
    to the send waiting on a reply, else the newest pack, else the transcript."""
    task_dir = Path(task_dir)
    if text is None:
        if file == "-":
            text = sys.stdin.read()
        elif file:
            text = Path(file).read_text(errors="replace")
        else:
            text = _clipboard()
    if not (text or "").strip():
        raise SystemExit("Nothing to save: " + ("the file is empty." if file else "the clipboard is empty."))
    if "This file is a merged representation" in text[:3000]:
        raise SystemExit("That's a review pack, not a reply. Copy the reviewer's answer and try again.")
    run = run or _default_run(task_dir)
    if source == "gptpro":
        wait = [e for e in pending(task_dir) if e.get("run") == run and (turn is None or e.get("turn") == turn)]
        if wait:
            e = sorted(wait, key=lambda e: e.get("sent_at") or "")[-1]
            path = save(task_dir, run, e.get("turn"), text, source, pack=task_dir / e["pack"] if e.get("pack") else None,
                        chat_url=e.get("chat_url"))
            _drop_pending(task_dir, e)
            return path
    rec = _pack_for(task_dir, run, turn)
    if turn is None:
        turn = rec["turn"] if rec and rec.get("turn") else _transcript_turn(task_dir, run)
    return save(task_dir, run, turn, text, source, pack=rec["path"] if rec else None, chat_url=chat_for(task_dir, run))


# --- ChatGPT -----------------------------------------------------------------------------------

def _chatgpt():
    """johto.chatgpt, looked up at call time (tests put a stub in sys.modules)."""
    try:
        return importlib.import_module("johto.chatgpt")
    except ImportError as e:
        raise ChatGPTUnavailable(f"ChatGPT automation isn't available ({e}).") from None


def _sent_before(task_dir: Path, run: str, pack: Path, rec: dict | None) -> str | None:
    """Why this pack can't be sent again, or None."""
    rel, name = _rel(task_dir, pack), Path(pack).name
    if any(e.get("pack") == rel for e in pending(task_dir)):
        return f"{name} was sent and its review isn't back yet"
    if any(Path(r.get("pack") or "").name == name for r in list_reviews(task_dir) if r.get("run") == run):
        return f"{name} already has a saved review"
    if rec and rec.get("sent_at"):
        return f"{name} was already sent ({rec['sent_at'][:16].replace('T', ' ')})"
    if rec and rec.get("last_entry_at"):
        # Another pack of the same checkpoint (same transcript reach, same diff) already went or was answered.
        key = (rec.get("transcript_entries"), rec.get("last_entry_at"), rec.get("diff_sha"))
        seen = {Path(r.get("pack") or "").name for r in list_reviews(task_dir) if r.get("run") == run}
        for o in delta.packs(task_dir, run):
            other = Path(o.get("path") or "").name
            if other != name and (o.get("transcript_entries"), o.get("last_entry_at"), o.get("diff_sha")) == key \
                    and (o.get("sent_at") or other in seen):
                return f"the same checkpoint went as {other}"
    return None


def _titles(task_dir: Path, run: str, turn) -> tuple[str, str]:
    """(chat title, tag): the tag is the short message ChatGPT echoes and the pack replaces; a new chat is then
    named with the title."""
    t = turn if turn is not None else "?"
    try:
        repo = (json.loads((Path(task_dir) / "task.json").read_text()).get("repo") or {}).get("full_name")
    except (OSError, ValueError, AttributeError):
        repo = None
    title = f"Johto review · {Path(task_dir).name} {run} turn {t}" + (f" · {repo}" if repo else "")
    return title, f"Johto {Path(task_dir).name} {run} t{t}"


def _age_s(iso: str | None) -> float:
    when = delta._when(iso)
    return time.time() - when if when else 0.0


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def _try_lock(p: Path):
    """An open file holding an exclusive lock on p, or None when someone else holds it."""
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = open(p, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _prepare_send(task_dir: Path, run: str, pack, no_send: bool, log, force: bool) -> dict:
    """Under the run's send lock: settle, the busy check, the pack, and the "sending" entry in pending.json.
    Returns a refusal ({"sent": False, "why", ...}) or what the send needs."""
    settle(task_dir, run, log=log)
    chat = chat_for(task_dir, run)
    busy = [e for e in pending(task_dir) if e.get("run") == run]
    if busy and not force:
        e = busy[-1]
        what = ("is being sent now" if state_of(e) == "sending" else
                f"(sent {(e.get('sent_at') or '')[11:16]}) isn't back yet")
        return {"sent": False, "why": "busy", "reason": f"the review of {Path(e['pack']).name} {what}",
                "pack": str(task_dir / e["pack"]), "chat_url": e.get("chat_url")}
    if pack is None:
        pack = delta.next_pack(task_dir, run, log=log, full=chat is None)
    pack = Path(pack)
    rec = delta.pack_record(task_dir, run, pack)
    if not force:
        why = _sent_before(task_dir, run, pack, rec)
        if why:
            return {"sent": False, "why": "nothing_new", "reason": f"nothing new: {why}", "pack": str(pack),
                    "chat_url": chat}
        if rec and rec.get("partial"):
            detail = str(rec["partial"]).removeprefix("PARTIAL: ")
            return {"sent": False, "why": "partial", "pack": str(pack), "chat_url": chat,
                    "reason": f"{pack.name} is partial ({detail}); check it, then send it with --force"}
    kind, turn = (rec or {}).get("kind", "full"), (rec or {}).get("turn")
    chat_url = chat if kind == "followup" else None
    if kind == "followup" and not chat:
        log("No ChatGPT conversation is on record for this run, so the follow-up starts a new one.")
    title, tag = _titles(task_dir, run, turn)
    cg = _chatgpt()
    text = pack.read_text(errors="replace")
    now = stages.now_iso()
    entry = {"id": uuid.uuid4().hex[:12], "run": run, "turn": turn, "kind": kind, "pack": _rel(task_dir, pack),
             "chat_url": chat_url, "sent_at": now, "started_at": now, "title": title, "tag": tag,
             "state": "sending", "pid": os.getpid()}
    sha = getattr(cg, "text_sha", None)
    if callable(sha):
        entry["pack_sha"] = sha(text)
    if not no_send:
        add_pending(task_dir, entry)
    return {"entry": entry, "pack": pack, "text": text, "kind": kind, "turn": turn, "title": title, "tag": tag,
            "chat_url": chat_url, "cg": cg}


def send_review(task_dir: Path, run: str | None = None, pack=None, no_send: bool = False, log=print, *,
                force: bool = False) -> dict:
    """Send the run's next pack to GPT Pro: a follow-up into the run's chat when it has one, else a full pack into
    a new chat. Sends whose outcome isn't known are settled first (settle). Records the send in pending.json
    before ChatGPT starts, then chats.json and packs.json once it went, and logs review_sent.

    Returns {"sent", "why", "reason", "pack", "chat_url", ...}. why is None when it went (or with no_send),
    "busy" when the run's last review isn't back (or a send is under way), "nothing_new" when this pack went
    already or has a saved review, and "partial" when the pack's checkout missed some of the diff. `force`
    skips those checks. Raises ChatGPTUnavailable when johto.chatgpt can't load, and chatgpt.ChatGPTError when
    the send fails."""
    task_dir = Path(task_dir)
    run = run or packmod._pick_run(task_dir, None).name
    # The busy check, the pack build and the "sending" entry are one step per run: a second sender (the turn
    # watcher and `johto review send` at once) is told busy instead of sending the same checkpoint again.
    lock = _try_lock(reviews_dir(task_dir) / f".send-{run}.lock")
    if lock is None:
        return {"sent": False, "why": "busy", "reason": f"another send of {run} is being prepared now",
                "pack": str(pack) if pack else None, "chat_url": chat_for(task_dir, run)}
    try:
        prep = _prepare_send(task_dir, run, pack, no_send, log, force)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    if "sent" in prep:   # refused: busy, nothing new or partial
        return prep
    entry, pack, text, kind, turn, title, tag, chat_url, cg = (
        prep[k] for k in ("entry", "pack", "text", "kind", "turn", "title", "tag", "chat_url", "cg"))

    def on_chat(url: str) -> None:
        # The tag message is answered: an interrupted send from here on may have reached this chat.
        if not no_send and url and url != entry["chat_url"]:
            entry["chat_url"] = url
            _update(task_dir, entry, {"chat_url": url})

    try:
        res = cg.send(text, title, chat_url=chat_url, account=settings.get("chatgpt_account"), no_send=no_send,
                      log=log, tag=tag, on_chat=on_chat)
    except Exception as e:
        if not no_send:
            url = getattr(e, "chat_url", None) or entry["chat_url"]
            if getattr(e, "maybe_sent", False) and url:
                # Send was clicked but didn't show working: settle() reads the chat before any retry.
                _update(task_dir, entry, {"state": "unconfirmed", "chat_url": url, "unconfirmed": str(e)[:300]},
                        drop=("pid",))
                stages.log(task_dir, "review_unconfirmed", run=run, turn=turn, pack=entry["pack"], chat_url=url,
                           error=str(e)[:300])
            else:
                _drop_pending(task_dir, entry)
        raise
    out = {"sent": bool(res.get("sent")), "why": None, "reason": None, "pack": str(pack), "kind": kind,
           "turn": turn, "title": title, "chat_url": res.get("chat_url"), "chars": res.get("chars"),
           "usage_pct": res.get("usage_pct"), "titled": res.get("titled")}
    warn = settings.get("chatgpt_warn_usage_pct")
    if out["usage_pct"] is not None and warn is not None and out["usage_pct"] < warn:
        log(f"ChatGPT shows {out['usage_pct']}% of this week's Pro usage left.")
    if out["sent"]:
        _confirm(task_dir, {**entry, "chat_url": out["chat_url"]}, sent_at=stages.now_iso())
    elif not no_send:
        _drop_pending(task_dir, entry)
    return out


def _confirm(task_dir: Path, e: dict, sent_at: str | None = None, **extra) -> None:
    """The pack is in the chat: the entry waits on the reply, and chats.json, packs.json and the log say so."""
    when = sent_at or e.get("sent_at") or stages.now_iso()
    _update(task_dir, e, {"state": "sent", "chat_url": e.get("chat_url"), "sent_at": when},
            drop=("pid", "unconfirmed", "error"))
    if e.get("chat_url"):
        set_chat(task_dir, e["run"], e["chat_url"])
    pack = Path(task_dir) / e["pack"] if e.get("pack") else None
    if pack is not None and delta.pack_record(task_dir, e["run"], pack):
        delta.update_pack(task_dir, e["run"], pack, sent_at=when, chat_url=e.get("chat_url"))
    stages.log(task_dir, "review_sent", run=e["run"], turn=e.get("turn"), pack_kind=e.get("kind"),
               pack=e.get("pack"), chat_url=e.get("chat_url"), title=e.get("title"), **extra)


def _clear(task_dir: Path, e: dict, why: str) -> None:
    """The pack never reached the chat: drop the entry and anything that says it was sent, so it can go again."""
    _drop_pending(task_dir, e)
    pack = Path(task_dir) / e["pack"] if e.get("pack") else None
    rec = delta.pack_record(task_dir, e["run"], pack) if pack is not None else None
    if rec and rec.get("sent_at") and rec.get("chat_url") in (None, e.get("chat_url")):
        delta.update_pack(task_dir, e["run"], pack, sent_at=None, chat_url=None)
    if e.get("kind") != "followup" and e.get("chat_url") and chat_for(task_dir, e["run"]) == e.get("chat_url"):
        with _locked(task_dir):   # a new chat that only holds the tag message: the next full pack starts fresh
            p = reviews_dir(task_dir) / CHATS
            chats = _read_json(p, {})
            chats.pop(e["run"], None)
            _write(p, json.dumps(chats, indent=1))
    stages.log(task_dir, "review_send_cleared", run=e["run"], turn=e.get("turn"), pack=e.get("pack"),
               chat_url=e.get("chat_url"), why=why)


def _pack_text(task_dir: Path, e: dict) -> str | None:
    try:
        return (Path(task_dir) / e["pack"]).read_text(errors="replace") if e.get("pack") else None
    except OSError:
        return None


def settle(task_dir: Path, run: str | None = None, log=print) -> list[dict]:
    """Decide sends whose outcome isn't known: "unconfirmed" ones, and "sending" ones whose process is gone (or
    that are over SENDING_STALE_S old). The chat is read (chatgpt.inspect, read-only): when its newest message
    is the pack, the entry waits on the reply; when it isn't, the entry is cleared so the pack can go again;
    when the chat can't be read, the entry stays. Returns [{"entry", "outcome": "sent"|"cleared"|"kept",
    "why"}] for the entries looked at."""
    task_dir = Path(task_dir)
    out, cg = [], None
    for e in pending(task_dir):
        st = state_of(e)
        if (run and e.get("run") != run) or st not in ("sending", "unconfirmed"):
            continue
        if st == "sending" and _alive(e.get("pid")) and _age_s(e.get("started_at") or e.get("sent_at")) < SENDING_STALE_S:
            continue                     # still under way in another process
        name = Path(e.get("pack") or "the pack").name
        if not e.get("chat_url"):
            _clear(task_dir, e, "the send stopped before ChatGPT opened a chat")
            out.append({"entry": e, "outcome": "cleared", "why": "it stopped before ChatGPT opened a chat"})
            log(f"{task_dir.name} {e.get('run')}: the send of {name} stopped before reaching a chat; it can go again.")
            continue
        text = _pack_text(task_dir, e)
        if text is None:
            out.append({"entry": e, "outcome": "kept", "why": "the pack file is gone"})
            continue
        cg = cg or _chatgpt()
        try:
            info = cg.inspect(e["chat_url"], account=settings.get("chatgpt_account"))
        except Exception as err:
            _mark_pending(task_dir, e, checked_at=stages.now_iso(), error=str(err)[:300])
            out.append({"entry": e, "outcome": "kept", "why": f"couldn't read the chat ({err})"})
            continue
        held = cg.holds(info, text)
        if held:
            _confirm(task_dir, e, settled=True)
            out.append({"entry": e, "outcome": "sent", "why": "the pack is in the chat"})
            log(f"{task_dir.name} {e.get('run')}: {name} did reach GPT Pro ({e['chat_url']}); waiting on the reply.")
        elif held is False:
            _clear(task_dir, e, "the pack isn't in the chat")
            out.append({"entry": e, "outcome": "cleared", "why": "the pack isn't in the chat"})
            log(f"{task_dir.name} {e.get('run')}: {name} never reached GPT Pro; it can go again.")
        else:
            _mark_pending(task_dir, e, checked_at=stages.now_iso())
            out.append({"entry": e, "outcome": "kept", "why": "the chat doesn't say"})
    return out


def _dirs(task_dir: Path | None) -> list[Path]:
    return [Path(task_dir)] if task_dir else sorted(p.parent.parent for p in config.TASKS.glob(f"*/{DIR}/{PENDING}"))


def expire_pending(task_dir: Path | None = None, max_age_s: float = PENDING_TTL_S, log=print) -> list[dict]:
    """Drop sends with no reply saved after max_age_s (one task, or all), so the run's next pack can go. Logs
    review_expired; returns the entries dropped, each with "task" (the short name) added."""
    gone = []
    for d in _dirs(task_dir):
        for e in pending(d):
            age = _age_s(e.get("sent_at") or e.get("started_at"))
            if age < max_age_s:
                continue
            if e.get("last_status") == "error" and age < 4 * max_age_s:
                continue   # the chat couldn't be read at the last check (Chrome closed, asleep): look again first
            _drop_pending(d, e)
            stages.log(d, "review_expired", run=e.get("run"), turn=e.get("turn"), pack=e.get("pack"),
                       chat_url=e.get("chat_url"), state=state_of(e))
            log(f"{d.name} {e.get('run')} turn {e.get('turn')}: no reply after {max_age_s / 3600:g} hours; "
                "stopped waiting on it.")
            gone.append({**e, "task": d.name})
    return gone


def _seen(task_dir: Path, markdown: str, message_id: str | None) -> bool:
    """True when the chat's newest reply is one already saved (the new one hasn't started showing yet)."""
    sha = _sha(markdown)
    return any(r.get("sha") == sha or (message_id and r.get("message_id") == message_id) for r in list_reviews(task_dir))


def _title_only(markdown: str, entry: dict) -> bool:
    """The reply is just the tag or title (the answer to the tag message): the pack itself never went."""
    flat = " ".join(markdown.split()).strip("\"'`*. ")
    return any(x and flat == " ".join(str(x).split()) for x in (entry.get("tag"), entry.get("title")))


def fetch_pending(task_dir: Path | None = None, log=print) -> list[Path]:
    """Save every finished reply that's waiting (one task, or all of them). Sends whose outcome isn't known are
    settled first. Replies still being written stay pending; each check is noted on the entry (last_status,
    checked_at)."""
    saved: list[Path] = []
    cg = None
    for d in _dirs(task_dir):
        if any(state_of(e) != "sent" for e in pending(d)):
            settle(d, log=log)
        for e in pending(d):
            if state_of(e) != "sent" or not e.get("chat_url"):
                continue
            cg = cg or _chatgpt()
            try:
                res = cg.fetch(e["chat_url"], account=settings.get("chatgpt_account"))
            except cg.ChatGPTError as err:
                log(f"{d.name} {e['run']}: couldn't read the chat ({err})")
                _mark_pending(d, e, last_status="error", checked_at=stages.now_iso(), error=str(err)[:300])
                continue
            status, md = res.get("status"), (res.get("markdown") or "").strip()
            if status != "done" or not md or _seen(d, md, res.get("message_id")) or _title_only(md, e):
                _mark_pending(d, e, last_status=status if status != "done" else "waiting", checked_at=stages.now_iso())
                continue
            path = resolve_pending(d, e["chat_url"], md, message_id=res.get("message_id"))
            log(f"{d.name} {e['run']} turn {e.get('turn')}: saved {path}")
            saved.append(path)
    return saved


# --- Findings in a reply ----------------------------------------------------------------------------

_FINDINGS = re.compile(r"```[ \t]*findings[^\n]*\n(.*?)(?:```|\Z)", re.S | re.I)


def findings(text: str) -> list[dict]:
    """The findings in a GPT reply: its last ```findings JSON block, else its "## Findings" table, read
    leniently (grading.parse_findings). [] when it has neither, or they can't be read."""
    try:
        from . import grading
        return grading.parse_findings(text or "") or []
    except Exception:
        pass
    for m in reversed(list(_FINDINGS.finditer(text or ""))):
        try:
            v = json.loads(re.sub(r",\s*([\]}])", r"\1", m.group(1)))
        except ValueError:
            continue
        if isinstance(v, dict):
            v = v.get("findings") if isinstance(v.get("findings"), list) else [v]
        if isinstance(v, list):
            return [{**f, "id": str(f["id"]).upper() if f.get("id") else None} for f in v
                    if isinstance(f, dict) and (f.get("id") or f.get("finding"))]
    return []


def _fid_num(f: dict) -> int:
    m = re.match(r"F(\d+)$", str(f.get("id") or ""))
    return int(m.group(1)) if m else 10**6


def last_findings(task_dir: Path, run: str) -> list[dict]:
    """GPT's findings for one run, merged by ID across its saved replies (oldest first, the latest value of each
    field wins, so a follow-up that only gives an ID and a status keeps the finding's text). Each has "review",
    the file it last came from. Ordered by ID."""
    merged: dict[str, dict] = {}
    for r in list_reviews(task_dir):
        if r.get("run") != run or (r.get("source") or "gptpro") != "gptpro":
            continue
        for f in findings(body(Path(r["path"]))):
            fid = f.get("id")
            if fid:
                keep = {k: v for k, v in f.items() if v not in (None, "", [])}
                merged[fid] = {**merged.get(fid, {}), **keep, "review": r["name"]}
    return sorted(merged.values(), key=_fid_num)


# --- CLI -----------------------------------------------------------------------------------------

def _task(name: str) -> Path:
    from . import workspace
    try:
        return workspace.resolve(name)
    except FileNotFoundError as e:
        raise SystemExit(str(e))


def _show_path(p: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(config.ROOT.resolve()))
    except ValueError:
        return str(p)


def _cmd_add(a) -> None:
    d = _task(a.task)
    before = {p.name for p in reviews_dir(d).glob("*.md")}
    path = add(d, file=a.file, run=a.run, turn=a.turn, source=a.source)
    fm, text = _parse(path)
    verb = "Already saved as" if path.name in before else "Saved"
    print(f"{verb} {_show_path(path)} ({fm.get('run')}, turn {fm.get('turn')}, {len(text):,} characters)")
    print(f"  {_first_line(text)[:120]}")


def _cmd_list(a) -> None:
    d = _task(a.task)
    rows = list_reviews(d)
    for r in rows:
        when = (r.get("saved_at") or "")[:16].replace("T", " ")
        print(f"{r['name']:<34} turn {r.get('turn') or '?':<3} {when}  {r['chars']:>7,} chars  {r['first_line'][:70]}")
    for e in pending(d):
        st = state_of(e)
        status = f", {e['last_status']}" if e.get("last_status") and st == "sent" else ""
        if st == "sending":
            status += ", being sent"
        elif st == "unconfirmed":
            status += ", Send was clicked but didn't show working (the chat is checked before any retry)"
        print(f"waiting: {e['run']} turn {e.get('turn')}, sent {(e.get('sent_at') or '')[11:16]}{status}  "
              f"{e.get('chat_url') or ''}".rstrip())
    if not rows and not pending(d):
        print(f"No saved reviews for {d.name}.")


def _cmd_show(a) -> None:
    d = _task(a.task)
    rows = list_reviews(d)
    if a.name:
        rows = [r for r in rows if r["name"] == a.name or r["name"].startswith(a.name)]
    if not rows:
        raise SystemExit(f"No saved review{' ' + a.name if a.name else ''} for {d.name}.")
    r = rows[-1]
    print(f"# {r['name']} · {r.get('run')} turn {r.get('turn')} · {r.get('source')} · {(r.get('saved_at') or '')[:16]}\n")
    print(body(Path(r["path"])))


def _cmd_pack(a) -> None:
    d = _task(a.task)
    out = delta.next_pack(d, a.run, full=a.full, ai=True if a.ai else False if a.no_ai else None)
    print(out)


def _cmd_send(a) -> None:
    d = _task(a.task)
    try:
        res = send_review(d, a.run, pack=a.pack, no_send=a.no_send, force=a.force)
    except ChatGPTUnavailable as e:
        raise SystemExit(str(e))
    except Exception as e:
        if type(e).__name__ == "ChatGPTError":
            raise SystemExit(f"ChatGPT: {e}")
        raise
    if res.get("sent"):
        print(f"Sent {Path(res['pack']).name} ({res.get('kind')}, turn {res.get('turn')}) to {res.get('chat_url')}")
    elif res.get("reason"):
        print(f"Not sent: {res['reason']}.")
    else:
        print(f"Not sent (--no-send): {Path(res['pack']).name}" + (f" -> {res['chat_url']}" if res.get("chat_url") else ""))


def _cmd_fetch(a) -> None:
    d = _task(a.task) if a.task else None
    try:
        saved = fetch_pending(d)
    except ChatGPTUnavailable as e:
        raise SystemExit(str(e))
    except Exception as e:
        if type(e).__name__ == "ChatGPTError":
            raise SystemExit(f"ChatGPT: {e}")
        raise
    left = sum(len(pending(x)) for x in ([d] if d else [p.parent.parent for p in config.TASKS.glob(f"*/{DIR}/{PENDING}")]))
    if not saved:
        print("No replies ready yet." if left else "Nothing waiting.")
    elif left:
        print(f"{left} still waiting.")


def register(sub) -> None:
    """`johto review add|list|show|pack|send|fetch`."""
    p = sub.add_parser("review", help="outside reviews: save GPT Pro replies, build and send review packs")
    rs = p.add_subparsers(dest="review_cmd", required=True)

    s = rs.add_parser("add", help="save a review reply (from the clipboard unless --file)")
    s.add_argument("task")
    s.add_argument("--run", help="e.g. validate-1 (default: the send waiting on a reply, else the newest pack's run)")
    s.add_argument("--turn", type=int, help="the turn it reviews (default: the turn its pack covered)")
    s.add_argument("--source", default="gptpro", help="who wrote it (default gptpro)")
    s.add_argument("--file", help="read the reply from this file ('-' for stdin)")
    s.set_defaults(func=_cmd_add)

    s = rs.add_parser("list", help="saved reviews and sends still waiting")
    s.add_argument("task")
    s.set_defaults(func=_cmd_list)

    s = rs.add_parser("show", help="print a saved review (default: the newest)")
    s.add_argument("task")
    s.add_argument("name", nargs="?", help="file name or its start, e.g. validate-1-t02")
    s.set_defaults(func=_cmd_show)

    s = rs.add_parser("pack", help="build the next review pack (a follow-up after the first) and print its path")
    s.add_argument("task")
    s.add_argument("run", nargs="?", help="e.g. validate-1 (default: most recent; works mid-run)")
    s.add_argument("--full", action="store_true", help="a full pack even if there's an earlier one")
    s.add_argument("--ai", action="store_true", help="full pack with the outside Claude review (paid; last in the pack)")
    s.add_argument("--no-ai", action="store_true", help="full pack without the outside Claude review (the default "
                   "unless settings pack_outside_review is on)")
    s.set_defaults(func=_cmd_pack)

    s = rs.add_parser("send", help="send the next pack to GPT Pro (follow-ups go to the run's chat)")
    s.add_argument("task")
    s.add_argument("run", nargs="?", help="e.g. validate-1 (default: most recent)")
    s.add_argument("--pack", help="send this pack file instead of the next one")
    s.add_argument("--no-send", action="store_true", help="prepare it in ChatGPT but don't send")
    s.add_argument("--force", action="store_true",
                   help="send even if this pack was sent, a review is still due, or the pack is partial")
    s.set_defaults(func=_cmd_send)

    s = rs.add_parser("fetch", help="save GPT Pro replies that have finished")
    s.add_argument("task", nargs="?", help="one task (default: all)")
    s.set_defaults(func=_cmd_fetch)
