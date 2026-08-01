"""
Opens the persistent Playwright browser profile used by the dedicated Reddit
downloader account.

Run this once to log into the dedicated account manually. The session is
saved into the app's data directory (see get_data_directory()) and reused by
the app going forward without needing to log in again.

Usage:
    .venv/Scripts/python.exe Tools/open_browser_profile.py
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from DownloaderForReddit.core.reddit_source import PROFILE_DIR


def main():
    PROFILE_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            no_viewport=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto("https://www.reddit.com")
        except Exception as e:
            # Never let a navigation failure close the window -- the whole point of this script
            # is to let the user see the browser's state, including when something is broken
            # (e.g. reddit rate-limiting/429). Print the error and fall through to keeping the
            # window open regardless, so it can be inspected/navigated manually.
            print(f"Navigation failed: {type(e).__name__}: {e}")
        print(
            "Log into the dedicated downloader account, then close the window to save the session."
        )
        page.wait_for_event("close", timeout=0)
        context.close()


if __name__ == "__main__":
    main()
