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
        seqs = normalize.sequences("Look at server/api.go and room.Get then fooBar 42 `x` HTTP done now")
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

    def test_a_red_prompt_blocks_the_validation_run(self):
        """The prompt reaches Studio and Claude only in his own words: a red overlap stops the lane gate first."""
        add_brief(self.d, AI)
        data_write(self.d, "opening_prompt", "Please fix it so the retry limit is never reached by mistake.")
        with self.assertRaises(ActionError) as e:
            actions.lane_gate(self.d, "validate", skip_qc=True)
        self.assertEqual(e.exception.code, "blocked_overlap")
        self.assertEqual(e.exception.status, 423)
        self.assertEqual(e.exception.details["fields"], ["opening_prompt"])
        actions.lane_gate(self.d, "calib")   # nothing to guard before a lane that sends no prompt
        o = e.exception.details["overlaps"][0]
        overlap.accept(self.d, "opening_prompt", o["id"])
        actions._guard_prompt_overlap(self.d)   # "my own words" clears it

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


REVIEW = ("The run never checked what happens when two workers pick up the same failed item at once, so the "
          "duplicate retry path stays untested.")


def write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


class NewSources(Base):
    """AI text the newer features save under the task folder: outside reviews, grading rows, the kit, checks,
    helper-chat notes, follow-up packs."""

    def red(self, key: str, text: str) -> list[dict]:
        data_write(self.d, key, text)
        return actions.red_overlaps(self.d).get(key, [])

    def test_a_saved_gpt_pro_review_is_a_source(self):
        write(self.d / "reviews" / "validate-1-t02-gptpro.md",
              "---\nrun: validate-1\nturn: 2\nsource: gptpro\n---\n\n" + REVIEW + "\n")
        red = self.red("validation_notes", "It never checked what happens when two workers pick up the same item.")
        self.assertEqual(len(red), 1)
        self.assertEqual(red[0]["source"]["kind"], "review")
        self.assertEqual(red[0]["source"]["label"], "Outside review: GPT Pro, validate-1 turn 2")
        self.assertNotIn("source: gptpro", red[0]["source"]["excerpt"])

    def test_a_review_quoting_his_words_does_not_make_them_red(self):
        prompt = "two workers can pick up the same failed item and both retry it at once"
        data_write(self.d, "opening_prompt", prompt)
        goal = "Each failed item is retried by exactly one worker even when several workers poll together."
        data_write(self.d, "goal", goal)
        write(self.d / "reviews" / "validate-1-t01-gptpro.md",
              f'You wrote that {prompt}. The goal says "{goal}" and the run met it.\n')
        self.assertEqual(actions.red_overlaps(self.d), {})

    def test_grading_rows_count_but_austins_marks_do_not(self):
        # johto/grading.py's row shape: finding, limits and verify.summary are AI text; scope and marks are his.
        limits = "Checked by running the classes in isolation without the database or the real queue."
        summary = "The reproduction confirmed that two workers both retried the same failed item."
        other = "Some other text in a bookkeeping key that the guard should never read as AI text."
        write(self.d / "grading.json", json.dumps({"version": 1, "rows": [
            {"id": "R1", "finding": REVIEW, "kind": "defect", "limits": limits, "scope": "necessary",
             "marks": {"selected": True, "goal": True, "dp": 1, "notes": False}, "refs": ["queue/queue.go:40"],
             "sources": [{"type": "review", "ref": other}],
             "verify": {"status": "confirmed", "summary": summary, "path": "checks/verify-R1.md"}}]}))
        red = self.red("goal", "So the duplicate retry path stays untested, which we want covered.")
        self.assertEqual([o["source"]["kind"] for o in red], ["grading"])
        self.assertTrue(red[0]["source"]["label"].startswith("Grading table: "))
        self.assertEqual([o["source"]["kind"] for o in self.red("goal", limits)], ["grading"])
        self.assertEqual([o["source"]["kind"] for o in self.red("goal", summary)], ["grading"])
        self.assertEqual(self.red("validation_notes", other), [])

    def test_kit_and_checks_count_for_his_later_fields_not_the_prompt(self):
        write(self.d / "kit.md", "# Kit\n\n- " + REVIEW + "\n")
        write(self.d / "checks" / "goal-coverage.md", "The goal names a threshold that the validation notes never mention.\n")
        self.assertEqual([o["source"]["kind"] for o in self.red("goal", "So the duplicate retry path stays untested.")],
                         ["kit"])
        red = self.red("validation_notes", "The goal names a threshold that the validation notes never mention.")
        self.assertEqual(red[0]["source"]["kind"], "check")
        # Built after the prompt was written, so never counted against it (and then they are his words).
        self.assertEqual(self.red("opening_prompt", "So the duplicate retry path stays untested, can you look?"), [])

    def test_helper_chat_notes_count_even_for_the_prompt(self):
        write(self.d / "notes" / "ai-facts.md", "The importer drops rows older than the last sync without any warning at all.\n")
        write(self.d / "notes" / "my-own.md", "The importer drops rows older than the last sync without any warning at all.\n")
        red = self.red("opening_prompt", "the importer drops rows older than the last sync, why?")
        self.assertEqual([(o["source"]["kind"], o["source"]["label"]) for o in red],
                         [("notes", "Helper chat notes (ai-facts.md)")])

    def test_a_new_file_shows_up_without_a_restart(self):
        data_write(self.d, "validation_notes", "It never checked what happens when two workers pick up the same item.")
        self.assertEqual(actions.red_overlaps(self.d), {})
        write(self.d / "reviews" / "validate-1-t01-gptpro.md", REVIEW + "\n")
        self.assertEqual(list(actions.red_overlaps(self.d)), ["validation_notes"])

    def test_outside_review_in_a_follow_up_pack(self):
        run = self.run_("calib")
        write(self.d / "runs" / run / "review-followup-20261031-120000.xml",
              f'<file path="1-review/04-outside-review.md">\n{REVIEW}\n</file>\n')
        red = self.red("validation_notes", "So the duplicate retry path stays untested in this run.")
        self.assertEqual(red[0]["source"]["kind"], "audit")


if __name__ == "__main__":
    unittest.main()
