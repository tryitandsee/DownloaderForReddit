from datetime import UTC, datetime, timedelta
from threading import Event

import pytest

from DownloaderForReddit.core import const
from DownloaderForReddit.core.reddit_source import (
    _END_OF_LISTING_EXPR,
    _EXTRACT_POSTS_EXPR,
    _INJECTED_SCRIPT,
    BrowserRedditSource,
    StopRequestedError,
)

# Captured from the real rendered DOM of three profile submitted-listings (usernames anonymized,
# post ids left as reddit returned them). These are the pages that must be told apart: a history
# short enough to fit on one page ends in the end-of-feed tracker, a profile with no visible posts
# renders the empty-feed placeholder, and a long history renders neither -- it carries a
# load-after partial instead, and only produces a tracker after the user scrolls to the end.
SHORT_HISTORY_TAIL = (
    "</shreddit-post>\n</article>\n"
    '<hr class="border-0 border-b-sm border-solid border-b-neutral-border-weak">\n'
    '<span id="end-of-feed-tracker" actioned=""></span>'
)
NO_VISIBLE_POSTS_TAIL = (
    '<div class="mt-[100px] flex justify-center items-center flex-col" '
    'id="empty-feed-content" data-cuj-omit="">\n'
    '<div class="text-title-2 text-neutral-content-strong" role="heading" aria-level="2">'
    "Welcome!</div>\n"
    '<div class="text-body-1 text-neutral-content-weak">u/example_user likes to keep their '
    "posts hidden, but check out their stats to learn more about them.</div>\n</div>"
)
LONG_HISTORY_TAIL = (
    "</shreddit-post>\n</article>\n"
    '<faceplate-partial slot="load-after" loading="programmatic" '
    'src="/svc/shreddit/profiles/profile_posts-more-posts/new/?sort=new'
    '&after=dDNfMXY4a2d5aQ%3D%3D&name=example_user&feedLength=35&distance=25">'
    "</faceplate-partial>"
)

MARKER_IDS = ("end-of-feed-tracker", "empty-feed-content")

POST_TIME = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
SUBMITTED_LISTING_URL = "https://www.reddit.com/user/example_user/submitted/?sort=new"
SUBREDDIT_LISTING_URL = "https://www.reddit.com/r/example_subreddit/new/"


def make_source(calls):
    source = BrowserRedditSource()
    source.set_on_profile_exhausted(
        lambda page_owner, posts: calls.append((page_owner, posts))
    )
    return source


def raw_post(reddit_id, created=POST_TIME):
    return {
        "id": reddit_id,
        "postType": "image",
        "contentHref": "https://i.redd.it/rogyn4busm9h1.png",
        "permalink": f"/r/example_subreddit/comments/{reddit_id[3:]}/example_title/",
        "postTitle": "example title",
        "domain": "i.redd.it",
        "author": "example_user",
        "subredditPrefixedName": "r/example_subreddit",
        "createdTimestamp": created.isoformat(),
        "score": "12",
        "nsfw": "",
        "moreCursor": "dDNfMXY4a2d5aQ",
    }


class FakePage:
    """Stands in for a Playwright page over the two expressions _scroll_and_collect evaluates.
    `batches` is what each successive read returns -- one entry per scroll, cumulative, the way a
    real listing grows as shreddit lazy-loads."""

    def __init__(self, batches, ended_after=None, url=SUBMITTED_LISTING_URL):
        self.url = url
        self.batches = batches
        self.ended_after = ended_after
        self.reads = 0
        self.scrolls = 0
        self.mouse = self

    def evaluate(self, expression):
        if expression == _EXTRACT_POSTS_EXPR:
            batch = self.batches[min(self.reads, len(self.batches) - 1)]
            self.reads += 1
            return batch
        if expression == _END_OF_LISTING_EXPR:
            return self.ended_after is not None and self.scrolls >= self.ended_after
        raise AssertionError(f"unexpected evaluate: {expression}")

    def wheel(self, x, y):
        self.scrolls += 1

    def wait_for_timeout(self, timeout):
        pass


def test_dispatch_feed_exhausted_confirms_coverage_of_the_submitted_listing():
    calls = []
    source = make_source(calls)

    source._dispatch_feed_exhausted(
        SUBMITTED_LISTING_URL,
        "end-of-feed-tracker",
        [raw_post("t3_1ug7l94")],
    )

    assert [page_owner for page_owner, _ in calls] == ["example_user"]
    assert [post.reddit_id for post in calls[0][1]] == ["1ug7l94"]


def test_dispatch_feed_exhausted_confirms_coverage_of_a_profile_with_no_visible_posts():
    calls = []
    source = make_source(calls)

    source._dispatch_feed_exhausted(
        SUBMITTED_LISTING_URL,
        "empty-feed-content",
        [],
    )

    assert calls == [("example_user", [])]


def test_dispatch_feed_exhausted_ignores_other_tabs_of_the_same_profile():
    calls = []
    source = make_source(calls)

    for url in (
        "https://www.reddit.com/user/example_user/",
        "https://www.reddit.com/user/example_user/comments/",
        "https://www.reddit.com/user/example_user/upvoted/",
        SUBREDDIT_LISTING_URL,
    ):
        source._dispatch_feed_exhausted(url, "end-of-feed-tracker", [])

    assert calls == []


def test_dispatch_feed_exhausted_ignores_a_marker_seen_during_explicit_navigation():
    calls = []
    source = make_source(calls)

    with source._suppressed_ambient():
        source._dispatch_feed_exhausted(
            SUBMITTED_LISTING_URL,
            "end-of-feed-tracker",
            [],
        )

    assert calls == []


def test_dispatch_profile_pagination_exhausted_confirms_coverage_of_rendered_posts():
    calls = []
    source = make_source(calls)
    page = FakePage([[raw_post("t3_1ug7l94")]])

    source._dispatch_profile_pagination_exhausted(page)

    assert [page_owner for page_owner, _ in calls] == ["example_user"]
    assert [post.reddit_id for post in calls[0][1]] == ["1ug7l94"]


def test_dispatch_profile_pagination_exhausted_skips_a_stale_empty_read():
    # Unlike signal 3's empty-feed-content, an empty read here means the read raced the page
    # rather than a genuinely empty profile (a pagination response only fires because more posts
    # were loading) -- confirming coverage over it would be the exact premature confirm this
    # check exists to prevent.
    calls = []
    source = make_source(calls)
    page = FakePage([[]])

    source._dispatch_profile_pagination_exhausted(page)

    assert calls == []


def test_dispatch_profile_pagination_exhausted_skips_a_page_that_navigated_elsewhere():
    calls = []
    source = make_source(calls)
    page = FakePage([[raw_post("t3_1ug7l94")]], url=SUBREDDIT_LISTING_URL)

    source._dispatch_profile_pagination_exhausted(page)

    assert calls == []


def test_injected_script_queries_only_markers_that_prove_the_listing_ended():
    assert all(f"#{marker_id}" in _INJECTED_SCRIPT for marker_id in MARKER_IDS)
    assert any(marker_id in SHORT_HISTORY_TAIL for marker_id in MARKER_IDS)
    assert any(marker_id in NO_VISIBLE_POSTS_TAIL for marker_id in MARKER_IDS)
    assert not any(marker_id in LONG_HISTORY_TAIL for marker_id in MARKER_IDS)


def test_scroll_and_collect_stops_without_scrolling_when_the_listing_already_ended():
    page = FakePage([[raw_post("t3_1ug7l94")]], ended_after=0)

    posts, confirmed = BrowserRedditSource()._scroll_and_collect(page, since=None)

    assert (len(posts), confirmed, page.scrolls) == (1, True, 0)


def test_scroll_and_collect_confirms_coverage_when_the_marker_appears_mid_scroll():
    page = FakePage(
        [
            [raw_post("t3_1ug7l94")],
            [raw_post("t3_1ug7l94"), raw_post("t3_1ug7iba")],
        ],
        ended_after=1,
    )

    posts, confirmed = BrowserRedditSource()._scroll_and_collect(page, since=None)

    assert (len(posts), confirmed, page.scrolls) == (2, True, 1)


def test_scroll_and_collect_keeps_scrolling_through_a_single_empty_batch():
    page = FakePage(
        [
            [raw_post("t3_1ug7l94")],
            [raw_post("t3_1ug7l94")],
            [raw_post("t3_1ug7l94"), raw_post("t3_1ug7iba")],
        ],
        ended_after=3,
    )

    posts, confirmed = BrowserRedditSource()._scroll_and_collect(page, since=None)

    assert (len(posts), confirmed, page.scrolls) == (2, True, 3)


def test_scroll_and_collect_treats_repeated_empty_batches_as_the_end_when_no_marker_renders():
    page = FakePage([[raw_post("t3_1ug7l94")]])

    posts, confirmed = BrowserRedditSource()._scroll_and_collect(page, since=None)

    assert (len(posts), confirmed, page.scrolls) == (1, True, 2)


def test_scroll_and_collect_ignores_an_end_of_listing_marker_on_a_subreddit_listing():
    page = FakePage(
        [
            [raw_post("t3_1ug7l94")],
            [raw_post("t3_1ug7l94"), raw_post("t3_1ug7iba")],
        ],
        ended_after=0,
        url=SUBREDDIT_LISTING_URL,
    )

    _posts, confirmed = BrowserRedditSource()._scroll_and_collect(page, since=None)

    assert (confirmed, page.scrolls) == (True, 3)


def test_scroll_and_collect_stops_at_the_checkpoint_without_reading_the_whole_listing():
    # `since` is the naive-UTC checkpoint stored on the reddit object; posts carry tz-aware
    # timestamps, and only the second batch reaches back past the checkpoint.
    since = (POST_TIME + timedelta(days=1)).replace(tzinfo=None)
    newer = raw_post("t3_1ug7l94", created=POST_TIME + timedelta(days=2))
    page = FakePage([[newer], [newer, raw_post("t3_1ug7iba")]])

    posts, confirmed = BrowserRedditSource()._scroll_and_collect(page, since=since)

    assert (len(posts), confirmed, page.scrolls) == (2, True, 1)


def test_scroll_and_collect_keeps_scrolling_past_an_out_of_order_old_post():
    # shreddit's render order isn't guaranteed strictly newest-first (pinned posts, promoted/
    # resurfaced items) -- an old post sitting ahead of a still-undiscovered new one in the same
    # batch must not be read as "reached the checkpoint, nothing left to find".
    since = POST_TIME.replace(tzinfo=None)
    old_post = raw_post("t3_1ug7l94", created=POST_TIME - timedelta(days=1))
    new_post = raw_post("t3_1ug7iba", created=POST_TIME + timedelta(days=1))
    page = FakePage([[old_post, new_post]], ended_after=1)

    posts, confirmed = BrowserRedditSource()._scroll_and_collect(page, since=since)

    assert (len(posts), confirmed, page.scrolls) == (2, True, 1)


def test_scroll_and_collect_reports_unconfirmed_when_the_scroll_cap_is_hit():
    page = FakePage(
        [[raw_post(f"t3_{n}")] for n in range(const.MAX_SCROLL_ITERATIONS + 2)]
    )

    posts, confirmed = BrowserRedditSource()._scroll_and_collect(page, since=None)

    assert (len(posts), confirmed) == (const.MAX_SCROLL_ITERATIONS + 1, False)


def test_scroll_and_collect_paces_every_scroll_when_a_pacer_is_registered():
    page = FakePage(
        [[raw_post("t3_1ug7l94")], [raw_post("t3_1ug7l94"), raw_post("t3_1ug7iba")]],
        ended_after=1,
    )
    source = BrowserRedditSource()
    pace_calls = []
    source.set_scroll_pacer(lambda: pace_calls.append(None))

    source._scroll_and_collect(page, since=None)

    assert len(pace_calls) == page.scrolls == 1


def test_scroll_and_collect_never_scrolls_when_no_pacer_is_registered():
    # No set_scroll_pacer call -- confirms the pacer is optional (registered by DownloadRunner,
    # not required for a bare BrowserRedditSource) rather than a hard dependency.
    page = FakePage([[raw_post("t3_1ug7l94")]], ended_after=0)

    posts, confirmed = BrowserRedditSource()._scroll_and_collect(page, since=None)

    assert (len(posts), confirmed, page.scrolls) == (1, True, 0)


def test_scroll_and_collect_raises_immediately_when_stop_is_requested():
    """Regression test: continue_run is only checked between objects/scrolls, never during a
    page.goto/mouse.wheel call itself -- most of a download's wall-clock time -- so a Stop click
    previously had no effect until whatever navigation was already in flight finished on its own.
    set_stop_event gives BrowserRedditSource direct, thread-safe access to the same Event the GUI
    sets, checked at the top of every scroll via _check_should_continue."""
    page = FakePage(
        [[raw_post(f"t3_{n}")] for n in range(const.MAX_SCROLL_ITERATIONS + 2)]
    )
    source = BrowserRedditSource()
    stop_requested = Event()
    source.set_stop_event(stop_requested)
    stop_requested.set()

    with pytest.raises(StopRequestedError):
        source._scroll_and_collect(page, since=None)

    assert page.scrolls == 0


def test_check_should_continue_is_a_no_op_when_no_stop_event_is_registered():
    # No set_stop_event call -- confirms the check tolerates a bare BrowserRedditSource, same as
    # the scroll pacer.
    BrowserRedditSource()._check_should_continue()
