from praw.models import Comment

from ..database.model_enums import CommentDownload
from ..database.models import RedditObject


class CommentFilter:
    def __init__(self):
        pass

    def filter_extraction(self, comment: Comment, reddit_object: RedditObject):
        if comment.is_submitter:
            return any(
                (
                    reddit_object.extract_comments != CommentDownload.DO_NOT_DOWNLOAD,
                    reddit_object.download_comments != CommentDownload.DO_NOT_DOWNLOAD,
                    reddit_object.download_comment_content
                    != CommentDownload.DO_NOT_DOWNLOAD,
                )
            )
        return any(
            (
                reddit_object.extract_comments == CommentDownload.DOWNLOAD,
                reddit_object.download_comments == CommentDownload.DOWNLOAD,
                reddit_object.download_comment_content == CommentDownload.DOWNLOAD,
            )
        )

    def filter_download(self, comment: Comment, reddit_object: RedditObject):
        if comment.is_submitter:
            return reddit_object.download_comments != CommentDownload.DO_NOT_DOWNLOAD
        return reddit_object.download_comments == CommentDownload.DOWNLOAD

    def filter_content_download(self, comment: Comment, reddit_object: RedditObject):
        if comment.is_submitter:
            return (
                reddit_object.download_comment_content
                != CommentDownload.DO_NOT_DOWNLOAD
            )
        return reddit_object.download_comment_content == CommentDownload.DOWNLOAD
