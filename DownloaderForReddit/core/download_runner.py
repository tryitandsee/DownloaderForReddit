import logging
import platform
import re
import time
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from queue import Queue
from threading import Event, Thread
from typing import cast

from playwright.sync_api import Error as PlaywrightError
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from DownloaderForReddit.core.download.downloader import Downloader

from ..database.models import (
    Content,
    DownloadSession,
    Post,
    RedditObject,
    Subreddit,
    User,
)
from ..messaging.message import ContentFoundPayload, Message
from ..utils import injector, video_merger
from ..version import __version__
from . import const
from .content_runner import ContentRunner
from .errors import PERMANENT_ERRORS
from .reddit_source import RateLimitedError, StopRequestedError, ValidationError
from .runner import verify_run
from .submission_filter import SubmissionFilter

ExtractionSet = namedtuple(
    "ExtractionSet",
    "extraction_type extraction_object significant_id download_session_id",
)

# Matches the base36 post id out of any reddit permalink/comments url, the same id
# Post.reddit_id stores -- lets prepare_single_submission check for a duplicate before
# spending a browser navigation on a post already in the database.
_POST_ID_RE = re.compile(r"/comments/([A-Za-z0-9]+)")

# [mine] TODO: stub for a future "force download" GUI toggle -- hardcoded on for now to
# re-test RedditUploadsExtractor's mp4 fetch against an already-downloaded post without
# fighting Post.reddit_id's uniqueness constraint. See prepare_submission below.
FORCE_DOWNLOAD = False


class DownloadRunner(QObject):
    """
    Owns the extraction/download thread pool and queues for the process lifetime. A single
    instance is created once (see main.py) and moved to its own QThread; explicit downloads and
    ambient extraction both queue work onto it via request_download rather than each constructing
    their own runner/threads/executors.
    """

    remove_invalid_object = pyqtSignal(int)
    remove_forbidden_object = pyqtSignal(int)
    download_session_signal = pyqtSignal(
        int
    )  # emits the id of a DownloadSession that just finished
    pool_idle = (
        pyqtSignal()
    )  # emitted once whenever the whole pool (all sessions) goes idle
    request_download = pyqtSignal(
        object
    )  # GUI/ambient emit a params dict; queued onto this runner's thread
    # Emitted from BrowserRedditSource's response listener, which runs on Playwright's own
    # thread -- emit() is the thread-safe hand-off onto this runner's own thread, same pattern as
    # request_download.
    rate_limited = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger(f"DownloaderForReddit.{__name__}")
        self.db = injector.get_database_handler()
        self.settings_manager = injector.get_settings_manager()
        self.reddit_source = injector.get_reddit_source()
        self.submission_filter = SubmissionFilter()

        # Governs the pool threads only; set once, at real app shutdown (see stop_pool). A
        # Stop/Terminate click must not touch this -- it has to keep servicing later sessions.
        self.stop_run = Event()
        # Backs the continue_run property below. A plain bool wouldn't do -- stop_download() is
        # only ever invoked via a queued cross-thread signal (this runner lives on its own
        # QThread), so it can't run until whatever's currently executing on that thread returns
        # control to the Qt event loop. A paced bulk run (see _pace) can occupy
        # that thread in a plain Python time.sleep loop for minutes, so continue_run has to be
        # something the GUI thread can flip directly and immediately -- a threading.Event's
        # internal lock needs no Qt dispatch to be visible cross-thread.
        self.stop_requested = Event()
        self.cancelled_sessions = set()
        self.hard_stopped_sessions = set()

        self.submission_queue = Queue(maxsize=-1)
        self.extractor = None
        self.extraction_thread = None
        self.download_queue = Queue(maxsize=-1)
        self.downloader = None
        self.download_thread = None
        # Set once the pool goes idle, cleared as soon as it's busy again -- idle_tick fires on
        # every ~2s poll timeout, but its DB/merge/signal work must only run on the busy->idle
        # transition, not on every tick of an indefinitely idle pool.
        self._pool_was_idle = False
        # True for the whole span of a start_batch() call -- idle_tick runs concurrently on a
        # different thread and only sees queue/future state, so without this it could see empty
        # queues and declare idle while start_batch is still mid-navigation, before it's queued
        # anything, or while a paced run (see _pace) is sitting between objects or between
        # scrolls with both queues legitimately empty.
        self._batch_in_progress = False

        # Per-batch state below, reset at the top of every start_batch() call -- safe as plain
        # instance attributes because start_batch is only ever invoked serially (queued onto this
        # object's own thread via request_download), never reentrantly.
        self.user_id_list = None
        self.subreddit_id_list = None
        self.reddit_object_id_list = None
        self.run_unextracted = False
        self.unextracted_id_list = None
        self.run_undownloaded = False
        self.undownloaded_id_list = None
        self.run_new = True
        self.single_submission_urls = None
        self.submissions = None

        self.continue_run = True
        self.stopped = False
        self.filter_subreddits = False
        self.validated_subreddits = []
        self.failed_connection_attempts = 0
        self.download_session_id = None
        self._current_fetch_object = None

        self.request_download.connect(self.start_batch)
        self.rate_limited.connect(self.handle_rate_limited)
        self.reddit_source.set_on_rate_limited(self.rate_limited.emit)
        # Reuses the exact pacing already built for between-object waits (see
        # run_paced_bulk_download) to also pace individual scrolls within one object's listing --
        # one pacing mechanism, not a separate one for each.
        self.reddit_source.set_scroll_pacer(self._pace)
        # Gives reddit_source direct, thread-safe read access to the same Event request_stop sets
        # -- continue_run is only checked between objects/scrolls, never during a page.goto/
        # mouse.wheel call itself (most of a download's wall-clock time), so without this a Stop
        # click has no effect until whatever navigation is currently in flight finishes on its own.
        self.reddit_source.set_stop_event(self.stop_requested)

    @property
    def continue_run(self):
        return not self.stop_requested.is_set()

    @continue_run.setter
    def continue_run(self, value):
        if value:
            self.stop_requested.clear()
        else:
            self.stop_requested.set()

    def start_pool(self):
        """Creates the extractor/downloader and their worker threads. Called once, at app start."""
        self.extractor = ContentRunner(
            self.submission_queue,
            self.download_queue,
            self.cancelled_sessions,
            self.stop_run,
            self.idle_tick,
        )
        self.extraction_thread = Thread(target=self.extractor.run)
        self.extraction_thread.start()
        self.downloader = Downloader(
            self.download_queue,
            self.cancelled_sessions,
            self.hard_stopped_sessions,
            self.stop_run,
        )
        self.download_thread = Thread(target=self.downloader.run)
        self.download_thread.start()

    def stop_pool(self):
        """Shuts the pool down for good. Called once, from close(), at real app shutdown."""
        self.stop_run.set()
        self.submission_queue.put(None)
        if self.extraction_thread is not None:
            self.extraction_thread.join()
        if self.download_thread is not None:
            self.download_thread.join()

    def idle_tick(self):
        """
        Called from ContentRunner's ~2s poll timeout on its own worker thread. Detects when the
        whole pool (every open session, explicit or ambient) has drained, stamps end_time for any
        session left open, and reports the pool as idle. Coarse-grained by design -- see
        PLAN_background_download.md -- exact per-item timing isn't needed by anything.
        """
        assert self.extractor is not None
        assert self.downloader is not None
        idle = (
            not self._batch_in_progress
            and not self.extractor.futures
            and not self.downloader.futures
            and self.submission_queue.empty()
            and self.download_queue.empty()
        )
        if not idle:
            self._pool_was_idle = False
            return
        if self._pool_was_idle:
            return
        self._pool_was_idle = True
        self._current_fetch_object = None  # [mine] feat(gui): download status window
        video_merger.merge_videos()
        with self.db.get_scoped_session() as session:
            open_sessions = (
                session.query(DownloadSession)
                .filter(DownloadSession.end_time == None)
                .all()
            )
            finished_ids = [dl_session.id for dl_session in open_sessions]
            for dl_session in open_sessions:
                dl_session.end_time = datetime.now()
            session.commit()
        for session_id in finished_ids:
            self.cancelled_sessions.discard(session_id)
            self.hard_stopped_sessions.discard(session_id)
            self.download_session_signal.emit(session_id)
        if finished_ids:
            self.logger.debug(
                "Download pool idle", extra={"finished_sessions": finished_ids}
            )
        self.pool_idle.emit()

    def validate_subreddit(self, subreddit_obj):
        result = self.reddit_source.validate_subreddit(subreddit_obj.name)
        return self.validate_object(result, subreddit_obj)

    def validate_object(self, result, reddit_object):
        # [mine] fix(core): active now tracks whether the dedicated account follows a User --
        # validation no longer touches it, since "exists on reddit" and "is followed" are
        # unrelated facts.
        if result.valid:
            Message.send_debug(
                f"{reddit_object.name} exists and is reachable on Reddit"
            )
            return True
        if result.error == ValidationError.NOT_FOUND:
            self.handle_invalid_reddit_object(reddit_object)
        elif result.error == ValidationError.FORBIDDEN:
            self.handle_forbidden_reddit_object(reddit_object)
        elif result.error == ValidationError.RATE_LIMITED:
            self.handle_too_many_requests_error(reddit_object)
        elif result.error == ValidationError.CONNECTION_ERROR:
            self.handle_failed_connection()
        else:
            self.handle_unknown_error(reddit_object)
        return False

    def handle_invalid_reddit_object(self, reddit_object):
        self.logger.warning(
            "Invalid reddit object detected",
            extra={
                "object_type": reddit_object.object_type,
                "reddit_object": reddit_object.name,
            },
        )
        Message.send_warning(
            f"Invalid {reddit_object.object_type.lower()}: {reddit_object.name}"
        )
        self.remove_invalid_object.emit(reddit_object.id)

    def handle_forbidden_reddit_object(self, reddit_object):
        self.logger.warning(
            "Forbidden reddit object detected",
            extra={
                "object_type": reddit_object.object_type,
                "reddit_object": reddit_object.name,
            },
        )
        Message.send_warning(
            f"Forbidden {reddit_object.object_type.lower()}: {reddit_object.name}"
        )
        self.remove_forbidden_object.emit(reddit_object.id)

    def handle_failed_connection(self):
        if self.failed_connection_attempts >= 3:
            self.continue_run = False
            self.logger.error(
                "Failed connection attempts exceeded.  Ending download session"
            )
            Message.send_critical(
                "Failed connection attempts exceeded.  The download session has been canceled.  "
                "Please try the download again later."
            )
        else:
            self.logger.error(
                "Failed to connect to reddit",
                extra={"connection_attempts": self.failed_connection_attempts},
            )
            Message.send_error(
                f"Failed to connect to reddit.  Connection attempts remaining: "
                f"{3 - self.failed_connection_attempts}"
            )
            self.failed_connection_attempts += 1

    def handle_rate_limited(self):
        """Connected to rate_limited, emitted the moment reddit returns an HTTP 429 anywhere
        (bulk downloads, ambient-triggered extraction, gallery metadata fetches). Cancels the open
        session immediately rather than letting queued work keep hitting a rate-limited endpoint.
        There's no auto-resume/cooldown -- clear_rate_limit() runs at the top of the next
        user-initiated start_batch, so starting another download is the resume signal."""
        self.logger.error("Reddit rate limit (429) reached, pausing downloads")
        Message.send_critical(
            "Reddit rate limit reached (HTTP 429). Downloading has been paused.\n"
            "Please wait a few minutes before starting another download."
        )
        self.stop_download(hard_stop=True)

    def handle_too_many_requests_error(self, reddit_object):
        self.logger.error("Too many requests error")
        message = (
            f"Reddit rate limit reached.  {reddit_object.object_type.capitalize()} ({reddit_object.name}) could "
            f"not be validated.  Please try again later.\n"
            f"For More information about this error, please visit the link below:\n"
            f"{const.RATE_LIMIT_DOC_URL}"
        )
        Message.send_error(message)

    def handle_unknown_error(self, reddit_object):
        self.logger.error(
            "Failed to validate reddit object due to unknown error",
            extra={
                "object_type": reddit_object.object_type,
                "reddit_object": reddit_object.name,
            },
        )

    def run_unextracted_posts(self):
        self.logger.debug("Running unextracted posts")
        post_id_list = self.unextracted_id_list
        if post_id_list is None:
            with self.db.get_scoped_session() as session:
                post_id_list = (
                    session.query(Post.id)
                    .filter(Post.extracted == False)
                    .filter(Post.retry_attempts <= 3)
                    .filter(
                        or_(
                            Post.extraction_error == None,
                            Post.extraction_error.notin_(PERMANENT_ERRORS),
                        )
                    )
                )
        self.logger.debug("%s unfinished posts to download", post_id_list.count())
        for (post_id,) in post_id_list.all():  # comma used to unpack result tuple
            extraction_set = ExtractionSet(
                extraction_type="POST",
                extraction_object=post_id,
                significant_id=None,
                download_session_id=self.download_session_id,
            )
            self.submission_queue.put(extraction_set)
        self.logger.debug("Finished unextracted posts")

    def run_undownloaded_content(self):
        self.logger.debug("Running undownloaded content")
        content_id_list = self.undownloaded_id_list
        if content_id_list is None:
            with self.db.get_scoped_session() as session:
                content_id_list = (
                    session.query(Content)
                    .filter(Content.downloaded == False)
                    .filter(Content.retry_attempts <= 3)
                    .filter(
                        or_(
                            Content.download_error == None,
                            Content.download_error.notin_(PERMANENT_ERRORS),
                        )
                    )
                )
        self.logger.debug(
            "%s unfinished content items to download", content_id_list.count()
        )
        for content in content_id_list.all():
            self.download_queue.put((content.id, self.download_session_id))
        self.logger.debug("Finished undownloaded content")

    @pyqtSlot(object)
    def start_batch(self, params):
        """
        Entry point for a batch of work, explicit or ambient -- replaces what used to be a fresh
        DownloadRunner's constructor + run(). Called via request_download so it always executes on
        this runner's own thread, keeping every batch serialized (safe for the per-batch instance
        state below, and matching reddit_source's own single-worker executor).
        :param params: dict of the same keys the old DownloadRunner(**kwargs) constructor took --
                       user_id_list, subreddit_id_list, reddit_object_id_list, run_unextracted,
                       unextracted_id_list, run_undownloaded, undownloaded_id_list, run_new,
                       single_submission_urls, submissions.
        """
        # A new user-initiated batch is the resume signal -- there's no automatic cooldown/resume.
        self.reddit_source.clear_rate_limit()

        self.user_id_list = params.get("user_id_list")
        self.subreddit_id_list = params.get("subreddit_id_list")
        self.reddit_object_id_list = params.get("reddit_object_id_list")
        self.run_unextracted = params.get("run_unextracted", False)
        self.unextracted_id_list = params.get("unextracted_id_list")
        self.run_undownloaded = params.get("run_undownloaded", False)
        self.undownloaded_id_list = params.get("undownloaded_id_list")
        self.run_new = params.get("run_new", True)
        self.single_submission_urls = params.get("single_submission_urls")
        self.submissions = params.get("submissions")

        self.continue_run = True
        self.stopped = False
        self.filter_subreddits = False
        self.validated_subreddits = []
        self.failed_connection_attempts = 0
        self.download_session_id = None
        # Cleared here, not just observed in idle_tick -- a batch that turns out to have no work
        # (e.g. every submission was a duplicate) never queues anything for idle_tick to see, so
        # without this the GUI's "running" state (set when this batch was requested) would never
        # be released by a pool_idle emission.
        self._pool_was_idle = False

        self._batch_in_progress = True
        try:
            # [mine] feat(core): batch single-post download mode - validate every url before creating a session
            if self.single_submission_urls is not None:
                self.run_batch(
                    self.prepare_single_submission(url)
                    for url in self.single_submission_urls
                )
                return
            if self.submissions is not None:
                self.run_batch(
                    self.prepare_submission(submission)
                    for submission in self.submissions
                )
                return
            self.run_normal()
        finally:
            self._batch_in_progress = False

    def run_batch(self, prepared_submissions):
        # Shared by single_submission_urls and submissions: validate everything first, only create
        # a DownloadSession if at least one survives.
        extraction_sets = [es for es in prepared_submissions if es is not None]
        if not extraction_sets:
            # Nothing was queued, so the pool's idle state hasn't actually changed -- but the GUI
            # already flipped into "running" mode as soon as ambient found a match, before dedup
            # ran. Report idle now instead of leaving that GUI shift to be undone by idle_tick's
            # next ~2s poll timeout.
            self.pool_idle.emit()
            return
        self.create_download_session()
        for extraction_set in extraction_sets:
            extraction_set = extraction_set._replace(
                download_session_id=self.download_session_id
            )
            self.submission_queue.put(extraction_set)

    def run_normal(self):
        self.create_download_session()
        if self.run_unextracted:
            self.run_unextracted_posts()
        if self.run_undownloaded:
            self.run_undownloaded_content()
        if self.run_new:
            self.run_download()

    def log_download_settings(self):
        self.logger.info(
            "Download runner started.",
            extra={
                "dfr_version": __version__,
                "platform": platform.platform,
                "run_unextracted": self.run_unextracted,
                "run_undownloaded": self.run_undownloaded,
                "run_new": self.run_new,
                "last_update": self.settings_manager.last_update,
                "extraction_thread_count": self.settings_manager.extraction_thread_count,
                "download_thread_count": self.settings_manager.download_thread_count,
                "multi_part_threshold": self.settings_manager.multi_part_threshold,
                "finish_incomplete_extractions": self.settings_manager.finish_incomplete_extractions_at_session_start,
                "finish_incomplete_downloads": self.settings_manager.finish_incomplete_downloads_at_session_start,
            },
        )

    def create_download_session(self):
        with self.db.get_scoped_session() as session:
            download_session = DownloadSession(
                start_time=datetime.now(),
                extraction_thread_count=self.settings_manager.extraction_thread_count,
                download_thread_count=self.settings_manager.download_thread_count,
            )
            session.add(download_session)
            session.commit()
            self.download_session_id = download_session.id

    def run_download(self):
        if self.reddit_object_id_list is not None:
            # Explicit multi-select download -- deliberately uncapped and cooldown-free, the
            # "download manually" escape hatch run_paced_bulk_download's skip message points to.
            total = len(self.reddit_object_id_list)
            for index, ro_id in enumerate(self.reddit_object_id_list):
                self.get_reddit_object_submissions(ro_id, progress=(index + 1, total))
        else:
            if self.user_id_list is not None and self.subreddit_id_list is not None:
                self.filter_subreddits = True
                self.validate_subreddit_list()
            if self.user_id_list is not None:
                self.run_paced_bulk_download(
                    User, self.user_id_list, self.get_user_submissions
                )
            else:
                self.run_paced_bulk_download(
                    Subreddit, self.subreddit_id_list, self.get_subreddit_submissions
                )

    def run_paced_bulk_download(self, model_class, id_list, get_submissions):
        """
        Bulk "download N users/subreddits" -- visits each object one at a time, in the order
        given (the object list table's current sort), capped at BULK_DOWNLOAD_LIMIT actually-
        downloaded objects and skipping (without counting toward that cap) any object whose
        date_last_download_utc checkpoint is inside BULK_DOWNLOAD_COOLDOWN_HOURS. See _pace for
        the pacing between objects (also reused between scrolls within one object's listing).
        """
        cooldown = timedelta(hours=const.BULK_DOWNLOAD_COOLDOWN_HOURS)
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.db.get_scoped_session() as session:
            objs = [
                (obj_id, session.query(model_class).get(obj_id)) for obj_id in id_list
            ]

        def eligible(obj):
            if obj is None:
                return False
            last = obj.date_last_download_utc
            return last is None or now - last >= cooldown

        total = min(
            sum(1 for _, obj in objs if eligible(obj)), const.BULK_DOWNLOAD_LIMIT
        )

        downloaded = 0
        # Suppressed for this whole loop, not just each navigation -- an automated run over up to
        # BULK_DOWNLOAD_LIMIT objects, paced BULK_DOWNLOAD_PACE_MS apart, shouldn't keep
        # yanking the tab back into the foreground while the person is doing something else, the
        # same focus-stealing ambient mode otherwise exists to avoid. A deliberate single-object
        # download (add_to_download, or a manual navigation onto a tracked listing) still does.
        with self.reddit_source.suppress_bring_to_front():
            for obj_id, obj in objs:
                if not self.continue_run or downloaded >= const.BULK_DOWNLOAD_LIMIT:
                    break
                if obj is None:
                    continue
                last = obj.date_last_download_utc
                if last is not None and now - last < cooldown:
                    remaining = cooldown - (now - last)
                    hours, remainder = divmod(int(remaining.total_seconds()), 3600)
                    minutes = remainder // 60
                    Message.send_warning(
                        f"{obj.name} was downloaded too recently. Try again in "
                        f"{hours}h {minutes}m or download manually."
                    )
                    continue
                downloaded += 1
                get_submissions(obj_id, progress=(downloaded, total))
                if downloaded < total:
                    self._pace()

    def _pace(self):
        pace_ms = (
            const.BULK_DOWNLOAD_PACE_MS_SLOW
            if self.settings_manager.slow_mode
            else const.BULK_DOWNLOAD_PACE_MS
        )
        assert self.extractor is not None
        assert self.downloader is not None
        while self.continue_run:
            time.sleep(pace_ms / 1000)
            if (
                not self.extractor.futures
                and not self.downloader.futures
                and self.submission_queue.empty()
                and self.download_queue.empty()
            ):
                return

    # [mine] feat(core): fetch a single post by URL and build its SUBMISSION ExtractionSet, or None if invalid
    def prepare_single_submission(self, url):
        match = _POST_ID_RE.search(url)
        if match is not None and not FORCE_DOWNLOAD:
            reddit_id = match.group(1)
            with self.db.get_scoped_session() as session:
                already_downloaded = (
                    session.query(Post.id).filter(Post.reddit_id == reddit_id).first()
                    is not None
                )
            if already_downloaded:
                # Skip the navigation entirely -- the reddit_id parsed straight out of the url
                # is enough to know this is a duplicate without fetching the post.
                Message.send_content_found(
                    ContentFoundPayload(
                        reddit_id=reddit_id,
                        author="",
                        subreddit="",
                        permalink=url,
                        is_new=False,
                    )
                )
                return None
        try:
            submission = self.reddit_source.get_post(url)
        except RateLimitedError:
            # handle_rate_limited already messaged the user and cancelled the session.
            return None
        except StopRequestedError:
            # stop_download already messaged the user; nothing further to report.
            return None
        if submission is None:
            Message.send_error(f"Failed to fetch post: {url}")
            return None
        with self.db.get_scoped_session() as session:
            # Mirrors prepare_submission's own author resolution below -- otherwise this
            # pre-check can pass (or fail) on a different criterion than the one that actually
            # decides the download, e.g. a tracked-but-download_enabled=False author would pass
            # this check yet still get silently dropped by prepare_submission with no message.
            author = (
                session.query(User)
                .filter(
                    func.lower(User.name) == submission.author.lower(),
                    User.significant == True,
                    User.download_enabled == True,
                )
                .first()
            )
            if author is None:
                Message.send_error(
                    f"Author {submission.author} is not tracked. Add the user before "
                    f"downloading their post."
                )
                return None
        return self.prepare_submission(submission)

    # Shared by explicit single-post downloads (prepare_single_submission, above) and ambient
    # matches: resolves whichever of author/subreddit is actually tracked, then hands off to
    # _finalize_submission -- the one place that checks for a duplicate post and reports a
    # "content found" event, also used directly by queue_submissions below, where the tracked
    # object is already known rather than needing to be resolved by name.
    def prepare_submission(self, submission):
        with self.db.get_scoped_session() as session:
            # Filters must mirror the tracked/download_enabled criteria the ambient poll used to decide
            # this submission was a match (gui/downloader_for_reddit_gui.py:handle_ambient_posts) --
            # otherwise a stale, non-tracked User row sharing the author's name (e.g. auto-created as a
            # post's author FK on some earlier, unrelated download) wins over the actually-tracked
            # Subreddit that caused the match, and the post gets saved under the wrong template/path.
            author = (
                session.query(User)
                .filter(
                    func.lower(User.name) == submission.author.lower(),
                    User.significant == True,
                    User.download_enabled == True,
                )
                .first()
            )
            subreddit = (
                session.query(Subreddit)
                .filter(
                    func.lower(Subreddit.name) == submission.subreddit.lower(),
                    Subreddit.significant == True,
                    Subreddit.download_enabled == True,
                )
                .first()
            )
            significant = author or subreddit
            if significant is None:
                return None
            significant_id = significant.id
        return self._finalize_submission(submission, significant_id)

    # See content (submission, already known to belong to significant_id), check for a duplicate
    # post, report one structured "content found" event either way, and hand back an ExtractionSet
    # ready to download -- the one place all three "found a submission" paths (bulk queue_submissions,
    # ambient/explicit prepare_submission) end up, so a bulk download shows up in the content feed
    # panel exactly like ambient matches and explicit single-post downloads do.
    def _finalize_submission(self, submission, significant_id):
        # [mine] get_scoped_update_session (commits), not get_scoped_session -- the FORCE_DOWNLOAD
        # branch below deletes rows and that delete must actually persist, or create_post's own
        # duplicate check downstream still sees the old Post row and silently refuses to recreate it.
        with self.db.get_scoped_update_session() as session:
            existing_post = (
                session.query(Post)
                .filter(
                    or_(
                        Post.reddit_id == submission.reddit_id,
                        Post.url == submission.url,
                    )
                )
                .first()
            )
            if existing_post is not None and FORCE_DOWNLOAD:
                session.query(Content).filter(
                    Content.post_id == existing_post.id
                ).delete()
                session.delete(existing_post)
                session.flush()
                existing_post = None
            is_new = existing_post is None
        Message.send_content_found(
            ContentFoundPayload(
                reddit_id=submission.reddit_id,
                author=submission.author,
                subreddit=submission.subreddit,
                permalink=submission.permalink,
                is_new=is_new,
            )
        )
        if not is_new:
            return None
        # download_session_id filled in by the caller once a session actually gets created
        # (run_batch for ambient/explicit, queue_submissions for bulk).
        return ExtractionSet(
            extraction_type="SUBMISSION",
            extraction_object=submission,
            significant_id=significant_id,
            download_session_id=None,
        )

    def validate_subreddit_list(self):
        """
        Validates the list of subreddits to make sure they all exist so that the user list can be constrained to the
        list of verified subreddits.
        """
        with self.db.get_scoped_session() as session:
            for subreddit_id in self.subreddit_id_list:
                if self.continue_run:
                    subreddit = session.query(Subreddit).get(subreddit_id)
                    if self.validate_subreddit(subreddit):
                        self.validated_subreddits.append(subreddit.name)
                    else:
                        subreddit.set_inactive()
                else:
                    break

    @verify_run
    def get_reddit_object_submissions(
        self, reddit_object_id: int, progress: tuple[int, int] | None = None
    ) -> None:
        """
        Takes a RedditObject id and then calls the appropriate method to get submissions for the object depending on
        what type of reddit object it is (user or subreddit)
        :param reddit_object_id: The id of the reddit object to be downloaded.
        """
        with self.db.get_scoped_session() as session:
            object_type = (
                session.query(RedditObject.object_type)
                .filter(RedditObject.id == reddit_object_id)
                .first()
            )
            if object_type[0] == "USER":
                self.get_user_submissions(
                    reddit_object_id, session=session, progress=progress
                )
            else:
                self.get_subreddit_submissions(
                    reddit_object_id, session=session, progress=progress
                )

    @verify_run
    def get_user_submissions(
        self,
        user_id: int,
        session: Session | None = None,
        progress: tuple[int, int] | None = None,
    ) -> None:
        if session is None:
            with self.db.get_scoped_session() as db_session:
                return self.get_user_submissions(
                    user_id, session=db_session, progress=progress
                )
        user = cast(User, session.query(User).get(user_id))
        user.set_existing()
        self._current_fetch_object = (
            user.name
        )  # [mine] feat(gui): download status window
        prefix = f"({progress[0]}/{progress[1]}) " if progress else ""
        Message.send_info(
            f"{prefix}Downloading user: {user.name}"
        )  # [mine] GUI progress logging
        coverage = {"confirmed": False}

        def source_method(name):
            result, submissions, coverage["confirmed"] = (
                self.reddit_source.validate_and_iter_user_submissions(
                    name, user.date_last_download_utc
                )
            )
            return result, submissions

        self.get_validated_submissions(user, source_method)
        if coverage["confirmed"]:
            user.set_date_last_download_utc()
        return None

    @verify_run
    def get_subreddit_submissions(
        self,
        subreddit_id: int,
        session: Session | None = None,
        progress: tuple[int, int] | None = None,
    ) -> None:
        if session is None:
            with self.db.get_scoped_session() as db_session:
                return self.get_subreddit_submissions(
                    subreddit_id, session=db_session, progress=progress
                )
        subreddit = cast(Subreddit, session.query(Subreddit).get(subreddit_id))
        subreddit.set_existing()
        self._current_fetch_object = (
            subreddit.name
        )  # [mine] feat(gui): download status window
        prefix = f"({progress[0]}/{progress[1]}) " if progress else ""
        Message.send_info(
            f"{prefix}Downloading subreddit: {subreddit.name}"
        )  # [mine] GUI progress logging
        coverage = {"confirmed": False}

        def source_method(name):
            result, submissions, coverage["confirmed"] = (
                self.reddit_source.validate_and_iter_subreddit_submissions(
                    name, subreddit.date_last_download_utc
                )
            )
            return result, submissions

        self.get_validated_submissions(subreddit, source_method)
        if coverage["confirmed"]:
            subreddit.set_date_last_download_utc()
        return None

    def get_validated_submissions(self, reddit_object, source_method):
        """Validates reddit_object and fetches its submissions via source_method."""

        def on_batch(raw_batch):
            submissions = self.filter_submissions(reddit_object, raw_batch)
            self.queue_submissions(reddit_object, submissions)

        def all_already_known(batch):
            """Use reddit_id only -- a url match is a crosspost and should not count."""
            ids = [s.reddit_id for s in batch]
            with self.db.get_scoped_session() as session:
                known_ids = {
                    reddit_id
                    for (reddit_id,) in session.query(Post.reddit_id)
                    .filter(Post.reddit_id.in_(ids))
                    .all()
                }
            return all(s.reddit_id in known_ids for s in batch)

        self.reddit_source.set_on_posts_collected(on_batch)
        self.reddit_source.set_all_known_checker(all_already_known)
        try:
            result, _ = source_method(reddit_object.name)
        except RateLimitedError:
            # handle_rate_limited already messaged the user and cancelled the session.
            return
        except StopRequestedError:
            # stop_download already messaged the user; nothing further to report.
            return
        except PlaywrightError:
            extra = {
                "object_type": reddit_object.object_type,
                "reddit_object": reddit_object.name,
            }
            self.logger.exception(
                "Browser navigation failed.  Ending submission extraction", extra=extra
            )
            Message.send_error(
                f"Failed to extract submissions for: {reddit_object.name}. Please try again shortly."
            )
            return
        finally:
            self.reddit_source.set_on_posts_collected(None)
            self.reddit_source.set_all_known_checker(None)
        self.validate_object(result, reddit_object)

    def queue_submissions(self, reddit_object, submissions):
        for submission in submissions:
            extraction_set = self._finalize_submission(submission, reddit_object.id)
            if extraction_set is None:
                continue
            extraction_set = extraction_set._replace(
                download_session_id=self.download_session_id
            )
            self.submission_queue.put(extraction_set)

    def filter_submissions(self, reddit_object, raw_submissions):
        submissions = []
        for submission in raw_submissions:
            if (
                not self.filter_subreddits
                or submission.subreddit in self.validated_subreddits
            ) and self.submission_filter.filter_submission(submission, reddit_object):
                submissions.append(submission)
        return submissions

    def stop_download(self, hard_stop=False):
        """
        Cancels whatever's currently open (explicit or ambient), rather than killing the pool --
        the pool must keep working for the next ambient poll / next explicit download after this.
        """
        self.stopped = True
        self.continue_run = False
        with self.db.get_scoped_session() as session:
            open_session_ids = [
                row[0]
                for row in session.query(DownloadSession.id)
                .filter(DownloadSession.end_time == None)
                .all()
            ]
        for session_id in open_session_ids:
            self.cancelled_sessions.add(session_id)
            if hard_stop:
                self.hard_stopped_sessions.add(session_id)
        Message.send_warning("\nStopped\n")
