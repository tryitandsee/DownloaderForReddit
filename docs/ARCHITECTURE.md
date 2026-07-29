### Entry point & wiring (`main.py`)

Bootstraps two things before showing the window:
- **MessageReceiver** — runs in its own `QThread`, drains a global `Queue` and emits `text_output` / `non_text_output` signals consumed by the main window
- **DownloaderForRedditGUI** — the main `QMainWindow`

Global singletons (settings, database, message queue) are lazily initialised via `DownloaderForReddit/utils/injector.py` and accessed from anywhere with `injector.get_*()`.

### Download pipeline

A download session creates a `DownloadRunner` (`core/download_runner.py`) which is moved to a `QThread`. It orchestrates two worker threads:

1. **Extractor thread** — runs `ContentRunner` (`core/content_runner.py`), pulls from a `submission_queue`, calls extractors, and pushes `Content` IDs into `download_queue`
2. **Downloader thread** — runs `Downloader` (`core/download/downloader.py`), pulls from `download_queue`, submits to a `ThreadPoolExecutor` (default 4 threads)

Users and subreddits are one flow, not two: both are scraped by `BrowserRedditSource._collect_listing`/`_validate_and_collect_listing`, which scroll the listing until Reddit's pagination ceiling or a post older than the object's `date_last_download_utc` checkpoint, and both set that checkpoint (`DownloadRunner.get_user_submissions`/`get_subreddit_submissions`) only when the scan confirmed coverage rather than hitting the scroll safety cap. Ambient browsing is the one place the two still differ: both coverage signals there (the known-post streak and Reddit's end-of-feed marker) are profile-page-only, so a subreddit's checkpoint only ever advances from an explicit download. The one path that deliberately does not scroll is the home feed (`iter_home_feed` → `_collect`), which has no checkpoint and would otherwise run to the ceiling every time.

`submission_queue` uses `None` as a stop sentinel. There's no queue-level pause; a rate limit instead cancels the current `DownloadSession` outright (see Rate limiting, below).

### Rate limiting

`BrowserRedditSource` (`core/reddit_source.py`) registers a `context.on("response", ...)` listener alongside its existing follow-state listener, watching every response in the shared browser context for an HTTP 429. On the first 429 it sets a `threading.Event` and calls back into `DownloadRunner` (via `set_on_rate_limited`, mirroring `set_on_posts_found`), which emits a `rate_limited` `pyqtSignal` to hop onto its own thread. `DownloadRunner.handle_rate_limited` messages the user and cancels the open session via `stop_download(hard_stop=True)`.

Independently, every navigation-triggering method on `BrowserRedditSource` (`_collect`, `_collect_listing`, `_validate`, `_validate_and_collect_listing`, `_get_post_impl`, `_get_gallery_media_metadata_impl`) checks the event first and raises `RateLimitedError` instead of navigating, as does every iteration of `_scroll_and_collect` — a scroll can run dozens of lazy-load fetches and has to abort mid-scan, not just at its start -- this covers callers `DownloadRunner`'s own per-object loops don't gate, e.g. an extractor fetching gallery metadata from `ContentRunner`'s thread pool for an already-queued, ambient-triggered post. There's no auto-resume/cooldown timer -- the event is cleared at the top of the next user-initiated `start_batch`, so starting another download is the resume signal.

### Extractor system

`SubmissionHandler` (`core/submission_handler.py`) calls `assign_extractor(url)` which matches the URL against each extractor's `url_key` list. Each extractor inherits `BaseExtractor` and implements `extract_content()`. The result is one or more `Content` DB records.

Key extractors: `RedditUploadsExtractor` (i.redd.it, galleries), `RedditVideoExtractor` (v.redd.it), `RedgifExtractor`, `ImgurExtractor`.

### GUI

Built with PyQt5. UI layouts are auto-generated Python in `guiresources/*_auto.py` — **do not hand-edit these**; they are compiled from `.ui` files. The main window class `DownloaderForRedditGUI` (`gui/downloader_for_reddit_gui.py`) mixes in the generated `Ui_MainWindow`.

New menu items should be added programmatically in the main GUI class rather than via the auto-generated file, e.g.:
```python
action = QAction("My Item", self)
action.triggered.connect(self.my_handler)
self.help_menu.addAction(action)
```

The user and subreddit lists share one `QTabWidget` (`object_tab_widget`) in the main window's splitter rather than sitting side by side, and both are `QTableView`s backed by the same `RedditObjectListModel` (Name / Last Download / Last Checked / Expected, sortable by header click). The visible tab *is* the download target — the Download button downloads whichever list is showing, replacing the old Users/Subreddits radio buttons, and header-click sorting replaced the View menu's "Sort Lists By"/"Sort Order" submenus, so that menu is gone entirely. The third radio's mode (constrain users to the subreddit list) survives as `CONSTRAIN_USERS_TO_SUBREDDIT_LIST` in `gui/downloader_for_reddit_gui.py`, a module-level flag with no GUI, the same stub pattern as `download_runner.FORCE_DOWNLOAD`.

New dialogs: pure-Python `QWidget` or `QDialog` subclasses work fine without a `.ui` file. Use `show()` for non-modal, `exec_()` for modal.

### Messaging (GUI output)

Worker threads never touch the GUI directly. They call `Message.send_info(text)` (etc.) which puts a `Message` object onto the global queue. `MessageReceiver` drains it and emits one of three signals that the main window consumes: `text_output` (`handle_message`, log lines), `non_text_output` (`handle_progress`, progress-bar/counter pulses with no text), and `content_output` (`handle_content_found`, structured `MessageType.CONTENT_FOUND` events — see Ambient downloader below).

Log levels map to GUI visibility: `send_debug` is console-only unless debug output is enabled; `send_info` appears in the main output pane.

`Message`/`MessageReceiver` have no Qt dependency at the emission layer (`Message.send*` just puts onto a plain `queue.Queue`) — only the GUI's consumption of `MessageReceiver`'s signals is Qt-specific, so a future headless mode can drain the same queue directly.

### Ambient downloader

Ambient discovery is push-based, not polled: `core/reddit_source.py:BrowserRedditSource` injects a `MutationObserver` into every page in the persistent browser context (via `context.add_init_script()`, which covers every tab including ones the user opens manually — confirmed empirically) that watches for added `<shreddit-post>` elements and reports them to Python via a Playwright binding (`context.expose_function("__dfrPostsFound", ...)`), not a network request.

A real `fetch()` to a local HTTP server was tried first and rejected: reddit.com's own Content-Security-Policy `connect-src` is a strict allowlist with no localhost exception, so the browser blocks the request outright (confirmed empirically against the actual CSP header) — there's no permission prompt to grant, it's just blocked. A CDP binding call isn't a network request, so CSP doesn't apply to it.

That still leaves a separate, real constraint: Playwright's sync API only pumps incoming CDP protocol messages while a call on that thread is blocked/in-flight (confirmed empirically — a bound function's Python callback does not fire while the worker thread sits genuinely idle, e.g. `time.sleep`). `BrowserRedditSource` runs a dedicated pump thread (`_pump_loop`/`_pump_once`, never the Playwright worker thread itself) that keeps the worker thread perpetually occupied in short `page.wait_for_timeout(PUMP_INTERVAL_MS)` calls, specifically so `__dfrPostsFound` callbacks get delivered promptly instead of only at the next unrelated blocking call (confirmed empirically: callbacks arrive interleaved within one pump interval, not delayed to the end of a long-running wait). Each pump iteration is its own executor submission rather than one long-running task, so an explicit-download job queued on the same single-worker executor waits at most one pump interval (FIFO) instead of being starved indefinitely.

The binding callback (`BrowserRedditSource._handle_posts_found`) immediately hands off to a spawned thread rather than doing any DB work inline, since it's not documented whether it runs on the Playwright worker thread itself. That thread forwards parsed posts to whatever consumer registered via `set_on_posts_found` (the GUI's `handle_ambient_posts`, `gui/downloader_for_reddit_gui.py`). That consumer matches against tracked+download_enabled users/subreddits and emits `ambient_matches_found`, queued onto the same standing `DownloadRunner` as explicit downloads via `request_download.emit`. Both explicit downloads (`DownloadRunner.prepare_single_submission`) and ambient matches (`DownloadRunner.prepare_submission`) funnel through the same `prepare_submission` method, which reports one structured `Message.send_content_found` event per discovered post (new or already-downloaded) — shown live in the content feed panel embedded in the main window.

Explicit navigation (single-post fetch, full user/subreddit fetch, validation) reuses the *same* page ambient browsing uses — there is deliberately no separate dedicated page for explicit actions. That was tried and reverted: Chromium focuses newly-created tabs, so a second page for explicit actions kept visibly stealing tab focus and interrupting whatever the user was looking at, which defeats the entire point of ambient mode ("browse naturally, content gets pushed" — not "browsing gets interrupted by a silent extra tab"). Explicit navigation briefly takes over the one shared tab instead, matching this app's original, long-standing behavior — a much smaller cost than focus fighting on every explicit action. It reads posts via one batched `page.evaluate()` call per navigation (all `<shreddit-post>` attributes in a single round-trip) rather than per-post `get_attribute()` calls.

Because explicit navigation reuses the ambient page, its own primer push would otherwise get reported back in as an ambient "match" and double-queue the same posts. `BrowserRedditSource._suppressed_ambient()` is a context manager each explicit-navigation method wraps its `page.goto()`/read in; `_dispatch_posts_found` drops anything received while it's active. `_open_url_impl` (the "Open in Browser" GUI action) is deliberately *not* wrapped — it navigates the user's own view to a page they asked to look at, so a real ambient match there is correct, not a self-report to suppress.

**Known limitation**: there is no scroll/backfill — discovery only ever sees whatever Reddit's server renders on the initial page load, not deep history. A newly-tracked user/subreddit only picks up their most recent batch of posts, not everything back through older posts. TODO: revisit if deep backfill is needed again.

All Python-driven Playwright calls (explicit discovery, single-post fetch, gallery metadata) go through one single-worker `ThreadPoolExecutor` in `BrowserRedditSource`, since Playwright's sync API is thread-bound and this also serializes browser activity so it doesn't look like concurrent bot sessions. Ambient discovery does not use this executor at all — it never blocks on, or is blocked by, Playwright.

### Database

SQLAlchemy 1.3 with Alembic migrations. Models live in `database/models.py`. A scoped session helper is available via `injector.get_database_handler().get_scoped_session()` (use as a context manager). The SQLite file lives in the OS app-data directory.
