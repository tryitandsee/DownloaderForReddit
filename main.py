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

import ctypes
import logging
import sys

from playwright.sync_api import Error as PlaywrightError
from PyQt5 import QtCore, QtWidgets

from DownloaderForReddit.core.cli import CLI
from DownloaderForReddit.core.download_runner import DownloadRunner
from DownloaderForReddit.database.migration import Migrator
from DownloaderForReddit.gui.downloader_for_reddit_gui import DownloaderForRedditGUI
from DownloaderForReddit.local_logging import logger
from DownloaderForReddit.messaging.message_receiver import MessageReceiver
from DownloaderForReddit.utils import injector
from DownloaderForReddit.version import __version__

if sys.platform == "win32":
    myappid = f"SomeGuySoftware.DownloaderForReddit.{__version__}"
    AppUserModelID = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
        myappid
    )


def log_unhandled_exception(exc_type, value, traceback):
    # No sys.exit() here -- this hook exists to keep PyQt5 from aborting the whole process (its
    # own default behavior for an exception escaping a slot) over a single bad GUI action. Calling
    # sys.exit() defeated that purpose by making every uncaught exception, anywhere, fatal --
    # including mid-download, with no relation to closing/exiting the app.
    logger = logging.getLogger(f"DownloaderForReddit.{__name__}")
    logger.critical("Unhandled exception", exc_info=(exc_type, value, traceback))


def check_migration():
    migrator = Migrator()
    migrator.check_migration()


def check_args(args):
    cli = CLI()
    cli.parse_args(args)


def main():
    check_args(sys.argv[1:])

    logger.make_logger()
    sys.excepthook = log_unhandled_exception

    check_migration()

    app = QtWidgets.QApplication(sys.argv)

    try:
        injector.get_reddit_source()
    except PlaywrightError as e:
        if "Opening in existing browser session" in str(e):
            QtWidgets.QMessageBox.critical(
                None,
                "Downloader for Reddit",
                "Only one instance of Downloader for Reddit is allowed at a time.\n\n"
                "Please close the other instance (or its browser window) and try again.",
            )
            sys.exit(1)
        raise

    queue = injector.get_message_queue()
    message_thread = QtCore.QThread()
    receiver = MessageReceiver(queue)
    scheduler = injector.get_scheduler()

    # Standing download runner: owns the extraction/download thread pool for the process
    # lifetime. Explicit downloads and ambient extraction both queue work onto this one instance
    # rather than each spinning up their own runner/threads/executors.
    download_runner = DownloadRunner()
    download_thread = QtCore.QThread()
    download_runner.moveToThread(download_thread)
    download_thread.started.connect(download_runner.start_pool)
    download_thread.start()

    window = DownloaderForRedditGUI(queue, receiver, scheduler, download_runner)

    receiver.text_output.connect(window.handle_message)
    receiver.non_text_output.connect(window.handle_progress)
    receiver.content_output.connect(window.handle_content_found)
    receiver.follow_state_output.connect(window.handle_follow_state_changed)

    receiver.moveToThread(message_thread)
    message_thread.started.connect(receiver.run)
    receiver.finished.connect(message_thread.quit)
    receiver.finished.connect(receiver.deleteLater)
    message_thread.finished.connect(message_thread.deleteLater)
    message_thread.start()

    schedule_thread = QtCore.QThread()
    scheduler.moveToThread(schedule_thread)
    scheduler.run_task.connect(window.run_scheduled_download)
    scheduler.countdown.connect(window.update_scheduled_download)
    scheduler.finished.connect(schedule_thread.quit)
    scheduler.finished.connect(scheduler.deleteLater)
    schedule_thread.finished.connect(schedule_thread.deleteLater)
    schedule_thread.started.connect(scheduler.run)
    schedule_thread.start()

    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
