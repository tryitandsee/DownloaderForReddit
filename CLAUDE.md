# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## History

This started as a personal fork of an active upstream project, but no longer tracks or rebases onto it — `main` is the trunk with ordinary, non-squashed commit history.

Do not commit untracked files, idiot

Very short commit messages! The only one reading these is a future LLM. Only give the absolute bare minimum of clues.

Default to ONE commit for a session's work. Only split into multiple commits when explicitly told to.

One commit per session. Do not split unless told to split.

If you're about to make a second commit for this session's work and nobody asked for a split, stop — you're wrong.

The number of commits for a session's work is one, unless the user explicitly says otherwise.

Different concern, different layer, different risk level, a bug found mid-task — none of these are a reason for a second commit. Only an explicit instruction to split is.

Squash it. All of it. One commit.

When logging or displaying a submission/content item, order fields as: user/subreddit, reddit_id, url.

## Diagnostics

Before hand-writing a query against the live database or grepping the log, use
`Tools/dfr_query.py` (`--help` for subcommands). It reads the database read-only, is safe
while the app is running, and handles the schema's mixed UTC/local datetime conventions.
Grepping `DownloaderForReddit.log` directly returns whole embedded tracebacks and will
blow the read cap; `dfr_query.py log` filters before including them.

## Lint

mypy must pass too, not just ruff. No `type: ignore`.

## Architecture

@docs/ARCHITECTURE.md

Every time you make a major architectural change, make sure to update that file.