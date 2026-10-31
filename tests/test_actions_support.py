"""Shared fixtures for the action tests: a throwaway JOHTO_ROOT, a fake Claude run, fixture field text.

Everything lives in a temp folder; config paths are patched for the test's duration, so nothing here
can touch the real data/ or tasks/. The field text below is test fixture data for the fake Studio.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest import mock

from johto import config
from johto.dossier import store

SLOT = {"action": "Modify", "domain": "Backend / API", "codebase_type": "Personal-scale",
        "prompt_style": "Casual prose", "interaction_mode": "Interactive/pairing"}
TASKS = [{"task_id": f"task_fixture{i:024d}", "task_name": f"Task fix{i:04d}"} for i in range(1, 4)]
OTHER = {"task_id": "task_fixtureother0000000000000", "task_name": "Task othr0001"}
LEAD_ID = "Ltest001"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
DOSSIER_ID = "acme__widget__01234567"
PROMPT = ("The widget sync job keeps retrying failed uploads forever and the queue grows until the worker "
          "falls over. It started after the retry change last month. Can you look into why the backoff never kicks in?")


def make_root(root: Path) -> dict:
    data, tasks = root / "data", root / "tasks"
    (data / "scout").mkdir(parents=True)
    (data / "dossiers").mkdir()
    tasks.mkdir()
    rows = [{**t, "status": "Ready to Claim", "owned_by": None, "slot": SLOT, "repo": None, "repo_url": None,
             "opening_prompt": None, "updated_at": "2026-10-30T00:00:00Z"} for t in TASKS]
    rows.append({**OTHER, "status": "Ready to Claim", "owned_by": None,
                 "slot": {**SLOT, "prompt_style": "Explicit spec"}, "repo": None, "repo_url": None,
                 "opening_prompt": None, "updated_at": "2026-10-30T00:00:00Z"})
    (data / "studio_tasks.json").write_text(json.dumps(rows))
    (data / "slots.json").write_text(json.dumps({"synced_at": "2026-10-30T00:00:00-0700", "groups": [
        {"slot": SLOT, "count": 3, "tasks": TASKS},
        {"slot": {**SLOT, "prompt_style": "Explicit spec"}, "count": 1, "tasks": [OTHER]}]}))
    lead = {"id": LEAD_ID, "repo": "acme/widget", "commit": COMMIT,
            "repo_url_pinned": f"https://github.com/acme/widget/tree/{COMMIT}", "primary_language": "Go",
            "slot_fit": {k: SLOT[k] for k in ("action", "domain", "codebase_type")},
            "title": "Uploads retry forever", "facts": ["queue/queue.go resets attempts on requeue"],
            "files": ["queue/queue.go"], "mechanism": "attempt counter reset", "verify": "go test ./...",
            "one_shot_risk": {"level": "low", "why": "two layers"}, "upstream": {"related": [], "looks_solved": False},
            "source": "agent", "status": "new", "live_services": []}
    (data / "scout" / "leads.json").write_text(json.dumps([lead]))
    (data / "scout" / "repos.json").write_text(json.dumps({"acme/widget": {
        "full_name": "acme/widget", "commit": COMMIT, "studio_language": "Go", "fork": False,
        "description": "fixture repo"}}))
    doc = {"schema": "johto.dossier/1", "id": DOSSIER_ID, "repo": "acme/widget", "commit": COMMIT,
           "pinned_url": f"https://github.com/acme/widget/tree/{COMMIT}", "generated_at": "2026-10-30T00:00:00Z",
           "headline": "fixture sync service", "verdict": {"promise": "high", "why": "fixture", "best_fits": []},
           "blocks": [{"type": "opportunity", "id": "o1", "title": "Retry storms in the queue", "area": "queue",
                       "ambition": "Bounded retries survive restarts", "why_not_done": "fixture",
                       "evidence": [{"pointer": "queue/queue.go:10-20", "fact": "resets attempts"}],
                       "where_models_fail": ["adds a sleep in the worker"],
                       "bounded_task": {"exists": True, "shape": "attempt counter persists", "size": "M",
                                        "check_in_session": "go test ./queue/"},
                       "slot_fit": [{k: SLOT[k] for k in ("action", "domain", "codebase_type")}],
                       "one_shot": {"likelihood": "unlikely", "why": "two layers"},
                       "upstream": {"related": [], "looks_solved": False},
                       "self_contained": {"ok": True, "needs": []}}]}
    (data / "dossiers" / f"{DOSSIER_ID}.json").write_text(json.dumps(doc))
    return {"data": data, "tasks": tasks}


def patch_config(root: Path) -> list:
    return [mock.patch.object(config, "ROOT", root), mock.patch.object(config, "DATA", root / "data"),
            mock.patch.object(config, "TASKS", root / "tasks"), mock.patch.object(config, "FAKE_STUDIO", True),
            mock.patch.object(store, "P", store.Paths(root / "data"))]


def _entry(kind: str, ts: str, text: str, stop: str | None = None) -> dict:
    if kind == "user":
        return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}
    return {"type": "assistant", "timestamp": ts,
            "message": {"model": "claude-opus-5-5", "stop_reason": stop, "output_config": {"effort": "high"},
                        "content": [{"type": "text", "text": text}]}}


def fake_claude(run_dir: Path, prompt: str, follow_up: str = "What about the requeue path?") -> Path:
    """A collected-looking run: a two-turn transcript (40 active minutes) and a diff."""
    out = run_dir / "prework_output"
    out.mkdir(parents=True, exist_ok=True)
    rows = [_entry("user", "2026-10-30T10:00:00Z", prompt),
            _entry("assistant", "2026-10-30T10:10:00Z", "Reading the queue."),
            _entry("assistant", "2026-10-30T10:25:00Z", "Found it.", "end_turn"),
            _entry("user", "2026-10-30T10:30:00Z", follow_up),
            _entry("assistant", "2026-10-30T10:45:00Z", "Fixed the requeue path too.", "end_turn")]
    p = out / f"{uuid.uuid4()}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (out / "final.diff").write_text("diff --git a/queue/queue.go b/queue/queue.go\n--- a/queue/queue.go\n"
                                    "+++ b/queue/queue.go\n@@ -1 +1 @@\n-a\n+b\n")
    return p


FIXTURE_TEXT = {
    "Opening Prompt": PROMPT,
    "High-Level Task Goal": ("Uploads that fail with a retryable error back off and stop after the configured limit, "
                             "and the queue stays bounded under a burst of failures. Raising the limit or deleting "
                             "failed items does not count."),
    "Decision Points": ("Better: traces the retry loop back to where the attempt counter is reset. Worse: adds a sleep "
                        "in the worker. Better: writes a test that floods the queue with failures. Worse: only checks "
                        "one failing upload by hand."),
    "Difficulty & Codebase Understanding": ("Widget is a small Go service that syncs files to object storage. The queue "
                                            "lives in queue/queue.go and the worker in worker/run.go. The attempt "
                                            "counter is stored on the job row and reset on every requeue. That reset is "
                                            "what makes the bug hard to see."),
    "Validation Notes": "Claude found the counter reset on the second turn. It needed one nudge to add the flood test.",
}


def write_fixture_text(d: Path, ratings=("No", "3", "3")) -> None:
    import re
    p = d / "submission.md"
    s = p.read_text()
    for h, t in FIXTURE_TEXT.items():
        s = re.sub(rf"(^## {re.escape(h)}[ \t]*\n)", lambda m, t=t: m.group(1) + t + "\n", s, count=1, flags=re.M)
    one, ts, st = ratings
    s = re.sub(r"^One-shot:.*$", f"One-shot: {one}", s, count=1, flags=re.M)
    s = re.sub(r"^Task Success:.*$", f"Task Success: {ts}", s, count=1, flags=re.M)
    s = re.sub(r"^Steering:.*$", f"Steering: {st}", s, count=1, flags=re.M)
    p.write_text(s)
