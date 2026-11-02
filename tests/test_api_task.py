"""The task workspace API (johto/web/api_task.py) over stub where / grading / kit / draftcheck modules.

The real modules are built in parallel, so these tests put stand-ins in sys.modules: that pins the
contract the routes call, and lets the "not available yet" answer be tested by removing a module.
"""
import json
import sys
import types
import unittest
from unittest import mock

from test_realstate_app import SAME, call
from test_realstate_derive import RealRoot
from johto.web import api_task, jobs

SHORT = "aprompt1"
_NOT_GIVEN = object()


def _row(rid, **kw):
    r = {"id": rid, "finding": f"finding {rid}", "kind": "defect",
         "sources": [{"type": "review", "ref": "reviews/validate-1-t01-gptpro.md", "finding_id": "F1", "at": "x",
                      "excerpt": "a sentence the page must not get"}],
         "suggested_scope": "explicit", "scope": None,
         "result": {"run": "validate-1", "turn": 2, "value": "missed", "why": "dropped"},
         "basis": "isolated", "refs": ["a.py:3"], "limits": "clock substituted", "review_status": "new",
         "severity": "high", "marks": {"selected": False, "goal": False, "dp": None, "notes": False, "wording": "x"},
         "marked_at": None, "verify": {"status": "none", "at": None, "path": "", "summary": "", "fix": "y"}}
    r.update(kw)
    return r


class Stubs:
    """Stand-ins for the four modules, recording how the routes call them."""

    def __init__(self):
        self.calls = []
        self.rows = {"R1": _row("R1", suggestion="Write that it keeps continuations."), "R2": _row("R2")}
        self.latest = None

        where = types.ModuleType("johto.where")
        where.card = lambda d, touch=True, source="where": (self.calls.append(("card", d.name, touch, source)), {"task": d.name, "short": "x", "next": {"text": "Write the goal", "command": f"johto where {d.name}"}, "since": ["Review ready"]})[1]
        where.overview = lambda: [{"short": SHORT, "title": "t", "needs_you": "Finish", "machine": "idle", "action": "Open",
                                   "command": "johto where", "priority": 2}]

        grading = types.ModuleType("johto.grading")
        grading.load = lambda d: {"version": 1, "run": "validate-1", "updated_at": "now", "rows": list(self.rows.values()),
                                  "draft": "never shown"}

        def refresh(d, run=None, log=print):
            self.calls.append(("refresh", d.name, run))
            log("read 1 review")
            return grading.load(d)
        grading.refresh = refresh

        def mark(d, ids, *, selected=_NOT_GIVEN, goal=_NOT_GIVEN, dp=_NOT_GIVEN, notes=_NOT_GIVEN, scope=_NOT_GIVEN):
            given = {k: v for k, v in dict(selected=selected, goal=goal, dp=dp, notes=notes, scope=scope).items()
                     if v is not _NOT_GIVEN}
            self.calls.append(("mark", tuple(ids), given))
            out = []
            for i in ids:
                if i not in self.rows:   # as grading.mark does (GradingError is a ValueError)
                    raise ValueError(f"No row {i} in {d.name}'s grading table.")
                r = self.rows[i]
                for k, v in given.items():
                    if k == "scope":
                        r["scope"] = v
                    else:
                        r["marks"][k] = v
                out.append(r)
            return out
        grading.mark = mark

        kit = types.ModuleType("johto.kit")
        kit.render = lambda d, run=None: (self.calls.append(("kit", d.name, run)), f"# Kit for {d.name}\n\n- row R1")[1]

        draftcheck = types.ModuleType("johto.draftcheck")

        def run(d, *, deep=False, full=False, log=print):
            self.calls.append(("check", d.name, deep, full))
            log("checked 6 fields")
            issue = {"id": "c1", "field": "goal", "quote": "it fo", "category": "incomplete", "evidence": "rule saved-cut",
                     "consequence": "The saved goal stops mid-word.", "source": "rule",
                     "replacement": "it follows the loan", "suggested_text": "x"}
            res = {"at": "now", "fields_sha": {"goal": "abc"}, "attention": [issue], "resolved": [], "decide": [],
                   "optional": [], "deep": deep, "path": "checks/x.json", "rewrite": "no", "run": "validate-1",
                   "stale": [dict(issue, id="c0")], "since": {"at": "then", "changed": ["goal"], "draft": "x"},
                   "sources": {"goal": "studio", "codebase": None, "bad": {"text": "x"}},
                   "ai": {"called": False, "error": None, "prompt": "never shown"},
                   "ai_state": {"issues": [issue]}}
            self.latest = res
            return res
        draftcheck.run = run
        draftcheck.latest = lambda d: self.latest
        self.modules = {"johto.where": where, "johto.grading": grading, "johto.kit": kit, "johto.draftcheck": draftcheck}


BANNED = ("suggest", "replace", "rewrite", "wording", "draft", "fix", "excerpt", "why")


def banned_keys(v, path=""):
    """Every dict key anywhere in v that names wording for a field."""
    out = []
    if isinstance(v, dict):
        for k, x in v.items():
            if any(b in k.lower() for b in BANNED) and k != "suggested_scope":
                out.append(f"{path}.{k}")
            out += banned_keys(x, f"{path}.{k}")
    elif isinstance(v, list):
        for i, x in enumerate(v):
            out += banned_keys(x, f"{path}[{i}]")
    return out


class TaskApi(RealRoot):
    def setUp(self):
        super().setUp()
        self.stubs = Stubs()
        self.mods = mock.patch.dict(sys.modules, self.stubs.modules)
        self.mods.start()

    def tearDown(self):
        self.mods.stop()
        super().tearDown()

    def post(self, path, body=None, **kw):
        return call("POST", path, body or {}, headers=SAME, **kw)

    # --- card and overview

    def test_card_calls_where(self):
        st, out = call("GET", f"/api/tasks/{SHORT}/card")
        self.assertEqual(st, 200)
        self.assertEqual(out["next"]["text"], "Write the goal")
        self.assertEqual(self.stubs.calls, [("card", SHORT, False, "web")])   # a refetch is not a visit
        call("GET", f"/api/tasks/{SHORT}/card", query="touch=1")
        self.assertEqual(self.stubs.calls[-1], ("card", SHORT, True, "web"))

    def test_a_saved_review_reads_digest_first(self):
        """The web twin of `johto review show`: the digest, then the full text; only saved names are read."""
        from johto import config, reviews
        d = config.TASKS / SHORT
        text = "## Digest\nThe run missed the retry path.\n\n## Findings\n| ID |\n\n## Detail\nLong part.\n"
        name = reviews.save(d, "validate-1", 1, text).name
        st, out = call("GET", f"/api/tasks/{SHORT}/reviews/{name}")
        self.assertEqual(st, 200, out)
        self.assertEqual(out["digest"], "The run missed the retry path.")
        self.assertIn("## Detail\nLong part.", out["text"])
        self.assertEqual((out["run"], out["turn"]), ("validate-1", 1))
        self.assertNotIn("path", out)
        self.assertEqual(call("GET", f"/api/tasks/{SHORT}/reviews/{name[:-3]}")[0], 200)   # the name without .md
        self.assertEqual(call("GET", f"/api/tasks/{SHORT}/reviews/nosuch.md")[0], 404)
        self.assertEqual(call("GET", f"/api/tasks/{SHORT}/reviews/..%2Ftask.json")[0], 400)
        self.assertIsNone(reviews.digest("An older reply with no sections."))

    def test_unknown_and_bad_tasks(self):
        self.assertEqual(call("GET", "/api/tasks/nosuch99/card")[0], 404)
        st, out = call("GET", "/api/tasks/no.such/card")
        self.assertIn(st, (400, 404))

    def test_missing_module_says_not_available(self):
        with mock.patch.dict(sys.modules, {"johto.where": None, "johto.draftcheck": None}):
            st, out = call("GET", f"/api/tasks/{SHORT}/card")
            self.assertEqual((st, out["error"]["code"]), (503, "not_available"))
            self.assertEqual(out["error"]["message"], "The task card isn't available yet.")
            st, out = self.post(f"/api/tasks/{SHORT}/check", {"deep": True})
            self.assertEqual((st, out["error"]["code"]), (503, "not_available"))
        # A module without the function the route needs counts as missing too.
        bare = types.ModuleType("johto.kit")
        with mock.patch.dict(sys.modules, {"johto.kit": bare}):
            st, out = call("GET", f"/api/tasks/{SHORT}/kit")
            self.assertEqual((st, out["error"]["details"]["missing"]), (503, "render"))

    def test_overview(self):
        st, out = call("GET", "/api/overview")
        self.assertEqual((st, out["rows"][0]["short"]), (200, SHORT))
        self.assertEqual(call("GET", "/api/overview", demo_mode=True)[1], {"rows": []})

    # --- grading

    def test_grading_passes_only_contract_keys(self):
        st, out = call("GET", f"/api/tasks/{SHORT}/grading")
        self.assertEqual(st, 200)
        self.assertEqual([r["id"] for r in out["rows"]], ["R1", "R2"])
        self.assertNotIn("draft", out)
        r1 = out["rows"][0]
        self.assertNotIn("suggestion", r1)
        self.assertEqual(r1["marks"], {"selected": False, "goal": False, "dp": None, "notes": False})
        self.assertEqual(r1["result"], {"run": "validate-1", "turn": 2, "value": "missed"})
        self.assertEqual(banned_keys(out), [])

    def test_mark_sends_only_the_marks_given(self):
        st, out = self.post(f"/api/tasks/{SHORT}/grading/mark", {"ids": ["R1"], "goal": True, "selected": True})
        self.assertEqual(st, 200)
        self.assertEqual(self.stubs.calls[-1], ("mark", ("R1",), {"goal": True, "selected": True}))
        self.assertTrue(out["rows"][0]["marks"]["goal"])
        # null clears a decision point or scope; an absent key leaves it alone.
        self.post(f"/api/tasks/{SHORT}/grading/mark", {"ids": ["R1", "R2", "R1"], "dp": None})
        self.assertEqual(self.stubs.calls[-1], ("mark", ("R1", "R2"), {"dp": None}))
        self.post(f"/api/tasks/{SHORT}/grading/mark", {"ids": ["R2"], "scope": "necessary", "dp": 2})
        self.assertEqual(self.stubs.calls[-1], ("mark", ("R2",), {"dp": 2, "scope": "necessary"}))
        self.assertEqual(banned_keys(out), [])

    def test_mark_refuses_bad_input(self):
        path = f"/api/tasks/{SHORT}/grading/mark"
        for body in ({"ids": ["R1"]}, {"ids": [], "goal": True}, {"ids": "R1", "goal": True}, {"ids": ["R1"], "dp": 4},
                     {"ids": ["R1"], "dp": True}, {"ids": ["R1"], "scope": "maybe"}, {"ids": ["R1"], "goal": "yes"},
                     {"ids": ["../x"], "goal": True}):
            st, out = self.post(path, body)
            self.assertEqual((st, out["error"]["code"]), (400, "invalid"), body)
        st, out = self.post(path, {"ids": ["R9"], "notes": True})
        self.assertEqual((st, out["error"]["code"]), (404, "not_found"))
        self.assertFalse(any(c[0] == "mark" and c[1] == ("R1",) for c in self.stubs.calls))

    def test_refresh(self):
        st, out = self.post(f"/api/tasks/{SHORT}/grading/refresh", {"run": "validate-1"})
        self.assertEqual(st, 200)
        self.assertEqual(self.stubs.calls[-1], ("refresh", SHORT, "validate-1"))
        self.assertEqual(out["log"], ["read 1 review"])
        self.assertEqual(len(out["rows"]), 2)
        self.assertEqual(self.post(f"/api/tasks/{SHORT}/grading/refresh", {"run": "../../x"})[0], 400)

    # --- kit

    def test_kit(self):
        st, out = call("GET", f"/api/tasks/{SHORT}/kit", query="run=validate-1")
        self.assertEqual((st, out["run"]), (200, "validate-1"))
        self.assertTrue(out["markdown"].startswith("# Kit for"))
        self.assertEqual(self.stubs.calls[-1], ("kit", SHORT, "validate-1"))
        self.assertEqual(call("GET", f"/api/tasks/{SHORT}/kit", query="run=a/b")[0], 400)

    # --- check

    def test_check_is_this_modules_and_carries_no_wording(self):
        st, out = call("GET", f"/api/tasks/{SHORT}/check")
        self.assertEqual((st, out), (200, {"check": None, "deep_blocked": None}))   # api_task's, not the lint route's
        st, out = self.post(f"/api/tasks/{SHORT}/check", {"full": True})
        self.assertEqual(st, 200)
        self.assertEqual(self.stubs.calls[-1], ("check", SHORT, False, True))
        issue = out["check"]["attention"][0]
        self.assertEqual(set(issue), {"id", "field", "quote", "category", "evidence", "consequence", "source"})
        self.assertNotIn("rewrite", out["check"])
        self.assertNotIn("ai_state", out["check"])
        self.assertEqual(out["check"]["since"], {"at": "then", "changed": ["goal"]})
        self.assertEqual(out["check"]["sources"], {"goal": "studio", "codebase": None})
        self.assertEqual(out["check"]["ai"], {"called": False, "error": None})
        self.assertEqual(set(out["check"]["stale"][0]), set(issue))
        self.assertEqual(out["check"]["run"], "validate-1")
        self.assertEqual(banned_keys(out), [])
        st, out = call("GET", f"/api/tasks/{SHORT}/check")
        self.assertEqual(out["check"]["attention"][0]["quote"], "it fo")
        self.assertEqual(banned_keys(out), [])

    def test_deep_check_is_a_job(self):
        st, out = self.post(f"/api/tasks/{SHORT}/check", {"deep": True})
        self.assertEqual(st, 202)
        job = out["job"]
        self.assertEqual((job["kind"], job["target"], job["pool"]), ("check_deep", f"t:{SHORT}", "ai"))
        done = jobs.wait(job["id"], 10)
        self.assertEqual(done["state"], "succeeded", done.get("error"))
        self.assertEqual(self.stubs.calls[-1], ("check", SHORT, True, False))
        self.assertEqual(banned_keys(done["result"]), [])
        self.assertEqual(done["result"]["check"]["deep"], True)

    def test_deep_check_waits_for_every_field(self):
        sys.modules["johto.draftcheck"].deep_blocker = lambda d: "Decision Points is empty. The AI check runs once every field is written."
        try:
            st, out = call("GET", f"/api/tasks/{SHORT}/check")
            self.assertIn("Decision Points is empty", out["deep_blocked"])
            n = len(self.stubs.calls)
            st, out = self.post(f"/api/tasks/{SHORT}/check", {"deep": True})
            self.assertEqual((st, out["error"]["code"]), (422, "missing_fields"))
            self.assertEqual(len(self.stubs.calls), n)   # no job, no AI call
        finally:
            del sys.modules["johto.draftcheck"].deep_blocker

    def test_second_deep_check_joins_the_running_one(self):
        import threading
        gate = threading.Event()
        orig = self.stubs.modules["johto.draftcheck"].run

        def slow(d, **kw):
            gate.wait(5)
            return orig(d, **kw)
        self.stubs.modules["johto.draftcheck"].run = slow
        try:
            a = self.post(f"/api/tasks/{SHORT}/check", {"deep": True})[1]["job"]
            b = self.post(f"/api/tasks/{SHORT}/check", {"deep": True})[1]["job"]
            self.assertEqual(a["id"], b["id"])
        finally:
            gate.set()
            jobs.wait(a["id"], 10)

    # --- guards

    def test_writes_need_the_page_and_are_off_in_the_demo(self):
        st, out = call("POST", f"/api/tasks/{SHORT}/grading/mark", {"ids": ["R1"], "goal": True},
                       headers={"Host": "127.0.0.1:8768", "Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        st, out = call("POST", f"/api/tasks/{SHORT}/grading/mark", {"ids": ["R1"], "goal": True}, headers=SAME,
                       demo_mode=True)
        self.assertEqual(st, 202)
        self.assertIn("toast", out)
        self.assertFalse(any(c[0] == "mark" for c in self.stubs.calls))
        self.assertEqual(call("GET", f"/api/tasks/{SHORT}/card", cookie=False)[0], 401)
        st, out = call("GET", f"/api/tasks/{SHORT}/card", demo_mode=True)
        self.assertEqual((st, out["error"]["code"]), (404, "demo"))

    def test_module_failure_is_a_plain_error(self):
        def boom(d):
            raise RuntimeError("grading.json is not JSON")
        self.stubs.modules["johto.grading"].load = boom
        st, out = call("GET", f"/api/tasks/{SHORT}/grading")
        self.assertEqual(st, 500)
        self.assertIn("grading.json is not JSON", out["error"]["message"])


class Allowlists(unittest.TestCase):
    def test_clean_grading_defaults(self):
        self.assertEqual(api_task.clean_grading(None), {"version": 1, "run": None, "updated_at": None, "rows": []})

    def test_clean_check_none(self):
        self.assertIsNone(api_task.clean_check(None))
        self.assertEqual(api_task.clean_check({"at": "x"})["attention"], [])

    def test_routes_are_registered_before_api_actions(self):
        from johto.web import app as webapp
        self.assertEqual(webapp.EXTENSIONS[0], "api_task")
        paths = {(r.path, tuple(sorted(r.methods or ()))) for r in api_task.routes}
        self.assertIn(("/api/tasks/{short}/check", ("POST",)), paths)
        json.dumps(sorted(paths))


if __name__ == "__main__":
    unittest.main()
