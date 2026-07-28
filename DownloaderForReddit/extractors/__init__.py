# ruff: noqa: N999, I001
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

# Import each extractor class in the Extractors package so that BaseExtractor.__subclasses__() will pick up the
# extractor class to be used in the Extractor.assign_extractor method. GenericVideoExtractor matches URLs against a
# ~1000-entry list of short yt-dlp site keys via substring containment, which can coincidentally match URLs meant
# for a dedicated extractor (e.g. a redgifs slug containing a 2-letter key like "dw"). It must be imported last so
# assign_extractor's first-match-wins iteration always prefers a dedicated extractor when one applies.
from .erome_extractor import EromeExtractor
from .gfycat_extractor import GfycatExtractor
from .imgur_extractor import ImgurExtractor
from .reddit_uploads_extractor import RedditUploadsExtractor
from .reddit_video_extractor import RedditVideoExtractor
from .redgifs_extractor import RedgifsExtractor
from .vidble_extractor import VidbleExtractor
from .generic_video_extractor import GenericVideoExtractor
