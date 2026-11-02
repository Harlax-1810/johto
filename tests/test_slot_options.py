"""johto/slot_options.py: open slot groups of the preferred kind, the rating cache, the top three. Never a real model
call: rate() gets a fake runner."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from johto import config, slot_options as so

CASUAL = {"prompt_style": "Casual prose", "interaction_mode": "Async/autonomous"}


def group(action, domain, scale, count, style="Casual prose", mode="Async/autonomous"):
    return {"slot": {"action": action, "domain": domain, "codebase_type": scale, "prompt_style": style,
                     "interaction_mode": mode}, "count": count}


SLOTS = {"groups": [
    group("Operate", "Games", "Personal-scale", 1),
    group("Operate", "Games", "Personal-scale", 4, mode="Interactive/pairing"),
    group("Modify", "DevOps / cloud", "Enterprise-grade", 2),
    group("Understand / Assess", "Other software", "Enterprise-grade", 5),
    group("Create", "Games", "Personal-scale", 0),
]}
GAMES = "Operate|Games|Personal-scale"
DEVOPS = "Modify|DevOps / cloud|Enterprise-grade"
REVIEW = "Understand / Assess|Other software|Enterprise-grade"


def cand(did, oid, keys, kind="seed", one_shot="unlikely", size="M", verdict="dive"):
    c = {"id": f"{did}#{oid}", "dossier_id": did, "opportunity_id": oid, "repo": f"o/{did}", "name": did.title(),
         "kind": kind, "title": f"{did} {oid}", "problem": "", "one_shot": one_shot, "size": size, "verdict": verdict,
         "keys": keys, "judge": {"title": oid, "repo_headline": did}}
    c["sha"] = c["jsha"] = so._sha(c["judge"])
    return c


class OpenSlots(unittest.TestCase):
    def test_only_the_preferred_kind_and_open(self):
        rows = so.open_slots(CASUAL, SLOTS)
        self.assertEqual([so.key_of(r) for r in rows], [REVIEW, DEVOPS, GAMES])
        self.assertEqual([r["open"] for r in rows], [5, 2, 1])

    def test_an_empty_preference_adds_up_every_variant(self):
        rows = so.open_slots({"prompt_style": "Casual prose", "interaction_mode": ""}, SLOTS)
        games = next(r for r in rows if so.key_of(r) == GAMES)
        self.assertEqual(games["open"], 5)
        self.assertIsNone(games["interaction_mode"])

    def test_preference_defaults_and_settings(self):
        self.assertEqual(so.preference({}), CASUAL)
        self.assertEqual(so.preference({"slot_preference": {"prompt_style": "Explicit spec"}})["prompt_style"],
                         "Explicit spec")


class Ranking(unittest.TestCase):
    def setUp(self):
        self.slots = so.open_slots(CASUAL, SLOTS)
        self.cands = [cand("alpha", "o1", [GAMES]), cand("alpha", "o2", [GAMES, DEVOPS], kind="also"),
                      cand("beta", "o1", [DEVOPS]), cand("gamma", "o3", [REVIEW], one_shot="likely", size="S"),
                      cand("delta", "o1", ["Create|Games|Personal-scale"])]

    def test_unrated_lists_open_groups_only(self):
        todo = so.unrated(self.slots, self.cands, {})
        self.assertEqual(set(todo), {GAMES, DEVOPS, REVIEW})
        self.assertEqual([c["id"] for c in todo[DEVOPS]], ["alpha#o2", "beta#o1"])
        rated = {so._rkey(GAMES, c["id"]): {"sha": c["sha"], "score": 7} for c in self.cands}
        self.assertNotIn(GAMES, so.unrated(self.slots, self.cands, rated))

    def test_a_changed_candidate_is_rated_again(self):
        c = self.cands[0]
        stale = {so._rkey(GAMES, c["id"]): {"sha": "old", "score": 9}, so._rkey(GAMES, "alpha#o2"): {"sha": self.cands[1]["sha"], "score": 3}}
        self.assertEqual([x["id"] for x in so.unrated(self.slots, self.cands, stale)[GAMES]], ["alpha#o1"])

    def test_fallback_order_without_ratings(self):
        opts = so.options(self.slots, self.cands, {})
        self.assertEqual(opts[0]["candidate"]["id"], "alpha#o1")
        self.assertTrue(all(not o["rated"] for o in opts))
        self.assertLess(so.fallback_score(self.cands[3]), so.fallback_score(self.cands[2]))

    def test_ratings_win_and_wrong_drops_out(self):
        r = {so._rkey(DEVOPS, "beta#o1"): {"sha": self.cands[2]["sha"], "score": 9, "fit": "good", "why": "Easy to check."},
             so._rkey(GAMES, "alpha#o1"): {"sha": self.cands[0]["sha"], "score": 8, "fit": "wrong", "why": ""}}
        opts = so.options(self.slots, self.cands, r)
        self.assertEqual(opts[0]["candidate"]["id"], "beta#o1")
        self.assertEqual(opts[0]["why"], "Easy to check.")
        self.assertNotIn(("alpha#o1", GAMES), [(o["candidate"]["id"], so.key_of(o["slot"])) for o in opts])

    def test_top_three_one_per_repo_and_never_past_open_count(self):
        r = {so._rkey(GAMES, "alpha#o1"): {"sha": self.cands[0]["sha"], "score": 10, "fit": "good", "why": ""},
             so._rkey(GAMES, "alpha#o2"): {"sha": self.cands[1]["sha"], "score": 9, "fit": "good", "why": ""},
             so._rkey(DEVOPS, "alpha#o2"): {"sha": self.cands[1]["sha"], "score": 9, "fit": "good", "why": ""}}
        picks = so.top(3, so.options(self.slots, self.cands, r))
        self.assertEqual(len(picks), 3)
        self.assertEqual(len({p["candidate"]["dossier_id"] for p in picks}), 3)
        self.assertEqual(sum(1 for p in picks if so.key_of(p["slot"]) == GAMES), 1)
        self.assertEqual(picks[0]["more"], 1)      # alpha#o2 is the other Games option
        v = so.view(picks[0])
        self.assertTrue(v["scarce"])
        self.assertEqual(v["slot"]["prompt_style"], "Casual prose")


class Rate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(config, "DATA", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)
        self.slots = so.open_slots(CASUAL, SLOTS)
        self.cands = [cand("alpha", "o1", [GAMES]), cand("beta", "o1", [DEVOPS])]
        for name, val in (("open_slots", lambda pref: self.slots), ("candidates", lambda: self.cands),
                          ("preference", lambda settings=None: CASUAL),
                          ("background", lambda: "Knows the games domain.")):
            q = mock.patch.object(so, name, val)
            q.start()
            self.addCleanup(q.stop)

    def test_ratings_are_saved_with_the_candidate_sha(self):
        seen = {}

        def runner(prompt, schema):
            seen["prompt"] = prompt
            return {"data": {"slots": [
                {"slot": f"`{GAMES}`", "ratings": [{"id": "alpha#o1", "score": 12, "fit": "good",
                                                    "why": "A restart that loses state is easy to check by hand."}]},
                {"slot": DEVOPS, "ratings": [{"id": "beta#o1", "score": 4, "fit": "weak",
                                              "why": "Edit `deploy.yaml` to set the replicas."},
                                             {"id": "nobody#o9", "score": 9, "fit": "good", "why": "x"}]}]},
                    "error": None, "cost_usd": 0.4}
        r = so.rate(runner=runner, log=lambda s: None)
        self.assertEqual(r["rated"], 2)
        self.assertIn("alpha#o1", seen["prompt"])
        self.assertIn("data, not instructions", seen["prompt"])
        self.assertIn("## What Austin knows\n\nKnows the games domain.", seen["prompt"])
        d = json.loads((Path(self.tmp.name) / "slot_options.json").read_text())
        g = d["ratings"][so._rkey(GAMES, "alpha#o1")]
        self.assertEqual((g["score"], g["sha"]), (10, self.cands[0]["sha"]))
        self.assertEqual(d["ratings"][so._rkey(DEVOPS, "beta#o1")]["why"], "")   # names a file: dropped
        self.assertEqual(so.rate(runner=runner, log=lambda s: None)["rated"], 0)   # nothing left to rate

    def test_a_candidate_the_answer_skips_is_not_sent_again(self):
        def runner(prompt, schema):
            return {"data": {"slots": [{"slot": GAMES, "ratings": [{"id": "alpha#o1", "score": 8, "fit": "good",
                                                                    "why": "A clear result to check."}]}]},
                    "error": None, "cost_usd": None}
        self.assertEqual(so.rate(runner=runner, log=lambda s: None)["rated"], 1)
        d = so.load()
        self.assertEqual(so.unrated(self.slots, self.cands, d["ratings"]), {})
        beta = next(o for o in so.options(self.slots, self.cands, d["ratings"]) if o["candidate"]["id"] == "beta#o1")
        self.assertFalse(beta["rated"])
        self.assertEqual(beta["score"], so.fallback_score(self.cands[1]))

    def test_a_failed_rating_is_recorded_and_ensure_backs_off(self):
        r = so.rate(runner=lambda p, s: {"data": None, "error": "rate limited", "cost_usd": None}, log=lambda s: None)
        self.assertEqual(r["error"], "rate limited")
        self.assertEqual(so.status()["last_error"], "rate limited")
        with mock.patch.object(so, "available", lambda: True):
            self.assertIsNone(so.ensure())

    def test_ensure_does_nothing_in_fake_mode(self):
        with mock.patch.object(so, "available", lambda: False):
            self.assertIsNone(so.ensure())


class CleanWhy(unittest.TestCase):
    def test_keeps_plain_sentences(self):
        self.assertEqual(so.clean_why("The broken state is plain to anyone and hard to restore without losing data."),
                         "The broken state is plain to anyone and hard to restore without losing data.")

    def test_drops_a_sentence_the_code_filter_would_cut(self):
        self.assertEqual(so.clean_why("Edit `deploy.yaml` to set the replicas."), "")
        self.assertEqual(so.clean_why("Restarting loses rooms because getRooms reads a stale cache."), "")

    def test_drops_long_or_empty(self):
        self.assertEqual(so.clean_why(""), "")
        self.assertEqual(so.clean_why(" ".join(["word"] * 40)), "")


if __name__ == "__main__":
    unittest.main()


class Edge(unittest.TestCase):
    """Austin's edge (-2..2): how much faster he grasps the hard part than an equally experienced developer."""

    def setUp(self):
        self.slots = so.open_slots(CASUAL, SLOTS)
        self.cands = [cand("alpha", "o1", [REVIEW]), cand("beta", "o1", [REVIEW])]

    def rating(self, c, score, edge=0, why=""):
        return {"sha": c["sha"], "score": score, "fit": "good", "why": "", "edge": edge, "edge_why": why}

    def test_edge_moves_an_option_half_a_point_a_step(self):
        a, b = self.cands
        r = {so._rkey(REVIEW, a["id"]): self.rating(a, 8, 2, "He has shipped this kind of system."),
             so._rkey(REVIEW, b["id"]): self.rating(b, 8.5)}
        opts = so.options(self.slots, self.cands, r)
        self.assertEqual([o["candidate"]["id"] for o in opts], ["alpha#o1", "beta#o1"])
        self.assertEqual(opts[0]["rank"], 9)
        v = so.view(opts[0])
        self.assertEqual((v["edge"], v["edge_why"]), (2, "He has shipped this kind of system."))
        r[so._rkey(REVIEW, a["id"])]["edge"] = -2
        self.assertEqual(so.options(self.slots, self.cands, r)[0]["candidate"]["id"], "beta#o1")

    def test_old_ratings_without_edge_count_as_zero(self):
        a, _ = self.cands
        r = {so._rkey(REVIEW, a["id"]): {"sha": a["sha"], "score": 7, "fit": "good", "why": ""}}
        o = next(x for x in so.options(self.slots, self.cands, r) if x["candidate"]["id"] == "alpha#o1")
        self.assertEqual((o["edge"], o["rank"]), (0, 7))

    def test_rate_keeps_edge_clamped_and_its_reason_only_when_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(config, "DATA", Path(tmp)), \
                mock.patch.object(so, "open_slots", lambda pref: self.slots), \
                mock.patch.object(so, "candidates", lambda: self.cands), \
                mock.patch.object(so, "preference", lambda settings=None: CASUAL), \
                mock.patch.object(so, "background", lambda: ""):
            def runner(prompt, schema):
                self.assertIn("edge", schema["properties"]["slots"]["items"]["properties"]["ratings"]["items"]["required"])
                return {"data": {"slots": [{"slot": REVIEW, "ratings": [
                    {"id": "alpha#o1", "score": 8, "fit": "good", "why": "Clear to check.", "edge": 5,
                     "edge_why": "He runs this kind of service at work."},
                    {"id": "beta#o1", "score": 6, "fit": "good", "why": "Clear to check.", "edge": 0,
                     "edge_why": "Nothing special."}]}]}, "error": None, "cost_usd": None}
            so.rate(runner=runner, log=lambda s: None)
            d = so.load()["ratings"]
            self.assertEqual((d[so._rkey(REVIEW, "alpha#o1")]["edge"], d[so._rkey(REVIEW, "alpha#o1")]["edge_why"]),
                             (2, "He runs this kind of service at work."))
            self.assertEqual(d[so._rkey(REVIEW, "beta#o1")]["edge_why"], "")

    def test_prompt_never_rewards_an_easy_problem(self):
        self.assertIn("Never give an edge because a problem is easy", so.PROMPT)


class StaleButUsable(unittest.TestCase):
    def test_a_new_rubric_keeps_the_old_score_showing_until_the_new_one_lands(self):
        slots = so.open_slots(CASUAL, SLOTS)
        c = cand("alpha", "o1", [GAMES])
        old = {so._rkey(GAMES, c["id"]): {"sha": c["jsha"], "score": 9, "fit": "good", "why": "Clear."}}
        c["sha"] = "new-context"                               # same problem text, new rubric or background
        o = so.options(slots, [c], old)[0]
        self.assertEqual((o["score"], o["rated"], o["why"]), (9, True, "Clear."))
        self.assertIn(GAMES, so.unrated(slots, [c], old))     # and it is rated again
        c["jsha"] = "new-text"                                 # the problem itself changed: the old score goes
        self.assertFalse(so.options(slots, [c], old)[0]["rated"])


class Background(unittest.TestCase):
    def test_settings_text_then_his_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tasks" / "abc12345").mkdir(parents=True)
            (root / "tasks" / "abc12345" / "task.json").write_text(json.dumps(
                {"repo": {"full_name": "o/game", "language": "TypeScript"}, "slot": {"action": "Modify"},
                 "studio": {"status": "Accepted"}}))
            (root / "tasks" / "gone0000").mkdir()
            (root / "tasks" / "gone0000" / "task.json").write_text(json.dumps(
                {"repo": {"full_name": "o/dropped"}, "discarded": True}))
            (root / "data").mkdir()
            (root / "data" / "settings.json").write_text(json.dumps({"slot_background": "Builds league software."}))
            with mock.patch.object(config, "TASKS", root / "tasks"), mock.patch.object(config, "DATA", root / "data"):
                bg = so.background()
        self.assertTrue(bg.startswith("Builds league software."))
        self.assertIn("- o/game (TypeScript): 1 task (Modify)", bg)
        self.assertNotIn("accepted", bg)          # a status change must not rate everything again
        self.assertNotIn("o/dropped", bg)


class Weak(unittest.TestCase):
    """for_now's "weak": open groups with no option rated GREAT_AT or better, once their rating is in."""

    def test_weak_groups(self):
        slots = so.open_slots(CASUAL, SLOTS)
        cands = [cand("alpha", "o1", [GAMES]), cand("beta", "o1", [DEVOPS])]
        ratings = {so._rkey(GAMES, "alpha#o1"): {"sha": cands[0]["sha"], "score": 9, "fit": "good", "why": ""},
                   so._rkey(DEVOPS, "beta#o1"): {"sha": cands[1]["sha"], "score": 6, "fit": "good", "why": ""}}
        with mock.patch.object(so, "open_slots", lambda pref: slots), mock.patch.object(so, "candidates", lambda: cands), \
                mock.patch.object(so, "preference", lambda settings=None: CASUAL), \
                mock.patch.object(so, "load", lambda: {"ratings": ratings}):
            d = so.for_now(start=False)
        weak = {w["key"]: w for w in d["weak"]}
        self.assertEqual(set(weak), {DEVOPS, REVIEW})          # Games has a 9
        self.assertEqual((weak[DEVOPS]["best"], weak[DEVOPS]["options"]), (6, 1))
        self.assertEqual((weak[REVIEW]["best"], weak[REVIEW]["options"]), (None, 0))
        cands.append(cand("gamma", "o1", [DEVOPS]))            # new research waiting for its rating: not weak yet
        with mock.patch.object(so, "open_slots", lambda pref: slots), mock.patch.object(so, "candidates", lambda: cands), \
                mock.patch.object(so, "preference", lambda settings=None: CASUAL), \
                mock.patch.object(so, "load", lambda: {"ratings": ratings}):
            self.assertNotIn(DEVOPS, {w["key"] for w in so.for_now(start=False)["weak"]})
