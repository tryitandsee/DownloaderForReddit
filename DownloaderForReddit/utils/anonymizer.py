# [mine] feat(gui): screenshot mode -- display-layer anonymization of names and paths
import os
import re

from ..database.models import RedditObject
from . import injector

_ALIAS_PREFIXES = {"USER": "user", "SUBREDDIT": "sub"}
_UNKNOWN_ALIASES = {"user": "user_?", "r": "sub_?"}

# A name may end in a hyphen, which \b treats as a boundary.
_NAME_EDGE_BEFORE = r"(?<![A-Za-z0-9_-])"
_NAME_EDGE_AFTER = r"(?![A-Za-z0-9_-])"

# Matches owners by position, covering names that were never promoted to a tracked object -- the
# common case in the content feed.
_URL_OWNER_RE = re.compile(r"/(r|user)/([A-Za-z0-9_-]+)", re.IGNORECASE)

# Every surface displaying these parses its text as HTML, where an angle-bracket placeholder is an
# unknown tag and renders as nothing.
_ROOT_PLACEHOLDER = "[downloads]"
_HOME_PLACEHOLDER = "[home]"

# Output lines and feed entries arrive as HTML whose anchor text is the path or permalink itself
# (see downloader.get_downloaded_output_data). Only the visible text may be redacted -- rewriting
# the href would break opening the file or post the link exists to open.
_HREF_RE = re.compile(r'href="[^"]*"')
# The 'h' shields the index digits from the name alternation's lookarounds, which a tracked name of
# "12" would otherwise match inside a stash marker.
_STASH_RE = re.compile("\x00h(\\d+)\x00")


def alias_for(object_type: str, object_id: int) -> str:
    return f"{_ALIAS_PREFIXES.get(object_type, 'obj')}_{object_id}"


def _path_pattern(path: str) -> str:
    """Paths reach the display layer with either separator: settings store '/', the downloader
    reports what the OS produced."""
    return r"[\\/]".join(re.escape(part) for part in re.split(r"[\\/]", path) if part)


class Anonymizer:
    def __init__(self):
        self.enabled = False
        self._aliases: dict[str, str] = {}
        self._name_re: re.Pattern | None = None
        self._path_res: list[tuple[re.Pattern, str]] = []

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if enabled:
            self.rebuild()

    def rebuild(self) -> None:
        """Only 'significant' objects: RedditObject also holds an incidental row for every post
        author ever seen, which would put thousands of names into one alternation."""
        with injector.get_database_handler().get_scoped_session() as session:
            rows = (
                session.query(
                    RedditObject.id, RedditObject.name, RedditObject.object_type
                )
                .filter(RedditObject.significant.is_(True))
                .all()
            )
        self._aliases = {
            name.lower(): alias_for(object_type, object_id)
            for object_id, name, object_type in rows
            if name
        }
        names = sorted(self._aliases, key=len, reverse=True)
        self._name_re = (
            re.compile(
                _NAME_EDGE_BEFORE
                + f"(?:{'|'.join(re.escape(name) for name in names)})"
                + _NAME_EDGE_AFTER,
                re.IGNORECASE,
            )
            if names
            else None
        )
        self._path_res = self._build_path_patterns()

    def _build_path_patterns(self) -> list[tuple[re.Pattern, str]]:
        settings = injector.get_settings_manager()
        roots = {
            settings.user_save_directory: _ROOT_PLACEHOLDER,
            settings.subreddit_save_directory: _ROOT_PLACEHOLDER,
            os.path.expanduser("~"): _HOME_PLACEHOLDER,
        }
        # Longest first so a home directory that contains a save directory (or the reverse) can't
        # be half-replaced by the shorter one.
        return [
            (re.compile(_path_pattern(root), re.IGNORECASE), placeholder)
            for root, placeholder in sorted(
                roots.items(), key=lambda item: len(item[0]), reverse=True
            )
            if root
        ]

    def alias(self, object_type: str, object_id: int) -> str:
        return alias_for(object_type, object_id)

    def name(self, reddit_object) -> str:
        if not self.enabled:
            return reddit_object.name
        return alias_for(reddit_object.object_type, reddit_object.id)

    def redact(self, text: str | None) -> str | None:
        if not self.enabled or not text:
            return text
        hrefs: list[str] = []

        def stash(match: re.Match) -> str:
            hrefs.append(match.group(0))
            return f"\x00h{len(hrefs) - 1}\x00"

        text = _HREF_RE.sub(stash, text)
        for pattern, placeholder in self._path_res:
            text = pattern.sub(placeholder, text)
        text = _URL_OWNER_RE.sub(self._redact_url_owner, text)
        if self._name_re is not None:
            text = self._name_re.sub(
                lambda match: self._aliases[match.group(0).lower()], text
            )
        return _STASH_RE.sub(lambda match: hrefs[int(match.group(1))], text)

    def _redact_url_owner(self, match: re.Match) -> str:
        kind, name = match.group(1), match.group(2)
        alias = self._aliases.get(name.lower()) or _UNKNOWN_ALIASES[kind.lower()]
        return f"/{kind}/{alias}"


_anonymizer = Anonymizer()


def get_anonymizer() -> Anonymizer:
    return _anonymizer
