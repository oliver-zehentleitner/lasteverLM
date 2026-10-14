# Context index

Why `core/` is built the way it is. Family-wide reasoning lives in the
repository root's `context/`.

## 0

## 1

## 2

## 3

## 4

## 5

## 6

## 7

## 8

## 9

## A
- [auth.md](auth.md) — one resolver for session JWT and demo token, why both schemes may be active at once and why there is no audience claim; why a secret under 32 bytes is refused; why read-only admin access is its own dependency

## B

## C

- [chunking.md](chunking.md) — why the token-window chunker lives in core with regex tokens and serves both parsed sources and generated documents; why its window is 420 regex tokens after measuring German text
- [credit.md](credit.md) — why the credit check is the same positive balance for every user, admins included (an earlier admin exemption, superseded), and why the admin flag is read from users on every request instead of a JWT claim

## D
- [database.md](database.md) — why core has no database driver, types connections structurally and ships every helper sync and async; why usage has no foreign keys; why the SQL is tested against a throwaway container with a two-column ledger

## E

## F

## G

## H

- [health.md](health.md) — why the build labels in /healthz default to unknown and why each service keeps its own route; why dependency states are reported but never fail liveness

## I

## J

## K

## L

## M

## N

## O

## P
- [packaging.md](packaging.md) — why services install core by path from the same checkout instead of a uv workspace, a lockfile or a published package
- [pricing.md](pricing.md) — why an unpriced model fails the call and the start instead of costing zero, and why the shipped default prices are empty
- [providers.md](providers.md) — why embeddings request their width and why completions run with reasoning off by default; which switch the platform honours; why an unusable embedding vector fails the call; why speech speed is passed through unchecked; why a response without usage is an error; why transcription and speech synthesis are billed per second as the platform reports it; why a lasting 4xx is a permanent error; why the decision model is billed per reported input token and called through the SDK's generic post

## Q

## R
- [ratelimit.md](ratelimit.md) — why the window is counted over access_log excluding 429s, why the middleware is plain ASGI, why the last X-Forwarded-For entry is the client, and why an active ban survives the purge for at most ninety days; why the four thresholds are runtime settings in one database row re-read every thirty seconds; why the ban cap became one of them; why IPv6 is keyed per /64; why the limiter fails closed

## S
- [storage.md](storage.md) — why ingest and rag share one synchronous S3 client from core and why presigned URLs are signed with SigV4; why only a missing key reads as not there; why object_key rejects an unsafe name

## T

## U

## V

## W

## X

## Y

## Z

