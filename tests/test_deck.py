"""johto.deck: ready picks with the open slots they fit, familiar repos first, used and unlikely work left out.

Run: .venv/bin/python -m unittest tests.test_deck -v
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import test_actions_support as fx  # noqa: E402

from johto import cli, config, deck  # noqa: E402

FIT = {k: fx.SLOT[k] for k in ("action", "domain", "codebase_type")}


def opp(oid: str, title: str, **kw) -> dict:
    base = {"type": "opportunity", "id": oid, "title": title, "area": "", "ambition": "", "why_not_done": "",
            "evidence": [], "where_models_fail": [], "bounded_task": {"size": "M", "shape": ""},
            "slot_fit": [FIT], "one_shot": {"likelihood": "unlikely"}, "upstream": {"looks_solved": False}}
    base.update(kw)
    return base


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        fx.make_root(self.root)
        self.patches = fx.patch_config(self.root) + [mock.patch("johto.scout.rank.rank", return_value=[])]
        for p in self.patches:
            p.start()
        self.dossier("acme__widget__01234567", "acme/widget", [opp("o1", "Retry storms in the queue")])

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def dossier(self, did: str, repo: str, opps: list[dict], status: str = "ready") -> None:
        dd = self.root / "data" / "dossiers"
        idx_p = dd / "index.json"
        idx = json.loads(idx_p.read_text()) if idx_p.exists() else {"target": 10, "paused": False, "items": []}
        idx["items"].append({"id": did, "repo": repo, "status": status, "commit": fx.COMMIT, "rank": 0})
        idx_p.write_text(json.dumps(idx))
        (dd / f"{did}.json").write_text(json.dumps({"id": did, "repo": repo, "commit": fx.COMMIT,
                                                    "verdict": {"promise": "high"}, "blocks": opps}))

    def task(self, short: str, repo: str, action: str = "Modify", prompt: str = "", **meta) -> Path:
        d = self.root / "tasks" / short
        d.mkdir(parents=True)
        (d / "task.json").write_text(json.dumps({"task_id": f"task_{short}", "task_name": f"Task {short}",
                                                 "slot": {**fx.SLOT, "action": action},
                                                 "repo": {"full_name": repo}, "runs": [], **meta}))
        (d / "submission.md").write_text(f"## Opening Prompt\n\n{prompt}\n")
        return d


class Picks(Base):
    def test_a_pick_shows_its_open_combos_and_variants(self):
        ps = deck.picks()
        self.assertEqual(len(ps), 1)
        p = ps[0]
        self.assertEqual((p["kind"], p["dossier_id"], p["opportunity_id"]), ("dossier", "acme__widget__01234567", "o1"))
        self.assertEqual(p["open_total"], 4)
        f = p["fits"][0]
        self.assertEqual((f["action"], f["open"]), ("Modify", 4))
        self.assertEqual([(v["prompt_style"], v["count"]) for v in f["variants"]],
                         [("Casual prose", 3), ("Explicit spec", 1)])
        self.assertFalse(p["familiar"])
        self.assertIn("--slot 'Modify | Backend / API | Personal-scale'", p["command"])
        self.assertTrue(p["url"].endswith("/repos/acme__widget__01234567#o=o1"))

    def test_familiar_repos_come_first(self):
        self.dossier("other__repo__00000000", "other/repo", [opp("o1", "Cache drift after restarts")])
        self.task("aaaa1111", "acme/widget", action="Create", prompt="Something about exports.")
        ps = deck.picks()
        self.assertEqual(ps[0]["repo"], "acme/widget")
        self.assertTrue(ps[0]["familiar"])
        self.assertEqual(ps[0]["used_in"], ["aaaa1111"])
        self.assertTrue(any("you know this repo" in w for w in ps[0]["why"]))
        self.assertTrue(any("unlike your others" in w for w in ps[0]["why"]))
        self.assertFalse(ps[1]["familiar"])

    def test_an_opportunity_a_task_took_is_left_out(self):
        self.task("aaaa1111", "acme/widget", dossier={"id": "acme__widget__01234567", "opportunity_id": "o1"})
        self.assertEqual(deck.picks(), [])

    def test_a_task_claimed_another_way_that_matches_the_opportunity_is_left_out(self):
        self.task("aaaa1111", "acme/widget", prompt="Retry storms keep hitting the queue after a deploy.")
        self.assertEqual(deck.picks(), [])

    def test_unlikely_picks_are_left_out(self):
        dd = self.root / "data" / "dossiers"
        for p in dd.glob("*.json"):
            p.unlink()
        self.dossier("acme__widget__01234567", "acme/widget", [
            opp("o1", "Solved already", upstream={"looks_solved": True}),
            opp("o2", "Too easy", one_shot={"likelihood": "likely"}),
            opp("o3", "No open slot", slot_fit=[{**FIT, "action": "Operate"}]),
            opp("o4", "A real one")])
        self.assertEqual([p["opportunity_id"] for p in deck.picks()], ["o4"])

    def test_at_most_two_per_repo_and_three_in_all(self):
        self.dossier("other__repo__00000000", "other/repo", [opp(f"o{i}", f"Other problem {i}") for i in range(1, 4)])
        self.task("aaaa1111", "other/repo", action="Create", prompt="unrelated words entirely")
        ps = deck.picks()
        self.assertEqual(len(ps), 3)
        self.assertEqual(sum(1 for p in ps if p["repo"] == "other/repo"), 2)

    def test_leads_from_scout_rank_join_the_deck(self):
        lead = json.loads((self.root / "data" / "scout" / "leads.json").read_text())[0]
        row = {"lead": copy.deepcopy(lead), "verdict": {"overall": "ok"}, "score": 9}
        with mock.patch("johto.scout.rank.rank", return_value=[row]):
            ps = deck.picks()
        kinds = {p["kind"]: p for p in ps}
        self.assertIn("lead", kinds)
        self.assertEqual(kinds["lead"]["command"], f"johto start --lead {fx.LEAD_ID}")
        lead["status"] = "claimed"
        with mock.patch("johto.scout.rank.rank", return_value=[{**row, "lead": lead}]):
            self.assertNotIn("lead", {p["kind"] for p in deck.picks()})

    def test_an_overlapping_studio_prompt_costs_score(self):
        rows = json.loads((self.root / "data" / "studio_tasks.json").read_text())
        rows.append({"task_id": "x", "task_name": "Task zzzz0000", "repo": "acme/widget",
                     "opening_prompt": "Retry storms in the queue never stop growing"})
        (self.root / "data" / "studio_tasks.json").write_text(json.dumps(rows))
        p = deck.picks()[0]
        self.assertEqual(p["overlap"]["with"], "Task zzzz0000")
        self.assertTrue(any("close to Task zzzz0000" in w for w in p["why"]))

    def test_render_and_cli(self):
        text = deck.render(deck.picks())
        self.assertIn("acme/widget · Retry storms in the queue", text)
        self.assertIn("fits Modify | Backend / API | Personal-scale: 4 open", text)
        self.assertIn("claim: johto start --repo acme/widget", text)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["deck", "--json"])
        self.assertEqual(json.loads(buf.getvalue())[0]["opportunity_id"], "o1")
        self.assertIn("No ready picks", deck.render([]))


if __name__ == "__main__":
    unittest.main()
