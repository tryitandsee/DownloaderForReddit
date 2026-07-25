"""
Opens the persistent Playwright browser profile used by the dedicated Reddit
downloader account (see PLAN_reddit_source_rewrite.md).

Run this once to log into the dedicated account manually. The session is
saved into browser_profile/ (gitignored) and reused by the app going forward
without needing to log in again.

Usage:
    .venv/Scripts/python.exe Tools/open_browser_profile.py
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = Path(__file__).resolve().parent.parent / "browser_profile"


def main():
    PROFILE_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            no_viewport=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.reddit.com")
        print("Log into the dedicated downloader account, then close the window to save the session.")
        page.wait_for_event("close", timeout=0)
        context.close()


if __name__ == "__main__":
    main()
