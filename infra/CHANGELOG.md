# Changelog

All notable changes to this directory are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.17.0] - 2026-10-14

### Changed

- `MODEL_PRICES` in production and `compose.yaml` adds the judge candidates from the platform's catalog, cents per million input and output tokens: `glm-5.3-flash` 10 / 35, `deepseek-v4.1-flash` 15 / 60, `glm-5.3` 75 / 300.

### Fixed

- `MODEL_PRICES` for `qwen3.8-27b` in production and `compose.yaml`: 15 cents per million input tokens and 100 per million output tokens, as the platform's catalog lists them on 2026-10-14, instead of 100 for both.

## [0.15.0] - 2026-10-14

### Added

- `VOUCHER_KEY` for account's encrypted voucher codes: `make env` generates it once (32 random bytes, standard base64) and keeps it in `build/cluster.env`, `make secrets` puts it into the `lasteverlm-account` Secret, `compose.yaml` has a fixed local value.
- `DECISION_MODEL` in the ConfigMap (`config.decisionModel`, empty = core's default `clef`) for rag's decision outputs; production and `compose.yaml` set `clef` and the `MODEL_PRICES` entries for `clef` (10) and `clef-flash` (5), cents per million input tokens with `out` 0, as the platform reports the usage.

## [0.12.0] - 2026-10-14

### Changed

- `docker.yml` keeps the BuildKit cache in the registry (`ghcr.io/oliver-zehentleitner/lasteverlm-<service>:buildcache`, every stage) instead of the GitHub Actions cache: the repository's Actions caches stood at 11.3 GB against GitHub's 10 GB cap, so stages were evicted and rebuilt (a main build of v0.11.0's commit reused one step of ingest). A long stage, such as a compiled tool, now reruns only when it changes.

## [0.11.0] - 2026-10-14

### Added

- `STT_MODEL` in the ConfigMap (`config.sttModel`, empty = core's default `whisper-large-v3-turbo`) for ingest's audio sources; production and `compose.yaml` set `whisper-large-v3-turbo` and its `MODEL_PRICES` entry, 1111 cents per million seconds of audio (0.00001111 USD per second in the platform's model catalog).

## [0.10.0] - 2026-10-14

### Added

- `docker.yml` attaches a provenance attestation (`mode=max`), an SBOM and the OCI annotations from `docker/metadata-action` to every image.
- `make web-csp-check`: runs the web image (built with `CSP_MODE=report` or given as `WEB_IMAGE`) and walks it in headless Chromium (Playwright image pinned by digest) against the live API with the demo token; lists every CSP violation, console error and failed response, with a canary violation that has to be reported (`scripts/web-csp-check.sh`, `scripts/csp/walk.mjs`).
- `ci.yml`: the Postgres suites of core, account, ingest and rag run in CI, against a PostgreSQL 18 service container with pgvector (`pgvector/pgvector:0.8.7-pg18-trixie`, as in `compose.yaml`); a skipped test fails the step. Before, they ran only where a developer started them with Docker.
- `tofu/.terraform.lock.hcl` is committed: provider version and checksums for Linux and macOS (amd64, arm64).
- Unit tests for `scripts/dburls.py` (`tests/test_dburls.py`): the three derived URLs with their roles, databases, percent-encoded credentials and `sslmode=verify-full`, and no URL on stdout when an input is missing.

### Changed

- web's probes use `/healthz` instead of `/`.

### Fixed

- `make env` failed silently without a tofu state: `tofu output -raw` exits 0 with a warning on stdout, which was written into `build/cluster.env` as values. It now reads `tofu output -json` through `scripts/tofu_outputs.py` (with unit tests) and stops, naming the missing outputs, before the file is written.

### Security

- The ingress access log no longer records the query string (`$args`): the magic-link callback (`/auth/callback?token=…` and `/api/account/auth/callback?token=…`) put sign-in tokens into the controller log. Rolled out with `make platform`.
- The client address no longer comes from a request header: ingress-nginx took `$remote_addr` from the client's own `X-Forwarded-For` (`enable-real-ip` with the default trusted range `0.0.0.0/0`), so core's rate limiter, blocks, bans and the login log trusted an address any client could set; without the header every request carried a node address. The load balancer now sends the PROXY protocol header and nginx reads the address from it (`use-proxy-protocol`); `make platform` sets the load balancer hostname annotation so in-cluster requests keep going through the load balancer. Rolled out with `make platform`, not with a tag.
- Every container (API services, workers, web, the migration Job) runs with a read-only root filesystem and the `RuntimeDefault` seccomp profile; `/tmp` is an `emptyDir` limited to `tmpSizeLimit` (256 MiB). Every pod except `account` sets `automountServiceAccountToken: false`. The migration and bootstrap Jobs run as the image's `postgres` user (uid 70) with dropped capabilities instead of root, and their `postgres` image is pinned by digest (18.6, Alpine 3.24); the migration Job has resource requests and limits.
- NetworkPolicies: no pod in the namespace accepts a connection except from the ingress controller's namespace and, on the API port, from `account` (System page health probes). A pod elsewhere in the cluster, or a compromised pod in the namespace, can no longer reach the services directly, past the edge rate limits and the client address nginx sets. `networkPolicy.enabled` in the values.
- Every action in the workflows is pinned to a commit SHA with its release as a comment, so a moved tag cannot change what runs; `ktw-lint.yml`'s rolling `keep-the-why@lint-latest` stays as it was. `deploy.yml` passes `KUBECONFIG_B64` to the kubeconfig step through `env` instead of expanding the secret inside the script.
- `make up` sets the OpenTofu state to mode 600 (it was created group- and world-readable with the default umask). `SECURITY.md` now says what the state holds: the object storage keys and the cluster's admin credentials, not the database password.

## [0.8.2] - 2026-10-13

### Added

- ServiceAccount `account` with a Role `account-kubernetes-read` (`get`/`list` on `pods` and `apps/deployments`, namespace only) and its RoleBinding; the `account` pods use it with `automountServiceAccountToken: true`, so the admin System page lists pods and deployments. Enabled per service with `kubernetesRead` in the values.

## [0.7.1] - 2026-10-13

### Added

- `APP_VERSION` and `APP_COMMIT` in the ConfigMap (`appVersion`, `appCommit`): `make deploy` sets them to `TAG` and the full SHA of the checked-out commit (`COMMIT`, default `git rev-parse HEAD`), so `/healthz` and the System page show the build instead of `unknown`.

## [0.7.0] - 2026-10-13

### Added

- `PDF_VISION_MAX_PAGES` in the ConfigMap (`config.pdfVisionMaxPages`, default 100 as in ingest): the most pages of one PDF the vision model reads.

## [0.6.1] - 2026-10-13

### Fixed

- `deploy.yml`: the concurrency group `deploy` moved from the workflow to the job, so the skipped runs that every `main` push triggers no longer cancel a pending tag deploy (v0.6.0's run ended `cancelled`).

## [0.6.0] - 2026-10-13

### Fixed

- Smoke test under ingest's per-user library: the uploaded text carries a run id, so every run takes a new source through the pipeline instead of being linked as already ready (the v0.5.1 deploy's smoke step failed on that), and the cleanup deletes the library source after the notebook.

## [0.4.1] - 2026-10-13

### Fixed

- Smoke test follows rag's mandatory conversations: it opens a conversation after the notebook, asks with its `conversation_id` and reads the history from `/api/rag/conversations/{id}/messages`; the v0.4.0 deploy's smoke step failed with 422 without it.

## [0.3.0] - 2026-10-13

### Added

- `VOUCHER_DEFAULT_CENTS` in the ConfigMap (`config.voucherDefaultCents`, default 1000 as in account); production sets 500 (5 EUR).

## [0.2.1] - 2026-10-13

### Changed

- CPU requests lowered to measured use (APIs 50m, workers 100m, web 25m); autoscaler target 80 % instead of 70 %.
- Python in `scripts/` and `tests/` formatted with black (line length 100, `pyproject.toml`); `make check` and CI run `black --check`.
- Production configuration (domain, ACME e-mail, models, prices, embedding dimension, TTS, S3 endpoint) committed in `chart/values-prod.yaml`; `make deploy` and `deploy.yml` read it, `deploy.yml` needs only the `KUBECONFIG_B64` secret (plus optional `DEMO_TOKEN`) and no repository variables; `make smoke` defaults to its domain; the chart fails the render on invalid `modelPrices` JSON.

### Removed

- `make values` and `scripts/values.py`; deploy configuration no longer comes from the environment.

### Fixed

- `deploy.yml` checks out the commit `docker.yml` built (`workflow_run.head_sha`) instead of the tag name, which could be moved in between while secrets are in scope; `GITHUB_TOKEN` limited to `contents: read`.
- `make secrets` writes the SMTP Secret with the names account reads (`SMTP_USERNAME`, `SMTP_FROM_ADDRESS`, `SMTP_STARTTLS` instead of `SMTP_USER`, `SMTP_FROM`) and fails loud on `SMTP_HOST` without `SMTP_FROM_ADDRESS`; the Secret is renamed `lasteverlm-smtp-seed` → `lasteverlm-smtp`, mounted into account as before.

## [0.2.0] - 2026-10-13

### Added

- rag API and worker mount the `lasteverlm-s3` Secret (audio files go to the bucket); `TTS_VOICE` (default `ryan`) in the ConfigMap, from `make values` and the deploy workflow's repository variable.
- `compose.yaml`: local development stack (PostgreSQL 18 with pgvector, RustFS as S3 storage, Mailpit, every service, an nginx gateway routing like the ingress), set up by the same bootstrap and migration scripts as the cluster; `make local-up`, `local-down`, `local-reset`, `local-smoke`.
- `docs/operations.md`: what cert-manager did for the domain and how to check the certificate.

### Changed

- `make deploy` fails loud on a short commit SHA as `TAG`; images carry the full SHA.

### Removed

- An empty file `--aws-sigv4` that an earlier `make bucket` bug had left in the directory.

## [0.1.0] - 2026-10-12

### Changed

- Chart no longer states `runAsUser` per service; the images set numeric `USER` lines.

### Added

- End-to-end smoke test (`tests/smoke.py`): demo-token login, notebook, text upload, ready, ask with citation, cleanup; runs against any URL, with a unit test against an in-process stub.
- Helm chart (`chart/`) for the whole application: ConfigMap, API Deployments with autoscalers, worker Deployments for ingest and rag, Services, Ingress with per-IP rate limiting, ClusterIssuer, migrations as a pre-upgrade hook.
- `Makefile` taking a clean checkout to a running cluster: `up`, `kubeconfig`, `bucket`, `db-trust`, `platform`, `secrets`, `db-bootstrap`, `values`, `migrations`, `deploy`, `package`, `down`; `check`, `test` and `smoke` for verification. Database URLs with `sslmode=verify-full` against the mounted CA; generated application secrets; trusted-IP list on the managed database; TLS-only ingress with HSTS.
- Migration runner (`db/migrate.sh`) that applies each service's `db/migrations/` in order infra, core, account, ingest, rag with the psql variable `EMBEDDING_DIMENSION`; `0001_extensions.sql` creates pgvector.
- OpenTofu configuration for the Vultr Kubernetes cluster and Object Storage (`tofu/`); the managed PostgreSQL is an input (`DATABASE_ADMIN_URL`) with a bootstrap Job for role, database and pgvector; service migrations run under the application role.
- Platform chart values (`platform/`): ingress-nginx with JSON access log, cert-manager, metrics-server.
- Repository workflows (`.github/workflows/`): ci, docker, deploy, ktw-lint, pages.
- Operations runbook (`docs/operations.md`) and Keep the Why context (`context/`).

