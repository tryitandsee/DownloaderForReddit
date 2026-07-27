from datetime import datetime

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QButtonGroup, QWidget

from ...core import const
from ...database.model_enums import *
from ...guiresources.widgets.object_settings_widget_auto import Ui_ObjectSettingsWidget
from ...utils import injector


class ObjectSettingsWidget(QWidget, Ui_ObjectSettingsWidget):

    def __init__(self, parent=None):
        QWidget.__init__(self, parent=parent)
        self.setupUi(self)
        self.settings_manager = injector.get_settings_manager()
        self.setup_widgets()
        self.connect_edit_widgets()
        self.selected_objects = []

        date_limit_group = QButtonGroup(self)
        date_limit_group.addButton(self.absolute_date_limit_radio)
        date_limit_group.addButton(self.custom_date_limit_radio)

        date_lock_group = QButtonGroup(self)
        date_lock_group.addButton(self.update_custom_date_limit_radio)
        date_lock_group.addButton(self.do_not_update_custom_date_limit_radio)

    @property
    def object_type(self):
        try:
            return self.selected_objects[0].object_type
        except (IndexError, AttributeError):
            return None

    def set_objects(self, object_list):
        if object_list:
            self.selected_objects = object_list
            self.sync_widgets_to_object()
            self.sync_sort_methods(self.object_type)

    def sync_sort_methods(self, object_type):
        pos = self.post_sort_combo.findData(PostSortMethod.RISING, Qt.UserRole)
        if object_type == 'SUBREDDIT':
            if pos < 0:
                self.post_sort_combo.insertItem(2, 'RISING', PostSortMethod.RISING)
        else:
            if pos >= 0:
                self.post_sort_combo.removeItem(pos)

    def setup_widgets(self):
        for value in LimitOperator:
            self.score_limit_operator_combo.addItem(value.display_name, value)
            self.comment_score_operator_combo.addItem(value.display_name, value)
        for ext in ['txt', 'html']:
            self.self_post_file_format_combo.addItem(f'.{ext}', ext)
            self.comment_file_format_combo.addItem(f'.{ext}', ext)
        for value in NsfwFilter:
            self.nsfw_filter_combo.addItem(value.display_name, value)
        for value in PostSortMethod:
            self.post_sort_combo.addItem(value.display_name, value)
        for value in CommentDownload:
            self.comment_extract_combo.addItem(value.display_name, value)
            self.comment_download_combo.addItem(value.display_name, value)
            self.comment_content_download_combo.addItem(value.display_name, value)
        for value in CommentSortMethod:
            self.comment_sort_combo.addItem(value.display_name, value)

        self.hash_content_checkbox.stateChanged.connect(self.sync_duplicate_controls_enabled)
        self.duplicate_control_method_combo.currentIndexChanged.connect(self.sync_duplicate_controls_enabled)

        self.post_limit_max_button.clicked.connect(
            lambda: self.post_limit_spinbox.setValue(self.post_limit_spinbox.maximum()))
        self.comment_limit_max_button.clicked.connect(
            lambda: self.comment_limit_spinbox.setValue(self.comment_limit_spinbox.maximum()))
        self.comment_depth_max_button.clicked.connect(
            lambda: self.comment_depth_spinbox.setValue(self.comment_depth_spinbox.maximum()))
        self.comment_reply_max_button.clicked.connect(
            lambda: self.comment_reply_limit_spinbox.setValue(self.comment_reply_limit_spinbox.maximum()))

        for value in DuplicateControlMethod:
            self.duplicate_control_method_combo.addItem(value.display_name, value)

    def connect_edit_widgets(self):
        self.setup_checkbox(self.lock_settings_checkbox, 'lock_settings')
        self.setup_checkbox(self.enable_download_checkbox, 'download_enabled')
        self.post_limit_spinbox.valueChanged.connect(lambda x: self.set_object_value('post_limit', x))
        self.score_limit_spinbox.valueChanged.connect(lambda x: self.set_object_value('post_score_limit', x))
        self.score_limit_operator_combo.currentIndexChanged.connect(
            lambda x: self.set_object_value('post_score_limit_operator', self.score_limit_operator_combo.itemData(x))
        )
        self.limit_date_checkbox.stateChanged.connect(self.limit_date_checkbox_toggled)
        self.custom_date_limit_radio.toggled.connect(self.custom_date_limit_toggled)
        self.custom_date_limit_edit.dateTimeChanged.connect(self.set_date_limit_from_edit)
        self.update_custom_date_limit_radio.toggled.connect(lambda x: self.set_object_value('update_date_limit', x))

        self.setup_checkbox(self.avoid_duplicates_checkbox, 'avoid_duplicates')
        self.setup_checkbox(self.hash_content_checkbox, 'hash_duplicates')
        self.duplicate_control_method_combo.currentIndexChanged.connect(
            lambda x: self.set_object_value('duplicate_control_method', self.duplicate_control_method_combo.itemData(x))
        )
        self.setup_checkbox(self.extract_self_post_content_checkbox, 'extract_self_post_links')
        self.setup_checkbox(self.download_self_post_text_checkbox, 'download_self_post_text')
        self.self_post_file_format_combo.currentIndexChanged.connect(
            lambda: self.set_object_value('self_post_file_format',
                                          self.self_post_file_format_combo.currentData(Qt.UserRole))
        )
        self.setup_checkbox(self.download_videos_checkbox, 'download_videos')
        self.setup_checkbox(self.download_images_checkbox, 'download_images')
        self.setup_checkbox(self.download_gifs_checkbox, 'download_gifs')
        self.nsfw_filter_combo.currentIndexChanged.connect(
            lambda x: self.set_object_value('download_nsfw', self.nsfw_filter_combo.itemData(x))
        )
        self.post_sort_combo.currentIndexChanged.connect(
            lambda x: self.set_object_value('post_sort_method', self.post_sort_combo.itemData(x))
        )
        self.comment_extract_combo.currentIndexChanged.connect(
            lambda x: self.set_object_value('extract_comments', self.comment_extract_combo.itemData(x))
        )
        self.comment_download_combo.currentIndexChanged.connect(
            lambda x: self.set_object_value('download_comments', self.comment_download_combo.itemData(x))
        )
        self.comment_content_download_combo.currentIndexChanged.connect(
            lambda x: self.set_object_value('download_comment_content', self.comment_content_download_combo.itemData(x))
        )
        self.comment_limit_spinbox.valueChanged.connect(lambda x: self.set_object_value('comment_limit', x))
        self.comment_depth_spinbox.valueChanged.connect(lambda x: self.set_object_value('comment_depth', x))
        self.comment_reply_limit_spinbox.valueChanged.connect(lambda x: self.set_object_value('comment_reply_limit', x))
        self.comment_score_limit_spinbox.valueChanged.connect(lambda x: self.set_object_value('comment_score_limit', x))
        self.comment_score_operator_combo.currentIndexChanged.connect(
            lambda x: self.set_object_value('comment_score_limit_operator',
                                            self.comment_score_operator_combo.itemData(x))
        )
        self.comment_sort_combo.currentIndexChanged.connect(
            lambda x: self.set_object_value('comment_sort_method', self.comment_sort_combo.itemData(x))
        )
        self.comment_file_format_combo.currentIndexChanged.connect(
            lambda: self.set_object_value('comment_file_format',
                                          self.comment_file_format_combo.currentData(Qt.UserRole))
        )
    def setup_checkbox(self, checkbox, attribute):
        checkbox.stateChanged.connect(lambda: self.set_object_value(attribute, checkbox.isChecked()))

    def limit_date_checkbox_toggled(self):
        checked = self.limit_date_checkbox.isChecked()
        self.absolute_date_limit_radio.setEnabled(checked)
        self.custom_date_limit_radio.setEnabled(checked)
        self.custom_date_limit_edit.setEnabled(checked)
        self.update_custom_date_limit_radio.setEnabled(checked)
        self.do_not_update_custom_date_limit_radio.setEnabled(checked)
        if not checked:
            self.set_object_value('date_limit', datetime.fromtimestamp(const.FIRST_POST_EPOCH - 2000))
        else:
            self.custom_date_limit_edit.setDateTime(datetime.fromtimestamp(const.FIRST_POST_EPOCH))

    def custom_date_limit_toggled(self):
        checked = self.custom_date_limit_radio.isChecked() and self.limit_date_checkbox.isChecked()
        self.custom_date_limit_edit.setEnabled(checked)
        self.update_custom_date_limit_radio.setEnabled(checked)
        self.do_not_update_custom_date_limit_radio.setEnabled(checked)
        if checked:
            self.set_date_limit_from_edit()
        else:
            self.set_object_value('date_limit', None)

    def set_date_limit_from_edit(self):
        epoch = self.custom_date_limit_edit.dateTime().toSecsSinceEpoch()
        self.set_object_value('date_limit', datetime.fromtimestamp(epoch))

    def set_object_value(self, attr, value, set_null=False):
        for obj in self.selected_objects:
            if set_null and value == '':
                value = None
            setattr(obj, attr, value)

    def sync_widgets_to_object(self):
        self.sync_optional()
        self.sync_spin_box(self.post_limit_spinbox, 'post_limit')
        self.sync_spin_box(self.score_limit_spinbox, 'post_score_limit')
        self.sync_combo(self.score_limit_operator_combo, 'post_score_limit_operator')
        self.sync_date_limits()
        self.sync_checkbox(self.avoid_duplicates_checkbox, 'avoid_duplicates')
        self.sync_checkbox(self.hash_content_checkbox, 'hash_duplicates')
        self.sync_combo(self.duplicate_control_method_combo, 'duplicate_control_method')
        self.sync_checkbox(self.extract_self_post_content_checkbox, 'extract_self_post_links')
        self.sync_checkbox(self.download_self_post_text_checkbox, 'download_self_post_text')
        self.sync_combo(self.self_post_file_format_combo, 'self_post_file_format')
        self.sync_checkbox(self.download_videos_checkbox, 'download_videos')
        self.sync_checkbox(self.download_images_checkbox, 'download_images')
        self.sync_checkbox(self.download_gifs_checkbox, 'download_gifs')
        self.sync_combo(self.nsfw_filter_combo, 'download_nsfw')
        self.sync_combo(self.post_sort_combo, 'post_sort_method')
        self.sync_combo(self.comment_extract_combo, 'extract_comments')
        self.sync_combo(self.comment_download_combo, 'download_comments')
        self.sync_combo(self.comment_content_download_combo, 'download_comment_content')
        self.sync_spin_box(self.comment_limit_spinbox, 'comment_limit')
        self.sync_spin_box(self.comment_depth_spinbox, 'comment_depth')
        self.sync_spin_box(self.comment_reply_limit_spinbox, 'comment_reply_limit')
        self.sync_spin_box(self.comment_score_limit_spinbox, 'comment_score_limit')
        self.sync_combo(self.comment_score_operator_combo, 'comment_score_limit_operator')
        self.sync_combo(self.comment_sort_combo, 'comment_sort_method')
        self.sync_combo(self.comment_file_format_combo, 'comment_file_format')

    def sync_optional(self):
        try:
            self.sync_checkbox(self.lock_settings_checkbox, 'lock_settings')
            self.sync_checkbox(self.enable_download_checkbox, 'download_enabled')
            visibility = True
        except (AttributeError, TypeError):
            visibility = False
        self.lock_settings_checkbox.setVisible(visibility)
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
            spin_box.lineEdit().setText('-')

    def sync_date_edit(self, date_edit, attr):
        value = self.get_value(attr)
        if value is not None:
            date_edit.setDateTime(value)
        else:
            date_edit.lineEdit().setText('-')

    def sync_line_edit(self, line_edit, attr):
        value = self.get_value(attr)
        if value is None:
            value = ''
        line_edit.setText(value)

    def get_value(self, attr):
        value = getattr(self.selected_objects[0], attr)
        if len(self.selected_objects) == 1 or all(getattr(x, attr) == value for x in self.selected_objects):
            return value
        return None

    def sync_date_limits(self):
        date_limit = self.selected_objects[0].date_limit
        if all(x.date_limit == date_limit for x in self.selected_objects):
            if date_limit is not None:
                if date_limit.timestamp() < const.FIRST_POST_EPOCH:
                    self.limit_date_checkbox.setChecked(False)
                else:
                    self.limit_date_checkbox.setChecked(True)
                    self.custom_date_limit_radio.setChecked(True)
                    self.custom_date_limit_edit.setDateTime(date_limit)
            else:
                self.limit_date_checkbox.setChecked(True)
                self.absolute_date_limit_radio.setChecked(True)
                self.custom_date_limit_edit.setDisabled(True)
                self.update_custom_date_limit_radio.setDisabled(True)
                self.do_not_update_custom_date_limit_radio.setDisabled(True)

        abs_date_limit = self.selected_objects[0].absolute_date_limit
        if all(x.absolute_date_limit == abs_date_limit for x in self.selected_objects):
            self.absolute_date_limit_label.setText(self.selected_objects[0].absolute_date_limit_display)
        else:
            self.absolute_date_limit_label.setText('---')

        update_limit = self.selected_objects[0].update_date_limit
        if all(x.update_date_limit == update_limit for x in self.selected_objects):
            if update_limit:
                self.update_custom_date_limit_radio.setChecked(True)
            else:
                self.do_not_update_custom_date_limit_radio.setChecked(True)
        else:
            self.update_custom_date_limit_radio.setChecked(False)
            self.do_not_update_custom_date_limit_radio.setChecked(False)

    def sync_duplicate_controls_enabled(self) -> None:
        """
        Updates the enabled state of duplicate control-related inputs based on whether hashing is enabled.
        """
        hash_enabled = self.hash_content_checkbox.isChecked()
        self.duplicate_control_method_combo.setEnabled(hash_enabled)
        self.duplicate_control_method_combo_label.setEnabled(hash_enabled)
