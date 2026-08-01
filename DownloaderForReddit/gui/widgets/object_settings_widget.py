from PyQt5.QtCore import Qt
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import QMenu, QWidget

from ...database.model_enums import *
from ...guiresources.widgets.object_settings_widget_auto import Ui_ObjectSettingsWidget
from ...utils import injector
from ...utils.token_parser import TokenParser


class ObjectSettingsWidget(QWidget, Ui_ObjectSettingsWidget):
    def __init__(self, parent=None):
        QWidget.__init__(self, parent=parent)
        self.setupUi(self)
        self.settings_manager = injector.get_settings_manager()
        self.setup_widgets()
        self.connect_edit_widgets()
        self.selected_objects = []

    def set_objects(self, object_list):
        if object_list:
            self.selected_objects = object_list
            self.sync_widgets_to_object()

    def setup_widgets(self):
        for ext in ["txt", "html"]:
            self.self_post_file_format_combo.addItem(f".{ext}", ext)
            self.comment_file_format_combo.addItem(f".{ext}", ext)
        for value in NsfwFilter:
            self.nsfw_filter_combo.addItem(value.display_name, value)
        for value in CommentDownload:
            self.comment_extract_combo.addItem(value.display_name, value)
            self.comment_download_combo.addItem(value.display_name, value)
            self.comment_content_download_combo.addItem(value.display_name, value)

        self.hash_content_checkbox.stateChanged.connect(
            self.sync_duplicate_controls_enabled
        )
        self.duplicate_control_method_combo.currentIndexChanged.connect(
            self.sync_duplicate_controls_enabled
        )

        self.comment_limit_max_button.clicked.connect(
            lambda: self.comment_limit_spinbox.setValue(
                self.comment_limit_spinbox.maximum()
            )
        )
        self.comment_depth_max_button.clicked.connect(
            lambda: self.comment_depth_spinbox.setValue(
                self.comment_depth_spinbox.maximum()
            )
        )
        self.comment_reply_max_button.clicked.connect(
            lambda: self.comment_reply_limit_spinbox.setValue(
                self.comment_reply_limit_spinbox.maximum()
            )
        )

        for value in DuplicateControlMethod:
            self.duplicate_control_method_combo.addItem(value.display_name, value)

        for line_edit in (
            self.post_download_naming_line_edit,
            self.post_save_path_structure_line_edit,
            self.comment_download_naming_line_edit,
            self.comment_save_path_structure_line_edit,
            self.duplicate_naming_line_edit,
            self.duplicate_save_structure_line_edit,
        ):
            line_edit.setContextMenuPolicy(Qt.CustomContextMenu)
            line_edit.customContextMenuRequested.connect(
                lambda _checked=False, le=line_edit: self.path_token_context_menu(le)
            )
        for button, line_edit in (
            (
                self.post_download_naming_available_tokens_button,
                self.post_download_naming_line_edit,
            ),
            (
                self.post_save_structure_available_tokens_button,
                self.post_save_path_structure_line_edit,
            ),
            (
                self.comment_download_naming_available_tokens_button,
                self.comment_download_naming_line_edit,
            ),
            (
                self.comment_save_structure_available_tokens_button,
                self.comment_save_path_structure_line_edit,
            ),
            (
                self.duplicate_naming_available_tokens_button,
                self.duplicate_naming_line_edit,
            ),
            (
                self.duplicate_save_structure_available_tokens_button,
                self.duplicate_save_structure_line_edit,
            ),
        ):
            button.clicked.connect(
                lambda _checked=False, le=line_edit: self.path_token_context_menu(le)
            )

    def path_token_context_menu(self, line_edit):
        menu = QMenu()
        for key in TokenParser.token_dict:
            menu.addAction(
                key.replace("_", " ").title(),
                lambda token=key: self.insert_token(line_edit, token),
            )
        menu.exec_(QCursor.pos())

    def insert_token(self, line_edit, token):
        if line_edit.hasSelectedText():
            line_edit.del_()
        line_edit.insert(f"%[{token}]")

    def connect_edit_widgets(self):
        self.setup_checkbox(self.enable_download_checkbox, "download_enabled")
        self.setup_checkbox(self.avoid_duplicates_checkbox, "avoid_duplicates")
        self.setup_checkbox(self.hash_content_checkbox, "hash_duplicates")
        self.duplicate_control_method_combo.currentIndexChanged.connect(
            lambda x: self.set_object_value(
                "duplicate_control_method",
                self.duplicate_control_method_combo.itemData(x),
            )
        )
        self.duplicate_naming_line_edit.textChanged.connect(
            lambda text: self.set_object_value("duplicate_naming_method", text)
        )
        self.duplicate_save_structure_line_edit.textChanged.connect(
            lambda text: self.set_object_value("duplicate_save_structure", text)
        )
        self.setup_checkbox(
            self.extract_self_post_content_checkbox, "extract_self_post_links"
        )
        self.setup_checkbox(
            self.download_self_post_text_checkbox, "download_self_post_text"
        )
        self.self_post_file_format_combo.currentIndexChanged.connect(
            lambda: self.set_object_value(
                "self_post_file_format",
                self.self_post_file_format_combo.currentData(Qt.UserRole),
            )
        )
        self.setup_checkbox(self.download_videos_checkbox, "download_videos")
        self.setup_checkbox(self.download_images_checkbox, "download_images")
        self.setup_checkbox(self.download_gifs_checkbox, "download_gifs")
        self.nsfw_filter_combo.currentIndexChanged.connect(
            lambda x: self.set_object_value(
                "download_nsfw", self.nsfw_filter_combo.itemData(x)
            )
        )
        self.post_download_naming_line_edit.textChanged.connect(
            lambda text: self.set_object_value("post_download_naming_method", text)
        )
        self.post_save_path_structure_line_edit.textChanged.connect(
            lambda text: self.set_object_value("post_save_structure", text)
        )
        self.comment_extract_combo.currentIndexChanged.connect(
            lambda x: self.set_object_value(
                "extract_comments", self.comment_extract_combo.itemData(x)
            )
        )
        self.comment_download_combo.currentIndexChanged.connect(
            lambda x: self.set_object_value(
                "download_comments", self.comment_download_combo.itemData(x)
            )
        )
        self.comment_content_download_combo.currentIndexChanged.connect(
            lambda x: self.set_object_value(
                "download_comment_content",
                self.comment_content_download_combo.itemData(x),
            )
        )
        self.comment_limit_spinbox.valueChanged.connect(
            lambda x: self.set_object_value("comment_limit", x)
        )
        self.comment_depth_spinbox.valueChanged.connect(
            lambda x: self.set_object_value("comment_depth", x)
        )
        self.comment_reply_limit_spinbox.valueChanged.connect(
            lambda x: self.set_object_value("comment_reply_limit", x)
        )
        self.comment_file_format_combo.currentIndexChanged.connect(
            lambda: self.set_object_value(
                "comment_file_format",
                self.comment_file_format_combo.currentData(Qt.UserRole),
            )
        )
        self.comment_download_naming_line_edit.textChanged.connect(
            lambda text: self.set_object_value("comment_naming_method", text)
        )
        self.comment_save_path_structure_line_edit.textChanged.connect(
            lambda text: self.set_object_value("comment_save_structure", text)
        )

    def setup_checkbox(self, checkbox, attribute):
        checkbox.stateChanged.connect(
            lambda: self.set_object_value(attribute, checkbox.isChecked())
        )

    def set_object_value(self, attr, value, set_null=False):
        for obj in self.selected_objects:
            if set_null and value == "":
                value = None
            setattr(obj, attr, value)

    def sync_widgets_to_object(self):
        self.sync_optional()
        self.sync_checkbox(self.avoid_duplicates_checkbox, "avoid_duplicates")
        self.sync_checkbox(self.hash_content_checkbox, "hash_duplicates")
        self.sync_combo(self.duplicate_control_method_combo, "duplicate_control_method")
        self.sync_line_edit(self.duplicate_naming_line_edit, "duplicate_naming_method")
        self.sync_line_edit(
            self.duplicate_save_structure_line_edit, "duplicate_save_structure"
        )
        self.sync_checkbox(
            self.extract_self_post_content_checkbox, "extract_self_post_links"
        )
        self.sync_checkbox(
            self.download_self_post_text_checkbox, "download_self_post_text"
        )
        self.sync_combo(self.self_post_file_format_combo, "self_post_file_format")
        self.sync_checkbox(self.download_videos_checkbox, "download_videos")
        self.sync_checkbox(self.download_images_checkbox, "download_images")
        self.sync_checkbox(self.download_gifs_checkbox, "download_gifs")
        self.sync_combo(self.nsfw_filter_combo, "download_nsfw")
        self.sync_line_edit(
            self.post_download_naming_line_edit, "post_download_naming_method"
        )
        self.sync_line_edit(
            self.post_save_path_structure_line_edit, "post_save_structure"
        )
        self.sync_combo(self.comment_extract_combo, "extract_comments")
        self.sync_combo(self.comment_download_combo, "download_comments")
        self.sync_combo(self.comment_content_download_combo, "download_comment_content")
        self.sync_spin_box(self.comment_limit_spinbox, "comment_limit")
        self.sync_spin_box(self.comment_depth_spinbox, "comment_depth")
        self.sync_spin_box(self.comment_reply_limit_spinbox, "comment_reply_limit")
        self.sync_combo(self.comment_file_format_combo, "comment_file_format")
        self.sync_line_edit(
            self.comment_download_naming_line_edit, "comment_naming_method"
        )
        self.sync_line_edit(
            self.comment_save_path_structure_line_edit, "comment_save_structure"
        )

    def sync_optional(self):
        try:
            self.sync_checkbox(self.enable_download_checkbox, "download_enabled")
            visibility = True
        except AttributeError, TypeError:
            visibility = False
        self.enable_download_checkbox.setVisible(visibility)

    def sync_checkbox(self, checkbox, attr):
        value = self.get_value(attr)
        if value is not None:
            if value:
                checkbox.setCheckState(2)
            else:
                checkbox.setCheckState(0)
        else:
            checkbox.setCheckState(1)

    def sync_combo(self, combo, attr):
        value = self.get_value(attr)
        if value is not None:
            combo.setCurrentIndex(combo.findData(value))
        else:
            combo.setCurrentIndex(-1)

    def sync_spin_box(self, spin_box, attr):
        value = self.get_value(attr)
        if value is not None:
            spin_box.setValue(value)
        else:
            spin_box.lineEdit().setText("-")

    def sync_date_edit(self, date_edit, attr):
        value = self.get_value(attr)
        if value is not None:
            date_edit.setDateTime(value)
        else:
            date_edit.lineEdit().setText("-")

    def sync_line_edit(self, line_edit, attr):
        value = self.get_value(attr)
        if value is None:
            value = ""
        line_edit.setText(value)

    def get_value(self, attr):
        value = getattr(self.selected_objects[0], attr)
        if len(self.selected_objects) == 1 or all(
            getattr(x, attr) == value for x in self.selected_objects
        ):
            return value
        return None

    def sync_duplicate_controls_enabled(self) -> None:
        """
        Updates the enabled state of duplicate control-related inputs based on whether hashing is enabled.
        """
        hash_enabled = self.hash_content_checkbox.isChecked()
        self.duplicate_control_method_combo.setEnabled(hash_enabled)
        self.duplicate_control_method_combo_label.setEnabled(hash_enabled)
