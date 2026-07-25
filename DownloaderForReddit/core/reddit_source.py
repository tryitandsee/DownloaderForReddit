"""
Reddit discovery via browser automation (Playwright), replacing PRAW after Reddit
disabled this app's client_id and locked app registration behind manual review.
See PLAN_reddit_source_rewrite.md for the full design and the Phase 0/1 findings
this implementation is based on.
"""

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional, Protocol

from playwright.sync_api import Locator, Page, sync_playwright

PROFILE_DIR = Path(__file__).resolve().parent.parent.parent / 'browser_profile'

logger = logging.getLogger(f'DownloaderForReddit.{__name__}')


class ValidationError(Enum):
    NOT_FOUND = 'not_found'
    FORBIDDEN = 'forbidden'
    RATE_LIMITED = 'rate_limited'
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

    def iter_user_submissions(self, name: str) -> Iterable[SubmissionData]: ...

    def iter_subreddit_submissions(self, name: str) -> Iterable[SubmissionData]: ...

    def iter_home_feed(self) -> Iterable[SubmissionData]: ...  # following-only aggregation

    def iter_multireddit(self, owner: str, name: str) -> Iterable[SubmissionData]: ...


def _strip_fullname_prefix(fullname: str) -> str:
    return re.sub(r'^t\d+_', '', fullname)


def _strip_subreddit_prefix(prefixed_name: str) -> str:
    return re.sub(r'^(r/|u_)', '', prefixed_name)


def _parse_post(post: Locator) -> Optional[SubmissionData]:
    raw_id = post.get_attribute('id')
    if not raw_id:
        return None
    try:
        return SubmissionData(
            reddit_id=_strip_fullname_prefix(raw_id),
            title=post.get_attribute('post-title') or '',
            url=post.get_attribute('content-href') or '',
            domain=post.get_attribute('domain') or '',
            author=post.get_attribute('author') or '',
            subreddit=_strip_subreddit_prefix(post.get_attribute('subreddit-prefixed-name') or ''),
            created=datetime.fromisoformat(post.get_attribute('created-timestamp')),
            score=int(post.get_attribute('score') or 0),
            nsfw=post.get_attribute('nsfw') is not None,
            is_self=post.get_attribute('post-type') == 'text',
            permalink=post.get_attribute('permalink') or '',
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
    mechanism". All discovery is serialized through one window by design: parallel scroll
    sessions would read as bot activity.
    """

    SCROLL_PASSES = 10
    SCROLL_PAUSE_MS = 1000

    def __init__(self):
        self._lock = threading.Lock()
        self._playwright = None
        self._context = None

    def start(self):
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
        )

    def stop(self):
        if self._context is not None:
            self._context.close()
        if self._playwright is not None:
            self._playwright.stop()

    def _page(self) -> Page:
        return self._context.pages[0] if self._context.pages else self._context.new_page()

    def _collect(self, url: str) -> Iterable[SubmissionData]:
        with self._lock:
            page = self._page()
            page.goto(url)
            page.wait_for_timeout(2000)
            seen = set()
            for _ in range(self.SCROLL_PASSES):
                for post in page.locator('shreddit-post').all():
                    data = _parse_post(post)
                    if data is not None and data.reddit_id not in seen:
                        seen.add(data.reddit_id)
                        yield data
                page.mouse.wheel(0, 2000)
                page.wait_for_timeout(self.SCROLL_PAUSE_MS)

    def iter_user_submissions(self, name: str) -> Iterable[SubmissionData]:
        yield from self._collect(f'https://www.reddit.com/user/{name}/submitted/?sort=new')

    def iter_subreddit_submissions(self, name: str) -> Iterable[SubmissionData]:
        yield from self._collect(f'https://www.reddit.com/r/{name}/new/')

    def iter_home_feed(self) -> Iterable[SubmissionData]:
        yield from self._collect('https://www.reddit.com/new/')

    def iter_multireddit(self, owner: str, name: str) -> Iterable[SubmissionData]:
        yield from self._collect(f'https://www.reddit.com/user/{owner}/m/{name}/')

    def validate_user(self, name: str) -> ValidationResult:
        return self._validate(f'https://www.reddit.com/user/{name}/')

    def validate_subreddit(self, name: str) -> ValidationResult:
        return self._validate(f'https://www.reddit.com/r/{name}/')

    def _validate(self, url: str) -> ValidationResult:
        # Best-effort: matches reddit's known 404/private-community copy, but this hasn't
        # been confirmed against real invalid/private/suspended pages (Phase 0/1 only
        # probed valid targets). Verify before relying on this in Phase 3.
        with self._lock:
            page = self._page()
            page.goto(url)
            page.wait_for_timeout(1500)
            body_text = page.locator('body').inner_text()
        if 'nobody on reddit goes by that name' in body_text.lower():
            return ValidationResult(valid=False, error=ValidationError.NOT_FOUND)
        if 'community doesn’t exist' in body_text.lower() or 'page not found' in body_text.lower():
            return ValidationResult(valid=False, error=ValidationError.NOT_FOUND)
        if 'this community is private' in body_text.lower() or 'suspended' in body_text.lower():
            return ValidationResult(valid=False, error=ValidationError.FORBIDDEN)
        return ValidationResult(valid=True)
