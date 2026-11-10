"""The dossier routes, called through Starlette's router with a real ASGI scope (no httpx needed)."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from johto import config
from johto.dossier import store, worker
from johto.web import api_dossier, app as webapp
from test_dossier_fixtures import valid_doc


def call(method, path, body=None, cookie=True, demo=False):
    application = webapp.create_app()
    cookies = []
    if cookie:
        cookies.append(f"johto={webapp.TOKEN}")
    if demo:
        cookies.append("johto_mode=demo")
    # The page's own headers: dossier writes check Host and Origin like every other write (build-plan 0.7).
    headers = [(b"content-type", b"application/json"), (b"host", b"127.0.0.1:8765"), (b"origin", b"http://127.0.0.1:8765")]
    if cookies:
        headers.append((b"cookie", "; ".join(cookies).encode()))
    raw = json.dumps(body).encode() if body is not None else b""
    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": headers, "scheme": "http", "server": ("127.0.0.1", 8765), "client": ("127.0.0.1", 1),
             "root_path": "", "http_version": "1.1"}
    sent = {"body": b""}

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            sent["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            sent["body"] += msg.get("body", b"")

    asyncio.run(application.router(scope, receive, send))  # router only: no lifespan, no worker thread
    return sent["status"], json.loads(sent["body"] or b"null")


class ApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        store.use_data_dir(Path(self._tmp.name))
        self.p = [mock.patch.object(worker.WORKER, "kick"), mock.patch.object(worker.WORKER, "start"),
                  mock.patch.object(worker.settings, "path", lambda: Path(self._tmp.name) / "settings.json"),
                  mock.patch.object(api_dossier.candidates, "open_combos",
                                    lambda: {("Modify", "Backend / API", "Enterprise-grade"): 45})]
        for p in self.p:
            p.start()
        pool = [{"full_name": f"o/r{i}", "commit": f"{i:040x}", "pinned_url": "u", "language": "Go",
                 "promise": 0.5, "reasons": [], "fits": [{"action": "Create", "domain": "Games",
                                                          "codebase_type": "Personal-scale", "open": 2}]}
                for i in range(1, 5)]
        worker.fill(pool)
        self.ids = [it["id"] for it in store.load()["items"]]
        # r2 ready with a dossier, r1 generating, r3 declined, r4 queued
        doc = valid_doc()
        doc["id"] = self.ids[1]
        store.save_dossier(doc)
        store.update(self.ids[1], status="ready")
        store.update(self.ids[0], status="generating", progress={"step": "reading the code"})
        store.update(self.ids[2], status="declined", closed_at="2026-10-30T10:00:00-0700")

    def tearDown(self):
        for p in self.p:
            p.stop()
        store.use_data_dir(config.DATA)
        self._tmp.cleanup()

    def test_auth_required(self):
        self.assertEqual(call("GET", "/api/dossiers", cookie=False)[0], 401)
        self.assertEqual(call("POST", f"/api/dossiers/{self.ids[1]}/act", {}, cookie=False)[0], 401)

    def test_list_order_and_summary(self):
        status, body = call("GET", "/api/dossiers")
        self.assertEqual(status, 200)
        self.assertEqual((body["target"], body["paused"]), (10, True))
        self.assertEqual(set(body["worker"]), {"running", "current", "busy", "paused", "paused_reason",
                                               "backoff_until", "model"})
        self.assertEqual([i["status"] for i in body["items"]], ["ready", "generating", "queued", "declined"])
        ready = body["items"][0]
        self.assertEqual(ready["headline"], valid_doc()["headline"])
        self.assertEqual(len(ready["top"]), 3)
        self.assertEqual(set(ready["top"][0]), {"id", "title", "area", "one_shot", "slot_fit", "size"})
        self.assertEqual(ready["top"][0]["size"], "M")
        self.assertEqual(ready["fits"], [{"action": "Modify", "domain": "Backend / API",
                                          "codebase_type": "Enterprise-grade", "open": 45}])
        queued = body["items"][2]
        self.assertIsNone(queued["headline"])
        self.assertEqual(queued["fits"][0]["domain"], "Games")
        self.assertEqual(queued["fits"][0]["open"], 0)  # open counts always come from slots.json

    def test_get_one(self):
        status, body = call("GET", f"/api/dossiers/{self.ids[1]}")
        self.assertEqual(status, 200)
        self.assertEqual(body["item"]["status"], "ready")
        self.assertEqual(body["dossier"]["id"], self.ids[1])
        status, body = call("GET", f"/api/dossiers/{self.ids[3]}")
        self.assertEqual((status, body["dossier"]), (200, None))
        status, body = call("GET", "/api/dossiers/nope__nope__00000000")
        self.assertEqual((status, body["error"]["code"]), (404, "not_found"))

    def test_decline_act_restore(self):
        did = self.ids[1]
        s, b = call("POST", f"/api/dossiers/{did}/decline", {"reason": "bogus"})
        self.assertEqual((s, b["error"]["code"]), (400, "bad_request"))
        s, b = call("POST", f"/api/dossiers/{did}/decline", {"reason": "too_big", "note": "two weeks"})
        self.assertEqual((s, b["item"]["status"], b["item"]["decline"]["note"]), (200, "declined", "two weeks"))
        worker.WORKER.kick.assert_called()
        s, b = call("POST", f"/api/dossiers/{did}/restore")
        self.assertEqual((s, b["item"]["status"]), (200, "ready"))
        s, b = call("POST", f"/api/dossiers/{did}/act", {"opportunity_id": "o2", "task_short": "abc12345"})
        self.assertEqual((s, b["item"]["status"], b["item"]["actioned"]["opportunity_id"]), (200, "actioned", "o2"))
        s, b = call("POST", f"/api/dossiers/{did}/act", {})
        self.assertEqual((s, b["error"]["code"]), (409, "conflict"))
        s, b = call("POST", f"/api/dossiers/{self.ids[0]}/decline", {"reason": "other"})
        self.assertEqual(s, 409)  # generating

    def test_refill_and_worker(self):
        s, b = call("POST", "/api/dossiers/refill")
        self.assertEqual(s, 202)
        self.assertIn("running", b["worker"])
        worker.WORKER.start.assert_called()
        s, b = call("PUT", "/api/dossiers/worker", {"paused": "yes"})
        self.assertEqual(s, 400)
        s, b = call("PUT", "/api/dossiers/worker", {"paused": True, "stop": False})
        self.assertEqual((s, b["paused"], b["worker"]["paused"], b["stopping"]), (200, True, True, []))
        self.assertEqual(store.load()["paused_reason"], "paused in the app")
        self.assertNotIn("stop", store.load())
        s, b = call("PUT", "/api/dossiers/worker", {"paused": True})  # stops the running one by default
        self.assertEqual((s, b["stopping"]), (200, [self.ids[0]]))
        self.assertEqual(store.load()["stop"], {self.ids[0]: "stopped by pause"})
        self.assertEqual(set(b["worker"]), {"running", "current", "busy", "paused", "paused_reason",
                                            "backoff_until", "model"})
        s, b = call("PUT", "/api/dossiers/worker", {"paused": True, "stop": "yes"})
        self.assertEqual(s, 400)
        s, b = call("PUT", "/api/dossiers/worker", {"paused": False})
        self.assertEqual((s, b["paused"], b["stopping"]), (200, False, []))

    def test_worker_model_is_set_through_the_same_route(self):
        from johto import settings
        with mock.patch.object(settings, "path", lambda: Path(self._tmp.name) / "settings.json"):
            s, b = call("PUT", "/api/dossiers/worker", {"model": "gemini"})
            self.assertEqual(s, 400)
            s, b = call("PUT", "/api/dossiers/worker", {"model": "codex"})
            self.assertEqual((s, b["model"], b["model_word"], b["worker"]["model"]), (200, "codex", "Codex CLI", "codex"))
            self.assertEqual(settings.get("dossier_model"), "codex")
            s, b = call("PUT", "/api/dossiers/worker", {"model": "claude"})
            self.assertEqual((s, b["model"], b["worker"]["model"]), (200, "claude", "claude"))

    def test_stop_worker_shuts_down(self):
        with mock.patch.object(api_dossier.worker, "shutdown") as sd:
            api_dossier.stop_worker()
        sd.assert_called_once()

    def test_demo_fixture_actions_stay_in_memory(self):
        fixtures = sorted(api_dossier.FIXTURES.glob("*.json"))
        if not fixtures:
            self.skipTest("no fixture dossiers")
        fid = json.loads(fixtures[0].read_text())["id"]
        s, b = call("GET", "/api/dossiers", demo=True)
        self.assertIn(fid, [i["id"] for i in b["items"]])
        s, b = call("GET", "/api/dossiers")
        self.assertNotIn(fid, [i["id"] for i in b["items"]])
        s, b = call("GET", f"/api/dossiers/{fid}")
        self.assertEqual((s, b["dossier"]["id"]), (200, fid))
        s, b = call("POST", f"/api/dossiers/{fid}/decline", {"reason": "other"}, demo=True)
        self.assertEqual(b["item"]["status"], "declined")
        s, b = call("POST", f"/api/dossiers/{fid}/restore", demo=True)
        self.assertEqual(b["item"]["status"], "ready")
        self.assertFalse(any(it["id"] == fid for it in store.load()["items"]))


if __name__ == "__main__":
    unittest.main()
