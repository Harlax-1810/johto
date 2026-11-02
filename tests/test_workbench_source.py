"""Source checks on the task page (screens/workbench and the editor): the first layer is words (rule 5).

The pages have no JS test runner, so these read the files. Nothing Austin reads may carry a command line, an
internal file name, a run id or an exit code; those live only in the server's drill-in data, never in the page's
own text. Every write is his press: no Studio route is called from an effect or a timer.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "johto" / "web" / "static"
FILES = sorted((STATIC / "screens" / "workbench").glob("*.js")) + [STATIC / "components" / "field.js"]


def code(p: Path) -> str:
    """The file without comments (block and line comments; a // inside a string or regex is kept)."""
    s = re.sub(r"/\*.*?\*/", "", p.read_text(), flags=re.S)
    return "\n".join(re.sub(r"(^|\s)//.*$", r"\1", line) for line in s.splitlines())


class FirstLayerWords(unittest.TestCase):
    def test_the_files_exist(self):
        names = {p.name for p in FILES}
        for n in ("index.js", "prompt.js", "run.js", "write.js", "findings.js", "submit.js", "after.js", "data.js"):
            self.assertIn(n, names)

    def test_no_command_lines(self):
        for p in FILES:
            self.assertNotRegex(code(p), r"\bjohto [a-z]", p.name)

    def test_no_internal_file_names(self):
        for p in FILES:
            self.assertNotRegex(code(p), r"\.jsonl?[\"'`]|\.md[\"'`]|submission\.md|task\.json", p.name)

    def test_no_run_ids(self):
        for p in FILES:
            self.assertNotRegex(code(p), r"\b(?:calib|validate)-\d", p.name)

    def test_no_exit_codes(self):
        for p in FILES:
            self.assertNotRegex(code(p), r"\bexit(?: code)? \d|\bexit code\b", p.name)

    def test_no_em_dashes_in_the_page_text(self):
        for p in FILES:
            self.assertNotIn("—", code(p), p.name)


class HisPressOnly(unittest.TestCase):
    STUDIO = re.compile(r"studio/(?:save|submit|unclaim|qc)")

    def test_studio_writes_sit_behind_a_press(self):
        """Studio writes live in named functions the buttons call, never in a useEffect or a timer."""
        for p in FILES:
            src = code(p)
            for m in re.finditer(r"useEffect\(\(\) => \{(.*?)\n  \}, \[", src, re.S):
                self.assertNotRegex(m.group(1), self.STUDIO, p.name)
            for m in re.finditer(r"setTimeout\((.*?)\)", src):
                self.assertNotRegex(m.group(1), self.STUDIO, p.name)

    def test_submit_and_unclaim_ask_twice(self):
        submit = code(STATIC / "screens" / "workbench" / "submit.js")
        self.assertIn('useConfirm("POST", `/api/tasks/${short}/studio/submit`)', submit)
        self.assertIn("Press again to submit. This can't be undone.", submit)
        index = code(STATIC / "screens" / "workbench" / "index.js")
        self.assertIn('api.confirmable("POST", path', index)
        self.assertIn("/studio/unclaim", index)

    def test_cmd_s_never_writes_to_studio(self):
        """⌘S keeps his text here; Save to Studio is only ever its own button (or the palette)."""
        index = code(STATIC / "screens" / "workbench" / "index.js")
        i = index.index('e.key.toLowerCase() !== "s"')
        handler = index[i:index.index("window.addEventListener", i)]
        self.assertIn("flushFields()", handler)
        self.assertNotIn("save(", handler.replace("flushFields()", ""))
        self.assertNotRegex(handler, self.STUDIO)
        submit = code(STATIC / "screens" / "workbench" / "submit.js")
        self.assertNotIn("⌘S", submit)   # no key hint that promises a Studio save

    def test_red_prompt_blocks_the_run_and_its_check(self):
        data = code(STATIC / "screens" / "workbench" / "data.js")
        check = data[data.index("export async function checkPrompt"):]
        self.assertLess(check.index("if (promptRed("), check.index("api."))
        self.assertIn("blocked_overlap", data[data.index("export async function startValidation"):])

    def test_red_overlap_blocks_save_here_too(self):
        data = code(STATIC / "screens" / "workbench" / "data.js")
        fn = data[data.index("export async function saveToStudio"):data.index("export async function rerunQc")]
        self.assertIn('o.level === "red" && !o.accepted', fn)
        self.assertLess(fn.index("if (red.length)"), fn.index("studio/save"))


class NoAiWordingInHisFields(unittest.TestCase):
    def test_findings_never_write_into_a_field(self):
        """The findings panel places findings (grading marks); it never sends text to a section."""
        for name in ("findings.js", "write.js", "submit.js", "run.js", "prompt.js", "after.js"):
            src = code(STATIC / "screens" / "workbench" / name)
            self.assertNotRegex(src, r"/sections/\$\{", name)
        field = code(STATIC / "components" / "field.js")
        self.assertIn("authorship: { ...typed.current }", field)   # only his typing is sent, with its authorship

    def test_ai_text_wears_the_mark(self):
        findings = code(STATIC / "screens" / "workbench" / "findings.js")
        # The panel wears the AI mark whatever its title ("Findings" until a review is saved).
        self.assertIn('<${AiTag} label=${title} />', findings)
        self.assertIn('"Findings from the review" : "Findings"', findings)
        self.assertIn('class="wb-fp ai-block"', findings)
        run = code(STATIC / "screens" / "workbench" / "run.js")
        self.assertIn("Claude's last message", run)
        prompt = code(STATIC / "screens" / "workbench" / "prompt.js")
        self.assertIn("AiBlock", prompt)
        self.assertIn('label="Claude asked"', run)

    def test_overlap_flags_name_the_source_in_words(self):
        """A red flag says where the words came from in plain words, never an internal kind."""
        field = code(STATIC / "components" / "field.js")
        fn = field[field.index("export function sourceWords"):field.index("export function groupOverlaps")]
        for kind, words in (("grading", "a finding"), ("kit", "a finding"), ("lead", "a line from Claude's research"),
                            ("notes", "something Claude told you in a helper chat"),
                            ("autoqc", "Mercor's automatic check"), ("review", "the review")):
            self.assertRegex(fn, rf'k === "{kind}"\) return "{re.escape(words)}"', kind)
        self.assertIn('return "text the app showed you"', fn)


if __name__ == "__main__":
    unittest.main()
