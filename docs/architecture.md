# Architecture

Status: v2, the contract the services are built against. A change to a
contract below is a pull request here first, and the owner of every
affected service is told.

## Overview

Everything runs on one cloud account and lives in one repository. Four
application services, two workers and one static frontend on a managed
Kubernetes cluster, one managed PostgreSQL with pgvector, one object
storage bucket, one inference API for the language model and the
embeddings. Every container is horizontally scalable; PostgreSQL is the
only state.

```text
browser ── web (static SPA) ────────────┐
                                        ▼
                         ingress-nginx (TLS, path routing)
                 │                │                │
          /api/account      /api/ingest        /api/rag
                 ▼                ▼                ▼
             account           ingest            rag
                 │                │                │
                 └────── Vultr Managed PostgreSQL + pgvector ──────┘
                                  │                │
                         Vultr Object Storage   Vultr Serverless Inference
                         (uploads, S3 API)      (OpenAI-compatible API)
```

| Part | Directory | Owner | Role |
|---|---|---|---|
| core | `core/` | Agent G | Python library: models, settings, auth, rate-limit middleware, provider interfaces, usage accounting. Copied into the images of account, ingest, rag. Never published as a package. |
| account | `account/` | Agent F | magic-link login, allowed domains, vouchers, credit balance, admin panel backend, visitor and access logs, blocks and bans |
| ingest | `ingest/` | Agent C | notebooks and sources: upload endpoints; the ingest worker (`lasteverlm-ingest-worker`) parses, chunks, embeds, summarizes |
| rag | `rag/` | Agent D | retrieval, cited answers, message history; the rag worker (`lasteverlm-rag-worker`) generates outputs |
| web | `web/` | Agent E | wizard, two-column notebook view, viewer, login, credits, admin panel |
| infra | `infra/` | Agent B | cluster provisioning, Helm chart, ingress, migrations runner, deploy automation, smoke test |

One repository, isolated contexts: every directory above is its own
Keep the Why project with its own `context/`, `parent: ..`, and the
root holds what is family-wide.

Rules that hold everywhere:

- **Services share the database, never each other's APIs.** The schema is the contract between them. There are no service-to-service HTTP calls.
- **Every stage is an interface with one implementation.** New source kinds, chunkers, embedding or LLM providers, retrievers are added as modules, never by editing callers.
- **Configuration from the environment only.** A service fails at startup when a required value is missing. Secrets never enter a repository.
- **Every model call is accounted.** One `usage` row per LLM or embedding call, priced at write time. Zero balance means 402, for admins as well.
- **Nothing is shipped but containers.** Python source goes into the image; no PyPI, no conda, no compiled artifacts. One image per service, tagged with the Git SHA and the release tag, in GitHub's container registry.
- **Every process can run any number of times.** API pods hold no state; background work is claimed from PostgreSQL by worker pods; uploads live in object storage; sessions are JWTs.

## Platform (Vultr)

| Need | Service | Notes |
|---|---|---|
| Cluster | Vultr Kubernetes Engine | one namespace `lasteverlm`, ingress-nginx, cert-manager with Let's Encrypt |
| Database, vectors | Vultr Managed PostgreSQL | extension is named `vector`: `CREATE EXTENSION IF NOT EXISTS vector;` |
| Uploads | Vultr Object Storage | S3-compatible, one bucket, accessed with boto3 |
| LLM, embeddings | Vultr Serverless Inference | OpenAI-compatible, base URL `https://api.vultrinference.com/v1`, used through the OpenAI SDK so any compatible provider can replace it |
| Speech | Vultr Serverless Inference | text-to-speech for audio outputs and speech-to-text for audio sources (`/v1/audio/speech`, `/v1/audio/transcriptions`); the platform runs both speech models in the United States only (TTS in atl, STT in sjc), so the text of an audio output and the bytes of an audio source leave the EU for that call, while documents, chunks and vectors stay in the Amsterdam region |
| Mail | external SMTP | configured through the environment (`SMTP_*`, Kubernetes Secret `lasteverlm-smtp`), shown read-only in the admin panel; not a Vultr service |

## Data model (PostgreSQL)

All ids are `uuid`. All timestamps are `timestamptz default now()`.
Migrations are numbered SQL files, each service keeps the ones for its
own tables in `db/migrations/`, and infra applies all of them in one
`make migrate`. Plain SQL, no ORM-specific migration tooling.

```sql
-- account
users        (id, email unique, is_admin bool, created_at, last_login_at)
magic_links  (id, user_email, token_hash, expires_at, used_at, created_at)
vouchers     (id, code_hash unique, amount_cents, created_by, redeemed_by, redeemed_at, created_at)
ledger       (id, user_id, amount_cents, reason, voucher_id, created_at)      -- credits only, positive
settings     (key primary key, value jsonb, updated_at)                       -- allowed_domains
login_log    (id, user_id, at)
visit_log    (id, at, path, user_id null, ip_hash, ua_family)                 -- no raw IP, 30 days

-- ingest
notebooks    (id, owner uuid, title, created_at)
library_sources (id, owner uuid, name, kind, dedupe_key text null, url text null, size_bytes bigint null,
                 storage_path, status, error, summary text null, claimed_at, claimed_by, attempts, created_at)
                 -- the processed unit, one per user and content; unique (owner, dedupe_key)
sources      (id, notebook_id, library_source_id, created_at)   -- the link of a library source into a notebook; its id is the public source id
chunks       (id, library_source_id, idx, page, text, embedding vector(1536))   -- one set per library source, searched from every notebook that links it

-- rag
messages     (id, notebook_id, role, content, citations jsonb, created_at)
outputs      (id, notebook_id, kind, title, content jsonb, status, error, share_token text null unique,
              source_id uuid null, claimed_at timestamptz null, claimed_by text null, attempts int default 0, created_at)

-- shared (owned by core's migration, applied after infra's)
usage        (id, user_id, notebook_id null, service, kind, model,
              tokens_in, tokens_out, cost_cents, created_at)
access_log   (id, at, ip inet, path, method, status, user_id null, service)    -- raw IP, security purpose, 30 days
ip_blocks    (ip inet primary key, blocked_until, strikes int, updated_at)
ip_bans      (ip inet primary key, reason, created_at, lifted_at null)
```

- `sources.kind`: `pdf` | `text` | `url` | `image` | `audio` | `generated` (`audio` is an MP3 or WAV upload transcribed by `STT_MODEL` into one passage without pages, the platform returns no timestamps; `generated` is a finished output indexed as a source by rag, see Outputs).
- `sources.status`: `processing` | `ready` | `failed`. The API also shows `waiting_for_credit`, never stored: a queued (`processing`) source whose owner has no credit, which the ingest worker does not claim until the balance is positive again.
- `sources.summary`: one summary per source, written by ingest at the end of processing, used by the output generators.
- `outputs.kind`: `report` | `quiz` | `table` | `mindmap` | `infographic` | `audio` | `decision` | `chart`. `outputs.status`: `waiting` (sources still processing) | `processing` | `ready` | `failed`.
- `claimed_at`, `claimed_by`, `attempts` on `sources` and `outputs` make those tables the job queues of the two workers (see Workers).
- `chunks.embedding` dimension follows `EMBEDDING_DIMENSION`; the migration takes it as a parameter, default 1536.
- `usage.kind`: `llm` | `embedding` | `tts` | `stt` | `decision`. `usage.service`: `ingest` | `rag`. For `tts` and `stt` the priced unit is seconds of audio as the platform reports them (generated speech for `tts`, the uploaded file for `stt`), rounded up per call: `tokens_in` holds the seconds, `tokens_out` is 0, and `MODEL_PRICES` lists `TTS_MODEL` and `STT_MODEL` with `in` in cents per million seconds. For `decision` it is the input tokens the decision model reports (`usage.input_tokens`; it generates no text): `tokens_in` holds them, `tokens_out` is 0, and `MODEL_PRICES` lists `DECISION_MODEL` with `in` in cents per million tokens.
- **Balance** = `sum(ledger.amount_cents) - sum(usage.cost_cents)` per user. Usage rows are the debit; there is no second ledger write.
- Storage object path: `<owner>/<library_id>/<name>` for an upload, `<notebook_id>/<output_id>/<name>` for a file an output produces (`audio.mp3`). Deleting a library source or an output deletes its objects; detaching a source from a notebook deletes nothing.
- **Source library**, per user: a file or image is deduplicated by `sha256:<hex>` of its bytes (a PDF by bytes plus capture mode), a URL by `url:<normalised URL>`; the same content again links the existing entry instead of storing or processing it (one set of chunks and embeddings per entry, billed once at first processing). Nothing is shared between users; the same bytes from two users are two entries. Generated outputs are library rows of kind `generated` owned by the user, listed by `GET /library?kind=generated` with their origin notebook (`origin: {"id", "title"}`, `id` null once that notebook is deleted); linking one into another notebook (`{"library_id"}`) reuses its chunks at no cost. Rag owns their whole lifecycle (trash, restore, purge, see Outputs); ingest's trash and purge skip kind `generated`, and deleting a notebook or detaching a source removes only links. A new user's first library access seeds five demo sources (a PDF, a text, an image, a URL, a recording, one coherent fictional set; the recording carries facts no other source has, and existing users get it only through a demo reset) processed at the demo user's expense, flagged `demo` (`ingest/context/library.md`). Removing a source from a notebook detaches the link; deleting an entry in the library detaches it everywhere and moves it to the trash, where it stays for the user's `trash_days` preference (default 14) before the ingest worker purges it with its chunks and object, hourly and at start; it can be restored or purged at once meanwhile, and uploading the same bytes again restores it (`ingest/context/library.md`). Deleting a notebook detaches its sources and frees no storage (`ingest/context/library.md`).

## Credits and accounting

- Prices per model live in config (`MODEL_PRICES`, JSON, cents per million tokens, in and out), with defaults in core. The writer of a usage row prices it: `cost_cents = ceil(tokens_in * price_in + tokens_out * price_out)`.
- Core provides `record_usage(conn, user_id, notebook_id, service, kind, model, tokens_in, tokens_out)`, `balance_cents(conn, user_id)`, `has_credit(conn, user_id)` and `is_admin(conn, user_id)` (async twins with an `a` prefix), plus `lasteverlm_core.web.admin_only`, the FastAPI dependency behind every admin endpoint of ingest and rag. Services call these, never write `usage` by hand.
- Core provides the object-storage client (`lasteverlm_core.storage`: put, get, delete, presigned GET with an expiry). Ingest and rag use it; no service carries its own S3 code.
- A voucher is a single-use code; redeeming it writes one positive ledger row. Default amount `VOUCHER_DEFAULT_CENTS` (1000 in code, 500 in production). The code is stored hashed and shown once; the admin list never shows it.
- Before any model call a service checks `has_credit`, i.e. `balance_cents > 0` for every user, admins included; otherwise 402 `{"error": "credit exhausted"}` (`core/context/credit.md`). The maintainer tests with vouchers like everyone else.

## Authentication

- Bearer token on every request. Two schemes, resolved by one function in core, `current_user(authorization) -> User`:
  - JWT, HS256 with `AUTH_JWT_SECRET`, `sub` = user id, 7 days, issued by account after a magic link.
  - `DEMO_TOKEN` from config, resolves to the demo user `00000000-0000-4000-8000-000000000001`, seeded by account's migration with email `demo@localhost` and an initial ledger credit. Exists for development and recorded demos; unset in production.
- Every service answers `GET /healthz` with `{"status", "version", "commit"}` from `APP_VERSION` and `APP_COMMIT` (the image tag and the full SHA, set by the chart; "unknown" when unset), plus `"dependencies"`, `{"postgres"?, "storage"?, "inference"?: "ok" | "down"}`, for the dependencies the service uses once it runs core's `DependencyProbe` (checked at most every 15 s, 2 s each). `status` stays `"ok"` while the process answers; a dependency outage never fails the liveness probe.
- Magic link: `POST /api/account/auth/request-link` answers 202 for an address that may sign in and 403 `{"error", "code": "not_allowed"}` for any other, no mail in that case (`account/context/auth.md`); the mail carries `<PUBLIC_BASE_URL>/auth/callback?token=...` and the token itself for the paste form; the frontend exchanges the token at `GET /api/account/auth/callback?token=...` for `{jwt, user}`. Tokens are stored hashed, single use, 15 minutes.
- Who may sign in: addresses in `ADMIN_EMAILS`, addresses on the allowlist (`settings` key `allowed_addresses`) and addresses whose domain is on it (`settings` key `allowed_domains`, seeded once from `AUTH_ALLOWED_DOMAINS`). Both lists are edited entry by entry in the admin panel. Not in any repository.
- Roles: `user`, `viewer`, `admin` (`users.role`). The first user ever created and addresses in `ADMIN_EMAILS` are admins; a user whose domain is in the allowlist's `viewer_domains` signs in as viewer; an admin changes roles in the panel. A viewer reads every admin endpoint of account, ingest and rag (core's `admin_read` dependency); every write stays admin only (`admin_only`). `is_admin` is kept in sync with the role for compatibility.
- The JWT lives 24 hours (`JWT_TTL_DAYS`, default 1); sign-out is client-side; the secret must be at least 32 bytes.

## API contract v1

JSON in and out unless stated. Errors: `{"error": "<message>"}` with 4xx/5xx, plus a stable `"code"` where a client must tell refusals apart (today `not_allowed`). 401 unauthenticated, 402 credit exhausted, 403 not admin, 404 not yours or missing.

### account, prefix `/api/account`

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/auth/request-link` | `{"email"}` | 202, or 403 `code: not_allowed`, or 429 `code: too_many_links` (five links per address per hour) |
| GET | `/auth/callback?token=` | | `{"jwt", "user": User}` |
| GET | `/me` | | `User` |
| GET, PUT | `/me/preferences` | PUT: any subset, `null` clears | `Preferences` |
| POST | `/vouchers/redeem` | `{"code"}` | `User` |
| POST | `/visits` | `{"path"}` | 204 |
| GET | `/admin/users` | | `[AdminUser]` (viewer or admin) |
| PATCH | `/admin/users/{id}` | `{"role": "user"\|"viewer"\|"admin"}` | `AdminUser`; 409 for the last admin or the demo user |
| GET | `/admin/logins?limit=` | | `[{"user_id","email","at"}]` |
| GET | `/admin/visits?days=` | | `[{"day","path","visits","unique"}]` |
| GET | `/admin/settings/smtp` | | `Smtp` (read-only, from the environment, never the password) |
| POST | `/admin/settings/smtp/test` | | 202 |
| GET | `/admin/system` | | `SystemInfo`: version and commit of the build, every service's health and build, pods and deployments from the cluster API (`cluster: false` and empty lists outside a cluster or without the read role) |
| GET | `/admin/settings/allowlist` | | `Allowlist` |
| POST | `/admin/settings/domains` | `{"domain"}` | 201 `Allowlist`; 409 listed, 422 bad value |
| DELETE | `/admin/settings/domains/{domain}` | | 204 |
| POST | `/admin/settings/addresses` | `{"address"}` | 201 `Allowlist`; 409 listed, 422 bad value |
| DELETE | `/admin/settings/addresses/{address}` | | 204 |
| POST | `/admin/vouchers` | `{"amount_cents", "count"}` | `{"codes": []}` once |
| GET | `/admin/vouchers?limit=` | | `[AdminVoucher]`, newest first |
| DELETE | `/admin/vouchers/{id}` | | 204 open; 409 redeemed |
| GET | `/admin/login-attempts?limit=` | | `[{"at","email","ip","outcome": "allowed|refused"}]`, newest first, kept `RETENTION_DAYS` |
| GET, PUT | `/admin/settings/ratelimit` | PUT: `{"per_minute","block_minutes","ban_after_strikes","strike_window_hours"}` | `RateLimitRules` |
| DELETE | `/admin/settings/ratelimit` | | 204, back to the environment defaults |
| GET | `/admin/stats` | | `AccountStats` |
| DELETE | `/admin/users/{id}` | admin only | 204; deletes a user and everything they own, orchestrated: rag's purge, then ingest's purge (both on the admin's bearer, `RAG_URL`/`INGEST_URL`), then account's rows (the user, usage, the login log); vouchers and `login_attempts` stay as history. 409 for the caller's own account and the demo user; 502 `{"error", "code": "purge_failed", "step": "rag"|"ingest"|"account", "message"}` when a step fails, with nothing of account's touched, so the user is still listed and the call can be repeated |
| GET | `/healthz` | | `{"status": "ok"}` |

```json
User:      {"id", "email", "role": "user"|"viewer"|"admin", "is_admin", "balance_cents", "created_at", "preferences": Preferences}
Preferences: {"ui_language": "de"|"en"|null, "output_language": <code>|null, "audio_voice": <voice id>|null, "trash_days": 1..365|null}  -- null: not set; the web resolves defaults
AdminUser: {"id", "email", "is_admin", "created_at", "last_login_at", "balance_cents", "usage_cents"}
Smtp:      {"host", "port", "username", "from_address", "starttls"}   -- empty host: mail not configured
Allowlist: {"allowed_domains": [], "allowed_addresses": [], "viewer_domains": []}   -- viewer_domains: users from these domains sign in as viewers
AdminVoucher: {"id", "amount_cents", "created_by", "created_at", "redeemed_by", "redeemed_at", "status": "open|redeemed"}
RateLimitRules: {"per_minute", "block_minutes", "ban_after_strikes", "strike_window_hours", "ban_max_days", "retention_days"}  -- the last two read-only
SystemInfo: {"version", "commit", "cluster", "services": [{"name", "version", "commit", "healthy", "dependencies": {"postgres"|"storage"|"inference": "ok"|"down"} (what the service's healthz reports, null until it does)}], "pods": [{"name", "service", "image_tag", "ready", "restarts", "node", "started_at", "phase", "reason" (the container's waiting or terminated reason, or a termination within the last ten minutes, null when running), "template_hash"}], "deployments": [{"name", "replicas", "ready", "updated", "progressing" (true only while a rollout is under way), "available", "replica_sets" (counted from the pods' template hashes)}]}. The admin's architecture diagram colours its nodes from this: green when every replica is ready without restarts, amber for a rollout or partial readiness, red only for failure (a failing pod reason, climbing restarts, none ready outside a rollout, healthz down), grey when unknown; the external boxes take `dependencies`.
AccountStats: {"users", "logins_24h", "vouchers": {"issued", "redeemed", "open"}, "credits_granted_cents",
               "usage_cost_cents": {"total", "last_24h"}, "blocks_active", "bans_active"}
```

### ingest, prefix `/api/ingest`

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/notebooks` | | `[Notebook]` |
| POST | `/notebooks` | `{"title"}` | `Notebook` |
| DELETE | `/notebooks/{id}` | | 204 |
| GET | `/notebooks/{id}/sources` | | `[Source]` |
| POST | `/notebooks/{id}/sources` | multipart `file` (+ field `capture`: `auto`\|`text`\|`vision` for PDFs), or `{"url"}`, or `{"library_id"}` | 202 `Source` (new link, `status: "processing"` when newly processed); 200 `Source` when already in this notebook |
| DELETE | `/sources/{id}` | | 204, detaches the source from the notebook only |
| GET | `/library` | | `[LibraryEntry]`, newest first, the user's own, never generated |
| GET | `/library/{id}` | | `LibraryEntry` |
| DELETE | `/library/{id}` | | 204, detached from every notebook and moved to the trash (chunks stay) |
| GET | `/library/trash` | | `[TrashEntry]`: the entry plus `deleted_at` and `purge_at` |
| POST | `/library/{id}/restore` | | 204, back in the library, not re-linked |
| DELETE | `/library/{id}/purge` | | 204 at once, from the trash only (404 otherwise): chunks and object go |
| GET | `/admin/stats` | viewer or admin | `IngestStats` |
| DELETE | `/admin/users/{id}` | admin only | 204; account's second step when a user is deleted: library objects (originals, converted audio), library rows, trash, notebooks and their sources (rag's rows cascade from the notebooks); idempotent; objects before rows, a storage error is a 502 naming the key |
| GET | `/healthz` | | `{"status": "ok"}` |

```json
Notebook: {"id", "title", "created_at"}
Source:   {"id", "notebook_id", "library_id", "name", "kind", "capture" (PDFs), "demo", "preview_url" (image, PDF, text, audio: presigned GET, 1 h; url: the URL; for audio the file that was transcribed, the converted MP3 when the upload was converted), "original_url" (audio only: presigned GET of the uploaded original when it was converted, else null), "status", "error", "created_at"}
LibraryEntry: {"id", "name", "kind", "capture" (PDFs), "demo", "preview_url" (image, PDF, text, audio: presigned GET, 1 h; url: the URL; for audio the file that was transcribed, the converted MP3 when the upload was converted), "original_url" (audio only: presigned GET of the uploaded original when it was converted, else null), "status", "error", "size_bytes", "url", "created_at", "notebooks": [{"id", "title", "source_id"}]}
IngestStats: {"sources": {"processing", "ready", "failed"}, "jobs": {"pending", "running", "failed"}, "chunks", "source_bytes"}
```

Status codes: `POST /notebooks` 201, `POST /notebooks/{id}/sources` 202, `DELETE` 204, `{"url"}` 501 until the url loader exists.

Processing runs after the upload response: store the file, parse,
chunk (about 800 tokens, 100 overlap), embed, insert chunks, set
`ready` or `failed` with `error`. The pipeline is the `worker`
entrypoint of the ingest package; in v1 it runs in-process as a
background task, later as its own pod with no code change. No queue in
v1; the frontend polls `GET /sources`.

### rag, prefix `/api/rag`

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/notebooks/{id}/conversations` | | `[Conversation]` |
| POST | `/notebooks/{id}/conversations` | `{"title"}?` (default "Chat N") | `Conversation` |
| DELETE | `/conversations/{id}` | | 204, messages included (closing a chat tab) |
| GET | `/conversations/{id}/messages` | | `[Message]` |
| GET | `/chunks/{id}` | | `ChunkView` (the cited passage verbatim; 404 unless the caller owns it) |
| POST | `/notebooks/{id}/ask` | `{"question", "conversation_id", "scope": {"output_id"}?}` | `Answer`; 422 without a conversation |
| PATCH | `/conversations/{id}` | `{"title"}` (1–80 chars) | `Conversation` |
| POST | `/outputs/voice-sample` | `{"voice", "speed"?, "language"?}` | `{"url", "cached"}`: a short MP3 preview, synthesized once per voice/speed/language and then served from storage (one `tts` usage row for the first caller, 402 without credit, 503 without TTS) |
| GET | `/outputs/options` | | `{"languages": [{"code","label"}], "audio": {"voices": [{"id","label","gender"}], "speeds": [], "default_voice"}}` |
| POST | `/notebooks/{id}/outputs` | `{"kind", "options"?}` — options per kind: `{"language"}` for text kinds, `{"language", "voice", "speed"}` for audio; 422 outside the catalogue | 202 `Output` with status `waiting` or `processing` |
| GET | `/notebooks/{id}/outputs` | | `[Output]` |
| GET | `/library/outputs?kind=&trash=` | | `[Output]`, the user's outputs across notebooks (`trash=true`: the trashed ones) |
| GET | `/outputs/{id}` | | `Output` |
| DELETE | `/outputs/{id}` | | 204, moves the output to the trash (`deleted_at`), its generated library row with it |
| POST | `/outputs/{id}/restore` | | `Output` |
| DELETE | `/outputs/{id}/purge` | | 204; 409 unless trashed |
| DELETE | `/notebooks/{id}/outputs` | | 204, trashes every output of the notebook; the web calls it before ingest's notebook delete when the user chooses to delete the documents too |
| POST | `/outputs/{id}/share` | | `{"share_token"}` |
| DELETE | `/outputs/{id}/share` | | 204 |
| GET | `/shared/{token}` | no auth | `Output` without ids, read-only |
| GET | `/admin/stats` | viewer or admin | `RagStats` |
| DELETE | `/admin/users/{id}` | admin only | 204; account's first step when a user is deleted: the stored files of the outputs in the user's notebooks, then rag's rows there, in one transaction; idempotent; a file that cannot be deleted is a 502 naming its key and no row is touched |
| GET | `/healthz` | | `{"status": "ok"}` |

```json
Answer:   {"message_id", "answer", "citations": [Citation], "units", "check"}
Citation: {"n", "source_id", "source_name", "source_kind", "chunk_id", "page", "quote" (verbatim chunk text), "similarity", "chunk_index", "chunk_count"}
Conversation: {"id", "notebook_id", "title", "created_at"}
Message:  {"id", "conversation_id", "role", "content", "citations", "units", "check", "created_at"}
Output:   {"id", "notebook_id" (null once the origin notebook is deleted), "origin_title", "kind", "title", "content", "status", "error", "share_token", "source_id" (the generated library row), "options", "deleted_at", "purge_at", "provenance", "created_at"}
ChunkView: {"chunk_id", "source_id", "source_name", "source_kind", "page", "text", "chunk_index", "chunk_count"}
RagStats: {"notebooks": {"total"}, "messages": {"total", "last_24h"}, "outputs": {"by_kind": {<kind>: n}, "by_status": {<status>: n}},
           "jobs": {"pending", "running", "failed"}, "shared": {"outputs"}}
```

`scope.output_id` restricts retrieval to the chunks of one generated
document ("ask about this"); an output that is not `ready` answers 409
`{"error": "output is not ready"}` so the frontend can tell "wait" from
"gone". `source_kind` lets the frontend render a
citation into a PDF differently from one into a generated report.

Citations are built by rag, not by the provider: retrieved chunks go
numbered into the prompt, the model answers with `[n]` markers, rag maps
every marker back to its chunk and drops markers that point at nothing.
Retrieval: top-k (8) by cosine similarity over `chunks`, filtered by
`notebook_id` and sources with status `ready`; a similarity floor from
config. If nothing passes the floor, the answer says that the sources do
not cover the question and `citations` is empty. The service never
answers from model knowledge alone.

## Workers: PostgreSQL as the job queue

Background work never runs inside an API process. ingest and rag each
ship a second entrypoint, `worker`, deployed from the same image as a
separate Deployment with its own replica count. A worker loop claims
one job at a time:

```sql
update sources set claimed_at = now(), claimed_by = $worker, attempts = attempts + 1
where id = (select id from sources
            where status = 'processing' and (claimed_at is null or claimed_at < now() - interval '10 minutes')
            order by created_at for update skip locked limit 1)
returning *;
```

The same shape on `outputs` with status `waiting`/`processing`. A claim
older than ten minutes is considered abandoned and may be re-claimed;
after three attempts the row becomes `failed` with the last error. No
broker, no extra component: PostgreSQL's row locks make any number of
workers safe, and a dead pod costs at most one retry delay. The API
only inserts rows and reads status; the frontend polls.

## Outputs (generated documents)

An output is a document generated from a notebook's sources: a report,
a quiz, a data table, a mind map, an infographic, or an audio version.
The rag service owns them. One interface, `OutputGenerator`, one module
per kind, each with a prompt, an output schema and a renderer in web.

| Kind | `content` | Rendered as |
|---|---|---|
| `report` | `{"markdown", "units": [{"text", "refs", "support"}]}` | Markdown document with `[n]` markers; one unit per non-heading paragraph |
| `quiz` | `{"questions": [{"question", "options", "answer", "explanation", "refs", "support"}]}` | interactive quiz with reveal |
| `table` | `{"columns": [], "rows": [[]], "row_refs": [{"refs", "support"}]}` | sortable table, CSV export; `row_refs[i]` belongs to `rows[i]` |
| `mindmap` | `{"root": {"label", "children": [], "refs", "support"}}` | markmap in the browser; every node carries its refs, the root is no claim unless it cites |
| `infographic` | `{"headline", "facts": [{"label", "value", "refs", "support"}], "sections": [{"title", "text", "refs", "support"}]}` | card grid |
| `audio` | `{"url", "transcript": [{"speaker": "narrator"|"interviewer"|"expert", "text", "refs", "support"}]}` | audio player with the transcript under it. Options: `audio_style` = `overview` (default) | `story` (one narrator, a narrative) | `conversation` (an interviewer and an expert, two voices: `voice` and `audio_voice_2`, which defaults to the next platform voice of the same language and must differ from the first; 422 otherwise or with another style); the catalogue lists `audio.styles` and `audio.default_style`. The row stores `{"key"}` (the object key); every read (GET output, the list, `shared/{token}`) replaces it with a presigned GET URL valid for one hour, because an `<audio>` element cannot send a bearer header. The script is written by the LLM and synthesized in sentence-boundary segments by `TTS_MODEL` with `TTS_VOICE`; the MP3 frames are concatenated |

Provenance: every unit of every kind (a report paragraph, a quiz
question, a table row, a mind-map node, an infographic fact or section,
a transcript paragraph or turn) carries `refs`, the numbers of the
passages it cites, and `support`: `supported` when a deterministic
word-overlap check (`lexical-overlap-v1`, threshold 0.5, no model call)
finds at least half of the unit's content words in its best cited
passage, `unsupported` when it does not, `unverified` when it does not
and the output was written in another language than the material
(word overlap says nothing across languages); absent means the unit is
no claim. Failing units are kept and marked, never dropped, and the
output is never blocked. `Output.provenance` is the generation log:
`{version, prompt_version, model, options, focus_query, retrieval: {k,
floor, candidates: [{chunk_id, source_id, source_name, page,
similarity, in_prompt}]}, passages: [{n, chunk_id, source_id,
source_name, source_kind, page, chunk_index, chunk_count, similarity,
quote}], usage: {llm, embedding, tts}, started_at, finished_at, check:
{method, threshold, supported, unsupported, unverified}}`; the shared
view carries it without `chunk_id`, `source_id` and the source
selection, so a shared document is checkable without login
(`rag/context/citations.md`). Chat answers go through the same check
(`Message.units`, `Message.check`).

How a generator gets its input: every source carries a `summary`
written by ingest; the generator takes all summaries of the notebook
plus the top-k chunks for its own focus query. That keeps large
notebooks inside the context window.

Lifecycle: `POST /outputs` creates the row with status `waiting` when
any source of the notebook is still processing, otherwise `processing`;
the rag worker claims it (a `waiting` row is claimable once every
source of its notebook is `ready` or `failed`), generates, writes
`ready` or `failed`. Every generation writes a `usage` row. When an output is
ready, rag chunks and embeds its text as a source of kind `generated`
in the same notebook, so the chat can answer questions about generated
documents and cite them. Deleting an output deletes that source.

Ownership: an output belongs to the user (`outputs.owner`); the notebook it was made in is its origin (`notebook_id`, set to null when that notebook is deleted, `origin_title` kept for display). Deleting an output moves it to the trash for the user's `trash_days`; the rag worker purges trashed outputs at start and hourly: the stored file, then the generated library row, then the output row (`rag/context/outputs.md`).

Sharing: `POST /outputs/{id}/share` sets a random `share_token`; the
page `/s/<token>` in web renders `GET /api/rag/shared/<token>` without
login, read-only, and records a visit. `DELETE .../share` revokes it.
Printing is a print stylesheet on the viewer; the browser produces the
PDF. Audio, mind map and infographic are not printed as images; the
report and the table are the printable kinds.

## Rate limiting, blocking, banning, access log

Two layers, both per client IP (`X-Forwarded-For` as set by the
ingress, never trusted from anywhere else):

1. **Ingress**, coarse: ingress-nginx rate-limit annotations (requests
   per second with a burst) answer 503 before anything reaches a pod.
   Access log as JSON on the ingress.
2. **Core middleware**, fine, loaded by every API service (the client address comes from the PROXY protocol between the load balancer and ingress-nginx, which rewrites `X-Forwarded-For`; pods accept nothing but the ingress through NetworkPolicies; IPv6 clients are keyed by their /64; a database failure in the limiter fails closed with 500): a sliding
   window per IP in PostgreSQL (`ip_blocks`). Over the limit means 429
   and `blocked_until = now() + 15 minutes`, `strikes + 1`. The third
   strike within 24 hours writes an `ip_bans` row; a banned IP gets 403
   on everything until an admin lifts it, at most 90 days. Every request
   writes one `access_log` row (sampled to 1 in N for `/healthz`).

The rule's five values (requests per minute, block minutes, strikes
to a ban, strike window, ban cap) have their defaults in configuration
(`RATE_LIMIT_PER_MINUTE` 120, `BLOCK_MINUTES` 15, `BAN_AFTER_STRIKES` 3,
`STRIKE_WINDOW_HOURS`, `BAN_MAX_DAYS` 90) and can be overridden at run time: the admin
panel writes them to the `settings` table (key `ratelimit`), and core's
middleware in every service re-reads that row at most every 30 seconds
(`core/context/ratelimit.md`). Blocks, bans, the access log and the
sign-in attempts (e-mail, IP, allowed or refused) are visible in the
admin panel; bans can be lifted there.
Raw IPs exist only in these tables and in `login_attempts`, for the security purpose.
The access log, expired blocks and lifted bans are purged after
`RETENTION_DAYS` (30); an active ban survives that purge and falls after
the ban cap (`BAN_MAX_DAYS`, 90, or the stored rule) at the latest. The account service runs the purge at
every start with core's statements. The visitor log keeps hashes.

## Model prices

`MODEL_PRICES` is JSON: `{"<model>": {"in": <cents per million tokens>, "out": <cents per million tokens>}}`;
`cost_cents = ceil((tokens_in * in + tokens_out * out) / 1e6)`. Core
ships defaults for the models on the platform; a service refuses to
start when its configured model has no price (fail loud). Until the
defaults exist, every deployment sets `MODEL_PRICES`.

## Configuration

| Variable | Used by |
|---|---|
| `DATABASE_URL` | account, ingest, rag |
| `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | ingest, rag (audio files) |
| `INFERENCE_BASE_URL`, `INFERENCE_API_KEY` | ingest, rag |
| `LLM_MODEL` | ingest (source summaries), rag |
| `STT_MODEL` (default `whisper-large-v3-turbo`; MP3 and WAV only, 25 MiB, 4 h) | ingest (audio sources) |
| `DECISION_MODEL` (default `clef`; `clef-flash` costs half per token) | rag (Decision outputs) |
| `JUDGE_MODELS` (JSON level → model, levels `low`, `medium`, `high`; defaults in core; every model needs a price) | rag (answer judge) |
| `SIMILARITY_FLOOR`, `TTS_MODEL`, `TTS_VOICE` (default `ryan`; the platform's voices are not the OpenAI names) | rag |
| `EMBEDDING_MODEL`, `EMBEDDING_DIMENSION` | ingest, rag (identical on both) |
| `MODEL_PRICES` | ingest, rag (JSON; defaults in core) |
| `AUTH_JWT_SECRET` | account (issues), ingest, rag (verify) |
| `DEMO_TOKEN` | all services; unset in production |
| `INGEST_URL`, `RAG_URL` (defaults `http://ingest`, `http://rag`) | account (System page probes, user deletion) |
| `JWT_TTL_DAYS` (1), `LLM_TIMEOUT_SECONDS` (60; the rag worker uses 180) | account, rag |
| `PUBLIC_BASE_URL`, `AUTH_ALLOWED_DOMAINS` (seed only), `ADMIN_EMAILS`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_ADDRESS`, `SMTP_STARTTLS` | account |
| `VISIT_HASH_SECRET` | account (shared by its replicas: daily visitor-hash salt = HMAC(secret, date); without it each process salts on its own) |
| `API_BASE_URL` | web (build time) |

## Runtime contract

- Images `ghcr.io/oliver-zehentleitner/lasteverlm-<service>:<git-sha>` and `:<tag>` for account, ingest, rag, web, built from `<service>/Dockerfile` with the repository root as build context so that `core/` can be copied in.
- API services listen on 8000 and answer `GET /healthz` with `{"status": "ok"}` at the root (what Kubernetes probes) and under their prefix (what the ingress exposes); web serves on 8080, `GET /` is 200. Services receive the full request path including their prefix.
- Containers run as non-root (`runAsNonRoot: true`); every Dockerfile sets a `USER`.
- Configuration: ConfigMap `lasteverlm-config` (PUBLIC_BASE_URL, S3_ENDPOINT, S3_BUCKET, INFERENCE_BASE_URL, LLM_MODEL, EMBEDDING_MODEL, EMBEDDING_DIMENSION, SIMILARITY_FLOOR, MODEL_PRICES). Secrets per concern: `lasteverlm-db` (DATABASE_URL, app role, `sslmode=require`), `lasteverlm-s3` (ingest), `lasteverlm-inference` (ingest, rag), `lasteverlm-auth` (AUTH_JWT_SECRET, DEMO_TOKEN), `lasteverlm-account` (AUTH_ALLOWED_DOMAINS, ADMIN_EMAILS), optional `lasteverlm-smtp` (the mail configuration, read at start). Account gets neither S3 nor inference.
- Migrations: each service keeps `<service>/db/migrations/NNNN_<topic>.sql`; `make migrate` applies infra, core, account, ingest, rag in that order, one transaction per file, version key `<service>/NNNN_<topic>` in `schema_migrations`, never edit an applied file. The psql variable `:EMBEDDING_DIMENSION` is passed on every run. `CREATE EXTENSION vector` is infra's own first migration; services never create extensions. A migration runs as the application role (`SET ROLE lasteverlm`, the admin connection is a member of it); it must not rely on superuser rights or on owning objects the application role does not own.

## Deployment

One Helm chart at `infra/chart`, chart version equal to the repository
tag, one release `lasteverlm` in namespace `lasteverlm` on a Vultr
Kubernetes cluster. The chart holds a Deployment, a Service and a
HorizontalPodAutoscaler per API service (minimum two replicas), a
Deployment per worker (ingest, rag), the web Deployment, the Ingress
with TLS from cert-manager and the rate-limit annotations, the
ConfigMap, and a migration Job that runs before the rollout. Secrets
are created from the environment by `make secrets` and never
committed. The cluster runs in Frankfurt; Object Storage has no
Frankfurt location and lives in Amsterdam.

Automation in `.github/workflows/`:

- `ci.yml`: per directory, path-filtered, `make check` (and `build` for web).
- `docker.yml`: on push to `main` and on tags `v*`, build the changed images and push them to ghcr.io tagged with the SHA (and the tag).
- `deploy.yml`: on tags `v*`, after the images exist, `helm upgrade --install` with that tag; also runnable by hand with a tag as input. A merge to `main` never deploys.
- `ktw-lint.yml`, `pages.yml` (docs site and the Keep the Why dashboard of the whole family).

Versioning: SemVer tags `v*` on the repository. One tag releases
everything at once; services that did not change get the same image
tag from an unchanged SHA layer.

## Extension points, by design

| Later feature | Where it goes | What stays untouched |
|---|---|---|
| URL and image sources | one more `SourceLoader` in ingest | API, schema, rag, web beyond a kind label |
| another LLM or embedding provider | one more provider module in core, selected by config | every service |
| hybrid retrieval (full text + vector) | one more `Retriever` in rag | ingest, web |
| another output kind | one more `OutputGenerator` in rag plus its renderer in web | ingest, account, the API |
| more throughput | replicas on the HPA or the worker Deployments | code |
| password or OAuth login | one more auth scheme in account and core's `current_user` | ingest, rag, web beyond the login page |
| web search as a way to add sources (not built, see below) | a search provider in core, one ingest endpoint returning hits, one wizard step; the user picks hits, each becomes a `url` source through the existing loader | the URL loader, dedupe, billing, rag |

Web search was considered and left out on 2026-10-14: the platform has no
search API, so it would add a second vendor with its own key and send
queries to another third party, and the quality of what a search returns
(paywalls, cookie walls, pages that are only JavaScript) is outside this
system's control. The row above is how it would plug in.
