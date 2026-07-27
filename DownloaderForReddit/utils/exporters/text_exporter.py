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

logger = logging.getLogger(__name__)


def export_posts_to_text(post_list, file_path):
    """
    Exports the supplied list of posts to a text file.
    :param post_list: A list of posts that are to be exported to a text file.
    :param file_path: The path at which the text file will be created.
    """
    with open(file_path, mode='a', encoding='utf-8') as file:
        for post in post_list:
            post_serial = format_post_output(post)
            file.write(post_serial + '\n\n')
    logger.info('Exported posts to text file', extra={'export_count': len(post_list)})


def format_post_output(post):
    """
    Formats the attributes of the supplied post into a format that is easy to read from a text file.
    :param post: The post that is to be formatted.
    :return: The supplied posts attributes in a readable formatted string.
    """
    return f'Author: {post.author}\nSubreddit: {post.subreddit}\nTitle: {post.title}\nCreated: {post.date_posted}\nUrl: {post.url}\nStatus: {post.status}\nSave Status: {post.save_status}'


def export_url_list(url_list, file_path):
    """
    Exports a list of urls to a text file.
    :param url_list: A list of urls that are to be exported to a text file.
    :param file_path: The path at which the text file will be created.
    """
    with open(file_path, 'a') as file:
        file.writelines(f'{url}\n' for url in url_list)
    logger.info('Exported url list to text file', extra={'export_count': len(url_list)})


def export_reddit_objects_to_text(object_list, file_path):
    """
    Exports a list of names in the supplied object list to a text file.
    :param object_list: A list of reddit objects who's names are to be exported to a text file.
    :param file_path: The path at which the text file will be created.
    """
    with open(file_path, mode='a', encoding='utf-8') as file:
        file.writelines(ro.name + '\n' for ro in object_list)
    logger.info('Exported reddit objects to text file', extra={'export_count': len(object_list)})
