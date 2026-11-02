"""The action routes end to end against the fake Studio, fake gh and fake lanes.

claim -> lane -> collect -> checklist -> submission run -> save (gists, fields, AutoQC) -> submit,
plus the two-press nonces, undo windows, the Studio worker's ordering, and auth.

Run: .venv/bin/python -m unittest tests.test_actions_api -v   (from the checkout; no pytest needed)
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import test_actions_support as fx  # noqa: E402

from johto import actions, workspace  # noqa: E402
from johto.fakes import gh as fake_gh, studio as fake  # noqa: E402
from johto.web import api_actions, jobs  # noqa: E402

PORT = 8766


def app():
    from starlette.applications import Starlette
    return Starlette(routes=api_actions.routes)


def call(method, path, body=None, cookie=True, origin=True, query=b""):
    headers = [(b"content-type", b"application/json"), (b"host", f"127.0.0.1:{PORT}".encode())]
    if cookie:
        headers.append((b"cookie", b"johto=test-token"))
    if origin:
        headers.append((b"origin", f"http://127.0.0.1:{PORT}".encode()))
    raw = json.dumps(body).encode() if body is not None else b""
    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": query,
             "headers": headers, "scheme": "http", "server": ("127.0.0.1", PORT), "client": ("127.0.0.1", 1),
             "root_path": "", "http_version": "1.1"}
    sent = {"body": b""}

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            sent["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            sent["body"] += msg.get("body", b"")

    asyncio.run(app().router(scope, receive, send))
    return sent["status"], (json.loads(sent["body"]) if sent["body"] else None)


def done(job: dict, timeout=20) -> dict:
    j = jobs.wait(job["id"], timeout)
    assert j["state"] in ("succeeded", "failed", "cancelled"), j
    return j


def wait_for(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("timed out")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = fx.make_root(self.root)
        self.patches = fx.patch_config(self.root)
        for p in self.patches:
            p.start()
        api_actions.set_auth(lambda req: req.cookies.get("johto") == "test-token")
        self.short_patch = mock.patch.object(api_actions, "_summary", lambda d: None)
        self.short_patch.start()
        self.timing = [mock.patch.object(api_actions, "UNDO_DROP_S", 0.6),
                       mock.patch.object(api_actions, "CODEBASE_CHECK_DELAY_S", 1.0),
                       mock.patch.object(api_actions, "QC_POLL_S", 0.05)]
        for p in self.timing:
            p.start()
        fake.reset()
        fake.QC_DELAY_S = 0.1
        fake_gh.ACCOUNT = "austeane"
        jobs.EVENTS.clear()
        jobs.start()

    def tearDown(self):
        for j in jobs.active():
            try:
                jobs.cancel(j["id"])
            except actions.ActionError:
                pass
            jobs.wait(j["id"], 5)
        for p in self.timing + [self.short_patch] + self.patches:
            p.stop()
        api_actions.set_auth(None)
        fake.reset()
        self.tmp.cleanup()

    # helpers
    def claim(self, **body) -> dict:
        body = {"lead_id": fx.LEAD_ID, "prompt_style": "Casual prose", "interaction_mode": "Interactive/pairing",
                "expect_status": "Ready to Claim", "codebase_check": False, **body}
        st, res = call("POST", "/api/claim", body)
        self.assertEqual(st, 202, res)
        return done(res["job"])

    def claimed(self) -> tuple[str, Path]:
        j = self.claim()
        self.assertEqual(j["state"], "succeeded", j)
        short = j["result"]["short"]
        return short, self.root / "tasks" / short

    def lane_and_collect(self, short: str, kind="calib") -> str:
        st, res = call("POST", f"/api/tasks/{short}/lane", {"kind": kind})
        self.assertEqual(st, 202, res)
        j = done(res["job"])
        self.assertEqual(j["state"], "succeeded", j)
        run = j["result"]["run"]
        fx.fake_claude(self.root / "tasks" / short / "runs" / run, fx.PROMPT)
        st, res = call("POST", f"/api/tasks/{short}/collect", {"run": run})
        self.assertEqual(st, 202, res)
        j = done(res["job"])
        self.assertEqual(j["state"], "succeeded", j)
        return run


class FullFlow(Base):
    def test_claim_collect_check_save_qc_submit(self):
        short, d = self.claimed()
        meta = workspace.load(d)
        t = fake.task(meta["task_id"])
        self.assertEqual(fake.status_name(t), "Writing")
        self.assertEqual(t["owned_by"], fake.ME)
        self.assertEqual(t["custom_fields"]["field_repo_name"], "acme/widget")
        self.assertIn("Lead", (d / "brief.md").read_text())
        self.assertEqual(json.loads((self.root / "data/scout/leads.json").read_text())[0]["status"], "claimed")
        slots = json.loads((self.root / "data/slots.json").read_text())
        self.assertEqual(slots["groups"][0]["count"], 2)

        run = self.lane_and_collect(short, "calib")
        self.assertEqual(run, "calib-1")
        rec = actions.run_record(d, run)
        self.assertTrue(rec["transcript"] and rec["diff"] and rec["collected_at"])

        st, res = call("GET", f"/api/tasks/{short}/checklist")
        self.assertEqual(st, 200)
        self.assertFalse(res["ready"])
        keys = {r["key"]: r for r in res["rows"]}
        self.assertEqual(keys["validation_run"]["status"], "fail")   # no validate-N and none chosen yet

        st, res = call("POST", f"/api/tasks/{short}/submission-run", {"run": "calib-1"})
        self.assertEqual(st, 200, res)
        self.assertEqual(res["run"], "calib-1")
        self.assertEqual(workspace.load(d)["submission_run"], "calib-1")

        fx.write_fixture_text(d)          # stands in for Austin typing his fields
        st, res = call("POST", f"/api/tasks/{short}/studio/save", {})
        self.assertEqual(st, 202, res)
        j = done(res["job"])
        self.assertEqual(j["state"], "succeeded", j)
        self.assertEqual(j["result"]["run"], "calib-1")
        qc = done(jobs.get(j["result"]["qc_job"]))
        self.assertEqual(qc["state"], "succeeded", qc)
        self.assertEqual(qc["result"]["counts"]["fail"], 0)
        cf = fake.task(meta["task_id"])["custom_fields"]
        self.assertTrue(cf["field_conversation_log"].startswith("https://gist.github.com/austeane/"))
        self.assertEqual(cf["field_opening_prompt"], fx.PROMPT)
        self.assertEqual(cf["field_claude_minutes"], 40)
        gist_files = [c["name"] for c in fake_gh.CREATED]
        self.assertEqual(sorted(gist_files), [f"{short}-conversation.jsonl", f"{short}-final.diff"])

        st, res = call("GET", f"/api/tasks/{short}/checklist")
        rows = {r["key"]: r for r in res["rows"]}
        self.assertEqual(rows["trajectory_logs"]["status"], "fail")    # Studio won't submit without it
        self.assertFalse(res["ready"])
        # Austin uploads the run's transcript to Trajectory Logs in Studio; the refresh reads it back.
        fake.patch_fields(meta["task_id"], {"field_trajectory_log_files": [
            {"file_s3_url": f"s3://bucket/file_{'b' * 32}_{Path(rec['transcript']).name}"}]})
        st, res = call("POST", f"/api/tasks/{short}/studio/refresh", {})
        self.assertIn(st, (200, 202), res)
        if st == 202:
            done(res["job"])
        st, res = call("GET", f"/api/tasks/{short}/checklist")
        rows = {r["key"]: r for r in res["rows"]}
        for k in ("autoqc", "studio_sync", "ratings", "validation_run", "gists", "trajectory_logs"):
            self.assertIn(rows[k]["status"], ("pass", "warn"), rows[k])
        self.assertTrue(res["ready"], res["rows"])

        st, res = call("POST", f"/api/tasks/{short}/studio/submit", {})
        self.assertEqual(st, 409, res)
        self.assertEqual(res["error"]["code"], "needs_confirm")
        nonce = res["error"]["details"]["nonce"]
        self.assertEqual(res["error"]["details"]["expires_in"], 4)
        self.assertTrue(res["error"]["details"]["rows"])
        self.assertEqual(fake.status_name(fake.task(meta["task_id"])), "Writing")   # nothing written yet

        st, res = call("POST", f"/api/tasks/{short}/studio/submit", {"confirm": nonce})
        self.assertEqual(st, 202, res)
        j = done(res["job"])
        self.assertEqual(j["state"], "succeeded", j)
        self.assertEqual(fake.status_name(fake.task(meta["task_id"])), "Awaiting Review")
        self.assertTrue(workspace.load(d)["submitted_at"])
        names = [n for n, _ in jobs.EVENTS]
        self.assertIn("job", names)
        self.assertIn("toast", names)
        self.assertIn("task", names)

        # The nonce was single use.
        st, res = call("POST", f"/api/tasks/{short}/studio/submit", {"confirm": nonce})
        self.assertNotEqual(st, 202)

    def test_studio_calls_run_on_the_worker_thread(self):
        seen = []
        real_call = fake._call
        actions.me()          # read once per process; prime it so the count below is the claim's own

        def spy(name, *args):
            seen.append((name, threading.current_thread().name))
            return real_call(name, *args)
        with mock.patch.object(fake, "_call", spy):
            short, d = self.claimed()
        names = [n for n, _ in seen]
        self.assertEqual(names, ["task", "transition", "patch_fields"])
        self.assertEqual({t for _, t in seen}, {"studio-worker"})
        self.assertTrue(jobs.worker.state()["last_ok_at"])


class Gates(Base):
    def test_submit_refused_while_checklist_fails(self):
        short, d = self.claimed()
        st, res = call("POST", f"/api/tasks/{short}/studio/submit", {})
        self.assertEqual(st, 422)
        self.assertEqual(res["error"]["code"], "checklist_failed")
        self.assertTrue(any(r["blocking"] for r in res["error"]["details"]["rows"]))

    def test_unclaim_two_press_and_nonce_bound_to_route(self):
        short, d = self.claimed()
        st, res = call("POST", f"/api/tasks/{short}/studio/unclaim", {"confirm": True})
        self.assertEqual(st, 409)
        nonce = res["error"]["details"]["nonce"]
        # A nonce from unclaim does not submit.
        st, res = call("POST", f"/api/tasks/{short}/studio/submit", {"confirm": nonce})
        self.assertIn(st, (409, 422))
        st, res = call("POST", f"/api/tasks/{short}/studio/unclaim", {})
        nonce = res["error"]["details"]["nonce"]
        st, res = call("POST", f"/api/tasks/{short}/studio/unclaim", {"confirm": nonce})
        self.assertEqual(st, 202, res)
        self.assertEqual(done(res["job"])["state"], "succeeded")
        meta = workspace.load(d)
        self.assertEqual(fake.status_name(fake.task(meta["task_id"])), "Ready to Claim")
        self.assertTrue(meta["unclaimed_at"])

    def test_expired_nonce_asks_again(self):
        short, d = self.claimed()
        st, res = call("POST", f"/api/tasks/{short}/studio/unclaim", {})
        nonce = res["error"]["details"]["nonce"]
        later = time.time() + api_actions.NONCE_S + 5
        with mock.patch.object(api_actions.time, "time", lambda: later):
            st, res = call("POST", f"/api/tasks/{short}/studio/unclaim", {"confirm": nonce})
        self.assertEqual(st, 409)
        self.assertIn("too late", res["error"]["message"])

    def test_save_refuses_when_not_mine_anymore(self):
        short, d = self.claimed()
        fake.set_status(workspace.load(d)["task_id"], "Awaiting Review")
        st, res = call("POST", f"/api/tasks/{short}/studio/save", {})
        j = done(res["job"])
        self.assertEqual(j["state"], "failed")
        self.assertEqual(j["error"]["code"], "precondition_failed")
        self.assertEqual(j["error"]["details"]["status"], "Awaiting Review")

    def test_studio_error_is_verbatim(self):
        short, d = self.claimed()
        fake.inject("patch_fields", fake.StudioError(500, {"detail": "field_task_success: not a valid choice"}, "/tasks/x"))
        st, res = call("POST", f"/api/tasks/{short}/studio/save", {})
        j = done(res["job"])
        self.assertEqual(j["error"]["code"], "studio_error")
        self.assertIn("field_task_success: not a valid choice", j["error"]["message"])
        self.assertEqual(j["error"]["details"]["status"], 500)
        # The failure reaches Austin as a toast, with Studio's words verbatim and a link to the task.
        toasts = [e for n, e in jobs.EVENTS if n == "toast" and e.get("level") == "error"]
        self.assertTrue(any("Save to Studio failed" in t["text"] and "not a valid choice" in t["text"]
                            and t["link"]["href"] == f"/task/{short}" for t in toasts), toasts)

    def test_dead_qc_poll_marks_the_row_failed(self):
        short, d = self.claimed()
        fake.QC_DELAY_S = 60
        fake.inject("qc_audit", fake.StudioError(503, {"detail": "upstream timed out"}, "/qc-audits/x"))
        try:
            st, res = call("POST", f"/api/tasks/{short}/studio/qc", {"spec": "AutoQC"})
            j = done(res["job"])
            child = done({"id": j["result"]["qc_job"]})
        finally:
            fake.QC_DELAY_S = 0
        self.assertEqual(child["state"], "failed")
        q = workspace.load(d)["qc"][-1]
        self.assertEqual(q["status"], "failed")
        self.assertIn("upstream timed out", q["failure_reason"])
        row = next(r for r in actions.checklist(d) if r["key"] == "autoqc")
        self.assertEqual((row["status"], row["fix"]["action"]), ("fail", "studio_qc"))
        toasts = [e["text"] for n, e in jobs.EVENTS if n == "toast" and e.get("level") == "error"]
        self.assertTrue(any(t.startswith("AutoQC failed for") for t in toasts), toasts)

    def test_run_names_are_checked(self):
        short, d = self.claimed()
        for method, path, body in (("POST", f"/api/tasks/{short}/collect", {"run": "../.."}),
                                   ("POST", f"/api/tasks/{short}/clean", {"run": "../../data"}),
                                   ("POST", f"/api/tasks/{short}/decision", {"run": "..", "decision": "keep"}),
                                   ("POST", f"/api/tasks/{short}/submission-run", {"run": "../x"})):
            st, res = call(method, path, body)
            self.assertIn(st, (400, 404), (path, res))
        st, res = call("POST", "/api/tasks/..%2F..%2Fdata/collect", {})
        self.assertIn(st, (400, 404))

    def test_one_recheck_per_save(self):
        short, d = self.claimed()
        self.lane_and_collect(short, "validate")
        fake.CALLS.clear()
        st, res = call("POST", f"/api/tasks/{short}/studio/save", {})
        self.assertEqual(done(res["job"])["state"], "succeeded")
        names = [n for n, _ in fake.CALLS]
        self.assertEqual(names[:4], ["task", "patch_fields", "run_qc", "qc_audit"][:len(names[:4])], names)
        self.assertNotIn("me", names)

    def test_auth_and_origin(self):
        st, res = call("GET", "/api/jobs", cookie=False)
        self.assertEqual(st, 401)
        st, res = call("POST", "/api/claim", {}, origin=False)
        self.assertEqual(st, 403)
        st, res = call("GET", "/api/jobs", origin=False)
        self.assertEqual(st, 200)

    def test_gh_account_blocks_gists(self):
        short, d = self.claimed()
        self.lane_and_collect(short, "validate")
        fake_gh.ACCOUNT = "someone-else"
        st, res = call("POST", f"/api/tasks/{short}/studio/save", {})
        j = done(res["job"])
        self.assertEqual(j["error"]["code"], "gh_account")
        self.assertIn("gh auth switch -u austeane", j["error"]["message"])


class ClaimPaths(Base):
    def test_taken_slot_offers_the_next_one(self):
        fake.take(fx.TASKS[0]["task_id"])
        j = self.claim(task_id=fx.TASKS[0]["task_id"])
        self.assertEqual(j["state"], "failed")
        self.assertEqual(j["error"]["code"], "taken")
        self.assertEqual(j["error"]["details"]["alternative"]["task_id"], fx.TASKS[1]["task_id"])
        self.assertFalse(any((self.root / "tasks").iterdir()))

    def test_undo_claim_within_30s(self):
        j = self.claim(codebase_check=True)
        short = j["result"]["short"]
        undo = j["result"]["undo"]
        st, res = call("POST", f"/api/undo/{undo['action_id']}")
        self.assertEqual(st, 200, res)
        uj = done(res["job"])
        self.assertEqual(uj["state"], "succeeded", uj)
        self.assertFalse((self.root / "tasks" / short).exists())
        self.assertTrue(list((self.root / "tasks" / ".unclaimed").iterdir()))
        self.assertEqual(fake.status_name(fake.task(fx.TASKS[0]["task_id"])), "Ready to Claim")
        self.assertEqual(json.loads((self.root / "data/scout/leads.json").read_text())[0]["status"], "new")
        cb = jobs.get(j["result"]["codebase_check_job"])
        self.assertEqual(done(cb)["state"], "cancelled")
        st, res = call("POST", f"/api/undo/{undo['action_id']}")
        self.assertEqual(st, 410)

    def test_codebase_check_runs_after_the_undo_window(self):
        j = self.claim(codebase_check=True)
        cb = done(jobs.get(j["result"]["codebase_check_job"]))
        self.assertEqual(cb["state"], "succeeded", cb)
        d = self.root / "tasks" / j["result"]["short"]
        wait_for(lambda: (workspace.load(d).get("qc") or [{}])[-1].get("status") == "completed")
        self.assertEqual(workspace.load(d)["qc"][-1]["spec"], "Codebase Check")

    def test_claim_from_dossier_puts_research_in_brief(self):
        st, res = call("POST", "/api/claim", {"lead_id": None, "dossier_id": fx.DOSSIER_ID, "opportunity_id": "o1",
                                              "task_id": fx.TASKS[0]["task_id"], "prompt_style": "Casual prose",
                                              "interaction_mode": "Interactive/pairing", "codebase_check": False})
        self.assertEqual(st, 202, res)
        j = done(res["job"])
        self.assertEqual(j["state"], "succeeded", j)
        d = self.root / "tasks" / j["result"]["short"]
        brief = (d / "brief.md").read_text()
        self.assertIn("Dossier (AI research)", brief)
        self.assertIn("Retry storms in the queue", brief)
        meta = workspace.load(d)
        self.assertEqual(meta["dossier"], {"id": fx.DOSSIER_ID, "opportunity_id": "o1"})
        self.assertEqual(meta["label"], "widget · Retry storms in the queue")
        # Nothing but the template went into Austin's file.
        self.assertNotIn("Retry storms", (d / "submission.md").read_text())

    def test_partial_claim_is_reported_and_retry_finishes_it(self):
        fake.inject("patch_fields", fake.StudioError(502, "Bad gateway", "/tasks/x"))
        j = self.claim(task_id=fx.TASKS[0]["task_id"])
        self.assertEqual(j["error"]["code"], "claim_partial")
        self.assertEqual(j["error"]["details"]["step"], "saving_repo")
        j = self.claim(task_id=fx.TASKS[0]["task_id"])
        self.assertEqual(j["state"], "succeeded", j)


class Drops(Base):
    def test_drop_can_be_undone_inside_10s(self):
        short, d = self.claimed()
        run = self.lane_and_collect(short)
        st, res = call("POST", f"/api/tasks/{short}/decision", {"run": run, "decision": "drop"})
        self.assertEqual(st, 202, res)
        self.assertEqual(res["job"]["progress"]["step"], "waiting")
        st, u = call("POST", f"/api/undo/{res['undo']['action_id']}")
        self.assertEqual(st, 200, u)
        self.assertEqual(done(res["job"])["state"], "cancelled")
        self.assertEqual(fake.status_name(fake.task(workspace.load(d)["task_id"])), "Writing")

    def test_drop_unclaims_tears_down_and_links_the_log(self):
        short, d = self.claimed()
        run = self.lane_and_collect(short)
        st, res = call("POST", f"/api/tasks/{short}/decision", {"run": run, "decision": "drop"})
        j = done(res["job"])
        self.assertEqual(j["state"], "succeeded", j)
        meta = workspace.load(d)
        self.assertEqual(fake.status_name(fake.task(meta["task_id"])), "Ready to Claim")
        self.assertTrue(meta["discard"]["url"].startswith("https://docs.google.com/forms/"))
        self.assertEqual(meta["decisions"][run]["decision"], "drop")
        self.assertEqual(json.loads((self.root / "data/scout/leads.json").read_text())[0]["status"], "discarded")
        st, res = call("POST", f"/api/tasks/{short}/discard-logged")
        self.assertEqual(st, 200)
        self.assertTrue(workspace.load(d)["discard"]["logged_at"])

    def test_keep_and_rework(self):
        short, d = self.claimed()
        run = self.lane_and_collect(short)
        st, res = call("POST", f"/api/tasks/{short}/decision", {"run": run, "decision": "rework"})
        self.assertEqual(st, 200, res)
        self.assertEqual(res["decisions"][run]["decision"], "rework")
        st, res = call("POST", f"/api/tasks/{short}/decision", {"run": run, "decision": "keep"})
        self.assertEqual(st, 200, res)
        self.assertEqual(done(res["job"])["state"], "succeeded")
        self.assertEqual(workspace.load(d)["decisions"][run]["decision"], "keep")


class Worker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        fx.make_root(self.root)
        self.patches = fx.patch_config(self.root)
        for p in self.patches:
            p.start()
        fake.reset()
        self.w = jobs.StudioWorker()
        self.w.start()

    def tearDown(self):
        fake.DELAY_S = 0
        fake.sign_in()
        self.w.stop()
        for p in self.patches:
            p.stop()
        fake.reset()
        self.tmp.cleanup()

    def test_one_call_at_a_time_and_writes_first(self):
        fake.DELAY_S = 0.15
        tid = fx.TASKS[0]["task_id"]
        order, live, peak = [], [0], [0]
        lock = threading.Lock()
        real_task = fake.task

        def tracked(task_id, tag=None):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            try:
                return real_task(task_id)
            finally:
                with lock:
                    live[0] -= 1
                    order.append(tag)

        with mock.patch.object(fake, "task", tracked):
            ts = [threading.Thread(target=self.w.call, args=("task", tid), kwargs={"tag": f"bg{i}", "priority": 2})
                  for i in range(4)]
            for t in ts:
                t.start()
                time.sleep(0.02)
            hi = threading.Thread(target=self.w.call, args=("task", tid), kwargs={"tag": "write", "priority": 0})
            hi.start()
            for t in ts + [hi]:
                t.join(10)
        self.assertEqual(peak[0], 1)
        self.assertLess(order.index("write"), 3, order)   # jumped the background queue

    def test_signed_out_holds_calls_until_the_session_is_back(self):
        fake.sign_out()
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("me", self.w.call("me", priority=1)))
        t.start()
        wait_for(lambda: self.w.state()["state"] == "signed_out")
        self.assertNotIn("me", out)
        fake.sign_in()
        self.w.wake()
        t.join(5)
        self.assertEqual(out["me"]["user_id"], fake.ME)
        self.assertIn(self.w.state()["state"], ("ok", "busy"))


if __name__ == "__main__":
    unittest.main()
