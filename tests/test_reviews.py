"""Saved reviews and ChatGPT sends (johto/reviews.py): dedupe, run and turn inference, the pending lifecycle
(sending, sent, unconfirmed; settled by reading the chat; expiry), follow-ups into the same chat, never sending
a pack twice or a partial pack, the result's "why", reading the findings block, and the `johto review`
commands.

johto.chatgpt is replaced by a stub in sys.modules, so nothing reaches a browser.

Run: .venv/bin/python -m unittest tests.test_reviews -v
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from test_delta import DIFF2, TURN1, TURN2, Fixture  # noqa: E402

from johto import delta, pack, reviews, stages  # noqa: E402

REPLY = "## Task Success evidence\n\n1. Requeue still resets attempts: `queue/queue.go:4`.\n"
PACK_TEXT = "This file is a merged representation of the entire codebase, combined into a single document by Repomix."


class StubChatGPT(types.ModuleType):
    """The johto.chatgpt contract: send, fetch, inspect, holds, text_sha, usage and ChatGPTError. `chats` holds
    each chat's newest user message, as inspect() would see it."""

    class ChatGPTError(Exception):
        pass

    def __init__(self):
        super().__init__("johto.chatgpt")
        self.sent, self.replies, self.n, self.chats, self.inspected = [], {}, 0, {}, []
        self.usage_left = 60

    @staticmethod
    def text_sha(text):
        return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()

    def send(self, text, title, *, chat_url=None, account=None, no_send=False, log=print, tag=None, on_chat=None):
        self.sent.append({"title": title, "tag": tag, "chat_url": chat_url, "account": account, "no_send": no_send,
                          "chars": len(text)})
        if no_send:
            return {"chat_url": chat_url or "", "sent": False, "chars": len(text), "usage_pct": self.usage_left}
        if not chat_url:
            self.n += 1
            chat_url = f"https://chatgpt.com/c/chat-{self.n}"
        if on_chat:
            on_chat(chat_url)
        self.chats[chat_url] = text
        return {"chat_url": chat_url, "sent": True, "chars": len(text), "usage_pct": self.usage_left,
                "titled": True}

    def fetch(self, chat_url, *, account=None):
        r = self.replies.get(chat_url, {"status": "thinking", "markdown": None, "message_id": None})
        if isinstance(r, Exception):
            raise r
        return r

    def inspect(self, chat_url, *, account=None):
        self.inspected.append(chat_url)
        got = self.chats.get(chat_url)
        if isinstance(got, Exception):
            raise got
        if got is None:
            return {"found": False, "last_user_sha": None, "assistant_status": "none"}
        return {"found": True, "last_user_sha": self.text_sha(got), "assistant_status": "thinking"}

    def holds(self, info, text):
        if not info.get("found"):
            return False
        return None if not info.get("last_user_sha") else info["last_user_sha"] == self.text_sha(text)

    def usage(self, account=None):
        return self.usage_left


class Base(Fixture):
    def setUp(self):
        super().setUp()
        self.cg = StubChatGPT()
        self.mods = mock.patch.dict(sys.modules, {"johto.chatgpt": self.cg})
        self.mods.start()
        self.log = []

    def tearDown(self):
        self.mods.stop()
        super().tearDown()

    def say(self, *a):
        self.log.append(" ".join(map(str, a)))


class Save(Base):
    def test_dedupe_ignores_whitespace_and_front_matter_round_trips(self):
        p = reviews.save(self.d, "validate-1", 2, REPLY, pack=self.run_dir / "review-pack-x.xml",
                         chat_url="https://chatgpt.com/c/a")
        self.assertEqual(p.name, "validate-1-t02-gptpro.md")
        again = reviews.save(self.d, "validate-1", 2, "  " + REPLY.replace("\n\n", "\n \n") + "\n\n")
        self.assertEqual(again, p)
        self.assertEqual(len(list(reviews.reviews_dir(self.d).glob("*.md"))), 1)
        r = reviews.list_reviews(self.d)[0]
        self.assertEqual((r["run"], r["turn"], r["source"], r["pack"], r["chat_url"]),
                         ("validate-1", 2, "gptpro", "runs/validate-1/review-pack-x.xml", "https://chatgpt.com/c/a"))
        self.assertEqual(r["first_line"], "Task Success evidence")
        self.assertEqual(reviews.body(p).strip(), REPLY.strip())
        self.assertEqual(stages.last(self.d, "review_saved")["path"], "reviews/validate-1-t02-gptpro.md")

    def test_a_second_reply_for_the_same_turn_gets_its_own_file(self):
        a = reviews.save(self.d, "validate-1", 1, REPLY)
        b = reviews.save(self.d, "validate-1", 1, REPLY + "\nMore.")
        self.assertEqual((a.name, b.name), ("validate-1-t01-gptpro.md", "validate-1-t01-gptpro-2.md"))
        self.assertEqual(reviews.latest(self.d, "validate-1")["name"], b.name)


class SaveFillsGrading(Base):
    def test_a_saved_review_reaches_the_grading_table(self):
        """Every view reads grading.json: a saved reply's findings are there without anyone pressing Refresh."""
        from johto import grading
        from test_grading import REPLY1
        reviews.save(self.d, "validate-1", 1, REPLY1)
        rows = grading.load(self.d)["rows"]
        self.assertEqual(len([r for r in rows if r["sources"][0]["type"] == "review"]), 3)


class Unwrap(Base):
    """A reply pasted as the API's JSON is saved as the Markdown inside it; anything else is saved as it came."""

    def test_a_responses_api_message_object(self):
        from johto import grading
        from test_grading import REPLY1
        wrapped = json.dumps({"id": "msg_1", "type": "message", "status": "completed", "role": "assistant",
                              "content": [{"type": "output_text", "annotations": [], "text": REPLY1}]})
        p = reviews.save(self.d, "validate-1", 1, wrapped)
        self.assertEqual(reviews.body(p).strip(), REPLY1.strip())
        self.assertEqual(reviews.digest(reviews.body(p)), "First review.")
        self.assertEqual([f["id"] for f in reviews.findings(reviews.body(p))], ["F1", "F2", "F3"])
        self.assertEqual(len([r for r in grading.load(self.d)["rows"] if r["sources"][0]["type"] == "review"]), 3)
        # The same reply as plain Markdown is the same review.
        self.assertEqual(reviews.save(self.d, "validate-1", 1, REPLY1), p)

    def test_other_api_shapes(self):
        whole = {"object": "response", "output": [{"type": "reasoning", "summary": []},
                                                   {"type": "message", "role": "assistant",
                                                    "content": [{"type": "output_text", "text": "## Digest\n\nA.\n"}]}]}
        self.assertEqual(reviews.unwrap(json.dumps(whole)).strip(), "## Digest\n\nA.")
        self.assertEqual(reviews.unwrap(json.dumps({"output_text": "## Digest\n\nB.\n"})).strip(), "## Digest\n\nB.")
        self.assertEqual(reviews.unwrap(json.dumps([whole["output"][1]])).strip(), "## Digest\n\nA.")
        chat = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "## Digest\n\nC.\n"}}]}
        self.assertEqual(reviews.unwrap(json.dumps(chat)).strip(), "## Digest\n\nC.")

    def test_not_a_wrapper_is_left_alone(self):
        bare = json.dumps([{"id": "F1", "finding": "x", "kind": "defect"}])
        self.assertEqual(reviews.unwrap(bare), bare)
        self.assertEqual(reviews.unwrap(REPLY), REPLY)
        self.assertEqual(reviews.unwrap("{not json"), "{not json")
        self.assertEqual(reviews.unwrap(json.dumps({"type": "message", "content": []})), json.dumps({"type": "message", "content": []}))


class Remove(Base):
    @staticmethod
    def _from_review(rows):
        return [r for r in rows if any(s.get("type") == "review" for s in r.get("sources") or [])]

    def test_a_removed_review_leaves_the_folder_and_the_table_but_not_his_marks(self):
        """Re-entering a review pasted wrong: the file goes aside, its unmarked rows go, a marked row stays and
        takes the re-entered finding with the same ID, so the mark survives."""
        from johto import grading
        from test_grading import REPLY1
        p = reviews.save(self.d, "validate-1", 1, REPLY1)
        first, *others = [r["id"] for r in self._from_review(grading.load(self.d)["rows"])]
        self.assertEqual(len(others), 2)
        grading.mark(self.d, [first], goal=True)
        out = reviews.remove(self.d, p.name)
        self.assertEqual((out["run"], out["turn"], sorted(out["dropped"]), out["kept"]),
                         ("validate-1", 1, sorted(others), [first]))
        self.assertEqual(reviews.list_reviews(self.d), [])
        self.assertEqual(len(list((reviews.reviews_dir(self.d) / reviews.REMOVED).glob(f"*-{p.name}"))), 1)
        rows = {r["id"]: r for r in grading.load(self.d)["rows"]}
        self.assertIn(first, rows)
        self.assertFalse(any(x in rows for x in others))
        self.assertEqual(self._from_review(rows.values()), [])
        self.assertEqual(stages.last(self.d, "review_removed")["path"], f"reviews/{p.name}")
        reviews.save(self.d, "validate-1", 1, REPLY1.replace("First review", "Second reading"))
        rows = {r["id"]: r for r in grading.load(self.d)["rows"]}
        again = [r["id"] for r in self._from_review(rows.values())]
        self.assertEqual(len(again), 3)
        self.assertIn(first, again)
        self.assertTrue(rows[first]["marks"]["goal"])
        self.assertFalse(any(x in rows for x in others))

    def test_only_a_plain_name_inside_the_folder(self):
        reviews.save(self.d, "validate-1", 1, REPLY)
        with self.assertRaises(ValueError):
            reviews.remove(self.d, "../task.json")
        with self.assertRaises(ValueError):
            reviews.remove(self.d, "pending.json")
        with self.assertRaises(FileNotFoundError):
            reviews.remove(self.d, "validate-9-t01-gptpro.md")
        self.assertEqual(len(reviews.list_reviews(self.d)), 1)


class Add(Base):
    def test_turn_comes_from_the_newest_pack(self):
        self.full_pack()
        self.write_run(TURN1 + TURN2, DIFF2)   # the transcript moved on; the review is of the packed turn
        p = reviews.add(self.d, text=REPLY)
        fm, _ = reviews._parse(p)
        self.assertEqual((fm["run"], fm["turn"], fm["pack"]), ("validate-1", 1, "runs/validate-1/review-pack-20261030-101500.xml"))

    def test_turn_comes_from_the_transcript_without_packs(self):
        self.write_run(TURN1 + TURN2, DIFF2)
        p = reviews.add(self.d, text=REPLY, source="claude")
        self.assertEqual(p.name, "validate-1-t02-claude.md")

    def test_clipboard_by_default_and_a_pack_is_refused(self):
        with mock.patch.object(reviews, "_clipboard", return_value=REPLY):
            p = reviews.add(self.d)
        self.assertTrue(p.exists())
        with mock.patch.object(reviews, "_clipboard", return_value=PACK_TEXT), self.assertRaises(SystemExit) as e:
            reviews.add(self.d)
        self.assertIn("review pack", str(e.exception))
        with mock.patch.object(reviews, "_clipboard", return_value="  \n"), self.assertRaises(SystemExit):
            reviews.add(self.d)

    def test_a_pasted_reply_settles_the_send_waiting_on_it(self):
        self.full_pack()
        reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertEqual(len(reviews.pending(self.d)), 1)
        p = reviews.add(self.d, text=REPLY)
        fm, _ = reviews._parse(p)
        self.assertEqual((fm["turn"], fm["chat_url"]), (1, "https://chatgpt.com/c/chat-1"))
        self.assertEqual(reviews.pending(self.d), [])


class Send(Base):
    def test_full_pack_to_a_new_chat_then_followup_to_the_same_chat(self):
        first = self.full_pack()
        res = reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertTrue(res["sent"])
        self.assertEqual(res["pack"], str(first))
        self.assertEqual(self.cg.sent[-1]["title"], "Johto review · t1 validate-1 turn 1 · acme/widget")
        self.assertEqual(self.cg.sent[-1]["tag"], "Johto t1 validate-1 t1")
        self.assertIsNone(res["why"])
        self.assertIsNone(self.cg.sent[-1]["chat_url"])
        self.assertEqual(self.cg.sent[-1]["account"], reviews.settings.get("chatgpt_account"))
        self.assertEqual(reviews.chat_for(self.d, "validate-1"), "https://chatgpt.com/c/chat-1")
        p = reviews.pending(self.d)
        self.assertEqual([(e["run"], e["turn"], e["pack"], e["state"]) for e in p],
                         [("validate-1", 1, f"runs/validate-1/{first.name}", "sent")])
        self.assertNotIn("pid", p[0])
        self.assertTrue(delta.pack_record(self.d, "validate-1", first)["sent_at"])
        self.assertEqual(stages.last(self.d, "review_sent")["chat_url"], "https://chatgpt.com/c/chat-1")

        # A second send while the first review is still being written does nothing.
        busy = reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertFalse(busy["sent"])
        self.assertEqual(busy["why"], "busy")
        self.assertIn("isn't back yet", busy["reason"])

        self.cg.replies["https://chatgpt.com/c/chat-1"] = {"status": "done", "markdown": REPLY, "message_id": "m1"}
        saved = reviews.fetch_pending(self.d, log=self.say)
        self.assertEqual([x.name for x in saved], ["validate-1-t01-gptpro.md"])

        # Same pack again: refused, nothing new.
        again = reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertFalse(again["sent"])
        self.assertEqual(again["why"], "nothing_new")
        self.assertIn("nothing new", again["reason"])
        self.assertEqual(len(self.cg.sent), 1)

        # A new turn: a follow-up into the same chat.
        self.write_run(TURN1 + TURN2, DIFF2)
        res = reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertTrue(res["sent"])
        self.assertEqual(res["kind"], "followup")
        self.assertEqual(self.cg.sent[-1]["chat_url"], "https://chatgpt.com/c/chat-1")
        self.assertEqual(self.cg.sent[-1]["title"], "Johto review · t1 validate-1 turn 2 · acme/widget")
        self.assertTrue(Path(res["pack"]).name.startswith("review-followup-"))

    def test_no_send_records_nothing(self):
        self.full_pack()
        res = reviews.send_review(self.d, "validate-1", no_send=True, log=self.say)
        self.assertFalse(res["sent"])
        self.assertTrue(self.cg.sent[-1]["no_send"])
        self.assertEqual(reviews.pending(self.d), [])
        self.assertIsNone(reviews.chat_for(self.d, "validate-1"))
        self.assertIsNone(stages.last(self.d, "review_sent"))

    def test_low_usage_never_stops_a_send(self):
        # Austin, 2026-11-01: reviews always go out, and no low-usage warning unless he sets one.
        self.full_pack()
        self.cg.usage_left = 5
        res = reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertTrue(res["sent"])
        self.assertFalse(any("Pro usage" in line for line in self.log))

    def test_a_warning_he_sets_is_only_a_warning(self):
        self.full_pack()
        self.cg.usage_left = 5
        real = reviews.settings.get
        with mock.patch.object(reviews.settings, "get", lambda k: 20 if k == "chatgpt_warn_usage_pct" else real(k)):
            res = reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertTrue(res["sent"])
        self.assertTrue(any("5% of this week's Pro usage" in line for line in self.log))

    def clicked(self, landed: str | None):
        """A send whose Send click didn't show working; `landed` is what the chat then holds."""
        def send(text, title, **kw):
            url = "https://chatgpt.com/c/maybe"
            kw["on_chat"](url)
            self.cg.chats[url] = text if landed == "pack" else landed
            e = StubChatGPT.ChatGPTError("Clicked Send, but ChatGPT didn't show it working.")
            e.chat_url, e.maybe_sent = url, True
            raise e
        return mock.patch.object(self.cg, "send", send)

    def test_a_send_that_may_have_gone_is_not_sent_twice(self):
        first = self.full_pack()
        with self.clicked("pack"), self.assertRaises(StubChatGPT.ChatGPTError):
            reviews.send_review(self.d, "validate-1", log=self.say)
        e = reviews.pending(self.d)[0]
        self.assertEqual((e["pack"], e["chat_url"], e["state"]),
                         (f"runs/validate-1/{first.name}", "https://chatgpt.com/c/maybe", "unconfirmed"))
        self.assertIn("didn't show it working", e["unconfirmed"])
        self.assertIsNone(reviews.chat_for(self.d, "validate-1"))     # not until the chat shows it
        self.assertEqual(stages.last(self.d, "review_unconfirmed")["chat_url"], "https://chatgpt.com/c/maybe")
        # The retry reads the chat first: the pack is there, so it waits on the reply instead of resending.
        again = reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertEqual((again["sent"], again["why"]), (False, "busy"))
        self.assertIn("isn't back yet", again["reason"])
        self.assertEqual(self.cg.sent, [])
        self.assertEqual(self.cg.inspected, ["https://chatgpt.com/c/maybe"])
        e = reviews.pending(self.d)[0]
        self.assertEqual(e["state"], "sent")
        self.assertNotIn("unconfirmed", e)
        self.assertEqual(reviews.chat_for(self.d, "validate-1"), "https://chatgpt.com/c/maybe")
        self.assertTrue(delta.pack_record(self.d, "validate-1", first)["sent_at"])
        self.assertTrue(stages.last(self.d, "review_sent")["settled"])
        self.assertTrue(any("did reach GPT Pro" in line for line in self.log))

    def test_a_send_that_never_landed_is_cleared_and_sent_again(self):
        first = self.full_pack()
        with self.clicked("Reply with exactly this title and nothing else: Johto t1 validate-1 t1"), \
                self.assertRaises(StubChatGPT.ChatGPTError):
            reviews.send_review(self.d, "validate-1", log=self.say)
        res = reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertTrue(res["sent"])
        self.assertEqual(res["chat_url"], "https://chatgpt.com/c/chat-1")   # a new chat, not the one with only the tag
        self.assertEqual(stages.last(self.d, "review_send_cleared")["why"], "the pack isn't in the chat")
        self.assertEqual([e["chat_url"] for e in reviews.pending(self.d)], ["https://chatgpt.com/c/chat-1"])
        self.assertEqual(delta.pack_record(self.d, "validate-1", first)["chat_url"], "https://chatgpt.com/c/chat-1")

    def test_a_chat_that_cannot_be_read_keeps_the_entry(self):
        self.full_pack()
        with self.clicked("pack"), self.assertRaises(StubChatGPT.ChatGPTError):
            reviews.send_review(self.d, "validate-1", log=self.say)
        self.cg.chats["https://chatgpt.com/c/maybe"] = StubChatGPT.ChatGPTError("tab closed")
        again = reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertEqual(again["why"], "busy")
        e = reviews.pending(self.d)[0]
        self.assertEqual((e["state"], e["error"]), ("unconfirmed", "tab closed"))

    def sending(self, **kw) -> dict:
        first = self.full_pack()
        e = {"id": "e1", "run": "validate-1", "turn": 1, "kind": "full", "pack": f"runs/validate-1/{first.name}",
             "chat_url": None, "sent_at": stages.now_iso(), "started_at": stages.now_iso(), "title": "t",
             "tag": "Johto t1 validate-1 t1", "state": "sending", "pid": 999_999_9, **kw}
        reviews.add_pending(self.d, e)
        return e

    def test_a_send_cut_off_before_a_chat_is_cleared(self):
        self.sending()
        res = reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertTrue(res["sent"])
        self.assertEqual(self.cg.inspected, [])               # nothing to read: no chat was opened
        self.assertEqual([e["state"] for e in reviews.pending(self.d)], ["sent"])

    def test_a_send_cut_off_after_the_click_is_found_in_the_chat(self):
        e = self.sending(chat_url="https://chatgpt.com/c/cut")
        self.cg.chats["https://chatgpt.com/c/cut"] = (self.d / e["pack"]).read_text()
        res = reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertEqual(res["why"], "busy")
        self.assertEqual(reviews.pending(self.d)[0]["state"], "sent")
        self.assertEqual(reviews.chat_for(self.d, "validate-1"), "https://chatgpt.com/c/cut")

    def test_a_send_under_way_elsewhere_is_left_alone(self):
        self.sending(pid=os.getpid(), chat_url="https://chatgpt.com/c/now")
        res = reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertEqual((res["why"], res["reason"]), ("busy", "the review of review-pack-20261030-101500.xml is being sent now"))
        self.assertEqual(self.cg.inspected, [])

    def test_pending_says_sending_before_chatgpt_starts(self):
        self.full_pack()
        seen = []

        def watching(text, title, **kw):
            seen.append(dict(reviews.pending(self.d)[0]))
            kw["on_chat"]("https://chatgpt.com/c/live")
            seen.append(dict(reviews.pending(self.d)[0]))
            return {"chat_url": "https://chatgpt.com/c/live", "sent": True, "chars": len(text), "usage_pct": 50}
        with mock.patch.object(self.cg, "send", watching):
            reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertEqual([(x["state"], x["chat_url"]) for x in seen],
                         [("sending", None), ("sending", "https://chatgpt.com/c/live")])
        self.assertEqual(seen[0]["pid"], os.getpid())
        self.assertEqual(seen[0]["pack_sha"], StubChatGPT.text_sha("full pack"))
        self.assertEqual(reviews.pending(self.d)[0]["state"], "sent")

    def test_a_partial_pack_is_not_sent_unless_forced(self):
        first = self.full_pack()
        delta.update_pack(self.d, "validate-1", first, partial="PARTIAL: 2 hunks rejected in a.go")
        res = reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertEqual((res["sent"], res["why"]), (False, "partial"))
        self.assertIn("2 hunks rejected in a.go", res["reason"])
        self.assertEqual(self.cg.sent, [])
        self.assertTrue(reviews.send_review(self.d, "validate-1", log=self.say, force=True)["sent"])

    def test_no_chatgpt_module_is_a_normal_error(self):
        self.full_pack()
        with mock.patch.dict(sys.modules, {"johto.chatgpt": None}):
            with self.assertRaises(reviews.ChatGPTUnavailable) as cm:
                reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertNotIsInstance(cm.exception, SystemExit)
        self.assertEqual(reviews.pending(self.d), [])

    def test_an_old_send_expires(self):
        self.full_pack()
        reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertEqual(reviews.expire_pending(self.d, log=self.say), [])
        old = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - 4 * 3600))
        reviews._mark_pending(self.d, reviews.pending(self.d)[0], sent_at=old)
        gone = reviews.expire_pending(None, log=self.say)
        self.assertEqual([(g["task"], g["run"], g["turn"]) for g in gone], [("t1", "validate-1", 1)])
        self.assertEqual(reviews.pending(self.d), [])
        self.assertEqual(stages.last(self.d, "review_expired")["state"], "sent")

    def test_a_failure_before_send_records_nothing(self):
        self.full_pack()

        def refused(*a, **k):
            e = StubChatGPT.ChatGPTError("The pack in the edit box doesn't match the pack.")
            e.chat_url, e.maybe_sent = "https://chatgpt.com/c/titled", False
            raise e
        with mock.patch.object(self.cg, "send", refused):
            with self.assertRaises(StubChatGPT.ChatGPTError):
                reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertEqual(reviews.pending(self.d), [])
        self.assertIsNone(stages.last(self.d, "review_sent"))

    def test_first_send_without_a_pack_builds_a_full_one(self):
        built = []

        def fake_build(task_dir, run_name=None, max_tokens=pack.MAX_TOKENS, ai=True, log=print):
            built.append(ai)
            return self.full_pack()
        with mock.patch.object(pack, "build", fake_build):
            res = reviews.send_review(self.d, "validate-1", log=self.say)
        self.assertEqual(len(built), 1)
        self.assertTrue(res["sent"])


class Fetch(Base):
    def setUp(self):
        super().setUp()
        self.full_pack()
        reviews.send_review(self.d, "validate-1", log=self.say)
        self.chat = reviews.chat_for(self.d, "validate-1")

    def test_thinking_stays_pending(self):
        self.assertEqual(reviews.fetch_pending(self.d, log=self.say), [])
        e = reviews.pending(self.d)[0]
        self.assertEqual(e["last_status"], "thinking")
        self.assertTrue(e["checked_at"])

    def test_an_old_reply_is_not_taken_for_the_new_one(self):
        reviews.save(self.d, "validate-1", 0, REPLY, message_id="m0")
        self.cg.replies[self.chat] = {"status": "done", "markdown": REPLY, "message_id": "m0"}
        self.assertEqual(reviews.fetch_pending(self.d, log=self.say), [])
        self.assertEqual(len(reviews.pending(self.d)), 1)

    def test_the_title_answer_is_not_a_review(self):
        # The pack never went: the chat's newest reply is the Instant answer to the tag message.
        for said in (reviews.pending(self.d)[0]["title"], reviews.pending(self.d)[0]["tag"]):
            self.cg.replies[self.chat] = {"status": "done", "markdown": f'"{said}."', "message_id": "t1"}
            self.assertEqual(reviews.fetch_pending(self.d, log=self.say), [])
            self.assertEqual(len(reviews.pending(self.d)), 1)

    def test_fetch_settles_an_unconfirmed_send_then_saves_its_reply(self):
        e = reviews.pending(self.d)[0]
        reviews._update(self.d, e, {"state": "unconfirmed", "unconfirmed": "no sign"})
        self.cg.replies[self.chat] = {"status": "done", "markdown": REPLY, "message_id": "m1"}
        saved = reviews.fetch_pending(self.d, log=self.say)
        self.assertEqual(len(saved), 1)
        self.assertEqual(self.cg.inspected, [self.chat])

    def test_errors_are_logged_and_kept(self):
        self.cg.replies[self.chat] = StubChatGPT.ChatGPTError("tab closed")
        self.assertEqual(reviews.fetch_pending(self.d, log=self.say), [])
        self.assertEqual(reviews.pending(self.d)[0]["last_status"], "error")
        self.assertTrue(any("tab closed" in line for line in self.log))

    def test_done_is_saved_for_every_task(self):
        self.cg.replies[self.chat] = {"status": "done", "markdown": REPLY, "message_id": "m1"}
        saved = reviews.fetch_pending(None, log=self.say)
        self.assertEqual(len(saved), 1)
        fm, text = reviews._parse(saved[0])
        self.assertEqual((fm["chat_url"], fm["message_id"], fm["turn"]), (self.chat, "m1", 1))
        self.assertEqual(reviews.pending(self.d), [])
        self.assertEqual(stages.last(self.d, "review_saved")["path"], f"reviews/{saved[0].name}")

    def test_resolve_needs_a_waiting_send(self):
        with self.assertRaises(ValueError):
            reviews.resolve_pending(self.d, "https://chatgpt.com/c/other", REPLY)


class Findings(Base):
    BLOCK = ('## Digest\n\nShort.\n\n## Findings\n\n| ID | Finding |\n|---|---|\n| F1 | x |\n\n## Detail\n\n...\n\n'
             '```findings\n[{"id": "F1", "finding": "Requeue resets attempts.", "kind": "defect", "status": "new",'
             ' "severity": "high", "result": "missed", "basis": "traced", "refs": ["queue/queue.go:4"],'
             ' "limits": "not run", "supersedes": null},\n {"id": "F2", "finding": "Backoff is credited.",'
             ' "kind": "credit", "status": "new", "severity": "low", "result": "credit", "basis": "executed",'
             ' "refs": [], "limits": "none", "supersedes": null},]\n```\n')

    def test_block_is_read_even_with_a_trailing_comma(self):
        got = reviews.findings(self.BLOCK)
        self.assertEqual([(f["id"], f["status"], f["basis"]) for f in got], [("F1", "new", "traced"), ("F2", "new", "executed")])

    def test_missing_or_broken_blocks_give_nothing(self):
        self.assertEqual(reviews.findings("## Review\n\nNo block here."), [])
        self.assertEqual(reviews.findings("```findings\n[{not json\n```"), [])
        self.assertEqual(reviews.findings(""), [])

    def test_a_real_reply(self):
        text = (Path(__file__).parent / "fixtures" / "grading" / "demo-validate-1-t01-gptpro.md").read_text()
        got = reviews.findings(text)
        self.assertGreaterEqual(len(got), 10)
        self.assertEqual(got[0]["id"], "F1")
        self.assertEqual(got[0]["basis"], "isolated")       # the qualification survives

    def test_last_findings_merge_by_id(self):
        reviews.save(self.d, "validate-1", 1, self.BLOCK)
        follow = '## Digest\n\nF1 fixed.\n\n```findings\n[{"id": "F1", "status": "resolved"}, {"id": "F3", "finding": "New gap.", "status": "new"}]\n```\n'
        reviews.save(self.d, "validate-1", 2, follow)
        reviews.save(self.d, "validate-1", 2, "```findings\n[{\"id\": \"F9\", \"finding\": \"other\"}]\n```", source="claude")
        got = reviews.last_findings(self.d, "validate-1")
        self.assertEqual([(f["id"], f["status"]) for f in got], [("F1", "resolved"), ("F2", "new"), ("F3", "new")])
        self.assertEqual(got[0]["finding"], "Requeue resets attempts.")
        self.assertEqual(got[0]["review"], "validate-1-t02-gptpro.md")
        self.assertEqual(reviews.last_findings(self.d, "calib-1"), [])


class Cli(Base):
    def run_cli(self, *argv) -> str:
        p = argparse.ArgumentParser()
        reviews.register(p.add_subparsers(dest="cmd", required=True))
        a = p.parse_args(list(argv))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            a.func(a)
        return buf.getvalue()

    def test_add_list_show(self):
        self.full_pack()
        f = self.root / "reply.md"
        f.write_text(REPLY)
        out = self.run_cli("review", "add", "t1", "--file", str(f))
        self.assertIn("Saved tasks/t1/reviews/validate-1-t01-gptpro.md (validate-1, turn 1", out)
        self.assertIn("Task Success evidence", out)
        self.assertIn("Already saved as", self.run_cli("review", "add", "t1", "--file", str(f)))
        self.assertIn("validate-1-t01-gptpro.md", self.run_cli("review", "list", "t1"))
        shown = self.run_cli("review", "show", "t1", "validate-1-t01")
        self.assertIn("queue/queue.go:4", shown)
        self.assertNotIn("sha:", shown)

    def test_pack_prints_the_path_and_leaves_the_clipboard_alone(self):
        first = self.full_pack()
        with mock.patch("subprocess.run") as run:
            out = self.run_cli("review", "pack", "t1")
        self.assertEqual(out.strip().splitlines()[-1], str(first))
        self.assertFalse(any(c.args and c.args[0] and c.args[0][0] == "pbcopy" for c in run.call_args_list))

    def test_send_and_fetch(self):
        self.full_pack()
        self.assertIn("Sent review-pack-", self.run_cli("review", "send", "t1"))
        self.assertIn("waiting: validate-1 turn 1", self.run_cli("review", "list", "t1"))
        self.assertEqual(self.run_cli("review", "fetch", "t1").strip(), "No replies ready yet.")
        self.cg.replies["https://chatgpt.com/c/chat-1"] = {"status": "done", "markdown": REPLY, "message_id": "m1"}
        self.run_cli("review", "fetch")
        self.assertEqual(json.loads((reviews.reviews_dir(self.d) / "pending.json").read_text()), [])


    def test_send_without_chatgpt_exits_with_a_message(self):
        self.full_pack()
        with mock.patch.dict(sys.modules, {"johto.chatgpt": None}), self.assertRaises(SystemExit) as cm:
            self.run_cli("review", "send", "t1")
        self.assertIn("ChatGPT automation isn't available", str(cm.exception))

    def test_pack_ai_flag(self):
        with mock.patch.object(delta, "next_pack", return_value=Path("/x.xml")) as np:
            self.run_cli("review", "pack", "t1", "--ai")
            self.run_cli("review", "pack", "t1")
        self.assertEqual([c.kwargs["ai"] for c in np.call_args_list], [True, None])


if __name__ == "__main__":
    unittest.main()


class SendRace(Base):
    def test_two_senders_send_one_review(self):
        """The turn watcher and `johto review send` for the same run at once: one send, the other is told busy."""
        out = self.full_pack()
        slow = self.cg.send

        def send(*a, **kw):
            time.sleep(0.3)
            return slow(*a, **kw)
        self.cg.send = send
        real_pending = reviews.pending

        def pending_then_yield(d):   # the gap between the busy check and add_pending
            rows = real_pending(d)
            time.sleep(0.05)
            return rows
        res = []
        with mock.patch.object(reviews, "pending", pending_then_yield):
            ts = [threading.Thread(target=lambda: res.append(reviews.send_review(self.d, "validate-1", pack=out,
                                                                                  log=lambda *a: None)))
                  for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(len([x for x in self.cg.sent if not x["no_send"]]), 1)
        self.assertEqual(sorted(str(r.get("why")) for r in res), ["None", "busy"])

    def test_the_same_checkpoint_in_another_pack_is_nothing_new(self):
        first = self.full_pack()
        reviews.send_review(self.d, "validate-1", pack=first, log=self.say)
        reviews._set_pending(self.d, lambda rows: [])   # its reply came back and was handled
        again = self.full_pack("review-pack-20261030-101501.xml")   # a second build of the same checkpoint
        res = reviews.send_review(self.d, "validate-1", pack=again, log=self.say)
        self.assertEqual(res["why"], "nothing_new")
        self.assertIn("the same checkpoint went as review-pack-20261030-101500.xml", res["reason"])


class ExpireAfterFetch(Base):
    def test_a_reply_that_landed_overnight_is_saved(self):
        """The Mac slept 8 hours; GPT Pro finished long ago. The watcher reads the chat before it gives up."""
        from johto import turnwatch
        out = self.full_pack()
        reviews.send_review(self.d, "validate-1", pack=out, log=lambda *a: None)
        chat = reviews.chat_for(self.d, "validate-1")
        old = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - 8 * 3600))
        reviews._set_pending(self.d, lambda rows: [dict(r, sent_at=old, started_at=old) for r in rows])
        self.cg.replies[chat] = {"status": "done", "markdown": "## Digest\n\nThe overnight review.\n", "message_id": "m9"}
        t = turnwatch._Tick(state={}, sets={}, dry_run=False, log=lambda *a: None)
        with mock.patch.object(turnwatch, "save_state", lambda s: None), \
                mock.patch.object(turnwatch, "_notify", lambda *a, **k: None):
            turnwatch._fetch(t)
        self.assertEqual(len(reviews.list_reviews(self.d)), 1, [a["action"] for a in t.actions])
        self.assertNotIn("review_expired", [a["action"] for a in t.actions])

    def test_a_chat_that_couldnt_be_read_waits(self):
        out = self.full_pack()
        reviews.send_review(self.d, "validate-1", pack=out, log=lambda *a: None)
        old = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - 4 * 3600))
        reviews._set_pending(self.d, lambda rows: [dict(r, sent_at=old, last_status="error") for r in rows])
        self.assertEqual(reviews.expire_pending(self.d, log=self.say), [])
        older = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - 13 * 3600))
        reviews._set_pending(self.d, lambda rows: [dict(r, sent_at=older) for r in rows])
        self.assertEqual(len(reviews.expire_pending(self.d, log=self.say)), 1)
