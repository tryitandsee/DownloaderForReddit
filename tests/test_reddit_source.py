from datetime import UTC, datetime, timedelta

from DownloaderForReddit.core.reddit_source import (
    _END_OF_LISTING_EXPR,
    _EXTRACT_POSTS_EXPR,
    _INJECTED_SCRIPT,
    _MAX_SCROLL_ITERATIONS,
    BrowserRedditSource,
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
    source.set_on_profile_exhausted(calls.append)
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
    )

    assert calls == ["example_user"]


def test_dispatch_feed_exhausted_confirms_coverage_of_a_profile_with_no_visible_posts():
    calls = []
    source = make_source(calls)

    source._dispatch_feed_exhausted(
        SUBMITTED_LISTING_URL,
        "empty-feed-content",
    )

    assert calls == ["example_user"]


def test_dispatch_feed_exhausted_ignores_other_tabs_of_the_same_profile():
    calls = []
    source = make_source(calls)

    for url in (
        "https://www.reddit.com/user/example_user/",
        "https://www.reddit.com/user/example_user/comments/",
        "https://www.reddit.com/user/example_user/upvoted/",
        SUBREDDIT_LISTING_URL,
    ):
        source._dispatch_feed_exhausted(url, "end-of-feed-tracker")

    assert calls == []


def test_dispatch_feed_exhausted_ignores_a_marker_seen_during_explicit_navigation():
    calls = []
    source = make_source(calls)

    with source._suppressed_ambient():
        source._dispatch_feed_exhausted(
            SUBMITTED_LISTING_URL,
            "end-of-feed-tracker",
        )

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


def test_scroll_and_collect_reports_unconfirmed_when_the_scroll_cap_is_hit():
    page = FakePage([[raw_post(f"t3_{n}")] for n in range(_MAX_SCROLL_ITERATIONS + 2)])

    posts, confirmed = BrowserRedditSource()._scroll_and_collect(page, since=None)

    assert (len(posts), confirmed) == (_MAX_SCROLL_ITERATIONS + 1, False)
