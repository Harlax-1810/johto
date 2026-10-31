"""`johto scout ...` and `johto next` subcommands."""
from __future__ import annotations

import json
import sys
import textwrap

from .. import config, slots
from . import discover, github, leads, rank, vet

ACTIONS = ["Create", "Modify", "Operate", "Understand / Assess"]
DOMAINS = ["Backend / API", "Web frontend", "DevOps / cloud", "Dev tooling", "Data / DB", "Games",
           "Other software", "AI agents"]
CTYPES = ["Personal-scale", "Enterprise-grade"]


def _match(value: str, options: list[str], what: str) -> str:
    v = value.strip().lower()
    for o in options:
        if o.lower() == v:
            return o
    hits = [o for o in options if o.lower().startswith(v) or v in o.lower()]
    if len(hits) == 1:
        return hits[0]
    sys.exit(f"Unknown {what} {value!r}; choose from {options}")


def parse_slot(text: str) -> dict:
    """'Modify | Web frontend | Personal' -> slot dict. The largest open group fills style/mode."""
    parts = [p for p in (x.strip() for x in text.split("|")) if p]
    if len(parts) < 3:
        sys.exit('--slot needs "<action> | <domain> | <codebase type>"')
    slot = {"action": _match(parts[0], ACTIONS, "action"), "domain": _match(parts[1], DOMAINS, "domain"),
            "codebase_type": _match(parts[2], CTYPES, "codebase type")}
    groups = [g for g in slots.load()["groups"]
              if all(g["slot"][k] == slot[k] for k in ("action", "domain", "codebase_type"))]
    best = max(groups, key=lambda g: g["count"]) if groups else None
    slot["prompt_style"] = best["slot"]["prompt_style"] if best else None
    slot["interaction_mode"] = best["slot"]["interaction_mode"] if best else None
    slot["open"] = sum(g["count"] for g in groups)
    return slot


def auto_slots(n: int) -> list[dict]:
    """Top-N open (action, domain, codebase type) combinations by open-slot count."""
    agg: dict[tuple, dict] = {}
    for g in slots.load()["groups"]:
        k = (g["slot"]["action"], g["slot"]["domain"], g["slot"]["codebase_type"])
        a = agg.setdefault(k, {**g["slot"], "open": 0, "_best": 0})
        a["open"] += g["count"]
        if g["count"] > a["_best"]:
            a["_best"] = g["count"]
            a["prompt_style"], a["interaction_mode"] = g["slot"]["prompt_style"], g["slot"]["interaction_mode"]
    out = sorted(agg.values(), key=lambda a: -a["open"])[:n]
    for a in out:
        a.pop("_best", None)
    return out


def _slot_label(s: dict) -> str:
    return f"{s['action']} | {s['domain']} | {s['codebase_type']}"


def _vet_row(rec: dict, ctype: str) -> str:
    v = rec["verdicts"][ctype]
    c = rec["counts"]
    flag = {"ok": "OK  ", "maybe": "MAYBE", "reject": "NO  "}[v["overall"]]
    why = ",".join(v["fails"] + [b + "?" for b in v["borderline"]])
    return (f"  {flag} {rec['display_name']:<38} {rec['studio_language']:<10} src={c['source_files']:<5} "
            f"tests={c['test_files']:<4} ai={rec['ai']['level']:<6} used={rec['studio_usage']} {why}")


def vet_slot(slot: dict, limit: int, vet_limit: int, quiet: bool = False) -> list[dict]:
    cands = discover.discover(slot, limit=limit)
    if not quiet:
        print(f"\n{_slot_label(slot)}  (open {slot.get('open', '?')}) — {len(cands)} candidates, vetting {vet_limit}")
    out = []
    for c in cands[:vet_limit]:
        try:
            rec = vet.vet(c["full_name"])
        except github.GitHubError as e:
            if not quiet:
                print(f"  ERR  {c['display_name']}: {e}")
            continue
        out.append(rec)
        if not quiet:
            print(_vet_row(rec, slot["codebase_type"]))
    return out


# --- Handlers -------------------------------------------------------------------

def cmd_repos(args):
    targets = [parse_slot(args.slot)] if args.slot else auto_slots(args.auto)
    for slot in targets:
        vet_slot(slot, args.limit, args.vet_limit)


def _print_leads(ls: list[dict]) -> None:
    for l in ls:
        print(f"  [{l['id']}] ({l['source']}, risk {l['one_shot_risk']['level']}"
              f"{', LOOKS SOLVED' if l['upstream']['looks_solved'] else ''}) {l['title']}")


def cmd_leads(args):
    slot = parse_slot(args.slot)
    rec = vet.vet(args.repo)
    print(_vet_row(rec, slot["codebase_type"]))
    found = leads.upsert(leads.mine_issues(rec, slot, max_leads=args.max))
    print(f"issue leads: {len(found)}")
    _print_leads(found)
    if args.agent:
        print("running Claude lead scout (several minutes)...")
        ag = leads.upsert(leads.agent_leads(rec, slot, timeout=args.timeout))
        print(f"agent leads: {len(ag)}")
        _print_leads(ag)


def cmd_run(args):
    targets = [parse_slot(args.slot)] if args.slot else auto_slots(args.groups)
    picked: list[tuple[dict, dict]] = []
    for slot in targets:
        recs = vet_slot(slot, args.limit, args.vet_limit)
        good = [r for r in recs if r["verdicts"][slot["codebase_type"]]["overall"] == "ok"]
        maybe = [r for r in recs if r["verdicts"][slot["codebase_type"]]["overall"] == "maybe"]
        for r in (good + maybe)[: args.repos_per_group]:
            picked.append((r, slot))
    print(f"\nMining issues for {len(picked)} repos...")
    for rec, slot in picked:
        found = leads.upsert(leads.mine_issues(rec, slot, max_leads=args.max))
        print(f"  {rec['display_name']}: {len(found)} issue leads")
    budget = args.agent_leads
    for rec, slot in picked:
        if budget <= 0:
            break
        print(f"Claude lead scout on {rec['display_name']} for {_slot_label(slot)} ...")
        try:
            ag = leads.upsert(leads.agent_leads(rec, slot, timeout=args.timeout))
            print(f"  {len(ag)} agent leads")
        except (RuntimeError, OSError) as e:
            print(f"  failed: {e}")
        budget -= 1
    print()
    cmd_next(argparse_ns(n=args.n, slot=args.slot, all=False, json=False))


def argparse_ns(**kw):
    import argparse
    return argparse.Namespace(**kw)


def _wrap(prefix: str, text: str, width: int = 110) -> str:
    return textwrap.fill(text, width=width, initial_indent=prefix, subsequent_indent=" " * len(prefix))


def cmd_next(args):
    slot_filter = parse_slot(args.slot) if args.slot else None
    ranked = rank.rank(slot_filter, include_dropped=args.all)
    if args.json:
        print(json.dumps(ranked[: args.n], indent=1, default=str))
        return
    if not ranked:
        print("No leads yet. Run `johto scout run` first.")
        return
    for i, r in enumerate(ranked[: args.n], 1):
        l, rec = r["lead"], r["repo"]
        styles = ", ".join(f"{g['slot']['prompt_style']}/{g['slot']['interaction_mode'].split('/')[0]}×{g['count']}"
                           for g in r["groups"]) or "no open slot"
        c = rec.get("counts", {})
        print(f"\n{i}. [{l['id']}] {l['title']}   score {r['score']}")
        print(f"   slot  {_slot_label(l['slot_fit'])} — open {r['open']} ({styles})")
        print(f"   repo  {rec.get('display_name', l['repo'])} · {l['primary_language']} · "
              f"src {c.get('source_files', '?')} / tests {c.get('test_files', '?')} · "
              f"repo check {r['verdict'].get('overall', '?')} · used in Studio {r['studio_usage']}x · ai {rec.get('ai', {}).get('level', '?')}")
        print(f"         {l['repo_url_pinned']}")
        if l.get("issue_url"):
            print(f"   issue {l['issue_url']}")
        for f in l["facts"]:
            print(_wrap("   - ", f))
        if l["files"]:
            print(_wrap("   files ", ", ".join(l["files"][:6])))
        print(_wrap("   hard  ", l["mechanism"]))
        print(_wrap("   check ", l["verify"]))
        print(_wrap(f"   1-shot {l['one_shot_risk']['level']}: ", l["one_shot_risk"]["why"]))
        up = l.get("upstream") or {}
        if up.get("looks_solved"):
            print("   !! upstream: looks already solved")
        for rel in up.get("related", [])[:3]:
            print(f"   rel   [{rel.get('state')}] {rel['title'][:70]} {rel['url']}")
        if r["overlap"][0] > 0.4:
            print(f"   !! similar to existing Studio task {r['overlap'][1]} (overlap {r['overlap'][0]})")


def cmd_show(args):
    l = leads.find(args.lead_id)
    if not l:
        sys.exit(f"No lead {args.lead_id}")
    rec = vet.load_repos().get(l["repo"], {})
    print(json.dumps(l, indent=1))
    if rec:
        print("\nrepo verdicts:", json.dumps(rec.get("verdicts"), indent=1))
        print("verify:", rec.get("verify"))


def cmd_drop(args):
    l = leads.set_status(args.lead_id, "dropped")
    print(f"dropped {l['id']}: {l['title']}" if l else f"No lead {args.lead_id}")


# --- Registration ---------------------------------------------------------------

def register(sub) -> None:
    sc = sub.add_parser("scout", help="find repos and problem leads that fit open slots")
    ss = sc.add_subparsers(dest="scout_cmd", required=True)

    p = ss.add_parser("repos", help="discover and vet repos for a slot (or the top open slots)")
    p.add_argument("--slot", help='"<action> | <domain> | <codebase type>"')
    p.add_argument("--auto", type=int, default=3, help="top N open slot combos (when no --slot)")
    p.add_argument("--limit", type=int, default=15, help="candidates to discover per slot")
    p.add_argument("--vet-limit", type=int, default=8, help="candidates to vet per slot")
    p.set_defaults(func=cmd_repos)

    p = ss.add_parser("leads", help="problem leads for one repo and slot")
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--slot", required=True)
    p.add_argument("--agent", action=__import__("argparse").BooleanOptionalAction, default=False,
                   help="also run the Claude lead scout (slow, uses your Claude plan)")
    p.add_argument("--max", type=int, default=5, help="max issue leads")
    p.add_argument("--timeout", type=int, default=900)
    p.set_defaults(func=cmd_leads)

    p = ss.add_parser("run", help="end to end: open slots -> repos -> vet -> leads")
    p.add_argument("--slot")
    p.add_argument("--groups", type=int, default=3, help="top N open slot combos")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--vet-limit", type=int, default=8)
    p.add_argument("--repos-per-group", type=int, default=3)
    p.add_argument("--agent-leads", type=int, default=0, help="Claude lead-scout runs to allow (each ~5-10 min)")
    p.add_argument("--max", type=int, default=4, help="max issue leads per repo")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--n", type=int, default=10)
    p.set_defaults(func=cmd_run)

    p = ss.add_parser("show", help="full detail for one lead")
    p.add_argument("lead_id")
    p.set_defaults(func=cmd_show)

    p = ss.add_parser("drop", help="hide a lead from `johto next`")
    p.add_argument("lead_id")
    p.set_defaults(func=cmd_drop)

    p = sub.add_parser("next", help="ranked problem leads that match open slots")
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--slot")
    p.add_argument("--all", action="store_true", help="include dropped/used leads")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_next)
