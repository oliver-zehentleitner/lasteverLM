# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Keep a Changelog records what changed; [Keep the Why](https://keepthewhy.com)
preserves why it changed, in `context/`.

## [Unreleased]

## [0.22.23] - 2026-10-18

### Fixed

- `POST /sources/estimate` refused `library_ids: []`, which rag sends when every selected source is read already and only a web page is to be fetched again; the plan then showed the raw validation error. Either list may be empty now, not both (#682, the empty-list part).

## [0.22.16] - 2026-10-15

### Fixed

- A generated document could show in its own notebook's Sources list (badge "Generated", checkbox, bin) besides Outputs. `GET /notebooks/{id}/sources` returned every generated row's origin link, and only the web hid them by matching the outputs list. A row no output pointed to (left by an abandoned generation attempt) slipped through. The list now leaves out every generated row's origin link (link id = row id); a document linked into another notebook is still a source there.

## [0.22.9] - 2026-10-15

### Changed

- An audio transcript that degenerated into a repetition loop is asked for once more at temperature 0.4, like an image, instead of failing at once (core's transcriber now passes sampling settings; the speech model honours the temperature and ignores penalties). A second loop fails the source; both calls are billed.

## [0.22.8] - 2026-10-15

### Added

- A user can have the starter notebook made again (`POST /starter/copy`, for the help page's "Recreate"): a fresh, free copy of the current starter, with rag's handover for the outputs. Starter sources the user still has are linked (restored from the trash if need be), and only the ones that are gone are copied. Without `force=true`, a user who still has a starter notebook gets 409 `starter_exists`; there is at most one such copy an hour (429 `starter_copy_too_soon` with `retry_after`), and 404 `no_starter` without a starter. Notebooks carry `starter` (migration 0019, existing starter copies backfilled).

### Fixed

- Large images and vision-read PDF pages are read in tiles. The platform scales every image to the same small budget, so a large screenshot's small text was unreadable and the vision model filled it with invented lines: on a 2880×1800 app screen, most detail lines of 72 cards were invented. An image or rendered page over 1.5 MP is cut into the fewest crops of at most 1.5 MP (a 2880×1800 screenshot into four, an A4 page into two), overlapping by 48 px. The crops are read in parallel with the loop check per tile and joined in reading order, without the lines the overlap read twice. Every call is billed, the estimate counts the tiles (recorded at upload), and the chunks view gives `tiles`. The measurements are in `docs/vision-eval.md`.

## [0.22.7] - 2026-10-15

### Added

- Sources say which model made their text. A linked source and the chunks view carry `read_by: {"vision": <model> | null, "stt": <model> | null}`: the vision model for an image or vision-read PDF pages, the speech model for a recording, `null` otherwise and for sources read before. Each chunk in the chunks view carries `vision`: true for an image, and for a PDF chunk that starts on a page the vision model read. In `auto` capture that is only the pages without a text layer. Recorded when a read finishes (migration 0018: `library_sources.read_by`, `vision_pages`) and kept on starter copies.

## [0.22.6] - 2026-10-15

### Fixed

- A model-made transcript that degenerated into a repetition loop was indexed as the source. One case was a screenshot read as a few labels followed by hundreds of "Internet-UI" until the output cap; audio, quizzes and decisions were then built from it. Every image, vision-read PDF page and audio transcript is now checked deterministically (an n-gram repeated 100 times back to back, a capped answer whose tail is one repetition, or 500 words with fewer than 25 distinct ones). An image or page is asked for once more with settings against repetition (temperature 0.4, frequency, presence and repetition penalties). If it still loops, the source fails with "the model's description degenerated into repetition" (audio: "transcript"). Every call is billed, and nothing is indexed.

## [0.22.3] - 2026-10-15

### Added

- A URL that returns structured data is read instead of failing with "unsupported content type": JSON (and any `+json`), NDJSON, XML (and any `+xml`, RSS and Atom included), CSV, TSV and YAML. Each value becomes a line with its key path, element path or column name, so a chunk cites it with its context; one object, element or row is one passage, and the snapshot shows the same rendering. JSON and YAML numbers are written positionally (`0.00001`, not `1e-05`); the rendering is documented as a contract in `docs/README.md`, because rag's number check tells values from `row <n>` and `[<n>]` positions by it. A `text/plain` body that parses cleanly as a JSON object or array is read as JSON. XML is parsed by defusedxml (no entity expansion, no external entities or DTDs), YAML by `safe_load`; nesting over 32 levels, more than 10,000 values, CSV rows over 500 columns and fields over 100,000 characters fail the source with that reason. A type still unsupported fails with a message naming it and the supported list.

## [0.21.1] - 2026-10-15

### Changed

- What started work holds now includes rag's waiting and processing outputs (`outputs.reserved_cents`) and the explain steps of running agent plans: a read starts only if the balance covers its estimate plus the owner's funded reads still waiting plus those, in the claim and when a request is funded, the same sum rag holds. Before, a read could start on balance a running output or plan had reserved.

## [0.21.0] - 2026-10-15

### Changed

- An action starts only if the balance covers its estimated maximum plus the owner's other funded reads still waiting, and then runs to the end: a process or refetch request that passes this check marks its rows funded (`funded_request_at`, holding their estimate in `funded_cents`, migration 0017) and the claim takes them even after the balance went negative; an unfunded row waits until the balance less what funded reads hold covers its estimate. Fixes a confirmed agent plan whose source waited half-way because an earlier step had spent the balance.

## [0.20.5] - 2026-10-15

### Changed

- A read that has started runs to the end: credit is checked once, before a source is read, not before every model call, so a vision PDF or a long recording is never stopped halfway with its first pages paid for. The balance may end slightly below zero; the owner's next read waits until it is positive again. The exchange-rate check still runs before every call.

## [0.20.4] - 2026-10-15

### Fixed

- A new user saw no starter notebook after signing in: the copy ran only on the first library listing or upload, and the web's first call is `GET /notebooks`. The notebook list now makes the copy first, so it is in that very response (`GET /library?kind=generated` copies too); once a user is considered, it costs one indexed exists query.

## [0.19.6] - 2026-10-14

### Fixed

- A web page whose content is built by JavaScript (an app shell, such as a single-page app) now fails with "the page has no readable text without JavaScript (it builds its content in the browser); add a page that serves its text as HTML" instead of "page contains no readable text", which did not say why; a page without text and without scripts says "the page contains no readable text".

## [0.19.3] - 2026-10-14

### Fixed

- An unchanged refetch of a web page read before snapshots existed now stores its snapshot (and `final_url`); before, such a page had no "Original" until its content changed. A page that has its snapshot keeps it, without a second write; `fetched_at` stays.

## [0.19.2] - 2026-10-14

### Added

- Every read of a web page version keeps a snapshot of what was read: the fetched HTML rebuilt from an allowlist (no scripts, styles, frames, forms, images or other external resources, no event handlers), stored as `text/html` beside the source, with `final_url` after redirects (migration 0016). Sources and library entries carry `snapshot_url` (presigned, one hour, inline), `final_url` and `checked_at`; an unchanged refetch keeps the snapshot and `fetched_at` and only moves `checked_at`; purging removes the snapshots with their versions.

## [0.19.1] - 2026-10-14

### Added

- `GET /sources/{id}/text`: a text or Markdown source's stored text as `text/plain; charset=utf-8`, read or not, for the viewer's "Original" view from the API's own origin (no presigned URL, no CSP change); 415 for other kinds, 413 above 2 MiB.

## [0.19.0] - 2026-10-14

### Added

- Web pages have versions: `POST /sources/{id}/refetch` fetches a page again; unchanged (by the hash of its extracted text) only `fetched_at` moves and nothing is charged, changed a new version (`version + 1`) takes the links and is read, and the old version is kept read-only (hidden, its chunks kept for old citations). Sources and library entries carry `version` and `fetched_at` (migration 0015); purging a page purges its older versions.
- `POST /sources/process` `{"library_ids"}` reads the caller's unread sources (`added` to `processing`; `{"queued", "unchanged"}`), and `POST /sources/estimate` `{"library_ids", "refetch"}` gives what reading them would charge (min/max and a basis such as "12 pages, text layer") from the page count and recording length stored at upload (migration 0014).

### Changed

- A source is read when it is used, not when it is added: uploads, URLs and texts are stored as `added` (migration 0013) and no worker reads them until `POST /sources/process` (rag before an answer or an output, the web's "read now"); a failed source uploaded again goes back to `added`. `GET /admin/stats` `sources` counts `added`.

## [0.18.5] - 2026-10-14

### Added

- `GET /sources/{id}/chunks?offset&limit` for the "As read" view: the source's chunks in order, each with `chunk_id`, `index` (as citations carry them), `page` and `text`, plus `chunk_count` and the capture mode; paged (limit 1–200), owner only.

### Changed

- A new user gets a copy of the starter notebook an admin marks (account's settings `starter_notebook`) on their first library access or upload, instead of the Zeltavia demo seeds: its uploads are copied with their stored objects, chunks and embeddings, ready at once, flagged `starter`, without processing or charge; rag copies the outputs from the id maps ingest writes to `output_copies` in the same transaction. Without a starter nothing is seeded. The `demo` field and column are now `starter` (migration 0012).

### Removed

- The Zeltavia demo seeds: `seeds.py`, the demo assets and their build scripts, `SEED_DEMO_LIBRARY`, and the demo user's sponsorship of processing (the `demo_seed` action and the claim query's exception for demo rows).

## [0.18.4] - 2026-10-14

### Added

- `GET /estimate?kind=text|url|pdf_text|pdf_vision|image|audio`: the cost and charge of one nominal unit of a source (a page, a web page, an image, a minute of audio) through core's billing at the current pricing rules, plus the once-per-source part (summary and fixed fee), for account's public price list. Any bearer; the nominal sizes are constants in `api/estimate.py`, listed in docs.

## [0.18.3] - 2026-10-14

### Changed

- Without an exchange rate in the pricing rules sources wait instead of failing, as in rag: no worker claims one, a job in flight goes back to the queue before its next model call (attempt not counted), and queued sources show `error` "pricing has no exchange rate, ask an admin" until an admin sets the rate; then they run without a re-upload.

## [0.18.0] - 2026-10-14

### Changed

- Usage is booked through core's new `record_usage` (cost and charge in core: the cost in US cents, charged in euro cents at the pricing rules' rate): every model call of processing one source is one action, `source:<kind>` (`demo_seed` for the demo sources), with the library source's id as `action_id`; the claim query uses core's `funded_sql` (credits minus charges) instead of its own copy of the balance. Without an exchange rate in the pricing rules the worker makes no model call: the source fails with "pricing has no exchange rate, ask an admin" instead of a call paid upstream but never booked.

## [0.17.0] - 2026-10-14

### Added

- Notebooks can be renamed (`PATCH /notebooks/{id}` `{"title"}`, trimmed, 1–200 characters as at creation) and shared read-only by a link: `POST /notebooks/{id}/share` gives `{"share_token"}` (made once, the same until `DELETE /notebooks/{id}/share` revokes it); migration 0011 adds `notebooks.share_token` with a unique index. Notebooks carry `share_token` (null when not shared). The public view is rag's; it shows sources by name and kind only.

## [0.16.0] - 2026-10-14

### Added

- `POST /notebooks/{id}/sources/bulk` `{"library_ids"}` links up to 200 library entries (uploads in any state, ready outputs) into a notebook in one transaction, without processing or cost; answers `{"added", "already"}` (library ids), an existing link is never doubled, and an id that cannot be linked makes it a 404 that adds nothing. `GET /library?kind=all` lists uploads and outputs together; output entries carry `output_id`.

### Changed

- Adding a source no longer needs credit; processing does. Without credit an upload, URL or text is stored and queued (no more 402) and shown as `waiting_for_credit` in sources and library entries; the worker's claim query skips sources whose owner has no positive balance (no attempt counted) and takes them once there is credit again. Credit running out mid-job puts the source back to wait instead of failing it. `GET /admin/stats` `jobs` gains `waiting_for_credit`; `pending` counts only sources a worker can take.
- A WAV over the transcription model's 25 MiB is converted to MP3 like the other formats (mono 16 kHz, 32 kbit/s, the original kept and offered as `original_url`) instead of being refused; a WAV within the cap and every MP3 pass through untouched. Transcription is billed per second, so the cost is the same; a 44.1 kHz stereo WAV hits the cap after about 2.5 minutes. The image's ffmpeg gains the WAV demuxer and PCM decoders (+1.4 MB).

## [0.14.0] - 2026-10-14

### Added

- A fifth demo source: `hollin-interview.mp3`, an 80-second radio interview with the keeper of Hollin Lighthouse (two platform TTS voices, 637 KB), whose facts are in no other demo source. Seeded and processed like the others at the demo user's expense, transcribed by the normal pipeline. Owners already seeded do not get it; the demo reset does. The script (`hollin-interview.txt`) and `demo/build_audio.py` rebuild it.

### Changed

- `GET /healthz` reports `dependencies`: `postgres`, `storage` and `inference`, each `"ok"` or `"down"` (core's `DependencyProbe`, 2 s per check, cached 15 s), for the admin system view; `status` stays `"ok"` while one is down.

## [0.12.0] - 2026-10-14

### Added

- `DELETE /admin/users/{id}` (admins only): deletes everything a user owns in ingest, the stored objects first (originals and converted recordings, trash included), then the library rows, notebooks and seed mark in one transaction; 503 naming the first key on a storage failure, with the rows kept for the retry; 204 also when nothing is left. For account's user deletion.
- Audio in other formats: M4A/MP4 (also a video's audio track), AAC, Ogg (Vorbis, Opus), FLAC and WebM are accepted, recognised by their first bytes and stored as uploaded, capped by `MAX_AUDIO_UPLOAD_BYTES` (60 MiB). The worker converts them with ffmpeg to mono 16 kHz MP3 at 32 kbit/s (demuxer forced from the bytes, file protocol only, under `/tmp`, at most 300 s; a failure names ffmpeg's last line), stores the MP3 beside the original (migration 0010, `transcribed_path`) and transcribes that; core's 25 MiB and four hours apply to the MP3. `preview_url` of a converted recording is the MP3, the new `original_url` the original as a download under its name (`null` when nothing was converted); purging removes both objects. The image builds a minimal ffmpeg 7.1.5 from the checksum-verified source (+5 MB; Debian's package would add 627 MB).

### Changed

- Outputs are library items: `GET /library?kind=generated` lists the user's ready outputs (rag's generated library sources, not trashed) with their origin notebook (`origin: {id, title}` from rag's `outputs`, the id null once that notebook is deleted), and `POST /notebooks/{id}/sources` with `{"library_id"}` links one into any of the user's notebooks, reusing its chunks (nothing processed or billed). Deleting a notebook or detaching an output removes only the link; the output stays (rag owns its trash, restore and purge). Needs rag's migration 0004 (`outputs.source_id` on library sources, `origin_title`).

## [0.11.0] - 2026-10-14

### Added

- Audio sources: `.mp3` and `.wav` uploads (`audio/mpeg`, `audio/mp3`, `audio/wav`, `audio/x-wav`), checked at upload with core's `probe_audio` (WAV or MP3 by the bytes, at most 25 MiB and four hours, 415 with core's reason otherwise); other audio formats get 415 "audio must be MP3 or WAV". The worker transcribes the whole file with `STT_MODEL` in one billed call (usage kind `stt` in seconds, credit checked before, claim refreshed after; own client, one attempt, `STT_TIMEOUT_SECONDS` 300, below the claim timeout) into one passage without a page, chunked like text; an answer the platform billed but without speech or duration is recorded and fails the source. `STT_MODEL` needs a price at start. Deduplicated by sha256 like every file; `preview_url` is the presigned original, so the browser plays it; the daily admin stats count `audio` in `by_kind`. Migration 0009 adds `audio` to `library_sources.kind`.

### Changed

- A provider error core marks `permanent` (a 4xx other than 408, 409, 429) fails the source at once instead of being retried, for every model call.

### Removed

- Migration `0004_source_library_contract.sql`, the contract step of `0003_source_library.sql`: drops the columns of `sources` (`name`, `kind`, `storage_path`, `status`, `error`, `summary`, `claimed_at`, `claimed_by`, `attempts`, `size_bytes`) and of `chunks` (`source_id`, `notebook_id`) that moved to `library_sources`, and makes `library_source_id` not null on both, after backfilling any row an old pod wrote during 0003's rollout. Ships in a release after the one with 0003. On a database that already has 0005–0009 it runs after them (the migration job applies every file not yet recorded, in order); it touches no column those add.

## [0.10.1] - 2026-10-14

### Fixed

- A PDF page whose text layer is glyph codes (a font without a usable ToUnicode map, extracted as `&MM,:N K OP&M,MM P2QL …`) no longer counts as text: a page needs at least 60 % letters among its visible characters as well as the 20-character minimum. `auto` sends such a page to the vision model, `text` skips it (and fails with "no legible text" when no page is left); the garbage used to be chunked, embedded and cited.

## [0.10.0] - 2026-10-14

### Added

- `make test-postgres`: the repository's SQL against a throwaway PostgreSQL with pgvector (`tests/test_postgres.py`): the library and its dedupe conflict, ownership on every owner-scoped call, the trash with the owner's retention, the queue (generated, trashed and backed-off rows, stale takeover, concurrent claims), the seed claim, credit and usage, the admin counts.
- PDF and text sources carry `preview_url` like images (a presigned GET of the stored original, one hour; the object's type makes the browser show a PDF in its viewer and text as `text/plain; charset=utf-8`), and URL sources carry their URL, so the library can open a document; generated rows stay `null`.

### Changed

- `GET /admin/stats` and `GET /admin/stats/daily` admit viewers as well as admins (core's `admin_read` over `admin_role`): read-only admin access for reviewers. A viewer is an ordinary user everywhere else in ingest. Needs account's `users.role` column.
- mypy runs in strict mode; the last `type: ignore` comments are gone (`record_usage` takes core's `UsageKind`, `ensure_seeded` core's `Storage`).
- Dedupe keys are built in one place (`dedupe.py`) for uploads and the demo seeds.
- PDFs of more than 2000 pages fail in every capture mode, also behind a URL, before any page is read ("PDF has N pages; at most 2000 are read"); the text layer of every page used to be extracted however many there were.
- The Dockerfile pins `python:3.13-slim` by digest (the multi-arch index) for both stages, through one `PYTHON_IMAGE` build argument; the comment says how to refresh it.

### Fixed

- A source whose worker died or hung on every attempt was taken over by the next worker after each claim timeout, without end. The claim now fails a stale row that has had three attempts ("processing did not finish in 3 attempts").
- A job that ran longer than the claim timeout (a vision PDF of many pages) could be taken over by a second worker while still running, reading and billing every page twice. The worker now refreshes its claim with every answered model call and every embedding batch.
- A multipart upload accepts one file and at most the `capture` field; more answer 400 before the parser spools them (the default allowed 1000 of each).
- A JSON body that does not parse answers 400 `body is not valid JSON` instead of 500.
- Routing errors (404 for an unknown path, 405) and the form parser's 400 use the `{"error"}` shape instead of Starlette's `{"detail"}`, and keep their headers (`Allow`).
- Documentation: the README's CI and ktw-lint badges pointed to the retired single-service repository; `docs/README.md` lacked `capture`, `demo` and `preview_url` in the source, library and trash shapes; `.env.example` lacked `IMAGE_MAX_TOKENS`, `PDF_VISION_MAX_PAGES` and `SEED_DEMO_LIBRARY`.
- The URL guard's DNS lookup was not bounded by the fetch deadline (`getaddrinfo` has no timeout); a slow resolver could hold a worker past the 30 seconds. The lookup now runs in a small thread pool and a lookup that does not answer in time is a transient failure.
- The test fake, the `has_credit` docstring and `docs/README.md` still said an admin always has credit; core changed that (15f8fa6, "admins need credit like every user"). The fake now applies core's rule and the test asserts it.

## [0.9.0] - 2026-10-13

### Added

- Image sources carry `preview_url`, a presigned GET of the stored original valid for one hour, on the notebook's source list, the library and the trash, so the frontend shows the image instead of its file name; `null` for every other kind.

## [0.7.2] - 2026-10-13

### Fixed

- Uploads write the object to the bucket before the library row: a worker could claim the row first and fail with `NoSuchKey` (the v0.7.1 smoke failure). A failed upload now answers 503 and leaves no row.
- A transient failure releases the claim with a backoff of 5 and then 15 seconds (`retry_at`, migration `0008_retry_at.sql`), so the next attempt follows promptly instead of after the 10-minute stale-claim timeout.

## [0.7.1] - 2026-10-13

### Changed

- `/healthz` answers with core's `Health`: `status`, plus `version` and `commit` from `APP_VERSION` and `APP_COMMIT`, so the admin system view shows ingest's build.

## [0.7.0] - 2026-10-13

### Added

- Demo library: every new user's library starts with four demo sources around the fictional Zeltavia (field notes PDF, glossary, map image, the Wikipedia article on dead reckoning), seeded once on the first library access or upload and processed at the demo user's expense; entries carry `demo: true`. `SEED_DEMO_LIBRARY` turns it off. Migration `0007_library_seeds.sql`.

## [0.6.0] - 2026-10-13

### Added

- PDF capture modes per upload (`capture` field: `text`, `vision`, `auto`, default `auto`): `vision` has the vision model read every rendered page (pypdfium2, 150 dpi, one billed call per page), `auto` only the pages without a text layer, so scanned and mixed PDFs work. The mode is stored on the library source and returned in sources, library and trash entries; the same bytes in two modes are two library sources. `PDF_VISION_MAX_PAGES` caps the pages per PDF (100). Migration `0006_pdf_capture.sql`.
- Library trash: `GET /library/trash` (with `deleted_at`, `purge_at`), `POST /library/{id}/restore`, `DELETE /library/{id}/purge`. Workers purge entries past the owner's `trash_days` (account preference, default 14) at start and once an hour. Migration `0005_library_trash.sql` adds `library_sources.deleted_at`.
- `GET /api/ingest/admin/stats/daily?days=30` (admins only): library sources created per UTC day by kind, and how many of them failed, every day present, oldest first.

### Changed

- Model-reading loaders bill every call as it returns through a callback, and the credit is checked before the next call (before, an image's usage was returned at the end).
- `DELETE /library/{id}` moves the entry to the trash: removed from every notebook, chunks and stored object kept until purged. Uploading the same bytes or URL again restores a trashed entry.

## [0.5.0] - 2026-10-13

### Added

- Per-user source library: `GET /library`, `GET /library/{id}` (with the notebooks each source is in) and `DELETE /library/{id}` (removes it from every notebook, chunks and stored object included). Generated outputs are never part of it.
- `POST /notebooks/{id}/sources` with `{"library_id"}` adds a library source to another notebook without uploading or processing it again.
- Uploading the same bytes (sha256) or entering the same normalised URL again links the user's existing library source: 202 with a new link, 200 with the existing one when it is already in this notebook. A failed one is queued again.
- Migration `0003_source_library.sql` (expand step): `library_sources`, `sources.library_source_id`, `chunks.library_source_id`; every existing source gets a library source with the same id.

### Changed

- `DELETE /sources/{id}` detaches the source from its notebook; it stays in the library with its chunks and stored object. Deleting a notebook detaches its sources instead of deleting their files.
- Source responses carry `library_id`. Objects of new library sources are stored at `<owner>/<library_id>/<name>`.
- The worker processes library sources once, whatever number of notebooks link them; it never claims rag's generated rows (before, it could claim one between rag creating and finishing it).

## [0.3.0] - 2026-10-13

### Added

- `GET /api/ingest/admin/stats` (admins only, core's `admin_only`): sources by status, jobs pending/running/failed, total chunks and stored bytes.
- Migration `0002_source_size.sql`: `sources.size_bytes`, written on every new source.

### Changed

- Credit checks use core's `has_credit`: admins are never out of credit, everyone else still gets 402 at zero balance.

## [0.2.1] - 2026-10-13

### Changed

- Formatting is checked with `black` (line length 100) instead of `ruff format`; `ruff check` stays the linter.

## [0.2.0] - 2026-10-13

### Changed

- Object storage comes from core (`lasteverlm_core.storage`, shared with rag); ingest's own S3 client is gone, the `S3_*` variables are unchanged.

### Added

- URL sources: `POST /notebooks/{id}/sources` with `{"url"}` stores the URL, the worker fetches it and loads web pages (HTML text extraction), PDFs and plain text behind it. Every connection and redirect hop is checked against public addresses only.
- Image sources (PNG, JPEG, WebP, GIF, checked by their first bytes): the worker has `LLM_MODEL` transcribe the text and describe the image, bills that call as its own `usage` row, then indexes the text like any other source. `IMAGE_MAX_TOKENS` caps the answer.

### Fixed

- A blank image fails its source at once with "the image holds no readable content" instead of being retried three times, and the model call that found it empty is recorded as usage.

## [0.1.0] - 2026-10-12

### Changed

- `CHUNK_TOKENS` and `CHUNK_OVERLAP_TOKENS` are optional overrides; unset, the chunker runs with core's defaults, where the family tunes them.

- The LLM is built with `LLM_MAX_TOKENS` from core's inference settings; thinking is off for the summary (core default), so a reasoning model returns content instead of spending the budget on reasoning. The embedder requests `EMBEDDING_DIMENSION` from the model on every call (core).
- `USER 10001` in the Dockerfile, numeric so Kubernetes' `runAsNonRoot` can verify it.

- Imports models, token resolution, usage accounting, model prices and the provider implementations from `lasteverlm-core` (installed from `core/`); the local mirror `contracts.py` and the own embedder implementation are gone, the summarizer is a prompt on core's `LLM`.
- Core's `RateLimitMiddleware` is mounted outermost on the API.
- The chunker and the `Passage`/`Chunk` types come from core (`lasteverlm_core.chunking`); the service's own copy is gone.
- `INFERENCE_BASE_URL` is optional and defaults to the platform's inference endpoint.

- `make check` installs first (`make install`, `uv sync --all-extras`), so it runs on a fresh checkout and in CI.
- The migration no longer opens its own transaction; the migrations runner wraps each file in one.

### Added

- Credit check before storing, embedding and summarizing; 402 `credit exhausted` when the balance is zero.
- Dockerfile, Makefile (`check`, `run`, `build`), health endpoint on `/healthz` and `/api/ingest/healthz`.
- Embedding provider interface with an OpenAI-compatible implementation through the OpenAI SDK.
- FastAPI service implementing the ingest endpoints of the lasteverLM API contract v1.1, scoped to the bearer-token user.
- Loaders for PDF and plain text sources behind a registry open for url and image.
- Migration `db/migrations/0001_ingest.sql` for `notebooks`, `sources` and `chunks`.
- Mirror of the not yet published core library in `contracts.py` (models, token verification, accounting helpers, price parsing).
- Per-source summary written by the language model at the end of processing, accounted as a `usage` row of kind `llm`.
- S3-compatible object storage for uploads at `<notebook_id>/<source_id>/<name>`.
- Token-window chunking with overlap.
- Unit tests for chunking, loaders, pricing, token verification, the worker and the API against in-memory fakes.
- Usage accounting: one `usage` row per model call, priced from `MODEL_PRICES`; refusal to start when a configured model has no price.
- Worker entrypoint `lasteverlm-ingest-worker` that claims queued sources from PostgreSQL with `FOR UPDATE SKIP LOCKED`, counts attempts, retries transient failures by stale-claim takeover and fails a source for good on the third attempt, and stops after the current source on SIGTERM.

