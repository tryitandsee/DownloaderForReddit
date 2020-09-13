# [mine] feat(gui): download status window showing active threads and queue depth
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QHeaderView, QSizePolicy,
)
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QFont


class DownloadStatusDialog(QWidget):

    def __init__(self, get_runner):
        super().__init__(None, Qt.Window)
        self.get_runner = get_runner
        self.setWindowTitle('Download Status')
        self.resize(600, 400)
        self._build_ui()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        section_font = QFont()
        section_font.setBold(True)

        fetcher_heading = QLabel('Fetcher')
        fetcher_heading.setFont(section_font)
        layout.addWidget(fetcher_heading)
        self.fetcher_object_label = QLabel('User/Subreddit: —')
        self.fetcher_submission_label = QLabel('Last submission: —')
        layout.addWidget(self.fetcher_object_label)
        layout.addWidget(self.fetcher_submission_label)

        threads_heading = QLabel('Active download threads')
        threads_heading.setFont(section_font)
        layout.addWidget(threads_heading)

        self.thread_table = QTableWidget(0, 2)
        self.thread_table.setHorizontalHeaderLabels(['Thread', 'Downloading'])
        self.thread_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.thread_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.thread_table.verticalHeader().setVisible(False)
        self.thread_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.thread_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.thread_table)

        queue_heading = QLabel('Queue')
        queue_heading.setFont(section_font)
        layout.addWidget(queue_heading)

        row = QHBoxLayout()
        self.pending_label = QLabel('Pending: 0')
        self.completed_label = QLabel('Completed: 0')
        row.addWidget(self.pending_label)
        row.addStretch()
        row.addWidget(self.completed_label)
        layout.addLayout(row)

    def refresh(self):
        runner = self.get_runner()
        downloader = getattr(runner, 'downloader', None)

        fetcher_obj = getattr(runner, '_fetcher_object', None)
        fetcher_sub = getattr(runner, '_fetcher_submission', None)
        self.fetcher_object_label.setText(f'User/Subreddit: {fetcher_obj or "—"}')
        self.fetcher_submission_label.setText(f'Last submission: {fetcher_sub or "—"}')

        if downloader is None:
            self.thread_table.setRowCount(0)
            self.pending_label.setText('Pending: —')
            self.completed_label.setText('Completed: —')
            return

        active = dict(downloader._active_downloads)
        self.thread_table.setRowCount(len(active))
        for row, (thread, info) in enumerate(active.items()):
            short = thread.rsplit('_', 1)[-1] if '_' in thread else thread
            self.thread_table.setItem(row, 0, QTableWidgetItem(short))
            self.thread_table.setItem(row, 1, QTableWidgetItem(info))

        pending = max(0, len(downloader.futures) - len(active))
        self.pending_label.setText(f'Pending: {pending}')
        self.completed_label.setText(f'Completed: {downloader.download_count}')
