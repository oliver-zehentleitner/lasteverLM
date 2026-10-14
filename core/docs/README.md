# Using lasteverlm-core from a service

The README covers installation. This page is the longer form of how the
pieces fit into a FastAPI service.

## Startup

```python
from lasteverlm_core import (
    AuthSettings,
    ConfigError,
    InferenceSettings,
    load_model_prices,
    require_prices,
)

try:
    auth = AuthSettings.from_env()
    inference = InferenceSettings.from_env()
    prices = load_model_prices()
    require_prices(prices, [inference.llm_model, inference.embedding_model])
except ConfigError as exc:
    raise SystemExit(f"configuration: {exc}") from exc
```

Every loader takes an optional `env` mapping, which is how tests pass
their own environment instead of patching `os.environ`.

## Health

Every service answers `GET /healthz` (at the root for the probe, under
its prefix for the ingress) with `Health`, which also names the build:

```python
from lasteverlm_core import Health

health = Health.from_env()  # APP_VERSION, APP_COMMIT; "unknown" when unset


@app.get("/healthz", response_model=Health)
def healthz() -> Health:
    return health
```

The two values come from the ConfigMap and do not change while the
process runs, so reading them once at startup is enough. A missing value
is `"unknown"`, never an error: a probe must not fail over a label.

`dependencies` reports the state of what the service uses, only those:
`{"postgres": "ok", "storage": "down", ...}`. `DependencyProbe` runs the
checks concurrently, each bounded by 2 s (`DEPENDENCY_TIMEOUT_SECONDS`),
and answers from a 15 s cache (`DEPENDENCY_CACHE_SECONDS`), so the
liveness probe does not hammer them. A check that raises, returns `False`
or times out is `"down"` and logged as a warning; nothing raises. `status`
stays `"ok"` whatever the dependencies say: an outage must not get the
pod restarted. A service without a probe serializes no `dependencies` key.

```python
from lasteverlm_core import (
    DependencyProbe, Health, inference_check, postgres_check, storage_check,
)

probe = DependencyProbe({
    "postgres": postgres_check(lambda: pool.connection(timeout=2)),  # select 1
    "storage": storage_check(storage),             # HEAD on the bucket (S3Storage.ping)
    "inference": inference_check(client),          # GET /models, 2 s, no retry
})


@app.get("/healthz", response_model=Health)
async def healthz() -> Health:
    return Health.from_env(dependencies=await probe.states())
```

An async pool (account) uses `apostgres_check(lambda: pool.connection())`.
A sync check runs in a worker thread that is abandoned at the timeout, so
give it a client or pool timeout of its own.

## Authentication

```python
from fastapi import Depends, Header, HTTPException
from lasteverlm_core import AuthError, Principal, current_user


def principal(authorization: str | None = Header(default=None)) -> Principal:
    try:
        return current_user(authorization, auth)
    except AuthError as exc:
        raise HTTPException(401, {"error": str(exc)}) from exc
```

`Principal.scheme` is `"jwt"` or `"demo"`; a handler that must not be
reachable with the shared demo token (an admin action) checks it.

The account service issues tokens with `issue_jwt(user_id, secret)`;
the other services only verify. Both schemes may be configured at once,
so a development deployment can log in by mail and still run a recorded
demo with the token.

### Admin-only endpoints

The token carries no admin claim. Whether a caller is an admin is read
from `users.is_admin` (account's table) on every request, so granting or
revoking it takes effect at once; `is_admin(conn, user_id)` and the async
`ais_admin` do the lookup, and a user without a `users` row is no admin.
`lasteverlm_core.web.admin_only` turns it into a FastAPI dependency: it
resolves the caller with the service's own principal dependency (401
stays there) and answers 403 `admin only` unless the lookup says admin.
The service passes the lookup because only it knows its pool; a sync
lookup runs in the thread pool, an async one is awaited:

```python
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from lasteverlm_core import is_admin
from lasteverlm_core.web import admin_only


def admin_lookup(request: Request, user_id: UUID) -> bool:
    with request.app.state.pool.connection() as conn:
        return is_admin(conn, user_id)


AdminUser = Annotated[UUID, Depends(admin_only(current_user, admin_lookup))]
```

Reviewers get read-only access: `users.role` is `"user"`, `"viewer"` or
`"admin"` (`is_admin` is `role = 'admin'`). `admin_role(conn, user_id)`
and the async `aadmin_role` read it, a user without a row is `"user"`.
`admin_read(current_user, role_lookup)` admits viewers and admins and
goes on every admin endpoint that only reads; `admin_only` stays on
everything that changes something. A viewer has no other privilege:
credit and the services treat it like a user.

```python
from lasteverlm_core import Role, admin_role
from lasteverlm_core.web import admin_read


def role_lookup(request: Request, user_id: UUID) -> Role:
    with request.app.state.pool.connection() as conn:
        return admin_role(conn, user_id)


AdminReader = Annotated[UUID, Depends(admin_read(current_user, role_lookup))]
```

The module imports FastAPI, which core does not depend on (every service
does; the `web` extra names it), so it is not re-exported from the
package root.

## Accounting

Before a model call: `has_credit(conn, user_id)`, otherwise 402
`{"error": "credit exhausted"}`. It is true when `balance_cents > 0`,
for every user alike: admins top up with vouchers like everyone else, and
the admin flag gates only the admin endpoints. After the call:
`record_usage(...)` with the `Usage` the provider returned. `record_usage` prices the row and raises
`UnpricedModelError` before touching the database when the model has no
price; `require_prices` at startup makes that unreachable in a correctly
configured deployment. The caller owns the transaction. The account
service, which runs on an async pool, uses `abalance_cents`,
`ahas_credit` and `arecord_usage`; `BALANCE_SQL` and `USAGE_SQL` are exported for a report
query that needs them inline.

Speech synthesis and transcription are priced per second of audio, not
per token, as the platform bills them. A `tts` or `stt` row carries the
seconds in `tokens_in` and 0 in `tokens_out`, and `MODEL_PRICES` lists
`TTS_MODEL` and `STT_MODEL` with `in` in cents per million seconds
(`out` 0). Both providers return the usage; a billed failure carries it
on the `ProviderError`:

```python
try:
    speech = tts.synthesize(segment)  # SpeechResult(audio, usage)
except ProviderError as exc:
    if exc.usage is not None:
        record_usage(conn, user_id, notebook_id, "rag", exc.usage.kind, exc.usage.model,
                     exc.usage.tokens_in, exc.usage.tokens_out, prices=prices)
    raise
record_usage(conn, user_id, notebook_id, "rag", "tts", speech.usage.model,
             speech.usage.tokens_in, 0, prices=prices)
```

## Rate limiting

```python
from lasteverlm_core import (
    PostgresRateLimitStore,
    RateLimiter,
    RateLimitMiddleware,
    RateLimitSettings,
)

limiter = RateLimiter(PostgresRateLimitStore(pool.connection), RateLimitSettings.from_env())
app.add_middleware(RateLimitMiddleware, limiter=limiter, service="rag", auth=auth)
```

The middleware is pure ASGI and goes outermost. Per request it asks the
limiter for a decision (403 banned, 429 blocked with `Retry-After`, or
pass), lets the app answer, and writes one `access_log` row with the
response status and the user the bearer token resolved to (anonymous
when it did not). Requests to a path ending in `/healthz` are logged one
in `ACCESS_LOG_HEALTHZ_SAMPLE`.

Rules, all per client IP. With `trust_forwarded` (the default; account
names it `TRUST_PROXY_HEADERS`) that is the last `X-Forwarded-For`
entry, the one the ingress wrote; earlier entries come from the client
and are never read. Without it, the peer address. An IPv4-mapped IPv6
address counts as its IPv4 address, an IPv6 zone is dropped. The rules
count, block and ban under `limit_key(ip)`: an IPv4 address as is, an
IPv6 address as its /64 (`2001:db8:1:2::/64`), because one subscriber
holds a whole /64. `access_log`, `ip_blocks` and `ip_bans` store that
key, and `lift_ban` takes it as the admin panel shows it.

1. More than `RATE_LIMIT_PER_MINUTE` logged requests in the last minute
   (the limiter's own 429s not counted): 429 for `BLOCK_MINUTES`, one strike.
2. Strikes accumulate while each new one is within `STRIKE_WINDOW_HOURS`
   of the previous; after a quiet window the count restarts at one.
3. The `BAN_AFTER_STRIKES`th strike bans: 403 on everything until the
   admin panel calls `lift_ban(conn, ip)`, for at most `ban_max_days`
   (`BAN_MAX_DAYS`, 90): an older ban no longer refuses anything, and the purge later
   deletes it, lifted or not.

### Runtime rules

The four thresholds of rules 1 to 3 and the ban cap can be changed from
the admin panel without a deploy. The env values are the defaults; a row
in account's `settings` table with key `ratelimit`
(`RATE_LIMIT_SETTINGS_KEY`) overrides them:

```json
{"per_minute": 120, "block_minutes": 15, "ban_after_strikes": 3, "strike_window_hours": 24, "ban_max_days": 90}
```

Integers within `RATE_LIMIT_RULE_BOUNDS` (inclusive): `per_minute`
1–10000, `block_minutes` 1–1440, `ban_after_strikes` 1–100,
`strike_window_hours` 1–720, `ban_max_days` 1–365. `ban_max_days` may be
missing (a row stored before it was editable) and then comes from
`defaults`. `RateLimitRules.parse(value, defaults)` validates (raising `ValueError` that names the field); account's PUT
uses it, so the panel and the limiter accept exactly the same values.
Every process re-reads the row through its store at most every
`RULES_TTL_SECONDS` (30), so a change applies everywhere within half a
minute. Without a row the env defaults apply; an unusable row (or an
unreadable table) also means the env defaults, with one warning per
distinct problem. `RateLimiter.rules` is what is in force.
`BAN_MAX_DAYS` and `RETENTION_DAYS` govern the purge and stay env only.

`AsyncPostgresRateLimitStore` takes `AsyncConnectionPool.connection`;
`MemoryRateLimitStore` is for tests and for `make run-dev` without a
database.

Retention is not the middleware's job. The account service owns it and
runs the purge at start, on its async pool, with the exported
statements:

```python
from lasteverlm_core import PURGE_SQL, RateLimitSettings, purge_params

settings = RateLimitSettings.from_env()
params = purge_params(settings.retention_days, settings.ban_max_days)
async with conn.cursor() as cur:
    for statement in PURGE_SQL:
        await cur.execute(statement, params)
```

Every statement takes the same two parameters: `cutoff` (now minus
`RETENTION_DAYS`) for `access_log`, `ip_blocks` and lifted bans, and
`ban_cutoff` (now minus `BAN_MAX_DAYS`) for bans nobody lifted. A sync
caller uses `purge(conn, retention_days, ban_max_days)`. The purge is
cleanup only: the limiter already treats a ban past `BAN_MAX_DAYS` as
expired, so an admin list may show such a ban until the next purge.

## Providers

```python
from lasteverlm_core import OpenAICompatibleEmbedder, OpenAICompatibleLLM, make_client

client = make_client(inference)
embedder = OpenAICompatibleEmbedder(
    client, inference.embedding_model, inference.embedding_dimension
)
llm = OpenAICompatibleLLM(client, inference.llm_model, max_tokens=inference.llm_max_tokens)

result = embedder.embed(["text"])  # EmbeddingResult(vectors, usage)
answer = llm.complete(system, user, 400)  # CompletionResult(text, usage, finish_reason)
draft = llm.complete(system, user, 2000, thinking=True)
```

The embedder sends `dimensions=EMBEDDING_DIMENSION` on every call and
raises `ProviderError` when a vector of another width comes back: the
platform's embedding model is natively wider than pgvector can index,
and the OpenAI `dimensions` parameter is how it is told the width.
A vector with a NaN or infinite component, or a zero norm, is a
`ProviderError` too: pgvector's cosine distance against it is NaN, the
similarity floor would drop the chunk without a trace, and its source
would stay `ready` while nothing in it can be found.

The language model on the platform is a reasoning model. Left alone it
spends the completion budget on thinking and returns empty content when
`max_tokens` is small, so `complete` sends the thinking switch off
unless the instance or the call says `thinking=True`; `thinking_body`
is the request fragment it adds. With thinking off, an empty answer is
a `ProviderError` (its message carries the `finish_reason`); with
thinking on, the result is returned as is and the caller reads
`finish_reason` (`"length"` means the budget ran out). `max_tokens`
falls back to the instance default, `LLM_MAX_TOKENS` (1024). Reasoning
tokens are completion tokens and are billed as such through `usage`.

Both raise `ProviderError` on a failed request, and on a response that
reports no usage: a call is never accounted at zero by accident. When the
provider answered and billed the call but the answer is unusable (empty
with thinking off, a bad vector), the error carries `.usage`. Record it,
then re-raise:

```python
try:
    result = llm.complete(system, user, 400)
except ProviderError as exc:
    if exc.usage is not None:
        record_usage(conn, user_id, notebook_id, "rag", "llm", exc.usage.model,
                     exc.usage.tokens_in, exc.usage.tokens_out, prices=prices)
    raise
```

The `usage` of a result goes straight into `record_usage`. `make_client`
waits `LLM_TIMEOUT_SECONDS` (60) per request; `make_client(inference,
timeout=180)` is for a worker with long generations. Another provider is another module
implementing `Embedder` or `LLM`; callers are typed against the
protocols.

`OpenAICompatibleTextToSpeech(client, TTS_MODEL, voice=TTS_VOICE)`
returns the MP3 bytes of one `synthesize(text)` call. The voice
defaults to `DEFAULT_TTS_VOICE` (`ryan`): the platform's voices are not
the OpenAI names and `alloy` is rejected; it offers vivian, serena,
uncle_fu, dylan, eric, ryan, aiden, ono_anna and sohee.
`synthesize(text, voice=..., speed=...)` overrides the voice for one
call and sends `speed` only when given (the platform takes 0.25 to 4;
outside that range the 400 becomes a `ProviderError`). A failed request
is a `ProviderError`; the caller records the usage (see Accounting).

## Object storage

```python
from lasteverlm_core import S3Settings, S3Storage, object_key

storage = S3Storage(S3Settings.from_env())  # S3_ENDPOINT, S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY
key = object_key(notebook_id, output_id, "audio.mp3")
storage.put(key, mp3, "audio/mpeg")
url = storage.presigned_get(key)  # valid PRESIGN_SECONDS (3600); expires_s= up to 7 days
link = storage.presigned_get(key, download_name="Vortrag Müller.m4a")  # saved, not played
storage.delete(key)
```

`object_key` takes two UUIDs and one plain name segment (no `/` or `\`,
not `.` or `..`, no control characters, at most 255 characters) and
raises `ValueError` otherwise, so a key never leaves its owner's prefix.
One bucket for the family; keys are `<user_id>/<library_source_id>/<name>`
for an upload (a source lives in the user's library) and `<notebook_id>/<output_id>/<name>` for a file an
output produces. `get` returns the bytes, `delete(*keys)` takes one or more
keys (sent 1000 per request); a key the store refuses to delete is a
`StorageError` naming it and its code, after every batch was sent. `exists(key)` is a HEAD request: `False`
only when the key is missing, a `StorageError` for anything else (no
access, no bucket, endpoint down), so a cache check in front of a paid
generation never mistakes a broken bucket for an empty one. A presigned GET is how a browser fetches
a private object without a bearer header; it is signed with SigV4 and
needs no request to storage. With `download_name` the URL also signs
`response-content-disposition` (`attachment_disposition`: an ASCII
`filename` fallback and the exact name as `filename*`), so the browser
saves the file under that name; a link's `download` attribute would be
ignored, because the bucket is another origin. A name that is empty or
holds a control character is a `ValueError`. `S3_REGION` is optional (`us-east-1`).
Every failure is a `StorageError`. The client is synchronous (boto3);
an async service calls it through `anyio.to_thread.run_sync`.
`Storage` is the protocol callers are typed against; tests pass a stub
client to `S3Storage(settings, client=...)`.

## Migrations

`db/migrations/0001_core_usage.sql`, `0002_core_rate_limit.sql` and
`0003_core_usage_tts.sql` (adds `tts` to `usage.kind`) are plain SQL, one transaction each, applied by infra's `make migrate` after
infra's own migration and before account, ingest and rag. They create no
extension and reference no table of another service, because none exists
yet at that point in the order.
