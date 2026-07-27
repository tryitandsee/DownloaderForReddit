# ruff: noqa: N999
from sqlalchemy import event

from ..messaging.message import Message, MessageType
from ..scheduling.tasks import (
    DownloadTask,  # import here so the database table is created along with the others
)
from .models import Comment, Content, Post


@event.listens_for(Post, 'after_insert')
def post_created(mapper, connection, target):
    Message.send(MessageType.POTENTIAL_PROGRESS)


@event.listens_for(Post.extracted, 'set')
def post_extracted(target, value, old_value, initiator):
    if value:
        Message.send(MessageType.ACTUAL_PROGRESS)


@event.listens_for(Content, 'after_insert')
def content_created(mapper, connection, target):
    Message.send(MessageType.POTENTIAL_COUNT)


@event.listens_for(Content.downloaded, 'set')
def content_downloaded(target, value, old_value, initiator):
    if value:
        Message.send(MessageType.ACTUAL_COUNT)


@event.listens_for(Comment, 'after_insert')
def comment_created(mapper, connection, target):
    Message.send(MessageType.POTENTIAL_PROGRESS)


@event.listens_for(Comment.extracted, 'set')
def comment_extracted(target, value, old_value, initiator):
    if value:
        Message.send(MessageType.ACTUAL_PROGRESS)
