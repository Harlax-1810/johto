"""POST /api/claim with an origin (spec 7.2): a claim from a brief's seed or from a place to explore.

The origin is stored in task.json with the dossier id and names the task's label; a place claim has no research
problem attached and marks the repo's research as started with a null opportunity id; the 30 s undo puts that back.
Runs on the action tests' harness (fake Studio, temp JOHTO_ROOT).
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import test_actions_support as fx  # noqa: E402
from test_actions_api import Base, call, done  # noqa: E402

from johto import workspace  # noqa: E402
from johto.dossier import store, worker  # noqa: E402
from johto.fakes import studio as fake  # noqa: E402

VARIANT = {"prompt_style": "Casual prose", "interaction_mode": "Interactive/pairing", "codebase_check": False}
GROUP = {k: fx.SLOT[k] for k in ("action", "domain", "codebase_type")}


class OriginClaims(Base):
    def setUp(self):
        super().setUp()
        self.kick = mock.patch.object(worker.WORKER, "kick")
        self.kick.start()
        with store.edit() as idx:
            idx["items"].append({**store.new_item({"full_name": "acme/widget", "commit": fx.COMMIT}, 1),
                                 "id": fx.DOSSIER_ID, "status": "ready"})

    def tearDown(self):
        self.kick.stop()
        super().tearDown()

    def post(self, **body) -> tuple[int, dict]:
        return call("POST", "/api/claim", {"lead_id": None, "dossier_id": fx.DOSSIER_ID, **VARIANT, **body})

    def claim_ok(self, **body) -> dict:
        st, res = self.post(**body)
        self.assertEqual(st, 202, res)
        j = done(res["job"])
        self.assertEqual(j["state"], "succeeded", j)
        return j

    def test_seed_origin_is_stored_and_names_the_label(self):
        j = self.claim_ok(origin={"kind": "seed", "opportunity_id": "o1"}, slot=GROUP)
        meta = workspace.load(self.root / "tasks" / j["result"]["short"])
        self.assertEqual(meta["origin"], {"kind": "seed", "dossier_id": fx.DOSSIER_ID, "opportunity_id": "o1",
                                          "title": "Retry storms in the queue"})
        self.assertEqual(meta["dossier"], {"id": fx.DOSSIER_ID, "opportunity_id": "o1"})
        self.assertEqual(meta["label"], "Widget · Retry storms in the queue")
        it = store.get(fx.DOSSIER_ID)
        self.assertEqual((it["status"], it["actioned"]["opportunity_id"]), ("actioned", "o1"))

    def test_place_origin_has_no_problem_and_marks_the_research_started(self):
        j = self.claim_ok(origin={"kind": "place", "place_id": "p2", "name": "Retries and backoff"}, slot=GROUP)
        short = j["result"]["short"]
        meta = workspace.load(self.root / "tasks" / short)
        self.assertEqual(meta["origin"], {"kind": "place", "dossier_id": fx.DOSSIER_ID, "place_id": "p2",
                                          "name": "Retries and backoff"})
        self.assertEqual(meta["dossier"], {"id": fx.DOSSIER_ID, "opportunity_id": None})
        self.assertEqual(meta["label"], "Widget · Retries and backoff")
        self.assertEqual({k: meta["slot"][k] for k in GROUP}, GROUP)
        it = store.get(fx.DOSSIER_ID)
        self.assertEqual(it["status"], "actioned")
        self.assertEqual(it["actioned"], {"task_short": short, "opportunity_id": None})
        # Nothing from the research went into Austin's own file.
        self.assertNotIn("Retries and backoff", (self.root / "tasks" / short / "submission.md").read_text())

    def test_place_name_comes_from_the_brief_when_there_is_one(self):
        brief = {"schema": "johto.brief/1", "dossier_id": fx.DOSSIER_ID, "name": "Widget",
                 "places": [{"id": "p2", "name": "Queues under load"}], "seeds": []}
        (store.P.dir / f"{fx.DOSSIER_ID}.brief.json").write_text(json.dumps(brief))
        j = self.claim_ok(origin={"kind": "place", "place_id": "p2", "name": "something else"}, slot=GROUP)
        meta = workspace.load(self.root / "tasks" / j["result"]["short"])
        self.assertEqual(meta["label"], "Widget · Queues under load")
        self.assertEqual(meta["origin"]["name"], "Queues under load")

    def test_undo_puts_the_research_back(self):
        j = self.claim_ok(origin={"kind": "place", "place_id": "p1", "name": "Retries"}, slot=GROUP)
        short = j["result"]["short"]
        st, res = call("POST", f"/api/undo/{j['result']['undo']['action_id']}")
        self.assertEqual(st, 200, res)
        self.assertEqual(done(res["job"])["state"], "succeeded")
        self.assertFalse((self.root / "tasks" / short).exists())
        it = store.get(fx.DOSSIER_ID)
        self.assertEqual((it["status"], it["actioned"]), ("ready", None))
        self.assertEqual(fake.status_name(fake.task(fx.TASKS[0]["task_id"])), "Ready to Claim")

    def test_already_actioned_research_stays_as_it_was_after_undo(self):
        store.update(fx.DOSSIER_ID, status="actioned", actioned={"task_short": "zzzz9999", "opportunity_id": "o1"})
        j = self.claim_ok(origin={"kind": "place", "place_id": "p1", "name": "Retries"}, slot=GROUP)
        st, res = call("POST", f"/api/undo/{j['result']['undo']['action_id']}")
        self.assertEqual(st, 200, res)
        done(res["job"])
        it = store.get(fx.DOSSIER_ID)
        self.assertEqual((it["status"], it["actioned"]["task_short"]), ("actioned", "zzzz9999"))

    def test_bad_origins_are_refused_before_studio(self):
        cases = [
            ({"origin": {"kind": "seed", "opportunity_id": "o9"}}, 404),        # no such problem
            ({"origin": {"kind": "place", "place_id": "x1", "name": "A"}}, 400),
            ({"origin": {"kind": "place", "place_id": "p1"}}, 400),            # no name, no brief
            ({"origin": {"kind": "place", "place_id": "p1", "name": "A"}, "opportunity_id": "o1"}, 400),
            ({"origin": {"kind": "seed", "opportunity_id": "o1"}, "opportunity_id": "o2"}, 400),
            ({"origin": {"kind": "other"}}, 400),
        ]
        for body, want in cases:
            st, res = self.post(**body)
            self.assertEqual(st, want, (body, res))
        st, res = call("POST", "/api/claim", {"lead_id": None, "origin": {"kind": "place", "place_id": "p1"},
                                              **VARIANT})
        self.assertEqual(st, 400, res)
        self.assertFalse(any(p.name != ".unclaimed" for p in (self.root / "tasks").iterdir()))
        self.assertEqual(store.get(fx.DOSSIER_ID)["status"], "ready")

    def test_claim_without_origin_is_unchanged(self):
        j = self.claim_ok(opportunity_id="o1", task_id=fx.TASKS[0]["task_id"])
        meta = workspace.load(self.root / "tasks" / j["result"]["short"])
        self.assertNotIn("origin", meta)
        self.assertEqual(meta["label"], "widget · Retry storms in the queue")
        self.assertEqual(store.get(fx.DOSSIER_ID)["status"], "ready")


if __name__ == "__main__":
    unittest.main()
