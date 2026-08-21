"""
Reddit discovery via browser automation (Playwright)
"""

import html
import json
import logging
import re
import threading
import time
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
from ..utils import injector
from ..utils.system_util import get_data_directory
from . import const

PROFILE_DIR = Path(get_data_directory()) / "browser_profile"
REDDIT_BASE_URL = "https://www.reddit.com"

# GraphQL operation fired when the dedicated account follows/unfollows a user by clicking
# reddit's own follow button -- see PLAN_follow_status_sync.md.
FOLLOW_STATE_OPERATION = "UpdateProfileFollowState"

# A follow request's accountId is a t2_ fullname and RedditObject stores no fullname, so the
# username comes off the page instead. Gated strictly to the target's own profile page: the
# follow button also appears in SPA widgets that could follow someone other than the page owner.
_PROFILE_URL_RE = re.compile(r"^https://www\.reddit\.com/user/([^/?]+)/?")

# A listing's own proof that nothing more will load, read by __dfrFeedExhausted, _listing_ended,
# and _handle_profile_pagination_response. Only a marker's presence ever confirms coverage, never
# the absence of load-after: a false confirm permanently skips that user's backlog.
_END_OF_FEED_MARKER = "end-of-feed-tracker"
_EMPTY_FEED_MARKER = "empty-feed-content"
_END_OF_LISTING_SELECTOR = f"#{_END_OF_FEED_MARKER}, #{_EMPTY_FEED_MARKER}"
_END_OF_LISTING_EXPR = f"!!document.querySelector({_END_OF_LISTING_SELECTOR!r})"
_PROFILE_MORE_POSTS_RE = re.compile(
    r"^https://www\.reddit\.com/svc/shreddit/profiles/profile_posts-more-posts/"
)
# Not _PROFILE_URL_RE: the same markers render on overview/comments/upvoted, which say nothing
# about coverage of the posts listing.
_SUBMITTED_LISTING_URL_RE = re.compile(
    r"^https://www\.reddit\.com/user/([^/?]+)/submitted/"
)
_SUBREDDIT_NEW_LISTING_URL_RE = re.compile(r"^https://www\.reddit\.com/r/([^/?]+)/new/")

# Empty scrolls before a listing with no end-of-listing marker is treated as finished.
_MAX_CONSECUTIVE_EMPTY_SCROLLS = 2

logger = logging.getLogger(f"DownloaderForReddit.{__name__}")

# Injected via add_init_script(), so it runs on every page in the persistent context, including
# tabs the user opens manually. Reports posts over a CDP binding rather than fetch(): reddit's
# CSP connect-src has no localhost exception, but a binding call isn't a network request at all.
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
            nsfw: el.getAttribute('nsfw'),
            // Diagnostic only -- see _dispatch_posts_found.
            moreCursor: el.getAttribute('more-posts-cursor'),
        }));
    }
    window.__dfrExtractPosts = extractPosts;

    const seen = new Set();
    function isReady(p) {
        // content-href hydrates asynchronously; read too early it's indistinguishable from a
        // post that has none. Gallery urls are built from the id alone.
        return p.id && (p.postType === 'gallery' || p.contentHref);
    }
    function pushNewPosts() {
        const fresh = extractPosts().filter((p) => isReady(p) && !seen.has(p.id));
        if (fresh.length === 0) return;
        fresh.forEach((p) => seen.add(p.id));
        if (window.__dfrPostsFound) window.__dfrPostsFound(fresh);
    }

    function reportIfExhausted() {
        const marker = document.querySelector('__END_OF_LISTING_SELECTOR__');
        if (!marker) return;
        if (!window.__dfrFeedExhausted) return;
        // Deliberately no one-shot latch: Python may defer confirming coverage, and needs this
        // to re-report on the next mutation once the pending posts land.
        window.__dfrFeedExhausted(marker.id, extractPosts());
    }

    let debounceHandle = null;
    function scheduleScan() {
        if (debounceHandle) clearTimeout(debounceHandle);
        debounceHandle = setTimeout(scan, 300);
    }

    function scan() {
        pushNewPosts();
        reportIfExhausted();
    }

    function start() {
        scan();
        new MutationObserver(scheduleScan).observe(document.body, {
            childList: true,
            subtree: true,
            attributes: true,
            attributeFilter: ['content-href'],
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
""".replace("__END_OF_LISTING_SELECTOR__", _END_OF_LISTING_SELECTOR)

# Falls back to [] if evaluated before the init script installs __dfrExtractPosts -- a fresh
# page() call can race it.
_EXTRACT_POSTS_EXPR = "window.__dfrExtractPosts ? window.__dfrExtractPosts() : []"


class RateLimitedError(Exception):
    """Raised instead of navigating once reddit has returned an HTTP 429 -- see
    BrowserRedditSource._handle_response and _check_should_continue."""


class StopRequestedError(Exception):
    """Raised instead of navigating (or continuing a scroll) once a Stop/Terminate click has set
    DownloadRunner's stop_requested Event -- see set_stop_event and _check_should_continue.
    continue_run alone is only checked between objects and scrolls, never during the
    goto/wheel/wait call itself, which is where most of a download's wall-clock time goes."""


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
    nsfw: bool
    is_self: bool
    permalink: str
    post_type: (
        str  # raw shreddit post-type: image, text, link, video, gallery, crosspost
    )


class RedditSource(Protocol):
    def validate_user(self, name: str) -> ValidationResult: ...

    def validate_subreddit(self, name: str) -> ValidationResult: ...

    # `since` stops the scroll at the first post already covered by a prior confirmed scan. The
    # returned bool is whether coverage was confirmed (False only if the safety cap was hit).
    def iter_user_submissions(
        self, name: str, since: datetime | None = None
    ) -> tuple[list[SubmissionData], bool]: ...

    def iter_subreddit_submissions(
        self, name: str, since: datetime | None = None
    ) -> tuple[list[SubmissionData], bool]: ...

    # Combined validate + collect in one navigation, avoiding a separate profile-page visit.
    def validate_and_iter_user_submissions(
        self, name: str, since: datetime | None = None
    ) -> tuple[ValidationResult, list[SubmissionData], bool]: ...

    def validate_and_iter_subreddit_submissions(
        self, name: str, since: datetime | None = None
    ) -> tuple[ValidationResult, list[SubmissionData], bool]: ...

    def get_post(self, url: str) -> SubmissionData | None: ...

    def open_url(self, url: str) -> None: ...


def to_naive_utc(value: datetime) -> datetime:
    """SQLite silently drops tzinfo on write (see RedditObject.date_last_download_utc), so
    anything compared against a value read back from that column must be naive UTC too."""
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _strip_fullname_prefix(fullname: str) -> str:
    return re.sub(r"^t\d+_", "", fullname)


def _strip_subreddit_prefix(prefixed_name: str) -> str:
    return re.sub(r"^(r/|u_)", "", prefixed_name)


def classify_listing_url(url: str) -> tuple[str, str] | None:
    """Does this URL match a scroll-eligible submissions listing -- a user's `/submitted/` or a
    subreddit's `/new/`? Returns (name, object_type) if so, else None. Also used by the GUI to
    decide whether a *manually* navigated-to page should trigger a scan."""
    match = _SUBMITTED_LISTING_URL_RE.match(url)
    if match:
        return match.group(1), "USER"
    match = _SUBREDDIT_NEW_LISTING_URL_RE.match(url)
    if match:
        return match.group(1), "SUBREDDIT"
    return None


def _scroll_label(url: str) -> str:
    """Human-readable label for scroll-status feedback (e.g. "u/foo", "r/bar") -- falls back to
    the raw url if it's not a recognized listing shape."""
    classified = classify_listing_url(url)
    if classified is None:
        return url
    name, object_type = classified
    return f"{'u' if object_type == 'USER' else 'r'}/{name}"


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
        # content-href/permalink are relative for crossposts and self posts; urljoin leaves
        # already-absolute urls untouched.
        post_type = raw.get("postType") or ""
        reddit_id = _strip_fullname_prefix(raw_id)
        if post_type == "gallery":
            # A gallery's content-href differs depending on which page it was read from. Build
            # the form RedditUploadsExtractor's url_key dispatches on instead.
            url = f"{REDDIT_BASE_URL}/gallery/{reddit_id}"
        else:
            # Left unset rather than urljoin'd: an empty content-href would yield REDDIT_BASE_URL
            # itself, one fake url every such post shares and falsely dedupes against.
            content_href = raw.get("contentHref")
            url = urljoin(REDDIT_BASE_URL, content_href) if content_href else ""
        permalink = urljoin(REDDIT_BASE_URL, raw.get("permalink") or "")
        created_timestamp = raw.get("createdTimestamp")
        if created_timestamp is None:
            raise ValueError("missing createdTimestamp")
        return SubmissionData(
            reddit_id=reddit_id,
            title=raw.get("postTitle") or "",
            url=url,
            domain=raw.get("domain") or "",
            author=raw.get("author") or "",
            subreddit=_strip_subreddit_prefix(raw.get("subredditPrefixedName") or ""),
            created=datetime.fromisoformat(created_timestamp),
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


def _parse_post_json(data: dict) -> SubmissionData | None:
    reddit_id = data.get("id")
    if not reddit_id:
        return None
    try:
        is_gallery = bool(data.get("is_gallery"))
        crosspost = bool(data.get("crosspost_parent_list"))
        is_video = bool(data.get("is_video"))
        is_self = bool(data.get("is_self"))
        post_hint = data.get("post_hint") or ""

        if is_self:
            post_type = "text"
        elif is_gallery:
            post_type = "gallery"
        elif crosspost:
            post_type = "crosspost"
        elif is_video:
            post_type = "video"
        elif post_hint == "image":
            post_type = "image"
        else:
            post_type = "link"

        if post_type == "gallery":
            url = f"{REDDIT_BASE_URL}/gallery/{reddit_id}"
        else:
            url = data.get("url") or ""

        permalink = urljoin(REDDIT_BASE_URL, data.get("permalink") or "")
        created_utc = data.get("created_utc")
        if created_utc is None:
            raise ValueError("missing created_utc")
        return SubmissionData(
            reddit_id=reddit_id,
            title=data.get("title") or "",
            url=url,
            domain=data.get("domain") or "",
            author=data.get("author") or "",
            subreddit=data.get("subreddit") or "",
            created=datetime.fromtimestamp(created_utc, tz=UTC),
            nsfw=bool(data.get("over_18", False)),
            is_self=is_self,
            permalink=permalink,
            post_type=post_type,
        )
    except (TypeError, ValueError, KeyError):
        logger.warning("Failed to parse post json", extra={"reddit_id": reddit_id})
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


def _listing_ended(page: Page) -> bool:
    """The DOM-read half of the end-of-listing signal (the ambient half is __dfrFeedExhausted).

    Restricted to profile listings, the only pages the markers were verified against -- a marker
    turning up somewhere other than the true end would confirm coverage that was never scanned.
    Subreddits fall back to the empty-scroll stop."""
    if _SUBMITTED_LISTING_URL_RE.match(page.url) is None:
        return False
    return bool(page.evaluate(_END_OF_LISTING_EXPR))


class BrowserRedditSource:
    """
    RedditSource backed by a single long-lived, persistent Playwright browser window logged
    into a dedicated downloader account. Discovery reads post data off <shreddit-post> element
    attributes, not network responses.

    One page is shared by explicit navigation and ambient browsing -- deliberately no second
    page for explicit actions. An earlier version tried that: Chromium focuses newly-created
    tabs, so every explicit action stole focus, exactly the disruption ambient mode exists to
    avoid. Since explicit navigation reuses the page, its own primer push would come back as an
    ambient "match" -- _suppressed_ambient marks that window so _dispatch_posts_found drops it.

    Historical backfill (scrolling to catch up a newly-tracked object's older posts) is not
    implemented -- discovery only sees what Reddit renders on initial page load.

    Playwright's sync API is thread-bound, and only pumps incoming CDP messages while a call on
    that thread is blocked/in-flight -- a bound JS function's Python callback does not fire
    while the worker sits idle (confirmed empirically). _pump_loop keeps the worker occupied in
    short page.wait_for_timeout() calls so callbacks arrive promptly, one executor submission
    at a time so a queued explicit-download job waits at most one pump interval.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._playwright = None
        self._context = None
        self._page = None
        self._on_posts_found: (
            Callable[[list[SubmissionData], str | None, str], None] | None
        ) = None
        self._on_rate_limited: Callable[[], None] | None = None
        self._on_profile_exhausted: (
            Callable[[str, list[SubmissionData]], None] | None
        ) = None
        self._scroll_pacer: Callable[[], None] | None = None
        self._on_posts_collected: Callable[[list[SubmissionData]], None] | None = None
        self._all_already_known: Callable[[list[SubmissionData]], bool] | None = None
        # Guards every page.goto: a scan submits its scrolls one at a time, so another
        # navigation could otherwise goto the shared page out from under it. Acquired before
        # touching the executor, so a contended wait blocks the caller, not the worker.
        self._page_lock = threading.Lock()
        self._rate_limited = threading.Event()
        self._stop_requested: threading.Event | None = None
        # start() fires the injected script's primer scan before the GUI exists to register a
        # consumer, so that first batch is buffered instead of dropped on every app launch.
        self._pending_posts_lock = threading.Lock()
        self._pending_posts: list[tuple[list[SubmissionData], str | None, str]] = []
        self._suppress_ambient = threading.Event()
        # A bulk run paced minutes apart shouldn't keep stealing the tab into the foreground; a
        # single deliberate click should.
        self._suppress_bring_to_front = threading.Event()
        self._pump_stop = threading.Event()
        self._pump_thread = None
        self._user_agent: str | None = None

    def set_on_posts_found(
        self, callback: Callable[[list[SubmissionData], str | None, str], None]
    ):
        """Registered by the GUI once it's ready to receive ambient matches -- set after
        construction, since BrowserRedditSource is created before the GUI exists."""
        with self._pending_posts_lock:
            self._on_posts_found = callback
            pending = self._pending_posts
            self._pending_posts = []
        for posts, page_owner, url in pending:
            callback(posts, page_owner, url)

    def set_on_rate_limited(self, callback: Callable[[], None]):
        """Registered by DownloadRunner to cancel the active session the moment reddit returns
        a 429, rather than continuing to hammer a rate-limited endpoint. See _handle_response."""
        self._on_rate_limited = callback

    def set_stop_event(self, event: threading.Event):
        """Registered by DownloadRunner with its own stop_requested Event -- checked before
        every navigation and inside the scroll loop (_check_should_continue), so a Stop click
        aborts in-flight browsing instead of waiting for the gap between objects/scrolls."""
        self._stop_requested = event

    def set_scroll_pacer(self, callback: Callable[[], None]):
        """DownloadRunner's own _pace, reused before every scroll in _scroll_and_collect --
        ungapped wheel events look like a bot, and outrunning the download pipeline just builds
        an unbounded backlog. Reused rather than a second mechanism for within-object pacing."""
        self._scroll_pacer = callback

    def set_on_posts_collected(
        self, callback: Callable[[list[SubmissionData]], None] | None
    ):
        """Registered by DownloadRunner around a single explicit scan, not for the app's
        lifetime like set_scroll_pacer -- it closes over the reddit_object being downloaded.
        _scroll_and_collect calls it with each batch as the scroll progresses, so posts get
        queued immediately instead of after the whole scroll."""
        self._on_posts_collected = callback

    def set_all_known_checker(
        self, callback: Callable[[list[SubmissionData]], bool] | None
    ):
        self._all_already_known = callback

    def set_on_profile_exhausted(
        self, callback: Callable[[str, list[SubmissionData]], None]
    ):
        """Registered by the GUI for when organic browsing reaches the end of a tracked user's
        submitted listing -- either Reddit's end-of-feed marker in a pagination response
        (_handle_profile_pagination_response) or one in the initial DOM (_dispatch_feed_exhausted),
        the only signal a profile too short to scroll produces.

        Called with the posts currently rendered, not just the username: the marker only proves
        Reddit rendered nothing further, not that every rendered post has a Post row yet (race
        with __dfrPostsFound), so the callback must check DB coverage itself."""
        self._on_profile_exhausted = callback

    def is_rate_limited(self) -> bool:
        return self._rate_limited.is_set()

    def clear_rate_limit(self):
        """Called at the start of a new download batch -- there's no automatic cooldown/resume,
        the next download the user starts is the resume signal."""
        self._rate_limited.clear()

    def _check_should_continue(self) -> None:
        if self._rate_limited.is_set():
            raise RateLimitedError("Reddit rate limit (429) reached")
        if self._stop_requested is not None and self._stop_requested.is_set():
            raise StopRequestedError("Stop requested")

    def _handle_response(self, response) -> None:
        """Fired for every response in the context, not just navigations, so a 429 is caught
        whatever triggered it -- gallery metadata fetches and ambient-triggered extraction never
        go through DownloadRunner's per-object loop. Only the transition matters; later 429s are
        redundant until clear_rate_limit() runs."""
        if response.status == 429 and not self._rate_limited.is_set():
            self._rate_limited.set()
            logger.warning(
                "Reddit rate limit (429) reached, pausing downloads",
                extra={"url": response.url},
            )
            if self._on_rate_limited is not None:
                self._on_rate_limited()

    def _handle_posts_found(self, source: dict, raw_posts: list[dict]) -> None:
        """Invoked via the __dfrPostsFound binding. expose_binding, not expose_function, for the
        leading `source` dict: ambient covers every tab in the context, not just self._page.
        This may run on the Playwright worker thread's own call stack, so it only hands off --
        no DB work here."""
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
            # Diagnostic: does the last post's cursor go empty once infinite scroll runs out?
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
                self._pending_posts.append((posts, page_owner, url))
                return
            callback = self._on_posts_found
        callback(posts, page_owner, url)

    def _handle_feed_exhausted(
        self, source: dict, marker_id: str, raw_posts: list[dict]
    ) -> None:
        """Hand-off only, same threading reasoning as _handle_posts_found."""
        url = source["page"].url
        threading.Thread(
            target=self._dispatch_feed_exhausted,
            args=(url, marker_id, raw_posts),
            daemon=True,
        ).start()

    def _dispatch_feed_exhausted(
        self, url: str, marker_id: str, raw_posts: list[dict]
    ) -> None:
        if self._suppress_ambient.is_set():
            return
        match = _SUBMITTED_LISTING_URL_RE.match(url)
        if match is None:
            return
        page_owner = match.group(1)
        # Posts ride along with the marker rather than coming from a separate __dfrPostsFound
        # push: the two handlers spawn independent threads with no ordering guarantee, so a post
        # in this batch may have no Post row yet. The callback checks the DB itself.
        posts = parse_posts_payload(raw_posts or [])
        logger.info(
            "Ambient listing rendered an end-of-listing marker",
            extra={
                "page_owner": page_owner,
                "marker_id": marker_id,
                "rendered_posts": len(posts),
            },
        )
        if self._on_profile_exhausted is not None:
            self._on_profile_exhausted(page_owner, posts)

    def _handle_follow_state_response(self, response) -> None:
        """Not routed through a binding, so unlike __dfrPostsFound the body can be read right
        here -- no executor hop, no pump loop."""
        request = response.request
        if request.method != "POST":
            return
        # post_data decodes strict utf-8 and raises on the non-utf8 bodies background requests
        # send, so decode the buffer leniently instead.
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
        # Checked before the expensive response.json() read -- frame.url is already local.
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
            # Hand off rather than do DB work on whatever thread this fires on.
            threading.Thread(
                target=self._dispatch_profile_pagination_exhausted,
                args=(response.frame.page,),
                daemon=True,
            ).start()

    def _dispatch_profile_pagination_exhausted(self, page: Page) -> None:
        """Posts and owner are re-read from the page rather than carried over from the response:
        by the time this runs the page may have navigated elsewhere."""

        def read_current():
            return _read_posts(page), page.url

        try:
            posts, current_url = self._run(read_current)
        except PlaywrightError:
            logger.warning(
                "Failed to read rendered posts before confirming profile exhaustion",
                exc_info=True,
            )
            return
        match = _SUBMITTED_LISTING_URL_RE.match(current_url)
        # An empty read means a stale/wrong read, not an empty profile: a pagination response
        # only fires because more posts were loading.
        if match is None or not posts:
            return
        if self._on_profile_exhausted is not None:
            self._on_profile_exhausted(match.group(1), posts)

    @contextmanager
    def _suppressed_ambient(self):
        """Explicit navigation reuses the ambient-facing page, so its own primer push must be
        dropped rather than counted as a discovered match."""
        self._suppress_ambient.set()
        try:
            yield
        finally:
            self._suppress_ambient.clear()

    @contextmanager
    def suppress_bring_to_front(self):
        """Wrapped around run_paced_bulk_download's loop -- see _suppress_bring_to_front."""
        self._suppress_bring_to_front.set()
        try:
            yield
        finally:
            self._suppress_bring_to_front.clear()

    @contextmanager
    def _locked_page(self, operation: str, target: str):
        """Wraps every self._page_lock acquisition so a contended wait gets logged -- diagnostic
        for the unproven theory that two navigations raced for the shared page. If this never
        logs, the wrong-page symptom is coming from somewhere else."""
        start = time.monotonic()
        with self._page_lock:
            waited = time.monotonic() - start
            if waited > 0.5:
                logger.info(
                    "Page lock was contended",
                    extra={
                        "operation": operation,
                        "target": target,
                        "waited_seconds": round(waited, 1),
                    },
                )
            yield

    def _run(self, fn, *args):
        """Runs fn on the single Playwright-owning worker thread, blocking the caller for its
        result. Every Playwright touch must go through this (or an equivalent submit), never
        called directly from a method that might be running off the worker thread."""
        return self._executor.submit(fn, *args).result()

    @staticmethod
    def _pick_wait(normal_ms: int, slow_ms: int) -> int:
        """Checked live so slow mode can be toggled mid-session."""
        return slow_ms if injector.get_settings_manager().slow_mode else normal_ms

    def _goto_and_wait(self, page: Page, url: str) -> None:
        page.goto(url)
        # A deliberate single-object download should be visible while it runs.
        if not self._suppress_bring_to_front.is_set():
            page.bring_to_front()
        page.wait_for_timeout(const.GOTO_LISTING_WAIT_MS)
        # Diagnostic: if goto() didn't land where asked, everything downstream runs against the
        # wrong page silently. Host+path only, since reddit appends its own query strings.
        requested, actual = urlsplit(url), urlsplit(page.url)
        if (requested.netloc, requested.path.rstrip("/")) != (
            actual.netloc,
            actual.path.rstrip("/"),
        ):
            logger.warning(
                "Post-goto page.url doesn't match the requested navigation",
                extra={"requested_url": url, "actual_url": page.url},
            )

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
        # Closing the browser window closes the context -- null it out so the next call
        # relaunches instead of raising into an explicit download.
        self._context.on("close", lambda _: setattr(self, "_context", None))
        self._context.on("response", self._handle_follow_state_response)
        self._context.on("response", self._handle_response)
        self._context.on("response", self._handle_profile_pagination_response)
        self._context.expose_binding("__dfrPostsFound", self._handle_posts_found)
        self._context.expose_binding("__dfrFeedExhausted", self._handle_feed_exhausted)
        self._context.add_init_script(_INJECTED_SCRIPT)
        # Diagnostic: a persistent profile can restore several tabs, and pages[0] being the right
        # one to drive for the app's lifetime is an unverified assumption.
        if len(self._context.pages) > 1:
            logger.warning(
                "Persistent context restored more than one tab; using pages[0]",
                extra={"restored_urls": [p.url for p in self._context.pages]},
            )
        self._page = (
            self._context.pages[0] if self._context.pages else self._context.new_page()
        )
        self._user_agent = self._page.evaluate("navigator.userAgent")
        logger.info(
            "Browser context (re)launched", extra={"selected_tab_url": self._page.url}
        )

    def get_request_context(self) -> tuple[str | None, list[dict]]:
        """Lent to out-of-browser downloads (core/download/request_context.py). Deliberately
        avoids _get_page(): a download must never pop a Chromium window open to read cookies."""
        if self._context is None:
            return self._user_agent, []
        return self._run(self._get_request_context_impl)

    def _get_request_context_impl(self) -> tuple[str | None, list[dict]]:
        if self._context is None:
            return self._user_agent, []
        return self._user_agent, self._context.cookies()

    def _pump_loop(self):
        """Own OS thread, never the Playwright worker's -- resubmits one short wait at a time so
        it never monopolizes the worker. See the class docstring."""
        while not self._pump_stop.is_set():
            pumped = self._executor.submit(self._pump_once).result()
            if not pumped:
                # Nothing to wait on yet (starting up, or the window is closed) -- don't spin.
                self._pump_stop.wait(const.PUMP_INTERVAL_MS / 1000)

    def _pump_once(self) -> bool:
        if self._context is None or not self._context.pages:
            return False
        try:
            self._context.pages[0].wait_for_timeout(const.PUMP_INTERVAL_MS)
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
        assert self._context is not None
        if self._page is None or self._page.is_closed():
            if len(self._context.pages) > 1:
                logger.warning(
                    "Multiple tabs open when replacing a closed/missing page; using pages[0]",
                    extra={"open_urls": [p.url for p in self._context.pages]},
                )
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else self._context.new_page()
            )
            logger.info(
                "Replaced closed/missing page",
                extra={"selected_tab_url": self._page.url},
            )
        return self._page

    def _scroll_and_collect(
        self, page: Page, since: datetime | None
    ) -> tuple[list[SubmissionData], bool]:
        """
        Scrolls an endless-scroll shreddit listing (user and subreddit listings share a shape),
        collecting posts until Reddit stops loading more or a post at or before `since` is
        reached. `since` is naive UTC (RedditObject.date_last_download_utc) and posts' `created`
        is aware, so it's normalized first. Returns the deduped posts and whether coverage was
        confirmed -- False only if the iteration safety cap was hit, i.e. the scan gave up.

        The listing's own end-of-listing marker is the one positive proof nothing more will
        load, and short-circuits without a scroll for a one-page history. An empty batch is the
        fallback, but a slow lazy-load looks exactly like a finished one, so it takes
        _MAX_CONSECUTIVE_EMPTY_SCROLLS of them rather than the first.

        Each read/scroll is its own _run submission rather than one call holding the worker for
        the whole scan -- a scan takes minutes (paced, see set_scroll_pacer) and other queued
        work (an extractor's gallery media_metadata fetch) must be able to run in between. This
        method may run on any thread; it only touches `page` inside a closure handed to _run.
        """

        def reached_checkpoint(posts: list[SubmissionData]) -> bool:
            # all(), not any(): pinned and resurfaced posts mean render order isn't strictly
            # newest-first, so one old post can sit ahead of undiscovered ones. `not posts`
            # stops an empty batch from vacuously satisfying all() -- empty_scrolls handles it.
            if since is None or not posts:
                return False
            return all(to_naive_utc(post.created) <= since for post in posts)

        def read_state():
            return _read_posts(page), _listing_ended(page), page.url

        def scroll_and_read():
            page.mouse.wheel(0, 15000)
            page.wait_for_timeout(
                self._pick_wait(const.SCROLL_PAUSE_MS, const.SCROLL_PAUSE_MS_SLOW)
            )
            return _read_posts(page), _listing_ended(page), page.url

        initial_posts, ended, url = self._run(read_state)
        label = _scroll_label(url)
        collected: dict[str, SubmissionData] = {
            post.reddit_id: post for post in initial_posts
        }
        if collected and self._on_posts_collected is not None:
            self._on_posts_collected(list(collected.values()))
        if ended:
            Message.send_scroll_status(
                f"{label}: end-of-listing marker present, no scroll needed"
            )
            return list(collected.values()), True
        if reached_checkpoint(list(collected.values())):
            Message.send_scroll_status(f"{label}: already caught up, no scroll needed")
            return list(collected.values()), True
        if (
            collected
            and self._all_already_known is not None
            and self._all_already_known(list(collected.values()))
        ):
            Message.send_scroll_status(f"{label}: already caught up, no scroll needed")
            return list(collected.values()), True
        empty_scrolls = 0
        for idx in range(const.MAX_SCROLL_ITERATIONS):
            try:
                self._check_should_continue()
                if self._scroll_pacer is not None:
                    self._scroll_pacer()
                    # The pacer blocks on a queue drain -- a 429 or Stop can land during it.
                    self._check_should_continue()
            except (RateLimitedError, StopRequestedError) as e:
                Message.send_scroll_status(f"{label}: scroll stopped -- {e}")
                raise
            Message.send_scroll_status(
                f"{label}: scrolling ({idx + 1}/{const.MAX_SCROLL_ITERATIONS})..."
            )
            batch, ended, url = self._run(scroll_and_read)
            new_posts = [post for post in batch if post.reddit_id not in collected]
            for post in new_posts:
                collected[post.reddit_id] = post
            if new_posts and self._on_posts_collected is not None:
                self._on_posts_collected(new_posts)
            if ended:
                Message.send_scroll_status(
                    f"{label}: reached end-of-listing marker, stopping"
                )
                return list(collected.values()), True
            if (
                new_posts
                and self._all_already_known is not None
                and self._all_already_known(new_posts)
            ):
                Message.send_scroll_status(
                    f"{label}: this batch is all already-downloaded posts, stopping"
                )
                return list(collected.values()), True
            if reached_checkpoint(new_posts):
                Message.send_scroll_status(
                    f"{label}: reached previously-downloaded posts, stopping"
                )
                return list(collected.values()), True
            if new_posts:
                empty_scrolls = 0
                continue
            empty_scrolls += 1
            if empty_scrolls >= _MAX_CONSECUTIVE_EMPTY_SCROLLS:
                logger.info(
                    "Listing scan stopping without an end-of-listing marker",
                    extra={"url": url, "collected": len(collected)},
                )
                Message.send_scroll_status(
                    f"{label}: no new posts after {empty_scrolls} scrolls, stopping"
                )
                return list(collected.values()), True
        Message.send_scroll_status(f"{label}: hit scroll safety cap, giving up")
        return list(collected.values()), False

    @staticmethod
    def _same_listing(page: Page, url: str) -> bool:
        """Host+path only, since reddit appends its own query strings (matches the
        post-goto diagnostic in _goto_and_wait)."""
        requested, actual = urlsplit(url), urlsplit(page.url)
        return (requested.netloc, requested.path.rstrip("/")) == (
            actual.netloc,
            actual.path.rstrip("/"),
        )

    def _collect_listing(
        self, url: str, since: datetime | None
    ) -> tuple[list[SubmissionData], bool]:
        self._check_should_continue()
        page = self._run(self._get_page)
        with self._suppressed_ambient():
            if not self._run(self._same_listing, page, url):
                self._run(self._goto_and_wait, page, url)
            return self._scroll_and_collect(page, since)

    def iter_user_submissions(
        self, name: str, since: datetime | None = None
    ) -> tuple[list[SubmissionData], bool]:
        # Deliberately not wrapped in self._executor.submit: the inner steps submit themselves,
        # and one more submission around them would deadlock on the single worker. _page_lock,
        # not the executor, is what keeps another navigation out of the gaps.
        url = f"https://www.reddit.com/user/{name}/submitted/?sort=new"
        with self._locked_page("iter_user_submissions", name):
            return self._collect_listing(url, since)

    def iter_subreddit_submissions(
        self, name: str, since: datetime | None = None
    ) -> tuple[list[SubmissionData], bool]:
        url = f"https://www.reddit.com/r/{name}/new/"
        with self._locked_page("iter_subreddit_submissions", name):
            return self._collect_listing(url, since)

    def validate_user(self, name: str) -> ValidationResult:
        url = f"https://www.reddit.com/user/{name}/"
        with self._locked_page("validate_user", name):
            return self._executor.submit(self._validate, url).result()

    def validate_subreddit(self, name: str) -> ValidationResult:
        url = f"https://www.reddit.com/r/{name}/"
        with self._locked_page("validate_subreddit", name):
            return self._executor.submit(self._validate, url).result()

    def _validate(self, url: str) -> ValidationResult:
        self._check_should_continue()
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
            page.wait_for_timeout(const.VALIDATE_WAIT_MS)
            return self._check_validity(page)

    @staticmethod
    def _check_validity(page: Page) -> ValidationResult:
        """Matches reddit's page copy, confirmed against real examples of each case."""
        body_text = page.locator("body").inner_text().lower()
        if (
            "nobody on reddit goes by that name" in body_text
            or "this user has deleted their account" in body_text
        ):
            return ValidationResult(valid=False, error=ValidationError.NOT_FOUND)
        if "community doesn’t exist" in body_text or "page not found" in body_text:  # noqa: RUF001 -- matches reddit's actual page copy, which uses a curly apostrophe
            return ValidationResult(valid=False, error=ValidationError.NOT_FOUND)
        if (
            "this community is private" in body_text
            or "suspended" in body_text
            or "has been banned" in body_text
        ):
            return ValidationResult(valid=False, error=ValidationError.FORBIDDEN)
        return ValidationResult(valid=True)

    def validate_and_iter_user_submissions(
        self, name: str, since: datetime | None = None
    ) -> tuple[ValidationResult, list[SubmissionData], bool]:
        # Not wrapped in self._executor.submit -- see iter_user_submissions.
        url = f"https://www.reddit.com/user/{name}/submitted/?sort=new"
        with self._locked_page("validate_and_iter_user_submissions", name):
            return self._validate_and_collect_listing(url, since)

    def validate_and_iter_subreddit_submissions(
        self, name: str, since: datetime | None = None
    ) -> tuple[ValidationResult, list[SubmissionData], bool]:
        url = f"https://www.reddit.com/r/{name}/new/"
        with self._locked_page("validate_and_iter_subreddit_submissions", name):
            return self._validate_and_collect_listing(url, since)

    def _validate_and_collect_listing(
        self, url: str, since: datetime | None
    ) -> tuple[ValidationResult, list[SubmissionData], bool]:
        """One navigation serves both validation and the scrape: the listing page shows the same
        404/private/suspended copy as the profile page."""
        self._check_should_continue()
        page = self._run(self._get_page)
        with self._suppressed_ambient():
            try:
                if not self._run(self._same_listing, page, url):
                    self._run(self._goto_and_wait, page, url)
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
            validation = self._run(self._check_validity, page)
            if not validation.valid:
                return validation, [], False
            posts, coverage_confirmed = self._scroll_and_collect(page, since)
            return validation, posts, coverage_confirmed

    def get_post(self, url: str) -> SubmissionData | None:
        return self._executor.submit(self._get_post_impl, url).result()

    def _get_post_impl(self, url: str) -> SubmissionData | None:
        # _fetch_post_json accepts absolute URLs (urljoin passes them through unchanged).
        # Called directly — already running in the executor thread; re-submitting would deadlock.
        data = self._fetch_post_json(url)
        if data is None:
            return None
        return _parse_post_json(data)

    def get_gallery_media_metadata(self, permalink: str) -> dict:
        """
        Fetches a gallery post's media_metadata (original-resolution image URLs, PRAW's shape)
        via reddit's .json endpoint. A bare `requests` call gets a 403 -- wrong TLS/JA3
        fingerprint, exactly the detection this rewrite exists to avoid -- so it fetch()es from
        inside the logged-in page. The one deliberate exception to "never call .json": an
        occasional per-gallery lookup, not a bulk discovery pattern.
        Reference: https://github.com/mikf/gallery-dl/blob/master/gallery_dl/extractor/reddit.py
        """
        post = self._executor.submit(self._fetch_post_json, permalink).result()
        if post is None:
            return {}
        media_metadata = post.get("media_metadata") or {}
        # URLs come back HTML-entity-escaped; PRAW unescaped these before callers saw them.
        for value in media_metadata.values():
            source = value.get("s")
            if isinstance(source, dict):
                for key in ("u", "gif", "mp4"):
                    if key in source:
                        source[key] = html.unescape(source[key])
        return media_metadata

    def get_mp4_preview_url(self, permalink: str) -> str | None:
        """
        [mine] Fetches a gif post's mp4 preview variant (Reddit transcodes every uploaded gif)
        via get_gallery_media_metadata's .json endpoint -- a browser-discovered SubmissionData
        has no PRAW-style `.preview` field for RedditUploadsExtractor to read it off directly.
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
        self._check_should_continue()
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
        page.wait_for_timeout(const.SINGLE_POST_WAIT_MS)
        if not data:
            return None
        try:
            return data[0]["data"]["children"][0]["data"]
        except KeyError, IndexError, TypeError:
            logger.warning("Unexpected post json shape", extra={"url": url})
            return None

    def open_url(self, url: str) -> None:
        """[mine] Navigates to an arbitrary url the user clicked in the output log
        (hyperlink_delegate.py) -- possibly untracked, so it stays a plain unsuppressed nav."""
        with self._locked_page("open_url", url):
            self._executor.submit(self._open_url_impl, url).result()

    def _open_url_impl(self, url: str) -> None:
        """Deliberately not _suppressed_ambient: the user asked to look at this page, so an
        ambient match here is the intended behavior."""
        page = self._get_page()
        try:
            page.goto(_normalize_reddit_url(url))
            page.bring_to_front()
        except PlaywrightError:
            logger.warning(
                "Navigation failed opening url", extra={"url": url}, exc_info=True
            )
