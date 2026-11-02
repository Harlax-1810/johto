"""johto/slot_research.py: which repo to research for a slot with no great option, when auto mode starts it, and
how a run is recorded. Never a real research run: the dossier worker and brief are stubbed."""
from __future__ import annotations

import contextlib
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from johto import config, settings, slot_research as sr

GAMES = "Operate|Games|Personal-scale"
DEVOPS = "Modify|DevOps / cloud|Enterprise-grade"


def entry(name, domains, types_=("Personal-scale",), lang="Python", promise=0.5):
    return {"full_name": name, "commit": "c" * 40, "language": lang, "promise": promise,
            "codebase_types_clean": list(types_), "domains": {d: {"strength": s} for d, s in domains.items()}}


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "data").mkdir()
        (root / "tasks").mkdir()
        for name, val in (("DATA", root / "data"), ("TASKS", root / "tasks")):
            p = mock.patch.object(config, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.root = root


class PickRepo(Tmp):
    def test_domain_strength_and_codebase_type_must_fit(self):
        pool = [entry("o/weak", {"Games": 0.5}), entry("o/big", {"Games": 1.0}, types_=("Enterprise-grade",)),
                entry("o/ok", {"Games": 0.6})]
        self.assertEqual(sr.pick_repo(GAMES, pool, researched=set(), tried=set())["full_name"], "o/ok")

    def test_researched_and_tried_repos_are_skipped(self):
        pool = [entry("o/a", {"Games": 1.0}), entry("o/b", {"Games": 0.9}), entry("o/c", {"Games": 0.8})]
        got = sr.pick_repo(GAMES, pool, researched={"o/a"}, tried={"o/b"})
        self.assertEqual(got["full_name"], "o/c")
        self.assertIsNone(sr.pick_repo(GAMES, pool, researched={"o/a", "o/b", "o/c"}, tried=set()))

    def test_strength_first_then_a_language_he_knows_then_promise(self):
        (self.root / "tasks" / "t1").mkdir()
        (self.root / "tasks" / "t1" / "task.json").write_text(json.dumps({"repo": {"language": "TypeScript"}}))
        pool = [entry("o/go", {"Games": 1.0}, lang="Go", promise=0.9),
                entry("o/ts", {"Games": 1.0}, lang="TypeScript", promise=0.4),
                entry("o/strong", {"Games": 0.7}, lang="TypeScript", promise=1.0)]
        self.assertEqual(sr.pick_repo(GAMES, pool, researched=set(), tried=set())["full_name"], "o/ts")


class Due(unittest.TestCase):
    def weak(self, key, open_, best=None):
        return {"key": key, "open": open_, "best": best}

    def test_fewest_open_first_then_no_option(self):
        w = [self.weak(DEVOPS, 2, 6), self.weak(GAMES, 2), self.weak("A|B|C", 5)]
        self.assertEqual(sr.due(w, [], now=1e9)["key"], GAMES)

    def test_cooldown_and_max_tries(self):
        now = 1e9
        w = [self.weak(GAMES, 1)]
        recent = [{"group": GAMES, "state": "ready", "started": now - 60}]
        self.assertIsNone(sr.due(w, recent, now=now))
        old = [{"group": GAMES, "state": "ready", "started": now - sr.COOLDOWN_S - 1}]
        self.assertEqual(sr.due(w, old, now=now)["key"], GAMES)
        tried = [{"group": GAMES, "state": "failed", "started": now - 10 * sr.COOLDOWN_S}] * sr.MAX_AUTO_TRIES
        self.assertIsNone(sr.due(w, tried, now=now))
        nothing = [{"group": GAMES, "state": "nothing", "started": now - 10 * sr.COOLDOWN_S}] * sr.MAX_AUTO_TRIES
        self.assertEqual(sr.due(w, nothing, now=now)["key"], GAMES)   # no repo then: the scout may vet one later


class Mode(Tmp):
    def test_default_auto_and_saved_to_settings(self):
        self.assertEqual(sr.mode(), "auto")
        sr.set_mode("manual")
        self.assertEqual(sr.mode(), "manual")
        self.assertEqual(json.loads(settings.path().read_text())["slot_research"], "manual")
        with self.assertRaises(ValueError):
            sr.set_mode("sometimes")


class ForNow(Tmp):
    def setUp(self):
        super().setUp()
        self.started = []
        for name, val in (("running", lambda: None),
                          ("start", lambda g, auto=False, **k: self.started.append(g) or {"state": "running",
                                                                                          "repo": "o/x"})):
            p = mock.patch.object(sr, name, val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(config, "FAKE", False)
        p.start()
        self.addCleanup(p.stop)
        self.weak = [{"key": GAMES, "open": 1, "best": None, "action": "Operate", "domain": "Games",
                      "codebase_type": "Personal-scale", "options": 0}]

    def test_auto_starts_one_when_the_lane_is_free(self):
        d = sr.for_now(self.weak)
        self.assertEqual(self.started, [GAMES])
        self.assertEqual(d["started"], "o/x")
        self.assertEqual(d["weak"][0]["tries"], 0)

    def test_never_while_claude_works_or_in_manual(self):
        sr.for_now(self.weak, lane_busy=True)
        sr.set_mode("manual")
        d = sr.for_now(self.weak)
        self.assertEqual(self.started, [])
        self.assertEqual(d["mode"], "manual")

    def test_shows_the_last_run(self):
        sr._record({"id": "r1", "group": GAMES, "repo": "o/acme", "state": "failed", "started": 1.0,
                    "error": "usage limit"})
        d = sr.for_now(self.weak, start_auto=False)
        self.assertEqual(d["weak"][0]["last"]["repo"], "o/acme")
        self.assertEqual(d["weak"][0]["tries"], 1)


class Research(Tmp):
    """_research: queue the repo with its focus, run it, write its brief, record how it ended."""

    def stubs(self, ok=True, brief_ok=True):
        calls = {}

        @contextlib.contextmanager
        def lock(blocking=False, wait_s=0.0):
            calls["blocking"] = blocking
            yield True

        store = types.SimpleNamespace(generation_lock=lock)
        worker = types.SimpleNamespace(
            queue_manual=lambda e: calls.setdefault("focus", e["focus"]) and "o__game__cccccccc",
            run_item=lambda did, echo=None: types.SimpleNamespace(ok=ok, error=None if ok else "usage limit",
                                                                  cost_usd=1.0))
        brief = types.SimpleNamespace(generate=lambda did, locked=False, echo=None:
                                      calls.setdefault("brief_locked", locked) and {"ok": brief_ok, "cost_usd": 0.5,
                                                                                    "error": None if brief_ok else "bad"})
        pkg = types.SimpleNamespace(store=store, worker=worker, brief=brief)
        return calls, mock.patch.dict("sys.modules", {"johto.dossier.store": store, "johto.dossier.worker": worker,
                                                      "johto.dossier.brief": brief}), pkg

    def ctx(self):
        return types.SimpleNamespace(step=lambda *a: None, log=lambda *a: None, add_cost=lambda *a: None)

    def run_one(self, **kw):
        calls, patch, pkg = self.stubs(**kw)
        run = {"id": "r1", "group": GAMES, "repo": "o/game", "state": "running", "started": 1.0}
        e = {"full_name": "o/game", "focus": [GAMES + "|Casual prose|Async/autonomous"]}
        import johto.dossier as dossier
        with patch, mock.patch.object(dossier, "store", pkg.store, create=True), \
                mock.patch.object(dossier, "worker", pkg.worker, create=True), \
                mock.patch.object(dossier, "brief", pkg.brief, create=True):
            try:
                sr._research(run, e, self.ctx())
            except RuntimeError:
                pass
        return calls, sr.load()["runs"][-1]

    def test_ready(self):
        calls, rec = self.run_one()
        self.assertTrue(calls["blocking"])
        self.assertEqual(calls["focus"], [GAMES + "|Casual prose|Async/autonomous"])
        self.assertTrue(calls["brief_locked"])
        self.assertEqual((rec["state"], rec["did"]), ("ready", "o__game__cccccccc"))

    def test_failed_research_is_recorded(self):
        _, rec = self.run_one(ok=False)
        self.assertEqual((rec["state"], rec["error"]), ("failed", "usage limit"))

    def test_a_failed_brief_is_recorded(self):
        _, rec = self.run_one(brief_ok=False)
        self.assertEqual(rec["state"], "failed")
        self.assertIn("brief", rec["error"])

    def test_requeue_dead_marks_running_runs_failed(self):
        sr._record({"id": "r2", "group": GAMES, "repo": "o/x", "state": "running", "started": 1.0})
        sr.requeue_dead()
        self.assertEqual(sr.load()["runs"][-1]["state"], "failed")


class AfterRating(Tmp):
    def test_a_finished_rating_starts_research_in_auto_only(self):
        from johto import slot_options
        from johto.web import data
        seen = []
        weak = [{"key": GAMES, "open": 1, "best": 7}]
        with mock.patch.object(slot_options, "for_now", lambda start=True: {"weak": weak}), \
                mock.patch.object(data, "real_snapshot", lambda: {"lane": {"state": "free"}}), \
                mock.patch.object(sr, "for_now", lambda w, lane_busy=False: seen.append((w, lane_busy))):
            sr.after_rating()
            self.assertEqual(seen, [(weak, False)])
            sr.set_mode("manual")
            sr.after_rating()
            self.assertEqual(len(seen), 1)

    def test_a_finished_research_run_starts_the_rating(self):
        started = []
        r = Research()
        r.root, r.tmp = self.root, self.tmp
        from johto import slot_options
        with mock.patch.object(slot_options, "available", lambda: True), \
                mock.patch.object(slot_options, "ensure", lambda: started.append(1)):
            _, rec = r.run_one()
        self.assertEqual(rec["state"], "ready")
        self.assertEqual(started, [1])


class Start(Tmp):
    def test_no_fitting_repo_is_recorded_as_nothing(self):
        with mock.patch.object(sr, "running", lambda: None), mock.patch.object(sr, "pick_repo", lambda g: None):
            run = sr.start(GAMES)
        self.assertEqual(run["state"], "nothing")
        self.assertEqual(sr.load()["runs"][-1]["group"], GAMES)

    def test_one_at_a_time(self):
        with mock.patch.object(sr, "running", lambda: {"target": "o/x"}):
            self.assertIn("error", sr.start(GAMES))

    def test_a_cli_run_counts_as_running_until_it_goes_stale(self):
        import time
        sr._record({"id": "r3", "group": GAMES, "repo": "o/x", "state": "running", "started": time.time()})
        with mock.patch("johto.web.jobs.active", lambda kind=None: []):
            self.assertEqual(sr.running()["params"]["group"], GAMES)
            sr._record({"id": "r3", "group": GAMES, "repo": "o/x", "state": "running",
                        "started": time.time() - sr.RUN_FRESH_S - 1})
            self.assertIsNone(sr.running())


if __name__ == "__main__":
    unittest.main()
