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


@dataclass
class ContentFoundPayload:
    reddit_id: str
    author: str
    subreddit: str
    permalink: str  # comments page, not the content url -- always browsable in the same shape
    is_new: bool


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
        payload: ContentFoundPayload | None = None,
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
        payload: ContentFoundPayload | None = None,
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
    def send_extraction_error(cls, message: str):
        cls.send(MessageType.POTENTIAL_PROGRESS, priority=MessagePriority.ERROR)
        cls.send(MessageType.TEXT, message, MessagePriority.ERROR)

    @classmethod
    def send_download_error(cls, message: str):
        cls.send(MessageType.POTENTIAL_PROGRESS, priority=MessagePriority.ERROR)
        cls.send(MessageType.TEXT, message, MessagePriority.ERROR)
