from queue import Queue
from threading import Event
from types import SimpleNamespace

from DownloaderForReddit.core.content_runner import ContentRunner
from DownloaderForReddit.core.errors import Error
from DownloaderForReddit.core.submission_handler import SubmissionHandler
from DownloaderForReddit.database.database_handler import DatabaseHandler
from DownloaderForReddit.database.models import Post, User
from DownloaderForReddit.extractors.redgifs_extractor import RedgifsExtractor
from DownloaderForReddit.utils import injector


class LibraryError(BaseException):
    """Like redgifs errors, which derive from BaseException."""


def make_runner(monkeypatch, db):
    monkeypatch.setattr(injector, "database_handler", db)
    monkeypatch.setattr(
        injector, "settings_manager", SimpleNamespace(extraction_thread_count=1)
    )
    return ContentRunner(Queue(), Queue(), set(), Event(), lambda: None)


def add_post(db, **post_kwargs) -> int:
    with db.get_scoped_update_session() as session:
        user = User(
            name="example_user",
            download_self_post_text=False,
            extract_self_post_links=True,
        )
        session.add(user)
        session.flush()
        post = Post(
            title="post",
            url="https://example.com/post",
            author_id=user.id,
            significant_reddit_object_id=user.id,
            **post_kwargs,
        )
        session.add(post)
        session.flush()
        return post.id


def extraction_state(db, post_id):
    with db.get_scoped_session() as session:
        post = session.query(Post).get(post_id)
        return post.extracted, post.extraction_error, post.retry_attempts


def test_finish_post_records_a_failure_when_extraction_raises(monkeypatch):
    db = DatabaseHandler(in_memory=True)
    post_id = add_post(db, is_self=False)

    def raise_library_error(self):
        raise LibraryError

    monkeypatch.setattr(
        SubmissionHandler, "extract_submission_content", raise_library_error
    )
    make_runner(monkeypatch, db).finish_post(post_id=post_id, download_session_id=1)

    assert extraction_state(db, post_id) == (False, Error.UNKNOWN_ERROR, 1)


def test_finish_post_marks_a_text_post_without_links_as_extracted(monkeypatch):
    db = DatabaseHandler(in_memory=True)
    post_id = add_post(db, is_self=True, text_html='<div class="md"><p>hi</p></div>')

    make_runner(monkeypatch, db).finish_post(post_id=post_id, download_session_id=1)

    assert extraction_state(db, post_id) == (True, None, 0)


def test_redgifs_gif_id_ignores_query_extension_and_trailing_junk():
    assert [
        RedgifsExtractor.get_gif_id(url)
        for url in (
            "https://www.redgifs.com/watch/polishedboldstickleback?from=embed",
            "https://i.redgifs.com/i/mildbleakambushbug.jpg",
            "https://www.redgifs.com/ifr/whitesmokefantasticlungfish/////?/",
        )
    ] == [
        "polishedboldstickleback",
        "mildbleakambushbug",
        "whitesmokefantasticlungfish",
    ]
