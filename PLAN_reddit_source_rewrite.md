# Plan: Replace PRAW with a browser-automation Reddit source

## Why

The shared hardcoded `CLIENT_ID` (`frGEUVAuHGL2PQ`, [reddit_utils.py:45](DownloaderForReddit/utils/reddit_utils.py#L45)) has been disabled by Reddit. Symptom: `401` inside `Authorizer.refresh()` for *some* requests while others succeed — the long-lived instance in [download_runner.py:52](DownloaderForReddit/core/download_runner.py#L52) rides a cached access token; the per-call fresh instances the extractors mint ([content_runner.py:120](DownloaderForReddit/core/content_runner.py#L120), [reddit_uploads_extractor.py:47](DownloaderForReddit/extractors/reddit_uploads_extractor.py#L47)) must re-exchange the refresh token and hit the dead client.

Registering a replacement app is not viable: Reddit now routes all app registrations through manual review. There is no client-side fix for a disabled client id.

## Decision: stop being a fork

Replacing PRAW abandons the one thing upstream exists to do. The "small clean changes, squashed `# [mine]` commit, rebase onto new releases" model in [CLAUDE.md](CLAUDE.md) cannot survive this — you cannot rebase a browser-automation rewrite onto a PRAW codebase.

- Reuse this repo (keeps ~80% of the value — see below); do **not** start a new repo.
- `main` becomes the trunk; stop rebasing onto upstream.
- Rewrite [CLAUDE.md](CLAUDE.md): drop the fork/rebase/squash conventions and `# [mine]` tagging.

### What is kept (transport-agnostic, downstream of discovery)

- DB models, dedup, download-session tracking ([database/models.py](DownloaderForReddit/database/models.py))
- Threaded downloader + `ThreadPoolExecutor`, file-naming, folder structure ([core/download/downloader.py](DownloaderForReddit/core/download/downloader.py))
- Media extractors (i.redd.it, v.redd.it, redgifs, imgur) — they consume URLs, not PRAW objects
- PyQt GUI, scheduler, settings, filters

### What is replaced

The **discovery/auth seam only**: `reddit_utils` + object validation + everywhere a live PRAW object is read.

### Tracked-list source of truth (phased)

- **Now:** keep the existing `reddit_object` model as the tracked list; the reconcile step pushes it to the dedicated account's follows / `/m/dfr`.
- **Eventually:** make the dedicated account's **follows + multireddit membership the source of truth** and drop the `reddit_object` list. This loses per-`reddit_object` config (post limits, per-user filters) — acceptable, that config is unused. Adding a target becomes "follow them in the browser."

## Transport decision

Evaluated tiers, worst→best on detection:

1. `requests` + `.json` + browser cookies — cookies prove *who*, but urllib3/OpenSSL emit a fixed JA3/JA4 + HTTP/2 fingerprint + header order no browser produces. Detectable.
2. `curl_cffi` (`impersonate='chrome'`) + cookies — forges TLS/HTTP2 fingerprint. Lightweight, keeps threading. **Still loses to a JS/Turnstile challenge**, and calling `.json` at all is a request pattern the real site never emits.
3. **Browser automation (chosen).** Real fingerprint, runs JS, clears challenges by construction.

### Critical constraint: `.json` is a tell

The modern frontend (shreddit) does **not** call `.json`. Injecting `fetch('/user/x/submitted.json')` from inside a real browser is anomalous traffic — right fingerprint, wrong request pattern. Never do this.

**Surface is shreddit (new reddit), not old.reddit — decided.** `old.reddit.com` renders server-side HTML with none of shreddit's structured per-post attributes below; driving it means brittle HTML scraping, rejected. The whole approach is committed to shreddit.

### Actual mechanism (confirmed by probe, supersedes the original interception plan)

Passive `svc`/`gql` network interception was the original plan but turned out to be unnecessary and mostly a dead end — those calls are telemetry/experiment-flag/token noise, not post data (confirmed empirically: captured payloads were `ExposeVariant`, `CreateCaptchaToken`, trophy-case info, never a listing). The real mechanism is simpler:

**Posts are server-rendered directly into the DOM as `<shreddit-post>` custom elements, with post data on the element's own attributes** — no JSON payload to intercept at all:

```html
<shreddit-post permalink="/r/pics/comments/1v5o0fq/oc_the_iron_giant/"
                content-href="https://i.redd.it/2x34qbhfs8fh1.jpeg"
                id="t3_1v5o0fq" post-type="image" domain="i.redd.it"
                author="Maj_Gen_Error" subreddit-prefixed-name="r/pics"
                created-timestamp="2026-07-24T21:04:21.481000+0000"
                post-title="[OC] The Iron Giant..." score="6" comment-count="1" ...>
```

This holds for the initial page load and for posts appended on scroll (same element type, same attributes) — so the source just re-queries `page.locator('shreddit-post').all()` after each scroll and reads attributes via `get_attribute()`. No network capture, no JSON parsing, no persisted-query-hash fragility. This is *more* robust than the original interception plan, since public custom-element attributes are a more stable contract than internal GraphQL operation names.

**Sort matters.** The default/first-visit sort (`BEST`, a personalized ranking) returned a literal "There is no content to display" empty state for an account that had already browsed its own follows — `BEST` is exhausted by prior viewing, not a feed bug. Driving `/new/` (`https://www.reddit.com/new/`, confirmed via the `<shreddit-feed reload-url="...?sort=NEW">` attribute) returns real content. **The source must drive `new`, not the default sort.**

Accepted cost (already conceded): feeds are recent-only and chronologically capped (~1000, a Reddit-wide cap independent of method). **Deep pagination is gone.**

## Aggregation strategy: two feeds, not N navigations

A human scrolls their home feed; nobody visits 400 profiles in sequence. Exploit Reddit's own aggregated feeds:

1. **Tracked users → the home feed (following-only).** On the dedicated account, **follow only** the tracked users and disable *Show recommendations in home feed* in settings — the home feed then *is* the pure aggregated tracked-user feed (confirmed: recommendations can be turned off and the feed stays clean). One scroll session yields recent posts from all of them; route each captured post to its DFR user-config **by author**. This avoids the deprecated/flaky `r/friends` entirely.
2. **Tracked subreddits → a custom multireddit (`/m/dfr`).** The subreddit analog: one scroll session, route **by subreddit**.

Steady-state run = two aggregated scroll sessions. **Backfill of a newly-added target still needs a one-off per-target visit** — keep a per-profile discovery path for that. The same per-profile path (`/user/<name>/submitted/?sort=new`, confirmed working in Phase 1) doubles as the recovery mechanism for the home feed's confirmed completeness gap: more frequent polling narrows each run's window (fewer posts to potentially miss per cycle), and a per-target backfill sweep can be run periodically or on-demand to catch anything the aggregated feed dropped. Not required for MVP — the gap is accepted for now — but this is the path to close it later without redesigning discovery.

### Dedicated downloader account (required)

Following users pollutes the home feed of whatever account does it (confirmed in practice — it ruined the personal feed). That pollution is exactly what we want *if* the feed contains nothing else, so the follows and `/m/dfr` multireddit live on a **dedicated Reddit account** that follows only tracked users. The personal account is never touched; the reconcile step mutates the dedicated account's state freely.

- Playwright drives a **separate persistent browser profile** logged into the dedicated account.
- The dedicated account must follow **only** tracked users — any stray follow/subscription contaminates the home feed and breaks the by-author routing assumption.
- Prefer an **aged** or existing secondary account, not a brand-new one — fresh accounts draw tighter rate limits, more challenges, and bulk-following can get a new account actioned. Pace the follow sync slowly.
- Low blast radius: the tracked-list lives in the DFR DB, so a lost/banned account is recoverable by re-running the reconcile.
- Verify any following-count cap during Phase 0; shard across multiple dedicated accounts if tracked users exceed it.

## The seam: `RedditSource` interface

Everything hinges on one boundary so the downstream stack never knows the transport. Downstream currently reads live PRAW objects; the source must return plain data (a shim/dataclass exposing the attribute names extractors already use: `.url`, `.is_self`, `.selftext`, `.author`, `.subreddit`, `.created`, `.id`, `.title`, `.media`, ...), populated from `<shreddit-post>` element attributes (`content-href`, `permalink`, `post-type`, `id`, `author`, `subreddit-prefixed-name`, `created-timestamp`, `post-title`, `domain`, ...).

```python
class RedditSource(Protocol):
    def validate_user(self, name: str) -> ValidationResult: ...
    def validate_subreddit(self, name: str) -> ValidationResult: ...
    def iter_user_submissions(self, name: str) -> Iterable[SubmissionData]: ...
    def iter_subreddit_submissions(self, name: str) -> Iterable[SubmissionData]: ...
    def iter_home_feed(self) -> Iterable[SubmissionData]: ...  # following-only aggregation
    def iter_multireddit(self, owner: str, name: str) -> Iterable[SubmissionData]: ...
```

`BrowserRedditSource` is the first (only) implementation ([core/reddit_source.py](DownloaderForReddit/core/reddit_source.py)). PRAW code deleted, not kept behind the interface.

## Phases

### Phase 0 — Probe (done — see Tools/probe_all_traffic.py, Tools/open_browser_profile.py)

Ran against the dedicated account, driving `/new/` and subreddit `/new/` feeds directly with Playwright, reading `<shreddit-post>` element attributes from `document.html` (network interception was tried first and found to be a dead end — see above).

**Gate A — per-content-type, does the feed card carry a real source URL, not just a preview?**

| Type | `content-href` | Verdict |
|---|---|---|
| image | full `i.redd.it/....jpeg` source | **Pass** |
| outbound link (redgifs etc.) | full outbound URL | **Pass** |
| video (v.redd.it) | base `v.redd.it/<id>` only, no manifest | **Pass via re-route** — `yt-dlp` (already used in [generic_video_extractor.py:61](DownloaderForReddit/extractors/generic_video_extractor.py#L61)) resolves v.redd.it DASH manifests from just the base URL; replaces the hand-rolled DASH/audio-guessing in [reddit_video_extractor.py](DownloaderForReddit/extractors/reddit_video_extractor.py) rather than reimplementing it |
| crosspost | parent post's reddit **permalink**, not the media | Same resolution need as today ([reddit_uploads_extractor.py:44](DownloaderForReddit/extractors/reddit_uploads_extractor.py#L44) already does this for PRAW) — not new complexity |
| gallery | only `preview.redd.it` renditions, capped at **width=1080** — no original source URL on the card | **Fails as feed-only.** Decision: defer galleries to the per-post-visit path (grouped with the other deferred per-post work below), not MVP feed-only scope. |

**Gate B — is the feed complete for followed users, not just ranked?** `sort=BEST` (the default) returned a literal empty state ("There is no content to display") for an account that had already browsed its follows — personalization exhaustion, not missing content. Switching to `sort=new` (drive `/new/`) returned real posts. **Decision: the source always drives `new`.** Formal ID-diff against a followed user's own `/user/<name>/new/` feed to confirm zero silent drops is still open — do this in Phase 1/2 once the shim exists, not blocking further work.

Net result: **proceed**, with galleries routed to the deferred per-post-visit path instead of feed-only.

### Phase 1 — Map the shim surface (done)

**`reddit_id` dedup continuity — checked against the real DB, mismatch found.** `Post.reddit_id` in the live DB (`dfr.db`) stores PRAW's **bare** id (e.g. `1hsm2z6`, no prefix). The DOM `id` attribute is a **fullname** (`t3_1v5o0fq`). These do not match as-is — `SubmissionData.reddit_id` must strip the `t3_`/`t1_` prefix from the DOM `id` before use, or the first run treats the entire existing library as new and duplicates it. (Corrects an earlier wrong assumption in this plan that they already matched.)

**Full `SubmissionData` field mapping**, from a survey of every PRAW attribute read across `submittable_creator.py`, `download_runner.py`, `submission_filter.py`, `comment_handler.py`, `update_runner.py`, and the extractors:

| `SubmissionData` field | Source | DOM attribute | Notes |
|---|---|---|---|
| `reddit_id` | `submission.id` | `id` | strip `t3_` prefix (see above) |
| `title` | `submission.title` | `post-title` | |
| `url` | `submission.url` | `content-href` | see Gate A table for per-type caveats |
| `domain` | `submission.domain` | `domain` | |
| `author` | `submission.author.name` | `author` | |
| `subreddit` | `submission.subreddit.display_name` | `subreddit-prefixed-name` | strip `r/`/`u_` prefix |
| `created` | `submission.created` (epoch) | `created-timestamp` (ISO 8601) | needs parse, not a raw passthrough |
| `score` | `submission.score` | `score` | also re-read later by `update_runner.py:90` for existing posts — needs a lightweight per-post re-fetch path, not just initial discovery |
| `over_18` (nsfw) | `submission.over_18` | `nsfw` (boolean attr) | |
| `is_self` | `submission.is_self` | `post-type=="text"` (confirmed: also `domain="self.<subreddit>"`, `content-href` points back to the permalink itself) | body content not needed for this field, just dispatch |
| `selftext` / `selftext_html` | `submission.selftext[_html]` | none on the card | self posts are already in the **deferred per-post-visit** bucket |
| `pinned` / `stickied` | `submission.pinned/stickied` | not found in either `/new/` sample (expected — `/new/` sorts by recency and wouldn't surface a pin) | used by [download_runner.py:390](DownloaderForReddit/core/download_runner.py#L390) to bypass the date-limit cutoff; needs a probe against a subreddit's default/hot listing where a stickied post would appear — still open, low priority (edge case, not gating) |
| media_metadata (gallery) | `submission.media_metadata` | not on the card (preview-only) | confirmed Gate A fail — deferred to per-post visit |
| gif→mp4 upgrade | `submission.preview.images[0].variants.mp4.source.url` | not on the card | new finding, same as gallery — `.gif`-ending `content-href` needs a per-post visit for the mp4 upgrade in [reddit_uploads_extractor.py:137](DownloaderForReddit/extractors/reddit_uploads_extractor.py#L137); until visited, fall back to downloading the plain `.gif` |
| video fallback URL | `submission.media.reddit_video.fallback_url` | not on the card (`content-href` is only the base `v.redd.it/<id>`) | resolved via re-route to `yt-dlp`, not a DOM field — see Gate A |
| comments (full tree) | `submission.comments`, `.replies`, `.comment_sort`, etc. | none | entire [comment_handler.py](DownloaderForReddit/core/comment_handler.py) is PRAW-tree-walking; already in the deferred bucket |

**Gate B formal check — FAILED.** User-profile pages use `/user/<name>/submitted/?sort=new` (not `/user/<name>/new/`, which doesn't exist) — confirmed active via the page's own `feed_options":{"sort":"new"}` tracking data and highlighted sort tab, so this isn't a repeat of the `BEST`-default trap. Diffed a followed user's 7 home-feed post IDs against 22 IDs from their own `submitted/?sort=new` page: **zero overlap**, despite the ID ranges being adjacent, not far apart. The home feed, even at `sort=new`, does not reliably surface everything a followed user has posted — it appears to be capped/interleaved across all followed sources rather than a clean per-source union.

**Decision: accept the gap, keep the aggregated feeds as primary.** Some missed posts are tolerable for now — the two-feed aggregation stays the primary discovery path for the throughput/human-plausibility reasons below, not per-target sweeps. Revisit with a periodic per-target reconciliation pass later if the miss rate turns out to matter in practice.

**New Phase 2 gap surfaced:** `pinned`/`stickied` have no confirmed DOM attribute yet — need a probe pass specifically looking for a stickied post's markup (none appeared in the Phase 0 samples).

### Phase 2 — `RedditSource` interface + `BrowserRedditSource` (done)
- `RedditSource` Protocol and `SubmissionData`/`ValidationResult`/`ValidationError` dataclasses defined in [core/reddit_source.py](DownloaderForReddit/core/reddit_source.py).
- `BrowserRedditSource` implemented: navigates to `/new/` (home feed), `/r/<sub>/new/`, `/user/<name>/submitted/?sort=new` (corrected from the plan's original `/user/<name>/new/`, which doesn't exist — see Phase 1), or `/user/<owner>/m/<name>/`; scrolls, reads `page.locator('shreddit-post').all()` attributes each pass, dedups by `reddit_id` across scroll iterations via a `seen` set.
- Playwright auth via a **persistent** browser profile (`launch_persistent_context`, fixed `browser_profile/` user-data dir at the project root) logged into the **dedicated account** — already-logged-in, no cookie extraction, no DPAPI.
- **Serial by design:** a `threading.Lock` wraps every navigate-and-collect call so only one scroll session runs at a time — intended, not a limitation (parallel scroll sessions would read as bot activity). `start()`/`stop()` own the Playwright process and persistent context lifecycle explicitly, so the caller controls when the one long-lived window opens/closes rather than each call launching its own.
- **Smoke-tested against live data:** `iter_subreddit_submissions('pics')` correctly parsed 5 real posts — bare `reddit_id` (prefix stripped), stripped `subreddit` name, parsed `created` datetime, correct `post_type` dispatch (`gallery` vs `image`).
- **`validate_user`/`validate_subreddit` are best-effort and unverified.** They pattern-match reddit's known 404/private/suspended page copy, but Phase 0/1 only ever probed *valid* targets — no real invalid/private/suspended page has been inspected. Confirm before relying on this in Phase 3.

### Phase 3 — Rewire discovery (OAuth teardown done, discovery rewiring in progress)
- **PRAW OAuth login removed (done, pulled forward from Phase 5).** Startup no longer calls `load_token()`/`check_authorized_connection()` ([downloader_for_reddit_gui.py](DownloaderForReddit/gui/downloader_for_reddit_gui.py)); `sign_out`/`start_oauth_flow`/`finish_oauth_flow` and the "Connect Reddit Account" menu wiring are deleted (menu item now shown disabled). `DownloaderForReddit/core/user_auth.py` (`UserAuth`, the `QOAuth2AuthorizationCodeFlow` wrapper) and its test are deleted outright. `reddit_utils.py` lost `save_token`/`load_token`/`delete_token`/`check_authorized_connection`/`connection_is_authorized`/`TOKEN_SCOPES`/`TOKEN_DURATION`/`REDIRECT_URL` — all OAuth-only and fully dead once the GUI no longer calls them. The `reddit_access`/`reddit_access_token` settings fields are removed from `settings_manager.py`. `PyQtNetworkAuth` and `cryptography` dropped from `requirements.txt`/`requirements-mine.txt` (no remaining callers). `reddit_utils.get_reddit_instance()` itself is **kept** — still called by not-yet-rewired sites below; deleting it now would break the app before the replacement lands. All 151 files in the package still compile after this pass.
- Replace `reddit_utils.get_reddit_instance()` usages with the source. **Not started** — still called from `content_runner.py`, `reddit_uploads_extractor.py`, `reddit_video_extractor.py`, `download_runner.py`, `update_runner.py`, and `reddit_utils.NameChecker`.
- Rewrite `download_runner.validate_object()` ([download_runner.py:102](DownloaderForReddit/core/download_runner.py#L102)) against `ValidationResult` (map missing→invalid, private→forbidden, throttle→rate-limit).
- Feed discovery: use `iter_home_feed` / `iter_multireddit` for steady-state; per-target iterators for backfill.
- Concurrency: discovery serializes through the browser; media download stays parallel on the existing `Downloader` threads.
- `ExtractionSet`/`RunPair` currently carry live PRAW submission objects end-to-end (`download_runner.py`); these need to carry `SubmissionData` instead, which ripples into `content_runner.py` and `submittable_creator.create_post()` (currently reads `submission.title`/`.created`/`.domain`/etc. directly off the PRAW object — see the Phase 1 field-mapping table for the exact replacement reads).

### Phase 4 — Account reconcile
- Sync step on the **dedicated account**: DFR tracked users → follows (only these); tracked subs → `/m/dfr` membership. Enforce follow-only-tracked-users; handle any following cap; pace mutations to stay human.

### Deferred (post-MVP)
- **Per-post visit/expand operations.** Self-post text-link extraction ([submission_handler.py](DownloaderForReddit/core/submission_handler.py) `extract_self_post`) and comment operations ([content_runner.py:119](DownloaderForReddit/core/content_runner.py#L119)) currently read post *body/comment* content, which in the browser model means navigating to or expanding each individual post — extra activity beyond the feed scroll. These are rare in practice, so postpone them: MVP covers feed-listing media only. Revisit once the listing path is proven.
- **Gallery posts.** Per Gate A, the feed card only exposes `preview.redd.it` renditions capped at width=1080, not original sources. Route galleries through the same per-post-visit path as the items above rather than the feed-only path — grouped here because it's the same navigate-to-permalink mechanism, not new work.

### Phase 5 — Cut over
- OAuth login UI and token storage/encryption already deleted in Phase 3 (see above). Remaining: delete PRAW/`prawcore` themselves and `reddit_utils.get_reddit_instance()` once all call sites are rewired to `RedditSource`.
- Rewrite [CLAUDE.md](CLAUDE.md) to drop fork conventions.

## Open unknowns

1. Following-count cap vs. number of tracked users — not yet hit with 2 follows, needs checking at scale.
2. Does Reddit ever serve a JS/behavioral challenge on these scroll sessions? (browser handles it, but affects cadence/throughput)
3. Multireddit (`/m/<multi>`) page — confirmed reachable at `/user/<owner>/m/<name>/`, but its `<shreddit-post>` markup hasn't been inspected yet the way the home/subreddit feeds have; also unconfirmed whether it has the same completeness gap as the home feed.
4. `pinned`/`stickied` DOM attribute — not found in `/new/`-sorted samples (expected, since pinning doesn't apply to strict recency sort); needs a default/hot listing probe. Low priority.
5. `validate_user`/`validate_subreddit` invalid/private/suspended-page detection — implemented as best-effort text matching against reddit's known copy, never verified against a real 404/private/suspended page. Must confirm before Phase 3 wiring, since `download_runner.validate_object()` gates the whole download path on this.

Resolved: recommendations can be disabled in settings; `sort=new` (not the default `BEST`) is required for non-empty results; posts are DOM attributes on `<shreddit-post>`, no network interception needed; image/link/video content types pass Gate A, gallery does not (deferred to per-post visit); the home feed is not complete even at `sort=new` — accepted as a known gap, not a blocker.

## Risks

- DOM contract stability: `<shreddit-post>` attribute names are shreddit's public custom-element contract, more stable than internal GraphQL operation names, but still unversioned and could change without notice.
- Throughput: single-browser discovery is far slower than the old 4-thread PRAW discovery, and galleries now require a per-post visit on top of that.
- Dedicated-account trust: bulk-following is detectable activity, riskier on a fresh account (throttles, challenges, bans); use an aged account and pace the follow sync. Isolation keeps the personal account and feed clean.
- Feed contamination: any stray follow/subscription on the dedicated account injects non-tracked posts into the home feed; the reconcile step must enforce follow-only-tracked-users.
- Deep-history downloads are no longer possible for the ~1000 cap — accepted.
- Incomplete home feed: confirmed via ID diff (zero overlap between a followed user's home-feed posts and their own `submitted/?sort=new` posts) that the aggregated home feed silently misses posts even at `sort=new`. Accepted for now — some missed posts are tolerable, and the gap should narrow naturally once polling is frequent (smaller window per run means fewer posts at risk of being dropped); the `/user/<name>/submitted/?sort=new` path also gives a concrete backfill/reconciliation mechanism if the miss rate turns out to matter.
