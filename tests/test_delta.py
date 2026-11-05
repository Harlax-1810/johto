"""Follow-up review packs (johto/delta.py) and pack records: what a follow-up holds (with the reply format and
the findings so far), the interdiff, the token cap, next_pack's choice (and its outside-review setting),
checkpoints (packed, reviewed, diff changed), backfill from older pack files, and condense(since=...). Also
pack.py's own pieces that need no repomix: the checkout's checks and PARTIAL report, the guide, and a build
cleaning only its own work folder.

Everything runs in a temp JOHTO_ROOT with a local git repo standing in for GitHub: no network, no lanes, no
repomix, no paid review.

Run: .venv/bin/python -m unittest tests.test_delta -v
"""
from __future__ import annotations

import calendar
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from johto import config, delta, pack, stages

FULL = "acme/widget"
OPENING = "The widget sync job keeps retrying failed uploads forever. Can you look into why the backoff never kicks in?"
FOLLOW = "What about the requeue path, does it reset the attempts too?"
BASE_FILES = {"queue/queue.go": "package queue\n\nfunc Requeue() {\n\tattempts = 0\n}\n",
              "worker/run.go": "package worker\n\nfunc Run() {}\n"}


def _git(cwd: Path, *args) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def make_origin(root: Path) -> str:
    """A local repo standing in for github.com/acme/widget; returns the pinned commit."""
    repo = root / "origin" / FULL
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    for rel, text in BASE_FILES.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "config", "uploadpack.allowFilter", "true")
    _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    return _git(repo, "rev-parse", "HEAD")


def user(ts: str, text: str) -> dict:
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def claude(ts: str, text: str = "", tool: dict | None = None, stop: str | None = None) -> dict:
    content = [{"type": "text", "text": text}] if text else []
    if tool:
        content.append({"type": "tool_use", **tool})
    return {"type": "assistant", "timestamp": ts,
            "message": {"model": "claude-opus-5-5", "stop_reason": stop, "content": content}}


def result(ts: str, tool_id: str, text: str) -> dict:
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}]}}


TURN1 = [user("2026-10-30T10:00:00Z", OPENING),
         claude("2026-10-30T10:01:00Z", "Reading the queue.", {"id": "t1", "name": "Bash", "input": {"command": "cat queue/queue.go"}}),
         result("2026-10-30T10:01:05Z", "t1", "attempts = 0"),
         claude("2026-10-30T10:05:00Z", "", {"id": "t2", "name": "Edit", "input": {"file_path": "/exp/x/model_a/queue/queue.go",
                                                                                   "new_string": "attempts++"}}),
         result("2026-10-30T10:05:01Z", "t2", "ok"),
         claude("2026-10-30T10:10:00Z", "Found it: Requeue resets the counter.", stop="end_turn"),
         {"type": "system", "subtype": "turn_duration", "durationMs": 600000, "timestamp": "2026-10-30T10:10:01Z"}]
COMPACT = [{"type": "user", "isCompactSummary": True, "timestamp": "2026-10-30T10:20:00Z",
            "message": {"role": "user", "content": "This session is being continued from a previous conversation. " * 200}}]
TURN2 = [user("2026-10-30T10:30:00Z", FOLLOW),
         claude("2026-10-30T10:31:00Z", "", {"id": "t3", "name": "Edit", "input": {"file_path": "/exp/x/model_a/worker/run.go",
                                                                                   "new_string": "retry()"}}),
         result("2026-10-30T10:31:01Z", "t3", "ok"),
         claude("2026-10-30T10:40:00Z", "Fixed the requeue path too.", stop="end_turn")]

DIFF1 = ("diff --git a/queue/queue.go b/queue/queue.go\n--- a/queue/queue.go\n+++ b/queue/queue.go\n"
         "@@ -2,4 +2,4 @@\n \n func Requeue() {\n-\tattempts = 0\n+\tattempts++\n }\n")
DIFF2 = DIFF1 + ("diff --git a/worker/run.go b/worker/run.go\n--- a/worker/run.go\n+++ b/worker/run.go\n"
                 "@@ -1,3 +1,3 @@\n package worker\n \n-func Run() {}\n+func Run() { retry() }\n")


class Fixture(unittest.TestCase):
    """A temp JOHTO_ROOT with one task (t1, run validate-1), a local origin repo and no lanes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.commit = make_origin(self.root)
        self.patches = [mock.patch.object(config, "ROOT", self.root), mock.patch.object(config, "DATA", self.root / "data"),
                        mock.patch.object(config, "TASKS", self.root / "tasks"),
                        mock.patch.object(pack, "GIT_URL", f"file://{self.root}/origin/{{}}"),
                        mock.patch.object(pack, "_live_transcript", lambda run_dir: None),
                        mock.patch.object(pack, "_live_diff", lambda run_dir: None)]
        for p in self.patches:
            p.start()
        self.d = self.root / "tasks" / "t1"
        self.run_dir = self.d / "runs" / "validate-1"
        (self.run_dir / "prework_output").mkdir(parents=True)
        (self.root / "data").mkdir()
        meta = {"task_id": "task_t1", "task_name": "Task t1",
                "slot": {"action": "Modify", "domain": "Backend / API", "codebase_type": "Personal-scale",
                         "prompt_style": "Casual prose", "interaction_mode": "Interactive/pairing"},
                "repo": {"full_name": FULL, "commit": self.commit, "language": "Go",
                         "pinned_url": f"https://github.com/{FULL}/tree/{self.commit}"},
                "runs": [{"kind": "validate", "dir": "runs/validate-1"}]}
        (self.d / "task.json").write_text(json.dumps(meta))
        (self.d / "submission.md").write_text(f"## Opening Prompt\n\n{OPENING}\n")
        self.tpath = self.run_dir / "prework_output" / "session.jsonl"
        self.write_run(TURN1, DIFF1)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def write_run(self, entries: list[dict], diff: str) -> None:
        self.tpath.write_text("".join(json.dumps(e) + "\n" for e in entries))
        (self.run_dir / "prework_output" / "final.diff").write_text(diff)

    def full_pack(self, name: str = "review-pack-20261030-101500.xml") -> Path:
        """Stand-in for pack.build (repomix and the outside review are not under test): a file plus its record."""
        out = self.run_dir / name
        out.write_text("full pack")
        diff = (self.run_dir / "prework_output" / "final.diff").read_text()
        st = delta.state(self.tpath, diff)
        delta.record_pack(self.d, "validate-1", out, "full", st.pop("turn"), diff=diff, **st)
        return out


class Records(Fixture):
    def test_record_saves_the_diff_and_logs(self):
        out = self.full_pack()
        recs = delta.packs(self.d, "validate-1")
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual((r["kind"], r["turn"], r["path"]), ("full", 1, str(out)))
        self.assertEqual(Path(r["diff_path"]).read_text(), DIFF1)
        self.assertEqual(r["last_entry_at"], "2026-10-30T10:10:00Z")
        stored = json.loads((self.run_dir / "packs.json").read_text())[0]
        self.assertEqual(stored["path"], out.name)   # relative, so a copied task folder still works
        ev = stages.last(self.d, "pack_built")
        self.assertEqual((ev["run"], ev["pack_kind"], ev["turn"]), ("validate-1", "full", 1))

    def test_a_full_pack_build_records_itself(self):
        """pack._build reads how far the pack reaches before condensing, then records it with its diff."""
        out = self.run_dir / "review-pack-20261030-101500.xml"
        out.write_text("full pack")
        covered = pack._covered(self.tpath, DIFF1)
        self.write_run(TURN1 + TURN2, DIFF2)   # the run moves on while the pack is being built
        pack._record(self.d, "validate-1", out, covered, DIFF1, 1234, log=self.fail, outside_review=False)
        r = delta.packs(self.d, "validate-1")[0]
        self.assertEqual((r["turn"], r["tokens"], r["outside_review"], r["last_entry_at"]),
                         (1, 1234, False, "2026-10-30T10:10:00Z"))

    def test_compaction_and_local_commands_are_not_new_material(self):
        before = delta.state(self.tpath, DIFF1)
        exit_cmd = user("2026-10-30T10:50:00Z", "<command-name>/exit</command-name>")
        self.write_run(TURN1 + COMPACT + [exit_cmd], DIFF1)
        after = delta.state(self.tpath, DIFF1)
        self.assertEqual(before, after)

    def test_backfill_from_older_pack_files(self):
        # File stamps are the Mac's local time: make one for 10:15 UTC, between the two turns.
        local = time.strftime("%Y%m%d-%H%M%S", time.localtime(calendar.timegm((2026, 10, 30, 10, 15, 0))))
        xml = delta.repomix_xml([("1-review/00-grading-guide.md", "guide"), ("1-review/03-final.diff", DIFF1)], "hdr")
        (self.run_dir / f"review-pack-{local}.xml").write_text(xml)
        (self.run_dir / f"review-followup-{local[:-2]}59.xml").write_text("hand-made")
        self.write_run(TURN1 + COMPACT + TURN2, DIFF2)
        recs = delta.packs(self.d, "validate-1")
        self.assertEqual([r["kind"] for r in recs], ["full", "followup"])
        full = recs[0]
        self.assertEqual((full["turn"], full["last_entry_at"], full["backfilled"]), (1, "2026-10-30T10:10:00Z", True))
        self.assertEqual(Path(full["diff_path"]).read_text().rstrip(), DIFF1.rstrip())
        self.assertIsNone(recs[1].get("diff_path"))
        self.assertIsNone(stages.last(self.d, "pack_built"))   # backfill isn't a new build
        self.assertEqual(delta.backfill(self.d, "validate-1"), [])   # once only


class Condense(Fixture):
    def test_since_keeps_only_later_entries_and_numbers_turns(self):
        self.write_run(TURN1 + COMPACT + TURN2, DIFF2)
        md, stats = pack.condense(self.tpath, since="2026-10-30T10:10:00Z", brief_compactions=True)
        self.assertNotIn(OPENING, md)
        self.assertIn("## Human, follow-up 1 (10:30:00)", md)
        self.assertIn("compacted its context at 10:20:00", md)
        self.assertNotIn("being continued", md)
        self.assertEqual(stats["turns"], 2)

    def test_a_turn_under_way_gets_a_continuing_heading(self):
        md, _ = pack.condense(self.tpath, since="2026-10-30T10:01:05Z")
        self.assertTrue(md.lstrip().startswith("## Continuing the opening prompt, after the last pack"))
        self.assertIn("`Edit` /exp/x/model_a/queue/queue.go", md)
        self.assertNotIn("cat queue/queue.go", md)

    def test_no_since_is_unchanged(self):
        md, stats = pack.condense(self.tpath)
        self.assertIn("## Human, opening prompt (10:00:00)", md)
        self.assertEqual(stats["turns"], 1)


class Followup(Fixture):
    def test_holds_new_turns_interdiff_and_current_files(self):
        first = self.full_pack()
        self.write_run(TURN1 + COMPACT + TURN2, DIFF2)
        out = delta.build_followup(self.d, "validate-1", log=lambda *_: None)
        self.assertTrue(out.name.startswith("review-followup-"))
        x = out.read_text()
        readme = delta.xml_file(x, "1-review/00-read-me.md")
        self.assertIn("continues your review of turn 1", readme)
        self.assertIn(f'**Follow-up 1** (10:30:00 UTC; Claude worked', readme)
        self.assertIn(FOLLOW, readme)
        t = delta.xml_file(x, "1-review/01-transcript.md")
        self.assertIn("Fixed the requeue path too.", t)
        self.assertNotIn("Reading the queue.", t)          # turn 1 was in the first pack
        inter = delta.xml_file(x, "1-review/02-interdiff.diff")
        self.assertIn("+func Run() { retry() }", inter)
        self.assertNotIn("attempts++", inter.split("\n\n", 1)[1])   # unchanged since the first pack
        self.assertIn("1 file changed", inter)
        self.assertEqual(delta.xml_file(x, "2-code/worker/run.go"), "package worker\n\nfunc Run() { retry() }")
        self.assertIsNone(delta.xml_file(x, "2-code/queue/queue.go"))
        self.assertTrue(x.index("1-review/00-read-me.md") < x.index("2-code/worker/run.go"))
        rec = delta.packs(self.d, "validate-1")[-1]
        self.assertEqual((rec["kind"], rec["turn"], rec["base"], rec["interdiff"]), ("followup", 2, first.name, "trees"))
        self.assertTrue((self.root / "data" / "cache" / "repos" / "acme__widget.git").is_dir())

    def test_falls_back_to_comparing_diffs(self):
        self.full_pack()
        self.write_run(TURN1 + TURN2, DIFF2)
        with mock.patch.object(pack, "GIT_URL", f"file://{self.root}/missing/{{}}"):
            out = delta.build_followup(self.d, "validate-1", log=lambda *_: None)
        x = out.read_text()
        inter = delta.xml_file(x, "1-review/02-interdiff.diff")
        self.assertIn("+now/worker/run.go", inter.replace("+++ ", "+"))
        self.assertIn("++func Run() { retry() }", inter)
        self.assertIn("compared file by file", delta.xml_file(x, "1-review/00-read-me.md"))
        self.assertEqual(delta.packs(self.d, "validate-1")[-1]["interdiff"], "diffs")

    def test_base_is_the_pack_the_reviewer_saw(self):
        first = self.full_pack()
        mid = TURN1 + [user("2026-10-30T10:20:00Z", "And the worker?"), claude("2026-10-30T10:25:00Z", "Looked.", stop="end_turn")]
        self.write_run(mid, DIFF1)
        delta.build_followup(self.d, "validate-1", log=lambda *_: None)   # built, never sent
        self.write_run(mid + TURN2, DIFF2)
        delta.update_pack(self.d, "validate-1", first, sent_at="2026-10-30T10:16:00-0700")
        out = delta.build_followup(self.d, "validate-1", log=lambda *_: None)
        t = delta.xml_file(out.read_text(), "1-review/01-transcript.md")
        self.assertIn("And the worker?", t)               # the unsent follow-up's turn is repeated
        self.assertEqual(delta.packs(self.d, "validate-1")[-1]["base"], first.name)

    def test_token_cap(self):
        self.full_pack()
        long = [claude(f"2026-10-30T10:3{i % 10}:{i // 10:02d}Z", "word " * 400) for i in range(1, 200)]
        self.write_run(TURN1 + TURN2[:1] + long + TURN2[1:], DIFF2)
        out = delta.build_followup(self.d, "validate-1", log=lambda *_: None, max_tokens=12_000)
        self.assertLessEqual(pack._tokens(out.read_text()), 12_000)
        self.assertIn("cut to fit", out.read_text())

    def test_nothing_new_is_refused(self):
        self.full_pack()
        with self.assertRaises(SystemExit):
            delta.build_followup(self.d, "validate-1", log=lambda *_: None)

    def test_asks_for_the_reply_format_and_lists_findings_so_far(self):
        from johto import reviews
        self.full_pack()
        reviews.save(self.d, "validate-1", 1, '## Digest\n\nx\n\n```findings\n[{"id": "F1", "finding": "Requeue resets '
                     'attempts.", "kind": "defect", "severity": "high", "status": "new"}, {"id": "F2", "finding": '
                     '"Reads the queue first.", "kind": "credit", "severity": "low", "status": "new"}]\n```\n')
        self.write_run(TURN1 + TURN2, DIFF2)
        readme = delta.xml_file(delta.build_followup(self.d, "validate-1", log=lambda *_: None).read_text(),
                                "1-review/00-read-me.md")
        self.assertIn("## Your findings so far", readme)
        self.assertIn("- **F1** (defect, high, new): Requeue resets attempts.", readme)
        self.assertIn("number new findings from F3", readme)
        self.assertIn("## Reply format", readme)
        self.assertIn("```findings", readme)
        self.assertIn("IDs stay the same for the whole chat", readme)
        self.assertIn("Don't suggest replies or prompts to send to Claude", readme)
        self.assertNotIn("Same rules and output format", readme)

    def test_without_findings_on_record_it_numbers_from_f1(self):
        self.full_pack()
        self.write_run(TURN1 + TURN2, DIFF2)
        readme = delta.xml_file(delta.build_followup(self.d, "validate-1", log=lambda *_: None).read_text(),
                                "1-review/00-read-me.md")
        self.assertIn("number this reply's findings from F1", readme)

    def test_needs_an_earlier_pack(self):
        with self.assertRaises(SystemExit) as e:
            delta.build_followup(self.d, "validate-1", log=lambda *_: None)
        self.assertIn("--full", str(e.exception))


class Checkpoints(Fixture):
    def test_checkpoint_packed_follows_the_transcript_not_the_diff(self):
        first = self.full_pack()
        self.assertEqual(delta.checkpoint_packed(self.d, "validate-1")["path"], str(first))
        (self.run_dir / "prework_output" / "final.diff").write_text(DIFF1 + "\n")   # collect rewrites the diff
        self.assertEqual(delta.checkpoint_packed(self.d, "validate-1")["path"], str(first))
        self.write_run(TURN1 + TURN2, DIFF2)
        self.assertIsNone(delta.checkpoint_packed(self.d, "validate-1"))
        first.unlink()
        self.write_run(TURN1, DIFF1)
        self.assertIsNone(delta.checkpoint_packed(self.d, "validate-1"))     # its file is gone

    def test_diff_changed_since_the_pack_gpt_saw(self):
        first = self.full_pack()
        self.assertIsNone(delta.reviewed_checkpoint(self.d, "validate-1"))
        self.assertIsNone(delta.diff_changed(self.d, "validate-1"))          # GPT hasn't seen one yet
        delta.update_pack(self.d, "validate-1", first, sent_at="2026-10-30T10:16:00-0700")
        self.assertEqual(delta.reviewed_checkpoint(self.d, "validate-1")["path"], str(first))
        self.assertFalse(delta.diff_changed(self.d, "validate-1"))
        live = DIFF1.replace("--- a/queue/queue.go", "index 1111111..2222222 100644\n--- a/queue/queue.go")
        self.assertFalse(delta.diff_changed(self.d, "validate-1", live))     # the live diff's extra lines don't count
        self.assertTrue(delta.diff_changed(self.d, "validate-1", DIFF2))
        self.write_run(TURN1 + TURN2, DIFF2)
        self.assertTrue(delta.diff_changed(self.d, "validate-1"))


class PackPieces(Fixture):
    """pack.py without repomix or the outside review."""

    def meta(self, commit=None):
        return {"repo": {"full_name": FULL, "commit": commit or self.commit}}

    def test_checkout_applies_the_diff(self):
        dest, note, partial = pack.checkout(self.meta(), DIFF2, self.root / "work" / "repo")
        self.assertEqual((note, partial), ("applied", None))
        self.assertIn("retry()", (dest / "worker/run.go").read_text())

    def test_a_task_review_context_goes_into_the_task_file(self):
        (self.d / "review-context.md").write_text("# History for the reviewer\n")
        seen = {}

        def fake_run(cmd, cwd=None, timeout=None, input=None):
            seen["task"] = (Path(cmd[len(pack.REPOMIX)]) / "1-review" / "01-task.md").read_text()
            Path(cmd[cmd.index("--output") + 1]).write_text("pack")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        slot = dict.fromkeys(("action", "domain", "codebase_type", "prompt_style", "interaction_mode"), "x")
        g = {"meta": {"task_name": "t", "slot": slot, "repo": {"full_name": FULL, "commit": self.commit,
                                                               "pinned_url": "u", "language": "Go"}},
             "run_dir": self.run_dir, "sub": {"sections": {}}, "live": False, "diff": DIFF1,
             "transcript_path": self.tpath}
        clone = self.root / "work" / "repo"
        clone.mkdir(parents=True)
        with mock.patch.object(pack, "gather", return_value=g), mock.patch.object(pack, "_run", fake_run), \
                mock.patch.object(pack, "checkout", return_value=(clone, "applied", None)):
            pack._build(self.d, "validate-1", 200_000, False, lambda *_: None, self.root / "work")
        self.assertIn("# History for the reviewer", seen["task"])

    def test_files_read_through_bash_count_as_mentioned(self):
        clone = self.root / "work" / "repo"
        (clone / "worker").mkdir(parents=True)
        (clone / "worker/run.go").write_text("x")
        (clone / "worker/other.go").write_text("x")
        text = ("sed -n 1,9p worker/other.go\nsed -n 1,9p /exp/t/model_a/worker/run.go\ngrep y worker/run.go:12\n"
                "cat /tmp/scratchpad/nope.py\n")
        self.assertEqual(pack._mentioned(text, clone), ["worker/run.go", "worker/other.go"])
        (clone / "tests").mkdir()
        (clone / "tests/run_test.go").write_text("x")
        text = "tests/run_test.go tests/run_test.go tests/run_test.go worker/other.go"
        self.assertEqual(pack._mentioned(text, clone), ["worker/other.go", "tests/run_test.go"])

    def test_rejected_hunks_mark_the_checkout_partial(self):
        bad = DIFF2.replace("-func Run() {}", "-func Run() { gone() }")
        missing = ("diff --git a/nope.go b/nope.go\n--- a/nope.go\n+++ b/nope.go\n@@ -1 +1 @@\n-a\n+b\n")
        dest, note, partial = pack.checkout(self.meta(), bad + missing, self.root / "work" / "repo")
        self.assertEqual(partial, "PARTIAL: 2 hunks rejected in nope.go, worker/run.go")
        self.assertEqual(note, partial)
        self.assertIn("attempts++", (dest / "queue/queue.go").read_text())   # the rest still applied

    def test_a_failed_clone_or_checkout_stops_the_build(self):
        with mock.patch.object(pack, "GIT_URL", f"file://{self.root}/missing/{{}}"), self.assertRaises(SystemExit) as e:
            pack.checkout(self.meta(), DIFF1, self.root / "work" / "repo")
        self.assertIn("Couldn't clone acme/widget", str(e.exception))
        with self.assertRaises(SystemExit) as e:
            pack.checkout(self.meta("0" * 40), DIFF1, self.root / "work" / "repo2")
        self.assertIn("Couldn't check out acme/widget at 000000000000", str(e.exception))

    def test_guide_puts_the_outside_review_last_and_flags_partial(self):
        g = pack.guide(True, "PARTIAL: 1 hunk rejected in a.go")
        self.assertTrue(g.splitlines()[2].startswith("> **PARTIAL: 1 hunk rejected in a.go.**"))
        self.assertIn("3-outside-review/candidate-claims.md", g)
        self.assertIn("candidates to check, not evidence", g)
        self.assertLess(g.index("4. `2-code/`"), g.index("5. Last, `3-outside-review"))
        self.assertIn("## Digest", g)
        self.assertIn('"basis": "executed|isolated|traced|claimed"', g)
        plain = pack.guide(False)
        self.assertNotIn("candidate-claims", plain)
        self.assertNotIn("PARTIAL", plain)

    def test_build_removes_only_its_own_work_folder(self):
        packs = self.root / "data" / "cache" / "packs"
        other = packs / "t1-validate-1-someone-else"
        seen = []

        def fake_build(task_dir, run_name, max_tokens, ai, log, work):
            seen.append(work)
            other.mkdir(parents=True)        # another build starts meanwhile
            (work / "pack").mkdir()
            raise SystemExit("stop here")
        with mock.patch.object(pack, "PACKS", packs), mock.patch.object(pack, "_build", fake_build):
            with self.assertRaises(SystemExit):
                pack.build(self.d, "validate-1", log=lambda *_: None)
        self.assertEqual(seen[0].parent, packs)
        self.assertFalse(seen[0].exists())
        self.assertTrue(other.is_dir())


class NextPack(Fixture):
    def setUp(self):
        super().setUp()
        self.builds = []

        def fake_build(task_dir, run_name=None, max_tokens=pack.MAX_TOKENS, ai=True, log=print):
            self.builds.append(ai)
            return self.full_pack(f"review-pack-2026103{len(self.builds)}-101500.xml")
        self.p = mock.patch.object(pack, "build", fake_build)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        super().tearDown()

    def test_full_then_existing_then_followup(self):
        quiet = lambda *_: None   # noqa: E731
        first = delta.next_pack(self.d, "validate-1", log=quiet, ai=False)
        self.assertEqual(self.builds, [False])
        self.assertEqual(delta.next_pack(self.d, "validate-1", log=quiet), first)     # nothing changed
        self.write_run(TURN1 + TURN2, DIFF2)
        follow = delta.next_pack(self.d, "validate-1", log=quiet)
        self.assertTrue(follow.name.startswith("review-followup-"))
        self.assertEqual(delta.next_pack(self.d, "validate-1", log=quiet), follow)    # still nothing new
        full = delta.next_pack(self.d, "validate-1", log=quiet, full=True, ai=False)
        self.assertTrue(full.name.startswith("review-pack-"))
        self.assertEqual(len(self.builds), 2)
        self.assertEqual(delta.next_pack(self.d, "validate-1", log=quiet, full=True), full)

    def test_outside_review_only_when_the_setting_is_on(self):
        quiet = lambda *_: None   # noqa: E731
        with mock.patch.object(config, "FAKE", False):
            delta.next_pack(self.d, "validate-1", log=quiet)
            (self.root / "data" / "settings.json").write_text(json.dumps({"pack_outside_review": True}))
            self.write_run(TURN1 + TURN2, DIFF2)
            delta.next_pack(self.d, "validate-1", log=quiet, full=True)
        self.assertEqual(self.builds, [False, True])


if __name__ == "__main__":
    unittest.main()


class Gather(Fixture):
    def test_a_stopped_uncollected_lane_is_refused_not_packed_as_no_changes(self):
        """t4sk0004 validate-2 on the PC: transcript readable, diff not. No pack of an unknown tree."""
        (self.run_dir / "prework_output" / "final.diff").unlink()
        with mock.patch.object(pack, "_live_transcript", lambda run_dir: self.tpath), \
                mock.patch.object(pack, "_live_diff", lambda run_dir: None):
            with self.assertRaisesRegex(SystemExit, "validate-1's diff can't be read.*johto collect t1 validate-1"):
                pack.gather(self.d, "validate-1")

    def test_a_collected_run_uses_what_was_collected(self):
        """Turns typed into the lane after the collect aren't in the submission, so they aren't in the pack."""
        m = json.loads((self.d / "task.json").read_text())
        m["runs"][0]["collected_at"] = "2026-10-30T11:00:00-0700"
        (self.d / "task.json").write_text(json.dumps(m))
        later = self.root / "later.jsonl"
        later.write_text("".join(json.dumps(e) + "\n" for e in TURN1 + [user("2026-10-31T07:31:31Z", "hello")]))
        asked = []
        with mock.patch.object(pack, "_live_transcript", lambda run_dir: asked.append("t") or later), \
                mock.patch.object(pack, "_live_diff", lambda run_dir: asked.append("d") or "live diff"):
            g = pack.gather(self.d, "validate-1")
            self.assertEqual((g["transcript_path"], g["diff"], g["live"], asked), (self.tpath, DIFF1, False, []))
            g = pack.gather(self.d, "validate-1", live=True)   # on purpose
            self.assertEqual((g["transcript_path"], g["diff"]), (later, "live diff"))

    def test_fake_mode_never_reaches_a_lane(self):
        asked = []
        with mock.patch.object(config, "FAKE", True), \
                mock.patch.object(pack, "_live_transcript", lambda run_dir: asked.append("t")), \
                mock.patch.object(pack, "_live_diff", lambda run_dir: asked.append("d")):
            g = pack.gather(self.d, "validate-1")
            delta.current_diff(self.d, "validate-1")
        self.assertEqual((asked, g["diff"]), ([], DIFF1))


class NextPackRace(Fixture):
    def test_two_callers_at_once_build_one_pack(self):
        """The web lane watcher and `johto watch` asking for the next pack of one checkpoint get the same pack."""
        import threading
        built = []

        def slow_build(task_dir, run, ai=False, log=print):
            time.sleep(0.4)
            out = self.run_dir / f"review-pack-20261030-1015{len(built):02d}.xml"
            out.write_text("full pack")
            built.append(out.name)
            diff = (self.run_dir / "prework_output" / "final.diff").read_text()
            st = delta.state(self.tpath, diff)
            delta.record_pack(self.d, "validate-1", out, "full", st.pop("turn"), diff=diff, **st)
            return out
        got = []
        with mock.patch.object(pack, "build", slow_build):
            ts = [threading.Thread(target=lambda: got.append(delta.next_pack(self.d, "validate-1", log=lambda *a: None)))
                  for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(len(built), 1, built)
        self.assertEqual(got[0], got[1])
