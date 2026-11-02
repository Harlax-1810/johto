"""GPT Pro reviews through Austin's Chrome: send a review pack to ChatGPT Pro and read the reply later.

GPT Pro's review of a lane turn is Austin's best grading input, and waiting on it is not focused time, so
johto drives the ChatGPT tab itself. It works in one dedicated "review tab" in the Chrome profile that has
ChatGPT Pro (settings `chatgpt_account`), the same way johto/studio.py drives Studio: each step runs as a
small Python script through `browser-harness` (CDP into the running Chrome). The tab is attached without
being activated, so it stays in the background; only creating it brings it forward once. Its target id is
kept in data/chatgpt-tab.json and the tab is reused; johto closes only that tab.

How a pack is sent (Austin's method; it keeps the pack in the model's context instead of an attachment):
  1. Open https://chatgpt.com/ (new chat) or the run's existing chat (follow-ups share one conversation).
  2. Set the model slider to Instant.
  3. Send a short message asking for a short tag back ("Reply with exactly this ...: Johto t1 validate-1 t2")
     and wait for the answer. The tag marks the message to edit; a new chat is named afterwards (step 9).
  4. Open "Edit message" on that message.
  5. Move the model slider to Pro (the edit box stays open).
  6. Load the pack into the page in chunks (each CDP request stays under the harness daemon's 64 KB line
     limit), check its length and SHA-256, then clear the edit box and paste it with a synthetic paste
     event. Typing it with Input.insertText took 11 minutes and put chunks out of order; pasting into the
     composer turns long text into a file attachment; the system clipboard is never touched.
  7. Check before sending: the edit box text matches the pack (whitespace-normalized, tolerating the space
     ProseMirror shows before autolinked URLs), no attachment appeared, and the model reads Pro.
     Otherwise refuse. A dry run (no_send=True) stops here and leaves the edit box filled.
  8. Click the edit box's Send and confirm the reply started.
  9. For a new chat, set its title with an in-page PATCH to /backend-api/conversation/<id>.

fetch() reads the reply from ChatGPT's own conversation API, from inside a chatgpt.com page of that
profile (the access token never leaves the page), with a fallback that reads the page itself. inspect()
reads the same API to tell whether a send whose outcome isn't known reached the chat: it returns the
length and a fingerprint of the chat's newest user message (text_sha, computed in the page), and how far the
answer after it has got.

data/chatgpt.lock keeps two sends from driving the tab at once. Real ChatGPT is used only from this
checkout's data (or with JOHTO_REAL_CHATGPT=1 / JOHTO_REAL_STUDIO=1); fake mode (JOHTO_FAKE_STUDIO=1)
uses FakeBrowser, which answers every step like a working tab, and tests inject their own runner.

Command line (for live checks): `python -m johto.chatgpt --dry-run-send FILE --title T [--chat URL]`,
`--send FILE --title T`, `--fetch CHAT_URL [--out FILE]`, `--inspect CHAT_URL`, `--usage`, `--close-tab`,
`--show-script STEP`.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable

from . import config, settings

MARK = "@@JOHTO-GPT@@"
NEW_CHAT = "https://chatgpt.com/"
INSTANT, PRO = 0, 4                  # the model slider: 0 = Instant ... 4 = Pro
CHUNK_CHARS = 15_000                 # characters per chunk loaded into the page
LINE_LIMIT = 60_000                  # bytes per harness request (the daemon reads lines of at most 64 KB)
RUNNER_SLACK_S = 45                  # harness startup and tab creation, on top of each step's own deadline
LOCK_WAIT_S = 600.0                  # how long send() waits for another send to finish
LOCK_STALE_S = 30 * 60               # a lock older than this (or from a dead process) is taken over
TITLE_PROMPT = "Reply with exactly this title and nothing else: {title}"
NO_SEND_BUTTON = "The edit box's Send button is gone"   # the submit step's one failure before its click

Runner = Callable[[str, float], str]   # (script, timeout seconds) -> the harness's stdout


class ChatGPTError(RuntimeError):
    """A send, fetch or usage read that failed. chat_url is set when a chat exists despite the failure;
    maybe_sent is True when Send was clicked (or may have been), so the pack may be on its way."""

    def __init__(self, message: str, chat_url: str | None = None, maybe_sent: bool = False):
        super().__init__(message)
        self.chat_url = chat_url
        self.maybe_sent = maybe_sent


class HarnessError(RuntimeError):
    """browser-harness itself failed (exit code, timeout, missing binary)."""


# --- Text checks ---------------------------------------------------------------------------------

# The whitespace set of JavaScript's \s, spelled out so Python and the page normalize the same way.
_WS_CODES = (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029,
             0x202F, 0x205F, 0x3000, 0xFEFF)
WS_CLASS = "[" + "".join(f"\\u{c:04x}" for c in _WS_CODES) + "]"
_WS = re.compile(WS_CLASS + "+")
_AUTOLINK = re.compile(r" (?=http)")
_SURROGATE = re.compile("[%s-%s]" % (chr(0xD800), chr(0xDFFF)))


def normalize(text: str) -> str:
    """Collapse whitespace and drop spaces right before "http" (ProseMirror shows 'f"https://x' as
    'f" https://x'). The page runs the same rules (J.norm in LIB_JS) on both sides before comparing."""
    return _AUTOLINK.sub("", _WS.sub(" ", text).strip(" "))


def same_text(a: str, b: str) -> bool:
    return normalize(a) == normalize(b)


def js_len(text: str) -> int:
    """Length as JavaScript counts it (UTF-16 units): characters outside the BMP count twice."""
    return len(text.encode("utf-16-le")) // 2


def clean_text(text: str) -> str:
    """Lone surrogates can't round-trip through the page; replace them the way TextEncoder does."""
    return _SURROGATE.sub(chr(0xFFFD), text)


def title_message(title: str) -> str:
    return TITLE_PROMPT.format(title=title)


def text_sha(text: str) -> str:
    """SHA-256 of the whitespace-normalized text: what inspect() reports for a chat's newest user message, so a
    pack file and the message ChatGPT holds compare equal despite the editor's whitespace."""
    return hashlib.sha256(normalize(clean_text(text)).encode("utf-8")).hexdigest()


def dense_sha(text: str) -> str:
    """SHA-256 of the text with all whitespace removed: equal when only the spacing differs."""
    return hashlib.sha256(_WS.sub("", clean_text(text)).encode("utf-8")).hexdigest()


def norm_len(text: str) -> int:
    """Length of the normalized text in JavaScript units (inspect()'s last_user_chars)."""
    return js_len(normalize(clean_text(text)))


def parse_usage(text: str | None) -> int | None:
    """'8% usage remaining · Resets every week ...' -> 8. None when the banner isn't shown."""
    m = re.search(r"(\d{1,3})\s*%\s*usage remaining", text or "", re.I)
    return int(m.group(1)) if m else None


def conversation_id(chat_url: str | None) -> str | None:
    m = re.search(r"chatgpt\.com/(?:[^?#]*/)?c/([0-9A-Za-z-]{8,})", chat_url or "")
    return m.group(1) if m else None


def canonical_url(href: str | None) -> str | None:
    """The chat link without query or fragment; None when the page isn't a chat yet."""
    if not conversation_id(href):
        return None
    return href.split("#", 1)[0].split("?", 1)[0]


# --- Chunks loaded into the page -------------------------------------------------------------------

_PUSH = "window.__johtoPack.push({})"
_COST: dict[str, int] = {}


def chunk_expr(chunk: str) -> str:
    return _PUSH.format(json.dumps(chunk))


def request_bytes(expr: str) -> int:
    """Bytes of the harness request line that evaluates expr (helpers._send JSON-encodes it again)."""
    req = {"method": "Runtime.evaluate",
           "params": {"expression": expr, "returnByValue": True, "awaitPromise": True},
           "session_id": "S" * 64}
    return len(json.dumps(req)) + 1


_OVERHEAD = request_bytes(chunk_expr(""))


def _cost(ch: str) -> int:
    """Bytes one character adds to the request: JSON-escaped for JS, then JSON-escaped again."""
    c = _COST.get(ch)
    if c is None:
        c = _COST[ch] = len(json.dumps(json.dumps(ch)[1:-1])) - 2
    return c


def chunks(text: str, max_chars: int = CHUNK_CHARS, max_bytes: int = LINE_LIMIT) -> list[str]:
    """Split text so each chunk has at most max_chars characters and its push request fits max_bytes."""
    budget = max_bytes - _OVERHEAD
    out: list[str] = []
    start = size = 0
    for i, ch in enumerate(text):
        c = _cost(ch)
        if i > start and (i - start >= max_chars or size + c > budget):
            out.append(text[start:i])
            start, size = i, 0
        size += c
    if start < len(text):
        out.append(text[start:])
    return out


# --- Reading a conversation ------------------------------------------------------------------------

_CITE = re.compile("[ \t]*%s.*?%s" % (chr(0xE200), chr(0xE201)), re.S)   # ChatGPT citation markers
_PRIVATE = re.compile("[%s-%s]" % (chr(0xE200), chr(0xE2FF)))


def chain_after_last_user(conv: dict) -> tuple[dict | None, list[dict]]:
    """Walk from current_node up to the newest visible user message. Returns (that user message's ids,
    the messages after it in order). The page's `conversation` snippet does the same walk."""
    m = conv.get("mapping") or {}
    node, after, user = conv.get("current_node"), [], None
    for _ in range(100_000):
        if not node or node not in m:
            break
        msg = m[node].get("message")
        if msg:
            role = (msg.get("author") or {}).get("role")
            hidden = (msg.get("metadata") or {}).get("is_visually_hidden_from_conversation")
            if role == "user" and not hidden:
                text = _text(msg)
                user = {"id": msg.get("id"), "create_time": msg.get("create_time"), "chars": js_len(text),
                        "norm_chars": norm_len(text), "norm_sha": text_sha(text), "dense_sha": dense_sha(text),
                        "head": normalize(text)[:200], "tail": normalize(text)[-200:]}
                break
            after.append(msg)
        node = m[node].get("parent")
    after.reverse()
    return user, after


def _text(msg: dict) -> str:
    content = msg.get("content") or {}
    if content.get("content_type") not in ("text", "multimodal_text"):
        return ""
    parts = []
    for p in content.get("parts") or []:
        if isinstance(p, str):
            parts.append(p)
        elif isinstance(p, dict) and isinstance(p.get("text"), str):
            parts.append(p["text"])
    return "\n\n".join(x for x in parts if x)


def _is_reply(msg: dict) -> bool:
    """A message Austin sees as the answer: assistant text to everyone, not reasoning or tool calls."""
    meta = msg.get("metadata") or {}
    return ((msg.get("author") or {}).get("role") == "assistant"
            and msg.get("recipient") in (None, "all")
            and msg.get("channel") != "analysis"
            and not meta.get("is_visually_hidden_from_conversation")
            and bool(_text(msg).strip()))


def clean_markdown(text: str) -> str:
    return _PRIVATE.sub("", _CITE.sub("", text)).strip()


def reply_from_chain(after: list[dict]) -> dict:
    """{"status": "thinking"|"done"|"error", "markdown", "message_id"} from the messages after the last
    user message. Done only when the newest message is a finished, visible answer that ends the turn."""
    res: dict = {"status": "thinking", "markdown": None, "message_id": None}
    replies = [m for m in after if _is_reply(m)]
    finals = [m for m in replies if m.get("channel") in (None, "final")] or replies
    if finals:
        res["markdown"] = clean_markdown("\n\n".join(_text(m).strip() for m in finals))
        res["message_id"] = finals[-1].get("id")
    if not after:
        return res
    last = after[-1]
    status = last.get("status")
    finish = ((last.get("metadata") or {}).get("finish_details") or {}).get("type")
    if status in ("finished_partial_completion", "finished_with_error") or finish == "interrupted":
        res["status"] = "error"
        res["error"] = "ChatGPT stopped before finishing the reply"
    elif finals and last is finals[-1] and status == "finished_successfully" and last.get("end_turn") is not False:
        res["status"] = "done"
    return res


def latest_reply(conv: dict) -> dict:
    return reply_from_chain(chain_after_last_user(conv)[1])


# --- Checks between steps (refuse to send on any doubt) ------------------------------------------

def check_model(info: dict, target: int) -> None:
    want = "Pro" if target == PRO else "Instant"
    if info.get("value") != target:
        raise ChatGPTError(f"The model slider is at {info.get('value')}, not {want}.")
    label = f"{info.get('label') or ''} {info.get('valuetext') or ''}".strip()
    if label and not re.search(rf"\b{want}\b", label):
        raise ChatGPTError(f"The model picker reads {label!r}, not {want}.")


def check_loaded(res: dict, text: str, n_chunks: int) -> None:
    if res.get("chunks") != n_chunks:
        raise ChatGPTError(f"Only {res.get('chunks')} of {n_chunks} pack chunks reached the page.")
    if res.get("length") != js_len(text):
        raise ChatGPTError(f"The page holds {res.get('length')} characters of the pack, expected {js_len(text)}.")
    if res.get("sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest():
        raise ChatGPTError("The pack in the page doesn't match the file (checksum differs). Not sent.")


def check_filled(res: dict) -> None:
    if not res.get("edit_open"):
        raise ChatGPTError("The edit box closed before sending. Not sent.")
    if not res.get("match"):
        raise ChatGPTError(
            f"The edit box doesn't match the pack ({res.get('edit_len')} vs {res.get('pack_len')} characters "
            f"after normalizing; first difference at {res.get('diff_at')}: pack {res.get('pack_near')!r}, "
            f"box {res.get('edit_near')!r}). Not sent.")
    if res.get("attachments"):
        raise ChatGPTError("ChatGPT shows a file attachment next to the message. Not sent.")
    button = res.get("button") or ""
    if not re.search(r"\bPro\b", button):
        raise ChatGPTError(f"The model button reads {button!r}, not Pro. Not sent.")


# --- Browser scripts -------------------------------------------------------------------------------
# Every step is one script for browser-harness: the prelude (helpers below) + the step's run() + MAIN.
# The script prints one MARK line with a JSON result. Page code comes from LIB_JS and the JS snippets,
# passed in as data, so the same strings are what tests syntax-check.

LIB_JS = r"""
const WS = /__WS__+/g;
const J = {
  vis(el) {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  },
  txt(el) { return el ? String(el.innerText || el.textContent || '').trim() : ''; },
  norm(s) { return String(s || '').replace(WS, ' ').replace(/^ +| +$/g, '').replace(/ (?=http)/g, ''); },
  rect(el) {
    if (!el) return null;
    el.scrollIntoView({block: 'center'});
    const r = el.getBoundingClientRect();
    return {x: r.x + r.width / 2, y: r.y + r.height / 2};
  },
  modelBtn() {
    return [...document.querySelectorAll('button[aria-label="Select ChatGPT model"]')].find(J.vis) || null;
  },
  slider() {
    const all = [...document.querySelectorAll('[role="slider"]')].filter(J.vis);
    return all.find(s => s.getAttribute('aria-valuemax') === '4') || all[0] || null;
  },
  sliderInfo() {
    const s = J.slider();
    if (!s) return null;
    let label = '';
    for (let el = s.parentElement, i = 0; el && i < 6 && !label; el = el.parentElement, i++) label = J.txt(el);
    return {value: Number(s.getAttribute('aria-valuenow')), max: Number(s.getAttribute('aria-valuemax')),
            valuetext: s.getAttribute('aria-valuetext') || '', label: label.slice(0, 200)};
  },
  stopBtn() {
    return [...document.querySelectorAll('button')].find(b => J.vis(b) &&
      (b.getAttribute('data-testid') === 'stop-button' || /stop/i.test(b.getAttribute('aria-label') || ''))) || null;
  },
  composer() {
    const c = document.querySelector('#prompt-textarea[contenteditable="true"]');
    if (c) return c;
    const eds = [...document.querySelectorAll('div.ProseMirror[contenteditable="true"]')].filter(J.vis);
    eds.sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom);
    return eds[0] || null;
  },
  edit() {
    const sends = [...document.querySelectorAll('button')].filter(b => J.txt(b) === 'Send' && J.vis(b));
    for (const send of sends) {
      for (let w = send.parentElement, i = 0; w && i < 8; w = w.parentElement, i++) {
        const cancel = [...w.querySelectorAll('button')].find(b => J.txt(b) === 'Cancel');
        const box = [...w.querySelectorAll('[contenteditable="true"], textarea')].find(e => e.id !== 'prompt-textarea');
        if (cancel && box) return {box, send, cancel, wrap: w};
      }
    }
    return null;
  },
  sendBtn() {
    const e = J.edit();
    const inEdit = b => !!(e && e.wrap.contains(b));
    const byId = document.querySelector('button[data-testid="send-button"]');
    if (byId && !inEdit(byId)) return byId;
    return [...document.querySelectorAll('button[aria-label*="Send" i]')].find(b => J.vis(b) && !inEdit(b)) || null;
  },
  editBtns() { return [...document.querySelectorAll('button[aria-label="Edit message"]')]; },
  boxText(box) { return box.tagName === 'TEXTAREA' ? box.value : String(box.innerText || ''); },
  attachments(scope) {
    if (!scope) return 0;
    const sel = 'button[aria-label*="Remove file" i], button[aria-label*="Remove attachment" i], ' +
      '[data-testid*="attachment" i], [data-testid*="file-tile" i], [data-testid*="file-preview" i]';
    return [...scope.querySelectorAll(sel)].filter(J.vis).length;
  },
  usage() {
    const m = String(document.body.innerText || '').match(/\d{1,3}\s*%\s*usage remaining[^\n]*/i);
    return m ? m[0] : null;
  },
  assistants(scope) {
    return [...(scope || document).querySelectorAll('[data-message-author-role="assistant"], ' +
      '[data-content-search-unit-key$=":assistant"], article[data-turn="assistant"]')];
  },
  // The answer inside one assistant turn. Class names change (a finished reply once had no .markdown at
  // all), so try a few and fall back to the turn itself; J.md skips buttons and screen-reader-only text.
  replyRoot(node) {
    if (!node) return null;
    for (const sel of ['.markdown', '[class*="markdown"]', '.prose', '[data-message-content]']) {
      const m = [...node.querySelectorAll(sel)];
      if (m.length) return m[m.length - 1];
    }
    return node;
  },
  md(root) {
    const SKIP = new Set(['button', 'svg', 'style', 'script', 'img']);
    const BLOCK = new Set(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'pre', 'blockquote', 'table',
                           'hr', 'div', 'section', 'li']);
    const tag = n => n.nodeType === 1 ? n.tagName.toLowerCase() : '';
    const off = n => /(^|\s)sr-only(\s|$)/.test(String(n.className || ''));   // screen-reader-only labels
    const inl = n => {
      if (n.nodeType === 3) return n.textContent;
      const t = tag(n);
      if (!t || SKIP.has(t) || off(n)) return '';
      const kids = () => [...n.childNodes].map(inl).join('');
      if (t === 'br') return '\n';
      if (t === 'code') return '`' + n.textContent + '`';
      if (t === 'strong' || t === 'b') { const s = kids(); return s.trim() ? '**' + s + '**' : s; }
      if (t === 'em' || t === 'i') { const s = kids(); return s.trim() ? '*' + s + '*' : s; }
      if (t === 'a') {
        const h = n.getAttribute('href') || '';
        const s = kids();
        return h && !h.startsWith('#') ? '[' + s + '](' + h + ')' : s;
      }
      return kids();
    };
    const kidsBlk = (n, d) => [...n.childNodes].map(c => blk(c, d)).join('');
    const item = (li, d) => {
      let s = '';
      for (const c of li.childNodes) {
        const t = tag(c);
        if (t === 'ul' || t === 'ol') s = s.replace(/\s+$/, '') + '\n' + blk(c, d + 1).replace(/\n+$/, '');
        else if (t === 'p') s += inl(c).trim() + ' ';
        else if (t === 'pre') s += '\n' + blk(c, d).trim() + '\n';
        else s += inl(c);
      }
      return s.trim();
    };
    const blk = (n, d) => {
      if (n.nodeType === 3) return n.textContent.trim() ? n.textContent : '';
      const t = tag(n);
      if (!t || SKIP.has(t) || off(n)) return '';
      if (/^h[1-6]$/.test(t)) return '#'.repeat(Number(t[1])) + ' ' + inl(n).trim() + '\n\n';
      if (t === 'p') return inl(n).trim() + '\n\n';
      if (t === 'hr') return '---\n\n';
      if (t === 'pre') {
        const code = n.querySelector('code');
        const m = ((code && code.className) || '').match(/language-([\w+#.-]+)/);
        return '```' + (m ? m[1] : '') + '\n' + (code || n).textContent.replace(/\n+$/, '') + '\n```\n\n';
      }
      if (t === 'ul' || t === 'ol') {
        let i = Number(n.getAttribute('start') || 1);
        const items = [...n.children].filter(c => tag(c) === 'li')
          .map(li => '  '.repeat(d) + (t === 'ol' ? (i++) + '. ' : '- ') + item(li, d));
        return items.join('\n') + (d ? '\n' : '\n\n');
      }
      if (t === 'blockquote') return kidsBlk(n, d).trim().split('\n').map(l => '> ' + l).join('\n') + '\n\n';
      if (t === 'table') {
        const rows = [...n.querySelectorAll('tr')].map(tr => [...tr.children]
          .map(c => inl(c).trim().replace(/\|/g, '\\|').replace(/\n+/g, ' ')));
        if (!rows.length) return '';
        const w = Math.max(...rows.map(r => r.length));
        const line = r => '| ' + r.concat(Array(w - r.length).fill('')).join(' | ') + ' |';
        return [line(rows[0]), '|' + ' --- |'.repeat(w), ...rows.slice(1).map(line)].join('\n') + '\n\n';
      }
      const hasBlock = [...n.children].some(c => BLOCK.has(tag(c)));
      if (hasBlock) return kidsBlk(n, d);
      const s = inl(n).trim();
      return s ? s + '\n\n' : '';
    };
    return kidsBlk(root, 0).replace(/\n{3,}/g, '\n\n').trim();
  },
};
""".replace("__WS__", WS_CLASS)

# Page snippets: bodies of an async function with J in scope; named constants come from page(name, **vars).
JS: dict[str, str] = {
    "state": "return {href: location.href, ready: document.readyState, "
             "composer: !!J.composer(), model: !!J.modelBtn(), edits: J.editBtns().length};",
    "session": r"""
try {
  const r = await fetch('/api/auth/session', {credentials: 'include'});
  const j = r.ok ? await r.json() : null;
  return {logged_in: !!(j && j.accessToken), email: (j && j.user && j.user.email) || null, http: r.status};
} catch (e) { return {logged_in: false, error: String(e)}; }""",
    "usage": "return J.usage();",
    "slider": "return J.sliderInfo();",
    "slider_focus": "const s = J.slider(); if (!s) return false; s.focus(); return true;",
    "model_btn_exists": "return !!J.modelBtn();",
    "model_btn_pointer": r"""
const b = J.modelBtn(); if (!b) return false;
const o = {bubbles: true, cancelable: true, button: 0, buttons: 1, pointerType: 'mouse', isPrimary: true};
b.dispatchEvent(new PointerEvent('pointerdown', o)); b.dispatchEvent(new MouseEvent('mousedown', o));
b.dispatchEvent(new PointerEvent('pointerup', o)); b.dispatchEvent(new MouseEvent('mouseup', o));
return true;""",
    "model_btn_click": "const b = J.modelBtn(); if (!b) return false; b.click(); return true;",
    "model_btn_rect": "return J.rect(J.modelBtn());",
    "button_text": "return J.txt(J.modelBtn());",
    "pointer_outside": r"""
const e = J.edit(); const el = e ? e.box : document.body;
const o = {bubbles: true, cancelable: true, button: 0, pointerType: 'mouse', isPrimary: true};
el.dispatchEvent(new PointerEvent('pointerdown', o)); el.dispatchEvent(new PointerEvent('pointerup', o));
return true;""",
    "dom_key": r"""
const el = TARGET === 'slider' ? J.slider() : (J.edit() || {}).box;
if (!el) return false;
el.focus();
const o = {key: KEY, code: CODE, keyCode: KC, which: KC, metaKey: META, bubbles: true, cancelable: true};
el.dispatchEvent(new KeyboardEvent('keydown', o)); el.dispatchEvent(new KeyboardEvent('keyup', o));
return true;""",
    "composer_focus": "const c = J.composer(); if (!c) return false; c.focus(); return true;",
    "composer_text": "const c = J.composer(); return c ? J.boxText(c) : null;",
    "composer_exec_insert": r"""
const c = J.composer(); if (!c) return false;
c.focus(); document.execCommand('selectAll'); document.execCommand('insertText', false, MSG);
return true;""",
    "send_ready": "const b = J.sendBtn(); return !!(b && !b.disabled && b.getAttribute('aria-disabled') !== 'true');",
    "send_click": "const b = J.sendBtn(); if (!b) return false; b.click(); return true;",
    "counts": r"""
const main = document.body;  // the turns aren't always inside <main> (seen 2026-10-31)
let mds = J.assistants(main);
if (!mds.length) mds = [...main.querySelectorAll('.markdown')];
return {edits: J.editBtns().length, md: mds.length, md_last: mds.length ? J.txt(mds[mds.length - 1]).length : 0,
        stop: !!J.stopBtn(), href: location.href, hits: String(main.innerText || '').split(TITLE).length - 1};""",
    "edit_click": r"""
const bs = J.editBtns(); if (!bs.length) return null;
let pick = null;
for (let i = bs.length - 1; i >= 0 && !pick; i--) {
  for (let el = bs[i].parentElement, k = 0; el && k < 10; el = el.parentElement, k++) {
    const t = J.txt(el);
    if (t.includes(TITLE)) { pick = bs[i]; break; }
    if (t.length > TITLE.length + 4000) break;
  }
}
(pick || bs[bs.length - 1]).click();
return {matched: !!pick, count: bs.length};""",
    "edit_btn_rect": "const bs = J.editBtns(); return bs.length ? J.rect(bs[bs.length - 1]) : null;",
    "edit_state": "const e = J.edit(); if (!e) return null; const t = J.boxText(e.box); "
                  "return {text: t.slice(0, 4000), len: t.length};",
    "edit_cancel": "const e = J.edit(); if (e) e.cancel.click(); return !!e;",
    "edit_focus": "const e = J.edit(); if (!e) return false; e.box.focus(); return true;",
    "edit_len": "const e = J.edit(); return e ? J.boxText(e.box).trim().length : -1;",
    "edit_exec_select": r"""
const e = J.edit(); if (!e) return false;
e.box.focus(); if (e.box.select) e.box.select(); else document.execCommand('selectAll');
return true;""",
    "edit_exec_delete": "document.execCommand('delete'); return true;",
    "pack_reset": "window.__johtoPack = []; return true;",
    "pack_digest": r"""
const parts = window.__johtoPack || [];
const t = parts.join('');
const h = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(t));
return {length: t.length, chunks: parts.length,
        sha256: [...new Uint8Array(h)].map(b => b.toString(16).padStart(2, '0')).join('')};""",
    "paste": r"""
const e = J.edit(); if (!e) throw new Error('The edit box closed');
const text = (window.__johtoPack || []).join('');
if (text.length !== EXPECTED) throw new Error('The page holds ' + text.length + ' characters, expected ' + EXPECTED);
const box = e.box;
box.focus();
if (box.tagName === 'TEXTAREA') {
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(box, text);
  box.dispatchEvent(new Event('input', {bubbles: true}));
  return {handled: true, how: 'textarea'};
}
const dt = new DataTransfer();
dt.setData('text/plain', text);
const ev = new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true});
box.dispatchEvent(ev);
return {handled: ev.defaultPrevented, how: 'paste'};""",
    "verify": r"""
const e = J.edit();
const open = !!J.slider();
const button = J.txt(J.modelBtn());
if (!e) return {edit_open: false, button, picker_open: open};
const pack = J.norm((window.__johtoPack || []).join(''));
const got = J.norm(J.boxText(e.box));
let at = -1;
if (pack !== got) {
  const n = Math.min(pack.length, got.length);
  at = n;
  for (let i = 0; i < n; i++) { if (pack.charCodeAt(i) !== got.charCodeAt(i)) { at = i; break; } }
}
const near = s => at < 0 ? '' : s.slice(Math.max(0, at - 60), at + 60);
const c = J.composer();
const scopes = [e.wrap, c ? (c.closest('form') || c.parentElement) : null];
return {edit_open: true, match: pack === got, pack_len: pack.length, edit_len: got.length, diff_at: at,
        pack_near: near(pack), edit_near: near(got), button, picker_open: open,
        attachments: scopes.reduce((n, s) => n + J.attachments(s), 0)};""",
    "submit_click": "const e = J.edit(); if (!e) return false; e.send.click(); return true;",
    "submit_rect": "const e = J.edit(); return e && !e.send.disabled ? J.rect(e.send) : null;",
    "progress": r"""
const main = document.querySelector('main') || document.body;
const thinking = [...main.querySelectorAll('div, span')].some(el => el.childElementCount === 0 &&
  /^(Pro thinking|Thinking|Reasoning)\b/.test(String(el.textContent || '').trim()) && J.vis(el));
return {stop: !!J.stopBtn(), thinking, edit_open: !!J.edit(), href: location.href};""",
    # The access token stays inside this function; only message data is returned.
    "conversation": r"""
let token = null;
try {
  const s = await fetch('/api/auth/session', {credentials: 'include'});
  if (s.ok) { const j = await s.json(); token = j && j.accessToken; }
} catch (e) {}
if (!token) return {api_error: 'logged_out'};
let r;
try {
  r = await fetch('/backend-api/conversation/' + encodeURIComponent(CID),
                  {headers: {Authorization: 'Bearer ' + token}, credentials: 'include'});
} catch (e) { return {api_error: 'fetch failed: ' + String(e)}; }
token = null;
if (!r.ok) return {http: r.status};
let conv;
try { conv = await r.json(); } catch (e) { return {http: r.status, api_error: 'not JSON'}; }
const m = conv && conv.mapping;
if (!m || !conv.current_node) return {http: r.status, api_error: 'unexpected shape: ' + Object.keys(conv || {}).slice(0, 12).join(',')};
const after = [];
let user = null, id = conv.current_node, guard = 0;
while (id && m[id] && guard++ < 100000) {
  const msg = m[id].message;
  if (msg) {
    const role = msg.author && msg.author.role;
    const hidden = msg.metadata && msg.metadata.is_visually_hidden_from_conversation;
    if (role === 'user' && !hidden) {
      const parts = (msg.content && msg.content.parts) || [];
      const text = parts.map(p => typeof p === 'string' ? p : (p && typeof p.text === 'string' ? p.text : ''))
        .filter(x => x).join('\n\n');
      const n = J.norm(text);
      const hex = async t => [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(t)))]
        .map(b => b.toString(16).padStart(2, '0')).join('');
      user = {id: msg.id, create_time: msg.create_time, chars: text.length, norm_chars: n.length,
              norm_sha: await hex(n), dense_sha: await hex(text.replace(WS, '')), head: n.slice(0, 200),
              tail: n.slice(-200)};
      break;
    }
    after.push(msg);
  }
  id = m[id].parent;
}
after.reverse();
return {http: r.status, title: conv.title || null, current_node: conv.current_node, user, after};""",
    # The access token stays inside this function too; only the outcome is returned.
    "rename": r"""
let token = null;
try {
  const s = await fetch('/api/auth/session', {credentials: 'include'});
  if (s.ok) { const j = await s.json(); token = j && j.accessToken; }
} catch (e) {}
if (!token) return {api_error: 'logged_out'};
let r;
try {
  r = await fetch('/backend-api/conversation/' + encodeURIComponent(CID),
                  {method: 'PATCH', credentials: 'include', body: JSON.stringify({title: TITLE}),
                   headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'}});
} catch (e) { return {api_error: 'fetch failed: ' + String(e)}; }
token = null;
let j = null;
try { j = await r.json(); } catch (e) {}
return {http: r.status, success: !!(j && j.success)};""",
    "dom_reply": r"""
const main = document.body;  // the turns aren't always inside <main> (seen 2026-10-31)
const stop = !!J.stopBtn();
const turns = J.assistants(main);
const users = [...main.querySelectorAll('[data-message-author-role="user"], [data-content-search-unit-key$=":user"], article[data-turn="user"]')];
let node = turns.length ? turns[turns.length - 1] : null;
if (node && users.length && (node.compareDocumentPosition(users[users.length - 1]) & Node.DOCUMENT_POSITION_FOLLOWING)) node = null;
let root = node ? J.replyRoot(node) : null;
if (!turns.length) {
  const mds = [...main.querySelectorAll('.markdown, [class*="markdown"]')];
  root = mds.length ? mds[mds.length - 1] : null;
}
const md = root ? J.md(root) : null;
// While Pro thinks, the turn holds only its status line; that is not the answer.
const thinking = !!md && md.length < 200 && /^(Pro thinking|Thinking|Reasoning|Thought for)\b/i.test(md.trim());
const holder = (node || root) && (node || root).closest('[data-message-id]');
const key = node && (node.getAttribute('data-message-id') || (node.getAttribute('data-content-search-unit-key') || '').split(':')[0]);
return {stop: stop || thinking, found: !!root, markdown: thinking ? null : md,
        message_id: key || (holder && holder.getAttribute('data-message-id')) || null};""",
}

_PRELUDE = r'''
import json, time
import helpers as _H

ARGS = json.loads(__ARGS__)
MARK = ARGS["mark"]
ACCOUNT = ARGS["account"].lower()
JS = ARGS["js"]
LIB = ARGS["lib"]
DEADLINE = time.time() + ARGS["timeout"]
SID = None
INFO = {}
_KEYS = {"ArrowLeft": (37, "ArrowLeft"), "ArrowRight": (39, "ArrowRight"), "Backspace": (8, "Backspace"),
         "Escape": (27, "Escape"), "a": (65, "KeyA")}


class Fail(Exception):
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


def left():
    return DEADLINE - time.time()


def norm(s):
    return " ".join(str(s or "").split())


def ev(expr):
    r = cdp("Runtime.evaluate", session_id=SID, expression=expr, returnByValue=True, awaitPromise=True)
    if r.get("exceptionDetails"):
        d = r["exceptionDetails"]
        msg = (d.get("exception") or {}).get("description") or d.get("text") or "error"
        raise Fail("page", str(msg).splitlines()[0][:300])
    return (r.get("result") or {}).get("value")


def page(name, **consts):
    """Run one JS snippet (LIB's J in scope, consts as JS constants) and return its JSON result."""
    pre = "".join("const %s = %s;\n" % (k, json.dumps(v)) for k, v in consts.items())
    src = ("(async () => { /*" + name + "*/\n" + LIB + "\n" + pre + "const __f = async () => {\n" + JS[name]
           + "\n};\nreturn JSON.stringify(await __f());\n})()")
    v = ev(src)
    return json.loads(v) if isinstance(v, str) else v


def until(name, secs, **consts):
    end = min(time.time() + secs, DEADLINE)
    while True:
        v = page(name, **consts)
        if v or time.time() >= end:
            return v
        time.sleep(0.4)


def key(k, mods=0, commands=None):
    """A real key press through CDP, sent to this tab's session (the tab stays in the background)."""
    vk, code = _KEYS[k]
    base = {"key": k, "code": code, "modifiers": mods, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
    down = dict(base, type="keyDown")
    if len(k) == 1 and not mods:
        down["text"] = k
    if commands:
        down["commands"] = commands
    cdp("Input.dispatchKeyEvent", session_id=SID, **down)
    cdp("Input.dispatchKeyEvent", session_id=SID, type="keyUp", **base)


def click_at(x, y):
    cdp("Input.dispatchMouseEvent", session_id=SID, type="mouseMoved", x=x, y=y)
    for t in ("mousePressed", "mouseReleased"):
        cdp("Input.dispatchMouseEvent", session_id=SID, type=t, x=x, y=y, button="left", clickCount=1)


def _pages():
    return {t["targetId"]: t for t in cdp("Target.getTargets")["targetInfos"] if t["type"] == "page"}


def _account_of(t):
    try:
        return (_H._profile_accounts().get(t.get("browserContextId")) or "").lower() or None
    except Exception:
        return None


def _attach(tid):
    """Attach to a tab without activating it (switch_tab minus Target.activateTarget and set_session,
    so the shared daemon's current tab doesn't move)."""
    global SID
    SID = cdp("Target.attachToTarget", targetId=tid, flatten=True)["sessionId"]
    for method, params in (("Emulation.setFocusEmulationEnabled", {"enabled": True}),
                           ("Page.setWebLifecycleState", {"state": "active"})):
        try:
            cdp(method, session_id=SID, **params)
        except Exception:
            pass


def use_tab(mode="ours", start="https://chatgpt.com/"):
    """Attach to the review tab. mode "any" also accepts another chatgpt.com tab of ACCOUNT (read-only
    steps); a missing review tab is created in ACCOUNT's profile, which brings it forward once."""
    pages = _pages()
    tid = ARGS.get("target_id")
    if tid in pages and _account_of(pages[tid]) in (None, ACCOUNT):
        _attach(tid)
        INFO.update(target_id=tid, ours=True, created=False)
        return
    if mode == "any":
        for t in pages.values():
            if (t.get("url") or "").startswith("https://chatgpt.com") and _account_of(t) == ACCOUNT:
                _attach(t["targetId"])
                INFO.update(target_id=t["targetId"], ours=False, created=False)
                return
    try:
        prev = _H._send({"meta": "session"}).get("session_id")
    except Exception:
        prev = None
    try:
        tid = new_tab(start, account=ACCOUNT)
    except StopIteration:
        raise Fail("no_window", "No ordinary tab in that profile")
    except RuntimeError as e:
        raise Fail("no_window" if "No open Chrome window" in str(e) else "tab", str(e)[:300])
    if prev:
        try:
            _H._send({"meta": "set_session", "session_id": prev})
        except Exception:
            pass
    _attach(tid)
    INFO.update(target_id=tid, ours=True, created=True)


def nav(url, secs=45):
    """Load url in the attached tab and wait for the new document (matched by the navigation's loaderId,
    so nothing has to run in the old page, which may be frozen after hours in the background)."""
    r = cdp("Page.navigate", session_id=SID, url=url)
    if r.get("errorText"):
        raise Fail("load", "Couldn't open %s: %s" % (url, r["errorText"]))
    loader = r.get("loaderId")
    end = min(time.time() + secs, DEADLINE)
    st = None
    usable = 0
    while time.time() < end:
        time.sleep(0.5)
        try:
            if loader and cdp("Page.getFrameTree", session_id=SID)["frameTree"]["frame"].get("loaderId") != loader:
                continue
            st = page("state")
        except Exception:
            continue
        if st and st["ready"] == "complete":
            return st
        # A background ChatGPT tab can stay "loading" for minutes while the app already works:
        # accept it once the composer and model picker have been there for a few polls.
        usable = usable + 1 if st and st.get("composer") and st.get("model") else 0
        if usable >= 4:
            return st
    raise Fail("load", "ChatGPT didn't load %s (last state %s)" % (url, json.dumps(st)))


def on_chatgpt():
    """Make sure the attached tab shows chatgpt.com (navigating only our own tab)."""
    try:
        href = ev("location.href") or ""
    except Fail:
        href = ""
    if href.startswith("https://chatgpt.com"):
        return
    if not INFO.get("ours"):
        raise Fail("tab", "That tab isn't on chatgpt.com")
    nav("https://chatgpt.com/")


def open_picker():
    if not page("model_btn_exists"):
        raise Fail("model", "No model button on the page")
    for how in ("pointer", "click", "mouse"):
        if how == "mouse":
            r = page("model_btn_rect")
            if r:
                click_at(r["x"], r["y"])
        else:
            page("model_btn_" + how)
        info = until("slider", 2.5)
        if info:
            return info
    raise Fail("model", "The model picker didn't open")
'''

_MAIN = r'''
try:
    _res = run() or {}
    _out = {"ok": True, **_res, **INFO}
except Fail as e:
    _out = {"ok": False, "code": e.code, "error": str(e), **INFO}
except Exception as e:
    _out = {"ok": False, "code": "error", "error": (type(e).__name__ + ": " + str(e))[:600], **INFO}
finally:
    if SID:
        try:
            cdp("Target.detachFromTarget", sessionId=SID)
        except Exception:
            pass
print(MARK + json.dumps(_out), flush=True)
'''

STEPS: dict[str, str] = {
    "open": r'''
def run():
    url, cid = ARGS["url"], ARGS.get("conversation_id")
    use_tab("ours", start=url)
    if not INFO.get("created"):
        try:
            page("edit_cancel")      # a dry run or refused send may have left the edit box filled
        except Exception:
            pass
    nav(url, secs=min(45, left() - 5))
    st = None
    while left() > 5:
        st = page("state")
        if st["composer"]:
            break
        time.sleep(0.5)
    else:
        raise Fail("load", "No message box on ChatGPT (state %s)" % json.dumps(st))
    s = page("session")
    if s.get("error"):
        raise Fail("load", "ChatGPT's session check failed: " + s["error"])
    if not s.get("logged_in"):
        raise Fail("logged_out", "No ChatGPT session")
    away = 0
    while left() > 5:
        st = page("state")
        if cid and cid not in st["href"]:
            away += 1
            if away >= 6:
                raise Fail("chat_missing", "The chat didn't open (the tab went to %s)" % st["href"])
        elif st["model"] and (not cid or st["edits"] > 0):
            break
        time.sleep(0.5)
    else:
        raise Fail("load", "ChatGPT didn't finish loading (state %s)" % json.dumps(st))
    time.sleep(1.0)
    return {"url": st["href"], "usage": page("usage"), "email": s.get("email")}
''',
    "model": r'''
def run():
    use_tab("ours")
    target = ARGS["target"]
    info = page("slider") or open_picker()
    tries = 0
    while info["value"] != target:
        tries += 1
        if tries > 10:
            raise Fail("model", "The model slider stopped at %s" % info["value"])
        k = "ArrowRight" if info["value"] < target else "ArrowLeft"
        page("slider_focus")
        key(k)
        time.sleep(0.3)
        new = page("slider")
        if new and new["value"] == info["value"]:
            page("dom_key", TARGET="slider", KEY=k, CODE=k, KC=_KEYS[k][0], META=False)
            time.sleep(0.3)
            new = page("slider")
        if not new:
            raise Fail("model", "The model picker closed while moving the slider")
        info = new
    time.sleep(0.3)
    info = page("slider") or info
    if ARGS.get("escape"):
        key("Escape")
        time.sleep(0.4)
        if page("slider"):
            page("pointer_outside")
            time.sleep(0.4)
    info["button"] = page("button_text")
    info["picker_open"] = bool(page("slider"))
    return info
''',
    "title": r'''
def run():
    use_tab("ours")
    msg, title = ARGS["message"], ARGS["title"]
    before = page("counts", TITLE=title)
    if not page("composer_focus"):
        raise Fail("composer", "No message box on the page")
    if norm(page("composer_text")):
        key("a", 4, ["selectAll"])
        key("Backspace")
        time.sleep(0.3)
        if norm(page("composer_text")):
            raise Fail("composer", "The message box holds a draft that wouldn't clear")
    cdp("Input.insertText", session_id=SID, text=msg)
    time.sleep(0.4)
    if norm(page("composer_text")) != norm(msg):
        page("composer_exec_insert", MSG=msg)
        time.sleep(0.4)
        if norm(page("composer_text")) != norm(msg):
            raise Fail("composer", "Couldn't type the title message")
    if not until("send_ready", 5):
        raise Fail("composer", "The send button isn't available")
    page("send_click")
    sent_at, stable, last = time.time(), 0, None
    while left() > 3:
        last = page("counts", TITLE=title)
        sent = last["edits"] > before["edits"] or last["hits"] > before["hits"]
        replied = (last["md"] > before["md"] and last["md_last"] > 0) or last["hits"] >= before["hits"] + 2
        if sent and replied and not last["stop"] and time.time() - sent_at > 2:
            stable += 1
            if stable >= 2:
                return {"url": last["href"], "edits": last["edits"]}
        else:
            stable = 0
        time.sleep(1)
    raise Fail("title", "ChatGPT didn't answer the title message in time (%s)" % json.dumps(last))
''',
    "edit": r'''
def run():
    use_tab("ours")
    title = ARGS["title"]
    r = page("edit_click", TITLE=title)
    if not r:
        raise Fail("edit", "No Edit message button on the page")
    st = until("edit_state", 6)
    if not st:
        rect = page("edit_btn_rect")
        if rect:
            click_at(rect["x"], rect["y"])
        st = until("edit_state", 6)
    if not st:
        raise Fail("edit", "The edit box didn't open")
    if title not in st["text"]:
        page("edit_cancel")
        raise Fail("edit", "The edit box opened on another message")
    return {"matched": r.get("matched"), "chars": st["len"]}
''',
    "load": r'''
def run():
    use_tab("ours")
    page("pack_reset")
    exprs = ARGS["exprs"]
    for i, expr in enumerate(exprs):
        n = ev(expr)
        if n != i + 1:
            raise Fail("load", "Chunk %d of %d landed as %s" % (i + 1, len(exprs), n))
    return page("pack_digest")
''',
    "fill": r'''
def run():
    use_tab("ours")
    if not page("edit_focus"):
        raise Fail("edit", "The edit box isn't open")
    time.sleep(0.3)
    if page("slider"):
        page("pointer_outside")
        time.sleep(0.4)
        page("edit_focus")
    cleared = "keys"
    key("a", 4, ["selectAll"])
    key("Backspace")
    time.sleep(0.3)
    if page("edit_len") > 0:
        cleared = "dom keys"
        page("dom_key", TARGET="edit", KEY="a", CODE="KeyA", KC=65, META=True)
        page("dom_key", TARGET="edit", KEY="Backspace", CODE="Backspace", KC=8, META=False)
        time.sleep(0.3)
    if page("edit_len") > 0:
        cleared = "execCommand"
        page("edit_exec_select")
        time.sleep(0.5)
        page("edit_exec_delete")
        time.sleep(0.3)
    n = page("edit_len")
    if n < 0:
        raise Fail("edit", "The edit box closed while clearing it")
    if n:
        raise Fail("clear", "The edit box still holds %d characters after clearing" % n)
    p = page("paste", EXPECTED=ARGS["js_len"])
    prev = -1
    while left() > 15:
        n = page("edit_len")
        if n == prev and n > 0:
            break
        prev = n
        time.sleep(1)
    res = page("verify")
    if res.get("picker_open"):
        page("pointer_outside")
        time.sleep(0.4)
        res["button"] = page("button_text")
    res.update(cleared_by=cleared, paste=p)
    return res
''',
    "submit": r'''
def run():
    use_tab("ours")
    if not page("submit_click"):
        raise Fail("submit", "The edit box's Send button is gone")
    t0, retried, st = time.time(), False, None
    while left() > 3:
        st = page("progress")
        if st["stop"] or st["thinking"]:
            return {"started": True, "signal": "stop" if st["stop"] else "thinking", "url": st["href"]}
        if st["edit_open"] and not retried and time.time() - t0 > 20:
            r = page("submit_rect")
            if r:
                click_at(r["x"], r["y"])
            retried = True
        time.sleep(1)
    return {"started": False, "url": (st or {}).get("href"), "edit_open": (st or {}).get("edit_open")}
''',
    "fetch": r'''
def run():
    use_tab("any")
    on_chatgpt()
    return page("conversation", CID=ARGS["conversation_id"])
''',
    "rename": r'''
def run():
    use_tab("any")
    on_chatgpt()
    return page("rename", CID=ARGS["conversation_id"], TITLE=ARGS["title"])
''',
    "fetch_dom": r'''
def run():
    use_tab("ours")
    try:
        href = ev("location.href") or ""
    except Fail:
        href = ""
    if ARGS["conversation_id"] not in href:
        nav(ARGS["url"], secs=min(45, left() - 10))
    st = None
    while left() > 5:
        st = page("state")
        if st["edits"] > 0:
            break
        time.sleep(0.5)
    time.sleep(1.0)
    return page("dom_reply")
''',
    "usage": r'''
def run():
    use_tab("any")
    on_chatgpt()
    time.sleep(0.5)
    return {"usage": until("usage", 5)}
''',
    "close": r'''
def run():
    tid = ARGS.get("target_id")
    if tid and tid in _pages():
        cdp("Target.closeTarget", targetId=tid)
        return {"closed": True}
    return {"closed": False}
''',
}

STEP_NAMES = {
    "open": "opening the chat", "model": "choosing the model", "title": "sending the title message",
    "edit": "opening the edit box", "load": "loading the pack into the page", "fill": "filling the edit box",
    "submit": "sending", "rename": "naming the chat", "fetch": "reading the reply",
    "fetch_dom": "reading the reply from the page",
    "usage": "reading usage", "close": "closing the review tab",
}


def step_source(step: str, *, account: str, target_id: str | None, timeout: float, **args) -> str:
    """The Python for one step. Args travel as one JSON literal on the ARGS line."""
    full = {"step": step, "mark": MARK, "account": account, "target_id": target_id, "timeout": timeout,
            "lib": LIB_JS, "js": JS, **args}
    return _PRELUDE.replace("__ARGS__", repr(json.dumps(full))) + STEPS[step] + _MAIN


def build_script(step: str, **kw) -> str:
    """The browser-harness script for one step.

    browser-harness runs stdin with exec() inside a function, so functions a script defines can't
    call each other there. The step's source therefore runs in one namespace of its own.
    """
    return (f"# johto chatgpt step: {step}\n"
            f"_JOHTO_STEP = {step_source(step, **kw)!r}\n"
            "exec(compile(_JOHTO_STEP, '<johto chatgpt step>', 'exec'),"
            " {**globals(), **locals(), '__name__': '__johto_step__'})\n")


def parse_script(script: str) -> tuple[str, dict]:
    """(step, args) of a script from build_script: what fake runners use to answer."""
    step = re.match(r"# johto chatgpt step: (\w+)", script).group(1)
    m = re.search(r"^_JOHTO_STEP = (.*)$", script, re.M)
    src = ast.literal_eval(m.group(1)) if m else script
    line = next(ln for ln in src.splitlines() if ln.startswith("ARGS = json.loads("))
    return step, json.loads(ast.literal_eval(line[len("ARGS = json.loads("):-1]))


def parse_result(stdout: str, step: str) -> dict:
    line = next((ln for ln in reversed(stdout.splitlines()) if ln.startswith(MARK)), None)
    if line is None:
        raise ChatGPTError(f"ChatGPT {STEP_NAMES.get(step, step)}: no answer from the browser. "
                           f"Output: {stdout[-500:].strip()}")
    return json.loads(line[len(MARK):])


# --- Runners ---------------------------------------------------------------------------------------

def run_harness(script: str, timeout: float) -> str:
    try:
        out = subprocess.run(["browser-harness"], input=script, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise HarnessError("browser-harness isn't installed") from None
    except subprocess.TimeoutExpired:
        raise HarnessError(f"the browser step took longer than {timeout:.0f} s") from None
    if out.returncode != 0:
        raise HarnessError(out.stderr.strip()[-2000:] or "browser-harness failed")
    return out.stdout


def _restart_daemon() -> None:
    subprocess.run(["python3", "-c", "import admin; admin.restart_daemon()"],
                   cwd=str(Path.home() / "dev/browser-harness"), capture_output=True, timeout=60)


def _daemon_broken(msg: str) -> bool:
    return any(s in msg for s in ("keepalive", "websocket", "WebSocket", "close frame", "Broken pipe",
                                  "BrokenPipe", "Connection refused", "ConnectionRefused"))


def _real_ok() -> bool:
    flag = os.environ.get("JOHTO_REAL_CHATGPT", "").strip().lower() in ("1", "true", "yes", "on")
    return config.ROOT == config.CHECKOUT or config.REAL_OK or flag


def default_runner() -> Runner:
    """FakeBrowser in fake mode; the real harness only from the real checkout's data (or with a flag)."""
    if config.FAKE:
        from .fakes import guard_paths
        try:
            guard_paths()
        except RuntimeError as e:
            raise ChatGPTError(str(e)) from None
        return FakeBrowser(state_file=config.DATA / "fake" / "chatgpt.json")
    if not _real_ok():
        raise ChatGPTError(f"ChatGPT: refused because JOHTO_ROOT ({config.ROOT}) isn't this checkout. "
                           "Set JOHTO_REAL_CHATGPT=1 to allow it.")
    return run_harness


# --- Tab record and lock ---------------------------------------------------------------------------

def _tab_file() -> Path:
    return config.DATA / "chatgpt-tab.json"


def _read_json(p: Path) -> dict:
    try:
        v = json.loads(p.read_text())
        return v if isinstance(v, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(p)


def tab_id(account: str) -> str | None:
    return (_read_json(_tab_file()).get(account.lower()) or {}).get("target_id")


def save_tab_id(account: str, target_id: str | None) -> None:
    data = _read_json(_tab_file())
    if target_id:
        data[account.lower()] = {"target_id": target_id, "saved": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    else:
        data.pop(account.lower(), None)
    _write_json(_tab_file(), data)


def _lock_file() -> Path:
    return config.DATA / "chatgpt.lock"


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError):
        return True
    return True


@contextlib.contextmanager
def lock(what: str, wait: float = 0.0, log: Callable[[str], None] | None = None):
    """Hold data/chatgpt.lock while driving the review tab. Waits up to `wait` seconds for another
    holder; takes over a lock whose process is gone or that is older than LOCK_STALE_S."""
    p = _lock_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    end, told = time.time() + wait, False
    while True:
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            info = _read_json(p)
            if not _alive(info.get("pid")) or time.time() - float(info.get("t") or 0) > LOCK_STALE_S:
                p.unlink(missing_ok=True)
                continue
            if time.time() >= end:
                raise ChatGPTError(f"Another ChatGPT step is using the review tab ({info.get('what')}, "
                                   f"since {info.get('at')}). Try again when it finishes.") from None
            if log and not told:
                log(f"Waiting for another ChatGPT step to finish ({info.get('what')}).")
                told = True
            time.sleep(1.0)
            continue
        with os.fdopen(fd, "w") as f:
            json.dump({"pid": os.getpid(), "t": time.time(), "at": time.strftime("%H:%M:%S"), "what": what}, f)
        break
    try:
        yield
    finally:
        if _read_json(p).get("pid") == os.getpid():
            p.unlink(missing_ok=True)


# --- Driving the review tab ------------------------------------------------------------------------

def _friendly(step: str, account: str, code: str | None, error: str) -> str:
    if code == "no_window" or "No open Chrome window" in error:
        return f"Open a Chrome window in the {account} profile, then try again."
    if code == "logged_out":
        return f"ChatGPT isn't logged in in the {account} Chrome profile. Log in at chatgpt.com there, then try again."
    if "CDP WS handshake failed" in error or "DevToolsActivePort" in error:
        return "Chrome isn't reachable. Open Chrome (click Allow if it asks), then try again."
    return f"ChatGPT, {STEP_NAMES.get(step, step)}: {error}"


class Browser:
    """Runs step scripts against one profile's review tab and keeps its target id up to date."""

    def __init__(self, account: str, runner: Runner | None = None, log: Callable[[str], None] = print):
        self.account = account.lower()
        self.runner = runner or default_runner()
        self.log = log

    def call(self, step: str, timeout: float = 60.0, retry: bool = False, **args) -> dict:
        tid = tab_id(self.account)
        script = build_script(step, account=self.account, target_id=tid, timeout=timeout, **args)
        for attempt in (1, 2):
            try:
                res = parse_result(self.runner(script, timeout + RUNNER_SLACK_S), step)
            except HarnessError as e:
                if retry and attempt == 1 and _daemon_broken(str(e)):
                    _restart_daemon()
                    continue
                raise ChatGPTError(_friendly(step, self.account, None, str(e))) from None
            if not res.get("ok") and retry and attempt == 1 and _daemon_broken(res.get("error") or ""):
                _restart_daemon()
                continue
            break
        if res.get("ours") and res.get("target_id") and res["target_id"] != tid:
            save_tab_id(self.account, res["target_id"])
        if not res.get("ok"):
            raise ChatGPTError(_friendly(step, self.account, res.get("code"), res.get("error") or "failed"))
        return res


def _account(account: str | None) -> str:
    return (account or settings.get("chatgpt_account") or config.BROWSER_ACCOUNT).lower()


def send(text: str, title: str, *, chat_url: str | None = None, account: str | None = None,
         no_send: bool = False, log: Callable[[str], None] = print, runner: Runner | None = None,
         lock_wait: float = LOCK_WAIT_S, tag: str | None = None,
         on_chat: Callable[[str], None] | None = None) -> dict:
    """Send text as a GPT Pro message (a new chat, or a follow-up in chat_url) by Austin's method.

    tag is the short text of the message that gets edited (default: the title); a new chat is then named
    `title` through the conversation API. on_chat(url) is called once the chat's link is known, before the
    pack goes in, so a caller can record where an interrupted send may have landed.
    Returns {"chat_url", "sent", "chars", "usage_pct", "pack_sha", "tag", "titled"}. no_send=True stops
    with the pack in the edit box. Raises ChatGPTError on any failure, before sending when a check
    doesn't pass."""
    text = clean_text(text)
    if not text.strip():
        raise ChatGPTError("Nothing to send: the pack is empty.")
    title = " ".join(str(title or "").split())[:120] or "Johto review"
    tag = " ".join(str(tag or "").split())[:120] or title
    pack_sha = text_sha(text)
    cid = conversation_id(chat_url) if chat_url else None
    if chat_url and not cid:
        raise ChatGPTError(f"Not a ChatGPT chat link: {chat_url}")
    browser = Browser(_account(account), runner=runner, log=log)
    parts = chunks(text)
    exprs = [chunk_expr(c) for c in parts]
    with lock(f"send {title}", wait=lock_wait, log=log):
        opened = browser.call("open", url=chat_url or NEW_CHAT, conversation_id=cid, timeout=75, retry=True)
        usage_pct = parse_usage(opened.get("usage"))
        warn = settings.get("chatgpt_warn_usage_pct")
        if usage_pct is not None and warn is not None and usage_pct < warn:
            log(f"ChatGPT shows {usage_pct}% of this week's usage left. Sending anyway.")
        check_model(browser.call("model", target=INSTANT, escape=True, timeout=40, retry=True), INSTANT)
        t = browser.call("title", message=title_message(tag), title=tag, timeout=150)
        chat = canonical_url(t.get("url")) or (canonical_url(chat_url) if chat_url else None)
        if not chat:
            raise ChatGPTError(f"ChatGPT answered the title message but the tab isn't on a chat ({t.get('url')}).")
        log(f"Title message answered: {chat}")
        if on_chat:
            on_chat(chat)
        browser.call("edit", title=tag, timeout=40)
        check_model(browser.call("model", target=PRO, escape=False, timeout=40), PRO)
        loaded = browser.call("load", exprs=exprs, timeout=60 + 2 * len(exprs))
        check_loaded(loaded, text, len(exprs))
        filled = browser.call("fill", js_len=js_len(text), timeout=240)
        try:
            check_filled(filled)
        except ChatGPTError as e:
            raise ChatGPTError(str(e), chat_url=chat) from None
        log(f"The pack is in the edit box ({len(text):,} characters, Pro).")
        base = {"chars": len(text), "usage_pct": usage_pct, "pack_sha": pack_sha, "tag": tag, "titled": False}
        if no_send:
            log("Dry run: not sent. The edit box is still open in the review tab; cancel it there.")
            return {"chat_url": chat, "sent": False, **base}
        try:
            s = browser.call("submit", timeout=60)
        except ChatGPTError as e:
            # Only a missing Send button fails before the click; any other failure may come after it.
            raise ChatGPTError(str(e), chat_url=chat, maybe_sent=NO_SEND_BUTTON not in str(e)) from None
        url = canonical_url(s.get("url")) or chat
        if not s.get("started"):
            raise ChatGPTError("Clicked Send, but ChatGPT didn't show it working. Check the review tab.", chat_url=url,
                               maybe_sent=True)
        log(f"Sent to GPT Pro: {url}")
        titled = _rename(browser, url, title, log) if not cid else False
        return {"chat_url": url, "sent": True, **base, "titled": titled}


def _rename(browser: Browser, chat_url: str, title: str, log: Callable[[str], None]) -> bool:
    """Name a new chat through the conversation API (the token stays in the page). A failure only logs:
    the pack is already sent, and the chat keeps the tag as its name."""
    cid = conversation_id(chat_url)
    if not cid:
        return False
    try:
        r = browser.call("rename", conversation_id=cid, title=title, timeout=30)
    except ChatGPTError as e:
        log(f"Couldn't name the chat: {e}")
        return False
    if r.get("success"):
        log(f"Named the chat: {title}")
        return True
    log(f"Couldn't name the chat ({r.get('api_error') or 'HTTP %s' % r.get('http')}).")
    return False


def inspect(chat_url: str, *, account: str | None = None, runner: Runner | None = None,
            log: Callable[[str], None] | None = None) -> dict:
    """What a chat holds now, to settle a send whose outcome isn't known. Read-only (no navigation).

    Returns {"found", "last_user_chars", "last_user_sha", "last_user_dense_sha", "last_user_head",
    "last_user_tail", "assistant_status", "title"}: the newest user message's normalized length, text_sha(),
    dense_sha() and first and last 200 normalized characters (holds() compares them with a pack), and "none"
    (nothing after it yet), "thinking", "done" or "error" for the answer. found is False when the chat
    doesn't exist. Raises ChatGPTError when the API can't be read."""
    cid = conversation_id(chat_url)
    if not cid:
        raise ChatGPTError(f"Not a ChatGPT chat link: {chat_url}")
    browser = Browser(_account(account), runner=runner, log=log or (lambda *_: None))
    r = browser.call("fetch", conversation_id=cid, timeout=60, retry=True)
    if r.get("api_error") == "logged_out":
        raise ChatGPTError(_friendly("fetch", browser.account, "logged_out", ""))
    empty = {"last_user_chars": None, "last_user_sha": None, "last_user_dense_sha": None, "last_user_head": None,
             "last_user_tail": None, "title": None}
    if r.get("http") == 404:
        return {"found": False, **empty, "assistant_status": "none"}
    if r.get("http") != 200 or not isinstance(r.get("after"), list):
        raise ChatGPTError(f"Couldn't read the chat from ChatGPT's API ({r.get('api_error') or 'HTTP %s' % r.get('http')}).",
                           chat_url=chat_url)
    user = r.get("user") or {}
    after = r["after"]
    return {"found": True, "last_user_chars": user.get("norm_chars"), "last_user_sha": user.get("norm_sha"),
            "last_user_dense_sha": user.get("dense_sha"),
            "last_user_head": user.get("head"), "last_user_tail": user.get("tail"), "title": r.get("title"),
            "assistant_status": reply_from_chain(after)["status"] if after else "none"}


def holds(info: dict, text: str) -> bool | None:
    """Whether inspect()'s result shows `text` as the chat's newest user message: True, False, or None when
    it can't tell (no fingerprint). Exact on purpose (the whitespace-normalized hash, or the hash with all
    whitespace removed, for an editor that stored the spacing differently): a wrong yes would save the
    previous pack's answer as this one's review."""
    if not info.get("found"):
        return False
    sha, dense = info.get("last_user_sha"), info.get("last_user_dense_sha")
    if not sha and not dense:
        return None
    return bool((sha and sha == text_sha(text)) or (dense and dense == dense_sha(text)))


def fetch(chat_url: str, *, account: str | None = None, runner: Runner | None = None,
          log: Callable[[str], None] | None = None) -> dict:
    """The newest reply in chat_url: {"status": "thinking"|"done"|"error"|"not_found", "markdown",
    "message_id"} (plus "source" and "title", or "error"). Read-only; no navigation on the API path."""
    cid = conversation_id(chat_url)
    if not cid:
        raise ChatGPTError(f"Not a ChatGPT chat link: {chat_url}")
    browser = Browser(_account(account), runner=runner, log=log or (lambda *_: None))
    r = browser.call("fetch", conversation_id=cid, timeout=60, retry=True)
    if r.get("api_error") == "logged_out":
        raise ChatGPTError(_friendly("fetch", browser.account, "logged_out", ""))
    if r.get("http") == 404:
        return {"status": "not_found", "markdown": None, "message_id": None}
    if r.get("http") == 200 and isinstance(r.get("after"), list):
        return {**reply_from_chain(r["after"]), "source": "api", "title": r.get("title")}
    why = r.get("api_error") or f"HTTP {r.get('http')}"
    try:
        with lock("fetch from page", wait=0):
            d = browser.call("fetch_dom", url=chat_url, conversation_id=cid, timeout=90)
    except ChatGPTError as e:
        return {"status": "error", "markdown": None, "message_id": None,
                "error": f"ChatGPT's API failed ({why}) and the page couldn't be read: {e}"}
    md = (d.get("markdown") or "").strip() or None
    status = "thinking" if d.get("stop") or not md else "done"
    return {"status": status, "markdown": md, "message_id": d.get("message_id"), "source": "page"}


def usage(account: str | None = None, *, runner: Runner | None = None) -> int | None:
    """Percent of this week's ChatGPT usage left, from the sidebar banner; None when it isn't shown."""
    r = Browser(_account(account), runner=runner).call("usage", timeout=45, retry=True)
    return parse_usage(r.get("usage"))


def close_tab(account: str | None = None, *, runner: Runner | None = None) -> bool:
    """Close the review tab johto opened (never another tab) and forget it."""
    acct = _account(account)
    if not tab_id(acct):
        return False
    with lock("close the review tab", wait=0):
        r = Browser(acct, runner=runner).call("close", timeout=20)
    save_tab_id(acct, None)
    return bool(r.get("closed"))


# --- Fake browser ----------------------------------------------------------------------------------

class FakeBrowser:
    """Stands in for browser-harness: answers each step script like a working ChatGPT tab.

    Used in fake mode and by tests. `results[step]` overrides a step's answer (a dict, or a function of
    the step's args); `calls` records (step, args) in order. It really evaluates the pack chunks, so the
    load step's length and checksum come from what the page would hold. Chats sent are kept in
    state_file (fake mode) so a later fetch from another process finds them."""

    def __init__(self, state_file: Path | None = None, reply: str | None = None, think_s: float | None = None):
        self.state_file = state_file
        self.reply = reply
        self.think_s = float(os.environ.get("JOHTO_FAKE_GPT_THINK_S") or 0) if think_s is None else think_s
        self.results: dict = {}
        self.calls: list[tuple[str, dict]] = []
        self.url = NEW_CHAT
        self.pack = ""
        self.title = ""

    def __call__(self, script: str, timeout: float) -> str:
        step, args = parse_script(script)
        self.calls.append((step, args))
        if step in self.results:
            r = self.results[step]
            r = r(args) if callable(r) else dict(r)
        else:
            r = getattr(self, "_" + step)(args)
        if "ok" not in r:
            r = {"ok": True, "target_id": args.get("target_id") or "FAKE-TAB", "ours": True,
                 "created": not args.get("target_id"), **r}
        return "harness noise\n" + MARK + json.dumps(r) + "\n"

    def steps(self) -> list[str]:
        return [s for s, _ in self.calls]

    def _state(self) -> dict:
        return _read_json(self.state_file) if self.state_file else getattr(self, "_mem", {})

    def _save(self, data: dict) -> None:
        if self.state_file:
            _write_json(self.state_file, data)
        else:
            self._mem = data

    def _open(self, a):
        self.url = a["url"]
        return {"url": self.url, "usage": "42% usage remaining · Resets every week", "email": "fake@example.com"}

    def _model(self, a):
        label = "Pro" if a["target"] == PRO else "Instant"
        return {"value": a["target"], "max": 4, "valuetext": "", "label": label,
                "button": "Instant" if a.get("escape") else "Thinking effort", "picker_open": not a.get("escape")}

    def _title(self, a):
        self.title = a["title"]
        if not conversation_id(self.url):
            self.url = f"https://chatgpt.com/c/fake-{uuid.uuid4().hex[:12]}"
        return {"url": self.url, "edits": 1}

    def _edit(self, a):
        return {"matched": True, "chars": len(title_message(a["title"]))}

    def _load(self, a):
        parts = [json.loads(e[len("window.__johtoPack.push("):-1]) for e in a["exprs"]]
        self.pack = "".join(parts)
        return {"length": js_len(self.pack), "chunks": len(parts),
                "sha256": hashlib.sha256(self.pack.encode("utf-8")).hexdigest()}

    def _fill(self, a):
        n = len(normalize(self.pack))
        return {"edit_open": True, "match": True, "pack_len": n, "edit_len": n, "diff_at": -1,
                "pack_near": "", "edit_near": "", "button": "Pro", "picker_open": False, "attachments": 0,
                "cleared_by": "keys", "paste": {"handled": True, "how": "paste"}}

    def _submit(self, a):
        data = self._state()
        data.setdefault("chats", {})[conversation_id(self.url)] = {
            "title": self.title, "chars": len(self.pack), "sent_at": time.time(),
            "norm_chars": norm_len(self.pack), "norm_sha": text_sha(self.pack), "dense_sha": dense_sha(self.pack),
            "head": normalize(self.pack)[:200], "tail": normalize(self.pack)[-200:]}
        self._save(data)
        return {"started": True, "signal": "stop", "url": self.url}

    def _rename(self, a):
        data = self._state()
        chat = (data.get("chats") or {}).get(a["conversation_id"])
        if not chat:
            return {"http": 404, "success": False}
        chat["title"] = a["title"]
        self._save(data)
        return {"http": 200, "success": True}

    def _fetch(self, a):
        chat = (self._state().get("chats") or {}).get(a["conversation_id"])
        if not chat:
            return {"http": 404}
        user = {"id": "fake-user", "create_time": chat["sent_at"], "chars": chat["chars"],
                "norm_chars": chat.get("norm_chars"), "norm_sha": chat.get("norm_sha"),
                "dense_sha": chat.get("dense_sha"), "head": chat.get("head"), "tail": chat.get("tail")}
        if time.time() - chat["sent_at"] < self.think_s:
            return {"http": 200, "title": chat["title"], "user": user, "after": []}
        text = self.reply or (f"Fake GPT Pro review for {chat['title']}.\n\n"
                              f"Received {chat['chars']:,} characters. Nothing was graded.")
        msg = {"id": "fake-reply-" + a["conversation_id"], "author": {"role": "assistant"}, "recipient": "all",
               "content": {"content_type": "text", "parts": [text]}, "status": "finished_successfully",
               "end_turn": True, "channel": "final", "metadata": {}}
        return {"http": 200, "title": chat["title"], "user": user, "after": [msg]}

    def _fetch_dom(self, a):
        return {"stop": False, "found": False, "markdown": None, "message_id": None}

    def _usage(self, a):
        return {"usage": "42% usage remaining · Resets every week"}

    def _close(self, a):
        return {"closed": bool(a.get("target_id"))}


# --- Command line ----------------------------------------------------------------------------------

def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m johto.chatgpt",
                                description="Drive GPT Pro reviews in Austin's Chrome (see the module docstring).")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run-send", metavar="FILE", help="every step except the final Send; the edit box stays filled")
    g.add_argument("--send", metavar="FILE", help="send FILE to GPT Pro")
    g.add_argument("--fetch", metavar="CHAT_URL", help="status and markdown of the newest reply")
    g.add_argument("--inspect", metavar="CHAT_URL", help="length and fingerprint of the chat's newest message")
    g.add_argument("--usage", action="store_true", help="percent of this week's usage left")
    g.add_argument("--close-tab", action="store_true", help="close the review tab johto opened")
    g.add_argument("--show-script", metavar="STEP", choices=sorted(STEPS), help="print one step's script")
    p.add_argument("--title", help="chat title (with --send / --dry-run-send)")
    p.add_argument("--chat", help="follow up in this chat instead of a new one")
    p.add_argument("--account", help="Chrome profile account (default: settings chatgpt_account)")
    p.add_argument("--out", help="with --fetch: write the markdown to this file")
    a = p.parse_args(argv)
    try:
        if a.show_script:
            print(step_source(a.show_script, account=_account(a.account), target_id=tab_id(_account(a.account)),
                               timeout=60, url=NEW_CHAT, conversation_id=None, target=PRO, escape=False,
                               message=title_message("T"), title="T", exprs=[], js_len=0))
            return
        if a.dry_run_send or a.send:
            if not a.title:
                p.error("--title is required with --send / --dry-run-send")
            text = Path(a.dry_run_send or a.send).read_text(encoding="utf-8")
            res = send(text, a.title, chat_url=a.chat, account=a.account, no_send=bool(a.dry_run_send))
            print(json.dumps(res, indent=2, ensure_ascii=False))
        elif a.fetch:
            res = fetch(a.fetch, account=a.account)
            md = res.get("markdown") or ""
            print(json.dumps({k: v for k, v in res.items() if k != "markdown"} | {"chars": len(md)}, indent=2, ensure_ascii=False))
            if a.out and md:
                Path(a.out).write_text(md + "\n", encoding="utf-8")
                print(f"Wrote {a.out}")
            elif md:
                print("\n" + md)
        elif a.inspect:
            print(json.dumps(inspect(a.inspect, account=a.account), indent=2, ensure_ascii=False))
        elif a.usage:
            pct = usage(a.account)
            print("No usage banner shown." if pct is None else f"{pct}% of this week's usage left.")
        elif a.close_tab:
            print("Closed the review tab." if close_tab(a.account) else "No review tab to close.")
    except ChatGPTError as e:
        if e.chat_url:
            raise SystemExit(f"{e}\nChat: {e.chat_url}")
        raise SystemExit(str(e))


if __name__ == "__main__":
    main(sys.argv[1:])
