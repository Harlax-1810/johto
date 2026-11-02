"""Find work, the repo brief and the slot map routes (johto/web/api_discover.py, spec 3.2 to 3.4 and 7.2).

Called through the whole app's router (session and demo guards included) against a temp data dir with the
Vidlabel-shaped dossier and slots from test_brief.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from test_brief import DID, REPO, SLOTS, Root  # noqa: E402
from test_dossier_api import call  # noqa: E402

from johto import config  # noqa: E402
from johto.dossier import store, worker  # noqa: E402
from johto.web import api_discover as ad  # noqa: E402

DEMO_ID = "acme__widget__9f1e2d3c"   # the fixture dossier the demo ships


class Base(Root):
    def setUp(self):
        super().setUp()
        self.tasks = self.tmp / "tasks"
        self.tasks.mkdir()
        self.cp = [mock.patch.object(config, "DATA", store.P.data), mock.patch.object(config, "TASKS", self.tasks),
                   mock.patch.object(worker.WORKER, "kick"), mock.patch.object(worker.WORKER, "start")]
        for p in self.cp:
            p.start()

    def tearDown(self):
        for p in self.cp:
            p.stop()
        super().tearDown()

    def task(self, short: str, **meta) -> None:
        d = self.tasks / short
        d.mkdir()
        (d / "task.json").write_text(json.dumps({"task_id": f"task_{short}", "task_name": f"Task {short}",
                                                 "repo": {"full_name": REPO}, **meta}))

    def get(self, path, demo=False):
        status, body = call("GET", path, demo=demo)
        self.assertEqual(status, 200, body)
        return body


class FindWork(Base):
    def test_shape_and_lede_with_a_brief(self):
        self.install_example()
        body = self.get("/api/discover")
        self.assertEqual(set(body), {"lede", "slots", "open_total", "repos", "research", "set_aside"})
        self.assertEqual(body["lede"]["title"], "Vidlabel looks strongest today.")
        self.assertEqual(body["open_total"], sum(g["count"] for g in SLOTS["groups"]))
        row = body["repos"][0]
        self.assertEqual((row["id"], row["name"], row["state"], row["verdict"]), (DID, "Vidlabel", "ready", "dive"))
        self.assertTrue(row["list_line"])
        self.assertEqual(len(row["tags"]), 3)            # the first three place names
        self.assertLessEqual(len(row["top_fits"]), 2)
        self.assertTrue(all(f["open"] > 0 for f in row["top_fits"]))
        self.assertEqual([f["open"] for f in row["top_fits"]], sorted((f["open"] for f in row["top_fits"]), reverse=True))

    def test_slots_list_every_open_kind_with_all_five_fields(self):
        self.install_example()
        body = self.get("/api/discover")
        cells = body["slots"]
        self.assertEqual(len(cells), len(SLOTS["groups"]))
        self.assertEqual(sum(c["open"] for c in cells), body["open_total"])
        for c in cells:
            self.assertEqual(set(c), {"action", "domain", "codebase_type", "prompt_style", "interaction_mode", "open"})
        spec = [c for c in cells if c["prompt_style"] == "Explicit spec"]
        self.assertEqual([(c["action"], c["interaction_mode"], c["open"]) for c in spec],
                         [("Modify", "Async/autonomous", 5)])

    def test_seeds_carry_every_group_they_fit(self):
        """The page recounts each suggestion under his filters, so a row lists each seed's slot groups."""
        self.install_example()
        row = self.get("/api/discover")["repos"][0]
        self.assertTrue(row["seeds"])
        self.assertTrue(all(x["title"] and x["keys"] for x in row["seeds"]))
        self.assertIn("Operate|DevOps / cloud|Enterprise-grade", {k for x in row["seeds"] for k in x["keys"]})
        self.assertTrue(set(row["fit_keys"]) <= set(row["fits_any"]))
        self.assertEqual(row["open_seeds"], sum(1 for x in row["seeds"] if any(
            g["count"] for g in SLOTS["groups"]
            if "|".join((g["slot"]["action"], g["slot"]["domain"], g["slot"]["codebase_type"])) in x["keys"])))

    def test_a_seed_already_his_is_left_out(self):
        self.install_example()
        before = {x["seed"] for x in self.get("/api/discover")["repos"][0]["seeds"]}
        self.assertIn("o4", before)
        self.task("aaaa1111", studio={"status": "Writing"}, dossier={"id": DID, "opportunity_id": "o4"})
        after = {x["seed"] for x in self.get("/api/discover")["repos"][0]["seeds"]}
        self.assertEqual(after, before - {"o4"})

    def test_order_ignores_group_size_and_puts_few_left_first(self):
        """One open slot is enough; a suggestion with only a few left comes first because it can go any minute."""
        rows = [{"state": "ready", "verdict": "dive", "scarce": False, "open_seeds": 3, "name": "Big"},
                {"state": "ready", "verdict": "dive", "scarce": True, "open_seeds": 1, "name": "Last one"},
                {"state": "ready", "verdict": "maybe", "scarce": True, "open_seeds": 3, "name": "Maybe"},
                {"state": "ready", "verdict": "dive", "scarce": False, "open_seeds": 1, "name": "Alpha"}]
        self.assertEqual([r["name"] for r in ad._sort_rows(rows)], ["Last one", "Big", "Alpha", "Maybe"])

    def test_brief_coming_row_has_fit_keys_and_no_seeds(self):
        row = self.get("/api/discover")["repos"][0]
        self.assertEqual(row["state"], "brief_coming")
        self.assertEqual(row["seeds"], [])
        self.assertTrue(row["fit_keys"])
        self.assertTrue(set(row["fit_keys"]) <= set(row["fits_any"]))

    def test_filter_key_matches_a_seed_fit(self):
        self.install_example()
        row = self.get("/api/discover")["repos"][0]
        self.assertIn("Operate|DevOps / cloud|Enterprise-grade", row["fits_any"])   # o4's fit

    def test_brief_coming_row_uses_the_headline_and_two_problems(self):
        row = self.get("/api/discover")["repos"][0]
        self.assertEqual(row["state"], "brief_coming")
        self.assertEqual(row["one_liner"], self.dossier["headline"])
        self.assertEqual(len(row["problems"]), 2)
        self.assertIsNone(row["list_line"])

    def test_brief_coming_row_skips_problems_already_claimed(self):
        first = ad.opp_title(next(b for b in self.dossier["blocks"] if b.get("id") == "o1"))
        self.assertIn(first, self.get("/api/discover")["repos"][0]["problems"])
        self.task("aaaa1111", studio={"status": "Writing"}, dossier={"id": DID, "opportunity_id": "o1"})
        problems = self.get("/api/discover")["repos"][0]["problems"]
        self.assertNotIn(first, problems)
        self.assertEqual(len(problems), 2)

    def test_researching_row_and_set_aside(self):
        with store.edit() as idx:
            idx["items"].append({**store.new_item({"full_name": "o/gen", "commit": "1" * 40}, 2),
                                 "status": "generating"})
            idx["items"].append({**store.new_item({"full_name": "o/nope", "commit": "2" * 40}, 3),
                                 "status": "declined", "decline": {"reason": "too_big"},
                                 "closed_at": "2026-10-30T10:00:00-0700"})
        body = self.get("/api/discover")
        gen = next(r for r in body["repos"] if r["repo"] == "o/gen")
        self.assertEqual((gen["state"], gen["generating"], gen["top_fits"]), ("researching", True, []))
        self.assertEqual((gen["seeds"], gen["fit_keys"]), ([], []))
        self.assertNotIn("o/nope", [r["repo"] for r in body["repos"]])
        self.assertEqual([s["owner"] for s in body["set_aside"]], ["o"])
        self.assertEqual(body["research"]["generating"], "Gen")
        self.assertIn("Gen", body["research"]["today_line"])

    def test_yours_counts_tasks_on_the_repo(self):
        self.task("aaaa1111", studio={"status": "Writing"})
        self.task("bbbb2222", studio={"status": "Accepted"})
        self.task("cccc3333", studio={"status": "Writing"}, unclaimed_at="2026-10-30T00:00:00Z")
        row = self.get("/api/discover")["repos"][0]
        self.assertEqual(row["yours"], {"done": 1, "in_flight": 1})

    def test_research_paused_words(self):
        with store.edit() as idx:
            idx["paused"] = True
        self.assertEqual(self.get("/api/discover")["research"]["today_line"], "Research is paused.")


class Brief(Base):
    def test_ready_brief(self):
        self.install_example()
        body = self.get(f"/api/discover/{DID}")
        for k in ("id", "repo", "github_url", "status", "name", "state", "brief", "fits", "used", "also_researched",
                  "generated_at", "drill"):
            self.assertIn(k, body)
        self.assertEqual((body["state"], body["name"], body["github_url"]), ("ready", "Vidlabel", f"https://github.com/{REPO}"))
        b = body["brief"]
        self.assertEqual([s["opportunity_id"] for s in b["seeds"]], ["o2", "o3", "o4"])
        for s in b["seeds"]:
            self.assertIn(s["one_shot"], ("unlikely", "possible", "likely", "not_checked"))
            for f in s["fits"]:
                self.assertIn("variants", f)
        o4 = next(s for s in b["seeds"] if s["opportunity_id"] == "o4")
        self.assertEqual((o4["size"], o4["fits"][0]["open"]), ("S", 24))
        for p in b["places"]:
            self.assertLessEqual(len(p["fits"]), 4)
            self.assertTrue(all(f["open"] > 0 for f in p["fits"]))
        self.assertIsInstance(b["facts"]["fails"], list)
        self.assertEqual({o["opportunity_id"] for o in body["also_researched"]}, {"o1", "o5"})
        self.assertEqual([f["open"] for f in body["fits"]], sorted((f["open"] for f in body["fits"]), reverse=True))
        self.assertTrue(body["drill"]["dossier_available"])

    def test_fallback_without_a_brief(self):
        body = self.get(f"/api/discover/{DID}")
        self.assertEqual(body["state"], "missing")
        b = body["brief"]
        self.assertEqual(len(b["seeds"]), 3)
        self.assertEqual(b["places"], [])
        self.assertIsNone(b["verdict"]["level"])
        for s in b["seeds"]:
            self.assertTrue(s["title"])
            self.assertIn("fits", s)

    def test_used_names_the_seed_and_the_place(self):
        self.install_example()
        self.task("aaaa1111", studio={"status": "Writing"}, label="Vidlabel · Repeat scans double-count loans",
                  origin={"kind": "seed", "dossier_id": DID, "opportunity_id": "o2"},
                  dossier={"id": DID, "opportunity_id": "o2"})
        self.task("bbbb2222", studio={"status": "Awaiting Review"}, label="Vidlabel · Reminders and nightly runs",
                  origin={"kind": "place", "dossier_id": DID, "place_id": "p3"},
                  dossier={"id": DID, "opportunity_id": None})
        used = self.get(f"/api/discover/{DID}")["used"]
        seed = next(u for u in used if u["kind"] == "seed")
        place = next(u for u in used if u["kind"] == "place")
        self.assertEqual((seed["id"], seed["short"], seed["stage_word"]), ("o2", "aaaa1111", "in progress"))
        self.assertEqual((place["id"], place["short"], place["stage_word"]), ("p3", "bbbb2222", "in review"))

    def test_a_task_from_a_scout_lead_uses_the_lead_title(self):
        """A task claimed from a scout lead, written in Studio (only a short prompt locally): the lead's title
        names it and helps match it to its seed."""
        self.install_example()
        b = self.get(f"/api/discover/{DID}")["brief"]
        seed = b["seeds"][0]
        (store.P.data / "scout").mkdir(parents=True, exist_ok=True)
        (store.P.data / "scout" / "leads.json").write_text(json.dumps([{"id": "Lx1", "title": seed["title"]}]))
        self.task("cccc3333", studio={"status": "Accepted"}, lead_id="Lx1",
                  slot={"action": seed["fits"][0]["action"]})
        used = self.get(f"/api/discover/{DID}")["used"]
        mine = next(u for u in used if u["short"] == "cccc3333")
        self.assertEqual((mine["kind"], mine["id"]), ("seed", seed["opportunity_id"]))
        label = next(t["label"] for t in ad.load_tasks() if t["short"] == "cccc3333")
        self.assertEqual(label, f"{REPO.split('/')[-1]} · {seed['title']}")

    def test_unknown_is_404(self):
        status, body = call("GET", "/api/discover/nobody__here__00000000")
        self.assertEqual((status, body["error"]["code"]), (404, "not_found"))

    def test_session_required(self):
        for path in ("/api/discover", f"/api/discover/{DID}", "/api/slots/map"):
            self.assertEqual(call("GET", path, cookie=False)[0], 401, path)


class SlotMap(Base):
    def test_cells_cover_every_action_domain_and_scale(self):
        self.install_example()
        body = self.get("/api/slots/map")
        self.assertEqual(set(body) >= {"synced_at", "synced_words", "actions", "domains", "scales", "cells", "names"}, True)
        self.assertEqual(len(body["cells"]), len(body["actions"]) * len(body["domains"]) * len(body["scales"]))
        cell = next(c for c in body["cells"] if (c["action"], c["domain"], c["codebase_type"]) ==
                    ("Operate", "DevOps / cloud", "Enterprise-grade"))
        self.assertEqual((cell["open"], cell["repos"]), (24, [DID]))
        empty = next(c for c in body["cells"] if (c["action"], c["domain"], c["codebase_type"]) ==
                     ("Create", "Web frontend", "Personal-scale"))
        self.assertEqual((empty["open"], empty["repos"]), (3, []))
        self.assertEqual(body["names"][DID], "Vidlabel")
        self.assertEqual(body["actions"][0], "Create")
        self.assertTrue(body["synced_words"].startswith("Synced"))

    def test_synced_words(self):
        now = 1_800_000_000
        iso = lambda s: __import__("time").strftime("%Y-%m-%dT%H:%M:%S%z", __import__("time").localtime(now - s))  # noqa: E731
        self.assertEqual(ad.synced_words(iso(20), now), "Synced just now")
        self.assertEqual(ad.synced_words(iso(2 * 3600 + 5), now), "Synced 2 hours ago")
        self.assertEqual(ad.synced_words(None, now), "Not synced yet")


class Demo(Base):
    def test_demo_answers_from_the_fixtures_only(self):
        self.install_example()
        self.task("aaaa1111", studio={"status": "Writing"})
        body = self.get("/api/discover", demo=True)
        self.assertEqual([r["id"] for r in body["repos"]], [DEMO_ID])
        self.assertEqual(body["set_aside"], [])
        self.assertEqual(body["repos"][0]["yours"]["done"], 0)   # never Austin's own tasks
        b = self.get(f"/api/discover/{DEMO_ID}", demo=True)
        self.assertEqual(b["id"], DEMO_ID)
        self.assertTrue(b["brief"]["seeds"])
        # Austin's research is not served in demo mode.
        status, _ = call("GET", f"/api/discover/{DID}", demo=True)
        self.assertEqual(status, 404)
        m = self.get("/api/slots/map", demo=True)
        self.assertNotIn(DID, {r for c in m["cells"] for r in c["repos"]})

    def test_demo_claim_writes_nothing(self):
        status, body = call("POST", "/api/claim", {"dossier_id": DID, "origin": {"kind": "place", "place_id": "p3"}},
                            demo=True)
        self.assertEqual(status, 202)
        self.assertNotIn("job", body)
        self.assertIn("nothing was written", body["toast"]["text"])
        self.assertEqual(list(self.tasks.iterdir()), [])
        self.assertEqual(store.get(DID)["status"], "ready")


class Words(unittest.TestCase):
    def test_hide_code_and_titles(self):
        self.assertEqual(ad.hide_code("`foo_bar()` in a/b/c.py:12"), "")
        self.assertEqual(ad.drop_commands("Small Go service. All testable with go test, no paid services."),
                         "Small Go service.")
        self.assertEqual(ad.plain_title("Output = ['value'] comes with KeyError: 'value'"), "A related upstream thread")
        self.assertEqual(ad.plain_title("Unsuccessful reprint of a label from the queue #412"),
                         "Unsuccessful reprint of a label from the queue")
        self.assertEqual(ad.opp_title({"title": "fix `parse_x()`", "area": "parsing"}), "Parsing")

    def test_a_task_with_no_origin_finds_its_seed(self):
        """Same slot action, at least two shared words, and a clear winner; otherwise no guess."""
        seeds = [{"id": "o1", "title": "Exported rows go missing when the export job retries",
                  "fits": [{"action": "Fix a bug"}]},
                 {"id": "o2", "title": "The login page loops forever", "fits": [{"action": "Fix a bug"}]},
                 {"id": "o3", "title": "Exported rows lose their labels", "fits": [{"action": "Add a feature"}]}]
        text = "When the export job retries, some exported rows go missing."
        self.assertEqual(ad.match_seed("Fix a bug / Backend", text, seeds)["id"], "o1")
        self.assertIsNone(ad.match_seed("Refactor", text, seeds))
        self.assertIsNone(ad.match_seed("Fix a bug", "Something unrelated entirely", seeds))

    def test_claim_label_rules(self):
        doc = {"id": DID, "repo": REPO, "blocks": [{"type": "opportunity", "id": "o2", "title": "Repeat scans"}]}
        with mock.patch.object(ad, "load_brief", lambda did, demo=False: None):
            label, origin = ad.claim_label(doc, {"kind": "seed", "opportunity_id": "o2"})
            self.assertEqual(label, "Vidlabel · Repeat scans")
            self.assertEqual(origin, {"kind": "seed", "dossier_id": DID, "opportunity_id": "o2", "title": "Repeat scans"})
            with self.assertRaises(ValueError):
                ad.claim_label(doc, {"kind": "seed", "opportunity_id": "o9"})
            with self.assertRaises(ValueError):
                ad.claim_label(doc, {"kind": "place", "place_id": "../x"})
            with self.assertRaises(ValueError):
                ad.claim_label(doc, {"kind": "other"})


if __name__ == "__main__":
    unittest.main()
