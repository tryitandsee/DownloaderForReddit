# Debugging cheatsheet: querying the DFR sqlite db and log directly

No `sqlite3` CLI on this machine -- use the venv's Python (`sqlite3` is stdlib) instead.

## Locations

```
DB:  %APPDATA%\SomeGuySoftware\DownloaderForReddit\dfr.db
LOG: %APPDATA%\SomeGuySoftware\DownloaderForReddit\DownloaderForReddit.log
```

In Git Bash: `$USERPROFILE/AppData/Roaming/SomeGuySoftware/DownloaderForReddit/`

## Running a query

```bash
"P:\Sync\dfr\.venv\Scripts\python.exe" -c "
import sqlite3, os
conn = sqlite3.connect(os.path.join(os.environ['APPDATA'], 'SomeGuySoftware', 'DownloaderForReddit', 'dfr.db'))
cur = conn.cursor()
cur.execute('SELECT ...')
print(cur.fetchall())
"
```

## Useful queries

**Find a post by reddit_id** (the shreddit `id` attribute, no `t3_` prefix):
```sql
SELECT id, reddit_id, title, url, date_posted, extracted, extraction_error
FROM post WHERE reddit_id = 'xxxxxxx';
```

**Content/download status for a post:**
```sql
SELECT c.id, c.url, c.downloaded, c.download_error, c.md5_hash
FROM content c WHERE c.post_id = <post.id>;
```

**Find a tracked reddit_object by name** (case-insensitive; watch for duplicate rows -- see
PLAN_db_cruft.md):
```sql
SELECT id, name, object_type, significant, active FROM reddit_object
WHERE name = 'SomeUser' COLLATE NOCASE;
```

**Full settings row for a reddit_object** (date_limit/absolute_date_limit, nsfw filter,
avoid_duplicates, post_limit, etc. -- get column names first since there are ~45 of them):
```python
cur.execute('PRAGMA table_info(reddit_object)')
cols = [c[1] for c in cur.fetchall()]
cur.execute('SELECT * FROM reddit_object WHERE id = <id>')
print(dict(zip(cols, cur.fetchone())))
```

**Most recent known posts for a reddit_object** (sanity-check what the known-post scroll-stop
in reddit_source.py would have seen):
```sql
SELECT reddit_id, title, date_posted FROM post
WHERE significant_reddit_object_id = <id> ORDER BY date_posted DESC LIMIT 20;
```

**Check for a duplicate-content match** (filter_content's dedup check is against the
*resolved media URL* passed to `make_content()`, not `Post.url` -- the two can differ, e.g.
for redgifs/imgur links that get resolved to a CDN URL before the dedup check runs):
```sql
SELECT id, post_id, url, downloaded FROM content WHERE url = '<resolved media url>';
```

**Post count per tracked user** (mirrors the Makefile's old `db/unused` target, for finding
users added but never posted):
```sql
SELECT ro.name, COUNT(p.id) AS post_count
FROM reddit_object ro LEFT JOIN post p ON p.significant_reddit_object_id = ro.id
WHERE ro.object_type = 'USER' AND ro.significant = 1
GROUP BY ro.id ORDER BY post_count ASC LIMIT 20;
```

## Reading the log

The log is JSON-per-record (one `{...}` block per line group, not JSONL -- each record spans
multiple lines). Grep for a timestamp prefix or a name/message to jump to the right spot, then
`Read` the file at that line offset for full context:

```bash
grep -n "07/25/2026 06:1" DownloaderForReddit.log   # narrow to a time window
grep -n "SomeUsername" DownloaderForReddit.log      # narrow to a reddit_object
```

`finish_messages` log entries (search `"message": "Download complete"`) summarize a whole
download session -- `post_extraction_count`/`download_count` of 0 across an entire run is the
fastest signal something upstream (filtering, validation, scroll) is silently eating everything,
before diving into per-post detail.
