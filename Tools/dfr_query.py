"""Read-only diagnostics against the live Downloader For Reddit database and log.

Deliberately does not import DownloaderForReddit. DatabaseHandler.__init__ calls
metadata.create_all(), so importing the database layer writes to the database being
inspected, and local_logging.logger attaches handlers on import.

    python Tools/dfr_query.py object <name>
    python Tools/dfr_query.py objects --order-by expected
    python Tools/dfr_query.py log --level ERROR --since 2h
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta

VELOCITY_WINDOW_DAYS = 30
VELOCITY_WINDOW_LAG_DAYS = 7
DEFAULT_ROW_CAP = 40
DEFAULT_LOG_LIMIT = 20
TRACEBACK_LINES = 12
EXTRA_CHARS = 240

# The schema mixes two clock conventions and stores both naive. Every datetime leaving
# this script is resolved through here, so a column that is not classified raises rather
# than silently getting compared against the wrong clock.
COLUMN_CONVENTION = {
    "date_posted": "utc",
    "date_last_download_utc": "utc",
    "extraction_date": "local",
    "download_date": "local",
    "inactive_date": "local",
    "date_created": "local",
    "start_time": "local",
    "end_time": "local",
}

# date_added is absent on purpose: Column(DateTime, default=datetime.now()) evaluates the
# default once at import, so every row that took it shares one meaningless timestamp.

LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
LOG_RECORD_KEYS = frozenset(
    {
        "levelname",
        "asctime",
        "filename",
        "module",
        "name",
        "funcName",
        "lineno",
        "message",
        "exc_info",
    }
)
LOG_BLIND_SPOTS = (
    "no matches. Note this file cannot see: send_debug calls (the file handler is "
    "INFO-level while the logger is DEBUG), or Message.send_info text (that goes to the "
    "GUI output pane, not the log). Absence here is not evidence the code did not run."
)


def data_directory() -> str:
    subpath = os.path.join("SomeGuySoftware", "DownloaderForReddit")
    if sys.platform == "win32":
        return os.path.join(os.environ["APPDATA"], subpath)
    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~"), "Library", "Application Support", subpath
        )
    return os.path.join(os.path.expanduser("~"), f".{subpath}")


def database_path() -> str:
    return os.path.join(data_directory(), "dfr.db")


def log_path() -> str:
    return os.path.join(data_directory(), "DownloaderForReddit.log")


def connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def parse_stored(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def iso(column: str, value: str | None) -> str | None:
    """Render a stored naive datetime as an offset-explicit string."""
    moment = parse_stored(value)
    if moment is None:
        return None
    if COLUMN_CONVENTION[column] == "utc":
        return moment.replace(tzinfo=UTC).isoformat()
    return moment.astimezone().isoformat()


def elapsed_days(
    date_last_download_utc: str | None, extraction_date: str | None
) -> float | None:
    """Days since coverage was last confirmed, by whichever signal is more recent.

    Each stored value is measured against its own clock and only the resulting durations
    are compared, since the two columns use different conventions.
    """
    deltas = []
    last_download = parse_stored(date_last_download_utc)
    if last_download is not None:
        deltas.append(datetime.now(UTC).replace(tzinfo=None) - last_download)
    last_extraction = parse_stored(extraction_date)
    if last_extraction is not None:
        deltas.append(datetime.now() - last_extraction)
    if not deltas:
        return None
    return max(0.0, min(deltas).total_seconds() / 86_400)


def expected_new(window_posts: int, elapsed: float | None) -> float:
    if elapsed is None:
        return 0.0
    rate = (window_posts + 1) / (VELOCITY_WINDOW_DAYS + 1)
    return rate * min(elapsed, VELOCITY_WINDOW_DAYS)


def velocity_window() -> tuple[str, str]:
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    end = now_utc - timedelta(days=VELOCITY_WINDOW_LAG_DAYS)
    start = end - timedelta(days=VELOCITY_WINDOW_DAYS)
    return start.isoformat(sep=" "), end.isoformat(sep=" ")


def emit(payload: object) -> None:
    print(json.dumps(payload, indent=2, default=str))


OBJECT_SQL = """
-- absolute_date_limit and date_limit are omitted: their default is a local-clock
-- constant while written values track UTC post dates, so neither convention is safe to
-- state, and the browser pipeline no longer reads either one.
SELECT ro.id, ro.name, ro.object_type, ro.date_created, ro.date_last_download_utc,
       ro.significant, ro.download_enabled, ro.active, ro.new, ro.inactive_date,
       ro.post_limit
FROM reddit_object ro
WHERE ro.name = ? COLLATE NOCASE
"""

POST_STATS_SQL = """
SELECT COUNT(*) AS total_posts,
       MAX(p.date_posted) AS date_posted,
       MAX(p.extraction_date) AS extraction_date,
       SUM(CASE WHEN p.date_posted >= ? AND p.date_posted < ? THEN 1 ELSE 0 END)
           AS window_posts
FROM post p
WHERE p.significant_reddit_object_id = ?
"""

LAST_DOWNLOAD_SQL = """
SELECT MAX(c.download_date) AS download_date
FROM content c JOIN post p ON p.id = c.post_id
WHERE p.significant_reddit_object_id = ?
"""


def describe_object(connection: sqlite3.Connection, name: str, object_type: str | None):
    """Describe every row with this name.

    `name` is not unique: reddit's user and subreddit namespaces are independent, and
    untracked subreddit rows created during scraping can repeat. Returning one row would
    mean silently answering about a different object than the one asked about.
    """
    sql = OBJECT_SQL
    params: list[object] = [name]
    if object_type is not None:
        sql += " AND ro.object_type = ?"
        params.append(object_type)
    rows = connection.execute(sql, params).fetchall()
    return {
        "name": name,
        "matched": len(rows),
        "objects": [describe_row(connection, row) for row in rows],
    }


def describe_row(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    window_start, window_end = velocity_window()
    stats = connection.execute(
        POST_STATS_SQL, (window_start, window_end, row["id"])
    ).fetchone()
    last_download = connection.execute(LAST_DOWNLOAD_SQL, (row["id"],)).fetchone()

    window_posts = stats["window_posts"] or 0
    elapsed = elapsed_days(row["date_last_download_utc"], stats["extraction_date"])
    return {
        "name": row["name"],
        "id": row["id"],
        "object_type": row["object_type"],
        "flags": {
            "significant": bool(row["significant"]),
            "download_enabled": bool(row["download_enabled"]),
            "active": bool(row["active"]),
            "new": bool(row["new"]),
            "post_limit": row["post_limit"],
        },
        "posts": {
            "total": stats["total_posts"] or 0,
            "in_velocity_window": window_posts,
            "velocity_window": {
                "days": VELOCITY_WINDOW_DAYS,
                "lag_days": VELOCITY_WINDOW_LAG_DAYS,
                "start_utc": window_start,
                "end_utc": window_end,
            },
            "newest_date_posted": iso("date_posted", stats["date_posted"]),
            "last_extraction": iso("extraction_date", stats["extraction_date"]),
        },
        "checkpoints": {
            "date_last_download_utc": iso(
                "date_last_download_utc", row["date_last_download_utc"]
            ),
            "last_content_download": iso(
                "download_date", last_download["download_date"]
            ),
            "elapsed_days_since_coverage": elapsed,
        },
        "expected_new": expected_new(window_posts, elapsed),
        "dates": {
            "date_created": iso("date_created", row["date_created"]),
            "inactive_date": iso("inactive_date", row["inactive_date"]),
        },
    }


LIST_SQL = """
SELECT ro.id, ro.name, ro.object_type, ro.date_last_download_utc,
       ro.significant, ro.download_enabled,
       (SELECT COUNT(*) FROM post p
         WHERE p.significant_reddit_object_id = ro.id) AS total_posts,
       (SELECT COUNT(*) FROM post p
         WHERE p.significant_reddit_object_id = ro.id
           AND p.date_posted >= ? AND p.date_posted < ?) AS window_posts,
       (SELECT MAX(p.extraction_date) FROM post p
         WHERE p.significant_reddit_object_id = ro.id) AS extraction_date
FROM reddit_object ro
"""

SORT_KEYS = {
    "expected": lambda row: -row["expected_new"],
    "name": lambda row: row["name"].lower(),
    "window_posts": lambda row: -row["window_posts"],
    "total_posts": lambda row: -row["total_posts"],
    "elapsed": lambda row: -(row["elapsed_days_since_coverage"] or 0.0),
}


def list_objects(
    connection: sqlite3.Connection,
    order_by: str,
    limit: int,
    object_type: str | None,
    all_objects: bool,
) -> dict:
    window_start, window_end = velocity_window()
    sql = LIST_SQL
    params: list[object] = [window_start, window_end]
    clauses = []
    if not all_objects:
        clauses.append("ro.significant = 1")
    if object_type is not None:
        clauses.append("ro.object_type = ?")
        params.append(object_type)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)

    rows = []
    for record in connection.execute(sql, params):
        elapsed = elapsed_days(
            record["date_last_download_utc"], record["extraction_date"]
        )
        window_posts = record["window_posts"] or 0
        rows.append(
            {
                "name": record["name"],
                "object_type": record["object_type"],
                "download_enabled": bool(record["download_enabled"]),
                "total_posts": record["total_posts"] or 0,
                "window_posts": window_posts,
                "elapsed_days_since_coverage": elapsed,
                "date_last_download_utc": iso(
                    "date_last_download_utc", record["date_last_download_utc"]
                ),
                "last_extraction": iso("extraction_date", record["extraction_date"]),
                "expected_new": expected_new(window_posts, elapsed),
            }
        )

    rows.sort(key=SORT_KEYS[order_by])
    return {
        "matched": len(rows),
        "shown": min(len(rows), limit),
        "truncated": len(rows) > limit,
        "scope": "all objects" if all_objects else "significant (tracked)",
        "objects": rows[:limit],
    }


COVERAGE_SQL = """
SELECT COUNT(*) AS objects,
       SUM(CASE WHEN ro.download_enabled = 0 THEN 1 ELSE 0 END) AS download_disabled,
       SUM(CASE WHEN ro.date_last_download_utc IS NOT NULL THEN 1 ELSE 0 END)
           AS with_checkpoint,
       SUM(CASE WHEN EXISTS (SELECT 1 FROM post p
                              WHERE p.significant_reddit_object_id = ro.id)
                THEN 1 ELSE 0 END) AS with_posts,
       SUM(CASE WHEN ro.date_last_download_utc IS NULL
                     AND NOT EXISTS (SELECT 1 FROM post p
                                      WHERE p.significant_reddit_object_id = ro.id)
                THEN 1 ELSE 0 END) AS with_neither
FROM reddit_object ro
"""


def coverage_stats(connection: sqlite3.Connection, all_objects: bool) -> dict:
    sql = COVERAGE_SQL
    if not all_objects:
        sql += " WHERE ro.significant = 1"
    row = connection.execute(sql).fetchone()
    return {
        "scope": "all objects" if all_objects else "significant (tracked)",
        "objects": row["objects"],
        "download_disabled": row["download_disabled"] or 0,
        "with_date_last_download_utc": row["with_checkpoint"] or 0,
        "with_any_post": row["with_posts"] or 0,
        "with_neither": row["with_neither"] or 0,
    }


def parse_since(value: str) -> datetime:
    units = {"m": "minutes", "h": "hours", "d": "days"}
    if value and value[-1] in units and value[:-1].replace(".", "", 1).isdigit():
        return datetime.now() - timedelta(**{units[value[-1]]: float(value[:-1])})
    return datetime.fromisoformat(value)


def log_files(path: str, rotated: bool) -> list[str]:
    if not rotated:
        return [path]
    candidates = [f"{path}.2", f"{path}.1", path]
    return [candidate for candidate in candidates if os.path.exists(candidate)]


def format_record(record: dict, traceback: bool) -> str:
    line = (
        f"{record.get('asctime')} {record.get('levelname')} "
        f"{record.get('funcName')}: {record.get('message')}"
    )
    extras = {key: value for key, value in record.items() if key not in LOG_RECORD_KEYS}
    if extras:
        rendered = json.dumps(extras, default=str)
        if len(rendered) > EXTRA_CHARS:
            rendered = f"{rendered[:EXTRA_CHARS]}...(truncated)"
        line += f" {rendered}"
    if traceback and record.get("exc_info"):
        lines = record["exc_info"].splitlines()
        shown = lines[-TRACEBACK_LINES:]
        if len(lines) > TRACEBACK_LINES:
            shown.insert(0, f"    ...({len(lines) - TRACEBACK_LINES} earlier lines)")
        line += "\n" + "\n".join(f"    {entry}" for entry in shown)
    return line


def tail_log(
    path: str,
    level: str | None,
    since: str | None,
    contains: str | None,
    func_name: str | None,
    limit: int,
    traceback: bool,
    rotated: bool,
) -> tuple[list[str], int]:
    threshold = LEVEL_ORDER[level] if level else 0
    cutoff = parse_since(since) if since else None
    matches = []
    for file_path in log_files(path, rotated):
        with open(file_path, encoding="utf-8", errors="replace") as log_file:
            for line in log_file:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if LEVEL_ORDER.get(record.get("levelname"), 0) < threshold:
                    continue
                if cutoff is not None:
                    stamped = record.get("asctime")
                    if stamped is None or datetime.fromisoformat(stamped) < cutoff:
                        continue
                if contains and contains.lower() not in line.lower():
                    continue
                if func_name and record.get("funcName") != func_name:
                    continue
                matches.append(record)
    tail = matches[-limit:]
    return [format_record(record, traceback) for record in tail], len(matches)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None, help="override the database path")
    parser.add_argument("--log", default=None, help="override the log path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    single = subparsers.add_parser("object", help="everything about one object")
    single.add_argument("name")
    single.add_argument("--type", dest="object_type", choices=["USER", "SUBREDDIT"])

    listing = subparsers.add_parser("objects", help="tracked objects, sorted")
    listing.add_argument("--order-by", choices=sorted(SORT_KEYS), default="expected")
    listing.add_argument("--limit", type=int, default=DEFAULT_ROW_CAP)
    listing.add_argument("--type", dest="object_type", choices=["USER", "SUBREDDIT"])
    listing.add_argument("--all", action="store_true", help="include untracked objects")

    coverage = subparsers.add_parser("coverage", help="checkpoint and post coverage")
    coverage.add_argument("--all", action="store_true")

    log = subparsers.add_parser("log", help="filtered log records")
    log.add_argument("--level", choices=sorted(LEVEL_ORDER), default=None)
    log.add_argument("--since", default=None, help="30m, 2h, 7d, or an ISO timestamp")
    log.add_argument("--contains", default=None)
    log.add_argument("--func", dest="func_name", default=None)
    log.add_argument("--limit", type=int, default=DEFAULT_LOG_LIMIT)
    log.add_argument("--traceback", action="store_true", help="include exc_info")
    log.add_argument("--rotated", action="store_true", help="also read .log.1/.log.2")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.command == "log":
        lines, matched = tail_log(
            args.log or log_path(),
            args.level,
            args.since,
            args.contains,
            args.func_name,
            args.limit,
            args.traceback,
            args.rotated,
        )
        if not matched:
            print(LOG_BLIND_SPOTS)
            return 0
        print("\n".join(lines))
        if matched > len(lines):
            print(f"\n({matched} matched, showing last {len(lines)})")
        return 0

    connection = connect(args.db or database_path())
    try:
        if args.command == "object":
            emit(describe_object(connection, args.name, args.object_type))
        elif args.command == "objects":
            emit(
                list_objects(
                    connection,
                    args.order_by,
                    args.limit,
                    args.object_type,
                    args.all,
                )
            )
        else:
            emit(coverage_stats(connection, args.all))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
