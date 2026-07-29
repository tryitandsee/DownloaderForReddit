import pytest

from DownloaderForReddit.core import reddit_source


@pytest.fixture(autouse=True)
def never_launch_a_browser(monkeypatch):
    """The test suite must never start Playwright: it would take over the persistent
    browser_profile the real app holds a lock on, and hit reddit from a test run."""

    def fail(*args, **kwargs):
        raise AssertionError("a test tried to launch a browser")

    monkeypatch.setattr(reddit_source, "sync_playwright", fail)
