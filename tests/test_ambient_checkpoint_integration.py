"""
This test is a piece of vibe coded shit. Will revisit when it fails.

Ties handle_profile_exhausted (ambient, gui/downloader_for_reddit_gui.py) together with
_scroll_and_collect (explicit scan, core/reddit_source.py): they never talk to each other
directly, only through one shared value -- User.date_last_download_utc. Each has its own unit
tests proving its own logic in isolation; this file proves the handoff between them is safe --
that a checkpoint the ambient side leaves unconfirmed doesn't cause a later scan to skip content,
and a checkpoint it does confirm is precise enough for that scan to trust."""

from datetime import UTC, datetime, timedelta

from DownloaderForReddit.core.reddit_source import (
    _END_OF_LISTING_EXPR,
    _EXTRACT_POSTS_EXPR,
    BrowserRedditSource,
    parse_posts_payload,
)
from DownloaderForReddit.database.database_handler import DatabaseHandler
from DownloaderForReddit.database.models import User
from DownloaderForReddit.gui.downloader_for_reddit_gui import DownloaderForRedditGUI
from tests.test_downloader_for_reddit_gui import make_fake_gui, mark_old
from tests.test_reddit_source import SUBMITTED_LISTING_URL


def post_for(post_id, created):
    return {
        "id": f"t3_{post_id}",
        "postType": "image",
        "contentHref": f"https://i.redd.it/{post_id}.png",
        "permalink": f"/r/example_subreddit/comments/{post_id}/example_title/",
        "postTitle": "example title",
        "domain": "i.redd.it",
        "author": "example_user",
        "subredditPrefixedName": "r/example_subreddit",
        "createdTimestamp": created.isoformat(),
        "score": "12",
        "nsfw": "",
        "moreCursor": None,
    }


class FakePage:
    """Same stand-in as test_reddit_source.FakePage -- not imported from there since that
    module's fixtures (raw_post, POST_TIME) are keyed to a single shared post rather than the
    several distinctly-timed posts this file needs."""

    def __init__(self, batches, url=SUBMITTED_LISTING_URL):
        self.url = url
        self.batches = batches
        self.reads = 0
        self.scrolls = 0
        self.mouse = self

    def evaluate(self, expression):
        if expression == _EXTRACT_POSTS_EXPR:
            batch = self.batches[min(self.reads, len(self.batches) - 1)]
            self.reads += 1
            return batch
        if expression == _END_OF_LISTING_EXPR:
            return False
        raise AssertionError(f"unexpected evaluate: {expression}")

    def wheel(self, x, y):
        self.scrolls += 1

    def wait_for_timeout(self, timeout):
        pass


def read_checkpoint(db):
    with db.get_scoped_session() as session:
        return session.query(User).filter(User.name == "example_user").first().date_last_download_utc


def test_unconfirmed_ambient_checkpoint_does_not_hide_content_from_a_later_scan():
    # pending_post renders but hasn't downloaded -- handle_profile_exhausted must defer rather
    # than confirm (see tests/test_downloader_for_reddit_gui.py's own tests for that in
    # isolation). The real-world risk this guards against: if it wrongly confirmed anyway, a scan
    # started moments later would read that bad checkpoint back as `since` and stop before ever
    # finding pending_post.
    db = DatabaseHandler(in_memory=True)
    with db.get_scoped_update_session() as session:
        session.add(User(name="example_user", significant=True, download_enabled=True))
    fake_self = make_fake_gui(db)
    created = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    raw = post_for("pending_post", created)

    DownloaderForRedditGUI.handle_profile_exhausted(
        fake_self, "example_user", parse_posts_payload([raw])
    )
    checkpoint = read_checkpoint(db)
    assert checkpoint is None

    page = FakePage([[raw]])
    found_posts, _confirmed = BrowserRedditSource()._scroll_and_collect(page, since=checkpoint)

    # The point isn't *how* the scan decided it was done (no end marker here, so it's the
    # empty-scroll fallback) -- it's that a None checkpoint never short-circuits reached_checkpoint,
    # so pending_post always gets read at least once instead of being silently skipped.
    assert [post.reddit_id for post in found_posts] == ["pending_post"]


def test_confirmed_ambient_checkpoint_lets_a_later_scan_stop_without_rescrolling():
    # Once pending_post is actually downloaded, a repeat marker report confirms coverage (see
    # test_downloader_for_reddit_gui.py's own deferred-then-downloaded test for that in
    # isolation). A scan against that now-confirmed checkpoint, seeing only that same
    # already-old post, should trust it and stop without scrolling at all.
    db = DatabaseHandler(in_memory=True)
    with db.get_scoped_update_session() as session:
        session.add(User(name="example_user", significant=True, download_enabled=True))
    fake_self = make_fake_gui(db)
    created = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    raw = post_for("pending_post", created)
    posts = parse_posts_payload([raw])

    DownloaderForRedditGUI.handle_profile_exhausted(fake_self, "example_user", posts)
    mark_old(db, "pending_post")
    DownloaderForRedditGUI.handle_profile_exhausted(fake_self, "example_user", posts)
    checkpoint = read_checkpoint(db)
    assert checkpoint is not None

    page = FakePage([[raw]])
    found_posts, confirmed = BrowserRedditSource()._scroll_and_collect(page, since=checkpoint)

    assert (len(found_posts), confirmed, page.scrolls) == (1, True, 0)


def test_confirmed_ambient_checkpoint_still_finds_a_new_post_rendered_out_of_order():
    # The companion case to the one above, and the actual bug from the screenshot this session
    # started from: a scan against a real, correctly-confirmed checkpoint must still keep
    # scrolling if the very same batch that satisfies the checkpoint also contains a post newer
    # than it (out-of-order render -- pinned/promoted content isn't guaranteed newest-first).
    # reached_checkpoint's any()-to-all() fix (test_reddit_source.py) is what makes that true;
    # this proves it holds even against a checkpoint this file confirmed for real, not a
    # hand-picked `since` value.
    db = DatabaseHandler(in_memory=True)
    with db.get_scoped_update_session() as session:
        session.add(User(name="example_user", significant=True, download_enabled=True))
    fake_self = make_fake_gui(db)
    old_created = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    old_raw = post_for("old_post", old_created)
    posts = parse_posts_payload([old_raw])

    DownloaderForRedditGUI.handle_profile_exhausted(fake_self, "example_user", posts)
    mark_old(db, "old_post")
    DownloaderForRedditGUI.handle_profile_exhausted(fake_self, "example_user", posts)
    checkpoint = read_checkpoint(db)
    assert checkpoint is not None

    new_raw = post_for("new_post", checkpoint.replace(tzinfo=UTC) + timedelta(days=1))
    page = FakePage([[old_raw, new_raw]])

    found_posts, _confirmed = BrowserRedditSource()._scroll_and_collect(page, since=checkpoint)

    # Both posts are read on the very first pass: reached_checkpoint requires the *whole* batch
    # to be at or before the checkpoint (all(), not any()), so old_post alone doesn't trigger an
    # early stop that would leave new_post undiscovered.
    assert {post.reddit_id for post in found_posts} == {"old_post", "new_post"}
