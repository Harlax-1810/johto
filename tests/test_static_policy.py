"""Source checks on the web pages and the helper skill for rules no Python test reaches.

The pages have no JS test runner, so these read the files: private fields stay out of the lane's reply area,
calibration is offered only when settings.calibrate_first is on, and the helper chat never uses up "since
your last look".
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "johto" / "web" / "static"


def read(rel: str) -> str:
    return (STATIC / rel).read_text()


def func(src: str, name: str) -> str:
    """The text of `function name(...) {...}` up to the next top-level function."""
    m = re.search(rf"^(export )?function {name}\b.*?(?=^(export )?function |\Z)", src, re.M | re.S)
    assert m, name
    return m.group(0)


# Each class names its owner (spec.md 10). A builder edits only its own classes, with Edit, never by rewriting the
# file. When a screen moves, move its check to the new file and keep the intent (spec.md 9.3).


class LanePane(unittest.TestCase):  # owner: home
    """The lane surface moved to Now's lane card (screens/now.js LaneFocus, filled by api_now.lane_body). It shows
    the opening prompt and none of the private fields."""

    def test_the_lane_shows_the_prompt_not_the_private_fields(self):
        api = (ROOT / "johto" / "web" / "api_now.py").read_text()
        body = re.search(r"^def lane_body\b.*?(?=^def )", api, re.M | re.S).group(0)
        self.assertIn('"opening_prompt"', body)
        card = func(read("screens/now.js"), "LaneFocus")
        for src in (body, card):
            for private in ("goal", "decision_points", "codebase", "validation_notes"):
                self.assertNotIn(f'"{private}"', src)
                self.assertNotIn(f"sections?.{private}", src)
                self.assertNotIn(f"sections.{private}", src)
                self.assertNotIn(f"texts.{private}", src)


class NoCalibrationInTheFrame(unittest.TestCase):  # owner: lead (app.js is frozen)
    def test_no_calibrate_entry_in_the_palette(self):
        self.assertNotIn("t-calib", read("app.js"))


class NoCalibrationByDefault(unittest.TestCase):  # owner: workspace
    """The task page (screens/workbench) goes from the prompt straight to the validation run (Austin's order)."""

    def test_the_prompt_leads_to_validation(self):
        run = read("screens/workbench/run.js")
        self.assertIn('"Start the validation run"', run)
        data = read("screens/workbench/data.js")
        data = data[data.index("export async function startValidation"):data.index("export async function takeOffQueue")]
        self.assertIn('{ short, kind: "validate" }', data)
        self.assertIn('{ surface: "prompt", kind: "skip" }', data)   # the prompt's Done skips calibration
        self.assertNotIn("the goal is next", read("screens/workbench/prompt.js"))

    def test_no_calibrate_entry_on_the_task_page(self):
        for p in sorted((STATIC / "screens" / "workbench").glob("*.js")):
            src = p.read_text()
            self.assertNotIn("Calibrate first", src, p.name)
            self.assertNotIn('kind: "calib"', src, p.name)

    def test_details_need_no_fields_before_the_run(self):
        """Only the opening prompt is needed before the run; the goal may be written while it works."""
        src = func(read("screens/workbench/run.js"), "runPrimary")
        self.assertIn('const empty = !(task.sections?.opening_prompt?.text || "").trim();', src)
        self.assertIn("disabled: empty", src)


class PipedTests(unittest.TestCase):  # owner: workspace
    def test_a_hidden_result_never_reads_as_failed(self):
        """facts.py sets last_ok to null when a pipe hides the exit code: the run's Details say unknown, not failed."""
        src = func(read("screens/workbench/run.js"), "testsLine")
        self.assertIn("last_ok === false", src)
        self.assertIn("unknown (piped)", src)
        for line in src.splitlines():
            if "failing" in line:
                self.assertIn("last_ok === false", line)
        api = (ROOT / "johto" / "web" / "api_workbench.py").read_text()   # the run card's words: a dict lookup,
        self.assertIn('{True: "tests passed", False: "tests failing"}.get(tests.get("last_ok"))', api)   # None says nothing


class PipedTestsInTheLane(unittest.TestCase):  # owner: home
    """The lane's test facts: the drawer foot (components/drawer.js footFacts) and Now's lane card (api_now.lane_body).
    The new wording omits a hidden result: `last_ok === false` (Python: `is False`) is the only path to "failing"."""

    def test_a_hidden_result_never_reads_as_failed(self):
        foot = func(read("components/drawer.js"), "footFacts")
        self.assertIn("last_ok === false", foot)
        self.assertNotIn("failed", foot)
        for line in foot.splitlines():
            if "failing" in line:
                self.assertIn("last_ok === false", line)
        api = (ROOT / "johto" / "web" / "api_now.py").read_text()
        body = re.search(r"^def lane_body\b.*?(?=^def )", api, re.M | re.S).group(0)
        self.assertNotIn("failed", body)
        lines = body.splitlines()
        for i, line in enumerate(lines):
            if "failing" in line:
                self.assertIn('get("last_ok") is False', lines[i - 1])


class PipedTestsInWords(unittest.TestCase):  # owner: lead (lib/words.js is frozen)
    def test_only_false_reads_as_failing(self):
        src = read("lib/words.js")
        self.assertIn('if (lastOk === false) return "tests failing";', src)
        self.assertNotIn("tests failed", src)


class HelperSkill(unittest.TestCase):  # owner: lead
    @unittest.skipUnless((ROOT / ".claude" / "skills" / "johto-task" / "SKILL.md").exists(), "the helper skill isn't in this copy")
    def test_every_where_card_in_the_skill_is_no_touch(self):
        text = (ROOT / ".claude" / "skills" / "johto-task" / "SKILL.md").read_text()
        cards = re.findall(r"`johto where <t>[^`]*`", text)
        self.assertTrue(cards)
        for c in cards:
            self.assertIn("--no-touch", c)


if __name__ == "__main__":
    unittest.main()
