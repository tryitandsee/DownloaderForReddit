### Entry point & wiring (`main.py`)

Bootstraps three things before showing the window:
- **MessageReceiver** — runs in its own `QThread`, drains a global `Queue` and emits `text_output` / `non_text_output` signals consumed by the main window
- **Scheduler** — runs in its own `QThread`, fires `run_task` signals on a cron-like schedule
- **DownloaderForRedditGUI** — the main `QMainWindow`

Global singletons (settings, database, message queue) are lazily initialised via `DownloaderForReddit/utils/injector.py` and accessed from anywhere with `injector.get_*()`.

### Download pipeline

A download session creates a `DownloadRunner` (`core/download_runner.py`) which is moved to a `QThread`. It orchestrates two worker threads:

1. **Extractor thread** — runs `ContentRunner` (`core/content_runner.py`), pulls from a `submission_queue`, calls extractors, and pushes `Content` IDs into `download_queue`
2. **Downloader thread** — runs `Downloader` (`core/download/downloader.py`), pulls from `download_queue`, submits to a `ThreadPoolExecutor` (default 4 threads)

The two queues use sentinel values (`None` to stop, `'HOLD'`/`'RELEASE_HOLD'` to pause) rather than thread events.

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

New dialogs: pure-Python `QWidget` or `QDialog` subclasses work fine without a `.ui` file. Use `show()` for non-modal, `exec_()` for modal.

### Messaging (GUI output)

Worker threads never touch the GUI directly. They call `Message.send_info(text)` (etc.) which puts a `Message` object onto the global queue. `MessageReceiver` drains it and emits a signal that `handle_message()` on the main window consumes.

Log levels map to GUI visibility: `send_debug` is console-only unless debug output is enabled; `send_info` appears in the main output pane.

### Ambient downloader

Every 15s (`ambient_poll_timer` in `gui/downloader_for_reddit_gui.py`) a background thread reads whatever `<shreddit-post>` elements are currently on the page in the dedicated account's persistent Playwright browser window (`core/reddit_source.py:BrowserRedditSource.read_current_page_posts`) — no navigation, just whatever the user is casually browsing. Matches against tracked+download_enabled users/subreddits are emitted back to the GUI thread (`ambient_matches_found` signal) and queued onto the same standing `DownloadRunner` as explicit downloads via `request_download.emit`.

All Playwright calls (ambient poll, explicit discovery, single-post fetch) go through one single-worker `ThreadPoolExecutor` in `BrowserRedditSource`, since Playwright's sync API is thread-bound and this also serializes browser activity so it doesn't look like concurrent bot sessions.

### Database

SQLAlchemy 1.3 with Alembic migrations. Models live in `database/models.py`. A scoped session helper is available via `injector.get_database_handler().get_scoped_session()` (use as a context manager). The SQLite file lives in the OS app-data directory.
