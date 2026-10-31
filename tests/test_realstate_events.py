"""The SSE hub (replay, gaps, modes, the wire format) and the engine that publishes real-state changes."""
import asyncio
import time
import unittest

from test_realstate_derive import RealRoot
from johto.web import events


class HubTest(unittest.TestCase):
    def test_replay_inside_the_buffer_and_gaps(self):
        h = events.Hub(size=5)
        ids = [h.publish("queue", {"n": i}) for i in range(8)]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual([e[2]["n"] for e in h.replay(ids[4], "real")], [5, 6, 7])
        self.assertEqual(h.replay(ids[-1], "real"), [])
        self.assertIsNone(h.replay(ids[0], "real"))          # fell out of the buffer
        self.assertIsNone(h.replay(None, "real"))
        self.assertIsNone(h.replay(ids[-1] + 100, "real"))    # an id from another server

    def test_modes(self):
        h = events.Hub()
        start = h.last_id
        h.publish("queue", {"x": 1})
        h.publish("toast", {"x": 2}, mode=None)
        h.publish("queue", {"x": 3}, mode="demo")
        self.assertEqual([e[2]["x"] for e in h.replay(start, "real")], [1, 2])
        self.assertEqual([e[2]["x"] for e in h.replay(start, "demo")], [2, 3])

    def test_ids_start_after_any_earlier_server(self):
        self.assertGreater(events.Hub().last_id, int(time.time() * 1000) - 5000)

    def test_stream_state_then_live_events_and_resume(self):
        async def run():
            h = events.Hub()
            old = events.hub
            events.hub = h
            try:
                async def initial():
                    return {"queue": []}
                gen = events.stream("real", None, initial)
                first = await gen.__anext__()
                self.assertIn("event: state", first)
                self.assertTrue(first.startswith(f"id: {h.last_id}\n"))
                h.publish("task", {"summary": {"short": "a"}, "changed": ["sections"]})
                h.publish("queue", {"queue": []}, mode="demo")       # never reaches a real client
                h.publish("toast", {"text": "hi"})
                got = [await gen.__anext__(), await gen.__anext__()]
                self.assertIn("event: task", got[0])
                self.assertIn("event: toast", got[1])
                lid = int(got[0].split("\n")[0][4:])
                await gen.aclose()
                gen2 = events.stream("real", str(lid), initial)
                again = await gen2.__anext__()
                self.assertIn("event: toast", again)                 # replay, no state
                await gen2.aclose()
                gen3 = events.stream("real", "12", initial)
                self.assertIn("event: state", await gen3.__anext__())
                await gen3.aclose()
                self.assertEqual(h.clients, 0)
            finally:
                events.hub = old
        asyncio.run(run())

    def test_wire_format(self):
        s = events.fmt(7, "lane", {"state": "free"})
        self.assertEqual(s, 'id: 7\nevent: lane\ndata: {"state":"free"}\n\n')
        self.assertEqual(events.fmt(None, "ping", {}), "event: ping\ndata: {}\n\n")


class EngineTest(RealRoot):
    def setUp(self):
        super().setUp()
        self.hub = events.Hub()
        self.engine = events.Engine(self.hub)
        self.engine.recompute()                       # prime: no task events for what was already there
        self.mark = self.hub.last_id

    def published(self):
        return [(e[1], e[2]) for e in self.hub.replay(self.mark, "real")]

    def test_a_file_change_publishes_task_and_queue(self):
        self.put("bdecide1", decisions={"calib-1": {"decision": "keep", "at": "2026-10-30T22:00:00-0700"}})
        self.engine.recompute()
        names = [n for n, _ in self.published()]
        self.assertIn("queue", names)
        task = [d for n, d in self.published() if n == "task"]
        self.assertEqual(task[0]["summary"]["short"], "bdecide1")
        self.assertEqual(task[0]["summary"]["stage"], "details")
        self.assertIn("decisions", task[0]["changed"])

    def test_nothing_changed_publishes_nothing(self):
        self.engine.recompute()
        self.assertEqual(self.published(), [])

    def test_section_edit_names_sections(self):
        sub = self.root / "tasks" / "cdetail1" / "submission.md"
        sub.write_text(sub.read_text().replace("## Decision Points\n", "## Decision Points\nLorem better.\n"))
        self.engine.recompute()
        task = [d for n, d in self.published() if n == "task"]
        self.assertEqual(task[0]["changed"], ["sections"])

    def test_removed_task_and_lane_change(self):
        import shutil
        shutil.rmtree(self.root / "tasks" / "aprompt1")
        self.alive = set()
        self.engine.recompute()
        pub = self.published()
        self.assertIn(("task.removed", {"short": "aprompt1"}), pub)
        lane = [d for n, d in pub if n == "lane"]
        self.assertEqual(lane[-1]["state"], "exited")

    def test_pack_job_toast(self):
        self.jobs = [{"id": "pack-1", "kind": "pack", "short": "bdecide1", "run": "calib-1", "state": "running",
                      "progress": {"step": "x"}}]
        self.engine.recompute()
        self.jobs = [{"id": "pack-1", "kind": "pack", "short": "bdecide1", "run": "calib-1", "state": "succeeded",
                      "progress": {"step": "x"}}]
        self.engine.recompute()
        toasts = [d for n, d in self.published() if n == "toast"]
        self.assertTrue(toasts and toasts[-1]["text"].startswith("Review pack ready"))
        self.assertEqual(toasts[-1]["link"], {"label": "Open the task", "href": "/task/bdecide1"})

    def test_jobs_publisher_hook(self):
        old = events.hub
        events.hub = self.hub
        try:
            events.from_jobs("toast", {"text": "saved"})
            events.from_jobs("task", {"short": "x"})          # becomes a recompute, not a raw event
        finally:
            events.hub = old
        names = [n for n, _ in self.published()]
        self.assertEqual(names, ["toast"])

    def test_watcher_filter(self):
        from johto import config
        self.assertEqual(events._relevant(str(config.TASKS / "a" / "task.json")), "tasks")
        self.assertIsNone(events._relevant(str(config.TASKS / "a" / "runs" / "calib-1" / "model_a" / "x.ts")))
        self.assertIsNone(events._relevant(str(config.TASKS / "a" / ".lock")))
        self.assertEqual(events._relevant(str(config.DATA / "scout" / "leads.json")), "leads")
        self.assertIsNone(events._relevant(str(config.DATA / "cache" / "code" / "x")))

    def test_engine_thread_picks_up_a_file_change(self):
        self.engine.start(watch=True)
        try:
            deadline = time.time() + 5
            while not self.engine.watching and time.time() < deadline:
                time.sleep(0.05)
            time.sleep(0.3)
            self.put("bdecide1", decisions={"calib-1": {"decision": "keep", "at": "2026-10-30T22:00:00-0700"}})
            while time.time() < deadline:
                if any(n == "task" and d["summary"]["short"] == "bdecide1" for n, d in self.published()):
                    break
                time.sleep(0.1)
            self.assertTrue(any(n == "task" and d["summary"]["short"] == "bdecide1" for n, d in self.published()))
        finally:
            self.engine.stop()


if __name__ == "__main__":
    unittest.main()
