DB='${HOME}/Library/Application Support/SomeGuySoftware/DownloaderForReddit/dfr.db'

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy .

tdd:
	nodemon -e py -x "python -W ignore::DeprecationWarning -m unittest --failfast"

test:
	python -m unittest

install: ## Install requirements
	@[ -n "${VIRTUAL_ENV}" ] || (echo "ERROR: This should be run from a virtualenv" && exit 1)
	pip install -r requirements.txt

.PHONY: requirements.txt
requirements.txt: ## Regenerate requirements.txt
requirements.txt: requirements.in
	pip-compile $< > $@

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
