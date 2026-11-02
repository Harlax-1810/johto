"""The overlap guard knows the repo brief and the plain findings (spec 6.7 and 7.5): a protected field that copies a
brief sentence or a plain row is red, every earlier brief version still counts, and the cache follows the files."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_actions import Base  # noqa: E402
from test_brief import vidlabel_dossier, example  # noqa: E402
from test_sync import data_write  # noqa: E402

from johto import actions, plain, workspace  # noqa: E402
from johto.dossier import brief, store  # noqa: E402
from johto.web.guard import ledger, overlap  # noqa: E402


class LedgerBrief(Base):
    def setUp(self):
        super().setUp()
        ledger._CACHE.clear()
        overlap._INDEX.clear()
        meta = workspace.load(self.d)
        self.repo = meta["repo"]["full_name"]
        d = vidlabel_dossier()
        d["repo"] = self.repo
        d["id"] = store.make_id(self.repo, meta["repo"].get("commit") or "0" * 40)
        store.save_dossier(d)
        self.did = d["id"]
        actions.update_meta(self.d, lambda m: m.update(dossier={"id": self.did, "opportunity_id": "o2"}))
        self.doc = example()
        self.doc.update(dossier_id=self.did, repo=self.repo, dossier_sha=brief.dossier_sha(self.did))
        brief._write(self.doc)

    def red(self, key: str, text: str) -> list[dict]:
        data_write(self.d, key, text)
        return actions.red_overlaps(self.d).get(key) or []

    def test_brief_strings_are_sources(self):
        items = [it for it in ledger.items(self.d) if it["ref"] == f"{self.did}.brief"]
        texts = {it["text"] for it in items}
        self.assertTrue(items)
        self.assertEqual({(it["kind"], it["label"]) for it in items}, {("lead", "Repo brief")})
        for t in (self.doc["verdict"]["lede"], self.doc["how_it_works"], self.doc["seeds"][0]["models_miss"],
                  self.doc["seeds"][1]["detail"]["today"][2], self.doc["places"][0]["questions"][0],
                  self.doc["places"][0]["rivals"][0]["has"], self.doc["heading"][1]["what"]):
            self.assertIn(t, texts)
        # the server's stamps, ids, sources and provenance are not prose
        for t in (self.did, self.doc["dossier_sha"], "o3", "release notes v0.9.0", "Number allocation",
                  self.doc["places"][0]["rivals"][0]["source"], "Create", "dive"):
            self.assertNotIn(t, texts)

    def test_goal_copying_a_seed_sentence_is_red(self):
        red = self.red("goal", "Scans misfire because a new tape can then get that number, and scanning the old "
                               "label opens the wrong record, which is the bug.")
        self.assertEqual(len(red), 1)
        self.assertEqual(red[0]["source"]["label"], "Repo brief")
        self.assertIn("a new tape can then get that number", red[0]["text"])

    def test_opening_prompt_copying_a_place_question_is_red(self):
        red = self.red("opening_prompt", "hey, which printed labels stay on shelves after a tape retires, and who notices it?")
        self.assertEqual(len(red), 1, "the brief is read before the prompt, so it counts against the prompt too")

    def test_own_words_pass(self):
        self.assertEqual(self.red("goal", "Numbers from retired tapes should stay out of circulation for good."), [])

    def test_an_earlier_brief_version_still_counts(self):
        old = self.doc["seeds"][1]["problem"]
        new = example()
        new.update(dossier_id=self.did, repo=self.repo, dossier_sha=brief.dossier_sha(self.did))
        new["seeds"][1]["problem"] = "A retired tape gives up its number. The next new tape can take it."
        time.sleep(0.01)
        brief._write(new)
        self.assertEqual(len(brief.history(self.did)), 1)
        self.assertIn(old, {it["text"] for it in ledger.items(self.d)})
        red = self.red("goal", "So: a new tape can then get that number, and scanning opens the wrong record.")
        self.assertEqual(len(red), 1)

    def test_cache_follows_the_brief_file(self):
        first = ledger.items(self.d)
        self.assertIs(ledger.items(self.d), first, "cached while nothing changed")
        doc = json.loads(brief.path(self.did).read_text())
        doc["list_line"] = "A brand new line about why this repo is promising for hard tasks today."
        brief.path(self.did).write_text(json.dumps(doc))
        st = brief.path(self.did).stat()
        os.utime(brief.path(self.did), (st.st_atime, st.st_mtime + 5))
        self.assertIn(doc["list_line"], {it["text"] for it in ledger.items(self.d)})

    def test_plain_rows_are_sources_for_fields_written_after_the_run(self):
        row = "Claude left every retired barcode number in the pool after the catalogue closed the failed reprint."
        (self.d / "plain.json").write_text(json.dumps({"schema": plain.SCHEMA, "rows": {
            "R1": {"plain": row, "sha": plain.sha("finding"), "at": "2026-11-01T03:00:00-0700"}}}))
        items = [it for it in ledger.items(self.d) if it["ref"] == "plain.json#R1"]
        self.assertEqual([(it["kind"], it["label"], it["text"]) for it in items],
                         [("grading", "Finding in plain words", row)])
        red = self.red("decision_points", "1. Claude left every retired barcode number in the pool after the reprint.")
        self.assertEqual(len(red), 1)
        self.assertEqual(red[0]["source"]["label"], "Finding in plain words")
        # the prompt was written before any review existed, so plain rows never count against it
        overlap._INDEX.clear()
        self.assertEqual(self.red("opening_prompt", "claude left every retired barcode number in the pool after reprint"),
                         [])


if __name__ == "__main__":
    unittest.main()
