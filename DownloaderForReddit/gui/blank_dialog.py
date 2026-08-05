from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QDialog, QVBoxLayout


class BlankDialog(QDialog):
    closing = pyqtSignal()

    def __init__(self, parent=None):
        QDialog.__init__(self, parent=parent)
        self.setLayout(QVBoxLayout())

    def add_widgets(self, *widgets):
        for x in widgets:
            self.layout().addWidget(x)

    def closeEvent(self, event):
        self.closing.emit()
        super().closeEvent(event)
