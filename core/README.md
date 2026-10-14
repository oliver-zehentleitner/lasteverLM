# lasteverLM-core

[![ci](https://github.com/oliver-zehentleitner/lasteverLM/actions/workflows/ci.yml/badge.svg)](https://github.com/oliver-zehentleitner/lasteverLM/actions/workflows/ci.yml)
[![ktw-lint](https://github.com/oliver-zehentleitner/lasteverLM/actions/workflows/ktw-lint.yml/badge.svg)](https://github.com/oliver-zehentleitner/lasteverLM/actions/workflows/ktw-lint.yml)
[![Keep the Why](https://keepthewhy.com/assets/badge.svg)](https://keepthewhy.com)

The shared Python library of [lasteverLM](https://github.com/oliver-zehentleitner/lasteverLM),
a NotebookLM-style assistant. The account, ingest and rag services import
it; it runs no service of its own. It holds the one definition of what
the three services share:

| Module | What |
|---|---|
| `lasteverlm_core.models` | the API models of the contract (`User`, `Notebook`, `Source`, `Citation`, `ChunkView`, `Message`, `Answer`, `Output`, `UsageRecord`, `Health` (the `/healthz` body with `APP_VERSION` / `APP_COMMIT` and the `dependencies` states), the kind and status literals (`usage.kind` includes `tts` and `stt`, `SourceKind` includes `audio`), `DEMO_USER_ID`) |
| `lasteverlm_core.auth` | `current_user(authorization, auth) -> Principal`: session JWT first, demo token second; `issue_jwt`, `verify_jwt`, `AuthSettings.from_env` |
| `lasteverlm_core.pricing` | `load_model_prices` (`MODEL_PRICES` over the defaults), parsing, `cost_cents`, `require_prices`; an unpriced model is a configuration error, never a free call; the unit per usage kind (tokens; seconds of audio for `tts` and `stt`) is in the module docstring |
| `lasteverlm_core.usage` | `record_usage`, `balance_cents`, `has_credit` (balance above zero, admins included), `is_admin`, `admin_role` (`user`, `viewer` or `admin`) (sync psycopg) and their async twins `arecord_usage`, `abalance_cents`, `ahas_credit`, `ais_admin`, `aadmin_role`; the SQL as constants |
| `lasteverlm_core.web` | `admin_only(current_user, lookup)` (admins, for endpoints that change something) and `admin_read(current_user, role_lookup)` (viewers and admins, for endpoints that only read): FastAPI dependencies answering 403 `admin only`; need FastAPI (the `web` extra), not re-exported from the root |
| `lasteverlm_core.ratelimit` | `RateLimitMiddleware` with the block and ban rules (per IPv4 address or IPv6 /64, `limit_key`), three stores (PostgreSQL sync, async, memory), `purge` with `PURGE_SQL` and `purge_params`, `lift_ban`; a ban holds until lifted, at most `ban_max_days`; the four thresholds and the ban cap overridable at runtime from account's `settings` row `ratelimit` (`RateLimitRules`, `RATE_LIMIT_RULE_BOUNDS`) |
| `lasteverlm_core.chunking` | `TokenWindowChunker` with `Passage` and `Chunk`: the token windows both ingest (sources) and rag (generated documents) embed; defaults 420/50 regex tokens, about 800 model tokens for German |
| `lasteverlm_core.providers` | `Embedder`, `LLM`, `TextToSpeech`, `SpeechToText`, `DecisionModel` interfaces and their OpenAI-compatible implementations; embeddings request `EMBEDDING_DIMENSION`, completions run with reasoning off unless asked (`thinking=True`); speech with the platform voice `ryan` by default, `synthesize` returning a `SpeechResult` (MP3 bytes, `tts` usage in seconds from `x-usage-seconds`); `OpenAICompatibleSpeechToText.transcribe(audio, filename)` returns a `TranscriptionResult` (text, `stt` usage in seconds, measured duration); `OpenAICompatibleDecision.decide(state, questions)` returns a `DecisionResult` (one answer per question id from the platform's `/systemone`, `decision` usage in input tokens), questions checked by `validate_decision_questions` first |
| `lasteverlm_core.health` | `DependencyProbe` (the `/healthz` dependency states: concurrent, 2 s per check, cached 15 s, `"down"` instead of raising) with `postgres_check` / `apostgres_check`, `storage_check` (`S3Storage.ping`, a bucket HEAD), `inference_check` (`GET /models`) |
| `lasteverlm_core.audio` | `probe_audio`: WAV or MP3 detected from the bytes, duration measured from the file, `AudioError` above 25 MiB (`MAX_AUDIO_BYTES`) or four hours (`MAX_AUDIO_SECONDS`) and for any other format; `measure_audio` (format and duration without the limits); `billed_seconds` |
| `lasteverlm_core.storage` | `S3Storage` (put, get, exists, delete, presigned GET with an expiry, default one hour) over `S3Settings.from_env`, `object_key`; the family's only S3 code |
| `lasteverlm_core.settings` | `env_required`, `env_optional`, `env_int`, `InferenceSettings.from_env` (with `stt_model`); every miss is a `ConfigError` at startup |
| `db/migrations/` | `usage` (kinds `llm`, `embedding`, `tts`, `stt`, `decision`), `access_log`, `ip_blocks`, `ip_bans`; applied by infra after its own migrations and before the services' |

The contract these implement is the family's
[architecture](https://github.com/oliver-zehentleitner/lasteverLM/blob/main/docs/architecture.md);
a change to it is a pull request there first.

- How to use it from a service, in detail: [`docs/README.md`](docs/README.md)
- Why it is built this way: [`context/index.md`](context/index.md)
- Family rules and standards: the root [AGENTS.md](https://github.com/oliver-zehentleitner/lasteverLM/blob/main/AGENTS.md) and [repository standard](https://github.com/oliver-zehentleitner/lasteverLM/blob/main/docs/repository-standard.md)

## Using it from a service

Core lives in `core/` of the same repository. A service installs it by
path, from its own directory, and builds its image with the repository
root as context so that `core/` can be copied in.

`<service>/Makefile`:

```make
venv:
	uv venv $(VENV)
	VIRTUAL_ENV=$(VENV) uv pip install -e ../core -e '.[dev]'
```

`<service>/pyproject.toml` lists `lasteverlm-core` like any other
dependency (the name resolves to `../core` through the install line
above; nothing is published to PyPI):

```toml
dependencies = ["lasteverlm-core", "fastapi>=0.115", ...]
```

`<service>/Dockerfile`, built as `docker build -f <service>/Dockerfile .`
from the repository root:

```dockerfile
FROM python:3.13-slim AS build
WORKDIR /app
COPY core ./core
COPY <service> ./<service>
RUN pip install --no-cache-dir --prefix=/install ./core ./<service>
```

Then, in the service:

```python
from lasteverlm_core import (
    AuthError,
    AuthSettings,
    InferenceSettings,
    RateLimiter,
    RateLimitMiddleware,
    PostgresRateLimitStore,
    RateLimitSettings,
    current_user,
    load_model_prices,
    record_usage,
    balance_cents,
    require_prices,
)

auth = AuthSettings.from_env()  # AUTH_JWT_SECRET and/or DEMO_TOKEN
inference = InferenceSettings.from_env()  # INFERENCE_*, LLM_MODEL, EMBEDDING_*
prices = load_model_prices()  # MODEL_PRICES over the defaults
require_prices(prices, [inference.llm_model, inference.embedding_model])  # fail loud

app.add_middleware(
    RateLimitMiddleware,
    limiter=RateLimiter(PostgresRateLimitStore(pool.connection), RateLimitSettings.from_env()),
    service="ingest",
    auth=auth,
)

principal = current_user(request.headers.get("authorization"), auth)  # AuthError -> 401
if balance_cents(conn, principal.user_id) <= 0:
    ...  # 402 credit exhausted
record_usage(
    conn,
    principal.user_id,
    notebook_id,
    "ingest",
    "embedding",
    result.usage.model,
    result.usage.tokens_in,
    result.usage.tokens_out,
    prices=prices,
)
```

## Configuration

Core reads nothing on import. The loaders below are called by the
services at startup and raise `ConfigError` naming the first missing or
invalid value.

| Variable | Loader | Meaning |
|---|---|---|
| `AUTH_JWT_SECRET` | `AuthSettings.from_env` | HS256 secret the account service signs session tokens with; verified by every service; at least 32 bytes (`MIN_JWT_SECRET_BYTES`), shorter is a `ConfigError` |
| `DEMO_TOKEN` | `AuthSettings.from_env` | shared demo token resolving to the demo user; unset in production. At least one of the two is required |
| `INFERENCE_BASE_URL`, `INFERENCE_API_KEY` | `InferenceSettings.from_env` | OpenAI-compatible API; the URL defaults to Vultr Serverless Inference |
| `LLM_MODEL`, `LLM_MAX_TOKENS` | `InferenceSettings.from_env` | the language model and the default completion budget (1024); below 1 a `ConfigError` |
| `LLM_TIMEOUT_SECONDS` | `InferenceSettings.from_env` | seconds per inference request for `make_client` (default 60); a caller with long generations passes `timeout=` |
| `EMBEDDING_MODEL`, `EMBEDDING_DIMENSION` | `InferenceSettings.from_env` | the embedding model and the vector width requested from it on every call (default 1536; the deployment uses 1024, below pgvector's 2000-dimension index limit); must match `chunks.embedding`; below 1 a `ConfigError` |
| `DECISION_MODEL` | `InferenceSettings.from_env` | the decision model for Decision outputs (default `clef`; `clef-flash` answers at half the price per token); needs a `MODEL_PRICES` entry with `in` in cents per million input tokens |
| `JUDGE_MODELS` | `InferenceSettings.from_env` | JSON `{"<level>": "<model>"}` for the opt-in answer judge, levels `low`, `medium`, `high`, merged over `DEFAULT_JUDGE_MODELS` (`glm-5.3-flash`, `deepseek-v4.1-flash`, `glm-5.3`); an unknown level or a blank model is a `ConfigError`; the service running the judge needs a `MODEL_PRICES` entry for every model (usage kind `llm`) |
| `STT_MODEL` | `InferenceSettings.from_env` | the transcription model for audio sources (default `whisper-large-v3-turbo`); needs a `MODEL_PRICES` entry with `in` in cents per million seconds |
| `MODEL_PRICES` | `load_model_prices` | JSON `{"<model>": {"in": <cents per million tokens>, "out": ...}}` (for `tts` and `stt` models per million seconds), merged over the defaults in `pricing.py` (empty until the platform's models are known, so every deployment sets it) |
| `RATE_LIMIT_PER_MINUTE`, `BLOCK_MINUTES`, `BAN_AFTER_STRIKES` | `RateLimitSettings.from_env` | defaults 120, 15, 3; with `STRIKE_WINDOW_HOURS` the defaults of the runtime rules the admin panel can override; outside the panel's bounds a `ConfigError` at startup, as is a `RETENTION_DAYS` or `ACCESS_LOG_HEALTHZ_SAMPLE` below 1 |
| `STRIKE_WINDOW_HOURS`, `ACCESS_LOG_HEALTHZ_SAMPLE`, `RETENTION_DAYS` | `RateLimitSettings.from_env` | defaults 24, 100 (one `/healthz` row in a hundred is logged), 30 |
| `BAN_MAX_DAYS` | `RateLimitSettings.from_env` | default 90, bounds 1–365; a ban older than this has expired, and the purge deletes it; the default of the runtime rule `ban_max_days` |
| `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | `S3Settings.from_env` | the object store; all four required where storage is used |
| `S3_REGION` | `S3Settings.from_env` | default `us-east-1` |
| `APP_VERSION`, `APP_COMMIT` | `Health.from_env` | the build `/healthz` reports; `"unknown"` when unset |

## Run locally

Core is a library; there is nothing to run. `make run` says so.

## Test

```bash
make install   # uv sync --all-extras: creates .venv with the dev extras; idempotent
make check     # install, then ruff, black --check, mypy --strict, pytest
```

`make check` runs `install` first, so it works on a fresh checkout and
is what CI runs.

```bash
make test-postgres   # the SQL paths against a throwaway pgvector container
```

`make test-postgres` starts `pgvector/pgvector:pg17` on port 5433 (via
`sg docker -c`), runs `tests/test_postgres.py` against it (usage
accounting, the rate-limit stores, purge, lift_ban, with tuple and
dict-row connections, sync and async) and stops the container. It is
skipped when Docker is not available and is not part of `make check`.
Variables: `PG_IMAGE`, `PG_PORT`, `DOCKER` (the command prefix).

`make check` needs no database, network or inference key: the
PostgreSQL helpers are tested against fake psycopg-shaped connections,
the middleware against the in-memory store, the providers against a fake
client. The real SQL is covered by `make test-postgres`.

## Build and deploy

Core ships inside the service images (see "Using it from a service");
there is no core image. `make build` produces the wheel and sdist in
`dist/` for anyone who wants the package on its own. Its migrations are
applied by infra's `make migrate` in the order infra, core, account,
ingest, rag.

## Versioning

Core has no tag of its own: one SemVer tag on the repository releases
everything at once, and services inside one checkout always use the
core next to them. `lasteverlm_core.__version__` follows the
repository's version.
