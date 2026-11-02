"""turnwatch: a finished lane turn gets packed and sent once, short turns aren't sent, restarts don't resend,
an unreachable PC is skipped, nudges go out once, a dry run writes nothing, and one watcher runs at a time.
The review policy: only meaningful turns are reviewed (async: each substantive turn; interactive: the first,
then a changed diff; assessments: a new report); questions, permission waits and problems are said out loud,
everything else quietly; partial packs and stale sends are handled.

Run: PYTHONPATH=.:tests .venv/bin/python -m unittest tests.test_turnwatch -v
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from johto import budget, config, stages, turnwatch, workspace  # noqa: E402

KEY = "PREWORK_zz01-abc"
CONTAINER = f"prework-{KEY}-model_a"
RKEY = "PREWORK_zz02-def"
RCONTAINER = f"prework-{RKEY}-model_a"


def iso_ago(s: float) -> str:
    return datetime.fromtimestamp(time.time() - s, tz=timezone.utc).isoformat()


def turn(i, ended="end_turn", wm=12.0, ago=60):
    return {"index": i, "start": iso_ago(ago + wm * 60), "end": iso_ago(ago), "working_minutes": wm,
            "background_minutes": 0.0, "ended": ended, "prompt": f"prompt {i}"}


def status(turns, wm=None):
    wm = sum(t["working_minutes"] for t in turns) if wm is None else wm
    return {"run": "validate-1", "live": True, "working_minutes": wm, "background_minutes": 0.0,
            "idle_minutes": 0.0, "turns": turns, "last_turn": turns[-1] if turns else None,
            "turn_open": bool(turns) and turns[-1]["ended"] == "open", "budget": 90,
            "line": f"Claude has worked {wm:.0f} of 90 minutes."}


class FakeBudget:
    def __init__(self):
        self.st = {}
        self.calls = []

    def status(self, task_dir, run=None):
        self.calls.append((Path(task_dir).name, run))
        return self.st[(Path(task_dir).name, run)]

    def nudge_due(self, st, sent):
        return budget.nudge_due(st, sent)   # the real rule: the highest threshold reached, once


def facts(kind="substantive", **kw) -> dict:
    out = {"kind": kind, "why": kind, "asks": None, "report_chars": 0, "pending_tool": None, "permission": None,
           "problem": None, "edits": 1 if kind == "substantive" else 0}
    out.update(kw)
    return out


class ClassifyingBudget(FakeBudget):
    """A budget with turn_facts: `facts[turn index]` (else substantive); `seen` facts when a screen is given."""

    def __init__(self):
        super().__init__()
        self.facts, self.seen, self.fact_calls = {}, None, []

    def turn_facts(self, st, tr, screen=None, min_working_minutes=None):
        self.fact_calls.append((tr["index"], screen, st.get("background_running")))
        if screen is not None and self.seen is not None:
            return self.seen
        return self.facts.get(tr["index"], facts())


class FakeDelta:
    def __init__(self):
        self.calls = []
        self.fail = 0
        self.log_event = False
        self.sent_packs = set()

    def next_pack(self, task_dir, run=None, log=print, full=False):
        self.calls.append((Path(task_dir).name, run))
        self.full = full
        if self.fail:
            self.fail -= 1
            raise RuntimeError("repomix not found")
        p = Path(task_dir) / "runs" / run / f"pack-{len(self.calls)}.xml"
        self.reviewed = True
        if self.log_event:
            stages.log(task_dir, "pack_built", run=run, path=f"runs/{run}/{p.name}", pack_kind="full")
        return p

    def pack_record(self, task_dir, run, path):
        return {"path": str(path), "sent_at": "x"} if Path(path).name in self.sent_packs else None


class CheckpointDelta(FakeDelta):
    """A delta with diff_changed: None until a pack was built, then `moved`."""

    def __init__(self):
        super().__init__()
        self.reviewed, self.moved = False, False

    def diff_changed(self, task_dir, run):
        return self.moved if self.reviewed else None


class FakeReviews:
    def __init__(self):
        self.sent = []
        self.pend = {}
        self.saved = []
        self.replies = []

    def send_review(self, task_dir, run=None, pack=None, no_send=False, log=print):
        if self.replies:
            return self.replies.pop(0)
        self.sent.append((Path(task_dir).name, run, str(pack)))
        return {"chat_url": "https://chatgpt.com/c/abc", "sent": True, "chars": 10, "usage_pct": 60}

    def pending(self, task_dir):
        return self.pend.get(Path(task_dir).name, [])

    def fetch_pending(self, task_dir=None, log=print):
        out, self.saved = self.saved, []
        return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="johto-turnwatch-test-"))
        self.saved = {k: getattr(config, k) for k in ("ROOT", "DATA", "TASKS", "PREWORK_HOME", "FAKE", "REAL_OK")}
        config.ROOT, config.DATA, config.TASKS = self.tmp, self.tmp / "data", self.tmp / "tasks"
        config.PREWORK_HOME, config.FAKE = self.tmp / "prework", False
        config.REAL_OK = True          # the temp root stands in for the checkout's data
        config.DATA.mkdir(parents=True)
        self.d = self.add_task("zz01", {"kind": "validate", "dir": "runs/validate-1", "container": CONTAINER})
        self.proj = config.PREWORK_HOME / "cc-config" / f"{KEY}_model_a" / "projects" / "-w"
        self.proj.mkdir(parents=True)
        self.transcript("x" * 10)
        self.budget, self.delta, self.reviews = FakeBudget(), FakeDelta(), FakeReviews()
        self.notes, self.running, self.ssh_calls = [], {CONTAINER}, []
        self.ssh_reply = None
        self.patches = [
            mock.patch.object(turnwatch, "_budget", lambda: self.budget),
            mock.patch.object(turnwatch, "_delta", lambda: self.delta),
            mock.patch.object(turnwatch, "_reviews", lambda: self.reviews),
            mock.patch.object(turnwatch, "_running_local", lambda: set(self.running)),
            mock.patch.object(turnwatch, "_ssh", self.fake_ssh),
            mock.patch.object(turnwatch, "_notify", self.fake_notify),
            mock.patch.object(turnwatch, "_screen", self.fake_screen),
        ]
        for p in self.patches:
            p.start()
        os.environ["JOHTO_NO_NOTIFY"] = "1"
        self.lines, self.loud, self.screens, self.screen = [], [], [], None

    def fake_notify(self, title, body, href=None, attention=False):
        self.notes.append((title, body))
        if attention:
            self.loud.append(body)

    def fake_screen(self, r):
        self.screens.append(r["key"])
        return self.screen

    def tearDown(self):
        for p in self.patches:
            p.stop()
        for k, v in self.saved.items():
            setattr(config, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_task(self, short, *runs) -> Path:
        d = config.TASKS / short
        for rec in runs:
            rd = d / rec["dir"]
            rd.mkdir(parents=True, exist_ok=True)
            (rd / "prework-manifest.json").write_text(json.dumps({"lane": {"container": rec["container"]}}))
        workspace.save(d, {"task_id": f"id-{short}", "task_name": f"Task {short}", "runs": list(runs)})
        return d

    def fake_ssh(self, host, script, timeout=20):
        self.ssh_calls.append((host, script))
        return self.ssh_reply

    def transcript(self, text: str, age_s: float = 60) -> None:
        p = self.proj / "s.jsonl"
        with open(p, "a") as f:
            f.write(text)
        t = time.time() - age_s
        os.utime(p, (t, t))

    def tick(self, dry_run=False):
        return turnwatch.tick(dry_run=dry_run, log=self.lines.append)

    def state(self) -> dict:
        return json.loads((config.DATA / "turnwatch.json").read_text())

    def kinds(self, d=None) -> list[str]:
        return [e["kind"] for e in stages.read(d or self.d)]


class NewTurns(Base):
    def test_ended_turn_is_packed_and_sent_once(self):
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12)])
        acts = self.tick()
        self.assertEqual(self.delta.calls, [("zz01", "validate-1")])
        self.assertEqual(len(self.reviews.sent), 1)
        self.assertTrue(self.reviews.sent[0][2].endswith("pack-1.xml"))
        self.assertEqual([a["action"] for a in acts], ["turn_ended", "pack", "send"])
        self.assertEqual(self.kinds(), ["turn_ended", "pack_built"])
        ev = stages.last(self.d, "turn_ended")
        self.assertEqual((ev["run"], ev["turn"], ev["working_minutes"]), ("validate-1", 1, 12.0))
        self.assertEqual(len(self.notes), 1)
        self.assertIn("sent to GPT Pro", self.notes[0][1])
        rec = self.state()["runs"]["zz01/validate-1"]["turns"]["1"]
        self.assertEqual(rec["status"], "sent")
        self.assertEqual(rec["chat_url"], "https://chatgpt.com/c/abc")
        # nothing changed: no new read, no new pack
        self.tick()
        self.assertEqual(len(self.budget.calls), 1)
        self.assertEqual(len(self.reviews.sent), 1)

    def test_a_run_with_no_chat_gets_a_full_pack_only_when_it_will_be_sent(self):
        chats = {}
        self.reviews.chat_for = lambda d, run: chats.get(run)
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12)])
        self.tick()
        self.assertTrue(self.delta.full)                 # no chat yet: a full pack starts one
        chats["validate-1"] = "https://chatgpt.com/c/abc"
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12, ago=600), turn(2, wm=12)])
        self.transcript("y" * 5)
        self.tick()
        self.assertFalse(self.delta.full)                # the chat exists: a follow-up
        (config.DATA / "settings.json").write_text(json.dumps({"auto_review": False}))
        chats.clear()
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12, ago=900), turn(2, wm=12, ago=600),
                                                         turn(3, wm=12)])
        self.transcript("z" * 5)
        self.tick()
        self.assertEqual(len(self.delta.calls), 3)
        self.assertFalse(self.delta.full)                # he pastes by hand: follow-ups for his own chat

    def test_pack_built_is_logged_once_when_delta_logs_it(self):
        self.delta.log_event = True
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12)])
        self.tick()
        self.assertEqual(self.kinds(), ["turn_ended", "pack_built"])

    def test_pack_already_sent_is_not_sent_again(self):
        self.delta.sent_packs = {"pack-1.xml"}
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12)])
        self.tick()
        self.assertEqual(self.reviews.sent, [])
        self.assertEqual(self.state()["runs"]["zz01/validate-1"]["turns"]["1"]["status"], "sent")

    def test_waits_for_a_quiet_transcript(self):
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12, ago=5)])
        self.transcript("y", age_s=5)
        self.tick()
        self.assertEqual(self.delta.calls, [])
        self.transcript("", age_s=60)
        self.tick()
        self.assertEqual(len(self.delta.calls), 1)

    def test_short_turn_is_not_sent(self):
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=1.0)])
        self.tick()
        self.assertEqual(self.delta.calls, [])
        self.assertEqual(self.reviews.sent, [])
        self.assertEqual(self.kinds(), ["turn_ended"])
        self.assertIn("too short", self.notes[0][1])
        self.assertEqual(self.state()["runs"]["zz01/validate-1"]["turns"]["1"]["status"], "short")

    def test_auto_review_off_builds_the_pack_only(self):
        (config.DATA / "settings.json").write_text(json.dumps({"auto_review": False}))
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12)])
        self.tick()
        self.assertEqual(len(self.delta.calls), 1)
        self.assertEqual(self.reviews.sent, [])
        self.assertIn("review pack ready", self.notes[0][1])

    def test_a_scratch_copy_builds_packs_but_never_sends(self):
        config.REAL_OK = False
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12)])
        acts = self.tick(dry_run=True)
        self.assertIn("would not send it", acts[-1]["detail"])
        self.tick()
        self.assertEqual(len(self.delta.calls), 1)
        self.assertEqual(self.reviews.sent, [])
        self.assertIn("review pack ready", self.notes[0][1])

    def test_next_turn_gets_its_own_pack(self):
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12, ago=600)])
        self.tick()
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12, ago=600), turn(2, wm=8)])
        self.transcript("more")
        self.tick()
        self.assertEqual(len(self.delta.calls), 2)
        self.assertEqual(len(self.reviews.sent), 2)
        self.assertEqual(self.state()["runs"]["zz01/validate-1"]["last_turn"], 2)

    def test_turn_he_already_answered_is_not_reviewed(self):
        self.budget.st[("zz01", "validate-1")] = status([turn(1, ended="open")])
        self.tick()
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12), turn(2, ended="open", wm=1)])
        self.transcript("more")
        self.tick()
        self.assertEqual(self.delta.calls, [])
        self.assertEqual(self.kinds(), ["turn_ended"])
        self.assertEqual(self.state()["runs"]["zz01/validate-1"]["turns"]["1"]["status"], "moved_on")

    def test_turn_waiting_on_a_background_job_waits(self):
        self.budget.st[("zz01", "validate-1")] = dict(status([turn(1, wm=12)]), background_running=1)
        self.tick()
        self.assertEqual(self.delta.calls, [])
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=15)])
        self.transcript("done")
        self.tick()
        self.assertEqual(len(self.delta.calls), 1)

    def test_a_background_job_that_never_ends_holds_the_review_for_a_few_minutes_only(self):
        self.budget.st[("zz01", "validate-1")] = dict(status([turn(1, wm=12, ago=60)]), background_running=1)
        self.tick()
        self.assertEqual(self.delta.calls, [])
        recheck = self.state()["runs"]["zz01/validate-1"]["recheck_at"]
        self.assertLessEqual(recheck, time.time() + turnwatch.BUSY_READ_S + 1)
        # five minutes later, same transcript: the dev server is still up, so the review goes anyway
        self.budget.st[("zz01", "validate-1")] = dict(status([turn(1, wm=12, ago=400)]), background_running=1)
        st = self.state()
        st["runs"]["zz01/validate-1"]["recheck_at"] = time.time() - 1
        turnwatch.save_state(st)
        self.tick()
        self.assertEqual(len(self.delta.calls), 1)
        self.assertNotIn("recheck_at", self.state()["runs"]["zz01/validate-1"])

    def test_turn_that_continues_after_a_background_job_is_reviewed_again(self):
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12, ago=900)])
        self.tick()
        # a background notice woke Claude up and it worked 10 more minutes in the same turn
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=22, ago=60)])
        self.transcript("more")
        self.tick()
        self.assertEqual(len(self.reviews.sent), 2)
        self.assertTrue(stages.last(self.d, "turn_ended")["continued"])
        # a few seconds more is not a new review
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=22.5, ago=30)])
        self.transcript("tiny")
        self.tick()
        self.assertEqual(len(self.reviews.sent), 2)

    def test_old_turns_on_a_new_run_are_marked_seen(self):
        self.budget.st[("zz01", "validate-1")] = status([turn(1, ago=7300), turn(2, ended="interrupted", ago=7200),
                                                          turn(3, ago=3600)], wm=50)
        self.transcript("", age_s=3600)
        acts = self.tick()
        self.assertEqual(self.delta.calls, [])
        self.assertEqual(self.notes, [])          # the 45-minute nudge passed long ago: no notification
        self.assertEqual([a["action"] for a in acts], ["baseline"])
        ent = self.state()["runs"]["zz01/validate-1"]
        self.assertEqual((ent["last_turn"], ent["nudges"]), (3, [45]))
        self.assertEqual(sorted(ent["turns"]), ["1", "2", "3"])

    def test_torn_down_and_stopped_runs_are_ignored(self):
        workspace.save(self.d, {"task_name": "Task zz01", "runs": [
            {"kind": "validate", "dir": "runs/validate-1", "container": CONTAINER, "torn_down_at": "x"}]})
        self.budget.st[("zz01", "validate-1")] = status([turn(1)])
        self.tick()
        workspace.save(self.d, {"task_name": "Task zz01", "runs": [
            {"kind": "validate", "dir": "runs/validate-1", "container": CONTAINER}]})
        self.running = set()
        self.tick()
        self.assertEqual(self.budget.calls, [])


class Restart(Base):
    def test_restart_never_repacks_or_resends(self):
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12)])
        self.tick()
        # a new process reads the saved state; the transcript grew (an /exit) but the turn is the same
        self.transcript("/exit")
        self.tick()
        self.assertEqual(len(self.delta.calls), 1)
        self.assertEqual(len(self.reviews.sent), 1)
        self.assertEqual(self.kinds().count("turn_ended"), 1)

    def test_send_cut_off_by_a_restart_is_not_sent_twice(self):
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12)])
        st = {"runs": {"zz01/validate-1": {"where": "this Mac", "seen": None, "read_at": 0, "last_turn": 1,
                                           "baselined": True, "nudges": [], "turns": {"1": {
                                               "status": "sending", "pid": -5, "pack": "/p/pack-1.xml",
                                               "attempts": 0, "end": None, "working_minutes": 12}}}}}
        turnwatch.save_state(st)
        self.reviews.pend["zz01"] = [{"run": "validate-1", "pack": "runs/validate-1/pack-1.xml", "chat_url": "u"}]
        self.tick()
        self.assertEqual(self.reviews.sent, [])
        self.assertEqual(self.state()["runs"]["zz01/validate-1"]["turns"]["1"]["status"], "sent")

    def test_failures_retry_with_backoff_then_give_up(self):
        self.delta.fail = 10
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12)])
        self.tick()
        rec = self.state()["runs"]["zz01/validate-1"]["turns"]["1"]
        self.assertEqual((rec["status"], rec["attempts"]), ("failed", 1))
        self.assertIn("repomix not found", rec["error"])
        self.assertIn("Trying again", self.notes[-1][1])
        self.tick()                                  # not due yet
        self.assertEqual(len(self.delta.calls), 1)
        for n in range(2, turnwatch.MAX_ATTEMPTS + 1):
            st = self.state()
            st["runs"]["zz01/validate-1"]["turns"]["1"]["next_try"] = 0
            turnwatch.save_state(st)
            self.tick()
            self.assertEqual(len(self.delta.calls), n)
        rec = self.state()["runs"]["zz01/validate-1"]["turns"]["1"]
        self.assertEqual(rec["status"], "gave_up")
        self.assertEqual(self.notes[-1][0], "Johto: review not sent")
        self.assertEqual(len([n for n in self.notes if "Trying again" in n[1]]), 1)
        self.tick()
        self.assertEqual(len(self.delta.calls), turnwatch.MAX_ATTEMPTS)

    def test_waits_for_the_last_review_before_sending_the_next(self):
        busy = {"sent": False, "reason": "the review of pack-0.xml (sent 12:00) isn't back yet"}
        self.reviews.replies = [busy, busy]
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12)])
        self.tick()
        rec = self.state()["runs"]["zz01/validate-1"]["turns"]["1"]
        self.assertEqual((rec["status"], rec["attempts"]), ("queued", 0))
        self.assertIn("when the last review is back", self.notes[-1][1])
        for _ in range(2):                           # still busy, then free: sent, with no new notice in between
            st = self.state()
            st["runs"]["zz01/validate-1"]["turns"]["1"]["next_try"] = 0
            turnwatch.save_state(st)
            self.tick()
        self.assertEqual(len(self.reviews.sent), 1)
        self.assertEqual(len(self.delta.calls), 1)
        self.assertEqual([n[1].split(" · ")[-1] for n in self.notes],
                         ["it goes to GPT Pro when the last review is back", "sent to GPT Pro"])

    def test_nothing_new_is_not_a_failure(self):
        self.reviews.replies = [{"sent": False, "reason": "nothing new: pack-1.xml already has a saved review"}]
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12)])
        self.tick()
        rec = self.state()["runs"]["zz01/validate-1"]["turns"]["1"]
        self.assertEqual((rec["status"], rec.get("attempts")), ("sent", 0))

    def test_a_missing_chatgpt_module_does_not_stop_the_watcher(self):
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12)])
        with mock.patch.object(self.reviews, "send_review", side_effect=SystemExit("ChatGPT automation isn't available")):
            self.tick()
        rec = self.state()["runs"]["zz01/validate-1"]["turns"]["1"]
        self.assertEqual(rec["status"], "failed")
        self.assertIn("isn't available", rec["error"])

    def test_retry_after_pack_only_resends(self):
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12)])
        with mock.patch.object(self.reviews, "send_review", side_effect=RuntimeError("ChatGPT tab not found")):
            self.tick()
        st = self.state()
        st["runs"]["zz01/validate-1"]["turns"]["1"]["next_try"] = 0
        turnwatch.save_state(st)
        self.tick()
        self.assertEqual(len(self.delta.calls), 1)      # the pack from the first try is reused
        self.assertEqual(len(self.reviews.sent), 1)
        self.assertEqual(self.state()["runs"]["zz01/validate-1"]["turns"]["1"]["status"], "sent")


class Policy(Base):
    def setUp(self):
        super().setUp()
        self.budget, self.delta = ClassifyingBudget(), CheckpointDelta()

    def slot(self, **slot):
        meta = workspace.load(self.d)
        meta["slot"] = {"action": "Modify", "interaction_mode": "Interactive/pairing", **slot}
        workspace.save(self.d, meta)

    def next_turn(self, n, **kw):
        self.budget.st[("zz01", "validate-1")] = status([turn(i, wm=12, ago=600 + 60 * (n - i)) for i in range(1, n)]
                                                        + [turn(n, **kw)])
        self.transcript(f"turn {n}")
        return self.tick()

    def test_first_substantive_turn_then_only_a_changed_diff(self):
        self.slot()
        self.next_turn(1)
        self.assertEqual(len(self.reviews.sent), 1)
        self.next_turn(2)                                   # substantive, but the diff is what GPT saw
        self.assertEqual(len(self.reviews.sent), 1)
        self.assertEqual(len(self.delta.calls), 1)          # no pack either
        ev = stages.last(self.d, "turn_ended")
        self.assertEqual((ev["turn"], ev["turn_kind"], ev["review"]), (2, "substantive", False))
        self.assertEqual(self.state()["runs"]["zz01/validate-1"]["turns"]["2"]["status"], "checkpoint")
        self.assertIn("no review: the code hasn't changed since the last review", self.notes[-1][1])
        self.assertEqual(self.loud, [])
        self.delta.moved = True
        self.next_turn(3)
        self.assertEqual(len(self.reviews.sent), 2)

    def test_progress_and_question_turns_only_save_a_checkpoint(self):
        self.slot()
        self.budget.facts = {1: facts("progress"), 2: facts("question", asks="Should I also fix the importer?")}
        self.next_turn(1, wm=12)
        self.assertEqual((self.delta.calls, self.reviews.sent), ([], []))
        self.assertIn("no review: a progress update", self.notes[-1][1])
        self.next_turn(2)
        self.assertEqual((self.delta.calls, self.reviews.sent), ([], []))
        self.assertEqual(self.loud, ["Claude asked you something: Should I also fix the importer?"])
        self.assertEqual([e["turn_kind"] for e in stages.read(self.d, ("turn_ended",))], ["progress", "question"])
        self.assertEqual(stages.last(self.d, "turn_ended")["asked"], "Should I also fix the importer?")

    def test_a_substantive_turn_that_asks_is_reviewed_and_said_out_loud(self):
        self.slot()
        self.budget.facts = {1: facts(asks="Want me to run the slow suite too?")}
        self.next_turn(1)
        self.assertEqual(len(self.reviews.sent), 1)
        self.assertEqual(self.loud, ["Claude asked you something: Want me to run the slow suite too?"])
        self.assertIn("sent to GPT Pro", self.notes[-1][1])

    def test_async_reviews_each_substantive_turn(self):
        self.slot(interaction_mode="Async/autonomous")
        self.next_turn(1)
        self.next_turn(2)
        self.assertEqual(len(self.reviews.sent), 2)

    def test_an_assessment_reviews_a_new_report(self):
        self.slot(action="Understand / Assess")
        self.next_turn(1)
        self.budget.facts = {2: facts(edits=0, report_chars=4000, why="a 4,000-character report")}
        self.next_turn(2)
        self.assertEqual(len(self.reviews.sent), 2)

    def test_problem_turns_are_said_out_loud_and_not_reviewed(self):
        self.budget.facts = {1: facts("problem", problem="usage limit reached (resets 4pm)")}
        self.next_turn(1)
        self.assertEqual(self.delta.calls, [])
        self.assertEqual(self.loud, ["Claude needs you: usage limit reached (resets 4pm)"])
        self.assertEqual(self.state()["runs"]["zz01/validate-1"]["turns"]["1"]["status"], "problem")

    def test_every_turn_policy_reviews_progress_turns_too(self):
        (config.DATA / "settings.json").write_text(json.dumps({"review_policy": "every_turn"}))
        self.budget.facts = {1: facts("progress"), 2: facts("progress")}
        self.next_turn(1)
        self.next_turn(2, wm=1.0)                            # still under review_min_working_minutes
        self.assertEqual(len(self.reviews.sent), 1)

    def test_background_wait_is_over_when_the_turn_is_judged(self):
        self.budget.st[("zz01", "validate-1")] = dict(status([turn(1, wm=12, ago=400)]), background_running=1)
        self.tick()
        self.assertEqual(len(self.reviews.sent), 1)
        self.assertEqual(self.budget.fact_calls[-1], (1, None, 0))

    def test_dry_run_says_the_kind_and_the_decision(self):
        self.budget.facts = {1: facts("progress")}
        self.budget.st[("zz01", "validate-1")] = status([turn(1)])
        acts = self.tick(dry_run=True)
        self.assertIn("(12 minutes, progress)", acts[0]["detail"])
        self.assertEqual(acts[1]["detail"], "would save a checkpoint without a review: a progress update")


class Waiting(Base):
    def setUp(self):
        super().setUp()
        self.budget = ClassifyingBudget()
        self.budget.st[("zz01", "validate-1")] = status([turn(1, ended="open", wm=4)])

    def test_ask_user_question_is_said_once(self):
        self.budget.facts = {1: facts("question", asks="Which branch should I keep?",
                                      pending_tool={"id": "tu1", "name": "AskUserQuestion", "detail": "x"})}
        self.tick()
        self.assertEqual(self.loud, ["Claude asked you something: Which branch should I keep?"])
        self.assertEqual(self.screens, [])                  # the transcript says it all
        self.assertEqual(stages.last(self.d, "claude_waiting")["turn_kind"], "question")
        self.transcript("more")
        self.tick()
        self.assertEqual(len(self.loud), 1)

    def test_a_tool_call_with_no_result_gets_one_look_at_the_screen(self):
        self.budget.facts = {1: facts("progress", pending_tool={"id": "tu2", "name": "Bash", "detail": "npm test"})}
        self.screen = {"permission": True, "input_visible": False}
        self.budget.seen = facts("permission", permission="Bash npm test")
        self.tick()
        self.assertEqual(self.screens, ["zz01/validate-1"])
        self.assertEqual(self.loud, ["Claude is waiting for permission: Bash npm test"])
        self.transcript("more")
        self.tick()
        self.assertEqual(len(self.screens), 1)              # once per tool call

    def test_a_running_tool_is_not_news(self):
        self.budget.facts = {1: facts("progress", pending_tool={"id": "tu3", "name": "Bash", "detail": "make"})}
        self.screen = {"permission": False, "input_visible": False}
        self.budget.seen = facts("progress")
        self.tick()
        self.assertEqual((self.loud, self.notes), ([], []))
        self.assertEqual(self.state()["runs"]["zz01/validate-1"]["asked_tool"], "tu3")

    def test_dry_run_does_not_read_the_screen(self):
        self.budget.facts = {1: facts("progress", pending_tool={"id": "tu4", "name": "Bash", "detail": "make"})}
        acts = self.tick(dry_run=True)
        self.assertEqual(self.screens, [])
        self.assertIn("would look at the lane's screen", acts[0]["detail"])


class SendOutcomes(Base):
    def test_a_partial_pack_waits_for_austin(self):
        self.reviews.replies = [{"sent": False, "why": "partial", "pack": "/p/pack-1.xml",
                                 "reason": "pack-1.xml is partial (2 hunks rejected in a.go)"}]
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12)])
        self.tick()
        rec = self.state()["runs"]["zz01/validate-1"]["turns"]["1"]
        self.assertEqual((rec["status"], rec.get("attempts")), ("partial", 0))
        self.assertIn("--force", self.notes[-1][1])
        st = self.state()
        st["runs"]["zz01/validate-1"]["turns"]["1"]["next_try"] = 0
        turnwatch.save_state(st)
        self.tick()
        self.assertEqual(self.reviews.replies, [])          # not retried

    def test_an_unconfirmed_send_goes_back_through_send_review(self):
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12)])
        st = {"runs": {"zz01/validate-1": {"where": "this Mac", "seen": None, "read_at": 0, "last_turn": 1,
                                           "baselined": True, "nudges": [], "turns": {"1": {
                                               "status": "sending", "pid": -5, "pack": "/p/pack-1.xml",
                                               "attempts": 0, "end": None, "working_minutes": 12}}}}}
        turnwatch.save_state(st)
        self.reviews.pend["zz01"] = [{"run": "validate-1", "pack": "runs/validate-1/pack-1.xml", "chat_url": "u",
                                      "state": "unconfirmed"}]
        self.reviews.replies = [{"sent": False, "why": "busy", "pack": "/x/runs/validate-1/pack-1.xml",
                                 "reason": "the review of pack-1.xml (sent 12:00) isn't back yet"}]
        self.tick()
        self.assertEqual(self.reviews.replies, [])          # send_review was asked (it reads the chat first)
        rec = self.state()["runs"]["zz01/validate-1"]["turns"]["1"]
        self.assertEqual(rec["status"], "sent")

    def test_a_send_with_no_reply_for_hours_is_dropped_with_a_notice(self):
        self.running = set()
        self.reviews.pend["zz01"] = [{"run": "validate-1", "turn": 2}]
        self.reviews.expire_pending = lambda task_dir=None, log=print: [
            {"task": "zz01", "run": "validate-1", "turn": 2, "chat_url": "https://chatgpt.com/c/old"}]
        acts = self.tick()
        self.assertEqual([a["action"] for a in acts], ["review_expired"])
        self.assertEqual(self.notes[-1][0], "Johto: no GPT Pro reply")
        self.assertIn("zz01 validate-1 turn 2 · nothing after 3 hours", self.notes[-1][1])


class Notify(unittest.TestCase):
    def test_only_attention_makes_a_sound(self):
        runs = []
        with mock.patch.object(turnwatch, "_quiet", return_value=False), \
                mock.patch.object(turnwatch.shutil, "which", return_value="/bin/terminal-notifier"), \
                mock.patch.object(turnwatch.subprocess, "run", side_effect=lambda cmd, **kw: runs.append(cmd)):
            turnwatch._notify("t", "quiet one")
            turnwatch._notify("t", "loud one", attention=True)
        self.assertNotIn("-sound", runs[0])
        self.assertEqual(runs[1][runs[1].index("-sound") + 1], "default")
        runs.clear()
        with mock.patch.object(turnwatch, "_quiet", return_value=False), \
                mock.patch.object(turnwatch.shutil, "which", return_value=None), \
                mock.patch.object(turnwatch.subprocess, "run", side_effect=lambda cmd, **kw: runs.append(cmd)):
            turnwatch._notify("t", "quiet")
            turnwatch._notify("t", "loud", attention=True)
        self.assertNotIn("sound name", runs[0][2])
        self.assertIn('sound name "Glass"', runs[1][2])


class Remote(Base):
    def setUp(self):
        super().setUp()
        self.running = set()
        self.add_task("zz02", {"kind": "validate", "dir": "runs/validate-2", "container": RCONTAINER,
                               "host": "pc", "remote_dir": "/home/u/johto-lanes/zz02/validate-2", "port_slot": 1})

    def reply(self, rc=0, docker_rc=0, mtime=None, size=500, names=(RCONTAINER,)):
        now = time.time()
        out = f"{now:.6f}\n@@docker\n" + "".join(n + "\n" for n in names) + f"@@rc {docker_rc}\n"
        out += f"{RKEY} {(mtime or now - 120):.6f} {size}\n"
        return subprocess.CompletedProcess(["ssh"], rc, out, "")

    def test_one_ssh_per_host_reads_a_changed_transcript(self):
        self.budget.st[("zz02", "validate-2")] = status([turn(1, wm=20)])
        self.ssh_reply = self.reply()
        self.tick()
        self.assertEqual([h for h, _ in self.ssh_calls], ["pc"])
        self.assertIn(RKEY, self.ssh_calls[0][1])
        self.assertIn("docker ps", self.ssh_calls[0][1])
        self.assertEqual(len(self.reviews.sent), 1)
        mtime = self.state()["runs"]["zz02/validate-2"]["seen"][0]
        self.ssh_reply = self.reply(mtime=mtime)       # same version: no new read
        self.tick()
        self.assertEqual(len(self.budget.calls), 1)

    def test_unreachable_host_or_stopped_docker_is_skipped(self):
        self.budget.st[("zz02", "validate-2")] = status([turn(1, wm=20)])
        for reply in (None, subprocess.CompletedProcess(["ssh"], 255, "", "timeout"), self.reply(docker_rc=1)):
            self.ssh_reply = reply
            acts = self.tick()
            self.assertEqual(self.budget.calls, [])
            self.assertEqual(self.reviews.sent, [])
        self.assertEqual([a["action"] for a in acts], [])   # the run was never seen, so nothing to report
        acts = self.tick(dry_run=True)
        self.assertIn("Docker isn't running on pc", acts[0]["detail"])

    def test_probe_parses_running_containers_and_stats(self):
        self.ssh_reply = subprocess.CompletedProcess(["ssh"], 0, f"1.5\n@@docker\n{RCONTAINER}\nother\n@@rc 0\n"
                                                     f"{RKEY} 10.0 100\n{RKEY} 12.0 50\n", "")
        p = turnwatch.probe_host("pc", [RKEY, "bad key;rm"])
        self.assertEqual((p["error"], p["now"], p["running"]), (None, 1.5, {RCONTAINER}))
        self.assertEqual(p["sigs"], {RKEY: [12.0, 150]})
        self.assertNotIn("bad key", self.ssh_calls[0][1])


class Nudges(Base):
    def test_each_threshold_nudges_once(self):
        self.budget.st[("zz01", "validate-1")] = status([turn(1, ended="open", wm=50)], wm=50)
        self.transcript("a", age_s=1)
        self.tick()
        self.assertEqual([n[0] for n in self.notes], ["Johto: 45 Claude minutes"])
        self.assertEqual(stages.last(self.d, "budget_nudge")["threshold"], 45)
        self.budget.st[("zz01", "validate-1")] = status([turn(1, ended="open", wm=52)], wm=52)
        st = self.state()
        st["runs"]["zz01/validate-1"]["read_at"] = 0
        turnwatch.save_state(st)
        self.tick()
        self.assertEqual(len(self.notes), 1)
        # two thresholds at once: one notification, for the higher
        self.budget.st[("zz01", "validate-1")] = status([turn(1, ended="open", wm=91)], wm=91)
        st = self.state()
        st["runs"]["zz01/validate-1"]["read_at"] = 0
        turnwatch.save_state(st)
        self.tick()
        self.assertEqual([n[0] for n in self.notes][1:], ["Johto: 90 Claude minutes"])
        self.assertEqual(self.state()["runs"]["zz01/validate-1"]["nudges"], [45, 90])

    def test_busy_reads_are_spaced_out(self):
        self.budget.st[("zz01", "validate-1")] = status([turn(1, ended="open", wm=5)], wm=5)
        for _ in range(3):
            self.transcript("a", age_s=1)
            self.tick()
        self.assertEqual(len(self.budget.calls), 1)


class DryRun(Base):
    def test_dry_run_writes_nothing(self):
        self.budget.st[("zz01", "validate-1")] = status([turn(1, wm=12)], wm=50)
        self.reviews.pend["zz01"] = [{"run": "validate-1", "turn": 1, "chat_url": "u"}]
        acts = self.tick(dry_run=True)
        self.assertFalse((config.DATA / "turnwatch.json").exists())
        self.assertFalse((self.d / "events.jsonl").exists())
        self.assertEqual((self.delta.calls, self.reviews.sent, self.notes), ([], [], []))
        self.assertEqual([a["action"] for a in acts], ["turn_ended", "pack", "send", "nudge", "fetch"])
        self.assertTrue(all(a["dry_run"] for a in acts))
        text = "\n".join(self.lines)
        self.assertIn("1 run not torn down: 1 live, 0 stopped", text)
        self.assertIn("zz01 validate-1 on this Mac: 1 turn", text)
        self.assertIn("would send it to GPT Pro", text)
        self.assertFalse(list(config.DATA.glob("turnwatch*")))


class Reviews(Base):
    def test_saved_reply_is_announced_once(self):
        self.running = set()
        p = self.d / "reviews" / "validate-1-t03-gptpro.md"
        self.reviews.pend["zz01"] = [{"run": "validate-1"}]
        self.reviews.saved = [p]
        acts = self.tick()
        self.assertEqual([(a["action"], a["turn"]) for a in acts], [("review_saved", 3)])
        self.assertEqual(self.notes, [("Johto: GPT Pro review ready", "zz01 validate-1 turn 3 · "
                                       "reviews/validate-1-t03-gptpro.md")])
        self.reviews.saved = [p]
        st = self.state()
        st["fetched_at"] = 0
        turnwatch.save_state(st)
        self.tick()
        self.assertEqual(len(self.notes), 1)

    def test_fetch_is_skipped_with_nothing_pending(self):
        with mock.patch.object(self.reviews, "fetch_pending") as fp:
            self.tick()
        fp.assert_not_called()


class Lock(Base):
    def test_one_watcher_at_a_time(self):
        fd = turnwatch.acquire()
        self.assertIsNotNone(fd)
        self.assertEqual(turnwatch.holder(), os.getpid())
        self.assertIsNone(turnwatch.acquire())
        a = argparse.Namespace(once=True, dry_run=False, interval=20)
        with self.assertRaises(SystemExit) as cm:
            turnwatch.cmd_watch(a)
        self.assertIn(str(os.getpid()), str(cm.exception))
        turnwatch.release(fd)
        fd2 = turnwatch.acquire()
        self.assertIsNotNone(fd2)
        turnwatch.release(fd2)

    def test_stale_pid_does_not_block(self):
        turnwatch.lock_path().write_text("999999\n")
        fd = turnwatch.acquire()
        self.assertIsNotNone(fd)
        self.assertEqual(turnwatch.holder(), os.getpid())
        turnwatch.release(fd)

    def test_the_web_app_runs_it_in_a_thread_until_stopped(self):
        ticks = []
        with mock.patch.object(turnwatch, "tick", lambda dry_run=False, log=print: ticks.append(1) or []), \
                mock.patch.object(turnwatch, "_stamped", lambda msg: None):
            self.assertTrue(turnwatch.start(interval=0.01))
            deadline = time.time() + 3
            while len(ticks) < 2 and time.time() < deadline:
                time.sleep(0.01)
            self.assertIsNone(turnwatch.acquire())      # the thread holds the one-watcher lock
            turnwatch.stop()
            turnwatch._THREAD.join(3)
        self.assertGreaterEqual(len(ticks), 2)
        fd = turnwatch.acquire()
        self.assertIsNotNone(fd)
        turnwatch.release(fd)

    def test_the_web_app_steps_aside_for_a_terminal_watcher(self):
        fd = turnwatch.acquire()
        said = []
        try:
            with mock.patch.object(turnwatch, "_stamped", said.append):
                self.assertFalse(turnwatch.start())
        finally:
            turnwatch.release(fd)
        self.assertIn("already running", said[0])

    def test_reviews_always_go_out_by_default(self):
        from johto import settings
        self.assertIsNone(settings.DEFAULTS["chatgpt_warn_usage_pct"])
        self.assertTrue(settings.DEFAULTS["auto_review"])

    def test_dry_run_needs_no_lock(self):
        fd = turnwatch.acquire()
        self.running = set()
        try:
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                turnwatch.cmd_watch(argparse.Namespace(once=True, dry_run=True, interval=20))
        finally:
            turnwatch.release(fd)
        self.assertIn("1 run not torn down: 0 live, 1 stopped", out.getvalue())


class Command(unittest.TestCase):
    def test_register_adds_watch(self):
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="cmd")
        turnwatch.register(sub)
        a = p.parse_args(["watch", "--once", "--dry-run", "--interval", "5"])
        self.assertEqual((a.once, a.dry_run, a.interval, a.func), (True, True, 5.0, turnwatch.cmd_watch))
        a = p.parse_args(["watch"])
        self.assertEqual((a.once, a.dry_run, a.interval), (False, False, 20.0))


if __name__ == "__main__":
    unittest.main()
