import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from queue import Empty

from .runner import Runner, verify_run
from .submission_handler import SubmissionHandler
from .submittable_creator import SubmittableCreator
from ..database.models import Post
from ..utils import injector


class ContentRunner(Runner):

    def __init__(self, submission_queue, download_queue, cancelled_sessions, stop_run, on_idle_tick):
        super().__init__(stop_run)
        self.logger = logging.getLogger(__name__)
        self.submission_queue = submission_queue
        self.download_queue = download_queue
        # download_session_ids a Stop/Terminate click has cancelled -- checked per item rather than
        # via stop_run, since stop_run now only governs actual app shutdown (a standing pool must
        # keep servicing later sessions after a Stop click, not die with the session that was
        # active when it was clicked).
        self.cancelled_sessions = cancelled_sessions
        self.on_idle_tick = on_idle_tick
        self.output_queue = injector.get_message_queue()
        self.settings_manager = injector.get_settings_manager()
        self.db = injector.get_database_handler()

        self.thread_count = self.settings_manager.extraction_thread_count
        self.executor = ThreadPoolExecutor(max_workers=self.thread_count)
        self.futures = []
        self._active_extractions = {}  # [mine] feat(gui): download status window

    @property
    def running(self):
        return len(self.futures) > 0

    def run(self):
        self.logger.debug('Content extractor running')
        while self.continue_run:
            try:
                item = self.submission_queue.get(timeout=2)
                if item is None:
                    break
                extraction_type, extraction_object, significant_id, download_session_id = item
                if download_session_id in self.cancelled_sessions:
                    continue
                if extraction_type == 'SUBMISSION':
                    future = self.executor.submit(self.handle_submission, submission=extraction_object,
                                                  significant_id=significant_id,
                                                  download_session_id=download_session_id)
                else:
                    future = self.executor.submit(self.finish_post, post_id=extraction_object,
                                                  download_session_id=download_session_id)
                future.add_done_callback(self.remove_future)
                self.futures.append(future)
            except Empty:
                self.on_idle_tick()
        self.executor.shutdown(wait=True)
        self.download_queue.put(None)
        self.logger.debug('Content extractor exiting')

    def remove_future(self, future):
        self.futures.remove(future)

    @verify_run
    def handle_submission(self, submission, significant_id, download_session_id):
        """
        Takes a reddit submission and creates a Post from its data.  Then calls the appropriate methods for the post.
        If comments are to be extracted from the submission, this is also handled here.
        :param submission: The reddit submission that is to be extracted.
        :param significant_id: The id of the reddit object for which the submissions was extracted from reddit.
        :param download_session_id: The id of the DownloadSession this submission was queued under.
        """
        # [mine] feat(gui): download status window
        thread = threading.current_thread().name
        self._active_extractions[thread] = getattr(submission, 'url', str(submission))
        try:
            with self.db.get_scoped_session() as session:
                post = SubmittableCreator.create_post(submission, significant_id, session, download_session_id)
                if post is not None:
                    submission_handler = SubmissionHandler(submission, post, download_session_id, session,
                                                           self.download_queue, self.stop_run)
                    submission_handler.extract_submission()
                    if post.significant_reddit_object.run_comment_operations:
                        submission_handler.extract_comments()
        finally:
            self._active_extractions.pop(thread, None)

    def finish_post(self, post_id, download_session_id):
        # [mine] feat(gui): download status window
        thread = threading.current_thread().name
        self._active_extractions[thread] = f'post:{post_id}'
        try:
            with self.db.get_scoped_session() as session:
                post = session.query(Post).get(post_id)
                self.handle_post(post, download_session_id)
        finally:
            self._active_extractions.pop(thread, None)

    @verify_run
    def handle_post(self, post, download_session_id):
        """
        Calls the appropriate methods for the supplied post.
        :param post: The post that is to be extracted.
        :param download_session_id: The id of the DownloadSession this post was queued under.
        """
        with self.db.get_scoped_session() as session:
            submission_handler = SubmissionHandler(None, post, download_session_id, session, self.download_queue,
                                                   self.stop_run)
            if not post.is_self:
                submission_handler.extract_submission_content()
            else:
                submission_handler.extract_self_post()
            if post.significant_reddit_object.run_comment_operations:
                # Comment extraction stays PRAW-based indefinitely (not ported to the browser
                # source). submission_handler.extract_comments() no-ops when self.submission isn't
                # a live PRAW object, so nothing further to do here.
                submission_handler.extract_comments()
