from sqlalchemy import and_, exists, or_

from ..utils import injector, system_util
from .models import Comment, Content, ListAssociation, Post, User


def check_session(method):
    def check(*args, **kwargs):
        if kwargs.get("session", None) is None:
            with injector.get_database_handler().get_scoped_session() as session:
                kwargs["session"] = session
                return method(*args, **kwargs)
        return method(*args, **kwargs)

    return check


class ModelManger:
    """
    Manages the deletion of database objects and their related models and respective files.
    """

    @classmethod
    @check_session
    def delete_list(cls, ro_list, session=None, cascade=False):
        if cascade:
            for ro in ro_list.reddit_objects:
                cls.delete_reddit_object(ro, session=session)
        session.delete(ro_list)
        session.commit()

    @classmethod
    @check_session
    def delete_reddit_object(cls, reddit_object, session=None, delete_files=False):
        post_ids = session.query(Post.id).filter(
            Post.significant_reddit_object_id == reddit_object.id
        )
        comment_ids = session.query(Comment.id).filter(Comment.post_id.in_(post_ids))
        cls.bulk_delete(Comment, session, comment_ids)
        files = []
        if delete_files:
            content = session.query(Content).filter(Content.post_id.in_(post_ids))
            files = [c.get_full_file_path() for c in content]
        content_ids = session.query(Content.id).filter(Content.post_id.in_(post_ids))
        cls.bulk_delete(Content, session, content_ids)
        cls.bulk_delete(Post, session, post_ids)
        session.query(ListAssociation).filter(
            ListAssociation.reddit_object_id == reddit_object.id
        ).delete()
        session.delete(reddit_object)
        session.commit()
        for file in files:
            system_util.delete_file(file)

    @classmethod
    @check_session
    def delete_post(cls, post, session=None, delete_files=False):
        comments = session.query(Comment.id).filter(Comment.post_id == post.id)
        cls.bulk_delete(Comment, session, comments)
        files = []
        if delete_files:
            # First query full content model so that file paths can be retrieved.
            content = session.query(Content).filter(Content.post_id == post.id)
            files = [c.get_full_file_path() for c in content]
        # Query content id's so that the query can be bulk deleted
        content_ids = session.query(Content.id).filter(Content.post_id == post.id)
        cls.bulk_delete(Content, session, content_ids)
        session.delete(post)
        for file in files:
            system_util.delete_file(file)
        session.commit()

    @classmethod
    @check_session
    def delete_content(
        cls, content, delete_post=False, session=None, delete_file=False
    ):
        if delete_post:
            session.delete(content.post)
        if delete_file:
            system_util.delete_file(content.get_full_file_path())
        session.delete(content)
        session.commit()

    @classmethod
    @check_session
    def delete_comment(
        cls, comment, session=None, delete_post=False, delete_files=False
    ):
        content = session.query(Content).filter(Content.comment_id == comment.id)
        files = [c.get_full_file_path() for c in content] if delete_files else []
        content_ids = session.query(Content.id).filter(Content.comment_id == comment.id)
        cls.bulk_delete(Content, session, content_ids)
        if delete_post and comment.post is not None:
            session.delete(comment.post)
        session.delete(comment)
        for file in files:
            system_util.delete_file(file)
        session.commit()

    @classmethod
    @check_session
    def delete_orphaned_undownloaded_content(cls, session=None) -> int:
        """
        Deletes undownloaded content whose post, comment or user row is gone. The downloader
        needs those rows, so such content fails on every run without recording an error and
        stays in the unfinished list forever. Downloaded rows are kept for duplicate checks.
        :return: The number of deleted content rows.
        """
        count = (
            session.query(Content)
            .filter(
                Content.downloaded == False,
                or_(
                    and_(Content.post_id == None, Content.comment_id == None),
                    and_(
                        Content.post_id != None,
                        ~exists().where(Post.id == Content.post_id),
                    ),
                    and_(
                        Content.comment_id != None,
                        ~exists().where(Comment.id == Content.comment_id),
                    ),
                    and_(
                        Content.user_id != None,
                        ~exists().where(User.__table__.c.id == Content.user_id),
                    ),
                ),
            )
            .delete(synchronize_session=False)
        )
        session.commit()
        return count

    @classmethod
    @check_session
    def delete_ownerless_posts(cls, session=None) -> int:
        """
        Deletes posts with no author and no significant object, with their comments and content.
        Deleting a user used to leave some of its posts behind, with the foreign keys set to NULL
        or pointing at the deleted row. Files on disk are not touched.
        :return: The number of deleted posts.
        """
        post_ids = session.query(Post.id).filter(
            Post.significant_reddit_object_id == None,
            or_(
                Post.author_id == None,
                ~exists().where(User.__table__.c.id == Post.author_id),
            ),
        )
        comment_ids = session.query(Comment.id).filter(Comment.post_id.in_(post_ids))
        content_ids = session.query(Content.id).filter(
            or_(Content.post_id.in_(post_ids), Content.comment_id.in_(comment_ids))
        )
        cls.bulk_delete(Content, session, content_ids)
        cls.bulk_delete(Comment, session, comment_ids)
        count = post_ids.count()
        cls.bulk_delete(Post, session, post_ids)
        session.commit()
        return count

    @classmethod
    def bulk_delete(cls, model, session, query):
        """
        Deletes the rows of `model` whose ids `query` returns, in one statement.
        :param model: The database model in the query which will be deleted.
        :param session: The session that has been used for the supplied query.
        :param query: The query containing the id's of the objects to be deleted.
        """
        session.query(model).filter(model.id.in_(query.subquery())).delete(
            synchronize_session="fetch"
        )
