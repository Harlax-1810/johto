"""Plain-words findings and the code-token filter every screen's API uses (redesign spec 7.5).

Two jobs:

- hide_code(text): strips machine detail (backticked code, paths, path:line, URLs, issue and PR numbers, commit
  hashes, snake_case and camelCase identifiers, flags) from a string that is shown on a first-layer screen, and
  returns "" when too little is left to read. The UI never does its own regex for this.
- problems(text): the never-list of spec 6.5, shared by the repo brief validator (johto/dossier/brief.py) and the
  plain findings here. Each problem is a short sentence for a repair prompt or a log.

Plain findings: tasks/<t>/plain.json holds one plain sentence per grading row, written by a cheap headless Claude
call (sonnet, low effort, no tools) from the row's finding text. A row whose finding text changed since (its sha
differs) is stale and is written again. The rows are AI text, so the overlap guard reads them
(johto/web/guard/ledger.py, kind "grading"). Nothing here writes Austin's fields.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from . import config

SCHEMA = "johto.plain/1"
FILE = "plain.json"
MODEL = "sonnet"
EFFORT = "low"
MAX_WORDS = 30
TIMEOUT_S = 8 * 60
BEGIN, END = "<<<PLAIN", "PLAIN>>>"
BATCH = 40   # rows per call

# --- hide_code ------------------------------------------------------------------------------------------

# Product names with an inner capital that read as camelCase to a regex.
CAMEL_OK = {"iOS", "iPadOS", "macOS", "watchOS", "tvOS", "visionOS", "iPhone", "iPad", "iPod", "iCloud", "iTunes",
            "eBay", "gRPC", "jQuery", "pH", "mTLS", "eBPF", "iMessage", "iCal", "macOSX"}
# File extensions that make a word.ext token a file name (Node.js and friends are products, not files).
EXTS = ("py|pyi|js|mjs|cjs|ts|tsx|jsx|go|rs|rb|php|java|kt|kts|cs|c|h|cc|cpp|hpp|swift|dart|ex|exs|erl|scala|clj|lua|"
        "pl|r|sql|yml|yaml|json|jsonl|toml|ini|cfg|conf|env|lock|md|rst|txt|html|htm|css|scss|sass|less|vue|svelte|"
        "xml|sh|bash|zsh|ps1|bat|rego|proto|graphql|gql|tf|hcl|gradle|mk|cmake|dockerfile|tmpl|tpl|j2|ejs|hbs|"
        "twig|blade|ipynb|csv|tsv|log|pem|crt|key|db|sqlite|wasm|so|dll|exe|bin|tar|gz|zip|patch|diff")
PRODUCT_JS = re.compile(r"^[A-Z][A-Za-z0-9]*\.js$")   # Node.js, Vue.js, D3.js
# Products named after their domain: Customer.io, Monday.com, Notion.so, Socket.IO, Cal.com, Read.ai.
PRODUCT_DOMAIN = re.compile(r"^[A-Z][A-Za-z0-9-]*\.(?:io|IO|com|ai|so|app|dev|co|net|org|me|ly|sh|fm|tv|xyz|"
                            r"cloud|im|to|gg|run|page|chat|email)$")
FILE_NAMES = {"dockerfile", "makefile", "readme", "license", "changelog", "procfile", "gemfile", "rakefile",
              "jenkinsfile", "vagrantfile", "justfile", "taskfile", "claude.md", "agents.md"}

_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_TICKS = re.compile(r"`[^`\n]*`")
_URL = re.compile(r"\bhttps?://\S+|\bwww\.\S+", re.I)
_PATH_LINE = re.compile(r"(?<![\w/])[\w.~/-]*\w\.\w{1,8}:\d+(?:-\d+)?\b|(?<![\w/])[\w.~-]+(?:/[\w.-]+)+:\d+(?:-\d+)?\b")
_ISSUE = re.compile(r"(?:\b(?:issues?|PRs?|pull requests?|tickets?|bugs?|GH)\s*[-#]?\s*)?#\d+\b|"
                    r"\b(?:issue|PR|pull request|ticket)\s+\d+\b", re.I)
_HEX = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
_FLAG = re.compile(r"(?<![\w-])--?[A-Za-z][\w-]*(?:=\S+)?")
_SCOPE = re.compile(r"\b[\w.]+::[\w:]+(?:\(\))?s?|\b[\w.]+\(\)s?")
_CALL = re.compile(r"\b([A-Za-z_][\w.]*)\(([^()\n]{0,60})\)")
_OPP_ID = re.compile(r"\b[or]\d{1,2}\b")
_SNAKE = re.compile(r"\b[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+\b|\b_+[A-Za-z0-9]\w*\b")
_CAMEL = re.compile(r"\b[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*\b")
_DOTTED = re.compile(r"\b[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+\b")
_PREP = r"(?:\b(?:in|at|from|inside|via|under|see|on|of|with|by|near|into|through|commit|file|function|method|class|module|line|lines)\s+)?"
_LINE = re.compile(r"\b(?:on |at )?(?:line|lines|L)\s?\d+(?:\s?[-–]\s?\d+)?\b", re.I)


def _is_path(tok: str) -> bool:
    """A token that names a file or a path (not a product such as Node.js, not 'read/write')."""
    t = tok.strip(".,;:!?)(\"'[]{}")
    if not t:
        return False
    low = t.lower()
    if low in FILE_NAMES or low.startswith(("./", "../", "~/", "/")) and len(t) > 1:
        return True
    if "/" in t:
        parts = [p for p in t.split("/") if p]
        if len(parts) >= 3 or len(parts) == 2 and not all(re.fullmatch(r"[A-Za-z]+", p) for p in parts):
            return True
        if any(re.search(rf"\.(?:{EXTS})$", p, re.I) for p in parts):
            return True
        return False
    if PRODUCT_JS.match(t):
        return False
    return bool(re.fullmatch(rf"[\w.-]*\w\.(?:{EXTS})", t, re.I)) or bool(re.fullmatch(r"\.[\w.-]+", t))


def _dotted_code(tok: str) -> bool:
    """Class.method, module.attr, settings.FOO: dotted names that aren't sentence punctuation or a product."""
    if PRODUCT_JS.match(tok) or PRODUCT_DOMAIN.match(tok) or tok.count(".") == 0:
        return False
    parts = tok.split(".")
    if any(not p for p in parts):
        return False
    if all(len(p) == 1 for p in parts):          # e.g, i.e (abbreviations)
        return False
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return False                              # a decimal number: 3.141, 1.250
    if re.fullmatch(r"(?:[A-Z][a-z]*\.)+[A-Z][a-z]*", tok) and len(parts) <= 2 and all(len(p) <= 3 for p in parts):
        return False                              # "U.S", "Ph.D"
    return True


def _code_name(name: str) -> bool:
    return bool("_" in name or "." in name and not PRODUCT_JS.match(name) or _CAMEL.fullmatch(name)
                and name not in CAMEL_OK)


# A sentence that tells someone to run something ("All testable with go test") is a command, never first-layer text.
_COMMAND = re.compile(r"\b(?:go (?:test|build|run|vet)|npm|pnpm|yarn|npx|pytest|cargo|make|pip3?|uv run|"
                      r"docker[- ]compose|docker (?:run|build)|mvn|gradlew?|bundle exec|rake|composer|php artisan|"
                      r"manage\.py|tox|nox|jest|vitest|mix test|dotnet (?:test|build))\b", re.I)


def drop_commands(text) -> str:
    """The text without the sentences that name a command to run."""
    parts = re.split(r"(?<=[.!?])\s+", str(text or "").strip())
    return " ".join(x for x in parts if x and not _COMMAND.search(x)).strip()


def plain_title(title) -> str:
    """A short title (an upstream thread's, say) without numbers or code, else a plain stand-in."""
    t = re.sub(r"\s*\(?#\d+\)?|\b\d{3,}\b", " ", str(title or ""))
    t = re.sub(r"\s{2,}", " ", t).strip(" :-")
    h = hide_code(t, strict=True)
    if h and not re.search(r"[`()\[\]{}<>'\"=_]|\w\.\w|\b[a-z]+[A-Z]\w*|\b[A-Z][a-z]+[A-Z]\w*", h):
        return h
    return t if t and len(t.split()) >= 2 and not re.search(r"[`()\[\]{}<>'\"=_]|\w\.\w|[a-z][A-Z]", t) \
        else "A related upstream thread"


# Strict mode (a finding shown in the review's own words, a title): more shapes count as code, and a sentence that
# lost any token is dropped whole, because a removal mid-sentence can change what it says ("A persisted keeps").
PASCAL_OK = {"GitHub", "GitLab", "JavaScript", "TypeScript", "PostgreSQL", "MySQL", "MongoDB", "MariaDB", "WordPress",
             "YouTube", "LinkedIn", "PayPal", "OpenAPI", "OAuth", "Shelfnote", "SendGrid", "MailChimp", "HubSpot",
             "WebSocket", "WebSockets", "OpenID", "ClickHouse", "RabbitMQ", "DigitalOcean", "CloudFlare", "SoundCloud",
             "MusicBrainz", "ListenBrainz", "LastFm", "DokuWiki", "MediaWiki", "NextCloud", "OwnCloud", "ActiveCampaign",
             "ConvertKit", "MailerLite", "GraphQL", "SQLite", "DevOps", "AutoQC", "PowerPoint", "OneDrive", "SharePoint",
             "FastAPI", "NodeJS", "VSCode", "JetBrains", "IntelliJ", "WebRTC", "WebAuthn", "TikTok", "WhatsApp",
             "FaceTime", "AirPlay", "ChromeOS", "OpenAI", "DeepSeek", "HashiCorp", "TeamCity", "BitBucket", "Bitbucket",
             "AppImage", "FreeBSD", "OpenBSD", "NetBSD", "ArchLinux", "OpenSSL", "LibreOffice", "OpenOffice", "PyPI",
             "NuGet", "CocoaPods", "DynamoDB", "BigQuery", "CockroachDB", "InfluxDB", "TimescaleDB", "MinIO",
             "ElasticSearch", "OpenSearch", "SonarQube", "KeyCloak", "AudioBookshelf", "PhotoPrism", "ImageMagick"}
SLASH_OK = {"and/or", "read/write", "client/server", "input/output", "yes/no", "on/off", "either/or", "he/she",
            "his/her", "true/false", "start/stop", "open/close", "import/export", "upload/download", "frontend/backend",
            "front/back", "get/set", "add/remove", "create/delete", "show/hide", "enable/disable", "save/load",
            "push/pull", "login/logout", "sign-in/sign-up", "lock/unlock", "allow/deny", "pass/fail", "before/after",
            "in/out", "up/down", "left/right", "read/unread", "public/private", "desktop/mobile", "web/mobile"}
_PASCAL = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b")
_GLOB = re.compile(r"(?<!\S)[\w./-]*\w[\w./-]*\*[\w./*-]*|(?<!\S)\*[\w./-]+")
_ASSIGN = re.compile(r"(?<!\S)\S*\w?=\S*")
_SLASH = re.compile(r"(?<![\w/])[a-z][\w-]*(?:/[a-z][\w.-]*)+(?![\w/])")


def _strict_gaps(s: str, gap: str) -> str:
    s = _GLOB.sub(gap, s)
    s = _ASSIGN.sub(gap, s)
    s = _SLASH.sub(lambda m: m.group(0) if m.group(0).lower() in SLASH_OK else gap, s)
    s = _PASCAL.sub(lambda m: m.group(0) if m.group(0) in PASCAL_OK or m.group(0) in CAMEL_OK else gap, s)
    return s


def hide_code(text: str | None, min_words: int = 5, strict: bool = False) -> str:
    """The text with command sentences and code tokens removed and the leftover punctuation tidied; "" when fewer
    than `min_words` words survive (the UI then shows its own fallback line). With `strict`, globs, assignments,
    extension-less paths and PascalCase names count as code too, and a sentence that lost any token is dropped
    whole, so what is left says only what the writer said."""
    if not text:
        return ""
    text = drop_commands(text)
    if strict:
        kept = []
        for sent in _sentences(str(text).replace("\x00", " ")):
            marked = _mark_code(_strict_gaps(sent, " \x00 "), " \x00 ")
            if "\x00" not in marked:
                kept.append(sent.strip())
        out = _tidy(re.sub(r"[ \t]+", " ", " ".join(kept)))
        return out if _word_count(out) >= min_words else ""
    gap = " \x00 "   # marks a removed token until the tidy-up below
    s = _mark_code(str(text).replace("\x00", " "), gap)
    return _finish(s, min_words)


def _mark_code(s: str, gap: str) -> str:
    """The code tokens in s replaced by the gap marker."""
    s = _FENCE.sub("\n", s)
    s = re.sub(_PREP + r"`[^`\n]*`", gap, s, flags=re.I)
    s = _TICKS.sub(gap, s)
    s = re.sub(_PREP + _URL.pattern, gap, s, flags=re.I)
    s = re.sub(_PREP + r"(?:" + _PATH_LINE.pattern + r")", gap, s, flags=re.I)
    s = _LINE.sub(gap, s)
    s = _ISSUE.sub(gap, s)
    s = re.sub(_PREP + _HEX.pattern, gap, s, flags=re.I)
    s = _SCOPE.sub(gap, s)
    s = _CALL.sub(lambda m: gap if _code_name(m.group(1)) else m.group(0), s)
    s = re.sub(_PREP + _FLAG.pattern, gap, s, flags=re.I)
    s = _OPP_ID.sub(gap, s)

    out = []
    for raw in re.split(r"(\s+)", s):
        if not raw or raw.isspace() or raw == "\x00":
            out.append(raw)
            continue
        core = raw.strip(".,;:!?)(\"'[]{}")
        if core and (_is_path(core) or _dotted_code(core)):
            i = raw.find(core)
            trail = raw[i + len(core):].lstrip(")]}\"'")
            out.append(gap + trail)
            continue
        raw = _SNAKE.sub(gap, raw)
        raw = _CAMEL.sub(lambda m: m.group(0) if m.group(0) in CAMEL_OK else gap, raw)
        out.append(raw)
    return "".join(out)


def _finish(s: str, min_words: int) -> str:
    """Tidy a gap-marked string: gaps, the prepositions that went with them, stray punctuation."""
    s = re.sub(r"[ \t]+", " ", s)
    # neighbouring gaps joined by "and", "or" or commas are one gap; a preposition before a gap goes with it
    s = re.sub(r"\x00(?:\s*(?:,|;|and|or|&|/|plus)\s*\x00)+", "\x00", s, flags=re.I)
    s = re.sub(r"\b(?:in|at|from|inside|via|under|see|on|of|with|by|near|into|through)\s*\x00", "\x00", s,
               flags=re.I)
    s = s.replace("\x00", " ")
    s = re.sub(r"[ \t]+", " ", s)
    for _ in range(2):
        s = re.sub(r"\b(?:in|at|from|inside|via|under|see|on|of|with|by|near|into|through)\s*(?=[.,;:)]|$|"
                   r"\b(?:and|or|but)\b)", "", s, flags=re.I)
        s = re.sub(r"(?:,\s*|\s)\b(?:and|or|but|plus)\s*(?=[.,;:)]|$)", "", s, flags=re.I)
    s = _tidy(s)
    if _word_count(s) < min_words:
        return ""
    return s


def _tidy(s: str) -> str:
    s = re.sub(r"\(\s*[,;:.]*\s*\)|\[\s*[,;:.]*\s*\]", " ", s)    # empty brackets
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\s+([.,;:!?)])", r"\1", s)                         # space before punctuation
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", s)                   # ", ," -> ","
    s = re.sub(r"[,;:]+\s*([.!?])", r"\1", s)                       # ", ." -> "."
    s = re.sub(r"(?:(?<=^)|(?<=[.!?]\s))[,;:.]\s*", "", s)          # sentence starting with punctuation
    s = re.sub(r"\.{2,}", ".", s)
    s = re.sub(r"\s*\n\s*", "\n", s).strip(" \t\n,;:")
    s = re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), s)
    return s.strip()


def _word_count(s: str) -> int:
    return sum(1 for w in s.split() if re.search(r"[A-Za-z]", w))


def words(s: str | None) -> int:
    """Whitespace-separated tokens (the brief schema's x-maxWords counts these)."""
    return len((s or "").split())


# --- the never-list (spec 6.5) --------------------------------------------------------------------------

DASHES = re.compile(r"[\u2014\u2013]")
IMPERATIVE = re.compile(r"^\W*(?:Add|Make|Implement|Fix|Build|Write|Create|Refactor|Ensure|Try|Please|Update|"
                        r"Change|Remove|Replace|Rewrite|Use)\b|^\W*(?:I want|I need|Can you|Could you|We need|"
                        r"We want|You should|You need|Let's)\b")
YOU = re.compile(r"\b(?:you|your|yours|you're|you'll|yourself)\b", re.I)
MARKETING = re.compile(r"\b(?:robust|seamless(?:ly)?|powerful|cutting[- ]edge|comprehensive|leverag(?:e|es|ed|ing)|"
                       r"world[- ]class|best[- ]in[- ]class|state[- ]of[- ]the[- ]art|revolutionary|game[- ]chang\w*|"
                       r"blazing(?:ly)?|next[- ]generation|supercharge\w*)\b", re.I)
COMPARE = re.compile(r"(?<![\w-])(?:better|worse)(?![\w-])|\bshould\b", re.I)
STORY = re.compile(r"\bas an? (?:user|admin|administrator|operator|developer|customer)\s*,|\bacceptance criteria\b|"
                   r"\buser stor(?:y|ies)\b|\bthe goal is\b|\bdefinition of done\b", re.I)
MODEL_NAMES = re.compile(r"\b(?:opus|sonnet|haiku|claude[- ](?:opus|sonnet|haiku|\d)[\w.-]*|gpt[- ]?\d[\w.-]*|"
                         r"gpt[- ]?pro|chatgpt|gemini|codex|llama\s?\d)\b|\b(?:high|medium|low|max)\s+effort\b", re.I)
COST = re.compile(r"\$\s?\d|\b\d+(?:\.\d+)?\s?USD\b", re.I)
COMMANDS = re.compile(
    r"\b(?:npm (?:i|install|run|test|ci|start)|npx|yarn (?:add|install|test|run)|pnpm(?: \w+)?|pip3? install|"
    r"python3? -m|pytest|go (?:test|run|build|mod|get)|cargo (?:test|run|build)|(?-i:docker[- ]compose)|"
    r"docker (?:run|pull|exec|build|push)|php artisan|composer (?:install|require)|bundle exec|rake \w+|"
    r"(?-i:gradle\w*)|mvn \w+|kubectl|helm (?:install|upgrade)|git (?:clone|checkout|log|show|diff|pull|push|commit)|"
    r"curl |wget |sudo |apt(?:-get)? install|brew install|make (?:test|build|install|dev|run)|manage\.py)\b", re.I)
VERSION = re.compile(r"\bv?\d+\.\d+\.\d+(?:[-.][\w.]+)?\b|\bv\d+\.\d+\b|\w@\d|==\d")
EXIT = re.compile(r"\bexit(?:ed)?(?: with)?(?: code| status)?\s+\d+\b|\bexit (?:code|status)\b", re.I)
LINE_NO = re.compile(r"\blines?\s+\d+|\bL\d{2,}\b|[A-Za-z_]:\d+\b")   # not times or ratios (10:30, 16:9)
QUESTION = re.compile(r"\?")


def _first(rx: re.Pattern, s: str) -> str | None:
    m = rx.search(s)
    return m.group(0).strip() if m else None


def _sentences(s: str) -> list[str]:
    return [x for x in re.split(r"(?<=[.!?])\s+|\n+", s) if x.strip()]


def problems(text: str | None, *, questions: bool = False) -> list[str]:
    """What in this model-written string breaks the never-list (spec 6.5), one short sentence each.
    `questions` allows a question mark (places' open questions, a seed's check-by-hand list)."""
    if not text:
        return []
    s = str(text)
    out: list[str] = []
    if "`" in s:
        out.append("has backticks")
    if DASHES.search(s):
        out.append("has an em or en dash")
    toks = [t.strip(".,;:!?)(\"'[]{}") for t in s.split()]
    paths = [t for t in toks if t and _is_path(t)]
    if paths:
        out.append(f"names a file or path ({paths[0]})")
    if _URL.search(s):
        out.append(f"has a URL ({_first(_URL, s)})")
    if LINE_NO.search(s):
        out.append(f"has a line number ({_first(LINE_NO, s)})")
    ids = [t for t in toks if t and t not in paths and (_SNAKE.fullmatch(t) or (_CAMEL.fullmatch(t) and t not in
                                                                                     CAMEL_OK)
                                                        or "::" in t or t.endswith("()") or _dotted_code(t))]
    if ids:
        out.append(f"has a code identifier ({ids[0]})")
    if COMMANDS.search(s):
        out.append(f"has a command ({_first(COMMANDS, s)})")
    if _FLAG.search(s):
        out.append(f"has a command-line flag ({_first(_FLAG, s)})")
    if VERSION.search(s):
        out.append(f"has a version number ({_first(VERSION, s)})")
    if _HEX.search(s):
        out.append(f"has a commit hash ({_first(_HEX, s)})")
    if _ISSUE.search(s):
        out.append(f"has an issue or PR number ({_first(_ISSUE, s)})")
    if EXIT.search(s):
        out.append("mentions an exit code")
    if MODEL_NAMES.search(s):
        out.append(f"names a model or effort ({_first(MODEL_NAMES, s)})")
    if COST.search(s):
        out.append("mentions a cost")
    for sent in _sentences(s):
        m = IMPERATIVE.match(sent)
        if m:
            out.append(f"reads as an instruction (starts with {m.group(0).strip()!r})")
            break
    if YOU.search(s):
        out.append(f"addresses the reader ({_first(YOU, s)!r})")
    if STORY.search(s):
        out.append(f"reads as a goal or user story ({_first(STORY, s)!r})")
    if COMPARE.search(s):
        out.append(f"makes a better-or-worse judgement ({_first(COMPARE, s)!r})")
    if MARKETING.search(s):
        out.append(f"uses a marketing word ({_first(MARKETING, s)!r})")
    if not questions and QUESTION.search(s):
        out.append("is a question (questions belong only in the open questions)")
    return out


# --- plain findings: storage ----------------------------------------------------------------------------

def sha(finding: str | None) -> str:
    return hashlib.sha256((finding or "").encode()).hexdigest()[:16]


def path(task_dir: Path) -> Path:
    return Path(task_dir) / FILE


def _read(task_dir: Path) -> dict:
    try:
        data = json.loads(path(task_dir).read_text())
    except (OSError, ValueError):
        return {"schema": SCHEMA, "rows": {}}
    if not isinstance(data, dict) or not isinstance(data.get("rows"), dict):
        return {"schema": SCHEMA, "rows": {}}
    return data


def _grading_rows(task_dir: Path) -> list[dict]:
    try:
        data = json.loads((Path(task_dir) / "grading.json").read_text())
    except (OSError, ValueError):
        return []
    rows = data.get("rows") if isinstance(data, dict) else data
    return [r for r in rows or [] if isinstance(r, dict) and r.get("id") and (r.get("finding") or "").strip()]


def _active(r: dict) -> bool:
    return r.get("review_status") not in ("withdrawn", "superseded") and not r.get("other_run")


def load(task_dir: Path) -> dict[str, str]:
    """{row id: plain sentence} for rows whose finding text is unchanged since the sentence was written."""
    stored = _read(task_dir)["rows"]
    out = {}
    for r in _grading_rows(task_dir):
        rec = stored.get(r["id"])
        if isinstance(rec, dict) and rec.get("plain") and rec.get("sha") == sha(r.get("finding")):
            out[r["id"]] = rec["plain"]
    return out


def stored_texts(task_dir: Path) -> dict[str, str]:
    """Every plain sentence in the file, fresh or stale: what the overlap guard must know was shown."""
    return {k: v["plain"] for k, v in _read(task_dir)["rows"].items() if isinstance(v, dict) and v.get("plain")}


def missing(task_dir: Path) -> list[str]:
    """Active grading rows with no fresh plain sentence, in table order."""
    fresh = load(task_dir)
    return [r["id"] for r in _grading_rows(task_dir) if _active(r) and r["id"] not in fresh]


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_LOCK = threading.Lock()


def _lock(task_dir: Path) -> threading.Lock:
    with _LOCKS_LOCK:
        return _LOCKS.setdefault(str(Path(task_dir).resolve()), threading.Lock())


def _write_rows(task_dir: Path, rows: dict[str, dict]) -> None:
    """Merge rows into plain.json (atomic)."""
    p = path(task_dir)
    data = _read(task_dir)
    data["schema"] = SCHEMA
    data["rows"].update(rows)
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    os.replace(tmp, p)


# --- plain findings: generation -------------------------------------------------------------------------

PROMPT = """You rewrite findings from a code review into plain product words for a reader who never reads code.

Each finding below is about what Claude (an AI coding model) did in one session on a software project. For each
one, write one plain sentence of at most {max_words} words that says the same thing in the words a user of the
product would use: what the product does, what a user or operator sees, what Claude got right or missed.

Rules:
- Keep the finding's meaning exactly. Do not soften it, do not add a claim, do not add advice or a fix.
- Third person, present or past tense. Short and concrete.
- Never write: file names or paths, line numbers, code identifiers (snake_case, camelCase, Class.method, ::, ()),
  backticks, commands, flags, version numbers, commit hashes, issue or PR numbers (#123), exit codes, model
  names, effort or cost, em or en dashes, questions, the words "you" or "your", "should", "better" or "worse",
  marketing words, or a sentence that starts with a verb telling someone what to do.
- If a finding cannot be said without code, describe the behaviour the code causes instead.

Project: {project}

Findings (JSON, id -> finding text):
{findings}

Answer with only a JSON object mapping each id to its plain sentence, between a line {begin} and a line {end}:
{begin}
{{"R1": "...", "R2": "..."}}
{end}
"""


def build_prompt(rows: list[dict], project: str) -> str:
    findings = json.dumps({r["id"]: r["finding"] for r in rows}, indent=1, ensure_ascii=False)
    return PROMPT.format(max_words=MAX_WORDS, project=project or "a software project", findings=findings,
                         begin=BEGIN, end=END)


def check_row(text) -> list[str]:
    """Why a plain sentence can't be shown (empty list when it can)."""
    if not isinstance(text, str) or not text.strip():
        return ["is empty"]
    errs = []
    if words(text) > MAX_WORDS:
        errs.append(f"has {words(text)} words, limit {MAX_WORDS}")
    return errs + problems(text)


def parse(text: str) -> dict | None:
    if not text:
        return None
    i = text.rfind(BEGIN)
    body = text[i + len(BEGIN):] if i >= 0 else text
    j = body.find(END)
    body = (body[:j] if j >= 0 else body).strip()
    body = re.sub(r"^```(?:json)?\s*\n|\n```\s*$", "", body).strip()
    try:
        doc = json.loads(body)
    except ValueError:
        m = re.search(r"\{.*\}", body, re.S)
        if not m:
            return None
        try:
            doc = json.loads(m.group(0))
        except ValueError:
            return None
    return doc if isinstance(doc, dict) else None


def _claude_bin() -> str:
    return str(config.CLAUDE_BIN)


def _run_claude(prompt: str, timeout: int = TIMEOUT_S) -> dict:
    """One headless call: {"text", "cost_usd", "error"}. Sonnet, low effort, no tools, the subscription's
    login (the dossier generator's environment scrub), no session kept."""
    from .dossier import generate as dgen
    cmd = [_claude_bin(), "-p", "--model", MODEL, "--effort", EFFORT, "--output-format", "json", "--tools", "",
           "--strict-mcp-config", "--setting-sources", "project", "--no-session-persistence",
           "--disable-slash-commands", "--no-chrome"]
    with tempfile.TemporaryDirectory(prefix="johto-plain-") as tmp:
        try:
            r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, timeout=timeout, input=prompt,
                               env=dgen._env())
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"text": "", "cost_usd": None, "error": f"Claude didn't run: {e}"}
    try:
        out = json.loads(r.stdout)
    except ValueError:
        out = {"result": r.stdout}
    err = None if r.returncode == 0 and not out.get("is_error") else (r.stderr or r.stdout or "")[-600:]
    return {"text": out.get("result") or "", "cost_usd": out.get("total_cost_usd"), "error": err}


Runner = Callable[[str], dict]


def available() -> bool:
    """Whether a generation may run: never in fake mode unless a stub claude is configured."""
    return not config.FAKE or config.CLAUDE_BIN_SET


def _project(task_dir: Path) -> str:
    try:
        meta = json.loads((Path(task_dir) / "task.json").read_text())
    except (OSError, ValueError):
        return ""
    repo = (meta.get("repo") or {}).get("full_name") or ""
    name = repo.split("/")[-1]
    desc = (meta.get("repo") or {}).get("description") or ""
    return f"{name}: {hide_code(desc, min_words=1)}" if desc else name


def generate(task_dir: Path, row_ids: list[str] | None = None, *, runner: Runner | None = None,
             log: Callable[[str], None] | None = None) -> dict:
    """Write plain sentences for these rows (default: the missing ones). Invalid rows are dropped (they fall
    back to the finding with code hidden). Returns {"written", "dropped", "cost_usd", "error"}."""
    task_dir = Path(task_dir)
    say = log or (lambda s: None)
    with _lock(task_dir):
        want = set(row_ids) if row_ids is not None else set(missing(task_dir))
        rows = [r for r in _grading_rows(task_dir) if r["id"] in want]
        res = {"written": [], "dropped": {}, "cost_usd": 0.0, "error": None}
        if not rows:
            return res
        project = _project(task_dir)
        for i in range(0, len(rows), BATCH):
            batch = rows[i:i + BATCH]
            say(f"Writing {len(batch)} finding{'s' if len(batch) != 1 else ''} in plain words")
            out = (runner or _run_claude)(build_prompt(batch, project))
            res["cost_usd"] = round(res["cost_usd"] + float(out.get("cost_usd") or 0), 4)
            if out.get("error"):
                res["error"] = out["error"]
                say(f"The call failed: {str(out['error'])[:200]}")
                continue
            got = parse(out.get("text") or "")
            if got is None:
                res["error"] = "the answer had no JSON between the markers"
                continue
            now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            keep = {}
            for r in batch:
                text = got.get(r["id"])
                text = " ".join(text.split()) if isinstance(text, str) else text
                errs = check_row(text)
                if errs:
                    res["dropped"][r["id"]] = errs
                    continue
                keep[r["id"]] = {"plain": text, "sha": sha(r["finding"]), "at": now}
            if keep:
                _write_rows(task_dir, keep)
                res["written"] += list(keep)
        say(f"{len(res['written'])} written, {len(res['dropped'])} dropped, ${res['cost_usd']:.3f}")
        return res


def ensure(task_dir: Path, *, runner: Runner | None = None) -> dict | None:
    """Start one background job when rows are missing; None when nothing is missing, a generation can't run
    (fake mode without a stub claude) or one is already running for this task (that job is returned)."""
    task_dir = Path(task_dir)
    if not available() and runner is None:
        return None
    if not missing(task_dir):
        return None
    from .web import jobs
    target = f"t:{task_dir.name}"
    live = jobs.active(kind="plain", target=target)
    if live:
        return live[0]

    def work(ctx):
        ctx.step("writing")
        r = generate(task_dir, runner=runner, log=ctx.log)
        ctx.add_cost(r.get("cost_usd") or 0)
        return {"written": len(r["written"]), "dropped": len(r["dropped"])}
    return jobs.submit("plain", target, work, pool="ai", priority=2, steps=["writing"], step="waiting")


# --- CLI (johto grading plain <task>) ----------------------------------------------------------------------

def cmd_plain(a) -> None:
    from . import workspace
    d = workspace.resolve(a.task)
    ids = None
    if getattr(a, "force", False):
        ids = [r["id"] for r in _grading_rows(d) if _active(r)]
    todo = ids if ids is not None else missing(d)
    if not todo:
        print("Every finding already has plain words.")
        return
    if not available():
        raise SystemExit("Headless Claude is off in fake mode (set JOHTO_CLAUDE_BIN to a stub).")
    r = generate(d, todo, log=print)
    for rid, errs in r["dropped"].items():
        print(f"  {rid} dropped: {'; '.join(errs)}")
    if r["error"]:
        raise SystemExit(f"failed: {r['error']}")
