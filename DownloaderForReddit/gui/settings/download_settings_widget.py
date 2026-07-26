from DownloaderForReddit.guiresources.settings.download_settings_widget_auto import Ui_DownloadSettingsWidget
from .abstract_settings_widget import AbstractSettingsWidget
from DownloaderForReddit.utils import injector
from DownloaderForReddit.database.models import RedditObjectList
from DownloaderForReddit.core.reddit_object_creator import RedditObjectCreator


class DownloadSettingsWidget(AbstractSettingsWidget, Ui_DownloadSettingsWidget):

    def __init__(self, **kwargs):
        super().__init__()
        self.db = injector.get_database_handler()
        self.session = self.db.get_session()
        self.main_window = kwargs.pop('main_window', None)
        self.kwargs = kwargs

        self.lists = []

        self.master_user_list = None
        self.master_subreddit_list = None
        self.make_master_lists()

        self.add_list(self.master_user_list)
        self.add_list(self.master_subreddit_list)

        self.list_combo_box.currentIndexChanged.connect(
            lambda idx: self.list_settings_widget.set_objects([self.lists[idx]])
        )
        self.list_settings_widget.set_objects([self.lists[0]])

    @property
    def description(self):
        return 'Sets the master default download settings used for every user/subreddit -- ' \
               'per-object customization is not supported; every user/subreddit uses these settings.'

    def make_master_lists(self):
        creator = RedditObjectCreator('USER')
        self.master_user_list = creator.create_reddit_object_list('Master User List', commit=False)
        creator.list_type = 'SUBREDDIT'
        self.master_subreddit_list = creator.create_reddit_object_list('Master Subreddit List', commit=False)

    def add_list(self, ro_list: RedditObjectList):
        name = f'{ro_list.name}  [MASTER_{ro_list.list_type}]'
        self.lists.append(ro_list)
        self.list_combo_box.addItem(name)

    def load_settings(self):
        self.list_combo_box.setCurrentIndex(0)

    def apply_settings(self):
        self.set_from_master()
        self.session.commit()
        self.main_window.refresh_list_models()

    def set_from_master(self):
        self.settings.user_download_defaults = self.master_user_list.get_default_dict()
        self.settings.subreddit_download_defaults = self.master_subreddit_list.get_default_dict()

    def close(self):
        self.session.close()
        super().close()
