from PyQt6.QtCore import QAbstractListModel, QModelIndex, Qt, pyqtSignal
from PyQt6.QtGui import QColor

from ..utils import html_formatting, injector
from ..utils.anonymizer import get_anonymizer


class OutputViewModel(QAbstractListModel):
    added = pyqtSignal()

    """
    List model that controls the output of messages collected from all parts of the application.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings_manager = injector.get_settings_manager()
        self.display_priority = self.settings_manager.output_priority_level
        self.messages = []
        self.display_messages = []

    def update_output_level(self):
        priority = self.settings_manager.output_priority_level
        if priority != self.display_priority:
            self.display_priority = priority
            self.display_messages.clear()
            for message in self.messages:
                if message.priority.value >= self.display_priority.value:
                    self.insertRow(message)

    def handle_message(self, message):
        self.messages.append(message)
        if message.priority.value >= self.display_priority.value:
            self.insertRow(message)

    def rowCount(self, *args):
        return len(self.display_messages)

    def insertRow(self, item, parent=QModelIndex(), *args):
        self.beginInsertRows(parent, self.rowCount() - 1, self.rowCount())
        self.display_messages.append(item)
        self.endInsertRows()
        self.added.emit()

    def removeRow(self, row, parent=QModelIndex(), *args):
        self.beginRemoveRows(parent, row, row)
        del self.display_messages[row]
        self.endRemoveRows()

    def refresh(self):
        row_count = self.rowCount()
        if row_count == 0:
            return
        self.dataChanged.emit(self.index(0), self.index(row_count - 1))

    def clear(self, parent=QModelIndex()):
        self.beginRemoveRows(parent, 0, self.rowCount() - 1)
        self.messages.clear()
        self.display_messages.clear()
        self.endRemoveRows()

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        row = index.row()
        if role == Qt.ItemDataRole.DisplayRole:
            if self.settings_manager.show_priority_level:
                text = self.display_messages[row].output
            else:
                text = self.display_messages[row].message
            return html_formatting.format_html(get_anonymizer().redact(text))
        if (
            role == Qt.ItemDataRole.ForegroundRole
            and self.settings_manager.use_color_output
        ):
            r, g, b = getattr(
                self.settings_manager,
                f"{self.display_messages[row].priority.name.lower()}_color",
            )
            return QColor(r, g, b)
        return None
