"""johto/understand.py, johto/precedent.py and the API behind the Prompt step's task understanding. Never a real
research run: Claude, the clone and the GitHub facts are stubbed."""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from johto import config, precedent, understand as U, understand_prompt as P

COMMIT = "b7d6c5e4f30201ffeeddccbbaa99887766554433"


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "data" / "dossiers").mkdir(parents=True)
        (root / "tasks").mkdir()
        for name, val in (("DATA", root / "data"), ("TASKS", root / "tasks")):
            p = mock.patch.object(config, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.root = root

    def task(self, short="abc12345", action="Operate", with_dossier=True) -> Path:
        d = self.root / "tasks" / short
        d.mkdir()
        meta = {"task_id": "task_x", "task_name": f"Task {short}",
                "slot": {"action": action, "domain": "Other software", "codebase_type": "Enterprise-grade",
                         "prompt_style": "Casual prose", "interaction_mode": "Async/autonomous"},
                "repo": {"full_name": "acme/partbin", "commit": COMMIT, "description": "A shop-floor inventory app."}}
        if with_dossier:
            meta["dossier"] = {"id": "acme__partbin__b7d6c5e4", "opportunity_id": "o5"}
            meta["origin"] = {"kind": "seed", "dossier_id": "acme__partbin__b7d6c5e4", "opportunity_id": "o5",
                              "title": "Undo a bad import"}
            dossier = {"blocks": [{"type": "overview", "title": "Partbin at a glance"},
                                  {"type": "opportunity", "id": "o5", "title": "Undo a bad stock import",
                                   "upstream": {"related": [{"url": "https://github.com/acme/partbin/issues/812"}]},
                                   "bounded_task": {"check_in_session": "Initial state: a stack with 300 parts."}}]}
            (self.root / "data" / "dossiers" / "acme__partbin__b7d6c5e4.json").write_text(json.dumps(dossier))
            brief = {"name": "Partbin", "seeds": [{"opportunity_id": "o5", "title": "Undo a bad stock import",
                                                   "problem": "A bad import wrecked 300 counts.",
                                                   "detail": {"today": ["Parts have a ledger."]}}]}
            (self.root / "data" / "dossiers" / "acme__partbin__b7d6c5e4.brief.json").write_text(json.dumps(brief))
        (d / "task.json").write_text(json.dumps(meta))
        return d


class Validate(unittest.TestCase):
    def test_the_worked_example_passes(self):
        self.assertEqual(U.validate(P.EXAMPLE, "Operate", repo="acme/partbin"), [])

    def test_instructions_reader_and_a_missing_claude_fail(self):
        doc = copy.deepcopy(P.EXAMPLE)
        doc["job"] = "Fix the counts so you can see them."
        errs = dict(U.validate(doc, "Operate"))
        self.assertIn("job", errs)
        msgs = [m for w, m in U.validate(doc, "Operate") if w == "job"]
        self.assertTrue(any("instruction" in m for m in msgs))
        self.assertTrue(any("addresses the reader" in m for m in msgs))
        self.assertTrue(any("Claude" in m for m in msgs))

    def test_versions_and_issue_numbers_only_where_allowed(self):
        doc = copy.deepcopy(P.EXAMPLE)
        doc["story"] = "Partbin v2.3.1 shifted counts, see #812, for Claude."
        where = {w for w, _ in U.validate(doc, "Operate")}
        self.assertIn("story", where)
        self.assertNotIn("real.real[0]", where)        # names v2.3.0 and is allowed to

    def test_word_limits_and_list_lengths(self):
        doc = copy.deepcopy(P.EXAMPLE)
        doc["headline"] = "Claude " + "word " * 40
        doc["hard"] = ["Only one."]
        errs = U.validate(doc, "Operate")
        self.assertTrue(any(w == "headline" and "words" in m for w, m in errs))
        self.assertTrue(any(w == "hard" and "items" in m for w, m in errs))

    def test_kind_keys_follow_the_task_type(self):
        errs = U.validate(P.EXAMPLE, "Modify")
        where = {w for w, _ in errs}
        self.assertIn("kind.symptom", where)
        self.assertIn("kind.lane_now", where)       # an Operate key on a Modify task

    def test_code_paths_must_exist_in_the_clone(self):
        with tempfile.TemporaryDirectory() as t:
            clone = Path(t)
            (clone / "a").mkdir()
            (clone / "a" / "b.ts").write_text("x")
            doc = copy.deepcopy(P.EXAMPLE)
            doc["code"] = [{"path": "a/b.ts:1-2", "what": "Exists."}, {"path": "nope/c.ts:3", "what": "Missing."},
                           {"path": "/etc/passwd", "what": "Absolute."}]
            where = {w for w, _ in U.validate(doc, "Operate", clone=clone)}
            self.assertNotIn("code[0].path", where)
            self.assertIn("code[1].path", where)
            self.assertIn("code[2].path", where)

    def test_threads_outside_the_repo_fail(self):
        doc = copy.deepcopy(P.EXAMPLE)
        doc["upstream"]["threads"][0]["url"] = "https://github.com/other/repo/issues/1"
        self.assertIn("upstream.threads[0].url", {w for w, _ in U.validate(doc, "Operate", repo="acme/partbin")})

    def test_prune_drops_bad_items_but_keeps_minimums(self):
        doc = copy.deepcopy(P.EXAMPLE)
        doc["code"].append({"path": "/abs", "what": "Bad."})
        doc["hard"] = ["Fix it now.", "Fine sentence about memory."]
        doc["extra"] = 1
        errs = U.validate(doc, "Operate")
        out = U.prune(doc, errs)
        self.assertEqual(len(out["code"]), len(P.EXAMPLE["code"]))
        self.assertEqual(len(out["hard"]), 2)          # the minimum is two, so the bad one stays (and still fails)
        self.assertNotIn("extra", out)


class FirstRelease(unittest.TestCase):
    def tags(self, out):
        return mock.patch.object(U, "_git", return_value=SimpleNamespace(returncode=0, stdout=out))

    def test_oldest_release_holding_the_commit(self):
        rels = [{"tagName": "0.20.3", "publishedAt": "2026-10-27"}, {"tagName": "0.12.1", "publishedAt": "2026-03-30"},
                {"tagName": "0.12.0", "publishedAt": "2026-03-01"}]
        with self.tags("0.12.1\n0.20.3\nnightly\n"):
            self.assertEqual(U.first_release(Path("."), "abc1234", rels), "0.12.1")

    def test_unknown_when_the_list_was_cut_before_it(self):
        rels = [{"tagName": f"1.{n}", "publishedAt": f"2026-01-{n % 28 + 1:02d}T{n:03d}"} for n in range(U.RELEASE_LIMIT)]
        oldest = min(rels, key=lambda r: r["publishedAt"])["tagName"]
        with self.tags(" ".join(r["tagName"] for r in rels)):
            self.assertIsNone(U.first_release(Path("."), "abc1234", rels))
        with self.tags("1.5"):
            self.assertEqual(U.first_release(Path("."), "abc1234", rels), "1.5")
        self.assertNotEqual(oldest, "1.5")


class Extract(unittest.TestCase):
    def test_takes_the_last_block(self):
        text = f"notes\n{P.BEGIN}\n{{\"a\": 1}}\n{P.END}\nmore\n{P.BEGIN}\n```json\n{{\"b\": 2}}\n```\n{P.END}"
        self.assertEqual(U.extract(text), ({"b": 2}, None))

    def test_errors(self):
        self.assertIsNotNone(U.extract("")[1])
        self.assertIsNotNone(U.extract("no markers")[1])
        self.assertIsNotNone(U.extract(f"{P.BEGIN}\n[1]\n{P.END}")[1])


def _pass(text, cost=0.5):
    return SimpleNamespace(text=text, cost=cost, is_error=False, model="claude-opus-test", error_text=lambda: "x")


def _wrap(doc) -> str:
    return f"{P.BEGIN}\n{json.dumps(doc)}\n{P.END}"


class Generate(Tmp):
    def facts(self, urls, full, clone, commit):
        return {"pinned": {"commit": commit, "date": "2026-10-30", "after_release": "v2.4.0"}, "releases": [],
                "threads": {"https://github.com/acme/partbin/issues/812": {"url": "https://github.com/acme/partbin/issues/812",
                                                                           "kind": "issue", "number": 812, "title": "Bad counts",
                                                                           "body": "long", "comments": []}}}

    def clone(self, full, commit, dest, deadline, cancel):
        for c in P.EXAMPLE["code"]:
            p = Path(dest) / c["path"].split(":")[0]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        return dest

    def gen(self, d, outputs):
        calls = []

        def runner(args, cwd, timeout, prog, cancel):
            calls.append(args)
            return _pass(outputs[len(calls) - 1])
        with mock.patch("johto.dossier.generate.cleanup"):
            res = U.generate(d, runner=runner, clone_fn=self.clone, facts_fn=self.facts)
        return res, calls

    def test_writes_the_document_with_facts(self):
        d = self.task()
        res, calls = self.gen(d, [_wrap(P.EXAMPLE)])
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(calls), 1)
        self.assertIn("--session-id", calls[0])
        self.assertIn("Read", ",".join(calls[0]))
        doc = U.load(d)
        self.assertEqual(doc["action"], "Operate")
        self.assertEqual(doc["doc"]["headline"], P.EXAMPLE["headline"])
        thread = doc["facts"]["threads"]["https://github.com/acme/partbin/issues/812"]
        self.assertNotIn("body", thread)                # bodies feed the prompt, not the page
        self.assertEqual(doc["inputs_sha"], U.inputs_sha(U.research(d)))
        self.assertEqual(U.state(d), "ready")

    def test_one_repair_pass_then_written(self):
        d = self.task()
        bad = copy.deepcopy(P.EXAMPLE)
        bad["job"] = "Fix the counts."
        res, calls = self.gen(d, [_wrap(bad), _wrap(P.EXAMPLE)])
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(calls), 2)
        self.assertIn("--resume", calls[1])
        self.assertIn("job", calls[1][2])                 # the repair prompt names the field

    def test_still_failing_is_a_failed_state(self):
        d = self.task()
        bad = copy.deepcopy(P.EXAMPLE)
        bad["headline"] = "Fix it."
        res, _ = self.gen(d, [_wrap(bad), _wrap(bad)])
        self.assertFalse(res["ok"])
        self.assertIsNone(U.load(d))
        self.assertEqual(U.state(d), "failed")
        self.assertFalse(U.wanted(d))                     # not retried on every page load

    def test_overwrite_keeps_history_and_stale_after_new_research(self):
        d = self.task()
        self.gen(d, [_wrap(P.EXAMPLE)])
        self.gen(d, [_wrap(P.EXAMPLE)])
        self.assertEqual(len(U.history(d)), 1)
        dossier_p = self.root / "data" / "dossiers" / "acme__partbin__b7d6c5e4.json"
        doc = json.loads(dossier_p.read_text())
        doc["blocks"][1]["title"] = "Changed research"
        dossier_p.write_text(json.dumps(doc))
        self.assertEqual(U.state(d), "stale")
        self.assertTrue(U.wanted(d))

    def test_no_pinned_commit_fails_early(self):
        d = self.task()
        meta = json.loads((d / "task.json").read_text())
        meta["repo"]["commit"] = ""
        (d / "task.json").write_text(json.dumps(meta))
        res = U.generate(d, runner=lambda *a: self.fail("ran"), clone_fn=self.clone, facts_fn=self.facts)
        self.assertFalse(res["ok"])


class View(Tmp):
    def test_view_before_research_has_the_brief_fallback_and_type_rules(self):
        d = self.task()
        v = U.view(d)
        self.assertEqual(v["state"], "missing")
        self.assertIsNone(v["doc"])
        self.assertEqual(v["fallback"]["title"], "Undo a bad stock import")
        self.assertTrue(any(r["id"] == "slot-modify-vs-operate" for r in v["rules"]))
        self.assertTrue(v["notes"])                       # Operate's Initial State note

    def test_research_inputs_hold_the_starting_state_recipe(self):
        d = self.task()
        r = U.research(d)
        self.assertEqual(r["starting_state"], "Initial state: a stack with 300 parts.")
        inputs = U.prompt_inputs(r, {"pinned": {}, "releases": [], "threads": {}})
        prompt = P.build("Operate", inputs)
        self.assertIn("This task's type: Operate", prompt)
        self.assertIn("lane_now", prompt)
        self.assertNotIn("{inputs}", prompt)

    def test_the_research_is_sorted_by_the_field_it_bears_on(self):
        # Before the document exists, the Write step's panel has the dossier and brief; with it, the research too.
        d = self.task()
        f = U.for_fields(U.research(d), None)
        self.assertEqual(set(f), {"goal", "decision_points", "codebase", "initial_state"})
        self.assertEqual([g["source"] for g in f["goal"]], ["dossier", "brief"])
        self.assertEqual(f["goal"][1]["items"], [{"text": "A bad import wrecked 300 counts."}])
        self.assertEqual(f["initial_state"][0]["items"][0]["text"], "Initial state: a stack with 300 parts.")
        self.assertEqual(f["decision_points"], [])                 # nothing in that dossier about models
        U._write(d, {"schema": U.SCHEMA_ID, "action": "Operate", "doc": P.EXAMPLE, "inputs_sha": U.inputs_sha(U.research(d))})
        f = U.for_fields(U.research(d), U.load(d))
        self.assertEqual(f["goal"][0], {"head": "The task in a nutshell", "source": "research",
                                        "items": [{"text": P.EXAMPLE["headline"]}]})
        heads = {g["head"]: g for g in f["codebase"]}
        code = heads["Where in the code"]["items"]
        self.assertEqual(code[0]["path"], P.EXAMPLE["code"][0]["path"])
        self.assertEqual(code[0]["text"], P.EXAMPLE["code"][0]["what"])
        self.assertEqual(heads["Words a newcomer misreads"]["items"][0]["text"],
                         f"{P.EXAMPLE['terms'][0]['term']}: {P.EXAMPLE['terms'][0]['means']}")
        self.assertEqual([x["text"] for x in {g["head"]: g for g in f["decision_points"]}["The hard part"]["items"]],
                         P.EXAMPLE["hard"])
        parts = {g["head"]: g for g in f["initial_state"]}["The parts of the starting state"]["items"]
        self.assertTrue(parts[0]["text"].startswith(P.EXAMPLE["kind"]["starting_state"][0]["part"] + ": "))
        # A Modify task has no Initial state field, so nothing is sorted under it.
        m = self.task("mod00001", action="Modify")
        self.assertEqual(U.for_fields(U.research(m), None)["initial_state"], [])
        # Every item is a fact to read, never a drafted field: no group text is longer than the research it quotes.
        for groups in f.values():
            for g in groups:
                for it in g["items"]:
                    self.assertTrue(it["text"])
                    self.assertNotIn("\n", it["text"])

    def test_each_type_has_a_guide_and_schema(self):
        for a in P.ACTIONS:
            self.assertIn(a, P.TYPE_GUIDE)
            self.assertIn(a, P.KIND_SCHEMA)
            self.assertIn(a, U.TYPE_RULES)

    def test_auto_research_only_before_the_prompt_is_written(self):
        d = self.task()
        self.assertTrue(U.wanted(d))
        (d / "submission.md").write_text("## Opening Prompt\nRepair the counts please.\n\n## High-Level Task Goal\n\n")
        self.assertTrue(U.prompt_written(d))
        self.assertFalse(U.wanted(d))
        self.assertFalse(U.view(d)["auto"])
        (d / "submission.md").write_text("## Opening Prompt\n<!-- a comment only -->\n")
        self.assertTrue(U.wanted(d))
        meta = json.loads((d / "task.json").read_text())
        meta["runs"] = [{"name": "validate-1"}]
        (d / "task.json").write_text(json.dumps(meta))
        self.assertFalse(U.wanted(d))

    def test_a_lead_and_the_task_brief_notes_are_research(self):
        d = self.task(with_dossier=False)
        meta = json.loads((d / "task.json").read_text())
        meta["lead_id"] = "L1"
        (d / "task.json").write_text(json.dumps(meta))
        (self.root / "data" / "scout").mkdir()
        (self.root / "data" / "scout" / "leads.json").write_text(json.dumps([
            {"id": "L0", "title": "Another"},
            {"id": "L1", "title": "Bulk recount skips bins", "mechanism": "Offset paging over rows it changes.",
             "issue_url": "https://github.com/acme/partbin/issues/7", "status": "claimed"}]))
        (d / "brief.md").write_text("# Task brief\n\n## Slot\n- Task type: Operate\n\n## Upstream check (done by hand)\n"
                                    "- Same symptom in https://github.com/acme/partbin/issues/640\n\n"
                                    "## Passing difficulty\n- One-shot means drop it.\n")
        r = U.research(d)
        self.assertEqual(r["lead"]["title"], "Bulk recount skips bins")
        self.assertNotIn("status", r["lead"])
        self.assertIn("## Upstream check", r["task_notes"])
        self.assertNotIn("## Slot", r["task_notes"])
        self.assertNotIn("One-shot", r["task_notes"])
        self.assertTrue(U.has_subject(r))
        self.assertEqual(U._related_urls(r), ["https://github.com/acme/partbin/issues/7",
                                              "https://github.com/acme/partbin/issues/640"])
        self.assertIn("task_research_notes", json.dumps(U.prompt_inputs(r, {})))

    def test_nothing_to_research_never_runs(self):
        d = self.task(with_dossier=False)
        r = U.research(d)
        self.assertFalse(U.has_subject(r))
        self.assertFalse(U.wanted(d))
        self.assertIs(U.view(d)["subject"], False)
        res = U.generate(d, runner=lambda *a: self.fail("ran"), clone_fn=lambda *a: self.fail("cloned"),
                         facts_fn=lambda *a: self.fail("facts"))
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], U.NO_SUBJECT)

    def test_ensure_does_nothing_in_fake_mode(self):
        d = self.task()
        with mock.patch.object(config, "FAKE", True), mock.patch.object(config, "CLAUDE_BIN_SET", False):
            self.assertIsNone(U.ensure(d))


class Ledger(Tmp):
    def test_understanding_text_is_a_guard_source(self):
        from johto.web.guard import ledger
        d = self.task(with_dossier=False)
        (d / "understand.json").write_text(json.dumps({"schema": U.SCHEMA_ID, "doc": P.EXAMPLE}))
        items = [i for i in ledger.items(d) if i["label"] == "Task understanding"]
        texts = {i["text"] for i in items}
        self.assertIn(P.EXAMPLE["story"], texts)
        self.assertNotIn(P.EXAMPLE["upstream"]["threads"][0]["url"], texts)
        self.assertNotIn("mixed", texts)
        self.assertTrue(all(i["kind"] == "lead" for i in items))


class Precedent(Tmp):
    def studio_tasks(self):
        rows = [
            {"task_id": "t1", "task_name": "Task aaaa1111", "status": "Accepted", "updated_at": "2026-11-01T10:00",
             "slot": {"action": "Operate"}, "repo": "o/one"},
            {"task_id": "t2", "task_name": "Task bbbb2222", "status": "Rejected", "updated_at": "2026-11-01T12:00",
             "slot": {"action": "Operate"}, "repo": "o/two"},
            {"task_id": "t3", "task_name": "Task cccc3333", "status": "Writing", "updated_at": "2026-11-01T12:00",
             "slot": {"action": "Operate"}, "repo": "o/three"},
            {"task_id": "t4", "task_name": "Task dddd4444", "status": "Accepted", "updated_at": "2026-11-01T12:00",
             "slot": {"action": "Modify"}, "repo": "o/four"},
        ]
        (self.root / "data" / "studio_tasks.json").write_text(json.dumps(rows))

    def test_refresh_reads_judged_tasks_once_and_for_action_orders_them(self):
        self.studio_tasks()
        seen = []

        class Api:
            def task(self, tid):
                seen.append(tid)
                return {"custom_fields": {"field_review_writer_feedback": {"value": f"note {tid}"},
                                          "field_opening_prompt": "a prompt", "field_initial_state": None}}
        self.assertEqual(precedent.refresh(api=Api()), {"read": 3, "kept": 0, "failed": 0})
        self.assertEqual(sorted(seen), ["t1", "t2", "t4"])
        self.assertEqual(precedent.refresh(api=Api())["kept"], 3)
        (self.root / "tasks" / "bbbb2222").mkdir()
        (self.root / "tasks" / "bbbb2222" / "task.json").write_text("{}")
        p = precedent.for_action("Operate")
        self.assertEqual([j["status"] for j in p["judged"]], ["Accepted", "Rejected"])
        self.assertEqual(p["judged"][0]["feedback"], "note t1")
        self.assertTrue(p["judged"][1]["mine"])
        self.assertEqual(p["counts"], {"Accepted": 1, "Rejected": 1, "Writing": 1})
        self.assertEqual([j["short"] for j in precedent.for_action("Operate", exclude="aaaa1111")["judged"]],
                         ["bbbb2222"])

    def test_one_unreadable_task_keeps_the_rest(self):
        self.studio_tasks()

        class Api:
            def task(self, tid):
                if tid == "t2":
                    raise RuntimeError("tab closed")
                return {"custom_fields": {}}
        self.assertEqual(precedent.refresh(api=Api())["failed"], 1)


class Api(Tmp):
    def test_get_and_unknown_task(self):
        from johto.web import api_understand as A
        d = self.task()

        def req(short):
            return SimpleNamespace(path_params={"short": short}, cookies={})
        with mock.patch.object(U, "ensure", return_value=None), mock.patch.object(precedent, "ensure", return_value=None):
            r = asyncio.run(A.get_understand(req(d.name)))
            body = json.loads(r.body)
            self.assertEqual(body["state"], "missing")
            self.assertEqual(body["action"], "Operate")
            self.assertEqual(body["fields"]["goal"][0]["source"], "dossier")   # the Write step's panel
            self.assertEqual(asyncio.run(A.get_understand(req("nope"))).status_code, 404)
            self.assertEqual(asyncio.run(A.get_understand(req("../x"))).status_code, 404)



class Mounted(unittest.TestCase):
    def test_routes_and_page_are_mounted(self):
        src = (Path(__file__).resolve().parent.parent / "johto" / "web" / "app.py").read_text()
        self.assertIn('"api_understand"', src)
        self.assertIn('Route("/task/{short}/understand", index)', src)
        static = Path(__file__).resolve().parent.parent / "johto" / "web" / "static"
        self.assertIn("understand.css", (static / "index.html").read_text())
        self.assertIn("UnderstandPage", (static / "screens" / "workbench" / "index.js").read_text())


if __name__ == "__main__":
    unittest.main()
