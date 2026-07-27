from collections import deque

from .message import ContentFoundPayload

MAX_ENTRIES = 200


class ContentFeedStore:
    """
    Plain, Qt-free store of recently discovered content -- deliberately has no QObject base so a
    future headless consumer can drain the message queue and call add() directly, the same way
    the GUI's handle_content_found does.

    Suppresses repeat entries for the same reddit_id within a run: the ambient observer can
    report the same post more than once (e.g. scrolling away and back re-adds the DOM node), and
    the explicit/ambient paths can both see the same post.
    """

    def __init__(self, max_entries: int = MAX_ENTRIES):
        self._seen_ids: set[str] = set()
        self.entries: deque[ContentFoundPayload] = deque(maxlen=max_entries)

    def add(self, payload: ContentFoundPayload) -> bool:
        """Returns True if the payload was added (genuinely new this run), False if suppressed
        as a repeat of an already-reported reddit_id."""
        if payload.reddit_id in self._seen_ids:
            return False
        self._seen_ids.add(payload.reddit_id)
        self.entries.append(payload)
        return True
