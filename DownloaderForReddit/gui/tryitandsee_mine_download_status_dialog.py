# [mine] feat(gui): download status window showing active threads and queue depth
import logging

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


class DownloadStatusDialog(QWidget):
    def __init__(self, get_runner):
        super().__init__()
        self.logger = logging.getLogger(f"DownloaderForReddit.{__name__}")
        self.get_runner = get_runner
        self._build_ui()
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)

        self.fetcher_object_label = QLabel("Fetcher: idle...")
        layout.addWidget(self.fetcher_object_label)

        panel_row = QHBoxLayout()
        extraction_column = QVBoxLayout()
        self.extraction_status_label = QLabel()
        extraction_column.addWidget(self.extraction_status_label)
        download_column = QVBoxLayout()
        self.download_status_label = QLabel()
        download_column.addWidget(self.download_status_label)

        self.extraction_table = QTableWidget(0, 4)
        self.extraction_table.setHorizontalHeaderLabels(
            ["Thread", "User", "ID", "Extracting"]
        )
        self.extraction_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents
        )
        self.extraction_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents
        )
        self.extraction_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents
        )
        self.extraction_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.Stretch
        )
        self.extraction_table.verticalHeader().setVisible(False)
        self.extraction_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.extraction_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.extraction_table.setMaximumHeight(150)
        extraction_column.addWidget(self.extraction_table)
        panel_row.addLayout(extraction_column)

        self.thread_table = QTableWidget(0, 4)
        self.thread_table.setHorizontalHeaderLabels(
            ["Thread", "User", "ID", "Downloading"]
        )
        self.thread_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents
        )
        self.thread_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents
        )
        self.thread_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents
        )
        self.thread_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.Stretch
        )
        self.thread_table.verticalHeader().setVisible(False)
        self.thread_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.thread_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.thread_table.setMaximumHeight(150)
        download_column.addWidget(self.thread_table)
        panel_row.addLayout(download_column)

        layout.addLayout(panel_row)

        bottom_row = QHBoxLayout()
        self.completed_label = QLabel("Completed: 0")
        self.duplicate_label = QLabel("Duplicates: 0")
        bottom_row.addWidget(self.completed_label)
        bottom_row.addStretch()
        bottom_row.addWidget(self.duplicate_label)
        layout.addLayout(bottom_row)

    def refresh(self):
        try:
            self._refresh()
        except Exception:
            self.logger.exception("Failed to refresh download status dialog")

    def _refresh(self):
        try:
            runner = self.get_runner()
            downloader = getattr(runner, "downloader", None)
            extractor = getattr(runner, "extractor", None)
            fetcher_obj = getattr(runner, "_current_fetch_object", None)
        except RuntimeError:
            runner = None
            downloader = None
            extractor = None
            fetcher_obj = None
        self.fetcher_object_label.setText(f"Fetcher: {fetcher_obj or 'idle...'}")

        if runner is None or downloader is None:
            self.extraction_status_label.setText("Extractor: no active session")
            self.download_status_label.setText("Downloader: no active session")
            self.extraction_table.setRowCount(0)
            self.thread_table.setRowCount(0)
            self.completed_label.setText("Completed: —")
            self.duplicate_label.setText("Duplicates: —")
            return

        ext_futures = len(getattr(extractor, "futures", []))
        dl_futures = len(getattr(downloader, "futures", []))
        ext_running = getattr(extractor, "running", False)
        dl_running = getattr(downloader, "running", False)

        ext_status = "running" if ext_running else "idle"
        dl_status = "running" if dl_running else "idle"

        self.extraction_status_label.setText(
            f"Extractor: {ext_status}  |  extraction jobs: {ext_futures}"
        )
        self.download_status_label.setText(
            f"Downloader: {dl_status}  |  download jobs: {dl_futures}"
        )

        active_ext = dict(getattr(extractor, "_active_extractions", {}))
        self.extraction_table.setRowCount(len(active_ext))
        for row, (thread, (user, item_id, info)) in enumerate(active_ext.items()):
            short = thread.rsplit("_", 1)[-1] if "_" in thread else thread
            self.extraction_table.setItem(row, 0, QTableWidgetItem(short))
            self.extraction_table.setItem(row, 1, QTableWidgetItem(user))
            self.extraction_table.setItem(row, 2, QTableWidgetItem(str(item_id)))
            self.extraction_table.setItem(row, 3, QTableWidgetItem(info))

        active = dict(downloader._active_downloads)
        self.thread_table.setRowCount(len(active))
        for row, (thread, (user, content_id, title)) in enumerate(active.items()):
            short = thread.rsplit("_", 1)[-1] if "_" in thread else thread
            self.thread_table.setItem(row, 0, QTableWidgetItem(short))
            self.thread_table.setItem(row, 1, QTableWidgetItem(user))
            self.thread_table.setItem(row, 2, QTableWidgetItem(str(content_id)))
            self.thread_table.setItem(row, 3, QTableWidgetItem(title))

        self.completed_label.setText(f"Completed: {downloader.download_count}")
        self.duplicate_label.setText(f"Duplicates: {downloader.duplicate_count}")
