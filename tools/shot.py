"""Screenshot the running `johto web` app in headless Chromium (Playwright).

Usage:
  .venv/bin/python tools/shot.py OUT.png "/?demo=1&more=1#item=decide:d3m0aaa1:calib-1" [--w 1440 --h 900]
      [--press "Meta+k"] [--click ".row:nth-child(3)"] [--wait 1200] [--full]

The path's query sets demo cookies (demo=1, more=1, lane=waiting|working|free|needs_login).
Reads the auth token from data/web-url.txt, so the server must be running.
"""
import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("path")
    ap.add_argument("--w", type=int, default=1440)
    ap.add_argument("--h", type=int, default=900)
    ap.add_argument("--press", action="append", default=[])
    ap.add_argument("--click", action="append", default=[])
    ap.add_argument("--wait", type=int, default=1500)
    ap.add_argument("--full", action="store_true")
    a = ap.parse_args()
    url = (ROOT / "data" / "web-url.txt").read_text().strip()
    base, token = url.split("?t=")
    path, _, frag = a.path.partition("#")
    sep = "&" if "?" in path else "?"
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": a.w, "height": a.h}, device_scale_factor=2)
        logs = []
        pg.on("console", lambda m: logs.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        pg.on("pageerror", lambda e: logs.append(f"pageerror: {e}"))
        pg.goto(f"{base.rstrip('/')}{path}{sep}t={token}" + (f"#{frag}" if frag else ""), wait_until="load")
        pg.wait_for_timeout(a.wait)
        for sel in a.click:
            pg.click(sel)
            pg.wait_for_timeout(400)
        for k in a.press:
            pg.keyboard.press(k)
            pg.wait_for_timeout(400)
        pg.screenshot(path=a.out, full_page=a.full)
        b.close()
    for l in logs:
        print(l, file=sys.stderr)
    print(a.out)


if __name__ == "__main__":
    main()
