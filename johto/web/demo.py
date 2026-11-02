"""A hypothetical afternoon with six tasks in flight, for designing and reviewing the UI.

Leads, repos, slots, files and code are invented. Protected fields (the ones only Austin writes)
hold lorem ipsum so nothing here can be mistaken for, or copied into, real submission text.
"""
from __future__ import annotations

import copy
import json
import time

from .. import config
from . import data

LOREM = ("Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore "
         "et dolore magna aliqua. Ut enim ad minim veniam quis nostrud exercitation ullamco laboris nisi ut "
         "aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum "
         "dolore eu fugiat nulla pariatur. Excepteur sint occaecat cupidatat non proident sunt in culpa qui "
         "officia deserunt mollit anim id est laborum.").split()


def lorem(words: int, start: int = 0, sentences_every: int = 0) -> str:
    out = []
    for i in range(words):
        w = LOREM[(start + i) % len(LOREM)].strip(".")
        if not out or (sentences_every and i % sentences_every == 0):
            w = w.capitalize()
        out.append(w)
        if sentences_every and (i + 1) % sentences_every == 0:
            out[-1] += "."
    s = " ".join(out)
    return s if s.endswith(".") else s + "."


def dp_lorem() -> str:
    return ("Better: " + lorem(16, 3) + " Worse: " + lorem(12, 20) + "\n\n"
            "Better: " + lorem(14, 40) + " Worse: " + lorem(10, 55))


def _ts(minutes_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - minutes_ago * 60))


def _demo_lead(lid, repo, commit, lang, action, domain, title, files, mechanism, verify, risk, why):
    return {"id": lid, "repo": repo, "commit": commit, "repo_url_pinned": f"https://github.com/{repo}/tree/{commit}",
            "primary_language": lang, "slot_fit": {"action": action, "domain": domain, "codebase_type": "Personal-scale"},
            "title": title, "facts": [f"{files[0]} handles this today without any check for the case in the title."],
            "files": files, "mechanism": mechanism, "verify": verify, "one_shot_risk": {"level": risk, "why": why},
            "upstream": {"related": [], "looks_solved": False, "keywords": title.lower().split()[:3]},
            "source": "demo", "issue_url": None, "repo_notes": ["Invented repo for the demo."],
            "created_at": "2026-10-01T09:00:00", "status": "new"}


# Invented repos and leads: the demo never reads data/scout/.
DEMO_LEADS = {x["id"]: x for x in (
    _demo_lead("Ldemo001", "acme/chatd", "5ad0c0de11b2c3d4e5f60718293a4b5c6d7e8f90", "Go", "Create", "Backend / API",
               "Lock and unlock a room from the admin API", ["room/room.go", "server/api.go"],
               "Rooms have no locked state; joins are only checked against the room's password.",
               "Lock a room, then try to join it as a new user and as an operator.", "medium",
               "The API shape is open, so the model has to choose between several reasonable designs."),
    _demo_lead("Ldemo002", "acme/chatd", "5ad0c0de11b2c3d4e5f60718293a4b5c6d7e8f90", "Go", "Create", "Backend / API",
               "List live clients and kick one", ["room/client.go", "server/api.go"],
               "Clients are tracked per room but never exposed; there is no way to remove one.",
               "Join with two clients, list them, kick one and check it disconnects.", "medium",
               "Kicking touches connection teardown, which is easy to get subtly wrong."),
    _demo_lead("Ldemo003", "acme/playlog", "9f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c", "TypeScript", "Modify",
               "Web frontend", "Import returns no tracks for some accounts", ["src/sources/import.ts"],
               "The importer stops at the first empty page instead of following the next cursor.",
               "Import an account whose second page is empty and check later pages arrive.", "low",
               "The bug is in one function, but the fix needs a fixture the repo doesn't have."),
    _demo_lead("Ldemo004", "acme/photobox", "1b2c3d4e5f60718293a4b5c6d7e8f9001a2b3c4d", "JavaScript", "Modify",
               "Web frontend", "Tag saves fail silently", ["src/tags/save.js", "src/api/tags.js"],
               "A failed save is caught and dropped, so the UI shows the tag as saved.",
               "Make the API return 500 and check the UI shows the error and keeps the old tag.", "medium",
               "Error states cross the UI and API layers."),
    _demo_lead("Ldemo005", "acme/photobox", "1b2c3d4e5f60718293a4b5c6d7e8f9001a2b3c4d", "JavaScript", "Modify",
               "Web frontend", "Overlapping syncs duplicate photos", ["src/sync/index.js"],
               "Two syncs can start at once; each inserts the same new files.",
               "Start two syncs together and count the photos afterwards.", "medium",
               "Needs a lock or queue; the model must pick where it lives."),
    _demo_lead("Ldemo006", "acme/flowrun", "2c3d4e5f60718293a4b5c6d7e8f9001a2b3c4d5e", "Go", "Create", "Backend / API",
               "SQLite as a storage backend", ["store/store.go", "store/postgres.go"],
               "Storage is Postgres-only, behind an interface that leaks Postgres types.",
               "Run the store test suite against SQLite.", "low",
               "Large change across the storage layer; one-shot is unlikely."),
    _demo_lead("Ldemo007", "acme/flowrun", "2c3d4e5f60718293a4b5c6d7e8f9001a2b3c4d5e", "Go", "Create", "Backend / API",
               "foreach in step templates", ["engine/template.go"],
               "Templates support only plain substitution, so lists must be expanded by hand.",
               "Render a step that loops over a list input.", "medium",
               "Template parsing is easy; scoping loop variables is where it goes wrong."),
    _demo_lead("Ldemo008", "acme/playlog", "9f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c", "TypeScript", "Modify",
               "Web frontend", "Desktop presence shows the wrong track", ["src/clients/presence.ts"],
               "Presence updates use the previous track when two updates arrive close together.",
               "Send two updates a second apart and check the presence shows the second.", "medium",
               "A race between two async updates."),
)}


def _lead(lead_id: str) -> dict | None:
    return DEMO_LEADS.get(lead_id)


INTERACTIVE = "Interactive/pairing"
CASUAL = "Casual prose"

TASKS = [
    # short, task_id, lead, slot, stage
    ("d3m0aab3", "task_demo_d3m0aab3", "Ldemo001", ("Create", "Backend / API", "Personal-scale", CASUAL, INTERACTIVE),
     "validating", "chatd · Lock/unlock room"),
    ("d3m0aaa9", "task_demo_d3m0aaa9", "Ldemo002", ("Create", "Backend / API", "Personal-scale", CASUAL, INTERACTIVE),
     "prompt", "chatd · Live clients + kick"),
    ("d3m0aaa1", "task_demo_d3m0aaa1", "Ldemo003", ("Modify", "Web frontend", "Personal-scale", CASUAL, INTERACTIVE),
     "decide", "playlog · Import returns no tracks"),
    ("d3m0aab1", "task_demo_d3m0aab1", "Ldemo004", ("Modify", "Web frontend", "Personal-scale", CASUAL, INTERACTIVE),
     "rate", "photobox · Tag save failures"),
    ("d3m0aaa7", "task_demo_d3m0aaa7", "Ldemo005", ("Modify", "Web frontend", "Personal-scale", CASUAL, INTERACTIVE),
     "details", "photobox · Overlapping syncs"),
    ("d3m0aaa6", "task_demo_d3m0aaa6", "Ldemo006", ("Create", "Backend / API", "Personal-scale", "Explicit spec",
                                                    "Async/autonomous"), "studio", "flowrun · SQLite support"),
    ("d3m0aaa4", "task_demo_d3m0aaa4", "Ldemo007", ("Create", "Backend / API", "Personal-scale", CASUAL, INTERACTIVE),
     "studio", "flowrun · foreach in templates"),
    ("d3m0aaa8", "task_demo_d3m0aaa8", "Ldemo008", ("Modify", "Web frontend", "Personal-scale", CASUAL, INTERACTIVE),
     "decide", "playlog · Desktop presence"),
]

STAGE_LABEL = {"prompt": "Prompt", "calibration": "Calibration", "decide": "Keep or drop", "details": "Details",
               "validating": "Validating", "rate": "Rate", "studio": "Studio"}


def _slot(t) -> dict:
    return dict(zip(("action", "domain", "codebase_type", "prompt_style", "interaction_mode"), t))


def _facts(turns, active, wall, task_min, files, runs, last_ok, after_edit=True, diff=None, final=""):
    return {"turns": turns, "follow_ups": max(0, turns - 1), "active_min": active, "wall_min": wall,
            "task_claude_min": task_min, "files_edited": files,
            "tests": {"runs": runs, "last_ok": last_ok, "last_cmd": None, "after_last_edit": after_edit},
            "transcripts": 1, "model_ok": True, "effort_ok": True, "diffstat": diff or [],
            "first_prompt_matches": True, "final_message_excerpt": final}


def _note(i, t, text):
    return {"id": f"n{i}", "at": _ts(30 - i), "t_run_s": t, "turn": None, "text": text}


def build() -> dict:
    tasks = {}
    for short, tid, lead_id, slot_t, stage, label in TASKS:
        slot = _slot(slot_t)
        lead = _lead(lead_id)
        repo_full = (lead or {}).get("repo", "")
        rrec = {}
        repo = {"full_name": rrec.get("full_name", repo_full), "commit": (lead or {}).get("commit"),
                "pinned_url": (lead or {}).get("repo_url_pinned"), "language": (lead or {}).get("primary_language")}
        texts = {k: "" for k in data.SECTION_KEYS}
        prompt_words = 230 if slot["prompt_style"] == "Explicit spec" else 41
        if stage != "prompt":
            texts["opening_prompt"] = lorem(prompt_words, 0, 13)
        if stage in ("validating", "rate", "studio"):
            texts["goal"] = lorem(34, 5, 17)
            texts["decision_points"] = dp_lorem()
            texts["codebase"] = lorem(78, 11, 19)
        if stage == "details":
            texts["goal"] = lorem(30, 5, 15)
            texts["decision_points"] = "Better: " + lorem(15, 3) + " Worse: " + lorem(11, 21) + "\n\nBetter: " + lorem(9, 44)
        if stage == "studio":
            texts["validation_notes"] = lorem(52, 30, 13)
        locked = "sent in calibration 1" if stage not in ("prompt",) else None
        if stage in ("validating", "rate", "studio"):
            locked = "sent in calibration 1 and validation 1"
        sections = {k: data.section(k, v, slot, others=texts,
                                    locked=locked if k == "opening_prompt" else None)
                    for k, v in texts.items() if k != "initial_state"}
        rt = {"one_shot": None, "task_success": None, "steering": None}
        if stage == "studio":
            rt = {"one_shot": "No", "task_success": 3, "steering": 3}
        summary = {"short": short, "task_id": tid, "task_name": f"Task {short}", "label": label, "repo": repo,
                   "slot": slot, "stage": stage, "stage_label": STAGE_LABEL[stage],
                   "studio": {"status": "Writing", "url": f"{config.STUDIO}/annotator/tasks/{tid}/"},
                   "lead_id": lead_id, "claude_min": 0, "my_min": 0, "updated_at": _ts(5)}
        tasks[short] = {"summary": summary, "sections": sections, "ratings": data.ratings(rt, slot), "runs": [],
                        "decisions": {}, "ready": None,
                        "studio": {"status": "Writing", "saved_at": None, "changed_since_save": [],
                                   "feedback": None, "qc": None, "url": summary["studio"]["url"]},
                        "gists": {}, "lead": lead, "checklist": [],
                        "upstream_mentions": [{"url": x.get("url"), "title": x.get("title"), "state": x.get("state"),
                                               "mentioned": False}
                                              for x in ((lead or {}).get("upstream") or {}).get("related", [])],
                        "discard": None}

    # Runs and stage-specific details.
    t = tasks["d3m0aab3"]
    t["summary"].update(claude_min=38, my_min=64)
    t["runs"] = [
        {"name": "calib-1", "kind": "calib", "n": 1, "created_at": _ts(140), "collected_at": _ts(105),
         "state": "collected", "facts": _facts(4, 31, 35, 31, ["room/room.go", "server/api.go"], 3, True),
         "moments": [], "audit": None, "notes": [], "recommend": {"decision": "keep", "rule": "4 turns"}},
        {"name": "validate-1", "kind": "validate", "n": 1, "created_at": _ts(16), "collected_at": None,
         "state": "active", "facts": _facts(3, 14.3, 16, 38, ["room/room.go", "server/api.go"], 2, True),
         "moments": [], "audit": None,
         "notes": [_note(1, 12 * 60, "called room.Add on an absent room"),
                   _note(2, 21 * 60, "no test for reopening with auto-expiry on")], "recommend": None},
    ]

    t = tasks["d3m0aaa1"]
    t["summary"].update(claude_min=6.4, my_min=21)
    t["runs"] = [{
        "name": "calib-1", "kind": "calib", "n": 1, "created_at": _ts(25), "collected_at": _ts(2),
        "state": "collected",
        "facts": _facts(1, 6.4, 9, 6.4, ["src/sources/import.ts",
                                          "src/sources/import.test.ts"], 2, True,
                        diff=[{"path": "src/sources/import.ts", "add": 41, "del": 12, "new": False, "test": False},
                              {"path": "src/sources/import.test.ts", "add": 58, "del": 0, "new": True, "test": True}],
                        final="The import source now reads tracks from its history page again when the "
                              "activity feed has no track entries. I added a test with a recorded response."),
        "moments": [
            {"id": "m1", "category": "speed", "level": "warn",
             "text": "1 turn, 6.4 min: reviewers drop interactive tasks Opus finishes in about 5 minutes", "ref": None},
            {"id": "m2", "category": "tests", "level": "ok", "text": "npm run test passed after the last edit", "ref": None},
            {"id": "m3", "category": "model", "level": "ok", "text": "Opus 5.5 · high on every reply", "ref": None},
            {"id": "m4", "category": "lane", "level": "ok", "text": "one transcript in this lane", "ref": None}],
        "audit": None, "notes": [_note(1, 4 * 60, "read the source poller only, never the upload side")],
        "recommend": {"decision": "drop", "rule": "Drop is recommended: 1 turn, under 10 active min, last test passed."}}]

    t = tasks["d3m0aaa7"]
    t["summary"].update(claude_min=22, my_min=37)
    t["runs"] = [{
        "name": "calib-1", "kind": "calib", "n": 1, "created_at": _ts(90), "collected_at": _ts(58),
        "state": "collected",
        "facts": _facts(3, 22, 31, 22, ["src/web/cache/index.ts", "src/web/init/setup.ts"],
                        4, False, diff=[{"path": "src/web/cache/index.ts", "add": 64, "del": 9, "new": False, "test": False},
                                        {"path": "src/web/init/setup.ts", "add": 12, "del": 3, "new": False, "test": False},
                                        {"path": "src/web/test/offline_sync.test.ts", "add": 88, "del": 0, "new": True, "test": True}]),
        "moments": [
            {"id": "m1", "category": "honesty", "level": "warn", "text": "said \"all tests pass\"; the last test run failed", "ref": {"entry": 212, "turn": 3}},
            {"id": "m2", "category": "scope", "level": "warn", "text": "src/server/tree.js untouched", "ref": None}],
        "audit": None,
        "notes": [_note(1, 6 * 60, "added a boolean lock, drops the last reload"),
                  _note(2, 15 * 60, "never looked at the purge after root write")], "recommend": {"decision": None, "rule": ""}}]

    t = tasks["d3m0aab1"]
    t["summary"].update(claude_min=56, my_min=72)
    t["runs"] = [
        {"name": "calib-1", "kind": "calib", "n": 1, "created_at": _ts(200), "collected_at": _ts(170), "state": "collected",
         "facts": _facts(4, 26, 30, 26, [], 3, True), "moments": [], "audit": None, "notes": [], "recommend": {"decision": "keep", "rule": ""}},
        {"name": "validate-1", "kind": "validate", "n": 1, "created_at": _ts(80), "collected_at": _ts(4), "state": "collected",
         "facts": _facts(6, 47, 61, 56, ["src/web/api/api.ts", "src/web/api/client.ts",
                                         "src/web/store/store.ts", "src/web/nav.tsx"], 5, True),
         "moments": [{"id": "m1", "category": "scope", "level": "warn", "text": "panel/Info.tsx still closes the dialog on failure", "ref": None},
                     {"id": "m2", "category": "tests", "level": "ok", "text": "npm run build and the new tests passed after the last edit", "ref": None},
                     {"id": "m3", "category": "model", "level": "ok", "text": "Opus 5.5 · high on every reply", "ref": None}],
         "audit": None,
         "notes": [_note(1, 9 * 60, "rolled back with dropLabel, deletes existing labels"),
                   _note(2, 17 * 60, "had to point it at the in-place mutation"),
                   _note(3, 28 * 60, "left the failed event in the event store"),
                   _note(4, 36 * 60, "asked before touching the server"),
                   _note(5, 44 * 60, "no error state in Info.tsx")], "recommend": None}]

    t = tasks["d3m0aaa6"]
    t["summary"].update(claude_min=52, my_min=61)
    t["runs"] = [{"name": "validate-1", "kind": "validate", "n": 1, "created_at": _ts(240), "collected_at": _ts(180),
                  "state": "collected", "facts": _facts(1, 52, 55, 52, ["engine/", "models/"], 6, True), "moments": [],
                  "audit": None, "notes": [], "recommend": None}]
    t["studio"].update(saved_at=_ts(14), changed_since_save=["goal"], qc={
        "audit_id": "qcaud_demo", "spec": "AutoQC", "status": "completed", "started_at": _ts(16), "finished_at": _ts(12),
        "counts": {"pass": 11, "fail": 1, "neutral": 1},
        "dims": [{"section": "Task & Rationale", "dimension": "High-Level Task Goal", "status": "fail", "field_key": "goal",
                  "justification": "The goal names a method (a storage adapter interface) rather than a checkable "
                                   "outcome, so other valid designs would fail it."},
                 {"section": "Authenticity", "dimension": "AI Detection", "status": "neutral", "field_key": None,
                  "justification": "Advisory warning only; the reviewer decides."}]})
    t["gists"] = {"conversation_log": "https://gist.github.com/austeane/0d1e", "final_output": "https://gist.github.com/austeane/9c2a"}
    t["checklist"] = [
        {"key": "autoqc", "status": "fail", "title": "AutoQC 14:31 · High-Level Task Goal", "blocking": True,
         "detail": "The goal names a method (a storage adapter interface) rather than a checkable outcome, so other valid designs would fail it.",
         "fix": {"action": "focus_field", "field": "goal", "span": None}},
        {"key": "studio_sync", "status": "pass", "title": "Saved to Studio 14:29", "detail": "1 field changed since: High-Level Task Goal", "blocking": False, "fix": None},
        {"key": "ratings", "status": "pass", "title": "Ratings pass", "detail": "Personal-scale · Task Success 3 · Steering 3", "blocking": False, "fix": None},
        {"key": "validation_run", "status": "pass", "title": "Validation 1", "detail": "Opus 5.5 · high · 1 prompt (async) · 52 min · sent prompt = Opening Prompt · its own lane", "blocking": False, "fix": None},
        {"key": "provenance", "status": "pass", "title": "Your words", "detail": "no red overlap · typed 4,112 characters, pasted 0", "blocking": False, "fix": None},
        {"key": "gists", "status": "pass", "title": "Gists", "detail": "conversation log, final diff (secret, austeane)", "blocking": False, "fix": None},
        {"key": "lint", "status": "warn", "title": "Local checks", "detail": "0 errors · 2 warnings", "blocking": False, "fix": None}]

    t = tasks["d3m0aaa4"]
    t["summary"].update(claude_min=52, my_min=61)
    t["studio"].update(status="Writing", saved_at=_ts(60 * 20), feedback=(
        "Explain what \"re-check\" means in the success criterion. All criteria must be unambiguous on their own. "
        "Criteria 1 and 8 are not mentioned in the prompt."))
    t["checklist"] = [
        {"key": "studio_sync", "status": "pass", "title": "Saved to Studio", "detail": "yesterday 16:02 · nothing changed since", "blocking": False, "fix": None},
        {"key": "ratings", "status": "pass", "title": "Ratings pass", "detail": "Personal-scale · Task Success 3 · Steering 2", "blocking": False, "fix": None},
        {"key": "provenance", "status": "pass", "title": "Your words", "detail": "no red overlap", "blocking": False, "fix": None}]
    t = tasks["d3m0aaa8"]
    t["summary"].update(claude_min=6, my_min=24)
    t["discard"] = {"url": config.DISCARD_FORM + "?usp=pp_url", "logged_at": None,
                    "facts": {"Task": "Task d3m0aaa8", "Repo": "acme/playlog @9f1e2d3c", "Reason": "Too easy: the model solved it",
                              "Slot": "Modify · Web frontend · Personal-scale · Casual prose · Interactive/pairing",
                              "Model and effort": "Opus 5.5 - high", "Outcome": "Solved on the first try (one-shot)",
                              "Turns after the prompt": "0", "Minutes": "24"},
                    "yours": ["In one or two lines: what specifically was the task?",
                              "In one or two lines: why did you think the task would be hard?",
                              "In one or two lines: did the model make any mistakes at all? If so what?"]}

    # One red overlap on the details task to show the guard.
    dp = tasks["d3m0aaa7"]["sections"]["decision_points"]
    txt = dp["text"]
    s = txt.find("Worse")
    if s > 0:
        dp["overlap"] = [{"id": "ov1", "level": "red", "start": s + 7, "end": s + 7 + 42, "text": txt[s + 7:s + 49],
                          "accepted": False, "source": {"kind": "transcript_claude", "ref": "calib-1#turn2",
                                                         "label": "Claude, calibration 1, turn 2",
                                                         "excerpt": txt[s + 7:s + 49], "match_start": 0, "match_end": 42}}]
    return {"tasks": tasks}


def queue(tasks: dict, lane_state: str, more: bool = False) -> tuple[list, list]:
    items = []
    if more:
        items.append({"id": "blocker_studio", "kind": "blocker_studio", "tier": 0, "short": None,
                      "title": "Studio tab is signed out", "label": "Open Studio", "reason": "3 Studio calls are waiting",
                      "est_s": None, "surface": "blocker", "lane": False, "since": _ts(3), "group": "queue",
                      "primary": {"label": "Open Studio", "action": "studio_open_tab", "args": {}}})
    if lane_state == "waiting":
        items.append({"id": "lane_waiting:d3m0aab3", "kind": "lane_waiting", "tier": 1, "short": "d3m0aab3",
                      "title": "Reply to Claude", "label": tasks["d3m0aab3"]["summary"]["label"],
                      "reason": "Claude has waited 1:12", "est_s": None, "surface": "lane", "lane": True,
                      "since": _ts(1.2), "group": "queue",
                      "primary": {"label": "Open the lane", "action": "lane_open", "args": {}}})
    items += [
        {"id": "decide:d3m0aaa1:calib-1", "kind": "decide", "tier": 3, "short": "d3m0aaa1", "title": "Keep or drop calibration",
         "label": tasks["d3m0aaa1"]["summary"]["label"], "reason": "Calibration 1 finished 2 min ago", "est_s": 120,
         "surface": "decide", "lane": False, "since": _ts(2), "group": "queue", "primary": {"label": "Keep", "action": "keep", "args": {}}},
        {"id": "rate:d3m0aab1:validate-1", "kind": "rate", "tier": 3, "short": "d3m0aab1", "title": "Rate validation 1",
         "label": tasks["d3m0aab1"]["summary"]["label"], "reason": "Validation 1 finished 4 min ago", "est_s": 480,
         "surface": "rate", "lane": False, "since": _ts(4), "group": "queue", "primary": {"label": "Done", "action": "done", "args": {}}},
        *([{"id": "log_discard:d3m0aaa8", "kind": "log_discard", "tier": 3, "short": "d3m0aaa8", "title": "Log the discarded run",
            "label": tasks["d3m0aaa8"]["summary"]["label"], "reason": "Dropped 10 min ago", "est_s": 120, "surface": "log_discard",
            "lane": False, "since": _ts(10), "group": "queue", "primary": {"label": "Logged", "action": "logged", "args": {}}},
           {"id": "feedback:d3m0aaa4", "kind": "feedback", "tier": 4, "short": "d3m0aaa4", "title": "Reviewer sent it back",
            "label": tasks["d3m0aaa4"]["summary"]["label"], "reason": "Sent back yesterday", "est_s": 900, "surface": "feedback",
            "lane": False, "since": _ts(60 * 20), "group": "queue", "primary": {"label": "Save to Studio and rerun AutoQC", "action": "save", "args": {}}}]
          if more else []),
        {"id": "fix_qc:d3m0aaa6", "kind": "fix_qc", "tier": 4, "short": "d3m0aaa6", "title": "Fix 1 AutoQC check",
         "label": tasks["d3m0aaa6"]["summary"]["label"], "reason": "AutoQC failed 1 of 13 checks at 14:31", "est_s": 300,
         "surface": "studio", "lane": False, "since": _ts(12), "group": "queue", "primary": {"label": "Save to Studio and rerun AutoQC", "action": "save", "args": {}}},
        {"id": "write_details:d3m0aaa7", "kind": "write_details", "tier": 5, "short": "d3m0aaa7", "title": "Write details",
         "label": tasks["d3m0aaa7"]["summary"]["label"], "reason": "Kept after calibration 1", "est_s": 1500,
         "surface": "details", "lane": False, "since": _ts(55), "group": "queue", "primary": {"label": "Done, queue validation", "action": "done", "args": {}}},
        {"id": "write_prompt:d3m0aaa9", "kind": "write_prompt", "tier": 5, "short": "d3m0aaa9", "title": "Write the opening prompt",
         "label": tasks["d3m0aaa9"]["summary"]["label"], "reason": "Claimed 20 min ago", "est_s": 600,
         "surface": "write_prompt", "lane": False, "since": _ts(20), "group": "queue", "primary": {"label": "Done, go to validation", "action": "done", "args": {}}},
        {"id": "pick", "kind": "pick", "tier": 6, "short": None, "title": "Pick the next problem",
         "label": "", "reason": "1 task before validation", "est_s": 180, "surface": "pick", "lane": False,
         "since": _ts(60), "group": "queue", "primary": {"label": "Claim", "action": "claim", "args": {}}},
    ]
    running = [{"id": "validating:d3m0aab3", "kind": "validating", "tier": 9, "short": "d3m0aab3",
                "title": "Claude working · validation 1" if lane_state == "working" else "Waiting for your reply · validation 1",
                "label": tasks["d3m0aab3"]["summary"]["label"], "reason": "", "est_s": None, "surface": "lane",
                "lane": True, "since": _ts(16), "group": "running", "primary": None}]
    if lane_state == "waiting":
        running = []
    return items, running


def lane(tasks: dict, state: str) -> dict:
    t = tasks["d3m0aab3"]
    facts = copy.deepcopy(t["runs"][1]["facts"])
    q = None
    if state == "waiting":
        q = ("Before I wire this into the API: should locking a room that isn't loaded yet load it first, "
             "or return 404 like the user endpoints do?")
    return {"state": state, "rate_limited": None, "since": _ts(1.2 if state == "waiting" else 14.3),
            "run": {"short": "d3m0aab3", "run": "validate-1", "kind": "validate", "label": t["summary"]["label"],
                    "mode": "interactive", "container": "prework-demo"},
            "pty": {"alive": True, "clients": 0}, "question": q, "evidence": None, "facts": facts,
            "input_locked": False, "queue": [{"short": "d3m0aaa9", "kind": "calib", "label": "chatd · Live clients + kick", "since": _ts(1)}],
            "auto_prepare": True, "foreign": None}


FINISHED = {
    "rows": [
        {"short": "d3m0aab2", "label": "chatd · Invite link expiry", "repo": "acme/chatd", "slot": "Create · Backend · Personal",
         "status": "In review", "submitted_at": None, "times_submitted": 1, "my_min": 94, "claude_min": 71,
         "runs": ["c1", "v1"], "last": "submitted 2 h ago", "feedback": None},
        {"short": "d3m0aaa4", "label": "flowrun · foreach in templates", "repo": "acme/flowrun", "slot": "Create · Backend · Personal",
         "status": "Sent back", "submitted_at": None, "times_submitted": 1, "my_min": 61, "claude_min": 52,
         "runs": ["c1", "v1"], "last": "sent back yesterday",
         "feedback": "Define what \"re-check\" means in the success criterion. All criteria must be unambiguous on their own."},
        {"short": "d3m0aaa8", "label": "playlog · Desktop presence", "repo": "acme/playlog", "slot": "Modify · Web · Personal",
         "status": "Dropped", "submitted_at": None, "times_submitted": 0, "my_min": 24, "claude_min": 6,
         "runs": ["c1"], "last": "one-shot in 6 min", "feedback": None},
        {"short": "d3m0aaa3", "label": "photobox · Album sort order", "repo": "acme/photobox", "slot": "Modify · Web · Personal",
         "status": "Accepted", "submitted_at": None, "times_submitted": 1, "my_min": 103, "claude_min": 66,
         "runs": ["c1", "v1"], "last": "accepted Tuesday", "feedback": None},
    ],
    "week": {"accepted": 1, "in_review": 1, "sent_back": 1, "dropped": 1, "dropped_reasons": {"one-shot": 1},
             "hours_per_accepted": 1.7, "lane_busy_pct": 68, "claude_waited_min": 41},
    "ledger": [{"source": "lead_estimate", "word": "likely", "n": 1, "one_shot": 1},
               {"source": "lead_estimate", "word": "possible", "n": 2, "one_shot": 0},
               {"source": "lead_estimate", "word": "not_checked", "n": 2, "one_shot": 1}],
    "spend": {"week_usd": 11.4, "per_submitted_usd": 5.7},
}

TERMINAL = [
    "\x1b[2m╭──────────────────────────────────────────────────────────────╮\x1b[0m",
    "\x1b[2m│\x1b[0m \x1b[1m✻ Welcome to Claude Code\x1b[0m                                    \x1b[2m│\x1b[0m",
    "\x1b[2m│\x1b[0m   /exp/d3m0aab3/model_a · Opus 5.5 · effort high             \x1b[2m│\x1b[0m",
    "\x1b[2m╰──────────────────────────────────────────────────────────────╯\x1b[0m",
    "",
    "\x1b[38;5;245m> \x1b[0m\x1b[2m(your opening prompt)\x1b[0m",
    "",
    "\x1b[38;5;75m⏺\x1b[0m I'll start by reading how groups are loaded and where lock state lives.",
    "",
    "\x1b[38;5;75m⏺\x1b[0m \x1b[1mRead\x1b[0m(room/room.go)",
    "  \x1b[2m⎿  Read 1241 lines\x1b[0m",
    "",
    "\x1b[38;5;75m⏺\x1b[0m \x1b[1mUpdate\x1b[0m(room/room.go)",
    "  \x1b[2m⎿  Updated room/room.go with 18 additions and 2 removals\x1b[0m",
    "",
    "\x1b[38;5;75m⏺\x1b[0m \x1b[1mBash\x1b[0m(go test ./room/ ./server/)",
    "  \x1b[2m⎿  ok   github.com/acme/chatd/room       0.41s\x1b[0m",
    "  \x1b[2m   ok   github.com/acme/chatd/server   0.88s\x1b[0m",
    "",
    "\x1b[38;5;75m⏺\x1b[0m Before I wire this into the API: should locking a room that isn't loaded",
    "  yet load it first, or return 404 like the user endpoints do?",
    "",
    "\x1b[2m────────────────────────────────────────────────────────────────────────────\x1b[0m",
    "\x1b[38;5;245m>\x1b[0m ",
]
