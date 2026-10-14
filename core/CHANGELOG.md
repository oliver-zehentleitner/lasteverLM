# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Keep a Changelog records what changed; [Keep the Why](https://keepthewhy.com)
preserves why it changed, in `context/`.

## [Unreleased]

### Changed

- The ban cap is a runtime rule: `RateLimitRules` gains `ban_max_days` (bounds 1–365 in `RATE_LIMIT_RULE_BOUNDS`, default `BAN_MAX_DAYS`), and the limiter ends a ban after the current rules' value instead of the env setting. `RateLimitRules.parse(value, defaults=None)` accepts a row without `ban_max_days`, as stored before, and takes it from `defaults`. `BAN_MAX_DAYS` above 365 is now a `ConfigError` at startup.

## [0.17.0] - 2026-10-14

### Added

- `InferenceSettings.judge_models` (`JUDGE_MODELS`): JSON mapping a judge level (`JudgeLevel`, `JUDGE_LEVELS`: `low`, `medium`, `high`) to the model that checks an answer, merged over `DEFAULT_JUDGE_MODELS` (`glm-5.3-flash`, `deepseek-v4.1-flash`, `glm-5.3`) level by level by `parse_judge_models`. An unknown level, a blank model or malformed JSON is a `ConfigError` at startup. The service that runs the judge checks the prices with `require_prices`. A judge call is billed as usage kind `llm` under the judge model.

### Changed

- `OutputKind` gains `chart`. The proposal is an ordinary `llm` call, so there is no new usage kind; rag owns the outputs kind constraint.

## [0.16.0] - 2026-10-14

### Changed

- `SourceStatus` gains `waiting_for_credit`: an API value, never stored, for a queued source whose owner has no credit (adding a source needs none, processing does).

## [0.15.0] - 2026-10-14

### Added

- Decision model: `DecisionModel` and `OpenAICompatibleDecision(client, model).decide(state, questions) -> DecisionResult(answers, usage)` over the platform's `/systemone` (sent through the OpenAI client's generic `post`, so key, timeout, retries and error mapping are shared; a 422 is `permanent`). `validate_decision_questions` refuses before the call what the platform would refuse: 1 to 64 questions, type `noul`, `choice` (1 to 255 options) or `score` (1 to 10 levels). Usage kind `decision` (migration `0005_core_usage_decision.sql`) is billed per input token as reported in `usage.input_tokens`; a response without it, or one that misses a question or answers it as another type, is a `ProviderError`. `InferenceSettings.decision_model` (`DECISION_MODEL`, default `clef`, `DEFAULT_DECISION_MODEL`). `OutputKind` gains `decision`.

## [0.14.0] - 2026-10-14

### Added

- `Message.units` / `Answer.units: list[dict[str, Any]] | None` and `Message.check` / `Answer.check: dict[str, Any] | None`, default `None`: the support check rag runs on a chat answer (per paragraph its refs and support, plus method, threshold and counts), shaped as on outputs. rag owns the shapes; `None` for answers without citations and older messages.
- `Output.provenance` and `SharedOutput.provenance: dict[str, Any] | None = None`: the generation log rag stores with an output (`outputs.provenance`). rag owns and documents its shape and strips the ids before a shared output is returned; `None` for outputs made before it.
- `Health.dependencies`, `{"postgres"?, "storage"?, "inference"?: "ok" | "down"}` (`Dependency`, `DependencyState`), omitted from the body while empty, and `lasteverlm_core.health`: `DependencyProbe` runs the checks concurrently with 2 s each, caches the states for 15 s and reports `"down"` (logged) instead of raising; `postgres_check` / `apostgres_check` (`select 1`), `storage_check` over the new `S3Storage.ping` (bucket HEAD), `inference_check` (`GET /models`, no retry). `status` stays `"ok"`.

## [0.13.0] - 2026-10-14

### Changed

- `Output` for outputs in the user's library: `notebook_id` is `UUID | None` (`None` once the origin notebook is deleted; the output survives it), new `origin_title: str = ""` (the origin notebook's title, kept), and `deleted_at` / `purge_at: datetime | None = None` (set while the output is in the trash). The owner is not in the model; it is always the caller. `SharedOutput` is unchanged.

## [0.12.0] - 2026-10-14

### Fixed

- `S3Storage.delete` reads the `Errors` list of `delete_objects`, which answers 200 even when single keys fail, and raises a `StorageError` naming the failed keys and their codes (at most ten, then a count) after every batch was sent. A refused delete (`AccessDenied` on one key) used to pass silently. A missing key (`NoSuchKey`) is still no error.

## [0.11.1] - 2026-10-14

### Fixed

- Text to speech is billed per second of generated speech, as the platform charges it, instead of per input character (about 15 times too much). `synthesize` returns `SpeechResult(audio, usage)`, the usage (kind `tts`, seconds in `tokens_in`) read from the `x-usage-seconds` response header and rounded up. A response without a readable header is a `ProviderError` carrying the duration measured from the MP3 (`measure_audio`); empty audio is one carrying the reported usage. `MODEL_PRICES` gives `in` for a `tts` model in cents per million seconds. Breaking: callers take `.audio` and record `.usage`.

## [0.11.0] - 2026-10-14

### Added

- `presigned_get(key, expires_s, *, download_name=None)` on `Storage` and `S3Storage`: with a name, the URL signs `response-content-disposition` as an attachment (`attachment_disposition`: ASCII `filename` fallback plus the exact UTF-8 name as `filename*`), so a cross-origin link downloads under that name. Without it, nothing changes. An empty name or one with control characters is a `ValueError`.
- Speech to text for audio sources: `SpeechToText` and `OpenAICompatibleSpeechToText(client, model).transcribe(audio, filename)` returning `TranscriptionResult` (`text`, `usage`, `seconds`), multipart against `/audio/transcriptions` with `response_format=json`. Usage kind `stt` (`UsageKind`, migration `0004_core_usage_stt.sql`) is billed in seconds of audio, rounded up per call, from the duration the platform reports; a response without it is a `ProviderError` carrying the measured seconds, an empty transcript one carrying the reported usage. `MODEL_PRICES` prices it with `in` in cents per million seconds. `InferenceSettings.stt_model` (`STT_MODEL`, default `whisper-large-v3-turbo`, `DEFAULT_STT_MODEL`). `SourceKind` gains `audio`.
- `lasteverlm_core.audio`: `probe_audio` detects WAV or MP3 from the bytes, measures the duration from the file and raises `AudioError` (a `ValueError`, message meant for the user) for any other format, above 25 MiB (`MAX_AUDIO_BYTES`) or above four hours (`MAX_AUDIO_SECONDS`); `transcribe` calls it before any request. `billed_seconds` rounds up.
- `ProviderError.permanent`: `True` when the platform refused a request with a 4xx a retry will not change (all except 408, 409, 429), for every provider call, so a worker fails the job once instead of paying for further attempts. 5xx, timeouts, connection errors and unusable answers stay `False`.

## [0.10.0] - 2026-10-14

### Added

- Read-only admin access: `Role` (`"user" | "viewer" | "admin"`, `ROLES`), `ROLE_SQL`, `admin_role` / `aadmin_role` reading account's `users.role` (no row is `"user"`), and `lasteverlm_core.web.admin_read(current_user, role_lookup)`, which admits viewers and admins with 403 `admin only` for everyone else. `admin_only` is unchanged and stays admin-only for writes.

### Security

- `AuthSettings` refuses a `jwt_secret` shorter than 32 bytes (`MIN_JWT_SECRET_BYTES`, the HS256 key length) with `ConfigError`, in the dataclass itself so that account, which builds its settings without `from_env`, is covered too.
- The rate limiter keys IPv6 clients by their /64 (`limit_key`, e.g. `2001:db8:1:2::/64`) for the window, blocks and bans; IPv4 stays per address. A client could otherwise step out of every block and ban by picking another address in its own /64. `access_log`, `ip_blocks` and `ip_bans` hold the key, and `lift_ban` takes it as shown.
- `object_key` requires UUID ids and one plain name segment (no `/` or `\`, not `.` or `..`, no control characters, at most 255 characters) and raises `ValueError` otherwise, so no caller can build a key outside its owner's prefix.
- `presigned_get` refuses an expiry above seven days (`PRESIGN_MAX_SECONDS`, the SigV4 limit) as well as one below a second.
- `UsageRecord` caps `tokens_in`, `tokens_out` and `cost_cents` at the `integer` columns' maximum, so an absurd price or count fails as a named `ValidationError` before the insert.
- `client_ip` takes the last `X-Forwarded-For` entry (the one the ingress wrote), not the first, which the client controls: a forged header could escape blocks and bans and get another address banned. Repeated header lines count as one list. IPv4-mapped IPv6 addresses are keyed as IPv4, an IPv6 zone is dropped.
- `RateLimitSettings.from_env` holds the four rule thresholds to `RATE_LIMIT_RULE_BOUNDS` and `RETENTION_DAYS`, `BAN_MAX_DAYS`, `ACCESS_LOG_HEALTHZ_SAMPLE` to at least 1, failing with `ConfigError` at startup (`RATE_LIMIT_PER_MINUTE=0` blocked every request).

### Changed

- A provider response without `usage` is a `ProviderError` instead of a call accounted at zero tokens. `ProviderError` carries `.usage` when the provider billed an unusable answer (empty with thinking off, a rejected vector, a wrong count); callers record it, then re-raise.
- `make_client` takes its timeout from `InferenceSettings.llm_timeout_seconds` (`LLM_TIMEOUT_SECONDS`, default 60, at least 1) unless `timeout=` is passed.
- `load_model_prices` lives in `pricing.py` next to what it parses; the package-root import is unchanged, `lasteverlm_core.settings.load_model_prices` is gone (no service imported it from there). It removes the late import with `noqa: E402` that settings needed to reach pricing.
- README environment table lists `S3_*` and `APP_VERSION` / `APP_COMMIT`, which it had missed.
- Docstrings on the public classes that had none (results, provider and storage protocols, the row models, `make_client`); the test's last `type: ignore` replaced by real ASGI callables.

### Fixed

- `OpenAICompatibleTextToSpeech.synthesize` raises `ProviderError` on an empty audio body instead of returning zero bytes.
- `InferenceSettings.from_env` raises `ConfigError` for `LLM_MAX_TOKENS` or `EMBEDDING_DIMENSION` below 1.

## [0.7.3] - 2026-10-13

### Changed

- `OpenAICompatibleEmbedder.embed` raises `ProviderError` when a vector has a non-finite component (NaN, inf) or a zero norm, naming the input index: pgvector's cosine distance against such a vector is NaN, so the chunk was dropped silently and its source stayed `ready` while unsearchable.

## [0.7.0] - 2026-10-13

### Added

- `Health` (`status`, `version`, `commit`) with `Health.from_env()`: the `/healthz` body reporting the build from `APP_VERSION` and `APP_COMMIT`, each `"unknown"` (`UNKNOWN_BUILD`) when unset or blank.
- `Storage.exists(key)` / `S3Storage.exists`: a HEAD on the key; `False` only for a missing key (`404`, `NoSuchKey`, `NotFound`), every other failure a `StorageError`, so a broken bucket never reads as "not cached yet".

## [0.4.0] - 2026-10-13

### Added

- `Output` and `SharedOutput` gain `options` (default `{}`): the resolved generation options (language, voice, speed) rag stores in `outputs.options`; older outputs validate with `{}`.
- `TextToSpeech.synthesize(text, *, voice=None, speed=None)`: `OpenAICompatibleTextToSpeech` uses the call's voice or the constructor's and sends `speed` only when given (platform range 0.25 to 4). Calls with the text alone work as before.
- Runtime rate-limit rules: the limiter overrides `per_minute`, `block_minutes`, `ban_after_strikes` and `strike_window_hours` from account's `settings` row `ratelimit` (jsonb), re-read at most every 30 s per process; env values stay the defaults, an unusable row falls back to them with one warning. `RateLimitRules` (`parse`, `from_settings`, `as_json`), `RATE_LIMIT_SETTINGS_KEY`, `RATE_LIMIT_RULE_BOUNDS`, `RULES_TTL_SECONDS`, `RULES_SQL`; `RateLimiter.rules` and `current_rules`; stores gain `rule_overrides`.
- `Citation` gains the optional `similarity` (cosine similarity to the question, 3 decimals), `chunk_index` (0-based `chunks.idx`) and `chunk_count` (chunks of the source); messages stored without them still validate. `quote` is documented as the verbatim chunk text, never trimmed.
- `ChunkView`: one chunk with source name, kind, page, `chunk_index`, `chunk_count` and full `text`, the response of rag's `GET /api/rag/chunks/{chunk_id}`.

## [0.3.1] - 2026-10-13

### Changed

- `has_credit` / `ahas_credit` no longer exempt admins: the check is `balance_cents > 0` for every user. `is_admin`, `ais_admin` and `admin_only` are unchanged and gate only the admin endpoints.

## [0.3.0] - 2026-10-13

### Added

- `has_credit` / `ahas_credit`: the pre-call check, true for an admin whatever the balance and for anyone else with `balance_cents > 0`; replaces `balance_cents(...) <= 0` in the services. Admins are still billed.
- `is_admin` / `ais_admin`: `users.is_admin` per request (no JWT claim); a user without a row is no admin. `HAS_CREDIT_SQL`, `IS_ADMIN_SQL` exported.
- `lasteverlm_core.web.admin_only(current_user, lookup)`: FastAPI dependency for admin-only endpoints, 403 `admin only` for non-admins; the `web` extra names FastAPI.

## [0.2.1] - 2026-10-13

### Changed

- black is the formatter (`[tool.black]`, line length 100, `black --check` in `make lint`); ruff stays the linter. No code changed: ruff's format output was already black-compatible.

## [0.2.0] - 2026-10-13

### Added

- `lasteverlm_core.storage`: `S3Storage` with put, get, delete and `presigned_get(key, expires_s=3600)` (SigV4), `S3Settings.from_env`, `object_key`, the `Storage` protocol; moved from ingest so that ingest and rag share one S3 client. New dependency `boto3`.
- `purge_params(retention_days, ban_max_days)` and the package-level export of `PURGE_SQL`, so an async caller runs the purge statements with the same parameters as `purge`.
- Usage kind `tts`, priced per input character (`tokens_in` = characters, `tokens_out` = 0); migration `0003_core_usage_tts.sql` widens the check on `usage.kind`.

### Changed

- `OpenAICompatibleTextToSpeech` defaults to the platform voice `ryan` (`DEFAULT_TTS_VOICE`); the OpenAI name `alloy` is rejected by the platform.
- An active ban holds until an admin lifts it, for at most `BAN_MAX_DAYS` (default 90): the limiter treats an older ban as expired and the purge deletes it, lifted or not. `PURGE_SQL` takes `cutoff` and `ban_cutoff`; `purge` gains `ban_max_days`. The store methods take the limiter's clock: `is_banned(ip, since)`, `ban(ip, reason, now)`.

## [0.1.0] - 2026-10-12

### Added

- API models of the contract v1 as Pydantic models with `UUID` ids and `datetime` timestamps; kind and status literals; `DEMO_USER_ID`.
- Bearer-token resolution `current_user`: session JWT (HS256, issuer `lasteverlm`) first, demo token second, both schemes configurable at once; `issue_jwt`, `verify_jwt`, `AuthSettings.from_env`.
- Environment helpers failing loud with `ConfigError`; `InferenceSettings.from_env`, `load_model_prices`.
- Migrations `0001_core_usage.sql` (`usage`) and `0002_core_rate_limit.sql` (`access_log`, `ip_blocks`, `ip_bans`).
- Model prices: `parse_model_prices`, `cost_cents` with exact decimal arithmetic, `require_prices`; an unpriced model raises `UnpricedModelError`.
- Provider interfaces `Embedder`, `LLM`, `TextToSpeech` reporting token usage, with OpenAI-compatible implementations and `make_client`.
- `TokenWindowChunker` with `Passage` and `Chunk`, the one chunker for parsed sources and generated documents.
- Rate limiting per client IP: `RateLimitMiddleware` (pure ASGI) over `RateLimiter` rules, stores for sync and async psycopg and in memory, `purge`, `lift_ban`, settings from the environment.
- Usage accounting `record_usage` and `balance_cents`, sync and async, over the caller's psycopg connection; the SQL exported as constants.

### Changed

- Chunker defaults are 420 regex tokens with 50 overlap (were 800/100): measured on a German PDF, the embedding model counts about 1.9 tokens per regex token, so the old window was about 1540 model tokens.
- `LLM.complete` takes `max_tokens` optionally (default `LLM_MAX_TOKENS`, 1024) and a `thinking` switch; reasoning is off by default and an empty answer without thinking is a `ProviderError`. `CompletionResult` carries `finish_reason`.
- The embedder requests `EMBEDDING_DIMENSION` from the provider on every call through the `dimensions` parameter.
- The OpenAI SDK dependency is `openai>=3`; provider tests run the SDK against a fake httpx2 transport.

### Fixed

- `make test-postgres`: the SQL paths against a throwaway pgvector container (`tests/test_postgres.py`), skipped without Docker.
- Scalar reads (`balance_cents`, the rate-limit store queries) work on connections with a `dict_row` row factory; the connection protocols accept any psycopg row type.

