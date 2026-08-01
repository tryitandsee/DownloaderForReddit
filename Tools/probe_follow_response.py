"""
Probe: can `UpdateProfileFollowState` GraphQL responses be read from inside a
sync-API `context.on("response", ...)` handler, and does that handler fire
promptly while the worker thread is genuinely idle?

Opens the same persistent browser profile the app uses -- close the real app
first, only one process can hold that profile's lock at a time. Manually
click follow/unfollow on a user's profile page once logged in; this script
logs everything it can read off the matching request/response.

Usage:
    .venv/Scripts/python.exe Tools/probe_follow_response.py
"""

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from DownloaderForReddit.core.reddit_source import PROFILE_DIR

OPERATION = "UpdateProfileFollowState"


def handle_response(response):
    request = response.request
    if request.method != "POST":
        return
    # request.post_data decodes the raw body as strict utf-8 and raises on any request with a
    # non-utf8 body (e.g. binary analytics beacons) -- read the buffer instead and decode
    # leniently so one unrelated request doesn't kill this listener for the rest of the session.
    buffer = request.post_data_buffer
    post_data = buffer.decode("utf-8", errors="replace") if buffer else ""
    if OPERATION not in post_data:
        return
    print(f"\n--- {OPERATION} response seen at {time.strftime('%X')} ---")
    print("request.post_data:", post_data)
    print("response.url:", response.url)
    print("response.status:", response.status)
    print("request.frame.url:", request.frame.url)
    try:
        body = response.json()
        print("response.json():", json.dumps(body, indent=2))
    except Exception as e:
        print(f"response.json() FAILED: {type(e).__name__}: {e}")


def main():
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            no_viewport=True,
        )
        context.on("response", handle_response)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto("https://www.reddit.com")
        except Exception as e:
            print(f"Navigation failed: {type(e).__name__}: {e}")

        print(
            "Log in if needed, then manually click follow/unfollow on a profile page.\n"
            "Watching for idle-dispatch: this script does NOT pump the worker thread with\n"
            "repeated wait_for_timeout() calls the way the real app's BrowserRedditSource does --\n"
            "if the response is only logged after you interact with the page again (not\n"
            "immediately on click), that confirms response events need the same pump-loop\n"
            "treatment as the __dfrPostsFound binding.\n"
            "Close the window when done."
        )
        page.wait_for_event("close", timeout=0)
        try:
            context.close()
        except Exception as e:
            # The window is already closed by this point (that's what we just waited for) --
            # closing an already-gone context raises internally on some Playwright versions.
            # Harmless for a one-shot probe script; just don't let it end in a scary traceback.
            print(
                f"context.close() raised (harmless, already closed): {type(e).__name__}: {e}"
            )


if __name__ == "__main__":
    main()
