# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.17.0] - 2026-10-14

### Added

- Output kind `chart` in two steps.
  - `POST /api/rag/notebooks/{id}/charts/propose` (`instructions`, `max` 1–12, default 6) returns candidate charts from one LLM call over numeric passages and stores nothing. The chart types are `bar`, `line`, `pie`, `stacked_bar` and `scatter`. When the sources hold no number series, the list is empty with a reason.
  - A `chart` output is created from the picked candidates (`options.charts`) without a second model call. Its cited chunks are loaded again from the notebook, and every value is checked strictly (`number-match-v1`): a value is `supported` only when the number is written in a cited passage, a computed value is `derived`, and anything else is `unsupported`.
  - Migration `0008_rag_chart_kind.sql`. The catalogue lists `chart`.
- Report options `report_style` (`briefing` default, `detailed`, `executive_summary`, `explainer`, `academic`) and `length` (`short`, `medium` default, `long`); quiz options `question_count` (5–30, default 10), `difficulty` (`easy`, `medium` default, `hard`) and `audience` (`children`, `teens`, `adults` default, `experts`). Defaults are stored on the output; the catalogue lists them under `report` and `quiz`. A quiz with fewer questions than asked gets the corrective retry, extra ones are cut.

- `instructions` for every kind: the user's own notes (at most 500 characters, whitespace collapsed, blank dropped), given to the generator below the system rules; they steer focus and tone and never override the rules, the citation rules or the reply format. Stored in the options and the provenance log; the catalogue lists `instructions.max_chars`.

- `PATCH /api/rag/outputs/{id}` `{"title"}` renames a ready output outside the trash (trimmed, 1–200 characters) together with its generated source; 409 while it is still being generated.

- `GET /api/rag/shared/notebooks/{token}` (no auth): a notebook shared through ingest's `notebooks.share_token`, as its title, its ready uploads by name and kind, and its ready outputs outside the trash as their shared view; 404 for an unknown or revoked token.

## [0.15.0] - 2026-10-14

### Added

- Output kind `decision`: a question (`question`, `decision_type` `yes_no` | `choice` with `choices` | `scale` with `levels`) answered by the platform's decision model (`DECISION_MODEL`, default `clef`, which needs a price) over the retrieved passages, with evidence for and against, gaps and a short verdict written independently by the LLM from the same passages; `agreement` (`consistent`, `conflicting`, `unclear`, `undecided`) compares the two, and without supported evidence the verdict is "The sources do not decide this.". Migration `0007_rag_decision_kind.sql`; the provenance log lists both calls.

## [0.14.0] - 2026-10-14

### Added

- Chat answers get the outputs' support check: `Answer.units` (one `{"text", "refs", "support"}` per paragraph) and `Answer.check` (the counts, method `lexical-overlap-v1`), stored with the assistant message (migration `0006_rag_message_support.sql`) and returned on `Message`; `null` for an answer without citations. An answer in another language than its passages marks low overlap `unverified`.

- Provenance for table, quiz and mind map: `content.row_refs` (one `{"refs", "support"}` per table row), `refs` and `support` on every quiz question (checked with its answer and explanation) and on every mind-map node below the root; the same check and log as the other kinds.

- Provenance for report, infographic and audio: every unit (report paragraph, infographic fact and section, audio paragraph or turn) carries `refs` (the passage numbers it cites) and `support` (`supported`, `unsupported` or `unverified`) from a deterministic word-overlap check after generation (`lexical-overlap-v1`, threshold 0.5, no model call); failing units are kept and marked, never dropped. The report keeps `[n]` markers in its markdown and lists `content.units`. `Output.provenance` (migration `0005_rag_output_provenance.sql`, core's model) is the generation log: options, retrieval candidates and which entered the prompt, the numbered passages with their text, model, prompt version, usage, timestamps and the check's counts; the shared view carries it without ids.

### Changed

- `/healthz` reports `dependencies`: `postgres`, `storage` (when configured) and `inference`, each `ok` or `down`, through core's `DependencyProbe` (2 s per check, cached 15 s); `status` stays `ok` when one is down.

## [0.13.0] - 2026-10-14

### Added

- Audio styles: option `audio_style` (`overview`, the default and the previous behaviour; `story`, the material told as a narrative by one narrator; `conversation`, an interviewer and an expert grounded in the material) with a prompt and a JSON schema per style, and `audio_voice_2` for the expert (default: another voice with the first one's native language). Each turn is read with its speaker's voice and billed per second as before; the audio content carries `transcript` (`[{"speaker", "text"}]`), also on the shared view. The options catalogue lists `styles` and `default_style`.

- Outputs in the user's library: `GET /api/rag/library/outputs?kind=&trash=` lists the caller's outputs from every notebook, including deleted notebooks; `Output` carries `origin_title` (the notebook's title when the output was made), `deleted_at` and `purge_at` (core's model). The generated source of an output is the user's library row, which ingest can link into another notebook without reprocessing.
- A trash for outputs: `POST /api/rag/outputs/{id}/restore`, `DELETE /api/rag/outputs/{id}/purge` (409 unless trashed), `DELETE /api/rag/notebooks/{id}/outputs` (trashes every output made in that notebook). The worker purges trashed outputs after the owner's `trash_days` (default 14), at start and then hourly: the stored file first, then the generated source, then the row; a file that cannot be deleted is logged and kept for the next sweep.
- Migration `0004_rag_outputs_library.sql`: `outputs.owner`, `origin_title`, `deleted_at`; `notebook_id` nullable with `on delete set null`; `source_id` references the generated library row (expand only, `owner` stays nullable for the rolling deploy).

### Changed

- `DELETE /api/rag/outputs/{id}` moves the output to the trash instead of deleting it; the output and its generated source leave every notebook, `GET` and the share link answer 404 until it is restored.
- An output survives the deletion of its notebook (`notebook_id` becomes `null`); a job whose notebook is gone fails with "the notebook was deleted".
- `DELETE /api/rag/admin/users/{id}` deletes the user's outputs and generated sources by owner, so outputs of deleted notebooks and trashed ones go too.

## [0.12.0] - 2026-10-14

### Added

- `DELETE /api/rag/admin/users/{id}` (admins only), account's first step when a user is deleted: the stored files of the outputs in the user's notebooks, then rag's rows there (generated sources, outputs, messages, conversations) in one transaction. Idempotent, always 204; a file that cannot be deleted is a 502 naming its key, with every row kept.

## [0.11.1] - 2026-10-14

### Fixed

- Text to speech is billed per second of generated speech, as the platform reports and charges it (core's `SpeechResult`), instead of per input character, which charged about 14 times the platform's price (100 characters: 2.8 cents against 7 seconds at 0.196 cents); `MODEL_PRICES` for `TTS_MODEL` is read as cents per million seconds.

## [0.10.0] - 2026-10-14

### Added

- Source selection: `source_ids` on `/ask` and on `POST .../outputs` (link ids of the notebook's uploads; absent or empty means all, any other id is a 422). Chat retrieves only from the selected uploads and its document list marks the others `not in scope`; an output is generated from the selected sources only and records them as `options.sources`.

### Changed

- The Docker image builds on `python:3.13-slim` pinned by digest (refresh steps in the Dockerfile).
- `GET /api/rag/admin/stats` and `/admin/stats/daily` admit viewers as well as admins (core's `admin_read` over `users.role`); a viewer is a plain user everywhere else.
- The worker gives each inference request at least 180 s (`LLM_TIMEOUT_SECONDS` from core, default 60, can raise it); the API keeps the configured value.

### Fixed

- A model or embedding call that the provider billed but that still failed (an empty answer, an unusable vector) writes its usage row before the error propagates, in chat, generation and indexing.
- An output with a source selection waits only for the selected sources to finish processing, not for every upload of the notebook.
- A failure while cleaning up after a failed generation attempt (removing the generated source or the audio file) is logged on its own and no longer replaces the attempt's error.
- A text-to-speech answer without audio (output or voice preview) is a provider failure instead of an empty MP3; its usage row is written before the check.

### Security

- Prompts treat text from the user's files as data: both system prompts say so, names are one bounded line, and marker-shaped text in sources and summaries is rewritten (`[2]` → `(2)`) so a source cannot forge a passage or a citation. The coverage warning logs a bounded name.
- `Output.error` no longer carries provider, storage or internal exception messages (they can name endpoints or quote model output): rag's own reasons are shown as they are, everything else as a fixed text per class, with the detail in the log. A schema failure no longer quotes the validation error.

## [0.9.0] - 2026-10-13

### Added

- `PATCH /api/rag/conversations/{id}` renames a chat tab: `{"title"}`, trimmed, 1–80 characters.
- The chat's system prompt lists the notebook's ready documents by kind and title, uploads and generated, so "which documents can you see?" or "the mind map you made" is answered; the model is called even when no passage matches, and only an empty notebook gets the fixed refusal.

### Changed

- An unscoped `/ask` retrieves generated documents again, bounded: the uploads fill the k passages by score, generated chunks above the floor may take at most `RETRIEVAL_GENERATED_SLOTS` (default 2) of them and are listed after the uploads. `RETRIEVAL_INCLUDE_GENERATED` is gone.

## [0.7.3] - 2026-10-13

### Fixed

- A generated document could cover one source only: the generator's passages were a global top-k that one source could fill. Retrieval for generators now keeps each ready source's best chunks and fills the rest by score; the prompts require every source to be covered (the mind map's root spans them), and a source the document never mentions is logged as a warning.

## [0.7.1] - 2026-10-13

### Changed

- `/healthz` answers core's `Health` (`status`, `version`, `commit` from `APP_VERSION` / `APP_COMMIT`), so the admin system view shows rag's build.

## [0.7.0] - 2026-10-13

### Added

- `POST /api/rag/outputs/voice-sample`: a short preview of a voice at a speed and in a language, synthesized once and then served from storage.

## [0.6.0] - 2026-10-13

### Added

- Citations carry their provenance: `similarity` (the chunk's score for the question), `chunk_index` and `chunk_count`; persisted messages keep them.
- `GET /api/rag/chunks/{chunk_id}`: the exact chunk behind a citation, for its owner.
- Output options: `language` for every kind, `voice` and `speed` for audio, validated per kind, stored on the output (migration `0003_rag_output_options.sql`) and returned as `options`; `GET /api/rag/outputs/options` serves the catalogue.
- `GET /api/rag/admin/stats/daily?days=30` for the admin charts: messages and outputs (total and by kind) per UTC day, every day present.

### Changed

- A citation's `quote` is the verbatim chunk text; it was cut at 300 characters before.
- `TTS_VOICE` must be one of the platform's voices; startup refuses any other.

## [0.5.0] - 2026-10-13

### Changed

- Retrieval, the output queue's gate and the generator input read sources through ingest's source library (`sources` link → `library_sources` → `chunks`); the source id in answers stays the link's.
- A generated output is indexed as a hidden `generated` library row plus its link, created in one transaction and deleted with the output.

## [0.4.0] - 2026-10-13

### Added

- Conversations: several chats per notebook (`GET`/`POST /api/rag/notebooks/{id}/conversations`, `DELETE /api/rag/conversations/{id}`, `GET /api/rag/conversations/{id}/messages`); migration `0002_rag_conversations.sql` moves existing history into `Chat 1`.
- The model sees the newest six messages of the conversation, so a follow-up question can refer to an earlier turn.

### Changed

- `POST .../ask` requires `conversation_id` (422 without it); `GET /api/rag/notebooks/{id}/messages` is removed in favour of the per-conversation list.
- Admins need credit like every user again (core's `has_credit` dropped the admin exemption); README and tests follow.

## [0.3.0] - 2026-10-13

### Added

- `GET /api/rag/admin/stats` for admins: notebooks, messages (total, last 24 h), outputs by kind and by status, jobs pending, running and failed, shared outputs.

### Changed

- The credit check is core's `has_credit`: admins have unlimited credit; everyone else still gets 402 at a zero balance.

## [0.2.1] - 2026-10-13

### Changed

- Formatting is black's (line length 100); `make lint` runs `black --check` and keeps `ruff check` for linting.

## [0.2.0] - 2026-10-13

### Added

- Audio output: an LLM-written spoken-word script, synthesized with `TTS_MODEL` and `TTS_VOICE` (default `ryan`) in sentence-boundary segments, stored as one MP3 in object storage and served as a presigned URL valid for one hour; one `tts` usage row per segment.

### Changed

- Deleting an output also deletes its stored files.

## [0.1.0] - 2026-10-12

### Added

- Core's rate-limit middleware in front of every endpoint (`ip_blocks`, `ip_bans`, `access_log` per the architecture).
- Answer generation over an OpenAI-compatible inference API with citations mapped back to source, chunk and page; markers naming no passage are stripped.
- Bearer token verification (JWT or shared demo token), notebook ownership check, 402 before any model call when the caller has no credit.
- Database migration `db/migrations/0001_rag.sql` for `messages` and `outputs`.
- FastAPI service for the `/api/rag` prefix: ask, messages, outputs, sharing, `/healthz` at the root and under the prefix.
- Fixed refusal with empty citations when no chunk passes the similarity floor; the model is not called.
- Generated documents: report, quiz, table, mind map, infographic through one `OutputGenerator` interface; audio prepared and failing clearly until text-to-speech exists.
- Indexing of a ready output as a source of kind `generated` in its notebook, so chat can cite it; `scope.output_id` on `/ask` restricts retrieval to it.
- Multi-stage Dockerfile with a non-root user; `make check`, `make run`, `make worker`, `make build`.
- One priced `usage` row per embedding and LLM call; startup refuses a configured model without a price.
- Retrieval over `chunks` with pgvector, scoped to one notebook and ready sources, with a configurable similarity floor.
- Sharing of a ready output through a revocable token and a read-only endpoint without authentication.
- Worker entrypoint `lasteverlm-rag-worker` that claims output jobs from the `outputs` table with `FOR UPDATE SKIP LOCKED`, so API pods stay stateless and workers scale on their own.
- Unit tests with mocked store, retriever and providers for citation mapping, the refusal path, credit, pricing, the output lifecycle, sharing and the generators' schemas.

### Changed

- Citations are contract-clean: grouped markers become single `[n]` markers and `n` is renumbered densely in order of first appearance after made-up markers are dropped.
- A scoped `/ask` retrieves without a similarity floor and with k=12 over its one document, so a summary-type question is answered.
- An unscoped `/ask` retrieves from the uploaded sources only; generated documents restate them and are reached through `scope.output_id`. `RETRIEVAL_INCLUDE_GENERATED=true` restores the previous behaviour.
- Generators are told to title the document by its subject and form, never with the notebook's name (the first live run produced four documents named after the notebook).
- The worker closes its connection pool on shutdown and logs every finished output.
- A model reply without content (a reasoning model that spent its budget on thinking) is a provider failure: `/ask` answers 502 and stores no message, a generation is retried by the worker; never an empty answer.
- The LLM is constructed with `LLM_MAX_TOKENS` as its default budget and called with thinking disabled (core's default) for answers and generators; the generator call-back carries core's `complete` signature.
- Generated documents are chunked with `lasteverlm_core.chunking`, the family's one chunker; the local copy is gone.
- Shared API models, bearer-token resolution, pricing, usage accounting and the provider adapters come from `lasteverlm_core` (installed by path from `../core`); the local mirror is gone.

