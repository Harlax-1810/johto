"""The grading table (johto/grading.py): parsing GPT Pro's findings block (good, missing, malformed), one row per
finding across follow-ups in a chat, dedupe across sources, Austin's marks surviving refresh, dossier hazards as
unscoped candidates, run-fact claim rows, the selected run, the `johto grading` command, extraction and verify
with fake Claude runners.

Everything runs in a temp JOHTO_ROOT with a local git repo standing in for GitHub (tests/test_delta.py's
fixture): no network, no lanes, no paid Claude call.

Run: .venv/bin/python -m unittest tests.test_grading -v
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from test_delta import DIFF1, OPENING, TURN1, Fixture, claude, result, user  # noqa: E402

from johto import actions, grading, reviews, stages  # noqa: E402
from johto.dossier import store  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "grading"
CHAT = "https://chatgpt.com/c/chat-1"


def block(findings: list[dict], info: str = "findings") -> str:
    return f"```{info}\n{json.dumps(findings, indent=1)}\n```\n"


def finding(fid: str, text: str, **kw) -> dict:
    base = {"id": fid, "finding": text, "kind": "defect", "status": "new", "severity": "medium", "result": "missed",
            "basis": "traced", "refs": ["queue/queue.go:4"], "limits": "", "supersedes": None}
    return {**base, **kw}


REPLY1 = ("## Digest\n\nFirst review.\n\n## Findings\n\n| ID | Finding |\n|---|---|\n| F1 | x |\n\n## Detail\n\nText.\n\n"
          + block([finding("F1", "Requeue resets the attempts counter to zero, so backoff never grows.", severity="high"),
                   finding("F2", "The worker never reads the retry limit from config.", refs=["worker/run.go:3"]),
                   finding("F3", "Claude found the counter reset at the right line.", kind="credit", result="credit")]))


class PackAsks(unittest.TestCase):
    def test_the_pack_asks_for_code_facts_not_field_shaped_sentences(self):
        from johto import pack
        guide = pack.GUIDE.lower()
        self.assertNotIn("strong senior engineer", guide)
        self.assertNotIn("what the task needs", guide)
        self.assertIn("what did the run miss or get wrong, with file:line?", " ".join(guide.split()))
        self.assertIn("don't frame findings as what a strong or weak model", pack.RULES.lower())
        self.assertIn("defect|credit|behavior|claim|limitation", pack.REPLY_FORMAT)

    def test_a_behavior_finding_is_read(self):
        self.assertEqual(grading.normalize({"id": "F2", "finding": "The due date is dropped.", "kind": "behavior"})["kind"],
                         "requirement")


class Cut(unittest.TestCase):
    def test_cut_at_a_word_boundary(self):
        self.assertEqual(grading.cut("Shows the combined labels, not removal of the older ones", 45),
                         "Shows the combined labels, not removal of…")
        self.assertEqual(grading.cut("short", 45), "short")
        self.assertEqual(grading.cut("a" * 60, 10), "a" * 9 + "…")   # one long word is cut where it must be


class Base(Fixture):
    def setUp(self):
        super().setUp()
        self.store = mock.patch.object(store, "P", store.Paths(self.root / "data"))
        self.store.start()
        self.log: list[str] = []

    def tearDown(self):
        self.store.stop()
        super().tearDown()

    def say(self, *a):
        self.log.append(" ".join(map(str, a)))

    def review(self, text: str, turn: int = 1, chat: str | None = CHAT, run: str = "validate-1") -> Path:
        return reviews.save(self.d, run, turn, text, chat_url=chat)

    def refresh(self, **kw) -> dict:
        return grading.refresh(self.d, log=self.say, **kw)

    def rows(self) -> dict:
        """Rows after a refresh (a refresh never changes marks, so marking tests can use it too)."""
        self.refresh()
        return {r["id"]: r for r in grading.load(self.d)["rows"]}

    def meta(self, **fields) -> None:
        p = self.d / "task.json"
        m = json.loads(p.read_text())
        m.update(fields)
        p.write_text(json.dumps(m))


# --- parsing -------------------------------------------------------------------------------------------

class Parse(unittest.TestCase):
    def test_good_block(self):
        got = grading.parse_findings(REPLY1)
        self.assertEqual([f["id"] for f in got], ["F1", "F2", "F3"])
        self.assertEqual((got[0]["severity"], got[0]["result"], got[0]["basis"], got[0]["status"]),
                         ("high", "missed", "traced", "new"))
        self.assertEqual(got[2]["kind"], "credit")

    def test_missing_block_is_none(self):
        self.assertIsNone(grading.parse_findings("## Task Success\n\n1. It missed the requeue path.\n"))
        self.assertIsNone(grading.parse_findings(""))

    def test_other_code_blocks_are_not_findings(self):
        text = "```json\n" + json.dumps([finding("F1", "x y z")]) + "\n```\n"
        self.assertIsNone(grading.parse_findings(text))

    def test_malformed_block_keeps_what_reads(self):
        body = ('[\n {"id": "F1", "finding": "Requeue resets attempts.", "severity": "Major",},\n'
                ' {"id": "F2", "finding": broken here},\n'
                ' {"id": "f 3", "finding": "Isolated check of the limit.", "basis": "Isolated execution"},\n]')
        got = grading.parse_findings(f"text\n```findings\n{body}\n```\n")
        self.assertEqual([f["id"] for f in got], ["F1", "F3"])
        self.assertEqual(got[0]["severity"], "high")
        self.assertEqual(got[1]["basis"], "isolated")

    def test_trailing_commas_unclosed_fence_and_bare_objects(self):
        got = grading.parse_findings('```findings\n[{"id": "F1", "finding": "A thing.", "refs": ["a.py:1",],},]\n')
        self.assertEqual(got[0]["refs"], ["a.py:1"])
        got = grading.parse_findings('```findings\n{"id": "F1", "finding": "One."}\n{"id": "F2", "finding": "Two."}\n```')
        self.assertEqual([f["id"] for f in got], ["F1", "F2"])
        got = grading.parse_findings('```findings\n{"findings": [{"id": "F4", "finding": "Wrapped."}]}\n```')
        self.assertEqual(got[0]["id"], "F4")

    def test_last_block_wins(self):
        old = block([finding("F1", "Old.")])
        new = block([finding("F1", "New.", status="resolved")])
        got = grading.parse_findings(f"Quoting the earlier reply:\n{old}\nNow:\n{new}")
        self.assertEqual((got[0]["finding"], got[0]["status"]), ("New.", "resolved"))

    def test_table_when_the_block_is_missing(self):
        text = ("## Findings\n\n| ID | Finding | Status | Severity | Run result | How known | Evidence |\n"
                "|---|---|---|---|---|---|---|\n"
                "| F1 | Requeue resets attempts. | new | high | missed | isolated execution | `queue/queue.go:4` |\n"
                "| F2 | Limit unread. | unchanged | low | partial | source tracing | worker/run.go:3, turn 2 |\n\n## Detail\n")
        got = grading.parse_findings(text)
        self.assertEqual([(f["id"], f["basis"], f["result"]) for f in got],
                         [("F1", "isolated", "missed"), ("F2", "traced", "partial")])
        self.assertEqual(got[1]["refs"], ["worker/run.go:3", "turn 2"])

    def test_follow_up_with_only_id_and_status(self):
        got = grading.parse_findings(block([{"id": "F2", "status": "unchanged"}]))
        self.assertEqual((got[0]["id"], got[0]["finding"], got[0]["status"]), ("F2", None, "unchanged"))

    def test_demo_fixture(self):
        got = grading.parse_findings((FIX / "demo-validate-1-t01-gptpro.md").read_text())
        self.assertEqual(len(got), 12)
        self.assertEqual([got[0]["basis"], got[1]["basis"]], ["isolated", "isolated"])
        self.assertIn("stand-in", got[0]["limits"])
        self.assertNotIn("stand-in", got[1]["limits"])   # one finding's substitution isn't moved to another


# --- rows and identity ---------------------------------------------------------------------------------

class Rows(Base):
    def test_one_row_per_finding_with_sources(self):
        p = self.review(REPLY1)
        data = self.refresh()
        self.assertEqual([r["id"] for r in data["rows"]], ["R1", "R2", "R3"])
        r1 = data["rows"][0]
        self.assertEqual(r1["sources"], [{"type": "review", "ref": f"reviews/{p.name}", "finding_id": "F1",
                                          "at": reviews.list_reviews(self.d)[0]["saved_at"], "chat": CHAT}])
        self.assertEqual((r1["scope"], r1["result"], r1["basis"], r1["review_status"], r1["severity"]),
                         (None, {"run": "validate-1", "turn": 1, "value": "missed"}, "traced", "new", "high"))
        self.assertEqual(r1["marks"], {"selected": False, "goal": False, "dp": None, "notes": False})
        self.assertEqual(r1["verify"]["status"], "none")
        self.assertEqual(data["rows"][2]["result"]["value"], "credit")
        self.assertEqual(stages.last(self.d, "grading_written")["rows"], 3)

    def test_follow_up_updates_the_same_rows(self):
        self.review(REPLY1)
        self.refresh()
        self.review(block([finding("F1", "Requeue now keeps the attempts counter.", status="resolved", result="credit",
                                   basis="isolated", limits="Ran Requeue alone."),
                           {"id": "F2", "status": "unchanged"},
                           finding("F4", "The new retry path has no test.", severity="low")]), turn=2)
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        r1 = rows["R1"]
        self.assertEqual((r1["finding"], r1["review_status"], r1["result"]["value"], r1["result"]["turn"], r1["basis"],
                          r1["limits"]), ("Requeue now keeps the attempts counter.", "resolved", "credit", 2,
                                          "isolated", "Ran Requeue alone."))
        self.assertEqual([s["finding_id"] for s in r1["sources"]], ["F1", "F1"])
        self.assertEqual(len({s["ref"] for s in r1["sources"]}), 2)
        self.review(block([{"id": "F9", "status": "resolved"}]), turn=3)   # no saved review describes F9
        self.assertEqual(len(self.rows()), 4)
        r2 = rows["R2"]   # only a status in the follow-up: everything else carries forward
        self.assertEqual((r2["review_status"], r2["result"]["value"], r2["finding"]),
                         ("unchanged", "missed", "The worker never reads the retry limit from config."))
        self.assertEqual(rows["R4"]["finding"], "The new retry path has no test.")

    def test_withdrawn_stays_and_hides(self):
        self.review(REPLY1)
        self.review(block([{"id": "F2", "status": "withdrawn", "limits": "The limit is read in main.go."}]), turn=2)
        data = self.refresh()
        r2 = next(r for r in data["rows"] if r["id"] == "R2")
        self.assertEqual(r2["review_status"], "withdrawn")
        self.assertNotIn("R2 ", grading.render(data))
        self.assertIn("R2 ", grading.render(data, all=True))
        self.assertIn("1 withdrawn, superseded or from another run): --all shows them", grading.render(data))

    def test_supersedes(self):
        self.review(REPLY1)
        self.review(block([finding("F4", "Requeue resets attempts only on the manual path.", supersedes="F1")]), turn=2)
        rows = self.rows()
        self.assertEqual((rows["R1"]["review_status"], rows["R1"]["superseded_by"]), ("superseded", "R4"))
        self.assertFalse(grading.active(rows["R1"]))

    def test_identity_is_per_chat(self):
        self.review(REPLY1)
        self.review(block([finding("F1", "A different thing in another chat about logging.")]), chat="https://chatgpt.com/c/other")
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows["R4"]["finding"], "A different thing in another chat about logging.")

    def test_near_identical_findings_share_a_row(self):
        self.review(REPLY1)
        self.review(block([finding("F7", "Requeue resets the attempts counter to zero, so the backoff never grows.")]),
                    chat="https://chatgpt.com/c/other")
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(rows["R1"]["sources"]), 2)
        self.assertEqual({s["chat"] for s in rows["R1"]["sources"]}, {CHAT, "https://chatgpt.com/c/other"})

    def test_look_alikes_stay_apart(self):
        slicing = "Batch upload in _split_batch loses the due date when only the due date changes within a loan."
        matching = "Loan matching in _compare_loan_rows ignores the due date when it compares returns."
        self.review(block([finding("F1", slicing, refs=["ledger.py:134"]),
                           finding("F2", matching, refs=["ledger.py:579"])]))
        self.assertEqual(len(self.rows()), 2)
        # Same words, opposite claim, or another file: still separate.
        a = {"finding": "The export keeps every loan across branches.", "kind": "defect", "refs": []}
        self.assertFalse(grading.similar(a, {**a, "finding": "The export does not keep every loan across branches."}))
        self.assertFalse(grading.similar({**a, "refs": ["a.py:1"]}, {**a, "refs": ["b.py:1"]}))
        self.assertFalse(grading.similar(a, {**a, "kind": "credit"}))
        self.assertTrue(grading.similar(a, {**a, "finding": "The export keeps every loan across the branches."}))

    def test_same_review_never_merges(self):
        self.review(block([finding("F1", "Requeue resets the attempts counter."),
                           finding("F2", "Requeue resets the attempts counter!")]))
        self.assertEqual(len(self.rows()), 2)

    def test_refresh_is_idempotent(self):
        self.review(REPLY1)
        self.refresh()
        before = (self.d / "grading.json").read_text()
        n = len(stages.read(self.d, ("grading_written",)))
        self.refresh()
        self.assertEqual((self.d / "grading.json").read_text(), before)
        self.assertEqual(len(stages.read(self.d, ("grading_written",))), n)

    def test_rows_are_never_deleted_or_renumbered(self):
        p = self.review(REPLY1)
        self.refresh()
        p.unlink()
        self.review(block([finding("F9", "Something new about worker shutdown.")]), turn=2, chat="https://chatgpt.com/c/x")
        rows = self.rows()
        self.assertEqual(sorted(rows, key=lambda i: int(i[1:])), ["R1", "R2", "R3", "R4"])
        self.assertEqual(len(rows["R1"]["sources"]), 1)   # its review is gone; the row and its source stay

    def test_missing_block_is_reported_not_guessed(self):
        self.review("## Task Success\n\nIt missed the requeue path.\n")
        data = self.refresh()
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["_missing"], ["validate-1-t01-gptpro.md"])
        self.assertTrue(any("refresh --extract" in line for line in self.log))


class Marks(Base):
    def setUp(self):
        super().setUp()
        self.review(REPLY1)
        self.refresh()

    def test_marks_and_scope_survive_refresh_and_follow_ups(self):
        grading.mark(self.d, ["R1"], goal=True, dp=2, scope="necessary")
        grading.mark(self.d, "R3", notes=True)
        marked = self.rows()["R1"]
        self.review(block([finding("F1", "Requeue still resets attempts on the bulk path.", status="worsened",
                                   severity="high")]), turn=2)
        self.refresh()
        r1, r3 = self.rows()["R1"], self.rows()["R3"]
        self.assertEqual(r1["marks"], {"selected": True, "goal": True, "dp": 2, "notes": False})
        self.assertEqual((r1["scope"], r1["marked_at"]), ("necessary", marked["marked_at"]))
        self.assertEqual(r1["review_status"], "worsened")
        self.assertTrue(r1["changed_since_mark"])
        self.assertTrue(r3["marks"]["notes"])
        self.assertIn("R1", [r["id"] for r in grading.open_decisions(grading.load(self.d))])
        grading.mark(self.d, ["R1"], dp=1)   # Austin looked again
        self.assertFalse(self.rows()["R1"]["changed_since_mark"])
        self.assertNotIn("R1", [r["id"] for r in grading.open_decisions(grading.load(self.d))])

    def test_open_decisions_are_scope_questions_only(self):
        """Credits and claims need no scope call; what the run missed (or a behavior the request may imply) does."""
        self.review(block([finding("F4", "Claude explains the retry math correctly.", kind="credit", result="credit",
                                   severity="high"),
                           finding("F5", "Claude says every gap has a test.", kind="claim", result="unknown",
                                   severity="high"),
                           finding("F6", "The due date is lost at a batch boundary.", kind="behavior", result="missed")]),
                    turn=2)
        data = self.refresh()
        by = {r["finding"]: r["id"] for r in data["rows"]}
        ids = [r["id"] for r in grading.open_decisions(data)]
        self.assertNotIn(by["Claude explains the retry math correctly."], ids)
        self.assertNotIn(by["Claude says every gap has a test."], ids)
        self.assertIn(by["The due date is lost at a batch boundary."], ids)
        self.assertIn("R1", ids)   # a high-severity miss with no scope

    def test_looking_at_another_run_does_not_flag_marks_as_changed(self):
        """`johto grading t refresh --run validate-2`, then a plain refresh: nothing about R1's finding changed."""
        (self.d / "runs" / "validate-2").mkdir(parents=True, exist_ok=True)
        m = json.loads((self.d / "task.json").read_text())
        m["runs"].append({"kind": "validate", "dir": "runs/validate-2", "collected_at": "2026-10-31T10:00:00-0700"})
        (self.d / "task.json").write_text(json.dumps(m))
        grading.mark(self.d, ["R1"], goal=True)
        self.refresh(run="validate-2")
        self.refresh()
        self.assertFalse(self.rows()["R1"].get("changed_since_mark"))

    def test_the_table_says_which_turn_the_review_covers(self):
        self.write_run(TURN1 + [user("2026-10-30T10:30:00Z", "Now write tests that reproduce it."),
                                claude("2026-10-30T10:40:00Z", "Added tests.", stop="end_turn"),
                                user("2026-10-30T10:50:00Z", "And the bulk path?"),
                                claude("2026-10-30T11:00:00Z", "Done.", stop="end_turn")], DIFF1)
        data = self.refresh()
        self.assertEqual(data["coverage"], {"run": "validate-1", "reviewed_turn": 1, "run_turns": 3})
        out = grading.render(data)
        self.assertIn("Results from the review of turn 1 of 3; turns 2-3 not reviewed.", out.splitlines()[1])
        self.assertIn("missed, turn 1", out)

    def test_mark_rules(self):
        changed = grading.mark(self.d, ["r2", "3"], dp=1)
        self.assertEqual([r["id"] for r in changed], ["R2", "R3"])
        self.assertTrue(all(r["marks"]["selected"] for r in changed))   # a decision point selects the row
        self.assertEqual(grading.mark(self.d, ["R2"], dp=1), [])          # no change, nothing returned
        grading.mark(self.d, ["R2"], dp=None)
        self.assertIsNone(self.rows()["R2"]["marks"]["dp"])
        self.assertTrue(self.rows()["R2"]["marks"]["selected"])
        grading.mark(self.d, ["R3"], selected=False)
        self.assertEqual(self.rows()["R3"]["marks"], {"selected": False, "goal": False, "dp": None, "notes": False})
        grading.mark(self.d, ["R1"], scope="outside")
        grading.mark(self.d, ["R1"], scope=None)
        self.assertIsNone(self.rows()["R1"]["scope"])
        for bad in ({"dp": 4}, {"scope": "maybe"}):
            with self.assertRaises(grading.GradingError):
                grading.mark(self.d, ["R1"], **bad)
        with self.assertRaisesRegex(grading.GradingError, "No row R9"):
            grading.mark(self.d, ["R9"], goal=True)
        self.assertEqual(stages.last(self.d, "grading_marked")["rows"], ["R1"])

    def test_used_by(self):
        grading.mark(self.d, ["R1"], goal=True, dp=2)
        grading.mark(self.d, ["R2"], dp=1, notes=True)
        self.assertEqual([r["id"] for r in grading.used_by(self.d, "goal")], ["R1"])
        self.assertEqual([r["id"] for r in grading.used_by(self.d, "Decision Points")], ["R2", "R1"])
        self.assertEqual([r["id"] for r in grading.used_by(self.d, "field_self_check")], ["R2"])
        with self.assertRaises(grading.GradingError):
            grading.used_by(self.d, "opening_prompt")

    def test_render_order_and_labels(self):
        grading.mark(self.d, ["R2"], goal=True, dp=2, notes=True)
        out = grading.render(grading.load(self.d))
        lines = [ln for ln in out.splitlines() if ln.startswith("R")]
        self.assertEqual([ln.split()[0] for ln in lines], ["R2", "R1"])   # selected, then by severity
        self.assertIn("1 more (1 low severity or credits): --all shows them.", out)   # R3, a credit, is folded
        self.assertIn("R3 ", grading.render(grading.load(self.d), all=True))
        self.assertTrue(lines[0].rstrip().endswith("G 2 N"))
        self.assertIn("1 selected", out.splitlines()[0])


# --- dossier and facts rows -------------------------------------------------------------------------------

DOSSIER = {"schema": "johto.dossier/1", "id": "acme__widget__abc12345", "repo": "acme/widget", "generated_at": "2026-10-30T21:00:00-0700",
           "blocks": [{"type": "opportunity", "id": "o2", "title": "Retry backoff across requeues", "area": "retries",
                       "evidence": [{"pointer": "queue/queue.go:3-5", "fact": "Requeue sets attempts to zero."}],
                       "where_models_fail": ["queue.go Requeue resets attempts, so a retry loop looks bounded but never backs off.",
                                             "worker/run.go Run ignores the limit read in config.go."],
                       "upstream": {"related": [{"url": "https://github.com/acme/widget/issues/7", "title": "Backoff never grows",
                                                 "state": "open"}], "looks_solved": False}}]}


class Dossier(Base):
    def setUp(self):
        super().setUp()
        (self.root / "data" / "dossiers").mkdir(parents=True)
        (self.root / "data" / "dossiers" / f"{DOSSIER['id']}.json").write_text(json.dumps(DOSSIER))

    def test_hazards_are_unscoped_candidates(self):
        self.meta(dossier={"id": DOSSIER["id"], "opportunity_id": "o2"})
        data = self.refresh()
        self.assertEqual(len(data["rows"]), 2)
        r = data["rows"][0]
        self.assertEqual((r["kind"], r["suggested_scope"], r["scope"], r["limits"], r["review_status"], r["basis"]),
                         ("requirement", None, None, grading.DOSSIER_LIMITS, None, "none"))
        self.assertEqual(r["sources"][0]["type"], "dossier")
        self.assertEqual(r["sources"][0]["finding_id"], "o2.wmf1")
        self.assertEqual(r["refs"], ["queue.go"])
        self.assertEqual(grading.open_decisions(data), [])   # a hypothesis isn't a decision by itself
        grading.mark(self.d, ["R1"], scope="outside")
        self.refresh()
        self.assertEqual(self.rows()["R1"]["scope"], "outside")

    def test_link_from_brief(self):
        (self.d / "brief.md").write_text("# brief\n\n## Dossier (AI research)\n\nRepo page: /repos/acme__widget__abc12345  ·  "
                                         "generated x\n\n### Retry backoff across requeues (retries)\n- What...\n")
        self.assertEqual(len(self.refresh()["rows"]), 2)

    def test_no_link_no_rows(self):
        self.assertEqual(self.refresh()["rows"], [])


def bash(tid: str, ts: str, cmd: str) -> dict:
    return claude(ts, "", {"id": tid, "name": "Bash", "input": {"command": cmd}})


class Facts(Base):
    def claims(self, entries) -> list[dict]:
        self.write_run(entries, DIFF1)
        return [r for r in self.refresh()["rows"] if r["kind"] == "claim"]

    def test_claim_without_any_test(self):
        rows = self.claims(TURN1[:-2] + [claude("2026-10-30T10:10:00Z", "Done. All tests pass.", stop="end_turn")])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertIn('Claude said "All tests pass" (turn 1); no test command was detected', r["finding"])
        self.assertEqual((r["basis"], r["result"]["value"], r["severity"], r["refs"]), ("claimed", "unknown", "low", ["turn 1"]))
        self.assertEqual(r["limits"], grading.FACTS_LIMITS)
        self.assertEqual(r["sources"][0]["type"], "facts")

    def test_claims_in_different_turns_stay_apart(self):
        rows = self.claims([user("2026-10-30T10:00:00Z", OPENING), claude("2026-10-30T10:01:00Z", "All tests pass.", stop="end_turn"),
                            user("2026-10-30T10:30:00Z", "And now?"), claude("2026-10-30T10:31:00Z", "All tests pass.", stop="end_turn")])
        self.assertEqual([r["result"]["turn"] for r in rows], [1, 2])

    def test_claim_after_a_failed_test(self):
        rows = self.claims([user("2026-10-30T10:00:00Z", OPENING),
                            bash("t1", "2026-10-30T10:01:00Z", "go test ./... 2>&1 | tail -3"),
                            result("2026-10-30T10:01:30Z", "t1", "Exit code 1\nFAIL queue"),
                            claude("2026-10-30T10:02:00Z", "Fixed; the test suite passes now.", stop="end_turn")])
        self.assertEqual(len(rows), 1)
        self.assertIn("ended with exit 1", rows[0]["finding"])
        self.assertEqual(rows[0]["severity"], "medium")

    def test_backed_or_negated_claims_add_nothing(self):
        ok = [user("2026-10-30T10:00:00Z", OPENING), bash("t1", "2026-10-30T10:01:00Z", "go test ./..."),
              result("2026-10-30T10:01:30Z", "t1", "ok  queue 0.1s"),
              claude("2026-10-30T10:02:00Z", "All tests pass.", stop="end_turn")]
        self.assertEqual(self.claims(ok), [])
        neg = TURN1[:-2] + [claude("2026-10-30T10:10:00Z", "Not all tests pass yet; two still fail.", stop="end_turn")]
        self.assertEqual(self.claims(neg), [])

    def test_test_runner_detection(self):
        runs = ["timeout 300 npx mocha --reporter spec src/a.test.ts 2>&1 | tail -60", "cd web && npm run test:unit",
                "SP=/tmp/s && $SP/venv/bin/python manage.py test app.tests", "timeout 60 php vendor/bin/phpunit t/P.php",
                "uv run pytest -q", "go test ./...", "make test"]
        not_runs = ["cat phpunit.xml | grep DB_", 'grep -n "manage.py test" README.md', "npx tsc --noEmit",
                    "ls requirements; which tox", "python3 - <<'EOF'\np = 'tests/x.php'\nEOF"]
        self.assertEqual([grading.runs_tests(c) for c in runs], [True] * len(runs))
        self.assertEqual([grading.runs_tests(c) for c in not_runs], [False] * len(not_runs))
        self.assertEqual(grading._short_cmd(runs[0]), "npx mocha --reporter spec src/a.test.ts")

    def test_mocha_run_backs_the_claim(self):
        self.assertEqual(self.claims([user("2026-10-30T10:00:00Z", OPENING),
                                      bash("t1", "2026-10-30T10:01:00Z", "timeout 400 npx mocha --reporter spec 2>&1 | tail"),
                                      result("2026-10-30T10:01:30Z", "t1", "793 passing"),
                                      claude("2026-10-30T10:02:00Z", "All 793 tests pass.", stop="end_turn")]), [])

    def test_edit_after_the_last_passing_test(self):
        rows = self.claims([user("2026-10-30T10:00:00Z", OPENING), bash("t1", "2026-10-30T10:01:00Z", "go test ./..."),
                            result("2026-10-30T10:01:30Z", "t1", "ok"),
                            claude("2026-10-30T10:02:00Z", "", {"id": "t2", "name": "Edit",
                                                                "input": {"file_path": "/exp/x/model_a/queue/queue.go", "new_string": "x"}}),
                            result("2026-10-30T10:02:01Z", "t2", "ok"),
                            claude("2026-10-30T10:03:00Z", "All 12 tests pass.", stop="end_turn")])
        self.assertEqual(len(rows), 1)
        self.assertIn("files were edited after", rows[0]["finding"])


# --- which run -----------------------------------------------------------------------------------------

class SelectedRun(Base):
    def add_run(self, name: str, kind: str = "validate", collected: bool = True) -> None:
        (self.d / "runs" / name).mkdir(parents=True, exist_ok=True)
        m = json.loads((self.d / "task.json").read_text())
        rec = {"kind": kind, "dir": f"runs/{name}"}
        if collected:
            rec["collected_at"] = "2026-10-31T10:00:00-0700"
        m["runs"].append(rec)
        (self.d / "task.json").write_text(json.dumps(m))

    def test_fallbacks(self):
        self.add_run("validate-2", collected=False)
        self.assertEqual(grading.selected_run(self.d), "validate-2")   # nothing collected: the latest validation run
        m = json.loads((self.d / "task.json").read_text())
        m["runs"][0]["collected_at"] = "2026-10-30T11:00:00-0700"
        (self.d / "task.json").write_text(json.dumps(m))
        self.assertEqual(grading.selected_run(self.d), "validate-1")   # the latest collected validation run
        self.meta(submission_run="validate-2")
        self.assertEqual(grading.selected_run(self.d), "validate-2")   # Austin's choice

    def test_actions_rule_wins_when_present(self):
        with mock.patch.object(actions, "submission_run", lambda meta, d: "calib-9", create=True):
            self.assertEqual(grading.selected_run(self.d), "calib-9")

    def test_run_change_marks_other_run_rows(self):
        self.review(REPLY1)
        self.refresh()
        self.add_run("validate-2")
        self.meta(submission_run="validate-2")
        data = self.refresh()
        self.assertEqual(data["run"], "validate-2")
        self.assertEqual(data["run_changed"]["from"], "validate-1")
        self.assertTrue(all(r["other_run"] for r in data["rows"]))
        self.assertEqual(len(data["rows"]), 3)
        self.assertNotIn("R1 ", grading.render(data))
        self.assertIn("missed (validate-1)", grading.render(data, all=True))


# --- CLI -----------------------------------------------------------------------------------------------

class Cli(Base):
    def cli(self, *argv) -> str:
        p = argparse.ArgumentParser()
        sub = p.add_subparsers()
        grading.register(sub)
        a = p.parse_args(["grading", *argv])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            a.func(a)
        return out.getvalue()

    def test_show_mark_and_clear(self):
        self.review(REPLY1)
        self.assertIn("R1", self.cli("t1"))
        out = self.cli("t1", "mark", "R1", "R2", "--goal", "--dp", "2", "--scope", "necessary")
        self.assertIn("Marked R1, R2.", out)
        r = self.rows()["R1"]
        self.assertEqual((r["marks"]["goal"], r["marks"]["dp"], r["scope"]), (True, 2, "necessary"))
        self.cli("t1", "mark", "R1", "--no-dp", "--scope", "none", "--no-goal")
        r = self.rows()["R1"]
        self.assertEqual((r["marks"]["goal"], r["marks"]["dp"], r["scope"], r["marks"]["selected"]), (False, None, None, True))
        self.cli("t1", "mark", "R1", "--unselect")
        self.assertFalse(self.rows()["R1"]["marks"]["selected"])
        self.assertIn("rows", self.cli("t1", "refresh"))
        with self.assertRaises(SystemExit):
            self.cli("t1", "mark", "R1")
        with self.assertRaisesRegex(SystemExit, "No row R7"):
            self.cli("t1", "mark", "R7", "--notes")


# --- extraction (fake runner) --------------------------------------------------------------------------

class Extract(Base):
    def test_extract_writes_a_sidecar_and_rows(self):
        p = self.review("## Task Success\n\n1. Requeue resets attempts: `queue/queue.go:4`.\n")
        calls = []

        def runner(prompt, cwd, **kw):
            calls.append((prompt, kw))
            return {"text": "Here:\n" + block([finding("F1", "Requeue resets attempts.")]), "cost_usd": 0.03, "error": None}
        data = self.refresh(extract=True, runner=runner)
        self.assertEqual(len(calls), 1)
        self.assertIn("Requeue resets attempts: `queue/queue.go:4`", calls[0][0])
        self.assertEqual((calls[0][1]["tools"], calls[0][1]["model"]), ("", grading.EXTRACT_MODEL))
        side = p.with_suffix(".findings.json")
        self.assertEqual(json.loads(side.read_text())["findings"][0]["id"], "F1")
        self.assertEqual(data["rows"][0]["keys"], [f"extract:{p.name}#F1"])
        self.assertTrue(data["rows"][0]["sources"][0]["extracted"])
        self.refresh(runner=lambda *a, **k: self.fail("the sidecar is read; no second call"))
        self.assertEqual(len(self.rows()), 1)

    def test_failed_extract_keeps_nothing(self):
        p = self.review("## Notes\n\nNothing structured.\n")
        data = self.refresh(extract=True, runner=lambda *a, **k: {"text": "", "error": "boom", "cost_usd": None})
        self.assertEqual(data["rows"], [])
        self.assertFalse(p.with_suffix(".findings.json").exists())

    def test_no_extract_never_calls_claude(self):
        self.review("## Notes\n\nPlain.\n")
        self.refresh(runner=lambda *a, **k: self.fail("extract is opt-in"))


# --- verify (fake runner, local origin repo) ---------------------------------------------------------------

def confirmed_runner(seen: dict):
    def run(prompt, cwd, **kw):
        cwd = Path(cwd)
        seen.update(prompt=prompt, cwd=cwd, kw=kw, applied=(cwd / "queue" / "queue.go").read_text(),
                    git_diff=__import__("subprocess").run(["git", "diff", "--stat"], cwd=cwd, capture_output=True,
                                                          text=True).stdout)
        (cwd / ".johto-verify").mkdir()
        (cwd / ".johto-verify" / "check.sh").write_text("grep -n 'attempts' queue/queue.go\n")
        reply = {"status": "confirmed", "executed": True, "summary": "Requeue increments attempts after the diff.",
                 "ran": ["sh .johto-verify/check.sh"], "files": [".johto-verify/check.sh"], "substitutions": [],
                 "limits": "Only Requeue in isolation.", "evidence": ["queue/queue.go:4"]}
        return {"text": f"<<<VERIFY\n{json.dumps(reply)}\nVERIFY>>>", "cost_usd": 0.5, "error": None, "seconds": 30}
    return run


class Verify(Base):
    def setUp(self):
        super().setUp()
        self.review(REPLY1)
        self.refresh()

    def test_confirmed_by_execution(self):
        seen: dict = {}
        res = grading.verify(self.d, "R1", runner=confirmed_runner(seen), log=self.say)
        self.assertEqual((res["status"], res["executed"]), ("confirmed", True))
        self.assertIn("attempts++", seen["applied"])                   # the run's diff is applied
        self.assertIn("queue/queue.go", seen["git_diff"])              # and shows as the diff over the pinned commit
        self.assertIn("Requeue resets the attempts counter", seen["prompt"])
        self.assertIn(OPENING, seen["prompt"])
        self.assertEqual(seen["kw"]["tools"], "Bash,Read,Grep,Glob,Write,Edit")
        self.assertFalse(seen["cwd"].exists())                         # the checkout is removed
        r = self.rows()["R1"]
        self.assertEqual((r["basis"], r["verify"]["status"]), ("isolated", "confirmed"))
        report = self.d / r["verify"]["path"]
        text = report.read_text()
        self.assertTrue(report.name.startswith("verify-R1-"))
        self.assertIn("grep -n 'attempts' queue/queue.go", text)       # the scratch test survives in the report
        self.assertIn("Only Requeue in isolation.", text)
        self.assertEqual(stages.last(self.d, "verify_done")["status"], "confirmed")
        bare = self.root / "data" / "cache" / "repos" / "acme__widget.git"
        self.assertEqual(list((bare / "worktrees").glob("*")) if (bare / "worktrees").exists() else [], [])
        self.refresh()
        self.assertEqual(self.rows()["R1"]["basis"], "isolated")       # a later refresh keeps the stronger basis

    def test_refuted_or_unreadable_keeps_basis(self):
        def refuted(prompt, cwd, **kw):
            return {"text": '<<<VERIFY\n{"status": "refuted", "executed": true, "summary": "No reset."}\nVERIFY>>>',
                    "error": None}
        grading.verify(self.d, "R2", runner=refuted, log=self.say)
        r = self.rows()["R2"]
        self.assertEqual((r["basis"], r["verify"]["status"]), ("traced", "refuted"))
        grading.verify(self.d, "R3", runner=lambda *a, **k: {"text": "no block", "error": None}, log=self.say)
        r3 = self.rows()["R3"]
        self.assertEqual(r3["verify"]["status"], "inconclusive")
        self.assertIn("The run failed", (self.d / r3["verify"]["path"]).read_text())

    def test_diff_that_does_not_apply_stops_before_claude(self):
        (self.run_dir / "prework_output" / "final.diff").write_text(
            "diff --git a/queue/queue.go b/queue/queue.go\n--- a/queue/queue.go\n+++ b/queue/queue.go\n"
            "@@ -1,3 +1,3 @@\n-package nothing\n+package queue2\n \n func Requeue() {\n")
        with self.assertRaisesRegex(grading.GradingError, "doesn't apply fully"):
            grading.verify(self.d, "R1", runner=lambda *a, **k: self.fail("no Claude call"), log=self.say)
        self.assertFalse((self.d / "checks").exists())

    def test_plan_and_unknown_row(self):
        p = grading.plan_verify(self.d, "R1")
        self.assertEqual((p["run"], p["files"]), ("validate-1", ["queue/queue.go"]))
        self.assertIn("5-20 min", p["estimate"])
        self.assertNotIn("$", p["estimate"])        # no spend cap: Austin is on a Claude subscription
        with self.assertRaises(grading.GradingError):
            grading.plan_verify(self.d, "R42")


if __name__ == "__main__":
    unittest.main()
