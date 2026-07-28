import logging

from DownloaderForReddit.database.model_enums import NsfwFilter
from DownloaderForReddit.database.models import Subreddit, User

logger = logging.getLogger(f"DownloaderForReddit.{__name__}")


def import_legacy(json_element):
    logger.info("Importing from legacy json file")
    reddit_objects = []
    for ro in json_element:
        reddit_object = make_reddit_object(ro)
        if reddit_object is not None:
            reddit_objects.append(reddit_object)
    return reddit_objects


def make_reddit_object(data):
    try:
        model = User if data["object_type"] == "USER" else Subreddit
        kwargs = get_ro_kwargs(data)
        return model(**kwargs)
    except KeyError:
        print("Error")
        return None


def get_ro_kwargs(data):
    return {
        "name": data["name"],
        "avoid_duplicates": data["avoid_duplicates"],
        "download_videos": data["download_videos"],
        "download_images": data["download_images"],
        "download_nsfw": convert_download_nsfw(data["nsfw_filter"]),
        "download_enabled": data["download_enabled"],
    }


def convert_download_nsfw(nsfw):
    if nsfw == "INCLUDE":
        return NsfwFilter.INCLUDE
    if nsfw == "EXCLUDE":
        return NsfwFilter.EXCLUDE
    return NsfwFilter.ONLY
