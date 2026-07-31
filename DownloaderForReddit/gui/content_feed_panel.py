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

from ..messaging.message import ContentFoundPayload, ContentSkippedPayload
from ..utils.anonymizer import get_anonymizer
from ..viewmodels.hyperlink_delegate import HyperlinkDelegate

MAX_ROWS = 200
REDDIT_ID_ROLE = Qt.UserRole
# Unredacted text each row's display text is derived from, so screenshot mode can be toggled
# after the fact -- a QListWidgetItem holds only its final string, unlike a model's data().
RAW_TEXT_ROLE = Qt.UserRole + 1


class ContentFeedPanel(QWidget):
    def __init__(self):
        super().__init__()
        self._items_by_reddit_id: dict[str, QListWidgetItem] = {}
        # Distinct reasons seen per post -- a multi-item post (e.g. a gallery) can report the same
        # filter reason more than once, which shouldn't inflate the badge's count.
        self._skip_reasons_by_reddit_id: dict[str, list[str]] = {}
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
        raw = f'{label}<a href="{permalink}">{permalink}</a>'
        item = QListWidgetItem(get_anonymizer().redact(raw))
        item.setData(RAW_TEXT_ROLE, raw)
        item.setData(REDDIT_ID_ROLE, payload.reddit_id)
        if not payload.is_new:
            item.setForeground(Qt.gray)
        self._append_item(item)
        self._items_by_reddit_id[payload.reddit_id] = item

    def add_status(self, text: str):
        # Not tied to a reddit_id, but the label (u/name, r/name) still needs redaction like
        # any other row -- RAW_TEXT_ROLE lets refresh() re-render it if Screenshot Mode toggles
        # later.
        raw = html.escape(text)
        item = QListWidgetItem(get_anonymizer().redact(raw))
        item.setData(RAW_TEXT_ROLE, raw)
        item.setForeground(Qt.darkGray)
        self._append_item(item)

    def _append_item(self, item: QListWidgetItem):
        bar = self.list_widget.verticalScrollBar()
        pos, max_ = bar.value(), bar.maximum()
        at_bottom = max_ == 0 or pos == max_ or (pos / max_) * 100 >= 96
        self.list_widget.addItem(item)
        while self.list_widget.count() > MAX_ROWS:
            evicted = self.list_widget.takeItem(0)
            self._items_by_reddit_id.pop(evicted.data(REDDIT_ID_ROLE), None)
        if at_bottom:
            self.list_widget.scrollToBottom()

    def mark_skipped(self, payload: ContentSkippedPayload):
        # Appends a small badge to the post's existing "found" line, mirroring the [NEW]/[OLD]
        # bracketed-prefix style, rather than adding a new line or a long inline sentence. The
        # full reason still goes in the tooltip -- if the entry already scrolled out of MAX_ROWS,
        # there's nothing to append to, so drop it silently, same as any other aged-out row.
        item = self._items_by_reddit_id.get(payload.reddit_id)
        if item is None:
            return
        reasons = self._skip_reasons_by_reddit_id.setdefault(payload.reddit_id, [])
        if payload.reason in reasons:
            return
        reasons.append(payload.reason)
        self._render_item(item)
        item.setToolTip("\n".join(reasons))

    def _render_item(self, item: QListWidgetItem):
        reasons = self._skip_reasons_by_reddit_id.get(item.data(REDDIT_ID_ROLE), [])
        badges = "".join(f" [SKIP-{self._reason_word(r)}]" for r in reasons)
        item.setText(f"{get_anonymizer().redact(item.data(RAW_TEXT_ROLE))}{badges}")

    def refresh(self):
        for row in range(self.list_widget.count()):
            self._render_item(self.list_widget.item(row))

    @staticmethod
    def _reason_word(reason: str) -> str:
        # Reason strings are human sentences (see submission_handler.py, base_extractor.py), not
        # a fixed enum, so the badge word is inferred rather than kept as a second, parallel code
        # that would have to stay in sync with every skip call site by hand.
        lowered = reason.lower()
        for keyword, word in (
            ("duplicate", "dupe"),
            ("crosspost", "xpost"),
            ("text post", "txt"),
            ("extension", "ext"),
            ("video", "vid"),
        ):
            if keyword in lowered:
                return word
        first_word = reason.split(maxsplit=1)[0] if reason else "skip"
        return html.escape(
            "".join(c for c in first_word if c.isalnum()).lower() or "skip"
        )

    def clear(self):
        self.list_widget.clear()
        self._items_by_reddit_id.clear()
        self._skip_reasons_by_reddit_id.clear()
