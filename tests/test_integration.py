from datetime import UTC, datetime

import pytest

from DownloaderForReddit.core.reddit_source import BrowserRedditSource, ValidationResult
from DownloaderForReddit.core.submission_filter import SubmissionFilter
from DownloaderForReddit.database.database_handler import DatabaseHandler
from DownloaderForReddit.database.models import Post, User
from tests.test_ambient_checkpoint_integration import post_for
from tests.test_download_runner import FakePutQueue, make_runner
from tests.test_reddit_source import FakePage

POST_TIME = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def db():
    return DatabaseHandler(in_memory=True)


@pytest.fixture
def user_id(db):
    with db.get_scoped_update_session() as session:
        user = User(name="example_user", significant=True, download_enabled=True)
        session.add(user)
        session.flush()
        return user.id


@pytest.fixture
def runner(db):
    runner = make_runner()
    runner.db = db
    runner.reddit_source = BrowserRedditSource()
    runner.submission_filter = SubmissionFilter()
    runner.filter_subreddits = False
    runner.validated_subreddits = []
    runner.download_session_id = None
    runner.submission_queue = FakePutQueue()
    return runner


@pytest.fixture
def make_page():
    def _make_page(pages_of_ids, created=POST_TIME, **fake_page_kwargs):
        cumulative, batches = [], []
        for page_ids in pages_of_ids:
            cumulative = cumulative + [
                post_for(post_id, created) for post_id in page_ids
            ]
            batches.append(list(cumulative))
        return FakePage(batches, **fake_page_kwargs)

    return _make_page


@pytest.fixture
def mark_downloaded(db):
    def _mark_downloaded(*post_ids):
        with db.get_scoped_update_session() as session:
            for post_id in post_ids:
                session.add(
                    Post(reddit_id=post_id, url=f"https://i.redd.it/{post_id}.png")
                )

    return _mark_downloaded


@pytest.fixture
def crosspost_pairs():
    """Returns (original_id, crosspost_id) pairs -- the crosspost reuses its original's url under its
    own, different reddit_id."""

    def _crosspost_pairs(count: int) -> list[tuple[str, str]]:
        return [(f"original{idx}", f"crosspost{idx}") for idx in range(1, count + 1)]

    return _crosspost_pairs


@pytest.fixture
def mark_crossposted_in_db(db: DatabaseHandler):
    def _mark_crossposted_in_db(pairs: list[tuple[str, str]]) -> None:
        with db.get_scoped_update_session() as session:
            for original_id, crosspost_id in pairs:
                session.add(
                    Post(
                        reddit_id=original_id,
                        url=f"https://i.redd.it/{crosspost_id}.png",
                    )
                )

    return _mark_crossposted_in_db


@pytest.fixture
def run_scan(runner, db, user_id):
    def _run_scan(page):
        result = {}

        def source_method(_name):
            posts, confirmed = runner.reddit_source._scroll_and_collect(
                page, since=None
            )
            result["posts"], result["confirmed"] = posts, confirmed
            return ValidationResult(valid=True), posts

        with db.get_scoped_session() as session:
            reddit_object = session.query(User).get(user_id)
            runner.get_validated_submissions(reddit_object, source_method)
        return result["confirmed"], result["posts"]

    return _run_scan


def test_stops_without_scrolling_when_the_first_page_is_all_downloaded(
    make_page, mark_downloaded, run_scan
):
    post_ids = [f"post{idx}" for idx in range(1, 26)]
    mark_downloaded(*post_ids)
    page = make_page([post_ids])

    confirmed, _posts = run_scan(page)

    assert (confirmed, page.scrolls) == (True, 0)


def test_one_new_post_keeps_a_page_from_counting_as_done(
    make_page, mark_downloaded, run_scan
):
    mixed_ids = [f"post{idx}" for idx in range(1, 26)]
    mark_downloaded(*mixed_ids[:24])
    filler_ids = [f"filler{idx}" for idx in range(1, 26)]
    page = make_page([mixed_ids, filler_ids], ended_after=1)

    confirmed, _posts = run_scan(page)

    assert (confirmed, page.scrolls) == (True, 1)


def test_stops_mid_scroll_not_just_on_the_initial_read(
    make_page, mark_downloaded, run_scan
):
    new_ids = [f"new{idx}" for idx in range(1, 26)]
    old_ids = [f"old{idx}" for idx in range(1, 26)]
    mark_downloaded(*old_ids)
    page = make_page([new_ids, old_ids])

    confirmed, _posts = run_scan(page)

    assert (confirmed, page.scrolls) == (True, 1)


def test_a_crosspost_matching_only_by_url_does_not_stop_the_scan(
    crosspost_pairs, mark_crossposted_in_db, make_page, run_scan
):
    pairs = crosspost_pairs(25)
    mark_crossposted_in_db(pairs)
    crosspost_ids = [crosspost_id for _, crosspost_id in pairs]
    filler_ids = [f"filler{idx}" for idx in range(1, 26)]
    page = make_page([crosspost_ids, filler_ids], ended_after=1)

    confirmed, _posts = run_scan(page)

    assert (confirmed, page.scrolls) == (True, 1)


def test_a_run_of_crossposts_does_not_hide_real_new_content_further_down(
    crosspost_pairs, mark_crossposted_in_db, make_page, run_scan
):
    pairs = crosspost_pairs(25)
    mark_crossposted_in_db(pairs)
    crosspost_ids = [crosspost_id for _, crosspost_id in pairs]
    new_ids = [f"new{idx}" for idx in range(1, 26)]
    page = make_page([crosspost_ids, new_ids], ended_after=1)

    confirmed, posts = run_scan(page)

    found_ids = {post.reddit_id for post in posts}
    assert confirmed is True
    assert found_ids.issuperset(new_ids)


@pytest.mark.skip(reason="stub")
def test_empty_page_confirms_coverage_without_scrolling(make_page, run_scan):
    pass


@pytest.mark.skip(reason="stub")
def test_partial_page_confirms_coverage_without_scrolling(make_page, run_scan):
    pass


@pytest.mark.skip(reason="stub")
def test_clean_small_update_stops_after_a_run_of_old_posts(
    make_page, mark_downloaded, run_scan
):
    # new posts, then a run of 6+ already-downloaded posts.
    pass


@pytest.mark.skip(reason="stub")
def test_clean_large_update_scrolls_until_an_old_page_confirms_coverage(
    make_page, mark_downloaded, run_scan
):
    # several full pages of new posts, then a page (full or partial) that's all old.
    pass


@pytest.mark.skip(reason="stub")
def test_dirty_update_with_interleaved_new_and_old_posts(
    make_page, mark_downloaded, run_scan
):
    pass


@pytest.mark.skip(reason="stub")
def test_new_user_with_a_large_backlog_scrolls_to_the_end_marker(make_page, run_scan):
    pass


@pytest.mark.skip(reason="stub")
def test_interrupted_download_stops_at_a_run_of_old_pages_leaving_trailing_new_posts_unscanned(
    make_page, mark_downloaded, run_scan
):
    # [NEW] page, two full [OLD] pages, then a trailing [NEW] page -- the scan should stop at
    # the old pages; the trailing new page is only picked up if ambient discovery or a manual
    # scroll reaches it later.
    pass
