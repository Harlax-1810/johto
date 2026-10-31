import json
import tempfile
import unittest
from pathlib import Path

from johto.dossier import schema
from test_dossier_fixtures import DID, REPO, SHA, _opp, make_root, valid_doc

FIXTURES = Path(__file__).resolve().parent.parent / "johto" / "web" / "fixtures" / "dossiers"


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def errs(self, doc, root=True, **kw):
        return schema.validate(doc, self.root if root else None, **kw)

    def assertError(self, errors, *needles):
        joined = "\n".join(errors)
        for n in needles:
            self.assertIn(n, joined, f"expected {n!r} in:\n{joined}")

    def test_valid_document(self):
        self.assertEqual(self.errs(valid_doc(), expect={"id": DID, "repo": REPO, "commit": SHA}), [])

    def test_expect_mismatch(self):
        self.assertError(self.errs(valid_doc(), expect={"id": "other"}), "id: must be 'other'")

    def test_top_level_types(self):
        d = valid_doc()
        d["schema"] = "johto.dossier/2"
        d["commit"] = "abc"
        d["cost_usd"] = "4"
        d["verdict"]["promise"] = "great"
        del d["headline"]
        e = self.errs(d)
        self.assertError(e, "schema: must be", "commit: must be a 40-character", "cost_usd: must be a number",
                         "verdict.promise: 'great' is not one of", "headline: missing")

    def test_not_an_object(self):
        self.assertEqual(schema.validate([]), ["document: must be a JSON object"])

    def test_word_limits(self):
        d = valid_doc()
        d["headline"] = " ".join(["word"] * 17)
        d["blocks"][4]["ambition"] = " ".join(["x"] * 46)
        d["blocks"][0]["stack"] = ["a b c d e f g"]
        e = self.errs(d)
        self.assertError(e, "headline: 17 words, limit 16", "ambition: 46 words, limit 45",
                         "stack[0]: 7 words, limit 6")

    def test_em_dash_and_addressing(self):
        d = valid_doc()
        d["blocks"][4]["area"] = "queue — state"
        d["blocks"][4]["where_models_fail"] = ["Please check the lease."]
        d["blocks"][5]["why_not_done"] = "You would need a week."
        e = self.errs(d)
        self.assertError(e, "area: contains an em dash", "where_models_fail[0]: addresses a reader",
                         "why_not_done: addresses a reader")

    def test_pointer_format(self):
        d = valid_doc()
        d["blocks"][7]["pointer"] = "src/db/store.ts"
        d["blocks"][2]["steps"][0]["pointer"] = "/etc/passwd:1"
        d["blocks"][2]["steps"][1]["pointer"] = "../x.ts:1"
        e = self.errs(d)
        self.assertError(e, "is not 'path:line' or 'path:start-end'", "'/etc/passwd' must be relative",
                         "'../x.ts' must be relative")

    def test_pointer_against_files(self):
        d = valid_doc()
        d["blocks"][7]["pointer"] = "src/db/missing.ts:1-3"
        d["blocks"][4]["evidence"][0]["pointer"] = "src/jobs/queue.ts:70-81"
        d["blocks"][5]["evidence"][0]["pointer"] = "src/jobs/queue.ts:9-3"
        d["blocks"][6]["evidence"][0]["pointer"] = "src/jobs:1"
        e = self.errs(d)
        self.assertError(e, "src/db/missing.ts is not a file", "ends past the end of src/jobs/queue.ts (80 lines)",
                         "bad line range", "src/jobs is not a file")

    def test_last_line_is_in_range(self):
        d = valid_doc()
        d["blocks"][7]["pointer"] = "src/jobs/queue.ts:80"
        self.assertEqual(self.errs(d), [])

    def test_file_without_trailing_newline(self):
        (self.root / "nonl.txt").write_text("a\nb\nc")
        d = valid_doc()
        d["blocks"][7]["pointer"] = "nonl.txt:3"
        self.assertEqual(self.errs(d), [])
        d["blocks"][7]["pointer"] = "nonl.txt:4"
        self.assertError(self.errs(d), "ends past the end of nonl.txt (3 lines)")

    def test_without_root_only_shape(self):
        d = valid_doc()
        d["blocks"][7]["pointer"] = "src/nowhere.ts:9999"
        d["blocks"][1]["nodes"][0]["paths"] = ["nowhere/"]
        self.assertEqual(self.errs(d, root=False), [])
        self.assertError(self.errs(d), "src/nowhere.ts is not a file", "nowhere/ does not exist")

    def test_paths_and_globs(self):
        d = valid_doc()
        d["blocks"][1]["nodes"][2]["paths"] = ["src/db/*.go"]
        d["blocks"][3]["items"][0]["paths"] = ["src/jobs/queue.ts:1-2", "lib/"]
        e = self.errs(d)
        self.assertError(e, "'src/db/*.go' matches nothing", "lib/ does not exist")
        self.assertEqual(sum("queue.ts:1-2" in x for x in e), 0)

    def test_symlink_out_of_root_is_not_a_file(self):
        outside = Path(self._tmp.name).parent / f"outside-{Path(self._tmp.name).name}.txt"
        outside.write_text("x\n" * 10)
        try:
            (self.root / "link.txt").symlink_to(outside)
            d = valid_doc()
            d["blocks"][7]["pointer"] = "link.txt:1"
            self.assertError(self.errs(d), "link.txt is not a file")
        finally:
            outside.unlink()

    def test_opportunity_count(self):
        d = valid_doc()
        d["blocks"] = [b for b in d["blocks"] if b.get("id") != "o3"]
        self.assertError(self.errs(d), "has 2 opportunity blocks, needs 3 to 7")
        d = valid_doc()
        d["blocks"] += [_opp(i) for i in range(4, 9)]
        self.assertError(self.errs(d), "has 8 opportunity blocks, needs 3 to 7")

    def test_duplicate_opportunity_ids(self):
        d = valid_doc()
        d["blocks"][5]["id"] = "o1"
        self.assertError(self.errs(d), "duplicate opportunity id 'o1'")

    def test_block_limit(self):
        d = valid_doc()
        d["blocks"] += [{"type": "callout", "tone": "info", "text": "x"} for _ in range(30)]
        self.assertError(self.errs(d), "blocks: has 42 items, limit 30")

    def test_unknown_block_type_and_title(self):
        d = valid_doc()
        d["blocks"].append({"type": "chart"})
        d["blocks"][0]["title"] = " ".join(["t"] * 11)
        self.assertError(self.errs(d), "'chart' is not one of", "title: 11 words, limit 10")

    def test_architecture_edges_and_limits(self):
        d = valid_doc()
        arch = d["blocks"][1]
        arch["edges"].append({"from": "api", "to": "ghost"})
        arch["nodes"].append(dict(arch["nodes"][0]))
        arch["nodes"][1]["kind"] = "lambda"
        e = self.errs(d)
        self.assertError(e, "edges[2].to: 'ghost' is not a node id", "duplicate node id 'api'",
                         "kind: 'lambda' is not one of")
        arch["nodes"] = [{"id": f"n{i}", "label": "x", "kind": "module", "paths": []} for i in range(15)]
        arch["edges"] = []
        self.assertError(self.errs(d), "nodes: has 15 items, limit 14")

    def test_flow_and_questions_limits(self):
        d = valid_doc()
        d["blocks"][2]["steps"] = [{"label": "s", "pointer": "src/server.ts:1"}] * 11
        d["blocks"][10]["items"] = ["q"] * 7
        self.assertError(self.errs(d), "steps: has 11 items, limit 10", "items: has 7 items, limit 6")

    def test_html_rules(self):
        d = valid_doc()
        d["blocks"][11]["html"] = ("<svg xmlns='http://www.w3.org/2000/svg' onload='x()'><script>1</script>"
                                   "<a href='javascript:void(0)'></a><iframe></iframe></svg>")
        e = self.errs(d)
        self.assertError(e, "contains <script", "on*= event handler", "http(s):// URL", "javascript: URL",
                         "embedding or meta tag")
        d["blocks"][11]["html"] = "<div>" + "x" * (12 * 1024) + "</div>"
        self.assertError(self.errs(d), "bytes, limit 12288")

    def test_html_allows_plain_svg_attributes(self):
        d = valid_doc()
        d["blocks"][11]["html"] = ("<style>.a{opacity:.5}</style><svg viewBox='0 0 9 9'>"
                                   "<g opacity='0.4' font-family='x'><text>on disk</text></g></svg>")
        self.assertEqual(self.errs(d), [])

    def test_opportunity_fields(self):
        d = valid_doc()
        o = d["blocks"][4]
        o["bounded_task"]["size"] = "XL"
        o["one_shot"]["likelihood"] = "maybe"
        o["slot_fit"] = [{"action": "Fix", "domain": "Backend / API", "codebase_type": "Personal"}]
        o["upstream"]["related"][0]["url"] = "http://example.com"
        o["upstream"]["related"][0]["state"] = "draft"
        o["upstream"]["looks_solved"] = "no"
        o["self_contained"]["needs"] = ["a b c d e f g"]
        o["evidence"] = []
        o["where_models_fail"] = []
        e = self.errs(d)
        self.assertError(e, "size: 'XL'", "likelihood: 'maybe'", "action: 'Fix'", "codebase_type: 'Personal'",
                         "must be an https://github.com/ link", "state: 'draft'", "looks_solved: must be true",
                         "needs[0]: 7 words, limit 6", "evidence: needs at least 1 item",
                         "where_models_fail: needs at least 1 item")

    def test_bounded_task_absent_needs_no_shape(self):
        d = valid_doc()
        d["blocks"][4]["bounded_task"] = {"exists": False}
        self.assertEqual(self.errs(d), [])
        d["blocks"][4]["bounded_task"] = {"exists": True}
        self.assertError(self.errs(d), "bounded_task.shape: missing", "bounded_task.size: None",
                         "bounded_task.check_in_session: missing")

    def test_overview_shapes(self):
        d = valid_doc()
        ov = d["blocks"][0]
        ov["size"]["source_files"] = -1
        ov["run"]["setup"] = ["", 3]
        ov["stack"] = ["x"] * 9
        e = self.errs(d)
        self.assertError(e, "source_files: must be a whole number", "setup[0]: must be a command string",
                         "setup[1]: must be a command string", "stack: has 9 items, limit 8")

    def test_html_rules_close_url_and_handler_gaps(self):
        d = valid_doc()
        for html, msg in (("<svg><image href='//evil/x.png'/></svg>", "protocol-relative URL"),
                          ("<div style='background:url(//evil/x)'></div>", "protocol-relative URL"),
                          ("<svg/onload=x()></svg>", "on*= event handler"),
                          ("<svg><rect x='1'onclick='x()'/></svg>", "on*= event handler"),
                          ("<svg><image xlink:href='pic.png'/></svg>", "href that is not a #fragment"),
                          ("<svg><use href=evil.svg#a /></svg>", "href that is not a #fragment"),
                          ("<img src='x.png'>", "src that is not a data: URL"),
                          ("<style>.a{background:url(x.png)}</style>", "url() that is not")):
            with self.subTest(html=html):
                d["blocks"][11]["html"] = html
                self.assertError(self.errs(d), msg)
        d["blocks"][11]["html"] = ("<svg viewBox='0 0 9 9'><defs><marker id='m'/></defs>"
                                   "<path marker-end='url(#m)'/><use href='#m'/>"
                                   "<image href='#m'/><img src='data:image/png;base64,AA'></svg>")
        self.assertEqual(self.errs(d), [])

    def test_web_fixtures_have_a_valid_shape(self):
        for p in sorted(FIXTURES.glob("*.json")):
            with self.subTest(fixture=p.name):
                self.assertEqual(schema.validate(json.loads(p.read_text())), [])


class PolicyTest(unittest.TestCase):
    """Text checks that keep research text from taking the shape of Austin's own fields."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def errs(self, d, **kw):
        return schema.validate(d, self.root, **kw)

    def test_valid_doc_passes_policy(self):
        self.assertEqual(self.errs(valid_doc()), [])

    def test_where_models_fail_is_a_hazard_in_a_named_file(self):
        d = valid_doc()
        d["blocks"][4]["where_models_fail"] = [
            "Ordering by id in src/db/store.ts still picks the wrong row.",
            "src/jobs/queue.ts retries rather than releasing the lease.",
            "The lease outlives a worker crash.",
            "Nothing in test/store.test.ts kills a worker mid-job.",
        ]
        e = "\n".join(self.errs(d))
        self.assertIn("where_models_fail[0]: starts with 'Ordering'", e)
        self.assertIn("where_models_fail[1]: uses 'rather than'", e)
        self.assertIn("where_models_fail[2]: names no file", e)
        self.assertNotIn("where_models_fail[3]", e)

    def test_ambition_and_shape_are_not_instructions_or_goals(self):
        d = valid_doc()
        d["blocks"][4]["ambition"] = "Fix the queue so job state is consistent."
        d["blocks"][4]["bounded_task"]["shape"] = "The lease must be released instead of expiring."
        d["blocks"][5]["ambition"] = "Uses better-sqlite3 for the store; a better design is missing."
        e = "\n".join(self.errs(d))
        self.assertIn("(opportunity o1).ambition: starts with the instruction 'Fix'", e)
        self.assertIn("shape: uses 'instead of'", e)
        self.assertIn("shape: uses 'must'", e)
        self.assertIn("(opportunity o2).ambition: uses 'better'", e)  # the word, not better-sqlite3

    def test_questions_end_in_a_question_mark(self):
        d = valid_doc()
        d["blocks"][10]["items"] = ["Reproduce the skip before claiming o1.", "Does the lease survive a restart?"]
        e = self.errs(d)
        self.assertEqual(e, ["blocks[10] (questions).items[0]: must be a question ending in '?'"])

    def test_policy_off_without_root_or_when_asked(self):
        d = valid_doc()
        d["blocks"][10]["items"] = ["Reproduce the skip."]
        self.assertEqual(schema.validate(d), [])
        self.assertEqual(self.errs(d, policy=False), [])
        self.assertTrue(schema.validate(d, policy=True))

    def test_context_checks_slot_labels(self):
        d = valid_doc()
        d["blocks"][4]["slot_fit"] = [{"action": "Understand / Assess", "domain": "Backend / API",
                                       "codebase_type": "Enterprise-grade"}]
        e = "\n".join(self.errs(d, context={"source_files": 150, "codebase_types": ["Personal-scale"]}))
        self.assertIn("slot_fit[0].action: Understand / Assess needs 200+ source files; this repo has 150", e)
        self.assertIn("slot_fit[0].codebase_type: 'Enterprise-grade' failed the Studio codebase check", e)
        self.assertIn("verdict.best_fits[0].codebase_type", e)
        self.assertEqual(self.errs(d, context={"source_files": 450, "codebase_types": ["Enterprise-grade"]}), [])
        self.assertEqual(self.errs(d, context={}), [])


if __name__ == "__main__":
    unittest.main()
