from dataclasses import dataclass
from enum import Enum

from ..utils import injector


class MessageType(Enum):
    TEXT = 1

    POTENTIAL_PROGRESS = 2
    ACTUAL_PROGRESS = 3
    POTENTIAL_COUNT = 4
    ACTUAL_COUNT = 5

    CONTENT_FOUND = 6
    FOLLOW_STATE_CHANGED = 7
    CONTENT_SKIPPED = 8


@dataclass
class ContentFoundPayload:
    reddit_id: str
    author: str
    subreddit: str
    permalink: (
        str  # comments page, not the content url -- always browsable in the same shape
    )
    is_new: bool


@dataclass
class FollowStatePayload:
    username: str
    followed: bool


@dataclass
class ContentSkippedPayload:
    # A free-text reason rather than an enum/code: skip sites (crosspost links, disabled
    # self-post settings, content filters) already compute their own human-readable message for
    # logging/failed_extraction_message, so this just reuses that string instead of introducing a
    # second classification scheme -- any future filter can report through this same payload.
    reddit_id: str
    reason: str


class MessagePriority(Enum):
    DEBUG = 1
    INFO = 2
    WARNING = 3
    ERROR = 4
    CRITICAL = 5
    REQUESTED = 10


class Message:
    message_queue = injector.get_message_queue()

    def __init__(
        self,
        message_type: MessageType,
        message: str | None = None,
        priority: MessagePriority = MessagePriority.INFO,
        payload: ContentFoundPayload
        | FollowStatePayload
        | ContentSkippedPayload
        | None = None,
    ):
        self.message_type = message_type
        self.message = message
        self.priority = priority
        self.payload = payload

    @property
    def output(self):
        return f"{self.priority.name}:  {self.message}"

    @classmethod
    def send(
        cls,
        message_type: MessageType,
        message: str | None = None,
        priority: MessagePriority = MessagePriority.INFO,
        payload: ContentFoundPayload
        | FollowStatePayload
        | ContentSkippedPayload
        | None = None,
    ) -> None:
        m = cls(message_type, message, priority, payload)
        cls.message_queue.put(m)

    @classmethod
    def send_debug(cls, message: str) -> None:
        cls.send(MessageType.TEXT, message, MessagePriority.DEBUG)

    @classmethod
    def send_info(cls, message: str) -> None:
        cls.send(MessageType.TEXT, message, MessagePriority.INFO)

    @classmethod
    def send_warning(cls, message: str) -> None:
        cls.send(MessageType.TEXT, message, MessagePriority.WARNING)

    @classmethod
    def send_error(cls, message: str) -> None:
        cls.send(MessageType.TEXT, message, MessagePriority.ERROR)

    @classmethod
    def send_critical(cls, message: str) -> None:
        cls.send(MessageType.TEXT, message, MessagePriority.CRITICAL)

    @classmethod
    def send_requested(cls, message: str) -> None:
        cls.send(MessageType.TEXT, message, MessagePriority.REQUESTED)

    @classmethod
    def send_content_found(cls, payload: ContentFoundPayload) -> None:
        cls.send(MessageType.CONTENT_FOUND, payload=payload)

    @classmethod
    def send_follow_state_changed(cls, payload: FollowStatePayload) -> None:
        cls.send(MessageType.FOLLOW_STATE_CHANGED, payload=payload)

    @classmethod
    def send_content_skipped(cls, payload: ContentSkippedPayload) -> None:
        cls.send(MessageType.CONTENT_SKIPPED, payload=payload)

    @classmethod
    def send_extraction_error(cls, message: str):
        cls.send(MessageType.POTENTIAL_PROGRESS, priority=MessagePriority.ERROR)
        cls.send(MessageType.TEXT, message, MessagePriority.ERROR)

    @classmethod
    def send_download_error(cls, message: str):
        cls.send(MessageType.POTENTIAL_PROGRESS, priority=MessagePriority.ERROR)
        cls.send(MessageType.TEXT, message, MessagePriority.ERROR)
