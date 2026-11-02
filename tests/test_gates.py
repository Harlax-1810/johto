"""Gates before a validation lane (same prompt, prompt AutoQC), the prompt each run records, stage events,
the collect hook for the writing kit, and the CLI wiring for module commands.

Run: .venv/bin/python -m unittest tests.test_gates -v   (with PYTHONPATH=.:tests)
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import test_actions_support as fx  # noqa: E402
from test_actions import Base  # noqa: E402

from johto import actions, cli, lanes, lint, stages, sync, workspace  # noqa: E402
from johto.actions import ActionError  # noqa: E402
from johto.fakes import lanes as fake_lanes, studio as fake  # noqa: E402

NEW_PROMPT = ("The widget sync job retries failed uploads with no limit, so one bad file can fill the queue "
              "overnight. Can you find out why the attempt limit never applies and fix it?")
F = "field_opening_prompt"


def set_prompt(d: Path, text: str) -> None:
    sync.write_section_file(d, "Opening Prompt", text)


def studio_edit(d: Path, **fields) -> None:
    """Austin typing in Studio."""
    fake.patch_fields(workspace.load(d)["task_id"], fields)


def studio_fields(d: Path) -> dict:
    return fake.task(workspace.load(d)["task_id"])["custom_fields"]


def events(d: Path, kind: str) -> list[dict]:
    return stages.read(d, (kind,))


def iso_utc(offset_s: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_s))


class GateBase(Base):
    def setUp(self):
        super().setUp()
        self.lines: list[str] = []
        # No real johto.kit during these tests (another builder may be writing one).
        self.nokit = mock.patch.dict(sys.modules, {"johto.kit": None})
        self.nokit.start()

    def tearDown(self):
        self.nokit.stop()
        super().tearDown()

    def log(self, line):
        self.lines.append(str(line))

    def saved_prompt(self, text: str = fx.PROMPT) -> None:
        """The prompt written here and saved to Studio (both copies agree)."""
        set_prompt(self.d, text)
        actions.push(self.d)

    def qc(self) -> str:
        aid = actions.start_qc(self.d)
        actions.poll_qc(self.d, aid, every=0)
        return aid

    def lane(self, **kw) -> Path:
        return actions.start_lane(self.d, "validate", log=self.log, **kw)

    def refused(self, code: str, **kw) -> ActionError:
        before = sorted(p.name for p in (self.d / "runs").glob("*-*"))
        with self.assertRaises(ActionError) as e:
            self.lane(**kw)
        self.assertEqual(e.exception.code, code, e.exception.message)
        self.assertEqual(sorted(p.name for p in (self.d / "runs").glob("*-*")), before, "no lane may be set up")
        return e.exception

    def collected_run(self, kind: str, prompt: str) -> str:
        run_dir = fake_lanes.new_lane(self.d, kind)
        fx.fake_claude(run_dir, prompt)
        actions.collect(self.d, run_dir.name)
        return run_dir.name


class PromptRecord(GateBase):
    def test_sha_ignores_whitespace_only(self):
        self.assertEqual(lanes.prompt_sha("a  b\n\n c "), lanes.prompt_sha("a b c"))
        self.assertNotEqual(lanes.prompt_sha("a b c"), lanes.prompt_sha("a b d"))

    def test_lane_start_records_the_prompt_and_an_event(self):
        set_prompt(self.d, fx.PROMPT)
        run_dir = actions.start_lane(self.d, "calib")
        rec = actions.run_record(self.d, run_dir.name)
        self.assertEqual(rec["prompt_sha"], lanes.prompt_sha(fx.PROMPT))
        self.assertEqual(rec["prompt_head"], fx.PROMPT[:200])
        ev = events(self.d, "lane_started")[-1]
        self.assertEqual((ev["run"], ev["lane_kind"], ev["host"]), ("calib-1", "calib", "local"))

    def test_real_new_lane_records_the_prompt_too(self):
        set_prompt(self.d, fx.PROMPT)

        def setup(cmd, **kw):
            (Path(kw["cwd"]) / "prework-manifest.json").write_text(json.dumps({"lane": {"container": "prework-x-model_a"}}))
            return mock.Mock(returncode=0, stdout="[OK] done\n", stderr="")
        with mock.patch.object(lanes, "_guard_real", lambda *a: None), \
                mock.patch.object(lanes, "_pick_slot", lambda stop: 0), \
                mock.patch.object(lanes.subprocess, "run", setup), contextlib.redirect_stdout(io.StringIO()):
            run_dir = lanes.new_lane(self.d, "validate")
        rec = actions.run_record(self.d, run_dir.name)
        self.assertEqual(rec["prompt_sha"], lanes.prompt_sha(fx.PROMPT))


class SamePrompt(GateBase):
    def test_a_repeat_of_an_earlier_validation_prompt_is_refused(self):
        self.saved_prompt()
        self.qc()
        self.collected_run("validate", fx.PROMPT)
        e = self.refused("same_prompt")
        self.assertIn("validate-1 already ran this prompt", e.message)
        self.assertIn("`johto resume fix0001 validate-1`", e.message)
        self.assertIn("`johto lane fix0001 validate --again`", e.message)
        self.assertEqual(e.status, 409)
        run_dir = self.lane(again=True)
        self.assertEqual(run_dir.name, "validate-2")
        self.assertTrue(events(self.d, "lane_started")[-1]["again"])

    def test_whitespace_changes_are_still_the_same_prompt(self):
        self.saved_prompt()
        self.collected_run("validate", fx.PROMPT.replace(". ", ".\n\n"))
        self.refused("same_prompt", skip_qc=True)

    def test_recorded_sha_and_head_count_for_runs_without_a_transcript(self):
        set_prompt(self.d, fx.PROMPT)
        for rec in ({"prompt_sha": lanes.prompt_sha(fx.PROMPT)}, {"prompt_head": fx.PROMPT[:200]}):
            meta = workspace.load(self.d)
            meta["runs"] = [{"kind": "validate", "dir": "runs/validate-1", "created_at": "2026-10-31T13:11:26-0700",
                             "host": "build-box", **rec}]
            workspace.save(self.d, meta)
            (self.d / "runs" / "validate-1").mkdir(parents=True, exist_ok=True)
            e = self.refused("same_prompt", skip_qc=True)
            self.assertEqual("prompt_head" in rec, "first 200 characters" in e.message, e.message)

    def test_the_refusal_names_the_pc_and_the_saved_review(self):
        """A PC run: the message says where it ran, steers over ssh, and points at its saved review."""
        set_prompt(self.d, fx.PROMPT)
        meta = workspace.load(self.d)
        meta["runs"] = [{"kind": "validate", "dir": "runs/validate-1", "created_at": "2026-10-31T13:11:26-0700",
                         "host": "build-box", "remote_dir": "/home/a/johto-lanes/fix0001/validate-1",
                         "prompt_sha": lanes.prompt_sha(fx.PROMPT)}]
        workspace.save(self.d, meta)
        (self.d / "runs" / "validate-1").mkdir(parents=True, exist_ok=True)
        from johto import reviews
        reviews.save(self.d, "validate-1", 1, "## Digest\nNothing new.\n")
        e = self.refused("same_prompt", skip_qc=True)
        self.assertIn("on build-box.", e.message)
        self.assertIn("It has a saved review of turn 1", e.message)
        self.assertIn("`johto review show fix0001 validate-1-t01", e.message)
        self.assertIn("`ssh -t build-box 'cd /home/a/johto-lanes/fix0001/validate-1/model_a && ./claude-dev", e.message)
        self.assertNotIn("johto resume", e.message)
        self.assertNotIn("rec", e.details["runs"][0])

    def test_a_new_prompt_a_calibration_run_and_a_removed_lane(self):
        self.collected_run("calib", fx.PROMPT)    # calibration runs don't count
        set_prompt(self.d, fx.PROMPT)
        self.assertEqual(self.lane(skip_qc=True).name, "validate-1")
        fx.fake_claude(self.d / "runs" / "validate-1", fx.PROMPT)
        actions.collect(self.d, "validate-1")
        actions.update_meta(self.d, lambda m: m["runs"][-1].update(torn_down_at="2026-10-31T01:12:28-0700"))
        e = self.refused("same_prompt", skip_qc=True)
        self.assertNotIn("johto resume", e.message)      # its lane is gone
        set_prompt(self.d, NEW_PROMPT)
        self.assertEqual(self.lane(skip_qc=True).name, "validate-2")


class QcGate(GateBase):
    def test_passes_after_autoqc_on_this_prompt(self):
        self.saved_prompt()
        aid = self.qc()
        run_dir = self.lane()
        self.assertEqual(run_dir.name, "validate-1")
        self.assertEqual(events(self.d, "lane_started")[-1]["qc_audit"], aid)
        self.assertTrue(any("passed AutoQC" in ln for ln in self.lines), self.lines)

    def test_no_autoqc_yet_or_since_the_prompt_changed(self):
        self.saved_prompt()
        e = self.refused("prompt_qc")
        self.assertEqual(e.details["reason"], "stale")
        self.assertIn("No AutoQC since the opening prompt last changed", e.message)
        self.assertIn("`johto lane fix0001 validate --qc`", e.message)
        self.qc()
        self.saved_prompt(NEW_PROMPT)        # changed in both places after the audit
        self.assertEqual(self.refused("prompt_qc").details["reason"], "stale")

    def test_a_failed_prompt_dimension_blocks_with_its_reason(self):
        self.saved_prompt()
        fake.QC_FAIL.add("Prompt Style Fit")
        self.qc()
        e = self.refused("prompt_qc")
        self.assertEqual(e.details["reason"], "failed")
        self.assertIn("Prompt Style Fit: The Prompt Style Fit check failed on this text", e.message)
        self.assertEqual([x["dimension"] for x in e.details["failed"]], ["Prompt Style Fit"])

    def test_a_justification_with_braces_is_quoted_as_is(self):
        self.saved_prompt()
        fake.QC_FAIL.add("Doesn't Solve It")
        aid = self.qc()

        def put(m):
            for x in m["qc"][-1]["dims"]:
                if x["dimension"] == "Doesn't Solve It":
                    x["justification"] = "It names {the fix} in the prompt."
        actions.update_meta(self.d, put)
        with mock.patch.object(fake, "qc_audits", lambda tid, status=None: []):
            e = self.refused("prompt_qc")
        self.assertIn("Doesn't Solve It: It names {the fix} in the prompt.", e.message)
        self.assertEqual(e.details["audit_id"], aid)

    def test_other_dimensions_do_not_block_the_lane(self):
        self.saved_prompt()
        fake.QC_FAIL.add("High-Level Task Goal")   # the goal may be written after the validation run
        self.qc()
        self.assertEqual(self.lane().name, "validate-1")

    def test_the_prompt_dimensions_come_from_rules_when_it_has_them(self):
        rules = types.ModuleType("johto.rules")
        rules.PROMPT_QC_DIMENSIONS = ("Seeded Action Match",)
        with mock.patch.dict(sys.modules, {"johto.rules": rules}):
            self.assertEqual(actions.prompt_dimensions(), ("Seeded Action Match",))
        with mock.patch.dict(sys.modules, {"johto.rules": None}):
            self.assertEqual(actions.prompt_dimensions(), actions.PROMPT_DIMS_FALLBACK)

    def test_studio_prompt_differs_from_the_local_one(self):
        self.saved_prompt()
        self.qc()
        studio_edit(self.d, **{F: NEW_PROMPT})
        e = self.refused("prompt_qc")
        self.assertEqual(e.details["reason"], "differs")
        self.assertIn("Studio's opening prompt isn't the one in submission.md", e.message)

    def test_running_and_unreadable(self):
        self.saved_prompt()
        fake.QC_DELAY_S = 3600
        actions.start_qc(self.d)
        self.assertEqual(self.refused("prompt_qc").details["reason"], "running")
        fake.sign_out()
        e = self.refused("prompt_qc")
        self.assertEqual(e.details["reason"], "unreadable")
        self.assertIn("--skip-qc", e.message)

    def test_skip_qc_warns_and_reads_nothing_from_studio(self):
        set_prompt(self.d, fx.PROMPT)
        fake.CALLS.clear()
        self.assertEqual(self.lane(skip_qc=True).name, "validate-1")
        self.assertEqual(fake.CALLS, [])
        warn = [ln for ln in self.lines if ln.startswith("warning:")]
        self.assertEqual(len(warn), 1)
        self.assertTrue(events(self.d, "lane_started")[-1]["skip_qc"])

    def test_calibration_lanes_are_not_gated(self):
        set_prompt(self.d, fx.PROMPT)
        self.assertEqual(actions.start_lane(self.d, "calib").name, "calib-1")

    def test_no_prompt_anywhere(self):
        with self.assertRaises(ActionError) as e:
            self.lane(need_prompt=True)
        self.assertEqual(e.exception.code, "missing_fields")
        with self.assertRaises(ActionError) as e:
            self.lane(need_prompt=True, skip_qc=True)
        self.assertIn("`johto pull fix0001`", e.exception.message)
        self.assertEqual(self.lane().name, "validate-1")   # the web app's lanes take the prompt later


class StudioAudits(GateBase):
    """AutoQC Austin ran in Studio himself: no record here, so Studio's history decides."""

    def setUp(self):
        super().setUp()
        self.saved_prompt()
        self.tid = workspace.load(self.d)["task_id"]

    def history(self, *versions):
        rows = [{"version": v, "updated_at": at, "custom_fields": {F: p}} for v, at, p in versions]
        return mock.patch.object(fake, "history", lambda tid: {"history": rows})

    def audits_at(self, version):
        real = fake.qc_audits
        return mock.patch.object(fake, "qc_audits", lambda tid, status=None: [
            {**a, "subject_version": version} for a in real(tid, status)])

    def test_without_history_it_counts_as_stale(self):
        fake.run_qc(self.tid)
        self.assertEqual(self.refused("prompt_qc").details["reason"], "stale")

    def test_the_audited_version_held_this_prompt(self):
        fake.run_qc(self.tid)
        with self.history((3, iso_utc(-600), "old text"), (4, iso_utc(-300), fx.PROMPT)), self.audits_at(4):
            self.assertEqual(self.lane().name, "validate-1")
        with self.history((3, iso_utc(-600), "old text"), (4, iso_utc(-300), fx.PROMPT)), self.audits_at(3):
            self.assertEqual(self.refused("prompt_qc", again=True).details["reason"], "stale")

    def test_by_time_when_the_audit_has_no_version(self):
        fake.run_qc(self.tid)
        with self.history((1, iso_utc(-900), "old text"), (2, iso_utc(-600), fx.PROMPT), (3, iso_utc(-300), fx.PROMPT)):
            self.assertEqual(self.lane().name, "validate-1")
        with self.history((1, iso_utc(-900), "old text"), (2, iso_utc(+600), fx.PROMPT)):
            self.assertEqual(self.refused("prompt_qc", again=True).details["reason"], "stale")


class QcNow(GateBase):
    """--qc: Studio's edits win, only the prompt is ever saved, AutoQC runs, then the lane."""

    def patched(self) -> list[tuple]:
        return [args[1] for name, args in fake.CALLS if name == "patch_fields"]

    def test_only_this_copy_changed_saves_the_prompt_alone(self):
        fx.write_fixture_text(self.d)
        actions.push(self.d)
        self.collected_run("validate", fx.PROMPT)
        set_prompt(self.d, NEW_PROMPT)
        p = self.d / "submission.md"
        p.write_text(p.read_text().replace("It needed one nudge", "It needed two nudges"))
        fake.CALLS.clear()
        run_dir = self.lane(qc=True)
        self.assertEqual(run_dir.name, "validate-2")
        self.assertEqual(self.patched(), [(F,)])
        cf = studio_fields(self.d)
        self.assertEqual(cf[F], NEW_PROMPT)
        self.assertIn("one nudge", cf["field_self_check"])      # the other fields stay as Studio has them
        self.assertEqual(actions.run_record(self.d, "validate-2")["prompt_sha"], lanes.prompt_sha(NEW_PROMPT))
        q = actions.latest_qc(workspace.load(self.d))
        self.assertEqual(q["prompt_sha"], lanes.prompt_sha(NEW_PROMPT))
        self.assertTrue(any("Prompt Style Fit" in ln for ln in self.lines), self.lines)
        self.assertEqual(events(self.d, "studio_pushed")[-1]["fields"], [F])

    def test_studio_edit_wins_even_after_a_run(self):
        self.saved_prompt()
        self.collected_run("validate", fx.PROMPT)
        studio_edit(self.d, **{F: NEW_PROMPT})
        fake.CALLS.clear()
        self.assertEqual(self.lane(qc=True).name, "validate-2")
        self.assertEqual(self.patched(), [])
        self.assertEqual(workspace.parse_submission(self.d)["sections"]["Opening Prompt"], NEW_PROMPT)
        self.assertIn("pulled_from_studio", (self.d / "app" / "authorship.jsonl").read_text())

    def test_changed_in_both_places_touches_nothing(self):
        self.saved_prompt()
        self.collected_run("validate", fx.PROMPT)
        studio_edit(self.d, **{F: NEW_PROMPT})
        set_prompt(self.d, NEW_PROMPT + " It is urgent.")
        fake.CALLS.clear()
        e = self.refused("sync_conflict", qc=True)
        self.assertIn("isn't clear which one is newer", e.message)
        self.assertEqual(self.patched(), [])
        self.assertNotIn("run_qc", [n for n, _ in fake.CALLS])

    def test_studio_empty_gets_the_prompt(self):
        set_prompt(self.d, fx.PROMPT)
        fake.CALLS.clear()
        self.assertEqual(self.lane(qc=True).name, "validate-1")
        self.assertEqual(self.patched(), [(F,)])

    def test_an_already_passed_prompt_is_not_checked_again(self):
        self.saved_prompt()
        aid = self.qc()
        fake.CALLS.clear()
        self.assertEqual(self.lane(qc=True).name, "validate-1")
        self.assertNotIn("run_qc", [n for n, _ in fake.CALLS])
        self.assertEqual(events(self.d, "lane_started")[-1]["qc_audit"], aid)
        self.assertTrue(any("isn't run again" in ln for ln in self.lines), self.lines)

    def test_a_failing_prompt_sets_up_no_lane(self):
        set_prompt(self.d, fx.PROMPT)
        fake.QC_FAIL.add("No Formatting Tells")
        e = self.refused("prompt_qc", qc=True)
        self.assertEqual(e.details["reason"], "failed")
        self.assertTrue(any(ln.strip().startswith("FAIL") and "No Formatting Tells" in ln for ln in self.lines), self.lines)

    def ai_flag(self, justification: str):
        """AutoQC's AI Detection fails with this justification (the fake's own text names no field)."""
        fake.QC_FAIL.add("AI Detection")
        real = fake.qc_audit

        def audit(aid):
            a = real(aid)
            for sec in (a.get("outcome") or {}).get("sections") or []:
                for dd in sec["dimensions"]:
                    if dd["dimension"] == "AI Detection":
                        dd["justification"] = justification
            return a
        return mock.patch.object(fake, "qc_audit", audit)

    def test_ai_detection_on_the_goal_only_warns(self):
        """AI Detection scores every field: a flag on the goal or notes never needs another run."""
        set_prompt(self.d, fx.PROMPT)
        with self.ai_flag("GPTZero and Pangram both score field_high_level_goal as likely AI-generated."):
            self.assertEqual(self.lane(qc=True).name, "validate-1")
        self.assertTrue(any(ln.startswith("warning: AutoQC's AI Detection") for ln in self.lines), self.lines)
        self.assertTrue(any(ln.strip().startswith("warn") and "AI Detection" in ln for ln in self.lines), self.lines)

    def test_ai_detection_without_a_reason_only_warns(self):
        set_prompt(self.d, fx.PROMPT)
        with self.ai_flag(""):
            self.assertEqual(self.lane(qc=True).name, "validate-1")

    def test_ai_detection_on_the_prompt_blocks(self):
        set_prompt(self.d, fx.PROMPT)
        with self.ai_flag("The goal reads as human. The opening prompt scores as likely AI-generated on both detectors."):
            e = self.refused("prompt_qc", qc=True)
        self.assertEqual([x["dimension"] for x in e.details["failed"]], ["AI Detection"])

    def test_ai_detection_warning_on_a_stored_audit(self):
        """No --qc: the latest audit already checked this prompt and only the goal was flagged."""
        self.saved_prompt()
        with self.ai_flag("Flagged: field_self_check. The opening prompt is human-written."):
            self.qc()
        self.assertEqual(self.lane().name, "validate-1")
        self.assertTrue(any("AI Detection flagged the task" in ln for ln in self.lines), self.lines)

    def test_empty_post_run_fields_never_block(self):
        """Before the run only the prompt is written: AutoQC fails the goal, decision points and codebase, and
        the lane still starts."""
        self.saved_prompt()
        self.qc()
        dims = actions.latest_qc(workspace.load(self.d))["dims"]
        failed = {x["dimension"] for x in dims if x["status"] == "fail"}
        self.assertTrue({"High-Level Task Goal", "Decision Points", "Difficulty & Codebase Understanding"} <= failed)
        self.assertEqual(self.lane().name, "validate-1")

    def test_points_at_prompt(self):
        yes = ["The opening prompt scores 0.97 AI on GPTZero.", "field_opening_prompt: likely AI",
               "Both detectors flag the prompt and the goal."]
        no = ["", "Goal and validation notes are flagged as AI-generated.",
              "field_high_level_goal scored 0.9; field_opening_prompt scored low.",
              "The prompt reads as human; the decision points do not."]
        for t in yes:
            self.assertTrue(actions.points_at_prompt(t), t)
        for t in no:
            self.assertFalse(actions.points_at_prompt(t), t)

    def test_same_prompt_is_checked_after_studio_text_comes_in(self):
        """The local copy repeats validate-1, but Studio has his new prompt: --qc takes it and goes on."""
        self.saved_prompt()
        self.collected_run("validate", fx.PROMPT)
        self.refused("same_prompt")                 # both copies agree, and validate-1 ran them
        studio_edit(self.d, **{F: NEW_PROMPT})
        self.assertEqual(self.refused("prompt_qc").details["reason"], "differs")
        self.assertEqual(self.lane(qc=True).name, "validate-2")


class Events(GateBase):
    def test_claim_lane_collect_push_pull_qc_submit(self):
        self.assertEqual(events(self.d, "claimed")[-1]["task_name"], "Task fix0001")
        self.collected_run("validate", fx.PROMPT)
        ev = events(self.d, "collected")[-1]
        self.assertEqual((ev["run"], ev["turns"]), ("validate-1", 2))
        self.assertIn("working_minutes", ev)
        fx.write_fixture_text(self.d)
        actions.upload_gists(self.d)
        actions.push(self.d)
        self.assertIn(F, events(self.d, "studio_pushed")[-1]["fields"])
        studio_edit(self.d, field_self_check="Edited in Studio after the save.")
        actions.pull_from_studio(self.d)
        self.assertEqual(events(self.d, "studio_pulled")[-1]["fields"], ["Validation Notes"])
        n = len(events(self.d, "studio_pulled"))
        actions.pull_from_studio(self.d)            # nothing new: no event
        self.assertEqual(len(events(self.d, "studio_pulled")), n)
        aid = self.qc()
        ev = events(self.d, "prompt_qc")
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["audit_id"], ev[0]["prompt"]), (aid, "pass"))
        with self.assertRaises(ActionError) as e:     # Studio has no Trajectory Logs upload yet
            actions.submit(self.d)
        self.assertEqual(e.exception.code, "missing_fields")
        rec = actions.run_record(self.d, "validate-1")
        studio_edit(self.d, field_trajectory_log_files=[
            {"file_s3_url": f"s3://bucket/file_{'a' * 32}_{Path(rec['transcript']).name}"}])
        actions.submit(self.d)
        self.assertEqual(events(self.d, "submitted")[-1]["times"], 1)

    def test_prompt_qc_event_names_failed_dimensions(self):
        self.saved_prompt()
        fake.QC_FAIL.update({"No Formatting Tells", "Task Scope"})
        self.qc()
        ev = events(self.d, "prompt_qc")[-1]
        self.assertEqual((ev["prompt"], ev["failed"]), ("fail", ["No Formatting Tells"]))


class KitHook(GateBase):
    def kit(self, write):
        mod = types.ModuleType("johto.kit")
        mod.write = write
        return mock.patch.dict(sys.modules, {"johto.kit": mod})

    def run_dir(self) -> str:
        run_dir = fake_lanes.new_lane(self.d, "validate")
        fx.fake_claude(run_dir, fx.PROMPT)
        return run_dir.name

    def test_no_kit_module_is_quiet(self):
        info = actions.collect(self.d, self.run_dir(), log=self.log)
        self.assertIsNone(info["kit"])
        self.assertEqual(self.lines, [])
        self.assertEqual(events(self.d, "kit_written"), [])

    def test_kit_is_written_after_collect(self):
        calls = []

        def write(d, run):
            calls.append(run)
            (d / "kit.md").write_text("kit")
            return d / "kit.md"
        with self.kit(write):
            info = actions.collect(self.d, self.run_dir(), log=self.log)
        self.assertEqual(calls, [None])   # the submission run, whichever run was collected
        self.assertEqual(info["kit"], str(self.d / "kit.md"))
        self.assertEqual(events(self.d, "kit_written")[-1]["run"], "validate-1")

    def test_a_failing_kit_warns_once_and_the_collect_succeeds(self):
        with self.kit(mock.Mock(side_effect=RuntimeError("no grading yet"))):
            info = actions.collect(self.d, self.run_dir(), log=self.log)
        self.assertEqual(info["run"], "validate-1")
        self.assertTrue(actions.run_record(self.d, "validate-1").get("collected_at"))
        self.assertEqual(self.lines, ["warning: writing the kit for validate-1 failed: no grading yet"])
        self.assertEqual(events(self.d, "kit_written"), [])

    def test_the_real_collect_runs_the_hook_once(self):
        """lanes.collect (also used by the lane watcher) runs it; actions.collect then doesn't again."""
        run = self.run_dir()
        calls = []
        with self.kit(lambda d, r: calls.append(r)), mock.patch.object(actions, "L", lambda: lanes), \
                mock.patch.object(lanes, "_prework_collect", lambda *a: None):
            actions.collect(self.d, run, log=self.log)
        self.assertEqual(calls, [None])   # once, for the submission run
        self.assertEqual(len(events(self.d, "collected")), 1)


class SavedFieldsCheck(GateBase):
    def test_check_appends_the_saved_text_flags(self):
        flag = (lint.WARN, "Validation Notes", "Studio's copy ends mid-word")
        with mock.patch.object(lint, "check_saved_fields", create=True, new=lambda d: [flag]):
            self.assertIn(flag, actions.check(self.d))
        with mock.patch.object(lint, "check_saved_fields", create=True, new=mock.Mock(side_effect=ValueError("bad"))):
            self.assertIn((lint.WARN, "Studio", "the saved-text check didn't run: bad"), actions.check(self.d))


class Cli(GateBase):
    def sub(self):
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="cmd")
        sub.add_parser("lane")
        return p, sub

    def module(self, name, register=None):
        mod = types.ModuleType(f"johto.{name}")
        if register:
            mod.register = register
        return mod

    def test_register_modules_skips_missing_ones_and_taken_names(self):
        p, sub = self.sub()
        mods = {"johto.budget": self.module("budget", lambda s: s.add_parser("time")),
                "johto.reviews": self.module("reviews", lambda s: s.add_parser("lane")),
                "johto.deck": self.module("deck"), "johto.where": None}
        err = io.StringIO()
        with mock.patch.dict(sys.modules, mods), contextlib.redirect_stderr(err):
            done = cli.register_modules(sub, ("budget", "reviews", "deck", "where"))
        self.assertEqual(done, ["budget"])
        self.assertIn("time", sub.choices)
        self.assertIn("reviews wants a command name that is already taken", err.getvalue())

    def test_a_broken_module_is_not_hidden(self):
        _, sub = self.sub()
        bad = self.module("kit")
        with mock.patch.dict(sys.modules, {"johto.kit": bad}), \
                mock.patch.object(cli.importlib, "import_module",
                                  mock.Mock(side_effect=ModuleNotFoundError("no x", name="johto.grading_x"))):
            with self.assertRaises(ModuleNotFoundError):
                cli.register_modules(sub, ("kit",))

    def test_lane_flags(self):
        set_prompt(self.d, fx.PROMPT)
        out = io.StringIO()
        with mock.patch.object(lanes, "copy_to_clipboard", lambda t: True), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as e:
                cli.main(["lane", "fix0001", "validate", "--no-go"])
            self.assertIn("--qc", str(e.exception.code))
            cli.main(["lane", "fix0001", "validate", "--no-go", "--qc"])
        self.assertIn("Lane ready", out.getvalue())
        self.assertIn("The prompt passed AutoQC.", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["lane", "fix0001", "validate", "--qc", "--skip-qc"])


if __name__ == "__main__":
    unittest.main()
