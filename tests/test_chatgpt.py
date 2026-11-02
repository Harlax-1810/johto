"""ChatGPT Pro automation (johto/chatgpt.py): pack chunks, text checks, usage, reply parsing, the send flow
against a fake browser, and the real step scripts run against a small simulated page. Nothing here
touches Chrome. The node tests (JS syntax, J.norm, J.md) skip when node isn't installed.

Run: PYTHONPATH=.:tests .venv/bin/python -m unittest tests.test_chatgpt -v
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from johto import chatgpt, config, settings  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "chatgpt"
ACCT = "you@example.com"
CID = "8b1f0c9e-2d47-4a6b-9c31-5e7d2a1f0b64"
CHAT = f"https://chatgpt.com/c/{CID}"
EMOJI = chr(0x1F600)


def conversation() -> dict:
    return json.loads((FIX / "conversation_done.json").read_text())


def expected_reply() -> str:
    return (FIX / "reply_done.md").read_text(encoding="utf-8").strip()


class DataDir(unittest.TestCase):
    """Each test gets its own data/ (lock, tab record, settings)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="johto-gpt-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(config, "DATA", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict(settings.DEFAULTS, {"chatgpt_account": ACCT})
        p.start()
        self.addCleanup(p.stop)
        self.logs: list[str] = []

    def settings(self, **kv):
        (self.tmp / "settings.json").write_text(json.dumps(kv))


# --- Text checks -----------------------------------------------------------------------------------

class TestText(unittest.TestCase):
    def test_autolink_space_is_tolerated(self):
        self.assertTrue(chatgpt.same_text('url = f"https://x.test/a"', 'url = f" https://x.test/a"'))
        self.assertTrue(chatgpt.same_text("see\nhttps://a.test", "see https://a.test"))

    def test_whitespace_is_collapsed(self):
        self.assertTrue(chatgpt.same_text("a\n\n  b\tc\r\n", "a b c"))
        self.assertTrue(chatgpt.same_text("x" + chr(0xA0) + "y" + chr(0x2028) + "z", "x y z"))
        self.assertEqual(chatgpt.normalize("  lead and trail \n"), "lead and trail")

    def test_real_differences_are_caught(self):
        text = "".join(f"line {i}: some pack text with https://example.test/{i}\n" for i in range(400))
        self.assertFalse(chatgpt.same_text(text, text[:5000] + text[5100:]))      # a dropped piece
        half = len(text) // 2
        self.assertFalse(chatgpt.same_text(text, text[half:] + text[:half]))      # chunks out of order
        self.assertFalse(chatgpt.same_text("abc", "abd"))
        self.assertFalse(chatgpt.same_text("a b", "ab"))

    def test_js_len_counts_utf16(self):
        self.assertEqual(chatgpt.js_len("a" + EMOJI), 3)
        self.assertEqual(chatgpt.js_len("é"), 1)

    def test_clean_text_replaces_lone_surrogates(self):
        self.assertEqual(chatgpt.clean_text("a" + chr(0xD800) + "b"), "a" + chr(0xFFFD) + "b")

    def test_title_message_asks_for_the_title_back(self):
        self.assertEqual(chatgpt.title_message("Johto review · t1 validate-1 turn 2"),
                         "Reply with exactly this title and nothing else: Johto review · t1 validate-1 turn 2")

    def test_usage_parsing(self):
        self.assertEqual(chatgpt.parse_usage("8% usage remaining · Resets every week · Next reset is on Nov 3 at 11:37 PM"), 8)
        self.assertEqual(chatgpt.parse_usage("Upgrade\n100% usage remaining"), 100)
        self.assertIsNone(chatgpt.parse_usage(None))
        self.assertIsNone(chatgpt.parse_usage(""))
        self.assertIsNone(chatgpt.parse_usage("Usage: 8%"))

    def test_chat_links(self):
        self.assertEqual(chatgpt.conversation_id(CHAT), CID)
        self.assertEqual(chatgpt.conversation_id(f"https://chatgpt.com/g/g-p-abc-johto/c/{CID}?model=x"), CID)
        self.assertIsNone(chatgpt.conversation_id("https://chatgpt.com/"))
        self.assertIsNone(chatgpt.conversation_id(None))
        self.assertEqual(chatgpt.canonical_url(CHAT + "?model=gpt#x"), CHAT)
        self.assertIsNone(chatgpt.canonical_url("https://chatgpt.com/"))


# --- Chunks ----------------------------------------------------------------------------------------

class TestChunks(unittest.TestCase):
    def assert_chunks_ok(self, text: str):
        parts = chatgpt.chunks(text)
        self.assertEqual("".join(parts), text)
        for p in parts:
            self.assertTrue(p)
            self.assertLessEqual(len(p), chatgpt.CHUNK_CHARS)
            self.assertLessEqual(chatgpt.request_bytes(chatgpt.chunk_expr(p)), chatgpt.LINE_LIMIT)
        # What the page would hold after the pushes: same text, length and checksum.
        fake = chatgpt.FakeBrowser()
        res = fake._load({"exprs": [chatgpt.chunk_expr(p) for p in parts]})
        chatgpt.check_loaded(res, text, len(parts))
        return parts

    def test_worst_case_characters_stay_under_the_line_limit(self):
        for text in ('"' * 40_000, "\\" * 40_000, "é" * 40_000, EMOJI * 30_000, "\x01" * 30_000,
                     "\n" * 40_000, chr(0x2028) * 20_000):
            with self.subTest(sample=repr(text[:2])):
                self.assert_chunks_ok(text)

    def test_mixed_text(self):
        rng = random.Random(7)
        alphabet = ["a", " ", "\n", '"', "\\", "é", "中", EMOJI, "\t", "<", "{", "\x7f"]
        self.assert_chunks_ok("".join(rng.choice(alphabet) for _ in range(120_000)))

    def test_ascii_pack_uses_full_chunks(self):
        parts = self.assert_chunks_ok("x" * 705_000)
        self.assertEqual(len(parts), 47)
        self.assertTrue(all(len(p) == chatgpt.CHUNK_CHARS for p in parts))

    def test_empty_text(self):
        self.assertEqual(chatgpt.chunks(""), [])

    def test_real_review_packs(self):
        root = Path(os.environ.get("JOHTO_ROOT") or config.ROOT) / "tasks"
        packs = sorted(root.glob("*/runs/*/review-*.xml"))
        if not packs:
            self.skipTest(f"no review packs under {root}")
        for p in packs:
            with self.subTest(pack=str(p.relative_to(root))):
                self.assert_chunks_ok(p.read_text(encoding="utf-8"))

    def test_load_check_refuses_wrong_pages(self):
        text = "abc" + EMOJI
        good = {"length": chatgpt.js_len(text), "chunks": 1, "sha256": hashlib.sha256(text.encode()).hexdigest()}
        chatgpt.check_loaded(good, text, 1)
        for bad in ({**good, "chunks": 0}, {**good, "length": 4}, {**good, "sha256": "0" * 64}):
            with self.assertRaises(chatgpt.ChatGPTError):
                chatgpt.check_loaded(bad, text, 1)


# --- Reading a conversation ------------------------------------------------------------------------

class TestConversation(unittest.TestCase):
    FINAL = "d4e5f6a7-4444-4444-8444-000000000040"

    def node(self, conv, msg_id):
        return conv["mapping"][msg_id]["message"]

    def test_finished_pro_reply(self):
        r = chatgpt.latest_reply(conversation())
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["message_id"], self.FINAL)
        self.assertEqual(r["markdown"], expected_reply())    # citation marker stripped, no reasoning text
        self.assertNotIn("The filter can shorten a page", r["markdown"])

    def test_walk_stops_at_the_edited_message(self):
        user, after = chatgpt.chain_after_last_user(conversation())
        self.assertEqual(user["id"], "b2c3d4e5-2222-4222-8222-000000000020")
        self.assertEqual([m["content"]["content_type"] for m in after],
                         ["thoughts", "code", "execution_output", "reasoning_recap", "text"])

    def test_still_thinking(self):
        conv = conversation()
        for node in ("b2c3d4e5-2222-4222-8222-000000000020", "c3d4e5f6-3333-4333-8333-000000000030",
                     "c3d4e5f6-3333-4333-8333-000000000032", "c3d4e5f6-3333-4333-8333-000000000033"):
            conv["current_node"] = node
            with self.subTest(current=node):
                r = chatgpt.latest_reply(conv)
                self.assertEqual(r["status"], "thinking")
                self.assertIsNone(r["markdown"])

    def test_streaming_reply_is_thinking(self):
        conv = conversation()
        self.node(conv, self.FINAL)["status"] = "in_progress"
        self.node(conv, self.FINAL)["end_turn"] = None
        r = chatgpt.latest_reply(conv)
        self.assertEqual(r["status"], "thinking")
        self.assertTrue(r["markdown"])          # partial text is still returned

    def test_reply_that_does_not_end_the_turn(self):
        conv = conversation()
        self.node(conv, self.FINAL)["end_turn"] = False
        self.assertEqual(chatgpt.latest_reply(conv)["status"], "thinking")

    def test_interrupted_reply_is_an_error(self):
        conv = conversation()
        msg = self.node(conv, self.FINAL)
        msg["status"] = "finished_partial_completion"
        msg["metadata"]["finish_details"] = {"type": "interrupted"}
        r = chatgpt.latest_reply(conv)
        self.assertEqual(r["status"], "error")
        self.assertTrue(r["markdown"])

    def test_follows_current_node_to_another_branch(self):
        conv = conversation()
        conv["current_node"] = "a1b2c3d4-1111-4111-8111-000000000011"
        r = chatgpt.latest_reply(conv)
        self.assertEqual((r["status"], r["markdown"]), ("done", conv["title"]))

    def test_multimodal_parts_and_hidden_messages(self):
        after = [
            {"id": "h", "author": {"role": "assistant"}, "recipient": "all", "status": "finished_successfully",
             "content": {"content_type": "text", "parts": ["hidden"]},
             "metadata": {"is_visually_hidden_from_conversation": True}},
            {"id": "m", "author": {"role": "assistant"}, "recipient": "all", "status": "finished_successfully",
             "end_turn": True, "metadata": {},
             "content": {"content_type": "multimodal_text",
                         "parts": [{"content_type": "image_asset_pointer", "asset_pointer": "file-service://x"},
                                   "The review text."]}},
        ]
        r = chatgpt.reply_from_chain(after)
        self.assertEqual((r["status"], r["markdown"], r["message_id"]), ("done", "The review text.", "m"))

    def test_empty_chain(self):
        self.assertEqual(chatgpt.reply_from_chain([])["status"], "thinking")


# --- Scripts ---------------------------------------------------------------------------------------

STEP_ARGS = dict(url=chatgpt.NEW_CHAT, conversation_id=None, target=4, escape=False,
                 message="m", title="t", exprs=[], js_len=0)


class TestScripts(unittest.TestCase):
    def test_every_step_script_compiles_and_round_trips(self):
        for step in chatgpt.STEPS:
            with self.subTest(step=step):
                script = chatgpt.build_script(step, account=ACCT, target_id="T1", timeout=30, **STEP_ARGS)
                compile(script, step, "exec")
                got_step, args = chatgpt.parse_script(script)
                self.assertEqual((got_step, args["target_id"], args["account"]), (step, "T1", ACCT))

    def test_pack_text_survives_the_args_line(self):
        text = 'line "one"\nline two \\ ' + EMOJI + chr(0x2028) + "é"
        script = chatgpt.build_script("load", account=ACCT, target_id=None, timeout=30,
                                      exprs=[chatgpt.chunk_expr(text)])
        self.assertEqual(chatgpt.parse_script(script)[1]["exprs"], [chatgpt.chunk_expr(text)])

    def test_every_page_call_fits_the_harness_line(self):
        consts = 'const TITLE = "%s";\n' % ("t" * 200)
        for name, body in chatgpt.JS.items():
            src = "(async () => { /*" + name + "*/\n" + chatgpt.LIB_JS + "\n" + consts + body + "})()"
            self.assertLess(chatgpt.request_bytes(src), chatgpt.LINE_LIMIT, name)

    def test_parse_result_without_mark(self):
        with self.assertRaisesRegex(chatgpt.ChatGPTError, "no answer"):
            chatgpt.parse_result("Traceback ...", "open")


# --- A simulated page for running the real step scripts --------------------------------------------

class PageSim:
    """Answers the CDP calls the generated scripts make, with a ChatGPT page reduced to the parts they
    read: the model picker, the edit box, the pack array. Page snippets are recognized by name."""

    def __init__(self):
        self.tabs = {"T1": {"targetId": "T1", "type": "page", "url": "https://chatgpt.com/", "browserContextId": "C1"}}
        self.href = "https://chatgpt.com/"
        self.logged_in = True
        self.usage_text = "8% usage remaining · Resets every week"
        self.model = 2                  # the slider's value; self.picker says whether the picker is open
        self.picker = False
        self.cdp_keys_work = True       # False: a background tab that ignores CDP key events
        self.edit_open = True
        self.box = "Reply with exactly this title and nothing else: t"
        self.pack: list[str] = []
        self.calls: list[str] = []
        self.created: list[str] = []
        self.no_window = False
        self.composer = ""
        self.edits = self.md = self.hits = 0
        self.stop = False
        self.api = None                 # what the conversation API answers (None: the fixture)
        self.dom = {"stop": False, "found": True, "markdown": "## Review\n\nFrom the page.", "message_id": "m-dom"}

    # helpers the scripts call
    def cdp(self, method, session_id=None, **p):
        self.calls.append(method)
        if method == "Target.getTargets":
            return {"targetInfos": list(self.tabs.values())}
        if method == "Target.attachToTarget":
            return {"sessionId": "S-" + p["targetId"]}
        if method == "Target.closeTarget":
            self.tabs.pop(p["targetId"], None)
            return {}
        if method == "Page.navigate":
            self.href = p["url"]
            self.loader = f"L{len(self.calls)}"
            return {"frameId": "F1", "loaderId": self.loader}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F1", "loaderId": getattr(self, "loader", "L0")}}}
        if method == "Input.dispatchKeyEvent":
            if p["type"] == "keyDown" and self.cdp_keys_work:
                self.press(p["key"], p.get("modifiers", 0))
            return {}
        if method == "Input.insertText":
            self.composer += p["text"]
            return {}
        if method == "Runtime.evaluate":
            return {"result": {"value": self.evaluate(p["expression"])}}
        return {}

    def new_tab(self, url, account=None):
        if self.no_window:
            raise RuntimeError(f"No open Chrome window for {account}. Open profiles: []")
        tid = f"T{len(self.tabs) + 1}"
        self.tabs[tid] = {"targetId": tid, "type": "page", "url": url, "browserContextId": "C1"}
        self.created.append(tid)
        self.href = url
        return tid

    def press(self, key, mods=0):
        if key in ("ArrowRight", "ArrowLeft") and self.picker:
            self.model = max(0, min(4, self.model + (1 if key == "ArrowRight" else -1)))
        elif key == "Escape":
            self.picker = False
        elif key == "a" and mods & 4:
            self.selected = True
        elif key == "Backspace" and getattr(self, "selected", False):
            self.box, self.selected = "", False

    def evaluate(self, expr):
        if expr.startswith("window.__johtoPack.push("):
            self.pack.append(json.loads(expr[len("window.__johtoPack.push("):-1]))
            return len(self.pack)
        if expr == "location.href":
            return self.href
        name = re.match(r"\(async \(\) => \{ /\*(\w+)\*/", expr).group(1)
        consts = {}
        for k, v in re.findall(r"^const ([A-Z]+) = (.*);$", expr, re.M):
            try:
                consts[k] = json.loads(v)
            except ValueError:
                pass                     # LIB's own regex constant
        return json.dumps(getattr(self, "js_" + name)(**{k: v for k, v in consts.items() if k != "WS"}))

    # page snippets
    def js_state(self): return {"href": self.href, "ready": "complete",
                                "composer": True, "model": True, "edits": 1 if "/c/" in self.href else 0}
    def js_session(self): return {"logged_in": self.logged_in, "email": "a@x.test", "http": 200}
    def js_usage(self): return self.usage_text
    def label(self): return {0: "Instant", 4: "Pro"}.get(self.model, "Thinking")
    def js_slider(self): return {"value": self.model, "max": 4, "valuetext": "", "label": self.label()} if self.picker else None
    def js_slider_focus(self): return self.picker
    def js_model_btn_exists(self): return True
    def js_model_btn_pointer(self): self.picker = True; return True
    def js_model_btn_click(self): return True
    def js_model_btn_rect(self): return {"x": 10, "y": 10}
    def js_button_text(self): return "Thinking effort" if self.picker else self.label()
    def js_pointer_outside(self): self.picker = False; return True

    def js_dom_key(self, TARGET, KEY, CODE, KC, META):
        if TARGET == "slider":
            self.cdp_keys_work, saved = True, self.cdp_keys_work
            self.press(KEY)
            self.cdp_keys_work = saved
        else:
            self.press(KEY, 4 if META else 0)
        return True

    def js_edit_focus(self): return self.edit_open
    def js_edit_len(self): return len(self.box.strip()) if self.edit_open else -1
    def js_edit_exec_select(self): self.selected = True; return True
    def js_edit_exec_delete(self):
        if getattr(self, "selected", False):
            self.box, self.selected = "", False
        return True
    def js_paste(self, EXPECTED):
        text = "".join(self.pack)
        assert chatgpt.js_len(text) == EXPECTED
        self.box += text.replace('"https://', '" https://')     # ProseMirror's autolink artifact
        return {"handled": True, "how": "paste"}

    def js_verify(self):
        pack, got = chatgpt.normalize("".join(self.pack)), chatgpt.normalize(self.box)
        return {"edit_open": self.edit_open, "match": pack == got, "pack_len": len(pack), "edit_len": len(got),
                "diff_at": -1, "pack_near": "", "edit_near": "", "attachments": 0,
                "button": self.js_button_text(), "picker_open": self.picker}

    # composer, title message, edit button, send
    def js_composer_focus(self): return True
    def js_composer_text(self): return self.composer
    def js_composer_exec_insert(self, MSG): self.composer = MSG; return True
    def js_send_ready(self): return bool(self.composer)
    def js_send_click(self):
        if "/c/" not in self.href:
            self.href = "https://chatgpt.com/c/sim-0001"
        self.edits, self.md, self.hits, self.composer = self.edits + 1, self.md + 1, self.hits + 2, ""
        return True
    def js_counts(self, TITLE): return {"edits": self.edits, "md": self.md, "md_last": 40 if self.md else 0,
                                        "stop": self.stop, "href": self.href, "hits": self.hits}
    def js_edit_click(self, TITLE):
        self.edit_open, self.box = True, chatgpt.title_message(TITLE)
        return {"matched": True, "count": self.edits}
    def js_edit_state(self): return {"text": self.box[:4000], "len": len(self.box)} if self.edit_open else None
    def js_edit_btn_rect(self): return {"x": 5, "y": 5}
    def js_edit_cancel(self): self.edit_open = False; return True
    def js_submit_click(self):
        self.edit_open, self.stop = False, True
        return True
    def js_submit_rect(self): return None
    def js_progress(self): return {"stop": self.stop, "thinking": False, "edit_open": self.edit_open, "href": self.href}
    def js_conversation(self, CID):
        if self.api is not None:
            return self.api
        conv = conversation()
        user, after = chatgpt.chain_after_last_user(conv)
        return {"http": 200, "title": conv["title"], "current_node": conv["current_node"], "user": user, "after": after}
    def js_dom_reply(self): return self.dom
    def js_rename(self, CID, TITLE):
        self.renamed = (CID, TITLE)
        return {"http": 200, "success": True}

    def js_pack_reset(self): self.pack = []; return True
    def js_pack_digest(self):
        t = "".join(self.pack)
        return {"length": chatgpt.js_len(t), "chunks": len(self.pack),
                "sha256": hashlib.sha256(t.encode("utf-8")).hexdigest()}


class Clock:
    """time.time/time.sleep for the scripts: sleeping advances the clock instantly."""

    def __init__(self):
        self.now = 1_800_000_000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


def sim_runner(sim: PageSim):
    """A runner that execs the real script with the harness helpers replaced by the simulated page."""
    def run(script, timeout):
        helpers = types.ModuleType("helpers")
        helpers._profile_accounts = lambda: {"C1": ACCT}
        helpers._send = lambda req: {"session_id": "PREV"}
        env = {"__name__": "__main__", "cdp": sim.cdp, "new_tab": sim.new_tab}
        clock, buf = Clock(), io.StringIO()
        with mock.patch.dict(sys.modules, {"helpers": helpers}), mock.patch("time.sleep", clock.sleep), \
                mock.patch("time.time", clock.time), redirect_stdout(buf):
            # Like browser-harness: exec() inside a function, with separate globals and locals.
            exec(compile(script, "<step>", "exec"), env, {})
        return buf.getvalue()
    return run


class TestStepScripts(DataDir):
    """The generated scripts themselves, run against PageSim."""

    def browser(self, sim):
        return chatgpt.Browser(ACCT, runner=sim_runner(sim), log=self.logs.append)

    def test_open_new_chat_reads_usage(self):
        sim = PageSim()
        chatgpt.save_tab_id(ACCT, "T1")
        r = self.browser(sim).call("open", url=chatgpt.NEW_CHAT, conversation_id=None, timeout=20)
        self.assertEqual((r["target_id"], r["created"]), ("T1", False))
        self.assertEqual(chatgpt.parse_usage(r["usage"]), 8)
        self.assertIn("Page.navigate", sim.calls)
        self.assertNotIn("Target.activateTarget", sim.calls)       # the tab stays in the background
        self.assertEqual(sim.calls.count("Target.attachToTarget"), sim.calls.count("Target.detachFromTarget"))

    def test_open_creates_the_review_tab_once_and_remembers_it(self):
        sim = PageSim()
        chatgpt.save_tab_id(ACCT, "GONE")
        r = self.browser(sim).call("open", url=chatgpt.NEW_CHAT, conversation_id=None, timeout=20)
        self.assertTrue(r["created"])
        self.assertEqual(chatgpt.tab_id(ACCT), sim.created[0])

    def test_logged_out(self):
        sim = PageSim()
        sim.logged_in = False
        with self.assertRaisesRegex(chatgpt.ChatGPTError, "isn't logged in"):
            self.browser(sim).call("open", url=chatgpt.NEW_CHAT, conversation_id=None, timeout=20)

    def test_no_profile_window(self):
        sim = PageSim()
        sim.tabs.clear()
        sim.no_window = True
        with self.assertRaises(chatgpt.ChatGPTError) as cm:
            self.browser(sim).call("usage", timeout=20)
        self.assertEqual(str(cm.exception), f"Open a Chrome window in the {ACCT} profile, then try again.")

    def test_model_moves_slider_to_pro_and_keeps_picker_for_the_edit(self):
        sim = PageSim()
        r = self.browser(sim).call("model", target=4, escape=False, timeout=20)
        chatgpt.check_model(r, chatgpt.PRO)
        self.assertEqual(sim.model, 4)
        self.assertNotIn("Escape", json.dumps(r))
        self.assertTrue(r["picker_open"])

    def test_model_instant_with_escape(self):
        sim = PageSim()
        r = self.browser(sim).call("model", target=0, escape=True, timeout=20)
        chatgpt.check_model(r, chatgpt.INSTANT)
        self.assertEqual((sim.model, sim.picker, r["button"]), (0, False, "Instant"))

    def test_model_falls_back_to_dom_keys_when_cdp_keys_are_ignored(self):
        sim = PageSim()
        sim.cdp_keys_work = False
        r = self.browser(sim).call("model", target=4, escape=False, timeout=20)
        self.assertEqual(r["value"], 4)

    def test_load_and_fill_with_autolink_artifact(self):
        sim = PageSim()
        text = ('Please read the pack.\n<file path="a.py">\nurl = f"https://example.test/x"\n</file>\n'
                + ("é" + EMOJI + ' "q" \\ ') * 9000)
        parts = chatgpt.chunks(text)
        b = self.browser(sim)
        loaded = b.call("load", exprs=[chatgpt.chunk_expr(p) for p in parts], timeout=60)
        chatgpt.check_loaded(loaded, text, len(parts))
        sim.picker, sim.model = True, 4          # left open by the model step
        filled = b.call("fill", js_len=chatgpt.js_len(text), timeout=60)
        chatgpt.check_filled(filled)
        self.assertEqual(filled["cleared_by"], "keys")
        self.assertFalse(sim.picker)

    def test_fill_clears_with_dom_keys_when_cdp_keys_miss(self):
        # Tonight's failure: the select-all didn't take and the paste was appended to the title message.
        sim = PageSim()
        sim.cdp_keys_work = False
        sim.model = 4
        text = "the pack " * 1000
        b = self.browser(sim)
        b.call("load", exprs=[chatgpt.chunk_expr(p) for p in chatgpt.chunks(text)], timeout=60)
        filled = b.call("fill", js_len=chatgpt.js_len(text), timeout=60)
        self.assertEqual(filled["cleared_by"], "dom keys")
        chatgpt.check_filled(filled)

    def test_whole_send_through_the_real_scripts(self):
        sim = PageSim()
        sim.edit_open, sim.box = False, ""
        chatgpt.save_tab_id(ACCT, "T1")
        text = 'Please read the pack.\nurl = f"https://example.test/x"\n' + "body " * 20_000
        res = chatgpt.send(text, "Johto review · sim validate-1 turn 1", runner=sim_runner(sim), log=self.logs.append,
                           tag="Johto sim validate-1 t1")
        self.assertEqual(res, {"chat_url": "https://chatgpt.com/c/sim-0001", "sent": True, "chars": len(text),
                               "usage_pct": 8, "pack_sha": chatgpt.text_sha(text), "tag": "Johto sim validate-1 t1",
                               "titled": True})
        self.assertEqual(sim.renamed, ("sim-0001", "Johto review · sim validate-1 turn 1"))
        self.assertEqual(sim.model, 4)
        self.assertTrue(sim.stop)
        self.assertNotIn("Target.activateTarget", sim.calls)
        self.assertFalse((self.tmp / "chatgpt.lock").exists())

    def test_follow_up_dry_run_through_the_real_scripts(self):
        sim = PageSim()
        sim.edit_open, sim.box, sim.edits = False, "", 2
        res = chatgpt.send("the follow-up pack", "Johto review · sim validate-1 turn 2", chat_url=CHAT,
                           no_send=True, runner=sim_runner(sim), log=self.logs.append)
        self.assertEqual((res["chat_url"], res["sent"]), (CHAT, False))
        self.assertTrue(sim.edit_open)                     # left filled for Austin to look at
        self.assertEqual(chatgpt.normalize(sim.box), "the follow-up pack")
        self.assertFalse(sim.stop)

    def test_inspect_through_the_real_scripts(self):
        sim = PageSim()
        conv = conversation()
        user, _ = chatgpt.chain_after_last_user(conv)
        text = chatgpt._text(conv["mapping"][user["id"]]["message"])
        info = chatgpt.inspect(CHAT, runner=sim_runner(sim))
        self.assertEqual((info["found"], info["assistant_status"]), (True, "done"))
        self.assertEqual(info["last_user_sha"], chatgpt.text_sha(text))
        self.assertTrue(chatgpt.holds(info, text))
        self.assertFalse(chatgpt.holds(info, "another pack"))

    def test_fetch_through_the_real_scripts(self):
        sim = PageSim()
        r = chatgpt.fetch(CHAT, runner=sim_runner(sim))
        self.assertEqual((r["status"], r["source"]), ("done", "api"))
        self.assertEqual(r["markdown"], expected_reply())
        sim.api = {"http": 403}
        r = chatgpt.fetch(CHAT, runner=sim_runner(sim))
        self.assertEqual((r["status"], r["markdown"], r["source"]), ("done", "## Review\n\nFrom the page.", "page"))
        self.assertIn(CHAT, sim.href)

    def test_title_message_that_never_gets_an_answer(self):
        sim = PageSim()
        sim.js_send_click = lambda: True                   # the message never shows up
        with self.assertRaisesRegex(chatgpt.ChatGPTError, "didn't answer the title message"):
            self.browser(sim).call("title", message="m", title="t", timeout=30)

    def test_close_only_the_stored_tab(self):
        sim = PageSim()
        sim.tabs["T9"] = {"targetId": "T9", "type": "page", "url": "https://chatgpt.com/c/x", "browserContextId": "C1"}
        chatgpt.save_tab_id(ACCT, "T9")
        self.assertTrue(chatgpt.close_tab(ACCT, runner=sim_runner(sim)))
        self.assertEqual(sorted(sim.tabs), ["T1"])
        self.assertIsNone(chatgpt.tab_id(ACCT))
        self.assertFalse(chatgpt.close_tab(ACCT, runner=sim_runner(sim)))


# --- The send flow ---------------------------------------------------------------------------------

TEXT = "Please read the whole pack below.\n" + "def f():\n    return 'x'  # https://example.test\n" * 2000
TITLE = "Johto review · d3m0aaa9 validate-1 turn 2"
FLOW = ["open", "model", "title", "edit", "model", "load", "fill", "submit"]


class TestSend(DataDir):
    def send(self, fake, **kw):
        return chatgpt.send(kw.pop("text", TEXT), kw.pop("title", TITLE), runner=fake, log=self.logs.append, **kw)

    def test_new_chat(self):
        fake = chatgpt.FakeBrowser()
        res = self.send(fake)
        self.assertEqual(fake.steps(), FLOW + ["rename"])
        self.assertTrue(res["sent"])
        self.assertTrue(res["titled"])
        self.assertEqual(fake.calls[-1][1]["title"], TITLE)
        self.assertEqual(fake.calls[-1][1]["conversation_id"], chatgpt.conversation_id(res["chat_url"]))
        self.assertTrue(res["chat_url"].startswith("https://chatgpt.com/c/fake-"))
        self.assertEqual((res["chars"], res["usage_pct"]), (len(TEXT), 42))
        args = [a for _, a in fake.calls]
        self.assertEqual(args[0]["url"], chatgpt.NEW_CHAT)
        self.assertEqual((args[1]["target"], args[1]["escape"]), (chatgpt.INSTANT, True))
        self.assertEqual(args[2]["message"], chatgpt.title_message(TITLE))
        self.assertEqual((args[4]["target"], args[4]["escape"]), (chatgpt.PRO, False))
        self.assertEqual(args[6]["js_len"], chatgpt.js_len(TEXT))
        self.assertEqual(chatgpt.tab_id(ACCT), "FAKE-TAB")
        self.assertFalse((self.tmp / "chatgpt.lock").exists())
        self.assertFalse(any("usage" in line for line in self.logs))

    def test_follow_up_goes_to_the_same_chat(self):
        fake = chatgpt.FakeBrowser()
        res = self.send(fake, chat_url=CHAT + "?model=x")
        self.assertEqual(fake.steps(), FLOW)               # a follow-up keeps the chat's name
        self.assertFalse(res["titled"])
        self.assertEqual(fake.calls[0][1]["url"], CHAT + "?model=x")
        self.assertEqual(fake.calls[0][1]["conversation_id"], CID)
        self.assertEqual(res["chat_url"], CHAT)

    def test_dry_run_stops_before_send(self):
        fake = chatgpt.FakeBrowser()
        res = self.send(fake, no_send=True)
        self.assertEqual(fake.steps(), FLOW[:-1])
        self.assertFalse(res["sent"])
        self.assertTrue(res["chat_url"])

    def test_tag_is_the_message_that_gets_edited(self):
        fake = chatgpt.FakeBrowser()
        res = self.send(fake, tag="Johto d3m0aaa9 validate-1 t2")
        args = dict(zip(fake.steps(), [a for _, a in fake.calls]))
        self.assertEqual(args["title"]["message"], chatgpt.title_message("Johto d3m0aaa9 validate-1 t2"))
        self.assertEqual((args["title"]["title"], args["edit"]["title"]), ("Johto d3m0aaa9 validate-1 t2",) * 2)
        self.assertEqual(args["rename"]["title"], TITLE)
        self.assertEqual((res["tag"], res["pack_sha"]), ("Johto d3m0aaa9 validate-1 t2", chatgpt.text_sha(TEXT)))

    def test_on_chat_hears_the_link_before_the_pack_goes_in(self):
        fake = chatgpt.FakeBrowser()
        seen = []
        res = self.send(fake, on_chat=lambda url: seen.append((url, list(fake.steps()))))
        self.assertEqual(seen, [(res["chat_url"], ["open", "model", "title"])])

    def test_a_failed_rename_still_counts_as_sent(self):
        for result in ({"http": 403, "success": False}, {"ok": False, "code": "rename", "error": "boom"}):
            fake = chatgpt.FakeBrowser()
            fake.results["rename"] = result
            self.logs.clear()
            res = self.send(fake)
            self.assertEqual((res["sent"], res["titled"]), (True, False))
            self.assertTrue(any("Couldn't name the chat" in line for line in self.logs), self.logs)

    def test_low_usage_warns_but_sends(self):
        self.settings(chatgpt_warn_usage_pct=50)
        res = self.send(chatgpt.FakeBrowser())
        self.assertTrue(res["sent"])
        self.assertTrue(any("42%" in line for line in self.logs))

    def refused(self, step, result, pattern):
        fake = chatgpt.FakeBrowser()
        fake.results[step] = result
        with self.assertRaisesRegex(chatgpt.ChatGPTError, pattern):
            self.send(fake)
        self.assertNotIn("submit", fake.steps())
        self.assertFalse((self.tmp / "chatgpt.lock").exists())
        return fake

    def test_refuses_when_the_box_differs(self):
        self.refused("fill", {"edit_open": True, "match": False, "pack_len": 10, "edit_len": 12, "diff_at": 3,
                              "pack_near": "abc", "edit_near": "abX", "attachments": 0, "button": "Pro"},
                     "doesn't match the pack")

    def test_refuses_an_attachment(self):
        self.refused("fill", {"edit_open": True, "match": True, "attachments": 1, "button": "Pro"}, "attachment")

    def test_refuses_when_the_model_is_not_pro(self):
        self.refused("fill", {"edit_open": True, "match": True, "attachments": 0, "button": "Instant"}, "not Pro")
        self.refused("model", lambda a: {"value": a["target"], "label": "Instant"}, "not Pro")
        self.refused("model", lambda a: {"value": 3, "label": ""}, "slider is at 3")

    def test_refuses_when_the_edit_box_closed(self):
        self.refused("fill", {"edit_open": False, "button": "Pro"}, "closed")

    def test_refuses_a_bad_load(self):
        self.refused("load", lambda a: {"length": 1, "chunks": len(a["exprs"]), "sha256": ""}, "expected")

    def test_step_failure_names_the_step(self):
        self.refused("title", {"ok": False, "code": "title", "error": "ChatGPT didn't answer in time"},
                     "sending the title message: ChatGPT didn't answer")

    def test_no_window_message(self):
        self.refused("open", {"ok": False, "code": "no_window", "error": "No open Chrome window for x"},
                     f"^Open a Chrome window in the {ACCT} profile, then try again.$")

    def test_send_that_does_not_start(self):
        fake = chatgpt.FakeBrowser()
        fake.results["submit"] = {"started": False, "url": CHAT}
        with self.assertRaises(chatgpt.ChatGPTError) as cm:
            self.send(fake)
        self.assertEqual(cm.exception.chat_url, CHAT)
        self.assertTrue(cm.exception.maybe_sent)

    def test_only_a_missing_send_button_is_surely_unsent(self):
        for error, maybe in ((chatgpt.NO_SEND_BUTTON, False), ("page script timed out", True)):
            fake = chatgpt.FakeBrowser()
            fake.results["submit"] = {"ok": False, "code": "submit", "error": error}
            with self.assertRaises(chatgpt.ChatGPTError) as cm:
                self.send(fake)
            self.assertEqual(cm.exception.maybe_sent, maybe, error)
            self.assertTrue(cm.exception.chat_url)
        fake = chatgpt.FakeBrowser()
        fake.results["fill"] = {"edit_open": False, "button": "Pro"}
        with self.assertRaises(chatgpt.ChatGPTError) as cm:
            self.send(fake)
        self.assertFalse(cm.exception.maybe_sent)

    def test_input_errors(self):
        fake = chatgpt.FakeBrowser()
        with self.assertRaisesRegex(chatgpt.ChatGPTError, "empty"):
            self.send(fake, text="  \n")
        with self.assertRaisesRegex(chatgpt.ChatGPTError, "Not a ChatGPT chat link"):
            self.send(fake, chat_url="https://example.test/x")
        self.assertEqual(fake.calls, [])

    def test_busy_lock_refuses(self):
        (self.tmp / "chatgpt.lock").write_text(json.dumps(
            {"pid": os.getppid(), "t": __import__("time").time(), "at": "22:00:00", "what": "send other"}))
        fake = chatgpt.FakeBrowser()
        with self.assertRaisesRegex(chatgpt.ChatGPTError, "Another ChatGPT step"):
            self.send(fake, lock_wait=0)
        self.assertEqual(fake.calls, [])
        self.assertTrue((self.tmp / "chatgpt.lock").exists())   # not ours, left alone

    def test_stale_lock_is_taken_over(self):
        (self.tmp / "chatgpt.lock").write_text(json.dumps({"pid": 999_999_9, "t": 0, "what": "old"}))
        self.assertTrue(self.send(chatgpt.FakeBrowser(), lock_wait=0)["sent"])
        self.assertFalse((self.tmp / "chatgpt.lock").exists())

    def test_broken_daemon_is_restarted_once(self):
        fake = chatgpt.FakeBrowser()
        calls = {"n": 0}

        def flaky(script, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise chatgpt.HarnessError("websocket closed (keepalive ping timeout)")
            return fake(script, timeout)

        with mock.patch.object(chatgpt, "_restart_daemon") as restart:
            self.assertTrue(self.send(flaky)["sent"])
        restart.assert_called_once()

    def test_harness_failure_without_retry(self):
        def broken(script, timeout):
            raise chatgpt.HarnessError("CDP WS handshake failed: refused")
        with mock.patch.object(chatgpt, "_restart_daemon"):
            with self.assertRaisesRegex(chatgpt.ChatGPTError, "Chrome isn't reachable"):
                self.send(broken)


class TestFetch(DataDir):
    def test_api_reply(self):
        _, after = chatgpt.chain_after_last_user(conversation())
        fake = chatgpt.FakeBrowser()
        fake.results["fetch"] = {"http": 200, "title": "t", "after": after}
        r = chatgpt.fetch(CHAT, runner=fake)
        self.assertEqual((r["status"], r["message_id"], r["source"]), ("done", TestConversation.FINAL, "api"))
        self.assertEqual(r["markdown"], expected_reply())
        self.assertEqual(fake.calls[0][1]["conversation_id"], CID)

    def test_not_found_and_logged_out(self):
        fake = chatgpt.FakeBrowser()
        fake.results["fetch"] = {"http": 404}
        self.assertEqual(chatgpt.fetch(CHAT, runner=fake)["status"], "not_found")
        fake.results["fetch"] = {"api_error": "logged_out"}
        with self.assertRaisesRegex(chatgpt.ChatGPTError, "isn't logged in"):
            chatgpt.fetch(CHAT, runner=fake)
        with self.assertRaisesRegex(chatgpt.ChatGPTError, "Not a ChatGPT chat link"):
            chatgpt.fetch("https://chatgpt.com/", runner=fake)

    def test_falls_back_to_the_page(self):
        fake = chatgpt.FakeBrowser()
        fake.results["fetch"] = {"http": 500}
        fake.results["fetch_dom"] = {"stop": False, "found": True, "markdown": "## Review\n\nText.", "message_id": "m1"}
        r = chatgpt.fetch(CHAT, runner=fake)
        self.assertEqual((r["status"], r["markdown"], r["source"]), ("done", "## Review\n\nText.", "page"))
        fake.results["fetch_dom"] = {"stop": True, "found": True, "markdown": "## Rev", "message_id": "m1"}
        self.assertEqual(chatgpt.fetch(CHAT, runner=fake)["status"], "thinking")

    def test_page_fallback_waits_for_a_send(self):
        fake = chatgpt.FakeBrowser()
        fake.results["fetch"] = {"api_error": "unexpected shape: detail"}
        with chatgpt.lock("send"):
            r = chatgpt.fetch(CHAT, runner=fake)
        self.assertEqual(r["status"], "error")
        self.assertNotIn("fetch_dom", fake.steps())

    def test_fake_send_then_fetch_across_processes(self):
        state = self.tmp / "fake" / "chatgpt.json"
        sent = chatgpt.send(TEXT, TITLE, runner=chatgpt.FakeBrowser(state_file=state), log=self.logs.append)
        r = chatgpt.fetch(sent["chat_url"], runner=chatgpt.FakeBrowser(state_file=state))
        self.assertEqual(r["status"], "done")
        self.assertIn(TITLE, r["markdown"])
        thinking = chatgpt.FakeBrowser(state_file=state, think_s=3600)
        self.assertEqual(chatgpt.fetch(sent["chat_url"], runner=thinking)["status"], "thinking")

    def test_inspect_settles_a_send_across_processes(self):
        state = self.tmp / "fake" / "chatgpt.json"
        sent = chatgpt.send(TEXT, TITLE, runner=chatgpt.FakeBrowser(state_file=state), log=self.logs.append)
        info = chatgpt.inspect(sent["chat_url"], runner=chatgpt.FakeBrowser(state_file=state))
        self.assertEqual((info["found"], info["assistant_status"], info["title"]), (True, "done", TITLE))
        self.assertEqual(info["last_user_chars"], chatgpt.norm_len(TEXT))
        self.assertTrue(chatgpt.holds(info, TEXT))
        self.assertFalse(chatgpt.holds(info, TEXT + " and more"))
        info = chatgpt.inspect(sent["chat_url"], runner=chatgpt.FakeBrowser(state_file=state, think_s=3600))
        self.assertEqual(info["assistant_status"], "none")
        gone = chatgpt.inspect(CHAT, runner=chatgpt.FakeBrowser(state_file=state))
        self.assertEqual((gone["found"], chatgpt.holds(gone, TEXT)), (False, False))

    def test_inspect_errors_and_partial_answers(self):
        fake = chatgpt.FakeBrowser()
        fake.results["fetch"] = {"http": 500}
        with self.assertRaisesRegex(chatgpt.ChatGPTError, "HTTP 500"):
            chatgpt.inspect(CHAT, runner=fake)
        fake.results["fetch"] = {"api_error": "logged_out"}
        with self.assertRaisesRegex(chatgpt.ChatGPTError, "isn't logged in"):
            chatgpt.inspect(CHAT, runner=fake)
        thinking = {"id": "a1", "author": {"role": "assistant"}, "recipient": "all", "channel": "analysis",
                    "content": {"content_type": "text", "parts": ["hmm"]}, "status": "in_progress"}
        fake.results["fetch"] = {"http": 200, "title": "t", "user": {}, "after": [thinking]}
        info = chatgpt.inspect(CHAT, runner=fake)
        self.assertEqual(info["assistant_status"], "thinking")
        self.assertIsNone(chatgpt.holds(info, TEXT))       # no fingerprint: can't tell

    def test_holds_is_exact_but_spacing_blind(self):
        spaced = TEXT.replace("\n    return", "\n\treturn  ")
        info = {"found": True, "last_user_sha": "x" * 64, "last_user_dense_sha": chatgpt.dense_sha(spaced)}
        self.assertTrue(chatgpt.holds(info, TEXT))         # only the spacing differs
        self.assertFalse(chatgpt.holds(info, TEXT.replace("return 'x'", "return 'y'", 1)))
        tag = chatgpt.title_message("Johto q v1 t2")       # the tag message: the edit never went in
        self.assertFalse(chatgpt.holds({"found": True, "last_user_sha": chatgpt.text_sha(tag),
                                        "last_user_dense_sha": chatgpt.dense_sha(tag)}, TEXT))
        self.assertIsNone(chatgpt.holds({"found": True}, TEXT))

    def test_usage(self):
        self.assertEqual(chatgpt.usage(runner=chatgpt.FakeBrowser()), 42)
        fake = chatgpt.FakeBrowser()
        fake.results["usage"] = {"usage": None}
        self.assertIsNone(chatgpt.usage(runner=fake))


# --- Runner choice and command line ----------------------------------------------------------------

class TestRunnerChoice(DataDir):
    def patch_config(self, **kv):
        for k, v in kv.items():
            p = mock.patch.object(config, k, v)
            p.start()
            self.addCleanup(p.stop)

    def test_fake_mode_uses_the_fake_browser(self):
        self.patch_config(FAKE=True, ROOT=self.tmp)
        self.assertIsInstance(chatgpt.default_runner(), chatgpt.FakeBrowser)

    def test_scratch_root_refuses_real_chatgpt(self):
        self.patch_config(FAKE=False, ROOT=self.tmp, REAL_OK=False)
        with mock.patch.dict(os.environ, {"JOHTO_REAL_CHATGPT": ""}):
            with self.assertRaisesRegex(chatgpt.ChatGPTError, "JOHTO_REAL_CHATGPT=1"):
                chatgpt.default_runner()
        with mock.patch.dict(os.environ, {"JOHTO_REAL_CHATGPT": "1"}):
            self.assertIs(chatgpt.default_runner(), chatgpt.run_harness)

    def test_cli_dry_run_and_fetch_in_fake_mode(self):
        self.patch_config(FAKE=True, ROOT=self.tmp)
        pack = self.tmp / "pack.xml"
        pack.write_text(TEXT)
        buf = io.StringIO()
        with redirect_stdout(buf):
            chatgpt.main(["--send", str(pack), "--title", TITLE])
        out = buf.getvalue()
        sent = json.loads(out[out.index("{"):out.rindex("}") + 1])
        self.assertTrue(sent["sent"])
        md = self.tmp / "reply.md"
        with redirect_stdout(io.StringIO()):
            chatgpt.main(["--fetch", sent["chat_url"], "--out", str(md)])
        self.assertIn(TITLE, md.read_text())
        buf = io.StringIO()
        with redirect_stdout(buf):
            chatgpt.main(["--dry-run-send", str(pack), "--title", TITLE])
        self.assertIn('"sent": false', buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            chatgpt.main(["--inspect", sent["chat_url"]])
        self.assertEqual(json.loads(buf.getvalue())["last_user_sha"], chatgpt.text_sha(TEXT))


# --- Page code under node --------------------------------------------------------------------------

NODE = shutil.which("node")

FAKE_DOM = r"""
// Selectors: comma lists of tag, .class and [attr], [attr="v"], [attr*="v"], [attr$="v"], [attr^="v"].
const matches = (el, sel) => sel.split(',').some(one => {
  if (one.trim() === '*') return true;
  const m = one.trim().match(/^([a-z0-9]*)((?:\.[\w-]+)*)((?:\[[^\]]+\])*)$/i);
  if (!m) throw new Error('FAKE_DOM selector: ' + one);
  if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
  const cls = String(el.attrs.class || '').split(/\s+/);
  for (const c of (m[2].match(/\.[\w-]+/g) || [])) if (!cls.includes(c.slice(1))) return false;
  for (const a of (m[3].match(/\[[^\]]+\]/g) || [])) {
    const q = a.slice(1, -1).match(/^([\w-]+)(?:([*$^]?=)"([^"]*)")?$/);
    if (!q) throw new Error('FAKE_DOM attribute: ' + a);
    const v = el.attrs[q[1]];
    if (v === undefined) return false;
    if ((q[2] === '=' && v !== q[3]) || (q[2] === '*=' && !v.includes(q[3])) ||
        (q[2] === '$=' && !v.endsWith(q[3])) || (q[2] === '^=' && !v.startsWith(q[3]))) return false;
  }
  return true;
});
const E = (tag, attrs, ...kids) => {
  const el = {
    nodeType: 1, tagName: tag.toUpperCase(), attrs: attrs || {}, parentNode: null,
    childNodes: kids.map(k => typeof k === 'string' ? {nodeType: 3, textContent: k} : k),
    get children() { return this.childNodes.filter(c => c.nodeType === 1); },
    get parentElement() { return this.parentNode; },
    get className() { return this.attrs.class || ''; },
    get textContent() { return this.childNodes.map(c => c.textContent).join(''); },
    get innerText() { return this.textContent; },
    getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; },
    querySelectorAll(sel) {
      const out = [];
      const walk = n => { for (const c of n.children) { if (matches(c, sel)) out.push(c); walk(c); } };
      walk(this);
      return out;
    },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    closest(sel) { for (let n = this; n; n = n.parentNode) if (matches(n, sel)) return n; return null; },
    compareDocumentPosition(other) {
      let top = this;
      while (top.parentNode) top = top.parentNode;
      const order = [top, ...top.querySelectorAll('*')];
      return order.indexOf(other) > order.indexOf(this) ? 4 : 2;
    },
  };
  for (const c of el.childNodes) c.parentNode = el;
  return el;
};
"""


@unittest.skipUnless(NODE, "node isn't installed")
class TestPageCode(unittest.TestCase):
    def node(self, src: str):
        r = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_snippets_parse(self):
        with tempfile.TemporaryDirectory() as d:
            for name, body in chatgpt.JS.items():
                f = Path(d) / f"{name}.js"
                f.write_text("(async () => {\n" + chatgpt.LIB_JS + "\nconst __f = async () => {\n" + body
                             + "\n};\nreturn JSON.stringify(await __f());\n})()")
                r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True, timeout=30)
                self.assertEqual(r.returncode, 0, f"{name}: {r.stderr}")

    def test_page_normalizes_like_python(self):
        samples = ['f"https://x.test"', 'f" https://x.test"', "a\n\n b\t c ", " lead", "x\x1cy",
                   "x" + chr(0xA0) + "y", "p" + chr(0x2028) + "q", chr(0xFEFF) + "bom", "z" + chr(0x85) + "z",
                   "tab\there\r\nhttp://a http://b", "é " + EMOJI + "  end"]
        got = self.node(chatgpt.LIB_JS + f"\nconsole.log(JSON.stringify({json.dumps(samples)}.map(J.norm)));")
        self.assertEqual(got, [chatgpt.normalize(s) for s in samples])

    def test_page_markdown_from_dom(self):
        tree = r"""
const root = E('div', {class: 'markdown prose'},
  E('h2', {}, 'Task Success'),
  E('p', {}, 'The ', E('strong', {}, 'sync'), ' keeps ', E('code', {}, 'loan[0]'), ', see ',
    E('a', {href: 'https://x.test/a'}, 'the issue'), '.'),
  E('ul', {}, E('li', {}, 'first'), E('li', {}, E('p', {}, 'second'), E('ol', {}, E('li', {}, 'inner')))),
  E('pre', {}, E('div', {}, E('span', {}, 'python'), E('button', {}, 'Copy')),
    E('code', {class: 'language-python'}, 'print(1)\n')),
  E('div', {class: 'table-wrap'}, E('table', {},
    E('thead', {}, E('tr', {}, E('th', {}, 'Input'), E('th', {}, 'Value'))),
    E('tbody', {}, E('tr', {}, E('td', {}, 'a|b'), E('td', {}, '0.6'))))),
  E('blockquote', {}, E('p', {}, 'quoted')),
  E('hr', {}),
  E('button', {}, 'Copy'));
console.log(JSON.stringify(J.md(root)));
"""
        md = self.node(chatgpt.LIB_JS + FAKE_DOM + tree)
        self.assertEqual(md, "\n".join([
            "## Task Success",
            "",
            "The **sync** keeps `loan[0]`, see [the issue](https://x.test/a).",
            "",
            "- first",
            "- second",
            "  1. inner",
            "",
            "```python",
            "print(1)",
            "```",
            "",
            "| Input | Value |",
            "| --- | --- |",
            "| a\\|b | 0.6 |",
            "",
            "> quoted",
            "",
            "---",
        ]))


    def snippet(self, name: str, prelude: str, consts: dict | None = None):
        """Run one JS snippet under node after prelude (fake DOM, fake fetch); returns its result."""
        decl = "".join(f"const {k} = {json.dumps(v)};\n" for k, v in (consts or {}).items())
        src = (chatgpt.LIB_JS + FAKE_DOM + prelude + "\n(async () => {\n" + decl + "const __f = async () => {\n"
               + chatgpt.JS[name] + "\n};\nconsole.log(JSON.stringify(await __f()));\n})();")
        return self.node(src)

    def test_reply_root_does_not_need_the_markdown_class(self):
        got = self.node(chatgpt.LIB_JS + FAKE_DOM + r"""
const bare = E('div', {'data-message-author-role': 'assistant'},
  E('h4', {class: 'sr-only'}, 'ChatGPT said:'), E('p', {}, 'The answer.'));
const styled = E('div', {'data-message-author-role': 'assistant'},
  E('div', {class: 'text-message'}, E('div', {class: 'prose-like markdown-new'}, E('p', {}, 'Styled.'))));
console.log(JSON.stringify([J.md(J.replyRoot(bare)), J.md(J.replyRoot(styled)), J.assistants(E('main', {}, bare)).length]));
""")
        self.assertEqual(got, ["The answer.", "Styled.", 1])

    def dom(self, *turns: str) -> str:
        return ("const MAIN = E('main', {}, " + ", ".join(turns) + ");\n"
                "const document = {querySelector: s => s === 'main' ? MAIN : MAIN.querySelector(s),"
                " querySelectorAll: s => MAIN.querySelectorAll(s), body: MAIN};\n"
                "const Node = {DOCUMENT_POSITION_FOLLOWING: 4};\n")

    def test_dom_reply_reads_a_reply_without_markdown_class(self):
        user = "E('article', {'data-turn': 'user'}, E('p', {}, 'The pack.'))"
        done = ("E('article', {'data-turn': 'assistant', 'data-message-id': 'm9'},"
                " E('h6', {class: 'sr-only'}, 'ChatGPT said:'), E('p', {}, 'Findings follow.'))")
        r = self.snippet("dom_reply", self.dom(user, done))
        self.assertEqual(r, {"stop": False, "found": True, "markdown": "Findings follow.", "message_id": "m9"})
        thinking = "E('article', {'data-turn': 'assistant'}, E('div', {}, 'Pro thinking'))"
        r = self.snippet("dom_reply", self.dom(user, thinking))
        self.assertEqual((r["stop"], r["markdown"]), (True, None))
        r = self.snippet("dom_reply", self.dom(done, user))      # the newest user message has no answer yet
        self.assertEqual((r["found"], r["markdown"]), (False, None))

    FETCH = r"""
const CALLS = [];
globalThis.fetch = async (url, opts) => {
  CALLS.push({url, method: (opts && opts.method) || 'GET', headers: (opts && opts.headers) || {},
              body: opts && opts.body});
  if (url === '/api/auth/session') return {ok: true, status: 200, json: async () => ({accessToken: 'tok-secret'})};
  return {ok: true, status: 200, json: async () => ANSWER};
};
"""

    def test_page_fingerprint_matches_python(self):
        conv = conversation()
        user, _ = chatgpt.chain_after_last_user(conv)
        tricky = 'Pack:\n\n  url = f"https://x.test"\t' + chr(0xA0) + "é " + EMOJI + "  end\n"
        conv["mapping"][user["id"]]["message"]["content"]["parts"] = [tricky]
        r = self.snippet("conversation", "const ANSWER = " + json.dumps(conv) + ";\n" + self.FETCH, {"CID": CID})
        self.assertEqual(r["user"]["norm_sha"], chatgpt.text_sha(tricky))
        self.assertEqual(r["user"]["norm_chars"], chatgpt.norm_len(tricky))
        self.assertEqual(r["user"]["head"], chatgpt.normalize(tricky)[:200])
        self.assertEqual(r["user"]["tail"], chatgpt.normalize(tricky)[-200:])
        self.assertEqual(r["user"]["dense_sha"], chatgpt.dense_sha(tricky))
        self.assertEqual(r["user"]["chars"], chatgpt.js_len(tricky))

    def test_rename_keeps_the_token_in_the_page(self):
        prelude = "const ANSWER = {success: true};\n" + self.FETCH
        src = (chatgpt.LIB_JS + FAKE_DOM + prelude + "\n(async () => {\nconst CID = " + json.dumps(CID)
               + ";\nconst TITLE = \"Johto review · q v1 t2\";\nconst __f = async () => {\n" + chatgpt.JS["rename"]
               + "\n};\nconst r = await __f();\nconsole.log(JSON.stringify({r, calls: CALLS}));\n})();")
        got = self.node(src)
        self.assertEqual(got["r"], {"http": 200, "success": True})
        patch = got["calls"][1]
        self.assertEqual((patch["url"], patch["method"]), (f"/backend-api/conversation/{CID}", "PATCH"))
        self.assertEqual(json.loads(patch["body"]), {"title": "Johto review · q v1 t2"})
        self.assertEqual(patch["headers"]["Authorization"], "Bearer tok-secret")
        self.assertNotIn("tok-secret", json.dumps(got["r"]))


if __name__ == "__main__":
    unittest.main()
