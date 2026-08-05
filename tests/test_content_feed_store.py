from DownloaderForReddit.messaging.content_feed_store import ContentFeedStore
from DownloaderForReddit.messaging.message import ContentFoundPayload


def payload(reddit_id):
    return ContentFoundPayload(
        reddit_id=reddit_id,
        author="example_user",
        subreddit="example_subreddit",
        permalink=f"/r/example_subreddit/comments/{reddit_id}/x/",
        is_new=True,
    )


def test_add_returns_false_for_a_repeat_within_the_window():
    store = ContentFeedStore(max_entries=3)

    store.add(payload("a"))

    assert store.add(payload("a")) is False


def test_add_returns_true_once_a_repeat_has_aged_out_of_the_window():
    store = ContentFeedStore(max_entries=3)
    store.add(payload("a"))
    store.add(payload("b"))
    store.add(payload("c"))

    store.add(payload("d"))

    assert store.add(payload("a")) is True
