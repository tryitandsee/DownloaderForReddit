import logging
from datetime import datetime
from typing import Optional, Union
from sqlalchemy import or_
from sqlalchemy.orm.session import Session
from praw.models import Comment as PrawComment

from ..database.models import User, Subreddit, Post, Comment
from ..utils import injector
from .reddit_source import SubmissionData


class SubmittableCreator:

    logger = logging.getLogger(f'DownloaderForReddit.{__name__}')
    db = None

    @classmethod
    def get_db(cls):
        if cls.db is None:
            cls.db = injector.get_database_handler()
        return cls.db

    @classmethod
    def create_post(cls, submission: SubmissionData, significant_id: int, session: Session,
                    download_session_id: int) -> Optional[Post]:
        post = None
        if cls.check_duplicate_post(submission.reddit_id, submission.url, session):
            author = cls.get_author(submission, session)
            subreddit = cls.get_subreddit(submission, session)

            post = Post(
                title=submission.title,
                date_posted=submission.created,
                domain=submission.domain,
                score=submission.score,
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
                significant_reddit_object_id=significant_id
            )
            session.add(post)
            session.commit()
        return post

    @classmethod
    def check_duplicate_post(cls, reddit_id, url, session):
        # reddit_id is checked (not just url) because Post.reddit_id is DB-unique: the same post re-encountered
        # under a different url (e.g. a crosspost whose resolved url changed) must still be caught here, or the
        # later insert hits the unique constraint and raises uncaught inside create_post.
        return session.query(Post.id).filter(or_(Post.reddit_id == reddit_id, Post.url == url)).scalar() is None

    @classmethod
    def create_comment(cls, praw_comment: PrawComment, post: Post, session: Session, download_session_id: int,
                       parent_comment_id: Optional[int] = None):
        if cls.check_duplicate_comment(praw_comment.id, session):
            author = cls.get_author(praw_comment, session)
            subreddit = cls.get_subreddit(praw_comment, session)
            comment = Comment(
                author=author,
                subreddit=subreddit,
                post=post,
                reddit_id=praw_comment.id,
                body=praw_comment.body,
                body_html=praw_comment.body_html,
                score=praw_comment.score,
                date_posted=datetime.fromtimestamp(praw_comment.created),
                parent_id=parent_comment_id,
                download_session_id=download_session_id
            )
            session.add(comment)
            session.commit()
            return comment
        return None

    @classmethod
    def check_duplicate_comment(cls, praw_comment_id: str, session: Session):
        return session.query(Comment).filter(Comment.reddit_id == praw_comment_id).scalar() is None

    @classmethod
    def get_author(cls, praw_object: Union[SubmissionData, PrawComment], session: Session):
        try:
            name = praw_object.author if isinstance(praw_object, SubmissionData) else praw_object.author.name
            author = cls.get_db().get_or_create(User, name=name, defaults={}, session=session)[0]
        except AttributeError:
            cls.logger.error('Failed to get author', exc_info=True)
            author = cls.get_db().get_or_create(User, name='deleted', session=session)[0]
        return author

    @classmethod
    def get_subreddit(cls, praw_object: Union[SubmissionData, PrawComment], session: Session):
        try:
            name = praw_object.subreddit if isinstance(praw_object, SubmissionData) \
                else praw_object.subreddit.display_name
            subreddit = cls.get_db().get_or_create(Subreddit, name=name, defaults={}, session=session)[0]
        except AttributeError:
            cls.logger.error('Failed to get subreddit', exc_info=True)
            subreddit = cls.get_db().get_or_create(Subreddit, name='deleted', session=session)[0]
        return subreddit
