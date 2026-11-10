"""Mercor Studio API client.

Studio authenticates with a short-lived Clerk token that lives in the browser. Instead of
copying tokens out, every request runs as a `fetch` inside a logged-in Studio tab through
browser-harness. Nothing secret is printed or stored.
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.parse

from . import config

MARK = "@@JOHTO@@"


class StudioError(RuntimeError):
    def __init__(self, status: int, body, path: str):
        self.status, self.body, self.path = status, body, path
        super().__init__(f"Studio {status} on {path}: {str(body)[:500]}")


_SCRIPT = r'''
import json, time
REQ = json.loads(%(req)r)
ACCOUNT = %(account)r
START = %(start)r
TIMEOUT = %(timeout)r
tabs = [t for t in list_tabs(include_chrome=False)
        if (t.get("url") or "").startswith("https://studio.mercor.com")
        and t.get("account") in (None, ACCOUNT)]
# Use a tab that is signed in: a tab left on Studio's sign-in page has no session even when another tab has one
# (2026-11-01, every read failed that way). With none signed in, the first tab shows why.
T = None
for t in sorted(tabs, key=lambda t: "/sign-in" in (t.get("url") or "")):
    try:
        if js("!!(window.Clerk && window.Clerk.session)", target_id=t["targetId"]):
            T = t["targetId"]
            break
    except Exception:
        pass
if T is None and tabs:
    T = next((t["targetId"] for t in tabs if "/sign-in" not in (t.get("url") or "")), tabs[0]["targetId"])
if T is None:
    # The multi-account fork's new_tab takes the Chrome profile account; the released browser-harness
    # (0.1.x) takes only the URL and opens the tab in the attached profile. Either way, open Studio.
    import inspect as _i
    T = new_tab(START, account=ACCOUNT) if "account" in _i.signature(new_tab).parameters else new_tab(START)
    if isinstance(T, dict):
        T = T.get("targetId") or T.get("target_id")
ready = False
for _ in range(60):
    if js("!!(window.Clerk && window.Clerk.session)", target_id=T):
        ready = True
        break
    time.sleep(0.5)
if not ready:
    print(%(mark)r + json.dumps({"status": 0, "body": "Studio tab has no Clerk session (logged out?)"}))
else:
    # The released browser-harness waits at most 5 s for one js() call, and a Studio request (Clerk token, then the
    # API) can take longer. So the fetch starts in the tab and parks its result on window.__johto, and short js()
    # calls poll for it until TIMEOUT.
    KEY = "r%%d" %% int(time.time() * 1000)
    start = """(() => {
      const req = %%s; const key = %%s;
      window.__johto = window.__johto || {}; window.__johto[key] = null;
      (async () => {
        try {
          const token = await window.Clerk.session.getToken();
          const headers = {Authorization: 'Bearer ' + token, 'x-campaign-id': req.campaign,
                           'x-company-id': req.company, 'x-account-id': req.account};
          const init = {method: req.method, headers};
          if (req.body !== null) { headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(req.body); }
          const r = await fetch(req.url, init);
          window.__johto[key] = JSON.stringify({status: r.status, body: await r.text(), version: r.headers.get('X-Task-Version')});
        } catch (e) {
          window.__johto[key] = JSON.stringify({status: 0, body: 'Studio request failed in the tab: ' + ((e && e.message) || e)});
        }
      })();
      return true;
    })()""" %% (json.dumps(REQ), json.dumps(KEY))
    js(start, target_id=T)
    poll = "(() => { const v = (window.__johto || {})[%%s]; if (v !== null && v !== undefined) delete window.__johto[%%s]; return v === undefined ? null : v; })()" %% (json.dumps(KEY), json.dumps(KEY))
    deadline = time.time() + TIMEOUT
    res = None
    while res is None and time.time() < deadline:
        res = js(poll, target_id=T)
        if res is None:
            time.sleep(0.25)
    if res is None:
        res = json.dumps({"status": 0, "body": "Studio did not answer within %%d seconds" %% TIMEOUT})
    print(%(mark)r + res)
'''


def _run_harness(script: str, timeout: float) -> str:
    out = subprocess.run(["browser-harness"], input=script, capture_output=True, text=True,
                         timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[-2000:] or "browser-harness failed")
    return out.stdout


def _restart_daemon() -> None:
    subprocess.run(["python3", "-c", "import admin; admin.restart_daemon()"],
                   cwd=str(__import__("pathlib").Path.home() / "dev/browser-harness"),
                   capture_output=True, timeout=60)


def request(method: str, path: str, body=None, query: dict | None = None, timeout: float = 180):
    # Real Studio only from the real checkout's data (or with JOHTO_REAL_STUDIO=1), never in fake mode.
    config.require_real(f"Studio {method} {path}")
    url = config.API + path
    if query:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
            {k: v for k, v in query.items() if v is not None})
    req = {"method": method, "url": url, "body": body, "campaign": config.CAMPAIGN_ID,
           "company": config.COMPANY_ID, "account": config.ACCOUNT_ID}
    start = f"{config.STUDIO}/annotator/views/{config.VIEWS['mine']}/?world_id={config.WORLD_ID}&campaignId={config.CAMPAIGN_ID}"
    script = _SCRIPT % {"req": json.dumps(req), "account": config.BROWSER_ACCOUNT,
                        "start": start, "mark": MARK, "timeout": max(15, int(timeout) - 45)}
    try:
        stdout = _run_harness(script, timeout)
    except RuntimeError as e:
        if "keepalive" in str(e) or "websocket" in str(e).lower() or "close frame" in str(e):
            _restart_daemon()
            stdout = _run_harness(script, timeout)
        else:
            raise
    line = next((ln for ln in stdout.splitlines() if ln.startswith(MARK)), None)
    if line is None:
        raise RuntimeError(f"No response from Studio tab. Output: {stdout[-1000:]}")
    res = json.loads(line[len(MARK):])
    status, text = res["status"], res["body"]
    try:
        data = json.loads(text) if text else None
    except json.JSONDecodeError:
        data = text
    if not (200 <= status < 300):
        raise StudioError(status, data, path)
    return data


def get(path, **query):
    return request("GET", path, query=query or None)


# --- Tasks -----------------------------------------------------------------

def list_tasks(view: str | None = None, status: str | None = None, page_size: int = 200,
               max_pages: int = 50) -> list[dict]:
    """All tasks in the Johto world, optionally narrowed to a writer view or status id."""
    out, page = [], 1
    filters = json.dumps([{"id": "task_status_id", "value": [status]}]) if status else None
    while page <= max_pages:
        data = get(f"/tasks/world/{config.WORLD_ID}/detailed", page=page, page_size=page_size,
                   custom_view_id=config.VIEWS.get(view, view) if view else None,
                   filters=filters)
        out.extend(data.get("tasks", []))
        pages = (data.get("pagination") or {}).get("total_pages") or 1
        if page >= pages:
            break
        page += 1
    return out


def task(task_id: str) -> dict:
    return get(f"/tasks/{task_id}")


def find_task(name_or_id: str) -> dict:
    """Accept 'Task ab12cd34', 'ab12cd34', or a task_ id."""
    s = name_or_id.strip()
    if s.startswith("task_"):
        return task(s)
    name = s if s.lower().startswith("task ") else f"Task {s}"
    data = get(f"/tasks/world/{config.WORLD_ID}/search", name_search=name, limit=10)
    for t in data.get("tasks", []):
        if t.get("task_name", "").lower() == name.lower():
            return task(t["task_id"])
    raise LookupError(f"No task named {name!r}")


def history(task_id: str):
    return get(f"/tasks/{task_id}/history")


def me() -> dict:
    return get("/users/me")


def eligible_transitions(task_id: str) -> list[dict]:
    return get(f"/tasks/{task_id}/eligible-transitions")


def transition(task_id: str, name: str) -> dict:
    edges = eligible_transitions(task_id)
    edge = next((e for e in edges if e["transition_name"] == name), None)
    if edge is None:
        names = [e["transition_name"] for e in edges]
        raise StudioError(409, f"'{name}' is not available now; available: {names}", task_id)
    return request("POST", f"/tasks/{task_id}/transition",
                   body={"edge_id": edge["edge_id"], "next_assignee_override": None})


def patch_fields(task_id: str, fields: dict) -> dict:
    """Merge custom_fields into the task, then re-read it to confirm the merge.

    Raises if a seeded slot field changed or a value didn't stick.
    """
    before = task(task_id).get("custom_fields") or {}
    request("PATCH", f"/tasks/{task_id}", body={"custom_fields": fields})
    after = task(task_id).get("custom_fields") or {}
    lost = [k for k in config.SLOT_FIELDS if before.get(k) != after.get(k)]
    if lost:
        raise StudioError(500, f"seeded fields changed after save: {lost}. Check the task in Studio.", task_id)
    missing = [k for k, v in fields.items() if str(after.get(k)).strip() != str(v).strip()]
    if missing:
        raise StudioError(500, f"these fields did not save: {missing}", task_id)
    return after


# --- AutoQC ----------------------------------------------------------------

def run_qc(task_id: str, spec: str = "AutoQC") -> dict:
    return request("POST", "/qc-audits/", body={
        "subject_kind": "task", "subject_id": task_id, "source": "automatic",
        "is_batch": False, "is_admin": False, "qc_spec_id": config.QC_SPECS[spec]})


def qc_audits(task_id: str, status: str | None = None) -> list[dict]:
    data = get("/qc-audits/", subject_kind="task", subject_id=task_id, status=status, limit=500)
    return data.get("audits", data) if isinstance(data, dict) else data


def qc_audit(audit_id: str) -> dict:
    return get(f"/qc-audits/{audit_id}")


def wait_qc(audit_id: str, timeout: float = 900, every: float = 15) -> dict:
    end = time.time() + timeout
    while True:
        a = qc_audit(audit_id)
        if a.get("status") in ("completed", "failed", "cancelled") or time.time() > end:
            return a
        time.sleep(every)


def slot_of(t: dict) -> dict:
    cf = t.get("custom_fields") or {}
    dom = cf.get("field_domain") or []
    return {
        "action": cf.get("field_action"),
        "domain": ", ".join(dom) if isinstance(dom, list) else dom,
        "codebase_type": cf.get("field_codebase_type"),
        "prompt_style": cf.get("field_prompt_style"),
        "interaction_mode": cf.get("field_interaction_mode"),
    }


def status_name(t: dict) -> str:
    return (t.get("task_status_defn") or {}).get("status_name") or config.STATUS_NAME.get(
        t.get("task_status_id"), t.get("task_status_id", "?"))


def task_url(task_id: str) -> str:
    return f"{config.STUDIO}/annotator/tasks/{task_id}/?campaignId={config.CAMPAIGN_ID}"
