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
import os
import sys

from playwright.sync_api import Error as PlaywrightError
from PyQt6 import QtCore, QtWidgets

from DownloaderForReddit.core.cli import CLI
from DownloaderForReddit.core.download_runner import DownloadRunner
from DownloaderForReddit.database.migration import Migrator
from DownloaderForReddit.gui.downloader_for_reddit_gui import DownloaderForRedditGUI
from DownloaderForReddit.local_logging import logger
from DownloaderForReddit.messaging.message_receiver import MessageReceiver
from DownloaderForReddit.utils import injector, system_util
from DownloaderForReddit.version import __version__

if sys.platform == "win32":
    myappid = f"SomeGuySoftware.DownloaderForReddit.{__version__}"
    AppUserModelID = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
        myappid
    )


def log_unhandled_exception(exc_type, value, traceback):
    # No sys.exit() here -- this hook exists to keep PyQt6 from aborting the whole process (its
    # own default behavior for an exception escaping a slot) over a single bad GUI action. Calling
    # sys.exit() defeated that purpose by making every uncaught exception, anywhere, fatal --
    # including mid-download, with no relation to closing/exiting the app.
    logger = logging.getLogger(f"DownloaderForReddit.{__name__}")
    logger.critical("Unhandled exception", exc_info=(exc_type, value, traceback))


def check_migration():
    migrator = Migrator()
    migrator.check_migration()


def acquire_instance_lock() -> QtCore.QLockFile:
    """Held for the process lifetime. Taken before logging and migration, so a second instance
    never touches the shared log, dfr.db, or config.toml."""
    lock_path = os.path.join(
        system_util.get_data_directory(), "DownloaderForReddit.lock"
    )
    instance_lock = QtCore.QLockFile(lock_path)
    # 0 turns off age-based staleness; a lock left by a dead process is still reclaimed.
    instance_lock.setStaleLockTime(0)
    if not instance_lock.tryLock(0):
        if instance_lock.error() == QtCore.QLockFile.LockError.LockFailedError:
            text = (
                "Only one instance of Downloader for Reddit is allowed at a time.\n\n"
                "Please close the other instance and try again."
            )
        else:
            text = f"Could not create the lock file {lock_path}: {instance_lock.error().name}"
        QtWidgets.QMessageBox.critical(None, "Downloader for Reddit", text)
        sys.exit(1)
    return instance_lock


def check_args(args):
    cli = CLI()
    cli.parse_args(args)


def main():
    check_args(sys.argv[1:])

    # Created first: acquire_instance_lock's dialog needs it.
    app = QtWidgets.QApplication(sys.argv)
    instance_lock = acquire_instance_lock()  # noqa: F841 -- must stay referenced until exit

    logger.make_logger()
    sys.excepthook = log_unhandled_exception

    check_migration()

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

    # Standing download runner: owns the extraction/download thread pool for the process
    # lifetime. Explicit downloads and ambient extraction both queue work onto this one instance
    # rather than each spinning up their own runner/threads/executors.
    download_runner = DownloadRunner()
    download_thread = QtCore.QThread()
    download_runner.moveToThread(download_thread)
    download_thread.started.connect(download_runner.start_pool)
    download_thread.start()

    window = DownloaderForRedditGUI(queue, receiver, download_runner)

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

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
