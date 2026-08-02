from types import SimpleNamespace

import pytest
import requests

from DownloaderForReddit.core.download import HEADERS, request_context
from DownloaderForReddit.core.download.downloader import Downloader


@pytest.fixture(autouse=True)
def clear_module_state(monkeypatch):
    monkeypatch.setattr(request_context, "_snapshot", request_context.EMPTY_SNAPSHOT)
    monkeypatch.setattr(request_context, "_snapshot_time", None)
    HEADERS.clear()
    yield
    HEADERS.clear()


def make_cookie(**overrides):
    cookie = {
        "name": "reddit_session",
        "value": "abc123",
        "domain": ".reddit.com",
        "path": "/",
        "expires": -1,
        "secure": True,
    }
    cookie.update(overrides)
    return cookie


def sent_cookie_header(jar, url):
    """What requests would actually put on the wire for `url` -- the cookiejar's own domain,
    path, secure and expiry rules decide that, not the jar's contents."""
    request = requests.Request("GET", url).prepare()
    return requests.cookies.get_cookie_header(jar, request)


def test_build_cookie_jar_sends_a_session_cookie_to_its_own_domain():
    jar = request_context.build_cookie_jar([make_cookie()])

    header = sent_cookie_header(jar, "https://www.reddit.com/")

    assert header == "reddit_session=abc123"


def test_build_cookie_jar_does_not_send_a_reddit_cookie_to_another_host():
    jar = request_context.build_cookie_jar([make_cookie()])

    header = sent_cookie_header(jar, "https://i.imgur.com/abc.jpg")

    assert header is None


def test_build_cookie_jar_does_not_send_a_secure_cookie_over_http():
    jar = request_context.build_cookie_jar([make_cookie()])

    header = sent_cookie_header(jar, "http://www.reddit.com/")

    assert header is None


def test_build_cookie_jar_keeps_a_cookie_with_a_future_expiry():
    jar = request_context.build_cookie_jar([make_cookie(expires=4_000_000_000)])

    header = sent_cookie_header(jar, "https://www.reddit.com/")

    assert header == "reddit_session=abc123"


def test_build_cookie_jar_drops_a_cookie_that_has_already_expired():
    jar = request_context.build_cookie_jar([make_cookie(expires=1)])

    header = sent_cookie_header(jar, "https://www.reddit.com/")

    assert header is None


def make_content(url, post_url=None):
    post = SimpleNamespace(url=post_url) if post_url is not None else None
    return SimpleNamespace(url=url, post=post)


def test_content_referer_uses_reddit_for_reddit_hosted_media():
    content = make_content("https://i.redd.it/abc.jpg", post_url="https://reddit.com/x")

    assert request_context.content_referer(content) == request_context.REDDIT_REFERER


def test_content_referer_uses_erome_for_erome_media():
    content = make_content("https://s11.erome.com/abc.mp4")

    assert request_context.content_referer(content) == request_context.EROME_REFERER


def test_content_referer_prefers_the_post_url_the_media_was_extracted_from():
    content = make_content(
        "https://i.imgur.com/abc.jpg", post_url="https://imgur.com/a/xyz"
    )

    assert request_context.content_referer(content) == "https://imgur.com/a/xyz"


def test_content_referer_falls_back_to_the_content_urls_own_origin():
    content = make_content("https://i.imgur.com/abc.jpg")

    assert request_context.content_referer(content) == "https://i.imgur.com/"


class FakeRedditSource:
    def __init__(self, user_agent="Mozilla/5.0 Chrome", cookies=None):
        self.user_agent = user_agent
        self.cookies = cookies or []
        self.call_count = 0

    def get_request_context(self):
        self.call_count += 1
        return self.user_agent, self.cookies


def test_request_args_sends_the_browser_user_agent_and_referer(monkeypatch):
    source = FakeRedditSource()
    monkeypatch.setattr(request_context.injector, "peek_reddit_source", lambda: source)

    args = request_context.request_args("https://i.redd.it/abc.jpg", "https://x.com/")

    assert args["headers"] == {
        "User-Agent": "Mozilla/5.0 Chrome",
        "Referer": "https://x.com/",
    }


def test_request_args_is_empty_when_no_browser_is_running(monkeypatch):
    monkeypatch.setattr(request_context.injector, "peek_reddit_source", lambda: None)

    args = request_context.request_args("https://i.redd.it/abc.jpg")

    assert args["headers"] == {}
    assert len(args["cookies"]) == 0


def test_request_args_reuses_the_cached_snapshot_rather_than_re_reading_the_browser(
    monkeypatch,
):
    source = FakeRedditSource()
    monkeypatch.setattr(request_context.injector, "peek_reddit_source", lambda: source)

    request_context.request_args("https://i.redd.it/a.jpg")
    request_context.request_args("https://i.redd.it/b.jpg")

    assert source.call_count == 1


def test_request_args_falls_back_to_the_last_snapshot_when_the_browser_read_fails(
    monkeypatch,
):
    source = FakeRedditSource()
    monkeypatch.setattr(request_context.injector, "peek_reddit_source", lambda: source)
    request_context.request_args("https://i.redd.it/a.jpg")
    monkeypatch.setattr(request_context, "_snapshot_time", None)
    monkeypatch.setattr(
        source, "get_request_context", lambda: (_ for _ in ()).throw(RuntimeError())
    )

    args = request_context.request_args("https://i.redd.it/b.jpg")

    assert args["headers"]["User-Agent"] == "Mozilla/5.0 Chrome"


def test_request_args_lets_an_extractor_user_agent_win_over_the_browsers(monkeypatch):
    """Lowercase key on purpose: redgifs hands over a whole requests Session header dict, and
    the collapse that decides the winner is CaseInsensitiveDict's, at prepare() time."""
    source = FakeRedditSource()
    monkeypatch.setattr(request_context.injector, "peek_reddit_source", lambda: source)
    content = make_content("https://redgifs.com/abc.mp4")
    content.id = 7
    HEADERS[7] = {"user-agent": "redgifs/1.0", "authorization": "Bearer x"}

    args = Downloader.__new__(Downloader).request_args(content)
    prepared = requests.Request("GET", content.url, **args).prepare()

    assert prepared.headers["User-Agent"] == "redgifs/1.0"
    assert prepared.headers["Authorization"] == "Bearer x"
