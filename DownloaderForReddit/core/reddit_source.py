"""
Reddit discovery via browser automation (Playwright), replacing PRAW after Reddit
disabled this app's client_id and locked app registration behind manual review.
"""

import html
import json
import logging
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from ..messaging.message import FollowStatePayload, Message

PROFILE_DIR = Path(__file__).resolve().parent.parent.parent / "browser_profile"
REDDIT_BASE_URL = "https://www.reddit.com"

# GraphQL operation fired when the dedicated account follows/unfollows a user by clicking
# reddit's own follow button -- see PLAN_follow_status_sync.md.
FOLLOW_STATE_OPERATION = "UpdateProfileFollowState"

# The request's accountId (a t2_ fullname) doesn't tell us a username, and RedditObject stores
# no fullname -- so the target username is instead read off the page the click happened on. The
# follow button also exists elsewhere in the SPA (e.g. a "similar users" widget could appear
# alongside a profile and follow a *different* user than the page owner, in which case this
# regex would silently attribute the click to the wrong user) -- gating strictly to the target's
# own profile page keeps that mismatch out of scope rather than trying to detect it.
_PROFILE_URL_RE = re.compile(r"^https://www\.reddit\.com/user/([^/?]+)/?")

# Reddit's own lazy-load pagination fetch for a profile's post listing (fired by shreddit's
# infinite scroll as the user scrolls, confirmed empirically from a real capture). The response
# body embeds a trailing <faceplate-partial slot="load-after" ...> pointing at the next page's
# fetch URL as long as there's more content; once genuinely exhausted, that's replaced by a
# <span id="end-of-feed-tracker"> marker instead -- see _handle_profile_pagination_response.
_PROFILE_MORE_POSTS_RE = re.compile(
    r"^https://www\.reddit\.com/svc/shreddit/profiles/profile_posts-more-posts/"
)
_END_OF_FEED_MARKER = "end-of-feed-tracker"

# How long each pump iteration blocks the Playwright worker thread waiting for CDP messages.
# Bounds ambient-push latency (a post can wait up to this long for the pump to notice it) and
# the worst case an explicit-download task queued behind a pump iteration has to wait.
PUMP_INTERVAL_MS = 500

# How long to wait after each scroll for shreddit to lazy-load the next batch of posts, and how
# many scrolls to attempt before giving up (see BrowserRedditSource._scroll_and_collect). Reddit
# empirically stops loading more posts around 12 scrolls for a normal-sized history -- 42 is a
# safety cap well above that, not the expected normal stopping point.
_SCROLL_PAUSE_MS = 1500
_MAX_SCROLL_ITERATIONS = 42

logger = logging.getLogger(f"DownloaderForReddit.{__name__}")

# Injected once per document via context.add_init_script() -- runs on every page in the
# persistent context, including tabs the user opens manually (confirmed empirically; Playwright
# auto-attaches to every target in a persistent context regardless of who opened it). Defines
# window.__dfrExtractPosts (reused by explicit-navigation's page.evaluate() calls, so there's
# exactly one place that knows the <shreddit-post> attribute names) and reports newly-seen posts
# via the __dfrPostsFound binding (context.expose_binding, see
# BrowserRedditSource._handle_posts_found).
#
# A real network fetch() to a local server was tried first and rejected: reddit.com's own CSP
# connect-src is a strict allowlist with no localhost exception, so the browser blocks the
# request outright (confirmed empirically) -- there's no permission prompt to grant, unlike the
# unrelated Private Network Access gate. A CDP binding call isn't a network request at all, so
# CSP doesn't apply to it.
_INJECTED_SCRIPT = """
(() => {
    function extractPosts() {
        return Array.from(document.querySelectorAll('shreddit-post')).map((el) => ({
            id: el.getAttribute('id'),
            postType: el.getAttribute('post-type'),
            contentHref: el.getAttribute('content-href'),
            permalink: el.getAttribute('permalink'),
            postTitle: el.getAttribute('post-title'),
            domain: el.getAttribute('domain'),
            author: el.getAttribute('author'),
            subredditPrefixedName: el.getAttribute('subreddit-prefixed-name'),
            createdTimestamp: el.getAttribute('created-timestamp'),
            score: el.getAttribute('score'),
            nsfw: el.getAttribute('nsfw'),
            // Diagnostic only, not part of SubmissionData -- investigating whether an absent/empty
            // cursor on the last rendered post reliably signals "no more pages" for a profile
            // listing (see _dispatch_posts_found). Not yet used for anything functional.
            moreCursor: el.getAttribute('more-posts-cursor'),
        }));
    }
    window.__dfrExtractPosts = extractPosts;

    const seen = new Set();
    function pushNewPosts() {
        const fresh = extractPosts().filter((p) => p.id && !seen.has(p.id));
        if (fresh.length === 0) return;
        fresh.forEach((p) => seen.add(p.id));
        if (window.__dfrPostsFound) window.__dfrPostsFound(fresh);
    }

    let debounceHandle = null;
    function schedulePush() {
        if (debounceHandle) clearTimeout(debounceHandle);
        debounceHandle = setTimeout(pushNewPosts, 300);
    }

    function start() {
        pushNewPosts();
        new MutationObserver(schedulePush).observe(document.body, {childList: true, subtree: true});
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
"""

# Falls back to an empty list if evaluated before the init script has installed
# window.__dfrExtractPosts -- shouldn't happen after a real navigation, but a fresh page() call
# could race it.
_EXTRACT_POSTS_EXPR = "window.__dfrExtractPosts ? window.__dfrExtractPosts() : []"


class RateLimitedError(Exception):
    """Raised instead of navigating once reddit has returned an HTTP 429 -- see
    BrowserRedditSource._handle_response and _check_not_rate_limited."""


class ValidationError(Enum):
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    CONNECTION_ERROR = "connection_error"
    UNKNOWN = "unknown"


@dataclass
class ValidationResult:
    valid: bool
    error: ValidationError | None = None


@dataclass
class SubmissionData:
    reddit_id: str
    title: str
    url: str
    domain: str
    author: str
    subreddit: str
    created: datetime
    score: int
    nsfw: bool
    is_self: bool
    permalink: str
    post_type: (
        str  # raw shreddit post-type: image, text, link, video, gallery, crosspost
    )


class RedditSource(Protocol):
    def validate_user(self, name: str) -> ValidationResult: ...

    def validate_subreddit(self, name: str) -> ValidationResult: ...

    # since: if given, the scroll stops as soon as it reaches a post at or before this timestamp
    # (already covered by a prior confirmed scan) rather than scrolling to Reddit's own
    # pagination ceiling -- see BrowserRedditSource._scroll_and_collect. The returned bool is
    # whether coverage was confirmed (False only if the scroll safety cap was hit).
    def iter_user_submissions(
        self, name: str, since: datetime | None = None
    ) -> tuple[list[SubmissionData], bool]: ...

    def iter_subreddit_submissions(
        self, name: str, since: datetime | None = None
    ) -> tuple[list[SubmissionData], bool]: ...

    def iter_home_feed(self) -> list[SubmissionData]: ...  # following-only aggregation

    # Combined single-navigation validate + collect, for the initial fetch of a single user/subreddit
    # -- avoids a separate profile-page visit before the submitted/new-listing visit.
    def validate_and_iter_user_submissions(
        self, name: str, since: datetime | None = None
    ) -> tuple[ValidationResult, list[SubmissionData], bool]: ...

    def validate_and_iter_subreddit_submissions(
        self, name: str, since: datetime | None = None
    ) -> tuple[ValidationResult, list[SubmissionData], bool]: ...

    def get_post(self, url: str) -> SubmissionData | None: ...

    def open_url(self, url: str) -> None: ...


def _to_naive_utc(value: datetime) -> datetime:
    """SQLite silently drops tzinfo on write (see RedditObject.date_last_download_utc), so
    anything compared against a value read back from that column must be naive UTC too."""
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _strip_fullname_prefix(fullname: str) -> str:
    return re.sub(r"^t\d+_", "", fullname)


def _strip_subreddit_prefix(prefixed_name: str) -> str:
    return re.sub(r"^(r/|u_)", "", prefixed_name)


def _normalize_reddit_url(url: str) -> str:
    """Rewrite any reddit domain variant (old./np./amp./m.reddit.com, bare reddit.com) to
    www.reddit.com, since only shreddit renders <shreddit-post>."""
    parts = urlsplit(url)
    if parts.netloc.endswith("reddit.com") and parts.netloc != "www.reddit.com":
        parts = parts._replace(netloc="www.reddit.com")
    return urlunsplit(parts)


def _parse_post(raw: dict) -> SubmissionData | None:
    raw_id = raw.get("id")
    if not raw_id:
        return None
    try:
        # content-href/permalink are relative for some post types (crossposts, self posts) --
        # urljoin leaves already-absolute URLs (i.redd.it, v.redd.it, outbound links) untouched.
        post_type = raw.get("postType") or ""
        reddit_id = _strip_fullname_prefix(raw_id)
        if post_type == "gallery":
            # content-href for a gallery is unreliable -- observed as the bare comments permalink
            # on a feed card but https://www.reddit.com/gallery/<id> when read from the permalink
            # page itself. Build the /gallery/<id> form directly so it's the same regardless of
            # which page the post was read from; RedditUploadsExtractor dispatches on this exact
            # pattern (its url_key includes 'reddit.com/gallery').
            url = f"{REDDIT_BASE_URL}/gallery/{reddit_id}"
        else:
            url = urljoin(REDDIT_BASE_URL, raw.get("contentHref") or "")
        permalink = urljoin(REDDIT_BASE_URL, raw.get("permalink") or "")
        return SubmissionData(
            reddit_id=reddit_id,
            title=raw.get("postTitle") or "",
            url=url,
            domain=raw.get("domain") or "",
            author=raw.get("author") or "",
            subreddit=_strip_subreddit_prefix(raw.get("subredditPrefixedName") or ""),
            created=datetime.fromisoformat(raw.get("createdTimestamp")),
            score=int(raw.get("score") or 0),
            nsfw=raw.get("nsfw") is not None,
            is_self=post_type == "text",
            permalink=permalink,
            post_type=post_type,
        )
    except TypeError, ValueError:
        logger.warning(
            "Failed to parse shreddit-post attributes", extra={"raw_id": raw_id}
        )
        return None


def parse_posts_payload(raw_posts: list[dict]) -> list[SubmissionData]:
    posts = []
    for raw in raw_posts:
        parsed = _parse_post(raw)
        if parsed is not None:
            posts.append(parsed)
    return posts


def _read_posts(page: Page) -> list[SubmissionData]:
    return parse_posts_payload(page.evaluate(_EXTRACT_POSTS_EXPR))


class BrowserRedditSource:
    """
    RedditSource backed by a single long-lived, persistent Playwright browser window
    logged into a dedicated downloader account. Discovery reads post data directly off
    <shreddit-post> element attributes (server-rendered, no network interception) rather
    than intercepting network responses.

    One page is shared by both explicit navigation (single-post fetch, full user/subreddit
    fetch) and ambient browsing -- there is deliberately no separate, dedicated page for
    explicit actions. An earlier version of this class tried that, and it was wrong: Chromium
    focuses newly-created tabs, so every explicit action (or any later recreation of that page)
    visibly stole focus and interrupted whatever the user was looking at -- exactly the
    disruption ambient mode exists to avoid. Explicit navigation still does briefly take over
    the one shared tab (matching this app's original, long-standing behavior), which is a much
    smaller cost than a second tab silently fighting for focus.

    Ambient discovery is pushed *to* the app by injected page JS calling a Playwright binding
    (context.expose_binding), which relies on the pump loop below to actually be delivered
    promptly (see PUMP_INTERVAL_MS). Because explicit navigation reuses the same page, its own
    results would otherwise get reported back in as an ambient "match" too (the injected
    script's primer fires on every navigation, not just casual browsing) -- _suppressed_ambient
    marks the window around an explicit navigation so _dispatch_posts_found can drop pushes that
    are just an explicit action seeing its own page.

    Historical backfill (scrolling to catch up a newly-tracked user/subreddit's older posts) is
    not implemented -- discovery only ever sees whatever Reddit's server renders on initial page
    load. TODO: revisit if deep backfill is needed again.

    Playwright's sync API is thread-bound: it can only be driven from the thread that started
    it, AND it only pumps incoming CDP protocol messages while a call on that thread is
    blocked/in-flight -- confirmed empirically that a bound JS function's Python callback does
    not fire while the worker thread sits genuinely idle (e.g. time.sleep). A dedicated pump
    thread (_pump_loop) keeps the worker thread perpetually occupied in short
    page.wait_for_timeout() calls specifically so those callbacks get delivered promptly instead
    of only at the next unrelated blocking call -- confirmed empirically to work even for a
    binding call originating from a different tab than the one being pumped. Each pump call is
    its own executor submission, not one long-running task, so an explicit-download job queued
    on the same single-worker executor waits at most one pump interval (FIFO ordering) rather
    than being starved indefinitely.
    """

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._playwright = None
        self._context = None
        self._page = None
        self._on_posts_found: (
            Callable[[list[SubmissionData], str | None], None] | None
        ) = None
        self._on_rate_limited: Callable[[], None] | None = None
        self._on_profile_exhausted: Callable[[str], None] | None = None
        self._rate_limited = threading.Event()
        # BrowserRedditSource.start() (called from injector.get_reddit_source()) navigates the
        # initial page and fires the injected script's primer scan before the GUI exists to
        # register a consumer via set_on_posts_found -- buffer anything that arrives before then
        # and flush it once a consumer registers, rather than silently dropping that first batch
        # on every app launch. Each buffered entry is one dispatch's (posts, page_owner) --
        # page_owner varies per push, so entries can't be merged into a single flushed call.
        self._pending_posts_lock = threading.Lock()
        self._pending_posts: list[tuple[list[SubmissionData], str | None]] = []
        self._suppress_ambient = threading.Event()
        self._pump_stop = threading.Event()
        self._pump_thread = None

    def set_on_posts_found(
        self, callback: Callable[[list[SubmissionData], str | None], None]
    ):
        """Registered by the GUI (or a future headless consumer) once it's ready to receive
        ambient matches -- set after construction since BrowserRedditSource is created before
        the GUI exists (see injector.get_reddit_source())."""
        with self._pending_posts_lock:
            self._on_posts_found = callback
            pending = self._pending_posts
            self._pending_posts = []
        for posts, page_owner in pending:
            callback(posts, page_owner)

    def set_on_rate_limited(self, callback: Callable[[], None]):
        """Registered by DownloadRunner to be notified the moment reddit returns a 429, so the
        active download session can be cancelled immediately rather than continuing to hammer a
        rate-limited endpoint. See _handle_response."""
        self._on_rate_limited = callback

    def set_on_profile_exhausted(self, callback: Callable[[str], None]):
        """Registered by the GUI to be notified when organic browsing has scrolled a tracked
        user's profile all the way to Reddit's own end-of-feed marker -- see
        _handle_profile_pagination_response. Complements the ambient known-post-streak check in
        DownloaderForRedditGUI._match_and_queue_ambient_posts (that one confirms early once
        browsing has scrolled *past* previously-covered content; this one is the only signal for
        a user whose total post count never reaches that streak length)."""
        self._on_profile_exhausted = callback

    def is_rate_limited(self) -> bool:
        return self._rate_limited.is_set()

    def clear_rate_limit(self):
        """Called at the start of a new user-initiated download batch -- there's no automatic
        cooldown/resume, the next download the user starts is the resume signal."""
        self._rate_limited.clear()

    def _check_not_rate_limited(self) -> None:
        if self._rate_limited.is_set():
            raise RateLimitedError("Reddit rate limit (429) reached")

    def _handle_response(self, response) -> None:
        # Fired for every response in the context (context.on("response", ...)), not just
        # navigations -- catches a 429 regardless of which call triggered it, including gallery
        # metadata fetches and ambient-triggered extraction that never go through DownloadRunner's
        # own per-object loop. Only care about the transition to rate-limited; once set, later 429s
        # are redundant until clear_rate_limit() runs.
        if response.status == 429 and not self._rate_limited.is_set():
            self._rate_limited.set()
            logger.warning(
                "Reddit rate limit (429) reached, pausing downloads",
                extra={"url": response.url},
            )
            if self._on_rate_limited is not None:
                self._on_rate_limited()

    def _handle_posts_found(self, source: dict, raw_posts: list[dict]) -> None:
        # Invoked via the __dfrPostsFound binding (expose_binding, not expose_function -- the
        # leading `source` dict identifies which page pushed this, needed because ambient covers
        # every tab in the persistent context, not just self._page). May run on the Playwright
        # worker thread's own call stack (Playwright's threading model here isn't documented well
        # enough to assume otherwise) -- hand off immediately rather than risk doing DB work on
        # that thread, exactly as if this were a genuinely separate thread already.
        url = source["page"].url
        threading.Thread(
            target=self._dispatch_posts_found, args=(url, raw_posts), daemon=True
        ).start()

    def _dispatch_posts_found(self, url: str, raw_posts: list[dict]) -> None:
        if self._suppress_ambient.is_set():
            return
        posts = parse_posts_payload(raw_posts or [])
        if not posts:
            return
        match = _PROFILE_URL_RE.match(url)
        page_owner = match.group(1) if match else None
        if page_owner:
            # Diagnostic only -- see extractPosts()'s moreCursor comment. Logging the last raw
            # post's cursor (rendering order, so "last" is whatever loaded most recently) to see
            # what value it holds once a profile page's infinite scroll genuinely runs out.
            logger.info(
                "Ambient profile batch cursor",
                extra={
                    "page_owner": page_owner,
                    "batch_size": len(raw_posts or []),
                    "last_more_cursor": (raw_posts or [{}])[-1].get("moreCursor"),
                },
            )
        with self._pending_posts_lock:
            if self._on_posts_found is None:
                self._pending_posts.append((posts, page_owner))
                return
            callback = self._on_posts_found
        callback(posts, page_owner)

    def _handle_follow_state_response(self, response) -> None:
        # Confirmed empirically (PLAN_follow_status_sync.md): reading the response body directly
        # in this handler works, no executor hop needed, and delivery is immediate with no pump
        # loop involved -- unlike __dfrPostsFound, this isn't routed through a binding at all.
        request = response.request
        if request.method != "POST":
            return
        # post_data decodes the raw body as strict utf-8 and raises on any request with a
        # non-utf8 body (unrelated background requests hit this) -- read the buffer and decode
        # leniently instead, same fix as Tools/probe_follow_response.py.
        buffer = request.post_data_buffer
        post_data = buffer.decode("utf-8", errors="replace") if buffer else ""
        if FOLLOW_STATE_OPERATION not in post_data:
            return
        try:
            state = json.loads(post_data)["variables"]["input"]["state"]
        except json.JSONDecodeError, KeyError, TypeError:
            logger.warning(
                "Failed to parse follow-state request body",
                extra={"post_data": post_data},
            )
            return
        if state not in ("FOLLOWED", "NONE"):
            logger.warning("Unrecognized follow-state value", extra={"state": state})
            return
        followed = state == "FOLLOWED"
        # Gate to the target's own profile page -- see _PROFILE_URL_RE's comment. Checked before
        # the expensive response.json() read since frame.url is a cheap, already-local read.
        match = _PROFILE_URL_RE.match(request.frame.url)
        if match is None:
            logger.debug(
                "Follow-state request seen outside a profile page, ignoring",
                extra={"frame_url": request.frame.url},
            )
            return
        username = match.group(1)
        try:
            body = response.json()
        except PlaywrightError:
            logger.warning("Failed to read follow-state response body", exc_info=True)
            return
        errors = body.get("errors")
        if errors:
            logger.warning(
                "Follow/unfollow request failed",
                extra={"username": username, "errors": errors},
            )
            return
        logger.debug(
            "Follow-state request succeeded",
            extra={"username": username, "followed": followed},
        )
        Message.send_follow_state_changed(
            FollowStatePayload(username=username, followed=followed)
        )

    def _handle_profile_pagination_response(self, response) -> None:
        if not _PROFILE_MORE_POSTS_RE.match(response.url):
            return
        match = _PROFILE_URL_RE.match(response.request.frame.url)
        if match is None:
            return
        page_owner = match.group(1)
        try:
            body = response.text()
        except PlaywrightError:
            logger.warning(
                "Failed to read profile pagination response body", exc_info=True
            )
            return
        end_of_feed = _END_OF_FEED_MARKER in body
        logger.info(
            "Ambient profile pagination response",
            extra={"page_owner": page_owner, "end_of_feed": end_of_feed},
        )
        if end_of_feed and self._on_profile_exhausted is not None:
            # Mirrors _handle_posts_found's own reasoning: hand off rather than risk DB work
            # directly on whatever thread this event fires on.
            threading.Thread(
                target=self._on_profile_exhausted, args=(page_owner,), daemon=True
            ).start()

    @contextmanager
    def _suppressed_ambient(self):
        # Explicit navigation reuses the ambient-facing page, so its own primer push must be
        # dropped, not treated as a discovered match -- see the class docstring.
        self._suppress_ambient.set()
        try:
            yield
        finally:
            self._suppress_ambient.clear()

    def start(self):
        self._executor.submit(self._start_impl).result()
        self._pump_thread = threading.Thread(target=self._pump_loop, daemon=True)
        self._pump_thread.start()

    def _start_impl(self):
        self._playwright = sync_playwright().start()
        self._launch_context()
        with self._suppressed_ambient():
            self._page.goto(REDDIT_BASE_URL)

    def _launch_context(self):
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            no_viewport=True,
        )
        # If the user closes the browser window, all pages close and the persistent context
        # closes with them -- null it out so the next call relaunches instead of raising into
        # an explicit download.
        self._context.on("close", lambda _: setattr(self, "_context", None))
        self._context.on("response", self._handle_follow_state_response)
        self._context.on("response", self._handle_response)
        self._context.on("response", self._handle_profile_pagination_response)
        self._context.expose_binding("__dfrPostsFound", self._handle_posts_found)
        self._context.add_init_script(_INJECTED_SCRIPT)
        self._page = (
            self._context.pages[0] if self._context.pages else self._context.new_page()
        )

    def _pump_loop(self):
        # Runs on its own OS thread (never the Playwright worker thread) for the app's
        # lifetime, resubmitting one short wait to the executor at a time so it never
        # monopolizes the worker -- see the class docstring.
        while not self._pump_stop.is_set():
            pumped = self._executor.submit(self._pump_once).result()
            if not pumped:
                # No context/page to wait on yet (e.g. still starting up, or the browser
                # window is closed and nothing has relaunched it) -- avoid a tight spin loop.
                self._pump_stop.wait(PUMP_INTERVAL_MS / 1000)

    def _pump_once(self) -> bool:
        if self._context is None or not self._context.pages:
            return False
        try:
            self._context.pages[0].wait_for_timeout(PUMP_INTERVAL_MS)
        except PlaywrightError:
            return False
        return True

    def stop(self):
        self._pump_stop.set()
        if self._pump_thread is not None:
            self._pump_thread.join()
        self._executor.submit(self._stop_impl).result()
        self._executor.shutdown(wait=True)

    def _stop_impl(self):
        if self._context is not None:
            self._context.close()
        if self._playwright is not None:
            self._playwright.stop()

    def _get_page(self) -> Page:
        if self._context is None:
            logger.info("Playwright browser window was closed, relaunching")
            self._launch_context()
        if self._page is None or self._page.is_closed():
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else self._context.new_page()
            )
        return self._page

    def _collect(self, url: str) -> list[SubmissionData]:
        self._check_not_rate_limited()
        page = self._get_page()
        with self._suppressed_ambient():
            page.goto(url)
            page.wait_for_timeout(2000)
            return _read_posts(page)

    def _scroll_and_collect(
        self, page: Page, since: datetime | None
    ) -> tuple[list[SubmissionData], bool]:
        """
        Scrolls an endless-scroll shreddit listing page (user and subreddit listings share the
        same shape and both come through here), collecting posts until either Reddit stops loading
        more (its own pagination ceiling) or a post at or before `since` is reached (already covered by a prior confirmed
        scan). `since` is naive UTC (see RedditObject.date_last_download_utc); posts' `created` is
        timezone-aware, so it's normalized to naive UTC before comparing. Returns the deduped posts
        seen and whether coverage was confirmed -- False only if the iteration safety cap was hit
        first, meaning the scan gave up rather than confirmed.
        """

        def reached_checkpoint(posts: list[SubmissionData]) -> bool:
            if since is None:
                return False
            return any(_to_naive_utc(post.created) <= since for post in posts)

        collected: dict[str, SubmissionData] = {
            post.reddit_id: post for post in _read_posts(page)
        }
        if reached_checkpoint(list(collected.values())):
            return list(collected.values()), True
        for _ in range(_MAX_SCROLL_ITERATIONS):
            self._check_not_rate_limited()
            page.mouse.wheel(0, 15000)
            page.wait_for_timeout(_SCROLL_PAUSE_MS)
            new_posts = [
                post for post in _read_posts(page) if post.reddit_id not in collected
            ]
            if not new_posts:
                return list(collected.values()), True
            for post in new_posts:
                collected[post.reddit_id] = post
            if reached_checkpoint(new_posts):
                return list(collected.values()), True
        return list(collected.values()), False

    def _collect_listing(
        self, url: str, since: datetime | None
    ) -> tuple[list[SubmissionData], bool]:
        self._check_not_rate_limited()
        page = self._get_page()
        with self._suppressed_ambient():
            page.goto(url)
            page.wait_for_timeout(2000)
            return self._scroll_and_collect(page, since)

    def iter_user_submissions(
        self, name: str, since: datetime | None = None
    ) -> tuple[list[SubmissionData], bool]:
        url = f"https://www.reddit.com/user/{name}/submitted/?sort=new"
        return self._executor.submit(self._collect_listing, url, since).result()

    def iter_subreddit_submissions(
        self, name: str, since: datetime | None = None
    ) -> tuple[list[SubmissionData], bool]:
        url = f"https://www.reddit.com/r/{name}/new/"
        return self._executor.submit(self._collect_listing, url, since).result()

    def iter_home_feed(self) -> list[SubmissionData]:
        return self._executor.submit(
            self._collect, "https://www.reddit.com/new/"
        ).result()

    def validate_user(self, name: str) -> ValidationResult:
        url = f"https://www.reddit.com/user/{name}/"
        return self._executor.submit(self._validate, url).result()

    def validate_subreddit(self, name: str) -> ValidationResult:
        url = f"https://www.reddit.com/r/{name}/"
        return self._executor.submit(self._validate, url).result()

    def _validate(self, url: str) -> ValidationResult:
        self._check_not_rate_limited()
        page = self._get_page()
        with self._suppressed_ambient():
            try:
                page.goto(url)
            except PlaywrightError:
                logger.warning(
                    "Navigation failed during validation",
                    extra={"url": url},
                    exc_info=True,
                )
                return ValidationResult(
                    valid=False, error=ValidationError.CONNECTION_ERROR
                )
            page.wait_for_timeout(1500)
            return self._check_validity(page)

    @staticmethod
    def _check_validity(page: Page) -> ValidationResult:
        # Best-effort: matches reddit's known 404/private-community copy. NOT_FOUND is confirmed
        # working against a real nonexistent user; FORBIDDEN (private/suspended) is still
        # unverified -- no real example inspected yet.
        body_text = page.locator("body").inner_text().lower()
        if (
            "nobody on reddit goes by that name" in body_text
            or "this user has deleted their account" in body_text
        ):
            return ValidationResult(valid=False, error=ValidationError.NOT_FOUND)
        if "community doesn’t exist" in body_text or "page not found" in body_text:  # noqa: RUF001 -- matches reddit's actual page copy, which uses a curly apostrophe
            return ValidationResult(valid=False, error=ValidationError.NOT_FOUND)
        if "this community is private" in body_text or "suspended" in body_text:
            return ValidationResult(valid=False, error=ValidationError.FORBIDDEN)
        return ValidationResult(valid=True)

    def validate_and_iter_user_submissions(
        self, name: str, since: datetime | None = None
    ) -> tuple[ValidationResult, list[SubmissionData], bool]:
        url = f"https://www.reddit.com/user/{name}/submitted/?sort=new"
        return self._executor.submit(
            self._validate_and_collect_listing, url, since
        ).result()

    def validate_and_iter_subreddit_submissions(
        self, name: str, since: datetime | None = None
    ) -> tuple[ValidationResult, list[SubmissionData], bool]:
        url = f"https://www.reddit.com/r/{name}/new/"
        return self._executor.submit(
            self._validate_and_collect_listing, url, since
        ).result()

    def _validate_and_collect_listing(
        self, url: str, since: datetime | None
    ) -> tuple[ValidationResult, list[SubmissionData], bool]:
        # A single navigation serves both validation and the submissions scrape -- the listing page
        # shows the same 404/private/suspended copy as the plain profile page, so there's no need to
        # visit the profile page first just to check it exists.
        self._check_not_rate_limited()
        page = self._get_page()
        with self._suppressed_ambient():
            try:
                page.goto(url)
            except PlaywrightError:
                logger.warning(
                    "Navigation failed during validation",
                    extra={"url": url},
                    exc_info=True,
                )
                return (
                    ValidationResult(
                        valid=False, error=ValidationError.CONNECTION_ERROR
                    ),
                    [],
                    False,
                )
            page.wait_for_timeout(2000)
            validation = self._check_validity(page)
            if not validation.valid:
                return validation, [], False
            posts, coverage_confirmed = self._scroll_and_collect(page, since)
            return validation, posts, coverage_confirmed

    def get_post(self, url: str) -> SubmissionData | None:
        return self._executor.submit(self._get_post_impl, url).result()

    def _get_post_impl(self, url: str) -> SubmissionData | None:
        # A real old.reddit.com URL reached this method and correctly found no <shreddit-post>
        # (old.reddit.com has no web components at all) -- confirming the assumption that only
        # www.reddit.com's permalink page renders it, same as the listing pages. Normalize the
        # domain before navigating.
        url = _normalize_reddit_url(url)
        self._check_not_rate_limited()
        page = self._get_page()
        with self._suppressed_ambient():
            try:
                page.goto(url)
            except PlaywrightError:
                logger.warning(
                    "Navigation failed fetching single post",
                    extra={"url": url},
                    exc_info=True,
                )
                return None
            page.wait_for_timeout(2000)
            posts = _read_posts(page)
            if not posts:
                logger.warning("No shreddit-post found at url", extra={"url": url})
                return None
            return posts[0]

    def get_gallery_media_metadata(self, permalink: str) -> dict:
        """
        Fetches a gallery post's media_metadata (original-resolution image URLs, same shape PRAW's
        media_metadata always had) via reddit's .json endpoint. A bare `requests` call to this
        endpoint gets a 403 -- confirmed empirically, wrong TLS/JA3 fingerprint, exactly the
        detection this whole browser-automation rewrite exists to avoid. Calling fetch() from
        inside the logged-in browser page instead works (confirmed) and is the one deliberate
        exception to "never call .json" elsewhere in this source: a single occasional per-gallery
        lookup, not a bulk discovery pattern that would look anomalous.
        Reference: https://github.com/mikf/gallery-dl/blob/master/gallery_dl/extractor/reddit.py
        """
        post = self._executor.submit(self._fetch_post_json, permalink).result()
        if post is None:
            return {}
        media_metadata = post.get("media_metadata") or {}
        # Values come back with HTML-entity-escaped URLs (e.g. "&amp;" for "&") -- PRAW always
        # unescaped these before code elsewhere ever saw them, so do the same here.
        for value in media_metadata.values():
            source = value.get("s")
            if isinstance(source, dict):
                for key in ("u", "gif", "mp4"):
                    if key in source:
                        source[key] = html.unescape(source[key])
        return media_metadata

    def get_mp4_preview_url(self, permalink: str) -> str | None:
        """
        [mine] Fetches a gif post's mp4 preview variant (Reddit always transcodes an uploaded
        gif to mp4) via the same .json endpoint used by get_gallery_media_metadata -- a
        browser-discovered SubmissionData has no PRAW-style `.preview` field to read this off
        of directly, so RedditUploadsExtractor.extract_direct_link asks for it here instead.
        """
        post = self._executor.submit(self._fetch_post_json, permalink).result()
        if post is None:
            return None
        try:
            url = post["preview"]["images"][0]["variants"]["mp4"]["source"]["url"]
        except KeyError, IndexError, TypeError:
            return None
        return html.unescape(url)

    def _fetch_post_json(self, permalink: str) -> dict | None:
        url = (
            _normalize_reddit_url(urljoin(REDDIT_BASE_URL, permalink)).rstrip("/")
            + ".json"
        )
        self._check_not_rate_limited()
        page = self._get_page()
        try:
            data = page.evaluate(
                "(url) => fetch(url).then(r => r.ok ? r.json() : null)", url
            )
        except PlaywrightError:
            logger.warning(
                "Navigation failed fetching post json",
                extra={"url": url},
                exc_info=True,
            )
            return None
        if not data:
            return None
        try:
            return data[0]["data"]["children"][0]["data"]
        except KeyError, IndexError, TypeError:
            logger.warning("Unexpected post json shape", extra={"url": url})
            return None

    def open_url(self, url: str) -> None:
        # [mine] feat(core): navigate the dedicated account's browser window to a url -- lets the
        # GUI open a user/subreddit profile directly (e.g. to click follow manually), rather than
        # just copying the link for a separate, non-logged-in browser.
        self._executor.submit(self._open_url_impl, url).result()

    def _open_url_impl(self, url: str) -> None:
        # Deliberately not wrapped in _suppressed_ambient -- unlike the background fetches
        # above, this navigates the user's own view to a page they asked to look at, so a real
        # ambient match there is exactly the behavior "browse naturally, get pushed" implies.
        page = self._get_page()
        try:
            page.goto(_normalize_reddit_url(url))
            page.bring_to_front()
        except PlaywrightError:
            logger.warning(
                "Navigation failed opening url", extra={"url": url}, exc_info=True
            )
