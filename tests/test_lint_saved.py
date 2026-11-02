"""lint.check_saved_fields: Studio's saved text cut off mid-word, or shorter than the local copy.

The snapshots in tests/fixtures/saved_fields/ are invented Studio fields. Only one field among them is cut off:
t4sk0003's Validation Notes ("...it fo").

Run: .venv/bin/python -m unittest tests/test_lint_saved.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from johto import lint, workspace  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "saved_fields"
HEADING = {f: h for h, f in workspace.SECTIONS.items()}
NO_WORDS = frozenset()  # no system word list: the checks below must hold on a machine without one


def snapshots() -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text()) for p in sorted(FIXTURES.glob("*.json"))}


class TaskDir:
    """A temp task folder with app/studio-fields.json, and optionally submission.md and authorship.jsonl."""

    def __init__(self, studio: dict | None, local: dict | None = None, authorship: list[dict] | None = None):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        (self.d / "app").mkdir()
        if studio is not None:
            (self.d / "app" / "studio-fields.json").write_text(json.dumps({"at": "2026-03-10T09:00:37-0700",
                                                                           "fields": studio}))
        if local is not None:
            body = "<!--\nWrite every section yourself.\n-->\n\n"
            for f, heading in HEADING.items():
                body += f"## {heading}\n<!-- hint for {heading} -->\n{local.get(f, '')}\n\n"
                if heading == "Difficulty & Codebase Understanding":
                    body += "## Validation\nOne-shot: No\nTask Success:\nSteering:\nClaude minutes: auto\n\n"
            (self.d / "submission.md").write_text(body)
        if authorship:
            (self.d / "app" / "authorship.jsonl").write_text("".join(json.dumps(e) + "\n" for e in authorship))

    def __enter__(self) -> Path:
        return self.d

    def __exit__(self, *exc):
        self.tmp.cleanup()


class RealSnapshots(unittest.TestCase):
    def test_only_the_known_cut_is_flagged(self):
        snaps = snapshots()
        self.assertEqual(sorted(snaps), ["d3m0aaa9", "t4sk0001", "t4sk0003"])
        for short, snap in snaps.items():
            for local in (None, {}, snap["fields"]):  # no submission.md, an empty one, one equal to Studio
                with self.subTest(short, local=bool(local)), TaskDir(snap["fields"], local) as d:
                    got = lint.check_saved_fields(d)
                    if short != "t4sk0003":
                        self.assertEqual(got, [])
                        continue
                    self.assertEqual(len(got), 1, got)
                    level, field, msg = got[0]
                    self.assertEqual((level, field), (lint.WARN, "Validation Notes"))
                    self.assertIn('"...I ship the second half it fo"', msg)

    def test_same_result_without_a_word_list(self):
        for short, snap in snapshots().items():
            with self.subTest(short), mock.patch.object(lint, "_dictionary", return_value=NO_WORDS), \
                    TaskDir(snap["fields"]) as d:
                self.assertEqual(len(lint.check_saved_fields(d)), short == "t4sk0003")

    @unittest.skipUnless(lint.DICT_PATH.exists(), "no system word list (without one, 'real' before 'realistic' "
                                                  "and 'entry' before 'entrypoint' read as cut)")
    def test_every_whole_word_ending_in_real_text_passes(self):
        """Cut each real field after every whole word (no period): none of those endings may be flagged."""
        import re
        for short, snap in snapshots().items():
            texts = {f: lint._norm(v) for f, v in snap["fields"].items()}
            for f, s in texts.items():
                if short == "t4sk0003" and f == "field_self_check":
                    s = s[:-len(" fo")]
                others = [v for k, v in texts.items() if k != f]
                for m in re.finditer(r"[A-Za-z']+(?=[\s,;:!?)]|$)", s):
                    with self.subTest(short, end=s[max(0, m.end() - 30):m.end()]):
                        self.assertIsNone(lint.cut_off_word(s[:m.end()], others))


class CutOffWord(unittest.TestCase):
    def test_flags(self):
        cases = [
            ("I wanted to know what happens when I ship the second half it fo", [], "fo"),
            ("The next batch loses the permissi", ["Restarting fixes the permission cache."], "permissi"),
            ("It asked Cla", ["Claude ran for 40 minutes."], "Cla"),
            ("and then it stopped with the q", [], "q"),
        ]
        for text, others, want in cases:
            with self.subTest(text):
                self.assertEqual(lint.cut_off_word(text, others, NO_WORDS), want)

    def test_whole_endings_pass(self):
        cases = [
            ("Correct:\n\n* It imports rows\n* It follows the rate limit", []),  # bullets, no period
            ("A weak answer would only restart the job", []),
            ("Worker A takes the earlier batch and worker B", []),
            ("It was halfway through the change when it ran out of time. .", []),
            ("There are related open issues #4102 and #4310", []),
            ("Context matters, e.g", []),
            ("Matching ignores case for renamed labels", ["labels"]),  # used elsewhere
            ("the rows were fixed", ["fix the bug"]),                # inflection of a word used elsewhere
            ("the queue we split", ["splitting the queue"]),          # doubled consonant + ing
            ("A strong answer works with", ["without a lock"]),       # compound
            ("", []),
            ("Ends with code `foo`", []),
        ]
        for text, others in cases:
            with self.subTest(text):
                self.assertIsNone(lint.cut_off_word(text, others, NO_WORDS))

    def test_word_list_clears_long_words_only(self):
        others = ["This is not realistic."]
        self.assertEqual(lint.cut_off_word("The data is real", others, NO_WORDS), "real")
        self.assertIsNone(lint.cut_off_word("The data is real", others, frozenset({"real"})))
        # Short fragments stay flagged even when the word list has them ("fo" and "tha" are in web2).
        self.assertEqual(lint.cut_off_word("bug it fo", [], frozenset({"fo"})), "fo")


class AgainstLocalCopy(unittest.TestCase):
    NOTES = ("It got several things right but missed the stale cache issue. "
             "It is unclear what stays open when I ship the second half it found and stop there.")

    def check(self, studio: dict, local: dict, authorship=None) -> list[tuple]:
        with TaskDir(studio, local, authorship) as d:
            return lint.check_saved_fields(d)

    def test_studio_stops_mid_word(self):
        cut = self.NOTES[:self.NOTES.index("found") + 2]  # "...it fo"
        got = self.check({"field_self_check": cut}, {"field_self_check": self.NOTES})
        self.assertEqual(len(got), 1, got)  # one error, not also the mid-word warning
        self.assertEqual(got[0][:2], (lint.ERROR, "Validation Notes"))
        self.assertIn("stops mid-word", got[0][2])

    def test_studio_stops_early_on_a_whole_word(self):
        cut = self.NOTES[:self.NOTES.index(" It is")]
        got = self.check({"field_self_check": cut}, {"field_self_check": self.NOTES})
        self.assertEqual([g[:2] for g in got], [(lint.WARN, "Validation Notes")])
        self.assertIn("stops early", got[0][2])
        self.assertIn("more words", got[0][2])

    def test_small_difference_is_fine(self):
        text = self.NOTES + " That's it."
        self.assertEqual(self.check({"field_self_check": self.NOTES}, {"field_self_check": text}), [])

    def test_studio_shorter_and_different(self):
        studio = "It missed the stale cache issue."
        got = self.check({"field_self_check": studio}, {"field_self_check": self.NOTES})
        self.assertEqual(len(got), 1)
        self.assertIn("Studio has less than your local copy (6 vs", got[0][2])
        self.assertIn("Save the field to Studio", got[0][2])

    def test_studio_changed_after_the_last_pull(self):
        studio = "It missed the stale cache issue."
        pulls = [{"at": "2026-10-31T00:10:48-07:00", "field": "Validation Notes", "event": "pulled_from_studio",
                  "chars": len(self.NOTES)},
                 {"at": "2026-10-31T00:11:00-07:00", "field": "Opening Prompt", "event": "pulled_from_studio",
                  "chars": 12}]
        got = self.check({"field_self_check": studio}, {"field_self_check": self.NOTES}, pulls)
        self.assertEqual(len(got), 1)
        self.assertIn("changed in Studio after johto last pulled it", got[0][2])
        self.assertIn("johto pull", got[0][2])

    def test_nothing_in_studio_yet(self):
        got = self.check({}, {"field_high_level_goal": self.NOTES})
        self.assertEqual(got[0][:2], (lint.WARN, "Goal"))
        self.assertIn("Studio has none of your local copy", got[0][2])

    def test_prompt_and_initial_state_are_compared(self):
        prompt = "auth tests started failing on CI this morning but pass locally. mind taking a look?"
        got = self.check({"field_opening_prompt": "auth tests started failing"}, {"field_opening_prompt": prompt})
        self.assertEqual([g[1] for g in got], ["Opening Prompt"])

    def test_template_placeholders_are_not_a_local_copy(self):
        studio = {"field_high_level_goal": self.NOTES, "field_self_check": self.NOTES}
        self.assertEqual(self.check(studio, {}), [])

    def test_studio_longer_than_local_is_fine(self):
        self.assertEqual(self.check({"field_self_check": self.NOTES}, {"field_self_check": self.NOTES[:40]}), [])

    def test_no_snapshot(self):
        with TaskDir(None, {"field_self_check": self.NOTES}) as d:
            self.assertEqual(lint.check_saved_fields(d), [])
        with TaskDir(None) as d:
            (d / "app" / "studio-fields.json").write_text("{not json")
            self.assertEqual(lint.check_saved_fields(d), [])

    def test_non_text_fields_are_ignored(self):
        studio = {"field_self_check": self.NOTES, "field_task_success": "2 - Partial, major issues",
                  "field_trajectory_log_files": [{"file_s3_url": "s3://x"}], "field_claude_minutes": 50}
        self.assertEqual(self.check(studio, {"field_self_check": self.NOTES}), [])

    def test_messages_are_plain(self):
        cut = self.NOTES[:self.NOTES.index("found") + 2]
        msgs = [m for _, _, m in self.check({"field_self_check": cut, "field_rationale_difficulty": "x y"},
                                            {"field_self_check": self.NOTES, "field_high_level_goal": self.NOTES,
                                             "field_rationale_difficulty": self.NOTES})]
        self.assertGreaterEqual(len(msgs), 3)
        for m in msgs:
            self.assertNotIn("—", m)
            self.assertNotIn("field_", m)


if __name__ == "__main__":
    unittest.main()
