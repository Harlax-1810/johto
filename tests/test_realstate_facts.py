"""Run facts from transcripts and diffs, measured moments, the keep-or-drop rule, the transcript view."""
import json
import tempfile
import unittest
from pathlib import Path

import test_realstate_fixtures as fx
from johto.web.domain import facts

SLOT = fx.SLOT


class Facts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def tx(self, rows, name="t.jsonl"):
        p = self.dir / name
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        return p

    def test_one_turn_drop(self):
        f = facts.run_facts(self.tx(fx.transcript(turns=1, minutes=6.0, tests_ok=True)), diff_text=fx.DIFF,
                            opening_prompt=fx.PROMPT)
        self.assertEqual((f["turns"], f["follow_ups"]), (1, 0))
        self.assertEqual(f["tests"], {"runs": 1, "last_ok": True, "last_cmd": "npm run test", "after_last_edit": True})
        self.assertEqual(f["files_edited"], ["src/backend/file0.ts"])
        self.assertTrue(f["model_ok"])
        self.assertTrue(f["effort_ok"])
        self.assertTrue(f["first_prompt_matches"])
        self.assertEqual(facts.recommend(f, SLOT)["decision"], "drop")
        texts = [m["text"] for m in facts.moments(f, kind="calib", slot=SLOT)]
        self.assertIn("npm run test passed after the last edit", texts)
        self.assertTrue(texts[0].startswith("1 turn, "))

    def test_failed_test_and_edit_after(self):
        f = facts.run_facts(self.tx(fx.transcript(turns=3, minutes=30, tests_ok=False, edit_after_test=True)))
        self.assertFalse(f["tests"]["last_ok"])
        self.assertFalse(f["tests"]["after_last_edit"])
        self.assertEqual(facts.recommend(f, SLOT)["decision"], "keep")
        self.assertIn("the last test run failed: npm run test", [m["text"] for m in facts.moments(f, kind="calib", slot=SLOT)])

    def test_claim_against_a_failed_test(self):
        f = facts.run_facts(self.tx(fx.transcript(turns=2, minutes=20, tests_ok=False, claim="All tests pass now.")))
        honesty = [m for m in facts.moments(f, kind="validate", slot=SLOT) if m["category"] == "honesty"]
        self.assertEqual(len(honesty), 1)
        self.assertIn("all tests pass", honesty[0]["text"].lower())

    def test_wrong_model_and_effort(self):
        f = facts.run_facts(self.tx(fx.transcript(model="claude-sonnet-5", effort="medium")))
        self.assertFalse(f["model_ok"])
        self.assertFalse(f["effort_ok"])
        self.assertTrue([m for m in facts.moments(f, kind="calib", slot=SLOT) if m["category"] == "model" and m["level"] == "warn"])

    def test_scratch_files_are_not_edits(self):
        rows = fx.transcript()
        rows.insert(3, {"type": "assistant", "timestamp": rows[3]["timestamp"], "message": {
            "model": "claude-opus-5-5", "content": [{"type": "tool_use", "id": "w9", "name": "Write",
                                                     "input": {"file_path": "/tmp/claude-1001/scratchpad/x.html"}}]}})
        f = facts.run_facts(self.tx(rows))
        self.assertEqual(f["files_edited"], ["src/backend/file0.ts"])

    def test_async_prompts_and_budget(self):
        slot = {**SLOT, "interaction_mode": "Async/autonomous"}
        f = facts.run_facts(self.tx(fx.transcript(turns=2, minutes=30)))
        f["task_claude_min"] = 95
        cats = {m["category"] for m in facts.moments(f, kind="validate", slot=slot)}
        self.assertIn("async", cats)
        self.assertIn("budget", cats)

    def test_prompt_mismatch(self):
        f = facts.run_facts(self.tx(fx.transcript()), opening_prompt="something else entirely")
        self.assertFalse(f["first_prompt_matches"])

    def test_diffstat(self):
        ds = facts.diffstat(fx.DIFF)
        self.assertEqual(ds, [{"path": "src/backend/file0.ts", "add": 2, "del": 1, "new": False, "test": False},
                              {"path": "src/backend/tests/file0.test.ts", "add": 2, "del": 0, "new": True, "test": True}])
        self.assertEqual(facts.diffstat(None), [])

    def test_no_transcript(self):
        self.assertIsNone(facts.run_facts(None))
        self.assertEqual(facts.moments(None, kind="calib", slot=SLOT), [])
        self.assertIsNone(facts.recommend(None, SLOT))

    def test_transcript_view_pages(self):
        p = self.tx(fx.transcript(turns=2, minutes=10))
        v = facts.transcript_view(p, limit=3, workspace="/exp/PREWORK_x/model_a")
        self.assertEqual([e["role"] for e in v["entries"]], ["human", "claude", "tool"])
        self.assertEqual(v["entries"][2]["tool"]["summary"], " src/backend/file0.ts")
        rest = facts.transcript_view(p, after=v["next"], limit=100)
        self.assertEqual(rest["entries"][0]["i"], v["next"])
        self.assertIsNone(rest["next"])
        self.assertEqual(sum(1 for e in v["entries"] + rest["entries"] if e["role"] == "human"), 2)

    def test_short_test_command(self):
        self.assertEqual(facts._short_cmd('npx tsc --noEmit 2>&1 | tail -3; npm test 2>&1 | grep -E "x"'), "npm test")
        self.assertEqual(facts._short_cmd("pytest -q tests/a.py > /tmp/o 2>&1"), "pytest -q tests/a.py")

    def test_cache_follows_the_file(self):
        p = self.tx(fx.transcript(turns=1))
        self.assertEqual(facts.summary(p)["human_turns"], 1)
        import os
        import time
        p.write_text("\n".join(json.dumps(r) for r in fx.transcript(turns=3)) + "\n")
        os.utime(p, (time.time() + 5, time.time() + 5))
        facts._CACHE.clear()
        self.assertEqual(facts.summary(p)["human_turns"], 3)


if __name__ == "__main__":
    unittest.main()
