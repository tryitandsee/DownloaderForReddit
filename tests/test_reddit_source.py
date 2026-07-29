from DownloaderForReddit.core.reddit_source import _INJECTED_SCRIPT, BrowserRedditSource

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


def make_source(calls):
    source = BrowserRedditSource()
    source.set_on_profile_exhausted(calls.append)
    return source


def test_dispatch_feed_exhausted_confirms_coverage_of_the_submitted_listing():
    calls = []
    source = make_source(calls)

    source._dispatch_feed_exhausted(
        "https://www.reddit.com/user/example_user/submitted/?sort=new",
        "end-of-feed-tracker",
    )

    assert calls == ["example_user"]


def test_dispatch_feed_exhausted_confirms_coverage_of_a_profile_with_no_visible_posts():
    calls = []
    source = make_source(calls)

    source._dispatch_feed_exhausted(
        "https://www.reddit.com/user/example_user/submitted/?sort=new",
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
        "https://www.reddit.com/r/example_subreddit/new/",
    ):
        source._dispatch_feed_exhausted(url, "end-of-feed-tracker")

    assert calls == []


def test_dispatch_feed_exhausted_ignores_a_marker_seen_during_explicit_navigation():
    calls = []
    source = make_source(calls)

    with source._suppressed_ambient():
        source._dispatch_feed_exhausted(
            "https://www.reddit.com/user/example_user/submitted/?sort=new",
            "end-of-feed-tracker",
        )

    assert calls == []


def test_injected_script_queries_only_markers_that_prove_the_listing_ended():
    assert all(f"#{marker_id}" in _INJECTED_SCRIPT for marker_id in MARKER_IDS)
    assert any(marker_id in SHORT_HISTORY_TAIL for marker_id in MARKER_IDS)
    assert any(marker_id in NO_VISIBLE_POSTS_TAIL for marker_id in MARKER_IDS)
    assert not any(marker_id in LONG_HISTORY_TAIL for marker_id in MARKER_IDS)
