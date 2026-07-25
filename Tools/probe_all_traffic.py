"""
Ad hoc diagnostic: dumps every response URL (no filtering) seen while loading
and scrolling the home feed, plus the rendered HTML, so we can find where
shreddit actually gets post data from.
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = Path(__file__).resolve().parent.parent / "browser_profile"
OUT = Path(r"C:\Users\crcha\AppData\Local\Temp\claude\p--Sync-dfr\0393cb0d-20d6-48d2-8e7d-4074f2bf369e\scratchpad\probe_all")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    urls = []
    target_url = sys.argv[1] if len(sys.argv) > 1 else "https://www.reddit.com/new/"

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", lambda r: urls.append(f"{r.status} {r.request.method} {r.url}"))

        page.goto(target_url)
        page.wait_for_timeout(2500)
        for _ in range(6):
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(1200)

        (OUT / "urls.txt").write_text("\n".join(urls))
        (OUT / "document.html").write_text(page.content(), encoding="utf-8")

        print(f"Wrote {len(urls)} urls and document.html to {OUT}")
        context.close()


if __name__ == "__main__":
    main()
