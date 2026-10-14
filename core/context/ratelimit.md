# Rate limiting

## The sliding window is a count over access_log

**Id:** b92de8a5-cdbc-4e7a-958f-c9b461e2c554
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** core design, 2026-10-12; parent docs/architecture.md, "Rate limiting"
**Revisit when:** request volume makes the per-request count query visible in the database, or a shared cache enters the platform

The per-minute limit is `count(*)` over `access_log` for the client IP
in the last sixty seconds, excluding rows with status 429. `ip_blocks`
holds only the block end, the strike count and the time of the last
strike.

**Reason:** every request writes an `access_log` row anyway, so the
window exists without a second data structure, is exact rather than a
bucket approximation, and is shared by all replicas of all services
because it lives in the one database. Excluding 429s matters: while a
client is blocked its rejected requests are still logged, and if they
counted, a client that kept retrying would be over the limit again the
moment the block ended, turning one block into a permanent one.

**Rejected alternative:** a request counter and window start in
`ip_blocks`. Fewer rows scanned, rejected because a fixed window lets a
client send twice the limit across a window edge and because the
counter must be reset by someone.

**Rejected alternative:** in-memory counters per pod. Fastest, rejected
because the API services run with at least two replicas behind one
ingress and a limit per pod is a limit multiplied by the replica count.

## Plain ASGI middleware, not a Starlette base class

**Id:** b36407f7-b2e7-4f00-9209-0e7c49150934
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** core design, 2026-10-12
**Revisit when:** the middleware needs to read or rewrite the request body

`RateLimitMiddleware` implements the ASGI callable directly and wraps
`send` to learn the response status. It depends on `anyio` for running
the sync store in a worker thread, not on Starlette or FastAPI.

**Reason:** the middleware has to run the blocking PostgreSQL driver
without stalling the event loop and has to see the final status code of
every response, including streamed ones; the plain protocol gives both
with a few lines. Keeping web frameworks out of core means the library
installs into a worker process that has no HTTP server at all.

**Rejected alternative:** Starlette's `BaseHTTPMiddleware`. Shorter to
write, rejected because it buffers responses, has known interactions
with streaming and background tasks, and would make FastAPI a
dependency of a library two of whose users are workers.

## X-Forwarded-For is trusted by default

**Id:** 77cf17e5-23d0-4679-87dc-080107344514
**Type:** decision
**Status:** superseded
**Superseded by:** cc213071-7d4e-4a2e-8334-697ce37166d2
**Evidence:** confirmed
**Source:** parent docs/architecture.md ("as set by the ingress, never trusted from anywhere else"); core design 2026-10-12
**Revisit when:** a service becomes reachable other than through the ingress, or the ingress is replaced

`client_ip` takes the first `X-Forwarded-For` entry when
`trust_forwarded` is true (the default) and the peer address otherwise.
A value that is not an IP address becomes `0.0.0.0`.

**Reason:** in the cluster the pods are reachable only through
ingress-nginx, which sets the header from the real peer, so trusting it
there is the only way to see the client at all; the peer address would be
the ingress on every request. The default is the cluster case because
that is production; a local run passes `trust_forwarded=False`. The
fallback to a fixed unknown address keeps a malformed header from
breaking the request or the `inet` column, at the cost of lumping all
such clients together, which is acceptable because only a client that
bypassed the ingress could send one.

## An active ban survives the thirty-day purge

**Id:** 51544138-d1a5-41df-aaae-30101ed4315b
**Type:** decision
**Status:** superseded
**Superseded by:** a9ede8b5-5da7-4907-825a-0f7cb6d3b693
**Evidence:** inferred
**Source:** parent docs/architecture.md, "Rate limiting" (two sentences that pull apart: a ban holds until an admin lifts it, and raw IPs are purged after thirty days); core design 2026-10-12
**Revisit when:** the maintainer confirms or changes the retention rule for bans (written while the maintainer was unavailable; the first attended session confirms it)

`purge` deletes `access_log` and `ip_blocks` rows older than the
retention period, but from `ip_bans` only rows whose ban was lifted
before the cutoff. A ban nobody lifted stays, and so does its IP.

**Reason:** the architecture says both that a banned IP is refused until
an admin lifts the ban and that raw IPs are purged after thirty days. The
two cannot both hold for an active ban older than thirty days; read
literally, the purge would quietly end every ban after a month without
any admin seeing it. The security purpose that justifies keeping the IP
is strongest for exactly these rows, so the ban wins. This is the
author's reading, not a confirmed instruction.

**Rejected alternative:** purge bans by age like the other tables.
Rejected because it turns "until lifted" into "for thirty days" without
telling the admin panel.

## An active ban survives the purge for at most ninety days

**Id:** a9ede8b5-5da7-4907-825a-0f7cb6d3b693
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** the maintainer's decision on the ban retention rule, 2026-10-13
**Revisit when:** bans need to outlive ninety days (repeat offenders)

A ban holds until an admin lifts it, but at most `BAN_MAX_DAYS` (default
90). The limiter enforces the cap itself: `is_banned(ip, since)` counts
only an unlifted ban created at or after `now - BAN_MAX_DAYS`, so an
older ban stops refusing requests at the exact moment it expires. The
purge is cleanup, not the rule: `PURGE_SQL` drops from `ip_bans` every
row lifted before the retention cutoff (`RETENTION_DAYS`, 30, like
`access_log` and `ip_blocks`) and every row created before
`ban_cutoff`, lifted or not. Both cutoffs come from `purge_params`, so
the account service, which runs the statements at start on its async
pool, passes one dict. Until that purge runs, the admin panel's list may
still show an expired ban.

**Reason:** the architecture's two rules pulled apart for an active ban
older than the retention period: "until an admin lifts it" would keep a
raw IP forever, "purged after thirty days" would end every ban after a
month without anyone deciding it. The maintainer settled it with an upper
bound: long enough that a ban outlasts any burst and an admin has time to
look, short enough that no raw IP is kept indefinitely.

**Rejected alternative:** an active ban is never purged (the earlier,
unconfirmed reading). Rejected by the maintainer: it keeps personal data
without a time limit.

**Rejected alternative:** bans purged at `RETENTION_DAYS` like the other
tables. Rejected: thirty days ends a ban too soon for a security measure.

**Rejected alternative:** the purge alone ends a ban. Rejected because
the purge runs when the account service starts, so a ban would outlive
its ninety days by however long the service had been running.

## The four thresholds are runtime settings, re-read every thirty seconds

**Id:** 39da528f-5128-45cb-9393-41240be9cac4
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** the maintainer's request to see and edit the ban and block rules in the admin panel, 2026-10-13; design relayed by the coordinating agent
**Revisit when:** a threshold must apply faster than thirty seconds, or the settings read shows up in request latency

`per_minute`, `block_minutes`, `ban_after_strikes` and
`strike_window_hours` come from account's `settings` table, key
`ratelimit`, a jsonb object with exactly those four integer fields within
`RATE_LIMIT_RULE_BOUNDS`. The env values stay the defaults. Each limiter
re-reads the row through the store it already has at most every thirty
seconds. A missing row means the defaults; an unusable row or an
unreadable table means the defaults plus one warning per distinct
problem. `RateLimitRules.parse` is the one validation, used by the
limiter on read and by account's PUT on write. `BAN_MAX_DAYS` and
`RETENTION_DAYS` stay env only: they govern the purge, not the rule.
`BAN_MAX_DAYS` later became a runtime rule as well; see 121ac29b-a32e-4f87-910b-2508efa7877f.

**Reason:** an operator has to be able to tighten or loosen a threshold
during an incident without a deploy. One row in the database is seen by
every replica of every service, the same place the blocks and bans
already live, so the rule and its state cannot drift apart between
pods. Thirty seconds bounds the extra read to two per minute per process
and still applies a change within the minute an admin expects.

**Rejected alternative:** env only, a redeploy to change a threshold.
Rejected by the maintainer: too slow and too heavy for an incident.

**Rejected alternative:** a per-service cache longer than thirty
seconds. Rejected because an admin who saves a threshold expects it in
force within a minute everywhere.

**Rejected alternative:** a missing row also warns. Not done: no row is
the normal state until an admin first saves the rules, and a warning
every process start would teach operators to ignore the warning that
matters, the malformed row.

## The last X-Forwarded-For entry is the client, never the first

**Id:** cc213071-7d4e-4a2e-8334-697ce37166d2
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** security audit 2026-10-14; ingress-nginx controller template (`nginx.tmpl`, real-ip block and `proxy_set_header X-Forwarded-For`)
**Revisit when:** more than one trusted proxy sits in front of the services, or the ingress is replaced

`client_ip` reads the last non-empty `X-Forwarded-For` entry (repeated
header lines joined in order) when `trust_forwarded` is true, and the
peer address otherwise. `trust_forwarded` (account's
`TRUST_PROXY_HEADERS`, default true) means exactly one thing: every
request reaches the service through exactly one proxy that writes the
header, the cluster's ingress-nginx. A run reachable any other way sets
it false. IPv4-mapped IPv6 addresses count as their IPv4 address and an
IPv6 zone is dropped; anything that is not an address is `0.0.0.0`.

**Reason:** the first entry is whatever the client sent; only the last
one was written by the proxy we trust. Reading the first made the
limiter's key a client choice: a forged header escapes every block and
ban and can get someone else's address banned. With one trusted proxy
the last entry is its own, whether it overwrites the header (the
ingress's default, `X-Forwarded-For: $remote_addr`) or appends to it.

**Alternatives:** the first entry, as before: the client's word.
Counting trusted hops from the right with a configured proxy count:
correct for chains, but there is one proxy, and a count is one more
value to get wrong. The peer address without the header: always the
ingress pod, so every client would share one key.

## IPv6 clients are limited, blocked and banned per /64

**Id:** 1187889d-0b47-448b-a3ce-e1d19bfa37fd
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** security audit of core, 2026-10-14; the coordinator's decision on the finding, the same night
**Revisit when:** abuse arrives from whole /48s, or legitimate users behind one /64 (a shared network) get blocked together

The rules count, block and ban under `limit_key(ip)`: an IPv4 address
as it is, an IPv6 address as its /64 network (`2001:db8:1:2::/64`).
`access_log`, `ip_blocks` and `ip_bans` hold that key. The admin panel
shows it and lifts a ban with it, through a query parameter because of
the slash in it.

**Reason:** an IPv6 subscriber is normally given a whole /64 and can use
any of its 2^64 addresses, so a per-address key let one client step out
of every block and ban by changing its address. The /64 is the smallest
unit one subscriber controls, which is the same thing a single IPv4
address is for an IPv4 client.

**Rejected alternative:** per address, as before. Trivial to evade.

**Rejected alternative:** a /48 or /56. That would catch the rotating
client, but it would also put a provider's other customers under the
same block.

**Rejected alternative:** storing the full address and matching the prefix
in SQL (`ip <<= network`). It keeps more personal data than the rules
need, and every query would change for the same effect.

## The limiter fails closed

**Id:** de1310c3-187c-43b1-a9ee-143b5a648398
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** the coordinator's answer to the core audit, 2026-10-14 ("failing closed is intended")
**Revisit when:** the rate-limit tables move off the main database, or an outage of them is shown to take down requests that the database would not have blocked anyway

When the store cannot answer (the database is down, a query fails),
`RateLimiter.check` raises and the request ends in a 500. The middleware
does not let it through unchecked. A failed access-log write raises
after the response has gone out, so it shows in the logs, not to the
client.

**Reason:** the limiter is a security control, and letting requests
through while it is blind would make an outage an open door for exactly
the clients it exists to stop. Every API service needs the same database
for its real work anyway, so the cases where the limiter fails alone,
while the request could still have succeeded, are rare and short.

**Rejected alternative:** fail open, allowing and logging a warning.
Availability looks better on paper, but a banned client gets in for as
long as the database is unwell.

## The ban cap is a runtime rule too

**Id:** 121ac29b-a32e-4f87-910b-2508efa7877f
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** the maintainer's request to edit the ban cap in the admin panel like the other rules, 2026-10-14, relayed by the coordinating agent
**Revisit when:** the purge has to run on a schedule rather than at account's start

`ban_max_days` joins the stored `ratelimit` row as a fifth field, with
bounds 1 to 365. `BAN_MAX_DAYS` is its default. The limiter's
`is_banned` cutoff uses the current rules' value, so a changed cap
applies within thirty seconds. Account's start-up purge uses the same
value. A row written before this change has no `ban_max_days`; `parse`
takes it from the defaults instead of rejecting the row, so a stored
four-field override keeps working. This revises the earlier choice in
"The four thresholds are runtime settings" that the cap stays env only
because it governs the purge. The cap also decides when the limiter
stops refusing a ban, and that is the part an admin wants to change.
`RETENTION_DAYS` stays env only: it only governs the purge.

**Reason:** an admin who sees a ban in the panel should be able to
shorten or lengthen how long bans hold without a deploy, the same as the
thresholds that create them.

**Rejected alternative:** the cap stays env only (the earlier design).
Rejected by the maintainer: it was the only ban rule the panel could not
change.

**Rejected alternative:** reject a stored row without `ban_max_days`.
Rejected: it would silently drop an admin's existing override, since an
unusable row falls back to the env defaults.

The upper bound of 365 days keeps the limit on how long a raw IP is kept
(the "ban survives the purge for at most" entry) in place.
