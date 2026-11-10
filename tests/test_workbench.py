"""The task page's payload (johto/web/api_workbench.py, spec 7.3 and 7.7): one GET per task, in words.

The fixture root has one task per stage (test_realstate_fixtures). Each test reads the payload the page draws from
and checks what Austin would see: the step, the lede, the path, the submit list. The payload never carries field
text (the page reads that from GET /api/tasks/{short}), rule ids stay on the server, and the first layer has no
command lines, file names, run ids or exit codes.
"""
import json
import re
import unittest
from pathlib import Path
from unittest import mock

import test_realstate_fixtures as fx
from test_realstate_app import SAME, call
from test_realstate_derive import RealRoot
from johto import config
from johto.web import api_workbench as wb

KEYS = {"short", "title", "slot", "studio", "origin", "step", "stage", "lede", "path", "current_field", "write_fields",
        "fields", "run", "findings", "submit", "feedback", "calibrate", "dropped", "lede_field", "one_shot"}
STEP = {"aprompt1": "prompt", "bdecide1": "run", "cdetail1": "prompt", "drate001": "write", "echosen1": "write",
        "fsave001": "submit", "gfixqc01": "submit", "hsubmit1": "submit", "isentbk1": "write", "jdrop001": "review",
        "kreview1": "review", "llive001": "run"}
# Keys whose values may name a run (the page posts them back) or hold the drill-in ("See it in the code").
MACHINE_KEYS = {"id", "run", "drill", "url", "short", "dossier_id", "stage", "kind", "state", "action", "field",
                "rows", "current_field", "write_fields", "step", "result", "scope", "suggested_scope", "tone",
                "lane", "plain_state", "level", "status", "changed", "upstream"}
RUN_ID = re.compile(r"\b(?:calib|validate)-\d+\b")
CLI = re.compile(r"\bjohto [a-z]")
FILE = re.compile(r"\.(?:json|jsonl|md)\b")
EXIT = re.compile(r"\bexit(?: code)? \d+\b", re.I)


def words(v, path=""):
    """Every string a person reads in the payload: (path, text), skipping machine keys."""
    if isinstance(v, dict):
        for k, x in v.items():
            if k in MACHINE_KEYS:
                continue
            yield from words(x, f"{path}.{k}")
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from words(x, f"{path}[{i}]")
    elif isinstance(v, str):
        yield path, v


class WbRoot(RealRoot):
    """The fixture root, with plain-words generation off: a GET never starts headless Claude in a test."""

    def setUp(self):
        super().setUp()
        p = mock.patch("johto.plain.available", return_value=False)
        p.start()
        self.addCleanup(p.stop)


class Payload(WbRoot):
    def get(self, short, **kw):
        st, out = call("GET", f"/api/tasks/{short}/workbench", **kw)
        self.assertEqual(st, 200, out)
        return out

    def test_every_stage_has_the_whole_shape(self):
        for short, step in STEP.items():
            with self.subTest(short=short):
                out = self.get(short)
                self.assertEqual(set(out), KEYS)
                self.assertEqual(out["step"], step)
                self.assertEqual([p["id"] for p in out["path"]],
                                 ["prompt", "run", "write", "review" if step == "review" else "submit"])
                self.assertEqual(sum(p["state"] == "current" for p in out["path"]), 1 if step != "review" or
                                 out["stage"] != "accepted" else 0)
                self.assertFalse(out["calibrate"])   # Austin's order: no calibration unless settings ask for it
                self.assertTrue(out["lede"].endswith("."), out["lede"])

    def test_the_payload_never_carries_field_text(self):
        texts = [fx.PROMPT, fx.GOAL, fx.DPS, fx.CODEBASE, fx.NOTES]
        for short in STEP:
            blob = json.dumps(self.get(short))
            for t in texts:
                for line in [x.strip() for x in t.splitlines() if len(x.strip()) > 30]:
                    self.assertNotIn(line[:40], blob, short)

    def test_the_first_layer_is_words(self):
        """No command lines, file names, run ids or exit codes outside the drill-in (rule 5)."""
        for short in STEP:
            for path, text in words(self.get(short)):
                with self.subTest(short=short, path=path):
                    self.assertNotRegex(text, RUN_ID)
                    self.assertNotRegex(text, CLI)
                    self.assertNotRegex(text, FILE)
                    self.assertNotRegex(text, EXIT)
                    self.assertNotIn("—", text)   # no em dashes

    def test_rule_ids_stay_on_the_server(self):
        for short in STEP:
            out = self.get(short)
            blob = json.dumps(out)
            self.assertNotIn('"rule"', blob)
            for item in (out.get("submit") or {}).get("blockers", []) + (out.get("submit") or {}).get("calls", []):
                self.assertRegex(item["id"], r"^i[0-9a-f]{10}$")
                self.assertNotIn(item["title"], wb.RULES)

    def test_prompt_step(self):
        out = self.get("aprompt1")
        self.assertEqual(out["lede"], "Write the opening prompt.")
        self.assertEqual(out["path"][0]["summary"], "Not written")
        self.assertIsNone(out["submit"])
        self.assertEqual(out["run"]["state"], "none")
        out = self.get("cdetail1")   # a kept calibration with no validation run yet: start it
        self.assertEqual(out["lede"], "Start the validation run.")
        self.assertEqual(out["path"][1]["summary"], "Not started")

    def test_run_step(self):
        out = self.get("llive001")
        self.assertIn(out["run"]["state"], ("working", "asks"))
        self.assertTrue(out["lede"].startswith("Claude "))
        self.assertEqual(out["path"][1]["state"], "current")

    def test_write_step(self):
        out = self.get("drate001")
        self.assertEqual((out["current_field"], out["lede"]), ("ratings", "Rate the run."))
        self.assertEqual(out["write_fields"], ["goal", "decision_points", "codebase", "ratings", "validation_notes"])
        self.assertEqual(out["path"][2]["state"], "current")
        rows = out["findings"]["rows"]
        for r in rows:
            self.assertIn(r["result"], ("missed", "partial", "credit", "unknown"))
            self.assertTrue(r["result_words"])

    def test_submit_step_lists_blockers_calls_and_passes(self):
        out = self.get("gfixqc01")
        sub = out["submit"]
        self.assertEqual(sub["qc"]["state"], "failed")
        self.assertFalse(sub["can_submit"])
        qc = [b for b in sub["blockers"] if b.get("ai") == "Mercor's automatic check"]
        self.assertEqual(len(qc), 1)
        self.assertIn({"label": "Run the check again", "action": "rerun_qc"}, qc[0]["actions"])
        # With blockers the lede counts only them (the calls don't stop him).
        n = len(sub["blockers"])
        self.assertTrue(out["lede"].endswith("to fix before you submit."), out["lede"])
        self.assertEqual(out["lede"].split()[0].lower(), wb._number_word(n))
        for item in sub["blockers"] + sub["calls"]:
            self.assertTrue(item["actions"] or item.get("ai"), item)   # every item has its fix as a button
        out = self.get("hsubmit1")
        self.assertEqual(out["submit"]["qc"]["state"], "passed")
        self.assertEqual(out["submit"]["can_submit"], not out["submit"]["blockers"])
        if out["submit"]["can_submit"]:
            self.assertTrue(out["lede"].startswith("Ready to submit."), out["lede"])
        out = self.get("fsave001")
        self.assertEqual(out["submit"]["submit_why"], "Save to Studio first.")

    def test_sent_back_returns_to_write_with_the_reviewers_words(self):
        out = self.get("isentbk1")
        self.assertEqual(out["step"], "write")
        self.assertEqual(out["feedback"]["state"], "sent_back")
        self.assertEqual(out["feedback"]["words"], "Lorem reviewer feedback.")
        self.assertEqual(out["lede"], "The reviewer sent this back.")

    def test_after_submit(self):
        out = self.get("kreview1")
        self.assertTrue(out["lede"].startswith("In review"))
        self.assertEqual(out["path"][3]["label"], "Review")
        out = self.get("jdrop001")
        self.assertEqual((out["lede"], out["path"][3]["summary"]), ("This task was dropped.", "Dropped"))
        self.assertEqual(out["path"][2]["state"], "later")   # nothing was written: no green check

    def test_calibration_shows_only_when_settings_ask(self):
        self.assertFalse(self.get("aprompt1")["calibrate"])
        real = __import__("johto.settings", fromlist=["get"]).get
        with mock.patch("johto.settings.get", lambda k: True if k == "calibrate_first" else real(k)):
            self.assertTrue(self.get("aprompt1")["calibrate"])


class Coverage(unittest.TestCase):
    def test_newest_turns_without_a_review_raise_the_flag(self):
        revs = [{"run": "validate-1", "turn": 1}]
        c = wb.coverage(revs, "validate-1", 3)
        self.assertEqual((c["reviewed_turns"], c["missing_turns"]), ([1], [2, 3]))
        self.assertEqual(c["flag"], "The review covers turn 1 of 3.")
        self.assertEqual(c["action"], "Review turns 2 and 3")

    def test_no_review_yet(self):
        c = wb.coverage([], "validate-1", 3)
        self.assertEqual(c["flag"], "No review of this run yet.")
        self.assertEqual(c["missing_turns"], [1, 2, 3])
        self.assertEqual(wb.coverage([], "validate-1", 1)["action"], "Review turn 1")

    def test_covered_runs_have_no_flag(self):
        c = wb.coverage([{"run": "validate-1", "turn": 3}, {"run": "validate-1", "turn": 2}], "validate-1", 3)
        self.assertIsNone(c["flag"])
        self.assertEqual(c["missing_turns"], [])
        whole = wb.coverage([{"run": "validate-1", "turn": None}], "validate-1", 2)
        self.assertIsNone(whole["flag"])
        other = wb.coverage([{"run": "calib-1", "turn": 3}], "validate-1", 3)
        self.assertEqual(other["flag"], "No review of this run yet.")


class RuleTable(unittest.TestCase):
    def test_every_rule_reads_as_words(self):
        for rid, (title, why) in wb.RULES.items():
            with self.subTest(rule=rid):
                self.assertTrue(title and title[0].isupper())
                for s in (title, why or ""):
                    self.assertNotIn("—", s)
                    self.assertNotRegex(s, CLI)
                    if re.search(r"[-_ ]", rid):   # a rule id never shows; plain words like "check" may
                        self.assertNotIn(rid, s)

    def test_checklist_rows_read_as_words(self):
        """The run row drops its CLI summary (model, effort, prompts, minutes); the saved-copy row names fields."""
        t = {"checklist": [
            {"key": "validation_run", "status": "warn", "title": "Validation 1", "blocking": False,
             "detail": "Opus 5.5 · high · 3 prompts · 11.2 min · validate-1's first prompt differs from your Opening Prompt"},
            {"key": "studio_saved", "status": "warn", "title": "Studio's saved text", "blocking": False,
             "detail": "Goal: Studio has less...; Decision Points: Studio has less... run johto pull",
             "fix": {"action": "focus_field", "field": "goal"},
             "flags": [{"level": "warn", "field": "Goal", "message": "x"},
                       {"level": "warn", "field": "Decision Points", "message": "y"}]}]}
        _, calls, _ = wb._checklist_items(t)
        run, saved = calls
        self.assertEqual(run["why"], "Validation 1's first prompt differs from your Opening Prompt.")
        self.assertNotRegex(run["why"], r"Opus|high|min|prompts")
        self.assertEqual(saved["title"], "Studio holds less than your goal and decision points")
        for s in (saved["title"], saved["why"]):
            self.assertNotIn("johto", s)

    def test_prompt_check_failures_only_count_for_the_prompt_before_a_save(self):
        """The prompt check runs AutoQC with only the prompt in Studio; the empty fields' fails wait for the save."""
        dims = [{"dimension": "Task Scope", "status": "fail"}, {"dimension": "AI Detection", "status": "fail"},
                {"dimension": "High-Level Task Goal", "status": "fail"}]
        t = {"studio": {"qc": {"status": "completed", "dims": dims}}}
        self.assertEqual([d["dimension"] for d in wb._qc_dims(t)], ["Task Scope", "AI Detection"])
        t["studio"]["saved_at"] = "2026-11-01T05:00:00-0700"
        self.assertEqual(len(wb._qc_dims(t)), 3)

    def test_a_known_rule_uses_the_table(self):
        it = wb.lint_item("warn", "goal", "no must-nots (shortcuts that don't count, e.g. loosening tests)", None)
        self.assertEqual(it["title"], "The goal names no shortcut that doesn't count")
        self.assertEqual(it["actions"], [{"label": "Go to the goal", "action": "focus_field:goal"}])
        self.assertEqual(it["tone"], "wait")

    def test_an_unknown_rule_falls_back_to_the_message_with_code_hidden(self):
        it = wb.lint_item("error", "codebase", "the `vidlabel/server/loans.py` helper is named but never explained here",
                          "no-such-rule")
        self.assertEqual(it["tone"], "stop")
        self.assertNotIn("vidlabel/server", it["title"] + it["why"])
        self.assertNotIn("no-such-rule", json.dumps(it))

    def test_evidence_keys(self):
        self.assertEqual(wb._rule_key("minutes-working · validate-1"), "minutes-working")
        self.assertEqual(wb._rule_key("validate-1 turn 1"), "first-prompt")
        self.assertEqual(wb._rule_key("goal_fid · sync"), "sync")
        self.assertEqual(wb._rule_key("R4"), "row")

    def test_words_only(self):
        self.assertEqual(wb.words_only("Run `johto collect x validate-1` to collect it. Claude exited with code 1."
                                       " The goal is fine as written now.", 3),
                         "The goal is fine as written now.")
        self.assertIn("validation 2", wb.words_only("validate-2 is the run that Studio uses for this task.", 3).lower())
        self.assertEqual(wb.words_only("apiRoomHandler routes only .members, .guests, .limits and .invites.", 3), "")

    def test_piped_tests_say_nothing(self):
        """A pipe hides the exit code (last_ok None): the run card names no result, never "failing"."""
        t = {"summary": {"stage": "rate"}, "runs": [{"name": "validate-1", "kind": "validate", "collected_at": "x",
                                                     "facts": {"tests": {"runs": 3, "last_ok": None}, "turns": 2}}]}
        r = wb.run_block(t)
        self.assertIsNone(r["tests_words"])
        t["runs"][0]["facts"]["tests"]["last_ok"] = False
        self.assertEqual(wb.run_block(t)["tests_words"], "tests failing")


    def test_a_waiting_lane_is_a_question_on_the_run_card(self):
        """Now's lane_waiting need is the same fact the run card shows: Claude asked, in his words."""
        t = {"summary": {"stage": "validating"}, "runs": [{"name": "validate-1", "kind": "validate", "live": True,
                                                           "facts": {}}],
             "lane_need": {"kind": "lane_waiting", "question": "Should I also update the old migration?",
                           "run": "validate-1"}}
        r = wb.run_block(t)
        self.assertEqual(r["state"], "asks")
        self.assertIn("old migration", r["question"])
        t["lane_need"]["run"] = "validate-2"   # another run's need says nothing about this one
        self.assertEqual(wb.run_block(t)["state"], "working")


class Reviews(WbRoot):
    def post(self, short, body):
        return call("POST", f"/api/tasks/{short}/reviews", body, headers=SAME)

    def test_a_pasted_review_is_saved_and_named_in_words(self):
        from johto import reviews
        with mock.patch.object(wb, "_plain_mod", return_value=None):
            st, out = self.post("drate001", {"text": "## Digest\nThe run missed the retry path.\n", "source": "gptpro"})
        self.assertEqual(st, 200, out)
        self.assertEqual(out["toast"]["text"], "Saved the review of validation 1, turn 5.")
        saved = reviews.list_reviews(config.TASKS / "drate001")
        self.assertEqual([(r["run"], r["turn"]) for r in saved], [("validate-1", 5)])
        self.assertNotRegex(json.dumps(out["toast"]), RUN_ID)

    def test_bad_input_is_refused(self):
        self.assertEqual(self.post("drate001", {"text": "  "})[0], 422)
        self.assertEqual(self.post("drate001", {"text": "x", "source": "chatgpt-auto"})[0], 400)
        self.assertEqual(self.post("drate001", {"text": "x", "run": "../x"})[0], 400)
        self.assertEqual(self.post("drate001", {"text": "x", "turn": "2"})[0], 400)
        self.assertEqual(self.post("drate001", {"text": "x" * (wb.MAX_REVIEW_CHARS + 1)})[0], 422)
        self.assertEqual(self.post("aprompt1", {"text": "A reply with no run to attach to."})[0], 404)

    def test_writes_need_the_page(self):
        st, _ = call("POST", "/api/tasks/drate001/reviews", {"text": "x"})
        self.assertEqual(st, 403)

    def test_re_entering_a_review_replaces_the_saved_one(self):
        """"Re-enter the review": the run's newest saved review goes aside, the pasted one takes its place at the
        same turn, and the toast says so in words."""
        from johto import reviews
        d = config.TASKS / "drate001"
        with mock.patch.object(wb, "_plain_mod", return_value=None):
            self.assertEqual(self.post("drate001", {"text": "## Digest\nNothing to replace.\n", "replace": True})[0], 404)
            st, first = self.post("drate001", {"text": "## Digest\nFirst reading.\n", "source": "gptpro", "turn": 3})
            self.assertEqual(st, 200, first)
            st, out = self.post("drate001", {"text": "## Digest\nSecond reading.\n", "source": "gptpro", "replace": True})
        self.assertEqual(st, 200, out)
        self.assertEqual(out["toast"]["text"], "Replaced the review of validation 1, turn 3.")
        self.assertEqual(out["replaced"]["name"], first["review"]["name"])
        saved = reviews.list_reviews(d)
        self.assertEqual([(r["run"], r["turn"]) for r in saved], [("validate-1", 3)])
        self.assertIn("Second reading", reviews.body(Path(saved[0]["path"])))
        self.assertEqual(len(list((d / "reviews" / reviews.REMOVED).glob("*.md"))), 1)
        self.assertNotRegex(json.dumps(out["toast"]), RUN_ID)
        self.assertEqual(self.post("drate001", {"text": "x", "replace": "no-such-review.md"})[0], 404)
        self.assertEqual(self.post("drate001", {"text": "x", "replace": 3})[0], 400)

    def test_the_api_json_of_a_reply_is_saved_as_its_markdown(self):
        from johto import reviews
        wrapped = json.dumps({"type": "message", "role": "assistant", "status": "completed",
                              "content": [{"type": "output_text", "text": "## Digest\n\nFrom the API.\n"}]})
        with mock.patch.object(wb, "_plain_mod", return_value=None):
            st, out = self.post("drate001", {"text": wrapped, "source": "gptpro"})
        self.assertEqual(st, 200, out)
        saved = reviews.list_reviews(config.TASKS / "drate001")
        self.assertEqual(len(saved), 1)
        self.assertEqual(reviews.body(Path(saved[0]["path"])).strip(), "## Digest\n\nFrom the API.")
        self.assertEqual(saved[0]["first_line"], "Digest")


class ReviewLatest(WbRoot):
    def test_it_packs_and_copies_and_never_sends(self):
        """202 with a job; the job packs the missing turns with the existing pack code and only copies the pack."""
        seen = {}

        def submit(kind, target, work, **kw):
            seen.update(kind=kind, target=target, work=work, **kw)
            return {"id": "j1", "kind": kind, "target": target, "state": "queued"}
        with mock.patch("johto.web.jobs.submit", submit), mock.patch("johto.web.jobs.active", lambda **kw: []):
            st, out = call("POST", "/api/tasks/drate001/review-latest", {}, headers=SAME)
        self.assertEqual(st, 202, out)
        self.assertEqual(out["job"]["kind"], "review_pack")
        self.assertEqual(seen["params"]["run"], "validate-1")
        self.assertEqual(seen["params"]["turns"], [1, 2, 3, 4, 5])
        pack = config.TASKS / "drate001" / "pack.md"
        pack.write_text("the pack")

        class Ctx:
            logs = []
            def step(self, s): pass
            def log(self, s): self.logs.append(s)
        with mock.patch("johto.delta.next_pack", lambda d, run, log=None, ai=True: (self.assertFalse(ai), pack)[1]), \
                mock.patch.object(config, "FAKE", True), mock.patch("subprocess.run") as run:
            res = seen["work"](Ctx())
        run.assert_not_called()   # a fake server leaves the clipboard alone
        self.assertEqual(res["text"], "Copied. Paste it into GPT Pro.")
        code = Path(wb.__file__).read_text()   # the module has no network client at all
        self.assertNotRegex(code, r"(?i)chatgpt\.com|openai|requests\.post|httpx")

    def test_no_run_yet(self):
        st, out = call("POST", "/api/tasks/aprompt1/review-latest", {}, headers=SAME)
        self.assertEqual(st, 409)


class Demo(unittest.TestCase):
    STEPS = {"d3m0aaa9": "prompt", "d3m0aab3": "run", "d3m0aaa1": "run", "d3m0aab1": "write", "d3m0aaa7": "prompt",
             "d3m0aaa6": "submit", "d3m0aaa4": "write", "d3m0aaa8": "review"}

    def test_demo_tasks_answer_from_demo_data(self):
        for short, step in self.STEPS.items():
            with self.subTest(short=short):
                st, out = call("GET", f"/api/tasks/{short}/workbench", demo_mode=True)
                self.assertEqual(st, 200, out)
                self.assertEqual(set(out), KEYS)
                self.assertEqual(out["step"], step)
                for path, text in words(out):
                    self.assertNotRegex(text, RUN_ID, path)
                    self.assertNotRegex(text, CLI, path)
        st, out = call("GET", "/api/tasks/d3m0aaa4/workbench", demo_mode=True)
        self.assertEqual(out["feedback"]["state"], "sent_back")
        self.assertEqual(out["path"][1]["summary"], "Done")
        st, out = call("GET", "/api/tasks/d3m0aab1/workbench", demo_mode=True)
        self.assertTrue(out["findings"]["rows"])
        self.assertTrue(all(r["plain"] for r in out["findings"]["rows"]))


if __name__ == "__main__":
    unittest.main()
