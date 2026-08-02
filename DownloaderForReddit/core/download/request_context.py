"""
Lends `requests` calls the identity of the Playwright browser the person is logged in with --
otherwise they arrive as `python-requests/x.y` with no cookies and no referer, which plenty of
hosts refuse. The cookie jar is domain-scoped, not a flat name/value dict, so reddit session
cookies aren't handed to imgur just for sharing a browser profile.
"""

import logging
import threading
import time
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from requests.cookies import RequestsCookieJar

from DownloaderForReddit.utils import injector

logger = logging.getLogger(__name__)

REDDIT_REFERER = "https://www.reddit.com/"
EROME_REFERER = "https://www.erome.com/"
SNAPSHOT_TTL_SECONDS = 300


class BrowserSnapshot(NamedTuple):
    user_agent: str | None
    cookies: RequestsCookieJar


EMPTY_SNAPSHOT = BrowserSnapshot(None, RequestsCookieJar())

_snapshot_lock = threading.Lock()
_snapshot = EMPTY_SNAPSHOT
_snapshot_time: float | None = None


def build_cookie_jar(cookies: list[dict]) -> RequestsCookieJar:
    jar = RequestsCookieJar()
    for cookie in cookies:
        expires = cookie.get("expires")
        jar.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain", ""),
            path=cookie.get("path", "/"),
            # Playwright's -1 (session cookie, which is what reddit's auth cookies are) would
            # read to cookiejar as long-expired and be dropped.
            expires=None if expires is None or expires < 0 else expires,
            secure=cookie.get("secure", False),
        )
    return jar


def get_snapshot() -> BrowserSnapshot:
    global _snapshot, _snapshot_time
    with _snapshot_lock:
        if (
            _snapshot_time is not None
            and time.monotonic() - _snapshot_time < SNAPSHOT_TTL_SECONDS
        ):
            return _snapshot
        source = injector.peek_reddit_source()
        if source is None:
            return _snapshot
        try:
            user_agent, cookies = source.get_request_context()
        except Exception:
            # Downloader.download()'s bare except would misreport this as a content error.
            logger.debug("Could not read the browser's request context", exc_info=True)
            return _snapshot
        _snapshot = BrowserSnapshot(user_agent, build_cookie_jar(cookies))
        _snapshot_time = time.monotonic()
        return _snapshot


def content_referer(content) -> str | None:
    """The page a browser would have been on when it fetched this content -- the post's own url
    when the media was extracted from one (an imgur album), the host otherwise."""
    host = urlsplit(content.url).netloc.lower()
    if host.endswith((".redd.it", "reddit.com")):
        return REDDIT_REFERER
    if "erome." in host:
        return EROME_REFERER
    post = getattr(content, "post", None)
    if post is not None and post.url:
        return post.url
    return origin_referer(content.url)


def origin_referer(url: str) -> str | None:
    split = urlsplit(url)
    if not split.scheme or not split.netloc:
        return None
    return f"{split.scheme}://{split.netloc}/"


def request_args(url: str, referer: str | None = None) -> dict[str, Any]:
    """Keyword arguments for a `requests` call that make it look like it came from the browser."""
    snapshot = get_snapshot()
    headers = {}
    if snapshot.user_agent:
        headers["User-Agent"] = snapshot.user_agent
    if referer:
        headers["Referer"] = referer
    return {"headers": headers, "cookies": snapshot.cookies}
