"""The provenance guard (design 8.4): a 6-word run shared with AI text the app showed is red and blocks Save
until Austin rewords it or marks it as his own words.

Run: .venv/bin/python -m unittest discover -s tests -p "test_guard_overlap.py"
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_actions import Base  # noqa: E402
from test_sync import data_write  # noqa: E402

from johto import actions, workspace  # noqa: E402
from johto.actions import ActionError  # noqa: E402
from johto.web.guard import normalize, overlap  # noqa: E402

AI = ("The attempt counter is reset every time the item goes back on the queue, so the "
      "retry limit is never reached.")


def add_brief(d: Path, text: str) -> None:
    with open(d / "brief.md", "a") as fh:
        fh.write("\n## Dossier (AI research)\n\n- " + text + "\n")


class Normalize(unittest.TestCase):
    def test_code_like_tokens_end_a_run(self):
        seqs = normalize.sequences("Look at server/api.go and group.Get then fooBar 42 `x` HTTP done now")
        words = [[w for w, _, _ in s] for s in seqs]
        self.assertEqual(words, [["look", "at"], ["and"], ["then"], ["done", "now"]])

    def test_curly_quotes_and_case(self):
        self.assertEqual([w for w, _, _ in normalize.sequences("“Don’t Panic,” he said.")[0]],
                         ["don't", "panic", "he", "said"])


class Overlap(Base):
    def test_copied_research_is_red_and_blocks_save(self):
        add_brief(self.d, AI)
        data_write(self.d, "goal", "Fix it so the retry limit is never reached by mistake.")
        sub = workspace.parse_submission(self.d)
        red = actions.red_overlaps(self.d, sub)
        self.assertEqual(list(red), ["goal"])
        o = red["goal"][0]
        self.assertEqual(o["text"], "so the retry limit is never reached")
        self.assertEqual(o["source"]["kind"], "lead")
        row = next(r for r in actions.checklist(self.d) if r["key"] == "provenance")
        self.assertEqual((row["status"], row["blocking"]), ("fail", True))
        from johto.web import api_actions
        with self.assertRaises(ActionError) as e:
            api_actions._guard_overlap(self.d)
        self.assertEqual(e.exception.code, "blocked_overlap")
        # "My own words" clears it, and is logged.
        overlap.accept(self.d, "goal", o["id"])
        self.assertEqual(actions.red_overlaps(self.d), {})
        self.assertIn("accept_overlap", (self.d / "app" / "authorship.jsonl").read_text())
        api_actions._guard_overlap(self.d)
        row = next(r for r in actions.checklist(self.d) if r["key"] == "provenance")
        self.assertEqual(row["status"], "pass")

    def test_five_words_and_identifiers_pass(self):
        add_brief(self.d, AI)
        data_write(self.d, "goal", "The retry limit is never hit, see queue/queue.go and attempt counter is reset.")
        self.assertEqual(actions.red_overlaps(self.d), {})

    def test_his_own_words_coming_back_are_not_flagged(self):
        """AutoQC quotes his goal and Claude echoes his prompt: neither makes his text red."""
        prompt = "the sync job keeps retrying failed uploads forever and the queue grows"
        data_write(self.d, "opening_prompt", prompt)
        goal = "Uploads that fail with a retryable error back off and stop after the configured limit."
        data_write(self.d, "goal", goal)
        actions.update_meta(self.d, lambda m: m.setdefault("qc", []).append(
            {"audit_id": "a1", "spec": "AutoQC", "status": "completed", "dims": [
                {"dimension": "High-Level Task Goal", "status": "pass",
                 "justification": f'The goal says "{goal}" which is checkable.'}]}))
        run = self.run_("calib")
        rec = actions.run_record(self.d, run)
        with open(rec["transcript"], "a") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text":
                     "You said the sync job keeps retrying failed uploads forever and the queue grows, so I looked."}]}}) + "\n")
        self.assertEqual(actions.red_overlaps(self.d), {})

    def test_copying_claude_into_validation_notes_is_red(self):
        run = self.run_("calib")
        rec = actions.run_record(self.d, run)
        with open(rec["transcript"], "a") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text":
                     "I verified the change handles concurrent writers without losing any items."}]}}) + "\n")
        data_write(self.d, "validation_notes", "Claude says it handles concurrent writers without losing any items.")
        red = actions.red_overlaps(self.d)
        self.assertEqual(list(red), ["validation_notes"])
        self.assertEqual(red["validation_notes"][0]["source"]["kind"], "transcript_claude")

    def test_task_detail_carries_overlaps(self):
        add_brief(self.d, AI)
        data_write(self.d, "goal", "Fix it so the retry limit is never reached by mistake.")
        from johto.web import data
        t = data.real_task(self.d)
        self.assertEqual(len(t["sections"]["goal"]["overlap"]), 1)
        self.assertEqual(t["sections"]["codebase"]["overlap"], [])


if __name__ == "__main__":
    unittest.main()
