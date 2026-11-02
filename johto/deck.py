"""`johto deck`: three ready picks, weighted toward repos Austin already knows and toward work unlike what he did.

GPT Pro's UX review, section 7 of the history plan: keep a small deck of prepared problems with their slot fit
shown, so starting a task never begins with a search. Candidates come from two places johto already ranks:

- repo research (data/dossiers): each opportunity of a ready or actioned dossier that no task used yet;
- scout leads (`johto next`, scout.rank.rank()) that aren't claimed, used or dropped.

A pick needs an open slot it fits, must not look solved upstream, and must not be likely to be one-shot. It
scores higher for a familiar repo (one he has a task on), more open slots, a lower one-shot chance and a
dossier's high promise; lower when it overlaps a prompt already written on that repo (his or anyone's in
Studio). The three are chosen one by one, each repeat of a repo or an action costing a little, at most two per
repo. Each pick shows the open slot combinations it fits, how many remain, and the command or page that claims
it. Reads only.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from . import config, workspace

N = 3
MAX_PER_REPO = 2
OVERLAP_PENALTY_AT = 0.4    # a Studio prompt on the repo sharing this much of the pick's words (scout.rank's rule)
MATCH_AT, MATCH_LEAD = 0.4, 0.1   # a task without a dossier reference took the opportunity its text matches best
CLOSE_AT = 0.5              # a pick whose title words his task on the repo mostly uses is close to that task
ONE_SHOT = {"unlikely": 2.0, "possible": 1.0, "likely": -9.0}        # dossier one_shot.likelihood
LEAD_RISK = {"low": "unlikely", "medium": "possible", "high": "likely"}  # scout one_shot_risk.level, same words
SIZE = {"S": -0.5, "M": 0.5, "L": 0.0}
PROMISE = {"high": 1.0, "medium": 0.5, "low": -0.5}


# --- what is open and what he knows ------------------------------------------------------------------------

def slot_groups() -> list[dict]:
    """data/slots.json groups (read at call time, so a JOHTO_ROOT copy or a test's folder is honoured)."""
    try:
        return json.loads((config.DATA / "slots.json").read_text()).get("groups") or []
    except (OSError, ValueError):
        return []


def fit_rows(fits: list[dict], groups: list[dict]) -> list[dict]:
    """Per (action, domain, codebase type) the pick fits: open slots and the prompt style / mode variants."""
    out, seen = [], set()
    for f in fits or []:
        k = (f.get("action"), f.get("domain"), f.get("codebase_type"))
        if None in k or k in seen:
            continue
        seen.add(k)
        vs = [{"prompt_style": g["slot"].get("prompt_style"), "interaction_mode": g["slot"].get("interaction_mode"),
               "count": int(g.get("count") or 0)}
              for g in groups if (g["slot"].get("action"), g["slot"].get("domain"), g["slot"].get("codebase_type")) == k
              and int(g.get("count") or 0) > 0]
        vs.sort(key=lambda v: -v["count"])
        out.append({"action": k[0], "domain": k[1], "codebase_type": k[2], "open": sum(v["count"] for v in vs),
                    "variants": vs})
    out.sort(key=lambda r: -r["open"])
    return out


TEXT_FIELDS = ("field_opening_prompt", "field_high_level_goal", "field_rationale_difficulty", "field_codebase_overview")


def _leads() -> dict[str, dict]:
    try:
        return {x.get("id"): x for x in json.loads((config.DATA / "scout" / "leads.json").read_text())}
    except (OSError, ValueError):
        return {}


def _studio_text(d: Path) -> str:
    try:
        f = json.loads((d / "app" / "studio-fields.json").read_text()).get("fields") or {}
    except (OSError, ValueError, AttributeError):
        return ""
    return " ".join(str(f.get(k) or "") for k in TEXT_FIELDS)


def my_tasks() -> list[dict]:
    """Austin's task folders: {short, repo (lower case), action, status, prompt, text, dossier, lead_id}. `text`
    is everything that says what the task is about: its label, its lead, the local fields and Studio's copy."""
    leads = _leads()
    out = []
    for p in sorted(config.TASKS.glob("*/task.json")):
        try:
            meta = workspace.load(p.parent)
        except (OSError, ValueError):
            continue
        try:
            sections = workspace.parse_submission(p.parent)["sections"]
        except OSError:
            sections = {}
        lead = leads.get(meta.get("lead_id")) or {}
        text = " ".join([meta.get("label") or "", lead.get("title") or "", lead.get("mechanism") or "",
                         *sections.values(), _studio_text(p.parent)])
        out.append({"short": p.parent.name, "repo": ((meta.get("repo") or {}).get("full_name") or "").lower(),
                    "action": (meta.get("slot") or {}).get("action"),
                    "status": (meta.get("studio") or {}).get("status") or "Writing",
                    "prompt": sections.get("Opening Prompt", ""), "text": text,
                    "dossier": meta.get("dossier") or {}, "lead_id": meta.get("lead_id")})
    return out


def contained(title: str, text: str) -> float:
    """Share of a pick's title words that a task's text uses: high when the task is about the same problem."""
    a, b = _tokens(title), _tokens(text)
    return len(a & b) / len(a) if a else 0.0


def used_opportunities(tasks: list[dict], docs: dict[str, dict]) -> dict[tuple, str]:
    """{(dossier id, opportunity id): task short} for the opportunities his tasks took: the task's own
    dossier reference, else (tasks claimed another way) the opportunity whose title the task's text contains
    best, when that is at least MATCH_AT and clearly ahead of the next."""
    from .dossier import schema
    out: dict[tuple, str] = {}
    for t in tasks:
        ref = t.get("dossier") or {}
        if ref.get("id") and ref.get("opportunity_id"):
            out[(ref["id"], ref["opportunity_id"])] = t["short"]
            continue
        rows = []
        for did, doc in docs.items():
            if (doc.get("repo") or "").lower() != t["repo"]:
                continue
            for o in schema.opportunities(doc):
                rows.append((contained(f"{o.get('title') or ''} {o.get('area') or ''}", t["text"]), did, o.get("id")))
        rows.sort(reverse=True)
        if rows and rows[0][0] >= MATCH_AT and (len(rows) == 1 or rows[0][0] - rows[1][0] >= MATCH_LEAD):
            out[(rows[0][1], rows[0][2])] = t["short"]
    return out


def _studio_tasks() -> list[dict]:
    try:
        return json.loads((config.DATA / "studio_tasks.json").read_text())
    except (OSError, ValueError):
        return []


def _tokens(text: str) -> set[str]:
    from .scout import rank
    return rank._tokens(text)


def overlap(text: str, repo: str, prompts: list[tuple[str, str, str]]) -> tuple[float, str | None]:
    """Highest token overlap between a pick's text and a prompt already written on the same repo:
    (share, "Task xyz" or the short of his task)."""
    mine = _tokens(text)
    best, who = 0.0, None
    for r, name, prompt in prompts:
        if r != repo or not prompt:
            continue
        other = _tokens(prompt)
        if not other or not mine:
            continue
        ov = len(mine & other) / min(len(mine), len(other))
        if ov > best:
            best, who = ov, name
    return best, who


# --- candidates --------------------------------------------------------------------------------------------

def _web_base() -> str:
    try:
        url = (config.DATA / "web-url.txt").read_text().strip()
    except OSError:
        url = ""
    return (url or f"http://127.0.0.1:{config.PORT}/").rstrip("/") + "/"


def _slot_arg(f: dict) -> str:
    return f"{f['action']} | {f['domain']} | {f['codebase_type']}"


def _dossiers() -> list[tuple[dict, dict]]:
    """(index item, dossier) for each ready or actioned dossier."""
    from .dossier import store
    try:
        items = store.load().get("items") or []
    except Exception:
        return []
    out = []
    for it in items:
        if it.get("status") in ("ready", "actioned"):
            doc = store.load_dossier(it["id"])
            if doc:
                out.append((it, doc))
    return out


def dossier_candidates(groups: list[dict], tasks: list[dict]) -> list[dict]:
    from .dossier import schema
    pairs = _dossiers()
    used = used_opportunities(tasks, {it["id"]: doc for it, doc in pairs})
    out = []
    for it, doc in pairs:
        repo = (doc.get("repo") or it.get("repo") or "")
        promise = (doc.get("verdict") or {}).get("promise")
        for o in schema.opportunities(doc):
            if (it["id"], o.get("id")) in used:
                continue
            bt = o.get("bounded_task") or {}
            out.append({
                "kind": "dossier", "id": f"{it['id']}/{o.get('id')}", "dossier_id": it["id"],
                "opportunity_id": o.get("id"), "lead_id": None, "repo": repo, "title": o.get("title") or "",
                "area": o.get("area"), "fits": fit_rows(o.get("slot_fit") or [], groups),
                "one_shot": (o.get("one_shot") or {}).get("likelihood"), "size": bt.get("size"),
                "looks_solved": bool((o.get("upstream") or {}).get("looks_solved")), "promise": promise,
                "commit": doc.get("commit") or it.get("commit"),
                "text": " ".join(str(x or "") for x in (o.get("title"), o.get("area"), o.get("ambition"),
                                                        bt.get("shape"))),
            })
    return out


def lead_candidates(groups: list[dict], tasks: list[dict]) -> list[dict]:
    """Scout leads as `johto next` ranks them. Skipped when the slot or Studio task files are missing (ranking
    would fetch them from Studio)."""
    if not (config.DATA / "slots.json").exists() or not (config.DATA / "studio_tasks.json").exists():
        return []
    try:
        from .scout import rank
        rows = rank.rank()
    except Exception:
        return []
    claimed = {t["lead_id"] for t in tasks if t.get("lead_id")}
    out = []
    for r in rows:
        lead = r["lead"]
        if lead.get("status") in ("claimed", "used", "dropped", "discarded") or lead.get("id") in claimed:
            continue
        verdict = (r.get("verdict") or {}).get("overall")
        if verdict == "reject":
            continue
        out.append({
            "kind": "lead", "id": lead["id"], "dossier_id": None, "opportunity_id": None, "lead_id": lead["id"],
            "repo": lead.get("repo") or "", "title": lead.get("title") or "", "area": None,
            "fits": fit_rows([lead.get("slot_fit") or {}], groups),
            "one_shot": LEAD_RISK.get((lead.get("one_shot_risk") or {}).get("level")), "size": None,
            "looks_solved": bool((lead.get("upstream") or {}).get("looks_solved")), "promise": None,
            "verdict": verdict, "commit": lead.get("commit"),
            "text": " ".join([lead.get("title") or "", lead.get("mechanism") or "", *(lead.get("facts") or [])]),
        })
    return out


# --- scoring and choosing ----------------------------------------------------------------------------------

def score(c: dict, tasks: list[dict], prompts: list[tuple[str, str, str]]) -> dict:
    """Adds score, familiar, used_in, open_total, overlap and why to a candidate. `prompts` are Studio's
    opening prompts by anyone: (repo, task name, prompt)."""
    repo = c["repo"].lower()
    mine = [t for t in tasks if t["repo"] == repo]
    c["familiar"] = bool(mine)
    c["used_in"] = [t["short"] for t in mine]
    c["open_total"] = sum(f["open"] for f in c["fits"])
    close = max(((contained(f"{c['title']} {c.get('area') or ''}", t["text"]), t["short"]) for t in mine),
                default=(0.0, None))
    ov, who = overlap(c["text"], repo, prompts)
    if close[0] >= CLOSE_AT and close[0] >= ov:
        ov, who = close
    c["overlap"] = {"share": round(ov, 2), "with": who}
    s = 1.0 * math.log1p(c["open_total"])
    why = []
    if mine:
        s += 3.0
        acc = sum(1 for t in mine if t["status"] == "Accepted")
        why.append(f"you know this repo ({len(mine)} task{'s' if len(mine) != 1 else ''}"
                   + (f", {acc} accepted" if acc else "") + ")")
        if c["fits"] and not any(t["action"] == c["fits"][0]["action"] for t in mine):
            s += 0.5
            act = c["fits"][0]["action"]
            why.append(f"{'an' if act[:1] in 'AEIOU' else 'a'} {act} task, unlike your others there")
    s += ONE_SHOT.get(c.get("one_shot") or "", 0.0)
    if c.get("one_shot") in ("unlikely", "possible"):
        why.append(f"one-shot {c['one_shot']}")
    s += SIZE.get(c.get("size") or "", 0.0)
    s += PROMISE.get(c.get("promise") or "", 0.0)
    s += {"ok": 1.0, "maybe": 0.0}.get(c.get("verdict") or "", 0.0)
    if ov > OVERLAP_PENALTY_AT:
        s -= 3.0
        why.append(f"close to {who} ({ov:.0%} of its words match)")
    c["score"] = round(s, 2)
    c["why"] = why
    return c


def eligible(c: dict) -> bool:
    return c["open_total"] > 0 and not c["looks_solved"] and c.get("one_shot") != "likely"


def choose(cands: list[dict], n: int = N) -> list[dict]:
    """Greedy: the best, then the best of the rest after a small cost for each repeat of a repo or an action."""
    left = sorted(cands, key=lambda c: -c["score"])
    out: list[dict] = []
    while left and len(out) < n:
        def adjusted(c):
            same_repo = sum(1 for p in out if p["repo"].lower() == c["repo"].lower())
            same_action = sum(1 for p in out if p["fits"] and c["fits"] and p["fits"][0]["action"] == c["fits"][0]["action"])
            return c["score"] - 1.5 * same_repo - 1.0 * same_action - (99 if same_repo >= MAX_PER_REPO else 0)
        best = max(left, key=adjusted)
        if adjusted(best) < -50:
            break
        left.remove(best)
        out.append(best)
    return out


def _command(c: dict) -> tuple[str, str | None]:
    """(terminal command, web page) that claims the pick."""
    if c["kind"] == "lead":
        return f"johto start --lead {c['lead_id']}", None
    f = c["fits"][0] if c["fits"] else None
    cmd = f"johto start --repo {c['repo']}" + (f" --commit {c['commit']}" if c.get("commit") else "")
    if f:
        cmd += f" --slot '{_slot_arg(f)}'"
    return cmd, f"{_web_base()}repos/{c['dossier_id']}#o={c['opportunity_id']}"


def picks(n: int = N) -> list[dict]:
    """The deck: up to n picks, best first. Each: {kind, id, dossier_id, opportunity_id, lead_id, repo, title,
    area, familiar, used_in, fits: [{action, domain, codebase_type, open, variants: [{prompt_style,
    interaction_mode, count}]}], open_total, one_shot, size, why, score, overlap, command, url}."""
    groups = slot_groups()
    tasks = my_tasks()
    prompts = [((s.get("repo") or "").lower(), s.get("task_name") or "", s.get("opening_prompt") or "")
               for s in _studio_tasks()]
    cands = [score(c, tasks, prompts) for c in dossier_candidates(groups, tasks) + lead_candidates(groups, tasks)]
    out = choose([c for c in cands if eligible(c)], n)
    for c in out:
        c["command"], c["url"] = _command(c)
        c.pop("text", None)
        c.pop("looks_solved", None)
    return out


# --- output ------------------------------------------------------------------------------------------------

def _variant(v: dict) -> str:
    mode = (v.get("interaction_mode") or "").split("/")[0].strip()
    return f"{v.get('prompt_style')} / {mode} {v['count']}"


def render(ps: list[dict]) -> str:
    if not ps:
        return ("No ready picks: no dossier opportunity or lead fits an open slot. `johto slots --refresh` updates "
                "the open slots; the Repos page researches more repos.")
    lines = []
    for i, p in enumerate(ps, 1):
        tag = "familiar repo" if p["familiar"] else "new repo"
        lines.append(f"{i}. {p['repo']} · {p['title']}  ({tag}{', ' + p['size'] if p.get('size') else ''})")
        for f in p["fits"][:3]:
            vs = ", ".join(_variant(v) for v in f["variants"][:4])
            lines.append(f"   fits {f['action']} | {f['domain']} | {f['codebase_type']}: {f['open']} open"
                         + (f" ({vs})" if vs else ""))
        if p["why"]:
            why = "; ".join(p["why"])
            lines.append(f"   {why[:1].upper()}{why[1:]}.")
        lines.append(f"   claim: {p['command']}")
        if p.get("url"):
            lines.append(f"   or open {p['url']}")
    return "\n".join(lines)


def cmd_deck(a) -> None:
    ps = picks(a.n)
    if a.json:
        print(json.dumps(ps, indent=1, default=str))
        return
    print(render(ps))


def register(sub) -> None:
    s = sub.add_parser("deck", help="three ready picks, with the open slots each fits")
    s.add_argument("-n", type=int, default=N, help="how many picks (default 3)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_deck)
