"""The brief routes (johto/web/api_brief.py) and the dossier worker's brief hook, with a stub claude."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from test_brief import DID, FakeClaude  # noqa: E402
from test_dossier_api import call  # noqa: E402

from johto.dossier import brief, generate, store, worker  # noqa: E402
from johto.web import jobs  # noqa: E402

FIXTURE = "acme__widget__9f1e2d3c"


class Routes(FakeClaude):
    def setUp(self):
        super().setUp()
        os.environ["FAKE_PLAN"] = json.dumps(["valid"])

    def test_list(self):
        status, body = call("GET", "/api/briefs")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"items": [{"id": DID, "state": "missing", "generated_at": None, "verdict": None}]})
        self.install_example()
        _, body = call("GET", "/api/briefs")
        self.assertEqual((body["items"][0]["state"], body["items"][0]["verdict"]), ("ready", "dive"))

    def test_get_missing_serves_the_fallback(self):
        status, body = call("GET", f"/api/briefs/{DID}")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"id", "state", "brief", "fallback", "error", "progress"})
        self.assertEqual((body["state"], body["brief"]), ("missing", None))
        self.assertEqual(len(body["fallback"]["seeds"]), 3)

    def test_get_ready(self):
        self.install_example()
        status, body = call("GET", f"/api/briefs/{DID}")
        self.assertEqual((status, body["state"], body["fallback"]), (200, "ready", None))
        seed = body["brief"]["seeds"][0]
        self.assertEqual(set(seed) >= {"one_shot", "size", "fits", "upstream", "used"}, True)
        self.assertIn("facts", body["brief"])
        self.assertFalse(body["brief"]["stale"])

    def test_unknown_is_404(self):
        status, body = call("GET", "/api/briefs/nobody__here__00000000")
        self.assertEqual((status, body["error"]["code"]), (404, "not_found"))
        status, _ = call("POST", "/api/briefs/nobody__here__00000000/regenerate")
        self.assertEqual(status, 404)

    def test_needs_the_session(self):
        status, _ = call("GET", "/api/briefs", cookie=False)
        self.assertEqual(status, 401)

    def test_regenerate_runs_a_job(self):
        status, body = call("POST", f"/api/briefs/{DID}/regenerate")
        self.assertEqual(status, 202, body)
        job = jobs.wait(body["job"]["id"], 30)
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(job["result"]["state"], "ready")
        self.assertEqual(job["cost_usd"], 0.75)
        self.assertEqual(brief.state(DID), "ready")

    def test_regenerate_is_409_while_generating(self):
        brief._set(DID, status="generating", pid=os.getpid())
        brief._current[DID] = generate.Cancel()
        try:
            status, body = call("POST", f"/api/briefs/{DID}/regenerate")
        finally:
            brief._current.pop(DID)
        self.assertEqual((status, body["error"]["code"]), (409, "conflict"))

    def test_regenerate_says_research_is_off_in_fake_mode(self):
        with mock.patch.object(brief, "CLAUDE_BIN", None), mock.patch.object(brief.config, "FAKE", True), \
                mock.patch.object(brief.config, "CLAUDE_BIN_SET", False):
            status, body = call("POST", f"/api/briefs/{DID}/regenerate")
        self.assertEqual(status, 202)
        self.assertIn("Research is off", body["toast"]["text"])
        self.assertFalse((self.tmp / "args.0").exists())

    def test_demo_reads_research_files_only(self):
        self.install_example()
        brief._set(DID, status="failed", error="timed out")
        boom = mock.Mock(side_effect=AssertionError("demo read real state"))
        with mock.patch.object(brief, "task_metas", boom), mock.patch.object(brief, "status", boom), \
                mock.patch.object(brief, "state", boom):
            status, body = call("GET", "/api/briefs", demo=True)
            self.assertEqual(status, 200)
            ids = {it["id"]: it for it in body["items"]}
            self.assertEqual(ids[FIXTURE]["state"], "missing")
            self.assertEqual(ids[DID]["state"], "ready")
            status, body = call("GET", f"/api/briefs/{FIXTURE}", demo=True)
            self.assertEqual((status, body["state"]), (200, "missing"))
            self.assertEqual(len(body["fallback"]["seeds"]), 3)
            status, body = call("GET", f"/api/briefs/{DID}", demo=True)
            self.assertEqual((body["state"], body["error"], body["brief"]["used"]), ("ready", None, []))

    def test_demo_cannot_regenerate(self):
        status, body = call("POST", f"/api/briefs/{DID}/regenerate", demo=True)
        self.assertEqual(status, 202)
        self.assertIn("Demo data", body["toast"]["text"])
        self.assertNotIn("job", body)
        self.assertFalse((self.tmp / "args.0").exists())


class WorkerHook(FakeClaude):
    def setUp(self):
        super().setUp()
        os.environ["FAKE_PLAN"] = json.dumps(["valid"])

    def test_a_dossier_written_ready_queues_its_brief(self):
        with store.edit() as idx:
            idx["items"][0]["status"] = "queued"
        doc = json.loads(store.dossier_path(DID).read_text())
        res = generate.Result(ok=True, doc=doc, cost_usd=1.0, elapsed_s=10, model="m", passes=1)
        with mock.patch.object(worker.generate, "generate", lambda *a, **k: res):
            worker.run_item(DID, model="claude")   # whatever the real settings say
        self.assertEqual(brief.state(DID), "queued")

    def test_start_queues_missing_and_stale_briefs(self):
        w = worker.Worker(echo=lambda s: None)
        self.assertEqual(w.queue_briefs(), [DID])
        self.assertEqual(w.queue_briefs(), [])

    def test_step_runs_a_queued_brief_before_dossiers(self):
        worker.set_paused(False)
        brief.queue(DID)
        w = worker.Worker(echo=lambda s: None)
        w._force_fill = False
        w._last_fill = time.time()
        self.assertTrue(w.step())
        self.assertEqual(brief.state(DID), "ready")
        self.assertFalse(w.step(), "nothing left to do")

    def test_pause_stops_briefs_too(self):
        worker.set_paused(True, "test")
        brief.queue(DID)
        w = worker.Worker(echo=lambda s: None)
        w._force_fill = False
        w._last_fill = time.time()
        self.assertFalse(w.step())
        self.assertEqual(brief.state(DID), "queued")
        self.assertFalse((self.tmp / "args.0").exists())

    def test_pause_with_stop_asks_running_briefs_to_stop(self):
        brief._set(DID, status="generating", pid=12345)
        st = worker.set_paused(True, "test", stop=True)
        self.assertIn(DID, st["stopping"])
        self.assertEqual(brief._load_status()["stop"], {DID: worker.STOPPED_BY_PAUSE})

    def test_status_keeps_its_keys_and_brief_current_names_the_brief(self):
        brief._current[DID] = generate.Cancel()
        try:
            st = worker.status()
            self.assertEqual(brief.current(), DID)
        finally:
            brief._current.pop(DID)
        self.assertNotIn("brief", st)
        self.assertIsNone(st["current"])


if __name__ == "__main__":
    unittest.main()
