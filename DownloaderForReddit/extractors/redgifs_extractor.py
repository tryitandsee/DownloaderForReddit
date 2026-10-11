import re
import threading
from typing import ClassVar
from urllib.parse import urlsplit

import redgifs
from redgifs.errors import HTTPException, RedGifsError

from ..core.download import HEADERS
from ..core.errors import Error
from .base_extractor import BaseExtractor

# Post urls carry query strings, trailing junk and image extensions (i.redgifs.com/i/<id>.jpg);
# the api only accepts the bare lowercase id.
GIF_ID_RE = re.compile(r"redgifs\.com/(?:watch|ifr|i)/([a-z0-9]+)", re.IGNORECASE)


class RedgifsExtractor(BaseExtractor):
    url_key: ClassVar[list[str]] = ["redgifs"]

    # redgifs.API().login() fetches a brand new temporary token every call (the package never
    # caches one) -- extraction runs on a ThreadPoolExecutor, so every concurrent post extraction
    # was hitting redgifs' auth endpoint fresh. Share one authenticated client across every
    # instance/thread instead, matching how other tools (e.g. gallery-dl) reuse a single token.
    # Reference: https://github.com/mikf/gallery-dl/blob/master/gallery_dl/extractor/redgifs.py
    _api = None
    _api_lock = threading.Lock()

    def __init__(self, post, **kwargs):
        """
        An extractor class that interacts exclusively with the redgifs website.
        """
        super().__init__(post, **kwargs)

    @classmethod
    def _get_api(cls, force_relogin=False):
        with cls._api_lock:
            if cls._api is None or force_relogin:
                cls._api = redgifs.API()
                cls._api.login()
            return cls._api

    def extract_content(self):
        gif_id = self.get_gif_id(self.url)
        if gif_id is None:
            self.handle_failed_extract(
                error=Error.FAILED_TO_LOCATE, message="No redgifs id in url"
            )
            return
        try:
            api = self._get_api()
            try:
                response = api.get_gif(gif_id)
            except HTTPException as exc:
                if exc.status != 401:
                    raise
                # Shared token expired or was invalidated by redgifs -- re-login once and retry.
                api = self._get_api(force_relogin=True)
                response = api.get_gif(gif_id)
            url = self.get_download_url(response)
            extension = urlsplit(url).path.rsplit(".", 1)[-1]
            content = self.make_content(url, extension)
            if content is not None:
                HEADERS[content.id] = api.http.headers
        # redgifs errors derive from BaseException, so `except Exception` alone misses them.
        except (Exception, RedGifsError) as exc:
            error = Error.FAILED_TO_LOCATE
            if isinstance(exc, HTTPException) and exc.status in (404, 410):
                error = Error.DOES_NOT_EXIST
            elif isinstance(exc, HTTPException) and exc.status == 429:
                error = Error.RATE_LIMIT_ERROR
            elif isinstance(exc, HTTPException) and exc.status == 403:
                # Temporary on redgifs; see RETRYABLE_FORBIDDEN_HOSTS in core/download/downloader.py.
                error = Error.UNSUCCESSFUL_RESPONSE
            self.handle_failed_extract(
                error=error,
                message="Failed to extract content from redgifs",
                extractor_error_message=str(exc),
                gif_id=gif_id,
            )

    @staticmethod
    def get_gif_id(url):
        match = GIF_ID_RE.search(url)
        return match.group(1).lower() if match else None

    @staticmethod
    def get_download_url(data):
        try:
            return data.urls.hd
        except KeyError:
            return data.urls.sd

    # def extract_with_yt_dlp(self):
    #     """
    #     This method is out of date at the moment.  But with the way redgifs is changing their api, it could very well
    #     be relevant again soon.  So this is staying here, just in case.
    #     """
    #     try:
    #         with YoutubeDL({'format': 'mp4'}) as ydl:
    #             result = ydl.extract_info(self.url, download=False)
    #             content = self.make_content(result['url'], 'mp4')
    #             HEADERS[content.id] = result['http_headers']
    #     except:
    #         message = 'Failed to locate content'
    #         self.handle_failed_extract(error=Error.FAILED_TO_LOCATE, message=message, extractor_error_message=message)
