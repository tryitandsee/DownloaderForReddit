from collections import deque

from .message import ContentFoundPayload

MAX_ENTRIES = 200


class ContentFeedStore:
    """
    Plain, Qt-free store of recently discovered content -- deliberately has no QObject base so a
    future headless consumer can drain the message queue and call add() directly, the same way
    the GUI's handle_content_found does.
    """

    def __init__(self, max_entries: int = MAX_ENTRIES):
        self._seen_ids: set[str] = set()
        self.entries: deque[ContentFoundPayload] = deque(maxlen=max_entries)

    def add(self, payload: ContentFoundPayload) -> bool:
        if payload.reddit_id in self._seen_ids:
            return False
        if len(self.entries) == self.entries.maxlen:
            evicted = self.entries[0]
            self._seen_ids.discard(evicted.reddit_id)
        self._seen_ids.add(payload.reddit_id)
        self.entries.append(payload)
        return True
