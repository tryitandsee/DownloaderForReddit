"""
Phase 0 probe (see PLAN_reddit_source_rewrite.md): opens the dedicated
account's home feed, a multireddit, and a user profile in the persistent
browser profile, scrolls each, and dumps every intercepted svc/gql response
body to disk for inspection.

This does not decide Gate A / Gate B itself -- it only collects the raw
payloads. Inspect the dumped JSON afterwards to check:

  Gate A: does each content type (image / gallery / video / outbound link)
          carry the fields the extractors need, not just a preview?
  Gate B: does the home feed contain every post from a followed user,
          compared against that user's own /user/<name> feed?

Usage:
    .venv/Scripts/python.exe Tools/probe_reddit_feeds.py \
        --user <followed-username> --multi <multireddit-name> \
        --owner <dedicated-account-username> --out <dir>
"""

import argparse
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = Path(__file__).resolve().parent.parent / "browser_profile"
INTERESTING = re.compile(r"/svc/|graphql|/gql", re.IGNORECASE)


def make_capture(out_dir: Path, label: str, counter: dict):
    def capture(response):
        if not INTERESTING.search(response.url):
            return
        try:
            body = response.json()
        except Exception:
            return
        counter["n"] += 1
        path = out_dir / f"{label}_{counter['n']:03d}.json"
        path.write_text(json.dumps({"url": response.url, "body": body}, indent=2))
        print(f"  captured {response.url} -> {path.name}")

    return capture


def scroll(page, times=6, pause_ms=1200):
    for _ in range(times):
        page.mouse.wheel(0, 2000)
        page.wait_for_timeout(pause_ms)


def probe(page, out_dir, label, url, scrolls=6):
    print(f"[{label}] navigating to {url}")
    counter = {"n": 0}
    handler = make_capture(out_dir, label, counter)
    page.on("response", handler)
    page.goto(url)
    page.wait_for_timeout(2000)
    scroll(page, times=scrolls)
    page.remove_listener("response", handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True, help="a followed username to probe /user/<name> and Gate B")
    parser.add_argument("--multi", required=True, help="multireddit name (without /m/ prefix)")
    parser.add_argument("--owner", required=True, help="dedicated account username that owns the multireddit")
    parser.add_argument("--out", required=True, help="output directory for captured JSON")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()

        probe(page, out_dir, "home", "https://www.reddit.com/", scrolls=8)
        probe(page, out_dir, "multi", f"https://www.reddit.com/user/{args.owner}/m/{args.multi}/", scrolls=8)
        probe(page, out_dir, "user", f"https://www.reddit.com/user/{args.user}/", scrolls=8)

        print(f"\nDone. Captured payloads written to {out_dir}")
        context.close()


if __name__ == "__main__":
    main()
