import logging
from typing import cast

import pytest

from DownloaderForReddit.core.reddit_source import parse_posts_payload
from DownloaderForReddit.database.database_handler import DatabaseHandler
from DownloaderForReddit.database.models import Post, User
from DownloaderForReddit.gui.downloader_for_reddit_gui import DownloaderForRedditGUI


def raw_post(reddit_id):
    return {
        "id": reddit_id,
        "postType": "image",
        "contentHref": "https://i.redd.it/rogyn4busm9h1.png",
        "permalink": f"/r/example_subreddit/comments/{reddit_id[3:]}/example_title/",
        "postTitle": "example title",
        "domain": "i.redd.it",
        "author": "example_user",
        "subredditPrefixedName": "r/example_subreddit",
        "createdTimestamp": "2026-07-20T12:00:00+00:00",
        "score": "12",
        "nsfw": "",
        "moreCursor": None,
    }


def post_for(post_id):
    """Like raw_post() but keyed by a bare id with its own url, so a scenario with more than one
    post doesn't have them collide on the known-by-url check the way raw_post()'s single shared
    url would."""
    return {
        "id": f"t3_{post_id}",
        "postType": "image",
        "contentHref": f"https://i.redd.it/{post_id}.png",
        "permalink": f"/r/example_subreddit/comments/{post_id}/example_title/",
        "postTitle": "example title",
        "domain": "i.redd.it",
        "author": "example_user",
        "subredditPrefixedName": "r/example_subreddit",
        "createdTimestamp": "2026-07-20T12:00:00+00:00",
        "score": "12",
        "nsfw": "",
        "moreCursor": None,
    }


def mark_old(db, post_id):
    """Flips a post from [NEW] (rendered, no Post row) to [OLD] (downloaded) -- the content feed
    panel's own vocabulary for the same distinction (see ContentFeedPanel.add_entry). Simulates
    _match_and_queue_ambient_posts having won its race against the marker dispatch and already
    created the Post row."""
    with db.get_scoped_update_session() as session:
        session.add(Post(reddit_id=post_id, url=f"https://i.redd.it/{post_id}.png"))


class FakeSignal:
    def emit(self):
        pass


def make_fake_gui(db_handler) -> DownloaderForRedditGUI:
    """Calls the real, unbound handle_profile_exhausted directly (DownloaderForRedditGUI is a
    QMainWindow -- constructing one drags in a QApplication this test has no business needing)
    with a plain object standing in for self, providing only the attributes the method reads:
    a real DatabaseHandler(in_memory=True) session, a stub Qt signal, and the streak dict. Cast
    rather than typed as its real duck-typed shape -- callers pass it straight into a method
    typed to take the real class, same as the existing DownloadRunner.__new__ tests do."""
    fake_self = type(
        "FakeSelf",
        (),
        {
            "db_handler": db_handler,
            "logger": logging.getLogger("test"),
            "reddit_object_changed": FakeSignal(),
            "_ambient_known_streaks": {},
        },
    )()
    return cast(DownloaderForRedditGUI, fake_self)


def test_handle_profile_exhausted_defers_the_checkpoint_when_a_rendered_post_is_not_downloaded():
    db = DatabaseHandler(in_memory=True)
    with db.get_scoped_update_session() as session:
        session.add(User(name="example_user", significant=True, download_enabled=True))
    fake_self = make_fake_gui(db)
    posts = parse_posts_payload([raw_post("t3_1ug7l94")])

    DownloaderForRedditGUI.handle_profile_exhausted(fake_self, "example_user", posts)

    with db.get_scoped_session() as session:
        user = session.query(User).filter(User.name == "example_user").first()
        assert user.date_last_download_utc is None


def test_handle_profile_exhausted_confirms_coverage_when_every_rendered_post_is_known():
    db = DatabaseHandler(in_memory=True)
    with db.get_scoped_update_session() as session:
        user = User(name="example_user", significant=True, download_enabled=True)
        session.add(user)
        session.flush()
        session.add(Post(reddit_id="1ug7l94", url="https://i.redd.it/rogyn4busm9h1.png"))
    fake_self = make_fake_gui(db)
    posts = parse_posts_payload([raw_post("t3_1ug7l94")])

    DownloaderForRedditGUI.handle_profile_exhausted(fake_self, "example_user", posts)

    with db.get_scoped_session() as session:
        user = session.query(User).filter(User.name == "example_user").first()
        assert user.date_last_download_utc is not None


def test_handle_profile_exhausted_confirms_coverage_once_a_deferred_post_is_later_downloaded():
    # The injected script's marker report has no one-shot latch (removed deliberately -- see
    # reddit_source.reportIfExhausted's comment): it keeps re-firing on later DOM mutations, so a
    # deferred confirm must be able to succeed once the pending download actually lands.
    db = DatabaseHandler(in_memory=True)
    with db.get_scoped_update_session() as session:
        session.add(User(name="example_user", significant=True, download_enabled=True))
    fake_self = make_fake_gui(db)
    posts = parse_posts_payload([raw_post("t3_1ug7l94")])

    DownloaderForRedditGUI.handle_profile_exhausted(fake_self, "example_user", posts)
    with db.get_scoped_session() as session:
        user = session.query(User).filter(User.name == "example_user").first()
        assert user.date_last_download_utc is None

    with db.get_scoped_update_session() as session:
        session.add(Post(reddit_id="1ug7l94", url="https://i.redd.it/rogyn4busm9h1.png"))
    DownloaderForRedditGUI.handle_profile_exhausted(fake_self, "example_user", posts)

    with db.get_scoped_session() as session:
        user = session.query(User).filter(User.name == "example_user").first()
        assert user.date_last_download_utc is not None


def test_handle_profile_exhausted_does_not_reconfirm_an_already_covered_profile():
    # Without the source's re-firing marker latched off, a settled-but-still-mutating profile
    # page (hover cards, vote state, lazy media) would otherwise reconfirm coverage on every
    # mutation, advancing the checkpoint to a later "now" each time -- exactly the
    # checkpoint-ahead-of-coverage bug this check exists to prevent.
    db = DatabaseHandler(in_memory=True)
    with db.get_scoped_update_session() as session:
        user = User(name="example_user", significant=True, download_enabled=True)
        session.add(user)
        session.flush()
        session.add(Post(reddit_id="1ug7l94", url="https://i.redd.it/rogyn4busm9h1.png"))
    fake_self = make_fake_gui(db)
    posts = parse_posts_payload([raw_post("t3_1ug7l94")])

    DownloaderForRedditGUI.handle_profile_exhausted(fake_self, "example_user", posts)
    with db.get_scoped_session() as session:
        first_checkpoint = (
            session.query(User).filter(User.name == "example_user").first()
        ).date_last_download_utc

    DownloaderForRedditGUI.handle_profile_exhausted(fake_self, "example_user", posts)
    with db.get_scoped_session() as session:
        second_checkpoint = (
            session.query(User).filter(User.name == "example_user").first()
        ).date_last_download_utc

    assert second_checkpoint == first_checkpoint


def test_handle_profile_exhausted_confirms_coverage_for_a_profile_with_no_visible_posts():
    db = DatabaseHandler(in_memory=True)
    with db.get_scoped_update_session() as session:
        session.add(User(name="example_user", significant=True, download_enabled=True))
    fake_self = make_fake_gui(db)

    DownloaderForRedditGUI.handle_profile_exhausted(fake_self, "example_user", [])

    with db.get_scoped_session() as session:
        user = session.query(User).filter(User.name == "example_user").first()
        assert user.date_last_download_utc is not None


# Each scenario is a sequence of steps, one handle_profile_exhausted call per step -- modelling
# the injected script's reportIfExhausted() (reddit_source.py) re-firing __dfrFeedExhausted on
# each later DOM mutation while the end-of-listing marker stays present. A step is
# (rendered_ids, ids_to_flip_from_[NEW]_to_[OLD]_first, expect_confirmed_after_this_step) --
# posts are [NEW] the moment they're rendered and stay that way until mark_old() gives them a
# Post row, same as the content feed panel's own [NEW]/[OLD] labels. "Confirmed" means coverage
# was confirmed and date_last_download_utc got set, same word the codebase itself already uses
# (_match_and_queue_ambient_posts's `confirmed`, the "Ambient confirmed full coverage" log line).
#
# date_last_download_utc becomes `since` in BrowserRedditSource._scroll_and_collect: confirmed
# means a later explicit scan stops here (reached_checkpoint); not confirmed means it still has
# to scroll down to this content itself.
SEQUENCE_SCENARIOS = [
    pytest.param(
        [
            ([], [], True),
        ],
        id="empty_profile_confirms_immediately",
    ),
    pytest.param(
        [
            (["post_a", "post_b"], ["post_a", "post_b"], True),
        ],
        id="all_posts_already_old_confirms_immediately",
    ),
    pytest.param(
        [
            (["only_post"], [], False),
            (["only_post"], ["only_post"], True),
            (["only_post"], [], True),
        ],
        id="new_post_defers_until_it_becomes_old",
    ),
    pytest.param(
        [
            (["first_post"], [], False),
            (["first_post", "second_post"], [], False),
            (["first_post", "second_post"], ["first_post", "second_post"], True),
        ],
        id="second_post_appears_new_while_first_still_pending",
    ),
    pytest.param(
        [
            (["post_1", "post_2", "post_3"], [], False),
            (["post_1", "post_2", "post_3"], ["post_1", "post_2", "post_3"], True),
        ],
        # docs/ARCHITECTURE.md: "a short history never scrolls", so the marker fires on the very
        # first render, before any of a short profile's few posts are downloaded. Unlike the
        # separate ambient known-post-streak check (_AMBIENT_KNOWN_STREAK_THRESHOLD = 6 in
        # _match_and_queue_ambient_posts, which can never accumulate a run of 6 on a 3-post
        # profile), this all-known check has no length threshold.
        id="short_profile_history_all_new_then_downloaded",
    ),
]


@pytest.mark.parametrize("steps", SEQUENCE_SCENARIOS)
def test_handle_profile_exhausted_across_a_sequence_of_marker_reports(steps):
    db = DatabaseHandler(in_memory=True)
    with db.get_scoped_update_session() as session:
        session.add(User(name="example_user", significant=True, download_enabled=True))
    fake_self = make_fake_gui(db)

    for rendered_ids, ids_to_mark_old, expect_confirmed in steps:
        for post_id in ids_to_mark_old:
            mark_old(db, post_id)
        posts = parse_posts_payload([post_for(post_id) for post_id in rendered_ids])

        DownloaderForRedditGUI.handle_profile_exhausted(fake_self, "example_user", posts)

        with db.get_scoped_session() as session:
            user = session.query(User).filter(User.name == "example_user").first()
            assert (user.date_last_download_utc is not None) == expect_confirmed
