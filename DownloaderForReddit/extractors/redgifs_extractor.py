import threading

import redgifs

from .base_extractor import BaseExtractor
from ..core.errors import Error
from ..core.download import HEADERS


class RedgifsExtractor(BaseExtractor):

    url_key = ['redgifs']

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
        gif_id = self.get_gif_id()
        try:
            api = self._get_api()
            try:
                response = api.get_gif(gif_id)
            except Exception:
                # Shared token may have expired or been invalidated by redgifs -- re-login once
                # and retry before giving up.
                api = self._get_api(force_relogin=True)
                response = api.get_gif(gif_id)
            url = self.get_download_url(response)
            content = self.make_content(url, 'mp4')
            if content is not None:
                HEADERS[content.id] = api.http.headers
        except Exception as exc:
            message = 'Failed to extract content from redgifs'
            self.handle_failed_extract(error=Error.FAILED_TO_LOCATE, message=message, extractor_error_message=str(exc), gif_id=gif_id)

    def get_gif_id(self):
        return self.url.rsplit('/', 1)[-1]

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
