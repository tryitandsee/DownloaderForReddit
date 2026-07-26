# [mine] feat(gui): download status window showing active threads and queue depth
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QHeaderView, QSizePolicy,
)
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QFont


class DownloadStatusDialog(QWidget):

    def __init__(self, get_runner):
        super().__init__()
        self.get_runner = get_runner
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
        self.fetcher_object_label = QLabel('Current: —')
        layout.addWidget(self.fetcher_object_label)

        pipeline_heading = QLabel('Pipeline')
        pipeline_heading.setFont(section_font)
        layout.addWidget(pipeline_heading)
        self.pipeline_label = QLabel()
        layout.addWidget(self.pipeline_label)

        extraction_heading = QLabel('Active extraction threads')
        extraction_heading.setFont(section_font)
        layout.addWidget(extraction_heading)

        self.extraction_table = QTableWidget(0, 2)
        self.extraction_table.setHorizontalHeaderLabels(['Thread', 'Extracting'])
        self.extraction_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.extraction_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.extraction_table.verticalHeader().setVisible(False)
        self.extraction_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.extraction_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self.extraction_table)

        threads_heading = QLabel('Active download threads')
        threads_heading.setFont(section_font)
        layout.addWidget(threads_heading)

        self.thread_table = QTableWidget(0, 2)
        self.thread_table.setHorizontalHeaderLabels(['Thread', 'Downloading'])
        self.thread_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.thread_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.thread_table.verticalHeader().setVisible(False)
        self.thread_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.thread_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self.thread_table)

        bottom_row = QHBoxLayout()
        self.completed_label = QLabel('Completed: 0')
        self.duplicate_label = QLabel('Duplicates: 0')
        bottom_row.addWidget(self.completed_label)
        bottom_row.addStretch()
        bottom_row.addWidget(self.duplicate_label)
        layout.addLayout(bottom_row)

    def refresh(self):
        try:
            self._refresh()
        except Exception:
            pass

    def _refresh(self):
        try:
            runner = self.get_runner()
            downloader = getattr(runner, 'downloader', None)
            extractor = getattr(runner, 'extractor', None)
            fetcher_obj = getattr(runner, '_current_fetch_object', None)
        except RuntimeError:
            runner = None
            downloader = None
            extractor = None
            fetcher_obj = None
        self.fetcher_object_label.setText(f'Current: {fetcher_obj or "—"}')

        if runner is None or downloader is None:
            self.pipeline_label.setText('No active session')
            self.extraction_table.setRowCount(0)
            self.thread_table.setRowCount(0)
            self.completed_label.setText('Completed: —')
            self.duplicate_label.setText('Duplicates: —')
            return

        sub_q = getattr(runner, 'submission_queue', None)
        dl_q = getattr(runner, 'download_queue', None)
        ext_futures = len(getattr(extractor, 'futures', []))
        dl_futures = len(getattr(downloader, 'futures', []))
        ext_running = getattr(extractor, 'running', False)
        dl_running = getattr(downloader, 'running', False)

        sub_qsize = sub_q.qsize() if sub_q is not None else '?'
        dl_qsize = dl_q.qsize() if dl_q is not None else '?'

        ext_status = 'running' if ext_running else 'idle'
        dl_status = 'running' if dl_running else 'idle'

        self.pipeline_label.setText(
            f'Extractor: {ext_status}  |  extraction jobs: {ext_futures}  |  submission queue: {sub_qsize}\n'
            f'Downloader: {dl_status}  |  download jobs: {dl_futures}  |  download queue: {dl_qsize}'
        )

        active_ext = dict(getattr(extractor, '_active_extractions', {}))
        self.extraction_table.setRowCount(len(active_ext))
        for row, (thread, info) in enumerate(active_ext.items()):
            short = thread.rsplit('_', 1)[-1] if '_' in thread else thread
            self.extraction_table.setItem(row, 0, QTableWidgetItem(short))
            self.extraction_table.setItem(row, 1, QTableWidgetItem(info))

        active = dict(downloader._active_downloads)
        self.thread_table.setRowCount(len(active))
        for row, (thread, info) in enumerate(active.items()):
            short = thread.rsplit('_', 1)[-1] if '_' in thread else thread
            self.thread_table.setItem(row, 0, QTableWidgetItem(short))
            self.thread_table.setItem(row, 1, QTableWidgetItem(info))

        self.completed_label.setText(f'Completed: {downloader.download_count}')
        self.duplicate_label.setText(f'Duplicates: {downloader.duplicate_count}')
