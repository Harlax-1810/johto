"""The run monitor (johto/web/lane/runmon.py): clocks, the last turn, the review, and never blocking on ssh."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import test_realstate_fixtures as fx
from johto import budget, config, lanes
from johto.web.lane import runmon


def _tx(*, turns=2, minutes=10.0, end_turn=True, last=None, start_ago=30.0, background=False):
    out = fx.transcript(turns=turns, minutes=minutes, end_turn=end_turn, start_ago=start_ago)
    if last is not None:
        out[-1]["message"]["content"] = [{"type": "text", "text": last}]
    if not end_turn:     # Claude is mid-tool-call
        out[-1]["message"]["content"].append({"type": "tool_use", "id": "toolu_open", "name": "Bash",
                                              "input": {"command": "npm run test"}})
    if background:
        # A background shell started in the last turn that hasn't reported back.
        out.insert(-1, {"type": "assistant", "isSidechain": False, "timestamp": out[-2]["timestamp"],
                        "message": {"model": "claude-opus-5-5", "stop_reason": "tool_use", "content": [
                            {"type": "tool_use", "id": "toolu_bg", "name": "Bash",
                             "input": {"command": "npm run dev", "run_in_background": True}}]}})
        out.insert(-1, {"type": "user", "isSidechain": False, "timestamp": out[-2]["timestamp"],
                        "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_bg",
                                                 "content": "Command running in background with ID: bg1"}]},
                        "toolUseResult": {"backgroundTaskId": "bg1"}})
    return out


class MonitorRoot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "prework"
        (self.root / "data").mkdir()
        self.patches = [mock.patch.object(config, "ROOT", self.root), mock.patch.object(config, "DATA", self.root / "data"),
                        mock.patch.object(config, "TASKS", self.root / "tasks"),
                        mock.patch.object(config, "PREWORK_HOME", self.home), mock.patch.object(lanes, "HOME", self.home),
                        mock.patch.object(config, "FAKE", False)]
        for p in self.patches:
            p.start()
        runmon.clear()

    def tearDown(self):
        runmon.stop()
        for p in reversed(self.patches):
            p.stop()
        runmon.clear()
        self._tmp.cleanup()

    def task(self, short, runs, **extra):
        return fx.make_task(self.root, short, sub=fx.submission(prompt=fx.PROMPT), runs=runs, prework_home=self.home,
                            extra=extra)

    def live(self, short="m1", **tx):
        return self.task(short, [{"kind": "calib", "tx": fx.transcript(turns=1, minutes=8.0), "collected_ago": 200},
                                 {"kind": "validate", "tx": None, "live_tx": _tx(**tx), "collected": False,
                                  "created_ago": 40}])


class Compute(MonitorRoot):
    def test_collected_run(self):
        d = self.task("c1", [{"kind": "validate", "tx": fx.transcript(turns=3, minutes=21.0), "collected_ago": 5}])
        m = runmon.compute(d, "validate-1", network=False, up=False)
        self.assertEqual((m["state"], m["host"], m["run_word"], m["turns"]), ("collected", "Mac", "validation 1", 3))
        self.assertAlmostEqual(m["working_minutes"], m["task_minutes"])
        self.assertGreater(m["working_minutes"], 0)
        self.assertIsNone(m["human_wait"])
        self.assertIsNone(m["turn_class"])
        self.assertEqual(m["command"], "johto time c1 validate-1")
        self.assertEqual(m["review"]["state"], "none")

    def test_waiting_run_counts_the_human_wait_and_the_task_budget(self):
        d = self.live(last="Done. The retries now back off.")
        m = runmon.compute(d, "validate-1", network=False, up=True)
        self.assertEqual(m["state"], "waiting")
        self.assertEqual(m["turn_class_source"], "budget")      # REVIEWLOOP's budget.turn_facts
        self.assertIsNone(m["question"])
        self.assertIsNotNone(m["human_wait"])
        self.assertGreaterEqual(m["human_wait"]["minutes"], 0)
        # The task's budget adds the calibration run's minutes to this run's.
        calib = budget.status(d, "calib-1")["working_minutes"]
        self.assertAlmostEqual(m["task_minutes"], round(calib + m["working_minutes"], 1))
        self.assertEqual(m["budget"], budget.budget_minutes())

    def test_question_problem_and_running_turn(self):
        d = self.live("q1", last="I fixed it.\n\nShould I also migrate the old rows?")
        m = runmon.compute(d, "validate-1", network=False, up=True)
        # The turn edited code (substantive) and then asked: the question is kept for Home.
        self.assertIn("migrate the old rows", m["question"])
        from johto.web.domain import queue
        self.assertEqual(queue.turn_of(m), "question")
        d = self.live("w1", end_turn=False, start_ago=10.2)
        m = runmon.compute(d, "validate-1", network=False, up=True)
        self.assertEqual((m["state"], m["turn_class"], m["human_wait"]), ("working", None, None))

    def test_budget_classify_turn_wins_when_it_exists(self):
        d = self.live(last="Lorem.")
        seen = []

        def classify(st, turn):
            seen.append(turn)
            return "progress"
        with mock.patch.object(budget, "turn_facts", None, create=True), \
                mock.patch.object(budget, "classify_turn", classify, create=True):
            m = runmon.compute(d, "validate-1", network=False, up=True)
        self.assertEqual((m["turn_class"], m["turn_class_source"]), ("progress", "budget"))
        self.assertEqual(seen, [2])

    def test_rule_when_budget_has_no_classifier(self):
        with mock.patch.object(budget, "turn_facts", None, create=True), \
                mock.patch.object(budget, "classify_turn", None, create=True):
            d = self.live("n1", last="Done. The retries now back off.")
            m = runmon.compute(d, "validate-1", network=False, up=True)
            self.assertEqual((m["turn_class"], m["turn_class_source"]), ("substantive", "rule"))
            d = self.live("n2", last="Done.\n\nWhich table should it use?")
            m = runmon.compute(d, "validate-1", network=False, up=True)
            self.assertEqual(m["turn_class"], "question")
            self.assertIn("Which table", m["question"])

    def test_lane_down_stops_the_clock(self):
        d = self.live(end_turn=False)
        m = runmon.compute(d, "validate-1", network=False, up=False)
        self.assertEqual((m["state"], m["live"]), ("stopped", False))
        self.assertIsNone(m["turn_class"])

    def test_pc_run_never_uses_the_network_unless_asked(self):
        d = self.task("p1", [{"kind": "validate", "tx": None, "collected": False,
                              "rec": {"host": "build-box", "remote_dir": "/home/a/johto-lanes/p1/validate-1"}}])
        with mock.patch("subprocess.run", side_effect=AssertionError("no ssh")):
            m = runmon.compute(d, "validate-1", network=False)
        self.assertEqual((m["host"], m["state"], m["up"]), ("PC", "unknown", None))
        # Docker stopped on the PC: the run is paused there.
        with mock.patch.object(runmon, "_probe", lambda h, c, n: {"up": None, "why": "Docker isn't running on build-box"}), \
                mock.patch.object(budget, "status", lambda d, r: runmon._collected_status(d, r)):
            m = runmon.compute(d, "validate-1", network=True)
        self.assertEqual(m["state"], "paused")
        self.assertIn("Docker", m["why"])

    def test_pc_run_waiting(self):
        d = self.task("p2", [{"kind": "validate", "tx": None, "collected": False,
                              "rec": {"host": "build-box", "remote_dir": "/x"}}])
        tp = d / "runs" / "validate-1" / ".live-transcripts" / "-exp" / "live.jsonl"
        fx._write_jsonl(tp, _tx(last="Which table should it use?"))

        def status(task_dir, run):
            from johto import transcript
            s = transcript.summarize(tp)
            return {"run": run, "live": True, "transcript": str(tp), "working_minutes": s["working_minutes"],
                    "background_minutes": 0.0, "idle_minutes": 0.0, "turns": s["turns"], "last_turn": s["turns"][-1],
                    "turn_open": False, "background_running": 0, "budget": 90, "line": "x"}
        with mock.patch.object(runmon, "_probe", lambda h, c, n: {"up": True, "why": None}), \
                mock.patch.object(budget, "status", status):
            m = runmon.compute(d, "validate-1", network=True)
        self.assertEqual((m["host"], m["state"]), ("PC", "waiting"))
        self.assertIn("Which table", m["question"])


class Reviews(MonitorRoot):
    def test_reviewing_queued_ready(self):
        d = self.task("r1", [{"kind": "validate", "tx": fx.transcript(turns=3), "collected_ago": 5}])
        run_dir = d / "runs" / "validate-1"
        self.assertEqual(runmon.review_status(d, "validate-1", 3)["state"], "none")
        (run_dir / "review-pack-20261031-120000.xml").write_text("<pack/>")
        (run_dir / "packs.json").write_text(json.dumps([{"path": "review-pack-20261031-120000.xml", "kind": "full",
                                                         "turn": 2, "built_at": fx.iso(10)}]))
        rv = runmon.review_status(d, "validate-1", 3)
        self.assertEqual((rv["state"], rv["turn"], rv["text"], rv["covers_latest"]),
                         ("queued", 2, "Review queued for turn 2", False))
        rd = d / "reviews"
        rd.mkdir()
        (rd / "pending.json").write_text(json.dumps([{"run": "validate-1", "turn": 2, "pack": "runs/validate-1/x.xml",
                                                      "chat_url": "https://chatgpt.com/c/x", "sent_at": fx.iso(3)}]))
        self.assertEqual(runmon.review_status(d, "validate-1", 3)["text"], "Reviewing turn 2")
        (rd / "pending.json").write_text("[]")
        from johto import reviews
        reviews.save(d, "validate-1", 3, "## Digest\nLorem.", pack=run_dir / "review-pack-20261031-120000.xml")
        rv = runmon.review_status(d, "validate-1", 3)
        self.assertEqual((rv["state"], rv["turn"], rv["covers_latest"]), ("ready", 3, True))
        # Two more turns later, the same review is of an earlier turn, and says so.
        rv = runmon.review_status(d, "validate-1", 5)
        self.assertEqual((rv["state"], rv["covers_latest"], rv["text"]), ("ready", False, "Review of turn 3 of 5"))

    def test_backfilled_pack_is_not_queued(self):
        d = self.task("r2", [{"kind": "validate", "tx": fx.transcript(turns=1), "collected_ago": 5}])
        run_dir = d / "runs" / "validate-1"
        (run_dir / "packs.json").write_text(json.dumps([{"path": "review-pack-1.xml", "kind": "full", "turn": 1,
                                                         "built_at": fx.iso(10), "backfilled": True}]))
        self.assertEqual(runmon.review_status(d, "validate-1", 1)["state"], "none")


class WhichRun(MonitorRoot):
    def test_current_run_and_submission_run(self):
        d = self.task("s1", [{"kind": "validate", "n": 1, "tx": fx.transcript(), "collected_ago": 50},
                             {"kind": "validate", "n": 2, "tx": None, "collected": False, "created_ago": 5}])
        meta = json.loads((d / "task.json").read_text())
        self.assertEqual(runmon.current_run(meta, d), "validate-2")     # not collected, not removed
        self.assertEqual(runmon.submission_run(meta, d), "validate-1")  # the latest collected validation run
        meta["runs"][1]["torn_down_at"] = fx.iso(1)
        self.assertEqual(runmon.current_run(meta, d), "validate-1")
        meta["submission_run"] = "validate-2"
        self.assertEqual(runmon.submission_run(meta, d), "validate-2")  # Austin's choice
        meta["submission_run"] = "validate-9"
        self.assertEqual(runmon.submission_run(meta, d), "validate-1")  # a choice whose folder is gone


class Cache(MonitorRoot):
    def test_inline_get_is_cached_until_something_changes(self):
        d = self.task("k1", [{"kind": "validate", "tx": fx.transcript(), "collected_ago": 5}])
        calls = []
        real = runmon.compute

        def counted(*a, **kw):
            calls.append(1)
            return real(*a, **kw)
        with mock.patch.object(runmon, "compute", counted):
            a = runmon.get(d, "validate-1", up=False)
            b = runmon.get(d, "validate-1", up=False)
            self.assertEqual(len(calls), 1)
            self.assertEqual(a, b)
            runmon.get(d, "validate-1", up=True)       # the lane came up: measured again
            self.assertEqual(len(calls), 2)

    def test_the_thread_never_blocks_a_request(self):
        d = self.task("k2", [{"kind": "validate", "tx": fx.transcript(), "collected_ago": 5}])
        gate = threading.Event()
        real = runmon.compute

        def slow(*a, **kw):
            gate.wait(5)            # a PC that takes its time
            return real(*a, **kw)
        pokes = []
        runmon.listen(lambda: pokes.append(1))
        with mock.patch.object(runmon, "compute", slow):
            runmon.start()
            t0 = time.time()
            self.assertIsNone(runmon.get(d, "validate-1"))
            self.assertLess(time.time() - t0, 0.5)
            gate.set()
            for _ in range(100):
                if runmon.get(d, "validate-1"):
                    break
                time.sleep(0.05)
        self.assertEqual(runmon.get(d, "validate-1")["state"], "collected")
        self.assertTrue(pokes)
        runmon._listeners.clear()

    def test_public_drops_what_changes_every_time(self):
        p = runmon.public({"run": "validate-1", "computed_at": "x", "human_wait": {"since": "s", "minutes": 3.2}})
        self.assertEqual(p, {"run": "validate-1", "human_wait": {"since": "s"}})
        self.assertIsNone(runmon.public(None))


if __name__ == "__main__":
    unittest.main()
