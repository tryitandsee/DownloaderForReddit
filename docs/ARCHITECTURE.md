### Entry point & wiring (`main.py`)

Bootstraps two things before showing the window:
- **MessageReceiver** — runs in its own `QThread`, drains a global `Queue` and emits `text_output` / `non_text_output` signals consumed by the main window
- **DownloaderForRedditGUI** — the main `QMainWindow`

Global singletons (settings, database, message queue) are lazily initialised via `DownloaderForReddit/utils/injector.py` and accessed from anywhere with `injector.get_*()`.

### Download pipeline

A download session creates a `DownloadRunner` (`core/download_runner.py`) which is moved to a `QThread`. It orchestrates two worker threads:

1. **Extractor thread** — runs `ContentRunner` (`core/content_runner.py`), pulls from a `submission_queue`, calls extractors, and pushes `Content` IDs into `download_queue`
2. **Downloader thread** — runs `Downloader` (`core/download/downloader.py`), pulls from `download_queue`, submits to a `ThreadPoolExecutor` (default 4 threads)

Users and subreddits share one scrape path: `BrowserRedditSource._collect_listing`/`_validate_and_collect_listing` scroll the listing until it renders its own end-of-listing marker or a post older than the object's `date_last_download_utc` checkpoint is reached. That checkpoint only advances when the scan confirmed coverage, not when it hit the scroll safety cap. Ambient browsing's coverage signals (see Ambient downloader) are profile-page-only, so a subreddit's checkpoint only ever advances from an explicit download.

Both methods skip navigating if the page is already on that listing (`_same_listing`), so a scan cut short by a 429 resumes scrolling in place instead of reloading to the top.

An explicit scan queues each batch of posts as the scroll finds them rather than waiting for the whole listing: `_scroll_and_collect` calls back into `queue_submissions`/`_finalize_submission` per batch via `set_on_posts_collected`. Each read/scroll is submitted individually to `BrowserRedditSource`'s single-worker executor (`_run`), pacing between them (`set_scroll_pacer`) — this keeps the worker free for other queued Playwright work (e.g. gallery metadata fetches) instead of holding it for the whole scan.

`submission_queue` uses `None` as a stop sentinel. There's no queue-level pause; a rate limit cancels the current `DownloadSession` outright (see Rate limiting).

A bulk "Download" of the user/subreddit list (`DownloadRunner.run_paced_bulk_download`) visits objects one at a time in table sort order, capped at `const.BULK_DOWNLOAD_LIMIT` (20) per run, skipping objects whose checkpoint is within `const.BULK_DOWNLOAD_COOLDOWN_HOURS`, paced `const.BULK_DOWNLOAD_PACE_SECONDS` apart. An explicit multi-select download bypasses both the cap and the cooldown. The same pacer runs between scrolls within one object.

Stop is responsive mid-navigation, not just between objects/scrolls: `DownloadRunner.continue_run`/`stop_requested` is a `threading.Event` shared directly with `BrowserRedditSource` (`set_stop_event`). `_check_should_continue` raises `StopRequestedError` from every navigation-triggering method and every scroll iteration, mirroring the rate-limit check, and is caught silently at the same call sites.

### Rate limiting

`BrowserRedditSource` watches every response in the shared browser context for an HTTP 429 (`context.on("response", ...)`). On the first 429 it sets a `threading.Event` and calls back into `DownloadRunner` (`set_on_rate_limited`), which messages the user and cancels the session (`stop_download(hard_stop=True)`). Independently, every navigation-triggering method and scroll iteration checks that event first and raises `RateLimitedError` instead of navigating. There's no auto-resume — the event is cleared at the top of the next `start_batch`.

### Outbound HTTP identity

Downloads and extractor fetches use `requests`, outside the browser. `core/download/request_context.py`'s `request_args(url, referer)` returns `headers`/`cookies` kwargs lending them the browser's user agent and cookies, snapshotted from `BrowserRedditSource.get_request_context()` and TTL-cached (reading it occupies the single Playwright worker). It uses `injector.peek_reddit_source()` so a download never launches a browser, and never raises — `Downloader.download`'s bare `except` would misreport that as a content error.

Cookies go in a domain-scoped `RequestsCookieJar` so reddit's aren't sent to imgur. Playwright's `expires=-1` session cookies are translated to `None`, which `http.cookiejar` would otherwise drop as expired.

`HEADERS` entries (redgifs auth) are layered on top and win.

### Download retries

Three independent layers, none with any backoff:

1. **Transport** — none. Every call is a bare `requests.get`, so `HTTPAdapter(max_retries=0)`. No `Retry` is mounted anywhere; a status code would never trigger one regardless.
2. **Within-call** — multipart only. `MultipartDownloader.download_part` retries a chunk up to 3 times on any non-206, 404 included. The single-file path has no loop.
3. **Across sessions** — `Content.retry_attempts`, incremented by `set_download_error`. `DownloadRunner.run_undownloaded_content` requeues undownloaded content whose error isn't in `errors.NON_DOWNLOADABLE` and whose `retry_attempts <= 3`.

`Downloader.handle_unsuccessful_response` maps 404/410 to `DOES_NOT_EXIST` and 403 to `FORBIDDEN`, both `NON_DOWNLOADABLE`, so those get one attempt. Everything else stays `UNSUCCESSFUL_RESPONSE` and is retried — including 429, which burns all 4 attempts against a rate limit. Rows predating that mapping still carry `UNSUCCESSFUL_RESPONSE` with a 404 message.

### Extractor system

`SubmissionHandler` (`core/submission_handler.py`) calls `assign_extractor(url)` which matches the URL against each extractor's `url_key` list. Each extractor inherits `BaseExtractor` and implements `extract_content()`. The result is one or more `Content` DB records.

Key extractors: `RedditUploadsExtractor` (i.redd.it, galleries), `RedditVideoExtractor` (v.redd.it), `RedgifExtractor`, `ImgurExtractor`.

`SubmissionHandler.extract_comments()` is a no-op stub — comment downloading needs a `BrowserRedditSource`-based implementation, e.g. scraping a `/user/<name>/comments/?sort=new` page the way submission listings are scraped. `SubmittableCreator`/`CommentExtractor` take plain values and are unaffected.

### GUI

Built with PyQt5. UI layouts are auto-generated Python in `guiresources/*_auto.py` — **do not hand-edit these**; they are compiled from `.ui` files. The main window class `DownloaderForRedditGUI` (`gui/downloader_for_reddit_gui.py`) mixes in the generated `Ui_MainWindow`.

New menu items should be added programmatically in the main GUI class rather than via the auto-generated file, e.g.:
```python
action = QAction("My Item", self)
action.triggered.connect(self.my_handler)
self.help_menu.addAction(action)
```

The user and subreddit lists share one `QTabWidget` (`object_tab_widget`), both `QTableView`s backed by the same `RedditObjectListModel` (Name / Last Download / Last Checked / Expected, sortable by header click). The visible tab is the download target. `CONSTRAIN_USERS_TO_SUBREDDIT_LIST` in `gui/downloader_for_reddit_gui.py` is a module-level flag with no GUI, same pattern as `download_runner.FORCE_DOWNLOAD`.

New dialogs: pure-Python `QWidget` or `QDialog` subclasses work fine without a `.ui` file. Use `show()` for non-modal, `exec_()` for modal.

### Messaging (GUI output)

Worker threads never touch the GUI directly. They call `Message.send_info(text)` (etc.) which puts a `Message` object onto the global queue. `MessageReceiver` drains it and emits one of three signals: `text_output` (`handle_message`, log lines), `non_text_output` (`handle_progress`, progress pulses with no text), and `content_output` (`handle_content_found`, `MessageType.CONTENT_FOUND`/`CONTENT_SKIPPED`/`SCROLL_STATUS` — see Ambient downloader). `SCROLL_STATUS` carries `_scroll_and_collect`'s scroll progress and stop reason, shown in the content feed panel.

Log levels map to GUI visibility: `send_debug` is console-only unless debug output is enabled; `send_info` appears in the main output pane.

### Screenshot mode

`utils/anonymizer.py` holds a process-wide `Anonymizer` toggled by the View menu's "Screenshot Mode" action. Display-layer only — nothing it produces is ever written to the database or log. Tracked objects get a `user_<id>`/`sub_<id>` alias; an untracked author/subreddit still redacts structurally (`/r/<x>/`, `/user/<x>/` → `sub_?`/`user_?`); a post permalink collapses to `reddit.com/comments/<id>`. Anchor `href`s are left intact — only visible link text is redacted.

Toggling re-renders existing output. `RedditObjectListModel`/`OutputViewModel` rebuild text in `data()` and emit `dataChanged`. `ContentFeedPanel` rows keep their unredacted source in `RAW_TEXT_ROLE` and re-derive from that instead.

### Ambient downloader

Ambient discovery is push-based: `BrowserRedditSource` injects a `MutationObserver` into every page in the persistent browser context (`context.add_init_script()`) that watches for added `<shreddit-post>` elements and reports them via a Playwright binding (`__dfrPostsFound`), not a network request — reddit.com's CSP blocks a local `fetch()` outright.

Playwright's sync API only pumps CDP messages while a call is blocked/in-flight, so `BrowserRedditSource` runs a dedicated pump thread (`_pump_loop`) that keeps the worker thread occupied in short `page.wait_for_timeout` calls, so binding callbacks arrive promptly. Each pump iteration is its own executor submission, so a queued explicit-download task waits at most one pump interval.

Every path that turns a scraped submission into download work — ambient matches, explicit single-post downloads, bulk scans — ends at `DownloadRunner._finalize_submission`, which dedupes and reports one `Message.send_content_found` event.

Explicit navigation reuses the same page ambient browsing uses (no second tab — a second page steals Chromium's tab focus). `BrowserRedditSource._suppressed_ambient()` wraps explicit navigations so their own primer push isn't double-counted as an ambient match. Double-clicking a tracked object, or a person manually navigating onto a tracked listing (`classify_listing_url` + `scroll_eligible_navigation`), both trigger a real scan the same way — deduped per-url (`_last_scroll_trigger_url`) so repeated scroll batches on one page load don't re-trigger it.

**Known gap**: a manual-nav trigger always scans through `DownloadRunner`'s single shared page, not the specific tab that fired it — a second tab open on a different tracked listing would hijack the first tab instead.

Ambient browsing confirms coverage of a user (advancing `date_last_download_utc`) through three profile-page-only signals, all in `handle_profile_exhausted`/`_match_and_queue_ambient_posts`:
1. a run of `_AMBIENT_KNOWN_STREAK_THRESHOLD` consecutive already-known posts
2. Reddit's `end-of-feed-tracker` marker in a pagination response
3. an end-of-listing marker in the listing's initial DOM (`__dfrFeedExhausted`)

Signal 3 exists because a short history never scrolls, so signals 1–2 are unreachable for it. Gated to `/user/<name>/submitted/` specifically — the same markers render on other profile tabs.

Signals 2 and 3 only prove Reddit rendered nothing further, not that every rendered post has a Post row yet -- their dispatch and `_dispatch_posts_found` run on independent daemon threads with no ordering guarantee. So both pass their own freshly-read posts into `handle_profile_exhausted`, which stamps `date_last_download_utc` only once every one of them is already known; otherwise it defers. Signal 3's marker report has no one-shot latch, so a later DOM mutation (e.g. the pending download landing) lets it retry within the same page load; failing that, an explicit scan confirms coverage instead.

**Known limitation**: no scroll/backfill on ambient discovery — it only sees whatever Reddit renders on initial load, so a newly-tracked object only picks up its most recent batch.

All Python-driven Playwright calls go through one single-worker `ThreadPoolExecutor` in `BrowserRedditSource` (thread-bound sync API, and serializes browser activity). Ambient discovery doesn't use this executor — it never blocks on Playwright.

### Database

SQLAlchemy 1.3 with Alembic migrations. Models live in `database/models.py`. A scoped session helper is available via `injector.get_database_handler().get_scoped_session()` (use as a context manager). The SQLite file lives in the OS app-data directory.

### Settings

`persistence/settings_manager.py`'s `SettingsManager` (a lazily-initialized `injector` singleton) reads/writes a TOML file (`config.toml`, also in the OS app-data directory), organized into `[section]` blocks (e.g. `main_window_gui`, `core`). Each setting is loaded once in `__init__` via `self.get(section, key, default)` and exposed as a plain attribute; callers read/write the attribute directly and `save_all()` persists everything back to disk (called from the main window's `close()`).
