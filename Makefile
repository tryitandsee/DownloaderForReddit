DB='${HOME}/Library/Application Support/SomeGuySoftware/DownloaderForReddit/dfr.db'

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy .

test:
	uv run pytest

install: ## Install requirements
	uv sync --upgrade --all-groups

define UNUSED_SQL
SELECT name FROM (
SELECT name,
(SELECT COUNT(*) FROM post WHERE post.author_id = reddit_object.id) AS count
FROM reddit_object
WHERE object_type = 'USER' AND count = 0
LIMIT 20
)
endef
export UNUSED_SQL
db/unused: ## Find users that were added but have never posted
	# @echo "$$UNUSED_SQL"
	sqlite3 $(DB) "$$UNUSED_SQL"
