import platform
import logging
from queue import Queue, Empty
from threading import Thread, Event
from datetime import datetime
from playwright.sync_api import Error as PlaywrightError
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot
from collections import defaultdict, namedtuple
from sqlalchemy import func, or_

from DownloaderForReddit.core.download.downloader import Downloader
from . import const
from .content_runner import ContentRunner
from .reddit_source import ValidationError
from .submission_filter import SubmissionFilter
from .submittable_creator import SubmittableCreator
from .runner import verify_run
from .errors import NON_DOWNLOADABLE
from ..database.models import DownloadSession, RedditObject, User, Subreddit, Post, Content
from ..utils import injector, video_merger
from ..messaging.message import Message
from ..version import __version__


ExtractionSet = namedtuple('ExtractionSet', 'extraction_type extraction_object significant_id download_session_id')


class DownloadRunner(QObject):
    """
    Owns the extraction/download thread pool and queues for the process lifetime. A single
    instance is created once (see main.py) and moved to its own QThread; explicit downloads and
    ambient extraction both queue work onto it via request_download rather than each constructing
    their own runner/threads/executors.
    """

    remove_invalid_object = pyqtSignal(int)
    remove_forbidden_object = pyqtSignal(int)
    download_session_signal = pyqtSignal(int)  # emits the id of a DownloadSession that just finished
    pool_idle = pyqtSignal()  # emitted once whenever the whole pool (all sessions) goes idle
    request_download = pyqtSignal(object)  # GUI/ambient emit a params dict; queued onto this runner's thread

    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger(f'DownloaderForReddit.{__name__}')
        self.db = injector.get_database_handler()
        self.settings_manager = injector.get_settings_manager()
        self.reddit_source = injector.get_reddit_source()
        self.submission_filter = SubmissionFilter()

        # Governs the pool threads only; set once, at real app shutdown (see stop_pool). A
        # Stop/Terminate click must not touch this -- it has to keep servicing later sessions.
        self.stop_run = Event()
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
        # anything (e.g. get_home_feed_submissions's Playwright call, which can easily take longer
        # than idle_tick's ~2s poll interval).
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

    def start_pool(self):
        """Creates the extractor/downloader and their worker threads. Called once, at app start."""
        self.extractor = ContentRunner(self.submission_queue, self.download_queue, self.cancelled_sessions,
                                       self.stop_run, self.idle_tick)
        self.extraction_thread = Thread(target=self.extractor.run)
        self.extraction_thread.start()
        self.downloader = Downloader(self.download_queue, self.cancelled_sessions, self.hard_stopped_sessions,
                                     self.stop_run)
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
        idle = (
            not self._batch_in_progress and
            not self.extractor.futures and not self.downloader.futures and
            self.submission_queue.empty() and self.download_queue.empty()
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
            open_sessions = session.query(DownloadSession).filter(DownloadSession.end_time == None).all()
            finished_ids = [dl_session.id for dl_session in open_sessions]
            for dl_session in open_sessions:
                dl_session.end_time = datetime.now()
            session.commit()
        for session_id in finished_ids:
            self.cancelled_sessions.discard(session_id)
            self.hard_stopped_sessions.discard(session_id)
            self.download_session_signal.emit(session_id)
        if finished_ids:
            self.logger.debug('Download pool idle', extra={'finished_sessions': finished_ids})
        self.pool_idle.emit()

    def validate_subreddit(self, subreddit_obj):
        result = self.reddit_source.validate_subreddit(subreddit_obj.name)
        return self.validate_object(result, subreddit_obj)

    def validate_object(self, result, reddit_object):
        # [mine] fix(core): active now tracks whether the dedicated account follows a User --
        # validation no longer touches it, since "exists on reddit" and "is followed" are
        # unrelated facts.
        if result.valid:
            Message.send_debug(f'{reddit_object.name} is valid')
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
        self.logger.warning('Invalid reddit object detected', extra={'object_type': reddit_object.object_type,
                                                                     'reddit_object': reddit_object.name})
        Message.send_warning(f'Invalid {reddit_object.object_type.lower()}: {reddit_object.name}')
        self.remove_invalid_object.emit(reddit_object.id)

    def handle_forbidden_reddit_object(self, reddit_object):
        self.logger.warning('Forbidden reddit object detected', extra={'object_type': reddit_object.object_type,
                                                                       'reddit_object': reddit_object.name})
        Message.send_warning(f'Forbidden {reddit_object.object_type.lower()}: {reddit_object.name}')
        self.remove_forbidden_object.emit(reddit_object.id)

    def handle_failed_connection(self):
        if self.failed_connection_attempts >= 3:
            self.continue_run = False
            self.logger.error('Failed connection attempts exceeded.  Ending download session', exc_info=True)
            Message.send_critical('Failed connection attempts exceeded.  The download session has been canceled.  '
                                  'Please try the download again later.')
        else:
            self.logger.error('Failed to connect to reddit',
                              extra={'connection_attempts': self.failed_connection_attempts})
            Message.send_error(f'Failed to connect to reddit.  Connection attempts remaining: '
                               f'{3 - self.failed_connection_attempts}')
            self.failed_connection_attempts += 1

    def handle_too_many_requests_error(self, reddit_object):
        self.logger.error('Too many requests error', exc_info=True)
        message = (
            f'Reddit rate limit reached.  {reddit_object.object_type.capitalize()} ({reddit_object.name}) could '
            f'not be validated.  Please try again later.\n'
            f'For More information about this error, please visit the link below:\n'
            f'{const.RATE_LIMIT_DOC_URL}'
        )
        Message.send_error(message)

    def handle_unknown_error(self, reddit_object):
        self.logger.error('Failed to validate reddit object due to unknown error',
                          extra={'object_type': reddit_object.object_type, 'reddit_object': reddit_object.name},
                          exc_info=True)

    def run_unextracted_posts(self):
        self.logger.debug('Running unextracted posts')
        post_id_list = self.unextracted_id_list
        if post_id_list is None:
            with self.db.get_scoped_session() as session:
                post_id_list = session.query(Post.id)\
                    .filter(Post.extracted == False) \
                    .filter(Post.retry_attempts <= 3) \
                    .filter(or_(Post.extraction_error == None, Post.extraction_error.notin_(NON_DOWNLOADABLE)))
        self.logger.debug(f'{post_id_list.count()} unfinished posts to download')
        for post_id, in post_id_list.all():  # comma used to unpack result tuple
            extraction_set = ExtractionSet(extraction_type='POST', extraction_object=post_id, significant_id=None,
                                           download_session_id=self.download_session_id)
            self.submission_queue.put(extraction_set)
        self.logger.debug('Finished unextracted posts')

    def run_undownloaded_content(self):
        self.logger.debug('Running undownloaded content')
        content_id_list = self.undownloaded_id_list
        if content_id_list is None:
            with self.db.get_scoped_session() as session:
                content_id_list = session.query(Content)\
                    .filter(Content.downloaded == False) \
                    .filter(Content.retry_attempts <= 3) \
                    .filter(or_(Content.download_error == None, Content.download_error.notin_(NON_DOWNLOADABLE)))
        self.logger.debug(f'{content_id_list.count()} unfinished content items to download')
        for content in content_id_list.all():
            self.download_queue.put((content.id, self.download_session_id))
        self.logger.debug('Finished undownloaded content')

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
        self.user_id_list = params.get('user_id_list')
        self.subreddit_id_list = params.get('subreddit_id_list')
        self.reddit_object_id_list = params.get('reddit_object_id_list')
        self.run_unextracted = params.get('run_unextracted', False)
        self.unextracted_id_list = params.get('unextracted_id_list')
        self.run_undownloaded = params.get('run_undownloaded', False)
        self.undownloaded_id_list = params.get('undownloaded_id_list')
        self.run_new = params.get('run_new', True)
        self.single_submission_urls = params.get('single_submission_urls')
        self.submissions = params.get('submissions')

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
                self.run_batch(self.prepare_single_submission(url) for url in self.single_submission_urls)
                return
            if self.submissions is not None:
                self.run_batch(self.prepare_submission(submission) for submission in self.submissions)
                return
            self.run_normal()
        finally:
            self._batch_in_progress = False

    def run_batch(self, prepared_submissions):
        # Shared by single_submission_urls and submissions: validate everything first, only create
        # a DownloadSession if at least one survives (no run_download(), so this never touches
        # set_date_limit).
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
            extraction_set = extraction_set._replace(download_session_id=self.download_session_id)
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
        self.logger.info('Download runner started.', extra={
            'dfr_version': __version__,
            'platform': platform.platform,
            'run_unextracted': self.run_unextracted,
            'run_undownloaded': self.run_undownloaded,
            'run_new': self.run_new,
            'last_update': self.settings_manager.last_update,
            'extraction_thread_count': self.settings_manager.extraction_thread_count,
            'download_thread_count': self.settings_manager.download_thread_count,
            'multi_part_threshold': self.settings_manager.multi_part_threshold,
            'finish_incomplete_extractions': self.settings_manager.finish_incomplete_extractions_at_session_start,
            'finish_incomplete_downloads': self.settings_manager.finish_incomplete_downloads_at_session_start,
        })

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
            for ro_id in self.reddit_object_id_list:
                self.get_reddit_object_submissions(ro_id)
        else:
            if self.user_id_list is not None and self.subreddit_id_list is not None:
                self.filter_subreddits = True
                self.validate_subreddit_list()
            if self.user_id_list is not None:
                # Bulk user downloads go through the home feed (one aggregated "new" scrape) rather than
                # visiting each user's submitted page individually.
                # Downloading a single user via the context menu still goes through
                # get_reddit_object_submissions -> iter_user_submissions above.
                self.get_home_feed_submissions()
            else:
                for subreddit_id in self.subreddit_id_list:
                    self.get_subreddit_submissions(subreddit_id)

    # [mine] feat(core): fetch a single post by URL and build its SUBMISSION ExtractionSet, or None if invalid
    def prepare_single_submission(self, url):
        try:
            submission = self.reddit_source.get_post(url)
        except PlaywrightError:
            self.logger.error('Browser navigation failed while fetching single post', extra={'url': url},
                              exc_info=True)
            Message.send_error(f'Failed to fetch post: {url}')
            return None
        if submission is None:
            Message.send_error(f'Failed to fetch post: {url}')
            return None
        with self.db.get_scoped_session() as session:
            author = session.query(User).filter(User.name == submission.author).first()
            if author is None:
                Message.send_error(f'Author {submission.author} is not tracked. Add the user before '
                                   f'downloading their post.')
                return None
            if not SubmittableCreator.check_duplicate_post(submission.reddit_id, submission.url, session):
                Message.send_warning(f'Already downloaded - skipped: {submission.url}')
                return None
            author_id = author.id
        Message.send_info(f'Downloading single post by {submission.author}')
        # download_session_id filled in by run_batch once a session actually gets created
        return ExtractionSet(extraction_type='SUBMISSION', extraction_object=submission, significant_id=author_id,
                             download_session_id=None)

    # Ambient extraction: same idea as prepare_single_submission but for a SubmissionData already
    # in hand (no url to re-navigate to). Resolves against whichever of author/subreddit is
    # actually tracked -- the ambient poll already matched on one or both, case-insensitively, so
    # this looks up the same way rather than the exact-match self.reddit_source lookups elsewhere.
    def prepare_submission(self, submission):
        with self.db.get_scoped_session() as session:
            # Filters must mirror the tracked/download_enabled criteria the ambient poll used to decide
            # this submission was a match (gui/downloader_for_reddit_gui.py:ambient_poll) -- otherwise a
            # stale, non-tracked User row sharing the author's name (e.g. auto-created as a post's author
            # FK on some earlier, unrelated download) wins over the actually-tracked Subreddit that caused
            # the match, and the post gets saved under the wrong template/path.
            author = session.query(User).filter(func.lower(User.name) == submission.author.lower(),
                                                 User.significant == True, User.download_enabled == True).first()
            subreddit = session.query(Subreddit).filter(
                func.lower(Subreddit.name) == submission.subreddit.lower(),
                Subreddit.significant == True, Subreddit.download_enabled == True).first()
            significant = author or subreddit
            if significant is None:
                return None
            if not SubmittableCreator.check_duplicate_post(submission.reddit_id, submission.url, session):
                return None
            significant_id = significant.id
        Message.send_debug(f'checking {submission.author} : {submission.reddit_id} : {submission.url}')
        # download_session_id filled in by run_batch once a session actually gets created
        return ExtractionSet(extraction_type='SUBMISSION', extraction_object=submission,
                             significant_id=significant_id, download_session_id=None)

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
    def get_reddit_object_submissions(self, reddit_object_id):
        """
        Takes a RedditObject id and then calls the appropriate method to get submissions for the object depending on
        what type of reddit object it is (user or subreddit)
        :param reddit_object_id: The id of the reddit object to be downloaded.
        """
        with self.db.get_scoped_session() as session:
            object_type = session.query(RedditObject.object_type).filter(RedditObject.id == reddit_object_id).first()
            if object_type[0] == 'USER':
                self.get_user_submissions(reddit_object_id, session=session)
            else:
                self.get_subreddit_submissions(reddit_object_id, session=session)

    @verify_run
    def get_user_submissions(self, user_id, session=None):
        if session is None:
            with self.db.get_scoped_session() as session:
                return self.get_user_submissions(user_id, session=session)
        user = session.query(User).get(user_id)
        user.set_existing()
        self._current_fetch_object = user.name  # [mine] feat(gui): download status window
        Message.send_info(f'Downloading user: {user.name}')  # [mine] GUI progress logging
        self.get_validated_submissions(user, self.reddit_source.validate_and_iter_user_submissions)

    @verify_run
    def get_subreddit_submissions(self, subreddit_id, session=None):
        if session is None:
            with self.db.get_scoped_session() as session:
                return self.get_subreddit_submissions(subreddit_id, session=session)
        subreddit = session.query(Subreddit).get(subreddit_id)
        subreddit.set_existing()
        self._current_fetch_object = subreddit.name  # [mine] feat(gui): download status window
        self.get_validated_submissions(subreddit, self.reddit_source.validate_and_iter_subreddit_submissions)

    def get_validated_submissions(self, reddit_object, source_method):
        """
        Validates and fetches submissions for a single user/subreddit in one navigation -- the
        submitted/new listing page shows the same 404/private/suspended copy as the plain profile
        page, so there's no need for validate_user/validate_subreddit's separate profile-page visit
        before this one.
        """
        known_ids = self.get_known_post_ids(reddit_object)
        try:
            result, raw_submissions = source_method(reddit_object.name, limit=reddit_object.post_limit,
                                                     known_ids=known_ids)
        except PlaywrightError:
            extra = {'object_type': reddit_object.object_type, 'reddit_object': reddit_object.name}
            self.logger.error('Browser navigation failed.  Ending submission extraction',
                              extra=extra, exc_info=True)
            Message.send_error(f'Failed to extract submissions for: {reddit_object.name}. Please try again shortly.')
            return
        if self.validate_object(result, reddit_object):
            submissions = self.filter_submissions(reddit_object, raw_submissions)
            self.queue_submissions(reddit_object, submissions)

    def queue_submissions(self, reddit_object, submissions):
        date_limit = 0
        if not submissions:
            return

        for submission in submissions:
            created_epoch = submission.created.timestamp()
            if created_epoch > date_limit:
                date_limit = created_epoch
            extraction_set = ExtractionSet(extraction_type='SUBMISSION', extraction_object=submission,
                                           significant_id=reddit_object.id,
                                           download_session_id=self.download_session_id)
            self.submission_queue.put(extraction_set)
        if date_limit > 0:
            reddit_object.set_date_limit(date_limit)  # date limit modified after submissions are extracted

    def filter_submissions(self, reddit_object, raw_submissions):
        submissions = []
        for submission in raw_submissions:
            if not self.submission_filter.date_filter(submission, reddit_object):
                # Don't assume raw_submissions is strictly newest-first -- a repost/crosspost (or
                # the scroll-stop early-exit in reddit_source.py) can leave an older post ahead of
                # a genuinely new one. Skip it rather than break, so one out-of-order old post
                # can't silently discard every newer submission after it.
                continue
            if (not self.filter_subreddits or submission.subreddit in self.validated_subreddits) \
                    and self.submission_filter.filter_submission(submission, reddit_object):
                submissions.append(submission)
        return submissions

    @verify_run
    def get_home_feed_submissions(self):
        """
        Bulk user downloads pull from the dedicated account's home feed (sorted "new") in a single scrape,
        rather than visiting each tracked user's own page. Users are NOT individually validated here
        (that would mean one profile-page visit per user, exactly the per-user navigation this path
        exists to avoid) -- a deleted/suspended/not-yet-followed user's posts just won't appear in the
        feed and nothing is downloaded for them this run; they won't get auto-marked inactive from a
        bulk run the way a single context-menu download still does.
        Requires the dedicated account to actually follow every user in self.user_id_list (followed
        manually, one at a time -- reddit's follow rate limit is ~10/day); a user whose posts never
        appear in the aggregated feed (e.g. not yet followed) will simply have nothing downloaded for
        them this run.
        """
        with self.db.get_scoped_session() as session:
            users = session.query(User).filter(User.id.in_(self.user_id_list)).all()
            users_by_name = {user.name: user for user in users}
            for user in users:
                user.set_existing()

            if not users_by_name:
                return

            self._current_fetch_object = 'Home Feed'
            Message.send_info('Downloading home feed')
            try:
                raw_submissions = self.reddit_source.iter_home_feed()
            except PlaywrightError:
                self.logger.error('Browser navigation failed while fetching home feed', exc_info=True)
                Message.send_error('Failed to fetch home feed. Please try again shortly.')
                return

            by_user = defaultdict(list)
            for submission in raw_submissions:
                user = users_by_name.get(submission.author)
                if user is not None:
                    by_user[user].append(submission)

            for user, user_raw_submissions in by_user.items():
                if not self.continue_run:
                    break
                submissions = self.filter_submissions(user, user_raw_submissions)
                self.queue_submissions(user, submissions)

    def get_known_post_ids(self, reddit_object):
        with self.db.get_scoped_session() as session:
            rows = session.query(Post.reddit_id) \
                .filter(Post.significant_reddit_object_id == reddit_object.id) \
                .all()
            return {row[0] for row in rows}

    def stop_download(self, hard_stop=False):
        """
        Cancels whatever's currently open (explicit or ambient), rather than killing the pool --
        the pool must keep working for the next ambient poll / next explicit download after this.
        """
        self.stopped = True
        self.continue_run = False
        with self.db.get_scoped_session() as session:
            open_session_ids = [row[0] for row in session.query(DownloadSession.id)
                                .filter(DownloadSession.end_time == None).all()]
        for session_id in open_session_ids:
            self.cancelled_sessions.add(session_id)
            if hard_stop:
                self.hard_stopped_sessions.add(session_id)
        Message.send_warning('\nStopped\n')
