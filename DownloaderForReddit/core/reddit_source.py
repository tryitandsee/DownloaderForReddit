"""
Reddit discovery via browser automation (Playwright), replacing PRAW after Reddit
disabled this app's client_id and locked app registration behind manual review.
See PLAN_reddit_source_rewrite.md for the full design and the Phase 0/1 findings
this implementation is based on.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import List, Optional, Protocol, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError, Locator, Page, sync_playwright

PROFILE_DIR = Path(__file__).resolve().parent.parent.parent / 'browser_profile'
REDDIT_BASE_URL = 'https://www.reddit.com'

logger = logging.getLogger(f'DownloaderForReddit.{__name__}')


class ValidationError(Enum):
    NOT_FOUND = 'not_found'
    FORBIDDEN = 'forbidden'
    RATE_LIMITED = 'rate_limited'
    CONNECTION_ERROR = 'connection_error'
    UNKNOWN = 'unknown'


@dataclass
class ValidationResult:
    valid: bool
    error: Optional[ValidationError] = None


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
    post_type: str  # raw shreddit post-type: image, text, link, video, gallery, crosspost


class RedditSource(Protocol):

    def validate_user(self, name: str) -> ValidationResult: ...

    def validate_subreddit(self, name: str) -> ValidationResult: ...

    def iter_user_submissions(self, name: str) -> List[SubmissionData]: ...

    def iter_subreddit_submissions(self, name: str) -> List[SubmissionData]: ...

    def iter_home_feed(self) -> List[SubmissionData]: ...  # following-only aggregation

    # Combined single-navigation validate + collect, for the initial fetch of a single user/subreddit
    # -- avoids a separate profile-page visit before the submitted/new-listing visit.
    def validate_and_iter_user_submissions(self, name: str) -> Tuple[ValidationResult, List[SubmissionData]]: ...

    def validate_and_iter_subreddit_submissions(self, name: str) -> Tuple[ValidationResult, List[SubmissionData]]: ...

    def get_post(self, url: str) -> Optional[SubmissionData]: ...


def _strip_fullname_prefix(fullname: str) -> str:
    return re.sub(r'^t\d+_', '', fullname)


def _strip_subreddit_prefix(prefixed_name: str) -> str:
    return re.sub(r'^(r/|u_)', '', prefixed_name)


def _normalize_reddit_url(url: str) -> str:
    # User-supplied post URLs may point at any reddit domain variant (old./np./amp./m.reddit.com,
    # or bare reddit.com) -- only the shreddit frontend at www.reddit.com renders <shreddit-post>;
    # old.reddit.com in particular is the legacy plain-HTML UI and has no custom elements at all.
    parts = urlsplit(url)
    if parts.netloc.endswith('reddit.com') and parts.netloc != 'www.reddit.com':
        parts = parts._replace(netloc='www.reddit.com')
    return urlunsplit(parts)


def _parse_post(post: Locator) -> Optional[SubmissionData]:
    raw_id = post.get_attribute('id')
    if not raw_id:
        return None
    try:
        # content-href/permalink are relative for some post types (crossposts, self posts) --
        # urljoin leaves already-absolute URLs (i.redd.it, v.redd.it, outbound links) untouched.
        url = urljoin(REDDIT_BASE_URL, post.get_attribute('content-href') or '')
        permalink = urljoin(REDDIT_BASE_URL, post.get_attribute('permalink') or '')
        return SubmissionData(
            reddit_id=_strip_fullname_prefix(raw_id),
            title=post.get_attribute('post-title') or '',
            url=url,
            domain=post.get_attribute('domain') or '',
            author=post.get_attribute('author') or '',
            subreddit=_strip_subreddit_prefix(post.get_attribute('subreddit-prefixed-name') or ''),
            created=datetime.fromisoformat(post.get_attribute('created-timestamp')),
            score=int(post.get_attribute('score') or 0),
            nsfw=post.get_attribute('nsfw') is not None,
            is_self=post.get_attribute('post-type') == 'text',
            permalink=permalink,
            post_type=post.get_attribute('post-type') or '',
        )
    except (TypeError, ValueError):
        logger.warning('Failed to parse shreddit-post attributes', extra={'raw_id': raw_id})
        return None


class BrowserRedditSource:
    """
    RedditSource backed by a single long-lived, persistent Playwright browser window
    logged into a dedicated downloader account. Discovery reads post data directly off
    <shreddit-post> element attributes (server-rendered, no network interception) rather
    than intercepting network responses -- see PLAN_reddit_source_rewrite.md "Actual
    mechanism".

    Playwright's sync API is thread-bound: it can only be driven from the thread that
    started it. DownloadRunner runs on a QThread, NameChecker runs on its own thread, and
    the GUI closes from the main thread -- so a plain threading.Lock is not enough (that
    only serializes access, it doesn't relocate the caller onto the right thread). All
    Playwright work is therefore submitted to a dedicated single-worker executor, which
    both pins every call to one real OS thread and serializes discovery for free (no two
    scroll sessions ever run at once -- intended, not a limitation: parallel scroll
    sessions would read as bot activity).
    """

    SCROLL_PASSES = 10
    SCROLL_PAUSE_MS = 1000
    KNOWN_POST_STOP_THRESHOLD = 10

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._playwright = None
        self._context = None

    def start(self):
        self._executor.submit(self._start_impl).result()

    def _start_impl(self):
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            no_viewport=True,
        )
        self._page().goto(REDDIT_BASE_URL)

    def stop(self):
        self._executor.submit(self._stop_impl).result()
        self._executor.shutdown(wait=True)

    def _stop_impl(self):
        if self._context is not None:
            self._context.close()
        if self._playwright is not None:
            self._playwright.stop()

    def _page(self) -> Page:
        return self._context.pages[0] if self._context.pages else self._context.new_page()

    def _collect(self, url: str, limit: Optional[int] = None, known_ids: Optional[set] = None) -> List[SubmissionData]:
        page = self._page()
        page.goto(url)
        page.wait_for_timeout(2000)
        return self._scroll_and_collect(page, url, limit, known_ids)

    def _scroll_and_collect(self, page: Page, url: str, limit: Optional[int] = None,
                            known_ids: Optional[set] = None) -> List[SubmissionData]:
        seen = set()
        results = []
        consecutive_known = 0
        for scroll_pass in range(self.SCROLL_PASSES):
            for post in page.locator('shreddit-post').all():
                data = _parse_post(post)
                if data is not None and data.reddit_id not in seen:
                    seen.add(data.reddit_id)
                    results.append(data)
                    # Posts are sorted by "new", so once we've seen a CONSECUTIVE run of
                    # already-downloaded posts we've caught up with the last run -- stop scrolling
                    # instead of always doing all SCROLL_PASSES regardless of content. Must be
                    # consecutive, not cumulative: known and new posts can be interspersed (e.g.
                    # crossposts/reposts sorting slightly out of strict chronological order), and a
                    # cumulative count would stop before reaching a genuinely new post sitting past
                    # scattered known ones.
                    if known_ids is not None and data.reddit_id in known_ids:
                        consecutive_known += 1
                        if consecutive_known >= self.KNOWN_POST_STOP_THRESHOLD:
                            logger.debug('Caught up with already-downloaded posts, stopping scroll', extra={
                                'url': url, 'scroll_pass': scroll_pass + 1, 'collected': len(results),
                            })
                            return results
                    else:
                        consecutive_known = 0
                    if limit is not None and len(results) >= limit:
                        logger.debug('Reached post limit, stopping scroll', extra={
                            'url': url, 'scroll_pass': scroll_pass + 1, 'collected': len(results),
                        })
                        return results
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(self.SCROLL_PAUSE_MS)
        logger.debug('Reached SCROLL_PASSES limit without catching up or hitting post limit', extra={
            'url': url, 'scroll_passes': self.SCROLL_PASSES, 'collected': len(results),
        })
        return results

    def iter_user_submissions(self, name: str, limit: Optional[int] = None,
                              known_ids: Optional[set] = None) -> List[SubmissionData]:
        url = f'https://www.reddit.com/user/{name}/submitted/?sort=new'
        return self._executor.submit(self._collect, url, limit, known_ids).result()

    def iter_subreddit_submissions(self, name: str, limit: Optional[int] = None,
                                   known_ids: Optional[set] = None) -> List[SubmissionData]:
        url = f'https://www.reddit.com/r/{name}/new/'
        return self._executor.submit(self._collect, url, limit, known_ids).result()

    def iter_home_feed(self, limit: Optional[int] = None) -> List[SubmissionData]:
        return self._executor.submit(self._collect, 'https://www.reddit.com/new/', limit).result()

    def validate_user(self, name: str) -> ValidationResult:
        url = f'https://www.reddit.com/user/{name}/'
        return self._executor.submit(self._validate, url).result()

    def validate_subreddit(self, name: str) -> ValidationResult:
        url = f'https://www.reddit.com/r/{name}/'
        return self._executor.submit(self._validate, url).result()

    def _validate(self, url: str) -> ValidationResult:
        page = self._page()
        try:
            page.goto(url)
        except PlaywrightError:
            logger.warning('Navigation failed during validation', extra={'url': url}, exc_info=True)
            return ValidationResult(valid=False, error=ValidationError.CONNECTION_ERROR)
        page.wait_for_timeout(1500)
        return self._check_validity(page)

    @staticmethod
    def _check_validity(page: Page) -> ValidationResult:
        # Best-effort: matches reddit's known 404/private-community copy. NOT_FOUND is confirmed
        # working against a real nonexistent user (see PLAN_reddit_source_rewrite.md); FORBIDDEN
        # (private/suspended) is still unverified -- no real example inspected yet.
        body_text = page.locator('body').inner_text().lower()
        if 'nobody on reddit goes by that name' in body_text or 'this user has deleted their account' in body_text:
            return ValidationResult(valid=False, error=ValidationError.NOT_FOUND)
        if 'community doesn’t exist' in body_text or 'page not found' in body_text:
            return ValidationResult(valid=False, error=ValidationError.NOT_FOUND)
        if 'this community is private' in body_text or 'suspended' in body_text:
            return ValidationResult(valid=False, error=ValidationError.FORBIDDEN)
        return ValidationResult(valid=True)

    def validate_and_iter_user_submissions(self, name: str, limit: Optional[int] = None,
                                           known_ids: Optional[set] = None
                                           ) -> Tuple[ValidationResult, List[SubmissionData]]:
        url = f'https://www.reddit.com/user/{name}/submitted/?sort=new'
        return self._executor.submit(self._validate_and_collect, url, limit, known_ids).result()

    def validate_and_iter_subreddit_submissions(self, name: str, limit: Optional[int] = None,
                                                known_ids: Optional[set] = None
                                                ) -> Tuple[ValidationResult, List[SubmissionData]]:
        url = f'https://www.reddit.com/r/{name}/new/'
        return self._executor.submit(self._validate_and_collect, url, limit, known_ids).result()

    def _validate_and_collect(self, url: str, limit: Optional[int] = None, known_ids: Optional[set] = None
                              ) -> Tuple[ValidationResult, List[SubmissionData]]:
        # A single navigation serves both validation and the submissions scrape -- the submitted/new
        # listing page shows the same 404/private/suspended copy as the plain profile page, so there's
        # no need to visit the profile page first just to check it exists.
        page = self._page()
        try:
            page.goto(url)
        except PlaywrightError:
            logger.warning('Navigation failed during validation', extra={'url': url}, exc_info=True)
            return ValidationResult(valid=False, error=ValidationError.CONNECTION_ERROR), []
        page.wait_for_timeout(2000)
        validation = self._check_validity(page)
        if not validation.valid:
            return validation, []
        return validation, self._scroll_and_collect(page, url, limit, known_ids)

    def get_post(self, url: str) -> Optional[SubmissionData]:
        return self._executor.submit(self._get_post_impl, url).result()

    def _get_post_impl(self, url: str) -> Optional[SubmissionData]:
        # A real old.reddit.com URL reached this method and correctly found no <shreddit-post>
        # (old.reddit.com has no web components at all) -- confirming the assumption that only
        # www.reddit.com's permalink page renders it, same as the listing pages. Normalize the
        # domain before navigating.
        url = _normalize_reddit_url(url)
        page = self._page()
        try:
            page.goto(url)
        except PlaywrightError:
            logger.warning('Navigation failed fetching single post', extra={'url': url}, exc_info=True)
            return None
        page.wait_for_timeout(2000)
        post = page.locator('shreddit-post').first
        if post.count() == 0:
            logger.warning('No shreddit-post found at url', extra={'url': url})
            return None
        return _parse_post(post)
