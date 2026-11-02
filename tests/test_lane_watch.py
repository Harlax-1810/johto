"""lane_watch: collect once Claude has exited and the transcript is quiet; skip a lane where only /login ran;
one collect at a time per run; phases reach listeners; the open hook gets the first chance at the page; the pack
is delta.next_pack's (no outside review unless settings say so), or the one already made for that checkpoint."""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from johto import config, delta, workspace
from johto.web import api_pack, lane_watch
from johto.web.lane import monitor


def jl(*entries) -> str:
    return "".join(json.dumps(e) + "\n" for e in entries)


HUMAN = {"type": "user", "timestamp": "2026-10-30T20:00:00.000Z", "message": {"role": "user", "content": "go"}}
LOGIN = [{"type": "user", "timestamp": "2026-10-30T20:00:00.000Z",
          "message": {"role": "user", "content": "<command-name>/login</command-name>"}}]
CLAUDE = {"type": "assistant", "timestamp": "2026-10-30T20:01:00.000Z",
          "message": {"model": "claude-opus-5-5", "stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]}}


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="johto-watch-test-"))
        self.saved = {k: getattr(config, k) for k in ("ROOT", "DATA", "TASKS", "PREWORK_HOME")}
        config.ROOT, config.DATA, config.TASKS = self.tmp, self.tmp / "data", self.tmp / "tasks"
        config.PREWORK_HOME = self.tmp / "prework"
        config.DATA.mkdir(parents=True)
        self.d = config.TASKS / "zz09"
        run_dir = self.d / "runs" / "calib-1"
        run_dir.mkdir(parents=True)
        self.container = "johtotest-PREWORK_zz09-abc-model_a"
        (run_dir / "prework-manifest.json").write_text(json.dumps({"lane": {"container": self.container}}))
        (self.d / "task.json").write_text(json.dumps({"task_id": "t", "task_name": "Task zz09", "runs": [
            {"kind": "calib", "dir": "runs/calib-1", "container": self.container}]}))
        self.proj = config.PREWORK_HOME / "cc-config" / "PREWORK_zz09-abc_model_a" / "projects" / "-w"
        self.proj.mkdir(parents=True)
        self.saved_prefix = monitor.PREFIX
        monitor.PREFIX = "johtotest-"
        os.environ["JOHTO_NO_NOTIFY"] = "1"
        lane_watch.STATE.clear()
        self.collected = []

        class L:
            def collect(_, d, run):
                self.collected.append((d.name, run))
                return {"summary": {"follow_ups": [], "active_minutes": 1.0}}
        self.patches = [
            mock.patch.object(lane_watch, "_lanes", lambda: L()),
            mock.patch.object(monitor, "running_containers", lambda: [self.container]),
            mock.patch.object(delta, "checkpoint_packed", lambda d, run: self.packed),
            mock.patch.object(delta, "next_pack", self.next_pack),
        ]
        self.packed, self.built, self.build_error = None, [], None
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        for k, v in self.saved.items():
            setattr(config, k, v)
        monitor.PREFIX = self.saved_prefix
        lane_watch.STATE.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def next_pack(self, d, run=None, log=print, **kw):
        self.built.append((Path(d).name, run, kw))
        log("Pack: /x/pack.xml (1,000 tokens)")
        if self.build_error:
            raise SystemExit(self.build_error)
        return Path("/x/pack.xml")

    def transcript(self, text: str, age_s: float = 60) -> Path:
        p = self.proj / "s.jsonl"
        p.write_text(text)
        t = time.time() - age_s
        os.utime(p, (t, t))
        return p

    def tick_and_wait(self):
        before = {t.name for t in threading.enumerate()}
        lane_watch.tick()
        for t in threading.enumerate():
            if t.name.startswith("collect-") and t.name not in before:
                t.join(10)

    def test_collects_after_exit_and_quiet(self):
        self.transcript(jl(HUMAN, CLAUDE))
        seen = []
        lane_watch.listen(lambda k, e: seen.append((k, e["phase"])))
        opened = []
        lane_watch.OPEN_HOOK = lambda short, frag: opened.append((short, frag)) or True
        try:
            with mock.patch.object(monitor, "claude_alive", return_value=False):
                self.tick_and_wait()
        finally:
            lane_watch.OPEN_HOOK = None
            lane_watch._LISTENERS.clear()
        self.assertEqual(self.collected, [("zz09", "calib-1")])
        self.assertEqual([p for k, p in seen if k == "zz09/calib-1"], ["collecting", "packing", "done"])
        self.assertEqual(opened, [("zz09", "field=goal")])
        rec = workspace.load(self.d)["runs"][0]
        self.assertIn("transcript_mtime", rec["auto"])
        self.assertEqual(rec["auto"]["pack"], "/x/pack.xml")
        # the same transcript version is not collected twice
        with mock.patch.object(monitor, "claude_alive", return_value=False):
            self.tick_and_wait()
        self.assertEqual(len(self.collected), 1)

    def test_the_pack_is_the_next_one_with_no_outside_review_forced(self):
        before = set(api_pack.JOBS)
        out = lane_watch.collect_run(self.d, "calib-1", pack=True)
        self.assertEqual((out["phase"], out["pack"]), ("done", "/x/pack.xml"))
        self.assertEqual(self.built, [("zz09", "calib-1", {})])   # ai is left to settings pack_outside_review
        job = next(api_pack.JOBS[j] for j in set(api_pack.JOBS) - before)
        self.assertEqual((job["state"], job["out"], job["log"]), ("done", "/x/pack.xml", ["Pack: /x/pack.xml (1,000 tokens)"]))

    def test_a_checkpoint_already_packed_is_not_packed_again(self):
        self.packed = {"path": "/x/review-followup-1.xml"}
        out = lane_watch.collect_run(self.d, "calib-1", pack=True)
        self.assertEqual((out["phase"], out["pack"]), ("done", "/x/review-followup-1.xml"))
        self.assertEqual(self.built, [])
        self.assertEqual(workspace.load(self.d)["runs"][0]["auto"]["pack"], "/x/review-followup-1.xml")

    def test_a_pack_job_already_running_is_waited_on(self):
        job = {"id": "j1", "short": "zz09", "run": "calib-1", "state": "running", "out": None, "error": None,
               "_done": threading.Event()}
        api_pack.JOBS["j1"] = job
        try:
            threading.Timer(0.2, lambda: (job.update(state="done", out="/x/by-hand.xml"), job["_done"].set())).start()
            out = lane_watch.collect_run(self.d, "calib-1", pack=True)
        finally:
            api_pack.JOBS.pop("j1", None)
        self.assertEqual(out["pack"], "/x/by-hand.xml")
        self.assertEqual(self.built, [])

    def test_a_failed_pack_is_a_phase(self):
        self.build_error = "Couldn't clone acme/widget, so no pack was made: boom"
        out = lane_watch.collect_run(self.d, "calib-1", pack=True)
        self.assertEqual(out["phase"], "failed")
        self.assertIn("Couldn't clone", out["message"])

    def test_waits_while_claude_runs_or_the_transcript_moves(self):
        self.transcript(jl(HUMAN, CLAUDE))
        with mock.patch.object(monitor, "claude_alive", return_value=True):
            self.tick_and_wait()
        self.transcript(jl(HUMAN, CLAUDE), age_s=1)
        with mock.patch.object(monitor, "claude_alive", return_value=False):
            self.tick_and_wait()
        self.assertEqual(self.collected, [])

    def test_login_only_lane_is_not_a_run(self):
        self.transcript(jl(*LOGIN))
        with mock.patch.object(monitor, "claude_alive", return_value=False):
            self.tick_and_wait()
        self.assertEqual(self.collected, [])

    def test_one_collect_at_a_time(self):
        self.transcript(jl(HUMAN, CLAUDE))
        gate = threading.Event()

        class Slow:
            def collect(_, d, run):
                gate.wait(5)
                self.collected.append((d.name, run))
                return {"summary": {}}
        with mock.patch.object(lane_watch, "_lanes", lambda: Slow()):
            t = threading.Thread(target=lane_watch.collect_run, args=(self.d, "calib-1"), kwargs={"pack": False})
            t.start()
            time.sleep(0.2)
            second = lane_watch.collect_run(self.d, "calib-1", pack=False)
            self.assertEqual(second["phase"], "collecting", "the second call leaves the running one alone")
            gate.set()
            t.join(5)
        self.assertEqual(self.collected, [("zz09", "calib-1")])
        self.assertEqual(lane_watch.phase("zz09", "calib-1"), "done")

    def test_lane_start_waits_for_a_collect_in_flight(self):
        from johto.web.lane import service
        self.transcript(jl(HUMAN, CLAUDE))
        gate = threading.Event()

        class Slow:
            def collect(_, d, run):
                gate.wait(5)
                return {"summary": {}}
        with mock.patch.object(lane_watch, "_lanes", lambda: Slow()):
            t = threading.Thread(target=lane_watch.collect_run, args=(self.d, "calib-1"), kwargs={"pack": False})
            t.start()
            time.sleep(0.2)
            out = {}
            w = threading.Thread(target=lambda: out.update(service._collect_and_wait(self.d, "calib-1")))
            w.start()
            time.sleep(1.5)
            self.assertTrue(w.is_alive(), "still waiting while the watcher collects")
            gate.set()
            w.join(5)
            t.join(5)
        self.assertEqual(out.get("phase"), "done")

    def test_collect_failure_is_a_phase_not_a_crash(self):
        self.transcript(jl(HUMAN, CLAUDE))

        class Broken:
            def collect(_, d, run):
                raise RuntimeError("prework collect failed in runs/calib-1")
        with mock.patch.object(lane_watch, "_lanes", lambda: Broken()):
            res = lane_watch.collect_run(self.d, "calib-1")
        self.assertEqual(res["phase"], "failed")
        self.assertIn("prework collect failed", res["message"])


if __name__ == "__main__":
    unittest.main()


class NotifyTests(unittest.TestCase):
    """Design 6: one notification per run, never while a Johto tab is visible and focused; a hidden tab gets a web
    notification over SSE; with no tab, macOS shows it (never in fake mode)."""

    def setUp(self):
        from johto.web import app, events
        self.app, self.events = app, events
        self.presence = dict(app.PRESENCE)
        self.published = []
        self.patches = [mock.patch.object(events.hub, "publish", lambda name, data, mode="real": self.published.append((name, data))),
                        mock.patch.object(lane_watch.subprocess, "run"), mock.patch.object(config, "FAKE", False)]
        for p in self.patches:
            p.start()
        self.run = lane_watch.subprocess.run
        os.environ.pop("JOHTO_NO_NOTIFY", None)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.app.PRESENCE.clear()
        self.app.PRESENCE.update(self.presence)
        os.environ["JOHTO_NO_NOTIFY"] = "1"

    def test_focused_tab_gets_nothing(self):
        self.app.PRESENCE.update(visible=True, focused=True)
        with mock.patch.object(lane_watch, "_tabs", return_value=1):
            lane_watch._notify("Johto: validation 1 ready", "x", "/")
        self.assertEqual((self.published, self.run.call_count), ([], 0))

    def test_hidden_tab_gets_a_web_notification(self):
        self.app.PRESENCE.update(visible=False, focused=False)
        with mock.patch.object(lane_watch, "_tabs", return_value=1):
            lane_watch._notify("Johto: validation 1 ready", "x", "/#item=rate:a", tag="a/validate-1")
        self.assertEqual([n for n, _ in self.published], ["notify"])
        self.assertEqual(self.published[0][1]["href"], "/#item=rate:a")
        self.assertEqual(self.run.call_count, 0)

    def test_no_tab_uses_macos_and_fake_mode_stays_quiet(self):
        with mock.patch.object(lane_watch, "_tabs", return_value=0):
            lane_watch._notify("Johto: validation 1 ready", "x", "/")
            self.assertEqual(self.run.call_count, 1)
            with mock.patch.object(config, "FAKE", True):
                lane_watch._notify("Johto: validation 1 ready", "x", "/")
            self.assertEqual(self.run.call_count, 1)

    def test_summary_plurals(self):
        self.assertEqual(lane_watch._summary_line({"follow_ups": ["a"], "active_minutes": 3.5}), "1 follow-up, 3.5 working minutes")
        self.assertEqual(lane_watch._summary_line({"follow_ups": [], "active_minutes": 1}), "0 follow-ups, 1 working minute")
