import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from johto import config
from johto.dossier import generate, store, worker
from test_dossier_fixtures import valid_doc


def pool(*names):
    return [{"full_name": n, "commit": f"{i:040x}", "pinned_url": f"https://github.com/{n}/tree/{i:040x}",
             "language": "Go", "promise": round(1 - i / 100, 2), "reasons": [f"r{i}"],
             "fits": [{"action": "Modify", "domain": "Backend / API", "codebase_type": "Personal-scale", "open": 3}]}
            for i, n in enumerate(names, 1)]


NAMES = [f"o/r{i}" for i in range(1, 16)]


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        store.use_data_dir(Path(self._tmp.name))
        self.kick = mock.patch.object(worker.WORKER, "kick")
        self.kick.start()
        # The real data/settings.json may name codex; these tests stand in for the Claude generator.
        self.settings = mock.patch.object(worker.settings, "path", lambda: Path(self._tmp.name) / "settings.json")
        self.settings.start()

    def tearDown(self):
        self.settings.stop()
        self.kick.stop()
        store.use_data_dir(config.DATA)
        self._tmp.cleanup()

    def items(self):
        return store.load()["items"]

    def status_of(self, repo):
        return next(it["status"] for it in self.items() if it["repo"] == repo)


class StoreTest(StoreCase):
    def test_new_index_starts_paused(self):
        self.assertFalse(store.P.index.exists())
        self.assertTrue(store.load()["paused"])
        with store.edit():
            pass
        idx = json.loads(store.P.index.read_text())
        self.assertEqual((idx["paused"], idx["target"], idx["items"]), (True, 10, []))

    def test_make_id(self):
        self.assertEqual(store.make_id("acme/playlog", "9F1E2D3C4b5a"), "acme__playlog__9f1e2d3c")

    def test_generation_lock_is_exclusive(self):
        with store.generation_lock() as a:
            self.assertTrue(a)
            self.assertTrue(store.generating_elsewhere())
            with store.generation_lock() as b:
                self.assertFalse(b)
        self.assertFalse(store.generating_elsewhere())

    def test_busy_probe_never_writes_the_lock_file(self):
        store.P.dir.mkdir(parents=True, exist_ok=True)
        store.P.gen_lock.write_text("4242\n")
        self.assertFalse(store.generation_busy())
        self.assertEqual(store.P.gen_lock.read_text(), "4242\n")

    def test_lock_waits_out_a_probe(self):
        import fcntl
        store.P.dir.mkdir(parents=True, exist_ok=True)
        with open(store.P.gen_lock, "a+") as probe:
            fcntl.flock(probe, fcntl.LOCK_SH)  # a status probe in flight
            threading.Timer(0.2, lambda: fcntl.flock(probe, fcntl.LOCK_UN)).start()
            with store.generation_lock(wait_s=2) as got:
                self.assertTrue(got)

    def test_requeue_stale(self):
        worker.fill(pool("o/a", "o/b"))
        store.update(store.make_id("o/a", f"{1:040x}"), status="generating", started_at="x")
        with store.generation_lock():  # a live generation: nothing is stale
            self.assertEqual(store.requeue_stale(), [])
        self.assertEqual(store.requeue_stale(), [store.make_id("o/a", f"{1:040x}")])
        self.assertEqual(self.status_of("o/a"), "queued")


class FillTest(StoreCase):
    def test_fills_to_target_in_rank_order(self):
        r = worker.fill(pool(*NAMES))
        self.assertEqual(len(r["added"]), 10)
        its = self.items()
        self.assertEqual([it["repo"] for it in its], NAMES[:10])
        self.assertEqual([it["rank"] for it in its], list(range(1, 11)))
        self.assertTrue(all(it["status"] == "queued" for it in its))

    def test_ready_counts_and_closed_repos_are_skipped(self):
        worker.fill(pool(*NAMES))
        for n in ("o/r1", "o/r2"):
            store.update(store.make_id(n, next(p["commit"] for p in pool(*NAMES) if p["full_name"] == n)),
                         status="ready")
        with store.edit() as idx:
            idx["items"][2]["status"] = "declined"
        r = worker.fill(pool(*NAMES))
        self.assertEqual(r["added"], [store.make_id("o/r11", f"{11:040x}")])
        repos = [it["repo"] for it in self.items()]
        self.assertEqual(repos.count("o/r3"), 1)  # a declined repo is not queued again

    def test_reranks_and_drops_queued_that_left_the_pool(self):
        worker.fill(pool(*NAMES[:3]))
        r = worker.fill(pool("o/r3", "o/r1"))
        self.assertEqual(r["dropped"], [store.make_id("o/r2", f"{2:040x}")])
        ranks = {it["repo"]: it["rank"] for it in self.items()}
        self.assertEqual(ranks, {"o/r1": 2, "o/r3": 1})

    def test_better_new_repos_displace_worse_queued_ones(self):
        worker.fill(pool(*NAMES[5:15]))  # r6..r15 queued
        first = {it["repo"]: it["queued_at"] for it in self.items()}
        r = worker.fill(pool(*NAMES))  # r1..r5 now rank above them
        self.assertEqual([it["repo"] for it in self.items()], NAMES[:10])
        self.assertEqual(len(r["added"]), 5)
        self.assertEqual(len(r["dropped"]), 5)
        self.assertEqual(self.items()[7]["queued_at"], first["o/r8"])  # kept items keep their queue time

    def test_ready_items_take_their_current_rank(self):
        worker.fill(pool("o/a", "o/b"))
        store.update(store.make_id("o/a", f"{1:040x}"), status="ready")
        worker.fill(pool("o/b", "o/a"))
        ranks = {it["repo"]: (it["status"], it["rank"]) for it in self.items()}
        self.assertEqual(ranks, {"o/a": ("ready", 2), "o/b": ("queued", 1)})

    def test_new_commit_moves_a_queued_item(self):
        worker.fill(pool("o/a"))
        p = pool("o/a")
        p[0]["commit"] = "f" * 40
        worker.fill(p)
        (it,) = self.items()
        self.assertEqual((it["commit"], it["id"]), ("f" * 40, store.make_id("o/a", "f" * 40)))

    def test_trims_excess_queued(self):
        worker.fill(pool(*NAMES))
        with store.edit() as idx:
            idx["target"] = 4
        r = worker.fill(pool(*NAMES))
        self.assertEqual(len(r["dropped"]), 6)
        self.assertEqual([it["repo"] for it in self.items()], NAMES[:4])

    def test_manual_item_keeps_its_commit(self):
        with store.edit() as idx:
            it = store.new_item({**pool("o/a")[0], "commit": "e" * 40}, None, manual=True)
            idx["items"].append(it)
        worker.fill(pool("o/a"))
        (it,) = self.items()
        self.assertEqual((it["commit"], it["id"], it["rank"]), ("e" * 40, store.make_id("o/a", "e" * 40), 1))

    def test_manual_items_survive_and_go_first(self):
        worker.fill(pool("o/a", "o/b"))
        with store.edit() as idx:
            idx["items"].append(store.new_item(pool("o/z")[0], None, manual=True))
        worker.fill(pool("o/a", "o/b"))
        self.assertEqual(worker.next_queued(store.load())["repo"], "o/z")


class ActionsTest(StoreCase):
    def setUp(self):
        super().setUp()
        worker.fill(pool("o/a", "o/b"))
        self.a = store.make_id("o/a", f"{1:040x}")
        doc = valid_doc()
        doc["id"] = self.a
        store.save_dossier(doc)
        store.update(self.a, status="ready")

    def test_decline_and_restore(self):
        it = worker.decline(self.a, "too_easy", "one file")
        self.assertEqual((it["status"], it["decline"]), ("declined", {"reason": "too_easy", "note": "one file"}))
        self.assertIsNotNone(it["closed_at"])
        worker.WORKER.kick.assert_called()
        it = worker.restore(self.a)
        self.assertEqual((it["status"], it["decline"], it["closed_at"]), ("ready", None, None))

    def test_decline_rejects_bad_reason_and_generating(self):
        with self.assertRaises(ValueError):
            worker.decline(self.a, "meh")
        store.update(self.a, status="generating")
        with self.assertRaises(worker.Conflict):
            worker.decline(self.a, "other")
        with self.assertRaises(KeyError):
            worker.decline("nope", "other")

    def test_act(self):
        it = worker.act(self.a, "o2", "t4sk0001")
        self.assertEqual((it["status"], it["actioned"]), ("actioned", {"task_short": "t4sk0001", "opportunity_id": "o2"}))
        with self.assertRaises(worker.Conflict):
            worker.act(self.a)

    def test_restore_without_dossier_requeues(self):
        b = store.make_id("o/b", f"{2:040x}")
        store.update(b, status="failed", error="boom", finished_at="x")
        it = worker.restore(b)
        self.assertEqual((it["status"], it["error"], it["manual"]), ("queued", None, True))

    def test_pause_resume(self):
        self.assertTrue(worker.set_paused(True, "why")["paused"])
        self.assertEqual(store.load()["paused_reason"], "why")
        self.assertFalse(worker.set_paused(False)["paused"])
        self.assertNotIn("paused_reason", store.load())
        self.assertIn("resumed_at", store.load())


class RunItemTest(StoreCase):
    def setUp(self):
        super().setUp()
        worker.fill(pool("o/a"))
        self.a = store.make_id("o/a", f"{1:040x}")

    def fake(self, **kw):
        def gen(entry, did, on_progress=None, echo=None, cancel=None):
            on_progress({"step": "reading the code", "elapsed_s": 5, "log_tail": ["Read x"]})
            self.seen = store.get(did)
            r = generate.Result(**kw)
            if r.ok:
                r.doc = {**valid_doc(), "id": did}
            return r
        return mock.patch.object(worker.generate, "generate", gen)

    def waiting(self):
        """A generation that runs until it is cancelled."""
        self.started = threading.Event()

        def gen(entry, did, on_progress=None, echo=None, cancel=None):
            self.started.set()
            for _ in range(200):
                if cancel.cancelled:
                    return generate.Result(cancelled=True, error=cancel.reason, elapsed_s=1)
                time.sleep(0.05)
            return generate.Result(error="never cancelled")
        return mock.patch.object(worker.generate, "generate", gen)

    def run_in_thread(self):
        out = {}
        th = threading.Thread(target=lambda: out.update(res=worker.run_item(self.a)))
        th.start()
        self.assertTrue(self.started.wait(5))
        return th, out

    def test_ready(self):
        with self.fake(ok=True, cost_usd=3.2, elapsed_s=600, model="claude-opus-5-5", passes=1):
            worker.run_item(self.a)
        self.assertEqual(self.seen["status"], "generating")
        self.assertEqual(self.seen["progress"]["step"], "reading the code")
        it = store.get(self.a)
        self.assertEqual((it["status"], it["cost_usd"], it["elapsed_s"]), ("ready", 3.2, 600))
        self.assertIsNotNone(it["finished_at"])
        self.assertEqual(store.load_dossier(self.a)["id"], self.a)

    def test_failed(self):
        with self.fake(ok=False, error="failed validation", errors=["x: bad"], cost_usd=1.0):
            worker.run_item(self.a)
        it = store.get(self.a)
        self.assertEqual((it["status"], it["error"], it["errors"]), ("failed", "failed validation", ["x: bad"]))
        self.assertIsNone(store.load_dossier(self.a))

    def test_retryable_requeues(self):
        with self.fake(ok=False, retryable=True, error="usage limit"):
            worker.run_item(self.a)
        self.assertEqual(store.get(self.a)["status"], "queued")

    def test_retryable_gives_up_after_max_retries(self):
        with self.fake(ok=False, retryable=True, error="hit the Claude usage limit; requeued"):
            for _ in range(worker.MAX_RETRIES):
                worker.run_item(self.a)
        it = store.get(self.a)
        self.assertEqual((it["status"], it["retries"]), ("failed", worker.MAX_RETRIES))
        self.assertIn("gave up after 3 tries", it["error"])

    def test_not_queued_is_a_no_op(self):
        store.update(self.a, status="declined")
        with self.fake(ok=True):
            res = worker.run_item(self.a)
        self.assertTrue(res.skipped)
        self.assertEqual(store.get(self.a)["status"], "declined")
        self.assertFalse(hasattr(self, "seen"))

    def test_cancelled_requeues_without_failing(self):
        with self.fake(ok=False, cancelled=True, error="stopped by pause"):
            worker.run_item(self.a)
        it = store.get(self.a)
        self.assertEqual((it["status"], it["error"], it["finished_at"]), ("queued", "stopped by pause", None))
        self.assertEqual(worker.fail_streak(store.load()), 0)

    def test_pause_with_stop_stops_a_running_generation(self):
        with self.waiting():
            th, out = self.run_in_thread()
            self.assertEqual(worker.status()["current"], self.a)
            st = worker.set_paused(True, "lane", stop=True)
            self.assertEqual(st["stopping"], [self.a])
            th.join(5)
        self.assertTrue(out["res"].cancelled)
        it = store.get(self.a)
        self.assertEqual((it["status"], it["error"]), ("queued", "stopped by pause"))
        self.assertNotIn("stop", store.load())
        self.assertIsNone(worker.current())

    def test_stop_request_from_another_process(self):
        with self.waiting(), mock.patch.object(worker, "WATCH_S", 0.05):
            th, out = self.run_in_thread()
            with store.edit() as idx:  # what set_paused does in another process
                store.request_stop(idx, self.a, "stopped by pause")
            th.join(5)
        self.assertEqual((out["res"].cancelled, out["res"].error), (True, "stopped by pause"))
        self.assertEqual(store.get(self.a)["status"], "queued")

    def test_pause_without_stop_lets_it_finish(self):
        with self.waiting(), mock.patch.object(worker, "WATCH_S", 0.05):
            th, out = self.run_in_thread()
            self.assertEqual(worker.set_paused(True, "x")["stopping"], [])
            time.sleep(0.3)
            self.assertTrue(th.is_alive())
            worker.cancel_running("test over")
            th.join(5)

    def test_shutdown_stops_the_current_generation(self):
        with self.waiting():
            th, out = self.run_in_thread()
            worker.shutdown(timeout=5)
            th.join(5)
        worker.WORKER._stop.clear()
        self.assertEqual(out["res"].error, worker.SHUTDOWN_REASON)
        self.assertEqual(store.get(self.a)["status"], "queued")


class StepTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.p = [mock.patch.object(worker.candidates, "ranked", lambda limit: pool("o/a", "o/b")),
                  mock.patch.object(worker, "run_item", self.run_item)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        super().tearDown()

    def run_item(self, did, echo=None):
        self.calls.append(did)
        store.update(did, status="ready", finished_at=store.now_iso())
        return generate.Result(ok=True)

    def test_paused_fills_but_never_generates(self):
        w = worker.Worker(echo=lambda s: None)
        self.assertFalse(w.step())
        self.assertEqual(self.calls, [])
        self.assertEqual(len(store.load()["items"]), 2)
        self.assertTrue(store.load()["paused"])

    def test_unpaused_generates_best_first_one_at_a_time(self):
        worker.set_paused(False)
        w = worker.Worker(echo=lambda s: None)
        self.assertTrue(w.step())
        self.assertEqual(self.calls, [store.make_id("o/a", f"{1:040x}")])
        with store.generation_lock():  # another process is generating
            self.assertFalse(w.step())
        self.assertEqual(len(self.calls), 1)

    def test_fail_streak_pauses(self):
        worker.set_paused(False)
        later = "9999-01-01T00:00:00+0000"
        with store.edit() as idx:
            for i in range(3):
                it = store.new_item(pool(f"o/f{i}")[0], None)
                it.update(status="failed", finished_at=later)
                idx["items"].append(it)
        self.assertEqual(worker.fail_streak(store.load()), 3)
        w = worker.Worker(echo=lambda s: None)
        self.assertFalse(w.step())
        self.assertTrue(store.load()["paused"])
        self.assertIn("failed in a row", store.load()["paused_reason"])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
