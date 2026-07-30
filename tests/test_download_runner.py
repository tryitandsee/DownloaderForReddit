from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event

from DownloaderForReddit.core import download_runner as download_runner_module
from DownloaderForReddit.core.download_runner import DownloadRunner
from DownloaderForReddit.messaging.message import Message


def make_runner():
    """DownloadRunner.__init__ reaches into injector-backed singletons (DB, settings,
    reddit_source) that this test has no business touching -- run_paced_bulk_download and _pace
    only read the instance state set up below. stop_requested is wired up manually since the
    continue_run property reads/writes it directly."""
    runner = DownloadRunner.__new__(DownloadRunner)
    runner.stop_requested = Event()
    return runner


def make_bulk_download_runner():
    """run_paced_bulk_download calls self._pace() between objects -- stubbed here since these
    tests cover which objects get downloaded and in what order, not the queue-drain wait between
    them (that's test_pace_* below, which needs the real _pace)."""
    runner = make_runner()
    runner._pace = lambda: None
    return runner


class FakeDB:
    def __init__(self, session):
        self._session = session

    @contextmanager
    def get_scoped_session(self):
        yield self._session


class FakeObject:
    def __init__(self, obj_id, name, date_last_download_utc=None):
        self.id = obj_id
        self.name = name
        self.date_last_download_utc = date_last_download_utc


class FakeQuery:
    def __init__(self, objects_by_id):
        self._objects_by_id = objects_by_id

    def get(self, obj_id):
        return self._objects_by_id.get(obj_id)


class FakeSession:
    def __init__(self, objects):
        self._objects_by_id = {obj.id: obj for obj in objects}

    def query(self, _model_class):
        return FakeQuery(self._objects_by_id)


class FakeQueue:
    def __init__(self, empty=True):
        self._empty = empty

    def empty(self):
        return self._empty


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC).replace(tzinfo=None)


def test_run_paced_bulk_download_downloads_objects_never_downloaded():
    runner = make_bulk_download_runner()
    session = FakeSession([FakeObject(1, "alice"), FakeObject(2, "bob")])
    runner.db = FakeDB(session)
    calls = []

    runner.run_paced_bulk_download(FakeObject, [1, 2], lambda obj_id, progress: calls.append((obj_id, progress)))

    assert calls == [(1, (1, 2)), (2, (2, 2))]


def test_run_paced_bulk_download_skips_object_within_cooldown_without_counting_toward_cap(
    monkeypatch,
):
    monkeypatch.setattr(download_runner_module, "datetime", _FixedDatetime)
    monkeypatch.setattr(download_runner_module.const, "BULK_DOWNLOAD_LIMIT", 1)
    warnings = []
    monkeypatch.setattr(Message, "send_warning", lambda message: warnings.append(message))

    runner = make_bulk_download_runner()
    recently_downloaded = FakeObject(1, "alice", date_last_download_utc=NOW - timedelta(hours=1))
    never_downloaded = FakeObject(2, "bob")
    runner.db = FakeDB(FakeSession([recently_downloaded, never_downloaded]))
    calls = []

    runner.run_paced_bulk_download(FakeObject, [1, 2], lambda obj_id, progress: calls.append((obj_id, progress)))

    assert calls == [(2, (1, 1))]
    assert len(warnings) == 1
    assert "alice" in warnings[0]
    assert "too recently" in warnings[0]


def test_run_paced_bulk_download_stops_once_cap_reached(monkeypatch):
    monkeypatch.setattr(download_runner_module.const, "BULK_DOWNLOAD_LIMIT", 2)

    runner = make_bulk_download_runner()
    objects = [FakeObject(i, f"user{i}") for i in range(1, 5)]
    runner.db = FakeDB(FakeSession(objects))
    calls = []

    runner.run_paced_bulk_download(FakeObject, [1, 2, 3, 4], lambda obj_id, progress: calls.append((obj_id, progress)))

    assert calls == [(1, (1, 2)), (2, (2, 2))]


def test_run_paced_bulk_download_downloads_object_whose_cooldown_has_elapsed(monkeypatch):
    monkeypatch.setattr(download_runner_module, "datetime", _FixedDatetime)

    runner = make_bulk_download_runner()
    long_ago = FakeObject(1, "alice", date_last_download_utc=NOW - timedelta(days=1))
    runner.db = FakeDB(FakeSession([long_ago]))
    calls = []

    runner.run_paced_bulk_download(FakeObject, [1], lambda obj_id, progress: calls.append((obj_id, progress)))

    assert calls == [(1, (1, 1))]


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.replace(tzinfo=tz) if tz else NOW


def test_pace_returns_once_queues_and_futures_drain(monkeypatch):
    sleeps = []
    monkeypatch.setattr(download_runner_module.time, "sleep", sleeps.append)

    runner = make_runner()
    runner.continue_run = True
    runner.extractor = type("Extractor", (), {"futures": []})()
    runner.downloader = type("Downloader", (), {"futures": []})()
    runner.submission_queue = FakeQueue(empty=True)
    runner.download_queue = FakeQueue(empty=True)

    runner._pace()

    assert len(sleeps) == 1


def test_pace_polls_until_futures_drain(monkeypatch):
    sleeps = []
    monkeypatch.setattr(download_runner_module.time, "sleep", sleeps.append)

    runner = make_runner()
    runner.continue_run = True
    remaining_futures = [1, 1, 0]

    class DrainingExtractor:
        @property
        def futures(self):
            return [] if not remaining_futures or remaining_futures.pop(0) == 0 else [1]

    runner.extractor = DrainingExtractor()
    runner.downloader = type("Downloader", (), {"futures": []})()
    runner.submission_queue = FakeQueue(empty=True)
    runner.download_queue = FakeQueue(empty=True)

    runner._pace()

    assert len(sleeps) == 3


def test_pace_exits_early_when_continue_run_goes_false(monkeypatch):
    sleeps = []
    monkeypatch.setattr(download_runner_module.time, "sleep", sleeps.append)

    runner = make_runner()
    runner.continue_run = False
    runner.extractor = type("Extractor", (), {"futures": [1]})()
    runner.downloader = type("Downloader", (), {"futures": []})()
    runner.submission_queue = FakeQueue(empty=False)
    runner.download_queue = FakeQueue(empty=False)

    runner._pace()

    assert sleeps == []


def test_stop_requested_set_directly_is_seen_without_calling_stop_download():
    """Regression test: stop_download() only ever runs via a queued cross-thread Qt signal, so a
    paced run sitting in _pace can't wait on it -- the GUI's Stop/Terminate handlers instead set
    runner.stop_requested directly (see request_stop in gui/downloader_for_reddit_gui.py).
    continue_run must observe that immediately, with nothing else in between."""
    runner = make_runner()
    assert runner.continue_run is True

    runner.stop_requested.set()

    assert runner.continue_run is False


class FakeSubmission:
    def __init__(self, reddit_id="abc123", url="https://example.com/x", author="alice",
                 subreddit="pics", permalink="/r/pics/comments/abc123/x/"):
        self.reddit_id = reddit_id
        self.url = url
        self.author = author
        self.subreddit = subreddit
        self.permalink = permalink


class FakeUpdateSession:
    def __init__(self, existing_post):
        self._existing_post = existing_post

    def query(self, _model_class):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._existing_post


class FakeDb:
    """Stands in for injector.get_database_handler(): _finalize_submission only ever needs
    get_scoped_update_session, never get_scoped_session (that's prepare_submission's job of
    resolving which tracked object a submission belongs to, not exercised by these tests)."""

    def __init__(self, existing_post=None):
        self._existing_post = existing_post

    @contextmanager
    def get_scoped_update_session(self):
        yield FakeUpdateSession(self._existing_post)


class FakePutQueue:
    def __init__(self):
        self.put_calls = []

    def put(self, item):
        self.put_calls.append(item)


def test_queue_submissions_reports_content_found_and_enqueues_new_post(monkeypatch):
    """Regression test: queue_submissions used to build an ExtractionSet and enqueue it directly,
    so a bulk "download N users/subreddits" run never reported anything to the content feed panel
    -- only ambient matches and explicit single-post downloads did, via prepare_submission. Bulk
    downloads must go through the same _finalize_submission "see content, filter, download" step."""
    found = []
    monkeypatch.setattr(Message, "send_content_found", lambda payload: found.append(payload))

    runner = make_runner()
    runner.db = FakeDb(existing_post=None)
    runner.download_session_id = 42
    runner.submission_queue = FakePutQueue()
    reddit_object = FakeObject(7, "pics")
    submission = FakeSubmission()

    runner.queue_submissions(reddit_object, [submission])

    assert len(found) == 1
    assert found[0].is_new is True
    assert len(runner.submission_queue.put_calls) == 1
    extraction_set = runner.submission_queue.put_calls[0]
    assert extraction_set.significant_id == 7
    assert extraction_set.download_session_id == 42


def test_queue_submissions_reports_but_skips_enqueue_for_duplicate_post(monkeypatch):
    found = []
    monkeypatch.setattr(Message, "send_content_found", lambda payload: found.append(payload))

    runner = make_runner()
    runner.db = FakeDb(existing_post=object())
    runner.download_session_id = 42
    runner.submission_queue = FakePutQueue()
    reddit_object = FakeObject(7, "pics")
    submission = FakeSubmission()

    runner.queue_submissions(reddit_object, [submission])

    assert len(found) == 1
    assert found[0].is_new is False
    assert runner.submission_queue.put_calls == []
