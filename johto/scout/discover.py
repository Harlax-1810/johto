"""Find candidate repositories for an open slot group.

Two sources:
- GitHub repository search, with topic queries tuned per domain and size/star bands per
  codebase type.
- awesome-selfhosted-data, a curated list of real self-hostable applications (shallow
  clone, refreshed weekly). Good for enterprise-grade slots and many personal ones.
"""
from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
import time
from pathlib import Path

from .. import config
from . import github

PREFERRED_LANGS = {"TypeScript", "JavaScript", "Python", "Go", "Rust"}
ASH_DIR = config.DATA / "cache" / "awesome-selfhosted-data"
ASH_URL = "https://github.com/awesome-selfhosted/awesome-selfhosted-data.git"

# Topic combinations per domain. Each inner tuple is ANDed in one search query.
DOMAIN_TOPICS = {
    "Web frontend": [("react", "self-hosted"), ("vue", "self-hosted"), ("svelte",), ("pwa",),
                     ("nextjs", "app"), ("web-app",), ("sveltekit",), ("react", "web-app")],
    "Backend / API": [("self-hosted",), ("rest-api", "self-hosted"), ("fastapi",), ("django", "self-hosted"),
                      ("golang", "self-hosted"), ("rails", "self-hosted"), ("webhooks",), ("api-server",)],
    "Games": [("game",), ("roguelike",), ("godot-game",), ("pygame",), ("browser-game",),
              ("multiplayer-game",), ("game", "typescript"), ("puzzle-game",)],
    "Dev tooling": [("cli", "developer-tools"), ("tui",), ("git", "cli"), ("developer-tools",),
                    ("terminal", "cli"), ("command-line-tool",), ("devtools",)],
    "Data / DB": [("analytics", "self-hosted"), ("etl",), ("dashboard", "self-hosted"),
                  ("database-gui",), ("data-visualization", "self-hosted"), ("sqlite", "app"),
                  ("data-pipeline",)],
    "DevOps / cloud": [("homelab",), ("docker-compose", "self-hosted"), ("monitoring", "self-hosted"),
                       ("uptime-monitor",), ("ci-cd", "self-hosted"), ("deployment",), ("infrastructure-as-code",)],
    "AI agents": [("llm", "self-hosted"), ("ai-agent",), ("chatbot", "self-hosted"), ("rag",),
                  ("ai-agents",), ("llm", "agent")],
    "Other software": [("desktop-app",), ("tauri",), ("electron", "app"), ("discord-bot",),
                       ("telegram-bot",), ("home-automation",), ("self-hosted",)],
}

# awesome-selfhosted tags that suggest each domain. Any app fits Backend/Web/Other.
ASH_DOMAIN_TAGS = {
    "Games": ("Games",),
    "Dev tooling": ("Software Development",),
    "Data / DB": ("Database Management", "Analytics", "Low Code", "Personal Dashboards"),
    "DevOps / cloud": ("Monitoring", "Automation", "Continuous Integration", "Self-hosting Solutions",
                       "Backup", "Proxy", "Web Servers", "DNS", "Network Utilities", "VPN"),
    "AI agents": ("Generative Artificial Intelligence", "GenAI", "Generative AI"),
}
ASH_PLATFORM_LANG = {"Nodejs": "JavaScript", "Python": "Python", "Go": "Go", "Rust": "Rust",
                     "PHP": "PHP", "Ruby": "Ruby", "Java": "Java", ".NET": "C#", "Dart": "Dart",
                     "Kotlin": "Kotlin", "Shell": "Shell", "Haskell": "Haskell", "Elixir": "Elixir",
                     "Erlang": "Erlang", "Perl": "Perl", "C": "C", "C++": "C++", "Deno": "TypeScript",
                     "Scala": "Scala", "Clojure": "Clojure", "Lua": "Lua", "Swift": "Swift"}

LIBRARY_WORDS = re.compile(
    r"\b(library|framework|sdk|toolkit|boilerplate|starter|template|awesome|tutorial|course|"
    r"bindings|wrapper|plugin for|component library|ui kit|collection of|examples?|"
    r"cheat ?sheet|interview|leetcode|algorithms?)\b", re.I)

BANDS = {
    # (stars range, size KB max) per codebase type
    "Personal-scale": ("20..3000", 60000),
    "Enterprise-grade": (">=400", None),
}


def _pushed_after(days: int = 540) -> str:
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def _norm(item: dict, source: str) -> dict:
    lic = item.get("license") or {}
    return {
        "full_name": item["full_name"].lower(),
        "display_name": item["full_name"],
        "source": source,
        "stars": item.get("stargazers_count", 0),
        "language": item.get("language"),
        "description": (item.get("description") or "")[:300],
        "topics": item.get("topics", []),
        "pushed_at": item.get("pushed_at"),
        "created_at": item.get("created_at"),
        "size_kb": item.get("size"),
        "default_branch": item.get("default_branch"),
        "fork": item.get("fork", False),
        "archived": item.get("archived", False),
        "license": lic.get("spdx_id") if isinstance(lic, dict) else None,
    }


def _plausible(c: dict) -> bool:
    if c["fork"] or c["archived"]:
        return False
    if c["full_name"] in {r.lower() for r in config.SWE_BENCH_REPOS}:
        return False
    desc = c["description"]
    if not desc:
        return False
    if LIBRARY_WORDS.search(desc) and not re.search(r"\b(app|application|self-hosted|game)\b", desc, re.I):
        return False
    ascii_ratio = sum(ch.isascii() for ch in desc) / max(len(desc), 1)
    return ascii_ratio > 0.9


def search_github(domain: str, codebase_type: str, per_query: int = 30) -> list[dict]:
    stars, size_max = BANDS.get(codebase_type, BANDS["Personal-scale"])
    out = []
    for topics in DOMAIN_TOPICS.get(domain, DOMAIN_TOPICS["Other software"]):
        q = " ".join(f"topic:{t}" for t in topics)
        q += f" stars:{stars} pushed:>{_pushed_after()} fork:false archived:false"
        if size_max:
            q += f" size:<{size_max}"
        if codebase_type == "Enterprise-grade":
            q += " size:>8000"
        try:
            items = github.search_repos(q, per_page=per_query)
        except github.GitHubError:
            continue
        out.extend(_norm(i, f"search:{'+'.join(topics)}") for i in items)
    return out


# --- awesome-selfhosted-data -----------------------------------------------------

def _refresh_ash(max_age_days: float = 7) -> Path:
    marker = ASH_DIR / ".fetched"
    if marker.exists() and time.time() - marker.stat().st_mtime < max_age_days * 86400:
        return ASH_DIR
    if ASH_DIR.exists():
        shutil.rmtree(ASH_DIR)
    ASH_DIR.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--filter=blob:none", "--sparse",
                    ASH_URL, str(ASH_DIR)], check=True, timeout=300)
    subprocess.run(["git", "-C", str(ASH_DIR), "sparse-checkout", "set", "software"],
                   check=True, timeout=300)
    marker.touch()
    return ASH_DIR


def _parse_yml(text: str) -> dict:
    """Tiny parser for awesome-selfhosted software entries (flat keys, simple lists)."""
    out: dict = {}
    key = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" ") and ":" in line:
            key, _, val = line.partition(":")
            val = val.strip().strip("'\"")
            out[key] = val if val else []
        elif line.lstrip().startswith("- ") and key is not None and isinstance(out.get(key), list):
            out[key].append(line.strip()[2:].strip().strip("'\""))
    return out


def awesome_selfhosted() -> list[dict]:
    root = _refresh_ash() / "software"
    entries = []
    for f in sorted(root.glob("*.yml")):
        d = _parse_yml(f.read_text(errors="ignore"))
        url = d.get("source_code_url") or ""
        m = re.match(r"https://github\.com/([^/]+/[^/#?]+)", url)
        if not m or d.get("archived") == "true":
            continue
        langs = [ASH_PLATFORM_LANG[p] for p in d.get("platforms", []) if p in ASH_PLATFORM_LANG]
        try:
            stars = int(d.get("stargazers_count") or 0)
        except ValueError:
            stars = 0
        entries.append({
            "full_name": m.group(1).removesuffix(".git").lower(),
            "display_name": m.group(1).removesuffix(".git"),
            "source": "awesome-selfhosted",
            "stars": stars,
            "language": langs[0] if langs else None,
            "description": (d.get("description") or "")[:300],
            "topics": d.get("tags", []),
            "pushed_at": d.get("updated_at"),
            "size_kb": None,
            "default_branch": None,
            "fork": False,
            "archived": False,
            "license": (d.get("licenses") or [None])[0],
        })
    return entries


def _ash_fits(c: dict, domain: str, codebase_type: str) -> bool:
    tags = " ".join(c["topics"])
    want = ASH_DOMAIN_TAGS.get(domain)
    if want and not any(w.lower() in tags.lower() for w in want):
        return False
    if domain == "Web frontend" and c["language"] not in ("JavaScript", "TypeScript"):
        return False
    if codebase_type == "Enterprise-grade":
        return c["stars"] >= 400
    return 20 <= c["stars"] <= 4000


def _recent(c: dict, days: int = 540) -> bool:
    p = c.get("pushed_at") or ""
    return not p or p[:10] >= _pushed_after(days)


def score(c: dict, codebase_type: str) -> float:
    s = 0.0
    if c["language"] in PREFERRED_LANGS:
        s += 3
    elif c["language"] in config.LANGUAGES or c["language"] == "TypeScript":
        s += 1
    stars = c["stars"] or 0
    if codebase_type == "Personal-scale":
        s += 2 if 50 <= stars <= 1500 else 1 if stars >= 20 else 0
    else:
        s += 2 if 1000 <= stars <= 30000 else 1
    if c["source"] == "awesome-selfhosted":
        s += 1.5  # curated as a real application
    if any(t in ("self-hosted", "selfhosted") for t in c["topics"]):
        s += 0.5
    return s


def discover(slot: dict, limit: int = 15, use_awesome: bool = True, use_search: bool = True,
             exclude: set[str] | None = None) -> list[dict]:
    domain, ctype = slot["domain"], slot["codebase_type"]
    cands: dict[str, dict] = {}
    if use_search:
        for c in search_github(domain, ctype):
            cands.setdefault(c["full_name"], c)
    if use_awesome:
        try:
            for c in awesome_selfhosted():
                if _ash_fits(c, domain, ctype):
                    prev = cands.get(c["full_name"])
                    if prev:
                        prev["source"] += "+awesome-selfhosted"
                    else:
                        cands[c["full_name"]] = c
        except (subprocess.SubprocessError, OSError):
            pass
    exclude = exclude or set()
    ok = [c for c in cands.values() if _plausible(c) and _recent(c) and c["full_name"] not in exclude]
    for c in ok:
        c["discover_score"] = score(c, ctype)
    ok.sort(key=lambda c: (-c["discover_score"], -(c["stars"] or 0)))
    # Alternate between curated (awesome-selfhosted) and search results for variety.
    curated = [c for c in ok if "awesome-selfhosted" in c["source"]]
    searched = [c for c in ok if "awesome-selfhosted" not in c["source"]]
    mixed = []
    while curated or searched:
        if curated:
            mixed.append(curated.pop(0))
        if searched:
            mixed.append(searched.pop(0))
    return mixed[:limit]


# --- Pool discovery: large self-hostable, stateful applications -------------------------
#
# Each fragment is ANDed with one band per codebase type whose open slot count calls for it
# (see pool.grow). Fragments lean on lane-friendly languages (Go, TypeScript, JavaScript,
# Python) because the lane image lacks other toolchains.

LANE_LANGS = ("Go", "TypeScript", "JavaScript", "Python")
POOL_BANDS = {
    "Enterprise-grade": "stars:>=250 size:>4000",
    "Personal-scale": "stars:25..2500 size:150..60000",
}
POOL_QUERIES: dict[str, list[str]] = {
    "Backend / API": [
        "topic:self-hosted language:Go", "topic:self-hosted language:TypeScript",
        "topic:self-hosted language:Python", "topic:self-hosted language:JavaScript",
        "topic:selfhosted language:Go", "topic:selfhosted language:TypeScript", "topic:selfhosted language:Python",
        "topic:crm", "topic:erp", "topic:helpdesk", "topic:headless-cms", "topic:cms language:TypeScript",
        "topic:cms language:Go", "topic:ecommerce language:TypeScript", "topic:ecommerce language:Python",
        "topic:project-management language:TypeScript", "topic:wiki language:Go", "topic:forum",
        "topic:activitypub language:Go", "topic:activitypub language:TypeScript", "topic:fediverse language:Python",
        "topic:booking", "topic:invoicing", "topic:accounting language:Python", "topic:inventory-management",
        "topic:issue-tracker", "topic:email-server", "topic:chat language:Go", "topic:matrix language:Go",
        "topic:api-gateway language:Go", "topic:rss-reader", "topic:django topic:self-hosted",
        "topic:fastapi topic:self-hosted", "topic:nestjs topic:self-hosted", "topic:pocketbase",
    ],
    "Web frontend": [
        "topic:react topic:self-hosted", "topic:nextjs topic:self-hosted", "topic:vue topic:self-hosted",
        "topic:svelte topic:self-hosted", "topic:sveltekit", "topic:pwa language:TypeScript", "topic:kanban",
        "topic:note-taking language:TypeScript", "topic:whiteboard", "topic:markdown-editor language:TypeScript",
        "topic:personal-finance language:TypeScript", "topic:habit-tracker", "topic:flashcards", "topic:recipes",
        "topic:bookmarks language:TypeScript", "topic:todo-app language:TypeScript", "topic:react topic:dashboard",
        "topic:local-first", "topic:offline-first",
    ],
    "Other software": [
        "topic:home-automation language:Python", "topic:home-automation language:TypeScript",
        "topic:media-server", "topic:photo-management", "topic:music-player language:TypeScript",
        "topic:podcast language:TypeScript", "topic:ebooks", "topic:tauri language:TypeScript",
        "topic:electron topic:self-hosted", "topic:desktop-app language:TypeScript", "topic:password-manager",
        "topic:file-sharing language:Go", "topic:file-sync", "topic:caldav", "topic:webrtc language:Go",
        "topic:discord-bot language:TypeScript", "topic:telegram-bot language:Python", "topic:iot language:Go",
    ],
    "DevOps / cloud": [
        "topic:monitoring language:Go", "topic:observability language:Go", "topic:observability language:TypeScript",
        "topic:uptime-monitor", "topic:status-page", "topic:paas", "topic:gitops language:Go",
        "topic:ci-cd language:Go", "topic:homelab language:Go", "topic:homelab language:TypeScript",
        "topic:backup language:Go", "topic:reverse-proxy language:Go", "topic:dns language:Go",
        "topic:log-management", "topic:incident-management", "topic:feature-flags", "topic:secrets-management",
        "topic:docker topic:dashboard", "topic:kubernetes topic:dashboard", "topic:infrastructure language:Go",
    ],
    "Dev tooling": [
        "topic:developer-tools language:Go", "topic:developer-tools language:TypeScript",
        "topic:developer-tools language:Python", "topic:code-review", "topic:git language:Go",
        "topic:git language:TypeScript", "topic:api-client", "topic:http-client topic:app", "topic:code-search",
        "topic:snippets", "topic:load-testing language:Go", "topic:database-gui", "topic:mock-server",
        "topic:package-registry", "topic:git-server", "topic:tui language:Go", "topic:devtools language:TypeScript",
        "topic:testing-tools language:TypeScript",
    ],
    "Games": [
        "topic:game language:TypeScript", "topic:game language:JavaScript", "topic:game language:Python",
        "topic:game language:Go", "topic:multiplayer-game", "topic:browser-game", "topic:phaser", "topic:pygame",
        "topic:roguelike language:TypeScript", "topic:roguelike language:Python", "topic:chess language:TypeScript",
        "topic:card-game", "topic:board-game", "topic:game-server language:Go", "topic:ebiten", "topic:html5-game",
        "topic:io-game", "topic:mmorpg",
    ],
    "Data / DB": [
        "topic:analytics language:Go", "topic:analytics language:TypeScript", "topic:web-analytics",
        "topic:business-intelligence", "topic:data-visualization topic:self-hosted", "topic:etl language:Python",
        "topic:etl language:Go", "topic:database-management", "topic:sql-editor", "topic:time-series language:Go",
        "topic:data-catalog", "topic:dashboard topic:self-hosted", "topic:spreadsheet", "topic:low-code",
        "topic:airtable-alternative", "topic:search-engine language:Go", "topic:product-analytics",
    ],
    "AI agents": [
        "topic:llm topic:self-hosted", "topic:chatgpt topic:self-hosted", "topic:rag language:Python",
        "topic:rag language:TypeScript", "topic:ai-agents language:Python", "topic:ai-agents language:TypeScript",
        "topic:llmops", "topic:chatbot language:Python", "topic:ollama language:TypeScript",
        "topic:ollama language:Go",
    ],
}


def search_pool(domain: str, codebase_type: str, per_query: int = 50,
                pushed_days: int = 270) -> list[dict]:
    """Run the pool queries for one domain and codebase-type band (cached 3 days)."""
    band = POOL_BANDS[codebase_type]
    out = []
    for frag in POOL_QUERIES.get(domain, []):
        q = f"{frag} {band} pushed:>{_pushed_after(pushed_days)} fork:false archived:false"
        try:
            items = github.search_repos(q, per_page=per_query)
        except github.GitHubError:
            continue
        for i in items:
            c = _norm(i, f"pool:{frag}|{codebase_type}")
            c["hint"] = {"domain": domain, "codebase_type": codebase_type, "query": frag}
            out.append(c)
    return out


def ash_pool(min_stars: int = 80, pushed_days: int = 270) -> list[dict]:
    """awesome-selfhosted entries in lane-friendly languages, recently updated."""
    try:
        entries = awesome_selfhosted()
    except (subprocess.SubprocessError, OSError):
        return []
    out = []
    for c in entries:
        if c["language"] not in LANE_LANGS or (c["stars"] or 0) < min_stars or not _recent(c, pushed_days):
            continue
        c["hint"] = {"ash_tags": c["topics"]}
        out.append(c)
    return out
