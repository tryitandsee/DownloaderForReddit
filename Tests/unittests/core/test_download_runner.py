from unittest import TestCase
from unittest.mock import patch, MagicMock
import logging
from datetime import datetime, timedelta
import prawcore

from DownloaderForReddit.core.download_runner import DownloadRunner
from DownloaderForReddit.database.database_handler import DatabaseHandler
from DownloaderForReddit.utils import injector
from Tests.mockobjects.mock_objects import MockPrawSubmission, get_user, get_subreddit, get_post


logging.disable(logging.CRITICAL)
DL = 'DownloaderForReddit.core.download_runner.DownloadRunner'


@patch('DownloaderForReddit.utils.reddit_utils.get_reddit_instance', return_value=None)
class TestDownloadRunner(TestCase):

    def submission_generator(self, submission_list):
        """
        Helper list to turn a list of mock praw posts into a generator similar to what is returned from praw when
        extracting submissions from reddit.
        :param submission_list: A list of mock submissions.
        :return: A generator of the supplied mock submissions.
        """
        for x in submission_list:
            yield x

    @classmethod
    def setUpClass(cls):
        cls.now = datetime.now()
        cls.settings_manager = MagicMock()
        injector.settings_manager = cls.settings_manager
        injector.database_handler = DatabaseHandler(in_memory=True)

    @patch(f'{DL}.get_subreddit_submissions')
    @patch(f'{DL}.get_user_submissions')
    @patch(f'{DL}.get_reddit_object_submissions')
    def test_setup_for_user_download(self, get_ro_submissions, get_user_submissions, get_sub_submissions, reddit_utils):
        download_runner = DownloadRunner(user_id_list=[2, 3, 4])
        download_runner.run_download()
        get_ro_submissions.assert_not_called()
        get_user_submissions.assert_called()
        get_sub_submissions.assert_not_called()

    @patch(f'{DL}.get_subreddit_submissions')
    @patch(f'{DL}.get_user_submissions')
    @patch(f'{DL}.get_reddit_object_submissions')
    def test_setup_for_subreddit_download(self, get_ro_submissions, get_user_submissions, get_sub_submissions,
                                          reddit_utils):
        download_runner = DownloadRunner(subreddit_id_list=[2, 3, 4])
        download_runner.run_download()
        get_ro_submissions.assert_not_called()
        get_user_submissions.assert_not_called()
        get_sub_submissions.assert_called()

    @patch(f'{DL}.get_subreddit_submissions')
    @patch(f'{DL}.get_user_submissions')
    @patch(f'{DL}.get_reddit_object_submissions')
    def test_setup_for_ro_download(self, get_ro_submissions, get_user_submissions, get_sub_submissions, reddit_utils):
        download_runner = DownloadRunner(reddit_object_id_list=[2, 3, 4])
        download_runner.run_download()
        get_ro_submissions.assert_called()
        get_user_submissions.assert_not_called()
        get_sub_submissions.assert_not_called()

    @patch(f'{DL}.validate_subreddit_list')
    @patch(f'{DL}.get_subreddit_submissions')
    @patch(f'{DL}.get_user_submissions')
    @patch(f'{DL}.get_reddit_object_submissions')
    def test_setup_for_restricted_download(self, get_ro_submissions, get_user_submissions, get_sub_submissions,
                                           validate_subreddit_list, reddit_utils):
        download_runner = DownloadRunner(user_id_list=[2, 3, 4], subreddit_id_list=[4, 6, 2])
        download_runner.run_download()
        get_ro_submissions.assert_not_called()
        get_user_submissions.assert_called()
        get_sub_submissions.assert_not_called()
        validate_subreddit_list.assert_called()
        self.assertTrue(download_runner.filter_subreddits)

    @patch(f'{DL}.get_raw_submissions')
    def test_get_submissions_with_old_stickied_posts(self, get_raw_submissions, reddit_utils):
        user = get_user(absolute_date_limit=self.now - timedelta(days=10))
        mock_submissions = []
        for x in range(2):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=100), stickied=True))
        for x in range(2, 20):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x)))
        get_raw_submissions.return_value = self.submission_generator(mock_submissions)
        self.assertEqual(20, len(mock_submissions))

        download_runner = DownloadRunner()
        submissions = download_runner.get_submissions(None, user)

        self.assertEqual(8, len(submissions))
        for sub in submissions:
            self.assertGreater(sub.created, user.absolute_date_limit.timestamp())
            self.assertFalse(sub.stickied)

    @patch(f'{DL}.get_raw_submissions')
    def test_get_submissions_with_new_stickied_posts(self, get_raw_submissions, reddit_utils):
        user = get_user(absolute_date_limit=self.now - timedelta(days=10))
        mock_submissions = []
        for x in range(2):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x), stickied=True))
        for x in range(2, 20):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x)))
        get_raw_submissions.return_value = self.submission_generator(mock_submissions)
        self.assertEqual(20, len(mock_submissions))

        download_runner = DownloadRunner()
        submissions = download_runner.get_submissions(None, user)

        self.assertEqual(10, len(submissions))
        stickied = 0
        for sub in submissions:
            self.assertGreater(sub.created, user.absolute_date_limit.timestamp())
            if sub.stickied:
                stickied += 1
        self.assertEqual(2, stickied)

    @patch(f'{DL}.get_raw_submissions')
    def test_get_submissions_with_no_stickied_posts(self, get_raw_submissions, reddit_utils):
        user = get_user(absolute_date_limit=self.now - timedelta(days=10))
        mock_submissions = []
        for x in range(20):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x)))
        get_raw_submissions.return_value = self.submission_generator(mock_submissions)
        self.assertEqual(20, len(mock_submissions))

        download_runner = DownloadRunner()
        submissions = download_runner.get_submissions(None, user)

        self.assertEqual(10, len(submissions))
        for sub in submissions:
            self.assertGreater(sub.created, user.absolute_date_limit.timestamp())

    @patch(f'{DL}.get_raw_submissions')
    def test_get_submissions_with_old_stickied_posts_restricted_download(self, get_raw_submissions, reddit_utils):
        allowed_subreddit = get_subreddit(name='allowed')
        setattr(allowed_subreddit, 'display_name', 'allowed')
        forbidden_subreddit = get_subreddit(name='forbidden')
        setattr(forbidden_subreddit, 'display_name', 'forbidden')
        user = get_user(absolute_date_limit=self.now - timedelta(days=10))
        mock_submissions = []
        for x in range(2):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=100), stickied=True,
                                                       subreddit=allowed_subreddit))
        for x in range(2, 18):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x),
                                                       subreddit=allowed_subreddit))
        for x in range(2, 18):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x),
                                                       subreddit=forbidden_subreddit))
        get_raw_submissions.return_value = self.submission_generator(mock_submissions)
        self.assertEqual(34, len(mock_submissions))

        download_runner = DownloadRunner()
        download_runner.filter_subreddits = True
        download_runner.validated_subreddits.append(allowed_subreddit.display_name)
        submissions = download_runner.get_submissions(None, user)

        self.assertEqual(8, len(submissions))
        for sub in submissions:
            self.assertGreater(sub.created, user.absolute_date_limit.timestamp())
            self.assertFalse(sub.stickied)
            self.assertNotEqual(sub.subreddit, forbidden_subreddit)

    @patch(f'{DL}.get_raw_submissions')
    def test_get_submissions_with_new_stickied_posts_restricted_download(self, get_raw_submissions, reddit_utils):
        allowed_subreddit = get_subreddit(name='allowed')
        setattr(allowed_subreddit, 'display_name', 'allowed')
        forbidden_subreddit = get_subreddit(name='forbidden')
        setattr(forbidden_subreddit, 'display_name', 'forbidden')
        user = get_user(absolute_date_limit=self.now - timedelta(days=10))
        mock_submissions = []
        for x in range(2):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x), stickied=True,
                                                       subreddit=allowed_subreddit))
        for x in range(2, 20):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x),
                                                       subreddit=allowed_subreddit))
        for x in range(2, 20):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x),
                                                       subreddit=forbidden_subreddit))
        get_raw_submissions.return_value = self.submission_generator(mock_submissions)
        self.assertEqual(38, len(mock_submissions))

        download_runner = DownloadRunner()
        download_runner.filter_subreddits = True
        download_runner.validated_subreddits.append(allowed_subreddit.display_name)
        submissions = download_runner.get_submissions(None, user)

        self.assertEqual(10, len(submissions))
        stickied = 0
        for sub in submissions:
            self.assertGreater(sub.created, user.absolute_date_limit.timestamp())
            self.assertNotEqual(sub.subreddit, forbidden_subreddit)
            if sub.stickied:
                stickied += 1
        self.assertEqual(2, stickied)

    @patch(f'{DL}.get_raw_submissions')
    def test_get_submissions_with_no_stickied_posts_restricted_download(self, get_raw_submissions, reddit_utils):
        allowed_subreddit = get_subreddit(name='allowed')
        setattr(allowed_subreddit, 'display_name', 'allowed')
        forbidden_subreddit = get_subreddit(name='forbidden')
        setattr(forbidden_subreddit, 'display_name', 'forbidden')
        user = get_user(absolute_date_limit=self.now - timedelta(days=10))
        mock_submissions = []
        for x in range(20):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x),
                                                       subreddit=allowed_subreddit))
        for x in range(20):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x),
                                                       subreddit=forbidden_subreddit))
        get_raw_submissions.return_value = self.submission_generator(mock_submissions)
        self.assertEqual(40, len(mock_submissions))

        download_runner = DownloadRunner()
        download_runner.filter_subreddits = True
        download_runner.validated_subreddits.append(allowed_subreddit.display_name)
        submissions = download_runner.get_submissions(None, user)

        self.assertEqual(10, len(submissions))
        for sub in submissions:
            self.assertGreater(sub.created, user.absolute_date_limit.timestamp())
            self.assertNotEqual(sub.subreddit, forbidden_subreddit)

    @patch(f'{DL}.get_raw_submissions')
    def test_get_submissions_with_old_pinned_posts(self, get_raw_submissions, reddit_utils):
        user = get_user(absolute_date_limit=self.now - timedelta(days=10))
        mock_submissions = []
        for x in range(2):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=100), pinned=True))
        for x in range(2, 20):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x)))
        get_raw_submissions.return_value = self.submission_generator(mock_submissions)
        self.assertEqual(20, len(mock_submissions))

        download_runner = DownloadRunner()
        submissions = download_runner.get_submissions(None, user)

        self.assertEqual(8, len(submissions))
        for sub in submissions:
            self.assertGreater(sub.created, user.absolute_date_limit.timestamp())
            self.assertFalse(sub.stickied)

    @patch(f'{DL}.get_raw_submissions')
    def test_get_submissions_with_new_pinned_posts(self, get_raw_submissions, reddit_utils):
        user = get_user(absolute_date_limit=self.now - timedelta(days=10))
        mock_submissions = []
        for x in range(2):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x), pinned=True))
        for x in range(2, 20):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x)))
        get_raw_submissions.return_value = self.submission_generator(mock_submissions)
        self.assertEqual(20, len(mock_submissions))

        download_runner = DownloadRunner()
        submissions = download_runner.get_submissions(None, user)

        self.assertEqual(10, len(submissions))
        pinned = 0
        for sub in submissions:
            self.assertGreater(sub.created, user.absolute_date_limit.timestamp())
            if sub.pinned:
                pinned += 1
        self.assertEqual(2, pinned)

    @patch(f'{DL}.get_raw_submissions')
    def test_get_submissions_with_old_pinned_posts_restricted_download(self, get_raw_submissions, reddit_utils):
        allowed_subreddit = get_subreddit(name='allowed')
        setattr(allowed_subreddit, 'display_name', 'allowed')
        forbidden_subreddit = get_subreddit(name='forbidden')
        setattr(forbidden_subreddit, 'display_name', 'forbidden')
        user = get_user(absolute_date_limit=self.now - timedelta(days=10))
        mock_submissions = []
        for x in range(2):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=100), pinned=True,
                                                       subreddit=allowed_subreddit))
        for x in range(2, 18):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x),
                                                       subreddit=allowed_subreddit))
        for x in range(2, 18):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x),
                                                       subreddit=forbidden_subreddit))
        get_raw_submissions.return_value = self.submission_generator(mock_submissions)
        self.assertEqual(34, len(mock_submissions))

        download_runner = DownloadRunner()
        download_runner.filter_subreddits = True
        download_runner.validated_subreddits.append(allowed_subreddit.display_name)
        submissions = download_runner.get_submissions(None, user)

        self.assertEqual(8, len(submissions))
        for sub in submissions:
            self.assertGreater(sub.created, user.absolute_date_limit.timestamp())
            self.assertFalse(sub.stickied)
            self.assertNotEqual(sub.subreddit, forbidden_subreddit)

    @patch(f'{DL}.get_raw_submissions')
    def test_get_submissions_with_new_pinned_posts_restricted_download(self, get_raw_submissions, reddit_utils):
        allowed_subreddit = get_subreddit(name='allowed')
        setattr(allowed_subreddit, 'display_name', 'allowed')
        forbidden_subreddit = get_subreddit(name='forbidden')
        setattr(forbidden_subreddit, 'display_name', 'forbidden')
        user = get_user(absolute_date_limit=self.now - timedelta(days=10))
        mock_submissions = []
        for x in range(2):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x), pinned=True,
                                                       subreddit=allowed_subreddit))
        for x in range(2, 20):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x),
                                                       subreddit=allowed_subreddit))
        for x in range(2, 20):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x),
                                                       subreddit=forbidden_subreddit))
        get_raw_submissions.return_value = self.submission_generator(mock_submissions)
        self.assertEqual(38, len(mock_submissions))

        download_runner = DownloadRunner()
        download_runner.filter_subreddits = True
        download_runner.validated_subreddits.append(allowed_subreddit.display_name)
        submissions = download_runner.get_submissions(None, user)

        self.assertEqual(10, len(submissions))
        pinned = 0
        for sub in submissions:
            self.assertGreater(sub.created, user.absolute_date_limit.timestamp())
            self.assertNotEqual(sub.subreddit, forbidden_subreddit)
            if sub.pinned:
                pinned += 1
        self.assertEqual(2, pinned)

    def make_single_post_runner(self, author_name, url):
        runner = DownloadRunner(single_submission_urls=[url])
        submission = MagicMock()
        submission.author.name = author_name
        submission.url = url
        runner.reddit_instance = MagicMock()
        runner.reddit_instance.submission.return_value = submission
        return runner, submission

    @patch(f'{DL}.hold')
    @patch(f'{DL}.start_downloader')
    @patch(f'{DL}.start_extractor')
    @patch(f'{DL}.create_download_session')
    @patch(f'{DL}.run_download')
    @patch(f'{DL}.prepare_single_submission')
    def test_run_enqueues_only_valid_submissions_and_skips_run_download(self, prepare, run_download, create_session,
                                                                        start_extractor, start_downloader, hold,
                                                                        reddit_utils):
        valid = MagicMock()
        prepare.side_effect = [valid, None]
        download_runner = DownloadRunner(single_submission_urls=['http://fake.site/a', 'http://fake.site/b'])

        download_runner.run()

        self.assertEqual(2, prepare.call_count)
        create_session.assert_called()
        run_download.assert_not_called()
        self.assertIs(valid, download_runner.submission_queue.get_nowait())
        self.assertTrue(download_runner.submission_queue.empty())

    @patch(f'{DL}.hold')
    @patch(f'{DL}.create_download_session')
    @patch(f'{DL}.prepare_single_submission')
    def test_run_creates_no_session_when_all_submissions_invalid(self, prepare, create_session, hold, reddit_utils):
        prepare.return_value = None
        finished = MagicMock()
        download_runner = DownloadRunner(single_submission_urls=['http://fake.site/a', 'http://fake.site/b'])
        download_runner.finished.connect(finished)

        download_runner.run()

        create_session.assert_not_called()
        hold.assert_not_called()
        finished.assert_called()

    @patch('DownloaderForReddit.core.download_runner.Message')
    def test_prepare_single_submission_returns_extraction_set_for_tracked_author(self, message, reddit_utils):
        with injector.database_handler.get_scoped_session() as session:
            user = get_user(name='SinglePostUser')
            session.add(user)
            session.commit()
            author_id = user.id
        runner, submission = self.make_single_post_runner('SinglePostUser', 'http://fake.site/new')

        extraction_set = runner.prepare_single_submission('http://fake.site/new')

        self.assertEqual('SUBMISSION', extraction_set.extraction_type)
        self.assertIs(submission, extraction_set.extraction_object)
        self.assertEqual(author_id, extraction_set.significant_id)

    @patch('DownloaderForReddit.core.download_runner.Message')
    def test_prepare_single_submission_skips_duplicate_url(self, message, reddit_utils):
        url = 'http://fake.site/duplicate'
        with injector.database_handler.get_scoped_session() as session:
            user = get_user(name='DuplicateUser')
            get_post(session=session, url=url, author=user, significant=user)
            session.commit()
        runner, _ = self.make_single_post_runner('DuplicateUser', url)

        self.assertIsNone(runner.prepare_single_submission(url))
        message.send_warning.assert_called()

    @patch('DownloaderForReddit.core.download_runner.Message')
    def test_prepare_single_submission_errors_when_author_not_tracked(self, message, reddit_utils):
        url = 'http://fake.site/untracked'
        runner, _ = self.make_single_post_runner('UntrackedUser', url)

        self.assertIsNone(runner.prepare_single_submission(url))
        message.send_error.assert_called()

    @patch(f'{DL}.get_raw_submissions')
    def test_too_many_requests_exception_is_properly_handled(self, get_raw_submissions, reddit_utils):
        user = get_user()
        mock_submissions = []
        for x in range(4):
            mock_submissions.append(MockPrawSubmission(created=self.now - timedelta(days=x)))
        get_raw_submissions.side_effect = prawcore.exceptions.TooManyRequests(MagicMock())

        download_runner = DownloadRunner()
        submissions = download_runner.get_submissions(None, user)

        get_raw_submissions.assert_called()
        self.assertEqual(0, len(submissions))

