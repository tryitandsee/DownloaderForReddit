"""
Downloader for Reddit takes a list of reddit users and subreddits and downloads content posted to reddit either by the
users or on the subreddits.


Copyright (C) 2017, Kyle Hickey


This file is part of the Downloader for Reddit.

Downloader for Reddit is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

Downloader for Reddit is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with Downloader for Reddit.  If not, see <http://www.gnu.org/licenses/>.
"""

import logging
from collections import namedtuple
from urllib.parse import urlsplit

import praw

from ..version import __version__
from . import system_util

USER_AGENT = (
    f'{system_util.get_platform_str()}:DownloaderForReddit:{__version__} '
    f'(by /u/MalloyDelacroix; contact: downloaderforreddit@gmail.com)'
)
CLIENT_ID = 'frGEUVAuHGL2PQ'


logger = logging.getLogger(f'DownloaderForReddit.{__name__}')
ValidationSet = namedtuple('ValidationSet', 'name date_created valid')
_token = None


def extract_name_from_text(text, object_type):
    """Reduce a pasted profile/subreddit URL (any reddit domain variant) down to the bare
    name, so a user/subreddit URL entered instead of a plain name doesn't get validated or
    stored as the literal URL string."""
    text = text.strip()
    parts = urlsplit(text)
    if 'reddit.com' not in parts.netloc:
        return text
    segments = [s for s in parts.path.split('/') if s]
    prefixes = ('u', 'user') if object_type == 'USER' else ('r',)
    if len(segments) >= 2 and segments[0] in prefixes:
        return segments[1]
    return text


def get_reddit_instance():
    if _token is not None:
        return praw.Reddit(client_id=CLIENT_ID, user_agent=USER_AGENT, client_secret=None, refresh_token=_token)
    return praw.Reddit(client_id=CLIENT_ID, user_agent=USER_AGENT, client_secret=None)


def get_post_author_name(praw_post):
    """
    Handles an exception thrown in the event that a post retrieved from reddit does not have an author attribute.
    """
    try:
        return praw_post.author.name
    except AttributeError:
        return 'Unable to find author name'


def get_post_sub_name(praw_post):
    """
    Handles an exception thrown in the event that a post retrieved from reddit does not have a subreddit attribute.
    """
    try:
        return praw_post.subreddit.display_name
    except AttributeError:
        return 'Unable to find subreddit name'


class NameChecker:

    """
    This class is to check for the existence of a reddit object name using the browser-based reddit source, then
    report back whether the name exists or not.  This class is intended to be ran in a separate thread.

    date_created is always None: BrowserRedditSource's validate methods only check page text for a 404/private/
    suspended page (see reddit_source.py); they don't scrape account/subreddit creation date. name is returned
    as-supplied rather than reddit's corrected capitalization, for the same reason. Both are accepted, pre-existing
    tolerated values elsewhere in this codebase (see reddit_object_creator.py).
    """

    def __init__(self, object_type):
        """
        Initializes the NameChecker and establishes its operation setup regarding whether to target users or subreddits.
        :param object_type: The type of reddit object (USER or SUBREDDIT) that the supplied names will be.
        :type object_type: str, None
        """
        super().__init__()
        self.logger = logging.getLogger(f'DownloaderForReddit.{__name__}')
        from . import injector
        self.source = injector.get_reddit_source()
        self.continue_run = True
        self.object_type = object_type

    def check_reddit_object_name(self, name):
        if self.object_type == 'USER':
            return self.check_user_name(name)
        return self.check_subreddit_name(name)

    def check_user_name(self, name):
        name = extract_name_from_text(name, 'USER')
        try:
            result = self.source.validate_user(name)
            return ValidationSet(name=name, date_created=None, valid=result.valid)
        except:
            self.logger.exception('Unable to validate user name', extra={'user_name': name})
            return ValidationSet(name=name, date_created=None, valid=False)

    def check_subreddit_name(self, name):
        name = extract_name_from_text(name, 'SUBREDDIT')
        try:
            result = self.source.validate_subreddit(name)
            return ValidationSet(name=name, date_created=None, valid=result.valid)
        except:
            self.logger.exception('Unable to validate subreddit name', extra={'subreddit_name': name})
            return ValidationSet(name=name, date_created=None, valid=False)
