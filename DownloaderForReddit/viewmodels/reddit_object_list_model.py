import logging
from datetime import UTC, datetime, timedelta

from PyQt5.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    Qt,
    QThread,
    pyqtSignal,
)
from PyQt5.QtGui import QColor
from sqlalchemy import case, func
from sqlalchemy.orm.exc import NoResultFound

from ..core.reddit_object_creator import RedditObjectCreator
from ..database.filters import RedditObjectFilter
from ..database.models import (
    Content,
    ListAssociation,
    Post,
    RedditObject,
    RedditObjectList,
)
from ..messaging.message import Message
from ..utils import general_utils, injector
from ..utils.anonymizer import get_anonymizer


class RedditObjectListModel(QAbstractTableModel):
    reddit_object_added = pyqtSignal(int)
    existing_object_added = pyqtSignal(tuple)
    new_object_in_list = pyqtSignal(int)
    count_change = pyqtSignal(int)

    columns = (
        "name",
        "date_added",
        "last_download",
        "date_last_download_utc",
        "expected_new",
    )
    column_headers = (
        "Name",
        "Date Added",
        "Last Download",
        "Last Checked",
        "Expected",
    )

    velocity_window_days = 30
    velocity_window_lag_days = 7

    def __init__(self, list_type):
        """
        A list model that holds a list of reddit objects to display.  Handles calls to the database made through the
        GUI.
        """
        super().__init__()
        self.logger = logging.getLogger(f"DownloaderForReddit.{__name__}")
        self.settings_manager = injector.get_settings_manager()
        self.db = injector.get_database_handler()
        self.session = self.db.get_session()
        self.list_type = list_type
        self.list = None
        self.reddit_objects = None

        self.validator = None
        self.validation_thread = None
        self.validating = False
        self.last_added = None

        self.sort_column = None
        self.sort_desc = False
        self.search_term = ""
        self.last_download_cache = {}
        self.expected_new_cache = {}

    @property
    def name(self):
        try:
            return self.list.name
        except AttributeError:
            return None

    def get_id_list(self, download_enabled=True):
        try:
            if download_enabled:
                return [x.id for x in self.reddit_objects if x.download_enabled]
            return [x.id for x in self.reddit_objects]
        except TypeError:
            # Indicates there is no list set for this model
            return []

    def get_object(self, object_name):
        for ro in self.reddit_objects:
            if ro.name == object_name:
                return ro
        return None

    def add_new_list(self, list_name, list_type):
        creator = RedditObjectCreator(list_type)
        ro_list = creator.create_reddit_object_list(list_name)
        if ro_list is not None:
            self.session.add(ro_list)
            self.list = ro_list
            self.sort_list()
            return True
        return False

    def delete_current_list(self):
        try:
            list_id = self.list.id
            self.reddit_objects.clear()
            self.list = None
            self.session.query(ListAssociation).filter(
                ListAssociation.reddit_object_list_id == list_id
            ).delete()
            self.session.query(RedditObjectList).filter(
                RedditObjectList.id == list_id
            ).delete()
            self.session.commit()
        except AttributeError:
            pass

    def set_list(self, list_name):
        try:
            self.list = (
                self.session.query(RedditObjectList)
                .filter(RedditObjectList.name == list_name)
                .filter(RedditObjectList.list_type == self.list_type)
                .one()
            )
            self.sort_list()
            self.refresh_expected_new()
        except NoResultFound:
            pass

    def sort_list(self):
        try:
            order = self.settings_manager.list_order_method
            desc = self.settings_manager.order_list_desc
            f = RedditObjectFilter()
            filters = (("name", "like", self.search_term),) if self.search_term else ()
            self.beginResetModel()
            try:
                self.reddit_objects = f.filter(
                    self.session,
                    *filters,
                    query=self.list.reddit_objects,
                    order_by=order,
                    desc=desc,
                ).all()
                self.refresh_last_download_cache()
                self.apply_column_sort()
            finally:
                self.endResetModel()
            self.check_last_added()
            self.send_count_change()
        except AttributeError:
            # AttributeError indicates that no list is set for this view model
            pass

    def refresh_last_download_cache(self):
        """
        RedditObject.last_download is a per-row correlated query -- calling it once per row
        during table paint/sort (rather than its original one-off tooltip usage) is an N+1 query
        storm that also corrupts the shared session's transaction state under PyQt5's GUI event
        loop. Fetch every row's last download date in a single grouped query instead.
        """
        self.last_download_cache = {}
        ids = self.get_id_list(download_enabled=False)
        if not ids:
            return
        rows = (
            self.session.query(
                Post.significant_reddit_object_id, func.max(Content.download_date)
            )
            .join(Content)
            .filter(Post.significant_reddit_object_id.in_(ids))
            .group_by(Post.significant_reddit_object_id)
            .all()
        )
        self.last_download_cache = dict(rows)

    def refresh_expected_new_cache(self):
        """
        Estimates how many unseen posts each object has accumulated since we last confirmed
        coverage of it: score = posting rate * days elapsed since that confirmation.

        The rate window is lagged by velocity_window_lag_days because the most recent days are
        always still being filled in -- a post made two days ago may simply not have been
        discovered yet -- so a trailing window systematically undercounts.

        Not called from sort_list(): pool_idle drives refresh_session() -> sort_list() constantly
        during ambient browsing, and recomputing there would re-run this aggregate every time and
        reshuffle rows mid-browse. Scores update on list load and on the Refresh button only.
        """
        self.expected_new_cache = {}
        try:
            # Deliberately the whole list rather than self.reddit_objects, which is narrowed by
            # any active search. Unlike last_download_cache, this one is not rebuilt by
            # sort_list(), so a cache built while a search was active would leave every filtered
            # out row reading 0.0 once the search is cleared.
            reddit_objects = self.list.reddit_objects.all()
        except AttributeError:
            return
        if not reddit_objects:
            return
        ids = [ro.id for ro in reddit_objects]
        now_utc = datetime.now(UTC).replace(tzinfo=None)
        now_local = datetime.now()
        window_end = now_utc - timedelta(days=self.velocity_window_lag_days)
        window_start = window_end - timedelta(days=self.velocity_window_days)
        rows = (
            self.session.query(
                Post.significant_reddit_object_id,
                func.sum(
                    case(
                        [
                            (
                                (Post.date_posted >= window_start)
                                & (Post.date_posted < window_end),
                                1,
                            )
                        ],
                        else_=0,
                    )
                ),
                func.max(Post.extraction_date),
            )
            .filter(Post.significant_reddit_object_id.in_(ids))
            .group_by(Post.significant_reddit_object_id)
            .all()
        )
        post_data = {ro_id: (count, extraction) for ro_id, count, extraction in rows}
        for reddit_object in reddit_objects:
            count, last_extraction = post_data.get(reddit_object.id, (0, None))
            # date_last_download_utc is naive UTC while extraction_date is naive local, so each is
            # measured against its own clock and only the resulting durations are compared.
            deltas = []
            if reddit_object.date_last_download_utc is not None:
                deltas.append(now_utc - reddit_object.date_last_download_utc)
            if last_extraction is not None:
                deltas.append(now_local - last_extraction)
            if not deltas:
                self.expected_new_cache[reddit_object.id] = 0.0
                continue
            elapsed_days = max(0.0, min(deltas).total_seconds() / 86_400)
            # A rate measured over velocity_window_days says nothing credible about a horizon
            # longer than itself. Uncapped, the smoothing floor alone earned a year-dormant
            # account a score of 10 -- above the median active user -- purely for being stale.
            elapsed_days = min(elapsed_days, self.velocity_window_days)
            rate = ((count or 0) + 1) / (self.velocity_window_days + 1)
            self.expected_new_cache[reddit_object.id] = rate * elapsed_days

    def refresh_expected_new(self):
        self.refresh_expected_new_cache()
        self.apply_column_sort()
        self.refresh()

    def apply_column_sort(self):
        if self.sort_column is None or self.reddit_objects is None:
            return
        field = self.columns[self.sort_column]
        if field == "name":
            key = lambda ro: ro.name.lower()
        elif field == "last_download":
            key = lambda ro: (
                self.last_download_cache.get(ro.id) is not None,
                self.last_download_cache.get(ro.id),
            )
        elif field == "expected_new":
            key = lambda ro: self.expected_new_cache.get(ro.id, 0.0)
        else:
            key = lambda ro: (getattr(ro, field) is not None, getattr(ro, field))
        self.reddit_objects.sort(key=key, reverse=self.sort_desc)

    def sort(self, column, order=Qt.AscendingOrder):
        self.sort_column = column
        self.sort_desc = order == Qt.DescendingOrder
        self.apply_column_sort()
        self.refresh()

    def check_last_added(self):
        if self.last_added is not None:
            try:
                index = self.reddit_objects.index(self.last_added)
            except ValueError:
                # Not in the current (possibly search-filtered) results -- leave last_added set
                # so a later sort_list() call (e.g. once the search is cleared) can still find it.
                return
            self.new_object_in_list.emit(index)
            self.last_added = None

    def search_list(self, term):
        self.search_term = term or ""
        self.sort_list()

    def check_name(self, name):
        """
        Checks the reddit object list to see if an object with the supplied name exists in the list.
        :param name: The name that is to be checked for existence.
        :return: True if the name exists, False if it does not.
        :type name: str
        :rtype: bool
        """
        ro = (
            self.session.query(RedditObject)
            .filter(func.lower(RedditObject.name) == func.lower(name))
            .scalar()
        )
        return ro in self.reddit_objects

    def remove_reddit_objects(self, *reddit_objects):
        for ro in reddit_objects:
            self.list.reddit_objects.remove(ro)
        self.session.commit()
        self.sort_list()

    def add_reddit_object(self, name: str):
        self.add_reddit_objects([name])

    def add_reddit_objects(self, name_list: list):
        """
        A long and complicated method so that name validation can be done in a separate thread.  Sqlite objects can't
        be modified from a different thread than the one that they were created in.  This necessitates using PyQt's
        threading frame work, which is much more verbose than Python's standard, but which does support signaling.
        :param name_list: A list of names to be validated, made into reddit objects, and added to the current reddit
                          object list.
        """
        name_list = self.check_existing(name_list)
        self.validating = True
        self.validation_thread = QThread()
        self.validator = ObjectValidator(
            name_list, self.list_type, list_defaults=self.list.get_default_dict()
        )
        self.validator.moveToThread(self.validation_thread)
        self.validation_thread.started.connect(self.validator.run)
        self.validator.new_object_signal.connect(self.add_validated_reddit_object)
        self.validator.invalid_name_signal.connect(
            lambda name: Message.send_warning(f"Invalid name: {name}")
        )
        self.validator.finished.connect(self.validation_thread.quit)
        self.validator.finished.connect(self.validator.deleteLater)
        self.validation_thread.finished.connect(self.validation_thread.deleteLater)
        self.validation_thread.start()

    def check_existing(self, name_list):
        """
        Checks the supplied list of names for names that already exist in the database.  If duplicate names are found,
        the existing_object_added signal is emitted and the names are removed from the list.
        :param name_list: A list of names that are to be checked for duplication in the database.
        :return: The supplied list of names with any duplicate names removed.
        """
        existing_ids = []
        existing_names = []
        for name in name_list:
            ro = (
                self.session.query(RedditObject)
                .filter(func.lower(RedditObject.name) == name.lower())
                .first()
            )
            if ro is not None:
                existing_ids.append(ro.id)
                existing_names.append(ro.name)
                self.sync_existing_ro_to_list(ro)
                if ro in self.list.reddit_objects:
                    name_list.remove(name)
                    self.last_added = ro
        if len(existing_names) > 0:
            self.existing_object_added.emit(
                (self.list_type, existing_ids, existing_names)
            )
            self.check_last_added()
        return name_list

    def sync_existing_ro_to_list(self, reddit_object):
        if not reddit_object.significant:
            for key, value in self.list.get_default_dict().items():
                setattr(reddit_object, key, value)
            reddit_object.significant = True
            # [mine] fix(core): a user newly promoted from an incidental post-author row to
            # actually tracked isn't followed by the dedicated account yet -- see active's
            # definition in database/models.py
            if reddit_object.object_type == "USER":
                reddit_object.active = False
            reddit_object.save()

    def add_validated_reddit_object(self, ro_id):
        id_list = self.get_id_list(download_enabled=False)
        if ro_id not in id_list:
            reddit_object = self.session.query(RedditObject).get(ro_id)
            self.insertRow(reddit_object)
            self.sort_list()

    def add_complete_reddit_object(self, reddit_object):
        reddit_object, _created = self.db.get_or_create(
            type(reddit_object), session=self.session, name=reddit_object.name
        )
        if reddit_object.id not in self.get_id_list(download_enabled=False):
            self.insertRow(reddit_object)
            self.sort_list()

    def insertRow(self, item, parent=QModelIndex(), *args, **kwargs):
        if item is not None:
            self.beginInsertRows(parent, self.rowCount() - 1, self.rowCount())
            self.list.reddit_objects.append(item)
            self.endInsertRows()
            self.session.commit()
            self.reddit_object_added.emit(item.id)
            self.last_added = item
            self.send_count_change()
            return True
        return False

    def removeRows(self, position, rows, parent=QModelIndex(), *args):
        self.beginRemoveRows(parent, position, position - 1)
        for _x in range(rows):
            self.list.reddit_objects.remove(self.list.reddit_objects[position])
        self.endRemoveRows()
        self.session.commit()
        self.send_count_change()
        return True

    def removeRow(self, row, parent=QModelIndex(), *args):
        self.beginRemoveRows(parent, row, row)
        del self.list.reddit_objects[row]
        self.endRemoveRows()
        self.session.commit()
        self.send_count_change()
        return True

    def rowCount(self, parent=QModelIndex(), *args, **kwargs):
        try:
            return len(self.reddit_objects) if self.reddit_objects else 0
        except AttributeError:
            return 0

    def columnCount(self, parent=QModelIndex(), *args, **kwargs):
        return len(self.columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.column_headers[section]
        return None

    def send_count_change(self):
        """
        Emits the 'count_change' signal with the correct row count.
        """
        row_count = self.rowCount()
        self.count_change.emit(row_count)

    def _last_download_tooltip(self, reddit_object):
        absolute = reddit_object.get_display_datetime(
            self.last_download_cache.get(reddit_object.id)
        )
        if absolute is None:
            return None
        return f"{absolute}\nIncludes deleted content"

    def _format_datetime_cell(self, date_time, reddit_object, utc):
        if not self.settings_manager.relative_time_display:
            return reddit_object.get_display_datetime(date_time)
        if date_time is None:
            return None
        now = datetime.now(UTC).replace(tzinfo=None) if utc else datetime.now()
        return general_utils.format_relative_datetime(date_time, now)

    def data(self, index, role=Qt.DisplayRole):
        row = index.row()
        if index.isValid():
            try:
                if role == Qt.DisplayRole or role == Qt.EditRole:
                    field = self.columns[index.column()]
                    reddit_object = self.reddit_objects[row]
                    if field == "last_download":
                        return self._format_datetime_cell(
                            self.last_download_cache.get(reddit_object.id),
                            reddit_object,
                            utc=False,
                        )
                    if field == "date_last_download_utc":
                        return self._format_datetime_cell(
                            getattr(reddit_object, field), reddit_object, utc=True
                        )
                    if field == "expected_new":
                        return (
                            f"{self.expected_new_cache.get(reddit_object.id, 0.0):,.1f}"
                        )
                    if field == "date_added":
                        return self._format_datetime_cell(
                            reddit_object.date_added, reddit_object, utc=False
                        )
                    if field == "name":
                        return get_anonymizer().name(reddit_object)
                    return getattr(reddit_object, field)
                if role == Qt.ForegroundRole:
                    if (
                        not self.reddit_objects[row].download_enabled
                        and self.settings_manager.colorize_disabled_reddit_objects
                    ):
                        r, g, b = (
                            self.settings_manager.disabled_reddit_object_display_color
                        )
                        return QColor(r, g, b, 255)
                    if (
                        not self.reddit_objects[row].active
                        and self.settings_manager.colorize_inactive_reddit_objects
                    ):
                        r, g, b = (
                            self.settings_manager.inactive_reddit_object_display_color
                        )
                        return QColor(r, g, b, 255)
                    if (
                        self.reddit_objects[row].new
                        and self.settings_manager.colorize_new_reddit_objects
                    ):
                        r, g, b = self.settings_manager.new_reddit_object_display_color
                        return QColor(r, g, b, 255)
                    return None
                if role == Qt.ToolTipRole:
                    field = self.columns[index.column()]
                    reddit_object = self.reddit_objects[row]
                    if (
                        self.settings_manager.relative_time_display
                        and field == "last_download"
                    ):
                        return self._last_download_tooltip(reddit_object)
                    return self.set_tooltips(reddit_object)
                if role == Qt.UserRole:
                    return self.reddit_objects[row]
                return None
            except IndexError:
                pass
        return None

    def raw_data(self, row):
        try:
            return self.reddit_objects[row]
        except IndexError:
            return None

    def set_tooltips(self, reddit_object):
        """
        Builds the tooltip text based on what options are selected in the settings manager and returns the text.
        :param reddit_object: The reddit object the tooltip text is based off of.
        :type reddit_object: RedditObject
        :return: Text formatted to be displayed as a tooltip.
        :rtype: str
        """
        anonymizer = get_anonymizer()
        tooltip_dict = {
            "name": f"Name: {anonymizer.name(reddit_object)}",
            "download_enabled": f"Download Enabled: {reddit_object.download_enabled}",
            "last_download_date": f"Last Download: {reddit_object.last_download}",
            "download_naming_method": f"Name Downloads By: {reddit_object.post_download_naming_method}",
            "subreddit_save_method": f"Subreddit Save Method: {reddit_object.post_save_structure}",
            "download_images": f"Download Images: {reddit_object.download_images}",
            "download_videos": f"Download Videos: {reddit_object.download_videos}",
            "download_nsfw": f"NSFW Filter: {reddit_object.download_nsfw.display_name}",
            "date_added": f"Date Added: {reddit_object.date_added_display}",
            "total_score": f"Total Score: {reddit_object.total_score_display}",
            "post_count": f"Post Count: {reddit_object.post_count}",
            "content_count": f"Content Count: {reddit_object.content_count}",
            "comment_count": f"Comment Count: {reddit_object.comment_count}",
        }
        tooltip = ""
        for key, value in tooltip_dict.items():
            if self.settings_manager.main_window_tooltip_display_dict[key]:
                tooltip += f"{anonymizer.redact(value)}\n"
        return tooltip.strip()

    def nsfw_filter_display(self, filter_method):
        for key, value in self.settings_manager.nsfw_filter_dict.items():
            if value == filter_method:
                return key
        return None

    def flags(self, QModelIndex):
        return Qt.ItemIsSelectable | Qt.ItemIsEnabled

    def refresh(self):
        """
        Refreshes the displayed items in the list. This has to be called when the sort order is changed or the new
        sort order will not be displayed until the list is moved.
        """
        row_count = self.rowCount()
        if row_count == 0:
            return
        first = self.createIndex(0, 0)
        second = self.createIndex(row_count - 1, self.columnCount() - 1)
        self.dataChanged.emit(first, second)

    def refresh_session(self):
        try:
            list_id = self.list.id
            self.session.close()
            self.session = self.db.get_session()
            self.list = self.session.query(RedditObjectList).get(list_id)
            self.sort_list()
        except AttributeError:
            # AttributeError here indicates that the list model is not currently being used
            pass

    def close_session(self):
        name = self.list.name
        self.session.close()
        return name

    def open_session(self, list_name=None):
        self.session = self.db.get_session()
        if list_name is not None:
            self.set_list(list_name)


class ObjectValidator(QObject):
    new_object_signal = pyqtSignal(int)
    invalid_name_signal = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, name_list, list_type, list_defaults):
        super().__init__()
        self.name_list = name_list
        self.list_type = list_type
        self.list_defaults = list_defaults

    def run(self):
        object_creator = RedditObjectCreator(self.list_type)
        for name in self.name_list:
            creation_tuple = object_creator.create_reddit_object(
                name, self.list_defaults
            )
            if creation_tuple is not None:
                reddit_object_id, _created = creation_tuple
                self.new_object_signal.emit(reddit_object_id)
            else:
                self.invalid_name_signal.emit(name)
        self.finished.emit()
