"""The real-mode HTTP surface, called through Starlette's router (no lifespan, no threads), and demo mode
staying exactly as it was."""
import asyncio
import json
import unittest

from test_realstate_derive import RealRoot
from johto.web import app as webapp
from johto.web import demo

_APP = None


def call(method, path, body=None, cookie=True, demo_mode=False, headers=None, query=""):
    global _APP
    if _APP is None:
        _APP = webapp.create_app()
    cookies = [f"johto={webapp.TOKEN}"] if cookie else []
    if demo_mode:
        cookies.append("johto_mode=demo")
    hs = [(b"content-type", b"application/json")]
    if cookies:
        hs.append((b"cookie", "; ".join(cookies).encode()))
    for k, v in (headers or {}).items():
        hs.append((k.lower().encode(), v.encode()))
    raw = json.dumps(body).encode() if body is not None else b""
    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": query.encode(),
             "headers": hs, "scheme": "http", "server": ("127.0.0.1", 8768), "client": ("127.0.0.1", 1),
             "root_path": "", "http_version": "1.1"}
    sent = {"body": b"", "headers": {}}

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            sent["status"] = msg["status"]
            sent["headers"] = {k.decode(): v.decode() for k, v in msg.get("headers", [])}
        elif msg["type"] == "http.response.body":
            sent["body"] += msg.get("body", b"")

    asyncio.run(_APP.router(scope, receive, send))
    ct = sent["headers"].get("content-type", "")
    out = json.loads(sent["body"] or b"null") if "json" in ct else sent["body"].decode()
    return sent["status"], out


SAME = {"Host": "127.0.0.1:8768", "Origin": "http://127.0.0.1:8768"}


class RealApi(RealRoot):
    def test_state(self):
        st, s = call("GET", "/api/state")
        self.assertEqual(st, 200)
        self.assertEqual(s["server"]["mode"], "real")
        self.assertEqual(s["lane"]["state"], "waiting")
        self.assertEqual(s["queue"][0]["kind"], "lane_waiting")
        self.assertEqual({t["short"]: t["stage"] for t in s["tasks"]}, self.expected)
        for k in ("settings", "running", "jobs", "spend", "studio", "slots"):
            self.assertIn(k, s)

    def test_auth_and_origin(self):
        self.assertEqual(call("GET", "/api/state", cookie=False)[0], 401)
        st, out = call("PUT", "/api/tasks/aprompt1/sections/goal", {"text": "x"},
                       headers={"Host": "127.0.0.1:8768", "Origin": "http://evil.example"})
        self.assertEqual((st, out["error"]["code"]), (403, "forbidden"))
        self.assertEqual(call("PUT", "/api/tasks/aprompt1/sections/goal", {"text": "x"}, headers={"Host": "evil:8768"})[0], 403)

    def test_plain_url_needs_no_token_from_this_machine(self):
        def st(headers, method="GET", path="/api/state", body=None):
            return call(method, path, body, cookie=False, headers=headers)[0]
        self.assertEqual(st({"Host": "127.0.0.1:8768", "Sec-Fetch-Site": "same-origin"}), 200)
        self.assertEqual(st({"Host": "localhost:8768", "Sec-Fetch-Site": "none"}), 200)
        self.assertNotIn(st({**SAME, "Sec-Fetch-Site": "same-origin"}, "PUT", "/api/tasks/aprompt1/sections/goal", {"text": "x"}),
                         (401, 403))
        # Local scripts (curl, urllib, requests, websockets) send no fetch metadata: they need the token.
        self.assertEqual(st({"Host": "127.0.0.1:8768"}), 401)
        self.assertEqual(st({"Host": "127.0.0.1:8768"}, "PUT", "/api/tasks/aprompt1/sections/goal", {"text": "x"}), 401)
        self.assertEqual(st(SAME, "PUT", "/api/tasks/aprompt1/sections/goal", {"text": "x"}), 401)
        self.assertEqual(st(SAME, "POST", "/api/tasks/aprompt1/studio/unclaim", {}), 401)
        # Sockets: browsers send Origin (and no fetch metadata) on the handshake; scripts send no Origin.
        self.assertTrue(webapp.local_ok({"host": "127.0.0.1:8768", "origin": "http://127.0.0.1:8768"}, ws=True))
        self.assertFalse(webapp.local_ok({"host": "127.0.0.1:8768"}, ws=True))
        self.assertFalse(webapp.local_ok({"host": "127.0.0.1:8768", "origin": "http://127.0.0.1:3000"}, ws=True))
        # A write from the page without Origin is refused even with the cookie.
        self.assertEqual(call("PUT", "/api/tasks/aprompt1/sections/goal", {"text": "x"},
                              headers={"Host": "127.0.0.1:8768"})[0], 403)
        # The dossier routes check Origin too (another local port's page carries the SameSite cookie).
        self.assertEqual(call("PUT", "/api/dossiers/worker", {"paused": False},
                              headers={"Host": "127.0.0.1:8768", "Origin": "http://127.0.0.1:3000"})[0], 403)
        # Other sites' pages and DNS-rebinding hosts still need the token.
        self.assertEqual(st({"Host": "127.0.0.1:8768", "Sec-Fetch-Site": "cross-site"}), 401)
        self.assertEqual(st({"Host": "127.0.0.1:8768", "Sec-Fetch-Site": "same-site"}), 401)
        self.assertEqual(st({"Host": "127.0.0.1:8768", "Origin": "http://evil.example"}), 401)
        self.assertEqual(st({"Host": "127.0.0.1:8768", "Origin": "null"}), 401)
        self.assertEqual(st({"Host": "rebind.example:8768"}), 401)
        self.assertEqual(st({}), 401)

    def test_task_detail_list_run_transcript_diff(self):
        st, d = call("GET", "/api/tasks/bdecide1")
        self.assertEqual((st, d["summary"]["stage"]), (200, "decide"))
        self.assertEqual(d["runs"][0]["recommend"]["decision"], "drop")
        st, lst = call("GET", "/api/tasks")
        self.assertEqual(len(lst["tasks"]), len(self.expected))
        st, run = call("GET", "/api/tasks/bdecide1/runs/calib-1")
        self.assertEqual((st, run["name"]), (200, "calib-1"))
        st, tx = call("GET", "/api/tasks/bdecide1/runs/calib-1/transcript", query="after=0&limit=2")
        self.assertEqual((st, len(tx["entries"]), tx["entries"][0]["role"]), (200, 2, "human"))
        st, diff = call("GET", "/api/tasks/bdecide1/runs/calib-1/diff")
        self.assertEqual(st, 200)
        self.assertTrue(diff.startswith("diff --git"))
        self.assertEqual(call("GET", "/api/tasks/nope0000")[0], 404)
        self.assertEqual(call("GET", "/api/tasks/..%2Fx")[0], 404)
        self.assertEqual(call("GET", "/api/tasks/llive001/runs/validate-1/diff")[0], 404)   # not collected
        st, live = call("GET", "/api/tasks/llive001/runs/validate-1/transcript")
        self.assertEqual(sum(1 for e in live["entries"] if e["role"] == "human"), 2)

    def test_sections_lock_conflict_and_save(self):
        st, out = call("PUT", "/api/tasks/bdecide1/sections/opening_prompt", {"text": "x", "base_rev": "0"}, headers=SAME)
        self.assertEqual((st, out["error"]["code"]), (423, "locked"))
        st, d = call("GET", "/api/tasks/cdetail1")
        rev = d["sections"]["decision_points"]["rev"]
        st, out = call("PUT", "/api/tasks/cdetail1/sections/decision_points", {"text": "Better: lorem.", "base_rev": rev}, headers=SAME)
        self.assertEqual(st, 200)
        self.assertEqual(out["section"]["text"], "Better: lorem.")
        sub = (self.root / "tasks" / "cdetail1" / "submission.md").read_text()
        self.assertIn("## Decision Points\nBetter: lorem.\n", sub)
        # An edit made outside the app, then a save based on the old text.
        (self.root / "tasks" / "cdetail1" / "submission.md").write_text(sub.replace("Better: lorem.", "Vim text."))
        st, out = call("PUT", "/api/tasks/cdetail1/sections/decision_points", {"text": "Mine", "base_rev": rev}, headers=SAME)
        self.assertEqual((st, out["error"]["code"]), (409, "conflict_rev"))
        self.assertEqual(out["error"]["details"]["text"], "Vim text.")
        # Two quick saves from the page (the second based on the first's old rev) never conflict.
        st, d = call("GET", "/api/tasks/cdetail1")
        r0 = d["sections"]["codebase"]["rev"]
        self.assertEqual(call("PUT", "/api/tasks/cdetail1/sections/codebase", {"text": "A", "base_rev": r0}, headers=SAME)[0], 200)
        self.assertEqual(call("PUT", "/api/tasks/cdetail1/sections/codebase", {"text": "AB", "base_rev": r0}, headers=SAME)[0], 200)

    def test_unlock_two_step(self):
        st, out = call("POST", "/api/tasks/bdecide1/sections/opening_prompt/unlock", {}, headers=SAME)
        self.assertEqual((st, out["error"]["code"]), (409, "needs_confirm"))
        nonce = out["error"]["details"]["nonce"]
        st, out = call("POST", "/api/tasks/bdecide1/sections/opening_prompt/unlock", {"confirm": nonce}, headers=SAME)
        self.assertEqual(st, 200)
        self.assertIsNone(out["section"]["locked"])
        self.assertIn("prompt_unlocked_at", self.meta("bdecide1"))

    def test_done_prompt_details_rate(self):
        st, out = call("POST", "/api/tasks/aprompt1/done", {"surface": "prompt"}, headers=SAME)
        self.assertEqual((st, out["error"]["code"]), (422, "missing_fields"))
        call("PUT", "/api/tasks/aprompt1/sections/opening_prompt",
             {"text": "Sometimes the retries leave failed scrobbles behind and it takes me a few runs to clear them all out."},
             headers=SAME)
        # By default there's no calibration: the goal comes next.
        st, out = call("POST", "/api/tasks/aprompt1/done", {"surface": "prompt"}, headers=SAME)
        self.assertEqual(st, 200, out)
        self.assertEqual(out["summary"]["stage"], "details")
        self.assertTrue(self.meta("aprompt1")["skipped_calibration"])
        self.assertNotIn("ready", self.meta("aprompt1"))
        # "Calibrate first" still queues a calibration run.
        self.put("aprompt1", skipped_calibration=None, skipped_calibration_at=None)
        st, out = call("POST", "/api/tasks/aprompt1/done", {"surface": "prompt", "kind": "calib"}, headers=SAME)
        self.assertEqual(st, 200, out)
        self.assertEqual(out["summary"]["stage"], "waiting_calib")
        self.assertEqual(self.meta("aprompt1")["ready"]["kind"], "calib")
        # Details on a task whose chosen run is a calibration: no validation is queued; rating comes next.
        self.put("echosen1", details_ready=None)
        st, out = call("POST", "/api/tasks/echosen1/done", {"surface": "details"}, headers=SAME)
        if st == 409:
            st, out = call("POST", "/api/tasks/echosen1/done", {"surface": "details", "confirm": out["error"]["details"]["nonce"]}, headers=SAME)
        self.assertEqual(st, 200, out)
        self.assertEqual(out["summary"]["stage"], "rate")
        self.assertNotIn("ready", self.meta("echosen1"))
        # Rate: incomplete, then failing, then done.
        st, out = call("POST", "/api/tasks/drate001/done", {"surface": "rate"}, headers=SAME)
        self.assertEqual((st, out["error"]["code"]), (422, "missing_fields"))
        sub = self.root / "tasks" / "drate001" / "submission.md"
        sub.write_text(sub.read_text().replace("One-shot: \n", "One-shot: No\n").replace("Task Success: \n", "Task Success: 5\n")
                       .replace("Steering: \n", "Steering: 1\n").replace("## Validation Notes\n", "## Validation Notes\nLorem.\n"))
        st, out = call("POST", "/api/tasks/drate001/done", {"surface": "rate"}, headers=SAME)
        self.assertEqual((st, out["error"]["code"]), (422, "checklist_failed"))
        sub.write_text(sub.read_text().replace("Task Success: 5", "Task Success: 3"))
        st, out = call("POST", "/api/tasks/drate001/done", {"surface": "rate"}, headers=SAME)
        self.assertEqual(st, 200, out)
        self.assertEqual(out["summary"]["stage"], "ready_to_save")

    def test_ratings_put(self):
        st, out = call("PUT", "/api/tasks/drate001/ratings", {"one_shot": "No", "task_success": 3, "steering": 2}, headers=SAME)
        self.assertEqual(st, 200)
        self.assertTrue(out["ratings"]["passes"])
        self.assertIn("Task Success: 3", (self.root / "tasks" / "drate001" / "submission.md").read_text())

    def test_label_hide_presence(self):
        st, out = call("PUT", "/api/tasks/aprompt1/label", {"label": "scrobbler · retries"}, headers=SAME)
        self.assertEqual((st, out["summary"]["label"]), (200, "scrobbler · retries"))
        st, s = call("GET", "/api/state")
        rid = s["queue"][1]["id"]
        self.assertEqual(call("POST", f"/api/items/{rid}/hide", {}, headers=SAME)[0], 204)
        self.assertNotIn(rid, [i["id"] for i in call("GET", "/api/state")[1]["queue"]])
        self.assertEqual(call("DELETE", f"/api/items/{rid}/hide", None, headers=SAME)[0], 204)
        self.assertIn(rid, [i["id"] for i in call("GET", "/api/state")[1]["queue"]])
        self.assertEqual(call("POST", "/api/presence", {"visible": True}, headers=SAME)[0], 204)

    def test_finished(self):
        st, f = call("GET", "/api/finished")
        self.assertEqual({r["short"]: r["status"] for r in f["rows"]},
                         {"isentbk1": "Sent back", "jdrop001": "Dropped", "kreview1": "In review"})
        self.assertEqual(f["week"]["in_review"], 1)

    def test_builder_routes_are_mounted_and_demo_guarded(self):
        st, out = call("POST", "/api/tasks/d3m0aaa1/decision", {"run": "calib-1", "decision": "keep"}, demo_mode=True, headers=SAME)
        self.assertEqual(st, 202)
        self.assertTrue(out["toast"]["text"].startswith("Demo data: nothing was written"))
        st, out = call("POST", "/api/tasks/bdecide1/decision", {"run": "calib-1", "decision": "keep"}, cookie=False,
                       headers={"Host": "127.0.0.1:8768", "Origin": "http://evil.example"})
        self.assertEqual(st, 401)
        st, out = call("GET", "/api/tasks/bdecide1/checklist")
        self.assertEqual(st, 200)


class DemoUnchanged(unittest.TestCase):
    def test_demo_state_is_the_demo_afternoon(self):
        st, s = call("GET", "/api/state", demo_mode=True)
        self.assertEqual(st, 200)
        self.assertEqual(s["server"], {"version": "0.1", "fake": False, "read_only": True, "started_at": webapp.STARTED,
                                       "port": 8765, "mode": "demo"})
        q, running = demo.queue(webapp._demo["tasks"], "working", False)
        self.assertEqual([i["id"] for i in s["queue"]], [i["id"] for i in q])
        self.assertEqual(s["lane"]["state"], "working")
        self.assertEqual(s["spend"]["today_usd"], 1.54)
        st, d = call("GET", "/api/tasks/d3m0aaa1", demo_mode=True)
        self.assertEqual(d["summary"]["label"], "playlog · Import returns no tracks")
        st, f = call("GET", "/api/finished", demo_mode=True)
        self.assertEqual(f, demo.FINISHED)
        st, tx = call("GET", "/api/tasks/d3m0aab3/runs/validate-1/transcript", demo_mode=True)
        self.assertTrue(tx["entries"])

    def test_demo_writes_stay_local(self):
        st, out = call("POST", "/api/tasks/d3m0aaa1/done", {"surface": "prompt"}, demo_mode=True, headers=SAME)
        self.assertEqual(st, 202)
        st, out = call("PUT", "/api/tasks/d3m0aaa9/sections/opening_prompt", {"text": "Lorem ipsum."}, demo_mode=True, headers=SAME)
        self.assertEqual((st, out["section"]["text"]), (200, "Lorem ipsum."))


if __name__ == "__main__":
    unittest.main()
