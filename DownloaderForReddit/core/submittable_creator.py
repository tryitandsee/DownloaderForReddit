import logging
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm.session import Session

from ..database.models import Post, Subreddit, User
from ..utils import injector
from .reddit_source import SubmissionData


class SubmittableCreator:
    logger = logging.getLogger(f"DownloaderForReddit.{__name__}")
    db = None

    @classmethod
    def get_db(cls):
        if cls.db is None:
            cls.db = injector.get_database_handler()
        return cls.db

    @classmethod
    def create_post(
        cls,
        submission: SubmissionData,
        significant_id: int,
        session: Session,
        download_session_id: int,
    ) -> Post | None:
        post = None
        if cls.check_duplicate_post(submission.reddit_id, submission.url, session):
            author = cls.get_author(submission, session)
            subreddit = cls.get_subreddit(submission, session)

            post = Post(
                title=submission.title,
                date_posted=submission.created,
                domain=submission.domain,
                nsfw=submission.nsfw,
                reddit_id=submission.reddit_id,
                url=submission.url,
                is_self=submission.is_self,
                # self-post body text isn't in the feed card; extraction is deferred to a per-post visit
                text=None,
                text_html=None,
                extraction_date=datetime.now(),
                author=author,
                subreddit=subreddit,
                download_session_id=download_session_id,
                significant_reddit_object_id=significant_id,
            )
            session.add(post)
            session.commit()
        return post

    @classmethod
    def check_duplicate_post(cls, reddit_id, url, session):
        # reddit_id is checked (not just url) because Post.reddit_id is DB-unique: the same post re-encountered
        # under a different url (e.g. a crosspost whose resolved url changed) must still be caught here, or the
        # later insert hits the unique constraint and raises uncaught inside create_post.
        # An empty url means "unknown", not "no url" -- matching on it would treat every post read
        # before content-href hydrates as a duplicate of every other one (see reddit_source._parse_post).
        filters = [Post.reddit_id == reddit_id]
        if url:
            filters.append(Post.url == url)
        return session.query(Post.id).filter(or_(*filters)).first() is None

    @classmethod
    def get_author(cls, submission: SubmissionData, session: Session):
        try:
            author = cls.get_db().get_or_create(
                User, name=submission.author, defaults={}, session=session
            )[0]
        except AttributeError:
            cls.logger.exception("Failed to get author")
            author = cls.get_db().get_or_create(User, name="deleted", session=session)[
                0
            ]
        return author

    @classmethod
    def get_subreddit(cls, submission: SubmissionData, session: Session):
        try:
            subreddit = cls.get_db().get_or_create(
                Subreddit, name=submission.subreddit, defaults={}, session=session
            )[0]
        except AttributeError:
            cls.logger.exception("Failed to get subreddit")
            subreddit = cls.get_db().get_or_create(
                Subreddit, name="deleted", session=session
            )[0]
        return subreddit
