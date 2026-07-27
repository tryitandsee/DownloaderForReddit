# [mine] feat(gui): live feed of discovered content
import html

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QMenu,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..messaging.message import ContentFoundPayload
from ..viewmodels.hyperlink_delegate import HyperlinkDelegate

MAX_ROWS = 200


class ContentFeedPanel(QWidget):
    def __init__(self):
        super().__init__()
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)

        # A single-column list instead of a table -- a table's auto-resizing columns
        # recompute on every insert, which gets expensive as this grows; a list item is just a
        # line of text, no per-column layout pass needed.
        self.list_widget = QListWidget()
        self.list_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._context_menu)
        # Renders the item's text as HTML and opens <a href> clicks in the app's own Playwright
        # browser (see HyperlinkDelegate) -- same delegate output_list_view already uses.
        self.list_widget.setItemDelegate(HyperlinkDelegate())
        layout.addWidget(self.list_widget)

    def _context_menu(self):
        menu = QMenu()
        menu.addAction("Clear Content Feed", self.clear)
        menu.exec_(QCursor.pos())

    def add_entry(self, payload: ContentFoundPayload):
        # Equal width so bracketed status labels line up visually -- lost automatically when
        # this stopped being a QTableWidget with a dedicated status column.
        status = "NEW" if payload.is_new else "OLD"
        by = payload.author or payload.subreddit
        label = html.escape(f"[{status}] {by}: ")
        permalink = html.escape(payload.permalink)
        item = QListWidgetItem(f'{label}<a href="{permalink}">{permalink}</a>')
        if not payload.is_new:
            item.setForeground(Qt.gray)
        bar = self.list_widget.verticalScrollBar()
        pos, max_ = bar.value(), bar.maximum()
        at_bottom = max_ == 0 or pos == max_ or (pos / max_) * 100 >= 96
        self.list_widget.addItem(item)
        while self.list_widget.count() > MAX_ROWS:
            self.list_widget.takeItem(0)
        if at_bottom:
            self.list_widget.scrollToBottom()

    def clear(self):
        self.list_widget.clear()
