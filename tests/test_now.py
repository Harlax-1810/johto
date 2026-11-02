"""Now (GET /api/now) and Submitted (GET /api/now/submitted): the shape spec.md 7.4 promises, the words on the first
layer (rule 5), reviewer lessons by the fixed rule of 7.6, and the screens' sources carrying no command hints."""
import json
import re
import time
import unittest
from pathlib import Path

from johto.web import api_now
from test_realstate_app import SAME, call
from test_realstate_derive import RealRoot

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "johto" / "web" / "static"
FIX = Path(__file__).resolve().parent / "fixtures"
NOW = time.time()
ISO = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(NOW - 75))

FREE = {"state": "free", "facts": None, "queue": []}
SETTINGS = {"wip_before_validation": 2}
NOW_KEYS = {"lede", "focus", "after", "idle_pick", "slot_picks", "lessons"}

# What the first layer never says (spec 1, rule 5): CLI hints, file names, run ids, exit codes, paths, model ids.
BANNED = [r"\bjohto ", r"\.json\b", r"\b(?:validate|calib)-\d", r"\bexit code\b", r"\bexit \d", r"/Users/",
          r"claude-(?:opus|sonnet|haiku)", r"\bOpus \d", r"`", r"\bdocker (?:run|pull|compose)", r"\bpip install"]


def first_layer(item: dict) -> list[str]:
    """Every string Now shows without a click for one item (the "whole message" sheet is one click down)."""
    out = [item.get(k) or "" for k in ("lede", "verb", "what", "fact")]
    out.append((item.get("primary") or {}).get("label") or "")
    b = item.get("body") or {}
    for k in ("context", "sentence", "question", "field_words", "fact", "prompt"):
        out.append(b.get(k) or "")
    out += list(b.get("message") or []) + list(b.get("facts") or [])
    out += [s.get("summary") or "" for s in b.get("path") or []]
    return out


def assert_plain(tc: unittest.TestCase, strings, where=""):
    for s in strings:
        for rx in BANNED:
            tc.assertIsNone(re.search(rx, s), f"{where}: {rx!r} in {s!r}")


def lane(state="waiting", short="xq1", run="validate-1", tests=None, edits=None, question=None):
    return {"state": state, "since": ISO, "run": {"short": short, "run": run, "kind": "validate", "label": "chatd · Room limits"},
            "facts": {"turns": 3, "tests": tests or {}, "files_edited": edits, "task_claude_min": 24.0},
            "question": question, "input": {"empty": True}}


def item(kind, short="xq1", **kw):
    base = {"id": f"{kind}:{short}:validate-1:abc", "kind": kind, "tier": 1, "short": short, "title": kw.pop("title", "Step"),
            "label": "chatd · Room limits", "reason": kw.pop("reason", ""), "est_s": 600, "since": ISO,
            "lane": kind.startswith("lane_"), "run": "validate-1", "host": "Mac", "primary": None}
    base.update(kw)
    return base


def ctx(short="xq1", **kw):
    c = {"short": short, "label": "chatd · Room limits", "slot": {"action": "Modify"}, "stage": "validating",
         "texts": {"opening_prompt": "Can limiting a room load it first?", "goal": "SECRET GOAL TEXT",
                   "decision_points": "SECRET DP", "codebase": "SECRET CB", "validation_notes": "SECRET NOTES"},
         "runs": [{"name": "validate-1", "kind": "validate"}], "monitor": {}, "repo": "acme/chatd"}
    c.update(kw)
    return c


MESSAGE = """I read `room/room.go:120` and ran `go test ./...` (exit 0) on claude-opus-5-5.

The limit lives on the room struct, so an unloaded room has nowhere to keep it.

```
$ johto lane xq1 validate
```

Should limiting a room that isn't loaded yet load it first, or return 404 like the user endpoints do?"""


class Shape(unittest.TestCase):
    def test_empty_queue_with_the_lane_free_offers_the_idle_pick(self):
        pick = {"id": "d1", "name": "Mailroom", "line": "Two copies email the same people twice.", "fit": None}
        d = api_now.build([], FREE, {}, SETTINGS, pick=pick, lessons=[], now=NOW)
        self.assertEqual(set(d), NOW_KEYS)
        self.assertIsNone(d["focus"])
        self.assertEqual(d["after"], [])
        self.assertEqual(d["lede"]["title"], "Nothing needs you. The lane is free.")
        self.assertEqual(d["idle_pick"]["name"], "Mailroom")

    def test_no_idle_pick_while_claude_works_or_enough_tasks_wait_for_their_run(self):
        pick = {"id": "d1", "name": "Mailroom", "line": "", "fit": None}
        busy = api_now.build([], {**lane("working"), "facts": {}}, {"xq1": ctx()}, SETTINGS, pick=pick, lessons=[], now=NOW)
        self.assertIsNone(busy["idle_pick"])
        self.assertNotIn("free", busy["lede"]["title"])
        waiting = {s: ctx(short=s, stage="prompt") for s in ("a1", "a2")}
        full = api_now.build([], FREE, waiting, SETTINGS, pick=pick, lessons=[], now=NOW)
        self.assertIsNone(full["idle_pick"])

    def test_slot_picks_show_while_claude_works_and_replace_the_idle_pick(self):
        pick = {"id": "d1", "name": "Mailroom", "line": "", "fit": None}
        sp = {"items": [{"dossier_id": "d2", "opportunity_id": "o1", "title": "Restarts lose rooms", "open": 1}],
              "rating": False, "weak": []}
        busy = api_now.build([], {**lane("working"), "facts": {}}, {"xq1": ctx()}, SETTINGS, pick=pick, lessons=[],
                             now=NOW, slot_picks=sp)
        self.assertEqual(busy["slot_picks"]["items"][0]["title"], "Restarts lose rooms")
        free = api_now.build([], FREE, {}, SETTINGS, pick=pick, lessons=[], now=NOW, slot_picks=sp)
        self.assertIsNone(free["idle_pick"])
        none = api_now.build([], FREE, {}, SETTINGS, pick=pick, lessons=[], now=NOW,
                             slot_picks={"items": [], "rating": False, "weak": []})
        self.assertIsNone(none["slot_picks"])
        self.assertEqual(none["idle_pick"]["name"], "Mailroom")
        # Slots with no great option still show, with their research, when no pick is ready.
        weak = {"items": [], "rating": False, "weak": [{"key": "Operate|Games|Personal-scale"}],
                "research": {"mode": "auto", "running": None, "weak": [{"key": "Operate|Games|Personal-scale"}]}}
        shown = api_now.build([], FREE, {}, SETTINGS, pick=pick, lessons=[], now=NOW, slot_picks=weak)
        self.assertEqual(shown["slot_picks"]["research"]["mode"], "auto")
        self.assertEqual(shown["idle_pick"]["name"], "Mailroom")

    def test_lane_question_shows_claudes_words_without_code_and_answers_in_the_lane(self):
        q = [item("lane_waiting", title="Answer Claude's question")]
        c = ctx(message=MESSAGE)
        d = api_now.build(q, lane(tests={"runs": 2, "last_ok": True}, edits=["a", "b"]), {"xq1": c}, SETTINGS,
                          pick=None, lessons=[], now=NOW)
        f = d["focus"]
        self.assertEqual(f["kind_card"], "lane")
        self.assertEqual(f["lede"], "Claude asked you something on chatd.")
        self.assertEqual(f["primary"]["action"], "lane_open")
        self.assertEqual(f["primary"]["label"], "Answer in the lane")
        b = f["body"]
        self.assertTrue(b["question"].startswith("Should limiting a room"))
        self.assertTrue(any("nowhere to keep it" in p for p in b["message"]))
        self.assertIn("Ran the tests: all passed", b["facts"])
        self.assertIn("Changed 2 files", b["facts"])
        self.assertEqual(b["budget"], 90)
        self.assertIn("Validation run", b["context"])
        assert_plain(self, first_layer(f), "lane question")

    def test_a_hidden_test_result_never_reads_as_failed(self):
        def facts(last_ok):
            d = api_now.build([item("lane_waiting")], lane(tests={"runs": 1, "last_ok": last_ok}, edits=[]),
                              {"xq1": ctx()}, SETTINGS, pick=None, lessons=[], now=NOW)
            return d["focus"]["body"]["facts"]
        self.assertNotIn("Ran the tests: failing", facts(None))
        self.assertFalse([f for f in facts(None) if "tests" in f])
        self.assertIn("Ran the tests: failing", facts(False))
        for v in (None, True, False):
            self.assertFalse([f for f in facts(v) if "failed" in f])

    def test_paste_card_shows_only_the_opening_prompt(self):
        d = api_now.build([item("lane_paste", title="Paste your prompt")], lane("ready_for_prompt"), {"xq1": ctx()},
                          SETTINGS, pick=None, lessons=[], now=NOW)
        f = d["focus"]
        self.assertEqual(f["primary"]["action"], "lane_type")
        self.assertEqual(f["primary"]["args"], {"what": "prompt"})
        self.assertEqual(f["body"]["prompt"], "Can limiting a room load it first?")
        self.assertNotIn("SECRET", json.dumps(d))

    def test_login_types_login_and_says_so_plainly(self):
        d = api_now.build([item("lane_login", title="Log in to Claude in the lane")], lane("needs_login"), {"xq1": ctx()},
                          SETTINGS, pick=None, lessons=[], now=NOW)
        f = d["focus"]
        self.assertEqual(f["primary"]["args"], {"what": "login"})
        self.assertEqual(f["fact"], "Claude in the lane is signed out.")

    def test_blocker_is_one_sentence_with_its_fix(self):
        q = [item("blocker_docker", short=None, title="Docker isn't running", reason="docker info failed: exit 1",
                  lane=False, tier=0)]
        d = api_now.build(q, FREE, {}, SETTINGS, pick=None, lessons=[], now=NOW)
        f = d["focus"]
        self.assertEqual(f["kind_card"], "blocker")
        self.assertEqual(f["tone"], "stop")
        assert_plain(self, first_layer(f), "blocker")

    def test_setup_failed_offers_a_fresh_folder(self):
        q = [item("setup_failed", title="Lane setup failed", reason="prework setup exited 2: see /Users/x/log.txt")]
        d = api_now.build(q, lane("setup_failed"), {"xq1": ctx()}, SETTINGS, pick=None, lessons=[], now=NOW)
        f = d["focus"]
        self.assertEqual(f["kind_card"], "blocker")
        self.assertEqual(f["primary"]["action"], "lane_start")
        assert_plain(self, first_layer(f), "setup failed")

    def test_writing_item_opens_the_task_at_its_field(self):
        q = [item("write_details", title="Write the decision points", field="decision_points", lane=False,
                  reason="Grading placed 4 findings; see tasks/xq1/grading.json")]
        c = ctx(stage="details", runs=[{"name": "validate-1", "kind": "validate", "collected_at": ISO}])
        d = api_now.build(q, FREE, {"xq1": c}, SETTINGS, pick=None, lessons=[], now=NOW)
        f = d["focus"]
        self.assertEqual(f["kind_card"], "writing")
        self.assertEqual(f["place"]["kind"], "task")
        self.assertTrue(f["place"]["href"].startswith("/task/xq1#step="))
        self.assertEqual([s["id"] for s in f["body"]["path"]], ["prompt", "run", "write", "submit"])
        assert_plain(self, first_layer(f), "writing")

    def test_sent_back_card_quotes_the_reviewer_once(self):
        q = [item("feedback", title="Read the reviewer's notes", lane=False)]
        les = [{"short": "xq1", "label": "x", "state": "sent_back", "sentence": "Say how you checked the restart case.",
                "full": "Say how you checked the restart case.", "at": ISO, "seen": False},
               {"short": "zz9", "label": "y", "state": "accepted", "sentence": "Good.", "full": "Good.", "at": ISO,
                "seen": False}]
        d = api_now.build(q, FREE, {"xq1": ctx(stage="feedback")}, SETTINGS, pick=None, lessons=les, now=NOW)
        self.assertEqual(d["focus"]["body"]["quote"], "Say how you checked the restart case.")
        self.assertEqual([x["short"] for x in d["lessons"]], ["zz9"])

    def test_after_this_holds_at_most_three_and_pick_rows_never_show(self):
        q = [item("write_details", short=f"t{i}", lane=False) for i in range(6)] + [item("pick", short="p1", lane=False)]
        ctxs = {f"t{i}": ctx(short=f"t{i}", stage="details") for i in range(6)}
        d = api_now.build(q, FREE, ctxs, SETTINGS, pick=None, lessons=[], now=NOW)
        self.assertEqual(d["focus"]["short"], "t0")
        self.assertEqual(len(d["after"]), 3)
        self.assertNotIn("pick", [i["kind"] for i in [d["focus"], *d["after"]]])

    def test_focus_follows_a_pinned_item(self):
        q = [item("write_details", short="t0", lane=False), item("rate", short="t1", lane=False)]
        ctxs = {s: ctx(short=s, stage="rate") for s in ("t0", "t1")}
        d = api_now.build(q, FREE, ctxs, SETTINGS, pick=None, lessons=[], now=NOW, focus_id="rate:t1")
        self.assertEqual(d["focus"]["short"], "t1")
        self.assertEqual([i["short"] for i in d["after"]], ["t0"])

    def test_calibration_is_never_the_next_run_on_offer(self):
        free = {**FREE, "queue": [{"short": "xq1", "kind": "calib", "label": "chatd"}]}
        d = api_now.build([], free, {"xq1": ctx(stage="prompt")}, SETTINGS, pick=None, lessons=[], now=NOW)
        self.assertNotIn("calibration", json.dumps(d).lower())

    def test_plain_facts(self):
        p = api_now.plain_fact
        self.assertEqual(p("The lane is free"), "The lane is free.")
        self.assertEqual(p("Validation 1 was collected yesterday at 13:24"), "Validation run was collected yesterday at 13:24.")
        self.assertEqual(p("validate-2 is waiting"), "Validation run 2 is waiting.")
        self.assertEqual(p("Grading placed 4 findings; see tasks/xq1/grading.json"), "Grading placed 4 findings.")
        self.assertEqual(p("docker info failed: exit 1"), "Docker info failed.")
        self.assertEqual(p("The tests ran (exit 0) twice"), "The tests ran twice.")
        self.assertEqual(p(""), "")

    def test_run_words_have_no_ids(self):
        self.assertEqual(api_now.run_words("validate-1"), "validation run")
        self.assertEqual(api_now.run_words("validate-2"), "validation run 2")
        self.assertEqual(api_now.run_words("nonsense"), "")


class Lessons(unittest.TestCase):
    def setUp(self):
        self.fb = json.loads((FIX / "reviewer_feedback.json").read_text())

    def test_the_improvement_sentence_from_real_feedback(self):
        s = api_now.lesson_sentence(self.fb["t4sk0001"]["feedback"])
        self.assertTrue(s.startswith("The goal should state the row limit."))
        w = api_now.lesson_sentence(self.fb["t4sk0003"]["feedback"])
        self.assertTrue(w.startswith("The validation notes are cut off mid-word"), w)
        for t in (s, w):
            self.assertLessEqual(len(t), 160)
            self.assertNotIn("**", t)

    def test_marker_rule_and_first_sentence_fallback(self):
        self.assertEqual(api_now.lesson_sentence("Good work overall. Next time, please name the threshold in the goal."),
                         "Next time, please name the threshold in the goal.")
        self.assertEqual(api_now.lesson_sentence("Clear and correct. Nothing else."), "Clear and correct. Nothing else.")
        long = "Please " + "make the goal checkable " * 20
        self.assertLessEqual(len(api_now.lesson_sentence(long)), 160)
        self.assertTrue(api_now.lesson_sentence(long).endswith("…"))
        self.assertEqual(api_now.lesson_sentence(""), "")

    def test_a_lesson_is_new_until_he_puts_it_away(self):
        studio = {"feedback": "Please name the threshold.", "feedback_at": "2026-10-31T13:00:00-0700"}
        les = api_now.lesson_of("t1", "chatd · Room limits", "accepted", studio)
        self.assertEqual(set(les) - {"seen"}, {"short", "label", "state", "sentence", "full", "at"})
        self.assertEqual(les["state"], "accepted")
        self.assertFalse(les["seen"])
        seen = api_now.lesson_of("t1", "x", "accepted", {**studio, "feedback_seen_at": "2026-10-31T14:00:00-0700"})
        self.assertTrue(seen["seen"])
        self.assertIsNone(api_now.lesson_of("t1", "x", "accepted", {"feedback": ""}))

    def test_only_unseen_lessons_reach_now(self):
        lessons = [{"short": "a", "seen": True}, {"short": "b", "seen": False}]
        d = api_now.build([], FREE, {}, SETTINGS, pick=None, lessons=lessons, now=NOW)
        self.assertEqual([x["short"] for x in d["lessons"]], ["b"])

    def test_submitted_lede_says_only_counts_that_matter(self):
        rows = [{"state": "accepted", "at": ISO}, {"state": "accepted", "at": ISO}, {"state": "in_review", "at": ISO},
                {"state": "dropped", "at": ISO}]
        self.assertEqual(api_now.submitted_lede(rows, NOW), {"title": "Two accepted this week.", "support": "One in review."})
        self.assertEqual(api_now.submitted_lede([], NOW)["title"], "Nothing submitted yet.")
        self.assertEqual(api_now.submitted_lede([{"state": "dropped", "at": ISO}], NOW)["title"],
                         "Nothing is waiting on a reviewer.")


class RealNow(RealRoot):
    def test_now_answers_the_spec_shape(self):
        st, d = call("GET", "/api/now")
        self.assertEqual(st, 200, d)
        self.assertEqual(set(d), NOW_KEYS)
        self.assertTrue(d["lede"]["title"])
        self.assertIsNotNone(d["focus"])
        self.assertLessEqual(len(d["after"]), 3)
        for it in [d["focus"], *d["after"]]:
            for k in ("id", "kind", "short", "title", "label", "est_s", "since", "lane", "primary", "lede", "fact", "body"):
                self.assertIn(k, it)
            assert_plain(self, first_layer(it), it["kind"])

    def test_sent_back_feedback_is_a_lesson_until_seen(self):
        st, d = call("GET", "/api/now")
        self.assertIn("isentbk1", [x["short"] for x in d["lessons"]])
        les = next(x for x in d["lessons"] if x["short"] == "isentbk1")
        self.assertEqual(les["state"], "sent_back")
        self.assertEqual(les["sentence"], "Lorem reviewer feedback.")
        st, r = call("POST", "/api/now/lessons/isentbk1/seen", {}, headers=SAME)
        self.assertEqual(st, 200, r)
        self.assertTrue(self.meta("isentbk1")["studio"]["feedback_seen_at"])
        st, d = call("GET", "/api/now")
        self.assertNotIn("isentbk1", [x["short"] for x in d["lessons"]])
        st, r = call("POST", "/api/now/lessons/nosuch01/seen", {}, headers=SAME)
        self.assertEqual(st, 404)

    def test_submitted_rows_newest_first_with_state_and_lesson(self):
        st, d = call("GET", "/api/now/submitted")
        self.assertEqual(st, 200, d)
        by = {r["short"]: r for r in d["rows"]}
        self.assertEqual(by["isentbk1"]["state"], "sent_back")
        self.assertEqual(by["isentbk1"]["sentence"], "Lorem reviewer feedback.")
        self.assertEqual(by["kreview1"]["state"], "in_review")
        self.assertTrue(by["kreview1"]["when"].startswith("since "))
        self.assertTrue(d["lede"]["title"])
        for r in d["rows"]:
            assert_plain(self, [r["label"], r["when"], r.get("sentence") or ""], r["short"])

    def test_a_focus_query_too_long_is_ignored(self):
        st, d = call("GET", "/api/now", query="focus=" + "x" * 300)
        self.assertEqual(st, 200)


class DemoNow(unittest.TestCase):
    def test_demo_answers_from_the_demo_queue(self):
        st, d = call("GET", "/api/now", demo_mode=True)
        self.assertEqual(st, 200, d)
        self.assertEqual(set(d), NOW_KEYS)
        self.assertIsNotNone(d["focus"])
        kinds = [i["kind"] for i in [d["focus"], *d["after"]]]
        self.assertNotIn("decide", kinds)       # no calibration in Austin's order
        for it in [d["focus"], *d["after"]]:
            assert_plain(self, first_layer(it), it["kind"])

    def test_demo_writes_go_nowhere(self):
        st, d = call("POST", "/api/now/lessons/d3m0aaa4/seen", {}, demo_mode=True, headers=SAME)
        self.assertNotEqual(st, 200)

    def test_demo_submitted(self):
        st, d = call("GET", "/api/now/submitted", demo_mode=True)
        self.assertEqual(st, 200)
        self.assertEqual({r["state"] for r in d["rows"]}, {"in_review", "sent_back", "dropped", "accepted"})


class ScreenSources(unittest.TestCase):
    """The first-layer modules carry no CLI hints, file names, run ids or exit codes (spec 9.3)."""
    FILES = ["app.js", "screens/now.js", "screens/finished.js", "components/shell.js",
             "components/drawer.js", "components/monitor.js"]
    PATTERNS = [r"\bjohto [a-z]", r"\.json\"", r"\b(?:validate|calib)-\d", r"\bexit code\b", r"code \$\{msg\.code",
                r"Opus \d", r"claude-opus"]

    def test_no_command_hints_in_the_screens(self):
        for rel in self.FILES:
            src = (STATIC / rel).read_text()
            for rx in self.PATTERNS:
                hit = re.search(rx, src)
                self.assertIsNone(hit, f"{rel}: {hit.group(0) if hit else ''}")

    def test_now_hands_the_next_key_back_when_it_leaves(self):
        src = (STATIC / "screens/now.js").read_text()
        self.assertIn("setNextHandler(go)", src)
        self.assertIn("setNextHandler(null)", src)

    def test_now_has_no_reply_box(self):
        """Decision 13.1: Claude's question shows on Now, the answer is typed in the lane."""
        src = (STATIC / "screens/now.js").read_text()
        self.assertNotIn("<textarea", src)
        self.assertNotIn('"/api/lane/type", { what: "text"', src)


if __name__ == "__main__":
    unittest.main()
