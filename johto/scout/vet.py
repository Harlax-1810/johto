"""Check a candidate repository against Johto's codebase rules.

Mirrors Studio's "Codebase Check" AutoQC (reachable pinned commit, application not
library, size fits the codebase type, upstream and not SWE-bench, runnable verification),
and adds the onboarding's other avoid-list items: AI-generated repos, heavy reuse in
Studio, and dependence on paid services.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path, PurePosixPath

from .. import config, slots
from . import github

REPOS_FILE = config.DATA / "scout" / "repos.json"
_LOCK = threading.RLock()  # vet() may run in worker threads; repos.json is read-modify-write

SOURCE_EXT = {
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".kts",
    ".cs", ".swift", ".php", ".rb", ".dart", ".sql", ".sh", ".bash", ".hs", ".ex", ".exs", ".erl",
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".scala", ".clj", ".cljs", ".lua", ".vue",
    ".svelte", ".gd", ".m", ".mm", ".zig", ".nim", ".ml", ".elm", ".fs", ".r", ".jl", ".astro",
}
SKIP_DIRS = {
    "node_modules", "vendor", "dist", "build", "out", "target", ".git", "third_party",
    "third-party", "thirdparty", "external", "generated", "__generated__", ".next", "coverage",
    "bower_components", "docs", "doc", "examples", "example", "site-packages", ".venv", "venv",
    "Pods", "deps", ".yarn", "public/build", "static/vendor", "assets/vendor", "lib/vendor",
}
TEST_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs|e2e|integration[-_]tests?)(/|$)|_test\.(go|py|rb|exs?)$|"
    r"(^|/)test_[^/]+\.py$|\.(test|spec)\.(t|j)sx?$|_spec\.rb$|(^|/)[A-Z]\w*Tests?\.(java|kt|cs|swift)$",
    re.I)
AGENT_FILES = ("CLAUDE.md", "AGENTS.md", "AGENT.md", ".cursorrules", ".github/copilot-instructions.md",
               "GEMINI.md", ".windsurfrules", ".clinerules", "WARP.md", "CRUSH.md", "QWEN.md",
               "opencode.json", ".aider.conf.yml")
AGENT_DIRS = (".claude/", ".cursor/", ".aider", ".codex/", ".gemini/", ".kiro/", ".agents/", ".agent/",
              ".roo/", ".opencode/", ".junie/", ".augment/", ".amazonq/", ".trae/", ".windsurf/", ".qoder/",
              ".crush/", ".specstory/", ".taskmaster/", "memory-bank/", ".github/instructions/",
              ".github/prompts/", ".github/chatmodes/", ".clinerules/")
AI_COMMIT_RE = re.compile(
    r"co-authored-by:\s*(claude|codex|copilot|cursor|devin|gemini|chatgpt|openai|aider|jules|openhands|"
    r"cline|kiro|windsurf|cascade|qwen|grok|amp\b)|"
    r"generated with \[?(claude code|codex|cursor|amp)|🤖 generated with|\[ai\]|noreply@anthropic\.com|"
    r"cursoragent@cursor\.com|noreply@openai\.com", re.I)
AI_AUTHORS = re.compile(r"(claude|copilot|codex|devin|cursor|gemini|openai|google-labs-jules|openhands|"
                        r"sweep-ai)[-_\w]*(\[bot\])?$", re.I)
PAID_API_RE = re.compile(
    r"\b(OPENAI_API_KEY|ANTHROPIC_API_KEY|STRIPE_(SECRET|API)_KEY|TWILIO|AWS_ACCESS_KEY_ID|"
    r"GOOGLE_MAPS_API_KEY|SENDGRID_API_KEY|requires? an? (paid|api) (account|key|subscription))", re.I)
APP_HINTS = re.compile(r"\b(self-host|selfhost|docker[- ]compose|screenshots?|demo|deploy|"
                       r"web ?app|the app|application|install(ation)? guide|features|game|play)\b", re.I)
LIB_HINTS = re.compile(r"\b(library|framework|sdk|api reference|bindings|npm install --save|"
                       r"as a dependency|import \w+ from ['\"][\w@/-]+['\"]|go get [\w./-]+$)\b", re.I | re.M)
STUDIO_LANG = {"TypeScript": "Typescript", "JavaScript": "JavaScript", "Python": "Python", "Go": "Go",
               "Rust": "Rust", "Java": "Java", "Kotlin": "Kotlin", "C#": "C#", "Swift": "Swift",
               "PHP": "PHP", "Ruby": "Ruby", "Dart": "Dart", "Shell": "Shell", "Haskell": "Haskell",
               "Vue": "JavaScript", "Svelte": "JavaScript", "PLpgSQL": "SQL", "TSQL": "SQL"}


def load_repos() -> dict:
    return json.loads(REPOS_FILE.read_text()) if REPOS_FILE.exists() else {}


def save_repos(d: dict) -> None:
    REPOS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = REPOS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=1, sort_keys=True))
    tmp.replace(REPOS_FILE)  # atomic, so readers never see a half-written file


def studio_language(gh_lang: str | None) -> str:
    return STUDIO_LANG.get(gh_lang or "", "Other")


def _skipped(path: str) -> bool:
    parts = PurePosixPath(path).parts
    for i in range(len(parts) - 1):
        if parts[i] in SKIP_DIRS or "/".join(parts[i:i + 2]) in SKIP_DIRS:
            return True
    name = parts[-1]
    return name.endswith((".min.js", ".d.ts", ".pb.go", "_pb2.py", ".generated.ts", ".g.dart"))


def source_paths(paths: list[str]) -> list[str]:
    return [p for p in paths if PurePosixPath(p).suffix.lower() in SOURCE_EXT and not _skipped(p)]


def count_files(paths: list[str]) -> dict:
    src = source_paths(paths)
    tests = [p for p in src if TEST_RE.search(p)]
    by_ext: dict[str, int] = {}
    for p in src:
        e = PurePosixPath(p).suffix.lower()
        by_ext[e] = by_ext.get(e, 0) + 1
    top = sorted(by_ext.items(), key=lambda kv: -kv[1])[:5]
    return {"source_files": len(src), "test_files": len(tests), "tracked_files": len(paths),
            "top_extensions": dict(top), "test_examples": tests[:5]}


def verification(full: str, sha: str, paths: set[str]) -> dict:
    cmds = []
    if "package.json" in paths:
        try:
            pkg = json.loads(github.file_text(full, "package.json", sha) or "{}")
            scripts = pkg.get("scripts") or {}
            cmds += [f"npm run {k}" for k in ("test", "test:unit", "typecheck", "check", "build", "lint")
                     if k in scripts]
        except json.JSONDecodeError:
            pass
    if "Makefile" in paths:
        mk = github.file_text(full, "Makefile", sha)
        cmds += [f"make {t}" for t in ("test", "check", "build") if re.search(rf"^{t}:", mk, re.M)]
    if "go.mod" in paths:
        cmds.append("go test ./...")
    if "Cargo.toml" in paths:
        cmds.append("cargo test")
    if paths & {"pytest.ini", "tox.ini", "conftest.py", "setup.cfg", "pyproject.toml"}:
        cmds.append("pytest")
    if paths & {"pom.xml", "build.gradle", "build.gradle.kts"}:
        cmds.append("gradle/maven test")
    if "composer.json" in paths:
        cmds.append("composer test / phpunit")
    if "Gemfile" in paths and any(p.startswith("spec/") for p in paths):
        cmds.append("bundle exec rspec")
    if "pubspec.yaml" in paths:
        cmds.append("flutter/dart test")
    if "project.godot" in paths:
        cmds.append("godot --headless (check for GUT/gdUnit tests)")
    ci = sorted(p for p in paths if p.startswith(".github/workflows/"))
    return {"commands": cmds, "ci_workflows": ci[:6]}


def ai_signals(full: str, sha: str, paths: set[str]) -> dict:
    agent_files = [p for p in paths if p in AGENT_FILES or p.endswith(tuple("/" + a for a in AGENT_FILES))]
    agent_dirs = sorted({d for p in paths for d in AGENT_DIRS if p.startswith(d)})
    hits = []
    try:
        cs = github.commits(full, sha, 100)
    except github.GitHubError:
        cs = []
    for c in cs:
        msg = (c.get("commit") or {}).get("message", "")
        author = ((c.get("author") or {}).get("login") or "")
        if AI_COMMIT_RE.search(msg) or AI_AUTHORS.match(author):
            hits.append({"sha": c["sha"], "title": msg.splitlines()[0][:100] if msg else "",
                         "author": author})
    n = max(len(cs), 1)
    frac = len(hits) / n
    if agent_files or agent_dirs or frac >= 0.15 or len(hits) >= 10:
        level = "heavy"
    elif hits:
        level = "some"
    else:
        level = "none"
    return {"level": level, "agent_files": agent_files[:6] + agent_dirs, "ai_commits": len(hits),
            "commits_sampled": len(cs), "examples": hits[:3]}


def _app_or_lib(readme: str, meta: dict, paths: set[str]) -> tuple[str, str]:
    topics = set(meta.get("topics") or [])
    desc = meta.get("description") or ""
    text = desc + "\n" + readme[:8000]
    app = len(APP_HINTS.findall(text)) + (2 if paths & {"docker-compose.yml", "docker-compose.yaml",
                                                         "compose.yml", "compose.yaml", "Dockerfile"} else 0)
    app += 2 if "project.godot" in paths else 0
    lib = len(LIB_HINTS.findall(text)) + 3 * len(topics & {"library", "framework", "sdk", "npm-package",
                                                            "python-library", "component-library"})
    if re.search(r"\b(awesome list|curated list|tutorial|course)\b", desc, re.I):
        return "fail", "curated list / tutorial"
    if lib >= app + 3:
        return "fail", f"library signals {lib} vs app {app}"
    if lib > app:
        return "borderline", f"library signals {lib} vs app {app}"
    return "pass", f"app signals {app}, library {lib}"


def _english(readme: str) -> bool:
    text = re.sub(r"https?://\S+|<[^>]+>|!\[[^\]]*\]\([^)]*\)|```.*?```", " ", readme[:12000], flags=re.S)
    words = re.findall(r"[A-Za-z]+", text[:6000].lower())
    if len(words) < 30:
        return bool(readme.strip()) and sum(ch.isascii() for ch in readme[:2000]) / max(len(readme[:2000]), 1) > 0.9
    common = {"the", "and", "to", "of", "a", "is", "in", "for", "you", "with", "this", "it", "on", "your"}
    return sum(w in common for w in words) / len(words) > 0.06


# --- Traits: what kind of application this is (read by pool.py) ---------------------

MANIFEST_NAMES = ("package.json", "go.mod", "pyproject.toml", "requirements.txt", "Pipfile", "Gemfile",
                  "composer.json", "Cargo.toml", "mix.exs", "pom.xml", "build.gradle", "build.gradle.kts")
COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
                 "docker-compose.dev.yml", "docker-compose.prod.yml")


def _names(alts: str) -> re.Pattern:
    """Match whole dependency names (bounded by start, space, quote, slash or @)."""
    return re.compile(rf"(?<![\w.-])({alts})(?![\w-])", re.I | re.M)


DEP_SIGNALS = {
    "stores": [
        ("Postgres", _names(r"pg|postgres|postgresql|psycopg2?(-binary)?|psycopg|asyncpg|jackc/pgx(/v\d)?|lib/pq|"
                            r"pg-promise|pgvector|postgres-js|@neondatabase/serverless|postgis")),
        ("MySQL", _names(r"mysql2?|mysqlclient|pymysql|aiomysql|mariadb|go-sql-driver/mysql")),
        ("SQLite", _names(r"sqlite3?|better-sqlite3|go-sqlite3|mattn/go-sqlite3|modernc\.org/sqlite|libsql|"
                          r"@libsql/client|aiosqlite|glebarez/sqlite|ncruces/go-sqlite3")),
        ("MongoDB", _names(r"mongodb|mongoose|pymongo|motor|mongo-driver")),
        ("Redis", _names(r"redis|ioredis|go-redis|valkey|aioredis|redis/go-redis(/v\d)?|rueidis")),
        ("embedded store", _names(r"bbolt|etcd-io/bbolt|boltdb/bolt|dgraph-io/badger(/v\d)?|cockroachdb/pebble|"
                                  r"leveldb|goleveldb|rocksdb|lmdb|buntdb|nutsdb")),
        ("search index", _names(r"elasticsearch|@elastic/elasticsearch|opensearch|meilisearch|typesense|"
                                r"blevesearch/bleve(/v\d)?|tantivy|whoosh")),
        ("ClickHouse", _names(r"clickhouse|clickhouse-go(/v\d)?|@clickhouse/client|clickhouse-driver")),
        ("browser database", _names(r"dexie|idb|localforage|rxdb|pouchdb")),
    ],
    "orm": _names(r"prisma|@prisma/client|typeorm|sequelize|knex|drizzle-orm|kysely|@mikro-orm/core|objection|"
                  r"gorm\.io/gorm|jmoiron/sqlx|sqlx|entgo\.io/ent|uptrace/bun|sqlc|pressly/goose(/v\d)?|"
                  r"golang-migrate/migrate(/v\d)?|sqlalchemy|sqlmodel|django|peewee|tortoise-orm|alembic|"
                  r"activerecord|rails|doctrine/orm|laravel/framework|spring-boot-starter-data-jpa|"
                  r"hibernate-core|ecto_sql|diesel|sea-orm|xorm\.io/xorm|upper/db|go-pg/pg|go-jet/jet"),
    "jobs": _names(r"bullmq|bull|bee-queue|agenda|node-cron|croner|node-schedule|pg-boss|graphile-worker|"
                   r"hibiken/asynq|asynq|riverqueue/river|robfig/cron(/v\d)?|go-co-op/gocron(/v\d)?|celery|rq|"
                   r"dramatiq|huey|apscheduler|arq|sidekiq|resque|good_job|oban|temporalio|@temporalio/worker|"
                   r"amqplib|rabbitmq/amqp091-go|streadway/amqp|nats|nats-io/nats\.go|kafkajs|segmentio/kafka-go|"
                   r"confluent-kafka|aiokafka|pika|kombu|django-q|procrastinate|faktory"),
    "realtime": _names(r"socket\.io|socket\.io-client|ws|gorilla/websocket|coder/websocket|nhooyr\.io/websocket|"
                       r"websockets|channels|yjs|y-websocket|y-protocols|automerge|@automerge/automerge|"
                       r"@liveblocks/client|centrifugal/centrifuge|mqtt|eclipse/paho\.mqtt\.golang|"
                       r"pion/webrtc(/v\d)?|mediasoup|livekit-server-sdk|@hocuspocus/server|sse-starlette"),
    "ai": _names(r"openai|anthropic|@anthropic-ai/sdk|langchain|@langchain/core|langgraph|llama-index|llama_index|"
                 r"ollama|transformers|ai|@ai-sdk/openai|litellm|@modelcontextprotocol/sdk|mcp|"
                 r"sentence-transformers|chromadb|qdrant-client|weaviate-client|pinecone|tiktoken|"
                 r"sashabaranov/go-openai|tmc/langchaingo|openai/openai-go|anthropics/anthropic-sdk-go"),
    "frontend": _names(r"react|react-dom|vue|svelte|@sveltejs/kit|@angular/core|solid-js|preact|next|nuxt|astro|"
                       r"htmx\.org|lit|ember-source|alpinejs|@remix-run/react|@builder\.io/qwik|jquery"),
    "game": _names(r"phaser|pixi\.js|babylonjs|@babylonjs/core|kaboom|kaplay|excalibur|pygame|pygame-ce|arcade|"
                   r"pyglet|raylib|hajimehoshi/ebiten(/v\d)?|bevy|macroquad|colyseus|boardgame\.io|matter-js|"
                   r"planck-js|ursina|panda3d"),
}
COMPOSE_STORES = [
    ("Postgres", re.compile(r"image:\s*['\"]?[\w./-]*(postgres|postgis|timescale|pgvector)", re.I)),
    ("MySQL", re.compile(r"image:\s*['\"]?[\w./-]*(mysql|mariadb)", re.I)),
    ("MongoDB", re.compile(r"image:\s*['\"]?[\w./-]*mongo", re.I)),
    ("Redis", re.compile(r"image:\s*['\"]?[\w./-]*(redis|valkey|dragonfly|keydb)", re.I)),
    ("search index", re.compile(r"image:\s*['\"]?[\w./-]*(elasticsearch|opensearch|meilisearch|typesense)", re.I)),
    ("ClickHouse", re.compile(r"image:\s*['\"]?[\w./-]*clickhouse", re.I)),
    ("message queue", re.compile(r"image:\s*['\"]?[\w./-]*(rabbitmq|nats|kafka|redpanda)", re.I)),
    ("object storage", re.compile(r"image:\s*['\"]?[\w./-]*(minio|seaweedfs|garage)", re.I)),
]
MIGRATION_PATH = re.compile(r"(^|/)(migrations?|migrate|alembic|db/migrate|schema_migrations)/|"
                            r"(^|/)prisma/schema\.prisma$|(^|/)schema\.sql$", re.I)
JOBS_PATH = re.compile(r"(^|/)(workers?|jobs|queues?|cron|crons|scheduler|schedulers|background)/", re.I)
SYNC_PATH = re.compile(r"(^|/)(sync|syncer|replication|replica|offline|crdt|collab|collaboration|realtime)"
                       r"(/|\.[a-z]+$|_)", re.I)
MULTIUSER_PATH = re.compile(r"(^|/)(auth|authz|authn|permissions?|rbac|acl|roles?|users?|teams?|"
                            r"organi[sz]ations?|orgs|tenants?|tenancy|workspaces?|memberships?|invites?)"
                            r"(/|\.[a-z]+$|_)", re.I)
FRONTEND_EXT = {".tsx", ".jsx", ".vue", ".svelte", ".astro"}
FRONTEND_DIR = re.compile(r"(^|/)(frontend|client|web|webapp|webui|web-ui|ui|www|components|pages|views|"
                          r"app/routes|src/routes|src/lib|src/app|src/stores?|src/hooks|src/composables|"
                          r"static|public|assets/js|js)/", re.I)
SERVER_EXT = {".go", ".py", ".rb", ".php", ".java", ".kt", ".cs", ".rs", ".ex", ".exs", ".erl", ".scala",
              ".clj"}
SERVER_DIR = re.compile(r"(^|/)(server|backend|api|services?|src/server|apps/api|apps/server|"
                        r"packages/server|packages/backend)/", re.I)


def _manifest_paths(paths: list[str]) -> list[str]:
    """Root manifests first, then up to two levels deep (monorepo apps), at most 8."""
    def depth(p: str) -> int:
        return p.count("/")
    picks = [p for p in paths if PurePosixPath(p).name in MANIFEST_NAMES and depth(p) <= 2 and not _skipped(p)
             and not re.search(r"(^|/)(test|tests|e2e|fixtures?|examples?|docs?|scripts)/", p)]
    picks.sort(key=lambda p: (depth(p), PurePosixPath(p).name != "package.json", p))
    return picks[:8]


def _dep_names(path: str, text: str) -> str:
    """Dependency names from a manifest, one per line (raw text when the format is not parsed)."""
    name = PurePosixPath(path).name
    if name in ("package.json", "composer.json"):
        try:
            pkg = json.loads(text or "{}")
        except json.JSONDecodeError:
            return ""
        keys = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies", "require",
                "require-dev")
        return "\n".join(k for sec in keys for k in (pkg.get(sec) or {}) if isinstance(pkg.get(sec), dict))
    if name == "go.mod":
        return "\n".join(m.group(1) for m in re.finditer(r"^\s*(?:require\s+)?([\w.-]+\.[a-z]+/[\w./-]+)\s+v",
                                                       text, re.M))
    if name == "Gemfile":
        return "\n".join(re.findall(r"^\s*gem\s+['\"]([\w-]+)", text, re.M))
    if name in ("requirements.txt", "Pipfile", "pyproject.toml"):
        return "\n".join(m.group(1) for m in re.finditer(r"(?:^|[\"'\s])([A-Za-z][\w.-]*)\s*(?:[<>=~!\[;]|$)",
                                                       text, re.M))
    return text


def traits(full: str, sha: str, paths: list[str], counts_src: list[str]) -> dict:
    manifests = _manifest_paths(paths)
    deps_text = "\n".join(_dep_names(p, github.file_text(full, p, sha)) for p in manifests)
    compose = [p for p in paths if PurePosixPath(p).name in COMPOSE_NAMES and p.count("/") <= 2
               and not _skipped(p)][:3]
    compose_text = "\n".join(github.file_text(full, p, sha) for p in compose)

    def hits(rx: re.Pattern, text: str) -> list[str]:
        return sorted({m.group(1).lower() for m in rx.finditer(text)})[:8]

    stores = {label for label, rx in DEP_SIGNALS["stores"] if rx.search(deps_text)}
    stores |= {label for label, rx in COMPOSE_STORES if rx.search(compose_text)}
    non_test = [p for p in counts_src if not TEST_RE.search(p)]
    frontend = [p for p in non_test if PurePosixPath(p).suffix.lower() in FRONTEND_EXT
                or (PurePosixPath(p).suffix.lower() in (".ts", ".js", ".mjs") and FRONTEND_DIR.search(p))]
    server = [p for p in non_test if PurePosixPath(p).suffix.lower() in SERVER_EXT
              or (PurePosixPath(p).suffix.lower() in (".ts", ".js", ".mjs") and SERVER_DIR.search(p))]
    migrations = sorted({MIGRATION_PATH.search(p).group(0).strip("/") for p in paths if MIGRATION_PATH.search(p)
                         and not _skipped(p)})
    jobs_dirs = sorted({JOBS_PATH.search(p).group(0).strip("/") for p in non_test if JOBS_PATH.search(p)})
    return {
        "frontend_files": len(frontend),
        "server_files": len(server),
        "sql_files": sum(p.lower().endswith(".sql") for p in paths if not _skipped(p)),
        "stores": sorted(stores),
        "orm": hits(DEP_SIGNALS["orm"], deps_text),
        "migrations": migrations[:4],
        "jobs": hits(DEP_SIGNALS["jobs"], deps_text) + jobs_dirs[:3],
        "realtime": hits(DEP_SIGNALS["realtime"], deps_text),
        "sync_paths": len([p for p in non_test if SYNC_PATH.search(p)]),
        "multi_user_paths": len([p for p in non_test if MULTIUSER_PATH.search(p)]),
        "ai_deps": hits(DEP_SIGNALS["ai"], deps_text),
        "frontend_deps": hits(DEP_SIGNALS["frontend"], deps_text),
        "game_deps": hits(DEP_SIGNALS["game"], deps_text),
        "godot": "project.godot" in paths or any(p.endswith("/project.godot") for p in paths[:20000]),
        "manifests": manifests,
        "compose_files": compose,
    }


def verdict(rec: dict, codebase_type: str) -> dict:
    n = rec["counts"]["source_files"]
    if codebase_type == "Enterprise-grade":
        size = "pass" if n >= 200 else "fail"
    else:
        size = "fail" if n < 20 or n > 1500 else ("borderline" if n > 500 else "pass")
    checks = {
        "reachable": "pass" if rec.get("commit") and not rec.get("tree_error") else "fail",
        "application": rec["app_check"][0],
        "size": size,
        "upstream": "fail" if rec["fork"] or rec["swe_bench"] else "pass",
        "verification": "pass" if rec["counts"]["test_files"] or rec["verify"]["commands"] else "fail",
        "ai_generated": {"none": "pass", "some": "borderline", "heavy": "fail"}[rec["ai"]["level"]],
        "studio_overuse": "pass" if rec["studio_usage"] < 3 else "borderline" if rec["studio_usage"] < 6 else "fail",
        "english": "pass" if rec["english_readme"] else "borderline",
        "self_contained": "borderline" if rec["paid_api_mentions"] else "pass",
    }
    fails = [k for k, v in checks.items() if v == "fail"]
    borders = [k for k, v in checks.items() if v == "borderline"]
    overall = "reject" if fails else ("maybe" if borders else "ok")
    return {"checks": checks, "overall": overall, "fails": fails, "borderline": borders}


def _add_found_via(rec: dict, found_via: dict | None) -> bool:
    if not found_via:
        return False
    seen = rec.setdefault("found_via", [])
    if any(f.get("source") == found_via.get("source") for f in seen):
        return False
    seen.append(found_via)
    del seen[:-12]
    return True


def vet(full_name: str, codebase_types: tuple[str, ...] = ("Personal-scale", "Enterprise-grade"),
        force: bool = False, found_via: dict | None = None, keep_commit: bool = False) -> dict:
    """Vet a repo and store the record in repos.json.

    `found_via` records which discovery query or list surfaced the repo (kept across re-vets).
    `keep_commit` re-vets at the commit already pinned instead of the branch head, so a pinned
    commit that dossiers or tasks refer to does not move.
    """
    full_name = full_name.lower()
    with _LOCK:
        repos = load_repos()
        old = repos.get(full_name)
        if (not force and old and time.time() - old.get("vetted_ts", 0) < 3 * 86400
                and "traits" in old):
            if _add_found_via(old, found_via):
                repos[full_name] = old
                save_repos(repos)
            return old
    meta = github.repo(full_name)
    real = meta["full_name"]
    branch = meta["default_branch"]
    if keep_commit and old and old.get("commit") and old["full_name"] == real.lower():
        sha = old["commit"]
    else:
        sha = github.head_sha(real, branch)
    rec: dict = {
        "full_name": real.lower(), "display_name": real, "html_url": meta["html_url"],
        "description": (meta.get("description") or "")[:300], "language": meta.get("language"),
        "studio_language": studio_language(meta.get("language")), "stars": meta.get("stargazers_count"),
        "topics": meta.get("topics", []), "license": (meta.get("license") or {}).get("spdx_id"),
        "default_branch": branch, "pushed_at": meta.get("pushed_at"), "fork": bool(meta.get("fork")),
        "fork_parent": (meta.get("parent") or {}).get("full_name"), "archived": bool(meta.get("archived")),
        "open_issues": meta.get("open_issues_count"), "has_issues": meta.get("has_issues"),
        "created_at": meta.get("created_at"),
        "commit": sha, "pinned_url": f"https://github.com/{real}/tree/{sha}",
        "swe_bench": real.lower() in {r.lower() for r in config.SWE_BENCH_REPOS},
    }
    try:
        tr = github.tree(real, sha)
        paths = [e["path"] for e in tr.get("tree", []) if e.get("type") == "blob"]
        rec["tree_truncated"] = bool(tr.get("truncated"))
    except github.GitHubError as e:
        paths, rec["tree_error"] = [], str(e)
    pset = set(paths)
    rec["counts"] = count_files(paths)
    rec["verify"] = verification(real, sha, pset)
    rec["ai"] = ai_signals(real, sha, pset)
    readme = github.readme(real)
    rec["english_readme"] = _english(readme)
    rec["app_check"] = _app_or_lib(readme, meta, pset)
    rec["docker"] = sorted(pset & {"docker-compose.yml", "docker-compose.yaml", "compose.yml",
                                   "compose.yaml", "Dockerfile"})
    rec["paid_api_mentions"] = sorted({m.group(0) for m in PAID_API_RE.finditer(readme)})[:5]
    rec["traits"] = traits(real, sha, paths, source_paths(paths))
    rec["studio_usage"] = slots.repo_usage().get(real.lower(), 0)
    rec["verdicts"] = {ct: verdict(rec, ct) for ct in codebase_types}
    rec["vetted_ts"] = time.time()
    rec["vetted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _LOCK:
        repos = load_repos()
        prev = repos.get(rec["full_name"]) or repos.get(full_name) or {}
        rec["found_via"] = list(prev.get("found_via") or [])
        _add_found_via(rec, found_via)
        repos[rec["full_name"]] = rec
        if full_name != rec["full_name"]:
            repos.pop(full_name, None)
        save_repos(repos)
    return rec
