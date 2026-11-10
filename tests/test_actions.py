"""johto.actions on its own: which run is submitted, gists per run, the checklist, lane safety, the CLI.

Run: .venv/bin/python -m unittest discover -s tests -p "test_actions*.py"
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import test_actions_support as fx  # noqa: E402

from johto import actions, cli, config, lanes, lint, workspace  # noqa: E402
from johto.actions import ActionError  # noqa: E402
from johto.fakes import gh as fake_gh, lanes as fake_lanes, studio as fake  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        fx.make_root(self.root)
        self.patches = fx.patch_config(self.root)
        for p in self.patches:
            p.start()
        fake.reset()
        fake.QC_DELAY_S = 0
        fake_gh.ACCOUNT = fake_gh.GH_ACCOUNT
        fake_gh.CREATED.clear()
        lead = actions.load_lead(fx.LEAD_ID)
        repo = actions.repo_info(lead["repo"], lead["commit"], lead["primary_language"])
        self.d = actions.claim(fx.TASKS[0]["task_id"], repo, lead)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        fake.reset()
        self.tmp.cleanup()

    def run_(self, kind_or_dir, kind: str | None = None) -> str:
        d = self.d if kind is None else kind_or_dir
        kind = kind or kind_or_dir
        run_dir = fake_lanes.new_lane(d, kind)
        fx.fake_claude(run_dir, fx.PROMPT)
        actions.collect(d, run_dir.name)
        return run_dir.name


class SubmissionRun(Base):
    def test_precedence_explicit_then_chosen_then_latest_validate(self):
        c1 = self.run_("calib")
        self.assertEqual(actions.validation_run(self.d), (None, None))
        v1 = self.run_("validate")
        v2 = self.run_("validate")
        self.assertEqual(actions.validation_run(self.d)[0], v2)
        actions.set_submission_run(self.d, c1)
        self.assertEqual(actions.validation_run(self.d)[0], c1)
        self.assertEqual(actions.validation_run(self.d, v1)[0], v1)
        actions.set_submission_run(self.d, None)
        self.assertEqual(actions.validation_run(self.d)[0], v2)

    def test_only_a_collected_run_can_be_chosen(self):
        fake_lanes.new_lane(self.d, "calib")
        with self.assertRaises(ActionError) as e:
            actions.set_submission_run(self.d, "calib-1")
        self.assertEqual(e.exception.code, "lane_state")
        with self.assertRaises(ActionError) as e:
            actions.set_submission_run(self.d, "../../etc")
        self.assertEqual(e.exception.code, "not_found")

    def test_gists_follow_the_chosen_run(self):
        c1 = self.run_("calib")
        v1 = self.run_("validate")
        g1 = actions.upload_gists(self.d)
        self.assertEqual(g1["run"], v1)
        self.assertEqual(len(g1["uploaded"]), 2)
        self.assertEqual(actions.upload_gists(self.d)["uploaded"], [])     # nothing missing
        actions.set_submission_run(self.d, c1)
        g2 = actions.upload_gists(self.d)
        self.assertEqual(g2["run"], c1)
        self.assertEqual(len(g2["uploaded"]), 2)
        self.assertNotEqual(g1["gists"]["conversation_log"], g2["gists"]["conversation_log"])

    def test_claude_minutes_come_from_the_submitted_run(self):
        self.run_("calib")
        actions.set_submission_run(self.d, "calib-1")
        self.assertEqual(actions.fields(self.d)["field_claude_minutes"], 40)


class Checklist(Base):
    def rows(self):
        return {r["key"]: r for r in actions.checklist(self.d)}

    def test_failures_sort_first_and_block(self):
        rows = actions.checklist(self.d)
        order = [r["status"] for r in rows]
        self.assertEqual(order, sorted(order, key={"fail": 0, "pending": 1, "warn": 2, "pass": 3}.get))
        self.assertTrue(actions.submit_blockers(rows))
        for r in rows:
            self.assertEqual(set(r) >= {"key", "status", "title", "detail", "blocking", "fix"}, True)

    def test_studio_saved_text_row(self):
        self.assertNotIn("studio_saved", self.rows())
        warn = [(lint.WARN, "Validation Notes", 'Studio\'s text looks cut off: it ends "...it fo".')]
        with mock.patch.object(lint, "check_saved_fields", lambda d: warn):
            r = self.rows()["studio_saved"]
        self.assertEqual((r["status"], r["blocking"]), ("warn", False))
        self.assertEqual(r["fix"]["field"], "validation_notes")
        cut = [(lint.ERROR, "Goal", "Studio's copy stops mid-word")] + warn
        with mock.patch.object(lint, "check_saved_fields", lambda d: cut):
            r = self.rows()["studio_saved"]
            self.assertTrue(any("Studio's copy stops mid-word" in m for _, _, m in actions.check(self.d)))
        self.assertEqual((r["status"], r["blocking"], r["fix"]["field"]), ("fail", True, "goal"))

    def test_studio_sync_notices_edits_after_save(self):
        self.run_("validate")
        fx.write_fixture_text(self.d)
        actions.push(self.d)
        self.assertEqual(self.rows()["studio_sync"]["status"], "pass")
        p = self.d / "submission.md"
        p.write_text(p.read_text().replace("It needed one nudge", "It needed two nudges"))
        r = self.rows()["studio_sync"]
        self.assertEqual(r["status"], "fail")
        self.assertEqual(r["changed"], ["validation_notes"])

    def test_autoqc_must_be_newer_than_the_save(self):
        self.run_("validate")
        fx.write_fixture_text(self.d)
        actions.push(self.d)
        aid = actions.start_qc(self.d)
        actions.poll_qc(self.d, aid, every=0)
        self.assertEqual(self.rows()["autoqc"]["status"], "pass")
        with mock.patch.object(actions, "now_iso", lambda: "2999-01-01T00:00:00-0700"):
            actions.push(self.d)
        self.assertEqual(self.rows()["autoqc"]["status"], "pending")

    def test_autoqc_failures_point_at_the_field_and_do_not_block(self):
        self.run_("validate")
        fx.write_fixture_text(self.d)
        actions.push(self.d)
        fake.QC_FAIL.add("High-Level Task Goal")
        actions.poll_qc(self.d, actions.start_qc(self.d), every=0)
        r = self.rows()["autoqc"]
        self.assertEqual(r["status"], "fail")
        self.assertFalse(r["blocking"])
        self.assertEqual(r["fix"], {"action": "focus_field", "field": "goal", "span": None})
        self.assertIn("fake AutoQC, forced", r["dims"][0]["justification"])

    def test_a_failing_rating_grid_blocks(self):
        self.run_("validate")
        fx.write_fixture_text(self.d, ratings=("No", "5", "1"))
        r = self.rows()["ratings"]
        self.assertEqual(r["status"], "fail")
        self.assertTrue(r["blocking"])
        self.assertIn("Task Success 5 is always rejected", r["detail"])


class LaneSafety(Base):
    def test_start_lane_refuses_without_touching_docker(self):
        meta = workspace.load(self.d)
        known = "prework-PREWORK_fix0001-abc-model_a"
        meta["runs"] = [{"kind": "calib", "dir": "runs/calib-1", "container": known}]
        workspace.save(self.d, meta)
        (self.d / "runs" / "calib-1").mkdir(parents=True)
        boom = mock.Mock(side_effect=AssertionError("must not set up a lane"))
        cases = [([known], [known], "lane_busy"),           # Claude alive
                 (["prework-someone-else"], [], "lane_busy"),  # a lane the app doesn't know
                 ([known], [], "lane_state")]               # known but not collected
        for running, active, code in cases:
            with mock.patch.object(config, "FAKE_STUDIO", False), \
                    mock.patch.object(lanes, "free_slot", lambda: None), \
                    mock.patch.object(lanes, "running_lanes", lambda r=running: r), \
                    mock.patch.object(lanes, "claude_active", lambda c, a=active: c in a), \
                    mock.patch.object(lanes, "new_lane", boom):
                with self.assertRaises(ActionError) as e:
                    actions.start_lane(self.d, "validate")
                self.assertEqual(e.exception.code, code)
        boom.assert_not_called()

    def test_start_lane_beside_a_live_lane_when_a_slot_is_free(self):
        """A second lane gets its own ports, so Claude running in the first one doesn't block it."""
        known = "prework-PREWORK_fix0001-abc-model_a"
        made = mock.Mock(return_value=self.d / "runs" / "validate-1")
        with mock.patch.object(config, "FAKE_STUDIO", False), \
                mock.patch.object(lanes, "free_slot", lambda: 1), \
                mock.patch.object(lanes, "running_lanes", lambda: [known]), \
                mock.patch.object(lanes, "claude_active", lambda c: True), \
                mock.patch.object(lanes, "new_lane", made):
            actions.start_lane(self.d, "validate")
        made.assert_called_once_with(self.d, "validate", stop_others=False)

    def test_clean_keeps_live_and_uncollected_lanes(self):
        run = self.run_("calib")
        fake_lanes.new_lane(self.d, "validate")
        c_calib = actions.run_record(self.d, run)["container"]
        c_val = actions.run_record(self.d, "validate-1")["container"]
        with mock.patch.object(fake_lanes, "running_lanes", lambda: [c_calib, c_val]), \
                mock.patch.object(fake_lanes, "claude_active", lambda c: c == c_calib):
            res = actions.clean(self.d)
            self.assertEqual({s["run"] for s in res["skipped"]}, {"calib-1", "validate-1"})
            with self.assertRaises(ActionError):
                actions.clean(self.d, "validate-1")
            actions.clean(self.d, "validate-1", force=True)

    def test_clean_keeps_a_stopped_uncollected_lane(self):
        """A stopped container (Docker restart, reboot) still holds the run's only transcript in its config dir."""
        fake_lanes.new_lane(self.d, "validate")
        fake_lanes.TORN_DOWN.clear()
        with mock.patch.object(fake_lanes, "running_lanes", lambda: []):
            res = actions.clean(self.d)
        self.assertEqual([s["run"] for s in res["skipped"]], ["validate-1"])
        self.assertIn("start the lane and collect it first", res["skipped"][0]["reason"])
        self.assertEqual(fake_lanes.TORN_DOWN, [])

    def test_probe_errors_count_as_alive(self):
        import subprocess
        def boom(*a, **kw):
            raise subprocess.TimeoutExpired(a[0], 20)
        with mock.patch.object(lanes, "_run", boom):
            self.assertTrue(lanes.claude_active("prework-x-model_a"))
        from johto.web.lane import monitor
        with mock.patch.object(monitor, "_docker", boom):
            self.assertTrue(monitor.claude_alive("prework-x-model_a", unknown=True))
            self.assertFalse(monitor.claude_alive("prework-x-model_a"))

    def test_scratch_root_never_touches_real_studio_or_prework_lanes(self):
        with mock.patch.object(config, "ROOT", self.root), mock.patch.object(config, "FAKE", False), \
                mock.patch.object(config, "REAL_OK", False):
            from johto import gists, studio
            with self.assertRaises(config.RealWriteRefused):
                studio.request("GET", "/tasks/x")
            with self.assertRaises(config.RealWriteRefused):
                gists.create(__file__, "x", "y")
            with self.assertRaises(config.RealWriteRefused):
                lanes.new_lane(self.d, "calib")
        with mock.patch.object(config, "FAKE", True), self.assertRaises(config.RealWriteRefused):
            studio.request("GET", "/tasks/x")

    def test_claiming_a_task_already_mine_with_another_repo_is_refused(self):
        tid = workspace.load(self.d)["task_id"]
        before = (self.d / "task.json").read_text()
        other = {**workspace.load(self.d)["repo"], "full_name": "someone/else", "pinned_url": "https://github.com/someone/else/tree/x"}
        with self.assertRaises(ActionError) as e:
            actions.claim(tid, other)
        self.assertEqual(e.exception.code, "conflict")
        self.assertEqual((self.d / "task.json").read_text(), before)

    def test_prints_from_lanes_go_to_the_job_log(self):
        lines, other = [], io.StringIO()
        with contextlib.redirect_stdout(other):
            def elsewhere():
                print("from another thread")
            with actions.capture_prints(lines.append):
                print("setting up")
                t = threading.Thread(target=elsewhere)
                t.start()
                t.join()
        self.assertEqual(lines, ["setting up"])


class ClaimAndDiscard(Base):
    def test_claim_writes_research_to_brief_only(self):
        sub = (self.d / "submission.md").read_text()
        for text in ("Uploads retry forever", "attempt counter reset", "resets attempts"):
            self.assertNotIn(text, sub)
        self.assertIn("Uploads retry forever", (self.d / "brief.md").read_text())

    def test_swe_bench_repo_is_refused_before_any_studio_call(self):
        fake.CALLS.clear()
        repo = actions.repo_info("django/django", fx.COMMIT, "Python")
        with self.assertRaises(ActionError) as e:
            actions.claim(fx.TASKS[1]["task_id"], repo)
        self.assertEqual(e.exception.code, "invalid")
        self.assertEqual(fake.CALLS, [])

    def test_discard_link_fills_facts_not_free_text(self):
        self.run_("calib")   # has a follow-up, so the "how it struggled" answers stay empty
        res = actions.discard(self.d, "easy")
        self.assertTrue(res["unclaimed"])
        url = res["url"]
        self.assertIn("Task+fix0001", url)
        self.assertIn("Too+easy", url)
        self.assertNotIn("entry.1167518327", url)
        self.assertEqual(fake.status_name(fake.task(fx.TASKS[0]["task_id"])), "Ready to Claim")


class Cli(Base):
    def out(self, *argv) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(list(argv))
        return buf.getvalue()

    def test_check_and_submission_run(self):
        self.run_("calib")
        out = self.out("check", self.d.name)
        self.assertIn("no validation run yet", out.lower())
        out = self.out("submission-run", self.d.name, "calib-1")
        self.assertIn("submits calib-1", out)
        out = self.out("check", self.d.name)
        self.assertIn("(run calib-1)", out)
        open_part, _, resolved = out.lower().partition("resolved since the last check")
        self.assertNotIn("no validation run yet", open_part)
        self.assertIn("no validation run yet", resolved)

    def test_action_errors_exit_with_the_plain_message(self):
        with self.assertRaises(SystemExit) as e:
            cli.main(["submission-run", self.d.name, "validate-9"])
        self.assertIn("No run validate-9", str(e.exception))


if __name__ == "__main__":
    unittest.main()


class StudioTab(unittest.TestCase):
    """studio._SCRIPT runs inside browser-harness; here its helpers are fakes."""

    def run_script(self, tabs, sessions):
        import io, contextlib
        from johto import studio
        used, opened, started, polls = [], [], [], []   # ...and a poll reads the answer, null once first

        def js(expr, target_id=None):
            used.append(target_id)
            if "Clerk.session)" in expr and "getToken" not in expr:
                return sessions.get(target_id, False)
            if "getToken" in expr:                  # the fetch starts in the tab...
                started.append(target_id)
                return True
            if not polls:                           # ...and the first poll finds nothing yet
                polls.append(target_id)
                return None
            return json.dumps({"status": 200, "body": "{}", "version": None})

        script = studio._SCRIPT % {"req": json.dumps({"method": "GET", "url": "u", "body": None, "campaign": "c",
                                                      "company": "co", "account": "a"}),
                                   "account": "me@x", "start": "https://studio.mercor.com/start", "mark": studio.MARK,
                                   "timeout": 30}
        ns = {"list_tabs": lambda include_chrome=True: tabs, "js": js,
              "new_tab": lambda url, account=None: opened.append(url) or "new"}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("time.sleep", lambda s: None):
            exec(script, ns)
        return used[-1], opened, out.getvalue()

    def test_a_signed_in_tab_wins_over_one_left_on_sign_in(self):
        tabs = [{"targetId": "signin", "url": "https://studio.mercor.com/sign-in/", "account": "me@x"},
                {"targetId": "tasks", "url": "https://studio.mercor.com/annotator/tasks/", "account": "me@x"}]
        last, opened, out = self.run_script(tabs, {"tasks": True})
        self.assertEqual(last, "tasks")
        self.assertEqual(opened, [])
        self.assertIn('"status": 200', out)

    def test_signed_out_everywhere_reuses_a_tab_and_says_so(self):
        tabs = [{"targetId": "signin", "url": "https://studio.mercor.com/sign-in/", "account": "me@x"}]
        last, opened, out = self.run_script(tabs, {})
        self.assertEqual((last, opened), ("signin", []))
        self.assertIn("no Clerk session", out)

    def test_no_studio_tab_opens_one(self):
        last, opened, _ = self.run_script([], {"new": True})
        self.assertEqual(opened, ["https://studio.mercor.com/start"])
        self.assertEqual(last, "new")


class Drafts(Base):
    """A task folder before any slot: prepared end to end locally, then bound to an open slot."""

    def draft(self) -> Path:
        lead = actions.load_lead(fx.LEAD_ID)
        repo = actions.repo_info(lead["repo"], lead["commit"], lead["primary_language"])
        return actions.draft(fx.SLOT, repo, lead)

    def test_a_draft_has_no_task_id_and_every_local_step_works(self):
        d = self.draft()
        meta = workspace.load(d)
        self.assertTrue(d.name.startswith("d-"))
        self.assertIsNone(meta["task_id"])
        self.assertTrue(workspace.is_draft(meta))
        self.assertEqual(meta["slot"], fx.SLOT)
        self.assertIn("no slot yet", (d / "brief.md").read_text())
        self.assertEqual(workspace.resolve(d.name), d)
        # The lane starts without Studio: no prompt check to wait for.
        fx.write_fixture_text(d)
        gate = actions.lane_gate(d, "validate")
        self.assertTrue(gate["draft"])
        self.assertFalse(gate["checked"])
        run_dir = fake_lanes.new_lane(d, "validate")
        fx.fake_claude(run_dir, fx.PROMPT)
        actions.collect(d, run_dir.name)
        rows = {r["key"]: r for r in actions.checklist(d)}
        self.assertEqual(rows["slot"]["status"], "pending")
        self.assertEqual(rows["slot"]["fix"]["action"], "claim_slot")
        self.assertEqual(rows["slot"]["open"], 2)   # three in the fixture, one claimed in setUp
        self.assertNotIn("studio_sync", rows)
        self.assertEqual(rows["trajectory_logs"]["status"], "pending")
        # Nothing reaches Studio before a slot is claimed.
        with self.assertRaises(ActionError) as cm:
            actions.push(d)
        self.assertEqual(cm.exception.code, "no_slot")
        with self.assertRaises(ActionError):
            actions.start_qc(d)
        self.assertEqual(actions.pull_from_studio(d)["status"], "Draft")
        self.assertEqual([x.name for x, _ in actions.drafts()], [d.name])

    def test_bind_claims_a_matching_open_slot_and_keeps_the_folder(self):
        d = self.draft()
        fx.write_fixture_text(d)
        res = actions.bind(d)
        self.assertEqual(res["task_id"], fx.TASKS[1]["task_id"])   # TASKS[0] went to Base.setUp's claim
        meta = workspace.load(d)
        self.assertEqual(meta["task_id"], res["task_id"])
        self.assertEqual(meta["task_name"], fx.TASKS[1]["task_name"])
        self.assertFalse(workspace.is_draft(meta))
        self.assertNotIn("draft", meta)
        self.assertTrue(meta["drafted"]["bound_at"])
        self.assertIn("studio_at_claim", meta)
        self.assertTrue(d.exists())
        self.assertIn(res["task_id"], (d / "brief.md").read_text())
        t = fake.task(res["task_id"])
        self.assertEqual(fake.status_name(t), "Writing")
        self.assertEqual(t["custom_fields"]["field_repo_name"], "acme/widget")
        # The slots file no longer counts it.
        self.assertNotIn(res["task_id"], [x["task_id"] for x in actions.slot_tasks(fx.SLOT)])
        # Now the usual Studio path works: every field saved.
        pushed = actions.push(d)
        self.assertIn("field_high_level_goal", pushed["fields"])
        self.assertEqual(fake.task(res["task_id"])["custom_fields"]["field_opening_prompt"], fx.PROMPT)
        with self.assertRaises(ActionError) as cm:
            actions.bind(d)
        self.assertEqual(cm.exception.code, "invalid")

    def test_bind_refuses_without_an_open_slot_and_a_wrong_seed(self):
        d = self.draft()
        for t in fx.TASKS[1:]:
            fake.take(t["task_id"])
        with self.assertRaises(ActionError) as cm:
            actions.bind(d)
        self.assertEqual(cm.exception.code, "no_open_slot")
        self.assertTrue(workspace.is_draft(workspace.load(d)))
        with self.assertRaises(ActionError) as cm:
            actions.bind(d, fx.OTHER["task_id"])    # Explicit spec, not the draft's Casual prose
        self.assertEqual(cm.exception.code, "slot_mismatch")
        self.assertEqual(fake.status_name(fake.task(fx.OTHER["task_id"])), "Ready to Claim")

    def test_a_draft_lane_gets_a_task_name_prework_accepts(self):
        d = self.draft()
        name = workspace.lane_task(workspace.load(d), d)
        self.assertRegex(name, r"^Task [a-z0-9]{8}$")          # prework.py's rule: 'Task' + 8 letters or digits
        self.assertTrue(name.startswith("Task " + d.name.replace("-", "")), name)
        meta = workspace.load(d)
        meta["task_id"], meta["task_name"] = "task_" + "0" * 32, "Task ab12cd34"
        self.assertEqual(workspace.lane_task(meta, d), "Task ab12cd34")   # a claimed task keeps Studio's name

    def test_shelving_a_draft_moves_it_aside(self):
        leads = self.root / "data/scout/leads.json"
        before = json.loads(leads.read_text())[0]["status"]
        d = self.draft()
        res = actions.shelve_draft(d)
        self.assertFalse(d.exists())
        self.assertTrue(Path(res["moved_to"]).exists())
        self.assertEqual(json.loads(leads.read_text())[0]["status"], before)   # the lead goes back as it was

    def test_the_cli_starts_a_draft_and_claims_it(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["start", "--lead", fx.LEAD_ID, "--draft", "--mode", "Interactive/pairing", "--yes"])
        short = next(l for l in out.getvalue().splitlines() if l.startswith("Draft:")).split()[1]
        d = self.root / "tasks" / short
        self.assertTrue(workspace.is_draft(workspace.load(d)))
        fx.write_fixture_text(d)
        with contextlib.redirect_stdout(io.StringIO()):
            self.run_(d, "validate")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["claim", short, "--yes"])
        text = out.getvalue()
        self.assertIn("Claimed", text)
        self.assertIn("Saved", text)
        meta = workspace.load(d)
        self.assertFalse(workspace.is_draft(meta))
        self.assertTrue(meta["gists"].get("conversation_log"))
        self.assertEqual(fake.task(meta["task_id"])["custom_fields"]["field_high_level_goal"],
                         fx.FIXTURE_TEXT["High-Level Task Goal"])
        self.assertEqual(meta["qc"][-1]["spec"], "AutoQC")
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["claim", short, "--yes"])
