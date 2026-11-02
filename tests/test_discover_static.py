"""The discover screens show no machine detail on their first layer (spec 1 and 5): no CLI hints, no internal
file names, no run ids, no exit codes. The code drill (code.js) renders the dossier's own blocks and is the one
place paths may appear, but its own strings follow the same rules."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "johto" / "web" / "static"
FILES = sorted((STATIC / "screens" / "discover").glob("*.js")) + [STATIC / "css" / "discover.css"]

# Strings a person would read: template text and quoted literals (imports and comments are skipped).
NEVER = [
    (re.compile(r"\bjohto [a-z]"), "a johto CLI hint"),
    (re.compile(r"\.json\""), "an internal file name"),
    (re.compile(r"\b(?:validate|calib|calibration|submission)-\d+\b"), "a run id"),
    (re.compile(r"\bexit (?:code|\d)"), "an exit code"),
    (re.compile(r"[—–]"), "an em or en dash"),
]


def visible_lines(p: Path):
    for i, line in enumerate(p.read_text().splitlines(), 1):
        s = line.strip()
        if s.startswith(("//", "/*", "*", "import ")):
            continue
        yield i, line


class NoMachineDetail(unittest.TestCase):
    def test_files_exist(self):
        names = {p.name for p in FILES}
        self.assertTrue({"index.js", "find.js", "brief.js", "inspector.js", "claim.js", "slotmap.js"} <= names, names)

    def test_no_cli_hints_file_names_run_ids_or_exit_codes(self):
        bad = []
        for p in FILES:
            for i, line in visible_lines(p):
                for rx, what in NEVER:
                    if rx.search(line):
                        bad.append(f"{p.name}:{i}: {what}: {line.strip()[:100]}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
